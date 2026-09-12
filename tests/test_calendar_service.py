from __future__ import annotations

from datetime import datetime, timezone

from core.calendar_service import (
    CalDAVProvider, build_ics_event, parse_ics_events, _as_datetime,
    events_overlap, validate_event_window,
)


class Response:
    def __init__(self, status_code=207, content=b"", headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def test_ics_round_trip_preserves_event_fields():
    raw = build_ics_event({
        "id": "meeting@ano", "title": "Point projet", "description": "Ligne 1\nLigne 2",
        "location": "Bureau", "start": "2026-08-16T14:00:00+00:00",
        "end": "2026-08-16T15:00:00+00:00", "attendees": ["alice@example.com"],
    })
    event = parse_ics_events(raw)[0]
    assert event["id"] == "meeting@ano"
    assert event["title"] == "Point projet"
    assert event["description"] == "Ligne 1\nLigne 2"
    assert event["attendees"] == ["alice@example.com"]


def test_caldav_lists_multistatus_events():
    xml = b'''<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
    <d:response><d:href>/cal/a.ics</d:href><d:propstat><d:prop>
    <d:getetag>"v1"</d:getetag><c:calendar-data>BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:a1\nDTSTART:20260816T140000Z\nDTEND:20260816T150000Z\nSUMMARY:Point projet\nEND:VEVENT\nEND:VCALENDAR</c:calendar-data>
    </d:prop></d:propstat></d:response></d:multistatus>'''
    session = Session([Response(content=xml)])
    provider = CalDAVProvider("https://dav.example/cal", "user", "secret", session)

    rows = provider.list_events(datetime(2026, 8, 16, tzinfo=timezone.utc),
                                datetime(2026, 8, 17, tzinfo=timezone.utc))

    assert rows[0]["title"] == "Point projet"
    assert rows[0]["href"] == "https://dav.example/cal/a.ics"
    assert session.calls[0][0] == "REPORT"
    assert session.calls[0][2]["auth"] == ("user", "secret")


def test_caldav_creates_with_put():
    session = Session([Response(status_code=201, headers={"ETag": '"new"'})])
    provider = CalDAVProvider("https://dav.example/cal", session=session)
    event = provider.create_event("Déjeuner", "2026-08-16T12:00:00+00:00",
                                  "2026-08-16T13:00:00+00:00")

    assert event["title"] == "Déjeuner"
    method, url, kwargs = session.calls[0]
    assert method == "PUT" and url.endswith(".ics")
    assert b"SUMMARY:D\xc3\xa9jeuner" in kwargs["data"]


def test_calendar_window_rejects_invalid_duration_and_handles_all_day_end():
    start, end = validate_event_window("2026-08-16T14:00:00+00:00", "2026-08-16T15:00:00+00:00")
    assert end > start
    assert _as_datetime("2026-08-16", end=True).hour == 23
    assert events_overlap(start, end, "2026-08-16T14:30:00+00:00", "2026-08-16T16:00:00+00:00")
    assert not events_overlap(start, end, "2026-08-16T15:00:00+00:00", "2026-08-16T16:00:00+00:00")


def test_calendar_window_rejects_end_before_start():
    import pytest
    from core.calendar_service import CalendarError
    with pytest.raises(CalendarError, match="fin"):
        validate_event_window("2026-08-16T15:00:00+00:00", "2026-08-16T14:00:00+00:00")
