from pathlib import Path
import json

from core.plugin_registry import PluginRegistry


def test_plugin_discovery_toggle_and_failure_isolation(tmp_path: Path):
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / "hello.py").write_text(
        "PLUGIN={'name':'hello_test','description':'test','parameters':{'type':'OBJECT','properties':{}}}\n"
        "def run(parameters, **kwargs): return 'bonjour'\n",
        encoding="utf-8",
    )
    (plugins / "broken.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")
    registry = PluginRegistry(plugins, tmp_path / "states.json", {"core_tool"})
    registry.discover()
    # Découvert mais jamais approuvé : ni exécutable, ni déclaré au modèle.
    assert registry.run("hello_test", {}) == "Plugin indisponible : hello_test."
    assert registry.declarations() == []
    assert "broken.py" in registry.errors
    assert registry.set_enabled("hello_test", True)
    assert registry.run("hello_test", {}) == "bonjour"
    assert registry.set_enabled("hello_test", False)
    assert registry.declarations() == []


def test_plugin_code_never_runs_before_explicit_approval(tmp_path: Path):
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    marker = tmp_path / "executed.txt"
    (plugins / "sneaky.py").write_text(
        f"open(r'{marker}', 'w').write('ran')\n"
        "PLUGIN={'name':'sneaky','description':'test','parameters':{'type':'OBJECT','properties':{}}}\n"
        "def run(parameters, **kwargs): return 'ok'\n",
        encoding="utf-8",
    )
    registry = PluginRegistry(plugins, tmp_path / "states.json", set())
    registry.discover()
    assert not marker.exists()  # aucun import au simple fait de découvrir le fichier
    assert registry.run("sneaky", {}) == "Plugin indisponible : sneaky."
    assert not marker.exists()  # toujours pas exécuté : jamais approuvé
    assert registry.set_enabled("sneaky", True)
    assert registry.run("sneaky", {}) == "ok"
    assert marker.exists()


def test_plugin_source_change_revokes_previous_approval(tmp_path: Path):
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    plugin_file = plugins / "hello.py"
    plugin_file.write_text(
        "PLUGIN={'name':'hello_test','description':'test','parameters':{'type':'OBJECT','properties':{}}}\n"
        "def run(parameters, **kwargs): return 'v1'\n",
        encoding="utf-8",
    )
    registry = PluginRegistry(plugins, tmp_path / "states.json", set())
    registry.discover()
    assert registry.set_enabled("hello_test", True)
    assert registry.run("hello_test", {}) == "v1"

    plugin_file.write_text(
        "PLUGIN={'name':'hello_test','description':'test','parameters':{'type':'OBJECT','properties':{}}}\n"
        "def run(parameters, **kwargs): return 'v2 — code modifié'\n",
        encoding="utf-8",
    )
    registry.discover()
    assert not registry.enabled("hello_test")
    assert registry.run("hello_test", {}) == "Plugin indisponible : hello_test."
    assert registry.set_enabled("hello_test", True)
    assert registry.run("hello_test", {}) == "v2 — code modifié"


def test_versioned_package_manifest_async_and_metadata(tmp_path: Path):
    plugins = tmp_path / "plugins"
    package = plugins / "modern_echo"
    package.mkdir(parents=True)
    (package / "plugin.json").write_text(json.dumps({
        "api_version": "1", "name": "modern_echo", "version": "1.2.3",
        "description": "Répète un message de manière testable.",
        "entrypoint": "main.py:run", "permissions": ["network"],
        "parameters": {"type": "OBJECT", "properties": {}},
    }), encoding="utf-8")
    (package / "main.py").write_text(
        "async def run(parameters, session_memory=None):\n"
        "    session_memory['called'] = True\n"
        "    return 'ok async'\n", encoding="utf-8")
    registry = PluginRegistry(plugins, tmp_path / "states.json", set())
    registry.discover()
    assert registry.set_enabled("modern_echo", True)
    memory = {}
    assert registry.run("modern_echo", {}, session_memory=memory) == "ok async"
    assert memory["called"] is True
    status = registry.status()[0]
    assert status["version"] == "1.2.3"
    assert status["permissions"] == ["network"]
    assert status["format"] == "package"
    assert status["checksum"]


def test_package_rejects_unknown_permission(tmp_path: Path):
    package = tmp_path / "plugins" / "bad_plugin"
    package.mkdir(parents=True)
    (package / "plugin.json").write_text(json.dumps({
        "api_version": "1", "name": "bad_plugin", "version": "1.0.0",
        "description": "Plugin invalide de test.", "entrypoint": "main.py:run",
        "permissions": ["root_access"], "parameters": {"type": "OBJECT"},
    }), encoding="utf-8")
    (package / "main.py").write_text("def run(parameters): return 'no'\n", encoding="utf-8")
    registry = PluginRegistry(tmp_path / "plugins", tmp_path / "states.json", set())
    registry.discover()
    assert not registry.plugins
    assert "bad_plugin/plugin.json" in registry.errors
