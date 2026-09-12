"""Modèle d'habitudes local, volontairement conservateur.

Il n'apprend que les actions explicitement réussies. Les données restent dans
SQLite local et une habitude ne devient suggérable qu'après cinq occurrences
réparties dans les trois dernières semaines.
"""
from __future__ import annotations

import re
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "memory" / "habits.db"
WINDOW_DAYS = 21
MIN_OCCURRENCES = 5       # « au-delà de 4 »
MIN_DISTINCT_DAYS = 4     # un clic répété ne crée pas une habitude
DECLINE_DAYS = 30


def _event(value: str) -> str:
    return re.sub(r"[^a-z0-9:_-]", "", str(value).casefold())[:100]


class HabitModel:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or DEFAULT_PATH)
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=5)
        con.row_factory = sqlite3.Row
        return con

    def _init_db(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as con:
                con.executescript("""
                    CREATE TABLE IF NOT EXISTS habitudes (
                        jour TEXT NOT NULL,
                        creneau_30min INTEGER NOT NULL,
                        evenement TEXT NOT NULL,
                        occurrences INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (jour, creneau_30min, evenement)
                    );
                    CREATE TABLE IF NOT EXISTS habit_refus (
                        evenement TEXT PRIMARY KEY,
                        jusquau TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS habit_meta (
                        cle TEXT PRIMARY KEY,
                        valeur TEXT NOT NULL
                    );
                """)
        except Exception:
            pass

    @staticmethod
    def slot(when: datetime) -> int:
        return when.hour * 2 + when.minute // 30

    def record(self, event: str, when: datetime | None = None) -> None:
        event = _event(event)
        if not event:
            return
        when = when or datetime.now()
        try:
            with self._lock, self._connect() as con:
                con.execute("""
                    INSERT INTO habitudes (jour, creneau_30min, evenement, occurrences)
                    VALUES (?, ?, ?, 1)
                    ON CONFLICT(jour, creneau_30min, evenement)
                    DO UPDATE SET occurrences = occurrences + 1
                """, (when.date().isoformat(), self.slot(when), event))
                # Données hors fenêtre : aucune trace historique inutile.
                con.execute("DELETE FROM habitudes WHERE jour < ?",
                            ((when.date() - timedelta(days=WINDOW_DAYS)).isoformat(),))
        except Exception:
            pass

    def import_once(self, source: str, events: list[tuple[str, datetime]]) -> None:
        """Importe un historique existant une seule fois, de façon idempotente."""
        source = _event(f"import:{source}")
        if not source:
            return
        try:
            with self._lock, self._connect() as con:
                if con.execute("SELECT 1 FROM habit_meta WHERE cle = ?", (source,)).fetchone():
                    return
                for event, when in events:
                    event = _event(event)
                    if not event or not isinstance(when, datetime):
                        continue
                    con.execute("""
                        INSERT INTO habitudes (jour, creneau_30min, evenement, occurrences)
                        VALUES (?, ?, ?, 1)
                        ON CONFLICT(jour, creneau_30min, evenement)
                        DO UPDATE SET occurrences = occurrences + 1
                    """, (when.date().isoformat(), self.slot(when), event))
                con.execute("INSERT INTO habit_meta (cle, valeur) VALUES (?, '1')", (source,))
        except Exception:
            pass

    def candidate(self, when: datetime | None = None) -> str | None:
        """Réserve au plus une suggestion par jour et renvoie son événement."""
        when = when or datetime.now()
        day = when.date().isoformat()
        cutoff = (when.date() - timedelta(days=WINDOW_DAYS)).isoformat()
        slot = self.slot(when)
        try:
            with self._lock, self._connect() as con:
                already = con.execute(
                    "SELECT valeur FROM habit_meta WHERE cle = 'suggestion_jour'"
                ).fetchone()
                if already and already["valeur"] == day:
                    return None
                row = con.execute("""
                    SELECT h.evenement, SUM(h.occurrences) AS total,
                           COUNT(DISTINCT h.jour) AS active_days
                    FROM habitudes h
                    LEFT JOIN habit_refus r ON r.evenement = h.evenement
                    WHERE h.jour >= ? AND h.creneau_30min = ?
                      AND (r.jusquau IS NULL OR r.jusquau < ?)
                    GROUP BY h.evenement
                    HAVING total >= ? AND active_days >= ?
                    ORDER BY total DESC, h.evenement ASC
                    LIMIT 1
                """, (cutoff, slot, day, MIN_OCCURRENCES, MIN_DISTINCT_DAYS)).fetchone()
                if not row:
                    return None
                con.execute("""
                    INSERT INTO habit_meta (cle, valeur) VALUES ('suggestion_jour', ?)
                    ON CONFLICT(cle) DO UPDATE SET valeur = excluded.valeur
                """, (day,))
                return str(row["evenement"])
        except Exception:
            return None

    def decline(self, event: str, when: datetime | None = None) -> None:
        event = _event(event)
        if not event:
            return
        when = when or datetime.now()
        until = (when.date() + timedelta(days=DECLINE_DAYS)).isoformat()
        try:
            with self._lock, self._connect() as con:
                con.execute("""
                    INSERT INTO habit_refus (evenement, jusquau) VALUES (?, ?)
                    ON CONFLICT(evenement) DO UPDATE SET jusquau = excluded.jusquau
                """, (event, until))
        except Exception:
            pass


def suggestion_text(event: str) -> str | None:
    """Formulation déterministe : aucune décision ni donnée n'est envoyée au LLM."""
    if event == "music":
        return "C'est souvent ton créneau musique. Je mets ta playlist ?"
    if event.startswith("app:") and len(event) > 4:
        app = event[4:].replace("-", " ")
        return f"Tu ouvres souvent {app} vers cette heure-ci. Je le lance ?"
    return None
