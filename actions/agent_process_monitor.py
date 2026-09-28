"""État réel des agents CLI et veille de fin de tour ou de processus."""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil

from core import action_kit as kit


_NAMES = {"codex": "codex", "claude": "claude", "claude code": "claude",
          "agy": "agy", "antigravity": "agy", "antigravity cli": "agy"}
_WATCHES: dict[tuple[str, int, float], threading.Event] = {}
_LOCK = threading.Lock()


def _agents(kind: str) -> list[dict[str, Any]]:
    found = []
    owner = psutil.Process().username()
    for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time", "username"]):
        try:
            if proc.info["username"] != owner:
                continue
            argv = proc.info["cmdline"] or []
            if not argv:
                continue
            program = Path(argv[0]).name.casefold()
            if program in {"python", "python3", "node"} and len(argv) > 1:
                program = Path(argv[1]).name.casefold()
            name = "agy" if program in {"agy", "antigravity"} else program
            if kind == "all" and name not in {"codex", "claude", "agy"}:
                continue
            if kind != "all" and name != kind:
                continue
            if not proc.terminal() or "app-server" in argv[1:3]:
                continue
            found.append({"name": name, "pid": proc.pid,
                          "started": float(proc.info["create_time"]),
                          "cwd": proc.cwd(), "tty": proc.terminal()})
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
    return sorted(found, key=lambda item: (item["name"], item["pid"]))


def _created_at(record: dict[str, Any]) -> float:
    try:
        return datetime.fromisoformat(str(record.get("timestamp", "")).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0


def _session_file(agent: dict[str, Any]) -> Path | None:
    name = agent["name"]
    root = Path.home() / (".codex/sessions" if name == "codex" else ".claude/projects")
    if name not in {"codex", "claude"} or not root.is_dir():
        return None
    peers = [item for item in _agents(name) if item["cwd"] == agent["cwd"]]
    if len(peers) != 1:
        return None
    candidates = []
    try:
        files = sorted(root.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:30]
    except OSError:
        return None
    for path in files:
        try:
            with path.open(encoding="utf-8", errors="replace") as stream:
                first = None
                for _ in range(128):
                    line = stream.readline()
                    if not line:
                        break
                    row = json.loads(line)
                    meta = row.get("payload", row) if name == "codex" else row
                    if meta.get("cwd") and row.get("timestamp"):
                        first = row
                        break
            if first is None:
                continue
            meta = first.get("payload", first) if name == "codex" else first
            if str(meta.get("cwd") or "") != agent["cwd"]:
                continue
            timestamp = _created_at(first)
            if timestamp >= agent["started"] - 30:
                candidates.append((timestamp, path))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return max(candidates, default=(None, None))[1]


def _turn_state(path: Path, name: str, cursor: dict[str, float] | None = None) -> tuple[str, float]:
    """Ne lit que les marqueurs, jamais le contenu privé des conversations."""
    last_request = float(cursor.get("request", 0.0)) if cursor is not None else 0.0
    last_finish = float(cursor.get("finish", 0.0)) if cursor is not None else 0.0
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            if cursor is not None:
                offset = int(cursor.get("offset", 0))
                if path.stat().st_size >= offset:
                    stream.seek(offset)
                else:
                    last_request = last_finish = 0.0
            while True:
                start = stream.tell()
                line = stream.readline()
                if not line:
                    break
                if not line.endswith("\n"):
                    stream.seek(start)
                    break
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                stamp = _created_at(row)
                if name == "codex":
                    payload = row.get("payload") or {}
                    if row.get("type") == "response_item" and payload.get("role") == "user":
                        last_request = max(last_request, stamp)
                    if row.get("type") == "event_msg" and payload.get("type") == "task_complete":
                        last_finish = max(last_finish, stamp)
                elif name == "claude":
                    if row.get("type") == "user" and "toolUseResult" not in row and not row.get("isMeta"):
                        last_request = max(last_request, stamp)
                    if row.get("type") == "system" and row.get("subtype") == "turn_duration":
                        last_finish = max(last_finish, stamp)
            if cursor is not None:
                cursor.update(offset=stream.tell(), request=last_request, finish=last_finish)
    except OSError:
        return "unknown", 0.0
    if last_request and last_finish >= last_request:
        return "finished", last_finish
    if last_request:
        return "working", last_request
    return "unknown", 0.0


def _alive(agent: dict[str, Any]) -> bool:
    try:
        proc = psutil.Process(agent["pid"])
        return abs(proc.create_time() - agent["started"]) < 0.01 and proc.is_running() \
            and proc.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def _line(agent: dict[str, Any], path: Path | None,
          cursor: dict[str, float] | None = None) -> tuple[str, str]:
    label = f"{agent['name']} (PID {agent['pid']}, terminal {agent['tty']})"
    if not _alive(agent):
        return "exited", f"{label} : processus terminé. Son résultat n'est pas vérifiable par le seul processus."
    if path:
        state, stamp = _turn_state(path, agent["name"], cursor)
        when = datetime.fromtimestamp(stamp).strftime("%H:%M:%S") if stamp else ""
        if state == "finished":
            return state, f"{label} : dernier tour terminé à {when} ; le terminal reste ouvert."
        if state == "working":
            return state, f"{label} : un tour a commencé à {when} ; aucune fin enregistrée."
    return "unknown", f"{label} : processus actif ; impossible de déduire si sa tâche est finie."


def _notify(player: Any, speak: Any, message: str) -> None:
    if player is not None and callable(getattr(player, "show_card", None)):
        try:
            player.show_card("info", "Surveillance des agents", message)
        except Exception:
            pass
    if callable(speak):
        try:
            speak(message)
        except Exception:
            pass


def _watch(agent: dict[str, Any], path: Path | None, stop: threading.Event,
           player: Any, speak: Any) -> None:
    key = (agent["name"], agent["pid"], agent["started"])
    cursor: dict[str, float] = {}
    try:
        while not stop.wait(1.5):
            state, message = _line(agent, path, cursor)
            if state in {"finished", "exited"}:
                _notify(player, speak, message)
                break
    finally:
        with _LOCK:
            _WATCHES.pop(key, None)


@kit.action("agent_process_monitor")
def agent_process_monitor(parameters: dict | None = None, player=None, speak=None, **_kwargs) -> str:
    params = parameters or {}
    raw = str(params.get("agent") or "all").strip().casefold()
    kind = "all" if raw in {"", "all", "tous", "tout"} else _NAMES.get(raw, raw)
    if kind != "all" and not re.fullmatch(r"[a-z0-9_.+-]{1,48}", kind):
        return "Nom de processus invalide. Donnez le nom exact du programme en terminal."
    action = str(params.get("action") or "status").strip().casefold()
    if action in {"stop", "cancel", "arrete", "arrête"}:
        with _LOCK:
            keys = [key for key in _WATCHES if kind == "all" or key[0] == kind]
            for key in keys:
                _WATCHES[key].set()
        return f"Surveillance arrêtée pour {len(keys)} session(s)."
    agents = _agents(kind)
    if not agents:
        return (f"Aucun processus terminal {kind if kind != 'all' else 'Codex, Claude Code ou agy'} actif. "
                "Sans suivi préalable, je ne peux pas confirmer qu'une tâche passée a réussi.")
    lines = []
    for agent in agents:
        path = _session_file(agent)
        state, message = _line(agent, path)
        lines.append(message)
        if action in {"watch", "surveille", "notify"} and state not in {"finished", "exited"}:
            key = (agent["name"], agent["pid"], agent["started"])
            with _LOCK:
                if key not in _WATCHES:
                    stop = _WATCHES[key] = threading.Event()
                    threading.Thread(target=_watch, args=(agent, path, stop, player, speak),
                                     name=f"agent-watch-{agent['pid']}", daemon=True).start()
            lines.append("Surveillance active : je signalerai une fin enregistrée ou la sortie du processus.")
    return "\n".join(lines)
