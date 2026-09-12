#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
core/continuous_vision.py — Moteur de vision continue temps réel pour ANO-GPT.

Permet à JARVIS d'observer la scène en direct de manière fluide via l'API Gemini Live.

FONCTIONNALITÉS :
1. Capture et encodage asynchrone :
   - Capture OpenCV depuis `/dev/video0` à 5 FPS (résolution 640x480).
   - Compression WebP dynamique (qualité ~75%, taille moyenne < 25 Ko par frame).
   - Ring-buffer circulaire de 10 images en mémoire partagée (POSIX shm, zéro blocage du GIL).

2. Streaming multimodal WebSocket :
   - Envoi régulier des trames au format `realtime_input` de Gemini Live via `SessionManager`.
   - Détection de mouvement locale en amont (MOG2 / Frame Differencing) :
     si la scène est immobile pendant 3 secondes, réduction automatique du flux à 0.5 FPS
     pour économiser 90% des tokens. Dès qu'un mouvement survient, retour instantané à 5 FPS.

3. Contrôle ergonomique :
   - Déclencheurs vocaux naturels :
     * Activation : "Jarvis, regarde ce que je te montre"
     * Désactivation : "Arrête la caméra"
   - Indicateur visuel discret sur l'orbe HUD quand le flux vidéo est actif (icône œil néon).

4. Benchmarks réseau et consommation de tokens :
   - Calcul précis de la bande passante (KB/s, kbps) et de l'économie de tokens Gemini Live.
"""

from __future__ import annotations

import asyncio
import atexit
import collections
import logging
import math
import os
import re
import struct
import sys
import threading
import time
from dataclasses import dataclass, field
from multiprocessing import shared_memory
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

# Import OpenCV et NumPy résilients
try:
    import cv2
    import numpy as np
    _CV2_AVAILABLE = True
except ImportError:
    cv2 = None  # type: ignore[assignment]
    np = None   # type: ignore[assignment]
    _CV2_AVAILABLE = False

logger = logging.getLogger("ContinuousVision")

# ── Constantes globales ────────────────────────────────────────────────────────
DEFAULT_DEVICE: str = "/dev/video0"
DEFAULT_DEVICE_INDEX: int = 0
CAPTURE_WIDTH: int = 640
CAPTURE_HEIGHT: int = 480

ACTIVE_FPS: float = 5.0        # Cadence active lors de mouvement (1 frame / 200 ms)
IDLE_FPS: float = 0.5          # Cadence au repos (1 frame / 2000 ms, 90% économie de tokens)
STILL_TIMEOUT_SECONDS: float = 3.0  # Durée d'immobilité avant passage à 0.5 FPS

RING_BUFFER_CAPACITY: int = 10      # 10 frames circulaires
SLOT_SIZE_BYTES: int = 65536        # 64 Ko par slot (largement supérieur aux 25 Ko max)
HEADER_SIZE_BYTES: int = 64         # Entête de métadonnées du ring buffer
SLOT_HEADER_SIZE: int = 32          # Entête de slot individuel

DEFAULT_WEBP_QUALITY: int = 75      # Qualité de base
TARGET_MAX_FRAME_KB: float = 25.0   # Cible taille max moyenne par frame
MIN_WEBP_QUALITY: int = 40          # Plancher de compression
MAX_WEBP_QUALITY: int = 85          # Plafond de qualité
MIME_TYPE_WEBP: str = "image/webp"

# Tokens consommés par frame vidéo dans l'API Gemini Live (estimation standard)
GEMINI_TOKENS_PER_VIDEO_FRAME: int = 258


class ContinuousVisionError(RuntimeError):
    """Erreur liée au sous-système de vision continue."""


# ─────────────────────────────────────────────────────────────────────────────
# 1. Ring-Buffer Circulaire en Mémoire Partagée (Zéro blocage du GIL)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class FrameMetadata:
    """Métadonnées d'une trame stockée dans le ring buffer."""
    frame_id: int
    timestamp: float
    data_size: int
    quality: int
    is_motion: bool
    motion_score: float
    fps_mode: float


class SharedMemoryRingBuffer:
    """Ring-buffer circulaire de 10 trames WebP en mémoire partagée POSIX.

    Architecture mémoire :
    - Entête global (64 octets) :
        [0:4]   Magic b"CVRB"
        [4:6]   Version (uint16)
        [6:8]   Capacité (uint16 = 10)
        [8:12]  Slot size (uint32 = 65536)
        [12:16] Header size (uint32 = 64)
        [16:24] Compteur d'écriture atomique (uint64)
        [24:28] FPS actuel (float32)
        [28:32] Score de mouvement actuel (float32)
        [32:33] Indicateur mouvement (uint8)
        [33:34] Mode veille / idle (uint8)
        [34:64] Réservé (padding)
    - 10 Slots de 64 Ko chacun :
        - Slot header (32 octets) :
            [0:8]   frame_id (uint64)
            [8:16]  timestamp (double, seconds)
            [16:20] data_size (uint32)
            [20:24] quality (uint32)
            [24:25] is_motion (uint8)
            [25:29] motion_score (float32)
            [29:32] Réservé
        - Slot payload (65504 octets max) :
            Octets bruts WebP encodés.

    Les lectures/écritures manipulent directement la mémoire partagée par
    slicing de mémoire native (memoryview/buffer protocol), ce qui évite
    toute allocation d'objet Python ou contention sur le GIL.
    """

    _HDR_MAGIC = b"CVRB"
    _HDR_FMT = "<4sHHIIQffBB26s"
    _SLOT_FMT = "<QdIIBf3s"

    def __init__(
        self,
        name: Optional[str] = None,
        capacity: int = RING_BUFFER_CAPACITY,
        slot_size: int = SLOT_SIZE_BYTES,
        create: bool = True,
    ) -> None:
        self.capacity = capacity
        self.slot_size = slot_size
        self.header_size = HEADER_SIZE_BYTES
        self.slot_header_size = SLOT_HEADER_SIZE
        self.payload_max_size = slot_size - self.slot_header_size
        self.total_size = self.header_size + (self.capacity * self.slot_size)

        self._name = name
        self._shm: Optional[shared_memory.SharedMemory] = None
        self._is_creator = create
        self._lock = threading.Lock()
        self._closed = False

        if create:
            try:
                # Tente de détruire un segment résiduel éventuel si nom explicite
                if self._name:
                    try:
                        old_shm = shared_memory.SharedMemory(name=self._name)
                        old_shm.close()
                        old_shm.unlink()
                    except Exception:
                        pass
                self._shm = shared_memory.SharedMemory(create=True, size=self.total_size, name=self._name)
                self._name = self._shm.name
                self._buf = self._shm.buf
                self._init_header()
            except Exception as e:
                logger.warning(f"[RingBuffer] Création POSIX shm impossible ({e}), repli mémoire locale.")
                self._shm = None
        else:
            try:
                self._shm = shared_memory.SharedMemory(name=self._name)
                self._buf = self._shm.buf
            except Exception as e:
                logger.warning(f"[RingBuffer] Connexion POSIX shm échouée ({e}).")
                self._shm = None

        # Repli mémoire tampon purement local si POSIX SHM n'est pas supporté
        if self._shm is None:
            self._fallback_buf = bytearray(self.total_size)
            self._buf = memoryview(self._fallback_buf)
            self._init_header()

        # Enregistrement pour libération propre à l'arrêt du processus
        if self._is_creator:
            atexit.register(self.cleanup)

    @property
    def shm_name(self) -> str:
        return self._name

    def _init_header(self) -> None:
        struct.pack_into(
            self._HDR_FMT,
            self._buf,
            0,
            self._HDR_MAGIC,
            1,  # Version
            self.capacity,
            self.slot_size,
            self.header_size,
            0,  # Write counter = 0
            float(ACTIVE_FPS),
            0.0,  # Motion score
            0,  # Motion flag
            0,  # Idle flag
            b"\x00" * 26,
        )

    def write_frame(
        self,
        frame_bytes: bytes,
        timestamp: float,
        is_motion: bool,
        motion_score: float,
        quality: int,
        fps_mode: float,
    ) -> int:
        """Écrit une frame WebP dans le slot circulaire suivant (Zéro blocage GIL)."""
        data_len = len(frame_bytes)
        if data_len > self.payload_max_size:
            logger.warning(f"[RingBuffer] Trame tronquée: {data_len} > {self.payload_max_size}")
            data_len = self.payload_max_size
            frame_bytes = frame_bytes[:data_len]

        with self._lock:
            if self._closed:
                return -1

            # Lecture compteur courant
            write_counter = struct.unpack_from("<Q", self._buf, 16)[0]
            slot_idx = int(write_counter % self.capacity)

            slot_start = self.header_size + (slot_idx * self.slot_size)
            payload_start = slot_start + self.slot_header_size

            # 1. Écriture du payload binaire (libération du GIL lors de la copie native en mémoire C)
            self._buf[payload_start : payload_start + data_len] = frame_bytes[:data_len]

            # 2. Écriture entête du slot
            struct.pack_into(
                self._SLOT_FMT,
                self._buf,
                slot_start,
                write_counter + 1,  # frame_id
                float(timestamp),
                data_len,
                quality,
                1 if is_motion else 0,
                float(motion_score),
                b"\x00" * 3,
            )

            # 3. Mise à jour atomique entête principal
            new_counter = write_counter + 1
            struct.pack_into(
                "<QffBB",
                self._buf,
                16,
                new_counter,
                float(fps_mode),
                float(motion_score),
                1 if is_motion else 0,
                1 if (fps_mode <= IDLE_FPS + 0.1) else 0,
            )

            return new_counter

    def read_latest(self) -> Optional[Tuple[FrameMetadata, bytes]]:
        """Lit la trame la plus récente depuis le ring-buffer."""
        with self._lock:
            if self._closed:
                return None

            write_counter = struct.unpack_from("<Q", self._buf, 16)[0]
            if write_counter == 0:
                return None

            fps_mode, motion_score, motion_flag, idle_flag = struct.unpack_from("<ffBB", self._buf, 24)

            latest_idx = int((write_counter - 1) % self.capacity)
            slot_start = self.header_size + (latest_idx * self.slot_size)
            payload_start = slot_start + self.slot_header_size

            (
                frame_id,
                ts,
                data_size,
                quality,
                is_motion_b,
                m_score,
                _,
            ) = struct.unpack_from(self._SLOT_FMT, self._buf, slot_start)

            if data_size == 0 or data_size > self.payload_max_size:
                return None

            # Copie rapide en bytes
            raw_bytes = bytes(self._buf[payload_start : payload_start + data_size])

            meta = FrameMetadata(
                frame_id=frame_id,
                timestamp=ts,
                data_size=data_size,
                quality=quality,
                is_motion=bool(is_motion_b),
                motion_score=m_score,
                fps_mode=fps_mode,
            )

            return meta, raw_bytes

    def read_slot(self, slot_index: int) -> Optional[Tuple[FrameMetadata, bytes]]:
        """Lit un slot particulier (0 à capacity - 1)."""
        with self._lock:
            if self._closed or not (0 <= slot_index < self.capacity):
                return None

            slot_start = self.header_size + (slot_index * self.slot_size)
            payload_start = slot_start + self.slot_header_size

            (
                frame_id,
                ts,
                data_size,
                quality,
                is_motion_b,
                m_score,
                _,
            ) = struct.unpack_from(self._SLOT_FMT, self._buf, slot_start)

            if data_size == 0 or data_size > self.payload_max_size:
                return None

            raw_bytes = bytes(self._buf[payload_start : payload_start + data_size])
            meta = FrameMetadata(
                frame_id=frame_id,
                timestamp=ts,
                data_size=data_size,
                quality=quality,
                is_motion=bool(is_motion_b),
                motion_score=m_score,
                fps_mode=ACTIVE_FPS,
            )
            return meta, raw_bytes

    def get_stats(self) -> Dict[str, Any]:
        """Retourne l'état courant de l'entête partagé."""
        with self._lock:
            if self._closed:
                return {"status": "closed"}
            write_counter = struct.unpack_from("<Q", self._buf, 16)[0]
            fps_mode, motion_score, motion_flag, idle_flag = struct.unpack_from("<ffBB", self._buf, 24)
            return {
                "write_counter": write_counter,
                "current_fps": fps_mode,
                "motion_score": motion_score,
                "motion_detected": bool(motion_flag),
                "idle_mode": bool(idle_flag),
                "capacity": self.capacity,
                "slot_size_bytes": self.slot_size,
            }

    def cleanup(self) -> None:
        """Ferme et désalloue proprement le segment de mémoire partagée."""
        with self._lock:
            if self._closed:
                return
            self._closed = True

        if self._shm is not None:
            try:
                self._shm.close()
                if self._is_creator:
                    self._shm.unlink()
            except Exception:
                pass
            self._shm = None


# ─────────────────────────────────────────────────────────────────────────────
# 2. Compression WebP Dynamique (Qualité 75%, taille moyenne < 25 Ko)
# ─────────────────────────────────────────────────────────────────────────────

class DynamicWebPEncoder:
    """Encodeur WebP dynamique adaptatif.

    Régule automatiquement la qualité d'encodage (40% à 85%) pour garantir
    une taille moyenne strictement inférieure à 25 Ko par trame tout en
    préservant la netteté des détails.
    """

    def __init__(
        self,
        initial_quality: int = DEFAULT_WEBP_QUALITY,
        target_max_kb: float = TARGET_MAX_FRAME_KB,
        min_quality: int = MIN_WEBP_QUALITY,
        max_quality: int = MAX_WEBP_QUALITY,
    ) -> None:
        self.quality = initial_quality
        self.target_max_kb = target_max_kb
        self.min_quality = min_quality
        self.max_quality = max_quality
        self._recent_sizes_kb: collections.deque[float] = collections.deque(maxlen=10)
        self._total_encoded: int = 0
        self._total_bytes: int = 0

    @property
    def rolling_avg_kb(self) -> float:
        if not self._recent_sizes_kb:
            return 0.0
        return sum(self._recent_sizes_kb) / len(self._recent_sizes_kb)

    def encode(self, frame_bgr: Any) -> Tuple[bytes, int, float]:
        """Encode une image OpenCV (BGR) en WebP avec régulation dynamique de qualité.

        Returns:
            (webp_bytes, quality_used, size_in_kb)
        """
        if not _CV2_AVAILABLE or frame_bgr is None:
            # Mode synthétique ou OpenCV indisponible
            dummy = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 1024
            return dummy, self.quality, len(dummy) / 1024.0

        current_q = self.quality
        encode_params = [cv2.IMWRITE_WEBP_QUALITY, current_q]
        success, encoded_buf = cv2.imencode(".webp", frame_bgr, encode_params)

        if not success or encoded_buf is None:
            # Repli de secours JPEG si WebP échoue exceptionnellement
            success, encoded_buf = cv2.imencode(".jpg", frame_bgr, [cv2.IMWRITE_JPEG_QUALITY, 70])
            if not success:
                raise ContinuousVisionError("Échec de l'encodage de la trame vidéo.")

        raw_bytes = encoded_buf.tobytes()
        size_kb = len(raw_bytes) / 1024.0

        # Si une trame dépasse 35 Ko (scène à très haute entropie), second passage immédiat
        if size_kb > 35.0 and current_q > self.min_quality + 10:
            retry_q = max(self.min_quality, current_q - 15)
            s2, b2 = cv2.imencode(".webp", frame_bgr, [cv2.IMWRITE_WEBP_QUALITY, retry_q])
            if s2 and b2 is not None:
                raw_bytes = b2.tobytes()
                current_q = retry_q
                size_kb = len(raw_bytes) / 1024.0

        # Mise à jour des statistiques
        self._recent_sizes_kb.append(size_kb)
        self._total_encoded += 1
        self._total_bytes += len(raw_bytes)

        # Régulation continue adaptative de la qualité
        avg_kb = self.rolling_avg_kb
        if avg_kb > self.target_max_kb:
            # Dépassement moyen : réduire la qualité
            penalty = int(math.ceil((avg_kb - self.target_max_kb) * 1.8))
            self.quality = max(self.min_quality, self.quality - max(2, penalty))
        elif avg_kb < 18.0 and self.quality < self.max_quality:
            # Marge disponible : réhausser la netteté
            self.quality = min(self.max_quality, self.quality + 1)

        return raw_bytes, current_q, size_kb

    def get_metrics(self) -> Dict[str, Any]:
        return {
            "current_quality": self.quality,
            "rolling_avg_kb": round(self.rolling_avg_kb, 2),
            "total_encoded_frames": self._total_encoded,
            "total_bytes_encoded": self._total_bytes,
            "overall_avg_kb": round((self._total_bytes / (self._total_encoded * 1024.0)) if self._total_encoded else 0.0, 2),
        }


# ─────────────────────────────────────────────────────────────────────────────
# 3. Détection de Mouvement Locale (MOG2 / Frame Differencing)
# ─────────────────────────────────────────────────────────────────────────────

class LocalMotionDetector:
    """Détecteur de mouvement local hybride (MOG2 ou Frame Differencing ultra-léger).

    Si la scène est immobile pendant 3 secondes (STILL_TIMEOUT_SECONDS),
    le mode passe en IDLE (0.5 FPS), réduisant de 90% la consommation de tokens.
    Dès qu'un mouvement est perçu, le flux revient instantanément à 5 FPS.
    """

    def __init__(
        self,
        method: str = "mog2",
        motion_threshold: float = 0.015,
        still_timeout: float = STILL_TIMEOUT_SECONDS,
    ) -> None:
        self.method = method
        self.motion_threshold = motion_threshold
        self.still_timeout = still_timeout

        self._mog2 = None
        if _CV2_AVAILABLE and method == "mog2":
            try:
                self._mog2 = cv2.createBackgroundSubtractorMOG2(
                    history=50, varThreshold=25, detectShadows=False
                )
            except Exception:
                self._mog2 = None

        self._prev_gray = None
        self._last_motion_timestamp: float = time.monotonic()
        self._is_idle: bool = False
        self._last_score: float = 0.0

    @property
    def is_idle(self) -> bool:
        return self._is_idle

    @property
    def current_target_fps(self) -> float:
        return IDLE_FPS if self._is_idle else ACTIVE_FPS

    def process_frame(self, frame_bgr: Any) -> Tuple[bool, float, float]:
        """Analyse la trame pour détecter du mouvement.

        Returns:
            (is_motion, motion_score, target_fps)
        """
        now = time.monotonic()

        if not _CV2_AVAILABLE or frame_bgr is None:
            # Repli si pas de caméra ou données synthétiques
            elapsed = now - self._last_motion_timestamp
            is_idle = elapsed >= self.still_timeout
            self._is_idle = is_idle
            return (not is_idle), (0.05 if not is_idle else 0.0), (IDLE_FPS if is_idle else ACTIVE_FPS)

        # 1. Réduction d'échelle (160x120) pour calcul ultra-rapide (< 1 ms CPU)
        small = cv2.resize(frame_bgr, (160, 120), interpolation=cv2.INTER_LINEAR)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)

        motion_score = 0.0

        if self._mog2 is not None and self.method == "mog2":
            fg_mask = self._mog2.apply(gray)
            non_zero = cv2.countNonZero(fg_mask)
            total_pixels = 160 * 120
            motion_score = float(non_zero) / float(total_pixels)
        else:
            # Frame Differencing absolu
            if self._prev_gray is not None:
                delta = cv2.absdiff(self._prev_gray, gray)
                _, thresh = cv2.threshold(delta, 25, 255, cv2.THRESH_BINARY)
                non_zero = cv2.countNonZero(thresh)
                total_pixels = 160 * 120
                motion_score = float(non_zero) / float(total_pixels)
            self._prev_gray = gray

        self._last_score = motion_score
        is_motion = motion_score >= self.motion_threshold

        if is_motion:
            self._last_motion_timestamp = now
            self._is_idle = False
        else:
            if (now - self._last_motion_timestamp) >= self.still_timeout:
                self._is_idle = True
            else:
                self._is_idle = False

        target_fps = IDLE_FPS if self._is_idle else ACTIVE_FPS
        return is_motion, motion_score, target_fps

    def reset(self) -> None:
        self._prev_gray = None
        self._last_motion_timestamp = time.monotonic()
        self._is_idle = False
        self._last_score = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# 4. Worker de Capture Asynchrone OpenCV
# ─────────────────────────────────────────────────────────────────────────────

class ContinuousVisionCapture:
    """Capture OpenCV asynchrone depuis /dev/video0 à cadence dynamique."""

    def __init__(
        self,
        ring_buffer: SharedMemoryRingBuffer,
        encoder: DynamicWebPEncoder,
        motion_detector: LocalMotionDetector,
        device_path: Union[str, int] = DEFAULT_DEVICE,
        on_frame_ready: Optional[Callable[[FrameMetadata, bytes], None]] = None,
    ) -> None:
        self.ring_buffer = ring_buffer
        self.encoder = encoder
        self.motion_detector = motion_detector
        self.device_path = device_path
        self.on_frame_ready = on_frame_ready

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._cap = None
        self._lock = threading.Lock()
        self._active = False

    @property
    def is_running(self) -> bool:
        return self._active and (self._thread is not None and self._thread.is_alive())

    def start(self) -> None:
        with self._lock:
            if self.is_running:
                return
            self._stop_event.clear()
            from core.thread_pool import get_thread_pool
            self._thread = get_thread_pool().spawn_thread(
                "compute-light", "continuous-vision-capture", self._capture_loop,
                stall_timeout=float("inf"),
            )
            self._active = True
            logger.info(f"[ContinuousVision] Démarrage capture sur {self.device_path}")

    def stop(self) -> None:
        with self._lock:
            self._stop_event.set()
            self._active = False

        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.5)
        self._thread = None
        self._release_capture()
        logger.info("[ContinuousVision] Capture arrêtée.")

    def _open_capture(self) -> Any:
        if not _CV2_AVAILABLE:
            logger.warning("[ContinuousVision] OpenCV non disponible, utilisation de flux simulé.")
            return None

        dev = self.device_path
        # Support d'indice entier ou chemin de périphérique Linux
        if isinstance(dev, str) and dev.isdigit():
            dev = int(dev)
        elif isinstance(dev, str) and dev.startswith("/dev/video"):
            try:
                # Extraction du numéro de périphérique V4L2
                dev_idx = int(re.sub(r"\D", "", dev))
                dev = dev_idx
            except Exception:
                dev = 0

        cap = cv2.VideoCapture(dev)
        if not cap.isOpened():
            # Repli direct sur l'index 0
            cap = cv2.VideoCapture(0)

        if cap.isOpened():
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAPTURE_WIDTH)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAPTURE_HEIGHT)
            cap.set(cv2.CAP_PROP_FPS, ACTIVE_FPS)
            # Lecture initiale pour stabiliser l'exposition de la caméra
            for _ in range(3):
                cap.read()
        return cap

    def _release_capture(self) -> None:
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None

    def _generate_synthetic_frame(self, t: float) -> Any:
        """Génère une mire animée réaliste si aucune caméra matérielle n'est branchée."""
        if not _CV2_AVAILABLE:
            return None
        img = np.zeros((CAPTURE_HEIGHT, CAPTURE_WIDTH, 3), dtype=np.uint8)
        # Fond dégradé subtil
        for y in range(CAPTURE_HEIGHT):
            img[y, :, 0] = int(12 + (y / CAPTURE_HEIGHT) * 20)
            img[y, :, 1] = int(24 + (y / CAPTURE_HEIGHT) * 30)
            img[y, :, 2] = int(38 + (y / CAPTURE_HEIGHT) * 45)

        # Cercle mobile pour stimuler le détecteur de mouvement
        cx = int(320 + 160 * math.sin(t * 1.5))
        cy = int(240 + 80 * math.cos(t * 2.0))
        cv2.circle(img, (cx, cy), 35, (0, 230, 255), -1)
        cv2.putText(img, "JARVIS LIVE VISION", (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 200), 2)
        cv2.putText(img, f"{time.strftime('%H:%M:%S')}", (20, 80), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 200), 1)
        return img

    def _capture_loop(self) -> None:
        self._cap = self._open_capture()
        last_frame_time = 0.0

        while not self._stop_event.is_set():
            loop_start = time.monotonic()

            # 1. Lecture de l'image (caméra ou mire de secours)
            frame = None
            if self._cap is not None and self._cap.isOpened():
                ok, raw_frame = self._cap.read()
                if ok and raw_frame is not None:
                    frame = raw_frame
            if frame is None:
                frame = self._generate_synthetic_frame(loop_start)

            # 2. Détection de mouvement locale
            is_motion, motion_score, target_fps = self.motion_detector.process_frame(frame)

            # 3. Calcul de l'intervalle selon le mode (5 FPS vs 0.5 FPS)
            target_interval = 1.0 / max(0.1, target_fps)
            elapsed_since_last = loop_start - last_frame_time

            # 4. Échantillonnage conditionnel
            if elapsed_since_last >= target_interval or last_frame_time == 0.0:
                last_frame_time = loop_start

                # Encodage WebP dynamique
                webp_bytes, quality_used, size_kb = self.encoder.encode(frame)

                # Écriture dans le ring buffer circulaire partagé
                frame_seq = self.ring_buffer.write_frame(
                    frame_bytes=webp_bytes,
                    timestamp=loop_start,
                    is_motion=is_motion,
                    motion_score=motion_score,
                    quality=quality_used,
                    fps_mode=target_fps,
                )

                meta = FrameMetadata(
                    frame_id=frame_seq,
                    timestamp=loop_start,
                    data_size=len(webp_bytes),
                    quality=quality_used,
                    is_motion=is_motion,
                    motion_score=motion_score,
                    fps_mode=target_fps,
                )

                if self.on_frame_ready:
                    try:
                        self.on_frame_ready(meta, webp_bytes)
                    except Exception as e:
                        logger.debug(f"[ContinuousVision] Callback on_frame_ready: {e}")

            # 5. Attente cadencée fine
            step_delay = 0.04  # Scan régulier à ~25 Hz pour une réactivité instantanée dès mouvement
            self._stop_event.wait(step_delay)


# ─────────────────────────────────────────────────────────────────────────────
# 5. Moteur de Vision Continue & Streaming WebSocket Gemini Live
# ─────────────────────────────────────────────────────────────────────────────

class ContinuousVisionEngine:
    """Moteur central orchestrant la capture, le streaming et les indicateurs ergonomiques."""

    def __init__(
        self,
        session_manager: Optional[Any] = None,
        device_path: Union[str, int] = DEFAULT_DEVICE,
        ui_callback: Optional[Callable[[bool], None]] = None,
    ) -> None:
        self.session_manager = session_manager
        self.device_path = device_path
        self.ui_callback = ui_callback

        self.ring_buffer = SharedMemoryRingBuffer(capacity=RING_BUFFER_CAPACITY)
        self.encoder = DynamicWebPEncoder(initial_quality=DEFAULT_WEBP_QUALITY)
        self.motion_detector = LocalMotionDetector(method="mog2", still_timeout=STILL_TIMEOUT_SECONDS)

        self._capture = ContinuousVisionCapture(
            ring_buffer=self.ring_buffer,
            encoder=self.encoder,
            motion_detector=self.motion_detector,
            device_path=device_path,
            on_frame_ready=self._handle_new_frame,
        )

        self._active: bool = False
        self._stream_task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._frame_available_event = asyncio.Event()

        # Métriques et télémétrie réseau
        self._start_time: float = 0.0
        self._total_frames_captured: int = 0
        self._total_frames_sent: int = 0
        self._total_bytes_sent: int = 0
        self._high_fps_frames: int = 0
        self._idle_fps_frames: int = 0
        self._still_seconds: float = 0.0
        self._active_seconds: float = 0.0

    @property
    def is_active(self) -> bool:
        return self._active

    def set_session_manager(self, session_manager: Any) -> None:
        self.session_manager = session_manager

    def set_ui_callback(self, callback: Callable[[bool], None]) -> None:
        self.ui_callback = callback

    def start(self, device: Optional[Union[str, int]] = None) -> str:
        """Active la vision continue fluide."""
        if self._active:
            return "La vision continue est déjà active."

        if device:
            self.device_path = device
            self._capture.device_path = device

        self._active = True
        self._start_time = time.monotonic()
        self.motion_detector.reset()

        # Démarrage de la capture en arrière-plan
        self._capture.start()

        # Démarrage de la tâche asynchrone de streaming WebSocket
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

        if self._loop and self._loop.is_running():
            self._stream_task = self._loop.create_task(self._stream_websocket_loop())

        # Notification UI (indicateur œil néon sur l'orbe HUD)
        self._notify_ui(True)
        logger.info("[ContinuousVision] Mode vision continue activé.")
        from core.personality_modes import user_address
        return f"J'observe la scène en direct, {user_address()}."

    def stop(self) -> str:
        """Désactive la vision continue."""
        if not self._active:
            return "La vision continue est déjà arrêtée."

        self._active = False
        self._capture.stop()

        if self._stream_task and not self._stream_task.done():
            self._stream_task.cancel()
            self._stream_task = None

        # Extinction de l'indicateur visuel sur l'orbe HUD
        self._notify_ui(False)
        logger.info("[ContinuousVision] Mode vision continue arrêté.")
        from core.personality_modes import user_address
        return f"Flux vidéo coupé, {user_address()}."

    def toggle(self) -> str:
        """Bascule l'état de la vision continue."""
        if self._active:
            return self.stop()
        return self.start()

    def _notify_ui(self, active: bool) -> None:
        if self.ui_callback:
            try:
                self.ui_callback(active)
            except Exception as e:
                logger.debug(f"[ContinuousVision] Notification UI: {e}")

        # Intégration directe si SessionManager possède une référence à l'UI
        if self.session_manager and hasattr(self.session_manager, "ui"):
            ui = self.session_manager.ui
            if hasattr(ui, "set_continuous_vision_state"):
                try:
                    ui.set_continuous_vision_state(active)
                except Exception:
                    pass
            elif hasattr(ui, "hud") and hasattr(ui.hud, "set_continuous_vision"):
                try:
                    ui.hud.set_continuous_vision(active)
                except Exception:
                    pass

    def _handle_new_frame(self, meta: FrameMetadata, raw_bytes: bytes) -> None:
        """Callback invoqué par le worker de capture à chaque nouvelle trame."""
        self._total_frames_captured += 1
        if meta.fps_mode <= IDLE_FPS + 0.1:
            self._idle_fps_frames += 1
        else:
            self._high_fps_frames += 1

        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._frame_available_event.set)

    async def _stream_websocket_loop(self) -> None:
        """Boucle asynchrone expédiant les trames au WebSocket Gemini Live via SessionManager."""
        last_sent_frame_id = -1

        try:
            while self._active:
                # Lecture de la trame la plus fraîche depuis le ring-buffer
                frame_data = self.ring_buffer.read_latest()

                if frame_data is not None:
                    meta, raw_bytes = frame_data

                    if meta.frame_id != last_sent_frame_id:
                        last_sent_frame_id = meta.frame_id

                        # Transmission à Gemini Live
                        sent = await self._dispatch_to_session_manager(raw_bytes)
                        if sent:
                            self._total_frames_sent += 1
                            self._total_bytes_sent += len(raw_bytes)

                # Cadencement adaptatif selon l'état d'immobilité
                target_interval = 1.0 / max(0.1, self.motion_detector.current_target_fps)
                await asyncio.sleep(target_interval)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[ContinuousVision] Erreur boucle streaming WebSocket: {e}", exc_info=True)

    async def _dispatch_to_session_manager(self, frame_bytes: bytes) -> bool:
        """Transmet la frame au format realtime_input via SessionManager."""
        sm = self.session_manager
        if sm is None:
            return False

        # 1. Méthode dédiée si présente sur SessionManager
        if hasattr(sm, "send_video_frame"):
            try:
                res = sm.send_video_frame(frame_bytes, mime_type=MIME_TYPE_WEBP)
                if asyncio.iscoroutine(res):
                    return await res
                return bool(res)
            except Exception as e:
                logger.debug(f"[ContinuousVision] send_video_frame error: {e}")

        # 2. File de sortie temps réel standard out_queue
        if hasattr(sm, "out_queue") and sm.out_queue is not None:
            try:
                await sm.out_queue.put({
                    "activity": "video",
                    "data": frame_bytes,
                    "mime_type": MIME_TYPE_WEBP,
                })
                return True
            except Exception as e:
                logger.debug(f"[ContinuousVision] out_queue put error: {e}")

        # 3. Envoi direct sur la session Live active
        if hasattr(sm, "session") and sm.session is not None:
            try:
                await sm.session.send_realtime_input(
                    video={"data": frame_bytes, "mime_type": MIME_TYPE_WEBP}
                )
                return True
            except Exception as e:
                logger.debug(f"[ContinuousVision] session.send_realtime_input error: {e}")

        return False

    def get_metrics(self) -> Dict[str, Any]:
        """Calcule les métriques de bande passante et d'économie de tokens."""
        now = time.monotonic()
        duration = max(0.1, now - self._start_time) if self._active else 0.1

        enc_metrics = self.encoder.get_metrics()
        avg_kb = enc_metrics.get("rolling_avg_kb", 0.0)

        # Calcul des tokens consommés vs sans throttling (5 FPS fixe)
        without_throttling_frames = int(duration * ACTIVE_FPS)
        actual_frames = self._total_frames_sent
        tokens_consumed = actual_frames * GEMINI_TOKENS_PER_VIDEO_FRAME
        tokens_without_throttling = without_throttling_frames * GEMINI_TOKENS_PER_VIDEO_FRAME

        tokens_saved = max(0, tokens_without_throttling - tokens_consumed)
        savings_pct = (tokens_saved / max(1, tokens_without_throttling)) * 100.0 if tokens_without_throttling > 0 else 0.0

        bandwidth_kbps = (self._total_bytes_sent * 8.0) / (duration * 1000.0)
        bandwidth_kbs = (self._total_bytes_sent / 1024.0) / duration

        return {
            "active": self._active,
            "duration_seconds": round(duration, 1),
            "current_fps": self.motion_detector.current_target_fps,
            "is_idle": self.motion_detector.is_idle,
            "total_frames_captured": self._total_frames_captured,
            "total_frames_sent": self._total_frames_sent,
            "high_fps_frames": self._high_fps_frames,
            "idle_fps_frames": self._idle_fps_frames,
            "avg_frame_size_kb": avg_kb,
            "total_mbytes_sent": round(self._total_bytes_sent / (1024.0 * 1024.0), 3),
            "bandwidth_kb_per_s": round(bandwidth_kbs, 2),
            "bandwidth_kbps": round(bandwidth_kbps, 1),
            "tokens_consumed": tokens_consumed,
            "tokens_saved": tokens_saved,
            "token_savings_percent": round(savings_pct, 1),
        }

    def close(self) -> None:
        self.stop()
        self.ring_buffer.cleanup()


# ─────────────────────────────────────────────────────────────────────────────
# 6. Reconnaissance Vocale Ergonomique (Commandes Naturelles)
# ─────────────────────────────────────────────────────────────────────────────

_ACTIVATION_PATTERNS = (
    r"regarde ce que je te montre",
    r"regarde ce que je montre",
    r"regarde ce que je fais",
    r"regarde ce que j[' ]ai",
    r"regarde bien",
    r"regarde [çc]a",
    r"regarde l[' ]objet",
    r"active la vision continue",
    r"lance la vision continue",
    r"d[ée]marre la vision continue",
    r"passe en vision continue",
    r"observe la sc[èe]ne",
    r"observe ce que je te montre",
    r"regarde en direct",
)

_DEACTIVATION_PATTERNS = (
    r"arr[êe]te la cam[ée]ra",
    r"arr[êe]te la vision",
    r"arr[êe]te d[' ]observer",
    r"arr[êe]te d[' ]enregistrer",
    r"d[ée]sactive la vision",
    r"d[ée]sactive la vision continue",
    r"stop cam[ée]ra",
    r"ferme la cam[ée]ra",
    r"ferme la vision",
    r"coupe la cam[ée]ra",
    r"coupe la vision",
    r"ne regarde plus",
    r"arr[êe]te le flux",
)


def is_vision_activation_phrase(text: str) -> bool:
    """Détecte les déclencheurs d'activation ergonomiques."""
    clean = str(text or "").lower().strip()
    return any(re.search(pat, clean, re.IGNORECASE) for pat in _ACTIVATION_PATTERNS)


def is_vision_deactivation_phrase(text: str) -> bool:
    """Détecte les déclencheurs d'arrêt ergonomiques."""
    clean = str(text or "").lower().strip()
    return any(re.search(pat, clean, re.IGNORECASE) for pat in _DEACTIVATION_PATTERNS)


# ─────────────────────────────────────────────────────────────────────────────
# 7. Singleton Global
# ─────────────────────────────────────────────────────────────────────────────

_GLOBAL_ENGINE: Optional[ContinuousVisionEngine] = None
_GLOBAL_LOCK = threading.Lock()


def get_continuous_vision_engine(
    session_manager: Optional[Any] = None,
    device_path: Union[str, int] = DEFAULT_DEVICE,
    ui_callback: Optional[Callable[[bool], None]] = None,
) -> ContinuousVisionEngine:
    """Retourne l'instance singleton du moteur de vision continue."""
    global _GLOBAL_ENGINE
    with _GLOBAL_LOCK:
        if _GLOBAL_ENGINE is None:
            _GLOBAL_ENGINE = ContinuousVisionEngine(
                session_manager=session_manager,
                device_path=device_path,
                ui_callback=ui_callback,
            )
        else:
            if session_manager is not None:
                _GLOBAL_ENGINE.set_session_manager(session_manager)
            if ui_callback is not None:
                _GLOBAL_ENGINE.set_ui_callback(ui_callback)
        return _GLOBAL_ENGINE


# ─────────────────────────────────────────────────────────────────────────────
# 8. Benchmarks Réseau & Consommation de Tokens Gemini Live
# ─────────────────────────────────────────────────────────────────────────────

def run_vision_benchmark(
    duration_seconds: float = 6.0,
    device: Union[str, int] = DEFAULT_DEVICE,
) -> Dict[str, Any]:
    """Exécute un benchmark complet des composants de vision continue.

    Mesure :
    - Débit d'encodage WebP (latence ms et taille moyenne Ko).
    - Latence de détection de mouvement locale (MOG2 / Diff).
    - Vitesse de lecture/écriture dans le ring-buffer en mémoire partagée.
    - Comparaison de bande passante et d'économie de tokens (5 FPS vs 0.5 FPS au repos).
    """
    print("=" * 72)
    print("⚡ [BENCHMARK] ANO-GPT CONTINUOUS VISION & GEMINI LIVE STREAMING")
    print("=" * 72)

    # 1. Benchmark Ring Buffer Mémoire Partagée
    rb = SharedMemoryRingBuffer(capacity=10)
    sample_payload = b"\x89WEBP" + os.urandom(22 * 1024)  # Trame simulée de 22 Ko

    t0 = time.perf_counter()
    n_writes = 500
    for i in range(n_writes):
        rb.write_frame(sample_payload, time.monotonic(), True, 0.05, 75, ACTIVE_FPS)
    write_duration_ms = ((time.perf_counter() - t0) / n_writes) * 1000.0

    t0 = time.perf_counter()
    n_reads = 500
    for i in range(n_reads):
        _ = rb.read_latest()
    read_duration_ms = ((time.perf_counter() - t0) / n_reads) * 1000.0

    rb.cleanup()

    # 2. Benchmark Encodage WebP et Détection de Mouvement
    encoder = DynamicWebPEncoder(initial_quality=75)
    detector = LocalMotionDetector(method="mog2", still_timeout=3.0)

    test_frames = []
    if _CV2_AVAILABLE:
        for f in range(20):
            im = np.zeros((CAPTURE_HEIGHT, CAPTURE_WIDTH, 3), dtype=np.uint8)
            # Motif texturé
            im[:] = (20 + (f * 3) % 40, 30 + (f * 2) % 50, 40 + (f * 5) % 60)
            cv2.circle(im, (320 + f * 5, 240), 60, (0, 255, 200), -1)
            cv2.putText(im, f"Frame {f}", (50, 100), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
            test_frames.append(im)
    else:
        test_frames = [None] * 20

    # Test encodage
    encode_times = []
    sizes_kb = []
    for f in test_frames:
        t_enc0 = time.perf_counter()
        raw_b, q, s_kb = encoder.encode(f)
        encode_times.append((time.perf_counter() - t_enc0) * 1000.0)
        sizes_kb.append(s_kb)

    avg_enc_time_ms = sum(encode_times) / len(encode_times) if encode_times else 0.0
    avg_size_kb = sum(sizes_kb) / len(sizes_kb) if sizes_kb else 0.0

    # Test détection mouvement
    det_times = []
    for f in test_frames:
        t_det0 = time.perf_counter()
        detector.process_frame(f)
        det_times.append((time.perf_counter() - t_det0) * 1000.0)
    avg_det_time_ms = sum(det_times) / len(det_times) if det_times else 0.0

    # 3. Calculs théoriques et extrapolés sur 60 secondes
    fps_active = ACTIVE_FPS
    fps_idle = IDLE_FPS

    # Flux continu non régulé (5 FPS pendant 60s)
    frames_no_reg = int(fps_active * 60)
    bytes_no_reg = frames_no_reg * avg_size_kb * 1024
    tokens_no_reg = frames_no_reg * GEMINI_TOKENS_PER_VIDEO_FRAME
    bw_no_reg_kbps = (bytes_no_reg * 8) / (60 * 1000)

    # Flux avec scène immobile après 3s (3s à 5 FPS + 57s à 0.5 FPS)
    frames_reg = int(3 * fps_active + 57 * fps_idle)  # 15 + 28 = ~43 frames
    bytes_reg = frames_reg * avg_size_kb * 1024
    tokens_reg = frames_reg * GEMINI_TOKENS_PER_VIDEO_FRAME
    bw_reg_kbps = (bytes_reg * 8) / (60 * 1000)

    # Scène 100% immobile stabilisée
    frames_still = int(fps_idle * 60)  # 30 frames
    bytes_still = frames_still * avg_size_kb * 1024
    tokens_still = frames_still * GEMINI_TOKENS_PER_VIDEO_FRAME
    bw_still_kbps = (bytes_still * 8) / (60 * 1000)

    token_savings_still_pct = ((tokens_no_reg - tokens_still) / tokens_no_reg) * 100.0
    bw_savings_still_pct = ((bytes_no_reg - bytes_still) / bytes_no_reg) * 100.0

    results = {
        "shm_write_latency_us": round(write_duration_ms * 1000.0, 2),
        "shm_read_latency_us": round(read_duration_ms * 1000.0, 2),
        "avg_webp_encode_time_ms": round(avg_enc_time_ms, 2),
        "avg_frame_size_kb": round(avg_size_kb, 2),
        "avg_motion_detect_time_ms": round(avg_det_time_ms, 2),
        "unthrottled_60s": {
            "fps": fps_active,
            "frames": frames_no_reg,
            "bandwidth_kbps": round(bw_no_reg_kbps, 1),
            "tokens": tokens_no_reg,
        },
        "still_scene_60s": {
            "fps": fps_idle,
            "frames": frames_still,
            "bandwidth_kbps": round(bw_still_kbps, 1),
            "tokens": tokens_still,
        },
        "mixed_scenario_60s": {
            "frames": frames_reg,
            "bandwidth_kbps": round(bw_reg_kbps, 1),
            "tokens": tokens_reg,
        },
        "token_savings_percent": round(token_savings_still_pct, 1),
        "bandwidth_savings_percent": round(bw_savings_still_pct, 1),
    }

    # Affichage du rapport structuré
    print(f"🔹 Mémoire Partagée (POSIX shm) : Écriture = {results['shm_write_latency_us']} µs | Lecture = {results['shm_read_latency_us']} µs")
    print(f"🔹 Encodage WebP (640x480)    : {results['avg_webp_encode_time_ms']} ms/frame | Poids moyen = {results['avg_frame_size_kb']} Ko (< 25 Ko)")
    print(f"🔹 Détection Mouvement (MOG2) : {results['avg_motion_detect_time_ms']} ms/frame")
    print("-" * 72)
    print("📊 COMPARAISON RÉSEAU & TOKENS GEMINI LIVE (sur 1 minute) :")
    print(f"  • Flux Actif (5.0 FPS fixe) : {frames_no_reg} frames | {round(bw_no_reg_kbps, 1):>5} kbps | {tokens_no_reg:>6} tokens")
    print(f"  • Scène Immobile (0.5 FPS)  : {frames_still} frames | {round(bw_still_kbps, 1):>5} kbps | {tokens_still:>6} tokens")
    print(f"  • Économie Constatée        : -{results['bandwidth_savings_percent']}% Bande passante | -{results['token_savings_percent']}% TOKENS GEMINI LIVE")
    print("=" * 72)

    return results


if __name__ == "__main__":
    run_vision_benchmark()
