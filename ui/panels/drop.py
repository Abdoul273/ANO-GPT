from __future__ import annotations

import math
import time
from pathlib import Path


from PyQt6.QtCore import (
    QPointF, QRectF, Qt,
    QTimer, pyqtSignal,
)
from PyQt6.QtGui import (
    QBrush, QColor, QDragEnterEvent, QDropEvent, QFont, QPainter,
    QPen,
)
from PyQt6.QtWidgets import (
    QFileDialog, QVBoxLayout, QWidget,
)

from ui.core.qtflags import _OS
from ui.panels.file_chip import _FILE_ICONS, _file_category, _fmt_size
from ui.styles.theme import C, qcol

class GlobalDropOverlay(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.setAcceptDrops(True)
        self._pulse = 0.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._animate)
        self._timer.start(40)

    def _animate(self):
        self._pulse = (self._pulse + 0.05) % (2 * math.pi)
        if self.isVisible():
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()

        # Fond translucide sombre style JARVIS
        p.fillRect(0, 0, W, H, QColor(0, 6, 10, 220))

        box_w = min(560, W - 60)
        box_h = min(260, H - 60)
        box_x = (W - box_w) / 2
        box_y = (H - box_h) / 2
        rect = QRectF(box_x, box_y, box_w, box_h)

        glow_alpha = int(140 + 80 * math.sin(self._pulse))
        pen = QPen(QColor(0, 212, 255, glow_alpha), 2, Qt.PenStyle.DashLine)
        pen.setDashPattern([8, 6])
        p.setPen(pen)
        p.setBrush(QColor(10, 26, 38, 190))
        p.drawRoundedRect(rect, 16.0, 16.0)

        p.setFont(QFont("Inter", 34))
        p.setPen(QColor(C.PRI))
        p.drawText(rect.adjusted(0, 30, 0, -120), Qt.AlignmentFlag.AlignCenter, "📥")

        p.setFont(QFont("Inter", 14, QFont.Weight.Bold))
        p.setPen(QColor(C.WHITE))
        p.drawText(rect.adjusted(0, 95, 0, -60), Qt.AlignmentFlag.AlignCenter, "Déposez votre fichier ici")

        p.setFont(QFont("Inter", 10))
        p.setPen(QColor(C.TEXT_DIM))
        p.drawText(rect.adjusted(0, 135, 0, -20), Qt.AlignmentFlag.AlignCenter, "ANO-GPT prend en charge images, documents, audio, vidéo, archives et code.")
        p.end()

class FileDropZone(QWidget):
    file_selected = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(86)
        self._current_file: str | None = None
        self._hovering  = False
        self._drag_over = False
        self._dash_offset = 0.0
        self._pulse = 0.0  # pour animation de pulsation
        self._anim_tmr = QTimer(self)
        self._anim_tmr.timeout.connect(self._animate)
        self._anim_tmr.start(40)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._canvas = _DropCanvas(self)
        layout.addWidget(self._canvas)

    def _animate(self):
        self._dash_offset = (self._dash_offset + 0.8) % 20
        self._pulse = 0.5 + 0.5 * math.sin(time.time() * 2.5)
        self._canvas.update()

    def dragEnterEvent(self, e: QDragEnterEvent):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self._drag_over = True; self._canvas.update()

    def dragLeaveEvent(self, e):
        self._drag_over = False; self._canvas.update()

    def dropEvent(self, e: QDropEvent):
        self._drag_over = False
        urls = e.mimeData().urls()
        if urls:
            path = urls[0].toLocalFile()
            if Path(path).is_file():
                self._set_file(path)
        self._canvas.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._browse()

    def enterEvent(self, e):
        self._hovering = True; self._canvas.update()

    def leaveEvent(self, e):
        self._hovering = False; self._canvas.update()

    def current_file(self) -> str | None:
        return self._current_file

    def clear_file(self):
        self._current_file = None; self._canvas.update()

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Sélectionner un fichier pour ANO-GPT", str(Path.home()),
            "Tous les fichiers (*.*);;"
            "Images (*.jpg *.jpeg *.png *.gif *.webp *.bmp *.svg);;"
            "Documents (*.pdf *.docx *.txt *.md *.pptx);;"
            "Données (*.csv *.xlsx *.json *.xml);;"
            "Code (*.py *.js *.ts *.html *.css *.java *.cpp *.go);;"
            "Audio (*.mp3 *.wav *.ogg *.m4a *.aac *.flac);;"
            "Video (*.mp4 *.avi *.mov *.mkv *.wmv *.webm);;"
            "Archives (*.zip *.rar *.tar *.gz *.7z)",
        )
        if path:
            self._set_file(path)

    def _set_file(self, path: str):
        self._current_file = path
        self._canvas.update()
        self.file_selected.emit(path)


class _DropCanvas(QWidget):
    def __init__(self, zone: FileDropZone):
        super().__init__(zone)
        self._z = zone

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        z    = self._z
        W, H = self.width(), self.height()
        pad  = 1
        rect = QRectF(pad, pad, W - pad * 2, H - pad * 2)
        radius = 11.0

        # Surface : même élévation que les autres cartes du panneau
        if z._drag_over:
            bg_col = qcol(C.PRI_GHO, 235)
        elif z._hovering:
            bg_col = qcol(C.ELEV2)
        else:
            bg_col = qcol(C.ELEV1)
        p.setBrush(QBrush(bg_col)); p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(rect, radius, radius)

        # Contour : plein au repos, tirets animés seulement pendant le glisser
        if z._current_file:
            p.setPen(QPen(qcol(C.GREEN, 110), 1.2))
        elif z._drag_over:
            pen = QPen(qcol(C.PRI, 230), 1.6, Qt.PenStyle.DashLine)
            pen.setDashOffset(z._dash_offset)
            p.setPen(pen)
        elif z._hovering:
            p.setPen(QPen(qcol(C.PRI_DIM, 190), 1.2))
        else:
            p.setPen(QPen(qcol(C.BORDER, 130), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect, radius, radius)

        if z._current_file:   self._paint_file(p, W, H)
        elif z._drag_over:    self._paint_drag_over(p, W, H)
        else:                 self._paint_idle(p, W, H, z._hovering)
        p.end()

    def _paint_idle(self, p, W, H, hover):
        cx, cy = W / 2, H / 2 - 6
        col = qcol(C.PRI if hover else C.PRI_DIM)
        # Flèche montante, tracé fin et arrondi
        pen = QPen(col, 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawLine(QPointF(cx, cy - 11), QPointF(cx, cy + 5))
        p.drawLine(QPointF(cx - 6, cy - 5), QPointF(cx, cy - 11))
        p.drawLine(QPointF(cx + 6, cy - 5), QPointF(cx, cy - 11))
        p.drawLine(QPointF(cx - 11, cy + 10), QPointF(cx + 11, cy + 10))
        p.setFont(QFont("Inter", 8))
        p.setPen(QPen(qcol(C.TEXT if hover else C.TEXT_DIM), 1))
        p.drawText(QRectF(0, cy + 18, W, 16), Qt.AlignmentFlag.AlignCenter,
                   "Déposer un fichier  ·  ou cliquer")

    def _paint_drag_over(self, p, W, H):
        cy = H / 2
        p.setFont(QFont("Inter", 20))
        p.setPen(QPen(qcol(C.PRI), 1))
        p.drawText(QRectF(0, cy - 24, W, 32), Qt.AlignmentFlag.AlignCenter, "⬇")
        p.setFont(QFont("Inter", 8, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.PRI), 1))
        p.drawText(QRectF(0, cy + 12, W, 16), Qt.AlignmentFlag.AlignCenter, "Relâchez pour charger")

    def _paint_file(self, p, W, H):
        path = Path(self._z._current_file)
        cat  = _file_category(path)
        icon, icon_col = _FILE_ICONS.get(cat, _FILE_ICONS["unknown"])
        size_str = _fmt_size(path.stat().st_size)
        ext_str  = path.suffix.upper().lstrip(".") or "FILE"

        block_x, block_w = 10, 60
        p.setFont(QFont("Segoe UI Emoji", 22) if _OS == "Windows" else QFont("Inter", 22))
        p.setPen(QPen(qcol(icon_col), 1))
        p.drawText(QRectF(block_x, 0, block_w, H), Qt.AlignmentFlag.AlignCenter, icon)

        tx = block_x + block_w + 6
        tw = W - tx - 38

        p.setFont(QFont("Inter", 8, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.WHITE), 1))
        name = path.name if len(path.name) <= 34 else path.name[:31] + "..."
        p.drawText(QRectF(tx, H * 0.18, tw, 16),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)

        p.setFont(QFont("Inter", 7))
        p.setPen(QPen(qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(tx, H * 0.18 + 18, tw, 14),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   f"{ext_str}  ·  {size_str}")

        p.setFont(QFont("Inter", 6))
        p.setPen(QPen(qcol("#1e5c6a"), 1))
        par = str(path.parent)
        if len(par) > 42: par = "…" + par[-41:]
        p.drawText(QRectF(tx, H * 0.18 + 34, tw, 12),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, par)

        p.setFont(QFont("Inter", 9, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.RED, 180), 1))
        p.drawText(QRectF(W - 34, 0, 28, H), Qt.AlignmentFlag.AlignCenter, "✕")

    def mousePressEvent(self, e):
        z = self._z
        if z._current_file and e.pos().x() > self.width() - 34:
            z.clear_file()
        else:
            z.mousePressEvent(e)
