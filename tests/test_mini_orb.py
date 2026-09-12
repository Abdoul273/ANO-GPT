"""Tests unitaires pour le composant MiniOrbOverlay."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QImage, QMouseEvent, QPainter
from PyQt6.QtWidgets import QApplication

from ui.orb.arc_core import HudCanvas
from ui.orb.companion import CompanionOrb
from ui.orb.mini_orb import MiniOrbOverlay, paint_reactor


@pytest.fixture(scope="session")
def qapp():
    """Initialise l'application Qt pour l'environnement de test offscreen."""
    app = QApplication.instance() or QApplication([])
    return app


class MockOrbSource:
    """Source factice exposant le contrat requis par MiniOrbOverlay."""

    def __init__(self, ws: str = "idle", volume: float = 0.0, energy: float = 0.5):
        self._ws = ws
        self._volume = volume
        self._energy = energy
        self._PALETTES = dict(HudCanvas._PALETTES)


def test_mini_orb_initialization(qapp):
    """Vérifie l'initialisation de MiniOrbOverlay, ses attributs de transparence et son état masqué."""
    source = MockOrbSource()
    overlay = MiniOrbOverlay(source)

    assert overlay.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert overlay.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    assert "background: transparent" in overlay.styleSheet()
    assert "border: none" in overlay.styleSheet()
    assert overlay.isVisible() is False
    assert overlay.isHidden() is True


def test_mini_orb_tick_updates_only_when_visible(qapp, monkeypatch):
    """Vérifie que _tick() déclenche update() uniquement si le widget est visible."""
    source = MockOrbSource()
    overlay = MiniOrbOverlay(source)

    update_called = False

    def fake_update():
        nonlocal update_called
        update_called = True

    monkeypatch.setattr(overlay, "update", fake_update)

    # 1. Masqué (comportement par défaut) : update() ne doit pas être appelé
    assert overlay.isVisible() is False
    overlay._tick()
    assert update_called is False

    # 2. Visible : update() doit être appelé
    monkeypatch.setattr(overlay, "isVisible", lambda: True)
    overlay._tick()
    assert update_called is True


def test_mini_orb_paint_edge_cases(qapp):
    """Vérifie qu'une taille inférieure à 2x2 ne provoque aucune exception lors du dessin."""
    source = MockOrbSource()
    overlay = MiniOrbOverlay(source)

    # Tailles limites (0x0, 1x1, 1x10, 10x1)
    for w, h in [(0, 0), (1, 1), (1, 10), (10, 1)]:
        overlay.resize(w, h)
        # paintEvent doit gérer le cas W < 2 ou H < 2 sans lever d'exception
        overlay.paintEvent(None)


def test_mini_orb_renders_all_states(qapp):
    """Vérifie le rendu dans une QImage via QPainter pour tous les états de l'assistant."""
    source = MockOrbSource()
    overlay = MiniOrbOverlay(source)
    overlay.resize(120, 120)

    states = ["idle", "listening", "thinking", "acting", "speaking", "error"]
    for state in states:
        source._ws = state
        img = QImage(120, 120, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(0)

        painter = QPainter(img)
        overlay.render(painter)
        painter.end()

        assert not img.isNull()
        # Le noyau central doit être dessiné (alpha non nul au centre)
        assert img.pixelColor(60, 60).alpha() > 0


def test_mini_orb_zero_black_background(qapp):
    """Vérifie l'absence de fond noir / opaque autour de l'orbe.

    Le composant doit être 100% transparent hors des éléments holographiques/néon.
    Ce test sert de témoin RED avant suppression de la plaque sombre / bordure
    (plate QRadialGradient et drawEllipse(QRectF(1, 1, W-2, H-2))).
    """
    source = MockOrbSource()
    overlay = MiniOrbOverlay(source)
    overlay.resize(120, 120)

    img = QImage(120, 120, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0)

    painter = QPainter(img)
    overlay.render(painter)
    painter.end()

    # Les coins extérieurs doivent être strictement transparents
    assert img.pixelColor(2, 2).alpha() == 0
    assert img.pixelColor(118, 2).alpha() == 0
    assert img.pixelColor(2, 118).alpha() == 0
    assert img.pixelColor(118, 118).alpha() == 0

    # Les bordures extérieures du médaillon (60, 2) et (2, 60) doivent aussi
    # être 100% transparentes (pas de fond noir ou d'ellipse opaque jusqu'aux bords).
    # Dans le code initial avec la 'plate' QRadialGradient, ce point a alpha > 0.
    assert img.pixelColor(60, 2).alpha() == 0
    assert img.pixelColor(2, 60).alpha() == 0


def test_mini_orb_reacts_to_volume_and_energy(qapp):
    """Vérifie qu'un volume et une énergie maximaux augmentent l'activité lumineuse."""
    # Rendu à l'état de repos (volume=0.0, energy=0.5)
    source_idle = MockOrbSource(ws="idle", volume=0.0, energy=0.5)
    overlay_idle = MiniOrbOverlay(source_idle)
    overlay_idle.resize(120, 120)
    img_idle = QImage(120, 120, QImage.Format.Format_ARGB32_Premultiplied)
    img_idle.fill(0)
    p_idle = QPainter(img_idle)
    overlay_idle.render(p_idle)
    p_idle.end()

    # Rendu à haute activité (volume=1.0, energy=1.0)
    source_active = MockOrbSource(ws="idle", volume=1.0, energy=1.0)
    overlay_active = MiniOrbOverlay(source_active)
    overlay_active.resize(120, 120)
    img_active = QImage(120, 120, QImage.Format.Format_ARGB32_Premultiplied)
    img_active.fill(0)
    p_active = QPainter(img_active)
    overlay_active.render(p_active)
    p_active.end()

    def total_luminosity(img: QImage) -> int:
        ptr = img.constBits()
        ptr.setsize(img.sizeInBytes())
        return sum(bytes(ptr))

    lum_idle = total_luminosity(img_idle)
    lum_active = total_luminosity(img_active)

    assert not img_active.isNull()
    assert lum_active >= lum_idle


def test_mini_orb_enhanced_stroke_density_and_contrast(qapp):
    """Vérifie que l'orbe présente une densité lumineuse renforcée et respecte les marges strictes.

    TDD Phase RED : Fixe les critères d'acceptation pour l'agrandissement et la visibilité :
    1. Préservation absolue de la transparence sur les marges extérieures (alpha == 0 aux points
       de contrôle (2, 2), (118, 2), (2, 118), (118, 118), (60, 2), (2, 60)).
    2. Densité lumineuse totale minimale accrue d'au moins 15% par rapport à l'ancienne
       référence filiforme (~1 075 000). Seuil minimal requis : >= 1 236 000.
    3. Contraste renforcé de la cage interne et des arcs orbitaux (présence d'au moins 1800 pixels
       avec opacité alpha >= 100 contre ~1380 dans la version filiforme).
    """
    source = MockOrbSource(ws="idle")
    overlay = MiniOrbOverlay(source)
    overlay.resize(120, 120)

    img = QImage(120, 120, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0)

    painter = QPainter(img)
    overlay.render(painter)
    painter.end()

    # 1. Respect strict des marges et coins extérieurs (aucun débordement)
    exterior_control_points = [(2, 2), (118, 2), (2, 118), (118, 118), (60, 2), (2, 60)]
    for pt in exterior_control_points:
        assert img.pixelColor(*pt).alpha() == 0, (
            f"Débordement détecté sur MiniOrbOverlay au point de contrôle {pt} "
            f"(alpha={img.pixelColor(*pt).alpha()} attendu 0)"
        )

    # 2. Validation directe de l'interface paint_reactor sur le respect des marges
    img_direct = QImage(120, 120, QImage.Format.Format_ARGB32_Premultiplied)
    img_direct.fill(0)
    p_direct = QPainter(img_direct)
    paint_reactor(p_direct, QRectF(0, 0, 120, 120), source, elapsed=0.0)
    p_direct.end()

    for pt in exterior_control_points:
        assert img_direct.pixelColor(*pt).alpha() == 0, (
            f"Débordement détecté sur paint_reactor au point de contrôle {pt} "
            f"(alpha={img_direct.pixelColor(*pt).alpha()} attendu 0)"
        )

    # 3. Extraction et mesure de l'énergie photonique totale et du contraste
    ptr = img.constBits()
    ptr.setsize(img.sizeInBytes())
    data = bytes(ptr)
    total_luminosity = sum(data)
    alphas = data[3::4]
    high_contrast_pixels = sum(1 for a in alphas if a >= 100)

    # Seuil d'amplification lumineuse (> 15% au-dessus de la référence filiforme de ~1 075 000)
    min_enhanced_luminosity = 1_236_000
    assert total_luminosity >= min_enhanced_luminosity, (
        f"Luminosité cumulée insuffisante ({total_luminosity} < {min_enhanced_luminosity}). "
        "L'orbe doit générer une présence photonique renforcée (traits épaissis et halos)."
    )

    # Seuil de contraste pour la cage et les arcs orbitaux (alpha >= 100)
    min_high_contrast_pixels = 1_800
    assert high_contrast_pixels >= min_high_contrast_pixels, (
        f"Signal de contraste insuffisant ({high_contrast_pixels} < {min_high_contrast_pixels} pixels alpha >= 100). "
        "La cage et les arcs doivent être nettement plus contrastés."
    )


def test_companion_orb_initialization(qapp):
    """Vérifie l'initialisation de CompanionOrb, ses attributs de fenêtre, géométrie et style."""
    source = MockOrbSource()
    companion = CompanionOrb(source)
    companion.show()

    assert companion.windowTitle() == CompanionOrb.WINDOW_TITLE
    flags = companion.windowFlags()
    assert flags & Qt.WindowType.Tool
    assert flags & Qt.WindowType.FramelessWindowHint
    assert flags & Qt.WindowType.WindowStaysOnTopHint
    assert flags & Qt.WindowType.WindowDoesNotAcceptFocus

    assert companion.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert companion.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    assert companion.width() == CompanionOrb.FULL_W
    assert companion.height() == CompanionOrb.FULL_H

    # Une seule surface native, sans widget enfant ni fond QSS.
    assert companion.findChildren(MiniOrbOverlay) == []
    assert companion._orb_rect.width() == companion.ORB
    assert companion._orb_rect.height() == companion.ORB
    assert companion._message == ""
    assert companion._bubble_on is False
    assert companion._timer.isActive()

    # Le masque initial n'englobe que l'orbe
    initial_mask = companion._input_region.boundingRect()
    assert initial_mask.x() == companion.FULL_W - companion.ORB
    assert initial_mask.width() == companion.ORB
    assert initial_mask.height() == companion.ORB


def test_companion_orb_say_and_clear(qapp):
    """Vérifie l'affichage de message via say() et la réinitialisation via _clear_bubble()."""
    source = MockOrbSource()
    companion = CompanionOrb(source)
    companion.show()

    msg = "J.A.R.V.I.S en ligne."
    companion.say(msg)

    assert companion._bubble_on is True
    assert companion._message == msg
    assert companion._bubble_tmr.isActive()

    # Le masque doit maintenant englober la bulle et l'orbe
    bubble_mask = companion._input_region.boundingRect()
    assert bubble_mask.x() == 0
    assert bubble_mask.width() == companion.FULL_W

    # Nettoyage de la bulle
    companion._clear_bubble()

    assert companion._bubble_on is False
    assert companion._message == ""

    cleared_mask = companion._input_region.boundingRect()
    assert cleared_mask.x() == companion.FULL_W - companion.ORB
    assert cleared_mask.width() == companion.ORB


def test_companion_orb_say_edge_cases(qapp):
    """Vérifie les cas limites de say() : messages vides, troncature et hideEvent."""
    source = MockOrbSource()
    companion = CompanionOrb(source)
    companion.show()

    # Message vide ou espaces : aucun changement d'état
    companion.say("")
    assert companion._bubble_on is False
    companion.say("   \n\t  ")
    assert companion._bubble_on is False

    # Message long > 180 caractères : tronqué à 177 + ellipse
    long_msg = "A" * 200
    companion.say(long_msg)
    assert companion._bubble_on is True
    assert companion._message.endswith("…")
    assert len(companion._message) == 178

    # hideEvent doit arrêter le timer d'expiration de la bulle
    assert companion._bubble_tmr.isActive()
    companion.hide()
    assert not companion._bubble_tmr.isActive()
    assert not companion._timer.isActive()
    assert companion._message == ""


def test_companion_orb_mouse_clicks_and_callbacks(qapp):
    """Vérifie les interactions souris (clic simple vs drag, double-clic de restauration)."""
    source = MockOrbSource()
    clicked = False
    restored = False

    def on_click():
        nonlocal clicked
        clicked = True

    def on_restore():
        nonlocal restored
        restored = True

    companion = CompanionOrb(source, on_click=on_click, on_restore=on_restore)

    # 1. Clic simple valide (déplacement <= 6px)
    event_press = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        QPointF(50, 50),
        QPointF(100, 100),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    event_release_short = QMouseEvent(
        QMouseEvent.Type.MouseButtonRelease,
        QPointF(52, 52),
        QPointF(102, 102),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    companion.mousePressEvent(event_press)
    companion.mouseReleaseEvent(event_release_short)
    assert clicked is True

    # 2. Glissement / drag (déplacement > 6px) ne doit pas déclencher on_click
    clicked = False
    event_release_drag = QMouseEvent(
        QMouseEvent.Type.MouseButtonRelease,
        QPointF(70, 70),
        QPointF(120, 120),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    companion.mousePressEvent(event_press)
    companion.mouseReleaseEvent(event_release_drag)
    assert clicked is False

    # 3. Double-clic déclenche on_restore
    event_dbl = QMouseEvent(
        QMouseEvent.Type.MouseButtonDblClick,
        QPointF(50, 50),
        QPointF(100, 100),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    companion.mouseDoubleClickEvent(event_dbl)
    assert restored is True



def test_companion_transparency_with_application_theme(qapp):
    """Le thème global ne doit pas peindre la fenêtre compagnon en noir."""
    from ui.styles.qss import get_global_style

    previous = qapp.styleSheet()
    companion = None
    try:
        qapp.setStyleSheet(get_global_style())
        companion = CompanionOrb(MockOrbSource())
        companion.show()
        qapp.processEvents()
        for message in (None, "ANO en ligne", None):
            companion.say(message) if message else companion._clear_bubble()
            img = companion.grab().toImage()
            rect = companion._orb_rect
            # Dans le masque circulaire, mais hors des photons de l'orbe.
            assert img.pixelColor(rect.center().x(), rect.top()).alpha() == 0
            # L'espace réservé entre la bulle et l'orbe reste vide.
            assert img.pixelColor(companion.BUBBLE_W + 4, companion.FULL_H // 2).alpha() == 0
    finally:
        if companion is not None:
            companion.close()
        qapp.setStyleSheet(previous)


def test_embedded_orb_preserves_underlying_surface(qapp):
    from PyQt6.QtWidgets import QWidget
    from PyQt6.QtGui import QColor

    parent = QWidget()
    parent.resize(160, 160)
    parent.setStyleSheet("background: #010308;")
    surface = QWidget(parent)
    surface.setGeometry(parent.rect())
    surface.setStyleSheet("background: #bc638a;")
    orb = MiniOrbOverlay(MockOrbSource(), parent)
    orb.setGeometry(20, 20, 120, 120)
    orb.show()
    parent.show()
    qapp.processEvents()
    try:
        img = parent.grab().toImage()
        assert img.pixelColor(22, 22) == QColor("#bc638a")
        assert img.pixelColor(80, 22) == QColor("#bc638a")
    finally:
        parent.close()


def test_mini_orb_heartbeat_voice_reactivity(qapp):
    """Vérifie que la voix (volume speech 0.20-0.35) amplifie nettement la dynamique visuelle et le battement."""
    # 1. État de silence (volume=0.0)
    source_silent = MockOrbSource(ws="listening", volume=0.0, energy=0.5)
    img_silent = QImage(120, 120, QImage.Format.Format_ARGB32_Premultiplied)
    img_silent.fill(0)
    p1 = QPainter(img_silent)
    paint_reactor(p1, QRectF(0, 0, 120, 120), source_silent, elapsed=0.45)
    p1.end()

    # 2. Utilisateur qui parle (volume vocal modéré typique 0.25)
    source_speaking = MockOrbSource(ws="listening", volume=0.25, energy=0.5)
    img_speaking = QImage(120, 120, QImage.Format.Format_ARGB32_Premultiplied)
    img_speaking.fill(0)
    p2 = QPainter(img_speaking)
    paint_reactor(p2, QRectF(0, 0, 120, 120), source_speaking, elapsed=0.45)
    p2.end()

    def get_luminosity(img: QImage) -> int:
        ptr = img.constBits()
        ptr.setsize(img.sizeInBytes())
        return sum(bytes(ptr))

    lum_silent = get_luminosity(img_silent)
    lum_speaking = get_luminosity(img_speaking)

    # La parole doit produire une augmentation d'énergie lumineuse et de présence nette
    assert lum_speaking > lum_silent * 1.05


def test_direct_volume_injection(qapp):
    """Vérifie que set_volume sur MiniOrbOverlay et CompanionOrb injecte le niveau audio avec succès."""
    source = MockOrbSource(ws="idle", volume=0.0)
    overlay = MiniOrbOverlay(source)
    overlay.set_volume(0.35)
    assert overlay._direct_vol == 0.35
    assert getattr(source, "_direct_vol", 0.0) == 0.35

    companion = CompanionOrb(source)
    companion.set_volume(0.50)
    assert companion._direct_vol == 0.50
    assert getattr(source, "_direct_vol", 0.0) == 0.50


def test_mini_orb_max_activity_boundary_isolation(qapp):
    """Vérifie qu'à volume et pulsation maximaux, aucune fuite de photons n'atteint les marges transparentes."""
    source = MockOrbSource(ws="speaking", volume=1.0, energy=1.0)
    img = QImage(120, 120, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0)
    p = QPainter(img)
    # Test à différentes phases du battement
    for t in (0.1, 0.25, 0.5, 0.75, 1.0):
        paint_reactor(p, QRectF(0, 0, 120, 120), source, elapsed=t)
    p.end()

    # Les points de contrôle extérieurs doivent rester strictement transparents (alpha == 0)
    exterior_control_points = [(2, 2), (118, 2), (2, 118), (118, 118), (60, 2), (2, 60)]
    for pt in exterior_control_points:
        assert img.pixelColor(*pt).alpha() == 0, (
            f"Débordement détecté en pic d'activité au point {pt}: alpha={img.pixelColor(*pt).alpha()}"
        )
