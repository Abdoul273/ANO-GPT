from __future__ import annotations

from PyQt6.QtCore import QRect, QSize, Qt
from PyQt6.QtWidgets import QLayout, QWidget, QWidgetItem

from ui.media.camera import _CameraPreview
from ui.panels.clipboard import ClipboardPanel
from ui.paths import _DEFAULT_H, _DEFAULT_W, _MIN_H, _MIN_W, _RIGHT_W


class HudOverlayLayout(QLayout):
    """Calque unique du HUD : surfaces plein cadre + chrome en grille logique.

    Un QStackedLayout re-parenterait l'orbe et casserait le contrat
    `hud.parentWidget() is centralWidget()`. Ce layout pose les surfaces
    plein cadre comme StackAll, et place le chrome d'après sizeHint pour
    éviter les chevauchements HiDPI/Wayland.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setContentsMargins(0, 0, 0, 0)
        self._fill: list[QWidgetItem] = []
        self._roles: dict[str, QWidgetItem] = {}

    def add_fill(self, widget: QWidget) -> None:
        # Avec PyQt6/Qt 6.11 sous Python 3.14, ``QLayout.addChildWidget`` peut
        # segfault lorsqu'un enfant appartient déjà au central widget (cas du
        # tiroir et des overlays). Ce layout place explicitement chaque widget
        # dans ``_place`` : il n'a donc pas besoin du rattachement C++ implicite.
        # Conserver/normaliser le parent Python suffit et évite ce crash natif.
        parent = self.parentWidget()
        if parent is not None and widget.parentWidget() is not parent:
            widget.setParent(parent)
        self._fill.append(QWidgetItem(widget))
        self.invalidate()

    def add_role(self, widget: QWidget, role: str) -> None:
        parent = self.parentWidget()
        if parent is not None and widget.parentWidget() is not parent:
            widget.setParent(parent)
        self._roles[role] = QWidgetItem(widget)
        self.invalidate()

    def addItem(self, item):
        self._fill.append(item)

    def count(self) -> int:
        return len(self._fill) + len(self._roles)

    def itemAt(self, index: int):
        items = self._fill + list(self._roles.values())
        return items[index] if 0 <= index < len(items) else None

    def takeAt(self, index: int):
        items = self._fill + list(self._roles.values())
        if not (0 <= index < len(items)):
            return None
        item = items[index]
        if item in self._fill:
            self._fill.remove(item)
        else:
            for key, value in list(self._roles.items()):
                if value is item:
                    del self._roles[key]
                    break
        return item

    def sizeHint(self) -> QSize:
        return QSize(_DEFAULT_W, _DEFAULT_H)

    def minimumSize(self) -> QSize:
        return QSize(_MIN_W, _MIN_H)

    def expandingDirections(self):
        return Qt.Orientation.Horizontal | Qt.Orientation.Vertical

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._place(rect)

    def _widget(self, role: str) -> QWidget | None:
        item = self._roles.get(role)
        return None if item is None else item.widget()

    def _place(self, rect: QRect) -> None:
        W, H = rect.width(), rect.height()
        full = QRect(0, 0, W, H)
        for item in self._fill:
            widget = item.widget()
            if widget is not None:
                widget.setGeometry(full)

        telemetry = self._widget("telemetry")
        header = self._widget("header")
        status = self._widget("status")
        cmd = self._widget("cmd")

        header_x = W - 16
        if header is not None:
            hw = min(500, max(360, int(W * 0.43)), W - 32)
            header_x = W - hw - 16
            header.setGeometry(header_x, 12, hw, 58)
            header.raise_()

        if status is not None:
            if W >= 1050:
                pw = min(220, W - 32)
                status_x = (W - pw) // 2
            else:
                gap_left = 244
                gap_right = max(gap_left + 120, header_x - 12)
                pw = min(200, gap_right - gap_left)
                status_x = gap_left + max(0, (gap_right - gap_left - pw) // 2)
            status.setGeometry(status_x, 16, pw, 32)
            status.raise_()

        if telemetry is not None:
            th = min(max(420, telemetry.sizeHint().height()), H - 32)
            telemetry.setGeometry(16, 16, 212, th)
            telemetry.raise_()

        if cmd is not None:
            if W < 1000:
                cx = 244
                cw_w = max(360, W - cx - 16)
            else:
                cw_w = min(700, W - 32)
                cx = (W - cw_w) // 2
            cy = H - 104
            cmd.setGeometry(cx, cy, cw_w, cmd.sizeHint().height() or 54)
            cmd.raise_()

        content = self._widget("content")
        if content is not None and not content.isHidden():
            pw = min(480, W - 32)
            ph = min(360, max(180, H - 260))
            content.setGeometry(W - pw - 16, H - ph - 96, pw, ph)
            content.raise_()

        music = self._widget("music")
        if music is not None and music.isVisible():
            pw = music.width()
            ph = music.sizeHint().height() or 180
            music.setGeometry(W - pw - 16, 74, pw, ph)
            music.raise_()

        cards = self._widget("cards")
        if cards is not None:
            # Les notifications restent dans la colonne droite, mais passent
            # sous le lecteur musique quand il est affiché : aucune carte ne
            # doit recouvrir les boutons pause/suivant/fermer.
            top = (header.y() + header.height() + 10) if header is not None else 80
            if music is not None and music.isVisible():
                top = max(top, music.y() + music.height() + 10)
            w = cards.width()
            bottom = content.y() - 12 if content is not None and not content.isHidden() else H - 112
            h = max(0, bottom - top)
            cards.setGeometry(W - w - 16, top, w, h)
            cards.raise_()

        nearby = self._widget("nearby")
        if nearby is not None and nearby.isVisible():
            w = min(540, W - 32)
            h = min(440, H - 140)
            nearby.setGeometry(W - w - 16, 74, w, h)
            nearby.raise_()

        speech = self._widget("speech")
        if speech is not None and speech.isVisible():
            max_w = min(int(W * 0.82), W - 48)
            ow = speech.fit_to_width(max_w)
            oh = speech.FIXED_HEIGHT
            cmd_top = cmd.y() if cmd is not None else H - 80
            speech.animate_to(QRect((W - ow) // 2, cmd_top - oh - 14, ow, oh))

        thought = self._widget("thought")
        if thought is not None and thought.isVisible():
            tw = min(540, W - 48)
            th = getattr(thought, "FIXED_HEIGHT", 56)
            orb_cy = H / 2.0
            orb_r = min(W, H) * 0.185
            thought_y = int(orb_cy + orb_r + 14)
            thought_x = (W - tw) // 2
            thought.animate_to(QRect(thought_x, thought_y, tw, th))
            thought.raise_()

        drop = self._widget("drop")
        if drop is not None and drop.isVisible():
            drop.setGeometry(0, 0, W, H)
            drop.raise_()

        preview = self._widget("cam_preview")
        if preview is not None:
            pw = _CameraPreview._W
            ph = preview.height() or _CameraPreview._H
            preview.setGeometry(W - _RIGHT_W - pw - 12, H - ph - 28, pw, ph)

        clipboard = self._widget("clipboard")
        if clipboard is not None and clipboard.isVisible():
            pw = ClipboardPanel._W
            ph = clipboard.sizeHint().height() or ClipboardPanel._H
            clipboard.setGeometry((W - pw) // 2, H - ph - 6, pw, ph)
            clipboard.raise_()

        drawer = self._widget("drawer")
        if drawer is not None and drawer.isVisible():
            dw = min(340, W - 32)
            dh = min(720, H - 32)
            drawer.setGeometry(16, 16, dw, dh)
            # Les panneaux permanents sont relevés à chaque recalcul.
            drawer.raise_()

        for role, size in (
            ("setup", (460, 390)),
            ("remote", None),
            ("customize", None),
            ("ai_config", None),
            ("generated_image", None),
            ("generated_artifact", None),
            ("audio", None),
            ("memory", None),
            ("plugin", None),
        ):
            overlay = self._widget(role)
            if overlay is None or not overlay.isVisible():
                continue
            if size is not None:
                ow, oh = size
            else:
                ow, oh = overlay._OW, overlay._OH
            ow = min(ow, W - 16)
            oh = min(oh, H - 16)
            scrim = getattr(overlay, "_scrim", None)
            if scrim is not None and scrim.isVisible():
                scrim.setGeometry(full)
                scrim.raise_()
            overlay.setGeometry((W - ow) // 2, (H - oh) // 2, ow, oh)
            overlay.raise_()
