#!/usr/bin/env python3
"""Découpe ui.py en package ui/. Exécuter depuis la racine du projet."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LINES = (ROOT / "ui.py").read_text(encoding="utf-8").splitlines(keepends=True)


def sl(a: int, b: int) -> str:
    return "".join(LINES[a - 1 : b])


def write(rel: str, content: str) -> None:
    path = ROOT / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if not content.endswith("\n"):
        content += "\n"
    path.write_text(content, encoding="utf-8")
    n = content.count("\n")
    mark = "  ** OVER 450 **" if n > 450 else ""
    print(f"{n:4d}  {rel}{mark}")


F = "from __future__ import annotations\n\n"
STD = (
    "import json\nimport math\nimport os\nimport platform\nimport random\n"
    "import re\nimport subprocess\nimport sys\nimport threading\nimport time\n"
    "import traceback\nfrom pathlib import Path\n\nimport psutil\n\n"
)
QT = sl(23, 38)

# ── inits ──────────────────────────────────────────────────────────────────
for d in (
    "ui", "ui/styles", "ui/core", "ui/orb", "ui/panels", "ui/media",
    "ui/dialogs", "ui/window",
):
    (ROOT / d).mkdir(parents=True, exist_ok=True)

write("ui/styles/__init__.py", F)
write("ui/core/__init__.py", F)
write("ui/orb/__init__.py", F)
write("ui/panels/__init__.py", F)
write("ui/media/__init__.py", F)
write("ui/dialogs/__init__.py", F)
write("ui/window/__init__.py", F)

# ── paths ──────────────────────────────────────────────────────────────────
write("ui/paths.py", F + sl(3, 14) + """
from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import QApplication


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = _base_dir()
CONFIG_DIR = BASE_DIR / "config"
API_FILE = CONFIG_DIR / "api_keys.json"

_DEFAULT_W, _DEFAULT_H = 1200, 800
_MIN_W, _MIN_H = 820, 580
_LEFT_W = 184
_RIGHT_W = 352

_cache: dict = {}
_loaded = False
_lock = threading.Lock()
_write_worker: QThread | None = None


def _load_from_disk() -> dict:
    try:
        return json.loads(API_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _ensure_loaded() -> None:
    global _cache, _loaded
    with _lock:
        if _loaded:
            return
        _cache = _load_from_disk()
        _loaded = True


threading.Thread(target=_ensure_loaded, daemon=True, name="ui-config-load").start()


def _read_full_config() -> dict:
    \"\"\"Snapshot mémoire. Ne lit jamais le disque depuis le thread GUI Qt.\"\"\"
    if QApplication.instance() is None:
        _ensure_loaded()
    with _lock:
        return dict(_cache)


def _write_full_config(data: dict) -> None:
    \"\"\"Écrit api_keys.json hors du thread GUI si Qt tourne déjà.\"\"\"
    global _cache, _loaded, _write_worker
    payload = dict(data)
    with _lock:
        _cache = payload
        _loaded = True

    def _dump() -> None:
        try:
            API_FILE.parent.mkdir(parents=True, exist_ok=True)
            API_FILE.write_text(json.dumps(payload, indent=4), encoding="utf-8")
        except Exception:
            pass

    if QApplication.instance() is None:
        _dump()
        return

    class _Writer(QThread):
        def run(self):
            _dump()

    worker = _Writer()
    _write_worker = worker
    worker.start()
""")

write("ui/core/qtflags.py", F + """import os
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
""")

# ── theme / qss ────────────────────────────────────────────────────────────
write("ui/styles/theme.py", F + STD + QT + """
from ui.paths import BASE_DIR, CONFIG_DIR, API_FILE  # noqa: F401

""" + sl(105, 303) + sl(471, 487))

write("ui/styles/qss.py", F + "from ui.styles.theme import C\n\n" + sl(305, 468))

# ── hud ────────────────────────────────────────────────────────────────────
write("ui/core/hud_paint.py", F + """import math

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QBrush, QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap,
)

from ui.styles.theme import C, qcol

""" + sl(491, 704))

write("ui/core/hud_button.py", F + """from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import QBrush, QColor, QLinearGradient, QPainter, QPen
from PyQt6.QtWidgets import QPushButton

from ui.core.hud_paint import Hud
from ui.styles.theme import C, make_svg_icon, qcol

""" + sl(706, 857))

write("ui/core/fade_widget.py", F + """from PyQt6.QtCore import QEasingCurve, QPropertyAnimation
from PyQt6.QtWidgets import QGraphicsOpacityEffect, QWidget

""" + sl(887, 954))

write("ui/core/speech_text.py", F + "import re\n\n" + sl(860, 883))

write("ui/core/metrics.py", F + """import threading
import time

import psutil

from ui.core.qtflags import _OS

""" + sl(957, 1098))

# ── orb ────────────────────────────────────────────────────────────────────
# HudCanvas split: sprites mixin + paint mixin + core
write("ui/orb/arc_sprites.py", F + """import math

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QBrush, QColor, QConicalGradient, QPainter, QPen, QPixmap, QRadialGradient,
)

from ui.styles.theme import C


class _HudSpritesMixin:
""" + sl(1316, 1418).replace("    def _new_pm", "    def _new_pm").replace(
        "    def _build_cache", "    def _build_cache"
    ))

# The slice includes method bodies already indented at class level. Prefix class then methods.
# sl(1316) starts at "    def _new_pm" - good for mixin.

write("ui/orb/arc_paint.py", F + """import math
import time

from PyQt6.QtCore import QLineF, QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QBrush, QColor, QPainter, QPainterPath, QPen, QPolygonF, QRadialGradient,
)


class _HudPaintMixin:
""" + sl(1650, 1941))

write("ui/orb/arc_core.py", F + STD + QT + """
from ui.core.qtflags import _GL_BASE
from ui.orb.arc_paint import _HudPaintMixin
from ui.orb.arc_sprites import _HudSpritesMixin
from ui.styles.theme import C

""" + sl(1101, 1314).replace(
    "class HudCanvas(_GL_BASE):",
    "class HudCanvas(_HudPaintMixin, _HudSpritesMixin, _GL_BASE):",
) + sl(1419, 1648))

write("ui/orb/mini_orb.py", F + STD + QT + """
from ui.orb.arc_core import HudCanvas
from ui.styles.theme import C

""" + sl(1944, 2047))

write("ui/orb/companion.py", F + STD + QT + """
from ui.orb.arc_core import HudCanvas
from ui.orb.mini_orb import MiniOrbOverlay
from ui.styles.theme import C

""" + sl(2050, 2203))

# ── panels ─────────────────────────────────────────────────────────────────
write("ui/panels/interface_frame.py", F + STD + QT + """
from ui.styles.theme import C

""" + sl(2206, 2291))

write("ui/panels/telemetry.py", F + STD + QT + """
from ui.core.hud_paint import Hud
from ui.core.metrics import _metrics  # noqa: F401
from ui.styles.theme import C, qcol

""" + sl(2294, 2512))

write("ui/panels/status_pill.py", F + STD + QT + """
from ui.core.hud_paint import Hud
from ui.styles.theme import C, qcol

""" + sl(2515, 2590))

write("ui/panels/log_widget.py", F + STD + QT + """
from ui.styles.theme import C, qcol

""" + sl(2593, 2742))

write("ui/panels/file_chip.py", F + STD + QT + """
from ui.core.qtflags import _OS
from ui.styles.theme import C

""" + sl(2746, 2848))

write("ui/panels/floating_panel.py", F + STD + QT + """
from ui.core.hud_button import HudButton
from ui.core.hud_paint import Hud
from ui.styles.theme import C, qcol

""" + sl(2851, 3017))

write("ui/panels/cards_stack.py", F + STD + QT + """
from ui.core.hud_button import HudButton
from ui.core.hud_paint import Hud
from ui.styles.theme import C, make_svg_icon, qcol

""" + sl(3020, 3318) + "\n" + sl(3851, 3909))

write("ui/panels/music_widgets.py", F + STD + QT + """
from ui.core.hud_paint import Hud
from ui.styles.theme import C, make_svg_icon, qcol

""" + sl(3369, 3696))

write("ui/panels/music_player.py", F + STD + QT + """
from ui.core.hud_button import HudButton
from ui.core.hud_paint import Hud
from ui.panels.floating_panel import FloatingPanel
from ui.panels.music_widgets import _CoverArt, _Marquee, _NeonSeek, _Spectrum
from ui.styles.theme import C

""" + sl(3698, 3848))

write("ui/panels/speech_overlay.py", F + STD + QT + """
from ui.styles.theme import C

""" + sl(4883, 5091))

write("ui/panels/clipboard.py", F + STD + QT + """
from ui.core.hud_button import HudButton
from ui.core.hud_paint import Hud
from ui.styles.theme import C, qcol

""" + sl(6444, 6528))

write("ui/panels/drop.py", F + STD + QT + """
from ui.panels.file_chip import _FILE_ICONS, _file_category, _fmt_size
from ui.styles.theme import C, qcol

""" + sl(4834, 4880) + "\n" + sl(5094, 5286))

# ── media ──────────────────────────────────────────────────────────────────
write("ui/media/map_views.py", F + STD + QT + """
from ui.core.qtflags import QWebEngineView
from ui.panels.floating_panel import FloatingPanel
from ui.styles.theme import C

""" + sl(3321, 3366))

write("ui/media/gallery.py", F + STD + QT + """
from ui.core.hud_button import HudButton
from ui.core.hud_paint import Hud
from ui.styles.theme import C

""" + sl(3912, 4186))

write("ui/media/video_widgets.py", F + STD + QT + """
from ui.core.hud_paint import Hud
from ui.core.qtflags import QVideoSink, _QTMULTIMEDIA
from ui.styles.theme import C

""" + sl(4189, 4296))

_play = sl(4443, 4764)
_play = _play.replace("if not _QTMULTIMEDIA:", "if not qtmultimedia_enabled():")
_play = _play.replace("if not _WEBENGINE or", "if not webengine_enabled() or")
_play = _play.replace("if (_QTMULTIMEDIA and", "if (qtmultimedia_enabled() and")
write("ui/media/video_playback.py", F + STD + QT + """
from ui.core.qtflags import (
    QAudioOutput, QMediaPlayer, QWebEngineSettings, QWebEngineView,
    qtmultimedia_enabled, webengine_enabled,
)
from ui.media.video_widgets import _VideoFrameCanvas
from ui.styles.theme import C

class _VideoPlaybackMixin:
""" + _play)

write("ui/media/video_hub.py", F + STD + QT + """
from ui.core.hud_button import HudButton
from ui.core.hud_paint import Hud
from ui.core.qtflags import QWebEngineView, QAudioOutput, QMediaPlayer, _QTMULTIMEDIA, _WEBENGINE
from ui.media.video_playback import _VideoPlaybackMixin
from ui.media.video_widgets import VideoResultCard, _VideoFrameCanvas
from ui.panels.music_widgets import _SeekSlider
from ui.styles.theme import C, qcol

""" + sl(4299, 4441).replace(
    "class VideoHubOverlay(QWidget):",
    "class VideoHubOverlay(_VideoPlaybackMixin, QWidget):",
) + sl(4766, 4832))

write("ui/media/camera.py", F + STD + QT + """
from ui.styles.theme import C

""" + sl(5290, 5346))

# ── dialogs ────────────────────────────────────────────────────────────────
write("ui/dialogs/setup.py", F + STD + QT + """
from ui.core.fade_widget import FadeInWidget
from ui.core.qtflags import _OS
from ui.styles.theme import C

""" + sl(5350, 5474))

write("ui/dialogs/customize.py", F + STD + QT + """
from ui.core.fade_widget import FadeInWidget
from ui.styles.theme import C, DEFAULT_UI_COLOR, qcol

""" + sl(5478, 5706))

write("ui/dialogs/ai_config.py", F + STD + QT + """
from ui.core.fade_widget import FadeInWidget
from ui.paths import _read_full_config
from ui.styles.theme import C

""" + sl(5710, 6031))

write("ui/dialogs/plugins.py", F + STD + QT + """
from ui.core.fade_widget import FadeInWidget
from ui.styles.theme import C

""" + sl(6035, 6092))

write("ui/dialogs/memory.py", F + STD + QT + """
from ui.core.fade_widget import FadeInWidget
from ui.styles.theme import C

""" + sl(6096, 6160))

write("ui/dialogs/audio.py", F + STD + QT + """
from core.live_speech_config import DEFAULT_LIVE_VOICE, LIVE_VOICE_OPTIONS, normalise_live_voice

from ui.core.fade_widget import FadeInWidget
from ui.paths import _read_full_config
from ui.styles.theme import C

""" + sl(6164, 6439))

write("ui/dialogs/remote.py", F + STD + QT + """
from ui.core.fade_widget import FadeInWidget
from ui.styles.theme import C

""" + sl(6532, 6745))

print("\nbase modules written")
