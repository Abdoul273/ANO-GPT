"""Small, transparent raster reactor shared by the HUD and desktop companion."""
from __future__ import annotations

import math
import time

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen, QRadialGradient
from PyQt6.QtWidgets import QWidget


def _alpha(color: QColor, opacity: int) -> QColor:
    result = QColor(color)
    result.setAlpha(opacity)
    return result


def paint_reactor(p: QPainter, bounds: QRectF, source, elapsed: float) -> None:
    """Paint photons only; no background, native window or GPU context."""
    if min(bounds.width(), bounds.height()) < 2:
        return
    palettes = getattr(source, '_PALETTES', {})
    state = getattr(source, '_ws', 'idle')
    palette = palettes.get(state, palettes.get('idle', {}))
    core = QColor(palette.get('core', QColor('#78e1ff')))
    wire = QColor(palette.get('wire', QColor('#00beff')))
    hot = QColor(palette.get('hot', QColor('#e1faff')))
    accent = QColor('#ae65ff') if state == 'idle' else QColor(palette.get('halo', wire))

    # Read audio volume from any available attribute (live volume, target, or direct feed)
    src_vol = float(getattr(source, '_volume', 0.0))
    target_vol = float(getattr(source, '_target_vol', 0.0))
    direct_vol = float(getattr(source, '_direct_vol', 0.0))
    raw_vol = max(0.0, min(1.0, max(src_vol, target_vol, direct_vol)))

    # Perceptual acoustic power curve: lifts quiet-to-moderate speech into prominent visual range
    vol_boost = math.pow(raw_vol, 0.55) if raw_vol > 0.001 else 0.0

    energy = max(.12, min(1., float(getattr(source, '_energy', .5))))
    spin = float(palette.get('spin', 1.))
    t = elapsed

    # Organic technological heartbeat (battement systolique)
    # Accelerates during speech, voice activity or active states
    is_active = (state in ('listening', 'speaking', 'acting', 'thinking')) or raw_vol > 0.02
    beat_hz = 1.20 + (2.10 * vol_boost if is_active else 0.0)
    beat_phase = (t * beat_hz) % 1.0

    # Systolic surge: sharp rising contraction followed by exponential relaxation & dicrotic rebound
    p1 = math.exp(-beat_phase * 4.8) * math.sin(beat_phase * math.pi)
    p2 = (math.exp(-max(0.0, beat_phase - 0.22) * 7.5)
          * math.sin(max(0.0, beat_phase - 0.22) * math.pi * 1.6)) if beat_phase > 0.22 else 0.0
    raw_beat = max(0.0, p1 * 1.05 + p2 * 0.35)

    # Beat intensity: gentle resting rhythm at idle, surging strongly with voice amplitude
    beat = raw_beat * (0.30 + 0.70 * vol_boost)

    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
    p.translate(bounds.center())
    scale = min(bounds.width(), bounds.height()) / 98.
    p.scale(scale, scale)

    def glow(radius, color, intensity):
        gradient = QRadialGradient(QPointF(0, 0), radius)
        gradient.setColorAt(0, _alpha(color, intensity))
        gradient.setColorAt(.45, _alpha(color, int(intensity * .42)))
        gradient.setColorAt(1, _alpha(color, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(gradient)
        p.drawEllipse(QPointF(0, 0), radius, radius)

    def arc(radius, start, span, color, width=1.):
        rect = QRectF(-radius, -radius, 2 * radius, 2 * radius)
        p.setBrush(Qt.BrushStyle.NoBrush)
        for thickness, opacity in ((width + 3.5, 30), (width + 1.5, 75), (width, 240)):
            pen = QPen(_alpha(color, opacity), thickness)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.drawArc(rect, int(start * 16), int(span * 16))

    # Outer aura (strictly bounded inside radius 44 so exterior margins remain 100% transparent)
    glow(44, wire, min(255, 55 + int(energy * 20) + int(vol_boost * 35) + int(beat * 20)))

    # Broken orbital tracks: accelerate and stretch when user speaks
    orb_spin = spin * (1.0 + 1.8 * vol_boost + 0.4 * beat)
    for radius, velocity, phase, color in ((43, 13, 15, wire), (39, -19, 155, accent)):
        for offset, length in ((0, 84), (180, 45)):
            dyn_len = length + int(vol_boost * 20 + beat * 10)
            arc(radius, t * velocity * orb_spin + phase + offset, dyn_len, color, 1.8 + 0.3 * beat)

    # Acoustic shockwave rings (ondes de choc acoustiques de battement)
    if is_active or vol_boost > 0.02:
        for wave_idx, wave_offset in enumerate((0.0, 0.5)):
            w_phase = (t * (2.0 + 1.4 * vol_boost) + wave_offset) % 1.0
            w_r = 13.0 + w_phase * 18.0  # Dilates from r=13 to r=31 max
            w_fade = math.sin(w_phase * math.pi)
            w_alpha = int(w_fade * (35 + 130 * vol_boost) * (0.5 + 0.5 * beat))
            if w_alpha > 8:
                w_pen = QPen(_alpha(hot if wave_idx == 0 else wire, w_alpha), 1.2 + 0.5 * vol_boost)
                w_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                p.setPen(w_pen)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(QPointF(0, 0), w_r, w_r)

    # Fine telemetry ticks: dance like a dynamic radial audio equalizer
    p.setBrush(Qt.BrushStyle.NoBrush)
    tick_speed = 8.0 + 12.0 * vol_boost
    for i in range(48):
        a = math.tau * i / 48
        major = i % 4 == 0
        r = 34.5
        tick_noise = math.sin(i * 2.3 + t * tick_speed)
        pulse = (0.5 + 0.5 * tick_noise) ** 2
        eq_jump = vol_boost * (3.5 + 7.0 * pulse) + beat * 2.6
        length = (3.2 if major else 1.5) + eq_jump
        tick_alpha = min(255, (210 if major else 120) + int(energy * 45) + int(vol_boost * 45) + int(beat * 35))
        p.setPen(QPen(_alpha(core, tick_alpha), 1.5 if (major or vol_boost > 0.3) else 1.2))
        p.drawLine(QPointF(math.cos(a) * r, math.sin(a) * r),
                   QPointF(math.cos(a) * (r - length), math.sin(a) * (r - length)))

    # Luminous spherical cage: dilates with heartbeat and voice volume
    p.save()
    p.rotate(-18 + 7 * math.sin(t * .35))
    radius = min(30.8, 24.0 + vol_boost * 4.6 + beat * 2.2)
    p.setPen(QPen(_alpha(wire, min(255, 200 + int(vol_boost * 50) + int(beat * 35))), 1.5 + 0.3 * beat))
    p.drawEllipse(QPointF(0, 0), radius, radius)
    for i in range(4):
        phase = t * .36 * spin + i * math.pi / 4
        width = max(.8, abs(math.cos(phase)) * radius)
        long_alpha = min(255, int(100 + 120 * abs(math.sin(phase)) + 35 * beat + 45 * vol_boost))
        p.setPen(QPen(_alpha(core, long_alpha), 1.2 + 0.3 * beat))
        p.drawEllipse(QRectF(-width, -radius, 2 * width, 2 * radius))
    for lat in (-.5, 0., .5):
        y = radius * lat
        width = radius * math.sqrt(1 - lat * lat)
        lat_alpha = min(255, int(130 + 70 * vol_boost + 30 * beat))
        p.setPen(QPen(_alpha(wire, lat_alpha), 1.1 + 0.3 * vol_boost))
        p.drawEllipse(QRectF(-width, y - 3.5, 2 * width, 7))
    p.restore()

    # Two asymmetric orbital satellites with an extended glowing tail
    for phase, color in ((0, hot), (math.pi, accent)):
        angle = t * .65 * orb_spin + phase
        trail_dots = 7 + int(vol_boost * 3)
        for j in range(trail_dots):
            a = angle - j * .035
            dot = QPointF(43 * math.cos(a), -43 * math.sin(a))
            p.setPen(Qt.PenStyle.NoPen)
            sat_alpha = max(30, min(255, 255 - j * 30 + int(vol_boost * 30)))
            p.setBrush(_alpha(color, sat_alpha))
            dot_r = max(0.8, 2.0 - j * .15 + beat * 0.35)
            p.drawEllipse(dot, dot_r, dot_r)

    # Plasma ribbon: systolic breath + voice turbulence
    breath = 1.0 + (0.055 + 0.09 * beat) * math.sin(t * (2.2 + 2.8 * vol_boost))
    core_dil = vol_boost * 5.6 + beat * 2.6
    points = []
    for i in range(97):
        a = math.tau * i / 96
        turb = (math.sin(a * 3 + t * (1.8 + 2.4 * vol_boost)) * 2.2
                + math.cos(a * 5 - t * (1.0 + 1.4 * vol_boost)) * 1.3)
        r_plasma = (12.0 + turb) * breath + core_dil
        points.append(QPointF(r_plasma * math.cos(a), r_plasma * math.sin(a)))
    path = QPainterPath(points[0])
    for point in points[1:]:
        path.lineTo(point)
    path.closeSubpath()

    p.setBrush(_alpha(core, 15 + int(vol_boost * 20)))
    for width, opacity in ((5.5 + beat * 1.2, 35 + int(vol_boost * 25)),
                           (2.8 + beat * 0.8, 85 + int(vol_boost * 40)),
                           (1.5, 255)):
        p.setPen(QPen(_alpha(hot, opacity), width))
        p.drawPath(path)

    # Deep glowing singularity with heartbeat pulse
    glow(20.0 + vol_boost * 8.0 + beat * 3.5, core, min(255, 175 + int(energy * 80) + int(vol_boost * 40) + int(beat * 30)))
    glow(7.0 + vol_boost * 4.5 + beat * 2.5, hot, 255)
    p.setBrush(_alpha(hot, 255))
    p.setPen(Qt.PenStyle.NoPen)
    core_dot = 2.8 + vol_boost * 2.4 + beat * 1.5
    p.drawEllipse(QPointF(0, 0), core_dot, core_dot)
    p.restore()


class MiniOrbOverlay(QWidget):
    """Embedded transparent view; the desktop window paints the reactor directly."""

    def __init__(self, source, parent=None):
        super().__init__(parent)
        self._source = source
        self._direct_vol = 0.0
        self._started = time.monotonic()
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, parent is None)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAutoFillBackground(False)
        self.setStyleSheet('background: transparent; border: none;')
        self._timer = QTimer(self)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._tick)
        self.hide()

    def set_volume(self, level: float) -> None:
        """Permet d'injecter directement le volume audio."""
        try:
            val = max(0.0, min(1.0, float(level)))
            self._direct_vol = val
            if self._source is not None:
                setattr(self._source, '_direct_vol', val)
                if hasattr(self._source, "set_volume"):
                    self._source.set_volume(val)
        except Exception:
            pass

    def _tick(self):
        if self.isVisible():
            self.update()

    def showEvent(self, event):
        self._timer.start()
        super().showEvent(event)

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def paintEvent(self, event):
        if min(self.width(), self.height()) < 2:
            return
        p = QPainter(self)
        if self._source is not None and self._direct_vol > 0.0:
            setattr(self._source, '_direct_vol', self._direct_vol)
        paint_reactor(p, QRectF(self.rect()), self._source, time.monotonic() - self._started)
        p.end()
