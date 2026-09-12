from __future__ import annotations

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QBrush, QColor, QPainter, QPixmap, QRadialGradient


class _HudSpritesMixin:
    """Sprites de lueur cuits une fois par teinte : un dégradé radial plein
    cadre coûte un écran entier de pixels à chaque image, un blit de pixmap
    256² ne coûte rien."""

    _SPRITE = 256

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
            q.fillRect(0, 0, W, H, QColor(2, 5, 11))
        aura = min(W, H) * self._ORB_SCALE * 2.35
        q.setOpacity(0.55)
        q.drawPixmap(QRectF(W / 2.0 - aura, H / 2.0 - aura, aura * 2, aura * 2), glow,
                     QRectF(0, 0, self._SPRITE, self._SPRITE))
        q.end()
        self._pm = {"glow": glow, "nucleus": nucleus, "photon": photon, "scene": scene}
        self._cache_key = self._sprite_key(W, H)
