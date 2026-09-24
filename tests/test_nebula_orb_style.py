"""Comportements visibles du style NEBULA."""

import math

import pytest
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtWidgets import QApplication

from ui.orb import registry
from ui.orb.host import OrbHost
from ui.orb.styles.nebula import NebulaOrb


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_nebula_est_selectionnable_et_transparente(qapp):
    assert "nebula" in {spec.id for spec in registry.selectable_specs()}
    orb = NebulaOrb()
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


def test_nebula_ouvre_ses_bras_avec_la_voix(qapp):
    orb = NebulaOrb()
    try:
        orb.state = "THINKING"
        for frame in range(40):
            orb.advance(1/30, frame/30)
        thinking = sum(math.hypot(x, y) for x, y in zip(orb._x, orb._y))/len(orb._x)

        orb.state = "SPEAKING"
        orb.speaking = True
        orb._volume = .8
        for frame in range(40):
            orb.advance(1/30, 2+frame/30)
        speaking = sum(math.hypot(x, y) for x, y in zip(orb._x, orb._y))/len(orb._x)
        assert speaking > thinking * 1.25
    finally:
        orb.shutdown()
        orb.close()


def test_les_grains_circulent_separement_et_accelerent_avec_la_voix(qapp):
    orb = NebulaOrb()
    try:
        a, b = 50, 51

        def separation() -> float:
            return (orb._grain_phases[a] - orb._grain_phases[b]) % math.tau

        def change(before: float, after: float) -> float:
            return abs((after-before+math.pi) % math.tau-math.pi)

        before = separation()
        for frame in range(45):
            orb.advance(1/30, frame/30)
        idle_change = change(before, separation())
        rotation = orb._rotation

        orb._volume = .8
        before = separation()
        for frame in range(45):
            orb.advance(1/30, 2+frame/30)
        voice_change = change(before, separation())

        assert idle_change > .1
        assert voice_change > idle_change * 1.5
        assert orb._rotation > rotation
    finally:
        orb.shutdown()
        orb.close()


def test_nebula_recoit_letat_au_changement_de_style(qapp):
    host = OrbHost("", "JARVIS", "pulse")
    host.state = "LISTENING"
    host.set_volume(.45)
    host.set_background_image_active(True)
    previous = host.orb
    try:
        assert host.set_style("nebula")
        assert isinstance(host.orb, NebulaOrb)
        assert host.orb.visual_state == "listening"
        assert host.orb._target_vol == pytest.approx(.45)
        assert host.orb._background_photo_active
        assert not previous._anim_tmr.isActive()
    finally:
        host.orb.shutdown()
        host.close()
