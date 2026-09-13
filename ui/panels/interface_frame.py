from __future__ import annotations

import json
import math
import os
import platform
import random
import re
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

import psutil

from PyQt6.QtCore import (
    QEasingCurve, QEvent, QLineF, QPointF, QRect, QRectF, QSize, Qt,
    QTimer, QThread, pyqtSignal, QPropertyAnimation, QUrl,
)
from PyQt6.QtGui import (
    QBrush, QColor, QConicalGradient, QDragEnterEvent, QDropEvent, QFont, QImage,
    QDesktopServices, QFontDatabase, QFontMetrics, QFontMetricsF, QIcon, QKeySequence,
    QLinearGradient, QPainter,
    QPainterPath, QPen, QPixmap, QPolygonF, QRadialGradient, QRegion, QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QFrame, QGraphicsOpacityEffect, QGridLayout,
    QHBoxLayout, QLabel, QLayout, QLineEdit, QProgressBar,
    QMainWindow, QPushButton, QScrollArea, QSizePolicy, QSlider,
    QTextBrowser, QTextEdit, QVBoxLayout, QWidget, QSplashScreen,
)

from ui.styles.theme import C

class InterfaceFrame(QWidget):
    """Couche HUD périphérique indépendante du canvas de l'orbe.

    Elle donne de la profondeur aux zones vides (rails, repères, grille et
    télémétrie décorative) sans dessiner dans la zone centrale ni modifier
    ``HudCanvas``. Le coût reste faible : huit images par seconde et uniquement
    quelques primitives QPainter.
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
        # dessin complet (centaines d'ellipses) est donc rendu une fois dans
        # un pixmap, et paintEvent se contente de le recopier.
        self._cache: QPixmap | None = None
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(500)

    def _tick(self):
        self._phase = (self._phase + 0.018) % 1.0
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

        # Matrice de points périphérique. La large zone centrale reste vierge.
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
        p.end()
        return pix
