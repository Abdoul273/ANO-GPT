from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

from actions import reminder as reminder_mod


def test_notify_script_publishes_a_persistent_due_event(tmp_path, monkeypatch):
    monkeypatch.setattr(reminder_mod, "_scripts_dir", lambda: tmp_path)
    due = datetime(2030, 5, 4, 12, 30)

    script = reminder_mod._write_notify_script(
        "JARVISReminder_test", "Prendre le médicament", "linux", due
    )
    source = script.read_text(encoding="utf-8")

    compile(source, str(script), "exec")
    assert '"task_name": "JARVISReminder_test"' in source
    assert '"message": "Prendre le m\u00e9dicament"' in source
    assert '"due": "2030-05-04 12:30:00"' in source
    assert '"--urgency=critical"' in source
    assert 'replace(_dst)' in source


def test_triggered_events_are_consumed_once_and_sorted(tmp_path, monkeypatch):
    monkeypatch.setattr(reminder_mod, "_events_dir", lambda: tmp_path)
    now = datetime.now()
    later = {
        "task_name": "later",
        "message": "Deuxième",
        "due": "2030-01-01 10:01:00",
        "triggered": now.isoformat(timespec="seconds"),
    }
    first = {
        "task_name": "first",
        "message": "Premier",
        "due": "2030-01-01 10:00:00",
        "triggered": now.isoformat(timespec="seconds"),
    }
    (tmp_path / "later.json").write_text(json.dumps(later), encoding="utf-8")
    (tmp_path / "first.json").write_text(json.dumps(first), encoding="utf-8")

    events = reminder_mod.pop_triggered_reminders()

    assert [event["task_name"] for event in events] == ["first", "later"]
    assert list(tmp_path.iterdir()) == []
    assert reminder_mod.pop_triggered_reminders() == []


def test_stale_and_malformed_events_are_discarded(tmp_path, monkeypatch):
    monkeypatch.setattr(reminder_mod, "_events_dir", lambda: tmp_path)
    stale = {
        "task_name": "old",
        "message": "Trop ancien",
        "triggered": (datetime.now() - timedelta(days=2)).isoformat(timespec="seconds"),
    }
    (tmp_path / "old.json").write_text(json.dumps(stale), encoding="utf-8")
    (tmp_path / "bad.json").write_text("not-json", encoding="utf-8")

    assert reminder_mod.pop_triggered_reminders(max_age_hours=1) == []
    assert list(tmp_path.iterdir()) == []


def test_linux_timer_is_precise_and_persistent(tmp_path, monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(reminder_mod.shutil, "which", lambda name: "/usr/bin/systemd-run")
    monkeypatch.setattr(reminder_mod, "_python_exe", lambda: "/usr/bin/python3")

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(reminder_mod.subprocess, "run", fake_run)
    result = reminder_mod._schedule_linux(
        datetime(2030, 5, 4, 12, 30), "JARVISReminder_test", tmp_path / "task.py"
    )

    assert result == {"scheduler": "systemd", "job_id": "JARVISReminder_test"}
    assert "--timer-property=AccuracySec=1s" in calls[0]
    assert "--timer-property=Persistent=true" in calls[0]


def test_natural_language_supports_named_and_fractional_times():
    noon = reminder_mod._parse_reminder_text("rappelle-moi demain à midi de déjeuner")
    half_hour = reminder_mod._parse_reminder_text("dans une demi-heure faire une pause")

    assert noon is not None and noon["time"] == "12:00"
    assert "déjeuner" in noon["message"].lower()
    assert half_hour is not None
    expected = datetime.now() + timedelta(minutes=30)
    assert half_hour["date"] == expected.strftime("%Y-%m-%d")
    assert half_hour["time"] == expected.strftime("%H:%M")
    assert "pause" in half_hour["message"].lower()


def test_due_reminder_is_spoken_then_its_card_is_dismissed(monkeypatch):
    from actions import media_control
    from main import JarvisLive

    shown: list[tuple[str, str, str]] = []
    dismissed: list[tuple[str, str]] = []
    volumes: list[int] = []
    refreshed: list[bool] = []

    class FakeUI:
        def show_card(self, card_type, title, body, actions=None):
            shown.append((card_type, title, body))

        def dismiss_cards(self, card_type="", title=""):
            dismissed.append((card_type, title))

        def write_log(self, text):
            pass

    class FakeSession:
        def __init__(self, owner):
            self.owner = owner
            self.requests = []

        async def send_realtime_input(self, **payload):
            self.requests.append(payload)
            self.owner._turn_done_event.set()

    monkeypatch.setattr(media_control, "get_current_volume", lambda: 35)
    monkeypatch.setattr(media_control, "system_volume", lambda value: volumes.append(value))

    async def scenario():
        jarvis = object.__new__(JarvisLive)
        jarvis.ui = FakeUI()
        jarvis._dashboard = None
        jarvis._model_turn_active = False
        jarvis._is_speaking = False
        jarvis._turn_done_event = asyncio.Event()
        jarvis.audio_in_queue = asyncio.Queue()
        jarvis.session = FakeSession(jarvis)
        jarvis._show_active_reminders_card = lambda: refreshed.append(True)

        await jarvis._announce_due_reminder({"message": "Appeler maman"})
        return jarvis.session.requests

    requests = asyncio.run(scenario())

    assert shown == [("info", "⏰ RAPPEL", "**Appeler maman**\n\nAnnonce vocale en cours…")]
    assert ("info", "⏰ RAPPEL") in dismissed
    assert ("result", "Rappels actifs") in dismissed
    assert volumes == [70, 35]
    assert refreshed == [True]
    assert "Appeler maman" in requests[0]["text"]
