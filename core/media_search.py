"""Moteur de recherche locale tolérant pour la bibliothèque multimédia."""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote_plus


_DISPLAY_JUNK = re.compile(
    r"\b(?:official|officiel(?:le)?|clip|audio|vid[ée]o|lyrics?|paroles?|"
    r"music|hd|4k|\d{3,4}p|\d{2,3}k(?:bps)?|mp3|m4a|web[- ]?dl|"
    r"visuali[sz]er|prod(?:uced)?\s+by)\b",
    re.IGNORECASE,
)
_TRACK_PREFIX = re.compile(r"^\s*(?:track\s*)?\d{1,3}\s*[-_. )]+", re.IGNORECASE)
_TAG_FIELDS = ("title", "artist", "album", "album_artist")
_CACHE_LOCK = threading.Lock()
_CACHE_LOADED = False
_TAG_CACHE: dict[str, dict[str, Any]] = {}


@lru_cache(maxsize=65_536)
def readable_media_name(value: str) -> str:
    """Décode les noms issus du Web et retire seulement le bruit technique."""
    text = str(value or "")
    for _ in range(2):
        decoded = unquote_plus(text)
        if decoded == text:
            break
        text = decoded
    text = html.unescape(text)
    text = _TRACK_PREFIX.sub("", text)
    text = re.sub(r"[_]+", " ", text)
    text = _DISPLAY_JUNK.sub(" ", text)
    text = re.sub(r"[()[\]{}]+", " ", text)
    return re.sub(r"\s+", " ", text).strip(" -_.")


@lru_cache(maxsize=65_536)
def normalize_text(value: str) -> str:
    text = readable_media_name(value)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.casefold()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


@lru_cache(maxsize=65_536)
def _edit_similarity(left: str, right: str) -> float:
    if left == right:
        return 1.0
    if not left or not right:
        return 0.0
    previous = list(range(len(right) + 1))
    for i, lc in enumerate(left, 1):
        current = [i]
        for j, rc in enumerate(right, 1):
            current.append(min(
                current[-1] + 1,
                previous[j] + 1,
                previous[j - 1] + (lc != rc),
            ))
        previous = current
    return max(0.0, 1.0 - previous[-1] / max(len(left), len(right)))


@lru_cache(maxsize=65_536)
def _phonetic(token: str) -> str:
    """Clé phonétique volontairement multilingue et peu agressive."""
    token = normalize_text(token).replace(" ", "")
    if not token:
        return ""
    replacements = (
        ("ph", "f"), ("th", "t"), ("sh", "s"), ("ch", "s"),
        ("qu", "k"), ("ck", "k"), ("ou", "u"), ("oo", "u"),
        ("ee", "i"), ("y", "i"), ("q", "k"), ("c", "k"),
    )
    for source, target in replacements:
        token = token.replace(source, target)
    first = token[0]
    tail = re.sub(r"[aeiou]", "", token[1:])
    return first + re.sub(r"(.)\1+", r"\1", tail)


@lru_cache(maxsize=65_536)
def token_similarity(left: str, right: str) -> float:
    if left == right:
        return 1.0
    if not left or not right:
        return 0.0
    edit = _edit_similarity(left, right)
    sequence = SequenceMatcher(None, left, right).ratio()
    containment = 0.0
    if left in right or right in left:
        containment = 0.92 * min(len(left), len(right)) / max(len(left), len(right))
    phonetic = 0.0
    lp, rp = _phonetic(left), _phonetic(right)
    if min(len(left), len(right)) >= 3 and lp and lp == rp:
        phonetic = 0.92
    return min(1.0, max(edit, sequence * 0.96, containment, phonetic))


def intelligent_score(query: str, candidate: str) -> float:
    """Score 0→1, robuste aux fautes vocales et à un token parasite."""
    q = normalize_text(query)
    c = normalize_text(candidate)
    if not q or not c:
        return 0.0
    if q == c:
        return 1.0
    if q in c:
        return min(0.98, 0.88 + 0.10 * len(q) / len(c))

    query_tokens = [token for token in q.split() if len(token) > 1]
    candidate_tokens = [token for token in c.split() if len(token) > 1]
    if not query_tokens or not candidate_tokens:
        return 0.0

    similarities = sorted((
        max(token_similarity(qt, ct) for ct in candidate_tokens)
        for qt in query_tokens
    ), reverse=True)
    coverage = sum(similarities) / len(similarities)
    sequence = SequenceMatcher(None, q.replace(" ", ""), c.replace(" ", "")).ratio()
    sequence_weight = 1.05 if sequence >= 0.78 else (0.98 if sequence >= 0.72 else 0.78)
    score = max(coverage * 0.96, sequence * sequence_weight)

    # La dictée vocale ajoute parfois un mot inexistant. Si tous les autres
    # tokens sont très convaincants, on tolère exactement un intrus.
    if len(similarities) >= 2 and similarities[0] >= 0.74:
        reliable = similarities[:-1]
        # Ce repli sert à retrouver un candidat, pas à le déclarer certain :
        # son plafond volontairement bas évite qu'un seul mot commun fasse
        # remonter une longue liste de faux positifs.
        tolerant = (sum(reliable) / len(reliable)) * 0.72
        score = max(score, tolerant)
    return min(1.0, score)


def _cache_path() -> Path:
    root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return root / "ano-gpt" / "media-tags-v1.json"


def _load_cache() -> None:
    global _CACHE_LOADED, _TAG_CACHE
    with _CACHE_LOCK:
        if _CACHE_LOADED:
            return
        try:
            payload = json.loads(_cache_path().read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                _TAG_CACHE = payload
        except Exception:
            _TAG_CACHE = {}
        _CACHE_LOADED = True


def _signature(path: Path) -> str:
    try:
        stat = path.stat()
        return f"{stat.st_mtime_ns}:{stat.st_size}"
    except OSError:
        return ""


def _probe_tags(path: Path) -> dict[str, str]:
    if not shutil.which("ffprobe"):
        return {}
    try:
        process = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries",
                "format_tags=title,artist,album,album_artist", "-of", "json",
                "--", str(path),
            ],
            capture_output=True,
            text=True,
            timeout=4,
        )
        payload = json.loads(process.stdout or "{}")
        raw = payload.get("format", {}).get("tags", {})
        lowered = {str(key).casefold(): value for key, value in raw.items()}
        return {
            field: readable_media_name(str(lowered.get(field, "")))
            for field in _TAG_FIELDS
            if lowered.get(field)
        }
    except Exception:
        return {}


def media_tags(path: Path, probe: bool = False) -> dict[str, str]:
    _load_cache()
    key, signature = str(path), _signature(path)
    cached = _TAG_CACHE.get(key, {})
    if signature and cached.get("signature") == signature:
        return dict(cached.get("tags") or {})
    if not probe:
        return {}
    tags = _probe_tags(path)
    with _CACHE_LOCK:
        _TAG_CACHE[key] = {"signature": signature, "tags": tags}
    return tags


def populate_tag_cache(paths: Iterable[Path], max_workers: int = 6) -> None:
    """Indexe les tags manquants en parallèle, puis persiste le cache."""
    unique = list(dict.fromkeys(Path(path) for path in paths))
    _load_cache()
    missing = [
        path for path in unique
        if _TAG_CACHE.get(str(path), {}).get("signature") != _signature(path)
    ]
    if missing:
        with ThreadPoolExecutor(max_workers=max(1, min(max_workers, 8))) as pool:
            list(pool.map(lambda path: media_tags(path, probe=True), missing))
    try:
        destination = _cache_path()
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=destination.parent, delete=False
        ) as handle:
            json.dump(_TAG_CACHE, handle, ensure_ascii=False, separators=(",", ":"))
            temporary = Path(handle.name)
        temporary.replace(destination)
    except Exception:
        pass


def rank_media_paths(
    query: str,
    paths: Iterable[Path],
    kinds: dict[str, str] | None = None,
    limit: int = 8,
    min_score: float = 0.42,
    enrich_if_below: float = 0.68,
) -> list[dict[str, Any]]:
    """Classe les chemins, puis consulte les tags si le nom reste incertain."""
    paths = list(dict.fromkeys(Path(path) for path in paths))

    def rank() -> list[dict[str, Any]]:
        ranked: list[dict[str, Any]] = []
        for path in paths:
            tags = media_tags(path)
            filename = readable_media_name(path.stem)
            fields = [("filename", filename), ("folder", path.parent.name)]
            fields.extend((field, tags.get(field, "")) for field in _TAG_FIELDS)
            scored = [(intelligent_score(query, text), field) for field, text in fields if text]
            score, matched_on = max(scored, default=(0.0, "filename"))
            if score < min_score:
                continue
            title = tags.get("title") or filename
            ranked.append({
                "title": title,
                "artist": tags.get("artist") or tags.get("album_artist") or "",
                "album": tags.get("album") or "",
                "path": str(path),
                "score": round(score, 3),
                "matched_on": matched_on,
                "source": "local",
                "kind": (kinds or {}).get(str(path), "audio"),
            })
        return sorted(ranked, key=lambda item: (-item["score"], item["title"].casefold()))[:limit]

    results = rank()
    if not paths or (results and results[0]["score"] >= enrich_if_below):
        return results
    populate_tag_cache(paths)
    return rank()
