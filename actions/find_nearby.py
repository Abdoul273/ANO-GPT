#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""actions/find_nearby.py — Recherche de lieux à proximité.

La recherche elle-même vit dans `core/places.py` (SerpAPI Google Maps, puis
OpenStreetMap). Ce module fait le lien avec l'assistant : il détermine d'où
chercher, épingle les résultats dans la grande carte et rend un texte lisible.
"""

from __future__ import annotations

from typing import Any

from core.geolocation import (
    geocode,
    get_precise_user_coords,
    get_user_coords,
    get_user_location,
)
from core.places import describe_places, has_serpapi, search_places

from core import action_kit as kit


def get_location(*, require_precise_gps: bool = False, max_location_age_s: float | None = None) -> dict[str, Any]:
    """Position réelle de l'utilisateur, au format attendu ici.

    Le relevé GPS frais passe avant la position IP : une pharmacie annoncée à
    cent cinquante mètres n'a de sens que mesurée depuis l'endroit où l'on se
    tient réellement. La position approximative reste le repli, pour que la
    recherche fonctionne encore sans téléphone appairé.

    Aucun repli codé en dur au-delà : une position inventée renvoie des
    commerces à des milliers de kilomètres, ce qui est pire qu'une erreur
    franche.
    """
    info = get_user_location()
    coords = None
    try:
        coords = (get_precise_user_coords(max_age_s=max_location_age_s)
                  if max_location_age_s is not None else get_precise_user_coords())
    except Exception:
        coords = None
    if require_precise_gps and not coords:
        raise RuntimeError(
            "aucun relevé GPS précis reçu d'ANO Remote. Connecte l'application "
            "Android, ouvre le contrôle à distance et autorise la localisation"
        )
    if not coords:
        coords = get_user_coords()
    if not coords:
        raise RuntimeError(
            "Position introuvable : renseigne 'user_city' et 'user_country' "
            "dans config/api_keys.json, ou vérifie la connexion réseau."
        )
    lat, lon = coords
    city = (info.get("city") or "").strip() or info.get("country_name", "") or "votre position"
    return {"latitude": lat, "longitude": lon, "city": city}


@kit.action("find_nearby")
def find_nearby(parameters: dict | None = None, session_memory=None, ui=None) -> str:
    """Outil `find_nearby` : cherche des lieux et les montre sur la carte."""
    params = parameters or {}
    query = str(params.get("query") or "").strip()
    category = str(params.get("category") or "").strip()
    near = str(params.get("near") or "").strip()
    try:
        radius_km = float(params.get("radius_km") or 5.0)
    except (TypeError, ValueError):
        radius_km = 5.0
    radius_km = max(0.1, min(radius_km, 100.0))

    terms = query or category
    if not terms:
        return "Précisez ce que vous cherchez (pharmacie, restaurant, hôtel…)."

    # « près de la gare » : le point de départ est le lieu cité, pas l'utilisateur.
    if near:
        coords = geocode(near)
        if not coords:
            return f"Impossible de situer « {near} »."
        center_lat, center_lon = coords
        city = near
    else:
        try:
            if params.get("_require_precise_gps"):
                location = get_location(
                    require_precise_gps=True,
                    max_location_age_s=params.get("_max_location_age_s"),
                )
            else:
                location = get_location()
        except Exception as exc:
            return f"Impossible de déterminer votre position : {exc}"
        center_lat = float(location["latitude"])
        center_lon = float(location["longitude"])
        city = location["city"]

    try:
        places, sources = search_places(
            terms, (center_lat, center_lon), radius_km=radius_km
        )
    except Exception as exc:
        return f"La recherche de lieux a échoué : {exc}"

    widened = 0.0
    if not places and radius_km < 30.0:
        # Rien dans le rayon demandé : on élargit une fois avant de renoncer,
        # en le disant — « la pharmacie la plus proche est à 12 km » vaut
        # mieux qu'un « aucun résultat » sec.
        widened = min(30.0, max(radius_km * 3.0, 10.0))
        try:
            places, sources = search_places(
                terms, (center_lat, center_lon), radius_km=widened
            )
        except Exception:
            places, sources = [], sources

    if not places:
        advice = ""
        if not has_serpapi():
            # Sans clé, on n'a qu'OpenStreetMap, très clairsemé hors d'Europe.
            advice = (
                " Ajoutez une clé 'serpapi_api_key' dans config/api_keys.json "
                "pour couvrir les lieux absents d'OpenStreetMap."
            )
        return (f"Aucun résultat pour « {terms} » dans un rayon de "
                f"{max(radius_km, widened):g} km autour de {city}.{advice}")

    if ui is not None and hasattr(ui, "show_nearby_map"):
        try:
            ui.show_nearby_map(terms, center_lat, center_lon, places)
        except Exception as exc:
            print(f"[FindNearby] Carte indisponible : {exc}")

    text = describe_places(places, terms, city, sources)
    if widened:
        text = (f"Rien à moins de {radius_km:g} km ; j'ai élargi à {widened:g} km. "
                + text)
    return text


if __name__ == "__main__":
    print(find_nearby({"query": "pharmacie"}))
