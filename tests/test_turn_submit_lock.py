"""Le verrou de soumission survit aux reconnexions et aux annulations.

Reproduit le « RuntimeError: Lock is not acquired » vu sur la livraison
vidéo : une tâche de fond attendait le verrou pendant qu'une reconnexion le
remplaçait et qu'un reset le relâchait de l'extérieur.
"""

from __future__ import annotations

import asyncio

import pytest

from core.audio_engine import AudioEngine
from core.background_task import log_task_result, spawn_logged
from core.session_manager import SessionManager


class _Session:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_realtime_input(self, text: str) -> None:
        self.sent.append(text)


class _UI:
    def __init__(self) -> None:
        self.logs: list[str] = []

    def write_log(self, line: str) -> None:
        self.logs.append(line)


class _Connection:
    def should_apologize(self) -> bool:
        return False


class _Host:
    _submit_text_turn = SessionManager._submit_text_turn
    _defer_turn = SessionManager._defer_turn
    _flush_deferred_turns = SessionManager._flush_deferred_turns
    _resend_unanswered = SessionManager._resend_unanswered

    def __init__(self) -> None:
        self.ui = _UI()
        self.session = _Session()
        self._turn_submit_lock = asyncio.Lock()
        self._turn_done_event = asyncio.Event()
        self._audio_turn_pending = False
        self._active_turn_task = None
        self._deferred_turns: list[str] = []
        self._unanswered: list[str] = []
        self._conn = _Connection()
        self._toolkit_context_on_reconnect = False
        self._recent_live_turns: list[tuple[str, str]] = []

    def _maybe_show_clock_particles(self, text: str) -> None:
        pass

    def _activity_cancel(self) -> None:
        pass

    def reset_audio_and_turn_state(self, reason: str = "") -> None:
        pass

    def set_speaking(self, value: bool) -> None:
        pass


async def _finish_turn(host: _Host, delay: float = 0.01) -> None:
    await asyncio.sleep(delay)
    host._turn_done_event.set()


async def _test_lock_survives_reconnection_and_delivers_on_new_session():
    host = _Host()
    old_session = host.session
    first = asyncio.create_task(host._submit_text_turn("premier", timeout_s=1.0))
    await asyncio.sleep(0)
    second = asyncio.create_task(host._submit_text_turn("résultat vidéo", timeout_s=1.0))
    await asyncio.sleep(0.01)
    # Reconnexion pendant que la seconde attend le verrou : même verrou,
    # nouvelle session et nouvel événement de fin de tour.
    host.session = _Session()
    host._turn_done_event = asyncio.Event()
    # Fin du premier tour sur l'ancien événement capturé dans le tour.
    await asyncio.sleep(0.01)
    # L'ancien tour tenait `done` de l'ancienne session : on le libère en
    # le cancelant, comme le ferait un reset.
    first.cancel()
    assert await first is False
    asyncio.create_task(_finish_turn(host))
    assert await second is True
    assert host.session.sent == ["résultat vidéo"]
    assert old_session.sent == ["premier"]
    assert not host._turn_submit_lock.locked()


async def _test_external_reset_never_releases_the_lock():
    host = _Host()
    host._turn_done_event = asyncio.Event()
    host._do_reset_state_async = lambda source: AudioEngine._do_reset_state_async(host, source)
    turn = asyncio.create_task(host._submit_text_turn("bonjour", timeout_s=1.0))
    await asyncio.sleep(0.01)
    assert host._turn_submit_lock.locked()
    # Le reset annule le tour en vol ; c'est lui qui relâche le verrou.
    AudioEngine.reset_audio_and_turn_state(host, "test")
    assert await turn is False
    assert not host._turn_submit_lock.locked()


async def _test_result_without_session_is_deferred_then_flushed():
    host = _Host()
    host.session = None
    assert await host._submit_text_turn("résultat perdu ?") is False
    assert host._deferred_turns == ["résultat perdu ?"]
    host.session = _Session()
    asyncio.create_task(_finish_turn(host, 0.5))
    await host._resend_unanswered()
    assert host.session.sent == ["résultat perdu ?"]
    assert host._deferred_turns == []


async def _test_toolkit_reconnect_replays_local_context_without_live_handle():
    host = _Host()
    host._toolkit_context_on_reconnect = True
    host._recent_live_turns = [("où est le morceau ?", "Il est dans Musique.")]
    host._unanswered = ["lance-le"]
    asyncio.create_task(_finish_turn(host, 0.5))
    await host._resend_unanswered()
    assert host.session.sent == [
        "[Contexte local juste avant la reconnexion. Continue cette "
        "conversation naturellement, sans le répéter.]\n"
        "Utilisateur : où est le morceau ?\nANO-GPT : Il est dans Musique.\n\n"
        "lance-le"
    ]
    assert host._toolkit_context_on_reconnect is False


async def _test_spawn_logged_reports_exception():
    ui = _UI()

    async def boom() -> None:
        raise ValueError("explosion")

    task = spawn_logged(boom(), name="boom", ui=ui)
    with pytest.raises(ValueError):
        await task
    await asyncio.sleep(0)
    assert any("boom" in line and "explosion" in line for line in ui.logs)


def test_log_task_result_ignores_cancelled():
    async def run() -> None:
        task = asyncio.create_task(asyncio.sleep(10))
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        log_task_result(task)

    asyncio.run(run())


def test_lock_survives_reconnection_and_delivers_on_new_session():
    asyncio.run(_test_lock_survives_reconnection_and_delivers_on_new_session())


def test_external_reset_never_releases_the_lock():
    asyncio.run(_test_external_reset_never_releases_the_lock())


def test_result_without_session_is_deferred_then_flushed():
    asyncio.run(_test_result_without_session_is_deferred_then_flushed())


def test_toolkit_reconnect_replays_local_context_without_live_handle():
    asyncio.run(_test_toolkit_reconnect_replays_local_context_without_live_handle())


def test_spawn_logged_reports_exception():
    asyncio.run(_test_spawn_logged_reports_exception())
