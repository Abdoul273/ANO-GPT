"""GÉODÉSIQUE — membrane triangulée 3D, articulée par le spectre audio."""
from __future__ import annotations

import math

from PyQt6.QtCore import QLineF, QPointF, Qt
from PyQt6.QtGui import QColor, QPainter, QPen, QPolygonF

from ui.orb.base import BaseOrb, mix, with_alpha


class WireframeOrb(BaseOrb):
    RADIUS_RATIO = .28

    def __init__(self, face_path: str = "", assistant_name: str = "ANO-GPT", parent=None):
        super().__init__(face_path, assistant_name, parent)
        golden = (1.0 + math.sqrt(5.0)) / 2.0
        raw = [
            (-1, golden, 0), (1, golden, 0), (-1, -golden, 0), (1, -golden, 0),
            (0, -1, golden), (0, 1, golden), (0, -1, -golden), (0, 1, -golden),
            (golden, 0, -1), (golden, 0, 1), (-golden, 0, -1), (-golden, 0, 1),
        ]
        vertices = []
        for x, y, z in raw:
            length = math.sqrt(x*x + y*y + z*z)
            vertices.append((x/length, y/length, z/length))
        faces = [
            (0, 11, 5), (0, 5, 1), (0, 1, 7), (0, 7, 10), (0, 10, 11),
            (1, 5, 9), (5, 11, 4), (11, 10, 2), (10, 7, 6), (7, 1, 8),
            (3, 9, 4), (3, 4, 2), (3, 2, 6), (3, 6, 8), (3, 8, 9),
            (4, 9, 5), (2, 4, 11), (6, 2, 10), (8, 6, 7), (9, 8, 1),
        ]
        # Une subdivision : 42 sommets / 120 arêtes. Les grandes facettes
        # restent lisibles et la voix garde du temps CPU sur deux cœurs.
        for _ in range(1):
            midpoint_cache: dict[tuple[int, int], int] = {}

            def midpoint(a: int, b: int, cache=midpoint_cache) -> int:
                key = (min(a, b), max(a, b))
                found = cache.get(key)
                if found is not None:
                    return found
                va, vb = vertices[a], vertices[b]
                x, y, z = ((va[i] + vb[i]) * .5 for i in range(3))
                length = math.sqrt(x*x + y*y + z*z)
                found = len(vertices)
                vertices.append((x/length, y/length, z/length))
                cache[key] = found
                return found

            divided = []
            for a, b, c in faces:
                ab, bc, ca = midpoint(a, b), midpoint(b, c), midpoint(c, a)
                divided.extend(((a, ab, ca), (b, bc, ab),
                                (c, ca, bc), (ab, bc, ca)))
            faces = divided

        self._vertices = []
        for x, y, z in vertices:
            sector = (math.atan2(y, x) + math.pi) * 8.0 / math.tau
            band = int(sector) % 8
            self._vertices.append((x, y, z, band, (band + 1) % 8,
                                   sector - int(sector)))
        self._edges = sorted({(min(a, b), max(a, b))
                              for a, b, c in faces
                              for a, b in ((a, b), (b, c), (c, a))})
        self._screen_x = [0.0] * len(vertices)
        self._screen_y = [0.0] * len(vertices)
        self._depth = [0.0] * len(vertices)
        self._mesh_lines = [QLineF() for _ in self._edges]
        self._front = [QLineF(-1000, -1000, -1000, -1000) for _ in range(75)]
        self._scan_lines = [QLineF(-1000, -1000, -1000, -1000) for _ in range(36)]
        self._front_used = 0
        self._scan_used = 0
        self._nodes = QPolygonF([QPointF() for _ in vertices])
        self._node_refs = [self._nodes[i] for i in range(len(vertices))]
        self._bands_live = [0.0] * 8
        self._metal_color = QColor("#ffad38")
        self._glint_color = QColor("#fff1bd")
        self._yaw = .0
        self._pitch = .0
        self._wave = .0
        self._scan = .0
        self._scale = .92
        self._shock = .0
        self._last_volume = .0
        self.advance(1/30, 0.0)

    def advance(self, dt: float, t: float) -> None:
        state = self.visual_state
        volume = self.volume
        bands = self.bands
        for index in range(8):
            target = bands[index]
            rate = 14.0 if target > self._bands_live[index] else 7.0
            self._bands_live[index] += (target - self._bands_live[index]) * (1-math.exp(-dt*rate))
        self._shock = max(self._shock * math.exp(-dt*4.0),
                          min(1.0, max(0.0, volume-self._last_volume)*3.0))
        self._last_volume = volume
        target_scale = (.79 if state == "listening" else
                        1.05 + .17*volume if state == "speaking" else
                        1.09 if state == "acting" else .92)
        self._scale += (target_scale-self._scale) * (1-math.exp(-dt*6.0))
        self._yaw += dt * (.22 + .12*self.spin + .32*volume)
        self._pitch += dt * (.13 + .09*self.spin)
        self._wave += dt * (2.1 + 1.5*volume)
        self._scan = (self._scan + dt * (.52 + .24*volume)) % 1.8

        cy, sy = math.cos(self._yaw), math.sin(self._yaw)
        cp, sp = math.cos(self._pitch), math.sin(self._pitch)
        wave = self._wave
        scale = self._scale * (1.0 + .025*math.sin(wave*.8))
        for index, (x, y, z, band, next_band, blend) in enumerate(self._vertices):
            signal = self._bands_live[band]*(1.0-blend) + self._bands_live[next_band]*blend
            membrane = 1.0 + signal*(.11 + .065*math.sin(wave*2.2-y*5.0))
            membrane += self._shock * .10 * max(0.0, 1.0-abs(y-(self._scan-.9))/.35)
            xr = x*cy-z*sy
            zr = x*sy+z*cy
            yr = y*cp-zr*sp
            zr = y*sp+zr*cp
            perspective = scale*membrane/(1.0-zr*.20)
            self._screen_x[index] = xr*perspective
            self._screen_y[index] = yr*perspective
            self._depth[index] = zr

    def paint_orb(self, p: QPainter, cx: float, cy: float, radius: float, t: float) -> None:
        scan_y = self._scan-.9
        front_used = 0
        scan_used = 0
        for slot, (a, b) in enumerate(self._edges):
            ax, ay = cx+radius*self._screen_x[a], cy+radius*self._screen_y[a]
            bx, by = cx+radius*self._screen_x[b], cy+radius*self._screen_y[b]
            depth = (self._depth[a]+self._depth[b])*.5
            self._mesh_lines[slot].setLine(ax, ay, bx, by)
            if depth > -.02 and front_used < len(self._front):
                self._front[front_used].setLine(ax, ay, bx, by)
                front_used += 1
            if (depth > .05 and scan_used < len(self._scan_lines)
                    and abs((self._vertices[a][1]+self._vertices[b][1])*.5-scan_y) < .18):
                self._scan_lines[scan_used].setLine(ax, ay, bx, by)
                scan_used += 1
        for slot in range(front_used, self._front_used):
            self._front[slot].setLine(-1000, -1000, -1000, -1000)
        for slot in range(scan_used, self._scan_used):
            self._scan_lines[slot].setLine(-1000, -1000, -1000, -1000)
        self._front_used = front_used
        self._scan_used = scan_used
        for index, point in enumerate(self._node_refs):
            point.setX(cx+radius*self._screen_x[index])
            point.setY(cy+radius*self._screen_y[index])

        pal = self.palette_live
        metal = mix(self._metal_color, pal["wire"], .42)
        glint = mix(self._glint_color, pal["hot"], .42)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(with_alpha(metal, .19), .8))
        p.drawLines(self._mesh_lines)
        p.setPen(QPen(with_alpha(metal, .54+.22*min(1.0, self.energy)), 1.25))
        p.drawLines(self._front)
        p.setPen(QPen(with_alpha(glint, .85), 2.1,
                      Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLines(self._scan_lines)
        p.setPen(QPen(with_alpha(glint, .72), 2.0,
                      Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawPoints(self._nodes)
        p.restore()
