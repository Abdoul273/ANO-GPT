#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/benchmark_event_bus.py — Benchmark de performance pour AsyncEventBus."""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path

# Assure que le package core est importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtCore import QCoreApplication

from core.event_bus import (
    AsyncEventBus,
    EventPriority,
    AudioCaptureFrameEvent,
    ModelSpeechDeltaEvent,
    BargeInDetectedEvent,
    SystemAlertEvent,
    EventBusBridge,
)


def print_banner():
    print("=" * 74)
    print("  BENCHMARK DE PERFORMANCE — AsyncEventBus & EventBusBridge (ANO-GPT)")
    print("=" * 74)


def benchmark_sync_throughput(n_events: int = 50_000) -> float:
    bus = AsyncEventBus(history_maxlen=100)
    counts = {"high": 0, "normal": 0, "low": 0}

    @bus.on(AudioCaptureFrameEvent, priority=EventPriority.HIGH)
    def on_high(evt):
        counts["high"] += 1

    @bus.on(AudioCaptureFrameEvent, priority=EventPriority.NORMAL)
    def on_normal(evt):
        counts["normal"] += 1

    @bus.on(AudioCaptureFrameEvent, priority=EventPriority.LOW)
    def on_low(evt):
        counts["low"] += 1

    event = AudioCaptureFrameEvent(pcm_bytes=b"\x00\x01" * 160, rms=0.042)

    for _ in range(500):
        bus.publish_sync(event)
    counts["high"] = counts["normal"] = counts["low"] = 0

    t_start = time.perf_counter()
    for _ in range(n_events):
        bus.publish_sync(event)
    duration = time.perf_counter() - t_start

    rate = n_events / duration
    avg_latency_us = (duration / n_events) * 1_000_000

    assert counts["high"] == n_events
    assert counts["normal"] == n_events
    assert counts["low"] == n_events

    print("  [1] Dispatch Synchrone (3 handlers HIGH/NORMAL/LOW) :")
    print(f"      - Evenements traites : {n_events:,}")
    print(f"      - Duree totale       : {duration:.4f} s")
    print(f"      - Latence moyenne    : {avg_latency_us:.2f} us / evenement")
    print(f"      - Debit effectif     : {rate:,.0f} evts/s")
    return rate


async def benchmark_async_throughput(n_events: int = 20_000) -> float:
    bus = AsyncEventBus(history_maxlen=100)
    counts = {"async": 0, "sync": 0}

    @bus.on(ModelSpeechDeltaEvent, priority=EventPriority.HIGH)
    async def on_async(evt):
        counts["async"] += 1

    @bus.on(ModelSpeechDeltaEvent, priority=EventPriority.NORMAL)
    def on_sync(evt):
        counts["sync"] += 1

    event = ModelSpeechDeltaEvent(text="delta chunk", audio_chunk=b"\x12\x34", is_final=False)

    for _ in range(200):
        await bus.publish(event)
    counts["async"] = counts["sync"] = 0

    t_start = time.perf_counter()
    for _ in range(n_events):
        await bus.publish(event)
    duration = time.perf_counter() - t_start

    rate = n_events / duration
    avg_latency_us = (duration / n_events) * 1_000_000

    assert counts["async"] == n_events
    assert counts["sync"] == n_events

    print("  [2] Dispatch Asynchrone (await bus.publish + async def) :")
    print(f"      - Evenements traites : {n_events:,}")
    print(f"      - Duree totale       : {duration:.4f} s")
    print(f"      - Latence moyenne    : {avg_latency_us:.2f} us / evenement")
    print(f"      - Debit effectif     : {rate:,.0f} evts/s")
    return rate


def benchmark_qt_bridge_throughput(n_events: int = 20_000) -> float:
    QCoreApplication.instance() or QCoreApplication([])
    bus = AsyncEventBus(history_maxlen=100)
    bridge = EventBusBridge(bus=bus)

    sig_count = 0
    def on_barge(trigger, conf):
        nonlocal sig_count
        sig_count += 1

    bridge.barge_in_detected.connect(on_barge)
    event = BargeInDetectedEvent(trigger_type="vad", confidence=0.98)

    for _ in range(200):
        bus.publish_sync(event)

    sig_count = 0
    t_start = time.perf_counter()
    for _ in range(n_events):
        bus.publish_sync(event)
    duration = time.perf_counter() - t_start

    rate = n_events / duration
    avg_latency_us = (duration / n_events) * 1_000_000

    assert sig_count == n_events

    print("  [3] Pont Qt EventBusBridge -> pyqtSignal :")
    print(f"      - Signaux emis       : {n_events:,}")
    print(f"      - Duree totale       : {duration:.4f} s")
    print(f"      - Latence moyenne    : {avg_latency_us:.2f} us / evenement")
    print(f"      - Debit effectif     : {rate:,.0f} evts/s")
    bridge.detach()
    return rate


def benchmark_exception_isolation_throughput(n_events: int = 15_000) -> float:
    bus = AsyncEventBus(history_maxlen=100)
    crash_count = 0
    healthy_count = 0

    eb_logger = logging.getLogger("ano_gpt.event_bus")
    prev_level = eb_logger.level
    eb_logger.setLevel(logging.CRITICAL)

    try:
        @bus.on(SystemAlertEvent, priority=EventPriority.HIGH)
        def crashing_handler(evt):
            nonlocal crash_count
            crash_count += 1
            raise ValueError("Simulation d un handler bugge")

        @bus.on(SystemAlertEvent, priority=EventPriority.NORMAL)
        def healthy_handler(evt):
            nonlocal healthy_count
            healthy_count += 1

        event = SystemAlertEvent(severity="WARNING", source="test", message="Benchmark load")

        t_start = time.perf_counter()
        for _ in range(n_events):
            bus.publish_sync(event)
        duration = time.perf_counter() - t_start

        rate = n_events / duration
        avg_latency_us = (duration / n_events) * 1_000_000

        assert crash_count == n_events
        assert healthy_count == n_events
        assert bus.get_history_stats()["total_errors"] == n_events

        print("  [4] Isolation d Exceptions sous Charge (1 crash + 1 valide par tour) :")
        print(f"      - Evenements traites : {n_events:,}")
        print(f"      - Erreurs isolees    : {crash_count:,} (aucun crash fatal)")
        print(f"      - Handlers sains     : {healthy_count:,} (100% executes)")
        print(f"      - Duree totale       : {duration:.4f} s")
        print(f"      - Latence moyenne    : {avg_latency_us:.2f} us / evenement")
        print(f"      - Debit effectif     : {rate:,.0f} evts/s")
        return rate
    finally:
        eb_logger.setLevel(prev_level)


def main():
    print_banner()
    target_rate = 10_000.0

    print("\n> Lancement des tests de charge...")
    r1 = benchmark_sync_throughput(50_000)
    print()
    r2 = asyncio.run(benchmark_async_throughput(20_000))
    print()
    r3 = benchmark_qt_bridge_throughput(20_000)
    print()
    r4 = benchmark_exception_isolation_throughput(15_000)

    print("\n" + "=" * 74)
    print("  BILAN DES PERFORMANCES PAR RAPPORT A L OBJECTIF (10 000 evts/s)")
    print("=" * 74)
    benchmarks = [
        ("Dispatch Synchrone (3 ecouteurs)", r1),
        ("Dispatch Asynchrone (await bus.publish)", r2),
        ("Pont Qt pyqtSignal (EventBusBridge)", r3),
        ("Isolation d Exceptions sous charge", r4),
    ]

    all_passed = True
    print(f"  {'Scenario':<42} | {'Debit mesure':<14} | Statut")
    print("  " + "-" * 70)
    for name, rate in benchmarks:
        status = "[SUCCES]" if rate >= target_rate else "[ECHEC]"
        ratio = rate / target_rate
        print(f"  {name:<42} | {rate:>10,.0f} evts/s | {status} ({ratio:.1f}x cible)")
        if rate < target_rate:
            all_passed = False

    print("=" * 74)
    if all_passed:
        print("  TOUS LES TESTS DE DEBIT ONT REUSSI (Objectif de 10 000 evts/s valide) !")
        sys.exit(0)
    else:
        print("  CERTAINS DEBITS SONT INFERIEURS A L OBJECTIF.")
        sys.exit(1)


if __name__ == "__main__":
    main()
