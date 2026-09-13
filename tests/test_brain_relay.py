"""Le fournisseur choisi pense pour tout ; Gemini ne garde que la voix."""

import asyncio
import json
import types

import pytest

from core import brain_relay, llm_client


@pytest.fixture
def config(tmp_path, monkeypatch):
    path = tmp_path / "api_keys.json"
    monkeypatch.setattr(llm_client, "CONFIG_PATH", path)
    llm_client._config_cache.update({"mtime": None, "data": {}})

    def write(**values):
        path.write_text(json.dumps(values), encoding="utf-8")
        llm_client._config_cache.update({"mtime": None, "data": {}})

    return write


def test_gemini_seul_ne_declenche_aucun_relais(config):
    config(llm_provider="gemini", gemini_api_key="g-secret")
    assert llm_client.main_brain() is None
    assert llm_client.relay_active() is False


def test_le_fournisseur_choisi_devient_le_cerveau(config):
    config(
        llm_provider="azure_openai",
        azure_openai_api_key="az-secret",
        azure_openai_model="anogpt-brain",
        gemini_api_key="g-secret",
    )
    assert llm_client.main_brain() == ("azure_openai", "anogpt-brain")
    assert llm_client.relay_active() is True
    assert "Azure" in llm_client.main_brain_label()


def test_openrouter_devient_le_cerveau_sans_changer_la_voix(config):
    config(
        llm_provider="openrouter",
        openrouter_api_key="or-secret",
        openrouter_model="google/gemini-2.5-pro",
        gemini_api_key="g-secret",
    )
    assert llm_client.main_brain() == ("openrouter", "google/gemini-2.5-pro")
    # Le relais actif signifie que Gemini Live conserve seulement l'écoute/TTS.
    assert llm_client.relay_active() is True


def test_un_fournisseur_sans_cle_ne_prend_jamais_la_main(config):
    """Choisir OpenAI sans clé doit rendre la parole à Gemini, pas au vide."""
    config(llm_provider="openai", gemini_api_key="g-secret")
    assert llm_client.main_brain() is None
    assert llm_client.relay_active() is False


def test_le_mode_auto_suit_la_premiere_cle_disponible(config):
    config(
        llm_provider="auto",
        brain_provider="auto",
        deepseek_api_key="ds-secret",
        gemini_api_key="g-secret",
    )
    assert llm_client.main_brain() == ("deepseek", "deepseek-v4-flash")


def test_les_outils_sont_traduits_au_format_openai():
    declarations = [
        {
            "name": "open_app",
            "description": "Ouvre une application.",
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "app_name": {"type": "STRING", "description": "Nom exact"},
                    "workspace": {"type": "INTEGER"},
                },
                "required": ["app_name"],
            },
        },
        # Jamais offert au cerveau : il se rappellerait lui-même sans fin.
        {"name": "consult_brain", "description": "…", "parameters": {}},
    ]
    tools = brain_relay.openai_tools(declarations)
    assert [tool["function"]["name"] for tool in tools] == ["open_app"]
    schema = tools[0]["function"]["parameters"]
    assert schema["type"] == "object"
    assert schema["properties"]["app_name"]["type"] == "string"
    assert schema["properties"]["workspace"]["type"] == "integer"
    assert schema["required"] == ["app_name"]


class _Dispatcher:
    """Répartiteur minimal : il note ce qu'on lui demande d'exécuter."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def _execute_tool_batch(self, function_calls):
        responses = []
        for fc in function_calls:
            self.calls.append((fc.name, fc.args))
            responses.append(types.SimpleNamespace(
                id=fc.id, name=fc.name,
                response={"result": "Kitty est ouvert.", "ok": True},
            ))
        return responses


def test_le_cerveau_execute_les_outils_puis_conclut(monkeypatch, config):
    config(llm_provider="deepseek", deepseek_api_key="ds-secret")
    tours = []

    def fake_call_brain(messages, tools, timeout):
        tours.append(list(messages))
        if len(tours) == 1:
            return {"content": "", "tool_calls": [{
                "id": "call-1",
                "function": {"name": "open_app", "arguments": {"app_name": "kitty"}},
            }]}
        return {"content": "C'est ouvert.", "tool_calls": []}

    monkeypatch.setattr(brain_relay, "call_brain", fake_call_brain)
    dispatcher = _Dispatcher()
    answer = asyncio.run(brain_relay.run_turn(
        dispatcher, "ouvre kitty",
        declarations=[{"name": "open_app", "description": "Ouvre.",
                       "parameters": {"type": "OBJECT", "properties": {}}}],
        base_prompt="Tu es ANO-GPT.",
    ))

    assert answer == "C'est ouvert."
    assert dispatcher.calls == [("open_app", {"app_name": "kitty"})]
    # Le résultat de l'outil doit revenir au modèle, sinon il conclut à l'aveugle.
    derniers = tours[-1]
    assert derniers[-1]["role"] == "tool"
    assert "Kitty est ouvert." in derniers[-1]["content"]


def test_un_cerveau_qui_boucle_finit_par_conclure(monkeypatch, config):
    """Sans borne, un modèle qui rappelle sans fin garderait la voix muette."""
    config(llm_provider="deepseek", deepseek_api_key="ds-secret")
    appels = {"n": 0}

    def fake_call_brain(messages, tools, timeout):
        appels["n"] += 1
        if tools is None:
            return {"content": "Voilà où j'en suis.", "tool_calls": []}
        return {"content": "", "tool_calls": [{
            "id": f"call-{appels['n']}",
            "function": {"name": "open_app", "arguments": {"app_name": "kitty"}},
        }]}

    monkeypatch.setattr(brain_relay, "call_brain", fake_call_brain)
    answer = asyncio.run(brain_relay.run_turn(
        _Dispatcher(), "ouvre kitty",
        declarations=[{"name": "open_app", "description": "Ouvre.",
                       "parameters": {"type": "OBJECT", "properties": {}}}],
    ))
    assert answer == "Voilà où j'en suis."
    assert appels["n"] == brain_relay.MAX_TOOL_ROUNDS + 1


def test_sans_cerveau_le_relais_refuse_de_repondre(config):
    config(llm_provider="gemini", gemini_api_key="g-secret")
    with pytest.raises(RuntimeError):
        asyncio.run(brain_relay.run_turn(_Dispatcher(), "salut"))


def test_gemini_ne_garde_que_l_outil_de_consultation():
    """En relais, laisser les autres outils à Gemini, c'est l'inviter à agir."""
    from core.session_manager import SessionManager
    from core.tool_dispatcher import CONSULT_BRAIN_DECLARATION

    class _Stub(SessionManager):
        _tool_declarations = [{"name": "open_app"}, {"name": "web_search"}]
        _plugins = types.SimpleNamespace(declarations=lambda: [])
        _relay_enabled = True

        def __init__(self):  # pragma: no cover - aucun état à construire
            pass

    stub = _Stub()
    live = SessionManager._live_declarations(stub)
    assert live == [CONSULT_BRAIN_DECLARATION]

    stub._relay_enabled = False
    assert [decl["name"] for decl in SessionManager._live_declarations(stub)] == [
        "open_app", "web_search",
    ]
