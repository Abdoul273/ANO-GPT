from __future__ import annotations

import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PyQt6.QtWidgets import QApplication


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = _base_dir()
CONFIG_DIR = BASE_DIR / "config"
API_FILE = CONFIG_DIR / "api_keys.json"

_DEFAULT_W, _DEFAULT_H = 1200, 800
_MIN_W, _MIN_H = 820, 580
_LEFT_W = 184
_RIGHT_W = 352

_cache: dict = {}
_loaded = False
_lock = threading.Lock()
_write_executor: ThreadPoolExecutor | None = None


def _load_from_disk() -> dict:
    try:
        return json.loads(API_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _ensure_loaded() -> None:
    global _cache, _loaded
    with _lock:
        if _loaded:
            return
        _cache = _load_from_disk()
        _loaded = True


threading.Thread(target=_ensure_loaded, daemon=True, name="ui-config-load").start()


def _read_full_config() -> dict:
    """Snapshot mémoire. Ne lit jamais le disque depuis le thread GUI Qt."""
    if QApplication.instance() is None:
        _ensure_loaded()
    with _lock:
        return dict(_cache)


def _patch_cached_config(patch: dict) -> None:
    """Met à jour le cache UI après une écriture atomique externe.

    Le panneau IA persiste via ``core.llm_client``. Sans cette synchronisation,
    Qt pouvait encore afficher l'ancienne sélection et faire croire que le
    bouton « Appliquer » était sans effet.
    """
    global _loaded
    with _lock:
        _cache.update(patch)
        _loaded = True


def _write_full_config(data: dict) -> None:
    """Écrit api_keys.json hors du thread GUI si Qt tourne déjà."""
    global _cache, _loaded, _write_executor
    payload = dict(data)
    with _lock:
        _cache = payload
        _loaded = True

    def _dump() -> None:
        try:
            API_FILE.parent.mkdir(parents=True, exist_ok=True)
            temporary = API_FILE.with_name(API_FILE.name + ".tmp")
            temporary.write_text(json.dumps(payload, indent=4), encoding="utf-8")
            os.replace(temporary, API_FILE)
        except Exception:
            pass

    if QApplication.instance() is None:
        _dump()
        return

    with _lock:
        if _write_executor is None:
            _write_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ui-config-write")
        _write_executor.submit(_dump)
