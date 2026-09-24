"""tests/test_smart_screen_pointer.py — Tests TDD pour le ciblage et l'encadrement écran temps réel.

Couvre la Tâche 3 du plan docs/plans/2026-09-05-confirmation-card-and-live-pointer.md :
1. Résolution de widget interne :
   - Widget masqué (isVisible() == False) : AUCUN cadre fantôme, message d'absence explicatif.
   - Widget visible : calcul géométrique exact via mapToGlobal() et encadrement précis.
2. Détection en temps réel pour cibles d'écran externes :
   - Non détecté : AUCUN cadre tracé, message d'absence explicite.
   - Détecté : conversion bounding box vers pixels réels et encadrement HighlightRegionItem.
3. Intégration ToolDispatcher :
   - Rejet des cibles non visibles sans hallucination de coordonnées.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import time
from unittest.mock import MagicMock, patch
import pytest
from PyQt6.QtCore import QPoint
from PyQt6.QtWidgets import QApplication, QLabel, QWidget

from ui.visual_pointer import DrawPathItem, HighlightRegionItem, ScreenTargetBox, VisualPointerOverlay


@pytest.fixture(scope="module")
def qapp():
    """Initialise QApplication pour les tests Qt."""
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture
def overlay(qapp):
    """Fournit une instance propre de VisualPointerOverlay."""
    ov = VisualPointerOverlay()
    ov.clear()
    yield ov
    ov.clear()


# ══════════════════════════════════════════════════════════════════════════════
# 1. Tests Résolution de Widget Interne ANO-GPT
# ══════════════════════════════════════════════════════════════════════════════

def test_internal_widget_not_visible_no_ghost_frame(overlay):
    """Vérifie que si un widget cible est masqué (non visible),

    aucun cadre fantôme n'est créé et un message d'absence est renvoyé.
    """
    win = QWidget()
    btn = QLabel("Carte Confirmation", win)
    btn.setObjectName("confirmation_card")
    win.hide()
    btn.hide()

    # Enregistrement du widget cible auprès du résolveur
    overlay.register_target_widget("confirmation", btn)

    result = overlay.point_on_target("confirmation", description="Confirmation requise")

    # 1. Aucun cadre n'a été créé dans active_annotations
    assert len(overlay.active_annotations) == 0, "Aucun cadre ne doit être tracé si le widget est masqué !"

    # 2. Le message informe clairement de la non-visibilité
    msg = result.lower()
    assert any(w in msg for w in ["pas affiché", "non visible", "masqué", "introuvable"]), (
        f"Message inattendu : {result}"
    )


def test_internal_widget_visible_highlights_exact_geometry(overlay):
    """Vérifie que si un widget cible est visible, ses coordonnées globales réelles

    (mapToGlobal) sont calculées et un cadre HighlightRegionItem est tracé exactement dessus.
    """
    win = QWidget()
    win.setGeometry(100, 100, 400, 300)
    btn = QLabel("Carte Confirmation", win)
    btn.setObjectName("confirmation_card")
    btn.setGeometry(50, 40, 220, 90)

    win.show()
    btn.show()

    overlay.register_target_widget("confirmation", btn)

    overlay.point_on_target("confirmation", description="Confirmation requise", duration=3.0)

    # 1. Une annotation doit être créée
    assert len(overlay.active_annotations) == 1, "Une annotation doit être créée pour un widget visible"
    ann = overlay.active_annotations[0]
    assert isinstance(ann, HighlightRegionItem)

    # 2. Vérification des coordonnées réelles (avec padding tolérance +/- 8px)
    global_pos = btn.mapToGlobal(QPoint(0, 0))
    expected_x = global_pos.x()
    expected_y = global_pos.y()
    expected_w = btn.width()
    expected_h = btn.height()

    assert abs(ann.x - expected_x) <= 8.0, f"X attendu ~{expected_x}, obtenu {ann.x}"
    assert abs(ann.y - expected_y) <= 8.0, f"Y attendu ~{expected_y}, obtenu {ann.y}"
    assert abs(ann.w - expected_w) <= 16.0, f"W attendu ~{expected_w}, obtenu {ann.w}"
    assert abs(ann.h - expected_h) <= 16.0, f"H attendu ~{expected_h}, obtenu {ann.h}"
    assert "Confirmation requise" in ann.label

    win.close()


def test_widget_resolution_from_vision_worker_stays_on_qt_thread(overlay, qapp):
    win = QWidget()
    win.setGeometry(100, 100, 200, 100)
    widget = QLabel("Horloge", win)
    widget.setObjectName("clock_widget")
    widget.setGeometry(10, 10, 80, 30)
    win.show()
    widget.show()
    overlay.register_target_widget("clock_widget", widget)

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(overlay.point_on_target, "clock_widget")
        deadline = time.monotonic() + 2.0
        while not pending.done() and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        result = pending.result(timeout=1.0)

    qapp.processEvents()
    assert "encadrée" in result
    assert len(overlay.active_annotations) == 1
    win.close()


# ══════════════════════════════════════════════════════════════════════════════
# 2. Tests Détection Écran Temps Réel (Cibles Externes)
# ══════════════════════════════════════════════════════════════════════════════

def test_external_screen_target_not_found_no_ghost_frame(overlay):
    """Vérifie que si la détection visuelle ne trouve pas l'élément sur l'écran capturé,

    aucun cadre fantôme n'est créé et un message d'absence est renvoyé.
    """
    with patch("ui.visual_pointer.detect_screen_target_live", return_value=None):
        result = overlay.point_on_screen(
            description="Bouton Sublime Text",
            target="sublime_text_install_button",
            mode="auto",
        )

        assert len(overlay.active_annotations) == 0, "Aucun cadre ne doit être tracé si l'élément n'est pas vu !"
        msg = result.lower()
        assert any(w in msg for w in ["pas visible", "non détecté", "introuvable", "pas affiché"]), (
            f"Message inattendu : {result}"
        )


def test_external_screen_target_found_highlights_box(overlay):
    """Vérifie que si l'élément externe est détecté avec une bounding box normalisée

    [ymin, xmin, ymax, xmax] (0-1000), elle est convertie et encadrée avec précision.
    """
    # Bounding box Gemini normalisée [ymin, xmin, ymax, xmax] 0-1000
    mock_box = [200, 300, 400, 700]

    with patch("ui.visual_pointer.detect_screen_target_live", return_value=mock_box):
        overlay.point_on_screen(
            description="Bouton Sublime Text",
            target="sublime_text_install_button",
            mode="auto",
        )

        assert len(overlay.active_annotations) == 1, "Un cadre doit être tracé pour l'élément détecté"
        ann = overlay.active_annotations[0]
        assert isinstance(ann, HighlightRegionItem)
        assert ann.w > 20.0
        assert ann.h > 20.0
        assert "Bouton Sublime Text" in ann.label


def test_external_target_uses_the_captured_monitor_geometry_not_primary_screen(overlay):
    """Une boîte vue sur un écran secondaire doit rester sur cet écran."""
    captured = ScreenTargetBox(
        box=(100.0, 200.0, 300.0, 600.0),
        origin=(1920.0, 0.0),
        size=(1280.0, 720.0),
    )
    with patch("ui.visual_pointer.detect_screen_target_live", return_value=captured):
        overlay.point_on_target("horloge", description="L'heure affichée")

    ann = overlay.active_annotations[0]
    assert ann.x == pytest.approx(1920.0 + 0.2 * 1280.0)
    assert ann.y == pytest.approx(0.1 * 720.0)
    assert ann.w == pytest.approx(0.4 * 1280.0)
    assert ann.h == pytest.approx(0.2 * 720.0)


def test_clock_in_layer_shell_dock_gets_visible_edge_arrow(overlay, monkeypatch):
    from ui import visual_pointer

    monkeypatch.setattr(visual_pointer.kit, "hypr_json", lambda *args, **kwargs: [{
        "name": "eDP-1", "x": 0, "y": 0, "width": 1920, "height": 1080,
        "reserved": [60, 10, 10, 10],
    }])

    callout = overlay.point_at_rect(18, 850, 35, 45, label="Horloge")

    assert callout is True
    annotation = overlay.active_annotations[-1]
    assert isinstance(annotation, DrawPathItem)
    assert annotation.points[-1].x() == pytest.approx(70)
    assert annotation.points[-1].y() == pytest.approx(872.5)


def test_live_detection_checks_other_monitor_without_shrinking_desktop(monkeypatch):
    from types import SimpleNamespace
    from core import multimodal_vision, screen_capture
    from ui import visual_pointer
    from google import genai

    captures = []

    def capture(**kwargs):
        captures.append(kwargs)
        name = kwargs["monitor_name"]
        origin = (0, 0) if name == "DP-1" else (1920, 0)
        return b"image", "image/jpeg", {
            "capture_origin": origin, "capture_size": (1920, 1080), "monitor": name,
        }

    responses = iter([
        SimpleNamespace(text='{"box": null, "label": "", "confidence": 0}'),
        SimpleNamespace(text='{"box": [10, 850, 40, 980], "label": "horloge", "confidence": 92}'),
    ])
    monkeypatch.setattr(multimodal_vision, "_get_api_key", lambda: "test-key")
    monkeypatch.setattr(multimodal_vision, "_call_gemini_vision", lambda *args: (next(responses), "test-model"))
    monkeypatch.setattr(genai, "Client", lambda **kwargs: object())
    monkeypatch.setattr(screen_capture, "monitor_names_focused_first", lambda: ["DP-1", "DP-2"])
    monkeypatch.setattr(screen_capture, "capture_window_or_screen", capture)
    monkeypatch.setattr(visual_pointer, "_confirm_target_box", lambda *args: True)

    found = visual_pointer.detect_screen_target_live("horloge", query="sur mon système")

    assert found is not None
    assert found.monitor == "DP-2"
    assert found.origin == (1920.0, 0.0)
    assert [call["monitor_name"] for call in captures] == ["DP-1", "DP-2"]
    assert all(call["target"] == "monitor" for call in captures)


# ══════════════════════════════════════════════════════════════════════════════
# 3. Tests Intégration ToolDispatcher
# ══════════════════════════════════════════════════════════════════════════════

def test_tool_dispatcher_point_on_screen_smart_resolution(overlay):
    """Vérifie que ToolDispatcher._agent_point_on_screen délègue correctement au résolveur

    intelligent sans tracer dans le vide si la cible est introuvable.
    """
    from core.tool_dispatcher import ToolDispatcher

    mock_ui = MagicMock()
    mock_ui.visual_pointer = overlay

    class DummyAgent:
        ui = mock_ui
        _agent_point_on_screen = ToolDispatcher._agent_point_on_screen

    dispatcher = DummyAgent()

    with patch("ui.visual_pointer.detect_screen_target_live", return_value=None):
        res = dispatcher._agent_point_on_screen({
            "target": "carte_confirmation",
            "description": "Carte de confirmation",
        })

        assert len(overlay.active_annotations) == 0
        assert any(w in res.lower() for w in ["pas visible", "non détecté", "pas affiché", "introuvable"])


def test_tool_dispatcher_rechecks_model_coordinates_against_fresh_screen(overlay):
    """Des coordonnées non ancrées ne doivent jamais dessiner un laser au hasard."""
    from core.tool_dispatcher import ToolDispatcher

    mock_ui = MagicMock()
    mock_ui.visual_pointer = overlay

    class DummyAgent:
        ui = mock_ui
        _agent_point_on_screen = ToolDispatcher._agent_point_on_screen

    dispatcher = DummyAgent()
    with patch("ui.visual_pointer.detect_screen_target_live", return_value=None):
        res = dispatcher._agent_point_on_screen({
            "description": "L'heure dans la barre du haut",
            "coordinates": [10, 10],
            "mode": "laser",
        })

    assert len(overlay.active_annotations) == 0
    assert any(word in res.lower() for word in ["pas visible", "introuvable"])
