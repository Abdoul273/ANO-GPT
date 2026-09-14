"""Tests unitaires pour l'écran de bienvenue HUD Hacker et son moteur de sound design."""

from __future__ import annotations

import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
from PyQt6.QtWidgets import QApplication
import pytest

from ui.sound import hud_sound
from ui.sound.hud_sound import HudSoundEngine, get_hud_sound
from ui.dialogs import welcome_hud
from ui.dialogs.welcome_hud import WelcomeHudOverlay


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


@pytest.fixture(autouse=True)
def isolated_sound(monkeypatch, tmp_path):
    monkeypatch.setattr(hud_sound, "SOUNDS_DIR", tmp_path / "sounds")
    monkeypatch.setattr(HudSoundEngine, "_instance", None)
    monkeypatch.setattr(hud_sound, "_sound_engine", None)
    monkeypatch.setattr(HudSoundEngine, "_init_audio_device", lambda self: None)


def test_hud_sound_engine_assets_and_buffers():
    engine = get_hud_sound()
    assert engine is not None
    assert not engine._buffers  # Aucune synthèse/disque dans le constructeur Qt.
    engine._ensure_sound_assets()
    expected_sounds = {
        "boot_surge",
        "data_chirp_1",
        "data_chirp_2",
        "data_chirp_3",
        "radar_ping",
        "hud_hover",
        "access_granted",
    }
    for s in expected_sounds:
        assert s in engine._buffers, f"Le tampon audio {s} doit être chargé"
        assert len(engine._buffers[s]) > 0


def test_hud_sound_mute_toggle():
    engine = get_hud_sound()
    init_state = engine.muted
    toggled = engine.toggle_mute()
    assert toggled != init_state
    engine.muted = init_state
    assert engine.muted == init_state


def test_hud_sound_play_safely():
    engine = get_hud_sound()
    # Ne doit pas lever d'exception même si le périphérique audio est occupé ou simulé
    engine.play_boot_surge()
    engine.play_chirp(1)
    engine.play_radar_ping()
    engine.play_hover()
    engine.play_access_granted()


def test_welcome_hud_overlay_lifecycle(qapp, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(welcome_hud, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    overlay = WelcomeHudOverlay(assistant_name="JARVIS")
    assert overlay._name == "JARVIS"
    overlay.resize(1200, 700)

    engaged_called = []
    dismissed_called = []
    overlay.engaged.connect(lambda: engaged_called.append(True))
    overlay.dismissed.connect(lambda: dismissed_called.append(True))

    overlay.show()
    assert overlay._timer.isActive()

    # Avancer plusieurs frames de boot
    for _ in range(10):
        clock[0] += 0.033
        overlay._tick_frame()

    assert overlay._elapsed > 0.0

    # Déclencher l'engagement
    overlay._engage()
    assert overlay._is_engaging is True

    # Continuer jusqu'à complétion de la transition
    clock[0] += 0.7  # La transition finit même si Qt a raté des frames.
    overlay._tick_frame()

    assert len(engaged_called) == 1
    assert len(dismissed_called) == 1
    assert not overlay._timer.isActive()
    overlay.close()


def test_hud_sound_uses_one_independent_stream():
    engine = get_hud_sound()
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    class Stream:
        def __enter__(self):
            return self

        def write(self, samples):
            entered.set()
            assert release.wait(2)

        def __exit__(self, *args):
            finished.set()

    engine._buffers["hud_hover"] = np.zeros((16, 2), dtype=np.float32)
    device = SimpleNamespace(OutputStream=Mock(return_value=Stream()), play=Mock())
    engine._sd, engine._has_audio_device = device, True
    engine.play_hover()
    try:
        assert entered.wait(2)
        for _ in range(20):
            engine.play_hover()
        assert device.OutputStream.call_count == 1
        device.play.assert_not_called()
    finally:
        release.set()
        assert finished.wait(2)


def test_hud_thread_failure_does_not_crash_qt_or_keep_lock(monkeypatch):
    engine = get_hud_sound()
    engine._sd, engine._has_audio_device = object(), True
    monkeypatch.setattr(threading.Thread, "start", Mock(side_effect=RuntimeError("no worker")))
    engine.play_hover()
    assert engine._play_lock.acquire(blocking=False)
    engine._play_lock.release()
