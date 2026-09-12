from __future__ import annotations

import json
import math
import os
import platform
import random
import re
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

import psutil

from PyQt6.QtCore import (
    QBuffer, QByteArray, QEasingCurve, QEvent, QIODevice, QLineF, QPointF,
    QPropertyAnimation, QRect, QRectF, QSize, Qt, QThread, QTimer, QUrl,
    pyqtProperty, pyqtSignal,
)
from PyQt6.QtGui import (
    QBrush, QColor, QConicalGradient, QDragEnterEvent, QDropEvent, QFont, QImage,
    QDesktopServices, QFontDatabase, QFontMetrics, QFontMetricsF, QIcon, QImageReader,
    QKeySequence, QLinearGradient, QPainter,
    QPainterPath, QPen, QPixmap, QPolygonF, QRadialGradient, QRegion, QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QFrame, QGraphicsOpacityEffect, QGridLayout,
    QHBoxLayout, QLabel, QLayout, QLineEdit, QProgressBar,
    QMainWindow, QPushButton, QScrollArea, QSizePolicy, QSlider,
    QTextBrowser, QTextEdit, QVBoxLayout, QWidget, QSplashScreen,
)

from ui.core.hud_button import HudButton
from ui.core.hud_paint import Hud
from ui.styles.theme import C, qcol

class _GalleryImageView(QLabel):
    """Image principale conservant son ratio et sa source haute définition."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._original = QPixmap()
        self._scaled_for = QSize()
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(320, 220)
        self.setStyleSheet(
            f"background: rgba(0, 3, 8, 0.78); border: 1px solid {C.BORDER_B};"
        )

    def set_image(self, pixmap: QPixmap) -> None:
        self._original = pixmap if pixmap is not None else QPixmap()
        self._scaled_for = QSize()
        if self._original.isNull():
            self.clear()
            return
        self._rescale()

    def clear_image(self) -> None:
        self._original = QPixmap()
        self._scaled_for = QSize()
        self.clear()

    def _rescale(self) -> None:
        target = self.size() - QSize(24, 24)
        if (self._original.isNull() or target.width() < 2 or target.height() < 2
                or target == self._scaled_for):
            return
        self.setPixmap(self._original.scaled(
            target,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))
        self._scaled_for = target

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._rescale()


class ImageGalleryOverlay(QWidget):
    """Galerie cyberpunk plein écran alimentée sans ouvrir de navigateur."""

    closed = pyqtSignal()
    _MAX_IMAGES = 8
    _MAX_ENCODED_BYTES = 12 * 1024 * 1024
    _MAX_IMAGE_EDGE = 1920
    _MAX_PIXELS = 32_000_000

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setObjectName("ImageGalleryOverlay")
        self._images: list[dict] = []
        self._index = 0
        self._scan = 0.0
        self._closing = False
        self._fade = 1.0

        # QGraphicsOpacityEffect rasterise tout le widget (photo 1920p comprise)
        # dans un pixmap hors écran à chaque frame. Sur Qt 6.11 + xcb, le
        # QPainter de paintEvent se détruit alors pendant ce capture et
        # SIGSEGV dans QPainter::end — l'application disparaissait pile à
        # l'affichage d'une photo, puis le superviseur la relançait.
        self.setGraphicsEffect(None)
        self._anim = QPropertyAnimation(self, b"fade", self)
        self._anim.finished.connect(self._on_animation_finished)
        self._fx = QTimer(self)
        self._fx.setInterval(120)
        self._fx.timeout.connect(self._tick)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 18, 24, 20)
        outer.setSpacing(10)

        header = QHBoxLayout()
        header.setContentsMargins(8, 3, 8, 3)
        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        self._kicker = QLabel("VISUAL.INTELLIGENCE // IMAGE MATRIX")
        self._kicker.setFont(Hud.micro_font(6, 1.8))
        self._kicker.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        title_col.addWidget(self._kicker)
        self._title = QLabel("◈  GALERIE VISUELLE")
        self._title.setFont(Hud.micro_font(10, 1.3))
        self._title.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        title_col.addWidget(self._title)
        header.addLayout(title_col)
        header.addStretch()
        self._counter = QLabel("0 / 0")
        self._counter.setFont(Hud.micro_font(7, 1.2))
        self._counter.setStyleSheet(f"color: {C.GREEN}; background: transparent;")
        header.addWidget(self._counter)
        close_btn = HudButton("FERMER", icon="x", accent=C.TEXT_DIM,
                              hover_accent=C.RED, size=11)
        close_btn.setFixedSize(96, 30)
        close_btn.clicked.connect(self.close_gallery)
        header.addWidget(close_btn)
        outer.addLayout(header)

        self._view = _GalleryImageView()
        outer.addWidget(self._view, stretch=1)

        meta = QHBoxLayout()
        meta.setContentsMargins(8, 0, 8, 0)
        self._image_title = QLabel("")
        self._image_title.setFont(QFont("Inter", 9, QFont.Weight.Bold))
        self._image_title.setStyleSheet(f"color: {C.WHITE}; background: transparent;")
        self._image_title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        meta.addWidget(self._image_title, stretch=1)
        self._dimensions = QLabel("")
        self._dimensions.setFont(Hud.micro_font(6, 1.0))
        self._dimensions.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        meta.addWidget(self._dimensions)
        self._source = QPushButton("SOURCE ↗")
        self._source.setCursor(Qt.CursorShape.PointingHandCursor)
        self._source.setFont(Hud.micro_font(7, .8))
        self._source.setStyleSheet(f"""
            QPushButton {{ color: {C.PRI}; background: {C.PRI_GHO};
                border: 1px solid {C.BORDER_B}; padding: 6px 12px; }}
            QPushButton:hover {{ color: {C.WHITE}; border-color: {C.PRI}; }}
            QPushButton:disabled {{ color: {C.TEXT_DIM}; border-color: {C.BORDER}; }}
        """)
        self._source.clicked.connect(self._open_source)
        meta.addWidget(self._source)
        outer.addLayout(meta)

        nav = QHBoxLayout()
        nav.setContentsMargins(8, 0, 8, 0)
        nav.setSpacing(8)
        self._prev = HudButton("PRÉC.", icon="chevron-left", accent=C.PRI, size=11)
        self._prev.setFixedSize(88, 58)
        self._prev.clicked.connect(lambda: self.select(self._index - 1))
        nav.addWidget(self._prev)
        self._thumb_layout = QHBoxLayout()
        self._thumb_layout.setSpacing(7)
        nav.addLayout(self._thumb_layout, stretch=1)
        self._next = HudButton("SUIV.", icon="chevron-right", accent=C.PRI, size=11)
        self._next.setFixedSize(88, 58)
        self._next.clicked.connect(lambda: self.select(self._index + 1))
        nav.addWidget(self._next)
        outer.addLayout(nav)
        self.hide()

    def _get_fade(self) -> float:
        return self._fade

    def _set_fade(self, value: float) -> None:
        self._fade = max(0.0, min(1.0, float(value)))
        self.update()

    fade = pyqtProperty(float, _get_fade, _set_fade)

    @classmethod
    def _decode_pixmap(cls, data: bytes) -> QPixmap:
        """Décode à une taille et un format sûrs pour le backing-store Qt."""
        payload = bytes(data)
        if not payload or len(payload) > cls._MAX_ENCODED_BYTES:
            return QPixmap()
        buffer = QBuffer()
        buffer.setData(QByteArray(payload))
        if not buffer.open(QIODevice.OpenModeFlag.ReadOnly):
            return QPixmap()
        reader = QImageReader(buffer)
        reader.setAutoTransform(True)
        reader.setDecideFormatFromContent(True)
        size = reader.size()
        if size.isValid():
            if size.width() * size.height() > cls._MAX_PIXELS:
                return QPixmap()
            if max(size.width(), size.height()) > cls._MAX_IMAGE_EDGE:
                reader.setScaledSize(size.scaled(
                    QSize(cls._MAX_IMAGE_EDGE, cls._MAX_IMAGE_EDGE),
                    Qt.AspectRatioMode.KeepAspectRatio,
                ))
        image = reader.read()
        if image.isNull() or image.width() < 2 or image.height() < 2:
            return QPixmap()
        target = (
            QImage.Format.Format_ARGB32_Premultiplied
            if image.hasAlphaChannel()
            else QImage.Format.Format_RGB32
        )
        if image.format() != target:
            image = image.convertToFormat(target)
        return QPixmap.fromImage(image)

    def show_gallery(self, query: str, images: list[dict]) -> bool:
        """Prépare des images sûres pour le thread Qt.

        Les réponses Web peuvent contenir un fichier invalide ou une image de
        dizaines de mégapixels. Les conserver telles quelles surchargeait le
        backing-store Qt et pouvait faire tomber l'application au moment de
        l'affichage. La galerie borne donc le décodage retenu et le format GPU.
        """
        prepared: list[dict] = []
        for item in images[:self._MAX_IMAGES]:
            if not isinstance(item, dict):
                continue
            data = item.get("bytes") or b""
            if not isinstance(data, (bytes, bytearray)) or not data:
                continue
            pixmap = self._decode_pixmap(bytes(data))
            if pixmap.isNull():
                continue
            thumb = pixmap.scaled(
                QSize(74, 48),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            prepared.append({
                "title": str(item.get("title") or "Image"),
                "source": str(item.get("source") or "WEB"),
                "source_url": str(item.get("source_url") or ""),
                "width": int(item.get("width") or pixmap.width()),
                "height": int(item.get("height") or pixmap.height()),
                "_pixmap": pixmap,
                "_thumb": thumb,
            })
        if not prepared:
            return False

        self._images = prepared
        self._index = 0
        self._title.setText(f"◈  GALERIE — {query.upper()}"[:72])
        self._rebuild_thumbnails()
        self.select(0)
        self._closing = False
        self._fade = 0.0
        self.show()
        self.raise_()
        self._anim.stop()
        self._anim.setDuration(300)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.start()
        self._fx.start()
        return True

    def _rebuild_thumbnails(self) -> None:
        while self._thumb_layout.count():
            item = self._thumb_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for index, image in enumerate(self._images):
            button = QPushButton()
            button.setObjectName(f"gallery-thumb-{index}")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFixedHeight(58)
            button.setMinimumWidth(56)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            button.setIcon(QIcon(image.get("_thumb") or image["_pixmap"]))
            button.setIconSize(QSize(74, 48))
            button.clicked.connect(lambda _=False, idx=index: self.select(idx))
            self._thumb_layout.addWidget(button)

    def _style_thumbnails(self) -> None:
        for index in range(self._thumb_layout.count()):
            button = self._thumb_layout.itemAt(index).widget()
            if button is None:
                continue
            active = index == self._index
            border = C.PRI if active else C.BORDER
            glow = C.PRI_GHO if active else "rgba(0, 3, 8, 0.72)"
            button.setStyleSheet(f"""
                QPushButton {{ background: {glow}; border: {'2px' if active else '1px'} solid {border};
                    padding: 3px; }}
                QPushButton:hover {{ border-color: {C.PRI}; background: {C.ELEV1}; }}
            """)

    def select(self, index: int) -> None:
        if not self._images:
            return
        self._index = index % len(self._images)
        item = self._images[self._index]
        self._view.set_image(item["_pixmap"])
        self._counter.setText(f"{self._index + 1:02d} / {len(self._images):02d}")
        self._image_title.setText(str(item.get("title") or "Image")[:110])
        self._dimensions.setText(
            f"{item.get('width', 0)}×{item.get('height', 0)}  //  {item.get('source') or 'WEB'}"
        )
        self._source.setEnabled(bool(item.get("source_url")))
        many = len(self._images) > 1
        self._prev.setEnabled(many)
        self._next.setEnabled(many)
        self._style_thumbnails()

    def _open_source(self) -> None:
        if not self._images:
            return
        url = str(self._images[self._index].get("source_url") or "")
        if url.startswith(("https://", "http://")):
            from core.browser_policy import open_chrome
            open_chrome(url)

    def close_gallery(self) -> None:
        if self._closing or not self.isVisible():
            return
        self._closing = True
        self._anim.stop()
        self._anim.setDuration(280)
        self._anim.setEasingCurve(QEasingCurve.Type.InCubic)
        self._anim.setStartValue(self._fade)
        self._anim.setEndValue(0.0)
        self._anim.start()
        # Filet si le signal `finished` n'arrive pas (pile hors écran, animation
        # interrompue). Idempotent : `_finish_close` ne fait rien si déjà sorti.
        QTimer.singleShot(310, self._finish_close)

    def dismiss_now(self) -> None:
        """Retire la galerie lorsqu'une autre surface immersive prend la main."""
        if not self.isVisible() and not self._images:
            return
        self._anim.stop()
        self._fx.stop()
        self._closing = False
        self._fade = 1.0
        self.hide()
        self._images.clear()
        self._view.clear_image()
        self.closed.emit()

    def _on_animation_finished(self) -> None:
        self._finish_close()

    def _finish_close(self) -> None:
        if not self._closing:
            return
        self._closing = False
        self._fx.stop()
        self._fade = 1.0
        self.hide()
        self._images.clear()
        self._view.clear_image()
        self.closed.emit()

    def _tick(self) -> None:
        if not self.isVisible():
            self._fx.stop()
            return
        self._scan = (self._scan + (5.0 if self._closing else 1.25)) % Hud.SCAN_PERIOD
        self.update()

    def paintEvent(self, event):
        if self.width() < 8 or self.height() < 8:
            return
        painter = QPainter(self)
        if not painter.isActive():
            return
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            if self._fade < 0.999:
                painter.setOpacity(self._fade)
            rect = QRectF(3, 3, self.width() - 6, self.height() - 6)
            Hud.chassis(painter, rect, accent=qcol(C.PRI), scan=self._scan)
            Hud.tick(painter, rect, 64)
        except Exception:
            # Une erreur Python dans paintEvent ne doit jamais laisser un
            # QPainter actif : son destructeur SIGSEGV sur cette pile Qt.
            pass
        finally:
            if painter.isActive():
                painter.end()
