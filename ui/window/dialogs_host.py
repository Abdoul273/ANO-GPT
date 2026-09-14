from __future__ import annotations

import os
import threading


from PyQt6.QtWidgets import (
    QApplication,
)

from ui.panels.clipboard import ClipboardPanel
from ui.dialogs.ai_config import AIConfigOverlay
from ui.dialogs.audio import AudioSettingsOverlay
from ui.dialogs.customize import CustomizeOverlay
from ui.dialogs.memory import MemoryOverlay
from ui.dialogs.plugins import PluginOverlay
from ui.dialogs.remote import RemoteKeyOverlay
from ui.dialogs.setup import SetupOverlay
from ui.paths import CONFIG_DIR, _read_full_config, _write_full_config
from ui.styles.theme import (
    C, DEFAULT_UI_COLOR, apply_ui_accent, current_palette, make_svg_icon,
    retheme_all_widgets,
)


class DialogsHostMixin:
    def _dismiss_interactive_overlays(self) -> None:
        """Retire les panneaux qui peuvent intercepter les clics sous une vue plein écran.

        Une carte, la caméra ou une vidéo recouvre tout le HUD. Laisser le
        tiroir ou le voile d'un réglage ouverts derrière elle désynchronise le
        bouton engrenage (il reste coché) et peut laisser un scrim invisible
        capturer le prochain clic quand la vue se ferme.
        """
        if hasattr(self, "_quick_drawer"):
            self._quick_drawer.hide()
        if hasattr(self, "_drawer_btn"):
            self._drawer_btn.setChecked(False)
        for attribute in (
            "_remote_overlay", "_customize_overlay", "_ai_config_overlay",
            "_audio_settings_overlay", "_memory_overlay", "_plugin_overlay",
            "_welcome_overlay",
        ):
            overlay = getattr(self, attribute, None)
            if overlay is None:
                continue
            overlay.hide()
            # FadeInWidget termine normalement son fondu plus tard. Ici, la
            # surface plein écran prend immédiatement la main : le scrim doit
            # donc cesser de recevoir les clics tout de suite.
            scrim = getattr(overlay, "_scrim", None)
            if scrim is not None:
                scrim.hide()

    def _show_settings_overlay(self, overlay, role, attribute):
        # Les dialogues doivent participer au même ordre de superposition
        # que les cartes, y compris après un recalcul du HUD.
        previous = getattr(self, attribute, None)
        if previous is not None and previous is not overlay:
            previous.hide()
            scrim = getattr(previous, "_scrim", None)
            if scrim is not None:
                scrim.hide()
                scrim.deleteLater()
            previous.deleteLater()
        setattr(self, attribute, overlay)
        self._quick_drawer.hide()
        self._drawer_btn.setChecked(False)
        self.centralWidget().layout().add_role(overlay, role)
        overlay.show()
        self._relayout()

    def notify_phone_connected(self) -> None:
        if self._remote_overlay and self._remote_overlay.isVisible():
            self._remote_overlay.mark_connected()

    def _open_remote(self):
        if not self.on_remote_clicked:
            self._log.append_log("SYS : tableau de bord arrêté. Accès distant indisponible.")
            return
        result = self.on_remote_clicked()
        if not result:
            self._log.append_log("SYS : impossible de générer la clé distante.")
            return
        url    = result[0]
        key    = result[1]
        auto   = result[2] if len(result) >= 3 else ""
        manual = result[3] if len(result) >= 4 else url
        if self._remote_overlay:
            self._remote_overlay._do_close()
        cw  = self.centralWidget()
        ow, oh = RemoteKeyOverlay._OW, RemoteKeyOverlay._OH
        ov  = RemoteKeyOverlay(url, key, auto_login_url=auto, manual_url=manual,
                               expiry_secs=600, parent=cw)
        ov.set_new_key_callback(self.on_remote_clicked)
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.closed.connect(lambda: setattr(self, '_remote_overlay', None))
        self._show_settings_overlay(ov, "remote", "_remote_overlay")
        self._log.append_log(f"SYS : clé distante générée. Saisie manuelle : {manual or url}")
    def _open_customize(self):
        cfg = _read_full_config()
        if self._customize_overlay:
            self._customize_overlay.hide()
        cw = self.centralWidget()
        ov = CustomizeOverlay(
            cfg.get("assistant_name", "ANO-GPT") or "ANO-GPT",
            cfg.get("user_name", ""),
            cfg.get("ui_color", "") or DEFAULT_UI_COLOR,
            cfg.get("background_image", ""),
            parent=cw,
        )
        ow, oh = CustomizeOverlay._OW, CustomizeOverlay._OH
        oh = min(oh, cw.height() - 16)
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.on_preview = self._preview_ui_color
        # Le choix d'une vignette s'affiche tout de suite ; « Appliquer »
        # persiste simplement ce choix dans la configuration.
        ov.on_background_preview = self.set_background_image
        ov.saved.connect(self._apply_name_update)
        self._show_settings_overlay(ov, "customize", "_customize_overlay")

    def _open_ai_config(self):
        if self._ai_config_overlay:
            self._ai_config_overlay.hide()
        cw = self.centralWidget()
        ov = AIConfigOverlay(parent=cw)
        ow, oh = AIConfigOverlay._OW, AIConfigOverlay._OH
        oh = min(oh, cw.height() - 16)
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.provider_changed.connect(self._on_ai_provider_changed)
        ov.voice_key_changed.connect(self._on_voice_key_changed)
        self._show_settings_overlay(ov, "ai_config", "_ai_config_overlay")

    def _on_ai_provider_changed(self, provider: str) -> None:
        """Trace le choix et fait repartir la session vocale sur ce cerveau."""
        if hasattr(self, "_log"):
            self._log.write_log(f"SYS : cerveau IA changé pour « {provider} ».")
        callback = getattr(self, "on_brain_change", None)
        if callable(callback):
            try:
                callback(provider)
            except Exception as exc:
                if hasattr(self, "_log"):
                    self._log.write_log(f"ERR : bascule de cerveau impossible — {exc}")

    def _on_voice_key_changed(self) -> None:
        """La clé de la voix a changé : la session Gemini Live doit repartir."""
        if hasattr(self, "_log"):
            self._log.write_log("SYS : clé voix Gemini mise à jour — reconnexion vocale.")
        callback = getattr(self, "on_brain_change", None)
        if callable(callback):
            try:
                from core.llm_client import _load_config
                callback(str(_load_config().get("brain_provider") or "auto"))
            except Exception as exc:
                if hasattr(self, "_log"):
                    self._log.write_log(f"ERR : reconnexion vocale impossible — {exc}")

    def _open_audio_settings(self):
        if self._audio_settings_overlay:
            self._audio_settings_overlay.hide()
        cw = self.centralWidget()
        ov = AudioSettingsOverlay(win=self, parent=cw)
        ow, oh = AudioSettingsOverlay._OW, AudioSettingsOverlay._OH
        oh = min(oh, cw.height() - 16)
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        self._show_settings_overlay(ov, "audio", "_audio_settings_overlay")

    def _open_memory(self):
        if self._memory_overlay:
            self._memory_overlay.hide()
        cw = self.centralWidget()
        ov = MemoryOverlay(parent=cw)
        ow, oh = MemoryOverlay._OW, min(MemoryOverlay._OH, cw.height() - 16)
        ov.setGeometry((cw.width() - ow) // 2, (cw.height() - oh) // 2, ow, oh)
        self._show_settings_overlay(ov, "memory", "_memory_overlay")

    def _open_plugins(self):
        if self._plugin_overlay:
            self._plugin_overlay.hide()
        cw = self.centralWidget()
        ov = PluginOverlay(self, parent=cw)
        ow, oh = PluginOverlay._OW, min(PluginOverlay._OH, cw.height() - 16)
        ov.setGeometry((cw.width() - ow) // 2, (cw.height() - oh) // 2, ow, oh)
        self._show_settings_overlay(ov, "plugin", "_plugin_overlay")

    def _preview_ui_color(self, hex_color: str):
        old = current_palette()
        if apply_ui_accent(hex_color):
            retheme_all_widgets(old, current_palette())

    def _apply_name_update(self, name: str, user_name: str, ui_color: str = "",
                           background_image: str = ""):
        self._assistant_name = name.strip() or "ANO-GPT"
        display = self._assistant_name.upper()
        self.setWindowTitle(f"{display} — NEURAL INTERFACE")
        self._title_lbl.setText(display)
        if display in ("JARVIS", "J.A.R.V.I.S"):
            self._sub_lbl.setText("Just A Rather Very Intelligent System")
        else:
            self._sub_lbl.setText("Assistant IA personnel")
        self._log._ai_name_lc = self._assistant_name.lower()
        self.hud._assistant_name = display
        if hasattr(self, "_speech_overlay"):
            self._speech_overlay.set_assistant_name(self._assistant_name)
        color_changed = False
        if ui_color:
            old = current_palette()
            if apply_ui_accent(ui_color):
                retheme_all_widgets(old, current_palette())
                color_changed = old["PRI"] != C.PRI
        try:
            data = _read_full_config()
            data["assistant_name"] = self._assistant_name
            data["user_name"] = user_name.strip()
            if ui_color:
                data["ui_color"] = ui_color.strip().lower()
            data["background_image"] = background_image.strip()
            _write_full_config(data)
            self.set_background_image(background_image)
            self._log.append_log(f"SYS : identité mise à jour — {display}")
            self._log.append_log(
                "SYS : image d’arrière-plan appliquée."
                if background_image else "SYS : image d’arrière-plan retirée."
            )
            if color_changed:
                self._log.append_log(f"SYS : couleur de l’interface appliquée — {ui_color}")
        except Exception as e:
            self._log.append_log(f"ERR : échec de l’enregistrement de la configuration — {e}")

    def _on_clipboard_changed(self):
        try:
            text = QApplication.clipboard().text().strip()
            if len(text) >= 10:
                self._clipboard_sig.emit(text)
        except Exception:
            pass

    def _show_clipboard_panel(self, text: str):
        self._clipboard_panel.show_clipboard(text)
        self._position_clipboard_panel()

    def _position_clipboard_panel(self):
        cw = self.centralWidget()
        pw = ClipboardPanel._W
        ph = self._clipboard_panel.sizeHint().height() or ClipboardPanel._H
        x = (cw.width() - pw) // 2
        y = cw.height() - ph - 6
        self._clipboard_panel.setGeometry(x, y, pw, ph)
        self._clipboard_panel.raise_()

    def _on_clipboard_action(self, cmd: str):
        if self.on_text_command:
            threading.Thread(target=self.on_text_command, args=(cmd,), daemon=True).start()

    def _do_interrupt(self):
        # Le retour visuel ne dépend pas de la prochaine itération asyncio :
        # un clic sur Arrêter doit rendre l'état ÉCOUTE immédiatement.
        if not self._muted:
            self._apply_state("LISTENING")
        if hasattr(self, "_video_hub") and self._video_hub.isVisible():
            self._video_hub.close_video()
        if hasattr(self, "_image_gallery") and self._image_gallery.isVisible():
            self._image_gallery.close_gallery()
        if self.on_interrupt:
            self.on_interrupt()

    def _set_muted(self, value: bool):
        """Idempotent mute setter — safe target for cross-thread signals."""
        target = bool(value)
        if not target and getattr(self, "_manual_mic_lock", False):
            # Les réglages, le réveil vocal et les appels inter-fils passent
            # ici. Seul `_toggle_mute`, appelé par le bouton micro, peut lever
            # ce verrou.
            if hasattr(self, "_log"):
                self._log.append_log(
                    "SYS : micro verrouillé — réactive-le avec le bouton micro."
                )
            return
        if target != self._muted:
            self._apply_mute_state(target)

    def _toggle_mute(self):
        """Bascule demandée par un clic volontaire sur un bouton micro."""
        target = not self._muted
        self._manual_mic_lock = target
        self._apply_mute_state(target)

    def _apply_mute_state(self, muted: bool):
        """Applique l'état UI sans décider qui a le droit de le modifier."""
        self._muted = bool(muted)
        self.hud.muted = self._muted
        self._style_mute_btn()
        if self._muted:
            self._apply_state("MUTED")
            self._log.append_log("SYS : micro coupé.")
        else:
            self._apply_state("LISTENING")
            self._log.append_log("SYS : micro actif.")

    def _mute_from_shortcut(self):
        """F4 peut couper le micro, mais ne contourne jamais le verrou manuel."""
        if self._muted:
            self._log.append_log("SYS : micro verrouillé — clique sur le bouton micro pour l'activer.")
            return
        self._toggle_mute()

    def _style_mute_btn(self):
        """Micro coupé = état anormal, teinter en rouge ; micro actif = accent cyan."""
        if not hasattr(self, "_mute_btn"):
            return
        if self._muted:
            self._mute_btn.setIcon(make_svg_icon("mic-off", C.MUTED_C, 20))
            self._mute_btn.setToolTip("Micro coupé · Cliquez sur ce bouton pour réactiver")
            self._mute_btn.setStyleSheet(f"""
                QPushButton {{
                    background: rgba(255, 51, 102, 0.18); color: {C.MUTED_C};
                    border: 1px solid rgba(255, 51, 102, 0.55); border-radius: 10px;
                    padding: 0;
                }}
                QPushButton:hover {{ background: rgba(255, 51, 102, 0.28); }}
            """)
        else:
            self._mute_btn.setIcon(make_svg_icon("mic", C.PRI, 20))
            self._mute_btn.setToolTip("Micro actif · Clic ou F4 pour couper")
            self._mute_btn.setStyleSheet(f"""
                QPushButton {{
                    background: {C.PRI_GHO}; color: {C.PRI};
                    border: 1px solid {C.PRI_DIM}; border-radius: 10px;
                    padding: 0;
                }}
                QPushButton:hover {{
                    background: rgba(0, 212, 255, 0.25);
                    border-color: {C.PRI};
                }}
            """)
        if hasattr(self, "_status_pill"):
            self._status_pill.set_state("MUTED" if self._muted else self.hud.state)

    def _send(self):
        txt = self._input.text().strip()
        if not txt: return
        self._input.clear()
        self._log.append_log(f"Vous : {txt}")
        if self.on_text_command:
            threading.Thread(target=self.on_text_command, args=(txt,), daemon=True).start()

    def _apply_state(self, state: str):
        self.hud.state    = state
        self.hud.speaking = (state == "SPEAKING")
        self._status_pill.set_state("MUTED" if self._muted else state)
        if hasattr(self, "_interrupt_btn"):
            if state in ("SPEAKING", "THINKING", "ACTING") or self.hud.speaking:
                self._interrupt_btn.show()
            else:
                self._interrupt_btn.hide()

    def _check_config(self) -> bool:
        d = _read_full_config()
        return bool(d.get("gemini_api_key")) and bool(d.get("os_system"))

    def _show_setup(self):
        ov = SetupOverlay(self.centralWidget())
        cw = self.centralWidget()
        ow, oh = 460, 390
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.done.connect(self._on_setup_done)
        ov.show()
        self._overlay = ov

    def _on_setup_done(self, key: str, os_name: str):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        _write_full_config({"gemini_api_key": key, "os_system": os_name})
        self._ready = True
        if self._overlay:
            self._overlay.hide()
            self._overlay = None
        self._apply_state("LISTENING")
        self._assistant_name = _read_full_config().get("assistant_name", "ANO-GPT") or "ANO-GPT"
        self._log.append_log(f"SYS : initialisé. OS={os_name.upper()}. {self._assistant_name} en ligne.")
        self._show_welcome_hud()

    def _show_welcome_hud(self) -> None:
        """Affiche l'écran de bienvenue HUD Hacker avec animations et sound design."""
        from ui.dialogs.welcome_hud import WelcomeHudOverlay
        if getattr(self, "_welcome_overlay", None) is not None:
            self._welcome_overlay.show()
            self._welcome_overlay.raise_()
            self._relayout()
            return
        cw = self.centralWidget()
        ov = WelcomeHudOverlay(cw, assistant_name=self._assistant_name)
        self._welcome_overlay = ov
        ov.engaged.connect(self._on_welcome_engaged)
        ov.dismissed.connect(self._on_welcome_dismissed)
        cw.layout().add_role(ov, "welcome")
        ov.show()
        ov.raise_()
        self._relayout()

    def _on_welcome_engaged(self) -> None:
        self._apply_state("LISTENING")
        if hasattr(self, "_log") and self._log:
            self._log.append_log(f"SYS : {self._assistant_name} — noyau initialisé avec succès.")

    def _on_welcome_dismissed(self) -> None:
        ov = getattr(self, "_welcome_overlay", None)
        if ov is not None:
            ov.hide()
            ov.deleteLater()
            self._welcome_overlay = None
        self._relayout()
