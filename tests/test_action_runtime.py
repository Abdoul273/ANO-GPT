import asyncio
import types as builtin_types

import pytest

from core.action_runtime import (
    ActionCircuitOpen,
    ActionPolicy,
    ActionRuntime,
    ActionValidationError,
)


DECLARATIONS = [
    {
        "name": "send_message",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "receiver": {"type": "STRING"},
                "message_text": {"type": "STRING"},
                "platform": {"type": "STRING"},
                "confirm": {"type": "BOOLEAN"},
            },
            "required": ["receiver", "message_text", "platform"],
        },
    },
    {
        "name": "show_map",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "lat": {"type": "NUMBER"},
                "lon": {"type": "NUMBER"},
                "radius_km": {"type": "NUMBER"},
            },
            "required": [],
        },
    },
]


def test_alias_types_et_champs_inventes_sont_normalises():
    runtime = ActionRuntime(DECLARATIONS)
    args = runtime.prepare(
        "send_message",
        {
            "recipient": "  Alice  ",
            "message": " Bonjour ",
            "service": " WhatsApp ",
            "confirm": "oui",
            "hallucinated_field": "ignored",
        },
    )
    assert args == {
        "receiver": "Alice",
        "message_text": "Bonjour",
        "platform": "WhatsApp",
        "confirm": True,
    }


def test_parametre_requis_manquant_produit_une_erreur_actionnable():
    runtime = ActionRuntime(DECLARATIONS)
    with pytest.raises(ActionValidationError, match="receiver"):
        runtime.prepare(
            "send_message",
            {"message_text": "Bonjour", "platform": "Signal"},
        )


def test_nombres_invalides_et_infinis_sont_refuses():
    runtime = ActionRuntime(DECLARATIONS)
    with pytest.raises(ActionValidationError, match="lat"):
        runtime.prepare("show_map", {"lat": "inf", "lon": 2})


def test_resultat_negatif_normal_ne_declenche_pas_le_disjoncteur():
    assert ActionRuntime.looks_failed("Lieu introuvable près de vous") is False
    assert ActionRuntime.looks_failed("Tool 'show_map' failed: réseau") is True
    assert ActionRuntime.looks_failed("Impossible de trouver cette vidéo") is True
    assert ActionRuntime.should_trip_from_result("Impossible de trouver cette vidéo") is False
    assert ActionRuntime.should_trip_from_result("Tool 'show_map' failed: réseau") is True


def test_lectures_independantes_peuvent_etre_parallelisees():
    assert ActionRuntime.can_run_in_parallel("web_search") is True
    assert ActionRuntime.can_run_in_parallel("send_message") is False


def test_circuit_breaker_coupe_les_echecs_en_boucle():
    runtime = ActionRuntime(DECLARATIONS)
    for _ in range(3):
        runtime.note_failure("show_map")
    with pytest.raises(ActionCircuitOpen):
        runtime.ensure_available("show_map")
    runtime.note_success("show_map")
    runtime.ensure_available("show_map")


def test_historique_borne_et_secrets_masques():
    runtime = ActionRuntime(DECLARATIONS)
    memory = {}
    for index in range(20):
        runtime.remember(
            memory,
            name="send_message",
            args={"receiver": "Alice", "api_key": "secret-value", "index": index},
            result="ok",
            ok=True,
            duration_ms=12.5,
        )
    assert len(memory["_action_history"]) == 12
    assert memory["_last_action"]["args"]["api_key"] == "[REDACTED]"
    assert memory["_action_history"][0]["args"]["index"] == 8


def test_lease_limite_la_concurrence_dune_action():
    runtime = ActionRuntime(DECLARATIONS)
    active = 0
    peak = 0

    async def worker():
        nonlocal active, peak
        async with runtime.lease("show_map"):
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1

    async def scenario():
        await asyncio.gather(*(worker() for _ in range(5)))

    asyncio.run(scenario())
    assert peak == 1


def test_repartiteur_central_rejette_avant_execution(monkeypatch):
    import main

    class UI:
        muted = False

        def __init__(self):
            self.logs = []
            self.states = []

        def write_log(self, text):
            self.logs.append(text)

        def set_state(self, state):
            self.states.append(state)

    class FunctionResponse:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    jarvis = main.JarvisLive.__new__(main.JarvisLive)
    jarvis.ui = UI()
    jarvis._tool_session_memory = {}
    jarvis._action_runtime = ActionRuntime(DECLARATIONS)
    called = False

    async def should_not_run(*_args):
        nonlocal called
        called = True

    jarvis._execute_tool_impl = should_not_run
    monkeypatch.setattr(main, "types", builtin_types.SimpleNamespace(FunctionResponse=FunctionResponse))
    monkeypatch.setattr(main.tool_stats, "record", lambda *a, **k: None)
    fc = builtin_types.SimpleNamespace(
        id="1", name="send_message", args={"message_text": "bonjour"}
    )

    # Les erreurs du modèle ne doivent jamais ouvrir le disjoncteur de l'outil.
    for _ in range(4):
        response = asyncio.run(jarvis._execute_tool(fc))

    assert called is False
    assert response.response["ok"] is False
    assert "paramètres requis" in response.response["result"]
    assert jarvis._tool_session_memory["_last_action"]["ok"] is False
    jarvis._action_runtime.ensure_available("send_message")


def test_repartiteur_central_borne_une_action_bloquee(monkeypatch):
    import core.action_runtime as runtime_module
    import main

    class UI:
        muted = False

        def write_log(self, _text):
            pass

        def set_state(self, _state):
            pass

    class FunctionResponse:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    declarations = [{
        "name": "slow",
        "parameters": {"type": "OBJECT", "properties": {}, "required": []},
    }]
    jarvis = main.JarvisLive.__new__(main.JarvisLive)
    jarvis.ui = UI()
    jarvis._tool_session_memory = {}
    jarvis._action_runtime = ActionRuntime(declarations)

    async def blocked(*_args):
        await asyncio.sleep(1)

    jarvis._execute_tool_impl = blocked
    monkeypatch.setitem(
        runtime_module._POLICIES,
        "slow",
        ActionPolicy(timeout_s=0.01),
    )
    monkeypatch.setattr(main, "types", builtin_types.SimpleNamespace(FunctionResponse=FunctionResponse))
    monkeypatch.setattr(main.tool_stats, "record", lambda *a, **k: None)
    fc = builtin_types.SimpleNamespace(id="2", name="slow", args={})

    response = asyncio.run(jarvis._execute_tool(fc))

    assert response.response["ok"] is False
    assert "délai" in response.response["result"]


def test_timeout_vision_libere_le_verrou_et_interdit_la_relance_automatique(monkeypatch):
    import core.action_runtime as runtime_module
    import main

    class UI:
        muted = False
        def write_log(self, _text): pass
        def set_state(self, _state): pass

    class FunctionResponse:
        def __init__(self, **kwargs): self.__dict__.update(kwargs)

    declarations = [{
        "name": "screen_process",
        "parameters": {"type": "OBJECT", "properties": {}, "required": []},
    }]
    jarvis = main.JarvisLive.__new__(main.JarvisLive)
    jarvis.ui = UI()
    jarvis._tool_session_memory = {}
    jarvis._action_runtime = ActionRuntime(declarations)
    jarvis._vision_busy = True
    jarvis._pending_vision = (b"old", "image/jpeg", "old", "screen")
    jarvis._interrupted = False
    jarvis._noise_turn = False
    jarvis._event_bus = None

    async def verified(_name): return ""
    async def blocked(*_args): await asyncio.sleep(1)
    jarvis._verify_sensitive_voice_command = verified
    jarvis._execute_tool_impl = blocked
    monkeypatch.setitem(runtime_module._POLICIES, "screen_process", ActionPolicy(timeout_s=0.01))
    monkeypatch.setattr(main, "types", builtin_types.SimpleNamespace(FunctionResponse=FunctionResponse))
    monkeypatch.setattr(main.tool_stats, "record", lambda *a, **k: None)

    response = asyncio.run(jarvis._execute_tool(
        builtin_types.SimpleNamespace(id="vision", name="screen_process", args={})
    ))

    assert jarvis._vision_busy is False
    assert jarvis._pending_vision is None
    assert "ne relance pas automatiquement" in response.response["result"]


def test_lot_de_lectures_independantes_est_execute_en_parallele():
    import main

    jarvis = main.JarvisLive.__new__(main.JarvisLive)
    jarvis._action_runtime = ActionRuntime(DECLARATIONS)
    active = 0
    peak = 0

    async def fake_execute(fc):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return fc.id

    jarvis._execute_tool = fake_execute
    calls = [
        builtin_types.SimpleNamespace(id="a", name="web_search", args={}),
        builtin_types.SimpleNamespace(id="b", name="weather_report", args={}),
    ]

    responses = asyncio.run(jarvis._execute_tool_batch(calls))

    assert responses == ["a", "b"]
    assert peak == 2


def test_lot_avec_action_mutante_conserve_lordre():
    import main

    jarvis = main.JarvisLive.__new__(main.JarvisLive)
    jarvis._action_runtime = ActionRuntime(DECLARATIONS)
    active = 0
    peak = 0

    async def fake_execute(fc):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.005)
        active -= 1
        return fc.id

    jarvis._execute_tool = fake_execute
    calls = [
        builtin_types.SimpleNamespace(id="a", name="web_search", args={}),
        builtin_types.SimpleNamespace(id="b", name="send_message", args={}),
    ]

    responses = asyncio.run(jarvis._execute_tool_batch(calls))

    assert responses == ["a", "b"]
    assert peak == 1


@pytest.mark.parametrize("value", ["invalide", "x" * 100])
def test_recovered_text_still_obeys_schema(value):
    runtime = ActionRuntime([{"name": "choose", "parameters": {
        "properties": {"choice": {"type": "STRING", "enum": ["yes", "no"], "maxLength": 3}},
        "required": ["choice"],
    }}])
    with pytest.raises(ActionValidationError):
        runtime.prepare("choose", {"invented": value})
    assert runtime.prepare("choose", {"invented": "yes"}) == {"choice": "yes"}
