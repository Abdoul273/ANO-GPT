#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_radial_waveform.py — Tests unitaires et d'intégration du visualiseur spectral circulaire.
"""

import numpy as np
import pytest

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QImage, QPainter
from PyQt6.QtWidgets import QApplication

from ui.orb.radial_waveform import (
    AudioSource,
    BiDirectionalAudioBridge,
    CircularFFTEngine,
    ParticleSystem,
    RadialWaveformRenderer,
    RadialWaveformWidget,
    SpectralBenchmarks,
)


@pytest.fixture(scope="session")
def qapp():
    """Initialise l'application Qt offscreen pour l'ensemble des tests."""
    app = QApplication.instance()
    if app is None:
        app = QApplication(["--platform", "offscreen"])
    return app


# ── 1. TESTS DE L'ANALYSE SPECTRALE FFT & DU LISSAGE TEMPOREL ────────────────

def test_fft_engine_initialization():
    """Vérifie la configuration des bandes 64 et 128 et les bornes 20Hz-20kHz."""
    engine64 = CircularFFTEngine(num_bands=64, min_freq=20.0, max_freq=20000.0)
    assert engine64.num_bands == 64
    assert engine64.band_center_freqs[0] >= 20.0
    assert engine64.band_center_freqs[-1] <= 20000.0

    engine128 = CircularFFTEngine(num_bands=128, min_freq=20.0, max_freq=20000.0)
    assert engine128.num_bands == 128
    assert engine128.band_center_freqs[0] < engine128.band_center_freqs[-1]


def test_fft_engine_frequency_selectivity():
    """Vérifie qu'une onde sinusoïdale pure excite la bande fréquentielle correspondante."""
    engine = CircularFFTEngine(num_bands=64, sample_rate=44100, fft_size=2048)
    sr = 44100
    t = np.linspace(0, 2048 / sr, 2048, endpoint=False)

    # 1. Fréquence pure basse : 60 Hz (Sub/Bass)
    bass_tone = np.sin(2 * np.pi * 60.0 * t).astype(np.float32)
    bands_bass, _, _, _ = engine.process(bass_tone, dt=0.016)
    max_bass_band = int(np.argmax(bands_bass))
    assert engine.band_center_freqs[max_bass_band] < 250.0

    # 2. Fréquence pure médium vocal : 1000 Hz (Mids)
    engine2 = CircularFFTEngine(num_bands=64, sample_rate=44100, fft_size=2048)
    mid_tone = np.sin(2 * np.pi * 1000.0 * t).astype(np.float32)
    bands_mid, _, _, _ = engine2.process(mid_tone, dt=0.016)
    max_mid_band = int(np.argmax(bands_mid))
    assert 250.0 <= engine2.band_center_freqs[max_mid_band] <= 4000.0

    # 3. Fréquence pure aigu : 8000 Hz (Highs)
    engine3 = CircularFFTEngine(num_bands=64, sample_rate=44100, fft_size=2048)
    high_tone = np.sin(2 * np.pi * 8000.0 * t).astype(np.float32)
    bands_high, _, _, _ = engine3.process(high_tone, dt=0.016)
    max_high_band = int(np.argmax(bands_high))
    assert engine3.band_center_freqs[max_high_band] > 4000.0


def test_asymmetric_temporal_smoothing_attack_vs_decay():
    """Valide rigoureusement le lissage asymétrique : Attack 10ms vs Decay 150ms."""
    engine = CircularFFTEngine(num_bands=64)
    dt = 0.016  # 16 ms (trame à 60 FPS)

    # Signal audio riche (bruit blanc centré)
    np.random.seed(42)
    burst = (np.random.randn(engine.fft_size) * 0.7).astype(np.float32)

    # 1. Échelon montant (Attack) : doit monter très rapidement (tau = 10ms)
    # alpha_attack = 1 - exp(-16/10) = 1 - 0.2018 = 0.798 (environ 80% dès la 1ère trame)
    bands_1, _, _, _ = engine.process(burst, dt=dt)
    val_after_attack = np.mean(bands_1)
    assert val_after_attack > 0.45, "L'attaque à 10ms doit répondre immédiatement"

    # Stabilisation
    for _ in range(10):
        engine.process(burst, dt=dt)
    peak_val = np.mean(engine._smoothed_bands)

    # 2. Échelon descendant (Decay) : doit descendre très doucement (tau = 150ms)
    # alpha_decay = 1 - exp(-16/150) = 1 - 0.8988 = 0.101 (environ 10% de descente par trame)
    silence = np.zeros(engine.fft_size, dtype=np.float32)
    bands_drop1, _, _, _ = engine.process(silence, dt=dt)
    val_drop1 = np.mean(bands_drop1)

    # La retombée après 1 trame doit conserver ~90% de l'amplitude
    ratio_retained = val_drop1 / max(1e-4, peak_val)
    assert 0.80 <= ratio_retained <= 0.95, (
        f"Le decay à 150ms doit être doux : ratio retenu {ratio_retained:.3f} attendu ~0.90"
    )


def test_vocal_transient_detection():
    """Vérifie la détection d'attaque vocale dans la bande des formants (300-3400 Hz)."""
    engine = CircularFFTEngine(num_bands=64)
    dt = 0.016
    silence = np.zeros(engine.fft_size, dtype=np.float32)

    # Établir le plancher de bruit
    for _ in range(5):
        engine.process(silence, dt=dt)

    # Attaque vocale soudaine à 800 Hz
    t = np.linspace(0, 2048 / 44100, 2048, endpoint=False)
    voice_onset = np.sin(2 * np.pi * 800.0 * t).astype(np.float32) * 0.9
    _, _, peak_detected, intensity = engine.process(voice_onset, dt=dt)

    assert peak_detected is True, "Une brusque augmentation d'énergie vocale doit déclencher le transitoire"
    assert intensity > 0.2, "L'intensité du pic vocal doit être significative"


# ── 2. TESTS DU DÉGRADÉ DE COULEUR & RENDU GRAPHIQUE CIRCULAIRE ─────────────

def test_spectral_color_gradient():
    """Valide les teintes requises : bleu profond (basses), cyan (médiums), magenta (aigus)."""
    renderer = RadialWaveformRenderer(num_bands=64)
    colors = renderer.band_colors

    # Basses (indice 0 à ~10) : Dominance bleue
    c_bass = colors[0]
    assert c_bass.blue() > 160 and c_bass.red() < 80, "Les basses doivent être bleu profond"

    # Médiums (centre, indice ~30 à 40) : Dominance cyan (vert + bleu élevés, rouge bas)
    c_mid = colors[35]
    assert c_mid.green() > 180 and c_mid.blue() > 180 and c_mid.red() < 80, (
        "Les médiums doivent être cyan électrique"
    )

    # Aigus (derniers indices, ~60 à 63) : Dominance magenta (rouge + bleu élevés, vert bas)
    c_high = colors[-1]
    assert c_high.red() > 180 and c_high.blue() > 120 and c_high.green() < 80, (
        "Les aigus doivent être magenta incandescent"
    )


def test_renderer_drawing(qapp):
    """Vérifie que le moteur de rendu s'exécute sur QPainter sans exception."""
    renderer = RadialWaveformRenderer(num_bands=64, layout="symmetric")
    bands = np.linspace(0.1, 0.9, 64).astype(np.float32)
    caps = bands * 1.1

    canvas = QImage(400, 400, QImage.Format.Format_ARGB32_Premultiplied)
    canvas.fill(Qt.GlobalColor.transparent)

    p = QPainter(canvas)
    renderer.render(
        painter=p,
        width=400,
        height=400,
        smoothed_bands=bands,
        peak_caps=caps,
        show_blur=True,
        show_caps=True,
    )
    p.end()

    # Vérifier que le canevas n'est pas vide (des pixels ont été peints)
    non_zero = False
    for y in range(100, 300, 20):
        for x in range(100, 300, 20):
            if canvas.pixelColor(x, y).alpha() > 0:
                non_zero = True
                break
        if non_zero:
            break
    assert non_zero, "Le rendu radial doit tracer des éléments visibles sur le canevas"


# ── 3. TESTS DE DYNAMIQUE DES PARTICULES ─────────────────────────────────────

def test_particle_system_lifecycle():
    """Vérifie l'éjection, le freinage et l'expiration des particules lumineuses."""
    ps = ParticleSystem(max_particles=50)
    vecs = [(1.0, 0.0), (0.0, 1.0)]
    cols = [QColor(0, 240, 255), QColor(255, 0, 150)]

    ps.spawn_burst(cx=100.0, cy=100.0, radius=50.0, unit_vectors=vecs, colors=cols, intensity=1.0)
    assert len(ps.particles) > 0, "Les particules doivent être instanciées lors d'un burst"

    p0 = ps.particles[0]
    initial_vx = p0.vx

    # Mise à jour avec friction aérodynamique
    ps.update(dt=0.1)
    assert abs(p0.vx) < abs(initial_vx), "La vitesse de la particule doit décroître (drag)"

    # Écoulement du temps jusqu'à extinction
    ps.update(dt=1.5)
    assert len(ps.particles) == 0, "Toutes les particules doivent s'éteindre après leur durée de vie"


# ── 4. TESTS DE SYNCHRONISATION BI-DIRECTIONNELLE ─────────────────────────────

def test_bidirectional_audio_bridge():
    """Valide l'ingestion conjointe de la capture (micro) et de l'émission (TTS/MPV)."""
    bridge = BiDirectionalAudioBridge(target_sr=44100)

    # 1. Envoi audio capturé (voix utilisateur 16 kHz)
    mic_chunk = (np.sin(np.linspace(0, 10, 512)) * 30000).astype(np.int16).tobytes()
    bridge.feed_captured(mic_chunk, sample_rate=16000)

    assert bridge.is_captured_active is True
    assert bridge.is_emitted_active is False
    assert bridge.get_active_source_label() == "USER VOICE"

    # 2. Envoi audio émis (musique MPV 44.1 kHz)
    mpv_chunk = (np.cos(np.linspace(0, 10, 1024)) * 25000).astype(np.int16).tobytes()
    bridge.feed_emitted(mpv_chunk, sample_rate=44100)

    assert bridge.is_captured_active is True
    assert bridge.is_emitted_active is True
    assert bridge.get_active_source_label() == "DUAL (VOICE + MPV)"

    # 3. Extraction d'un bloc combiné pour analyse FFT
    samples, source = bridge.get_samples_for_analysis(2048)
    assert len(samples) == 2048
    assert source == AudioSource.COMBINED


# ── 5. TESTS DU WIDGET QT & DES BENCHMARKS ───────────────────────────────────

def test_radial_waveform_widget_instantiation(qapp):
    """Vérifie le cycle de vie du RadialWaveformWidget PyQt6."""
    widget = RadialWaveformWidget(num_bands=64, layout="symmetric")
    widget.resize(300, 300)
    assert widget.num_bands == 64

    # Changement dynamique de résolution de bandes
    widget.set_bands(128)
    assert widget.num_bands == 128

    # Injection audio test
    test_pcm = np.random.randn(1024).astype(np.float32)
    widget.feed_captured_audio(test_pcm, sample_rate=16000)
    widget.feed_emitted_audio(test_pcm, sample_rate=24000)

    # Exécution manuelle d'un tick d'animation
    widget._on_tick()
    assert len(widget._current_bands) == 128

    widget.close()


def test_benchmarks_execution(qapp):
    """Exécute la suite de benchmarks pour garantir l'absence de régression de performance."""
    results = SpectralBenchmarks.run_all(iterations=30)
    assert "fft_filterbank_64_us" in results
    assert "fft_filterbank_128_us" in results
    assert "render_64_ms" in results
    assert "render_128_ms" in results

    # Le traitement DSP (FFT + lissage) doit être sous la milliseconde
    assert results["fft_filterbank_64_us"] < 2500.0
    assert results["fft_filterbank_128_us"] < 2500.0
