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
        assert orb._PARTICLE_N == 240
        # Points plus larges : le nuage reste dense à l'œil sans monter le
        # nombre de particules, seul levier qui coûte vraiment du CPU.
        assert orb._PARTICLE_POINT_SCALE == 2.20
        assert orb._IDLE_PARTICLE_BUDGET == 160
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
    assert "_blit_glow" not in neural
    # Les filaments ont été retirés : leur tracé par paire mangeait le CPU que
    # la voix partage avec l'interface. Le nuage seul porte la scène.
    assert "_draw_particle_cloud" in neural
    assert "_draw_orbit_rings" not in neural
    assert "_draw_projector_base" not in paint
    assert "_draw_light_column" not in paint
    assert "pmc[\"ring\"]" not in paint


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
