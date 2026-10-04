"""Politique des outils : actions destructives, échecs, outils différés, annonce.

Tables et prédicats purs, lus par le répartiteur (``core/tool_dispatcher.py``).
"""
from __future__ import annotations

from typing import Any

# Outils dont une exécution ne se rattrape pas : c'est la liste que ferme la
# reconnaissance du locuteur. Le reste (chercher, ouvrir, afficher, la météo)
# reste accessible à qui passe dans la pièce — un invité peut demander l'heure.
_DESTRUCTIVE_TOOLS = frozenset({
    "close_app", "shutdown_jarvis", "shell_exec", "file_controller",
    "dev_agent", "code_helper", "game_updater", "send_message",
    "email_control", "calendar_control", "cloud_integrations_control", "contacts_control", "github_control", "computer_control", "hypr_control",
    "auto_extension_control",
    "phone_call", "phone_hangup", "phone_sms", "phone_contacts",
})

# Ces deux-là ne sont dangereux que sur certaines actions : refuser à un invité
# de baisser le volume n'aurait aucun sens.
_DESTRUCTIVE_SETTINGS = frozenset({
    "shutdown", "restart", "suspend", "lock_screen", "toggle_wifi",
    "toggle_bluetooth", "airplane_mode", "close_window",
})


def _is_destructive(name: str, args: dict) -> bool:
    if name == "computer_settings":
        return str(args.get("action", "")).strip().lower() in _DESTRUCTIVE_SETTINGS
    if name == "email_control":
        # Lire ses mails est déjà une intrusion ; les envoyer l'est plus encore.
        return True
    if name == "github_control":
        return True
    return name in _DESTRUCTIVE_TOOLS


_FAILURE_MARKERS = ("failed", "échec", "echec", "a échoué", "erreur", "error:",
                    "impossible", "introuvable", "clic annulé", "aucun clic effectué",
                    "je n'ai pas trouvé")


def _looks_like_failure(result: Any) -> bool:
    head = " ".join(str(result or "").split())[:160].casefold()
    return head.startswith(("tool '", "erreur", "échec", "echec")) or any(
        m in head[:60] for m in _FAILURE_MARKERS
    )


# Seules les identifications (photo + modèle de vision) sont longues ; lister
# les visages connus ou en oublier un reste une réponse immédiate.
_DEFERRED_VISION_ACTIONS = frozenset({"identify", "who", "what", "look", "regarde"})


def _vision_is_deferred(args: dict) -> bool:
    action = str(args.get("action") or "identify").strip().lower()
    return action in _DEFERRED_VISION_ACTIONS


# Le coach TikTok n'est long que lorsqu'il fait analyser des vidéos ;
# l'export, la liste et le meilleur horaire lisent des données locales.
_DEFERRED_TIKTOK_ACTIONS = frozenset({
    "diagnose", "why", "pourquoi", "analyse", "analyze", "video",
    "review", "account", "bilan", "compte", "plan",
    "draft", "before_post", "pre_post", "file", "fichier", "avant",
})


def _music_is_deferred(args: dict) -> bool:
    """Seule l'identification écoute la pièce ; rejouer ou lister est immédiat."""
    action = str(args.get("action") or "identify").strip().lower()
    return action not in {"play", "lance", "jouer", "play_last", "history",
                          "historique", "last", "dernière", "derniere"}


def _tiktok_is_deferred(args: dict) -> bool:
    action = str(args.get("action") or "diagnose").strip().lower()
    return action in _DEFERRED_TIKTOK_ACTIONS


# Outils qui font attendre : leur description commence par la consigne
# d'annoncer avant d'appeler. Une seule source pour tous, alignée sur la règle
# « annonce avant d'agir » du prompt.
_ANNOUNCE_BEFORE_TOOLS = frozenset({
    "deep_think", "consult_brain", "web_search", "smart_search", "image_search",
    "tiktok_coach", "visual_recognition", "music_recognition",
    "generate_image", "generate_video", "generate_document", "download_music",
    "youtube_video", "file_search", "file_processor", "background_tasks", "dev_agent",
    "simulate_decision", "flight_finder", "find_nearby", "navigate",
})
_ANNOUNCE_PREFIX = (
    "AVANT d'appeler cet outil, dis à voix haute une phrase courte qui nomme ce que tu lances "
    "et demande de patienter (ex. « je lance ça, patiente, je te reviens ») ; ne l'appelle "
    "jamais en silence. "
)


def announce_before_call(declarations: list[dict]) -> list[dict]:
    """Préfixe la description des outils longs par la consigne d'annonce."""
    out: list[dict] = []
    for decl in declarations:
        name = str(decl.get("name") or "")
        desc = str(decl.get("description") or "")
        if name in _ANNOUNCE_BEFORE_TOOLS and not desc.startswith(_ANNOUNCE_PREFIX):
            decl = {**decl, "description": _ANNOUNCE_PREFIX + desc}
        out.append(decl)
    return out
