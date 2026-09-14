from __future__ import annotations

import threading

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from ui.core.qtflags import QWebEngineView, webengine_enabled
from ui.paths import _read_full_config
from ui.media.camera import _CameraPreview
from ui.media.gallery import ImageGalleryOverlay
from ui.media.map_views import NearbyMapPanel
from ui.media.video_hub import VideoHubOverlay
from ui.orb.companion import CompanionOrb
from ui.orb.glsl_orb import create_hud_orb
from ui.orb.mini_orb import MiniOrbOverlay
from ui.orb.radial_waveform import BiDirectionalAudioBridge, CircularFFTEngine
from ui.panels.rich_card_system import CardManager
from ui.core.background_image import BackgroundImage
from ui.panels.clipboard import ClipboardPanel
from ui.panels.drop import GlobalDropOverlay
from ui.panels.floating_panel import FloatingPanel
from ui.panels.interface_frame import InterfaceFrame
from ui.panels.music_player import MusicPlayerPanel
from ui.panels.speech_overlay import CenterSpeechOverlay
from ui.panels.status_pill import _StatusPill
from ui.panels.thought_overlay import ThoughtOverlay
from ui.styles.theme import C
from ui.window.overlay_layout import HudOverlayLayout


class SceneMixin:
    """Construit la scène : orbe, chrome, surfaces immersives, layout."""

    def _assemble_scene(self, face_path: str, display_name: str) -> QWidget:
        central = QWidget()
        central.setStyleSheet(f"QWidget {{ background: {C.BG}; }}")
        self.setCentralWidget(central)
        layout = HudOverlayLayout(central)

        # Toujours le premier calque : l'image ne modifie ni l'orbe, ni les
        # panneaux, ni les cartes placés au-dessus.
        self._background_image = BackgroundImage(
            _read_full_config().get("background_image", ""), central
        )
        layout.add_fill(self._background_image)

        self.hud = create_hud_orb(face_path, display_name)
        if hasattr(self.hud, "set_background_image_active"):
            self.hud.set_background_image_active(self._background_image.has_image)
        layout.add_fill(self.hud)
        self._volume_sig.connect(self.hud.set_volume)
        self._spectrum_bridge = BiDirectionalAudioBridge(target_sr=44100)
        self._spectrum_fft = CircularFFTEngine(num_bands=64, sample_rate=44100)
        self._spectrum_timer = QTimer(self)
        self._spectrum_timer.timeout.connect(self._update_audio_spectrum)
        # Le spectre ne sert qu'au visuel : 5 Hz suffit largement et garde le
        # thread Qt disponible pour la voix.
        self._spectrum_timer.start(200)

        self._interface_frame = InterfaceFrame()
        layout.add_fill(self._interface_frame)

        self._telemetry_panel = FloatingPanel("Système", closeable=True)
        self._telemetry_panel.add_widget(self._build_left_panel())
        layout.add_role(self._telemetry_panel, "telemetry")

        self._header_panel = FloatingPanel("", closeable=False)
        self._header_panel.add_widget(self._build_header())
        layout.add_role(self._header_panel, "header")

        self._status_pill = _StatusPill()
        layout.add_role(self._status_pill, "status")

        self._cmd_panel = FloatingPanel("", closeable=False)
        self._cmd_panel.add_layout(self._build_input_row())
        layout.add_role(self._cmd_panel, "cmd")

        self._content_panel = self._build_content_panel()
        layout.add_role(self._content_panel, "content")

        self._card_stack = CardManager()
        self._card_scroll = QScrollArea()
        self._card_scroll.setObjectName("NotificationRail")
        self._card_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._card_scroll.setWidgetResizable(True)
        self._card_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._card_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._card_scroll.setStyleSheet("""
            QScrollArea#NotificationRail {
                background: transparent;
                border: none;
            }
            QScrollArea#NotificationRail QScrollBar:vertical,
            QScrollArea#NotificationRail QScrollBar:horizontal {
                width: 0px;
                height: 0px;
                background: transparent;
                border: none;
            }
        """)
        self._card_scroll.viewport().setStyleSheet("background: transparent;")
        self._card_scroll.setWidget(self._card_stack)
        self._card_scroll.setFixedWidth(self._card_stack.width() + 12)
        layout.add_role(self._card_scroll, "cards")

        self._music_player_panel = MusicPlayerPanel()
        self._music_player_panel.hide()
        self._music_status_sig.connect(self._on_music_status)
        layout.add_role(self._music_player_panel, "music")
        self._bind_music_player()

        self._nearby_map_panel = NearbyMapPanel()
        self._nearby_map_panel.hide()
        self._nearby_map_sig.connect(self._on_show_nearby_map)
        layout.add_role(self._nearby_map_panel, "nearby")

        self._cam_cont = self._build_camera_surface()
        self._cam_cont.hide()
        layout.add_fill(self._cam_cont)

        self._map_cont = self._build_map_surface()
        self._map_cont.hide()
        layout.add_fill(self._map_cont)

        # Parent explicite avant toute insertion dans le layout : Qt 6.11 /
        # PyQt 6 sous Python 3.14 peut sinon tomber dans addChildWidget lors
        # d'un changement de parent tardif d'une surface riche en sous-layouts.
        self._image_gallery = ImageGalleryOverlay(central)
        self._image_gallery.hide()
        self._image_gallery.closed.connect(self._sync_fullscreen_orb)
        layout.add_fill(self._image_gallery)

        from ui.media.generated_image_preview import GeneratedImagePreview
        self._generated_image_preview = GeneratedImagePreview(central)
        self._generated_image_preview.hide()
        layout.add_role(self._generated_image_preview, "generated_image")
        from ui.media.generated_image_preview import GeneratedArtifactPreview
        self._generated_artifact_preview = GeneratedArtifactPreview(central)
        self._generated_artifact_preview.hide()
        layout.add_role(self._generated_artifact_preview, "generated_artifact")

        self._video_hub = VideoHubOverlay()
        self._video_hub.hide()
        self._video_hub.selected.connect(self._on_video_selected)
        self._video_hub.closed.connect(self._sync_fullscreen_orb)
        layout.add_fill(self._video_hub)

        self._mini_orb = MiniOrbOverlay(self.hud, central)
        self._companion = CompanionOrb(
            self.hud,
            on_click=self._toggle_mute,
            on_restore=self._restore_from_companion,
        )
        self._volume_sig.connect(self._companion.set_volume)
        self._volume_sig.connect(self._mini_orb.set_volume)
        self._focus_is_ours = True
        self._focus_sig.connect(self._on_focus_changed)
        self._focus_watcher = None
        try:
            from core.hypr_focus import FocusWatcher
            self._focus_watcher = FocusWatcher(
                lambda mine: self._focus_sig.emit(mine),
                ignore_titles=(CompanionOrb.WINDOW_TITLE,),
            )
            if not self._focus_watcher.start():
                self._focus_watcher = None
        except Exception as exc:
            print(f"[Companion] Suivi du focus indisponible : {exc}")
            self._focus_watcher = None

        self._quick_drawer = self._build_quick_drawer()
        layout.add_role(self._quick_drawer, "drawer")
        self._update_autostart_btn(self._check_autostart())
        from memory.config_manager import get_brief_enabled as _gbe
        self._update_brief_btn(_gbe())

        self._cam_preview = _CameraPreview()
        layout.add_role(self._cam_preview, "cam_preview")
        self._clipboard_panel = ClipboardPanel()
        self._clipboard_panel.action_requested.connect(self._on_clipboard_action)
        QApplication.clipboard().dataChanged.connect(self._on_clipboard_changed)
        layout.add_role(self._clipboard_panel, "clipboard")

        self._speech_overlay = CenterSpeechOverlay()
        self._speech_overlay.set_assistant_name(self._assistant_name)
        self._speech_overlay.set_reposition_callback(self._position_speech_overlay)
        self._speech_overlay.hide()
        layout.add_role(self._speech_overlay, "speech")
        self._speech_buf = ""
        self._speech_buf_speaker: str | None = None

        self._thought_overlay = ThoughtOverlay()
        self._thought_overlay.set_reposition_callback(self._position_thought_overlay)
        self._thought_overlay.hide()
        layout.add_role(self._thought_overlay, "thought")

        self._drop_overlay = GlobalDropOverlay()
        self._drop_overlay.hide()
        layout.add_fill(self._drop_overlay)
        return central

    def set_background_image(self, path: str) -> bool:
        """Change uniquement le calque photo derrière le HUD."""
        layer = getattr(self, "_background_image", None)
        applied = bool(layer is not None and layer.set_image(path))
        if layer is not None:
            # Wayland peut conserver un backing-store opaque d'un sibling après
            # son premier paint. Réaffirmer la pile et invalider les deux
            # calques rend le changement fiable, même après plusieurs images.
            layer.show()
            layer.lower()
        hud = getattr(self, "hud", None)
        if hud is not None and hasattr(hud, "set_background_image_active"):
            hud.set_background_image_active(bool(layer is not None and layer.has_image))
            hud.update()
        if layer is not None:
            layer.update()
        central = self.centralWidget()
        if central is not None:
            central.update()
        return applied

    def _on_audio_pcm(self, pcm: bytes, sample_rate: int, emitted: bool) -> None:
        if emitted:
            self._spectrum_bridge.feed_emitted(pcm, sample_rate)
        else:
            self._spectrum_bridge.feed_captured(pcm, sample_rate)

    def _update_audio_spectrum(self) -> None:
        setter = getattr(self.hud, "set_audio_bands", None)
        if not callable(setter) or not self.isVisible():
            return
        samples, _source = self._spectrum_bridge.get_samples_for_analysis(
            self._spectrum_fft.fft_size
        )
        bands, _peaks, _vocal_peak, _intensity = self._spectrum_fft.process(samples)
        setter(bands.reshape(8, 8).max(axis=1).tolist())

    def _bind_music_player(self) -> None:
        try:
            from core.player_ipc import get_player
            self._player = get_player(
                callback=lambda status: self._music_status_sig.emit(status)
            )
            self._music_player_panel.btn_play.clicked.connect(self._player.toggle_pause)
            self._music_player_panel.btn_prev.clicked.connect(self._player.prev)
            self._music_player_panel.btn_next.clicked.connect(self._player.next)
            self._music_player_panel.btn_stop.clicked.connect(self._player.stop)
            self._music_player_panel.btn_shuffle.clicked.connect(
                lambda: self._player.set_shuffle(
                    not self._player.get_status().get("shuffle", False)
                )
            )
            self._music_player_panel.seek_requested.connect(
                lambda pct: self._player.seek(
                    pct * (self._player.get_status().get("duration", 0) or 1)
                )
            )
        except Exception:
            self._player = None

    def _build_camera_surface(self) -> QWidget:
        cont = QWidget()
        cont.setStyleSheet("background: #000308;")
        lay = QVBoxLayout(cont)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        hdr = QHBoxLayout()
        hdr.setContentsMargins(8, 5, 8, 5)
        self._cam_title = QLabel("◈  FLUX CAMERA")
        self._cam_title.setFont(QFont("Inter", 8, QFont.Weight.Bold))
        self._cam_title.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        hdr.addWidget(self._cam_title)
        hdr.addStretch()
        close_btn = QPushButton("✕  FERMER")
        close_btn.setFont(QFont("Inter", 8, QFont.Weight.Bold))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                color: {C.TEXT_DIM}; background: transparent;
                border: none; padding: 2px 6px;
            }}
            QPushButton:hover {{ color: {C.PRI}; }}
        """)
        close_btn.clicked.connect(self._close_camera_view)
        hdr.addWidget(close_btn)
        lay.addLayout(hdr)
        self._cam_live_lbl = QLabel()
        self._cam_live_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cam_live_lbl.setStyleSheet("background: transparent;")
        self._cam_live_lbl.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        lay.addWidget(self._cam_live_lbl, stretch=1)
        return cont

    def _build_map_surface(self) -> QWidget:
        cont = QWidget()
        cont.setStyleSheet("background: #000308;")
        lay = QVBoxLayout(cont)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        hdr = QHBoxLayout()
        hdr.setContentsMargins(8, 5, 8, 5)
        self._map_title = QLabel("◈  CARTE")
        self._map_title.setFont(QFont("Inter", 8, QFont.Weight.Bold))
        self._map_title.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        hdr.addWidget(self._map_title)
        hdr.addStretch()
        # Bascule manuelle : le globe (aperçu) et la carte de rues (Leaflet)
        # montrent tous deux la position — l'un ou l'autre, au choix, sans
        # perdre l'endroit affiché.
        self._map_toggle_btn = QPushButton("🌐  GLOBE")
        self._map_toggle_btn.setFont(QFont("Inter", 8, QFont.Weight.Bold))
        self._map_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._map_toggle_btn.setStyleSheet(f"""
            QPushButton {{
                color: {C.TEXT_DIM}; background: transparent;
                border: none; padding: 2px 6px;
            }}
            QPushButton:hover {{ color: {C.PRI}; }}
        """)
        self._map_toggle_btn.clicked.connect(self._toggle_map_view)
        hdr.addWidget(self._map_toggle_btn)
        close_btn = QPushButton("✕  FERMER")
        close_btn.setFont(QFont("Inter", 8, QFont.Weight.Bold))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                color: {C.TEXT_DIM}; background: transparent;
                border: none; padding: 2px 6px;
            }}
            QPushButton:hover {{ color: {C.PRI}; }}
        """)
        close_btn.clicked.connect(self.close_map)
        hdr.addWidget(close_btn)
        lay.addLayout(hdr)
        if webengine_enabled() and QWebEngineView is not None:
            self._map_view = QWebEngineView()
            lay.addWidget(self._map_view, stretch=1)
            # Un guidage démarré juste après un show_map (le cas courant :
            # navigate affiche la destination puis lance le guidage) injecte
            # son JS sur une page encore en train de charger — ANO_START_
            # NAVIGATION n'existe pas encore, l'appel ne fait alors rien,
            # silencieusement. _on_start_navigation attend ce signal avant
            # d'agir si un chargement est en cours.
            self._map_view.loadFinished.connect(self._on_map_load_finished)
        else:
            self._map_view = None
            fallback = QLabel(
                "La carte s'ouvre dans votre navigateur.\n\n"
                "Pour l'intégrer à ANO-GPT sous Arch/EndeavourOS :\n"
                "sudo pacman -S python-pyqt6-webengine"
            )
            fallback.setAlignment(Qt.AlignmentFlag.AlignCenter)
            fallback.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            lay.addWidget(fallback, stretch=1)
        # Suivi du rendu actuellement chargé : « leaflet » (carte de rues,
        # défaut) ou « globe » (aperçu). Bascule manuelle via le bouton
        # d'en-tête (_toggle_map_view) ou automatique vers Leaflet au
        # démarrage d'un guidage. Voir media_host._render_map.
        self._map_mode = "leaflet"
        self._map_last_args = None
        self._map_loading = False
        return cont

    def _connect_window_signals(self) -> None:
        self._log_sig.connect(self._handle_log)
        self._state_sig.connect(self._apply_state)
        self._content_sig.connect(self._show_content)
        self._reconfig_sig.connect(self._show_setup)
        self._camera_sig.connect(self._show_camera_frame)
        self._cam_stream_sig.connect(self._on_cam_stream)
        self._cam_frame_sig.connect(self._on_cam_frame)
        self._clipboard_sig.connect(self._show_clipboard_panel)
        self._setmute_sig.connect(self._set_muted)
        self._map_sig.connect(self._on_show_map)
        self._map_close_sig.connect(self._on_close_map)
        self._live_pos_sig.connect(self._on_update_live_position)
        self._nav_sig.connect(self._on_start_navigation)
        self._image_gallery_sig.connect(self._on_show_image_gallery)
        self._image_gallery_close_sig.connect(self._image_gallery.close_gallery)
        self._generated_image_preview_sig.connect(self._on_show_generated_image_preview)
        self._generated_artifact_preview_sig.connect(self._on_show_generated_artifact_preview)
        self._video_results_sig.connect(self._on_show_video_results)
        self._video_play_sig.connect(self._on_play_video)
        self._video_control_sig.connect(self._video_hub.control)
        self._video_close_sig.connect(self._video_hub.close_video)
        self._weather_sig.connect(self._weather_lbl.setText)
        self._card_sig.connect(self._on_show_card)
        self._update_card_sig.connect(self._on_update_card)
        self._dismiss_cards_sig.connect(self._on_dismiss_cards)
        self._download_card_sig.connect(self._on_music_download)
        self._task_card_sig.connect(self._on_task_card)
        self._transcript_sig.connect(self._on_user_transcript)
        self._audio_pcm_sig.connect(self._on_audio_pcm)
        self._gesture_sig.connect(self._on_gesture_sig)
        self._continuous_vision_sig.connect(self._on_continuous_vision_sig)
        self._thought_sig.connect(self._on_thought_sig)
        self._accent_sig.connect(self._on_accent_changed)
        self._clock_particles_sig.connect(self._on_clock_particles)
        self._show_sig.connect(self._show_existing_window)
        self._cam_stop = threading.Event()

    def _on_user_transcript(self, text: str, final: bool = False, turn_id: str = "") -> None:
        """Affiche le brouillon STT au même endroit que les paroles d'ANO.

        Le widget télémétrie garde son rôle de trace compacte ; la carte au bas
        de l'écran devient la lecture immédiate et lisible de la phrase en
        cours. Les mises à jour sont des révisions de phrase, pas des logs.
        """
        previous = getattr(self, "_active_transcript_turn_id", "")
        if turn_id and previous and turn_id != previous:
            # Les ids sont créés aléatoirement : une mise à jour d'un tour plus
            # ancien ne peut pas remplacer le texte live déjà visible.
            if turn_id in getattr(self, "_closed_transcript_turn_ids", set()):
                return
            self._closed_transcript_turn_ids = set(list(
                getattr(self, "_closed_transcript_turn_ids", set())
            )[-15:] + [previous])
        if turn_id:
            self._active_transcript_turn_id = turn_id
        self._live_transcript.set_transcript(text, final)
        # La grande carte doit suivre exactement le même texte que le moniteur
        # « Votre voix ». Attendre Azure/Gemini final faisait apparaître la
        # transcription *après* la réponse d'ANO-GPT, alors que l'hypothèse
        # affichée en direct était déjà compréhensible. La même carte évolue
        # donc de « écoute en direct » à « transcription confirmée » sans
        # changer de phrase ni casser le rythme visuel.
        if text and hasattr(self, "_speech_overlay"):
            self._speech_overlay.show_speech(text, speaker="user", final=final)

    def _show_existing_window(self) -> None:
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()

    def _start_scene_timers(self) -> None:
        self._clock_tmr = QTimer(self)
        self._clock_tmr.timeout.connect(self._tick_clock)
        self._clock_tmr.start(1000)
        self._tick_clock()
        self._metric_tmr = QTimer(self)
        self._metric_tmr.timeout.connect(self._update_metrics)
        self._metric_tmr.start(2000)
        self._update_metrics()
        self._weather_tmr = QTimer(self)
        self._weather_tmr.timeout.connect(self._refresh_weather)
        self._weather_tmr.start(20 * 60 * 1000)
        self._refresh_weather()

    def _on_gesture_sig(self, icon: str, label: str, value: float) -> None:
        if hasattr(self, "hud") and self.hud:
            self.hud.show_gesture_feedback(icon, label, value)

    def _on_continuous_vision_sig(self, active: bool) -> None:
        if hasattr(self, "hud") and self.hud:
            self.hud.set_continuous_vision(active)

    def _on_accent_changed(self, hex_color: str, custom_palette: dict = None) -> None:
        from ui.styles.theme import apply_ui_accent, current_palette, retheme_all_widgets
        old = current_palette()
        if hex_color and apply_ui_accent(hex_color):
            retheme_all_widgets(old, current_palette())
        if hasattr(self, "hud") and self.hud and hasattr(self.hud, "set_accent_color"):
            self.hud.set_accent_color(hex_color, custom_palette)

    def _on_clock_particles(self, duration: float) -> None:
        if hasattr(self, "hud") and self.hud and hasattr(self.hud, "show_clock_particles"):
            self.hud.show_clock_particles(duration)

    def _on_thought_sig(self, text: str, is_active: bool) -> None:
        if hasattr(self, "_thought_overlay") and self._thought_overlay:
            if is_active and text:
                self._thought_overlay.show_thought(text)
                self._position_thought_overlay()
            else:
                self._thought_overlay.fade_out()

    def _install_shortcuts(self) -> None:
        QShortcut(QKeySequence("F1"), self).activated.connect(self._show_welcome_hud)
        QShortcut(QKeySequence("F4"), self).activated.connect(self._mute_from_shortcut)
        QShortcut(QKeySequence("F8"), self).activated.connect(self._toggle_gesture_control)
        QShortcut(QKeySequence("F11"), self).activated.connect(self._toggle_fullscreen)
        QShortcut(QKeySequence("Escape"), self).activated.connect(self._do_interrupt)

    def _toggle_gesture_control(self) -> None:
        try:
            from core.gesture_control import get_gesture_controller
            ctl = get_gesture_controller()
            ctl.start()
            msg = ctl.toggle(force_camera=True)
            if hasattr(self, "_log") and self._log:
                self._log.append_log(f"SYS : {msg}")
        except Exception:
            pass
