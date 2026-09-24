"""Fin des réponses Live, avec et sans audio."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

from core.audio_engine import PcmSilenceCompactor
from core.live_model_policy import LiveModelPolicy
from core.session_manager import SessionManager, _voice_output_is_degenerate


def test_silence_compactor_keeps_later_sentence_and_short_pauses():
    frame = 480  # 20 ms à 24 kHz
    voice = np.full(frame, 1200, dtype=np.int16).tobytes()
    silence = np.zeros(frame, dtype=np.int16).tobytes()
    compactor = PcmSilenceCompactor()

    first = list(compactor.compact(voice + silence * 20))
    second = list(compactor.compact(silence * 20 + voice))

    assert first[0] == voice
    assert first.count(silence) == 12
    assert second == [voice]  # la phrase après la pause n'est pas coupée
    assert list(compactor.compact(silence * 2)) == [silence, silence]


def test_silent_completed_turn_returns_to_listening():
    host = SimpleNamespace(
        _is_speaking=False,
        audio_in_queue=asyncio.Queue(),
        ui=SimpleNamespace(muted=False, set_state=Mock()),
    )
    SessionManager._finish_silent_turn(host)
    host.ui.set_state.assert_called_once_with("LISTENING")

    host.ui.set_state.reset_mock()
    host._is_speaking = True
    SessionManager._finish_silent_turn(host)
    host.ui.set_state.assert_not_called()

    host._is_speaking = False
    host.audio_in_queue.put_nowait(b"voix")
    SessionManager._finish_silent_turn(host)
    host.ui.set_state.assert_not_called()


def test_mostly_silent_voice_switches_model_once():
    rate = 48_000
    assert _voice_output_is_degenerate(
        text_chars=64, received=round(13.39 * rate),
        queued=round(1.21 * rate), silence=round(12.18 * rate), pcm_rate=rate,
    )
    assert not _voice_output_is_degenerate(
        text_chars=64, received=round(3.4 * rate),
        queued=round(2.6 * rate), silence=round(0.8 * rate), pcm_rate=rate,
    )

    event = asyncio.Event()
    host = SimpleNamespace(
        _live_models=LiveModelPolicy(primary="bad-live", fallback="working-live"),
        _voice_change_event=event,
        _voice_reconnect_requested=False,
        _voice_degraded_reconnect=False,
        ui=SimpleNamespace(write_log=Mock()),
    )
    SessionManager._fallback_after_bad_voice(host)
    assert host._live_models.current == "working-live"
    assert host._voice_reconnect_requested
    assert event.is_set()
    host.ui.write_log.assert_called_once()

    SessionManager._fallback_after_bad_voice(host)
    host.ui.write_log.assert_called_once()
