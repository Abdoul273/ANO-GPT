"""Routage du cerveau optionnel : priorité, absence de clé et repli."""

import json

import pytest

from core import llm_client


@pytest.fixture
def brain_config(tmp_path, monkeypatch):
    path = tmp_path / "api_keys.json"
    monkeypatch.setattr(llm_client, "CONFIG_PATH", path)
    llm_client._config_cache.update({"mtime": None, "data": {}})

    def write(**values):
        path.write_text(json.dumps(values), encoding="utf-8")
        llm_client._config_cache.update({"mtime": None, "data": {}})

    return write


def test_aucune_cle_ne_force_un_fournisseur(brain_config):
    brain_config(brain_provider="auto")
    assert llm_client.resolve_brain_provider() is None
    assert llm_client.think_deep("Question") == llm_client.BRAIN_UNCONFIGURED


def test_auto_priorise_deepseek_puis_grok(brain_config):
    brain_config(
        brain_provider="auto",
        deepseek_api_key="ds-secret",
        xai_api_key="xai-secret",
    )
    assert llm_client.configured_brain_providers()[:2] == ["deepseek", "grok"]
    assert llm_client.resolve_brain_provider() == "deepseek"


def test_auto_priorise_azure_openai_lorsqu_il_est_configure(brain_config):
    brain_config(
        brain_provider="auto",
        azure_openai_api_key="azure-secret",
        deepseek_api_key="ds-secret",
    )
    assert llm_client.configured_brain_providers()[:2] == ["azure_openai", "deepseek"]
    assert llm_client.resolve_brain_provider() == "azure_openai"


def test_endpoint_azure_utilise_le_deploiement_et_la_version_ga():
    assert llm_client._azure_openai_endpoint(
        "https://ano.openai.azure.com/", "mon-gpt"
    ) == (
        "https://ano.openai.azure.com/openai/deployments/mon-gpt/"
        "chat/completions?api-version=2024-10-21"
    )


def test_endpoint_azure_refuse_une_url_non_azure():
    with pytest.raises(ValueError, match="Endpoint Azure invalide"):
        llm_client._azure_openai_endpoint("https://example.test", "mon-gpt")


def test_une_priorite_personnalisee_est_respectee(brain_config):
    brain_config(
        brain_provider="auto",
        brain_provider_priority=["grok", "deepseek"],
        deepseek_api_key="ds-secret",
        xai_api_key="xai-secret",
    )
    assert llm_client.resolve_brain_provider() == "grok"


def test_repli_sur_grok_si_deepseek_echoue(brain_config, monkeypatch):
    brain_config(
        brain_provider="auto",
        deepseek_api_key="ds-secret",
        xai_api_key="xai-secret",
    )
    calls = []

    def fake_call(messages, tools, timeout, provider, **kwargs):
        calls.append(provider)
        if provider == "deepseek":
            raise RuntimeError("quota")
        return {"content": "Réponse de secours.", "tool_calls": []}

    monkeypatch.setattr(llm_client, "_call_openai_compat", fake_call)
    assert llm_client.think_deep("Question") == "Réponse de secours."
    assert calls == ["deepseek", "grok"]


def test_enregistrer_une_cle_ne_change_pas_le_provider(brain_config):
    brain_config(llm_provider="gemini", brain_provider="auto")
    assert llm_client.save_provider_config("deepseek", "ds-secret", "deepseek-v4-flash")
    saved = json.loads(llm_client.CONFIG_PATH.read_text(encoding="utf-8"))
    assert saved["llm_provider"] == "gemini"
    assert saved["brain_provider"] == "auto"
    assert saved["deepseek_model"] == "deepseek-v4-flash"


def test_appliquer_gemini_est_restitue_comme_fournisseur_actif(brain_config):
    brain_config(brain_provider="auto", gemini_api_key="gem-secret")

    assert llm_client.set_active_provider("gemini", "gemini-flash-latest")

    saved = json.loads(llm_client.CONFIG_PATH.read_text(encoding="utf-8"))
    assert saved["llm_provider"] == "gemini"
    assert saved["llm_model"] == "gemini-flash-latest"
    assert llm_client.get_llm_provider() == "gemini"
    assert next(item for item in llm_client.list_providers() if item["id"] == "gemini")["active"]


def test_modeles_cloud_par_defaut_sont_actuels():
    assert llm_client.PROVIDERS["deepseek"]["default_model"] == "deepseek-v4-flash"
    assert llm_client.PROVIDERS["grok"]["default_model"] == "grok-4.3"
    assert llm_client.PROVIDERS["openai"]["default_model"] == "gpt-5.6-terra"
    assert llm_client.PROVIDERS["anthropic"]["default_model"] == "claude-sonnet-5"
