"""Tests unitaires pour le watcher d'anticipation de réunions et le moteur proactif."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from actions import proactive
from core import calendar_watcher
from core.calendar_watcher import (
    CalendarWatcher,
    format_meeting_announcement,
    parse_event_start,
)


class DummyCalendarService:
    def __init__(self, events: list[dict[str, Any]], authenticated: bool = True):
        self._events = events
        self._authenticated = authenticated
        self.list_calls: list[tuple[Any, Any]] = []

    def status(self) -> SimpleNamespace:
        return SimpleNamespace(authenticated=self._authenticated, message="OK")

    def list_events(self, start: Any, end: Any, **_: Any) -> list[dict[str, Any]]:
        self.list_calls.append((start, end))
        return list(self._events)


def test_parse_event_start_identifies_timed_vs_allday():
    # Événement horodaté ISO
    ev1 = {"start": "2026-09-06T14:30:00+02:00"}
    dt1 = parse_event_start(ev1)
    assert dt1 is not None
    assert dt1.hour == 14 and dt1.minute == 30

    # Événement UTC avec 'Z'
    ev2 = {"start": "2026-09-06T12:00:00Z"}
    dt2 = parse_event_start(ev2)
    assert dt2 is not None

    # Événement journée entière (sans heure 'T') -> ignoré
    ev_allday = {"start": "2026-09-06"}
    assert parse_event_start(ev_allday) is None

    # Champ vide ou invalide -> ignoré
    assert parse_event_start({}) is None
    assert parse_event_start({"start": "pas-une-date"}) is None


def test_format_meeting_announcement_single_meeting():
    now = datetime(2026, 9, 6, 14, 0, tzinfo=timezone.utc)

    # Réunion dans 10 minutes
    ev = {"title": "Point projet", "start": "2026-09-06T14:10:00Z"}
    msg = format_meeting_announcement([ev], now=now)
    assert msg == "Tu as Point projet dans 10 minutes."

    # Réunion dans 5 minutes (ex: après différé ou délai court)
    ev_5m = {"title": "Sync client", "start": "2026-09-06T14:05:00Z"}
    msg_5m = format_meeting_announcement([ev_5m], now=now)
    assert msg_5m == "Tu as Sync client dans 5 minutes."


def test_format_meeting_announcement_multiple_meetings_grouped():
    now = datetime(2026, 9, 6, 14, 0, tzinfo=timezone.utc)

    ev1 = {"title": "Point projet", "start": "2026-09-06T14:09:00Z"}
    ev2 = {"title": "Revue sprint", "start": "2026-09-06T14:10:00Z"}

    # 2 réunions groupées
    msg = format_meeting_announcement([ev1, ev2], now=now)
    assert "Tu as 2 réunions dans 10 minutes : Point projet et Revue sprint." == msg

    # 3 réunions groupées
    ev3 = {"title": "Design review", "start": "2026-09-06T14:10:00Z"}
    msg3 = format_meeting_announcement([ev1, ev2, ev3], now=now)
    assert "Tu as 3 réunions dans 10 minutes : Point projet, Revue sprint et Design review." == msg3


def test_watcher_detects_meeting_in_window_and_ignores_others(tmp_path):
    now = datetime(2026, 9, 6, 14, 0, tzinfo=timezone.utc)
    state_file = tmp_path / "calendar_anticipation.json"
    watcher = CalendarWatcher(state_file)

    events = [
        # 1. Réunion dans 9 minutes -> DOIT être détectée
        {"id": "meet-1", "title": "Entretien", "start": "2026-09-06T14:09:00Z"},
        # 2. Réunion dans 25 minutes -> Trop tôt, ignorée
        {"id": "meet-2", "title": "Futur", "start": "2026-09-06T14:25:00Z"},
        # 3. Réunion passée il y a 5 minutes -> Ignorée
        {"id": "meet-3", "title": "Passé", "start": "2026-09-06T13:55:00Z"},
        # 4. Événement journée entière -> Ignoré
        {"id": "meet-4", "title": "Férié", "start": "2026-09-06"},
    ]

    service = DummyCalendarService(events)
    result = watcher.check_and_produce_announcement(service, now=now)

    assert result is not None
    message, upcoming = result
    assert len(upcoming) == 1
    assert upcoming[0]["id"] == "meet-1"
    assert message == "Tu as Entretien dans 10 minutes."


def test_watcher_never_announces_same_event_twice(tmp_path):
    now = datetime(2026, 9, 6, 14, 0, tzinfo=timezone.utc)
    state_file = tmp_path / "calendar_anticipation.json"
    watcher = CalendarWatcher(state_file)

    events = [
        {"id": "meet-unique", "title": "Point hebdo", "start": "2026-09-06T14:08:00Z"}
    ]
    service = DummyCalendarService(events)

    # 1ère passe : annonce générée
    result1 = watcher.check_and_produce_announcement(service, now=now)
    assert result1 is not None

    # 2ème passe (5 minutes plus tard, réunion toujours dans la fenêtre) : AUCUN doublon
    now_plus_5m = now + timedelta(minutes=5)
    result2 = watcher.check_and_produce_announcement(service, now=now_plus_5m)
    assert result2 is None, "L'événement ne doit jamais être annoncé une deuxième fois"


def test_watcher_state_survives_restart(tmp_path):
    now = datetime(2026, 9, 6, 14, 0, tzinfo=timezone.utc)
    state_file = tmp_path / "calendar_anticipation.json"

    # Instance 1
    watcher1 = CalendarWatcher(state_file)
    events = [
        {"id": "meet-persist", "title": "Conseil", "start": "2026-09-06T14:07:00Z"}
    ]
    service = DummyCalendarService(events)
    assert watcher1.check_and_produce_announcement(service, now=now) is not None

    # Instance 2 (simule le redémarrage de l'application)
    watcher2 = CalendarWatcher(state_file)
    assert watcher2.is_announced("meet-persist")
    assert watcher2.check_and_produce_announcement(service, now=now) is None


def test_watcher_groups_multiple_near_meetings(tmp_path):
    now = datetime(2026, 9, 6, 14, 0, tzinfo=timezone.utc)
    state_file = tmp_path / "calendar_anticipation.json"
    watcher = CalendarWatcher(state_file)

    events = [
        {"id": "m1", "title": "Standup", "start": "2026-09-06T14:05:00Z"},
        {"id": "m2", "title": "Architecture", "start": "2026-09-06T14:10:00Z"},
    ]
    service = DummyCalendarService(events)
    result = watcher.check_and_produce_announcement(service, now=now)

    assert result is not None
    message, upcoming = result
    assert len(upcoming) == 2
    assert "Tu as 2 réunions" in message
    assert "Standup" in message
    assert "Architecture" in message

    # Les deux sont marqués comme annoncés
    assert watcher.is_announced("m1")
    assert watcher.is_announced("m2")


def test_in_quiet_hours_respects_time_window():
    # Par défaut : 23h à 8h
    # 23h30 -> nuit / calme
    assert proactive.in_quiet_hours(datetime(2026, 9, 6, 23, 30)) is True
    # 02h00 -> nuit / calme
    assert proactive.in_quiet_hours(datetime(2026, 9, 6, 2, 0)) is True
    # 07h59 -> nuit / calme
    assert proactive.in_quiet_hours(datetime(2026, 9, 6, 7, 59)) is True
    # 08h00 -> fin de la plage calme
    assert proactive.in_quiet_hours(datetime(2026, 9, 6, 8, 0)) is False
    # 14h00 -> journée active
    assert proactive.in_quiet_hours(datetime(2026, 9, 6, 14, 0)) is False
    # 22h59 -> pas encore l'heure calme
    assert proactive.in_quiet_hours(datetime(2026, 9, 6, 22, 59)) is False


def test_proactive_engine_proactive_mode_quiet_hours_and_expiration(tmp_path, monkeypatch):
    """Vérifie que _run_proactive_mode respecte les heures calmes et expire les réunions passées."""
    from core.proactive_engine import ProactiveEngine
    monkeypatch.setattr("core.proactive_engine.desktop_blocks_proactivity", lambda: "")
    monkeypatch.setattr("core.proactive_engine.in_quiet_hours", lambda: False)

    service = proactive.ProactiveService(tmp_path / "proactive_state.json")

    class FakeUI:
        def __init__(self):
            self.logs = []
            self.cards = []
            self.muted = False

        def write_log(self, msg: str):
            self.logs.append(msg)

        def show_card(self, *args):
            self.cards.append(args)

    class Host:
        def __init__(self):
            self.ui = FakeUI()
            self.session = object()
            self._proactive = service
            self._speaking_lock = proactive.threading.Lock()
            self._is_speaking = False
            self._model_turn_active = False
            self._activity_open = False
            self._phone_active = False
            self.submitted = []

        async def _submit_text_turn(self, text: str, timeout_s: float = 90.0) -> bool:
            self.submitted.append(text)
            return True

    host = Host()
    loop = asyncio.new_event_loop()

    async def scenario():
        service.bind(loop)

        # 1. Événement calendrier avec heure de début passée -> doit être écarté (discard)
        past_ts = (datetime.now().astimezone() - timedelta(minutes=5)).timestamp()
        service.publish(
            "calendar",
            "Tu as Réunion expirée dans 10 minutes.",
            dedupe_key="cal:expired",
            priority=85,
            data={"starts": [past_ts]},
        )

        task = asyncio.create_task(ProactiveEngine._run_proactive_mode(host))
        # Laisser le consommateur traiter l'événement
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Vérifier que l'événement expiré a été ignoré et non prononcé
        assert len(host.submitted) == 0
        assert any("déjà commencée" in log for log in host.ui.logs)

    loop.run_until_complete(scenario())
    loop.close()
