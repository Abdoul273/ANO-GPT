"""Tâches de fond asyncio dont l'échec ne doit jamais se perdre.

Une tâche créée avec ``asyncio.create_task`` et jamais attendue rapporte son
exception sous la forme d'un « Task exception was never retrieved » à la
destruction de l'objet — parfois plusieurs minutes après, sans contexte. Ici,
le rapport est immédiat, nommé, et remonte dans le journal de l'interface.
"""

from __future__ import annotations

import asyncio
import traceback
from typing import Any, Coroutine

# La boucle asyncio ne garde qu'une référence faible sur ses tâches : une tâche
# que personne ne retient peut être ramassée en plein vol. Toute tâche lancée
# ici reste donc retenue jusqu'à sa fin.
_LIVE_TASKS: set[asyncio.Task] = set()


def log_task_result(task: asyncio.Task, ui: Any = None) -> None:
    """Callback ``add_done_callback`` : trace toute exception non récupérée."""
    if task.cancelled():
        return
    try:
        exc = task.exception()
    except Exception:
        return
    if exc is None:
        return
    name = task.get_name()
    print(f"[Tâche] ✗ {name} : {type(exc).__name__}: {exc}")
    traceback.print_exception(type(exc), exc, exc.__traceback__)
    if ui is not None and hasattr(ui, "write_log"):
        try:
            ui.write_log(f"ERR: tâche {name} — {type(exc).__name__}: {str(exc)[:160]}")
        except Exception:
            pass


def spawn_logged(
    coro: Coroutine[Any, Any, Any],
    *,
    name: str,
    ui: Any = None,
    registry: set | None = None,
) -> asyncio.Task:
    """Crée une tâche de fond dont l'exception est journalisée aussitôt.

    ``registry`` : ensemble facultatif où la tâche est inscrite le temps de sa
    vie (limite « une à la fois » des générations longues).
    """
    task = asyncio.create_task(coro, name=name)
    _LIVE_TASKS.add(task)
    task.add_done_callback(_LIVE_TASKS.discard)
    if registry is not None:
        registry.add(task)
        task.add_done_callback(registry.discard)
    task.add_done_callback(lambda t: log_task_result(t, ui))
    return task
