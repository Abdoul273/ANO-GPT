from __future__ import annotations

import threading
import time
from pathlib import Path


from PyQt6.QtCore import (
    QSize, Qt,
)
from PyQt6.QtGui import (
    QFont,
)
from PyQt6.QtWidgets import (
    QFileDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QTextEdit, QVBoxLayout, QWidget,
)

from ui.panels.file_chip import FileChipWidget, _fmt_size
from ui.panels.floating_panel import FloatingPanel
from ui.panels.telemetry import LiveTranscriptWidget, MetricBar
from ui.core.qtflags import _OS
from ui.paths import _LEFT_W
from ui.styles.theme import C, hairline, make_svg_icon, section_label


class ChromeMixin:
    def _build_header(self) -> QWidget:
        """Contenu de la barre haute flottante (marque, pastille d'état, heure, météo)."""
        w = QWidget()
        w.setStyleSheet("background: transparent; border: none;")
        lay = QHBoxLayout(w)
        lay.setContentsMargins(4, 0, 4, 0)
        lay.setSpacing(10)

        self._drawer_btn = QPushButton()
        self._drawer_btn.setIcon(make_svg_icon("settings", C.TEXT_DIM, 18))
        self._drawer_btn.setIconSize(QSize(18, 18))
        self._drawer_btn.setFixedSize(30, 30)
        self._drawer_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._drawer_btn.setToolTip("Réglages et contrôles")
        self._drawer_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_DIM};
                border: 1px solid transparent; border-radius: 4px; padding: 0;
            }}
            QPushButton:hover {{ color: {C.PRI}; background: {C.ELEV1};
                                 border-color: {C.HAIRLINE_S}; }}
            QPushButton:checked {{ color: {C.PRI}; background: {C.PRI_GHO};
                                   border-color: {C.PRI}; }}
        """)
        self._drawer_btn.setCheckable(True)
        self._drawer_btn.clicked.connect(self._toggle_drawer)
        lay.addWidget(self._drawer_btn)

        brand = QVBoxLayout(); brand.setSpacing(0)
        _disp = self._assistant_name.upper()
        self._title_lbl = QLabel(_disp)
        _tf = QFont("Inter", 13, QFont.Weight.Bold)
        _tf.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.8)
        self._title_lbl.setFont(_tf)
        self._title_lbl.setStyleSheet(f"color: {C.WHITE}; background: transparent;")
        brand.addWidget(self._title_lbl)
        _sub_text = "ANONYMOUS  //  NEURAL INTERFACE"
        self._sub_lbl = QLabel(_sub_text)
        self._sub_lbl.setFont(QFont("Inter", 7))
        self._sub_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        brand.addWidget(self._sub_lbl)
        lay.addLayout(brand)
        lay.addStretch()

        self._weather_lbl = QLabel("")
        self._weather_lbl.setFont(QFont("Inter", 9))
        self._weather_lbl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        self._weather_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self._weather_lbl)

        right_col = QVBoxLayout(); right_col.setSpacing(0)
        self._clock_lbl = QLabel("00:00:00")
        _cf = QFont("Inter", 13, QFont.Weight.Bold)
        _cf.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.0)
        self._clock_lbl.setFont(_cf)
        self._clock_lbl.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        self._clock_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        right_col.addWidget(self._clock_lbl)
        self._date_lbl = QLabel("")
        self._date_lbl.setFont(QFont("Inter", 7))
        self._date_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        self._date_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        right_col.addWidget(self._date_lbl)
        lay.addLayout(right_col)
        return w

    def _tick_clock(self):
        self._clock_lbl.setText(time.strftime("%H:%M:%S"))
        self._date_lbl.setText(time.strftime("%a %d %b %Y"))

    def _refresh_weather(self) -> None:
        threading.Thread(target=self._weather_worker, daemon=True, name="weather-fetch").start()

    def _weather_worker(self) -> None:
        try:
            from core.geolocation import get_user_coords
            from actions.weather_report import _fetch_open_meteo, _WMO_CODES
            coords = get_user_coords()
            if not coords:
                return
            data = _fetch_open_meteo(*coords)
            if not data:
                return
            cur = data.get("current", {}) or {}
            temp = cur.get("temperature_2m")
            code = cur.get("weather_code")
            if temp is None:
                return
            icon = "🌙" if not cur.get("is_day", 1) else "☀️"
            if code is not None:
                if code >= 95:
                    icon = "⛈️"
                elif code >= 71:
                    icon = "❄️"
                elif code >= 61 or code in (80, 81, 82):
                    icon = "🌧️"
                elif code >= 45:
                    icon = "🌫️"
                elif code >= 2:
                    icon = "☁️" if cur.get("is_day", 1) else "🌙"
            desc = _WMO_CODES.get(code, "")
            text = f"{icon} {temp:.0f}°C" + (f" · {desc}" if desc else "")
        except Exception:
            return
        self._weather_sig.emit(text)

    def _build_left_panel(self) -> QWidget:
        """Colonne télémétrie : jauges, puis infos système, puis état des modules."""
        w = QWidget()
        w.setFixedWidth(_LEFT_W)
        w.setObjectName("LeftPanel")
        w.setStyleSheet("""
            QWidget#LeftPanel {
                background: transparent;
                border: none;
            }
        """)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(7)

        lay.addWidget(section_label("Télémétrie active"))

        self._bar_cpu = MetricBar("CPU", "", C.PRI)
        self._bar_mem = MetricBar("MÉMOIRE", "", "#00e0c0")
        self._bar_net = MetricBar("RÉSEAU", "", C.GREEN)
        self._bar_gpu = MetricBar("GPU", "", "#7fb0ff")
        self._bar_tmp = MetricBar("TEMP", "", "#ff8fa8")
        for bar in (self._bar_cpu, self._bar_mem, self._bar_net,
                    self._bar_gpu, self._bar_tmp):
            lay.addWidget(bar)

        lay.addSpacing(2)
        lay.addWidget(hairline())
        lay.addSpacing(2)

        # Infos système : paires intitulé / valeur, sans cadre
        def _kv(key: str, value: str, col: str = C.TEXT):
            row = QHBoxLayout(); row.setContentsMargins(0, 0, 0, 0); row.setSpacing(4)
            k = QLabel(key)
            k.setFont(QFont("Inter", 7))
            k.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            v = QLabel(value)
            v.setFont(QFont("Inter", 8, QFont.Weight.Bold))
            v.setStyleSheet(f"color: {col}; background: transparent;")
            v.setAlignment(Qt.AlignmentFlag.AlignRight)
            row.addWidget(k); row.addStretch(); row.addWidget(v)
            return row, v

        r1, self._uptime_lbl = _kv("ACTIF", "--:--", C.GREEN)
        r2, self._proc_lbl   = _kv("PROCESSUS", "--", C.TEXT)
        os_name = {"Windows": "WIN", "Darwin": "macOS", "Linux": "LINUX"}.get(_OS, _OS.upper())
        r3, _ = _kv("SYSTÈME", os_name, C.TEXT_MED)
        for r in (r1, r2, r3):
            lay.addLayout(r)

        # Transcription en direct, juste sous les infos système : la preuve
        # visible que la parole est bien captée ET comprise.
        lay.addSpacing(6)
        self._live_transcript = LiveTranscriptWidget()
        lay.addWidget(self._live_transcript)

        lay.addStretch()

        # État des modules : puce + libellé, sans boîte
        lay.addWidget(section_label("Modules"))
        for txt, col in (("Noyau IA", C.GREEN),
                         ("Sécurité", C.PRI),
                         ("Protocole XLIX", C.TEXT_DIM)):
            row = QHBoxLayout(); row.setContentsMargins(0, 0, 0, 0); row.setSpacing(7)
            dot = QLabel("●")
            dot.setFont(QFont("Inter", 7))
            dot.setStyleSheet(f"color: {col}; background: transparent;")
            lbl = QLabel(txt)
            lbl.setFont(QFont("Inter", 8))
            lbl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
            row.addWidget(dot); row.addWidget(lbl); row.addStretch()
            lay.addLayout(row)
        return w
    def _build_input_row(self) -> QVBoxLayout:
        wrapper = QVBoxLayout()
        wrapper.setContentsMargins(0, 0, 0, 0)
        wrapper.setSpacing(7)

        command_head = QHBoxLayout()
        command_head.setContentsMargins(5, 0, 5, 0)
        command_head.setSpacing(8)
        link_lbl = section_label("Command link", C.PRI_DIM)
        command_head.addWidget(link_lbl)
        command_head.addStretch()
        key_lbl = QLabel("ENTER  //  EXECUTE")
        key_font = QFont("Inter", 6, QFont.Weight.Bold)
        key_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.2)
        key_lbl.setFont(key_font)
        key_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        command_head.addWidget(key_lbl)
        wrapper.addLayout(command_head)

        # Pastille compacte pour fichier attaché
        self._file_chip = FileChipWidget()
        self._file_chip.file_cleared.connect(self._clear_file)
        self._file_chip.hide()
        wrapper.addWidget(self._file_chip)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)

        # Bouton 📎 Joindre un fichier (Icône vectorielle Lucide)
        self._attach_btn = QPushButton()
        self._attach_btn.setIcon(make_svg_icon("paperclip", C.TEXT_MED, 20))
        self._attach_btn.setIconSize(QSize(20, 20))
        self._attach_btn.setFixedSize(42, 42)
        self._attach_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._attach_btn.setToolTip("Joindre un fichier (Drag & Drop disponible)")
        self._attach_btn.setStyleSheet(f"""
            QPushButton {{
                background: {C.ELEV1}; color: {C.TEXT_MED};
                border: 1px solid {C.HAIRLINE}; border-radius: 10px; padding: 0;
            }}
            QPushButton:hover {{
                background: {C.ELEV2}; color: {C.PRI};
                border-color: {C.HAIRLINE_S};
            }}
            QPushButton:pressed {{ background: {C.SURFACE}; }}
        """)
        self._attach_btn.clicked.connect(self._browse_file)
        row.addWidget(self._attach_btn)

        # Champ de texte principal
        self._input = QLineEdit()
        self._input.setPlaceholderText("Demandez, créez ou contrôlez votre système…")
        self._input.setFont(QFont("Inter", 9))
        self._input.setFixedHeight(42)
        self._input.setStyleSheet(f"""
            QLineEdit {{
                background: {C.ELEV1}; color: {C.WHITE};
                border: 1px solid {C.HAIRLINE}; border-radius: 10px;
                padding: 0 15px;
            }}
            QLineEdit:hover {{ border-color: {C.HAIRLINE_S}; }}
            QLineEdit:focus {{ border-color: {C.PRI}; background: {C.ELEV2}; }}
        """)
        self._input.returnPressed.connect(self._send)
        row.addWidget(self._input, stretch=1)

        # Bouton ⏹ Interrompre (masqué au repos, visible quand l'IA parle/réfléchit)
        self._interrupt_btn = QPushButton()
        self._interrupt_btn.setIcon(make_svg_icon("square", C.MUTED_C, 18))
        self._interrupt_btn.setIconSize(QSize(18, 18))
        self._interrupt_btn.setFixedSize(42, 42)
        self._interrupt_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._interrupt_btn.setToolTip("Interrompre (Échap)")
        self._interrupt_btn.setStyleSheet(f"""
            QPushButton {{
                background: rgba(255, 51, 102, 0.15); color: {C.MUTED_C};
                border: 1px solid rgba(255, 51, 102, 0.50); border-radius: 10px; padding: 0;
            }}
            QPushButton:hover {{
                background: rgba(255, 51, 102, 0.30); color: #ffffff;
                border-color: {C.RED};
            }}
            QPushButton:pressed {{ background: rgba(255, 51, 102, 0.40); }}
        """)
        self._interrupt_btn.clicked.connect(self._do_interrupt)
        self._interrupt_btn.hide()
        row.addWidget(self._interrupt_btn)

        # Bouton 🎤 Micro actif/coupé
        self._mute_btn = QPushButton()
        self._mute_btn.setFixedSize(42, 42)
        self._mute_btn.setIconSize(QSize(20, 20))
        self._mute_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mute_btn.clicked.connect(self._toggle_mute)
        self._style_mute_btn()
        row.addWidget(self._mute_btn)

        # Bouton ➜ Envoyer
        send = QPushButton()
        send.setIcon(make_svg_icon("send", C.PRI, 20))
        send.setIconSize(QSize(20, 20))
        send.setFixedSize(42, 42)
        send.setCursor(Qt.CursorShape.PointingHandCursor)
        send.setToolTip("Envoyer (Entrée)")
        send.setStyleSheet(f"""
            QPushButton {{
                background: {C.PRI_GHO}; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 10px; padding: 0;
            }}
            QPushButton:hover {{ background: {C.PRI}; color: {C.DARK};
                                 border-color: {C.PRI}; }}
            QPushButton:pressed {{ background: {C.PRI_DIM}; }}
        """)
        send.clicked.connect(self._send)
        row.addWidget(send)

        wrapper.addLayout(row)
        return wrapper

    def _build_content_panel(self) -> FloatingPanel:
        panel = FloatingPanel("Briefing", closeable=True, parent=self.centralWidget())
        panel.hide()

        self._content_ts_lbl = QLabel("")
        self._content_ts_lbl.setFont(QFont("Inter", 7))
        self._content_ts_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        panel.add_widget(self._content_ts_lbl)

        self._content_display = QTextEdit()
        self._content_display.setReadOnly(True)
        self._content_display.setFont(QFont("Inter", 10))
        self._content_display.setMinimumHeight(80)
        self._content_display.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._content_display.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._content_display.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self._content_display.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._content_display.setAccessibleName("Contenu du briefing")
        self._content_display.setStyleSheet(f"""
            QTextEdit {{
                background: rgba(8, 16, 26, 0.55);
                color: {C.TEXT};
                border: 1px solid rgba(0, 212, 255, 0.16);
                border-left: 2px solid rgba(0, 212, 255, 0.45);
                border-radius: 4px;
                padding: 12px 14px;
                selection-background-color: {C.PRI_GHO};
            }}
            QScrollBar:vertical {{
                background: transparent; width: 10px; border: none; margin: 4px 1px;
            }}
            QScrollBar::handle:vertical {{
                background: {C.TEXT_DIM}; border-radius: 4px; min-height: 32px;
            }}
            QScrollBar::handle:vertical:hover {{ background: {C.PRI}; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0; border: none;
            }}
        """)
        panel.add_widget(self._content_display, stretch=1)
        return panel

    def _show_content(self, title: str, text: str):
        import time as _time
        if hasattr(self._content_panel, "_title_lbl"):
            self._content_panel._title_lbl.setText(title.upper()[:48])
        self._content_ts_lbl.setText(_time.strftime("%H:%M:%S"))
        self._content_display.setPlainText(text)
        self._content_display.moveCursor(
            self._content_display.textCursor().MoveOperation.Start
        )
        cw = self.centralWidget()
        pw = min(480, cw.width() - 32)
        ph = min(360, max(180, cw.height() - 260))
        self._content_panel.setGeometry(cw.width() - pw - 16, cw.height() - ph - 96, pw, ph)
        self._content_panel.fade_in()
        self._relayout()



    def _browse_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Sélectionner un fichier pour ANO-GPT", str(Path.home()),
            "Tous les fichiers (*.*);;"
            "Images (*.jpg *.jpeg *.png *.gif *.webp *.bmp *.svg);;"
            "Documents (*.pdf *.docx *.txt *.md *.pptx);;"
            "Données (*.csv *.xlsx *.json *.xml);;"
            "Code (*.py *.js *.ts *.html *.css *.java *.cpp *.go);;"
            "Audio (*.mp3 *.wav *.ogg *.m4a *.aac *.flac);;"
            "Video (*.mp4 *.avi *.mov *.mkv *.wmv *.webm);;"
            "Archives (*.zip *.rar *.tar *.gz *.7z)",
        )
        if path:
            self._on_file_selected(path)

    def _clear_file(self):
        self._current_file = None
        if hasattr(self, "_file_chip"):
            self._file_chip.clear_file()
        self._log.append_log("FICHIER : retiré")

    def _on_file_selected(self, path: str):
        self._current_file = path
        if hasattr(self, "_file_chip"):
            self._file_chip.set_file(path)
        p    = Path(path)
        size = _fmt_size(p.stat().st_size)
        self._log.append_log(f"FICHIER : {p.name} ({size}) chargé")
        if self.on_text_command:
            msg = (
                f"[FILE_UPLOADED] path={path} | name={p.name} | "
                f"type={p.suffix.lstrip('.')} | size={size} | "
                f"Dis brièvement à l’utilisateur en français naturel que tu vois le fichier "
                f"'{p.name}' ({size}) et demande-lui ce qu’il veut en faire."
            )
            threading.Thread(target=self.on_text_command, args=(msg,), daemon=True).start()
