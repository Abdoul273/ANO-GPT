from __future__ import annotations



from PyQt6.QtCore import (
    QRect, QSize, Qt,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QColor, QFont, QImage,
    QPainter,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QFrame, QLabel, QSizePolicy, QVBoxLayout, QWidget,
)

from ui.core.hud_paint import Hud
from ui.core.qtflags import QVideoSink
from ui.styles.theme import C

class _VideoFrameCanvas(QWidget):
    """Peint chaque image décodée dans Qt, sans surface native Wayland."""

    frame_ready = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("LocalVideoCanvas")
        self.setMinimumSize(320, 180)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self._image = QImage()
        self.sink = QVideoSink(self)
        self.sink.videoFrameChanged.connect(self._on_frame)

    def _on_frame(self, frame) -> None:
        try:
            image = frame.toImage()
        except Exception:
            return
        if image.isNull():
            return
        # Une copie détache l'image du buffer du décodeur, qui peut être
        # recyclé immédiatement après le retour du signal.
        self._image = image.copy()
        self.update()
        self.frame_ready.emit()

    def clear_frame(self) -> None:
        self._image = QImage()
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#000000"))
        if self._image.isNull():
            painter.setPen(QColor(C.TEXT_DIM))
            painter.setFont(Hud.micro_font(7, 1.2))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "DÉCODAGE DU FLUX VIDÉO…")
            painter.end()
            return
        target = QSize(self.width(), self.height())
        scaled = self._image.size().scaled(target, Qt.AspectRatioMode.KeepAspectRatio)
        rect = QRect(
            (self.width() - scaled.width()) // 2,
            (self.height() - scaled.height()) // 2,
            scaled.width(), scaled.height(),
        )
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawImage(rect, self._image)
        painter.end()


class VideoResultCard(QFrame):
    """Carte vidéo cliquable cyberpunk épurée, avec badge durée et surbrillance néon."""

    selected = pyqtSignal(int)

    def __init__(self, index: int, video: dict, parent=None):
        super().__init__(parent)
        self._index = index
        self.setObjectName("VideoResultCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumSize(250, 215)
        self.setStyleSheet(f"""
            QFrame#VideoResultCard {{
                background: rgba(4, 16, 28, 0.88);
                border: 1px solid rgba(0, 212, 255, 0.22);
                border-radius: 6px;
            }}
            QFrame#VideoResultCard:hover {{
                background: rgba(8, 28, 48, 0.96);
                border: 1px solid {C.PRI};
            }}
            QLabel {{ border: none; background: transparent; }}
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 10)
        layout.setSpacing(6)

        # Conteneur de la miniature avec badge durée superposé
        thumb_container = QWidget(self)
        thumb_container.setMinimumHeight(122)
        thumb_container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        thumb_layout = QVBoxLayout(thumb_container)
        thumb_layout.setContentsMargins(0, 0, 0, 0)

        self._thumbnail = QLabel(thumb_container)
        self._thumbnail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._thumbnail.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._thumbnail.setStyleSheet(f"""
            background: #000408;
            border: 1px solid rgba(0, 212, 255, 0.12);
            border-radius: 4px;
            color: {C.TEXT_DIM};
        """)
        pixmap = QPixmap()
        if pixmap.loadFromData(video.get("thumbnail_bytes") or b"") and not pixmap.isNull():
            self._thumbnail.setPixmap(pixmap.scaled(
                420, 220, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            ))
        else:
            self._thumbnail.setText("▶  FLUX VIDÉO")
            self._thumbnail.setFont(Hud.micro_font(8, 1.5))
        thumb_layout.addWidget(self._thumbnail)

        # Badge durée flottant
        duration_text = str(video.get("duration") or ("EN DIRECT" if video.get("live") else "00:00"))
        self._duration_badge = QLabel(duration_text, thumb_container)
        self._duration_badge.setFont(Hud.micro_font(6, 1.0))
        is_live = bool(video.get("live"))
        badge_color = C.GREEN if is_live else C.WHITE
        badge_bg = "rgba(0, 30, 15, 0.85)" if is_live else "rgba(0, 5, 12, 0.85)"
        badge_border = C.GREEN if is_live else "rgba(0, 212, 255, 0.45)"
        self._duration_badge.setStyleSheet(f"""
            color: {badge_color};
            background: {badge_bg};
            border: 1px solid {badge_border};
            border-radius: 3px;
            padding: 2px 6px;
        """)
        self._duration_badge.adjustSize()
        # Positionnement dans le coin inférieur droit de la miniature
        self._duration_badge.move(8, 8)  # Sera repositionné lors du resizeEvent

        layout.addWidget(thumb_container, stretch=1)
        self._thumb_container = thumb_container

        # Titre cyberpunk avec préfixe index néon
        title = QLabel(f"<span style='color:{C.PRI}; font-family:monospace;'>[{index + 1:02d}]</span>  {str(video.get('title') or 'Sans titre')[:85]}")
        title.setWordWrap(True)
        title.setFont(QFont("Inter", 9, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {C.WHITE};")
        title.setMaximumHeight(40)
        layout.addWidget(title)

        # Métadonnées chaîne et vues
        is_local = bool(video.get("path"))
        channel = str(video.get("channel") or video.get("folder") or
                      ("VIDÉO LOCALE" if is_local else "YOUTUBE"))
        views = video.get("views")
        views_txt = f" • {int(views):,} vues" if views else ""
        meta = QLabel(f"◈ {channel[:36]}{views_txt}")
        meta.setFont(Hud.micro_font(6, .8))
        meta.setStyleSheet(f"color: {C.TEXT_DIM};")
        layout.addWidget(meta)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "_duration_badge") and hasattr(self, "_thumb_container"):
            bw = self._duration_badge.width()
            bh = self._duration_badge.height()
            cw = self._thumb_container.width()
            ch = self._thumb_container.height()
            self._duration_badge.move(max(0, cw - bw - 6), max(0, ch - bh - 6))

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.selected.emit(self._index)
        super().mouseReleaseEvent(event)

