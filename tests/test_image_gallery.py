"""Galerie immersive des images trouvées en arrière-plan."""

import inspect
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QBuffer, QIODevice
from PyQt6.QtGui import QColor, QImage
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

import main
from ui import ImageGalleryOverlay, MainWindow


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _jpeg(color: str) -> bytes:
    image = QImage(800, 520, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "JPEG")
    return bytes(buffer.data())


def _images():
    return [
        {
            "title": "Chat directement lié à la demande",
            "bytes": _jpeg("#2a7192"),
            "width": 800,
            "height": 520,
            "source": "Source A",
            "source_url": "https://example.com/chat",
        },
        {
            "title": "Deuxième chat",
            "bytes": _jpeg("#7b2a86"),
            "width": 800,
            "height": 520,
            "source": "Source B",
            "source_url": "https://example.com/chat-2",
        },
    ]


def test_la_galerie_affiche_la_meilleure_image_et_les_miniatures(qapp):
    gallery = ImageGalleryOverlay()
    gallery.resize(1000, 700)
    try:
        assert gallery.show_gallery("chat", _images()) is True
        qapp.processEvents()

        assert gallery.isVisible()
        assert gallery._index == 0
        assert gallery._counter.text() == "01 / 02"
        assert "directement lié" in gallery._image_title.text()
        assert gallery._thumb_layout.count() == 2
        assert not gallery._view._original.isNull()
    finally:
        gallery.dismiss_now()
        gallery.deleteLater()


def test_navigation_source_et_sortie_animee(qapp):
    gallery = ImageGalleryOverlay()
    gallery.resize(1000, 700)
    try:
        gallery.show_gallery("chat", _images())
        gallery.select(1)
        assert gallery._counter.text() == "02 / 02"
        assert gallery._source.isEnabled()

        gallery.close_gallery()
        assert gallery._closing is True
        assert gallery._anim.duration() == 280
        QTest.qWait(330)
        assert not gallery.isVisible()
    finally:
        gallery.deleteLater()


def test_la_galerie_est_une_surface_plein_ecran_au_meme_titre_que_la_carte():
    fullscreen = inspect.getsource(MainWindow._fullscreen_surface_visible)
    show = inspect.getsource(MainWindow._on_show_image_gallery)
    assert "_image_gallery" in fullscreen
    assert "centralWidget().rect()" in show
    assert "_map_cont.hide()" in show
    assert "_close_camera_view" in show


def test_le_modele_est_force_vers_la_galerie_sans_navigateur():
    declaration = next(item for item in main.TOOL_DECLARATIONS
                       if item["name"] == "image_search")
    description = declaration["description"].lower()
    assert "must be used" in description
    assert "never use browser_control" in description
    assert declaration["parameters"]["properties"]["limit"]["maximum"] == 8
    assert any(item["name"] == "close_image_gallery" for item in main.TOOL_DECLARATIONS)


def test_la_galerie_peint_sans_effet_dopacite(qapp):
    """QGraphicsOpacityEffect + photo plein écran SIGSEGV dans QPainter::end."""
    gallery = ImageGalleryOverlay()
    gallery.resize(1000, 700)
    try:
        assert gallery.graphicsEffect() is None
        assert gallery.show_gallery("chat", _images()) is True
        qapp.processEvents()
        assert gallery.graphicsEffect() is None
        gallery.repaint()
        qapp.processEvents()
        grabbed = gallery.grab()
        assert not grabbed.isNull()
        assert grabbed.width() == 1000
        assert not gallery._view._original.isNull()
    finally:
        gallery.dismiss_now()
        gallery.deleteLater()


def test_une_image_invalide_n_ouvre_pas_la_galerie(qapp):
    gallery = ImageGalleryOverlay()
    try:
        assert gallery.show_gallery("x", [
            {"title": "cassée", "bytes": b"not-an-image", "source": "Web"},
            {"title": "vide", "bytes": b"", "source": "Web"},
        ]) is False
        assert not gallery.isVisible()
        assert ImageGalleryOverlay._decode_pixmap(b"\x00\x01\x02").isNull()
    finally:
        gallery.deleteLater()


def test_le_decodage_borne_la_taille_sans_charger_le_fichier_entier(qapp):
    image = QImage(2400, 1600, QImage.Format.Format_RGB32)
    image.fill(QColor("#123456"))
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "JPEG")
    pixmap = ImageGalleryOverlay._decode_pixmap(bytes(buffer.data()))
    assert not pixmap.isNull()
    assert max(pixmap.width(), pixmap.height()) <= ImageGalleryOverlay._MAX_IMAGE_EDGE
