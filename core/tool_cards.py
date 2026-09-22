"""Cartes « tâche en cours » : libellés, résumé des arguments, extrait du résultat.

Partagé par le répartiteur et les livraisons différées (``core/tool_deferred.py``).
"""
from __future__ import annotations

import re
from typing import Any


_FAILURE_MARKERS = ("failed", "échec", "echec", "a échoué", "erreur", "error:", "impossible", "introuvable")


def _looks_like_failure(result: Any) -> bool:
    head = " ".join(str(result or "").split())[:160].casefold()
    return head.startswith(("tool '", "erreur", "échec", "echec")) or any(
        m in head[:60] for m in _FAILURE_MARKERS
    )


def _task_result_excerpt(result: Any, limit: int = 220) -> str:
    """Première ligne utile du résultat, sans balisage ni consigne au modèle."""
    text = str(result or "").strip()
    if not text:
        return ""
    # Les blocs [VISION…] / [AGENT] et les consignes « dis-le à l'utilisateur »
    # s'adressent au modèle, pas à l'œil.
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    keep = [ln for ln in lines if not ln.startswith(("[", "{", "```"))] or lines
    excerpt = " ".join(keep[:3])
    excerpt = re.sub(r"[*_`#>]+", "", excerpt)
    excerpt = re.sub(r"\s+", " ", excerpt).strip()
    return excerpt[: limit - 1] + "…" if len(excerpt) > limit else excerpt


def _task_card_summary(name: str, args: Any) -> str:
    """Ce que fait la tâche, en une ligne, d'après ses arguments."""
    if not isinstance(args, dict):
        return ""
    for key in ("query", "question", "text", "app_name", "path", "file", "url", "title",
                "message", "prompt", "command", "recipient", "to", "action"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            val = " ".join(val.split())
            return val[:119] + "…" if len(val) > 120 else val
    return ""


# Libellés humains pour la carte « tâche en cours » (latence perçue > 1 s).
_TOOL_LABELS = {
    "consult_brain": "Réflexion",
    "web_search": "Recherche web",
    "image_search": "Recherche d'images",
    "weather_report": "Météo",
    "open_app": "Ouverture d'application",
    "close_app": "Fermeture d'application",
    "browser_control": "Navigateur",
    "file_controller": "Fichiers",
    "send_message": "Envoi de message",
    "shell_exec": "Commande",
    "visual_recognition": "Reconnaissance visuelle",
    "music_recognition": "Reconnaissance musicale",
    "music_control": "Musique",
    "download_music": "Téléchargement musique",
    "proactive_mode": "Mode proactif",
    "background_tasks": "Tâche de fond",
    "calendar_control": "Agenda",
    "cloud_integrations_control": "Intégration cloud",
    "prayer_control": "Prière",
    "tiktok_tracker": "TikTok",
    "tiktok_coach": "Coach TikTok",
    "github_control": "GitHub",
    "simulate_decision": "Simulation stratégique",
    "auto_extension_control": "Extensions autonomes",
    "contacts_control": "Contacts",
    "phone_call": "Appel téléphonique Android",
    "phone_hangup": "Raccrochage",
    "phone_contacts": "Contacts du téléphone",
    "phone_sms": "SMS depuis le téléphone",
    "sparring_partner": "Session d'entraînement",
    "focus_guard": "Bouclier anti-distraction",
    "youtube_video": "YouTube",
    "show_map": "Carte",
    "show_country_info": "Fiche pays",
    "reminder": "Rappel",
    "computer_control": "Contrôle de l'ordinateur",
    "game_updater": "Mise à jour de jeu",
    "flight_finder": "Recherche de vol",
    "capture_control": "Capture d'écran",
    "screen_process": "Analyse visuelle",
    "camera_control": "Caméra",
    "deep_think": "Réflexion approfondie",
    "voice_style": "Style vocal",
    "second_brain": "Second Brain",
    "devsecops": "DevSecOps & Système",
    "hypr_orchestrator": "Orchestrateur Hyprland",
    "point_on_screen": "Pointeur visuel",
    "capability_guide": "Compétences",
}
