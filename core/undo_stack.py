"""Pile d'annulation commune aux actions effectuées par ANO-GPT.

Les entrées vivent uniquement pendant le processus courant. Une action doit
capturer son état *avant* la modification puis enregistrer une fonction sans
argument capable de le restaurer. La pile est volontairement bornée afin de
ne pas conserver indéfiniment du contenu de fichier en mémoire.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable


MAX_DEPTH = 12


@dataclass
class UndoEntry:
    label: str
    callback: Callable[[], str | None]
    created_at: float = field(default_factory=time.monotonic)


_entries: list[UndoEntry] = []
_lock = threading.RLock()


def push(label: str, callback: Callable[[], str | None]) -> bool:
    """Enregistre une opération réversible sans jamais faire échouer l'action."""
    if not callable(callback):
        return False
    try:
        entry = UndoEntry(" ".join(str(label).split())[:160], callback)
        with _lock:
            _entries.append(entry)
            del _entries[:-MAX_DEPTH]
        return True
    except Exception:
        return False


def history() -> list[str]:
    with _lock:
        return [entry.label for entry in reversed(_entries)]


def peek() -> str:
    with _lock:
        return _entries[-1].label if _entries else ""


def undo_last() -> str:
    """Retire puis exécute la dernière annulation disponible."""
    with _lock:
        entry = _entries.pop() if _entries else None
    if entry is None:
        return "Il n'y a aucune action d'ANO-GPT à annuler."
    try:
        detail = entry.callback() or ""
    except Exception as exc:
        return f"Impossible d'annuler « {entry.label} » : {exc}"
    suffix = f" {detail}" if detail else ""
    return f"Action annulée : {entry.label}.{suffix}"


def clear() -> None:
    with _lock:
        _entries.clear()
