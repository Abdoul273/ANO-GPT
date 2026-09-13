"""tests/test_visual_pointer.py — Tests unitaires pour ui/visual_pointer.py."""

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtWidgets import QApplication

from ui.visual_pointer import (
    DrawPathItem,
    HighlightRegionItem,
    LaserPointItem,
    ScreenOverlayWindow,
    VisualAnnotation,
    VisualPointerOverlay,
    get_visual_pointer,
)


@pytest.fixture(scope="module")
def qapp():
    """Initialise QApplication pour les tests Qt."""
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


# ══════════════════════════════════════════════════════════════════════════════
# 1. Tests du cycle de vie des annotations de base
# ══════════════════════════════════════════════════════════════════════════════

def test_visual_annotation_lifecycle():
    class DummyAnnotation(VisualAnnotation):
        def paint(self, painter, now):
            pass

    t0 = 100.0
    item = DummyAnnotation(start_time=t0, duration=3.0, fade_in=0.5, fade_out=0.5)

    # Avant le début
    assert item.alpha(t0 - 1.0) == 0.0

    # Au début du fondu d'entrée
    assert item.alpha(t0 + 0.25) == pytest.approx(0.5, abs=0.01)

    # Pleine opacité (plateau)
    assert item.alpha(t0 + 1.5) == 1.0
    assert not item.is_expired(t0 + 1.5)

    # Fondu de sortie (reste 0.25s sur 0.5s de fade_out)
    assert item.alpha(t0 + 2.75) == pytest.approx(0.5, abs=0.01)

    # Expiré
    assert item.is_expired(t0 + 3.0)
    assert item.alpha(t0 + 3.1) == 0.0


# ══════════════════════════════════════════════════════════════════════════════
# 2. Tests de HighlightRegionItem (Rectangle néon + Flèche animée)
# ══════════════════════════════════════════════════════════════════════════════

def test_highlight_region_item_paint(qapp):
    img = QImage(800, 600, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0)
    p = QPainter(img)

    # Cas 1 : y >= 90 (flèche au-dessus pointant vers le bas)
    item = HighlightRegionItem(100, 150, 200, 100, label="Bouton Valider", duration=3.0)
    assert item.label == "Bouton Valider"
    assert item.w == 200.0
    assert item.h == 100.0

    # Rendu à mi-parcours (alpha 1.0)
    item.paint(p, item.start_time + 1.0)

    # Cas 2 : y < 90 (flèche en-dessous pointant vers le haut)
    item_top = HighlightRegionItem(100, 30, 150, 40, label="Menu Haut", duration=2.5)
    item_top.paint(p, item_top.start_time + 0.5)

    p.end()
    # Vérification que des pixels non-transparents ont bien été peints
    assert any(img.pixelColor(x, y).alpha() > 0 for x in range(100, 300) for y in range(150, 250))


# ══════════════════════════════════════════════════════════════════════════════
# 3. Tests de LaserPointItem (Point rouge + Ondes concentriques)
# ══════════════════════════════════════════════════════════════════════════════

def test_laser_point_item_paint(qapp):
    img = QImage(600, 600, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0)
    p = QPainter(img)

    laser = LaserPointItem(300, 300, duration=2.0)
    assert laser.x == 300.0
    assert laser.y == 300.0

    # Rendu à 0.5s (ondes expansives et réticule tournant)
    laser.paint(p, laser.start_time + 0.5)
    p.end()

    # Le centre doit avoir une forte composante rouge/blanche
    center_color = img.pixelColor(300, 300)
    assert center_color.alpha() > 200
    assert center_color.red() > 200


# ══════════════════════════════════════════════════════════════════════════════
# 4. Tests de DrawPathItem (Trajectoire néon + Comète)
# ══════════════════════════════════════════════════════════════════════════════

def test_draw_path_item_paint(qapp):
    img = QImage(800, 600, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0)
    p = QPainter(img)

    path_pts = [(100, 100), (300, 250), (500, 200)]
    path_item = DrawPathItem(path_pts, duration=3.5, label="Déplacer fichier")

    assert len(path_item.points) == 3
    assert path_item._length > 100.0

    path_item.paint(p, path_item.start_time + 1.2)
    p.end()

    # Départ et arrivée doivent avoir des pixels dessinés
    assert img.pixelColor(100, 100).alpha() > 0
    assert img.pixelColor(500, 200).alpha() > 0


# ══════════════════════════════════════════════════════════════════════════════
# 5. Tests de ScreenOverlayWindow (Wayland compliance & Flags)
# ══════════════════════════════════════════════════════════════════════════════

def test_screen_overlay_window_flags(qapp):
    overlay = VisualPointerOverlay()
    screen = qapp.primaryScreen()
    win = ScreenOverlayWindow(screen, overlay)

    # 1. Vérification des flags non-bloquants critiques
    flags = win.windowFlags()
    assert bool(flags & Qt.WindowType.FramelessWindowHint)
    assert bool(flags & Qt.WindowType.WindowStaysOnTopHint)
    assert bool(flags & Qt.WindowType.WindowTransparentForInput)
    assert bool(flags & Qt.WindowType.Tool)
    assert bool(flags & Qt.WindowType.WindowDoesNotAcceptFocus)

    # 2. Vérification des attributs de transparence
    assert win.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert win.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    assert win.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

    # 3. Titre Hyprland pour ciblage par règle de fenêtre
    assert "ANO Visual Pointer" in win.windowTitle()


# ══════════════════════════════════════════════════════════════════════════════
# 6. Tests du gestionnaire VisualPointerOverlay & point_on_screen
# ══════════════════════════════════════════════════════════════════════════════

def test_visual_pointer_overlay_methods(qapp):
    overlay = VisualPointerOverlay()
    assert len(overlay.active_annotations) == 0
    assert not overlay._timer.isActive()

    # 1. Ajout de highlight_region
    overlay.highlight_region(50, 50, 100, 60, label="Test", duration=2.0)
    assert len(overlay.active_annotations) == 1
    assert overlay._timer.isActive()

    # 2. Ajout de laser_point
    overlay.laser_point(200, 300, duration=1.5)
    assert len(overlay.active_annotations) == 2

    # 3. Ajout de draw_path
    overlay.draw_path([(10, 10), (50, 50)], duration=2.0, label="Chemin")
    assert len(overlay.active_annotations) == 3

    # 4. Nettoyage immédiat
    overlay.clear()
    assert len(overlay.active_annotations) == 0
    assert not overlay._timer.isActive()


def test_point_on_screen_coordinate_formats(qapp):
    overlay = VisualPointerOverlay()

    # Format 1 : 2 coordonnées [x, y] -> Laser
    res_laser = overlay.point_on_screen("Cible Laser", [400, 300])
    assert "laser" in res_laser.lower()
    assert len(overlay.active_annotations) == 1
    assert isinstance(overlay.active_annotations[-1], LaserPointItem)

    # Format 2 : 4 coordonnées standard [x, y, w, h] -> Highlight
    res_rect = overlay.point_on_screen("Bouton Sauvegarder", [250, 150, 120, 45])
    assert "zone mise en valeur" in res_rect.lower()
    assert len(overlay.active_annotations) == 2
    assert isinstance(overlay.active_annotations[-1], HighlightRegionItem)

    # Format 3 : 4 coordonnées Gemini 0-1000 [ymin, xmin, ymax, xmax] -> Highlight converti
    res_gemini = overlay.point_on_screen("Logo Gemini", [100, 200, 300, 600], mode="gemini_box")
    assert "zone mise en valeur" in res_gemini.lower()
    assert len(overlay.active_annotations) == 3
    assert isinstance(overlay.active_annotations[-1], HighlightRegionItem)

    # Format 4 : 6 coordonnées [x1, y1, x2, y2, x3, y3] -> Path
    res_path = overlay.point_on_screen("Glisser-déposer", [100, 100, 300, 200, 500, 400])
    assert "trajectoire visuelle" in res_path.lower()
    assert len(overlay.active_annotations) == 4
    assert isinstance(overlay.active_annotations[-1], DrawPathItem)

    # Format invalide
    res_err = overlay.point_on_screen("Vide", [])
    assert "erreur" in res_err.lower()

    overlay.clear()


# ══════════════════════════════════════════════════════════════════════════════
# 7. Test de non-régression du Singleton
# ══════════════════════════════════════════════════════════════════════════════

def test_singleton_pointer(qapp):
    p1 = get_visual_pointer()
    p2 = get_visual_pointer()
    assert p1 is p2
