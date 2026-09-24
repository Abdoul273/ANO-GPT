"""PULSE — résonateur circulaire à membrane sonore et ondes d'écho."""
from __future__ import annotations

import math

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QPainter, QPen, QPixmap, QPolygonF, QRadialGradient

from ui.orb.base import BaseOrb, with_alpha


class PulseOrb(BaseOrb):
    RADIUS_RATIO = .27
    SAMPLE_COUNT = 80
    ECHO_COUNT = 3

    def __init__(self, face_path: str = "", assistant_name: str = "ANO-GPT", parent=None):
        super().__init__(face_path, assistant_name, parent)
        idle = self._PALETTES["idle"]
        idle.update(core=QColor("#6bffc0"), halo=QColor("#00a870"),
                    wire=QColor("#00e69a"), hot=QColor("#d9ffee"))
        self._PALETTES["speaking"].update(
            core=QColor("#78ffd1"), halo=QColor("#00c98b"),
            wire=QColor("#34ffc2"), hot=QColor("#effff9"),
        )
        for key in ("core", "halo", "wire", "hot"):
            self._pal_live[key] = QColor(idle[key])
        self._samples = []
        for index in range(self.SAMPLE_COUNT):
            angle = math.tau * index / self.SAMPLE_COUNT
            sector = index * 8.0 / self.SAMPLE_COUNT
            band = int(sector)
            self._samples.append((math.cos(angle), math.sin(angle), band,
                                  (band + 1) % 8, sector - band, angle))
        self._outline = QPolygonF([QPointF() for _ in self._samples])
        self._points = [self._outline[index] for index in range(self.SAMPLE_COUNT)]
        self._radii = [0.0] * self.SAMPLE_COUNT
        self._levels = [0.0] * 8
        self._echoes = [index / self.ECHO_COUNT for index in range(self.ECHO_COUNT)]
        self._echo_strength = [.35] * self.ECHO_COUNT
        self._echo_cursor = 0
        self._phase = 0.0
        self._scale = .94
        self._impact = 0.0
        self._last_volume = 0.0
        self._glow_sprites: dict[str, QPixmap] = {}
        self._glow_src = QRectF(0, 0, 128, 128)
        self._glow_dst = QRectF()
        self._scan_rect = QRectF()
        self.on_palette_changed()
        self.advance(1/30, 0.0)

    def on_palette_changed(self) -> None:
        sprites = {}
        for state, palette in self._PALETTES.items():
            pixmap = QPixmap(128, 128)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            gradient = QRadialGradient(64, 64, 64)
            gradient.setColorAt(0.0, with_alpha(palette["hot"], .88))
            gradient.setColorAt(.15, with_alpha(palette["core"], .55))
            gradient.setColorAt(.48, with_alpha(palette["halo"], .13))
            gradient.setColorAt(1.0, with_alpha(palette["halo"], 0.0))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(gradient)
            painter.drawEllipse(0, 0, 128, 128)
            painter.end()
            sprites[state] = pixmap
        self._glow_sprites = sprites

    def advance(self, dt: float, t: float) -> None:
        state = self.visual_state
        volume = self.volume
        self._phase += dt * (.85 + .28 * self.pulse_speed + volume * 1.1)
        target_scale = (.78 if state == "listening" else
                        1.02 + .17*volume if state == "speaking" else
                        1.08 if state == "acting" else
                        .89 if state == "thinking" else .94)
        self._scale += (target_scale-self._scale) * (1.0-math.exp(-dt*7.0))
        onset = max(0.0, volume-self._last_volume)
        self._last_volume = volume
        self._impact = max(self._impact*math.exp(-dt*5.0), min(1.0, onset*4.0))
        if onset > .075:
            self._echoes[self._echo_cursor] = 0.0
            self._echo_strength[self._echo_cursor] = min(1.0, .45 + onset*2.5)
            self._echo_cursor = (self._echo_cursor + 1) % self.ECHO_COUNT
        echo_speed = .14 + .045*self.pulse_speed + volume*.12
        for index in range(self.ECHO_COUNT):
            self._echoes[index] += dt*echo_speed
            if self._echoes[index] >= 1.0:
                self._echoes[index] -= 1.0
                self._echo_strength[index] = .33 + volume*.32

        bands = self.bands
        for index in range(8):
            target = bands[index]
            rate = 18.0 if target > self._levels[index] else 8.0
            self._levels[index] += (target-self._levels[index]) * (1.0-math.exp(-dt*rate))
        phase = self._phase
        for index, (_, _, band, next_band, blend, angle) in enumerate(self._samples):
            signal = self._levels[band]*(1.0-blend) + self._levels[next_band]*blend
            interference = math.sin(angle*6.0-phase*3.6)
            self._radii[index] = self._scale * (.67 + signal*(.15+.09*interference))
            self._radii[index] += .014*math.sin(angle*3.0+phase*1.3)
            self._radii[index] += self._impact*.025

    def paint_orb(self, p: QPainter, cx: float, cy: float, radius: float, t: float) -> None:
        pal = self.palette_live
        center = QPointF(cx, cy)
        for index, (co, si, _, _, _, _) in enumerate(self._samples):
            point = self._points[index]
            distance = radius*self._radii[index]
            point.setX(cx + co*distance)
            point.setY(cy + si*distance)

        p.save()
        glow_size = radius*(.82 + self._impact*.22 + self.volume*.12)
        self._glow_dst.setRect(cx-glow_size*.5, cy-glow_size*.5, glow_size, glow_size)
        p.drawPixmap(self._glow_dst,
                     self._glow_sprites.get(self.visual_state, self._glow_sprites["idle"]),
                     self._glow_src)
        p.setBrush(Qt.BrushStyle.NoBrush)
        for progress, strength in zip(self._echoes, self._echo_strength):
            echo_radius = radius*(.20 + progress*1.08)*self._scale
            opacity = strength*(1.0-progress)*(.35+.29*min(1.0, self.energy))
            p.setPen(QPen(with_alpha(pal["wire"], opacity), 1.1+progress*.8))
            p.drawEllipse(center, echo_radius, echo_radius)

        scan_radius = radius*.97*self._scale
        self._scan_rect.setRect(cx-scan_radius, cy-scan_radius,
                                scan_radius*2, scan_radius*2)
        p.setPen(QPen(with_alpha(pal["hot"], .45+.30*self.volume), 2.2,
                      Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(self._scan_rect, int(-self._phase*1050), 560)

        p.setPen(QPen(with_alpha(pal["wire"], .75+.20*min(1.0, self.energy)), 2.4))
        p.drawPolygon(self._outline)
        p.setPen(QPen(with_alpha(pal["hot"], .78), 1.3))
        p.drawEllipse(center, radius*.12*self._scale, radius*.12*self._scale)
        p.restore()
