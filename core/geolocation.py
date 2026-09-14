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


def get_live_position(resolve_place: bool = True, *, max_age_s: float | None = None) -> Optional[dict]:
    """Dernière position relevée si elle est encore fraîche, sinon None.

    ``resolve_place=False`` évite tout appel réseau lorsque seules les
    coordonnées sont requises, notamment pour afficher la carte sans latence.
    """
    try:
        d = json.loads(_LIVE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None

    max_age = _LIVE_TTL if max_age_s is None else max(0.0, float(max_age_s))
    if time.time() - float(d.get("_at", 0)) > max_age:
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


# Une réponse à plus de 25 km du point demandé n'est pas un quartier, c'est
# une réponse fausse : Nominatim recale sur la feature la plus proche de son
# index, et l'index OSM de zones rurales peut être clairsemé au point de
# renvoyer le bourg le plus proche à des dizaines de km. Mieux vaut ne rien
# dire qu'annoncer un lieu où l'utilisateur n'est pas.
_REVERSE_GEOCODE_MAX_DRIFT_KM = 25.0
# Un mauvais résultat mis en cache restait faux pour toujours (pas de TTL) :
# l'utilisateur signale son erreur, il n'a aucun moyen de la faire corriger
# avant l'expiration.
_GEOCODE_CACHE_TTL_S = 30 * 24 * 3600.0


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    from math import asin, cos, radians, sin, sqrt
    r = 6371.0
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * r * asin(sqrt(a))


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
    cached = cache.get(key)
    if cached and (time.time() - float(cached.get("_at", 0))) < _GEOCODE_CACHE_TTL_S:
        return cached.get("place")

    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"lat": lat, "lon": lon, "format": "json", "zoom": 14},
            headers={"User-Agent": "ANO-GPT/1.0 (assistant personnel)"},
            timeout=5,
        )
        r.raise_for_status()
        data = r.json() or {}
        addr = data.get("address") or {}
    except Exception:
        return None

    # Nominatim rend aussi les coordonnées de la feature qu'il a trouvée :
    # loin du point demandé, sa réponse ne décrit pas où l'utilisateur est.
    try:
        found_lat, found_lon = float(data.get("lat")), float(data.get("lon"))
        drift_km = _haversine_km(lat, lon, found_lat, found_lon)
    except (TypeError, ValueError):
        drift_km = None
    if drift_km is not None and drift_km > _REVERSE_GEOCODE_MAX_DRIFT_KM:
        print(f"[Géoloc] réponse Nominatim écartée : {drift_km:.0f} km du point demandé")
        place = {"city": "", "country_code": (addr.get("country_code") or "").lower(),
                 "country_name": addr.get("country") or ""}
        cache[key] = {"_at": time.time(), "place": place}
        _write_geocode_cache(cache)
        return place

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
    cache[key] = {"_at": time.time(), "place": place}
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


def _nominatim_search(place: str, country_code: str) -> Optional[Tuple[float, float]]:
    """Repli fin quand Open-Meteo ne connaît pas le lieu (quartier, commune)."""
    if not _REQUESTS:
        return None
    try:
        params = {"q": place, "format": "json", "limit": 1}
        if country_code:
            params["countrycodes"] = country_code.lower()
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params=params,
            headers={"User-Agent": "ANO-GPT/1.0 (assistant personnel)"},
            timeout=6,
        )
        r.raise_for_status()
        results = r.json() or []
        if not results:
            return None
        return float(results[0]["lat"]), float(results[0]["lon"])
    except Exception:
        return None


def geocode(place: str, *, country_code: str = "") -> Optional[Tuple[float, float]]:
    """Coordonnées d'un lieu (ville, pays) via Open-Meteo (sans clé), avec
    un petit cache disque puisque ça ne change jamais pour un même lieu.

    ``country_code`` (ISO-3166-1 alpha2, ex. "GN") favorise un pays quand le
    nom est ambigu : sans lui, « Kaloum » — un nom générique — a déjà
    répondu un village du Nigeria plutôt que le quartier de Conakry, parce
    qu'Open-Meteo rend le premier résultat mondial sans contexte. Repli sur
    la recherche mondiale si rien ne correspond dans le pays indiqué — pour
    ne pas bloquer une destination délibérément à l'étranger.
    """
    if not place:
        return None
    cc = country_code.strip().upper()
    cache = _read_geocode_cache()
    cache_key = f"{place}@{cc}" if cc else place
    if cache_key in cache:
        lat, lon = cache[cache_key]
        return lat, lon
    if not _REQUESTS:
        return None

    def _search(*, with_country: bool) -> Optional[list]:
        params = {"name": place, "count": 5 if with_country else 1,
                  "language": "fr", "format": "json"}
        if with_country and cc:
            params["countryCode"] = cc
        r = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params=params, timeout=6,
        )
        r.raise_for_status()
        return r.json().get("results") or []

    try:
        results = _search(with_country=bool(cc))
        if results:
            top = results[0]
            lat, lon = float(top["latitude"]), float(top["longitude"])
            cache[cache_key] = [lat, lon]
            _write_geocode_cache(cache)
            return lat, lon
    except Exception:
        pass

    # Open-Meteo indexe des villes, pas des quartiers : « Kaloum » (un
    # quartier de Conakry) y est absent avec un biais pays et répond un
    # village du Niger sans lui. Nominatim (OpenStreetMap), déjà utilisé
    # pour le géocodage inverse, connaît les échelles plus fines.
    nominatim_result = _nominatim_search(place, cc)
    if nominatim_result:
        cache[cache_key] = list(nominatim_result)
        _write_geocode_cache(cache)
        return nominatim_result

    if cc:
        # Dernier recours, sans biais : peut renvoyer un résultat homonyme
        # dans un autre pays, mais vaut mieux qu'un échec sec.
        try:
            results = _search(with_country=False)
            if results:
                top = results[0]
                lat, lon = float(top["latitude"]), float(top["longitude"])
                cache[cache_key] = [lat, lon]
                _write_geocode_cache(cache)
                return lat, lon
        except Exception:
            pass
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


def get_precise_user_coords(
    max_accuracy_m: float = 500.0, *, max_age_s: float | None = None,
) -> Optional[Tuple[float, float]]:
    """Coordonnées *réelles* issues d'un relevé navigateur/GPS frais.

    Contrairement à :func:`get_user_coords`, cette fonction ne retombe jamais
    sur l'IP, une ville configurée ou un géocodage. Ces sources conviennent à
    une recherche régionale, mais pas à une demande explicite comme « affiche
    ma position ». Une mesure trop imprécise est également refusée.
    """
    live = get_live_position(resolve_place=False, max_age_s=max_age_s)
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
