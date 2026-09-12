"""
geolocation.py — Localisation approximative de l'utilisateur pour ANO-GPT.

Un poste de bureau n'a pas de GPS : la position vient soit d'un réglage
manuel (config/api_keys.json → "user_country"), soit de l'adresse IP
publique (précision ville, via un service de géolocalisation IP gratuit).
Résultat mis en cache (config/location_cache.json) pour ne pas refaire
l'appel réseau à chaque recherche.

Priorité : GPS du téléphone > réglage manuel > cache IP récent > nouvel appel
IP. Sans source fiable, la localisation reste inconnue : parler français ne
prouve jamais que l'utilisateur est en France.
"""
import json
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

try:
    import requests
    _REQUESTS = True
except ImportError:
    _REQUESTS = False

from config import get_config

_CACHE_PATH = Path(__file__).resolve().parent.parent / "config" / "location_cache.json"
_GEOCODE_CACHE_PATH = Path(__file__).resolve().parent.parent / "config" / "geocode_cache.json"
_CACHE_TTL = 6 * 3600  # la position IP change rarement : 6h suffit largement

_DEFAULT: Dict[str, Any] = {
    "country_code": "", "country_name": "", "hl": "fr",
    "city": "", "lat": None, "lon": None, "source": "default",
}


def _read_cache(ignore_ttl: bool = False) -> Optional[dict]:
    try:
        data = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        if ignore_ttl or time.time() - data.get("_cached_at", 0) < _CACHE_TTL:
            return data
    except Exception:
        pass
    return None


def _write_cache(data: dict) -> None:
    try:
        payload = dict(data)
        payload["_cached_at"] = time.time()
        _CACHE_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


# ── Position live (GPS du téléphone via le dashboard) ────────────────────────
#
# Le portable n'a ni GPS ni modem, et geoclue retomberait de toute façon sur
# l'IP depuis la fermeture de Mozilla Location Service (2024). La seule source
# réellement précise disponible est le GPS du téléphone, qui rejoint déjà le
# dashboard en HTTPS pour le micro — `navigator.geolocation` y fonctionne.

_LIVE_PATH = Path(__file__).resolve().parent.parent / "config" / "live_position.json"
# Au-delà, la position est considérée périmée : l'utilisateur a pu se déplacer
# entre deux localités. Mieux vaut retomber sur l'IP que d'affirmer une position
# précise mais fausse.
_LIVE_TTL = 20 * 60


def set_live_position(lat: float, lon: float, accuracy_m: float | None = None,
                      source: str = "phone-gps") -> dict:
    """Enregistre une position relevée par un appareil. Renvoie l'entrée écrite."""
    lat, lon = float(lat), float(lon)
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        raise ValueError(f"coordonnées hors bornes : {lat}, {lon}")

    entry = {
        "lat": lat,
        "lon": lon,
        "accuracy_m": float(accuracy_m) if accuracy_m is not None else None,
        "source": source,
        "_at": time.time(),
    }
    try:
        _LIVE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _LIVE_PATH.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return entry


def get_live_position(resolve_place: bool = True) -> Optional[dict]:
    """Dernière position relevée si elle est encore fraîche, sinon None.

    ``resolve_place=False`` évite tout appel réseau lorsque seules les
    coordonnées sont requises, notamment pour afficher la carte sans latence.
    """
    try:
        d = json.loads(_LIVE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None

    if time.time() - float(d.get("_at", 0)) > _LIVE_TTL:
        return None
    lat, lon = d.get("lat"), d.get("lon")
    if lat is None or lon is None:
        return None

    cfg = get_config()
    place = (reverse_geocode(lat, lon) or {}) if resolve_place else {}
    return {
        "country_code": (place.get("country_code")
                         or cfg.get("user_country", "") or "").lower(),
        "country_name": place.get("country_name") or cfg.get("user_country_name", ""),
        "hl": (cfg.get("user_lang", "fr") or "fr").strip().lower(),
        "city": place.get("city", ""),
        "lat": lat,
        "lon": lon,
        "accuracy_m": d.get("accuracy_m"),
        "source": d.get("source", "phone-gps"),
    }


def reverse_geocode(lat: float, lon: float) -> Optional[dict]:
    """Nom du lieu à ces coordonnées (Nominatim/OSM, sans clé), avec cache.

    Le cache est arrondi à ~100 m : inutile d'interroger le réseau à chaque
    rafraîchissement GPS alors que le quartier ne change pas, et Nominatim
    limite à 1 requête/seconde.
    """
    if not _REQUESTS:
        return None
    key = f"rev:{round(lat, 3)},{round(lon, 3)}"
    cache = _read_geocode_cache()
    if key in cache:
        return cache[key]

    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"lat": lat, "lon": lon, "format": "json", "zoom": 14},
            headers={"User-Agent": "ANO-GPT/1.0 (assistant personnel)"},
            timeout=5,
        )
        r.raise_for_status()
        addr = (r.json() or {}).get("address") or {}
    except Exception:
        return None

    # Du plus fin au plus large : on veut le quartier (T10, Kouria, Keitaya),
    # pas seulement l'agglomération.
    city = next(
        (addr[k] for k in ("neighbourhood", "suburb", "quarter", "village",
                           "town", "city_district", "city", "county")
         if addr.get(k)),
        "",
    )
    place = {
        "city": city,
        "country_code": (addr.get("country_code") or "").lower(),
        "country_name": addr.get("country") or "",
    }
    cache[key] = place
    _write_geocode_cache(cache)
    return place


def _detect_via_ip() -> Optional[dict]:
    if not _REQUESTS:
        return None
    try:
        r = requests.get("https://ipapi.co/json/", timeout=4)
        r.raise_for_status()
        d = r.json()
        if d.get("error"):
            return None
        cc = (d.get("country_code") or "").strip().lower()
        if not cc:
            return None
        return {
            "country_code": cc,
            "country_name": d.get("country_name") or cc.upper(),
            "hl": (d.get("languages") or "fr").split(",")[0].split("-")[0] or "fr",
            "city": d.get("city") or "",
            "lat": d.get("latitude"),
            "lon": d.get("longitude"),
            "source": "ip",
        }
    except Exception:
        return None


def get_user_location(force_refresh: bool = False) -> Dict[str, Any]:
    """Position de l'utilisateur, de la source la plus précise à la plus vague.

    Ordre : GPS du téléphone (~10 m) → IP (quartier/ville, souvent faux en
    mobile) → ville configurée → pays. La ville configurée ne peut pas primer :
    l'utilisateur se déplace entre plusieurs localités, et une ville figée
    renverrait des résultats à des dizaines de kilomètres sans le signaler.
    """
    live = get_live_position()
    if live:
        return live

    cfg = get_config()
    manual = (cfg.get("user_country") or "").strip().lower()
    if manual:
        return {
            "country_code": manual,
            "country_name": cfg.get("user_country_name", "") or manual.upper(),
            "hl": (cfg.get("user_lang", "fr") or "fr").strip().lower(),
            "city": (cfg.get("user_city", "") or "").strip(),
            "lat": None, "lon": None,
            "source": "config",
        }

    if not force_refresh:
        cached = _read_cache()
        if cached:
            return cached

    detected = _detect_via_ip()
    if detected:
        _write_cache(detected)
        return detected

    stale = _read_cache(ignore_ttl=True)
    if stale:
        return stale
    return dict(_DEFAULT)


def _read_geocode_cache() -> dict:
    try:
        return json.loads(_GEOCODE_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_geocode_cache(cache: dict) -> None:
    try:
        _GEOCODE_CACHE_PATH.write_text(
            json.dumps(cache, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass


def geocode(place: str) -> Optional[Tuple[float, float]]:
    """Coordonnées d'un lieu (ville, pays) via Open-Meteo (sans clé), avec
    un petit cache disque puisque ça ne change jamais pour un même lieu."""
    if not place:
        return None
    cache = _read_geocode_cache()
    if place in cache:
        lat, lon = cache[place]
        return lat, lon
    if not _REQUESTS:
        return None
    try:
        r = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": place, "count": 1, "language": "fr", "format": "json"},
            timeout=6,
        )
        r.raise_for_status()
        results = r.json().get("results") or []
        if not results:
            return None
        top = results[0]
        lat, lon = float(top["latitude"]), float(top["longitude"])
        cache[place] = [lat, lon]
        _write_geocode_cache(cache)
        return lat, lon
    except Exception:
        return None


def get_user_coords() -> Optional[Tuple[float, float]]:
    """Coordonnées approximatives de l'utilisateur (pour météo/carte).
    Vient de la géolocalisation IP si disponible, sinon d'un géocodage de
    la ville/pays configuré."""
    loc = get_user_location()
    lat, lon = loc.get("lat"), loc.get("lon")
    if lat is not None and lon is not None:
        return float(lat), float(lon)
    place = ", ".join(p for p in (loc.get("city"), loc.get("country_name")) if p)
    return geocode(place or loc.get("country_name") or "")


def get_precise_user_coords(max_accuracy_m: float = 500.0) -> Optional[Tuple[float, float]]:
    """Coordonnées *réelles* issues d'un relevé navigateur/GPS frais.

    Contrairement à :func:`get_user_coords`, cette fonction ne retombe jamais
    sur l'IP, une ville configurée ou un géocodage. Ces sources conviennent à
    une recherche régionale, mais pas à une demande explicite comme « affiche
    ma position ». Une mesure trop imprécise est également refusée.
    """
    live = get_live_position(resolve_place=False)
    if not live:
        return None
    accuracy = live.get("accuracy_m")
    if accuracy is None or float(accuracy) > float(max_accuracy_m):
        return None
    return float(live["lat"]), float(live["lon"])


if __name__ == "__main__":
    import pprint
    pprint.pprint(get_user_location(force_refresh=True))
    pprint.pprint(get_user_coords())
