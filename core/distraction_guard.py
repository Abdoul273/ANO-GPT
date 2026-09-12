"""Gardien de concentration local, explicable et entièrement réversible.

L'observation ne démarre qu'après une commande explicite. Les titres de
fenêtres servent à calculer une empreinte en mémoire et ne sont jamais écrits
sur disque. Le blocage de flux exige une URL CDP exacte : en mode dégradé,
aucune fenêtre n'est fermée sur la base de son seul titre.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from core.browser_policy import open_chrome
from collections import deque
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from actions import browser_tab_control
from core.hypr_focus import active_window


_DISTRACTING_URLS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("YouTube Shorts", re.compile(r"^https?://(?:www\.)?youtube\.com/shorts(?:/|\?|$)", re.I)),
    ("Instagram Reels", re.compile(r"^https?://(?:www\.)?instagram\.com/reels?(?:/|\?|$)", re.I)),
    ("Facebook Reels", re.compile(r"^https?://(?:www\.)?facebook\.com/reels?(?:/|\?|$)", re.I)),
    ("X/Twitter", re.compile(r"^https?://(?:www\.)?(?:x|twitter)\.com/(?:home|explore)(?:/|\?|$)", re.I)),
    ("TikTok", re.compile(r"^https?://(?:www\.)?tiktok\.com/(?:foryou|following)?(?:\?|$)", re.I)),
)


def distracting_feed(url: str) -> str | None:
    """Nom du flux interdit, uniquement pour une URL HTTP(S) non ambiguë."""
    clean = str(url or "").strip()
    if not clean:
        return None
    for label, pattern in _DISTRACTING_URLS:
        if pattern.search(clean):
            return label
    return None


def _default_state_path() -> Path:
    return Path.home() / ".config" / "jarvis" / "focus_guard.json"


class DistractionGuard:
    WINDOW_SECONDS = 10 * 60
    MIN_NO_PROGRESS_SECONDS = 5 * 60
    STABLE_PROGRESS_SECONDS = 8 * 60
    INTERVENTION_COOLDOWN_SECONDS = 10 * 60

    def __init__(
        self,
        state_path: str | Path | None = None,
        *,
        clock: Callable[[], float] = time.time,
        window_provider: Callable[[], tuple[str, str] | None] = active_window,
        tab_provider: Callable[[], list[dict]] = browser_tab_control.get_open_tabs,
        tab_closer: Callable[[dict], bool] = browser_tab_control._close_one_tab,
        url_opener: Callable[[str], Any] = open_chrome,
    ) -> None:
        self.path = Path(state_path) if state_path else _default_state_path()
        self._clock = clock
        self._window_provider = window_provider
        self._tab_provider = tab_provider
        self._tab_closer = tab_closer
        self._url_opener = url_opener
        self._lock = threading.RLock()
        self._events: deque[tuple[float, str]] = deque(maxlen=240)
        self._last_context = ""
        self._stable_since = 0.0
        self._last_intervention = 0.0
        self._flow_deferrals = 0
        self._blocked_urls: list[str] = []
        self._state = self._load()

    def _defaults(self) -> dict[str, Any]:
        return {
            "active": False,
            "goal": "",
            "phase": "idle",
            "phase_started": 0.0,
            "work_minutes": 50,
            "break_minutes": 10,
            "switch_threshold": 12,
            "blocking_enabled": True,
            "last_progress": 0.0,
            "blocked_count": 0,
        }

    def _load(self) -> dict[str, Any]:
        state = self._defaults()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for key in state:
                    if key in raw:
                        state[key] = raw[key]
        except Exception:
            pass
        # Une session morte depuis des heures ne reprend pas en bloquant des
        # pages au prochain démarrage. La reprise n'est permise que 4 h.
        now = self._clock()
        if state["active"] and now - float(state.get("phase_started") or 0) > 4 * 3600:
            state.update(active=False, phase="idle")
        return state

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._state, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except Exception:
            pass

    @property
    def active(self) -> bool:
        return bool(self._state.get("active"))

    def _fingerprint(self, window: tuple[str, str]) -> str:
        # Le sel par session empêche de comparer ces empreintes entre deux
        # lancements ; aucune donnée de navigation brute n'est persistée.
        salt = str(self._state.get("phase_started") or "idle")
        raw = f"{salt}\0{window[0].casefold()}\0{window[1].casefold()}"
        return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:16]

    def observe_window(self, window: tuple[str, str] | None, now: float | None = None) -> dict:
        now = self._clock() if now is None else float(now)
        if not self.active or not window or not any(window):
            return self.metrics(now)
        fingerprint = self._fingerprint(window)
        with self._lock:
            if not self._last_context:
                self._last_context = fingerprint
                self._stable_since = now
            elif fingerprint != self._last_context:
                self._events.append((now, fingerprint))
                self._last_context = fingerprint
                self._stable_since = now
            elif (
                self._stable_since
                and now - self._stable_since >= self.STABLE_PROGRESS_SECONDS
                and now - float(self._state.get("last_progress") or 0) >= self.STABLE_PROGRESS_SECONDS
            ):
                # Une longue période stable est une preuve raisonnable de flow,
                # sans inspecter le contenu de la fenêtre.
                self._state["last_progress"] = now
                self._save()
            self._prune(now)
            return self.metrics(now)

    def _prune(self, now: float) -> None:
        cutoff = now - self.WINDOW_SECONDS
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def metrics(self, now: float | None = None) -> dict[str, Any]:
        now = self._clock() if now is None else float(now)
        self._prune(now)
        switches = len(self._events)
        unique = len({fingerprint for _, fingerprint in self._events})
        threshold = max(4, int(self._state.get("switch_threshold") or 12))
        score = min(100, round((switches / threshold) * 70 + (unique / 8) * 30))
        no_progress = now - float(self._state.get("last_progress") or now)
        return {
            "switches_10m": switches,
            "unique_contexts_10m": unique,
            "dispersion_score": score,
            "dispersed": switches >= threshold and no_progress >= self.MIN_NO_PROGRESS_SECONDS,
            "stable_seconds": max(0, round(now - self._stable_since)) if self._stable_since else 0,
            "no_progress_seconds": max(0, round(no_progress)),
        }

    def _block_feeds(self) -> list[dict[str, str]]:
        if not self.active or not self._state.get("blocking_enabled"):
            return []
        blocked = []
        try:
            tabs = self._tab_provider() or []
        except Exception:
            return []
        for tab in tabs:
            url = str(tab.get("url") or "")
            label = distracting_feed(url)
            # Sans CDP, le « tab » est une fenêtre entière et son URL est
            # inconnue : ne jamais la fermer par déduction sur le titre.
            if not label or tab.get("source") != "cdp":
                continue
            # L'inventaire CDP peut prendre une seconde. L'utilisateur a pu
            # dire « stop » entre-temps : revérifier juste avant l'action rend
            # l'arrêt prioritaire même dans cette course rare.
            with self._lock:
                if not self.active or not self._state.get("blocking_enabled"):
                    break
            try:
                closed = bool(self._tab_closer(tab))
            except Exception:
                closed = False
            if closed:
                safe_url = url[:500]
                if safe_url not in self._blocked_urls:
                    self._blocked_urls.append(safe_url)
                    del self._blocked_urls[:-20]
                self._state["blocked_count"] = int(self._state.get("blocked_count") or 0) + 1
                blocked.append({"label": label, "url": safe_url})
        if blocked:
            self._save()
        return blocked

    def poll(self) -> list[dict[str, Any]]:
        """Une sonde : observation, blocage précis et transitions de pause."""
        now = self._clock()
        events: list[dict[str, Any]] = []
        try:
            window = self._window_provider()
        except Exception:
            window = None
        metrics = self.observe_window(window, now)
        blocked = self._block_feeds()
        with self._lock:
            still_active = self.active
        if not still_active:
            return []
        if blocked:
            labels = ", ".join(dict.fromkeys(item["label"] for item in blocked))
            events.append({
                "key": f"focus-block-{int(now // 60)}",
                "priority": 70,
                "message": f"Mode Focus : flux infini bloqué ({labels}). Retourne à ton objectif : {self._state['goal']}.",
            })

        if metrics["dispersed"] and now - self._last_intervention >= self.INTERVENTION_COOLDOWN_SECONDS:
            self._last_intervention = now
            events.append({
                "key": f"focus-dispersion-{int(now // 600)}",
                "priority": 75,
                "message": (
                    f"Tu as changé de contexte {metrics['switches_10m']} fois en dix minutes "
                    f"sans progression signalée. Reviens à « {self._state['goal']} » ou dis "
                    "« pause focus » si tu as besoin de souffler."
                ),
            })

        if self._state.get("phase") == "focus":
            due = float(self._state["phase_started"]) + int(self._state["work_minutes"]) * 60
            if now >= due:
                stable = metrics["stable_seconds"] >= 8 * 60 and not metrics["dispersed"]
                if stable and self._flow_deferrals < 1:
                    self._flow_deferrals += 1
                    self._state["phase_started"] = now - (int(self._state["work_minutes"]) - 5) * 60
                    self._save()
                    events.append({
                        "key": f"focus-flow-{int(now // 300)}",
                        "priority": 45,
                        "message": "Tu es stable et concentré : je protège encore cinq minutes de flow avant la pause.",
                    })
                else:
                    self._state.update(phase="break", phase_started=now)
                    self._flow_deferrals = 0
                    self._save()
                    events.append({
                        "key": f"focus-break-{int(now // 60)}",
                        "priority": 80,
                        "message": f"Pause intelligente de {self._state['break_minutes']} minutes : lève-toi, regarde au loin et évite un autre écran.",
                    })
        elif self._state.get("phase") == "break":
            due = float(self._state["phase_started"]) + int(self._state["break_minutes"]) * 60
            if now >= due:
                self._state.update(phase="focus", phase_started=now, last_progress=now)
                self._events.clear()
                self._save()
                events.append({
                    "key": f"focus-resume-{int(now // 60)}",
                    "priority": 80,
                    "message": f"Pause terminée. Reprends doucement ton objectif : {self._state['goal']}.",
                })
        return events

    def control(self, parameters: dict | None) -> str:
        args = dict(parameters or {})
        action = str(args.get("action") or "status").strip().casefold().replace(" ", "_")
        now = self._clock()
        with self._lock:
            if action in {"start", "demarrer", "activer"}:
                goal = " ".join(str(args.get("goal") or "").split())[:240]
                if not goal:
                    return "Indique l'objectif concret de la session Focus."
                try:
                    work = max(15, min(120, int(args.get("work_minutes") or 50)))
                    pause = max(3, min(30, int(args.get("break_minutes") or 10)))
                    threshold = max(4, min(30, int(args.get("switch_threshold") or 12)))
                except (TypeError, ValueError):
                    return "Durée ou seuil invalide : utilise des nombres entiers."
                self._state.update(
                    active=True, goal=goal, phase="focus", phase_started=now,
                    work_minutes=work, break_minutes=pause,
                    switch_threshold=threshold,
                    blocking_enabled=args.get("block_feeds", True) is not False,
                    last_progress=now, blocked_count=0,
                )
                self._blocked_urls.clear()
                self._events.clear()
                self._last_context = ""
                self._stable_since = now
                self._last_intervention = 0.0
                self._flow_deferrals = 0
                self._save()
                block = "activé" if self._state["blocking_enabled"] else "désactivé"
                return (
                    f"Mode Focus actif pendant {work} minutes sur « {goal} ». "
                    f"Pause prévue : {pause} minutes. Blocage des flux : {block} "
                    "(filtrage URL exact lorsque le navigateur expose CDP)."
                )

            if action in {"stop", "arreter", "desactiver"}:
                was_active = self.active
                self._state.update(active=False, phase="idle")
                self._events.clear()
                self._save()
                return "Mode Focus arrêté ; les flux ne sont plus bloqués." if was_active else "Le mode Focus était déjà arrêté."

            if action in {"progress", "avance", "checkpoint"}:
                if not self.active:
                    return "Aucune session Focus active."
                self._state["last_progress"] = now
                self._events.clear()
                self._save()
                note = " ".join(str(args.get("note") or "").split())[:160]
                return "Progression enregistrée" + (f" : {note}." if note else ".")

            if action in {"break", "pause"}:
                if not self.active:
                    return "Aucune session Focus active."
                self._state.update(phase="break", phase_started=now)
                self._save()
                return f"Pause de {self._state['break_minutes']} minutes démarrée."

            if action in {"resume", "reprendre"}:
                if not self.active:
                    return "Aucune session Focus active."
                self._state.update(phase="focus", phase_started=now, last_progress=now)
                self._events.clear()
                self._save()
                return f"Session reprise sur « {self._state['goal']} »."

            if action in {"block", "bloquer"}:
                if not self.active:
                    return "Démarre d'abord une session Focus."
                self._state["blocking_enabled"] = True
                self._save()
                return "Blocage précis des flux infinis activé."

            if action in {"allow", "autoriser", "debloquer"}:
                self._state["blocking_enabled"] = False
                self._save()
                return "Flux infinis autorisés ; la session Focus reste active."

            if action in {"restore", "restaurer"}:
                urls = list(dict.fromkeys(self._blocked_urls))
                if not urls:
                    return "Aucun onglet bloqué à restaurer."
                restored = 0
                for url in urls:
                    try:
                        restored += bool(self._url_opener(url))
                    except Exception:
                        pass
                if restored:
                    self._blocked_urls.clear()
                return f"{restored}/{len(urls)} onglet(s) restauré(s)."

            metrics = self.metrics(now)
            if not self.active:
                return "Mode Focus inactif."
            elapsed = max(0, int((now - float(self._state["phase_started"])) // 60))
            duration = self._state["work_minutes"] if self._state["phase"] == "focus" else self._state["break_minutes"]
            remaining = max(0, int(duration) - elapsed)
            return (
                f"Mode Focus {self._state['phase']} — objectif : {self._state['goal']} · "
                f"environ {remaining} min restantes · {metrics['switches_10m']} changements/10 min · "
                f"dispersion {metrics['dispersion_score']}/100 · "
                f"{self._state['blocked_count']} flux bloqué(s)."
            )
