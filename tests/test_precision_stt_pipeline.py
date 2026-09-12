"""Tests d'intégration du pipeline studio-grade dans PrecisionTranscriber."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np

from core.audio_capture import AudioCaptureConfig, AudioCaptureStream
from core.audio_denoise import DenoiserMetrics
from core.audio_vad import VoiceActivityDetector
from core.precision_stt import PrecisionTranscriber


class _Session:
    def __init__(self):
        self.sent = []

    async def send_realtime_input(self, **kwargs):
        self.sent.append(kwargs)

    async def receive(self):
        yield SimpleNamespace(server_content=SimpleNamespace(
            interim_input_transcription=SimpleNamespace(text="ouvre le terminal"),
            input_transcription=SimpleNamespace(text="ouvre le terminal"),
            turn_complete=False,
        ))
        yield SimpleNamespace(server_content=None, voice_activity=SimpleNamespace(
            voice_activity_type="ACTIVITY_END",
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


def _mock_vad(*, speech: bool, confidence: float = 0.95) -> MagicMock:
    mock_vad = MagicMock(spec=VoiceActivityDetector)
    mock_vad.threshold = 0.5
    mock_vad.confidence.return_value = confidence
    mock_vad.is_speech.return_value = speech
    mock_vad.reset.return_value = None
    return mock_vad


def test_pipeline_filters_noise_before_gemini():
    """Du bruit sans voix est stoppé avant tout appel réseau."""
    client = _FakeClient()
    engine = PrecisionTranscriber(
        "key",
        client_factory=lambda _: client,
        vad=_mock_vad(speech=False, confidence=0.04),
        require_speech=True,
    )

    noise = (np.random.randn(16000) * 500).astype(np.int16)
    result = asyncio.run(engine.transcribe(noise))
    assert not result.available
    assert "aucun contenu vocal détecté" in result.error
    assert len(client.live.calls) == 0


def test_pipeline_requires_speech_by_default():
    """Le VAD est une barrière réseau par défaut, pas un simple métrique."""
    client = _FakeClient()
    engine = PrecisionTranscriber(
        "key",
        client_factory=lambda _: client,
        vad=_mock_vad(speech=False, confidence=0.03),
    )

    result = asyncio.run(engine.transcribe(np.ones(16000, dtype=np.int16)))

    assert not result.available
    assert "aucun contenu vocal détecté" in result.error
    assert len(client.live.calls) == 0


def test_pipeline_transcribes_speech_when_confirmed():
    """La parole confirmée traverse le débruiteur, le VAD et atteint Gemini."""
    client = _FakeClient()
    mock_vad = _mock_vad(speech=True, confidence=0.95)

    engine = PrecisionTranscriber(
        "key",
        client_factory=lambda _: client,
        vad=mock_vad,
        require_speech=True,
    )

    t = np.arange(16000) / 16000.0
    speech = ((0.3 * np.sin(2 * np.pi * 300 * t) + 0.2 * np.sin(2 * np.pi * 800 * t)) * 20000).astype(np.int16)

    result = asyncio.run(engine.transcribe(speech))
    assert result.available
    assert result.text == "ouvre le terminal"
    assert len(client.live.calls) == 1
    assert mock_vad.confidence.called


def test_pipeline_denoise_runs_before_vad():
    """L'ordre de la chaîne est Denoise → VAD → Gemini."""
    order: list[str] = []
    client = _FakeClient()

    mock_denoise = MagicMock()
    mock_denoise.process.side_effect = lambda audio: (order.append("denoise") or audio)
    mock_denoise.metrics = DenoiserMetrics(rms_in_db=-30.0, rms_out_db=-18.0, agc_gain_db=12.0)

    mock_vad = _mock_vad(speech=True, confidence=0.9)
    mock_vad.confidence.side_effect = lambda audio: (order.append("vad") or 0.9)

    engine = PrecisionTranscriber(
        "key",
        client_factory=lambda _: client,
        denoiser=mock_denoise,
        vad=mock_vad,
        require_speech=True,
    )
    audio = np.ones(16000, dtype=np.int16)
    result = asyncio.run(engine.transcribe(audio))
    assert result.available
    assert order[:2] == ["denoise", "vad"]
    mock_denoise.process.assert_called_once()


def test_pipeline_transcribe_stream_denoises_then_filters():
    """transcribe_stream débruite chaque trame avant le VAD."""
    client = _FakeClient()
    order: list[str] = []

    speech_frames = [
        (np.ones(320, dtype=np.int16) * 5000).tobytes()
        for _ in range(25)
    ]

    mock_denoise = MagicMock()
    mock_denoise.process.side_effect = lambda frame: (order.append("denoise") or frame)
    mock_denoise.metrics = DenoiserMetrics()

    mock_vad = _mock_vad(speech=True, confidence=0.98)

    def _filter(frames):
        collected = list(frames)
        order.append("vad")
        return iter(collected)

    mock_vad.filter_speech_frames.side_effect = _filter

    engine = PrecisionTranscriber(
        "key",
        client_factory=lambda _: client,
        denoiser=mock_denoise,
        vad=mock_vad,
    )

    capture_stream = AudioCaptureStream(AudioCaptureConfig(frame_ms=20))
    capture_stream.iter_frames = MagicMock(return_value=iter(speech_frames))

    result = asyncio.run(engine.transcribe_stream(capture_stream, max_duration_seconds=5.0))
    assert result.available
    assert result.text == "ouvre le terminal"
    assert order[0] == "denoise"
    assert "vad" in order
    assert order.index("denoise") < order.index("vad")
    assert len(client.live.calls) == 1


def test_vad_failure_rejects_even_preprocessed_audio():
    async def scenario():
        client = _FakeClient()
        vad = _mock_vad(speech=True)
        vad.confidence.side_effect = RuntimeError("detector unavailable")
        engine = PrecisionTranscriber("key", client_factory=lambda _: client, vad=vad)
        for already_processed in (False, True):
            result = await engine.transcribe(np.ones(4800, dtype=np.int16),
                                              already_processed=already_processed)
            assert not result.available
        assert client.live.calls == []
    asyncio.run(scenario())


def test_diagnostic_cache_cannot_bypass_required_speech():
    async def scenario():
        client = _FakeClient()
        engine = PrecisionTranscriber("key", client_factory=lambda _: client,
                                      vad=_mock_vad(speech=False, confidence=0.0))
        clip = np.ones(4800, dtype=np.int16)
        diagnostic = await engine.transcribe(clip, require_speech=False)
        assert diagnostic.available
        guarded = await engine.transcribe(clip, require_speech=True)
        assert not guarded.available
        assert len(client.live.calls) == 1
    asyncio.run(scenario())
