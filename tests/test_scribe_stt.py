import asyncio
import base64
import json
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock

import aiohttp
import pytest

from core import scribe_stt as stt

PCM = b"\x00\x01" * 32000


def host():
    return SimpleNamespace(
        ui=SimpleNamespace(muted=False, write_log=Mock(), set_user_transcript=Mock()),
        _is_speaking=False, _interrupted=False, _speech_output_epoch=0,
        _audio_drop_count=0, out_queue=asyncio.Queue(), _on_text_command=Mock(),
        session=SimpleNamespace(send_realtime_input=AsyncMock()),
        _model_turn_active=False, _is_thinking=False, _precision_stt_enabled=False,
    )


def test_stream_uses_french_filter_and_sends_only_final_transcript(monkeypatch):
    async def scenario():
        requests, sent = [], []
        class Socket:
            def __init__(self): self.incoming = asyncio.Queue()
            async def receive_json(self): return {"message_type": "session_started"}
            def __aiter__(self): return self
            async def __anext__(self): return await self.incoming.get()
            async def close(self): pass
            async def send_json(self, message):
                sent.append(message)
                kind = "committed_transcript" if message.get("commit") else "partial_transcript"
                await self.incoming.put(SimpleNamespace(
                    type=aiohttp.WSMsgType.TEXT,
                    data=json.dumps({"message_type": kind, "text": "Quelle heure est-il ?"}),
                ))
        class Client:
            def __init__(self, **kwargs): pass
            async def ws_connect(self, url, **kwargs):
                requests.append((url, kwargs))
                return Socket()
            async def close(self): pass
        monkeypatch.setattr(stt.aiohttp, "ClientSession", Client)
        partial = Mock()
        turn = stt.ScribeTurn({"elevenlabs_api_key": "secret"}, partial)
        try:
            await turn.start()
            await turn.feed(PCM)
            assert await turn.finish() == "Quelle heure est-il ?"
            assert requests[0][1]["params"]["language_code"] == "fr"
            assert requests[0][1]["params"]["filter_background_audio"] == "true"
            assert requests[0][1]["params"]["commit_strategy"] == "manual"
            assert requests[0][1]["params"]["no_verbatim"] == "false"
            assert base64.b64decode(sent[0]["audio_base_64"]) == PCM
            assert sent[-1]["commit"] is True
        finally:
            await turn.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("state", ["speaking", "muted", "interrupted", "old_epoch"])
def test_late_or_forbidden_transcripts_never_reach_gemini(state):
    async def scenario():
        h = host()
        if state == "speaking": h._is_speaking = True
        if state == "muted": h.ui.muted = True
        if state == "interrupted": h._interrupted = True
        if state == "old_epoch": h._speech_output_epoch = 1
        assert not await stt.accept_transcript(h, "Ouvre Chrome", PCM, 0)
        h._on_text_command.assert_not_called()
    asyncio.run(scenario())


def test_accepted_text_uses_existing_command_route():
    async def scenario():
        h = host()
        assert await stt.accept_transcript(h, "Ouvre Chrome", PCM, 0)
        h._on_text_command.assert_called_once_with("Ouvre Chrome")
        h.session.send_realtime_input.assert_not_called()
        h.ui.set_user_transcript.assert_called_once_with("Ouvre Chrome", final=True)
    asyncio.run(scenario())


def test_second_stt_rejects_a_conflicting_scribe_transcript():
    async def scenario():
        h = host()
        h._precision_stt_enabled = True
        h._precision_stt_mode = "all"
        h._precision_stt_model = "models/gemini-3.5-transcribe-live"
        h._gemini_api_key = ""

        class Precision:
            async def transcribe(self, _clip, _sample_rate, **_kwargs):
                return SimpleNamespace(available=True, text="Quel est l'e-mail que je viens de recevoir ?")

        h._precision_stt = Precision()
        with pytest.raises(stt.UncertainTranscript):
            await stt.refine_transcript(
                h, "Elle est limitée sur le numéro de téléphone.", PCM,
            )
        assert h._precision_turn_text == ""
    asyncio.run(scenario())


def test_unknown_enrolled_speaker_is_rejected():
    async def scenario():
        h = host()
        h._speaker = SimpleNamespace(enrolled=True, verify=Mock(return_value=SimpleNamespace(known=False)))
        assert not await stt.accept_transcript(h, "Ouvre Chrome", PCM, 0)
        h._on_text_command.assert_not_called()
    asyncio.run(scenario())


@pytest.mark.parametrize("heard,verified,available,accepted", [
    ("Salut !", "salut", True, True),
    ("salut", "allo", True, False),
    ("salut", "oui", True, False),
    ("oui", "", False, False),
])
def test_short_words_are_verified_and_fail_closed(heard, verified, available, accepted):
    async def scenario():
        h = host()
        h._precision_stt_enabled = True
        h._precision_stt_mode = "all"
        h._precision_turn_text = "ancienne commande"
        h._precision_stt = SimpleNamespace(transcribe=AsyncMock(
            return_value=SimpleNamespace(text=verified, available=available)))
        pcm = PCM[:9600]  # 300 ms : les mots courts doivent être vérifiés aussi.
        if accepted:
            assert await stt.refine_transcript(h, heard, pcm) == heard
        else:
            with pytest.raises(stt.UncertainTranscript):
                await stt.refine_transcript(h, heard, pcm)
            assert h._precision_turn_text == ""
        h._precision_stt.transcribe.assert_awaited_once()
    asyncio.run(scenario())


def test_phone_can_use_scribe_when_only_pc_microphone_is_muted():
    async def scenario():
        h = host()
        h.ui.muted = True
        assert await stt.accept_transcript(h, "Ouvre Chrome", PCM, 0, source="phone")
        h._is_speaking = True
        assert not await stt.accept_transcript(h, "Ouvre Chrome", PCM, 0, source="phone")
    asyncio.run(scenario())


def test_runner_never_sends_pcm_to_gemini():
    async def scenario():
        h = host()
        completed = asyncio.Event()
        loop = asyncio.get_running_loop()
        h._on_text_command.side_effect = lambda text: loop.call_soon_threadsafe(completed.set)
        turns = []
        class Turn:
            def __init__(self, settings, partial):
                self.pcm = bytearray()
                self.closed = False
                turns.append(self)
            async def start(self): pass
            async def feed(self, pcm): self.pcm.extend(pcm)
            async def finish(self): return "Ouvre Chrome"
            async def close(self): self.closed = True
        task = asyncio.create_task(stt.run_scribe(h, {}, Turn))
        try:
            for message in [{"activity":"start"}, {"data":PCM}, {"activity":"end"}]:
                await h.out_queue.put(message)
            await asyncio.wait_for(completed.wait(), 2)
            assert turns[0].pcm == PCM and turns[0].closed
            h.session.send_realtime_input.assert_not_called()
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
    asyncio.run(scenario())


def test_runner_discards_audio_if_assistant_starts_during_connection():
    async def scenario():
        h = host()
        closed = asyncio.Event()
        feed = AsyncMock()
        class Turn:
            def __init__(self, settings, partial): pass
            async def start(self):
                h._speech_output_epoch += 1
                h._is_speaking = True
            async def feed(self, pcm): await feed(pcm)
            async def close(self): closed.set()
        task = asyncio.create_task(stt.run_scribe(h, {}, Turn))
        try:
            for message in [{"activity":"start"}, {"data":PCM}, {"activity":"end"}]:
                await h.out_queue.put(message)
            await asyncio.wait_for(closed.wait(), 2)
            feed.assert_not_called()
            h._on_text_command.assert_not_called()
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
    asyncio.run(scenario())


def test_uncertainty_blocks_command_but_allows_immediate_repeat():
    async def scenario():
        h = host()
        h._precision_stt_enabled = True
        h._precision_stt_mode = "all"
        h._precision_stt = SimpleNamespace(transcribe=AsyncMock(side_effect=[
            SimpleNamespace(available=True, text="allo"),
            SimpleNamespace(available=True, text="salut"),
        ]))
        completed = asyncio.Event()
        loop = asyncio.get_running_loop()
        h._on_text_command.side_effect = lambda _: loop.call_soon_threadsafe(completed.set)
        class Turn:
            def __init__(self, settings, partial): self.pcm = bytearray()
            async def start(self): pass
            async def feed(self, pcm): self.pcm.extend(pcm)
            async def finish(self): return "salut"
            async def close(self): pass
        task = asyncio.create_task(stt.run_scribe(h, {}, Turn))
        try:
            for _ in range(2):
                for msg in [{"activity": "start"}, {"data": PCM}, {"activity": "end"}]:
                    await h.out_queue.put(msg)
            await asyncio.wait_for(completed.wait(), 2)
            h._on_text_command.assert_called_once_with("salut")
            assert h._precision_stt.transcribe.await_count == 2
            assert any("non exécutée" in str(c) for c in h.ui.write_log.call_args_list)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
    asyncio.run(scenario())


def test_stt_selection_locks_gemini_and_requests_reconnection(tmp_path, monkeypatch):
    from memory import config_manager as cm
    from core.session_manager import SessionManager
    manager = cm.ConfigManager(tmp_path / "settings.json", use_env=False)
    manager._file.update({"elevenlabs_api_key": "secret", "elevenlabs_voice_id": "VoiceChosen"})
    monkeypatch.setattr(cm, "_default_manager", manager)
    async def scenario():
        h = SimpleNamespace(_loop=asyncio.get_running_loop(),
                            _voice_change_event=asyncio.Event(),
                            _voice_reconnect_requested=False)
        SessionManager._on_stt_provider_change(h, "gemini")
        await asyncio.sleep(0)
        assert h._voice_change_event.is_set() and h._voice_reconnect_requested
        assert manager._file.read() == {
            "elevenlabs_api_key": "secret", "elevenlabs_voice_id": "VoiceChosen",
                "stt_provider": "gemini",
        }
    asyncio.run(scenario())


def test_empty_or_hallucinated_text_does_not_trigger_a_command():
    async def scenario():
        h = host()
        for text in ("", "hum", "Merci d'avoir regardé cette vidéo"):
            assert not await stt.accept_transcript(h, text, PCM, 0)
        h._on_text_command.assert_not_called()
    asyncio.run(scenario())


def test_transport_error_redacts_server_details(monkeypatch):
    async def scenario():
        class Client:
            def __init__(self, **kwargs): pass
            async def ws_connect(self, *args, **kwargs):
                raise OSError("secret-key-in-untrusted-error")
            async def close(self): pass
        monkeypatch.setattr(stt.aiohttp, "ClientSession", Client)
        turn = stt.ScribeTurn({"elevenlabs_api_key": "secret"})
        with pytest.raises(stt.ScribeError) as error:
            await turn.start()
        assert "secret" not in str(error.value)
        assert turn.client is None
    asyncio.run(scenario())


def test_confirmed_addressed_stop_can_interrupt_thinking_without_new_response():
    async def scenario():
        h = host()
        h._is_thinking = True
        h.interrupt = Mock()
        assert await stt.accept_transcript(h, "ANO stop", PCM, 0)
        h.interrupt.assert_called_once()
        h._on_text_command.assert_not_called()
    asyncio.run(scenario())


def test_seuls_arrete_toi_et_ecoute_coupent_sans_adresse():
    async def scenario():
        h = host()
        h._is_thinking = True
        h.interrupt = Mock()
        assert await stt.accept_transcript(h, "stop", PCM, 0)
        assert await stt.accept_transcript(h, "arrête-toi", PCM, 0)
        assert await stt.accept_transcript(h, "écoute", PCM, 0)
        # « stop » nu est ignoré, contrairement aux deux formules exactes
        # explicitement choisies par l'utilisateur.
        assert h.interrupt.call_count == 2
    asyncio.run(scenario())


def test_spanish_garbage_does_not_trigger_a_command():
    async def scenario():
        h = host()
        assert not await stt.accept_transcript(h, "Yo no hablo español", PCM, 0)
        assert not await stt.accept_transcript(h, "1 2 3", PCM, 0)
        assert not await stt.accept_transcript(h, "Start", PCM, 0)
        h._on_text_command.assert_not_called()
    asyncio.run(scenario())


def test_final_transcript_keeps_common_words_and_dictated_spelling():
    async def scenario():
        h = host()
        text = "explique la discorde et le mot dockeur puis point py"
        assert await stt.accept_transcript(h, text, PCM * 4, 0,
                                           acoustic_voice_ms=6000)
        h._on_text_command.assert_called_once_with(text)
    asyncio.run(scenario())
