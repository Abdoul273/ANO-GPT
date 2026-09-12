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
    assert registry.run("hello_test", {}) == "bonjour"
    assert "broken.py" in registry.errors
    assert registry.set_enabled("hello_test", False)
    assert registry.declarations() == []


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
