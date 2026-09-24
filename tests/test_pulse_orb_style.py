"""Comportements visibles du style PULSE."""

import pytest
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtWidgets import QApplication

from ui.orb import registry
from ui.orb.host import OrbHost
from ui.orb.styles.pulse import PulseOrb


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_pulse_est_selectionnable_et_transparent(qapp):
    assert "pulse" in {spec.id for spec in registry.selectable_specs()}
    orb = PulseOrb()
    try:
        image = QImage(500, 500, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0)
        painter = QPainter(image)
        orb.paint_orb(painter, 250, 250, 130, 0.0)
        painter.end()
        assert image.pixelColor(0, 0).alpha() == 0
        assert any(image.pixelColor(x, y).alpha() > 0
                   for x in range(150, 350, 4) for y in range(150, 350, 4))
    finally:
        orb.shutdown()
        orb.close()


def test_membrane_se_deforme_selon_les_bandes_audio(qapp):
    orb = PulseOrb()
    try:
        orb.state = "SPEAKING"
        orb.speaking = True
        for frame in range(35):
            orb.advance(1/30, frame/30)
        calm = max(orb._radii)-min(orb._radii)

        orb._bands[:] = [1.0, 0.0, .8, 0.0, 1.0, 0.0, .8, 0.0]
        for frame in range(35):
            orb.advance(1/30, 2+frame/30)
        active = max(orb._radii)-min(orb._radii)
        assert active > calm * 3
    finally:
        orb.shutdown()
        orb.close()


def test_attaque_vocale_lance_un_echo_sans_figer_les_ondes(qapp):
    orb = PulseOrb()
    try:
        orb.state = "SPEAKING"
        orb.speaking = True
        orb._volume = .5
        orb.advance(1/30, 0.0)
        assert orb._impact > 0
        assert orb._echo_cursor == 1
        launched = orb._echoes[0]
        for frame in range(15):
            orb.advance(1/30, frame/30)
        assert orb._echoes[0] > launched
        assert orb._phase > 0
    finally:
        orb.shutdown()
        orb.close()


def test_pulse_garde_letat_au_changement_de_style(qapp):
    host = OrbHost("", "JARVIS", "nebula")
    host.state = "LISTENING"
    host.set_volume(.45)
    previous = host.orb
    try:
        assert host.set_style("pulse")
        assert isinstance(host.orb, PulseOrb)
        assert host.orb.visual_state == "listening"
        assert host.orb._target_vol == pytest.approx(.45)
        assert not previous._anim_tmr.isActive()
    finally:
        host.orb.shutdown()
        host.close()
