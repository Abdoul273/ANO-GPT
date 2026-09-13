"""Tests unitaires et de validation d'écoute pour core/spatial_audio.py.

Vérifie :
1. Calculs trigonométriques 3D (coordonnées écran, sphériques et cartésiennes)
2. Modélisation physique HRTF (ITD Woodworth, ILD Head Shadowing, Encoches Pinna)
3. Traitement dynamique des modes d'Orbe (Mini-Orbe haut-droit vs Plein Écran Holographique)
4. Réverbération spatiale holographique futuriste
5. Lissage des transitions (absence de clics et sauts de phase)
6. Traitement de flux PCM temps réel (continuité et recouvrement Overlap-Add)
7. Génération et validation du template PipeWire filter-chain
8. Génération du fichier audio de démonstration d'écoute binaurale
"""
from __future__ import annotations

import math
from types import SimpleNamespace

import numpy as np
import pytest

from core.spatial_audio import (
    DEFAULT_SAMPLE_RATE,
    HolographicRoomReverb,
    OrbMode,
    PipeWireSpatialChain,
    SOFALoader,
    SpatialAudioProcessor,
    SpatialTransitionSmoother,
    SphericalCoords,
    cartesian_to_spherical,
    compute_head_shadow_coefficients,
    compute_pinna_notch_frequencies,
    compute_woodworth_itd,
    generate_binaural_listening_test,
    generate_parametric_hrir,
    get_preset_coordinates,
    screen_coords_to_spherical,
    spherical_to_cartesian,
    spatial_audio_requested,
)


def test_spatial_audio_auto_casque_et_override(monkeypatch):
    monkeypatch.delenv("ANOGPT_SPATIAL_AUDIO", raising=False)
    assert spatial_audio_requested([SimpleNamespace(
        name="alsa_output.pci", description="Haut-parleurs internes", kind="internal"
    )]) is False
    assert spatial_audio_requested([SimpleNamespace(
        name="bluez_output.1", description="Casque Bluetooth", kind="bluetooth"
    )]) is True
    monkeypatch.setenv("ANOGPT_SPATIAL_AUDIO", "1")
    assert spatial_audio_requested([]) is True
    monkeypatch.setenv("ANOGPT_SPATIAL_AUDIO", "0")
    assert spatial_audio_requested([SimpleNamespace(kind="bluetooth")]) is False


def test_cartesian_and_spherical_conversions():
    # Source droit devant à 1m
    sph = cartesian_to_spherical(0.0, 0.0, 1.0)
    assert pytest.approx(sph.azimuth_deg, abs=1e-3) == 0.0
    assert pytest.approx(sph.elevation_deg, abs=1e-3) == 0.0
    assert pytest.approx(sph.distance_m, abs=1e-3) == 1.0

    # Source à droite (+X = 1m, Z = 1m) -> 45°
    sph_right = cartesian_to_spherical(1.0, 0.0, 1.0)
    assert pytest.approx(sph_right.azimuth_deg, abs=1e-2) == 45.0
    assert pytest.approx(sph_right.elevation_deg, abs=1e-2) == 0.0

    # Source en hauteur (+Y = 1m, horizontal = 1m) -> 45° élévation
    sph_up = cartesian_to_spherical(0.0, 1.0, 1.0)
    assert pytest.approx(sph_up.azimuth_deg, abs=1e-2) == 0.0
    assert pytest.approx(sph_up.elevation_deg, abs=1e-2) == 45.0

    # Aller-retour sphérique -> cartésien -> sphérique
    orig = SphericalCoords(azimuth_deg=35.0, elevation_deg=20.0, distance_m=0.85)
    cart = spherical_to_cartesian(orig)
    back = cartesian_to_spherical(cart.x, cart.y, cart.z)
    assert pytest.approx(back.azimuth_deg, abs=1e-2) == orig.azimuth_deg
    assert pytest.approx(back.elevation_deg, abs=1e-2) == orig.elevation_deg
    assert pytest.approx(back.distance_m, abs=1e-2) == orig.distance_m


def test_screen_coords_to_spherical():
    # Centre de l'écran (u=0.5, v=0.45 = hauteur yeux)
    center = screen_coords_to_spherical(0.5, 0.45)
    assert pytest.approx(center.azimuth_deg, abs=1e-2) == 0.0
    assert pytest.approx(center.elevation_deg, abs=1e-2) == 0.0

    # Mini-Orbe : coin supérieur droit (u=0.90, v=0.10)
    top_right = screen_coords_to_spherical(0.90, 0.10)
    assert top_right.azimuth_deg > 15.0  # Orienté vers la droite
    assert top_right.elevation_deg > 8.0  # Orienté vers le haut

    # Compagnon : coin inférieur droit (u=0.88, v=0.88)
    bottom_right = screen_coords_to_spherical(0.88, 0.88)
    assert bottom_right.azimuth_deg > 15.0
    assert bottom_right.elevation_deg < -5.0  # Orienté vers le bas


def test_orb_presets():
    # Plein écran centré
    full = get_preset_coordinates(OrbMode.FULLSCREEN_CENTER)
    assert full.azimuth_deg == 0.0
    assert full.elevation_deg == 0.0

    # Mini-Orbe haut-droite
    mini = get_preset_coordinates(OrbMode.MINI_ORB_TOP_RIGHT)
    assert 15.0 <= mini.azimuth_deg <= 45.0
    assert 5.0 <= mini.elevation_deg <= 30.0

    # Compagnon bas-droite
    comp = get_preset_coordinates(OrbMode.COMPANION_BOTTOM_RIGHT)
    assert 15.0 <= comp.azimuth_deg <= 45.0
    assert comp.elevation_deg < 0.0


def test_woodworth_itd():
    # 0° d'azimut -> ITD nul
    del_l, del_r, itd = compute_woodworth_itd(0.0)
    assert del_l == 0.0
    assert del_r == 0.0
    assert itd == 0.0

    # +90° à droite : oreille droite ipsilatérale (0 retard), gauche retardée
    del_l_90, del_r_90, itd_90 = compute_woodworth_itd(90.0)
    assert del_r_90 == 0.0
    assert del_l_90 > 0.0
    # Retard théorique maximal : (a/c)*(1 + pi/2) ~ 0.655 ms
    assert pytest.approx(itd_90 * 1000.0, abs=0.05) == 0.655

    # -90° à gauche : oreille gauche ipsilatérale, droite retardée
    del_l_neg90, del_r_neg90, itd_neg90 = compute_woodworth_itd(-90.0)
    assert del_l_neg90 == 0.0
    assert del_r_neg90 > 0.0
    assert pytest.approx(itd_neg90, abs=1e-5) == itd_90

    # Croissance monotone de l'ITD avec l'azimut
    _, _, itd_30 = compute_woodworth_itd(30.0)
    _, _, itd_60 = compute_woodworth_itd(60.0)
    assert 0.0 < itd_30 < itd_60 < itd_90


def test_head_shadow_filtering():
    # Source à droite (+45°)
    (b_l, a_l), (b_r, a_r) = compute_head_shadow_coefficients(45.0)

    # Stabilité des filtres IIR (pôles à l'intérieur du cercle unité)
    assert abs(a_l[1]) < 1.0
    assert abs(a_r[1]) < 1.0

    # Test de réponse fréquentielle : l'oreille gauche (controlatérale / ombre)
    # doit atténuer les hautes fréquences davantage que l'oreille droite
    w, h_l = np.linspace(0.1, math.pi, 64), np.zeros(64, dtype=complex)
    for i, freq in enumerate(w):
        z = np.exp(1j * freq)
        h_l[i] = (b_l[0] + b_l[1] * (z ** -1)) / (a_l[0] + a_l[1] * (z ** -1))

    for i, freq in enumerate(w):
        z = np.exp(1j * freq)
        h_r_val = (b_r[0] + b_r[1] * (z ** -1)) / (a_r[0] + a_r[1] * (z ** -1))
        # À haute fréquence, le côté droit (ipsilatéral) est plus fort que le côté gauche (ombré)
        if freq > math.pi * 0.4:
            assert abs(h_r_val) > abs(h_l[i])


def test_pinna_notch_frequency():
    # Une élévation plus haute (+45°) déplace l'encoche de Pinna vers le haut (> 8 kHz)
    # par rapport à une élévation basse (-30°)
    f_high = compute_pinna_notch_frequencies(45.0)
    f_low = compute_pinna_notch_frequencies(-30.0)
    assert f_high > f_low
    assert 5500.0 <= f_low <= 8000.0
    assert 8000.0 <= f_high <= 10000.0


def test_parametric_hrir_generation():
    coords = SphericalCoords(azimuth_deg=32.0, elevation_deg=18.0, distance_m=0.85)
    ir_l, ir_r = generate_parametric_hrir(coords, sample_rate=24000, ir_length=128)

    assert len(ir_l) == 128
    assert len(ir_r) == 128
    assert not np.any(np.isnan(ir_l))
    assert not np.any(np.isnan(ir_r))
    assert not np.any(np.isinf(ir_l))
    assert not np.any(np.isinf(ir_r))

    # Énergie non nulle
    assert np.sum(ir_l ** 2) > 1e-4
    assert np.sum(ir_r ** 2) > 1e-4

    # Source à droite (+32°) : pic de l'oreille droite plus précoce ou plus net que gauche
    peak_l = np.argmax(np.abs(ir_l))
    peak_r = np.argmax(np.abs(ir_r))
    assert peak_r <= peak_l


def test_holographic_reverb():
    reverb = HolographicRoomReverb(sample_rate=24000)

    # Signal impulsionnel mono
    test_sig = np.zeros(1024, dtype=np.float32)
    test_sig[0] = 1.0

    out_l, out_r = reverb.process(test_sig, wet_mix=0.30, stereo_widening=0.40)

    assert len(out_l) == 1024
    assert len(out_r) == 1024
    assert not np.any(np.isnan(out_l))
    assert not np.any(np.isnan(out_r))

    # Élargissement stéréo : les deux canaux ne sont pas strictement identiques
    diff = np.max(np.abs(out_l - out_r))
    assert diff > 0.01

    # Présence de réverbération dans le temps après l'impulsion (échantillons > 100)
    tail_energy = np.sum(out_l[100:] ** 2) + np.sum(out_r[100:] ** 2)
    assert tail_energy > 1e-4


def test_spatial_smoother():
    smoother = SpatialTransitionSmoother(time_constant_sec=0.08, sample_rate=24000)
    smoother.set_target(SphericalCoords(0.0, 0.0, 0.70), reverb_wet=0.20, snap_immediately=True)

    # Nouvelle cible : Mini-Orbe en haut à droite (32°, 18°)
    smoother.set_target(SphericalCoords(32.0, 18.0, 0.85), reverb_wet=0.05)

    # Avance d'un bloc (512 échantillons = ~21.3 ms)
    coords_step1, wet_step1 = smoother.step(512)
    # Doit avoir commencé à bouger sans sauter directement à 32°
    assert 0.0 < coords_step1.azimuth_deg < 32.0
    assert 0.0 < coords_step1.elevation_deg < 18.0
    assert 0.05 < wet_step1 < 0.20

    # Après plusieurs pas (~250 ms), converge vers la cible
    for _ in range(15):
        coords_final, wet_final = smoother.step(512)

    assert pytest.approx(coords_final.azimuth_deg, abs=1.0) == 32.0
    assert pytest.approx(coords_final.elevation_deg, abs=1.0) == 18.0
    assert pytest.approx(wet_final, abs=0.02) == 0.05


def test_realtime_chunk_processing():
    processor = SpatialAudioProcessor(sample_rate=24000, default_mode=OrbMode.MINI_ORB_TOP_RIGHT)

    # Signal sinusoïdal 440 Hz int16 mono (1024 échantillons)
    t = np.linspace(0, 1024 / 24000.0, 1024, endpoint=False)
    sine = (np.sin(2 * np.pi * 440 * t) * 16000).astype(np.int16)
    chunk_bytes = sine.tobytes()

    # Traitement
    out_bytes = processor.process_chunk(chunk_bytes, input_channels=1)

    # Entrée 1024 mono int16 (2048 octets) -> Sortie 1024 stéréo int16 (4096 octets)
    assert len(out_bytes) == len(chunk_bytes) * 2

    stereo = np.frombuffer(out_bytes, dtype=np.int16).reshape(-1, 2)
    assert stereo.shape == (1024, 2)
    assert not np.any(np.isnan(stereo))

    # Continuité inter-blocs : un second bloc ne doit pas avoir de saut/clic
    out_bytes_2 = processor.process_chunk(chunk_bytes, input_channels=1)
    stereo_2 = np.frombuffer(out_bytes_2, dtype=np.int16).reshape(-1, 2)
    # L'énergie du second bloc doit être du même ordre de grandeur
    rms_1 = np.sqrt(np.mean(stereo.astype(float) ** 2))
    rms_2 = np.sqrt(np.mean(stereo_2.astype(float) ** 2))
    assert abs(rms_1 - rms_2) / max(1.0, rms_1) < 0.35


def test_mode_switch_smoothness():
    processor = SpatialAudioProcessor(sample_rate=24000, default_mode=OrbMode.FULLSCREEN_CENTER)

    # Génération d'un signal continu de test
    chunk = (np.ones(512, dtype=np.float32) * 0.3 * 32767.0).astype(np.int16).tobytes()

    # Traiter en plein écran
    processor.process_chunk(chunk)

    # Bascule vers Mini-Orbe
    processor.set_orb_mode(OrbMode.MINI_ORB_TOP_RIGHT)
    assert processor.current_mode == OrbMode.MINI_ORB_TOP_RIGHT

    # Traiter immédiatement après la bascule : aucun crash ni NaN
    out_trans = processor.process_chunk(chunk)
    stereo_trans = np.frombuffer(out_trans, dtype=np.int16).reshape(-1, 2)
    assert not np.any(np.isnan(stereo_trans))
    assert np.max(np.abs(stereo_trans)) > 100


def test_pipewire_filter_chain_template_creation(tmp_path):
    conf_file = tmp_path / "pipewire-test.conf"
    hrir_file = tmp_path / "test_hrir.wav"

    # Export d'une impulsion test
    coords = SphericalCoords(30.0, 15.0, 0.8)
    ir_l, ir_r = generate_parametric_hrir(coords)
    assert SOFALoader.export_hrir_wav(hrir_file, ir_l, ir_r)
    assert hrir_file.exists()

    # Génération de la configuration PipeWire
    generated_conf = PipeWireSpatialChain.create_template_config(
        output_conf_path=conf_file,
        hrir_wav_path=hrir_file,
    )
    assert generated_conf.exists()
    content = generated_conf.read_text(encoding="utf-8")

    # Vérification des balises PipeWire indispensables
    assert "libpipewire-module-filter-chain" in content
    assert "convolver" in content
    assert "anogpt_spatial_sink" in content
    assert str(hrir_file.resolve()) in content


def test_binaural_listening_demo_generation(tmp_path):
    test_wav = tmp_path / "binaural_demo.wav"
    out_path = generate_binaural_listening_test(output_path=test_wav, duration_s=1.0)

    assert out_path.exists()
    assert out_path.stat().st_size > 4000  # Vérifie que le WAV contient des données

    # Lecture pour vérifier le format stéréo
    ir_l, ir_r, sr = SOFALoader.load_hrir_wav(out_path)
    assert sr == DEFAULT_SAMPLE_RATE
    assert len(ir_l) == len(ir_r)
    assert len(ir_l) == int(DEFAULT_SAMPLE_RATE * 1.0)
    assert np.max(np.abs(ir_l)) > 0.01
    assert np.max(np.abs(ir_r)) > 0.01
