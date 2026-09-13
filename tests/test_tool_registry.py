"""tests/test_tool_registry.py — Tests unitaires exhaustifs pour le registre d'outils ANO-GPT.

Valide :
1. Métaprogrammation et génération automatique de FunctionDeclaration / JSON Schema
2. Décorateur @tool et introspection des types (Pydantic, dataclasses, Literals, Optional)
3. Dispatcher dynamique et validation Pydantic des arguments
4. Injection transparente de ExecutionContext
5. Résilience : Rate Limiter (sliding window) et Circuit Breaker (états, cooldown, transition)
6. Sécurité vocale (authentification du locuteur et confirmation destructive)
7. Adaptateur de migration (LegacyToolAdapter) et coexistence avec l'ancien système
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Literal, Optional

from google.genai import types
from pydantic import BaseModel, Field

from core.tool_registry import (
    CircuitBreaker,
    ExecutionContext,
    LegacyToolAdapter,
    SlidingWindowRateLimiter,
    ToolRegistry,
    build_tool_metadata,
    build_production_declarations,
    parse_google_docstring,
    python_type_to_schema,
    tool,
)


def test_catalogue_production_conserve_tous_les_outils_et_parametres_sensibles():
    from core.tool_dispatcher import TOOL_DECLARATIONS

    registry, declarations = build_production_declarations(TOOL_DECLARATIONS)
    expected = {item["name"] for item in TOOL_DECLARATIONS}
    assert set(registry.list_tools()) == expected
    assert {item["name"] for item in declarations} == expected
    close_decl = next(item for item in declarations if item["name"] == "close_app")
    assert {"force", "selection", "target"} <= set(
        close_decl["parameters"]["properties"]
    )


# ══════════════════════════════════════════════════════════════════════════════
# 1. TESTS DE MÉTAPROGRAMMATION ET GÉNÉRATEUR DE SCHÉMAS
# ══════════════════════════════════════════════════════════════════════════════

def test_parse_google_docstring():
    doc = """Retourne les métriques système en temps réel.
Informations complémentaires sur le matériel.

Args:
    detail_level (Literal['summary', 'full']): Niveau de précision souhaité
        avec explications multilignes.
    timeout_s: Délai d'attente maximum.

Returns:
    SystemStatusReport: Le rapport d'activité.
"""
    desc, param_descs = parse_google_docstring(doc)
    assert desc == "Retourne les métriques système en temps réel.\nInformations complémentaires sur le matériel."
    assert "detail_level" in param_descs
    assert "Niveau de précision souhaité avec explications multilignes." in param_descs["detail_level"]
    assert param_descs["timeout_s"] == "Délai d'attente maximum."


def test_python_type_to_schema_primitives():
    s_str = python_type_to_schema(str, description="Une chaîne")
    assert s_str.type == types.Type.STRING
    assert s_str.description == "Une chaîne"

    s_int = python_type_to_schema(int)
    assert s_int.type == types.Type.INTEGER

    s_float = python_type_to_schema(float)
    assert s_float.type == types.Type.NUMBER

    s_bool = python_type_to_schema(bool)
    assert s_bool.type == types.Type.BOOLEAN


def test_python_type_to_schema_literal():
    lit_type = Literal["fast", "slow", "turbo"]
    s_lit = python_type_to_schema(lit_type, description="Vitesse")
    assert s_lit.type == types.Type.STRING
    assert s_lit.enum == ["fast", "slow", "turbo"]
    assert s_lit.description == "Vitesse"


def test_python_type_to_schema_union_and_optional():
    opt_type = int | None
    s_opt = python_type_to_schema(opt_type)
    assert s_opt.type == types.Type.INTEGER
    assert s_opt.nullable is True

    opt_type2 = Optional[str]
    s_opt2 = python_type_to_schema(opt_type2)
    assert s_opt2.type == types.Type.STRING
    assert s_opt2.nullable is True


def test_python_type_to_schema_pydantic_model():
    class UserProfile(BaseModel):
        username: str = Field(description="Nom d'utilisateur")
        age: int = 18
        roles: list[str] = Field(default_factory=list)

    s_model = python_type_to_schema(UserProfile)
    assert s_model.type == types.Type.OBJECT
    assert "username" in s_model.properties
    assert s_model.properties["username"].type == types.Type.STRING
    assert s_model.properties["username"].description == "Nom d'utilisateur"
    assert s_model.properties["age"].type == types.Type.INTEGER
    assert s_model.properties["roles"].type == types.Type.ARRAY
    assert s_model.properties["roles"].items.type == types.Type.STRING
    assert s_model.required == ["username"]


def test_python_type_to_schema_dataclass():
    @dataclasses.dataclass
    class Coords:
        lat: float
        lon: float
        name: str = "home"

    s_dc = python_type_to_schema(Coords)
    assert s_dc.type == types.Type.OBJECT
    assert "lat" in s_dc.properties
    assert s_dc.properties["lat"].type == types.Type.NUMBER
    assert set(s_dc.required) == {"lat", "lon"}


def test_build_tool_metadata_excludes_execution_context():
    def sample_action(
        city: str,
        limit: int = 5,
        context: ExecutionContext | None = None,
    ) -> str:
        """Cherche des informations.

        Args:
            city: Ville cible.
            limit: Nombre maximal.
        """
        return "ok"

    tdef = build_tool_metadata(sample_action)
    assert tdef.name == "sample_action"
    assert tdef.context_param_name == "context"
    params = tdef.declaration.parameters
    assert "city" in params.properties
    assert "limit" in params.properties
    assert "context" not in params.properties
    assert params.required == ["city"]


# ══════════════════════════════════════════════════════════════════════════════
# 2. TESTS DU DÉCORATEUR @tool ET ENREGISTREMENT DANS LE REGISTRE
# ══════════════════════════════════════════════════════════════════════════════

def test_tool_decorator_custom_registry():
    reg = ToolRegistry()

    @tool(
        name="custom_math",
        description="Effectue une addition.",
        destructive=False,
        rate_limit="60/minute",
        registry=reg,
    )
    def add_numbers(a: int, b: int = 10) -> int:
        """Additionne deux entiers.

        Args:
            a: Premier nombre.
            b: Deuxième nombre.
        """
        return a + b

    assert "custom_math" in reg.list_tools()
    tdef = reg.get("custom_math")
    assert tdef is not None
    assert tdef.description == "Effectue une addition."
    assert tdef.rate_limit == "60/minute"
    decl_dict = reg.to_declarations_dict()[0]
    assert decl_dict["name"] == "custom_math"
    assert "a" in decl_dict["parameters"]["properties"]
    assert decl_dict["parameters"]["required"] == ["a"]


# ══════════════════════════════════════════════════════════════════════════════
# 3. TESTS DU RATE LIMITER ET DU CIRCUIT BREAKER
# ══════════════════════════════════════════════════════════════════════════════

def test_sliding_window_rate_limiter():
    async def scenario():
        limiter = SlidingWindowRateLimiter("3/second")

        # 3 appels autorisés
        for _ in range(3):
            allowed, wait = await limiter.can_execute()
            assert allowed is True
            assert wait == 0.0
            await limiter.record()

        # Le 4e doit être bloqué
        allowed, wait = await limiter.can_execute()
        assert allowed is False
        assert wait > 0.0

        # Attente de l'expiration de la fenêtre
        await asyncio.sleep(1.05)
        allowed, _ = await limiter.can_execute()
        assert allowed is True

    asyncio.run(scenario())


def test_circuit_breaker_lifecycle():
    async def scenario():
        cb = CircuitBreaker(failure_threshold=2, cooldown_s=0.2)
        assert cb.state == "CLOSED"

        # Premier échec
        await cb.record_failure()
        assert cb.state == "CLOSED"
        allowed, _ = await cb.can_execute()
        assert allowed is True

        # Deuxième échec -> circuit ouvert
        await cb.record_failure()
        assert cb.state == "OPEN"
        allowed, remaining = await cb.can_execute()
        assert allowed is False
        assert remaining > 0.0

        # Attente du cooldown -> passage en HALF_OPEN
        await asyncio.sleep(0.25)
        allowed, _ = await cb.can_execute()
        assert allowed is True
        assert cb.state == "HALF_OPEN"

        # Succès d'essai -> refermeture
        await cb.record_success()
        assert cb.state == "CLOSED"

    asyncio.run(scenario())


# ══════════════════════════════════════════════════════════════════════════════
# 4. TESTS DU DISPATCHER DYNAMIQUE (VALIDATION ET EXÉCUTION)
# ══════════════════════════════════════════════════════════════════════════════

def test_dispatcher_sync_and_async_execution():
    async def scenario():
        reg = ToolRegistry()

        @tool(name="sync_echo", registry=reg)
        def sync_echo(msg: str) -> str:
            return f"sync: {msg}"

        @tool(name="async_echo", registry=reg)
        async def async_echo(msg: str) -> str:
            await asyncio.sleep(0.01)
            return f"async: {msg}"

        res1 = await reg.execute("sync_echo", {"msg": "hello"})
        assert res1.ok is True
        assert res1.result == "sync: hello"

        res2 = await reg.execute("async_echo", {"msg": "world"})
        assert res2.ok is True
        assert res2.result == "async: world"

    asyncio.run(scenario())


def test_dispatcher_pydantic_validation_and_coercion():
    async def scenario():
        reg = ToolRegistry()

        @tool(name="process_item", registry=reg)
        def process_item(item_id: int, tags: list[str] = None) -> dict:
            return {"item_id": item_id, "tags": tags or []}

        # Coercition automatique de string vers int et découpage de liste
        res = await reg.execute("process_item", {"item_id": "42", "tags": "a, b, c"})
        assert res.ok is True
        assert res.result == {"item_id": 42, "tags": ["a", "b", "c"]}

        # Erreur de validation gracieuse (ne fait pas crasher l'app)
        res_err = await reg.execute("process_item", {"item_id": "not_an_int"})
        assert res_err.ok is False
        assert "Arguments invalides" in res_err.result
        assert res_err.retryable is True

    asyncio.run(scenario())


def test_dispatcher_context_injection():
    async def scenario():
        reg = ToolRegistry()

        @tool(name="contextual_tool", registry=reg)
        async def contextual_tool(text: str, context: ExecutionContext) -> str:
            return f"User {context.user_id} said {text}"

        ctx = ExecutionContext(user_id="Alice")
        res = await reg.execute("contextual_tool", {"text": "Bonjour"}, context=ctx)
        assert res.ok is True
        assert res.result == "User Alice said Bonjour"

    asyncio.run(scenario())


# ══════════════════════════════════════════════════════════════════════════════
# 5. TESTS DE SÉCURITÉ VOCALE ET DE CONFIRMATION DESTRUCTIVE
# ══════════════════════════════════════════════════════════════════════════════

def test_dispatcher_voice_stranger_blocks_destructive_tool():
    async def scenario():
        reg = ToolRegistry()

        @tool(name="format_disk", destructive=True, registry=reg)
        async def format_disk(drive: str) -> str:
            return f"Disk {drive} formatted"

        # Appelant légitime
        ctx_owner = ExecutionContext(voice_verified=True, is_stranger=False)
        res_ok = await reg.execute("format_disk", {"drive": "C"}, context=ctx_owner)
        assert res_ok.ok is True

        # Appelant étranger
        ctx_stranger = ExecutionContext(voice_verified=False, is_stranger=True)
        res_refused = await reg.execute("format_disk", {"drive": "C"}, context=ctx_stranger)
        assert res_refused.ok is False
        assert "REFUSÉ" in res_refused.result
        assert "voix courante n'est pas reconnue" in res_refused.result

    asyncio.run(scenario())


def test_dispatcher_precision_stt_verification_divergence():
    async def scenario():
        reg = ToolRegistry()

        @tool(name="delete_database", destructive=True, registry=reg)
        async def delete_database(db: str) -> str:
            return "deleted"

        async def mock_divergent_verifier(tool_name: str) -> str:
            return "ACTION NON EXÉCUTÉE : divergence STT détectée."

        ctx = ExecutionContext(
            voice_verified=True,
            is_stranger=False,
            voice_verifier=mock_divergent_verifier,
        )
        res = await reg.execute("delete_database", {"db": "prod"}, context=ctx)
        assert res.ok is False
        assert "divergence STT détectée" in res.result

    asyncio.run(scenario())


# ══════════════════════════════════════════════════════════════════════════════
# 6. TESTS DE LA SUITE D'OUTILS MIGRÉS (5 OUTILS REPRÉSENTATIFS)
# ══════════════════════════════════════════════════════════════════════════════

def test_migrated_tools_declaration_and_execution():
    async def scenario():
        from core.migrated_tools import SystemStatusReport
        from core.tool_registry import tool_registry

        # 1. Vérification de la présence des 5 outils
        for expected in ("system_status", "open_app", "close_app", "weather_report", "shell_exec"):
            assert expected in tool_registry.list_tools()

        # 2. Test system_status
        ctx = ExecutionContext()
        res_status = await tool_registry.execute("system_status", {"detail_level": "summary"}, ctx)
        assert res_status.ok is True
        assert isinstance(res_status.result, SystemStatusReport)
        assert res_status.result.cpu_percent >= 0.0
        func_resp = res_status.to_function_response(call_id="call-99")
        assert func_resp.name == "system_status"
        assert func_resp.response["ok"] is True
        assert "cpu_percent" in func_resp.response["result"]

        # 3. Test open_app avec alias d'argument
        t_open = tool_registry.get("open_app")
        assert "app" in t_open.aliases
        assert t_open.destructive is False

        # 4. Test close_app (destructive)
        t_close = tool_registry.get("close_app")
        assert t_close.destructive is True
        assert t_close.requires_voice_id is True

        # 5. Test batch execution
        batch_res = await tool_registry.execute_batch([
            ("system_status", {"detail_level": "summary"}),
            ("system_status", {"detail_level": "summary"}),
        ], context=ctx)
        assert len(batch_res) == 2
        assert all(r.ok for r in batch_res)

    asyncio.run(scenario())


# ══════════════════════════════════════════════════════════════════════════════
# 7. TESTS DE L'ADAPTATEUR DE TRANSITION (LegacyToolAdapter)
# ══════════════════════════════════════════════════════════════════════════════

def test_legacy_tool_adapter_progressive_migration():
    async def scenario():
        reg = ToolRegistry()

        # Outil moderne enregistré en premier
        @tool(name="migrated_calc", registry=reg)
        def modern_calc(x: int) -> int:
            return x * 2

        # Liste historique simulant TOOL_DECLARATIONS
        legacy_declarations = [
            {
                "name": "migrated_calc",  # Doit être ignoré car déjà migré
                "description": "Ancien calculateur.",
                "parameters": {"type": "OBJECT", "properties": {"x": {"type": "INTEGER"}}},
            },
            {
                "name": "legacy_notepad",  # Doit être adapté
                "description": "Ouvre le bloc-notes historique.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {"filename": {"type": "STRING"}},
                    "required": ["filename"],
                },
            },
        ]

        legacy_called = []

        def mock_legacy_dispatcher(name: str, args: dict, context: ExecutionContext):
            legacy_called.append((name, args))
            return f"Legacy {name} called with {args.get('filename')}"

        adapter = LegacyToolAdapter(
            registry=reg,
            declarations=legacy_declarations,
            dispatcher=mock_legacy_dispatcher,
            destructive_tools={"legacy_notepad"},
        )
        mounted = adapter.install()
        assert mounted == 1  # Seulement legacy_notepad est monté
        assert "migrated_calc" in reg.list_tools()
        assert "legacy_notepad" in reg.list_tools()

        # Exécution de l'outil moderne (utilise la fonction moderne)
        res_mod = await reg.execute("migrated_calc", {"x": 5})
        assert res_mod.ok is True
        assert res_mod.result == 10

        # Exécution de l'outil adapté (utilise le mock legacy dispatcher)
        ctx = ExecutionContext(voice_verified=True, is_stranger=False)
        res_leg = await reg.execute("legacy_notepad", {"filename": "memo.txt"}, ctx)
        assert res_leg.ok is True
        assert "memo.txt" in str(res_leg.result)
        assert len(legacy_called) == 1

        # Vérification que le disjoncteur et la voix protègent aussi l'outil legacy
        ctx_stranger = ExecutionContext(voice_verified=False, is_stranger=True)
        res_blocked = await reg.execute("legacy_notepad", {"filename": "memo.txt"}, ctx_stranger)
        assert res_blocked.ok is False
        assert "REFUSÉ" in res_blocked.result

    asyncio.run(scenario())
