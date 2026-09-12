"""tests/test_navigation.py — Tests de la navigation guidée pas-à-pas d'ANO-GPT."""

import json
import math
import time
import pytest

from core.navigation import (
    OFF_ROUTE_DISTANCE_M,
    OFF_ROUTE_TIME_THRESHOLD_S,
    RECALCULATE_MIN_INTERVAL_S,
    NavigationManager,
    NavigationRoute,
    NavigationSession,
    RouteStep,
    calculate_bearing,
    decode_valhalla_polyline,
    distance_to_polyline,
    format_maneuver_fr,
    get_navigation_manager,
    haversine_distance,
    parse_osrm_response,
    parse_valhalla_response,
    point_to_segment_distance,
    voice_instruction_for_distance,
)
from core.map_render import render_map


# ── Traduction des manœuvres en français ──────────────────────────────────────

def test_translate_turn_maneuvers():
    """Vérifie la traduction des virages et leurs variantes."""
    # Tournez à droite
    r = format_maneuver_fr({"maneuver": {"type": "turn", "modifier": "right"}, "name": "Avenue de la Gare"})
    assert r["icon"] == "turn-right"
    assert "Tournez à droite" in r["instruction"]
    assert "Avenue de la Gare" in r["instruction"]
    assert "tournez à droite sur Avenue de la Gare" in r["voice_action"]

    # Tournez franchement à gauche
    l_sharp = format_maneuver_fr({"maneuver": {"type": "turn", "modifier": "sharp left"}, "name": "Rue Neuve"})
    assert l_sharp["icon"] == "turn-sharp-left"
    assert "franchement à gauche" in l_sharp["instruction"]

    # Demi-tour
    uturn = format_maneuver_fr({"maneuver": {"type": "turn", "modifier": "uturn"}, "name": ""})
    assert uturn["icon"] == "u-turn"
    assert "Faites demi-tour" in uturn["instruction"]


def test_translate_roundabouts():
    """Vérifie la traduction des ronds-points avec numéro de sortie."""
    # 2e sortie
    rb2 = format_maneuver_fr({
        "maneuver": {"type": "roundabout", "modifier": "slight right", "exit": 2},
        "name": "Boulevard Maritime"
    })
    assert rb2["icon"] == "roundabout"
    assert "2e sortie" in rb2["instruction"]
    assert "deuxième sortie" in rb2["voice_action"]
    assert "Boulevard Maritime" in rb2["instruction"]

    # 1ère sortie
    rb1 = format_maneuver_fr({
        "maneuver": {"type": "roundabout", "modifier": "right", "exit": 1},
        "name": ""
    })
    assert "1ère sortie" in rb1["instruction"]
    assert "première sortie" in rb1["voice_action"]


def test_translate_forks_and_ramps():
    """Vérifie la traduction des bifurcations, bretelles et autoroutes."""
    fork = format_maneuver_fr({"maneuver": {"type": "fork", "modifier": "slight left"}, "name": "A1"})
    assert fork["icon"] == "fork-left"
    assert "Restez à gauche" in fork["instruction"]

    ramp = format_maneuver_fr({"maneuver": {"type": "on ramp", "modifier": "right"}, "name": "Périphérique"})
    assert ramp["icon"] == "ramp-on"
    assert "bretelle" in ramp["instruction"]

    exit_ramp = format_maneuver_fr({"maneuver": {"type": "off ramp", "modifier": "right"}, "name": "Sortie 12"})
    assert exit_ramp["icon"] == "ramp-off"
    assert "sortie" in exit_ramp["instruction"]


def test_translate_arrival():
    """Vérifie la détection d'arrivée à destination."""
    arr = format_maneuver_fr({"maneuver": {"type": "arrive"}}, is_last=True)
    assert arr["icon"] == "arrive"
    assert "arrivé à destination" in arr["instruction"]
    assert "arrivé à destination" in arr["voice_action"]


# ── Seuils d'annonces vocales (500m / 150m / maintenant) ─────────────────────

def test_voice_threshold_phrasing():
    step = {
        "voice_action": "tournez à droite sur Boulevard Maritime",
        "type": "turn",
        "icon": "turn-right"
    }

    # Palier 500m
    msg_500 = voice_instruction_for_distance(step, 500.0)
    assert "Dans 500 mètres" in msg_500
    assert "tournez à droite" in msg_500

    # Palier 150m
    msg_150 = voice_instruction_for_distance(step, 150.0)
    assert "Dans 150 mètres" in msg_150

    # Palier « maintenant » (<= 35m)
    msg_now = voice_instruction_for_distance(step, 20.0)
    assert "maintenant" in msg_now
    assert "Tournez à droite" in msg_now


# ── Géodésie & Calculs de distances ──────────────────────────────────────────

def test_haversine_distance():
    # Coordonnées connues : ~111 km par degré de latitude
    p1 = (9.0, 0.0)
    p2 = (10.0, 0.0)
    dist = haversine_distance(p1[0], p1[1], p2[0], p2[1])
    assert 110_000 <= dist <= 112_000


def test_point_to_segment_distance():
    # Segment de (0, 0) à (0, 2)
    # Point à (1, 1) -> distance orthogonale ~111.3 km
    p = (1.0, 1.0)
    a = (0.0, 0.0)
    b = (0.0, 2.0)
    dist, proj = point_to_segment_distance(p[0], p[1], a[0], a[1], b[0], b[1])
    assert 110_000 <= dist <= 112_000
    assert abs(proj[0] - 0.0) < 0.01
    assert abs(proj[1] - 1.0) < 0.01


def test_distance_to_polyline_and_off_route():
    polyline = [(9.50, -13.70), (9.51, -13.70), (9.52, -13.70)]
    # Point exactement sur la polyligne
    on_route = (9.505, -13.70)
    dist, seg, _ = distance_to_polyline(on_route[0], on_route[1], polyline)
    assert dist < 5.0

    # Point écarté de ~100m
    # 0.001 deg de lon à lat 9.5° ~ 110m
    off_point = (9.505, -13.701)
    dist_off, _, _ = distance_to_polyline(off_point[0], off_point[1], polyline)
    assert dist_off > OFF_ROUTE_DISTANCE_M


def test_calculate_bearing():
    # Plein Nord
    assert abs(calculate_bearing(9.0, 0.0, 10.0, 0.0) - 0.0) < 1.0
    # Plein Est
    assert abs(calculate_bearing(0.0, 0.0, 0.0, 1.0) - 90.0) < 1.0


# ── Parsing de réponse OSRM ──────────────────────────────────────────────────

def test_parse_osrm_response():
    mock_osrm_json = {
        "routes": [{
            "distance": 4500.0,
            "duration": 600.0,
            "geometry": {
                "coordinates": [
                    [-13.700, 9.500],
                    [-13.700, 9.520],
                    [-13.680, 9.520]
                ]
            },
            "legs": [{
                "steps": [
                    {
                        "distance": 2200.0,
                        "duration": 300.0,
                        "name": "Route du Port",
                        "maneuver": {
                            "type": "depart",
                            "modifier": "straight",
                            "location": [-13.700, 9.500]
                        }
                    },
                    {
                        "distance": 2300.0,
                        "duration": 300.0,
                        "name": "Corniche Sud",
                        "maneuver": {
                            "type": "turn",
                            "modifier": "right",
                            "location": [-13.700, 9.520]
                        }
                    },
                    {
                        "distance": 0.0,
                        "duration": 0.0,
                        "name": "Arrivée",
                        "maneuver": {
                            "type": "arrive",
                            "location": [-13.680, 9.520]
                        }
                    }
                ]
            }]
        }]
    }

    route = parse_osrm_response(
        mock_osrm_json,
        origin=(9.500, -13.700),
        destination=(9.520, -13.680),
        destination_name="Pharmacie Centrale"
    )

    assert route is not None
    assert route.distance_m == 4500.0
    assert route.duration_s == 600.0
    assert len(route.polyline) == 3
    assert len(route.steps) == 3
    assert route.steps[1].icon == "turn-right"
    assert "Tournez à droite" in route.steps[1].instruction


# ── Machine d'état de Navigation (NavigationSession) ─────────────────────────

def test_navigation_session_flow_and_announcements():
    """Simule le déplacement d'un véhicule et vérifie les paliers 500m, 150m, maintenant et arrivée."""
    step0 = RouteStep(
        index=0,
        instruction="Départ sur Route du Port",
        voice_action="prenez la route sur Route du Port",
        voice_text="Départ sur Route du Port.",
        icon="straight",
        modifier="straight",
        maneuver_type="depart",
        name="Route du Port",
        distance_m=1000.0,
        duration_s=120.0,
        location=(9.500, -13.700),
    )
    step1 = RouteStep(
        index=1,
        instruction="Tournez à droite sur Corniche",
        voice_action="tournez à droite sur Corniche",
        voice_text="Tournez à droite sur Corniche.",
        icon="turn-right",
        modifier="right",
        maneuver_type="turn",
        name="Corniche",
        distance_m=1000.0,
        duration_s=120.0,
        location=(9.510, -13.700), # Manœuvre à 9.510
    )
    step_arr = RouteStep(
        index=2,
        instruction="Arrivé à destination",
        voice_action="vous êtes arrivé à destination",
        voice_text="Vous êtes arrivé à destination.",
        icon="arrive",
        modifier="straight",
        maneuver_type="arrive",
        name="Destination",
        distance_m=0.0,
        duration_s=0.0,
        location=(9.510, -13.690),
    )

    route = NavigationRoute(
        origin=(9.500, -13.700),
        destination=(9.510, -13.690),
        destination_name="Hôpital",
        distance_m=2000.0,
        duration_s=240.0,
        polyline=[(9.500, -13.700), (9.510, -13.700), (9.510, -13.690)],
        steps=[step0, step1, step_arr]
    )

    spoken_messages = []
    session = NavigationSession(route, voice_callback=lambda msg: spoken_messages.append(msg))

    t0 = 1000.0

    # 1. Position initiale (à l'origine, step 0 validé => avance vers step 1)
    state = session.update_position(9.500, -13.700, now=t0)
    assert state["active"] is True
    assert state["step_index"] in (0, 1)

    # 2. Avance vers la manœuvre step 1 (à environ 500m de 9.510)
    # 9.5055 est à ~500m de 9.510
    state = session.update_position(9.5055, -13.700, now=t0 + 10)
    assert any("500 mètres" in m for m in spoken_messages)

    # 3. Avance à ~150m de la manœuvre (9.5086)
    spoken_messages.clear()
    state = session.update_position(9.5086, -13.700, now=t0 + 20)
    assert any("150 mètres" in m for m in spoken_messages)

    # 4. Palier « maintenant » (à 20m de la manœuvre, 9.5098)
    spoken_messages.clear()
    state = session.update_position(9.5098, -13.700, now=t0 + 30)
    assert any("maintenant" in m for m in spoken_messages)

    # 5. Franchissement de la manœuvre (9.510, -13.700) -> passage à l'étape suivante
    state = session.update_position(9.510, -13.700, now=t0 + 35)
    assert session.current_step_index == 2

    # 6. Arrivée à destination finale (9.510, -13.690)
    spoken_messages.clear()
    state = session.update_position(9.510, -13.690, now=t0 + 60)
    assert state["arrived"] is True
    assert any("arrivé à destination" in m for m in spoken_messages)


def test_navigation_off_route_detection_and_recalc_rate_limit():
    """Vérifie la détection d'écart > 60m pendant 10s et la limitation à 1 recalcul / 30s."""
    step = RouteStep(
        index=0, instruction="Route", voice_action="route", voice_text="route",
        icon="straight", modifier="straight", maneuver_type="depart", name="R1",
        distance_m=5000.0, duration_s=600.0, location=(9.500, -13.700)
    )
    route = NavigationRoute(
        origin=(9.500, -13.700),
        destination=(9.550, -13.700),
        destination_name="Fin",
        distance_m=5000.0,
        duration_s=600.0,
        polyline=[(9.500, -13.700), (9.550, -13.700)],
        steps=[step]
    )

    reroute_calls = []
    session = NavigationSession(route, reroute_callback=lambda pt: reroute_calls.append(pt))

    t0 = 1000.0

    # 1. Écart immédiat à 100m de la route à t0
    # Lon écartée de -13.700 à -13.701 (~110m)
    state = session.update_position(9.510, -13.701, now=t0)
    assert state["off_route"] is True
    assert state["needs_reroute"] is False
    assert len(reroute_calls) == 0

    # 2. Après 5s d'écart : pas encore de recalcul (seuil 10s)
    state = session.update_position(9.512, -13.701, now=t0 + 5.0)
    assert state["off_route"] is True
    assert state["needs_reroute"] is False
    assert len(reroute_calls) == 0

    # 3. Après 11s d'écart continu : déclenchement du recalcul silencieux
    state = session.update_position(9.515, -13.701, now=t0 + 11.0)
    assert state["needs_reroute"] is True
    assert len(reroute_calls) == 1

    # 4. Après 15s (4s plus tard) : toujours écarté mais rate-limit de 30s actif
    state = session.update_position(9.518, -13.701, now=t0 + 15.0)
    assert state["needs_reroute"] is False
    assert len(reroute_calls) == 1

    # 5. Après 45s (>30s depuis le dernier recalcul) : nouveau recalcul autorisé
    state = session.update_position(9.520, -13.701, now=t0 + 45.0)
    assert state["needs_reroute"] is True
    assert len(reroute_calls) == 2


# ── Rendu HTML de la carte avec HUD de navigation ────────────────────────────

def test_map_render_contains_turn_by_turn_hud_elements():
    """Vérifie que la carte HTML intègre les éléments requis pour le guidage pas-à-pas."""
    html = render_map("Navigation", (9.64, -13.57), places=[])

    # Éléments DOM du HUD de navigation
    assert 'id="nav-hud"' in html
    assert 'id="nav-dist-val"' in html
    assert 'id="nav-instr-txt"' in html
    assert 'id="nav-icon-box"' in html
    assert 'id="nav-reroute-toast"' in html
    assert 'id="nav-voice-btn"' in html

    # API JavaScript globale pour le suivi en direct
    assert "window.ANO_UPDATE_GPS" in html
    assert "window.ANO_START_NAVIGATION" in html
    assert "window.ANO_STOP_NAVIGATION" in html

    # Moteur client OSRM avec étapes et traduction
    assert "steps=true" in html
    assert "translateStep" in html
    assert "checkVoiceThresholds" in html
    assert "speechSynthesis" in html


# ── Valhalla, Vitesse / Cap & Action Navigation ───────────────────────────────

def test_decode_valhalla_polyline():
    encoded = "_ibE_seK_seK_seK"
    coords = decode_valhalla_polyline(encoded, precision=6)
    assert len(coords) >= 1
    assert isinstance(coords[0], tuple)
    assert len(coords[0]) == 2


def test_parse_valhalla_response():

    data = {
        "trip": {
            "summary": {"length": 5.2, "time": 420},
            "legs": [{
                "shape": "",
                "maneuvers": [
                    {
                        "type": 1,
                        "instruction": "Prenez la route vers le nord",
                        "street_names": ["Avenue de la Liberté"],
                        "length": 1.2,
                        "time": 90,
                    },
                    {
                        "type": 10,
                        "instruction": "Tournez à droite sur Rue du Port",
                        "street_names": ["Rue du Port"],
                        "length": 2.0,
                        "time": 180,
                    },
                    {
                        "type": 4,
                        "instruction": "Vous êtes arrivé à destination",
                        "street_names": [],
                        "length": 0.0,
                        "time": 0,
                    }
                ]
            }]
        }
    }
    route = parse_valhalla_response(data, (9.50, -13.70), (9.55, -13.65), "Port de Conakry")
    assert route is not None
    assert route.destination_name == "Port de Conakry"
    assert route.distance_m == 5200.0
    assert len(route.steps) == 3
    assert "Avenue de la Liberté" in route.steps[0].instruction
    assert route.steps[0].maneuver_type == "depart"
    assert route.steps[1].icon == "turn-right"
    assert route.steps[1].instruction.count("Rue du Port") == 1

    session = NavigationSession(route)
    state = session.update_position(9.501, -13.699)
    assert state["step_index"] == 1
    assert state["current_step"]["name"] == "Rue du Port"


def test_navigation_with_heading_and_speed():
    step0 = RouteStep(0, "Départ", "départ", "Départ.", "straight", "straight", "depart", "Rue 1", 100, 10, (9.50, -13.70))
    step1 = RouteStep(1, "Tournez à droite", "tournez à droite", "Tournez à droite.", "turn-right", "right", "turn", "Avenue 2", 400, 30, (9.503, -13.70))
    step2 = RouteStep(2, "Arrivé", "arrivé", "Arrivé.", "arrive", "straight", "arrive", "", 0, 0, (9.505, -13.70))

    route = NavigationRoute(
        origin=(9.50, -13.70),
        destination=(9.505, -13.70),
        destination_name="Arrivée",
        distance_m=500.0,
        duration_s=40.0,
        polyline=[(9.50, -13.70), (9.503, -13.70), (9.505, -13.70)],
        steps=[step0, step1, step2],
    )

    announced = []
    session = NavigationSession(route, voice_callback=lambda msg: announced.append(msg))

    # Mise à jour avec heading (45°) et vitesse (15 m/s)
    state = session.update_position(9.501, -13.70, accuracy_m=5.0, heading=45.0, speed=15.0)
    assert state["heading"] == 45.0
    assert state["speed"] == 15.0


def test_navigation_action():
    from actions.navigation import navigation_action
    from unittest.mock import patch, MagicMock

    mock_mgr = MagicMock()
    mock_mgr.is_navigating = True
    mock_mgr.get_status_summary.return_value = "Navigation active vers Paris."
    mock_mgr.stop_navigation.return_value = "Navigation vers Paris arrêtée."
    mock_mgr.start_navigation_to_query.return_value = ("Itinéraire calculé vers Kaloum : 10 km.", None)

    with patch("actions.navigation.get_navigation_manager", return_value=mock_mgr):
        res_status = navigation_action({"action": "status"})
        assert res_status == "Navigation active vers Paris."

        res_stop = navigation_action({"action": "stop"})
        assert res_stop == "Navigation vers Paris arrêtée."

        res_start = navigation_action({"action": "start", "destination": "Kaloum"})
        assert "Kaloum" in res_start
