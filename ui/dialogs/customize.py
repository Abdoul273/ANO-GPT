from __future__ import annotations

import math
from pathlib import Path


from PyQt6.QtCore import (
    QPointF, QRectF, QSize, Qt,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QBrush, QColor, QConicalGradient, QFont, QIcon, QPainter,
    QPen, QPixmap,
)
from PyQt6.QtWidgets import (
    QGridLayout,
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea, QToolButton, QVBoxLayout, QWidget,
)

from ui.core.fade_widget import FadeInWidget
from ui.orb import registry as orb_registry
from ui.paths import BASE_DIR
from ui.styles.cyber import CyberHeader, micro_label
from ui.styles.theme import C, DEFAULT_UI_COLOR, qcol

class HueWheel(QWidget):
    hue_picked    = pyqtSignal(str)
    hue_committed = pyqtSignal(str)
    _RING = 16

    def __init__(self, initial_hex: str = DEFAULT_UI_COLOR, parent=None):
        super().__init__(parent)
        self.setFixedSize(148, 148)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hue  = 0.53
        self._drag = False
        self.set_color(initial_hex)

    def color(self) -> str:
        return QColor.fromHsvF(self._hue, 1.0, 1.0).name()

    def set_color(self, hex_str: str):
        c = QColor((hex_str or "").strip())
        if c.isValid() and c.hsvHueF() >= 0:
            self._hue = c.hsvHueF()
            self.update()

    def _ring_rect(self) -> QRectF:
        m = self._RING / 2 + 3
        return QRectF(self.rect()).adjusted(m, m, -m, -m)

    def _hue_from_pos(self, pos: QPointF) -> float:
        c  = QRectF(self.rect()).center()
        dx = pos.x() - c.x()
        dy = c.y() - pos.y()
        ang = math.atan2(dy, dx)
        return (ang / (2 * math.pi)) % 1.0

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect   = self._ring_rect()
        center = rect.center()
        grad = QConicalGradient(center, 0)
        for i in range(0, 361, 20):
            grad.setColorAt(i / 360.0, QColor.fromHsvF((i % 360) / 360.0, 1.0, 1.0))
        p.setPen(QPen(QBrush(grad), self._RING))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(rect)
        preview = QColor.fromHsvF(self._hue, 1.0, 1.0)
        inner   = rect.adjusted(30, 30, -30, -30)
        p.setPen(QPen(qcol(C.BORDER_B), 1))
        p.setBrush(QBrush(preview))
        p.drawEllipse(inner)
        r   = rect.width() / 2
        ang = self._hue * 2 * math.pi
        hx  = center.x() + r * math.cos(ang)
        hy  = center.y() - r * math.sin(ang)
        p.setPen(QPen(QColor("#00060a"), 2))
        p.setBrush(QBrush(QColor("#ffffff")))
        p.drawEllipse(QPointF(hx, hy), 7.5, 7.5)
        p.end()

    def mousePressEvent(self, e):
        self._drag = True
        self._hue  = self._hue_from_pos(e.position())
        self.update()
        self.hue_picked.emit(self.color())

    def mouseMoveEvent(self, e):
        if self._drag:
            self._hue = self._hue_from_pos(e.position())
            self.update()
            self.hue_picked.emit(self.color())

    def mouseReleaseEvent(self, e):
        if self._drag:
            self._drag = False
            self.hue_committed.emit(self.color())


# ── CustomizeOverlay (hérite de FadeInWidget) ──────────────────────────────
class CustomizeOverlay(FadeInWidget):
    saved = pyqtSignal(str, str, str, str, str)
    _OW, _OH = 420, 800
    _BACKGROUND_DIR = BASE_DIR / "background"
    _IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}

    def __init__(self, assistant_name="ANO-GPT", user_name="",
                 ui_color=DEFAULT_UI_COLOR, background_image="", orb_style="",
                 parent=None):
        super().__init__(parent, duration=300)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 18, 24, 18)
        lay.setSpacing(8)

        def _lbl(txt, fs=9, bold=False, color=C.PRI, align=Qt.AlignmentFlag.AlignCenter):
            w = QLabel(txt); w.setAlignment(align)
            w.setFont(QFont("Inter", fs,
                            QFont.Weight.Bold if bold else QFont.Weight.Normal))
            w.setStyleSheet(f"color: {color}; background: transparent;")
            return w

        header = CyberHeader("Personnaliser l'assistant", "APPARENCE", parent=self)
        header.close_clicked.connect(self._cancel)
        lay.addWidget(header)
        lay.addWidget(micro_label("Nom de l'assistant"))
        self._name_input = QLineEdit(assistant_name)
        self._name_input.setFont(QFont("Inter", 10))
        self._name_input.setFixedHeight(32)
        lay.addWidget(self._name_input)
        lay.addSpacing(4)
        lay.addWidget(micro_label("Votre nom  —  vide = mode par défaut"))
        self._user_input = QLineEdit(user_name)
        self._user_input.setPlaceholderText("ex. Tony   (laisser vide pour auto)")
        self._user_input.setFont(QFont("Inter", 10))
        self._user_input.setFixedHeight(32)
        lay.addWidget(self._user_input)
        lay.addSpacing(4)
        clr_hdr = QHBoxLayout()
        clr_hdr.addWidget(micro_label("Couleur de l'interface  —  faites glisser"))
        clr_hdr.addStretch()
        df_btn = QPushButton("DEFAUT")
        df_btn.setFixedSize(64, 20)
        df_btn.setFont(QFont("Inter", 7, QFont.Weight.Bold))
        df_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        df_btn.clicked.connect(lambda: self._set_color(DEFAULT_UI_COLOR))
        clr_hdr.addWidget(df_btn)
        lay.addLayout(clr_hdr)
        self._initial_color = (ui_color or DEFAULT_UI_COLOR).strip().lower()
        self._sel_color     = self._initial_color
        self.on_preview     = None
        self.on_background_preview = None
        self._wheel = HueWheel(self._sel_color)
        wheel_row = QHBoxLayout()
        wheel_row.addStretch(); wheel_row.addWidget(self._wheel); wheel_row.addStretch()
        lay.addLayout(wheel_row)
        self._wheel.hue_picked.connect(self._on_wheel_pick)
        self._wheel.hue_committed.connect(self._on_wheel_commit)
        self._hex_input = QLineEdit(self._sel_color)
        self._hex_input.setPlaceholderText("#00d4ff   (couleur hex personnalisée)")
        self._hex_input.setFont(QFont("Inter", 10))
        self._hex_input.setFixedHeight(28)
        self._hex_input.textEdited.connect(self._on_hex_edited)
        lay.addWidget(self._hex_input)
        lay.addSpacing(5)
        lay.addWidget(micro_label("Image d'arrière-plan"))
        self._background_path = str(background_image or "")
        self._initial_background_path = self._background_path
        self._background_buttons: dict[str, QToolButton] = {}
        self._background_gallery = QScrollArea()
        self._background_gallery.setWidgetResizable(True)
        self._background_gallery.setFixedHeight(132)
        self._background_gallery.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._background_gallery.setStyleSheet(
            "QScrollArea { background: rgba(8, 16, 26, 0.55);"
            " border: 1px solid rgba(0, 212, 255, 0.16);"
            " border-left: 2px solid rgba(0, 212, 255, 0.45); border-radius: 4px; }"
        )
        self._gallery_content = QWidget()
        self._gallery_content.setStyleSheet("background: transparent;")
        self._gallery_grid = QGridLayout(self._gallery_content)
        self._gallery_grid.setContentsMargins(7, 7, 7, 7)
        self._gallery_grid.setSpacing(6)
        self._background_gallery.setWidget(self._gallery_content)
        lay.addWidget(self._background_gallery)
        self._refresh_background_gallery()
        hint = _lbl("Dépose tes images dans le dossier  background/  d’ANO-GPT.", 7,
                    color=C.TEXT_DIM, align=Qt.AlignmentFlag.AlignLeft)
        lay.addWidget(hint)
        bg_row = QHBoxLayout(); bg_row.setSpacing(8)
        pick_bg = QPushButton("ACTUALISER LA GALERIE")
        pick_bg.clicked.connect(self._refresh_background_gallery)
        clear_bg = QPushButton("RETIRER")
        clear_bg.clicked.connect(self._clear_background)
        for button in (pick_bg, clear_bg):
            button.setFixedHeight(27)
            button.setFont(QFont("Inter", 8, QFont.Weight.Bold))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            bg_row.addWidget(button)
        lay.addLayout(bg_row)
        lay.addSpacing(5)
        lay.addWidget(micro_label("Style de l'orbe"))
        self.on_orb_preview = None
        self._orb_style = orb_registry.resolve(orb_style)
        self._initial_orb_style = self._orb_style
        self._orb_buttons: dict[str, QToolButton] = {}
        orb_grid = QGridLayout()
        orb_grid.setContentsMargins(0, 0, 0, 0)
        orb_grid.setSpacing(6)
        for index, spec in enumerate(orb_registry.selectable_specs()):
            button = self._make_orb_tile(spec)
            self._orb_buttons[spec.id] = button
            orb_grid.addWidget(button, index // 3, index % 3)
        lay.addLayout(orb_grid)
        self._update_orb_selection()
        lay.addSpacing(6)
        btn_row = QHBoxLayout(); btn_row.setSpacing(8)
        save_btn = QPushButton("▸  APPLIQUER")
        save_btn.setFixedHeight(34)
        save_btn.setFont(QFont("Inter", 9, QFont.Weight.Bold))
        save_btn.setObjectName("CyberPrimary")
        save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        save_btn.clicked.connect(self._save)
        btn_row.addWidget(save_btn)
        cancel_btn = QPushButton("ANNULER")
        cancel_btn.setFixedHeight(34)
        cancel_btn.setFont(QFont("Inter", 9))
        cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel_btn.clicked.connect(self._cancel)
        btn_row.addWidget(cancel_btn)
        lay.addLayout(btn_row)

    def _set_color(self, hx: str, update_wheel: bool = True, preview: bool = True):
        self._sel_color = hx.strip().lower()
        self._hex_input.blockSignals(True)
        self._hex_input.setText(self._sel_color)
        self._hex_input.blockSignals(False)
        if update_wheel:
            self._wheel.set_color(self._sel_color)
        if preview and self.on_preview:
            self.on_preview(self._sel_color)

    def _on_wheel_pick(self, hx: str):
        self._sel_color = hx
        self._hex_input.blockSignals(True)
        self._hex_input.setText(hx)
        self._hex_input.blockSignals(False)

    def _on_wheel_commit(self, hx: str):
        self._set_color(hx, update_wheel=False)

    def _on_hex_edited(self, text: str):
        t = text.strip().lower()
        if t.startswith("#") and len(t) == 7:
            try:
                int(t[1:], 16)
            except ValueError:
                return
            self._set_color(t, update_wheel=True, preview=True)

    def _make_orb_tile(self, spec) -> QToolButton:
        button = QToolButton()
        button.setCheckable(True)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        button.setFixedSize(120, 38)
        button.setIconSize(QSize(18, 18))
        button.setText(spec.label)
        button.setToolTip(spec.tagline)
        button.setFont(QFont("Inter", 7, QFont.Weight.Bold))
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        swatch = QPixmap(18, 18)
        swatch.fill(Qt.GlobalColor.transparent)
        painter = QPainter(swatch)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor(spec.swatch)
        painter.setPen(QPen(color, 1.6))
        painter.setBrush(QColor(color.red(), color.green(), color.blue(), 70))
        painter.drawEllipse(QRectF(2, 2, 14, 14))
        painter.end()
        button.setIcon(QIcon(swatch))
        button.setStyleSheet(f"""
            QToolButton {{ color: {C.TEXT_MED}; background: rgba(14, 23, 36, 0.92);
              border: 1px solid rgba(0, 212, 255, 0.30); border-radius: 4px; padding: 3px; }}
            QToolButton:hover {{ color: {C.WHITE}; border-color: {C.PRI}; }}
            QToolButton:checked {{ color: {C.PRI}; border: 1px solid {C.PRI};
              background: rgba(0, 212, 255, 0.18); }}
        """)
        button.clicked.connect(lambda checked=False, sid=spec.id: self._select_orb(sid))
        return button

    def _select_orb(self, style_id: str):
        # Aperçu immédiat ; si le style refuse de naître, la sélection reste.
        if self.on_orb_preview and not self.on_orb_preview(style_id):
            self._update_orb_selection()
            return
        self._orb_style = style_id
        self._update_orb_selection()

    def _update_orb_selection(self):
        for style_id, button in self._orb_buttons.items():
            button.blockSignals(True)
            button.setChecked(style_id == self._orb_style)
            button.blockSignals(False)

    def sync_external_appearance(self, background_path: str, orb_style: str) -> None:
        """Garde Appliquer/Annuler cohérents après une commande de l'assistant."""
        self._background_path = self._initial_background_path = background_path
        self._orb_style = self._initial_orb_style = orb_style
        self._refresh_background_gallery()
        self._update_orb_selection()

    def _cancel(self):
        if self.on_orb_preview and self._orb_style != self._initial_orb_style:
            self.on_orb_preview(self._initial_orb_style)
        if self.on_preview and self._sel_color != self._initial_color:
            self.on_preview(self._initial_color)
        if (self.on_background_preview
                and self._background_path != self._initial_background_path):
            self.on_background_preview(self._initial_background_path)
        self.hide()

    def _background_files(self) -> list[Path]:
        self._BACKGROUND_DIR.mkdir(parents=True, exist_ok=True)
        return sorted(
            (item for item in self._BACKGROUND_DIR.iterdir()
             if item.is_file() and item.suffix.casefold() in self._IMAGE_SUFFIXES),
            key=lambda item: item.name.casefold(),
        )

    def _refresh_background_gallery(self):
        while self._gallery_grid.count():
            item = self._gallery_grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._background_buttons.clear()
        choices = [("", "HUD\nDÉFAUT", QPixmap())]
        for item in self._background_files():
            thumb = QPixmap(str(item))
            choices.append((str(item), item.stem[:18], thumb))
        # Une configuration existante hors du dossier reste utilisable sans
        # empêcher l'utilisateur de la remplacer depuis la galerie.
        if self._background_path and all(path != self._background_path for path, _, _ in choices):
            legacy = Path(self._background_path)
            if legacy.is_file():
                choices.append((str(legacy), f"ACTUEL\n{legacy.stem[:12]}", QPixmap(str(legacy))))
        for index, (path, label, pixmap) in enumerate(choices):
            button = self._make_background_tile(path, label, pixmap)
            self._background_buttons[path] = button
            self._gallery_grid.addWidget(button, index // 3, index % 3)
        self._update_background_selection()

    def _make_background_tile(self, path: str, label: str, pixmap: QPixmap) -> QToolButton:
        button = QToolButton()
        button.setCheckable(True)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        button.setFixedSize(120, 92)
        button.setIconSize(QSize(104, 57))
        button.setText(label)
        button.setFont(QFont("Inter", 7, QFont.Weight.Bold))
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        if not pixmap.isNull():
            button.setIcon(QIcon(pixmap.scaled(
                QSize(104, 57), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )))
        button.setStyleSheet(f"""
            QToolButton {{ color: {C.TEXT_MED}; background: rgba(14, 23, 36, 0.92);
              border: 1px solid rgba(0, 212, 255, 0.30); border-radius: 4px; padding: 3px; }}
            QToolButton:hover {{ color: {C.WHITE}; border-color: {C.PRI};
              background: rgba(0, 212, 255, 0.12); }}
            QToolButton:checked {{ color: {C.PRI}; border: 1px solid {C.PRI};
              background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 rgba(0, 212, 255, 0.30), stop:1 rgba(255, 43, 214, 0.14)); }}
        """)
        button.clicked.connect(lambda checked=False, selected=path: self._select_background(selected))
        return button

    def _select_background(self, path: str):
        self._background_path = path
        self._update_background_selection()
        if self.on_background_preview:
            self.on_background_preview(path)

    def _update_background_selection(self):
        for path, button in self._background_buttons.items():
            button.blockSignals(True)
            button.setChecked(path == self._background_path)
            button.blockSignals(False)

    def _clear_background(self):
        self._select_background("")

    def _save(self):
        name = self._name_input.text().strip() or "ANO-GPT"
        user = self._user_input.text().strip()
        self.saved.emit(name, user, self._sel_color or DEFAULT_UI_COLOR,
                        self._background_path, self._orb_style)
        self.hide()
