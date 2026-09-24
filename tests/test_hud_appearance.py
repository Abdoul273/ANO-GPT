"""Changement en direct du fond et de l'orbe par l'assistant."""

import threading
import time

import pytest
from PyQt6.QtGui import QColor, QImage
from PyQt6.QtWidgets import QApplication

from actions.hud_appearance import hud_appearance, resolve_orb
from ui import MainWindow


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _call_from_worker(qapp, func):
    outcome = {}

    def run():
        try:
            outcome["result"] = func()
        except Exception as exc:
            outcome["error"] = exc

    worker = threading.Thread(target=run)
    worker.start()
    deadline = time.monotonic() + 5
    while worker.is_alive() and time.monotonic() < deadline:
        qapp.processEvents()
        worker.join(.01)
    worker.join(timeout=.1)
    assert not worker.is_alive()
    if "error" in outcome:
        raise outcome["error"]
    return outcome["result"]


def test_noms_orbes_compris_en_francais():
    assert resolve_orb("GÉODÉSIQUE") == "wireframe"
    assert resolve_orb("IRIS") == "iris"
    assert resolve_orb("spectre") == "radial"
    choices = hud_appearance({"action": "list"}, None)
    assert "GÉODÉSIQUE" in choices
    assert "SPECTRE" in choices
    assert "wireframe" not in choices
    assert "radial" not in choices


def test_changements_rapides_conservent_le_dernier_choix(qapp, monkeypatch, tmp_path):
    import json
    from ui import paths

    config = tmp_path / "api_keys.json"
    monkeypatch.setattr(paths, "API_FILE", config)
    monkeypatch.setattr(paths, "_cache", dict(paths._read_full_config()))
    monkeypatch.setattr(paths, "_loaded", True)
    paths._write_full_config({"orb_style": "pulse", "background_image": "un.png"})
    paths._write_full_config({"orb_style": "iris", "background_image": "deux.png"})
    paths._write_executor.submit(lambda: None).result(timeout=5)
    assert json.loads(config.read_text(encoding="utf-8")) == {
        "orb_style": "iris", "background_image": "deux.png",
    }


def test_changement_simultane_depuis_un_fil_audio(qapp, monkeypatch, tmp_path):
    from ui.window import scene

    monkeypatch.setattr(MainWindow, "_refresh_weather", lambda self: None)
    saved = []
    monkeypatch.setattr(scene, "_write_full_config", lambda data: saved.append(dict(data)))
    window = MainWindow("config/jarvis.png")
    image = QImage(64, 64, QImage.Format.Format_RGB32)
    image.fill(QColor("#45328a"))
    background = tmp_path / "essai.png"
    assert image.save(str(background))
    try:
        result = _call_from_worker(qapp, lambda: hud_appearance({
            "action": "apply", "orb_style": "IRIS", "background_image": str(background),
        }, window))
        assert "visible et enregistré" in result
        assert window.hud.style_id == "iris"
        assert window._background_image.path == str(background)
        assert window.hud._snapshot.background_active
        assert saved[-1]["orb_style"] == "iris"
        assert saved[-1]["background_image"] == str(background)

        bad = tmp_path / "illisible.png"
        bad.write_text("pas une image", encoding="utf-8")
        with pytest.raises(ValueError, match="illisible"):
            _call_from_worker(qapp, lambda: hud_appearance({
                "orb_style": "pulse", "background_image": str(bad),
            }, window))
        assert window.hud.style_id == "iris"
        assert window._background_image.path == str(background)

        _call_from_worker(qapp, lambda: hud_appearance({
            "orb_style": "PULSE", "background_image": "aucun",
        }, window))
        assert window.hud.style_id == "pulse"
        assert not window._background_image.has_image
        assert saved[-1]["background_image"] == ""
    finally:
        window.close()
