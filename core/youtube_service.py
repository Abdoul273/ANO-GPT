"""Recherche YouTube structurée avec yt-dlp et état de sélection."""

from __future__ import annotations

import json
import re
import shutil
from core.action_kit import TTLCache
import subprocess
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import asdict, dataclass
from typing import Any, Callable, List


class YouTubeUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class YouTubeResult:
    id: str
    title: str
    url: str
    channel: str = ""
    duration_seconds: int = 0
    views: int = 0
    live: bool = False

    @property
    def duration(self) -> str:
        if self.live:
            return "EN DIRECT"
        seconds = max(0, self.duration_seconds)
        if not seconds:
            return ""
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["duration"] = self.duration
        result["thumbnail_url"] = f"https://i.ytimg.com/vi/{self.id}/hqdefault.jpg"
        return result


def _extract_video_renderers(obj: Any) -> list[dict[str, Any]]:
    renderers: list[dict[str, Any]] = []
    if isinstance(obj, dict):
        if "videoRenderer" in obj and isinstance(obj["videoRenderer"], dict):
            renderers.append(obj["videoRenderer"])
        for v in obj.values():
            renderers.extend(_extract_video_renderers(v))
    elif isinstance(obj, list):
        for item in obj:
            renderers.extend(_extract_video_renderers(item))
    return renderers


def _parse_yt_initial_data(html: str) -> dict[str, Any] | None:
    match = re.search(r"ytInitialData\s*=\s*({.+?});\s*(?:</script>|var )", html, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except Exception:
            pass
    idx = html.find("ytInitialData")
    if idx != -1:
        brace_start = html.find("{", idx)
        if brace_start != -1:
            end_script = html.find("</script>", brace_start)
            if end_script != -1:
                candidate = html[brace_start:end_script].rstrip(";").strip()
                try:
                    return json.loads(candidate)
                except Exception:
                    pass
    return None


def _extract_text(field: Any) -> str:
    if not field:
        return ""
    if isinstance(field, str):
        return field
    if isinstance(field, dict):
        if "simpleText" in field:
            return str(field["simpleText"])
        if "runs" in field and isinstance(field["runs"], list):
            return "".join(str(r.get("text", "")) for r in field["runs"] if isinstance(r, dict))
    return ""


def _parse_duration(length_str: str) -> int:
    if not length_str:
        return 0
    parts = [p.strip() for p in length_str.split(":") if p.strip().isdigit()]
    try:
        if len(parts) == 1:
            return int(parts[0])
        elif len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        elif len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except Exception:
        pass
    return 0


def _parse_views(view_str: str) -> int:
    if not view_str:
        return 0
    digits = re.sub(r"[^\d]", "", view_str)
    try:
        return int(digits) if digits else 0
    except Exception:
        return 0


def _search_youtube_scraping(query: str, limit: int = 6) -> List[YouTubeResult]:
    """Recherche YouTube par scraping direct en mode repli."""
    import requests
    from urllib.parse import quote_plus

    url = f"https://www.youtube.com/results?search_query={quote_plus(query)}&sp=EgIQAQ%3D%3D"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if not response.ok:
            return []
    except Exception:
        return []

    html = response.text
    results: List[YouTubeResult] = []
    seen: set[str] = set()

    data = _parse_yt_initial_data(html)
    if data:
        renderers = _extract_video_renderers(data)
        for vr in renderers:
            video_id = str(vr.get("videoId") or "").strip()
            if not video_id or video_id in seen:
                continue
            seen.add(video_id)
            title = _extract_text(vr.get("title")) or "Sans titre"
            channel = (
                _extract_text(vr.get("ownerText"))
                or _extract_text(vr.get("longBylineText"))
                or _extract_text(vr.get("shortBylineText"))
            )
            duration_str = _extract_text(vr.get("lengthText"))
            duration_sec = _parse_duration(duration_str)
            views_str = _extract_text(vr.get("viewCountText"))
            views = _parse_views(views_str)
            is_live = False
            badges = vr.get("badges", [])
            for badge in badges:
                badge_text = _extract_text(badge.get("metadataBadgeRenderer", {}).get("label")).upper()
                if "LIVE" in badge_text or "DIRECT" in badge_text:
                    is_live = True

            results.append(YouTubeResult(
                id=video_id,
                title=title,
                url=f"https://www.youtube.com/watch?v={video_id}",
                channel=channel,
                duration_seconds=duration_sec,
                views=views,
                live=is_live,
            ))
            if len(results) >= limit:
                break

    if not results:
        # Fallback par expressions régulières directes
        video_ids = re.findall(r'"videoId":"([A-Za-z0-9_-]{11})"', html)
        titles = re.findall(r'"title":\{"runs":\[\{"text":"([^"]+)"', html)
        channels = re.findall(r'"ownerText":\{"runs":\[\{"text":"([^"]+)"', html)
        for i, vid in enumerate(video_ids):
            if vid in seen:
                continue
            seen.add(vid)
            title = titles[i] if i < len(titles) else "Sans titre"
            channel = channels[i] if i < len(channels) else ""
            results.append(YouTubeResult(
                id=vid,
                title=title,
                url=f"https://www.youtube.com/watch?v={vid}",
                channel=channel,
            ))
            if len(results) >= limit:
                break

    return results


_HEDGE_AFTER_S = 2.5
_SEARCH_TTL = 120.0
_SEARCH_CACHE = TTLCache(maxsize=64)


def search_youtube(
    query: str,
    limit: int = 6,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> List[YouTubeResult]:
    query = (query or "").strip()
    if not query:
        return []
    limit = max(1, min(int(limit), 20))

    cache_key = (query.casefold(), limit)
    cached = _SEARCH_CACHE.get(cache_key)
    if cached is not None:
        return list(cached)

    results: List[YouTubeResult] = []
    ytdlp_available = bool(shutil.which("yt-dlp"))

    if ytdlp_available:
        command = [
            "yt-dlp", f"ytsearch{limit}:{query}", "--flat-playlist",
            "--dump-json", "--no-warnings", "--no-playlist",
            "--socket-timeout", "12",
        ]
        # yt-dlp est le plus fiable mais met souvent 4 à 8 s ; le scraping de
        # la page de résultats répond en une seconde. On lance yt-dlp, et si
        # il traîne au-delà de _HEDGE_AFTER_S le scraping part en parallèle :
        # le premier lot exploitable gagne.
        pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="yt-search")
        ytdlp_future = pool.submit(runner, command, capture_output=True, text=True, timeout=35)
        scrape_future = None
        try:
            try:
                process = ytdlp_future.result(timeout=_HEDGE_AFTER_S)
            except FutureTimeout:
                scrape_future = pool.submit(_search_youtube_scraping, query, limit)
                while True:
                    try:
                        process = ytdlp_future.result(timeout=0.1)
                        break
                    except FutureTimeout:
                        pass
                    if scrape_future.done() and not scrape_future.exception():
                        scraped = scrape_future.result()
                        if scraped:
                            pool.shutdown(wait=False, cancel_futures=True)
                            _SEARCH_CACHE.set(cache_key, scraped[:limit], _SEARCH_TTL)
                            return scraped[:limit]
                        scrape_future = None
            if process.returncode == 0 or (process.stdout or "").strip():
                seen: set[str] = set()
                for line in (process.stdout or "").splitlines():
                    try:
                        data = json.loads(line)
                    except (TypeError, json.JSONDecodeError):
                        continue
                    video_id = str(data.get("id") or "").strip()
                    if not video_id or video_id in seen:
                        continue
                    seen.add(video_id)
                    try:
                        duration = int(data.get("duration") or 0)
                    except (TypeError, ValueError):
                        duration = 0
                    try:
                        views = int(data.get("view_count") or 0)
                    except (TypeError, ValueError):
                        views = 0
                    results.append(YouTubeResult(
                        id=video_id,
                        title=str(data.get("title") or "Sans titre"),
                        url=f"https://www.youtube.com/watch?v={video_id}",
                        channel=str(data.get("channel") or data.get("uploader") or ""),
                        duration_seconds=duration,
                        views=views,
                        live=str(data.get("live_status") or "").lower() in {"is_live", "is_upcoming"},
                    ))
                if results:
                    _SEARCH_CACHE.set(cache_key, results[:limit], _SEARCH_TTL)
                    return results[:limit]
        except subprocess.TimeoutExpired as exc:
            raise YouTubeUnavailable("la recherche YouTube a expiré.") from exc
        except Exception:
            # yt-dlp a échoué (SubprocessError, etc.) -> fallback au scraping
            pass
        finally:
            pool.shutdown(wait=False)

    # Fallback au scraping direct
    try:
        results = _search_youtube_scraping(query, limit)
        if results:
            _SEARCH_CACHE.set(cache_key, results[:limit], _SEARCH_TTL)
            return results[:limit]
    except Exception:
        pass

    if not ytdlp_available and not results:
        raise YouTubeUnavailable("yt-dlp n'est pas installé et le scraping a échoué.")
    if not results:
        raise YouTubeUnavailable("la recherche YouTube a échoué.")

    return results[:limit]


_FRENCH_ORDINALS = {
    "premier": 1, "premiere": 1, "première": 1, "1er": 1, "1ere": 1, "1ère": 1,
    "deuxieme": 2, "deuxième": 2, "second": 2, "seconde": 2, "2eme": 2, "2ème": 2,
    "troisieme": 3, "troisième": 3, "3eme": 3, "3ème": 3,
    "quatrieme": 4, "quatrième": 4, "4eme": 4, "4ème": 4,
    "cinquieme": 5, "cinquième": 5, "5eme": 5, "5ème": 5,
    "sixieme": 6, "sixième": 6, "6eme": 6, "6ème": 6,
    "septieme": 7, "septième": 7, "7eme": 7, "7ème": 7,
    "huitieme": 8, "huitième": 8, "8eme": 8, "8ème": 8,
    "neuvieme": 9, "neuvième": 9, "9eme": 9, "9ème": 9,
    "dixieme": 10, "dixième": 10, "10eme": 10, "10ème": 10,
}


def resolve_result(results: list[dict[str, Any]], value: Any) -> dict[str, Any] | None:
    """Résout un résultat par numéro (1-based), ordinal, ID ou titre partiel."""
    if not results:
        return None
    text = str(value or "1").strip()
    if text.isdigit():
        index = int(text) - 1
        return results[index] if 0 <= index < len(results) else None
    lowered = text.casefold()
    if lowered in _FRENCH_ORDINALS:
        index = _FRENCH_ORDINALS[lowered] - 1
        return results[index] if 0 <= index < len(results) else None
    for result in results:
        if lowered == str(result.get("id", "")).casefold():
            return result
    for result in results:
        if lowered in str(result.get("title", "")).casefold():
            return result
    return None

