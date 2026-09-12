import asyncio
import time
from types import SimpleNamespace

import numpy as np
import pytest



class _UI:
    def __init__(self):
        self.logs = []
        self.transcripts = []

    def write_log(self, text):
        self.logs.append(text)

    def set_user_transcript(self, text, final=False):
        self.transcripts.append((text, final))


def test_live_transcribe_is_verbatim_not_smart():
    import core.gemini_transcribe_stt as stt
    assert stt.TRANSCRIBE_MODE == "VERBATIM"


def test_stale_local_pcm_is_rejected_before_it_reaches_gemini():
    import core.gemini_transcribe_stt as stt

    now = 100.0
    assert not stt._is_stale_audio_message({"data": b"pcm"}, now=now)
    assert not stt._is_stale_audio_message(
        {"_captured_at": now - stt._MAX_QUEUED_AUDIO_AGE_S}, now=now
    )
    assert stt._is_stale_audio_message(
        {"_captured_at": now - stt._MAX_QUEUED_AUDIO_AGE_S - 0.01}, now=now
    )


def test_transcript_helper_propagates_turn_id_when_ui_supports_it():
    import core.gemini_transcribe_stt as stt

    class UI:
        def __init__(self):
            self.value = None

        def set_user_transcript(self, text, final=False, turn_id=""):
            self.value = (text, final, turn_id)

    ui = UI()
    stt._set_transcript(ui, "bonjour", turn_id="turn-42")
    assert ui.value == ("bonjour", False, "turn-42")


def test_azure_keeps_the_good_realtime_preview_when_gemini_final_drifts():
    import core.gemini_transcribe_stt as stt

    assert stt.select_consensus_text(
        "dis moi si je peux t aider",
        "quelle heure est il",
        "Quelle heure est-il ?",
    ) == "quelle heure est il"


def test_consensus_rejects_three_unrelated_transcripts():
    import core.gemini_transcribe_stt as stt

    assert stt.select_consensus_text(
        "ouvre Chrome", "quelle heure est-il", "météo de demain"
    ) == ""


def test_all_confirmed_segments_are_preserved():
    import core.gemini_transcribe_stt as stt

    class Session:
        async def receive(self):
            yield SimpleNamespace(server_content=SimpleNamespace(
                interim_input_transcription=SimpleNamespace(text="Rappelle"),
                input_transcription=SimpleNamespace(text="Rappelle-moi"),
                turn_complete=False,
            ))
            yield SimpleNamespace(server_content=SimpleNamespace(
                interim_input_transcription=None,
                input_transcription=SimpleNamespace(
                    text="une facture aujourd'hui"
                ),
                turn_complete=True,
            ))

    async def scenario():
        turn = stt.GeminiTranscribeTurn(SimpleNamespace())
        turn.session = Session()
        turn.final = asyncio.get_running_loop().create_future()
        turn._accepting = True
        turn._ending = True
        turn.reader = asyncio.create_task(turn._read())
        await asyncio.wait_for(asyncio.shield(turn.final), 1)
        await turn.close()
        assert turn.final.result() == "Rappelle-moi une facture aujourd'hui"

    asyncio.run(scenario())


def test_feed_preserves_microphone_pcm_without_a_second_filter():
    import core.gemini_transcribe_stt as stt

    class Session:
        def __init__(self):
            self.blobs = []

        async def send_realtime_input(self, **kwargs):
            self.blobs.append(kwargs)

    class Denoiser:
        def process(self, data):
            n = len(data) // 2
            return (np.zeros(n, dtype=np.int16)).tobytes()

        def reset(self):
            return None

    async def scenario():
        host = SimpleNamespace(_stt_denoiser=Denoiser())
        turn = stt.GeminiTranscribeTurn(host)
        turn.session = Session()
        raw = np.ones(320, dtype=np.int16).tobytes()
        await turn.feed(raw)
        assert turn.pcm == raw
        assert turn.session.blobs[0]["audio"].data == raw
        assert "audio" in turn.session.blobs[0]

    asyncio.run(scenario())


def test_long_turn_executes_without_second_live_pass(monkeypatch):
    """Le flux live EST déjà VERBATIM : pas de 2ᵉ handshake Gemini."""
    import core.gemini_transcribe_stt as stt

    accepted = []
    refine_calls = []

    class Turn:
        def __init__(self, host, on_partial):
            self.pcm = bytearray()

        async def start(self):
            return None

        async def begin(self, _on_partial):
            return None

        async def feed(self, data):
            self.pcm.extend(data)

        async def finish(self):
            return "envoie un rappel à un client pour son site web"

        async def close(self):
            return None

    async def accept(host, text, pcm, epoch, prosody, source, acoustic_voice_ms):
        accepted.append(text)
        return True

    async def refine(*_args, **_kwargs):
        refine_calls.append(True)
        raise AssertionError("refine_transcript ne doit plus être appelé")

    monkeypatch.setattr(stt, "accept_transcript", accept)
    monkeypatch.setattr(stt, "input_allowed", lambda *args: True)
    monkeypatch.setattr(stt, "refine_transcript", refine, raising=False)
    host = SimpleNamespace(
        out_queue=asyncio.Queue(), ui=_UI(), _speech_output_epoch=0,
        _last_voice_evidence_ms=800.0, _audio_drop_count=0,
        _precision_turn_text="", _precision_stt_enabled=True,
    )

    async def scenario():
        task = asyncio.create_task(stt.run_gemini_transcribe(host, turn_factory=Turn))
        await host.out_queue.put({"activity": "start", "_audio_epoch": 0})
        await host.out_queue.put({"data": np.ones(16000, dtype="<i2").tobytes()})
        await host.out_queue.put({"activity": "end", "_voice_evidence_ms": 800.0})
        for _ in range(30):
            if accepted:
                break
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    assert accepted == ["envoie un rappel à un client pour son site web"]
    assert refine_calls == []


@pytest.mark.parametrize("send_error", [False, True])
def test_incomplete_turn_never_executes_an_earlier_segment(monkeypatch, send_error):
    import core.gemini_transcribe_stt as stt
    monkeypatch.setattr(stt, "_FINALIZE_TIMEOUT_S", 0.01)

    class Session:
        async def send_realtime_input(self, **kwargs):
            if send_error:
                raise ConnectionError("network lost")

    async def scenario():
        turn = stt.GeminiTranscribeTurn(SimpleNamespace())
        turn.session = Session()
        turn.pcm = np.ones(4800, dtype=np.int16).tobytes()
        turn._last_confirmed = "supprime le dossier"
        turn.final = asyncio.get_running_loop().create_future()
        with pytest.raises(stt.GeminiTranscribeError, match="non confirmée"):
            await turn.finish()
        assert not turn._reusable

    asyncio.run(scenario())


def test_gemini_transcribe_is_the_only_final_text_source(monkeypatch):
    import core.gemini_transcribe_stt as stt

    accepted = []

    class Turn:
        def __init__(self, host, on_partial):
            self.pcm = bytearray()
            self.on_partial = on_partial

        async def start(self):
            return None

        async def begin(self, on_partial):
            self.on_partial = on_partial
            self.on_partial("ouvre Google")

        async def feed(self, data):
            self.pcm.extend(data)

        async def finish(self):
            return "ouvre Google Chrome"

        async def close(self):
            return None

    async def accept(host, text, pcm, epoch, prosody, source, acoustic_voice_ms):
        accepted.append((text, bytes(pcm), epoch, prosody, source, acoustic_voice_ms))
        return True

    monkeypatch.setattr(stt, "accept_transcript", accept)
    monkeypatch.setattr(stt, "input_allowed", lambda *args: True)
    host = SimpleNamespace(
        out_queue=asyncio.Queue(), ui=_UI(), _speech_output_epoch=0,
        _last_voice_evidence_ms=200.0, _audio_drop_count=0,
        _precision_turn_text="",
    )

    async def scenario():
        task = asyncio.create_task(stt.run_gemini_transcribe(host, turn_factory=Turn))
        await host.out_queue.put({"activity": "start", "_audio_epoch": 7})
        await host.out_queue.put({"data": np.ones(3200, dtype="<i2").tobytes()})
        await host.out_queue.put({"activity": "prosody", "text": "Voix calme."})
        await host.out_queue.put({"activity": "end", "_voice_evidence_ms": 200.0})
        for _ in range(20):
            if accepted:
                break
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    assert accepted == [("ouvre Google Chrome", np.ones(3200, dtype="<i2").tobytes(),
                         7, "Voix calme.", "pc", 200.0)]
    assert host._precision_turn_text == "ouvre Google Chrome"


def test_cancelled_echo_turn_aborts_without_killing_session(monkeypatch):
    import core.gemini_transcribe_stt as stt

    aborted = []
    closed = []
    accepted = []

    class Turn:
        def __init__(self, host, on_partial):
            self.pcm = bytearray()

        async def start(self):
            return None

        async def begin(self, _on_partial):
            return None

        async def abort(self):
            aborted.append(True)

        async def close(self):
            closed.append(True)

    async def accept(*_args, **_kwargs):
        accepted.append(True)

    monkeypatch.setattr(stt, "accept_transcript", accept)
    monkeypatch.setattr(stt, "input_allowed", lambda *args: True)
    host = SimpleNamespace(
        out_queue=asyncio.Queue(), ui=_UI(), _speech_output_epoch=0,
        _last_voice_evidence_ms=0.0, _audio_drop_count=0,
        _precision_turn_text="",
    )

    async def scenario():
        task = asyncio.create_task(stt.run_gemini_transcribe(host, turn_factory=Turn))
        await host.out_queue.put({"activity": "start", "_audio_epoch": 1})
        await host.out_queue.put({"activity": "cancel", "_audio_epoch": 1})
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    assert aborted == [True]
    assert closed == [True]
    assert accepted == []
    assert not any(log.startswith("STT :") for log in host.ui.logs)


def test_empty_confirmation_is_ignored_without_stt_error(monkeypatch):
    """Un silence / écho ne doit ni alarmer l'UI ni tuer la boucle Transcribe."""
    import core.gemini_transcribe_stt as stt

    closed = []
    accepted = []

    class Turn:
        def __init__(self, host, on_partial):
            self.pcm = bytearray()

        async def start(self):
            return None

        async def begin(self, _on_partial):
            return None

        async def feed(self, data):
            self.pcm.extend(data)

        async def finish(self):
            return ""

        async def close(self):
            closed.append(True)

    async def accept(*_args, **_kwargs):
        accepted.append(True)

    monkeypatch.setattr(stt, "accept_transcript", accept)
    monkeypatch.setattr(stt, "input_allowed", lambda *args: True)
    host = SimpleNamespace(
        out_queue=asyncio.Queue(), ui=_UI(), _speech_output_epoch=0,
        _last_voice_evidence_ms=200.0, _audio_drop_count=0,
        _precision_turn_text="",
    )

    async def scenario():
        task = asyncio.create_task(stt.run_gemini_transcribe(host, turn_factory=Turn))
        await host.out_queue.put({"activity": "start", "_audio_epoch": 0})
        await host.out_queue.put({"data": np.ones(3200, dtype="<i2").tobytes()})
        await host.out_queue.put({"activity": "end", "_voice_evidence_ms": 200.0})
        for _ in range(20):
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    assert accepted == []
    assert closed == [True]
    assert not any(log.startswith("STT :") for log in host.ui.logs)


def test_reader_cancel_does_not_nameerror_on_missing_stream():
    """Régression : `_read` référençait `stream` (inconnu) à l'annulation,
    ce qui tuait le TaskGroup et coupait la voix en plein briefing."""
    import core.gemini_transcribe_stt as stt

    class DummySession:
        async def receive(self):
            await asyncio.sleep(3600)
            if False:
                yield None

    async def scenario():
        turn = stt.GeminiTranscribeTurn(SimpleNamespace())
        turn.session = DummySession()
        turn.final = asyncio.get_running_loop().create_future()
        turn.reader = asyncio.create_task(turn._read())
        await asyncio.sleep(0)
        await turn.close()

    asyncio.run(scenario())


def test_speaking_mid_turn_aborts_without_error(monkeypatch):
    import core.gemini_transcribe_stt as stt

    aborted = []

    class Turn:
        def __init__(self, host, on_partial):
            self.pcm = bytearray()

        async def start(self):
            return None

        async def begin(self, _on_partial):
            return None

        async def feed(self, data):
            self.pcm.extend(data)

        async def abort(self):
            aborted.append(True)

        async def finish(self):
            raise AssertionError("finish ne doit pas être appelé pendant la voix")

        async def close(self):
            return None  # fermeture à l’arrêt de la tâche

    allowed = {"value": True}

    async def accept(*_args, **_kwargs):
        raise AssertionError("aucune transcription ne doit être acceptée")

    monkeypatch.setattr(stt, "accept_transcript", accept)
    monkeypatch.setattr(stt, "input_allowed", lambda *args: allowed["value"])
    host = SimpleNamespace(
        out_queue=asyncio.Queue(), ui=_UI(), _speech_output_epoch=0,
        _last_voice_evidence_ms=0.0, _audio_drop_count=0,
        _precision_turn_text="",
    )

    async def scenario():
        task = asyncio.create_task(stt.run_gemini_transcribe(host, turn_factory=Turn))
        await host.out_queue.put({"activity": "start", "_audio_epoch": 0})
        for _ in range(15):
            await asyncio.sleep(0)
        await host.out_queue.put({"data": np.ones(640, dtype="<i2").tobytes()})
        for _ in range(10):
            await asyncio.sleep(0)
        allowed["value"] = False
        await host.out_queue.put({"activity": "end", "_voice_evidence_ms": 50.0})
        for _ in range(20):
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    assert aborted == [True]
    assert not any(log.startswith("STT :") for log in host.ui.logs)


def test_persistent_reader_handles_two_turns_and_ignores_idle_text():
    import core.gemini_transcribe_stt as stt

    class Session:
        def __init__(self):
            self.messages = asyncio.Queue()
            self.receives = 0

        async def send_realtime_input(self, **kwargs):
            pass

        async def receive(self):
            self.receives += 1
            while True:
                message = await self.messages.get()
                yield message
                if message.server_content.turn_complete:
                    return  # comportement réel du SDK google-genai

    def message(text, complete=False):
        return SimpleNamespace(server_content=SimpleNamespace(
            input_transcription=SimpleNamespace(text=text),
            turn_complete=complete,
        ))

    async def scenario():
        turn = stt.GeminiTranscribeTurn(SimpleNamespace())
        turn.session = Session()
        turn.reader = asyncio.create_task(turn._read())
        try:
            # Un événement serveur avant begin ne doit pas tuer le lecteur.
            await turn.session.messages.put(message("ancien texte", True))
            await asyncio.sleep(0)
            for expected in ["ouvre Chrome", "non ne le ferme pas"]:
                await turn.begin()
                turn.pcm.extend(b"\x01\x00" * 4800)
                finish = asyncio.create_task(turn.finish())
                await asyncio.sleep(0)
                await turn.session.messages.put(message(expected, True))
                assert await asyncio.wait_for(finish, 1) == expected
            assert turn.session.receives >= 3
        finally:
            await turn.close()

    asyncio.run(scenario())


def test_abort_discards_late_results_and_requires_new_connection():
    import core.gemini_transcribe_stt as stt

    class Session:
        async def send_realtime_input(self, **kwargs):
            pass

        async def receive(self):
            yield SimpleNamespace(server_content=SimpleNamespace(
                input_transcription=SimpleNamespace(text="supprime tout"),
                turn_complete=True,
            ))
            await asyncio.Event().wait()

    async def scenario():
        displayed = []
        turn = stt.GeminiTranscribeTurn(SimpleNamespace())
        turn.session = Session()
        await turn.begin(displayed.append)
        await turn.abort()
        turn.reader = asyncio.create_task(turn._read())
        await asyncio.sleep(0)
        assert displayed == []
        assert turn.final.result() == ""
        with pytest.raises(stt.GeminiTranscribeError, match="renouveler"):
            await turn.begin()
        await turn.close()

    asyncio.run(scenario())


def test_reader_failure_never_reuses_confirmed_prefix():
    import core.gemini_transcribe_stt as stt

    class Session:
        async def send_realtime_input(self, **kwargs):
            pass

        async def receive(self):
            yield SimpleNamespace(server_content=SimpleNamespace(
                input_transcription=SimpleNamespace(text="envoie le message"),
                turn_complete=False,
            ))
            raise ConnectionError("connection lost before negation")

    async def scenario():
        turn = stt.GeminiTranscribeTurn(SimpleNamespace())
        turn.session = Session()
        await turn.begin()
        turn.pcm.extend(b"\x01\x00" * 4800)
        await turn._read()
        with pytest.raises(stt.GeminiTranscribeError):
            await turn.finish()

    asyncio.run(scenario())


def test_real_api_activity_end_finalizes_without_turn_complete():
    """Trace réseau réelle : texte, generation_complete, puis ACTIVITY_END."""
    import core.gemini_transcribe_stt as stt

    class Session:
        async def send_realtime_input(self, **kwargs):
            pass

        async def receive(self):
            yield SimpleNamespace(server_content=SimpleNamespace(
                input_transcription=SimpleNamespace(text="Ne ferme pas Google Chrome.")))
            yield SimpleNamespace(server_content=SimpleNamespace(generation_complete=True))
            yield SimpleNamespace(server_content=None, voice_activity=SimpleNamespace(
                voice_activity_type="ACTIVITY_END"))
            await asyncio.Event().wait()

    async def scenario():
        turn = stt.GeminiTranscribeTurn(SimpleNamespace())
        turn.session = Session()
        await turn.begin()
        turn.pcm.extend(b"\x01\x00" * 4800)
        finishing = asyncio.create_task(turn.finish())
        await asyncio.sleep(0)
        turn.reader = asyncio.create_task(turn._read())
        try:
            assert await asyncio.wait_for(finishing, 1) == "Ne ferme pas Google Chrome."
            assert turn._reusable
        finally:
            await turn.close()

    asyncio.run(scenario())
