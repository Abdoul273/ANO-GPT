"""Journal unique, structuré, et qui n'attend jamais le disque.

Trente-cinq modules appellent ``logging.getLogger`` sous quatre familles de
noms différentes, et aucun collecteur n'était installé : tout partait vers le
gestionnaire de dernier recours, c'est-à-dire nulle part dès que l'application
n'est pas lancée depuis un terminal. Une panne réelle devenait « l'outil a
rencontré une erreur », sans trace, sans pile d'appel, sans heure.

Trois partis pris :

* **JSONL** — une ligne par événement, lisible par ``jq`` comme par un script
  de diagnostic. Un journal qu'on ne peut pas interroger ne sert qu'à grossir.
* **Écriture déportée** — ``QueueHandler`` dépose, un thread unique écrit. Sur
  deux cœurs où Qt et l'audio partagent le GIL, un journal qui écrit depuis le
  thread audio est un journal qui hache la voix.
* **Les exceptions non rattrapées y entrent aussi** — 36 threads démons
  tournent ici ; sans crochet, celui qui meurt le fait en silence.
"""
from __future__ import annotations

import atexit
import json
import asyncio
import logging
import logging.handlers
import queue
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "anogpt.jsonl"

MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 3

# Bibliothèques bavardes : leur DEBUG noierait le signal utile.
_NOISY = (
    "urllib3", "httpx", "httpcore", "asyncio", "PIL", "matplotlib",
    "google", "google_genai", "websockets", "comtypes", "numba", "filelock",
)

# Champs déjà portés par LogRecord : tout le reste est un extra de l'appelant.
_STANDARD = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message", "asctime", "taskName",
}

_listener: logging.handlers.QueueListener | None = None
_lock = threading.Lock()


class JsonlFormatter(logging.Formatter):
    """Un événement, une ligne JSON — traçable et interrogeable."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created))
                  + f".{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "where": f"{record.module}:{record.lineno}",
            "thread": record.threadName,
        }
        if record.exc_info:
            payload["exc"] = "".join(traceback.format_exception(*record.exc_info)).strip()
        for key, value in record.__dict__.items():
            if key in _STANDARD or key.startswith("_"):
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = repr(value)
        return json.dumps(payload, ensure_ascii=False)


class _PassthroughQueueHandler(logging.handlers.QueueHandler):
    """Dépose l'enregistrement intact.

    ``QueueHandler.prepare`` formate le message et efface ``exc_info`` : conçu
    pour une file inter-processus, il détruit ici exactement ce qu'on veut
    garder — la pile d'appel comme champ séparé. Le collecteur vit dans le même
    processus, rien n'a besoin d'être sérialisé.
    """

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        return record


class _ConsoleFormatter(logging.Formatter):
    """Terminal : court et lisible. Le détail complet vit dans le JSONL."""

    def __init__(self) -> None:
        super().__init__("%(levelname)s %(name)s — %(message)s")


def setup_logging(level: int = logging.INFO, *, console_level: int = logging.WARNING,
                  directory: Path | None = None) -> Path:
    """Installe le collecteur unique. Idempotent : rappeler ne double rien."""
    global _listener
    with _lock:
        if _listener is not None:
            return LOG_FILE

        log_dir = Path(directory) if directory else LOG_DIR
        log_dir.mkdir(parents=True, exist_ok=True)
        destination = log_dir / LOG_FILE.name

        file_handler = logging.handlers.RotatingFileHandler(
            destination, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT,
            encoding="utf-8", delay=True,
        )
        file_handler.setFormatter(JsonlFormatter())
        file_handler.setLevel(level)

        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(_ConsoleFormatter())
        console.setLevel(console_level)

        # La file est non bornée à dessein : perdre un événement de panne coûte
        # bien plus cher que quelques kilo-octets en mémoire.
        records: queue.Queue = queue.Queue(-1)
        root = logging.getLogger()
        root.setLevel(level)
        for handler in list(root.handlers):
            root.removeHandler(handler)
        root.addHandler(_PassthroughQueueHandler(records))

        _listener = logging.handlers.QueueListener(
            records, file_handler, console, respect_handler_level=True,
        )
        _listener.start()

        for name in _NOISY:
            logging.getLogger(name).setLevel(logging.WARNING)

        _install_exception_hooks()
        atexit.register(shutdown_logging)
        logging.getLogger("anogpt.observability").info(
            "journal ouvert", extra={"file": str(destination)}
        )
        return destination


def shutdown_logging() -> None:
    """Vide la file avant la sortie : un journal tronqué ment sur la fin."""
    global _listener
    with _lock:
        listener, _listener = _listener, None
    if listener is not None:
        try:
            listener.stop()
        except Exception:
            pass


def _install_exception_hooks() -> None:
    """Ce qui meurt sans être rattrapé doit laisser une trace."""
    logger = logging.getLogger("anogpt.crash")

    previous_hook = sys.excepthook

    def _on_uncaught(exc_type, exc, tb) -> None:
        if not issubclass(exc_type, KeyboardInterrupt):
            logger.critical("exception non rattrapée", exc_info=(exc_type, exc, tb))
        previous_hook(exc_type, exc, tb)

    sys.excepthook = _on_uncaught

    def _on_thread_crash(args) -> None:
        # 36 threads démons tournent dans ANO-GPT : sans ce crochet, celui qui
        # meurt emporte silencieusement sa fonction (veille, relais, watcher).
        if issubclass(args.exc_type, SystemExit):
            return
        logger.critical(
            "thread interrompu par une exception",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            extra={"thread_name": getattr(args.thread, "name", "?")},
        )

    threading.excepthook = _on_thread_crash


def install_asyncio_handler(loop) -> None:
    """Une tâche asyncio qui meurt sans être attendue doit se voir aussi."""
    logger = logging.getLogger("anogpt.crash")

    def _handler(_loop, context: dict) -> None:
        exc = context.get("exception")
        logger.error(
            context.get("message", "erreur asyncio"),
            exc_info=exc if exc is not None else False,
            extra={"task": str(context.get("future") or context.get("task") or "")[:200]},
        )

    loop.set_exception_handler(_handler)


def tool_failure(tool: str, exc: BaseException, *, message: str = "",
                 duration_ms: float | None = None, args: Any = None) -> None:
    """Trace complète d'un échec d'outil, à côté de la phrase lue à l'utilisateur.

    Le modèle et l'utilisateur reçoivent une phrase courte et exploitable ; la
    pile d'appel, elle, doit rester quelque part — sinon « l'outil a rencontré
    une erreur » est tout ce qu'il reste pour diagnostiquer.
    """
    try:
        from core import incident_log
        # Un délai dépassé ou un circuit ouvert est déjà dit par la voix au
        # moment même ; l'annoncer une seconde fois avec « dis corrige »
        # n'aurait pas de sens (rien à corriger dans le code).
        transient = isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or \
            type(exc).__name__ in {"ActionCircuitOpen", "ActionQueueTimeout", "ActionRuntimeError"}
        incident_log.record(
            tool, exc, message=message, announce=not transient,
            extra={
                "arg_keys": sorted(args) if isinstance(args, dict) else None,
                "error_type": type(exc).__name__,
            },
        )
    except Exception:
        pass
    logging.getLogger("anogpt.tools").error(
        f"échec de l'outil {tool}",
        exc_info=exc,
        extra={
            "tool": tool,
            "spoken": message[:300],
            "error_type": type(exc).__name__,
            "error": " ".join(str(exc).split())[:400],
            "duration_ms": round(duration_ms, 1) if duration_ms is not None else None,
            "arg_keys": sorted(args) if isinstance(args, dict) else None,
        },
    )
