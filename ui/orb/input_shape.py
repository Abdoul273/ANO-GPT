"""X11 input-only shape, deliberately leaving the ARGB bounding surface intact."""
from __future__ import annotations

import ctypes
from functools import lru_cache


class _Rectangle(ctypes.Structure):
    _fields_ = [('x', ctypes.c_short), ('y', ctypes.c_short),
                ('width', ctypes.c_ushort), ('height', ctypes.c_ushort)]


@lru_cache(maxsize=1)
def _libraries():
    x11 = ctypes.CDLL('libX11.so.6')
    ext = ctypes.CDLL('libXext.so.6')
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
    ext.XShapeCombineRectangles.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.POINTER(_Rectangle), ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ]
    ext.XShapeCombineRectangles.restype = None
    return x11, ext


def set_x11_input_shape(window_id: int, rectangles: list[tuple[int, int, int, int]]) -> None:
    """SHAPE 1.1: ShapeInput=2, ShapeSet=0, Unsorted=0. Never ShapeBounding."""
    x11, ext = _libraries()
    display = x11.XOpenDisplay(None)
    if not display:
        raise RuntimeError('Cannot open X11 display for the companion input region')
    try:
        data = (_Rectangle * len(rectangles))(*(_Rectangle(*rect) for rect in rectangles))
        ext.XShapeCombineRectangles(display, window_id, 2, 0, 0, data, len(data), 0, 0)
        x11.XSync(display, 0)
    finally:
        x11.XCloseDisplay(display)
