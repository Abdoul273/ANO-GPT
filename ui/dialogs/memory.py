from __future__ import annotations



from PyQt6.QtCore import (
    Qt,
)
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from ui.core.fade_widget import FadeInWidget
from ui.styles.cyber import CyberHeader, cyber_section
from ui.styles.theme import C

class MemoryOverlay(FadeInWidget):
    """Affiche la mémoire SQLite autoritaire et permet d'oublier une entrée."""
    _OW, _OH = 560, 590

    def __init__(self, parent=None):
        super().__init__(parent, duration=260)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 15, 18, 15)
        outer.setSpacing(8)
        header = CyberHeader("Mémoire d’ANO-GPT", "SOUVENIRS", parent=self)
        header.close_clicked.connect(self.hide)
        outer.addWidget(header)
        self._summary = QLabel()
        self._summary.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        outer.addWidget(self._summary)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        outer.addWidget(self._scroll, stretch=1)
        self.refresh()

    def refresh(self) -> None:
        from core.memory_store import list_memories
        rows = list_memories(300)
        self._summary.setText(f"{len(rows)} souvenir(s) récent(s) — stockage local SQLite")
        content = QWidget()
        content.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(7)
        if not rows:
            empty = QLabel("Aucun souvenir enregistré.")
            empty.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            layout.addWidget(empty)
        for row in rows:
            frame = cyber_section()
            line = QHBoxLayout(frame)
            text = QLabel(
                f"{row.get('kind', '').upper()} · {row.get('key') or row.get('category') or 'souvenir'}\n"
                f"{row.get('value', '')}\n{row.get('updated', '')}"
            )
            text.setWordWrap(True)
            text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            text.setStyleSheet(f"color: {C.TEXT}; background: transparent; border: none;")
            line.addWidget(text, stretch=1)
            forget = QPushButton("Oublier")
            forget.setObjectName("CyberDanger")
            forget.setFixedWidth(72)
            forget.clicked.connect(lambda _, ident=row["id"]: self._forget(ident))
            line.addWidget(forget)
            layout.addWidget(frame)
        layout.addStretch()
        self._scroll.setWidget(content)

    def _forget(self, memory_id: int) -> None:
        from core.memory_store import forget_id
        forget_id(memory_id)
        self.refresh()
