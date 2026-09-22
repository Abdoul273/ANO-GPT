from __future__ import annotations

import math
import time

from PyQt6.QtCore import QLineF, QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QBrush, QColor, QFont, QPainter, QPainterPath, QPen, QPolygonF, QRadialGradient,
)


def _rgba(color: QColor, alpha: float) -> QColor:
    a = int(alpha)
    return QColor(color.red(), color.green(), color.blue(),
                  0 if a < 0 else (255 if a > 255 else a))


class _HudPaintMixin:
    """Compose la scène holographique. Chaque calque coûte un nombre fixe de
    primitives Qt (jamais « une par particule ») : le thread Qt partage le
    GIL avec la voix, le rendu doit rester bon marché quoi qu'il arrive."""

    # ══ Sprites ═══════════════════════════════════════════════════════════════
    def _blit(self, p: QPainter, name: str, cx: float, cy: float, r: float,
              opacity: float) -> None:
        if opacity <= 0.01 or r <= 1.0:
            return
        # Sur un dégradé flou, le lissage bilinéaire ne se voit pas mais coûte
        # cher sur une grande surface.
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        p.setOpacity(min(1.0, opacity))
        p.drawPixmap(QRectF(cx - r, cy - r, r * 2, r * 2), self._pm[name],
                     QRectF(0, 0, self._SPRITE, self._SPRITE))
        p.setOpacity(1.0)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

    # ══ Réticule HUD : graduations et arcs de cadre ═══════════════════════════
    def _draw_reticle(self, p: QPainter, cx: float, cy: float, Rs: float, t: float,
                      wire: QColor, hot: QColor, activity: float) -> None:
        """Graduations tournant lentement + deux arcs contrarotatifs.

        Traits de 1 px uniquement : au-delà, Qt quitte son rasteriseur
        cosmétique et une simple ellipse coûte dix fois plus cher.
        """
        outer = Rs * 1.50
        minor, major = [], []
        base = math.radians(self._sweep * 0.25)
        for i in range(60):
            a = base + i * (math.tau / 60.0)
            ca, sa = math.cos(a), math.sin(a)
            length = Rs * (0.055 if i % 5 == 0 else 0.024)
            (major if i % 5 == 0 else minor).append(
                QLineF(cx + ca * outer, cy + sa * outer,
                       cx + ca * (outer - length), cy + sa * (outer - length)))
        p.setPen(QPen(_rgba(wire, 48 + 40 * activity), 1.0))
        p.drawLines(minor)
        p.setPen(QPen(_rgba(hot, 120 + 90 * activity), 1.0))
        p.drawLines(major)

        p.setBrush(Qt.BrushStyle.NoBrush)
        for radius, speed, span, alpha, color in (
            (Rs * 1.44, 9.0, 118, 150, hot),
            (Rs * 1.56, -6.0, 74, 90, wire),
        ):
            rect = QRectF(cx - radius, cy - radius, radius * 2, radius * 2)
            start = (t * speed * (1.0 + 0.8 * activity)) % 360.0
            p.setPen(QPen(_rgba(color, alpha * (0.55 + 0.45 * activity)), 1.0))
            p.drawArc(rect, int(start * 16), int(span * 16))
            p.drawArc(rect, int((start + 180.0) * 16), int(span * 16))

    def _draw_orb_readouts(self, p: QPainter, cx: float, cy: float, Rs: float,
                           wire: QColor, hot: QColor, activity: float) -> None:
        """Balises de ciblage autour de l'orbe, façon console de commandement.

        Elles sont dessinées avec quelques traits et quatre libellés seulement.
        Cela apporte une lecture très nette à l'orbe sans lancer de widgets,
        de blur ou de minuteries supplémentaires pendant la voix.
        """
        if Rs < 70.0:
            return
        radius = Rs * 1.18
        glow = int(90 + 95 * activity)
        p.save()
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(_rgba(wire, glow), 1.0))

        # Quatre bras incomplets : ils cadrent l'arc-reacteur sans former une
        # cage circulaire de plus autour de lui.
        arm, notch = Rs * 0.15, Rs * 0.045
        for x, y, sx, sy in (
            (cx - radius, cy - radius, 1, 1),
            (cx + radius, cy - radius, -1, 1),
            (cx - radius, cy + radius, 1, -1),
            (cx + radius, cy + radius, -1, -1),
        ):
            p.drawLine(QPointF(x, y), QPointF(x + sx * arm, y))
            p.drawLine(QPointF(x, y), QPointF(x, y + sy * arm))
            p.drawLine(QPointF(x + sx * notch, y + sy * notch),
                       QPointF(x + sx * (notch + Rs * 0.075), y + sy * (notch + Rs * 0.075)))

        font = QFont("JetBrains Mono", max(6, min(8, int(Rs * 0.030))), QFont.Weight.DemiBold)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.05)
        p.setFont(font)
        p.setPen(QPen(_rgba(hot, 145 + int(75 * activity)), 1.0))
        top = QRectF(cx - Rs * 0.47, cy - radius - Rs * 0.09, Rs * 0.94, Rs * 0.10)
        p.drawText(top, Qt.AlignmentFlag.AlignCenter, "A.N.O // NEURAL CORE")

        p.setPen(QPen(_rgba(wire, 120 + int(70 * activity)), 1.0))
        bottom = QRectF(cx - Rs * 0.50, cy + radius + Rs * 0.01, Rs, Rs * 0.10)
        p.drawText(bottom, Qt.AlignmentFlag.AlignCenter, f"{self._ws.upper()} // LINK")
        # Les deux marqueurs de côté sont des traits courts : les textes ne
        # se battent pas avec les panneaux de télémétrie voisins.
        marker_y = cy
        p.drawLine(QPointF(cx - radius - Rs * 0.13, marker_y), QPointF(cx - radius - Rs * 0.025, marker_y))
        p.drawLine(QPointF(cx + radius + Rs * 0.025, marker_y), QPointF(cx + radius + Rs * 0.13, marker_y))
        p.setBrush(QBrush(_rgba(hot, 200)))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRect(QRectF(cx - Rs * 0.055, cy - radius - 3, Rs * 0.11, 2))
        p.restore()

    # ══ Spectre radial : 48 barres pilotées par les 8 bandes FFT ══════════════
    def _draw_spectrum(self, p: QPainter, cx: float, cy: float, Rs: float,
                       core: QColor, hot: QColor) -> None:
        spec = self._spec
        if max(spec) < 0.03:
            return
        n = len(spec)
        r0 = Rs * 1.08
        reach = Rs * 0.26
        base = math.radians(self._sweep * 0.12)
        dim, bright = [], []
        for i, value in enumerate(spec):
            if value < 0.02:
                continue
            a = base + i * (math.tau / n)
            ca, sa = math.cos(a), math.sin(a)
            r1 = r0 + reach * value
            # Barre de 2 px : deux traits cosmétiques décalés d'un pixel
            # perpendiculairement, bien moins chers qu'un trait épais.
            ox, oy = -sa * 0.5, ca * 0.5
            lines = bright if value > 0.45 else dim
            lines.append(QLineF(cx + ca * r0 - ox, cy + sa * r0 - oy, cx + ca * r1 - ox, cy + sa * r1 - oy))
            lines.append(QLineF(cx + ca * r0 + ox, cy + sa * r0 + oy, cx + ca * r1 + ox, cy + sa * r1 + oy))
        if dim:
            p.setPen(QPen(_rgba(core, 130), 1.0))
            p.drawLines(dim)
        if bright:
            p.setPen(QPen(_rgba(hot, 220), 1.0))
            p.drawLines(bright)

    # ══ Nuage de photons ══════════════════════════════════════════════════════
    def _draw_particle_cloud(self, p: QPainter, m, cx: float, cy: float,
                             radius: float, core: QColor, wire: QColor,
                             hot: QColor) -> None:
        """Nuage sans noyau, rendu en lots : trois `drawPoints`, pas un
        `drawEllipse` par photon."""
        if self._clock_display_active:
            self._draw_clock_particle_formation(p, cx, cy, radius, core, wire, hot)
            return

        visible_budget = min(len(self._particles), self._particle_budget())
        projected = self._project(
            [(pt["x"], pt["y"], pt["z"]) for pt in self._particles[:visible_budget]],
            m, cx, cy, radius,
        )
        self._particle_screen = projected
        self._draw_particle_filaments(p, projected, wire, hot)
        self._draw_connection_electrons(p, projected)
        light = self._cloud_live[3]
        buckets: list[list[QPointF]] = [[], [], []]
        for x, y, z in projected:
            depth = (z + 1.25) / 2.5
            depth = 0.0 if depth < 0.0 else (0.999 if depth > 0.999 else depth)
            buckets[int(depth * 3.0)].append(QPointF(x, y))
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        scale = self._PARTICLE_POINT_SCALE
        for i, points in enumerate(buckets):
            if not points:
                continue
            depth = (i + 0.5) / 3.0
            poly = QPolygonF(points)
            color = hot if i == 2 else core
            alpha = (95 + 160 * depth) * light
            if i == 2:
                # Aura douce des photons de façade : un seul tracé large.
                p.setPen(QPen(_rgba(core, 48 * light), 2.8 * scale,
                              Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.drawPoints(poly)
            # Largeur plafonnée à 3 px : au-delà, Qt remplit une ellipse par
            # photon et le nuage coûte trois fois plus.
            p.setPen(QPen(_rgba(color, alpha), min(3.0, (1.0 + depth * 0.45) * scale),
                          Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawPoints(poly)

    def _draw_particle_filaments(self, p: QPainter, projected, wire: QColor,
                                 hot: QColor) -> None:
        """Segments groupés : quatre chemins, coût constant."""
        if not self._particle_links:
            return
        paths = [QPainterPath() for _ in range(4)]
        used = [False] * 4
        for first, second, closeness, _phase in self._particle_links:
            if first >= len(projected) or second >= len(projected):
                continue
            x1, y1, z1 = projected[first]
            x2, y2, z2 = projected[second]
            length = math.hypot(x2 - x1, y2 - y1)
            if length < 5.0 or length > 72.0:
                continue
            depth = (z1 + z2 + 2.5) / 5.0
            depth = 0.0 if depth < 0.0 else (1.0 if depth > 1.0 else depth)
            bucket = min(3, int((closeness * .65 + depth * .35) * 4.0))
            paths[bucket].moveTo(x1, y1)
            paths[bucket].lineTo(x2, y2)
            used[bucket] = True
        p.setBrush(Qt.BrushStyle.NoBrush)
        for index, path in enumerate(paths):
            if not used[index]:
                continue
            p.setPen(QPen(_rgba(wire, 18 + index * 14), .8))
            p.drawPath(path)

    def _draw_connection_electrons(self, p: QPainter, projected) -> None:
        """Électrons blancs qui parcourent les liaisons actives."""
        count = {"thinking": 4, "speaking": 10, "acting": 12}.get(self._ws, 0)
        if not count or not self._particle_links:
            return
        photons = []
        now = time.monotonic()
        stride = max(1, len(self._particle_links) // count)
        for slot in range(count):
            first, second, _close, phase = self._particle_links[(slot * stride) % len(self._particle_links)]
            if first >= len(projected) or second >= len(projected):
                continue
            x1, y1, _ = projected[first]
            x2, y2, _ = projected[second]
            progress = (now * (1.25 if self._ws == "speaking" else .55) + phase * .19) % 1.0
            photons.append(QPointF(x1 + (x2 - x1) * progress, y1 + (y2 - y1) * progress))
        if not photons:
            return
        p.setPen(QPen(QColor(255, 255, 255, 255), 2.2,
                      Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawPoints(QPolygonF(photons))

    def _draw_clock_particle_formation(self, p: QPainter, cx: float, cy: float,
                                       radius: float, core: QColor, wire: QColor,
                                       hot: QColor) -> None:
        """Dessine HH:MM dans le plan de l'écran, exclusivement en photons.

        La formation ne passe volontairement pas par la matrice 3D de l'orbe :
        une perspective inclinée est spectaculaire mais rend une heure ambiguë.
        """
        segments = self._clock_segments
        if not segments:
            return
        budget = min(len(self._particles), self._CLOCK_PARTICLE_BUDGET)
        elapsed = max(0.0, time.monotonic() - self._clock_particles_started_at)
        duration = max(2.0, self._clock_particles_until - self._clock_particles_started_at)
        # Aspiration en 1,05 s, cadran stable, dissolution sur 1,15 s.
        appear = min(1.0, elapsed / 1.05)
        vanish = min(1.0, max(0.0, (duration - elapsed) / 1.15))
        morph = min(appear, vanish)
        pulse = 0.90 + 0.10 * math.sin(elapsed * 5.8)
        dim_points, bright_points, colon_points = [], [], []
        projected = []
        for index, pt in enumerate(self._particles[:budget]):
            digit, segment = segments[index % len(segments)]
            x, y, _ = self._clock_segment_target(digit, segment, pt["segment_u"])
            # `_clock_segment_target` inverse X pour la caméra 3D ; la vue
            # frontale rétablit le sens gauche → droite.
            x = -x * 1.12
            thickness = math.sin(pt["phase"] * 2.73 + index * 0.17) * 0.030
            if segment in (0, 3, 6):
                y += thickness
            else:
                x += thickness
            if digit == 4:
                spread = 0.018 * (0.35 + 0.65 * math.sin(pt["phase"] + elapsed * 3.0))
                x += math.cos(pt["phase"]) * spread
                y += math.sin(pt["phase"]) * spread
            delay = ((index * 37) % 23) / 23.0 * 0.18
            settle = max(0.0, min(1.0, (morph - delay) / (1.0 - delay)))
            ease = settle * settle * (3.0 - 2.0 * settle)
            seed_r = 0.26 + pt["home_r"] * 0.66
            seed_x = math.cos(pt["phase"]) * seed_r
            seed_y = math.sin(pt["phase"]) * seed_r
            px = cx + (seed_x + (x - seed_x) * ease) * radius * 1.28
            py = cy + (seed_y + (y - seed_y) * ease) * radius * 1.28
            projected.append((px, py, 0.62))
            point = QPointF(px, py)
            if digit == 4:
                colon_points.append(point)
            elif index % 7 == 0:
                bright_points.append(point)
            else:
                dim_points.append(point)
        self._particle_screen = projected

        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        base_alpha = 205 * morph * pulse
        if dim_points:
            poly = QPolygonF(dim_points)
            p.setPen(QPen(_rgba(core, 36 * morph), 6.6,
                          Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawPoints(poly)
            p.setPen(QPen(_rgba(core, base_alpha), 2.55,
                          Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawPoints(poly)
        if bright_points:
            p.setPen(QPen(_rgba(hot, 252 * morph), 4.25,
                          Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawPoints(QPolygonF(bright_points))
        if colon_points:
            p.setPen(QPen(_rgba(wire, 245 * morph), 4.1,
                          Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawPoints(QPolygonF(colon_points))

    def _draw_neural_orb(self, p: QPainter, m, cx: float, cy: float, Rs: float,
                         t: float, core: QColor, halo: QColor, wire: QColor,
                         hot: QColor) -> None:
        """Le volume de photons, confiné dans la sphère."""
        p.save()
        if not self._clock_display_active:
            clip = QPainterPath()
            clip.addEllipse(QPointF(cx, cy), Rs * 1.02, Rs * 1.02)
            p.setClipPath(clip)
        self._draw_particle_cloud(p, m, cx, cy, Rs, core, wire, hot)
        p.restore()

    # ══ Noyau, limbe, ondes ═══════════════════════════════════════════════════
    def _draw_nucleus(self, p: QPainter, cx: float, cy: float, Rs: float, t: float,
                      hot: QColor, activity: float) -> None:
        if self._clock_display_active:
            # Le cadran occupe le centre : aucune lueur ne doit le voiler.
            return
        vol = self._volume
        beat = 0.5 + 0.5 * math.sin(t * self._pal_live["pulse_speed"] * 2.2)
        r = Rs * (0.36 + 0.26 * vol + 0.08 * self._bass + 0.02 * beat)
        self._blit(p, "nucleus", cx, cy, r, 0.60 + 0.40 * activity)
        dot = Rs * (0.028 + 0.030 * vol)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(255, 255, 255, int(150 + 100 * activity))))
        p.drawEllipse(QPointF(cx, cy), dot, dot)

    def _draw_limb(self, p: QPainter, cx: float, cy: float, Rs: float,
                   core: QColor, wire: QColor, activity: float) -> None:
        """Fresnel circulaire léger + lunette pointillée en rotation : la
        sphère se lit sans cerceau chrome."""
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(_rgba(core, 30 + 44 * activity), 1.0))
        p.drawEllipse(QPointF(cx, cy), Rs * 0.995, Rs * 0.995)
        p.setPen(QPen(_rgba(core, 12 + 18 * activity), 1.0))
        p.drawEllipse(QPointF(cx, cy), Rs * 1.006, Rs * 1.006)
        pen = QPen(_rgba(wire, 70 + 60 * activity), 1.0, Qt.PenStyle.CustomDashLine)
        pen.setDashPattern([6.0, 9.0])
        pen.setDashOffset(-self._sweep * 0.9)
        p.setPen(pen)
        p.drawEllipse(QPointF(cx, cy), Rs * 1.040, Rs * 1.040)

    def _draw_shockwaves(self, p: QPainter, cx: float, cy: float, Rs: float,
                         hot: QColor) -> None:
        """Ondes émises par la voix : nées d'un choc de basses, elles
        s'étendent puis se dissolvent au-delà du réticule."""
        if not self._waves:
            return
        p.setBrush(Qt.BrushStyle.NoBrush)
        for age in self._waves:
            radius = Rs * (1.0 + age * 0.62)
            alpha = 170 * (1.0 - age) ** 1.6
            if alpha < 4:
                continue
            p.setPen(QPen(_rgba(hot, alpha), 1.0))
            p.drawEllipse(QPointF(cx, cy), radius, radius)
            p.setPen(QPen(_rgba(hot, alpha * 0.45), 1.0))
            p.drawEllipse(QPointF(cx, cy), radius + 1.0, radius + 1.0)

    # ══ Composition ═══════════════════════════════════════════════════════════
    def paintEvent(self, _):
        W, H = self.width(), self.height()
        if W <= 0 or H <= 0:
            return
        started = time.monotonic()
        if self._cache_key != self._sprite_key(W, H):
            self._build_cache(W, H)

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        t = started - self._t0
        live = self._pal_live
        spd = live["pulse_speed"]
        core, halo, wire, hot = live["core"], live["halo"], live["wire"], live["hot"]
        energy = self._energy
        activity = min(1.0, energy * 0.6 + self._volume * 0.7)

        # Léger flottement organique : l'orbe dérive, jamais rivé au pixel.
        bob_x = min(W, H) * 0.006 * (math.sin(t * 0.17) + 0.4 * math.sin(t * 0.41 + 1.3))
        bob_y = min(W, H) * 0.005 * (math.sin(t * 0.13 + 0.7) + 0.4 * math.sin(t * 0.29 + 2.1))
        cx, cy = W / 2.0 + bob_x, H / 2.0 + bob_y

        # Respiration à deux fréquences, plus organique qu'un sinus seul.
        breathe = 0.60 * math.sin(t * spd * 1.1) + 0.40 * math.sin(t * 0.37 * spd + 2.0)
        R = min(W, H) * self._ORB_SCALE
        Rs = R * (1.0 + 0.022 * breathe + self._volume * 0.09 + self._shockwave * 0.04)
        m = self._matrix(self._yaw, self._pitch, self._roll)

        # 1. Fond et aura ambiante, cuits ensemble (voile translucide si une
        #    photo est derrière) ; la voix ajoute une lueur vivante par-dessus.
        p.drawPixmap(0, 0, self._pm["scene"])
        breath_glow = 0.10 * energy + 0.50 * self._volume + 0.25 * self._shockwave
        if breath_glow > 0.05:
            self._blit(p, "glow", cx, cy, Rs * 1.15, min(0.90, breath_glow))
        # 2. Réticule et spectre : le cadre HUD.
        self._draw_reticle(p, cx, cy, Rs, t, wire, hot, activity)
        self._draw_orb_readouts(p, cx, cy, Rs, wire, hot, activity)
        self._draw_spectrum(p, cx, cy, Rs, core, hot)
        # 3. Le volume de photons. Les fils elliptiques orbitaux ont été
        # retirés : les anneaux circulaires du noyau et le réticule restent.
        self._draw_neural_orb(p, m, cx, cy, Rs, t, core, halo, wire, hot)
        # 4. Noyau, limbe et ondes vocales.
        self._draw_nucleus(p, cx, cy, Rs, t, hot, activity)
        self._draw_limb(p, cx, cy, Rs, core, wire, activity)
        self._draw_shockwaves(p, cx, cy, Rs, hot)
        # 6. Indicateurs : vision continue, retour gestuel.
        if self._continuous_vision_active:
            self._draw_neon_eye_indicator(p, cx, cy, Rs, hot, wire, core)
        self._draw_holographic_gesture_badge(p, cx, cy, Rs, hot, wire, core)

        p.end()
        self._note_paint_cost((time.monotonic() - started) * 1000.0)

    # ══ Indicateurs ═══════════════════════════════════════════════════════════
    def _draw_neon_eye_indicator(
        self, p: QPainter, cx: float, cy: float, hud_r: float, hot: QColor, wire: QColor, core: QColor
    ) -> None:
        """Œil néon discret, quart supérieur droit : la vision continue est active."""
        now = time.monotonic()
        pulse = 0.82 + 0.18 * math.sin(now * 3.8)
        alpha = int(220 * pulse)
        eye_x = cx + hud_r * 0.92
        eye_y = cy - hud_r * 0.92
        badge_r = min(hud_r * 0.16, 24.0)

        p.save()
        glow = QRadialGradient(eye_x, eye_y, badge_r * 1.5)
        glow.setColorAt(0.0, QColor(0, 240, 255, int(70 * pulse)))
        glow.setColorAt(0.7, _rgba(wire, 30 * pulse))
        glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(glow))
        p.drawEllipse(QPointF(eye_x, eye_y), badge_r * 1.5, badge_r * 1.5)

        p.setPen(QPen(_rgba(wire, 160 * pulse), 1.0, Qt.PenStyle.DashLine))
        p.setBrush(QBrush(QColor(4, 12, 22, int(190 * pulse))))
        p.drawEllipse(QPointF(eye_x, eye_y), badge_r, badge_r)

        eye_w, eye_h = badge_r * 0.68, badge_r * 0.38
        path = QPainterPath()
        p_left, p_right = QPointF(eye_x - eye_w, eye_y), QPointF(eye_x + eye_w, eye_y)
        path.moveTo(p_left)
        path.quadTo(QPointF(eye_x, eye_y - eye_h), p_right)
        path.quadTo(QPointF(eye_x, eye_y + eye_h), p_left)
        p.setPen(QPen(QColor(0, 240, 255, alpha), 1.6))
        p.setBrush(QBrush(QColor(0, 180, 255, int(45 * pulse))))
        p.drawPath(path)

        iris_r = badge_r * 0.26
        iris = QRadialGradient(eye_x, eye_y, iris_r)
        iris.setColorAt(0.0, QColor(255, 255, 255, alpha))
        iris.setColorAt(0.4, QColor(0, 255, 220, alpha))
        iris.setColorAt(1.0, QColor(0, 140, 255, int(180 * pulse)))
        p.setPen(QPen(QColor(0, 255, 255, alpha), 1.0))
        p.setBrush(QBrush(iris))
        p.drawEllipse(QPointF(eye_x, eye_y), iris_r, iris_r)

        pupil_r = badge_r * 0.11
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(255, 255, 255, int(250 * pulse))))
        p.drawEllipse(QPointF(eye_x, eye_y), pupil_r, pupil_r)

        p.setPen(QPen(_rgba(wire, 140 * pulse), 1.0))
        p.drawLine(QLineF(eye_x - badge_r - 2, eye_y, eye_x - badge_r + 3, eye_y))
        p.drawLine(QLineF(eye_x + badge_r - 3, eye_y, eye_x + badge_r + 2, eye_y))

        f = p.font()
        f.setPointSize(max(5, int(badge_r * 0.28)))
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(0, 240, 255, int(210 * pulse)))
        p.drawText(QRectF(eye_x - badge_r, eye_y + badge_r * 0.95, badge_r * 2, badge_r * 0.55),
                   Qt.AlignmentFlag.AlignCenter, "LIVE")
        p.restore()

    def _draw_holographic_gesture_badge(
        self, p: QPainter, cx: float, cy: float, hud_r: float, hot: QColor, wire: QColor, core: QColor
    ) -> None:
        """Badge holographique du geste reconnu, au centre de l'orbe."""
        icon = self._gesture_icon
        expires = self._gesture_expires
        now = time.monotonic()
        if not icon or now >= expires:
            return
        fade = min(1.0, (expires - now) / 0.35)
        badge_r = min(hud_r * 0.36, 68.0)

        p.save()
        bg = QRadialGradient(cx, cy, badge_r)
        bg.setColorAt(0.0, QColor(0, 15, 28, int(210 * fade)))
        bg.setColorAt(0.7, QColor(core.red() // 3, core.green() // 3, core.blue() // 3, int(150 * fade)))
        bg.setColorAt(1.0, _rgba(wire, 40 * fade))
        p.setPen(QPen(_rgba(wire, 220 * fade), 1.5))
        p.setBrush(QBrush(bg))
        p.drawEllipse(QPointF(cx, cy), badge_r, badge_r)

        p.setPen(QPen(_rgba(hot, 150 * fade), 1.0, Qt.PenStyle.DashLine))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(cx, cy), badge_r * 0.84, badge_r * 0.84)

        glyph_map = {
            "play_pause": "⏯", "play": "▶", "pause": "⏸", "mute": "🤫", "shh": "🤫",
            "volume": "🔊", "next_track": "⏭", "next": "⏭",
        }
        glyph = glyph_map.get(str(icon).lower(), str(icon))
        f = p.font()
        f.setPointSize(max(14, int(badge_r * 0.48)))
        f.setBold(True)
        p.setFont(f)
        p.setPen(_rgba(hot, 255 * fade))
        p.drawText(QRectF(cx - badge_r, cy - badge_r * 0.72, badge_r * 2, badge_r * 1.05),
                   Qt.AlignmentFlag.AlignCenter, glyph)

        label = self._gesture_label
        if label:
            lf = p.font()
            lf.setPointSize(max(7, int(badge_r * 0.15)))
            lf.setBold(True)
            p.setFont(lf)
            p.setPen(_rgba(wire, 240 * fade))
            p.drawText(QRectF(cx - badge_r, cy + badge_r * 0.30, badge_r * 2, badge_r * 0.50),
                       Qt.AlignmentFlag.AlignCenter, str(label).upper())
        p.restore()
