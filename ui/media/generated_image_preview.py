"""Aperçu central, discret et temporaire des images créées par ANO-GPT."""
from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer, QRectF
from PyQt6.QtGui import QColor, QPainter, QPixmap, QRadialGradient
from PyQt6.QtWidgets import QLabel, QVBoxLayout, QWidget


class GeneratedImagePreview(QWidget):
    """Carte sans bord, avec un halo doux ; le visionneur reste séparé."""
    _OW, _OH = 580, 610
    _DISMISS_MS = 14_000

    def __init__(self, parent=None):
        super().__init__(parent)
        self._bytes = b""
        self._path = ""
        self._prompt = ""
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 24, 30, 28)
        layout.setSpacing(8)
        self._image = QLabel(self)
        self._image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image.setStyleSheet("background: transparent;")
        layout.addWidget(self._image, stretch=1)
        self._caption = QLabel("IMAGE CRÉÉE · demande « affiche l’image créée » pour le visionneur", self)
        self._caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._caption.setWordWrap(True)
        self._caption.setStyleSheet("color: rgba(220, 248, 255, 185); background: transparent; font-size: 11px;")
        layout.addWidget(self._caption)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

    def set_image(self, prompt: str, image_bytes: bytes, path: str) -> bool:
        pixmap = QPixmap()
        if not pixmap.loadFromData(image_bytes):
            pixmap.load(path)
        if pixmap.isNull():
            return False
        self._bytes, self._path, self._prompt = bytes(image_bytes), str(path), str(prompt)
        self._image.setPixmap(pixmap.scaled(520, 530, Qt.AspectRatioMode.KeepAspectRatio,
                                            Qt.TransformationMode.SmoothTransformation))
        self._caption.setText("IMAGE CRÉÉE · demande « affiche l’image créée » pour le visionneur")
        self._timer.start(self._DISMISS_MS)
        self.update()
        return True

    def payload(self) -> dict | None:
        if not self._path and not self._bytes:
            return None
        return {"title": self._prompt[:80] or "Image créée", "source": "Azure Foundry",
                "source_url": "", "bytes": self._bytes, "path": self._path}

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # Plusieurs halos translucides donnent une bordure organique/fumée,
        # sans cadre rectangulaire ni animation coûteuse pour l'audio.
        rect = QRectF(10, 8, self.width() - 20, self.height() - 16)
        for radius, alpha, color in ((0.80, 35, QColor(0, 220, 255)), (0.56, 42, QColor(168, 72, 255))):
            halo = QRadialGradient(rect.center(), max(rect.width(), rect.height()) * radius)
            halo.setColorAt(0.45, QColor(2, 12, 24, 0))
            halo.setColorAt(0.76, QColor(color.red(), color.green(), color.blue(), alpha))
            halo.setColorAt(1.0, QColor(color.red(), color.green(), color.blue(), 0))
            painter.setBrush(halo)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(rect)
        painter.end()


class GeneratedArtifactPreview(QWidget):
    """Carte centrale légère pour une vidéo ou un document tout juste créé."""
    _OW, _OH = 520, 330
    _DISMISS_MS = 12_000

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 34, 40, 34)
        layout.setSpacing(10)
        self._icon = QLabel(self); self._icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._icon.setStyleSheet("color: rgba(114, 235, 255, 235); background: transparent; font-size: 76px;")
        layout.addWidget(self._icon, stretch=1)
        self._title = QLabel(self); self._title.setAlignment(Qt.AlignmentFlag.AlignCenter); self._title.setWordWrap(True)
        self._title.setStyleSheet("color: rgba(241, 252, 255, 245); background: transparent; font-size: 16px; font-weight: 600;")
        layout.addWidget(self._title)
        self._hint = QLabel(self); self._hint.setAlignment(Qt.AlignmentFlag.AlignCenter); self._hint.setWordWrap(True)
        self._hint.setStyleSheet("color: rgba(192, 224, 238, 180); background: transparent; font-size: 11px;")
        layout.addWidget(self._hint)
        self._timer = QTimer(self); self._timer.setSingleShot(True); self._timer.timeout.connect(self.hide)

    def show_artifact(self, kind: str, title: str, path: str) -> None:
        is_video = kind == "video"
        self._icon.setText("▶" if is_video else "▤")
        self._title.setText(title[:110] or ("Vidéo créée" if is_video else "Document créé"))
        self._hint.setText("VIDÉO CRÉÉE · demande à l'ouvrir pour la regarder" if is_video
                           else "DOCUMENT CRÉÉ · demande à l'ouvrir pour le consulter")
        self.setToolTip(path)
        self._timer.start(self._DISMISS_MS)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self); painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(8, 6, self.width() - 16, self.height() - 12)
        for radius, alpha, color in ((0.84, 38, QColor(0, 220, 255)), (0.52, 48, QColor(220, 65, 255))):
            halo = QRadialGradient(rect.center(), max(rect.width(), rect.height()) * radius)
            halo.setColorAt(0.38, QColor(3, 14, 25, 0)); halo.setColorAt(0.75, QColor(color.red(), color.green(), color.blue(), alpha))
            halo.setColorAt(1.0, QColor(color.red(), color.green(), color.blue(), 0))
            painter.setPen(Qt.PenStyle.NoPen); painter.setBrush(halo); painter.drawEllipse(rect)
        painter.end()
