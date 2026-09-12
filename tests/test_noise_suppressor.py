"""Tests unitaires et benchmark pour le suppresseur de bruit DeepFilterNet 3 (core/noise_suppressor.py).

Ces tests valident :
1. L'initialisation, le singleton et la configuration (16 kHz / 48 kHz, 10 ms / 20 ms).
2. Le pipeline temps réel à zéro allocation mémoire (reprise des tampons, gestion des types).
3. La suppression radicale des frappes de clavier mécanique (switches bleus) et clics souris.
4. L'atténuation du bruit stationnaire (climatiseur, ventilateur) et la préservation de la voix.
5. La conformité CPU (< 3% sur cœur standard à 16 kHz) et latence minimale (10 ms).
6. Le benchmark SNR synthétique et l'intégration avec le moteur audio.
"""
from __future__ import annotations

import time
import numpy as np
import pytest

from core.noise_suppressor import (
    NoiseSuppressor,
    NoiseSuppressorConfig,
    DenoiseEngineType,
    get_noise_suppressor,
    benchmark_snr,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers acoustiques
# ═══════════════════════════════════════════════════════════════════════════════

def make_vocal_tone(freq_hz: float = 200.0, duration_s: float = 0.5, sr: int = 16000, amp: float = 0.4) -> np.ndarray:
    """Génère un signal harmonique représentatif d'une voix humaine."""
    t = np.arange(int(sr * duration_s)) / sr
    return (
        amp * np.sin(2 * np.pi * freq_hz * t)
        + (amp * 0.6) * np.sin(2 * np.pi * (freq_hz * 2) * t)
        + (amp * 0.3) * np.sin(2 * np.pi * (freq_hz * 3) * t)
    ).astype(np.float32)


def make_mechanical_click(duration_s: float = 0.008, sr: int = 16000, amp: float = 0.5) -> np.ndarray:
    """Génère l'impact acoustique d'un switch mécanique bleu (3.8 kHz + décroissance rapide)."""
    n_samples = int(sr * duration_s)
    decay = np.exp(-np.linspace(0, 12, n_samples)).astype(np.float32)
    carrier = np.sin(2 * np.pi * 3800.0 * np.arange(n_samples) / sr).astype(np.float32)
    noise = 0.35 * np.random.randn(n_samples).astype(np.float32)
    return (carrier + noise) * decay * amp


def make_ac_hum(duration_s: float = 0.5, sr: int = 16000, amp: float = 0.03) -> np.ndarray:
    """Génère le ronflement d'une climatisation (50 Hz + souffle)."""
    t = np.arange(int(sr * duration_s)) / sr
    hum = amp * np.sin(2 * np.pi * 50.0 * t) + (amp * 0.5) * np.sin(2 * np.pi * 150.0 * t)
    pink = (amp * 0.6) * np.random.randn(len(t)).astype(np.float32)
    return (hum + pink).astype(np.float32)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Tests d'initialisation et configuration
# ═══════════════════════════════════════════════════════════════════════════════

class TestNoiseSuppressorInit:
    """Validation de la configuration et des backends."""

    def test_default_initialization(self):
        s = NoiseSuppressor()
        assert s.sr == 16000
        assert s.hop == 160
        assert s.n_fft == 320
        assert s.n_bins == 161
        assert s.engine_type in (DenoiseEngineType.LIBDF_NATIVE, DenoiseEngineType.ZERO_ALLOC_DSP)

    def test_custom_48k_config(self):
        cfg = NoiseSuppressorConfig(sample_rate=48000, frame_ms=10, post_filter_beta=0.08)
        s = NoiseSuppressor(cfg)
        assert s.sr == 48000
        assert s.hop == 480
        assert s.n_fft == 960
        assert s.n_bins == 481

    def test_singleton_getter(self):
        s1 = get_noise_suppressor(sample_rate=16000)
        s2 = get_noise_suppressor(sample_rate=16000)
        assert s1 is s2

    def test_reset_clears_state(self):
        s = NoiseSuppressor()
        chunk = np.random.randn(1024).astype(np.float32)
        s.process(chunk)
        assert s._residual_len > 0
        s.reset()
        assert s._residual_len == 0
        assert np.all(s._in_buf == 0.0)


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Tests du pipeline temps réel à zéro allocation
# ═══════════════════════════════════════════════════════════════════════════════

class TestZeroAllocationPipeline:
    """Vérification des tampons pré-alloués et de l'absence de malloc en régime permanent."""

    def test_buffer_reusability(self):
        s = NoiseSuppressor()
        s.reset()
        buf_id_in = id(s._in_buf)
        buf_id_out = id(s._out_buf)

        chunk = np.zeros(1024, dtype=np.float32)
        for _ in range(50):
            _ = s.process(chunk)

        # Les tampons ne doivent pas être réalloués en régime permanent
        assert id(s._in_buf) == buf_id_in
        assert id(s._out_buf) == buf_id_out

    def test_supported_dtypes_and_shapes(self):
        s = NoiseSuppressor()
        # 1D float32
        f32_1d = np.random.randn(1024).astype(np.float32)
        out_f32 = s.process(f32_1d)
        assert out_f32.dtype == np.float32
        assert out_f32.shape == (1024,)

        # 2D float32 (canal mono)
        f32_2d = f32_1d.reshape(-1, 1)
        out_2d = s.process(f32_2d)
        assert out_2d.shape == (1024, 1)

        # int16 PCM
        i16 = (f32_1d * 32767).astype(np.int16)
        out_i16 = s.process(i16)
        assert out_i16.dtype == np.int16
        assert out_i16.shape == (1024,)

        # bytes PCM16
        b_in = i16.tobytes()
        out_b = s.process(b_in)
        assert isinstance(out_b, bytes)
        assert len(out_b) == len(b_in)

    def test_process_frame_single(self):
        s = NoiseSuppressor()
        frame = np.zeros(160, dtype=np.float32)
        out_frame = np.zeros(160, dtype=np.float32)
        snr_db = s.process_frame(frame, out=out_frame)
        assert isinstance(snr_db, float)
        assert out_frame.shape == (160,)

    def test_invalid_frame_size_raises(self):
        s = NoiseSuppressor()
        bad_frame = np.zeros(100, dtype=np.float32)
        with pytest.raises(ValueError, match="Taille de trame incorrecte"):
            s.process_frame(bad_frame)


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Tests de suppression des bruits non-stationnaires (clavier bleu)
# ═══════════════════════════════════════════════════════════════════════════════

class TestAcousticDenoisingPerformance:
    """Validation de l'efficacité de débruitage sur clavier mécanique et climatiseur."""

    def test_keyboard_click_attenuation_during_silence(self):
        s = NoiseSuppressor()
        s.reset()
        sr = 16000
        # 1 seconde de silence contenant 3 frappes de clavier mécanique
        audio = np.zeros(sr, dtype=np.float32)
        click = make_mechanical_click(sr=sr, amp=0.45)
        click_idx = int(0.3 * sr)
        audio[click_idx : click_idx + len(click)] += click

        cleaned = s.process(audio)

        # Atténuation au pic du clic
        raw_peak = np.max(np.abs(audio[click_idx : click_idx + len(click)]))
        clean_peak = np.max(np.abs(cleaned[click_idx : click_idx + len(click) + 160]))
        attenuation_db = 20 * np.log10(raw_peak / max(clean_peak, 1e-6))

        # Les clics clavier pendant les silences doivent être atténués d'au moins 25 dB
        assert attenuation_db >= 25.0, f"Atténuation insuffisante : {attenuation_db:.2f} dB"

    def test_ac_hum_attenuation(self):
        s = NoiseSuppressor()
        s.reset()
        sr = 16000
        # 1 seconde de climatiseur seul
        ac = make_ac_hum(duration_s=1.0, sr=sr, amp=0.04)
        cleaned = s.process(ac)

        p_raw = np.mean(ac[int(0.2 * sr):] ** 2)
        p_clean = np.mean(cleaned[int(0.2 * sr):] ** 2)
        attenuation_db = 10 * np.log10(p_raw / max(p_clean, 1e-12))

        # Le bruit de fond continu doit être atténué de plus de 15 dB
        assert attenuation_db >= 15.0, f"Atténuation clim insuffisante : {attenuation_db:.2f} dB"

    def test_voice_preservation(self):
        s = NoiseSuppressor()
        s.reset()
        sr = 16000
        # 1 seconde de voix propre sans bruit
        voice = make_vocal_tone(freq_hz=180.0, duration_s=1.0, sr=sr, amp=0.35)
        cleaned = s.process(voice)

        # Vérifier que l'énergie vocale est préservée (perte < 1.0 dB)
        v_in = voice[int(0.3 * sr) : int(0.8 * sr)]
        v_out = cleaned[int(0.3 * sr) : int(0.8 * sr)]
        loss_db = abs(20 * np.log10(np.std(v_in) / max(np.std(v_out), 1e-6)))

        assert loss_db < 1.0, f"Distorsion de la voix trop élevée : {loss_db:.3f} dB"


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Tests de performance et consommation CPU (< 3%)
# ═══════════════════════════════════════════════════════════════════════════════

class TestRealTimePerformance:
    """Validation de la consommation CPU et de la latence algorithmique."""

    def test_cpu_consumption_under_3_percent(self):
        s = NoiseSuppressor(NoiseSuppressorConfig(sample_rate=16000, frame_ms=10))
        sr = 16000
        chunk_size = 1024

        # Échauffement
        warmup = np.random.randn(chunk_size).astype(np.float32)
        for _ in range(20):
            _ = s.process(warmup)

        # Test sur 5 secondes d'audio (environ 78 chunks de 1024)
        n_chunks = int(5.0 * sr / chunk_size)
        data = np.random.randn(chunk_size).astype(np.float32)

        t0 = time.perf_counter()
        for _ in range(n_chunks):
            _ = s.process(data)
        elapsed = time.perf_counter() - t0

        audio_duration = n_chunks * chunk_size / sr
        rtf = elapsed / audio_duration
        cpu_pct = rtf * 100.0

        print(f"\n[Test CPU] Durée audio: {audio_duration:.2f}s | Traité en: {elapsed*1000:.2f}ms | CPU: {cpu_pct:.2f}%")
        assert cpu_pct < 3.0, f"Consommation CPU excessive : {cpu_pct:.2f}% (seuil: 3.0%)"

    def test_algorithmic_latency(self):
        s = NoiseSuppressor(NoiseSuppressorConfig(sample_rate=16000, frame_ms=10))
        # La latence algorithmique pour une trame de 10 ms (160 éch.) à 50% de recouvrement est de 160 échantillons
        assert s.hop == 160
        assert (s.hop / s.sr) * 1000.0 == 10.0


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Tests du Benchmark SNR complet
# ═══════════════════════════════════════════════════════════════════════════════

class TestBenchmarkSNR:
    """Validation de l'exécution du benchmark SNR complet."""

    def test_benchmark_snr_metrics(self):
        res = benchmark_snr(duration_s=2.0, sample_rate=16000, print_report=False)

        assert "snr_in_db" in res
        assert "snr_out_db" in res
        assert "delta_snr_db" in res
        assert "mechanical_click_attenuation_db" in res
        assert "voice_formant_loss_db" in res
        assert "cpu_percent" in res

        # Gains acoustiques attendus
        assert res["delta_snr_db"] > 15.0, f"Gain SNR insuffisant: {res['delta_snr_db']} dB"
        assert res["mechanical_click_attenuation_db"] > 25.0, f"Atténuation clics trop faible: {res['mechanical_click_attenuation_db']} dB"
        assert res["voice_formant_loss_db"] < 0.5, f"Distorsion vocale trop forte: {res['voice_formant_loss_db']} dB"
        assert res["cpu_percent"] < 3.0, f"Consommation CPU trop élevée: {res['cpu_percent']}%"
