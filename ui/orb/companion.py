"""One native alpha surface: no child orb, styled container or nested backing store."""
from __future__ import annotations

import math
import time

from PyQt6.QtCore import QRect, QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen, QRegion
from PyQt6.QtWidgets import QApplication, QWidget

from ui.orb.mini_orb import paint_reactor
from ui.styles.theme import C


class CompanionOrb(QWidget):
    WINDOW_TITLE = 'ANO Orb'
    ORB = 88
    GAP = 10
    BUBBLE_W = 250
    BUBBLE_MAX_H = 120
    BUBBLE_MS = 6500
    FULL_W = ORB + GAP + BUBBLE_W
    FULL_H = max(ORB, BUBBLE_MAX_H)

    def __init__(self, source, *, on_click=None, on_restore=None):
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setWindowTitle(self.WINDOW_TITLE)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAutoFillBackground(False)
        # No stylesheet on this surface: QStyle must never paint its background.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip('ANO-GPT · clic : micro · double-clic : ouvrir la fenêtre')
        self._source = source
        self._started = time.monotonic()
        self._on_click = on_click
        self._on_restore = on_restore
        self._press_at = None
        self._message = ''
        self._bubble_on = False
        self._font = QFont('DejaVu Sans', 9)
        self._orb_rect = QRect(self.FULL_W - self.ORB, (self.FULL_H - self.ORB) // 2,
                               self.ORB, self.ORB)
        self._bubble_rect = QRect()
        self.setFixedSize(self.FULL_W, self.FULL_H)
        self._timer = QTimer(self)
        # La bulle est décorative : 4 FPS est largement suffisant et évite
        # de voler le GIL au micro lorsque le grand HUD est masqué.
        self._timer.setInterval(250)
        self._timer.timeout.connect(self.update)
        self._bubble_tmr = QTimer(self)
        self._bubble_tmr.setSingleShot(True)
        self._bubble_tmr.timeout.connect(self._clear_bubble)
        self._direct_vol = 0.0
        self._relayout()

    def set_volume(self, level: float) -> None:
        """Injecte directement le volume audio."""
        try:
            val = max(0.0, min(1.0, float(level)))
            self._direct_vol = val
            if self._source is not None:
                setattr(self._source, '_direct_vol', val)
                if hasattr(self._source, "set_volume"):
                    self._source.set_volume(val)
        except Exception:
            pass

    def _relayout(self):
        region = QRegion(self._orb_rect, QRegion.RegionType.Ellipse)
        if self._bubble_on:
            metrics = QFontMetrics(self._font)
            text_rect = metrics.boundingRect(QRect(0, 0, self.BUBBLE_W - 28, 1000),
                                             Qt.TextFlag.TextWordWrap, self._message)
            height = min(self.BUBBLE_MAX_H, max(42, text_rect.height() + 24))
            self._bubble_rect = QRect(0, (self.FULL_H - height) // 2, self.BUBBLE_W, height)
            region |= QRegion(self._bubble_rect)
        else:
            self._bubble_rect = QRect()
        self._input_region = region
        if self.isVisible():
            self._apply_input_region()
        self.update()

    def _apply_input_region(self):
        if QApplication.platformName() == 'xcb':
            # QWidget.setMask also changes XShapeBounding. On XWayland that
            # produces an opaque black rectangle despite an intact alpha buffer.
            # Limit clicks only; the compositor keeps the complete ARGB surface.
            from ui.orb.input_shape import set_x11_input_shape
            scale = self.devicePixelRatioF()
            r = self._orb_rect
            left, top = round(r.x() * scale), round(r.y() * scale)
            size = round(r.width() * scale)
            rectangles = []
            for row in range(size):
                dy = (row + .5 - size / 2) / (size / 2)
                half = math.sqrt(max(0., 1 - dy * dy)) * size / 2
                x1, x2 = math.ceil(size / 2 - half), math.floor(size / 2 + half)
                if x2 > x1:
                    rectangles.append((left + x1, top + row, x2 - x1, 1))
            if self._bubble_on:
                b = self._bubble_rect
                rectangles.append(tuple(round(v * scale) for v in (b.x(), b.y(), b.width(), b.height())))
            set_x11_input_shape(int(self.winId()), rectangles)
        elif QApplication.platformName().startswith('wayland'):
            self.windowHandle().setMask(self._input_region)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        p.fillRect(event.rect(), Qt.GlobalColor.transparent)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        if self._source is not None and self._direct_vol > 0.0:
            setattr(self._source, '_direct_vol', self._direct_vol)
        paint_reactor(p, QRectF(self._orb_rect), self._source, time.monotonic() - self._started)
        if self._bubble_on:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setPen(QPen(QColor(0, 212, 255, 110), 1))
            p.setBrush(QColor(6, 11, 24, 225))
            p.drawRoundedRect(QRectF(self._bubble_rect).adjusted(1, 1, -1, -1), 12, 12)
            p.setFont(self._font)
            p.setPen(QColor(C.TEXT))
            p.drawText(self._bubble_rect.adjusted(14, 10, -14, -10),
                       Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignVCenter, self._message)
        p.end()

    def say(self, text):
        message = ' '.join((text or '').split())
        if not message:
            return
        self._message = message if len(message) <= 180 else message[:177].rstrip() + '…'
        self._bubble_on = True
        self._relayout()
        self._bubble_tmr.start(self.BUBBLE_MS)

    def _clear_bubble(self):
        self._bubble_tmr.stop()
        self._message = ''
        self._bubble_on = False
        self._relayout()

    def showEvent(self, event):
        # Style polishing can re-enable this flag when the global QSS changes.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self._timer.start()
        super().showEvent(event)
        self._apply_input_region()

    def hideEvent(self, event):
        self._timer.stop()
        self._clear_bubble()
        super().hideEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_at = event.globalPosition().toPoint()

    def mouseReleaseEvent(self, event):
        start, self._press_at = self._press_at, None
        if start is not None and event.button() == Qt.MouseButton.LeftButton:
            if (event.globalPosition().toPoint() - start).manhattanLength() <= 6 and self._on_click:
                self._on_click()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._on_restore:
            self._on_restore()
