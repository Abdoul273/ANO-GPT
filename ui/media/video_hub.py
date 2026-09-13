from __future__ import annotations



from PyQt6.QtCore import (
    QRectF, Qt,
    QTimer, pyqtSignal,
)
from PyQt6.QtGui import (
    QFont, QPainter,
)
from PyQt6.QtWidgets import (
    QFrame, QGridLayout,
    QHBoxLayout, QLabel, QPushButton, QScrollArea, QSlider,
    QVBoxLayout, QWidget,
)

from ui.core.hud_button import HudButton
from ui.core.hud_paint import Hud
from ui.media.video_playback import _VideoPlaybackMixin
from ui.panels.music_widgets import _SeekSlider
from ui.styles.theme import C, qcol

class VideoHubOverlay(_VideoPlaybackMixin, QWidget):
    """Galerie et lecteur YouTube intégrés, sans fenêtre navigateur externe."""

    selected = pyqtSignal(int)
    closed = pyqtSignal()
    _LOCAL_VIDEO_EXT = {
        ".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv",
        ".m4v", ".ts", ".mpg", ".mpeg",
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("VideoHubOverlay")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._videos: list[dict] = []
        self._index = -1
        self._scan = 0.0
        self._mode = "results"
        self._current_source = ""
        self._query = ""

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 18, 24, 20)
        outer.setSpacing(10)
        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        kicker = QLabel("MEDIA.INTELLIGENCE // VIDEO MATRIX")
        kicker.setFont(Hud.micro_font(6, 1.8))
        kicker.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        titles.addWidget(kicker)
        self._title = QLabel("◈  RÉSULTATS VIDÉO")
        self._title.setFont(Hud.micro_font(10, 1.3))
        self._title.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        titles.addWidget(self._title)
        header.addLayout(titles)
        header.addStretch()
        self._badge = QLabel("LECTEUR NATIF // PRÊT")
        self._badge.setFont(Hud.micro_font(6, 1.2))
        self._badge.setStyleSheet(f"color: {C.GREEN}; background: transparent;")
        header.addWidget(self._badge)
        close_button = HudButton("FERMER", icon="x", accent=C.TEXT_DIM,
                                 hover_accent=C.RED, size=11)
        close_button.setFixedSize(96, 30)
        close_button.clicked.connect(self.close_video)
        header.addWidget(close_button)
        outer.addLayout(header)

        self._results = QScrollArea()
        self._results.setWidgetResizable(True)
        self._results.setFrameShape(QFrame.Shape.NoFrame)
        self._results.setStyleSheet(f"""
            QScrollArea {{ background: rgba(0, 3, 8, 0.68); border: 1px solid {C.BORDER}; }}
            QScrollBar:vertical {{ background: {C.PANEL}; width: 8px; }}
            QScrollBar::handle:vertical {{ background: {C.PRI_DIM}; min-height: 30px; }}
        """)
        self._results_body = QWidget()
        self._results_body.setStyleSheet("background: transparent;")
        self._grid = QGridLayout(self._results_body)
        self._grid.setContentsMargins(12, 12, 12, 12)
        self._grid.setSpacing(10)
        self._results.setWidget(self._results_body)
        outer.addWidget(self._results, stretch=1)

        self._player_panel = QWidget()
        self._player_panel.setStyleSheet("background: transparent;")
        self._player_layout = QVBoxLayout(self._player_panel)
        self._player_layout.setContentsMargins(0, 0, 0, 0)
        self._player_layout.setSpacing(8)
        # WebEngine est lourd et ne doit pas être créé au démarrage. Il est
        # instancié uniquement à la première lecture, puis réutilisé.
        self._web = None
        self._local_player = None
        self._audio_output = None
        self._video_widget = None
        self._web_placeholder = QLabel("◈  INITIALISATION DU FLUX VIDÉO CYBER-NEURAL…  ◈")
        self._web_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._web_placeholder.setFont(Hud.micro_font(8, 1.4))
        self._web_placeholder.setStyleSheet(f"""
            QLabel {{
                color: {C.PRI};
                background: #02060c;
                border: 1px dashed {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self._player_layout.addWidget(self._web_placeholder, stretch=1)

        # En-tête métadonnées du flux en cours
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        kicker_stream = QLabel("SIGNAL REÇU // LECTURE ACTIVE")
        kicker_stream.setFont(Hud.micro_font(6, 1.4))
        kicker_stream.setStyleSheet(f"color: {C.GREEN}; background: transparent;")
        title_box.addWidget(kicker_stream)

        self._now_title = QLabel("")
        self._now_title.setFont(QFont("Inter", 10, QFont.Weight.Bold))
        self._now_title.setStyleSheet(f"color: {C.WHITE}; background: transparent;")
        self._now_title.setWordWrap(True)
        title_box.addWidget(self._now_title)
        self._player_layout.addLayout(title_box)

        # Deck de contrôle Cyberpunk flottant
        self._deck_frame = QFrame()
        self._deck_frame.setObjectName("cyberDeck")
        self._deck_frame.setStyleSheet(f"""
            QFrame#cyberDeck {{
                background: rgba(8, 16, 26, 0.92);
                border: 1px solid {C.BORDER_B};
                border-radius: 8px;
            }}
        """)
        deck_layout = QVBoxLayout(self._deck_frame)
        deck_layout.setContentsMargins(14, 10, 14, 10)
        deck_layout.setSpacing(8)

        # Timeline Cyberpunk avec slider néon
        timeline = QHBoxLayout()
        timeline.setSpacing(10)
        self._elapsed = QLabel("00:00")
        self._elapsed.setFont(Hud.micro_font(7, 1.0))
        self._elapsed.setStyleSheet(f"color: {C.PRI}; background: transparent; font-weight: bold;")
        timeline.addWidget(self._elapsed)

        self._progress = _SeekSlider(Qt.Orientation.Horizontal)
        self._progress.setRange(0, 1000)
        self._progress.setStyleSheet(f"""
            QSlider::groove:horizontal {{
                border: 1px solid {C.BORDER};
                height: 5px;
                background: #030810;
                border-radius: 2px;
            }}
            QSlider::sub-page:horizontal {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #005577, stop:0.5 #00aacc, stop:1 {C.PRI});
                border-radius: 2px;
            }}
            QSlider::handle:horizontal {{
                background: {C.PRI};
                border: 1px solid #ffffff;
                width: 12px;
                margin-top: -4px;
                margin-bottom: -4px;
                border-radius: 6px;
            }}
            QSlider::handle:horizontal:hover {{
                background: #ffffff;
                border: 1px solid {C.PRI};
            }}
        """)
        self._progress.sliderReleased.connect(self._seek_from_progress)
        self._progress.sliderMoved.connect(self._seek_to_progress_value)
        timeline.addWidget(self._progress, stretch=1)

        self._duration_label = QLabel("00:00")
        self._duration_label.setFont(Hud.micro_font(7, 1.0))
        self._duration_label.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        timeline.addWidget(self._duration_label)
        deck_layout.addLayout(timeline)

        # Boutons d'action Cyberpunk
        controls = QHBoxLayout()
        controls.setSpacing(6)

        btn_qss = f"""
            QPushButton {{
                color: {C.PRI};
                background: rgba(0, 212, 255, 0.07);
                border: 1px solid {C.BORDER_B};
                border-radius: 4px;
                font-family: 'Inter';
                font-weight: 600;
            }}
            QPushButton:hover {{
                color: #ffffff;
                background: rgba(0, 212, 255, 0.22);
                border-color: {C.PRI};
            }}
            QPushButton:pressed {{
                background: rgba(0, 212, 255, 0.35);
            }}
        """

        btn_toggle_qss = f"""
            QPushButton {{
                color: {C.WHITE};
                background: rgba(0, 212, 255, 0.18);
                border: 1px solid {C.PRI};
                border-radius: 4px;
                font-family: 'Inter';
                font-weight: 700;
            }}
            QPushButton:hover {{
                color: #ffffff;
                background: rgba(0, 212, 255, 0.32);
                border-color: #ffffff;
            }}
            QPushButton:pressed {{
                background: rgba(0, 212, 255, 0.45);
            }}
        """

        for label, action in (("⏮", "previous"), ("−10", "back")):
            button = QPushButton(label)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFixedSize(48, 32)
            button.setFont(QFont("Inter", 9, QFont.Weight.Bold))
            button.setStyleSheet(btn_qss)
            button.clicked.connect(lambda _=False, name=action: self.control(name))
            controls.addWidget(button)

        self._btn_toggle = QPushButton("▶ / Ⅱ")
        self._btn_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_toggle.setFixedSize(76, 32)
        self._btn_toggle.setFont(QFont("Inter", 9, QFont.Weight.Bold))
        self._btn_toggle.setStyleSheet(btn_toggle_qss)
        self._btn_toggle.clicked.connect(lambda: self.control("toggle"))
        controls.addWidget(self._btn_toggle)

        for label, action in (("+10", "forward"), ("⏭", "next")):
            button = QPushButton(label)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFixedSize(48, 32)
            button.setFont(QFont("Inter", 9, QFont.Weight.Bold))
            button.setStyleSheet(btn_qss)
            button.clicked.connect(lambda _=False, name=action: self.control(name))
            controls.addWidget(button)

        controls.addSpacing(10)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setStyleSheet(f"color: {C.BORDER}; max-width: 1px; margin: 4px 0;")
        controls.addWidget(sep)

        controls.addSpacing(10)

        # Section Volume
        self._last_volume = 80
        self._btn_mute = QPushButton("🔊")
        self._btn_mute.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_mute.setFixedSize(36, 32)
        self._btn_mute.setFont(QFont("Inter", 10))
        self._btn_mute.setStyleSheet(btn_qss)
        self._btn_mute.clicked.connect(self._toggle_mute)
        controls.addWidget(self._btn_mute)

        self._volume = QSlider(Qt.Orientation.Horizontal)
        self._volume.setRange(0, 100)
        self._volume.setValue(80)
        self._volume.setMaximumWidth(130)
        self._volume.setStyleSheet(f"""
            QSlider::groove:horizontal {{
                border: 1px solid {C.BORDER};
                height: 4px;
                background: #030810;
                border-radius: 2px;
            }}
            QSlider::sub-page:horizontal {{
                background: {C.PRI};
                border-radius: 2px;
            }}
            QSlider::handle:horizontal {{
                background: {C.WHITE};
                border: 1px solid {C.PRI};
                width: 10px;
                margin-top: -3px;
                margin-bottom: -3px;
                border-radius: 5px;
            }}
        """)
        self._volume.valueChanged.connect(self._on_volume_changed)
        controls.addWidget(self._volume)

        self._volume_label = QLabel("80%")
        self._volume_label.setFixedWidth(54)
        self._volume_label.setFont(Hud.micro_font(6, 1.1))
        self._volume_label.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        controls.addWidget(self._volume_label)

        controls.addStretch()

        full = QPushButton("⛶  PLEIN ÉCRAN")
        full.setCursor(Qt.CursorShape.PointingHandCursor)
        full.setFont(QFont("Inter", 9, QFont.Weight.Bold))
        full.setStyleSheet(f"""
            QPushButton {{
                color: {C.PRI};
                background: rgba(0, 212, 255, 0.08);
                border: 1px solid {C.BORDER_B};
                border-radius: 4px;
                padding: 6px 14px;
            }}
            QPushButton:hover {{
                color: {C.WHITE};
                background: rgba(0, 212, 255, 0.25);
                border-color: {C.PRI};
            }}
        """)
        full.clicked.connect(lambda: self.control("fullscreen"))
        controls.addWidget(full)

        deck_layout.addLayout(controls)
        self._player_layout.addWidget(self._deck_frame)
        outer.addWidget(self._player_panel, stretch=1)
        self._player_panel.hide()

        self._fx = QTimer(self)
        self._fx.setInterval(45)
        self._fx.timeout.connect(self._tick)
        self._progress_timer = QTimer(self)
        self._progress_timer.setInterval(700)
        self._progress_timer.timeout.connect(self._poll_progress)
        self._duration_seconds = 0.0
        self.hide()
    def status_text(self) -> str:
        if not self.isVisible() or self._mode != "player" or self._index < 0:
            return "Aucune vidéo n'est en lecture dans le lecteur intégré."
        title = self._videos[self._index].get("title") or "Vidéo YouTube"
        return f"Le lecteur intégré affiche « {title} »."

    def selection_is_local(self, index: int) -> bool:
        return 0 <= index < len(self._videos) and self._local_path(self._videos[index]) is not None

    def _stop_web(self) -> None:
        if self._web is not None:
            self._web.setHtml("<html><body style='background:#000'></body></html>")

    def _stop_local(self) -> None:
        if self._local_player is not None:
            self._local_player.stop()
        if self._video_widget is not None:
            self._video_widget.clear_frame()
            self._video_widget.hide()

    def stop_and_show_results(self) -> None:
        """Arrête le flux mais garde la sélection disponible dans la div."""
        self._stop_web()
        self._stop_local()
        self._progress_timer.stop()
        self._current_source = ""
        self._duration_seconds = 0.0
        self._elapsed.setText("00:00")
        self._progress.setValue(0)
        if not self._videos:
            self.close_video()
            return
        self._rebuild_video_cards()
        self._title.setText(f"◈  VIDÉOS — {(self._query or 'SÉLECTION').upper()}"[:78])
        self._badge.setText(f"LECTURE ARRÊTÉE // {len(self._videos):02d} VIDÉOS DISPONIBLES")
        self._mode = "results"
        self._player_panel.hide()
        self._results.show()
        self.show()
        self.raise_()
        self._fx.start()

    def close_video(self) -> None:
        if not self.isVisible():
            return
        self._stop_web()
        self._stop_local()
        self._fx.stop()
        self._progress_timer.stop()
        self.hide()
        self.closed.emit()

    def dismiss_now(self) -> None:
        self.close_video()

    def _toggle_mute(self) -> None:
        if self._volume.value() > 0:
            self._last_volume = self._volume.value()
            self._volume.setValue(0)
            self._btn_mute.setText("🔇")
            self._volume_label.setText("MUTÉ (0%)")
            self.control("mute")
        else:
            val = self._last_volume if self._last_volume > 0 else 80
            self._volume.setValue(val)
            self._btn_mute.setText("🔊")
            self._volume_label.setText(f"{val}%")
            self.control("volume", val)

    def _on_volume_changed(self, val: int) -> None:
        if val == 0:
            self._btn_mute.setText("🔇")
            self._volume_label.setText("MUTÉ (0%)")
        else:
            self._btn_mute.setText("🔊")
            self._volume_label.setText(f"{val}%")
            self._last_volume = val
        self.control("volume", val)

    def _tick(self) -> None:
        self._scan = (self._scan + 1.25) % Hud.SCAN_PERIOD
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(3, 3, self.width() - 6, self.height() - 6)
        Hud.chassis(painter, rect, accent=qcol(C.PRI), scan=self._scan)
        Hud.tick(painter, rect, 64)
        painter.end()
        super().paintEvent(event)
