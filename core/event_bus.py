"""core/event_bus.py — Bus d'''événements asynchrone central d'''ANO-GPT.

Architecture événementielle découplée pour éliminer le couplage fort par
callbacks dans JarvisLive, sécuriser les accès concurrents (asyncio, threads
audio, thread GUI Qt) et isoler les pannes.

Fonctionnalités :
1. Événements fortement typés immuables (@dataclass(frozen=True)) dérivant de BaseEvent.
2. Souscription asynchrone (async def) et synchrone (def pour réactivité sub-milliseconde).
3. Priorités d'''écoute : HIGH (barge-in VAD), NORMAL (UI, orchestration), LOW (stats, logging).
4. Pont Qt natif et thread-safe : EventBusBridge(QObject) avec pyqtSignal.
5. Buffer circulaire des 100 derniers événements avec capacité de replay pour auto-debug.
6. Isolation totale des exceptions pour éviter l'''effondrement en cascade des listeners.
7. Débit ultra-performant (> 100 000 événements / seconde).
"""

from __future__ import annotations

import asyncio
import collections
import dataclasses
from dataclasses import dataclass, field
from enum import IntEnum
import inspect
import logging
import sys
import threading
import time
from typing import (
    Any,
    Callable,
    Coroutine,
    Dict,
    Generic,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
    Type,
    TypeVar,
    Union,
)
import weakref

logger = logging.getLogger("ano_gpt.event_bus")

# ── Compatibilité Qt (PyQt6 prioritaire, fallback PyQt5 ou Dummy) ────────────
try:
    from PyQt6.QtCore import QObject, pyqtSignal
    _QT_AVAILABLE = True
except ImportError:
    try:
        from PyQt5.QtCore import QObject, pyqtSignal
        _QT_AVAILABLE = True
    except ImportError:
        _QT_AVAILABLE = False

        class QObject:  # type: ignore[no-redef]
            """Dummy QObject lorsque Qt n'''est pas installé."""
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

        class _DummySignal:
            """Dummy pyqtSignal pour environnements headless sans Qt."""
            def __init__(self, *args: Any) -> None:
                self._slots: list[Callable[..., Any]] = []

            def connect(self, slot: Callable[..., Any]) -> None:
                if slot not in self._slots:
                    self._slots.append(slot)

            def disconnect(self, slot: Optional[Callable[..., Any]] = None) -> None:
                if slot is None:
                    self._slots.clear()
                elif slot in self._slots:
                    self._slots.remove(slot)

            def emit(self, *args: Any) -> None:
                for slot in list(self._slots):
                    try:
                        slot(*args)
                    except Exception as exc:
                        logger.error("Erreur slot dummy signal: %s", exc)

        def pyqtSignal(*args: Any) -> Any:  # type: ignore[misc]
            return _DummySignal(*args)


# ════════════════════════════════════════════════════════════════════════════
# 1. Hiérarchie des Événements Typés (Frozen Dataclasses)
# ════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class BaseEvent:
    """Classe de base immuable pour tous les événements du bus.

    Chaque événement dispose d'''un nom d'''événement accessible par `event_name`
    et d'''un horodatage `timestamp` (en secondes UNIX, avec fallback automatique).
    """

    def __getattr__(self, name: str) -> Any:
        # Fournit un horodatage automatique si la sous-classe n'''en définit pas
        if name == "timestamp":
            return time.time()
        raise AttributeError(f"'''{self.__class__.__name__}''' n'''a pas d'''attribut '''{name}'''")

    @property
    def event_name(self) -> str:
        """Nom de la classe de l'''événement."""
        return self.__class__.__name__

    def to_dict(self) -> dict[str, Any]:
        """Sérialise l'''événement en dictionnaire pour le diagnostic ou l'''export."""
        data = dataclasses.asdict(self)
        data["_event_name"] = self.event_name
        if "timestamp" not in data:
            data["timestamp"] = self.timestamp
        return data


@dataclass(frozen=True)
class AudioCaptureFrameEvent(BaseEvent):
    """Trame audio brute capturée par le microphone (PCM 16kHz)."""
    pcm_bytes: bytes
    rms: float = 0.0
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True)
class ModelSpeechDeltaEvent(BaseEvent):
    """Segment de texte et/ou audio renvoyé en streaming par le modèle LLM."""
    text: str = ""
    audio_chunk: Optional[bytes] = None
    is_final: bool = False
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True)
class BargeInDetectedEvent(BaseEvent):
    """Interruption utilisateur détectée (VAD locale ou mot de réveil)."""
    trigger_type: str = "vad"   # "vad", "keyword", "rms", "manual"
    confidence: float = 1.0
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True)
class ToolExecutionRequestedEvent(BaseEvent):
    """Demande d'''exécution d'''un outil par le modèle d'''assistance."""
    tool_name: str
    call_id: str
    params: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True)
class ToolExecutionFinishedEvent(BaseEvent):
    """Résultat de l'''exécution d'''un outil (succès ou erreur)."""
    call_id: str
    result: Any = None
    duration_ms: float = 0.0
    error: Optional[str] = None
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True)
class ConnectionStateChangedEvent(BaseEvent):
    """Changement d'''état de la connexion WebSocket / Live Gemini."""
    old_state: str
    new_state: str
    retry_count: int = 0
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True)
class SystemAlertEvent(BaseEvent):
    """Alerte système interne (diagnostic, avertissement, circuit breaker)."""
    severity: str = "INFO"   # "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"
    source: str = "system"
    message: str = ""
    timestamp: float = field(default_factory=time.time)


# Événements complémentaires pratiques pour le HUD et le cycle de vie
@dataclass(frozen=True)
class UserTextMessageEvent(BaseEvent):
    """Texte soumis explicitement par l'''utilisateur via le HUD ou le terminal."""
    text: str
    source: str = "hud"
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True)
class UIStateChangedEvent(BaseEvent):
    """Changement d'''état visuel ou d'''interface utilisateur."""
    state: str
    details: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True)
class ScreenChangeEvent(BaseEvent):
    """La fenêtre active ou son contenu visuel a réellement changé."""
    window_class: str = ""
    window_title: str = ""
    reason: str = "visual"          # first | app_switch | visual
    phash_distance: int = 0
    ssim: float = 1.0
    keywords: tuple[str, ...] = ()
    has_error: bool = False
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True)
class GestureRecognizedEvent(BaseEvent):
    """Geste de contrôle silencieux identifié par le module de vision."""
    gesture: str
    label: str = ""
    icon: str = ""
    value: float = 0.0
    confidence: float = 1.0
    timestamp: float = field(default_factory=time.time)


# ════════════════════════════════════════════════════════════════════════════
# 2. Priorités d'''Écoute
# ════════════════════════════════════════════════════════════════════════════

class EventPriority(IntEnum):
    """Priorité d'''exécution des écouteurs d'''événements.

    Les écouteurs HIGH sont exécutés en premier (ex: coupure audio barge-in),
    suivis de NORMAL (UI, orchestration), puis LOW (logging, statistiques).
    """
    HIGH = 100
    NORMAL = 50
    LOW = 10


def _normalize_priority(priority: Union[EventPriority, int, str]) -> int:
    """Convertit n'''importe quelle spécification de priorité en entier comparable."""
    if isinstance(priority, EventPriority):
        return priority.value
    if isinstance(priority, int):
        return priority
    if isinstance(priority, str):
        key = priority.upper().strip()
        if key in EventPriority.__members__:
            return EventPriority[key].value
        try:
            return int(key)
        except ValueError:
            pass
    return EventPriority.NORMAL.value


# ════════════════════════════════════════════════════════════════════════════
# 3. Descripteur de Souscription
# ════════════════════════════════════════════════════════════════════════════

HandlerCallable = Union[
    Callable[[Any], None],
    Callable[[Any], Coroutine[Any, Any, None]],
]

T_Event = TypeVar("T_Event", bound=BaseEvent)


@dataclass
class Subscription:
    """Descripteur d'''une souscription active sur le bus d'''événements."""
    event_type: Type[BaseEvent]
    callback: HandlerCallable
    priority: int
    order: int
    is_async: bool
    bus_ref: weakref.ref[AsyncEventBus]
    active: bool = True

    def unsubscribe(self) -> None:
        """Désinscrit cet écouteur du bus associé."""
        if not self.active:
            return
        self.active = False
        bus = self.bus_ref()
        if bus is not None:
            bus._remove_subscription(self)


# ════════════════════════════════════════════════════════════════════════════
# 4. Bus d'''Événements Asynchrone Central
# ════════════════════════════════════════════════════════════════════════════

class AsyncEventBus:
    """Bus d'''événements central asynchrone haute performance.

    Fournit :
    - Publication asynchrone (`await bus.publish(event)`) et synchrone (`bus.publish_sync(event)`).
    - Dispatch ordonné par priorités (HIGH -> NORMAL -> LOW) et préservation FIFO.
    - Isolation stricte des exceptions des handlers.
    - Buffer circulaire des 100 derniers événements avec inspection et replay.
    - Support des singletons par instance hôte (ex: JarvisLive) ou global.
    """

    _default_instance: Optional[AsyncEventBus] = None
    _host_instances: weakref.WeakKeyDictionary[Any, AsyncEventBus] = (
        weakref.WeakKeyDictionary()
    )
    _registry_lock = threading.Lock()

    def __init__(self, history_maxlen: int = 100, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """Initialise une nouvelle instance d'''AsyncEventBus."""
        self._history: collections.deque[BaseEvent] = collections.deque(maxlen=history_maxlen)
        self._error_history: collections.deque[dict[str, Any]] = collections.deque(maxlen=50)

        # Structure de stockage : event_type -> list[Subscription]
        self._subscribers: dict[Type[BaseEvent], list[Subscription]] = collections.defaultdict(list)
        # Écouteurs globaux recevant tous les événements
        self._global_subscribers: list[Subscription] = []

        # Cache de dispatch pré-compilé et trié pour performance maximale : event_type -> tuple[Subscription]
        self._dispatch_cache: dict[Type[BaseEvent], tuple[Subscription, ...]] = {}

        # Compteur pour ordre FIFO stable à priorité égale
        self._order_counter: int = 0
        self._lock = threading.RLock()

        # Boucle asyncio de référence
        self._loop: Optional[asyncio.AbstractEventLoop] = loop

        # Statistiques
        self._total_published: int = 0
        self._total_errors: int = 0

    # ── Gestion des Singletons ──────────────────────────────────────────────

    @classmethod
    def get_instance(cls, host: Any = None) -> AsyncEventBus:
        """Récupère ou instancie le singleton associé à l'''hôte `host` (ex: JarvisLive).

        Si `host` est None, retourne le singleton de processus global par défaut.
        """
        with cls._registry_lock:
            if host is None:
                if cls._default_instance is None:
                    cls._default_instance = cls()
                return cls._default_instance

            # Recherche dans l'''attribut de l'''hôte s'''il existe déjà
            existing_bus = getattr(host, "_event_bus", None)
            if isinstance(existing_bus, cls):
                return existing_bus

            if host in cls._host_instances:
                bus = cls._host_instances[host]
                try:
                    host._event_bus = bus
                except Exception:
                    pass
                return bus

            # Création du bus spécifique à cette instance hôte
            new_bus = cls()
            cls._host_instances[host] = new_bus
            try:
                host._event_bus = new_bus
            except Exception:
                pass
            return new_bus

    @classmethod
    def for_instance(cls, host: Any) -> AsyncEventBus:
        """Alias explicite pour `get_instance(host)`."""
        return cls.get_instance(host)

    @classmethod
    def set_default(cls, bus: AsyncEventBus) -> None:
        """Relie les producteurs globaux au bus de l'orchestrateur actif."""
        if not isinstance(bus, cls):
            raise TypeError("bus doit être une instance de AsyncEventBus")
        with cls._registry_lock:
            cls._default_instance = bus

    @classmethod
    def reset(cls, host: Any = None) -> None:
        """Réinitialise l'''instance singleton pour les tests."""
        with cls._registry_lock:
            if host is None:
                cls._default_instance = None
            else:
                cls._host_instances.pop(host, None)
                if hasattr(host, "_event_bus"):
                    try:
                        delattr(host, "_event_bus")
                    except Exception:
                        host._event_bus = None

    @classmethod
    def reset_all(cls) -> None:
        """Réinitialise tous les singletons existants."""
        with cls._registry_lock:
            cls._default_instance = None
            cls._host_instances.clear()

    # ── Configuration de la boucle d'''événements ─────────────────────────────

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Associe explicitement la boucle asyncio principale."""
        self._loop = loop

    def get_loop(self) -> Optional[asyncio.AbstractEventLoop]:
        """Retourne la boucle asyncio active ou celle configurée."""
        if self._loop is not None and not self._loop.is_closed():
            return self._loop
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            return None

    # ── Souscription ────────────────────────────────────────────────────────

    def subscribe(
        self,
        event_type: Type[T_Event],
        callback: HandlerCallable,
        priority: Union[EventPriority, int, str] = EventPriority.NORMAL,
    ) -> Subscription:
        """Souscrit un écouteur synchrone ou asynchrone à un type d'''événement donné.

        Args:
            event_type: Classe dérivée de BaseEvent, ou BaseEvent pour tous les événements.
            callback: Fonction callable (def synchrone ou async def coroutine).
            priority: Priorité d'''exécution (HIGH, NORMAL, LOW).

        Returns:
            Un objet Subscription permettant de se désinscrire via `.unsubscribe()`.
        """
        if not (isinstance(event_type, type) and issubclass(event_type, BaseEvent)):
            raise TypeError(f"event_type doit être une sous-classe de BaseEvent, reçu: {event_type}")

        if not callable(callback):
            raise TypeError(f"callback doit être un callable, reçu: {type(callback)}")

        is_async = inspect.iscoroutinefunction(callback) or (
            callable(callback) and inspect.iscoroutinefunction(callback.__call__)
        )
        prio_val = _normalize_priority(priority)

        with self._lock:
            self._order_counter += 1
            sub = Subscription(
                event_type=event_type,
                callback=callback,
                priority=prio_val,
                order=self._order_counter,
                is_async=is_async,
                bus_ref=weakref.ref(self),
                active=True,
            )

            if event_type is BaseEvent:
                self._global_subscribers.append(sub)
                self._global_subscribers.sort(key=lambda s: (-s.priority, s.order))
            else:
                sub_list = self._subscribers[event_type]
                sub_list.append(sub)
                sub_list.sort(key=lambda s: (-s.priority, s.order))

            # Invalidation du cache de dispatch
            self._dispatch_cache.clear()
            return sub

    def on(
        self,
        event_type: Type[T_Event],
        priority: Union[EventPriority, int, str] = EventPriority.NORMAL,
    ) -> Callable[[HandlerCallable], HandlerCallable]:
        """Décorateur pratique pour souscrire une fonction ou coroutine à un événement."""
        def decorator(fn: HandlerCallable) -> HandlerCallable:
            self.subscribe(event_type, fn, priority=priority)
            return fn
        return decorator

    def unsubscribe(self, event_type: Type[BaseEvent], callback: HandlerCallable) -> bool:
        """Désinscrit un callback pour un type d'''événement donné."""
        with self._lock:
            target_list = self._global_subscribers if event_type is BaseEvent else self._subscribers.get(event_type, [])
            found = False
            for sub in list(target_list):
                if sub.callback == callback:
                    sub.active = False
                    target_list.remove(sub)
                    found = True

            if found:
                self._dispatch_cache.clear()
            return found

    def _remove_subscription(self, sub: Subscription) -> None:
        """Méthode interne appelée par Subscription.unsubscribe()."""
        with self._lock:
            target_list = (
                self._global_subscribers
                if sub.event_type is BaseEvent
                else self._subscribers.get(sub.event_type, [])
            )
            if sub in target_list:
                target_list.remove(sub)
                self._dispatch_cache.clear()

    def unsubscribe_all(self) -> None:
        """Supprime tous les écouteurs du bus."""
        with self._lock:
            for sub in self._global_subscribers:
                sub.active = False
            for subs in self._subscribers.values():
                for sub in subs:
                    sub.active = False
            self._global_subscribers.clear()
            self._subscribers.clear()
            self._dispatch_cache.clear()

    # ── Chaine de Dispatch Pré-compilée ─────────────────────────────────────

    def _get_handlers_for(self, evt_type: Type[BaseEvent]) -> tuple[Subscription, ...]:
        """Retourne la liste ordonnée et fusionnée des handlers pour un type d'''événement donné."""
        cached = self._dispatch_cache.get(evt_type)
        if cached is not None:
            return cached

        with self._lock:
            cached = self._dispatch_cache.get(evt_type)
            if cached is not None:
                return cached

            # Agrège les handlers spécifiques aux sous-classes et les globaux (BaseEvent)
            merged: list[Subscription] = []
            for cls in evt_type.__mro__:
                if issubclass(cls, BaseEvent) and cls is not BaseEvent:
                    merged.extend(self._subscribers.get(cls, []))

            merged.extend(self._global_subscribers)
            # Tri par priorité décroissante (-priority) puis ordre d'''enregistrement (order)
            merged.sort(key=lambda s: (-s.priority, s.order))

            chain = tuple(merged)
            self._dispatch_cache[evt_type] = chain
            return chain

    # ── Publication Asynchrone et Synchrone ─────────────────────────────────

    async def publish(self, event: BaseEvent) -> None:
        """Publie un événement de manière asynchrone.

        Les écouteurs sont appelés dans l'''ordre de priorité :
        - Les fonctions synchrones sont invoquées immédiatement.
        - Les coroutines asynchrones sont attendues (await).
        - Chaque exception est capturée et isolée.
        """
        if not isinstance(event, BaseEvent):
            raise TypeError(f"L'''événement doit dériver de BaseEvent, reçu: {type(event)}")

        # Enregistrement dans le buffer circulaire de diagnostic
        self._history.append(event)
        self._total_published += 1

        handlers = self._get_handlers_for(type(event))
        if not handlers:
            return

        for sub in handlers:
            if not sub.active:
                continue
            try:
                if sub.is_async:
                    await sub.callback(event)  # type: ignore[misc]
                else:
                    sub.callback(event)
            except Exception as exc:
                self._handle_handler_exception(sub, event, exc)

    def publish_sync(self, event: BaseEvent) -> None:
        """Publie un événement de manière synchrone (thread-safe).

        - Les handlers synchrones s'''exécutent immédiatement dans le thread courant.
        - Les handlers asynchrones sont planifiés sous forme de tâche sur la boucle asyncio.
        - Idéal pour les flux audio haute fréquence ou l'''émission depuis des threads dédiés.
        """
        if not isinstance(event, BaseEvent):
            raise TypeError(f"L'''événement doit dériver de BaseEvent, reçu: {type(event)}")

        self._history.append(event)
        self._total_published += 1

        handlers = self._get_handlers_for(type(event))
        if not handlers:
            return

        loop = self.get_loop()

        for sub in handlers:
            if not sub.active:
                continue
            try:
                if not sub.is_async:
                    sub.callback(event)
                else:
                    # Planification sur la boucle asyncio
                    self._schedule_async_handler(sub, event, loop)
            except Exception as exc:
                self._handle_handler_exception(sub, event, exc)

    # Alias d'''émission conventionnels
    emit = publish_sync
    publish_nowait = publish_sync

    def _schedule_async_handler(
        self,
        sub: Subscription,
        event: BaseEvent,
        loop: Optional[asyncio.AbstractEventLoop],
    ) -> None:
        """Planifie l'''exécution d'''un handler asynchrone sur la boucle active."""
        if loop is None or loop.is_closed():
            logger.warning(
                "Impossible de planifier le handler async %r pour %s : aucune boucle active.",
                sub.callback,
                event.event_name,
            )
            return

        async def _runner() -> None:
            try:
                await sub.callback(event)  # type: ignore[misc]
            except Exception as exc:
                self._handle_handler_exception(sub, event, exc)

        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        if current_loop is loop:
            loop.create_task(_runner())
        else:
            # Appel thread-safe depuis un autre thread (PyAudio, Qt GUI)
            asyncio.run_coroutine_threadsafe(_runner(), loop)

    # ── Isolation des Exceptions ────────────────────────────────────────────

    def _handle_handler_exception(self, sub: Subscription, event: BaseEvent, exc: Exception) -> None:
        """Capture, journalise et stocke l'''erreur d'''un écouteur sans perturber les autres."""
        self._total_errors += 1
        tb = sys.exc_info()[2]
        error_record = {
            "timestamp": time.time(),
            "event_type": event.event_name,
            "event": event,
            "handler": getattr(sub.callback, "__name__", repr(sub.callback)),
            "error_type": exc.__class__.__name__,
            "error_message": str(exc),
        }
        self._error_history.append(error_record)

        if logger.isEnabledFor(logging.ERROR):
            logger.error(
                "Exception isolée dans le handler '%s' pour l'événement '%s': %s",
                error_record["handler"],
                event.event_name,
                exc,
                exc_info=tb,
            )

    # ── Diagnostic en Direct, Buffer Circulaire & Replay ────────────────────

    def get_history(
        self,
        limit: Optional[int] = None,
        event_type: Optional[Type[BaseEvent]] = None,
    ) -> list[BaseEvent]:
        """Retourne un instantané des événements récents enregistrés dans le buffer circulaire.

        Args:
            limit: Nombre maximum d'''événements récents à retourner (les plus récents).
            event_type: Filtre optionnel par sous-classe d'''événement.
        """
        with self._lock:
            evts = list(self._history)

        if event_type is not None:
            evts = [e for e in evts if isinstance(e, event_type)]

        if limit is not None and limit > 0:
            return evts[-limit:]
        return evts

    def clear_history(self) -> None:
        """Vide le buffer circulaire de diagnostic."""
        with self._lock:
            self._history.clear()

    def replay(
        self,
        events: Optional[Sequence[BaseEvent]] = None,
        filter_type: Optional[Type[BaseEvent]] = None,
        record_in_history: bool = False,
    ) -> int:
        """Rejoue une séquence d'''événements pour le diagnostic ou l'''auto-debug.

        Args:
            events: Liste d'''événements à rejouer (par défaut: instantané du buffer actuel).
            filter_type: Type d'''événement à filtrer lors du replay.
            record_in_history: Si True, ré-enregistre les événements rejoués dans l'''historique.

        Returns:
            Le nombre d'''événements rejoués avec succès.
        """
        if events is None:
            with self._lock:
                to_replay = list(self._history)
        else:
            to_replay = list(events)

        if filter_type is not None:
            to_replay = [e for e in to_replay if isinstance(e, filter_type)]

        replayed_count = 0
        loop = self.get_loop()

        for evt in to_replay:
            if record_in_history:
                self._history.append(evt)

            handlers = self._get_handlers_for(type(evt))
            for sub in handlers:
                if not sub.active:
                    continue
                try:
                    if not sub.is_async:
                        sub.callback(evt)
                    else:
                        self._schedule_async_handler(sub, evt, loop)
                except Exception as exc:
                    self._handle_handler_exception(sub, evt, exc)
            replayed_count += 1

        return replayed_count

    def get_history_stats(self) -> dict[str, Any]:
        """Retourne des métriques d'''activité pour l'''auto-debug et la télémétrie."""
        with self._lock:
            history_snapshot = list(self._history)

        counts: dict[str, int] = collections.defaultdict(int)
        for e in history_snapshot:
            counts[e.event_name] += 1

        oldest = history_snapshot[0].timestamp if history_snapshot else None
        newest = history_snapshot[-1].timestamp if history_snapshot else None

        return {
            "total_published": self._total_published,
            "total_errors": self._total_errors,
            "history_size": len(history_snapshot),
            "history_maxlen": self._history.maxlen,
            "counts_by_type": dict(counts),
            "oldest_timestamp": oldest,
            "newest_timestamp": newest,
            "subscribers_count": sum(len(v) for v in self._subscribers.values()) + len(self._global_subscribers),
        }

    def get_recent_errors(self, limit: Optional[int] = None) -> list[dict[str, Any]]:
        """Retourne la liste des dernières exceptions interceptées par l'''isolation."""
        with self._lock:
            errs = list(self._error_history)
        if limit is not None and limit > 0:
            return errs[-limit:]
        return errs


# ════════════════════════════════════════════════════════════════════════════
# 5. Pont Direct Qt / UI Thread-Safe (EventBusBridge)
# ════════════════════════════════════════════════════════════════════════════

class EventBusBridge(QObject):
    """Pont bidirectionnel thread-safe entre AsyncEventBus et l'''interface PyQt.

    Transforme les événements typés du bus asynchrone en `pyqtSignal` Qt.
    Grâce au mécanisme de QueuedConnection natif de Qt, ces signaux peuvent être
    émis depuis n'''importe quel thread (PyAudio, asyncio, worker) et reçus
    en toute sécurité sur le thread GUI principal de l'''UI.
    """

    # ── Signaux typés pour les événements indispensables ────────────────────
    audio_frame_received     = pyqtSignal(bytes, float, float)        # pcm_bytes, rms, timestamp
    model_speech_delta       = pyqtSignal(str, object, bool)          # text, audio_chunk, is_final
    barge_in_detected        = pyqtSignal(str, float)                 # trigger_type, confidence
    tool_execution_requested = pyqtSignal(str, str, dict)             # tool_name, call_id, params
    tool_execution_finished  = pyqtSignal(str, object, float, object) # call_id, result, duration_ms, error
    connection_state_changed = pyqtSignal(str, str, int)              # old_state, new_state, retry_count
    system_alert             = pyqtSignal(str, str, str)              # severity, source, message
    gesture_recognized       = pyqtSignal(str, str, float)            # icon/gesture, label, value

    # Signal générique recevant l'''objet BaseEvent complet
    event_dispatched         = pyqtSignal(object)                     # BaseEvent

    # Alias pratiques pour compatibilité avec différentes conventions
    barge_in_triggered       = barge_in_detected
    tool_requested           = tool_execution_requested
    tool_finished            = tool_execution_finished
    connection_changed       = connection_state_changed
    speech_delta             = model_speech_delta

    def __init__(
        self,
        bus: Optional[AsyncEventBus] = None,
        parent: Optional[QObject] = None,
        priority: Union[EventPriority, int] = EventPriority.NORMAL,
    ) -> None:
        """Initialise le pont Qt et l'''attache optionnellement à un bus existant."""
        super().__init__(parent)
        self._bus: Optional[AsyncEventBus] = None
        self._subscriptions: list[Subscription] = []
        self._priority = priority

        if bus is not None:
            self.attach(bus)

    @property
    def bus(self) -> Optional[AsyncEventBus]:
        """Retourne le bus actuellement connecté."""
        return self._bus

    def attach(self, bus: AsyncEventBus) -> None:
        """Attache le pont à un AsyncEventBus et enregistre les écouteurs."""
        if self._bus is not None:
            self.detach()

        self._bus = bus
        prio = self._priority

        # Enregistrement des callbacks de conversion vers signaux Qt
        self._subscriptions.append(
            bus.subscribe(AudioCaptureFrameEvent, self._on_audio_frame, priority=prio)
        )
        self._subscriptions.append(
            bus.subscribe(ModelSpeechDeltaEvent, self._on_model_speech, priority=prio)
        )
        self._subscriptions.append(
            bus.subscribe(BargeInDetectedEvent, self._on_barge_in, priority=prio)
        )
        self._subscriptions.append(
            bus.subscribe(ToolExecutionRequestedEvent, self._on_tool_requested, priority=prio)
        )
        self._subscriptions.append(
            bus.subscribe(ToolExecutionFinishedEvent, self._on_tool_finished, priority=prio)
        )
        self._subscriptions.append(
            bus.subscribe(ConnectionStateChangedEvent, self._on_connection_changed, priority=prio)
        )
        self._subscriptions.append(
            bus.subscribe(SystemAlertEvent, self._on_system_alert, priority=prio)
        )
        self._subscriptions.append(
            bus.subscribe(GestureRecognizedEvent, self._on_gesture_recognized, priority=prio)
        )
        self._subscriptions.append(
            bus.subscribe(BaseEvent, self._on_any_event, priority=EventPriority.LOW)
        )

    def detach(self) -> None:
        """Détache le pont du bus d'''événements et annule toutes les souscriptions."""
        for sub in self._subscriptions:
            sub.unsubscribe()
        self._subscriptions.clear()
        self._bus = None

    # ── Callbacks de transformation Événement -> Signal Qt ──────────────────

    def _on_audio_frame(self, evt: AudioCaptureFrameEvent) -> None:
        self.audio_frame_received.emit(evt.pcm_bytes, evt.rms, evt.timestamp)

    def _on_model_speech(self, evt: ModelSpeechDeltaEvent) -> None:
        self.model_speech_delta.emit(evt.text, evt.audio_chunk, evt.is_final)

    def _on_barge_in(self, evt: BargeInDetectedEvent) -> None:
        self.barge_in_detected.emit(evt.trigger_type, evt.confidence)

    def _on_tool_requested(self, evt: ToolExecutionRequestedEvent) -> None:
        self.tool_execution_requested.emit(evt.tool_name, evt.call_id, evt.params)

    def _on_tool_finished(self, evt: ToolExecutionFinishedEvent) -> None:
        self.tool_execution_finished.emit(evt.call_id, evt.result, evt.duration_ms, evt.error)

    def _on_connection_changed(self, evt: ConnectionStateChangedEvent) -> None:
        self.connection_state_changed.emit(evt.old_state, evt.new_state, evt.retry_count)

    def _on_system_alert(self, evt: SystemAlertEvent) -> None:
        self.system_alert.emit(evt.severity, evt.source, evt.message)

    def _on_gesture_recognized(self, evt: GestureRecognizedEvent) -> None:
        self.gesture_recognized.emit(evt.icon or evt.gesture, evt.label, evt.value)

    def _on_any_event(self, evt: BaseEvent) -> None:
        self.event_dispatched.emit(evt)

    # ── Émission depuis Qt vers le bus ──────────────────────────────────────

    def publish_to_bus(self, event: BaseEvent) -> None:
        """Permet aux composants Qt d'''injecter des événements directement sur le bus."""
        if self._bus is not None:
            self._bus.publish_sync(event)
        else:
            logger.warning("Tentative de publication sans bus attaché sur EventBusBridge")
