#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_continuous_vision.py — Tests unitaires et d'intégration de core/continuous_vision.py.

Valide :
1. Ring-buffer circulaire en mémoire partagée (POSIX shm, zéro blocage du GIL, 10 slots).
2. Compression WebP dynamique (qualité adaptative 75%, taille moyenne < 25 Ko).
3. Détection de mouvement locale (MOG2 / Frame Differencing) & passage à 0.5 FPS après 3s d'immobilité (90% d'économie de tokens).
4. Commandes vocales ergonomiques d'activation/désactivation ("regarde ce que je te montre" / "arrête la caméra").
5. Intégration WebSocket Gemini Live dans SessionManager (send_video_frame, _send_realtime).
6. Indicateur visuel d'œil néon sur l'orbe HUD.
7. Benchmarks réseau et consommation de tokens.
"""

from __future__ import annotations

import asyncio
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import numpy as np
import cv2

from core.continuous_vision import (
    ACTIVE_FPS,
    IDLE_FPS,
    STILL_TIMEOUT_SECONDS,
    GEMINI_TOKENS_PER_VIDEO_FRAME,
    ContinuousVisionEngine,
    DynamicWebPEncoder,
    FrameMetadata,
    LocalMotionDetector,
    SharedMemoryRingBuffer,
    get_continuous_vision_engine,
    is_vision_activation_phrase,
    is_vision_deactivation_phrase,
    run_vision_benchmark,
)
from core.session_manager import SessionManager


# ─────────────────────────────────────────────────────────────────────────────
# 1. Tests du Ring-Buffer Circulaire en Mémoire Partagée
# ─────────────────────────────────────────────────────────────────────────────

def test_ring_buffer_creation_and_cleanup():
    """Vérifie la création, l'écriture de métadonnées et la désallocation propre."""
    rb = SharedMemoryRingBuffer(capacity=10)
    try:
        stats = rb.get_stats()
        assert stats["capacity"] == 10
        assert stats["write_counter"] == 0
        assert stats["current_fps"] == pytest.approx(ACTIVE_FPS, abs=0.1)
    finally:
        rb.cleanup()


def test_ring_buffer_circular_write_and_read():
    """Vérifie le comportement circulaire (wrap-around) et la lecture des trames."""
    rb = SharedMemoryRingBuffer(capacity=10)
    try:
        # Écriture de 15 trames pour tester le rebouclage circulaire (15 > 10)
        for i in range(15):
            payload = f"FRAME_DATA_{i}".encode("ascii")
            seq = rb.write_frame(
                frame_bytes=payload,
                timestamp=time.monotonic() + i * 0.2,
                is_motion=(i % 2 == 0),
                motion_score=0.05 * (i % 5),
                quality=75,
                fps_mode=ACTIVE_FPS,
            )
            assert seq == i + 1

        # La trame la plus récente doit être la 15ème (seq=15, index 14)
        latest = rb.read_latest()
        assert latest is not None
        meta, data = latest
        assert meta.frame_id == 15
        assert data == b"FRAME_DATA_14"

        # Le slot 0 doit maintenant contenir la 11ème trame (seq=11)
        slot0 = rb.read_slot(0)
        assert slot0 is not None
        s0_meta, s0_data = slot0
        assert s0_meta.frame_id == 11
        assert s0_data == b"FRAME_DATA_10"
    finally:
        rb.cleanup()


def test_ring_buffer_empty_read():
    """Vérifie qu'un buffer vide renvoie None sans lever d'exception."""
    rb = SharedMemoryRingBuffer(capacity=10)
    try:
        assert rb.read_latest() is None
        assert rb.read_slot(0) is None
    finally:
        rb.cleanup()


# ─────────────────────────────────────────────────────────────────────────────
# 2. Tests de la Compression WebP Dynamique
# ─────────────────────────────────────────────────────────────────────────────

def test_dynamic_webp_compression_quality_and_size():
    """Vérifie que la compression produit des images WebP valides < 25 Ko."""
    encoder = DynamicWebPEncoder(initial_quality=75, target_max_kb=25.0)

    # Création d'une mire OpenCV 640x480 standard
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    frame[:] = (30, 45, 60)
    cv2.circle(frame, (320, 240), 90, (0, 255, 200), -1)
    cv2.putText(frame, "TEST WEBP ENCODING", (40, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)

    webp_bytes, quality_used, size_kb = encoder.encode(frame)

    assert isinstance(webp_bytes, bytes)
    assert len(webp_bytes) > 0
    # Signature WebP (RIFF....WEBP)
    assert webp_bytes.startswith(b"RIFF")
    assert b"WEBP" in webp_bytes[:16]
    assert size_kb < 25.0
    assert quality_used == 75

    metrics = encoder.get_metrics()
    assert metrics["total_encoded_frames"] == 1
    assert metrics["rolling_avg_kb"] == pytest.approx(size_kb, abs=0.5)


def test_dynamic_webp_rate_control_adaptation():
    """Vérifie que la qualité diminue si les trames dépassent la taille cible."""
    # Cible très stricte à 1.0 Ko pour forcer la baisse de qualité
    encoder = DynamicWebPEncoder(initial_quality=75, target_max_kb=1.0, min_quality=40)

    frame = np.random.randint(0, 255, (480, 640, 3), dtype=np.uint8)

    # Encodage de plusieurs trames à fort bruit
    for _ in range(5):
        _, q_used, _ = encoder.encode(frame)

    # La qualité doit s'être abaissée automatiquement
    assert encoder.quality < 75
    assert encoder.quality >= 40


# ─────────────────────────────────────────────────────────────────────────────
# 3. Tests de la Détection de Mouvement & Throttling (90% économie tokens)
# ─────────────────────────────────────────────────────────────────────────────

def test_motion_detection_active_then_idle_throttling():
    """Vérifie la détection de mouvement et le basculement à 0.5 FPS après 3s d'immobilité."""
    detector = LocalMotionDetector(method="diff", still_timeout=0.2)  # Timeout court pour test

    frame_still1 = np.zeros((480, 640, 3), dtype=np.uint8)
    frame_still2 = np.zeros((480, 640, 3), dtype=np.uint8)

    # 1. Scène immobile : première comparaison
    is_motion, score, fps = detector.process_frame(frame_still1)
    is_motion, score, fps = detector.process_frame(frame_still2)

    assert not is_motion
    assert score == pytest.approx(0.0, abs=0.001)

    # Attente pour dépasser le still_timeout (0.2s)
    time.sleep(0.25)

    is_motion, score, fps = detector.process_frame(frame_still2)
    assert not is_motion
    assert detector.is_idle is True
    # Cadence réduite à 0.5 FPS
    assert fps == pytest.approx(IDLE_FPS, abs=0.01)

    # 2. Mouvement soudain : changement d'image
    frame_moving = np.zeros((480, 640, 3), dtype=np.uint8)
    frame_moving[100:300, 100:300] = 255  # Grand carré blanc (mouvement fort)

    is_motion, score, fps = detector.process_frame(frame_moving)
    assert is_motion is True
    assert score > 0.015
    assert detector.is_idle is False
    # Retour instantané à 5 FPS
    assert fps == pytest.approx(ACTIVE_FPS, abs=0.01)


def test_token_savings_calculation():
    """Valide mathématiquement l'économie de 90% des tokens Gemini Live."""
    # 5.0 FPS pendant 60 secondes = 300 frames
    active_frames = int(ACTIVE_FPS * 60)
    # 0.5 FPS pendant 60 secondes = 30 frames
    still_frames = int(IDLE_FPS * 60)

    active_tokens = active_frames * GEMINI_TOKENS_PER_VIDEO_FRAME
    still_tokens = still_frames * GEMINI_TOKENS_PER_VIDEO_FRAME

    tokens_saved = active_tokens - still_tokens
    savings_pct = (tokens_saved / active_tokens) * 100.0

    assert active_frames == 300
    assert still_frames == 30
    assert active_tokens == 77400
    assert still_tokens == 7740
    assert tokens_saved == 69660
    assert savings_pct == pytest.approx(90.0, abs=0.01)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Tests des Déclencheurs Vocaux Ergonomiques
# ─────────────────────────────────────────────────────────────────────────────

def test_voice_activation_triggers():
    """Valide les formulations d'activation vocale."""
    valid_activations = [
        "Jarvis, regarde ce que je te montre",
        "Regarde ce que je te montre",
        "regarde ce que je montre",
        "regarde bien ce que je fais",
        "active la vision continue",
        "lance la vision continue",
        "observe la scène",
        "regarde en direct s'il te plaît",
    ]
    for phrase in valid_activations:
        assert is_vision_activation_phrase(phrase), f"Échec activation pour: {phrase}"


def test_voice_deactivation_triggers():
    """Valide les formulations de désactivation vocale."""
    valid_deactivations = [
        "Arrête la caméra",
        "arrete la camera",
        "stop caméra",
        "coupe la caméra",
        "désactive la vision continue",
        "ferme la caméra",
        "ne regarde plus",
    ]
    for phrase in valid_deactivations:
        assert is_vision_deactivation_phrase(phrase), f"Échec désactivation pour: {phrase}"


def test_voice_triggers_rejection_of_unrelated_phrases():
    """Vérifie qu'aucune fausse activation ne se produit sur des phrases neutres."""
    unrelated = [
        "ouvre la porte",
        "allume la lumière",
        "quelle heure est-il",
        "mets de la musique",
        "fais un commit git",
        "comment réparer ce code",
    ]
    for phrase in unrelated:
        assert not is_vision_activation_phrase(phrase)
        assert not is_vision_deactivation_phrase(phrase)


# ─────────────────────────────────────────────────────────────────────────────
# 5. Tests d'Intégration dans SessionManager (WebSocket & out_queue)
# ─────────────────────────────────────────────────────────────────────────────

def test_session_manager_send_video_frame():
    """Vérifie que send_video_frame transmet bien la frame dans out_queue avec activity='video'."""
    async def _test():
        sm = SessionManager()
        sm.session = MagicMock()
        sm.out_queue = asyncio.Queue()
        sm._conn = MagicMock()
        sm._conn.is_connected = True

        dummy_frame = b"\x89WEBP_DUMMY_FRAME_BYTES"
        ok = await sm.send_video_frame(dummy_frame, mime_type="image/webp")

        assert ok is True
        assert not sm.out_queue.empty()
        queued_msg = await sm.out_queue.get()

        assert queued_msg["activity"] == "video"
        assert queued_msg["data"] == dummy_frame
        assert queued_msg["mime_type"] == "image/webp"

    asyncio.run(_test())


def test_session_manager_send_realtime_video_marker():
    """Vérifie que _send_realtime consomme les marqueurs vidéo et appelle session.send_realtime_input(video=...)."""
    async def _test():
        sm = SessionManager()
        mock_session = AsyncMock()
        sm.session = mock_session
        sm.out_queue = asyncio.Queue()
        sm._conn = MagicMock()
        sm._conn.is_connected = True

        dummy_webp = b"RIFF_FAKE_WEBP"
        await sm.out_queue.put({
            "activity": "video",
            "data": dummy_webp,
            "mime_type": "image/webp",
        })

        # Exécution d'une itération de _send_realtime
        task = asyncio.create_task(sm._send_realtime())
        # Attente brève pour laisser la tâche dépiler
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert mock_session.send_realtime_input.called
        call_kwargs = mock_session.send_realtime_input.call_args.kwargs
        assert "video" in call_kwargs
        assert call_kwargs["video"]["data"] == dummy_webp
        assert call_kwargs["video"]["mime_type"] == "image/webp"

    asyncio.run(_test())


def test_session_manager_voice_trigger_dispatch():
    """Vérifie la détection et le déclenchement automatique sur transcript dans SessionManager."""
    sm = SessionManager()
    sm.ui = MagicMock()

    # Mock du moteur continuous_vision pour éviter d'ouvrir /dev/video0 pendant le test unitaire
    mock_engine = MagicMock()
    mock_engine.start.return_value = "J'observe la scène en direct, Monsieur."
    mock_engine.stop.return_value = "Flux vidéo coupé, Monsieur."
    sm._continuous_vision_engine = mock_engine

    # 1. Activation vocale
    ack = sm.check_continuous_vision_voice_trigger("Jarvis, regarde ce que je te montre")
    assert ack == "J'observe la scène en direct, Monsieur."
    assert mock_engine.start.called

    # 2. Désactivation vocale
    ack_stop = sm.check_continuous_vision_voice_trigger("Arrête la caméra")
    assert ack_stop == "Flux vidéo coupé, Monsieur."
    assert mock_engine.stop.called


# ─────────────────────────────────────────────────────────────────────────────
# 6. Tests de l'Indicateur Visuel sur l'Orbe HUD (HudCanvas)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def qapp():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def test_hud_canvas_neon_eye_indicator(qapp):
    """Vérifie que HudCanvas possède et met à jour l'indicateur d'œil néon."""
    from ui.orb.arc_core import HudCanvas

    # Instanciation de HudCanvas avec l'application Qt
    hud = HudCanvas(face_path="", assistant_name="JARVIS")

    try:
        assert hasattr(hud, "continuous_vision")
        assert hasattr(hud, "set_continuous_vision")
        assert hud.continuous_vision is False

        # Activation de l'indicateur
        hud.set_continuous_vision(True)
        assert hud.continuous_vision is True
        assert hud._continuous_vision_active is True

        # Désactivation
        hud.set_continuous_vision(False)
        assert hud.continuous_vision is False
        assert hud._continuous_vision_active is False
    finally:
        hud.close()


# ─────────────────────────────────────────────────────────────────────────────
# 7. Test du Benchmark Réseau
# ─────────────────────────────────────────────────────────────────────────────

def test_run_vision_benchmark_structure():
    """Vérifie que run_vision_benchmark s'exécute et produit un rapport valide."""
    results = run_vision_benchmark(duration_seconds=1.0)

    assert "shm_write_latency_us" in results
    assert "shm_read_latency_us" in results
    assert "avg_webp_encode_time_ms" in results
    assert "avg_frame_size_kb" in results
    assert "token_savings_percent" in results
    assert "bandwidth_savings_percent" in results

    assert results["token_savings_percent"] == pytest.approx(90.0, abs=0.1)
    assert results["bandwidth_savings_percent"] == pytest.approx(90.0, abs=0.1)
    assert results["avg_frame_size_kb"] < 25.0
    assert results["shm_write_latency_us"] < 500.0  # Moins de 0.5 ms
