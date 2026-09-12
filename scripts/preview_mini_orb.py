"""Native compositor regression check; no microphone, model or ANO-GPT services.

QT_QPA_PLATFORM=xcb python3 scripts/preview_mini_orb.py --output /tmp/ano-orb-check
Shows a checkerboard behind the real companion, captures it with grim and
checks that transparent pixels survive the compositor (QWidget.grab cannot).
"""
from __future__ import annotations

import argparse
import ctypes
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault('QT_QPA_PLATFORM', 'xcb')

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QImage, QPainter
from PyQt6.QtWidgets import QApplication, QWidget

from ui.orb.arc_core import HudCanvas
from ui.orb.companion import CompanionOrb
from ui.styles.qss import get_global_style


class Source:
    _PALETTES = HudCanvas._PALETTES
    _ws = 'idle'
    _volume = .15
    _energy = .6


class Backdrop(QWidget):
    def __init__(self):
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setWindowTitle('ANO reactor transparency check')
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

    def paintEvent(self, event):
        p = QPainter(self)
        for y in range(0, self.height(), 16):
            for x in range(0, self.width(), 16):
                p.fillRect(x, y, 16, 16, QColor('#718698' if (x // 16 + y // 16) % 2 else '#263e57'))
        p.end()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('/tmp/ano-orb-check'))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    app = QApplication(['jarvis-dashboard'])
    if app.platformName() != 'xcb':
        parser.error('This regression check targets the XWayland backend used by main.py.')
    app.setStyle('Fusion')
    app.setStyleSheet(get_global_style())
    source = Source()
    orb = CompanionOrb(source)
    backdrop = Backdrop()
    failures = []
    baseline = None
    geometry = None

    def capture(name):
        path = args.output / f'{name}.png'
        subprocess.run(['grim', '-g', geometry, str(path)], check=True,
                   capture_output=True, timeout=15)
        return QImage(str(path))

    def prepare():
        nonlocal geometry
        r = orb.geometry()
        geometry = f'{r.x()},{r.y()} {r.width()}x{r.height()}'
        backdrop.setGeometry(r)
        backdrop.show()
        orb.hide()

    def reference():
        nonlocal baseline
        baseline = capture('background')
        orb.show()
        orb.raise_()

    def verify(name, bubble=False):
        actual = capture(name)
        points = [(orb.BUBBLE_W + 4, 60), (orb.FULL_W - 44, 16), (orb.FULL_W - 1, 1)]
        if not bubble:
            points += [(10, 60), (125, 60), (245, 60)]
        for x, y in points:
            a, b = actual.pixelColor(x, y), baseline.pixelColor(x, y)
            if max(abs(a.red()-b.red()), abs(a.green()-b.green()), abs(a.blue()-b.blue())) > 3:
                failures.append(f'{name}: background changed at {x},{y}: {a.name()} != {b.name()}')
        if actual.pixelColor(orb._orb_rect.center()) == baseline.pixelColor(orb._orb_rect.center()):
            failures.append(f'{name}: reactor is missing')
        verify_input(bubble)
        # The X11 visual bounding shape must remain rectangular; only input is shaped.
        from Xlib.display import Display
        display = Display()
        try:
            window = display.create_resource_object('window', int(orb.winId()))
            if window.shape_query_extents().bounding_shaped:
                failures.append(f'{name}: XShapeBounding reintroduced')
        finally:
            display.close()

    def verify_input(bubble):
        from ui.orb.input_shape import _libraries, _Rectangle
        x11, ext = _libraries()
        ext.XShapeGetRectangles.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int,
                                           ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
        ext.XShapeGetRectangles.restype = ctypes.POINTER(_Rectangle)
        x11.XFree.argtypes = [ctypes.c_void_p]
        display = x11.XOpenDisplay(None)
        if not display:
            raise RuntimeError('Cannot inspect native input shape')
        count, ordering = ctypes.c_int(), ctypes.c_int()
        data = ext.XShapeGetRectangles(display, int(orb.winId()), 2,
                                       ctypes.byref(count), ctypes.byref(ordering))
        try:
            rectangles = [(data[i].x, data[i].y, data[i].width, data[i].height) for i in range(count.value)]
            scale = orb.devicePixelRatioF()
            for px, py, expected in ((304, 60, True), (254, 60, False), (10, 60, bubble)):
                x, y = round(px * scale), round(py * scale)
                hit = any(rx <= x < rx + w and ry <= y < ry + h for rx, ry, w, h in rectangles)
                if hit != expected:
                    failures.append(f'Input region at {px},{py}: {hit} != {expected}')
        finally:
            x11.XFree(data)
            x11.XCloseDisplay(display)

    def speaking():
        source._ws, source._volume, source._energy = 'speaking', .8, 1.

    def finish():
        orb.close()
        backdrop.close()
        print('\n'.join(failures) if failures else f'PASS: native transparency, speaking, bubble and clearing. Captures: {args.output}')
        app.exit(bool(failures))

    def guard(callback):
        def run():
            try:
                callback()
            except Exception as error:
                failures.append(f'{type(error).__name__}: {error}')
                finish()
        return run

    orb.show()
    for delay, callback in (
        (500, prepare), (1000, reference), (1600, lambda: verify('idle')),
        (1800, speaking), (2300, lambda: verify('speaking')),
        (2500, lambda: orb.say('ANO-GPT · Réacteur en ligne')),
        (3100, lambda: verify('bubble', True)), (3300, orb._clear_bubble),
        (3900, lambda: verify('cleared')), (4100, finish),
    ):
        QTimer.singleShot(delay, guard(callback))
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
