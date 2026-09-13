from __future__ import annotations

import math
import random


from PyQt6.QtCore import (
    QEasingCurve, QPointF, QRect, QRectF, Qt,
    QTimer, pyqtSignal, QPropertyAnimation,
)
from PyQt6.QtGui import (
    QFont, QPainter,
)
from PyQt6.QtWidgets import (
    QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QLayout, QVBoxLayout, QWidget,
)

from ui.core.hud_button import HudButton
from ui.core.hud_paint import Hud
from ui.styles.cyber import cyber_hairline
from ui.styles.theme import C, qcol

class FloatingPanel(QFrame):
    """Panneau flottant translucide style HUD JARVIS.
    FOND : glass sombre rgba(1, 13, 20, 0.88), BORDURE : hairline accent 1px,
    TITRE : ◈ avec capitales fins, BOUTON FERMER discret.
    Déplaçable à la souris sur la barre de titre.
    """
    closed = pyqtSignal()

    def __init__(self, title: str = "", closeable: bool = False, parent=None):
        super().__init__(parent)
        self.setObjectName("FloatingPanel")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._dragging = False
        self._manual_position = None
        self._drag_start = QPointF()
        self._pulse = random.random() * math.tau
        self._scan = 0.0

        self._opacity = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._opacity)
        self._opacity.setOpacity(1.0)

        self._anim = QPropertyAnimation(self._opacity, b"opacity")
        self._anim.setDuration(200)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._hide_when_animation_finishes = False
        self._fade_generation = 0
        self._anim.finished.connect(self._on_opacity_animation_finished)

        self._fx_timer = QTimer(self)
        self._fx_timer.timeout.connect(self._tick_fx)
        self._fx_timer.start(250)

        self.setStyleSheet("""
            QFrame#FloatingPanel {
                background: transparent;
                border: none;
                border-radius: 14px;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(8)

        if title or closeable:
            self._hdr = QWidget(self)
            self._hdr.setCursor(Qt.CursorShape.SizeAllCursor)
            hdr_lay = QHBoxLayout(self._hdr)
            hdr_lay.setContentsMargins(4, 2, 4, 2)
            hdr_lay.setSpacing(6)

            dot = QLabel("◈")
            dot.setFont(QFont("Inter", 8, QFont.Weight.Bold))
            dot.setStyleSheet(f"color: {C.PRI}; background: transparent; border: none;")
            hdr_lay.addWidget(dot)

            self._title_lbl = QLabel(title.upper())
            self._title_lbl.setFont(QFont("Inter", 9, QFont.Weight.DemiBold))
            self._title_lbl.setStyleSheet(f"color: {C.WHITE}; background: transparent; border: none; letter-spacing: 1px;")
            hdr_lay.addWidget(self._title_lbl, stretch=1)

            if closeable:
                self._close_btn = HudButton(
                    icon="x", accent=C.TEXT_DIM, hover_accent=C.RED, size=11)
                self._close_btn.setFixedSize(20, 18)
                self._close_btn.clicked.connect(self._on_close)
                hdr_lay.addWidget(self._close_btn)

            layout.addWidget(self._hdr)
            layout.addWidget(cyber_hairline(self))

        self._container = QWidget(self)
        self._container_layout = QVBoxLayout(self._container)
        self._container_layout.setContentsMargins(0, 0, 0, 0)
        self._container_layout.setSpacing(4)
        layout.addWidget(self._container, stretch=1)

    def add_widget(self, widget: QWidget, stretch: int = 0):
        self._container_layout.addWidget(widget, stretch=stretch)

    def add_layout(self, lay: QLayout, stretch: int = 0):
        self._container_layout.addLayout(lay, stretch=stretch)

    def fade_in(self):
        self._fade_generation += 1
        self._hide_when_animation_finishes = False
        self.show()
        self.raise_()
        self._anim.stop()
        self._anim.setStartValue(self._opacity.opacity())
        self._anim.setEndValue(1.0)
        self._anim.start()

    def fade_out(self):
        self._fade_generation += 1
        generation = self._fade_generation
        self._hide_when_animation_finishes = True
        self._anim.stop()
        self._anim.setStartValue(self._opacity.opacity())
        self._anim.setEndValue(0.0)
        self._anim.start()
        # Certaines plateformes Qt ne livrent pas `finished` sur un effet
        # d'opacité d'un widget top-level sans parent. Le garde de génération
        # empêche ce repli de cacher un panneau qui aurait été réaffiché entre-
        # temps.
        QTimer.singleShot(
            self._anim.duration() + 20,
            lambda: self._finish_fade_out(generation),
        )

    def _on_opacity_animation_finished(self):
        if self._hide_when_animation_finishes:
            self.hide()

    def _finish_fade_out(self, generation: int):
        if generation == self._fade_generation and self._hide_when_animation_finishes:
            self.hide()

    def _on_close(self):
        self.fade_out()
        self.closed.emit()

    def setGeometry(self, *args):
        """Garde la position choisie à la souris lors des mises à jour du HUD."""
        rect = QRect(args[0]) if len(args) == 1 else QRect(*args)
        if self._manual_position is not None and self.parentWidget() is not None:
            parent = self.parentWidget()
            pos = self._manual_position
            rect.moveTo(
                max(0, min(pos.x(), parent.width() - rect.width())),
                max(0, min(pos.y(), parent.height() - rect.height())),
            )
        super().setGeometry(rect)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._drag_start = event.globalPosition() - QPointF(self.pos())
            event.accept()

    def mouseMoveEvent(self, event):
        if self._dragging and (event.buttons() & Qt.MouseButton.LeftButton):
            new_pos = (event.globalPosition() - self._drag_start).toPoint()
            if self.parentWidget():
                pw = self.parentWidget().width()
                ph = self.parentWidget().height()
                new_pos.setX(max(0, min(new_pos.x(), pw - self.width())))
                new_pos.setY(max(0, min(new_pos.y(), ph - self.height())))
            self._manual_position = new_pos
            self.move(new_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._dragging = False
        event.accept()

    def _tick_fx(self):
        # Un panneau caché n'a rien à animer : sans ce garde, le lecteur
        # musique repeignait son décor même refermé, et cette machine à deux
        # cœurs paie chaque image sur le GIL que Qt partage avec l'audio.
        if not self.isVisible():
            return
        self._pulse = (self._pulse + 0.07) % math.tau
        # Un balayage lent : à deux secondes il donne l'impression d'un
        # appareil qui s'affole ; à dix, celle d'un appareil qui veille.
        self._scan = (self._scan + 1.3) % Hud.SCAN_PERIOD
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        if W < 8 or H < 8:
            p.end()
            return

        rect = QRectF(3, 3, W - 6, H - 6)
        Hud.chassis(p, rect, accent=qcol(C.PRI), scan=self._scan,
                    pulse=self._pulse)
        Hud.tick(p, rect)
        p.end()
        super().paintEvent(event)
