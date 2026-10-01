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


def test_transcribed_reply_without_pcm_is_detected_even_when_short():
    assert _voice_output_is_degenerate(text_chars=12, received=0, queued=0, silence=0, pcm_rate=48000)
    assert not _voice_output_is_degenerate(text_chars=0, received=0, queued=0, silence=0, pcm_rate=48000)


def test_missing_voice_defers_only_the_answer_once_not_the_original_action():
    from core import session_manager as sm

    host = SimpleNamespace(
        _live_models=LiveModelPolicy(primary="bad-live", fallback="working-live"),
        _voice_change_event=asyncio.Event(),
        _defer_turn=Mock(),
        _live_user_text="ouvre kitty et tape okay",
        ui=SimpleNamespace(write_log=Mock()),
        _voice_only_turn=True,
    )
    answer = "La batterie est à 73 %."
    sm.SessionManager._fallback_after_bad_voice(host, answer)
    deferred = host._defer_turn.call_args[0][0]
    assert deferred.startswith(sm._VOICE_REPAIR_PREFIX)
    assert deferred.endswith(answer)
    assert "ouvre kitty" not in deferred
    assert host._voice_change_event.is_set()
    sm.SessionManager._fallback_after_bad_voice(host, answer)
    host._defer_turn.assert_called_once()


def test_voice_repair_cannot_execute_tools():
    from core.tool_dispatcher import ToolDispatcher

    async def run():
        execute = Mock(side_effect=AssertionError("aucune action ne doit être exécutée"))
        host = SimpleNamespace(_voice_only_turn=True, _execute_tool=execute)
        call = SimpleNamespace(id="1", name="open_app", args={"app_name": "kitty"})
        responses = await ToolDispatcher._execute_tool_batch(host, [call])
        assert len(responses) == 1
        assert responses[0].response["ok"] is False
        execute.assert_not_called()
    asyncio.run(run())


def test_receive_text_without_pcm_displays_answer_and_requests_voice_repair(monkeypatch):
    import pytest
    from core import session_manager as sm

    monkeypatch.setattr(sm, "_voice_engine_settings", lambda: {})
    monkeypatch.setattr(sm, "voice_settings_for_mode", lambda settings: {"voice_provider": "gemini"})
    monkeypatch.setattr(sm, "_live_audio_data", lambda response: b"")
    text = "La batterie est à 73 %."

    async def run():
        queue = asyncio.Queue()
        logs = []
        host = SimpleNamespace(
            _interrupted=False, _noise_turn=False, _model_turn_active=False,
            _is_speaking=False, _pending_vision=None, _vision_close_pending=False,
            _dashboard=None, _speech_display_open=False,
            _audio_played_sec=0.0, _turn_done_event=asyncio.Event(),
            speech_text_queue=queue, audio_in_queue=asyncio.Queue(),
            ui=SimpleNamespace(write_log=logs.append),
            thought_streamer=SimpleNamespace(on_speaking_start=Mock(), on_turn_complete=Mock()),
            discard_model_audio=lambda: False,
            _clear_interrupted=Mock(), _maybe_show_clock_particles=Mock(),
            _check_unbacked_claim=Mock(return_value=False),
            _end_discarded_turn=Mock(), _finish_silent_turn=Mock(),
            _remember_turn=Mock(), _fallback_after_bad_voice=Mock(),
        )
        host._queue_spoken_text = queue.put_nowait

        def flush(force=False):
            logs.append(queue.get_nowait())
            host._speech_display_open = True

        host._flush_synced_speech_text = flush

        class Session:
            async def receive(self):
                content = SimpleNamespace(
                    output_transcription=SimpleNamespace(text=text),
                    input_transcription=None, turn_complete=True,
                )
                yield SimpleNamespace(server_content=content, tool_call=None)
                raise asyncio.CancelledError

        host.session = Session()
        with pytest.raises(asyncio.CancelledError):
            await SessionManager._receive_audio(host)
        assert text in logs
        assert "[INLINE_END]" in logs
        assert queue.empty()
        assert host._turn_done_event.is_set()
        host._fallback_after_bad_voice.assert_called_once_with(text)
    asyncio.run(run())
