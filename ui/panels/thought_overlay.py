"""ui/panels/thought_overlay.py — Bulle translucide de pensée en cours sous l'orbe.

Affiche en direct les micro-étapes de réflexion de l'assistant (Thinking Tokens)
dans une bulle cyberpunk translucide avec texte gris doux défilant.
"""

from __future__ import annotations

import math
import time
from typing import Callable, Optional

from PyQt6.QtCore import (
    QEasingCurve, QPointF, QPropertyAnimation, QRect, QRectF, Qt, QTimer,
)
from PyQt6.QtGui import (
    QBrush, QColor, QFont, QFontMetrics, QLinearGradient, QPainter,
    QPen,
)
from PyQt6.QtWidgets import QGraphicsOpacityEffect, QWidget



class ThoughtOverlay(QWidget):
    """Bulle translucide sous l'orbe affichant 'Pensée en cours...' et les micro-étapes.

    Caractéristiques visuelles :
    - Fond glassmorphism sombre translucide (rgba) avec lueur néon subtile
    - En-tête avec voyant pulsant et libellé 'PENSÉE EN COURS...'
    - Texte en gris doux (#94a3b8 / Slate-400) défilant en douceur si la phrase
      dépasse la largeur disponible (effet ticker cyberpunk)
    - Animations fluides d'opacité et de géométrie
    """

    FIXED_HEIGHT = 56
    MIN_WIDTH = 340
    MAX_WIDTH = 580
    _PAD_X = 36

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(self.FIXED_HEIGHT)
        self.setMinimumWidth(self.MIN_WIDTH)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setMouseTracking(False)
        self.setStyleSheet("background: transparent;")

        # Effet d'opacité avec animation de fondu
        self._opacity = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._opacity)
        self._opacity.setOpacity(0.0)

        self._anim = QPropertyAnimation(self._opacity, b"opacity")
        self._anim.setDuration(190)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        # Animation de géométrie pour transitions douces de dimension
        self._geo_anim = QPropertyAnimation(self, b"geometry")
        self._geo_anim.setDuration(160)
        self._geo_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        # Horloge interne pour la pulsation du voyant et le défilement du texte
        self._t0 = time.monotonic()
        self._raw_text: str = ""
        self._scroll_x: float = 0.0
        self._text_width: int = 0
        self._is_scrolling: bool = False
        self._reposition_cb: Optional[Callable[[], None]] = None

        # Minuteur du ticker de défilement (30 FPS)
        self._ticker_timer = QTimer(self)
        self._ticker_timer.timeout.connect(self._on_ticker_tick)

        # Minuteur de pulsation du voyant
        self._pulse_timer = QTimer(self)
        self._pulse_timer.timeout.connect(self.update)
        self._pulse_timer.start(50)

    def set_reposition_callback(self, cb: Optional[Callable[[], None]]) -> None:
        """Définit le callback pour recalculer et animer la position sous l'orbe."""
        self._reposition_cb = cb

    def animate_to(self, rect: QRect) -> None:
        """Anime doucement le widget vers son nouveau rectangle géométrique."""
        if not self.isVisible() or self.geometry().isEmpty():
            self.setGeometry(rect)
            return
        self._geo_anim.stop()
        self._geo_anim.setStartValue(self.geometry())
        self._geo_anim.setEndValue(rect)
        self._geo_anim.start()

    def show_thought(self, text: str) -> None:
        """Affiche la micro-étape de pensée avec animation de fondu."""
        clean = (text or "").strip()
        if not clean:
            self.fade_out()
            return

        is_new = (clean != self._raw_text)
        self._raw_text = clean

        # Mesure de la largeur du texte pour déterminer si le défilement est requis
        font = QFont("Inter", 10, QFont.Weight.Medium)
        fm = QFontMetrics(font)
        self._text_width = fm.horizontalAdvance(self._raw_text)

        if is_new:
            self._scroll_x = 0.0

        # Recalcule la largeur idéale
        target_w = max(self.MIN_WIDTH, min(self.MAX_WIDTH, self._text_width + self._PAD_X))
        available_budget = target_w - self._PAD_X

        # Active le ticker si le texte déborde du budget disponible
        if self._text_width > available_budget:
            if not self._ticker_timer.isActive():
                self._ticker_timer.start(35)
            self._is_scrolling = True
        else:
            self._ticker_timer.stop()
            self._scroll_x = 0.0
            self._is_scrolling = False

        if self._reposition_cb:
            self._reposition_cb()

        # Fondu d'apparition
        if self._opacity.opacity() < 0.95:
            self._anim.stop()
            self._anim.setStartValue(self._opacity.opacity())
            self._anim.setEndValue(1.0)
            self._anim.start()
            self.show()
            self.raise_()

        self.update()

    def fade_out(self) -> None:
        """Fait disparaître en douceur la bulle."""
        self._ticker_timer.stop()
        self._anim.stop()
        self._anim.setStartValue(self._opacity.opacity())
        self._anim.setEndValue(0.0)
        self._anim.start()

    def _on_ticker_tick(self) -> None:
        """Avance d'un pas le défilement horizontal du texte ticker."""
        if not self._is_scrolling or not self.isVisible():
            return

        max_scroll = self._text_width + 50
        self._scroll_x += 1.2
        if self._scroll_x > max_scroll:
            self._scroll_x = -30.0

        self.update()

    def paintEvent(self, _) -> None:
        """Rendu graphique du conteneur en verre néon translucide et du texte défilant."""
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        W, H = self.width(), self.height()
        if W <= 0 or H <= 0:
            p.end()
            return

        t = time.monotonic() - self._t0

        # ── 1. Fond en verre translucide (Glassmorphism cyberpunk) ────────────
        card = QRectF(3, 2, W - 6, H - 4)
        bg_grad = QLinearGradient(0, 0, W, H)
        bg_grad.setColorAt(0.0, QColor(2, 14, 24, 205))
        bg_grad.setColorAt(0.6, QColor(6, 12, 22, 195))
        bg_grad.setColorAt(1.0, QColor(14, 8, 26, 205))

        # Aura diffuse extérieure
        glow_pen = QPen(QColor(0, 212, 255, 30), 4)
        p.setPen(glow_pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(card, 12.0, 12.0)

        # Contour fin violet/cyan
        border_pen = QPen(QColor(175, 105, 255, 110), 1.2)
        p.setPen(border_pen)
        p.setBrush(QBrush(bg_grad))
        p.drawRoundedRect(card, 12.0, 12.0)

        # Reflet lumineux en biseau supérieur
        sheen = QLinearGradient(0, card.top(), 0, card.top() + 16)
        sheen.setColorAt(0.0, QColor(0, 212, 255, 38))
        sheen.setColorAt(1.0, QColor(0, 212, 255, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(sheen))
        p.drawRoundedRect(QRectF(card.left(), card.top(), card.width(), 16), 12.0, 12.0)

        # Délicats coins cyber (ticks)
        tick = 12.0
        tick_pen = QPen(QColor(0, 212, 255, 180), 1.5)
        p.setPen(tick_pen)
        for cx, cy, dx, dy in (
            (card.left(), card.top(), 1, 1),
            (card.right(), card.top(), -1, 1),
            (card.left(), card.bottom(), 1, -1),
            (card.right(), card.bottom(), -1, -1),
        ):
            p.drawLine(QPointF(cx, cy), QPointF(cx + tick * dx, cy))
            p.drawLine(QPointF(cx, cy), QPointF(cx, cy + tick * dy))

        # ── 2. En-tête : voyant pulsant + libellé 'PENSÉE EN COURS...' ────────
        pulse = 0.5 + 0.5 * math.sin(t * 3.5)
        dot_r = 3.5 + 0.8 * pulse
        dot_x = card.left() + 20
        dot_y = card.top() + 15

        # Halo du voyant
        halo_color = QColor(175, 105, 255, int(70 + 80 * pulse))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(halo_color))
        p.drawEllipse(QPointF(dot_x, dot_y), dot_r + 3.0, dot_r + 3.0)

        # Cœur du voyant (ambre/violet vif)
        core_color = QColor(220, 180, 255, 230)
        p.setBrush(QBrush(core_color))
        p.drawEllipse(QPointF(dot_x, dot_y), dot_r, dot_r)

        # Libellé en-tête
        hdr_font = QFont("Inter", 8, QFont.Weight.Bold)
        hdr_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 2.0)
        p.setFont(hdr_font)
        p.setPen(QColor(0, 212, 255, 220))
        p.drawText(
            QRectF(dot_x + 10, card.top() + 8, card.width() - 40, 14),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            "◈ PENSÉE EN COURS...",
        )

        # ── 3. Texte en gris doux défilant (micro-étape de raisonnement) ───────
        txt_font = QFont("Inter", 10, QFont.Weight.Normal)
        p.setFont(txt_font)
        p.setPen(QColor(148, 163, 184))  # Gris doux Slate-400

        text_clip = QRectF(card.left() + 18, card.top() + 25, card.width() - 36, 22)
        p.save()
        p.setClipRect(text_clip)

        if self._is_scrolling:
            # Texte défilant (ticker)
            text_x = text_clip.left() - self._scroll_x
            text_y = text_clip.top()
            p.drawText(
                QRectF(text_x, text_y, self._text_width + 10, text_clip.height()),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                self._raw_text,
            )
        else:
            # Texte centré ou aligné proprement
            p.drawText(
                text_clip,
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                self._raw_text,
            )

        p.restore()
        p.end()
        super().paintEvent(_)
