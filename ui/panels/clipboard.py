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

from ui.core.hud_button import HudButton
from ui.core.hud_paint import Hud
from ui.styles.theme import C, qcol

class ClipboardPanel(QWidget):
    action_requested = pyqtSignal(str)
    _W, _H = 326, 112

    def __init__(self, parent=None):
        super().__init__(parent)
        # Le cadre est peint, comme tous les panneaux : Qt ne sait pas
        # découper un widget suivant un polygone par feuille de style.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setStyleSheet("ClipboardPanel { background: transparent; border: none; }")
        self.setFixedWidth(self._W)
        self._clip_text = ""
        self._scan = random.random() * Hud.SCAN_PERIOD
        self._pulse = random.random() * math.tau
        self._fx = QTimer(self)
        self._fx.timeout.connect(self._tick_fx)
        self._fx.start(100)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 9, 11, 10)
        lay.setSpacing(6)
        hdr = QHBoxLayout(); hdr.setSpacing(6)
        icon_lbl = QLabel("PRESSE-PAPIERS DÉTECTÉ")
        icon_lbl.setFont(Hud.micro_font(7, 1.9))
        icon_lbl.setStyleSheet(f"color: {C.ACC2}; background: transparent;")
        hdr.addWidget(icon_lbl); hdr.addStretch()
        x_btn = HudButton(icon="x", accent=C.TEXT_DIM, hover_accent=C.RED, size=11)
        x_btn.setFixedSize(20, 18)
        x_btn.clicked.connect(self.hide)
        hdr.addWidget(x_btn)
        lay.addLayout(hdr)
        self._preview = QLabel()
        self._preview.setFont(QFont("Inter", 8))
        self._preview.setStyleSheet(f"""
            color: {C.TEXT}; background: rgba(0, 212, 255, 0.06);
            border: none; border-left: 2px solid {C.ACC2}; padding: 5px 8px;
        """)
        self._preview.setWordWrap(False)
        self._preview.setFixedHeight(28)
        lay.addWidget(self._preview)
        btn_row = QHBoxLayout(); btn_row.setSpacing(5)
        for label, cmd_fmt in [
            ("TRADUIRE", "Traduis ce texte en français naturel : {text}"),
            ("RÉSUMER", "Résume ceci : {text}"),
            ("EXPLIQUER",   "Explique ceci : {text}"),
            ("CORRIGER",       "Corrige la grammaire et l’orthographe : {text}"),
        ]:
            b = HudButton(label, accent=C.TEXT_MED, hover_accent=C.PRI)
            b.setFixedHeight(23)
            b.clicked.connect(lambda _, c=cmd_fmt: self._trigger(c))
            btn_row.addWidget(b)
        lay.addLayout(btn_row)
        self._dismiss_timer = QTimer(self)
        self._dismiss_timer.setSingleShot(True)
        self._dismiss_timer.timeout.connect(self.hide)
        self.hide()

    def _tick_fx(self):
        self._scan = (self._scan + 0.85) % Hud.SCAN_PERIOD
        self._pulse = (self._pulse + 0.045) % math.tau
        if self.isVisible():
            self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(2, 2, self.width() - 4, self.height() - 4)
        # Accent ambre : ce panneau signale une opportunité, pas un résultat.
        Hud.chassis(p, rect, accent=qcol(C.ACC2), scan=self._scan, pulse=self._pulse)
        Hud.tick(p, rect, 46)
        p.end()
        super().paintEvent(event)

    def _trigger(self, cmd_fmt: str):
        if self._clip_text:
            self.action_requested.emit(cmd_fmt.format(text=self._clip_text[:800]))
        self.hide()

    def show_clipboard(self, text: str):
        self._clip_text = text
        preview = text[:58].replace('\n', ' ')
        if len(text) > 58:
            preview += "…"
        self._preview.setText(f'"{preview}"')
        self.show(); self.raise_()
        self._dismiss_timer.start(8000)
