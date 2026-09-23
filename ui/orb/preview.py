"""Banc d'essai d'un style d'orbe, hors de l'application.

    python -m ui.orb.preview [style] [--all]

Touches : 1-6 états · espace : rafale de voix · Tab : style suivant ·
M : muet · L : veille. ``--all`` inclut les styles en chantier (ready=False).
"""
from __future__ import annotations

import math
import sys
import time
from dataclasses import replace

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import QApplication, QMainWindow

from ui.orb import registry
from ui.orb.host import OrbHost

_STATES = {
    Qt.Key.Key_1: "IDLE", Qt.Key.Key_2: "LISTENING", Qt.Key.Key_3: "THINKING",
    Qt.Key.Key_4: "SPEAKING", Qt.Key.Key_5: "ACTING", Qt.Key.Key_6: "ERROR",
}


def main(argv: list[str]) -> int:
    include_all = "--all" in argv
    args = [a for a in argv if not a.startswith("--")]
    if include_all:
        for spec in registry.all_specs():
            registry.register(replace(spec, ready=True))
    app = QApplication(sys.argv)
    win = QMainWindow()
    win.setStyleSheet("QMainWindow { background: #05080d; }")
    host = OrbHost("", "ANO-GPT", args[0] if args else None)
    win.setCentralWidget(host)
    win.resize(760, 760)

    def title() -> None:
        win.setWindowTitle(f"Orbe — {host.style_id}  ·  {host.state}")

    burst_until = [0.0]

    def feed() -> None:
        now = time.monotonic()
        if now < burst_until[0]:
            host.set_volume(0.35 + 0.6 * abs(math.sin(now * 9.0)))

    timer = QTimer(win)
    timer.timeout.connect(feed)
    timer.start(40)

    def on_key(event) -> None:
        key = event.key()
        if key in _STATES:
            host.state = _STATES[key]
            host.speaking = _STATES[key] == "SPEAKING"
        elif key == Qt.Key.Key_Space:
            burst_until[0] = time.monotonic() + 2.0
        elif key == Qt.Key.Key_Tab:
            ids = [s.id for s in registry.selectable_specs()]
            if host.style_id in ids:
                start = ids.index(host.style_id)
                for step in range(1, len(ids) + 1):
                    if host.set_style(ids[(start + step) % len(ids)]):
                        break
        elif key == Qt.Key.Key_M:
            host.muted = not host.muted
        elif key == Qt.Key.Key_L:
            host.set_low_power(not host._snapshot.low_power)
        title()

    win.keyPressEvent = on_key  # type: ignore[method-assign]
    title()
    win.show()
    print(__doc__)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
