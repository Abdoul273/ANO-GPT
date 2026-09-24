"""NEBULA — galaxie inclinée à trois bras de poussière lumineuse."""
from __future__ import annotations

import math
import random

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QPainter, QPixmap, QRadialGradient

from ui.orb.base import BaseOrb, mix, with_alpha


class NebulaOrb(BaseOrb):
    RADIUS_RATIO = .33
    ARM_COUNT = 3
    GRAINS_PER_ARM = 170
    CORE_GRAINS = 66

    def __init__(self, face_path: str = "", assistant_name: str = "ANO-GPT", parent=None):
        super().__init__(face_path, assistant_name, parent)
        rng = random.Random(2094)
        self._grains: list[tuple[float, float, float, float, int, int]] = []
        for arm in range(self.ARM_COUNT):
            for index in range(8):
                radius = .17 + .76 * (index + .5) / 8
                angle = arm * math.tau / self.ARM_COUNT + 2.9 * math.log1p(radius * 3.0)
                angle += rng.gauss(0.0, .10)
                self._grains.append((radius, angle, rng.uniform(-.04, .04),
                                     rng.random() * math.tau, arm, 4))
        for arm in range(self.ARM_COUNT):
            for index in range(self.GRAINS_PER_ARM):
                radius = .10 + .89 * ((index + rng.random()) / self.GRAINS_PER_ARM) ** .83
                angle = arm * math.tau / self.ARM_COUNT + 2.9 * math.log1p(radius * 3.0)
                angle += rng.gauss(0.0, .13 + .19 * radius)
                lane = index % 12
                kind = 0 if lane < 7 else (1 if lane < 10 else 2)
                self._grains.append((radius, angle, rng.uniform(-.10, .10),
                                     rng.random() * math.tau, arm, kind))
        for index in range(self.CORE_GRAINS):
            radius = .02 + .20 * math.sqrt(rng.random())
            self._grains.append((radius, rng.random() * math.tau,
                                 rng.uniform(-.08, .08), rng.random() * math.tau,
                                 index % self.ARM_COUNT, 3))
        self._grain_phases = [grain[3] for grain in self._grains]
        self._grain_speeds = [(.45 + 1.35 * (1.0-grain[0])) * rng.uniform(.82, 1.18)
                              for grain in self._grains]
        self._x = [0.0] * len(self._grains)
        self._y = [0.0] * len(self._grains)
        self._sizes = (5, 13, 29, 13, 63)
        self._colors = (QColor("#ff4fd8"), QColor("#9569ff"), QColor("#64c5ff"))
        self._core_color = QColor("#fff0ff")
        self._sprites: dict[str, tuple[tuple[QPixmap, ...], ...]] = {}
        self._rotation = 0.0
        self._orbit = 0.0
        self._scale = .93
        self._flare = 0.0
        self._last_volume = 0.0
        self.on_palette_changed()
        self.advance(1/30, 0.0)

    def on_palette_changed(self) -> None:
        sprites = {}
        for state, palette in self._PALETTES.items():
            arms = []
            for arm in range(self.ARM_COUNT):
                tint = mix(self._colors[arm], palette["wire"], .25)
                variants = []
                for kind, size in enumerate(self._sizes):
                    color = mix(self._core_color, palette["hot"], .30) if kind == 3 else tint
                    pixmap = QPixmap(size, size)
                    pixmap.fill(Qt.GlobalColor.transparent)
                    painter = QPainter(pixmap)
                    gradient = QRadialGradient(size/2, size/2, size/2)
                    alpha = (.90, .66, .23, .78, .13)[kind]
                    gradient.setColorAt(0.0, with_alpha(color, alpha))
                    gradient.setColorAt(.23, with_alpha(color, alpha * .55))
                    gradient.setColorAt(1.0, with_alpha(color, 0.0))
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(gradient)
                    painter.drawEllipse(0, 0, size, size)
                    painter.end()
                    variants.append(pixmap)
                arms.append(tuple(variants))
            sprites[state] = tuple(arms)
        self._sprites = sprites

    def advance(self, dt: float, t: float) -> None:
        state = self.visual_state
        volume = self.volume
        speed = .17 + self.spin * .09 + (.26 if state == "acting" else 0.0)
        self._rotation += dt * speed
        self._orbit += dt * (.55 + .25 * volume)
        self._flare = max(self._flare * math.exp(-dt * 4.0),
                          min(1.0, max(0.0, volume-self._last_volume) * 3.5))
        self._last_volume = volume
        target = (.77 if state == "thinking" else
                  .87 if state == "listening" else
                  1.02 + .20*volume if state == "speaking" else
                  1.12 if state == "acting" else .93)
        self._scale += (target-self._scale) * (1.0-math.exp(-dt*5.0))
        tilt = .58 + .07 * math.sin(self._orbit * .4)
        shear = .42 * math.sin(self._orbit * .7)
        wobble = .09 if state == "error" else .015
        flow_speed = 1.0 + volume * 1.8
        flow_radius = .032 + volume * .055
        flow_angle = .10 + volume * .16
        for index, (radius, angle, depth, _phase, _arm, kind) in enumerate(self._grains):
            local_phase = self._grain_phases[index] + dt*self._grain_speeds[index]*flow_speed
            if local_phase >= math.tau:
                local_phase -= math.tau
            self._grain_phases[index] = local_phase
            local_sin = math.sin(local_phase)
            flow = .25 if kind == 4 else 1.0
            swirl = self._rotation + (1.0-radius) * shear
            theta = angle + swirl + flow*flow_angle*math.cos(local_phase)
            theta += wobble * local_sin
            shell = radius * self._scale * (1.0 + volume*.12*local_sin)
            shell += flow*flow_radius*(.4+.6*radius)*local_sin
            if kind == 3:
                shell *= 1.0 + self._flare*.22
            x = shell * math.cos(theta)
            y = shell * math.sin(theta)
            self._x[index] = x
            self._y[index] = y*tilt + depth*(.45+.12*local_sin)

    def paint_orb(self, p: QPainter, cx: float, cy: float, radius: float, t: float) -> None:
        sprites = self._sprites.get(self.visual_state, self._sprites["idle"])
        p.save()
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        for index, (_, _, _, _, arm, kind) in enumerate(self._grains):
            size = self._sizes[kind]
            p.drawPixmap(int(cx + radius*self._x[index] - size*.5),
                         int(cy + radius*self._y[index] - size*.5),
                         sprites[arm][kind])
        p.restore()
