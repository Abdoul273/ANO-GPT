"""core/tool_registry.py — Registre d'outils déclaratif, moderne et typé pour ANO-GPT.

Fournit :
1. Décorateur @tool avec métadonnées complètes (nom, description, rate-limiting,
   circuit breaker, confirmation vocale, timeouts, concurrence, alias).
2. Générateur automatique de google.genai.types.FunctionDeclaration et JSON Schema
   par métaprogrammation et introspection (signature, annotations Pydantic/dataclass/primitives,
   docstrings Google-style).
3. Dispatcher dynamique asynchrone avec validation Pydantic stricte des arguments,
   sémaphores de concurrence, injection du contexte (ExecutionContext), rate-limiting
   glissant et disjoncteur (Circuit Breaker).
4. Adaptateur de compatibilité (LegacyToolAdapter) assurant la transition fluide
   sans régression des 40+ outils historiques vers le nouveau registre.
"""

from __future__ import annotations

import asyncio
import dataclasses
import enum
import inspect
import re
import time
import types as py_types
import typing
from collections import deque
from dataclasses import dataclass, field
from typing import (
    Any,
    Awaitable,
    Callable,
    Literal,
    Mapping,
    Sequence,
    Union,
    get_args,
    get_origin,
)

from google.genai import types
from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model


# ══════════════════════════════════════════════════════════════════════════════
# 1. EXCEPTIONS DU REGISTRE D'OUTILS
# ══════════════════════════════════════════════════════════════════════════════

class ToolRegistryError(Exception):
    """Erreur de base du registre d'outils."""


class ToolNotFoundError(ToolRegistryError):
    """L'outil demandé n'est pas enregistré dans le registre."""


class ToolValidationError(ToolRegistryError):
    """Les arguments fournis par le modèle ne respectent pas le schéma."""


class ToolRateLimitError(ToolRegistryError):
    """La cadence maximale d'appels à l'outil a été dépassée."""


class ToolCircuitOpenError(ToolRegistryError):
    """Le disjoncteur (Circuit Breaker) est ouvert suite à des pannes répétées."""


class ToolSecurityError(ToolRegistryError):
    """Refus d'exécution : voix non authentifiée ou commande destructive rejetée."""


class ToolTimeoutError(ToolRegistryError):
    """L'outil a dépassé son délai maximum d'exécution."""


# ══════════════════════════════════════════════════════════════════════════════
# 2. STRUCTURES DE DONNÉES DU RUNTIME (CONTEXTE ET RÉSULTAT)
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class ExecutionContext:
    """Contexte d'exécution injecté dynamiquement dans les outils et le répartiteur.

    Permet aux outils d'accéder à l'UI (cartes, statut audio), à la mémoire de
    session et aux informations d'authentification vocale sans couplage rigide.
    """
    ui: Any = None
    call_id: str = ""
    session_id: str = ""
    user_id: str = "Anonymous"
    voice_verified: bool = True
    is_stranger: bool = False
    session_memory: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    # Callables dynamiques pour vérification biométrique / STT haute précision
    voice_checker: Callable[[], bool] | None = None
    voice_verifier: Callable[[str], Awaitable[str] | str] | None = None

    def voice_is_stranger(self) -> bool:
        """Détermine si la voix de l'interlocuteur est inconnue."""
        if self.voice_checker is not None:
            try:
                return bool(self.voice_checker())
            except Exception:
                return True
        return self.is_stranger or (not self.voice_verified)

    async def verify_sensitive_voice(self, tool_name: str) -> str:
        """Vérifie la commande via double transcription ASR si sensible.

        Retourne une chaîne vide si la commande est confirmée, ou le message d'erreur
        de divergence si elle doit être bloquée.
        """
        if self.voice_verifier is not None:
            try:
                res = self.voice_verifier(tool_name)
                if inspect.isawaitable(res):
                    return str(await res)
                return str(res)
            except Exception as exc:
                return f"Erreur de vérification vocale : {exc}"
        return ""

    def record_history(
        self,
        *,
        name: str,
        args: Mapping[str, Any],
        result: Any,
        ok: bool,
        duration_ms: float,
    ) -> None:
        """Consigne l'action dans la mémoire de session sans secrets."""
        if not isinstance(self.session_memory, dict):
            return
        history = self.session_memory.setdefault("_action_history", [])
        if not isinstance(history, list):
            history = self.session_memory["_action_history"] = []
        history.append({
            "action": name,
            "args": dict(args),
            "result": str(result)[:800],
            "ok": bool(ok),
            "duration_ms": round(duration_ms, 1),
            "at": round(time.time(), 3),
        })
        del history[:-12]
        self.session_memory["_last_action"] = history[-1]


@dataclass
class ToolResult:
    """Résultat structuré d'un appel d'outil prêt pour Gemini Live."""
    ok: bool
    result: Any
    error: str | None = None
    execution_time_ms: float = 0.0
    tool_name: str = ""
    call_id: str = ""
    silent: bool = False
    retryable: bool = False
    raw_response: Any = None

    def to_dict(self) -> dict[str, Any]:
        """Convertit le résultat en payload JSON pour le LLM."""
        formatted_result = self.result
        if isinstance(self.result, BaseModel):
            formatted_result = self.result.model_dump()
        elif dataclasses.is_dataclass(self.result) and not isinstance(self.result, type):
            formatted_result = dataclasses.asdict(self.result)

        payload: dict[str, Any] = {
            "ok": self.ok,
            "result": formatted_result,
        }
        if self.error:
            payload["error"] = self.error
        if self.silent:
            payload["silent"] = True
        if self.retryable:
            payload["retryable"] = True
        return payload

    def to_function_response(self, call_id: str | None = None) -> types.FunctionResponse:
        """Produit l'objet FunctionResponse attendu par google.genai."""
        cid = call_id or self.call_id or "unknown"
        return types.FunctionResponse(
            id=cid,
            name=self.tool_name or "unknown",
            response=self.to_dict(),
        )


# ══════════════════════════════════════════════════════════════════════════════
# 3. GESTIONNAIRES DE RÉSILIENCE : RATE LIMITER & CIRCUIT BREAKER
# ══════════════════════════════════════════════════════════════════════════════

class SlidingWindowRateLimiter:
    """Rate limiter à fenêtre glissante thread-safe et asyncio-safe.

    Exemple de spécification : '10/minute', '5/second', '100/hour'.
    """
    _PATTERN = re.compile(
        r"^\s*(?P<count>\d+)\s*/\s*(?P<unit>second|sec|s|minute|min|m|hour|hr|h|day|d)s?\s*$",
        re.IGNORECASE,
    )
    _UNIT_MULTIPLIERS = {
        "s": 1.0, "sec": 1.0, "second": 1.0,
        "m": 60.0, "min": 60.0, "minute": 60.0,
        "h": 3600.0, "hr": 3600.0, "hour": 3600.0,
        "d": 86400.0, "day": 86400.0,
    }

    def __init__(self, rate_spec: str):
        self.rate_spec = rate_spec.strip()
        match = self._PATTERN.match(self.rate_spec)
        if not match:
            raise ValueError(
                f"Format de rate_limit invalide '{rate_spec}'. "
                f"Attendu : '<nombre>/<seconde|minute|heure|jour>' (ex: '10/minute')."
            )
        self.max_requests = int(match.group("count"))
        unit = match.group("unit").lower()
        self.window_seconds = self._UNIT_MULTIPLIERS.get(unit, 60.0)
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def can_execute(self) -> tuple[bool, float]:
        """Vérifie si un appel est autorisé sans incrémenter le compteur.

        Retourne (autorisé, temps_d_attente_secondes).
        """
        async with self._lock:
            now = time.monotonic()
            self._evict_expired(now)
            if len(self._timestamps) < self.max_requests:
                return True, 0.0
            oldest = self._timestamps[0]
            wait_s = max(0.0, self.window_seconds - (now - oldest))
            return False, wait_s

    async def record(self) -> None:
        """Enregistre un appel effectif."""
        async with self._lock:
            now = time.monotonic()
            self._evict_expired(now)
            self._timestamps.append(now)

    def _evict_expired(self, now: float) -> None:
        threshold = now - self.window_seconds
        while self._timestamps and self._timestamps[0] <= threshold:
            self._timestamps.popleft()

    def reset(self) -> None:
        """Réinitialise l'historique."""
        self._timestamps.clear()


class CircuitBreaker:
    """Disjoncteur (Circuit Breaker) pour isoler les outils en panne continue.

    États :
    - CLOSED : Normal, les appels passent.
    - OPEN : Trop d'échecs consécutifs, les appels sont rejetés immédiatement
             pendant une période de cooldown.
    - HALF_OPEN : Période d'essai post-cooldown. Un succès referme le circuit,
                  un échec le rouvre pour un nouveau cooldown.
    """
    def __init__(self, failure_threshold: int = 3, cooldown_s: float = 30.0):
        self.failure_threshold = max(1, failure_threshold)
        self.cooldown_s = max(0.1, cooldown_s)
        self._failure_count: int = 0
        self._last_failure_time: float = 0.0
        self._state: str = "CLOSED"  # "CLOSED" | "OPEN" | "HALF_OPEN"
        self._lock = asyncio.Lock()

    @property
    def state(self) -> str:
        return self._state

    async def can_execute(self) -> tuple[bool, float]:
        """Détermine si l'outil peut être appelé.

        Retourne (autorisé, temps_restant_cooldown_s).
        """
        async with self._lock:
            now = time.monotonic()
            if self._state == "OPEN":
                elapsed = now - self._last_failure_time
                if elapsed >= self.cooldown_s:
                    self._state = "HALF_OPEN"
                    return True, 0.0
                return False, self.cooldown_s - elapsed
            return True, 0.0

    async def record_success(self) -> None:
        """Enregistre une exécution réussie, réinitialisant les pannes."""
        async with self._lock:
            self._failure_count = 0
            self._state = "CLOSED"

    async def record_failure(self) -> None:
        """Enregistre un échec technique et ouvre le circuit si seuil atteint."""
        async with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()
            if self._failure_count >= self.failure_threshold:
                self._state = "OPEN"

    def reset(self) -> None:
        """Réinitialise le circuit breaker."""
        self._failure_count = 0
        self._last_failure_time = 0.0
        self._state = "CLOSED"


# ══════════════════════════════════════════════════════════════════════════════
# 4. MÉTAPROGRAMMATION ET GÉNÉRATEUR AUTOMATIQUE DE FunctionDeclaration
# ══════════════════════════════════════════════════════════════════════════════

def parse_google_docstring(doc: str | None) -> tuple[str, dict[str, str]]:
    """Extrait la description globale et les descriptions de paramètres Google-style.

    Exemple :
        Retourne l'état des métriques en temps réel.

        Args:
            detail_level: Niveau de précision ('summary' ou 'full').
            timeout: Délai d'attente maximal en secondes.

        Returns:
            Rapport des métriques.
    """
    if not doc:
        return "", {}

    lines = doc.expandtabs(4).splitlines()
    main_desc: list[str] = []
    param_descs: dict[str, str] = {}
    current_section = "desc"
    current_param: str | None = None

    section_re = re.compile(
        r"^(?:Args|Arguments|Parameters|Returns|Yields|Raises|Note|Notes|Examples?):\s*$",
        re.IGNORECASE,
    )
    param_re = re.compile(
        r"^\s*([a-zA-Z_][a-zA-Z0-9_]*)(?:\s*\([^)]*\))?\s*:\s*(.*)$"
    )

    for line in lines:
        s_line = line.strip()
        sec_match = section_re.match(s_line)
        if sec_match:
            current_section = sec_match.group(0).rstrip(":").strip().lower()
            current_param = None
            continue

        if current_section == "desc":
            main_desc.append(line)
        elif current_section in ("args", "arguments", "parameters"):
            param_match = param_re.match(line)
            if param_match:
                current_param = param_match.group(1)
                param_descs[current_param] = param_match.group(2).strip()
            elif current_param and (line.startswith("    ") or line.startswith("\t")):
                param_descs[current_param] += " " + s_line

    description = "\n".join(main_desc).strip()
    return description, param_descs


def python_type_to_schema(
    type_hint: Any,
    description: str | None = None,
    default: Any = inspect.Parameter.empty,
) -> types.Schema:
    """Convertit récursivement un type Python vers google.genai.types.Schema.

    Gère les primitives (str, int, float, bool), Literal (enum), Union/Optional,
    les modèles Pydantic, dataclasses, énumérations enum.Enum, listes et dictionnaires.
    """
    origin = get_origin(type_hint)
    args = get_args(type_hint)

    # 1. Union / Optional / T | None
    if origin in (Union, getattr(py_types, "UnionType", None)):
        non_none = [a for a in args if a is not type(None)]
        nullable = type(None) in args
        if len(non_none) == 1:
            schema = python_type_to_schema(non_none[0], description=description)
            if nullable:
                schema.nullable = True
            return schema

        # Union de Literals
        if all(get_origin(a) is Literal for a in non_none):
            enums: list[Any] = []
            for a in non_none:
                enums.extend(get_args(a))
            is_str = all(isinstance(x, str) for x in enums)
            return types.Schema(
                type=types.Type.STRING if is_str else types.Type.INTEGER,
                enum=[str(x) for x in enums],
                description=description,
                nullable=nullable or None,
            )

        # Fallback pour union générique
        return types.Schema(
            type=types.Type.STRING,
            description=description,
            nullable=nullable or None,
        )

    # 2. Literal['a', 'b']
    if origin is Literal:
        is_str = all(isinstance(x, str) for x in args)
        return types.Schema(
            type=types.Type.STRING if is_str else types.Type.INTEGER,
            enum=[str(x) for x in args],
            description=description,
        )

    # 3. Enum Python
    if isinstance(type_hint, type) and issubclass(type_hint, enum.Enum):
        enum_vals = [str(e.value) for e in type_hint]
        return types.Schema(
            type=types.Type.STRING,
            enum=enum_vals,
            description=description,
        )

    # 4. Primitives Python (bool doit précéder int)
    if type_hint in (bool, "bool", "boolean"):
        return types.Schema(type=types.Type.BOOLEAN, description=description)
    if type_hint in (int, "int", "integer"):
        return types.Schema(type=types.Type.INTEGER, description=description)
    if type_hint in (float, "float", "number"):
        return types.Schema(type=types.Type.NUMBER, description=description)
    if type_hint in (str, "str", "string"):
        return types.Schema(type=types.Type.STRING, description=description)

    # 5. Listes / Séquences
    if origin in (list, Sequence, typing.List, typing.Sequence, set, typing.Set, tuple, typing.Tuple):
        item_type = args[0] if args else Any
        item_schema = python_type_to_schema(item_type)
        return types.Schema(
            type=types.Type.ARRAY,
            items=item_schema,
            description=description,
        )

    # 6. Modèles Pydantic (BaseModel)
    if isinstance(type_hint, type) and issubclass(type_hint, BaseModel):
        properties: dict[str, types.Schema] = {}
        required: list[str] = []
        for field_name, field_info in type_hint.model_fields.items():
            f_desc = field_info.description or None
            properties[field_name] = python_type_to_schema(
                field_info.annotation,
                description=f_desc,
            )
            if field_info.is_required():
                required.append(field_name)
        return types.Schema(
            type=types.Type.OBJECT,
            properties=properties,
            required=required or None,
            description=description or type_hint.__doc__ or None,
        )

    # 7. Dataclasses
    if dataclasses.is_dataclass(type_hint) and isinstance(type_hint, type):
        try:
            dc_hints = typing.get_type_hints(type_hint)
        except Exception:
            dc_hints = {}
        properties = {}
        required = []
        for f in dataclasses.fields(type_hint):
            field_t = dc_hints.get(f.name, f.type)
            properties[f.name] = python_type_to_schema(field_t)
            if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
                required.append(f.name)
        return types.Schema(
            type=types.Type.OBJECT,
            properties=properties,
            required=required or None,
            description=description or type_hint.__doc__ or None,
        )

    # 8. Dictionnaires / Mappings
    if type_hint in (dict, "dict") or origin in (dict, Mapping, typing.Dict, typing.Mapping):
        return types.Schema(type=types.Type.OBJECT, description=description)

    # Fallback générique
    return types.Schema(type=types.Type.STRING, description=description)


def schema_to_dict(schema: types.Schema | None) -> dict[str, Any]:
    """Convertit un types.Schema en dictionnaire standardisé (format JSON Schema / Live)."""
    if schema is None:
        return {}
    res: dict[str, Any] = {}
    if schema.type:
        res["type"] = schema.type.value if hasattr(schema.type, "value") else str(schema.type)
    if schema.description:
        res["description"] = schema.description
    if schema.enum:
        res["enum"] = list(schema.enum)
    if schema.properties:
        res["properties"] = {k: schema_to_dict(v) for k, v in schema.properties.items()}
    elif res.get("type") == "OBJECT":
        res["properties"] = {}
    if schema.required:
        res["required"] = list(schema.required)
    if schema.items:
        res["items"] = schema_to_dict(schema.items)
    if schema.nullable is not None:
        res["nullable"] = schema.nullable
    return res


def declaration_to_dict(decl: types.FunctionDeclaration) -> dict[str, Any]:
    """Convertit un types.FunctionDeclaration en dict pour compatibilité TOOL_DECLARATIONS."""
    return {
        "name": decl.name,
        "description": decl.description or "",
        "parameters": schema_to_dict(decl.parameters) if decl.parameters else {"type": "OBJECT", "properties": {}},
    }


def dict_to_genai_schema(d: Mapping[str, Any] | None) -> types.Schema:
    """Convertit un dictionnaire de schéma legacy vers types.Schema."""
    if not isinstance(d, Mapping):
        return types.Schema(type=types.Type.OBJECT, properties={})

    t_str = str(d.get("type", "OBJECT")).upper()
    type_enum = getattr(types.Type, t_str, types.Type.OBJECT)

    properties = None
    if "properties" in d and isinstance(d["properties"], Mapping):
        properties = {k: dict_to_genai_schema(v) for k, v in d["properties"].items()}

    items = None
    if "items" in d and isinstance(d["items"], Mapping):
        items = dict_to_genai_schema(d["items"])

    required = list(d["required"]) if d.get("required") else None
    enum_vals = list(d["enum"]) if d.get("enum") else None

    return types.Schema(
        type=type_enum,
        description=d.get("description"),
        properties=properties,
        items=items,
        required=required,
        enum=enum_vals,
        nullable=d.get("nullable"),
    )


# ══════════════════════════════════════════════════════════════════════════════
# 5. DÉFINITION D'OUTIL (METADATA + RUNTIME STATE)
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class ToolDefinition:
    """Représentation complète et autonome d'un outil enregistré."""
    name: str
    description: str
    func: Callable[..., Any]
    is_async: bool
    destructive: bool = False
    rate_limit: str | None = None
    requires_voice_id: bool = False
    timeout_s: float = 35.0
    max_concurrency: int = 1
    failure_threshold: int = 3
    cooldown_s: float = 30.0
    aliases: dict[str, str] = field(default_factory=dict)
    parallel_safe: bool = False
    arg_model: type[BaseModel] | None = None
    declaration: types.FunctionDeclaration | None = None
    circuit_breaker: CircuitBreaker = field(init=False)
    rate_limiter: SlidingWindowRateLimiter | None = field(init=False, default=None)
    context_param_name: str | None = None
    _semaphore: asyncio.Semaphore | None = field(init=False, default=None)

    def __post_init__(self):
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=self.failure_threshold,
            cooldown_s=self.cooldown_s,
        )
        if self.rate_limit:
            self.rate_limiter = SlidingWindowRateLimiter(self.rate_limit)

    def get_semaphore(self) -> asyncio.Semaphore:
        """Initialise paresseusement le sémaphore asyncio dans la boucle active."""
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.max_concurrency)
        return self._semaphore

    def to_dict(self) -> dict[str, Any]:
        """Exporte la déclaration au format dict historique."""
        if self.declaration:
            return declaration_to_dict(self.declaration)
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {"type": "OBJECT", "properties": {}},
        }

    def to_function_declaration(self) -> types.FunctionDeclaration:
        """Exporte l'objet types.FunctionDeclaration."""
        if self.declaration:
            return self.declaration
        return types.FunctionDeclaration(
            name=self.name,
            description=self.description,
            parameters=types.Schema(type=types.Type.OBJECT, properties={}),
        )


# ══════════════════════════════════════════════════════════════════════════════
# 6. FABRIQUE D'OUTILS ET VALIDATEUR DYNAMIQUE
# ══════════════════════════════════════════════════════════════════════════════

def is_context_type(annotation: Any) -> bool:
    """Détecte si un paramètre correspond à ExecutionContext (direct ou Union)."""
    if annotation is ExecutionContext:
        return True
    origin = get_origin(annotation)
    if origin in (Union, getattr(py_types, "UnionType", None)):
        return any(is_context_type(a) for a in get_args(annotation))
    name = getattr(annotation, "__name__", "")
    return name == "ExecutionContext"


def build_tool_metadata(
    func: Callable[..., Any],
    *,
    name: str | None = None,
    description: str | None = None,
    destructive: bool = False,
    rate_limit: str | None = None,
    requires_voice_id: bool = False,
    timeout_s: float = 35.0,
    max_concurrency: int = 1,
    failure_threshold: int = 3,
    cooldown_s: float = 30.0,
    aliases: dict[str, str] | None = None,
    parallel_safe: bool = False,
) -> ToolDefinition:
    """Inspecte un callable Python et génère automatiquement son ToolDefinition complet."""
    tool_name = (name or func.__name__).strip()
    doc_desc, param_doc_descs = parse_google_docstring(inspect.getdoc(func))
    tool_desc = (description or doc_desc or f"Tool {tool_name}.").strip()

    sig = inspect.signature(func)
    is_coro = inspect.iscoroutinefunction(func)

    # Résolution des type hints pour contourner les annotations différées (__future__.annotations)
    try:
        resolved_hints = typing.get_type_hints(func)
    except Exception:
        resolved_hints = {}

    # Détection des paramètres et génération des schémas / modèle Pydantic
    field_definitions: dict[str, Any] = {}
    properties: dict[str, types.Schema] = {}
    required_fields: list[str] = []
    context_param_name: str | None = None

    for param_name, param in sig.parameters.items():
        # Exclure *args et **kwargs
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue

        ann = resolved_hints.get(param_name, param.annotation)
        if ann is inspect.Parameter.empty:
            ann = Any

        # Détection du contexte d'exécution (invariablement injecté à l'exécution)
        if param_name in ("context", "_context", "ctx") or is_context_type(ann):
            context_param_name = param_name
            continue

        p_desc = param_doc_descs.get(param_name)

        # Détermination du caractère obligatoire et valeur par défaut
        has_default = param.default is not inspect.Parameter.empty
        default_val = param.default if has_default else ...

        # Pydantic field definition
        field_definitions[param_name] = (
            ann,
            Field(default=default_val, description=p_desc),
        )

        # Schema GenAI
        field_schema = python_type_to_schema(ann, description=p_desc, default=param.default)
        properties[param_name] = field_schema

        if not has_default:
            # Si pas de défaut et pas explicitement nullable
            origin = get_origin(ann)
            is_nullable = origin in (Union, getattr(py_types, "UnionType", None)) and type(None) in get_args(ann)
            if not is_nullable:
                required_fields.append(param_name)

    # Modèle dynamique Pydantic
    ArgModel = create_model(
        f"{tool_name.title().replace('_', '')}Arguments",
        **field_definitions,
    )
    ArgModel.model_config = ConfigDict(extra="ignore", arbitrary_types_allowed=True)

    # FunctionDeclaration GenAI
    parameters_schema = types.Schema(
        type=types.Type.OBJECT,
        properties=properties,
        required=required_fields or None,
    )
    declaration = types.FunctionDeclaration(
        name=tool_name,
        description=tool_desc,
        parameters=parameters_schema,
    )

    return ToolDefinition(
        name=tool_name,
        description=tool_desc,
        func=func,
        is_async=is_coro,
        destructive=destructive,
        rate_limit=rate_limit,
        requires_voice_id=requires_voice_id or destructive,
        timeout_s=timeout_s,
        max_concurrency=max_concurrency,
        failure_threshold=failure_threshold,
        cooldown_s=cooldown_s,
        aliases=dict(aliases or {}),
        parallel_safe=parallel_safe,
        arg_model=ArgModel,
        declaration=declaration,
        context_param_name=context_param_name,
    )


# ══════════════════════════════════════════════════════════════════════════════
# 7. REGISTRE CENTRAL ET DISPATCHER DYNAMIQUE
# ══════════════════════════════════════════════════════════════════════════════

class ToolRegistry:
    """Registre central d'outils ANO-GPT avec dispatching dynamique et résilience."""

    def __init__(self):
        self._tools: dict[str, ToolDefinition] = {}
        self._fallback_executor: Callable[[str, dict[str, Any], ExecutionContext], Any] | None = None
        self._lock = asyncio.Lock()

    def register(self, tool_def: ToolDefinition) -> None:
        """Enregistre ou remplace un outil dans le registre."""
        self._tools[tool_def.name] = tool_def

    def unregister(self, name: str) -> ToolDefinition | None:
        """Retire un outil du registre."""
        return self._tools.pop(name, None)

    def get(self, name: str) -> ToolDefinition | None:
        """Récupère la définition d'un outil."""
        return self._tools.get(name)

    def list_tools(self) -> list[str]:
        """Retourne les noms de tous les outils enregistrés."""
        return sorted(self._tools.keys())

    def get_declarations(self) -> list[types.FunctionDeclaration]:
        """Génère la liste d'objets types.FunctionDeclaration pour Gemini Live."""
        return [tool_def.to_function_declaration() for tool_def in self._tools.values()]

    def to_declarations_dict(self) -> list[dict[str, Any]]:
        """Exporte l'ensemble des déclarations sous forme de dictionnaires JSON-compatibles."""
        return [tool_def.to_dict() for tool_def in self._tools.values()]

    def to_gemini_tool(self) -> types.Tool:
        """Crée l'objet types.Tool prêt à être injecté dans LiveConnectConfig."""
        return types.Tool(function_declarations=self.get_declarations())

    def set_fallback_executor(
        self,
        executor: Callable[[str, dict[str, Any], ExecutionContext], Any] | None,
    ) -> None:
        """Définit un exécuteur de repli pour les outils non encore migrés."""
        self._fallback_executor = executor

    async def execute(
        self,
        tool_name: str,
        args: Mapping[str, Any] | None = None,
        context: ExecutionContext | None = None,
    ) -> ToolResult:
        """Dispatcher dynamique : validation, circuit breaker, rate limit et exécution."""
        started_at = time.perf_counter()
        raw_args = dict(args or {})
        ctx = context or ExecutionContext()

        # 1. Vérification de l'existence de l'outil
        tool_def = self._tools.get(tool_name)
        if tool_def is None:
            if self._fallback_executor is not None:
                try:
                    res = self._fallback_executor(tool_name, raw_args, ctx)
                    if inspect.isawaitable(res):
                        res = await res
                    duration = (time.perf_counter() - started_at) * 1000.0
                    return ToolResult(
                        ok=True,
                        result=res,
                        tool_name=tool_name,
                        call_id=ctx.call_id,
                        execution_time_ms=duration,
                        raw_response=res,
                    )
                except Exception as exc:
                    duration = (time.perf_counter() - started_at) * 1000.0
                    return ToolResult(
                        ok=False,
                        result=f"Erreur outil hérité '{tool_name}' : {exc}",
                        error=str(exc),
                        tool_name=tool_name,
                        call_id=ctx.call_id,
                        execution_time_ms=duration,
                        retryable=True,
                    )
            duration = (time.perf_counter() - started_at) * 1000.0
            return ToolResult(
                ok=False,
                result=f"Outil inconnu : '{tool_name}'. Vérifiez la liste des outils disponibles.",
                error=f"ToolNotFoundError: {tool_name}",
                tool_name=tool_name,
                call_id=ctx.call_id,
                execution_time_ms=duration,
            )

        # 2. Sécurité biométrique et vocale
        if tool_def.requires_voice_id or tool_def.destructive:
            if ctx.voice_is_stranger():
                duration = (time.perf_counter() - started_at) * 1000.0
                return ToolResult(
                    ok=False,
                    result=(
                        f"REFUSÉ : l'outil '{tool_name}' est restreint à l'utilisateur enregistré "
                        "et la voix courante n'est pas reconnue."
                    ),
                    error="VoiceAuthenticationFailed",
                    tool_name=tool_name,
                    call_id=ctx.call_id,
                    execution_time_ms=duration,
                )
            if tool_def.destructive:
                verif_error = await ctx.verify_sensitive_voice(tool_name)
                if verif_error:
                    duration = (time.perf_counter() - started_at) * 1000.0
                    return ToolResult(
                        ok=False,
                        result=verif_error,
                        error="VoiceVerificationMismatch",
                        tool_name=tool_name,
                        call_id=ctx.call_id,
                        execution_time_ms=duration,
                    )

        # 3. Rate Limiter (cadence maximale d'appels)
        if tool_def.rate_limiter is not None:
            allowed, wait_s = await tool_def.rate_limiter.can_execute()
            if not allowed:
                duration = (time.perf_counter() - started_at) * 1000.0
                return ToolResult(
                    ok=False,
                    result=(
                        f"L'outil '{tool_name}' a dépassé sa limite d'appels ({tool_def.rate_limit}). "
                        f"Veuillez patienter {wait_s:.1f} seconde(s) avant de réessayer."
                    ),
                    error=f"RateLimitExceeded (wait {wait_s:.1f}s)",
                    tool_name=tool_name,
                    call_id=ctx.call_id,
                    execution_time_ms=duration,
                    retryable=True,
                )

        # 4. Circuit Breaker (protection contre pannes en cascade)
        can_exec, remaining_cooldown = await tool_def.circuit_breaker.can_execute()
        if not can_exec:
            duration = (time.perf_counter() - started_at) * 1000.0
            return ToolResult(
                ok=False,
                result=(
                    f"L'outil '{tool_name}' est temporairement suspendu suite à des échecs répétés "
                    f"({remaining_cooldown:.1f}s restantes avant réévaluation)."
                ),
                error="CircuitBreakerOpen",
                tool_name=tool_name,
                call_id=ctx.call_id,
                execution_time_ms=duration,
                retryable=True,
            )

        # 5. Normalisation des alias et validation Pydantic
        clean_args = dict(raw_args)
        for alias, canon in tool_def.aliases.items():
            if alias in clean_args and canon not in clean_args:
                clean_args[canon] = clean_args.pop(alias)

        call_kwargs: dict[str, Any] = {}
        if tool_def.arg_model is not None:
            # Découpage des chaînes "a, b, c" pour les listes si nécessaire
            for field_name, field_info in tool_def.arg_model.model_fields.items():
                if field_name in clean_args and isinstance(clean_args[field_name], str):
                    origin = get_origin(field_info.annotation)
                    if origin in (list, Sequence, typing.List, typing.Sequence, set, typing.Set):
                        clean_args[field_name] = [
                            item.strip() for item in clean_args[field_name].split(",") if item.strip()
                        ]

            try:
                validated_model = tool_def.arg_model.model_validate(clean_args)
                call_kwargs = validated_model.model_dump()
            except ValidationError as val_err:
                duration = (time.perf_counter() - started_at) * 1000.0
                problems = [
                    f"{'.'.join(map(str, err.get('loc', [])))}: {err.get('msg')}"
                    for err in val_err.errors()
                ]
                msg = f"Arguments invalides pour '{tool_name}' — " + "; ".join(problems)
                return ToolResult(
                    ok=False,
                    result=msg,
                    error=f"ValidationError: {val_err}",
                    tool_name=tool_name,
                    call_id=ctx.call_id,
                    execution_time_ms=duration,
                    retryable=True,
                )
        else:
            call_kwargs = dict(clean_args)

        # 6. Injection du contexte d'exécution
        if tool_def.context_param_name:
            call_kwargs[tool_def.context_param_name] = ctx

        # 7. Exécution sous Sémaphore, Timeout et Concurrence
        sem = tool_def.get_semaphore()
        try:
            async with sem:
                if tool_def.rate_limiter is not None:
                    await tool_def.rate_limiter.record()

                async with asyncio.timeout(tool_def.timeout_s):
                    if tool_def.is_async:
                        raw_output = await tool_def.func(**call_kwargs)
                    else:
                        raw_output = await asyncio.to_thread(tool_def.func, **call_kwargs)

            # Succès : mise à jour du Circuit Breaker
            await tool_def.circuit_breaker.record_success()
            duration = (time.perf_counter() - started_at) * 1000.0

            result = ToolResult(
                ok=True,
                result=raw_output,
                tool_name=tool_name,
                call_id=ctx.call_id,
                execution_time_ms=duration,
                raw_response=raw_output,
            )
            ctx.record_history(
                name=tool_name,
                args=clean_args,
                result=raw_output,
                ok=True,
                duration_ms=duration,
            )
            return result

        except asyncio.TimeoutError:
            await tool_def.circuit_breaker.record_failure()
            duration = (time.perf_counter() - started_at) * 1000.0
            msg = f"L'action '{tool_name}' a dépassé son délai de sécurité ({tool_def.timeout_s}s)."
            ctx.record_history(
                name=tool_name,
                args=clean_args,
                result=msg,
                ok=False,
                duration_ms=duration,
            )
            return ToolResult(
                ok=False,
                result=msg,
                error="TimeoutError",
                tool_name=tool_name,
                call_id=ctx.call_id,
                execution_time_ms=duration,
                retryable=True,
            )

        except Exception as exc:
            await tool_def.circuit_breaker.record_failure()
            duration = (time.perf_counter() - started_at) * 1000.0
            error_msg = f"Échec de l'action '{tool_name}' : {type(exc).__name__}: {exc}"
            ctx.record_history(
                name=tool_name,
                args=clean_args,
                result=error_msg,
                ok=False,
                duration_ms=duration,
            )
            return ToolResult(
                ok=False,
                result=error_msg,
                error=str(exc),
                tool_name=tool_name,
                call_id=ctx.call_id,
                execution_time_ms=duration,
                retryable=True,
            )

    async def execute_batch(
        self,
        calls: Sequence[tuple[str, Mapping[str, Any]]],
        context: ExecutionContext | None = None,
    ) -> list[ToolResult]:
        """Exécute un lot d'appels : en parallèle si tous sont 'parallel_safe', sinon en séquence."""
        if not calls:
            return []

        all_parallel = all(
            (self._tools.get(name) is not None and self._tools[name].parallel_safe)
            for name, _ in calls
        )

        if all_parallel and len(calls) > 1:
            tasks = [self.execute(name, args, context) for name, args in calls]
            return list(await asyncio.gather(*tasks))

        results: list[ToolResult] = []
        for name, args in calls:
            results.append(await self.execute(name, args, context))
        return results


# ══════════════════════════════════════════════════════════════════════════════
# 8. DÉCORATEUR @tool
# ══════════════════════════════════════════════════════════════════════════════

# Instance globale singleton du registre
default_registry = ToolRegistry()
tool_registry = default_registry  # Alias usuel


def build_production_declarations(
    legacy_declarations: Sequence[Mapping[str, Any]],
) -> tuple[ToolRegistry, list[dict[str, Any]]]:
    """Construit le catalogue Gemini à partir du registre déclaratif.

    Les outils déjà migrés dont le contrat est strictement compatible prennent
    leur JSON Schema depuis les annotations Pydantic. Les autres sont montés
    par l'adaptateur, ce qui permet une migration progressive sans supprimer
    un paramètre historique potentiellement sensible.
    """
    from core import migrated_tools as _migrated_tools  # noqa: F401

    registry = ToolRegistry()
    compatible = {"system_status", "open_app", "weather_report", "point_on_screen"}
    legacy_names = {
        str(decl.get("name") or "") for decl in legacy_declarations
    }
    for name in compatible & legacy_names:
        definition = default_registry.get(name)
        if definition is not None:
            registry.register(definition)
    LegacyToolAdapter(registry, legacy_declarations).install()
    return registry, registry.to_declarations_dict()


def tool(
    _func: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    destructive: bool = False,
    rate_limit: str | None = None,
    requires_voice_id: bool = False,
    timeout_s: float = 35.0,
    max_concurrency: int = 1,
    failure_threshold: int = 3,
    cooldown_s: float = 30.0,
    aliases: dict[str, str] | None = None,
    parallel_safe: bool = False,
    registry: ToolRegistry | None = None,
) -> Any:
    """Décorateur pour enregistrer une fonction comme outil LLM typé et résilient.

    Usage :
        @tool(
            name="system_status",
            description="Retourne les métriques système en temps réel.",
            rate_limit="10/minute",
            destructive=False
        )
        async def get_system_status(detail_level: Literal["summary", "full"] = "summary") -> SystemStatusReport:
            ...
    """
    target_registry = registry or default_registry

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        tool_def = build_tool_metadata(
            fn,
            name=name,
            description=description,
            destructive=destructive,
            rate_limit=rate_limit,
            requires_voice_id=requires_voice_id,
            timeout_s=timeout_s,
            max_concurrency=max_concurrency,
            failure_threshold=failure_threshold,
            cooldown_s=cooldown_s,
            aliases=aliases,
            parallel_safe=parallel_safe,
        )
        target_registry.register(tool_def)
        fn.__tool_def__ = tool_def  # type: ignore[attr-defined]
        return fn

    if _func is not None:
        return decorator(_func)
    return decorator


# ══════════════════════════════════════════════════════════════════════════════
# 9. ADAPTATEUR DE TRANSITION : LegacyToolAdapter
# ══════════════════════════════════════════════════════════════════════════════

class LegacyToolAdapter:
    """Adaptateur permettant aux 40+ outils historiques de coexister immédiatement.

    Chaque outil déclaré dans les listes de dictionnaires historiques (TOOL_DECLARATIONS)
    mais non encore réécrit avec @tool est encapsulé avec circuit breaker, timeout
    et vérification vocale si destructive. Les outils migrés prennent automatiquement
    le dessus sur la version historique.
    """
    def __init__(
        self,
        registry: ToolRegistry,
        declarations: Sequence[Mapping[str, Any]] | None = None,
        dispatcher: Callable[[str, dict[str, Any], ExecutionContext], Any] | None = None,
        destructive_tools: set[str] | frozenset[str] | None = None,
        rate_limits: Mapping[str, str] | None = None,
        timeouts: Mapping[str, float] | None = None,
    ):
        self.registry = registry
        self.declarations = list(declarations or ())
        self.dispatcher = dispatcher
        self.destructive_tools = frozenset(destructive_tools or ())
        self.rate_limits = dict(rate_limits or {})
        self.timeouts = dict(timeouts or {})

    def install(self) -> int:
        """Installe les outils historiques non encore enregistrés dans le registre.

        Retourne le nombre d'outils hérités montés.
        """
        installed_count = 0
        for decl in self.declarations:
            t_name = str(decl.get("name") or "").strip()
            if not t_name:
                continue

            # Si l'outil a déjà été réécrit avec @tool, il prime sur l'hérité
            if self.registry.get(t_name) is not None:
                continue

            t_desc = str(decl.get("description") or "").strip()
            t_params_dict = decl.get("parameters") or {}
            genai_schema = dict_to_genai_schema(t_params_dict)

            is_destructive = t_name in self.destructive_tools
            r_limit = self.rate_limits.get(t_name)
            timeout = self.timeouts.get(t_name, 35.0)

            # Création du wrapper d'exécution
            dispatcher_fn = self.dispatcher

            async def _legacy_caller(
                _tname=t_name,
                _disp=dispatcher_fn,
                _context=None,
                **kwargs,
            ):
                if _disp is None:
                    raise ToolNotFoundError(f"Aucun exécuteur défini pour l'outil legacy '{_tname}'")
                res = _disp(_tname, kwargs, _context or ExecutionContext())
                if inspect.isawaitable(res):
                    return await res
                return res

            genai_declaration = types.FunctionDeclaration(
                name=t_name,
                description=t_desc,
                parameters=genai_schema,
            )

            tool_def = ToolDefinition(
                name=t_name,
                description=t_desc,
                func=_legacy_caller,
                is_async=True,
                destructive=is_destructive,
                rate_limit=r_limit,
                requires_voice_id=is_destructive,
                timeout_s=timeout,
                declaration=genai_declaration,
                context_param_name="_context",
            )
            self.registry.register(tool_def)
            installed_count += 1

        return installed_count
