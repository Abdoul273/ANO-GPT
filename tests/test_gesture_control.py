#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_gesture_control.py — Tests unitaires et d'intégration du contrôle gestuel."""

import time
import numpy as np

from core.gesture_control import (
    OneEuroFilter,
    HandJoint,
    HandLandmarks,
    GestureRecognizer,
    GestureController,
    RecognizedAction,
)
from core.event_bus import AsyncEventBus, GestureRecognizedEvent


def test_one_euro_filter_smoothing():
    """Vérifie que le One Euro Filter supprime le bruit stationnaire."""
    f = OneEuroFilter(min_cutoff=1.0, beta=0.007, d_cutoff=1.0)
    t = time.monotonic()
    
    # Signal stationnaire à 0.5 avec bruit gaussien
    noisy = [0.5 + np.random.normal(0, 0.05) for _ in range(50)]
    filtered = []
    for i, val in enumerate(noisy):
        filtered.append(f.filter(val, timestamp=t + i * 0.05))
    
    # Les dernières valeurs filtrées doivent être proches de 0.5 et avoir une variance réduite
    assert abs(filtered[-1] - 0.5) < 0.05
    assert np.var(filtered[25:]) < np.var(noisy[25:])


def test_one_euro_filter_responsiveness():
    """Vérifie que le One Euro Filter réagit sans latence excessive sur un échelon."""
    f = OneEuroFilter(min_cutoff=1.0, beta=0.01, d_cutoff=1.0)
    t = time.monotonic()
    
    # État initial à 0.0
    for i in range(10):
        f.filter(0.0, timestamp=t + i * 0.05)
    
    # Saut brusque à 1.0 (vitesse élevée → augmentation dynamique de cutoff)
    res = f.filter(1.0, timestamp=t + 11 * 0.05)
    assert res > 0.35  # Réaction vive dès la première trame rapide


def test_hand_landmarks_open_palm():
    """Vérifie la détection géométrique d'une paume ouverte (5 doigts tendus)."""
    coords = np.zeros((21, 3), dtype=np.float32)
    # Poignet en bas
    coords[HandJoint.WRIST] = [0.5, 0.8, 0.0]
    
    # MCPs
    for mcp in (HandJoint.INDEX_MCP, HandJoint.MIDDLE_MCP, HandJoint.RING_MCP, HandJoint.PINKY_MCP):
        coords[mcp] = [0.5, 0.5, 0.0]
    coords[HandJoint.THUMB_MCP] = [0.4, 0.6, 0.0]
    coords[HandJoint.THUMB_IP] = [0.35, 0.55, 0.0]
    coords[HandJoint.THUMB_TIP] = [0.25, 0.50, 0.0]  # Pouce écarté
    
    # PIPs et TIPs étendus vers le haut
    for base in (5, 9, 13, 17):
        coords[base + 1] = [0.5, 0.35, 0.0]  # PIP
        coords[base + 2] = [0.5, 0.25, 0.0]  # DIP
        coords[base + 3] = [0.5, 0.15, 0.0]  # TIP (bien au-dessus)
        
    lms = HandLandmarks(coords=coords)
    assert lms.is_open_palm() is True
    assert lms.is_fist() is False


def test_hand_landmarks_fist():
    """Vérifie la détection géométrique d'un poing fermé."""
    coords = np.zeros((21, 3), dtype=np.float32)
    coords[HandJoint.WRIST] = [0.5, 0.7, 0.0]
    
    # Doigts repliés : TIPs proches des MCPs
    for base in (1, 5, 9, 13, 17):
        coords[base] = [0.5, 0.5, 0.0]      # MCP
        coords[base + 1] = [0.5, 0.48, 0.0]  # PIP
        if base + 3 < 21:
            coords[base + 3] = [0.5, 0.52, 0.0]  # TIP replié vers la paume
            
    lms = HandLandmarks(coords=coords)
    assert lms.is_fist() is True
    assert lms.is_open_palm() is False


def test_hand_landmarks_vertical_index_shh():
    """Vérifie la posture d'index vertical dressé ('Chut')."""
    coords = np.zeros((21, 3), dtype=np.float32)
    coords[HandJoint.WRIST] = [0.5, 0.7, 0.0]
    
    # Index étendu verticalement vers le haut
    coords[HandJoint.INDEX_MCP] = [0.5, 0.5, 0.0]
    coords[HandJoint.INDEX_PIP] = [0.5, 0.35, 0.0]
    coords[HandJoint.INDEX_DIP] = [0.5, 0.25, 0.0]
    coords[HandJoint.INDEX_TIP] = [0.5, 0.15, 0.0]
    
    # Majeur, annulaire, auriculaire repliés
    for base in (9, 13, 17):
        coords[base] = [0.5, 0.5, 0.0]
        coords[base + 1] = [0.5, 0.48, 0.0]
        coords[base + 3] = [0.5, 0.52, 0.0]
        
    lms = HandLandmarks(coords=coords)
    assert lms.is_vertical_index() is True
    assert lms.is_open_palm() is False
    assert lms.is_fist() is False


def test_gesture_recognizer_palm_hold_1s():
    """Vérifie que la paume ouverte doit être maintenue 1 seconde pour déclencher play/pause."""
    rec = GestureRecognizer()
    coords = np.zeros((21, 3), dtype=np.float32)
    coords[HandJoint.WRIST] = [0.5, 0.8, 0.0]
    coords[HandJoint.THUMB_MCP] = [0.4, 0.6, 0.0]
    coords[HandJoint.THUMB_IP] = [0.35, 0.55, 0.0]
    coords[HandJoint.THUMB_TIP] = [0.25, 0.50, 0.0]
    for base in (5, 9, 13, 17):
        coords[base] = [0.5, 0.5, 0.0]
        coords[base + 1] = [0.5, 0.35, 0.0]
        coords[base + 3] = [0.5, 0.15, 0.0]
        
    lms = HandLandmarks(coords=coords)
    
    # Trame à t=0 : début du maintien, pas encore d'action
    action = rec.process(lms)
    # Soit rien, soit du volume dynamique au démarrage, mais pas play_pause
    assert action is None or action.action_type != "play_pause"
    
    # Simulation du temps à t = +1.1 seconde
    rec._palm_hold_start = time.monotonic() - 1.1
    action_1s = rec.process(lms)
    assert action_1s is not None
    assert action_1s.action_type == "play_pause"
    assert action_1s.icon == "play_pause"


def test_gesture_recognizer_shh_mute():
    """Vérifie le déclenchement immédiat du geste Chut."""
    rec = GestureRecognizer()
    coords = np.zeros((21, 3), dtype=np.float32)
    coords[HandJoint.WRIST] = [0.5, 0.6, 0.0]
    coords[HandJoint.INDEX_MCP] = [0.5, 0.45, 0.0]
    coords[HandJoint.INDEX_PIP] = [0.5, 0.32, 0.0]
    coords[HandJoint.INDEX_DIP] = [0.5, 0.22, 0.0]
    coords[HandJoint.INDEX_TIP] = [0.5, 0.15, 0.0]  # Bien dans la zone bouche
    
    for base in (9, 13, 17):
        coords[base] = [0.5, 0.45, 0.0]
        coords[base + 1] = [0.5, 0.44, 0.0]
        coords[base + 3] = [0.5, 0.48, 0.0]
        
    lms = HandLandmarks(coords=coords)
    action = rec.process(lms)
    assert action is not None
    assert action.action_type == "shh_mute"
    assert action.icon == "mute"


def test_gesture_controller_state_and_voice():
    """Vérifie l'activation intelligente et les commandes vocales."""
    ctl = GestureController()
    assert ctl.enabled is False
    assert ctl.active is False
    
    # Activation par commande vocale
    resp = ctl.handle_voice_command("Active le contrôle gestuel s'il te plaît")
    assert resp == "Contrôle gestuel activé."
    assert ctl.enabled is True
    assert ctl.active is False  # Caméra pas encore active
    
    # Activation de la caméra
    ctl.set_camera_active(True)
    assert ctl.active is True
    
    # Désactivation
    resp_off = ctl.handle_voice_command("Désactive le contrôle gestuel")
    assert resp_off == "Contrôle gestuel désactivé."
    assert ctl.enabled is False
    assert ctl.active is False


def test_gesture_controller_dispatch_mock():
    """Vérifie l'émission d'événements et signaux lors d'un geste reconnu."""
    received_actions = []
    bus_events = []

    bus = AsyncEventBus.get_instance()
    bus.subscribe(GestureRecognizedEvent, lambda evt: bus_events.append(evt))

    ctl = GestureController(on_action_callback=lambda act: received_actions.append(act))
    
    # Simulation d'une action reconnue
    act = RecognizedAction(
        action_type="play_pause",
        label="Lecture / Pause",
        icon="play_pause",
        value=1.0,
    )
    ctl._dispatch_action(act)
    
    assert len(received_actions) == 1
    assert received_actions[0].action_type == "play_pause"
    assert len(bus_events) >= 1
    assert bus_events[-1].gesture == "play_pause"
