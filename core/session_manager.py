"""Session Gemini Live : connexion, reconnexion, reprise, repli de modèle.

Concurrence
-----------
* **asyncio** — ``client.aio.live.connect``, TaskGroup, ``_send_realtime`` /
  ``_receive_audio`` / ``_submit_text_turn``.
* **thread d'import** — ``google.genai`` est chargé hors du thread Qt
  (voir ``_import_google_genai`` dans ``main.py``).
* Jamais de callback PortAudio ici. Le half-duplex est lu via les drapeaux
  de l'hôte (``_is_speaking``, ``_interrupted``, ``_model_turn_active``).
"""
from __future__ import annotations

import logging

import asyncio
from core.text_clean import strip_emoji
import json
import re
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Protocol

from core.background_task import spawn_logged
from core.ai_stt_corrector import STTCorrector, TranscriptAssembler, TranscriptGuard
from core.audio_engine import (
    CHANNELS,
    RECEIVE_SAMPLE_RATE,
    _OUTPUT_SLICE_MS,
    _MainAttr,
)
from core.event_bus import ModelSpeechDeltaEvent
from core.live_model_policy import (
    DEFAULT_FALLBACK_MODEL,
    DEFAULT_PRIMARY_MODEL,
)
from core.live_speech_config import (
    build_input_transcription_config,
    build_output_transcription_config,
    live_end_silence_ms,
    normalise_live_voice,
)
from core.conversation_language import detect_language_switch, normalise_conversation_language
from core.auto_persona import detect_contextual_persona
from core.personality_modes import (
    MODE_SPECS,
    PersonalityMode,
    active_mode,
    detect_mode_command,
    identity_address_line,
    set_active_mode,
    user_address,
    voice_settings_for_mode,
)
from core.stt import AudioPreprocessor
from core.tool_dispatcher import CONSULT_BRAIN_DECLARATION, TOOL_DECLARATIONS
from memory.memory_manager import format_memory_for_prompt, load_memory
from core import context_probe, memory_store, tool_packs
from actions.sparring_partner import observe_sparring_utterance
from core.prosody_analyzer import (
    ProsodyAnalyzer,
    MoodClassification,
    TTSModulation,
    get_prosody_analyzer,
)

types = _MainAttr("types")
genai = _MainAttr("genai")
save_live_voice = _MainAttr("save_live_voice")


def get_base_dir():
    import sys
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
PROMPT_PATH = BASE_DIR / "core" / "prompt.txt"
# Alias non daté, actuellement exposé par l'API pour cette clé. Une preview
# datée finit par disparaître ou changer de capacités et ne doit pas bloquer le
# démarrage de l'assistant.
LIVE_MODEL = DEFAULT_PRIMARY_MODEL
LIVE_FALLBACK_MODEL = DEFAULT_FALLBACK_MODEL

# Un tour audio sans le moindre retour serveur au bout de quelques secondes est
# perdu. Le conserver indéfiniment empêche toutes les commandes texte suivantes
# (interface et ANO Remote) de partir.
_STALE_AUDIO_TURN_S = 8.0
# Silence serveur toléré après une demande (voix, texte, réponse d'outil)
# avant de tenir la session pour morte et de la rouvrir.
_LIVE_REPLY_TIMEOUT_S = 15.0
_TIME_PARTICLE_RE = re.compile(
    r"\b(?:quelle?\s+heure|heure\s+est.il|l['’]heure|heure\s+actuelle|"
    r"il\s+est(?:\s+actuellement)?\s+\d{1,2}(?:\s*(?:h|:)\s*\d{0,2}|\s+heures?)?|"
    r"nous\s+sommes\s+(?:à\s+)?\d{1,2}(?:\s*(?:h|:)\s*\d{0,2}|\s+heures?)?|"
    r"what\s+time|time\s+is\s+it)\b",
    re.IGNORECASE,
)

def _resume_would_replay_turn(
    *, model_turn_active: bool, audio_turn_pending: bool,
    audio_playing: bool, audio_queued: bool,
    last_turn_complete_at: float, now: float,
) -> bool:
    """Vrai si une poignée Gemini risque de rejouer la dernière réponse."""
    recent_complete = (
        last_turn_complete_at > 0
        and now - last_turn_complete_at < 15.0
    )
    return bool(
        model_turn_active or audio_turn_pending or audio_playing
        or audio_queued or recent_complete
    )

def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


def _get_live_api_key() -> str:
    """Clé de la voix : `gemini_live_api_key` si elle existe, sinon la clé
    principale. Deux projets Google = voix gratuite + vision payante."""
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    return str(data.get("gemini_live_api_key") or "").strip() or data["gemini_api_key"]


def _voice_engine_settings() -> dict:
    try:
        value = json.loads(API_CONFIG_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _setting_bool(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().casefold() not in {"0", "false", "non", "off", "disabled"}
    return bool(value)


def _load_system_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        return (
            "You are JARVIS, Tony Stark's AI assistant. "
            "Be concise, direct, and always use the provided tools to complete tasks. "
            "Never simulate or guess results — always call the appropriate tool."
        )

_CTRL_RE = re.compile(r"<ctrl\d+>", re.IGNORECASE)

def _clean_transcript(text: str) -> str:
    text = _CTRL_RE.sub("", text)
    text = re.sub(r"[\x00-\x08\x0b-\x1f]", "", text)
    return text


def _live_audio_data(response) -> bytes | None:
    """Extrait le PCM Live sans déclencher l'avertissement de ``response.data``."""
    server_content = getattr(response, "server_content", None)
    model_turn = getattr(server_content, "model_turn", None)
    chunks = []
    for part in getattr(model_turn, "parts", None) or ():
        inline_data = getattr(part, "inline_data", None)
        data = getattr(inline_data, "data", None)
        if isinstance(data, bytes) and data:
            chunks.append(data)
    return b"".join(chunks) or None



class SessionHost(Protocol):
    """Contrat que l'orchestrateur expose au gestionnaire de session."""

    ui: Any
    session: Any
    audio_in_queue: Any
    out_queue: Any
    _conn: Any
    _live_models: Any
    _live_voice: str
    _interrupted: bool
    _model_turn_active: bool
    _plugins: Any

    def interrupt(self) -> None: ...
    def discard_model_audio(self) -> bool: ...
    def _end_discarded_turn(self) -> None: ...
    def _clear_interrupted(self) -> None: ...
    def _execute_tool_batch(self, function_calls) -> Any: ...
    def _reset_speech_sync(self) -> None: ...
    def _queue_spoken_text(self, text: str) -> None: ...
    def _remember_turn(self, user_text: str, assistant_text: str) -> None: ...


class SessionCallbacks(Protocol):
    """Callbacks typés SessionManager → orchestrateur."""

    def on_user_transcript(self, text: str) -> None: ...
    def on_turn_complete(self, user_text: str, assistant_text: str) -> None: ...
    def on_tool_calls(self, function_calls) -> Any: ...
    def on_go_away(self) -> None: ...


class SessionManager:
    """Connexion Gemini Live, backoff, poignées de reprise, repli de modèle.

    Les méthodes sont liées à l'hôte ``JarvisLive``. L'instance
    ``SessionManager()`` documente le moteur.
    """

    def _maybe_show_clock_particles(self, text: str) -> None:
        """Déclenche l'horloge sans toucher directement au thread Qt."""
        if not text or not _TIME_PARTICLE_RE.search(text):
            return
        trigger = getattr(getattr(self, "ui", None), "show_clock_particles", None)
        if callable(trigger):
            trigger()

    def _on_voice_provider_change(self, provider: str) -> None:
        from memory.config_manager import save_voice_provider
        save_voice_provider(provider)
        self._voice_reconnect_requested = True
        event = self._voice_change_event
        if self._loop and self._loop.is_running() and event is not None:
            self._loop.call_soon_threadsafe(event.set)

    def _on_stt_provider_change(self, provider: str) -> None:
        from memory.config_manager import save_stt_provider
        # Le STT est volontairement verrouillé sur Gemini Transcribe : aucun
        # crédit ElevenLabs ne peut être utilisé par erreur.
        if provider != "gemini":
            raise ValueError("Gemini Transcribe est le seul moteur de reconnaissance actif.")
        save_stt_provider("gemini")
        self._voice_reconnect_requested = True
        event = self._voice_change_event
        if self._loop and self._loop.is_running() and event is not None:
            self._loop.call_soon_threadsafe(event.set)

    def _on_brain_provider_change(self, provider: str) -> None:
        """Le cerveau a changé : la session Live doit être reconstruite.

        Le prompt et la liste d'outils de Gemini dépendent du cerveau actif, et
        ni l'un ni l'autre ne se modifie en cours de session : seule une
        reconnexion applique réellement le choix.
        """
        try:
            from core.llm_client import main_brain_label
            label = main_brain_label()
        except Exception:
            label = provider
        self.ui.write_log(f"SYS : cerveau — {label}.")
        self._voice_reconnect_requested = True
        event = self._voice_change_event
        if self._loop and self._loop.is_running() and event is not None:
            self._loop.call_soon_threadsafe(event.set)

    def _on_elevenlabs_voice_change(self, voice_id: str, model_id: str) -> None:
        from memory.config_manager import save_elevenlabs_voice
        save_elevenlabs_voice(voice_id, model_id)
        self._voice_reconnect_requested = True
        event = self._voice_change_event
        if self._loop and self._loop.is_running() and event is not None:
            self._loop.call_soon_threadsafe(event.set)

    def _on_live_voice_change(self, voice_name: str) -> None:
        """Persiste le choix Qt puis reconstruit la configuration Live."""
        voice = normalise_live_voice(voice_name)
        if voice == self._live_voice:
            return
        save_live_voice(voice)
        self._live_voice = voice
        self.ui.write_log(f"SYS : voix sélectionnée — {voice}.")
        self._voice_reconnect_requested = True
        event = self._voice_change_event
        if self._loop and self._loop.is_running() and event is not None:
            self._loop.call_soon_threadsafe(event.set)

    def _try_switch_conversation_language(self, text: str) -> bool:
        """Applique une demande explicite et reconstruit la session Live.

        Le prompt, le STT et la transcription de sortie sont immuables pendant
        une connexion Live : la reconnexion garantit donc une seule langue au
        tour suivant.
        """
        selected = detect_language_switch(text)
        if selected is None:
            return False
        current = normalise_conversation_language(
            getattr(self, "_conversation_language", "fr-FR")
        )
        if selected == current:
            self.ui.write_log(f"SYS : langue de conversation déjà en {selected.label_fr}.")
            return True
        from memory.config_manager import save_conversation_language
        save_conversation_language(selected.code)
        self._conversation_language = selected.code
        self.ui.write_log(
            f"SYS : langue de conversation → {selected.label_fr} — reconnexion en cours."
        )
        self._voice_reconnect_requested = True
        event = getattr(self, "_voice_change_event", None)
        if self._loop and self._loop.is_running() and event is not None:
            self._loop.call_soon_threadsafe(event.set)
        return True

    def _try_switch_personality_mode(self, text: str) -> bool:
        """Applique une commande explicite de ton, texte ou transcription vocale.

        Le prompt et la voix Gemini Live sont immuables pendant une connexion :
        l'événement ferme donc proprement la session pour la rouvrir avec le
        nouveau mode, sans jamais ouvrir le microphone pendant la synthèse.
        """
        target = detect_mode_command(text)
        if target is None:
            return False
        previous = active_mode()
        spec = set_active_mode(target)
        settings = voice_settings_for_mode(_voice_engine_settings())
        if settings.get("voice_provider", "gemini") == "gemini":
            self._live_voice = normalise_live_voice(settings.get("live_voice"))
        self._user_name = spec.user_address
        if previous is target:
            self.ui.write_log(f"SYS : mode {spec.label} déjà actif.")
            return True
        self.ui.write_log(f"SYS : mode {spec.label} activé — reconnexion vocale en cours.")
        self._voice_reconnect_requested = True
        event = getattr(self, "_voice_change_event", None)
        if self._loop and self._loop.is_running() and event is not None:
            self._loop.call_soon_threadsafe(event.set)
        return True

    def _extend_toolkit(self, text: str, *, origin: str = "demande") -> bool:
        """Ouvre les paquets que cette phrase réclame et relance la session.

        Gemini Live fige ses outils à la connexion : les élargir impose une
        reconnexion. Elle est faite tout de suite, poignée de reprise gardée,
        et ``_resend_unanswered`` renvoie la demande en attente — l'utilisateur
        obtient sa réponse avec le bon outil au lieu d'un refus.
        """
        wanted = tool_packs.resolve(text)
        active = getattr(self, "_active_tool_packs", frozenset())
        fresh = wanted - active
        if not fresh:
            return False
        self._active_tool_packs = active | fresh
        self._toolkit_reconnect_requested = True
        self._voice_reconnect_requested = True
        print(f"[Outils] {origin} → paquets ouverts : {tool_packs.labels(fresh)}")
        event = getattr(self, "_voice_change_event", None)
        if self._loop and self._loop.is_running() and event is not None:
            self._loop.call_soon_threadsafe(event.set)
        return True

    async def _run_live_liveness_watch(self) -> None:
        """Rouvre la session quand Gemini Live se tait sans raccrocher.

        Symptôme vécu : après une réponse d'outil (ou une phrase), plus aucun
        message serveur — ni transcription, ni audio, ni erreur. Le WebSocket
        reste « ouvert », donc aucune reconnexion ne part ; l'utilisateur parle
        et écrit dans le vide, sans la moindre trace dans le journal. Ici, une
        demande restée sans aucune réaction pendant ``_LIVE_REPLY_TIMEOUT_S``
        (hors outil encore en cours) déclenche la même reconnexion discrète
        que l'élargissement d'outils : poignée de reprise conservée, dernière
        demande renvoyée par ``_resend_unanswered``.
        """
        while True:
            await asyncio.sleep(2.0)
            since = float(getattr(self, "_awaiting_server_since", 0.0) or 0.0)
            if not since:
                continue
            if getattr(self, "_active_tool_tasks", None):
                continue
            if getattr(self, "_is_speaking", False):
                continue
            waited = time.monotonic() - since
            if waited < _LIVE_REPLY_TIMEOUT_S:
                continue
            self._awaiting_server_since = 0.0
            text = str(getattr(self, "_live_user_text", "") or "")
            if text:
                self._unanswered.append(text)
                del self._unanswered[:-2]
                self._live_user_text = ""
            self.ui.write_log(
                f"SYS : Gemini Live sans réaction depuis {waited:.0f} s — reconnexion, "
                "ta dernière demande sera renvoyée."
            )
            print(f"[JARVIS] ⚠️ Session Live muette ({waited:.0f}s) — reconnexion.")
            if hasattr(self, "reset_audio_and_turn_state"):
                self.reset_audio_and_turn_state("live_unresponsive")
            self._live_unresponsive_reconnect = True
            self._toolkit_reconnect_requested = True
            self._voice_reconnect_requested = True
            event = self._voice_change_event
            if event is not None:
                event.set()
            return

    async def _watch_live_voice_change(self) -> None:
        """Termine le TaskGroup quand un réglage de session immuable change."""
        event = self._voice_change_event
        if event is None:
            return
        await event.wait()
        raise RuntimeError("Gemini Live voice changed")

    @property
    def prosody_analyzer(self) -> ProsodyAnalyzer:
        """Instance de ProsodyAnalyzer liée à la session."""
        analyzer = getattr(self, "_prosody_analyzer", None)
        if analyzer is None:
            analyzer = get_prosody_analyzer()
            self._prosody_analyzer = analyzer
        return analyzer

    def get_current_mood(self) -> str:
        """Retourne l'état émotionnel actuel qualifié ('calme', 'agacé/pressé', 'chuchoté/nuit', 'fatigué', 'neutre')."""
        return self.prosody_analyzer.current_mood

    def get_prosody_context_instruction(self, state: Optional[str] = None) -> str:
        """Génère la directive contextuelle à injecter dynamiquement pour le WebSocket Gemini Live."""
        analyzer = self.prosody_analyzer
        if state is None:
            hour = datetime.now().hour
            is_night = (hour >= 21 or hour < 7)
            state = analyzer.current_mood
            if state == "neutre" and is_night:
                state = "chuchoté/nuit"
        return analyzer.get_gemini_instruction(state)

    def get_tts_modulation(self, state: Optional[str] = None) -> TTSModulation:
        """Retourne la modulation acoustique (vitesse, volume, pitch) pour le TTS local."""
        return self.prosody_analyzer.get_tts_modulation(state)

    def apply_prosody_to_tts_config(self, config: dict, state: Optional[str] = None) -> dict:
        """Modifie les paramètres TTS locaux (vitesse, volume, pitch) selon l'humeur acoustique."""
        return self.prosody_analyzer.apply_tts_modulation(config, state)

    def analyze_user_audio(
        self,
        samples: Any,
        sample_rate: int = 16000,
        *,
        is_night: Optional[bool] = None,
    ) -> MoodClassification:
        """Analyse acoustique temps réel des trames audio de l'utilisateur."""
        analyzer = self.prosody_analyzer
        res = analyzer.analyze(samples, sample_rate, is_night=is_night)
        prev = getattr(self, "_prosody_mood", "")
        self._prosody_mood = res.state
        if prev and prev != res.state:
            ui = getattr(self, "ui", None)
            if ui and hasattr(ui, "write_log"):
                ui.write_log(f"SYS : Humeur vocale détectée — {res.state} (conf: {res.confidence:.0%}).")
        return res

    async def inject_dynamic_prosody(self, state: Optional[str] = None) -> bool:
        """Injecte dynamiquement l'instruction de contexte prosodique dans la session Gemini Live active."""
        instruction = self.get_prosody_context_instruction(state)
        session = getattr(self, "session", None)
        if not session or not instruction:
            return False
        try:
            directive = f"[DIRECTIVE LOCALE NON PRONONÇABLE] {instruction}"
            await session.send_realtime_input(text=directive)
            return True
        except Exception as exc:
            print(f"[JARVIS] ⚠️ Impossible d'injecter la directive prosodique : {exc}")
            return False

    # ── Continuous Vision Engine (Vision continue fluide & streaming) ─────
    @property
    def continuous_vision(self) -> Any:
        """Instance de ContinuousVisionEngine liée à la session."""
        engine = getattr(self, "_continuous_vision_engine", None)
        if engine is None:
            from core.continuous_vision import get_continuous_vision_engine
            ui_cb = None
            ui = getattr(self, "ui", None)
            if ui is not None and hasattr(ui, "set_continuous_vision_state"):
                ui_cb = ui.set_continuous_vision_state
            engine = get_continuous_vision_engine(session_manager=self, ui_callback=ui_cb)
            self._continuous_vision_engine = engine
        return engine

    def start_continuous_vision(self, device: Optional[str] = None) -> str:
        """Active la vision continue temps réel pour observer la scène."""
        return self.continuous_vision.start(device=device)

    def stop_continuous_vision(self) -> str:
        """Désactive la vision continue."""
        return self.continuous_vision.stop()

    def close_all_cameras(self) -> str:
        """« Ferme la caméra » : tout ce qui filme s'arrête, quel que soit le
        chemin qui l'a ouvert (studio caméra, vision continue, veille des
        visages, flux de l'overlay). Répondre « fermée » alors que le studio
        tournait encore trompait l'utilisateur.
        """
        closed: list[str] = []
        try:
            from actions.visual_recognition import stop_watcher, watcher_running
            if watcher_running():
                stop_watcher()
                closed.append("veille des visages")
        except Exception:
            logging.getLogger(__name__).warning("Échec auxiliaire dans close_all_cameras")
        studio = getattr(self, "_camera", None)
        if studio is not None and getattr(studio, "active", False):
            try:
                studio.close()
                closed.append("caméra")
            except Exception as exc:
                print(f"[Caméra] fermeture du studio : {exc}")
        try:
            if getattr(self.continuous_vision, "is_active", False):
                self.continuous_vision.stop()
                closed.append("vision continue")
        except Exception as exc:
            print(f"[Caméra] arrêt de la vision continue : {exc}")
        try:
            self.ui.stop_camera_stream()
        except Exception:
            logging.getLogger(__name__).warning("Échec auxiliaire dans close_all_cameras")
        if not closed:
            return "La caméra est déjà fermée."
        return "Caméra fermée (" + ", ".join(closed) + ")."

    def toggle_continuous_vision(self) -> str:
        """Bascule l'état de la vision continue."""
        return self.continuous_vision.toggle()

    async def send_video_frame(self, frame_bytes: bytes, mime_type: str = "image/webp") -> bool:
        """Transmet une trame vidéo en temps réel au format realtime_input de Gemini Live."""
        session = getattr(self, "session", None)
        conn = getattr(self, "_conn", None)
        if not session or (conn and not getattr(conn, "is_connected", True)):
            return False
        try:
            out_q = getattr(self, "out_queue", None)
            if out_q is not None:
                await out_q.put({
                    "activity": "video",
                    "data": frame_bytes,
                    "mime_type": mime_type,
                })
                return True
            else:
                await session.send_realtime_input(
                    video={"data": frame_bytes, "mime_type": mime_type}
                )
                return True
        except Exception as exc:
            print(f"[JARVIS] ❌ Échec envoi trame vidéo continue : {exc}")
            return False

    def check_continuous_vision_voice_trigger(self, transcript: str) -> Optional[str]:
        """Détecte les commandes vocales d'activation/désactivation de la vision continue."""
        from core.continuous_vision import is_vision_activation_phrase, is_vision_deactivation_phrase
        if is_vision_activation_phrase(transcript):
            return self.start_continuous_vision()
        elif is_vision_deactivation_phrase(transcript):
            return self.close_all_cameras()
        return None

    # ── Persona Manager (Modes Métiers & Personas Dynamiques) ─────────────
    def check_persona_voice_trigger(self, transcript: str) -> Optional[Any]:
        """Détecte les commandes vocales de commutation de mode métier / persona."""
        if not transcript:
            return None
        # Citer ou comparer des modes ne doit jamais basculer de persona. Une
        # transition exige une formulation impérative explicite (ou la commande
        # courte entière « mode majordome »), jamais une sous-chaîne au milieu
        # d'une question telle que « quels modes as-tu ? ».
        folded = " ".join(re.findall(r"[a-z0-9]+", transcript.casefold()))
        direct = bool(re.fullmatch(r"(?:mode|persona|profil)\s+.+", folded))
        imperative = bool(re.search(
            r"\b(?:passe|passer|bascule|basculer|active|activer|mets|mettre|"
            r"mettez|enclenche|engage|reviens|remets)\b", folded,
        ))
        if not (direct or imperative):
            return None
        mgr = getattr(self, "persona_manager", None)
        if mgr is None:
            try:
                from core.persona_manager import get_persona_manager
                self.persona_manager = get_persona_manager()
                mgr = self.persona_manager
            except Exception:
                return None
        return mgr.detect_trigger(transcript)

    async def switch_persona(self, target: Any, full_in: str = "") -> bool:
        """Commute dynamiquement le mode métier / persona sans couper le WebSocket Gemini Live."""
        mgr = getattr(self, "persona_manager", None)
        if mgr is None:
            try:
                from core.persona_manager import get_persona_manager
                self.persona_manager = get_persona_manager()
                mgr = self.persona_manager
            except Exception as exc:
                print(f"[JARVIS] ⚠️ PersonaManager indisponible : {exc}")
                return False
        res = await mgr.async_switch_persona(target, session_manager=self, ui=getattr(self, "ui", None))
        return res.success

    def detect_contextual_persona(self, text: str) -> Optional[str]:
        """Retourne un persona pédagogique seulement pour une demande explicite."""
        return detect_contextual_persona(text)

    async def switch_persona_then_submit(self, target: str, text: str) -> bool:
        """Injecte la posture avant d'envoyer la demande texte au modèle."""
        if not await self.switch_persona(target, full_in=text):
            return False
        submit = getattr(self, "_submit_text_with_screen", None)
        if callable(submit):
            return bool(await submit(text))
        return await self._submit_text_turn(text)

    # ── Thought Streamer (Thinking Tokens & VUI Multimodale) ──────────────
    @property
    def thought_streamer(self) -> Any:
        """Instance de ThoughtStreamer liée à la session et à l'UI."""
        streamer = getattr(self, "_thought_streamer", None)
        if streamer is None:
            from core.thought_streamer import get_thought_streamer
            ui_cb = None
            ui = getattr(self, "ui", None)
            if ui is not None and hasattr(ui, "show_thought"):
                ui_cb = ui.show_thought
            streamer = get_thought_streamer(session_manager=self, ui_callback=ui_cb)
            self._thought_streamer = streamer
        return streamer

    def feed_thought_token(self, token: str, source: str = "gemini") -> None:
        """Transmet un token de réflexion au gestionnaire de pensée."""
        self.thought_streamer.feed_thought_token(token, source=source)

    def feed_tool_start(self, tool_name: str, args: Any = None) -> None:
        """Signale le début d'exécution d'un outil pour retour visuel et audio."""
        self.thought_streamer.feed_tool_start(tool_name, args)

    def feed_tool_end(self, tool_name: str, result: Any = None) -> None:
        """Signale la fin d'exécution d'un outil."""
        self.thought_streamer.feed_tool_end(tool_name, result)

    def speak(self, text: str, *, local_tts: bool = False, tts_player: Any = None):
        """Fait énoncer une réponse par Jarvis.

        Si local_tts est activé ou si un tts_player est fourni, utilise la synthèse
        locale avec vitesse et volume modulés selon l'humeur détectée.
        Sinon, soumet le tour à Gemini Live via WebSocket.
        """
        player = tts_player or (getattr(self, "tts_player", None) if local_tts else None)
        if player is not None and hasattr(player, "speak"):
            mod = self.get_tts_modulation()
            if hasattr(player, "_cfg"):
                cfg = player._cfg
                if hasattr(cfg, "rate"):
                    cfg.rate = mod.tts_rate
                if hasattr(cfg, "volume"):
                    cfg.volume = mod.tts_volume
                if hasattr(cfg, "speed"):
                    cfg.speed = mod.speed_factor
            player.speak(text)
            return

        if not self._loop or not self.session:
            return
        # Même précaution que pour une commande texte : on ne laisse pas un
        # tour vocal ouvert se mélanger au tour qu'on injecte ici.
        self._loop.call_soon_threadsafe(self._activity_end)
        asyncio.run_coroutine_threadsafe(
            self._submit_text_turn(text),
            self._loop
        )

    async def _submit_text_turn(
        self,
        text: str,
        timeout_s: float = 90.0,
        pending_timeout_s: float = _STALE_AUDIO_TURN_S,
    ) -> bool:
        """Soumet un tour texte sans le superposer à un autre tour modèle.

        Gemini Live accepte plusieurs messages temps réel d'un même énoncé,
        mais pas deux ``turn_complete`` concurrents. Au démarrage, le briefing,
        une salutation, un rappel ou une commande du dashboard pouvaient partir
        ensemble et le serveur fermait alors la connexion avec le code 1007.
        Le verrou reste pris jusqu'au ``turn_complete`` reçu par la boucle de
        lecture, pas seulement jusqu'à l'écriture du WebSocket.
        """
        text = strip_emoji(str(text or "")).strip()
        if not text:
            return False
        if not self.session:
            # Résultat d'une tâche longue arrivé pendant une reconnexion : on
            # le garde, `_resend_unanswered` le livrera sur la session suivante.
            self._defer_turn(text)
            return False
        self._maybe_show_clock_particles(text)
        deferred = [
            *[str(item or "").strip() for item in getattr(self, "_deferred_turns", ())],
            str(getattr(self, "_deferred_voice_note", "") or "").strip(),
            str(getattr(self, "_deferred_context", "") or "").strip(),
        ]
        deferred = [item for item in deferred if item]
        if deferred:
            text = "\n\n".join((*deferred, text))
            self._deferred_turns = []
            self._deferred_voice_note = ""
            self._deferred_context = ""
        # Le verrou est créé une seule fois (constructeur) : il sérialise les
        # tours texte, pas une connexion WebSocket. Le recréer à chaque
        # reconnexion laissait une tâche de fond (vidéo, image, recherche)
        # attendre un verrou que plus personne ne relâchait, ou relâcher un
        # verrou qu'elle ne tenait plus (« Lock is not acquired »).
        lock = getattr(self, "_turn_submit_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._turn_submit_lock = lock
        session_before = self.session
        async with lock:
            session = self.session
            if session is None:
                self._defer_turn(text)
                return False
            if session is not session_before:
                # La connexion a été remplacée pendant l'attente du verrou : la
                # nouvelle session est saine, on lui livre le résultat plutôt
                # que de le perdre — c'est précisément ce qu'attend une tâche
                # de deux minutes qui a survécu à une coupure.
                print("[JARVIS] Tour texte livré sur la session reconnectée.")
            # Le tour en vol est annulable dès maintenant par « stop » ou par
            # `reset_audio_and_turn_state` : c'est la coroutine annulée qui
            # relâche le verrou en sortant du `async with`, jamais un tiers.
            self._active_turn_task = asyncio.current_task()
            # Un tour micro ouvert au moment du briefing/rappel est de l'écho
            # naissant : l'annuler évite que Transcribe attende une phrase
            # fantôme et fasse tomber la session pendant que la voix sort.
            if getattr(self, "_activity_open", False):
                try:
                    self._activity_cancel()
                except Exception:
                    logging.getLogger(__name__).warning("Échec auxiliaire dans _submit_text_turn")
            trace = getattr(self, "_live_send_trace", None)
            if trace is not None:
                trace.append((time.monotonic(), "text_turn", text[:80]))
            done = self._turn_done_event
            # Un activity_end signifie « traite maintenant cette phrase », pas
            # « le tour est déjà terminé ». Attendre sa réponse avant d'envoyer
            # un tour texte évite un second code 1007 (audio puis texte
            # concurrents), notamment lorsqu'une commande dashboard arrive
            # juste après que l'utilisateur a parlé.
            if getattr(self, "_audio_turn_pending", False) and done is not None:
                try:
                    await asyncio.wait_for(done.wait(), timeout=pending_timeout_s)
                except asyncio.CancelledError:
                    self._active_turn_task = None
                    print("[JARVIS] ⚠️ Tour texte annulé avant envoi.")
                    return False
                except asyncio.TimeoutError:
                    # Gemini ne renvoie pas toujours `turn_complete` pour un
                    # faux départ VAD, une phrase coupée au moment du mute ou
                    # un tour audio vide. L'ancien code abandonnait alors la
                    # commande texte : elle était annoncée « différée » sans
                    # jamais être mise en file. Après ce délai sans fin de
                    # tour, le texte devient prioritaire et récupère la session.
                    self._audio_turn_pending = False
                    self._activity_open = False
                    self._clear_interrupted()
                    done.clear()
                    print(
                        "[JARVIS] ⚠️ Tour vocal bloqué abandonné ; "
                        "commande texte envoyée."
                    )
                    ui = getattr(self, "ui", None)
                    if ui is not None and hasattr(ui, "write_log"):
                        ui.write_log(
                            "SYS : tour vocal bloqué récupéré — "
                            "commande texte envoyée."
                        )
            if done is not None:
                done.clear()
            # Les modèles Live 3.1 refusent désormais client_content avec le
            # code 1007. Le texte temps réel clôt lui-même son entrée et
            # déclenche normalement la réponse audio.
            try:
                await session.send_realtime_input(text=text)
            except asyncio.CancelledError:
                self._active_turn_task = None
                print("[JARVIS] ⚠️ Tour texte annulé pendant l'envoi.")
                return False
            self._awaiting_server_since = time.monotonic()
            if done is not None:
                try:
                    await asyncio.wait_for(done.wait(), timeout=timeout_s)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    print("[JARVIS] ⚠️ Tour texte interrompu ou sans confirmation de fin.")
                    # Se retirer avant le reset : il annule le tour en vol, et
                    # ce tour, c'est nous — inutile de nous annuler nous-mêmes.
                    self._active_turn_task = None
                    if hasattr(self, "reset_audio_and_turn_state"):
                        self.reset_audio_and_turn_state("text_turn_timeout_or_cancel")
                    return False
                finally:
                    self._active_turn_task = None
            self._active_turn_task = None
            return True

    def _defer_turn(self, text: str) -> None:
        """Conserve un tour texte qui n'a pas pu partir (session absente).

        Il sera préfixé au prochain tour texte, ou renvoyé tel quel par
        `_resend_unanswered` dès que la connexion est rétablie. On ne garde
        que les derniers : un résultat vieux de plusieurs reconnexions n'a
        plus d'intérêt vocal, il reste de toute façon dans sa carte.
        """
        text = str(text or "").strip()
        if not text:
            return
        pending = list(getattr(self, "_deferred_turns", None) or [])
        if text not in pending:
            pending.append(text)
        self._deferred_turns = pending[-3:]
        ui = getattr(self, "ui", None)
        if ui is not None and hasattr(ui, "write_log"):
            ui.write_log("SYS : résultat conservé — il sera annoncé dès la reconnexion.")

    def speak_error(self, tool_name: str, error: str):
        short = str(error)[:120]
        self.ui.write_log(f"ERR: {tool_name} — {short}")
        try:
            self.ui.show_card(
                "error", f"Échec : {tool_name}",
                f"{short}\n\nDis-le-moi autrement ou réessaie — je n'ai pas pu terminer cette action.",
            )
        except Exception:
            logging.getLogger(__name__).warning("Échec auxiliaire dans speak_error")
        self.speak(f"{user_address()}, l'outil {tool_name} a rencontré une erreur. {short}")

    # ── Cerveau externe ──────────────────────────────────────────────────
    def _relay_declarations(self) -> list[dict]:
        """Outils réels de la machine, tels que le cerveau externe les verra."""
        base = list(getattr(self, "_tool_declarations", None) or TOOL_DECLARATIONS)
        try:
            base += list(self._plugins.declarations())
        except Exception as exc:
            print(f"[Relais] extensions ignorées : {exc}")
        return base

    def _live_declarations(self) -> list[dict]:
        """Outils offerts à Gemini Live.

        En mode relais il n'en garde qu'un : consulter le cerveau. Lui laisser
        les autres, c'est l'inviter à agir lui-même — exactement ce que
        l'utilisateur a demandé d'éviter en choisissant un autre fournisseur.
        """
        if getattr(self, "_relay_enabled", False):
            return [CONSULT_BRAIN_DECLARATION]
        from core.tool_dispatcher import announce_before_call
        return announce_before_call(tool_packs.select_declarations(
            self._relay_declarations(),
            getattr(self, "_active_tool_packs", frozenset()),
        ))

    def _relay_base_prompt(self) -> str:
        """Socle de prompt hérité par le cerveau externe."""
        base = str(getattr(self, "_relay_system_base", "") or "").strip()
        if base:
            return base
        try:
            return _load_system_prompt()
        except Exception:
            return ""

    def _build_config(self) -> types.LiveConnectConfig:
        from datetime import datetime

        # Load customization from config
        _cfg: dict = {}
        try:
            _cfg = json.loads(open(API_CONFIG_PATH, encoding="utf-8").read())
            self._asst_name = (_cfg.get("assistant_name") or "ANO-GPT").strip()
            _user_name = (_cfg.get("user_name") or "").strip()
            _user_lang = (_cfg.get("conversation_language") or _cfg.get("user_lang") or "fr").strip()
        except Exception:
            self._asst_name = "ANO-GPT"
            _user_name = ""
            _user_lang = "fr"
        self._conversation_language = normalise_conversation_language(_user_lang).code

        # Le mode est volontairement relu à chaque connexion : cette valeur
        # persiste entre sessions mais peut être changée à chaud par commande.
        mode_spec = MODE_SPECS[active_mode()]
        voice_settings = voice_settings_for_mode(_voice_engine_settings())
        if voice_settings.get("voice_provider", "gemini") == "gemini":
            self._live_voice = normalise_live_voice(voice_settings.get("live_voice"))

        memory     = load_memory()
        mem_str    = format_memory_for_prompt(memory)
        # Décrire un outil que la session n'a pas, c'est lui apprendre à
        # promettre ce qu'elle ne peut pas tenir : le guide suit les paquets.
        sys_prompt = tool_packs.filter_prompt(
            _load_system_prompt(),
            getattr(self, "_active_tool_packs", frozenset()),
        )

        # Mémoire longue durée : profil, faits récents et résumés des dernières
        # conversations. Le reste (les milliers de faits anciens) n'entre pas
        # ici — il est rappelé en cours de route, quand la phrase le touche.
        try:
            recall_str = memory_store.session_block()
        except Exception as exc:
            print(f"[Mémoire] socle indisponible : {exc}")
            recall_str = ""
        # Nouvelle session, nouveau contexte : ce qui avait été rappelé dans la
        # précédente n'y est plus.
        self._recalled_ids.clear()
        self._prosody_mode = ""

        # Contexte ambiant complet, une fois, pour que la toute première phrase
        # de la session tombe déjà sur un assistant qui sait où il est. Ensuite,
        # seuls les changements repartent, tour par tour.
        try:
            context_probe.clear_cache()
            ambient_str, self._ambient_state = context_probe.ambient_delta(None)
            self._ambient_fields = None
        except Exception as exc:
            print(f"[Contexte] socle indisponible : {exc}")
            ambient_str = ""

        # Le cerveau est relu à chaque connexion : changer de fournisseur dans
        # l'interface prend effet à la reconnexion suivante, sans redémarrage.
        try:
            from core.llm_client import main_brain_label, relay_active
            self._relay_enabled = relay_active()
            _brain_label = main_brain_label()
        except Exception as exc:
            print(f"[Relais] cerveau indéterminé, Gemini conservé : {exc}")
            self._relay_enabled = False
            _brain_label = "Google Gemini Live"

        now      = datetime.now()
        time_str = now.strftime("%A %d %B %Y — %H:%M")
        # Dynamic live system context
        import shutil as _sh
        _ollama_ok = bool(_sh.which("ollama"))
        _hypr_ok   = bool(_sh.which("hyprctl"))
        _wayland   = bool(__import__("os").environ.get("WAYLAND_DISPLAY"))
        time_ctx = (
            f"[DATE & HEURE EN TEMPS RÉEL]\n"
            f"Maintenant: {time_str}\n"
            f"Utilise ceci pour calculer les heures exactes des rappels.\n\n"
            f"[ÉTAT SYSTÈME LIVE]\n"
            f"Ollama local: {'actif (qwen2.5:0.5b disponible sur localhost:11434)' if _ollama_ok else 'non disponible'}\n"
            f"Hyprland: {'actif' if _hypr_ok else 'non disponible'}\n"
            f"Wayland: {'oui' if _wayland else 'non'}\n\n"
            f"[MOTEUR CONVERSATIONNEL ACTIF]\n"
            f"Voix et écoute: Google Gemini Live ({self._live_models.current})\n"
            f"Cerveau: {_brain_label}\n"
            f"Si l'utilisateur demande le fournisseur ou le modèle, indique ces "
            f"valeurs. Ollama est un composant local optionnel, pas le moteur de "
            f"cette conversation.\n\n"
        )

        # Identity & Persona injection — overrides any hardcoded name in prompt.txt
        persona = getattr(self, "_current_persona", None)
        if persona is None:
            try:
                from core.persona_manager import get_persona_manager
                self.persona_manager = get_persona_manager()
                self._current_persona = self.persona_manager.current_persona
                persona = self._current_persona
            except Exception:
                persona = None

        # Le mode de personnalité possède l'adresse utilisateur. Il ne doit
        # jamais être contredit par une persona métier ou la configuration UI.
        _user_name = mode_spec.user_address
        self._user_name = _user_name

        _addr = identity_address_line(mode_spec)
        identity_ctx = (
            f"[IDENTITY]\n"
            f"Your name is {self._asst_name}. "
            f"Always refer to yourself as {self._asst_name}.\n"
            f"{_addr}\n"
            f"LANGUAGE: The active conversation language is {normalise_conversation_language(self._conversation_language).label_native} "
            f"({self._conversation_language}). Respond ONLY in that language, with no mixing. "
            f"French is the default only until the user explicitly asks to switch languages.\n"
            f"[MODES DE TON DISPONIBLES] ANO-GPT possède exactement quatre modes : "
            f"Normal ; Astro — le pote ; Unfiltered & Coquin ; Majeur d'homme "
            f"(aussi appelé Majordome). Si l'utilisateur demande quels modes existent, "
            f"énumère-les fidèlement et ne prétends jamais ne pas avoir de modes. "
            f"Une simple question ou énumération ne les active pas : seul un ordre explicite le fait.\n\n"
            f"VOICE INPUT SAFETY: Assume microphone speech is in the active conversation language. Ignore wind, rain, music, "
            f"television, distant speech and unintelligible sounds. Never invent words or switch "
            f"language to explain ambiguous audio. If no clear request was spoken directly to you, "
            f"stay silent. If speech is probably directed to you but uncertain, ask briefly in "
            f"the active language to repeat. NEVER call a tool from uncertain or noisy audio.\n\n"
        )

        parts = [time_ctx, identity_ctx]
        if ambient_str:
            parts.append(
                ambient_str
                + "\n\nCes valeurs décrivent la machine au moment où la session "
                  "s'ouvre ; elles sont réactualisées en cours de conversation "
                  "quand quelque chose change. Sers-t'en pour comprendre « ça », "
                  "« cette fenêtre », « ce fichier », « la suite » — sans jamais "
                  "les réciter ni annoncer que tu les as.\n"
            )
        if mem_str:
            parts.append(mem_str)
        if recall_str:
            parts.append(recall_str)

        # Injection de la posture vocale et du contexte prosodique
        prosody_ctx = self.get_prosody_context_instruction()
        if prosody_ctx:
            parts.append(f"[POSTURE VOCALE & PROSODIE INITIALE]\n{prosody_ctx}\n")

        # Les quatre modes de personnalité sont l'unique autorité sur le ton
        # et l'adresse. La persona historique « Majordome » était chargée par
        # défaut et réinjectait « Monsieur » même lorsqu'Astro était actif.
        # Les capacités/outils restent disponibles via le prompt socle ; seule
        # cette identité concurrente est volontairement écartée.
        # Le socle (outils, langue, sécurité) d'abord ; le mode de ton ensuite
        # pour qu'il écrase le style JARVIS sans toucher aux règles d'exécution.
        parts.append(sys_prompt)

        # Ces deux personas sont déclenchés par une intention pédagogique
        # explicite. Contrairement aux personas historiques, ils doivent aussi
        # survivre à une reconnexion (par exemple après un changement de langue).
        if persona is not None and persona.id in {
            "job_interview_coach", "english_learning_coach",
        }:
            parts.append(persona.format_prompt_block())

        parts.append(mode_spec.prompt)
        # Les descriptions historiques de quelques outils sont en anglais,
        # mais elles ne doivent jamais faire dévier la réponse entendue. Cette
        # consigne arrive après tout le socle et après le mode : elle prévaut
        # aussi pour les confirmations et les erreurs d'outils.
        if self._conversation_language.startswith("fr"):
            parts.append(
                "[LANGUE DE SORTIE — RÈGLE ABSOLUE]\n"
                "Réponds exclusivement en français, y compris après un appel d'outil, "
                "une réussite, une attente ou une erreur. Ne traduis pas les termes "
                "techniques indispensables, mais ne produis aucune phrase anglaise."
            )

        # Le cerveau externe hérite de tout ce socle : identité, mémoire,
        # contexte machine, règles d'outils. Sans lui, il répondrait comme un
        # chatbot générique qui ne sait ni où il est ni à qui il parle.
        self._relay_system_base = "\n".join(parts)

        if self._relay_enabled:
            parts.append(
                "[CERVEAU EXTERNE ACTIF — RÈGLE ABSOLUE]\n"
                f"L'utilisateur a choisi {_brain_label} comme cerveau. Tu n'es "
                "plus celui qui réfléchit : tu es son oreille et sa voix.\n"
                "Pour TOUTE demande de l'utilisateur — question, ordre, action, "
                "bavardage — appelle l'outil `consult_brain` en lui passant la "
                "demande mot pour mot dans `question`. N'appelle aucun autre "
                "outil toi-même : le cerveau a les mêmes et s'en sert lui-même.\n"
                "Quand `consult_brain` répond, prononce son texte tel quel, sans "
                "rien ajouter, retirer, résumer ni commenter. Ne dis jamais que "
                "tu as consulté quoi que ce soit.\n"
                "Seules exceptions, où tu réponds toi-même sans outil : un bruit "
                "ou une phrase qui ne t'était pas adressée (reste silencieux), "
                "et une interruption où l'utilisateur te demande de te taire.\n"
            )

        target_temp = {
            PersonalityMode.NORMAL: 0.38,
            # Assez de liberté pour varier vraiment les réactions d'Astro,
            # sans monter au point d'inventer des faits ou des actions.
            PersonalityMode.ASTRO: 0.93,
            PersonalityMode.COQUIN: 0.74,
            PersonalityMode.MAJEUR: 0.22,
        }[mode_spec.key]
        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            # Faible température : une commande vocale demande de la fidélité,
            # pas de la créativité. Les indices de langue empêchent un bruit
            # ambigu d'être décodé arbitrairement en turc, thaï, anglais, etc.
            temperature=target_temp,
            output_audio_transcription=build_output_transcription_config(self._conversation_language),
            # Les noms propres sont les mots que le STT déforme le plus. Les
            # fournir comme adaptation acoustique améliore leur décodage sans
            # demander au modèle de réécrire ce que l'utilisateur a dit.
            input_audio_transcription=build_input_transcription_config(
                _user_lang,
                extra_phrases=(self._asst_name, _user_name),
            ),
            system_instruction="\n".join(parts),
            tools=[{"function_declarations": (
                self._live_declarations()
            )}],
            # La poignée rendue par le serveur au tour précédent : renvoyée
            # ici, Gemini reprend la conversation au lieu d'en ouvrir une
            # neuve. Sans elle, la configuration demandait une reprise que
            # personne ne recueillait — chaque coupure repartait amnésique.
            session_resumption=types.SessionResumptionConfig(
                handle=self._conn.resume_handle()
            ),
            # Empêche une conversation longue de mourir lorsque sa fenêtre de
            # contexte se remplit. Le serveur résume progressivement la partie
            # ancienne tout en conservant la session et ses outils actifs.
            context_window_compression=(
                types.ContextWindowCompressionConfig(
                    sliding_window=types.SlidingWindow(),
                ) if self._context_compression_enabled else None
            ),
            # Pipeline Mark-LII : Gemini Live reçoit le PCM brut et son VAD
            # serveur gère les bornes de tour. Aucun activity_start/end client.
            # Le silence de fin de tour est raccourci : c'est lui qui sépare
            # le dernier mot de l'utilisateur du début de la réponse.
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    silence_duration_ms=live_end_silence_ms(_cfg),
                ),
            ),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=self._live_voice
                    )
                )
            ),
        )
    async def _send_realtime(self):
        """Pont direct Mark-LII : file PCM → Gemini Live.

        Les marqueurs VAD locaux ne pilotent pas Gemini Live (son VAD serveur
        borne les tours) ; ils servent, avec le même PCM, aux sous-titres
        instantanés (`core.live_captions`), sans jamais retarder cet envoi.
        Les images temps réel conservent leur canal dédié.
        """
        from core.live_captions import LiveCaptions
        captions = LiveCaptions(self)
        self._live_captions = captions
        captions.start()
        try:
            while True:
                msg = await self.out_queue.get()
                marker = msg.get("activity") if isinstance(msg, dict) else None
                if marker != "video":
                    # La file peut contenir du PCM capturé avant une réponse,
                    # une interruption ou une coupure réseau. Ne pas le rejouer.
                    epoch = getattr(self, "_speech_output_epoch", 0)
                    captured_at = msg.get("_captured_at")
                    stale = (isinstance(captured_at, (int, float))
                             and time.monotonic() - captured_at > 1.0)
                    muted = (msg.get("_audio_source", "pc") != "phone"
                             and getattr(getattr(self, "ui", None), "muted", False))
                    if (stale or muted or getattr(self, "_is_speaking", False)
                            or getattr(self, "_interrupted", False)
                            or msg.get("_audio_epoch", epoch) != epoch):
                        continue
                    captions.push(msg)
                try:
                    if marker == "video":
                        await self.session.send_realtime_input(
                            video={
                                "data": msg["data"],
                                "mime_type": msg.get("mime_type", "image/webp"),
                            }
                        )
                    elif not marker:
                        await self.session.send_realtime_input(
                            audio={
                                "data": msg["data"],
                                "mime_type": msg["mime_type"],
                            }
                        )
                except Exception as exc:
                    print(f"[JARVIS] ❌ Envoi direct Mark-LII refusé : {exc}")
                    raise
        finally:
            self._live_captions = None
            await captions.close()

    async def _receive_audio(self):
        print("[JARVIS] 👂 Recv started")
        out_buf, in_buf = [], []
        _speaking_started = False
        _user_started = False
        from core.elevenlabs_voice import speak_live_turn
        voice_settings = voice_settings_for_mode(_voice_engine_settings())
        use_elevenlabs = voice_settings.get("voice_provider", "gemini") == "elevenlabs"
        transcript_guard = TranscriptGuard()
        transcript_assembler = TranscriptAssembler()

        try:
            while True:
                async for response in self.session.receive():
                    self._last_server_message_at = time.monotonic()
                    if response.server_content is not None or response.tool_call is not None:
                        self._awaiting_server_since = 0.0

                    # Reprise : le serveur renouvelle sa poignée en cours de
                    # route. C'est le seul moment où on peut la saisir.
                    update = getattr(response, "session_resumption_update", None)
                    if update is not None and getattr(update, "resumable", False):
                        self._conn.on_handle(getattr(update, "new_handle", None))

                    # Gemini limite la durée d'une connexion et prévient avant
                    # de raccrocher. Prévenu, on rouvre tout de suite et sans
                    # rien dire : ce n'est pas une panne.
                    if getattr(response, "go_away", None) is not None:
                        left = getattr(response.go_away, "time_left", None)
                        print(f"[JARVIS] Gemini annonce la fin de connexion (reste {left}).")
                        self._conn.on_go_away()

                    audio_data = _live_audio_data(response)
                    if audio_data:
                        if not self._model_turn_active:
                            # Le modèle recommence à parler : l'interruption
                            # précédente est close, quoi qu'il soit advenu de
                            # son turn_complete.
                            self._clear_interrupted()
                        self._model_turn_active = True
                        bus = getattr(self, "_event_bus", None)
                        if bus is not None:
                            bus.publish_sync(ModelSpeechDeltaEvent(
                                audio_chunk=audio_data, is_final=False,
                            ))
                        self.thought_streamer.on_speaking_start()
                        # Ce tour a été jugé « bruit » par le garde-fou local :
                        # sa réponse est coupée dès le premier bloc audio, avant
                        # qu'un seul son ne sorte des enceintes. L'interruption
                        # Dès que Gemini commence à répondre, la demande qui a
                        # déclenché ce tour n'est plus « sans réponse ».
                        # Attendre exclusivement `turn_complete` laissait une
                        # ancienne phrase dans la file de reprise lorsqu'une
                        # reconnexion technique survenait au milieu de la
                        # réponse ; elle pouvait alors écraser le fil courant.
                        self._live_user_text = ""
                        # tentée au moment du rejet arrivait souvent trop tôt —
                        # le tour du modèle n'avait pas encore commencé.
                        if self._noise_turn:
                            self._interrupted = True
                        if self.discard_model_audio():
                            pass  # discard: tour interrompu
                        else:
                            if self._turn_done_event and self._turn_done_event.is_set():
                                self._turn_done_event.clear()
                            if not self._audio_turn_active:
                                self._reset_speech_sync()
                                self._audio_turn_active = True
                            # Tranches de 20 ms : le lecteur peut interrompre
                            # entre les blocs, avec une réserve matérielle de 60 ms.
                            _audio_data = b"" if use_elevenlabs else audio_data
                            _SLICE = int(
                                RECEIVE_SAMPLE_RATE * CHANNELS * 2
                                * (_OUTPUT_SLICE_MS / 1000.0)
                            )
                            for _i in range(0, len(_audio_data), _SLICE):
                                if self._interrupted:
                                    break
                                chunk = _audio_data[_i : _i + _SLICE]
                                # Appliquer une vraie contre-pression au lieu de
                                # laisser une file illimitée créer plusieurs
                                # secondes de retard, ou de faire tomber toute
                                # la session sur QueueFull.
                                await self.audio_in_queue.put(chunk)
                                self._audio_enqueued_sec += len(chunk) / (RECEIVE_SAMPLE_RATE * CHANNELS * 2)

                    if response.server_content:
                        sc = response.server_content

                        # Affichage progressif de ce que l'utilisateur dit.
                        # Cette hypothèse Live reste strictement visuelle : la
                        # transcription finale est la seule qui alimente le
                        # contexte, les outils ou les actions de l'assistant.
                        interim = getattr(sc, "interim_input_transcription", None)
                        interim_text = _clean_transcript(getattr(interim, "text", ""))
                        if interim_text and getattr(self, "_activity_open", False):
                            self.ui.set_user_transcript(interim_text)

                        # Extraction des Thinking Tokens (Gemini 2.0 Flash Thinking)
                        if getattr(sc, "model_turn", None):
                            for part in getattr(sc.model_turn, "parts", []):
                                if getattr(part, "thought", False):
                                    t_txt = getattr(part, "text", "")
                                    if t_txt:
                                        self.thought_streamer.feed_thought_token(t_txt, source="gemini_thinking")

                        # Gemini signale qu'une nouvelle activité a coupé sa
                        # génération. Ce n'est pas un ordre utilisateur : seuls
                        # Échap, le bouton Arrêter, ou un mot-clé local (ano /
                        # stop / arrête-toi / écoute) ont le droit d'interrompre.
                        # Un « interrupted » serveur issu d'un bruit ou d'une
                        # hallucination STT est ignoré.
                        if getattr(sc, "interrupted", False):
                            # Le serveur a lui-même coupé le tour précédent :
                            # ce qui suit appartient à un nouveau tour.
                            self._end_discarded_turn()
                            if self._interrupted:
                                # L'utilisateur avait cliqué « Interrompre »
                                # puis reparlé : le serveur confirme que le
                                # tour coupé est fini. Sans cette remise à
                                # zéro, `_model_turn_active` restait vrai (pas
                                # de turn_complete pour un tour interrompu) et
                                # la réponse suivante était jetée : l'orbe
                                # était vert, l'assistant muet.
                                self._model_turn_active = False
                                self._is_thinking = False
                                self._audio_turn_active = False
                                self._clear_interrupted()
                                out_buf = []
                            else:
                                print("[STT] activité serveur ignorée — mot-clé d'arrêt requis")

                        if (not self.discard_model_audio()) and sc.output_transcription and sc.output_transcription.text:
                            self.thought_streamer.on_speaking_start()
                            if not self._model_turn_active:
                                self._clear_interrupted()
                            self._model_turn_active = True
                            txt = _clean_transcript(sc.output_transcription.text)
                            # Avoid duplicate messages from Gemini API
                            if txt and (not out_buf or out_buf[-1] != txt):
                                out_buf.append(txt)
                                self._maybe_show_clock_particles(txt)
                                bus = getattr(self, "_event_bus", None)
                                if bus is not None:
                                    bus.publish_sync(ModelSpeechDeltaEvent(
                                        text=txt, is_final=False,
                                    ))
                                self._queue_spoken_text(txt)
                                _speaking_started = True
                            # Certains tours texte n'ont pas de PCM sortant :
                            # leur transcription de sortie confirme tout aussi
                            # bien que la demande a commencé à être traitée.
                            self._live_user_text = ""

                        # Transcription unique Mark-LII : celle de Gemini Live
                        # est affichée telle qu'elle arrive, sans correcteur,
                        # garde acoustique, Vosk ni seconde passe réseau.
                        if sc.input_transcription and sc.input_transcription.text:
                            txt = _clean_transcript(sc.input_transcription.text)
                            if txt and (not in_buf or in_buf[-1] != txt):
                                # Les sous-titres instantanés s'effacent : la
                                # transcription de Live prend l'écran.
                                captions = getattr(self, "_live_captions", None)
                                if captions is not None:
                                    captions.live_transcript_seen()
                                # Même accumulation simple que Mark-LII : la
                                # transcription fournie par Live est la seule
                                # source de vérité du tour.
                                in_buf.append(txt)
                                merged = " ".join(in_buf).strip()
                                if self._try_switch_conversation_language(merged):
                                    # Le PCM a pu parvenir au modèle avant la
                                    # transcription : annuler son ancien tour
                                    # garantit que la confirmation partira après
                                    # reconnexion, dans la nouvelle langue.
                                    self.interrupt()
                                    in_buf = []
                                    self._live_user_text = ""
                                    self.ui.set_user_transcript(merged, final=True)
                                    continue
                                auto_persona = self.detect_contextual_persona(merged)
                                if auto_persona and getattr(
                                    getattr(self, "_current_persona", None), "id", None
                                ) != auto_persona:
                                    spawn_logged(
                                        self.switch_persona(auto_persona, full_in=merged),
                                        name="contextual-persona-switch", ui=self.ui,
                                    )
                                self._live_user_text = merged
                                self._last_user_speech = time.monotonic()
                                # Avant que le modèle ne réponde : si la phrase
                                # touche un domaine fermé, la session repart
                                # aussitôt avec les outils qu'il faut.
                                if self._extend_toolkit(merged):
                                    in_buf = []
                                    self.ui.set_user_transcript(merged, final=True)
                                    continue
                                # La veille est une commande locale : elle est
                                # marquée pendant la transcription, puis le
                                # gestionnaire coupe réellement le micro dès
                                # la fin de la réponse en cours.
                                if hasattr(self, "_continuous"):
                                    self._continuous.on_user_transcript(merged)
                                self.ui.set_user_transcript(merged)
                                if not _user_started:
                                    self.ui.write_log(f"[INLINE_START]Vous: {merged}")
                                    _user_started = True

                        # Ancien pipeline de filtrage local, conservé seulement
                        # temporairement comme référence de migration ; il ne
                        # peut plus s'exécuter sur le chemin Mark-LII.
                        if False and sc.input_transcription and sc.input_transcription.text:
                            txt = _clean_transcript(sc.input_transcription.text)
                            if txt:
                                try:
                                    txt = STTCorrector().correct(txt)
                                except Exception:
                                    logging.getLogger(__name__).warning("Échec auxiliaire dans _receive_audio")
                                # Mémoriser aussi les fragments différés. Le
                                # serveur peut envoyer soit une révision
                                # cumulative (« ouvre » → « ouvre Firefox »),
                                # soit de vrais morceaux (« cherche » puis
                                # « demain »). Les jeter couperait des mots.
                                previous = in_buf[0] if in_buf else ""
                                candidate = transcript_assembler.add(txt)
                                evidence_ms = (
                                    self._voice_evidence_ms if self._activity_open
                                    else self._last_voice_evidence_ms
                                )
                                audio_ms = (
                                    max(
                                        0.0,
                                        (time.monotonic() - self._activity_since) * 1000.0
                                        + AudioPreprocessor._ATTACK_MS,
                                    )
                                    if self._activity_open else self._last_voice_audio_ms
                                )
                                assessment = transcript_guard.assess(
                                    candidate,
                                    acoustic_voice_ms=evidence_ms,
                                    audio_duration_ms=audio_ms,
                                    # Gemini livre des révisions incrémentales.
                                    # Tant que le micro parle encore, « de »
                                    # peut devenir « demain » au fragment suivant :
                                    # on attend au lieu d'annuler un vrai tour.
                                    partial=self._activity_open,
                                )
                                if assessment.deferred:
                                    print(
                                        f"[STT] … Fragment différé "
                                        f"({assessment.reason}): {candidate!r}"
                                    )
                                elif not assessment.accepted:
                                    print(
                                        f"[STT] 🛡️ Transcription rejetée "
                                        f"({assessment.reason}): {candidate!r}"
                                    )
                                    # Hallucinations et langues étrangères : on
                                    # coupe le tour pour qu'aucun outil ne parte.
                                    if any(k in assessment.reason for k in (
                                        "hallucination", "alphabet inattendu",
                                        "répétition sans contenu", "répétition artificielle",
                                        "langue étrangère", "chiffres", "anglais isolé",
                                    )):
                                        self._noise_turn = True
                                        transcript_assembler.reset()
                                        in_buf = []
                                        self.interrupt()
                                    else:
                                        # Pour une hésitation isolée ou voix fatiguée, on réinitialise sans couper brutalement
                                        transcript_assembler.reset()
                                        in_buf = []
                                else:
                                    if self._try_switch_personality_mode(candidate):
                                        # Le modèle vient de recevoir le PCM, mais ne doit pas
                                        # répondre avec l'ancien prompt/ancienne voix.
                                        self.interrupt()
                                        transcript_assembler.reset()
                                        in_buf = []
                                        self.ui.set_user_transcript(candidate, final=True)
                                        continue
                                    from core.barge_in import InterruptPhraseDetector
                                    # Même règle que le détecteur local : durant
                                    # la sortie, seul « Ano stop/écoute » est
                                    # une interruption. Un mot nu reçu en retard
                                    # peut être l'écho du TTS ou la fin du tour
                                    # précédent ; il ne doit jamais couper ANO.
                                    kind = InterruptPhraseDetector.classify_strict_interrupt(candidate)
                                    busy = (
                                        self._model_turn_active
                                        or getattr(self, "_is_thinking", False)
                                        or getattr(self, "_is_speaking", False)
                                    )
                                    if busy and kind == "stop":
                                        self.interrupt()
                                        transcript_assembler.reset()
                                        in_buf = []
                                        self.ui.set_user_transcript(candidate, final=True)
                                        continue
                                    if busy and kind == "redirect":
                                        self.interrupt()
                                    # Une vraie phrase annule le soupçon : la
                                    # réponse qui suit est légitime.
                                    self._noise_turn = False
                                    merged = candidate
                                    in_buf = [merged] if merged else []
                                    self._observe_habit_reply(merged or txt)
                                    # Retenu tant que le modèle n'a pas
                                    # répondu : si la connexion tombe ici, la
                                    # phrase sera reposée à la reprise.
                                    self._live_user_text = merged or txt
                                    self._last_user_speech = time.monotonic()
                                    if hasattr(self, "_continuous"):
                                        self._continuous.on_user_transcript(merged or txt)
                                    screen = getattr(self, "_screen_mind", None)
                                    if screen is not None:
                                        try:
                                            if screen.wants_context(merged or txt):
                                                spawn_logged(
                                                    screen.inject_into_live(
                                                        self, merged or txt
                                                    ),
                                                    name="screen-context-inject", ui=self.ui,
                                                )
                                        except Exception:
                                            logging.getLogger(__name__).warning("Échec auxiliaire dans _receive_audio")
                                    try:
                                        from core.prosody import get_prosody_manager
                                        get_prosody_manager().record_user_query(merged or txt)
                                    except Exception:
                                        logging.getLogger(__name__).warning("Échec auxiliaire dans _receive_audio")
                                    # Retour visuel immédiat : la phrase s'écrit dans
                                    # le panneau de gauche pendant qu'elle est dite.
                                    self.ui.set_user_transcript(merged)
                                    if not _user_started:
                                        self.ui.write_log(f"[INLINE_START]Vous: {merged}")
                                        _user_started = True
                                    elif merged.startswith(previous):
                                        delta = merged[len(previous):].strip()
                                        if delta:
                                            self.ui.write_log(f"[INLINE]{delta}")

                        if sc.turn_complete:
                            if use_elevenlabs and out_buf and not self._interrupted:
                                await speak_live_turn(self, "".join(out_buf), voice_settings)
                            bus = getattr(self, "_event_bus", None)
                            if bus is not None:
                                bus.publish_sync(ModelSpeechDeltaEvent(is_final=True))
                            self.thought_streamer.on_turn_complete()
                            self._last_turn_complete_at = time.monotonic()
                            self._model_turn_active = False
                            self._audio_turn_pending = False
                            self._end_discarded_turn()
                            if self._turn_done_event:
                                self._turn_done_event.set()

                            if _user_started:
                                self.ui.write_log("[INLINE_END]")
                                _user_started = False
                            _speaking_started = False

                            # If this turn_complete ends an interrupted response, clear the
                            # flag and skip all further processing for that turn.
                            if self._interrupted:
                                self._clear_interrupted()
                                # La transcription d'entrée est indépendante du
                                # tour modèle et peut déjà contenir la NOUVELLE
                                # phrase. L'effacer ici provoquait exactement le
                                # silence après une interruption rapide.
                                out_buf = []
                                continue

                            full_in = " ".join(in_buf).strip()
                            if full_in:
                                # Tour terminé : la phrase se fige, ce qui montre
                                # qu'elle a bien été prise en compte en entier.
                                self.ui.set_user_transcript(full_in, final=True)

                                # Contrôle vocal ergonomique de la vision continue ("regarde ce que je te montre" / "arrête la caméra")
                                vision_feedback = self.check_continuous_vision_voice_trigger(full_in)
                                if vision_feedback:
                                    self.ui.write_log(f"SYS : {vision_feedback}")

                                controller = getattr(self, "_gesture_controller", None)
                                if controller is not None:
                                    gesture_feedback = controller.handle_voice_command(full_in)
                                    if gesture_feedback:
                                        if controller.enabled and not controller.active:
                                            try:
                                                self.camera.open(self.camera.source)
                                            except Exception as exc:
                                                gesture_feedback += f" Caméra indisponible : {exc}"
                                        self.ui.write_log(f"SYS : {gesture_feedback}")

                                # Commutation vocale dynamique de persona / mode métier ("Jarvis, passe en mode DevOps", etc.)
                                persona_match = self.check_persona_voice_trigger(full_in)
                                if persona_match:
                                    spawn_logged(
                                        self.switch_persona(persona_match, full_in=full_in),
                                        name="persona-voice-switch", ui=self.ui,
                                    )

                                # Pendant un entraînement, conserver la phrase
                                # finale et sa durée acoustique réelle. L'appel
                                # d'outil peut avoir précédé ce marqueur : la
                                # capture est idempotente et enrichit alors la
                                # mesure existante au lieu de la dupliquer.
                                observe_sparring_utterance(
                                    self._tool_session_memory,
                                    full_in,
                                    self._last_voice_audio_ms,
                                )
                            if full_in and self._dashboard:
                                asyncio.create_task(self._dashboard.broadcast({
                                    "type": "log", "speaker": "user",
                                    "text": full_in,
                                    "ts": datetime.now().isoformat(),
                                }))
                            in_buf = []
                            transcript_assembler.reset()
                            # Le tour est clos : plus rien n'attend de réponse.
                            self._live_user_text = ""
                            self._noise_turn = False

                            full_out = " ".join(out_buf).strip()
                            # Mémoire : le tour est complet, on peut le
                            # raconter plus tard et chercher ce qu'il évoque.
                            if full_in or full_out:
                                self._remember_turn(full_in, full_out)
                            if full_in:
                                asyncio.create_task(self._inject_turn_context(full_in))
                            if full_out and self._dashboard:
                                asyncio.create_task(self._dashboard.broadcast({
                                    "type": "log", "speaker": "jarvis",
                                    "text": full_out,
                                    "ts": datetime.now().isoformat(),
                                }))
                            out_buf = []

                            # Vision injection: model finished tool-response turn → now send the image
                            if self._pending_vision and self.session:
                                img_b, mime_t, question, angle = self._pending_vision
                                self._pending_vision = None
                                print(f"[Vision] 📤 {len(img_b):,} bytes (angle={angle}) → main session")
                                await self.session.send_realtime_input(
                                    video={"data": img_b, "mime_type": mime_t}
                                )
                                await self.session.send_realtime_input(text=question)
                                # Mark next turn_complete behaviour depending on angle
                                if self._vision_cam_active:
                                    # Camera: keep busy until JARVIS finishes speaking the answer
                                    self._vision_cam_active    = False
                                    self._vision_close_pending = True
                                else:
                                    # Screen-only: no camera to close; release busy flag now
                                    self._vision_busy = False
                            elif self._vision_close_pending:
                                # This turn_complete IS the vision answer — close camera + release busy flag
                                self._vision_close_pending = False
                                self._vision_busy = False
                                async def _cam_close():
                                    await asyncio.sleep(2.0)
                                    self.ui.stop_camera_stream()
                                asyncio.create_task(_cam_close())

                    if response.tool_call:
                        if self._interrupted or getattr(self, "_noise_turn", False):
                            calls = getattr(response.tool_call, "function_calls", []) or []
                            fn_responses = await self._execute_tool_batch(calls)
                            if fn_responses:
                                await self.session.send_tool_response(
                                    function_responses=fn_responses
                                )
                            continue
                        calls = getattr(response.tool_call, "function_calls", [])
                        for fc in calls:
                            self.thought_streamer.feed_tool_start(
                                getattr(fc, "name", "action"),
                                getattr(fc, "args", None),
                            )
                        fn_responses = await self._execute_tool_batch(
                            response.tool_call.function_calls
                        )
                        for fc in calls:
                            self.thought_streamer.feed_tool_end(
                                getattr(fc, "name", "action")
                            )
                        if not self._interrupted and not getattr(self, "_noise_turn", False):
                            await self.session.send_tool_response(
                                function_responses=fn_responses
                            )
                            self._awaiting_server_since = time.monotonic()
                        elif fn_responses:
                            await self.session.send_tool_response(
                                function_responses=fn_responses
                            )
                        # La demande a été EXÉCUTÉE (photo prise, message
                        # envoyé…) : une reconnexion ne doit plus la rejouer,
                        # sinon l'outil repart une seconde fois et le modèle
                        # commente la deuxième photo au lieu de la première.
                        self._live_user_text = ""
        except Exception as e:
            print(f"[JARVIS] ❌ Recv: {e}")
            if hasattr(self, "reset_audio_and_turn_state"):
                self.reset_audio_and_turn_state(f"recv_exception_{type(e).__name__}")
            trace = list(getattr(self, "_live_send_trace", ()))
            if trace:
                base = trace[-1][0]
                summary = ", ".join(
                    f"{kind}:{detail}({stamp - base:+.2f}s)"
                    for stamp, kind, detail in trace
                )
                print(f"[JARVIS] Derniers envois Live : {summary}")
            traceback.print_exc()
            raise
    async def _resend_unanswered(self) -> None:
        """Repose la ou les questions perdues avec la connexion précédente.

        Sans cela, une coupure au mauvais moment avale la phrase : l'utilisateur
        a parlé, l'assistant a entendu, et plus rien ne vient — sans qu'il
        sache jamais s'il doit répéter.

        La demande repart en texte, et non en audio : la transcription existe
        déjà, elle est fidèle, et la réémettre ne coûte pas une seconde de son.
        """
        if not self.session:
            return
        if not self._unanswered:
            await self._flush_deferred_turns()
            return
        pending, self._unanswered = self._unanswered[-2:], []
        # Laisse la session finir de s'établir : une entrée envoyée sur le
        # même souffle que la poignée de main arrive parfois avant que le
        # serveur n'ait fini d'installer le contexte.
        await asyncio.sleep(0.4)
        if not self.session:
            self._unanswered = pending
            return

        note = (
            "[Reprise après une coupure réseau : réponds à ce qui suit comme "
            "si la conversation n'avait pas été interrompue, en t'excusant en "
            "une demi-phrase si la coupure a duré.]\n"
            if self._conn.should_apologize() else ""
        )
        text = note + "\n".join(pending)
        try:
            await self._submit_text_turn(text)
            self.ui.write_log(f"SYS : reprise de « {pending[-1][:60]} ».")
        except Exception as exc:
            print(f"[JARVIS] Reprise impossible : {exc}")
            self._unanswered = pending

    async def _flush_deferred_turns(self) -> None:
        """Livre les résultats de tâches longues arrivés sans session."""
        pending = list(getattr(self, "_deferred_turns", None) or [])
        if not pending:
            return
        await asyncio.sleep(0.4)
        if not self.session:
            return
        self._deferred_turns = []
        text = "\n\n".join(pending)
        try:
            delivered = await self._submit_text_turn(text)
        except Exception as exc:
            print(f"[JARVIS] Livraison différée impossible : {exc}")
            delivered = False
        if delivered:
            self.ui.write_log("SYS : résultat différé annoncé après la reconnexion.")
