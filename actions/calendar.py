"""Action agenda unifiée, avec résolution des invités depuis les contacts."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Any

from core.calendar_service import (
    CalendarError, CalendarSetupRequired, get_calendar_service,
    validate_event_window,
)
from core.contacts import ContactError, get_contacts_book

from core import action_kit as kit


def _range(params: dict[str, Any]) -> tuple[Any, Any]:
    start = params.get("start") or params.get("date")
    end = params.get("end")
    if not start:
        today = date.today()
        start = datetime.combine(today, time.min).astimezone()
        end = datetime.combine(today + timedelta(days=1), time.min).astimezone()
    elif not end:
        if isinstance(start, str) and len(start.strip()) == 10:
            end = (date.fromisoformat(start.strip()) + timedelta(days=1)).isoformat()
        else:
            end = datetime.fromisoformat(str(start).replace("Z", "+00:00")) + timedelta(hours=1)
    return start, end


def _resolve_attendees(values: Any) -> list[str]:
    if isinstance(values, str):
        values = [part.strip() for part in values.split(",")]
    result = []
    for value in values or []:
        text = str(value).strip()
        if not text:
            continue
        if "@" in text:
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", text):
                raise ContactError(f"Adresse e-mail invalide pour l'invité « {text} ».")
            result.append(text)
            continue
        resolved = get_contacts_book().resolve(text, "email")
        if not resolved:
            raise ContactError(f"Contact introuvable pour l'invité « {text} ».")
        result.append(resolved.value)
    return list(dict.fromkeys(result))


def _format_events(events: list[dict[str, Any]]) -> str:
    if not events:
        return "Aucun événement dans cette période."
    lines = []
    for index, event in enumerate(events, 1):
        start = str(event.get("start", ""))
        where = f" — {event['location']}" if event.get("location") else ""
        lines.append(f"{index}. {start} · {event.get('title', 'Sans titre')}{where} [id: {event.get('id', '')}]")
    return "Agenda :\n" + "\n".join(lines)


def _format_conflicts(events: list[dict[str, Any]]) -> str:
    if not events:
        return "Créneau disponible : aucun chevauchement détecté."
    return "Conflit d'agenda détecté :\n" + "\n".join(
        f"- {event.get('start', '')} · {event.get('title', 'Sans titre')}"
        for event in events
    )


@kit.action("calendar_control")
def calendar_control(parameters: dict | None = None) -> str:
    params = parameters or {}
    action = str(params.get("action", "list") or "list").strip().casefold()
    provider = str(params.get("provider", "auto") or "auto")
    service = get_calendar_service()
    try:
        if action in {"status", "diagnostic"}:
            status = service.status(provider, verify=bool(params.get("verify", True)))
            return status.message + (f"\nProchaine étape : {status.next_step}" if status.next_step else "")
        if action in {"connect", "login", "authorize"}:
            return service.connect(provider if provider != "auto" else "google").message
        if action in {"list", "events", "agenda"}:
            start, end = _range(params)
            events = service.list_events(start, end, provider=provider,
                                         max_results=max(1, min(int(params.get("max_results", 20)), 100)),
                                         calendar_id=str(params.get("calendar_id", "primary")))
            return _format_events(events)
        if action in {"availability", "available", "conflicts", "check"}:
            start, end = _range(params)
            validate_event_window(start, end)
            conflicts = service.find_conflicts(
                start, end, provider=provider,
                calendar_id=str(params.get("calendar_id", "primary")),
            )
            return _format_conflicts(conflicts)
        if action in {"create", "add", "ajouter"}:
            title = str(params.get("title", "") or "").strip()
            start, end = _range(params)
            if not title:
                return "Précisez le titre de l'événement."
            begins, finishes = validate_event_window(start, end)
            conflicts = service.find_conflicts(
                begins, finishes, provider=provider,
                calendar_id=str(params.get("calendar_id", "primary")),
            )
            preview = (
                f"Prévisualisation : {title}, du {begins.isoformat()} au {finishes.isoformat()}.\n"
                + _format_conflicts(conflicts)
            )
            if bool(params.get("dry_run", False)):
                return preview
            if conflicts and not bool(params.get("allow_conflict", False)):
                return preview + "\nAucun événement créé. Demandez confirmation à l'utilisateur puis relancez avec allow_conflict=true si ce chevauchement est voulu."
            event = service.create_event(
                title, begins, finishes, provider=provider,
                description=str(params.get("description", "") or ""),
                location=str(params.get("location", "") or ""),
                attendees=_resolve_attendees(params.get("attendees") or []),
                calendar_id=str(params.get("calendar_id", "primary")),
            )
            return f"Événement créé : {event['title']} le {event['start']} (id : {event['id']})."
        if action in {"update", "modify", "modifier"}:
            event_id = str(params.get("id", "") or "").strip()
            if not event_id:
                return "Précisez l'identifiant de l'événement à modifier."
            changes = {key: params[key] for key in ("title", "start", "end", "description", "location") if key in params}
            if "attendees" in params:
                changes["attendees"] = _resolve_attendees(params["attendees"])
            event = service.update_event(event_id, changes, provider=provider,
                                         calendar_id=str(params.get("calendar_id", "primary")))
            return f"Événement modifié : {event['title']} le {event['start']}."
        if action in {"delete", "remove", "cancel", "supprimer"}:
            event_id = str(params.get("id", "") or "").strip()
            if not event_id:
                return "Précisez l'identifiant de l'événement à supprimer."
            service.delete_event(event_id, provider=provider,
                                 calendar_id=str(params.get("calendar_id", "primary")))
            return f"Événement supprimé : {event_id}."
        return "Action agenda inconnue. Actions : status, connect, list, availability, create, update, delete."
    except (CalendarError, CalendarSetupRequired, ContactError, ValueError) as exc:
        return f"Erreur agenda : {exc}"
