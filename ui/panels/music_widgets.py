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
from ui.styles.theme import C, make_svg_icon, qcol

class _SeekSlider(QSlider):
    """QSlider cliquable n'importe où sur la piste : clique où tu veux,
    la lecture saute directement à cette position (pas besoin de glisser
    depuis la poignée)."""

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            ratio = min(1.0, max(0.0, e.position().x() / max(1, self.width())))
            val = round(self.minimum() + ratio * (self.maximum() - self.minimum()))
            self.setValue(val)
            self.sliderMoved.emit(val)
            e.accept()
        super().mousePressEvent(e)


class _CoverArt(QWidget):
    """Pochette encadrée, cerclée d'un anneau qui tourne pendant la lecture.

    L'anneau ne tourne qu'en lecture : c'est le seul élément du panneau qui
    distingue « en pause » de « en cours » sans avoir à lire une étiquette.
    """

    _SIZE = 54

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(self._SIZE, self._SIZE)
        self._angle = 0.0
        self._playing = False
        # Le QSS global donne un fond à tout QWidget : sans cette règle, ce
        # widget peint un rectangle opaque qui perfore le panneau sous-jacent.
        self.setStyleSheet("background: transparent;")
        self._pix = None
        self._cache = None          # fond figé, reconstruit au changement d'image
        self._fallback = make_svg_icon("music", C.PRI, 22).pixmap(22, 22)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._spin)

    def set_playing(self, playing: bool) -> None:
        if playing == self._playing:
            return
        self._playing = playing
        if playing:
            self._timer.start(60)
        else:
            self._timer.stop()
        self.update()

    def setPixmap(self, pixmap) -> None:      # noqa: N802 - imite QLabel
        """Accepte une pochette réelle, si le lecteur en fournit une."""
        self._pix = pixmap if pixmap is not None and not pixmap.isNull() else None
        self._cache = None          # la pochette a changé : refaire le fond
        self.update()

    def _spin(self):
        if not self.isVisible():
            return
        self._angle = (self._angle + 2.6) % 360.0
        self.update()

    def _static(self) -> QPixmap:
        """Cadre et pochette, peints une seule fois.

        Ils ne changent qu'au chargement d'une nouvelle image ; les reconstruire
        vingt fois par seconde volait 1,4 ms par image à la boucle audio.
        """
        if self._cache is not None:
            return self._cache
        pixmap = QPixmap(self._SIZE, self._SIZE)
        pixmap.fill(Qt.GlobalColor.transparent)
        rect = QRectF(4, 4, self._SIZE - 8, self._SIZE - 8)
        frame = Hud.bevel(rect, 7.0)
        p = QPainter(pixmap)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(8, 22, 32, 230)))
        p.drawPath(frame)
        if self._pix is not None:
            p.save()
            p.setClipPath(frame)
            p.drawPixmap(rect.toRect(), self._pix.scaled(
                rect.size().toSize(), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation))
            p.restore()
        else:
            p.drawPixmap(QPointF(rect.center().x() - 11, rect.center().y() - 11),
                         self._fallback)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(qcol(C.PRI, 120), 1.1))
        p.drawPath(frame)
        p.end()
        self._cache = pixmap
        return pixmap

    def paintEvent(self, event):
        p = QPainter(self)
        p.drawPixmap(0, 0, self._static())

        if self._playing:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.save()
            center = QPointF(self._SIZE / 2, self._SIZE / 2)
            p.translate(center)
            p.rotate(self._angle)
            p.translate(-center)
            ring = QRectF(1.5, 1.5, self._SIZE - 3, self._SIZE - 3)
            p.setPen(QPen(qcol(C.PRI, 210), 1.6))
            p.drawArc(ring, 0, 70 * 16)
            p.setPen(QPen(qcol(C.NEON_PINK, 190), 1.6))
            p.drawArc(ring, 180 * 16, 70 * 16)
            p.restore()
        p.end()


class _NeonSeek(_SeekSlider):
    """Piste de lecture peinte : rail cranté, remplissage néon, tête lumineuse.

    La feuille de style Qt ne sait produire ni les crans ni le halo de la tête,
    et le rectangle arrondi qu'elle dessinait détonnait au milieu de panneaux
    à angles coupés.
    """

    def __init__(self, parent=None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.setRange(0, 1000)
        self.setFixedHeight(16)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet("background: transparent;")
        self._live = False

    def set_live(self, live: bool) -> None:
        """Marque la lecture en cours.

        Aucune animation propre : la piste se redessine quand la position
        change, c'est-à-dire une fois par seconde. Faire pulser la tête à
        dix-sept images par seconde coûtait 1 ms à chaque fois, prise sur la
        boucle audio — pour un halo que personne ne regarde.
        """
        if self._live != live:
            self._live = live
            self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        mid = H / 2.0
        span = max(1, self.maximum() - self.minimum())
        ratio = (self.value() - self.minimum()) / span
        head = max(0.0, min(1.0, ratio)) * W

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(0, 14, 22, 220)))
        p.drawRect(QRectF(0, mid - 2, W, 4))

        if head > 1:
            fill = QLinearGradient(0, 0, max(1.0, head), 0)
            fill.setColorAt(0.0, qcol(C.PRI, 130))
            fill.setColorAt(0.75, qcol(C.PRI))
            fill.setColorAt(1.0, qcol(C.NEON_PINK))
            p.setBrush(QBrush(fill))
            p.drawRect(QRectF(0, mid - 2, head, 4))

            glow = 1.0 if self._live else 0.55
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
            halo = QRadialGradient(head, mid, 13)
            halo.setColorAt(0.0, qcol(C.PRI, int(120 * glow)))
            halo.setColorAt(1.0, qcol(C.PRI, 0))
            p.setBrush(QBrush(halo))
            p.drawEllipse(QPointF(head, mid), 13, 13)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

        # Crans par-dessus le remplissage : ils donnent une échelle, donc une
        # idée de la durée restante. Dessinés dessous, la partie déjà lue
        # perdait sa graduation et la barre cessait d'être une règle.
        p.setPen(QPen(QColor(0, 212, 255, 40), 1))
        for x in range(0, int(W), 9):
            p.drawLine(QPointF(x, mid - 4), QPointF(x, mid + 4))

        if head > 1:
            p.setBrush(QBrush(qcol(C.WHITE)))
            p.setPen(QPen(qcol(C.PRI), 1))
            p.drawPath(Hud.bevel(QRectF(head - 3.5, mid - 5.5, 7, 11), 2.0))
        p.end()


class _Spectrum(QWidget):
    """Bandeau de barres animées, sous la pochette.

    Décor, pas mesure : ANO-GPT confie la lecture à un lecteur externe et ne
    reçoit aucun spectre audio. Les barres suivent des sinusoïdes de périodes
    différentes ; elles ne réagissent donc pas au son lui-même, seulement à
    l'état de lecture. C'est écrit ici pour que personne ne les prenne un jour
    pour une analyse réelle.
    """

    # Moins de barres et un dégradé unique : construire vingt-six dégradés par
    # image coûtait 2,6 ms, prises sur le GIL que se partagent Qt et l'audio.
    _BARS = 18

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(26)
        self._t = 0.0
        self._level = 0.0          # 0 à l'arrêt, 1 en lecture ; lissé
        # Le QSS global donne un fond à tout QWidget : sans cette règle, ce
        # widget peint un rectangle opaque qui perfore le panneau sous-jacent.
        self.setStyleSheet("background: transparent;")
        self._target = 0.0
        self._seeds = [0.6 + 1.9 * random.random() for _ in range(self._BARS)]
        self._brush = None          # dégradé construit une fois, à la taille
        self._brush_h = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(66)

    def set_playing(self, playing: bool) -> None:
        self._target = 1.0 if playing else 0.0

    def _tick(self):
        if not self.isVisible():
            return
        self._t += 0.16
        delta = self._target - self._level
        if abs(delta) < 0.01:
            if self._level != self._target:
                self._level = self._target
            elif self._level <= 0.0:
                return          # à l'arrêt, plus rien à redessiner
        else:
            self._level += delta * 0.12
        self.update()

    def paintEvent(self, event):
        if self._level <= 0.01:
            return
        p = QPainter(self)
        # Les barres sont des rectangles droits : l'anticrénelage ne change
        # rien à l'œil et double le coût.
        W, H = self.width(), self.height()
        gap = 3.0
        bar_w = max(1.5, (W - gap * (self._BARS - 1)) / self._BARS)

        # Un seul dégradé, sur toute la hauteur, réutilisé par chaque barre.
        # Chacune n'en montre que le haut, ce qui donne le même étagement de
        # couleur qu'un dégradé par barre — pour un vingtième du travail.
        if self._brush is None or self._brush_h != H:
            grad = QLinearGradient(0, 0, 0, H)
            grad.setColorAt(0.0, qcol(C.NEON_PINK, 210))
            grad.setColorAt(0.55, qcol(C.PRI, 190))
            grad.setColorAt(1.0, qcol(C.PRI, 45))
            self._brush = QBrush(grad)
            self._brush_h = H
        p.setOpacity(self._level)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._brush)

        for i in range(self._BARS):
            wave = 0.5 + 0.5 * math.sin(self._t * self._seeds[i] + i * 0.55)
            # Enveloppe : plus haut au centre, comme un spectre réel décroît
            # vers les aigus. Une rangée plate ressemble à un égaliseur mort.
            envelope = 0.45 + 0.55 * math.sin(math.pi * (i + 0.5) / self._BARS)
            height = max(2.0, wave * envelope * (H - 3) * self._level)
            p.drawRect(QRectF(i * (bar_w + gap), H - height, bar_w, height))
        p.end()


class _Marquee(QLabel):
    """Titre qui défile quand il dépasse, et seulement dans ce cas.

    Tronquer un titre par des points de suspension cache justement ce que
    l'utilisateur cherche : le nom du morceau en cours.
    """

    _GAP = 34.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self._offset = 0.0
        self._pause = 0
        self.setStyleSheet("background: transparent;")
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    def setText(self, text: str) -> None:     # noqa: N802 - imposé par QLabel
        super().setText(text)
        self._offset = 0.0
        self._pause = 28          # laisser lire le début avant de défiler
        self._sync()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync()

    def _sync(self):
        overflow = self._text_width() - self.width()
        if overflow > 4 and not self._timer.isActive():
            self._timer.start(50)
        elif overflow <= 4 and self._timer.isActive():
            self._timer.stop()
            self._offset = 0.0
            self.update()

    def _text_width(self) -> float:
        return QFontMetricsF(self.font()).horizontalAdvance(self.text())

    def _tick(self):
        if not self.isVisible():
            return
        if self._pause > 0:
            self._pause -= 1
            return
        self._offset += 0.7
        if self._offset > self._text_width() + self._GAP:
            self._offset = 0.0
            self._pause = 28
        self.update()

    def paintEvent(self, event):
        if not self._timer.isActive():
            super().paintEvent(event)
            return
        p = QPainter(self)
        p.setFont(self.font())
        p.setPen(QPen(qcol(C.WHITE), 1))
        y = self.height() / 2 + QFontMetricsF(self.font()).capHeight() / 2
        width = self._text_width()
        p.drawText(QPointF(-self._offset, y), self.text())
        # Seconde copie : la boucle se referme sans trou visible.
        p.drawText(QPointF(-self._offset + width + self._GAP, y), self.text())
        p.end()
