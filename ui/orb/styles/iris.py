"""IRIS — diaphragme holographique à huit lames articulées par le son."""
from __future__ import annotations

import math

from PyQt6.QtCore import QLineF, QPointF, Qt
from PyQt6.QtGui import QColor, QPainter, QPen, QPolygonF

from ui.orb.base import BaseOrb, with_alpha


class IrisOrb(BaseOrb):
    RADIUS_RATIO = .29
    BLADE_COUNT = 8

    def __init__(self, face_path: str = "", assistant_name: str = "ANO-GPT", parent=None):
        super().__init__(face_path, assistant_name, parent)
        idle = self._PALETTES["idle"]
        idle.update(core=QColor("#bba5ff"), halo=QColor("#7551ca"),
                    wire=QColor("#9d7aff"), hot=QColor("#f2eaff"))
        speaking = self._PALETTES["speaking"]
        speaking.update(core=QColor("#c6a8ff"), halo=QColor("#855eff"),
                        wire=QColor("#ad8aff"), hot=QColor("#fff0ff"))
        for key in ("core", "halo", "wire", "hot"):
            self._pal_live[key] = QColor(idle[key])

        self._blades = [QPolygonF([QPointF() for _ in range(5)])
                        for _ in range(self.BLADE_COUNT)]
        self._blade_points = [[blade[index] for index in range(5)]
                              for blade in self._blades]
        self._spines = [QLineF() for _ in range(self.BLADE_COUNT)]
        self._aperture = QPolygonF([QPointF() for _ in range(self.BLADE_COUNT)])
        self._aperture_points = [self._aperture[index] for index in range(self.BLADE_COUNT)]
        self._levels = [0.0] * self.BLADE_COUNT
        self._spin_angle = 0.0
        self._aperture_size = .27
        self._impact = 0.0
        self._last_volume = 0.0
        self._scan = 0.0
        self.advance(1/30, 0.0)

    def advance(self, dt: float, t: float) -> None:
        state = self.visual_state
        volume = self.volume
        self._spin_angle += dt * (.14 + .10*self.spin + .28*volume)
        self._scan = (self._scan + dt*(1.8+2.0*volume)) % self.BLADE_COUNT
        target = (.18 if state == "listening" else
                  .38+.16*volume if state == "speaking" else
                  .45 if state == "acting" else
                  .24 if state == "thinking" else .27)
        self._aperture_size += (target-self._aperture_size) * (1.0-math.exp(-dt*6.0))
        onset = max(0.0, volume-self._last_volume)
        self._last_volume = volume
        self._impact = max(self._impact*math.exp(-dt*5.0), min(1.0, onset*3.5))
        bands = self.bands
        for index in range(self.BLADE_COUNT):
            level = bands[index]
            rate = 17.0 if level > self._levels[index] else 8.0
            self._levels[index] += (level-self._levels[index]) * (1.0-math.exp(-dt*rate))

    def paint_orb(self, p: QPainter, cx: float, cy: float, radius: float, t: float) -> None:
        pal = self.palette_live
        step = math.tau/self.BLADE_COUNT
        rotation = self._spin_angle
        aperture = self._aperture_size
        for index, points in enumerate(self._blade_points):
            theta = rotation + index*step
            signal = self._levels[index]
            inner = aperture + signal*.035
            outer = .89 + signal*.06 + self._impact*.035
            twist = .12 + signal*.14 + .04*math.sin(rotation*2.2+index*.8)
            angle = theta+.10
            points[0].setX(cx + radius*inner*math.cos(angle))
            points[0].setY(cy + radius*inner*math.sin(angle))
            angle = theta+step*.90
            points[1].setX(cx + radius*inner*math.cos(angle))
            points[1].setY(cy + radius*inner*math.sin(angle))
            angle = theta+step*1.18+twist
            points[2].setX(cx + radius*outer*math.cos(angle))
            points[2].setY(cy + radius*outer*math.sin(angle))
            angle = theta+step*.28+twist
            points[3].setX(cx + radius*outer*math.cos(angle))
            points[3].setY(cy + radius*outer*math.sin(angle))
            angle = theta+step*.10+twist*.35
            middle = (inner+outer)*.58
            points[4].setX(cx + radius*middle*math.cos(angle))
            points[4].setY(cy + radius*middle*math.sin(angle))
            self._spines[index].setLine(points[0].x(), points[0].y(),
                                        points[3].x(), points[3].y())
            tip = self._aperture_points[index]
            tip.setX(points[0].x())
            tip.setY(points[0].y())

        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.setBrush(Qt.BrushStyle.NoBrush)
        for index, blade in enumerate(self._blades):
            distance = abs(index-self._scan)
            chase = max(0.0, 1.0-min(distance, self.BLADE_COUNT-distance))
            signal = self._levels[index]
            p.setBrush(with_alpha(pal["halo"], .17+.10*signal+.08*chase))
            p.setPen(QPen(with_alpha(pal["wire"], .38+.23*signal+.22*chase), 1.3))
            p.drawPolygon(blade)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(with_alpha(pal["hot"], .55+.25*self.volume), 1.9,
                      Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLines(self._spines)
        p.setPen(QPen(with_alpha(pal["hot"], .62), 1.4))
        p.drawPolygon(self._aperture)
        p.restore()
