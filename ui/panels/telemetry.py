from __future__ import annotations

import math
import random


from PyQt6.QtCore import (
    QPointF, QRectF, Qt,
    QTimer,
)
from PyQt6.QtGui import (
    QBrush, QColor, QFont, QPainter,
    QPen, QRadialGradient,
)
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget,
)

from ui.core.hud_paint import Hud
from ui.core.metrics import _metrics  # noqa: F401
from ui.styles.theme import C, qcol

class MetricBar(QWidget):
    """Jauge système : intitulé + valeur sur une ligne, piste fine en dessous.

    Pas de cadre : la lecture vient du contraste et de l'alignement. La valeur
    est lissée à l'affichage pour que la barre glisse au lieu de sauter, et la
    couleur bascule vers l'ambre puis le rouge au-delà des seuils.
    """

    _WARN, _CRIT = 65.0, 85.0

    def __init__(self, label: str, icon: str = "●", color: str = C.PRI, parent=None):
        super().__init__(parent)
        self._label = label
        self._icon = icon
        self._color = color
        self._value = 0.0        # cible
        self._shown = 0.0        # valeur animée
        self._text = "--"
        self.setFixedHeight(30)
        self.setMinimumWidth(80)
        # Le QSS global donne un fond à tout QWidget : sans cette règle, ce
        # widget peint un rectangle opaque qui perfore le panneau sous-jacent.
        self.setStyleSheet("background: transparent;")
        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._ease)
        self._tmr.start(33)

    def set_value(self, pct: float, text: str):
        self._value = max(0.0, min(100.0, pct))
        self._text = text
        self.update()

    def _ease(self):
        d = self._value - self._shown
        if abs(d) < 0.15:
            if self._shown != self._value:
                self._shown = self._value
                self.update()
            return
        self._shown += d * 0.18
        self.update()

    def _tone(self) -> str:
        if self._shown >= self._CRIT:
            return C.RED
        if self._shown >= self._WARN:
            return C.ACC
        return self._color

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        tone = self._tone()
        live = self._text != "--"

        # Intitulé en micro-capitales espacées, comme les étiquettes de la carte.
        p.setFont(Hud.micro_font(7, 1.8))
        p.setPen(QPen(qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(0, 1, W * 0.55, 14),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   self._label)

        # Valeur, calée à droite : c'est l'alignement qui l'empêche de danser.
        p.setFont(QFont("Inter", 11, QFont.Weight.Bold))
        p.setPen(QPen(qcol(tone if live else C.TEXT_DIM), 1))
        p.drawText(QRectF(W * 0.4, 0, W * 0.6, 17),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   self._text)

        # Piste segmentée : chaque cran vaut 5 %, ce qui rend une valeur
        # lisible d'un coup d'œil sans avoir à lire le nombre.
        bar_h, bar_y = 4.0, H - 10.0
        gap, cells = 2.0, 20
        cell_w = max(2.0, (W - gap * (cells - 1)) / cells)
        filled = self._shown / 100.0 * cells

        for index in range(cells):
            x = index * (cell_w + gap)
            cell = QRectF(x, bar_y, cell_w, bar_h)
            covered = filled - index
            if covered <= 0:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(QColor(0, 212, 255, 26)))
                p.drawRect(cell)
                continue
            if covered < 1:
                # Cran partiel : c'est lui qui fait glisser la jauge au lieu
                # de la faire sauter de 5 % en 5 %.
                cell.setWidth(cell_w * covered)
            ratio = index / max(1, cells - 1)
            colour = qcol(tone)
            if self._shown >= self._WARN and ratio > 0.72:
                colour = qcol(C.NEON_PINK)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(colour))
            p.drawRect(cell)

        if filled > 0.05:
            head = min(W, filled * (cell_w + gap))
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
            halo = QRadialGradient(head, bar_y + bar_h / 2, 12)
            halo.setColorAt(0.0, qcol(tone, 110))
            halo.setColorAt(1.0, qcol(tone, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(halo))
            p.drawEllipse(QPointF(head, bar_y + bar_h / 2), 12, 12)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        p.end()


class LiveTranscriptWidget(QFrame):
    """Ce que l'utilisateur est en train de dire, écrit en direct.

    Le texte partiel s'écrit mot à mot pendant qu'il parle (point pulsant,
    teinte accent) ; à la fin du tour il se fige en teinte atténuée jusqu'à la
    phrase suivante. Sans ce retour, rien à l'écran ne distingue « je n'ai pas
    été entendu » de « je n'ai pas été compris ».
    """

    _PLACEHOLDER = "Parlez — votre voix s'écrira ici."
    # On garde la FIN du texte (ce qui vient d'être dit), donc la coupe doit
    # tenir dans les trois lignes affichées : au-delà, ce sont justement les
    # derniers mots qui passeraient sous le bord et disparaîtraient.
    _MAX_CHARS   = 60
    _TEXT_H      = 48      # trois lignes compactes, sans écraser les petits écrans

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("LiveTranscript")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # Le cadre est peint dans paintEvent, comme tous les panneaux.
        self.setStyleSheet(
            "QFrame#LiveTranscript { background: transparent; border: none; }"
            "QLabel { background: transparent; }")
        self._live = False        # défini avant le minuteur qui le consulte
        self._scan = random.random() * Hud.SCAN_PERIOD
        self._fx = QTimer(self)
        self._fx.timeout.connect(self._tick_fx)
        self._fx.start(110)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 9, 11, 10)
        lay.setSpacing(5)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(6)
        self._dot = QLabel("●")
        self._dot.setFont(QFont("Inter", 6))
        self._dot.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        cap = QLabel("VOTRE VOIX")
        capf = QFont("Inter", 6, QFont.Weight.Bold)
        capf.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.3)
        cap.setFont(capf)
        cap.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        head.addWidget(self._dot)
        head.addWidget(cap)
        head.addStretch()
        lay.addLayout(head)

        self._text = QLabel(self._PLACEHOLDER)
        self._text.setWordWrap(True)
        self._text.setFont(QFont("Inter", 8))
        self._text.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._text.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        # Hauteur fixe : le panneau ne doit pas sauter à chaque mot reconnu.
        self._text.setFixedHeight(self._TEXT_H)
        lay.addWidget(self._text)

        self._live = False
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._pulse)
        self._timer.setInterval(60)

    def _pulse(self):
        self._phase = (self._phase + 0.12) % (2 * math.pi)
        a = 0.45 + 0.55 * (0.5 + 0.5 * math.sin(self._phase))
        self._dot.setStyleSheet(
            f"color: rgba(0, 212, 255, {a:.2f}); background: transparent;")

    def set_transcript(self, text: str, final: bool = False):
        text = (text or "").strip()
        if not text:
            self._text.setText(self._PLACEHOLDER)
            self._text.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            self._set_live(False)
            return
        if len(text) > self._MAX_CHARS:
            text = "…" + text[-self._MAX_CHARS:]
        self._text.setText(text)
        self._text.setStyleSheet(
            f"color: {C.TEXT_MED if final else C.PRI}; background: transparent;")
        self._set_live(not final)

    def _set_live(self, live: bool):
        if live == self._live:
            return
        self._live = live
        if live:
            self._timer.start()
        else:
            self._timer.stop()
            self._dot.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")

    def _tick_fx(self):
        # Le balayage n'a de sens que pendant l'écoute : au repos, un panneau
        # qui s'anime tout seul attire l'œil pour rien.
        if self._live:
            self._scan = (self._scan + 1.1) % Hud.SCAN_PERIOD
            self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(2, 2, self.width() - 4, self.height() - 4)
        Hud.chassis(p, rect, accent=qcol(C.PRI),
                    scan=self._scan if self._live else -1.0,
                    grid=False, cut=8.0, fill_alpha=228)
        p.end()
        super().paintEvent(event)
