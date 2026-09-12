"""Tests unitaires pour VoiceActivityDetector (Silero, WebRTC, padding)."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
import pytest

from core.audio_vad import VADConfig, VoiceActivityDetector
from core.vad_silero import MODEL_PATH


@pytest.fixture
def silero_opt_in(monkeypatch):
    monkeypatch.setenv("ANOGPT_ENABLE_ONNX_VAD", "1")


def test_vad_config_defaults():
    cfg = VADConfig()
    assert cfg.sample_rate == 16000
    assert cfg.threshold == 0.50
    assert cfg.preroll_ms == 250
    assert cfg.hangover_ms == 350
    assert cfg.min_speech_ms == 96


def test_vad_confirmation_uses_capture_frame_duration():
    vad = VoiceActivityDetector(VADConfig(min_speech_ms=96))
    # 96 ms exige cinq trames de capture de 20 ms (100 ms), jamais trois
    # fenêtres Silero de 32 ms alors que celles-ci ne sont pas les frames
    # filtrées.
    assert vad.min_speech_frames == 5


def test_vad_silence_rejected_on_any_backend():
    vad = VoiceActivityDetector()
    silence = np.zeros(16000, dtype=np.int16)
    conf = vad.confidence(silence)
    assert conf < 0.15, f"La confiance sur du silence pur doit être basse (obtenu: {conf})"
    assert not vad.is_speech(silence)


def test_vad_20ms_capture_frames_are_scored(monkeypatch):
    """Les trames 20 ms (320 éch.) ne doivent plus être rejetées d'office."""
    cfg = VADConfig()
    vad = VoiceActivityDetector(config=cfg)
    vad._session = None
    scores = []

    def fake_webrtc(frame_float):
        scores.append(frame_float.size)
        return 0.9

    vad._webrtc_vad = object()
    vad._webrtc_kind = "module"
    vad.backend = "webrtcvad"
    vad._webrtc_prob = fake_webrtc

    frame_20ms = (np.ones(320, dtype=np.int16) * 4000)
    is_voice, prob = vad.process_frame(frame_20ms)
    assert is_voice is True
    assert prob == 0.9
    assert scores == [320]


def test_vad_padding_preroll_and_hangover():
    cfg = VADConfig(preroll_ms=64, hangover_ms=64, min_speech_ms=32)
    vad = VoiceActivityDetector(config=cfg)

    frame_len = 512
    silence_frame = np.zeros(frame_len, dtype=np.int16)

    def mock_process_frame(frame):
        is_voice = bool(np.any(frame != 0))
        return is_voice, (1.0 if is_voice else 0.0)

    vad.process_frame = mock_process_frame

    speech_frame = (np.ones(frame_len, dtype=np.int16) * 5000)

    frames = [
        silence_frame,
        silence_frame,
        silence_frame,
        speech_frame,
        speech_frame,
        silence_frame,
        silence_frame,
        silence_frame,
        silence_frame,
    ]

    output = list(vad.filter_speech_frames(iter(frames)))

    assert len(output) >= 4, f"Le filtre doit conserver le preroll et le hangover (obtenu {len(output)})"
    assert any(np.array_equal(f, speech_frame) for f in output)


def test_vad_webrtc_fallback(monkeypatch):
    cfg = VADConfig()
    vad = VoiceActivityDetector(config=cfg)
    vad._session = None
    vad._init_webrtc()

    assert vad.backend in ("webrtcvad", "webrtcvad_native", "energy_heuristic")
    silence = np.zeros(16000, dtype=np.int16)
    assert not vad.is_speech(silence)


@pytest.mark.usefixtures("silero_opt_in")
@pytest.mark.skipif(not MODEL_PATH.exists(), reason="modèle Silero absent")
def test_vad_silero_loading_and_silence(silero_opt_in):
    vad = VoiceActivityDetector()
    assert vad.backend == "silero_onnx", "Silero ONNX devrait être chargé depuis models/silero_vad.onnx"

    silence = np.zeros(16000, dtype=np.int16)
    conf = vad.confidence(silence)
    assert conf < 0.05, f"La confiance sur du silence pur doit être quasi-nulle (obtenu: {conf})"
    assert not vad.is_speech(silence)


@pytest.mark.usefixtures("silero_opt_in")
@pytest.mark.skipif(not MODEL_PATH.exists(), reason="modèle Silero absent")
def test_vad_noise_discrimination(silero_opt_in):
    vad = VoiceActivityDetector()
    noise = (np.random.randn(16000) * 800).astype(np.int16)
    conf = vad.confidence(noise)
    assert conf < 0.25, f"Le bruit blanc ne doit pas être classifié comme de la parole (obtenu: {conf})"
    assert not vad.is_speech(noise)


@pytest.mark.usefixtures("silero_opt_in")
@pytest.mark.skipif(not MODEL_PATH.exists(), reason="modèle Silero absent")
def test_vad_speech_detection(silero_opt_in):
    vad = VoiceActivityDetector()

    if not shutil.which("espeak-ng") or not shutil.which("ffmpeg"):
        pytest.skip("espeak-ng ou ffmpeg non disponible pour générer de la parole réelle")

    with tempfile.TemporaryDirectory() as tmpdir:
        wav_path = Path(tmpdir) / "phrase.wav"
        subprocess.run(
            ["espeak-ng", "-v", "fr", "-s", "150", "-w", str(wav_path),
             "Bonjour ANO-GPT, peux-tu ouvrir Firefox s'il te plaît ?"],
            check=True, capture_output=True,
        )
        raw_pcm = subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-i", str(wav_path),
             "-ar", "16000", "-ac", "1", "-f", "s16le", "-"],
            check=True, capture_output=True,
        ).stdout

    speech_pcm = np.frombuffer(raw_pcm, dtype=np.int16)
    assert len(speech_pcm) > 16000

    conf = vad.confidence(speech_pcm)
    assert conf > 0.80, f"Silero VAD doit détecter la parole française (> 0.80, obtenu: {conf})"
    assert vad.is_speech(speech_pcm)


def test_confirmation_preserves_each_onset_frame_exactly_once():
    vad = VoiceActivityDetector(VADConfig(preroll_ms=100, min_speech_ms=96))
    frames = [np.full(320, n, dtype=np.int16) for n in range(8)]
    vad.process_frame = lambda f: (int(f[0]) >= 2, float(int(f[0]) >= 2))
    output = list(vad.filter_speech_frames(iter(frames)))
    # Les quatre premières trames de parole attendaient la confirmation.
    assert [int(f[0]) for f in output if f[0] >= 2] == [2, 3, 4, 5, 6, 7]


def test_one_loud_click_cannot_validate_a_whole_clip():
    vad = VoiceActivityDetector()
    vad._session = None
    vad._webrtc_vad = object()
    for scores, expected in [
        ([0.0] * 10 + [1.0] + [0.0] * 10, False),
        ([0.0] * 8 + [0.85] * 5 + [0.0] * 8, True),
    ]:
        iterator = iter(scores)
        vad._webrtc_prob = lambda _: next(iterator)
        assert vad.is_speech(np.zeros(320 * len(scores), dtype=np.int16)) is expected
