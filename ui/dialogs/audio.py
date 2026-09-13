from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
import threading


from PyQt6.QtCore import (
    Qt,
    QTimer, pyqtSignal,
)
from PyQt6.QtGui import (
    QFont,
)
from PyQt6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QProgressBar,
    QPushButton, QScrollArea, QSlider,
    QVBoxLayout, QWidget,
)

from core.live_speech_config import DEFAULT_LIVE_VOICE, LIVE_VOICE_OPTIONS, normalise_live_voice

from ui.core.fade_widget import FadeInWidget
from ui.paths import _read_full_config
from ui.styles.cyber import CyberHeader, micro_label
from ui.styles.theme import C

if TYPE_CHECKING:
    from ui.main_window import MainWindow

class AudioSettingsOverlay(FadeInWidget):
    """Panneau « Audio » : choix explicite et persistant du micro et de la
    sortie, vu-mètre en direct, sensibilité VAD et test d'écho."""
    _OW, _OH = 420, 680
    _echo_result = pyqtSignal(str)
    _voices_loaded = pyqtSignal(list, str)

    def __init__(self, win: "MainWindow", parent=None):
        super().__init__(parent, duration=300)
        self._win = win
        self._catalog_loading = False
        self._catalog_loaded = False
        self._voices_loaded.connect(self._on_voices_loaded)
        self._echo_result.connect(self._echo_hint_result)

        shell = QVBoxLayout(self)
        shell.setContentsMargins(20, 16, 20, 16)
        shell.setSpacing(8)
        # Hors de la zone défilante : la croix doit rester atteignable même si
        # une liste d'appareils est plus large que le panneau.
        header = CyberHeader("Audio", "VOIX & CAPTURE", parent=self)
        header.close_clicked.connect(self.hide)
        shell.addWidget(header)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        body = QWidget()
        body.setStyleSheet("background: transparent;")
        scroll.setWidget(body)
        shell.addWidget(scroll)
        outer = QVBoxLayout(body)
        outer.setContentsMargins(0, 0, 6, 0)
        outer.setSpacing(10)

        def _lbl(txt, fs=9, bold=False, color=C.PRI, align=Qt.AlignmentFlag.AlignLeft):
            w = QLabel(txt); w.setAlignment(align)
            w.setFont(QFont("Inter", fs, QFont.Weight.Bold if bold else QFont.Weight.Normal))
            w.setStyleSheet(f"color: {color}; background: transparent;")
            return w

        outer.addWidget(micro_label("Voix de l’assistant"))
        self._voice_combo = QComboBox()
        self._voice_combo.setFixedHeight(34)
        self._voice_combo.setFont(QFont("Inter", 9))
        selected_voice = normalise_live_voice(
            _read_full_config().get("live_voice", DEFAULT_LIVE_VOICE)
        )
        selected_index = 0
        for index, (name, character) in enumerate(LIVE_VOICE_OPTIONS):
            self._voice_combo.addItem(f"{name} — {character}", userData=name)
            if name == selected_voice:
                selected_index = index
        self._provider_combo = QComboBox()
        self._provider_combo.setFixedHeight(34)
        self._provider_combo.addItem("Gemini — voix native", "gemini")
        self._provider_combo.addItem("ElevenLabs", "elevenlabs")
        provider = _read_full_config().get("voice_provider", "gemini")
        self._provider_combo.setCurrentIndex(1 if provider == "elevenlabs" else 0)
        self._voice_combo.setEnabled(provider != "elevenlabs")
        self._provider_combo.activated.connect(self._on_provider_selected)
        outer.addWidget(self._provider_combo)
        self._voice_combo.setCurrentIndex(selected_index)
        self._voice_combo.activated.connect(self._on_voice_selected)
        outer.addWidget(self._voice_combo)
        self._voice_hint = _lbl(
            "Le choix est mémorisé et appliqué par une reconnexion vocale automatique.",
            7, color=C.TEXT_DIM,
        )
        self._voice_hint.setWordWrap(True)
        outer.addWidget(self._voice_hint)

        from core.elevenlabs_voice import DEFAULT_VOICE, DEFAULT_MODEL, MODEL_OPTIONS
        cfg = _read_full_config()
        self._eleven_panel = QWidget()
        eleven_layout = QVBoxLayout(self._eleven_panel)
        eleven_layout.setContentsMargins(0, 0, 0, 0)
        self._eleven_voice_combo = QComboBox()
        self._eleven_voice_combo.setEditable(False)
        self._eleven_voice_combo.setCursor(Qt.CursorShape.PointingHandCursor)
        self._eleven_voice_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._eleven_voice_combo.setMinimumHeight(34)
        self._eleven_voice_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._eleven_voice_combo.setMinimumContentsLength(20)
        self._eleven_voice_combo.setMaxVisibleItems(10)
        vid = cfg.get("elevenlabs_voice_id") or DEFAULT_VOICE
        self._eleven_voice_combo.addItem("George" if vid == DEFAULT_VOICE else "Voix enregistrée", vid)
        self._eleven_voice_combo.activated.connect(self._on_elevenlabs_selected)
        eleven_layout.addWidget(self._eleven_voice_combo)
        self._model_combo = QComboBox()
        self._model_combo.setMinimumHeight(34)
        self._model_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        for model, label in MODEL_OPTIONS:
            self._model_combo.addItem(label, model)
        self._model_combo.setCurrentIndex(max(0, self._model_combo.findData(cfg.get("elevenlabs_model_id") or DEFAULT_MODEL)))
        self._model_combo.activated.connect(self._on_elevenlabs_selected)
        eleven_layout.addWidget(self._model_combo)
        self._voices_refresh = QPushButton("Actualiser les voix")
        self._voices_refresh.clicked.connect(self._load_elevenlabs_voices)
        eleven_layout.addWidget(self._voices_refresh)
        self._catalog_hint = _lbl("Choisissez une voix dans la liste déroulante.", 7, color=C.TEXT_DIM)
        self._catalog_hint.setWordWrap(True)
        eleven_layout.addWidget(self._catalog_hint)
        outer.addWidget(self._eleven_panel)
        self._sync_provider_controls(provider)

        outer.addWidget(micro_label("Reconnaissance de votre voix"))
        self._stt_combo = QComboBox()
        self._stt_combo.setMinimumHeight(34)
        self._stt_combo.addItem("Gemini Transcribe — français, verbatim, haute précision", "gemini")
        self._stt_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._stt_combo.setCurrentIndex(0)
        self._stt_combo.setEnabled(False)
        outer.addWidget(self._stt_combo)
        self._stt_hint = _lbl(
            "Gemini Transcribe reçoit la phrase entière, en français et en mode verbatim. "
            "Seule une transcription finale confirmée peut être exécutée ; "
            "Pendant la réponse, utilisez Échap ou Arrêter pour interrompre. "
            "« ANO stop » est disponible si le détecteur local est actif.",
            7, color=C.TEXT_DIM,
        )
        self._stt_hint.setWordWrap(True)
        outer.addWidget(self._stt_hint)

        outer.addWidget(micro_label("Microphone"))
        self._device_combo = QComboBox()
        self._device_combo.setFixedHeight(32)
        self._device_combo.setFont(QFont("Inter", 9))
        outer.addWidget(self._device_combo)
        self._refresh_devices()
        self._device_combo.currentIndexChanged.connect(self._mark_device_change_pending)

        outer.addWidget(micro_label("Haut-parleurs"))
        self._output_combo = QComboBox()
        self._output_combo.setFixedHeight(32)
        outer.addWidget(self._output_combo)
        self._refresh_outputs()
        self._output_combo.currentIndexChanged.connect(self._mark_device_change_pending)

        self._device_apply = QPushButton("Appliquer les appareils audio")
        self._device_apply.setFixedHeight(34)
        self._device_apply.setFont(QFont("Inter", 8, QFont.Weight.Bold))
        self._device_apply.setObjectName("CyberPrimary")
        self._device_apply.setCursor(Qt.CursorShape.PointingHandCursor)
        self._device_apply.clicked.connect(self._apply_selected_devices)
        outer.addWidget(self._device_apply)
        self._device_apply_hint = _lbl(
            "Choisis le micro et les haut-parleurs, puis applique. Le choix reste mémorisé.",
            7, color=C.TEXT_DIM,
        )
        self._device_apply_hint.setWordWrap(True)
        outer.addWidget(self._device_apply_hint)

        refresh_btn = QPushButton("↻ Rafraîchir la liste")
        refresh_btn.setFixedHeight(26)
        refresh_btn.setFont(QFont("Inter", 8))
        refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh_btn.clicked.connect(self._refresh_all_devices)
        outer.addWidget(refresh_btn)

        outer.addWidget(micro_label("Niveau micro"))
        self._vu_bar = QProgressBar()
        self._vu_bar.setFixedHeight(14)
        self._vu_bar.setRange(0, 100)
        self._vu_bar.setTextVisible(False)
        outer.addWidget(self._vu_bar)
        self._vu_timer = QTimer(self)
        self._vu_timer.timeout.connect(self._poll_vu)
        self._vu_timer.start(60)

        outer.addWidget(micro_label("Sensibilité"))
        self._sens_slider = QSlider(Qt.Orientation.Horizontal)
        self._sens_slider.setRange(0, 100)
        self._sens_slider.setValue(60)  # correspond au réglage par défaut du VAD (-50 dB)
        self._sens_slider.valueChanged.connect(self._on_sensitivity_changed)
        outer.addWidget(self._sens_slider)
        sens_row = QHBoxLayout()
        sens_row.addWidget(_lbl("Peu sensible", 7, color=C.TEXT_DIM))
        sens_row.addStretch()
        sens_row.addWidget(_lbl("Très sensible", 7, color=C.TEXT_DIM))
        outer.addLayout(sens_row)

        outer.addSpacing(4)
        echo_btn = QPushButton("🔊 Tester l'écho")
        echo_btn.setFixedHeight(32)
        echo_btn.setFont(QFont("Inter", 8, QFont.Weight.Bold))
        echo_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        echo_btn.clicked.connect(self._run_echo_test)
        outer.addWidget(echo_btn)
        self._echo_hint = _lbl(
            "Joue un bip dans les haut-parleurs : si le niveau remonte fort "
            "juste après (au lieu de rester bas), l'écho n'est pas bien annulé.",
            7, color=C.TEXT_DIM,
        )
        self._echo_hint.setWordWrap(True)
        outer.addWidget(self._echo_hint)

        outer.addStretch()

    def _refresh_devices(self) -> None:
        try:
            from core.audio_router import list_input_devices, get_manual_override
            devices = list_input_devices()
            override = get_manual_override()
        except Exception:
            devices, override = [], None
        self._device_combo.blockSignals(True)
        self._device_combo.clear()
        self._device_combo.addItem("Automatique (recommandé)", userData=None)
        selected_idx = 0
        for i, d in enumerate(devices, start=1):
            tag = {"bluetooth": "BT", "usb": "USB", "internal": "Interne"}.get(d.kind, "")
            label = f"{d.description}" + (f"  [{tag}]" if tag else "")
            if d.kind == "bluetooth" and d.bt_quality == "narrowband":
                label += "  ⚠ mains-libres"
            self._device_combo.addItem(label, userData=d.name)
            if override and d.name == override:
                selected_idx = i
        self._device_combo.setCurrentIndex(selected_idx)
        self._device_combo.blockSignals(False)

    def _refresh_all_devices(self) -> None:
        self._refresh_devices()
        self._refresh_outputs()
        self._device_apply.setEnabled(True)
        self._device_apply_hint.setText("Liste actualisée : sélectionne les appareils puis applique.")

    def _mark_device_change_pending(self, _index: int) -> None:
        self._device_apply.setEnabled(True)
        self._device_apply_hint.setText(
            "Modification prête : clique sur « Appliquer les appareils audio »."
        )

    def _refresh_outputs(self) -> None:
        try:
            from core.audio_router import list_output_devices, get_output_override
            devices, override = list_output_devices(), get_output_override()
        except Exception:
            devices, override = [], None
        self._output_combo.blockSignals(True)
        self._output_combo.clear()
        self._output_combo.addItem("Sortie système automatique", userData=None)
        selected = 0
        for index, device in enumerate(devices, start=1):
            tag = {"bluetooth": "BT", "usb": "USB", "internal": "Interne"}.get(device.kind, "")
            self._output_combo.addItem(
                device.description + (f"  [{tag}]" if tag else ""),
                userData=device.name,
            )
            if override == device.name:
                selected = index
        self._output_combo.setCurrentIndex(selected)
        self._output_combo.blockSignals(False)

    def _apply_selected_devices(self) -> None:
        """Applique les deux choix comme une opération explicite et durable."""
        microphone = self._device_combo.currentData()
        output = self._output_combo.currentData()
        mic_callback = getattr(self._win, "on_mic_device_change", None)
        output_callback = getattr(self._win, "on_output_device_change", None)
        mic_ok = True
        output_ok = True
        try:
            if mic_callback:
                mic_ok = mic_callback(microphone) is not False
            if output_callback:
                output_ok = output_callback(output) is not False
        except Exception:
            mic_ok = output_ok = False

        if mic_ok and output_ok:
            self._device_apply.setEnabled(False)
            self._device_apply_hint.setText(
                "✓ Appareils appliqués et mémorisés. Le micro se reconnecte immédiatement."
            )
        else:
            self._device_apply_hint.setText(
                "Impossible d'appliquer un appareil : rafraîchis la liste puis réessaie."
            )

    def _sync_provider_controls(self, provider: str) -> None:
        eleven = provider == "elevenlabs"
        self._voice_combo.setVisible(not eleven)
        self._voice_combo.setEnabled(not eleven)
        self._eleven_panel.setVisible(eleven)
        if eleven and not self._catalog_loaded:
            self._load_elevenlabs_voices()

    def _load_elevenlabs_voices(self) -> None:
        if self._catalog_loading:
            return
        self._catalog_loading = True
        self._voices_refresh.setEnabled(False)
        self._catalog_hint.setText("Chargement des voix ElevenLabs…")
        settings = _read_full_config()

        def load():
            from core.elevenlabs_voice import list_voices
            try:
                voices, error = asyncio.run(list_voices(settings)), ""
            except RuntimeError as exc:
                voices, error = [], str(exc)
            except Exception:
                voices, error = [], "Impossible de charger les voix ElevenLabs."
            try:
                self._voices_loaded.emit(voices, error)
            except RuntimeError:
                pass  # Le panneau a été fermé et détruit pendant la requête.

        threading.Thread(target=load, name="elevenlabs-voices", daemon=True).start()

    def _on_voices_loaded(self, voices: list, error: str) -> None:
        self._catalog_loading = False
        self._voices_refresh.setEnabled(True)
        if error:
            self._catalog_hint.setText(error)
            return
        combo = self._eleven_voice_combo
        selected = combo.currentData()
        current_label = combo.currentText()
        combo.blockSignals(True)
        combo.clear()
        for voice in voices:
            combo.addItem(voice["label"], voice["voice_id"])
        index = combo.findData(selected)
        if index < 0 and selected:
            combo.addItem(current_label + " (enregistrée, hors catalogue)", selected)
            index = combo.count() - 1
        combo.setCurrentIndex(max(0, index))
        combo.blockSignals(False)
        self._catalog_loaded = True
        # Les voix ElevenLabs sont propres au compte. Une fois le catalogue
        # reçu, le registre des modes déduit trois candidats distincts à partir
        # de leurs métadonnées puis les persiste pour les futures sessions.
        try:
            from core.personality_modes import (
                choose_elevenlabs_mode_voices,
                save_elevenlabs_mode_voice_ids,
            )
            recommendations = choose_elevenlabs_mode_voices(voices)
            save_elevenlabs_mode_voice_ids(recommendations)
        except Exception:
            recommendations = {}
        recommended_count = len(recommendations)
        self._catalog_hint.setText(
            f"{len(voices)} voix disponibles. {recommended_count} voix de modes proposées automatiquement."
            if voices else "Aucune voix reçue. Ajoutez une voix à votre bibliothèque ElevenLabs."
        )

    def _on_elevenlabs_selected(self, _index: int) -> None:
        callback = getattr(self._win, "on_elevenlabs_voice_change", None)
        if not callback:
            self._voice_hint.setText("Le moteur vocal n’est pas encore connecté à l’interface.")
            return
        try:
            callback(self._eleven_voice_combo.currentData(), self._model_combo.currentData())
            self._voice_hint.setText("Voix ElevenLabs enregistrée — reconnexion en cours…")
        except Exception:
            self._voice_hint.setText("Impossible d’enregistrer la voix ElevenLabs.")

    def _on_provider_selected(self, index: int) -> None:
        provider = self._provider_combo.itemData(index)
        try:
            callback = getattr(self._win, "on_voice_provider_change", None)
            if callback is None:
                raise RuntimeError("Moteur vocal indisponible")
            callback(provider)
            self._sync_provider_controls(provider)
            self._voice_hint.setText("Choix enregistré — reconnexion vocale en cours…")
        except Exception:
            previous = _read_full_config().get("voice_provider", "gemini")
            self._provider_combo.setCurrentIndex(1 if previous == "elevenlabs" else 0)
            self._sync_provider_controls(previous)
            self._voice_hint.setText("Impossible d’enregistrer le fournisseur vocal.")

    def _on_stt_selected(self, index: int) -> None:
        callback = getattr(self._win, "on_stt_provider_change", None)
        try:
            if callback is None:
                raise RuntimeError("Moteur indisponible")
            callback(self._stt_combo.itemData(index))
            self._stt_hint.setText("Reconnaissance enregistrée — reconnexion en cours…")
        except Exception:
            self._stt_combo.setCurrentIndex(0)
            self._stt_hint.setText("Impossible de changer la reconnaissance vocale.")

    def _on_voice_selected(self, index: int) -> None:
        voice = normalise_live_voice(self._voice_combo.itemData(index))
        callback = getattr(self._win, "on_voice_change", None)
        if callback:
            try:
                callback(voice)
                self._voice_hint.setText(
                    f"Voix {voice} enregistrée — reconnexion en cours…"
                )
            except Exception as exc:
                self._voice_hint.setText(f"Impossible de changer la voix : {exc}")

    def _on_sensitivity_changed(self, value: int) -> None:
        if self._win.on_mic_sensitivity_change:
            try:
                self._win.on_mic_sensitivity_change(value / 100.0)
            except Exception:
                pass

    def _poll_vu(self) -> None:
        try:
            level = float(getattr(self._win.hud, "_target_vol", 0.0))
        except Exception:
            level = 0.0
        self._vu_bar.setValue(int(max(0.0, min(1.0, level)) * 100))

    def _run_echo_test(self) -> None:
        self._echo_hint.setText("Initialisation de la sortie audio…")
        threading.Thread(
            target=self._run_echo_test_worker,
            daemon=True,
            name="audio-echo-test",
        ).start()

    def _run_echo_test_worker(self) -> None:
        try:
            import numpy as _np
            import sounddevice as _sd
            sr = 44100
            t = _np.linspace(0, 0.6, int(sr * 0.6), endpoint=False)
            tone = 0.25 * _np.sin(2 * _np.pi * 880 * t) * _np.hanning(len(t))
            _sd.play(tone.astype(_np.float32), sr)
            self._echo_result.emit("Bip joué — observe le niveau du microphone.")
        except Exception as e:
            self._echo_result.emit(f"Impossible de jouer le test : {e}")

    def _echo_hint_result(self, message: str) -> None:
        self._echo_hint.setText(message)

    def hideEvent(self, e):
        self._vu_timer.stop()
        super().hideEvent(e)

    def showEvent(self, e):
        super().showEvent(e)
        self._vu_timer.start(60)
