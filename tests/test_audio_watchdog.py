import asyncio
import threading
import time
from unittest.mock import Mock
from types import SimpleNamespace

import pytest

from core.audio_engine import AudioEngine, hold_live_audio
from core.tool_dispatcher import _has_explicit_capture_intent


class DummyHost:
    def __init__(self):
        self._speaking_lock = threading.Lock()
        self._is_speaking = False
        self._model_turn_active = True
        self._is_thinking = True
        self._audio_turn_active = True
        self._audio_turn_pending = True
        self._interrupted = True
        self._noise_turn = True
        self._activity_open = True
        self._phone_active = False
        self._speech_display_open = False
        self._last_model_turn_data_at = 0.0
        self._last_watchdog_trigger = 0.0
        self._turn_submit_lock = threading.Lock()
        self._turn_submit_lock.acquire()
        self.audio_in_queue = asyncio.Queue()
        self.out_queue = asyncio.Queue()
        self._audio_abort_event = threading.Event()
        self._turn_done_event = threading.Event()
        self.ui = SimpleNamespace(
            muted=False,
            set_state=Mock(),
            write_log=Mock(),
            set_volume=Mock(),
        )
        self.loop = None


def test_hold_live_audio_logic():
    # When all are false, live audio is NOT held
    assert not hold_live_audio(
        speaking=False,
        model_turn_active=False,
        thinking=False,
        interrupted=False,
        noise_turn=False,
        text_turn_pending=False,
    )

    # Any true condition holds live audio
    assert hold_live_audio(
        speaking=True,
        model_turn_active=False,
        thinking=False,
        interrupted=False,
        noise_turn=False,
        text_turn_pending=False,
    )
    assert hold_live_audio(
        speaking=False,
        model_turn_active=True,
        thinking=False,
        interrupted=False,
        noise_turn=False,
        text_turn_pending=False,
    )
    assert hold_live_audio(
        speaking=False,
        model_turn_active=False,
        thinking=True,
        interrupted=False,
        noise_turn=False,
        text_turn_pending=False,
    )
    # Interrupted allows audio if not speaking and not thinking/model turn
    assert not hold_live_audio(
        speaking=False,
        model_turn_active=False,
        thinking=False,
        interrupted=True,
        noise_turn=False,
        text_turn_pending=False,
    )
    # But if thinking/model turn without interrupted, it holds
    assert hold_live_audio(
        speaking=False,
        model_turn_active=True,
        thinking=False,
        interrupted=False,
        noise_turn=False,
        text_turn_pending=False,
    )
    assert hold_live_audio(
        speaking=False,
        model_turn_active=False,
        thinking=False,
        interrupted=False,
        noise_turn=True,
        text_turn_pending=False,
    )
    assert hold_live_audio(
        speaking=False,
        model_turn_active=False,
        thinking=False,
        interrupted=False,
        noise_turn=False,
        text_turn_pending=True,
    )


def test_screen_recording_requires_an_explicitly_recognized_request():
    assert _has_explicit_capture_intent("Enregistre mon écran pendant une minute")
    assert _has_explicit_capture_intent("Fais une capture d'écran")
    assert not _has_explicit_capture_intent("Salut, comment ça va ?")
    assert not _has_explicit_capture_intent("")


def test_watchdog_triggers_when_held_and_stalled():
    host = DummyHost()
    host.audio_in_queue.put_nowait(b"chunk1")
    host.out_queue.put_nowait({"activity": "start"})

    # Attach AudioEngine methods to dummy host
    host.reset_audio_and_turn_state = AudioEngine.reset_audio_and_turn_state.__get__(host)
    host._do_reset_state_async = AudioEngine._do_reset_state_async.__get__(host)
    host.check_audio_watchdog = AudioEngine.check_audio_watchdog.__get__(host)
    host.set_speaking = Mock()
    host._reset_speech_sync = Mock()

    now = 100.0
    host._last_model_turn_data_at = 90.0  # 10s ago (> 5s threshold)
    host._last_watchdog_trigger = 0.0

    # Call watchdog
    triggered = host.check_audio_watchdog(now=now)
    assert triggered is True

    # State must be reset
    assert not host._model_turn_active
    assert not host._is_thinking
    assert not host._audio_turn_active
    assert not host._audio_turn_pending
    assert not host._interrupted
    assert not host._noise_turn
    assert not host._activity_open
    assert not host._turn_submit_lock.locked()
    assert host.audio_in_queue.empty()
    assert host.out_queue.empty()
    assert host.ui.set_state.called
    assert host.ui.set_state.call_args[0][0] == "LISTENING"


def test_watchdog_does_not_trigger_when_recently_active():
    host = DummyHost()
    host.reset_audio_and_turn_state = AudioEngine.reset_audio_and_turn_state.__get__(host)
    host._do_reset_state_async = AudioEngine._do_reset_state_async.__get__(host)
    host.check_audio_watchdog = AudioEngine.check_audio_watchdog.__get__(host)

    now = 100.0
    host._last_model_turn_data_at = 98.0  # only 2s ago (< 5.0s threshold)

    triggered = host.check_audio_watchdog(now=now)
    assert triggered is False

    # State must remain untouched
    assert host._model_turn_active
    assert host._is_thinking
    assert host._turn_submit_lock.locked()


def test_watchdog_rate_limiting():
    host = DummyHost()
    called = []
    host.reset_audio_and_turn_state = lambda source="unknown": called.append(source)
    host.check_audio_watchdog = AudioEngine.check_audio_watchdog.__get__(host)

    now = 100.0
    host._last_model_turn_data_at = 90.0
    host._last_watchdog_trigger = 99.0  # only 1s ago (< 2.0s debounce)

    triggered = host.check_audio_watchdog(now=now)
    assert triggered is False
    assert len(called) == 0

    # Once 2s passed
    now = 101.5
    triggered = host.check_audio_watchdog(now=now)
    assert triggered is True
    assert len(called) == 1
    assert called[0] == "watchdog_inactivity"


def test_watchdog_releases_speech_flag_stuck_without_audio():
    """Session tombée en pleine réponse : `_is_speaking` reste levé, plus
    aucun son n'arrive. Le micro doit être rendu au lieu de rester retenu."""
    host = DummyHost()
    host._is_speaking = True
    called = []
    host.reset_audio_and_turn_state = lambda source="unknown": called.append(source)
    host.check_audio_watchdog = AudioEngine.check_audio_watchdog.__get__(host)

    host._last_model_turn_data_at = 90.0
    assert host.check_audio_watchdog(now=93.0) is False   # 3 s : encore normal
    assert called == []
    assert host.check_audio_watchdog(now=97.0) is True    # > 6 s sans son
    assert called == ["watchdog_stalled_speech"]


def test_watchdog_keeps_speech_flag_while_audio_is_queued():
    host = DummyHost()
    host._is_speaking = True
    host.audio_in_queue.put_nowait(b"\x00\x00")
    called = []
    host.reset_audio_and_turn_state = lambda source="unknown": called.append(source)
    host.check_audio_watchdog = AudioEngine.check_audio_watchdog.__get__(host)

    host._last_model_turn_data_at = 90.0
    assert host.check_audio_watchdog(now=100.0) is False
    assert called == []
