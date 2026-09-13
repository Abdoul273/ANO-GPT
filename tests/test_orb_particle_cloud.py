"""Contrats du volume de particules de l'orbe QPainter."""
from __future__ import annotations

import pytest

from PyQt6.QtWidgets import QApplication

from ui.orb.arc_core import HudCanvas


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_le_corps_est_un_nuage_dense_de_particules_borne(qapp):
    orb = HudCanvas("")
    try:
        assert len(orb._particles) == orb._PARTICLE_N
        assert orb._PARTICLE_N == 300
        # Les points restent larges, avec un budget dense mais borné pour ne
        # pas voler le temps CPU nécessaire à la voix.
        assert orb._PARTICLE_POINT_SCALE == 2.20
        assert orb._IDLE_PARTICLE_BUDGET == 300
        assert orb._ACTIVE_PARTICLE_BUDGET == 240
        assert all(0.0 < pt["home_r"] <= 1.0 for pt in orb._particles)
        assert all({"x", "y", "z", "vx", "vy", "vz", "phase", "form_phase", "form_u", "lane"} <= pt.keys()
                   for pt in orb._particles)
    finally:
        orb.close()


def test_les_filaments_sont_brefs_et_bornes_par_la_proximite(qapp):
    orb = HudCanvas("")
    try:
        orb._cloud_live[4] = 1.0
        orb._update_particle_links()
        assert orb._MAX_LINKS_PER_PARTICLE == 1
        assert 0 < len(orb._particle_links) <= orb._FILAMENT_BUDGET
        assert all(0.0 < closeness <= 1.0 for _, _, closeness, _ in orb._particle_links)
    finally:
        orb.close()


def test_la_parole_declenche_un_sursaut_periodique_sans_pic_audio(qapp):
    orb = HudCanvas("")
    try:
        orb.state = "SPEAKING"
        orb._next_speaking_surge = 1.0
        orb._bass = orb._last_bass = 0.0
        orb._update_particles(0.02, 1.01)
        assert orb._cloud_pulse > 0.0
        assert orb._next_speaking_surge > 2.2
    finally:
        orb.close()


def test_le_nuage_ne_cree_pas_de_cage_filaire(qapp):
    orb = HudCanvas("")
    try:
        orb._update_particle_links()
        assert len(orb._particle_links) <= orb._FILAMENT_BUDGET
        assert all(first != second for first, second, _, _ in orb._particle_links)
        assert orb._sparks == []
    finally:
        orb.close()


def test_le_nuage_reste_spherique_comme_la_reference_threejs(qapp):
    orb = HudCanvas("")
    try:
        assert orb._FORMATION_NAMES == ("sphere",)
        assert set(orb._STATE_FORMATION.values()) == {"sphere"}
    finally:
        orb.close()


def test_les_etats_modulent_la_densite_et_le_rythme_sans_changer_de_forme(qapp):
    orb = HudCanvas("")
    try:
        assert orb._STATE_FORMATION["listening"] == "sphere"
        assert orb._STATE_FORMATION["thinking"] == "sphere"
        assert orb._STATE_FORMATION["speaking"] == "sphere"
        orb.state = "LISTENING"
        orb._volume = orb._bass = orb._mid = orb._treble = 0.8
        orb._update_particles(0.02, 1.0)
        assert orb._last_motion_drive >= 0.8
    finally:
        orb.close()


def test_lorbe_compose_une_scene_holographique(qapp):
    import inspect
    neural = inspect.getsource(HudCanvas._draw_neural_orb)
    paint = inspect.getsource(HudCanvas.paintEvent)
    # Le nuage reste le corps de l'orbe, confiné dans la sphère.
    assert "_draw_particle_cloud" in neural
    assert "setClipPath" in neural
    # Autour : réticule, spectre, noyau, limbe et ondes vocales. Les fils
    # elliptiques orbitaux ont été retirés ; les indicateurs restent dessinés.
    for layer in ("_draw_reticle", "_draw_spectrum", "_draw_nucleus",
                  "_draw_limb", "_draw_shockwaves", "_draw_neon_eye_indicator",
                  "_draw_holographic_gesture_badge"):
        assert layer in paint, layer
    assert "_draw_rings" not in paint
    # Le fond et l'aura sont cuits : un seul blit, jamais un dégradé plein cadre.
    assert 'self._pm["scene"]' in paint
    assert "QRadialGradient" not in paint


def test_la_physique_ne_depend_pas_de_la_cadence(qapp):
    """Un même laps de temps simulé donne la même position, à 20 ou 50 Hz."""
    import copy
    slow = HudCanvas("")
    fast = HudCanvas("")
    try:
        fast._particles = copy.deepcopy(slow._particles)
        slow.state = fast.state = "LISTENING"
        for step in range(20):
            slow._update_particles(0.05, step * 0.05)
        for step in range(50):
            fast._update_particles(0.02, step * 0.02)
        drift = max(
            abs(a["x"] - b["x"]) + abs(a["y"] - b["y"]) + abs(a["z"] - b["z"])
            for a, b in zip(slow._particles[:160], fast._particles[:160])
        )
        assert drift < 0.08
    finally:
        slow.close()
        fast.close()


def test_le_tick_mesure_un_dt_reel_et_borne(qapp):
    import time
    orb = HudCanvas("")
    try:
        orb.show()
        qapp.processEvents()
        orb._last_tick = time.monotonic() - 5.0  # pause longue : pas de saut
        yaw = orb._yaw
        orb._tick_frame()
        assert abs(orb._yaw - yaw) < 0.2
    finally:
        orb.close()


def test_la_cadence_reste_bornee_et_cede_a_la_voix(qapp):
    orb = HudCanvas("")
    try:
        # Caché, l'orbe ne garde qu'un battement lent pour la bulle compagnon.
        assert orb._desired_interval() == orb._FRAME_MS_SLEEP
        orb.show()
        qapp.processEvents()
        orb._on_battery = False
        orb._sim_ms = orb._paint_ms = 1.0
        assert orb._desired_interval() == orb._FRAME_MS
        orb.state = "SPEAKING"
        assert orb._desired_interval() == orb._FRAME_MS_VOICE
        # Pendant la voix, un coût modéré force tout de suite 60 ms : la
        # moyenne glissée du régulateur mettait plusieurs images à réagir.
        orb._paint_ms = 12.0
        assert orb._desired_interval() == orb._FRAME_MS_VOICE_HEAVY
        orb._paint_ms = 60.0
        assert orb._desired_interval() == orb._FRAME_MS_MAX
        orb.set_low_power(True)
        assert orb._desired_interval() == orb._FRAME_MS_SLEEP
    finally:
        orb.close()


def test_le_retard_de_la_boucle_audio_ralentit_lorbe(qapp, monkeypatch):
    from core import freeze_watch
    orb = HudCanvas("")
    try:
        orb.show()
        qapp.processEvents()
        orb._on_battery = False
        orb._sim_ms = orb._paint_ms = 1.0
        assert orb._desired_interval() == orb._FRAME_MS
        monkeypatch.setattr(freeze_watch, "lag", lambda name: 1.2)
        assert orb._desired_interval() == orb._FRAME_MS_MAX
        # Le battement revient : l'orbe garde la cadence minimale 2 s.
        monkeypatch.setattr(freeze_watch, "lag", lambda name: 0.0)
        assert orb._desired_interval() == orb._FRAME_MS_MAX
        orb._throttle_until = 0.0
        assert orb._desired_interval() == orb._FRAME_MS
    finally:
        orb.close()


def test_les_bandes_fft_invalides_ne_cassent_pas_lorbe(qapp):
    orb = HudCanvas("")
    try:
        orb.set_audio_bands([float("nan"), "x", 2.0, -1.0])
        assert orb._audio_bands == [0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        orb.state = "SLEEPING"
        assert orb._ws == "idle"
        orb.state = "processing"
        assert orb._ws == "thinking"
    finally:
        orb.close()


def test_la_photo_de_fond_reste_visible_derriere_lorbe(qapp):
    orb = HudCanvas("")
    try:
        orb.resize(400, 400)
        orb.set_background_image_active(True)
        orb._build_cache(400, 400)
        corner = orb._pm["scene"].toImage().pixelColor(4, 4)
        assert corner.alpha() < 255
        orb.set_background_image_active(False)
        orb._build_cache(400, 400)
        assert orb._pm["scene"].toImage().pixelColor(4, 4).alpha() == 255
    finally:
        orb.close()


def test_lorbe_garde_un_fond_sombre_derriere_les_photons(qapp):
    orb = HudCanvas("")
    try:
        orb.resize(640, 640)
        orb._energy = 0.75
        orb._volume = 0.35
        orb.show()
        qapp.processEvents()
        orb.repaint()
        qapp.processEvents()
        image = orb.grab().toImage()
        cx, cy = image.width() // 2, image.height() // 2

        def luma(x: int, y: int) -> int:
            color = image.pixelColor(x, y)
            return color.red() + color.green() + color.blue()

        photons = max(
            luma(cx + dx, cy + dy)
            for dx in range(-140, 141, 12)
            for dy in range(-140, 141, 12)
        )
        corner = luma(10, 10)
        assert corner <= 20
        assert photons > corner + 30
    finally:
        orb.close()


def test_horloge_de_particules_prepare_les_chiffres_actuels(qapp):
    orb = HudCanvas("")
    try:
        orb.show_clock_particles(duration=4)
        assert len(orb._clock_digits) == 4
        assert orb._CLOCK_PARTICLE_BUDGET == 240
        assert orb._clock_digits.isdigit()
        assert orb._clock_segments
        assert orb._clock_particles_until > 0
    finally:
        orb.close()
