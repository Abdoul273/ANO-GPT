"""SPECTRE — nuage de photons sculpté par les huit bandes audio.

Ce style ne trace que des particules : pas de cadran, de barre, de cercle ou de
fond. Le son déforme un volume 3D en huit secteurs ; l'écoute l'aspire, la
parole le dilate et les attaques lancent une onde dans le nuage.
"""
from __future__ import annotations

import math
import random

from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QPainter, QPen, QPolygonF

from ui.orb.base import BaseOrb, with_alpha


class RadialOrb(BaseOrb):
    RADIUS_RATIO = 0.31
    PARTICLE_COUNT = 1100

    def __init__(self, face_path: str = "", assistant_name: str = "ANO-GPT", parent=None):
        super().__init__(face_path, assistant_name, parent)
        rng = random.Random(2067)
        golden_angle = math.pi * (3.0 - math.sqrt(5.0))
        self._particles: list[tuple[float, float, float, float, float, int]] = []
        self._group_indices: list[list[int]] = [[], [], []]
        for index in range(self.PARTICLE_COUNT):
            # Fibonacci sphere + rayon intérieur : volume régulier sans grille.
            y = 1.0 - 2.0 * (index + .5) / self.PARTICLE_COUNT
            ring = math.sqrt(max(0.0, 1.0 - y * y))
            angle = index * golden_angle
            x, z = math.cos(angle) * ring, math.sin(angle) * ring
            lane = index % 12
            group = 0 if lane < 8 else (1 if lane < 11 else 2)
            if group == 2 and index % 24 == 11:
                shell = .06 + .38 * math.sqrt(rng.random())
            elif index % 7 == 0:
                shell = .10 + .55 * math.sqrt(rng.random())
            else:
                shell = .42 + .58 * math.sqrt(rng.random())
            band = int((math.atan2(y, x) + math.pi) * 8 / math.tau) % 8
            wave_offset = -shell * 11.0 + band * .68 + rng.random() * math.tau * .18
            self._particles.append((x, y, z, shell, wave_offset, band))
            self._group_indices[group].append(index)

        # QPolygonF et QPointF vivent jusqu'à la destruction du style. Chaque
        # image ne fait que déplacer ces points, puis trois drawPoints groupés.
        self._polygons = [QPolygonF([QPointF() for _ in group])
                          for group in self._group_indices]
        self._point_refs = [[polygon[index] for index in range(len(polygon))]
                            for polygon in self._polygons]
        self._x = [0.0] * self.PARTICLE_COUNT
        self._y = [0.0] * self.PARTICLE_COUNT
        self._levels = [0.0] * 8
        self._phase = 0.0
        self._rotation = 0.0
        self._travel = 0.0
        self._shock = 0.0
        self._last_volume = 0.0
        self._last_signal = 0.0
        self._scale = .91
        self._seen_state = self.visual_state
        self._transition = 0.0
        self.advance(1 / 30, 0.0)

    def advance(self, dt: float, t: float) -> None:
        state = self.visual_state
        volume = self.volume
        speed = .32 + self.spin * .13 + volume * .7
        self._phase += dt * speed
        self._rotation += dt * (.16 + self.spin * .10 + volume * .34)
        self._travel = (self._travel + dt * (1.0 + volume * .7)) % 1.35
        if state != self._seen_state:
            self._seen_state = state
            self._transition = 1.0
        self._transition *= math.exp(-dt * 6.0)

        bands = self.bands
        signal_sum = 0.0
        for index in range(8):
            target = bands[index]
            rate = 16.0 if target > self._levels[index] else 8.0
            self._levels[index] += (target - self._levels[index]) * (1.0 - math.exp(-dt * rate))
            signal_sum += self._levels[index]
        signal_mean = signal_sum * .125
        onset = max(0.0, volume - self._last_volume,
                    (signal_mean - self._last_signal) * 1.5)
        self._last_volume = volume
        self._last_signal = signal_mean
        self._shock = max(self._shock * math.exp(-dt * 4.2), min(1.0, onset * 3.5))

        if state == "listening":
            target_scale = .69 + .12 * signal_mean
        elif state == "speaking":
            target_scale = 1.04 + .22 * volume + .08 * signal_mean
        elif state == "thinking":
            target_scale = .86
        elif state == "acting":
            target_scale = 1.12
        elif state == "error":
            target_scale = .79
        else:
            target_scale = .90
        target_scale -= self._transition * .16
        self._scale += (target_scale - self._scale) * (1.0 - math.exp(-dt * 7.0))

        breath = 1.0 + .055 * math.sin(self._phase * 2.1)
        turbulence = .055 if state == "idle" else (.13 if state == "thinking" else .22)
        wave_speed = 2.5 if state == "idle" else (4.8 if state == "speaking" else 3.6)
        co, si = math.cos(self._rotation), math.sin(self._rotation)
        tilt = .28 * math.sin(self._phase * .9)
        ct, st = math.cos(tilt), math.sin(tilt)
        wave_phase = self._phase * wave_speed
        for index, (x, y, z, shell, wave_offset, band) in enumerate(self._particles):
            signal = self._levels[band]
            x_rot = x * co - z * si
            z_rot = x * si + z * co
            y_rot = y * ct - z_rot * st
            z_rot = y * st + z_rot * ct
            ripple = math.sin(wave_phase + wave_offset)
            front = max(0.0, 1.0 - abs(shell - self._travel) / .19)
            swell = self._scale * (breath + signal * ripple * turbulence)
            swell += self._shock * front * .26
            depth = 1.0 + z_rot * shell * .22
            drift = ripple * (.025 + signal * .075)
            self._x[index] = (x_rot * shell * swell - y_rot * drift) * depth
            self._y[index] = (y_rot * shell * swell + x_rot * drift) * depth

    def paint_orb(self, p: QPainter, cx: float, cy: float, radius: float, t: float) -> None:
        pal = self.palette_live
        energy = min(1.0, self.energy * .65 + self.volume * .35)
        for indices, points in zip(self._group_indices, self._point_refs):
            for slot, index in enumerate(indices):
                point = points[slot]
                point.setX(cx + radius * self._x[index])
                point.setY(cy + radius * self._y[index])

        p.save()
        p.setPen(QPen(with_alpha(pal["core"], .69 + energy * .16), 1.0))
        p.drawPoints(self._polygons[0])
        p.setPen(QPen(with_alpha(pal["wire"], .78 + energy * .20), 2.1,
                      Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawPoints(self._polygons[1])
        p.setPen(QPen(with_alpha(pal["hot"], .86 + energy * .14), 3.2,
                      Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawPoints(self._polygons[2])
        p.restore()
