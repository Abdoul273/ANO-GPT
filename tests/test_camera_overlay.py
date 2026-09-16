"""Régressions de l'affichage plein cadre du CameraStudio."""

import os
import inspect
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QBuffer, QIODevice
from PyQt6.QtGui import QColor, QImage
from PyQt6.QtWidgets import QApplication

from ui import JarvisUI, MainWindow


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qapp, monkeypatch):
    monkeypatch.setattr(MainWindow, "_refresh_weather", lambda self: None)
    value = MainWindow("config/jarvis.png")
    value.resize(1000, 700)
    value.show()
    qapp.processEvents()
    yield value
    value.close()
    value.deleteLater()
    qapp.processEvents()


def _jpeg() -> bytes:
    image = QImage(640, 480, QImage.Format.Format_RGB32)
    image.fill(QColor("#23658a"))
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "JPEG")
    return bytes(buffer.data())


def _facade(window: MainWindow) -> JarvisUI:
    ui = JarvisUI.__new__(JarvisUI)
    ui._win = window
    return ui


def test_un_flux_camera_utilise_le_conteneur_plein_cadre(window, qapp):
    """La vignette historique de 244 px rendait mensonger « caméra agrandie »."""
    window._show_camera_frame(_jpeg())
    qapp.processEvents()

    assert window._cam_cont.isVisible()
    assert window._cam_cont.geometry().size() == window.centralWidget().size()
    assert not window._cam_preview.isVisible()
    assert window._cam_live_lbl.pixmap() is not None
    assert not window._cam_live_lbl.pixmap().isNull()


def test_letat_du_studio_ouvre_et_ferme_reellement_loverlay(window, qapp):
    """L'ancienne méthode absente faisait disparaître l'erreur sans agir sur l'UI."""
    ui = _facade(window)
    ui.set_camera_state({"active": True, "source": "phone", "lens": "front"})
    qapp.processEvents()
    assert window._cam_cont.isVisible()
    assert "PHONE" in window._cam_title.text()
    assert "FRONT" in window._cam_title.text()

    ui.set_camera_state({"active": False})
    qapp.processEvents()
    assert not window._cam_cont.isVisible()


def test_le_bouton_fermer_arrete_aussi_camera_studio(window):
    """Masquer l'image sans libérer la webcam empêcherait sa prochaine ouverture."""
    called = threading.Event()
    ui = _facade(window)
    ui.on_camera_close = called.set
    window._on_cam_stream(True)

    window._close_camera_view()

    assert not window._cam_cont.isVisible()
    assert called.wait(1.0)


def test_larret_programmatique_masque_aussi_la_vue_camera(window, qapp):
    """La commande vocale passe par stop_camera_stream, pas par le bouton Qt."""
    ui = _facade(window)
    window._on_cam_stream(True)
    assert window._cam_cont.isVisible()

    ui.stop_camera_stream()
    qapp.processEvents()

    assert not window._cam_cont.isVisible()


def test_lorbe_reste_vivant_en_petit_sur_une_vue_plein_ecran(window, qapp):
    """Une image décorative ne réagirait plus à la voix : on garde le vrai canvas."""
    orb = window.hud
    window._on_cam_stream(True)
    qapp.processEvents()

    assert window.hud is orb
    mini = window._mini_orb
    assert mini.isVisible()
    assert mini._source is orb
    assert 88 <= mini.width() <= 112
    assert mini.width() == mini.height()
    assert mini.x() == 16
    assert mini.y() == window.centralWidget().height() - mini.height() - 16
    assert orb.geometry().size() == window.centralWidget().size()

    orb.set_volume(0.73)
    orb.state = "THINKING"
    assert orb._target_vol == pytest.approx(0.73)
    assert orb._ws == "thinking"
    assert mini._source._target_vol == pytest.approx(0.73)
    assert mini._source._ws == "thinking"

    window._on_cam_stream(False)
    qapp.processEvents()
    assert not mini.isVisible()
    assert orb.geometry().size() == window.centralWidget().size()


def test_la_carte_utilise_le_meme_mode_orbe_compact(window, monkeypatch):
    """Carte et caméra doivent partager le même comportement immersif."""
    # Afficher QWebEngineView sous le backend Qt « offscreen » fait avorter le
    # processus natif. On vérifie le raccord de la carte puis le moteur commun.
    # `_on_show_map` délègue désormais à `_render_map`, le rendu unique que
    # partagent la position seule et les lieux trouvés : c'est donc là que la
    # resynchronisation doit vivre, pour couvrir les deux chemins d'un coup.
    assert "_sync_fullscreen_orb" in inspect.getsource(MainWindow._render_map)
    assert "_dismiss_interactive_overlays" in inspect.getsource(MainWindow._render_map)
    assert "_render_map" in inspect.getsource(MainWindow._on_show_map)
    assert "_render_map" in inspect.getsource(MainWindow._on_show_places)
    assert "_sync_fullscreen_orb" in inspect.getsource(MainWindow._on_close_map)
    assert "_relayout" in inspect.getsource(MainWindow._on_close_map)
    monkeypatch.setattr(window, "_fullscreen_surface_visible", lambda: True)
    window._sync_fullscreen_orb()

    assert window._mini_orb.isVisible()
    assert window._mini_orb.width() <= 160

    monkeypatch.setattr(window, "_fullscreen_surface_visible", lambda: False)
    window._sync_fullscreen_orb()
    assert not window._mini_orb.isVisible()
