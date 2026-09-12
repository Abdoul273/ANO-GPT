"""Préparation bornée des vidéos locales pour la galerie et le lecteur Qt."""

from __future__ import annotations

import json
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterable


VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv",
    ".m4v", ".ts", ".mpg", ".mpeg",
}


def _clock(seconds: float) -> str:
    total = max(0, int(seconds or 0))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def _probe(path: Path) -> dict[str, Any]:
    if not shutil.which("ffprobe"):
        return {}
    try:
        process = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height:format=duration",
                "-of", "json", "--", str(path),
            ],
            capture_output=True, text=True, timeout=6,
        )
        payload = json.loads(process.stdout or "{}")
        stream = (payload.get("streams") or [{}])[0]
        duration = float((payload.get("format") or {}).get("duration") or 0)
        return {
            "duration_seconds": duration,
            "duration": _clock(duration) if duration else "",
            "width": int(stream.get("width") or 0),
            "height": int(stream.get("height") or 0),
        }
    except Exception:
        return {}


def _thumbnail(path: Path) -> bytes:
    if not shutil.which("ffmpeg"):
        return b""
    try:
        process = subprocess.run(
            [
                "ffmpeg", "-v", "error", "-ss", "1", "-i", str(path),
                "-frames:v", "1", "-vf", "scale=640:-2:force_original_aspect_ratio=decrease",
                "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
            ],
            capture_output=True, timeout=8,
        )
        payload = process.stdout or b""
        return payload if process.returncode == 0 and len(payload) <= 2_500_000 else b""
    except Exception:
        return b""


def prepare_local_video(item: dict[str, Any] | str | Path) -> dict[str, Any] | None:
    value = dict(item) if isinstance(item, dict) else {"path": str(item)}
    try:
        path = Path(str(value.get("path") or "")).expanduser().resolve()
        if not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
            return None
    except OSError:
        return None
    if not value.get("duration") or not value.get("width"):
        value.update(_probe(path))
    value.update({
        "path": str(path),
        "title": str(value.get("title") or path.stem.replace("_", " ").strip()),
        "folder": str(value.get("folder") or path.parent.name),
        "source": "local",
        "kind": "video",
        "size": path.stat().st_size,
    })
    if not value.get("thumbnail_bytes"):
        thumbnail = _thumbnail(path)
        if thumbnail:
            value["thumbnail_bytes"] = thumbnail
    return value


def prepare_local_videos(items: Iterable[dict[str, Any] | str | Path],
                         limit: int = 12) -> list[dict[str, Any]]:
    candidates = list(items)[:max(1, min(int(limit), 12))]
    if not candidates:
        return []
    with ThreadPoolExecutor(max_workers=min(4, len(candidates))) as pool:
        prepared = list(pool.map(prepare_local_video, candidates))
    return [item for item in prepared if item is not None]
