"""VISAGE — un visage holographique : regard vivant, bouche portée par la voix.

Tout est dessiné au trait, sans image : deux yeux qui clignent et bougent par
saccades, des sourcils qui portent l'expression de l'état, une bouche qui
s'ouvre au rythme du volume et des bandes FFT pendant que l'assistant parle.
"""
from __future__ import annotations

import math
import random

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QPainter, QPainterPath, QPen, QPixmap, QRadialGradient

from ui.orb.base import BaseOrb, ease, with_alpha

# Expression par état : ouverture des yeux, sourire (+) / moue (−),
# hauteur des sourcils, inclinaison des sourcils (+ = froncés vers le centre),
# direction du regard au repos (x, y en fraction de l'amplitude).
_EXPRESSIONS = {
    "idle":      {"open": .92, "smile": .30, "brow": .00, "tilt": .00, "gaze": (0.0, 0.0)},
    "listening": {"open": 1.1, "smile": .45, "brow": .22, "tilt": -.10, "gaze": (0.0, 0.0)},
    "thinking":  {"open": .80, "smile": .05, "brow": .12, "tilt": .10, "gaze": (.65, -.75)},
    "speaking":  {"open": 1.0, "smile": .35, "brow": .10, "tilt": -.05, "gaze": (0.0, 0.0)},
    "acting":    {"open": .70, "smile": .10, "brow": -.10, "tilt": .30, "gaze": (0.0, .15)},
    "error":     {"open": .85, "smile": -.55, "brow": .05, "tilt": -.45, "gaze": (0.0, .35)},
}


class FaceOrb(BaseOrb):
    RADIUS_RATIO = .26

    def __init__(self, face_path: str = "", assistant_name: str = "ANO-GPT", parent=None):
        super().__init__(face_path, assistant_name, parent)
        base = _EXPRESSIONS["idle"]
        self._open = base["open"]
        self._smile = base["smile"]
        self._brow = base["brow"]
        self._tilt = base["tilt"]
        self._gaze_x = self._gaze_y = 0.0
        self._look_x = self._look_y = 0.0
        self._next_saccade = 1.0
        self._blink = 0.0            # 0 ouvert → 1 fermé
        self._blink_t = -1.0         # < 0 : pas de clignement en cours
        self._next_blink = 2.5
        self._mouth = 0.0            # ouverture de la bouche 0–1
        self._mouth_wave = [0.0] * 8
        self._phase = 0.0
        self._rng = random.Random()
        self._glow_sprites: dict[str, QPixmap] = {}
        self._glow_src = QRectF(0, 0, 128, 128)
        self._glow_dst = QRectF()
        self._head_rect = QRectF()
        self._scan_rect = QRectF()
        self._eye_rect = QRectF()
        self._path = QPainterPath()
        self.on_palette_changed()

    def on_palette_changed(self) -> None:
        sprites = {}
        for state, palette in self._PALETTES.items():
            pixmap = QPixmap(128, 128)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            gradient = QRadialGradient(64, 64, 64)
            gradient.setColorAt(0.0, with_alpha(palette["core"], .30))
            gradient.setColorAt(.55, with_alpha(palette["halo"], .10))
            gradient.setColorAt(1.0, with_alpha(palette["halo"], 0.0))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(gradient)
            painter.drawEllipse(0, 0, 128, 128)
            painter.end()
            sprites[state] = pixmap
        self._glow_sprites = sprites

    # ── Simulation ──────────────────────────────────────────────────────────
    def advance(self, dt: float, t: float) -> None:
        state = self.visual_state
        expr = _EXPRESSIONS.get(state, _EXPRESSIONS["idle"])
        self._phase += dt
        self._open = ease(self._open, expr["open"], dt, 6.0)
        self._smile = ease(self._smile, expr["smile"], dt, 5.0)
        self._brow = ease(self._brow, expr["brow"] + .10*self.volume, dt, 7.0)
        self._tilt = ease(self._tilt, expr["tilt"], dt, 5.0)

        # Regard : direction de l'état + petites saccades aléatoires.
        self._next_saccade -= dt
        if self._next_saccade <= 0.0:
            spread = .25 if state in ("listening", "speaking") else .55
            self._look_x = self._rng.uniform(-spread, spread)
            self._look_y = self._rng.uniform(-spread*.5, spread*.5)
            self._next_saccade = self._rng.uniform(.8, 3.2)
        gx, gy = expr["gaze"]
        self._gaze_x = ease(self._gaze_x, gx + self._look_x*(1.0-abs(gx)), dt, 14.0)
        self._gaze_y = ease(self._gaze_y, gy + self._look_y*(1.0-abs(gy)), dt, 14.0)

        # Clignements : réguliers, parfois doublés.
        self._next_blink -= dt
        if self._blink_t < 0.0 and self._next_blink <= 0.0:
            self._blink_t = 0.0
            double = self._rng.random() < .18
            self._next_blink = .35 if double else self._rng.uniform(2.2, 5.5)
        if self._blink_t >= 0.0:
            self._blink_t += dt
            duration = .16
            k = self._blink_t / duration
            self._blink = math.sin(math.pi*k) if k < 1.0 else 0.0
            if k >= 1.0:
                self._blink_t = -1.0

        # Bouche : suit le volume quand il parle, se referme sinon.
        speaking = state == "speaking"
        target = min(1.0, self.volume*1.35) if speaking else 0.0
        self._mouth = ease(self._mouth, target, dt, 22.0 if target > self._mouth else 11.0)
        bands = self.bands
        for i in range(8):
            want = bands[i] if speaking else 0.0
            self._mouth_wave[i] = ease(self._mouth_wave[i], want, dt, 16.0)

    # ── Dessin ──────────────────────────────────────────────────────────────
    def paint_orb(self, p: QPainter, cx: float, cy: float, radius: float, t: float) -> None:
        pal = self.palette_live
        energy = min(1.0, self.energy)
        wire, hot = pal["wire"], pal["hot"]
        p.save()

        glow = radius*(2.4 + .25*self.volume)
        self._glow_dst.setRect(cx-glow*.5, cy-glow*.5, glow, glow)
        p.drawPixmap(self._glow_dst,
                     self._glow_sprites.get(self.visual_state, self._glow_sprites["idle"]),
                     self._glow_src)

        # Contour du visage : ovale fin + arc de balayage qui tourne.
        hw, hh = radius*.92, radius*1.12
        self._head_rect.setRect(cx-hw, cy-hh, hw*2, hh*2)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(with_alpha(wire, .28 + .22*energy), 1.4))
        p.drawEllipse(self._head_rect)
        sw, sh = hw*1.08, hh*1.06
        self._scan_rect.setRect(cx-sw, cy-sh, sw*2, sh*2)
        p.setPen(QPen(with_alpha(hot, .35 + .35*self.volume), 2.0,
                      Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(self._scan_rect, int(-self._phase*16*70*self.spin) % 5760, 900)

        # Yeux.
        eye_dx, eye_y = radius*.36, cy - radius*.18
        eye_w = radius*.30
        eye_h = radius*.34*max(.06, self._open*(1.0-self._blink))
        pupil_r = radius*.075
        gx, gy = self._gaze_x*eye_w*.28, self._gaze_y*eye_h*.30
        eye_pen = QPen(with_alpha(wire, .80 + .20*energy), 2.2)
        for side in (-1, 1):
            ex = cx + side*eye_dx
            self._eye_rect.setRect(ex-eye_w*.5, eye_y-eye_h*.5, eye_w, eye_h)
            p.setPen(eye_pen)
            p.setBrush(with_alpha(pal["core"], .10 + .08*energy))
            p.drawRoundedRect(self._eye_rect, eye_w*.5, eye_h*.5)
            if eye_h > pupil_r*1.6:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(with_alpha(hot, .95))
                r = min(pupil_r, eye_h*.38)
                p.drawEllipse(QPointF(ex+gx, eye_y+gy), r, r)

            # Sourcil : côté intérieur relevé ou abaissé selon l'inclinaison.
            by = eye_y - radius*(.30 + .14*self._brow)
            inner = by + radius*.10*self._tilt
            outer = by - radius*.03
            x_in, x_out = ex - side*eye_w*.55, ex + side*eye_w*.60
            self._path.clear()
            self._path.moveTo(x_out, outer)
            self._path.quadTo(ex, by - radius*.06, x_in, inner)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(with_alpha(wire, .70), 2.4, Qt.PenStyle.SolidLine,
                          Qt.PenCapStyle.RoundCap))
            p.drawPath(self._path)

        # Bouche : courbe de sourire, qui s'ouvre en onde quand il parle.
        mw, my = radius*.42, cy + radius*.48
        smile = self._smile*radius*.14
        opening = self._mouth*radius*.26
        self._path.clear()
        self._path.moveTo(cx-mw, my)
        self._path.cubicTo(cx-mw*.45, my+smile, cx+mw*.45, my+smile, cx+mw, my)
        if opening > 1.0:
            # Lèvre inférieure : une onde portée par les bandes.
            wave = self._mouth_wave
            steps = 16
            last = len(wave) - 1
            for i in range(steps, -1, -1):
                f = i/steps
                x = cx - mw + 2*mw*f
                env = math.sin(math.pi*f)
                pos = f*last
                k = min(last-1, int(pos))
                band = wave[k] + (wave[k+1]-wave[k])*(pos-k)
                y = my + smile*env*1.4 + opening*env*(.65 + .55*band)
                self._path.lineTo(x, y)
            self._path.closeSubpath()
            p.setBrush(with_alpha(pal["core"], .18 + .25*self._mouth))
        else:
            p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(with_alpha(hot, .85), 2.4, Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.drawPath(self._path)
        p.restore()
