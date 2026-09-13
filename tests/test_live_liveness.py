import asyncio
import time
from types import SimpleNamespace
from unittest.mock import Mock

from core import session_manager as sm


def _host(**kw):
    host = SimpleNamespace(
        _awaiting_server_since=0.0, _active_tool_tasks=set(), _is_speaking=False,
        _live_user_text="pharmacie la plus proche", _unanswered=[],
        _toolkit_reconnect_requested=False, _voice_reconnect_requested=False,
        _live_unresponsive_reconnect=False, _voice_change_event=asyncio.Event(),
        ui=SimpleNamespace(write_log=Mock()), reset_audio_and_turn_state=Mock(),
    )
    for k, v in kw.items():
        setattr(host, k, v)
    host._run_live_liveness_watch = sm.SessionManager._run_live_liveness_watch.__get__(host)
    return host


def test_silent_session_triggers_quiet_reconnect(monkeypatch):
    monkeypatch.setattr(sm, "_LIVE_REPLY_TIMEOUT_S", 0.0)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(sm.asyncio, "sleep", lambda *_: real_sleep(0))
    host = _host(_awaiting_server_since=time.monotonic() - 1.0)
    asyncio.run(host._run_live_liveness_watch())
    assert host._voice_change_event.is_set()
    assert host._toolkit_reconnect_requested and host._live_unresponsive_reconnect
    assert host._unanswered == ["pharmacie la plus proche"]
    host.reset_audio_and_turn_state.assert_called_once_with("live_unresponsive")
    assert "sans réaction" in host.ui.write_log.call_args[0][0]


def test_no_reconnect_while_tool_runs_or_nothing_pending(monkeypatch):
    monkeypatch.setattr(sm, "_LIVE_REPLY_TIMEOUT_S", 0.0)
    calls = {"n": 0}

    async def _sleep(*_):
        calls["n"] += 1
        if calls["n"] > 3:
            raise asyncio.CancelledError
    monkeypatch.setattr(sm.asyncio, "sleep", _sleep)
    host = _host(_awaiting_server_since=time.monotonic() - 1.0, _active_tool_tasks={object()})
    try:
        asyncio.run(host._run_live_liveness_watch())
    except asyncio.CancelledError:
        pass
    assert not host._voice_change_event.is_set()
