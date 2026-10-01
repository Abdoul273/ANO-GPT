"""Le visage doit rester animé pendant la voix, même micro coupé ou sans focus."""

import os
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtWidgets import QApplication

from ui.orb.contract import visual_state
from ui.orb.host import OrbHost
from ui.window.media_host import MediaHostMixin


@pytest.mark.parametrize("state,speaking,muted,expected", [
    ("SPEAKING", True, True, "speaking"),
    ("SPEAKING", False, True, "speaking"),
    ("THINKING", True, True, "speaking"),
    ("LISTENING", False, True, "idle"),
    ("THINKING", False, True, "idle"),
    ("LISTENING", False, False, "listening"),
])
def test_microphone_mute_does_not_hide_output_speech(state, speaking, muted, expected):
    assert visual_state(state, speaking, muted) == expected


def test_muted_portrait_articulates_real_audio_and_renders(qapp):
    host = OrbHost("", "ANO", "portrait")
    host.resize(320, 320)
    host.show()
    qapp.processEvents()
    orb = host.orb

    def render():
        image = QImage(320, 320, QImage.Format.Format_RGBA8888_Premultiplied)
        image.fill(0)
        painter = QPainter(image)
        try:
            orb.paint_orb(painter, 160, 160, 110, orb._t)
        finally:
            painter.end()
        return np.frombuffer(image.bits().asstring(image.sizeInBytes()), dtype=np.uint8).copy()

    try:
        idle = render()
        host.muted = True
        host.state = "SPEAKING"
        host.speaking = True
        for frame in range(10):
            host.set_volume(.65)
            orb._step_common(.04, time.monotonic())
            orb.advance(.04, (frame + 1) * .04)
        speaking = render()
        assert orb.visual_state == "speaking"
        assert orb._jaw > .2
        assert np.count_nonzero(idle != speaking) > 100
        assert not orb._broken
        assert orb._anim_tmr.isActive()

        host.speaking = False
        host.state = "LISTENING"
        for frame in range(20):
            host.set_volume(0.0)
            orb._step_common(.04, time.monotonic())
            orb.advance(.04, 1 + frame * .04)
        assert orb.visual_state == "idle"
        assert orb._jaw < .01
        assert host.muted
    finally:
        orb.shutdown()
        host.close()


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("visible,minimized,companion_visible,sleeping", [
    (True, False, False, False),
    (True, False, True, False),
    (False, False, True, True),
    (True, True, False, True),
])
def test_focus_loss_only_suspends_hidden_or_minimized_face(
    visible, minimized, companion_visible, sleeping,
):
    powers = []
    companion = SimpleNamespace(
        isVisible=lambda: companion_visible,
        show=lambda: None,
        hide=lambda: None,
        raise_=lambda: None,
    )
    window = SimpleNamespace(
        _companion=companion,
        hud=SimpleNamespace(set_low_power=powers.append),
        isVisible=lambda: visible,
        isMinimized=lambda: minimized,
        _companion_should_show=lambda: True,
        _hint_companion_rule=lambda: None,
    )
    MediaHostMixin._sync_companion(window)
    assert powers == [sleeping]
