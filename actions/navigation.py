"""actions/navigation.py — Action de Navigation GPS Parlée Pas-à-Pas (Mobile + PC).

Permet à l'assistant de piloter la navigation guidée :
- « Navigue vers l'aéroport / la pharmacie la plus proche »
- « Lance le GPS vers Paris en voiture / à pied / à vélo »
- « Arrête la navigation »
- « Où en est l'itinéraire ? » / « Prochaine manœuvre »
"""

from __future__ import annotations

from typing import Any, Dict, Optional
from core.navigation import get_navigation_manager

from core import action_kit as kit


# Le modèle transmet souvent le mode tel que dicté : on l'aligne sur les
# profils du moteur d'itinéraire au lieu de retomber silencieusement en voiture.
_MODE_ALIASES = {
    "driving": "driving", "drive": "driving", "car": "driving", "voiture": "driving",
    "en voiture": "driving", "auto": "driving", "taxi": "driving", "moto": "driving",
    "walking": "walking", "walk": "walking", "à pied": "walking", "a pied": "walking",
    "pied": "walking", "marche": "walking", "on foot": "walking",
    "cycling": "cycling", "bike": "cycling", "bicycle": "cycling", "vélo": "cycling",
    "velo": "cycling", "à vélo": "cycling", "a velo": "cycling",
}


@kit.action("navigation_action")
def navigation_action(
    parameters: Optional[Dict[str, Any]] = None,
    response=None,
    player=None,
    session_memory=None,
    speak=None,
) -> str:
    """Action principale de navigation GPS vocale et cartographique."""
    params = parameters or {}
    action = str(params.get("action") or "start").strip().lower()
    destination = str(params.get("destination") or params.get("query") or params.get("target") or "").strip()
    mode = _MODE_ALIASES.get(str(params.get("mode") or "driving").strip().lower(), "driving")

    nav_mgr = get_navigation_manager()

    if speak:
        nav_mgr.set_voice_speaker(speak)

    # 1. Arrêt de la navigation
    if action in ("stop", "cancel", "end", "close", "quitter", "arreter"):
        return nav_mgr.stop_navigation()

    # 2. Statut de la navigation en cours
    if action in ("status", "info", "state", "where", "prochaine"):
        return nav_mgr.get_status_summary()

    # 3. Démarrage de la navigation (action='start' ou par défaut)
    if not destination:
        # Si une navigation est déjà active, retourne le statut
        if nav_mgr.is_navigating:
            return nav_mgr.get_status_summary()
        return "Indiquez la destination vers laquelle vous souhaitez être guidé."

    msg, route = nav_mgr.start_navigation_to_query(
        query=destination,
        mode=mode,
        player=player,
    )
    return msg


# Alias
navigate = navigation_action
