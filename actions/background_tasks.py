"""File durable de veilles qui alimentent la proactivité d'ANO-GPT.

Les builds et la géolocalisation sont purement événementiels. Les pages web
n'offrant généralement aucun flux push, leur surveillance partage un unique
timer jusqu'au prochain contrôle et emploie ETag/Last-Modified pour éviter de
télécharger une page inchangée.
"""

from __future__ import annotations

import asyncio
import copy
import html as html_lib
import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

_STATE_PATH = Path.home() / ".config" / "jarvis" / "background_tasks.json"
_EVENTS_PATH = Path.home() / ".config" / "jarvis" / "background_events"
_GHOST_REPORTS_PATH = Path.home() / ".config" / "jarvis" / "ghost_reports"
_MIN_WEB_INTERVAL = 5 * 60
_MAX_WEB_INTERVAL = 24 * 3600


def _now() -> float:
    return time.time()


def _clean(value: Any, limit: int = 300) -> str:
    return " ".join(str(value or "").split())[:limit]


def persist_external_event(payload: dict) -> Path:
    """Spoule un événement quand ANO-GPT est arrêté."""
    if not isinstance(payload, dict) or not payload.get("topic"):
        raise ValueError("événement invalide")
    _EVENTS_PATH.mkdir(parents=True, exist_ok=True)
    event_id = f"{int(_now() * 1000)}-{uuid.uuid4().hex[:8]}"
    tmp = _EVENTS_PATH / f"{event_id}.tmp"
    target = _EVENTS_PATH / f"{event_id}.json"
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(target)
    return target


class BackgroundTaskService:
    """Registre persistant et ordonnanceur à un seul réveil."""

    def __init__(
        self,
        publish: Callable[..., bool],
        state_file: str | Path | None = None,
        on_task_update: Callable[[dict], None] | None = None,
    ) -> None:
        self._publish = publish
        self._state_file = Path(state_file) if state_file else _STATE_PATH
        self._lock = threading.RLock()
        self._tasks = self._load()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._ghost_job: asyncio.Task | None = None
        self._ghost_cancel: dict[str, threading.Event] = {}
        self._on_task_update = on_task_update

    def _notify_task(self, task: dict) -> None:
        """Diffuse l'état vers l'interface, sans rendre la tâche dépendante de Qt."""
        if self._on_task_update is None:
            return
        try:
            self._on_task_update(copy.deepcopy(task))
        except Exception:
            pass

    def _load(self) -> list[dict]:
        try:
            payload = json.loads(self._state_file.read_text(encoding="utf-8"))
            tasks = payload.get("tasks", []) if isinstance(payload, dict) else []
            loaded = [task for task in tasks if isinstance(task, dict) and task.get("id")]
            # Un processus ne survit pas au redémarrage de l'application. Une
            # mission interrompue est donc remise en file, jamais déclarée finie.
            for task in loaded:
                if task.get("kind") == "agent" and task.get("status") == "active":
                    task.setdefault("state", {})["phase"] = "queued"
            return loaded
        except Exception:
            return []

    def _save(self) -> None:
        with self._lock:
            snapshot = {"version": 1, "tasks": self._tasks}
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_file.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self._state_file)
        except Exception:
            pass

    def _signal(self) -> None:
        loop, wake = self._loop, self._wake
        if loop is not None and wake is not None:
            loop.call_soon_threadsafe(wake.set)

    @staticmethod
    def _new_task(kind: str, spec: dict, state: dict | None = None) -> dict:
        return {
            "id": f"task-{uuid.uuid4().hex[:8]}",
            "kind": kind,
            "status": "active",
            "created_at": _now(),
            "spec": spec,
            "state": state or {},
        }

    def add_price_watch(
        self,
        url: str,
        *,
        target_price: float | None = None,
        interval_seconds: int = 900,
        selector: str = "",
        label: str = "",
    ) -> dict:
        parsed = urlparse(str(url).strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("une URL http(s) complète est requise")
        if parsed.username or parsed.password:
            raise ValueError("les URL contenant des identifiants sont refusées")
        interval = max(_MIN_WEB_INTERVAL, min(_MAX_WEB_INTERVAL, int(interval_seconds)))
        spec = {
            "url": parsed.geturl(),
            "target_price": float(target_price) if target_price is not None else None,
            "interval_seconds": interval,
            "selector": _clean(selector, 150),
            "label": _clean(label, 120) or parsed.netloc,
        }
        task = self._new_task("price", spec, {"next_check": _now()})
        with self._lock:
            self._tasks.append(task)
        self._save()
        self._signal()
        self._notify_task(task)
        return dict(task)

    def add_build_wait(
        self, command_contains: str = "", *, message: str = ""
    ) -> dict:
        command_contains = _clean(command_contains, 120).casefold()
        task = self._new_task("build", {
            "command_contains": command_contains,
            "message": _clean(message) or "Le build est terminé.",
        })
        with self._lock:
            self._tasks.append(task)
        self._save()
        self._notify_task(task)
        return dict(task)

    def add_arrival_wait(
        self,
        message: str,
        *,
        lat: float | None = None,
        lon: float | None = None,
        radius_m: float = 250.0,
        label: str = "maison",
    ) -> dict:
        if lat is None or lon is None:
            from actions.proactive import _home_coordinates

            home = _home_coordinates()
            if home is None:
                raise ValueError(
                    "domicile inconnu : dis d'abord « considère cet endroit comme maison »"
                )
            lat, lon, configured_radius = home
            if radius_m == 250.0:
                radius_m = configured_radius
        lat, lon = float(lat), float(lon)
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("coordonnées invalides")
        radius_m = max(50.0, min(5000.0, float(radius_m)))
        inside: bool | None = None
        try:
            from core.geolocation import get_live_position
            from actions.proactive import _distance_m

            current = get_live_position(resolve_place=False)
            if current:
                inside = _distance_m(
                    float(current["lat"]), float(current["lon"]), lat, lon
                ) <= radius_m
        except Exception:
            pass
        task = self._new_task(
            "arrival",
            {
                "lat": lat,
                "lon": lon,
                "radius_m": radius_m,
                "label": _clean(label, 80) or "destination",
                "message": _clean(message) or f"Tu es arrivé à {label}.",
            },
            {"inside": inside},
        )
        with self._lock:
            self._tasks.append(task)
        self._save()
        self._notify_task(task)
        return dict(task)

    def add_agent_mission(
        self,
        mission: str,
        *,
        workspace: str | Path | None = None,
        timeout_minutes: int = 60,
        show_terminal: bool = False,
    ) -> dict:
        """Met une mission autonome en file sans bloquer la conversation."""
        mission = " ".join(str(mission or "").split())
        if not mission:
            raise ValueError("la mission est vide")
        if len(mission) > 8000:
            raise ValueError("la mission est trop longue (maximum 8000 caractères)")
        root = Path(workspace or Path.home()).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"dossier de travail introuvable : {root}")
        timeout_minutes = max(1, min(8 * 60, int(timeout_minutes)))
        task = self._new_task(
            "agent",
            {
                "mission": mission,
                "workspace": str(root),
                "timeout_seconds": timeout_minutes * 60,
                "show_terminal": bool(show_terminal),
            },
            {"phase": "queued"},
        )
        task["report_path"] = str(_GHOST_REPORTS_PATH / f"{task['id']}.md")
        with self._lock:
            self._tasks.append(task)
        self._save()
        self._signal()
        self._notify_task(task)
        return dict(task)

    def list_tasks(self, include_finished: bool = False) -> list[dict]:
        with self._lock:
            tasks = [dict(task) for task in self._tasks]
        if not include_finished:
            tasks = [task for task in tasks if task.get("status") == "active"]
        return tasks

    def agent_result(self, task_id: str = "") -> dict | None:
        """Dernier résultat agent, ou résultat ciblé par préfixe d'identifiant."""
        needle = str(task_id or "").strip().casefold()
        with self._lock:
            agents = [
                dict(task) for task in self._tasks
                if task.get("kind") == "agent" and task.get("status") != "active"
            ]
        if needle:
            agents = [
                task for task in agents
                if str(task.get("id") or "").casefold().startswith(needle)
            ]
        if not agents:
            return None
        return max(agents, key=lambda task: float(task.get("finished_at") or 0))

    def cancel(self, task_id: str) -> bool:
        needle = str(task_id or "").strip().casefold()
        if not needle:
            return False
        changed = False
        with self._lock:
            for task in self._tasks:
                if str(task.get("id", "")).casefold().startswith(needle):
                    if task.get("status") == "active":
                        task["status"] = "cancelled"
                        task["finished_at"] = _now()
                        task.setdefault("state", {})["phase"] = "cancelled"
                        event = self._ghost_cancel.get(str(task.get("id")))
                        if event is not None:
                            event.set()
                        changed = True
                    break
        if changed:
            self._save()
            self._signal()
            self._notify_task(task)
        return changed

    def handle_event(self, topic: str, data: dict | None = None) -> int:
        """Consomme notamment les fins de commande provenant du hook zsh."""
        topic = str(topic or "").casefold()
        data = dict(data or {})
        if topic not in {"terminal", "build"}:
            return 0
        command = _clean(data.get("command"), 200)
        duration = float(data.get("duration_s") or 0)
        exit_code = int(data.get("exit_code") or 0)
        matched: list[tuple[dict, str]] = []
        with self._lock:
            for task in self._tasks:
                if task.get("status") != "active" or task.get("kind") != "build":
                    continue
                needle = str(task.get("spec", {}).get("command_contains") or "")
                if needle and needle not in command.casefold():
                    continue
                phrase = str(task.get("spec", {}).get("message") or "Le build est terminé.")
                detail = (
                    f" Code de sortie {exit_code}, après {duration:.0f} secondes."
                    if duration else f" Code de sortie {exit_code}."
                )
                matched.append((task, phrase + detail))
                task["status"] = "completed"
                task["finished_at"] = _now()
                task.setdefault("state", {})["event"] = data
        for task, phrase in matched:
            self._publish(
                "background-build", phrase,
                dedupe_key=f"background:{task['id']}", priority=75,
                data={"task_id": task["id"], **data},
            )
        if matched:
            self._save()
            for task, _phrase in matched:
                self._notify_task(task)
        return len(matched)

    def observe_location(self, position: dict) -> int:
        try:
            from actions.proactive import _distance_m

            current_lat = float(position["lat"])
            current_lon = float(position["lon"])
        except (KeyError, TypeError, ValueError):
            return 0
        matched: list[tuple[dict, str]] = []
        dirty = False
        with self._lock:
            for task in self._tasks:
                if task.get("status") != "active" or task.get("kind") != "arrival":
                    continue
                spec, state = task.get("spec", {}), task.setdefault("state", {})
                inside = _distance_m(
                    current_lat, current_lon,
                    float(spec["lat"]), float(spec["lon"]),
                ) <= float(spec.get("radius_m") or 250)
                previous = state.get("inside")
                state["inside"] = inside
                dirty = True
                if previous is False and inside:
                    task["status"] = "completed"
                    task["finished_at"] = _now()
                    matched.append((task, str(spec.get("message") or "Tu es arrivé.")))
        for task, phrase in matched:
            self._publish(
                "background-arrival", phrase,
                dedupe_key=f"background:{task['id']}", priority=80,
                data={"task_id": task["id"]},
            )
        if dirty:
            self._save()
            for task, _phrase in matched:
                self._notify_task(task)
        return len(matched)

    async def run(self) -> None:
        """Attend le prochain instant utile; aucune boucle à fréquence fixe."""
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        await asyncio.to_thread(self._consume_spooled_events)
        # Après un redémarrage, les tâches persistantes doivent redevenir
        # visibles sans attendre une nouvelle commande de l'utilisateur.
        for task in self.list_tasks(include_finished=False):
            self._notify_task(task)
        try:
            while True:
                self._wake.clear()
                due = self._due_price_ids()
                for task_id in due:
                    await asyncio.to_thread(self._check_price, task_id)
                if self._ghost_job is None or self._ghost_job.done():
                    task_id = self._next_ghost_id()
                    if task_id:
                        self._ghost_job = asyncio.create_task(
                            self._run_ghost(task_id), name=f"ghost-agent-{task_id}"
                        )
                timeout = self._seconds_until_next_price_check()
                if self._wake.is_set():
                    continue
                try:
                    if timeout is None:
                        await self._wake.wait()
                    else:
                        await asyncio.wait_for(self._wake.wait(), timeout=max(0.1, timeout))
                except asyncio.TimeoutError:
                    pass
        finally:
            for event in self._ghost_cancel.values():
                event.set()
            if self._ghost_job is not None and not self._ghost_job.done():
                try:
                    await asyncio.wait_for(self._ghost_job, timeout=7)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass

    def _next_ghost_id(self) -> str:
        with self._lock:
            for task in self._tasks:
                if (
                    task.get("kind") == "agent"
                    and task.get("status") == "active"
                    and task.get("state", {}).get("phase", "queued") == "queued"
                ):
                    return str(task["id"])
        return ""

    async def _run_ghost(self, task_id: str) -> None:
        cancel_event = threading.Event()
        with self._lock:
            task = next((item for item in self._tasks if item.get("id") == task_id), None)
            if not task or task.get("status") != "active":
                return
            task.setdefault("state", {})["phase"] = "running"
            task["started_at"] = _now()
            spec = dict(task.get("spec", {}))
            report_path = str(task.get("report_path") or (_GHOST_REPORTS_PATH / f"{task_id}.md"))
            task["report_path"] = report_path
            self._ghost_cancel[task_id] = cancel_event
        self._save()
        self._notify_task(task)
        try:
            from core.ghost_agent import run_mission

            started = float(task.get("started_at") or _now())
            timeout_seconds = int(spec.get("timeout_seconds") or 3600)

            def on_progress(log_tail: str) -> None:
                # Une estimation est préférable à une fausse précision : le
                # journal transmis est, lui, la sortie réelle de l'agent.
                elapsed = max(0.0, _now() - started)
                estimated = min(95, max(5, int(elapsed * 100 / max(1, timeout_seconds))))
                with self._lock:
                    state = task.setdefault("state", {})
                    state["log_tail"] = str(log_tail)[-8_000:]
                    state["progress"] = estimated
                self._notify_task(task)

            result = await asyncio.to_thread(
                run_mission,
                str(spec.get("mission") or ""),
                str(spec.get("workspace") or Path.home()),
                report_path,
                timeout_seconds=timeout_seconds,
                cancelled=cancel_event.is_set,
                progress=on_progress,
                show_terminal=bool(spec.get("show_terminal", False)),
            )
            with self._lock:
                if task.get("status") != "cancelled":
                    task["status"] = result.status
                    task["finished_at"] = _now()
                    task["summary"] = result.summary
                    task["report_path"] = result.report_path
                    task.setdefault("state", {})["phase"] = result.status
                    task["state"]["exit_code"] = result.exit_code
                    task["state"]["changed_files"] = list(result.changed_files)
                    task["state"]["progress"] = 100 if result.status == "completed" else 0
            self._notify_task(task)
            if result.status == "completed":
                changed = [item.split(": ", 1)[-1] for item in result.changed_files]
                file_note = (
                    " Fichiers détectés : " + ", ".join(changed[:4])
                    + (f", et {len(changed) - 4} autre(s)." if len(changed) > 4 else ".")
                    if changed else " Aucun fichier modifié n'a été détecté."
                )
                phrase = f"Patron, la mission fantôme est terminée. {result.summary}{file_note}"
                priority = 70
            elif result.status == "timed_out":
                phrase = f"Patron, la mission fantôme a dépassé sa limite. {result.summary}"
                priority = 65
            elif result.status == "failed":
                phrase = f"Patron, la mission fantôme a échoué. {result.summary}"
                priority = 65
            else:
                phrase = ""
            if phrase:
                self._publish(
                    "ghost-agent", phrase,
                    dedupe_key=f"background:{task_id}", priority=priority,
                    data={"task_id": task_id, "report_path": result.report_path},
                )
        except Exception as exc:
            with self._lock:
                if task.get("status") != "cancelled":
                    task["status"] = "failed"
                    task["finished_at"] = _now()
                    task["summary"] = _clean(exc, 300)
                    task.setdefault("state", {})["phase"] = "failed"
            self._notify_task(task)
            if task.get("status") != "cancelled":
                self._publish(
                    "ghost-agent",
                    f"Patron, la mission fantôme n'a pas pu démarrer. {_clean(exc, 220)}",
                    dedupe_key=f"background:{task_id}", priority=65,
                    data={"task_id": task_id, "report_path": report_path},
                )
        finally:
            with self._lock:
                self._ghost_cancel.pop(task_id, None)
            self._save()
            self._signal()

    def _consume_spooled_events(self) -> None:
        try:
            paths = sorted(_EVENTS_PATH.glob("*.json"))
        except OSError:
            return
        for path in paths:
            claimed = path.with_suffix(".processing")
            try:
                path.replace(claimed)
                payload = json.loads(claimed.read_text(encoding="utf-8"))
                topic = str(payload.get("topic") or "").casefold()
                data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
                matched = self.handle_event(topic, data)
                if not matched and topic == "terminal":
                    duration = float(data.get("duration_s") or 0)
                    command = _clean(data.get("command") or "la commande", 100)
                    code = int(data.get("exit_code") or 0)
                    self._publish(
                        "terminal",
                        str(payload.get("message") or (
                            f"La commande {command} est terminée après "
                            f"{duration:.0f} secondes, avec le code {code}."
                        )),
                        dedupe_key=str(payload.get("dedupe_key") or f"terminal:{command}"),
                        priority=int(payload.get("priority") or 50),
                        data=data,
                    )
                claimed.unlink(missing_ok=True)
            except Exception:
                try:
                    claimed.replace(path)
                except OSError:
                    pass

    def _due_price_ids(self) -> list[str]:
        now = _now()
        with self._lock:
            return [
                str(task["id"])
                for task in self._tasks
                if task.get("status") == "active"
                and task.get("kind") == "price"
                and float(task.get("state", {}).get("next_check") or 0) <= now
            ]

    def _seconds_until_next_price_check(self) -> float | None:
        with self._lock:
            times = [
                float(task.get("state", {}).get("next_check") or 0)
                for task in self._tasks
                if task.get("status") == "active" and task.get("kind") == "price"
            ]
        return max(0.0, min(times) - _now()) if times else None

    def _check_price(self, task_id: str) -> None:
        with self._lock:
            task = next((t for t in self._tasks if t.get("id") == task_id), None)
            if not task or task.get("status") != "active":
                return
            spec = dict(task.get("spec", {}))
            state = task.setdefault("state", {})
            headers = {"User-Agent": "ANO-GPT price watcher/1.0"}
            if state.get("etag"):
                headers["If-None-Match"] = state["etag"]
            if state.get("last_modified"):
                headers["If-Modified-Since"] = state["last_modified"]
        interval = int(spec.get("interval_seconds") or 900)
        try:
            import requests

            response = requests.get(spec["url"], headers=headers, timeout=15)
            if response.status_code == 304:
                with self._lock:
                    state["next_check"] = _now() + interval
                    state["last_checked"] = _now()
                    state.pop("last_error", None)
                self._save()
                self._notify_task(task)
                return
            response.raise_for_status()
            price, currency = extract_price(response.text, spec.get("selector", ""))
            if price is None:
                raise ValueError("aucun prix structuré trouvé dans la page")
            with self._lock:
                baseline = state.get("baseline_price")
                target = spec.get("target_price")
                state.update({
                    "current_price": price,
                    "currency": currency,
                    "last_checked": _now(),
                    "next_check": _now() + interval,
                    "etag": response.headers.get("ETag", ""),
                    "last_modified": response.headers.get("Last-Modified", ""),
                })
                state.pop("last_error", None)
                if baseline is None:
                    state["baseline_price"] = price
                dropped = baseline is not None and price < float(baseline)
                reached = target is not None and price <= float(target)
                if dropped or reached:
                    task["status"] = "completed"
                    task["finished_at"] = _now()
            if dropped or reached:
                unit = f" {currency}" if currency else ""
                label = spec.get("label") or spec.get("url")
                self._publish(
                    "background-price",
                    f"Le prix de {label} est descendu à {price:g}{unit}.",
                    dedupe_key=f"background:{task_id}", priority=65,
                    data={"task_id": task_id, "url": spec["url"], "price": price},
                )
        except Exception as exc:
            with self._lock:
                state["last_checked"] = _now()
                state["next_check"] = _now() + interval
                state["last_error"] = _clean(exc, 180)
        self._save()
        self._notify_task(task)


def extract_price(document: str, selector: str = "") -> tuple[float | None, str]:
    """Extrait d'abord les données e-commerce structurées, puis le texte."""
    document = str(document or "")
    sample = document
    if selector:
        try:
            from bs4 import BeautifulSoup

            node = BeautifulSoup(document, "html.parser").select_one(selector)
            if node is not None:
                sample = node.get("content") or node.get_text(" ", strip=True)
        except Exception:
            pass
    currency_match = re.search(
        r'(?i)(?:priceCurrency["\']?\s*[:=]\s*["\']|itemprop=["\']priceCurrency["\'][^>]*content=["\'])([A-Z]{3})',
        document,
    )
    currency = currency_match.group(1).upper() if currency_match else ""
    patterns = [
        r'(?i)["\']price["\']\s*:\s*["\']?([0-9][0-9\s.,]*)',
        r'(?i)itemprop=["\']price["\'][^>]*(?:content|value)=["\']([0-9][0-9\s.,]*)',
        r'(?i)(?:property|name)=["\'](?:product:price:amount|price)["\'][^>]*content=["\']([0-9][0-9\s.,]*)',
    ]
    if selector:
        patterns.insert(0, r'([0-9][0-9\s.,]*)')
    for pattern in patterns:
        match = re.search(pattern, sample if selector else document)
        if match:
            value = _parse_number(match.group(1))
            if value is not None:
                return value, currency
    # Dernier repli volontairement borné aux symboles monétaires : prendre le
    # premier nombre libre d'une page confondrait prix, note, stock et année.
    fallback = re.search(
        r'(?i)(?:[$€£₽₹¥]|USD|EUR|GBP|GNF|XOF)\s*([0-9][0-9\s.,]*)|'
        r'([0-9][0-9\s.,]*)\s*(?:[$€£₽₹¥]|USD|EUR|GBP|GNF|XOF)',
        html_lib.unescape(document),
    )
    if fallback:
        return _parse_number(fallback.group(1) or fallback.group(2)), currency
    return None, currency


def _parse_number(raw: str) -> float | None:
    text = re.sub(r"\s+", "", raw or "")
    if not text:
        return None
    if "," in text and "." in text:
        decimal = "," if text.rfind(",") > text.rfind(".") else "."
        thousands = "." if decimal == "," else ","
        text = text.replace(thousands, "").replace(decimal, ".")
    elif "," in text:
        parts = text.split(",")
        text = "".join(parts) if len(parts[-1]) == 3 else "".join(parts[:-1]) + "." + parts[-1]
    elif text.count(".") > 1:
        parts = text.split(".")
        text = "".join(parts) if len(parts[-1]) == 3 else "".join(parts[:-1]) + "." + parts[-1]
    try:
        return float(text)
    except ValueError:
        return None


def format_tasks(tasks: list[dict]) -> str:
    if not tasks:
        return "Aucune tâche de fond active."
    lines = []
    for task in tasks:
        spec = task.get("spec", {})
        kind = task.get("kind")
        if kind == "price":
            detail = f"prix de {spec.get('label') or spec.get('url')}"
        elif kind == "build":
            detail = "fin du build" + (
                f" contenant « {spec.get('command_contains')} »"
                if spec.get("command_contains") else " suivant"
            )
        elif kind == "arrival":
            detail = f"arrivée à {spec.get('label') or 'destination'}"
        else:
            phase = task.get("state", {}).get("phase", "queued")
            mission = _clean(spec.get("mission"), 90)
            detail = f"mission fantôme [{phase}] : {mission}"
        status = str(task.get("status") or "active")
        lines.append(f"{task.get('id')} [{status}] — {detail}")
    heading = (
        "Tâches de fond actives :"
        if all(task.get("status") == "active" for task in tasks)
        else "Historique des tâches de fond :"
    )
    return heading + "\n" + "\n".join(lines)


def format_agent_result(task: dict | None) -> str:
    if not task:
        return "Aucun résultat de mission Agent Fantôme trouvé."
    state = task.get("state", {})
    changes = state.get("changed_files") if isinstance(state, dict) else []
    lines = [
        f"Mission {task.get('id')} — statut {task.get('status', 'inconnu')}.",
        str(task.get("summary") or "Aucun résumé disponible."),
    ]
    if changes:
        lines.append("Fichiers vérifiés : " + ", ".join(str(item) for item in changes[:12]))
    else:
        lines.append("Aucun changement de fichier détecté.")
    if task.get("report_path"):
        lines.append(f"Rapport complet : {task['report_path']}")
    return "\n".join(lines)
