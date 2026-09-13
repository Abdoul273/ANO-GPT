"""Socle d'exécution des actions ANO-GPT.

`core/action_runtime.py` protège le *répartiteur* : il borne un outil vu de
l'extérieur. Ce module protège l'*intérieur* des actions, là où le temps se
perd réellement — un `subprocess` sans délai, un `hyprctl` relancé quinze fois
pour la même question, une boucle d'attente qui tourne à vide.

La machine a deux cœurs et l'audio partage le GIL avec Qt : chaque processus
lancé, chaque milliseconde bloquée dans un fil du pool se paie sur la voix.
D'où les trois règles tenues ici :

1. **Aucun appel n'est illimité.** Tout passe par :func:`run`, qui tue le
   groupe de processus au bout du délai — un enfant bloqué ne retient jamais un
   fil du pool.
2. **On ne redemande pas ce qu'on sait déjà.** :class:`TTLCache` mémorise avec
   fusion des appels concurrents : dix actions qui interrogent Hyprland en même
   temps ne lancent qu'un seul `hyprctl`.
3. **Une action ne fait jamais tomber la session.** :func:`action` rattrape
   tout, journalise, et rend une phrase que la voix peut lire.

Le module ne dépend ni de Qt, ni de Gemini, ni d'une action concrète : il
s'importe et se teste seul.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import logging
import os
import shlex
import shutil
import signal
import subprocess
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence, TypeVar

__all__ = [
    "ProcResult",
    "run",
    "run_json",
    "run_batch",
    "spawn",
    "which",
    "have",
    "TTLCache",
    "memo",
    "invalidate",
    "CircuitBreaker",
    "breaker_for",
    "action",
    "ActionError",
    "stats_snapshot",
    "reset_stats",
    "wait_until",
    "hypr",
    "hypr_json",
    "hypr_clients",
    "hypr_activewindow",
    "hypr_workspaces",
    "hypr_monitors",
    "hypr_invalidate",
]

log = logging.getLogger("action_kit")

# Délai par défaut : assez pour un utilitaire local, trop court pour qu'un
# blocage passe inaperçu. Tout ce qui dépasse doit le déclarer explicitement.
DEFAULT_TIMEOUT = 8.0

# Sur une machine à deux cœurs, laisser dix `hyprctl` partir en parallèle coûte
# plus cher que de les sérialiser. Ce sémaphore borne les processus courts.
# Les travaux longs (ffmpeg, yt-dlp, conversion LibreOffice…) ne prennent pas
# de place : un encodage de vingt minutes ne doit pas bloquer les `hyprctl`.
_PROC_SLOTS = threading.BoundedSemaphore(4)


# ─────────────────────────────────────────────────────────────────────────────
# HTTP : même règle que les processus — aucun appel sans délai.
# ─────────────────────────────────────────────────────────────────────────────

# (connexion, lecture) : un service local muet (ZapZap, Hyprland, un agent MCP
# en panne) échoue en 2 s au lieu de retenir l'action — et le micro — jusqu'au
# plafond du répartiteur.
HTTP_TIMEOUT: tuple[float, float] = (2.0, 5.0)
_http_lock = threading.Lock()
_http_client: Any = None


def http() -> Any:
    """Client ``requests.Session`` partagé, délai ``HTTP_TIMEOUT`` imposé.

    Un ``timeout=`` explicite reste possible pour un appel long connu
    (téléchargement, API distante), mais l'absence de délai n'existe plus.
    Les actions n'appellent jamais ``requests`` directement (voir
    ``tests/test_subprocess_contract.py``).
    """
    global _http_client
    with _http_lock:
        if _http_client is None:
            import requests

            class _Session(requests.Session):
                def request(self, method, url, **kwargs):  # type: ignore[override]
                    if kwargs.get("timeout") is None:
                        kwargs["timeout"] = HTTP_TIMEOUT
                    return super().request(method, url, **kwargs)

            client = _Session()
            client.headers["User-Agent"] = "ANO-GPT/1.0"
            _http_client = client
        return _http_client
_SLOT_MAX_TIMEOUT = 30.0


class _NoSlot:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: Any) -> None:
        return None


_NO_SLOT = _NoSlot()


# ─────────────────────────────────────────────────────────────────────────────
# Résultat de processus
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ProcResult:
    """Issue d'un appel externe. Ne lève jamais : tout est dans les champs."""

    cmd: tuple[str, ...]
    code: int
    out: str = ""
    err: str = ""
    duration: float = 0.0
    timed_out: bool = False
    not_found: bool = False

    @property
    def ok(self) -> bool:
        return self.code == 0 and not self.timed_out and not self.not_found

    def __bool__(self) -> bool:  # `if run(...):` se lit naturellement
        return self.ok

    # Alias `CompletedProcess` : les actions migrées depuis `subprocess.run`
    # gardent leurs lectures `.returncode` / `.stdout` / `.stderr` intactes.
    @property
    def returncode(self) -> int:
        return self.code

    @property
    def stdout(self) -> str:
        return self.out

    @property
    def stderr(self) -> str:
        return self.err

    @property
    def text(self) -> str:
        """Sortie utile : stdout si succès, sinon stderr — jamais None."""
        return (self.out if self.out.strip() else self.err).strip()

    def json(self, default: Any = None) -> Any:
        if not self.ok:
            return default
        try:
            return json.loads(self.out)
        except (json.JSONDecodeError, ValueError):
            return default

    def lines(self) -> list[str]:
        return [ln for ln in self.out.splitlines() if ln.strip()]

    def reason(self) -> str:
        """Explication courte, lisible à voix haute."""
        if self.not_found:
            return f"« {self.cmd[0]} » n'est pas installé"
        if self.timed_out:
            return f"« {self.cmd[0]} » n'a pas répondu à temps"
        if self.code != 0:
            detail = self.err.strip().splitlines()
            return f"« {self.cmd[0]} » a échoué : {detail[0][:160]}" if detail else \
                   f"« {self.cmd[0]} » a échoué (code {self.code})"
        return "ok"


# ─────────────────────────────────────────────────────────────────────────────
# Découverte des binaires (mise en cache : `shutil.which` touche le disque)
# ─────────────────────────────────────────────────────────────────────────────

_which_cache: dict[str, str | None] = {}
_which_lock = threading.Lock()


def which(cmd: str) -> str | None:
    """Chemin absolu d'un binaire, mémorisé pour la durée du processus."""
    with _which_lock:
        if cmd in _which_cache:
            return _which_cache[cmd]
    path = shutil.which(cmd)
    with _which_lock:
        _which_cache[cmd] = path
    return path


def have(*cmds: str) -> bool:
    """Vrai si tous les binaires demandés existent."""
    return all(which(c) is not None for c in cmds)


def forget_which(cmd: str | None = None) -> None:
    """Oublie le cache des binaires (après une installation, par exemple)."""
    with _which_lock:
        if cmd is None:
            _which_cache.clear()
        else:
            _which_cache.pop(cmd, None)


# ─────────────────────────────────────────────────────────────────────────────
# Exécution
# ─────────────────────────────────────────────────────────────────────────────

def _as_argv(cmd: Sequence[str] | str, shell: bool) -> list[str]:
    if isinstance(cmd, str):
        return [cmd] if shell else shlex.split(cmd)
    return [str(part) for part in cmd]


def _kill_tree(proc: subprocess.Popen) -> None:
    """Termine le groupe entier : un `sh -c` tué seul laisse ses enfants."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate()
        except OSError:
            return
    try:
        proc.wait(timeout=1.0)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        pass


def run(
    cmd: Sequence[str] | str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = 0,
    backoff: float = 0.25,
    shell: bool = False,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
    stdin: str | None = None,
    preexec_fn: Callable[[], Any] | None = None,
    capture: bool = True,
    text_errors: str = "replace",
    cache_ttl: float = 0.0,
    quiet: bool = False,
) -> ProcResult:
    """Lance une commande externe sans jamais bloquer indéfiniment.

    Différences avec ``subprocess.run`` qui justifient ce détour :

    * le délai est **obligatoire** et tue le *groupe* de processus, pas
      seulement l'enfant direct — sinon un `xdg-open` figé retient un fil du
      pool réseau jusqu'à la fin de la session ;
    * rien ne remonte sous forme d'exception : une action n'a pas à envelopper
      chaque appel dans un `try` ;
    * ``retries`` couvre les IPC capricieuses (Hyprland pendant un changement
      d'espace de travail, MPRIS pendant le démarrage d'un lecteur) ;
    * ``cache_ttl`` fusionne les rafales identiques — précieux quand plusieurs
      actions interrogent la même source dans le même tour de parole.

    ``capture=False`` est indispensable pour les programmes qui se démonisent
    en gardant les tubes ouverts — `wl-copy` reste vivant pour servir le
    presse-papiers, et attendre la fin de sa sortie bloque jusqu'au délai
    alors que la copie a déjà réussi.
    """
    argv = _as_argv(cmd, shell)
    if not argv:
        return ProcResult(cmd=(), code=-1, err="commande vide")

    if cache_ttl > 0:
        key = ("proc", tuple(argv), shell, cwd)
        cached = _proc_cache.get(key)
        if cached is not None:
            return cached

    binary = argv[0]
    if not shell and which(binary) is None:
        res = ProcResult(cmd=tuple(argv), code=127, err=f"{binary} introuvable",
                         not_found=True)
        if not quiet:
            log.debug("binaire absent : %s", binary)
        return res

    full_env = None
    if env is not None:
        full_env = {**os.environ, **env}

    attempt = 0
    result: ProcResult
    while True:
        started = time.monotonic()
        proc = None
        try:
            with (_PROC_SLOTS if timeout <= _SLOT_MAX_TIMEOUT else _NO_SLOT):
                proc = subprocess.Popen(
                    argv if not shell else argv[0],
                    shell=shell,
                    cwd=cwd,
                    env=full_env,
                    stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                    stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                    stderr=subprocess.PIPE if capture else subprocess.DEVNULL,
                    text=True,
                    errors=text_errors,
                    # Groupe dédié : indispensable pour tuer toute la descendance.
                    start_new_session=True,
                    preexec_fn=preexec_fn,
                )
                try:
                    out, err = proc.communicate(input=stdin, timeout=timeout)
                    result = ProcResult(
                        cmd=tuple(argv), code=proc.returncode,
                        out=out or "", err=err or "",
                        duration=time.monotonic() - started,
                    )
                except subprocess.TimeoutExpired:
                    _kill_tree(proc)
                    out, err = "", ""
                    try:
                        out, err = proc.communicate(timeout=1.0)
                    except (subprocess.TimeoutExpired, ValueError, OSError):
                        pass
                    result = ProcResult(
                        cmd=tuple(argv), code=-signal.SIGKILL,
                        out=out or "", err=err or f"délai dépassé ({timeout:g} s)",
                        duration=time.monotonic() - started, timed_out=True,
                    )
        except FileNotFoundError:
            result = ProcResult(cmd=tuple(argv), code=127,
                                err=f"{binary} introuvable", not_found=True,
                                duration=time.monotonic() - started)
        except PermissionError as exc:
            result = ProcResult(cmd=tuple(argv), code=126, err=str(exc),
                                duration=time.monotonic() - started)
        except OSError as exc:
            result = ProcResult(cmd=tuple(argv), code=-1, err=str(exc),
                                duration=time.monotonic() - started)

        _record(f"proc:{binary}", result.duration, result.ok)

        # Un binaire absent ou refusé ne guérit pas en réessayant.
        if result.ok or result.not_found or attempt >= retries:
            break
        attempt += 1
        time.sleep(backoff * attempt)

    if not result.ok and not quiet:
        log.debug("échec %s → %s", " ".join(argv[:3]), result.reason())

    if cache_ttl > 0 and result.ok:
        _proc_cache.set(("proc", tuple(argv), shell, cwd), result, cache_ttl)
    return result


def run_json(
    cmd: Sequence[str] | str,
    *,
    default: Any = None,
    timeout: float = DEFAULT_TIMEOUT,
    **kw: Any,
) -> Any:
    """`run` + désérialisation JSON, `default` si quoi que ce soit rate."""
    return run(cmd, timeout=timeout, **kw).json(default)


def run_batch(
    cmds: Iterable[Sequence[str] | str],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    workers: int = 3,
    **kw: Any,
) -> list[ProcResult]:
    """Plusieurs commandes indépendantes, en parallèle borné.

    Deux cœurs : au-delà de trois processus simultanés on ne gagne plus rien et
    on vole du temps CPU au fil audio.
    """
    items = list(cmds)
    if not items:
        return []
    if len(items) == 1:
        return [run(items[0], timeout=timeout, **kw)]

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=min(workers, len(items)),
                            thread_name_prefix="kit-proc") as pool:
        return list(pool.map(lambda c: run(c, timeout=timeout, **kw), items))


def spawn(
    cmd: Sequence[str] | str,
    *,
    shell: bool = False,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
    detach: bool = True,
) -> int | None:
    """Démarre un programme et rend la main immédiatement.

    Pour lancer une application, pas pour lire une sortie. Session dédiée et
    flux vers /dev/null : le processus survit à ANO-GPT et ne devient pas
    zombie faute de `wait` (`start_new_session` le réattache à init).
    """
    argv = _as_argv(cmd, shell)
    if not argv:
        return None
    if not shell and which(argv[0]) is None:
        log.debug("lancement impossible, binaire absent : %s", argv[0])
        return None

    full_env = {**os.environ, **env} if env is not None else None
    try:
        proc = subprocess.Popen(
            argv if not shell else argv[0],
            shell=shell,
            cwd=cwd,
            env=full_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=detach,
        )
        return proc.pid
    except (OSError, ValueError) as exc:
        log.debug("lancement impossible %s : %s", argv[0], exc)
        return None


def wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float = 3.0,
    interval: float = 0.05,
    grow: float = 1.4,
    max_interval: float = 0.4,
) -> bool:
    """Attend une condition sans boucle serrée.

    Remplace les `time.sleep(1)` fixes qui parsèment les actions : on rend la
    main dès que la fenêtre est apparue, et l'intervalle s'allonge si l'attente
    dure — deux cœurs ne supportent pas un sondage à 20 Hz.
    """
    deadline = time.monotonic() + timeout
    delay = interval
    while True:
        try:
            if predicate():
                return True
        except Exception:  # une sonde qui rate ne doit pas casser l'attente
            log.debug("sonde d'attente en échec", exc_info=True)
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(delay, max(0.0, deadline - time.monotonic())))
        delay = min(delay * grow, max_interval)


# ─────────────────────────────────────────────────────────────────────────────
# Cache TTL avec fusion des appels concurrents
# ─────────────────────────────────────────────────────────────────────────────

class TTLCache:
    """Cache borné, à durée de vie, sûr entre fils.

    La fusion des appels en vol (*single-flight*) est le point important : sans
    elle, cinq actions déclenchées dans le même tour lancent cinq `hyprctl`
    identiques. Avec elle, une seule paie, les autres attendent le résultat.
    """

    def __init__(self, maxsize: int = 256) -> None:
        self._data: dict[Any, tuple[Any, float]] = {}
        self._inflight: dict[Any, threading.Event] = {}
        self._lock = threading.Lock()
        self._maxsize = maxsize
        self.hits = 0
        self.misses = 0

    def get(self, key: Any) -> Any | None:
        now = time.monotonic()
        with self._lock:
            entry = self._data.get(key)
            if entry is not None:
                value, expiry = entry
                if expiry > now:
                    self.hits += 1
                    return value
                del self._data[key]
            self.misses += 1
        return None

    def set(self, key: Any, value: Any, ttl: float) -> None:
        if ttl <= 0:
            return
        with self._lock:
            if len(self._data) >= self._maxsize:
                # Purge des entrées mortes ; sinon on sacrifie la plus ancienne.
                now = time.monotonic()
                dead = [k for k, (_, exp) in self._data.items() if exp <= now]
                for k in dead:
                    del self._data[k]
                if len(self._data) >= self._maxsize:
                    oldest = min(self._data, key=lambda k: self._data[k][1])
                    del self._data[oldest]
            self._data[key] = (value, time.monotonic() + ttl)

    def get_or_call(self, key: Any, factory: Callable[[], Any], ttl: float) -> Any:
        """Valeur mémorisée, calculée une seule fois même sous rafale."""
        hit = self.get(key)
        if hit is not None:
            return hit

        with self._lock:
            waiter = self._inflight.get(key)
            if waiter is None:
                waiter = threading.Event()
                self._inflight[key] = waiter
                owner = True
            else:
                owner = False

        if not owner:
            # Un autre fil calcule déjà : on attend sa réponse plutôt que de
            # lancer le même travail en double.
            waiter.wait(timeout=30.0)
            hit = self.get(key)
            if hit is not None:
                return hit
            return factory()

        try:
            value = factory()
            self.set(key, value, ttl)
            return value
        finally:
            with self._lock:
                self._inflight.pop(key, None)
            waiter.set()

    def invalidate(self, key: Any = None) -> None:
        with self._lock:
            if key is None:
                self._data.clear()
            else:
                self._data.pop(key, None)

    def invalidate_prefix(self, prefix: Any) -> None:
        """Oublie toutes les clés tuple commençant par `prefix`."""
        with self._lock:
            for k in [k for k in self._data
                      if isinstance(k, tuple) and k and k[0] == prefix]:
                del self._data[k]

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"entries": len(self._data), "hits": self.hits,
                    "misses": self.misses}


_proc_cache = TTLCache(maxsize=192)
_memo_cache = TTLCache(maxsize=512)

F = TypeVar("F", bound=Callable[..., Any])


def memo(ttl: float = 30.0, *, key: Callable[..., Any] | None = None) -> Callable[[F], F]:
    """Mémorise le résultat d'une fonction pendant `ttl` secondes.

    À réserver aux lectures (liste des fenêtres, applications installées,
    position GPS). Les arguments doivent être hachables, sinon la clé retombe
    sur leur `repr`.
    """
    def decorator(func: F) -> F:
        name = f"{func.__module__}.{func.__qualname__}"

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if key is not None:
                sig = key(*args, **kwargs)
            else:
                try:
                    hash((args, tuple(sorted(kwargs.items()))))
                    sig = (args, tuple(sorted(kwargs.items())))
                except TypeError:
                    sig = (repr(args), repr(sorted(kwargs.items())))
            return _memo_cache.get_or_call(("memo", name, sig),
                                           lambda: func(*args, **kwargs), ttl)

        wrapper._memo_name = name  # type: ignore[attr-defined]
        return wrapper  # type: ignore[return-value]
    return decorator


def invalidate(target: Any = None) -> None:
    """Vide les caches : tout, une fonction mémorisée, ou un préfixe."""
    if target is None:
        _memo_cache.invalidate()
        _proc_cache.invalidate()
        return
    name = getattr(target, "_memo_name", target)
    with _memo_cache._lock:
        for k in [k for k in _memo_cache._data
                  if isinstance(k, tuple) and len(k) > 1 and k[1] == name]:
            del _memo_cache._data[k]


# ─────────────────────────────────────────────────────────────────────────────
# Coupe-circuit
# ─────────────────────────────────────────────────────────────────────────────

class CircuitBreaker:
    """Cesse d'appeler ce qui échoue en boucle.

    Un service mort répond souvent *lentement* : réessayer à chaque tour, c'est
    faire attendre l'utilisateur pour rien. Après `threshold` échecs, le
    circuit s'ouvre pour `cooldown` secondes, puis laisse passer un appel
    d'essai.
    """

    def __init__(self, name: str, *, threshold: int = 3, cooldown: float = 45.0) -> None:
        self.name = name
        self.threshold = threshold
        self.cooldown = cooldown
        self._failures = 0
        self._open_until = 0.0
        self._lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        with self._lock:
            return time.monotonic() < self._open_until

    def record(self, ok: bool) -> None:
        with self._lock:
            if ok:
                self._failures = 0
                self._open_until = 0.0
                return
            self._failures += 1
            if self._failures >= self.threshold:
                self._open_until = time.monotonic() + self.cooldown
                log.info("circuit ouvert pour %s (%d échecs)", self.name,
                         self._failures)

    def call(self, func: Callable[[], Any], *, fallback: Any = None) -> Any:
        if self.is_open:
            return fallback
        try:
            result = func()
        except Exception:
            self.record(False)
            raise
        self.record(bool(result))
        return result

    def reset(self) -> None:
        with self._lock:
            self._failures = 0
            self._open_until = 0.0


_breakers: dict[str, CircuitBreaker] = {}
_breakers_lock = threading.Lock()


def breaker_for(name: str, **kw: Any) -> CircuitBreaker:
    with _breakers_lock:
        brk = _breakers.get(name)
        if brk is None:
            brk = CircuitBreaker(name, **kw)
            _breakers[name] = brk
        return brk


# ─────────────────────────────────────────────────────────────────────────────
# Statistiques
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _Stat:
    calls: int = 0
    errors: int = 0
    total: float = 0.0
    worst: float = 0.0
    samples: list[float] = field(default_factory=list)


_stats: dict[str, _Stat] = {}
_stats_lock = threading.Lock()


def _record(name: str, duration: float, ok: bool) -> None:
    with _stats_lock:
        st = _stats.get(name)
        if st is None:
            st = _Stat()
            _stats[name] = st
        st.calls += 1
        if not ok:
            st.errors += 1
        st.total += duration
        st.worst = max(st.worst, duration)
        st.samples.append(duration)
        if len(st.samples) > 100:
            del st.samples[:-100]


def stats_snapshot() -> dict[str, dict[str, float]]:
    """Vue instantanée des coûts, pour le tableau de bord et le diagnostic."""
    with _stats_lock:
        out: dict[str, dict[str, float]] = {}
        for name, st in _stats.items():
            ordered = sorted(st.samples)
            p95 = ordered[int(len(ordered) * 0.95)] if ordered else 0.0
            out[name] = {
                "calls": st.calls,
                "errors": st.errors,
                "avg_ms": (st.total / st.calls * 1000) if st.calls else 0.0,
                "p95_ms": p95 * 1000,
                "worst_ms": st.worst * 1000,
            }
        return out


def reset_stats() -> None:
    with _stats_lock:
        _stats.clear()


# ─────────────────────────────────────────────────────────────────────────────
# Décorateur d'action
# ─────────────────────────────────────────────────────────────────────────────

class ActionError(Exception):
    """Échec explicite : le message part tel quel vers la voix."""


def action(
    name: str,
    *,
    fallback: str = "",
    reraise: bool = False,
) -> Callable[[F], F]:
    """Rend un point d'entrée d'action incassable et mesuré.

    Une action qui lève met fin au tour de parole : l'utilisateur a parlé, et
    plus rien ne répond. Ici, l'exception devient une phrase lisible, la trace
    part dans le journal, et la durée alimente les statistiques.
    """
    def decorator(func: F) -> F:
        def _handle(exc: BaseException) -> str:
            if isinstance(exc, ActionError):
                return str(exc)
            log.warning("action %s en échec : %s", name, exc)
            log.debug("%s", traceback.format_exc())
            print(f"[{name}] ⚠️ {type(exc).__name__}: {str(exc)[:160]}")
            return fallback or _humanize(name, exc)

        if inspect.iscoroutinefunction(func):
            # Une action asynchrone renvoie une coroutine : l'envelopper de
            # façon synchrone laisserait l'exception se produire plus tard,
            # hors de portée du filet.
            @functools.wraps(func)
            async def awrapper(*args: Any, **kwargs: Any) -> Any:
                started = time.monotonic()
                ok = True
                try:
                    return await func(*args, **kwargs)
                except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
                    raise
                except Exception as exc:
                    ok = False
                    if reraise and not isinstance(exc, ActionError):
                        raise
                    return _handle(exc)
                finally:
                    _record(f"action:{name}", time.monotonic() - started, ok)

            return awrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            started = time.monotonic()
            ok = True
            try:
                return func(*args, **kwargs)
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:
                ok = False
                if reraise and not isinstance(exc, ActionError):
                    raise
                return _handle(exc)
            finally:
                _record(f"action:{name}", time.monotonic() - started, ok)

        return wrapper  # type: ignore[return-value]
    return decorator


def _humanize(name: str, exc: Exception) -> str:
    """Traduit une exception en phrase que l'assistant peut dire."""
    if isinstance(exc, TimeoutError):
        return f"{name} a mis trop de temps, je n'ai pas pu terminer."
    if isinstance(exc, FileNotFoundError):
        return "Je n'ai pas trouvé ce fichier."
    if isinstance(exc, PermissionError):
        return "Je n'ai pas les droits nécessaires pour ça."
    if isinstance(exc, (ConnectionError, OSError)):
        return "La connexion a échoué, réessaie dans un instant."
    return f"{name} a échoué : {str(exc)[:120]}"


# ─────────────────────────────────────────────────────────────────────────────
# Hyprland — un seul client, partagé
# ─────────────────────────────────────────────────────────────────────────────

# 87 appels à `hyprctl` étaient dispersés dans les actions, chacun payant un
# processus. Les lectures passent désormais par ici.
#
# La durée du cache est délibérément **plus courte que l'intervalle des boucles
# d'attente** des actions (0,15 s en général). Le gain visé n'est pas de garder
# un état longtemps — ce serait faux dès qu'une fenêtre s'ouvre toute seule —
# mais de fusionner les lectures quasi simultanées : `close_app` interroge la
# liste des fenêtres trois fois dans la même passe, et plusieurs actions la
# demandent dans le même tour de parole. Toute écriture invalide le cache, et
# la fusion des appels concurrents joue quelle que soit cette durée.
_HYPR_TTL = 0.12


def _hypr_available() -> bool:
    return which("hyprctl") is not None and bool(
        os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    )


def hypr(*args: str, timeout: float = 3.0, retries: int = 1) -> ProcResult:
    """Commande Hyprland (dispatch, keyword…). Écriture : jamais mise en cache.

    La reprise ne couvre que les échecs *transitoires* — compositeur injoignable
    ou muet pendant un changement d'espace de travail. Quand Hyprland répond en
    expliquant son refus (dispatcher inconnu, syntaxe Lua invalide), l'erreur
    est déterministe : réessayer ne ferait que payer un second processus, et le
    repli legacy de `window_instances` s'appuie précisément sur ce refus.
    """
    if not _hypr_available():
        return ProcResult(cmd=("hyprctl",), code=127,
                          err="Hyprland indisponible", not_found=True)
    res = run(["hyprctl", *args], timeout=timeout)
    if not res.ok and retries > 0 and not res.out.strip():
        res = run(["hyprctl", *args], timeout=timeout, retries=retries - 1,
                  backoff=0.15)
    # Toute écriture change l'état : les lectures mémorisées deviennent fausses.
    hypr_invalidate()
    return res


def hypr_json(*args: str, default: Any = None, ttl: float = _HYPR_TTL,
              timeout: float = 3.0) -> Any:
    """Lecture JSON d'Hyprland, mémorisée et fusionnée entre fils."""
    if not _hypr_available():
        return default

    def _fetch() -> Any:
        return run(["hyprctl", "-j", *args], timeout=timeout, retries=1).json(default)

    if ttl <= 0:
        return _fetch()
    value = _proc_cache.get_or_call(("hypr", args), _fetch, ttl)
    return default if value is None else value


def hypr_batch(*commands: str, timeout: float = 4.0) -> ProcResult:
    """Plusieurs commandes en **un seul** processus.

    `hyprctl --batch "dispatch a ; dispatch b"` remplace deux lancements : sur
    deux cœurs, l'économie est directement du temps rendu à l'audio.
    """
    if not commands:
        return ProcResult(cmd=("hyprctl",), code=0)
    if not _hypr_available():
        return ProcResult(cmd=("hyprctl",), code=127,
                          err="Hyprland indisponible", not_found=True)
    payload = " ; ".join(c.strip() for c in commands if c.strip())
    res = run(["hyprctl", "--batch", payload], timeout=timeout, retries=1)
    hypr_invalidate()
    return res


def hypr_clients(default: list | None = None) -> list[dict]:
    """Fenêtres ouvertes. Appelé partout : c'est la lecture la plus rentable."""
    value = hypr_json("clients", default=default if default is not None else [])
    return value if isinstance(value, list) else []


def hypr_activewindow() -> dict:
    value = hypr_json("activewindow", default={})
    return value if isinstance(value, dict) else {}


def hypr_workspaces() -> list[dict]:
    value = hypr_json("workspaces", default=[])
    return value if isinstance(value, list) else []


def hypr_monitors() -> list[dict]:
    value = hypr_json("monitors", default=[])
    return value if isinstance(value, list) else []


def hypr_invalidate() -> None:
    """À appeler après toute action qui déplace ou ferme une fenêtre."""
    _proc_cache.invalidate_prefix("hypr")
