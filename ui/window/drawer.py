from __future__ import annotations

import threading
import time

import psutil

from PyQt6.QtCore import (
    QSize, Qt,
)
from PyQt6.QtGui import (
    QFont,
)
from PyQt6.QtWidgets import (
    QComboBox, QFrame, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from ui.core.metrics import _metrics
from ui.panels.log_widget import LogWidget
from ui.panels.floating_panel import FloatingPanel
from ui.styles.theme import C, hairline, make_svg_icon, section_label


class DrawerMixin:
    def _update_metrics(self):
        # Sur batterie, l'intervalle passe de 2s à 5s : la télémétrie n'a pas
        # besoin d'être temps réel, et ça évite de réveiller le CPU pour rien
        # (ne pas ruiner le profil énergie déjà soigné de la machine).
        try:
            b = psutil.sensors_battery()
            want_ms = 5000 if (b and not b.power_plugged) else 2000
            if self._metric_tmr.interval() != want_ms:
                self._metric_tmr.setInterval(want_ms)
        except Exception:
            pass

        snap = _metrics.snapshot()
        cpu = snap["cpu"]
        self._bar_cpu.set_value(cpu, f"{cpu:.0f}%")
        mem = snap["mem"]
        self._bar_mem.set_value(mem, f"{mem:.0f}%")
        net = snap["net"]
        if net < 1.0:
            net_str = f"{net*1024:.0f}KB/s"
        else:
            net_str = f"{net:.1f}MB/s"
        net_pct = min(100, net * 10)
        self._bar_net.set_value(net_pct, net_str)
        gpu = snap["gpu"]
        if gpu >= 0:
            self._bar_gpu.set_value(gpu, f"{gpu:.0f}%")
        else:
            self._bar_gpu.set_value(0, "N/A")
        tmp = snap["tmp"]
        if tmp >= 0:
            tmp_pct = min(100, (tmp / 100) * 100)
            self._bar_tmp.set_value(tmp_pct, f"{tmp:.0f}°C")
        else:
            self._bar_tmp.set_value(0, "N/A")
        try:
            boot_t  = psutil.boot_time()
            elapsed = time.time() - boot_t
            h = int(elapsed // 3600)
            m = int((elapsed % 3600) // 60)
            self._uptime_lbl.setText(f"{h:02d}:{m:02d}")
        except Exception:
            self._uptime_lbl.setText("--:--")
        try:
            proc_count = len(psutil.pids())
            self._proc_lbl.setText(str(proc_count))
        except Exception:
            self._proc_lbl.setText("--")

    def _build_quick_drawer(self) -> QWidget:
        # Une seule silhouette dans toute l'application : liseré cyan discret,
        # remplissage cyan → magenta au survol. L'entrée principale se distingue
        # par son liseré gauche plein, pas par une forme différente.
        _BTN_STYLE_PRI = f"""
            QPushButton {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 rgba(0, 212, 255, 0.26), stop:1 rgba(143, 92, 255, 0.18));
                color: {C.WHITE};
                border: 1px solid {C.PRI}; border-left: 2px solid {C.PRI};
                border-radius: 4px;
                text-align: left; padding: 0 12px; font-weight: 600;
            }}
            QPushButton:hover {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 rgba(0, 212, 255, 0.42), stop:1 rgba(255, 43, 214, 0.28));
            }}
        """
        _BTN_STYLE_DIM = f"""
            QPushButton {{
                background: rgba(14, 23, 36, 0.92); color: {C.TEXT_MED};
                border: 1px solid rgba(0, 212, 255, 0.30); border-radius: 4px;
                text-align: left; padding: 0 12px; font-weight: 500;
            }}
            QPushButton:hover {{
                color: {C.WHITE};
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 rgba(0, 212, 255, 0.22), stop:1 rgba(255, 43, 214, 0.16));
                border-color: {C.PRI};
            }}
        """
        w = FloatingPanel("Paramètres", closeable=True, parent=self.centralWidget())
        w.closed.connect(lambda: self._drawer_btn.setChecked(False))
        # Même châssis que Système et Briefing, sans animation supplémentaire.
        w._fx_timer.stop()
        w.hide()
        scroll = QScrollArea(w)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        body = QWidget()
        body.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(body)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(6)
        scroll.setWidget(body)
        w.add_widget(scroll, stretch=1)
        remote_btn = QPushButton("Contrôle à distance")
        remote_btn.setFixedHeight(34)
        remote_btn.setFont(QFont("Inter", 8, QFont.Weight.Bold))
        remote_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        remote_btn.setIcon(make_svg_icon("activity", C.PRI, 15))
        remote_btn.setIconSize(QSize(15, 15))
        remote_btn.setStyleSheet(_BTN_STYLE_PRI)
        remote_btn.clicked.connect(self._open_remote)
        lay.addWidget(remote_btn)
        welcome_btn = QPushButton("Écran de bienvenue HUD  ·  F1")
        welcome_btn.setFixedHeight(32)
        welcome_btn.setFont(QFont("Inter", 8))
        welcome_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        welcome_btn.setIcon(make_svg_icon("cpu", C.PRI, 14))
        welcome_btn.setIconSize(QSize(14, 14))
        welcome_btn.setStyleSheet(_BTN_STYLE_DIM)
        welcome_btn.clicked.connect(self._show_welcome_hud)
        lay.addWidget(welcome_btn)
        fs_btn = QPushButton("Plein écran  ·  F11")
        fs_btn.setFixedHeight(32)
        fs_btn.setFont(QFont("Inter", 8))
        fs_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        fs_btn.setIcon(make_svg_icon("square", C.TEXT_MED, 14))
        fs_btn.setIconSize(QSize(14, 14))
        fs_btn.setStyleSheet(_BTN_STYLE_DIM)
        fs_btn.clicked.connect(self._toggle_fullscreen)
        lay.addWidget(fs_btn)
        sc_btn = QPushButton("Créer un raccourci")
        sc_btn.setFixedHeight(32)
        sc_btn.setFont(QFont("Inter", 8))
        sc_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        sc_btn.setStyleSheet(_BTN_STYLE_DIM)
        sc_btn.clicked.connect(self._create_desktop_shortcut)
        lay.addWidget(sc_btn)
        self._autostart_btn = QPushButton("◉  DEMARRAGE AUTO : NON")
        self._autostart_btn.setFixedHeight(32)
        self._autostart_btn.setFont(QFont("Inter", 8))
        self._autostart_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._autostart_btn.clicked.connect(self._toggle_autostart)
        lay.addWidget(self._autostart_btn)
        cust_btn = QPushButton("Personnaliser")
        cust_btn.setFixedHeight(32)
        cust_btn.setFont(QFont("Inter", 8))
        cust_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cust_btn.setStyleSheet(_BTN_STYLE_DIM)
        cust_btn.clicked.connect(self._open_customize)
        lay.addWidget(cust_btn)
        lay.addWidget(section_label("Personnalité"))
        self._personality_mode_combo = QComboBox()
        self._personality_mode_combo.setFixedHeight(32)
        self._personality_mode_combo.setFont(QFont("Inter", 8))
        self._personality_mode_combo.setCursor(Qt.CursorShape.PointingHandCursor)
        self._personality_mode_combo.setStyleSheet(f"""
            QComboBox {{
                background: rgba(1, 10, 18, 0.94); color: {C.WHITE};
                border: 1px solid rgba(0, 212, 255, 0.26); border-radius: 4px;
                padding: 0 10px;
            }}
            QComboBox:hover {{ border-color: rgba(0, 212, 255, 0.60); }}
            QComboBox::drop-down {{ border: none; width: 20px; }}
            QComboBox QAbstractItemView {{
                background: #06111c; color: {C.TEXT};
                border: 1px solid rgba(0, 212, 255, 0.40);
                selection-background-color: rgba(0, 212, 255, 0.18);
                selection-color: {C.PRI}; outline: none; padding: 2px;
            }}
        """)
        from core.personality_modes import MODE_SPECS, active_mode
        current_mode = active_mode()
        for mode, spec in MODE_SPECS.items():
            self._personality_mode_combo.addItem(spec.label, mode.value)
        self._personality_mode_combo.setCurrentIndex(
            max(0, self._personality_mode_combo.findData(current_mode.value))
        )
        self._personality_mode_combo.activated.connect(self._on_personality_mode_selected)
        lay.addWidget(self._personality_mode_combo)
        self._personality_mode_hint = QLabel()
        self._personality_mode_hint.setWordWrap(True)
        self._personality_mode_hint.setFont(QFont("Inter", 7))
        self._personality_mode_hint.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        self._set_personality_mode_hint(current_mode.value)
        lay.addWidget(self._personality_mode_hint)
        ai_cfg_btn = QPushButton("Configurer l'IA")
        ai_cfg_btn.setFixedHeight(32)
        ai_cfg_btn.setFont(QFont("Inter", 8))
        ai_cfg_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        ai_cfg_btn.setStyleSheet(_BTN_STYLE_DIM)
        ai_cfg_btn.clicked.connect(self._open_ai_config)
        lay.addWidget(ai_cfg_btn)
        audio_btn = QPushButton("Audio")
        audio_btn.setFixedHeight(32)
        audio_btn.setFont(QFont("Inter", 8))
        audio_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        audio_btn.setIcon(make_svg_icon("volume-2", C.TEXT_MED, 15))
        audio_btn.setIconSize(QSize(15, 15))
        audio_btn.setStyleSheet(_BTN_STYLE_DIM)
        audio_btn.clicked.connect(self._open_audio_settings)
        lay.addWidget(audio_btn)
        memory_btn = QPushButton("Mémoire")
        memory_btn.setFixedHeight(32)
        memory_btn.setFont(QFont("Inter", 8))
        memory_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        memory_btn.setStyleSheet(_BTN_STYLE_DIM)
        memory_btn.clicked.connect(self._open_memory)
        lay.addWidget(memory_btn)
        plugins_btn = QPushButton("Plugins")
        plugins_btn.setFixedHeight(32)
        plugins_btn.setFont(QFont("Inter", 8))
        plugins_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        plugins_btn.setStyleSheet(_BTN_STYLE_DIM)
        plugins_btn.clicked.connect(self._open_plugins)
        lay.addWidget(plugins_btn)
        self._brief_btn = QPushButton()
        self._brief_btn.setFixedHeight(32)
        self._brief_btn.setFont(QFont("Inter", 8))
        self._brief_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._brief_btn.clicked.connect(self._toggle_brief)
        lay.addWidget(self._brief_btn)

        lay.addSpacing(4)
        lay.addWidget(hairline("4px 0"))
        lay.addWidget(section_label("Journal de débogage"))
        self._log = LogWidget()
        self._log.setFixedHeight(200)
        lay.addWidget(self._log)

        w.adjustSize()
        return w

    def _set_personality_mode_hint(self, mode_value: str) -> None:
        from core.personality_modes import MODE_SPECS, normalize_mode
        spec = MODE_SPECS[normalize_mode(mode_value)]
        if spec.key.value == "normal":
            voice_txt = "voix : votre choix dans Audio"
        else:
            voice_txt = f"voix Gemini : {spec.gemini_voice}"
        self._personality_mode_hint.setText(
            f"Adresse : {spec.user_address} · {voice_txt} · ton à la reconnexion."
        )

    def _on_personality_mode_selected(self, index: int) -> None:
        combo = self._personality_mode_combo
        mode = str(combo.itemData(index) or "normal")
        self._set_personality_mode_hint(mode)
        # Même chemin que les commandes texte : le runtime réinjecte le prompt
        # et reconstruit la session Live, au lieu de ne changer que l'affichage.
        command = f"passe en mode {mode}"
        if self.on_text_command:
            threading.Thread(
                target=self.on_text_command, args=(command,), daemon=True,
                name="personality-mode-change",
            ).start()
            return
        # L'UI peut être affichée avant la connexion Live : conserver le choix
        # reste utile et la prochaine session le prendra en compte.
        from core.personality_modes import set_active_mode
        set_active_mode(mode)

    def _toggle_drawer(self, checked: bool):
        if checked:
            self._quick_drawer.show()
            self._position_quick_drawer()
            self._quick_drawer.raise_()
        else:
            self._quick_drawer.hide()

    def _position_quick_drawer(self):
        if not hasattr(self, '_quick_drawer'):
            return
        self._relayout()
