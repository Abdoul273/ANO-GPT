"""Comportements visibles du style GÉODÉSIQUE."""

import math

import pytest
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtWidgets import QApplication

from ui.orb import registry
from ui.orb.host import OrbHost
from ui.orb.styles.wireframe import WireframeOrb


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_geodesique_est_selectionnable_et_transparent(qapp):
    assert "wireframe" in {spec.id for spec in registry.selectable_specs()}
    orb = WireframeOrb()
    try:
        image = QImage(500, 500, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0)
        painter = QPainter(image)
        orb.paint_orb(painter, 250, 250, 120, 0.0)
        painter.end()
        assert image.pixelColor(0, 0).alpha() == 0
        assert image.pixelColor(250, 150).alpha() > 0 or any(
            image.pixelColor(x, y).alpha() > 0
            for x in range(150, 350, 3) for y in range(150, 350, 3)
        )
    finally:
        orb.shutdown()
        orb.close()


def test_geodesique_se_contracte_et_deforme_sa_membrane(qapp):
    orb = WireframeOrb()
    try:
        orb.state = "LISTENING"
        for frame in range(30):
            orb.advance(1/30, frame/30)
        listening = sum(math.hypot(x, y) for x, y in
                        zip(orb._screen_x, orb._screen_y))/len(orb._screen_x)

        orb.state = "SPEAKING"
        orb.speaking = True
        orb._volume = .8
        orb._bands[:] = [1.0] * 8
        for frame in range(30):
            orb.advance(1/30, 1+frame/30)
        speaking = sum(math.hypot(x, y) for x, y in
                       zip(orb._screen_x, orb._screen_y))/len(orb._screen_x)
        assert speaking > listening * 1.2

        orb._bands[:] = [0.0] * 8
        for frame in range(30):
            orb.advance(1/30, 2+frame/30)
        quiet = sum(math.hypot(x, y) for x, y in
                    zip(orb._screen_x, orb._screen_y))/len(orb._screen_x)
        assert speaking > quiet * 1.05
    finally:
        orb.shutdown()
        orb.close()


def test_geodesique_recoit_letat_au_changement_de_style(qapp):
    host = OrbHost("", "JARVIS", "pulse")
    host.state = "THINKING"
    host.set_volume(.5)
    host.set_background_image_active(True)
    previous = host.orb
    try:
        assert host.set_style("wireframe")
        assert isinstance(host.orb, WireframeOrb)
        assert host.orb.visual_state == "thinking"
        assert host.orb._target_vol == pytest.approx(.5)
        assert host.orb._background_photo_active
        assert not previous._anim_tmr.isActive()
    finally:
        host.orb.shutdown()
        host.close()
