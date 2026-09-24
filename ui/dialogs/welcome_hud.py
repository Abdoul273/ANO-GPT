"""Accueil cinématique léger de Jarvis."""

from __future__ import annotations

import math
import time

from PyQt6.QtCore import QLineF, QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QFont, QPainter, QPen, QPixmap, QRadialGradient
from PyQt6.QtWidgets import QApplication, QVBoxLayout, QWidget

from ui.paths import _read_full_config
from ui.sound.hud_sound import get_hud_sound


def _ease(value: float) -> float:
    value = max(0.0, min(1.0, value))
    return value * value * (3 - 2 * value)


class WelcomeHudOverlay(QWidget):
    """Séquence : ignition, assemblage du noyau, révélation, ouverture."""

    dismissed = pyqtSignal()
    engaged = pyqtSignal()
    _BUILD_END = 4.8

    def __init__(self, parent: QWidget | None = None, assistant_name: str = "ANO-GPT"):
        super().__init__(parent)
        self._name = (assistant_name or "ANO-GPT").strip().upper()
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self._sound = get_hud_sound()
        self._timer = QTimer(self)
        self._timer.setInterval(40)  # 25 i/s, l'audio partage le GIL avec Qt.
        self._timer.timeout.connect(self._tick_frame)
        self._background: QPixmap | None = None
        self._elapsed = 0.0
        self._start_time = 0.0
        self._is_engaging = False
        self._engage_t = 0.0
        self._has_emitted = False
        self._sound_played: set[int] = set()
        self._btn_rect = QRectF()
        self._btn_hovered = False

    def showEvent(self, event):
        super().showEvent(event)
        self._start_time = time.monotonic()
        self._elapsed = 0.0
        self._is_engaging = False
        self._engage_t = 0.0
        self._has_emitted = False
        self._sound_played.clear()
        self._timer.start()
        self._sound.play_boot_surge()
        self.setFocus()

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def closeEvent(self, event):
        self._timer.stop()
        super().closeEvent(event)

    def resizeEvent(self, event):
        self._background = None
        super().resizeEvent(event)

    def _tick_frame(self):
        self._elapsed = time.monotonic() - self._start_time
        for index, start in enumerate((1.15, 2.75, 4.25)):
            if self._elapsed >= start and index not in self._sound_played:
                self._sound_played.add(index)
                self._sound.play_chirp(index + 1)
        if self._elapsed >= 10.0 and not self._is_engaging:
            self._engage()
        if self._is_engaging:
            self._engage_t = min(1.0, (time.monotonic() - self._engage_started_at) / .58)
            if self._engage_t >= 1.0 and not self._has_emitted:
                self._has_emitted = True
                self._timer.stop()
                self.hide()
                self.engaged.emit()
                self.dismissed.emit()
                return
        self.update()

    def _engage(self):
        if self._is_engaging:
            return
        self._is_engaging = True
        self._engage_started_at = time.monotonic()
        self._sound.play_access_granted()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Escape, Qt.Key.Key_Return, Qt.Key.Key_Space):
            self._engage()
        elif event.key() == Qt.Key.Key_M:
            self._sound.toggle_mute()
            self.update()
        else:
            super().keyPressEvent(event)

    def mouseMoveEvent(self, event):
        hovered = self._btn_rect.contains(event.position())
        if hovered != self._btn_hovered:
            self._btn_hovered = hovered
            self.setCursor(Qt.CursorShape.PointingHandCursor if hovered else Qt.CursorShape.ArrowCursor)
            if hovered:
                self._sound.play_hover()
            self.update()
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and (
            self._btn_rect.contains(event.position()) or self._elapsed >= 5.0
        ):
            self._engage()
        super().mousePressEvent(event)

    def _make_background(self, w: int, h: int) -> QPixmap:
        """Trame et halo cuits une fois, puis recopiés pendant l'animation."""
        pixmap = QPixmap(w, h)
        p = QPainter(pixmap)
        p.fillRect(0, 0, w, h, QColor(2, 7, 16))
        glow = QRadialGradient(QPointF(w * .5, h * .4), max(w, h) * .58)
        glow.setColorAt(0, QColor(9, 38, 60, 210))
        glow.setColorAt(.44, QColor(5, 18, 32, 140))
        glow.setColorAt(1, QColor(2, 7, 16, 0))
        p.fillRect(0, 0, w, h, QBrush(glow))
        p.setPen(QPen(QColor(78, 181, 216, 22), 1))
        for x in range(0, w, 64):
            p.drawLine(x, 0, x, h)
        for y in range(0, h, 64):
            p.drawLine(0, y, w, y)
        p.setPen(QPen(QColor(65, 168, 209, 52), 1))
        gutter = max(24, int(w * .045))
        p.drawLine(gutter, 64, w - gutter, 64)
        p.drawLine(gutter, h - 45, w - gutter, h - 45)
        p.end()
        return pixmap

    def paintEvent(self, _event):
        w, h = self.width(), self.height()
        if w < 1 or h < 1:
            return
        if self._background is None or self._background.size() != self.size():
            self._background = self._make_background(w, h)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.drawPixmap(0, 0, self._background)
        t = self._elapsed
        cx, cy = w * .5, h * .4
        radius = min(w * .22, h * .215, 190.0)
        self._draw_header(p, w, h, t)
        self._draw_reactor(p, cx, cy, radius, t)
        self._draw_title(p, w, cx, cy + radius, t)
        self._draw_progress(p, w, h, t)
        if t >= self._BUILD_END:
            self._draw_button(p, w, h, t)
        else:
            self._btn_rect = QRectF()
        if self._is_engaging:
            progress = _ease(self._engage_t)
            p.setPen(QPen(QColor(143, 231, 252, int(190 * (1 - progress))), 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(cx, cy), max(w, h) * progress, max(w, h) * progress)
            p.fillRect(0, 0, w, h, QColor(2, 7, 16, int(255 * progress)))
        p.end()

    def _draw_header(self, p: QPainter, w: int, h: int, t: float):
        gutter = max(24, int(w * .045))
        p.setFont(QFont("JetBrains Mono", 9, QFont.Weight.DemiBold))
        p.setPen(QColor(140, 223, 243, 210))
        p.drawText(gutter, 42, "A.N.O  /  INITIALISATION")
        if w >= 850:
            p.setPen(QColor(73, 147, 171, 180))
            p.drawText(QRectF(w - gutter - 170, 27, 170, 22),
                       Qt.AlignmentFlag.AlignRight, "NEURAL INTERFACE  01")
        if w >= 900 and h >= 650:
            stages = ((1.0, "01", "IGNITION"), (2.35, "02", "ASSEMBLAGE"),
                      (3.7, "03", "INTERFACE PRÊTE"))
            x, y = gutter, h * .31
            for start, number, label in stages:
                active = t >= start
                p.setPen(QPen(QColor(47, 166, 205, 130 if active else 44), 1))
                p.drawLine(QPointF(x, y + 8), QPointF(x + 21, y + 8))
                p.setPen(QColor(140, 224, 244, 220) if active else QColor(74, 124, 145, 100))
                p.drawText(QPointF(x + 32, y + 12), f"{number}  /  {label}")
                y += 34

    def _draw_reactor(self, p: QPainter, cx: float, cy: float, r: float, t: float):
        ignition = _ease((t - .1) / .65)
        assembly = _ease((t - .85) / 2.0)
        if ignition <= 0:
            return
        p.save()
        p.setOpacity(ignition)
        glow = QRadialGradient(QPointF(cx, cy), r * 1.22)
        glow.setColorAt(0, QColor(68, 198, 243, 90))
        glow.setColorAt(.36, QColor(17, 105, 162, 46))
        glow.setColorAt(1, QColor(0, 95, 165, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(glow))
        p.drawEllipse(QPointF(cx, cy), r * 1.22, r * 1.22)
        p.setBrush(Qt.BrushStyle.NoBrush)
        for factor, alpha in ((.34, 74), (.55, 55), (.72, 58), (.93, 105), (1.10, 48)):
            p.setPen(QPen(QColor(84, 192, 224, int(alpha * assembly)), 1))
            p.drawEllipse(QPointF(cx, cy), r * factor * assembly, r * factor * assembly)
        for factor, start, span, speed, alpha in (
            (.93, 20, 72, 12, 190), (.93, 200, 72, 12, 190),
            (1.10, 104, 46, -8, 110), (1.10, 284, 46, -8, 110),
            (.72, 44, 100, -17, 125), (.72, 224, 100, -17, 125),
        ):
            rr = r * factor * assembly
            if rr < 2:
                continue
            p.setPen(QPen(QColor(125, 224, 247, int(alpha * assembly)), 1))
            rect = QRectF(cx - rr, cy - rr, rr * 2, rr * 2)
            p.drawArc(rect, int(((start + t * speed) % 360) * 16), span * 16)
        # Segments du stator : une cadence douce et des intervalles francs
        # donnent une silhouette mécanique, même quand l'animation s'arrête.
        rr = r * .82 * assembly
        rect = QRectF(cx - rr, cy - rr, rr * 2, rr * 2)
        for index in range(12):
            p.setPen(QPen(QColor(96, 213, 248, 145 if index % 3 else 225), 2))
            p.drawArc(rect, int(((index * 30 + 4 - t * 2) % 360) * 16), 21 * 16)
        for factor, alpha in ((.44, 82), (.61, 125)):
            rr = r * factor * assembly
            rect = QRectF(cx - rr, cy - rr, rr * 2, rr * 2)
            p.setPen(QPen(QColor(144, 233, 253, alpha), 1))
            p.drawArc(rect, int(((t * 11 + 18) % 360) * 16), 68 * 16)
            p.drawArc(rect, int(((t * 11 + 198) % 360) * 16), 68 * 16)
        ticks = []
        for index in range(48):
            angle = index * math.tau / 48 + t * .025
            ca, sa = math.cos(angle), math.sin(angle)
            inner = r * (1.005 if index % 4 else .98) * assembly
            outer = r * 1.035 * assembly
            ticks.append(QLineF(cx + ca * inner, cy + sa * inner,
                                cx + ca * outer, cy + sa * outer))
        p.setPen(QPen(QColor(126, 211, 239, int(105 * assembly)), 1))
        p.drawLines(ticks)
        core = QRadialGradient(QPointF(cx, cy), r * .25)
        core.setColorAt(0, QColor(255, 255, 255, 244))
        core.setColorAt(.12, QColor(159, 239, 255, 210))
        core.setColorAt(.44, QColor(31, 164, 229, 82))
        core.setColorAt(1, QColor(10, 101, 185, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(core))
        p.drawEllipse(QPointF(cx, cy), r * .25, r * .25)
        p.setBrush(QColor(236, 253, 255))
        p.drawEllipse(QPointF(cx, cy), max(2.5, r * .024), max(2.5, r * .024))
        p.restore()

    def _draw_title(self, p: QPainter, w: int, cx: float, bottom: float, t: float):
        reveal = _ease((t - 2.45) / 1.1)
        if reveal <= 0:
            return
        title_y = bottom + 57
        size = max(22, min(40, int(w * .035)))
        font = QFont("Inter", size, QFont.Weight.Black)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 3)
        p.setFont(font)
        while size > 18 and p.fontMetrics().horizontalAdvance(self._name) > w - 48:
            size -= 1
            font.setPointSize(size)
            p.setFont(font)
        p.setOpacity(reveal)
        p.setPen(QColor(237, 250, 255))
        p.drawText(QRectF(16, title_y - 35, w - 32, 48), Qt.AlignmentFlag.AlignCenter, self._name)
        if t >= self._BUILD_END:
            p.setFont(QFont("JetBrains Mono", 9, QFont.Weight.DemiBold))
            p.setPen(QColor(112, 209, 238))
            p.drawText(QRectF(12, title_y + 16, w - 24, 22),
                       Qt.AlignmentFlag.AlignCenter, "VOTRE INTERFACE EST PRÊTE")
        p.setOpacity(1)

    def _draw_progress(self, p: QPainter, w: int, h: int, t: float):
        width = min(400.0, w * .62)
        x, y = (w - width) / 2, h - 114
        progress = _ease(t / self._BUILD_END)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(33, 76, 96, 125))
        p.drawRect(QRectF(x, y, width, 2))
        p.setBrush(QColor(122, 226, 249))
        p.drawRect(QRectF(x, y, width * progress, 2))
        p.setFont(QFont("JetBrains Mono", 8, QFont.Weight.DemiBold))
        p.setPen(QColor(111, 181, 205))
        label = "INTERFACE PRÊTE" if progress >= 1 else "INITIALISATION VISUELLE"
        p.drawText(QRectF(x, y - 22, width, 17), Qt.AlignmentFlag.AlignLeft, label)
        p.drawText(QRectF(x, y - 22, width, 17), Qt.AlignmentFlag.AlignRight,
                   f"{round(progress * 100):02d}%")

    def _draw_button(self, p: QPainter, w: int, h: int, t: float):
        width = min(280.0, w * .56)
        self._btn_rect = QRectF((w - width) / 2, h - 88, width, 48)
        pulse = .5 + .5 * math.sin(t * 2.8)
        fill = QColor(18, 104, 143, 180 if self._btn_hovered else int(95 + 26 * pulse))
        border = QColor(160, 240, 255, 245 if self._btn_hovered else 175)
        p.setPen(QPen(border, 1))
        p.setBrush(fill)
        p.drawRoundedRect(self._btn_rect, 6, 6)
        p.setFont(QFont("Inter", 10, QFont.Weight.Bold))
        p.setPen(QColor(239, 252, 255))
        p.drawText(self._btn_rect, Qt.AlignmentFlag.AlignCenter, "OUVRIR JARVIS")


def preview_welcome_screen():
    import sys

    app = QApplication.instance() or QApplication(sys.argv)
    cfg = _read_full_config()
    name = cfg.get("assistant_name", "ANO-GPT") or "ANO-GPT"
    win = QWidget()
    win.setWindowTitle(f"{name.upper()} — HUD")
    win.resize(1280, 800)
    layout = QVBoxLayout(win)
    layout.setContentsMargins(0, 0, 0, 0)
    welcome = WelcomeHudOverlay(win, assistant_name=name)
    layout.addWidget(welcome)
    welcome.dismissed.connect(win.close)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    preview_welcome_screen()
