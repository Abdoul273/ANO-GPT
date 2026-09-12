"""Package UI d'ANO-GPT. `from ui import JarvisUI` reste le contrat public."""
from __future__ import annotations

from ui.core.fade_widget import FadeInWidget
from ui.core.hud_button import HudButton
from ui.core.hud_paint import Hud
from ui.core.metrics import _metrics, _nvml_gpu_windows, _SysMetrics
from ui.core.qtflags import (
    _GL_BASE, _OS, _QTMULTIMEDIA, _WEBENGINE, _WIN_HIDE,
    QAudioOutput, QMediaPlayer, QVideoSink, QWebEngineSettings, QWebEngineView,
)
from ui.core.speech_text import _advance_current_sentence
from ui.dialogs.ai_config import AIConfigOverlay, _KeyTestWorker
from ui.dialogs.audio import AudioSettingsOverlay
from ui.dialogs.customize import CustomizeOverlay, HueWheel
from ui.dialogs.memory import MemoryOverlay
from ui.dialogs.plugins import PluginOverlay
from ui.dialogs.remote import RemoteKeyOverlay
from ui.dialogs.setup import SetupOverlay
from ui.jarvis_ui import JarvisUI, _RootShim
from ui.main_window import MainWindow
from ui.media.camera import _CameraPreview
from ui.media.gallery import ImageGalleryOverlay, _GalleryImageView
from ui.media.map_views import NearbyMapPanel
from ui.media.video_hub import VideoHubOverlay
from ui.media.video_widgets import VideoResultCard, _VideoFrameCanvas
from ui.orb.arc_core import HudCanvas
from ui.orb.companion import CompanionOrb
from ui.orb.glsl_orb import GLSLOrbWidget, create_hud_orb
from ui.orb.mini_orb import MiniOrbOverlay
from ui.orb.radial_waveform import (
    CircularFFTEngine,
    RadialWaveformRenderer,
    RadialWaveformWidget,
)
from ui.panels.cards_stack import RichCardWidget, RightCardStack, _HexGlyph
from ui.panels.rich_card_system import (
    GlassCard, MediaCard, WeatherCard, TelemetryCard, PlanCard, CardManager,
)
from ui.panels.clipboard import ClipboardPanel
from ui.panels.drop import FileDropZone, GlobalDropOverlay, _DropCanvas
from ui.panels.file_chip import FileChipWidget, _file_category, _fmt_size
from ui.panels.floating_panel import FloatingPanel
from ui.panels.interface_frame import InterfaceFrame
from ui.panels.log_widget import LogWidget
from ui.panels.music_player import MusicPlayerPanel
from ui.panels.music_widgets import _CoverArt, _Marquee, _NeonSeek, _SeekSlider, _Spectrum
from ui.panels.speech_overlay import CenterSpeechOverlay
from ui.panels.status_pill import _StatusPill
from ui.panels.telemetry import LiveTranscriptWidget, MetricBar
from ui.paths import (
    API_FILE, BASE_DIR, CONFIG_DIR, _DEFAULT_H, _DEFAULT_W, _LEFT_W, _MIN_H, _MIN_W,
    _RIGHT_W, _base_dir, _read_full_config, _write_full_config,
)
from ui.styles.qss import get_global_style
from ui.styles.theme import (
    C, DEFAULT_UI_COLOR, SVG_LUCIDE, apply_ui_accent, current_palette, hairline,
    load_custom_font, make_svg_icon, qcol, retheme_all_widgets, section_label,
)

from ui.visual_pointer import (
    DrawPathItem, HighlightRegionItem, LaserPointItem, VisualPointerOverlay,
    get_visual_pointer,
)

__all__ = [
    "AIConfigOverlay", "API_FILE", "AudioSettingsOverlay", "BASE_DIR", "C",
    "CONFIG_DIR", "CenterSpeechOverlay", "ClipboardPanel", "CompanionOrb",
    "CustomizeOverlay", "DEFAULT_UI_COLOR", "DrawPathItem", "FadeInWidget",
    "CardManager", "GlassCard", "MediaCard", "PlanCard", "TelemetryCard", "WeatherCard",
    "FileChipWidget", "FileDropZone", "FloatingPanel", "GlobalDropOverlay",
    "GLSLOrbWidget", "HighlightRegionItem", "Hud", "HudButton", "HudCanvas", "HueWheel",
    "create_hud_orb",
    "ImageGalleryOverlay", "InterfaceFrame", "JarvisUI", "LaserPointItem",
    "LiveTranscriptWidget", "LogWidget", "MainWindow", "MemoryOverlay",
    "MetricBar", "MiniOrbOverlay", "MusicPlayerPanel", "NearbyMapPanel",
    "PluginOverlay", "RadialWaveformRenderer", "RadialWaveformWidget", "RemoteKeyOverlay", "RichCardWidget", "RightCardStack",
    "SetupOverlay", "VideoHubOverlay", "VideoResultCard", "VisualPointerOverlay",
    "_CameraPreview", "_CoverArt", "_GL_BASE", "_Marquee", "_OS", "_QTMULTIMEDIA",
    "_RootShim", "_Spectrum", "_StatusPill", "_WEBENGINE", "_metrics",
    "apply_ui_accent", "create_hud_orb", "get_global_style", "get_visual_pointer", "qcol",
]
