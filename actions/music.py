#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
music.py — Recherche et lecture de médias (musique ET vidéo), locale ou en ligne.
Module d'action pour l'assistant Jarvis.

Flux voulu par l'utilisateur :
« mets-moi tel morceau / telle vidéo »
  → l'assistant cherche d'abord dans la bibliothèque locale ;
  → trouvé     : lance directement dans le meilleur lecteur disponible
                 (VLC en priorité, repli automatique mpv/autres si échec) ;
  → pas trouvé : DEMANDE la permission de chercher sur YouTube ; si
                 l'utilisateur confirme, un flux direct est résolu via
                 yt-dlp et lu dans VLC/mpv — jamais un onglet navigateur
                 qui traîne, sauf dernier recours assumé.

Sources :
  - bibliothèque locale (Musique + Vidéos + Téléchargements + Bureau),
    avec correspondance tolérante aux noms de fichiers bruts issus de
    téléchargements (« Artiste_-Titre(256k)_officiel.mp3 ») ;
  - YouTube via yt-dlp (recherche + résolution de flux direct, sans
    téléchargement, sans navigateur).

Robustesse spécifique Arch Linux / Hyprland (Wayland) :
  - VLC (Qt) passe par Xwayland : DISPLAY est réparé (socket X11 réel,
    entêtes wlr-output-* en secours) et QT_QPA_PLATFORM=xcb est forcé,
    avec 3 variantes d'environnement essayées en cascade ;
  - si le lecteur favori échoue, repli automatique sur mpv puis sur les
    autres lecteurs compatibles — on ne répond plus « lancé » à tort ;
  - le succès n'est annoncé que si un flux audio réel est observé
    (pactl/PipeWire), avec secours MPRIS/playerctl et repli « process
    vivant » pour les lecteurs non-MPRIS (navigateurs) ;
  - les casques Bluetooth en profil mains-libres (HFP, 16 kHz mono)
    sont détectés et la lecture est rapatriée sur une vraie sortie
    stéréo ;
  - variables d'environnement toxiques (:99, variables de debug Qt,
    pseudo-proxy) neutralisées.

Le choix du lecteur et la confirmation YouTube sont mémorisés dans
session_memory entre deux tours.
"""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
import threading
import time
import unicodedata
from urllib.parse import quote
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from core.media_search import rank_media_paths

from core import action_kit as kit

_OS = platform.system()  # "Linux" | "Darwin" | "Windows"

try:
    from core.player_ipc import get_player
    _HAS_IPC_PLAYER = True
except ImportError:
    _HAS_IPC_PLAYER = False


# ═══════════════════════════════════════════════════════════════════════════
# Répertoires médias
# ═══════════════════════════════════════════════════════════════════════════

def _xdg_dir(env_var: str, fallback: str) -> Path:
    """Répertoire utilisateur XDG (Linux) avec repli sur le nom usuel."""
    if _OS == "Linux":
        val = os.environ.get(env_var, "")
        if val:
            p = Path(val).expanduser()
            try:
                if p.is_dir():
                    return p
            except OSError:
                pass
    return Path.home() / fallback


MUSIC_DIRS: List[Path] = [
    _xdg_dir("XDG_MUSIC_DIR", "Music"),
    Path.home() / "Musique",
    Path.home() / "Music",
]

VIDEO_DIRS: List[Path] = [
    _xdg_dir("XDG_VIDEOS_DIR", "Videos"),
    Path.home() / "Vidéos",
    Path.home() / "Videos",
]

SHARED_DIRS: List[Path] = [
    _xdg_dir("XDG_DOWNLOAD_DIR", "Downloads"),
    Path.home() / "Téléchargements",
    Path.home() / "Downloads",
    Path.home() / "Desktop",
    Path.home() / "Bureau",
]

AUDIO_EXT: Set[str] = {
    ".mp3", ".flac", ".m4a", ".opus", ".ogg", ".wav",
    ".aac", ".wma", ".mka", ".aiff", ".alac",
}
VIDEO_EXT: Set[str] = {
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv",
    ".wmv", ".m4v", ".ts", ".mpg", ".mpeg",
}
MEDIA_EXT: Set[str] = AUDIO_EXT | VIDEO_EXT

_PENDING_YT_KEY = "music_pending_youtube"
_PENDING_SOURCE_KEY = "music_pending_source"
_LOCAL_RESULTS_KEY = "music_local_results"
_LOCAL_VIDEO_CURRENT_KEY = "music_local_video_current"
_LOCAL_PATH_CACHE: Dict[str, Tuple[float, List[Path], Dict[str, str]]] = {}


# ═══════════════════════════════════════════════════════════════════════════
# Lecteurs connus
# binaire → (libellé, argv pour un fichier local, argv pour une URL/flux)
# `None` en argv = le lecteur ne gère pas ce cas.
# ═══════════════════════════════════════════════════════════════════════════

_PLAYERS: Dict[str, Dict[str, Any]] = {
    "vlc": {
        "label": "VLC",
        "file": ["vlc", "--play-and-exit"],
        "url": ["vlc", "--play-and-exit"],
        "mpris": True,
    },
    "mpv": {
        "label": "mpv (léger, démarre instantanément)",
        "file": ["mpv", "--force-window=yes"],
        "url": ["mpv", "--force-window=yes"],
        "mpris": True,
    },
    "audacious": {
        "label": "Audacious",
        "file": ["audacious"],
        "url": ["audacious"],
        "mpris": True,
    },
    "lollypop": {
        "label": "Lollypop (bibliothèque)",
        "file": ["lollypop"],
        "url": None,
        "mpris": True,
    },
    "firefox": {
        "label": "Firefox (YouTube)",
        "file": None,
        "url": ["firefox"],
        "mpris": False,
    },
    "google-chrome-stable": {
        "label": "Chrome (YouTube)",
        "file": None,
        "url": ["google-chrome-stable"],
        "mpris": False,
    },
}

# Ordre de repli : VLC d'abord (expérience « ça joue, point »), mpv ensuite
# car il est quasi infaillible sous Wayland/Hyprland, puis le reste.
_PREFERRED_ORDER: List[str] = ["vlc", "mpv", "audacious", "lollypop"]
_DEFAULT_PLAYER = "vlc"


# ═══════════════════════════════════════════════════════════════════════════
# Normalisation & correspondance fuzzy
# ═══════════════════════════════════════════════════════════════════════════

_JUNK_RE = re.compile(
    r"\b\d{2,4}k\b|\bofficial\b|\bofficiel(le)?\b|\bclip\b|\baudio\b|\bvideo\b"
    r"|\bvid[ée]o\b|\blyrics?\b|\bparoles?\b|\bhd\b|\b4k\b|\b1080p\b|\b720p\b"
    r"|\b2160p\b|\bmusic\b|\bwebrip\b|\bweb-dl\b|\bx264\b|\bx265\b|\bhevc\b"
    r"|\bavc\b|\baac2?0?\b|\b320kbps\b|\b128kbps\b|\bkbps\b|\bflac\b|\bmp3\b",
    re.IGNORECASE,
)


def _normalize(text: str) -> str:
    """Minuscules, sans accents, ponctuation réduite à des espaces."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = re.sub(r"[_\-.()\[\]{},;:!?'\"/\\]+", " ", text)
    text = _JUNK_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _bigrams(s: str) -> Set[str]:
    return {s[i:i + 2] for i in range(max(0, len(s) - 1))}


def _score(query: str, candidate: str) -> float:
    """Score de correspondance 0→1, tolérant aux fautes de frappe,
    aux variantes singulier/pluriel et aux mots parasites des noms de
    fichiers téléchargés."""
    q = _normalize(query)
    c = _normalize(candidate)
    if not q or not c:
        return 0.0
    q_tokens = [w for w in q.split() if len(w) > 1]
    if not q_tokens:
        return 0.0
    c_tokens = c.split()

    hits = 0.0
    for w in q_tokens:
        if w in c:
            hits += 1.0
            continue
        bg_q = _bigrams(w)
        best = 0.0
        for cw in c_tokens:
            if abs(len(cw) - len(w)) > 3:
                continue
            if bg_q:
                overlap = len(bg_q & _bigrams(cw)) / len(bg_q)
                if overlap >= 0.55:
                    best = max(best, 0.6)
            stem = w[:max(3, len(w) - 1)]
            if cw.startswith(stem) or w.startswith(cw[:max(3, len(cw) - 1)]):
                best = max(best, 0.5)
        hits += best

    score = hits / len(q_tokens)
    if q in c:
        score = min(1.0, score + 0.25)
    return min(1.0, score)


# ═══════════════════════════════════════════════════════════════════════════
# Recherche locale (musique + vidéo)
# ═══════════════════════════════════════════════════════════════════════════

def _walk_media(base: Path, exts: Set[str], max_depth: int = 7):
    """Parcours sûr : garde contre les boucles de symlinks, les erreurs de
    permission et les profondeurs absurdes. Rend des objets Path."""
    stack: List[Tuple[Path, int]] = [(base, 0)]
    seen_dirs: Set[Path] = set()
    try:
        base_rp = base.resolve()
    except OSError:
        return
    seen_dirs.add(base_rp)

    while stack:
        d, depth = stack.pop()
        if depth > max_depth:
            continue
        try:
            entries = d.iterdir()
        except (PermissionError, OSError):
            continue
        for entry in entries:
            try:
                if entry.is_symlink() and not entry.is_file():
                    continue
                if entry.is_file():
                    if entry.suffix.lower() in exts:
                        yield entry
                elif entry.is_dir():
                    if entry.name.startswith(".") or entry.name in {
                        "node_modules", "venv", ".venv", "__pycache__",
                        "build", "dist", "site-packages", "target",
                    }:
                        continue
                    rp = entry.resolve()
                    if rp not in seen_dirs:
                        seen_dirs.add(rp)
                        stack.append((entry, depth + 1))
            except OSError:
                continue


def _search_dirs(dirs: List[Path], exts: Set[str], query: str, limit: int,
                 min_score: float, kind: str) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    seen_bases: Set[Path] = set()
    for base in dirs:
        try:
            if not base or not base.is_dir():
                continue
            rp = base.resolve()
        except OSError:
            continue
        if rp in seen_bases:
            continue
        seen_bases.add(rp)
        for path in _walk_media(rp, exts):
            try:
                s = _score(query, path.stem)
            except Exception:
                continue
            if s >= min_score:
                results.append({
                    "title": path.stem.replace("_", " ").replace(".", " ").strip(),
                    "path": str(path),
                    "score": round(s, 3),
                    "source": "local",
                    "kind": kind,
                })
    results.sort(key=lambda r: -r["score"])
    return results[:limit]


def search_local(query: str, limit: int = 8,
                 min_score: float = 0.45,
                 media_kind: str = "audio") -> List[Dict[str, Any]]:
    """Cherche par nom, prononciation approximative et métadonnées audio.

    Le parcours est fait une seule fois par racine dédupliquée. Le moteur
    enrichit ensuite les résultats avec les tags titre/artiste/album lorsque
    le nom du fichier ne suffit pas à identifier le morceau.
    """
    roots: List[Tuple[Path, Set[str], str]] = []
    if media_kind in {"audio", "all"}:
        roots.extend((path, AUDIO_EXT, "audio") for path in MUSIC_DIRS + SHARED_DIRS)
    if media_kind in {"video", "all"}:
        roots.extend((path, VIDEO_EXT, "video") for path in VIDEO_DIRS + SHARED_DIRS)

    cache_key = media_kind + "|" + "|".join(str(item[0]) for item in roots)
    cached = _LOCAL_PATH_CACHE.get(cache_key)
    if cached and time.monotonic() - cached[0] < 30:
        paths, kinds = list(cached[1]), dict(cached[2])
        return rank_media_paths(
            query=query, paths=paths, kinds=kinds, limit=limit, min_score=min_score
        )
    seen_roots: Set[Tuple[Path, str]] = set()
    paths: List[Path] = []
    kinds: Dict[str, str] = {}
    seen_paths: Set[Path] = set()
    for base, extensions, kind in roots:
        try:
            resolved = base.resolve()
            root_key = (resolved, kind)
            if root_key in seen_roots or not resolved.is_dir():
                continue
        except OSError:
            continue
        seen_roots.add(root_key)
        for path in _walk_media(resolved, extensions):
            try:
                key = path.resolve()
            except OSError:
                key = path
            if key in seen_paths:
                continue
            seen_paths.add(key)
            paths.append(path)
            kinds[str(path)] = kind
    _LOCAL_PATH_CACHE[cache_key] = (time.monotonic(), list(paths), dict(kinds))
    return rank_media_paths(
        query=query,
        paths=paths,
        kinds=kinds,
        limit=limit,
        min_score=min_score,
    )


def _search_local_for_kind(query: str, media_kind: str) -> List[Dict[str, Any]]:
    """Conserve le chemin historique audio tout en ajoutant le filtre vidéo."""
    if media_kind == "audio":
        return search_local(query)
    return search_local(query, media_kind=media_kind)


def _resolve_local_result(results: List[Dict[str, Any]], value: Any) -> Optional[Dict[str, Any]]:
    if not results:
        return None
    text = str(value or "1").strip()
    if text.isdigit():
        index = int(text) - 1
        return results[index] if 0 <= index < len(results) else None
    normalized = _normalize(text)
    for result in results:
        if normalized and normalized in _normalize(result.get("title", "")):
            return result
    return None


def _format_local_results(query: str, results: List[Dict[str, Any]]) -> str:
    lines = [f"Résultats dans ta bibliothèque pour « {query} » :"]
    for index, item in enumerate(results, 1):
        artist = f" — {item['artist']}" if item.get("artist") else ""
        confidence = round(float(item.get("score", 0)) * 100)
        lines.append(f"{index}. {item['title']}{artist} ({confidence}% compatible)")
    lines.append("Dis-moi « lance la 2 » ou précise le titre.")
    return "\n".join(lines)


def _is_confident_local_match(results: List[Dict[str, Any]]) -> bool:
    if not results:
        return False
    first = float(results[0].get("score", 0))
    second = float(results[1].get("score", 0)) if len(results) > 1 else 0.0
    # Un résultat phonétique solide peut être lancé directement. En revanche,
    # deux candidats quasi ex æquo sont présentés à l'utilisateur.
    return first >= 0.66 and (second < 0.60 or first - second >= 0.035)


# ═══════════════════════════════════════════════════════════════════════════
# Recherche & résolution YouTube (sans téléchargement)
# ═══════════════════════════════════════════════════════════════════════════

class YoutubeUnavailable(RuntimeError):
    """Recherche impossible (yt-dlp absent, réseau coupé, YouTube injoignable)."""


def search_youtube(query: str, limit: int = 5) -> List[Dict[str, Any]]:
    """
    Cherche sur YouTube sans rien télécharger.
    Lève YoutubeUnavailable si la recherche n'a pas pu aboutir, afin que
    l'appelant distingue « aucun résultat » de « réseau indisponible » —
    sinon l'assistant annonce à tort que le morceau n'existe pas.
    """
    if not kit.which("yt-dlp"):
        raise YoutubeUnavailable(
            "yt-dlp n'est pas installé (sudo pacman -S yt-dlp).")
    r = kit.run(
        ["yt-dlp", f"ytsearch{limit}:{query}", "--flat-playlist",
         "--dump-json", "--no-warnings", "--no-playlist",
         "--socket-timeout", "10"], timeout=35,
    )
    if r.timed_out:
        raise YoutubeUnavailable("la recherche YouTube a expiré.")
    if r.not_found:
        raise YoutubeUnavailable(r.reason())

    if r.returncode != 0 and not (r.stdout or "").strip():
        err = (r.stderr or "").strip().splitlines()
        detail = err[-1][:160] if err else "cause inconnue"
        raise YoutubeUnavailable(f"YouTube est injoignable — {detail}")

    out: List[Dict[str, Any]] = []
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        vid = d.get("id")
        if not vid:
            continue
        dur = d.get("duration") or 0
        try:
            dur = int(dur)
        except (TypeError, ValueError):
            dur = 0
        views = d.get("view_count") or 0
        try:
            views = int(views)
        except (TypeError, ValueError):
            views = 0
        uploader = d.get("uploader") or d.get("channel") or ""
        out.append({
            "id": vid,
            "title": d.get("title", "sans titre"),
            "url": f"https://www.youtube.com/watch?v={vid}",
            "duration": f"{dur // 60}:{dur % 60:02d}" if dur else "",
            "uploader": uploader,
            "channel": uploader,
            "views": views,
            "thumbnail_url": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
            "source": "youtube",
        })
    return out


def resolve_stream_url(watch_url: str, timeout: int = 15) -> Optional[str]:
    """
    Résout une URL YouTube en URL de flux direct (audio ou combiné) lisible
    immédiatement par VLC/mpv sans ouvrir de navigateur.
    """
    if not kit.which("yt-dlp"):
        return None
    for fmt in ("bestaudio/bv*+ba/b", "bv*+ba/b", "best[ext=mp4]/best", "best"):
        try:
            r = kit.run(
                ["yt-dlp", "-f", fmt, "-g", "--no-warnings",
                 "--no-playlist", "--socket-timeout", "10", watch_url], timeout=timeout,
            )
        except Exception:
            continue
        if r.timed_out or r.not_found:
            continue
        if r.returncode == 0:
            lines = [l.strip() for l in (r.stdout or "").splitlines()
                     if l.strip()]
            if lines:
                return lines[0]
    return None


# ═══════════════════════════════════════════════════════════════════════════
# Lecteurs disponibles
# ═══════════════════════════════════════════════════════════════════════════

def available_players(for_url: bool = False) -> List[Dict[str, str]]:
    """Lecteurs réellement installés, capables de lire une URL si for_url."""
    key = "url" if for_url else "file"
    out: List[Dict[str, str]] = []
    for binary, spec in _PLAYERS.items():
        if binary == "firefox":
            continue
        if spec.get(key) is None:
            continue
        if not kit.which(binary):
            continue
        out.append({"binary": binary, "label": spec["label"]})
    return out


def _default_player(for_url: bool) -> Optional[str]:
    """VLC s'il est installé, sinon le premier lecteur compatible
    selon l'ordre de préférence."""
    players = {p["binary"] for p in available_players(for_url=for_url)}
    if not players:
        return None
    for b in _PREFERRED_ORDER:
        if b in players:
            return b
    return next(iter(players))


def _resolve_player(name: str, for_url: bool) -> Optional[str]:
    """Fait correspondre une réponse utilisateur (« vlc », « 2 ») à un binaire."""
    if not name:
        return None
    players = available_players(for_url=for_url)
    q = _normalize(name)
    if q.isdigit():
        idx = int(q) - 1
        if 0 <= idx < len(players):
            return players[idx]["binary"]
    for p in players:
        if q == _normalize(p["binary"]) or q in _normalize(p["label"]):
            return p["binary"]
    # Correspondance partielle (« chrome » → google-chrome-stable)
    for p in players:
        if q and (q in p["binary"] or p["binary"].split("-")[0] in q):
            return p["binary"]
    return None


def _format_player_list(players: List[Dict[str, str]]) -> str:
    return "\n".join(f"{i}. {p['label']}" for i, p in enumerate(players, 1))


# ═══════════════════════════════════════════════════════════════════════════
# Audio PipeWire/PulseAudio — sorties, flux, casques mains-libres
# ═══════════════════════════════════════════════════════════════════════════

def _audio_sinks() -> List[Dict[str, str]]:
    """Sorties audio connues de PipeWire/PulseAudio."""
    if not kit.which("pactl"):
        return []
    try:
        r = kit.run(["pactl", "list", "sinks", "short"], timeout=5)
    except Exception:
        return []
    sinks: List[Dict[str, str]] = []
    for line in (r.stdout or "").splitlines():
        f = line.split("\t")
        if len(f) >= 5:
            sinks.append({"id": f[0], "name": f[1],
                          "spec": f[3], "state": f[4]})
    return sinks


def _is_handsfree(sink: Dict[str, str]) -> bool:
    """Casque Bluetooth basculé en profil mains-libres (HSP/HFP).
    L'assistant garde le micro ouvert en permanence ; pour lui donner accès
    au micro du casque, PipeWire bascule celui-ci en HFP, où la sortie
    tombe à 16 kHz mono. La musique y part en qualité téléphone — d'où
    l'impression que « rien ne se lance » alors que le lecteur joue."""
    return sink["name"].startswith("bluez_output") and (
        "1ch" in sink["spec"] or "16000Hz" in sink["spec"]
    )


def _sink_inputs_for_pid(pid: int) -> List[Tuple[str, str]]:
    """(index du flux, sink de destination) des flux audio émis par ce process."""
    if not kit.which("pactl"):
        return []
    try:
        r = kit.run(["pactl", "list", "sink-inputs"], timeout=5)
    except Exception:
        return []
    found: List[Tuple[str, str]] = []
    idx: Optional[str] = None
    sink: Optional[str] = None
    for line in (r.stdout or "").splitlines():
        s = line.strip()
        if s.startswith("Sink Input #"):
            idx = s.split("#", 1)[1].strip()
            sink = None
        elif idx and s.startswith("Sink:"):
            sink = s.split(":", 1)[1].strip()
        elif idx and "application.process.id" in s:
            val = s.split("=", 1)[1].strip().strip('"')
            if val == str(pid):
                found.append((idx, sink or ""))
    return found


def _ensure_audible(streams: List[Tuple[str, str]]) -> None:
    """Rapatrie la lecture sur une vraie sortie stéréo si elle part en HFP."""
    sinks = _audio_sinks()
    if not sinks:
        return
    by_name = {s["name"]: s for s in sinks}
    good = next((s for s in sinks
                 if not _is_handsfree(s) and "2ch" in s["spec"]), None)
    if not good:
        return
    for idx, sink_name in streams:
        current = by_name.get(sink_name)
        if current and _is_handsfree(current):
            try:
                kit.run(["pactl", "move-sink-input", idx, good["id"]], timeout=5)
                print(f"[music] son basculé du casque en mains-libres "
                      f"vers {good['name']}.")
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════════════
# Environnement d'affichage (correctif Hyprland/Wayland)
# ═══════════════════════════════════════════════════════════════════════════

def _real_x_display() -> str:
    """Vrai display X de la session (Xwayland sous Hyprland).
    1) sockets /tmp/.X11-unix/X<n> ;
    2) entêtes /run/user/<uid>/wayland-*-lock de wlroots (Hyprland) en
       secours si Xwayland n'a pas encore créé sa socket.
    Sans DISPLAY valide, l'interface Qt de VLC ne se charge pas
    (« no suitable interface module ») et le lecteur tourne sans fenêtre."""
    try:
        socks = sorted(
            p.name for p in Path("/tmp/.X11-unix").iterdir()
            if p.name.startswith("X") and p.name[1:].isdigit()
        )
        if socks:
            return f":{socks[0][1:]}"
    except Exception:
        pass
    try:
        uid = os.getuid()
        locks = sorted(
            p.name for p in Path(f"/run/user/{uid}").glob("wayland-*-lock")
        )
        for lock in locks:
            try:
                with open(lock, "rb") as f:
                    head = f.read(64)
                m = re.search(rb"DISPLAY=(:\d+)", head)
                if m:
                    return m.group(1).decode()
            except Exception:
                continue
    except Exception:
        pass
    return ""


_BAD_DISPLAYS = {"", ":99", ":98", "none", "null"}


def _build_env(variant: int) -> Dict[str, str]:
    """Environnement nettoyé pour lancer un lecteur graphique sous
    Hyprland/Wayland.

    variant 0 : Wayland natif quand il est possible (DISPLAY valide laissé
                tel quel pour les lecteurs GTK/wayland) ;
    variant 1 : XCB forcé + DISPLAY réparé (LE correctif clé pour VLC/Qt
                sous Hyprland : sans QT_QPA_PLATFORM=xcb, Qt tente wayland,
                échoue, et VLC meurt ou joue sans interface ni son) ;
    variant 2 : XCB forcé + DISPLAY vidé (dernier recours : certains
                toolkits se débrouillent seuls via WAYLAND_DISPLAY).
    """
    env = os.environ.copy()

    # Nettoyage des variables toxiques héritées du process parent.
    for k in ("QT_DEBUG_PLUGINS", "QT_QPA_PLATFORMTHEME", "GDK_DEBUG",
              "LD_PRELOAD", "SDL_VIDEODRIVER"):
        env.pop(k, None)
    if env.get("http_proxy") in ("http://:0", "http://localhost:0"):
        env.pop("http_proxy", None)
        env.pop("HTTP_PROXY", None)

    wayland = bool(env.get("WAYLAND_DISPLAY"))
    real = _real_x_display()

    if variant == 1:
        env["QT_QPA_PLATFORM"] = "xcb"
        env["GDK_BACKEND"] = "x11"
        if real:
            env["DISPLAY"] = real
        elif env.get("DISPLAY", "") in _BAD_DISPLAYS:
            env.pop("DISPLAY", None)
    elif variant == 2:
        env["QT_QPA_PLATFORM"] = "xcb"
        env.pop("GDK_BACKEND", None)
        env.pop("DISPLAY", None)
    else:
        disp = env.get("DISPLAY", "")
        if disp in _BAD_DISPLAYS:
            if real:
                env["DISPLAY"] = real
            else:
                env.pop("DISPLAY", None)
        if wayland and not real:
            env.pop("DISPLAY", None)

    # Moins de logs parasites côté VLC.
    env.setdefault("VLC_VERBOSE", "-1")
    return env


# ═══════════════════════════════════════════════════════════════════════════
# Lancement d'un lecteur — vérification audio réelle
# ═══════════════════════════════════════════════════════════════════════════

# Un lecteur qui s'arrête APRÈS ce délai a bel et bien démarré : c'est
# l'utilisateur qui a fermé la fenêtre (ou le média s'est terminé). Un média
# refusé, lui, fait sortir le lecteur en une fraction de seconde.
_USER_CLOSE_S = 2.5


class PlayerClosedByUser(Exception):
    """La fenêtre du lecteur a été fermée pendant la vérification audio.

    Sans ce signal, `_launch_with_fallback` prenait la fermeture pour un échec
    et relançait la variante suivante, puis le lecteur suivant : jusqu'à
    3 variantes × 4 lecteurs = 12 fenêtres qui repoussaient l'une après
    l'autre. « Je ferme, il rouvre. »
    """


def _launch_one(binary: str, target: str, is_url: bool,
                variant: int = 0) -> Tuple[bool, str]:
    """Lance UN lecteur et vérifie qu'il joue réellement.
    Retourne (succès, diagnostic).

    Lève `PlayerClosedByUser` si la fenêtre a été fermée après démarrage :
    il ne faut alors surtout pas réessayer.
    """
    spec = _PLAYERS.get(binary)
    if not spec:
        return False, f"lecteur inconnu ({binary})"
    argv = spec["url" if is_url else "file"]
    if not argv:
        return False, f"{spec['label']} ne gère pas ce type de source"

    env = _build_env(variant)
    try:
        proc = subprocess.Popen(
            list(argv) + [target],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
    except Exception as e:
        return False, f"échec du lancement de {binary} : {e}"

    # stderr en DEVNULL et non en PIPE : un tube que personne ne vide
    # bloque définitivement le lecteur dès qu'il a écrit ~64 Ko de logs,
    # et VLC est très bavard (avertissements TagLib, codecs…).

    if not kit.which("pactl"):
        # Pas de PipeWire/PulseAudio pour vérifier le son : on se rabat
        # sur « le process tient ».
        try:
            proc.wait(timeout=1.5)
        except subprocess.TimeoutExpired:
            return True, "lancé (vérification audio indisponible)"
        return False, (f"{binary} s'est arrêté immédiatement "
                       f"(code {proc.returncode})")

    # On n'annonce plus un succès simplement parce que le process a
    # survécu 0,8 s : on attend la preuve d'une lecture réelle, c'est-à-
    # dire un flux audio rattaché à ce process. C'est ce qui faisait dire
    # « lancé dans VLC » alors qu'aucun son ne sortait.
    started = time.monotonic()
    grace = started + 2.0
    audio_wait = 12.0 if is_url else 8.0
    deadline = started + audio_wait
    streams: List[Tuple[str, str]] = []

    while time.monotonic() < deadline:
        rc = proc.poll()
        if rc is not None:
            if rc == 0 and time.monotonic() < grace:
                return False, (f"{binary} a refusé le média "
                               f"(arrêt immédiat, code 0)")
            if time.monotonic() - started >= _USER_CLOSE_S:
                raise PlayerClosedByUser(
                    f"{_PLAYERS[binary]['label']} a été fermé pendant la lecture"
                )
            return False, f"{binary} s'est arrêté (code {rc})"
        streams = _sink_inputs_for_pid(proc.pid)
        if streams:
            break
        time.sleep(0.25)

    if streams:
        _ensure_audible(streams)
        return True, "lecture audio confirmée"

    if proc.poll() is None:
        # Processus vivant mais aucun flux audio vu dans le délai.
        # Les lecteurs MPRIS ont une seconde chance de preuve via
        # playerctl ; les autres (navigateurs…) sont laissés en vie.
        if spec.get("mpris") and kit.which("playerctl"):
            for _ in range(6):
                try:
                    r = kit.run(["playerctl", "list-players"],
                                       timeout=4)
                    if binary in (r.stdout or ""):
                        return True, "lecture confirmée via MPRIS"
                except Exception:
                    break
                time.sleep(0.5)
        if proc.poll() is None:
            return True, (f"{binary} est lancé mais le son n'a pas pu être "
                          f"confirmé — vérifie ta sortie audio")
    return False, f"{binary} tourne mais n'émet aucun son"


def _kill_tree(proc: "subprocess.Popen") -> None:
    try:
        os.killpg(proc.pid, 15)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _launch_with_fallback(target: str, is_url: bool,
                          preferred: Optional[str]) -> Tuple[bool, str]:
    """Essaie le lecteur demandé (ou VLC par défaut), puis retombe sur mpv
    et les autres lecteurs compatibles. Pour chaque lecteur, jusqu'à 3
    variantes d'environnement sont tentées (Wayland natif → XCB+DISPLAY
    réparé → XCB sans DISPLAY). C'est le cœur du correctif Hyprland."""
    players = available_players(for_url=is_url)
    if not players:
        return False, ("aucun lecteur compatible n'est installé "
                       "(installe VLC : sudo pacman -S vlc)")
    installed = [p["binary"] for p in players]

    order: List[str] = []
    if preferred and preferred in installed:
        order.append(preferred)
    elif preferred:
        return False, ("ce lecteur n'est pas installé ou incompatible. "
                       "Choisis parmi :\n" + _format_player_list(players))
    else:
        default = _default_player(is_url)
        if default:
            order.append(default)
    for b in _PREFERRED_ORDER + installed:
        if b not in order and b in installed:
            order.append(b)

    notes: List[str] = []
    for binary in order:
        for variant in range(3):
            try:
                ok, note = _launch_one(binary, target, is_url, variant)
            except PlayerClosedByUser as e:
                # Fermeture volontaire : réessayer serait exactement le bug
                # « je ferme, il rouvre ». On s'arrête net.
                return True, str(e)
            if ok:
                label = _PLAYERS[binary]["label"]
                if notes and binary != (preferred or _DEFAULT_PLAYER):
                    note = (f"{note} — repli sur {label} "
                            f"({' ; '.join(notes)})")
                return True, note
            notes.append(note)
            # Le process a été tué proprement en cas d'échec ;
            # on passe à la variante suivante.
    return False, "aucun lecteur n'a pu jouer le média — " + " ; ".join(notes)


# ═══════════════════════════════════════════════════════════════════════════
# Contrôle de lecture (playerctl / MPRIS)
# ═══════════════════════════════════════════════════════════════════════════

def _playerctl(*args: str) -> Tuple[bool, str]:
    if not kit.which("playerctl"):
        return False, ("playerctl n'est pas installé — installe-le : "
                       "sudo pacman -S playerctl")
    try:
        r = kit.run(["playerctl", *args], timeout=5)
    except Exception as e:
        return False, f"échec du contrôle de lecture : {e}"
    if r.returncode != 0:
        err = (r.stderr or "").strip()
        if "No players found" in err or "No such player" in err:
            return False, "aucune lecture en cours."
        return False, f"échec : {err or 'commande refusée'}"
    return True, (r.stdout or "").strip() or "Fait."


def _mpris_target() -> Optional[str]:
    """Lecteur MPRIS actuellement prioritaire (pour cibler les commandes)."""
    ok, out = _playerctl("list-players")
    if not ok or not out:
        return None
    first = out.splitlines()[0].strip().rstrip(",")
    return first or None


def _now_playing() -> str:
    ok, meta = _playerctl("metadata", "--format",
                          "{{artist}} — {{title}}")
    if not ok:
        return "Aucune lecture en cours."
    meta = re.sub(r"\s*—\s*$", "", meta.strip()).strip(" —")
    return meta or "Lecture en cours (titre inconnu)."


def _toggle_shuffle(state: Optional[bool] = None) -> str:
    target = _mpris_target()
    args = ["shuffle"]
    if state is not None:
        args = ["shuffle", "on" if state else "off"]
    if target:
        ok, out = _playerctl("--player", target, *args)
    else:
        ok, out = _playerctl(*args)
    if not ok:
        return f"Impossible de changer le mode aléatoire : {out}"
    return f"Mode aléatoire : {out or 'activé' if state else 'modifié'}"


def _seek(position: str) -> str:
    """position : '1:30', '+30', '-10', '90' (secondes)."""
    position = position.strip()
    sign = ""
    if position[:1] in "+-":
        sign, position = position[0], position[1:]
    parts = position.split(":")
    try:
        secs = sum(float(p) * (60 ** i)
                   for i, p in enumerate(reversed(parts)))
    except ValueError:
        return "Position invalide (exemples : '1:30', '+30', '-10')."
    arg = f"{sign}{int(secs)}"
    target = _mpris_target()
    if target:
        ok, out = _playerctl("--player", target, "position", arg)
    else:
        ok, out = _playerctl("position", arg)
    return out if ok else f"Impossible de se déplacer : {out}"


def _set_volume(value: str) -> str:
    """Règle exclusivement le volume de la musique en cours, jamais le sink global."""
    value = value.strip()
    try:
        requested = int(value)
    except ValueError:
        return "Volume musique invalide (exemples : '50', '+10', '-10')."

    relative = value[:1] in "+-"
    if _HAS_IPC_PLAYER:
        ipc = get_player()
        state = ipc.get_status()
        # Le lecteur interne et Spotify (MPRIS externe) ont leur propre gain.
        if state.get("state") != "stopped":
            current = int(state.get("volume") or 80)
            level = max(0, min(100, current + requested if relative else requested))
            if ipc.set_volume(level):
                action = (f"{'augmenté' if requested > 0 else 'baissé'} de {abs(requested)}%"
                          if relative else f"réglé à {level}%")
                return f"Volume de la musique {action}."

    # Un lecteur lancé hors d'ANO-GPT reste ciblable via MPRIS/playerctl.
    # Il n'y a volontairement aucun repli vers pactl/wpctl/amixer ici.
    target = _mpris_target()
    if target:
        ok, out = _playerctl("--player", target, "volume")
        try:
            current = round(float(out) * 100)
        except (TypeError, ValueError):
            current = 80
        level = max(0, min(100, current + requested if relative else requested))
        ok, out = _playerctl("--player", target, "volume", f"{level / 100:.2f}")
        if ok:
            action = (f"{'augmenté' if requested > 0 else 'baissé'} de {abs(requested)}%"
                      if relative else f"réglé à {level}%")
            return f"Volume de la musique {action}."
        return f"Impossible de régler le volume de {target} : {out}"

    return "Aucune musique en cours à régler; le volume système n'a pas été modifié."


# ═══════════════════════════════════════════════════════════════════════════
# Session memory helpers
# ═══════════════════════════════════════════════════════════════════════════

def _sm_get(sm, key, default=None):
    if sm is None:
        return default
    try:
        return sm.get(key, default)
    except Exception:
        return default


def _sm_set(sm, key, value):
    if sm is None:
        return
    try:
        sm[key] = value
    except Exception:
        try:
            sm.set(key, value)
        except Exception:
            pass


_AFFIRMATIVE = {
    "oui", "ouais", "yes", "yep", "yeah", "vas y", "vasy", "go", "ok",
    "okay", "d accord", "daccord", "confirme", "confirmer", "carrement",
    "exact", "affirmatif", "sure", "fais le", "fais-le", "lance",
    "lance le", "bien sur", "evidemment", "allez",
}


def _is_affirmative(text: str) -> bool:
    n = _normalize(text)
    if not n:
        return False
    return n in _AFFIRMATIVE or any(n.startswith(a) for a in _AFFIRMATIVE)


def _spotify_binary() -> str:
    return next((name for name in ("spotify", "spotify-launcher") if kit.which(name)), "")


def _hide_spotify_window() -> None:
    """Déplace Spotify hors de la scène, sans voler le focus utilisateur."""
    if _OS != "Linux" or not kit.which("hyprctl"):
        return
    # Spotify/Electron crée sa fenêtre après le processus principal. Attendre
    # brièvement évite de laisser un flash à l'écran sur Hyprland.
    for _ in range(18):
        time.sleep(0.2)
        try:
            clients = kit.hypr_clients()
        except Exception:
            continue
        moved = False
        for client in clients if isinstance(clients, list) else []:
            identity = " ".join(str(client.get(key) or "") for key in (
                "class", "initialClass", "title", "initialTitle",
            )).casefold()
            address = str(client.get("address") or "")
            if "spotify" not in identity or not address:
                continue
            try:
                kit.hypr("dispatch", "movetoworkspacesilent",
                         f"special:music,address:{address}", timeout=1.0)
                moved = True
            except Exception:
                pass
        if moved:
            return


def _resume_spotify_when_ready() -> None:
    """Reprend le morceau Spotify déjà chargé, sans bloquer le flux vocal."""
    if not kit.which("playerctl"):
        return
    for _ in range(30):
        time.sleep(0.2)
        try:
            state = kit.run(
                ["playerctl", "--player=spotify", "status"], timeout=0.7,
            ).stdout.strip().casefold()
            if state == "paused":
                kit.run(
                    ["playerctl", "--player=spotify", "play"], timeout=0.7,
                )
                return
            if state == "playing":
                return
        except Exception:
            pass


def _play_spotify(query: str, session_memory, player=None) -> str:
    """Ouvre une recherche Spotify et relie son MPRIS à la carte intégrée."""
    binary = _spotify_binary()
    if not binary:
        return "Spotify n'est pas disponible ici ; je peux chercher ce morceau sur YouTube."
    uri = f"spotify:search:{quote(query, safe='')}"
    try:
        # spotify-launcher reçoit l'URI en argument positionnel, contrairement
        # au binaire Spotify historique qui accepte parfois --uri=…
        if kit.spawn([binary, uri]) is None:
            raise OSError("lancement refusé")
    except OSError as exc:
        return f"Impossible de lancer Spotify : {exc}"
    threading.Thread(target=_hide_spotify_window, daemon=True,
                     name="hide-spotify-window").start()
    threading.Thread(target=_resume_spotify_when_ready, daemon=True,
                     name="resume-spotify-playback").start()
    card_ready = _HAS_IPC_PLAYER and bool(kit.which("playerctl"))
    if card_ready:
        get_player().watch_mpris_player("spotify")
    _sm_set(session_memory, "music_source", "spotify")
    if not card_ready:
        return (
            f"Spotify est lancé et recherche « {query} », mais la carte intégrée n'est pas encore disponible : "
            "playerctl manque sur le système. Installe-le avec `sudo pacman -S playerctl`, puis relance ANO-GPT."
        )
    return (
        f"J'ai ouvert la recherche Spotify pour « {query} » et repris le morceau déjà chargé s'il était en pause. "
        "Spotify ne permet pas de démarrer automatiquement son premier résultat sans autorisation API ; dès qu'un morceau est lancé, "
        "sa pochette et ses contrôles apparaissent dans la carte musique ANO-GPT."
    )


def _fmt_playback(title: str, origin: str, note: str) -> str:
    base = f"« {title} » ({origin}) — lecture lancée."
    return f"{base} {note}" if note else base


# ═══════════════════════════════════════════════════════════════════════════
# Lancement effectif d'un résultat (local ou flux déjà résolu)
# ═══════════════════════════════════════════════════════════════════════════

def _play_result(target: str, is_url: bool, title: str, origin: str,
                 chosen: str, session_memory, player=None,
                 item: dict | None = None, playlist: list[dict] | None = None,
                 thumbnail: str = "") -> str:
    """Choisit le lecteur MPV Headless (IPC) sans fenêtre par défaut, ou repli fenêtré si demandé."""
    local_path = Path(target) if not is_url else None
    if (not chosen and local_path is not None and
            local_path.suffix.lower() in VIDEO_EXT and
            player is not None and hasattr(player, "play_video")):
        from core.local_video import prepare_local_video, prepare_local_videos

        video = prepare_local_video(item or {"path": target, "title": title})
        if video is None:
            return f"Impossible de préparer la vidéo locale « {title} »."
        videos = prepare_local_videos(playlist or [video]) if playlist else [video]
        player.play_video(video, videos or [video])
        _sm_set(session_memory, _LOCAL_VIDEO_CURRENT_KEY, video)
        return f"« {title} » — lecture dans le lecteur vidéo intégré."
    if _HAS_IPC_PLAYER and not chosen:
        ipc = get_player()
        if thumbnail:
            try:
                ipc.play(target, title=title, artist=origin, thumbnail=thumbnail)
            except TypeError:
                ipc.play(target, title=title, artist=origin)
        else:
            ipc.play(target, title=title, artist=origin)
        return f"« {title} » ({origin}) — lecture lancée en arrière-plan sans fenêtre."

    chosen_bin = _resolve_player(chosen, is_url) if chosen else None
    if chosen and not chosen_bin:
        return ("Ce lecteur n'est pas installé ou incompatible. "
                "Choisis parmi :\n"
                + _format_player_list(available_players(for_url=is_url)))

    ok, note = _launch_with_fallback(target, is_url, chosen_bin)
    if ok:
        return _fmt_playback(title, origin, note)
    return f"Impossible de lancer « {title} » : {note}"


def _play_youtube_best(query: str, chosen: str, session_memory,
                       player=None, media_kind: str = "audio") -> str:
    """Recherche YouTube, résout le flux et lance dans le lecteur approprié (audio HUD ou vidéo intégrée)."""
    try:
        yt_hits = search_youtube(query, limit=6)
    except YoutubeUnavailable as e:
        return f"Recherche YouTube impossible : {e}"
    if not yt_hits:
        return f"Rien trouvé sur YouTube pour « {query} »."

    best = yt_hits[0]
    title = best.get("title") or query
    uploader = best.get("uploader") or best.get("channel") or "YouTube"
    thumb = best.get("thumbnail_url") or (f"https://i.ytimg.com/vi/{best.get('id', '')}/hqdefault.jpg" if best.get("id") else "")

    if media_kind == "video" and player is not None and hasattr(player, "play_video"):
        video_item = {
            "id": best.get("id") or best["url"].split("v=")[-1],
            "title": title,
            "url": best["url"],
            "channel": uploader,
            "duration": best.get("duration", ""),
            "views": best.get("views", 0),
            "thumbnail_url": thumb,
        }
        playlist_items = [
            {
                "id": hit.get("id") or hit["url"].split("v=")[-1],
                "title": hit.get("title", ""),
                "url": hit.get("url", ""),
                "channel": hit.get("uploader") or hit.get("channel") or "",
                "duration": hit.get("duration", ""),
                "views": hit.get("views", 0),
                "thumbnail_url": hit.get("thumbnail_url") or (f"https://i.ytimg.com/vi/{hit.get('id', '')}/hqdefault.jpg" if hit.get("id") else ""),
            }
            for hit in yt_hits
        ]
        player.play_video(video_item, playlist_items)
        if session_memory is not None:
            session_memory["youtube_current"] = video_item
            session_memory["youtube_results"] = playlist_items
        return f"« {title} » — lecture de la vidéo dans le lecteur vidéo intégré."

    stream = resolve_stream_url(best["url"])
    target = stream or best["url"]
    if stream:
        return _play_result(stream, True, title, uploader,
                            chosen, session_memory, player=player, thumbnail=thumb)

    if _HAS_IPC_PLAYER and not chosen:
        ipc = get_player()
        ipc.play(target, title=title, artist=uploader, thumbnail=thumb)
        return f"« {title} » ({uploader}) — lecture lancée en arrière-plan sans fenêtre."

    # Repli lecteur vidéo intégré si disponible
    if player is not None and hasattr(player, "play_video"):
        video_item = {"id": best.get("id") or best["url"].split("v=")[-1], "title": title, "url": best["url"], "channel": uploader}
        player.play_video(video_item, [video_item])
        return f"« {title} » — lecture dans le lecteur vidéo intégré."

    # Résolution du flux direct impossible → repli navigateur
    browser_players = [p for p in available_players(for_url=True)
                       if p["binary"] == "google-chrome-stable"]
    chosen_bin = _resolve_player(chosen, True) if chosen else None
    binary = None
    if chosen_bin == "google-chrome-stable":
        binary = chosen_bin
    elif browser_players:
        binary = browser_players[0]["binary"]
    if binary:
        try:
            ok, note = _launch_one(binary, best["url"], True, variant=0)
        except PlayerClosedByUser:
            return (f"J'ai ouvert « {best['title']} » dans le navigateur, "
                    f"tu l'as refermé.")
        if ok:
            return (f"Le flux direct n'était pas exploitable, j'ai ouvert "
                    f"« {best['title']} » dans {_PLAYERS[binary]['label']} "
                    f"à la place.")
    return (f"J'ai trouvé « {best['title']} » sur YouTube mais je n'ai pas "
            f"pu le lancer.")


_AUTO_RAP_RE = re.compile(
    r"\b(hasard|al[eé]atoire|random|au pif|un son|une musique)\b", re.IGNORECASE
)
_NON_TRACK_RE = re.compile(
    r"\b(playlist|mix|compilation|best of|full album|album complet|interview|podcast|live set|1 heure|hour)\b",
    re.IGNORECASE,
)


def _duration_seconds(value: str) -> int:
    try:
        minutes, seconds = str(value or "").split(":", 1)
        return int(minutes) * 60 + int(seconds)
    except (TypeError, ValueError):
        return 0


def _best_track(hits: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Écarte playlists, podcasts et vidéos interminables pour un vrai son."""
    candidates = [
        hit for hit in hits
        if not _NON_TRACK_RE.search(str(hit.get("title") or ""))
        and 90 <= _duration_seconds(hit.get("duration", "")) <= 480
    ]
    return candidates[0] if candidates else None


def _play_auto_rap(chosen: str, session_memory, player=None) -> str:
    """Choisit et joue directement un morceau rap, sans carte de sélection."""
    for query in (
        "rap guinéen hit officiel",
        "rap français hit officiel",
        "american rap hit official audio",
    ):
        try:
            track = _best_track(search_youtube(query, limit=12))
        except YoutubeUnavailable:
            continue
        if not track:
            continue
        title = track.get("title") or "morceau rap"
        artist = track.get("uploader") or track.get("channel") or "YouTube"
        thumbnail = track.get("thumbnail_url") or ""
        stream = resolve_stream_url(track["url"])
        return _play_result(
            stream or track["url"], True, title, artist, chosen, session_memory,
            player=player, thumbnail=thumbnail,
        )
    return "Je n'ai pas réussi à trouver un morceau rap jouable tout de suite."


def _youtube_in_integrated_video(query: str, session_memory, player, *, play: bool) -> str:
    """Unifie toute lecture YouTube dans la carte vidéo native.

    Les anciens chemins de ``music_control`` résolvaient un flux audio et
    lançaient parfois un lecteur externe. Une musique YouTube est une vidéo
    YouTube : elle doit garder ses résultats, sa sélection et sa lecture dans
    le même composant ANO-GPT.
    """
    from actions.youtube_video import youtube_video

    return youtube_video(
        {"action": "play" if play else "search", "query": query, "limit": 8},
        player=player,
        session_memory=session_memory,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Point d'entrée outil
# ═══════════════════════════════════════════════════════════════════════════

@kit.action("music_control")
def music_control(parameters: dict = None, response=None, player=None,
                  session_memory=None) -> str:
    """
    Recherche et joue un morceau ou une vidéo, en local puis automatiquement sur YouTube.

    action : 'play' (défaut) | 'search' | 'select' | 'pause' | 'resume' | 'play_pause' | 'next'
             | 'previous' | 'stop' | 'now_playing' | 'list_players'
             | 'shuffle' | 'seek' | 'volume'
    query  : ce que l'utilisateur veut écouter/regarder
    player : lecteur choisi (« vlc », « mpv »…) — sinon VLC par défaut,
             avec repli automatique sur mpv puis les autres lecteurs.
    source : 'auto' (défaut) | 'local' | 'spotify' | 'youtube'
    confirm: True quand l'utilisateur vient de confirmer la recherche
             YouTube proposée au tour précédent (répond « oui » à
             « je cherche sur YouTube ? »)
    value  : position pour 'seek' (« 1:30 », « +30 ») ou volume (« 50 »,
             « +10 ») ; 'on'/'off' pour forcer le mode aléatoire.
    """
    p = parameters or {}
    action = str(p.get("action", "play") or "play").strip().lower()
    query = str(p.get("query", "") or "").strip()
    chosen = str(p.get("player", "") or "").strip()
    from core.browser_policy import BROWSER_ALIASES
    if chosen.lower() in BROWSER_ALIASES:
        chosen = "google-chrome-stable"
    source = str(p.get("source", "auto") or "auto").strip().lower()
    if source not in {"auto", "local", "spotify", "youtube"}:
        source = "auto"
    value = str(p.get("value", "") or "").strip()
    selection = p.get("index", p.get("result", value))
    confirm = bool(p.get("confirm", False)) or _is_affirmative(query)
    media_kind = str(p.get("kind", p.get("media_kind", "audio")) or "audio").strip().lower()
    if media_kind not in {"audio", "video", "all"}:
        media_kind = "audio"

    if player:
        try:
            player.write_log(f"[music] {action} {query!r}")
        except Exception:
            pass

    native_video = _sm_get(session_memory, _LOCAL_VIDEO_CURRENT_KEY)
    if native_video and player is not None and hasattr(player, "control_video"):
        native_actions = {
            "pause": "pause", "resume": "resume", "unpause": "resume",
            "play_pause": "toggle", "next": "next", "previous": "previous",
            "stop": "stop", "volume": "volume", "seek": "seek",
        }
        if action in native_actions:
            control_value: Any = value or query or None
            if action == "volume":
                try:
                    control_value = int(str(control_value).lstrip("+"))
                except (TypeError, ValueError):
                    control_value = 80
            elif action == "seek":
                raw = str(control_value or "0")
                sign = -1 if raw.startswith("-") else 1
                raw = raw.lstrip("+-")
                try:
                    parts = [float(part) for part in raw.split(":")]
                    control_value = sign * sum(
                        part * (60 ** index) for index, part in enumerate(reversed(parts))
                    )
                except ValueError:
                    control_value = 0
            player.control_video(native_actions[action], control_value)
            if action == "stop":
                _sm_set(session_memory, _LOCAL_VIDEO_CURRENT_KEY, None)
            return {
                "pause": "Vidéo locale mise en pause.",
                "resume": "Lecture de la vidéo locale reprise.",
                "unpause": "Lecture de la vidéo locale reprise.",
                "play_pause": "Lecture de la vidéo locale basculée.",
                "next": "Vidéo locale suivante.",
                "previous": "Vidéo locale précédente.",
                "stop": "Vidéo locale arrêtée. La sélection reste affichée.",
            }.get(action, "Commande envoyée au lecteur vidéo local.")
        if action == "now_playing" and hasattr(player, "video_status"):
            return player.video_status()

    # ── Contrôles de lecture ─────────────────────────────────────────────
    if _HAS_IPC_PLAYER:
        ipc = get_player()
        if action == "pause":
            ipc.pause()
            return "Lecture en pause."
        if action in ("resume", "play_pause", "unpause"):
            ipc.toggle_pause()
            return "Lecture basculée."
        if action == "next":
            ipc.next()
            return "Morceau suivant."
        if action == "previous":
            ipc.prev()
            return "Morceau précédent."
        if action == "stop":
            ipc.stop()
            return "Lecture arrêtée."
        if action == "now_playing":
            st = ipc.get_status()
            if st.get("state") != "stopped":
                return f"En cours : {st.get('title')} par {st.get('artist')}"

    if action in ("pause",):
        ok, msg = _playerctl("pause")
        return "Lecture en pause." if ok else msg
    if action in ("resume", "play_pause", "unpause"):
        ok, msg = _playerctl("play")
        return "Lecture reprise." if ok else msg
    if action == "next":
        ok, msg = _playerctl("next")
        return "Morceau suivant." if ok else msg
    if action == "previous":
        ok, msg = _playerctl("previous")
        return "Morceau précédent." if ok else msg
    if action == "stop":
        ok, msg = _playerctl("stop")
        return "Lecture arrêtée." if ok else msg
    if action == "now_playing":
        return _now_playing()
    if action == "shuffle":
        st = None if not value else value.lower() in ("on", "true", "1")
        return _toggle_shuffle(st)
    if action == "seek":
        return _seek(value or query)
    if action == "volume":
        return _set_volume(value or query)
    if action == "list_players":
        players = available_players()
        if not players:
            return "Aucun lecteur multimédia installé."
        return "Lecteurs disponibles :\n" + _format_player_list(players)

    # ── Sélection d'un résultat local précédemment proposé ───────────────
    remembered_local = _sm_get(session_memory, _LOCAL_RESULTS_KEY) or []
    if action == "select" or (
        action == "play" and remembered_local
        and ((not query and selection not in (None, "")) or query.isdigit())
    ):
        selected = _resolve_local_result(remembered_local, selection or query)
        if not selected:
            return "Ce résultat local n'existe pas. Relance une recherche ou choisis un numéro affiché."
        base_args = (
            selected["path"], False, selected["title"],
            selected.get("artist") or "ta bibliothèque locale", chosen, session_memory,
        )
        if Path(selected["path"]).suffix.lower() in VIDEO_EXT:
            return _play_result(
                *base_args, player=player, item=selected, playlist=remembered_local,
            )
        _sm_set(session_memory, _LOCAL_RESULTS_KEY, None)
        return _play_result(*base_args)

    # ── Recherche seule : locale par défaut, YouTube seulement si explicite ─
    if action == "search":
        if not query:
            return "Quel morceau veux-tu rechercher dans ta bibliothèque ?"
        if source == "youtube":
            return _youtube_in_integrated_video(query, session_memory, player, play=False)
        local_results = _search_local_for_kind(query, media_kind)
        if media_kind == "video" and local_results:
            from core.local_video import prepare_local_videos
            local_results = prepare_local_videos(local_results)
        _sm_set(session_memory, _LOCAL_RESULTS_KEY, local_results)
        if not local_results:
            return f"Aucun média local trouvé pour « {query} »."
        if media_kind == "video" and player is not None and hasattr(player, "show_video_results"):
            player.show_video_results(query, local_results)
        return _format_local_results(query, local_results)

    # ── Confirmation d'une recherche YouTube laissée en attente ──────────
    pending_source = _sm_get(session_memory, _PENDING_SOURCE_KEY)
    if pending_source:
        requested = _normalize(query)
        if source == "auto":
            if "spotify" in requested:
                source = "spotify"
            elif "youtube" in requested:
                source = "youtube"
        if source in {"spotify", "youtube"}:
            _sm_set(session_memory, _PENDING_SOURCE_KEY, None)
            query = pending_source["query"]
        elif query:
            _sm_set(session_memory, _PENDING_SOURCE_KEY, None)
    pending_yt = _sm_get(session_memory, _PENDING_YT_KEY)
    if pending_yt and (confirm or (query and _is_affirmative(query))):
        _sm_set(session_memory, _PENDING_YT_KEY, None)
        return _play_youtube_best(pending_yt.get("query", ""),
                                  chosen, session_memory, player=player, media_kind=media_kind)
    if pending_yt and query and not _is_affirmative(query):
        # L'utilisateur a reformulé une nouvelle recherche : on abandonne
        # l'attente et on repart sur sa nouvelle requête.
        _sm_set(session_memory, _PENDING_YT_KEY, None)

    if not query:
        return "Quel morceau ou quelle vidéo veux-tu que je lance ?"

    if source == "spotify":
        return _play_spotify(query, session_memory, player=player)

    # « Mets une musique au hasard » doit produire un morceau, pas huit clips
    # ou une vidéo de 24 minutes à choisir. Priorité Guinée → France → US.
    if media_kind == "audio" and source != "local" and _AUTO_RAP_RE.search(query):
        return _play_auto_rap(chosen, session_memory, player=player)

    # ── Recherche explicite YouTube (l'utilisateur l'a demandé) ──────────
    if source == "youtube":
        return _youtube_in_integrated_video(query, session_memory, player, play=True)

    # ── Détection automatique d'intention vidéo ──────────────────────────
    if media_kind == "audio" and re.search(r"\b(vid[ée]o|clip|film|tuto|regarder?|voir)\b", query, re.IGNORECASE):
        media_kind = "video"

    # L'utilisateur choisit explicitement son catalogue avant toute recherche
    # distante. Les fichiers locaux restent, eux, joués directement.
    local_hits = _search_local_for_kind(query, media_kind)
    if media_kind == "video" and local_hits:
        from core.local_video import prepare_local_videos
        local_hits = prepare_local_videos(local_hits)
    if local_hits:
        _sm_set(session_memory, _LOCAL_RESULTS_KEY, local_hits)
        if not _is_confident_local_match(local_hits):
            if source == "local":
                if media_kind == "video" and player is not None and hasattr(player, "show_video_results"):
                    player.show_video_results(query, local_hits[:12])
                noun = "médias" if media_kind == "video" else "morceaux"
                return (
                    f"J'ai trouvé plusieurs {noun} proches et je préfère éviter de lancer le mauvais.\n"
                    + _format_local_results(query, local_hits[:5])
                )
            if float(local_hits[0].get("score", 0)) < 0.50:
                local_hits = None

        if local_hits:
            best = local_hits[0]
            base_args = (
                best["path"], False, best["title"],
                best.get("artist") or "ta bibliothèque locale", chosen, session_memory,
            )
            if Path(best["path"]).suffix.lower() in VIDEO_EXT:
                return _play_result(
                    *base_args, player=player, item=best, playlist=local_hits,
                )
            _sm_set(session_memory, _LOCAL_RESULTS_KEY, None)
            return _play_result(*base_args)

    _sm_set(session_memory, _PENDING_SOURCE_KEY, {"query": query})
    return (
        f"Je n'ai pas « {query} » dans ta bibliothèque locale. Tu le veux via "
        "Spotify ou YouTube ?"
    )



# ═══════════════════════════════════════════════════════════════════════════
# Auto-test rapide : python music.py "nom du morceau"
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) or "freeze corleone"
    print(music_control({"query": q}, session_memory={}))
