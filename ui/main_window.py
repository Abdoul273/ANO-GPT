from __future__ import annotations

import os

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QApplication, QMainWindow

from ui.core.speech_text import _advance_current_sentence
from ui.dialogs.setup import SetupOverlay
from ui.paths import (
    _DEFAULT_H, _DEFAULT_W, _MIN_H, _MIN_W, _read_full_config,
)
from ui.styles.qss import get_global_style
from ui.styles.theme import DEFAULT_UI_COLOR, apply_ui_accent
from ui.window.chrome import ChromeMixin
from ui.window.dialogs_host import DialogsHostMixin
from ui.window.drawer import DrawerMixin
from ui.window.media_host import MediaHostMixin
from ui.window.positions import PositionsMixin
from ui.window.scene import SceneMixin
from ui.window.system_ops import SystemOpsMixin


class MainWindow(
    MediaHostMixin,
    ChromeMixin,
    DrawerMixin,
    SystemOpsMixin,
    DialogsHostMixin,
    PositionsMixin,
    SceneMixin,
    QMainWindow,
):
    _log_sig        = pyqtSignal(str)
    _state_sig      = pyqtSignal(str)
    _content_sig    = pyqtSignal(str, str)
    _reconfig_sig   = pyqtSignal()
    _camera_sig     = pyqtSignal(bytes)
    _cam_stream_sig = pyqtSignal(bool)
    _cam_frame_sig  = pyqtSignal(bytes)
    _clipboard_sig  = pyqtSignal(str)
    _setmute_sig    = pyqtSignal(bool)
    _map_sig        = pyqtSignal(str, float, float, float)
    _map_close_sig  = pyqtSignal()
    _image_gallery_sig = pyqtSignal(str, list)
    _image_gallery_close_sig = pyqtSignal()
    _generated_image_preview_sig = pyqtSignal(str, bytes, str)
    _generated_artifact_preview_sig = pyqtSignal(str, str, str)
    _video_results_sig = pyqtSignal(str, list)
    _video_play_sig = pyqtSignal(dict, list)
    _video_control_sig = pyqtSignal(str, object)
    _video_close_sig = pyqtSignal()
    _weather_sig    = pyqtSignal(str)
    _card_sig       = pyqtSignal(str, str, str, list)
    _update_card_sig = pyqtSignal(str, str, str)
    _dismiss_cards_sig = pyqtSignal(str, str)
    _download_card_sig = pyqtSignal(dict)
    _music_status_sig = pyqtSignal(dict)
    _nearby_map_sig = pyqtSignal(str, float, float, list)
    _live_pos_sig   = pyqtSignal(float, float, object, object, object)
    _nav_sig        = pyqtSignal(float, float, str)
    _transcript_sig = pyqtSignal(str, bool, str)
    _volume_sig     = pyqtSignal(float)
    _audio_pcm_sig  = pyqtSignal(bytes, int, bool)
    _focus_sig      = pyqtSignal(bool)
    _gesture_sig    = pyqtSignal(str, str, float)
    _continuous_vision_sig = pyqtSignal(bool)
    _thought_sig    = pyqtSignal(str, bool)
    _accent_sig     = pyqtSignal(str, object)
    _clock_particles_sig = pyqtSignal(float)
    _show_sig       = pyqtSignal()

    def __init__(self, face_path: str):
        super().__init__()
        self._face_path = face_path

        _cfg = _read_full_config()
        self._assistant_name: str = (_cfg.get("assistant_name") or "ANO-GPT").strip()
        _display = self._assistant_name.upper()

        _ui_color = (_cfg.get("ui_color") or "").strip()
        if _ui_color and _ui_color.lower() != DEFAULT_UI_COLOR:
            apply_ui_accent(_ui_color)

        self.setWindowTitle(f"{_display} — NEURAL INTERFACE")
        self.setMinimumSize(_MIN_W, _MIN_H)
        self.resize(_DEFAULT_W, _DEFAULT_H)
        if not os.environ.get("WAYLAND_DISPLAY"):
            screen = QApplication.primaryScreen().availableGeometry()
            self.move(
                (screen.width()  - _DEFAULT_W) // 2,
                (screen.height() - _DEFAULT_H) // 2,
            )

        self.on_text_command   = None
        self.on_remote_clicked = None
        self.on_interrupt      = None
        self.on_camera_action  = None
        self.on_camera_open    = None
        self.on_camera_close   = None
        self.on_mic_device_change      = None
        self.on_output_device_change   = None
        self.on_plugins_list           = None
        self.on_plugin_toggle          = None
        self.on_mic_sensitivity_change = None
        self.on_voice_change           = None
        # Changement de cerveau : la session Live doit repartir pour que le
        # nouveau fournisseur prenne effet sans redémarrer l'application.
        self.on_brain_change           = None
        self.on_voice_provider_change  = None
        self.on_stt_provider_change     = None
        self.on_elevenlabs_voice_change = None
        self._muted            = False
        # Le clic explicite sur le micro crée un verrou matériel logique : ni
        # mot d'activation, ni outil, ni message distant ne peut le rouvrir.
        self._manual_mic_lock  = False
        self._current_file: str | None = None
        self._remote_overlay = None
        self._audio_settings_overlay = None
        self._memory_overlay = None
        self._plugin_overlay = None
        self._customize_overlay = None
        self._ai_config_overlay = None

        self.setStyleSheet(get_global_style())
        self._assemble_scene(face_path, _display)
        self._connect_window_signals()
        self._start_scene_timers()

        self._overlay: SetupOverlay | None = None
        self._ready = self._check_config()
        if not self._ready:
            self._show_setup()

        self.setAcceptDrops(True)
        self._install_shortcuts()

    def request_show(self) -> None:
        """Thread-safe : remet la fenêtre existante au premier plan."""
        self._show_sig.emit()

    def _handle_log(self, text: str):
        if hasattr(self, "_log") and self._log:
            self._log.append_log(text)
        if not hasattr(self, "_speech_overlay"):
            return

        t_clean = text.strip()
        if t_clean.startswith("[INLINE_START]"):
            content = t_clean[14:].strip()
            if content.lower().startswith(("vous:", "you:")):
                speaker = "user"
            else:
                speaker = "ai"
            parts = content.split(":", 1)
            val = parts[1].strip() if len(parts) > 1 else content
            self._speech_buf = _advance_current_sentence("", val)
            self._speech_buf_speaker = speaker
            if self._speech_buf:
                self._show_speech(self._speech_buf, speaker)
        elif t_clean == "[INLINE_END]":
            self._speech_buf = ""
            self._speech_buf_speaker = None
        elif t_clean.startswith("[INLINE]"):
            frag = t_clean[8:].strip()
            if frag:
                speaker = self._speech_buf_speaker or "ai"
                self._speech_buf = _advance_current_sentence(self._speech_buf, frag)
                self._show_speech(self._speech_buf, speaker)
        elif t_clean.startswith("Vous : "):
            val = t_clean[7:].strip()
            if val:
                self._speech_buf = ""
                self._speech_buf_speaker = None
                self._show_speech(val, "user")

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if hasattr(self, "hud"):
            self._sync_fullscreen_orb()
