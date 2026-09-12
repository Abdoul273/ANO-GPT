from __future__ import annotations

import math
import random
import time

from PyQt6.QtCore import QLineF, QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QBrush, QColor, QLinearGradient, QPainter, QPainterPath, QPen, QPolygonF,
    QRadialGradient,
)


class _HudPaintMixin:
    def _spawn_spark(self, core_r: float):
        ang = random.uniform(0, 2 * math.pi)
        self._sparks.append({
            "ang": ang, "dist": core_r * random.uniform(0.55, 0.85),
            "spd": core_r * random.uniform(1.15, 2.35),
            "curl": random.uniform(-0.9, 0.9),
            "life": 0.0, "max_life": random.uniform(0.55, 1.15),
            "size": random.uniform(1.0, 2.6),
        })

    def _spawn_arc(self, cx: float, cy: float, r: float):
        ang = random.uniform(0, 2 * math.pi)
        r0, r1 = r * 0.10, r * (0.94 + random.uniform(0, 0.30))
        segs = 6
        pts = []
        for j in range(segs + 1):
            tt = j / segs
            rr = r0 + (r1 - r0) * tt
            spread = 1 - abs(tt - 0.5) * 2
            a = ang + random.uniform(-1, 1) * 0.20 * spread
            jit = spread * r * 0.11
            pts.append(QPointF(cx + rr * math.cos(a) + random.uniform(-jit, jit),
                               cy + rr * math.sin(a) + random.uniform(-jit, jit)))
        self._arcs.append({"pts": pts, "life": 0.26, "max_life": 0.26,
                           "w": random.uniform(1.0, 2.2)})

    # ══ Noyau plasma turbulent ═══════════════════════════════════════════════
    def _plasma_polygon(self, cx: float, cy: float, r: float, t: float) -> QPolygonF:
        """Contour du noyau : somme d'harmoniques angulaires basses.

        Les phases sont globales (pas par sommet) : la surface ondule et reste
        lisse, au lieu du bruit dentelé qu'on obtient avec un aléa par point.
        """
        poly = QPolygonF()
        n = 56
        amp = (0.45 + 0.55 * self._energy) * (1.0 + self._volume * 0.5)
        harms = self._core_harm
        for i in range(n):
            ang = (i / n) * 6.28318
            wob = 0.0
            for k, a, ph, sp in harms:
                wob += a * math.sin(k * ang + ph + t * sp)
            rr = r * (1.0 + wob * amp)
            poly.append(QPointF(cx + rr * math.cos(ang), cy + rr * math.sin(ang)))
        return poly

    # ══ Tracé 3D avec dégradé de profondeur ══════════════════════════════════
    def _stroke_depth(self, p: "QPainter", proj_curves, col: QColor, width: float,
                      front: bool, a_min: int, a_max: int):
        """Trace les segments de la moitié `front`, groupés en niveaux d'alpha
        selon la profondeur — un seul drawPath par niveau."""
        nb = self._ZBUCKETS
        paths = [QPainterPath() for _ in range(nb)]
        used = [False] * nb
        for proj in proj_curves:
            prev = proj[0]
            for cur in proj[1:]:
                zm = prev[2] + cur[2]
                if (zm >= 0) == front:
                    b = int((zm * 0.25 + 0.5) * nb)
                    b = nb - 1 if b >= nb else (0 if b < 0 else b)
                    paths[b].moveTo(prev[0], prev[1])
                    paths[b].lineTo(cur[0], cur[1])
                    used[b] = True
                prev = cur
        p.setBrush(Qt.BrushStyle.NoBrush)
        r, g, b_ = col.red(), col.green(), col.blue()
        for i in range(nb):
            if not used[i]:
                continue
            f = (i + 0.5) / nb
            p.setPen(QPen(QColor(r, g, b_, int(a_min + (a_max - a_min) * f ** 1.6)),
                          width * (0.6 + 0.7 * f)))
            p.drawPath(paths[i])

    def _blit_glow(self, p: "QPainter", cx: float, cy: float, r: float, opacity: float):
        if opacity <= 0.01 or r <= 1:
            return
        # Interpolation rapide : sur un dégradé flou, le lissage bilinéaire ne
        # se voit pas mais coûte cher sur une surface de cette taille.
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        p.setOpacity(min(1.0, opacity))
        p.drawPixmap(QRectF(cx - r, cy - r, r * 2, r * 2), self._pm["glow"],
                     QRectF(0, 0, 256, 256))
        p.setOpacity(1.0)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

    def _draw_particle_cloud(self, p: "QPainter", m, cx: float, cy: float,
                             radius: float, core: QColor, wire: QColor,
                             hot: QColor) -> None:
        """Nuage dense sans noyau, rendu en lots pour rester ultra-fluide.

        Les 2 000 points sont regroupés en cinq plans de profondeur : au lieu
        de faire deux ``drawEllipse`` par particule, QPainter ne reçoit que
        cinq ``drawPoints``. C'est le point important sur cette machine où
        l'animation UI partage le GIL avec l'audio.
        """
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
        buckets = [[] for _ in range(3)]
        # À cette densité, l'anticrénelage de chaque photon est imperceptible
        # mais très coûteux. Le désactiver localement garde le mouvement fluide.
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        for particle_index, (x, y, z) in enumerate(projected):
            depth = max(0.0, min(1.0, (z + 1.25) / 2.5))
            buckets[min(2, int(depth * 3.0))].append(QPointF(x, y))
        # SourceOver conserve des photons colorés nets mais évite le coût très
        # élevé du blend additif quand 3 000 points se croisent dans une figure.
        # C'est ce qui permet d'augmenter visiblement la densité sans voler le
        # temps du thread audio partagé.
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        # PointsMaterial d'orb.ts est une passe unique additive. La double
        # passe « aura + coeur » n'était pas dans la référence et coûtait deux
        # fois trop cher sur QPainter.
        scale = self._PARTICLE_POINT_SCALE
        for i, points in enumerate(buckets):
            if not points:
                continue
            depth = (i + 0.5) / len(buckets)
            poly = QPolygonF(points)
            color = hot if i == len(buckets) - 1 else core
            alpha = int((72 + 138 * depth) * light)
            p.setPen(QPen(
                QColor(color.red(), color.green(), color.blue(), min(255, alpha)),
                (0.85 + depth * 0.75) * scale,
                Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
            ))
            p.drawPoints(poly)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

    def _draw_particle_filaments(self, p: "QPainter", projected, wire: QColor,
                                 hot: QColor) -> None:
        """Segments groupés, équivalent QPainter du LineSegments WebGL."""
        if not self._particle_links:
            return
        p.save()
        # Un QLinearGradient par trait saturait le CPU et pouvait faire tomber
        # Qt. orb.ts emploie un LineBasicMaterial uniforme : quatre chemins
        # groupés reproduisent exactement ce principe à coût constant.
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        paths = [QPainterPath() for _ in range(4)]
        used = [False] * len(paths)
        for first, second, closeness, phase in self._particle_links:
            if first >= len(projected) or second >= len(projected):
                continue
            x1, y1, z1 = projected[first]
            x2, y2, z2 = projected[second]
            # Les voisins 3D qui se recouvrent à l'écran ne font pas un trait
            # brillant inutile ; l'effet reste léger, rare et lisible.
            length = math.hypot(x2 - x1, y2 - y1)
            if length < 5.0 or length > 72.0:
                continue
            depth = max(0.0, min(1.0, (z1 + z2 + 2.5) / 5.0))
            bucket = min(3, int((closeness * .65 + depth * .35) * 4.0))
            paths[bucket].moveTo(x1, y1)
            paths[bucket].lineTo(x2, y2)
            used[bucket] = True
        for index, path in enumerate(paths):
            if not used[index]:
                continue
            alpha = 16 + index * 10  # opacity LineBasicMaterial ≈ 0.12
            p.setPen(QPen(QColor(wire.red(), wire.green(), wire.blue(), alpha), .72))
            p.drawPath(path)
        p.restore()

    def _draw_connection_electrons(self, p: "QPainter", projected) -> None:
        """Les électrons blancs d'orb.ts parcourent les liaisons actives."""
        count = {"thinking": 3, "speaking": 10, "acting": 10}.get(self._ws, 0)
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
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.setPen(QPen(QColor(255, 255, 255, 255), 2.2,
                      Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawPoints(QPolygonF(photons))
        p.restore()

    def _draw_clock_particle_formation(self, p: "QPainter", cx: float, cy: float,
                                       radius: float, core: QColor, wire: QColor,
                                       hot: QColor) -> None:
        """Dessine HH:MM dans le plan de l'écran, exclusivement en photons.

        La formation ne passe volontairement pas par la matrice 3D de l'orbe :
        une perspective inclinée est spectaculaire mais rend une heure ambiguë.
        Le reste de l'orbe continue de respirer et tourner autour de ce cadran.
        """
        segments = self._clock_segments
        if not segments:
            return
        budget = min(len(self._particles), self._CLOCK_PARTICLE_BUDGET)
        elapsed = max(0.0, time.monotonic() - self._clock_particles_started_at)
        duration = max(2.0, self._clock_particles_until - self._clock_particles_started_at)
        # Trois temps très lisibles : aspiration en 1,05 s, cadran stable,
        # puis dissolution durant les 1,15 dernières secondes.
        appear = min(1.0, elapsed / 1.05)
        vanish = min(1.0, max(0.0, (duration - elapsed) / 1.15))
        morph = min(appear, vanish)
        pulse = 0.90 + 0.10 * math.sin(elapsed * 5.8)
        dim_points, bright_points, colon_points = [], [], []
        projected = []
        for index, pt in enumerate(self._particles[:budget]):
            digit, segment = segments[index % len(segments)]
            x, y, _ = self._clock_segment_target(digit, segment, pt["segment_u"])
            # `_clock_segment_target` inverse X pour la caméra 3D. Cette vue
            # est volontairement frontale pour rendre HH : MM lisible : on
            # rétablit donc ici le sens naturel gauche → droite.
            x = -x * 1.12
            # Épaisseur déterministe du trait : chaque segment devient un
            # ruban de photons, plutôt qu'une simple ligne de points. La
            # direction transverse dépend de l'orientation du segment.
            thickness = math.sin(pt["phase"] * 2.73 + index * 0.17) * 0.030
            if segment in (0, 3, 6):
                y += thickness
            else:
                x += thickness
            # Une très faible dispersion garde les deux points ':' vivants au
            # lieu de les réduire à un pixel unique.
            if digit == 4:
                spread = 0.018 * (0.35 + 0.65 * math.sin(pt["phase"] + elapsed * 3.0))
                x += math.cos(pt["phase"]) * spread
                y += math.sin(pt["phase"]) * spread
            # Vague de convergence : chaque ruban arrive avec un micro-décalage
            # déterministe. C'est fluide, spectaculaire, et sans allocation.
            delay = ((index * 37) % 23) / 23.0 * 0.18
            settle = max(0.0, min(1.0, (morph - delay) / (1.0 - delay)))
            ease = settle * settle * (3.0 - 2.0 * settle)
            # Point de départ orbital stable, puis convergence lissée vers le
            # segment qui lui est assigné.
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

        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        base_alpha = int(205 * morph * pulse)
        if dim_points:
            # Aura obtenue avec les mêmes photons, dessinés en premier et en
            # transparence : l'heure gagne du relief sans fil ni glyphe Qt.
            p.setPen(QPen(QColor(core.red(), core.green(), core.blue(), int(36 * morph)), 6.6,
                          Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawPoints(QPolygonF(dim_points))
        if dim_points:
            p.setPen(QPen(QColor(core.red(), core.green(), core.blue(), base_alpha), 2.55,
                          Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawPoints(QPolygonF(dim_points))
        if bright_points:
            p.setPen(QPen(QColor(hot.red(), hot.green(), hot.blue(), int(252 * morph)), 4.25,
                          Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawPoints(QPolygonF(bright_points))
        if colon_points:
            p.setPen(QPen(QColor(wire.red(), wire.green(), wire.blue(), int(245 * morph)), 4.1,
                          Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawPoints(QPolygonF(colon_points))
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    def _draw_surface_mesh(self, p: "QPainter", m, cx: float, cy: float, radius: float,
                           t: float, wire: QColor, hot: QColor, activity: float) -> None:
        """Maillage triangulé de la coque : la signature de la référence."""
        (a0, a1, a2), (b0, b1, b2), (c0, c1, c2) = m
        projected = []
        motion = min(1.0, max(self._volume, self._bass, self._mid, self._treble))
        mode_speed, mode_wobble = {
            "idle": (.36, .009), "listening": (1.15, .018),
            "thinking": (1.75, .031), "speaking": (1.55, .026),
            "acting": (2.15, .038), "error": (1.35, .022),
        }.get(self._ws, (.5, .01))
        audio_wobble = motion * (.040 if self._ws in {"listening", "speaking"} else .014)
        for x, y, z, phase in self._mesh_nodes:
            # Respiration de surface minuscule, non-uniforme : réseau vivant.
            ripple = 1.0 + (mode_wobble + audio_wobble) * math.sin(
                t * mode_speed * (1.0 + motion * 2.4) + phase * 1.9
            )
            x1, y1, z1 = x * ripple, y * ripple, z * ripple
            rz = c0 * x1 + c1 * y1 + c2 * z1
            scale = self._CAM / (self._CAM - rz)
            projected.append((cx + (a0 * x1 + a1 * y1 + a2 * z1) * radius * scale,
                              cy + (b0 * x1 + b1 * y1 + b2 * z1) * radius * scale, rz))
        # Trois lots seulement : arrière discret, milieu, façade brillante.
        line_buckets = [[], [], []]
        for left, right in self._mesh_links:
            x1, y1, z1 = projected[left]
            x2, y2, z2 = projected[right]
            depth = (z1 + z2) * .5
            bucket = 0 if depth < -.25 else (1 if depth < .30 else 2)
            line_buckets[bucket].append(QLineF(x1, y1, x2, y2))
        if line_buckets[2]:
            p.setPen(QPen(QColor(wire.red(), wire.green(), wire.blue(), 26), 2.6,
                          Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLines(line_buckets[2])
        for i, lines in enumerate(line_buckets):
            # Coque volontairement discrète : la profondeur vient des noeuds,
            # non d'une cage filaire qui prendrait le dessus sur l'orbe.
            # Coque nettement plus lisible que l'ancienne cage fantôme : la
            # référence montre un réseau triangulé franc, pas une brume.
            alpha = (18, 52, int(105 + activity * 60))[i]
            width = (.30, .48, .74)[i]
            p.setPen(QPen(QColor(wire.red(), wire.green(), wire.blue(), alpha), width))
            p.drawLines(lines)
        # Les noeuds proches deviennent plus blancs et légèrement plus grands.
        node_buckets = [[], [], []]
        for x, y, z in projected:
            node_buckets[0 if z < -.25 else (1 if z < .30 else 2)].append(QPointF(x, y))
        p.setPen(Qt.PenStyle.NoPen)
        for i, points in enumerate(node_buckets):
            if not points:
                continue
            alpha = (86, 160, int(225 + activity * 30))[i]
            p.setPen(QPen(QColor(hot.red(), hot.green(), hot.blue(), alpha), (1.05, 1.55, 2.10)[i],
                          Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawPoints(QPolygonF(points))
        # Quelques synapses dominantes, avec un halo local plutôt qu'un bloom
        # global qui masquerait le réseau.
        p.setPen(Qt.PenStyle.NoPen)
        for index in range(7, len(projected), 29):
            x, y, z = projected[index]
            if z < .08:
                continue
            glow = 3.2 + 2.5 * math.sin(t * mode_speed + index) ** 2 + motion * 1.8
            p.setBrush(QBrush(QColor(wire.red(), wire.green(), wire.blue(), 42)))
            p.drawEllipse(QPointF(x, y), glow * 2.4, glow * 2.4)
            p.setBrush(QBrush(QColor(245, 253, 255, 235)))
            p.drawEllipse(QPointF(x, y), glow * .42, glow * .42)

    # ══ Socle holographique : le projecteur sous l'orbe ══════════════════════
    def _floor_geometry(self, cy: float, Rs: float) -> tuple[float, float, float]:
        """Sol du projecteur, borné pour ne jamais sortir du widget.

        Le socle est ce qui donne l'échelle : plutôt que de le laisser
        déborder sur un écran large, on le remonte et on le resserre.
        """
        flat = 0.19
        h = float(self.height())
        by = min(cy + Rs * 1.28, h - Rs * 0.34)
        base = min(Rs * 2.15, max(Rs * 1.10, (h - 2.0 - by) / flat))
        return by, base, flat

    def _draw_projector_base(self, p: "QPainter", cx: float, cy: float, Rs: float,
                             t: float, core: QColor, halo: QColor, hot: QColor,
                             activity: float) -> None:
        """Anneaux concentriques posés au sol, vus en perspective rasante.

        Quelques primitives seulement : le socle donne l'échelle et la
        profondeur de la scène sans coûter un seul calcul par point.
        """
        by, base, flat = self._floor_geometry(cy, Rs)
        pulse = 0.5 + 0.5 * math.sin(t * (0.9 + activity * 1.6))

        # Nappe lumineuse au sol : le sprite de corona déjà en cache, écrasé.
        # Un dégradé radial de cette taille coûterait un plein écran de pixels
        # calculés à chaque image — inacceptable à côté du moteur vocal.
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        p.setOpacity(0.30 + 0.22 * activity)
        p.drawPixmap(QRectF(cx - base, by - base * flat, base * 2, base * 2 * flat),
                     self._pm["glow"], QRectF(0, 0, 256, 256))
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        # Anneaux et graduations : un seul blit du socle cuit au cache.
        floor = self._pm["floor"]
        p.setOpacity(0.62 + 0.38 * activity)
        p.drawPixmap(QRectF(cx - base, by - base * flat, base * 2, base * 2 * flat),
                     floor, QRectF(0, 0, floor.width(), floor.height()))
        p.setOpacity(1.0)

        # Seule l'onde qui s'échappe du centre reste calculée : elle bouge.
        p.save()
        p.translate(cx, by)
        p.scale(1.0, flat)
        p.setBrush(Qt.BrushStyle.NoBrush)
        wave = base * (0.28 + 0.72 * ((t * 0.35) % 1.0))
        fade = int(150 * (1.0 - ((t * 0.35) % 1.0)) * (0.4 + 0.6 * activity))
        if fade > 4:
            p.setPen(QPen(QColor(hot.red(), hot.green(), hot.blue(), fade), 1.6))
            p.drawEllipse(QPointF(0.0, 0.0), wave, wave)
        p.restore()

        # Point de fuite incandescent au centre du socle.
        spot = Rs * (0.10 + 0.03 * pulse)
        gs = QRadialGradient(cx, by, spot * 2.4)
        gs.setColorAt(0.00, QColor(255, 255, 255, 210))
        gs.setColorAt(0.18, QColor(hot.red(), hot.green(), hot.blue(), 120))
        gs.setColorAt(0.55, QColor(core.red(), core.green(), core.blue(), 40))
        gs.setColorAt(1.00, QColor(core.red(), core.green(), core.blue(), 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(gs))
        p.drawEllipse(QPointF(cx, by), spot * 2.4, spot * 2.4 * 0.42)

    # ══ Colonne de lumière : le lien orbe ↔ socle ════════════════════════════
    def _draw_light_column(self, p: "QPainter", cx: float, cy: float, Rs: float,
                           t: float, core: QColor, hot: QColor, activity: float) -> None:
        """Faisceau vertical + pluie de photons entre la sphère et le sol."""
        by, _base, _flat = self._floor_geometry(cy, Rs)
        top = max(2.0, cy - Rs * 2.30)
        half = Rs * 0.30

        beam = QLinearGradient(0.0, top, 0.0, by)
        beam.setColorAt(0.00, QColor(core.red(), core.green(), core.blue(), 0))
        beam.setColorAt(0.34, QColor(core.red(), core.green(), core.blue(), int(10 + 10 * activity)))
        beam.setColorAt(0.72, QColor(core.red(), core.green(), core.blue(), int(16 + 16 * activity)))
        beam.setColorAt(1.00, QColor(hot.red(), hot.green(), hot.blue(), 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(beam))
        p.drawPolygon(QPolygonF([
            QPointF(cx - half * 0.34, top), QPointF(cx + half * 0.34, top),
            QPointF(cx + half, by), QPointF(cx - half, by),
        ]))

        # Filaments verticaux : ils descendent puis se dissolvent au sol.
        p.setBrush(Qt.BrushStyle.NoBrush)
        for i in range(6):
            phase = (t * (0.35 + 0.09 * i) + i * 0.41) % 1.0
            x = cx + half * (i / 2.5 - 1.0) * 0.86
            y0 = top + (by - top) * phase
            length = Rs * (0.22 + 0.30 * (1.0 - phase))
            alpha = int(150 * math.sin(phase * math.pi) * (0.35 + 0.65 * activity))
            if alpha <= 3:
                continue
            p.setPen(QPen(QColor(hot.red(), hot.green(), hot.blue(), alpha), 1.0))
            p.drawLine(QPointF(x, y0), QPointF(x, min(by, y0 + length)))

    # ══ Anneaux orbitaux gyroscopiques ═══════════════════════════════════════
    def _project_orbit_rings(self, m, cx: float, cy: float, Rs: float, t: float):
        """Projette les anneaux inclinés en rotation autour de la sphère."""
        curves = []
        for k, (radius, tilt, offset, speed, _width) in enumerate(self._ring_specs):
            spin = math.radians(t * speed * 0.55 + self._ring_phase[k])
            cs, ss = math.cos(spin), math.sin(spin)
            ct, st = math.cos(tilt + offset * 0.18), math.sin(tilt + offset * 0.18)
            local = []
            for lx, _ly, lz in self._ring_pts[k]:
                y1, z1 = -lz * st, lz * ct           # inclinaison autour de X
                x2, z2 = lx * cs + z1 * ss, -lx * ss + z1 * cs   # rotation autour de Y
                local.append((x2 * radius, y1 * radius, z2 * radius))
            curves.append(self._project(local, m, cx, cy, Rs))
        return curves

    def _draw_orbit_rings(self, p: "QPainter", curves, front: bool, t: float,
                          wire: QColor, hot: QColor, activity: float) -> None:
        """Halo large puis trait net — deux passes sur un seul tri de segments.

        Trier deux fois les mêmes segments doublait le travail Python pour un
        rendu identique ; les chemins sont donc construits une fois.
        """
        nb = self._ZBUCKETS
        paths = [QPainterPath() for _ in range(nb)]
        used = [False] * nb
        for proj in curves:
            prev = proj[0]
            current_bucket: int | None = None
            for cur in proj[1:]:
                zm = prev[2] + cur[2]
                if (zm >= 0) == front:
                    b = int((zm * 0.25 + 0.5) * nb)
                    b = nb - 1 if b >= nb else (0 if b < 0 else b)
                    if current_bucket != b:
                        paths[b].moveTo(prev[0], prev[1])
                        current_bucket = b
                        used[b] = True
                    paths[b].lineTo(cur[0], cur[1])
                else:
                    current_bucket = None
                prev = cur
        a_max = int(175 + 80 * activity) if front else int(46 + 34 * activity)
        a_min = 58 if front else 16
        line = hot if front else wire
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        for i in range(nb):
            if not used[i]:
                continue
            f = (i + 0.5) / nb
            alpha = int(a_min + (a_max - a_min) * f ** 1.6)
            if front:
                glow = QPen(QColor(wire.red(), wire.green(), wire.blue(), alpha // 5),
                            3.4 * (0.6 + 0.7 * f))
                glow.setCapStyle(Qt.PenCapStyle.RoundCap)
                glow.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                p.setPen(glow)
                p.drawPath(paths[i])
            stroke = QPen(QColor(line.red(), line.green(), line.blue(), alpha),
                          (1.6 if front else 1.05) * (0.6 + 0.7 * f))
            stroke.setCapStyle(Qt.PenCapStyle.RoundCap)
            stroke.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            p.setPen(stroke)
            p.drawPath(paths[i])
        if not front:
            return
        # Curseur lumineux qui file le long de chaque anneau.
        p.setPen(Qt.PenStyle.NoPen)
        for k, proj in enumerate(curves):
            x, y, z = proj[int((t * 26 + k * 11) % (len(proj) - 1))]
            if z < 0:
                continue
            p.setBrush(QBrush(QColor(hot.red(), hot.green(), hot.blue(), 90)))
            p.drawEllipse(QPointF(x, y), 6.5, 6.5)
            p.setBrush(QBrush(QColor(255, 255, 255, 235)))
            p.drawEllipse(QPointF(x, y), 2.1, 2.1)

    def _draw_limb(self, p: "QPainter", cx: float, cy: float, Rs: float,
                   core: QColor, hot: QColor, activity: float, t: float) -> None:
        """Fresnel circulaire très léger : la sphère se lit sans cerceau chrome."""
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(
            QColor(core.red(), core.green(), core.blue(), int(22 + 28 * activity)),
            max(1.0, Rs * 0.010),
        ))
        p.drawEllipse(QPointF(cx, cy), Rs * 0.992, Rs * 0.992)

    def _draw_neural_orb(self, p: "QPainter", m, cx: float, cy: float, Rs: float,
                         t: float, core: QColor, halo: QColor, wire: QColor,
                         hot: QColor) -> None:
        """Un volume de photons : seules les rencontres créent des filaments."""
        p.save()
        if not self._clock_display_active:
            clip = QPainterPath()
            clip.addEllipse(QPointF(cx, cy), Rs * 1.02, Rs * 1.02)
            p.setClipPath(clip)
        self._draw_particle_cloud(p, m, cx, cy, Rs, core, wire, hot)
        p.restore()

    # ══ Dessin ═══════════════════════════════════════════════════════════════
    def paintEvent(self, _):
        W, H = self.width(), self.height()
        if W <= 0 or H <= 0:
            return
        pal = self._PALETTES.get(self._ws, self._PALETTES["idle"])
        if self._cache_key != (W, H, self._ws):
            self._build_cache(W, H, pal)

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        t = time.monotonic() - self._t0
        live = self._pal_live
        spd = live["pulse_speed"]
        core, halo, wire, hot = live["core"], live["halo"], live["wire"], live["hot"]

        # ── Léger flottement organique : l'orbe dérive doucement comme s'il
        # flottait, au lieu de rester rivé au pixel — casse la rigidité "CAO".
        bob_x = min(W, H) * 0.006 * (math.sin(t * 0.17) + 0.4 * math.sin(t * 0.41 + 1.3))
        bob_y = min(W, H) * 0.005 * (math.sin(t * 0.13 + 0.7) + 0.4 * math.sin(t * 0.29 + 2.1))
        cx, cy = W / 2.0 + bob_x, H / 2.0 + bob_y

        # ── Respiration à deux fréquences (lente + fine) : plus organique
        # qu'un simple sinus, moins "machine à pomper" qu'un aller-retour net.
        breathe = (0.60 * math.sin(t * spd * 1.1) + 0.40 * math.sin(t * 0.37 * spd + 2.0))
        R      = min(W, H) * 0.345
        Rs     = R * (1.0 + 0.022 * breathe + self._volume * 0.10)
        m      = self._matrix(self._yaw, self._pitch, self._roll)

        # Fond cyberpunk volontairement vide et très sombre : aucun socle,
        # faisceau, radar ou cadre HUD ne concurrence les photons.
        p.fillRect(self.rect(), QColor(2, 5, 11))

        self._draw_neural_orb(p, m, cx, cy, Rs, t, core, halo, wire, hot)

        p.end()
        self._adapt_rate((time.monotonic() - self._t0 - t) * 1000.0)

    def _draw_neon_eye_indicator(
        self, p: QPainter, cx: float, cy: float, hud_r: float, hot: QColor, wire: QColor, core: QColor
    ) -> None:
        """Dessine un indicateur discret d'œil néon sur l'orbe HUD quand la vision continue est active."""
        now = time.monotonic()
        pulse = 0.82 + 0.18 * math.sin(now * 3.8)
        alpha = int(220 * pulse)

        # Position discrète : quart supérieur droit de l'orbe
        eye_x = cx + hud_r * 0.64
        eye_y = cy - hud_r * 0.64
        badge_r = min(hud_r * 0.16, 24.0)

        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # 1. Halo néon diffus
        glow = QRadialGradient(eye_x, eye_y, badge_r * 1.5)
        glow.setColorAt(0.0, QColor(0, 240, 255, int(70 * pulse)))
        glow.setColorAt(0.7, QColor(wire.red(), wire.green(), wire.blue(), int(30 * pulse)))
        glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(glow))
        p.drawEllipse(QPointF(eye_x, eye_y), badge_r * 1.5, badge_r * 1.5)

        # 2. Anneau réticule cyber pointillé
        p.setPen(QPen(QColor(wire.red(), wire.green(), wire.blue(), int(160 * pulse)), 1.0, Qt.PenStyle.DashLine))
        p.setBrush(QBrush(QColor(4, 12, 22, int(190 * pulse))))
        p.drawEllipse(QPointF(eye_x, eye_y), badge_r, badge_r)

        # 3. Contour d'œil néon stylisé (deux courbes de Bézier formant la fente oculaire)
        eye_w = badge_r * 0.68
        eye_h = badge_r * 0.38

        path = QPainterPath()
        p_left = QPointF(eye_x - eye_w, eye_y)
        p_right = QPointF(eye_x + eye_w, eye_y)
        c_top = QPointF(eye_x, eye_y - eye_h)
        c_bot = QPointF(eye_x, eye_y + eye_h)

        path.moveTo(p_left)
        path.quadToPoint(c_top, p_right)
        path.quadToPoint(c_bot, p_left)

        p.setPen(QPen(QColor(0, 240, 255, alpha), 1.6))
        p.setBrush(QBrush(QColor(0, 180, 255, int(45 * pulse))))
        p.drawPath(path)

        # 4. Iris néon circulaire
        iris_r = badge_r * 0.26
        iris_grad = QRadialGradient(eye_x, eye_y, iris_r)
        iris_grad.setColorAt(0.0, QColor(255, 255, 255, alpha))
        iris_grad.setColorAt(0.4, QColor(0, 255, 220, alpha))
        iris_grad.setColorAt(1.0, QColor(0, 140, 255, int(180 * pulse)))
        p.setPen(QPen(QColor(0, 255, 255, alpha), 1.0))
        p.setBrush(QBrush(iris_grad))
        p.drawEllipse(QPointF(eye_x, eye_y), iris_r, iris_r)

        # 5. Pupille centrale ultra-lumineuse
        pupil_r = badge_r * 0.11
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(255, 255, 255, int(250 * pulse))))
        p.drawEllipse(QPointF(eye_x, eye_y), pupil_r, pupil_r)

        # 6. Micro-repères tactiques horizontaux
        p.setPen(QPen(QColor(wire.red(), wire.green(), wire.blue(), int(140 * pulse)), 1.0))
        p.drawLine(QLineF(eye_x - badge_r - 2, eye_y, eye_x - badge_r + 3, eye_y))
        p.drawLine(QLineF(eye_x + badge_r - 3, eye_y, eye_x + badge_r + 2, eye_y))

        # 7. Micro-libellé "LIVE" futuriste sous l'icône
        f = p.font()
        f.setPointSize(max(5, int(badge_r * 0.28)))
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(0, 240, 255, int(210 * pulse)))
        lbl_rect = QRectF(eye_x - badge_r, eye_y + badge_r * 0.95, badge_r * 2, badge_r * 0.55)
        p.drawText(lbl_rect, Qt.AlignmentFlag.AlignCenter, "LIVE")

        p.restore()

    def _draw_holographic_gesture_badge(
        self, p: QPainter, cx: float, cy: float, hud_r: float, hot: QColor, wire: QColor, core: QColor
    ) -> None:
        """Dessine un badge holographique néon flottant au centre de l'orbe."""
        icon = getattr(self, "_gesture_icon", None)
        expires = getattr(self, "_gesture_expires", 0.0)
        now = time.monotonic()
        if not icon or now >= expires:
            return

        rem = expires - now
        fade = min(1.0, rem / 0.35)
        badge_r = min(hud_r * 0.36, 68.0)

        p.save()
        # Halo de fond radial
        bg = QRadialGradient(cx, cy, badge_r)
        bg.setColorAt(0.0, QColor(0, 15, 28, int(210 * fade)))
        bg.setColorAt(0.7, QColor(core.red() // 3, core.green() // 3, core.blue() // 3, int(150 * fade)))
        bg.setColorAt(1.0, QColor(wire.red(), wire.green(), wire.blue(), int(40 * fade)))

        p.setPen(QPen(QColor(wire.red(), wire.green(), wire.blue(), int(220 * fade)), 1.5))
        p.setBrush(QBrush(bg))
        p.drawEllipse(QPointF(cx, cy), badge_r, badge_r)

        # Anneau interne radar
        p.setPen(QPen(QColor(hot.red(), hot.green(), hot.blue(), int(150 * fade)), 1.0, Qt.PenStyle.DashLine))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(cx, cy), badge_r * 0.84, badge_r * 0.84)

        # Mapping de l'icône / glyphe
        glyph_map = {
            "play_pause": "⏯",
            "play": "▶",
            "pause": "⏸",
            "mute": "🤫",
            "shh": "🤫",
            "volume": "🔊",
            "next_track": "⏭",
            "next": "⏭",
        }
        glyph = glyph_map.get(str(icon).lower(), str(icon))

        # Texte / Icône centrée
        f = p.font()
        f.setPointSize(max(14, int(badge_r * 0.48)))
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(hot.red(), hot.green(), hot.blue(), int(255 * fade)))
        icon_rect = QRectF(cx - badge_r, cy - badge_r * 0.72, badge_r * 2, badge_r * 1.05)
        p.drawText(icon_rect, Qt.AlignmentFlag.AlignCenter, glyph)

        # Libellé en dessous
        label = getattr(self, "_gesture_label", "")
        if label:
            lf = p.font()
            lf.setPointSize(max(7, int(badge_r * 0.15)))
            lf.setBold(True)
            p.setFont(lf)
            p.setPen(QColor(wire.red(), wire.green(), wire.blue(), int(240 * fade)))
            lbl_rect = QRectF(cx - badge_r, cy + badge_r * 0.30, badge_r * 2, badge_r * 0.50)
            p.drawText(lbl_rect, Qt.AlignmentFlag.AlignCenter, str(label).upper())

        p.restore()

    def _adapt_rate(self, ms: float):
        """Ajuste l'intervalle du timer sur le coût réel d'une frame."""
        self._frame_ms += (ms - self._frame_ms) * 0.1
        want = 20 if self._frame_ms < 16 else min(40, int(self._frame_ms * 1.25))
        if getattr(self, "_speaking", False):
            # Les effets restent à 25 images/s pendant la voix : une frame
            # de décor ne doit pas prendre le budget d'une tranche audio.
            want = max(want, 40)
        if self._on_battery:
            want = max(want, 40)  # plafond ~25 fps sur batterie
        if abs(want - self._interval) >= 3:
            self._interval = want
            # En sommeil de calcul, la cadence est déjà fixée à 60 ms : la
            # laisser se réajuster ici réveillerait l'orbe caché.
            if not getattr(self, "_low_power", False):
                self._anim_tmr.setInterval(want)
