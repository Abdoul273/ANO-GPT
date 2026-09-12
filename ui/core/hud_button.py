from __future__ import annotations

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import QBrush, QColor, QLinearGradient, QPainter, QPen
from PyQt6.QtWidgets import QPushButton

from ui.core.hud_paint import Hud
from ui.styles.theme import C, make_svg_icon, qcol

class HudButton(QPushButton):
    """Touche biseautée du HUD : le seul bouton peint de l'interface.

    Qt ne sait pas découper un widget en polygone par feuille de style, et un
    `border-radius` au milieu de panneaux à angles coupés jurait immédiatement.
    Le bouton se peint donc lui-même, et l'illumination au survol est animée
    plutôt que commutée : un bouton qui s'allume d'un coup paraît cassé.
    """

    def __init__(self, text: str = "", *, icon: str = "", primary: bool = False,
                 accent: str | None = None, hover_accent: str | None = None,
                 size: int = 16, parent=None):
        super().__init__(text.upper(), parent)
        self._primary = primary
        self._accent = qcol(accent or C.PRI)
        # Un bouton de fermeture rouge en permanence crie l'alarme ; il ne doit
        # virer au rouge qu'au moment où la souris le vise.
        self._hover_accent = qcol(hover_accent) if hover_accent else None
        self._icon_name = icon
        self._icon_size = size
        self._glow = 0.0          # 0 au repos, 1 au survol
        self._pressed = False
        self._pix = None
        self._pix_hover = None
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFlat(True)
        self.setFont(Hud.micro_font(7, 1.5))
        # Sans cela, le style natif repeint un fond gris par-dessus la peinture.
        self.setStyleSheet("background: transparent; border: none;")
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._ease)
        self._timer.setInterval(24)
        self._rebuild_icon()

    def set_icon_name(self, name: str) -> None:
        self._icon_name = name
        self._rebuild_icon()
        self.update()

    def set_accent(self, accent: str) -> None:
        self._accent = qcol(accent)
        self._rebuild_icon()
        self.update()

    def set_hover_accent(self, accent: str | None) -> None:
        self._hover_accent = qcol(accent) if accent else None
        self._rebuild_icon()
        self.update()

    def _rebuild_icon(self) -> None:
        if not self._icon_name:
            self._pix = self._pix_hover = None
            return
        tone = C.DARK if self._primary else self._accent.name()
        self._pix = make_svg_icon(self._icon_name, tone, self._icon_size).pixmap(
            self._icon_size, self._icon_size)
        # Deux images plutôt qu'un rendu SVG à chaque image d'animation : la
        # bascule est invisible à l'œil et coûte mille fois moins.
        self._pix_hover = (
            make_svg_icon(self._icon_name, self._hover_accent.name(),
                          self._icon_size).pixmap(self._icon_size, self._icon_size)
            if self._hover_accent is not None and not self._primary else None
        )

    def enterEvent(self, event):
        self._timer.start()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._timer.start()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        self._pressed = True
        self.update()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        self._pressed = False
        self.update()
        super().mouseReleaseEvent(event)

    def _ease(self):
        target = 1.0 if self.underMouse() and self.isEnabled() else 0.0
        delta = target - self._glow
        if abs(delta) < 0.02:
            self._glow = target
            self._timer.stop()
        else:
            self._glow += delta * 0.28
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(1, 1, self.width() - 2, self.height() - 2)
        if self._pressed:
            rect = rect.adjusted(0.5, 1.0, -0.5, 0.0)
        path = Hud.bevel(rect, 5.0)
        enabled = self.isEnabled()
        glow = self._glow if enabled else 0.0
        accent = self._accent
        if self._hover_accent is not None:
            # Fondu de teinte vers l'accent de survol, proportionnel à la lueur.
            accent = QColor(
                int(accent.red() + (self._hover_accent.red() - accent.red()) * glow),
                int(accent.green() + (self._hover_accent.green() - accent.green()) * glow),
                int(accent.blue() + (self._hover_accent.blue() - accent.blue()) * glow),
            )

        if self._primary:
            fill = QLinearGradient(rect.left(), 0, rect.right(), 0)
            fill.setColorAt(0.0, accent)
            fill.setColorAt(1.0, accent.lighter(135))
            p.setBrush(QBrush(fill))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawPath(path)
            text_col = qcol(C.DARK)
        else:
            base = 16 + int(60 * glow)
            p.setBrush(QBrush(QColor(accent.red(), accent.green(), accent.blue(), base)))
            p.setPen(QPen(QColor(accent.red(), accent.green(), accent.blue(),
                                 110 + int(120 * glow)), 1.1))
            p.drawPath(path)
            text_col = accent.lighter(100 + int(35 * glow))

        if glow > 0.02:
            # Lueur portée : redessiner le contour en plus large et translucide
            # coûte une passe, là où une ombre Qt coûterait un rendu hors écran.
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(accent.red(), accent.green(), accent.blue(),
                                 int(70 * glow)), 3))
            p.drawPath(path)

        content = rect
        pix = self._pix
        if self._pix_hover is not None and glow > 0.5:
            pix = self._pix_hover
        if pix is not None:
            if self.text():
                icon_x = rect.left() + 9
                p.drawPixmap(QPointF(icon_x, rect.center().y() - self._icon_size / 2), pix)
                content = QRectF(icon_x + self._icon_size + 6, rect.top(),
                                 rect.width() - self._icon_size - 18, rect.height())
            else:
                p.drawPixmap(QPointF(rect.center().x() - self._icon_size / 2,
                                     rect.center().y() - self._icon_size / 2), pix)
        if self.text():
            p.setFont(self.font())
            p.setPen(QPen(text_col if enabled else qcol(C.TEXT_DIM), 1))
            p.drawText(content, Qt.AlignmentFlag.AlignCenter, self.text())
        p.end()
