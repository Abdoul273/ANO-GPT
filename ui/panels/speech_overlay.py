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

class CenterSpeechOverlay(QWidget):
    """Bulle de transcription vocale, style HUD cyberpunk : hauteur FIXE,
    largeur qui suit la longueur du texte (une seule ligne, jamais de
    retour à la ligne). Affiche les transcriptions de l'assistant et de
    l'utilisateur.
    """

    FIXED_HEIGHT = 80
    MIN_WIDTH = 280
    _PAD_X = 44          # marge horizontale totale (gauche + droite) autour du texte
    _MIN_FONT_PT = 13    # taille plancher avant de passer à l'ellipse

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(self.FIXED_HEIGHT)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setMouseTracking(False)

        self._opacity = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._opacity)
        self._opacity.setOpacity(0.0)

        self._anim = QPropertyAnimation(self._opacity, b"opacity")
        # La carte ne doit jamais apparaître après la syllabe / le fragment
        # qu'elle représente. Une entrée courte reste élégante sans introduire
        # de retard perceptible.
        self._anim.setDuration(90)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        # Anime la géométrie (largeur/position, la hauteur ne bouge jamais)
        # à chaque nouvelle phrase : la carte "respire" et s'élargit en
        # douceur au lieu de sauter brutalement à sa nouvelle taille.
        self._geo_anim = QPropertyAnimation(self, b"geometry")
        self._geo_anim.setDuration(90)
        self._geo_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.fade_out)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 8, 22, 8)
        lay.setSpacing(3)

        self._hdr_lbl = QLabel("")
        self._hdr_lbl.setFont(QFont("Inter", 8, QFont.Weight.Bold))
        self._hdr_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._hdr_lbl.setStyleSheet(f"color: {C.PRI_DIM}; background: transparent; letter-spacing: 3px;")
        lay.addWidget(self._hdr_lbl)

        self._txt_lbl = QLabel("")
        self._txt_lbl.setFont(QFont("Inter", 20, QFont.Weight.Normal))
        self._txt_lbl.setWordWrap(False)
        self._txt_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._txt_lbl.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        lay.addWidget(self._txt_lbl, stretch=1)

        # Poussé par la fenêtre principale (voir _position_speech_overlay) :
        # rappelé à chaque nouvelle phrase pour recalculer une géométrie
        # ancrée en bas de l'écran, juste au-dessus de la barre de saisie,
        # et l'atteindre en douceur via animate_to().
        self._reposition_cb = None
        self._base_pt = 20
        self._raw_text = ""
        self._assistant_name = "ANO-GPT"
        self._is_live = False
        self._active_speaker: str | None = None
        self._realtime_update = False

    def set_assistant_name(self, name: str) -> None:
        self._assistant_name = (name or "ANO-GPT").strip()

    def set_reposition_callback(self, cb):
        self._reposition_cb = cb

    def animate_to(self, rect: QRect):
        if not self.isVisible() or self.geometry().isEmpty():
            self.setGeometry(rect)
            return
        if self._realtime_update:
            # Les fragments arrivent à cadence vocale. Rejouer une animation
            # sur chacun produisait un retard accumulé ; l'ancrage suit donc
            # directement le texte déjà visible.
            self._geo_anim.stop()
            self.setGeometry(rect)
            return
        self._geo_anim.stop()
        self._geo_anim.setStartValue(self.geometry())
        self._geo_anim.setEndValue(rect)
        self._geo_anim.start()

    def show_speech(self, text: str, speaker: str = "ai", final: bool = True):
        clean = text.strip()
        if not clean:
            return
        self._realtime_update = self.isVisible() and self._active_speaker == speaker
        self._active_speaker = speaker

        if speaker == "ai":
            display_name = self._assistant_name.upper()
            if display_name in ("JARVIS", "J.A.R.V.I.S"):
                display_name = "J.A.R.V.I.S"
            self._hdr_lbl.setText(f"◈  {display_name}  ◈")
            self._hdr_lbl.setStyleSheet(f"color: {C.NEON_AMBER}; background: transparent; letter-spacing: 3px;")
            self._base_pt = 21
            self._txt_lbl.setStyleSheet(f"color: {C.WHITE}; background: transparent;")
        else:
            self._is_live = not final
            if final:
                self._hdr_lbl.setText("✓  ANONYMOUS")
                header_color = C.TEXT_MED
            else:
                self._hdr_lbl.setText("●  ANONYMOUS  ·  ÉCOUTE EN DIRECT")
                header_color = C.PRI
            self._hdr_lbl.setStyleSheet(
                f"color: {header_color}; background: transparent; letter-spacing: 1px;")
            self._base_pt = 16
            self._txt_lbl.setStyleSheet(
                f"color: {C.WHITE if final else C.PRI}; background: transparent;")

        # `text` est déjà la phrase accumulée jusqu'ici (le buffer complet
        # géré côté fenêtre principale, voir _handle_log) : on la garde
        # brute, la mise en forme (taille de police / ellipse) se fait dans
        # fit_to_width() une fois que la fenêtre principale connaît la
        # largeur disponible.
        self._raw_text = clean

        # Recalcule la géométrie (largeur dépendante du texte, hauteur
        # toujours fixe) et anime vers elle plutôt que de sauter — c'est ce
        # qui donne l'impression que la carte "suit" la phrase.
        if self._reposition_cb:
            self._reposition_cb()
        else:
            self.fit_to_width(600)

        self._hide_timer.stop()
        self._hide_timer.start(5000 if final or speaker == "ai" else 9000)

        if self._realtime_update:
            # Le contenu est un remplacement du même flux : visible au même
            # tour Qt, sans attente de fondu.
            self._anim.stop()
            self._opacity.setOpacity(1.0)
            self.show()
            self.raise_()
        elif self._opacity.opacity() < 0.95:
            self._anim.stop()
            self._anim.setStartValue(self._opacity.opacity())
            self._anim.setEndValue(1.0)
            self._anim.start()
            self.show()
            self.raise_()

    def fit_to_width(self, max_width: int) -> int:
        """Choisit la plus grande taille de police (entre _MIN_FONT_PT et
        _base_pt) qui tient sur une seule ligne dans `max_width`, et réduit
        le texte par une ellipse en dernier recours. Renvoie la largeur
        totale à donner au widget (texte + marges), jamais plus que
        `max_width`, jamais moins que MIN_WIDTH."""
        text_budget = max(40, max_width - self._PAD_X)

        pt = self._base_pt
        font = QFont("Inter", pt, QFont.Weight.Normal)
        fm = QFontMetrics(font)
        while pt > self._MIN_FONT_PT and fm.horizontalAdvance(self._raw_text) > text_budget:
            pt -= 1
            font.setPointSize(pt)
            fm = QFontMetrics(font)

        display = self._raw_text
        text_w = fm.horizontalAdvance(display)
        if text_w > text_budget:
            display = fm.elidedText(self._raw_text, Qt.TextElideMode.ElideRight, text_budget)
            text_w = fm.horizontalAdvance(display)

        self._txt_lbl.setFont(font)
        self._txt_lbl.setText(display)
        self._txt_lbl.setToolTip(self._raw_text if display != self._raw_text else "")

        return max(self.MIN_WIDTH, min(max_width, text_w + self._PAD_X))

    def fit_to_bounds(self, max_width: int, _max_height: int) -> QSize:
        """Renvoie la taille calculée tout en conservant la hauteur fixe."""
        return QSize(self.fit_to_width(max_width), self.FIXED_HEIGHT)

    def fade_out(self):
        self._anim.stop()
        self._anim.setStartValue(self._opacity.opacity())
        self._anim.setEndValue(0.0)
        self._anim.start()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()

        card = QRectF(3, 3, W - 6, H - 6)
        grad = QLinearGradient(0, 0, W, H)
        grad.setColorAt(0.0, QColor(0, 28, 39, 242) if self._is_live else QColor(0, 18, 28, 236))
        grad.setColorAt(0.58, QColor(7, 10, 22, 230))
        grad.setColorAt(1.0, QColor(18, 3, 24, 236))

        glow = QPen(QColor(0, 225, 255, 40), 7)
        p.setPen(glow)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(card, 14.0, 14.0)

        p.setPen(QPen(QColor(0, 245, 255, 205) if self._is_live else QColor(0, 225, 255, 130), 1.4))
        p.setBrush(QBrush(grad))
        p.drawRoundedRect(card, 14.0, 14.0)

        # Fin trait de "verre" sur le haut de la carte (reflet lumineux).
        sheen = QLinearGradient(0, card.top(), 0, card.top() + 22)
        sheen.setColorAt(0.0, QColor(0, 230, 255, 45))
        sheen.setColorAt(1.0, QColor(0, 230, 255, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(sheen))
        p.drawRoundedRect(QRectF(card.left(), card.top(), card.width(), 22), 14.0, 14.0)

        p.setPen(QPen(QColor(143, 252, 255, 20), 1))
        for y in range(28, H - 8, 10):
            p.drawLine(18, y, W - 18, y)
        p.setPen(QPen(QColor(255, 43, 214, 92), 1.2))
        p.drawLine(QPointF(card.left() + 34, card.bottom() - 8),
                   QPointF(card.left() + min(160, card.width() * 0.45), card.bottom() - 8))

        if self._is_live:
            # Indicateur fixe et peu coûteux : il distingue une hypothèse en
            # cours de la phrase finale sans voler de cycles au thread audio.
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 240, 255, 210))
            p.drawEllipse(QPointF(card.right() - 18, card.top() + 16), 3.0, 3.0)

        tick = 18.0
        pen = QPen(QColor(0, 225, 255, 210), 2)
        p.setPen(pen)
        for cx, cy, dx, dy in (
            (card.left(), card.top(), 1, 1),
            (card.right(), card.top(), -1, 1),
            (card.left(), card.bottom(), 1, -1),
            (card.right(), card.bottom(), -1, -1),
        ):
            p.drawLine(QPointF(cx, cy), QPointF(cx + tick * dx, cy))
            p.drawLine(QPointF(cx, cy), QPointF(cx, cy + tick * dy))

        p.end()
        super().paintEvent(_)
