from __future__ import annotations

import math

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QBrush, QColor, QConicalGradient, QPainter, QPen, QPixmap, QRadialGradient,
)

from ui.styles.theme import C


class _HudSpritesMixin:
    def _new_pm(self, w: int, h: int) -> QPixmap:
        pm = QPixmap(max(1, int(w)), max(1, int(h)))
        pm.fill(QColor(0, 0, 0, 0))
        return pm

    def _build_cache(self, W: int, H: int, pal: dict):
        wire, halo = pal["wire"], pal["halo"]
        hud_r = min(W, H) * 0.455
        D = int(hud_r * 2 + 40)
        c = D / 2.0

        def painter(pm):
            q = QPainter(pm)
            q.setRenderHint(QPainter.RenderHint.Antialiasing)
            return q

        # ── Fond cyberpunk sombre, sans grille ni décoration HUD ────────────
        bg = QPixmap(W, H)
        q = QPainter(bg)
        q.fillRect(0, 0, W, H, QColor(2, 5, 11))
        q.end()
        # Le mode particulaire n'emploie aucun sprite de radar, couronne ou
        # socle. On s'arrête ici afin de ne pas calculer ces décorations même
        # lorsqu'elles ne sont pas affichées.
        self._pm = {"bg": bg}
        self._hud_r = hud_r
        self._sprite_c = c
        self._cache_key = (W, H, self._ws)
        return

        # ── Balayage radar (conique) ────────────────────────────────────────
        sweep = self._new_pm(D, D)
        q = painter(sweep)
        cg = QConicalGradient(c, c, 0)
        cg.setColorAt(0.00, QColor(wire.red(), wire.green(), wire.blue(), 58))
        cg.setColorAt(0.06, QColor(wire.red(), wire.green(), wire.blue(), 30))
        cg.setColorAt(0.22, QColor(wire.red(), wire.green(), wire.blue(), 0))
        cg.setColorAt(1.00, QColor(wire.red(), wire.green(), wire.blue(), 0))
        q.setPen(Qt.PenStyle.NoPen)
        q.setBrush(QBrush(cg))
        q.drawEllipse(QPointF(c, c), hud_r, hud_r)
        # atténuation radiale : le faisceau s'efface vers le bord
        mask = QRadialGradient(c, c, hud_r)
        mask.setColorAt(0.00, QColor(0, 0, 0, 255))
        mask.setColorAt(0.55, QColor(0, 0, 0, 150))
        mask.setColorAt(1.00, QColor(0, 0, 0, 0))
        q.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
        q.setBrush(QBrush(mask))
        q.drawRect(0, 0, D, D)
        q.end()

        # ── Couronne HUD : graduations + arcs de cadre (un seul sprite) ─────
        ring = self._new_pm(D, D)
        q = painter(ring)
        q.save()
        q.translate(c, c)
        for i in range(72):
            major = (i % 6 == 0)
            q.setPen(QPen(QColor(wire.red(), wire.green(), wire.blue(),
                                 96 if major else 40), 1.4 if major else 1.0))
            q.drawLine(QPointF(hud_r, 0), QPointF(hud_r - (9 if major else 4), 0))
            q.rotate(5)
        q.restore()
        q.setBrush(Qt.BrushStyle.NoBrush)
        for rr, a, thick, span, offs in ((hud_r + 8, 74, 1.7, 124, (18, 198)),
                                         (hud_r - 22, 42, 1.1, 96, (95, 275))):
            q.setPen(QPen(QColor(wire.red(), wire.green(), wire.blue(), a), thick))
            rect = QRectF(c - rr, c - rr, rr * 2, rr * 2)
            for start in offs:
                q.drawArc(rect, int(start * 16), int(span * 16))
        q.end()

        # ── Corona du noyau : toutes les couches de bloom en un sprite ──────
        GD = 256
        glow = self._new_pm(GD, GD)
        q = painter(glow)
        hot = pal["hot"]
        gg = QRadialGradient(GD / 2, GD / 2, GD / 2)
        gg.setColorAt(0.00, QColor(255, 255, 255, 235))
        gg.setColorAt(0.07, QColor(hot.red(), hot.green(), hot.blue(), 210))
        gg.setColorAt(0.16, QColor(halo.red(), halo.green(), halo.blue(), 170))
        gg.setColorAt(0.32, QColor(halo.red(), halo.green(), halo.blue(), 78))
        gg.setColorAt(0.55, QColor(halo.red(), halo.green(), halo.blue(), 28))
        gg.setColorAt(0.78, QColor(halo.red(), halo.green(), halo.blue(), 8))
        gg.setColorAt(1.00, QColor(halo.red(), halo.green(), halo.blue(), 0))
        q.setPen(Qt.PenStyle.NoPen)
        q.setBrush(QBrush(gg))
        q.drawEllipse(0, 0, GD, GD)
        q.end()

        # ── Socle du projecteur : anneaux + graduations, cuits une fois ────
        # Cinq ellipses antialiasées de grand rayon et trois douzaines de
        # graduations coûtaient plus cher, chaque image, que tout le maillage
        # de la sphère. Elles ne bougent pas : elles appartiennent au cache.
        core = pal["core"]
        _f_by, f_base, f_flat = self._floor_geometry(H / 2.0, min(W, H) * 0.345)
        fw, fh = max(2, int(f_base * 2)), max(2, int(f_base * 2 * f_flat))
        floor = self._new_pm(fw, fh)
        q = painter(floor)
        q.translate(fw / 2.0, fh / 2.0)
        q.scale(1.0, f_flat)
        q.setBrush(Qt.BrushStyle.NoBrush)
        for factor, alpha, width in ((0.30, 210, 2.6), (0.62, 175, 2.0), (1.00, 138, 1.7)):
            q.setPen(QPen(QColor(core.red(), core.green(), core.blue(), alpha), width))
            q.drawEllipse(QPointF(0.0, 0.0), f_base * factor, f_base * factor)
        rr = f_base * 0.90
        q.setPen(QPen(QColor(core.red(), core.green(), core.blue(), 110), 1.4))
        for i in range(24):
            a = math.radians(i * 15)
            ca, sa = math.cos(a), math.sin(a)
            inner = rr - f_base * (0.075 if i % 3 == 0 else 0.035)
            q.drawLine(QPointF(rr * ca, rr * sa), QPointF(inner * ca, inner * sa))
        q.end()

        self._pm = {"bg": bg, "sweep": sweep, "ring": ring, "glow": glow, "floor": floor}
        self._hud_r = hud_r
        self._sprite_c = c
        self._cache_key = (W, H, self._ws)
