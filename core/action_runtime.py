"""Runtime commun des actions ANO-GPT.

Ce module reste indépendant de Qt, Gemini et des actions concrètes afin d'être
testable rapidement. Il apporte au répartiteur central :

* validation/coercition des arguments d'après les déclarations envoyées au LLM ;
* correction de quelques alias naturels fréquemment produits par les modèles ;
* délais et limites de concurrence adaptés à chaque famille d'action ;
* circuit breaker contre les outils qui échouent en boucle ;
* historique de contexte borné, sans secrets, pour les demandes de suivi.
"""

from __future__ import annotations

import asyncio
import copy
import math
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, MutableMapping


class ActionRuntimeError(Exception):
    """Erreur contrôlée pouvant être renvoyée telle quelle au modèle."""


class ActionValidationError(ActionRuntimeError):
    pass


class ActionCircuitOpen(ActionRuntimeError):
    pass


class ActionQueueFull(ActionRuntimeError):
    pass


@dataclass(frozen=True)
class ActionPolicy:
    # Un outil qui ne rend rien en vingt secondes bloque la voix ET le micro
    # (half-duplex) : passé ce délai il est annulé, l'utilisateur en est
    # informé et l'écoute reprend. Les outils longs déclarent leur propre
    # plafond ci-dessous.
    timeout_s: float = 20.0
    max_concurrency: int = 1
    failure_threshold: int = 3
    cooldown_s: float = 30.0
    queue_timeout_s: float = 10.0
    max_pending: int = 16


_POLICIES: dict[str, ActionPolicy] = {
    # Un tour Live reste bloqué tant que son outil ne revient pas. Une minute
    # de recherche en cascade donne l'impression que l'assistant est mort et
    # empêche toute nouvelle phrase dans le mode half-duplex.
    "web_search": ActionPolicy(timeout_s=15.0, max_concurrency=1),
    "image_search": ActionPolicy(timeout_s=55.0),
    # GPT-Image peut prendre plus d'une minute, notamment au premier appel de
    # la ressource Foundry. Le couper à 55 s abandonnait le tour vocal alors
    # que la génération était encore en cours côté Azure.
    "generate_image": ActionPolicy(timeout_s=210.0),
    # Sora rend une vidéo en plusieurs minutes : le travail réel se fait en
    # tâche de fond, ce plafond ne couvre que l'accusé de réception.
    "generate_video": ActionPolicy(timeout_s=60.0),
    "generate_document": ActionPolicy(timeout_s=210.0),
    "weather_report": ActionPolicy(timeout_s=25.0, max_concurrency=2),
    # Dataset pays local (immédiat) + météo Open-Meteo + population Banque
    # mondiale, chacune bornée à 6 s ; large marge sous ce plafond.
    "show_country_info": ActionPolicy(timeout_s=20.0, max_concurrency=2),
    # Caelestia est borné à 7 s : garder 3 s pour le dispatcher et la copie
    # éventuelle du fichier sans retenir un tour Live trente secondes.
    "capture_control": ActionPolicy(timeout_s=10.0),
    "find_nearby": ActionPolicy(timeout_s=20.0),
    "email_control": ActionPolicy(timeout_s=45.0),
    "youtube_video": ActionPolicy(timeout_s=70.0),
    # Les contrôles de lecture restent synchrones : au-delà de vingt secondes,
    # il vaut mieux rendre l'écoute que laisser une commande MPRIS bloquée.
    "music_control": ActionPolicy(timeout_s=20.0),
    # Deux fenêtres d'écoute (9 s + 12 s) plus l'empreinte réseau.
    "music_recognition": ActionPolicy(timeout_s=50.0),
    # Téléchargement des modèles au premier appel (37 Mo), puis Gemini et
    # recherche web pour un objet : large, mais un seul à la fois.
    "visual_recognition": ActionPolicy(timeout_s=60.0, max_concurrency=1),
    "download_music": ActionPolicy(timeout_s=90.0),
    # Une lecture TikTok ouvre un Chrome headless : dix à quinze secondes.
    "tiktok_tracker": ActionPolicy(timeout_s=60.0),
    # Le bilan interroge Gemini en texte ; le visionnage, lui, part en fond.
    "tiktok_coach": ActionPolicy(timeout_s=75.0),
    "file_controller": ActionPolicy(timeout_s=75.0),
    "browser_control": ActionPolicy(timeout_s=60.0),
    "screen_process": ActionPolicy(timeout_s=35.0),
    "deep_think": ActionPolicy(timeout_s=90.0),
    # Le relais tient une boucle d'agent complète (plusieurs allers-retours
    # d'outils) pendant que Gemini garde le tool call ouvert. Large, mais borné
    # côté relais bien avant : ce plafond n'est qu'un filet.
    "consult_brain": ActionPolicy(timeout_s=100.0),
    "simulate_decision": ActionPolicy(timeout_s=20.0),
    "shell_exec": ActionPolicy(timeout_s=120.0),
    "open_app": ActionPolicy(timeout_s=20.0),
    "close_app": ActionPolicy(timeout_s=20.0),
    "whatsapp_control": ActionPolicy(timeout_s=25.0, max_concurrency=1),
    "devsecops": ActionPolicy(timeout_s=90.0),
    "hypr_orchestrator": ActionPolicy(timeout_s=25.0),
    "second_brain": ActionPolicy(timeout_s=40.0),
    "capability_guide": ActionPolicy(timeout_s=8.0, max_concurrency=1),
}


_ARG_ALIASES: dict[str, dict[str, str]] = {
    "open_app": {"app": "app_name", "application": "app_name", "name": "app_name"},
    "close_app": {"app": "app_name", "application": "app_name", "name": "app_name"},
    "devsecops": {"cmd": "action", "command": "action", "service": "target", "container": "target", "package": "target", "branch": "target"},
    "hypr_orchestrator": {"command": "action", "app": "target", "ws": "workspace"},
    "second_brain": {"text": "query", "search": "query", "q": "query", "cmd": "command", "cmd_text": "command"},
    "send_message": {
        "recipient": "receiver", "destinataire": "receiver", "contact": "receiver", "to": "receiver",
        "message": "message_text", "text": "message_text", "content": "message_text",
        "body": "message_text", "msg": "message_text", "contenu": "message_text",
        "service": "platform", "app": "platform",
    },
    "whatsapp_control": {
        "contact": "receiver", "recipient": "receiver", "destinataire": "receiver", "to": "receiver",
        "text": "message", "message_text": "message", "content": "message", "body": "message",
        "msg": "message", "contenu": "message", "command": "action",
    },
    "email_control": {
        "recipient": "to", "destinataire": "to", "recipients": "to",
        "message": "body", "text": "body", "message_text": "body",
        "command": "action", "recipient_filter": "to_filter",
    },
    "web_search": {"text": "query", "search": "query", "q": "query"},
    "image_search": {"text": "query", "search": "query", "q": "query"},
    "weather_report": {"location": "city", "ville": "city"},
    "find_nearby": {"text": "query", "search": "query"},
    "screen_process": {"question": "text", "query": "text"},
    "deep_think": {"query": "question", "text": "question"},
    "simulate_decision": {"query": "decision", "text": "decision"},
    "file_controller": {"command": "action", "file": "path"},
    "capability_guide": {
        "phrase": "query", "text": "query", "demande": "query",
        "mode": "action", "command": "action",
    },
    "browser_control": {"command": "action"},
    "computer_control": {"command": "action"},
    "music_control": {"command": "action", "text": "query"},
    "music_recognition": {"command": "action", "source": "target", "platform": "target"},
    "visual_recognition": {"command": "action", "text": "question", "who": "name",
                           "person": "name", "angle": "source"},
    "youtube_video": {"command": "action", "text": "query"},
}


_SECRET_KEY_RE = re.compile(r"(?:api.?key|token|password|secret|authorization|cookie)", re.I)
_ERROR_PREFIXES = (
    "tool '", "erreur:", "erreur ", "error:", "error ",
    "échec:", "échec ", "failed:", "failed ", "impossible de ",
    "timeout:", "timed out:",
)
_CIRCUIT_ERROR_PREFIXES = ("tool '", "timeout:", "timed out:")

# Seulement les lectures réellement indépendantes. Les actions d'interface,
# d'écriture et de sélection restent séquentielles car leur ordre est souvent
# porteur de sens (chercher puis sélectionner, agir puis vérifier, etc.).
_PARALLEL_SAFE_ACTIONS = frozenset({
    "web_search", "system_status", "weather_report", "deep_think",
    "simulate_decision",
})


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        folded = value.strip().lower()
        if folded in {"1", "true", "yes", "oui", "on"}:
            return True
        if folded in {"0", "false", "no", "non", "off"}:
            return False
    raise ValueError("booléen attendu")


def _coerce_value(
    value: Any,
    expected: str,
    field: str,
    schema: Mapping[str, Any] | None = None,
    depth: int = 0,
) -> Any:
    schema = schema or {}
    if depth > 12:
        raise ValueError("structure trop profonde")
    if value is None and schema.get("nullable"):
        return None
    kind = (expected or "").upper()
    if kind == "STRING":
        if value is None:
            return ""
        if isinstance(value, (dict, list, tuple, set)):
            raise ValueError("texte attendu")
        text = str(value).strip()
        if len(text) > min(schema.get("maxLength", 100_000), 100_000):
            raise ValueError("texte trop long")
        if len(text) < schema.get("minLength", 0):
            raise ValueError("texte trop court")
        return text
    if kind == "INTEGER":
        if isinstance(value, bool):
            raise ValueError("entier attendu")
        number = int(value)
        if isinstance(value, float) and not value.is_integer():
            raise ValueError("entier attendu")
        if "minimum" in schema and number < schema["minimum"]:
            raise ValueError(f"minimum {schema['minimum']}")
        if "maximum" in schema and number > schema["maximum"]:
            raise ValueError(f"maximum {schema['maximum']}")
        return number
    if kind == "NUMBER":
        if isinstance(value, bool):
            raise ValueError("nombre attendu")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("nombre fini attendu")
        if "minimum" in schema and number < schema["minimum"]:
            raise ValueError(f"minimum {schema['minimum']}")
        if "maximum" in schema and number > schema["maximum"]:
            raise ValueError(f"maximum {schema['maximum']}")
        return number
    if kind == "BOOLEAN":
        return _coerce_bool(value)
    if kind == "ARRAY":
        if isinstance(value, str):
            # Les modèles produisent parfois "a, b" pour un tableau simple.
            values = [part.strip() for part in value.split(",") if part.strip()]
        elif isinstance(value, (tuple, set)):
            values = list(value)
        elif isinstance(value, list):
            values = value
        else:
            raise ValueError("liste attendue")
        if len(values) > min(schema.get("maxItems", 1000), 1000):
            raise ValueError("liste trop longue")
        if len(values) < schema.get("minItems", 0):
            raise ValueError("liste trop courte")
        item_schema = schema.get("items") or {}
        if item_schema.get("type"):
            values = [
                _coerce_value(item, str(item_schema["type"]), field, item_schema, depth + 1)
                for item in values
            ]
            if item_schema.get("enum") and any(item not in item_schema["enum"] for item in values):
                raise ValueError("élément hors des valeurs autorisées")
        return values
    if kind == "OBJECT":
        if not isinstance(value, Mapping):
            raise ValueError("objet attendu")
        if len(value) > 100:
            raise ValueError("objet trop volumineux")
        properties = schema.get("properties")
        if properties is None:
            return dict(value)
        result = {}
        for key, item in value.items():
            child = properties.get(key)
            if child is None:
                if schema.get("additionalProperties") is False:
                    raise ValueError(f"champ inconnu : {key}")
                result[key] = item
                continue
            result[key] = _coerce_value(item, str(child.get("type", "")), str(key), child, depth + 1)
            if child.get("enum") and result[key] not in child["enum"]:
                raise ValueError(f"{key}: valeur non autorisée")
        missing = [key for key in schema.get("required", []) if key not in result]
        if missing:
            raise ValueError("champs requis : " + ", ".join(missing))
        return result
    return value


def _redact(value: Any, *, key: str = "", depth: int = 0) -> Any:
    if _SECRET_KEY_RE.search(key):
        return "[REDACTED]"
    if depth >= 4:
        return "[…]"
    if isinstance(value, Mapping):
        return {
            str(k): _redact(v, key=str(k), depth=depth + 1)
            for k, v in list(value.items())[:30]
        }
    if isinstance(value, (list, tuple)):
        return [_redact(v, depth=depth + 1) for v in list(value)[:20]]
    if isinstance(value, str):
        return value[:800]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:200]


class ActionRuntime:
    def __init__(self, declarations: Iterable[Mapping[str, Any]]):
        self._schemas = {
            str(decl.get("name")): copy.deepcopy(decl.get("parameters") or {})
            for decl in declarations
            if decl.get("name")
        }
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._failures: dict[str, tuple[int, float]] = {}
        self._pending: dict[str, int] = {}

    @property
    def known_actions(self) -> frozenset[str]:
        return frozenset(self._schemas)

    def policy_for(self, name: str) -> ActionPolicy:
        return _POLICIES.get(name, ActionPolicy())

    def prepare(self, name: str, args: Mapping[str, Any] | None) -> dict[str, Any]:
        if name not in self._schemas:
            raise ActionValidationError(f"Action inconnue : {name}")
        if args is None:
            raw: dict[str, Any] = {}
        elif isinstance(args, Mapping):
            raw = dict(args)
        else:
            # Les SDK exposent parfois une vue dict-compatible sans l'enregistrer
            # comme Mapping. Une conversion sûre évite de rejeter un appel valide.
            try:
                raw = dict(args)
            except (TypeError, ValueError):
                raise ActionValidationError(
                    f"Arguments invalides pour {name} : objet attendu"
                ) from None

        aliases = _ARG_ALIASES.get(name, {})
        for source, target in aliases.items():
            # Un alias l'emporte aussi sur un champ cible présent mais vide :
            # le modèle envoie parfois `message_text=""` et le texte dans `text`.
            if source in raw and (target not in raw or raw[target] in (None, "")):
                raw[target] = raw.pop(source)

        schema = self._schemas[name]
        properties = schema.get("properties") or {}
        prepared: dict[str, Any] = {}
        problems: list[str] = []
        # Champs inventés porteurs d'un texte : ils servent de repli quand un
        # champ requis est vide (le modèle a mis le message au mauvais endroit).
        stray_text: dict[str, str] = {}
        for field, value in raw.items():
            if field not in properties:
                # Supprimer les champs inventés : les transmettre aux actions
                # rend leur comportement imprévisible et brouille les suivis.
                if isinstance(value, str) and value.strip():
                    stray_text[str(field)] = value.strip()
                continue
            try:
                field_schema = properties.get(field) or {}
                prepared[field] = _coerce_value(
                    value, str(field_schema.get("type", "")), field, field_schema
                )
                allowed = field_schema.get("enum")
                if allowed and prepared[field] not in allowed:
                    raise ValueError("valeur attendue : " + " | ".join(map(str, allowed)))
            except (TypeError, ValueError, OverflowError, RecursionError) as exc:
                problems.append(f"{field}: {exc}")

        missing = [
            field for field in (schema.get("required") or [])
            if field not in prepared or prepared[field] in (None, "", [])
        ]
        if missing and stray_text:
            # Récupération sans second tour : un seul champ texte requis vide et
            # un seul texte égaré → c'est lui, sans ambiguïté possible.
            missing_text = [
                f for f in missing
                if str((properties.get(f) or {}).get("type", "")).upper() in ("STRING", "")
            ]
            if len(missing_text) == 1 and len(stray_text) == 1:
                stray_value = next(iter(stray_text.values()))
                field = missing_text[0]
                field_schema = properties.get(field) or {}
                try:
                    value = _coerce_value(stray_value, str(field_schema.get("type", "")), field, field_schema)
                    allowed = field_schema.get("enum")
                    if allowed and value not in allowed:
                        raise ValueError("valeur attendue : " + " | ".join(map(str, allowed)))
                    prepared[field] = value
                    missing.remove(field)
                except (TypeError, ValueError, OverflowError, RecursionError) as exc:
                    problems.append(f"{field}: {exc}")
        if missing:
            hint = ""
            if stray_text:
                hint = (
                    " (texte reçu dans des champs non déclarés : "
                    + ", ".join(f"`{k}`" for k in stray_text)
                    + " — utilise " + ", ".join(f"`{m}`" for m in missing) + ")"
                )
            problems.append("paramètres requis manquants : " + ", ".join(missing) + hint)
        if problems:
            raise ActionValidationError(
                f"Arguments invalides pour {name} — " + "; ".join(problems)
            )
        return prepared

    def ensure_available(self, name: str) -> None:
        failures, opened_at = self._failures.get(name, (0, 0.0))
        policy = self.policy_for(name)
        if failures < policy.failure_threshold:
            return
        remaining = policy.cooldown_s - (time.monotonic() - opened_at)
        if remaining <= 0:
            self._failures.pop(name, None)
            return
        raise ActionCircuitOpen(
            f"{name} est temporairement suspendu après plusieurs échecs "
            f"({remaining:.0f} s restantes)"
        )

    def note_success(self, name: str) -> None:
        self._failures.pop(name, None)

    def note_failure(self, name: str) -> None:
        count, _ = self._failures.get(name, (0, 0.0))
        self._failures[name] = (count + 1, time.monotonic())

    @asynccontextmanager
    async def lease(self, name: str):
        self.ensure_available(name)
        policy = self.policy_for(name)
        semaphore = self._semaphores.get(name)
        if semaphore is None:
            semaphore = self._semaphores[name] = asyncio.Semaphore(policy.max_concurrency)
        if self._pending.get(name, 0) >= policy.max_pending:
            raise ActionQueueFull(f"File d’attente pleine pour {name}")
        self._pending[name] = self._pending.get(name, 0) + 1
        acquired = False
        try:
            try:
                await asyncio.wait_for(semaphore.acquire(), timeout=policy.queue_timeout_s)
                acquired = True
            except asyncio.TimeoutError:
                raise ActionQueueFull(f"Attente trop longue pour {name}") from None
            # Une panne peut avoir ouvert le circuit pendant l'attente.
            self.ensure_available(name)
            yield policy
        finally:
            self._pending[name] -= 1
            if acquired:
                semaphore.release()

    @staticmethod
    def looks_failed(result: Any) -> bool:
        text = str(result or "").strip().lower()
        return any(text.startswith(prefix) for prefix in _ERROR_PREFIXES)

    @staticmethod
    def should_trip_from_result(result: Any) -> bool:
        """Réserve le disjoncteur aux pannes techniques non gérées."""
        text = str(result or "").strip().lower()
        return any(text.startswith(prefix) for prefix in _CIRCUIT_ERROR_PREFIXES)

    @staticmethod
    def can_run_in_parallel(name: str, args: Mapping[str, Any] | None = None) -> bool:
        """Vrai uniquement pour une action de lecture sans dépendance d'interface."""
        return name in _PARALLEL_SAFE_ACTIONS

    @staticmethod
    def remember(
        memory: MutableMapping[str, Any] | None,
        *,
        name: str,
        args: Mapping[str, Any],
        result: Any,
        ok: bool,
        duration_ms: float,
    ) -> None:
        if memory is None:
            return
        history = memory.setdefault("_action_history", [])
        if not isinstance(history, list):
            history = memory["_action_history"] = []
        history.append({
            "action": name,
            "args": _redact(args),
            "result": _redact(result),
            "ok": bool(ok),
            "duration_ms": round(duration_ms, 1),
            "at": round(time.time(), 3),
        })
        del history[:-12]
        memory["_last_action"] = history[-1]


def friendly_runtime_error(name: str, exc: BaseException) -> str:
    if isinstance(exc, asyncio.TimeoutError):
        return (
            f"L’action {name} n'a rien rendu dans le délai imparti : son résultat n'est pas confirmé. "
            "Un effet peut encore avoir eu lieu ; vérifie son état sans relancer l’action, et "
            "demande-lui s'il veut réessayer."
        )
    if isinstance(exc, ActionValidationError):
        return str(exc) + ". Corrige les paramètres puis appelle l’action une seule fois."
    if isinstance(exc, ActionCircuitOpen):
        return str(exc) + ". Utilise une autre méthode ou attends avant de réessayer."
    if isinstance(exc, ActionQueueFull):
        return str(exc) + ". Attends la fin des actions en cours."
    return f"Échec contrôlé de {name} : {type(exc).__name__}: {str(exc)[:240]}"
