from __future__ import annotations

import math

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QBrush, QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap,
)

from ui.styles.theme import C, qcol

class Hud:
    """Le châssis dessiné que partagent tous les panneaux d'ANO-GPT.

    Avant, chaque panneau peignait son propre cadre : rayons, bordures et
    lueurs différaient d'un widget à l'autre et l'ensemble ressemblait à une
    collection plutôt qu'à un appareil. Tout passe désormais par ici, et la
    signature est celle de la grande carte : angles biseautés en diagonale,
    bordure en dégradé cyan → magenta, balayage interne et équerres d'angle.

    Toutes les méthodes sont statiques : ce sont des instructions de peinture,
    pas un état.
    """

    CUT = 11.0            # profondeur du biseau, en pixels
    BRACKET = 18.0        # longueur des équerres d'angle
    # Période du balayage. Multiple du pas de grille (32) pour que la grille
    # revienne exactement sur elle-même : sinon elle saute à chaque bouclage.
    SCAN_PERIOD = 96.0
    CYAN = (0, 212, 255)
    MAGENTA = (255, 43, 214)

    @staticmethod
    def bevel(rect: QRectF, cut: float | None = None) -> QPainterPath:
        """Contour à deux angles coupés, en haut à gauche et en bas à droite.

        Couper les quatre angles donne un octogone décoratif ; n'en couper que
        deux crée une diagonale, et c'est elle qui fait lire la forme comme une
        pièce d'équipement plutôt que comme une simple boîte.
        """
        c = min(Hud.CUT if cut is None else cut,
                rect.width() / 3.0, rect.height() / 3.0)
        path = QPainterPath()
        path.moveTo(rect.left(), rect.top() + c)
        path.lineTo(rect.left() + c, rect.top())
        path.lineTo(rect.right(), rect.top())
        path.lineTo(rect.right(), rect.bottom() - c)
        path.lineTo(rect.right() - c, rect.bottom())
        path.lineTo(rect.left(), rect.bottom())
        path.closeSubpath()
        return path

    @staticmethod
    def _mix(base: tuple[int, int, int], alpha: int) -> QColor:
        return QColor(base[0], base[1], base[2], alpha)

    # Cache du fond immobile. Repeindre le châssis entier à chaque image
    # coûtait 12 ms sur le lecteur musique — sur cette machine à deux cœurs,
    # Qt et la boucle audio se partagent le GIL, et ces millisecondes étaient
    # prises directement sur la voix : l'assistant s'entendait lui-même et
    # s'interrompait. Seuls le balayage et les équerres bougent vraiment ; tout
    # le reste est identique d'une image à l'autre, donc peint une seule fois.
    _LAYERS: "dict[tuple, QPixmap]" = {}
    _LAYER_LIMIT = 24

    @staticmethod
    def _static_layer(width: int, height: int, accent: QColor, cut: float | None,
                      grid: bool, fill_alpha: int, ratio: float) -> QPixmap:
        key = (width, height, accent.rgba(), cut, grid, fill_alpha, round(ratio, 2))
        cached = Hud._LAYERS.get(key)
        if cached is not None:
            return cached

        pixmap = QPixmap(max(1, int(width * ratio)), max(1, int(height * ratio)))
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(Qt.GlobalColor.transparent)

        rect = QRectF(0.6, 0.6, width - 1.2, height - 1.2)
        body = Hud.bevel(rect, cut)
        p = QPainter(pixmap)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        p.setPen(QPen(Hud._mix(Hud.CYAN, 22), 6))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(body)

        base = QLinearGradient(rect.left(), rect.top(), rect.right(), rect.bottom())
        base.setColorAt(0.0, QColor(14, 28, 42, fill_alpha))
        base.setColorAt(0.55, QColor(9, 19, 31, fill_alpha))
        base.setColorAt(1.0, QColor(14, 17, 32, fill_alpha))

        border = QLinearGradient(rect.left(), rect.top(), rect.right(), rect.bottom())
        border.setColorAt(0.0, QColor(accent.red(), accent.green(), accent.blue(), 125))
        border.setColorAt(0.58, Hud._mix(Hud.MAGENTA, 55))
        border.setColorAt(1.0, QColor(accent.red(), accent.green(), accent.blue(), 70))

        p.setBrush(QBrush(base))
        p.setPen(QPen(QBrush(border), 1.2))
        p.drawPath(body)

        if grid:
            # Grille fixe : la faire dériver avec le balayage interdisait toute
            # mise en cache, pour un mouvement que personne ne remarque.
            p.save()
            p.setClipPath(body)
            p.setPen(QPen(Hud._mix(Hud.CYAN, 12), 1))
            x = rect.left()
            while x < rect.right():
                p.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
                x += 32
            y = rect.top()
            while y < rect.bottom():
                p.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
                y += 24
            p.restore()

        hi = QLinearGradient(rect.left(), 0, rect.right(), 0)
        hi.setColorAt(0.0, QColor(accent.red(), accent.green(), accent.blue(), 175))
        hi.setColorAt(0.55, QColor(accent.red(), accent.green(), accent.blue(), 40))
        hi.setColorAt(1.0, Hud._mix(Hud.MAGENTA, 105))
        p.setPen(QPen(QBrush(hi), 1.3))
        p.drawLine(QPointF(rect.left() + (cut or Hud.CUT) + 6, rect.top()),
                   QPointF(rect.right() - 18, rect.top()))
        p.end()

        if len(Hud._LAYERS) >= Hud._LAYER_LIMIT:
            # Chaque redimensionnement crée une entrée : sans borne, un panneau
            # étiré à la souris remplirait la mémoire de fonds périmés.
            Hud._LAYERS.clear()
        Hud._LAYERS[key] = pixmap
        return pixmap

    @staticmethod
    def chassis(p: QPainter, rect: QRectF, *, accent: QColor | None = None,
                scan: float = -1.0, pulse: float = 0.0, cut: float | None = None,
                grid: bool = False, brackets: bool = True,
                fill_alpha: int = 238) -> QPainterPath:
        """Peint le fond, la bordure en dégradé et les décors internes.

        Renvoie le chemin biseauté, pour que l'appelant puisse y découper son
        propre contenu.
        """
        accent = accent or qcol(C.PRI)
        body = Hud.bevel(rect, cut)

        # Le fond immobile vient du cache : c'est lui qui coûtait cher.
        try:
            ratio = float(p.device().devicePixelRatioF())
        except Exception:
            ratio = 1.0
        layer = Hud._static_layer(
            max(1, round(rect.width())), max(1, round(rect.height())),
            accent, cut, grid, fill_alpha, ratio or 1.0,
        )
        p.drawPixmap(rect.topLeft(), layer)

        # Seules les deux parties réellement animées sont repeintes.
        if scan >= 0:
            p.save()
            p.setClipPath(body)
            Hud.scanline(p, rect, scan, accent)
            p.restore()
        if brackets:
            Hud.brackets(p, rect, accent, pulse, cut)
        return body

    @staticmethod
    def scanline(p: QPainter, rect: QRectF, scan: float, accent: QColor) -> None:
        """Bande lumineuse qui descend lentement dans le panneau."""
        span = max(1.0, rect.height() + 60.0)
        y = rect.top() - 30.0 + (scan % Hud.SCAN_PERIOD) / Hud.SCAN_PERIOD * span
        band = QLinearGradient(0, y - 26, 0, y + 26)
        band.setColorAt(0.0, QColor(accent.red(), accent.green(), accent.blue(), 0))
        band.setColorAt(0.5, QColor(accent.red(), accent.green(), accent.blue(), 26))
        band.setColorAt(1.0, QColor(accent.red(), accent.green(), accent.blue(), 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(band))
        p.drawRect(QRectF(rect.left(), y - 26, rect.width(), 52))

    @staticmethod
    def brackets(p: QPainter, rect: QRectF, accent: QColor,
                 pulse: float = 0.0, cut: float | None = None) -> None:
        """Équerres d'angle, respirant doucement."""
        c = cut if cut is not None else Hud.CUT
        alpha = int(120 + 45 * math.sin(pulse))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(accent.red(), accent.green(), accent.blue(), alpha), 1.6))
        k = Hud.BRACKET
        # Les deux angles droits reçoivent une équerre complète ; les angles
        # biseautés reçoivent un simple trait le long de la coupe, sinon
        # l'équerre flotterait dans le vide laissé par le biseau.
        p.drawLine(QPointF(rect.right() - k, rect.top()), QPointF(rect.right(), rect.top()))
        p.drawLine(QPointF(rect.right(), rect.top()), QPointF(rect.right(), rect.top() + k))
        p.drawLine(QPointF(rect.left(), rect.bottom() - k), QPointF(rect.left(), rect.bottom()))
        p.drawLine(QPointF(rect.left(), rect.bottom()), QPointF(rect.left() + k, rect.bottom()))
        p.setPen(QPen(Hud._mix(Hud.MAGENTA, 150), 1.6))
        p.drawLine(QPointF(rect.left(), rect.top() + c), QPointF(rect.left() + c, rect.top()))
        p.drawLine(QPointF(rect.right() - c, rect.bottom()), QPointF(rect.right(), rect.bottom() - c))

    @staticmethod
    def tick(p: QPainter, rect: QRectF, x_from_right: float = 52.0) -> None:
        """Petit repère magenta sur le bord supérieur : la marque de fabrique."""
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(Hud._mix(Hud.MAGENTA, 120))
        p.drawRect(QRectF(rect.right() - x_from_right, rect.top() + 1, 26, 2))

    @staticmethod
    def micro_font(size: int = 7, spacing: float = 1.9) -> QFont:
        """Police des micro-intitulés : petites capitales très espacées."""
        f = QFont("Inter", size, QFont.Weight.Bold)
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing)
        return f

    @staticmethod
    def hex_path(center: QPointF, radius: float) -> QPainterPath:
        """Hexagone pointe en haut, le même que les pastilles de la carte."""
        path = QPainterPath()
        for i in range(6):
            angle = math.pi / 180.0 * (60 * i - 90)
            point = QPointF(center.x() + radius * math.cos(angle),
                            center.y() + radius * math.sin(angle))
            path.moveTo(point) if i == 0 else path.lineTo(point)
        path.closeSubpath()
        return path
