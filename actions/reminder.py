#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
reminder.py — Système de rappels ultra‑réaliste, version corrigée et renforcée.
Parsing local avancé, planification inter‑OS, notifications natives,
registre persistant avec actions list / cancel.

Corrections par rapport à l'ancienne version :
    - `_base_dir` utilisait `Path(file)` au lieu de `Path(__file__)` ;
    - `_get_api_key` levait une KeyError si la clé était absente ;
    - `_sanitise` avait des échappements cassés ;
    - le script de notification généré utilisait `pathlib.Path(file)`
      (NameError au déclenchement) : remplacé par `__file__`, et il nettoie
      désormais aussi le registre des rappels ;
    - `_parse_reminder_text` ne comprenait pas « le 15 mars » (le « le »
      avant le jour) et les délais relatifs étaient limités : ajout de
      « dans X minutes / heures / jours / semaines » ;
    - aucune persistance : ajout d'un registre avec actions list & cancel ;
    - utilisation de python3 du PATH au lieu de sys.executable (fiable même
      en binaire figé) ;
    - le numéro de job `at` est maintenant capturé pour permettre l'annulation.
"""
import json
import platform
import re
import sys
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List

from core import action_kit as kit
from core.live_model_policy import FAST_MODEL


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def _get_os() -> str:
    _sys = platform.system()
    if _sys == "Darwin":
        return "mac"
    if _sys == "Linux":
        return "linux"
    return "windows"


def _scripts_dir() -> Path:
    d = Path.home() / ".jarvis" / "reminders"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _events_dir() -> Path:
    d = _scripts_dir() / "events"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _python_exe() -> str:
    """Interprète Python fiable pour exécuter le script de notification."""
    return kit.which("python3") or kit.which("python") or sys.executable


def _sanitise(text: str, max_len: int = 200) -> str:
    return (
        text.replace('"', " ")
        .replace("'", " ")
        .replace("\n", " ")
        .replace("\r", " ")
        .strip()
    )[:max_len]


def _get_api_key() -> str:
    try:
        config_path = _base_dir() / "config" / "api_keys.json"
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f).get("gemini_api_key", "")
    except Exception:
        return ""


def _run_quiet(argv: List[str], timeout: float = 5.0) -> bool:
    return kit.run(argv, timeout=timeout, quiet=True).ok


# ── Registre persistant des rappels ─────────────────────────────────────────
_REGISTRY_PATH = Path.home() / ".config" / "jarvis" / "reminders.json"
_REGISTRY_LOCK = threading.RLock()


def _load_registry() -> List[Dict[str, Any]]:
    with _REGISTRY_LOCK:
        try:
            data = json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []


def _save_registry(data: List[Dict[str, Any]]) -> None:
    with _REGISTRY_LOCK:
        try:
            _REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = _REGISTRY_PATH.with_name(
                f"{_REGISTRY_PATH.name}.{uuid.uuid4().hex}.tmp"
            )
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(_REGISTRY_PATH)
        except Exception:
            pass


def _register_reminder(task_name: str, target_dt: datetime, message: str,
                       scheduler: str, job_id: str) -> None:
    with _REGISTRY_LOCK:
        data = _load_registry()
        data = [e for e in data if e.get("task_name") != task_name]
        data.append({
            "task_name": task_name,
            "datetime": target_dt.strftime("%Y-%m-%d %H:%M"),
            "message": message,
            "os": _get_os(),
            "scheduler": scheduler,
            "job_id": job_id,
            "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        _save_registry(data)


def _unregister_reminder(task_name: str) -> None:
    with _REGISTRY_LOCK:
        data = [e for e in _load_registry() if e.get("task_name") != task_name]
        _save_registry(data)


# ── Script de notification ──────────────────────────────────────────────────
def _write_notify_script(task_name: str, message: str, os_name: str,
                         target_dt: datetime | None = None) -> Path:
    script_path = _scripts_dir() / f"{task_name}.py"
    msg_literal = json.dumps(message, ensure_ascii=False)
    task_literal = json.dumps(task_name)
    due_literal = json.dumps(
        (target_dt or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    )

    event_block = f"""
# Déposer d'abord l'événement pour que l'application puisse parler, même si
# la notification native ou le nettoyage échoue ensuite.
try:
    import json as _json, datetime as _datetime
    _events = pathlib.Path.home() / ".jarvis" / "reminders" / "events"
    _events.mkdir(parents=True, exist_ok=True)
    _event = {{
        "task_name": {task_literal},
        "message": {msg_literal},
        "due": {due_literal},
        "triggered": _datetime.datetime.now().isoformat(timespec="seconds"),
    }}
    _tmp = _events / ({task_literal} + ".tmp")
    _dst = _events / ({task_literal} + ".json")
    _tmp.write_text(_json.dumps(_event, ensure_ascii=False), encoding="utf-8")
    _tmp.replace(_dst)
    # Réveiller le service proactif par son socket : aucune boucle à 350 ms
    # n'est nécessaire quand ANO-GPT tourne. Le fichier reste le repli
    # persistant si l'application est arrêtée au moment de l'échéance.
    try:
        import socket as _socket
        _runtime = os.environ.get("XDG_RUNTIME_DIR")
        _sock_path = (pathlib.Path(_runtime) / "anogpt.sock"
                      if _runtime and pathlib.Path(_runtime).is_dir()
                      else pathlib.Path("/tmp") / ("anogpt-" + str(os.getuid()) + ".sock"))
        _wire = {{
            "topic": "reminder",
            "message": "Rappel : " + {msg_literal},
            "dedupe_key": "reminder:" + {task_literal},
            "priority": 100,
            "data": _event,
        }}
        with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as _s:
            _s.settimeout(1.0)
            _s.connect(str(_sock_path))
            _s.sendall(("proactive-event " + _json.dumps(_wire, ensure_ascii=False) + chr(10)).encode("utf-8"))
    except Exception:
        pass
except Exception:
    pass
"""

    if os_name == "windows":
        notify_block = f"""
message = {msg_literal}
notified = False
try:
    from plyer import notification
    notification.notify(title="J.A.R.V.I.S Reminder", message=message, timeout=15)
    notified = True
except Exception:
    pass
if not notified:
    try:
        from win10toast import ToastNotifier
        ToastNotifier().show_toast("J.A.R.V.I.S Reminder", message, duration=15, threaded=False)
        notified = True
    except Exception:
        pass
if not notified:
    try:
        import subprocess
        kit.run(["msg", "*", "/TIME:30", message], timeout=10)
    except Exception:
        pass
try:
    import winsound
    for freq in [800, 1000, 1200]:
        winsound.Beep(freq, 180)
        import time; time.sleep(0.08)
except Exception:
    pass
"""
    elif os_name == "mac":
        notify_block = f"""
message = {msg_literal}
notified = False
try:
    from plyer import notification
    notification.notify(title="J.A.R.V.I.S Reminder", message=message, timeout=15)
    notified = True
except Exception:
    pass
if not notified:
    try:
        import subprocess
        script = 'display notification "{{}}" with title "J.A.R.V.I.S Reminder"'.format(
            message.replace('"', '')
        )
        kit.run(["osascript", "-e", script], timeout=10)
    except Exception:
        pass
"""
    else:  # linux
        notify_block = f"""
message = {msg_literal}
notified = False
try:
    from plyer import notification
    notification.notify(title="J.A.R.V.I.S Reminder", message=message, timeout=15)
    notified = True
except Exception:
    pass
if not notified:
    try:
        import subprocess
        kit.run(
            ["notify-send", "--urgency=critical", "--expire-time=15000",
             "J.A.R.V.I.S Reminder", message]
        , timeout=10)
    except Exception:
        pass
"""

    script_body = f"""# Auto-generated by J.A.R.V.I.S reminder — do not edit
import sys, os, pathlib
{event_block}
{notify_block}
# Nettoyage du registre des rappels
try:
    import json as _json
    _reg = pathlib.Path.home() / ".config" / "jarvis" / "reminders.json"
    if _reg.exists():
        _data = _json.loads(_reg.read_text(encoding="utf-8"))
        _data = [_e for _e in _data if _e.get("task_name") != {task_literal}]
        _reg_tmp = _reg.with_name(_reg.name + "." + {task_literal} + ".tmp")
        _reg_tmp.write_text(_json.dumps(_data, ensure_ascii=False), encoding="utf-8")
        _reg_tmp.replace(_reg)
except Exception:
    pass
# Auto-suppression après déclenchement
try:
    pathlib.Path(__file__).unlink(missing_ok=True)
except Exception:
    pass
"""
    script_path.write_text(script_body, encoding="utf-8")
    script_path.chmod(0o600)
    return script_path


def pop_triggered_reminders(max_age_hours: float = 6.0) -> List[Dict[str, Any]]:
    """Consomme atomiquement les rappels déclenchés par le planificateur OS."""
    events: List[Dict[str, Any]] = []
    now = datetime.now()
    try:
        paths = sorted(_events_dir().glob("*.json"))
    except OSError:
        return []
    for path in paths:
        claimed = path.with_suffix(".processing")
        try:
            path.replace(claimed)
            payload = json.loads(claimed.read_text(encoding="utf-8"))
            triggered = datetime.fromisoformat(str(payload.get("triggered") or ""))
            age = (now - triggered).total_seconds()
            if (
                isinstance(payload, dict)
                and str(payload.get("message") or "").strip()
                and -60 <= age <= max(60.0, max_age_hours * 3600)
            ):
                events.append(payload)
            claimed.unlink(missing_ok=True)
        except Exception:
            try:
                path.unlink(missing_ok=True)
                claimed.unlink(missing_ok=True)
            except OSError:
                pass
    return sorted(events, key=lambda e: str(e.get("due") or e.get("triggered") or ""))


# ── Planificateurs OS ──────────────────────────────────────────────────────
def _schedule_windows(target_dt: datetime, task_name: str,
                      script_path: Path, message: str) -> Optional[Dict[str, str]]:
    python_exe = Path(sys.executable)
    pythonw = python_exe.parent / "pythonw.exe"
    if pythonw.exists():
        python_exe = pythonw

    xml_path = _scripts_dir() / f"{task_name}.xml"
    xml_content = (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        '  <RegistrationInfo><Description>J.A.R.V.I.S Reminder</Description></RegistrationInfo>\n'
        '  <Triggers><TimeTrigger>\n'
        f'    <StartBoundary>{target_dt.strftime("%Y-%m-%dT%H:%M:%S")}</StartBoundary>\n'
        '    <Enabled>true</Enabled>\n'
        '  </TimeTrigger></Triggers>\n'
        '  <Actions><Exec>\n'
        f'    <Command>{python_exe}</Command>\n'
        f'    <Arguments>"{script_path}"</Arguments>\n'
        '  </Exec></Actions>\n'
        '  <Settings>\n'
        '    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n'
        '    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n'
        '    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n'
        '    <StartWhenAvailable>true</StartWhenAvailable>\n'
        '    <ExecutionTimeLimit>PT5M</ExecutionTimeLimit>\n'
        '    <Enabled>true</Enabled>\n'
        '  </Settings>\n'
        '  <Principals><Principal>\n'
        '    <LogonType>InteractiveToken</LogonType>\n'
        '    <RunLevel>LeastPrivilege</RunLevel>\n'
        '  </Principal></Principals>\n'
        '</Task>'
    )
    xml_path.write_text(xml_content, encoding="utf-16")
    result = kit.run(
        ["schtasks", "/Create", "/TN", task_name, "/XML", str(xml_path), "/F"], timeout=15
    )
    try:
        xml_path.unlink(missing_ok=True)
    except Exception:
        pass
    if result.returncode != 0:
        script_path.unlink(missing_ok=True)
        err = (result.stderr or result.stdout).strip()
        print(f"[Reminder] ❌ schtasks: {err}")
        return None
    return {"scheduler": "schtasks", "job_id": task_name}


def _schedule_mac(target_dt: datetime, task_name: str,
                  script_path: Path) -> Optional[Dict[str, str]]:
    agents_dir = Path.home() / "Library" / "LaunchAgents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    label = f"com.jarvis.reminder.{task_name}"
    plist_path = agents_dir / f"{label}.plist"
    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
"http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>              <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{_python_exe()}</string>
        <string>{script_path}</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Year</key>    <integer>{target_dt.year}</integer>
        <key>Month</key>   <integer>{target_dt.month}</integer>
        <key>Day</key>     <integer>{target_dt.day}</integer>
        <key>Hour</key>    <integer>{target_dt.hour}</integer>
        <key>Minute</key>  <integer>{target_dt.minute}</integer>
    </dict>
    <key>RunAtLoad</key>          <false/>
    <key>StandardOutPath</key>    <string>/dev/null</string>
    <key>StandardErrorPath</key>  <string>/dev/null</string>
</dict>
</plist>
"""
    plist_path.write_text(plist_content, encoding="utf-8")
    plist_path.chmod(0o644)
    result = kit.run(
        ["launchctl", "load", str(plist_path)], timeout=15
    )
    if result.returncode != 0:
        plist_path.unlink(missing_ok=True)
        script_path.unlink(missing_ok=True)
        print(f"[Reminder] ❌ launchctl: {result.stderr.strip()}")
        return None
    return {"scheduler": "launchctl", "job_id": label}


def _schedule_linux(target_dt: datetime, task_name: str,
                    script_path: Path) -> Optional[Dict[str, str]]:
    py = _python_exe()
    if kit.which("systemd-run"):
        on_calendar = target_dt.strftime("%Y-%m-%d %H:%M:00")
        result = kit.run(
            [
                "systemd-run",
                "--user",
                f"--on-calendar={on_calendar}",
                "--timer-property=AccuracySec=1s",
                "--timer-property=Persistent=true",
                f"--unit={task_name}",
                "--",
                py, str(script_path),
            ], timeout=15
        )
        if result.returncode == 0:
            return {"scheduler": "systemd", "job_id": task_name}
        print(f"[Reminder] ⚠️ systemd-run failed: {result.stderr.strip()}, trying 'at'")

    if kit.which("at"):
        at_time = target_dt.strftime("%H:%M %Y-%m-%d")
        cmd_str = f"{py} {script_path}\n"
        result = kit.run(
            ["at", at_time],
            stdin=cmd_str, timeout=15
        )
        if result.returncode == 0:
            # at écrit « job N at ... » sur stderr
            m = re.search(r"job\s+(\d+)", result.stderr or "")
            job_id = m.group(1) if m else ""
            return {"scheduler": "at", "job_id": job_id}
        print(f"[Reminder] ❌ at: {result.stderr.strip()}")
        return None

    print("[Reminder] ❌ Ni systemd-run ni at n'ont été trouvés sur ce Linux.")
    return None


# ── Parsing local intelligent ─────────────────────────────────────────────
_SPELLED_NUMBERS = {
    "une": 1, "un": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5, "six": 6,
    "sept": 7, "huit": 8, "neuf": 9, "dix": 10, "quinze": 15, "vingt": 20,
    "trente": 30, "quarante-cinq": 45, "quarante cinq": 45, "soixante": 60,
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "ten": 10,
    "fifteen": 15, "twenty": 20, "thirty": 30,
}

_WEEKDAYS = {
    "lundi": 0, "mardi": 1, "mercredi": 2, "jeudi": 3, "vendredi": 4,
    "samedi": 5, "dimanche": 6,
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4,
    "saturday": 5, "sunday": 6,
}


def _parse_reminder_text(text: str) -> Optional[Dict[str, Any]]:
    """
    Analyse une phrase naturelle et retourne un dict avec :
    date (YYYY-MM-DD), time (HH:MM), message (chaîne)
    Prend en charge français, anglais, turc.
    """
    text = text.lower().strip()
    text = re.sub(
        r"\b(s'il te pla[iî]t|stp|please|peux-tu|tu peux|rappelle-moi|rappelle moi|"
        r"n'oublie pas|don't forget|hatırlat)\b", "", text
    ).strip()
    text = re.sub(r'[“”«»„"\'`]', '', text)

    now = datetime.now()
    target_dt = None
    time_str = None
    message = "Rappel"
    used_delta = False

    # Nombres dictés : « dans deux heures », « dans une demi-heure ».
    for word, digit in _SPELLED_NUMBERS.items():
        text = re.sub(rf"\b{word}\b", str(digit), text)

    # Moment de la journée dit à la voix : « 8h du soir », « 7 heures du matin », « 3 pm ».
    pm_hint = bool(re.search(r"\b(du soir|de l'après-midi|de l'apres-midi|pm|p\.m\.|"
                             r"ce soir|tonight|cet après-midi|cet apres-midi|this afternoon)\b", text))
    am_hint = bool(re.search(r"\b(du matin|am|a\.m\.)\b", text))
    text = re.sub(r"\b(du soir|de l'après-midi|de l'apres-midi|du matin|pm|am|p\.m\.|a\.m\.)\b",
                  " ", text)

    # « et demie », « et quart », « moins le quart » après l'heure.
    quarter_shift = 0
    for pat, shift in ((r"\s*et\s+demie?\b", 30), (r"\s*et\s+quart\b", 15),
                       (r"\s*moins\s+(?:le\s+)?quart\b", -15)):
        if re.search(pat, text):
            quarter_shift = shift
            text = re.sub(pat, " ", text)
            break

    # 2. Délais relatifs (dans X minutes/heures/jours/semaines)
    unit_map = {
        "minute": "minutes", "min": "minutes",
        "heure": "hours", "h": "hours", "hour": "hours", "hr": "hours",
        "jour": "days", "j": "days", "day": "days",
        "semaine": "weeks", "week": "weeks",
    }
    delta_patterns = [
        r"dans\s+(\d+)\s*(minute|min|heure|h|jour|j|semaine)s?",
        r"in\s+(\d+)\s*(minute|min|hour|hr|day|week)s?",
    ]
    for pat in delta_patterns:
        m = re.search(pat, text)
        if m:
            amt = int(m.group(1))
            unit_en = unit_map.get(m.group(2), "minutes")
            target_dt = now + timedelta(**{unit_en: amt})
            text = text[:m.start()] + text[m.end():]
            used_delta = True
            break

    if not used_delta:
        word_deltas = (
            (r"dans\s+(?:1\s+)?demi[- ]heure", timedelta(minutes=30)),
            (r"dans\s+(?:1\s+)?quart\s+d['’]heure", timedelta(minutes=15)),
            (r"dans\s+trois\s+quarts?\s+d['’]heure", timedelta(minutes=45)),
        )
        for pattern, delta in word_deltas:
            match = re.search(pattern, text)
            if match:
                target_dt = now + delta
                text = text[:match.start()] + text[match.end():]
                used_delta = True
                break

    # 1. Extraire une heure (HH:MM, HhMM, etc.)
    time_patterns = [
        r"(\d{1,2})[h:](\d{2})",
        r"(\d{1,2})\s*(?:heures?|h)\s*(\d{2})?",
        r"\b(?:à|a|at)\s+(\d{1,2})\b",
    ]
    for pat in time_patterns:
        m = re.search(pat, text)
        if m:
            hour = int(m.group(1))
            minute = int(m.group(2)) if m.lastindex >= 2 and m.group(2) else 0
            if pm_hint and hour < 12:
                hour += 12
            elif am_hint and hour == 12:
                hour = 0
            if quarter_shift:
                total = hour * 60 + minute + quarter_shift
                hour, minute = (total // 60) % 24, total % 60
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                time_str = f"{hour:02d}:{minute:02d}"
                text = text[:m.start()] + text[m.end():]
                break

    if not time_str:
        named_times = {
            "midi": "12:00", "noon": "12:00",
            "minuit": "00:00", "midnight": "00:00",
        }
        for label, value in named_times.items():
            match = re.search(rf"\b{label}\b", text)
            if match:
                time_str = value
                text = text[:match.start()] + text[match.end():]
                break

    # 3. Dates relatives
    if not target_dt:
        relative_dates = {
            "aujourd'hui": now, "today": now,
            "demain": now + timedelta(days=1), "tomorrow": now + timedelta(days=1),
            "après-demain": now + timedelta(days=2), "after tomorrow": now + timedelta(days=2),
            "dans 2 jours": now + timedelta(days=2), "in 2 days": now + timedelta(days=2),
            "dans 3 jours": now + timedelta(days=3), "in 3 days": now + timedelta(days=3),
            "dans une semaine": now + timedelta(weeks=1), "in a week": now + timedelta(weeks=1),
            "la semaine prochaine": now + timedelta(weeks=1), "next week": now + timedelta(weeks=1),
            "ce soir": now.replace(hour=20, minute=0, second=0),
            "tonight": now.replace(hour=20, minute=0, second=0),
            "ce matin": now.replace(hour=9, minute=0, second=0),
            "this morning": now.replace(hour=9, minute=0, second=0),
            "cet après-midi": now.replace(hour=14, minute=0, second=0),
            "this afternoon": now.replace(hour=14, minute=0, second=0),
        }
        for key, dt in relative_dates.items():
            if key in text:
                target_dt = dt
                if not time_str and dt.hour != 0:
                    time_str = dt.strftime("%H:%M")
                text = text.replace(key, "").strip()
                break

    # 3 bis. Jour de la semaine : « vendredi », « lundi prochain », « on monday ».
    if not target_dt:
        m = re.search(r"\b(" + "|".join(_WEEKDAYS.keys()) + r")\b(\s+prochain[e]?|\s+next)?", text)
        if m:
            wanted = _WEEKDAYS[m.group(1)]
            ahead = (wanted - now.weekday()) % 7
            if ahead == 0 and (m.group(2) or time_str is None or
                               now.replace(hour=int(time_str[:2]), minute=int(time_str[3:]),
                                           second=0, microsecond=0) <= now):
                ahead = 7
            target_dt = (now + timedelta(days=ahead)).replace(second=0, microsecond=0)
            if not time_str:
                time_str = "09:00"
            text = re.sub(r"\b(?:le\s+|on\s+)?" + re.escape(m.group(0)) + r"\b", " ", text, count=1)

    # 4. Dates absolues (mois + jour, ex: "15 mars", "le 15 mars", "march 12")
    if not target_dt:
        months_fr = {
            "janvier":1,"février":2,"fevrier":2,"mars":3,"avril":4,"mai":5,"juin":6,
            "juillet":7,"août":8,"aout":8,"septembre":9,"octobre":10,"novembre":11,
            "décembre":12,"decembre":12
        }
        months_en = {
            "january":1,"february":2,"march":3,"april":4,"may":5,"june":6,
            "july":7,"august":8,"september":9,"october":10,"november":11,"december":12
        }
        all_months = {**months_fr, **months_en}
        m = re.search(
            r"(?:(?:le|the)\s+)?(\d{1,2})\s+(" + "|".join(all_months.keys()) + r")\s*(\d{4})?",
            text
        )
        if m:
            day = int(m.group(1))
            month_name = m.group(2).lower()
            year = int(m.group(3)) if m.group(3) else now.year
            month = all_months[month_name]
            target_dt = datetime(year, month, day)
            if target_dt < now:
                target_dt = target_dt.replace(year=now.year + 1)
            text = text[:m.start()] + text[m.end():]
            text = text.strip()

    # 5. Si aucune date trouvée, supposer aujourd'hui (si heure future) ou demain
    if not target_dt:
        if time_str:
            h, m = map(int, time_str.split(":"))
            candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if candidate > now:
                target_dt = candidate
            else:
                target_dt = candidate + timedelta(days=1)
        else:
            return None

    # Fusion de la date et de l'heure si nécessaire (pas pour les deltas)
    if time_str and not used_delta:
        h, m = map(int, time_str.split(":"))
        target_dt = target_dt.replace(hour=h, minute=m, second=0, microsecond=0)

    # Nettoyer le message
    message = re.sub(r"\b(rappelle\s*-?\s*moi|remind\s+me|rappel\s*:?\s*|reminder\s*:?\s*)", "", text).strip()
    message = re.sub(r"\s{2,}", " ", message)
    for _ in range(3):  # « à de sortir » → « sortir »
        message = re.sub(
            r"^\s*(de|à|a|at|to|dans|in|sur|pour|d'|d|que|qu'il|qu'elle|le|la|les|l'|un|une|des)\s+",
            "", message
        ).strip()
    if not message:
        message = "Rappel"
    message = message.capitalize()

    return {
        "date": target_dt.strftime("%Y-%m-%d"),
        "time": target_dt.strftime("%H:%M"),
        "message": message[:200]
    }


def _detect_reminder_via_ai(description: str) -> Optional[Dict]:
    api_key = _get_api_key()
    if not api_key:
        return None
    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        prompt = (
            f"Extrais les informations de rappel de la phrase suivante en JSON :\n"
            f"{{'date': 'YYYY-MM-DD', 'time': 'HH:MM', 'message': 'texte du rappel'}}\n"
            f"Si un champ est absent, mets null. Phrase : \"{description}\"\n"
            "Réponds UNIQUEMENT par le JSON."
        )
        resp = client.models.generate_content(model=FAST_MODEL, contents=prompt)
        match = re.search(r'{.*}', resp.text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
    except Exception as e:
        print(f"[Reminder] Erreur IA : {e}")
    return None


# ── Actions list / cancel ──────────────────────────────────────────────────
def active_reminders() -> List[Dict[str, Any]]:
    """Rappels à venir, triés par date — pour la carte de l'UI."""
    now = datetime.now()
    upcoming = []
    for e in _load_registry():
        try:
            dt = datetime.strptime(e.get("datetime", ""), "%Y-%m-%d %H:%M")
            if dt > now:
                upcoming.append(e)
        except Exception:
            continue
    upcoming.sort(key=lambda x: x.get("datetime", ""))
    return upcoming


def _list_reminders() -> str:
    data = _load_registry()
    now = datetime.now()
    upcoming = []
    for e in data:
        try:
            dt = datetime.strptime(e.get("datetime", ""), "%Y-%m-%d %H:%M")
            if dt > now:
                upcoming.append(e)
        except Exception:
            continue
    if not upcoming:
        return "Aucun rappel programmé."
    upcoming.sort(key=lambda x: x.get("datetime", ""))
    lines = [f"📅 {len(upcoming)} rappel(s) programmé(s) :"]
    for i, e in enumerate(upcoming, 1):
        lines.append(f"  {i}. {e.get('datetime')} — {e.get('message', '')[:60]}")
    lines.append("\nPour annuler : « annule le rappel 1 » ou « cancel <nom> ».")
    return "\n".join(lines)


def _cancel_reminder(identifier: str) -> str:
    data = _load_registry()
    if not data:
        return "Aucun rappel programmé."
    entry = None
    if str(identifier).isdigit():
        idx = int(identifier) - 1
        if 0 <= idx < len(data):
            entry = data[idx]
    else:
        low = (identifier or "").lower()
        for e in data:
            if low in e.get("task_name", "").lower() or low in e.get("message", "").lower():
                entry = e
                break
    if not entry:
        return f"Rappel '{identifier}' introuvable."

    os_name = entry.get("os", _get_os())
    sched = entry.get("scheduler", "")
    job = entry.get("job_id", "")
    ok = False
    if os_name == "windows":
        ok = _run_quiet(["schtasks", "/Delete", "/TN", job, "/F"])
    elif os_name == "mac":
        plist = Path.home() / "Library" / "LaunchAgents" / f"{job}.plist"
        kit.run(["launchctl", "unload", str(plist)], timeout=15)
        try:
            plist.unlink(missing_ok=True)
        except Exception:
            pass
        ok = True
    else:
        if sched == "systemd":
            kit.run(["systemctl", "--user", "stop", f"{job}.service"], timeout=15)
            kit.run(["systemctl", "--user", "reset-failed", f"{job}.service"], timeout=15)
            ok = True
        elif sched == "at":
            ok = _run_quiet(["atrm", job])
        else:
            ok = True

    script = _scripts_dir() / f"{entry.get('task_name')}.py"
    try:
        script.unlink(missing_ok=True)
    except Exception:
        pass
    try:
        (_events_dir() / f"{entry.get('task_name')}.json").unlink(missing_ok=True)
        (_events_dir() / f"{entry.get('task_name')}.processing").unlink(missing_ok=True)
    except OSError:
        pass
    _unregister_reminder(entry.get("task_name"))
    if ok:
        return f"✅ Rappel annulé : « {entry.get('message', '')} » ({entry.get('datetime')})."
    return f"⚠️ Impossible d'annuler complètement {job}, mais l'entrée a été retirée."


# ── Point d'entrée principal ────────────────────────────────────────────────
@kit.action("reminder")
def reminder(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    """
    Définit, liste ou annule un rappel système avec notification.
    Actions : 'set' (défaut) | 'list' | 'cancel'
    Accepte soit une description naturelle, soit des champs explicites
    (date, time, message).
    """
    params = parameters or {}
    action = str(params.get("action", "set") or "set").lower().strip()
    action = {"add": "set", "create": "set", "delete": "cancel",
              "remove": "cancel", "annuler": "cancel"}.get(action, action)

    if action == "list":
        return _list_reminders()
    if action == "cancel":
        identifier = str(
            params.get("task_name") or params.get("value")
            or params.get("description") or ""
        ).strip()
        # Extraire un numéro d'une phrase (« annule le rappel 2 »)
        m = re.search(r"(\d+)", identifier)
        if m and "rappel" in identifier.lower():
            identifier = m.group(1)
        return _cancel_reminder(identifier)

    # ── Action 'set' ─────────────────────────────────────────────────────
    description = params.get("description", "").strip()
    date_str = params.get("date", "").strip()
    time_str = params.get("time", "").strip()
    message = params.get("message", "Rappel").strip()

    if description and not date_str:
        local = _parse_reminder_text(description)
        if local and local.get("date") and local.get("time"):
            date_str = local["date"]
            time_str = local["time"]
            message = local.get("message", message)
        else:
            ai = _detect_reminder_via_ai(description)
            if ai:
                date_str = ai.get("date") or ""
                time_str = ai.get("time") or ""
                message = ai.get("message") or message
            else:
                return ("Je n'ai pas compris la date ou l'heure du rappel. "
                        "Précisez par exemple 'demain 15h30' ou 'dans 20 minutes'.")

    if not date_str or not time_str:
        return "J'ai besoin d'une date et d'une heure pour le rappel."

    try:
        target_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    except ValueError:
        return "Format de date ou heure invalide. Utilisez AAAA-MM-JJ et HH:MM."

    if target_dt <= datetime.now():
        return "Ce moment est déjà passé — je ne peux pas programmer un rappel dans le passé."

    os_name = _get_os()
    safe_msg = _sanitise(message)
    task_name = (
        f"JARVISReminder_{target_dt.strftime('%Y%m%d_%H%M%S')}_"
        f"{uuid.uuid4().hex[:8]}"
    )

    try:
        script_path = _write_notify_script(task_name, safe_msg, os_name, target_dt)
    except Exception as e:
        return f"Impossible de préparer le script de notification : {e}"

    try:
        if os_name == "windows":
            job = _schedule_windows(target_dt, task_name, script_path, safe_msg)
        elif os_name == "mac":
            job = _schedule_mac(target_dt, task_name, script_path)
        else:
            job = _schedule_linux(target_dt, task_name, script_path)
    except Exception as e:
        script_path.unlink(missing_ok=True)
        print(f"[Reminder] ❌ Erreur de planification : {e}")
        return "Une erreur est survenue lors de la programmation du rappel."

    if not job:
        return "Je n'ai pas pu enregistrer le rappel dans le planificateur système."

    _register_reminder(task_name, target_dt, safe_msg,
                       job.get("scheduler", ""), job.get("job_id", ""))

    if player:
        try:
            player.write_log(f"[Reminder] ✅ {date_str} {time_str} — {safe_msg[:40]}")
        except Exception:
            pass

    jours_fr = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
    mois_fr = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
               "août", "septembre", "octobre", "novembre", "décembre"]
    jour_nom = jours_fr[target_dt.weekday()]
    mois_nom = mois_fr[target_dt.month - 1]
    friendly = f"{jour_nom} {target_dt.day} {mois_nom} à {target_dt.hour}h{target_dt.minute:02d}"
    return f"✅ Rappel programmé pour {friendly} : « {safe_msg} »."


# ── Test direct ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] in ("list", "cancel"):
        print(reminder({"action": sys.argv[1],
                        "description": " ".join(sys.argv[2:])}))
    elif len(sys.argv) > 1:
        print(reminder({"description": " ".join(sys.argv[1:])}))
    else:
        print('Usage: python reminder.py "rappelle-moi demain à 15h30 d\'appeler le médecin"')
