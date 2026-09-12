import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtWidgets import QApplication, QMainWindow, QScrollArea, QWidget

from ui.panels.cards_stack import RichCardWidget
from ui.panels.rich_card_system import GlassCard, CardManager
from ui.window.chrome import ChromeMixin
from ui.window.overlay_layout import HudOverlayLayout


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def wheel_down(widget):
    event = QWheelEvent(QPointF(30, 30), QPointF(widget.mapToGlobal(QPoint(30, 30))),
                        QPoint(), QPoint(0, -120), Qt.MouseButton.NoButton,
                        Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(widget, event)


@pytest.mark.parametrize("card_class", [RichCardWidget, GlassCard])
def test_long_cards_scroll_and_can_update(qapp, card_class):
    card = RichCardWidget("info", "Briefing", "Court") if card_class is RichCardWidget else GlassCard(title="Briefing")
    card.set_body("\n\n".join(f"Ligne {i} — Informations détaillées." for i in range(100)))
    card.show()
    qapp.processEvents()
    view = card.body_view if card_class is RichCardWidget else card._body_label
    assert view.verticalScrollBar().maximum() > 0
    wheel_down(view.viewport())
    assert view.verticalScrollBar().value() > 0
    card.set_body("Terminé")
    qapp.processEvents()
    assert view.verticalScrollBar().maximum() == 0
    card.close()


def test_briefing_scroll_and_notifications_do_not_overlap(qapp):
    class Window(ChromeMixin, QMainWindow):
        pass
    window = Window()
    central = QWidget()
    window.setCentralWidget(central)
    layout = HudOverlayLayout(central)
    panel = window._build_content_panel()
    window._content_display.setPlainText("\n".join(f"Actualité {i}" for i in range(100)))
    layout.add_role(panel, "content")
    manager = CardManager()
    rail = QScrollArea()
    rail.setWidgetResizable(True)
    rail.setWidget(manager)
    rail.setFixedWidth(manager.width() + 12)
    layout.add_role(rail, "cards")
    for i in range(3):
        manager.add_card("info", str(i), "\n\n".join(["Un résultat détaillé"] * 15))
    window.resize(1200, 800)
    panel.show()
    window.show()
    qapp.processEvents()
    assert rail.geometry().bottom() < panel.geometry().top()
    assert rail.verticalScrollBar().maximum() > 0
    view = window._content_display
    wheel_down(view.viewport())
    assert view.verticalScrollBar().value() > 0
    assert window.childAt(view.mapTo(window, QPoint(30, 30))) is view.viewport()
    window.close()


def test_native_reverb_matches_original_across_chunks(monkeypatch):
    from core import spatial_audio as module
    if not module._SCIPY_AVAILABLE:
        pytest.skip("SciPy optional")
    original = module.HolographicRoomReverb(24000)
    native = module.HolographicRoomReverb(24000)
    rng = np.random.default_rng(42)
    chunks = [rng.normal(0, .1, size).astype(np.float32) for size in (17, 480, 1200, 300, 720)]
    monkeypatch.setattr(module, "_SCIPY_AVAILABLE", False)
    expected = [original.process(chunk) for chunk in chunks]
    monkeypatch.setattr(module, "_SCIPY_AVAILABLE", True)
    actual = [native.process(chunk) for chunk in chunks]
    for pair_a, pair_b in zip(expected, actual):
        for a, b in zip(pair_a, pair_b):
            np.testing.assert_allclose(a, b, atol=1e-7)
    native.reset()
    left, right = native.process(np.zeros(480, dtype=np.float32))
    assert np.max(np.abs(left)) == np.max(np.abs(right)) == 0


def test_streaming_log_preserves_reading_position(qapp):
    from ui.panels.log_widget import LogWidget
    log = LogWidget()
    log.resize(320, 180)
    log.setPlainText("\n".join(f"Message {i}" for i in range(100)))
    log.show()
    qapp.processEvents()
    log.verticalScrollBar().setValue(30)
    log._text = "Nouvelle réponse"
    log._pos = 0
    log._tag = "ai"
    log._step()
    assert log.verticalScrollBar().value() == 30
    log.verticalScrollBar().setValue(log.verticalScrollBar().maximum())
    log._pos = 0
    log._step()
    assert log.verticalScrollBar().value() >= log.verticalScrollBar().maximum() - 4
    log.close()
