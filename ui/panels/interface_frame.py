from __future__ import annotations

import math

from PyQt6.QtCore import (
    QPointF, QRectF, Qt,
    QTimer,
)
from PyQt6.QtGui import (
    QBrush, QColor, QFont, QLinearGradient, QPainter,
    QPen, QPixmap, QPolygonF,
)
from PyQt6.QtWidgets import (
    QWidget,
)


class InterfaceFrame(QWidget):
    """Couche HUD périphérique indépendante du canvas de l'orbe.

    Elle donne de la profondeur aux zones vides (rails, repères, grille et
    télémétrie décorative) sans dessiner dans la zone centrale ni modifier
    ``HudCanvas``. Les informations décoratives sont regroupées sur les bords
    comme une vraie console, afin que l'orbe reste le point focal. Le coût
    reste faible : deux images par seconde et une image mise en cache entre
    deux battements.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("InterfaceFrame")
        # Le QSS global donne un fond à tout QWidget. Sans cette règle plus
        # spécifique, la couche dite transparente peindrait malgré tout un
        # rectangle noir et masquerait le widget de l'orbe.
        self.setStyleSheet("QWidget#InterfaceFrame { background: transparent; border: none; }")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._phase = 0.0
        # Ce calque translucide recouvre l'orbe : Qt le repeint à chaque image
        # de celui-ci (25-30 fois/s), pas seulement à son propre tick. Le
        # dessin complet est donc rendu une fois dans un pixmap, et paintEvent
        # se contente de le recopier entre deux battements de 500 ms.
        self._cache: QPixmap | None = None
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(500)

    def _tick(self):
        # Une variation lente suffit à faire vivre les moniteurs latéraux. La
        # couche est derrière l'orbe, donc une cadence supérieure ne serait
        # qu'une charge prise au thread audio.
        self._phase = (self._phase + 0.028) % 1.0
        self._cache = None
        if self.isVisible():
            self.update()

    def resizeEvent(self, event):
        self._cache = None
        super().resizeEvent(event)

    def paintEvent(self, _):
        W, H = self.width(), self.height()
        if W < 700 or H < 500:
            return
        if self._cache is None or self._cache.size() != self.size():
            self._cache = self._render(W, H)
        p = QPainter(self)
        p.drawPixmap(0, 0, self._cache)
        p.end()

    def _render(self, W: int, H: int) -> QPixmap:
        pix = QPixmap(W, H)
        pix.fill(Qt.GlobalColor.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Vignettes latérales très légères : elles structurent l'espace sans
        # poser une plaque opaque par-dessus l'orbe.
        left_glow = QLinearGradient(0, 0, min(360, W * 0.32), 0)
        left_glow.setColorAt(0.0, QColor(0, 110, 150, 24))
        left_glow.setColorAt(1.0, QColor(0, 20, 35, 0))
        p.fillRect(QRectF(0, 0, min(360, W * 0.32), H), QBrush(left_glow))
        right_glow = QLinearGradient(W, 0, max(W - 360, W * 0.68), 0)
        right_glow.setColorAt(0.0, QColor(95, 20, 145, 18))
        right_glow.setColorAt(1.0, QColor(0, 20, 35, 0))
        p.fillRect(QRectF(max(0, W - 360), 0, min(360, W * 0.32), H), QBrush(right_glow))

        # Matrice de points périphérique. La large zone centrale reste vierge :
        # il ne faut jamais transformer l'orbe en fond décoratif.
        p.setPen(Qt.PenStyle.NoPen)
        for x0, x1 in ((226, min(350, W // 3)), (max(W - 350, W * 2 // 3), W - 24)):
            for x in range(int(x0), int(x1), 22):
                for y in range(96, H - 112, 22):
                    distance = abs((y / max(1, H)) - self._phase)
                    alpha = 17 + int(16 * max(0.0, 1.0 - distance * 7.0))
                    p.setBrush(QColor(0, 212, 255, alpha))
                    p.drawEllipse(QPointF(x, y), 1.15, 1.15)

        # Rails techniques haut/bas, volontairement interrompus au centre.
        rail = QColor(0, 212, 255, 58)
        p.setPen(QPen(rail, 1))
        gap_l, gap_r = W * 0.39, W * 0.61
        for y in (78.0, H - 96.0):
            p.drawLine(QPointF(224, y), QPointF(gap_l, y))
            p.drawLine(QPointF(gap_r, y), QPointF(W - 24, y))
        p.setPen(QPen(QColor(255, 43, 214, 70), 1.2))
        p.drawLine(QPointF(W - 210, 78), QPointF(W - 146, 78))
        p.drawLine(QPointF(238, H - 96), QPointF(292, H - 96))

        # Deux rails verticaux découpés en segments. Ils donnent une lecture
        # "instrumentation" aux bords sans créer de second panneau flottant.
        side_x = (240.0, W - 240.0)
        rail_top, rail_bottom = 108.0, H - 128.0
        p.setPen(QPen(QColor(0, 212, 255, 48), 1.0))
        for x in side_x:
            p.drawLine(QPointF(x, rail_top), QPointF(x, rail_bottom))
            for y in range(int(rail_top + 12), int(rail_bottom), 18):
                direction = 1 if x < W / 2 else -1
                length = 8 if (y // 18) % 4 == 0 else 4
                p.drawLine(QPointF(x, y), QPointF(x + direction * length, y))

        # Traces de signal : très peu de segments, lisibles comme de la
        # télémétrie mais intentionnellement décoratifs. Les données système
        # réelles restent dans le panneau "Système" à gauche.
        for origin_x, direction, tint in (
            (254.0, 1.0, QColor(0, 212, 255, 92)),
            (W - 254.0, -1.0, QColor(255, 43, 214, 76)),
        ):
            path = QPolygonF()
            for step in range(15):
                x = origin_x + direction * step * 6.2
                wave = math.sin((step * 0.82) + self._phase * math.tau)
                envelope = 3.5 + (step % 5) * 1.1
                y = H * 0.50 + wave * envelope
                path.append(QPointF(x, y))
            p.setPen(QPen(tint, 1.0))
            p.drawPolyline(path)

        # Marques de calibration aux quatre coins : double niveau et point
        # d'index magenta. Le langage visuel devient plus précis sans épaissir
        # les cadres déjà portés par les panneaux.
        p.setPen(QPen(QColor(0, 212, 255, 52), 1.0))
        inset, inner_arm = 26.0, 24.0
        for x, y, sx, sy in ((inset, inset, 1, 1), (W-inset, inset, -1, 1),
                             (inset, H-inset, 1, -1), (W-inset, H-inset, -1, -1)):
            p.drawLine(QPointF(x, y), QPointF(x + sx * inner_arm, y))
            p.drawLine(QPointF(x, y), QPointF(x, y + sy * inner_arm))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 43, 214, 125))
        p.drawRect(QRectF(W - 88, 15, 22, 2))
        p.drawRect(QRectF(66, H - 17, 16, 2))

        # Crochets de cadre et micro-graduations dans les coins.
        p.setPen(QPen(QColor(0, 212, 255, 100), 1.2))
        margin, arm = 16.0, 42.0
        for x, y, sx, sy in ((margin, margin, 1, 1), (W-margin, margin, -1, 1),
                             (margin, H-margin, 1, -1), (W-margin, H-margin, -1, -1)):
            p.drawLine(QPointF(x, y), QPointF(x + sx * arm, y))
            p.drawLine(QPointF(x, y), QPointF(x, y + sy * arm))
            for step in range(10, 38, 9):
                p.drawLine(QPointF(x + sx * step, y),
                           QPointF(x + sx * step, y + sy * 4))

        # Labels minuscules façon avionique, lisibles mais non envahissants.
        font = QFont("Inter", 6, QFont.Weight.Bold)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.5)
        p.setFont(font)
        p.setPen(QColor(58, 138, 154, 155))
        p.drawText(QRectF(228, 82, 180, 14), "CORE LINK // XLIX")
        p.drawText(QRectF(W - 250, H - 91, 220, 14),
                   Qt.AlignmentFlag.AlignRight, "NEURAL INTERFACE // ONLINE")
        p.setPen(QColor(74, 147, 167, 130))
        p.drawText(QRectF(252, H * 0.50 - 24, 115, 13), "SIGNAL // 8.4")
        p.drawText(QRectF(W - 367, H * 0.50 + 12, 115, 13),
                   Qt.AlignmentFlag.AlignRight, "SYNC // STABLE")
        p.end()
        return pix
