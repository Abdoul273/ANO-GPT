import asyncio
from types import SimpleNamespace

import numpy as np

from core.precision_stt import (
    PrecisionTranscriber,
    should_refine_live_turn,
    transcript_similarity,
    transcripts_conflict,
    transcripts_agree,
)


class _Session:
    def __init__(self):
        self.sent = []

    async def send_realtime_input(self, **kwargs):
        self.sent.append(kwargs)

    async def receive(self):
        yield SimpleNamespace(server_content=SimpleNamespace(
            interim_input_transcription=SimpleNamespace(text="ferme Firefox"),
            input_transcription=None,
            turn_complete=False,
        ))
        yield SimpleNamespace(server_content=SimpleNamespace(
            interim_input_transcription=None,
            input_transcription=SimpleNamespace(text="ferme uniquement Firefox"),
            turn_complete=True,
        ))


class _Connect:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *_args):
        return False


class _Live:
    def __init__(self):
        self.calls = []
        self.session = _Session()

    def connect(self, **kwargs):
        self.calls.append(kwargs)
        return _Connect(self.session)


class _FakeClient:
    def __init__(self):
        self.live = _Live()
        self.aio = self
        self.closed = False

    async def aclose(self):
        self.closed = True


def test_transcription_35_envoie_du_pcm_francais_et_le_vocabulaire():
    client = _FakeClient()
    engine = PrecisionTranscriber(
        "key", vocabulary=["ANO-GPT", "Firefox"],
        client_factory=lambda _key: client, require_speech=False,
    )
    audio = np.full(16000, 0.1, dtype=np.float32)

    first = asyncio.run(engine.transcribe(audio))
    second = asyncio.run(engine.transcribe(audio))

    assert first.text == "ferme uniquement Firefox"
    assert second == first
    assert len(client.live.calls) == 1
    call = client.live.calls[0]
    assert call["model"] == "models/gemini-3.5-transcribe-live"
    config = call["config"].input_audio_transcription
    assert config.language_codes == ["fr-FR"]
    assert "ANO-GPT" in config.custom_vocabulary
    assert any("audio" in item for item in client.live.session.sent)
    assert client.closed


def test_comparaison_tolere_la_ponctuation_mais_refuse_une_negation():
    assert transcript_similarity(
        "Ferme Firefox, s'il te plaît", "ferme firefox s il te plait"
    ) > 0.9
    assert not transcripts_conflict("ferme Firefox", "Ferme Firefox.")
    assert transcripts_conflict("ferme Firefox", "ne ferme pas Firefox")


def test_une_divergence_importante_est_bloquee():
    assert transcripts_conflict(
        "supprime le dossier projet", "ouvre le dossier projet"
    )


def test_la_seconde_passe_garde_les_vraies_phrases_et_epargne_les_oui_non():
    assert not should_refine_live_turn(np.zeros(int(16000 * 0.35)))
    assert should_refine_live_turn(np.zeros(int(16000 * 0.8)))


def test_literal_agreement_preserves_every_word_and_order():
    assert transcripts_agree("Salut !", "salut")
    assert transcripts_agree("Écoute-moi.", "ecoute moi")
    for left, right in [
        ("salut", "allo"), ("salut", "oui"), ("oui", "non"),
        ("envoie le message à Marie", "envoie le message à Pierre"),
        ("ouvre Chrome", "n'ouvre pas Chrome"),
        ("envoie 15 euros", "envoie 1.5 euros"),
        ("ouvre puis ferme", "ferme puis ouvre"),
        ("oui oui", "oui"), ("", ""),
    ]:
        assert not transcripts_agree(left, right)


def test_interim_transcript_is_never_used_as_final():
    client = _FakeClient()
    async def receive():
        yield SimpleNamespace(server_content=SimpleNamespace(
            interim_input_transcription=SimpleNamespace(text="supprime tout"),
            input_transcription=None, turn_complete=True,
        ))
    client.live.session.receive = receive
    engine = PrecisionTranscriber("key", client_factory=lambda _: client)
    result = asyncio.run(engine.transcribe(np.ones(16000, dtype=np.int16)))
    assert not result.available and result.text == ""


def test_long_audio_is_rejected_instead_of_losing_first_words():
    client = _FakeClient()
    engine = PrecisionTranscriber("key", client_factory=lambda _: client)
    result = asyncio.run(engine.transcribe(np.ones(16000 * 26, dtype=np.int16)))
    assert not result.available
    assert not client.live.calls


def test_temporary_failure_can_be_retried_for_the_same_audio():
    from core.precision_stt import PrecisionResult
    from unittest.mock import AsyncMock
    async def scenario():
        # Signal constant synthétique : ce test couvre le cache/retry, pas le VAD.
        engine = PrecisionTranscriber("key", require_speech=False)
        engine._request = AsyncMock(side_effect=[
            PrecisionResult(available=False, error="timeout"),
            PrecisionResult(text="salut"),
        ])
        clip = np.ones(4800, dtype=np.int16)
        assert not (await engine.transcribe(clip)).available
        assert (await engine.transcribe(clip)).text == "salut"
        assert engine._request.await_count == 2
    asyncio.run(scenario())


def test_connection_ending_without_boundary_rejects_confirmed_prefix():
    client = _FakeClient()
    async def receive():
        yield SimpleNamespace(server_content=SimpleNamespace(
            input_transcription=SimpleNamespace(text="supprime le dossier"),
            turn_complete=False,
        ))
    client.live.session.receive = receive
    engine = PrecisionTranscriber("key", client_factory=lambda _: client,
                                  require_speech=False)
    result = asyncio.run(engine.transcribe(np.ones(4800, dtype=np.int16)))
    assert not result.available
    assert result.text == ""
