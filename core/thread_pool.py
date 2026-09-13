"""core/thread_pool.py - Gestionnaire centralisé de threads et pools spécialisés pour ANO-GPT.

Concurrence & POSIX :
---------------------
* Pools spécialisés avec limites strictes :
  - 'audio-io'      : 1 worker dédié avec priorité temps réel POSIX (SCHED_FIFO / nice négatif).
  - 'disk-io'       : 2 workers dédiés aux I/O fichiers, bases SQLite et indexeur.
  - 'compute-light' : min(4, CPU count) workers pour FFT, hashing, traitement d'images léger.
  - 'network-heavy' : workers I/O pour requêtes HTTP, météo, scrapers et notifications push.
* Naming convention strict : 'ano-{pool}-{task_name}-{uuid}' pour un repérage immédiat dans
  htop/gdb (via prctl comm Linux de 16 octets) et dans les frames Python.
* Monitoring & Watchdog :
  - Détection automatique des tâches bloquantes (> 5s configurable) avec log d'alerte et dump de stacktrace.
  - Métriques complètes d'attente en queue et durée d'exécution pour le dashboard télémétrique.
* Arrêt propre garanti sans freeze :
  - Threads daemon évitant le blocage de l'interpréteur Python à la fermeture (atexit hook bypass).
  - shutdown(wait=True, cancel_futures=True, timeout=...) avec annulation préventive des files.
* Intégration hybride Sync / Async :
  - Décorateur @run_in_pool('name') retournant des PoolFuture compatibles asyncio (await direct)
    et concurrent.futures (result(), cancel()).
  - Context managers pour cycle de vie, exécutions par lot (batch) et instrumentation de threads tiers.
"""

from __future__ import annotations

import asyncio
import collections
import concurrent.futures
import concurrent.futures.thread as _cft
import contextlib
import ctypes
import functools
import inspect
import logging
import os
import queue
import re
import sys
import threading
import time
import traceback
import uuid
import weakref
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Generator, Generic, Optional, ParamSpec, TypeVar

logger = logging.getLogger("anogpt.thread_pool")

# ── Primitives POSIX & libc ───────────────────────────────────────────────────

_LIBC: ctypes.CDLL | None = None
try:
    _LIBC = ctypes.CDLL("libc.so.6")
except Exception:
    try:
        _LIBC = ctypes.CDLL(None)
    except Exception:
        _LIBC = None

# Constante Linux PR_SET_NAME = 15
_PR_SET_NAME = 15


def set_posix_thread_name(name: str) -> None:
    """Attribue le nom du thread au niveau du noyau Linux (champ comm du task_struct).

    Visible immédiatement dans htop, top, ps -T, gdb et /proc/self/task/<tid>/comm.
    La taille est bornée à 16 octets par le noyau Linux (15 caractères + null byte).
    """
    if _LIBC is not None and hasattr(_LIBC, "prctl"):
        try:
            encoded = name.encode("utf-8", errors="replace")[:15]
            _LIBC.prctl(_PR_SET_NAME, encoded, 0, 0, 0)
        except Exception:
            pass


def format_posix_comm_name(pool_name: str, task_name: str, task_id: str) -> str:
    """Construit un nom compact tenant sur 15 caractères pour l'affichage htop/gdb.

    Exemples :
      ano-aud-writ-a1
      ano-dsk-sqli-b2
      ano-cmp-fft-c3
      ano-net-http-d4
    """
    pool_abbrs = {
        "audio-io": "aud",
        "disk-io": "dsk",
        "compute-light": "cmp",
        "network-heavy": "net",
    }
    abbr = pool_abbrs.get(pool_name, pool_name[:3])
    prefix = f"ano-{abbr}-"
    budget = 15 - len(prefix)
    if budget <= 0:
        return f"ano-{pool_name}"[:15]

    uid_short = task_id[:2]
    task_budget = max(1, budget - len(uid_short) - 1)
    task_short = re.sub(r"[^a-zA-Z0-9_]", "", task_name)[:task_budget]
    return f"{prefix}{task_short}-{uid_short}"[:15]


def configure_realtime_priority(pool_name: str = "audio-io") -> bool:
    """Configure la priorité temps réel POSIX (SCHED_FIFO ou nice négatif).

    Ordre de repli sécurisé :
    1. os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(20))
    2. os.setpriority(os.PRIO_PROCESS, native_tid, -10)
    3. os.nice(-10) / os.nice(-5)
    
    Ne lève JAMAIS d'exception si l'utilisateur n'a pas CAP_SYS_NICE ou root.
    """
    # 1. Tentative politique temps réel SCHED_FIFO
    if hasattr(os, "sched_setscheduler") and hasattr(os, "sched_param"):
        try:
            param = os.sched_param(20)
            os.sched_setscheduler(0, os.SCHED_FIFO, param)
            logger.info(
                "[%s] Priorité temps réel POSIX SCHED_FIFO activée (priorité=20)",
                pool_name,
            )
            return True
        except (PermissionError, OSError) as exc:
            logger.debug(
                "[%s] SCHED_FIFO non permis (unprivileged / no CAP_SYS_NICE: %s), repli sur renforcement nice",
                pool_name,
                exc,
            )

    # 2. Tentative priorité nice négative (processus ou thread natif)
    tid = threading.get_native_id() if hasattr(threading, "get_native_id") else 0
    for nice_val in (-10, -5, -2):
        try:
            if hasattr(os, "setpriority") and hasattr(os, "PRIO_PROCESS"):
                os.setpriority(os.PRIO_PROCESS, tid, nice_val)
                logger.info(
                    "[%s] Priorité POSIX nice=%d appliquée au thread natif %d",
                    pool_name,
                    nice_val,
                    tid,
                )
                return True
            elif hasattr(os, "nice"):
                delta = nice_val - os.nice(0)
                if delta < 0:
                    os.nice(delta)
                    logger.info(
                        "[%s] Priorité POSIX nice=%d appliquée via os.nice",
                        pool_name,
                        nice_val,
                    )
                    return True
        except (PermissionError, OSError):
            continue

    logger.debug(
        "[%s] Privilèges temps réel/nice négatif indisponibles ; exécution en priorité standard (nice=0)",
        pool_name,
    )
    return False


# ── Modèles de données & Télémétrie ───────────────────────────────────────────

T = TypeVar("T")
P = ParamSpec("P")
R = TypeVar("R")


@dataclass
class TaskRecord:
    """Enregistrement du cycle de vie complet d'une tâche de thread pool."""

    task_id: str
    pool_name: str
    task_name: str
    full_name: str
    submit_time: float
    stall_timeout: float = 5.0
    start_time: float | None = None
    end_time: float | None = None
    wait_time: float | None = None
    exec_duration: float | None = None
    thread_ident: int | None = None
    thread_native_id: int | None = None
    thread_name_orig: str | None = None
    last_warned_at: float | None = None
    stalled_count: int = 0
    completed: bool = False
    cancelled: bool = False
    error: str | None = None


class PoolMetrics:
    """Compteurs et métriques de latence pour un pool dédié."""

    def __init__(self, pool_name: str, max_workers: int) -> None:
        self.pool_name = pool_name
        self.max_workers = max_workers
        self.tasks_submitted: int = 0
        self.tasks_started: int = 0
        self.tasks_completed: int = 0
        self.tasks_failed: int = 0
        self.tasks_cancelled: int = 0
        self.stalls_detected: int = 0

        self.total_wait_time: float = 0.0
        self.max_wait_time: float = 0.0
        self.total_exec_duration: float = 0.0
        self.max_exec_duration: float = 0.0

        self.recent_tasks: collections.deque[dict[str, Any]] = collections.deque(maxlen=100)

    def record_submit(self) -> None:
        self.tasks_submitted += 1

    def record_start(self, wait_time: float) -> None:
        self.tasks_started += 1
        self.total_wait_time += wait_time
        if wait_time > self.max_wait_time:
            self.max_wait_time = wait_time

    def record_finish(self, record: TaskRecord) -> None:
        duration = record.exec_duration or 0.0
        self.total_exec_duration += duration
        if duration > self.max_exec_duration:
            self.max_exec_duration = duration

        if record.error is not None:
            self.tasks_failed += 1
        elif record.cancelled:
            self.tasks_cancelled += 1
        else:
            self.tasks_completed += 1

        self.recent_tasks.append({
            "task_id": record.task_id,
            "task_name": record.task_name,
            "full_name": record.full_name,
            "wait_time_ms": round((record.wait_time or 0.0) * 1000, 2),
            "exec_duration_ms": round(duration * 1000, 2),
            "success": record.error is None and not record.cancelled,
            "error": record.error,
            "stalled": record.stalled_count > 0,
            "finished_at": time.time(),
        })

    def to_dict(self, active_count: int, queue_depth: int) -> dict[str, Any]:
        started = max(1, self.tasks_started)
        finished = max(1, self.tasks_completed + self.tasks_failed)
        return {
            "pool_name": self.pool_name,
            "max_workers": self.max_workers,
            "active_tasks": active_count,
            "pending_in_queue": queue_depth,
            "tasks_submitted": self.tasks_submitted,
            "tasks_completed": self.tasks_completed,
            "tasks_failed": self.tasks_failed,
            "tasks_cancelled": self.tasks_cancelled,
            "stalls_detected": self.stalls_detected,
            "wait_time": {
                "avg_ms": round((self.total_wait_time / started) * 1000, 3),
                "max_ms": round(self.max_wait_time * 1000, 3),
                "total_sec": round(self.total_wait_time, 4),
            },
            "exec_duration": {
                "avg_ms": round((self.total_exec_duration / finished) * 1000, 3),
                "max_ms": round(self.max_exec_duration * 1000, 3),
                "total_sec": round(self.total_exec_duration, 4),
            },
            "recent_tasks_count": len(self.recent_tasks),
        }


# ── PoolFuture : Futur Hybride Sync / Async ───────────────────────────────────

class PoolFuture(Generic[T]):
    """Encapsulation d'un concurrent.futures.Future avec support direct de l'await asyncio.

    Permet à l'appelant de choisir à tout moment :
      val = fut.result()      # Usage synchrone bloquant classique
      val = await fut         # Usage asynchrone direct dans une coroutine asyncio
    """

    def __init__(self, future: concurrent.futures.Future[T], record: TaskRecord) -> None:
        self._future = future
        self._record = record

    @property
    def task_id(self) -> str:
        return self._record.task_id

    @property
    def task_name(self) -> str:
        return self._record.task_name

    @property
    def pool_name(self) -> str:
        return self._record.pool_name

    @property
    def full_name(self) -> str:
        return self._record.full_name

    @property
    def record(self) -> TaskRecord:
        return self._record

    @property
    def wait_time(self) -> float | None:
        return self._record.wait_time

    @property
    def exec_duration(self) -> float | None:
        return self._record.exec_duration

    def cancel(self) -> bool:
        cancelled = self._future.cancel()
        if cancelled:
            self._record.cancelled = True
        return cancelled

    def cancelled(self) -> bool:
        return self._future.cancelled()

    def running(self) -> bool:
        return self._future.running()

    def done(self) -> bool:
        return self._future.done()

    def result(self, timeout: float | None = None) -> T:
        return self._future.result(timeout=timeout)

    def exception(self, timeout: float | None = None) -> BaseException | None:
        return self._future.exception(timeout=timeout)

    def add_done_callback(self, fn: Callable[[PoolFuture[T]], Any]) -> None:
        def _wrapper(_: concurrent.futures.Future[T]) -> None:
            try:
                fn(self)
            except Exception:
                logger.exception("[%s] Erreur dans le callback de fin de tâche", self.full_name)

        self._future.add_done_callback(_wrapper)

    def __await__(self) -> Generator[Any, None, T]:
        loop = asyncio.get_running_loop()
        return asyncio.wrap_future(self._future, loop=loop).__await__()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._future, name)

    def __repr__(self) -> str:
        state = "cancelled" if self.cancelled() else ("done" if self.done() else "running")
        return f"<PoolFuture name='{self.full_name}' state={state}>"


# ── Exécuteur Daemon & Anti-Freeze ─────────────────────────────────────────────

class _DaemonThreadPoolExecutor(concurrent.futures.ThreadPoolExecutor):
    """ThreadPoolExecutor avec threads daemon et extraction de l'atexit bloquant.

    Sous CPython, standard ThreadPoolExecutor crée des threads non-daemon et enregistre
    chacun d'eux dans _threads_queues. Son hook atexit _python_exit() fait t.join() sans timeout,
    ce qui bloque l'extinction du processus si une tâche I/O ou C est suspendue.
    Cette classe résout définitivement ce freeze.
    """

    def __init__(
        self,
        max_workers: int,
        thread_name_prefix: str = "ano-worker",
        initializer: Callable[..., Any] | None = None,
        initargs: tuple[Any, ...] = (),
    ) -> None:
        super().__init__(
            max_workers=max_workers,
            thread_name_prefix=thread_name_prefix,
            initializer=initializer,
            initargs=initargs,
        )

    def _adjust_thread_count(self) -> None:
        # Ne jamais monkey-patcher globalement ``threading.Thread.__init__`` :
        # PortAudio, Qt ou un autre pool peut créer un thread exactement dans
        # cette fenêtre et devenir daemon par accident. On reprend ici le petit
        # algorithme CPython en construisant uniquement NOS workers en daemon.
        if self._idle_semaphore.acquire(timeout=0):
            return

        def weakref_cb(_ref: Any, q: Any = self._work_queue) -> None:
            q.put(None)

        num_threads = len(self._threads)
        if num_threads >= self._max_workers:
            return

        thread_name = "%s_%d" % (
            self._thread_name_prefix or self,
            num_threads,
        )
        executor_ref = weakref.ref(self, weakref_cb)
        if hasattr(self, "_create_worker_context"):
            # CPython 3.14+
            worker_args = (
                executor_ref,
                self._create_worker_context(),
                self._work_queue,
            )
        else:
            # CPython 3.11–3.13
            worker_args = (
                executor_ref,
                self._work_queue,
                self._initializer,
                self._initargs,
            )
        worker = threading.Thread(
            name=thread_name,
            target=_cft._worker,
            args=worker_args,
            daemon=True,
        )
        worker.start()
        self._threads.add(worker)
        # Volontairement absent de ``_threads_queues`` : le shutdown borné de
        # cette classe gère ces workers, sans le join infini de l'atexit stdlib.

    def shutdown(
        self,
        wait: bool = True,
        *,
        cancel_futures: bool = False,
        timeout: float | None = None,
    ) -> None:
        """Arrêt propre et borné sans risque de deadlock."""
        try:
            super().shutdown(wait=False, cancel_futures=cancel_futures)
        except TypeError:
            super().shutdown(wait=False)

        if wait:
            deadline = (time.monotonic() + timeout) if timeout is not None else None
            threads = list(self._threads)
            for t in threads:
                if not t.is_alive():
                    continue
                if deadline is not None:
                    rem = max(0.0, deadline - time.monotonic())
                    t.join(timeout=rem)
                    if t.is_alive():
                        logger.warning(
                            "Le thread worker '%s' ne s'est pas arrêté dans le délai imparti (%.2fs)",
                            t.name,
                            timeout,
                        )
                else:
                    t.join()


# ── Watchdog Anti-Stall ───────────────────────────────────────────────────────

class _WatchdogMonitor:
    """Surveillance active des tâches bloquées (> seuil stall, défaut 5s)."""

    def __init__(
        self,
        active_tasks_getter: Callable[[], list[TaskRecord]],
        metrics_getter: Callable[[str], PoolMetrics | None],
        check_interval: float = 1.0,
    ) -> None:
        self._active_tasks_getter = active_tasks_getter
        self._metrics_getter = metrics_getter
        self._check_interval = check_interval
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="ano-pool-watchdog",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
            self._thread = None

    def _run_loop(self) -> None:
        set_posix_thread_name("ano-watchdog")
        while not self._stop_event.wait(timeout=self._check_interval):
            try:
                self._check_stalled_tasks()
            except Exception:
                logger.exception("Erreur inattendue dans la boucle du Watchdog ANO-Pool")

    def _check_stalled_tasks(self) -> None:
        now = time.monotonic()
        active_tasks = self._active_tasks_getter()
        if not active_tasks:
            return

        frames: dict[int, Any] | None = None

        for task in active_tasks:
            if task.start_time is None or task.completed or task.cancelled:
                continue
            duration = now - task.start_time
            if duration >= task.stall_timeout:
                should_warn = (
                    task.last_warned_at is None
                    or (now - task.last_warned_at) >= task.stall_timeout
                )
                if should_warn:
                    task.last_warned_at = now
                    task.stalled_count += 1

                    if frames is None:
                        frames = sys._current_frames()

                    frame = frames.get(task.thread_ident) if task.thread_ident else None
                    if frame:
                        stacktrace = "".join(traceback.format_stack(frame)).strip()
                    else:
                        stacktrace = "<frame non accessible ou thread terminé>"

                    logger.warning(
                        "⚠️ [ANO-POOL STALL DETECTED] Tâche bloquante '%s' dans pool '%s' !\n"
                        "  → Durée d'exécution active : %.2fs (seuil d'alerte : %.1fs)\n"
                        "  → Thread ID natif : %s (Python ident : %s)\n"
                        "  → Trace d'exécution actuelle :\n%s",
                        task.full_name,
                        task.pool_name,
                        duration,
                        task.stall_timeout,
                        task.thread_native_id or "N/A",
                        task.thread_ident,
                        stacktrace,
                    )

                    metrics = self._metrics_getter(task.pool_name)
                    if metrics:
                        metrics.stalls_detected += 1


# ── Context Managers d'assistance ─────────────────────────────────────────────

class BatchContext:
    """Gestionnaire de contexte pour l'exécution d'un groupe de tâches dans un pool."""

    def __init__(self, pool_manager: ANOThreadPool, pool_name: str) -> None:
        self._pool_manager = pool_manager
        self.pool_name = pool_name
        self.futures: list[PoolFuture[Any]] = []

    def submit(
        self,
        fn: Callable[..., T],
        *args: Any,
        task_name: str | None = None,
        stall_timeout: float = 5.0,
        **kwargs: Any,
    ) -> PoolFuture[T]:
        fut = self._pool_manager.submit(
            self.pool_name,
            fn,
            *args,
            task_name=task_name,
            stall_timeout=stall_timeout,
            **kwargs,
        )
        self.futures.append(fut)
        return fut

    def wait(self, timeout: float | None = None) -> list[Any]:
        """Attend la résolution de toutes les tâches soumises dans ce batch."""
        results: list[Any] = []
        for f in self.futures:
            results.append(f.result(timeout=timeout))
        return results

    def cancel_all(self) -> None:
        """Annule toutes les tâches du lot qui ne sont pas encore démarrées."""
        for f in self.futures:
            f.cancel()


# ── Gestionnaire Central : ANOThreadPool ──────────────────────────────────────

@dataclass(frozen=True)
class PoolConfig:
    """Spécification d'un pool spécialisé."""

    name: str
    max_workers: int
    priority: str = "normal"  # "realtime", "high", "normal", "low"
    stall_timeout: float = 5.0
    description: str = ""


class ANOThreadPool:
    """Gestionnaire centralisé de pools de threads spécialisés pour ANO-GPT."""

    _instance: ANOThreadPool | None = None
    _singleton_lock: threading.Lock = threading.Lock()

    def __init__(
        self,
        custom_configs: list[PoolConfig] | None = None,
        watchdog_interval: float = 0.5,
    ) -> None:
        self._lock = threading.Lock()
        self._start_time = time.monotonic()
        self._is_shutdown = False

        # Configuration des 4 pools fondamentaux requis par ANO-GPT
        cpu_count = os.cpu_count() or 1
        default_configs = [
            PoolConfig(
                name="audio-io",
                max_workers=1,
                priority="realtime",
                stall_timeout=5.0,
                description="Capture micro & lecture haut-parleur (1 worker temps réel)",
            ),
            PoolConfig(
                name="disk-io",
                max_workers=2,
                priority="normal",
                stall_timeout=5.0,
                description="Lectures/écritures fichiers, base SQLite, indexeur local",
            ),
            PoolConfig(
                name="compute-light",
                max_workers=max(1, min(4, cpu_count)),
                priority="normal",
                stall_timeout=5.0,
                description="Calculs FFT, spectrogrammes, hashing et retouche d'images",
            ),
            PoolConfig(
                name="network-heavy",
                max_workers=max(4, min(16, cpu_count * 2 + 4)),
                priority="normal",
                stall_timeout=5.0,
                description="Requêtes HTTP, météo, scrapers web et notifications push",
            ),
        ]

        self._configs: dict[str, PoolConfig] = {cfg.name: cfg for cfg in default_configs}
        if custom_configs:
            for cfg in custom_configs:
                self._configs[cfg.name] = cfg

        self._executors: dict[str, _DaemonThreadPoolExecutor] = {}
        self._metrics: dict[str, PoolMetrics] = {}
        self._active_tasks: dict[str, TaskRecord] = {}

        for name, cfg in self._configs.items():
            self._init_pool(cfg)

        # Démarrage du watchdog
        self._watchdog = _WatchdogMonitor(
            active_tasks_getter=self._get_all_active_tasks,
            metrics_getter=lambda pname: self._metrics.get(pname),
            check_interval=watchdog_interval,
        )
        self._watchdog.start()

    @classmethod
    def get_instance(cls) -> ANOThreadPool:
        """Accès thread-safe au singleton global."""
        if cls._instance is None:
            with cls._singleton_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def _init_pool(self, cfg: PoolConfig) -> None:
        def _worker_init() -> None:
            set_posix_thread_name(f"ano-{cfg.name[:3]}-idle")
            threading.current_thread().name = f"ano-{cfg.name}-idle"
            if cfg.priority == "realtime":
                configure_realtime_priority(cfg.name)

        ex = _DaemonThreadPoolExecutor(
            max_workers=cfg.max_workers,
            thread_name_prefix=f"ano-{cfg.name}",
            initializer=_worker_init,
        )
        self._executors[cfg.name] = ex
        self._metrics[cfg.name] = PoolMetrics(cfg.name, cfg.max_workers)

    def _get_all_active_tasks(self) -> list[TaskRecord]:
        with self._lock:
            return list(self._active_tasks.values())

    def register_pool(
        self,
        name: str,
        max_workers: int,
        priority: str = "normal",
        stall_timeout: float = 5.0,
        description: str = "",
    ) -> None:
        """Enregistre dynamiquement un nouveau pool spécialisé."""
        with self._lock:
            if name in self._executors:
                logger.debug("Le pool '%s' est déjà enregistré", name)
                return
            cfg = PoolConfig(
                name=name,
                max_workers=max_workers,
                priority=priority,
                stall_timeout=stall_timeout,
                description=description,
            )
            self._configs[name] = cfg
            self._init_pool(cfg)
            logger.info("Nouveau pool '%s' enregistré (max_workers=%d)", name, max_workers)

    def get_executor(self, pool_name: str) -> concurrent.futures.ThreadPoolExecutor:
        """Fournit l'instance brute de ThreadPoolExecutor pour loop.run_in_executor."""
        with self._lock:
            ex = self._executors.get(pool_name)
            if ex is None:
                raise KeyError(f"Pool inconnu : '{pool_name}'. Disponibles : {list(self._executors.keys())}")
            return ex

    def submit(
        self,
        pool_name: str,
        fn: Callable[..., T],
        *args: Any,
        task_name: str | None = None,
        stall_timeout: float | None = None,
        **kwargs: Any,
    ) -> PoolFuture[T]:
        """Soumet une tâche avec nommage strict 'ano-{pool}-{task_name}-{uuid}'.

        Gère l'enregistrement des métriques d'attente et le suivi Watchdog.
        """
        if self._is_shutdown:
            raise RuntimeError("Impossible de soumettre une tâche : ANOThreadPool est arrêté.")

        executor = self.get_executor(pool_name)
        cfg = self._configs[pool_name]
        resolved_stall_timeout = stall_timeout if stall_timeout is not None else cfg.stall_timeout

        # Génération des identifiants et noms
        task_id = uuid.uuid4().hex[:8]
        raw_name = task_name or getattr(fn, "__name__", "task")
        safe_task_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", raw_name)
        full_name = f"ano-{pool_name}-{safe_task_name}-{task_id}"

        record = TaskRecord(
            task_id=task_id,
            pool_name=pool_name,
            task_name=safe_task_name,
            full_name=full_name,
            submit_time=time.monotonic(),
            stall_timeout=resolved_stall_timeout,
        )

        with self._lock:
            metrics = self._metrics[pool_name]
            metrics.record_submit()

        def _worker_wrapper() -> T:
            # 1. Prise en charge par le worker thread
            start_time = time.monotonic()
            record.start_time = start_time
            record.wait_time = max(0.0, start_time - record.submit_time)
            record.thread_ident = threading.get_ident()
            record.thread_native_id = (
                threading.get_native_id() if hasattr(threading, "get_native_id") else None
            )

            # Enregistrement du nom de thread Python et POSIX comm
            orig_thread_name = threading.current_thread().name
            record.thread_name_orig = orig_thread_name
            threading.current_thread().name = full_name
            posix_comm = format_posix_comm_name(pool_name, safe_task_name, task_id)
            set_posix_thread_name(posix_comm)

            # Enregistrement dans les tâches actives & métrique de queue
            with self._lock:
                self._active_tasks[task_id] = record
                metrics.record_start(record.wait_time)

            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                record.error = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                # 2. Clôture de la tâche et métriques d'exécution
                end_time = time.monotonic()
                record.end_time = end_time
                record.exec_duration = max(0.0, end_time - start_time)
                record.completed = record.error is None
                if record.exec_duration >= record.stall_timeout and record.stalled_count == 0:
                    record.stalled_count += 1
                    with self._lock:
                        metrics.stalls_detected += 1

                with self._lock:
                    self._active_tasks.pop(task_id, None)
                    metrics.record_finish(record)

                # Restitution du nom idle du worker
                threading.current_thread().name = f"ano-{pool_name}-idle"
                set_posix_thread_name(f"ano-{pool_name[:3]}-idle")

        raw_future = executor.submit(_worker_wrapper)
        return PoolFuture(raw_future, record)

    async def run_async(
        self,
        pool_name: str,
        fn: Callable[..., T],
        *args: Any,
        task_name: str | None = None,
        stall_timeout: float | None = None,
        **kwargs: Any,
    ) -> T:
        """Exécute une fonction bloquante dans le pool spécifié depuis une coroutine asyncio."""
        future = self.submit(
            pool_name,
            fn,
            *args,
            task_name=task_name,
            stall_timeout=stall_timeout,
            **kwargs,
        )
        return await future

    # ── Context Managers ──────────────────────────────────────────────────────

    def __enter__(self) -> ANOThreadPool:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.shutdown(wait=True, cancel_futures=True)

    @contextlib.contextmanager
    def batch(
        self,
        pool_name: str,
        wait: bool = True,
        timeout: float | None = None,
    ) -> Generator[BatchContext, None, None]:
        """Context manager regroupant des soumissions dans un lot dédié.

        Exemple :
            with pool.batch("disk-io") as b:
                f1 = b.submit(save_file, "a.json", data1)
                f2 = b.submit(save_file, "b.json", data2)
            # Toutes les tâches sont résolues en sortie du bloc
        """
        ctx = BatchContext(self, pool_name)
        try:
            yield ctx
        finally:
            if wait:
                ctx.wait(timeout=timeout)

    @contextlib.contextmanager
    def task_context(
        self,
        pool_name: str,
        task_name: str,
        stall_timeout: float = 5.0,
    ) -> Generator[TaskRecord, None, None]:
        """Instrumente temporairement le thread COURANT avec naming, comm POSIX et watchdog.

        Indispensable pour surveiller des threads tiers ou callbacks PortAudio / Qt.
        """
        task_id = uuid.uuid4().hex[:8]
        safe_task_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", task_name)
        full_name = f"ano-{pool_name}-{safe_task_name}-{task_id}"

        now = time.monotonic()
        record = TaskRecord(
            task_id=task_id,
            pool_name=pool_name,
            task_name=safe_task_name,
            full_name=full_name,
            submit_time=now,
            start_time=now,
            wait_time=0.0,
            stall_timeout=stall_timeout,
            thread_ident=threading.get_ident(),
            thread_native_id=threading.get_native_id() if hasattr(threading, "get_native_id") else None,
        )

        orig_name = threading.current_thread().name
        record.thread_name_orig = orig_name
        threading.current_thread().name = full_name
        set_posix_thread_name(format_posix_comm_name(pool_name, safe_task_name, task_id))

        with self._lock:
            self._active_tasks[task_id] = record
            metrics = self._metrics.get(pool_name)
            if metrics:
                metrics.record_submit()
                metrics.record_start(0.0)

        try:
            yield record
        except Exception as exc:
            record.error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            end_time = time.monotonic()
            record.end_time = end_time
            record.exec_duration = max(0.0, end_time - now)
            record.completed = record.error is None

            with self._lock:
                self._active_tasks.pop(task_id, None)
                metrics = self._metrics.get(pool_name)
                if metrics:
                    metrics.record_finish(record)

            threading.current_thread().name = orig_name
            set_posix_thread_name(f"ano-{pool_name[:3]}-idle")

    # ── Surveillance, Métriques & Arrêt ───────────────────────────────────────

    def get_metrics(self, pool_name: str | None = None) -> dict[str, Any]:
        """Fournit les métriques complètes d'exécution et de queue pour le dashboard."""
        with self._lock:
            active_counts: dict[str, int] = collections.defaultdict(int)
            for task in self._active_tasks.values():
                active_counts[task.pool_name] += 1

            pools_data: dict[str, Any] = {}
            target_pools = [pool_name] if pool_name else list(self._configs.keys())

            total_submitted = 0
            total_completed = 0
            total_failed = 0
            total_active = 0
            total_stalls = 0

            for p in target_pools:
                m = self._metrics.get(p)
                ex = self._executors.get(p)
                if m is None or ex is None:
                    continue

                act = active_counts[p]
                # Estimation de la profondeur de file (submitted - terminés - annulés - actifs)
                queue_depth = max(0, m.tasks_submitted - (m.tasks_completed + m.tasks_failed + m.tasks_cancelled + act))
                pools_data[p] = m.to_dict(active_count=act, queue_depth=queue_depth)

                total_submitted += m.tasks_submitted
                total_completed += m.tasks_completed
                total_failed += m.tasks_failed
                total_active += act
                total_stalls += m.stalls_detected

            return {
                "timestamp": time.time(),
                "uptime_sec": round(time.monotonic() - self._start_time, 2),
                "summary": {
                    "total_active": total_active,
                    "total_submitted": total_submitted,
                    "total_completed": total_completed,
                    "total_failed": total_failed,
                    "total_stalls": total_stalls,
                },
                "pools": pools_data,
            }

    def get_active_tasks(self) -> list[dict[str, Any]]:
        """Liste les tâches actuellement en cours d'exécution avec leur durée écoulée."""
        now = time.monotonic()
        results: list[dict[str, Any]] = []
        with self._lock:
            for task in self._active_tasks.values():
                dur = (now - task.start_time) if task.start_time else 0.0
                results.append({
                    "task_id": task.task_id,
                    "pool_name": task.pool_name,
                    "task_name": task.task_name,
                    "full_name": task.full_name,
                    "running_sec": round(dur, 2),
                    "stall_timeout": task.stall_timeout,
                    "is_stalled": dur >= task.stall_timeout,
                    "thread_ident": task.thread_ident,
                    "thread_native_id": task.thread_native_id,
                })
        return results

    def shutdown(
        self,
        wait: bool = True,
        cancel_futures: bool = True,
        timeout: float = 3.0,
    ) -> None:
        """Arrête proprement l'ensemble des pools et le watchdog sans bloquer l'application."""
        with self._lock:
            if self._is_shutdown:
                return
            self._is_shutdown = True

        logger.info("Arrêt de ANOThreadPool (wait=%s, cancel_futures=%s)...", wait, cancel_futures)

        # 1. Arrêt immédiat du watchdog
        self._watchdog.stop(timeout=0.5)

        # 2. Fermeture de chaque pool spécialisé
        deadline = time.monotonic() + timeout if timeout else None
        for name, executor in list(self._executors.items()):
            rem = max(0.1, deadline - time.monotonic()) if deadline else None
            try:
                executor.shutdown(wait=wait, cancel_futures=cancel_futures, timeout=rem)
            except Exception as exc:
                logger.warning("Exception lors de l'arrêt du pool '%s' : %s", name, exc)

        logger.info("ANOThreadPool arrêté avec succès.")

    def spawn_thread(
        self,
        pool_name: str,
        task_name: str,
        target: Callable[..., Any],
        args: tuple[Any, ...] = (),
        kwargs: dict[str, Any] | None = None,
        daemon: bool = True,
        priority: str = "normal",
        stall_timeout: float = 5.0,
    ) -> threading.Thread:
        """Instancie un thread autonome surveillé par le Watchdog et nommé selon la convention.

        Permet la migration immédiate des `threading.Thread(daemon=True)` dispersés dans l'app.
        """
        kwargs = kwargs or {}
        task_id = uuid.uuid4().hex[:8]
        safe_task_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", task_name)
        full_name = f"ano-{pool_name}-{safe_task_name}-{task_id}"

        def _thread_target() -> Any:
            set_posix_thread_name(format_posix_comm_name(pool_name, safe_task_name, task_id))
            if priority == "realtime":
                configure_realtime_priority(pool_name)

            with self.task_context(pool_name, safe_task_name, stall_timeout=stall_timeout):
                return target(*args, **kwargs)

        t = threading.Thread(
            target=_thread_target,
            name=full_name,
            daemon=daemon,
        )
        t.start()
        return t


# ── Décorateur @run_in_pool ───────────────────────────────────────────────────

def run_in_pool(
    pool_name: str,
    task_name: str | None = None,
    stall_timeout: float = 5.0,
) -> Callable[[Callable[P, R]], Callable[P, PoolFuture[R]]]:
    """Décorateur pour déporter l'exécution d'une fonction bloquante dans un pool dédié.

    Retourne un objet `PoolFuture` utilisable de manière synchrone (`.result()`)
    ou de manière asynchrone (`await`).

    Exemple :
        @run_in_pool("disk-io")
        def load_json(path: str) -> dict:
            with open(path) as f:
                return json.load(f)

        # Usage synchrone :
        fut = load_json("config.json")
        data = fut.result()

        # Usage asynchrone dans asyncio :
        data = await load_json("config.json")
    """

    def decorator(fn: Callable[P, R]) -> Callable[P, PoolFuture[R]]:
        if inspect.iscoroutinefunction(fn):
            raise TypeError(
                f"@run_in_pool ne peut pas être appliqué à la coroutine '{fn.__name__}'. "
                "Ce décorateur est réservé aux fonctions synchrones et bloquantes."
            )

        resolved_task_name = task_name or getattr(fn, "__name__", "task")

        @functools.wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> PoolFuture[R]:
            pool = ANOThreadPool.get_instance()
            return pool.submit(
                pool_name,
                fn,
                *args,
                task_name=resolved_task_name,
                stall_timeout=stall_timeout,
                **kwargs,
            )

        return wrapper

    return decorator


def get_thread_pool() -> ANOThreadPool:
    """Accès rapide au singleton ANOThreadPool."""
    return ANOThreadPool.get_instance()


_SHUTDOWN_HOOKS: list[tuple[str, Callable[[], Any]]] = []
_SHUTDOWN_HOOKS_LOCK = threading.Lock()


def register_shutdown_hook(name: str, stop: Callable[[], Any]) -> None:
    """Enregistre un ``stop()`` appelé par ``shutdown_all`` avant les pools.

    Pour les threads persistants (lecteur d'événements Hyprland, capture
    micro, enrichisseur sémantique, moniteur du lecteur) : ils sont daemon,
    mais un thread bloqué dans un appel C (PortAudio, socket) peut retenir
    l'extinction de l'interpréteur. Les arrêter explicitement d'abord rend la
    fermeture déterministe. Idempotent par nom : un redémarrage du composant
    remplace son ancien hook.
    """
    with _SHUTDOWN_HOOKS_LOCK:
        _SHUTDOWN_HOOKS[:] = [(n, f) for n, f in _SHUTDOWN_HOOKS if n != name]
        _SHUTDOWN_HOOKS.append((name, stop))


def unregister_shutdown_hook(name: str) -> None:
    with _SHUTDOWN_HOOKS_LOCK:
        _SHUTDOWN_HOOKS[:] = [(n, f) for n, f in _SHUTDOWN_HOOKS if n != name]


def run_shutdown_hooks() -> list[str]:
    """Exécute chaque hook une seule fois ; renvoie les noms en échec."""
    with _SHUTDOWN_HOOKS_LOCK:
        hooks = list(reversed(_SHUTDOWN_HOOKS))
        _SHUTDOWN_HOOKS.clear()
    failed: list[str] = []
    for name, stop in hooks:
        try:
            stop()
        except Exception as exc:
            failed.append(name)
            logger.warning("Hook d'arrêt '%s' en échec : %s", name, exc)
    return failed


def shutdown_all(wait: bool = True, cancel_futures: bool = True, timeout: float = 3.0) -> None:
    """Arrêt rapide : hooks des threads persistants, puis tous les pools."""
    run_shutdown_hooks()
    if ANOThreadPool._instance is not None:
        ANOThreadPool._instance.shutdown(wait=wait, cancel_futures=cancel_futures, timeout=timeout)


def lingering_threads() -> list[threading.Thread]:
    """Threads non-daemon encore vivants (hors thread principal)."""
    return [
        t for t in threading.enumerate()
        if t is not threading.main_thread() and t.is_alive() and not t.daemon
    ]


def exit_process_bounded(grace_s: float = 1.0, code: int = 0) -> None:
    """Termine le processus même si un thread refuse de mourir.

    À appeler en toute fin de ``main()`` : les hooks ``atexit`` sont exécutés
    explicitement, les flux vidés, puis ``os._exit`` si un thread non-daemon
    survit encore après ``grace_s``. Le verrou d'instance (``flock``) tombe
    avec le processus et le socket de contrôle est nettoyé au prochain
    démarrage : rien ne traîne.
    """
    deadline = time.monotonic() + max(0.0, grace_s)
    while lingering_threads() and time.monotonic() < deadline:
        time.sleep(0.05)
    stuck = lingering_threads()
    if not stuck:
        return
    logger.warning(
        "Fermeture forcée : %d thread(s) encore vivant(s) — %s",
        len(stuck), ", ".join(t.name for t in stuck[:6]),
    )
    import atexit
    try:
        atexit._run_exitfuncs()
    except Exception:
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    os._exit(code)
