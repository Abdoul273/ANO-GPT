"""core/incident_log.py — Registre des erreurs vécues, annoncées à la voix.

Une erreur d'outil ou de session ne doit plus se résumer à une ligne « ERR »
dans un journal que personne ne lit. Chaque incident est gardé ici avec sa
pile d'appel et le fichier du projet impliqué ; quelques secondes après, il
est annoncé par le canal proactif (« Une erreur vient de se produire dans
la météo : … Dis "corrige" et je m'en occupe. »). C'est ce registre que
``core.auto_fix`` lit quand l'utilisateur demande la réparation.

Fichier : $XDG_CONFIG_HOME/jarvis/incidents.json (30 derniers).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

ANNOUNCE_DELAY_S = 3.0        # « au bout de quelques secondes »
DEDUPE_WINDOW_S = 600.0       # la même erreur n'est pas répétée pendant 10 min
KEEP = 30

_LOCK = threading.RLock()
_INCIDENTS: list["Incident"] = []
_LAST_ANNOUNCED: dict[str, float] = {}
_PUBLISHER: Optional[Callable[..., Any]] = None
_LOGGER: Optional[Callable[[str], None]] = None
_TIMER: Optional[threading.Timer] = None
_PENDING: list["Incident"] = []
_LOADED = False


def _path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "jarvis" / "incidents.json"


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


@dataclass
class Incident:
    ts: float
    source: str                 # nom d'outil ou « session », « audio »…
    kind: str                   # type d'exception
    message: str                # phrase courte lue à l'utilisateur
    traceback: str = ""
    file: str = ""              # fichier du projet le plus proche du crash
    line: int = 0
    key: str = ""
    fixed: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def spoken(self) -> str:
        where = f" ({Path(self.file).name}, ligne {self.line})" if self.file else ""
        return f"{self.source}{where} : {self.message}"


def _project_frame(tb: str) -> tuple[str, int]:
    """Dernier cadre de la pile qui appartient au projet (pas à une lib)."""
    root = str(_repo_root())
    found = ("", 0)
    for m in re.finditer(r'File "([^"]+)", line (\d+)', tb):
        path = m.group(1)
        if path.startswith(root) and "/site-packages/" not in path and "/.venv/" not in path:
            found = (os.path.relpath(path, root), int(m.group(2)))
    return found


def bind(publisher: Optional[Callable[..., Any]], logger: Optional[Callable[[str], None]] = None) -> None:
    """Branche le canal proactif (``ProactiveService.publish``) et le journal UI."""
    global _PUBLISHER, _LOGGER
    _PUBLISHER, _LOGGER = publisher, logger


def _load() -> None:
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
        for item in raw if isinstance(raw, list) else []:
            try:
                _INCIDENTS.append(Incident(**{k: v for k, v in item.items() if k in Incident.__dataclass_fields__}))
            except Exception:
                continue
    except Exception:
        pass


def _save() -> None:
    try:
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps([asdict(i) for i in _INCIDENTS[-KEEP:]], ensure_ascii=False, indent=1),
                     encoding="utf-8")
    except Exception:
        pass


def record(source: str, exc: Optional[BaseException] = None, *, message: str = "",
           tb: str = "", extra: Optional[dict] = None) -> Incident:
    """Enregistre une erreur et programme son annonce."""
    with _LOCK:
        _load()
        if exc is not None and not tb:
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        kind = type(exc).__name__ if exc is not None else "Erreur"
        short = " ".join(str(message or (str(exc) if exc else "") or kind).split())[:220]
        file, line = _project_frame(tb)
        key = f"{source}:{kind}:{short[:60].casefold()}"
        inc = Incident(ts=time.time(), source=str(source or "système"), kind=kind, message=short,
                       traceback=tb[-6000:], file=file, line=line, key=key, extra=dict(extra or {}))
        _INCIDENTS.append(inc)
        del _INCIDENTS[:-KEEP]
        _save()
        _schedule_announce(inc)
        return inc


def _schedule_announce(inc: Incident) -> None:
    global _TIMER
    now = time.time()
    if now - _LAST_ANNOUNCED.get(inc.key, 0.0) < DEDUPE_WINDOW_S:
        return
    _LAST_ANNOUNCED[inc.key] = now
    _PENDING.append(inc)
    if _TIMER is not None:
        _TIMER.cancel()
    _TIMER = threading.Timer(ANNOUNCE_DELAY_S, _flush_announce)
    _TIMER.daemon = True
    _TIMER.start()


def _flush_announce() -> None:
    global _TIMER
    with _LOCK:
        pending, _PENDING[:] = list(_PENDING), []
        _TIMER = None
    if not pending:
        return
    latest = pending[-1]
    if len(pending) == 1:
        text = (f"Une erreur vient de se produire dans {latest.spoken()}. "
                "Dis « corrige » et je m'en occupe.")
    else:
        text = (f"{len(pending)} erreurs viennent de se produire, la dernière dans {latest.spoken()}. "
                "Dis « corrige » et je répare la dernière, ou « répare tout ».")
    if _LOGGER:
        try:
            _LOGGER(f"SYS : incident — {latest.source} : {latest.message}")
        except Exception:
            pass
    if _PUBLISHER:
        try:
            _PUBLISHER("incident", text, dedupe_key=f"incident:{latest.key}", priority=88,
                       data={"source": latest.source, "file": latest.file})
        except Exception:
            pass


def incidents(limit: int = 10, unresolved_only: bool = False) -> list[Incident]:
    with _LOCK:
        _load()
        items = [i for i in _INCIDENTS if not (unresolved_only and i.fixed)]
        return list(reversed(items[-limit:]))


def last(unresolved_only: bool = True) -> Optional[Incident]:
    found = incidents(limit=1, unresolved_only=unresolved_only)
    return found[0] if found else None


# Mots français → noms d'outils, pour « corrige la météo ».
_ALIASES = {
    "météo": "weather", "meteo": "weather", "mail": "email", "courriel": "email",
    "rappel": "reminder", "agenda": "calendar", "musique": "music", "carte": "map",
    "navigation": "navigate", "itinéraire": "navigate", "photo": "camera", "caméra": "camera",
    "vidéo": "video", "image": "image", "recherche": "search", "session": "session",
    "voix": "session", "tiktok": "tiktok", "téléphone": "phone", "sms": "phone",
}


def find(query: str, strict: bool = False) -> Optional[Incident]:
    """Incident visé par des mots (« la météo », « tiktok », « la dernière »).

    ``strict`` : ne pas se rabattre sur le plus récent quand les mots ne
    correspondent à aucun incident."""
    q = str(query or "").casefold().strip()
    items = incidents(limit=KEEP)
    if not items:
        return None
    if not q or re.search(r"derni|last|celle|cette|l'erreur", q):
        return items[0]
    words = [w for w in re.findall(r"[a-zà-ÿ0-9_]{3,}", q) if w not in {"erreur", "corrige", "répare", "repare", "outil"}]
    words += [_ALIASES[w] for w in words if w in _ALIASES]
    for inc in items:
        hay = f"{inc.source} {inc.message} {inc.file}".casefold()
        if any(w in hay for w in words):
            return inc
    return None if strict else items[0]


def mark_fixed(inc: Incident) -> None:
    with _LOCK:
        inc.fixed = True
        for i in _INCIDENTS:
            if i.key == inc.key and abs(i.ts - inc.ts) < 1:
                i.fixed = True
        _save()


def describe(inc: Incident) -> str:
    when = time.strftime("%H:%M:%S", time.localtime(inc.ts))
    where = f" dans {inc.file} ligne {inc.line}" if inc.file else ""
    return f"À {when}, {inc.source} a échoué{where} : {inc.kind} — {inc.message}"
