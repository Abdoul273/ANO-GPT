"""
youtube_video.py — YouTube intelligent ultra‑réaliste
Parsing local avancé, actions play / summarize / info / trending, fallback IA.
Utilise le scraping direct, yt-dlp en secours, les transcripts, et Gemini pour les résumés.
"""
import json
import re
import sys
import shutil
import platform
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime
from urllib.parse import quote_plus
from typing import Optional, Dict, Any

from core.youtube_service import (
    YouTubeUnavailable,
    resolve_result,
    search_youtube as search_youtube_structured,
)
import importlib.util

from actions.media_control import media_control

from core import action_kit as kit
from core.live_model_policy import BALANCED_MODEL, FAST_MODEL

_NUMPY = importlib.util.find_spec("numpy") is not None
_REQUESTS_OK = importlib.util.find_spec("requests") is not None

try:
    from youtube_transcript_api import YouTubeTranscriptApi
    _TRANSCRIPT_OK = True
except ImportError:
    _TRANSCRIPT_OK = False

# ── Configuration ────────────────────────────────────────────────────────────
def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

BASE_DIR        = _get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
_YT_VIDEO_FILTER = "EgIQAQ%3D%3D"   # exclut les Shorts de la recherche
_YT_RESULTS_KEY = "youtube_results"
_YT_CURRENT_KEY = "youtube_current"

_CONTROL_ACTIONS = {
    "pause": "youtube_pause",
    "resume": "youtube_play",
    "playback": "youtube_play",
    "toggle": "youtube_toggle",
    "mute": "youtube_mute",
    "volume": "youtube_volume",
    "seek": "youtube_seek",
    "forward": "youtube_seek",
    "back": "youtube_seek",
    "restart": "youtube_restart",
    "fullscreen": "youtube_fullscreen",
    "subtitles": "youtube_subtitles",
    "captions": "youtube_subtitles",
    "theater": "youtube_theater",
    "miniplayer": "youtube_miniplayer",
    "next": "youtube_next",
    "previous": "youtube_previous",
    "speed": "youtube_speed",
    "stop": "youtube_pause",
    "close": "youtube_pause",
}


def _youtube_search_url(query: str) -> str:
    return (
        "https://www.youtube.com/results"
        f"?search_query={quote_plus(query)}&sp={_YT_VIDEO_FILTER}"
    )

def _get_api_key() -> str:
    try:
        with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("gemini_api_key", "")
    except Exception:
        return ""

def _is_windows(): return platform.system() == "Windows"
def _is_mac():     return platform.system() == "Darwin"
def _is_linux():   return platform.system() == "Linux"

def _open_in_chrome(url: str) -> bool:
    from core.browser_policy import open_chrome
    return open_chrome(url)


def _open_url(url: str, prefer_browser: str = "chrome") -> bool:
    """Toutes les ouvertures web passent exclusivement par Chrome."""
    return _open_in_chrome(url)


def _play_video(url: str) -> bool:
    """Lit une vidéo YouTube dans un vrai lecteur (mpv/vlc), sinon navigateur."""
    # mpv/vlc savent lire YouTube directement (via yt-dlp pour mpv)
    for player_bin in ("mpv", "vlc"):
        if shutil.which(player_bin):
            try:
                kit.spawn([player_bin, url])
                return True
            except Exception:
                continue
    # Repli : navigateur
    return _open_url(url)


def _format_search_results(results: list[dict], query: str, player=None) -> str:
    lines = [f"Résultats YouTube pour « {query} » :"]
    for index, item in enumerate(results, 1):
        channel = f" — {item['channel']}" if item.get("channel") else ""
        duration = f" [{item['duration']}]" if item.get("duration") else ""
        views = f" • {item['views']:,} vues" if item.get("views") else ""
        lines.append(f"{index}. {item['title']}{channel}{duration}{views}")
    lines.append("Dites « lis la 2 », « ouvre la première » ou précisez un titre.")
    return "\n".join(lines)


def _attach_thumbnails(results: list[dict]) -> list[dict]:
    """Télécharge en parallèle de petites vignettes sûres pour l'interface Qt."""
    if not _REQUESTS_OK or not results:
        return results

    def fetch(index: int, item: dict) -> tuple[int, bytes]:
        video_id = str(item.get("id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{6,16}", video_id):
            return index, b""
        url = str(item.get("thumbnail_url") or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg")
        try:
            response = kit.http().get(url, headers=HEADERS, timeout=(3, 6))
            payload = response.content if response.ok else b""
            content_type = response.headers.get("content-type", "").lower()
            if not content_type.startswith("image/") or len(payload) > 2_500_000:
                payload = b""
            return index, payload
        except Exception:
            return index, b""

    with ThreadPoolExecutor(max_workers=min(6, len(results))) as pool:
        futures = [pool.submit(fetch, index, item) for index, item in enumerate(results)]
        for future in as_completed(futures):
            index, payload = future.result()
            if payload:
                results[index]["thumbnail_bytes"] = payload
    return results


def _search_and_remember(query: str, limit: int, session_memory, player=None) -> tuple[list[dict], str]:
    results = [r.to_dict() for r in search_youtube_structured(query, limit=limit)]
    if player is not None and hasattr(player, "show_video_results"):
        _attach_thumbnails(results)
    if session_memory is not None:
        session_memory[_YT_RESULTS_KEY] = results
        session_memory["youtube_last_query"] = query
    if results and player is not None and hasattr(player, "show_video_results"):
        player.show_video_results(query, results)
    return results, _format_search_results(results, query, player)


def _open_selected(item: dict, session_memory, player=None, playlist=None, browser: str = "") -> str:
    if not item:
        return "Cette vidéo YouTube est introuvable."
    url = item.get("url", "")
    if not url:
        return "Cette vidéo YouTube n'a pas d'adresse de lecture valide."
    if session_memory is not None:
        session_memory[_YT_CURRENT_KEY] = item
    title = item.get("title") or "la vidéo"

    # Si browser == "chrome", ouvrir dans Google Chrome même si un player Qt est fourni
    if browser != "chrome" and player is not None and hasattr(player, "play_video"):
        if session_memory is not None:
            session_memory["youtube_playback_target"] = "player"
        player.play_video(item, playlist or (session_memory or {}).get(_YT_RESULTS_KEY, []))
        return f"Lecture de « {title} » dans le lecteur vidéo intégré."

    if session_memory is not None:
        session_memory["youtube_playback_target"] = "browser"

    if not _open_url(url):
        return "Impossible d'ouvrir cette vidéo."
    return f"YouTube ouvert sur « {title} ». Le contrôle vocal du lecteur est actif."


def _youtube_control(action: str, params: dict, player=None, session_memory=None) -> str:
    mapped = _CONTROL_ACTIONS[action]
    value = params.get("value")
    if action == "volume":
        value = params.get("volume", value)
    elif action in {"seek", "forward", "back"}:
        value = params.get("seconds", value)
        try:
            value = abs(int(value or 10)) * (-1 if action == "back" else 1)
        except (TypeError, ValueError):
            value = -10 if action == "back" else 10
    elif action == "speed":
        value = params.get("speed", value)

    # Vérification de la cible de lecture et de l'état actif du lecteur Qt
    target = (session_memory or {}).get("youtube_playback_target")
    is_qt_active = False
    if target != "browser" and player is not None and hasattr(player, "control_video"):
        if hasattr(player, "is_video_active"):
            is_qt_active = bool(player.is_video_active())
        elif hasattr(player, "is_active"):
            is_qt_active = bool(player.is_active())
        elif hasattr(player, "video_status"):
            status = str(player.video_status() or "").strip()
            is_qt_active = status not in ("", "Lecteur inactif", "inactif")
        else:
            is_qt_active = True

    if not is_qt_active:
        return media_control({"action": mapped, "value": value})

    player.control_video(action, value)
    labels = {
        "pause": "Vidéo mise en pause.", "resume": "Lecture reprise.",
        "toggle": "Lecture basculée.", "mute": "Son de la vidéo basculé.",
        "next": "Lecture de la vidéo suivante.",
        "previous": "Lecture de la vidéo précédente.",
        "fullscreen": "Mode plein écran vidéo basculé.",
        "stop": "Vidéo arrêtée. Les résultats restent affichés.",
        "close": "Lecteur vidéo fermé.",
    }
    return labels.get(action, "Commande envoyée au lecteur vidéo intégré.")

# ── Résolution d'URL vidéo (scraping + yt-dlp) ─────────────────────────────
def _scrape_first_video_url(query: str) -> Optional[str]:
    if not _REQUESTS_OK:
        return None
    search_url = (
        f"https://www.youtube.com/results"
        f"?search_query={quote_plus(query)}"
        f"&sp={_YT_VIDEO_FILTER}"
    )
    try:
        r    = kit.http().get(search_url, headers=HEADERS, timeout=10)
        html = r.text
        video_ids = re.findall(r'"videoId":"([A-Za-z0-9_-]{11})"', html)
        seen = set()
        for vid in video_ids:
            if vid in seen:
                continue
            seen.add(vid)
            if f'/shorts/{vid}' in html:
                continue
            return f"https://www.youtube.com/watch?v={vid}"
    except Exception as e:
        print(f"[YouTube] ⚠️ scrape_first_video_url failed: {e}")
    return None

def _ytdlp_first_video_url(query: str) -> Optional[str]:
    """Repli via yt-dlp pour trouver l'URL de la première vidéo."""
    if not shutil.which("yt-dlp"):
        return None
    try:
        r = kit.run(
            ["yt-dlp", f"ytsearch1:{query}", "--get-id", "--no-warnings"], timeout=30,
        )
        lines = [l.strip() for l in (r.stdout or "").splitlines() if l.strip()]
        if lines:
            return f"https://www.youtube.com/watch?v={lines[0]}"
    except Exception as e:
        print(f"[YouTube] ⚠️ yt-dlp search failed: {e}")
    return None

def _resolve_video_url(query: str) -> Optional[str]:
    """Trouve l'URL d'une vidéo : scraping d'abord, yt-dlp ensuite."""
    url = _scrape_first_video_url(query)
    if url:
        return url
    return _ytdlp_first_video_url(query)

def _extract_video_id(url: str) -> Optional[str]:
    match = re.search(
        r"(?:v=|/v/|youtu.be/|/embed/|/shorts/)([A-Za-z0-9_-]{11})", url
    )
    return match.group(1) if match else None

def _is_valid_youtube_url(url: str) -> bool:
    return bool(re.search(r"(youtube.com|youtu.be)", url or ""))

def _ask_for_url(prompt_text: str = "URL de la vidéo YouTube :") -> Optional[str]:
    try:
        import tkinter as tk
        from tkinter import simpledialog
        root = tk._default_root
        if root is None:
            root = tk.Tk()
            root.withdraw()
        url = simpledialog.askstring("J.A.R.V.I.S", prompt_text, parent=root)
        return url.strip() if url else None
    except Exception as e:
        print(f"[YouTube] ⚠️ URL dialog failed: {e}")
        return None

# ── Transcript ──────────────────────────────────────────────────────────────
def _get_transcript(video_id: str) -> Optional[str]:
    if not _TRANSCRIPT_OK:
        return None
    try:
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
        transcript = None
        lang_priority = ["en", "fr", "de", "es", "it", "pt", "ru", "ja", "ko", "ar", "zh", "tr"]
        try:
            transcript = transcript_list.find_manually_created_transcript(lang_priority)
        except Exception:
            pass
        if transcript is None:
            try:
                transcript = transcript_list.find_generated_transcript(lang_priority)
            except Exception:
                # Prendre n'importe quel transcript disponible
                for t in transcript_list:
                    transcript = t
                    break
        if transcript is None:
            return None
        fetched = transcript.fetch()
        return " ".join(entry["text"] for entry in fetched)
    except Exception as e:
        print(f"[YouTube] ⚠️ Transcript fetch failed: {e}")
        return None

def _summarize_with_gemini(transcript: str, video_url: str) -> str:
    from google import genai as _genai
    from google.genai import types
    _client = _genai.Client(api_key=_get_api_key())
    max_chars = 80000
    truncated = transcript[:max_chars] + ("..." if len(transcript) > max_chars else "")
    response = _client.models.generate_content(
        model=BALANCED_MODEL,
        contents=f"Résume cette transcription de vidéo YouTube :\n\n{truncated}",
        config=types.GenerateContentConfig(
            system_instruction=(
                "Tu es JARVIS, un assistant IA. Résume les transcriptions de vidéos YouTube de manière claire et concise. "
                "Structure : une phrase de vue d'ensemble, puis 3 à 5 points clés. Sois direct. "
                "Adapte la langue de la réponse à celle de la transcription."
            )
        )
    )
    return response.text.strip()

def _save_summary(content: str, video_url: str) -> str:
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"youtube_summary_{ts}.txt"
    desktop  = Path.home() / "Desktop"
    desktop.mkdir(parents=True, exist_ok=True)
    filepath = desktop / filename
    header = (
        f"JARVIS — Résumé YouTube\n"
        f"{'─' * 50}\n"
        f"URL    : {video_url}\n"
        f"Date   : {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"{'─' * 50}\n\n"
    )
    filepath.write_text(header + content, encoding="utf-8")
    try:
        if _is_windows():
            kit.spawn(["notepad.exe", str(filepath)])
        elif _is_mac():
            kit.spawn(["open", "-t", str(filepath)])
        else:
            kit.spawn(['xdg-open', str(filepath)])
    except Exception as e:
        print(f"[YouTube] ⚠️ Could not open text editor: {e}")
    return str(filepath)

def _scrape_video_info(video_id: str) -> dict:
    if not _REQUESTS_OK:
        return {}
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        r    = kit.http().get(url, headers=HEADERS, timeout=12)
        html = r.text
        info = {}
        for key, pattern in [
            ("title",    r'"title":{"runs":\[{"text":"([^"]+)'),
            ("channel",  r'"ownerChannelName":"([^"]+)'),
            ("views",    r'"viewCount":"(\d+)'),
            ("duration", r'"lengthSeconds":"(\d+)'),
            ("likes",    r'"label":"([0-9,]+ likes)'),
        ]:
            match = re.search(pattern, html)
            if match:
                raw = match.group(1)
                if key == "views":
                    info[key] = f"{int(raw):,}"
                elif key == "duration":
                    secs = int(raw)
                    info[key] = f"{secs // 60}:{secs % 60:02d}"
                else:
                    info[key] = raw
        return info
    except Exception as e:
        print(f"[YouTube] ⚠️ Info scrape failed: {e}")
        return {}

def _scrape_trending(region: str = "FR", max_results: int = 8) -> list:
    if not _REQUESTS_OK:
        return []
    url = f"https://www.youtube.com/feed/trending?gl={region.upper()}"
    try:
        r    = kit.http().get(url, headers=HEADERS, timeout=12)
        html = r.text
        titles   = re.findall(r'"title":{"runs":\[{"text":"([^"]+)"}]', html)
        channels = re.findall(r'"ownerText":{"runs":\[{"text":"([^"]+)"', html)
        results, seen = [], set()
        for i, title in enumerate(titles):
            if title in seen or len(title) < 5:
                continue
            seen.add(title)
            channel = channels[i] if i < len(channels) else "Inconnu"
            results.append({"rank": len(results) + 1, "title": title, "channel": channel})
            if len(results) >= max_results:
                break
        return results
    except Exception as e:
        print(f"[YouTube] ⚠️ Trending scrape failed: {e}")
        return []

# ── Gestion des actions ─────────────────────────────────────────────────────
def _handle_play(parameters: dict, player) -> str:
    query = parameters.get("query", "").strip()
    if not query:
        return "Dites-moi ce que vous voulez regarder."
    if player:
        player.write_log(f"[YouTube] Recherche : {query}")
    print(f"[YouTube] 🔍 Recherche de la première vidéo (hors Shorts) pour : {query}")
    video_url = _resolve_video_url(query)
    if video_url:
        print(f"[YouTube] ▶️ Lecture : {video_url}")
        if _play_video(video_url):
            return f"Lecture de : {query}"
    print("[YouTube] ⚠️ Résolution échouée, ouverture de la page de recherche")
    fallback_url = (
        f"https://www.youtube.com/results"
        f"?search_query={quote_plus(query)}"
        f"&sp={_YT_VIDEO_FILTER}"
    )
    _open_url(fallback_url)
    return f"Recherche YouTube ouverte pour : {query} (sélection manuelle nécessaire)"

def _handle_summarize(parameters: dict, player, speak) -> str:
    if not _TRANSCRIPT_OK:
        return "youtube-transcript-api n'est pas installé. Lancez : pip install youtube-transcript-api"
    # Récupérer l'URL depuis les paramètres, ou chercher depuis une requête
    url   = parameters.get("url", "").strip()
    query = parameters.get("query", "").strip()
    if not url and query:
        # Pas d'URL mais une requête : on cherche la vidéo
        url = _resolve_video_url(query) or ""
    if not url:
        url = _ask_for_url("Veuillez coller l'URL de la vidéo YouTube :")
    if not url:
        return "Aucune URL fournie. Résumé annulé."
    if not _is_valid_youtube_url(url):
        return "Cette URL ne semble pas être une URL YouTube valide."
    video_id = _extract_video_id(url)
    if not video_id:
        return "Impossible d'extraire l'ID de la vidéo depuis cette URL."
    if player:
        player.write_log(f"[YouTube] Résumé : {url}")
    if speak:
        speak("Récupération de la transcription en cours...")
    transcript = _get_transcript(video_id)
    if not transcript:
        return "Je n'ai pas pu obtenir la transcription de cette vidéo."
    if speak:
        speak("Transcription récupérée. Génération du résumé...")
    try:
        summary = _summarize_with_gemini(transcript, url)
    except Exception as e:
        return f"Échec de la génération du résumé : {e}"
    if speak:
        speak(summary)
    if parameters.get("save", True):
        saved_path = _save_summary(summary, url)
        return f"Résumé terminé et sauvegardé sur le bureau : {saved_path}"
    return summary

def _handle_get_info(parameters: dict, player, speak) -> str:
    url   = parameters.get("url", "").strip()
    query = parameters.get("query", "").strip()
    if not url and query:
        url = _resolve_video_url(query) or ""
    if not url:
        url = _ask_for_url("Veuillez coller l'URL de la vidéo YouTube :")
    if not url or not _is_valid_youtube_url(url):
        return "Veuillez fournir une URL YouTube valide."
    video_id = _extract_video_id(url)
    if not video_id:
        return "Impossible d'extraire l'ID de la vidéo."
    if player:
        player.write_log(f"[YouTube] Infos : {url}")
    info = _scrape_video_info(video_id)
    if not info:
        return "Impossible de récupérer les informations de la vidéo."
    translate = {"title": "Titre", "channel": "Chaîne", "views": "Vues",
                 "duration": "Durée", "likes": "Likes"}
    lines = [f"{translate.get(key, key)} : {info[key]}"
             for key in ("title", "channel", "views", "duration", "likes") if key in info]
    result = "\n".join(lines)
    if speak:
        speak("Voici les informations de la vidéo. " + result.replace('\n', '. '))
    return result

def _handle_trending(parameters: dict, player, speak) -> str:
    region = parameters.get("region", "FR").upper()
    if player:
        player.write_log(f"[YouTube] Tendances : {region}")
    trending = _scrape_trending(region=region, max_results=8)
    if not trending:
        return f"Impossible de récupérer les tendances pour la région {region}."
    lines  = [f"Top tendances YouTube en {region} :"]
    lines += [f"{v['rank']}. {v['title']} — {v['channel']}" for v in trending]
    result = "\n".join(lines)
    if speak:
        top3 = trending[:3]
        spoken = "Voici les vidéos les plus tendance. " + ". ".join(
            f"Numéro {v['rank']} : {v['title']} par {v['channel']}" for v in top3
        )
        speak(spoken)
    return result

_ACTION_MAP = {
    "play":      _handle_play,
    "summarize": _handle_summarize,
    "get_info":  _handle_get_info,
    "trending":  _handle_trending,
}

# ── Parsing local multilingue ──────────────────────────────────────────────
def _parse_youtube_command_locally(text: str) -> Optional[Dict[str, Any]]:
    """
    Détecte l'action YouTube et extrait les paramètres (requête, URL, région).
    """
    text = text.lower().strip()
    text = re.sub(r"\b(s'il te pla[iî]t|stp|please|peux-tu|tu peux|je veux|j'aimerais)\b", "", text).strip()

    # 1. Une demande de recherche ne doit jamais lancer automatiquement une
    # vidéo. C'est précisément ce qui distinguait mal YouTube de la musique.
    search_patterns = [
        r"(?:cherche|recherche|trouve|find|search)\s+(?:la |le |les |l'|une |un )?(?:vidéo|clip|tuto|film )?(.+?)(?:\s+sur\s+youtube)?$",
    ]
    for pat in search_patterns:
        m = re.search(pat, text)
        if m:
            query = m.group(1).strip()
            return {"action": "search", "query": query}

    # 2. Action "select" : commandes ciblées sur un numéro ou ordinal de résultat
    select_patterns = [
        r"(?:ouvre|lance|mets|lis|joue|regarde|choisis|sélectionne)\s+(?:le\s+résultat(?:\s+vidéo)?(?:\s+youtube)?(?:\s+numéro|\s+n°|\s+#)?|la\s+vidéo(?:\s+numéro|\s+n°|\s+#)?|le\s+choix|le|la)?\s*([0-9]{1,2}|premier|première|1er|1ère|deuxième|second|seconde|2ème|2eme|troisième|3ème|3eme|quatrième|4ème|cinquième|sixième|septième|huitième|neuvième|dixième)\b",
        r"(?:choisis|sélectionne)\s+(?:la\s+vidéo|le\s+résultat|le|la)?\s*([0-9]{1,2}|premier|première|1er|1ère|deuxième|second|seconde|2ème|2eme|troisième|3ème|3eme|quatrième|4ème|cinquième|sixième|septième|huitième|neuvième|dixième)\b",
    ]
    for pat in select_patterns:
        m = re.search(pat, text)
        if m:
            idx_str = m.group(1).strip()
            return {"action": "select", "index": idx_str}

    # 3. Action "play" : verbe de lecture explicite.
    play_patterns = [
        r"(?:joue|lance|mets|ouvre|regarde|play|watch)\s+(?:la |le |les |l'|une |un )?(?:vidéo|clip|tuto|film )?(.+?)(?:\s+sur\s+youtube)?$",
    ]
    for pat in play_patterns:
        m = re.search(pat, text)
        if m:
            query = m.group(1).strip()
            return {"action": "play", "query": query}

    # 3. Action "summarize"
    summarize_patterns = [
        r"(?:résume|summarize|fais un résumé de|résumé)\s+(?:cette |la |l'|la vidéo )?(https?://\S+)",
        r"(?:résume|summarize)\s+(?:cette |la |l'|la vidéo )?(.+)",
    ]
    for pat in summarize_patterns:
        m = re.search(pat, text)
        if m:
            url_or_query = m.group(1).strip()
            if url_or_query.startswith("http"):
                return {"action": "summarize", "url": url_or_query}
            else:
                return {"action": "summarize", "query": url_or_query}

    # 4. Action "get_info"
    info_patterns = [
        r"(?:infos?|détails|détaille|renseignements?|information)\s+(?:sur |de |de la |de l'|de cette )?(?:la |l'|la vidéo )?(https?://\S+)",
        r"(?:donne-moi les infos|dis-moi tout sur)\s+(?:la |cette |la vidéo )?(https?://\S+)",
    ]
    for pat in info_patterns:
        m = re.search(pat, text)
        if m:
            return {"action": "get_info", "url": m.group(1).strip()}

    # 5. Action "trending"
    if re.search(r"\b(tendances?|trending|top|populaires?|actu(?:alités?)?\s*youtube)\b", text):
        region_match = re.search(r"\b(fr|us|gb|de|tr|jp|kr)\b", text)
        region = region_match.group(1).upper() if region_match else "FR"
        return {"action": "trending", "region": region}

    # 6. Recherche par défaut si "youtube" est mentionné sans verbe de lecture.
    if re.search(r"\byoutube\b", text):
        rest = re.sub(r"\b(youtube|sur youtube)\b", "", text).strip()
        if rest:
            return {"action": "search", "query": rest}
    return None

def _detect_youtube_intent_ai(description: str) -> Optional[Dict]:
    api_key = _get_api_key()
    if not api_key:
        return None
    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        prompt = (
            f"Analyse la phrase suivante et détermine l'action YouTube à effectuer.\n"
            f"Actions possibles : search (query, affiche une liste sans lecture), play (query, ouvre la première vidéo), "
            f"select (index), pause, resume, volume, seek, fullscreen, subtitles, speed, "
            f"summarize (url ou query), get_info (url ou query), transcript, trending (region).\n"
            f"Règle stricte : cherche/recherche/trouve = search ; joue/lance/ouvre/regarde = play.\n"
            f"Retourne UNIQUEMENT un objet JSON avec l'action et les paramètres.\n"
            f"Phrase : \"{description}\""
        )
        resp = client.models.generate_content(model=FAST_MODEL, contents=prompt)
        json_match = re.search(r'{.*}', resp.text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group(0))
    except Exception as e:
        print(f"[YouTube] Erreur IA intent: {e}")
    return None

# ── Point d'entrée principal ──────────────────────────────────────────────
@kit.action("youtube_video")
def youtube_video(
    parameters:     dict,
    response=None,
    player=None,
    session_memory=None,
    speak=None,
) -> str:
    """Point d'entrée unique de recherche et contrôle YouTube."""
    params = parameters or {}
    description = params.get("description", "").strip()
    action = params.get("action", "").lower().strip()
    query  = params.get("query", "").strip()
    url    = params.get("url", "").strip()
    region = params.get("region", "FR").strip()

    aliases = {
        "find": "search", "recherche": "search", "open": "play",
        "watch": "play", "select": "select", "choose": "select",
        "info": "get_info", "details": "get_info", "summary": "summarize",
        "transcription": "transcript", "caption": "subtitles",
        "play": "play", "unpause": "resume", "cinema": "theater",
        "full_screen": "fullscreen", "mini_player": "miniplayer",
    }
    action = aliases.get(action, action)

    # Interprétation naturelle conservée pour les appels texte directs.
    if description and not action:
        local = _parse_youtube_command_locally(description)
        if local:
            action = local.get("action", action)
            query  = local.get("query", query)
            url    = local.get("url", url)
            region = local.get("region", region)
        else:
            ai = _detect_youtube_intent_ai(description)
            if ai:
                action = ai.get("action", action)
                query  = ai.get("query", query)
                url    = ai.get("url", url)
                region = ai.get("region", region)
            else:
                if "youtube" in description.lower():
                    action = "search"
                    query = description
                else:
                    return "Je n'ai pas compris la commande YouTube."

    if not action:
        return (
            "Actions YouTube : rechercher, lire/sélectionner, pause, reprendre, "
            "volume, avancer/reculer, vitesse, plein écran, sous-titres, mode "
            "cinéma, mini-lecteur, suivante/précédente, infos, transcription et résumé."
        )

    if player and hasattr(player, "write_log"):
        player.write_log(f"[YouTube] Action: {action}")
    print(f"[YouTube] ▶️  Action: {action}  Params: {params}")

    # Fusionner les paramètres pour les handlers historiques (résumé, infos).
    params["query"]  = query or params.get("query", "")
    params["url"]    = url or params.get("url", "")
    params["region"] = region or params.get("region", "FR")
    browser = str(params.get("browser") or params.get("prefer_browser") or "").lower().strip()

    try:
        if action == "search":
            if not query:
                return "Quelle vidéo voulez-vous rechercher sur YouTube ?"
            limit = max(1, min(int(params.get("limit", 6) or 6), 12))
            try:
                results, formatted = _search_and_remember(
                    query, limit, session_memory, player
                )
            except YouTubeUnavailable:
                # Le lecteur ANO-GPT est le seul rendu autorisé : ne jamais
                # remplacer une carte de résultats par un onglet Chrome.
                if session_memory is not None:
                    session_memory[_YT_RESULTS_KEY] = []
                    session_memory["youtube_last_query"] = query
                return (
                    f"La recherche vidéo intégrée est momentanément indisponible pour « {query} ». "
                    "Réessaie dans un instant ; aucun navigateur externe n'a été ouvert."
                )
            return formatted if results else f"Aucun résultat YouTube pour « {query} »."

        if action in {"select", "play"}:
            # Une URL explicite est toujours prioritaire.
            if url and _is_valid_youtube_url(url):
                video_id = _extract_video_id(url) or ""
                direct = {
                    "id": video_id, "url": url,
                    "title": query or "Vidéo YouTube", "channel": "",
                    "duration": "", "views": 0,
                }
                return _open_selected(direct, session_memory, player, [direct], browser=browser)

            selection = params.get("index", params.get("result", params.get("value")))
            remembered = (session_memory or {}).get(_YT_RESULTS_KEY, [])
            if action == "select" or selection not in (None, ""):
                selected = resolve_result(remembered, selection or query)
                if not selected:
                    return "Ce résultat YouTube n'existe pas. Relancez une recherche ou choisissez un numéro affiché."
                return _open_selected(selected, session_memory, player, remembered, browser=browser)

            if remembered and query:
                candidate = resolve_result(remembered, query)
                if candidate:
                    return _open_selected(candidate, session_memory, player, remembered, browser=browser)

            if not query:
                # « Lis la première » peut arriver comme play sans index.
                selected = resolve_result(remembered, 1)
                return _open_selected(selected, session_memory, player, remembered, browser=browser) if selected else "Précisez la vidéo à lire."

            try:
                results, _ = _search_and_remember(query, 6, session_memory, player)
            except YouTubeUnavailable:
                return (
                    f"La recherche vidéo intégrée est momentanément indisponible pour « {query} ». "
                    "Réessaie dans un instant ; aucun navigateur externe n'a été ouvert."
                )
            if not results:
                return f"Aucune vidéo YouTube trouvée pour « {query} »."
            return _open_selected(results[0], session_memory, player, results, browser=browser)

        if action in _CONTROL_ACTIONS:
            return _youtube_control(action, params, player, session_memory)

        if action == "status":
            current = (session_memory or {}).get(_YT_CURRENT_KEY)
            now = (
                player.video_status() if player is not None and hasattr(player, "video_status")
                else media_control({"action": "now_playing"})
            )
            if current:
                return f"Vidéo YouTube sélectionnée : {current.get('title')}. {now}"
            return now

        if action == "transcript":
            target_url = url
            if not target_url:
                current = (session_memory or {}).get(_YT_CURRENT_KEY, {})
                target_url = current.get("url", "")
            if not target_url and query:
                results, _ = _search_and_remember(query, 1, session_memory)
                target_url = results[0]["url"] if results else ""
            video_id = _extract_video_id(target_url)
            if not video_id:
                return "Précisez une URL ou lancez d'abord une vidéo YouTube."
            transcript = _get_transcript(video_id)
            if not transcript:
                return "Aucune transcription n'est disponible pour cette vidéo."
            max_chars = max(500, min(int(params.get("max_chars", 12000)), 30000))
            suffix = "\n\n[Transcription tronquée]" if len(transcript) > max_chars else ""
            return transcript[:max_chars] + suffix

        handler = _ACTION_MAP.get(action)
        if handler:
            if action == "play":
                return handler(params, player) or "Terminé."
            return handler(params, player, speak) or "Terminé."

        return f"Action YouTube inconnue : '{action}'."
    except YouTubeUnavailable as exc:
        return f"Recherche YouTube indisponible : {exc}"
    except Exception as e:
        print(f"[YouTube] ❌ Erreur dans {action}: {e}")
        return f"L'action YouTube {action} a échoué : {e}"

# ── Test direct ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        print(youtube_video({"description": " ".join(sys.argv[1:])}))
    else:
        print("Usage: python youtube_video.py \"joue la chanson de Daft Punk\"")
