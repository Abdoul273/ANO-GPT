"""Le veilleur de gel signale la durée cumulée sans réarmer le battement."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

from core import freeze_watch as fw


def test_watch_reports_cumulative_duration_without_rearming(monkeypatch):
    reports: list[str] = []
    monkeypatch.setattr(fw, "_beats", {"audio": 0.0})
    monkeypatch.setattr(fw, "_reported", {})
    monkeypatch.setattr(fw, "dump_all_threads", lambda reason: reports.append(reason) or "path")
    clock = iter([10.0, 20.0, 50.0])
    monkeypatch.setattr(fw.time, "monotonic", lambda: next(clock))

    # Une seule itération de la boucle de veille, trois fois.
    def one_pass():
        now = fw.time.monotonic()
        for name, last in list(fw._beats.items()):
            stalled = now - last
            if stalled < fw.STALL_S:
                fw._reported.pop(name, None)
                continue
            last_report = fw._reported.get(name)
            if last_report is not None and now - last_report < fw.REPORT_COOLDOWN_S:
                continue
            if fw.dump_all_threads(f"{name} ne répond plus depuis {stalled:.1f} s (cumulé)") is not None:
                fw._reported[name] = now

    one_pass(); one_pass(); one_pass()
    assert fw._beats["audio"] == 0.0  # jamais réarmé par le veilleur
    assert reports == [
        "audio ne répond plus depuis 10.0 s (cumulé)",
        "audio ne répond plus depuis 50.0 s (cumulé)",
    ]


def test_tts_run_sync_works_with_and_without_running_loop():
    from core.tts import _run_sync

    async def coro():
        await asyncio.sleep(0)
        return threading.current_thread().name

    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tts-worker")
    try:
        assert _run_sync(coro, executor) == threading.current_thread().name

        async def inside_loop():
            return _run_sync(coro, executor)

        assert asyncio.run(inside_loop()).startswith("tts-worker")
    finally:
        executor.shutdown(wait=False)
