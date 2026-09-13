"""Fermeture déterministe : hooks d'arrêt et sortie bornée."""

from __future__ import annotations

import threading
import time

from core import thread_pool as tp


def test_hooks_run_once_in_reverse_order_and_survive_failures(monkeypatch):
    monkeypatch.setattr(tp, "_SHUTDOWN_HOOKS", [])
    order: list[str] = []
    tp.register_shutdown_hook("a", lambda: order.append("a"))
    tp.register_shutdown_hook("b", lambda: 1 / 0)
    tp.register_shutdown_hook("c", lambda: order.append("c"))
    tp.register_shutdown_hook("a", lambda: order.append("a2"))  # remplace « a »

    assert tp.run_shutdown_hooks() == ["b"]
    assert order == ["a2", "c"]
    assert tp.run_shutdown_hooks() == []  # déjà consommés


def test_shutdown_all_runs_hooks(monkeypatch):
    monkeypatch.setattr(tp, "_SHUTDOWN_HOOKS", [])
    stopped = threading.Event()
    tp.register_shutdown_hook("watcher", stopped.set)
    monkeypatch.setattr(tp.ANOThreadPool, "_instance", None)
    tp.shutdown_all(wait=False)
    assert stopped.is_set()


def test_exit_process_bounded_returns_when_no_thread_lingers(monkeypatch):
    monkeypatch.setattr(tp.os, "_exit", lambda code: (_ for _ in ()).throw(AssertionError("_exit appelé")))
    tp.exit_process_bounded(grace_s=0.1)


def test_exit_process_bounded_forces_exit_on_stuck_thread(monkeypatch):
    release = threading.Event()
    stuck = threading.Thread(target=release.wait, name="stuck-non-daemon", daemon=False)
    stuck.start()
    exited: list[int] = []
    monkeypatch.setattr(tp.os, "_exit", exited.append)
    monkeypatch.setattr(tp.atexit if hasattr(tp, "atexit") else __import__("atexit"), "_run_exitfuncs", lambda: None)
    started = time.monotonic()
    try:
        tp.exit_process_bounded(grace_s=0.2, code=3)
    finally:
        release.set()
        stuck.join(1.0)
    assert exited == [3]
    assert time.monotonic() - started < 2.0


def test_persistent_watchers_register_a_stop_hook(monkeypatch):
    monkeypatch.setattr(tp, "_SHUTDOWN_HOOKS", [])
    from core.semantic_enricher import _DaemonEnrichmentWorker as SemanticEnricher

    enricher = SemanticEnricher()
    try:
        assert any(n == "semantic-enricher" for n, _ in tp._SHUTDOWN_HOOKS)
    finally:
        tp.run_shutdown_hooks()
        enricher._thread.join(1.0)
