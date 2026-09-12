"""Tests unitaires pour AudioDenoiser (RNNoise ctypes mocké, AGC, repli)."""

from __future__ import annotations

import ctypes

import numpy as np
import pytest

from core.audio_denoise import (
    AGCConfig,
    AudioDenoiser,
    AutomaticGainControl,
    downsample_48k_to_16k,
    get_rnnoise_ctypes,
    rms_dbfs,
    upsample_16k_to_48k,
)


class _FakeRNNoise:
    """Mock ctypes : copie l'entrée, atténue, VAD configurable."""

    available = True

    def __init__(self, attenuate: float = 0.2, vad: float = 0.9):
        self.attenuate = attenuate
        self.vad = vad
        self.created = 0
        self.destroyed = 0
        self.frames = 0

    def create_state(self):
        self.created += 1
        return object()

    def destroy_state(self, state) -> None:
        self.destroyed += 1

    def process_frame(self, state, out_buf, in_buf) -> float:
        self.frames += 1
        for i in range(480):
            out_buf[i] = float(in_buf[i]) * self.attenuate
        return self.vad


def _tone(hz: float, n: int, amp: float = 0.3, sr: int = 16000) -> np.ndarray:
    t = np.arange(n) / float(sr)
    return (amp * np.sin(2 * np.pi * hz * t)).astype(np.float32)


def test_resample_exact_3x_ratio():
    x = _tone(400, 160)
    up = upsample_16k_to_48k(x)
    assert up.shape == (480,)
    down = downsample_48k_to_16k(up)
    assert down.shape == (160,)
    # Un sinus 400 Hz survit à un aller-retour 3:1
    corr = np.corrcoef(x, down)[0, 1]
    assert corr > 0.95


def test_rms_dbfs_known_level():
    # Amplitude 0.1258 ≈ -18 dBFS RMS pour un sinus (facteur 1/sqrt(2))
    tone = _tone(1000, 16000, amp=0.1258 * np.sqrt(2))
    db = rms_dbfs(tone)
    assert abs(db - (-18.0)) < 0.3


def test_agc_amplifies_quiet_speech():
    cfg = AGCConfig(target_rms_dbfs=-18.0, min_gain_db=-6.0, max_gain_db=+18.0)
    agc = AutomaticGainControl(sample_rate=16000, config=cfg)

    t = np.arange(16000) / 16000.0
    quiet = (0.018 * np.sin(2 * np.pi * 400 * t)).astype(np.float32)

    rms_before = np.sqrt(np.mean(quiet ** 2))
    out = None
    for i in range(10):
        chunk = quiet[i * 1600:(i + 1) * 1600]
        out = agc.process(chunk)

    rms_after = np.sqrt(np.mean(out ** 2))
    assert rms_after > rms_before * 1.5, "L'AGC devrait amplifier le signal faible vers -18 dBFS"
    assert agc.current_gain_db > 0.0


def test_agc_limits_loud_signal_without_clipping():
    cfg = AGCConfig(target_rms_dbfs=-18.0, limiter_ceiling=0.95)
    agc = AutomaticGainControl(sample_rate=16000, config=cfg)

    t = np.arange(1600) / 16000.0
    loud = (1.5 * np.sin(2 * np.pi * 300 * t)).astype(np.float32)

    out = agc.process(loud)
    assert np.max(np.abs(out)) <= 0.96, "Le limiteur AGC doit empêcher tout dépassement du plafond"


def test_agc_does_not_amplify_silence():
    cfg = AGCConfig(noise_gate_dbfs=-46.0, max_gain_db=+18.0)
    agc = AutomaticGainControl(sample_rate=16000, config=cfg)
    silence = (np.random.randn(1600).astype(np.float32) * 0.0005)
    agc.process(silence)
    assert agc.current_gain_db < 3.0


def test_audio_denoiser_with_mocked_rnnoise_reduces_noise():
    fake = _FakeRNNoise(attenuate=0.15, vad=0.05)
    denoiser = AudioDenoiser(sample_rate=16000, enable_agc=False, rnnoise=fake)

    noise = (np.random.randn(16000) * 800).astype(np.int16)
    cleaned = denoiser.process(noise)

    assert cleaned.shape == noise.shape
    assert cleaned.dtype == noise.dtype
    rms_in = np.sqrt(np.mean(noise.astype(np.float32) ** 2))
    rms_out = np.sqrt(np.mean(cleaned.astype(np.float32) ** 2))
    assert rms_out < rms_in * 0.4
    assert fake.frames > 0
    assert denoiser.metrics.rms_out_db < denoiser.metrics.rms_in_db


def test_audio_denoiser_mock_preserves_length_and_bytes():
    fake = _FakeRNNoise(attenuate=1.0, vad=0.95)
    denoiser = AudioDenoiser(sample_rate=16000, enable_agc=False, rnnoise=fake)
    pcm = (_tone(440, 3200) * 20000).astype(np.int16).tobytes()
    cleaned = denoiser.process(pcm)
    assert isinstance(cleaned, bytes)
    assert len(cleaned) == len(pcm)


def test_audio_denoiser_spectral_fallback_without_rnnoise():
    denoiser = AudioDenoiser(sample_rate=16000, enable_rnnoise=False, enable_agc=True)
    assert denoiser._rnnoise.available is False
    t = np.arange(3200) / 16000.0
    speech = (0.05 * np.sin(2 * np.pi * 300 * t)).astype(np.float32)
    out = denoiser.process(speech)
    assert out.shape == speech.shape
    assert np.max(np.abs(out)) <= 1.0


def test_audio_denoiser_empty_and_reset():
    denoiser = AudioDenoiser(sample_rate=16000, enable_rnnoise=False, enable_agc=True)
    empty_arr = np.array([], dtype=np.int16)
    assert denoiser.process(empty_arr).size == 0
    denoiser.reset()
    assert denoiser.metrics.rms_in_db == -100.0


@pytest.mark.skipif(
    not get_rnnoise_ctypes().available,
    reason="librnnoise.so absente — installer le paquet Arch rnnoise",
)
def test_rnnoise_ctypes_loading_and_execution():
    rnnoise = get_rnnoise_ctypes()
    st = rnnoise.create_state()
    assert st is not None

    in_buf = np.ones(480, dtype=np.float32) * 5000.0
    c_in = (ctypes.c_float * 480)(*in_buf)
    c_out = (ctypes.c_float * 480)()

    vad_prob = rnnoise.process_frame(st, c_out, c_in)
    assert isinstance(vad_prob, float)
    assert 0.0 <= vad_prob <= 1.0

    rnnoise.destroy_state(st)


@pytest.mark.skipif(
    not get_rnnoise_ctypes().available,
    reason="librnnoise.so absente — installer le paquet Arch rnnoise",
)
def test_audio_denoiser_noise_reduction():
    denoiser = AudioDenoiser(sample_rate=16000)
    assert denoiser._rnnoise.available

    noise = (np.random.randn(16000) * 800).astype(np.int16)
    cleaned = denoiser.process(noise)

    assert cleaned.shape == noise.shape
    assert cleaned.dtype == noise.dtype

    rms_in = np.sqrt(np.mean(noise.astype(np.float32) ** 2))
    rms_out = np.sqrt(np.mean(cleaned.astype(np.float32) ** 2))

    assert rms_out < rms_in * 0.4, "RNNoise doit atténuer significativement le bruit blanc stationnaire"
    assert denoiser.metrics.rms_out_db < denoiser.metrics.rms_in_db


@pytest.mark.skipif(
    not get_rnnoise_ctypes().available,
    reason="librnnoise.so absente — installer le paquet Arch rnnoise",
)
def test_audio_denoiser_speech_preservation():
    denoiser_no_agc = AudioDenoiser(sample_rate=16000, enable_agc=False)
    t = np.arange(16000) / 16000.0
    speech_float = (
        0.35 * np.sin(2 * np.pi * 300 * t) + 0.25 * np.sin(2 * np.pi * 900 * t)
    ).astype(np.float32)
    speech_pcm = (speech_float * 30000.0).astype(np.int16)

    cleaned_raw = denoiser_no_agc.process(speech_pcm)
    rms_in = np.sqrt(np.mean(speech_pcm.astype(np.float32) ** 2))
    rms_out = np.sqrt(np.mean(cleaned_raw.astype(np.float32) ** 2))
    assert rms_out > rms_in * 0.85, (
        f"RNNoise doit préserver les formants vocaux (> 85%, obtenu {rms_out / rms_in:.2f})"
    )

    denoiser_full = AudioDenoiser(sample_rate=16000, enable_agc=True)
    cleaned_full = denoiser_full.process(speech_pcm)
    rms_full = np.sqrt(np.mean(cleaned_full.astype(np.float32) ** 2))

    assert 3500 <= rms_full <= 5500, f"L'AGC doit stabiliser la voix autour de -18 dBFS (obtenu: {rms_full})"
    assert denoiser_full.metrics.last_vad_prob > 0.70, "Le VAD interne RNNoise doit reconnaître la parole"


@pytest.mark.skipif(
    not get_rnnoise_ctypes().available,
    reason="librnnoise.so absente — installer le paquet Arch rnnoise",
)
def test_audio_denoiser_bytes_interface():
    denoiser = AudioDenoiser(sample_rate=16000)
    t = np.arange(3200) / 16000.0
    pcm = (0.2 * np.sin(2 * np.pi * 440 * t) * 32767).astype(np.int16).tobytes()
    cleaned_bytes = denoiser.process(pcm)
    assert isinstance(cleaned_bytes, bytes)
    assert len(cleaned_bytes) == len(pcm)


def test_fallback_preserves_very_quiet_syllables():
    denoiser = AudioDenoiser(enable_rnnoise=False, enable_agc=False)
    quiet = _tone(200, 320, amp=0.001)
    cleaned = denoiser.process(quiet)
    assert np.sqrt(np.mean(cleaned ** 2)) >= 0.95 * np.sqrt(np.mean(quiet ** 2))
    assert np.isfinite(cleaned).all()
