"""Répartiteur d'outils ANO-GPT : déclarations, lease, circuit breaker.

Concurrence
-----------
* **asyncio** — ``_execute_tool`` / ``_execute_tool_batch`` (gather pour les
  lectures indépendantes) tournent dans la boucle Live.
* **asyncio.to_thread / run_in_executor** — chaque action bloquante quitte
  la boucle audio (GIL partagé avec Qt).
* **ActionRuntime.lease** — timeout, concurrence max, circuit breaker.
* Jamais d'appel d'outil depuis le callback PortAudio.
"""
from __future__ import annotations

import logging

import asyncio
from core.text_clean import strip_emoji_deep
from pathlib import Path
import re
import time
import traceback
from typing import Any, Protocol, Union

BASE_DIR = Path(__file__).resolve().parent.parent

from core.action_runtime import (
    ActionRuntime,
    ActionRuntimeError,
    friendly_runtime_error,
)
from core.audio_engine import SEND_SAMPLE_RATE, _MainAttr
from core.background_task import spawn_logged
from core.event_bus import ToolExecutionFinishedEvent, ToolExecutionRequestedEvent
from core.live_model_policy import DEFAULT_PRIMARY_MODEL as LIVE_MODEL
from core.live_speech_config import FRENCH_TECH_PHRASES
from core.tool_declarations import (  # noqa: F401 — ré-exportés
    CONSULT_BRAIN_DECLARATION,
    TOOL_DECLARATIONS,
    _RETIRED_TOOLS,
)
from core import memory_store, routines, screen_reader, speaker_id, tool_stats, undo_stack
from memory.memory_manager import update_memory

from actions.file_processor import file_processor
from actions.flight_finder import flight_finder
from actions.open_app import open_app
from actions.close_app import close_app
from actions.weather_report import weather_action
from actions.send_message import send_message
from actions.reminder import reminder
from actions.computer_settings import computer_settings
from actions.youtube_video import youtube_video
from actions.desktop import desktop_control
from actions.browser_control import browser_control
from actions.file_controller import file_controller
from actions.code_helper import code_helper
from actions.dev_agent import dev_agent
from actions.web_search import web_search as web_search_action
from actions.image_search import image_search as image_search_action
from actions.document_generation import generate_document as generate_document_action
from actions.computer_control import computer_control
from actions.game_updater import game_updater
from actions.media_control import media_control
from actions.shell_exec import shell_exec, hypr_control
from actions.devsecops import devsecops_control
from actions.hypr_orchestrator import hypr_orchestrator_control
from actions.auto_debug import auto_debug_action
from actions.navigation import navigation_action
from core.multimodal_vision import inspect_screen_live
from actions.capture import capture_control
from actions.music import music_control
from actions.download_music import download_music
from actions.find_nearby import find_nearby
from actions.email import email_control
from actions.calendar import calendar_control
from actions.cloud_integrations import cloud_integrations_control
from actions.prayer import prayer_control
from actions.tiktok_tracker import tiktok_tracker
from actions.tiktok_coach import tiktok_coach
from actions.github import github_control
from actions.contacts import contacts_control
from actions.sparring_partner import sparring_partner
from actions.background_tasks import BackgroundTaskService, format_tasks, format_agent_result
from actions.system_monitor import get_system_status
from actions.capability_guide import capability_guide as capability_guide_action

types = _MainAttr("types")


# Une commande brute de volume agit sur la sortie PipeWire/ALSA entière. Elle
# ne doit jamais pouvoir être utilisée à la place du contrôle du morceau.
_GLOBAL_VOLUME_SHELL_RE = re.compile(
    r"\b(?:amixer\s+(?:-D\s+\S+\s+)?(?:s?set\s+)?master|"
    r"pactl\s+set-sink-volume|wpctl\s+set-volume\s+@DEFAULT_AUDIO_SINK@)",
    re.IGNORECASE,
)

# L'enregistrement d'écran peut ouvrir le portail Wayland et capter des
# contenus privés. Une inférence isolée du modèle (« fais… » mal transcrit)
# ne suffit donc jamais : l'énoncé reconnu doit nommer cette intention.
_CAPTURE_INTENT_RE = re.compile(
    r"\b(?:capture(?:r)?|capture d.?ecran|capture ecran|"
    r"capture d.?écran|enregistre(?:r|ment)?|enregistrement|"
    r"filme(?:r)?|video d.?ecran|vidéo d.?écran|screenshot|screen record)\b",
    re.IGNORECASE,
)


def _has_explicit_capture_intent(transcript: object) -> bool:
    """True seulement si l'utilisateur a clairement demandé une capture."""
    return bool(_CAPTURE_INTENT_RE.search(str(transcript or "")))


def _is_global_volume_shell_command(args: dict) -> bool:
    raw = " ".join(str(args.get(key) or "") for key in ("command", "description"))
    return bool(_GLOBAL_VOLUME_SHELL_RE.search(raw))


def _explicit_system_volume_request(text: str) -> bool:
    """Distingue « la musique » de « le système » à partir de la phrase dite."""
    normalized = str(text or "").casefold()
    return bool(
        "volume" in normalized
        and re.search(r"\b(?:syst[eè]me|global|ordinateur|pc|toutes? les applications)\b", normalized)
    )


def _media_volume_request(text: str) -> bool:
    """Vrai uniquement quand l'utilisateur nomme le média à régler."""
    normalized = str(text or "").casefold()
    return bool(re.search(
        r"\b(?:musique|morceau|chanson|spotify|audio|vid[eé]o|youtube|film|clip|lecteur)\b",
        normalized,
    ))


def _system_volume_args_from_shell(args: dict) -> dict:
    """Convertit une commande brute déjà proposée en action sûre et dédiée."""
    raw = " ".join(str(args.get(key) or "") for key in ("command", "description"))
    value = re.search(r"(?<![\w.])(\d{1,3})\s*%", raw)
    amount = max(0, min(100, int(value.group(1)))) if value else 10
    if re.search(r"(?:\+\s*\d+\s*%|\d+%\+|volume_up|augment)", raw, re.IGNORECASE):
        return {"action": "volume_up", "value": str(max(1, amount))}
    if re.search(r"(?:-\s*\d+\s*%|\d+%-|volume_down|diminu)", raw, re.IGNORECASE):
        return {"action": "volume_down", "value": str(max(1, amount))}
    return {"action": "volume_set", "value": str(amount)}


def _relative_volume_value_from_shell(args: dict) -> str:
    """Préserve le signe (+/-) d'une commande modèle pour un lecteur média."""
    raw = " ".join(str(args.get(key) or "") for key in ("command", "description"))
    match = re.search(r"([+-]?)\s*(\d{1,3})\s*%", raw)
    if match and not match.group(1):
        # ALSA formule les deltas comme « 10%- » plutôt que « -10% ».
        suffix = re.search(r"\d{1,3}\s*%\s*([+-])", raw)
        if suffix:
            return f"{suffix.group(1)}{max(0, min(100, int(match.group(2))))}"
    if not match:
        return "-10" if re.search(r"diminu|baisse|moins", raw, re.IGNORECASE) else "+10"
    sign, amount = match.groups()
    if not sign:
        sign = "-" if re.search(r"diminu|baisse|moins", raw, re.IGNORECASE) else "+"
    return f"{sign}{max(0, min(100, int(amount)))}"


def _get_api_key() -> str:
    from core.session_manager import _get_api_key as _key
    return _key()


def _voice_engine_settings() -> dict:
    from core.session_manager import _voice_engine_settings as _settings
    return _settings()

# Outils dont une exécution ne se rattrape pas : c'est la liste que ferme la
# reconnaissance du locuteur. Le reste (chercher, ouvrir, afficher, la météo)
# reste accessible à qui passe dans la pièce — un invité peut demander l'heure.
_DESTRUCTIVE_TOOLS = frozenset({
    "close_app", "shutdown_jarvis", "shell_exec", "file_controller",
    "dev_agent", "code_helper", "game_updater", "send_message",
    "email_control", "calendar_control", "cloud_integrations_control", "contacts_control", "github_control", "computer_control", "hypr_control",
    "auto_extension_control",
    "phone_call", "phone_hangup", "phone_sms", "phone_contacts",
})

# Ces deux-là ne sont dangereux que sur certaines actions : refuser à un invité
# de baisser le volume n'aurait aucun sens.
_DESTRUCTIVE_SETTINGS = frozenset({
    "shutdown", "restart", "suspend", "lock_screen", "toggle_wifi",
    "toggle_bluetooth", "airplane_mode", "close_window",
})


def _is_destructive(name: str, args: dict) -> bool:
    if name == "computer_settings":
        return str(args.get("action", "")).strip().lower() in _DESTRUCTIVE_SETTINGS
    if name == "email_control":
        # Lire ses mails est déjà une intrusion ; les envoyer l'est plus encore.
        return True
    if name == "github_control":
        return True
    return name in _DESTRUCTIVE_TOOLS


_FAILURE_MARKERS = ("failed", "échec", "echec", "a échoué", "erreur", "error:", "impossible", "introuvable")


def _looks_like_failure(result: Any) -> bool:
    head = " ".join(str(result or "").split())[:160].casefold()
    return head.startswith(("tool '", "erreur", "échec", "echec")) or any(
        m in head[:60] for m in _FAILURE_MARKERS
    )


def _task_result_excerpt(result: Any, limit: int = 220) -> str:
    """Première ligne utile du résultat, sans balisage ni consigne au modèle."""
    text = str(result or "").strip()
    if not text:
        return ""
    # Les blocs [VISION…] / [AGENT] et les consignes « dis-le à l'utilisateur »
    # s'adressent au modèle, pas à l'œil.
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    keep = [ln for ln in lines if not ln.startswith(("[", "{", "```"))] or lines
    excerpt = " ".join(keep[:3])
    excerpt = re.sub(r"[*_`#>]+", "", excerpt)
    excerpt = re.sub(r"\s+", " ", excerpt).strip()
    return excerpt[: limit - 1] + "…" if len(excerpt) > limit else excerpt


def _task_card_summary(name: str, args: Any) -> str:
    """Ce que fait la tâche, en une ligne, d'après ses arguments."""
    if not isinstance(args, dict):
        return ""
    for key in ("query", "question", "text", "app_name", "path", "file", "url", "title",
                "message", "prompt", "command", "recipient", "to", "action"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            val = " ".join(val.split())
            return val[:119] + "…" if len(val) > 120 else val
    return ""


# Outils qui font attendre : leur description commence par la consigne
# d'annoncer avant d'appeler. Une seule source pour tous, alignée sur la règle
# « annonce avant d'agir » du prompt.
_ANNOUNCE_BEFORE_TOOLS = frozenset({
    "deep_think", "consult_brain", "web_search", "smart_search", "image_search",
    "tiktok_coach", "visual_recognition", "music_recognition",
    "generate_image", "generate_video", "generate_document", "download_music",
    "youtube_video", "file_search", "file_processor", "background_tasks", "dev_agent",
    "simulate_decision", "flight_finder", "find_nearby", "navigate",
})
_ANNOUNCE_PREFIX = (
    "AVANT d'appeler cet outil, dis à voix haute une phrase courte qui nomme ce que tu lances "
    "et demande de patienter (ex. « je lance ça, patiente, je te reviens ») ; ne l'appelle "
    "jamais en silence. "
)


def announce_before_call(declarations: list[dict]) -> list[dict]:
    """Préfixe la description des outils longs par la consigne d'annonce."""
    out: list[dict] = []
    for decl in declarations:
        name = str(decl.get("name") or "")
        desc = str(decl.get("description") or "")
        if name in _ANNOUNCE_BEFORE_TOOLS and not desc.startswith(_ANNOUNCE_PREFIX):
            decl = {**decl, "description": _ANNOUNCE_PREFIX + desc}
        out.append(decl)
    return out


# Libellés humains pour la carte « tâche en cours » (latence perçue > 1 s).
_TOOL_LABELS = {
    "consult_brain": "Réflexion",
    "web_search": "Recherche web",
    "image_search": "Recherche d'images",
    "weather_report": "Météo",
    "open_app": "Ouverture d'application",
    "close_app": "Fermeture d'application",
    "browser_control": "Navigateur",
    "file_controller": "Fichiers",
    "send_message": "Envoi de message",
    "shell_exec": "Commande",
    "visual_recognition": "Reconnaissance visuelle",
    "music_recognition": "Reconnaissance musicale",
    "music_control": "Musique",
    "download_music": "Téléchargement musique",
    "proactive_mode": "Mode proactif",
    "background_tasks": "Tâche de fond",
    "calendar_control": "Agenda",
    "cloud_integrations_control": "Intégration cloud",
    "prayer_control": "Prière",
    "tiktok_tracker": "TikTok",
    "tiktok_coach": "Coach TikTok",
    "github_control": "GitHub",
    "simulate_decision": "Simulation stratégique",
    "auto_extension_control": "Extensions autonomes",
    "contacts_control": "Contacts",
    "phone_call": "Appel téléphonique Android",
    "phone_hangup": "Raccrochage",
    "phone_contacts": "Contacts du téléphone",
    "phone_sms": "SMS depuis le téléphone",
    "sparring_partner": "Session d'entraînement",
    "focus_guard": "Bouclier anti-distraction",
    "youtube_video": "YouTube",
    "show_map": "Carte",
    "show_country_info": "Fiche pays",
    "reminder": "Rappel",
    "computer_control": "Contrôle de l'ordinateur",
    "game_updater": "Mise à jour de jeu",
    "flight_finder": "Recherche de vol",
    "capture_control": "Capture d'écran",
    "screen_process": "Analyse visuelle",
    "camera_control": "Caméra",
    "deep_think": "Réflexion approfondie",
    "voice_style": "Style vocal",
    "second_brain": "Second Brain",
    "devsecops": "DevSecOps & Système",
    "hypr_orchestrator": "Orchestrateur Hyprland",
    "point_on_screen": "Pointeur visuel",
    "capability_guide": "Compétences",
}




def _render_phone_outcome(outcome: dict, fallback: str,
                          choices_hint: str = "Demande lequel utiliser.") -> str:
    """Met en phrase la réponse d'ANO-Remote, listes de contacts comprises."""
    message = str(outcome.get("message") or fallback)
    choices = outcome.get("choices") or []
    if choices:
        rendered = ", ".join(
            f"choix {item.get('index', '?')} : {item.get('name', 'contact')} "
            f"({item.get('number_hint', 'numéro')})"
            for item in choices if isinstance(item, dict)
        )
        message += f" Choix Android : {rendered}. {choices_hint}"
    return message


class ToolHost(Protocol):
    """Contrat que l'orchestrateur expose au répartiteur d'outils."""

    ui: Any
    session: Any
    _action_runtime: Any
    _tool_session_memory: dict
    _plugins: Any
    _interrupted: bool
    _is_thinking: bool

    def speak(self, text: str) -> None: ...
    def _voice_is_stranger(self) -> bool: ...
    def _submit_text_turn(self, text: str, timeout_s: float = 90.0) -> Any: ...


class ToolDispatcher:
    """Déclarations d'outils, ``_execute_tool_impl``, lease, plugins.

    Les méthodes sont liées à l'hôte ``JarvisLive``.
    """
    _tool_session_memory: dict = {}
    _agent_tool_table: dict | None = None

    def _on_plugin_toggle(self, name: str, enabled: bool) -> None:
        if self._plugins.set_enabled(name, enabled):
            self._refresh_plugin_runtime()
            state = "activé" if enabled else "désactivé"
            self.ui.write_log(
                f"SYS : plugin {name} {state} — effectif à la prochaine reconnexion."
            )

    def _refresh_plugin_runtime(self) -> None:
        """Publie atomiquement la nouvelle table d'outils pour la prochaine session."""
        self._action_runtime = ActionRuntime(
            getattr(self, "_tool_declarations", TOOL_DECLARATIONS)
            + self._plugins.declarations()
        )
    async def _send_phone_sms(self, args: dict) -> str:
        """SMS par la SIM du téléphone, derrière la barrière de confirmation.

        L'envoi réel n'a lieu qu'après le clic humain ; la barrière refuse
        d'elle-même une demande identique déjà exécutée, ce qui empêche un
        rappel du modèle de faire partir deux fois le même message.
        """
        from core import human_confirmation

        target = str(args.get("target") or "").strip()
        body = str(args.get("body") or "").strip()
        selection = int(args.get("selection") or 0)
        if not target:
            return "Indiquez le destinataire du SMS."
        if not body:
            return "Indiquez le texte du SMS."
        if self._dashboard is None:
            return "ANO-Remote est indisponible : aucun SMS n'a été envoyé."

        loop = asyncio.get_running_loop()

        def _send() -> str:
            # Appelé depuis le thread de la confirmation : la requête repasse
            # par la boucle asyncio qui détient la socket du téléphone.
            future = asyncio.run_coroutine_threadsafe(
                self._dashboard.request_phone_sms(target, body, selection), loop
            )
            try:
                outcome = future.result(timeout=40.0)
            except TimeoutError:
                future.cancel()
                return "Délai SMS dépassé : statut inconnu. Vérifiez le téléphone avant de réessayer."
            return _render_phone_outcome(outcome, "Commande SMS traitée.")

        return human_confirmation.request(
            "phone:sms",
            "Envoyer un SMS depuis le téléphone",
            f"Destinataire : {target}\n\n{body}",
            _send,
        )

    async def _execute_tool(self, fc) -> types.FunctionResponse:
        """Valide, borne et observe chaque action avant son exécution réelle."""
        name = str(getattr(fc, "name", "") or "")
        if self._interrupted or getattr(self, "_noise_turn", False):
            return types.FunctionResponse(
                id=fc.id,
                name=name or "unknown",
                response={"result": "Action annulée par interruption utilisateur", "ok": False},
            )
        raw_args = getattr(fc, "args", None)
        started = time.perf_counter()
        prepared: dict = {}
        bus = getattr(self, "_event_bus", None)
        if bus is not None:
            bus.publish_sync(ToolExecutionRequestedEvent(
                tool_name=name or "unknown",
                call_id=str(getattr(fc, "id", "") or ""),
                params=dict(raw_args) if isinstance(raw_args, dict) else {},
            ))

        try:
            prepared = self._action_runtime.prepare(name, raw_args)
            verification = await self._verify_sensitive_voice_command(name, prepared)
            if verification:
                response = types.FunctionResponse(
                    id=fc.id, name=name,
                    response={"result": verification, "ok": False},
                )
            else:
                async with self._action_runtime.lease(name) as policy:
                    if self._interrupted or getattr(self, "_noise_turn", False):
                        return types.FunctionResponse(
                            id=fc.id, name=name,
                            response={"result": "Action annulée par interruption utilisateur", "ok": False},
                        )
                    timeout_s = policy.timeout_s
                    # L'action termine elle-même avant le garde-fou Live :
                    # deux secondes restent disponibles pour restituer un
                    # résultat ou une erreur propre.
                    prepared["_budget_s"] = max(0.1, policy.timeout_s - 2.0)
                    if name == "email_control" and str(prepared.get("action", "")).lower() in {
                        "connect", "login", "authorize",
                    }:
                        # OAuth attend jusqu'à 180 s la validation dans Chrome,
                        # puis échange le code et vérifie le profil Gmail.
                        timeout_s = max(timeout_s, 210.0)
                    if name == "github_control" and str(prepared.get("action", "")).lower() in {
                        "connect", "login", "authorize",
                    }:
                        # Device Flow GitHub : le code peut être validé jusqu'à 15 min dans Chrome.
                        timeout_s = max(timeout_s, 920.0)
                    # Le chien de garde audio coupe le micro dès 5 s sans
                    # aucune donnée audio — pensé pour une session Live
                    # bloquée, pas pour un outil qui attend légitimement un
                    # relevé GPS ou un appel réseau en cascade (navigate,
                    # web_search…). Sans ce battement, tout outil dépassant
                    # 5 s se fait couper le micro pendant qu'il tourne encore
                    # côté serveur, produisant des réponses incohérentes une
                    # fois le résultat enfin prêt. Le battement ne dispense
                    # jamais du délai réel de l'outil (asyncio.timeout reste
                    # le seul garde-fou qui l'arrête) ; il dit seulement au
                    # chien de garde « du travail attendu est en cours ».
                    async def _tool_heartbeat() -> None:
                        while True:
                            await asyncio.sleep(2.0)
                            self._last_model_turn_data_at = time.monotonic()

                    heartbeat = asyncio.ensure_future(_tool_heartbeat())
                    try:
                        async with asyncio.timeout(timeout_s):
                            response = await self._execute_tool_impl(fc, prepared)
                    finally:
                        heartbeat.cancel()
                        try:
                            await heartbeat
                        except asyncio.CancelledError:
                            pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if name == "screen_process":
                # Un appel HTTP synchrone lancé dans l'exécuteur peut survivre
                # au timeout asyncio. Son verrou logique ne doit jamais rendre
                # les demandes suivantes dépendantes de cet ancien appel.
                self._vision_busy = False
                self._pending_vision = None
            # Une erreur de paramètres ou un circuit déjà ouvert n'est pas une
            # panne de l'outil et ne doit pas prolonger sa suspension.
            if not isinstance(exc, ActionRuntimeError):
                self._action_runtime.note_failure(name)
            message = friendly_runtime_error(name or "inconnue", exc)
            if name == "screen_process" and isinstance(exc, TimeoutError):
                message = (
                    "La capture d'écran a dépassé son délai d'analyse. "
                    "La vision a été libérée pour la prochaine demande ; "
                    "ne relance pas automatiquement cette action."
                )
            # Le modèle a cherché une capacité absente : conserver la demande
            # utilisateur, pas l'hallucination de nom d'outil, pour l'analyse
            # de lacunes. Les pannes d'outils existants restent exclues.
            if "Action inconnue" in str(exc):
                try:
                    from core.auto_extension import get_auto_extension_manager
                    get_auto_extension_manager().record_unmet(
                        str(getattr(self, "_live_user_text", "") or ""),
                        "aucun outil correspondant dans le répartiteur central",
                    )
                except Exception as exc:
                    print(f"[Dispatcher] Besoin non couvert non enregistré : {exc}")
            duration_ms = (time.perf_counter() - started) * 1000.0
            tool_stats.record(name or "unknown", ok=False, duration_ms=duration_ms, error=message)
            self._action_runtime.remember(
                self._tool_session_memory,
                name=name or "unknown",
                args=prepared or (dict(raw_args) if isinstance(raw_args, dict) else {}),
                result=message,
                ok=False,
                duration_ms=duration_ms,
            )
            # L'utilisateur entend une phrase courte ; la pile d'appel, elle,
            # doit survivre quelque part — sinon « l'outil a rencontré une
            # erreur » est tout ce qui reste pour diagnostiquer.
            from core.observability import tool_failure
            tool_failure(name or "unknown", exc, message=message,
                         duration_ms=duration_ms,
                         args=prepared or (dict(raw_args) if isinstance(raw_args, dict) else {}))
            self.ui.write_log(f"ERR: {message}")
            if bus is not None:
                bus.publish_sync(ToolExecutionFinishedEvent(
                    call_id=str(getattr(fc, "id", "") or ""),
                    result=message,
                    duration_ms=duration_ms,
                    error=f"{type(exc).__name__}: {exc}",
                ))
            if not self.ui.muted:
                self.ui.set_state("LISTENING")
            return types.FunctionResponse(
                id=fc.id,
                name=name or "unknown",
                response={"result": message, "ok": False,
                          "retryable": isinstance(exc, ActionRuntimeError),
                          "error_type": type(exc).__name__},
            )

        payload = getattr(response, "response", {}) or {}
        result = payload.get("result", "") if isinstance(payload, dict) else str(payload)
        failed = (
            isinstance(payload, dict) and payload.get("ok") is False
        ) or self._action_runtime.looks_failed(result)
        if isinstance(payload, dict):
            payload["ok"] = not failed
        if self._action_runtime.should_trip_from_result(result):
            self._action_runtime.note_failure(name)
        elif not failed:
            self._action_runtime.note_success(name)
        duration_ms = (time.perf_counter() - started) * 1000.0
        tool_stats.record(
            name,
            ok=not failed,
            duration_ms=duration_ms,
            error=str(result)[:240] if failed else "",
        )
        # Seules les actions réellement réussies alimentent les habitudes.
        # open_app est capturé plus précisément par launch_tracker (nom de
        # l'application, sans titre ni document ouvert).
        if not failed and name == "music_control":
            self._habits.record("music")
        self._action_runtime.remember(
            self._tool_session_memory,
            name=name,
            args=prepared,
            result=result,
            ok=not failed,
            duration_ms=duration_ms,
        )
        if bus is not None:
            bus.publish_sync(ToolExecutionFinishedEvent(
                call_id=str(getattr(fc, "id", "") or ""),
                result=result,
                duration_ms=duration_ms,
                error=str(result)[:240] if failed else None,
            ))
        return response

    async def _verify_sensitive_voice_command(
        self, tool_name: str, args: dict | None = None,
    ) -> str:
        """Fait confirmer les mots par un second ASR avant une action sensible."""
        if (not getattr(self, "_precision_stt_enabled", True)
                or not _is_destructive(tool_name, args or {})):
            return ""
        clip = self._last_voice_clip
        main_text = str(self._live_user_text or "").strip()
        if (clip is None or not main_text
                or time.monotonic() - self._last_voice_clip_at > 20.0):
            return ""
        if self._precision_stt is None:
            vocabulary = [
                *FRENCH_TECH_PHRASES, self._asst_name,
                str(_voice_engine_settings().get("user_name", "Anonymous")),
            ]
            from core.precision_stt import PrecisionTranscriber
            self._precision_stt = PrecisionTranscriber(
                _get_api_key(), model=self._precision_stt_model,
                vocabulary=vocabulary,
            )
        result = await self._precision_stt.transcribe(clip, SEND_SAMPLE_RATE)
        if not result.available:
            if not self._precision_stt_warning_logged:
                self._precision_stt_warning_logged = True
                self.ui.write_log(
                    "WARN: vérification Gemini 3.5 indisponible — "
                    f"protection locale conservée ({result.error})."
                )
            return ""
        self._precision_stt_warning_logged = False
        self._tool_session_memory["precision_transcript"] = result.text
        from core.precision_stt import transcripts_conflict
        if not transcripts_conflict(main_text, result.text):
            print(f"[STT précision] ✅ {result.text}")
            return ""
        print(
            f"[STT précision] ⛔ divergence: Live={main_text!r} / "
            f"Transcribe={result.text!r}"
        )
        try:
            self.ui.show_card(
                "confirmation", "Commande vocale incertaine",
                "Deux moteurs ont compris des phrases différentes. "
                "L'action a été bloquée ; répète-la clairement.",
            )
        except Exception:
            logging.getLogger(__name__).warning("Échec auxiliaire dans _verify_sensitive_voice_command")
        return (
            "ACTION NON EXÉCUTÉE : la transcription haute précision ne confirme "
            "pas la phrase entendue par l'agent vocal. Demande simplement à "
            "l'utilisateur de répéter sa commande ; ne tente aucun autre outil."
        )

    async def _execute_tool_batch(self, function_calls) -> list[types.FunctionResponse]:
        """Exécute ensemble les lectures indépendantes, sinon conserve l'ordre."""
        calls = list(function_calls or ())
        self._is_thinking = True
        active = getattr(self, "_active_tool_tasks", None)
        if active is None:
            active = self._active_tool_tasks = set()
        started: set[asyncio.Task] = set()

        def cancelled_responses() -> list[types.FunctionResponse]:
            return [
                types.FunctionResponse(
                    id=fc.id,
                    name=str(getattr(fc, "name", "") or "unknown"),
                    response={
                        "result": "Action annulée par interruption utilisateur",
                        "ok": False,
                    },
                )
                for fc in calls
            ]

        async def run_clean(fc) -> types.FunctionResponse:
            # Ce que lit le modèle vocal ne doit pas contenir d'émojis : sur
            # un titre TikTok en pictogrammes, Gemini Live cale en « euh… ».
            resp = await self._execute_tool(fc)
            try:
                payload = getattr(resp, "response", None)
                if isinstance(payload, dict):
                    resp.response = strip_emoji_deep(payload)
            except Exception:
                logging.getLogger(__name__).warning("Échec auxiliaire dans run_clean")
            return resp

        def start(fc) -> asyncio.Task:
            task = asyncio.create_task(
                run_clean(fc),
                name=f"live-tool-{str(getattr(fc, 'name', 'unknown'))}",
            )
            started.add(task)
            active.add(task)
            task.add_done_callback(active.discard)
            return task

        try:
            if len(calls) > 1 and all(
                self._action_runtime.can_run_in_parallel(
                    str(getattr(fc, "name", "") or ""), getattr(fc, "args", None)
                )
                for fc in calls
            ):
                for fc in calls:
                    print(f"[JARVIS] 📞 {fc.name} (parallèle)")
                return list(await asyncio.gather(*(start(fc) for fc in calls)))

            responses = []
            for fc in calls:
                if self._interrupted or getattr(self, "_noise_turn", False):
                    responses.extend(cancelled_responses()[len(responses):])
                    return responses
                print(f"[JARVIS] 📞 {fc.name}")
                responses.append(await start(fc))
            return responses
        except asyncio.CancelledError:
            # `interrupt()` n'annule que ces enfants. Transformer leur
            # annulation en réponse d'outil garde la connexion Live intacte et
            # rend immédiatement le micro à l'utilisateur.
            for task in started:
                if not task.done():
                    task.cancel()
            if started:
                await asyncio.gather(*started, return_exceptions=True)
            return cancelled_responses()
        finally:
            active.difference_update(started)
            self._is_thinking = False

    @staticmethod
    def _compute_deep_research(question: str, context: str = "") -> str:
        """Calcul bloquant isolé du canal Gemini Live."""
        from core import agent_brain
        from core.llm_client import think_deep, BRAIN_UNCONFIGURED

        result = think_deep(question, context)
        if result != BRAIN_UNCONFIGURED:
            return result or "La recherche approfondie n'a rien renvoyé."
        if agent_brain.available():
            return agent_brain.think(question, context)
        return (
            "Aucun agent n'est installé et aucune clé DeepSeek, Grok, OpenAI "
            "ou Claude n'est enregistrée. Réponds toi-même à la demande."
        )

    async def _deliver_deep_research(self, question: str, context: str = "") -> None:
        """Calcule puis livre le résultat sans retenir un tool call Live."""
        task_id = f"deep-research-{time.monotonic_ns()}"
        self._task_card(task_id, "Réflexion approfondie en cours", question[:200], "running")

        try:
            if hasattr(self, "thought_streamer"):
                self.thought_streamer.feed_tool_start("agent_brain", {"question": question})
            result = await asyncio.to_thread(
                self._compute_deep_research, question, context
            )
            if hasattr(self, "thought_streamer"):
                self.thought_streamer.feed_tool_end("agent_brain")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            result = f"La recherche approfondie a échoué : {str(exc)[:300]}"

        result = str(result or "La recherche approfondie n'a rien renvoyé.").strip()
        # Une sortie anormalement énorme peut elle aussi faire refuser le tour
        # Live. La réponse vocale reste dense ; la carte conserve l'essentiel.
        result = result[:12000]
        self._task_card(task_id, "Réflexion approfondie terminée", _task_result_excerpt(result), "done")
        self._ui_card("show_card", "result", "Réflexion approfondie", result)

        # Session absente (reconnexion en cours) : `_submit_text_turn`
        # conserve le tour et le livre dès la connexion rétablie.
        prompt = (
            "[RÉSULTAT DE RECHERCHE APPROFONDIE TERMINÉ]\n"
            f"Question originale : {question}\n\n"
            f"Résultat : {result}\n\n"
            "Présente maintenant ce résultat directement en français, sans "
            "annoncer un nouveau délai et sans appeler aucun outil."
        )
        try:
            delivered = await self._submit_text_turn(prompt)
        except Exception as exc:
            delivered = False
            print(f"[DeepResearch] Livraison vocale différée : {exc}")
        if not delivered:
            self.ui.write_log(
                "WARN: résultat vocal différé ; il reste disponible dans la carte."
            )

    def _start_deep_research(self, question: str, context: str = "") -> bool:
        """Lance au plus une recherche lourde à la fois."""
        tasks = getattr(self, "_deep_research_tasks", None)
        if tasks is None:
            tasks = self._deep_research_tasks = set()
        tasks.intersection_update({task for task in tasks if not task.done()})
        if tasks:
            return False
        spawn_logged(
            self._deliver_deep_research(question, context),
            name="deep-research-background", ui=getattr(self, "ui", None), registry=tasks,
        )
        return True

    async def _deliver_deferred_tool(self, name: str, args: dict) -> None:
        """Travail long hors du tool call, puis restitution dans un nouveau tour."""
        title = _TOOL_LABELS.get(name, name)
        task_id = f"deferred-{name}-{time.monotonic_ns()}"
        self._task_card(task_id, f"{title} en cours", _task_card_summary(name, args), "running")
        try:
            if name == "consult_brain":
                from core import brain_relay
                result = await brain_relay.run_turn(
                    self, str(args.get("question") or ""),
                    context=str(args.get("context") or ""),
                    declarations=self._relay_declarations(), base_prompt=self._relay_base_prompt(),
                    timeout=float(args.get("_budget_s") or 18.0),
                )
            elif name == "generate_document":
                result = await asyncio.to_thread(generate_document_action, parameters=args, player=self.ui)
            elif name == "email_control":
                result = await asyncio.to_thread(
                    email_control, parameters=args, session_memory=self._tool_session_memory, ui=self.ui,
                )
            elif name == "download_music":
                # La recherche YouTube qui précède le worker yt-dlp peut elle
                # aussi être lente : elle ne doit jamais garder le tool call.
                result = await asyncio.to_thread(
                    download_music, parameters=args, player=self.ui, speak=self.speak,
                )
            else:
                return
        except Exception as exc:
            result = f"{title} a échoué : {str(exc)[:300]}"
            self._task_card(task_id, f"{title} — échec", _task_result_excerpt(result), "error")
        else:
            self._task_card(task_id, f"{title} terminée", _task_result_excerpt(result), "done")

        result = str(result or f"{title} terminé.").strip()
        self._ui_card("show_card", "result", title, result[:12_000])
        if self.session is not None:
            try:
                await self._submit_text_turn(
                    f"[RÉSULTAT {title.upper()}]\n{result[:8_000]}\n\n"
                    "Annonce directement ce résultat en français, sans rappeler d'outil.",
                    timeout_s=35.0,
                )
            except Exception as exc:
                self.ui.write_log(f"WARN: livraison différée {name} indisponible : {exc}")

    def _start_deferred_tool(self, name: str, args: dict) -> bool:
        """Un seul travail long du même type, accusé immédiat pour le micro."""
        tasks = getattr(self, "_deferred_tool_tasks", None)
        if tasks is None:
            tasks = self._deferred_tool_tasks = {}
        current = tasks.get(name)
        if current is not None and not current.done():
            return False
        task = asyncio.create_task(self._deliver_deferred_tool(name, dict(args)),
                                   name=f"deferred-{name}")
        tasks[name] = task
        task.add_done_callback(
            lambda done, key=name: tasks.pop(key, None) if tasks.get(key) is done else None
        )
        return True

    async def _deliver_image_generation(self, args: dict) -> None:
        """Génère l'image hors du tour Live et annonce chaque étape en français."""
        from actions.image_generation import generate_image

        prompt = str(args.get("prompt") or "")
        task_id = f"image-{time.monotonic_ns()}"
        self._task_card(task_id, "Création d'image en cours", prompt[:200], "running")
        worker = asyncio.create_task(
            asyncio.to_thread(generate_image, args, self.ui),
            name="azure-image-worker",
        )
        try:
            try:
                result = await asyncio.wait_for(asyncio.shield(worker), timeout=20.0)
            except asyncio.TimeoutError:
                # Le premier accusé de réception vient du résultat immédiat de
                # l'outil. Cette seconde phrase n'est envoyée que si Azure
                # travaille toujours, et passe par Gemini Live : même voix.
                if self.session is not None:
                    await self._submit_text_turn(
                        "[AVANCEMENT IMAGE] Dis uniquement en français : « Monsieur, "
                        "la création de l'image est toujours en cours. Je vous préviens dès qu'elle est prête. »",
                        timeout_s=25.0,
                    )
                result = await worker
        except asyncio.CancelledError:
            worker.cancel()
            raise
        except Exception as exc:
            result = f"Génération Azure échouée : {exc}"

        result = str(result or "La génération d'image n'a rien renvoyé.").strip()
        failed = _looks_like_failure(result)
        self._task_card(task_id, "Création d'image — échec" if failed else "Création d'image terminée",
                        _task_result_excerpt(result), "error" if failed else "done")
        self._ui_card("show_card", "result", "Création d'image", result)
        # Pas de test `self.session is not None` : si la connexion est en
        # reprise, le tour est conservé puis livré après la reconnexion.
        delivered = await self._submit_text_turn(
            "[RÉSULTAT DE GÉNÉRATION D'IMAGE]\n"
            f"Demande : {prompt}\nRésultat : {result}\n\n"
            "Annonce uniquement le résultat en français, avec le ton Majeur. "
            "Si l'image est créée, confirme qu'elle est prête et indique son chemin. "
            "Si elle a échoué, explique la raison en français sans proposer de la relancer automatiquement.",
            timeout_s=35.0,
        )
        if not delivered:
            self.ui.write_log("WARN: image prête ; annonce vocale différée (carte disponible).")

    def _start_image_generation(self, args: dict) -> bool:
        """Une image Azure à la fois : elle peut prendre plusieurs minutes."""
        tasks = getattr(self, "_image_generation_tasks", None)
        if tasks is None:
            tasks = self._image_generation_tasks = set()
        tasks.intersection_update({task for task in tasks if not task.done()})
        if tasks:
            return False
        spawn_logged(
            self._deliver_image_generation(dict(args)),
            name="azure-image-background", ui=self.ui, registry=tasks,
        )
        return True

    async def _deliver_video_generation(self, args: dict) -> None:
        """Sora travaille plusieurs minutes : tout se passe hors du tour Live."""
        from actions.video_generation import generate_video

        prompt = str(args.get("prompt") or "")
        title = "Création vidéo"
        task_id = f"video-{time.monotonic_ns()}"
        self._task_card(task_id, f"{title} en cours", prompt[:200] or "Génération Azure Sora…", "running")

        def progress(status: str) -> None:
            # Le signal Qt est thread-safe : pas besoin de repasser par la boucle.
            self._task_card(task_id, f"{title} en cours", f"Sora : {status}…", "running")

        worker = asyncio.create_task(
            asyncio.to_thread(generate_video, args, self.ui, progress),
            name="azure-video-worker",
        )
        try:
            try:
                result = await asyncio.wait_for(asyncio.shield(worker), timeout=25.0)
            except asyncio.TimeoutError:
                if self.session is not None:
                    await self._submit_text_turn(
                        "[AVANCEMENT VIDÉO] Dis uniquement en français : « Monsieur, "
                        "la vidéo est toujours en cours de génération. Je vous préviens dès qu'elle est prête. »",
                        timeout_s=25.0,
                    )
                result = await worker
        except asyncio.CancelledError:
            worker.cancel()
            raise
        except Exception as exc:
            result = f"Génération vidéo Azure échouée : {exc}"

        result = str(result or "La génération vidéo n'a rien renvoyé.").strip()
        failed = _looks_like_failure(result)
        self._task_card(task_id, f"{title} — échec" if failed else f"{title} terminée",
                        _task_result_excerpt(result), "error" if failed else "done")
        self._ui_card("show_card", "result", title, result)
        delivered = await self._submit_text_turn(
            "[RÉSULTAT DE GÉNÉRATION VIDÉO]\n"
            f"Demande : {prompt}\nRésultat : {result}\n\n"
            "Annonce uniquement le résultat en français, avec le ton Majeur. "
            "Si la vidéo est créée, confirme qu'elle est prête et indique son chemin. "
            "Si elle a échoué, explique la raison sans relancer la génération.",
            timeout_s=35.0,
        )
        if not delivered:
            self.ui.write_log("WARN: vidéo prête ; annonce vocale différée (carte disponible).")

    def _task_card(self, task_id: str, title: str, body: str, status: str) -> None:
        """Carte de tâche ; sans interface compatible, retombe sur show_card."""
        ui = getattr(self, "ui", None)
        if ui is None:
            return
        try:
            if hasattr(ui, "task_card"):
                ui.task_card(task_id, title, body, status)
            elif status == "running" and hasattr(ui, "show_card"):
                ui.show_card("task", title, body)
        except Exception as exc:
            print(f"[Dispatcher] Carte de tâche ignorée : {type(exc).__name__}: {exc}")

    def _ui_card(self, method: str, *args) -> None:
        """Appelle `show_card` / `update_card` / `dismiss_cards` sans jamais
        interrompre l'outil : une carte est décorative, mais son échec doit
        laisser une trace lisible plutôt que disparaître en silence."""
        try:
            getattr(self.ui, method)(*args)
        except Exception as exc:
            print(f"[Dispatcher] Carte UI ignorée ({method}) : {type(exc).__name__}: {exc}")

    def _safe_update_card(self, card_type: str, title: str, body: str) -> None:
        self._ui_card("update_card", card_type, title, body)

    def _start_video_generation(self, args: dict) -> bool:
        """Une vidéo Azure à la fois : Sora est lent et coûteux."""
        tasks = getattr(self, "_video_generation_tasks", None)
        if tasks is None:
            tasks = self._video_generation_tasks = set()
        tasks.intersection_update({task for task in tasks if not task.done()})
        if tasks:
            return False
        spawn_logged(
            self._deliver_video_generation(dict(args)),
            name="azure-video-background", ui=self.ui, registry=tasks,
        )
        return True

    async def _deliver_decision_simulation(self, decision: str, options: list[str]) -> None:
        """Produit une carte complète et ne prononce que la recommandation."""
        from core.decision_simulator import DecisionSimulationError, run_simulation

        title = "Simulation stratégique"
        try:
            self.ui.show_card("task", title, "Préparation des scénarios…")

            def progress(message: str) -> None:
                self.ui.update_card("task", title, message)

            simulation = await asyncio.to_thread(
                run_simulation, decision, options, progress=progress
            )
        except asyncio.CancelledError:
            self.ui.dismiss_cards("task", title)
            raise
        except DecisionSimulationError as exc:
            self.ui.dismiss_cards("task", title)
            self.ui.show_card("error", title, str(exc))
            return
        except Exception as exc:
            self.ui.dismiss_cards("task", title)
            self.ui.show_card("error", title, f"Simulation interrompue : {str(exc)[:400]}")
            return

        self.ui.dismiss_cards("task", title)
        body = (
            f"**Décision :** {simulation.decision}\n\n"
            f"**Options simulées :** {', '.join(simulation.options)}\n"
            f"**Archivée :** {simulation.saved_at}\n\n{simulation.synthesis}"
        )
        self.ui.show_card("result", title, body[:12_000])
        # Le résultat détaillé appartient à la carte. Cette instruction assure
        # une restitution vocale courte, sans relancer un autre outil.
        try:
            await self._submit_text_turn(
                "[SIMULATION STRATÉGIQUE TERMINÉE] Résumé vocal en deux phrases "
                "maximum : annonce seulement la recommandation principale et son "
                "risque décisif, sans outil ni préambule.\n\n"
                + simulation.synthesis[:3_500]
            )
        except Exception:
            self.ui.write_log("SYS : simulation terminée ; le détail est dans la carte.")

    def _start_decision_simulation(self, decision: str, options: list[str] | None = None) -> bool:
        tasks = getattr(self, "_decision_simulation_tasks", None)
        if tasks is None:
            tasks = self._decision_simulation_tasks = set()
        tasks.intersection_update({task for task in tasks if not task.done()})
        if tasks:
            return False
        spawn_logged(
            self._deliver_decision_simulation(decision, list(options or [])),
            name="decision-simulation-background", ui=self.ui, registry=tasks,
        )
        return True

    async def _execute_tool_impl(self, fc, prepared_args: dict | None = None) -> types.FunctionResponse:
        name = fc.name
        args = dict(prepared_args if prepared_args is not None else (fc.args or {}))

        print(f"[JARVIS] 🔧 {name}  {args}")
        self.ui.set_state("THINKING")

        if name == "save_memory":
            category = args.get("category", "notes")
            key      = args.get("key", "")
            value    = args.get("value", "")
            if key and value:
                update_memory({category: {key: {"value": value}}})
                # Le JSON reste la source du briefing et de la proactivité ;
                # la base longue durée est ce qui se cherche et se rappelle.
                self._store_memory(category, key, value)
                print(f"[Memory] 💾 save_memory: {category}/{key} = {value}")
            if not self.ui.muted:
                self.ui.set_state("LISTENING")
            return types.FunctionResponse(
                id=fc.id, name=name,
                response={"result": "ok", "silent": True}
            )

        if name == "voice_id":
            # Apprendre une voix demande trois empreintes : c'est court, mais
            # assez pour ne pas le faire sur la boucle qui porte l'audio.
            answer = await asyncio.get_running_loop().run_in_executor(
                None, self._agent_voice_id, args
            )
            if not self.ui.muted:
                self.ui.set_state("LISTENING")
            return types.FunctionResponse(id=fc.id, name=name,
                                          response={"result": answer})

        # Une voix inconnue ne commande pas la machine. Le refus part d'ici et
        # non du modèle : une consigne dans le prompt se contourne en insistant,
        # un verrou dans le code, non.
        if _is_destructive(name, args) and self._voice_is_stranger():
            print(f"[Voix] ⛔ {name} refusé — voix non reconnue")
            return types.FunctionResponse(
                id=fc.id, name=name,
                response={"result": (
                    "REFUSÉ : cette voix n'est pas celle de l'utilisateur "
                    "enregistré, et cette action est irréversible. Dis-le "
                    "simplement et poliment, propose de demander à l'utilisateur "
                    "lui-même. N'essaie aucun autre outil pour y arriver."
                )},
            )

        if name == "routine":
            # Rien à déléguer à un exécuteur : la routine part dans son propre
            # thread et rend la main tout de suite.
            answer = self._agent_routine(args)
            if not self.ui.muted:
                self.ui.set_state("LISTENING")
            return types.FunctionResponse(id=fc.id, name=name,
                                          response={"result": answer})

        loop = asyncio.get_running_loop()
        result = "Done."

        # Chaque tâche a sa propre carte, dès son départ : « <Tâche> en
        # cours », puis « terminée » avec le résultat, ou « échec ». Une clé
        # par appel : deux outils en parallèle ne se marchent pas dessus.
        label = _TOOL_LABELS.get(name, name)
        task_id = str(getattr(fc, "id", "") or "") or f"{name}-{id(fc)}"
        task_ok = True
        self._task_card(task_id, f"{label} en cours", _task_card_summary(name, args), "running")
        try:
            if name == "consult_brain":
                if self._start_deferred_tool(name, args):
                    result = (
                        "La réflexion est lancée en arrière-plan. Dis-le immédiatement ; "
                        "le résultat sera annoncé dès qu'il sera prêt."
                    )
                else:
                    result = "Une réflexion est déjà en cours. Dis-le brièvement."

            elif name == "deep_think":
                question = args.get("question", "")
                context = args.get("context", "")
                if self._start_deep_research(question, context):
                    # Répondre au tool call tout de suite. Le résultat réel
                    # arrivera dans un nouveau tour lorsqu'il sera prêt.
                    result = (
                        "La recherche approfondie est lancée en arrière-plan. "
                        "Dis simplement à l'utilisateur que tu recherches et "
                        "que le résultat arrivera automatiquement. Ne rappelle "
                        "pas cet outil."
                    )
                else:
                    result = (
                        "Une recherche approfondie est déjà en cours. Dis-le "
                        "brièvement et ne rappelle pas cet outil."
                    )

            elif name == "simulate_decision":
                decision = str(args.get("decision") or "")
                options = args.get("options") if isinstance(args.get("options"), list) else []
                if self._start_decision_simulation(decision, options):
                    result = (
                        "La simulation stratégique est lancée en arrière-plan. "
                        "Dis à l'utilisateur que la carte affichera le débat, les risques "
                        "et la recommandation dès qu'ils seront prêts."
                    )
                else:
                    result = "Une simulation stratégique est déjà en cours."

            elif name == "open_app":
                # ── Garde-fou anti-boucle ─────────────────────────────────
                # Le modèle Live peut re-décider d'appeler open_app après
                # avoir reçu le résultat (« kitty est ouvert »). Sans ce
                # verrou, chaque résultat renvoyé au modèle déclenche un
                # nouvel appel identique → fenêtres à l'infini.
                import time as _t_mod
                _now = _t_mod.monotonic()
                _app_sig = (
                    f"{str(args.get('app_name', '') or '').lower().strip()}"
                    f"|{str(args.get('command', '') or '').lower().strip()}"
                    f"|{str(args.get('workspace', '') or '').strip()}"
                )
                _OA_COOLDOWN = 10.0  # seconds — même app+cmd+ws dans ce délai = doublon
                if (
                    _app_sig == getattr(self, "_open_app_last_sig", "")
                    and _app_sig  # ne bloque pas les appels vides
                    and (_now - getattr(self, "_open_app_last_time", 0.0)) < _OA_COOLDOWN
                ):
                    _wait = _OA_COOLDOWN - (_now - self._open_app_last_time)
                    print(f"[open_app] ⏳ Doublon bloqué ({_wait:.1f}s restantes) — même app+commande+workspace")
                    result = (
                        "DÉJÀ FAIT. L'application a été lancée et la commande "
                        "tapée lors de l'appel précédent (il y a moins de 10 s). "
                        "Ne rappelle PAS cet outil. Confirme simplement à "
                        "l'utilisateur que c'est fait."
                    )
                else:
                    self._open_app_last_sig = _app_sig
                    self._open_app_last_time = _now
                    r = await loop.run_in_executor(None, lambda: open_app(parameters=args, response=None, player=self.ui, session_memory=self._tool_session_memory))
                    result = r or f"Opened {args.get('app_name')}."
                    # Ajouter une instruction anti-loop explicite dans le
                    # résultat pour que le modèle ne re-tente pas.
                    result += " Action terminée — ne rappelle PAS open_app."

            elif name == "close_app":
                r = await loop.run_in_executor(None, lambda: close_app(parameters=args, response=None, player=self.ui, session_memory=self._tool_session_memory))
                result = r or f"Closed {args.get('app_name')}."

            elif name == "weather_report":
                r = await loop.run_in_executor(None, lambda: weather_action(parameters=args, player=self.ui))
                result = r or "Weather delivered."
                from actions.weather_report import get_last_weather_card
                _card = get_last_weather_card()
                if _card:
                    self.ui.show_card("result", "Météo", _card)

            elif name == "location":
                # Résultat structuré du GPS/config, jamais une supposition à
                # partir de la langue ou de l'adresse du fournisseur IA.
                result = await loop.run_in_executor(
                    None, lambda: self._agent_location({"refresh": False})
                )

            elif name == "browser_control":
                r = await loop.run_in_executor(None, lambda: browser_control(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "file_controller":
                r = await loop.run_in_executor(
                    None,
                    lambda: file_controller(
                        parameters=args, player=self.ui,
                        session_memory=self._tool_session_memory,
                    ),
                )
                result = r or "Done."

            elif name == "send_message":
                r = await loop.run_in_executor(None, lambda: send_message(parameters=args, response=None, player=self.ui, session_memory=None))
                result = r or f"Message sent to {args.get('receiver')}."
                if result.startswith("[NEEDS_CONFIRM] "):
                    _plat, _recv, _txt = result[len("[NEEDS_CONFIRM] "):].split("|", 2)
                    _confirm_args = dict(args)
                    _confirm_args["confirm"] = True

                    def _resend(a=_confirm_args):
                        if self.ui.on_text_command:
                            self.ui.on_text_command(
                                f"envoie ce message maintenant : « {a.get('message_text')} » "
                                f"à {a.get('receiver')} sur {a.get('platform')}, confirme envoi"
                            )

                    self.ui.show_card(
                        "confirmation", f"Envoyer sur {_plat} ?",
                        f"**À :** {_recv}\n\n{_txt}",
                        [
                            {"label": "Envoyer", "primary": True, "callback": _resend},
                            {"label": "Annuler",
                             "callback": (lambda: self.ui.on_text_command("annule cet envoi")
                                          if self.ui.on_text_command else None)},
                        ],
                    )
                    result = (f"Aperçu affiché à l'utilisateur pour confirmation avant envoi "
                              f"(ne dis pas que c'est envoyé) : {_plat} → {_recv} : {_txt}")

            elif name == "whatsapp_control":
                from core.zapzap_controller import control as zapzap_control
                result = await loop.run_in_executor(
                    None,
                    lambda: zapzap_control(
                        args.get("action", "status"), args.get("receiver", ""),
                        args.get("message", ""),
                    ),
                )

            elif name == "reminder":
                r = await loop.run_in_executor(None, lambda: reminder(parameters=args, response=None, player=self.ui))
                result = r or "Reminder set."

                _action = str(args.get("action", "set") or "set").lower()
                if _action in ("set", "add", "create", "list") and r and not r.startswith(("Aucun", "La date")):
                    await loop.run_in_executor(None, self._show_active_reminders_card)

            elif name == "timer":
                action = str(args.get("action") or "set").lower()
                if action in {"set", "add", "create"}:
                    seconds = int(float(args.get("minutes") or 0) * 60)
                    if seconds < 1:
                        result = "Indique une durée positive en minutes."
                    else:
                        item = self._timers.create(str(args.get("name") or "Minuteur"), seconds)
                        result = f"Minuteur {item['name']} lancé pour {seconds // 60} minutes."
                elif action == "list":
                    active = self._timers.active()
                    result = "Aucun minuteur actif." if not active else "; ".join(
                        f"{t['name']} : {t['remaining'] // 60:02d}:{t['remaining'] % 60:02d}" for t in active)
                elif action == "cancel":
                    result = "Minuteur annulé." if self._timers.cancel(str(args.get("value") or args.get("name") or "")) else "Minuteur introuvable."
                else:
                    result = "Action minuteur inconnue."

            elif name == "youtube_video":
                r = await loop.run_in_executor(
                    None,
                    lambda: youtube_video(
                        parameters=args,
                        response=None,
                        player=self.ui,
                        session_memory=self._tool_session_memory,
                        speak=self.speak,
                    ),
                )
                result = r or "Done."

            elif name == "screen_process":
                import time as _t_mod
                _now = _t_mod.monotonic()
                _cooldown = 4.0  # seconds — covers echo window after speaking ends
                if self._vision_busy or (_now - self._vision_last_time) < _cooldown:
                    _wait = max(0, _cooldown - (_now - self._vision_last_time))
                    print(f"[Vision] ⏳ Cooldown active ({_wait:.1f}s remaining) — ignoring duplicate call")
                    result = "Vision is still processing the previous request. I will not call this again."
                else:
                    self._vision_busy      = True
                    self._vision_last_time = _now
                    angle     = args.get("angle", "screen").lower()
                    user_text = args.get("text", "What do you see?")
                    _skip_vision_pipeline = False
                    _meta = {}
                    if angle == "camera":
                        img_b, mime_t = await loop.run_in_executor(
                            None, self._grab_camera_still
                        )
                        self._vision_cam_active = True
                        print(f"[Vision] 📷 Camera: {len(img_b):,} bytes")
                        _stall = "camera"
                    else:
                        from core import screen_capture
                        img_b = mime_t = _meta = None
                        mind = getattr(self, "_screen_mind", None)
                        cached = mind.cached_capture(max_age_s=30.0) if mind is not None else None
                        if cached is not None and cached.webp_bytes:
                            img_b, mime_t, _meta = (
                                cached.webp_bytes, cached.mime_type, cached.as_meta()
                            )
                            print(
                                f"[Vision] 🖥️  Veille ({_meta.get('window_class', 'screen')}, "
                                f"{cached.age_s:.1f}s): {len(img_b):,} bytes"
                            )
                            if cached.ocr_usable and not screen_reader.wants_image(user_text):
                                self._pending_vision = None
                                self._vision_busy = False
                                result = cached.as_tool_result(user_text)
                                _skip_vision_pipeline = True
                        if not _skip_vision_pipeline:
                            if img_b is None:
                                img_b, mime_t, _meta = await loop.run_in_executor(
                                    None, lambda: screen_capture.capture_window_or_screen(target="active_window")
                                )
                            _win_cls = _meta.get("window_class", "screen")
                            print(f"[Vision] 🖥️  Active Window ({_win_cls}): {len(img_b):,} bytes")
                            _stall = "screen"

                    if not _skip_vision_pipeline:
                        def _show_vision_thumb():
                            try:
                                import base64, io
                                from PIL import Image
                                im = Image.open(io.BytesIO(img_b)).convert("RGB")
                                im.thumbnail((320, 200))
                                buf = io.BytesIO()
                                im.save(buf, format="JPEG", quality=70)
                                b64 = base64.b64encode(buf.getvalue()).decode("ascii")
                                label = "Caméra" if angle == "camera" else f"Écran ({_win_cls})"
                                body = (f"![aperçu](data:image/jpeg;base64,{b64})\n\n"
                                        f"_Ce que je regarde en ce moment — {user_text[:80]}_")
                                self.ui.show_card("info", f"👁 {label}", body)
                            except Exception as e:
                                print(f"[Vision] Miniature indisponible : {e}")

                        await loop.run_in_executor(None, _show_vision_thumb)

                        # Si la question porte sur un bug, une erreur ou un build échoué
                        _is_debug_q = any(k in user_text.lower() for k in ("bug", "erreur", "error", "plante", "crash", "traceback", "panic", "build", "echec", "échec"))
                        if angle != "camera" and _is_debug_q:
                            from core import auto_debug
                            _spoken, _diag = await loop.run_in_executor(
                                None, lambda: auto_debug.auto_debug_live(user_query=user_text, target_window="active_window", player=self.ui)
                            )
                            self._pending_vision = None
                            self._vision_busy    = False
                            result = f"[AUTO_DEBUG_DIRECT]\n{_diag.full_explanation}\n\nSynthèse vocale : {_spoken}"
                        else:
                            from core.multimodal_vision import (
                                detect_visual_domain,
                                inspect_screen_live,
                                should_use_expert_vision,
                            )
                            _read = None
                            if angle != "camera":
                                _read = await loop.run_in_executor(
                                    None, screen_reader.read, img_b, user_text
                                )
                            _win_info = None
                            try:
                                from core import screen_capture as _sc
                                _win_info = _sc.get_active_window(skip_anogpt=True)
                            except Exception:
                                _win_info = None
                            # Une image caméra n'a rien à voir avec la fenêtre
                            # active : un terminal au premier plan ne fait pas
                            # d'un visage une analyse de « code ».
                            _domain = detect_visual_domain(user_text, None if angle == "camera" else _win_info)
                            _ocr_ok = bool(_read is not None and _read.usable)
                            if (
                                not should_use_expert_vision(
                                    user_text,
                                    ocr_usable=_ocr_ok,
                                    domain=_domain,
                                    angle=angle,
                                )
                                and _ocr_ok
                            ):
                                self._pending_vision = None
                                self._vision_busy = False
                                result = _read.as_tool_result(user_text)
                            else:
                                def _expert():
                                    return inspect_screen_live(
                                        user_query=user_text,
                                        target="active_window",
                                        domain=_domain,
                                        player=self.ui,
                                        image_bytes=img_b,
                                        mime_type=mime_t,
                                        window_info=_win_info,
                                        metadata=_meta if isinstance(_meta, dict) else {},
                                        # Le répartiteur vient de tenter l'OCR :
                                        # « » (et non None) évite de relancer
                                        # Tesseract 8 s de plus quand il a échoué.
                                        extracted_text=(
                                            _read.text if _read is not None
                                            else ("" if angle != "camera" else None)
                                        ),
                                    )

                                try:
                                    _spoken, _diag = await loop.run_in_executor(None, _expert)
                                except Exception as _vis_exc:
                                    print(f"[Vision] analyse experte échouée : {_vis_exc}")
                                    _spoken, _diag = "", None
                                # À la caméra, la mémoire des visages dit qui est là :
                                # le modèle ne devine jamais une identité.
                                _faces = ""
                                if angle == "camera":
                                    try:
                                        from core.face_memory import faces_block_for_vision
                                        _faces = await loop.run_in_executor(
                                            None, faces_block_for_vision, img_b
                                        )
                                    except Exception as _face_exc:
                                        print(f"[Visages] bloc vision impossible : {_face_exc}")
                                _vision_failed = (
                                    _diag is None
                                    or not _diag.spoken_summary
                                    or "clé api" in _diag.spoken_summary.casefold()
                                    or "rencontré une difficulté" in _diag.spoken_summary.casefold()
                                )
                                if _faces and _vision_failed:
                                    # Gemini est indisponible mais la mémoire
                                    # locale, elle, a répondu : c'est la réponse.
                                    self._pending_vision = None
                                    self._vision_busy = False
                                    result = (
                                        f"{_faces}\n\nL'analyse de scène Gemini est indisponible "
                                        "pour l'instant, mais l'identification ci-dessus est fiable "
                                        "et TERMINÉE : réponds à partir d'elle (nomme la personne "
                                        "connue, ou demande qui c'est pour un inconnu). Ne rappelle "
                                        "pas screen_process."
                                    )
                                elif not _vision_failed:
                                    self._pending_vision = None
                                    self._vision_busy = False
                                    result = _diag.as_tool_result(user_text)
                                    if _faces:
                                        result += "\n\n" + _faces
                                elif _read is not None and _read.usable:
                                    # Le distant a lâché mais Tesseract a lu
                                    # l'écran : c'est une vraie réponse, pas
                                    # une panne à annoncer.
                                    self._pending_vision = None
                                    self._vision_busy = False
                                    result = _read.as_tool_result(user_text)
                                else:
                                    self._pending_vision = None
                                    self._vision_busy = False
                                    local_hint = ""
                                    if _read is not None and _read.text:
                                        local_hint = f"\n\nTexte local lisible :\n{_read.text[:2500]}"
                                    result = (
                                        f"[VISION_INDISPONIBLE] {_stall.capitalize()} capturé, "
                                        "mais aucun moteur distant n'a terminé l'analyse. "
                                        "Informe l'utilisateur en une phrase factuelle, sans inventer "
                                        "le contenu et sans rappeler automatiquement screen_process."
                                        + local_hint
                                    )

            elif name == "camera_control":
                result = await loop.run_in_executor(
                    None,
                    self._camera_tool,
                    (args.get("action") or "open").strip().lower(),
                    (args.get("source") or "").strip().lower(),
                    (args.get("lens") or "").strip().lower(),
                )

            elif name == "point_on_screen":
                desc = str(args.get("description") or "Élément ciblé")
                coords = args.get("coordinates") or []
                mode = str(args.get("mode") or "auto")
                dur = float(args.get("duration") or 3.0)
                result = self._agent_point_on_screen(desc, coords, mode, dur)

            elif name == "self_repair":
                # Une réparation peut réveiller dev_agent : elle a sa place dans
                # un exécuteur, pas sur la boucle qui porte la voix.
                result = await loop.run_in_executor(
                    None, self._agent_self_repair, args
                )

            elif name == "close_camera":
                # Le studio peut attendre son worker : hors de la boucle vocale.
                result = await loop.run_in_executor(None, self.close_all_cameras)

            elif name == "show_map":
                query = (args.get("query") or "").strip()
                radius_km = float(args.get("radius_km") or 3.0)
                lat_arg = args.get("lat")
                lon_arg = args.get("lon")
                _view_arg = str(args.get("view") or "").strip().casefold()
                view = "globe" if "globe" in _view_arg else (
                    "leaflet" if _view_arg else None
                )

                # « Ma position » exige une mesure actuelle : demander le GPS
                # au téléphone connecté avant de lire le fichier partagé.
                if not query and lat_arg is None and lon_arg is None and self._dashboard:
                    await self._dashboard.request_fresh_location(timeout=10.0)

                def _do_show_map():
                    if lat_arg is not None and lon_arg is not None:
                        lat, lon = float(lat_arg), float(lon_arg)
                        label = query or "position"
                    else:
                        from core.geolocation import geocode, get_precise_user_coords
                        if query:
                            coords = geocode(query)
                            label = query
                        else:
                            coords = get_precise_user_coords()
                            label = "votre position"
                        if not coords:
                            if not query:
                                return (
                                    "Je ne peux pas afficher votre position réelle sans un relevé GPS "
                                    "précis. Ouvrez le contrôle à distance sur votre téléphone "
                                    "et autorisez la localisation, puis réessayez. Je n'utiliserai pas "
                                    "Conakry ou la position IP comme si c'était votre position."
                                )
                            return f"Impossible de localiser « {query} » sur la carte."
                        lat, lon = coords
                    quartier = ""
                    if not query:
                        # « Voici ta position » sans le quartier n'a jamais de
                        # sens : le modèle n'avait alors que des coordonnées
                        # brutes et devinait « Conakry » depuis sa culture
                        # générale plutôt que de le dire précisément — et une
                        # question de suivi (« dans quel quartier ? ») partait
                        # en recherche web, qui ne peut évidemment pas savoir
                        # où l'utilisateur se trouve en ce moment.
                        from core.geolocation import reverse_geocode
                        place = reverse_geocode(lat, lon) or {}
                        quartier = str(place.get("city") or "")
                        if quartier:
                            label = f"{quartier}, {place.get('country_name') or 'votre position'}"
                    self.ui.show_map(label, lat, lon, radius_km, view=view)
                    style = {"globe": " (vue globe)", "leaflet": " (vue carte)"}.get(view, "")
                    where = f"quartier {quartier}, " if quartier else ""
                    return (
                        f"Carte affichée, centrée sur {label} ({where}coordonnées "
                        f"{lat:.4f}, {lon:.4f}){style}. Dis le quartier précis à "
                        f"l'utilisateur s'il est connu, pas seulement la ville."
                    )

                result = await loop.run_in_executor(None, _do_show_map)

            elif name == "show_country_info":
                country_query = (args.get("country") or "").strip()
                _view_arg = str(args.get("view") or "").strip().casefold()
                view = "globe" if "globe" in _view_arg else (
                    "leaflet" if _view_arg else None
                )

                def _do_show_country():
                    from core.country_info import fetch_country_info
                    info = fetch_country_info(country_query)
                    if info is None:
                        return (
                            f"Je ne trouve pas de pays correspondant à « {country_query} ». "
                            "Vérifie l'orthographe ou essaie le nom en anglais."
                        )
                    radius = max(200.0, (info.area_km2 or 1.0) ** 0.5 * 6.0)
                    self.ui.show_map(
                        info.name, info.lat, info.lon, radius, view=view,
                        country={
                            "flag": info.flag, "name": info.name,
                            "capital": info.capital,
                            "population": (
                                f"{info.population:,}".replace(",", " ")
                                if info.population else ""
                            ),
                            "currencies": info.currencies,
                            "languages": info.languages,
                            "timezone": info.timezone,
                            "area": (
                                f"{info.area_km2:,.0f} km²".replace(",", " ")
                                if info.area_km2 else ""
                            ),
                            "neighbors": info.neighbors,
                            "calling_code": info.calling_code,
                            "weather": (
                                f"{info.weather_emoji} {info.weather_text}, {info.temp_c:.0f}°C"
                                if info.temp_c is not None else ""
                            ),
                        },
                    )
                    return info.as_tool_result()

                result = await loop.run_in_executor(None, _do_show_country)

            elif name == "navigate":
                # Démarrer un guidage depuis une position IP ou périmée a déjà
                # renvoyé des distances à des milliers de km de la réalité :
                # même exigence de relevé frais qu'au premier « montre ma
                # position ». Un statut/arrêt n'a pas besoin de position.
                _nav_action = str(args.get("action") or "start").strip().lower()
                _needs_origin = _nav_action not in (
                    "stop", "cancel", "end", "close", "quitter", "arreter",
                    "status", "info", "state", "where", "prochaine",
                )
                _gps_ok = True
                if _needs_origin:
                    from core.geolocation import get_precise_user_coords
                    # Un relevé des 5 dernières minutes suffit — inutile de
                    # réveiller le téléphone si la position vient d'être
                    # utilisée (ex. juste après « montre ma position »).
                    _gps_ok = get_precise_user_coords(max_age_s=300.0) is not None
                    if not _gps_ok and self._dashboard:
                        # request_fresh_location rend False sans attendre le
                        # délai complet si aucun téléphone n'est connecté —
                        # inutile alors de patienter 10 s pour rien.
                        _gps_ok = await self._dashboard.request_fresh_location(timeout=10.0)
                        if _gps_ok:
                            _gps_ok = get_precise_user_coords(max_age_s=15.0) is not None

                if _needs_origin and not _gps_ok:
                    result = (
                        "Je n'ai aucune position GPS précise pour démarrer le guidage. "
                        "Ouvre ANO Remote sur ton téléphone, autorise la localisation, "
                        "puis redemande ; je n'utiliserai pas une position IP ou "
                        "ancienne comme point de départ."
                    )
                else:
                    r = await loop.run_in_executor(
                        None,
                        lambda: navigation_action(
                            parameters=args,
                            player=self.ui,
                            speak=self.speak,
                        ),
                    )
                    result = r or "Navigation initialisée."

            elif name == "find_nearby":
                # Chercher « autour de moi » exige de savoir où l'on est
                # maintenant : on redemande le GPS au téléphone avant de
                # chercher, comme pour « montre ma position ». Sans cela, des
                # distances au mètre près sont calculées depuis une position IP
                # vieille de plusieurs heures.
                around_user = not (args.get("near") or "").strip()
                fresh_location = False
                if around_user:
                    # Ne garde pas le micro fermé en attendant un téléphone :
                    # une mesure de plus de cinq minutes est trop ancienne
                    # pour annoncer des distances de proximité.
                    from core.geolocation import get_precise_user_coords
                    fresh_location = get_precise_user_coords(max_age_s=300.0) is not None

                if around_user and not fresh_location:
                    result = (
                        "Je n'ai aucune position GPS de moins de cinq minutes. Ouvre ANO "
                        "Remote, autorise la localisation, puis redemande ; je n'utiliserai "
                        "pas une position IP ou une ancienne position comme position précise."
                    )
                else:
                    lookup_args = dict(args)
                    if around_user:
                        lookup_args["_require_precise_gps"] = True
                        lookup_args["_max_location_age_s"] = 300.0

                    def _do_find_nearby():
                        return find_nearby(
                            parameters=lookup_args,
                            session_memory=self._tool_session_memory,
                            ui=self.ui,
                        )
                    result = await loop.run_in_executor(None, _do_find_nearby)

            elif name == "close_map":
                self.ui.close_map()
                result = "Carte fermée."

            elif name == "email_control":
                if self._start_deferred_tool(name, args):
                    result = ("Je traite les e-mails en arrière-plan. Dis-le immédiatement ; "
                              "le résultat sera annoncé dès qu'il sera prêt.")
                else:
                    result = "Un traitement d'e-mails est déjà en cours."

            elif name == "github_control":
                result = await loop.run_in_executor(None, lambda: github_control(args, ui=self.ui))

            elif name == "calendar_control":
                result = await loop.run_in_executor(None, lambda: calendar_control(args))

            elif name == "cloud_integrations_control":
                result = await loop.run_in_executor(None, lambda: cloud_integrations_control(args))

            elif name == "tiktok_tracker":
                result = await loop.run_in_executor(
                    None,
                    lambda: tiktok_tracker(parameters=args, player=self.ui, speak=self.speak),
                )

            elif name == "tiktok_coach" and str(args.get("action") or "").lower() in (
                "viral_video", "generate_video", "create_video", "make_video", "video_virale",
            ):
                # « Génère-moi une vidéo virale » : concept pensé pour CE compte,
                # puis la vidéo est réellement produite (Sora) en arrière-plan.
                from actions.tiktok_coach import viral_video_brief
                secs = int(args.get("seconds") or 12)
                brief = await loop.run_in_executor(
                    None, lambda: viral_video_brief(str(args.get("query") or ""), seconds=secs),
                )
                self._ui_card("show_card", "result", "Concept vidéo virale",
                              f"**{brief['concept']}**\n\n{brief['caption']}\n\n```text\n{brief['prompt']}\n```")
                launched = self._start_video_generation({"prompt": brief["prompt"], "seconds": brief["seconds"]})
                result = (
                    f"Concept retenu : {brief['concept']} Description prête : {brief['caption']}. "
                    + ("La VIDÉO est lancée avec Sora en arrière-plan (plusieurs minutes) : dis le concept "
                       "en une phrase, précise que la vidéo arrive toute seule, n'appelle aucun autre outil."
                       if launched else
                       "Une vidéo est déjà en cours de génération : dis-le, la nouvelle attendra.")
                )

            elif name == "tiktok_coach":
                result = await loop.run_in_executor(
                    None,
                    lambda: tiktok_coach(parameters=args, player=self.ui, speak=self.speak),
                )

            elif name in ("prayer", "prayer_control"):
                result = await loop.run_in_executor(None, lambda: prayer_control(args))

            elif name == "contacts_control":
                result = await loop.run_in_executor(None, lambda: contacts_control(args))

            elif name == "sparring_partner":
                result = sparring_partner(args, self._tool_session_memory)

            elif name == "focus_guard":
                result = await loop.run_in_executor(None, lambda: self._focus_guard.control(args))

            elif name == "phone_call":
                target = str(args.get("target") or "").strip()
                if not target:
                    result = "Indiquez le nom ou le numéro à appeler."
                elif self._dashboard is None:
                    result = "ANO-Remote est indisponible : aucun appel n'a été lancé."
                else:
                    outcome = await self._dashboard.request_phone_call(
                        target, selection=int(args.get("selection") or 0)
                    )
                    result = _render_phone_outcome(outcome, "Commande téléphonique traitée.")

            elif name == "phone_hangup":
                if self._dashboard is None:
                    result = "ANO-Remote est indisponible : impossible de raccrocher."
                else:
                    outcome = await self._dashboard.request_phone_hangup()
                    result = _render_phone_outcome(outcome, "Demande de raccrochage traitée.")

            elif name == "phone_contacts":
                query = str(args.get("query") or "").strip()
                if not query:
                    result = "Indiquez le nom ou la fin de numéro à chercher."
                elif self._dashboard is None:
                    result = ("ANO-Remote est indisponible : le carnet du téléphone "
                              "n'est pas consultable. Ne conclus pas que le contact "
                              "n'existe pas.")
                else:
                    outcome = await self._dashboard.request_phone_contacts(query)
                    result = _render_phone_outcome(
                        outcome, "Recherche de contacts traitée.",
                        choices_hint="Ce sont les contacts du téléphone.",
                    )

            elif name == "phone_sms":
                result = await self._send_phone_sms(args)

            elif name == "second_brain":
                result = await loop.run_in_executor(
                    None, lambda: self._agent_second_brain(args)
                )

            elif name == "computer_settings":
                r = await loop.run_in_executor(None, lambda: computer_settings(parameters=args, response=None, player=self.ui, session_memory=self._tool_session_memory))
                result = r or "Done."

            elif name == "desktop_control":
                r = await loop.run_in_executor(None, lambda: desktop_control(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "live_auto_debug":
                r = await loop.run_in_executor(
                    None, lambda: auto_debug_action(parameters=args, player=self.ui, speak=self.speak)
                )
                result = r or "Diagnostic d'erreur terminé."

            elif name == "code_helper":
                r = await loop.run_in_executor(None, lambda: code_helper(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "dev_agent":
                r = await loop.run_in_executor(None, lambda: dev_agent(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "web_search":
                r = await loop.run_in_executor(None, lambda: web_search_action(parameters=args, player=self.ui))
                result = r or "Done."
                # Mirror results to the on-screen content panel
                _mode = args.get("mode", "search")
                if r and not r.startswith("No results") and not r.startswith("Search failed"):
                    _query = args.get("query") or ", ".join(args.get("items", []))
                    _label = f"{_mode.upper()} — {_query[:38]}" if _query else _mode.upper()
                    self.ui.show_content(_label, r)
                    from actions.web_search import get_last_card_markdown
                    _md = get_last_card_markdown()
                    if _md:
                        self.ui.show_card("result", _label, _md)
            elif name == "image_search":
                r = await loop.run_in_executor(
                    None,
                    lambda: image_search_action(parameters=args, player=self.ui),
                )
                result = r or "Aucune image trouvée."
            elif name == "generate_image":
                if self._start_image_generation(args):
                    result = (
                        "La génération de l'image est lancée. Dis-le immédiatement "
                        "en français à l'utilisateur ; il recevra un point d'avancement "
                        "puis une annonce lorsque le fichier sera prêt."
                    )
                else:
                    result = (
                        "Une image est déjà en cours de création. Dis-le en français, "
                        "sans relancer la génération."
                    )
            elif name == "show_last_generated_image":
                result = ("J'ouvre l'image créée dans le visionneur intégré."
                          if self.ui.show_last_generated_image()
                          else "Aucune image créée n'est disponible dans cette session.")
            elif name == "generate_video":
                if self._start_video_generation(args):
                    result = (
                        "La génération de la vidéo est lancée. Dis-le immédiatement "
                        "en français à l'utilisateur ; il recevra un point d'avancement "
                        "puis une annonce lorsque le fichier sera prêt."
                    )
                else:
                    result = (
                        "Une vidéo est déjà en cours de création. Dis-le en français, "
                        "sans relancer la génération."
                    )
            elif name == "generate_document":
                if self._start_deferred_tool(name, args):
                    result = "La rédaction est lancée. Dis-le immédiatement ; le document sera annoncé dès qu'il sera prêt."
                else:
                    result = "Un document est déjà en cours de rédaction."
            elif name == "close_image_gallery":
                self.ui.close_image_gallery()
                result = "Galerie d'images fermée."
            elif name == "file_processor":
                if not args.get("file_path") and self.ui.current_file:
                    args["file_path"] = self.ui.current_file
                r = await loop.run_in_executor(
                    None,
                    lambda: file_processor(parameters=args, player=self.ui, speak=self.speak)
                )
                result = r or "Done."

            elif name == "computer_control":
                r = await loop.run_in_executor(None, lambda: computer_control(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "game_updater":
                r = await loop.run_in_executor(None, lambda: game_updater(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "flight_finder":
                r = await loop.run_in_executor(None, lambda: flight_finder(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "media_control":
                r = await loop.run_in_executor(None, lambda: media_control(parameters=args, player=self.ui))
                result = r or "Contrôle média exécuté."

            elif name == "capture_control":
                capture_action = str(args.get("action") or "").casefold()
                spoken = getattr(self, "_live_user_text", "")
                if (capture_action == "start_recording"
                        and not _has_explicit_capture_intent(spoken)):
                    result = (
                        "Enregistrement non lancé : la phrase reconnue ne demande pas "
                        "clairement une capture d'écran. Demande à l'utilisateur de "
                        "préciser s'il veut réellement enregistrer l'écran."
                    )
                else:
                    r = await loop.run_in_executor(
                        None,
                        lambda: capture_control(parameters=args, player=self.ui,
                                                session_memory=self._tool_session_memory),
                    )
                    result = r or "Capture effectuée."

            elif name == "visual_recognition":
                from actions.visual_recognition import visual_recognition
                r = await loop.run_in_executor(
                    None,
                    lambda: visual_recognition(parameters=args, player=self.ui,
                                               session_memory=self._tool_session_memory,
                                               speak=self.speak,
                                               grab_frame=self._grab_camera_still,
                                               save_photo=self._save_capture),
                )
                result = r or "Je n'ai rien reconnu."

            elif name == "music_recognition":
                from actions.music_recognition import music_recognition
                r = await loop.run_in_executor(
                    None,
                    lambda: music_recognition(parameters=args, player=self.ui,
                                              session_memory=self._tool_session_memory),
                )
                result = r or "Je n'ai pas reconnu la musique."

            elif name == "music_control":
                r = await loop.run_in_executor(
                    None,
                    lambda: music_control(parameters=args, player=self.ui,
                                          session_memory=self._tool_session_memory),
                )
                result = r or "Lecture lancée."

            elif name == "download_music":
                if self._start_deferred_tool(name, args):
                    result = ("Je prépare le téléchargement en arrière-plan. Dis-le immédiatement ; "
                              "je confirmerai le lancement dès que la recherche sera terminée.")
                else:
                    result = "Un téléchargement est déjà en cours de préparation."

            elif name == "background_tasks":
                result = self._background_tasks_control(args)

            elif name == "voice_style":
                result = self._agent_voice_style(args)

            elif name == "proactive_mode":
                action = str(args.get("action") or "status").strip().casefold()
                if action in {"silence", "silent", "off", "0"}:
                    state = self._proactive.set_silent(True)
                elif action in {"on", "actif", "active", "1"}:
                    state = self._proactive.set_silent(False)
                elif action in {"set_home", "home", "maison", "domicile"}:
                    from core.geolocation import get_live_position

                    position = get_live_position(resolve_place=False)
                    if not position:
                        result = (
                            "Position GPS indisponible. Ouvre ANO Remote sur le "
                            "téléphone puis réessaie."
                        )
                    else:
                        self._proactive.set_home(
                            position["lat"], position["lon"],
                            float(args.get("radius_m") or 250),
                        )
                        result = "Cette position est maintenant ton domicile."
                    state = None
                else:
                    state = "silence" if self._proactive.silent else "actif"
                if state is not None:
                    result = f"Mode proactif : {state}."

            elif name == "system_status":
                r = await loop.run_in_executor(None, get_system_status)
                result = str(r)

            elif name == "undo_action":
                if str(args.get("action") or "undo").casefold() == "list":
                    entries = undo_stack.history()
                    result = ("Actions annulables, de la plus récente à la plus ancienne :\n- "
                              + "\n- ".join(entries)) if entries else "Aucune action annulable."
                else:
                    result = await loop.run_in_executor(None, undo_stack.undo_last)

            elif name == "capability_guide":
                payload = dict(args)
                if not str(payload.get("query") or "").strip():
                    payload["query"] = str(getattr(self, "_live_user_text", "") or "")
                result = await asyncio.to_thread(
                    capability_guide_action, parameters=payload, player=self.ui,
                )

            elif name == "plugin_manager":
                action = str(args.get("action") or "list").casefold()
                plugin_name = str(args.get("name") or "").strip()
                if action == "reload":
                    self._plugins.discover()
                    self._refresh_plugin_runtime()
                    result = "Plugins rescannés. Les déclarations seront actualisées à la prochaine reconnexion."
                elif action in {"enable", "disable"}:
                    changed = self._plugins.set_enabled(plugin_name, action == "enable")
                    if changed:
                        self._refresh_plugin_runtime()
                    result = (("Plugin activé" if action == "enable" else "Plugin désactivé")
                              + f" : {plugin_name}. La modification prendra effet à la prochaine reconnexion.") \
                             if changed else f"Plugin inconnu : {plugin_name}."
                else:
                    rows = self._plugins.status()
                    if not rows:
                        result = "Aucun plugin installé dans le dossier plugins/."
                    else:
                        lines = []
                        for row in rows:
                            if row["error"]:
                                state = "ERREUR"
                            elif row["enabled"]:
                                state = "ACTIF"
                            elif row.get("needs_approval"):
                                state = "EN ATTENTE D'APPROBATION (code modifié ou jamais activé)"
                            else:
                                state = "INACTIF"
                            lines.append(f"- {row['name']} — {state}" + (f" : {row['error']}" if row["error"] else ""))
                        result = "Plugins ANO-GPT :\n" + "\n".join(lines)
                        self.ui.show_card("info", "Plugins ANO-GPT", result)

            elif name == "auto_extension_control":
                from core.auto_extension import get_auto_extension_manager
                result = get_auto_extension_manager().control(
                    str(args.get("action") or "list").casefold(),
                    str(args.get("id") or ""),
                    plugins=self._plugins,
                    refresh=self._refresh_plugin_runtime,
                )
                self.ui.show_card("info", "Extensions autonomes", result)

            elif name == "report_capability_gap":
                request_text = str(args.get("request") or "")
                # Repêchage des outils par contexte : le modèle se croit démuni
                # alors que l'outil existe, simplement dans un paquet fermé.
                # Rouvrir vaut mieux que journaliser une lacune imaginaire.
                extend = getattr(self, "_extend_toolkit", None)
                if callable(extend) and extend(request_text, origin="lacune signalée"):
                    result = ("[OUTILS_ELARGIS] Les outils de ce domaine viennent d'être "
                              "activés. La demande est relancée automatiquement : ne "
                              "signale aucune lacune.")
                else:
                    from core.auto_extension import get_auto_extension_manager
                    need = get_auto_extension_manager().record_unmet(
                        request_text, str(args.get("reason") or "outil absent"),
                    )
                    result = (f"Lacune enregistrée dans le groupe {need.id}." if need
                              else "Lacune non enregistrée : demande insuffisante.")

            elif name == "shell_exec":
                if _is_global_volume_shell_command(args):
                    requested = str(getattr(self, "_live_user_text", "") or "")
                    if _media_volume_request(requested) and not _explicit_system_volume_request(requested):
                        relative = _relative_volume_value_from_shell(args)
                        if re.search(r"\b(?:vid[eé]o|youtube|film|clip)\b", requested, re.IGNORECASE):
                            result = await loop.run_in_executor(
                                None,
                                lambda: youtube_video(
                                    {"action": "volume", "volume": relative},
                                    player=self.ui, session_memory=self._tool_session_memory,
                                ),
                            )
                        else:
                            result = await loop.run_in_executor(
                                None,
                                lambda: music_control(
                                    {"action": "volume", "value": relative},
                                    player=self.ui, session_memory=self._tool_session_memory,
                                ),
                            )
                    else:
                        system_args = _system_volume_args_from_shell(args)
                        result = await loop.run_in_executor(
                            None,
                            lambda: computer_settings(
                                parameters=system_args, response=None, player=self.ui,
                                session_memory=self._tool_session_memory,
                            ),
                        )
                else:
                    r = await loop.run_in_executor(None, lambda: shell_exec(parameters=args, player=self.ui))
                    result = r or "Commande exécutée."
                if result.startswith("[NEEDS_CONFIRM] "):
                    _cmd = result[len("[NEEDS_CONFIRM] "):]
                    self.ui.show_card(
                        "confirmation", "Commande risquée",
                        f"```\n{_cmd}\n```\nCette commande peut avoir des effets difficiles à annuler. L'exécuter ?",
                        [
                            {"label": "Oui, exécuter", "primary": True,
                             "callback": (lambda: self.ui.on_text_command(f"confirme la commande : {_cmd}")
                                          if self.ui.on_text_command else None)},
                            {"label": "Annuler",
                             "callback": (lambda: self.ui.on_text_command("annule, ne fais pas ça")
                                          if self.ui.on_text_command else None)},
                        ],
                    )
                    result = (f"Commande risquée détectée, confirmation demandée à l'utilisateur "
                              f"via une carte (ne l'exécute pas sans son accord explicite) : {_cmd}")

            elif name == "hypr_control":
                r = await loop.run_in_executor(None, lambda: hypr_control(parameters=args, player=self.ui))
                result = r or "Action Hyprland effectuée."

            elif name == "devsecops":
                r = await loop.run_in_executor(None, lambda: devsecops_control(parameters=args, player=self.ui))
                result = r or "Opération DevSecOps effectuée."

            elif name == "hypr_orchestrator":
                r = await loop.run_in_executor(None, lambda: hypr_orchestrator_control(parameters=args, player=self.ui))
                result = r or "Organisation Hyprland terminée."

            elif name == "shutdown_jarvis":
                requested = str(getattr(self, "_live_user_text", "") or "").casefold()
                if "veille" in requested or "repos" in requested:
                    # Filet final, y compris si une transcription a contourné
                    # l'interception locale : « veille » ne ferme jamais ANO.
                    sleeper = getattr(self, "_sleep", None)
                    if callable(sleeper):
                        sleeper("commande vocale")
                    result = "ANO-GPT est en veille ; l'application reste ouverte."
                    return types.FunctionResponse(
                        id=fc.id, name=name, response={"result": result}
                    )
                self.ui.write_log("SYS: Shutdown requested.")
                from core.personality_modes import user_address
                self.speak(f"Au revoir, {user_address()}.")
                # Une seconde pour laisser partir l'adieu, puis sortie : sur
                # la boucle elle-même, sans thread ni sommeil bloquant.
                import os as _os
                loop.call_later(1.0, _os._exit, 0)

            else:
                if name in self._plugins.plugins:
                    result = await loop.run_in_executor(
                        None,
                        lambda: self._plugins.run(
                            name, args, player=self.ui,
                            session_memory=self._tool_session_memory,
                        ),
                    )
                else:
                    result = f"Outil inconnu: {name}"

        except asyncio.CancelledError:
            self._task_card(task_id, f"{label} annulée", "Interrompue par l'utilisateur.", "error")
            raise
        except Exception as e:
            task_ok = False
            result = f"Tool '{name}' failed: {e}"
            traceback.print_exc()
            self.speak_error(name, e)
        finally:
            if task_ok:
                failed = _looks_like_failure(result)
                self._task_card(
                    task_id,
                    f"{label} — échec" if failed else f"{label} terminée",
                    _task_result_excerpt(result), "error" if failed else "done",
                )
            else:
                self._task_card(task_id, f"{label} — échec", _task_result_excerpt(result), "error")

        if not self.ui.muted:
            self.ui.set_state("LISTENING")

        print(f"[JARVIS] 📤 {name} → {str(result)[:80]}")
        return types.FunctionResponse(
            id=fc.id, name=name,
            response={"result": result}
        )

    def _agent_tools(self) -> dict:
        """Table des outils qu'un agent externe peut exécuter.

        Chaque valeur prend le dictionnaire d'arguments et rend le texte que
        l'agent lira. Ces fonctions tournent dans un fil d'exécution (voir
        `_tool` dans `_start_control_server`), jamais sur la boucle asyncio :
        elles ont donc le droit de bloquer.

        Le contrat est volontairement plat — des chaînes en entrée, une chaîne
        en sortie. Un agent lit du texte ; lui rendre une structure l'obligerait
        à la reformater avant de la restituer.
        """
        if self._agent_tool_table is None:
            self._agent_tool_table = {
                # ── le cœur visuel ──────────────────────────────────────────
                "find_nearby": self._agent_find_nearby,
                "show_map": self._agent_show_map,
                "close_map": lambda a: (self.ui.close_map(), "Carte fermée.")[1],
                "camera": self._agent_camera,
                "show_card": self._agent_show_card,
                # ── voix et état ────────────────────────────────────────────
                "speak": self._agent_speak,
                "ask": self._agent_ask,
                "assistant_status": self._agent_status,
                # ── données personnelles ────────────────────────────────────
                "email": self._agent_email,
                "calendar": lambda a: calendar_control(a),
                "cloud_integrations": lambda a: cloud_integrations_control(a),
                "contacts": lambda a: contacts_control(a),
                "memory_save": self._agent_memory_save,
                "memory_search": self._agent_memory_search,
                "second_brain": self._agent_second_brain,
                # ── machine et médias ───────────────────────────────────────
                "weather": self._agent_weather,
                "web_search": self._agent_web_search,
                "image_search": self._agent_image_search,
                "close_image_gallery": lambda a: (
                    self.ui.close_image_gallery(), "Galerie d'images fermée."
                )[1],
                "screenshot": self._agent_screenshot,
                "open_app": self._agent_open_app,
                "close_app": self._agent_close_app,
                "computer_settings": self._agent_computer_settings,
                "file_search": self._agent_file_search,
                "location": self._agent_location,
                "youtube": self._agent_youtube,
                "reminder": self._agent_reminder,
                "system_status": self._agent_system_status,
                "music": self._agent_music,
                "download_music": self._agent_download_music,
                "routine": self._agent_routine,
                "voice_id": self._agent_voice_id,
                "voice_style": self._agent_voice_style,
                "self_repair": self._agent_self_repair,
                "background_tasks": self._background_tasks_control,
                "devsecops": lambda a: devsecops_control(a),
                "hypr_orchestrator": lambda a: hypr_orchestrator_control(a),
                "auto_debug": lambda a: auto_debug_action(a, player=self.ui, speak=self.speak),
                "inspect_screen": lambda a: self._agent_inspect_screen(a),
                "navigate": lambda a: navigation_action(a, player=self.ui, speak=self.speak),
                "point_on_screen": lambda a: self._agent_point_on_screen(a),
                "search_personal_docs": self._agent_search_personal_docs,
                "prayer": lambda a: prayer_control(a),
            }
        return self._agent_tool_table

    @staticmethod
    def _arg(args: dict, key: str, default: str = "") -> str:
        value = args.get(key, default)
        return "" if value is None else str(value).strip()

    def _agent_find_nearby(self, args: dict) -> str:
        """Cherche des lieux ET les épingle dans la grande carte."""
        from actions.find_nearby import find_nearby

        return find_nearby(
            {
                "query": self._arg(args, "query"),
                "near": self._arg(args, "near"),
                "radius_km": args.get("radius_km") or 5.0,
            },
            session_memory=self._tool_session_memory,
            ui=self.ui,
        )

    def _agent_show_map(self, args: dict) -> str:
        query = self._arg(args, "query")
        radius = float(args.get("radius_km") or 3.0)
        lat, lon = args.get("lat"), args.get("lon")
        if lat is not None and lon is not None:
            self.ui.show_map(query or "Position", float(lat), float(lon), radius)
            return f"Carte centrée sur {lat}, {lon}."
        if not query:
            return "Précisez un lieu, ou des coordonnées lat/lon."
        from core.geolocation import geocode

        coords = geocode(query)
        if not coords:
            return f"Impossible de situer « {query} »."
        self.ui.show_map(query, coords[0], coords[1], radius)
        return f"Carte centrée sur {query}."

    def _agent_camera(self, args: dict) -> str:
        return self._camera_tool(
            self._arg(args, "action", "open").lower(),
            self._arg(args, "source").lower(),
            self._arg(args, "lens").lower(),
        )

    def _agent_show_card(self, args: dict) -> str:
        title = self._arg(args, "title") or "Agent"
        self.ui.show_card(
            self._arg(args, "type", "info").lower() or "info",
            title,
            self._arg(args, "body"),
        )
        return f"Carte « {title} » affichée."

    def _agent_speak(self, args: dict) -> str:
        """Fait prononcer une phrase par ANO-GPT.

        Le texte traverse la session Gemini Live, qui possède la voix : il est
        donc reformulé au passage, pas lu mot pour mot.
        """
        text = self._arg(args, "text")
        if not text:
            return "Rien à dire : le texte est vide."
        if not self.session:
            return "La session vocale n'est pas active : rien n'a été prononcé."
        self.speak(
            "[AGENT] Dis ceci à l'utilisateur, dans sa langue et sans appeler "
            f"le moindre outil : {text}"
        )
        return "Énoncé transmis à la voix."

    def _agent_ask(self, args: dict) -> str:
        """Envoie une demande à l'assistant vocal, qui décidera lui-même."""
        text = self._arg(args, "text")
        if not text:
            return "Rien à demander : le texte est vide."
        if not self.session:
            return "La session vocale n'est pas active."
        self._wake_up("agent")
        self._on_text_command(text)
        return f"Demande transmise à l'assistant : {text[:80]}"

    def _agent_status(self, args: dict) -> str:
        camera = self._camera
        continuous_active = bool(getattr(self, "_continuous", None) and self._continuous.is_active)
        if self.ui.muted:
            micro = "coupé"
        elif continuous_active:
            micro = "écoute continue"
        else:
            micro = "à l'écoute"
        session = "active" if self.session else "inactive"
        if camera is not None and camera.active:
            optique = f"ouverte ({camera.source}/{camera.lens})"
        else:
            optique = "fermée"
        policy = getattr(self, "_live_models", None)
        model = str(getattr(policy, "current", LIVE_MODEL)).removeprefix("models/")
        precision = (
            "active" if getattr(self, "_precision_stt_enabled", True)
            else "désactivée"
        )
        return (
            f"micro={micro} · session={session} · caméra={optique} · "
            f"Gemini Live={model} · vérification 3.5={precision}"
        )

    def _agent_email(self, args: dict) -> str:
        from actions.email import email_control

        parameters = dict(args)
        parameters.update({
            "action": self._arg(args, "action", "unread").lower() or "unread",
            "query": self._arg(args, "query"),
            "id": self._arg(args, "id"),
            "max_results": args.get("max_results") or 10,
        })
        return email_control(
            parameters,
            session_memory=self._tool_session_memory,
            ui=self.ui,
        )

    def _agent_memory_save(self, args: dict) -> str:
        from memory.memory_manager import remember

        key = self._arg(args, "key")
        value = self._arg(args, "value")
        if not key or not value:
            return "Précisez la clé et la valeur à mémoriser."
        category = self._arg(args, "category", "notes") or "notes"
        remember(key, value, category)
        return self._store_memory(category, key, value)

    def _agent_memory_search(self, args: dict) -> str:
        query = self._arg(args, "query")
        if query:
            rows = memory_store.search(query, limit=6)
            if rows:
                return "\n".join(
                    f"- {(r['key'] or '').replace('_', ' ') or r['kind']} : {r['value']}"
                    for r in rows
                )
            return f"Rien en mémoire à propos de « {query} »."

        # Sans terme de recherche, on rend le socle : profil, faits récents et
        # dernières conversations — de quoi situer sans tout déverser.
        summary = memory_store.session_block(max_chars=2500)
        return summary.strip() or "Aucun souvenir enregistré."

    def _agent_second_brain(self, args: dict) -> str:
        from actions.second_brain import second_brain_action

        return second_brain_action(args, player=self.ui, speak=self.speak)

    def _agent_voice_id(self, args: dict) -> str:
        """Outil `voice_id` : apprendre, oublier ou interroger l'empreinte."""
        action = (self._arg(args, "action", "status") or "status").lower()
        if getattr(self, "_speaker", None) is None:
            self._speaker = speaker_id.SpeakerID()
        speaker = self._speaker

        if not speaker.available:
            return ("La reconnaissance vocale n'est pas installée : il manque "
                    "le modèle (scripts/install_speaker_id.sh).")

        if action == "forget":
            self._speaker_verdict = None
            self._speaker_announced = None
            return speaker.forget()

        if action in ("enroll", "learn", "apprendre"):
            clips = list(self._voice_clips)
            if len(clips) < 2:
                return ("Il me faut deux ou trois phrases avant d'apprendre "
                        "une voix. Parle-moi encore un peu, puis redemande.")
            name = self._arg(args, "name") or "Anonymous"
            answer = speaker.enroll(clips[-3:], name)
            self._speaker_verdict = None
            self._speaker_announced = None
            return answer

        if not speaker.enrolled:
            return "Aucune voix n'est enregistrée pour l'instant."
        verdict = self._speaker_verdict
        if verdict is None:
            return f"Voix enregistrée : {speaker.name}. Rien de vérifié pour l'instant."
        return (f"Voix enregistrée : {speaker.name}. Dernier tour : "
                f"{'reconnu' if verdict.known else 'voix inconnue'} "
                f"(score {verdict.score:.2f}).")

    def _agent_voice_style(self, args: dict) -> str:
        """Lit ou change la préférence durable d'élocution."""
        from core.prosody import get_prosody_manager

        manager = get_prosody_manager()
        style = self._arg(args, "style")
        if not style:
            labels = {
                "professional": "professionnel",
                "stark": "Tony Stark",
                "synthetic": "ultra-synthétique",
            }
            label = labels[manager.preferred_style()]
            return f"Style vocal actuel : {label}."
        try:
            label = manager.set_preferred_style(style)
        except ValueError as exc:
            return str(exc)
        # Force la prochaine directive, même si le profil acoustique ne change pas.
        self._prosody_mode = ""
        return f"Style vocal {label} activé."
    def _dispatch_agent_tool(self, name: str, args: dict) -> str:
        """Appelle un outil de l'assistant par son nom, comme le ferait l'agent."""
        handler = self._agent_tools().get(name)
        if handler is None:
            return f"outil inconnu : {name}"
        return handler(dict(args or {}))

    def _agent_routine(self, args: dict) -> str:
        """Outil `routine` : exécute une routine nommée, ou liste les routines."""
        name = self._arg(args, "name")
        if not name:
            available = routines.names()
            return ("Routines disponibles : " + ", ".join(available)
                    if available else "Aucune routine définie.")
        found = routines.match(name)
        if found is None:
            available = routines.names()
            return (f"Aucune routine « {name} ». "
                    + ("Disponibles : " + ", ".join(available) if available
                       else "Le fichier config/routines.yaml est vide."))
        # La voix appelle déjà cet outil : elle annoncera elle-même le résultat,
        # inutile de lui faire répéter la phrase de la routine.
        self._run_routine(found, announce=False)
    def _agent_self_repair(self, args: dict) -> str:
        from actions.self_repair import self_repair

        return self_repair(
            parameters={
                "action": self._arg(args, "action", "diagnose") or "diagnose",
                "tool": self._arg(args, "tool"),
            },
            player=self.ui,
            # La réparation tourne en fond : c'est par `speak` que son résultat
            # est annoncé plus tard. Le diagnostic, lui, ne parle pas deux fois.
            speak=self.speak if (self._arg(args, "action", "") or "").lower() in
                  {"repair", "reparer", "fix", "corriger", "corrige", "restart"} else None,
            session_memory=self._tool_session_memory,
        )
    def _agent_weather(self, args: dict) -> str:
        from actions.weather_report import get_last_weather_card, weather_report

        result = weather_report({
            "city": self._arg(args, "city"),
            "time": self._arg(args, "time", "aujourd'hui") or "aujourd'hui",
        })
        # Ce chemin est employé par les raccourcis/agents locaux ; il doit
        # offrir la même carte que l'appel d'outil Gemini classique.
        card = get_last_weather_card()
        if card and getattr(self, "ui", None) is not None:
            self.ui.show_card("result", "Météo", card)
        return result

    def _agent_web_search(self, args: dict) -> str:
        from actions.web_search import web_search

        return web_search({
            "query": self._arg(args, "query"),
            "mode": self._arg(args, "mode", "search") or "search",
            "platform": self._arg(args, "platform"),
            "items": args.get("items") or [],
            "aspect": self._arg(args, "aspect", "general") or "general",
            "count": args.get("count") or 5,
        }, player=self.ui, session_memory=self._tool_session_memory)

    def _agent_image_search(self, args: dict) -> str:
        from actions.image_search import image_search

        return image_search({
            "query": self._arg(args, "query"),
            "limit": args.get("limit") or 6,
        }, player=self.ui)

    def _agent_screenshot(self, args: dict) -> str:
        from actions.capture import capture_control

        return capture_control({
            "action": self._arg(args, "action", "screenshot") or "screenshot",
            "path": self._arg(args, "path") or None,
            "monitor": self._arg(args, "monitor") or None,
            "window": self._arg(args, "window") or None,
            "copy_clipboard": args.get("copy_clipboard", True),
            "annotate": args.get("annotate", False),
            "delay": args.get("delay") or 0,
        }, player=self.ui, session_memory=self._tool_session_memory)

    def _agent_inspect_screen(self, args: dict) -> str:
        query = self._arg(args, "query") or "Analyse ce qui est affiché à l'écran."
        domain = self._arg(args, "domain", "auto") or "auto"
        target = self._arg(args, "target", "active_window") or "active_window"
        spoken, diag = inspect_screen_live(
            user_query=query,
            target=target,
            domain=None if domain == "auto" else domain,
            player=self.ui,
        )
        return spoken

    def _agent_point_on_screen(
        self,
        args_or_desc: Union[dict, str],
        coordinates: list | None = None,
        mode: str = "auto",
        duration: float = 3.0,
    ) -> str:
        """Active l'overlay d'annotation visuelle sur écran (rectangle, laser ou trajectoire)."""
        from ui.visual_pointer import get_visual_pointer

        target = ""
        if isinstance(args_or_desc, dict):
            desc = str(args_or_desc.get("description") or "Élément ciblé")
            target = str(args_or_desc.get("target") or args_or_desc.get("widget") or "").strip()
            raw_coords = args_or_desc.get("coordinates")
            if isinstance(raw_coords, str):
                try:
                    import json
                    coords = json.loads(raw_coords)
                except Exception:
                    coords = []
            else:
                coords = list(raw_coords) if raw_coords else []
            m = str(args_or_desc.get("mode") or "auto")
            dur = float(args_or_desc.get("duration") or 3.0)

            # Les coordonnées générées par un modèle sont souvent relatives à
            # une image recadrée, pas au bureau virtuel. Les afficher telles
            # quelles pouvait placer le laser sur le mauvais écran ou dans un
            # coin sans rapport avec l'élément cité. Une cible textuelle est
            # donc toujours relocalisée sur une capture fraîche avant dessin.
            if coords and not target:
                target = desc
                coords = []
        else:
            desc = str(args_or_desc or "Élément ciblé")
            coords = coordinates or []
            m = mode
            dur = duration

        vp = getattr(self.ui, "visual_pointer", None) or get_visual_pointer()
        return vp.point_on_screen(desc, coords, mode=m, duration=dur, target=target or None)

    def _agent_open_app(self, args: dict) -> str:
        from actions.open_app import open_app

        return open_app(parameters={
            "app_name": self._arg(args, "app_name"),
            "command": self._arg(args, "command"),
            "target": self._arg(args, "target"),
            "description": self._arg(args, "description"),
            "workspace": self._arg(args, "workspace") or None,
            "hidden": bool(args.get("hidden", False)),
            "count": args.get("count"),
            "instance_name": self._arg(args, "instance_name"),
        }, response=None, player=self.ui,
            session_memory=getattr(self, "_tool_session_memory", {}))

    def _agent_close_app(self, args: dict) -> str:
        from actions.close_app import close_app

        return close_app(parameters={
            "app_name": self._arg(args, "app_name"),
            "description": self._arg(args, "description"),
            "workspace": self._arg(args, "workspace") or None,
            "force": bool(args.get("force", False)),
        }, response=None, player=self.ui,
            session_memory=getattr(self, "_tool_session_memory", {}))

    def _agent_computer_settings(self, args: dict) -> str:
        from actions.computer_settings import computer_settings

        return computer_settings(parameters={
            "action": self._arg(args, "action"),
            "description": self._arg(args, "description"),
            "value": self._arg(args, "value") or None,
            "confirmed": self._arg(args, "confirmed"),
        }, response=None, player=self.ui,
            session_memory=getattr(self, "_tool_session_memory", {}))

    def _agent_file_search(self, args: dict) -> str:
        from actions.file_controller import file_controller

        return file_controller(parameters={
            "action": "find",
            "name": self._arg(args, "name"),
            "extension": self._arg(args, "extension"),
            "path": self._arg(args, "path", "home") or "home",
            "kind": self._arg(args, "kind"),
            "max_results": args.get("max_results") or 20,
        }, response=None, player=self.ui,
            session_memory=getattr(self, "_tool_session_memory", {}))

    def _agent_search_personal_docs(self, args: dict) -> str:
        """Recherche sémantique et syntaxique dans les documents personnels et code source."""
        from core.personal_rag import search_personal_docs

        query = self._arg(args, "query")
        pattern = self._arg(args, "file_pattern")
        try:
            max_results = int(args.get("max_results") or 5)
        except (ValueError, TypeError):
            max_results = 5

        return search_personal_docs(
            query=query,
            file_pattern=pattern,
            max_results=max_results,
        )

    def _agent_location(self, args: dict) -> str:
        import json
        from core.geolocation import get_user_location

        refresh = self._arg(args, "refresh").lower() in {
            "1", "true", "yes", "oui",
        }
        location = get_user_location(force_refresh=refresh)
        return json.dumps(location, ensure_ascii=False, sort_keys=True)

    def _agent_youtube(self, args: dict) -> str:
        from actions.youtube_video import youtube_video

        return youtube_video(parameters={
            "action": self._arg(args, "action"),
            "query": self._arg(args, "query"),
            "url": self._arg(args, "url"),
            "region": self._arg(args, "region", "FR") or "FR",
            "value": self._arg(args, "value"),
        }, response=None, player=self.ui,
            session_memory=self._tool_session_memory, speak=self.speak)

    def _agent_reminder(self, args: dict) -> str:
        from actions.reminder import reminder

        return reminder(parameters={
            "action": self._arg(args, "action", "set") or "set",
            "description": self._arg(args, "description"),
            "date": self._arg(args, "date"),
            "time": self._arg(args, "time"),
            "message": self._arg(args, "message", "Rappel") or "Rappel",
            "value": self._arg(args, "value"),
        }, response=None, player=self.ui,
            session_memory=self._tool_session_memory)

    def _agent_system_status(self, args: dict) -> str:
        from actions.system_monitor import system_status_tool

        return system_status_tool({
            "action": self._arg(args, "action", "status") or "status",
            "component": self._arg(args, "component"),
        })

    def _agent_music(self, args: dict) -> str:
        from actions.music import music_control

        return music_control(
            {
                "action": self._arg(args, "action", "play").lower() or "play",
                "query": self._arg(args, "query"),
                "value": self._arg(args, "value"),
                "kind": self._arg(args, "kind", "audio") or "audio",
            },
            player=self.ui, session_memory=self._tool_session_memory,
        )

    def _agent_download_music(self, args: dict) -> str:
        from actions.download_music import download_music as _download

        return _download(
            {
                "query": self._arg(args, "query") or self._arg(args, "url"),
                "url": self._arg(args, "url"),
            },
            player=self.ui,
            speak=self.speak,
        )

    def _background_tasks_control(self, args: dict) -> str:
        if getattr(self, "_background_tasks", None) is None:
            publisher = (
                self._proactive.publish
                if hasattr(self, "_proactive") else (lambda *a, **k: False)
            )
            self._background_tasks = BackgroundTaskService(
                publisher, on_task_update=self._render_background_task,
            )
        action = self._arg(args, "action", "list").casefold() or "list"
        if action in {"list", "status", "statut"}:
            return format_tasks(self._background_tasks.list_tasks())
        if action in {"history", "historique"}:
            return format_tasks(self._background_tasks.list_tasks(include_finished=True))
        if action in {"result", "rapport", "résultat", "resultat"}:
            task_id = self._arg(args, "task_id") or self._arg(args, "id")
            return format_agent_result(self._background_tasks.agent_result(task_id))
        if action in {"cancel", "annuler", "delete"}:
            task_id = self._arg(args, "task_id") or self._arg(args, "id")
            return (
                f"Tâche {task_id} annulée."
                if self._background_tasks.cancel(task_id)
                else "Tâche active introuvable."
            )
        try:
            if action in {"watch_price", "price", "surveille_prix"}:
                url = self._arg(args, "url")
                if not url:
                    from actions.browser_control import current_browser_url

                    url = current_browser_url()
                if not url:
                    return (
                        "Je ne connais pas l'URL de cette page. Donne-moi son "
                        "adresse ou ouvre-la avec mon outil navigateur."
                    )
                target = args.get("target_price")
                task = self._background_tasks.add_price_watch(
                    url,
                    target_price=float(target) if target is not None else None,
                    interval_seconds=int(args.get("interval_minutes") or 15) * 60,
                    selector=self._arg(args, "selector"),
                    label=self._arg(args, "label"),
                )
                return f"Je surveille ce prix en arrière-plan ({task['id']})."
            if action in {"wait_build", "build", "wait_command"}:
                task = self._background_tasks.add_build_wait(
                    self._arg(args, "command_contains"),
                    message=self._arg(args, "message"),
                )
                return f"Je te préviendrai à la fin du build ({task['id']})."
            if action in {"wait_arrival", "arrival", "arrivee"}:
                task = self._background_tasks.add_arrival_wait(
                    self._arg(args, "message", "Tu es arrivé à la maison."),
                    radius_m=float(args.get("radius_m") or 250),
                    label=self._arg(args, "label", "maison"),
                )
                return f"Je te le rappellerai à l'arrivée ({task['id']})."
            if action in {"delegate", "ghost", "agent", "fantome", "fantôme"}:
                workspace = self._arg(args, "workspace") or str(BASE_DIR)
                task = self._background_tasks.add_agent_mission(
                    self._arg(args, "mission") or self._arg(args, "message"),
                    workspace=workspace,
                    timeout_minutes=int(args.get("timeout_minutes") or 60),
                    show_terminal=bool(args.get("show_terminal", False)),
                )
                return (
                    "Mode Agent Fantôme activé. Je continue de t'écouter pendant "
                    f"que le sous-agent travaille ({task['id']})."
                    + (" Son journal est visible dans Kitty." if args.get("show_terminal") else "")
                )
        except (TypeError, ValueError) as exc:
            return f"Impossible de créer cette veille : {exc}."
        return "Action de tâche de fond inconnue."

    @staticmethod
    def _background_card_title(task: dict) -> str:
        return f"Tâche en arrière-plan · {str(task.get('id') or '')[-8:]}"

    @staticmethod
    def _background_progress_bar(progress: int | None) -> str:
        if progress is None:
            return "[░░░░░░░░░░] En attente"
        value = max(0, min(100, int(progress)))
        filled = round(value / 10)
        return f"[{'█' * filled}{'░' * (10 - filled)}] {value}%"

    def _background_card_body(self, task: dict, *, detail: bool = False) -> str:
        spec = task.get("spec") if isinstance(task.get("spec"), dict) else {}
        state = task.get("state") if isinstance(task.get("state"), dict) else {}
        kind = str(task.get("kind") or "tâche")
        status = str(task.get("status") or "active")
        phase = str(state.get("phase") or "queued")
        mission = str(spec.get("mission") or spec.get("label") or spec.get("message") or kind)
        progress = state.get("progress") if kind == "agent" else None
        lines = [
            f"**{mission[:300]}**",
            f"État : **{phase if status == 'active' else status}**",
            self._background_progress_bar(progress),
        ]
        if detail:
            journal = str(state.get("log_tail") or "").strip()
            if journal:
                lines += ["", "**Sortie en temps réel**", "```text", journal[-6000:], "```"]
            elif kind == "agent":
                lines += ["", "L'agent prépare encore sa première sortie."]
            if task.get("summary"):
                lines += ["", "**Résultat**", str(task["summary"])]
            if task.get("report_path"):
                lines += ["", f"Rapport : `{task['report_path']}`"]
        return "\n".join(lines)

    def _render_background_task(self, task: dict) -> None:
        """Met à jour une carte persistante sans jamais bloquer le worker agy."""
        task_id = str(task.get("id") or "")
        if not task_id or not hasattr(self, "ui"):
            return
        title = self._background_card_title(task)
        status = str(task.get("status") or "active")
        if status != "active":
            # La carte de suivi ne doit vivre que pendant le travail. À la
            # fin, la sortie cyberpunk de CardManager libère la place pour la
            # tâche suivante ; le résultat arrive séparément dans le journal.
            self.ui.dismiss_cards("agent-task", title)
            self.ui.dismiss_cards("agent-task-detail", f"Détails · {task_id[-8:]}")
            getattr(self, "_background_cards", set()).discard(task_id)
            getattr(self, "_background_detail_cards", set()).discard(task_id)
            return
        body = self._background_card_body(task)
        shown = getattr(self, "_background_cards", set())
        if task_id not in shown:
            shown.add(task_id)
            self._background_cards = shown
            actions = [
                {
                    "label": "Détails",
                    "dismiss": False,
                    "callback": lambda ident=task_id: self._show_background_task_details(ident),
                },
                {
                    "label": "Annuler",
                    "callback": lambda ident=task_id: self._background_tasks.cancel(ident),
                },
            ]
            self.ui.show_card("agent-task", title, body, actions)
        else:
            self.ui.update_card("agent-task", title, body)
        if task_id in getattr(self, "_background_detail_cards", set()):
            self.ui.update_card("agent-task-detail", f"Détails · {task_id[-8:]}",
                                self._background_card_body(task, detail=True))

    def _show_background_task_details(self, task_id: str) -> None:
        service = getattr(self, "_background_tasks", None)
        if service is None:
            return
        task = next((item for item in service.list_tasks(include_finished=True)
                     if str(item.get("id")) == task_id), None)
        if task is None:
            return
        shown = getattr(self, "_background_detail_cards", set())
        title = f"Détails · {task_id[-8:]}"
        body = self._background_card_body(task, detail=True)
        if task_id in shown:
            self.ui.update_card("agent-task-detail", title, body)
            return
        shown.add(task_id)
        self._background_detail_cards = shown
        self.ui.show_card("agent-task-detail", title, body)
