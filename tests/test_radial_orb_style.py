"""Comportements visibles du style SPECTRE."""

import math

import pytest
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtWidgets import QApplication

from ui.orb import registry
from ui.orb.host import OrbHost
from ui.orb.styles.radial import RadialOrb


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _render(orb: RadialOrb) -> QImage:
    image = QImage(500, 500, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    painter = QPainter(image)
    orb.paint_orb(painter, 250, 250, 110, 3.0)
    painter.end()
    return image


def test_spectre_est_selectionnable_et_garde_le_fond_transparent(qapp):
    assert "radial" in {spec.id for spec in registry.selectable_specs()}
    orb = RadialOrb()
    try:
        image = _render(orb)
        assert image.pixelColor(0, 0).alpha() == 0
        assert any(image.pixelColor(x, y).alpha() > 0
                   for x in range(150, 350, 4) for y in range(150, 350, 4))
    finally:
        orb.shutdown()
        orb.close()


def test_spectre_absorbe_a_lecoute_et_rayonne_pendant_la_parole(qapp):
    orb = RadialOrb()
    try:
        orb._bands[:] = [1.0] * 8
        orb.state = "LISTENING"
        for frame in range(20):
            orb.advance(1 / 30, frame / 30)
        _render(orb)
        listening = sum(math.hypot(x, y) for x, y in zip(orb._x, orb._y)) / len(orb._x)

        orb.state = "SPEAKING"
        orb.speaking = True
        for frame in range(20):
            orb.advance(1 / 30, 1 + frame / 30)
        _render(orb)
        speaking = sum(math.hypot(x, y) for x, y in zip(orb._x, orb._y)) / len(orb._x)
        assert speaking > listening * 1.08
    finally:
        orb.shutdown()
        orb.close()


def test_spectre_souvre_progressivement_avec_la_voix(qapp):
    orb = RadialOrb()
    try:
        orb.state = "SPEAKING"
        orb.speaking = True
        orb._bands[:] = [.15] * 8
        for frame in range(30):
            orb.advance(1 / 30, frame / 30)
        quiet = sum(math.hypot(x, y) for x, y in zip(orb._x, orb._y)) / len(orb._x)

        orb._volume = .85
        orb._bands[:] = [1.0] * 8
        orb.advance(1 / 30, 1.0)
        first_step = sum(math.hypot(x, y) for x, y in zip(orb._x, orb._y)) / len(orb._x)
        for frame in range(1, 30):
            orb.advance(1 / 30, 1 + frame / 30)
        loud = sum(math.hypot(x, y) for x, y in zip(orb._x, orb._y)) / len(orb._x)

        assert quiet < first_step < loud
        assert loud > quiet * 1.12
    finally:
        orb.shutdown()
        orb.close()


def test_spectre_recoit_letat_lors_du_changement_de_style(qapp):
    host = OrbHost("", "JARVIS", "pulse")
    host.state = "LISTENING"
    host.set_volume(.55)
    host.set_background_image_active(True)
    previous = host.orb
    try:
        assert host.set_style("radial")
        assert isinstance(host.orb, RadialOrb)
        assert host.orb.visual_state == "listening"
        assert host.orb._target_vol == pytest.approx(.55)
        assert host.orb._background_photo_active
        assert not previous._anim_tmr.isActive()
    finally:
        host.orb.shutdown()
        host.close()
