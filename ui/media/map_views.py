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

from ui.core.qtflags import QWebEngineView
from ui.panels.floating_panel import FloatingPanel
from ui.styles.theme import C

class NearbyMapPanel(FloatingPanel):
    """Carte & Commerces Proches (Leaflet HTML + Dark Matter + Pins multiples)."""
    def __init__(self, parent=None):
        super().__init__("Carte & Commerces Proches", closeable=True, parent=parent)
        self.setFixedSize(520, 420)

        content = QWidget()
        lay = QVBoxLayout(content)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(4)

        try:
            from PyQt6.QtWebEngineWidgets import QWebEngineView
            self._web_view = QWebEngineView()
            self._web_view.setStyleSheet("background: #00060a; border-radius: 8px;")
            lay.addWidget(self._web_view, stretch=1)
        except Exception:
            self._web_view = None
            lbl = QLabel("Module WebEngine indisponible pour la carte.")
            lbl.setStyleSheet(f"color: {C.TEXT_DIM};")
            lay.addWidget(lbl)

        self.add_widget(content)

    def load_places(self, query: str, center_lat: float, center_lon: float, places: list):
        """Remplit le panneau de repli avec LA carte d'ANO-GPT.

        Ce panneau dupliquait autrefois son propre HTML Leaflet, resté à
        l'ancien style : une même recherche s'affichait en cyberpunk dans la
        grande carte et en gris pâle ici, selon la disponibilité de WebEngine.
        Une seule carte existe désormais, et ce panneau la réutilise.
        """
        if not self._web_view:
            return
        from core.map_render import render_map

        radius = max(1.0, max((p.get("dist_km") or 0) for p in places) * 1.4) \
            if places else 3.0
        html = render_map(
            query, (center_lat, center_lon), places=places, radius_km=radius,
            center_label="Votre position", mark_center=True,
        )
        # Une base distante est indispensable : sans elle la page est jugée
        # locale et le navigateur refuse Leaflet et les tuiles.
        self._web_view.setHtml(html, QUrl("https://unpkg.com/"))
        self.fade_in()
