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
    QEasingCurve, QEvent, QLineF, QPointF, QRect, QRectF, QSize, Qt,
    QTimer, QThread, pyqtSignal, QPropertyAnimation, QUrl,
)
from PyQt6.QtGui import (
    QBrush, QColor, QConicalGradient, QDragEnterEvent, QDropEvent, QFont, QImage,
    QDesktopServices, QFontDatabase, QFontMetrics, QFontMetricsF, QIcon, QKeySequence,
    QLinearGradient, QPainter,
    QPainterPath, QPen, QPixmap, QPolygonF, QRadialGradient, QRegion, QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QFrame, QGraphicsOpacityEffect, QGridLayout,
    QHBoxLayout, QLabel, QLayout, QLineEdit, QProgressBar,
    QMainWindow, QPushButton, QScrollArea, QSizePolicy, QSlider,
    QTextBrowser, QTextEdit, QVBoxLayout, QWidget, QSplashScreen,
)

from ui.core.qtflags import (
    QAudioOutput, QMediaPlayer, QWebEngineSettings, QWebEngineView,
    qtmultimedia_enabled, webengine_enabled,
)
from ui.media.video_widgets import VideoResultCard, _VideoFrameCanvas
from ui.styles.theme import C

class _VideoPlaybackMixin:
    _LOCAL_VIDEO_EXT = {
        ".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv",
        ".m4v", ".ts", ".mpg", ".mpeg",
    }

    @staticmethod
    def _video_id(video: dict) -> str:
        direct = str(video.get("id") or "")
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", direct):
            return direct
        match = re.search(r"(?:v=|youtu\.be/|/embed/)([A-Za-z0-9_-]{11})", str(video.get("url") or ""))
        return match.group(1) if match else ""

    @classmethod
    def _local_path(cls, video: dict) -> Path | None:
        raw = str(video.get("path") or "").strip()
        if not raw:
            return None
        try:
            path = Path(raw).expanduser().resolve()
            return path if path.is_file() and path.suffix.lower() in cls._LOCAL_VIDEO_EXT else None
        except OSError:
            return None

    @classmethod
    def _is_playable(cls, video: dict) -> bool:
        return cls._local_path(video) is not None or bool(cls._video_id(video))

    def show_results(self, query: str, videos: list[dict]) -> bool:
        self._stop_web()
        self._stop_local()
        self._progress_timer.stop()
        self._videos = [dict(video) for video in videos[:12] if self._is_playable(video)]
        self._index = -1
        if not self._videos:
            return False
        self._query = str(query or "sélection vidéo")
        self._rebuild_video_cards()
        self._title.setText(f"◈  VIDÉOS — {self._query.upper()}"[:78])
        self._badge.setText(f"{len(self._videos):02d} CIBLES // CHOISISSEZ UNE VIDÉO")
        self._mode = "results"
        self._player_panel.hide()
        self._results.show()
        self.show()
        self.raise_()
        self._fx.start()
        return True

    def _rebuild_video_cards(self) -> None:
        while self._grid.count():
            item = self._grid.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        columns = 3 if len(self._videos) >= 3 else max(1, len(self._videos))
        for index, video in enumerate(self._videos):
            card = VideoResultCard(index, video)
            card.selected.connect(self.selected)
            self._grid.addWidget(card, index // columns, index % columns)

    def play_video(self, video: dict, playlist: list[dict] | None = None) -> bool:
        video_id = self._video_id(video)
        local_path = self._local_path(video)
        if not video_id and local_path is None:
            return False
        incoming = [dict(item) for item in (playlist or []) if self._is_playable(item)]
        if incoming:
            self._videos = incoming
        source_key = str(local_path) if local_path is not None else video_id
        self._index = next(
            (i for i, item in enumerate(self._videos)
             if (str(self._local_path(item)) if self._local_path(item) is not None
                 else self._video_id(item)) == source_key), -1
        )
        if self._index < 0:
            self._videos.append(dict(video))
            self._index = len(self._videos) - 1
        title = str(video.get("title") or "Vidéo YouTube")
        channel = str(video.get("channel") or video.get("folder") or
                      ("FICHIER LOCAL" if local_path is not None else ""))
        self._title.setText("◈  LECTEUR VIDÉO INTÉGRÉ")
        self._badge.setText(f"LECTURE {self._index + 1:02d}/{len(self._videos):02d}")
        self._now_title.setText(f"▶  {title}" + (f"  //  {channel}" if channel else ""))
        self._elapsed.setText("00:00")
        self._duration_label.setText(str(video.get("duration") or "00:00"))
        self._progress.setValue(0)
        self._duration_seconds = 0.0
        self._mode = "player"
        self._results.hide()
        self._player_panel.show()
        if local_path is not None:
            self._stop_web()
            self._current_source = "local"
            if self._ensure_local_player():
                self._web_placeholder.hide()
                if self._web is not None:
                    self._web.hide()
                self._video_widget.show()
                self._local_player.setSource(QUrl.fromLocalFile(str(local_path)))
                self._local_player.play()
                self._progress_timer.start()
        else:
            self._stop_local()
            self._current_source = "youtube"
            self._ensure_web()
            if self._web is not None:
                self._web.show()
                self._web.setHtml(self._player_html(video_id), QUrl("http://localhost/"))
                self._progress_timer.start()
        self.show()
        self.raise_()
        self._fx.start()
        return self._local_player is not None if local_path is not None else self._web is not None

    def _ensure_local_player(self) -> bool:
        if self._local_player is not None:
            return True
        if not qtmultimedia_enabled():
            self._show_player_error(
                "LECTEUR LOCAL INDISPONIBLE\nPyQt6-Multimedia est requis."
            )
            return False
        try:
            self._video_widget = _VideoFrameCanvas()
            self._audio_output = QAudioOutput(self)
            self._audio_output.setVolume(.8)
            self._local_player = QMediaPlayer(self)
            self._local_player.setAudioOutput(self._audio_output)
            self._local_player.setVideoSink(self._video_widget.sink)
            self._local_player.errorOccurred.connect(self._on_local_error)
            self._local_player.mediaStatusChanged.connect(self._on_local_media_status)
            self._local_player.durationChanged.connect(self._on_local_duration)
            self._local_player.positionChanged.connect(self._on_local_position)
            if self._web is not None:
                self._player_layout.insertWidget(0, self._video_widget, stretch=1)
                self._web.hide()
            else:
                self._player_layout.replaceWidget(self._web_placeholder, self._video_widget)
            self._web_placeholder.hide()
            return True
        except Exception as exc:
            self._local_player = None
            self._show_player_error(f"LECTEUR LOCAL INDISPONIBLE\n{exc}")
            return False

    def _show_player_error(self, message: str) -> None:
        if self._player_layout.indexOf(self._web_placeholder) < 0:
            self._player_layout.insertWidget(0, self._web_placeholder, stretch=1)
        if self._web is not None:
            self._web.hide()
        if self._video_widget is not None:
            self._video_widget.hide()
        self._web_placeholder.setText(message)
        self._web_placeholder.setStyleSheet(f"color: {C.RED}; background: #000308;")
        self._web_placeholder.show()

    def _on_local_error(self, _error, message: str = "") -> None:
        if message:
            self._badge.setText("ERREUR DE DÉCODAGE // " + message[:48].upper())

    def _on_local_media_status(self, status) -> None:
        if (qtmultimedia_enabled() and
                status == QMediaPlayer.MediaStatus.EndOfMedia and
                len(self._videos) > 1):
            self.control("next")

    def _on_local_duration(self, milliseconds: int) -> None:
        self._duration_seconds = max(0.0, milliseconds / 1000.0)
        self._duration_label.setText(self._clock(self._duration_seconds))

    def _on_local_position(self, milliseconds: int) -> None:
        seconds = max(0.0, milliseconds / 1000.0)
        self._elapsed.setText(self._clock(seconds))
        if self._duration_seconds > 0 and not self._progress.isSliderDown():
            self._progress.setValue(int(1000 * seconds / self._duration_seconds))

    def _ensure_web(self) -> bool:
        if self._web is not None:
            return True
        if not webengine_enabled() or os.environ.get("QT_QPA_PLATFORM", "").lower() == "offscreen":
            self._show_player_error(
                "LECTEUR VIDÉO INDISPONIBLE\nPyQt6-WebEngine ou un affichage graphique est requis."
            )
            return False
        try:
            # Lève l'interdiction d'autoplay Chromium pour démarrer le flux immédiatement
            flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
            if "--autoplay-policy=no-user-gesture-required" not in flags:
                os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (
                    f"{flags} --autoplay-policy=no-user-gesture-required"
                ).strip()

            self._web = QWebEngineView()
            settings = self._web.settings()
            settings.setAttribute(
                QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, False
            )
            settings.setAttribute(
                QWebEngineSettings.WebAttribute.JavascriptEnabled, True
            )
            settings.setAttribute(
                QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True
            )
            settings.setAttribute(
                QWebEngineSettings.WebAttribute.FullScreenSupportEnabled, True
            )
            self._web.setStyleSheet("background: #000;")
            if self._video_widget is not None:
                self._player_layout.insertWidget(0, self._web, stretch=1)
                self._video_widget.hide()
            else:
                self._player_layout.replaceWidget(self._web_placeholder, self._web)
                self._web_placeholder.hide()
            return True
        except Exception as exc:
            self._web = None
            self._show_player_error(f"LECTEUR VIDÉO INDISPONIBLE\n{exc}")
            return False

    @staticmethod
    def _player_html(video_id: str) -> str:
        safe_id = json.dumps(video_id)
        return f"""<!doctype html><html><head><meta charset='utf-8'>
<meta name='referrer' content='strict-origin-when-cross-origin'>
<style>
html,body,#player{{width:100%;height:100%;margin:0;padding:0;background:#000;overflow:hidden;border:none}}
#fallback{{display:none;width:100%;height:100%;margin:0;padding:0;border:none;background:#000}}
</style>
</head><body><div id='player'></div>
<iframe id='fallback' allow='autoplay; encrypted-media; picture-in-picture' allowfullscreen></iframe>
<script src='https://www.youtube.com/iframe_api'></script>
<script>
let player = null, started = false;
const videoId = {safe_id};
const embed = 'https://www.youtube-nocookie.com/embed/' + videoId + '?autoplay=1&playsinline=1&rel=0&modestbranding=1&enablejsapi=1&origin=http://localhost';

function fallback() {{
    if (started) return;
    started = true;
    const p = document.getElementById('player');
    if (p) p.style.display = 'none';
    const f = document.getElementById('fallback');
    if (f) {{
        f.src = embed;
        f.style.display = 'block';
    }}
}}

function onYouTubeIframeAPIReady() {{
    try {{
        player = new YT.Player('player', {{
            videoId:{safe_id},
            playerVars:{{autoplay:1,controls:1,rel:0,modestbranding:1,playsinline:1,enablejsapi:1,origin:'http://localhost'}},
            events: {{
                onReady: (e) => {{
                    started = true;
                    try {{ e.target.setVolume(80); }} catch(err) {{}}
                    try {{ e.target.playVideo(); }} catch(err) {{}}
                }},
                onError: (e) => {{
                    fallback();
                }},
                onAutoplayBlocked: () => {{
                    if (player && player.playVideo) player.playVideo();
                }}
            }}
        }});
    }} catch(e) {{
        fallback();
    }}
}}

setTimeout(fallback, 7000);

function sendIframeMsg(func, args) {{
    const f = document.getElementById('fallback');
    if (f && f.contentWindow) {{
        f.contentWindow.postMessage(JSON.stringify({{
            event: 'command',
            func: func,
            args: args || []
        }}), '*');
    }}
}}

function ctl(action,value) {{
    if (player && typeof player.getPlayerState === 'function') {{
        try {{
            if (action === 'pause') player.pauseVideo();
            else if (action === 'resume' || action === 'playback') player.playVideo();
            else if (action === 'toggle') {{
                player.getPlayerState() === 1 ? player.pauseVideo() : player.playVideo();
            }}
            else if (action === 'mute') {{
                player.isMuted() ? player.unMute() : player.mute();
            }}
            else if (action === 'volume') player.setVolume(Math.max(0, Math.min(100, Number(value))));
            else if (action === 'seek') player.seekTo(Number(value), true);
            else if (action === 'forward') {{
                const t = player.getCurrentTime ? player.getCurrentTime() : 0;
                player.seekTo(t + Math.abs(Number(value || 10)), true);
            }}
            else if (action === 'back') {{
                const t = player.getCurrentTime ? player.getCurrentTime() : 0;
                player.seekTo(Math.max(0, t - Math.abs(Number(value || 10))), true);
            }}
            else if (action === 'restart') player.seekTo(0, true);
            else if (action === 'speed') player.setPlaybackRate(Number(value));
            return;
        }} catch(e) {{}}
    }}
    if (action === 'pause') sendIframeMsg('pauseVideo');
    else if (action === 'resume' || action === 'playback') sendIframeMsg('playVideo');
    else if (action === 'mute') sendIframeMsg('mute');
    else if (action === 'volume') sendIframeMsg('setVolume', [Number(value)]);
    else if (action === 'seek') sendIframeMsg('seekTo', [Number(value), true]);
}}
</script></body></html>"""

    def _js(self, action: str, value=None) -> None:
        if self._web is None:
            return
        self._web.page().runJavaScript(f"ctl({json.dumps(action)},{json.dumps(value)})")

    @staticmethod
    def _clock(seconds) -> str:
        try:
            total = max(0, int(float(seconds)))
        except (TypeError, ValueError):
            total = 0
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"

    def _poll_progress(self) -> None:
        if not self.isVisible() or self._mode != "player":
            self._progress_timer.stop()
            return
        if self._current_source == "local" and self._local_player is not None:
            current = self._local_player.position() / 1000.0
            self._duration_seconds = self._local_player.duration() / 1000.0
            self._elapsed.setText(self._clock(current))
            self._duration_label.setText(self._clock(self._duration_seconds))
            if self._duration_seconds > 0 and not self._progress.isSliderDown():
                self._progress.setValue(int(1000 * current / self._duration_seconds))
            return
        if self._web is None:
            self._progress_timer.stop()
            return
        script = "player&&player.getDuration?{current:player.getCurrentTime(),duration:player.getDuration()}:null"

        def update(value):
            if not isinstance(value, dict):
                return
            current = float(value.get("current") or 0.0)
            self._duration_seconds = float(value.get("duration") or 0.0)
            self._elapsed.setText(self._clock(current))
            self._duration_label.setText(self._clock(self._duration_seconds))
            if self._duration_seconds > 0 and not self._progress.isSliderDown():
                self._progress.setValue(int(1000 * current / self._duration_seconds))

        self._web.page().runJavaScript(script, update)

    def _seek_from_progress(self) -> None:
        self._seek_to_progress_value(self._progress.value())

    def _seek_to_progress_value(self, value: int) -> None:
        if self._duration_seconds > 0:
            target = self._duration_seconds * value / 1000.0
            if self._current_source == "local" and self._local_player is not None:
                self._local_player.setPosition(int(target * 1000))
            else:
                self._js("seek", target)

    def control(self, action: str, value=None) -> None:
        action = str(action or "").lower()
        if action in {"next", "previous"} and self._videos:
            step = 1 if action == "next" else -1
            index = (self._index + step) % len(self._videos)
            self.play_video(self._videos[index], self._videos)
            return
        if action == "stop":
            self.stop_and_show_results()
            return
        if action == "close":
            self.close_video()
            return
        if action == "fullscreen":
            window = self.window()
            window.showNormal() if window.isFullScreen() else window.showFullScreen()
            return
        if self._current_source == "local" and self._local_player is not None:
            if action == "pause":
                self._local_player.pause()
            elif action in {"resume", "playback"}:
                self._local_player.play()
            elif action == "toggle":
                if self._local_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                    self._local_player.pause()
                else:
                    self._local_player.play()
            elif action == "mute" and self._audio_output is not None:
                self._audio_output.setMuted(not self._audio_output.isMuted())
            elif action == "volume" and self._audio_output is not None:
                raw_volume = str(value or "0").strip()
                if raw_volume[:1] in {"+", "-"}:
                    target = self._audio_output.volume() * 100 + float(raw_volume)
                else:
                    target = float(raw_volume)
                self._audio_output.setVolume(max(0.0, min(1.0, target / 100.0)))
            elif action in {"forward", "back"}:
                delta = abs(float(value or 10)) * (1 if action == "forward" else -1)
                self._local_player.setPosition(max(0, self._local_player.position() + int(delta * 1000)))
            elif action == "seek":
                self._local_player.setPosition(max(0, int(float(value or 0) * 1000)))
            elif action == "restart":
                self._local_player.setPosition(0)
            elif action == "speed":
                self._local_player.setPlaybackRate(max(.25, min(3.0, float(value or 1))))
            return
        self._js(action, value)
