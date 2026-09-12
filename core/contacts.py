"""Carnet de contacts local, durable et utilisable par les actions ANO-GPT."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

_BASE_DIR = Path(__file__).resolve().parent.parent
CONTACTS_FILE = _BASE_DIR / "memory" / "contacts.json"


class ContactError(ValueError):
    """Erreur de carnet affichable à l'utilisateur."""


class ContactAmbiguous(ContactError):
    def __init__(self, query: str, names: Iterable[str]):
        self.query = query
        self.names = tuple(names)
        super().__init__(
            f"Le contact « {query} » est ambigu : {', '.join(self.names)}."
        )


@dataclass(frozen=True)
class ResolvedContact:
    contact: dict[str, Any]
    value: str
    channel: str


def _fold(value: Any) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", str(value or "").casefold())
        if not unicodedata.combining(c)
    ).strip()


def _clean_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    return list(dict.fromkeys(str(v).strip() for v in value if str(v).strip()))


class ContactsBook:
    def __init__(self, path: Path | str = CONTACTS_FILE):
        self.path = Path(path)
        self._lock = threading.RLock()

    def _read(self) -> list[dict[str, Any]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError) as exc:
            raise ContactError(f"Carnet de contacts illisible : {exc}") from exc
        rows = raw.get("contacts", []) if isinstance(raw, dict) else raw
        return [dict(row) for row in rows if isinstance(row, dict)]

    def _write(self, rows: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".contacts-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"version": 1, "contacts": rows}, stream,
                          ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return sorted(self._read(), key=lambda row: _fold(row.get("name")))

    def save(self, *, name: str = "", contact_id: str = "", aliases: Any = None,
             emails: Any = None, phone: str | None = None, handles: Any = None,
             notes: str | None = None) -> dict[str, Any]:
        name = str(name or "").strip()
        with self._lock:
            rows = self._read()
            target = None
            if contact_id:
                target = next((r for r in rows if r.get("id") == contact_id), None)
            if target is None:
                exact = [r for r in rows if _fold(r.get("name")) == _fold(name)]
                if len(exact) == 1:
                    target = exact[0]
            if target is None:
                if not name:
                    raise ContactError("Le nom du contact est obligatoire.")
                target = {"id": uuid.uuid4().hex}
                rows.append(target)
            if name:
                target["name"] = name
            if aliases is not None:
                target["aliases"] = _clean_list(aliases)
            if emails is not None:
                target["emails"] = _clean_list(emails)
            if phone is not None:
                target["phone"] = str(phone or "").strip()
            if handles is not None:
                target["handles"] = {
                    _fold(k): str(v).strip() for k, v in dict(handles).items()
                    if str(k).strip() and str(v).strip()
                }
            if notes is not None:
                target["notes"] = str(notes or "").strip()
            target.setdefault("aliases", [])
            target.setdefault("emails", [])
            target.setdefault("phone", "")
            target.setdefault("handles", {})
            target.setdefault("notes", "")
            self._write(rows)
            return dict(target)

    def delete(self, value: str) -> bool:
        with self._lock:
            rows = self._read()
            matches = self.find(value)
            if not matches:
                return False
            if len(matches) > 1:
                raise ContactAmbiguous(value, (r.get("name", "") for r in matches))
            contact_id = matches[0].get("id")
            self._write([r for r in rows if r.get("id") != contact_id])
            return True

    def find(self, query: str) -> list[dict[str, Any]]:
        needle = _fold(query)
        if not needle:
            return self.list()
        rows = self.list()
        exact, prefix, partial = [], [], []
        for row in rows:
            values = [row.get("id", ""), row.get("name", "")]
            values.extend(row.get("aliases") or [])
            values.extend(row.get("emails") or [])
            values.extend((row.get("handles") or {}).values())
            folded = [_fold(v) for v in values if v]
            if needle in folded:
                exact.append(row)
            elif any(v.startswith(needle) for v in folded):
                prefix.append(row)
            elif any(needle in v for v in folded):
                partial.append(row)
        return exact or prefix or partial

    def resolve(self, query: str, channel: str = "") -> ResolvedContact | None:
        matches = self.find(query)
        if not matches:
            return None
        if len(matches) > 1:
            raise ContactAmbiguous(query, (r.get("name", "") for r in matches))
        row = matches[0]
        key = _fold(channel)
        handles = row.get("handles") or {}
        if key in {"email", "mail", "gmail"}:
            values = row.get("emails") or []
            if not values:
                raise ContactError(f"Aucune adresse e-mail enregistrée pour {row['name']}.")
            value = values[0]
        elif key and handles.get(key):
            value = handles[key]
        elif key in {"whatsapp", "signal", "sms", "telephone", "phone"} and row.get("phone"):
            value = row["phone"]
        else:
            value = row.get("name", query)
        return ResolvedContact(dict(row), str(value), key)


_BOOK: ContactsBook | None = None
_BOOK_LOCK = threading.Lock()


def get_contacts_book() -> ContactsBook:
    global _BOOK
    with _BOOK_LOCK:
        if _BOOK is None:
            _BOOK = ContactsBook()
        return _BOOK
