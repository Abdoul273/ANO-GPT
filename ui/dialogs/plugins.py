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

from ui.core.fade_widget import FadeInWidget
from ui.styles.cyber import CyberHeader, cyber_section
from ui.styles.theme import C

class PluginOverlay(FadeInWidget):
    _OW, _OH = 520, 520

    def __init__(self, win: "MainWindow", parent=None):
        super().__init__(parent, duration=260)
        self._win = win
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 15, 18, 15)
        outer.setSpacing(8)
        header = CyberHeader("Plugins de confiance", "EXTENSIONS", parent=self)
        header.close_clicked.connect(self.hide)
        outer.addWidget(header)
        warning = QLabel("Un plugin est du code Python local : n’active que des fichiers fiables.")
        warning.setWordWrap(True)
        warning.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        outer.addWidget(warning)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        outer.addWidget(self._scroll, stretch=1)
        self.refresh()

    def refresh(self) -> None:
        rows = self._win.on_plugins_list() if self._win.on_plugins_list else []
        content = QWidget()
        content.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(content)
        layout.setSpacing(7)
        for row in rows:
            frame = cyber_section()
            line = QHBoxLayout(frame)
            error = row.get("error") or ""
            details = error or row.get("description") or row.get("file")
            if not error and row.get("version"):
                permissions = ", ".join(row.get("permissions") or []) or "aucune permission déclarée"
                details += f"\nv{row['version']} · {row.get('format', 'legacy')} · {permissions}"
            label = QLabel(f"{row.get('name')}\n{details}")
            label.setWordWrap(True)
            label.setStyleSheet(f"color: {C.RED if error else C.TEXT}; background: transparent;")
            line.addWidget(label, stretch=1)
            toggle = QPushButton("ERREUR" if error else ("ACTIF" if row.get("enabled") else "INACTIF"))
            toggle.setEnabled(not bool(error))
            toggle.setCheckable(True)
            toggle.setChecked(bool(row.get("enabled")))
            toggle.clicked.connect(lambda checked, name=row.get("name"): self._toggle(name, checked))
            line.addWidget(toggle)
            layout.addWidget(frame)
        if not rows:
            layout.addWidget(QLabel("Aucun plugin installé dans plugins/."))
        layout.addStretch()
        self._scroll.setWidget(content)

    def _toggle(self, name: str, enabled: bool) -> None:
        if self._win.on_plugin_toggle:
            self._win.on_plugin_toggle(name, enabled)
        self.refresh()
