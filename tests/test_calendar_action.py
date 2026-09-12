from __future__ import annotations

from actions.calendar import calendar_control


class CalendarDouble:
    def __init__(self, conflicts=None):
        self.conflicts = conflicts or []
        self.created = []

    def find_conflicts(self, *_args, **_kwargs):
        return list(self.conflicts)

    def create_event(self, title, start, end, **kwargs):
        self.created.append((title, start, end, kwargs))
        return {"id": "created-1", "title": title, "start": start.isoformat()}


def test_create_calendar_event_previews_conflict_without_writing(monkeypatch):
    service = CalendarDouble([{"title": "Réunion", "start": "2026-08-16T14:00:00+00:00"}])
    monkeypatch.setattr("actions.calendar.get_calendar_service", lambda: service)
    result = calendar_control({"action": "create", "title": "Déjeuner", "start": "2026-08-16T14:30:00+00:00", "end": "2026-08-16T15:00:00+00:00"})
    assert "Conflit d'agenda" in result
    assert not service.created


def test_create_calendar_event_allows_explicitly_accepted_conflict(monkeypatch):
    service = CalendarDouble([{"title": "Réunion", "start": "2026-08-16T14:00:00+00:00"}])
    monkeypatch.setattr("actions.calendar.get_calendar_service", lambda: service)
    result = calendar_control({"action": "create", "title": "Déjeuner", "start": "2026-08-16T14:30:00+00:00", "end": "2026-08-16T15:00:00+00:00", "allow_conflict": True})
    assert "Événement créé" in result
    assert len(service.created) == 1


def test_dry_run_never_creates_calendar_event(monkeypatch):
    service = CalendarDouble()
    monkeypatch.setattr("actions.calendar.get_calendar_service", lambda: service)
    result = calendar_control({"action": "create", "title": "Déjeuner", "start": "2026-08-16T14:30:00+00:00", "end": "2026-08-16T15:00:00+00:00", "dry_run": True})
    assert "Prévisualisation" in result
    assert not service.created
