"""Comportements visibles du style IRIS."""

import math

import pytest
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtWidgets import QApplication

from ui.orb import registry
from ui.orb.host import OrbHost
from ui.orb.styles.iris import IrisOrb


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _render(orb: IrisOrb) -> QImage:
    image = QImage(500, 500, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    painter = QPainter(image)
    orb.paint_orb(painter, 250, 250, 130, 0.0)
    painter.end()
    return image


def test_iris_est_selectionnable_et_garde_son_ouverture_transparente(qapp):
    assert "iris" in {spec.id for spec in registry.selectable_specs()}
    orb = IrisOrb()
    try:
        image = _render(orb)
        assert image.pixelColor(0, 0).alpha() == 0
        assert image.pixelColor(250, 250).alpha() == 0
        assert any(image.pixelColor(x, y).alpha() > 0
                   for x in range(140, 360, 4) for y in range(140, 360, 4))
    finally:
        orb.shutdown()
        orb.close()


def test_iris_souvre_pendant_la_parole_et_reagit_par_lame(qapp):
    orb = IrisOrb()
    try:
        orb.state = "LISTENING"
        for frame in range(40):
            orb.advance(1/30, frame/30)
        listening = orb._aperture_size

        orb.state = "SPEAKING"
        orb.speaking = True
        orb._volume = .8
        for frame in range(40):
            orb.advance(1/30, 2+frame/30)
        assert orb._aperture_size > listening * 2

        orb._bands[:] = [0.0] * 8
        for frame in range(30):
            orb.advance(1/30, 4+frame/30)
        _render(orb)
        tip = orb._blade_points[0][3]
        baseline = math.hypot(tip.x()-250, tip.y()-250)

        orb._bands[0] = 1.0
        for frame in range(30):
            orb.advance(1/30, 6+frame/30)
        _render(orb)
        tip = orb._blade_points[0][3]
        assert math.hypot(tip.x()-250, tip.y()-250) > baseline + 5
    finally:
        orb.shutdown()
        orb.close()


def test_iris_garde_letat_au_changement_de_style(qapp):
    host = OrbHost("", "JARVIS", "pulse")
    host.state = "THINKING"
    host.set_volume(.5)
    host.set_background_image_active(True)
    previous = host.orb
    try:
        assert host.set_style("iris")
        assert isinstance(host.orb, IrisOrb)
        assert host.orb.visual_state == "thinking"
        assert host.orb._target_vol == pytest.approx(.5)
        assert host.orb._background_photo_active
        assert not previous._anim_tmr.isActive()
    finally:
        host.orb.shutdown()
        host.close()
