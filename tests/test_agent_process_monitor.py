"""La veille distingue fin de tour, processus ouvert et sortie réelle."""

import json
import threading

from actions import agent_process_monitor as monitor


def _write(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_codex_turn_complete_then_new_request(tmp_path):
    path = tmp_path / "codex.jsonl"
    rows = [
        {"timestamp": "2026-09-25T08:00:00Z", "type": "response_item",
         "payload": {"role": "user"}},
        {"timestamp": "2026-09-25T08:01:00Z", "type": "event_msg",
         "payload": {"type": "task_complete"}},
    ]
    _write(path, rows)
    cursor = {}
    assert monitor._turn_state(path, "codex", cursor)[0] == "finished"
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"timestamp": "2026-09-25T08:02:00Z",
                                 "type": "response_item", "payload": {"role": "user"}}) + "\n")
    assert monitor._turn_state(path, "codex", cursor)[0] == "working"
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"timestamp": "2026-09-25T08:03:00Z",
                                 "type": "event_msg", "payload": {"type": "task_complete"}}) + "\n")
    assert monitor._turn_state(path, "codex", cursor)[0] == "finished"


def test_claude_tool_result_is_not_new_user_request(tmp_path):
    path = tmp_path / "claude.jsonl"
    _write(path, [
        {"timestamp": "2026-09-25T08:00:00Z", "type": "user"},
        {"timestamp": "2026-09-25T08:00:30Z", "type": "user", "toolUseResult": {}},
        {"timestamp": "2026-09-25T08:01:00Z", "type": "system", "subtype": "turn_duration"},
    ])
    assert monitor._turn_state(path, "claude")[0] == "finished"


def test_process_running_without_log_does_not_claim_finished(monkeypatch):
    agent = {"name": "agy", "pid": 123, "started": 1.0, "tty": "/dev/pts/1"}
    monkeypatch.setattr(monitor, "_alive", lambda _agent: True)
    assert monitor._line(agent, None)[0] == "unknown"
    assert "impossible" in monitor._line(agent, None)[1]


def test_watch_notifies_a_recorded_end_once(monkeypatch):
    agent = {"name": "codex", "pid": 123, "started": 1.0, "tty": "/dev/pts/1"}
    notices = []
    monkeypatch.setattr(monitor, "_line", lambda *_args: ("finished", "tour terminé"))
    key = ("codex", 123, 1.0)
    monitor._WATCHES[key] = threading.Event()
    monitor._watch(agent, None, threading.Event(), None, notices.append)
    assert notices == ["tour terminé"]
    assert key not in monitor._WATCHES


def test_monitor_is_available_in_voice_core():
    from core.tool_dispatcher import TOOL_DECLARATIONS
    from core.tool_packs import CORE

    assert "agent_process_monitor" in CORE
    declaration = next(item for item in TOOL_DECLARATIONS
                       if item["name"] == "agent_process_monitor")
    assert "watch" in declaration["parameters"]["properties"]["action"]["description"]
