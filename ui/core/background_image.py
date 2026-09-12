"""Calque d'image d'arrière-plan, indépendant des panneaux du HUD."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QImageReader, QPainter, QPixmap
from PyQt6.QtWidgets import QWidget


class BackgroundImage(QWidget):
    """Peint une photo en mode *cover*, derrière et seulement derrière le HUD."""

    def __init__(self, path: str = "", parent=None):
        super().__init__(parent)
        self._pixmap = QPixmap()
        self._scaled = QPixmap()
        self._scaled_for = QSize()
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setStyleSheet("background: transparent;")
        self.set_image(path)

    def set_image(self, path: str) -> bool:
        candidate = Path(str(path or "")).expanduser()
        pixmap = self._load_pixmap(candidate) if candidate.is_file() else QPixmap()
        if candidate and not pixmap.isNull():
            self._pixmap = pixmap
            self._scaled_for = QSize()
            self._refresh_scaled()
            self.update()
            return True
        self._pixmap = QPixmap()
        self._scaled = QPixmap()
        self._scaled_for = QSize()
        self.update()
        return not str(path or "").strip()

    @property
    def has_image(self) -> bool:
        return not self._pixmap.isNull()

    @staticmethod
    def _load_pixmap(path: Path) -> QPixmap:
        """Décode à une taille sûre : pas de pic mémoire avec une photo 4K."""
        reader = QImageReader(str(path))
        reader.setAutoTransform(True)
        size = reader.size()
        # 1920×1080 est largement suffisant pour un fond plein écran et évite
        # que le changement d'une photo lourde bloque le thread Qt/voix.
        if size.isValid() and (size.width() > 1920 or size.height() > 1080):
            reader.setScaledSize(size.scaled(
                QSize(1920, 1080), Qt.AspectRatioMode.KeepAspectRatio,
            ))
        image = reader.read()
        return QPixmap.fromImage(image) if not image.isNull() else QPixmap()

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._refresh_scaled()

    def _refresh_scaled(self) -> None:
        target_size = self.size()
        if (self._pixmap.isNull() or target_size.isEmpty()
                or target_size == self._scaled_for):
            return
        self._scaled = self._pixmap.scaled(
            target_size,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._scaled_for = target_size

    def paintEvent(self, event):  # noqa: N802
        if self._pixmap.isNull() or self.width() < 1 or self.height() < 1:
            return
        self._refresh_scaled()
        if self._scaled.isNull():
            return
        x = (self.width() - self._scaled.width()) // 2
        y = (self.height() - self._scaled.height()) // 2
        painter = QPainter(self)
        painter.drawPixmap(x, y, self._scaled)
        painter.end()
