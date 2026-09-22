"""Recherche d'images native pour la galerie plein écran d'ANO-GPT.

Le navigateur de l'utilisateur n'est jamais ouvert. Les résultats sont cherchés
en arrière-plan, classés par proximité avec la requête, téléchargés avec des
garde-fous réseau puis décodés avant d'être transmis à Qt.
"""

from __future__ import annotations

import hashlib
import io
import ipaddress
import re
import socket
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from PIL import Image, ImageOps, UnidentifiedImageError

from core import action_kit as kit


_MAX_DOWNLOAD_BYTES = 12 * 1024 * 1024
_MAX_PIXELS = 32_000_000
_MIN_EDGE = 180
_MAX_EDGE = 2560
_DIRECT_IMAGE_RE = re.compile(r"\.(?:avif|gif|jpe?g|png|webp)(?:$|[?#])", re.I)
_STOP_WORDS = {
    "a", "au", "aux", "avec", "d", "de", "des", "du", "en", "et", "l",
    "la", "le", "les", "me", "moi", "montre", "photo", "photos", "image",
    "images", "pour", "sur", "un", "une", "the", "of", "show", "picture",
    "pictures", "find", "search",
}


def _fold(text: str) -> str:
    value = unicodedata.normalize("NFKD", str(text or "").casefold())
    value = "".join(char for char in value if not unicodedata.combining(char))
    return " ".join(re.findall(r"[a-z0-9]+", value))


def _query_tokens(query: str) -> tuple[str, ...]:
    return tuple(
        token for token in _fold(query).split()
        if len(token) > 1 and token not in _STOP_WORDS
    )


def _candidate_score(candidate: dict[str, Any], query: str, position: int) -> float:
    """Classe la proximité sémantique simple avant tout téléchargement.

    Le titre porte plus d'information que le domaine ou l'URL. La position du
    moteur reste un signal secondaire, tout comme la résolution annoncée.
    """
    folded_query = _fold(query)
    title = _fold(candidate.get("title", ""))
    source = _fold(candidate.get("source", ""))
    url_text = _fold(candidate.get("image_url", ""))
    tokens = _query_tokens(query)

    score = max(0.0, 7.0 - position * 0.22)
    if folded_query and folded_query in title:
        score += 18.0
    for token in tokens:
        if token in title.split():
            score += 7.0
        elif token in title:
            score += 4.0
        if token in source:
            score += 1.8
        if token in url_text:
            score += 1.2
    if _DIRECT_IMAGE_RE.search(str(candidate.get("image_url", ""))):
        score += 2.0
    width = int(candidate.get("width") or 0)
    height = int(candidate.get("height") or 0)
    if min(width, height) >= 720:
        score += 2.5
    elif min(width, height) >= 360:
        score += 1.0
    return score


def _normalise_candidate(raw: dict[str, Any]) -> dict[str, Any] | None:
    def as_int(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            match = re.search(r"\d+", str(value or ""))
            return int(match.group()) if match else 0

    image_url = str(
        raw.get("original") or raw.get("image") or raw.get("image_url") or ""
    ).strip()
    if not image_url:
        return None
    return {
        "title": str(raw.get("title") or raw.get("name") or "Image").strip(),
        "image_url": image_url,
        "thumbnail_url": str(raw.get("thumbnail") or "").strip(),
        "source_url": str(raw.get("link") or raw.get("url") or raw.get("source_url") or "").strip(),
        "source": str(raw.get("source") or raw.get("domain") or "Web").strip(),
        "width": as_int(raw.get("original_width") or raw.get("width")),
        "height": as_int(raw.get("original_height") or raw.get("height")),
    }


def _search_serpapi(query: str, count: int) -> list[dict[str, Any]]:
    from actions.web_search import _call_serpapi, _geo_params

    data = _call_serpapi({
        "q": query,
        "engine": "google_images",
        "ijn": "0",
        "safe": "active",
        **_geo_params(query),
    })
    results = data.get("images_results") or []
    return [candidate for raw in results[:count]
            if (candidate := _normalise_candidate(raw)) is not None]


def _public_http_url(url: str) -> bool:
    """Refuse les URL capables d'atteindre le réseau local ou les métadonnées."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        if parsed.username or parsed.password:
            return False
        default_port = 443 if parsed.scheme == "https" else 80
        addresses = socket.getaddrinfo(
            parsed.hostname, parsed.port or default_port, type=socket.SOCK_STREAM
        )
        if not addresses:
            return False
        for entry in addresses:
            ip = ipaddress.ip_address(entry[4][0].split("%", 1)[0])
            if not ip.is_global:
                return False
        return True
    except (OSError, ValueError):
        return False


def _open_public_response(url: str) -> requests.Response:
    """Suit au plus trois redirections, en validant chaque destination avant accès."""
    current = url
    for _ in range(4):
        if not _public_http_url(current):
            raise ValueError("destination réseau non publique")
        response = kit.http().get(
            current,
            timeout=(4.0, 10.0),
            stream=True,
            allow_redirects=False,
            headers={
                "User-Agent": "Mozilla/5.0 (ANO-GPT Image Gallery/1.0)",
                "Accept": "image/avif,image/webp,image/png,image/jpeg,image/*;q=0.8",
            },
        )
        if response.is_redirect or response.is_permanent_redirect:
            destination = urljoin(current, response.headers.get("location", ""))
            response.close()
            current = destination
            continue
        response.raise_for_status()
        return response
    raise ValueError("trop de redirections")


def _download_image(candidate: dict[str, Any]) -> dict[str, Any] | None:
    url = candidate["image_url"]
    try:
        with _open_public_response(url) as response:
            content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
            if content_type and not content_type.startswith("image/"):
                return None
            declared = int(response.headers.get("content-length") or 0)
            if declared > _MAX_DOWNLOAD_BYTES:
                return None

            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_content(64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > _MAX_DOWNLOAD_BYTES:
                    return None
                chunks.append(chunk)
            payload = b"".join(chunks)
        if not payload:
            return None

        with Image.open(io.BytesIO(payload)) as probe:
            width, height = probe.size
            if width * height > _MAX_PIXELS or min(width, height) < _MIN_EDGE:
                return None
            probe.verify()

        with Image.open(io.BytesIO(payload)) as opened:
            image = ImageOps.exif_transpose(opened)
            if getattr(image, "is_animated", False):
                image.seek(0)
            image.thumbnail((_MAX_EDGE, _MAX_EDGE), Image.Resampling.LANCZOS)
            has_alpha = image.mode in {"RGBA", "LA"} or "transparency" in image.info
            output = io.BytesIO()
            if has_alpha:
                image.convert("RGBA").save(output, format="PNG", optimize=True)
                mime = "image/png"
            else:
                image.convert("RGB").save(output, format="JPEG", quality=90, optimize=True)
                mime = "image/jpeg"
            clean = output.getvalue()

        result = dict(candidate)
        result.update({
            "bytes": clean,
            "width": image.width,
            "height": image.height,
            "mime": mime,
            "digest": hashlib.sha256(clean).hexdigest(),
        })
        return result
    except (
        requests.RequestException,
        OSError,
        ValueError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
    ):
        return None


_RESULT_CACHE = kit.TTLCache(maxsize=32)


def search_images(query: str, limit: int = 6) -> list[dict[str, Any]]:
    query = " ".join(str(query or "").split())
    if not query:
        return []
    limit = max(1, min(int(limit or 6), 8))
    cached = _RESULT_CACHE.get((query.casefold(), limit))
    if cached:
        return list(cached)
    candidate_count = min(18, max(10, limit * 2))

    candidates: list[dict[str, Any]] = []
    try:
        candidates = _search_serpapi(query, candidate_count)
    except Exception as serp_error:
        print(f"[ImageSearch] SerpApi indisponible : {serp_error}")

    for position, candidate in enumerate(candidates):
        candidate["_score"] = _candidate_score(candidate, query, position)
    ranked = sorted(candidates, key=lambda item: item["_score"], reverse=True)[:candidate_count]

    def safe_download(candidate: dict[str, Any]) -> dict[str, Any] | None:
        try:
            return _download_image(candidate)
        except Exception as error:
            print(f"[ImageSearch] Image ignorée ({error})")
            return None

    # Téléchargement par vagues dans l'ordre de pertinence : on s'arrête dès
    # que la galerie est pleine au lieu de rapatrier les dix-huit candidats.
    selected: list[dict[str, Any]] = []
    digests: set[str] = set()
    wave = max(limit + 2, 4)
    with ThreadPoolExecutor(max_workers=6, thread_name_prefix="image-fetch") as pool:
        for offset in range(0, len(ranked), wave):
            for item in pool.map(safe_download, ranked[offset:offset + wave]):
                if not item or item["digest"] in digests:
                    continue
                digests.add(item["digest"])
                item["score"] = float(item.pop("_score", 0.0))
                selected.append(item)
                if len(selected) >= limit:
                    break
            if len(selected) >= limit:
                break
    _RESULT_CACHE.set((query.casefold(), limit), selected, 300.0)
    return selected


@kit.action("image_search")
def image_search(parameters: dict | None = None, player=None) -> str:
    parameters = parameters or {}
    query = " ".join(str(parameters.get("query") or "").split())
    limit = max(1, min(int(parameters.get("limit") or 6), 8))
    if not query:
        return "Précisez ce que vous voulez voir en images."

    images = search_images(query, limit=limit)
    if not images:
        return f"Aucune image exploitable trouvée pour « {query} »."
    if player is not None and hasattr(player, "show_image_gallery"):
        player.show_image_gallery(query, images)

    sources = ", ".join(dict.fromkeys(item.get("source") or "Web" for item in images))
    return (
        f"{len(images)} image{'s' if len(images) > 1 else ''} pertinente"
        f"{'s' if len(images) > 1 else ''} affichée{'s' if len(images) > 1 else ''} "
        f"dans la galerie ANO-GPT pour « {query} ». Sources : {sources}."
    )
