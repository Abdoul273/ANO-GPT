from __future__ import annotations

import platform as _platform
import subprocess as _subprocess
import os as _os_early
from core.browser_policy import prefer_chrome
prefer_chrome()

# ── Fix Xlib.xauth warning sur Wayland (Hyprland) ────────────────────────────
# On ne pose un DISPLAY bidon QUE s'il n'y a ni X ni Wayland : cette valeur est
# héritée par tous les sous-processus (VLC, mpv, navigateurs…) et un ":99" qui
# ne pointe sur aucun serveur X empêche Qt de créer la moindre fenêtre — c'est
# ce qui rendait VLC invisible quand il était lancé depuis l'assistant.
if not _os_early.environ.get("DISPLAY") and not _os_early.environ.get("WAYLAND_DISPLAY"):
    _os_early.environ["DISPLAY"] = ":99"

# Qt 6.11.2/QtWayland segfault actuellement dans QBackingStore::endPaint sur
# cette machine lorsque le briefing et l'audio animent simultanément le HUD.
# XWayland affiche exactement la même fenêtre sous Hyprland et a tenu tous les
# tests réels. Garder un opt-out pour retester le backend natif après mise à
# jour de Qt, sans imposer ce contournement aux machines sans XWayland.
if (
    _os_early.environ.get("WAYLAND_DISPLAY")
    and _os_early.environ.get("DISPLAY")
    and _os_early.environ.get("ANOGPT_NATIVE_WAYLAND", "").strip().lower()
        not in {"1", "true", "yes", "on"}
):
    _os_early.environ["QT_QPA_PLATFORM"] = "xcb"

# ── Suppression des logs Qt/KDE parasites ─────────────────────────────────────
# NOTE: On ne touche PAS à FONTCONFIG_FILE pour ne pas casser le rendu des polices.
_os_early.environ["QT_LOGGING_RULES"] = (
    "kf.config.*=false;qt.qpa.*=false;*.debug=false"
)
# Rediriger stderr pour avaler le message Fontconfig (inoffensif)
import sys as _sys_early, io as _io_early
class _StderrFilter(_io_early.TextIOWrapper):
    _DROP = (b"Fontconfig error", b"Cannot load default config",
             b"Cannot load config file")
    def __init__(self):
        super().__init__(_io_early.FileIO(2, mode="wb", closefd=False),
                         line_buffering=True, encoding="utf-8", errors="replace")
    def write(self, s):
        sb = s.encode("utf-8", errors="replace") if s else b""
        if any(d in sb for d in self._DROP):
            return len(s)
        return super().write(s)
try:
    _sys_early.stderr = _StderrFilter()
except Exception:
    pass

# Les moteurs extraits accèdent aux dépendances différées via ``import main``.
# Quand ce fichier est lancé directement, son nom Python est ``__main__`` :
# sans cet alias, ``import main`` recharge une seconde copie du module dont
# ``genai``/``types`` restent à None, malgré la fin de l'import dans la copie
# réellement exécutée.
if __name__ == "__main__":
    _sys_early.modules.setdefault("main", _sys_early.modules[__name__])
# ─────────────────────────────────────────────────────────────────────────────


# ── Résilience aux crashs natifs du runtime ──────────────────────────────────
# PyQt, OpenSSL, ONNX Runtime et les pilotes audio sont du code natif : un
# SIGSEGV/SIGABRT contourne entièrement les try/except Python. Quand main.py est
# lancé directement, garder un minuscule parent sans Qt permet de faire revenir
# l'interface après ce type de crash. Une fermeture normale ou Ctrl+C ne
# redémarre jamais l'application, et la limite évite toute boucle infinie.
_SUPERVISOR_ENV = "ANOGPT_SUPERVISED_CHILD"
_INSTANCE_LOCK_HANDLE = None


def _notify_existing_instance(command: str = "show") -> bool:
    """Envoie une commande sans importer Qt ni le reste de l'application."""
    import socket as _socket_single

    runtime = _os_early.environ.get("XDG_RUNTIME_DIR")
    path = (
        _os_early.path.join(runtime, "anogpt.sock")
        if runtime and _os_early.path.isdir(runtime)
        else f"/tmp/anogpt-{_os_early.getuid()}.sock"
    )
    try:
        with _socket_single.socket(_socket_single.AF_UNIX, _socket_single.SOCK_STREAM) as sock:
            sock.settimeout(0.75)
            sock.connect(path)
            sock.sendall((command.strip() + "\n").encode("utf-8"))
            sock.recv(512)
        return True
    except OSError:
        return False


def _acquire_single_instance() -> bool:
    """Réserve l'unique superviseur avant toute création de fenêtre Qt."""
    global _INSTANCE_LOCK_HANDLE
    try:
        import fcntl as _fcntl_single

        runtime = _os_early.environ.get("XDG_RUNTIME_DIR")
        directory = runtime if runtime and _os_early.path.isdir(runtime) else "/tmp"
        path = _os_early.path.join(directory, f"anogpt-{_os_early.getuid()}.lock")
        handle = open(path, "a+", encoding="ascii")
        _fcntl_single.flock(handle.fileno(), _fcntl_single.LOCK_EX | _fcntl_single.LOCK_NB)
        _INSTANCE_LOCK_HANDLE = handle
        return True
    except (OSError, ImportError):
        _notify_existing_instance("show")
        print("[ANO-GPT] Une instance est déjà ouverte — fenêtre existante activée.")
        return False


def _supervise_native_process() -> None:
    import time as _time_supervisor

    child_env = dict(_os_early.environ)
    child_env[_SUPERVISOR_ENV] = "1"
    crash_times: list[float] = []
    native_crashes = {-6, -7, -11}  # SIGABRT, SIGBUS, SIGSEGV

    while True:
        try:
            completed = _subprocess.run(
                [_sys_early.executable, str(_os_early.path.abspath(__file__)),
                 *_sys_early.argv[1:]],
                env=child_env,
            )
        except KeyboardInterrupt:
            raise SystemExit(130)

        if completed.returncode not in native_crashes:
            raise SystemExit(completed.returncode)

        now = _time_supervisor.monotonic()
        crash_times = [stamp for stamp in crash_times if now - stamp < 300]
        crash_times.append(now)
        if len(crash_times) > 3:
            print(
                "[ANO-GPT] Trop de crashs natifs en 5 minutes ; redémarrage "
                "automatique suspendu. Utilisez Python 3.11 ou 3.12.",
                file=_sys_early.stderr,
            )
            raise SystemExit(completed.returncode)

        print(
            f"[ANO-GPT] Crash natif détecté (code {completed.returncode}) — "
            "redémarrage automatique dans 1 seconde…",
            file=_sys_early.stderr,
        )
        _time_supervisor.sleep(1.0)


if __name__ == "__main__" and not _os_early.environ.get(_SUPERVISOR_ENV):
    if not _acquire_single_instance():
        raise SystemExit(0)
    _supervise_native_process()
# ─────────────────────────────────────────────────────────────────────────────


# ── Nuclear: force CREATE_NO_WINDOW on EVERY subprocess call on Windows ───────
# This patches Popen itself, so no per-file flag is needed anywhere.
if _platform.system() == "Windows":
    _OrigPopen = _subprocess.Popen

    class _Popen(_OrigPopen):
        def __init__(self, args, **kw):
            kw["creationflags"] = kw.get("creationflags", 0) | _subprocess.CREATE_NO_WINDOW
            kw.pop("startupinfo", None)   # drop any stale/shared STARTUPINFO
            super().__init__(args, **kw)

    _subprocess.Popen = _Popen
# ─────────────────────────────────────────────────────────────────────────────


import asyncio
import concurrent.futures
import re
import signal
import threading
import time
import json
import sys
import traceback
import warnings
from datetime import datetime
from pathlib import Path

# ── Suppression des warnings bénins sur Wayland/KDE ──────────────────────────
# Xlib.xauth: warning, no xauthority details available
# → Ce print() vient directement de Xlib/xauth.py ligne 90 (pas de warnings module)
# Monkey-patch de Xlib.Xauth pour supprimer ce print() intrusif sur Wayland.
import logging
warnings.filterwarnings("ignore", message=".*xauthority.*")
warnings.filterwarnings("ignore", message=".*no xauthority.*")

def _patch_xlib_xauth_warning():
    """Monkey-patch Xlib.Xauthority pour supprimer le print 'no xauthority' sur Wayland."""
    try:
        import Xlib.xauth as _xa
        _OrigXauthority = _xa.Xauthority
        class _SilentXauthority(_OrigXauthority):
            def __init__(self, *args, **kwargs):
                import sys, io
                # Capturer stdout/stderr pendant __init__ pour avaler les print()
                _old_stdout, _old_stderr = sys.stdout, sys.stderr
                sys.stdout = sys.stderr = io.StringIO()
                try:
                    super().__init__(*args, **kwargs)
                finally:
                    sys.stdout, sys.stderr = _old_stdout, _old_stderr
        _xa.Xauthority = _SilentXauthority
    except Exception:
        pass  # Si Xlib n'est pas installé ou déjà patché

_patch_xlib_xauth_warning()

# Filtre les messages de Qt/KDE (kf.config.core) — via logging
logging.getLogger("kf.config.core").setLevel(logging.CRITICAL)
logging.getLogger("kf").setLevel(logging.CRITICAL)
# ─────────────────────────────────────────────────────────────────────────────


import collections
import threading as _threading_early

import numpy as np
from ui import JarvisUI
from memory.memory_manager import (
    load_memory, update_memory, format_memory_for_prompt,
)
from core import (context_probe, human_confirmation, memory_store, routines,
                  screen_reader, speaker_id, tool_packs, undo_stack)
from core.event_bus import (
    AsyncEventBus,
    ConnectionStateChangedEvent,
    EventBusBridge,
    SystemAlertEvent,
)
from core.memory_episode import EpisodeRecorder
from core.plugin_registry import PluginRegistry
from core.thread_pool import get_thread_pool, shutdown_all

# `google.genai` prend ~7-8s à importer sur cette machine (pydantic + son
# fichier types.py géant) — un import direct ici retarderait d'autant
# l'apparition de la fenêtre (l'orbe), puisque tout le module main.py doit
# finir de s'importer avant que `main()` ne puisse créer la fenêtre Qt.
# On lance l'import réel dans un thread dès la fin du chargement du module
# (voir le bas du fichier), pendant que Qt construit déjà la fenêtre ; tout
# usage de `genai`/`types` attend `_genai_ready` avant de s'en servir.
genai = None
types = None
sd = None
_genai_ready = _threading_early.Event()
_genai_import_started = False
_genai_import_lock = _threading_early.Lock()
_genai_import_error = None
_sounddevice_ready = _threading_early.Event()
_sounddevice_import_started = False
_sounddevice_import_lock = _threading_early.Lock()
_sounddevice_import_error = None


def _import_google_genai() -> None:
    global genai, types, _genai_import_error
    try:
        from google import genai as _genai
        from google.genai import types as _types
        genai = _genai
        types = _types
    except BaseException as exc:
        _genai_import_error = exc
    finally:
        # Même un import cassé doit réveiller run(), qui pourra afficher une
        # erreur claire au lieu de rester bloqué pour toujours.
        _genai_ready.set()


def _start_google_genai_import() -> None:
    global _genai_import_started
    if _genai_import_started:
        return
    with _genai_import_lock:
        if _genai_import_started:
            return
        _genai_import_started = True
        _threading_early.Thread(
            target=_import_google_genai,
            daemon=True,
            name="genai-import",
        ).start()


def _import_sounddevice() -> None:
    global sd, _sounddevice_import_error
    try:
        import sounddevice as _sounddevice
        sd = _sounddevice
    except BaseException as exc:
        _sounddevice_import_error = exc
    finally:
        _sounddevice_ready.set()


def _start_sounddevice_import() -> None:
    global _sounddevice_import_started
    if _sounddevice_import_started:
        return
    with _sounddevice_import_lock:
        if _sounddevice_import_started:
            return
        _sounddevice_import_started = True
        _threading_early.Thread(
            target=_import_sounddevice,
            daemon=True,
            name="sounddevice-import",
        ).start()

from actions.file_processor import file_processor
from actions.flight_finder     import flight_finder
from actions.open_app          import open_app
from actions.close_app         import close_app
from actions.weather_report    import weather_action
from actions.send_message      import send_message
from actions.reminder          import reminder
from actions.computer_settings import computer_settings
from actions.screen_processor  import _capture_camera, _capture_screen
from actions.youtube_video     import youtube_video
from actions.desktop           import desktop_control
from actions.browser_control   import browser_control
from actions.file_controller   import file_controller
from actions.code_helper       import code_helper
from actions.dev_agent         import dev_agent
from actions.web_search        import web_search as web_search_action
from actions.image_search      import image_search as image_search_action
from actions.computer_control  import computer_control
from actions.game_updater      import game_updater
from actions.media_control     import media_control
from actions.system_monitor    import SystemMonitor, get_system_status
from actions.proactive         import ProactiveService, desktop_blocks_proactivity
from actions.background_tasks  import BackgroundTaskService, format_tasks, format_agent_result
from actions.web_search        import (
    _news as _fetch_news_sync,
    DAILY_AI_CYBER_NEWS_QUERY,
)
from memory.config_manager     import get_brief_enabled, save_live_voice
from actions.shell_exec        import shell_exec, hypr_control
from actions.devsecops         import devsecops_control
from actions.hypr_orchestrator import hypr_orchestrator_control
from actions.auto_debug        import auto_debug_action
from actions.navigation        import navigation_action
from core.multimodal_vision    import inspect_screen_live
from core.screen_consciousness import get_screen_consciousness
from core.auto_debug           import auto_debug_live
from actions.capture           import capture_control
from actions.music             import music_control
from actions.find_nearby        import find_nearby
from actions.email              import email_control
from actions.calendar           import calendar_control
from actions.cloud_integrations import cloud_integrations_control
from actions.contacts           import contacts_control
from actions.sparring_partner   import sparring_partner, observe_sparring_utterance
from core.stt                  import AudioPreprocessor
from core.live_speech_config   import (
    DEFAULT_LIVE_VOICE,
    build_input_transcription_config,
    build_output_transcription_config,
    normalise_live_voice,
)
from core.personality_modes import voice_settings_for_mode
from core.live_model_policy    import (
    DEFAULT_FALLBACK_MODEL,
    DEFAULT_PRIMARY_MODEL,
    LiveModelPolicy,
)
from core.gemini_connection    import (
    ConnectionState,
    is_invalid_api_key_error,
    is_invalid_live_setup_error,
    safe_error_summary,
)
from core.speech_sync          import split_caption_units, caption_targets
from core.wake_word            import WakeWordDetector
from core.barge_in             import InterruptPhraseDetector, LocalBargeInListener
from core.continuous_conversation import (
    ContinuousConversationManager,
    DEFAULT_FOLLOW_UP_TIMEOUT_S,
    is_assistant_sleep_request,
)
from core.daily_briefing       import (
    collect_briefing_data,
    format_briefing_prompt,
    format_briefing_card,
    mark_briefing_delivered,
    should_trigger_daily_briefing,
)
from core.ipc                  import ControlServer
from core                      import audio_router
from core                      import tool_stats
from core.habit_model          import HabitModel, suggestion_text
from core.timers               import TimerService
from core.distraction_guard    import DistractionGuard
from core.action_runtime       import (
    ActionRuntime,
    ActionRuntimeError,
    friendly_runtime_error,
)



from core.audio_engine import (
    AudioEngine,
    CHANNELS,
    SEND_SAMPLE_RATE,
    RECEIVE_SAMPLE_RATE,
    CHUNK_SIZE,
    _OUTPUT_SLICE_MS,
    _OUTPUT_LATENCY_S,
    _MAX_UTTERANCE_S,
    _END_SILENCE_S,
    _BARGE_ARM_S,
    _BARGE_CONFIRM_S,
    _update_barge_in,
    HalfDuplexGate,
)
from core.session_manager import (
    SessionManager,
    LIVE_MODEL,
    LIVE_FALLBACK_MODEL,
    _STALE_AUDIO_TURN_S,
    _resume_would_replay_turn,
    _get_api_key,
    _voice_engine_settings,
    _setting_bool,
    _load_system_prompt,
    _clean_transcript,
    _live_audio_data,
    get_base_dir,
    BASE_DIR,
    API_CONFIG_PATH,
    PROMPT_PATH,
)
from core.tool_dispatcher import (
    ToolDispatcher,
    CONSULT_BRAIN_DECLARATION,
    TOOL_DECLARATIONS,
    _RETIRED_TOOLS,
    _DESTRUCTIVE_TOOLS,
    _DESTRUCTIVE_SETTINGS,
    _is_destructive,
    _TOOL_LABELS,
)
from core.proactive_engine import ProactiveEngine
from core.phone_relay import PhoneRelay

class JarvisLive(AudioEngine, SessionManager, ToolDispatcher, ProactiveEngine, PhoneRelay):
    """Orchestrateur vocal : compose audio, session Live, outils, proactivité, téléphone.

    Les cinq moteurs vivent dans ``core/`` et sont composés comme mixins. Les
    alias explicites ci-dessous préservent l'identité des méthodes historiques,
    tandis que l'héritage garantit qu'une nouvelle méthode ou propriété de
    moteur ne peut plus être oubliée lors de son intégration. Un seul objet porte
    ``_is_speaking`` / ``_interrupted`` / ``_model_turn_active``, et les tests
    qui extraient des unbound methods continuent de fonctionner.
    La communication entre moteurs passe par ces méthodes publiques de l'hôte
    et par ``asyncio.Event`` / ``threading.Event``, jamais par un accès croisé
    aux ``_private`` d'une autre instance.
    """

    _interrupted: bool = False
    _is_thinking: bool = False
    _model_turn_active: bool = False
    _active_turn_task = None


    # ── moteurs extraits : méthodes liées à l'hôte (tests + half-duplex) ────
    set_speaking = AudioEngine.set_speaking
    _estimate_spoken_seconds = staticmethod(AudioEngine._estimate_spoken_seconds)
    _queue_spoken_text = AudioEngine._queue_spoken_text
    _drain_spoken_text_queue = AudioEngine._drain_spoken_text_queue
    _flush_synced_speech_text = AudioEngine._flush_synced_speech_text
    _reset_speech_sync = AudioEngine._reset_speech_sync
    _clear_interrupted = AudioEngine._clear_interrupted
    interrupt = AudioEngine.interrupt
    discard_model_audio = AudioEngine.discard_model_audio
    _end_discarded_turn = AudioEngine._end_discarded_turn
    _DISCARD_TURN_MAX_S = AudioEngine._DISCARD_TURN_MAX_S
    _on_mic_device_change = AudioEngine._on_mic_device_change
    _on_mic_sensitivity_change = AudioEngine._on_mic_sensitivity_change
    _on_output_device_change = AudioEngine._on_output_device_change
    _activity_start = AudioEngine._activity_start
    _activity_end = AudioEngine._activity_end
    _enqueue_out = AudioEngine._enqueue_out
    _listen_audio = AudioEngine._listen_audio
    _play_audio = AudioEngine._play_audio

    _on_elevenlabs_voice_change = SessionManager._on_elevenlabs_voice_change
    _on_stt_provider_change = SessionManager._on_stt_provider_change
    _on_voice_provider_change = SessionManager._on_voice_provider_change
    _on_live_voice_change = SessionManager._on_live_voice_change
    _on_brain_provider_change = SessionManager._on_brain_provider_change
    _try_switch_conversation_language = SessionManager._try_switch_conversation_language
    _watch_live_voice_change = SessionManager._watch_live_voice_change
    _extend_toolkit = SessionManager._extend_toolkit
    speak = SessionManager.speak
    _submit_text_turn = SessionManager._submit_text_turn
    speak_error = SessionManager.speak_error
    _build_config = SessionManager._build_config
    _send_realtime = SessionManager._send_realtime
    _receive_audio = SessionManager._receive_audio
    _resend_unanswered = SessionManager._resend_unanswered
    analyze_user_audio = SessionManager.analyze_user_audio
    prosody_analyzer = SessionManager.prosody_analyzer
    get_current_mood = SessionManager.get_current_mood
    get_prosody_context_instruction = SessionManager.get_prosody_context_instruction
    get_tts_modulation = SessionManager.get_tts_modulation
    apply_prosody_to_tts_config = SessionManager.apply_prosody_to_tts_config
    inject_dynamic_prosody = SessionManager.inject_dynamic_prosody
    continuous_vision = SessionManager.continuous_vision
    start_continuous_vision = SessionManager.start_continuous_vision
    stop_continuous_vision = SessionManager.stop_continuous_vision
    toggle_continuous_vision = SessionManager.toggle_continuous_vision
    send_video_frame = SessionManager.send_video_frame
    check_continuous_vision_voice_trigger = SessionManager.check_continuous_vision_voice_trigger
    check_persona_voice_trigger = SessionManager.check_persona_voice_trigger
    switch_persona = SessionManager.switch_persona
    detect_contextual_persona = SessionManager.detect_contextual_persona
    switch_persona_then_submit = SessionManager.switch_persona_then_submit
    thought_streamer = SessionManager.thought_streamer
    feed_thought_token = SessionManager.feed_thought_token
    feed_tool_start = SessionManager.feed_tool_start
    feed_tool_end = SessionManager.feed_tool_end

    _on_plugin_toggle = ToolDispatcher._on_plugin_toggle
    _refresh_plugin_runtime = ToolDispatcher._refresh_plugin_runtime
    _execute_tool = ToolDispatcher._execute_tool
    _verify_sensitive_voice_command = ToolDispatcher._verify_sensitive_voice_command
    _execute_tool_batch = ToolDispatcher._execute_tool_batch
    _compute_deep_research = staticmethod(ToolDispatcher._compute_deep_research)
    _deliver_deep_research = ToolDispatcher._deliver_deep_research
    _deliver_image_generation = ToolDispatcher._deliver_image_generation
    _start_image_generation = ToolDispatcher._start_image_generation
    _start_deep_research = ToolDispatcher._start_deep_research
    _deliver_decision_simulation = ToolDispatcher._deliver_decision_simulation
    _start_decision_simulation = ToolDispatcher._start_decision_simulation
    _execute_tool_impl = ToolDispatcher._execute_tool_impl
    _agent_tools = ToolDispatcher._agent_tools
    _arg = staticmethod(ToolDispatcher._arg)
    _agent_find_nearby = ToolDispatcher._agent_find_nearby
    _agent_show_map = ToolDispatcher._agent_show_map
    _agent_camera = ToolDispatcher._agent_camera
    _agent_show_card = ToolDispatcher._agent_show_card
    _agent_speak = ToolDispatcher._agent_speak
    _agent_ask = ToolDispatcher._agent_ask
    _agent_status = ToolDispatcher._agent_status
    _agent_email = ToolDispatcher._agent_email
    _agent_memory_save = ToolDispatcher._agent_memory_save
    _agent_memory_search = ToolDispatcher._agent_memory_search
    _agent_second_brain = ToolDispatcher._agent_second_brain
    _agent_voice_id = ToolDispatcher._agent_voice_id
    _agent_voice_style = ToolDispatcher._agent_voice_style
    _dispatch_agent_tool = ToolDispatcher._dispatch_agent_tool
    _agent_routine = ToolDispatcher._agent_routine
    _agent_self_repair = ToolDispatcher._agent_self_repair
    _agent_weather = ToolDispatcher._agent_weather
    _agent_web_search = ToolDispatcher._agent_web_search
    _agent_image_search = ToolDispatcher._agent_image_search
    _agent_screenshot = ToolDispatcher._agent_screenshot
    _agent_inspect_screen = ToolDispatcher._agent_inspect_screen
    _agent_point_on_screen = ToolDispatcher._agent_point_on_screen
    _agent_open_app = ToolDispatcher._agent_open_app
    _agent_close_app = ToolDispatcher._agent_close_app
    _agent_computer_settings = ToolDispatcher._agent_computer_settings
    _agent_file_search = ToolDispatcher._agent_file_search
    _agent_search_personal_docs = ToolDispatcher._agent_search_personal_docs
    _agent_location = ToolDispatcher._agent_location
    _agent_youtube = ToolDispatcher._agent_youtube
    _agent_reminder = ToolDispatcher._agent_reminder
    _agent_system_status = ToolDispatcher._agent_system_status
    _agent_music = ToolDispatcher._agent_music
    _background_tasks_control = ToolDispatcher._background_tasks_control

    _import_existing_habits = ProactiveEngine._import_existing_habits
    _observe_habit_reply = ProactiveEngine._observe_habit_reply
    _get_user_name = ProactiveEngine._get_user_name
    _send_daily_briefing = ProactiveEngine._send_daily_briefing
    _send_startup_briefing = ProactiveEngine._send_startup_briefing
    _show_active_reminders_card = ProactiveEngine._show_active_reminders_card
    _announce_due_reminder = ProactiveEngine._announce_due_reminder
    _run_reminder_watch = ProactiveEngine._run_reminder_watch
    _render_timers = ProactiveEngine._render_timers
    _announce_due_timer = ProactiveEngine._announce_due_timer
    _run_timer_watch = ProactiveEngine._run_timer_watch
    _run_focus_guard_watch = ProactiveEngine._run_focus_guard_watch
    _run_calendar_watch = ProactiveEngine._run_calendar_watch
    _run_prayer_watch = ProactiveEngine._run_prayer_watch
    _run_github_backup_watch = ProactiveEngine._run_github_backup_watch
    _run_auto_extension_watch = ProactiveEngine._run_auto_extension_watch
    _run_gmail_watch = ProactiveEngine._run_gmail_watch
    _run_system_monitor = ProactiveEngine._run_system_monitor
    _run_tiktok_watch = ProactiveEngine._run_tiktok_watch
    _run_proactive_mode = ProactiveEngine._run_proactive_mode
    _run_habit_model = ProactiveEngine._run_habit_model
    _maybe_routine = ProactiveEngine._maybe_routine
    _run_routine = ProactiveEngine._run_routine

    camera = PhoneRelay.camera
    _uploads_dir = PhoneRelay._uploads_dir
    _camera_index = PhoneRelay._camera_index
    _save_capture = PhoneRelay._save_capture
    _on_phone_frame = PhoneRelay._on_phone_frame
    _phone_camera_command = PhoneRelay._phone_camera_command
    _on_camera_state = PhoneRelay._on_camera_state
    _camera_tool = PhoneRelay._camera_tool
    _confirm_phone_camera = PhoneRelay._confirm_phone_camera
    _close_camera_quietly = PhoneRelay._close_camera_quietly
    _on_camera_action = PhoneRelay._on_camera_action
    _grab_camera_still = PhoneRelay._grab_camera_still
    _make_remote_key = PhoneRelay._make_remote_key
    _relay_phone_audio = PhoneRelay._relay_phone_audio
    _on_phone_connected = PhoneRelay._on_phone_connected
    _show_human_confirmation = PhoneRelay._show_human_confirmation
    _hide_human_confirmation = PhoneRelay._hide_human_confirmation
    _process_dashboard_commands = PhoneRelay._process_dashboard_commands

    def _notify_human_confirmation(self, message: str) -> None:
        if hasattr(self, "ui") and self.ui:
            self.ui.write_log(f"VOX : {message}")
        if hasattr(self, "say") and callable(self.say):
            try:
                self.say(message)
            except Exception:
                pass
        elif hasattr(self, "speak") and callable(self.speak):
            try:
                self.speak(message)
            except Exception:
                pass

    def __init__(self, ui: JarvisUI):
        self.ui             = ui
        self._event_bus = AsyncEventBus.for_instance(self)
        AsyncEventBus.set_default(self._event_bus)
        self._event_bridge = EventBusBridge(self._event_bus)
        self._asst_name     = "ANO-GPT"   # updated each session from config
        self.session              = None
        self._tool_session_memory = {}   # état persistant inter-tours pour les tools (ex: désambiguïsation close_app)
        self._agent_tool_table = None    # table MCP, construite au premier appel
        self._plugins = PluginRegistry(
            Path(__file__).resolve().parent / "plugins",
            Path(__file__).resolve().parent / "config" / "plugin_states.json",
            {tool["name"] for tool in TOOL_DECLARATIONS},
        )
        self._plugins.discover()
        from core.tool_registry import build_production_declarations
        self._tool_registry, self._tool_declarations = build_production_declarations(
            TOOL_DECLARATIONS
        )
        self._action_runtime = ActionRuntime(
            self._tool_declarations
            + self._plugins.declarations()
            # Le relais vers le cerveau externe est un outil comme un autre du
            # point de vue du runtime : sans sa déclaration ici, tout appel
            # serait rejeté d'office comme « action inconnue ».
            + [CONSULT_BRAIN_DECLARATION]
        )
        human_confirmation.bind(
            show=self._show_human_confirmation,
            hide=self._hide_human_confirmation,
            log=self.ui.write_log,
            notify=self._notify_human_confirmation,
        )
        self.audio_in_queue       = None
        self.speech_text_queue    = None
        self.out_queue            = None
        self._loop                = None
        self._is_speaking         = False
        self._speaking_lock       = threading.Lock()
        self._audio_abort_event   = threading.Event()
        self._barge_stop_event    = threading.Event()
        self._speech_display_open = False
        self._speech_next_text_at = 0.0
        self._audio_turn_active   = False
        self._audio_enqueued_sec  = 0.0
        self._audio_played_sec    = 0.0
        self._speech_last_target_sec = 0.0
        # Fin d'audio déjà attribuée à un sous-titre, et latence réelle de la
        # sortie : les deux calent le texte sur ce qui est entendu.
        self._speech_audio_cursor = 0.0
        self._audio_output_latency = _OUTPUT_LATENCY_S
        self._phone_active        = False   # True while phone mic is streaming; pauses PC mic
        self._pending_vision       = None    # (img_bytes, mime_type, question, angle) to inject after tool response
        self._vision_cam_active    = False   # True if camera was opened for vision → auto-close after response
        self._vision_close_pending = False   # True after vision injected; next turn_complete closes camera
        self._vision_last_time     = 0.0     # monotonic time of last screen_process call (cooldown guard)
        self._vision_busy          = False   # True while a vision capture/inject cycle is in flight
        self._interrupted          = False   # True while draining audio after user interrupt
        self._discard_turn_audio   = False   # audio du tour coupé jeté jusqu'à son turn_complete
        self._discard_turn_audio_since = 0.0
        self._is_thinking          = False   # True while executing tools / reasoning
        self._active_turn_task     = None    # Current in-flight turn submit task
        # Ces tâches sont les actions enfant du tour Live, et non la tâche qui
        # reçoit la session. Les annuler permet au bouton Arrêter de stopper
        # l'action en cours sans fermer la connexion Gemini.
        self._active_tool_tasks: set[asyncio.Task] = set()
        # Une recherche profonde peut durer plus longtemps que le délai accordé
        # à un appel d'outil par Gemini Live. Elle vit donc hors du tour Live,
        # avec une référence forte pour survivre jusqu'à la livraison du résultat.
        self._deep_research_tasks: set[asyncio.Task] = set()
        self._image_generation_tasks: set[asyncio.Task] = set()
        # Les vidéos Sora suivent exactement le même contrat que les images :
        # elles vivent hors du tour Live, mais empêchent la veille automatique
        # afin que l'utilisateur puisse continuer la conversation et recevoir
        # le résultat final. L'attribut doit exister dès le démarrage (et non
        # seulement après la première demande vidéo).
        self._video_generation_tasks: set[asyncio.Task] = set()
        self._decision_simulation_tasks: set[asyncio.Task] = set()
        # Mémoire longue durée : l'enregistreur d'épisodes accumule les tours,
        # et l'ensemble des identifiants déjà injectés évite de redire au
        # modèle un souvenir qu'il a encore sous les yeux (le contexte Live est
        # cumulatif : réinjecter, c'est payer deux fois pour du bruit).
        self._episode              = EpisodeRecorder()
        self._recalled_ids: set[int] = set()
        # Reconnaissance du locuteur : l'audio confirmé du tour en cours, et
        # les derniers énoncés complets — ce sont eux qui servent à apprendre
        # une voix, sans avoir à ouvrir un mode « enregistrement ».
        self._speaker              = None    # SpeakerID, chargé au premier tour
        self._voice_chunks         = collections.deque(maxlen=1000)
        self._voice_clips          = collections.deque(maxlen=4)
        self._last_voice_clip       = None
        self._last_voice_clip_at    = 0.0
        self._speaker_verdict      = None
        self._speaker_verified_at  = 0.0
        self._speaker_check_pending = False
        self._speaker_announced    = None    # dernier état signalé au modèle
        # Contexte ambiant : l'état de la machine tel qu'il était quand il a
        # commencé à parler — c'est celui-là qui explique « ferme ça », pas
        # celui d'après la réponse, où l'assistant a pu changer la fenêtre.
        self._ambient_state: dict[str, str] = {}
        self._ambient_fields: dict[str, str] | None = None
        self._prosody_mode = ""
        # À l'extinction, la dernière conversation n'a pas encore de résumé.
        # On l'écrit sans passer par l'agent : lancer un sous-processus au
        # moment où le programme se ferme retarderait la fermeture.
        import atexit as _atexit
        _atexit.register(
            lambda: self._episode.flush(reason="arrêt", allow_agent=False)
        )
        self._model_turn_active    = False   # True entre le 1er contenu reçu et le turn_complete du modèle
        self._last_turn_complete_at = 0.0
        self._had_live_session     = False
        self._activity_open        = False   # True entre activity_start et activity_end (VAD client)
        self._activity_since       = 0.0     # monotonic du début du tour ouvert
        self._voice_evidence_ms    = 0.0     # voix humaine confirmée dans le tour courant
        self._last_voice_evidence_ms = 0.0   # photographie du dernier tour fermé
        self._last_voice_audio_ms  = 0.0
        self._gesture_controller = None
        self.ui.on_text_command   = self._on_text_command
        self.ui.on_remote_clicked = self._make_remote_key
        self.ui.on_camera_action  = self._on_camera_action
        self.ui.on_camera_open    = lambda: self.camera.open(self.camera.source)
        self.ui.on_camera_close   = self._close_camera_quietly
        # Un clic Qt arrive sur le thread principal, alors que les tâches Live
        # appartiennent à la boucle asyncio. Le pont ci-dessous évite un
        # task.cancel() hors-thread, qui pouvait sembler ne rien faire.
        self.ui.on_interrupt      = self._request_interrupt
        self.ui.on_mic_device_change      = self._on_mic_device_change
        self.ui.on_output_device_change   = self._on_output_device_change
        self.ui.on_plugins_list           = self._plugins.status
        self.ui.on_plugin_toggle          = self._on_plugin_toggle
        self.ui.on_mic_sensitivity_change = self._on_mic_sensitivity_change
        self.ui.on_voice_change           = self._on_live_voice_change
        self.ui.on_brain_change           = self._on_brain_provider_change
        self.ui.on_stt_provider_change = self._on_stt_provider_change
        self.ui.on_voice_provider_change = self._on_voice_provider_change
        self.ui.on_elevenlabs_voice_change = self._on_elevenlabs_voice_change
        self._preprocessor  = None   # défini dans _listen_audio, réglable en direct depuis l'UI
        self._mic_reopen_evt: threading.Event | None = None
        self._turn_done_event: asyncio.Event | None = None
        self._turn_submit_lock: asyncio.Lock | None = None
        self._audio_turn_pending = False
        # Reprise de session : poignée Gemini, rythme des tentatives et
        # décision de se taire ou non sur une coupure brève.
        self._conn          = ConnectionState()
        voice_settings = voice_settings_for_mode(_voice_engine_settings())
        # Azure Speech est un second avis facultatif : Gemini reste la source
        # STT primaire et aucune phrase n'est envoyée à Azure sans opt-in.
        self._azure_speech_enabled = _setting_bool(
            voice_settings.get("azure_speech_verify"), False
        )
        self._azure_fast_phrases = _setting_bool(
            voice_settings.get("azure_speech_fast_phrases"), False
        )
        self._azure_speech_verifier = None
        # Reconnaissance unifiée : ne jamais réactiver Scribe via une ancienne
        # configuration persistée.
        self._stt_provider = "gemini"
        self._live_voice = normalise_live_voice(
            voice_settings.get("live_voice", DEFAULT_LIVE_VOICE)
        )
        self._voice_change_event: asyncio.Event | None = None
        self._voice_reconnect_requested = False
        # Outils par contexte : seul le noyau est offert au modèle au départ.
        # Un paquet ouvert le reste jusqu'à la fin du processus — l'élargir
        # coûte une reconnexion, la refermer n'apporterait rien.
        self._active_tool_packs: frozenset[str] = frozenset()
        self._toolkit_reconnect_requested = False
        self._context_compression_enabled = True
        self._live_models = LiveModelPolicy(
            primary=voice_settings.get("live_model", LIVE_MODEL),
            fallback=voice_settings.get("live_model_fallback", LIVE_FALLBACK_MODEL),
        )
        # Éviter de relire le fichier de configuration dans le thread audio :
        # Scribe peut réutiliser la même seconde passe Gemini sans exposer la
        # clé à l'interface ou aux journaux.
        self._gemini_api_key = str(voice_settings.get("gemini_api_key", "") or "")
        # Ce que l'utilisateur a dit sans obtenir de réponse : renvoyé après
        # une coupure, sinon sa phrase est perdue avec la connexion.
        # Tour jugé « bruit » par le garde-fou : sa réponse ne doit pas sortir.
        self._noise_turn = False
        self._live_user_text = ""
        self._unanswered: list[str] = []
        self._dashboard     = None
        self._briefing_sent    = False          # morning briefing fires once per process
        self._sys_monitor      = SystemMonitor()  # persistent cooldown state
        self._proactive        = ProactiveService()
        self._screen_mind      = get_screen_consciousness()
        self._habits           = HabitModel()
        self._timers           = TimerService()
        self._focus_guard      = DistractionGuard()
        self._timer_card_titles: set[str] = set()
        self._import_existing_habits()
        self._pending_habit_event: tuple[str, float] | None = None
        self._background_tasks = BackgroundTaskService(self._proactive.publish)
        self._last_user_speech = time.monotonic()  # updated on every user utterance
        self._control          = None    # ControlServer (global hotkey → IPC)
        self._wake             = None    # WakeWordDetector (lazy, offline)
        self._camera           = None    # CameraStudio (lazy — OpenCV coûte cher)
        self._pending_phone_frame = None  # image reçue avant l'ouverture du studio
        self._wake_enabled     = True
        def _on_conversation_idle(reason: str) -> None:
            # Les travaux longs vivent hors du tour Live. Ne jamais mettre la
            # conversation en veille pendant l'un d'eux : le micro doit rester
            # disponible pour discuter, demander l'avancement ou lancer une
            # autre action, et le résultat final doit pouvoir être annoncé.
            if self._has_active_long_task():
                self.ui.write_log(
                    "SYS : veille différée — tâche longue toujours en cours ; "
                    "vous pouvez continuer à parler ou écrire."
                )
                return
            if self._loop and self._loop.is_running():
                self._loop.call_soon_threadsafe(self._sleep, reason)
            else:
                self._sleep(reason)

        self._continuous       = ContinuousConversationManager(
            timeout_s=DEFAULT_FOLLOW_UP_TIMEOUT_S,
            on_sleep=_on_conversation_idle,
            on_log=lambda msg: (
                self.ui.write_log(msg) if hasattr(self.ui, "write_log") else None
            ),
        )

        # Cinq moteurs extraits. Les méthodes restent liées à cet hôte
        # (tests + drapeaux half-duplex atomiques) ; les instances documentent
        # la composition et reçoivent les callbacks typés.
        self.audio = AudioEngine()
        self.session_mgr = SessionManager()
        self.tools = ToolDispatcher()
        self.proactive_engine = ProactiveEngine()
        self.phone = PhoneRelay()

    def _has_active_long_task(self) -> bool:
        """Indique si une action durable doit garder la conversation éveillée."""
        long_task_sets = (
            getattr(self, "_image_generation_tasks", set()),
            getattr(self, "_video_generation_tasks", set()),
            getattr(self, "_deep_research_tasks", set()),
            getattr(self, "_decision_simulation_tasks", set()),
        )
        return any(
            not task.done()
            for task_set in long_task_sets
            for task in task_set
        )

    def _on_text_command(self, text: str):
        text = str(text or "").strip()
        if not text:
            return
        # « Mets-toi en veille » concerne l'assistant, jamais le processus.
        # Le traiter avant le modèle interdit un shutdown_jarvis accidentel.
        if is_assistant_sleep_request(text):
            self._sleep("commande vocale")
            return
        # Écrire dans le chat est une action explicite de l'utilisateur : elle
        # doit réveiller l'assistant au même titre que le mot « ANO ». Sans ce
        # réveil, Gemini pouvait recevoir le texte alors que la sortie restait
        # muette après la veille, donnant l'impression qu'il ne répondait plus.
        if getattr(self.ui, "muted", False):
            self._wake_up("commande texte")
        if self._try_switch_conversation_language(text):
            return
        # Cette commande ne doit jamais être déléguée au modèle : elle doit
        # reconstruire son prompt système et, pour Gemini, sa voix native.
        if self._try_switch_personality_mode(text):
            return
        self._observe_habit_reply(text)
        try:
            from core.prosody import get_prosody_manager
            get_prosody_manager().record_user_query(text)
        except Exception:
            pass

        # Interception de validation conversationnelle si une confirmation humaine est en attente
        pending = human_confirmation.current()
        if pending is not None:
            clean = text.strip().lower()
            tokens = set(re.findall(r"\b\w+\b", clean))
            approve_tokens = {
                "oui", "confirmer", "confirme", "valider", "valide",
                "ok", "lance", "exécute", "execute", "installer", "installe"
            }
            cancel_tokens = {
                "non", "annuler", "annule", "stop", "refuse", "refuser",
                "abandonner"
            }
            if tokens & approve_tokens or "vas-y" in clean or "vas y" in clean or "c'est bon" in clean:
                human_confirmation.resolve(pending.token, accepted=True, source="chat/voix")
                return
            if tokens & cancel_tokens or "laisse tomber" in clean or "ne fais pas" in clean:
                human_confirmation.resolve(pending.token, accepted=False, source="chat/voix")
                return
        # Une routine reconnue part tout de suite : ni réseau, ni génération.
        if self._maybe_routine(text, "texte"):
            return
        auto_persona = self.detect_contextual_persona(text)
        if auto_persona:
            if self._loop and self.session:
                self._loop.call_soon_threadsafe(self._activity_end)
                if getattr(getattr(self, "_current_persona", None), "id", None) == auto_persona:
                    asyncio.run_coroutine_threadsafe(
                        self._submit_text_with_screen(text), self._loop
                    )
                else:
                    asyncio.run_coroutine_threadsafe(
                        self.switch_persona_then_submit(auto_persona, text), self._loop
                    )
            return
        # Commutation de persona / mode métier par commande texte ("Jarvis, passe en mode DevOps")
        p_match = self.check_persona_voice_trigger(text)
        if p_match:
            if self._loop and self.session:
                asyncio.run_coroutine_threadsafe(
                    self.switch_persona(p_match, full_in=text), self._loop
                )
            else:
                try:
                    from core.persona_manager import get_persona_manager
                    get_persona_manager().switch_persona(p_match, session_manager=self, ui=self.ui)
                except Exception as exc:
                    print(f"[Persona] Échec commutation texte : {exc}")
            return
        if should_trigger_daily_briefing(text):
            if self._loop and self.session:
                asyncio.run_coroutine_threadsafe(
                    self._send_daily_briefing("texte"), self._loop
                )
                return
        if not self._loop or not self.session:
            return
        # Un tour vocal encore ouvert entrerait en conflit avec le tour texte
        # qu'on envoie ici : on le referme d'abord.
        self._loop.call_soon_threadsafe(self._activity_end)
        asyncio.run_coroutine_threadsafe(
            self._submit_text_with_screen(text),
            self._loop
        )

    async def _submit_text_with_screen(self, text: str) -> bool:
        """Tour texte précédé du cliché d'écran si la question porte dessus."""
        mind = getattr(self, "_screen_mind", None)
        if mind is not None:
            try:
                await mind.inject_into_live(self, text)
            except Exception as exc:
                print(f"[Écran] injection texte ignorée : {exc}")
        return await self._submit_text_turn(text)
    def _wake_up(self, reason: str = "hotkey") -> str:
        """Unmute and start listening. Safe to call when already listening."""
        if self.ui.muted:
            self.ui.muted = False
            self.ui.write_log(f"SYS : réveil ({reason}) — micro actif.")
            print(f"[ANO-GPT] 🔔 Wake ({reason})")
            if self._wake is not None:
                self._wake.reset()
        if hasattr(self, "_continuous"):
            self._continuous.on_wake_up(reason)
        self._last_user_speech = time.monotonic()
        if hasattr(self, "_proactive"):
            self._proactive.wake()
        return "listening"


    def _sleep(self, reason: str = "hotkey") -> str:
        """Mute the mic. Wake word keeps running locally while muted."""
        wake = getattr(self, "_wake", None)
        if reason == "inactivité conversation" and not (
            wake is not None and getattr(wake, "available", False)
        ):
            # Sans modèle Vosk, mettre automatiquement le micro en veille est
            # un cul-de-sac : dire « ANO » ne peut plus le rouvrir. Tant que le
            # téléchargement du modèle local n'a pas abouti, rester à l'écoute
            # est le seul repli qui conserve un assistant vocal utilisable.
            self.ui.write_log(
                "SYS : veille différée — réveil « ANO » indisponible, micro maintenu actif."
            )
            return "listening"
        if hasattr(self, "_continuous"):
            self._continuous.reset()
        if not self.ui.muted:
            self.ui.muted = True
            self.ui.write_log(f"SYS : veille ({reason}) — micro coupé.")
            print(f"[ANO-GPT] 🔕 Sleep ({reason})")
        return "muted"
    # ── reconnaissance du locuteur ──────────────────────────────────────────

    def _check_speaker(self) -> None:
        """Ferme l'énoncé et lance sa vérification. Appelé à la fin du tour.

        Le calcul de l'empreinte coûte une centaine de millisecondes : il part
        dans un thread. La boucle audio, elle, ne fait ici qu'assembler des
        tableaux déjà en mémoire.
        """
        # Un verdict du tour précédent ne doit jamais autoriser le tour
        # courant. Sans cette remise à zéro, une seule reconnaissance du
        # propriétaire pouvait laisser passer ensuite une autre voix pendant
        # la courte fenêtre de calcul CAM++.
        self._speaker_verdict = None
        self._speaker_verified_at = 0.0
        self._speaker_check_pending = False

        chunks = list(self._voice_chunks)
        self._voice_chunks.clear()
        if not chunks:
            return
        clip = np.concatenate(chunks)
        self._last_voice_clip = clip.copy()
        self._last_voice_clip_at = time.monotonic()
        if clip.size < speaker_id.MIN_SECONDS * SEND_SAMPLE_RATE:
            return
        self._voice_clips.append(clip)
        self._speaker_check_pending = True

        def _work() -> None:
            try:
                if self._speaker is None:
                    self._speaker = speaker_id.SpeakerID()
                if not self._speaker.enrolled:
                    return
                verdict = self._speaker.verify(clip)
            except Exception as exc:
                print(f"[Voix] vérification impossible : {exc}")
                return
            if verdict is None:
                return
            self._speaker_verdict = verdict
            self._speaker_verified_at = time.monotonic()
            who = verdict.name or "voix inconnue"
            print(f"[Voix] 🎙️ {who} (score {verdict.score:.2f})")
            self._announce_speaker(verdict)
            self._speaker_check_pending = False

        def _work_guarded() -> None:
            try:
                _work()
            finally:
                self._speaker_check_pending = False

        get_thread_pool().submit(
            "compute-light", _work_guarded, task_name="speaker-verify",
            stall_timeout=15.0,
        )

    def _announce_speaker(self, verdict) -> None:
        """Signale au modèle un changement de locuteur, une seule fois.

        Le répéter à chaque tour polluerait le contexte et finirait par lui
        faire dire « bonjour Anonymous » toutes les trois phrases.
        """
        state = verdict.name or "inconnu"
        if state == self._speaker_announced or not self.session or not self._loop:
            return
        self._speaker_announced = state
        if verdict.known:
            note = (f"[VOIX] C'est bien {verdict.name} qui parle, sa voix est "
                    "reconnue. Tu peux le saluer par son prénom si l'occasion "
                    "s'y prête, sans expliquer comment tu le sais.")
        else:
            note = ("[VOIX] Cette voix n'est pas celle de l'utilisateur "
                    "enregistré. Reste courtois et serviable, mais ne fais "
                    "rien de destructeur ni de personnel : pas de suppression, "
                    "pas d'extinction, pas de lecture de ses messages.")
        # Gemini Live 3.1 refuse aussi les client_content incomplets. Cette
        # information est utile mais ne doit jamais faire tomber la voix.
        self._deferred_voice_note = note

    def _voice_is_stranger(self) -> bool:
        """Vrai si le tour courant n'est pas autorisé par Voice ID.

        Tant qu'aucune empreinte n'est configurée, Voice ID reste optionnel.
        Dès qu'un profil existe, la politique devient *fail closed* pour les
        outils sensibles : calcul en cours, extrait trop court, verdict absent,
        périmé ou voix inconnue bloquent tous l'action. Un ancien verdict connu
        ne peut donc plus autoriser une autre personne.
        """
        if getattr(self, "_speaker_check_pending", False):
            return True
        speaker = getattr(self, "_speaker", None)
        if speaker is None or not speaker.enrolled:
            return False
        verdict = self._speaker_verdict
        if verdict is None:
            return True
        if (time.monotonic() - self._speaker_verified_at) >= 30.0:
            return True
        return not verdict.known

    # ── mémoire longue durée ────────────────────────────────────────────────

    @staticmethod
    def _store_memory(category: str, key: str, value: str) -> str:
        """Écrit dans la base longue durée, en choisissant la bonne nature.

        Une préférence est vraie en permanence et doit être connue dès la
        première phrase ; un projet ou un souhait est daté et n'a de raison de
        remonter que lorsque la conversation y touche.
        """
        kind = (memory_store.KIND_PROFILE
                if category in ("identity", "preferences", "relationships")
                else memory_store.KIND_FACT)
        try:
            return memory_store.save(value, kind=kind, key=key,
                                     category=category)
        except Exception as exc:
            print(f"[Mémoire] écriture impossible : {exc}")
            return "C'est noté."

    def _remember_turn(self, user_text: str, assistant_text: str) -> None:
        """Ajoute le tour à l'épisode en cours, en clôturant celui d'avant.

        Dix minutes de silence séparent deux conversations : au-delà, ce qui
        précède est une autre histoire et mérite son propre résumé.
        """
        try:
            if self._episode.idle_seconds > 600:
                self._flush_episode("silence")
            self._episode.add_turn(user_text, assistant_text)
            # L'indexation SQLite part hors de la boucle audio : la voix et le
            # barge-in ne paient jamais le coût d'écriture du graphe.
            get_thread_pool().submit(
                "disk-io", self._record_second_brain_turn,
                user_text, assistant_text,
                task_name="record-conversation-turn",
                stall_timeout=15.0,
            )
        except Exception as exc:
            print(f"[Mémoire] tour non retenu : {exc}")

    @staticmethod
    def _record_second_brain_turn(user_text: str, assistant_text: str) -> None:
        try:
            from core.vector_memory import get_vector_memory
            from core.knowledge_graph import record_conversation_turn

            get_vector_memory().save_turn(user_text, assistant_text)
            record_conversation_turn(user_text, assistant_text)
        except Exception as exc:
            print(f"[Second Brain] tour non indexé : {exc}")

    def _flush_episode(self, reason: str, *, allow_agent: bool = True) -> None:
        """Lance l'écriture du résumé sans jamais faire attendre la voix."""
        get_thread_pool().submit(
            "disk-io", self._episode.flush,
            reason=reason, allow_agent=allow_agent,
            task_name="flush-episode", stall_timeout=30.0,
        )

    def _probe_ambient(self) -> None:
        """Photographie l'état de la machine au moment où il prend la parole.

        Dans un thread : `hyprctl` et `nmcli` sont des sous-processus, et une
        vingtaine de millisecondes passées ici seraient prises sur la boucle
        qui écoule l'audio.
        """
        def _work() -> None:
            try:
                self._ambient_fields = context_probe.ambient_fields()
            except Exception as exc:
                print(f"[Contexte] sondes indisponibles : {exc}")

        get_thread_pool().submit(
            "compute-light", _work, task_name="ambient-context-probe",
            stall_timeout=10.0,
        )

    async def _inject_turn_context(self, phrase: str) -> None:
        """Met à jour ce que le modèle sait : la machine, le ton, la mémoire.

        Une session Live fige son `system_instruction` à l'ouverture : le seul
        moyen d'enrichir le contexte en cours de route est d'envoyer un tour
        client. Avec `turn_complete=False`, il s'ajoute au contexte sans
        déclencher de réponse — l'assistant ne dira donc rien, mais au tour
        suivant il saura. D'où le décalage d'un tour, inévitable ici : la
        transcription d'entrée arrive pendant que le modèle répond déjà, trop
        tard pour peser sur la réponse courante.

        Ce décalage est sans conséquence sur le contexte ambiant, et c'est même
        l'inverse : les sondes ont été lues au moment où il a ouvert la bouche,
        donc l'état qu'il avait sous les yeux en disant « ferme ça » — pas
        celui d'après, que les actions de l'assistant ont pu changer.

        Les trois blocs partent ensemble, en un seul message : trois envois
        séparés coûteraient trois fois plus de contexte pour la même chose.
        """
        session = self.session
        if session is None:
            return

        parts: list[str] = []
        pending_mode = self._prosody_mode

        # 1. La machine — seulement ce qui a bougé depuis la dernière fois.
        try:
            ambient, state = await asyncio.to_thread(
                context_probe.ambient_delta,
                self._ambient_state, self._ambient_fields,
            )
        except Exception as exc:
            print(f"[Contexte] delta impossible : {exc}")
            ambient, state = "", self._ambient_state
        if ambient:
            parts.append(ambient)

        # 2. Le ton — il ne change qu'aux basculements (nuit, urgence, focus,
        #    question répétée), et le répéter à l'identique n'apprendrait rien.
        try:
            from core.prosody import get_prosody_manager

            manager = get_prosody_manager()
            profile = manager.evaluate_profile(
                query=phrase,
                active_window=(self._ambient_fields or {}).get("Fenêtre", ""),
            )
            if profile.mode != self._prosody_mode:
                parts.append(manager.format_prosody_instruction(profile))
                # Retenu seulement si l'envoi aboutit : marquer un ton comme
                # annoncé alors qu'il n'est pas parti le rendrait invisible
                # jusqu'au basculement suivant.
                pending_mode = profile.mode
        except Exception as exc:
            print(f"[Prosodie] évaluation impossible : {exc}")

        # 3. La mémoire — les souvenirs que la phrase vient de réveiller.
        ids: list[int] = []
        try:
            block, ids = await asyncio.to_thread(
                memory_store.recall_block, phrase,
                exclude_ids=tuple(self._recalled_ids),
            )
            if block:
                parts.append(block)
        except Exception as exc:
            print(f"[Mémoire] rappel impossible : {exc}")

        if not parts or self.session is not session:
            self._ambient_state = state
            return
        # Le modèle vient peut-être de reprendre la parole : injecter pendant
        # sa génération risquerait de la parasiter, et tout ceci sera de toute
        # façon aussi utile au tour d'après.
        if self._interrupted or self._model_turn_active:
            return
        # L'ancien client_content(turn_complete=False) est rejeté par Live 3.1.
        # Conserver le delta pour un prochain tour au lieu de sacrifier toute
        # la connexion à un enrichissement silencieux.
        self._deferred_context = "\n".join(parts)
        self._ambient_state = state
        self._prosody_mode = pending_mode
        self._recalled_ids.update(ids)
    async def _start_control_server(self) -> None:
        """
        Expose control commands on a Unix socket.

        Wayland gives no client global keyboard access, so the compositor owns
        the hotkey: a Hyprland bind runs `anogpt-ctl toggle`, which lands here.
        """
        server = ControlServer()

        def _toggle(_: str) -> str:
            return self._sleep("hotkey") if not self.ui.muted else self._wake_up("hotkey")

        def _ask(text: str) -> str:
            if not text:
                return "no text given"
            self._wake_up("ask")
            self._on_text_command(text)
            return f"sent: {text[:60]}"

        def _status(_: str) -> str:
            return (
                f"{'muted' if self.ui.muted else 'listening'} "
                f"session={'up' if self.session else 'down'} "
                f"wake={'on' if (self._wake and self._wake.available and self._wake_enabled) else 'off'}"
            )

        def _action_stats(raw: str) -> str:
            """Coût réel des actions et des processus qu'elles lancent.

            Les mesures vivent en mémoire dans le processus de l'assistant :
            le diagnostic hors ligne ne peut pas les voir, alors que ce sont
            elles qui disent où part le temps quand la voix hache. Trié par
            temps cumulé, cinquante lignes au plus.
            """
            from core.action_kit import stats_snapshot

            rows = stats_snapshot()
            if not rows:
                return "aucune action mesurée depuis le démarrage"
            filtre = raw.strip().lower()
            if filtre:
                rows = {k: v for k, v in rows.items() if filtre in k.lower()}
                if not rows:
                    return f"aucune mesure correspondant à « {filtre} »"
            ordre = sorted(rows.items(),
                           key=lambda kv: kv[1]["calls"] * kv[1]["avg_ms"],
                           reverse=True)[:50]
            lignes = [f"{'nom':38} {'appels':>7} {'échecs':>7} "
                      f"{'moy ms':>8} {'p95 ms':>8}"]
            for name, st in ordre:
                lignes.append(f"{name[:38]:38} {st['calls']:7.0f} "
                              f"{st['errors']:7.0f} {st['avg_ms']:8.1f} "
                              f"{st['p95_ms']:8.1f}")
            return "\n".join(lignes)

        def _show(_: str) -> str:
            self.ui.show_window()
            return "window activated"

        def _wake_toggle(_: str) -> str:
            self._wake_enabled = not self._wake_enabled
            state = "on" if self._wake_enabled else "off"
            self.ui.write_log(f"SYS : mot d'activation {state}.")
            return f"wake word {state}"

        def _proactive_mode(raw: str) -> str:
            mode = raw.strip().casefold()
            if mode in {"silence", "silent", "off", "0"}:
                return "proactif " + self._proactive.set_silent(True)
            if mode in {"on", "actif", "active", "1"}:
                return "proactif " + self._proactive.set_silent(False)
            if mode in {"", "status", "statut"}:
                return "proactif " + ("silence" if self._proactive.silent else "actif")
            raise ValueError("mode attendu : on, silence ou status")

        def _proactive_event(raw: str) -> str:
            try:
                payload = json.loads(raw)
            except Exception as exc:
                raise ValueError(f"événement JSON invalide : {exc}") from exc
            if not isinstance(payload, dict):
                raise ValueError("l'événement doit être un objet JSON")
            topic = str(payload.get("topic") or "custom").strip().lower()
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            message = str(payload.get("message") or "").strip()
            if topic == "terminal":
                duration = float(data.get("duration_s") or payload.get("duration_s") or 0)
                if duration < 20:
                    return "ignoré (commande courte)"
                command = str(data.get("command") or payload.get("command") or "la commande")[:100]
                code = int(data.get("exit_code") if data.get("exit_code") is not None
                           else payload.get("exit_code") or 0)
                message = message or (
                    f"La commande {command} est terminée après {duration:.0f} secondes, "
                    f"avec le code {code}."
                )
                # Une fin de build est aussi un bon événement pour revérifier
                # l'espace disque, sans instaurer un minuteur de sondage.
                asyncio.create_task(asyncio.to_thread(self._proactive.evaluate_disk))
                matched = self._background_tasks.handle_event(topic, {
                    **data,
                    "command": data.get("command") or payload.get("command") or "",
                    "duration_s": duration,
                    "exit_code": code,
                })
                if matched:
                    return f"{matched} tâche(s) de fond déclenchée(s)"
            accepted = self._proactive.publish(
                topic,
                message,
                dedupe_key=str(payload.get("dedupe_key") or ""),
                priority=int(payload.get("priority") or 50),
                data=data,
            )
            return "mis en file" if accepted else "ignoré (doublon ou invalide)"

        async def _tool(raw: str) -> str:
            """Point d'entrée des agents externes (MCP), via core/tool_bridge.

            Le travail part dans un fil : le socket de contrôle est servi par
            la boucle asyncio qui porte aussi l'audio, et une recherche de
            lieux de trois secondes y hacherait la voix.
            """
            from core.tool_bridge import dispatch

            return await asyncio.to_thread(dispatch, self._agent_tools(), raw)

        server.register("tool",      _tool)
        server.register("toggle",    _toggle)
        server.register("listen",    lambda _: self._wake_up("hotkey"))
        server.register("mute",      lambda _: self._sleep("hotkey"))
        server.register("ask",       _ask)
        server.register("status",    _status)
        server.register("action-stats", _action_stats)
        server.register("show",      _show)
        server.register("wake",      _wake_toggle)
        server.register("proactive-mode", _proactive_mode)
        server.register("proactive-event", _proactive_event)
        server.register("interrupt", lambda _: (self.interrupt(), "interrupted")[1])
        server.register("ping",      lambda _: "pong")

        try:
            await server.start()
            self._control = server
        except Exception as e:
            print(f"[ANO-GPT] ⚠️ Control socket unavailable: {e}")
            self.ui.write_log(f"SYS : socket de contrôle indisponible — {e}")
    def _request_interrupt(self) -> None:
        """Transfère une demande d'arrêt Qt vers la boucle Live propriétaire.

        `asyncio.Task.cancel()` n'est sûr que depuis le thread de sa boucle.
        Le bouton HUD, lui, est exécuté par Qt : sans ce relais, l'état pouvait
        afficher l'arrêt alors que l'outil continuait de tourner.
        """
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self.interrupt)
        else:
            # L'application peut recevoir le clic pendant son démarrage, avant
            # que la boucle audio ne soit prête.
            self.interrupt()

    # ── main loop ───────────────────────────────────────────────────────────

    async def run(self):
        self._loop = asyncio.get_event_loop()
        from core.observability import install_asyncio_handler
        install_asyncio_handler(self._loop)
        # Tous les ``asyncio.to_thread`` et ``run_in_executor(None, ...)`` des
        # sous-systèmes partagent désormais le pool supervisé réseau/IO, au
        # lieu de créer le pool implicite non observable d'asyncio.
        self._loop.set_default_executor(
            get_thread_pool().get_executor("network-heavy")
        )
        self._event_bus.set_loop(self._loop)
        self._voice_change_event = asyncio.Event()

        # Initialisation paresseuse des fonctionnalités vision/RAG terminées.
        # Elles tournent hors de la boucle audio et restent dormantes tant que
        # l'utilisateur n'active ni caméra ni gestes.
        try:
            from core.gesture_control import get_gesture_controller
            self._gesture_controller = get_gesture_controller(
                camera_source=self.camera, ui_instance=self.ui, audio_engine=self,
            )
            self._gesture_controller.start()
        except Exception as exc:
            self.ui.write_log(f"WARN : contrôle gestuel indisponible — {exc}")

        def _warm_personal_rag() -> None:
            try:
                from core.personal_rag import get_personal_rag
                get_personal_rag().start_background_indexing()
            except Exception as exc:
                print(f"[RAG] démarrage différé impossible : {exc}")

        # Même limitée à Documents, une première indexation vectorielle peut
        # monopoliser le GIL pendant des minutes. L'index déjà présent reste
        # immédiatement consultable; sa reconstruction/surveillance continue
        # est opt-in sur les petites machines.
        if _os_early.environ.get("ANOGPT_BACKGROUND_RAG", "").strip().lower() in {
            "1", "true", "yes", "on",
        }:
            get_thread_pool().submit(
                "disk-io", _warm_personal_rag,
                task_name="rag-bootstrap", stall_timeout=30.0,
            )
        else:
            self.ui.write_log(
                "SYS : indexation personnelle en arrière-plan différée pour préserver la voix."
            )

        def _maintain_storage() -> None:
            # Statistiques d'index et reprise WAL : quelques millisecondes, sur
            # le pool disque, jamais sur le chemin de la voix. Un compactage
            # réécrirait 275 Mo — il est seulement signalé, pas lancé ici.
            try:
                from core.storage_maintenance import startup_maintenance
                startup_maintenance()
            except Exception as exc:
                logging.getLogger("anogpt.storage").warning(
                    "entretien du stockage ignoré", exc_info=exc
                )

        get_thread_pool().submit(
            "disk-io", _maintain_storage,
            task_name="storage-maintenance", stall_timeout=60.0,
        )

        # Démarre ici en repli (main() le lance normalement juste après avoir
        # affiché la fenêtre). Le lancer pendant l'import de tous les modules
        # créait des contentions/cycles entre imports et pouvait figer le
        # démarrage complet.
        _start_google_genai_import()
        if not _genai_ready.is_set():
            print("[JARVIS] En attente de la fin du chargement de google.genai...")
            await asyncio.to_thread(_genai_ready.wait)
        if _genai_import_error is not None:
            raise RuntimeError(
                f"Impossible de charger google.genai: {_genai_import_error}"
            ) from _genai_import_error

        _start_sounddevice_import()
        try:
            await asyncio.wait_for(
                asyncio.to_thread(_sounddevice_ready.wait), timeout=20.0
            )
        except asyncio.TimeoutError as exc:
            raise RuntimeError(
                "Le système audio (PortAudio) ne répond pas après 20 secondes. "
                "Vérifie ou redémarre PipeWire/PulseAudio."
            ) from exc
        if _sounddevice_import_error is not None:
            raise RuntimeError(
                f"Impossible de charger sounddevice: {_sounddevice_import_error}"
            ) from _sounddevice_import_error
        saved_output = audio_router.get_output_override()
        if saved_output and not audio_router.set_output_override(saved_output):
            self.ui.write_log(
                "WARN : la sortie audio enregistrée n'est plus disponible ; "
                "la sortie système reste utilisée."
            )

        # Start dashboard (optional — needs: pip install fastapi "uvicorn[standard]" cryptography)
        try:
            from dashboard.server import DashboardServer
            self._dashboard = DashboardServer()
            self._dashboard.set_connect_callback(self._on_phone_connected)
            self._dashboard.set_frame_callback(self._on_phone_frame)
            self._dashboard.set_confirmation_callback(
                lambda token, accepted: human_confirmation.resolve(
                    token, accepted, source="AnoRemote"
                )
            )

            from core.navigation import get_navigation_manager
            nav_mgr = get_navigation_manager()

            def _location_event(position: dict) -> None:
                lat = position.get("lat")
                lon = position.get("lon")
                acc = position.get("accuracy_m")
                if lat is not None and lon is not None:
                    # Le prochain tour doit voir la nouvelle position, pas la
                    # valeur mise en cache avant la connexion du téléphone.
                    context_probe.clear_cache()
                    try:
                        self.ui.update_live_position(float(lat), float(lon), acc)
                    except Exception:
                        pass
                    try:
                        nav_mgr.update_position(float(lat), float(lon), acc)
                    except Exception:
                        pass

                matched = self._background_tasks.observe_location(position)
                if not matched:
                    self._proactive.observe_location(position)

            self._dashboard.set_location_callback(_location_event)
            # Réservé au bouton « réveiller » explicite. Les messages texte et
            # le micro d'ANO Remote utilisent leurs propres canaux et ne doivent
            # jamais réactiver implicitement le microphone matériel du PC.
            self._dashboard.set_wake_callback(
                lambda: self._wake_up("téléphone")
            )
            from core.context_probe import set_phone_presence_provider
            set_phone_presence_provider(
                lambda: bool(
                    self._dashboard
                    and (self._dashboard._clients or self._dashboard.phone_camera_online())
                )
            )
            # `spawn` retient la tâche : sans référence forte, le ramasse-
            # miettes pouvait annuler le serveur en pleine course.
            self._dashboard.spawn(self._dashboard.serve(), "dashboard-serve")
            # Runs for the whole lifetime, not just inside an active session
            self._dashboard.spawn(
                self._process_dashboard_commands(), "dashboard-commands"
            )
        except Exception as e:
            print(f"[Dashboard] Disabled: {e}")
            self._dashboard = None

        # Control socket — lives for the whole process, not per-session, so the
        # global hotkey keeps working across reconnects.
        await self._start_control_server()
        asyncio.create_task(
            self._background_tasks.run(), name="persistent-background-tasks"
        )
        # Une capture Grim peut tenir le GIL plus d'une seconde sur cette
        # machine. La conscience d'écran reste pleinement disponible quand
        # l'utilisateur dit « regarde l'écran », mais sa veille continue est
        # opt-in afin de ne jamais voler le budget de la voix.
        if _os_early.environ.get("ANOGPT_CONTINUOUS_SCREEN", "").strip().lower() in {
            "1", "true", "yes", "on",
        }:
            asyncio.create_task(
                self._screen_mind.run(self), name="screen-consciousness"
            )
        else:
            self.ui.write_log(
                "SYS : veille visuelle continue désactivée pour préserver la voix — "
                "dis « regarde l’écran » pour une analyse immédiate."
            )
        # L'index personnel démarre déjà dans son propre worker. Une seconde
        # marche historique sur Documents + dépôts ici doublait I/O, SQLite et
        # CPU au pire moment : les premières requêtes vocales. Elle reste
        # disponible à la demande; l'amorçage agressif est réservé à un choix
        # explicite sur les machines qui ont des ressources à y consacrer.
        if _os_early.environ.get("ANOGPT_EAGER_FILE_INDEX", "").strip().lower() in {
            "1", "true", "yes", "on",
        }:
            try:
                from core.file_indexer import get_file_indexer
                from core.knowledge_graph import bootstrap

                def _warm_second_brain():
                    bootstrap()
                    return get_file_indexer().scan_directory_incremental()

                asyncio.get_event_loop().run_in_executor(None, _warm_second_brain)
            except Exception as e:
                print(f"[Second Brain] Synchronisation initiale ignorée: {e}")

        while True:
            self._last_disconnect_was_net_err = False
            session_connected = False
            try:
                print("[JARVIS] Connecting...")
                self._event_bus.publish_sync(ConnectionStateChangedEvent(
                    old_state="offline", new_state="connecting",
                    retry_count=int(getattr(self._conn, "retry_count", 0) or 0),
                ))
                self.ui.set_state("THINKING")
                config = self._build_config()

                # Fresh client on every reconnect — avoids stale HTTP session state
                client = genai.Client(
                    api_key=_get_api_key(),
                    http_options={"api_version": "v1beta"}
                )

                live_model = self._live_models.current
                async with (
                    client.aio.live.connect(model=live_model, config=config) as session,
                    asyncio.TaskGroup() as tg,
                ):
                    self.session          = session
                    session_connected     = True
                    # Quatre secondes de PCM suffisent pour absorber une rafale
                    # réseau sans autoriser une latence et une RAM illimitées.
                    self.audio_in_queue   = asyncio.Queue(maxsize=80)
                    self.speech_text_queue = asyncio.Queue(maxsize=120)
                    self._stt_provider = "gemini"
                    self.out_queue        = asyncio.Queue(maxsize=200)
                    self._turn_done_event = asyncio.Event()
                    self._turn_submit_lock = asyncio.Lock()
                    self._audio_turn_pending = False
                    self._live_send_trace = collections.deque(maxlen=24)

                    # Reset transient state that must not carry over from a previous session
                    self._pending_vision       = None
                    self._vision_cam_active    = False
                    self._vision_close_pending = False
                    self._vision_busy          = False
                    self._vision_last_time     = 0.0
                    self._interrupted          = False
                    self._discard_turn_audio   = False
                    self._model_turn_active    = False
                    self._speech_display_open  = False
                    self._speech_next_text_at  = 0.0
                    # Nouvelle session : aucun tour n'est ouvert côté serveur.
                    self._activity_open        = False

                    resumed = self._conn.resume_handle() is not None
                    reconnecting = self._had_live_session
                    self._had_live_session = True
                    self._conn.on_connected()
                    self._event_bus.publish_sync(ConnectionStateChangedEvent(
                        old_state="connecting", new_state="connected", retry_count=0,
                    ))
                    print(
                        f"[JARVIS] Connected ({live_model})."
                        + (" (session reprise)" if resumed else "")
                    )
                    self.ui.set_state("LISTENING")
                    self.ui.write_log(
                        "SYS: connexion vocale rétablie." if reconnecting
                        else "SYS: JARVIS online."
                    )

                    if self._dashboard:
                        await self._dashboard.broadcast({"type": "status", "state": "active"})

                    # Ce que l'utilisateur avait demandé juste avant la coupure
                    # repart en premier : sinon sa question meurt avec la
                    # connexion et il doit la répéter sans savoir pourquoi.
                    tg.create_task(self._resend_unanswered())

                    tg.create_task(self._send_realtime())
                    tg.create_task(self._listen_audio())
                    tg.create_task(self._receive_audio())
                    tg.create_task(self._play_audio())
                    tg.create_task(self._run_proactive_mode())
                    tg.create_task(self._run_habit_model())
                    tg.create_task(self._run_gmail_watch())
                    tg.create_task(self._run_reminder_watch())
                    tg.create_task(self._run_timer_watch())
                    tg.create_task(self._run_focus_guard_watch())
                    tg.create_task(self._run_calendar_watch())
                    tg.create_task(self._run_prayer_watch())
                    tg.create_task(self._run_tiktok_watch())
                    tg.create_task(self._run_github_backup_watch())
                    tg.create_task(self._run_auto_extension_watch())
                    tg.create_task(self._watch_live_voice_change())
                    if self._dashboard:
                        tg.create_task(self._relay_phone_audio())

                    # Morning briefing — fires once per process launch (if enabled)
                    if not self._briefing_sent and get_brief_enabled():
                        self._briefing_sent = True
                        tg.create_task(self._send_startup_briefing())

            except asyncio.CancelledError:
                # Fermeture demandée par la fenêtre Qt : ne jamais transformer
                # cette annulation normale en reconnexion réseau.
                raise
            except KeyboardInterrupt:
                raise
            except SystemExit:
                raise
            except BaseException as e:
                # Catches both Exception and BaseExceptionGroup (Python 3.11+
                # TaskGroup raises BaseExceptionGroup when tasks are cancelled
                # externally, which `except Exception` would miss, letting the
                # exception escape the while-loop and causing asyncio.run() to
                # start shutdown — resulting in "executor after shutdown" errors).
                err_str = str(e)
                if self._toolkit_reconnect_requested:
                    # Élargissement de la boîte à outils : contrairement à un
                    # changement de voix, la poignée de reprise est conservée —
                    # la conversation continue là où elle s'était arrêtée, et
                    # `_resend_unanswered` renvoie la demande en attente avec
                    # les nouveaux outils.
                    self._toolkit_reconnect_requested = False
                    self._voice_reconnect_requested = False
                    self._voice_change_event.clear()
                    self.ui.write_log(
                        "SYS : outils élargis — "
                        f"{tool_packs.labels(self._active_tool_packs)}."
                    )
                    continue
                if self._voice_reconnect_requested:
                    self._voice_reconnect_requested = False
                    self._voice_change_event.clear()
                    # Une reprise conserve la configuration vocale d'origine :
                    # repartir sans poignée garantit l'application du choix.
                    self._conn.forget_session()
                    self.ui.write_log(
                        f"SYS : activation de la voix {self._live_voice}…"
                    )
                    continue
                print(f"[JARVIS] Error ({type(e).__name__}): {e}")
                self._event_bus.publish_sync(SystemAlertEvent(
                    severity="ERROR", source="gemini-live",
                    message=f"{type(e).__name__}: {e}",
                ))
                traceback.print_exc()

                # Certains couples modèle/endpoint Live plus anciens rejettent
                # encore ce champ. Un seul repli contrôlé évite toute boucle de
                # redémarrage tout en gardant la reprise de session classique.
                if (
                    not session_connected
                    and self._context_compression_enabled
                    and "contextwindowcompression" in re.sub(
                        r"[^a-z]", "", err_str.casefold()
                    )
                ):
                    self._context_compression_enabled = False
                    self._conn.forget_session()
                    self.ui.write_log(
                        "WARN : compression de contexte indisponible sur ce modèle — "
                        "reconnexion compatible sans compression."
                    )
                    continue

                if (
                    not session_connected
                    and not self._live_models.using_fallback
                    and (
                        self._live_models.should_fallback(e)
                        or is_invalid_live_setup_error(e)
                    )
                ):
                    fallback = self._live_models.activate_fallback()
                    self._conn.forget_session()
                    self._conn.on_go_away()
                    self.ui.write_log(
                        "WARN: Gemini 3.1 Live indisponible pour ce compte ; "
                        f"repli automatique vers {fallback}."
                    )
                    continue

                # Ne jamais assimiler le code WebSocket 1007 à une clé invalide :
                # Gemini l'emploie aussi pour un JSON de setup incompatible.
                if is_invalid_api_key_error(e):
                    self.ui.write_log("ERR: API key invalid — please re-enter your key.")
                    self.ui.set_state("SLEEPING")
                    self.ui.prompt_reconfig()
                    while not self.ui._win._ready:
                        await asyncio.sleep(1)
                    print("[JARVIS] New API key saved — reconnecting...")
                    _conn_backoff = 3
                    continue

                if is_invalid_live_setup_error(e):
                    detail = safe_error_summary(e)
                    self.ui.write_log(
                        "ERR: Configuration Gemini Live refusée par le serveur — "
                        f"{detail}"
                    )
                    # Une poignée de reprise que le serveur n'accepte plus
                    # ferait échouer toutes les tentatives suivantes, à
                    # l'identique : on repart d'une session neuve.
                    self._conn.forget_session()
                    self.ui.set_state("OFFLINE")
                    self._last_disconnect_was_net_err = False
                    await asyncio.sleep(10)
                    continue

                # Network / timeout errors — log clearly and back off
                is_net_err = any(k in err_str for k in (
                    "TimeoutError", "timed out", "getaddrinfo", "CancelledError",
                    "ConnectionRefusedError", "OSError", "Cannot connect",
                ))
                if is_net_err:
                    delay = self._conn.next_delay()
                    self._conn_backoff = delay
                    # Un hoquet de Wi-Fi de trois secondes se rattrape sans
                    # rien dire : une carte d'erreur pour une absence que
                    # personne n'a remarquée inquiète plus qu'elle n'informe.
                    if not self._conn.is_silent_outage():
                        self.ui.write_log(
                            f"NET: Connexion impossible — nouvelle tentative dans {delay:.0f}s. "
                            "(un VPN est peut-être nécessaire)"
                        )
                        self.ui.set_state("OFFLINE")
                        try:
                            self.ui.show_card(
                                "error", "Connexion perdue",
                                f"Impossible de joindre Gemini Live. Nouvelle tentative dans "
                                f"**{delay:.0f}s** — je continue d'essayer automatiquement.\n\n"
                                f"Si ça persiste, un VPN est peut-être nécessaire.",
                            )
                        except Exception:
                            pass
                else:
                    self._conn_backoff = self._conn.next_delay()
                self._last_disconnect_was_net_err = is_net_err
            finally:
                self.session = None
                # Une poignée capturée pendant ou juste après une longue
                # réponse peut demander à Gemini de rejouer ce tour à la
                # reconnexion (le briefing observé deux fois). Dans cette
                # fenêtre, une session neuve avec notre contexte reconstruit
                # est plus sûre qu'une reprise ambiguë.
                if _resume_would_replay_turn(
                    model_turn_active=self._model_turn_active,
                    audio_turn_pending=self._audio_turn_pending,
                    audio_playing=self._audio_turn_active,
                    audio_queued=(
                        self.audio_in_queue is not None
                        and not self.audio_in_queue.empty()
                    ),
                    last_turn_complete_at=self._last_turn_complete_at,
                    now=time.monotonic(),
                ):
                    self._conn.forget_session()
                self._conn.on_disconnected()
                # La phrase restée sans réponse repartira à la reprise.
                if self._live_user_text:
                    self._unanswered.append(self._live_user_text)
                    del self._unanswered[:-2]
                    self._live_user_text = ""
                # Le souvenir ne s'écrit que si la conversation est vraiment
                # finie. Une coupure de dix secondes que l'on va rattraper
                # n'est pas une fin de séance : la raconter comme telle
                # découpait un même échange en trois souvenirs bancals.
                if self._conn.conversation_lost():
                    self._flush_episode("fin de session")

            self.set_speaking(False)
            if hasattr(self, "reset_audio_and_turn_state"):
                self.reset_audio_and_turn_state("session_disconnect_recovery")
            silent = self._conn.is_silent_outage()
            if not getattr(self, "_last_disconnect_was_net_err", False) and not silent:
                self.ui.set_state("SLEEPING")

            if self._dashboard and not silent:
                await self._dashboard.broadcast({"type": "status", "state": "sleeping"})

            delay = self._conn.next_delay()
            self._conn_backoff = delay
            print(f"[JARVIS] Reconnecting in {delay:.0f}s...")
            if delay:
                await asyncio.sleep(delay)

def main():
    # Avant la fenêtre : une panne d'initialisation doit laisser une trace,
    # même quand ANO-GPT est lancé sans terminal (raccourci, service).
    from core.observability import setup_logging
    journal = setup_logging()
    ui = JarvisUI("face.png")
    ui.write_log(f"SYS : journal — {journal}")
    # La fenêtre est désormais visible ; le SDK lourd peut se charger pendant
    # que l'utilisateur termine la configuration, sans retarder le premier
    # rendu ni entrer en concurrence avec les imports de l'application.
    _start_google_genai_import()
    _start_sounddevice_import()

    runtime: dict[str, object] = {}

    def runner():
        ui.wait_for_api_key()
        jarvis = JarvisLive(ui)

        async def _async_main():
            loop = asyncio.get_running_loop()
            task = asyncio.create_task(jarvis.run(), name="jarvis-live-runtime")
            runtime["loop"] = loop
            runtime["task"] = task
            await task

        try:
            asyncio.run(_async_main())
        except KeyboardInterrupt:
            print("\n🔴 Shutting down...")
        except asyncio.CancelledError:
            pass
        finally:
            shutdown_all(wait=False, cancel_futures=True)

    runtime_thread = get_thread_pool().spawn_thread(
        "network-heavy", "jarvis-async-runtime", runner,
        stall_timeout=float("inf"),
    )
    # Ne jamais fermer Qt directement depuis le handler POSIX : le signal peut
    # tomber au milieu de QBackingStore::flush. Un timer Qt traite la demande
    # entre deux événements de peinture.
    sigint_requested = threading.Event()
    try:
        from PyQt6.QtCore import QTimer

        signal.signal(signal.SIGINT, lambda _sig, _frame: sigint_requested.set())
        sigint_timer = QTimer(ui._win)
        sigint_timer.setInterval(40)

        def _poll_sigint() -> None:
            if sigint_requested.is_set():
                sigint_timer.stop()
                ui.root._app.quit()

        sigint_timer.timeout.connect(_poll_sigint)
        sigint_timer.start()
    except (AttributeError, ValueError):
        pass
    ui.root.mainloop()

    # La fenêtre est partie, mais le runtime audio/réseau vit dans un thread.
    # L'annuler puis l'attendre empêche CPython de décharger ses extensions
    # pendant que des callbacks tentent encore d'utiliser les exécuteurs.
    loop = runtime.get("loop")
    task = runtime.get("task")
    if loop is not None and task is not None:
        try:
            loop.call_soon_threadsafe(task.cancel)
        except (RuntimeError, AttributeError):
            pass
    runtime_thread.join(timeout=8.0)
    shutdown_all(wait=False, cancel_futures=True)

if __name__ == "__main__":
    main()
