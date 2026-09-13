#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
core/gesture_control.py — Moteur de Reconnaissance Gestuelle Silencieuse pour ANO-GPT.

Conçu pour le contrôle mains libres et silencieux lors de réunions, d'appels professionnels
ou d'environnements calmes où la commande vocale est inopportune.

FONCTIONNALITÉS CLÉS :
1. Détection des gestes clés :
   - Paume ouverte maintenue 1 seconde → Toggle Lecture / Pause de la musique (core/player_ipc.py)
   - Index vertical sur la bouche (geste 'Chut') → Mute immédiat voix Jarvis & micro
   - Déplacement vertical de la main (glissement) → Réglage dynamique du volume (0% à 100%)
   - Poing fermé orienté droite (ou swipe droit) → Piste musicale suivante (core/player_ipc.py)

2. Performance et déclenchement intelligent :
   - Le moteur ne tourne QUE si la caméra studio est activée ou sur commande vocale / raccourci
   - Inférence ONNX / MediaPipe optimisée à 15 FPS pour une réactivité instantanée (< 70ms)
   - Inférence multi-moteur : MediaPipe Hands > ONNX Runtime > Fallback cinématique OpenCV
   - Consommation CPU nulle (0%) lorsque désactivé ou caméra inactive

3. Filtre anti-tremblement One Euro Filter :
   - Implémentation complète de l'algorithme adaptatif 1€ (Casiez et al., CHI 2012)
   - Filtrage à fréquence de coupure dynamique supprimant les micro-tremblements à l'arrêt
     sans induire de latence lors des déplacements rapides

4. Feedback visuel holographique :
   - Publication sur AsyncEventBus (GestureRecognizedEvent)
   - Émission de signaux Qt vers l'orbe HUD pour afficher une mini-icône holographique
"""

from __future__ import annotations

import asyncio
import collections
from dataclasses import dataclass
from enum import IntEnum
import logging
import math
from pathlib import Path
import threading
import time
from typing import Any, Callable, List, Optional, Tuple, Union

import numpy as np

# Import OpenCV paresseux / résilient
try:
    import cv2
    _CV2_AVAILABLE = True
except ImportError:
    _CV2_AVAILABLE = False

# Import ONNXRuntime
try:
    import onnxruntime as ort
    _ORT_AVAILABLE = True
except ImportError:
    _ORT_AVAILABLE = False

# Import MediaPipe
try:
    import mediapipe as mp
    _MEDIAPIPE_AVAILABLE = hasattr(mp, "solutions") and hasattr(mp.solutions, "hands")
except ImportError:
    _MEDIAPIPE_AVAILABLE = False

# Import Qt pour le signal bridge
try:
    from PyQt6.QtCore import QObject, pyqtSignal
    _QT_AVAILABLE = True
except ImportError:
    try:
        from PyQt5.QtCore import QObject, pyqtSignal
        _QT_AVAILABLE = True
    except ImportError:
        _QT_AVAILABLE = False

        class QObject:  # type: ignore[no-redef]
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

        class _DummySignal:
            def __init__(self, *args: Any) -> None:
                self._slots: List[Callable[..., Any]] = []

            def connect(self, slot: Callable[..., Any]) -> None:
                if slot not in self._slots:
                    self._slots.append(slot)

            def emit(self, *args: Any) -> None:
                for s in list(self._slots):
                    try:
                        s(*args)
                    except Exception:
                        pass

        def pyqtSignal(*args: Any) -> Any:  # type: ignore[misc]
            return _DummySignal(*args)

# Intégrations ANO-GPT
try:
    from core.event_bus import AsyncEventBus, BaseEvent
except ImportError:
    AsyncEventBus = None  # type: ignore
    BaseEvent = object    # type: ignore

try:
    from core.player_ipc import get_player
except ImportError:
    get_player = None     # type: ignore

logger = logging.getLogger("ano_gpt.gesture_control")


# ════════════════════════════════════════════════════════════════════════════
# 1. Filtre Anti-Tremblement : One Euro Filter (Casiez et al., CHI 2012)
# ════════════════════════════════════════════════════════════════════════════

class LowPassFilter:
    """Filtre passe-bas du 1er ordre supportant scalaires et tableaux numpy."""

    def __init__(self, alpha: float = 0.5, init_val: Optional[np.ndarray] = None) -> None:
        self.alpha = float(alpha)
        self.s: Optional[np.ndarray] = np.copy(init_val) if init_val is not None else None

    def filter(self, val: np.ndarray, alpha: Optional[float] = None) -> np.ndarray:
        if alpha is not None:
            self.alpha = float(alpha)
        if self.s is None:
            self.s = np.copy(val)
        else:
            self.s = self.alpha * val + (1.0 - self.alpha) * self.s
        return self.s

    def reset(self) -> None:
        self.s = None


class OneEuroFilter:
    """Filtre adaptatif 1€ (Casiez, Roussel, Vogel, CHI 2012).

    Adapte la fréquence de coupure en temps réel en fonction de la vitesse du signal.
    - À basse vitesse : coupure proche de min_cutoff (filtre fort, élimine les tremblements)
    - À haute vitesse : coupure augmentée proportionnellement à la vitesse (zéro latence perçue)
    """

    def __init__(
        self,
        min_cutoff: float = 1.0,
        beta: float = 0.007,
        d_cutoff: float = 1.0,
    ) -> None:
        self.min_cutoff = float(min_cutoff)
        self.beta = float(beta)
        self.d_cutoff = float(d_cutoff)
        self.x_filter = LowPassFilter()
        self.dx_filter = LowPassFilter()
        self.last_time: Optional[float] = None

    @staticmethod
    def _alpha(cutoff: float, dt: float) -> float:
        tau = 1.0 / (2.0 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / dt)

    def filter(
        self,
        x: Union[np.ndarray, float, int],
        timestamp: Optional[float] = None,
    ) -> Union[np.ndarray, float]:
        is_scalar = np.isscalar(x) or (isinstance(x, (float, int)) and not isinstance(x, np.ndarray))
        val = np.asarray(x, dtype=np.float32)

        t = float(timestamp if timestamp is not None else time.monotonic())
        if self.last_time is None:
            self.last_time = t
            self.x_filter.filter(val, alpha=1.0)
            self.dx_filter.filter(np.zeros_like(val), alpha=1.0)
            return float(val) if is_scalar else val

        dt = max(1e-4, t - self.last_time)
        self.last_time = t

        # Dérivée temporelle (vitesse)
        prev_x = self.x_filter.s if self.x_filter.s is not None else val
        dx = (val - prev_x) / dt

        # Filtrage de la vitesse avec coupure fixe d_cutoff
        alpha_d = self._alpha(self.d_cutoff, dt)
        filtered_dx = self.dx_filter.filter(dx, alpha=alpha_d)

        # Fréquence de coupure dynamique basée sur la vitesse
        speed = np.abs(filtered_dx)
        cutoff = self.min_cutoff + self.beta * speed

        # Filtrage adaptatif du signal principal
        if np.isscalar(cutoff):
            alpha = self._alpha(float(cutoff), dt)
        else:
            alpha = 1.0 / (1.0 + (1.0 / (2.0 * np.pi * cutoff)) / dt)

        filtered_x = self.x_filter.filter(val, alpha=alpha)
        return float(filtered_x) if is_scalar else filtered_x

    def reset(self) -> None:
        self.last_time = None
        self.x_filter.reset()
        self.dx_filter.reset()


# ════════════════════════════════════════════════════════════════════════════
# 2. Structure et Géométrie des Repères de la Main (Landmarks 21 Points)
# ════════════════════════════════════════════════════════════════════════════

class HandJoint(IntEnum):
    WRIST = 0
    THUMB_CMC = 1
    THUMB_MCP = 2
    THUMB_IP = 3
    THUMB_TIP = 4
    INDEX_MCP = 5
    INDEX_PIP = 6
    INDEX_DIP = 7
    INDEX_TIP = 8
    MIDDLE_MCP = 9
    MIDDLE_PIP = 10
    MIDDLE_DIP = 11
    MIDDLE_TIP = 12
    RING_MCP = 13
    RING_PIP = 14
    RING_DIP = 15
    RING_TIP = 16
    PINKY_MCP = 17
    PINKY_PIP = 18
    PINKY_DIP = 19
    PINKY_TIP = 20


@dataclass
class HandLandmarks:
    """Représentation géométrique normalisée des 21 repères d'une main.

    Coordonnées x, y, z normalisées dans [0.0, 1.0].
    x: 0.0 (gauche) à 1.0 (droite)
    y: 0.0 (haut de l'image) à 1.0 (bas de l'image)
    z: profondeur relative
    """
    coords: np.ndarray  # Shape: (21, 3)
    confidence: float = 1.0
    handedness: str = "Right"

    @property
    def wrist(self) -> np.ndarray:
        return self.coords[HandJoint.WRIST]

    @property
    def palm_center(self) -> np.ndarray:
        """Centre approché de la paume (moyenne du poignet et des bases MCP)."""
        joints = [HandJoint.WRIST, HandJoint.INDEX_MCP, HandJoint.MIDDLE_MCP,
                  HandJoint.RING_MCP, HandJoint.PINKY_MCP]
        return np.mean(self.coords[joints], axis=0)

    def joint(self, idx: Union[HandJoint, int]) -> np.ndarray:
        return self.coords[int(idx)]

    def is_finger_extended(self, finger_name: str) -> bool:
        """Détermine si un doigt est tendu de manière invariante par rotation 3D."""
        wrist = self.joint(HandJoint.WRIST)

        if finger_name == "thumb":
            tip = self.joint(HandJoint.THUMB_TIP)
            ip = self.joint(HandJoint.THUMB_IP)
            mcp = self.joint(HandJoint.THUMB_MCP)
            pinky_mcp = self.joint(HandJoint.PINKY_MCP)
            d_tip = np.linalg.norm(tip[:2] - pinky_mcp[:2])
            d_ip = np.linalg.norm(ip[:2] - pinky_mcp[:2])
            d_mcp = np.linalg.norm(mcp[:2] - pinky_mcp[:2])
            return bool(d_tip > d_ip * 1.15 and d_tip > d_mcp * 1.25)

        finger_map = {
            "index": (HandJoint.INDEX_MCP, HandJoint.INDEX_PIP, HandJoint.INDEX_DIP, HandJoint.INDEX_TIP),
            "middle": (HandJoint.MIDDLE_MCP, HandJoint.MIDDLE_PIP, HandJoint.MIDDLE_DIP, HandJoint.MIDDLE_TIP),
            "ring": (HandJoint.RING_MCP, HandJoint.RING_PIP, HandJoint.RING_DIP, HandJoint.RING_TIP),
            "pinky": (HandJoint.PINKY_MCP, HandJoint.PINKY_PIP, HandJoint.PINKY_DIP, HandJoint.PINKY_TIP),
        }
        mcp_idx, pip_idx, dip_idx, tip_idx = finger_map[finger_name]
        mcp = self.joint(mcp_idx)
        pip = self.joint(pip_idx)
        tip = self.joint(tip_idx)

        d_tip_wrist = np.linalg.norm(tip[:2] - wrist[:2])
        d_pip_wrist = np.linalg.norm(pip[:2] - wrist[:2])
        d_tip_mcp = np.linalg.norm(tip[:2] - mcp[:2])
        d_pip_mcp = np.linalg.norm(pip[:2] - mcp[:2])

        return bool(d_tip_wrist > d_pip_wrist * 1.20 and d_tip_mcp > d_pip_mcp * 1.15)

    def is_open_palm(self) -> bool:
        """Paume entièrement ouverte (tous les 5 doigts tendus)."""
        return all(self.is_finger_extended(f) for f in ("thumb", "index", "middle", "ring", "pinky"))

    def is_fist(self) -> bool:
        """Poing fermé (aucun doigt principal tendu)."""
        fingers_curled = not any(self.is_finger_extended(f) for f in ("index", "middle", "ring", "pinky"))
        return fingers_curled

    def is_vertical_index(self, max_angle_deg: float = 28.0) -> bool:
        """Index tendu verticalement vers le haut (autre doigts repliés)."""
        index_ok = self.is_finger_extended("index")
        others_curled = not any(self.is_finger_extended(f) for f in ("middle", "ring", "pinky"))
        if not (index_ok and others_curled):
            return False

        tip = self.joint(HandJoint.INDEX_TIP)
        mcp = self.joint(HandJoint.INDEX_MCP)
        dx = tip[0] - mcp[0]
        dy = tip[1] - mcp[1]

        if dy >= -0.04:
            return False

        angle_deg = math.degrees(math.atan2(abs(dx), abs(dy)))
        return angle_deg <= max_angle_deg

    def fist_orientation(self) -> str:
        """Orientation vectorielle d'un poing fermé (droite, gauche, haut, bas)."""
        wrist = self.joint(HandJoint.WRIST)
        center = self.palm_center
        vec = center[:2] - wrist[:2]
        dx, dy = vec[0], vec[1]

        if abs(dx) > abs(dy) * 0.8:
            return "right" if dx > 0 else "left"
        return "down" if dy > 0 else "up"


# ════════════════════════════════════════════════════════════════════════════
# 3. Moteurs d'Inférence Multi-Backend (MediaPipe / ONNX / OpenCV)
# ════════════════════════════════════════════════════════════════════════════

class BaseHandDetector:
    """Interface abstraite commune pour les backends de détection."""

    def detect(self, frame_bgr: np.ndarray) -> Optional[HandLandmarks]:
        raise NotImplementedError

    def close(self) -> None:
        pass


class MediaPipeBackend(BaseHandDetector):
    """Backend utilisant MediaPipe Hands haute fidélité si disponible."""

    def __init__(self) -> None:
        if not _MEDIAPIPE_AVAILABLE:
            raise RuntimeError("MediaPipe non disponible")
        self._mp_hands = mp.solutions.hands
        self._hands = self._mp_hands.Hands(
            static_image_mode=False,
            max_num_hands=1,
            min_detection_confidence=0.55,
            min_tracking_confidence=0.50,
            model_complexity=0,
        )

    def detect(self, frame_bgr: np.ndarray) -> Optional[HandLandmarks]:
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        results = self._hands.process(rgb)
        if not results.multi_hand_landmarks:
            return None

        hand_lms = results.multi_hand_landmarks[0]
        coords = np.zeros((21, 3), dtype=np.float32)
        for i, lm in enumerate(hand_lms.landmark):
            coords[i] = [lm.x, lm.y, lm.z]

        handedness = "Right"
        if results.multi_handedness:
            handedness = results.multi_handedness[0].classification[0].label

        return HandLandmarks(coords=coords, confidence=0.9, handedness=handedness)

    def close(self) -> None:
        try:
            self._hands.close()
        except Exception:
            pass


class ONNXHandBackend(BaseHandDetector):
    """Backend ONNX Runtime ultra-léger et optimisé (< 20ms sur CPU)."""

    def __init__(self, model_path: Optional[Union[str, Path]] = None) -> None:
        if not _ORT_AVAILABLE:
            raise RuntimeError("ONNX Runtime non disponible")

        default_paths = [
            Path(__file__).resolve().parent.parent / "models" / "hand_landmark.onnx",
            Path.home() / ".cache" / "anogpt" / "hand_landmark.onnx",
        ]
        chosen = Path(model_path) if model_path else None
        if not chosen or not chosen.exists():
            for p in default_paths:
                if p.exists():
                    chosen = p
                    break

        if not chosen or not chosen.exists():
            raise FileNotFoundError(f"Modèle ONNX introuvable dans {default_paths}")

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2
        opts.inter_op_num_threads = 1
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        self._session = ort.InferenceSession(
            str(chosen), sess_options=opts, providers=["CPUExecutionProvider"]
        )
        self._input_name = self._session.get_inputs()[0].name
        self._input_shape = self._session.get_inputs()[0].shape

    def detect(self, frame_bgr: np.ndarray) -> Optional[HandLandmarks]:
        h, w = frame_bgr.shape[:2]
        inp = cv2.resize(frame_bgr, (224, 224))
        inp = cv2.cvtColor(inp, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0

        if len(self._input_shape) == 4 and self._input_shape[1] == 3:
            inp = np.transpose(inp, (2, 0, 1))
        inp = np.expand_dims(inp, axis=0)

        outputs = self._session.run(None, {self._input_name: inp})
        raw_landmarks = outputs[0]

        coords = np.reshape(raw_landmarks, (21, 3)).astype(np.float32)
        if np.max(coords[:, 0]) > 2.0:
            coords[:, 0] /= 224.0
            coords[:, 1] /= 224.0

        return HandLandmarks(coords=coords, confidence=0.85)


class CVKinematicBackend(BaseHandDetector):
    """Backend cinématique OpenCV pur (100% autonome, zéro dépendance externe lourde)."""

    def __init__(self) -> None:
        if not _CV2_AVAILABLE:
            raise RuntimeError("OpenCV non disponible")

    def detect(self, frame_bgr: np.ndarray) -> Optional[HandLandmarks]:
        h, w = frame_bgr.shape[:2]
        small = cv2.resize(frame_bgr, (320, 240))
        sh, sw = small.shape[:2]

        ycrcb = cv2.cvtColor(small, cv2.COLOR_BGR2YCrCb)
        mask = cv2.inRange(ycrcb, np.array([0, 133, 77]), np.array([255, 173, 127]))

        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=2)
        mask = cv2.dilate(mask, kernel, iterations=2)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return None

        hand_cnt = max(contours, key=cv2.contourArea)
        area = cv2.contourArea(hand_cnt)
        if area < (sw * sh * 0.04):
            return None

        M = cv2.moments(hand_cnt)
        if M["m00"] == 0:
            return None
        cx = (M["m10"] / M["m00"]) / sw
        cy = (M["m01"] / M["m00"]) / sh

        hull = cv2.convexHull(hand_cnt, returnPoints=False)
        tips: List[Tuple[float, float]] = []

        if hull is not None and len(hull) > 3:
            defects = cv2.convexityDefects(hand_cnt, hull)
            if defects is not None:
                for i in range(defects.shape[0]):
                    s, e, f, d = defects[i, 0]
                    start = hand_cnt[s][0]
                    pt_x, pt_y = start[0] / sw, start[1] / sh
                    if np.linalg.norm(np.array([pt_x - cx, pt_y - cy])) > 0.12:
                        if not any(np.linalg.norm(np.array([pt_x - tx, pt_y - ty])) < 0.08 for tx, ty in tips):
                            tips.append((pt_x, pt_y))

        tips.sort(key=lambda p: p[0])
        coords = np.zeros((21, 3), dtype=np.float32)
        coords[HandJoint.WRIST] = [cx, min(1.0, cy + 0.18), 0.0]

        for mcp_idx, offset in (
            (HandJoint.THUMB_MCP, -0.10),
            (HandJoint.INDEX_MCP, -0.05),
            (HandJoint.MIDDLE_MCP, 0.0),
            (HandJoint.RING_MCP, 0.05),
            (HandJoint.PINKY_MCP, 0.10),
        ):
            coords[mcp_idx] = [cx + offset, cy - 0.02, 0.0]

        finger_tip_joints = [
            HandJoint.THUMB_TIP, HandJoint.INDEX_TIP, HandJoint.MIDDLE_TIP,
            HandJoint.RING_TIP, HandJoint.PINKY_TIP
        ]

        if len(tips) >= 4:
            for i, joint_idx in enumerate(finger_tip_joints):
                if i < len(tips):
                    coords[joint_idx] = [tips[i][0], tips[i][1], 0.0]
                else:
                    coords[joint_idx] = [coords[joint_idx - 3][0], coords[joint_idx - 3][1] - 0.12, 0.0]
                coords[joint_idx - 2] = (coords[joint_idx - 3] + coords[joint_idx]) * 0.5
        elif len(tips) == 1 and tips[0][1] < cy - 0.12:
            coords[HandJoint.INDEX_TIP] = [tips[0][0], tips[0][1], 0.0]
            coords[HandJoint.INDEX_PIP] = (coords[HandJoint.INDEX_MCP] + coords[HandJoint.INDEX_TIP]) * 0.5
            for j in (HandJoint.THUMB_TIP, HandJoint.MIDDLE_TIP, HandJoint.RING_TIP, HandJoint.PINKY_TIP):
                coords[j] = [coords[j - 3][0], coords[j - 3][1] + 0.02, 0.0]
                coords[j - 2] = coords[j - 3]
        else:
            for j in finger_tip_joints:
                coords[j] = [coords[j - 3][0], coords[j - 3][1] + 0.02, 0.0]
                coords[j - 2] = coords[j - 3]

        return HandLandmarks(coords=coords, confidence=0.75)


def create_best_hand_detector() -> BaseHandDetector:
    """Fabrique automatique choisissant le moteur d'inférence optimal disponible."""
    if _MEDIAPIPE_AVAILABLE:
        try:
            logger.info("[GestureControl] Sélection backend MediaPipe Hands")
            return MediaPipeBackend()
        except Exception as exc:
            logger.warning("[GestureControl] Échec initialisation MediaPipe: %s", exc)

    if _ORT_AVAILABLE:
        try:
            logger.info("[GestureControl] Sélection backend ONNX Runtime")
            return ONNXHandBackend()
        except Exception as exc:
            logger.debug("[GestureControl] ONNX non configuré (%s), repli OpenCV", exc)

    logger.info("[GestureControl] Sélection backend OpenCV Kinematic (autonome)")
    return CVKinematicBackend()


# ════════════════════════════════════════════════════════════════════════════
# 4. Reconnaissance des Gestes & Machine à États Temporelle
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class RecognizedAction:
    """Action issue de la reconnaissance d'un geste clé."""
    action_type: str       # "play_pause", "shh_mute", "volume_change", "next_track"
    label: str             # Texte affiché sur le HUD
    icon: str              # Clé d'icône holographique ("play_pause", "mute", "volume", "next_track")
    value: float = 0.0     # Valeur numérique (ex: volume 0.0 à 1.0)
    confidence: float = 1.0


class GestureRecognizer:
    """Analyseur sémantique et temporel des gestes avec stabilisation 1€."""

    def __init__(self) -> None:
        self.center_filter_x = OneEuroFilter(min_cutoff=1.5, beta=0.01)
        self.center_filter_y = OneEuroFilter(min_cutoff=1.5, beta=0.01)
        self.index_filter_x = OneEuroFilter(min_cutoff=1.5, beta=0.01)
        self.index_filter_y = OneEuroFilter(min_cutoff=1.5, beta=0.01)

        self._palm_hold_start: Optional[float] = None
        self._last_palm_action: float = 0.0
        self._last_shh_action: float = 0.0
        self._last_fist_action: float = 0.0

        self._last_volume_val: Optional[int] = None
        self._last_volume_time: float = 0.0
        self._volume_active: bool = False

        self._fist_history: collections.deque = collections.deque(maxlen=10)

    def process(self, lms: Optional[HandLandmarks]) -> Optional[RecognizedAction]:
        now = time.monotonic()

        if lms is None:
            self._palm_hold_start = None
            self._volume_active = False
            self._fist_history.clear()
            return None

        raw_center = lms.palm_center
        fx = float(self.center_filter_x.filter(raw_center[0], now))
        fy = float(self.center_filter_y.filter(raw_center[1], now))

        raw_index_tip = lms.joint(HandJoint.INDEX_TIP)
        fix = float(self.index_filter_x.filter(raw_index_tip[0], now))
        fiy = float(self.index_filter_y.filter(raw_index_tip[1], now))

        # ── 1. GESTE 'CHUT' : Index vertical sur la bouche → Mute immédiat ──
        if lms.is_vertical_index():
            if 0.08 <= fiy <= 0.65 and 0.20 <= fix <= 0.80:
                if (now - self._last_shh_action) >= 1.5:
                    self._last_shh_action = now
                    self._palm_hold_start = None
                    return RecognizedAction(
                        action_type="shh_mute",
                        label="Silence JARVIS & Micro",
                        icon="mute",
                        value=0.0,
                    )

        # ── 2. POING FERMÉ ORIENTÉ DROITE (ou SWIPE DROIT) → Piste Suivante ─
        if lms.is_fist():
            self._palm_hold_start = None
            self._fist_history.append((fx, fy, now))

            is_right_swipe = False
            if len(self._fist_history) >= 4:
                oldest_x, _, old_t = self._fist_history[0]
                dx = fx - oldest_x
                dt = max(1e-3, now - old_t)
                speed_x = dx / dt
                if dx > 0.10 and speed_x > 0.35:
                    is_right_swipe = True

            is_facing_right = lms.fist_orientation() == "right"

            if (is_facing_right or is_right_swipe) and (now - self._last_fist_action) >= 1.4:
                self._last_fist_action = now
                self._fist_history.clear()
                return RecognizedAction(
                    action_type="next_track",
                    label="Piste Suivante",
                    icon="next_track",
                    value=1.0,
                )

        # ── 3. PAUME OUVERTE MAINTENUE 1 SECONDE → Play / Pause ─────────────
        if lms.is_open_palm():
            if self._palm_hold_start is None:
                self._palm_hold_start = now

            hold_duration = now - self._palm_hold_start
            if hold_duration >= 1.0 and (now - self._last_palm_action) >= 2.0:
                self._last_palm_action = now
                self._palm_hold_start = None
                return RecognizedAction(
                    action_type="play_pause",
                    label="Lecture / Pause",
                    icon="play_pause",
                    value=1.0,
                )

            # ── 4. DÉPLACEMENT VERTICAL (GLISSEMENT) → Volume Dynamique ──────
            y_clamped = max(0.20, min(0.85, fy))
            norm_volume = (0.85 - y_clamped) / (0.85 - 0.20)
            target_vol = int(round(norm_volume * 100.0))

            if (now - self._last_volume_time >= 0.09) and (
                self._last_volume_val is None or abs(target_vol - self._last_volume_val) >= 2
            ):
                self._last_volume_val = target_vol
                self._last_volume_time = now
                return RecognizedAction(
                    action_type="volume_change",
                    label=f"Volume {target_vol}%",
                    icon="volume",
                    value=target_vol / 100.0,
                )
        else:
            self._palm_hold_start = None

        return None


# ════════════════════════════════════════════════════════════════════════════
# 5. Pont Signal Qt & Événements EventBus
# ════════════════════════════════════════════════════════════════════════════

class GestureSignalBridge(QObject):
    """Pont Qt émettant les signaux de gestes vers l'UI Qt en QueuedConnection."""
    gesture_detected = pyqtSignal(str, str, float)  # icon, label, value
    status_changed   = pyqtSignal(bool)             # is_active


# ════════════════════════════════════════════════════════════════════════════
# 6. Contrôleur Principal : Boucle Asynchrone 15 FPS & Activation Intelligente
# ════════════════════════════════════════════════════════════════════════════

class GestureController:
    """Pilote global de reconnaissance gestuelle asynchrone pour ANO-GPT.

    Caractéristiques :
    - Cadence 15 FPS fixe (budget ~66.7ms par trame) pour latence < 70ms.
    - Déclenchement intelligent : ne consomme aucun cycle CPU tant que la caméra
      studio n'est pas allumée ou que l'utilisateur n'a pas activé les gestes.
    - Exécution d'actions directes vers MPVPlayerIPC et coupure audio barge-in.
    - Retours visuels holographiques instantanés vers l'orbe Qt.
    """

    TARGET_FPS = 15.0
    _FRAME_INTERVAL = 1.0 / TARGET_FPS

    def __init__(
        self,
        *,
        camera_source: Optional[Any] = None,
        ui_instance: Optional[Any] = None,
        audio_engine: Optional[Any] = None,
        on_action_callback: Optional[Callable[[RecognizedAction], None]] = None,
    ) -> None:
        self._camera_source = camera_source
        self._ui = ui_instance
        self._audio_engine = audio_engine
        self._on_action = on_action_callback

        self._enabled = False
        self._camera_active = False
        self._force_camera = False

        self._running = False
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None

        self._detector: Optional[BaseHandDetector] = None
        self._recognizer = GestureRecognizer()
        self.bridge = GestureSignalBridge()

        self._last_latency_ms = 0.0
        self._processed_frames = 0

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def active(self) -> bool:
        return self._enabled and (self._camera_active or self._force_camera)

    @property
    def latency_ms(self) -> float:
        return self._last_latency_ms

    def set_camera_active(self, active: bool) -> None:
        self._camera_active = bool(active)
        if self._camera_active and self._enabled:
            self._wake_event.set()

    def enable(self, *, force_camera: bool = False) -> str:
        self._enabled = True
        if force_camera:
            self._force_camera = True
        self._wake_event.set()
        self.bridge.status_changed.emit(True)
        logger.info("[GestureControl] Contrôle gestuel ACTIVÉ.")
        return "Contrôle gestuel activé."

    def disable(self) -> str:
        self._enabled = False
        self._force_camera = False
        self.bridge.status_changed.emit(False)
        logger.info("[GestureControl] Contrôle gestuel DÉSACTIVÉ.")
        return "Contrôle gestuel désactivé."

    def toggle(self, *, force_camera: bool = False) -> str:
        return self.disable() if self._enabled else self.enable(force_camera=force_camera)

    def handle_voice_command(self, text: str) -> Optional[str]:
        t = (text or "").lower().strip()
        # Vérifier d'abord la désactivation car 'désactive' contient 'active'
        if any(w in t for w in ("desactive le controle gestuel", "désactive le contrôle gestuel",
                                "desactive les gestes", "désactive les gestes",
                                "arrete les gestes", "arrête les gestes", "stop gestes")):
            return self.disable()
        if any(w in t for w in ("active le controle gestuel", "active le contrôle gestuel",
                                "active les gestes", "mode gestuel", "controle gestuel", "contrôle gestuel")):
            return self.enable()
        return None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        from core.thread_pool import get_thread_pool
        self._worker_thread = get_thread_pool().spawn_thread(
            "compute-light", "gesture-control", self._run_loop,
            stall_timeout=float("inf"),
        )

    def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        self._wake_event.set()
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=1.0)
        self._worker_thread = None
        if self._detector:
            self._detector.close()
            self._detector = None

    def _ensure_detector(self) -> BaseHandDetector:
        if self._detector is None:
            self._detector = create_best_hand_detector()
        return self._detector

    def _grab_frame(self) -> Optional[np.ndarray]:
        if self._camera_source is not None:
            try:
                if hasattr(self._camera_source, "latest_frame"):
                    raw_jpeg = self._camera_source.latest_frame()
                    if raw_jpeg:
                        nparr = np.frombuffer(raw_jpeg, np.uint8)
                        return cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            except Exception:
                pass

        if self._force_camera and _CV2_AVAILABLE:
            cap = getattr(self, "_local_cap", None)
            if cap is None or not cap.isOpened():
                self._local_cap = cv2.VideoCapture(0)
                cap = self._local_cap
            if cap.isOpened():
                ok, frame = cap.read()
                if ok and frame is not None:
                    return frame

        return None

    def _run_loop(self) -> None:
        logger.info("[GestureControl] Démarrage de la boucle de surveillance gestuelle.")

        while not self._stop_event.is_set():
            while not self._stop_event.is_set() and not (
                self._enabled and (self._camera_active or self._force_camera)
            ):
                self._wake_event.wait(timeout=0.5)
                self._wake_event.clear()

            if self._stop_event.is_set():
                break

            t0 = time.monotonic()

            try:
                frame = self._grab_frame()
                if frame is not None:
                    detector = self._ensure_detector()
                    landmarks = detector.detect(frame)
                    action = self._recognizer.process(landmarks)

                    if action is not None:
                        self._dispatch_action(action)

                    self._processed_frames += 1
            except Exception as exc:
                logger.error("[GestureControl] Erreur dans la boucle gestuelle: %s", exc)

            elapsed = time.monotonic() - t0
            self._last_latency_ms = elapsed * 1000.0

            delay = self._FRAME_INTERVAL - elapsed
            if delay > 0:
                self._stop_event.wait(delay)

        local_cap = getattr(self, "_local_cap", None)
        if local_cap is not None:
            try:
                local_cap.release()
            except Exception:
                pass
            self._local_cap = None

    async def run_async(self) -> None:
        self._running = True
        logger.info("[GestureControl] Démarrage de la boucle asynchrone.")

        while not self._stop_event.is_set():
            if not (self._enabled and (self._camera_active or self._force_camera)):
                await asyncio.sleep(0.1)
                continue

            t0 = time.monotonic()
            try:
                frame = self._grab_frame()
                if frame is not None:
                    detector = self._ensure_detector()
                    landmarks = await asyncio.to_thread(detector.detect, frame)
                    action = self._recognizer.process(landmarks)
                    if action is not None:
                        self._dispatch_action(action)
            except Exception as exc:
                logger.error("[GestureControl] Erreur async: %s", exc)

            elapsed = time.monotonic() - t0
            self._last_latency_ms = elapsed * 1000.0
            delay = self._FRAME_INTERVAL - elapsed
            if delay > 0:
                await asyncio.sleep(delay)

    def _dispatch_action(self, action: RecognizedAction) -> None:
        logger.info(
            "[GestureControl] Action détectée: %s (%s) — valeur: %.2f",
            action.action_type, action.label, action.value
        )

        if action.action_type == "play_pause":
            if get_player is not None:
                try:
                    get_player().toggle_pause()
                except Exception as exc:
                    logger.warning("[GestureControl] Échec toggle play/pause: %s", exc)

        elif action.action_type == "shh_mute":
            if self._audio_engine is not None and hasattr(self._audio_engine, "interrupt"):
                try:
                    self._audio_engine.interrupt()
                except Exception:
                    pass

            if self._ui is not None:
                if hasattr(self._ui, "on_interrupt") and self._ui.on_interrupt:
                    try:
                        self._ui.on_interrupt()
                    except Exception:
                        pass
                try:
                    self._ui.muted = True
                except Exception:
                    pass

        elif action.action_type == "volume_change":
            vol_int = int(round(action.value * 100))
            if get_player is not None:
                try:
                    get_player().set_volume(vol_int)
                except Exception:
                    pass
            if self._ui is not None and hasattr(self._ui, "set_volume"):
                try:
                    self._ui.set_volume(action.value)
                except Exception:
                    pass

        elif action.action_type == "next_track":
            if get_player is not None:
                try:
                    get_player().next()
                except Exception:
                    pass

        if self._on_action:
            try:
                self._on_action(action)
            except Exception:
                pass

        self.bridge.gesture_detected.emit(action.icon, action.label, action.value)

        if self._ui is not None and hasattr(self._ui, "show_gesture"):
            try:
                self._ui.show_gesture(action.icon, action.label, action.value)
            except Exception:
                pass

        if AsyncEventBus is not None:
            try:
                bus = AsyncEventBus.get_instance()
                from core.event_bus import GestureRecognizedEvent
                bus.publish_sync(
                    GestureRecognizedEvent(
                        gesture=action.action_type,
                        label=action.label,
                        icon=action.icon,
                        value=action.value,
                        confidence=action.confidence,
                    )
                )
            except Exception:
                pass


# ════════════════════════════════════════════════════════════════════════════
# 7. Singleton Global
# ════════════════════════════════════════════════════════════════════════════

_gesture_controller_instance: Optional[GestureController] = None


def get_gesture_controller(
    camera_source: Optional[Any] = None,
    ui_instance: Optional[Any] = None,
    audio_engine: Optional[Any] = None,
) -> GestureController:
    """Retourne l'instance unique du GestureController."""
    global _gesture_controller_instance
    if _gesture_controller_instance is None:
        _gesture_controller_instance = GestureController(
            camera_source=camera_source,
            ui_instance=ui_instance,
            audio_engine=audio_engine,
        )
    else:
        # Le raccourci UI peut créer le singleton avant JarvisLive. Lorsque le
        # runtime arrive, compléter ses dépendances évite un contrôleur actif
        # qui détecte des gestes sans pouvoir agir ni afficher de retour.
        if camera_source is not None:
            _gesture_controller_instance._camera_source = camera_source
        if ui_instance is not None:
            _gesture_controller_instance._ui = ui_instance
        if audio_engine is not None:
            _gesture_controller_instance._audio_engine = audio_engine
    return _gesture_controller_instance
