"""tests/test_event_bus.py — Tests unitaires et de performance pour core/event_bus.py.

Vérifie l architecture événementielle asynchrone d ANO-GPT :
1. Événements fortement typés immuables (@dataclass(frozen=True))
2. Singleton par instance de JarvisLive et cycle de vie
3. Priorités d écoute (HIGH > NORMAL > LOW) et dispatch synchrone/asynchrone
4. Buffer circulaire des 100 derniers événements et replay pour auto-debug
5. Isolation des exceptions
6. Pont Qt EventBusBridge et émission thread-safe de pyqtSignal
7. Seuil de performance minimal de 10 000 événements / seconde
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import FrozenInstanceError
import pytest
from PyQt6.QtCore import QCoreApplication

from core.event_bus import (
    AsyncEventBus,
    EventPriority,
    BaseEvent,
    AudioCaptureFrameEvent,
    ModelSpeechDeltaEvent,
    BargeInDetectedEvent,
    ToolExecutionRequestedEvent,
    ToolExecutionFinishedEvent,
    ConnectionStateChangedEvent,
    SystemAlertEvent,
    EventBusBridge,
)


@pytest.fixture(autouse=True)
def cleanup_event_bus():
    AsyncEventBus.reset_all()
    yield
    AsyncEventBus.reset_all()


def test_events_frozen_and_types():
    ts_now = time.time()
    audio_evt = AudioCaptureFrameEvent(b"\x00\x01\x02", 0.42, ts_now)
    assert audio_evt.pcm_bytes == b"\x00\x01\x02"
    assert audio_evt.rms == 0.42
    assert audio_evt.timestamp == ts_now
    assert audio_evt.event_name == "AudioCaptureFrameEvent"

    speech_evt = ModelSpeechDeltaEvent("Bonjour Jarvis", b"\x10\x20", True)
    assert speech_evt.text == "Bonjour Jarvis"
    assert speech_evt.audio_chunk == b"\x10\x20"
    assert speech_evt.is_final is True

    barge_evt = BargeInDetectedEvent("vad", 0.95)
    assert barge_evt.trigger_type == "vad"
    assert barge_evt.confidence == 0.95

    tool_req = ToolExecutionRequestedEvent("weather", "call_123", {"city": "Paris"})
    assert tool_req.tool_name == "weather"
    assert tool_req.call_id == "call_123"
    assert tool_req.params == {"city": "Paris"}

    tool_fin = ToolExecutionFinishedEvent("call_123", {"temp": 21}, 12.5, None)
    assert tool_fin.call_id == "call_123"
    assert tool_fin.result == {"temp": 21}
    assert tool_fin.duration_ms == 12.5
    assert tool_fin.error is None

    conn_evt = ConnectionStateChangedEvent("CONNECTING", "CONNECTED", 0)
    assert conn_evt.old_state == "CONNECTING"
    assert conn_evt.new_state == "CONNECTED"
    assert conn_evt.retry_count == 0

    alert_evt = SystemAlertEvent("WARNING", "audio_engine", "Buffer underrun")
    assert alert_evt.severity == "WARNING"
    assert alert_evt.source == "audio_engine"
    assert alert_evt.message == "Buffer underrun"

    with pytest.raises((FrozenInstanceError, AttributeError)):
        audio_evt.rms = 0.99  # type: ignore[misc]

    with pytest.raises((FrozenInstanceError, AttributeError)):
        barge_evt.confidence = 0.5  # type: ignore[misc]

    d = tool_req.to_dict()
    assert d["_event_name"] == "ToolExecutionRequestedEvent"
    assert d["tool_name"] == "weather"
    assert "timestamp" in d


def test_singleton_per_jarvis_instance():
    class MockJarvisLive:
        pass

    j1 = MockJarvisLive()
    j2 = MockJarvisLive()

    bus1 = AsyncEventBus.get_instance(j1)
    bus2 = AsyncEventBus.get_instance(j2)
    bus_global = AsyncEventBus.get_instance()

    assert bus1 is not bus2
    assert bus1 is not bus_global
    assert AsyncEventBus.get_instance(j1) is bus1
    assert AsyncEventBus.for_instance(j2) is bus2

    AsyncEventBus.reset(j1)
    bus1_new = AsyncEventBus.get_instance(j1)
    assert bus1_new is not bus1
    assert AsyncEventBus.get_instance(j2) is bus2

    AsyncEventBus.set_default(bus2)
    assert AsyncEventBus.get_instance() is bus2


def test_priorities_ordering_and_dispatch():
    async def _runner():
        bus = AsyncEventBus()
        calls = []

        @bus.on(BargeInDetectedEvent, priority=EventPriority.LOW)
        def handle_low(e):
            calls.append("LOW")

        @bus.on(BargeInDetectedEvent, priority=EventPriority.NORMAL)
        def handle_normal_1(e):
            calls.append("NORMAL_1")

        @bus.on(BargeInDetectedEvent, priority=EventPriority.HIGH)
        async def handle_high(e):
            calls.append("HIGH")

        @bus.on(BargeInDetectedEvent, priority=EventPriority.NORMAL)
        def handle_normal_2(e):
            calls.append("NORMAL_2")

        @bus.on(BaseEvent, priority=EventPriority.LOW)
        def handle_audit(e):
            calls.append("AUDIT_LOW")

        await bus.publish(BargeInDetectedEvent("vad", 0.99))
        assert calls == ["HIGH", "NORMAL_1", "NORMAL_2", "LOW", "AUDIT_LOW"]

    asyncio.run(_runner())


def test_exception_isolation_sync_and_async():
    async def _runner():
        bus = AsyncEventBus()
        results = []

        @bus.on(SystemAlertEvent, priority=EventPriority.HIGH)
        def crashing_sync_handler(evt):
            results.append("CRASH_SYNC_ATTEMPTED")
            raise ValueError("Erreur synchrone")

        @bus.on(SystemAlertEvent, priority=EventPriority.NORMAL)
        async def crashing_async_handler(evt):
            results.append("CRASH_ASYNC_ATTEMPTED")
            raise RuntimeError("Erreur asynchrone")

        @bus.on(SystemAlertEvent, priority=EventPriority.LOW)
        def healthy_handler(evt):
            results.append("HEALTHY_EXECUTED")

        await bus.publish(SystemAlertEvent("CRITICAL", "test", "Panic"))

        assert "CRASH_SYNC_ATTEMPTED" in results
        assert "CRASH_ASYNC_ATTEMPTED" in results
        assert "HEALTHY_EXECUTED" in results

        errors = bus.get_recent_errors()
        assert len(errors) == 2
        err_types = {e["error_type"] for e in errors}
        assert err_types == {"ValueError", "RuntimeError"}

    asyncio.run(_runner())


def test_circular_buffer_and_replay():
    bus = AsyncEventBus(history_maxlen=100)

    for i in range(125):
        bus.publish_sync(
            SystemAlertEvent(severity="INFO", source="test", message=f"event_{i}")
        )

    history = bus.get_history()
    assert len(history) == 100
    assert history[0].message == "event_25"
    assert history[-1].message == "event_124"

    bus.publish_sync(BargeInDetectedEvent("vad", 0.8))
    barge_history = bus.get_history(event_type=BargeInDetectedEvent)
    assert len(barge_history) == 1
    assert barge_history[0].confidence == 0.8

    replayed = []
    @bus.on(BargeInDetectedEvent)
    def on_replayed_barge(evt):
        replayed.append(evt)

    count = bus.replay(filter_type=BargeInDetectedEvent)
    assert count == 1
    assert len(replayed) == 1

    stats = bus.get_history_stats()
    assert stats["history_size"] == 100
    assert stats["total_published"] == 126
    assert stats["counts_by_type"]["BargeInDetectedEvent"] == 1


def test_qt_bridge_signals():
    QCoreApplication.instance() or QCoreApplication([])

    bus = AsyncEventBus()
    bridge = EventBusBridge(bus=bus)

    received = {}

    def on_audio(pcm, rms, ts):
        received["audio"] = (pcm, rms, ts)

    def on_speech(text, chunk, is_final):
        received["speech"] = (text, chunk, is_final)

    def on_barge(trigger, conf):
        received["barge"] = (trigger, conf)

    def on_tool_req(name, cid, params):
        received["tool_req"] = (name, cid, params)

    def on_tool_fin(cid, res, dur, err):
        received["tool_fin"] = (cid, res, dur, err)

    def on_conn(old_st, new_st, retries):
        received["conn"] = (old_st, new_st, retries)

    def on_alert(sev, src, msg):
        received["alert"] = (sev, src, msg)

    def on_dispatched(evt):
        received["dispatched"] = evt

    bridge.audio_frame_received.connect(on_audio)
    bridge.model_speech_delta.connect(on_speech)
    bridge.barge_in_detected.connect(on_barge)
    bridge.tool_execution_requested.connect(on_tool_req)
    bridge.tool_execution_finished.connect(on_tool_fin)
    bridge.connection_state_changed.connect(on_conn)
    bridge.system_alert.connect(on_alert)
    bridge.event_dispatched.connect(on_dispatched)

    bus.publish_sync(AudioCaptureFrameEvent(b"pcm_data", 0.77, 1000.0))
    bus.publish_sync(ModelSpeechDeltaEvent("Hello", b"chunk", False))
    bus.publish_sync(BargeInDetectedEvent("vad", 0.9))
    bus.publish_sync(ToolExecutionRequestedEvent("search", "id1", {"q": "python"}))
    bus.publish_sync(ToolExecutionFinishedEvent("id1", "ok", 5.0, None))
    bus.publish_sync(ConnectionStateChangedEvent("IDLE", "CONNECTING", 1))
    bus.publish_sync(SystemAlertEvent("WARNING", "core", "test alert"))

    assert received["audio"] == (b"pcm_data", 0.77, 1000.0)
    assert received["speech"] == ("Hello", b"chunk", False)
    assert received["barge"] == ("vad", 0.9)
    assert received["tool_req"] == ("search", "id1", {"q": "python"})
    assert received["tool_fin"] == ("id1", "ok", 5.0, None)
    assert received["conn"] == ("IDLE", "CONNECTING", 1)
    assert received["alert"] == ("WARNING", "core", "test alert")
    assert isinstance(received["dispatched"], SystemAlertEvent)

    bridge.detach()
    assert bridge.bus is None


def test_performance_throughput():
    bus = AsyncEventBus()
    counter = 0

    @bus.on(AudioCaptureFrameEvent, priority=EventPriority.HIGH)
    def on_frame_high(evt):
        nonlocal counter
        counter += 1

    @bus.on(AudioCaptureFrameEvent, priority=EventPriority.NORMAL)
    def on_frame_normal(evt):
        pass

    @bus.on(AudioCaptureFrameEvent, priority=EventPriority.LOW)
    def on_frame_low(evt):
        pass

    event = AudioCaptureFrameEvent(b"\x00" * 320, 0.05)
    num_events = 20_000

    start_time = time.perf_counter()
    for _ in range(num_events):
        bus.publish_sync(event)
    duration = time.perf_counter() - start_time

    rate = num_events / duration
    print(f"\n[Performance] Traitement de {num_events} événements en {duration:.4f}s = {rate:,.0f} evts/s")

    assert counter == num_events
    assert rate >= 10_000, f"Le débit de {rate:.0f} evts/s est inférieur au seuil de 10 000 evts/s"
