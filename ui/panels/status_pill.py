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

from ui.core.hud_paint import Hud
from ui.styles.theme import C, qcol

class _StatusPill(QWidget):
    """Pastille d'état de l'assistant : point pulsant + libellé.

    Sert de point de repère unique — c'est le seul endroit de l'interface où
    l'état courant est écrit en toutes lettres.
    """

    _STATES = {
        "IDLE":       ("EN VEILLE",  C.TEXT_DIM),
        "LISTENING":  ("À L'ÉCOUTE", "#00f5af"),
        "THINKING":   ("RÉFLEXION",  "#af69ff"),
        "PROCESSING": ("TRAITEMENT", "#af69ff"),
        "SPEAKING":   ("RÉPONSE",    "#ffaf3c"),
        "MUTED":      ("MICRO COUPÉ", C.MUTED_C),
        "OFFLINE":    ("HORS LIGNE", C.RED),
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._state = "IDLE"
        self._t = 0.0
        self.setFixedHeight(32)
        self.setMinimumWidth(140)
        # Le QSS global donne un fond à tout QWidget : sans cette règle, ce
        # widget peint un rectangle opaque qui perfore le panneau sous-jacent.
        self.setStyleSheet("background: transparent;")
        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._tick)
        self._tmr.start(50)

    def set_state(self, state: str):
        s = (state or "IDLE").upper()
        if s not in self._STATES:
            s = "IDLE"
        if s != self._state:
            self._state = s
            self.update()

    def _tick(self):
        self._t += 0.05
        if self._state != "IDLE":
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        label, col = self._STATES[self._state]
        c = qcol(col)

        pulse = 0.55 + 0.45 * math.sin(self._t * 3.0) if self._state != "IDLE" else 0.5

        # Balise biseautée, accordée aux panneaux : la capsule arrondie était
        # le dernier rayon rond au milieu d'angles coupés.
        rect = QRectF(0.5, 0.5, W - 1, H - 1)
        p.setPen(QPen(qcol(col, 70), 1))
        p.setBrush(QBrush(qcol(col, 22)))
        p.drawPath(Hud.bevel(rect, 7.0))

        # Barre pleine à gauche : le repère de couleur reste lisible même
        # quand le point pulsant est au plus bas de son cycle.
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(qcol(col, 210)))
        p.drawRect(QRectF(rect.left() + 1, rect.top() + 7, 2, H - 15))

        # Point d'état + halo
        dx, dy = 16.0, H / 2
        p.setBrush(QBrush(qcol(col, int(70 * pulse))))
        p.drawEllipse(QPointF(dx, dy), 7.0 * pulse + 2, 7.0 * pulse + 2)
        p.setBrush(QBrush(c))
        p.drawEllipse(QPointF(dx, dy), 3.2, 3.2)

        p.setFont(Hud.micro_font(8, 1.5))
        p.setPen(QPen(c, 1))
        p.drawText(QRectF(28, 0, W - 36, H),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, label)
        p.end()
