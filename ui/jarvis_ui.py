from __future__ import annotations

import math
import os
import sys
import time
import traceback

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QPainter, QPen, QPixmap, QRadialGradient
from PyQt6.QtWidgets import QApplication, QSplashScreen

from ui.main_window import MainWindow
from ui.styles.qss import get_global_style
from ui.styles.theme import load_custom_font


def _startup_splash_pixmap() -> QPixmap:
    """Première image cohérente avec l'accueil, dessinée une seule fois."""
    pixmap = QPixmap(520, 280)
    pixmap.fill(QColor(2, 7, 16))
    p = QPainter(pixmap)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    glow = QRadialGradient(QPointF(260, 113), 120)
    glow.setColorAt(0, QColor(24, 121, 171, 92))
    glow.setColorAt(1, QColor(2, 7, 16, 0))
    p.fillRect(0, 0, 520, 280, glow)
    p.setBrush(Qt.BrushStyle.NoBrush)
    for radius, alpha in ((48, 52), (62, 115), (78, 53)):
        p.setPen(QPen(QColor(111, 220, 247, alpha), 1))
        p.drawEllipse(QPointF(260, 113), radius, radius)
    p.setPen(QPen(QColor(136, 229, 250, 205), 2))
    for start in (22, 112, 202, 292):
        p.drawArc(QRectF(198, 51, 124, 124), start * 16, 53 * 16)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(239, 253, 255))
    p.drawEllipse(QPointF(260, 113), 4, 4)
    p.setFont(QFont("Inter", 19, QFont.Weight.Black))
    p.setPen(QColor(239, 250, 255))
    p.drawText(QRectF(30, 194, 460, 34), Qt.AlignmentFlag.AlignCenter, "ANO-GPT")
    p.setFont(QFont("JetBrains Mono", 8, QFont.Weight.DemiBold))
    p.setPen(QColor(121, 207, 232))
    p.drawText(QRectF(30, 233, 460, 20), Qt.AlignmentFlag.AlignCenter,
               "INITIALISATION DE L'INTERFACE")
    p.setPen(QPen(QColor(70, 165, 197, 90), 1))
    p.drawLine(28, 266, 492, 266)
    p.end()
    return pixmap


class _RootShim:
    def __init__(self, app: QApplication):
        self._app = app
    def mainloop(self):
        self._app.exec()
    def protocol(self, *_):
        pass


class JarvisUI:
    def __init__(self, face_path: str, size=None):
        def _qt_exception_hook(exc_type, exc_value, exc_tb):
            if issubclass(exc_type, KeyboardInterrupt):
                app = QApplication.instance()
                if app is not None:
                    app.quit()
                return
            print("[UI] Exception Qt récupérée :", file=sys.stderr)
            traceback.print_exception(exc_type, exc_value, exc_tb)

        sys.excepthook = _qt_exception_hook
        self._app = QApplication.instance() or QApplication(sys.argv)
        self._app.setApplicationName("jarvis-dashboard")
        self._app.setDesktopFileName("jarvis-dashboard")
        self._app.setStyle("Fusion")
        font = load_custom_font()
        self._app.setFont(font)
        self._app.setStyleSheet(get_global_style())

        splash = QSplashScreen(_startup_splash_pixmap())
        splash.show()
        QApplication.processEvents()

        self._win = MainWindow(face_path)
        splash.finish(self._win)
        self._win.show()
        self.root = _RootShim(self._app)
        # Le pointeur et ses fenêtres Qt naissent sur le thread principal.
        # La recherche visuelle peut ensuite tourner dans un worker.
        from ui.visual_pointer import get_visual_pointer
        self._visual_pointer = get_visual_pointer()

    @property
    def muted(self) -> bool:
        return self._win._muted

    @property
    def microphone_locked(self) -> bool:
        """Vrai après une coupure volontaire, jusqu'au prochain clic micro."""
        return bool(getattr(self._win, "_manual_mic_lock", False))

    def show_window(self) -> None:
        self._win.request_show()

    def control_hud_appearance(self, action: str, orb_style: str | None = None,
                               background_path: str | None = None) -> str:
        return self._win.control_hud_appearance(action, orb_style, background_path)

    @muted.setter
    def muted(self, v: bool):
        self._win._setmute_sig.emit(bool(v))

    @property
    def current_file(self) -> str | None:
        return self._win._drop_zone.current_file()

    @property
    def on_text_command(self):
        return self._win.on_text_command

    @on_text_command.setter
    def on_text_command(self, cb):
        self._win.on_text_command = cb

    @property
    def on_remote_clicked(self):
        return self._win.on_remote_clicked

    @on_remote_clicked.setter
    def on_remote_clicked(self, cb):
        self._win.on_remote_clicked = cb

    @property
    def on_interrupt(self):
        return self._win.on_interrupt

    @on_interrupt.setter
    def on_interrupt(self, cb):
        self._win.on_interrupt = cb

    @property
    def on_mic_device_change(self):
        return self._win.on_mic_device_change

    @on_mic_device_change.setter
    def on_mic_device_change(self, cb):
        self._win.on_mic_device_change = cb

    @property
    def on_output_device_change(self):
        return self._win.on_output_device_change

    @on_output_device_change.setter
    def on_output_device_change(self, cb):
        self._win.on_output_device_change = cb

    @property
    def on_plugins_list(self):
        return self._win.on_plugins_list

    @on_plugins_list.setter
    def on_plugins_list(self, cb):
        self._win.on_plugins_list = cb

    @property
    def on_plugin_toggle(self):
        return self._win.on_plugin_toggle

    @on_plugin_toggle.setter
    def on_plugin_toggle(self, cb):
        self._win.on_plugin_toggle = cb

    @property
    def on_mic_sensitivity_change(self):
        return self._win.on_mic_sensitivity_change

    @on_mic_sensitivity_change.setter
    def on_mic_sensitivity_change(self, cb):
        self._win.on_mic_sensitivity_change = cb

    @property
    def on_voice_change(self):
        return self._win.on_voice_change

    @on_voice_change.setter
    def on_voice_change(self, cb):
        self._win.on_voice_change = cb

    @property
    def on_brain_change(self):
        return self._win.on_brain_change

    @on_brain_change.setter
    def on_brain_change(self, cb):
        self._win.on_brain_change = cb

    @property
    def on_voice_provider_change(self):
        return self._win.on_voice_provider_change

    @on_voice_provider_change.setter
    def on_voice_provider_change(self, cb):
        self._win.on_voice_provider_change = cb

    @property
    def on_elevenlabs_voice_change(self):
        return self._win.on_elevenlabs_voice_change

    @on_elevenlabs_voice_change.setter
    def on_elevenlabs_voice_change(self, cb):
        self._win.on_elevenlabs_voice_change = cb

    @property
    def on_stt_provider_change(self):
        return self._win.on_stt_provider_change

    @on_stt_provider_change.setter
    def on_stt_provider_change(self, cb):
        self._win.on_stt_provider_change = cb

    def notify_phone_connected(self) -> None:
        self._win.notify_phone_connected()

    def set_state(self, state: str):
        self._win._state_sig.emit(state)

    def show_clock_particles(self, duration: float = 8.5) -> None:
        """Demande Qt-safe : l'orbe forme HH:MM avec ses particules."""
        self._win._clock_particles_sig.emit(float(duration))

    def set_volume(self, level: float):
        try:
            value = float(level)
        except (TypeError, ValueError):
            return
        if not math.isfinite(value):
            return
        self._win._volume_sig.emit(max(0.0, min(1.0, value)))

    def feed_audio_spectrum(self, pcm: bytes, sample_rate: int, emitted: bool) -> None:
        """Transporte du PCM vers le visualiseur sur le thread Qt."""
        if os.environ.get("ANOGPT_DISABLE_AUDIO_SPECTRUM", "").strip().lower() in {
            "1", "true", "yes", "on",
        }:
            return
        if pcm:
            self._win._audio_pcm_sig.emit(bytes(pcm), int(sample_rate), bool(emitted))

    def write_log(self, text: str):
        self._win._log_sig.emit(text)

    def set_user_transcript(self, text: str, final: bool = False, turn_id: str = ""):
        self._win._transcript_sig.emit(text, final, turn_id)

    def wait_for_api_key(self):
        while not self._win._ready:
            time.sleep(0.1)

    def show_content(self, title: str, text: str):
        self._win._content_sig.emit(title[:48], text[:4000])

    def prompt_reconfig(self):
        self._win._ready = False
        self._win._reconfig_sig.emit()

    def show_camera_frame(self, img_bytes: bytes):
        self._win._camera_sig.emit(img_bytes)

    def set_camera_state(self, state: dict) -> None:
        active = bool((state or {}).get("active"))
        source = str((state or {}).get("source") or "camera").upper()
        lens = str((state or {}).get("lens") or "").upper()
        if active:
            suffix = f" // {source}" + (f" · {lens}" if lens else "")
            self._win._cam_title.setText("◈  FLUX CAMERA" + suffix)
        self._win._cam_stream_sig.emit(active)

    def show_gesture(self, icon: str, label: str = "", value: float = 0.0) -> None:
        """Affiche une notification de geste holographique sur l'orbe HUD."""
        self._win._gesture_sig.emit(icon, label, value)

    def set_continuous_vision_state(self, active: bool) -> None:
        """Active ou désactive l'icône néon d'œil sur l'orbe HUD."""
        sig = getattr(self._win, "_continuous_vision_sig", None)
        if sig is not None:
            sig.emit(bool(active))
        elif hasattr(self._win, "hud") and self._win.hud:
            self._win.hud.set_continuous_vision(bool(active))

    def show_thought(self, text: str, is_active: bool = True) -> None:
        """Affiche les micro-étapes de réflexion dans la bulle translucide sous l'orbe."""
        sig = getattr(self._win, "_thought_sig", None)
        if sig is not None:
            sig.emit(str(text or ""), bool(is_active))
        elif hasattr(self._win, "_thought_overlay") and self._win._thought_overlay:
            if is_active:
                self._win._thought_overlay.show_thought(text)
            else:
                self._win._thought_overlay.fade_out()

    def set_accent_color(self, hex_color: str, palette: dict | None = None) -> None:
        """Ajuste dynamiquement l'accent de couleur et l'orbe de l'interface (support modes métiers / personas)."""
        sig = getattr(self._win, "_accent_sig", None)
        if sig is not None:
            sig.emit(str(hex_color or ""), palette)
        elif hasattr(self._win, "_on_accent_changed"):
            self._win._on_accent_changed(hex_color, palette)

    def hide_thought(self) -> None:
        """Masque la bulle de pensée en cours sous l'orbe."""
        self.show_thought("", is_active=False)

    @property
    def on_camera_action(self):
        return self._win.on_camera_action

    @on_camera_action.setter
    def on_camera_action(self, cb):
        self._win.on_camera_action = cb

    @property
    def on_camera_open(self):
        return self._win.on_camera_open

    @on_camera_open.setter
    def on_camera_open(self, cb):
        self._win.on_camera_open = cb

    @property
    def on_camera_close(self):
        return self._win.on_camera_close

    @on_camera_close.setter
    def on_camera_close(self, cb):
        self._win.on_camera_close = cb

    def start_camera_stream(self) -> None:
        self._win.start_camera_stream()

    def stop_camera_stream(self) -> None:
        self._win.stop_camera_stream()

    def show_map(self, title: str, lat: float, lon: float, radius_km: float = 3.0,
                 view: str | None = None, country: dict | None = None) -> bool:
        return self._win.show_map(title, lat, lon, radius_km, view=view, country=country)

    def update_live_position(self, lat: float, lon: float, accuracy_m: float | None = None,
                             heading: float | None = None, speed: float | None = None) -> None:
        self._win.update_live_position(lat, lon, accuracy_m, heading, speed)

    def start_navigation(self, dest_lat: float, dest_lon: float, dest_name: str = "Destination") -> None:
        self._win.start_navigation(dest_lat, dest_lon, dest_name)

    def close_map(self) -> None:
        self._win.close_map()

    def show_image_gallery(self, query: str, images: list[dict]) -> None:
        self._win.show_image_gallery(query, images)

    def show_generated_image_preview(self, prompt: str, image_bytes: bytes, path: str) -> None:
        self._win.show_generated_image_preview(prompt, image_bytes, path)

    def show_last_generated_image(self) -> bool:
        return self._win.show_last_generated_image()

    def show_generated_artifact_preview(self, kind: str, title: str, path: str) -> None:
        self._win.show_generated_artifact_preview(kind, title, path)

    def close_image_gallery(self) -> None:
        self._win.close_image_gallery()

    def show_video_results(self, query: str, videos: list[dict]) -> None:
        self._win.show_video_results(query, videos)

    def play_video(self, video: dict, playlist: list[dict] | None = None) -> None:
        self._win.play_video(video, playlist)

    def control_video(self, action: str, value=None) -> None:
        self._win.control_video(action, value)

    def close_video(self) -> None:
        self._win.close_video()

    def video_status(self) -> str:
        return self._win.video_status()

    def show_card(self, card_type: str, title: str, body: str, actions: list | None = None) -> None:
        self._win.show_card(card_type, title, body, actions or [])

    def show_music_download(self, payload: dict) -> None:
        self._win.show_music_download(payload or {})

    def task_card(self, task_id: str, title: str, body: str = "", status: str = "running") -> None:
        self._win.task_card(task_id, title, body, status)

    def update_card(self, card_type: str, title: str, body: str) -> None:
        self._win._update_card_sig.emit(card_type, title, body)

    def dismiss_cards(self, card_type: str = "", title: str = "") -> None:
        self._win.dismiss_cards(card_type, title)

    def show_nearby_map(self, query: str, center_lat: float, center_lon: float, places: list) -> None:
        self._win.show_nearby_map(query, center_lat, center_lon, places)

    @property
    def assistant_name(self) -> str:
        return self._win._assistant_name

    def start_speaking(self):
        self.set_state("SPEAKING")

    def stop_speaking(self):
        if not self.muted:
            self.set_state("LISTENING")

    @property
    def visual_pointer(self):
        """Gestionnaire d'annotations visuelles sur écran (Wayland overlay)."""
        return self._visual_pointer

    def highlight_region(
        self, x: float, y: float, w: float, h: float, label: str = "Ici", duration: float = 3.0
    ) -> None:
        """Rectangle néon pulsant avec flèche animée pointant vers la cible."""
        self.visual_pointer.highlight_region(x, y, w, h, label=label, duration=duration)

    def laser_point(self, x: float, y: float, duration: float = 2.0) -> None:
        """Point rouge laser avec ondes concentriques et réticule tournant."""
        self.visual_pointer.laser_point(x, y, duration=duration)

    def draw_path(self, points: list, duration: float = 3.5, label: str = "") -> None:
        """Trajectoire visuelle montrant un déplacement recommandé."""
        self.visual_pointer.draw_path(points, duration=duration, label=label)

    def point_on_screen(
        self,
        description: str,
        coordinates: list,
        mode: str = "auto",
        duration: float = 3.0,
    ) -> str:
        """Déclenche l'annotation visuelle sur écran pour Gemini / MCP."""
        return self.visual_pointer.point_on_screen(
            description, coordinates, mode=mode, duration=duration
        )

    def clear_visual_pointers(self) -> None:
        """Efface immédiatement les annotations visuelles affichées."""
        self.visual_pointer.clear()
