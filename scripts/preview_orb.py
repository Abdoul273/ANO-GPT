"""Prévisualisation et mesure de l'orbe ARC CORE, sans micro ni modèle.

    python3 scripts/preview_orb.py                 # fenêtre interactive
    python3 scripts/preview_orb.py --capture DIR   # une image par état + coût

Touches : 1 idle · 2 listening · 3 thinking · 4 speaking · 5 acting · 6 error
          espace : impulsion FFT · h : horloge · v : œil vision · g : geste
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import QApplication, QMainWindow

from ui.orb.arc_core import HudCanvas

STATES = ("IDLE", "LISTENING", "THINKING", "SPEAKING", "ACTING", "ERROR")


def _drive_audio(orb: HudCanvas, t: float) -> None:
    """Voix synthétique : 8 bandes FFT plausibles, syllabes et respirations."""
    syllable = max(0.0, math.sin(t * 6.3)) ** 1.4 * (0.6 + 0.4 * math.sin(t * 1.7))
    bands = [
        max(0.0, min(1.0, syllable * (1.0 - i * 0.09) * (0.55 + 0.45 * math.sin(t * (2.4 + i * 0.47) + i))))
        for i in range(8)
    ]
    orb.set_audio_bands(bands)
    orb.set_volume(0.7 * max(bands) + 0.3 * sum(bands) / 8.0)


def capture(out_dir: Path) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    out_dir.mkdir(parents=True, exist_ok=True)
    orb = HudCanvas("")
    orb.resize(900, 700)
    orb.show()
    app.processEvents()
    report = []
    for state in STATES:
        orb.state = state
        orb.speaking = state == "SPEAKING"
        t0 = time.monotonic()
        sims, paints = [], []
        # Deux secondes de simulation à 30 Hz : la palette a le temps de
        # glisser et la voix synthétique de faire respirer le nuage.
        for frame in range(60):
            t = frame / 30.0
            if state in ("SPEAKING", "LISTENING", "ACTING"):
                _drive_audio(orb, t)
            orb._last_tick = time.monotonic() - 1 / 30.0
            s = time.monotonic()
            orb._tick_frame()
            sims.append((time.monotonic() - s) * 1000.0)
            s = time.monotonic()
            orb.repaint()
            paints.append((time.monotonic() - s) * 1000.0)
            app.processEvents()
        image = orb.grab().toImage()
        path = out_dir / f"orb_{state.lower()}.png"
        image.save(str(path))
        sims.sort()
        paints.sort()
        report.append(
            f"{state:<10} sim médiane {sims[len(sims)//2]:5.2f} ms  max {sims[-1]:5.2f} | "
            f"paint médiane {paints[len(paints)//2]:5.2f} ms  max {paints[-1]:5.2f} | "
            f"cadence choisie {orb._desired_interval()} ms  ({(time.monotonic()-t0):.1f}s)"
        )
    orb.state = "IDLE"
    orb.speaking = False
    orb.show_clock_particles(duration=6)
    # La convergence dure 1,05 s en temps réel : on laisse le cadran se former.
    until = time.monotonic() + 1.6
    while time.monotonic() < until:
        orb._tick_frame()
        orb.repaint()
        app.processEvents()
        time.sleep(1 / 30.0)
    orb.grab().toImage().save(str(out_dir / "orb_clock.png"))
    orb.close()
    print("\n".join(report))
    return 0


def interactive() -> int:
    app = QApplication(sys.argv)
    win = QMainWindow()
    win.setWindowTitle("ANO-GPT — orbe ARC CORE")
    orb = HudCanvas("")
    win.setCentralWidget(orb)
    win.resize(900, 720)
    win.show()
    started = time.monotonic()
    drive = {"on": False}

    def feed():
        if drive["on"]:
            _drive_audio(orb, time.monotonic() - started)

    feeder = QTimer(win)
    feeder.timeout.connect(feed)
    feeder.start(40)

    def on_key(event):
        key = event.key()
        keys = {
            Qt.Key.Key_1: "IDLE", Qt.Key.Key_2: "LISTENING", Qt.Key.Key_3: "THINKING",
            Qt.Key.Key_4: "SPEAKING", Qt.Key.Key_5: "ACTING", Qt.Key.Key_6: "ERROR",
        }
        if key in keys:
            orb.speaking = keys[key] == "SPEAKING"
            orb.state = keys[key]
            drive["on"] = keys[key] in ("SPEAKING", "LISTENING", "ACTING")
        elif key == Qt.Key.Key_Space:
            orb.set_audio_bands([0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2])
            orb.set_volume(0.9)
        elif key == Qt.Key.Key_H:
            orb.show_clock_particles(duration=6)
        elif key == Qt.Key.Key_V:
            orb.set_continuous_vision(not orb.continuous_vision)
        elif key == Qt.Key.Key_G:
            orb.show_gesture_feedback("play_pause", "lecture", duration=2.0)

    win.keyPressEvent = on_key  # type: ignore[method-assign]
    print(__doc__)
    return app.exec()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--capture", metavar="DIR", help="capture une image par état puis quitte")
    args = parser.parse_args()
    if args.capture:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        raise SystemExit(capture(Path(args.capture)))
    raise SystemExit(interactive())
