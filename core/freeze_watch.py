"""core/freeze_watch.py — Détecteur de gel du thread Qt et de la boucle audio.

Quand « plus rien ne marche » sans la moindre ligne de journal, c'est que le
thread qui écrit le journal (Qt) ou celui qui parle et écoute (asyncio) est
lui-même figé. Aucun garde-fou logique ne peut alors s'exprimer. Ce module
tient deux battements de cœur — un timer Qt sur le thread principal, une
tâche sur la boucle asyncio — et un thread indépendant qui les surveille.
Dès qu'un battement manque plus de ``STALL_S`` secondes, la pile de TOUS les
threads est écrite dans ``logs/freeze-<heure>.txt`` et dans le journal
structuré : on sait enfin sur quelle ligne ça bloque.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
import traceback
from pathlib import Path

STALL_S = 3.0
REPORT_COOLDOWN_S = 30.0
_LOG = logging.getLogger("anogpt.freeze")

_beats: dict[str, float] = {}
_last_report = 0.0
_thread: threading.Thread | None = None


def beat(name: str) -> None:
    _beats[name] = time.monotonic()


def lag(name: str) -> float:
    """Retard du battement ``name`` en secondes (0 s'il n'a jamais battu).

    Lu depuis le thread Qt pour ralentir l'orbe dès que la boucle audio
    prend du retard — sans attendre le rapport de gel à 3 s.
    """
    last = _beats.get(name)
    return 0.0 if last is None else max(0.0, time.monotonic() - last)


def dump_all_threads(reason: str) -> Path | None:
    """Écrit la pile de chaque thread ; retourne le fichier produit."""
    global _last_report
    now = time.monotonic()
    if now - _last_report < REPORT_COOLDOWN_S:
        return None
    _last_report = now
    names = {t.ident: t.name for t in threading.enumerate()}
    lines = [f"# Gel détecté : {reason}", f"# {time.strftime('%Y-%m-%d %H:%M:%S')}", ""]
    for ident, frame in sys._current_frames().items():
        lines.append(f"## Thread {names.get(ident, '?')} ({ident})")
        lines.extend(traceback.format_stack(frame))
        lines.append("")
    text = "\n".join(lines)
    try:
        log_dir = Path(__file__).resolve().parent.parent / "logs"
        log_dir.mkdir(exist_ok=True)
        path = log_dir / f"freeze-{time.strftime('%Y%m%d-%H%M%S')}.txt"
        path.write_text(text, encoding="utf-8")
    except Exception:
        path = None
    _LOG.error("gel : %s — piles dans %s", reason, path, extra={"reason": reason})
    print(f"[FreezeWatch] ⚠️ {reason} — piles écrites dans {path}", file=sys.stderr)
    return path


def _watch() -> None:
    while True:
        time.sleep(0.5)
        now = time.monotonic()
        for name, last in list(_beats.items()):
            stalled = now - last
            if stalled >= STALL_S:
                dump_all_threads(f"{name} ne répond plus depuis {stalled:.1f} s")
                _beats[name] = now   # nouveau départ, sinon un rapport par tour


def start() -> None:
    """Démarre le thread de surveillance (idempotent)."""
    global _thread
    if _thread is not None or os.environ.get("ANOGPT_NO_FREEZE_WATCH"):
        return
    _thread = threading.Thread(target=_watch, daemon=True, name="freeze-watch")
    _thread.start()


def install_qt_heartbeat(interval_ms: int = 250) -> None:
    """À appeler sur le thread Qt, après QApplication : timer de battement."""
    try:
        from PyQt6.QtCore import QTimer
    except Exception:
        return
    timer = QTimer()
    timer.setInterval(interval_ms)
    timer.timeout.connect(lambda: beat("thread Qt"))
    timer.start()
    beat("thread Qt")
    # Le timer doit survivre à cette fonction.
    install_qt_heartbeat._timer = timer  # type: ignore[attr-defined]
    start()


async def asyncio_heartbeat(interval_s: float = 0.25) -> None:
    """Tâche à ajouter au TaskGroup de la session : battement de la boucle."""
    import asyncio
    beat("boucle audio")
    start()
    while True:
        await asyncio.sleep(interval_s)
        beat("boucle audio")
