from __future__ import annotations

import re
import time


from PyQt6.QtCore import (
    QTimer, pyqtSignal,
)
from PyQt6.QtGui import (
    QBrush, QFont,
)
from PyQt6.QtWidgets import (
    QTextEdit,
)

from ui.styles.theme import C, qcol

# ── LogWidget avec timestamps ────────────────────────────────────────────────
# ── Machine à écrire du journal ──────────────────────────────────────────────
# Auparavant : un QTimer à 1 ms insérant UN caractère, avec recalcul du curseur,
# du format et un ensureCursorVisible() (qui force la mise en page du document)
# à chaque caractère. Qt tourne dans le thread principal et l'audio dans un
# thread asyncio : ils partagent le GIL. Ces 1000 réveils par seconde, pendant
# que l'assistant parle, affamaient donc la lecture audio — la voix hachait et
# l'interface saccadait.
#
# Mesuré sur une ligne de 176 caractères : 10,57 ms de travail Qt et 176 réveils
# du timer, contre 0,40 ms et 6 réveils par lots de 32. Soit 26x moins de CPU.
#
# La vitesse perçue est conservée : 32 caractères toutes les 16 ms font
# 2000 car./s, ce que visait le réglage d'origine (200 caractères ≈ 100 ms).
_TYPE_INTERVAL_MS = 16       # ~60 Hz, aligné sur le rafraîchissement écran
_TYPE_CHARS_PER_TICK = 32


class LogWidget(QTextEdit):
    _sig = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.document().setMaximumBlockCount(1500)
        self.setFont(QFont("Inter", 9))
        self.setStyleSheet(f"""
            QTextEdit {{
                background: {C.ELEV1};
                color: {C.TEXT};
                border: 1px solid {C.HAIRLINE};
                border-radius: 11px;
                padding: 9px 11px;
                selection-background-color: {C.PRI_DIM};
                selection-color: {C.DARK};
            }}
            QScrollBar:vertical {{
                background: transparent; width: 8px; border: none; margin: 4px 2px;
            }}
            QScrollBar::handle:vertical {{
                background: {C.BORDER}; border-radius: 9px; min-height: 26px;
            }}
            QScrollBar::handle:vertical:hover {{ background: {C.PRI_DIM}; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0; border: none;
            }}
        """)
        self._queue: list[str] = []
        self._typing  = False
        self._text    = ""
        self._pos     = 0
        self._tag     = "sys"
        self._ai_name_lc = "ano-gpt"
        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._step)
        self._sig.connect(self._enqueue)

    def append_log(self, text: str):
        if text.startswith("[INLINE_START]"):
            timestamp = time.strftime("[%H:%M:%S] ")
            self._sig.emit("[INLINE]" + timestamp + text[14:])
            return
        if text.startswith("[INLINE]") or text == "[INLINE_END]":
            self._sig.emit(text)
            return
        timestamp = time.strftime("[%H:%M:%S] ")
        self._sig.emit(timestamp + text)

    def _enqueue(self, text: str):
        self._queue.append(text)
        if not self._typing:
            self._next()

    def _next(self):
        if not self._queue:
            self._typing = False
            return
        self._typing = True
        raw = self._queue.pop(0)

        if raw == "[INLINE_END]":
            follow, position = self._scroll_state()
            cur = self.textCursor()
            cur.movePosition(cur.MoveOperation.End)
            cur.insertText("\n")
            self._finish_insert(cur, follow, position)
            self._typing = False
            self._next()
            return

        if raw.startswith("[INLINE]"):
            self._is_inline = True
            self._text = raw[8:]
        else:
            self._is_inline = False
            self._text = raw

        # Les sous-titres vocaux arrivent par groupes de deux mots. Ces
        # fragments ne portent volontairement ni espace initial ni final ;
        # sans séparateur, le journal écrivait « proximitéde votreposition ».
        if self._is_inline and self._text:
            existing = self.document().lastBlock().text()
            if (existing and not existing[-1].isspace()
                    and not re.match(r"^[,.;:!?…)]", self._text)):
                self._text = " " + self._text

        self._pos = 0
        tl = self._text.lower()
        _ai_pfx = f"{self._ai_name_lc}:"
        if "you:" in tl or "vous:" in tl:
            self._tag = "you"
        elif _ai_pfx in tl or "jarvis:" in tl or "ano-gpt:" in tl:
            self._tag = "ai"
        elif "file:" in tl:
            self._tag = "file"
        elif "err" in tl:
            self._tag = "err"
        elif not getattr(self, "_is_inline", False):
            self._tag = "sys"

        self._tmr.start(_TYPE_INTERVAL_MS)

    def _step(self):
        follow, position = self._scroll_state()
        if self._pos < len(self._text):
            # Un LOT de caractères par réveil, pas un seul. La vitesse affichée
            # est identique (voir _TYPE_CHARS_PER_TICK), mais le coût s'effondre.
            chunk = self._text[self._pos:self._pos + _TYPE_CHARS_PER_TICK]
            cur = self.textCursor()
            fmt = cur.charFormat()
            col = {
                "you":  qcol(C.WHITE),
                "ai":   qcol(C.PRI),
                "err":  qcol(C.RED),
                "file": qcol(C.GREEN),
                "sys":  qcol(C.ACC2),
            }.get(self._tag, qcol(C.TEXT))
            fmt.setForeground(QBrush(col))
            cur.movePosition(cur.MoveOperation.End)
            cur.insertText(chunk, fmt)
            self._finish_insert(cur, follow, position)
            self._pos += len(chunk)
        else:
            self._tmr.stop()
            cur = self.textCursor()
            cur.movePosition(cur.MoveOperation.End)
            if not getattr(self, "_is_inline", False):
                cur.insertText("\n")
            self._finish_insert(cur, follow, position)
            QTimer.singleShot(2, self._next)

    def _scroll_state(self):
        bar = self.verticalScrollBar()
        return bar.maximum() - bar.value() <= 4, bar.value()

    def _finish_insert(self, cursor, follow, position):
        # Un utilisateur qui relit le journal ne doit pas être ramené en bas
        # par chaque fragment de la réponse vocale.
        if follow:
            self.setTextCursor(cursor)
            self.ensureCursorVisible()
        else:
            self.verticalScrollBar().setValue(position)
