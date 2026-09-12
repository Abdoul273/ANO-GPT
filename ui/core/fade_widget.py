from __future__ import annotations

import math
import random

from PyQt6.QtCore import QEasingCurve, QPropertyAnimation, QTimer, QRectF, Qt
from PyQt6.QtGui import QPainter
from PyQt6.QtWidgets import QGraphicsOpacityEffect, QWidget

from ui.core.hud_paint import Hud
from ui.styles.cyber import apply_cyber_style
from ui.styles.theme import C, qcol

class FadeInWidget(QWidget):
    """Panneau modal en surimpression : voile d'assombrissement + fondu.

    L'ancienne version animait `windowOpacity`, qui n'a aucun effet sur un
    widget enfant — seules les fenêtres de premier niveau la respectent. On
    passe donc par un QGraphicsOpacityEffect, qui fonctionne partout.
    """

    _SCRIM_ALPHA = 165

    def __init__(self, parent=None, duration: int = 250):
        super().__init__(parent)
        self._closing = False

        # Le châssis HUD est peint par `paintEvent` ; le panneau lui-même reste
        # donc transparent, et tous ses enfants héritent de la feuille commune.
        apply_cyber_style(self)

        # Voile : capte les clics et détache visuellement le panneau du fond
        self._scrim: QWidget | None = None
        if parent is not None:
            self._scrim = QWidget(parent)
            self._scrim.setStyleSheet(
                f"background: rgba(0, 6, 10, {self._SCRIM_ALPHA});")
            self._scrim.hide()

        self._fx = QGraphicsOpacityEffect(self)
        self._fx.setOpacity(0.0)
        self.setGraphicsEffect(self._fx)
        self._anim = QPropertyAnimation(self._fx, b"opacity", self)
        self._anim.setDuration(duration)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        # Toutes les fenêtres ouvertes depuis Paramètres partagent ce châssis
        # HUD : même langage visuel que les cartes, sans animation coûteuse.
        self._cyber_scan = random.random() * Hud.SCAN_PERIOD
        self._cyber_pulse = random.random() * math.tau
        self._cyber_timer = QTimer(self)
        self._cyber_timer.setInterval(125)
        self._cyber_timer.timeout.connect(self._tick_cyber_chassis)

    def _tick_cyber_chassis(self) -> None:
        if not self.isVisible():
            return
        self._cyber_scan = (self._cyber_scan + 1.2) % Hud.SCAN_PERIOD
        self._cyber_pulse = (self._cyber_pulse + 0.055) % math.tau
        self.update()

    def showEvent(self, event):
        super().showEvent(event)
        if self._scrim is not None:
            self._scrim.setGeometry(self.parentWidget().rect())
            self._scrim.show()
            self._scrim.raise_()
        self.raise_()
        self._closing = False
        self._cyber_timer.start()
        self._anim.stop()
        self._anim.setStartValue(self._fx.opacity())
        self._anim.setEndValue(1.0)
        self._anim.start()

    def hide(self):
        """Fondu sortant, puis masquage réel (et retrait du voile)."""
        if self._closing or not self.isVisible():
            super().hide()
            self._drop_scrim()
            self._cyber_timer.stop()
            return
        self._closing = True
        self._anim.stop()
        self._anim.setStartValue(self._fx.opacity())
        self._anim.setEndValue(0.0)
        self._anim.finished.connect(self._really_hide)
        self._anim.start()

    def _really_hide(self):
        try:
            self._anim.finished.disconnect(self._really_hide)
        except TypeError:
            pass
        self._closing = False
        self._drop_scrim()
        self._cyber_timer.stop()
        super().hide()

    def _drop_scrim(self):
        if self._scrim is not None:
            self._scrim.hide()

    def keyPressEvent(self, event):  # noqa: N802
        # Filet de sécurité : si la croix se retrouve hors du cadre parce que le
        # contenu déborde, Échap ferme quand même le réglage.
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            event.accept()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event):  # noqa: N802
        # Le fond propre à chaque réglage est conservé, puis le châssis commun
        # est peint par-dessus. Cela évite sept styles divergents de dialogues.
        super().paintEvent(event)
        if self.width() < 12 or self.height() < 12:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        Hud.chassis(
            painter, QRectF(3, 3, self.width() - 6, self.height() - 6),
            accent=qcol(C.PRI), scan=self._cyber_scan, pulse=self._cyber_pulse,
        )
        Hud.tick(painter, QRectF(3, 3, self.width() - 6, self.height() - 6), 46)
        painter.end()
