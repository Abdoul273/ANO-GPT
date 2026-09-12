"""actions/navigation.py — Action de Navigation GPS Parlée Pas-à-Pas (Mobile + PC).

Permet à l'assistant de piloter la navigation guidée :
- « Navigue vers l'aéroport / la pharmacie la plus proche »
- « Lance le GPS vers Paris en voiture / à pied / à vélo »
- « Arrête la navigation »
- « Où en est l'itinéraire ? » / « Prochaine manœuvre »
"""

from __future__ import annotations

from typing import Any, Dict, Optional
from core.navigation import get_navigation_manager, NavigationRoute

from core import action_kit as kit


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
    mode = str(params.get("mode") or "driving").strip().lower()

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
