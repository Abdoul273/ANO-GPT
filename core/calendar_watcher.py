"""Watcher d'anticipation de réunion pour le moteur de proactivité ANO-GPT.

Surveille les agendas connectés (Google Calendar, CalDAV) et déclenche
une alerte proactive 10 minutes avant le début de chaque réunion.
Gère le dédoublonnage persistant et le regroupement d'événements proches.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger("anogpt.calendar_watcher")

_DEFAULT_STATE_PATH = Path.home() / ".config" / "jarvis" / "calendar_anticipation.json"


def parse_event_start(event: dict[str, Any]) -> datetime | None:
    """Extrait et convertit la date/heure de début d'un événement.

    Ignore les événements journée entière (date seule sans heure 'T').
    Garantit un objet datetime conscient du fuseau horaire (timezone-aware).
    """
    raw = str(event.get("start") or "").strip()
    if not raw or "T" not in raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return parsed
    except Exception:
        return None


def format_meeting_announcement(
    meetings: list[dict[str, Any]],
    now: datetime | None = None,
) -> str:
    """Formule une annonce naturelle pour une ou plusieurs réunions proches.

    - 1 réunion : « Tu as [nom de la réunion] dans 10 minutes. »
    - Plusieurs réunions : « Tu as 2 réunions dans 10 minutes : [Nom 1] et [Nom 2]. »
    """
    if not meetings:
        return ""

    if len(meetings) == 1:
        title = str(meetings[0].get("title", "")).strip() or "une réunion"
        delay_str = "dans 10 minutes"
        if now:
            start_dt = parse_event_start(meetings[0])
            if start_dt:
                mins = max(1, round((start_dt - now).total_seconds() / 60))
                if mins < 8:
                    delay_str = f"dans {mins} minute{'s' if mins > 1 else ''}"
        return f"Tu as {title} {delay_str}."

    titles = [str(m.get("title", "")).strip() or "une réunion" for m in meetings]
    count = len(titles)
    if count == 2:
        names = f"{titles[0]} et {titles[1]}"
    else:
        names = ", ".join(titles[:-1]) + f" et {titles[-1]}"

    delay_str = "dans 10 minutes"
    if now:
        starts = [parse_event_start(m) for m in meetings]
        valid = [s for s in starts if s is not None]
        if valid:
            avg_mins = max(1, round(sum((s - now).total_seconds() for s in valid) / len(valid) / 60))
            if avg_mins < 8:
                delay_str = f"dans {avg_mins} minute{'s' if avg_mins > 1 else ''}"

    return f"Tu as {count} réunions {delay_str} : {names}."


class CalendarWatcher:
    """Détecte les réunions imminentes avec dédoublonnage persistant."""

    RETENTION_SECONDS = 7 * 86400  # 7 jours

    def __init__(self, state_file: str | Path | None = None) -> None:
        self._state_file = Path(state_file) if state_file else _DEFAULT_STATE_PATH
        self._lock = threading.RLock()
        self._announced: dict[str, float] = self._load_state()

    def _load_state(self) -> dict[str, float]:
        try:
            if self._state_file.is_file():
                raw = json.loads(self._state_file.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    cutoff = time.time() - self.RETENTION_SECONDS
                    return {
                        str(k): float(v)
                        for k, v in raw.items()
                        if isinstance(v, (int, float)) and float(v) >= cutoff
                    }
        except Exception as exc:
            logger.debug("Lecture de l'état calendar_watcher impossible : %s", exc)
        return {}

    def _save_state(self) -> None:
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            cutoff = time.time() - self.RETENTION_SECONDS
            payload = {
                k: v for k, v in self._announced.items()
                if v >= cutoff
            }
            fd, temporary = tempfile.mkstemp(prefix=".cal-watch-", dir=self._state_file.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(payload, stream, ensure_ascii=False, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(temporary, 0o600)
                os.replace(temporary, self._state_file)
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
        except Exception as exc:
            logger.warning("Sauvegarde de l'état calendar_watcher impossible : %s", exc)

    @staticmethod
    def event_id(event: dict[str, Any]) -> str:
        raw_id = str(event.get("id") or "").strip()
        if raw_id:
            return raw_id
        title = str(event.get("title") or "sans_titre").strip()
        start = str(event.get("start") or "").strip()
        return f"{title}:{start}"

    def is_announced(self, event_id: str) -> bool:
        with self._lock:
            return event_id in self._announced

    def mark_announced(self, meetings: list[dict[str, Any]]) -> None:
        now = time.time()
        with self._lock:
            for m in meetings:
                eid = self.event_id(m)
                self._announced[eid] = now
            self._save_state()

    def filter_upcoming_meetings(
        self,
        events: list[dict[str, Any]],
        now: datetime | None = None,
        window_seconds: float = 600.0,
    ) -> list[dict[str, Any]]:
        """Sélectionne les réunions non annoncées débutant dans la fenêtre [0, window_seconds]."""
        now = now or datetime.now().astimezone()
        upcoming: list[tuple[datetime, dict[str, Any]]] = []

        with self._lock:
            for event in events:
                eid = self.event_id(event)
                if eid in self._announced:
                    continue
                start_dt = parse_event_start(event)
                if start_dt is None:
                    continue
                delta_s = (start_dt - now).total_seconds()
                # Événements débutant entre maintenant et 10 minutes (avec marge de 15s)
                if 0 <= delta_s <= (window_seconds + 15.0):
                    upcoming.append((start_dt, event))

        upcoming.sort(key=lambda item: item[0])
        return [item[1] for item in upcoming]

    def check_and_produce_announcement(
        self,
        service: Any,
        now: datetime | None = None,
    ) -> tuple[str, list[dict[str, Any]]] | None:
        """Interroge le service, filtre les réunions proches et produit l'annonce sans doublon."""
        now = now or datetime.now().astimezone()
        start = now - timedelta(minutes=1)
        end = now + timedelta(minutes=15)

        try:
            events = service.list_events(start, end)
        except Exception as exc:
            logger.debug("Erreur lors de la récupération des événements agenda : %s", exc)
            return None

        if not events or not isinstance(events, list):
            return None

        upcoming = self.filter_upcoming_meetings(events, now=now)
        if not upcoming:
            return None

        message = format_meeting_announcement(upcoming, now=now)
        self.mark_announced(upcoming)
        return message, upcoming
