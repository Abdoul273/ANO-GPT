"""Contrat entre les outils MCP annoncés et ceux réellement servis par ANO-GPT."""

import inspect

import pytest

import anogpt_mcp
from main import JarvisLive


def test_chaque_outil_mcp_existe_et_explique_quand_lutiliser():
    """La docstring est la seule interface de découverte disponible à l'agent."""
    tools = anogpt_mcp.mcp._tool_manager._tools
    assert tools
    assert all(tool.description and tool.description.strip() for tool in tools.values())


def test_la_liste_de_selftest_reflete_exactement_les_outils_decores():
    """Un selftest incomplet donnerait une fausse assurance lors de l'installation."""
    decorated = set(anogpt_mcp.mcp._tool_manager._tools)
    assert set(anogpt_mcp._TOOL_NAMES) == decorated
    assert len(anogpt_mcp._TOOL_NAMES) == len(decorated)


def test_chaque_nom_mcp_possede_un_handler_dans_lapplication():
    """Un outil visible mais absent du socket échouerait seulement en production."""
    app = JarvisLive.__new__(JarvisLive)
    app._agent_tool_table = None
    table = app._agent_tools()
    expected = set(anogpt_mcp._APP_TOOL_NAMES.values())
    assert set(table) == expected
    assert all(callable(table[name]) for name in expected)


def test_les_replis_hors_ligne_suivent_la_classification_annoncee():
    """Une action visuelle invisible est plus trompeuse qu'un refus explicite."""
    assert not (anogpt_mcp._OFFLINE_TOOLS & anogpt_mcp._SCREEN_REQUIRED_TOOLS)
    assert anogpt_mcp._OFFLINE_TOOLS | anogpt_mcp._SCREEN_REQUIRED_TOOLS == set(
        anogpt_mcp._TOOL_NAMES
    )
    for name in anogpt_mcp._TOOL_NAMES:
        fn = getattr(anogpt_mcp, name)
        fn_target = getattr(fn, "fn", fn)
        source = inspect.getsource(fn_target)
        has_fallback = "offline=" in source
        assert has_fallback is (name in anogpt_mcp._OFFLINE_TOOLS), name


class _UiVide:
    muted = False

    def close_map(self):
        return None

    def show_map(self, *args, **kwargs):
        return None

    def show_card(self, *args, **kwargs):
        return None

    def close_image_gallery(self):
        return None

    def write_log(self, *args, **kwargs):
        return None

    def control_hud_appearance(self, *args, **kwargs):
        return "ok"


@pytest.fixture
def application_minimale(monkeypatch):
    """Neutralise les effets externes : ce test vérifie le contrat d'arguments."""
    app = JarvisLive.__new__(JarvisLive)
    app._agent_tool_table = None
    app._tool_session_memory = {}
    app.ui = _UiVide()
    app.session = None
    app._camera = None
    app._camera_tool = lambda *args: "caméra simulée"

    monkeypatch.setattr("actions.find_nearby.find_nearby", lambda *a, **k: "ok")
    monkeypatch.setattr("actions.email.email_control", lambda *a, **k: "ok")
    monkeypatch.setattr("actions.system_monitor.system_status_tool", lambda *a, **k: "ok")
    monkeypatch.setattr("actions.music.music_control", lambda *a, **k: "ok")
    monkeypatch.setattr("actions.weather_report.weather_report", lambda *a, **k: "ok")
    monkeypatch.setattr("actions.web_search.web_search", lambda *a, **k: "ok")
    monkeypatch.setattr("actions.image_search.image_search", lambda *a, **k: "ok")
    monkeypatch.setattr("actions.capture.capture_control", lambda *a, **k: "ok")
    monkeypatch.setattr("actions.open_app.open_app", lambda *a, **k: "ok")
    monkeypatch.setattr("actions.close_app.close_app", lambda *a, **k: "ok")
    monkeypatch.setattr("actions.computer_settings.computer_settings", lambda *a, **k: "ok")
    monkeypatch.setattr("actions.file_controller.file_controller", lambda *a, **k: "ok")
    monkeypatch.setattr("actions.youtube_video.youtube_video", lambda *a, **k: "ok")
    monkeypatch.setattr("actions.reminder.reminder", lambda *a, **k: "ok")
    monkeypatch.setattr("actions.auto_debug.auto_debug_action", lambda *a, **k: "ok")
    monkeypatch.setattr("actions.navigation.navigation_action", lambda *a, **k: "ok")
    monkeypatch.setattr("core.multimodal_vision.inspect_screen_live", lambda *a, **k: ("ok", None))
    monkeypatch.setattr("core.geolocation.get_user_location", lambda **k: {"city": "Conakry"})
    monkeypatch.setattr("memory.memory_manager.remember", lambda *a, **k: "ok")
    monkeypatch.setattr("memory.memory_manager.load_memory", lambda: {})
    monkeypatch.setattr("memory.memory_manager.format_memory_for_prompt", lambda m: "")
    return app


def test_tous_les_handlers_acceptent_un_dictionnaire_vide(application_minimale):
    """Les agents omettent parfois les champs facultatifs au premier appel."""
    for name, handler in application_minimale._agent_tools().items():
        try:
            handler({})
        except Exception as exc:  # pragma: no cover - précise le handler fautif
            pytest.fail(f"Le handler {name!r} refuse un dictionnaire vide : {exc}")


def test_les_outils_dangereux_ne_sont_pas_exposes():
    """Un accès shell ou un arrêt machine exige une décision explicite du propriétaire."""
    forbidden = {"shell_exec", "shutdown_jarvis", "computer_control", "dev_agent"}
    assert forbidden.isdisjoint(anogpt_mcp._TOOL_NAMES)
