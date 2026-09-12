"""
proactive.py — ProactiveEngine ultra‑réaliste, version corrigée et renforcée.
Contexte enrichi, détection de la langue, génération naturelle via Gemini.
Décide de façon autonome s'il y a quelque chose d'utile à dire.

Corrections par rapport à l'ancienne version :
    - `_get_api_key` levait une KeyError si la clé était absente :
      accès via .get() + try/except ;
    - le calcul du silence dans `build_prompt` utilisait `_last_triggered`
      au lieu du silence réel de l'utilisateur : la durée est désormais
      passée explicitement ;
    - l'import de `memory.memory_manager` n'était pas gardé : un module
      absent faisait crasher le moteur → repli json.dumps ;
    - le cooldown n'était pas persisté : au redémarrage, l'assistant
      pouvait reprendre la parole immédiatement → persistance horodatée ;
    - aucune plage de silence : l'assistant pouvait interpeller la nuit →
      heures calmes configurables (défaut 23h–8h), surmontables via force ;
    - modèle harmonisé avec les autres modules (gemini-flash-lite-latest) ;
    - helpers mémoire rendus tolérants aux structures malformées.
"""
import asyncio
from core import action_kit as kit
import json
import math
import re
import shutil
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from core.live_model_policy import FAST_MODEL

_STATE_PATH = Path.home() / ".config" / "jarvis" / "proactive_state.json"
_EVENT_STATE_PATH = Path.home() / ".config" / "jarvis" / "proactive_events.json"


@dataclass(frozen=True)
class ProactiveEvent:
    """Une information déjà formulée, en attente d'un moment calme."""

    topic: str
    message: str
    dedupe_key: str = ""
    priority: int = 50
    created_at: float = field(default_factory=time.time)
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return self.dedupe_key or self.topic


class ProactiveService:
    """Bus d'événements proactifs, persistant et sans boucle de sondage.

    Les producteurs peuvent appeler :meth:`publish` depuis n'importe quel fil.
    Le consommateur asyncio attend :meth:`next_event`; quand un portier bloque
    la parole, :meth:`defer` rend l'événement plus tard sans boucle active.
    """

    TOPIC_COOLDOWN_SECONDS = 3600
    MAX_PENDING = 100

    def __init__(self, state_file: str | Path | None = None) -> None:
        self._state_file = Path(state_file) if state_file else _EVENT_STATE_PATH
        self._lock = threading.RLock()
        self._pending: deque[ProactiveEvent] = deque(maxlen=self.MAX_PENDING)
        self._held: dict[str, ProactiveEvent] = {}
        self._queued_keys: set[str] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.PriorityQueue | None = None
        self._sequence = 0
        state = self._load_state()
        self._silent = bool(state.get("silent", False))
        delivered = state.get("delivered", {})
        self._delivered = {
            str(key): float(value)
            for key, value in delivered.items()
            if isinstance(value, (int, float))
        } if isinstance(delivered, dict) else {}
        self._was_home: bool | None = state.get("was_home") \
            if isinstance(state.get("was_home"), bool) else None

    def _load_state(self) -> dict:
        try:
            value = json.loads(self._state_file.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def _save_state(self) -> None:
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "silent": self._silent,
                "delivered": self._delivered,
                "was_home": self._was_home,
            }
            tmp = self._state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._state_file)
        except Exception:
            pass

    @property
    def silent(self) -> bool:
        return self._silent

    def set_silent(self, value: bool) -> str:
        self._silent = bool(value)
        self._save_state()
        return "silence" if self._silent else "actif"

    def bind(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Attache le service à la boucle qui possède la session vocale."""
        with self._lock:
            if self._loop is not None:
                return
            self._loop = loop or asyncio.get_running_loop()
            self._queue = asyncio.PriorityQueue(maxsize=self.MAX_PENDING)
            pending = list(self._pending)
            self._pending.clear()
        for event in pending:
            self._enqueue_on_loop(event)

    def publish(
        self,
        topic: str,
        message: str,
        *,
        dedupe_key: str = "",
        priority: int = 50,
        data: dict[str, Any] | None = None,
    ) -> bool:
        """Publie sans bloquer; False signifie doublon récent ou invalide."""
        topic = re.sub(r"[^a-z0-9_-]", "", str(topic).strip().lower())[:40]
        message = " ".join(str(message or "").split())[:500]
        key = (dedupe_key or topic).strip()[:160]
        if not topic or not message or not key:
            return False
        now = time.time()
        with self._lock:
            if now - self._delivered.get(key, 0.0) < self.TOPIC_COOLDOWN_SECONDS:
                return False
            if key in self._queued_keys:
                return False
            self._queued_keys.add(key)
            event = ProactiveEvent(
                topic=topic,
                message=message,
                dedupe_key=key,
                priority=max(0, min(100, int(priority))),
                data=dict(data or {}),
            )
            loop = self._loop
            if loop is None:
                if len(self._pending) >= self.MAX_PENDING:
                    evicted = self._pending.popleft()
                    self._queued_keys.discard(evicted.key)
                self._pending.append(event)
                return True
        loop.call_soon_threadsafe(self._enqueue_on_loop, event)
        return True

    def _enqueue_on_loop(self, event: ProactiveEvent) -> None:
        queue = self._queue
        if queue is None:
            with self._lock:
                self._pending.append(event)
            return
        self._sequence += 1
        item = (-event.priority, self._sequence, event)
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            with self._lock:
                self._queued_keys.discard(event.key)

    async def next_event(self) -> ProactiveEvent:
        if self._loop is None:
            self.bind()
        assert self._queue is not None
        _priority, _sequence, event = await self._queue.get()
        return event

    def defer(self, event: ProactiveEvent, delay: float = 20.0) -> None:
        """Réveille cet événement par timer, sans boucle de polling."""
        loop = self._loop
        if loop is not None:
            loop.call_later(max(0.1, delay), self._enqueue_on_loop, event)

    def hold(self, event: ProactiveEvent) -> None:
        """Endort l'événement jusqu'à un vrai changement d'activité."""
        with self._lock:
            self._held[event.key] = event

    def wake(self) -> None:
        """Réveille les annonces retenues (fin de parole, sortie de veille)."""
        with self._lock:
            events = list(self._held.values())
            self._held.clear()
            loop = self._loop
        if loop is None:
            return
        for event in events:
            loop.call_soon_threadsafe(self._enqueue_on_loop, event)

    def mark_delivered(self, event: ProactiveEvent) -> None:
        with self._lock:
            self._queued_keys.discard(event.key)
            self._held.pop(event.key, None)
            self._delivered[event.key] = time.time()
            cutoff = time.time() - 7 * 86400
            self._delivered = {
                key: stamp for key, stamp in self._delivered.items()
                if stamp >= cutoff
            }
        self._save_state()

    def discard(self, event: ProactiveEvent) -> None:
        with self._lock:
            self._queued_keys.discard(event.key)
            self._held.pop(event.key, None)

    def observe_location(self, position: dict) -> bool:
        """Publie le briefing seulement lors d'une transition dehors -> maison.

        Le domicile est optionnel et explicite dans ``config/api_keys.json`` :
        ``proactive_home: {lat, lon, radius_m}``. La première position initialise
        l'état sans parler, afin d'éviter un faux « retour » au démarrage.
        """
        home = _home_coordinates()
        if home is None:
            return False
        try:
            lat, lon = float(position["lat"]), float(position["lon"])
        except (KeyError, TypeError, ValueError):
            return False
        home_lat, home_lon, radius_m = home
        at_home = _distance_m(lat, lon, home_lat, home_lon) <= radius_m
        previous = self._was_home
        self._was_home = at_home
        self._save_state()
        if previous is False and at_home:
            return self.publish(
                "arrival",
                _arrival_briefing(),
                dedupe_key="arrival-home",
                priority=60,
            )
        return False

    def set_home(self, lat: float, lon: float, radius_m: float = 250.0) -> None:
        """Enregistre explicitement le domicile après une demande utilisateur."""
        lat, lon = float(lat), float(lon)
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("coordonnées de domicile invalides")
        radius_m = max(50.0, min(2000.0, float(radius_m)))
        path = Path(__file__).resolve().parent.parent / "config" / "api_keys.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["proactive_home"] = {
            "lat": lat, "lon": lon, "radius_m": radius_m,
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(config, ensure_ascii=False, indent=4), encoding="utf-8")
        tmp.replace(path)
        self._was_home = True
        self._save_state()

    async def watch_system_events(self) -> None:
        """UPower/UDisks dorment dans D-Bus et ne réveillent qu'au signal."""
        await asyncio.to_thread(self.evaluate_system_state)
        watchers = []
        if shutil.which("upower"):
            watchers.append(self._watch_command(["upower", "--monitor-detail"], battery=True))
        if shutil.which("dbus-monitor"):
            watchers.append(self._watch_command([
                "dbus-monitor", "--system",
                "type='signal',sender='org.freedesktop.UDisks2'",
            ], battery=False))
        if not watchers:
            await asyncio.Event().wait()
        await asyncio.gather(*watchers)

    async def _watch_command(self, command: list[str], *, battery: bool) -> None:
        while True:
            process = None
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                assert process.stdout is not None
                while await process.stdout.readline():
                    # Plusieurs lignes d'un même signal peuvent arriver ensemble.
                    # Le cooldown/dédoublonnage absorbe ces réveils sans parler deux fois.
                    await asyncio.to_thread(
                        self.evaluate_battery if battery else self.evaluate_disk
                    )
                await process.wait()
            except asyncio.CancelledError:
                if process and process.returncode is None:
                    process.terminate()
                raise
            except Exception:
                pass
            await asyncio.sleep(5)  # uniquement si le moniteur D-Bus est tombé

    def evaluate_system_state(self) -> None:
        self.evaluate_battery()
        self.evaluate_disk()

    def evaluate_battery(self) -> bool:
        try:
            import psutil
            battery = psutil.sensors_battery()
        except Exception:
            battery = None
        if not battery or battery.power_plugged or battery.percent >= 15:
            return False
        remaining = ""
        seconds = getattr(battery, "secsleft", -1)
        if isinstance(seconds, (int, float)) and 60 <= seconds < 7 * 86400:
            remaining = f" Il reste environ {max(1, round(seconds / 60))} minutes."
        return self.publish(
            "battery",
            f"Batterie faible : {battery.percent:.0f} %.{remaining} Branche le chargeur.",
            priority=90,
        )

    def evaluate_disk(self) -> bool:
        try:
            usage = shutil.disk_usage(Path.home())
            percent = 100 * usage.used / usage.total
        except Exception:
            return False
        if percent <= 93:
            return False
        reclaimable = _cache_reclaimable_gib()
        detail = (
            f" Je peux libérer environ {reclaimable:.1f} Go dans les caches."
            if reclaimable >= 0.1 else " Je peux analyser les caches à nettoyer."
        )
        return self.publish(
            "disk",
            f"Le disque est rempli à {percent:.0f} %.{detail}",
            priority=80,
        )


def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _home_coordinates() -> tuple[float, float, float] | None:
    try:
        path = Path(__file__).resolve().parent.parent / "config" / "api_keys.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        home = config.get("proactive_home") or {}
        lat = float(home["lat"])
        lon = float(home["lon"])
        radius = max(50.0, min(2000.0, float(home.get("radius_m", 250))))
        return lat, lon, radius
    except Exception:
        return None


def _arrival_briefing() -> str:
    try:
        from actions.reminder import active_reminders
        count = len(active_reminders())
    except Exception:
        count = 0
    reminder = (
        f" Tu as {count} rappel{'s' if count != 1 else ''} actif{'s' if count != 1 else ''}."
        if count else ""
    )
    return f"Bon retour à la maison.{reminder} Je reste disponible."


def _cache_reclaimable_gib() -> float:
    candidates = [
        Path.home() / ".cache/pip",
        Path.home() / ".cache/thumbnails",
        Path.home() / ".cache/yay",
        Path.home() / ".cache/paru",
        Path.home() / ".cache/uv",
    ]
    existing = [str(path) for path in candidates if path.exists()]
    if not existing or not shutil.which("du"):
        return 0.0
    try:
        result = kit.run(
            ["du", "-sb", "--", *existing],
            timeout=8,
        )
        total = sum(int(line.split()[0]) for line in result.stdout.splitlines() if line.split())
        return total / (1024 ** 3)
    except Exception:
        return 0.0


def desktop_blocks_proactivity() -> str:
    """Renvoie la raison de blocage : plein écran ou appel/micro capturé."""
    if shutil.which("hyprctl"):
        try:
            result = kit.run(
                ["hyprctl", "-j", "activewindow"], timeout=0.8,
            )
            window = json.loads(result.stdout or "{}")
            if bool(window.get("fullscreen")):
                return "plein écran"
            identity = " ".join(str(window.get(key) or "") for key in ("class", "title")).lower()
            if any(name in identity for name in ("zoom", "meet", "teams", "webex", "jitsi")):
                return "appel"
        except Exception:
            pass
    if shutil.which("pactl"):
        try:
            result = kit.run(
                ["pactl", "list", "source-outputs"], timeout=0.8,
            )
            blocks = result.stdout.lower().split("source output #")[1:]
            for block in blocks:
                if "corked: yes" in block:
                    continue
                if any(own in block for own in ("ano-gpt", "jarvis", "python")):
                    continue
                return "appel"
        except Exception:
            pass
    return ""


def in_quiet_hours(
    now: Optional[datetime] = None,
    quiet_start: int = 23,
    quiet_end: int = 8,
) -> bool:
    """Indique si l'instant donné se situe dans la plage d'heures calmes (défaut 23h–8h)."""
    now = now or datetime.now()
    h = now.hour
    if quiet_start > quiet_end:
        # Plage qui traverse minuit (ex: 23h → 8h)
        return h >= quiet_start or h < quiet_end
    return quiet_start <= h < quiet_end


class ProactiveEngine:
    """
    Surveille le silence de l'utilisateur et, après un délai configurable,
    envoie un contexte riche à Gemini pour qu'il génère (ou pas) un message
    proactif.

    Paramètres (ajustables) :
        min_silence_secs — silence minimum avant un premier déclenchement
                           (défaut : 900 s = 15 min)
        check_cooldown   — intervalle minimum entre deux messages proactifs
                           (défaut : 600 s = 10 min), persistant entre redémarrages
        quiet_start      — heure de début de la plage de silence (défaut : 23)
        quiet_end        — heure de fin de la plage de silence (défaut : 8)
    """

    def __init__(
        self,
        min_silence_secs: int = 900,
        check_cooldown: int = 600,
        quiet_start: int = 23,
        quiet_end: int = 8,
        state_file: Optional[str] = None,
    ):
        self.min_silence_secs = min_silence_secs
        self.check_cooldown = check_cooldown
        self.quiet_start = quiet_start
        self.quiet_end = quiet_end
        self._state_file = Path(state_file) if state_file else _STATE_PATH
        self._started_at = time.monotonic()
        self._last_triggered_wall = self._load_last_triggered()

    # ── Persistance du cooldown (survit aux redémarrages) ────────────────
    def _load_last_triggered(self) -> float:
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
            return float(data.get("last_triggered", 0.0))
        except Exception:
            return 0.0

    def _save_last_triggered(self) -> None:
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(
                json.dumps({"last_triggered": self._last_triggered_wall}),
                encoding="utf-8",
            )
        except Exception:
            pass

    # ── Heures calmes ────────────────────────────────────────────────────
    def _in_quiet_hours(self, now: Optional[datetime] = None) -> bool:
        return in_quiet_hours(now, self.quiet_start, self.quiet_end)

    # ── Décision de déclenchement ────────────────────────────────────────
    def should_trigger(self, last_user_speech: float, force: bool = False) -> bool:
        """
        Vérifie si les conditions de déclenchement sont remplies :
          - l'utilisateur est silencieux depuis assez longtemps
            (last_user_speech est un timestamp monotonic) ;
          - le délai minimal depuis la dernière prise de parole proactive
            est écoulé (persistant) ;
          - on n'est pas en heures calmes (sauf force=True).
        """
        now = time.monotonic()
        silence = now - last_user_speech
        gap = time.time() - self._last_triggered_wall
        if silence < self.min_silence_secs or gap < self.check_cooldown:
            return False
        if not force and self._in_quiet_hours():
            return False
        return True

    def mark_triggered(self) -> None:
        """Enregistre l'instant du dernier message proactif (persistant)."""
        self._last_triggered_wall = time.time()
        self._save_last_triggered()

    # ── Construction du contexte ─────────────────────────────────────────
    def build_prompt(self, memory: dict, silence_secs: float) -> str:
        """
        Construit un instantané de contexte très complet pour Gemini.
        Gemini l'utilise pour décider librement quoi dire (ou ne rien dire).
        """
        now = datetime.now()
        time_str = now.strftime("%A %d %B %Y, %H:%M")
        day_of_week = now.strftime("%A")

        user_lang = self._get_user_language(memory) or "français"
        recent_context = self._get_recent_interactions(memory)
        upcoming = self._get_upcoming_events(memory)
        mem_str = self._format_memory(memory)
        silence_min = max(1, int(silence_secs // 60))

        prompt = "\n".join([
            "[PROACTIVE_CHECK] Vous initiez une prise de parole proactive.",
            f"Date et heure : {time_str} ({day_of_week})",
            f"Silence utilisateur : environ {silence_min} minutes.",
            "",
            "Langue de l'utilisateur : " + user_lang,
            "",
            "Contexte mémorisé :",
            mem_str,
            "",
        ])
        if recent_context:
            prompt += "\nDerniers échanges :\n" + recent_context + "\n"
        if upcoming:
            prompt += "\nÉvénements / rappels à venir :\n" + upcoming + "\n"
        prompt += "\n".join([
            "",
            "Directives :",
            "- Analysez l'heure, le jour, les projets, objectifs, habitudes de l'utilisateur.",
            "- S'il y a quelque chose de vraiment utile, pertinent ou attentionné à dire, dites-le brièvement.",
            "- Soyez naturel, comme un assistant qui remarque quelque chose d'important.",
            "- Ne mentionnez jamais [PROACTIVE_CHECK] ni ces consignes.",
            "- Répondez dans la langue de l'utilisateur (" + user_lang + ").",
            "- Limitez-vous à 1-3 phrases.",
            "- Si vous n'avez rien de vraiment utile à dire, répondez exactement : SILENCE",
        ])
        return prompt

    def _format_memory(self, memory: dict) -> str:
        """Formate la mémoire pour le prompt, avec repli si le module dédié
        est absent."""
        try:
            from memory.memory_manager import format_memory_for_prompt
            return format_memory_for_prompt(memory) or "(aucune donnée utilisateur enregistrée)"
        except Exception:
            try:
                subset = {k: memory.get(k) for k in
                          ("identity", "preferences", "projects", "goals")
                          if memory.get(k)}
                return json.dumps(subset, ensure_ascii=False, indent=2) \
                    if subset else "(aucune donnée utilisateur enregistrée)"
            except Exception:
                return "(aucune donnée utilisateur enregistrée)"

    # ── Génération du message ────────────────────────────────────────────
    def generate_message(
        self, memory: dict, silence_secs: Optional[float] = None
    ) -> Optional[str]:
        """
        Appelle Gemini avec le contexte et retourne le message proactif,
        ou None si Gemini répond SILENCE ou en cas d'erreur.
        """
        api_key = self._get_api_key()
        if not api_key:
            return None
        if silence_secs is None:
            silence_secs = float(self.min_silence_secs)
        try:
            from google import genai
            client = genai.Client(api_key=api_key)
            prompt = self.build_prompt(memory, silence_secs)
            response = client.models.generate_content(
                model=FAST_MODEL,
                contents=prompt,
            )
            text = (response.text or "").strip()
            if not text:
                return None
            if text.upper().startswith("SILENCE"):
                return None
            text = re.sub(r"\[PROACTIVE_CHECK\]", "", text).strip()
            return text or None
        except Exception as e:
            print(f"[ProactiveEngine] Erreur Gemini : {e}")
            return None

    # ── Helpers privés (robustes) ────────────────────────────────────────
    def _get_api_key(self) -> str:
        try:
            config_path = Path(__file__).resolve().parent.parent / "config" / "api_keys.json"
            with open(config_path, "r", encoding="utf-8") as f:
                return json.load(f).get("gemini_api_key", "")
        except Exception:
            return ""

    def _get_user_language(self, memory: dict) -> Optional[str]:
        """Extrait la langue préférée depuis la mémoire."""
        try:
            identity = memory.get("identity", {}) or {}
            for field in ("language", "langue", "preferred_language"):
                val = identity.get(field)
                if isinstance(val, dict):
                    val = val.get("value")
                if val:
                    return str(val)
        except Exception:
            pass
        return None

    def _get_recent_interactions(self, memory: dict) -> str:
        """Récupère un résumé des derniers échanges (si stocké)."""
        try:
            recent = memory.get("recent_conversation") or memory.get("history", [])
            if not isinstance(recent, list) or not recent:
                return ""
            last = recent[-3:] if len(recent) > 3 else recent
            lines = []
            for entry in last:
                if not isinstance(entry, dict):
                    continue
                role = entry.get("role", "user")
                content = entry.get("content", "")
                lines.append(f"{role}: {content}")
            return "\n".join(lines)
        except Exception:
            return ""

    def _get_upcoming_events(self, memory: dict) -> str:
        """Récupère les rappels ou événements à venir (si gérés)."""
        try:
            reminders = memory.get("reminders", []) or memory.get("calendar", [])
            if not isinstance(reminders, list) or not reminders:
                return ""
            lines = []
            for r in reminders[:3]:
                if isinstance(r, dict) and r.get("text"):
                    lines.append("- " + r.get("text", ""))
            return "\n".join(lines)
        except Exception:
            return ""


# ────────────────────────────────────────────────────────────────────────────
# Point d'entrée outil (déclenchement forcé / test)
# ────────────────────────────────────────────────────────────────────────────

def proactive_control(
    parameters: dict = None, response=None, player=None, session_memory=None
) -> str:
    """
    Déclenche une vérification proactive immédiate (force=True).
    Paramètres :
        memory        : dict de mémoire utilisateur
        silence_secs  : durée de silence à indiquer à Gemini (défaut : min_silence)
    Retourne le message généré ou « SILENCE ».
    """
    params = parameters or {}
    memory = params.get("memory", {}) or {}
    engine = ProactiveEngine()
    silence = float(params.get("silence_secs", engine.min_silence_secs))
    msg = engine.generate_message(memory, silence_secs=silence)
    if msg:
        engine.mark_triggered()
        if player:
            try:
                player.write_log(f"[proactive] {msg[:60]}")
            except Exception:
                pass
        return msg
    return "SILENCE"


# ────────────────────────────────────────────────────────────────────────────
# Test direct
# ────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    engine = ProactiveEngine()
    print("Heures calmes :", engine._in_quiet_hours())
    print("Message :", proactive_control({"memory": {}}))
