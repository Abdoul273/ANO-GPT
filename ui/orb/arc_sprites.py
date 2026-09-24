from __future__ import annotations

import math
import random

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QBrush, QColor, QLinearGradient, QPainter, QPen, QPixmap, QPolygonF,
    QRadialGradient,
)


class _HudSpritesMixin:
    """Sprites de lueur cuits une fois par teinte : un dégradé radial plein
    cadre coûte un écran entier de pixels à chaque image, un blit de pixmap
    256² ne coûte rien."""

    _SPRITE = 256
    _REACTOR_SPRITE = 512

    @staticmethod
    def _quantize(color: QColor) -> tuple[int, int, int]:
        # Un pas de 8 par canal : la palette vivante glisse pendant ~1,2 s à
        # chaque changement d'état, ce pas limite les reconstructions à une
        # dizaine par transition, imperceptibles.
        return color.red() >> 3, color.green() >> 3, color.blue() >> 3

    def _sprite_key(self, W: int, H: int) -> tuple:
        live = self._pal_live
        return (W, H, self._background_photo_active, self._quantize(live["halo"]),
                self._quantize(live["hot"]), self._quantize(live["core"]))

    def _new_pm(self, w: int, h: int) -> QPixmap:
        pm = QPixmap(max(1, int(w)), max(1, int(h)))
        pm.fill(QColor(0, 0, 0, 0))
        return pm

    def _radial_sprite(self, stops: list[tuple[float, QColor]]) -> QPixmap:
        size = self._SPRITE
        pm = self._new_pm(size, size)
        q = QPainter(pm)
        q.setRenderHint(QPainter.RenderHint.Antialiasing)
        gradient = QRadialGradient(size / 2, size / 2, size / 2)
        for pos, color in stops:
            gradient.setColorAt(pos, color)
        q.setPen(Qt.PenStyle.NoPen)
        q.setBrush(QBrush(gradient))
        q.drawEllipse(0, 0, size, size)
        q.end()
        return pm

    def _reactor_sprite(self, core: QColor, hot: QColor) -> QPixmap:
        """Couronne technique précalculée : aucun segment à recalculer par image."""
        size = self._REACTOR_SPRITE
        center = size / 2.0
        pm = self._new_pm(size, size)
        q = QPainter(pm)
        q.setRenderHint(QPainter.RenderHint.Antialiasing)
        q.setBrush(Qt.BrushStyle.NoBrush)

        def arc(radius: float, start: float, span: float, color: QColor, width: float = 1.0):
            q.setPen(QPen(color, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
            q.drawArc(QRectF(center - radius, center - radius, radius * 2, radius * 2),
                      int(start * 16), int(span * 16))

        # Trois pistes de longueur différente créent une profondeur mécanique.
        for i in range(12):
            angle = i * 30.0 + 4.0
            arc(211, angle, 21, QColor(core.red(), core.green(), core.blue(), 108), 1.0)
            arc(203, angle + 3, 15, QColor(hot.red(), hot.green(), hot.blue(), 194), 2.0)
            arc(187, angle - 2, 26, QColor(core.red(), core.green(), core.blue(), 56), 1.0)
            arc(163, angle + 1, 19, QColor(core.red(), core.green(), core.blue(), 88), 1.0)
            if i % 3 == 0:
                arc(194, angle + 5, 8, QColor(hot.red(), hot.green(), hot.blue(), 216), 2.0)
        for i in range(48):
            angle = math.tau * i / 48.0
            ca, sa = math.cos(angle), math.sin(angle)
            r0 = 174 if i % 4 else 169
            q.setPen(QPen(QColor(core.red(), core.green(), core.blue(),
                                 115 if i % 4 else 188), 1.0))
            q.drawLine(QPointF(center + ca * r0, center + sa * r0),
                       QPointF(center + ca * 180, center + sa * 180))
        # Poussière photonique au second plan : elle tourne très lentement avec
        # la couronne et profite du même blit, sans coût de peinture additionnel.
        rng = random.Random(73)
        dim, bright = [], []
        for index in range(132):
            angle = rng.random() * math.tau
            radius = math.sqrt(rng.random()) * 218
            point = QPointF(center + math.cos(angle) * radius,
                            center + math.sin(angle) * radius)
            (bright if index % 7 == 0 else dim).append(point)
        q.setPen(QPen(QColor(core.red(), core.green(), core.blue(), 76), 1.0))
        q.drawPoints(QPolygonF(dim))
        q.setPen(QPen(QColor(hot.red(), hot.green(), hot.blue(), 128), 1.3))
        q.drawPoints(QPolygonF(bright))
        q.end()
        return pm

    def _build_cache(self, W: int, H: int) -> None:
        live = self._pal_live
        halo, hot, core = live["halo"], live["hot"], live["core"]
        h = (halo.red(), halo.green(), halo.blue())
        c = (core.red(), core.green(), core.blue())
        t = (hot.red(), hot.green(), hot.blue())

        # Aura ambiante : très diffuse, elle « éclaire » le fond autour de
        # l'orbe et donne l'impression que la sphère émet dans la pièce.
        glow = self._radial_sprite([
            (0.00, QColor(*h, 150)), (0.20, QColor(*h, 96)), (0.42, QColor(*h, 46)),
            (0.66, QColor(*h, 16)), (0.86, QColor(*h, 4)), (1.00, QColor(*h, 0)),
        ])
        # Noyau : blanc incandescent → teinte chaude → halo → rien.
        nucleus = self._radial_sprite([
            (0.00, QColor(255, 255, 255, 240)), (0.08, QColor(*t, 225)),
            (0.20, QColor(*c, 165)), (0.40, QColor(*h, 70)),
            (0.66, QColor(*h, 18)), (1.00, QColor(*h, 0)),
        ])
        # Photon : un disque doux réutilisé pour les curseurs des anneaux.
        photon = self._radial_sprite([
            (0.00, QColor(255, 255, 255, 235)), (0.30, QColor(*t, 170)),
            (0.60, QColor(*c, 50)), (1.00, QColor(*c, 0)),
        ])
        # Scène de fond : le fond sombre (ou le voile sur la photo) et l'aura
        # ambiante, cuits ensemble. Un blit W×H non redimensionné coûte une
        # copie mémoire ; l'aura redessinée à chaque image coûtait 3 ms.
        scene = QPixmap(max(1, W), max(1, H))
        scene.fill(QColor(0, 0, 0, 0))
        q = QPainter(scene)
        if self._background_photo_active:
            q.fillRect(0, 0, W, H, QColor(2, 5, 11, 150))
        else:
            q.fillRect(0, 0, W, H, QColor(1, 4, 9))

        # Plan holographique très discret, cuit avec le décor. Le point de
        # fuite derrière le noyau donne une profondeur de salle de commande,
        # sans transformer l'animation de l'orbe en travail de plein écran.
        horizon = H * 0.64
        floor = QLinearGradient(0, horizon, 0, H)
        floor.setColorAt(0.0, QColor(0, 24, 40, 0))
        floor.setColorAt(1.0, QColor(0, 80, 115, 34))
        q.fillRect(QRectF(0, horizon, W, H - horizon), QBrush(floor))
        q.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        q.setPen(QPen(QColor(*c, 20), 1.0))
        vanishing = QPointF(W / 2.0, horizon)
        for index in range(-8, 9):
            q.drawLine(vanishing, QPointF(W / 2.0 + index * W * 0.115, H))
        for index in range(1, 8):
            progress = index / 8.0
            y = horizon + (H - horizon) * progress * progress
            q.setPen(QPen(QColor(*c, 10 + index * 3), 1.0))
            q.drawLine(QPointF(0, y), QPointF(W, y))

        # Un unique trait d'horizon magenta signe la scène sans rivaliser
        # avec les couleurs sémantiques (écoute, réflexion, erreur) de l'orbe.
        q.setPen(QPen(QColor(255, 43, 214, 44), 1.0))
        q.drawLine(QPointF(W * 0.36, horizon), QPointF(W * 0.64, horizon))
        q.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        aura = min(W, H) * self._ORB_SCALE * 2.35
        q.setOpacity(0.55)
        q.drawPixmap(QRectF(W / 2.0 - aura, H / 2.0 - aura, aura * 2, aura * 2), glow,
                     QRectF(0, 0, self._SPRITE, self._SPRITE))
        q.end()
        self._pm = {"glow": glow, "nucleus": nucleus, "photon": photon,
                    "reactor": self._reactor_sprite(core, hot), "scene": scene}
        self._cache_key = self._sprite_key(W, H)
