"""Minuteurs nommés, simultanés et persistants."""
from __future__ import annotations

import asyncio, json, threading, uuid
from datetime import datetime, timedelta
from pathlib import Path


class TimerService:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or Path.home() / ".config" / "jarvis" / "timers.json")
        self._lock = threading.RLock()
        self._timers = self._load()

    def _load(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return [x for x in data if isinstance(x, dict) and x.get("due")]
        except Exception: return []

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._timers, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except Exception: pass

    def create(self, name: str, seconds: int, now: datetime | None = None) -> dict:
        now = now or datetime.now(); seconds = max(1, min(int(seconds), 7 * 86400))
        item = {"id": uuid.uuid4().hex[:8], "name": (name or "Minuteur").strip()[:60],
                "due": (now + timedelta(seconds=seconds)).isoformat(), "seconds": seconds}
        with self._lock: self._timers.append(item); self._save()
        return item

    def active(self, now: datetime | None = None) -> list[dict]:
        now = now or datetime.now()
        with self._lock:
            return [dict(t, remaining=max(0, int((datetime.fromisoformat(t["due"]) - now).total_seconds())))
                    for t in self._timers if datetime.fromisoformat(t["due"]) > now]

    def cancel(self, value: str) -> bool:
        value = str(value or "").casefold().strip()
        with self._lock:
            old = len(self._timers)
            self._timers = [t for t in self._timers if value not in (t["id"].casefold(), t["name"].casefold())]
            if len(self._timers) != old: self._save(); return True
        return False

    def due(self, now: datetime | None = None) -> list[dict]:
        now = now or datetime.now(); due = []
        with self._lock:
            keep = []
            for t in self._timers:
                (due if datetime.fromisoformat(t["due"]) <= now else keep).append(t)
            if len(keep) != len(self._timers): self._timers = keep; self._save()
        return due

    async def watch(self, on_tick, on_due) -> None:
        while True:
            await on_tick(self.active())
            for item in self.due(): await on_due(item)
            await asyncio.sleep(1)
