"""core/navigation.py — Navigation guidée pas-à-pas pour ANO-GPT.

Prend en charge :
1. L'interrogation d'OSRM (ou Valhalla/OSM) avec ``steps=true&annotations=true``
   pour récupérer le tracé et le détail de chaque manœuvre ;
2. La traduction complète des manœuvres en français naturel et phrases vocales
   concises pour le TTS ;
3. Le calcul géodésique précis (Haversine, distance point-polyligne, relèvement) ;
4. La machine d'état de navigation (NavigationSession) :
   - Avancement automatique d'étape en étape ;
   - Annonces vocales à 3 seuils : 500 m / 150 m / « maintenant » ;
   - Détection d'écart à l'itinéraire (> 60 m pendant 10 s) avec demande de recalcul ;
   - Limitation de débit des recalculs (au maximum 1 recalcul toutes les 30 s) ;
   - Détection d'arrivée à destination.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

try:
    import requests
    _REQUESTS = True
except ImportError:
    _REQUESTS = False


# ── Serveurs de routage ───────────────────────────────────────────────────────

_OSRM_DEFAULT = "https://router.project-osrm.org/route/v1/driving/"
_OSRM_FALLBACK = "https://routing.openstreetmap.de/routed-car/route/v1/driving/"

# Seuils de navigation (spécification ANO-GPT §2)
OFF_ROUTE_DISTANCE_M = 60.0       # Écart > 60 m considéré hors itinéraire
OFF_ROUTE_TIME_THRESHOLD_S = 10.0 # Écart prolongé pendant 10 s => recalcul
RECALCULATE_MIN_INTERVAL_S = 30.0 # Au plus 1 recalcul toutes les 30 s
ARRIVAL_DISTANCE_M = 30.0         # Distance pour considérer l'arrivée atteinte
STEP_COMPLETION_RADIUS_M = 35.0   # Rayon pour valider le passage d'une étape


# ── Traduction des manœuvres en français ──────────────────────────────────────

# Types de manœuvres OSRM :
# depart, turn, new name, roundabout, rotary, roundabout turn, exit roundabout,
# fork, merge, on ramp, off ramp, end of road, continue, arrive, use lane

_MODIFIERS_FR = {
    "uturn": "demi-tour",
    "sharp left": "franchement à gauche",
    "left": "à gauche",
    "slight left": "légèrement à gauche",
    "straight": "tout droit",
    "slight right": "légèrement à droite",
    "right": "à droite",
    "sharp right": "franchement à droite",
}

_MODIFIERS_VOICE = {
    "uturn": "faites demi-tour",
    "sharp left": "tournez franchement à gauche",
    "left": "tournez à gauche",
    "slight left": "serrez à gauche",
    "straight": "continuez tout droit",
    "slight right": "serrez à droite",
    "right": "tournez à droite",
    "sharp right": "tournez franchement à droite",
}

_ICONS_BY_MODIFIER = {
    "uturn": "u-turn",
    "sharp left": "turn-sharp-left",
    "left": "turn-left",
    "slight left": "turn-slight-left",
    "straight": "straight",
    "slight right": "turn-slight-right",
    "right": "turn-right",
    "sharp right": "turn-sharp-right",
}


def _ordinal_fr(number: int) -> str:
    """Convertit un numéro de sortie en adjectif ordinal français."""
    if number == 1:
        return "1ère"
    return f"{number}e"


def _ordinal_voice_fr(number: int) -> str:
    """Version parlée pour le TTS."""
    if number == 1:
        return "première"
    if number == 2:
        return "deuxième"
    if number == 3:
        return "troisième"
    if number == 4:
        return "quatrième"
    if number == 5:
        return "cinquième"
    if number == 6:
        return "sixième"
    return f"{number}ième"


def format_maneuver_fr(step: dict[str, Any], is_last: bool = False) -> dict[str, Any]:
    """Traduit une étape OSRM en libellés français (HUD et synthèse vocale).

    Renvoie un dictionnaire structuré contenant :
    - instruction : texte court pour l'affichage HUD
    - voice_action : action brute sans distance (ex: "tournez à droite")
    - voice_text : phrase complète par défaut
    - icon : identifiant de pictogramme
    - modifier : modificateur OSRM
    - type : type de manœuvre OSRM
    - name : nom de la voie
    """
    maneuver = step.get("maneuver") or {}
    m_type = maneuver.get("type", "turn")
    modifier = maneuver.get("modifier", "straight")
    name = (step.get("name") or "").strip()
    exit_no = maneuver.get("exit")

    target_road = f" sur {name}" if name else ""
    voice_road = f" sur {name}" if name else ""

    icon = _ICONS_BY_MODIFIER.get(modifier, "straight")

    if is_last or m_type == "arrive":
        instruction = "Vous êtes arrivé à destination"
        voice_action = "vous êtes arrivé à destination"
        voice_text = "Vous êtes arrivé à destination."
        icon = "arrive"

    elif m_type == "depart":
        instruction = f"Départ{target_road}" if target_road else "Prenez la route"
        voice_action = f"prenez la route{voice_road}"
        voice_text = f"Prenez la route{voice_road}."
        icon = "straight"

    elif m_type in ("roundabout", "rotary", "roundabout turn"):
        icon = "roundabout"
        if exit_no:
            ord_str = _ordinal_fr(exit_no)
            ord_v = _ordinal_voice_fr(exit_no)
            instruction = f"Au rond-point, {ord_str} sortie{target_road}"
            voice_action = f"au rond-point, prenez la {ord_v} sortie{voice_road}"
            voice_text = f"Au rond-point, prenez la {ord_v} sortie{voice_road}."
        else:
            mod_text = _MODIFIERS_FR.get(modifier, "vers la sortie")
            instruction = f"Au rond-point, {mod_text}{target_road}"
            voice_action = f"au rond-point, prenez {mod_text}{voice_road}"
            voice_text = f"Au rond-point, prenez {mod_text}{voice_road}."

    elif m_type == "exit roundabout":
        instruction = f"Sortez du rond-point{target_road}"
        voice_action = f"sortez du rond-point{voice_road}"
        voice_text = f"Sortez du rond-point{voice_road}."
        icon = "turn-right" if "right" in modifier else "turn-left"

    elif m_type == "fork":
        if "left" in modifier:
            instruction = f"Restez à gauche{target_road}"
            voice_action = f"restez sur la gauche{voice_road}"
            icon = "fork-left"
        elif "right" in modifier:
            instruction = f"Restez à droite{target_road}"
            voice_action = f"restez sur la droite{voice_road}"
            icon = "fork-right"
        else:
            instruction = f"Empruntez la bifurcation{target_road}"
            voice_action = f"empruntez la bifurcation{voice_road}"
            icon = "straight"
        voice_text = f"{voice_action.capitalize()}."

    elif m_type == "merge":
        instruction = f"Rejoignez la voie{target_road}"
        voice_action = f"rejoignez la voie{voice_road}"
        voice_text = f"Rejoignez la voie{voice_road}."
        icon = "merge"

    elif m_type == "on ramp":
        instruction = f"Prenez la bretelle{target_road}"
        voice_action = f"prenez la bretelle d'accès{voice_road}"
        voice_text = f"Prenez la bretelle d'accès{voice_road}."
        icon = "ramp-on"

    elif m_type == "off ramp":
        instruction = f"Prenez la sortie{target_road}"
        voice_action = f"prenez la sortie{voice_road}"
        voice_text = f"Prenez la sortie{voice_road}."
        icon = "ramp-off"

    elif m_type == "end of road":
        if "left" in modifier:
            instruction = f"En bout de route, à gauche{target_road}"
            voice_action = f"en bout de route, tournez à gauche{voice_road}"
            icon = "turn-left"
        elif "right" in modifier:
            instruction = f"En bout de route, à droite{target_road}"
            voice_action = f"en bout de route, tournez à droite{voice_road}"
            icon = "turn-right"
        else:
            instruction = f"En bout de route, continuez{target_road}"
            voice_action = f"en bout de route, continuez{voice_road}"
            icon = "straight"
        voice_text = f"{voice_action.capitalize()}."

    elif m_type in ("continue", "new name"):
        if modifier == "straight" or not modifier:
            instruction = f"Continuez{target_road}"
            voice_action = f"continuez tout droit{voice_road}"
            icon = "straight"
        else:
            action = _MODIFIERS_VOICE.get(modifier, "continuez")
            instruction = f"Continuez {_MODIFIERS_FR.get(modifier, '')}{target_road}"
            voice_action = f"{action}{voice_road}"
            icon = _ICONS_BY_MODIFIER.get(modifier, "straight")
        voice_text = f"{voice_action.capitalize()}."

    else:  # turn ou par défaut
        if modifier == "uturn":
            instruction = f"Faites demi-tour{target_road}"
            voice_action = f"faites demi-tour{voice_road}"
            icon = "u-turn"
        elif modifier in ("left", "sharp left", "slight left"):
            action = _MODIFIERS_VOICE.get(modifier, "tournez à gauche")
            instruction = f"Tournez {_MODIFIERS_FR.get(modifier, 'à gauche')}{target_road}"
            voice_action = f"{action}{voice_road}"
            icon = _ICONS_BY_MODIFIER.get(modifier, "turn-left")
        elif modifier in ("right", "sharp right", "slight right"):
            action = _MODIFIERS_VOICE.get(modifier, "tournez à droite")
            instruction = f"Tournez {_MODIFIERS_FR.get(modifier, 'à droite')}{target_road}"
            voice_action = f"{action}{voice_road}"
            icon = _ICONS_BY_MODIFIER.get(modifier, "turn-right")
        elif modifier == "straight":
            instruction = f"Continuez tout droit{target_road}"
            voice_action = f"continuez tout droit{voice_road}"
            icon = "straight"
        else:
            instruction = f"Continuez{target_road}"
            voice_action = f"continuez{voice_road}"
            icon = "straight"
        voice_text = f"{voice_action.capitalize()}."

    return {
        "instruction": instruction,
        "voice_action": voice_action,
        "voice_text": voice_text,
        "icon": icon,
        "modifier": modifier,
        "type": m_type,
        "name": name,
    }


def voice_instruction_for_distance(step_dict: dict[str, Any], distance_m: float) -> str:
    """Génère la phrase exacte à prononcer selon la distance restante.

    Trois paliers fondamentaux :
    - ~500 m : « Dans 500 mètres, tournez à droite sur Rue de la Paix »
    - ~150 m : « Dans 150 mètres, tournez à droite »
    - <= 30 m (« maintenant ») : « Tournez à droite maintenant. »
    """
    voice_action = step_dict.get("voice_action") or "continuez"
    m_type = step_dict.get("type", "")

    if m_type == "arrive" or step_dict.get("icon") == "arrive":
        if distance_m <= 35:
            return "Vous êtes arrivé à destination."
        if distance_m <= 180:
            return "Vous arrivez à destination dans 150 mètres."
        if distance_m <= 550:
            return "Vous arriverez à destination dans 500 mètres."
        return f"Destination dans {int(distance_m)} mètres."

    if distance_m <= 35:
        # Palier « maintenant »
        action_clean = voice_action.replace("tournez ", "tournez ").strip()
        return f"{action_clean.capitalize()} maintenant."

    if distance_m <= 220:
        # Palier ~150 m
        return f"Dans 150 mètres, {voice_action}."

    if distance_m <= 650:
        # Palier ~500 m
        return f"Dans 500 mètres, {voice_action}."

    # Pour les distances plus grandes
    if distance_m >= 1000:
        km = distance_m / 1000.0
        return f"Dans {km:.1f} kilomètre{'s' if km >= 2 else ''}, {voice_action}."
    return f"Dans {int(round(distance_m / 50.0) * 50)} mètres, {voice_action}."


# ── Géodésie & Calculs de trajectoire ────────────────────────────────────────

def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calcule la distance à vol d'oiseau en mètres entre deux coordonnées (WGS84)."""
    R = 6371000.0  # Rayon terrestre moyen en mètres
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (math.sin(delta_phi / 2.0) ** 2 +
         math.cos(phi1) * math.cos(phi2) * (math.sin(delta_lambda / 2.0) ** 2))
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return R * c


def point_to_segment_distance(
    p_lat: float, p_lon: float,
    a_lat: float, a_lon: float,
    b_lat: float, b_lon: float,
) -> tuple[float, tuple[float, float]]:
    """Distance minimale en mètres d'un point P au segment [A, B], avec point projeté."""
    R = 6371000.0
    mean_lat_rad = math.radians((a_lat + b_lat + p_lat) / 3.0)
    kx = R * math.cos(mean_lat_rad) * math.pi / 180.0
    ky = R * math.pi / 180.0

    ax, ay = a_lon * kx, a_lat * ky
    bx, by = b_lon * kx, b_lat * ky
    px, py = p_lon * kx, p_lat * ky

    dx = bx - ax
    dy = by - ay
    seg_len_sq = dx * dx + dy * dy

    if seg_len_sq <= 1e-9:
        dist = math.hypot(px - ax, py - ay)
        return dist, (a_lat, a_lon)

    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg_len_sq))
    proj_x = ax + t * dx
    proj_y = ay + t * dy

    dist = math.hypot(px - proj_x, py - proj_y)
    proj_lat = proj_y / ky
    proj_lon = proj_x / kx

    return dist, (proj_lat, proj_lon)


def distance_to_polyline(
    p_lat: float, p_lon: float,
    polyline: list[tuple[float, float]],
) -> tuple[float, int, tuple[float, float]]:
    """Calcule la distance minimale (mètres) d'un point à une polyligne.

    Renvoie : (distance_min_m, index_segment_plus_proche, (proj_lat, proj_lon))
    """
    if not polyline:
        return float("inf"), -1, (p_lat, p_lon)
    if len(polyline) == 1:
        d = haversine_distance(p_lat, p_lon, polyline[0][0], polyline[0][1])
        return d, 0, polyline[0]

    min_dist = float("inf")
    best_seg = 0
    best_proj = polyline[0]

    for i in range(len(polyline) - 1):
        a = polyline[i]
        b = polyline[i + 1]
        dist, proj = point_to_segment_distance(p_lat, p_lon, a[0], a[1], b[0], b[1])
        if dist < min_dist:
            min_dist = dist
            best_seg = i
            best_proj = proj

    return min_dist, best_seg, best_proj


def calculate_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calcule le cap (azimut) en degrés [0, 360) entre deux coordonnées."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_lambda = math.radians(lon2 - lon1)

    y = math.sin(delta_lambda) * math.cos(phi2)
    x = (math.cos(phi1) * math.sin(phi2) -
         math.sin(phi1) * math.cos(phi2) * math.cos(delta_lambda))
    bearing = math.degrees(math.atan2(y, x))
    return (bearing + 360.0) % 360.0


# ── Parsing d'itinéraire OSRM ────────────────────────────────────────────────

@dataclass
class RouteStep:
    index: int
    instruction: str
    voice_action: str
    voice_text: str
    icon: str
    modifier: str
    maneuver_type: str
    name: str
    distance_m: float
    duration_s: float
    location: tuple[float, float]  # (lat, lon)
    bearing_after: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "instruction": self.instruction,
            "voice_action": self.voice_action,
            "voice_text": self.voice_text,
            "icon": self.icon,
            "modifier": self.modifier,
            "type": self.maneuver_type,
            "name": self.name,
            "distance_m": round(self.distance_m, 1),
            "duration_s": round(self.duration_s, 1),
            "lat": self.location[0],
            "lon": self.location[1],
            "bearing_after": self.bearing_after,
        }


@dataclass
class NavigationRoute:
    origin: tuple[float, float]       # (lat, lon)
    destination: tuple[float, float]  # (lat, lon)
    destination_name: str
    distance_m: float
    duration_s: float
    polyline: list[tuple[float, float]] # [(lat, lon), ...]
    steps: list[RouteStep]
    raw_data: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "origin": [self.origin[0], self.origin[1]],
            "destination": [self.destination[0], self.destination[1]],
            "destination_name": self.destination_name,
            "distance_m": round(self.distance_m, 1),
            "duration_s": round(self.duration_s, 1),
            "polyline": [[p[0], p[1]] for p in self.polyline],
            "steps": [s.to_dict() for s in self.steps],
        }


def parse_osrm_response(
    data: dict[str, Any],
    origin: tuple[float, float],
    destination: tuple[float, float],
    destination_name: str = "Destination",
) -> Optional[NavigationRoute]:
    """Parse et traduit la réponse JSON d'OSRM en objet NavigationRoute structuré."""
    routes = data.get("routes") or []
    if not routes:
        return None

    route_raw = routes[0]
    total_dist = float(route_raw.get("distance", 0.0))
    total_dur = float(route_raw.get("duration", 0.0))

    coords = (route_raw.get("geometry") or {}).get("coordinates") or []
    polyline = [(float(c[1]), float(c[0])) for c in coords if len(c) >= 2]

    steps: list[RouteStep] = []
    legs = route_raw.get("legs") or []

    step_counter = 0
    for leg in legs:
        raw_steps = leg.get("steps") or []
        for i, s in enumerate(raw_steps):
            is_last = (i == len(raw_steps) - 1)
            parsed_fr = format_maneuver_fr(s, is_last=is_last)
            maneuver = s.get("maneuver") or {}
            loc = maneuver.get("location") or [destination[1], destination[0]]
            loc_latlon = (float(loc[1]), float(loc[0]))

            step_obj = RouteStep(
                index=step_counter,
                instruction=parsed_fr["instruction"],
                voice_action=parsed_fr["voice_action"],
                voice_text=parsed_fr["voice_text"],
                icon=parsed_fr["icon"],
                modifier=parsed_fr["modifier"],
                maneuver_type=parsed_fr["type"],
                name=parsed_fr["name"],
                distance_m=float(s.get("distance", 0.0)),
                duration_s=float(s.get("duration", 0.0)),
                location=loc_latlon,
                bearing_after=maneuver.get("bearing_after"),
            )
            steps.append(step_obj)
            step_counter += 1

    return NavigationRoute(
        origin=origin,
        destination=destination,
        destination_name=destination_name,
        distance_m=total_dist,
        duration_s=total_dur,
        polyline=polyline,
        steps=steps,
        raw_data=data,
    )


# ── Serveurs de routage ───────────────────────────────────────────────────────

_OSRM_DRIVING = "https://router.project-osrm.org/route/v1/driving/"
_OSRM_WALKING = "https://routing.openstreetmap.de/routed-foot/route/v1/driving/"
_OSRM_CYCLING = "https://routing.openstreetmap.de/routed-bike/route/v1/driving/"
_OSRM_FALLBACK = "https://routing.openstreetmap.de/routed-car/route/v1/driving/"
_VALHALLA_URL = "https://valhalla1.openstreetmap.de/route"


def decode_valhalla_polyline(encoded: str, precision: int = 6) -> list[tuple[float, float]]:
    """Décode une polyligne encodée (Valhalla 6 décimales ou Google Polyline)."""
    factor = float(10 ** precision)
    coordinates = []
    index, lat, lon = 0, 0, 0
    length = len(encoded)

    while index < length:
        # Latitude
        shift, result = 0, 0
        while True:
            byte = ord(encoded[index]) - 63
            index += 1
            result |= (byte & 0x1F) << shift
            shift += 5
            if byte < 0x20:
                break
        delta_lat = ~(result >> 1) if (result & 1) else (result >> 1)
        lat += delta_lat

        # Longitude
        shift, result = 0, 0
        while True:
            if index >= length:
                break
            byte = ord(encoded[index]) - 63
            index += 1
            result |= (byte & 0x1F) << shift
            shift += 5
            if byte < 0x20:
                break
        delta_lon = ~(result >> 1) if (result & 1) else (result >> 1)
        lon += delta_lon

        coordinates.append((lat / factor, lon / factor))

    return coordinates


def parse_valhalla_response(
    data: dict[str, Any],
    origin: tuple[float, float],
    destination: tuple[float, float],
    destination_name: str = "Destination",
) -> Optional[NavigationRoute]:
    """Parse la réponse JSON de Valhalla en objet NavigationRoute structuré."""
    trip = data.get("trip")
    if not trip:
        return None

    summary = trip.get("summary") or {}
    total_dist = float(summary.get("length", 0.0)) * 1000.0  # km -> m
    total_dur = float(summary.get("time", 0.0))

    legs = trip.get("legs") or []
    if not legs:
        return None

    leg = legs[0]
    shape_str = leg.get("shape") or ""
    polyline = decode_valhalla_polyline(shape_str, precision=6) if shape_str else [origin, destination]

    raw_maneuvers = leg.get("maneuvers") or []
    steps: list[RouteStep] = []

    for i, m in enumerate(raw_maneuvers):
        is_last = (i == len(raw_maneuvers) - 1)
        instr = m.get("instruction") or ("Arrivé" if is_last else "Continuez")
        street_names = m.get("street_names") or []
        street_name = street_names[0] if street_names else ""
        # Valhalla fournit parfois une instruction générique (par exemple
        # « Prenez la route vers le nord ») et place le nom de voie uniquement
        # dans ``street_names``. Le HUD et le TTS doivent conserver ce nom : il
        # est indispensable pour distinguer deux manœuvres proches. Ne pas le
        # rajouter lorsqu'il est déjà inclus dans l'instruction.
        if street_name and street_name.casefold() not in instr.casefold():
            separator = " sur " if not is_last else " — "
            instr = f"{instr.rstrip(' .')}{separator}{street_name}"
        dist_m = float(m.get("length", 0.0)) * 1000.0
        dur_s = float(m.get("time", 0.0))
        m_type = m.get("type", 0)

        # Extraction de la position de la manœuvre depuis la polyligne
        shape_idx = m.get("begin_shape_index", 0)
        loc = polyline[shape_idx] if shape_idx < len(polyline) else destination

        # Mapping des icônes
        icon = "straight"
        if is_last or m_type in (4, 5, 6):
            icon = "arrive"
        elif m_type in (9, 10, 11, 18, 20):
            icon = "turn-right"
        elif m_type in (14, 15, 16, 19, 21):
            icon = "turn-left"
        elif m_type in (12, 13):
            icon = "u-turn"
        elif m_type in (26, 27):
            icon = "roundabout"

        # Action vocale
        voice_action = instr.lower()
        if voice_action.endswith("."):
            voice_action = voice_action[:-1]

        if is_last or m_type in (4, 5, 6):
            maneuver_type = "arrive"
        elif i == 0 or m_type in (1, 2, 3):
            maneuver_type = "depart"
        else:
            maneuver_type = "turn"

        step_obj = RouteStep(
            index=i,
            instruction=instr,
            voice_action=voice_action,
            voice_text=instr + ("." if not instr.endswith(".") else ""),
            icon=icon,
            modifier="straight" if "right" not in icon and "left" not in icon else ("right" if "right" in icon else "left"),
            maneuver_type=maneuver_type,
            name=street_name,
            distance_m=dist_m,
            duration_s=dur_s,
            location=loc,
            bearing_after=m.get("bearing_after"),
        )
        steps.append(step_obj)

    return NavigationRoute(
        origin=origin,
        destination=destination,
        destination_name=destination_name,
        distance_m=total_dist,
        duration_s=total_dur,
        polyline=polyline,
        steps=steps,
        raw_data=data,
    )


def fetch_valhalla_route(
    start: tuple[float, float],
    end: tuple[float, float],
    destination_name: str = "Destination",
    mode: str = "driving",
    timeout: float = 8.0,
) -> Optional[NavigationRoute]:
    """Interroge le moteur de routage Valhalla."""
    if not _REQUESTS:
        return None

    costing = "auto"
    if mode in ("walk", "walking", "pedestrian"):
        costing = "pedestrian"
    elif mode in ("bike", "bicycle", "cycling"):
        costing = "bicycle"

    payload = {
        "locations": [
            {"lat": start[0], "lon": start[1]},
            {"lat": end[0], "lon": end[1]},
        ],
        "costing": costing,
        "directions_options": {
            "language": "fr-FR",
            "units": "kilometers",
        },
    }

    try:
        resp = requests.post(
            _VALHALLA_URL,
            json=payload,
            headers={"User-Agent": "ANO-GPT/1.0 (GPS Assistant)", "Content-Type": "application/json"},
            timeout=timeout,
        )
        if resp.status_code == 200:
            return parse_valhalla_response(resp.json(), start, end, destination_name)
    except Exception:
        pass
    return None


def fetch_osrm_route(
    start: tuple[float, float],
    end: tuple[float, float],
    destination_name: str = "Destination",
    mode: str = "driving",
    timeout: float = 8.0,
) -> Optional[NavigationRoute]:
    """Interroge le serveur OSRM avec steps=true & annotations=true."""
    if not _REQUESTS:
        return None

    base_url = _OSRM_DRIVING
    if mode in ("walk", "walking", "pedestrian"):
        base_url = _OSRM_WALKING
    elif mode in ("bike", "bicycle", "cycling"):
        base_url = _OSRM_CYCLING

    url = (
        f"{base_url}{start[1]},{start[0]};{end[1]},{end[0]}"
        "?steps=true&annotations=true&overview=full&geometries=geojson"
    )

    servers = [url, url.replace(_OSRM_DRIVING, _OSRM_FALLBACK)]
    for req_url in servers:
        try:
            resp = requests.get(
                req_url,
                headers={"User-Agent": "ANO-GPT/1.0 (GPS Assistant)"},
                timeout=timeout,
            )
            if resp.status_code == 200:
                data = resp.json()
                route = parse_osrm_response(data, start, end, destination_name)
                if route:
                    return route
        except Exception:
            continue

    return None


def fetch_route(
    start: tuple[float, float],
    end: tuple[float, float],
    destination_name: str = "Destination",
    mode: str = "driving",
    timeout: float = 8.0,
) -> Optional[NavigationRoute]:
    """Récupère l'itinéraire en chaînant automatiquement OSRM -> OSM Routing -> Valhalla."""
    # 1. Essai OSRM
    route = fetch_osrm_route(start, end, destination_name, mode=mode, timeout=timeout)
    if route:
        return route

    # 2. Essai Valhalla
    route_val = fetch_valhalla_route(start, end, destination_name, mode=mode, timeout=timeout)
    if route_val:
        return route_val

    return None


# ── Moteur d'état de navigation (NavigationSession) ──────────────────────────

class NavigationSession:
    """Session de navigation active avec gestion des seuils vocaux et écart d'itinéraire."""

    def __init__(
        self,
        route: NavigationRoute,
        voice_callback: Optional[Callable[[str], None]] = None,
        reroute_callback: Optional[Callable[[tuple[float, float]], None]] = None,
    ):
        self.route = route
        self.voice_callback = voice_callback
        self.reroute_callback = reroute_callback

        self.current_step_index = 0
        self.is_active = True
        self.is_arrived = False

        self.last_lat: Optional[float] = None
        self.last_lon: Optional[float] = None
        self.last_update_time: float = 0.0

        self.off_route_start_time: Optional[float] = None
        self.last_recalculation_time: float = 0.0

        self._announced: Dict[int, set[str]] = {}

    def update_position(
        self,
        lat: float,
        lon: float,
        accuracy_m: Optional[float] = None,
        heading: Optional[float] = None,
        speed: Optional[float] = None,
        now: Optional[float] = None,
    ) -> dict[str, Any]:
        """Met à jour la position GPS courante et avance l'état de navigation."""
        if not self.is_active or self.is_arrived:
            return {"active": False, "arrived": self.is_arrived}

        curr_time = now if now is not None else time.time()
        prev_lat = self.last_lat
        prev_lon = self.last_lon
        prev_time = self.last_update_time

        self.last_lat = lat
        self.last_lon = lon
        self.last_update_time = curr_time

        # Vitesse calculée (m/s) si non fournie par le matériel
        calc_speed = speed
        if calc_speed is None and prev_lat is not None and prev_lon is not None and (curr_time - prev_time) > 0.5:
            d_prev = haversine_distance(prev_lat, prev_lon, lat, lon)
            calc_speed = d_prev / (curr_time - prev_time)

        # 1. Distance à la destination finale
        dist_to_dest = haversine_distance(
            lat, lon, self.route.destination[0], self.route.destination[1]
        )
        if dist_to_dest <= ARRIVAL_DISTANCE_M:
            self.is_arrived = True
            self.is_active = False
            announcement = "Vous êtes arrivé à destination."
            self._announce(announcement)
            return {
                "active": False,
                "arrived": True,
                "distance_to_dest_m": 0.0,
                "voice_announcement": announcement,
                "instruction": "Arrivé à destination",
                "icon": "arrive",
                "heading": heading,
                "speed": calc_speed,
            }

        # 2. Distance à la polyligne globale pour contrôle hors-piste (>60 m pendant 10 s)
        dist_to_route, seg_idx, proj_pt = distance_to_polyline(lat, lon, self.route.polyline)
        is_off_route = dist_to_route > OFF_ROUTE_DISTANCE_M

        needs_reroute = False
        if is_off_route:
            if self.off_route_start_time is None:
                self.off_route_start_time = curr_time
            elif (curr_time - self.off_route_start_time) >= OFF_ROUTE_TIME_THRESHOLD_S:
                if (curr_time - self.last_recalculation_time) >= RECALCULATE_MIN_INTERVAL_S:
                    needs_reroute = True
                    self.last_recalculation_time = curr_time
                    self.off_route_start_time = None
                    if self.reroute_callback:
                        try:
                            self.reroute_callback((lat, lon))
                        except Exception:
                            pass
        else:
            self.off_route_start_time = None

        # L'instruction de départ est déjà prononcée par
        # ``start_navigation``. Elle décrit le point d'origine et ne doit pas
        # rester l'étape active après la première position GPS (sinon la
        # distance à ce point augmente et le guidage reste bloqué dessus).
        if (
            self.current_step_index == 0
            and len(self.route.steps) > 1
            and self.route.steps[0].maneuver_type == "depart"
        ):
            self.current_step_index = 1

        # 3. Étape courante et distance
        current_step = (
            self.route.steps[self.current_step_index]
            if self.current_step_index < len(self.route.steps)
            else self.route.steps[-1]
        )
        following_step = (
            self.route.steps[self.current_step_index + 1]
            if self.current_step_index + 1 < len(self.route.steps)
            else None
        )

        dist_to_step = haversine_distance(lat, lon, current_step.location[0], current_step.location[1])

        # 4. Seuils d'annonces vocales dynamiques ajustés selon la vitesse
        # À 90 km/h (25 m/s), le seuil 500m est annoncé plus tôt (~600m), à pied (~1.2 m/s) plus tard.
        spd_m_s = max(1.0, calc_speed or 8.0)
        t500_max = max(550.0, 500.0 + spd_m_s * 5.0)
        t500_min = 250.0
        t150_max = max(180.0, 150.0 + spd_m_s * 2.5)
        t150_min = 40.0
        t_now = max(30.0, spd_m_s * 2.0)

        step_idx = self.current_step_index
        if step_idx not in self._announced:
            self._announced[step_idx] = set()

        voice_phrase: Optional[str] = None

        # Seuil 500 m
        if t500_min < dist_to_step <= t500_max and "500m" not in self._announced[step_idx]:
            self._announced[step_idx].add("500m")
            voice_phrase = voice_instruction_for_distance(current_step.to_dict(), dist_to_step)
            self._announce(voice_phrase)

        # Seuil 150 m
        elif t150_min < dist_to_step <= t150_max and "150m" not in self._announced[step_idx]:
            self._announced[step_idx].add("150m")
            voice_phrase = voice_instruction_for_distance(current_step.to_dict(), dist_to_step)
            self._announce(voice_phrase)

        # Seuil « maintenant »
        elif dist_to_step <= t_now and "now" not in self._announced[step_idx]:
            self._announced[step_idx].add("now")
            voice_phrase = voice_instruction_for_distance(current_step.to_dict(), dist_to_step)
            self._announce(voice_phrase)

        # 5. Avancement à l'étape suivante après franchissement
        if dist_to_step <= 18.0 and self.current_step_index < len(self.route.steps) - 1:
            self.current_step_index += 1
        elif dist_to_step <= t_now and "now" not in self._announced[step_idx]:
            self._announced[step_idx].add("now")
            voice_phrase = voice_instruction_for_distance(current_step.to_dict(), dist_to_step)
            self._announce(voice_phrase)

        # 6. Calcul de la distance totale restante
        remaining_dist_m = dist_to_step
        for s in self.route.steps[self.current_step_index + 1:]:
            remaining_dist_m += s.distance_m

        remaining_duration_s = max(0.0, (remaining_dist_m / max(1.0, self.route.distance_m)) * self.route.duration_s)

        return {
            "active": True,
            "arrived": False,
            "step_index": self.current_step_index,
            "total_steps": len(self.route.steps),
            "current_step": current_step.to_dict(),
            "following_step": following_step.to_dict() if following_step else None,
            "distance_to_step_m": round(dist_to_step, 1),
            "remaining_distance_m": round(remaining_dist_m, 1),
            "remaining_duration_s": round(remaining_duration_s, 1),
            "off_route": is_off_route,
            "needs_reroute": needs_reroute,
            "voice_announcement": voice_phrase,
            "heading": heading,
            "speed": calc_speed,
        }

    def _announce(self, text: str) -> None:
        """Déclenche l'annonce vocale via le callback (zéro latence TTS)."""
        if not text or not self.voice_callback:
            return
        try:
            self.voice_callback(text)
        except Exception:
            pass


# ── Gestionnaire Global de Navigation (NavigationManager) ────────────────────

def submit_on_loop(loop: Optional[asyncio.AbstractEventLoop], coro) -> None:
    """Planifie ``coro`` sur ``loop``, y compris depuis un autre fil.

    ``get_event_loop()`` hors de la boucle crée une boucle fantôme (3.10+) ou
    lève ``RuntimeError`` (3.12+) : la diffusion dashboard n'arrivait jamais
    au téléphone. ``create_task`` n'est sûr que sur le fil de la boucle.
    """
    if loop is None or not loop.is_running():
        coro.close()
        return
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        loop.create_task(coro)
        return
    asyncio.run_coroutine_threadsafe(coro, loop)


class NavigationManager:
    """Gestionnaire singleton de navigation partagé entre UI, Dashboard et MCP."""

    _instance: Optional[NavigationManager] = None

    def __init__(self):
        self._session: Optional[NavigationSession] = None
        self._voice_speaker: Optional[Callable[[str], None]] = None
        self._ui_update_cb: Optional[Callable[[dict], None]] = None
        self._dashboard = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    @classmethod
    def get_instance(cls) -> NavigationManager:
        if cls._instance is None:
            cls._instance = NavigationManager()
        return cls._instance

    def set_voice_speaker(self, speaker: Callable[[str], None]) -> None:
        """Enregistre le moteur TTS direct (zéro latence)."""
        self._voice_speaker = speaker

    def set_ui_update_callback(self, cb: Callable[[dict], None]) -> None:
        """Enregistre le callback de mise à jour UI/carte."""
        self._ui_update_cb = cb

    def set_loop(self, loop: Optional[asyncio.AbstractEventLoop]) -> None:
        """Boucle asyncio du processus (posée depuis ``JarvisLive.run``)."""
        self._loop = loop

    def set_dashboard(self, dashboard) -> None:
        """Enregistre le serveur Dashboard pour la synchronisation mobile."""
        self._dashboard = dashboard
        loop = getattr(dashboard, "_loop", None)
        if loop is not None:
            self._loop = loop

    def _broadcast_dashboard(self, payload: dict[str, Any]) -> None:
        dashboard = self._dashboard
        if dashboard is None or not hasattr(dashboard, "broadcast"):
            return
        loop = self._loop or getattr(dashboard, "_loop", None)
        try:
            submit_on_loop(loop, dashboard.broadcast(payload))
        except Exception as exc:
            print(f"[Navigation] Diffusion dashboard ignorée : {exc}")

    @property
    def is_navigating(self) -> bool:
        return bool(self._session and self._session.is_active and not self._session.is_arrived)

    @property
    def current_session(self) -> Optional[NavigationSession]:
        return self._session

    def start_navigation(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        destination_name: str = "Destination",
        mode: str = "driving",
        precomputed_route: Optional[NavigationRoute] = None,
    ) -> Optional[NavigationRoute]:
        """Démarre une session de navigation guidée vers une destination."""
        route = precomputed_route or fetch_route(origin, destination, destination_name, mode=mode)
        if not route:
            return None

        def _speak(text: str):
            if self._voice_speaker:
                self._voice_speaker(text)

        def _reroute(new_origin: tuple[float, float]):
            self._on_reroute_needed(new_origin, mode=mode)

        self._session = NavigationSession(
            route=route,
            voice_callback=_speak,
            reroute_callback=_reroute,
        )

        # Notification vocale de démarrage
        if route.steps:
            first_step = route.steps[0]
            start_phrase = f"Guidage vers {destination_name}. {first_step.voice_text}"
            _speak(start_phrase)

        self._broadcast_dashboard({
            "type": "navigation_state",
            "active": True,
            "destination": destination_name,
        })

        return route

    def start_navigation_to_query(
        self,
        query: str,
        mode: str = "driving",
        player=None,
        origin_coords: Optional[tuple[float, float]] = None,
    ) -> tuple[str, Optional[NavigationRoute]]:
        """Lance automatiquement la navigation vers un lieu ou une adresse."""
        from core.geolocation import geocode, get_precise_user_coords, get_user_coords

        dest_coords = geocode(query)
        if not dest_coords:
            return f"Impossible de localiser « {query} » sur la carte.", None

        if origin_coords:
            origin = origin_coords
        else:
            orig = get_precise_user_coords() or get_user_coords()
            if not orig:
                return "Impossible de déterminer votre position de départ GPS.", None
            origin = orig

        route = self.start_navigation(
            origin=origin,
            destination=dest_coords,
            destination_name=query,
            mode=mode,
        )

        if not route:
            return f"Impossible de calculer l'itinéraire vers « {query} ».", None

        # Affichage de la carte et du tracé sur l'interface graphique
        if player:
            try:
                player.show_map(query, dest_coords[0], dest_coords[1], radius_km=max(2.0, (route.distance_m / 1000.0) * 1.3))
                if hasattr(player, "start_navigation"):
                    player.start_navigation(dest_coords[0], dest_coords[1], query)
            except Exception:
                pass

        dist_km = route.distance_m / 1000.0
        dur_min = int(round(route.duration_s / 60.0))
        msg = f"Itinéraire calculé vers {query} : {dist_km:.1f} km, environ {dur_min} min. Guidage en cours."
        return msg, route

    def update_position(
        self,
        lat: float,
        lon: float,
        accuracy_m: Optional[float] = None,
        heading: Optional[float] = None,
        speed: Optional[float] = None,
    ) -> Optional[dict[str, Any]]:
        """Achemine la nouvelle position GPS à la session active."""
        if not self.is_navigating or not self._session:
            return None

        state = self._session.update_position(
            lat=lat, lon=lon, accuracy_m=accuracy_m, heading=heading, speed=speed
        )

        if self._ui_update_cb:
            try:
                self._ui_update_cb(state)
            except Exception:
                pass

        return state

    def stop_navigation(self) -> str:
        """Arrête la navigation active."""
        if not self.is_navigating:
            return "Aucune navigation en cours."

        dest_name = self._session.route.destination_name if self._session else "destination"
        if self._session:
            self._session.is_active = False
            self._session = None

        if self._voice_speaker:
            try:
                self._voice_speaker("Navigation arrêtée.")
            except Exception:
                pass

        self._broadcast_dashboard({
            "type": "navigation_state",
            "active": False,
        })

        return f"Navigation vers {dest_name} arrêtée."

    def get_status_summary(self) -> str:
        """Retourne un état vocal et lisible de la navigation."""
        if not self.is_navigating or not self._session:
            return "Aucun guidage GPS n'est actuellement actif."

        session = self._session
        dest_name = session.route.destination_name
        step_idx = session.current_step_index
        total_steps = len(session.route.steps)

        if step_idx < total_steps:
            current_step = session.route.steps[step_idx]
            instr = current_step.instruction
        else:
            instr = "Arrivée imminente"

        rem_dist_km = session.route.distance_m / 1000.0
        rem_min = max(1, int(round(session.route.duration_s / 60.0)))

        return (
            f"Navigation active vers {dest_name} (étape {step_idx + 1}/{total_steps}). "
            f"Prochaine manœuvre : {instr}. "
            f"Distance restante : {rem_dist_km:.1f} km (environ {rem_min} minutes)."
        )

    def _on_reroute_needed(self, new_origin: tuple[float, float], mode: str = "driving") -> None:
        """Recalcul silencieux en cas de sortie prolongée de l'itinéraire."""
        if not self._session:
            return
        dest = self.route_destination()
        if not dest:
            return
        dest_coord, dest_name = dest
        new_route = fetch_route(new_origin, dest_coord, dest_name, mode=mode)
        if new_route:
            self._session.route = new_route
            self._session.current_step_index = 0
            self._session._announced.clear()
            if self._voice_speaker:
                self._voice_speaker("Nouvel itinéraire calculé.")

    def route_destination(self) -> Optional[tuple[tuple[float, float], str]]:
        if self._session:
            return (self._session.route.destination, self._session.route.destination_name)
        return None


def get_navigation_manager() -> NavigationManager:
    return NavigationManager.get_instance()
