"""core/places.py — Recherche de lieux pour ANO-GPT.

Trois sources, par ordre de richesse :

1. **SerpAPI, moteur Google Maps** — même appel GPS que `web_search` : note,
   nombre d'avis, adresse postale, téléphone, horaires, type d'établissement. C'est la seule source qui
   couvre correctement les villes où OpenStreetMap est clairsemé, Conakry
   la première.
2. **Overpass (OpenStreetMap)** — gratuit, sans clé, utilisé en repli.
3. **Nominatim** — pour situer une zone citée en toutes lettres
   (« près de la gare »), pas pour lister des commerces.

Toutes rendent le même dictionnaire normalisé, afin que la carte et le texte
n'aient qu'un seul format à connaître.
"""

from __future__ import annotations

import json
import math
import time
import urllib.request
from typing import Any

from core.service_resilience import read_with_retry

_USER_AGENT = "ANO-GPT/2.0 (JARVIS Assistant)"

_OVERPASS_SERVERS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)

# Résultats mis en cache dix minutes : refaire la même requête pendant qu'on
# discute d'une liste déjà affichée ne changerait rien et coûte un crédit.
_CACHE: dict[tuple, tuple[float, list[dict]]] = {}
_CACHE_TTL = 600


class PlaceSearchError(RuntimeError):
    """Aucune source n'a pu répondre."""


# ── configuration ─────────────────────────────────────────────────────────

def serpapi_key() -> str:
    # Même configuration que web_search, y compris SERPAPI_API_KEY.
    from actions.web_search import _get_serpapi_api_key
    return _get_serpapi_api_key() or ""


def has_serpapi() -> bool:
    return bool(serpapi_key())


# ── géométrie ─────────────────────────────────────────────────────────────

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def directions_url(from_lat: float, from_lon: float, lat: float, lon: float) -> str:
    return (
        "https://www.google.com/maps/dir/?api=1"
        f"&origin={from_lat},{from_lon}&destination={lat},{lon}"
    )


def _place(
    *,
    name: str,
    lat: float,
    lon: float,
    center: tuple[float, float],
    source: str,
    address: str = "",
    category: str = "",
    rating: float | None = None,
    reviews: int | None = None,
    phone: str = "",
    hours: str = "",
    website: str = "",
    price: str = "",
    open_now: bool | None = None,
) -> dict[str, Any]:
    """Fabrique le dictionnaire normalisé d'un lieu."""
    distance = haversine_km(center[0], center[1], lat, lon)
    return {
        "name": name.strip(),
        "lat": float(lat),
        "lon": float(lon),
        "dist_km": round(distance, 2),
        "address": address.strip(),
        "category": category.strip(),
        "rating": rating,
        "reviews": reviews,
        "phone": phone.strip(),
        "opening_hours": hours.strip(),
        "website": website.strip(),
        "price": price.strip(),
        "open_now": open_now,
        "source": source,
        "directions_url": directions_url(center[0], center[1], lat, lon),
    }


# ── SerpAPI (Google Maps) ─────────────────────────────────────────────────

def search_serpapi(
    query: str,
    center: tuple[float, float],
    *,
    zoom: int = 14,
    limit: int = 20,
    timeout: float = 3.0,
    language: str = "fr",
) -> list[dict[str, Any]]:
    """Interroge le moteur Google Maps de SerpAPI autour d'un point.

    Le paramètre `ll` porte la position ET le zoom : sans le zoom, Google
    élargit à la région entière et renvoie des adresses à des kilomètres.
    """
    if not has_serpapi():
        return []

    from actions.web_search import _nearby_map_results
    payload = _nearby_map_results(
        query, center, zoom=zoom, language=language, timeout=timeout,
    )

    if payload.get("error"):
        raise PlaceSearchError(str(payload["error"]))

    raw = payload.get("local_results") or []
    if isinstance(raw, dict):  # une réponse unique n'est pas encapsulée en liste
        raw = raw.get("places") or []
    # Une recherche très précise renvoie parfois une fiche unique.
    if not raw and payload.get("place_results"):
        raw = [payload["place_results"]]

    results: list[dict[str, Any]] = []
    for entry in raw[:limit]:
        gps = entry.get("gps_coordinates") or {}
        lat, lon = gps.get("latitude"), gps.get("longitude")
        title = entry.get("title") or ""
        if lat is None or lon is None or not title:
            continue
        hours = entry.get("hours")
        if isinstance(hours, list):
            hours = ", ".join(str(item) for item in hours[:2])
        results.append(
            _place(
                name=title,
                lat=float(lat),
                lon=float(lon),
                center=center,
                source="serpapi",
                address=str(entry.get("address") or ""),
                category=str(entry.get("type") or ""),
                rating=_as_float(entry.get("rating")),
                reviews=_as_int(entry.get("reviews")),
                phone=str(entry.get("phone") or ""),
                hours=str(hours or ""),
                website=str(entry.get("website") or ""),
                price=str(entry.get("price") or ""),
                open_now=_open_state(entry),
            )
        )
    return results


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _open_state(entry: dict) -> bool | None:
    """« Ouvert » / « Fermé » tel que Google le rend, quand il le rend."""
    state = entry.get("open_state") or entry.get("operating_hours")
    if isinstance(state, dict):
        state = json.dumps(state)
    if not state:
        return None
    text = str(state).lower()
    if "ferm" in text or "closed" in text:
        return False
    if "ouvert" in text or "open" in text:
        return True
    return None


# ── Overpass (OpenStreetMap) ──────────────────────────────────────────────

# Un mot tapé par l'utilisateur vers les étiquettes OSM correspondantes. La
# version précédente n'acceptait que huit catégories anglaises figées, ce qui
# rendait muette la moitié des demandes réelles.
_KEYWORD_TAGS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("pharmacie", "pharmacy", "medicament"), ('["amenity"="pharmacy"]',)),
    (("hopital", "hôpital", "hospital", "clinique", "urgence"),
     ('["amenity"="hospital"]', '["amenity"="clinic"]')),
    (("medecin", "médecin", "docteur", "doctor"), ('["amenity"="doctors"]',)),
    (("restaurant", "manger", "resto", "food", "dîner", "diner"),
     ('["amenity"="restaurant"]', '["amenity"="fast_food"]')),
    (("cafe", "café", "coffee"), ('["amenity"="cafe"]',)),
    (("bar", "pub", "boire"), ('["amenity"="bar"]', '["amenity"="pub"]')),
    (("hotel", "hôtel", "hebergement", "hébergement", "dormir", "logement"),
     ('["tourism"="hotel"]', '["tourism"="guest_house"]')),
    (("banque", "bank", "atm", "distributeur", "retrait"),
     ('["amenity"="bank"]', '["amenity"="atm"]')),
    (("essence", "carburant", "station", "fuel", "gas"), ('["amenity"="fuel"]',)),
    (("supermarche", "supermarché", "supermarket", "course", "épicerie", "epicerie"),
     ('["shop"="supermarket"]', '["shop"="convenience"]')),
    (("boulangerie", "bakery", "pain"), ('["shop"="bakery"]',)),
    (("marche", "marché", "market"), ('["amenity"="marketplace"]',)),
    (("electronique", "électronique", "electronics", "informatique",
      "ordinateur", "telephone", "téléphone", "computer"),
     ('["shop"="electronics"]', '["shop"="computer"]', '["shop"="mobile_phone"]')),
    (("vetement", "vêtement", "clothes", "clothing", "habit"), ('["shop"="clothes"]',)),
    (("librairie", "livre", "books", "bookshop"), ('["shop"="books"]',)),
    (("ecole", "école", "school", "universite", "université", "university"),
     ('["amenity"="school"]', '["amenity"="university"]')),
    (("police", "commissariat", "gendarmerie"), ('["amenity"="police"]',)),
    (("poste", "post office"), ('["amenity"="post_office"]',)),
    (("mosquee", "mosquée", "eglise", "église", "church", "mosque", "priere", "prière"),
     ('["amenity"="place_of_worship"]',)),
    (("bricolage", "quincaillerie", "hardware"),
     ('["shop"="doityourself"]', '["shop"="hardware"]')),
    (("coiffeur", "salon", "barber"), ('["shop"="hairdresser"]',)),
    (("garage", "mecanicien", "mécanicien", "reparation auto"),
     ('["shop"="car_repair"]',)),
    (("taxi", "gare", "bus", "transport"),
     ('["amenity"="taxi"]', '["amenity"="bus_station"]')),
)


def osm_tags_for(query: str) -> tuple[str, ...]:
    """Étiquettes OSM devinées à partir du texte de l'utilisateur."""
    text = (query or "").lower()
    matched: list[str] = []
    for keywords, tags in _KEYWORD_TAGS:
        if any(keyword in text for keyword in keywords):
            matched.extend(tags)
    if matched:
        # Garder l'ordre tout en supprimant les doublons entre familles.
        return tuple(dict.fromkeys(matched))
    # Rien de reconnu : ratisser les commerces et services nommés.
    return ('["shop"]', '["amenity"]')


def search_overpass(
    query: str,
    center: tuple[float, float],
    *,
    radius_km: float = 5.0,
    limit: int = 25,
    timeout: float = 4.0,
) -> list[dict[str, Any]]:
    tags = osm_tags_for(query)
    radius_m = int(radius_km * 1000)
    clauses = "".join(
        f'node{tag}(around:{radius_m},{center[0]},{center[1]});'
        f'way{tag}(around:{radius_m},{center[0]},{center[1]});'
        for tag in tags
    )
    body = f"[out:json][timeout:{int(timeout)}];({clauses});out center {limit * 3};"

    elements: list[dict] = []
    for attempt, server in enumerate(_OVERPASS_SERVERS):
        if attempt:
            time.sleep(0.25 * (2 ** (attempt - 1)))
        try:
            request = urllib.request.Request(
                server, data=body.encode("utf-8"),
                headers={"User-Agent": _USER_AGENT},
            )
            def fetch(request=request) -> list[dict]:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    return json.loads(response.read().decode("utf-8")).get("elements", [])

            elements = read_with_retry(
                f"overpass:{server}", fetch, transient=lambda _exc: True,
                attempts=1,
            )
            # Une réponse vide est valide ; le miroir suivant n'apporterait
            # que le même rayon avec une attente supplémentaire.
            break
        except Exception:
            continue

    needle = (query or "").lower().strip()
    results: list[dict[str, Any]] = []
    for element in elements:
        tags_map = element.get("tags", {})
        name = tags_map.get("name") or tags_map.get("brand")
        if not name:
            continue
        lat = element.get("lat") or (element.get("center") or {}).get("lat")
        lon = element.get("lon") or (element.get("center") or {}).get("lon")
        if lat is None or lon is None:
            continue

        street = tags_map.get("addr:street", "")
        number = tags_map.get("addr:housenumber", "")
        address = f"{number} {street}".strip() or tags_map.get("addr:full", "")

        results.append(
            _place(
                name=name,
                lat=float(lat),
                lon=float(lon),
                center=center,
                source="osm",
                address=address,
                category=tags_map.get("shop") or tags_map.get("amenity") or "",
                phone=tags_map.get("phone") or tags_map.get("contact:phone") or "",
                hours=tags_map.get("opening_hours", ""),
                website=tags_map.get("website") or tags_map.get("contact:website") or "",
            )
        )

    # Quand la requête libre ne correspond à aucune étiquette connue, on a
    # ratissé large : remonter d'abord ce qui porte le mot cherché.
    if needle and tags == ('["shop"]', '["amenity"]'):
        preferred = [item for item in results if needle in item["name"].lower()]
        if preferred:
            results = preferred
    return results


# ── recherche unifiée ─────────────────────────────────────────────────────

def _dedupe(places: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fusionne les doublons entre sources.

    Deux fiches du même commerce n'ont jamais exactement les mêmes
    coordonnées : on regroupe par nom et par proximité (~50 m), en gardant
    la fiche la plus riche.
    """
    kept: list[dict[str, Any]] = []
    for place in places:
        merged = False
        for existing in kept:
            same_name = existing["name"].lower() == place["name"].lower()
            close = haversine_km(
                existing["lat"], existing["lon"], place["lat"], place["lon"]
            ) < 0.05
            if same_name and close:
                if _richness(place) > _richness(existing):
                    existing.update(place)
                merged = True
                break
        if not merged:
            kept.append(place)
    return kept


def _richness(place: dict[str, Any]) -> int:
    """Nombre de champs utiles renseignés — sert à départager deux doublons."""
    return sum(
        1
        for field in ("address", "phone", "opening_hours", "website", "category")
        if place.get(field)
    ) + (1 if place.get("rating") is not None else 0)


def search_places(
    query: str,
    center: tuple[float, float],
    *,
    radius_km: float = 5.0,
    limit: int = 12,
    use_cache: bool = True,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Cherche des lieux autour d'un point.

    Renvoie les lieux triés par distance et la liste des sources ayant
    réellement répondu, pour que l'appelant puisse le dire à l'utilisateur.
    """
    radius_km = max(0.1, min(float(radius_km), 100.0))
    key = (query.lower().strip(), round(center[0], 3), round(center[1], 3),
           round(radius_km, 1))
    now = time.time()
    if use_cache and key in _CACHE:
        stamp, cached = _CACHE[key]
        if now - stamp < _CACHE_TTL:
            return cached[:limit], ["cache"]

    collected: list[dict[str, Any]] = []
    sources: list[str] = []

    if has_serpapi():
        try:
            found = search_serpapi(query, center, zoom=_zoom_for(radius_km))
            # `ll` et `nearby` restent des biais côté Google, pas une
            # frontière. La distance Haversine calculée localement est notre
            # autorité : aucun autre pays, aucune autre ville hors rayon.
            found = [p for p in found if p["dist_km"] <= radius_km]
            if found:
                collected.extend(found)
                sources.append("serpapi")
        except Exception as exc:
            print(f"[Places] SerpAPI indisponible : {exc}")

    # Un résultat cartographiable de Google suffit à répondre immédiatement.
    # Overpass peut prendre plusieurs secondes par miroir : on le réserve aux
    # recherches sans résultat Google ou sans clé SerpApi.
    if not collected:
        try:
            found = search_overpass(query, center, radius_km=radius_km)
            found = [p for p in found if p["dist_km"] <= radius_km]
            if found:
                collected.extend(found)
                sources.append("osm")
        except Exception as exc:
            print(f"[Places] Overpass indisponible : {exc}")

    if not collected:
        return [], sources

    merged = _dedupe(collected)
    final = sorted(merged, key=lambda item: item["dist_km"])

    _CACHE[key] = (now, final)
    return final[:limit], sources


def _zoom_for(radius_km: float) -> int:
    """Zoom Google correspondant grossièrement à un rayon en kilomètres."""
    if radius_km <= 1:
        return 16
    if radius_km <= 3:
        return 15
    if radius_km <= 8:
        return 14
    if radius_km <= 20:
        return 12
    return 11


def describe_places(places: list[dict[str, Any]], query: str, city: str,
                    sources: list[str]) -> str:
    """Résumé lisible, destiné au modèle et au panneau de texte."""
    if not places:
        return f"Aucun résultat pour « {query} » autour de {city}."

    origin = " + ".join(sources) if sources else "recherche locale"
    nearest = places[0]
    lines = [
        f"RECHERCHE LOCALE GPS VÉRIFIÉE : {len(places)} résultat(s) pour "
        f"« {query} » près de {city} ({origin}).",
        f"LE PLUS PROCHE : {nearest['name']} — {nearest['dist_km']} km.",
        "Dans la réponse vocale, cite au minimum le nom du lieu le plus proche, "
        "sa distance et le nombre de résultats; ne réponds jamais seulement "
        "« à proximité de votre position ».",
    ]
    for index, place in enumerate(places, 1):
        bits = [f"{index}. {place['name']} — {place['dist_km']} km"]
        if place.get("rating") is not None:
            note = f"{place['rating']}/5"
            if place.get("reviews"):
                note += f" ({place['reviews']} avis)"
            bits.append(note)
        if place.get("category"):
            bits.append(place["category"])
        lines.append(" · ".join(bits))
        details = []
        if place.get("address"):
            details.append(place["address"])
        if place.get("phone"):
            details.append(place["phone"])
        if place.get("opening_hours"):
            details.append(place["opening_hours"])
        if details:
            lines.append("   " + " — ".join(details))
    return "\n".join(lines)
