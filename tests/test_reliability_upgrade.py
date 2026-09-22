import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from core.action_runtime import ActionCircuitOpen, ActionPolicy, ActionQueueFull, ActionRuntime, ActionValidationError
from memory.config_manager import AssistantConfig, AtomicJSONFile, ConfigManager


def runtime_with(schema):
    return ActionRuntime([{"name": "test", "parameters": {
        "properties": {"value": schema}, "required": ["value"]}}])


@pytest.mark.parametrize("schema,value", [
    ({"type": "INTEGER"}, float("inf")),
    ({"type": "STRING", "maxLength": 3}, "long"),
    ({"type": "ARRAY", "maxItems": 1}, [1, 2]),
    ({"type": "ARRAY", "items": {"type": "STRING", "enum": ["yes"]}}, ["no"]),
    ({"type": "OBJECT", "properties": {"name": {"type": "STRING"}}, "required": ["name"]}, {}),
    ({"type": "OBJECT", "properties": {}, "additionalProperties": False}, {"unexpected": 1}),
])
def test_rejects_invalid_nested_arguments(schema, value):
    with pytest.raises(ActionValidationError):
        runtime_with(schema).prepare("test", {"value": value})


def test_nested_coercion():
    runtime = runtime_with({"type": "OBJECT", "properties": {
        "count": {"type": "INTEGER", "minimum": 1}}})
    assert runtime.prepare("test", {"value": {"count": "2"}}) == {"value": {"count": 2}}


def test_waiting_call_checks_circuit_again():
    runtime = runtime_with({"type": "STRING"})

    async def scenario():
        async with runtime.lease("test"):
            async def waiting():
                async with runtime.lease("test"):
                    pytest.fail("queued tool ran after circuit opened")
            task = asyncio.create_task(waiting())
            await asyncio.sleep(0)
            for _ in range(3):
                runtime.note_failure("test")
        with pytest.raises(ActionCircuitOpen):
            await task
        assert runtime._pending["test"] == 0
    asyncio.run(scenario())


def test_queue_timeout_and_cancellation_release_capacity(monkeypatch):
    import core.action_runtime as module
    monkeypatch.setitem(module._POLICIES, "test", ActionPolicy(queue_timeout_s=.01))
    runtime = runtime_with({"type": "STRING"})

    async def scenario():
        async with runtime.lease("test"):
            with pytest.raises(ActionQueueFull):
                async with runtime.lease("test"):
                    pytest.fail("semaphore limit bypassed")
        async with runtime.lease("test"):
            pass
        assert runtime._pending["test"] == 0
    asyncio.run(scenario())


def test_atomic_updates_preserve_concurrent_keys(tmp_path):
    path = tmp_path / "config.json"
    def update(index):
        AtomicJSONFile(path).update({str(index): index})
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(update, range(100)))
    assert len(AtomicJSONFile(path).read()) == 100


def test_corrupt_config_is_not_overwritten(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{invalid", encoding="utf-8")
    with pytest.raises(ValueError):
        AtomicJSONFile(path).update({"live_voice": "Charon"})
    assert path.read_text() == "{invalid"


def test_config_cache_does_not_share_mutable_values(tmp_path):
    manager = ConfigManager(tmp_path / "config.json", use_env=False)
    config = AssistantConfig()
    manager.save(config)
    config.brain_provider_priority.clear()
    loaded = manager.load()
    loaded.brain_provider_priority.clear()
    assert manager.load().brain_provider_priority


def test_failed_keyring_does_not_erase_saved_key(tmp_path, monkeypatch):
    import sys
    import types
    path = tmp_path / "config.json"
    AtomicJSONFile(path).write({"gemini_api_key": "old-key-long-enough"})
    def fail(*args):
        raise RuntimeError("locked")
    monkeypatch.setitem(sys.modules, "keyring", types.SimpleNamespace(set_password=fail))
    with pytest.raises(RuntimeError, match="not saved"):
        ConfigManager(path, use_env=False, use_keyring=True).save(
            AssistantConfig(gemini_api_key="new-key-long-enough"))
    assert AtomicJSONFile(path).read()["gemini_api_key"] == "old-key-long-enough"


def test_stats_ignore_corrupt_records_and_measure_tail_latency(tmp_path):
    from core.tool_stats import summary
    path = tmp_path / "usage.jsonl"
    entries = [[], None, {"tool": "test", "ms": "invalid"}, {"tool": "test", "ms": -1}]
    entries += [{"tool": "test", "ms": n, "ok": n != 20} for n in range(1, 21)]
    path.write_text("broken\n" + "\n".join(json.dumps(e) for e in entries))
    row, = summary(path)
    assert row["calls"] == 20
    assert row["p50_ms"] == 10
    assert row["p95_ms"] == 19
    assert row["errors"] == 1


def test_stats_keep_exception_class_without_message(tmp_path, monkeypatch):
    from core import tool_stats
    path = tmp_path / "usage.jsonl"
    monkeypatch.setattr(tool_stats, "LOG_PATH", path)
    tool_stats.record(
        "live_auto_debug",
        ok=False,
        duration_ms=1900,
        error="Échec contrôlé de live_auto_debug : ValueError: token=secret",
    )
    row = json.loads(path.read_text(encoding="utf-8"))
    assert row["error"] == "ValueError"
    assert "secret" not in path.read_text()


def test_stats_rotation_and_error_privacy(tmp_path, monkeypatch):
    from core import tool_stats
    path = tmp_path / "usage.jsonl"
    monkeypatch.setattr(tool_stats, "LOG_PATH", path)
    monkeypatch.setattr(tool_stats, "MAX_LOG_BYTES", 1)
    tool_stats.record("test", ok=False, duration_ms=1, error="api_key=very-secret")
    tool_stats.record("test", ok=True, duration_ms=2)
    backup = path.with_suffix(".jsonl.1")
    assert backup.exists()
    assert "very-secret" not in backup.read_text()
    assert "tool_execution_failed" in backup.read_text()
    monkeypatch.setattr(tool_stats, "MAX_LOG_BYTES", 5 * 1024 * 1024)
    assert len(tool_stats.load(path)) == 1


def test_diagnostics_do_not_expose_configuration(tmp_path):
    from core.diagnostics import collect
    path = tmp_path / "config" / "api_keys.json"
    AtomicJSONFile(path).write({"gemini_api_key": "a-private-key"})
    report = collect(tmp_path)
    assert "a-private-key" not in json.dumps(report)
    assert any(c["name"] == "configuration" and c["status"] == "ok" for c in report["checks"])


def test_dispatcher_respects_explicit_failure(monkeypatch):
    import main
    from types import SimpleNamespace
    jarvis = main.JarvisLive.__new__(main.JarvisLive)
    jarvis._action_runtime = runtime_with({"type": "STRING"})
    jarvis._tool_session_memory = {}
    jarvis.ui = SimpleNamespace(muted=False, write_log=lambda *_a: None, set_state=lambda *_a: None)
    jarvis._interrupted = False
    jarvis._noise_turn = False
    jarvis._event_bus = None
    recorded = []
    monkeypatch.setattr(main.tool_stats, "record", lambda *a, **kw: recorded.append(kw))
    async def verify(_name, _args):
        return ""
    async def execute(fc, args):
        return SimpleNamespace(response={"ok": False, "result": "Confirmation requise"})
    jarvis._verify_sensitive_voice_command = verify
    jarvis._execute_tool_impl = execute
    response = asyncio.run(jarvis._execute_tool(SimpleNamespace(name="test", id="1", args={"value": "yes"})))
    assert response.response["ok"] is False
    assert recorded[0]["ok"] is False
    assert jarvis._tool_session_memory["_last_action"]["ok"] is False


def test_cancelled_waiter_does_not_leak_capacity():
    runtime = runtime_with({"type": "STRING"})
    async def scenario():
        async with runtime.lease("test"):
            async def wait():
                async with runtime.lease("test"):
                    pytest.fail("cancelled task executed")
            task = asyncio.create_task(wait())
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert runtime._pending["test"] == 0
        async with runtime.lease("test"):
            pass
    asyncio.run(scenario())
