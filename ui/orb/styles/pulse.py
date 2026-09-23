"""PULSE — exemple de référence d'un style d'orbe, volontairement minimal.

Trois anneaux qui respirent avec la voix et un cœur lumineux. Tout le reste
(états, volume, palette, cadence, sommeil) vient de ``BaseOrb``.
"""
from __future__ import annotations

import math

from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QPainter, QPen, QRadialGradient

from ui.orb.base import BaseOrb, with_alpha


class PulseOrb(BaseOrb):
    RING_COUNT = 3

    def __init__(self, face_path: str = "", assistant_name: str = "ANO-GPT", parent=None):
        super().__init__(face_path, assistant_name, parent)
        self._phase = 0.0

    def advance(self, dt: float, t: float) -> None:
        self._phase += dt * self.pulse_speed * (0.6 + self.energy)

    def paint_orb(self, p: QPainter, cx: float, cy: float, radius: float, t: float) -> None:
        pal = self.palette_live
        center = QPointF(cx, cy)
        swell = 1.0 + 0.18 * self.volume

        glow = QRadialGradient(center, radius * 1.4 * swell)
        glow.setColorAt(0.0, with_alpha(pal["hot"], 0.85))
        glow.setColorAt(0.25, with_alpha(pal["core"], 0.45))
        glow.setColorAt(1.0, with_alpha(pal["halo"], 0.0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(glow)
        p.drawEllipse(center, radius * 1.4 * swell, radius * 1.4 * swell)

        p.setBrush(Qt.BrushStyle.NoBrush)
        for i in range(self.RING_COUNT):
            wave = math.sin(self._phase * math.tau + i * 2.1)
            r = radius * (0.55 + 0.25 * i) * (1.0 + 0.05 * wave + 0.10 * self.bands[i * 2])
            pen = QPen(with_alpha(pal["wire"], 0.35 + 0.4 * self.energy / (1 + i)), 1.6)
            p.setPen(pen)
            p.drawEllipse(center, r, r)
