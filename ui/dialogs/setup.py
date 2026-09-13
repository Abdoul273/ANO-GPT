from __future__ import annotations



from PyQt6.QtCore import (
    Qt,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QFont,
)
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout,
)

from ui.core.fade_widget import FadeInWidget
from ui.core.qtflags import _OS
from ui.styles.cyber import CyberHeader, micro_label
from ui.styles.theme import C

class SetupOverlay(FadeInWidget):
    done = pyqtSignal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent, duration=300)
        detected = {"darwin": "mac", "windows": "windows"}.get(
            _OS.lower(), "linux"
        )
        self._sel_os = detected

        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 22, 30, 22)
        layout.setSpacing(8)

        def _lbl(txt, font_size=9, bold=False, color=C.PRI,
                 align=Qt.AlignmentFlag.AlignCenter):
            w = QLabel(txt)
            w.setAlignment(align)
            w.setFont(QFont("Inter", font_size,
                            QFont.Weight.Bold if bold else QFont.Weight.Normal))
            w.setStyleSheet(f"color: {color}; background: transparent;")
            return w

        header = CyberHeader("Initialisation requise", "PREMIER DÉMARRAGE", parent=self)
        header.close_clicked.connect(self.hide)
        layout.addWidget(header)
        layout.addWidget(_lbl("Configurez ANO-GPT avant le premier démarrage.", 9, color=C.PRI_DIM))
        layout.addSpacing(6)
        layout.addWidget(micro_label("Clé API Gemini"))
        self._key_input = QLineEdit()
        self._key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_input.setPlaceholderText("AIza…")
        self._key_input.setFont(QFont("Inter", 10))
        self._key_input.setFixedHeight(32)
        layout.addWidget(self._key_input)
        layout.addSpacing(12)
        layout.addWidget(micro_label("Système d'exploitation"))
        det_name = {"windows": "Windows", "mac": "macOS", "linux": "Linux"}[detected]
        layout.addWidget(_lbl(f"Détecté automatiquement : {det_name}", 8, color=C.ACC2,
                               align=Qt.AlignmentFlag.AlignLeft))
        os_row = QHBoxLayout(); os_row.setSpacing(6)
        self._os_btns: dict[str, QPushButton] = {}
        for key, label in [("windows","⊞  Windows"),("mac","  macOS"),("linux","🐧  Linux")]:
            btn = QPushButton(label)
            btn.setFont(QFont("Inter", 9, QFont.Weight.Bold))
            btn.setFixedHeight(32)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _, k=key: self._sel(k))
            os_row.addWidget(btn)
            self._os_btns[key] = btn
        layout.addLayout(os_row)
        self._sel(detected)
        layout.addSpacing(12)
        init_btn = QPushButton("▸  INITIALISER")
        init_btn.setFont(QFont("Inter", 10, QFont.Weight.Bold))
        init_btn.setFixedHeight(36)
        init_btn.setObjectName("CyberPrimary")
        init_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        init_btn.clicked.connect(self._submit)
        layout.addWidget(init_btn)

    def _sel(self, key: str):
        self._sel_os = key
        # Sélection : teinte + liseré de la couleur, pas d'aplat saturé
        pal = {"windows": (C.PRI, "0, 212, 255"),
               "mac":     (C.ACC2, "255, 204, 0"),
               "linux":   (C.GREEN, "0, 255, 136")}
        for k, btn in self._os_btns.items():
            if k == key:
                fg, rgb = pal[k]
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: rgba({rgb}, 0.16); color: {fg};
                        border: 1px solid {fg}; border-left: 2px solid {fg};
                        border-radius: 4px; font-weight: bold;
                    }}
                """)
            else:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: rgba(14, 23, 36, 0.92); color: {C.TEXT_DIM};
                        border: 1px solid rgba(0, 212, 255, 0.30); border-radius: 4px;
                    }}
                    QPushButton:hover {{ color: {C.WHITE};
                        background: rgba(0, 212, 255, 0.12);
                        border-color: {C.PRI}; }}
                """)

    def _submit(self):
        key = self._key_input.text().strip()
        if not key:
            self._key_input.setStyleSheet(
                f"QLineEdit {{ border: 1px solid {C.RED};"
                f" border-bottom: 2px solid {C.RED}; }}")
            return
        self.done.emit(key, self._sel_os)
