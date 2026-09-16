from __future__ import annotations

import math
import random
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import psutil

from PyQt6.QtCore import (
    QEasingCurve,
    QParallelAnimationGroup,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QUrl,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QDesktopServices,
    QFont,
    QFontMetricsF,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QTextBrowser,
    QSlider,
    QVBoxLayout,
    QWidget,
)

# ── Import du thème ANO-GPT ou palette de secours autonome ───────────────────
try:
    from ui.styles.theme import C as _ThemeC
    from ui.styles.theme import make_svg_icon as _theme_svg_icon
    from ui.styles.theme import qcol as _theme_qcol
except Exception:
    _ThemeC = None
    _theme_svg_icon = None
    _theme_qcol = None


class CardTextBrowser(QTextBrowser):
    """Corps défilant avec barres de scroll masquées et défilement molette fluide."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setStyleSheet("""
            QTextBrowser {
                background: transparent;
                border: none;
                padding: 0px;
            }
            QScrollBar:vertical, QScrollBar:horizontal {
                width: 0px;
                height: 0px;
                background: transparent;
                border: none;
            }
        """)

    def wheelEvent(self, event):
        vbar = self.verticalScrollBar()
        if vbar and vbar.maximum() > 0:
            delta = event.angleDelta().y()
            vbar.setValue(vbar.value() - int(delta * 0.6))
            event.accept()
        else:
            super().wheelEvent(event)

    def text(self):
        return self.toPlainText()


class Theme:
    """Palette cybernétique néon & glassmorphism unifiée."""
    BG = getattr(_ThemeC, "BG", "#00060a")
    PANEL = getattr(_ThemeC, "PANEL", "#010d14")
    SURFACE = getattr(_ThemeC, "SURFACE", "#0a141c")
    SURFACE2 = getattr(_ThemeC, "SURFACE2", "#0e1a24")

    # Accents néon
    PRI = getattr(_ThemeC, "PRI", "#00d4ff")               # Cyan futuriste
    PRI_DIM = getattr(_ThemeC, "PRI_DIM", "#007a99")
    PRI_GLOW = getattr(_ThemeC, "PRI_GLOW", "rgba(0, 212, 255, 0.25)")
    NEON_PINK = getattr(_ThemeC, "NEON_PINK", "#ff2bd6")   # Rose magenta
    NEON_VIO = getattr(_ThemeC, "NEON_VIO", "#8f5cff")     # Violet cyber
    NEON_AMBER = getattr(_ThemeC, "NEON_AMBER", "#ffb300") # Ambre avertissement
    GREEN = getattr(_ThemeC, "GREEN", "#00ff88")           # Vert succès néon
    RED = getattr(_ThemeC, "RED", "#ff3355")               # Rouge alerte
    ACC = getattr(_ThemeC, "ACC", "#ff6b00")

    # Textes & encadrements (contraste WCAG AAA sur fond sombre)
    WHITE = getattr(_ThemeC, "WHITE", "#f0fbff")
    TEXT = getattr(_ThemeC, "TEXT", "#8ffcff")
    TEXT_MED = getattr(_ThemeC, "TEXT_MED", "#6dc8db")
    TEXT_DIM = getattr(_ThemeC, "TEXT_DIM", "#3a8a9a")
    BORDER_HAIR = "rgba(0, 212, 255, 0.16)"
    BORDER_BRIGHT = "rgba(0, 212, 255, 0.45)"

    # Base Glassmorphism optimisée
    GLASS_BG_COLOR = QColor(10, 16, 26, int(0.92 * 255))
    GLASS_BORDER_TOP = QColor(0, 212, 255, 210)
    GLASS_BORDER_MID = QColor(143, 92, 255, 140)
    GLASS_BORDER_BOT = QColor(0, 212, 255, 75)


def qcol(color_str: str, alpha: int = 255) -> QColor:
    """Création de QColor sécurisée avec gestion d'alpha."""
    if _theme_qcol:
        return _theme_qcol(color_str, alpha)
    c = QColor(color_str)
    c.setAlpha(alpha)
    return c


# ── Banque d'icônes SVG Lucide haute fidélité ─────────────────────────────────
SVG_ICONS: dict[str, str] = {
    "x": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>""",
    "music": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>""",
    "download": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" x2="12" y1="15" y2="3"/></svg>""",
    "folder": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/></svg>""",
    "play": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="{color}" stroke="{color}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><polygon points="6 3 20 12 6 21 6 3"/></svg>""",
    "pause": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="{color}" stroke="{color}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>""",
    "skip-back": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="19 20 9 12 19 4 19 20"/><line x1="5" x2="5" y1="19" y2="5"/></svg>""",
    "skip-forward": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="5 4 15 12 5 20 5 4"/><line x1="19" x2="19" y1="5" y2="19"/></svg>""",
    "shuffle": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 18h1.4c1.3 0 2.5-.6 3.3-1.7l6.1-8.6c.7-1.1 2-1.7 3.3-1.7H22"/><path d="m18 2 4 4-4 4"/><path d="M2 6h1.4c1.3 0 2.5.6 3.3 1.7l6.1 8.6c.7 1.1 2 1.7 3.3 1.7H22"/><path d="m18 14 4 4-4 4"/></svg>""",
    "volume-2": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/></svg>""",
    "sun": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.93 4.93 1.41 1.41"/><path d="m17.66 17.66 1.41 1.41"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.34 17.66-1.41 1.41"/><path d="m19.07 4.93-1.41 1.41"/></svg>""",
    "cloud": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.5 19H9a7 7 0 1 1 6.71-9h1.79a4.5 4.5 0 1 1 0 9Z"/></svg>""",
    "cloud-rain": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 14.899A7 7 0 1 1 15.71 8h1.79a4.5 4.5 0 0 1 2.5 8.242"/><path d="M16 14v6"/><path d="M8 14v6"/><path d="M12 16v6"/></svg>""",
    "wind": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.7 7.7a2.5 2.5 0 1 1 1.8 4.3H2"/><path d="M9.6 4.6A2 2 0 1 1 11 8H2"/><path d="M12.6 19.4A2 2 0 1 0 14 16H2"/></svg>""",
    "droplet": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22a7 7 0 0 0 7-7c0-2-1-3.9-3-5.5s-3.5-4-4-6.5c-.5 2.5-2 4.9-4 6.5C6 11.1 5 13 5 15a7 7 0 0 0 7 7z"/></svg>""",
    "cpu": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="16" height="16" x="4" y="4" rx="2"/><rect width="6" height="6" x="9" y="9" rx="1"/><path d="M15 2v2"/><path d="M15 20v2"/><path d="M2 15h2"/><path d="M2 9h2"/><path d="M20 15h2"/><path d="M20 9h2"/><path d="M9 2v2"/><path d="M9 20v2"/></svg>""",
    "activity": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>""",
    "wifi": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h.01"/><path d="M2 8.82a15 15 0 0 1 20 0"/><path d="M5 12.859a10 10 0 0 1 14 0"/><path d="M8.5 16.429a5 5 0 0 1 7 0"/></svg>""",
    "terminal": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 17 10 11 4 5"/><line x1="12" x2="20" y1="19" y2="19"/></svg>""",
    "check": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>""",
    "clock": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>""",
    "alert-triangle": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg>""",
    "info": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/></svg>""",
    "umbrella": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 12a10.06 10.06 0 0 0-20 0Z"/><path d="M12 12v8a2 2 0 0 0 4 0"/><path d="M12 2v1"/></svg>""",
    "shield-alert": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>""",
}


def render_icon(name: str, color_hex: str, size: int = 20) -> QPixmap:
    """Génère un QPixmap vectoriel net à partir de la banque Lucide."""
    tpl = SVG_ICONS.get(name)
    if not tpl:
        tpl = SVG_ICONS.get("info", "")
    xml = tpl.format(color=color_hex).encode("utf-8")
    renderer = QSvgRenderer(xml)
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter)
    painter.end()
    return pm


# ── Micro-composants réutilisables ───────────────────────────────────────────

class LiveIndicator(QWidget):
    """Indicateur lumineux pulsant 'LIVE' avec LED à respiration sinusoïdale."""

    def __init__(self, label: str = "LIVE", color: str = Theme.PRI, parent=None):
        super().__init__(parent)
        self._label = label
        self._color = qcol(color)
        self._phase = 0.0
        self.setFixedHeight(18)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._pulse)
        self._timer.start(50)

    def _pulse(self):
        if not self.isVisible():
            return
        self._phase = (self._phase + 0.12) % (2.0 * math.pi)
        self.update()

    def set_active(self, active: bool):
        if active and not self._timer.isActive():
            self._timer.start(50)
        elif not active and self._timer.isActive():
            self._timer.stop()
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Calcul d'intensité lumineuse
        glow = 0.35 + 0.65 * (0.5 + 0.5 * math.sin(self._phase))
        cy = self.height() / 2.0

        # Halo diffus
        halo = QRadialGradient(6, cy, 6)
        c_glow = QColor(self._color)
        c_glow.setAlpha(int(80 * glow))
        halo.setColorAt(0.0, c_glow)
        halo.setColorAt(1.0, QColor(self._color.red(), self._color.green(), self._color.blue(), 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(halo))
        p.drawEllipse(QPointF(6, cy), 6, 6)

        # Cœur de la LED
        c_core = QColor(self._color)
        c_core.setAlpha(int(255 * (0.7 + 0.3 * glow)))
        p.setBrush(QBrush(c_core))
        p.drawEllipse(QPointF(6, cy), 2.5, 2.5)

        # Intitulé micro-caps
        if self._label:
            f = QFont("Inter", 6, QFont.Weight.Bold)
            f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.4)
            p.setFont(f)
            p.setPen(QPen(qcol(Theme.TEXT_MED, int(210 * (0.8 + 0.2 * glow))), 1))
            p.drawText(QRectF(13, 0, self.width() - 13, self.height()),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       self._label)
        p.end()


class MarqueeLabel(QLabel):
    """Titre avec défilement horizontal fluide automatique si débordement."""

    _GAP = 36.0

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._text = text
        self._offset = 0.0
        self._pause = 30
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setStyleSheet("background: transparent; border: none;")

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    def setText(self, text: str):
        super().setText(text)
        self._text = text
        self._offset = 0.0
        self._pause = 30
        self._sync()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync()

    def _sync(self):
        overflow = self._text_width() - self.width()
        if overflow > 4 and not self._timer.isActive():
            self._timer.start(40)
        elif overflow <= 4 and self._timer.isActive():
            self._timer.stop()
            self._offset = 0.0
            self.update()

    def _text_width(self) -> float:
        return QFontMetricsF(self.font()).horizontalAdvance(self._text)

    def _tick(self):
        if not self.isVisible():
            return
        if self._pause > 0:
            self._pause -= 1
            return
        self._offset += 0.8
        if self._offset > self._text_width() + self._GAP:
            self._offset = 0.0
            self._pause = 30
        self.update()

    def paintEvent(self, event):
        if not self._timer.isActive():
            super().paintEvent(event)
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p.setFont(self.font())
        p.setPen(self.palette().color(self.foregroundRole()))

        metrics = QFontMetricsF(self.font())
        y = self.height() / 2.0 + metrics.capHeight() / 2.0
        tw = self._text_width()

        # Dessin en boucle double pour une continuité parfaite
        p.drawText(QPointF(-self._offset, y), self._text)
        p.drawText(QPointF(-self._offset + tw + self._GAP, y), self._text)
        p.end()


class SparklineGraph(QWidget):
    """Graphe technique haute cadence en temps réel avec grille néon et point de tête."""

    def __init__(self, max_points: int = 32, accent_color: str = Theme.PRI, parent=None):
        super().__init__(parent)
        self._max_points = max_points
        self._accent = qcol(accent_color)
        self._history: List[float] = [0.0] * max_points
        self.setFixedHeight(44)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    def add_sample(self, value: float):
        """Ajoute une valeur (0.0 à 100.0)."""
        val = max(0.0, min(100.0, float(value)))
        self._history.pop(0)
        self._history.append(val)
        self.update()

    def set_data(self, values: List[float]):
        if len(values) >= self._max_points:
            self._history = list(values[-self._max_points:])
        else:
            self._history = [0.0] * (self._max_points - len(values)) + list(values)
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = float(self.width()), float(self.height())
        if w < 10 or h < 10:
            p.end()
            return

        # Grille millimétrique d'arrière-plan
        p.setPen(QPen(QColor(0, 212, 255, 18), 1, Qt.PenStyle.DotLine))
        p.drawLine(QPointF(0, h * 0.25), QPointF(w, h * 0.25))
        p.drawLine(QPointF(0, h * 0.50), QPointF(w, h * 0.50))
        p.drawLine(QPointF(0, h * 0.75), QPointF(w, h * 0.75))

        # Construction de la courbe
        dx = w / max(1, self._max_points - 1)
        path = QPainterPath()
        fill_path = QPainterPath()

        points: List[QPointF] = []
        for i, val in enumerate(self._history):
            y = h - (val / 100.0 * (h - 6)) - 3
            pt = QPointF(i * dx, y)
            points.append(pt)

        if not points:
            p.end()
            return

        path.moveTo(points[0])
        fill_path.moveTo(QPointF(0, h))
        fill_path.lineTo(points[0])

        for i in range(1, len(points)):
            p0 = points[i - 1]
            p1 = points[i]
            cpx = (p0.x() + p1.x()) / 2.0
            path.cubicTo(cpx, p0.y(), cpx, p1.y(), p1.x(), p1.y())
            fill_path.cubicTo(cpx, p0.y(), cpx, p1.y(), p1.x(), p1.y())

        last_pt = points[-1]
        fill_path.lineTo(QPointF(last_pt.x(), h))
        fill_path.closeSubpath()

        # Dégradé d'aire sous la courbe
        grad = QLinearGradient(0, 0, 0, h)
        grad.setColorAt(0.0, QColor(self._accent.red(), self._accent.green(), self._accent.blue(), 90))
        grad.setColorAt(0.7, QColor(self._accent.red(), self._accent.green(), self._accent.blue(), 25))
        grad.setColorAt(1.0, QColor(self._accent.red(), self._accent.green(), self._accent.blue(), 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(grad))
        p.drawPath(fill_path)

        # Ligne de contour néon
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(self._accent, 1.6))
        p.drawPath(path)

        # Point indicateur de tête avec aura lumineuse
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        aura = QRadialGradient(last_pt.x(), last_pt.y(), 8)
        aura.setColorAt(0.0, QColor(self._accent.red(), self._accent.green(), self._accent.blue(), 180))
        aura.setColorAt(1.0, QColor(self._accent.red(), self._accent.green(), self._accent.blue(), 0))
        p.setBrush(QBrush(aura))
        p.drawEllipse(last_pt, 8, 8)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

        p.setBrush(QBrush(qcol(Theme.WHITE)))
        p.setPen(QPen(self._accent, 1))
        p.drawEllipse(last_pt, 2.2, 2.2)
        p.end()


class InteractiveSeekSlider(QSlider):
    """Barre de timeline interactive cliquable à n'importe quel point."""
    seek_moved_ratio = pyqtSignal(float)

    def __init__(self, parent=None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.setRange(0, 1000)
        self.setFixedHeight(18)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setStyleSheet("background: transparent;")

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            ratio = min(1.0, max(0.0, e.position().x() / max(1.0, float(self.width()))))
            val = round(self.minimum() + ratio * (self.maximum() - self.minimum()))
            self.setValue(val)
            self.seek_moved_ratio.emit(ratio)
            e.accept()
        super().mousePressEvent(e)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = float(self.width()), float(self.height())
        mid_y = h / 2.0

        span = max(1, self.maximum() - self.minimum())
        ratio = (self.value() - self.minimum()) / span
        head_x = max(0.0, min(1.0, ratio)) * w

        # Rail de fond
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(10, 20, 30, 220)))
        p.drawRoundedRect(QRectF(0, mid_y - 2.5, w, 5), 2.5, 2.5)

        # Remplissage néon progressif
        if head_x > 1.0:
            fill_grad = QLinearGradient(0, 0, head_x, 0)
            fill_grad.setColorAt(0.0, qcol(Theme.PRI_DIM))
            fill_grad.setColorAt(0.7, qcol(Theme.PRI))
            fill_grad.setColorAt(1.0, qcol(Theme.NEON_PINK))
            p.setBrush(QBrush(fill_grad))
            p.drawRoundedRect(QRectF(0, mid_y - 2.5, head_x, 5), 2.5, 2.5)

            # Tête de lecture avec halo
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
            glow = QRadialGradient(head_x, mid_y, 9)
            glow.setColorAt(0.0, qcol(Theme.PRI, 150))
            glow.setColorAt(1.0, qcol(Theme.PRI, 0))
            p.setBrush(QBrush(glow))
            p.drawEllipse(QPointF(head_x, mid_y), 9, 9)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

            # Curseur
            p.setBrush(QBrush(qcol(Theme.WHITE)))
            p.setPen(QPen(qcol(Theme.PRI), 1.2))
            p.drawEllipse(QPointF(head_x, mid_y), 4, 4)
        p.end()


class AudioSpectrumWidget(QWidget):
    """Égaliseur audio à 20 barres fréquentielles réactives."""

    _BAR_COUNT = 20

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(22)
        self._t = 0.0
        self._level = 0.0
        self._target_level = 0.0
        self._seeds = [0.8 + 2.2 * random.random() for _ in range(self._BAR_COUNT)]
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(50)

    def set_playing(self, playing: bool):
        self._target_level = 1.0 if playing else 0.0

    def _tick(self):
        if not self.isVisible():
            return
        self._t += 0.18
        diff = self._target_level - self._level
        if abs(diff) > 0.02:
            self._level += diff * 0.15
            self.update()
        elif self._level > 0.01:
            self.update()

    def paintEvent(self, event):
        if self._level <= 0.01:
            return
        p = QPainter(self)
        w, h = float(self.width()), float(self.height())
        gap = 2.5
        bar_w = max(1.5, (w - gap * (self._BAR_COUNT - 1)) / self._BAR_COUNT)

        grad = QLinearGradient(0, 0, 0, h)
        grad.setColorAt(0.0, qcol(Theme.NEON_PINK, 220))
        grad.setColorAt(0.5, qcol(Theme.PRI, 200))
        grad.setColorAt(1.0, qcol(Theme.PRI, 50))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(grad))
        p.setOpacity(self._level)

        for i in range(self._BAR_COUNT):
            wave = 0.5 + 0.5 * math.sin(self._t * self._seeds[i] + i * 0.6)
            envelope = 0.4 + 0.6 * math.sin(math.pi * (i + 0.5) / self._BAR_COUNT)
            bh = max(2.0, wave * envelope * (h - 2) * self._level)
            x = i * (bar_w + gap)
            p.drawRect(QRectF(x, h - bh, bar_w, bh))
        p.end()


class AnimatedWeatherWidget(QWidget):
    """Icône météo dynamique avec rendu d'effets visuels procéduraux."""

    def __init__(self, condition: str = "sunny", parent=None):
        super().__init__(parent)
        self._condition = condition.lower()
        self.setFixedSize(56, 56)
        self._phase = 0.0
        self._raindrops = [
            {"x": random.uniform(8, 48), "y": random.uniform(28, 54), "speed": random.uniform(1.2, 2.2)}
            for _ in range(7)
        ]
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._animate)
        self._timer.start(45)

    def set_condition(self, cond: str):
        self._condition = cond.lower()
        self.update()

    def _animate(self):
        if not self.isVisible():
            return
        self._phase = (self._phase + 0.08) % (2 * math.pi)
        if "rain" in self._condition or "pluie" in self._condition:
            for drop in self._raindrops:
                drop["y"] += drop["speed"]
                drop["x"] += 0.35  # Légère dérive au vent
                if drop["y"] > 54:
                    drop["y"] = 28
                    drop["x"] = random.uniform(8, 44)
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx, cy = 28.0, 28.0

        if "sun" in self._condition or "clair" in self._condition or "soleil" in self._condition:
            # Soleil avec rayons tournants
            sun_pulse = 0.85 + 0.15 * math.sin(self._phase * 1.5)
            p.save()
            p.translate(cx, cy)
            p.rotate(math.degrees(self._phase * 0.4))
            p.setPen(QPen(qcol(Theme.NEON_AMBER, 160), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            for i in range(8):
                ang = i * (math.pi / 4.0)
                p.drawLine(QPointF(11 * math.cos(ang), 11 * math.sin(ang)),
                           QPointF(17 * math.cos(ang), 17 * math.sin(ang)))
            p.restore()

            # Disque solaire rayonnant
            halo = QRadialGradient(cx, cy, 14)
            halo.setColorAt(0.0, qcol(Theme.NEON_AMBER, 230))
            halo.setColorAt(0.8, qcol(Theme.ACC, 180))
            halo.setColorAt(1.0, qcol(Theme.ACC, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(halo))
            p.drawEllipse(QPointF(cx, cy), 12 * sun_pulse, 12 * sun_pulse)

            p.setBrush(QBrush(qcol(Theme.WHITE)))
            p.drawEllipse(QPointF(cx, cy), 6 * sun_pulse, 6 * sun_pulse)

        elif "rain" in self._condition or "pluie" in self._condition:
            # Nuage bleu/gris avec pluie animée
            p.setPen(Qt.PenStyle.NoPen)
            cloud_grad = QLinearGradient(12, 16, 44, 34)
            cloud_grad.setColorAt(0.0, QColor(40, 70, 95, 230))
            cloud_grad.setColorAt(1.0, QColor(20, 35, 55, 240))
            p.setBrush(QBrush(cloud_grad))

            # Dessin de nuage composite
            p.drawEllipse(QPointF(20, 24), 9, 9)
            p.drawEllipse(QPointF(32, 22), 12, 12)
            p.drawEllipse(QPointF(40, 26), 8, 8)
            p.drawRoundedRect(QRectF(14, 25, 30, 11), 5, 5)

            # Gouttes de pluie
            p.setPen(QPen(qcol(Theme.PRI, 190), 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            for drop in self._raindrops:
                p.drawLine(QPointF(drop["x"], drop["y"]), QPointF(drop["x"] + 1.5, drop["y"] + 4.5))

        else:
            # Couvert / Nuageux
            p.setPen(Qt.PenStyle.NoPen)
            cloud_grad = QLinearGradient(12, 16, 44, 34)
            cloud_grad.setColorAt(0.0, QColor(60, 95, 125, 220))
            cloud_grad.setColorAt(1.0, QColor(25, 45, 65, 230))
            p.setBrush(QBrush(cloud_grad))
            p.drawEllipse(QPointF(20, 26), 9, 9)
            p.drawEllipse(QPointF(32, 22), 13, 13)
            p.drawEllipse(QPointF(42, 27), 8, 8)
            p.drawRoundedRect(QRectF(14, 27, 32, 12), 6, 6)

            # Bordure de brillance
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(qcol(Theme.PRI, 110), 1.2))
            p.drawArc(QRectF(19, 9, 26, 26), 30 * 16, 120 * 16)
        p.end()


# ── Composant de base : GlassCard(QFrame) ────────────────────────────────────

class GlassCard(QFrame):
    """Composant de base avec effet glassmorphism haute fidélité.

    Caractéristiques :
    - Fond noir translucide rgba(15, 15, 25, 0.85)
    - Bordure néon 1px avec dégradé subtil (cyan -> magenta)
    - En-tête standardisé (icône, titre, live-dot, horodatage, bouton x)
    - Animations d'entrée/sortie via QPropertyAnimation (glissement + opacité)
    - Compte à rebours auto-dismiss avec mise en pause au survol de la souris
    """

    closed = pyqtSignal()
    action_triggered = pyqtSignal(dict)
    dismiss_requested = pyqtSignal()

    CARD_WIDTH = 340

    def __init__(
        self,
        category: str = "SYSTÈME",
        title: str = "NOTIFICATION",
        icon_name: str = "info",
        accent_color: str = Theme.PRI,
        auto_dismiss_s: float = 0.0,
        parent=None,
    ):
        super().__init__(parent)
        self.category = category
        self.card_type = category.lower()
        self.card_title = title
        self.icon_name = icon_name
        self.accent_color = accent_color
        self._accent = qcol(accent_color)
        self.auto_dismiss_s = max(0.0, float(auto_dismiss_s))
        self._dismiss_remaining = self.auto_dismiss_s
        self._hovered = False
        self._is_closing = False
        self.pinned = False
        self._action_done = False
        self._cancel_callback = None

        self.setObjectName("GlassCard")
        self.setFixedWidth(self.CARD_WIDTH)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setStyleSheet("QFrame#GlassCard { background: transparent; border: none; }")

        # Effet d'opacité graphique pour l'animation
        self._opacity_effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._opacity_effect)
        self._opacity_effect.setOpacity(1.0)

        # Minuteur pour la barre de progression auto-dismiss
        self._dismiss_timer = QTimer(self)
        self._dismiss_timer.setInterval(30)
        self._dismiss_timer.timeout.connect(self._tick_dismiss)

        # Layout principal de la carte
        self._main_layout = QVBoxLayout(self)
        self._main_layout.setContentsMargins(14, 12, 14, 14)
        self._main_layout.setSpacing(8)

        # En-tête standardisé
        self._build_header()

        # Conteneur dédié pour le corps de la carte
        self._content_widget = QWidget(self)
        self._content_widget.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._content_layout = QVBoxLayout(self._content_widget)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        self._content_layout.setSpacing(6)
        self._main_layout.addWidget(self._content_widget)

        if self.auto_dismiss_s > 0:
            self._dismiss_timer.start()

    def _build_header(self):
        """Construit l'en-tête standardisé HUD."""
        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(8)

        # 1. Pastille avec icône thématique
        self._icon_label = QLabel(self)
        self._icon_label.setFixedSize(26, 26)
        self._icon_label.setPixmap(render_icon(self.icon_name, self.accent_color, 16))
        self._icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._icon_label.setStyleSheet(
            f"background: rgba({self._accent.red()}, {self._accent.green()}, {self._accent.blue()}, 0.16);"
            f"border: 1px solid rgba({self._accent.red()}, {self._accent.green()}, {self._accent.blue()}, 0.45);"
            "border-radius: 5px;"
        )
        header_row.addWidget(self._icon_label, alignment=Qt.AlignmentFlag.AlignVCenter)

        # 2. Colonne Titre & Catégorie
        title_box = QVBoxLayout()
        title_box.setContentsMargins(0, 0, 0, 0)
        title_box.setSpacing(1)

        self._cat_label = QLabel(self.category.upper(), self)
        f_cat = QFont("Inter", 6, QFont.Weight.Bold)
        f_cat.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.8)
        self._cat_label.setFont(f_cat)
        self._cat_label.setStyleSheet("color: rgba(0, 212, 255, 0.75); background: transparent;")
        title_box.addWidget(self._cat_label)

        self._title_label = QLabel(self.card_title[:40], self)
        f_title = QFont("Inter", 9, QFont.Weight.Bold)
        self._title_label.setFont(f_title)
        self._title_label.setStyleSheet(f"color: {Theme.WHITE}; background: transparent;")
        title_box.addWidget(self._title_label)
        header_row.addLayout(title_box, stretch=1)

        # 3. Indicateur de mise à jour en direct
        self._live_indicator = LiveIndicator("DIRECT", self.accent_color, self)
        header_row.addWidget(self._live_indicator, alignment=Qt.AlignmentFlag.AlignVCenter)

        # 4. Horodatage
        self._time_label = QLabel(time.strftime("%H:%M"), self)
        self._time_label.setFont(QFont("Inter", 7, QFont.Weight.Bold))
        self._time_label.setStyleSheet(f"color: {Theme.TEXT_DIM}; background: transparent;")
        header_row.addWidget(self._time_label, alignment=Qt.AlignmentFlag.AlignVCenter)

        # 5. Bouton de fermeture stylisé
        self._close_btn = QPushButton(self)
        self._close_btn.setFixedSize(22, 22)
        self._close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close_btn.setIcon(QIcon(render_icon("x", Theme.TEXT_DIM, 12)))
        self._close_btn.setIconSize(QSize(12, 12))
        self._close_btn.setStyleSheet(f"""
            QPushButton {{
                background: rgba(255, 255, 255, 0.04);
                border: 1px solid rgba(255, 255, 255, 0.09);
                border-radius: 4px;
            }}
            QPushButton:hover {{
                background: rgba(255, 51, 85, 0.25);
                border: 1px solid {Theme.RED};
            }}
        """)
        self._close_btn.clicked.connect(self.dismiss)
        header_row.addWidget(self._close_btn, alignment=Qt.AlignmentFlag.AlignVCenter)

        self._main_layout.addLayout(header_row)

        # Séparateur subtil néon
        sep = QFrame(self)
        sep.setFixedHeight(1)
        sep.setStyleSheet(
            "background: qlineargradient(x1:0, y1:0, x2:1, y2:0,"
            f" stop:0 {self.accent_color},"
            " stop:0.4 rgba(143, 92, 255, 0.45),"
            " stop:0.8 rgba(0, 212, 255, 0.25),"
            " stop:1 transparent); border: none;"
        )
        self._main_layout.addWidget(sep)

    def add_widget(self, widget: QWidget):
        """Ajoute un sous-composant dans le corps de la carte."""
        self._content_layout.addWidget(widget)

    def add_layout(self, layout):
        """Ajoute une disposition dans le corps de la carte."""
        self._content_layout.addLayout(layout)

    def set_body(self, body: str) -> None:
        """Met à jour le texte d'une carte générique en place avec barre de scroll masquée."""
        self._body_text = str(body or "")
        label = getattr(self, "_body_label", None)
        if label is None:
            label = CardTextBrowser(self)
            label.setReadOnly(True)
            from core.browser_policy import open_chrome
            label.setOpenExternalLinks(False)
            label.setOpenLinks(False)
            label.anchorClicked.connect(lambda url: open_chrome(url.toString()))
            label.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            label.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            label.setFont(QFont("Inter", 10))
            label.setStyleSheet(f"""
                QTextBrowser {{
                    color: {Theme.WHITE};
                    background: transparent;
                    border: none;
                    padding: 2px 0px;
                    selection-background-color: {Theme.PRI_DIM};
                }}
                QScrollBar:vertical, QScrollBar:horizontal {{
                    width: 0px;
                    height: 0px;
                    background: transparent;
                    border: none;
                }}
            """)
            self._body_label = label
            self.add_widget(label)
        label.setMarkdown(body)
        # Mesurer une copie : avant la première mise en page, le viewport fait
        # souvent 100 px et réécrit la largeur du document pendant son calcul.
        measured = label.document().clone()
        measured.setTextWidth(max(100, self.width() - 56))
        label.setFixedHeight(max(36, min(200, math.ceil(measured.size().height()) + 10)))
        self.updateGeometry()

    def close_card(self) -> None:
        """Compatibilité avec l'ancienne API ``RichCardWidget``."""
        self.dismiss()

    def set_header(self, title: str | None = None, category: str | None = None,
                   icon_name: str | None = None, accent_color: str | None = None) -> None:
        """Change titre, catégorie, icône ou couleur d'accent d'une carte en place."""
        if title is not None:
            self.card_title = title
            self._title_label.setText(title[:40])
        if category is not None:
            self.category = category
            self.card_type = category.lower()
            self._cat_label.setText(category.upper())
        if accent_color is not None:
            self.accent_color = accent_color
            self._accent = qcol(accent_color)
            self._live_indicator._color = qcol(accent_color)
        if icon_name is not None:
            self.icon_name = icon_name
        if icon_name is not None or accent_color is not None:
            self._icon_label.setPixmap(render_icon(self.icon_name, self.accent_color, 16))
            self._icon_label.setStyleSheet(
                f"background: rgba({self._accent.red()}, {self._accent.green()}, {self._accent.blue()}, 0.16);"
                f"border: 1px solid rgba({self._accent.red()}, {self._accent.green()}, {self._accent.blue()}, 0.45);"
                "border-radius: 5px;"
            )
        self.update()

    def start_auto_dismiss(self, seconds: float) -> None:
        """Arme (ou réarme) la disparition automatique."""
        self.auto_dismiss_s = max(0.0, float(seconds))
        self._dismiss_remaining = self.auto_dismiss_s
        if self.auto_dismiss_s > 0 and not self._is_closing and not self._hovered:
            self._dismiss_timer.start()

    def set_live_text(self, text: str, active: bool = True):
        self._live_indicator._label = text
        self._live_indicator.set_active(active)

    def enterEvent(self, event):
        self._hovered = True
        if self._dismiss_timer.isActive():
            self._dismiss_timer.stop()
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        if self.auto_dismiss_s > 0 and self._dismiss_remaining > 0 and not self._is_closing:
            self._dismiss_timer.start()
        self.update()
        super().leaveEvent(event)

    def _tick_dismiss(self):
        self._dismiss_remaining -= 0.03
        if self._dismiss_remaining <= 0:
            self._dismiss_timer.stop()
            self.dismiss()
        else:
            self.update()

    def dismiss(self):
        """Déclenche la fermeture avec animation de sortie."""
        if self._is_closing:
            return
        self._is_closing = True
        self._dismiss_timer.stop()
        if not getattr(self, "_action_done", False):
            self._action_done = True
            cancel_cb = getattr(self, "_cancel_callback", None)
            if callable(cancel_cb):
                try:
                    cancel_cb()
                except Exception:
                    pass
        self.dismiss_requested.emit()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = float(self.width()), float(self.height())
        if w < 16 or h < 16:
            p.end()
            return
        rect = QRectF(2.0, 2.0, w - 4.0, h - 4.0)

        # Contour chanfreiné cybernétique
        cut = 11.0
        path = QPainterPath()
        path.moveTo(rect.left() + cut, rect.top())
        path.lineTo(rect.right() - cut, rect.top())
        path.lineTo(rect.right(), rect.top() + cut)
        path.lineTo(rect.right(), rect.bottom() - cut)
        path.lineTo(rect.right() - cut, rect.bottom())
        path.lineTo(rect.left() + cut, rect.bottom())
        path.lineTo(rect.left(), rect.bottom() - cut)
        path.lineTo(rect.left(), rect.top() + cut)
        path.closeSubpath()

        # 1. Double passe : Ombre portée diffuse d'occlusion + Halo néon d'ambiance projeté
        p.setPen(QPen(QColor(0, 0, 0, 140), 3.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)

        glow_col = QColor(self._accent)
        glow_col.setAlpha(65 if not self._hovered else 115)
        p.setPen(QPen(glow_col, 2.2 if not self._hovered else 3.2))
        p.drawPath(path)

        # 2. Fond noir translucide dégradé riche (Glassmorphism OLED)
        p.setPen(Qt.PenStyle.NoPen)
        bg_grad = QLinearGradient(rect.left(), rect.top(), rect.right(), rect.bottom())
        bg_grad.setColorAt(0.0, QColor(14, 23, 36, int(0.92 * 255)))
        bg_grad.setColorAt(0.4, QColor(10, 17, 27, int(0.93 * 255)))
        bg_grad.setColorAt(1.0, QColor(5, 10, 17, int(0.96 * 255)))
        p.setBrush(QBrush(bg_grad))
        p.drawPath(path)

        # 3. Micro-texture holographique discrète (scanlines cyber)
        p.save()
        p.setClipPath(path)
        p.setPen(QPen(QColor(0, 212, 255, 5), 1))
        scan_y = rect.top() + 4.0
        while scan_y < rect.bottom() - 4.0:
            p.drawLine(QPointF(rect.left(), scan_y), QPointF(rect.right(), scan_y))
            scan_y += 6.0
        p.restore()

        # 4. Reflet spéculaire supérieur (Physical glass highlight)
        spec = QLinearGradient(rect.left() + cut, rect.top(), rect.right() - cut, rect.top())
        spec.setColorAt(0.0, QColor(255, 255, 255, 0))
        spec.setColorAt(0.2, QColor(255, 255, 255, 55))
        spec.setColorAt(0.5, QColor(220, 250, 255, 150))
        spec.setColorAt(0.8, QColor(255, 255, 255, 55))
        spec.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setPen(QPen(QBrush(spec), 1.2))
        p.drawLine(QPointF(rect.left() + cut + 2, rect.top() + 1),
                   QPointF(rect.right() - cut - 2, rect.top() + 1))

        # 5. Bordure néon avec dégradé cybernétique
        border_grad = QLinearGradient(rect.left(), rect.top(), rect.right(), rect.bottom())
        if self._hovered:
            border_grad.setColorAt(0.0, qcol(self.accent_color, 255))
            border_grad.setColorAt(0.45, qcol(Theme.NEON_PINK, 220))
            border_grad.setColorAt(0.8, qcol(Theme.NEON_VIO, 200))
            border_grad.setColorAt(1.0, qcol(self.accent_color, 160))
            p.setPen(QPen(QBrush(border_grad), 1.35))
        else:
            border_grad.setColorAt(0.0, qcol(self.accent_color, 195))
            border_grad.setColorAt(0.4, Theme.GLASS_BORDER_MID)
            border_grad.setColorAt(0.8, Theme.GLASS_BORDER_BOT)
            border_grad.setColorAt(1.0, qcol(self.accent_color, 100))
            p.setPen(QPen(QBrush(border_grad), 1.1))

        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)

        # 6. Équerres d'angles Sci-Fi HUD haute précision
        p.setPen(QPen(qcol(self.accent_color, 220 if not self._hovered else 255), 1.8))
        # Angle supérieur gauche (tick)
        p.drawLine(QPointF(rect.left() + cut, rect.top()), QPointF(rect.left(), rect.top() + cut))
        # Angle supérieur droit (bracket HUD complet)
        p.drawLine(QPointF(rect.right() - cut, rect.top()), QPointF(rect.right(), rect.top() + cut))
        p.drawLine(QPointF(rect.right() - cut - 5, rect.top()), QPointF(rect.right() - cut, rect.top()))
        # Angle inférieur gauche (bracket HUD complet)
        p.drawLine(QPointF(rect.left() + cut, rect.bottom()), QPointF(rect.left(), rect.bottom() - cut))
        p.drawLine(QPointF(rect.left() + cut + 5, rect.bottom()), QPointF(rect.left(), rect.bottom() - cut))
        # Angle inférieur droit (tick)
        p.drawLine(QPointF(rect.right() - cut, rect.bottom()), QPointF(rect.right(), rect.bottom() - cut))

        # 7. Barre de compte à rebours auto-dismiss
        if self.auto_dismiss_s > 0 and self._dismiss_remaining > 0:
            ratio = max(0.0, min(1.0, self._dismiss_remaining / self.auto_dismiss_s))
            prog_w = (rect.width() - cut * 2) * ratio
            if prog_w > 1:
                p.setPen(Qt.PenStyle.NoPen)
                prog_grad = QLinearGradient(rect.left() + cut, 0, rect.left() + cut + prog_w, 0)
                prog_grad.setColorAt(0.0, qcol(self.accent_color, 220))
                prog_grad.setColorAt(0.7, qcol(Theme.PRI, 220))
                prog_grad.setColorAt(1.0, qcol(Theme.WHITE, 255))
                p.setBrush(QBrush(prog_grad))
                p.drawRoundedRect(QRectF(rect.left() + cut, rect.bottom() - 3.0, prog_w, 2.5), 1.2, 1.2)
                p.setBrush(QBrush(qcol(Theme.WHITE)))
                p.drawEllipse(QPointF(rect.left() + cut + prog_w, rect.bottom() - 1.75), 2.2, 2.2)

        p.end()
        super().paintEvent(event)


# ── Cartes spécialisées modulaires ───────────────────────────────────────────

class MediaCard(GlassCard):
    """MediaCard : Pochette album, titre défilant, timeline interactive, contrôles."""

    play_toggled = pyqtSignal(bool)
    next_clicked = pyqtSignal()
    prev_clicked = pyqtSignal()
    seek_requested = pyqtSignal(float)

    def __init__(
        self,
        title: str = "Aucune lecture",
        artist: str = "ANO-GPT Audio",
        album: str = "Système",
        duration_s: float = 180.0,
        source: str = "YouTube",
        parent=None,
    ):
        super().__init__(
            category="LECTEUR MULTIMÉDIA",
            title=f"{source.upper()} AUDIO",
            icon_name="music",
            accent_color=Theme.NEON_PINK,
            parent=parent,
        )
        self.pinned = True
        self._is_playing = False
        self._duration_s = max(1.0, duration_s)
        self._current_pos_s = 0.0
        self._source = source
        self._ring_angle = 0.0

        self._build_media_ui(title, artist, album)

        # Minuteur de rotation de l'anneau de pochette
        self._spin_timer = QTimer(self)
        self._spin_timer.timeout.connect(self._spin_ring)

    def _build_media_ui(self, title: str, artist: str, album: str):
        # Ligne supérieure : pochette + infos + visualiseur
        top_row = QHBoxLayout()
        top_row.setSpacing(12)

        # Vignette pochette avec anneau néon
        self._cover_box = QWidget(self)
        self._cover_box.setFixedSize(58, 58)
        self._cover_box.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._cover_box.paintEvent = self._paint_cover
        top_row.addWidget(self._cover_box)

        # Informations texte
        meta_col = QVBoxLayout()
        meta_col.setContentsMargins(0, 2, 0, 2)
        meta_col.setSpacing(2)

        self._title_marquee = MarqueeLabel(title, self)
        self._title_marquee.setFont(QFont("Inter", 10, QFont.Weight.Bold))
        self._title_marquee.setStyleSheet(f"color: {Theme.WHITE};")
        self._title_marquee.setFixedHeight(18)
        meta_col.addWidget(self._title_marquee)

        self._artist_label = QLabel(f"{artist} • {album}", self)
        self._artist_label.setFont(QFont("Inter", 8))
        self._artist_label.setStyleSheet(f"color: {Theme.TEXT_MED}; background: transparent;")
        meta_col.addWidget(self._artist_label)

        # Badge source & codec
        codec_badge = QLabel(f"● {self._source.upper()} • 48 kHz / 24-bit Hi-Fi", self)
        f_codec = QFont("Inter", 6, QFont.Weight.Bold)
        f_codec.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.2)
        codec_badge.setFont(f_codec)
        codec_badge.setStyleSheet("color: rgba(0, 212, 255, 0.7); background: transparent;")
        meta_col.addWidget(codec_badge)

        top_row.addLayout(meta_col, stretch=1)
        self.add_layout(top_row)

        # Égaliseur de spectre
        self._spectrum = AudioSpectrumWidget(self)
        self.add_widget(self._spectrum)

        # Timeline interactive
        time_row = QHBoxLayout()
        time_row.setSpacing(8)

        self._lbl_elapsed = QLabel("00:00", self)
        self._lbl_elapsed.setFont(QFont("Inter", 7, QFont.Weight.Bold))
        self._lbl_elapsed.setStyleSheet(f"color: {Theme.PRI}; background: transparent;")
        time_row.addWidget(self._lbl_elapsed)

        self._seek_slider = InteractiveSeekSlider(self)
        self._seek_slider.seek_moved_ratio.connect(self._on_seek_ratio)
        time_row.addWidget(self._seek_slider, stretch=1)

        self._lbl_total = QLabel(self._format_time(self._duration_s), self)
        self._lbl_total.setFont(QFont("Inter", 7, QFont.Weight.Bold))
        self._lbl_total.setStyleSheet(f"color: {Theme.TEXT_DIM}; background: transparent;")
        time_row.addWidget(self._lbl_total)

        self.add_layout(time_row)

        # Barre des boutons de contrôle
        ctrl_row = QHBoxLayout()
        ctrl_row.setSpacing(8)
        ctrl_row.addStretch()

        def make_ctrl(icon_name: str, tip: str, primary: bool = False, w: int = 30) -> QPushButton:
            btn = QPushButton(self)
            btn.setFixedSize(w, 26)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip(tip)
            col = Theme.WHITE if primary else Theme.TEXT_MED
            btn.setIcon(QIcon(render_icon(icon_name, col, 14)))
            btn.setIconSize(QSize(14, 14))
            if primary:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {Theme.PRI}, stop:1 {Theme.NEON_PINK});
                        border: none; border-radius: 5px;
                    }}
                    QPushButton:hover {{
                        background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #4de3ff, stop:1 #ff5ce1);
                    }}
                """)
            else:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: rgba(255, 255, 255, 0.05);
                        border: 1px solid rgba(0, 212, 255, 0.2);
                        border-radius: 4px;
                    }}
                    QPushButton:hover {{
                        background: rgba(0, 212, 255, 0.15);
                        border: 1px solid {Theme.PRI};
                    }}
                """)
            return btn

        self._btn_shuffle = make_ctrl("shuffle", "Aléatoire")
        self._btn_prev = make_ctrl("skip-back", "Précédent")
        self._btn_prev.clicked.connect(self.prev_clicked.emit)

        self._btn_play = make_ctrl("play", "Lecture / Pause", primary=True, w=48)
        self._btn_play.clicked.connect(self._toggle_playback)

        self._btn_next = make_ctrl("skip-forward", "Suivant")
        self._btn_next.clicked.connect(self.next_clicked.emit)

        self._btn_vol = make_ctrl("volume-2", "Volume")

        for b in (self._btn_shuffle, self._btn_prev, self._btn_play, self._btn_next, self._btn_vol):
            ctrl_row.addWidget(b)
        ctrl_row.addStretch()

        self.add_layout(ctrl_row)

    def _paint_cover(self, event):
        p = QPainter(self._cover_box)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = 58.0, 58.0
        rect = QRectF(4, 4, w - 8, h - 8)

        # Fond vinyle cyber
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(8, 16, 26)))
        p.drawRoundedRect(rect, 8, 8)

        # Sillons concentriques
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(0, 212, 255, 30), 1))
        p.drawEllipse(rect.center(), 14, 14)
        p.drawEllipse(rect.center(), 8, 8)

        # Icône centrale
        p.drawPixmap(int(rect.center().x() - 10), int(rect.center().y() - 10),
                     render_icon("music", Theme.PRI, 20))

        # Anneau néon animé pendant la lecture
        if self._is_playing:
            p.save()
            p.translate(rect.center())
            p.rotate(self._ring_angle)
            p.setPen(QPen(qcol(Theme.PRI, 220), 1.8))
            p.drawArc(QRectF(-23, -23, 46, 46), 0, 80 * 16)
            p.setPen(QPen(qcol(Theme.NEON_PINK, 200), 1.8))
            p.drawArc(QRectF(-23, -23, 46, 46), 180 * 16, 80 * 16)
            p.restore()

    def _spin_ring(self):
        if not self.isVisible() or not self._is_playing:
            return
        self._ring_angle = (self._ring_angle + 3.0) % 360.0
        self._cover_box.update()

    def _toggle_playback(self):
        self.set_playing(not self._is_playing)
        self.play_toggled.emit(self._is_playing)

    def set_playing(self, playing: bool):
        self._is_playing = playing
        icon_name = "pause" if playing else "play"
        self._btn_play.setIcon(QIcon(render_icon(icon_name, Theme.WHITE, 14)))
        self._spectrum.set_playing(playing)
        if playing and not self._spin_timer.isActive():
            self._spin_timer.start(40)
        elif not playing and self._spin_timer.isActive():
            self._spin_timer.stop()
        self.set_live_text("EN COURS" if playing else "PAUSE", active=playing)
        self._cover_box.update()

    def set_track(self, title: str, artist: str, album: str = "", duration_s: float = 180.0):
        self._title_marquee.setText(title)
        self._artist_label.setText(f"{artist} • {album}" if album else artist)
        self._duration_s = max(1.0, duration_s)
        self._lbl_total.setText(self._format_time(self._duration_s))
        self.set_position(0.0)

    def set_position(self, current_s: float, total_s: Optional[float] = None):
        if total_s is not None:
            self._duration_s = max(1.0, total_s)
            self._lbl_total.setText(self._format_time(self._duration_s))
        self._current_pos_s = max(0.0, min(self._duration_s, current_s))
        self._lbl_elapsed.setText(self._format_time(self._current_pos_s))

        val = int((self._current_pos_s / self._duration_s) * 1000)
        self._seek_slider.blockSignals(True)
        self._seek_slider.setValue(val)
        self._seek_slider.blockSignals(False)

    def _on_seek_ratio(self, ratio: float):
        target_s = ratio * self._duration_s
        self.set_position(target_s)
        self.seek_requested.emit(target_s)

    @staticmethod
    def _format_time(seconds: float) -> str:
        s = int(seconds)
        m = s // 60
        sec = s % 60
        return f"{m:02d}:{sec:02d}"


class WeatherCard(GlassCard):
    """WeatherCard : Icône animée, température, vent, humidité et prévisions 24h."""

    def __init__(
        self,
        city: str = "Paris, FR",
        temp_c: float = 21.5,
        condition: str = "Ensoleillé",
        wind_kmh: float = 14.0,
        humidity_pct: int = 58,
        parent=None,
    ):
        super().__init__(
            category="ENVIRONNEMENT & MÉTÉO",
            title=city.upper(),
            icon_name="sun",
            accent_color=Theme.PRI,
            parent=parent,
        )
        self.pinned = True
        self._build_weather_ui(temp_c, condition, wind_kmh, humidity_pct)

    def _build_weather_ui(self, temp_c: float, condition: str, wind_kmh: float, humidity_pct: int):
        # Ligne principale : icône animée + grand affichage température
        hero_row = QHBoxLayout()
        hero_row.setSpacing(14)

        self._weather_icon = AnimatedWeatherWidget("sunny", self)
        hero_row.addWidget(self._weather_icon)

        temp_col = QVBoxLayout()
        temp_col.setContentsMargins(0, 0, 0, 0)
        temp_col.setSpacing(1)

        self._lbl_temp = QLabel(f"{temp_c:+.0f}°C", self)
        self._lbl_temp.setFont(QFont("Inter", 26, QFont.Weight.Bold))
        self._lbl_temp.setStyleSheet(f"color: {Theme.WHITE}; background: transparent;")
        temp_col.addWidget(self._lbl_temp)

        self._lbl_cond = QLabel(condition, self)
        self._lbl_cond.setFont(QFont("Inter", 9, QFont.Weight.Bold))
        self._lbl_cond.setStyleSheet(f"color: {Theme.TEXT}; background: transparent;")
        temp_col.addWidget(self._lbl_cond)

        self._lbl_feels = QLabel(f"Ressenti {temp_c + 1.2:.1f}°C • Min 14° / Max 24°", self)
        self._lbl_feels.setFont(QFont("Inter", 7))
        self._lbl_feels.setStyleSheet(f"color: {Theme.TEXT_DIM}; background: transparent;")
        temp_col.addWidget(self._lbl_feels)

        hero_row.addLayout(temp_col, stretch=1)
        self.add_layout(hero_row)

        # Grille de badges statistiques (Vent, Humidité, Pluie, Pression)
        metrics_row = QHBoxLayout()
        metrics_row.setSpacing(6)

        def make_metric(icon_name: str, label: str, val: str) -> QWidget:
            box = QFrame(self)
            box.setStyleSheet("""
                QFrame {
                    background: rgba(12, 22, 35, 0.65);
                    border: 1px solid rgba(0, 212, 255, 0.22);
                    border-radius: 6px;
                }
            """)
            lay = QVBoxLayout(box)
            lay.setContentsMargins(6, 4, 6, 4)
            lay.setSpacing(1)

            top = QHBoxLayout()
            top.setSpacing(4)
            icn = QLabel(box)
            icn.setPixmap(render_icon(icon_name, Theme.PRI, 11))
            top.addWidget(icn)

            lbl = QLabel(label, box)
            lbl.setFont(QFont("Inter", 6, QFont.Weight.Bold))
            lbl.setStyleSheet(f"color: {Theme.TEXT_DIM};")
            top.addWidget(lbl)
            top.addStretch()
            lay.addLayout(top)

            v = QLabel(val, box)
            v.setFont(QFont("Inter", 8, QFont.Weight.Bold))
            v.setStyleSheet(f"color: {Theme.WHITE};")
            lay.addWidget(v)
            return box

        metrics_row.addWidget(make_metric("wind", "VENT", f"{wind_kmh:.0f} km/h SO"))
        metrics_row.addWidget(make_metric("droplet", "HUMIDITÉ", f"{humidity_pct}%"))
        metrics_row.addWidget(make_metric("umbrella", "PLUIE", "10%"))
        metrics_row.addWidget(make_metric("activity", "PRESSION", "1016 hPa"))
        self.add_layout(metrics_row)

        # Bandeau de prévisions sur 24 heures (4 plages temporelles)
        self._build_forecast_bar()

    def _build_forecast_bar(self):
        forecast_box = QFrame(self)
        forecast_box.setStyleSheet("""
            QFrame {
                background: rgba(10, 20, 32, 0.7);
                border: 1px solid rgba(0, 212, 255, 0.18);
                border-radius: 6px;
            }
        """)
        lay = QVBoxLayout(forecast_box)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(4)

        tag = QLabel("PRÉVISIONS PROCHAINES 24 HEURES", forecast_box)
        f = QFont("Inter", 6, QFont.Weight.Bold)
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.4)
        tag.setFont(f)
        tag.setStyleSheet(f"color: {Theme.TEXT_DIM};")
        lay.addWidget(tag)

        strip = QHBoxLayout()
        strip.setSpacing(5)

        slots = [
            ("08h", "sun", "+15°C"),
            ("13h", "sun", "+22°C"),
            ("19h", "cloud-rain", "+18°C"),
            ("01h", "cloud", "+13°C"),
        ]

        for hour, icon, temp in slots:
            slot_w = QFrame(forecast_box)
            slot_w.setStyleSheet("""
                QFrame {
                    background: rgba(16, 28, 44, 0.55);
                    border: 1px solid rgba(0, 212, 255, 0.15);
                    border-radius: 4px;
                }
            """)
            s_lay = QVBoxLayout(slot_w)
            s_lay.setContentsMargins(3, 4, 3, 4)
            s_lay.setSpacing(2)
            s_lay.setAlignment(Qt.AlignmentFlag.AlignCenter)

            h_lbl = QLabel(hour, slot_w)
            h_lbl.setFont(QFont("Inter", 7))
            h_lbl.setStyleSheet(f"color: {Theme.TEXT_DIM};")
            s_lay.addWidget(h_lbl, alignment=Qt.AlignmentFlag.AlignCenter)

            ic = QLabel(slot_w)
            ic.setPixmap(render_icon(icon, Theme.PRI if icon != "cloud-rain" else Theme.NEON_PINK, 13))
            s_lay.addWidget(ic, alignment=Qt.AlignmentFlag.AlignCenter)

            t_lbl = QLabel(temp, slot_w)
            t_lbl.setFont(QFont("Inter", 8, QFont.Weight.Bold))
            t_lbl.setStyleSheet(f"color: {Theme.WHITE};")
            s_lay.addWidget(t_lbl, alignment=Qt.AlignmentFlag.AlignCenter)

            strip.addWidget(slot_w)

        lay.addLayout(strip)
        self.add_widget(forecast_box)


class TelemetryCard(GlassCard):
    """TelemetryCard : Surveillance temps réel de charge CPU, RAM, GPU et bande passante."""

    def __init__(self, update_interval_ms: int = 1000, parent=None):
        super().__init__(
            category="TÉLÉMÉTRIE MATÉRIELLE",
            title="SANTÉ SYSTÈME & RÉSEAU",
            icon_name="cpu",
            accent_color=Theme.GREEN,
            parent=parent,
        )
        self.pinned = True
        self._prev_net_in = 0
        self._prev_net_out = 0
        self._last_net_time = time.time()

        self._build_telemetry_ui()

        # Minuteur d'échantillonnage automatique
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(update_interval_ms)
        self._poll_timer.timeout.connect(self._poll_system_metrics)
        self._poll_timer.start()

    def _build_telemetry_ui(self):
        # Section CPU avec courbe Sparkline
        cpu_header = QHBoxLayout()
        cpu_header.setSpacing(4)
        lbl_cpu = QLabel("PROCESSEUR (CPU)", self)
        lbl_cpu.setFont(QFont("Inter", 7, QFont.Weight.Bold))
        lbl_cpu.setStyleSheet(f"color: {Theme.TEXT_MED};")
        cpu_header.addWidget(lbl_cpu)
        cpu_header.addStretch()

        self._lbl_cpu_val = QLabel("0%", self)
        self._lbl_cpu_val.setFont(QFont("Inter", 8, QFont.Weight.Bold))
        self._lbl_cpu_val.setStyleSheet(f"color: {Theme.WHITE};")
        cpu_header.addWidget(self._lbl_cpu_val)
        self.add_layout(cpu_header)

        self._spark_cpu = SparklineGraph(max_points=32, accent_color=Theme.PRI, parent=self)
        self.add_widget(self._spark_cpu)

        # Section RAM & Disque / GPU
        mem_row = QHBoxLayout()
        mem_row.setSpacing(10)

        # Jauge RAM
        ram_box = QVBoxLayout()
        ram_box.setSpacing(2)
        lbl_ram = QLabel("MÉMOIRE VIVE (RAM)", self)
        lbl_ram.setFont(QFont("Inter", 7, QFont.Weight.Bold))
        lbl_ram.setStyleSheet(f"color: {Theme.TEXT_MED};")
        ram_box.addWidget(lbl_ram)

        self._lbl_ram_val = QLabel("0.0 / 0.0 Go (0%)", self)
        self._lbl_ram_val.setFont(QFont("Inter", 8, QFont.Weight.Bold))
        self._lbl_ram_val.setStyleSheet(f"color: {Theme.WHITE};")
        ram_box.addWidget(self._lbl_ram_val)

        self._spark_ram = SparklineGraph(max_points=32, accent_color=Theme.NEON_VIO, parent=self)
        self._spark_ram.setFixedHeight(34)
        ram_box.addWidget(self._spark_ram)
        mem_row.addLayout(ram_box, stretch=1)

        self.add_layout(mem_row)

        # Section Bande passante Réseau
        net_box = QFrame(self)
        net_box.setStyleSheet("""
            QFrame {
                background: rgba(10, 20, 32, 0.7);
                border: 1px solid rgba(0, 212, 255, 0.22);
                border-radius: 6px;
            }
        """)
        n_lay = QHBoxLayout(net_box)
        n_lay.setContentsMargins(8, 5, 8, 5)
        n_lay.setSpacing(12)

        # Download
        down_col = QHBoxLayout()
        down_col.setSpacing(5)
        down_col.addWidget(QLabel(pixmap=render_icon("wifi", Theme.PRI, 13)))
        self._lbl_down = QLabel("↓ 0.0 Ko/s", net_box)
        self._lbl_down.setFont(QFont("Inter", 8, QFont.Weight.Bold))
        self._lbl_down.setStyleSheet(f"color: {Theme.TEXT};")
        down_col.addWidget(self._lbl_down)
        n_lay.addLayout(down_col)

        # Upload
        up_col = QHBoxLayout()
        up_col.setSpacing(5)
        up_col.addWidget(QLabel(pixmap=render_icon("activity", Theme.NEON_PINK, 13)))
        self._lbl_up = QLabel("↑ 0.0 Ko/s", net_box)
        self._lbl_up.setFont(QFont("Inter", 8, QFont.Weight.Bold))
        self._lbl_up.setStyleSheet(f"color: {Theme.TEXT};")
        up_col.addWidget(self._lbl_up)
        n_lay.addLayout(up_col)

        self.add_widget(net_box)

    def _poll_system_metrics(self):
        """Récupère les métriques matérielles en direct via psutil."""
        if not self.isVisible():
            return
        try:
            # CPU
            cpu_pct = psutil.cpu_percent(interval=None)
            self._lbl_cpu_val.setText(f"{cpu_pct:.1f}%")
            self._spark_cpu.add_sample(cpu_pct)

            # RAM
            vm = psutil.virtual_memory()
            used_gb = vm.used / (1024 ** 3)
            tot_gb = vm.total / (1024 ** 3)
            self._lbl_ram_val.setText(f"{used_gb:.1f} / {tot_gb:.1f} Go ({vm.percent:.0f}%)")
            self._spark_ram.add_sample(vm.percent)

            # Réseau
            net = psutil.net_io_counters()
            now = time.time()
            dt = max(0.1, now - self._last_net_time)
            if self._prev_net_in > 0:
                down_rate = (net.bytes_recv - self._prev_net_in) / dt
                up_rate = (net.bytes_sent - self._prev_net_out) / dt
                self._lbl_down.setText(f"↓ {self._fmt_bytes(down_rate)}/s")
                self._lbl_up.setText(f"↑ {self._fmt_bytes(up_rate)}/s")
            self._prev_net_in = net.bytes_recv
            self._prev_net_out = net.bytes_sent
            self._last_net_time = now

        except Exception:
            self._lbl_cpu_val.setText("N/A")

    @staticmethod
    def _fmt_bytes(bps: float) -> str:
        if bps > 1024 * 1024:
            return f"{bps / (1024 * 1024):.1f} Mo"
        if bps > 1024:
            return f"{bps / 1024:.0f} Ko"
        return f"{bps:.0f} o"


class PlanCard(GlassCard):
    """PlanCard : Liste dynamique des étapes de l'agent en cours avec statut en direct."""

    step_action_requested = pyqtSignal(int, str)

    def __init__(self, objective: str = "Mission Agent Autonome", steps: Optional[List[Dict[str, Any]]] = None, parent=None):
        super().__init__(
            category="PLAN D'EXÉCUTION AGENT",
            title="ORCHESTRATION DU PLAN",
            icon_name="terminal",
            accent_color=Theme.NEON_AMBER,
            parent=parent,
        )
        self.pinned = True
        self._objective = objective
        self._steps: List[Dict[str, Any]] = steps or []
        self._step_widgets: List[QFrame] = []

        self._build_plan_ui()

    def _build_plan_ui(self):
        # Description de l'objectif
        self._lbl_obj = QLabel(f"Objectif : {self._objective}", self)
        self._lbl_obj.setFont(QFont("Inter", 8, QFont.Weight.Bold))
        self._lbl_obj.setStyleSheet(f"color: {Theme.WHITE}; background: transparent;")
        self._lbl_obj.setWordWrap(True)
        self.add_widget(self._lbl_obj)

        # Progression globale
        prog_header = QHBoxLayout()
        self._lbl_prog = QLabel("Progression du plan : 0%", self)
        self._lbl_prog.setFont(QFont("Inter", 7, QFont.Weight.Bold))
        self._lbl_prog.setStyleSheet(f"color: {Theme.TEXT_MED};")
        prog_header.addWidget(self._lbl_prog)
        prog_header.addStretch()
        self.add_layout(prog_header)

        self._global_prog_bar = QFrame(self)
        self._global_prog_bar.setFixedHeight(5)
        self._global_prog_bar.setStyleSheet("""
            QFrame {
                background: rgba(0, 212, 255, 0.15);
                border-radius: 2.5px;
            }
        """)
        self.add_widget(self._global_prog_bar)

        # Conteneur des étapes
        self._steps_container = QVBoxLayout()
        self._steps_container.setSpacing(5)
        self.add_layout(self._steps_container)

        self.set_steps(self._steps)

    def set_steps(self, steps: List[Dict[str, Any]]):
        """Met à jour la liste complète des étapes."""
        self._steps = steps or []

        # Nettoyage des widgets existants
        for w in self._step_widgets:
            w.deleteLater()
        self._step_widgets.clear()

        done_count = 0
        for idx, s in enumerate(self._steps):
            status = s.get("status", "pending")
            if status == "done":
                done_count += 1
            w = self._create_step_row(idx, s)
            self._step_widgets.append(w)
            self._steps_container.addWidget(w)

        # Mise à jour du pourcentage
        total = max(1, len(self._steps))
        pct = int((done_count / total) * 100)
        self._lbl_prog.setText(f"Progression du plan : Étape {min(done_count + 1, total)}/{total} ({pct}%)")

        self._global_prog_bar.setStyleSheet(f"""
            QFrame {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 {Theme.PRI},
                    stop:{pct / 100.0:.2f} {Theme.GREEN},
                    stop:{min(1.0, (pct / 100.0) + 0.01):.2f} rgba(0, 212, 255, 0.15),
                    stop:1 rgba(0, 212, 255, 0.15));
                border-radius: 2.5px;
            }}
        """)

    def _create_step_row(self, index: int, step_data: Dict[str, Any]) -> QFrame:
        row = QFrame(self)
        status = step_data.get("status", "pending")

        # Style conditionnel selon le statut
        if status == "running":
            border_col = Theme.PRI
            bg_col = "rgba(0, 212, 255, 0.14)"
            icon_name = "activity"
            icon_col = Theme.PRI
        elif status == "done":
            border_col = "rgba(0, 255, 136, 0.4)"
            bg_col = "rgba(0, 255, 136, 0.07)"
            icon_name = "check"
            icon_col = Theme.GREEN
        elif status == "error":
            border_col = Theme.RED
            bg_col = "rgba(255, 51, 85, 0.18)"
            icon_name = "alert-triangle"
            icon_col = Theme.RED
        else:
            border_col = "rgba(0, 212, 255, 0.12)"
            bg_col = "rgba(10, 20, 32, 0.45)"
            icon_name = "clock"
            icon_col = Theme.TEXT_DIM

        row.setStyleSheet(f"""
            QFrame {{
                background: {bg_col};
                border: 1px solid {border_col};
                border-radius: 5px;
            }}
        """)

        lay = QHBoxLayout(row)
        lay.setContentsMargins(8, 5, 8, 5)
        lay.setSpacing(8)

        # Icône d'état
        icn_lbl = QLabel(row)
        icn_lbl.setPixmap(render_icon(icon_name, icon_col, 14))
        lay.addWidget(icn_lbl)

        # Textes de l'étape
        text_col = QVBoxLayout()
        text_col.setSpacing(1)

        title_txt = f"{index + 1}. {step_data.get('title', 'Étape')}"
        title_lbl = QLabel(title_txt, row)
        title_lbl.setFont(QFont("Inter", 8, QFont.Weight.Bold if status == "running" else QFont.Weight.Normal))
        title_lbl.setStyleSheet(f"color: {Theme.WHITE if status != 'pending' else Theme.TEXT_MED};")
        text_col.addWidget(title_lbl)

        detail_txt = step_data.get("detail", "")
        if detail_txt:
            det_lbl = QLabel(detail_txt, row)
            det_lbl.setFont(QFont("Inter", 7))
            det_lbl.setStyleSheet(f"color: {Theme.TEXT_DIM};")
            text_col.addWidget(det_lbl)

        lay.addLayout(text_col, stretch=1)

        # Badge durée ou indicateur en cours
        duration = step_data.get("duration", "")
        if duration:
            dur_lbl = QLabel(duration, row)
            dur_lbl.setFont(QFont("Inter", 7, QFont.Weight.Bold))
            dur_lbl.setStyleSheet(f"color: {Theme.TEXT_MED};")
            lay.addWidget(dur_lbl)

        return row

    def update_step(self, step_index: int, status: str, detail: str = "", duration: str = ""):
        """Met à jour une étape spécifique."""
        if 0 <= step_index < len(self._steps):
            self._steps[step_index]["status"] = status
            if detail:
                self._steps[step_index]["detail"] = detail
            if duration:
                self._steps[step_index]["duration"] = duration
            self.set_steps(self._steps)


class NeonProgressBar(QWidget):
    """Barre HUD : dégradé cyan→magenta, halo de tête, balayage discret.

    90 ms de cadence, comme les autres cartes : assez pour le shimmer, trop
    lent pour voler le GIL à la voix.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ratio = 0.0
        self._indeterminate = True
        self._scan = 0.0
        self._accent = qcol(Theme.PRI)
        self.setFixedHeight(16)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(90)

    def set_progress(self, percent: float, *, indeterminate: bool | None = None):
        self._ratio = max(0.0, min(1.0, float(percent) / 100.0))
        if indeterminate is not None:
            self._indeterminate = bool(indeterminate)
        elif percent > 1.5:
            self._indeterminate = False
        self.update()

    def _tick(self):
        if not self.isVisible():
            return
        self._scan = (self._scan + 0.08) % 1.0
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = float(self.width()), float(self.height())
        if w < 8 or h < 6:
            p.end()
            return
        mid_y = h / 2.0
        track = QRectF(1.0, mid_y - 3.5, w - 2.0, 7.0)

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(8, 18, 28, 230)))
        p.drawRoundedRect(track, 3.5, 3.5)
        p.setPen(QPen(QColor(0, 212, 255, 40), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(track, 3.5, 3.5)

        if self._indeterminate:
            band_w = max(36.0, w * 0.28)
            travel = max(1.0, w - band_w)
            ping = 1.0 - abs(self._scan * 2.0 - 1.0)
            x = 1.0 + ping * travel
            glow = QLinearGradient(x, 0, x + band_w, 0)
            glow.setColorAt(0.0, QColor(0, 212, 255, 0))
            glow.setColorAt(0.45, qcol(Theme.PRI, 210))
            glow.setColorAt(0.7, qcol(Theme.NEON_PINK, 200))
            glow.setColorAt(1.0, QColor(255, 43, 214, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(glow))
            p.drawRoundedRect(QRectF(x, track.top(), band_w, track.height()), 3.5, 3.5)
            p.end()
            return

        fill_w = track.width() * self._ratio
        if fill_w > 1.0:
            fill = QLinearGradient(track.left(), 0, track.left() + fill_w, 0)
            fill.setColorAt(0.0, qcol(Theme.PRI_DIM, 230))
            fill.setColorAt(0.55, qcol(Theme.PRI, 255))
            fill.setColorAt(1.0, qcol(Theme.NEON_PINK, 255))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(fill))
            p.drawRoundedRect(
                QRectF(track.left(), track.top(), fill_w, track.height()), 3.5, 3.5
            )

            sweep_x = track.left() + (fill_w - 18.0) * self._scan
            sweep = QLinearGradient(sweep_x, 0, sweep_x + 18.0, 0)
            sweep.setColorAt(0.0, QColor(255, 255, 255, 0))
            sweep.setColorAt(0.5, QColor(255, 255, 255, 70))
            sweep.setColorAt(1.0, QColor(255, 255, 255, 0))
            p.setClipRect(QRectF(track.left(), track.top(), fill_w, track.height()))
            p.setBrush(QBrush(sweep))
            p.drawRoundedRect(
                QRectF(sweep_x, track.top(), 18.0, track.height()), 3.5, 3.5
            )
            p.setClipping(False)

            head_x = track.left() + fill_w
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
            halo = QRadialGradient(head_x, mid_y, 9)
            halo.setColorAt(0.0, qcol(Theme.PRI, 160))
            halo.setColorAt(1.0, qcol(Theme.PRI, 0))
            p.setBrush(QBrush(halo))
            p.drawEllipse(QPointF(head_x, mid_y), 9, 9)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            p.setBrush(QBrush(qcol(Theme.WHITE)))
            p.setPen(QPen(qcol(Theme.PRI), 1.1))
            p.drawEllipse(QPointF(head_x, mid_y), 3.2, 3.2)
        p.end()


class DownloadCard(GlassCard):
    """Carte de téléchargement musical : titre, artiste, barre néon, statut."""

    cancel_requested = pyqtSignal(str)

    _STATUS_LABELS = {
        "searching": "RECHERCHE",
        "downloading": "TRANSFERT",
        "converting": "CONVERSION",
        "done": "PRÊT",
        "exists": "DÉJÀ LÀ",
        "error": "ÉCHEC",
        "cancelled": "ANNULÉ",
    }

    def __init__(self, payload: Optional[Dict[str, Any]] = None, parent=None):
        super().__init__(
            category="TÉLÉCHARGEMENT",
            title="YOUTUBE AUDIO",
            icon_name="download",
            accent_color=Theme.NEON_PINK,
            parent=parent,
        )
        self.card_type = "download"
        self.pinned = True
        self.download_id = ""
        self._status = "searching"
        self._percent = 0.0
        self._path = ""
        self._build_download_ui()
        self.apply_payload(payload or {})

    def _build_download_ui(self):
        top = QHBoxLayout()
        top.setSpacing(12)

        self._cover_box = QWidget(self)
        self._cover_box.setFixedSize(52, 52)
        self._cover_box.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._cover_box.paintEvent = self._paint_cover
        top.addWidget(self._cover_box)

        meta = QVBoxLayout()
        meta.setContentsMargins(0, 2, 0, 2)
        meta.setSpacing(2)

        self._title_marquee = MarqueeLabel("Recherche…", self)
        self._title_marquee.setFont(QFont("Inter", 10, QFont.Weight.Bold))
        self._title_marquee.setStyleSheet(f"color: {Theme.WHITE};")
        self._title_marquee.setFixedHeight(18)
        meta.addWidget(self._title_marquee)

        self._artist_label = QLabel("YouTube • meilleure qualité", self)
        self._artist_label.setFont(QFont("Inter", 8))
        self._artist_label.setStyleSheet(
            f"color: {Theme.TEXT_MED}; background: transparent;"
        )
        meta.addWidget(self._artist_label)

        self._codec_badge = QLabel("● AUDIO • M4A BEST", self)
        f_codec = QFont("Inter", 6, QFont.Weight.Bold)
        f_codec.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.2)
        self._codec_badge.setFont(f_codec)
        self._codec_badge.setStyleSheet(
            "color: rgba(0, 212, 255, 0.7); background: transparent;"
        )
        meta.addWidget(self._codec_badge)
        top.addLayout(meta, stretch=1)
        self.add_layout(top)

        pct_row = QHBoxLayout()
        pct_row.setContentsMargins(0, 4, 0, 0)
        pct_row.setSpacing(8)
        self._bar = NeonProgressBar(self)
        pct_row.addWidget(self._bar, stretch=1)
        self._pct_label = QLabel("0%", self)
        self._pct_label.setFont(QFont("Inter", 8, QFont.Weight.Bold))
        self._pct_label.setStyleSheet(f"color: {Theme.PRI}; background: transparent;")
        self._pct_label.setFixedWidth(42)
        self._pct_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        pct_row.addWidget(self._pct_label)
        self.add_layout(pct_row)

        info_row = QHBoxLayout()
        info_row.setContentsMargins(0, 0, 0, 0)
        self._speed_label = QLabel("Recherche de la version officielle…", self)
        self._speed_label.setFont(QFont("Inter", 7))
        self._speed_label.setStyleSheet(
            f"color: {Theme.TEXT_DIM}; background: transparent;"
        )
        info_row.addWidget(self._speed_label, stretch=1)
        self._eta_label = QLabel("", self)
        self._eta_label.setFont(QFont("Inter", 7, QFont.Weight.Bold))
        self._eta_label.setStyleSheet(
            f"color: {Theme.TEXT_MED}; background: transparent;"
        )
        info_row.addWidget(self._eta_label)
        self.add_layout(info_row)

        self._dest_label = QLabel("", self)
        self._dest_label.setFont(QFont("Inter", 7))
        self._dest_label.setStyleSheet(
            f"color: {Theme.TEXT_DIM}; background: transparent;"
        )
        self._dest_label.setWordWrap(True)
        self.add_widget(self._dest_label)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 4, 0, 0)
        btn_row.addStretch()
        self._action_btn = QPushButton("Annuler", self)
        self._action_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._action_btn.setFont(QFont("Inter", 8, QFont.Weight.Bold))
        self._action_btn.setFixedHeight(26)
        self._action_btn.setMinimumWidth(92)
        self._style_action_btn(primary=False)
        self._action_btn.clicked.connect(self._on_action)
        btn_row.addWidget(self._action_btn)
        self.add_layout(btn_row)

        self._cancel_callback = self._cancel_download

    def _style_action_btn(self, *, primary: bool):
        if primary:
            self._action_btn.setStyleSheet(f"""
                QPushButton {{
                    color: #020c14;
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                        stop:0 {Theme.PRI}, stop:1 {Theme.NEON_PINK});
                    border: none; border-radius: 5px; padding: 4px 12px;
                }}
                QPushButton:hover {{
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                        stop:0 #6ce8ff, stop:1 #ff5ce5);
                }}
            """)
        else:
            self._action_btn.setStyleSheet(f"""
                QPushButton {{
                    color: {Theme.TEXT};
                    background: rgba(255, 255, 255, 0.06);
                    border: 1px solid rgba(0, 212, 255, 0.3);
                    border-radius: 5px; padding: 4px 12px;
                }}
                QPushButton:hover {{
                    background: rgba(0, 212, 255, 0.18);
                    border-color: {Theme.PRI};
                    color: {Theme.WHITE};
                }}
            """)

    def _paint_cover(self, event):
        p = QPainter(self._cover_box)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(3, 3, 46, 46)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(8, 16, 26)))
        p.drawRoundedRect(rect, 8, 8)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(0, 212, 255, 40), 1))
        p.drawEllipse(rect.center(), 12, 12)
        p.drawEllipse(rect.center(), 6, 6)
        icon = "check" if self._status in {"done", "exists"} else (
            "alert-triangle" if self._status == "error" else "download"
        )
        color = Theme.GREEN if icon == "check" else (
            Theme.RED if icon == "alert-triangle" else Theme.PRI
        )
        p.drawPixmap(
            int(rect.center().x() - 9), int(rect.center().y() - 9),
            render_icon(icon, color, 18),
        )
        bar = getattr(self, "_bar", None)
        if bar is not None and self._status in {"downloading", "converting", "searching"}:
            p.save()
            p.translate(rect.center())
            p.rotate(bar._scan * 360.0)
            p.setPen(QPen(qcol(Theme.PRI, 210), 1.6))
            p.drawArc(QRectF(-20, -20, 40, 40), 0, 70 * 16)
            p.setPen(QPen(qcol(Theme.NEON_PINK, 190), 1.6))
            p.drawArc(QRectF(-20, -20, 40, 40), 180 * 16, 70 * 16)
            p.restore()
        p.end()

    def apply_payload(self, payload: Dict[str, Any]) -> None:
        if payload.get("id"):
            self.download_id = str(payload["id"])
        title = str(payload.get("title") or self._title_marquee._text or "Morceau")
        artist = str(payload.get("artist") or payload.get("channel") or "")
        status = str(payload.get("status") or self._status or "searching")
        percent = float(payload.get("percent") or self._percent or 0.0)
        self._status = status
        self._percent = percent
        if payload.get("path"):
            self._path = str(payload["path"])

        self._title_marquee.setText(title)
        if artist:
            self._artist_label.setText(f"{artist} • YouTube")
        dest = str(payload.get("destination") or "")
        path = self._path
        if path:
            self._dest_label.setText(path)
        elif dest:
            self._dest_label.setText(dest)

        live = self._STATUS_LABELS.get(status, "TRANSFERT")
        active = status in {"searching", "downloading", "converting"}
        self.set_live_text(live, active=active)
        self._bar.set_progress(percent, indeterminate=status == "searching")
        self._pct_label.setText(f"{int(percent)}%")

        speed = str(payload.get("speed") or "")
        size = str(payload.get("size") or "")
        eta = str(payload.get("eta") or "")
        if status == "searching":
            self._speed_label.setText("Recherche de la version officielle…")
            self._eta_label.setText("")
        elif status == "converting":
            self._speed_label.setText("Extraction audio meilleure qualité…")
            self._eta_label.setText("")
        elif status == "done":
            self._speed_label.setText("Enregistré dans Musique")
            self._eta_label.setText("")
            self._pct_label.setStyleSheet(
                f"color: {Theme.GREEN}; background: transparent;"
            )
        elif status == "exists":
            self._speed_label.setText("Déjà présent dans Musique")
            self._eta_label.setText("")
        elif status == "error":
            self._speed_label.setText(str(payload.get("message") or "Échec du téléchargement"))
            self._eta_label.setText("")
            self._pct_label.setStyleSheet(
                f"color: {Theme.RED}; background: transparent;"
            )
        elif status == "cancelled":
            self._speed_label.setText("Téléchargement annulé")
            self._eta_label.setText("")
        else:
            bits = [part for part in (speed, size) if part]
            self._speed_label.setText("  ·  ".join(bits) if bits else "Téléchargement…")
            self._eta_label.setText(f"ETA {eta}" if eta else "")

        terminal = status in {"done", "exists", "error", "cancelled"}
        self.pinned = not terminal
        if terminal:
            self._cancel_callback = None
        if status in {"done", "exists"}:
            self._action_btn.setText("Ouvrir")
            self._style_action_btn(primary=True)
            self.auto_dismiss_s = 12.0
            self._dismiss_remaining = 12.0
            if not self._hovered:
                self._dismiss_timer.start()
        elif status in {"error", "cancelled"}:
            self._action_btn.setText("Fermer")
            self._style_action_btn(primary=False)
            self.auto_dismiss_s = 8.0
            self._dismiss_remaining = 8.0
            if not self._hovered:
                self._dismiss_timer.start()
        else:
            self._action_btn.setText("Annuler")
            self._style_action_btn(primary=False)
        self._cover_box.update()
        self.updateGeometry()

    def _cancel_download(self):
        if not self.download_id:
            return
        try:
            from actions.download_music import cancel_download
            cancel_download(self.download_id)
        except Exception:
            pass
        self.cancel_requested.emit(self.download_id)

    def _on_action(self):
        if self._status in {"done", "exists"} and self._path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(self._path).parent)))
            return
        if self._status in {"error", "cancelled"}:
            self.dismiss()
            return
        self._cancel_download()
        self.apply_payload({"status": "cancelled", "percent": self._percent})
        self.dismiss()


# ── Orchestrateur : CardManager ──────────────────────────────────────────────

class CardManager(QWidget):
    """Orchestrateur intelligent de cartes riches avec empilement vertical sans chevauchement.

    Caractéristiques :
    - Gestion fluide des coordonnées Y sans jamais de superposition accidentelle
    - Animations synchronisées de glissement latéral + opacité à l'insertion et suppression
    - Réorganisation animée automatique des cartes restantes lors de la fermeture
    - Prise en charge de l'auto-dismiss configurable par carte
    """

    # Cinq e-mails doivent pouvoir rester visibles ensemble ; la zone est
    # défilable et les cartes se réorganisent déjà sans coût d'animation lourd.
    MAX_CARDS = 8
    CARD_SPACING = 10
    MARGIN_TOP = 8
    MARGIN_RIGHT = 12

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("CardManager")
        self.setFixedWidth(GlassCard.CARD_WIDTH + self.MARGIN_RIGHT * 2)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setStyleSheet("background: transparent;")

        self._cards: List[GlassCard] = []
        self._closing_cards: List[GlassCard] = []
        self._active_anims: Dict[GlassCard, QParallelAnimationGroup] = {}
        self._download_cards: Dict[str, "DownloadCard"] = {}

    def add_card(
        self,
        card_or_type: Any,
        title: str = "",
        body: str = "",
        actions: Optional[List[dict]] = None,
        auto_dismiss_s: float = 0.0,
    ) -> GlassCard:
        """Ajoute une carte riche à la pile verticale avec animation d'entrée fluide.

        Supporte à la fois une instance de GlassCard ou les signatures rétro-compatibles
        de l'ancien RightCardStack (type, titre, corps).
        """
        # Plusieurs producteurs (veille, annonce vocale, outil) peuvent livrer
        # la même information à quelques millisecondes d'écart. Une seule carte
        # doit alors rester visible ; les confirmations restent volontairement
        # distinctes car chacune porte une action utilisateur.
        if not isinstance(card_or_type, GlassCard):
            requested_type = str(card_or_type).strip().lower()
            requested_title = str(title or "Information")
            requested_body = str(body or "")
            if "confirm" not in requested_type:
                for existing in self._cards:
                    if (
                        existing.card_type == requested_type
                        and existing.card_title == requested_title
                        and getattr(existing, "_body_text", "") == requested_body
                    ):
                        self._reorganize_remaining_cards()
                        return existing
        # Construction de carte si chaîne passée
        if isinstance(card_or_type, GlassCard):
            card = card_or_type
            if auto_dismiss_s > 0 and card.auto_dismiss_s <= 0:
                card.auto_dismiss_s = auto_dismiss_s
                card._dismiss_remaining = auto_dismiss_s
                card._dismiss_timer.start()
            if "confirm" in getattr(card, "card_type", "").lower() or getattr(card, "category", "").upper() == "SÉCURITÉ":
                card.pinned = True
                card.auto_dismiss_s = 0.0
        else:
            # Rétrocompatibilité avec show_card("message", ...)
            card_type = str(card_or_type).lower()
            accent = Theme.PRI
            icon_name = "info"
            category = card_type.upper()
            pinned = False
            auto_dismiss = auto_dismiss_s

            is_confirmation = card_type == "confirmation" or "confirm" in card_type
            if is_confirmation:
                category = "SÉCURITÉ"
                accent = Theme.NEON_AMBER
                icon_name = "shield-alert"
                auto_dismiss = 0.0
                pinned = True
            elif "download" in card_type or "telecharg" in card_type:
                accent = Theme.NEON_PINK
                icon_name = "download"
            elif "media" in card_type or "music" in card_type:
                accent = Theme.NEON_PINK
                icon_name = "music"
            elif "weather" in card_type or "meteo" in card_type:
                accent = Theme.PRI
                icon_name = "sun"
            elif "telemetry" in card_type or "system" in card_type:
                accent = Theme.GREEN
                icon_name = "cpu"
            elif "plan" in card_type or "agent" in card_type:
                accent = Theme.NEON_AMBER
                icon_name = "terminal"
            elif "error" in card_type:
                accent = Theme.RED
                icon_name = "alert-triangle"

            card = GlassCard(
                category=category,
                title=title or "Information",
                icon_name=icon_name,
                accent_color=accent,
                auto_dismiss_s=auto_dismiss,
                parent=self,
            )
            card.pinned = pinned
            if is_confirmation:
                try:
                    from ui.visual_pointer import get_visual_pointer
                    vp = get_visual_pointer()
                    vp.register_target_widget("confirmation", card)
                    vp.register_target_widget("carte_confirmation", card)
                    vp.register_target_widget("carte de confirmation", card)
                    vp.register_target_widget("zone de notification", card)
                    vp.register_target_widget("notification", card)
                except Exception:
                    pass
            if body:
                card.set_body(body)
            if actions:
                cancel_action = next(
                    (a for a in actions if "annul" in str(a.get("label", "")).lower()),
                    None,
                )
                if cancel_action is None:
                    cancel_action = next((a for a in actions if not a.get("primary")), None)
                if cancel_action:
                    card._cancel_callback = cancel_action.get("callback")

                row = QHBoxLayout()
                row.setContentsMargins(0, 2, 0, 0)
                row.setSpacing(6)
                row.addStretch()
                for action in actions:
                    button = QPushButton(str(action.get("label") or "Action"), card)
                    button.setCursor(Qt.CursorShape.PointingHandCursor)
                    button.setFont(QFont("Inter", 8, QFont.Weight.Bold))
                    primary = bool(action.get("primary"))
                    if primary:
                        button.setStyleSheet(f"""
                            QPushButton {{
                                color: #020c14;
                                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                    stop:0 {card.accent_color}, stop:1 {Theme.NEON_PINK});
                                border: none;
                                border-radius: 5px;
                                padding: 6px 14px;
                                font-weight: 700;
                            }}
                            QPushButton:hover {{
                                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                    stop:0 #6ce8ff, stop:1 #ff5ce5);
                            }}
                            QPushButton:disabled {{
                                color: rgba(255, 255, 255, 0.4);
                                background: rgba(255, 255, 255, 0.1);
                                border: 1px solid rgba(255, 255, 255, 0.1);
                            }}
                        """)
                    else:
                        button.setStyleSheet(f"""
                            QPushButton {{
                                color: {Theme.TEXT};
                                background: rgba(255, 255, 255, 0.06);
                                border: 1px solid rgba(0, 212, 255, 0.3);
                                border-radius: 5px;
                                padding: 6px 12px;
                            }}
                            QPushButton:hover {{
                                background: rgba(0, 212, 255, 0.18);
                                border-color: {Theme.PRI};
                                color: {Theme.WHITE};
                            }}
                            QPushButton:disabled {{
                                color: rgba(255, 255, 255, 0.4);
                                background: rgba(255, 255, 255, 0.1);
                                border: 1px solid rgba(255, 255, 255, 0.1);
                            }}
                        """)

                    def _make_handler(btn, act, is_prim):
                        def _handler(_checked=False):
                            if is_prim or "confirmer" in str(act.get("label", "")).lower():
                                btn.setEnabled(False)
                                btn.setText("Validation...")
                            self._run_action(card, act)
                        return _handler

                    button.clicked.connect(_make_handler(button, action, primary))
                    row.addWidget(button)
                card.add_layout(row)

        # Gestion du plafond maximal de cartes actives (éviction douce de la plus ancienne non épinglée)
        if len(self._cards) >= self.MAX_CARDS:
            unpinned = [c for c in self._cards if not getattr(c, "pinned", False)]
            target = unpinned[0] if unpinned else self._cards[0]
            self.dismiss_card(target)

        card.setParent(self)
        card.dismiss_requested.connect(lambda c=card: self.dismiss_card(c))

        if getattr(card, "pinned", False) and ("confirm" in getattr(card, "card_type", "").lower() or getattr(card, "category", "").upper() == "SÉCURITÉ"):
            self._cards.insert(0, card)
        else:
            self._cards.append(card)

        card.show()
        card.raise_()

        # Calcul de la position cible stricte pour éviter tout chevauchement
        target_y = self._calculate_target_y(card)
        card_w = card.width()
        target_x = self.MARGIN_RIGHT

        # Position de départ décalée pour le glissement depuis la droite
        card.setGeometry(target_x + 80, target_y, card_w, card.sizeHint().height())
        card._opacity_effect.setOpacity(0.0)

        # Animation simultanée de glissement (pos) + fondu (opacity)
        group = QParallelAnimationGroup(self)

        anim_pos = QPropertyAnimation(card, b"pos", self)
        anim_pos.setDuration(320)
        anim_pos.setStartValue(QPoint(target_x + 80, target_y))
        anim_pos.setEndValue(QPoint(target_x, target_y))
        anim_pos.setEasingCurve(QEasingCurve.Type.OutCubic)
        group.addAnimation(anim_pos)

        anim_fade = QPropertyAnimation(card._opacity_effect, b"opacity", self)
        anim_fade.setDuration(280)
        anim_fade.setStartValue(0.0)
        anim_fade.setEndValue(1.0)
        anim_fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        group.addAnimation(anim_fade)

        self._active_anims[card] = group
        group.start()

        self._update_container_geometry()
        self._reorganize_remaining_cards()
        return card

    # Une carte par tâche : « <Tâche> en cours » dès le départ, puis
    # « <Tâche> terminée » (ou « — échec ») avec le résultat, avant de s'effacer.
    TASK_DONE_DISMISS_S = 6.0
    TASK_ERROR_DISMISS_S = 14.0

    def upsert_task_card(self, task_id: str, title: str, body: str, status: str) -> Optional[GlassCard]:
        task_id = str(task_id or "")
        status = (status or "running").lower()
        cards = getattr(self, "_task_cards", None)
        if cards is None:
            cards = self._task_cards = {}
        card = cards.get(task_id)
        if card is not None and (card not in self._cards or getattr(card, "_is_closing", False)):
            cards.pop(task_id, None)
            card = None
        if status == "running":
            if card is None:
                card = GlassCard(category="TÂCHE", title=title, icon_name="clock",
                                 accent_color=Theme.NEON_AMBER, parent=self)
                card.set_live_text("EN COURS", True)
                if body:
                    card.set_body(body)
                if task_id:
                    cards[task_id] = card
                self.add_card(card)
            else:
                card.set_header(title=title)
                if body:
                    card.set_body(body)
                self._reorganize_remaining_cards()
            return card
        # done / error
        if card is None:
            # La tâche a été trop rapide pour avoir eu sa carte « en cours » :
            # on montre quand même son résultat, brièvement.
            card = GlassCard(category="TÂCHE", title=title, icon_name="check",
                             accent_color=Theme.GREEN, parent=self)
            self.add_card(card)
        cards.pop(task_id, None)
        ok = status != "error"
        card.set_header(title=title, category="TÂCHE TERMINÉE" if ok else "TÂCHE EN ÉCHEC",
                        icon_name="check" if ok else "alert-triangle",
                        accent_color=Theme.GREEN if ok else Theme.RED)
        card.set_live_text("TERMINÉ" if ok else "ÉCHEC", False)
        if body:
            card.set_body(body)
        card.start_auto_dismiss(self.TASK_DONE_DISMISS_S if ok else self.TASK_ERROR_DISMISS_S)
        self._reorganize_remaining_cards()
        return card

    def upsert_download_card(self, payload: Dict[str, Any]) -> "DownloadCard":
        """Crée ou met à jour la carte de téléchargement identifiée par `id`."""
        dl_id = str((payload or {}).get("id") or "")
        existing = self._download_cards.get(dl_id) if dl_id else None
        if existing is not None and existing in self._cards:
            existing.apply_payload(payload or {})
            self._reorganize_remaining_cards()
            return existing
        card = DownloadCard(payload or {}, parent=self)
        if card.download_id:
            self._download_cards[card.download_id] = card
        self.add_card(card)
        return card

    def _run_action(self, card: GlassCard, action: dict) -> None:
        card._action_done = True
        card.action_triggered.emit(action)
        callback = action.get("callback")
        if callable(callback):
            callback()
        if action.get("dismiss", True):
            self.dismiss_card(card)

    def update_card(self, card_type: str, title: str, body: str) -> bool:
        wanted_type = card_type.strip().lower()
        for card in self._cards:
            if card.card_type == wanted_type and card.card_title == title:
                card.set_body(body)
                self._reorganize_remaining_cards()
                return True
        return False

    def dismiss_card(self, card: GlassCard):
        """Anime la sortie d'une carte puis réorganise verticalement les cartes restantes."""
        if card not in self._cards:
            return
        if not getattr(card, "_action_done", False):
            card._action_done = True
            cancel_cb = getattr(card, "_cancel_callback", None)
            if callable(cancel_cb):
                try:
                    cancel_cb()
                except Exception:
                    pass
            try:
                from ui.visual_pointer import get_visual_pointer
                vp = get_visual_pointer()
                vp.unregister_target_widget("confirmation")
                vp.unregister_target_widget("carte_confirmation")
                vp.unregister_target_widget("carte de confirmation")
                vp.unregister_target_widget("zone de notification")
                vp.unregister_target_widget("notification")
            except Exception:
                pass
        if card in self._closing_cards:
            return
        card._is_closing = True

        # Retrait immédiat des cartes actives pour garantir la réorganisation et le plafond
        self._cards.remove(card)
        self._closing_cards.append(card)
        dl_id = getattr(card, "download_id", "")
        if dl_id:
            self._download_cards.pop(dl_id, None)

        # Arrêt des animations en cours sur cette carte
        if card in self._active_anims:
            self._active_anims[card].stop()

        target_x = card.x() + 80
        group = QParallelAnimationGroup(self)

        anim_pos = QPropertyAnimation(card, b"pos", self)
        anim_pos.setDuration(260)
        anim_pos.setStartValue(card.pos())
        anim_pos.setEndValue(QPoint(target_x, card.y()))
        anim_pos.setEasingCurve(QEasingCurve.Type.InCubic)
        group.addAnimation(anim_pos)

        anim_fade = QPropertyAnimation(card._opacity_effect, b"opacity", self)
        anim_fade.setDuration(240)
        anim_fade.setStartValue(card._opacity_effect.opacity())
        anim_fade.setEndValue(0.0)
        anim_fade.setEasingCurve(QEasingCurve.Type.InCubic)
        group.addAnimation(anim_fade)

        def on_exit_finished():
            if card in self._closing_cards:
                self._closing_cards.remove(card)
            if card in self._active_anims:
                del self._active_anims[card]
            card.hide()
            card.closed.emit()
            card.deleteLater()
            self._reorganize_remaining_cards()

        group.finished.connect(on_exit_finished)
        self._active_anims[card] = group
        group.start()

        # Glissement anticipé des cartes restantes
        self._reorganize_remaining_cards()

    def _calculate_target_y(self, for_card: GlassCard) -> int:
        """Calcule le Y exact sans chevauchement en sommant les hauteurs des cartes précédentes."""
        current_y = self.MARGIN_TOP
        for c in self._cards:
            if c is for_card:
                break
            h = c.sizeHint().height() or c.height()
            current_y += h + self.CARD_SPACING
        return current_y

    def _reorganize_remaining_cards(self):
        """Glissement vertical fluide des cartes restantes pour combler les espaces."""
        current_y = self.MARGIN_TOP
        for c in self._cards:
            target_y = current_y
            h = c.sizeHint().height() or c.height()
            c.resize(c.width(), h)
            current_y += h + self.CARD_SPACING

            # Si la position doit changer, on anime le glissement vers le haut
            if c.y() != target_y:
                previous = self._active_anims.pop(c, None)
                if previous is not None:
                    previous.stop()
                anim = QPropertyAnimation(c, b"pos", self)
                anim.setDuration(240)
                anim.setStartValue(c.pos())
                anim.setEndValue(QPoint(self.MARGIN_RIGHT, target_y))
                anim.setEasingCurve(QEasingCurve.Type.OutCubic)
                self._active_anims[c] = anim
                anim.finished.connect(
                    lambda card=c, current=anim: (
                        self._active_anims.pop(card, None)
                        if self._active_anims.get(card) is current else None
                    )
                )
                anim.start()

        self._update_container_geometry()

    def _update_container_geometry(self):
        """Ajuste la hauteur totale du conteneur selon les cartes actives."""
        tot_h = self.MARGIN_TOP
        for c in self._cards:
            tot_h += (c.sizeHint().height() or c.height()) + self.CARD_SPACING
        self.setFixedHeight(max(100, tot_h + 20))
        self.updateGeometry()

    def dismiss_cards(self, card_type: str = "", title: str = "") -> int:
        """Ferme toutes les cartes correspondant à un type ou titre donné."""
        count = 0
        wanted_type = card_type.strip().lower()
        wanted_title = title.strip().casefold()
        for c in list(self._cards):
            match_type = not wanted_type or c.card_type == wanted_type
            match_title = not wanted_title or c.card_title.strip().casefold() == wanted_title
            if match_type and match_title:
                self.dismiss_card(c)
                count += 1
        return count

    def dismiss_all(self):
        """Ferme toutes les cartes actives."""
        for c in list(self._cards):
            self.dismiss_card(c)

    def sizeHint(self) -> QSize:
        tot_h = self.MARGIN_TOP
        for c in self._cards:
            tot_h += (c.sizeHint().height() or c.height()) + self.CARD_SPACING
        return QSize(self.width(), max(100, tot_h + 10))

    def wheelEvent(self, event):
        """Transmet l'événement molette à un conteneur QScrollArea parent de façon fluide."""
        parent = self.parent()
        while parent:
            if isinstance(parent, QScrollArea):
                vbar = parent.verticalScrollBar()
                if vbar:
                    delta = event.angleDelta().y()
                    vbar.setValue(vbar.value() - int(delta * 0.8))
                    event.accept()
                    return
            parent = parent.parent()
        super().wheelEvent(event)


# Alias de compatibilité transparente avec l'ancien système ANO-GPT
RightCardStack = CardManager


# ── Démonstration complète avec fausses données ──────────────────────────────

class RichCardDemoWindow(QMainWindow):
    """Fenêtre de démonstration complète du système de cartes riches ANO-GPT."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("ANO-GPT • Unified Rich Card System [PyQt6 Glassmorphism]")
        self.resize(1280, 840)
        self.setStyleSheet(f"background-color: {Theme.BG};")

        # Widget central
        central = QWidget(self)
        self.setCentralWidget(central)
        main_lay = QHBoxLayout(central)
        main_lay.setContentsMargins(24, 20, 24, 20)
        main_lay.setSpacing(20)

        # Panneau de contrôle gauche (HUD actions)
        dock = QFrame(central)
        dock.setFixedWidth(340)
        dock.setStyleSheet("""
            QFrame {
                background: rgba(12, 18, 28, 0.85);
                border: 1px solid rgba(0, 212, 255, 0.25);
                border-radius: 8px;
            }
        """)
        d_lay = QVBoxLayout(dock)
        d_lay.setContentsMargins(18, 16, 18, 16)
        d_lay.setSpacing(10)

        # En-tête dock
        d_title = QLabel("TABLEAU DE COMMANDES HUD", dock)
        f_dock = QFont("Inter", 8, QFont.Weight.Bold)
        f_dock.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.8)
        d_title.setFont(f_dock)
        d_title.setStyleSheet(f"color: {Theme.PRI}; border: none; background: transparent;")
        d_lay.addWidget(d_title)

        d_desc = QLabel("Générez et testez les cartes modulaires unifiées avec animations et anti-superposition.", dock)
        d_desc.setFont(QFont("Inter", 8))
        d_desc.setWordWrap(True)
        d_desc.setStyleSheet(f"color: {Theme.TEXT_DIM}; border: none; background: transparent;")
        d_lay.addWidget(d_desc)

        sep = QFrame(dock)
        sep.setFixedHeight(1)
        sep.setStyleSheet("background: rgba(0, 212, 255, 0.2); border: none;")
        d_lay.addWidget(sep)

        def make_spawn_btn(txt: str, col: str, cb: Callable):
            b = QPushButton(txt, dock)
            b.setFixedHeight(34)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setFont(QFont("Inter", 8, QFont.Weight.Bold))
            b.setStyleSheet(f"""
                QPushButton {{
                    background: rgba({qcol(col).red()}, {qcol(col).green()}, {qcol(col).blue()}, 0.12);
                    border: 1px solid rgba({qcol(col).red()}, {qcol(col).green()}, {qcol(col).blue()}, 0.4);
                    color: {Theme.WHITE};
                    border-radius: 4px;
                    text-align: left;
                    padding-left: 12px;
                }}
                QPushButton:hover {{
                    background: rgba({qcol(col).red()}, {qcol(col).green()}, {qcol(col).blue()}, 0.25);
                    border: 1px solid {col};
                    color: {col};
                }}
            """)
            b.clicked.connect(cb)
            return b

        d_lay.addWidget(make_spawn_btn("▶ MediaCard (YouTube/Spotify)", Theme.NEON_PINK, self.spawn_media))
        d_lay.addWidget(make_spawn_btn("🌦️ WeatherCard (Météo 24h)", Theme.PRI, self.spawn_weather))
        d_lay.addWidget(make_spawn_btn("⚡ TelemetryCard (CPU/RAM/Net)", Theme.GREEN, self.spawn_telemetry))
        d_lay.addWidget(make_spawn_btn("🤖 PlanCard (Agent Pipeline)", Theme.NEON_AMBER, self.spawn_plan))
        d_lay.addWidget(make_spawn_btn("🔔 Alerte Auto-Dismiss (5s)", Theme.RED, self.spawn_alert))

        d_lay.addSpacing(10)
        d_lay.addWidget(make_spawn_btn("⏭️ Avancer Étape Agent", Theme.WHITE, self.advance_plan))
        d_lay.addWidget(make_spawn_btn("🗑️ Tout Fermer (Dismiss All)", Theme.RED, self.dismiss_all))

        d_lay.addStretch()

        # Indicateur de statut
        self._status_lbl = QLabel("Système unifié actif • 0 superposition garantie", dock)
        self._status_lbl.setFont(QFont("Inter", 7))
        self._status_lbl.setStyleSheet(f"color: {Theme.TEXT_DIM}; border: none; background: transparent;")
        d_lay.addWidget(self._status_lbl)

        main_lay.addWidget(dock)

        # Zone centrale décorative avec orbe stylisée
        center_zone = QFrame(central)
        center_zone.setStyleSheet("background: transparent; border: none;")
        c_lay = QVBoxLayout(center_zone)
        c_lay.setAlignment(Qt.AlignmentFlag.AlignCenter)

        center_title = QLabel("ANO-GPT SCI-FI WORKSPACE", center_zone)
        center_title.setFont(QFont("Inter", 16, QFont.Weight.Bold))
        center_title.setStyleSheet("color: rgba(0, 212, 255, 0.4); letter-spacing: 2px;")
        c_lay.addWidget(center_title, alignment=Qt.AlignmentFlag.AlignCenter)

        center_sub = QLabel("Pile de notifications persistantes et éphémères à droite →", center_zone)
        center_sub.setFont(QFont("Inter", 10))
        center_sub.setStyleSheet("color: rgba(0, 212, 255, 0.25);")
        c_lay.addWidget(center_sub, alignment=Qt.AlignmentFlag.AlignCenter)

        main_lay.addWidget(center_zone, stretch=1)

        # Orchestrateur CardManager ancré sur la droite
        scroll = QScrollArea(central)
        scroll.setFixedWidth(GlassCard.CARD_WIDTH + 24)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("""
            QScrollArea { background: transparent; border: none; }
            QScrollBar:vertical, QScrollBar:horizontal {
                width: 0px; height: 0px; background: transparent; border: none;
            }
        """)

        self.manager = CardManager(scroll)
        scroll.setWidget(self.manager)
        main_lay.addWidget(scroll)

        # Référence active pour le test de PlanCard
        self._active_plan_card: Optional[PlanCard] = None
        self._active_media_card: Optional[MediaCard] = None

        # Minuteur de simulation pour animer la timeline MediaCard
        self._demo_timer = QTimer(self)
        self._demo_timer.setInterval(1000)
        self._demo_timer.timeout.connect(self._tick_demo_playback)
        self._demo_timer.start()

        # Initialisation avec les 4 cartes riches pré-remplies
        QTimer.singleShot(150, self.spawn_media)
        QTimer.singleShot(350, self.spawn_weather)
        QTimer.singleShot(550, self.spawn_telemetry)
        QTimer.singleShot(750, self.spawn_plan)

    def spawn_media(self):
        card = MediaCard(
            title="Nightcall (Cyber Remix)",
            artist="Kavinsky",
            album="OutRun (2013)",
            duration_s=259.0,
            source="YouTube",
            parent=self.manager,
        )
        card.set_position(64.0)
        card.set_playing(True)
        self._active_media_card = card
        self.manager.add_card(card)

    def spawn_weather(self):
        card = WeatherCard(
            city="Neo-Paris, FR",
            temp_c=22.4,
            condition="Ciel clair & Ensoleillé",
            wind_kmh=16.0,
            humidity_pct=52,
            parent=self.manager,
        )
        self.manager.add_card(card)

    def spawn_telemetry(self):
        card = TelemetryCard(update_interval_ms=1000, parent=self.manager)
        self.manager.add_card(card)

    def spawn_plan(self):
        steps = [
            {"id": 1, "title": "Analyse de la structure de code", "status": "done", "detail": "Revue de 14 fichiers UI", "duration": "0.4s"},
            {"id": 2, "title": "Refonte glassmorphism PyQt6", "status": "running", "detail": "Rendu rgba(15, 15, 25, 0.85) et gradient", "duration": "1.2s"},
            {"id": 3, "title": "Tests anti-superposition CardManager", "status": "pending", "detail": "Validation géométrie stricte"},
            {"id": 4, "title": "Validation visuelle & Intégration E2E", "status": "pending", "detail": "Tests unitaires & QA"},
        ]
        card = PlanCard(
            objective="Implémentation du système unifié de cartes riches",
            steps=steps,
            parent=self.manager,
        )
        self._active_plan_card = card
        self.manager.add_card(card)

    def spawn_alert(self):
        self.manager.add_card(
            card_or_type="error",
            title="Synchro Satellite Terminée",
            body="Les données de télémétrie locale ont été synchronisées avec succès. Auto-fermeture dans 5 secondes.",
            auto_dismiss_s=5.0,
        )

    def advance_plan(self):
        if not self._active_plan_card or not self._active_plan_card._steps:
            return
        steps = self._active_plan_card._steps
        for idx, s in enumerate(steps):
            if s.get("status") == "running":
                s["status"] = "done"
                s["duration"] = f"{random.uniform(0.5, 2.0):.1f}s"
                if idx + 1 < len(steps):
                    steps[idx + 1]["status"] = "running"
                    steps[idx + 1]["detail"] = "En cours d'exécution par l'agent..."
                break
        self._active_plan_card.set_steps(steps)

    def dismiss_all(self):
        self.manager.dismiss_all()

    def _tick_demo_playback(self):
        if self._active_media_card and self._active_media_card._is_playing:
            pos = self._active_media_card._current_pos_s + 1.0
            if pos > self._active_media_card._duration_s:
                pos = 0.0
            self._active_media_card.set_position(pos)


# ── Point d'entrée principal ──────────────────────────────────────────────────
if __name__ == "__main__":
    app = QApplication(sys.argv)
    demo = RichCardDemoWindow()
    demo.show()
    sys.exit(app.exec())
