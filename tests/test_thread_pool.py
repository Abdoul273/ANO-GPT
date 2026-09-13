"""Tests unitaires pour le gestionnaire centralisé de threads ANO-GPT (core/thread_pool.py)."""

from __future__ import annotations

import asyncio
import os
import threading
import time
import pytest

from core.thread_pool import (
    ANOThreadPool,
    PoolFuture,
    format_posix_comm_name,
    run_in_pool,
)


@pytest.fixture
def pool():
    """Fixture fournissant une instance isolée de ANOThreadPool fermée après chaque test."""
    instance = ANOThreadPool()
    yield instance
    instance.shutdown(wait=True, cancel_futures=True, timeout=1.0)


def test_specialized_pools_initialization(pool: ANOThreadPool):
    """Vérifie la création des 4 pools avec leurs limites et configurations strictes."""
    metrics = pool.get_metrics()
    pools = metrics["pools"]

    assert "audio-io" in pools
    assert "disk-io" in pools
    assert "compute-light" in pools
    assert "network-heavy" in pools

    # Limites strictes
    assert pools["audio-io"]["max_workers"] == 1
    assert pools["disk-io"]["max_workers"] == 3
    assert pools["compute-light"]["max_workers"] == max(1, min(4, os.cpu_count() or 1))
    assert pools["network-heavy"]["max_workers"] >= 4


def test_strict_naming_convention(pool: ANOThreadPool):
    """Vérifie la convention 'ano-{pool}-{task_name}-{uuid}' pendant l'exécution."""
    def worker_fn():
        current_name = threading.current_thread().name
        native_id = threading.get_native_id() if hasattr(threading, "get_native_id") else None
        return current_name, native_id

    fut = pool.submit("disk-io", worker_fn, task_name="read_sqlite")
    thread_name, native_id = fut.result()

    # ano-{pool}-{task_name}-{uuid}
    assert thread_name.startswith("ano-disk-io-read_sqlite-")
    # UUID suffix hex (8 chars)
    uuid_part = thread_name.split("-")[-1]
    assert len(uuid_part) == 8


def test_posix_comm_formatting():
    """Vérifie que le nom POSIX Linux respecte la limite de 15 caractères."""
    comm_audio = format_posix_comm_name("audio-io", "playback_pcm", "12345678")
    assert len(comm_audio) <= 15
    assert comm_audio.startswith("ano-aud-")

    comm_disk = format_posix_comm_name("disk-io", "sqlite_query", "abcdef12")
    assert len(comm_disk) <= 15
    assert comm_disk.startswith("ano-dsk-")


def test_metrics_queue_and_execution_duration(pool: ANOThreadPool):
    """Vérifie l'enregistrement des métriques d'attente et de durée d'exécution."""
    def sample_work(delay: float):
        time.sleep(delay)
        return 42

    fut = pool.submit("disk-io", sample_work, 0.05, task_name="quick_job")
    res = fut.result()
    assert res == 42

    metrics = pool.get_metrics("disk-io")
    disk_data = metrics["pools"]["disk-io"]

    assert disk_data["tasks_submitted"] == 1
    assert disk_data["tasks_completed"] == 1
    assert disk_data["tasks_failed"] == 0
    assert disk_data["wait_time"]["total_sec"] >= 0.0
    assert disk_data["exec_duration"]["total_sec"] >= 0.04
    assert len(disk_data) > 0


def test_watchdog_blocking_task_detection(pool: ANOThreadPool):
    """Vérifie la détection d'une tâche bloquante (> seuil stall) avec log et comptage."""
    def slow_work():
        time.sleep(0.6)
        return "finished"

    # Seuil stall réduit à 0.2s pour le test unitaire
    fut = pool.submit("compute-light", slow_work, task_name="heavy_fft", stall_timeout=0.2)
    time.sleep(0.35)

    active_tasks = pool.get_active_tasks()
    assert len(active_tasks) >= 1
    stalled_found = any(t["is_stalled"] for t in active_tasks)
    assert stalled_found

    assert fut.result() == "finished"

    metrics = pool.get_metrics("compute-light")
    assert metrics["pools"]["compute-light"]["stalls_detected"] >= 1


def test_decorator_sync_and_async():
    """Vérifie le fonctionnement du décorateur @run_in_pool en sync et en async."""
    @run_in_pool("compute-light", task_name="multiply")
    def sync_compute(a: int, b: int) -> int:
        return a * b

    # Appel synchrone classique
    fut = sync_compute(6, 7)
    assert isinstance(fut, PoolFuture)
    assert fut.result() == 42

    # Appel asynchrone dans une boucle asyncio
    async def async_caller():
        result = await sync_compute(8, 9)
        return result

    assert asyncio.run(async_caller()) == 72


def test_decorator_rejects_coroutine():
    """Vérifie que @run_in_pool lève TypeError si appliqué par erreur à une coroutine."""
    with pytest.raises(TypeError, match="ne peut pas être appliqué à la coroutine"):
        @run_in_pool("disk-io")
        async def invalid_async():
            pass


def test_batch_context_manager(pool: ANOThreadPool):
    """Vérifie le batch context manager et l'attente synchronisée de plusieurs tâches."""
    with pool.batch("disk-io") as batch:
        b1 = batch.submit(lambda x: x + 1, 10, task_name="task_a")
        b2 = batch.submit(lambda x: x * 2, 10, task_name="task_b")

    assert b1.result() == 11
    assert b2.result() == 20
    assert batch.wait() == [11, 20]


def test_task_context_manager(pool: ANOThreadPool):
    """Vérifie l'instrumentation temporaire du thread courant via task_context."""
    orig_name = threading.current_thread().name

    with pool.task_context("audio-io", "mic_callback", stall_timeout=1.0) as record:
        assert threading.current_thread().name.startswith("ano-audio-io-mic_callback-")
        assert record.pool_name == "audio-io"

    # Vérifie la restitution du nom original
    assert threading.current_thread().name == orig_name


def test_clean_shutdown_cancellation(pool: ANOThreadPool):
    """Vérifie que shutdown(cancel_futures=True) annule proprement les tâches en queue sans freeze."""
    # audio-io n'a qu'1 worker : la 1ère tâche l'occupe, la 2nde reste en attente
    fut1 = pool.submit("audio-io", lambda: time.sleep(0.3) or "t1", task_name="busy")
    fut2 = pool.submit("audio-io", lambda: "t2", task_name="waiting")

    pool.shutdown(wait=True, cancel_futures=True, timeout=1.0)

    assert fut1.done()
    assert fut2.cancelled()
