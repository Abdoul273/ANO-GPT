#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Télécharge le meilleur morceau YouTube dans ~/Musique.

« télécharge Nightcall » ne doit pas ramener un cover, un live, un mix d'une
heure ou un short. La recherche YouTube est classée (titre, chaîne Topic/VEVO,
durée de chanson, vues), puis yt-dlp extrait l'audio en meilleure qualité
(m4a, repli opus) avec métadonnées — même logique que YT-NEXUS, bornée à
l'audio et au dossier Musique.
"""

from __future__ import annotations

import math
import os
import re
import select
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

from core.media_search import intelligent_score, readable_media_name

from core import action_kit as kit


_YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "music.youtube.com"}
_YT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_QUERY_STOP = {
    "telecharge", "telecharger", "download", "dl", "mp3", "m4a", "flac",
    "musique", "morceau", "chanson", "son", "track", "audio", "youtube",
    "le", "la", "les", "de", "du", "des", "un", "une", "mon", "ma", "mes",
    "moi", "me", "s", "stp", "svp", "please", "the", "a", "of",
}
_JUNK_RE = re.compile(
    r"\b(?:live|cover|covers|karaoke|instrumental|nightcore|slowed|reverb|"
    r"sped\s*up|speed\s*up|8[\s-]?d|mashups?|compilation|megamix|"
    r"(?:1|one|10|ten)\s*(?:hour|heure)s?|\bhours?\b|\bheures?\b|"
    r"reaction|react|paroles?|lyrics?|lyric\s*video|visuali[sz]er|"
    r"piano|guitar|drum|tutorial|lesson|how\s*to|shorts?|#shorts)\b",
    re.IGNORECASE,
)
_OFFICIAL_RE = re.compile(
    r"\b(?:official(?:\s+(?:audio|video|music(?:\s+video)?))?|"
    r"audio\s+officiel|clip\s+officiel|vevo)\b",
    re.IGNORECASE,
)
_PROGRESS_PCT = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)%")
_PROGRESS_SPEED = re.compile(r"at\s+(\S+/s)")
_PROGRESS_SIZE = re.compile(r"of\s+~?(\S+)")
_PROGRESS_ETA = re.compile(r"ETA\s+(\S+)")
_DEST_RE = re.compile(r"Destination:\s+(.+)$")
_MIN_ACCEPT_TITLE = 0.42
_MIN_ACCEPT_SCORE = 48.0
_ALREADY_HAVE = 0.90

_ACTIVE: dict[str, dict[str, Any]] = {}
_ACTIVE_LOCK = threading.Lock()


def music_dir() -> Path:
    """Dossier Musique XDG, avec repli francophone puis anglophone."""
    from actions.music import MUSIC_DIRS

    for path in MUSIC_DIRS:
        try:
            if path.is_dir():
                return path
        except OSError:
            continue
    target = Path.home() / "Musique"
    target.mkdir(parents=True, exist_ok=True)
    return target


def clean_query(raw: str) -> str:
    """Retire les mots d'ordre vocaux, garde le titre entendu."""
    text = str(raw or "").strip()
    if not text:
        return ""
    if is_youtube_url(text):
        return text
    folded = (
        text.replace("é", "e").replace("è", "e").replace("ê", "e")
        .replace("à", "a").replace("ù", "u")
    )
    tokens = re.findall(r"[A-Za-z0-9éèêëàâäùûüôöçÉÈÊÀÂÙÛÔÇ+'._-]+", folded)
    kept = [tok for tok in tokens if tok.casefold() not in _QUERY_STOP]
    return " ".join(kept).strip() or text.strip()


def is_youtube_url(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if _YT_ID_RE.fullmatch(text):
        return True
    parsed = urlparse(text if "://" in text else f"https://{text}")
    host = (parsed.hostname or "").lower()
    if host in _YT_HOSTS:
        return True
    return "youtube.com/" in text or "youtu.be/" in text


def youtube_url(value: str) -> str:
    text = str(value or "").strip()
    if _YT_ID_RE.fullmatch(text):
        return f"https://www.youtube.com/watch?v={text}"
    if not text:
        return ""
    parsed = urlparse(text if "://" in text else f"https://{text}")
    host = (parsed.hostname or "").lower()
    if host == "youtu.be":
        vid = parsed.path.strip("/").split("/")[0]
        return f"https://www.youtube.com/watch?v={vid}" if vid else text
    if host in _YT_HOSTS:
        vid = parse_qs(parsed.query).get("v", [""])[0]
        if vid:
            return f"https://www.youtube.com/watch?v={vid}"
        if "/shorts/" in parsed.path:
            vid = parsed.path.split("/shorts/")[-1].split("/")[0]
            if vid:
                return f"https://www.youtube.com/watch?v={vid}"
    return text


def parse_duration(value: Any) -> int:
    """Accepte 215, '3:35' ou '1:02:10' → secondes."""
    if value is None or value == "":
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    parts = text.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return 0
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    return nums[0] if nums else 0


def requested_modes(query: str) -> set[str]:
    """Tokens de la requête qui autorisent un « défaut » (cover, live…)."""
    found: set[str] = set()
    for match in _JUNK_RE.finditer(query or ""):
        found.add(re.sub(r"\s+", " ", match.group(0).casefold()))
    return found


def score_track(query: str, item: dict[str, Any], allowed_junk: set[str] | None = None) -> float:
    """Score unique : similarité du titre + officialité + durée de chanson.

    Un cover, un live ou un mix d'une heure sont fortement pénalisés, sauf si
    la requête les demande. Les chaînes Topic / VEVO et l'audio officiel
    l'emportent sur un titre approximatif à fort trafic.
    """
    allowed_junk = allowed_junk if allowed_junk is not None else requested_modes(query)
    title = str(item.get("title") or "")
    channel = str(item.get("uploader") or item.get("channel") or "")
    duration = parse_duration(item.get("duration") or item.get("duration_s"))
    try:
        views = int(item.get("views") or item.get("view_count") or 0)
    except (TypeError, ValueError):
        views = 0

    title_score = intelligent_score(query, title)
    channel_score = intelligent_score(query, channel)
    score = title_score * 100.0 + channel_score * 18.0

    if _OFFICIAL_RE.search(title):
        score += 26.0
    if channel.endswith(" - Topic") or channel.endswith(" - Sujet"):
        score += 22.0
    if "VEVO" in channel.upper():
        score += 16.0

    if 90 <= duration <= 420:
        score += 18.0
    elif 60 <= duration <= 540:
        score += 8.0
    elif duration and duration < 45:
        score -= 28.0
    elif duration and duration > 900:
        score -= 38.0

    if views > 0:
        score += min(14.0, math.log10(views + 1) * 2.0)

    junk_hits = {
        re.sub(r"\s+", " ", match.group(0).casefold())
        for match in _JUNK_RE.finditer(title)
    }
    extra_junk = junk_hits - allowed_junk
    wanted_junk = junk_hits & allowed_junk
    if extra_junk and not wanted_junk:
        score -= 42.0
    elif extra_junk:
        score -= 8.0
    if wanted_junk:
        score += 36.0
    elif allowed_junk:
        if _OFFICIAL_RE.search(title):
            score -= 30.0
        if channel.endswith(" - Topic") or channel.endswith(" - Sujet") or "VEVO" in channel.upper():
            score -= 16.0

    item["match_score"] = round(score, 2)
    item["title_score"] = round(title_score, 3)
    return score


def pick_best_track(query: str, candidates: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Choisit le morceau le plus proche, ou rien si tout est hors-sujet."""
    if not candidates:
        return None
    allowed = requested_modes(query)
    ranked = sorted(
        candidates,
        key=lambda item: score_track(query, item, allowed),
        reverse=True,
    )
    best = ranked[0]
    title_score = float(best.get("title_score") or 0.0)
    total = float(best.get("match_score") or 0.0)
    if title_score < _MIN_ACCEPT_TITLE and total < _MIN_ACCEPT_SCORE:
        return None
    return best


def file_stem_for(track: dict[str, Any]) -> str:
    """Nom de fichier lisible, sans le bruit YouTube (Official Audio, 320k…).

    Seules les chaînes Topic / VEVO collent l'artiste devant le titre. Un
    pseudo YouTube (« Quentin-le_BG ») ne doit pas se retrouver dans le nom.
    """
    title = readable_media_name(str(track.get("title") or "morceau"))
    channel = str(track.get("uploader") or track.get("channel") or "")
    official_channel = bool(
        re.search(r"\s+-\s+(?:Topic|Sujet)\s*$", channel, re.I)
        or "VEVO" in channel.upper()
    )
    artist = re.sub(r"\s+-\s+(?:Topic|Sujet|VEVO)\s*$", "", channel, flags=re.I).strip()
    artist = re.sub(r"VEVO$", "", artist, flags=re.I).strip()
    if official_channel and artist and artist.casefold() not in title.casefold():
        name = f"{artist} - {title}"
    else:
        name = title
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    name = name.replace("%", "_")[:160]
    return name or "morceau"


def phrase_for(kind: str, title: str) -> str:
    """Phrase parlable selon le mode de ton actif."""
    from core.personality_modes import PersonalityMode, active_mode

    try:
        mode = active_mode()
    except Exception:
        mode = PersonalityMode.NORMAL
    name = (title or "le morceau").strip() or "le morceau"
    bank = {
        PersonalityMode.ASTRO: {
            "start": (
                f"C'est parti, je choppe {name}. Patienter, je te préviens "
                "dès que c'est dans Musique."
            ),
            "done": f"C'est plié. {name} est dans Musique, vas-y déguste.",
            "error": f"Putain, {name} a foiré. On réessaie si tu veux.",
            "cancelled": "OK, j'annule. T'as raison, on laisse tomber.",
            "exists": f"Tu l'as déjà, champion. {name} est dans Musique.",
            "busy": (
                f"C'est encore en cours, {name}. Patienter, je te préviens "
                "dès que c'est plié."
            ),
        },
        PersonalityMode.MAJEUR: {
            "start": (
                f"Téléchargement de {name} lancé, Monsieur. Un instant, "
                "je vous préviens dès que le fichier est dans Musique."
            ),
            "done": f"{name} est dans Musique, Monsieur.",
            "error": f"Le téléchargement de {name} a échoué, Monsieur.",
            "cancelled": "Téléchargement annulé, Monsieur.",
            "exists": f"{name} est déjà dans Musique, Monsieur.",
            "busy": (
                f"Le transfert de {name} est toujours en cours, Monsieur. "
                "Je vous préviens dès qu'il est prêt."
            ),
        },
        PersonalityMode.COQUIN: {
            "start": (
                f"Je récupère {name}. Patiente un instant, je te le dis "
                "dès que c'est dans Musique."
            ),
            "done": f"{name} est dans Musique.",
            "error": f"Le téléchargement de {name} a calé.",
            "cancelled": "D'accord, j'arrête.",
            "exists": f"Tu as déjà {name} dans Musique.",
            "busy": (
                f"{name} est encore en train d'arriver. Patiente, "
                "je te préviens."
            ),
        },
    }.get(mode, {
        "start": (
            f"Je lance le téléchargement de {name}. Patientez, je vous "
            "préviens dès que c'est dans Musique."
        ),
        "done": f"{name} est dans Musique.",
        "error": f"Le téléchargement de {name} a échoué.",
        "cancelled": "Téléchargement annulé.",
        "exists": f"Vous avez déjà {name} dans Musique.",
        "busy": (
            f"Le téléchargement de {name} est toujours en cours. Patientez, "
            "je vous préviens dès que c'est prêt."
        ),
    })
    return bank.get(kind, "")


def wait_active(timeout: float = 30.0) -> None:
    """Bloque jusqu'à la fin des téléchargements en cours (tests)."""
    deadline = time.monotonic() + max(0.05, float(timeout))
    while True:
        with _ACTIVE_LOCK:
            threads = [
                rec.get("thread") for rec in _ACTIVE.values()
                if rec.get("thread") is not None
            ]
        if not threads:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        threads[0].join(remaining)


def _announce(speak: Any, text: str) -> None:
    if not callable(speak) or not text:
        return
    try:
        speak(
            "[TÉLÉCHARGEMENT] Dis exactement ceci, sans rien ajouter et sans "
            f"appeler le moindre outil : {text}"
        )
    except Exception:
        pass


def _matching_active(query: str) -> Optional[dict[str, Any]]:
    needle = clean_query(query)
    with _ACTIVE_LOCK:
        recs = [dict(rec) for rec in _ACTIVE.values()]
    if not recs:
        return None
    if not needle:
        return recs[0]
    for rec in recs:
        hay = " ".join(
            str(rec.get(key) or "") for key in ("query", "title")
        )
        if intelligent_score(needle, hay) >= 0.5:
            return rec
    return None


def parse_progress_line(line: str) -> dict[str, Any]:
    """Extrait pourcentage, vitesse, taille et ETA d'une ligne yt-dlp."""
    text = (line or "").strip()
    out: dict[str, Any] = {}
    if not text:
        return out
    pct = _PROGRESS_PCT.search(text)
    if pct:
        out["percent"] = min(100.0, max(0.0, float(pct.group(1))))
        out["status"] = "downloading"
    speed = _PROGRESS_SPEED.search(text)
    if speed:
        out["speed"] = speed.group(1)
    size = _PROGRESS_SIZE.search(text)
    if size:
        out["size"] = size.group(1)
    eta = _PROGRESS_ETA.search(text)
    if eta and eta.group(1) not in {"NA", "Unknown"}:
        out["eta"] = eta.group(1)
    dest = _DEST_RE.search(text)
    if dest:
        out["path"] = dest.group(1).strip()
    lower = text.lower()
    if "[extractaudio]" in lower or "extracting audio" in lower or "[merger]" in lower:
        out["status"] = "converting"
        out.setdefault("percent", 97.0)
    if "deleting original file" in lower:
        out["status"] = "converting"
        out.setdefault("percent", 99.0)
    return out


def build_ydl_cmd(
    url: str,
    dest_dir: Path,
    stem: str,
    *,
    has_ffmpeg: bool | None = None,
) -> list[str]:
    """Commande yt-dlp audio : bestaudio → m4a qualité 0, métadonnées, turbo mesuré."""
    if has_ffmpeg is None:
        has_ffmpeg = bool(shutil.which("ffmpeg"))
    dest_dir.mkdir(parents=True, exist_ok=True)
    template = str(dest_dir / f"{stem}.%(ext)s")
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--newline",
        "--progress",
        "--retries", "8",
        "--fragment-retries", "8",
        "--retry-sleep", "http:exp=2:30",
        "--socket-timeout", "15",
        "--concurrent-fragments", "4",
        "--http-chunk-size", "2M",
        "--no-overwrites",
        "--trim-filenames", "180",
        "-o", template,
        "--add-metadata",
    ]
    if has_ffmpeg:
        cmd += [
            "-f", "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio/best",
            "-x", "--audio-format", "m4a", "--audio-quality", "0",
            "--embed-metadata",
            "--embed-thumbnail",
            "--convert-thumbnails", "jpg",
        ]
    else:
        cmd += ["-f", "bestaudio[ext=m4a]/bestaudio/best"]
    cmd.append(url)
    return cmd


def cancel_download(download_id: str) -> bool:
    with _ACTIVE_LOCK:
        rec = _ACTIVE.get(download_id)
    if not rec:
        return False
    rec["cancel"].set()
    proc = rec.get("proc")
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
        except OSError:
            pass
    return True


def _register(download_id: str) -> dict[str, Any]:
    rec = {"cancel": threading.Event(), "proc": None, "thread": None, "query": "", "title": ""}
    with _ACTIVE_LOCK:
        _ACTIVE[download_id] = rec
    return rec


def _unregister(download_id: str) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE.pop(download_id, None)


def _public_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if not str(key).startswith("_")}


def _notify(player: Any, payload: dict[str, Any]) -> None:
    if player is None:
        return
    visible = _public_payload(payload)
    fn = getattr(player, "show_music_download", None)
    if callable(fn):
        try:
            fn(visible)
            return
        except Exception:
            pass
    show = getattr(player, "show_card", None)
    update = getattr(player, "update_card", None)
    title = "Téléchargement"
    body = _markdown_fallback(visible)
    try:
        if callable(update):
            try:
                if update("download", title, body):
                    return
            except Exception:
                pass
        if callable(show):
            show("download", title, body)
    except Exception:
        pass


def _markdown_fallback(payload: dict[str, Any]) -> str:
    name = payload.get("title") or "Morceau"
    artist = payload.get("artist") or ""
    pct = int(float(payload.get("percent") or 0))
    filled = max(0, min(20, round(pct / 5)))
    bar = "█" * filled + "░" * (20 - filled)
    status = {
        "searching": "Recherche de la version officielle…",
        "downloading": "Téléchargement…",
        "converting": "Conversion audio…",
        "done": "Terminé",
        "error": "Échec",
        "cancelled": "Annulé",
        "exists": "Déjà dans Musique",
    }.get(str(payload.get("status") or ""), "")
    speed = payload.get("speed") or ""
    eta = payload.get("eta") or ""
    lines = [f"**{name}**"]
    if artist:
        lines.append(artist)
    lines.append(f"`{bar}` **{pct}%**")
    extra = " · ".join(part for part in (status, speed, f"ETA {eta}" if eta else "") if part)
    if extra:
        lines.append(extra)
    path = payload.get("path")
    if path:
        lines.append(f"`{path}`")
    return "\n".join(lines)


def _push(player: Any, state: dict[str, Any], **fields: Any) -> dict[str, Any]:
    state.update(fields)
    _notify(player, state)
    return state


def _already_have(query: str) -> Optional[dict[str, Any]]:
    try:
        from actions.music import search_local
        hits = search_local(query, limit=3, min_score=_ALREADY_HAVE)
    except Exception:
        return None
    return hits[0] if hits else None


def _search(query: str) -> list[dict[str, Any]]:
    from actions.music import YoutubeUnavailable, search_youtube

    try:
        results = search_youtube(query, limit=8)
    except YoutubeUnavailable:
        raise
    if not results:
        return []
    best = pick_best_track(query, results)
    if best and float(best.get("title_score") or 0) >= 0.55:
        return results
    boosted = f"{query} official audio"
    try:
        extra = search_youtube(boosted, limit=5)
    except YoutubeUnavailable:
        extra = []
    merged: dict[str, dict[str, Any]] = {}
    for item in list(results) + list(extra):
        key = str(item.get("id") or item.get("url") or item.get("title"))
        if key and key not in merged:
            merged[key] = item
    return list(merged.values())


def _run_yt_dlp(
    cmd: list[str],
    rec: dict[str, Any],
    on_line: Callable[[str], None],
    timeout: int = 600,
) -> tuple[int, str]:
    env = os.environ.copy()
    env.pop("http_proxy", None)
    env.pop("https_proxy", None)
    env.pop("HTTP_PROXY", None)
    env.pop("HTTPS_PROXY", None)
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
    except FileNotFoundError:
        return 127, "yt-dlp n'est pas installé (sudo pacman -S yt-dlp)."
    rec["proc"] = proc
    output: list[str] = []
    deadline = time.monotonic() + timeout
    assert proc.stdout is not None
    while True:
        if rec["cancel"].is_set():
            try:
                proc.terminate()
            except OSError:
                pass
            try:
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except OSError:
                    pass
            return 130, "annulé"
        if time.monotonic() > deadline:
            try:
                proc.kill()
            except OSError:
                pass
            return 124, "le téléchargement a expiré."
        ready, _, _ = select.select([proc.stdout], [], [], 0.2)
        if ready:
            line = proc.stdout.readline()
            if line:
                output.append(line.rstrip())
                on_line(line)
                continue
        if proc.poll() is not None:
            leftover = proc.stdout.read() or ""
            for extra in leftover.splitlines():
                output.append(extra.rstrip())
                on_line(extra)
            break
    code = proc.wait()
    return code, "\n".join(output[-12:])


def _maybe_retry_with_chrome_cookies(
    cmd: list[str],
    rec: dict[str, Any],
    on_line: Callable[[str], None],
    log: str,
) -> tuple[int, str]:
    blob = log.lower()
    if "sign in" not in blob and "bot" not in blob and "confirm you’re not" not in blob:
        if "confirm you're not" not in blob and "http error 403" not in blob:
            return 1, log
    if "--cookies-from-browser" in cmd:
        return 1, log
    retry = list(cmd)
    retry[1:1] = ["--cookies-from-browser", "chrome"]
    return _run_yt_dlp(retry, rec, on_line)


def _final_path(declared: str, dest_dir: Path, stem: str) -> Optional[Path]:
    candidates: list[Path] = []
    if declared:
        p = Path(declared)
        if p.is_file():
            return p
        candidates.append(p)
    try:
        for path in dest_dir.iterdir():
            if not path.is_file():
                continue
            if path.stem.startswith(stem) and path.suffix.lower() in {
                ".m4a", ".mp3", ".opus", ".ogg", ".webm", ".aac", ".flac", ".wav",
            }:
                candidates.append(path)
    except OSError:
        pass
    existing = [p for p in candidates if p.is_file()]
    if not existing:
        return None
    existing.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return existing[0]


def _download_job(
    rec: dict[str, Any],
    state: dict[str, Any],
    url: str,
    dest: Path,
    stem: str,
    player: Any,
    speak: Any,
) -> None:
    """yt-dlp en fond : la conversation n'attend pas la fin du fichier."""
    last_emit = 0.0
    declared_path = ""
    display = str(state.get("title") or stem)

    def on_line(line: str) -> None:
        nonlocal last_emit, declared_path
        parsed = parse_progress_line(line)
        if not parsed:
            return
        if parsed.get("path"):
            declared_path = str(parsed["path"])
        now = time.monotonic()
        pct = float(parsed.get("percent") or state.get("percent") or 0)
        status = str(parsed.get("status") or state.get("status") or "downloading")
        significant = (
            status != state.get("status")
            or abs(pct - float(state.get("percent") or 0)) >= 1.5
            or now - last_emit >= 0.18
        )
        if not significant:
            state.update({k: v for k, v in parsed.items() if v not in ("", None)})
            return
        last_emit = now
        _push(player, state, **parsed)

    try:
        cmd = build_ydl_cmd(url, dest, stem)
        code, log = _run_yt_dlp(cmd, rec, on_line)
        if code not in (0, 130) and log:
            code, log = _maybe_retry_with_chrome_cookies(cmd, rec, on_line, log)

        if rec["cancel"].is_set() or code == 130:
            _push(player, state, status="cancelled")
            _announce(speak, phrase_for("cancelled", display))
            return
        if code != 0:
            detail = (log or "erreur yt-dlp").strip().splitlines()
            hint = detail[-1][:180] if detail else "échec yt-dlp"
            _push(player, state, status="error", message=hint)
            _announce(speak, phrase_for("error", display))
            return

        path = _final_path(declared_path, dest, stem)
        _push(
            player, state,
            status="done",
            percent=100.0,
            path=str(path) if path else "",
            speed="",
            eta="",
        )
        _announce(speak, phrase_for("done", display))
    except Exception as exc:
        _push(player, state, status="error", message=str(exc)[:180])
        _announce(speak, phrase_for("error", display))
    finally:
        _unregister(str(state.get("id") or ""))


def _active_snapshot() -> list[dict[str, Any]]:
    with _ACTIVE_LOCK:
        return [{"id": key, **{k: v for k, v in rec.items() if k in ("query", "title", "state")}}
                for key, rec in _ACTIVE.items()]


def _cancel_by_voice(query: str) -> str:
    recs = _active_snapshot()
    if not recs:
        return "Aucun téléchargement en cours."
    target = _matching_active(query) if query else None
    if query and target is None and len(recs) > 1:
        return "Je ne vois pas ce téléchargement ; en cours : " + ", ".join(
            str(r.get("title") or r.get("query")) for r in recs)
    ids = ([key for key, rec in _ACTIVE.items() if rec is target or rec.get("title") == target.get("title")]
           if target else [r["id"] for r in recs])
    cancelled = [rid for rid in ids if cancel_download(rid)]
    if not cancelled:
        return "Aucun téléchargement à annuler."
    return f"Téléchargement annulé ({len(cancelled)})." if len(cancelled) > 1 else \
        f"Téléchargement de « {(target or recs[0]).get('title') or query} » annulé."


def _status_by_voice() -> str:
    recs = _active_snapshot()
    if not recs:
        return "Aucun téléchargement en cours."
    lines = []
    for rec in recs:
        state = rec.get("state") or {}
        pct = float(state.get("percent") or 0)
        extra = " ".join(x for x in (state.get("speed"), f"reste {state['eta']}" if state.get("eta") else "") if x)
        lines.append(f"- {rec.get('title') or rec.get('query')} : {pct:.0f} %" + (f" ({extra})" if extra else ""))
    return "Téléchargements en cours :\n" + "\n".join(lines)


def _recent_downloads(limit: int = 8) -> str:
    dest = music_dir()
    files = []
    for path in dest.glob("*"):
        if path.suffix.lower() in {".m4a", ".mp3", ".opus", ".webm", ".flac", ".ogg"}:
            try:
                files.append((path.stat().st_mtime, path))
            except OSError:
                continue
    if not files:
        return f"Aucun morceau dans {dest}."
    files.sort(reverse=True)
    return f"Derniers morceaux dans {dest.name} :\n" + "\n".join(
        f"- {readable_media_name(p.stem)}" for _, p in files[:limit])


@kit.action("download_music")
def download_music(parameters: dict | None = None, player=None, speak=None, **_kwargs) -> str:
    """Cherche le bon morceau, lance yt-dlp en fond, rend tout de suite la phrase d'attente."""
    params = parameters or {}
    action = str(params.get("action") or "download").strip().casefold()
    raw = str(params.get("query") or params.get("url") or params.get("title") or "").strip()
    if action in {"cancel", "stop", "annule", "annuler"}:
        return _cancel_by_voice(raw)
    if action in {"status", "progress", "etat", "état"}:
        return _status_by_voice()
    if action in {"list", "recent", "derniers"}:
        return _recent_downloads()
    if not raw:
        return "Dis-moi le titre du morceau à télécharger."
    if not shutil.which("yt-dlp"):
        return "yt-dlp n'est pas installé. Installe-le avec sudo pacman -S yt-dlp."

    query = clean_query(raw)
    busy = _matching_active(query)
    if busy:
        return phrase_for("busy", str(busy.get("title") or query or raw))

    download_id = f"dl-{uuid.uuid4().hex[:8]}"
    dest = music_dir()
    rec = _register(download_id)
    rec["query"] = query
    rec["title"] = query or raw
    state: dict[str, Any] = {
        "id": download_id,
        "status": "searching",
        "title": query or raw,
        "artist": "",
        "channel": "",
        "percent": 0.0,
        "speed": "",
        "eta": "",
        "size": "",
        "path": "",
        "destination": str(dest),
    }
    rec["state"] = state  # lisible par « où en est le téléchargement ? »
    _push(player, state)

    try:
        if rec["cancel"].is_set():
            _push(player, state, status="cancelled")
            _unregister(download_id)
            return phrase_for("cancelled", query or raw)

        if is_youtube_url(raw) or is_youtube_url(query):
            url = youtube_url(query if is_youtube_url(query) else raw)
            track = {
                "title": query or raw,
                "url": url,
                "uploader": "",
                "duration": "",
            }
        else:
            existing = _already_have(query)
            if existing:
                path = existing.get("path") or ""
                title = existing.get("title") or query
                artist = existing.get("artist") or ""
                _push(
                    player, state,
                    status="exists",
                    title=title,
                    artist=artist,
                    percent=100.0,
                    path=path,
                )
                _unregister(download_id)
                return phrase_for("exists", title)

            from actions.music import YoutubeUnavailable

            try:
                candidates = _search(query)
            except YoutubeUnavailable as exc:
                _push(player, state, status="error", message=str(exc))
                _unregister(download_id)
                return f"Je n'ai pas pu chercher sur YouTube : {exc}"

            track = pick_best_track(query, candidates)
            if track is None:
                _push(player, state, status="error", message="aucun match fiable")
                _unregister(download_id)
                return (
                    f"Je n'ai pas trouvé de version fiable de « {query} » "
                    "sur YouTube. Donne-moi le titre exact, ou l'artiste."
                )
            url = str(track.get("url") or "")
            if not url and track.get("id"):
                url = f"https://www.youtube.com/watch?v={track['id']}"

        stem = file_stem_for(track)
        artist = re.sub(
            r"\s+-\s+(?:Topic|Sujet|VEVO)\s*$",
            "",
            str(track.get("uploader") or track.get("channel") or ""),
            flags=re.I,
        ).strip()
        display = readable_media_name(str(track.get("title") or stem))
        rec["title"] = display
        _push(
            player, state,
            status="downloading",
            title=display,
            artist=artist,
            channel=str(track.get("uploader") or ""),
            percent=1.0,
        )

        worker = threading.Thread(
            target=_download_job,
            args=(rec, state, url, dest, stem, player, speak),
            name=f"yt-dl-{download_id}",
            daemon=True,
        )
        rec["thread"] = worker
        worker.start()
        return phrase_for("start", display)
    except Exception:
        _unregister(download_id)
        raise
