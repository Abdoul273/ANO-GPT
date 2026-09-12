"""Régressions de la coque HUD : l'orbe reste un composant indépendant."""

import threading

import pytest

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QImage, QPainter
from PyQt6.QtWidgets import QApplication

from ui import AudioSettingsOverlay, HudCanvas, InterfaceFrame, JarvisUI, MainWindow


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def no_external_voice_catalog(monkeypatch):
    monkeypatch.setattr(AudioSettingsOverlay, "_load_elevenlabs_voices", lambda self: None)


def test_la_couche_peripherique_ne_capture_jamais_la_souris(qapp):
    frame = InterfaceFrame()
    assert frame.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    assert frame._timer.interval() >= 100


def test_le_centre_destine_a_lorbe_reste_totalement_transparent(qapp):
    frame = InterfaceFrame()
    frame.resize(1200, 800)
    image = QImage(1200, 800, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    frame.render(painter)
    painter.end()

    assert QColor(image.pixelColor(600, 400)).alpha() == 0
    assert QColor(image.pixelColor(16, 16)).alpha() > 0


def test_la_coque_et_lorbe_restent_deux_freres_independants(qapp, monkeypatch):
    monkeypatch.setattr(MainWindow, "_refresh_weather", lambda self: None)
    window = MainWindow("config/jarvis.png")
    try:
        assert isinstance(window.hud, HudCanvas)
        assert isinstance(window._interface_frame, InterfaceFrame)
        assert window.hud.parentWidget() is window.centralWidget()
        assert window._interface_frame.parentWidget() is window.centralWidget()
        assert not window._interface_frame.isAncestorOf(window.hud)
        assert window._input.height() == 42
        assert window._mute_btn.width() == 42
    finally:
        window.close()


def test_la_composition_minimale_ne_fait_pas_chevaucher_les_panneaux(qapp, monkeypatch):
    monkeypatch.setattr(MainWindow, "_refresh_weather", lambda self: None)
    window = MainWindow("config/jarvis.png")
    try:
        window.resize(820, 580)
        window.show()
        qapp.processEvents()
        assert not window._status_pill.geometry().intersects(
            window._header_panel.geometry()
        )
        assert not window._cmd_panel.geometry().intersects(
            window._telemetry_panel.geometry()
        )
    finally:
        window.close()


def test_le_panneau_audio_permet_de_choisir_les_voix_live(qapp, monkeypatch):
    monkeypatch.setattr(MainWindow, "_refresh_weather", lambda self: None)
    window = MainWindow("config/jarvis.png")
    choices = []
    window.on_voice_change = choices.append
    overlay = AudioSettingsOverlay(window, parent=window.centralWidget())
    try:
        assert overlay._voice_combo.count() == 30
        index = overlay._voice_combo.findData("Sulafat")
        assert index >= 0
        overlay._on_voice_selected(index)
        assert choices == ["Sulafat"]
        assert "reconnexion" in overlay._voice_hint.text().casefold()
    finally:
        overlay.close()
        window.close()


def test_le_niveau_audio_nest_applique_que_par_le_thread_qt(qapp, monkeypatch):
    """Le callback PortAudio ne doit jamais modifier directement HudCanvas."""
    monkeypatch.setattr(MainWindow, "_refresh_weather", lambda self: None)
    window = MainWindow("config/jarvis.png")
    facade = JarvisUI.__new__(JarvisUI)
    facade._win = window
    before = window.hud._target_vol
    try:
        worker = threading.Thread(target=lambda: facade.set_volume(0.73))
        worker.start()
        worker.join()
        assert window.hud._target_vol == before
        qapp.processEvents()
        assert window.hud._target_vol == pytest.approx(0.73)
    finally:
        window.close()


def test_une_erreur_danimation_ne_termine_pas_lapplication(qapp, monkeypatch):
    canvas = HudCanvas("config/jarvis.png")
    monkeypatch.setattr(
        canvas,
        "_tick_frame",
        lambda: (_ for _ in ()).throw(RuntimeError("image invalide")),
    )
    canvas._tick()
    assert not canvas._anim_tmr.isActive()
    canvas.close()


def test_settings_stays_above_system_after_relayout(qapp, monkeypatch):
    from PyQt6.QtCore import QPoint
    from PyQt6.QtWidgets import QScrollArea
    from ui.panels.floating_panel import FloatingPanel

    monkeypatch.setattr(MainWindow, "_refresh_weather", lambda self: None)
    window = MainWindow("config/jarvis.png")
    try:
        window.show()
        window._drawer_btn.click()
        drawer = window._quick_drawer
        assert isinstance(drawer, FloatingPanel)
        for width, height in ((1200, 800), (820, 580)):
            window.resize(width, height)
            window._relayout()
            qapp.processEvents()
            assert window.centralWidget().rect().contains(drawer.geometry())
            assert drawer.pos() == window._telemetry_panel.pos()
            assert drawer.geometry().intersects(window._telemetry_panel.geometry())
            point = drawer.mapTo(window.centralWidget(), QPoint(8, 8))
            hit = window.centralWidget().childAt(point)
            assert hit is drawer or drawer.isAncestorOf(hit)
        scroll = drawer.findChild(QScrollArea)
        assert scroll.verticalScrollBar().maximum() > 0
        drawer._close_btn.click()
        assert not window._drawer_btn.isChecked()
    finally:
        window.close()


def test_une_surface_plein_ecran_reinitialise_le_tiroir_reglages(qapp, monkeypatch):
    """Après une carte, l'engrenage doit repartir d'un état décoché."""
    monkeypatch.setattr(MainWindow, "_refresh_weather", lambda self: None)
    window = MainWindow("config/jarvis.png")
    try:
        window.show()
        window._drawer_btn.click()
        assert window._quick_drawer.isVisible()
        assert window._drawer_btn.isChecked()

        window._dismiss_interactive_overlays()
        qapp.processEvents()
        assert not window._quick_drawer.isVisible()
        assert not window._drawer_btn.isChecked()

        window._drawer_btn.click()
        assert window._quick_drawer.isVisible()
    finally:
        window._quick_drawer.hide()
        qapp.processEvents()
        window.close()


def test_audio_opens_from_settings_and_stays_above_hud(qapp, monkeypatch):
    from PyQt6.QtCore import QPoint
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QPushButton, QScrollArea

    monkeypatch.setattr(MainWindow, "_refresh_weather", lambda self: None)
    window = MainWindow("config/jarvis.png")
    try:
        window.show()
        window._drawer_btn.click()
        audio = next(b for b in window._quick_drawer.findChildren(QPushButton)
                     if b.text() == "Audio")
        audio.click()
        overlay = window._audio_settings_overlay
        for _ in range(40):
            QTest.qWait(50)
            if overlay._fx.opacity() == 1.0:
                break
        assert overlay.isVisible()
        assert overlay._fx.opacity() == 1.0
        assert not window._quick_drawer.isVisible()
        for width, height in ((1200, 800), (820, 580)):
            window.resize(width, height)
            window._relayout()
            qapp.processEvents()
            assert window.centralWidget().rect().contains(overlay.geometry())
            point = overlay.mapTo(window.centralWidget(), QPoint(5, 5))
            hit = window.centralWidget().childAt(point)
            assert hit is overlay or overlay.isAncestorOf(hit)
            assert overlay._scrim.geometry() == window.centralWidget().rect()
        scroll = overlay.findChild(QScrollArea)
        assert scroll.verticalScrollBar().maximum() > 0
        overlay.hide()
        for _ in range(40):
            QTest.qWait(50)
            if not overlay.isVisible():
                break
        assert not overlay.isVisible()
        assert not overlay._scrim.isVisible()
    finally:
        window.close()


def test_dragged_panels_keep_position_after_hud_updates(qapp, monkeypatch):
    from PyQt6.QtCore import QPoint
    from PyQt6.QtTest import QTest

    monkeypatch.setattr(MainWindow, "_refresh_weather", lambda self: None)
    window = MainWindow("config/jarvis.png")
    try:
        window.resize(1200, 800)
        window.show()
        window._drawer_btn.click()
        qapp.processEvents()
        for panel in (window._telemetry_panel, window._quick_drawer):
            start = panel.pos()
            handle = panel._hdr
            QTest.mousePress(handle, Qt.MouseButton.LeftButton, pos=QPoint(8, 8))
            QTest.mouseMove(handle, QPoint(48, 28))
            QTest.mouseRelease(handle, Qt.MouseButton.LeftButton, pos=QPoint(48, 28))
            moved = panel.pos()
            assert moved != start
            window._relayout()
            window._on_music_status({})
            qapp.processEvents()
            assert panel.pos() == moved
        window.resize(820, 580)
        qapp.processEvents()
        for panel in (window._telemetry_panel, window._quick_drawer):
            assert window.centralWidget().rect().contains(panel.geometry())
    finally:
        window.close()



def test_elevenlabs_selection_reaches_live_callbacks(qapp, monkeypatch):
    monkeypatch.setattr(MainWindow, "_refresh_weather", lambda self: None)
    window = MainWindow("config/jarvis.png")
    facade = JarvisUI.__new__(JarvisUI)
    facade._win = window
    providers, choices = [], []
    facade.on_voice_provider_change = providers.append
    facade.on_elevenlabs_voice_change = lambda vid, model: choices.append((vid, model))
    overlay = AudioSettingsOverlay(window, parent=window.centralWidget())
    try:
        overlay._on_provider_selected(1)
        assert providers == ["elevenlabs"]
        assert not overlay._eleven_panel.isHidden()
        assert overlay._voice_combo.isHidden()
        overlay._on_voices_loaded([
            {"voice_id": "FrenchVoice1", "label": "Camille — français"},
            {"voice_id": "FrenchVoice2", "label": "Louis — français"},
        ], "")
        assert choices == []  # Charger le catalogue ne change pas la voix.
        index = overlay._eleven_voice_combo.findData("FrenchVoice2")
        from PyQt6.QtTest import QTest
        window.show()
        overlay.show()
        combo = overlay._eleven_voice_combo
        combo.showPopup()
        qapp.processEvents()
        view = combo.view()
        target = combo.model().index(index, 0)
        view.scrollTo(target)
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton,
                         pos=view.visualRect(target).center())
        assert choices[-1] == ("FrenchVoice2", "eleven_multilingual_v2")
        assert not overlay._eleven_voice_combo.isEditable()
        assert overlay._eleven_voice_combo.lineEdit() is None
        overlay._on_voices_loaded([], "Permission voices_read manquante")
        assert "voices_read" in overlay._catalog_hint.text()
        assert overlay._eleven_voice_combo.currentData() == "FrenchVoice2"
        overlay._on_provider_selected(0)
        assert providers[-1] == "gemini"
        assert overlay._eleven_panel.isHidden()
        assert overlay._voice_combo.isEnabled()
    finally:
        overlay.close()
        window.close()


def test_stt_selector_reaches_session_callback(qapp, monkeypatch):
    monkeypatch.setattr(MainWindow, "_refresh_weather", lambda self: None)
    window = MainWindow("config/jarvis.png")
    facade = JarvisUI.__new__(JarvisUI)
    facade._win = window
    choices = []
    facade.on_stt_provider_change = choices.append
    overlay = AudioSettingsOverlay(window, parent=window.centralWidget())
    try:
        # Gemini Transcribe est volontairement le seul moteur proposé : aucun
        # crédit ElevenLabs ne doit pouvoir partir en reconnaissance vocale.
        assert overlay._stt_combo.findData("elevenlabs") == -1
        index = overlay._stt_combo.findData("gemini")
        assert index >= 0
        overlay._on_stt_selected(index)
        assert choices == ["gemini"]
        assert "reconnexion" in overlay._stt_hint.text()
    finally:
        overlay.close()
        window.close()
