"""Agenda unifié Google Calendar / CalDAV, sans dépendance CalDAV obligatoire."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

import requests

_BASE_DIR = Path(__file__).resolve().parent.parent
_CONFIG_FILE = _BASE_DIR / "config" / "api_keys.json"
_CLIENT_SECRET_FILE = _BASE_DIR / "memory" / "credentials" / "gmail_client_secret.json"
_GOOGLE_TOKEN_FILE = _BASE_DIR / "memory" / "calendar_token.json"
_GOOGLE_SCOPES = ["https://www.googleapis.com/auth/calendar"]


class CalendarError(RuntimeError):
    pass


class CalendarSetupRequired(CalendarError):
    pass


@dataclass(frozen=True)
class CalendarStatus:
    provider: str
    configured: bool
    authenticated: bool
    message: str
    next_step: str = ""


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.stem}-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _config() -> dict[str, Any]:
    try:
        raw = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _as_datetime(value: Any, *, end: bool = False) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, date):
        result = datetime.combine(value, time.max if end else time.min)
    else:
        text = str(value or "").strip()
        if not text:
            result = datetime.now().astimezone()
        else:
            # Une date sans heure représente une journée entière. Le bornage de
            # fin doit donc couvrir la journée, pas minuit au début de celle-ci.
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
                try:
                    result = datetime.combine(date.fromisoformat(text), time.max if end else time.min)
                except ValueError as exc:
                    raise CalendarError(f"Date invalide : {value!r}.") from exc
                return result.astimezone()
            text = text.replace("Z", "+00:00")
            try:
                result = datetime.fromisoformat(text)
            except ValueError as exc:
                raise CalendarError(
                    f"Date invalide : {value!r}. Utilisez un format ISO, par exemple 2026-08-16T14:30."
                ) from exc
    if result.tzinfo is None:
        result = result.astimezone()
    return result


def validate_event_window(start: Any, end: Any) -> tuple[datetime, datetime]:
    """Normalise et borne un rendez-vous avant tout appel réseau."""
    begins = _as_datetime(start)
    finishes = _as_datetime(end)
    if finishes <= begins:
        raise CalendarError("La fin du rendez-vous doit être postérieure au début.")
    if finishes - begins > timedelta(days=366):
        raise CalendarError("Un rendez-vous ne peut pas durer plus de 366 jours.")
    return begins, finishes


def events_overlap(first_start: Any, first_end: Any, second_start: Any, second_end: Any) -> bool:
    """Retourne vrai pour deux créneaux strictement chevauchants."""
    a_start, a_end = validate_event_window(first_start, first_end)
    b_start, b_end = validate_event_window(second_start, second_end)
    return a_start < b_end and b_start < a_end


def _google_time(value: Any) -> dict[str, str]:
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        return {"date": value.strip()}
    return {"dateTime": _as_datetime(value).isoformat()}


class GoogleCalendarProvider:
    name = "google"

    def __init__(self, token_file: Path = _GOOGLE_TOKEN_FILE,
                 client_secret_file: Path = _CLIENT_SECRET_FILE):
        self.token_file = Path(token_file)
        self.client_secret_file = Path(client_secret_file)

    def status(self, verify: bool = False) -> CalendarStatus:
        if not self.client_secret_file.is_file():
            return CalendarStatus(
                self.name, False, False, "Google Calendar n'est pas configuré.",
                "Placez le fichier OAuth Google dans memory/credentials/gmail_client_secret.json.",
            )
        if not self.token_file.is_file():
            return CalendarStatus(
                self.name, True, False, "Google Calendar attend une autorisation OAuth.",
                "Demandez « connecte mon agenda Google ».",
            )
        try:
            service = self._service()
            if verify:
                service.calendarList().list(maxResults=1).execute()
            return CalendarStatus(self.name, True, True, "Google Calendar est connecté.")
        except Exception as exc:
            return CalendarStatus(self.name, True, False,
                                  f"Jeton Google Calendar inutilisable : {exc}",
                                  "Reconnectez l'agenda Google.")

    def connect(self, interactive: bool = True) -> CalendarStatus:
        if not self.client_secret_file.is_file():
            raise CalendarSetupRequired(self.status().next_step)
        try:
            from google_auth_oauthlib.flow import InstalledAppFlow
        except ImportError as exc:
            raise CalendarSetupRequired(
                "Installez google-auth-oauthlib et google-api-python-client."
            ) from exc
        if not interactive:
            raise CalendarSetupRequired("Une autorisation interactive est nécessaire.")
        flow = InstalledAppFlow.from_client_secrets_file(
            str(self.client_secret_file), _GOOGLE_SCOPES
        )
        from core.browser_policy import register_chrome
        credentials = flow.run_local_server(host="127.0.0.1", port=0, open_browser=True,
                                            browser=register_chrome(), timeout_seconds=180)
        _atomic_json(self.token_file, json.loads(credentials.to_json()))
        return self.status(verify=True)

    def _service(self):
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise CalendarSetupRequired(
                "Installez google-api-python-client google-auth google-auth-oauthlib."
            ) from exc
        if not self.token_file.is_file():
            raise CalendarSetupRequired("Google Calendar n'est pas encore autorisé.")
        credentials = Credentials.from_authorized_user_file(
            str(self.token_file), _GOOGLE_SCOPES
        )
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
            _atomic_json(self.token_file, json.loads(credentials.to_json()))
        if not credentials.valid:
            raise CalendarSetupRequired("Reconnectez Google Calendar.")
        return build("calendar", "v3", credentials=credentials, cache_discovery=False)

    @staticmethod
    def _event(raw: dict[str, Any]) -> dict[str, Any]:
        return {
            "provider": "google", "id": raw.get("id", ""),
            "title": raw.get("summary") or "Sans titre",
            "start": (raw.get("start") or {}).get("dateTime") or (raw.get("start") or {}).get("date", ""),
            "end": (raw.get("end") or {}).get("dateTime") or (raw.get("end") or {}).get("date", ""),
            "location": raw.get("location", ""), "description": raw.get("description", ""),
            "attendees": [a.get("email", "") for a in raw.get("attendees", []) if a.get("email")],
            "url": raw.get("htmlLink", ""),
        }

    def list_events(self, start: Any, end: Any, calendar_id: str = "primary",
                    max_results: int = 30) -> list[dict[str, Any]]:
        response = self._service().events().list(
            calendarId=calendar_id, timeMin=_as_datetime(start).astimezone(timezone.utc).isoformat(),
            timeMax=_as_datetime(end, end=True).astimezone(timezone.utc).isoformat(),
            singleEvents=True, orderBy="startTime", maxResults=max_results,
        ).execute()
        return [self._event(row) for row in response.get("items", [])]

    def create_event(self, title: str, start: Any, end: Any, *, calendar_id: str = "primary",
                     description: str = "", location: str = "",
                     attendees: list[str] | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "summary": title, "start": _google_time(start), "end": _google_time(end),
            "description": description, "location": location,
        }
        if attendees:
            body["attendees"] = [{"email": value} for value in attendees]
        raw = self._service().events().insert(
            calendarId=calendar_id, body=body, sendUpdates="all" if attendees else "none"
        ).execute()
        return self._event(raw)

    def update_event(self, event_id: str, changes: dict[str, Any],
                     calendar_id: str = "primary") -> dict[str, Any]:
        service = self._service()
        body = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
        mapping = {"title": "summary", "description": "description", "location": "location"}
        for source, target in mapping.items():
            if source in changes:
                body[target] = changes[source]
        for field in ("start", "end"):
            if field in changes and changes[field]:
                body[field] = _google_time(changes[field])
        if "attendees" in changes:
            body["attendees"] = [{"email": value} for value in changes["attendees"]]
        raw = service.events().update(calendarId=calendar_id, eventId=event_id,
                                      body=body, sendUpdates="all").execute()
        return self._event(raw)

    def delete_event(self, event_id: str, calendar_id: str = "primary") -> None:
        self._service().events().delete(calendarId=calendar_id, eventId=event_id,
                                        sendUpdates="all").execute()


def _ics_unfold(text: str) -> list[str]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    unfolded: list[str] = []
    for line in lines:
        if line.startswith((" ", "\t")) and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)
    return unfolded


def _ics_text(value: str) -> str:
    return value.replace("\\n", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def _ics_escape(value: Any) -> str:
    return str(value or "").replace("\\", "\\\\").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")


def _parse_ics_datetime(value: str, parameters: str = "") -> str:
    if "VALUE=DATE" in parameters or re.fullmatch(r"\d{8}", value):
        return datetime.strptime(value[:8], "%Y%m%d").date().isoformat()
    utc = value.endswith("Z")
    raw = value[:-1] if utc else value
    parsed = datetime.strptime(raw, "%Y%m%dT%H%M%S")
    if utc:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat().replace("+00:00", "Z")


def parse_ics_events(text: str) -> list[dict[str, Any]]:
    events, current = [], None
    for line in _ics_unfold(text):
        if line == "BEGIN:VEVENT":
            current = {"attendees": []}
            continue
        if line == "END:VEVENT" and current is not None:
            current.setdefault("title", "Sans titre")
            events.append(current)
            current = None
            continue
        if current is None or ":" not in line:
            continue
        head, value = line.split(":", 1)
        name, _, parameters = head.partition(";")
        key = name.upper()
        if key == "UID": current["id"] = _ics_text(value)
        elif key == "SUMMARY": current["title"] = _ics_text(value)
        elif key == "DESCRIPTION": current["description"] = _ics_text(value)
        elif key == "LOCATION": current["location"] = _ics_text(value)
        elif key == "DTSTART": current["start"] = _parse_ics_datetime(value, parameters)
        elif key == "DTEND": current["end"] = _parse_ics_datetime(value, parameters)
        elif key == "ATTENDEE": current["attendees"].append(re.sub(r"^mailto:", "", value, flags=re.I))
    return events


def _ics_datetime(value: Any) -> tuple[str, str]:
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        return ";VALUE=DATE", value.replace("-", "")
    dt = _as_datetime(value).astimezone(timezone.utc)
    return "", dt.strftime("%Y%m%dT%H%M%SZ")


def build_ics_event(event: dict[str, Any]) -> str:
    start_params, start = _ics_datetime(event["start"])
    end_params, end = _ics_datetime(event["end"])
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//ANO-GPT//Calendar//FR",
        "CALSCALE:GREGORIAN", "BEGIN:VEVENT", f"UID:{_ics_escape(event['id'])}",
        f"DTSTAMP:{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}",
        f"DTSTART{start_params}:{start}", f"DTEND{end_params}:{end}",
        f"SUMMARY:{_ics_escape(event.get('title') or 'Sans titre')}",
    ]
    for field, key in (("description", "DESCRIPTION"), ("location", "LOCATION")):
        if event.get(field): lines.append(f"{key}:{_ics_escape(event[field])}")
    for attendee in event.get("attendees") or []:
        lines.append(f"ATTENDEE:mailto:{_ics_escape(attendee)}")
    lines.extend(["END:VEVENT", "END:VCALENDAR", ""])
    return "\r\n".join(lines)


class CalDAVProvider:
    name = "caldav"

    def __init__(self, url: str, username: str = "", password: str = "",
                 session: requests.Session | None = None):
        self.url = url.rstrip("/") + "/" if url else ""
        self.username, self.password = username, password
        self.session = session or requests.Session()

    def status(self, verify: bool = False) -> CalendarStatus:
        if not self.url:
            return CalendarStatus(self.name, False, False, "CalDAV n'est pas configuré.",
                                  "Ajoutez caldav_url, caldav_username et caldav_password dans config/api_keys.json.")
        if not verify:
            return CalendarStatus(self.name, True, True, "CalDAV est configuré.")
        try:
            response = self._request("PROPFIND", self.url, headers={"Depth": "0"},
                                     data='<?xml version="1.0"?><propfind xmlns="DAV:"><prop><displayname/></prop></propfind>')
            if response.status_code not in {200, 207}:
                raise CalendarError(f"HTTP {response.status_code}")
            return CalendarStatus(self.name, True, True, "CalDAV est connecté.")
        except Exception as exc:
            return CalendarStatus(self.name, True, False, f"CalDAV inaccessible : {exc}")

    def _request(self, method: str, url: str, **kwargs):
        kwargs.setdefault("timeout", 15)
        kwargs.setdefault("auth", (self.username, self.password) if self.username else None)
        headers = kwargs.setdefault("headers", {})
        headers.setdefault("User-Agent", "ANO-GPT/1.0")
        return self.session.request(method, url, **kwargs)

    def list_events(self, start: Any, end: Any, max_results: int = 30, **_: Any) -> list[dict[str, Any]]:
        start_utc = _as_datetime(start).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        end_utc = _as_datetime(end, end=True).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        body = f'''<?xml version="1.0" encoding="utf-8" ?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
 <d:prop><d:getetag/><c:calendar-data/></d:prop>
 <c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">
 <c:time-range start="{start_utc}" end="{end_utc}"/>
 </c:comp-filter></c:comp-filter></c:filter>
</c:calendar-query>'''
        response = self._request("REPORT", self.url, headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"}, data=body)
        if response.status_code != 207:
            raise CalendarError(f"CalDAV a répondu HTTP {response.status_code}.")
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError as exc:
            raise CalendarError("Réponse CalDAV invalide.") from exc
        rows: list[dict[str, Any]] = []
        for item in root.findall("{DAV:}response"):
            href = item.findtext("{DAV:}href", "")
            data = item.findtext(".//{urn:ietf:params:xml:ns:caldav}calendar-data", "")
            etag = item.findtext(".//{DAV:}getetag", "")
            for event in parse_ics_events(data):
                event.update({"provider": "caldav", "href": urljoin(self.url, href), "etag": etag})
                rows.append(event)
        rows.sort(key=lambda row: row.get("start", ""))
        return rows[:max_results]

    def create_event(self, title: str, start: Any, end: Any, *, description: str = "",
                     location: str = "", attendees: list[str] | None = None, **_: Any) -> dict[str, Any]:
        event = {"id": f"{uuid.uuid4().hex}@ano-gpt", "title": title, "start": start,
                 "end": end, "description": description, "location": location,
                 "attendees": attendees or []}
        href = urljoin(self.url, f"{uuid.uuid4().hex}.ics")
        response = self._request("PUT", href, headers={"Content-Type": "text/calendar; charset=utf-8", "If-None-Match": "*"}, data=build_ics_event(event).encode())
        if response.status_code not in {200, 201, 204}:
            raise CalendarError(f"Création CalDAV refusée (HTTP {response.status_code}).")
        event.update({"provider": "caldav", "href": href, "etag": response.headers.get("ETag", "")})
        return event

    def _find(self, event_id: str) -> dict[str, Any]:
        rows = self.list_events(datetime.now().astimezone() - timedelta(days=366),
                                datetime.now().astimezone() + timedelta(days=3660), 1000)
        match = next((row for row in rows if row.get("id") == event_id or row.get("href") == event_id), None)
        if not match:
            raise CalendarError(f"Événement CalDAV introuvable : {event_id}.")
        return match

    def update_event(self, event_id: str, changes: dict[str, Any], **_: Any) -> dict[str, Any]:
        event = self._find(event_id)
        event.update({k: v for k, v in changes.items() if v is not None})
        headers = {"Content-Type": "text/calendar; charset=utf-8"}
        if event.get("etag"): headers["If-Match"] = event["etag"]
        response = self._request("PUT", event["href"], headers=headers,
                                 data=build_ics_event(event).encode())
        if response.status_code not in {200, 201, 204}:
            raise CalendarError(f"Modification CalDAV refusée (HTTP {response.status_code}).")
        event["etag"] = response.headers.get("ETag", event.get("etag", ""))
        return event

    def delete_event(self, event_id: str, **_: Any) -> None:
        event = self._find(event_id)
        headers = {"If-Match": event["etag"]} if event.get("etag") else {}
        response = self._request("DELETE", event["href"], headers=headers)
        if response.status_code not in {200, 202, 204}:
            raise CalendarError(f"Suppression CalDAV refusée (HTTP {response.status_code}).")


class CalendarService:
    """Choisit le fournisseur demandé, ou celui déjà connecté en mode auto."""

    def __init__(self):
        self._lock = threading.RLock()

    def provider(self, name: str = "auto"):
        wanted = (name or "auto").strip().casefold()
        cfg = _config()
        if wanted in {"caldav", "dav"}:
            return CalDAVProvider(str(cfg.get("caldav_calendar_url") or cfg.get("caldav_url") or ""),
                                  str(cfg.get("caldav_username") or ""), str(cfg.get("caldav_password") or ""))
        google = GoogleCalendarProvider()
        if wanted in {"google", "google_calendar", "gcal"}:
            return google
        if google.status().authenticated:
            return google
        caldav = CalDAVProvider(str(cfg.get("caldav_calendar_url") or cfg.get("caldav_url") or ""),
                                str(cfg.get("caldav_username") or ""), str(cfg.get("caldav_password") or ""))
        if caldav.status().configured:
            return caldav
        return google

    def status(self, provider: str = "auto", verify: bool = False) -> CalendarStatus:
        return self.provider(provider).status(verify=verify)

    def connect(self, provider: str = "google") -> CalendarStatus:
        selected = self.provider(provider)
        if not isinstance(selected, GoogleCalendarProvider):
            return selected.status(verify=True)
        return selected.connect()

    def list_events(self, start: Any, end: Any, provider: str = "auto", **kwargs):
        return self.provider(provider).list_events(start, end, **kwargs)

    def create_event(self, title: str, start: Any, end: Any, provider: str = "auto", **kwargs):
        validate_event_window(start, end)
        return self.provider(provider).create_event(title, start, end, **kwargs)

    def find_conflicts(self, start: Any, end: Any, provider: str = "auto", **kwargs) -> list[dict[str, Any]]:
        """Liste les rendez-vous qui se chevauchent, sans jamais les modifier."""
        begins, finishes = validate_event_window(start, end)
        events = self.list_events(begins, finishes, provider=provider, max_results=100, **kwargs)
        conflicts = []
        for event in events:
            event_start, event_end = event.get("start"), event.get("end")
            if event_start and event_end and events_overlap(begins, finishes, event_start, event_end):
                conflicts.append(event)
        return conflicts

    def update_event(self, event_id: str, changes: dict[str, Any], provider: str = "auto", **kwargs):
        return self.provider(provider).update_event(event_id, changes, **kwargs)

    def delete_event(self, event_id: str, provider: str = "auto", **kwargs):
        return self.provider(provider).delete_event(event_id, **kwargs)


_SERVICE: CalendarService | None = None
_SERVICE_LOCK = threading.Lock()


def get_calendar_service() -> CalendarService:
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is None:
            _SERVICE = CalendarService()
        return _SERVICE
