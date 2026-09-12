from __future__ import annotations

import os
import platform
import subprocess
import sys

from PyQt6.QtWidgets import QWidget

if platform.system() == "Windows":
    _WIN_HIDE: dict = {"creationflags": subprocess.CREATE_NO_WINDOW}
else:
    _WIN_HIDE: dict = {}

_OS = platform.system()

_GL_BASE = QWidget
if os.environ.get("ANOGPT_USE_OPENGL", "").strip().lower() in {
    "1", "true", "yes", "on",
}:
    try:
        from PyQt6.QtOpenGLWidgets import QOpenGLWidget
        _GL_BASE = QOpenGLWidget
    except ImportError:
        pass

try:
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    from PyQt6.QtWebEngineCore import QWebEngineSettings
    _WEBENGINE = True
except ImportError:
    QWebEngineView = None  # type: ignore[misc, assignment]
    QWebEngineSettings = None  # type: ignore[misc, assignment]
    _WEBENGINE = False

try:
    from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink
    _QTMULTIMEDIA = True
except ImportError:
    QAudioOutput = QMediaPlayer = QVideoSink = None  # type: ignore[misc, assignment]
    _QTMULTIMEDIA = False


def _ui_flag(name: str, default: bool) -> bool:
    mod = sys.modules.get("ui")
    if mod is None:
        return default
    return bool(getattr(mod, name, default))


def webengine_enabled() -> bool:
    return _ui_flag("_WEBENGINE", _WEBENGINE)


def qtmultimedia_enabled() -> bool:
    return _ui_flag("_QTMULTIMEDIA", _QTMULTIMEDIA)
