#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
music_recognition.py — « Tu connais cette musique ? »

Reconnaissance du morceau qui joue, dans l'ordre de fiabilité :

1. **Lecteur MPRIS** (Spotify, mpv, navigateur…) : si un lecteur diffuse un
   titre, ses métadonnées sont exactes et instantanées — pas besoin d'écouter.
2. **Empreinte acoustique** : capture de la sortie audio du système (monitor
   PipeWire, ce que les enceintes jouent, sans le bruit de la pièce) ; si elle
   est silencieuse, capture du micro (musique dans la pièce, radio, télé).
   L'empreinte est comparée à la base Shazam (`shazamio`, ou le CLI `songrec`).
   Deux fenêtres successives si la première ne donne rien : Shazam identifie
   mieux sur un refrain que sur une intro.
3. **Estimation Gemini** (dernier recours, annoncée comme telle) : le modèle
   écoute l'extrait et propose titre/artiste avec un niveau de confiance.

Le résultat est mémorisé (`last_identified_track`) pour enchaîner « lance-la
sur Spotify », « sur YouTube », « ajoute-la à ma bibliothèque ».
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import struct
import tempfile
import time
import wave
from pathlib import Path
from typing import Any, Callable, Optional

from core import action_kit as kit

CARD_TYPE = "music"
CAPTURE_S = 9.0                 # fenêtre d'écoute : Shazam est fiable dès 6-8 s
SECOND_WINDOW_S = 12.0          # seconde tentative plus longue, sur le refrain
SILENCE_DBFS = -50.0            # en dessous, la sortie système est muette
HISTORY_KEY = "music_recognition_history"
LAST_KEY = "last_identified_track"
_HISTORY_FILE = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "anogpt" / "music_recognition.jsonl"


# ════════════════════════════════════════════════════════════════════════════
# 1. Lecteurs MPRIS
# ════════════════════════════════════════════════════════════════════════════

def _playing_from_mpris() -> Optional[dict]:
    """Métadonnées du lecteur en cours de lecture, si un lecteur MPRIS joue."""
    if not kit.which("playerctl"):
        return None
    r = kit.run(["playerctl", "-a", "metadata", "--format",
                 "{{playerName}}\t{{status}}\t{{artist}}\t{{title}}\t{{album}}\t{{mpris:artUrl}}\t{{xesam:url}}"],
                timeout=2.0)
    if not r.ok:
        return None
    for line in r.lines():
        parts = (line.split("\t") + [""] * 7)[:7]
        player, status, artist, title, album, art, url = (p.strip() for p in parts)
        if status.lower() != "playing" or not title:
            continue
        # Un navigateur en lecture d'une page sans média renvoie un titre de
        # page : on exige au moins un artiste ou une URL média.
        if not artist and not url:
            continue
        return {
            "title": title, "artist": artist or "artiste inconnu", "album": album,
            "cover_url": art, "source": f"lecteur {player}", "confidence": "exacte",
            "method": "mpris", "player": player, "media_url": url,
        }
    return None


# ════════════════════════════════════════════════════════════════════════════
# 2. Capture audio
# ════════════════════════════════════════════════════════════════════════════

def _default_monitor() -> Optional[str]:
    r = kit.run(["pactl", "get-default-sink"], timeout=2.0)
    sink = r.text.strip() if r.ok else ""
    return f"{sink}.monitor" if sink else None


def _default_source() -> Optional[str]:
    try:
        from core import audio_router
        name = audio_router.get_manual_override()
        if name:
            return name
    except Exception:
        pass
    r = kit.run(["pactl", "get-default-source"], timeout=2.0)
    return r.text.strip() if r.ok and r.text.strip() else None


def _capture(device: str, seconds: float, out: Path) -> bool:
    """Enregistre `seconds` de `device` (PulseAudio/PipeWire) en WAV mono 44,1 kHz."""
    if not kit.which("ffmpeg"):
        return False
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "pulse", "-i", device, "-t", f"{seconds:.1f}",
           "-ac", "1", "-ar", "44100", "-sample_fmt", "s16", str(out)]
    r = kit.run(cmd, timeout=seconds + 6.0)
    return r.ok and out.exists() and out.stat().st_size > 44


def _level_dbfs(path: Path) -> float:
    """Niveau RMS du WAV en dBFS (−inf si vide) — sans numpy, 10 s suffisent."""
    try:
        with wave.open(str(path), "rb") as w:
            n = w.getnframes()
            if n == 0:
                return -math.inf
            raw = w.readframes(min(n, 44100 * 15))
    except (wave.Error, OSError):
        return -math.inf
    count = len(raw) // 2
    if count == 0:
        return -math.inf
    samples = struct.unpack(f"<{count}h", raw[: count * 2])
    rms = math.sqrt(sum(s * s for s in samples) / count)
    return 20 * math.log10(rms / 32768.0) if rms > 0 else -math.inf


def capture_playing_audio(seconds: float, progress: Optional[Callable[[str], None]] = None) -> tuple[Optional[Path], str]:
    """Sortie système d'abord, micro ensuite. Retourne (fichier, origine)."""
    tmp = Path(tempfile.mkdtemp(prefix="anogpt-shazam-"))
    monitor = _default_monitor()
    if monitor:
        if progress:
            progress("j'écoute ce que jouent les enceintes")
        out = tmp / "system.wav"
        if _capture(monitor, seconds, out) and _level_dbfs(out) > SILENCE_DBFS:
            return out, "sortie système"
    source = _default_source()
    if source:
        if progress:
            progress("rien sur les enceintes, j'écoute avec le micro")
        out = tmp / "mic.wav"
        if _capture(source, seconds, out) and _level_dbfs(out) > SILENCE_DBFS - 10:
            return out, "micro"
    return None, ""


# ════════════════════════════════════════════════════════════════════════════
# 3. Moteurs d'identification
# ════════════════════════════════════════════════════════════════════════════

def _shazamio_available() -> bool:
    try:
        import shazamio  # noqa: F401
        return True
    except Exception:
        return False


def _parse_shazam(data: dict, origin: str) -> Optional[dict]:
    track = (data or {}).get("track") or {}
    if not track.get("title"):
        return None
    meta = {}
    for section in track.get("sections") or []:
        for item in section.get("metadata") or []:
            if item.get("title") and item.get("text"):
                meta[str(item["title"]).lower()] = str(item["text"])
    spotify_uri = ""
    apple_url = ""
    for provider in (track.get("hub") or {}).get("providers") or []:
        for action in provider.get("actions") or []:
            uri = str(action.get("uri") or "")
            if provider.get("type") == "SPOTIFY" and uri.startswith("spotify:"):
                spotify_uri = uri
    for option in (track.get("hub") or {}).get("options") or []:
        for action in option.get("actions") or []:
            uri = str(action.get("uri") or "")
            if "music.apple.com" in uri:
                apple_url = uri
    genre = ((track.get("genres") or {}).get("primary")) or ""
    return {
        "title": track.get("title", ""),
        "artist": track.get("subtitle", ""),
        "album": meta.get("album", ""),
        "year": meta.get("released", ""),
        "label": meta.get("label", ""),
        "genre": genre,
        "cover_url": ((track.get("images") or {}).get("coverarthq")
                      or (track.get("images") or {}).get("coverart") or ""),
        "shazam_url": track.get("url", ""),
        "spotify_uri": spotify_uri,
        "apple_url": apple_url,
        "isrc": track.get("isrc", ""),
        "source": f"Shazam ({origin})",
        "confidence": "haute",
        "method": "shazam",
    }


def _identify_shazamio(path: Path) -> Optional[dict]:
    from shazamio import Shazam

    async def _go() -> dict:
        shazam = Shazam()
        recognize = getattr(shazam, "recognize", None) or shazam.recognize_song
        return await recognize(str(path))

    return _parse_shazam(asyncio.run(_go()), "empreinte")


def _identify_songrec(path: Path) -> Optional[dict]:
    """SongRec (Rust) : même base Shazam, binaire système `songrec`."""
    if not kit.which("songrec"):
        return None
    r = kit.run(["songrec", "audio-file-to-recognized-song", str(path)], timeout=40.0)
    if not r.ok:
        return None
    try:
        return _parse_shazam(json.loads(r.text), "empreinte")
    except (json.JSONDecodeError, TypeError):
        return None


def _identify_gemini(path: Path) -> Optional[dict]:
    """Dernier recours : le modèle écoute l'extrait. Annoncé comme estimation."""
    try:
        from actions.tiktok_coach import _gemini, _generate_json, TEXT_MODELS
    except Exception:
        return None
    try:
        _client, gtypes = _gemini()
        audio = gtypes.Part.from_bytes(data=path.read_bytes(), mime_type="audio/wav")
    except Exception:
        return None
    prompt = (
        "Écoute cet extrait. S'il s'agit d'une chanson connue, donne son titre et son artiste. "
        "Si tu n'es pas sûr, dis-le. Réponds en JSON strict : "
        '{"known": true|false, "title": "", "artist": "", "album": "", "year": "", '
        '"confidence": "haute|moyenne|faible", "genre": "", "why": "indices entendus (paroles, voix, riff)"}'
    )
    try:
        data = _generate_json([audio, prompt], TEXT_MODELS)
    except Exception:
        return None
    if not data.get("known") or not data.get("title"):
        return None
    return {
        "title": data.get("title", ""), "artist": data.get("artist", ""),
        "album": data.get("album", ""), "year": data.get("year", ""), "genre": data.get("genre", ""),
        "cover_url": "", "source": "estimation Gemini", "confidence": str(data.get("confidence") or "faible"),
        "method": "gemini", "why": data.get("why", ""),
    }


def fingerprint_engines() -> list[str]:
    engines = []
    if _shazamio_available():
        engines.append("shazamio")
    if kit.which("songrec"):
        engines.append("songrec")
    return engines


def identify_file(path: Path) -> Optional[dict]:
    """Empreinte d'abord (Shazam via shazamio puis songrec), Gemini en dernier."""
    for engine in (_identify_shazamio if _shazamio_available() else None, _identify_songrec):
        if engine is None:
            continue
        try:
            found = engine(path)
        except Exception as exc:
            print(f"[Musique] moteur {engine.__name__} en échec : {exc}")
            found = None
        if found:
            return found
    return _identify_gemini(path)


# ════════════════════════════════════════════════════════════════════════════
# 4. Orchestration
# ════════════════════════════════════════════════════════════════════════════

def identify_now_playing(progress: Optional[Callable[[str], None]] = None,
                         allow_mpris: bool = True) -> dict:
    """Retourne toujours un dict : {"found": bool, "track": {...} | None, "reason": str}."""
    if allow_mpris:
        found = _playing_from_mpris()
        if found:
            return {"found": True, "track": found, "reason": ""}
    if not kit.which("ffmpeg"):
        return {"found": False, "track": None,
                "reason": "ffmpeg manque pour capturer le son (sudo pacman -S ffmpeg)."}
    engines = fingerprint_engines()
    # Laisser la voix d'ANO (« J'écoute… ») finir de sortir des enceintes :
    # sinon c'est elle que l'on enregistre sur le monitor.
    time.sleep(0.8)
    path, origin = capture_playing_audio(CAPTURE_S, progress)
    if path is None:
        return {"found": False, "track": None,
                "reason": "Je n'entends aucune musique : ni sur les enceintes, ni au micro."}
    if progress:
        progress("je compare l'empreinte")
    track = identify_file(path)
    if track is None and engines:
        # Deuxième fenêtre plus longue : l'intro trompe, le refrain signe.
        if progress:
            progress("pas encore sûr, j'écoute un peu plus")
        path2, origin2 = capture_playing_audio(SECOND_WINDOW_S, None)
        if path2 is not None:
            track = identify_file(path2)
            origin = origin2 or origin
    if track is None:
        why = ("Empreinte non reconnue" if engines else
               "aucun moteur d'empreinte installé (pip install shazamio, ou sudo pacman -S songrec)")
        return {"found": False, "track": None,
                "reason": f"{why} — extrait capté via {origin}."}
    if "source" in track and origin and "empreinte" in track["source"]:
        track["source"] = f"Shazam, {origin}"
    return {"found": True, "track": track, "reason": ""}


def _remember(session_memory, track: dict) -> None:
    try:
        if session_memory is not None:
            session_memory[LAST_KEY] = dict(track)
            hist = list(session_memory.get(HISTORY_KEY) or [])
            hist.append({"at": time.time(), **{k: track.get(k, "") for k in ("title", "artist", "album")}})
            session_memory[HISTORY_KEY] = hist[-50:]
        _HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _HISTORY_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M"),
                                **{k: track.get(k, "") for k in ("title", "artist", "album", "year", "method")}},
                               ensure_ascii=False) + "\n")
    except Exception:
        pass


def _last(session_memory) -> Optional[dict]:
    try:
        if session_memory is not None and session_memory.get(LAST_KEY):
            return dict(session_memory[LAST_KEY])
        if _HISTORY_FILE.exists():
            lines = _HISTORY_FILE.read_text(encoding="utf-8").strip().splitlines()
            if lines:
                return json.loads(lines[-1])
    except Exception:
        pass
    return None


def _card_body(track: dict) -> str:
    lines = [f"## {track.get('title', '')}", f"**{track.get('artist', '')}**"]
    details = [x for x in (track.get("album"), track.get("year"), track.get("genre")) if x]
    if details:
        lines.append(" · ".join(str(d) for d in details))
    if track.get("label"):
        lines.append(f"Label : {track['label']}")
    lines.append(f"_Identifiée via {track.get('source', '')} — confiance {track.get('confidence', '')}_")
    if track.get("why"):
        lines.append(f"_Indices : {track['why']}_")
    links = []
    if track.get("shazam_url"):
        links.append(f"[Shazam]({track['shazam_url']})")
    if track.get("apple_url"):
        links.append(f"[Apple Music]({track['apple_url']})")
    if links:
        lines.append(" · ".join(links))
    return "\n".join(lines)


def _spoken(track: dict) -> str:
    title, artist = track.get("title", ""), track.get("artist", "")
    head = f"C'est « {title} » de {artist}"
    if track.get("album") and track.get("year"):
        head += f", album « {track['album']} », {track['year']}"
    elif track.get("year"):
        head += f", sortie en {track['year']}"
    head += "."
    if track.get("method") == "gemini":
        head = f"Je pense que c'est « {title} » de {artist} — c'est une estimation, confiance {track.get('confidence', 'faible')}."
    elif track.get("method") == "mpris":
        head += f" C'est ce que joue {track.get('player', 'ton lecteur')} en ce moment."
    return head + " Tu veux que je la lance sur Spotify, ou sur YouTube ?"


def _show(player: Any, track: dict) -> None:
    try:
        show = getattr(player, "show_card", None)
        if callable(show):
            actions = []
            on_text = getattr(player, "on_text_command", None)
            q = f"{track.get('artist', '')} {track.get('title', '')}".strip()
            if callable(on_text):
                actions = [
                    {"label": "Spotify", "primary": True,
                     "callback": lambda q=q: on_text(f"lance « {q} » sur Spotify")},
                    {"label": "YouTube", "callback": lambda q=q: on_text(f"lance « {q} » sur YouTube")},
                ]
            show(CARD_TYPE, "Musique reconnue", _card_body(track), actions)
    except Exception:
        pass
    cover = track.get("cover_url")
    if cover:
        try:
            resp = kit.http().get(cover, timeout=(3, 8))
            if resp.ok and resp.content and len(resp.content) < 4 * 1024 * 1024:
                gallery = getattr(player, "show_image_gallery", None)
                if callable(gallery):
                    gallery(f"{track.get('title', '')} — {track.get('artist', '')}",
                            [{"bytes": resp.content, "title": track.get("title", ""),
                              "source": track.get("artist", "")}])
        except Exception:
            pass


def _play(track: dict, target: str, session_memory, player) -> str:
    from actions import music as music_action
    query = f"{track.get('artist', '')} {track.get('title', '')}".strip()
    target = (target or "spotify").lower()
    if target == "spotify":
        uri = track.get("spotify_uri")
        if uri and music_action._spotify_binary():
            # URI directe : Spotify ouvre le morceau lui-même, pas une recherche.
            if kit.spawn([music_action._spotify_binary(), uri]) is not None:
                try:
                    if music_action._HAS_IPC_PLAYER and kit.which("playerctl"):
                        music_action.get_player().watch_mpris_player("spotify")
                except Exception:
                    pass
                return f"Je lance « {track.get('title', '')} » de {track.get('artist', '')} sur Spotify."
        return music_action._play_spotify(query, session_memory, player)
    if target == "youtube":
        return music_action.music_control({"action": "play", "query": query, "source": "youtube"},
                                          player=player, session_memory=session_memory) or "Lecture YouTube lancée."
    return music_action.music_control({"action": "play", "query": query},
                                      player=player, session_memory=session_memory) or "Lecture lancée."


# ════════════════════════════════════════════════════════════════════════════
# 5. Outil
# ════════════════════════════════════════════════════════════════════════════

def music_recognition(parameters: dict | None = None, player: Any = None,
                      session_memory: Any = None, speak: Callable[[str], None] | None = None, **_kw) -> str:
    p = parameters or {}
    action = str(p.get("action") or "identify").strip().lower()
    if action in {"play", "lance", "jouer", "play_last"}:
        track = _last(session_memory)
        if not track:
            return "Je n'ai encore reconnu aucune musique : dis-moi « tu connais cette musique ? » pendant qu'elle joue."
        return _play(track, str(p.get("target") or "spotify"), session_memory, player)
    if action in {"history", "historique", "last", "dernière", "derniere"}:
        track = _last(session_memory)
        if not track:
            return "Aucune musique reconnue pour l'instant."
        hist = list((session_memory or {}).get(HISTORY_KEY) or []) if session_memory is not None else []
        recent = ", ".join(f"« {h.get('title')} » de {h.get('artist')}" for h in hist[-5:][::-1]) or \
                 f"« {track.get('title')} » de {track.get('artist')}"
        return f"Dernières musiques reconnues : {recent}."
    # identify
    def _progress(msg: str) -> None:
        try:
            log = getattr(player, "write_log", None)
            if callable(log):
                log(f"SYS : reconnaissance musicale — {msg}.")
        except Exception:
            pass
    result = identify_now_playing(_progress, allow_mpris=not bool(p.get("listen_only")))
    if not result["found"]:
        return f"Je n'ai pas reconnu la musique. {result['reason']}"
    track = result["track"]
    _remember(session_memory, track)
    _show(player, track)
    return _spoken(track)
