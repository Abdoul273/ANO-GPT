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

from unittest.mock import MagicMock, patch
import pytest
from PyQt6.QtCore import QPoint, QRect
from PyQt6.QtWidgets import QApplication, QLabel, QWidget

from ui.visual_pointer import HighlightRegionItem, ScreenTargetBox, VisualPointerOverlay


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

    result = overlay.point_on_target("confirmation", description="Confirmation requise", duration=3.0)

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
        result = overlay.point_on_screen(
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
