"""Répartiteur d'outils ANO-GPT : déclarations, lease, circuit breaker.

Concurrence
-----------
* **asyncio** — ``_execute_tool`` / ``_execute_tool_batch`` (gather pour les
  lectures indépendantes) tournent dans la boucle Live.
* **asyncio.to_thread / run_in_executor** — chaque action bloquante quitte
  la boucle audio (GIL partagé avec Qt).
* **ActionRuntime.lease** — timeout, concurrence max, circuit breaker.
* Jamais d'appel d'outil depuis le callback PortAudio.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
import re
import threading
import time
import traceback
from typing import Any, Protocol, Union

BASE_DIR = Path(__file__).resolve().parent.parent

from core.action_runtime import (
    ActionRuntime,
    ActionRuntimeError,
    friendly_runtime_error,
)
from core.audio_engine import SEND_SAMPLE_RATE, _MainAttr
from core.event_bus import ToolExecutionFinishedEvent, ToolExecutionRequestedEvent
from core.live_model_policy import DEFAULT_PRIMARY_MODEL as LIVE_MODEL
from core.live_speech_config import FRENCH_TECH_PHRASES
from core.plugin_registry import PluginRegistry
from core import memory_store, routines, screen_reader, speaker_id, tool_stats, undo_stack
from memory.memory_manager import update_memory

from actions.file_processor import file_processor
from actions.flight_finder import flight_finder
from actions.open_app import open_app
from actions.close_app import close_app
from actions.weather_report import weather_action
from actions.send_message import send_message
from actions.reminder import reminder
from actions.computer_settings import computer_settings
from actions.screen_processor import _capture_camera, _capture_screen
from actions.youtube_video import youtube_video
from actions.desktop import desktop_control
from actions.browser_control import browser_control
from actions.file_controller import file_controller
from actions.code_helper import code_helper
from actions.dev_agent import dev_agent
from actions.web_search import web_search as web_search_action
from actions.image_search import image_search as image_search_action
from actions.image_generation import generate_image as generate_image_action
from actions.video_generation import generate_video as generate_video_action
from actions.document_generation import generate_document as generate_document_action
from actions.computer_control import computer_control
from actions.game_updater import game_updater
from actions.media_control import media_control
from actions.shell_exec import shell_exec, hypr_control
from actions.devsecops import devsecops_control
from actions.hypr_orchestrator import hypr_orchestrator_control
from actions.auto_debug import auto_debug_action
from actions.navigation import navigation_action
from core.multimodal_vision import inspect_screen_live
from core.auto_debug import auto_debug_live
from actions.capture import capture_control
from actions.music import music_control
from actions.download_music import download_music
from actions.find_nearby import find_nearby
from actions.email import email_control
from actions.calendar import calendar_control
from actions.cloud_integrations import cloud_integrations_control
from actions.prayer import prayer_control
from actions.tiktok_tracker import tiktok_tracker
from actions.tiktok_coach import tiktok_coach
from actions.github import github_control
from actions.contacts import contacts_control
from actions.sparring_partner import sparring_partner, observe_sparring_utterance
from actions.background_tasks import BackgroundTaskService, format_tasks, format_agent_result
from actions.system_monitor import get_system_status

types = _MainAttr("types")


# Une commande brute de volume agit sur la sortie PipeWire/ALSA entière. Elle
# ne doit jamais pouvoir être utilisée à la place du contrôle du morceau.
_GLOBAL_VOLUME_SHELL_RE = re.compile(
    r"\b(?:amixer\s+(?:-D\s+\S+\s+)?(?:s?set\s+)?master|"
    r"pactl\s+set-sink-volume|wpctl\s+set-volume\s+@DEFAULT_AUDIO_SINK@)",
    re.IGNORECASE,
)

# L'enregistrement d'écran peut ouvrir le portail Wayland et capter des
# contenus privés. Une inférence isolée du modèle (« fais… » mal transcrit)
# ne suffit donc jamais : l'énoncé reconnu doit nommer cette intention.
_CAPTURE_INTENT_RE = re.compile(
    r"\b(?:capture(?:r)?|capture d.?ecran|capture ecran|"
    r"capture d.?écran|enregistre(?:r|ment)?|enregistrement|"
    r"filme(?:r)?|video d.?ecran|vidéo d.?écran|screenshot|screen record)\b",
    re.IGNORECASE,
)


def _has_explicit_capture_intent(transcript: object) -> bool:
    """True seulement si l'utilisateur a clairement demandé une capture."""
    return bool(_CAPTURE_INTENT_RE.search(str(transcript or "")))


def _is_global_volume_shell_command(args: dict) -> bool:
    raw = " ".join(str(args.get(key) or "") for key in ("command", "description"))
    return bool(_GLOBAL_VOLUME_SHELL_RE.search(raw))


def _explicit_system_volume_request(text: str) -> bool:
    """Distingue « la musique » de « le système » à partir de la phrase dite."""
    normalized = str(text or "").casefold()
    return bool(
        "volume" in normalized
        and re.search(r"\b(?:syst[eè]me|global|ordinateur|pc|toutes? les applications)\b", normalized)
    )


def _media_volume_request(text: str) -> bool:
    """Vrai uniquement quand l'utilisateur nomme le média à régler."""
    normalized = str(text or "").casefold()
    return bool(re.search(
        r"\b(?:musique|morceau|chanson|spotify|audio|vid[eé]o|youtube|film|clip|lecteur)\b",
        normalized,
    ))


def _system_volume_args_from_shell(args: dict) -> dict:
    """Convertit une commande brute déjà proposée en action sûre et dédiée."""
    raw = " ".join(str(args.get(key) or "") for key in ("command", "description"))
    value = re.search(r"(?<![\w.])(\d{1,3})\s*%", raw)
    amount = max(0, min(100, int(value.group(1)))) if value else 10
    if re.search(r"(?:\+\s*\d+\s*%|\d+%\+|volume_up|augment)", raw, re.IGNORECASE):
        return {"action": "volume_up", "value": str(max(1, amount))}
    if re.search(r"(?:-\s*\d+\s*%|\d+%-|volume_down|diminu)", raw, re.IGNORECASE):
        return {"action": "volume_down", "value": str(max(1, amount))}
    return {"action": "volume_set", "value": str(amount)}


def _relative_volume_value_from_shell(args: dict) -> str:
    """Préserve le signe (+/-) d'une commande modèle pour un lecteur média."""
    raw = " ".join(str(args.get(key) or "") for key in ("command", "description"))
    match = re.search(r"([+-]?)\s*(\d{1,3})\s*%", raw)
    if match and not match.group(1):
        # ALSA formule les deltas comme « 10%- » plutôt que « -10% ».
        suffix = re.search(r"\d{1,3}\s*%\s*([+-])", raw)
        if suffix:
            return f"{suffix.group(1)}{max(0, min(100, int(match.group(2))))}"
    if not match:
        return "-10" if re.search(r"diminu|baisse|moins", raw, re.IGNORECASE) else "+10"
    sign, amount = match.groups()
    if not sign:
        sign = "-" if re.search(r"diminu|baisse|moins", raw, re.IGNORECASE) else "+"
    return f"{sign}{max(0, min(100, int(amount)))}"


def _get_api_key() -> str:
    from core.session_manager import _get_api_key as _key
    return _key()


def _voice_engine_settings() -> dict:
    from core.session_manager import _voice_engine_settings as _settings
    return _settings()

# Seul outil exposé à Gemini Live quand un autre fournisseur est le cerveau.
# Il n'est jamais offert au cerveau externe lui-même : il bouclerait sur place.
CONSULT_BRAIN_DECLARATION = {
    "name": "consult_brain",
    "description": (
        "Transmet la demande de l'utilisateur au cerveau choisi (Azure, OpenAI, "
        "DeepSeek, Claude…), qui réfléchit, exécute les outils nécessaires et "
        "rédige la réponse. Appelle-le pour TOUTE demande, même triviale, et "
        "prononce ensuite son texte mot pour mot."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "question": {
                "type": "STRING",
                "description": (
                    "La demande de l'utilisateur, transcrite mot pour mot, "
                    "sans reformulation ni résumé."
                ),
            },
            "context": {
                "type": "STRING",
                "description": (
                    "Optionnel : ce qui vient d'être dit ou fait et qui aide à "
                    "comprendre la demande (référence à « ça », à une fenêtre, "
                    "à un fichier)."
                ),
            },
        },
        "required": ["question"],
    },
}

TOOL_DECLARATIONS = [
    {
        "name": "open_app",
        "description": (
            "Opens any application on the computer. "
            "Use this whenever the user asks to open, launch, or start any app, "
            "website, or program. Always call this tool — never just say you opened it. "
            "If the user wants to run a command or type something into the app (e.g. 'lance kitty et tape codex'), "
            "pass the command in the 'command' parameter. "
            "Set hidden=true when the user wants an app running without seeing its window "
            "(e.g. 'lance X en arrière-plan/caché/sans l'afficher') — it launches on Hyprland's "
            "invisible special workspace, verified for real, never just focused away."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "app_name": {
                    "type": "STRING",
                    "description": "Exact name of the application (e.g. 'WhatsApp', 'Chrome', 'Spotify')"
                },
                "command": {
                    "type": "STRING",
                    "description": "Optional command or text to automatically type and execute in the application after opening (e.g. 'codex' or 'btop' when opening a terminal like kitty)."
                },
                "workspace": {
                    "type": "INTEGER",
                    "description": "Optional Hyprland/EndeavourOS workspace (bureau) number to launch the app into, e.g. 3 for 'launch kitty on desktop 3'. Omit to launch on the current workspace. Ignored if hidden=true."
                },
                "hidden": {
                    "type": "BOOLEAN",
                    "description": "true to launch the app invisibly on Hyprland's hidden special workspace, without ever showing its window."
                }
            },
            "required": ["app_name"]
        }
    },
    {
        "name": "close_app",
        "description": (
            "Closes/quits a running application. Use whenever the user asks to "
            "close, quit, stop, or kill an app that is currently open. This is the ONLY "
            "tool that should be used to close apps -- never use shell_exec/pkill or "
            "computer_settings for this. If multiple distinct instances of the app are "
            "open (e.g. several named terminals), this tool will itself ask the user "
            "which one to close and remember the answer for the next turn -- just call "
            "it again with the reply as app_name, do not try to resolve the ambiguity yourself. "
            "CRITICAL: this tool closes exactly ONE window by default. Always pass the user's "
            "exact wording in 'description' so it can tell 'ferme kitty que tu viens d'ouvrir' "
            "(the one you just launched) from 'ferme toutes les fenêtres kitty' (all of them). "
            "Never set target='all' unless the user explicitly asked to close every window."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "app_name": {
                    "type": "STRING",
                    "description": "Exact name of the application to close (e.g. 'VLC', 'Chrome')"
                },
                "list_instances": {
                    "type": "BOOLEAN",
                    "description": "If true, instead of closing the app, returns a list of all running instances (windows/processes) for this app."
                },
                "workspace": {
                    "type": "INTEGER",
                    "description": "Optional workspace (bureau) number to limit closing to windows on a specific desktop, e.g. 2 for 'ferme kitty dans le bureau 2'."
                },
                "description": {
                    "type": "STRING",
                    "description": "The user's exact original wording, verbatim. Essential: it carries which window is meant ('celui que tu viens d'ouvrir', 'cette fenêtre', 'toutes les fenêtres')."
                },
                "target": {
                    "type": "STRING",
                    "description": "Scope override, only when unambiguous: 'last' (the window the assistant just opened), 'active' (the focused window), 'all' (every matching window — only if the user explicitly said all/toutes)."
                },
                "selection": {
                    "type": "INTEGER",
                    "description": "Numéro du choix Android après une ambiguïté, 1 pour le premier, 2 pour le deuxième",
                },
                "force": {
                    "type": "BOOLEAN",
                    "description": "Force-kill instead of asking the app to close politely. Use only if a normal close already failed."
                }
            },
            "required": ["app_name"]
        }
    },
    {
        "name": "web_search",
        "description": (
            "Searches the web. Use for ANY question about current facts, events, prices, "
            "or topics — always prefer this over guessing. "
            "For SOMEONE ELSE's creator/influencer account handle, use mode='social' "
            "with the exact handle and platform. This performs a site-restricted profile lookup "
            "and only reports publicly indexed profile URLs; never guess an account. "
            "NEVER use it for the user's OWN TikTok (« mon TikTok », « ma dernière vidéo », "
            "« mes abonnés », « combien de vues ») → tiktok_tracker / tiktok_coach. "
            "Modes: 'search' (default), 'news' (latest headlines on a topic), "
            "'research' (deep comprehensive answer), 'price' (product cost lookup), "
            "'nearby' (a physical place/service near the user — 'closest hospital', "
            "'pharmacy near me' — uses local/Maps search and the user's real location "
            "instead of generic web results), "
            "'compare' (side-by-side comparison of items)."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query":  {"type": "STRING", "description": "Search query or topic"},
                "mode":   {"type": "STRING", "description": "search | social | news | research | price | nearby | compare"},
                "platform": {"type": "STRING", "description": "For social mode: tiktok | instagram | youtube | facebook | x | twitch | snapchat"},
                "items":  {"type": "ARRAY",  "items": {"type": "STRING"}, "description": "Items to compare (compare mode)"},
                "aspect": {"type": "STRING", "description": "Comparison aspect: price | specs | reviews | features"},
            },
            "required": ["query"]
        }
    },
    {
        "name": "image_search",
        "description": (
            "Searches images in the background and displays the best matching results "
            "inside ANO-GPT's native full-screen cyberpunk gallery. MUST be used when "
            "the user asks to find, search, show or display photos/images from the web "
            "(for example: 'cherche des photos de chat et affiche-les'). Never use "
            "browser_control, open_app or web_search for that request: do not open a "
            "browser window. Results are ranked for direct relevance, downloaded and "
            "validated before display. The first image is the strongest match."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {"type": "STRING", "description": "Exact visual subject requested by the user"},
                "limit": {"type": "INTEGER", "description": "Number of images to display, 1 to 8", "minimum": 1, "maximum": 8},
            },
            "required": ["query"],
        },
    },
    {
        "name": "generate_image",
        "description": (
            "Crée une nouvelle image avec le modèle d'image Azure Foundry dédié, "
            "l'enregistre localement et l'affiche dans la galerie ANO-GPT. "
            "Utilise-le uniquement quand l'utilisateur demande de créer, générer, "
            "dessiner ou imaginer une image inédite."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "prompt": {"type": "STRING", "description": "Description détaillée de l'image à créer"},
                "size": {"type": "STRING", "description": "1024x1024, 1024x1536 ou 1536x1024"},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "generate_video",
        "description": (
            "Crée une nouvelle vidéo avec le modèle vidéo Azure Foundry (Sora), "
            "l'enregistre dans ~/Vidéos/ANO-GPT et l'ouvre dans le lecteur ANO-GPT. "
            "Utilise-le quand l'utilisateur demande de créer, générer ou animer une "
            "vidéo inédite. Ne l'utilise jamais pour chercher une vidéo existante "
            "(youtube_video) ni pour lire un fichier local (file_controller)."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "prompt": {"type": "STRING", "description": "Description détaillée de la vidéo à créer"},
                "seconds": {"type": "INTEGER", "description": "Durée en secondes, 1 à 20", "minimum": 1, "maximum": 20},
                "size": {"type": "STRING", "description": "1280x720, 720x1280, 1920x1080, 1080x1920 ou 1024x1024"},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "generate_document",
        "description": (
            "Rédige un document complet (rapport, lettre, compte rendu, note, "
            "présentation) avec le modèle document Azure Foundry, l'enregistre dans "
            "~/Documents/ANO-GPT au format demandé et affiche le résultat. "
            "Utilise-le quand l'utilisateur demande d'écrire, rédiger ou générer un "
            "document, un rapport, un CV, une lettre ou un diaporama."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "subject": {"type": "STRING", "description": "Sujet et contenu attendu du document"},
                "title": {"type": "STRING", "description": "Titre du document"},
                "format": {"type": "STRING", "description": "md, txt, html, docx, pdf ou pptx"},
                "instructions": {"type": "STRING", "description": "Contraintes de ton, longueur ou plan"},
                "open_after": {"type": "BOOLEAN", "description": "Ouvrir le fichier une fois écrit"},
            },
            "required": ["subject"],
        },
    },
    {
        "name": "show_last_generated_image",
        "description": (
            "Ouvre dans le visionneur d'images intégré la dernière image créée par ANO-GPT. "
            "Utilise cet outil uniquement si l'utilisateur demande explicitement d'afficher, "
            "voir en grand ou ouvrir l'image qui vient d'être générée."
        ),
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    {
        "name": "close_image_gallery",
        "description": (
            "Closes the full-screen image gallery and returns to the normal ANO-GPT view. "
            "Use when the user says to close, hide or remove the displayed photos/images."
        ),
        "parameters": {"type": "OBJECT", "properties": {}, "required": []},
    },
    {
        "name": "system_status",
        "description": (
            "Returns real-time system metrics: CPU usage, RAM, GPU load, CPU temperature, "
            "uptime, and process count. Use when the user asks about computer performance, "
            "temperature, memory, or resource usage."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
        "name": "weather_report",
        "description": "Gives the weather report to user",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "city": {"type": "STRING", "description": "City name"}
            },
            "required": ["city"]
        }
    },
    {
        "name": "location",
        "description": (
            "Returns the user's verified current location, prioritizing ANO "
            "Remote phone GPS. ALWAYS call this when the user asks where they "
            "are, which city/area/country they are in, or asks for their "
            "location. Never infer a country from the language they speak."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "send_message",
        "description": (
            "Sends a text message via WhatsApp, Telegram, or another desktop messaging app. "
            "NEVER use it for an SMS: an SMS goes out through the phone_sms tool only. "
            "SAFETY: always shows a preview card and asks for confirmation before actually "
            "sending — the first call always previews, never sends. Once the user confirms, "
            "call again with the exact same receiver/message_text/platform plus 'confirm': true."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "receiver":     {"type": "STRING", "description": "Recipient contact name"},
                "message_text": {"type": "STRING", "description": "The message to send"},
                "platform":     {"type": "STRING", "description": "Platform: WhatsApp, Telegram, etc."},
                "confirm":      {"type": "BOOLEAN", "description": "true only after the user explicitly confirmed the previewed message"},
            },
            "required": ["receiver", "message_text", "platform"]
        }
    },
    {
        "name": "reminder",
        "description": (
            "Crée, liste ou annule un rappel persistant. Accepte soit date/time "
            "explicites, soit une description naturelle comme 'dans 20 minutes, "
            "appeler maman'. À l'échéance, ANO-GPT l'annonce à voix haute et "
            "retire automatiquement sa carte."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":  {"type": "STRING", "description": "set | list | cancel"},
                "description": {"type": "STRING", "description": "Demande naturelle complète, surtout pour une durée relative"},
                "date":    {"type": "STRING", "description": "Date in YYYY-MM-DD format"},
                "time":    {"type": "STRING", "description": "Time in HH:MM format (24h)"},
                "message": {"type": "STRING", "description": "Reminder message text"},
                "value":   {"type": "STRING", "description": "Numéro ou identifiant du rappel à annuler"}
            },
            "required": []
        }
    },
    {"name": "timer", "description": "Crée, liste ou annule plusieurs minuteurs nommés simultanés.",
     "parameters": {"type": "OBJECT", "properties": {
         "action": {"type": "STRING", "description": "set | list | cancel"},
         "name": {"type": "STRING", "description": "Nom, par exemple pâtes ou thé"},
         "minutes": {"type": "NUMBER", "description": "Durée en minutes"},
         "value": {"type": "STRING", "description": "Nom ou id à annuler"}}, "required": ["action"]}},
    {
        "name": "youtube_video",
        "description": (
            "Contrôleur YouTube complet. OBLIGATOIRE pour toute demande qui mentionne "
            "YouTube, une vidéo YouTube, une chaîne, un tutoriel vidéo, les sous-titres "
            "ou le lecteur YouTube. RÈGLE STRICTE : 'cherche/recherche/trouve une vidéo' "
            "=> action='search' (affiche des résultats, ne lance rien). 'joue/lance/ouvre/"
            "regarde une vidéo' => action='play'. Après une recherche, 'la 2/deuxième' "
            "=> action='select', index=2. Ne jamais utiliser music_control pour YouTube. "
            "Pour télécharger un morceau vers ~/Musique, utiliser download_music, pas cet outil. "
            "La recherche affiche une grille de cartes vidéo dans l'application et la lecture "
            "se fait dans le lecteur intégré : ne jamais demander d'ouvrir un navigateur. "
            "Gère recherche, sélection, lecture, pause, volume, navigation, vitesse, "
            "plein écran, sous-titres, mode cinéma, mini-lecteur, infos, transcription, "
            "résumé et tendances."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "search | play | select | pause | resume | toggle | mute | volume | seek | forward | back | restart | fullscreen | subtitles | theater | miniplayer | next | previous | speed | stop | close | status | get_info | transcript | summarize | trending"},
                "query":  {"type": "STRING", "description": "Termes de recherche YouTube"},
                "url":    {"type": "STRING", "description": "URL YouTube explicite"},
                "index":  {"type": "INTEGER", "description": "Numéro 1-based d'un résultat déjà affiché"},
                "result": {"type": "STRING", "description": "Numéro, ID ou partie du titre d'un résultat"},
                "limit":  {"type": "INTEGER", "description": "Nombre de résultats, 1 à 12"},
                "seconds": {"type": "INTEGER", "description": "Secondes pour seek/forward/back"},
                "volume": {"type": "INTEGER", "description": "Volume YouTube de 0 à 100"},
                "speed":  {"type": "NUMBER", "description": "Vitesse de lecture de 0.25 à 2.0"},
                "save":   {"type": "BOOLEAN", "description": "Sauvegarder le résumé"},
                "region": {"type": "STRING", "description": "Code pays pour les tendances, ex. FR, US"},
                "max_chars": {"type": "INTEGER", "description": "Taille maximale de transcription retournée"}
            },
            "required": []
        }
    },
    {
        "name": "screen_process",
        "description": (
            "Captures the screen or webcam and runs expert vision (OCR + Gemini Pro, "
            "diagrams, UI targets). MUST be called when the user asks what is on screen, "
            "what you see, look at the camera, analyze a schema/chart/PDF/code, etc. "
            "You have NO visual ability without this tool. "
            "The tool result is the finished analysis: answer from it, do not call "
            "the tool again, and do not wait for a later image. "
            "A block starting with [VISION EXPERTE] or [TEXTE DE L'ÉCRAN] is the answer. "
            "When using camera: the live view stays open until the user says close it "
            "or calls close_camera."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "angle": {"type": "STRING", "description": "'screen' to capture display, 'camera' for webcam. Default: 'screen'"},
                "text":  {"type": "STRING", "description": "The question or instruction about the captured image"}
            },
            "required": ["text"]
        }
    },
    {
        "name": "point_on_screen",
        "description": (
            "Visually highlights, points out, or traces a trajectory directly on the user's screen "
            "using a futuristic neon HUD overlay. MUST be used whenever explaining where an element, "
            "button, menu item, confirmation card, or region is located. "
            "Pass target (e.g. 'confirmation', 'carte de confirmation', 'terminal', 'bouton installer') "
            "and the real-time system will detect and frame it on screen without hallucinating coordinates. "
            "Coordinates can optionally be supplied: [x, y] for laser point; [x, y, w, h] for bounding box."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "target": {
                    "type": "STRING",
                    "description": "Name or visual label of the element/widget to highlight (e.g. 'confirmation', 'carte_confirmation', 'bouton installer', 'lecteur audio')."
                },
                "description": {
                    "type": "STRING",
                    "description": "Short explanation or label for the highlighted element (e.g. 'Bouton Paramètres', 'Demande de confirmation', 'Message d'erreur')"
                },
                "coordinates": {
                    "type": "ARRAY",
                    "items": {"type": "INTEGER"},
                    "description": "Optional pixel or normalized coordinates [x, y] or [x, y, w, h]. If omitted, target will be dynamically detected on screen."
                },
                "mode": {
                    "type": "STRING",
                    "description": "Optional visual mode: 'auto' (default), 'highlight' (pulsing neon box with arrow), 'laser' (red laser reticle with ripple waves), 'path' (animated trajectory)."
                },
                "duration": {
                    "type": "NUMBER",
                    "description": "Duration in seconds to show the visual annotation on screen (default 3.0s)."
                }
            },
            "required": ["description"]
        }
    },
    {
        "name": "camera_control",
        "description": (
            "Opens ANO-GPT's own full-screen live camera view and controls it. "
            "MUST be used when the user says: ouvre l'appareil photo, ouvre la caméra, "
            "open the camera, montre-moi la caméra, prends une photo, prends-moi en photo, "
            "filme, enregistre une vidéo, arrête la vidéo, passe sur la caméra du téléphone. "
            "NEVER call open_app for a camera or photo request — this tool shows the live "
            "feed inside ANO-GPT instead of launching any external camera application. "
            "passe sur la caméra frontale, caméra selfie, retourne la caméra. "
            "Actions: 'open' (show live view), 'photo' (save a still), 'video_start', "
            "'video_stop', 'switch' (toggle PC webcam ↔ phone camera), "
            "'lens' (pick the phone's front or back camera, see the lens parameter), "
            "'flip' (toggle between the phone's front and back camera), 'close'. "
            "The source parameter picks which camera: 'pc' for the computer webcam, "
            "'phone' for the camera of the paired Android phone. "
            "For a selfie or 'films-moi', use lens='front' — it switches to the phone "
            "automatically because only the phone has a front camera."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": "open | photo | video_start | video_stop | switch | lens | flip | close",
                },
                "source": {
                    "type": "STRING",
                    "description": "'pc' (webcam de l'ordinateur) ou 'phone' (caméra du téléphone appairé)",
                },
                "lens": {
                    "type": "STRING",
                    "description": (
                        "Objectif du téléphone : 'front' (caméra frontale, selfie, "
                        "celle qui regarde l'utilisateur) ou 'back' (caméra arrière). "
                        "Utilisable avec action='open', 'photo', 'video_start' ou 'lens'."
                    ),
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "close_camera",
        "description": (
            "Closes the live camera view shown on screen. "
            "Call when user says: close camera, stop camera, turn off camera, "
            "kamerayı kapat, kapat, creepy, etc."
        ),
        "parameters": {"type": "OBJECT", "properties": {}, "required": []}
    },
    {
        "name": "show_map",
        "description": (
            "Displays an interactive map on screen, centered on a place. Use whenever "
            "the user asks to see a map, to show where something is, or right after a "
            "web_search(mode='nearby') call so they can see the result visually (e.g. "
            "user asked for the nearest hospital: call web_search first, then show_map "
            "with that hospital's name/address as query so they see it on the map too). "
            "IMPORTANT: if the web_search(mode='nearby') result included a 'Coordonnées "
            "(pour show_map)' line for the place you're showing, pass those exact lat/lon "
            "instead of query — geocoding a specific business name often fails, exact "
            "coordinates never do. "
            "For 'ma position/où suis-je', omit query and lat/lon so the tool requests "
            "a fresh phone GPS reading. Never pass a guessed city such as Conakry for "
            "the user's own position. "
            "Speak about what's shown on the map in your reply (distance, address, etc.) "
            "— don't just open it silently."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {
                    "type": "STRING",
                    "description": "Place/address to center the map on (e.g. 'Conakry, Guinée'). Ignored if "
                                    "lat/lon are given. If everything is omitted, centers on the user's own location.",
                },
                "lat": {"type": "NUMBER", "description": "Exact latitude, when known (preferred over query for a specific business/place)"},
                "lon": {"type": "NUMBER", "description": "Exact longitude, when known (preferred over query for a specific business/place)"},
                "radius_km": {"type": "NUMBER", "description": "Approximate zoom radius in km (default 3)"},
            },
            "required": []
        }
    },
    {
        "name": "navigate",
        "description": (
            "Démarre ou contrôle le guidage GPS parlé pas-à-pas et l'itinéraire néon animé. "
            "OBLIGATOIRE dès que l'utilisateur demande : 'Navigue vers X', 'Guide-moi jusqu'à Y', 'Lance le GPS', "
            "'Itinéraire vers Z', 'Arrête la navigation', 'Où en est le trajet ?', 'Prochaine étape'. "
            "Affiche l'itinéraire complet sur la carte grand écran et énonce vocalement chaque manœuvre "
            "avec anticipation (seuils 500m / 150m / immédiat) synchronisé avec le GPS du smartphone Android (ANO-Remote)."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "destination": {"type": "STRING", "description": "Nom du lieu, adresse ou cible de destination"},
                "action": {"type": "STRING", "description": "start (défaut) | stop | status"},
                "mode": {"type": "STRING", "description": "driving (voiture) | walking (piéton) | cycling (vélo)"}
            },
            "required": []
        }
    },
    {
        "name": "find_nearby",
        "description": (
            "Finds ANY place, shop, business or service near the user "
            "('où est la pharmacie la plus proche ?', 'trouve-moi un bon restaurant', "
            "'où acheter une PS5 ?', 'hôtels autour de moi', 'stations d'essence'). "
            "Searches Google Maps through SerpAPI when a key is configured — ratings, "
            "reviews, addresses, phone numbers and opening hours — and always completes "
            "with OpenStreetMap. Results are pinned, numbered, on the SINGLE large map "
            "of ANO-GPT, framed so every result is visible. "
            "After the tool returns, ALWAYS tell the user how many places were found "
            "and cite the nearest place by its exact name and distance. Never answer "
            "with only a generic phrase such as 'à proximité de votre position'. "
            "Use plain words in the user's language for `query`; there is no fixed "
            "category list. Set `near` to search around a named place instead of the "
            "user's own position."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {
                    "type": "STRING",
                    "description": "What to look for, in plain words: 'pharmacie', 'restaurant italien', 'PS5', 'hôtel'",
                },
                "near": {
                    "type": "STRING",
                    "description": "Optional: search around this named place instead of the user's position",
                },
                "radius_km": {"type": "NUMBER", "description": "Search radius in km (default: 5.0)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "close_map",
        "description": "Closes the map view shown on screen, returning to the normal assistant view.",
        "parameters": {"type": "OBJECT", "properties": {}, "required": []}
    },
    {
        "name": "calendar_control",
        "description": (
            "Lit et modifie l'agenda Google Calendar ou CalDAV de façon sûre. Utiliser list pour le "
            "programme d'une période, availability pour vérifier les chevauchements, create/update/delete pour les rendez-vous, status "
            "pour diagnostiquer et connect pour autoriser Google. Les invités peuvent être "
            "des noms du carnet de contacts."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "status | connect | list | availability | create | update | delete"},
                "provider": {"type": "STRING", "description": "auto | google | caldav"},
                "id": {"type": "STRING", "description": "Identifiant de l'événement pour update/delete"},
                "title": {"type": "STRING", "description": "Titre du rendez-vous"},
                "start": {"type": "STRING", "description": "Début ISO 8601, date AAAA-MM-JJ, ou période dictée pour list : aujourd'hui, demain, cette semaine, semaine prochaine, ce week-end, ce mois, vendredi, 3 prochains jours"},
                "end": {"type": "STRING", "description": "Fin ISO 8601 ou date AAAA-MM-JJ"},
                "description": {"type": "STRING"},
                "location": {"type": "STRING"},
                "attendees": {"type": "ARRAY", "items": {"type": "STRING"}, "description": "E-mails ou noms du carnet"},
                "max_results": {"type": "INTEGER"},
                "calendar_id": {"type": "STRING", "description": "Google: primary par défaut"},
                "dry_run": {"type": "BOOLEAN", "description": "For create: preview the event and conflicts without writing"},
                "allow_conflict": {"type": "BOOLEAN", "description": "true only after the user explicitly accepts the displayed overlap"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "cloud_integrations_control",
        "description": "Notion et Figma utilisent leurs API gratuites avec un jeton personnel stocké dans le trousseau ; Calendar, NotebookLM, Gemini, Stitch et Figma Make s'ouvrent dans Google Chrome avec les abonnements de l'utilisateur.",
        "parameters": {"type": "OBJECT", "properties": {
            "service": {"type": "STRING", "description": "calendar | notion | figma | figma_make | notebooklm | gemini | stitch"},
            "action": {"type": "STRING", "description": "status | connect | open ; Notion : search | create_note ; Figma : inspect"},
            "query": {"type": "STRING"}, "title": {"type": "STRING"}, "content": {"type": "STRING"},
            "parent_id": {"type": "STRING"}, "file_key": {"type": "STRING"}},
            "required": ["service", "action"]},
    },
    {
        "name": "tiktok_tracker",
        "description": (
            "Suit le compte TikTok de l'utilisateur en quasi temps réel, comme l'application Blow : "
            "abonnés, j'aime, nombre de vidéos, vues/likes/commentaires des dernières vidéos, avec "
            "les variations depuis la lecture précédente et depuis le début de la journée. "
            "Actions : 'status' (chiffres actuels — défaut ; « où en est mon TikTok », « combien "
            "d'abonnés », « ça monte ? »), 'start' (« suis mon TikTok », « surveille mon compte » : "
            "lance la veille continue, carte à l'écran, annonces automatiques des nouveaux abonnés, "
            "paliers et vidéos qui décollent), 'stop', 'history' (évolution sur N heures, param hours), "
            "'videos' (détail des dernières vidéos), 'set_handle' (changer de compte, param handle), "
            "'set_interval' (param interval_s, minimum 45 s). Une lecture prend une dizaine de secondes : "
            "prévenir l'utilisateur que tu regardes. Les chiffres viennent de la page publique, pas "
            "d'un accès privé au compte."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": "status | start | stop | history | videos | set_handle | set_interval",
                },
                "handle": {"type": "STRING", "description": "@ TikTok (sans le @) ou URL du profil"},
                "hours": {"type": "NUMBER", "description": "Pour history : fenêtre en heures (défaut 24)"},
                "interval_s": {"type": "NUMBER", "description": "Pour set_interval : secondes entre deux lectures"},
            },
            "required": [],
        },
    },
    {
        "name": "tiktok_coach",
        "description": (
            "Coach TikTok personnel (comme Blow Up) pour aider l'utilisateur à percer. "
            "Actions : 'diagnose' — « pourquoi ma vidéo n'a pas marché », « pourquoi elle est bloquée à "
            "300 vues », « pourquoi si peu de likes sur ma dernière vidéo » : lit les chiffres, télécharge "
            "la vidéo, la visionne et explique les causes + quoi changer (query = quelle vidéo : "
            "« la dernière », « l'avant-dernière », « la plus vue », des mots du titre, ou une URL). "
            "'review' — bilan du compte : ce qui marche, ce qui bloque, plan et idées de vidéos. "
            "'draft' — « analyse cette vidéo avant que je la poste », « regarde ma vidéo dans Vidéos » : "
            "visionne un fichier local (path ou mots du nom ; par défaut la vidéo la plus récente), "
            "juge l'accroche et la rétention, propose montage, description, hashtags, texte de "
            "couverture et meilleure heure (note = précisions de l'utilisateur sur son intention). "
            "'best_time' — meilleure heure pour poster. diagnose et draft lancent le visionnage EN FOND "
            "et rendent tout de suite un premier constat à dire ; l'avis complet est annoncé tout seul "
            "environ une minute plus tard : ne relance pas l'outil, ne dis pas que c'est fini."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "diagnose | review | draft | best_time"},
                "query": {"type": "STRING", "description": "Vidéo visée (diagnose) ou fichier (draft), tel que dit"},
                "path": {"type": "STRING", "description": "Pour draft : chemin du fichier si connu"},
                "note": {"type": "STRING", "description": "Pour draft : ce que l'utilisateur veut obtenir avec cette vidéo"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "prayer_control",
        "description": (
            "Consulte les heures de prière musulmanes (Fajr, Dhuhr, Asr, Maghrib, Isha et Chourouk) "
            "calculées localement selon la position géographique actuelle, donne le temps restant avant la "
            "prochaine prière, ou active/désactive l'annonce vocale d'une prière spécifique (ex: couper Fajr). "
            "Actions disponibles : 'next' (prochaine prière et temps restant), 'today' (tous les horaires du jour), "
            "'toggle' (activer/désactiver une prière avec 'prayer' et optionnellement 'enabled'), "
            "'toggle_all' (activer/désactiver tous les rappels avec 'enabled'), "
            "'set_method' (changer de convention astronomique: MWL, UOIF, EGYPT, ISNA, MAKKAH, KARACHI), "
            "'status' (afficher la méthode, la ville et l'état de chaque prière)."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": "next | today | toggle | toggle_all | set_method | status",
                },
                "prayer": {
                    "type": "STRING",
                    "description": "Nom de la prière pour toggle: fajr | dhuhr | asr | maghrib | isha",
                },
                "enabled": {
                    "type": "BOOLEAN",
                    "description": "Pour toggle ou toggle_all: true pour activer, false pour désactiver",
                },
                "method": {
                    "type": "STRING",
                    "description": "Pour set_method: MWL | UOIF | EGYPT | ISNA | MAKKAH | KARACHI",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "contacts_control",
        "description": (
            "Gère le carnet LOCAL du PC utilisé par send_message, Gmail et les invitations "
            "calendrier. Il est distinct du carnet du téléphone : ne conclus JAMAIS depuis cet "
            "outil qu'un contact n'existe pas — pour cela, interroge phone_contacts. "
            "Utiliser add/update pour enregistrer noms, alias, e-mails, téléphone et "
            "identifiants de messagerie."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "list | search | add | update (merges new emails/aliases with the existing ones, newest first) | remove_email | remove_alias | remove_phone | delete"},
                "value": {"type": "STRING", "description": "remove_email/remove_alias: the exact value to remove"},
                "id": {"type": "STRING"}, "query": {"type": "STRING"},
                "name": {"type": "STRING"},
                "aliases": {"type": "ARRAY", "items": {"type": "STRING"}},
                "emails": {"type": "ARRAY", "items": {"type": "STRING"}},
                "phone": {"type": "STRING"}, "notes": {"type": "STRING"},
                "whatsapp": {"type": "STRING"}, "telegram": {"type": "STRING"},
                "signal": {"type": "STRING"}, "discord": {"type": "STRING"},
                "instagram": {"type": "STRING"}, "messenger": {"type": "STRING"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "phone_call",
        "description": (
            "Lance un appel cellulaire exclusivement depuis ANO-Remote Android. "
            "Transmets dans target le nom ou numéro demandé par l'utilisateur : Android comprend les "
            "variantes familiales usuelles (maman/Mum, papa/Dad). Si Android ne trouve rien, demande "
            "le numéro complet ou les deux derniers chiffres. Avec deux à cinq chiffres, Android renvoie "
            "des numéros masqués et tu dois demander lequel appeler ; rappelle ensuite cet outil avec le "
            "même target et le champ selection correspondant. "
            "Ne cherche jamais le contact sur le PC : l'application Android vérifie son "
            "propre carnet Contacts, refuse les absences et ambiguïtés, puis appelle avec "
            "la carte SIM seulement si les permissions et l'option d'appels automatiques "
            "ont été explicitement activées sur le téléphone."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "target": {
                    "type": "STRING",
                    "description": "Nom Android ou numéro prononcé, ex. garage, Maman, +224...",
                },
                "selection": {
                    "type": "INTEGER",
                    "description": "Numéro du choix Android, seulement après une liste de contacts (1 à 8).",
                    "minimum": 1, "maximum": 8,
                },
            },
            "required": ["target"],
        },
    },
    {
        "name": "phone_hangup",
        "description": (
            "Raccroche l'appel cellulaire en cours sur le téléphone ANO-Remote. "
            "À utiliser dès que l'utilisateur dit « raccroche », « coupe l'appel » ou "
            "« termine l'appel ». Aucun paramètre : le téléphone raccroche l'appel actif."
        ),
        "parameters": {"type": "OBJECT", "properties": {}, "required": []},
    },
    {
        "name": "phone_contacts",
        "description": (
            "Cherche un contact dans le carnet du téléphone Android (le vrai carnet de "
            "l'utilisateur). C'est le SEUL outil à utiliser pour répondre à « est-ce que j'ai "
            "un contact nommé X ? », « quel est le numéro de X ? » ou « combien de numéros "
            "finissent par 97 ? ». N'utilise jamais contacts_control pour cela : contacts_control "
            "ne lit que le carnet local du PC, souvent vide. La recherche accepte un nom ou une "
            "fin de numéro de deux à cinq chiffres ; les numéros reviennent masqués, seuls les "
            "quatre derniers chiffres sont visibles."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {"type": "STRING",
                          "description": "Nom cherché, ou fin de numéro (2 à 5 chiffres)."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "phone_sms",
        "description": (
            "Envoie un SMS depuis la carte SIM du téléphone Android. C'est le SEUL moyen "
            "d'envoyer un SMS : send_message ne sert qu'aux messageries de bureau "
            "(WhatsApp, Telegram, Signal…). Le destinataire est résolu par le téléphone dans "
            "son propre carnet. Une carte de confirmation s'affiche ; n'annonce jamais l'envoi "
            "avant le retour de l'outil et ne rappelle jamais l'outil tant qu'aucune réponse "
            "n'est arrivée. Si le téléphone renvoie plusieurs contacts, demande lequel puis "
            "rappelle avec le même target et le champ selection."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "target": {"type": "STRING",
                           "description": "Nom Android, numéro complet, ou fin de numéro."},
                "body": {"type": "STRING", "description": "Texte exact du SMS."},
                "selection": {"type": "INTEGER",
                              "description": "Numéro du choix Android, après une liste (1 à 8).",
                              "minimum": 1, "maximum": 8},
                "auto_reply_authorized": {
                    "type": "BOOLEAN",
                    "description": (
                        "true UNIQUEMENT si, juste après avoir reçu ce SMS, l'utilisateur "
                        "a explicitement dit qu'il laisse ANO-GPT rédiger ET envoyer la réponse. "
                        "Sinon omettre ou false : la confirmation humaine reste obligatoire."
                    ),
                },
            },
            "required": ["target", "body"],
        },
    },
    {
        "name": "sparring_partner",
        "description": (
            "Démarre et pilote un entraînement vocal interactif réaliste : entretien "
            "technique, entretien d'embauche, client difficile ou oral technique. "
            "Pour toute demande comme « entraîne-moi », appeler action=start, adopter le rôle "
            "retourné et poser une seule question à la fois. Après CHAQUE réponse de "
            "l'utilisateur, rappeler cet outil avec action=answer et la transcription exacte "
            "dans answer avant de donner le feedback ou la question suivante. Utiliser end "
            "quand l'utilisateur arrête afin de produire son rapport d'élocution. Ne jamais "
            "inventer de débit vocal : la durée réelle est capturée automatiquement quand elle existe."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "start | answer | status | pause | resume | end"},
                "scenario": {"type": "STRING", "description": "entretien_technique | entretien_embauche | client_difficile | oral_technique"},
                "role": {"type": "STRING", "description": "Rôle libre demandé, utilisé pour choisir le scénario le plus proche"},
                "difficulty": {"type": "STRING", "description": "debutant | intermediaire | avance | expert"},
                "objective": {"type": "STRING", "description": "Poste, technologie, examen, produit ou objectif précis"},
                "rounds": {"type": "INTEGER", "description": "Nombre de réponses à entraîner, de 3 à 8"},
                "answer": {"type": "STRING", "description": "Transcription exacte de la dernière réponse pour action=answer"},
                "duration_ms": {"type": "NUMBER", "description": "Durée vocale seulement si elle est réellement connue ; sinon omettre"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "focus_guard",
        "description": (
            "Active et pilote le Bouclier Anti-Distraction pendant une session de travail "
            "explicitement demandée. Il mesure localement les changements de contexte, intervient "
            "après dispersion sans progression, bloque précisément Shorts/Reels/X/TikTok quand "
            "CDP donne leur URL, et programme des pauses qui protègent le flow. Utiliser start avec "
            "un objectif concret ; progress quand l'utilisateur annonce une avancée ; break/resume "
            "pour les pauses ; allow pour suspendre le blocage ; restore pour rouvrir les onglets "
            "bloqués ; stop termine immédiatement et désactive tout blocage."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "start | status | progress | break | resume | block | allow | restore | stop"},
                "goal": {"type": "STRING", "description": "Objectif concret et unique de la session Focus"},
                "work_minutes": {"type": "INTEGER", "description": "Durée de travail entre 15 et 120 minutes"},
                "break_minutes": {"type": "INTEGER", "description": "Durée de pause entre 3 et 30 minutes"},
                "switch_threshold": {"type": "INTEGER", "description": "Nombre de changements en 10 minutes déclenchant l'intervention, 12 par défaut"},
                "block_feeds": {"type": "BOOLEAN", "description": "Bloquer les flux infinis pendant la session, vrai par défaut"},
                "note": {"type": "STRING", "description": "Avancée annoncée avec action=progress"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "email_control",
        "description": (
            "Connects and accesses the user's Gmail securely through OAuth 2.0. "
            "Use status to diagnose access, connect to launch or renew authorization directly in Google Chrome. "
            "When asked to connect Gmail, retry Gmail or launch its authorization via Chrome, "
            "call action='connect' immediately; do not substitute setup instructions or ask again. "
            "Use "
            "unread for unread mail, recent for the inbox, and search/advanced_search for "
            "natural-language or native Gmail queries with precise filters, "
            "read with an ID or the displayed result number, and summary for unread mail. "
            "For EVERY request that lists, summarizes, or identifies messages, call this tool: "
            "it renders one visible ANO-GPT card per message. Never only recite Gmail "
            "subjects from memory or from a briefing. "
            "Never claim the inbox is empty when this tool reports a setup or connection error. "
            "When the tool answers that Gmail is not configured yet, read the returned steps "
            "to the user instead of just repeating that it is not configured — and use "
            "action='setup' when they ask how to finish the Gmail configuration."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "status | setup | connect | unread (default) | recent | search | advanced_search | read | summary | send | reply | mark_read | mark_unread | archive | star | unstar | trash. send/reply/trash always show a preview the user must confirm with a click."},
                "to": {"type": "STRING", "description": "send: recipient address or a saved contact name"},
                "body": {"type": "STRING", "description": "send/reply: the message text to send, written out in full"},
                "cc": {"type": "STRING", "description": "send: optional carbon-copy addresses"},
                "query": {"type": "STRING", "description": "Natural-language request, native Gmail query (e.g. 'from:alice newer_than:30d'), or displayed number for read"},
                "id": {"type": "STRING", "description": "Gmail message ID or displayed result number for read"},
                "max_results": {"type": "INTEGER", "description": "Number of messages, from 1 to 100 (default 10)"},
                "from": {"type": "STRING", "description": "Exact sender name, address, or domain filter"},
                "to": {"type": "STRING", "description": "Exact recipient name or address filter"},
                "subject": {"type": "STRING", "description": "search: words that must occur in the subject; send/reply: the subject line (reply defaults to 'Re: …')"},
                "after": {"type": "STRING", "description": "Minimum date: YYYY-MM-DD or DD/MM/YYYY"},
                "before": {"type": "STRING", "description": "Maximum date: YYYY-MM-DD or DD/MM/YYYY"},
                "filename": {"type": "STRING", "description": "Attachment name or extension, e.g. pdf"},
                "label": {"type": "STRING", "description": "Gmail label to search"},
                "scope": {"type": "STRING", "description": "inbox | sent | drafts | trash | spam | all"},
                "has_attachment": {"type": "BOOLEAN", "description": "Only messages with attachments"},
                "unread": {"type": "BOOLEAN", "description": "true for unread, false for read"},
                "starred": {"type": "BOOLEAN", "description": "Only starred messages"},
                "important": {"type": "BOOLEAN", "description": "Only important messages"},
                "larger_than": {"type": "STRING", "description": "Minimum Gmail size, e.g. 10M"},
                "smaller_than": {"type": "STRING", "description": "Maximum Gmail size, e.g. 2M"},
                "include_spam_trash": {"type": "BOOLEAN", "description": "Include spam and trash in the search"},
                "client_secret_path": {"type": "STRING", "description": "Optional path to a downloaded Google Desktop OAuth client JSON, only for action='connect'"},
            },
            "required": [],
        },
    },
    {
        "name": "github_control",
        "description": (
            "Contrôle GitHub et Git local. Pour « connecte GitHub », appelle connect : "
            "OAuth s'ouvre exclusivement dans Google Chrome. commit_push initialise Git, "
            "crée un dépôt privé si origin manque, analyse les secrets, commit puis push. "
            "N'utilise jamais shell_exec pour GitHub."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "status | connect | disconnect | list | clone | init | create_repo | project_status | commit | push | commit_push | pull (fetch + rebase sûr, modifications locales mises de côté) | log | changes | issues | prs | create_issue | backup_enable | backup_disable"},
                "url": {"type": "STRING", "description": "clone : URL ou owner/repo"},
                "title": {"type": "STRING", "description": "create_issue : titre"},
                "body": {"type": "STRING", "description": "create_issue : description"},
                "state": {"type": "STRING", "description": "issues/prs : open (défaut), closed ou all"},
                "count": {"type": "INTEGER", "description": "log : nombre de commits (défaut 10)"},
                "project": {"type": "STRING", "description": "Nom ou chemin du projet local"},
                "path": {"type": "STRING", "description": "Chemin local explicite"},
                "repo_name": {"type": "STRING", "description": "Nom du dépôt ; défaut dossier"},
                "private": {"type": "BOOLEAN", "description": "Dépôt privé ; vrai par défaut"},
                "message": {"type": "STRING", "description": "Message de commit facultatif"},
                "branch": {"type": "STRING", "description": "Branche cible ; défaut branche courante"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "computer_settings",
        "description": (
            "Controls the computer: volume, brightness, window management, keyboard shortcuts, "
            "typing text on screen, fullscreen, dark mode, WiFi, restart, shutdown, "
            "scrolling, tab management, zoom, screenshots, lock screen, refresh/reload page. "
            "Utiliser pour « augmente/diminue le volume » sans média précisé : cela règle réellement "
            "le volume système. Si l'utilisateur mentionne musique, Spotify, vidéo ou YouTube, utiliser "
            "le contrôleur média correspondant à la place. "
            "Use for ANY single computer control command. Do NOT use this to close, quit, or kill "
            "an application — always use the dedicated close_app tool for that instead."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": (
                        "volume_set | volume_up | volume_down | mute | unmute | toggle_mute | "
                        "brightness_up | brightness_down | sleep_display | pause_video | "
                        "close_window | fullscreen | minimize | maximize | "
                        "snap_left | snap_right | switch_window | show_desktop | task_manager | "
                        "focus_search | refresh_page | close_tab | new_tab | next_tab | prev_tab | "
                        "go_back | go_forward | zoom_in | zoom_out | zoom_reset | find_on_page | "
                        "scroll_up | scroll_down | scroll_top | scroll_bottom | page_up | page_down | "
                        "copy | paste | cut | undo | redo | select_all | save | enter | escape | "
                        "screenshot | lock_screen | open_settings | file_explorer | open_run | "
                        "dark_mode | toggle_wifi | wifi_status | toggle_bluetooth | bluetooth_status | "
                        "airplane_mode | mic_toggle | power_profile | restart | shutdown | suspend | "
                        "type_text | press_key | reload_n. For volume_set, pass value as an integer "
                        "0-100 (e.g. 10 for 10%). For power_profile, value is performance | balanced | "
                        "power-saver (omit value to read the current profile)."
                    )
                },
                "description": {"type": "STRING", "description": "Natural language description of what to do"},
                "value":       {"type": "STRING", "description": "Optional value: volume level (0-100), text to type, key name, etc."}
            },
            "required": []
        }
    },
    {
        "name": "browser_control",
        "description": (
            "Controls Google Chrome, the user’s permanent browser choice. Never launch Firefox. Use for: opening websites, "
            "clicking elements, filling forms, scrolling, navigation, any web-based task. "
            "For a QUESTION whose answer is information, use web_search instead — this tool is "
            "for when the user wants a browser window opened or driven. "
            "Its 'screenshot' action captures the web page only; to capture the user's screen, "
            "use capture_control. "
            "Simple open/search requests launch the user's own browser normally (their real profile "
            "and logged-in accounts); interactive actions (click, type, fill_form...) attach an "
            "automation browser. "
            "Always pass browser='chrome'. Do not reuse another active browser or fall back to Firefox."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "go_to | search | click | type | scroll | fill_form | smart_click | smart_type | get_text | get_url | press | new_tab | close_tab | screenshot | back | forward | reload | switch | list_browsers | close | close_all"},
                "browser":     {"type": "STRING", "description": "Always use chrome, the user’s permanent browser choice."},
                "url":         {"type": "STRING", "description": "URL for go_to / new_tab action"},
                "query":       {"type": "STRING", "description": "Search query for search action"},
                "engine":      {"type": "STRING", "description": "Search engine: google | bing | duckduckgo | yandex (default: google)"},
                "selector":    {"type": "STRING", "description": "CSS selector for click/type"},
                "text":        {"type": "STRING", "description": "Text to click or type"},
                "description": {"type": "STRING", "description": "Element description for smart_click/smart_type"},
                "direction":   {"type": "STRING", "description": "up | down for scroll"},
                "amount":      {"type": "INTEGER", "description": "Scroll amount in pixels (default: 500)"},
                "key":         {"type": "STRING", "description": "Key name for press action (e.g. Enter, Escape, F5)"},
                "path":        {"type": "STRING", "description": "Save path for screenshot"},
                "incognito":   {"type": "BOOLEAN", "description": "Open in private/incognito mode"},
                "clear_first": {"type": "BOOLEAN", "description": "Clear field before typing (default: true)"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "file_controller",
        "description": (
            "Gestionnaire de fichiers dédié et rapide. OBLIGATOIRE pour toute question "
            "comme 'ai-je un fichier nommé X ?', 'cherche/trouve le fichier X', "
            "'où est X ?' ou toute recherche de fichier/dossier sur le disque. Pour ces "
            "demandes utiliser action='find', name=les mots EXACTEMENT entendus, path='home'. "
            "Si l'utilisateur cherche une vidéo locale par son nom, passer kind='video' "
            "et ne garder dans name que les mots du nom recherché. "
            "La recherche utilise l'index disque et tolère fautes vocales, accents et noms "
            "approximatifs. Ne jamais traduire/corriger arbitrairement le nom et ne jamais "
            "utiliser shell_exec/find/fd/locate à la place. Gère aussi list, create, delete, "
            "move, copy, rename, read, write, info et disk usage."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "list | create_file | create_folder | delete | move | copy | rename | read | write | find | largest | disk_usage | organize_desktop | info"},
                "path":        {"type": "STRING", "description": "File/folder path or shortcut: desktop, downloads, documents, home"},
                "destination": {"type": "STRING", "description": "Destination path for move/copy"},
                "new_name":    {"type": "STRING", "description": "New name for rename"},
                "content":     {"type": "STRING", "description": "Content for create_file/write"},
                "name":        {"type": "STRING", "description": "File name to search for"},
                "extension":   {"type": "STRING", "description": "File extension to search (e.g. .pdf)"},
                "kind":        {"type": "STRING", "description": "Optional file type filter: video | audio | image"},
                "count":       {"type": "INTEGER", "description": "Number of results for largest"},
                "max_results": {"type": "INTEGER", "description": "Nombre maximal de résultats, défaut 20"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "desktop_control",
        "description": "Controls the desktop safely: wallpaper, reversible organization/cleaning, restore, list and stats.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "wallpaper | wallpaper_url | current_wallpaper | random_wallpaper | organize | preview | clean | restore | list | stats"},
                "path":   {"type": "STRING", "description": "Image path for wallpaper"},
                "url":    {"type": "STRING", "description": "Image URL for wallpaper_url"},
                "mode":   {"type": "STRING", "description": "by_type or by_date for organize"},
                "archive": {"type": "STRING", "description": "Archive Bureau folder to restore; defaults to the latest"},
                "dry_run": {"type": "BOOLEAN", "description": "Preview file moves without changing anything"},
                "task":   {"type": "STRING", "description": "Natural language desktop task"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "live_auto_debug",
        "description": (
            "Performs forensic live debugging with GPT-5.6 Terra: exact error parsing, source correlation, "
            "evidence, confidence, safe verification commands and an optional validated unified patch. "
            "MUST be called when the user asks: 'C'est quoi ce bug dans mon terminal ?', 'Debug cette erreur', "
            "'Pourquoi mon code/build plante ?', 'Analyse ce traceback/panic', 'Aide-moi à corriger cette erreur', "
            "or points to any broken command, test failure or compiler output on screen. "
            "Captures the active terminal/IDE window, parses the exact error stack trace, links local source code, "
            "displays a rich debug card and explains the fix directly."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {"type": "STRING", "description": "The user's question or instruction about the error/bug"},
                "input_text": {"type": "STRING", "description": "Exact traceback, compiler output or log if the user provided it"},
                "target": {"type": "STRING", "description": "'active_window' | 'screen' (default: 'active_window')"},
                "auto_apply": {"type": "BOOLEAN", "description": "Apply only an explicitly requested unified patch after dry-run and automatic .bak backup"}
            },
            "required": []
        }
    },
    {
        "name": "code_helper",
        "description": "Writes, edits, explains, runs, or builds code files.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "write | edit | explain | run | build | optimize | screen_debug | auto (default: auto)"},
                "description": {"type": "STRING", "description": "What the code should do or what change to make"},
                "language":    {"type": "STRING", "description": "Programming language (default: python)"},
                "output_path": {"type": "STRING", "description": "Where to save the file"},
                "file_path":   {"type": "STRING", "description": "Path to existing file for edit/explain/run/build"},
                "code":        {"type": "STRING", "description": "Raw code string for explain"},
                "args":        {"type": "STRING", "description": "CLI arguments for run/build"},
                "timeout":     {"type": "INTEGER", "description": "Execution timeout in seconds (default: 30)"},
                "apply_fix":   {"type": "BOOLEAN", "description": "For screen_debug only: explicitly allow replacing a file after validation and backup"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "dev_agent",
        "description": "Builds complete multi-file projects from scratch: plans, writes files, installs deps, opens VSCode, runs and fixes errors.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "description":  {"type": "STRING", "description": "What the project should do"},
                "language":     {"type": "STRING", "description": "Programming language (default: python)"},
                "project_name": {"type": "STRING", "description": "Optional project folder name"},
                "timeout":      {"type": "INTEGER", "description": "Run timeout in seconds (default: 30)"},
            },
            "required": ["description"]
        }
    },
    {
        "name": "computer_control",
        "description": "Direct, safe and low-latency computer control: type in windows, click, hotkeys, scroll, cursor, visual targeting, window/workspace management, system snapshots, private clipboard status and PipeWire volume.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {
                    "type": "STRING",
                    "description": (
                        "type | smart_type | click | double_click | right_click | hotkey | press | "
                        "scroll | move | copy | paste | wait | clear_field | "
                        "focus_window | fullscreen | float | center | close | "
                        "move_to_workspace | switch_workspace | list_windows | "
                        "screen_find | screen_click | random_data | user_data | "
                        "system_status | workspace_overview | clipboard_status | "
                        "volume_get | volume_set | volume_mute. "
                        "Pour une capture d'écran destinée à l'utilisateur, utiliser capture_control, "
                        "jamais cet outil. "
                        "type types text into active window, or target 'window' if provided. "
                        "fullscreen toggles fullscreen mode on active or target window. "
                        "float toggles floating window mode. "
                        "center centers the active window. "
                        "close closes a window. "
                        "move moves the mouse cursor to exact (x, y) coordinates. "
                        "focus_window brings a window matching 'title' or 'window' to the front. "
                        "switch_workspace switches only the currently visible workspace; it must be used for "
                        "‘va/navigue au bureau N’ and must never move a window. "
                        "move_to_workspace moves a window to workspace given in 'workspace' and is allowed "
                        "only when the user explicitly says to move/send a window. "
                        "list_windows lists all open windows with their workspace number. "
                        "system_status is a lightweight local CPU/RAM/focus snapshot. "
                        "clipboard_status never exposes clipboard contents. "
                        "volume_set accepts a safe 0-100 percentage."
                    )
                },
                "text":        {"type": "STRING", "description": "Text to type or paste"},
                "window":      {"type": "STRING", "description": "Target window title or class to focus before typing or controlling"},
                "press_enter": {"type": "BOOLEAN", "description": "Whether to press Enter after typing text (default false, true for terminal commands)"},
                "x":           {"type": "INTEGER", "description": "X coordinate for move or click"},
                "y":           {"type": "INTEGER", "description": "Y coordinate for move or click"},
                "keys":        {"type": "STRING", "description": "Key combination e.g. 'ctrl+c'"},
                "key":         {"type": "STRING", "description": "Single key e.g. 'enter'"},
                "direction":   {"type": "STRING", "description": "up | down | left | right"},
                "amount":      {"type": "INTEGER", "description": "Scroll amount (default: 3)"},
                "seconds":     {"type": "NUMBER",  "description": "Seconds to wait"},
                "title":       {"type": "STRING",  "description": "Window title for focus_window / move_to_workspace / close (partial match)"},
                "workspace":   {"type": "INTEGER", "description": "Target workspace/bureau number for move_to_workspace / switch_workspace"},
                "value":       {"type": "INTEGER", "description": "Brightness or volume percentage, 0 to 100"},
                "mode":        {"type": "STRING", "description": "For volume_mute: toggle | on | off"},
                "description": {"type": "STRING",  "description": "Element description for screen_find/screen_click"},
                "type":        {"type": "STRING",  "description": "Data type for random_data"},
                "field":       {"type": "STRING",  "description": "Field for user_data: name|email|city"},
                "clear_first": {"type": "BOOLEAN", "description": "Clear field before typing (default: true)"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "game_updater",
        "description": (
            "THE ONLY tool for ANY Steam or Epic Games request. "
            "Use for: installing, downloading, updating games, listing installed games, "
            "checking download status, scheduling updates. "
            "ALWAYS call directly for any Steam/Epic/game request. "
            "NEVER use browser_control or web_search for Steam/Epic."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":    {"type": "STRING",  "description": "update | install | list | download_status | schedule | cancel_schedule | schedule_status (default: update)"},
                "platform":  {"type": "STRING",  "description": "steam | epic | both (default: both)"},
                "game_name": {"type": "STRING",  "description": "Game name (partial match supported)"},
                "app_id":    {"type": "STRING",  "description": "Steam AppID for install (optional)"},
                "hour":      {"type": "INTEGER", "description": "Hour for scheduled update 0-23 (default: 3)"},
                "minute":    {"type": "INTEGER", "description": "Minute for scheduled update 0-59 (default: 0)"},
                "shutdown_when_done": {"type": "BOOLEAN", "description": "Shut down PC when download finishes"},
            },
            "required": []
        }
    },
    {
        "name": "flight_finder",
        "description": "Searches Google Flights and speaks the best options.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "origin":      {"type": "STRING",  "description": "Departure city or airport code"},
                "destination": {"type": "STRING",  "description": "Arrival city or airport code"},
                "date":        {"type": "STRING",  "description": "Departure date (any format)"},
                "return_date": {"type": "STRING",  "description": "Return date for round trips"},
                "passengers":  {"type": "INTEGER", "description": "Number of passengers (default: 1)"},
                "cabin":       {"type": "STRING",  "description": "economy | premium | business | first"},
                "save":        {"type": "BOOLEAN", "description": "Save results to Notepad"},
            },
            "required": ["origin", "destination", "date"]
        }
    },
    {
        "name": "shutdown_jarvis",
        "description": (
            "Shuts down the assistant completely. "
            "Call this when the user expresses intent to end the conversation, "
            "close the assistant, say goodbye, or stop Jarvis. "
            "The user can say this in ANY language."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
    "name": "file_processor",
    "description": (
        "Processes any file that the user has uploaded or dropped onto the interface. "
        "Use this when the user refers to an uploaded file and wants an action on it. "
        "Supports: images (describe/ocr/resize/compress/convert), "
        "PDFs (summarize/extract_text/to_word), "
        "Word docs & text files (summarize/fix/reformat/translate), "
        "CSV/Excel (analyze/stats/filter/sort/convert), "
        "JSON/XML (validate/format/analyze), "
        "code files (explain/review/fix/optimize/run/document/test), "
        "audio (transcribe/trim/convert/info), "
        "video (trim/extract_audio/extract_frame/compress/transcribe/info), "
        "archives (list/extract), "
        "presentations (summarize/extract_text). "
        "ALWAYS call this tool when a file has been uploaded and the user gives a command about it. "
        "If the user's command is ambiguous, pick the most logical action for that file type."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "file_path": {
                "type": "STRING",
                "description": "Full path to the uploaded file. Leave empty to use the currently uploaded file."
            },
            "action": {
                "type": "STRING",
                "description": (
                    "What to do with the file. Examples by type:\n"
                    "image: describe | ocr | resize | compress | convert | info\n"
                    "pdf: summarize | extract_text | to_word | info\n"
                    "docx/txt: summarize | fix | reformat | translate_hint | word_count | to_bullet\n"
                    "csv/excel: analyze | stats | filter | sort | convert | info\n"
                    "json: validate | format | analyze | to_csv\n"
                    "code: explain | review | fix | optimize | run | document | test\n"
                    "audio: transcribe | trim | convert | info\n"
                    "video: trim | extract_audio | extract_frame | compress | transcribe | info | convert\n"
                    "archive: list | extract\n"
                    "pptx: summarize | extract_text | analyze"
                )
            },
            "instruction": {
                "type": "STRING",
                "description": "Free-form instruction if action doesn't cover it. E.g. 'translate this to Turkish', 'find all email addresses'"
            },
            "format": {
                "type": "STRING",
                "description": "Target format for conversion. E.g. 'mp3', 'pdf', 'csv', 'png'"
            },
            "width":     {"type": "INTEGER", "description": "Target width for image resize"},
            "height":    {"type": "INTEGER", "description": "Target height for image resize"},
            "scale":     {"type": "NUMBER",  "description": "Scale factor for image resize (e.g. 0.5)"},
            "quality":   {"type": "INTEGER", "description": "Quality 1-100 for image/video compress"},
            "start":     {"type": "STRING",  "description": "Start time for trim: seconds or HH:MM:SS"},
            "end":       {"type": "STRING",  "description": "End time for trim: seconds or HH:MM:SS"},
            "timestamp": {"type": "STRING",  "description": "Timestamp for video frame extraction HH:MM:SS"},
            "column":    {"type": "STRING",  "description": "Column name for CSV filter/sort"},
            "value":     {"type": "STRING",  "description": "Filter value for CSV filter"},
            "condition": {"type": "STRING",  "description": "Filter condition: equals|contains|gt|lt"},
            "ascending": {"type": "BOOLEAN", "description": "Sort order for CSV sort (default: true)"},
            "save":      {"type": "BOOLEAN", "description": "Save result to file (default: true)"},
            "destination": {"type": "STRING", "description": "Output folder for archive extract"},
        },
        "required": []
    }
},
    {
        "name": "media_control",
        "description": (
            "🎬 CONTRÔLE MÉDIA ULTRA-PUISSANT — Pause/Play/Volume YouTube, contrôle Chrome, volume système. "
            "Actions: youtube_pause, youtube_play, youtube_volume(0-100), youtube_seek(seconds), "
            "youtube_fullscreen, youtube_subtitles, youtube_theater, youtube_next, youtube_previous, "
            "youtube_back_10s, youtube_forward_10s, youtube_speed(0.5-2.0), "
            "volume(0-100), volume_up, volume_down, mute, "
            "chrome_new_tab, chrome_close_tab, chrome_reload, chrome_search(query), "
            "chrome_zoom_in, chrome_zoom_out, chrome_zoom_reset, chrome_back, chrome_forward. "
            "UTILISER POUR: pause YouTube, play, volume, seek, Chrome, volume système. "
            "C'est l'outil LE PLUS RAPIDE pour les actions média — priorité absolue!"
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": "Action à effectuer: youtube_pause | youtube_play | youtube_volume | youtube_seek | youtube_fullscreen | youtube_subtitles | youtube_theater | youtube_next | youtube_previous | youtube_back_10s | youtube_forward_10s | youtube_speed | volume | volume_up | volume_down | mute | chrome_new_tab | chrome_close_tab | chrome_reload | chrome_search | chrome_zoom_in | chrome_zoom_out | chrome_zoom_reset | chrome_back | chrome_forward"
                },
                "value": {"type": "STRING", "description": "Valeur optionnelle: niveau de volume (0-100), nombre de secondes, vitesse, requête de recherche"}
            },
            "required": ["action"]
        }
    },
    {
        "name": "shell_exec",
        "description": (
            "Exécute des commandes shell/bash arbitraires. "
            "Utiliser pour: toute commande terminal, scripts bash, pacman/yay/pip/npm, "
            "git, docker, opérations fichiers avancées, surveillance processus, "
            "installation de paquets. C'est l'outil de repli pour tout ce qui n'a pas "
            "d'outil dédié.\n"
            "NE JAMAIS UTILISER pour rechercher un fichier ou répondre à 'ai-je un fichier X' : "
            "utiliser file_controller action='find'. Interdiction d'exécuter find/fd/locate/rg "
            "pour une demande utilisateur de recherche de fichiers. "
            "NE PAS UTILISER quand un outil dédié existe — ces outils vérifient l'état réel "
            "du système, ce qu'une commande brute ne fait pas :\n"
            "- ouvrir une application → open_app\n"
            "- fermer une application → close_app (jamais pkill/killall)\n"
            "- capture d'écran ou enregistrement vidéo → capture_control (jamais grim/slurp)\n"
            "- fenêtres, workspaces, focus, plein écran → computer_control\n"
            "- saisie de texte, presse-papiers → computer_control (jamais wtype/wl-copy)\n"
            "- lecture audio/musique locale → music_control\n"
            "- télécharger un morceau YouTube dans ~/Musique → download_music "
            "(jamais yt-dlp à la main)\n"
            "- volume de la musique, Spotify ou du morceau en cours → music_control action='volume' "
            "(jamais amixer, pactl ou wpctl : ce sont des volumes système)\n"
            "- toute recherche, lecture ou commande YouTube → youtube_video\n"
            "Fournir soit 'command' (commande bash directe) soit 'description' (langage naturel). "
            "SÉCURITÉ : pour une commande risquée (sudo, rm -rf, systemctl stop/restart, git reset --hard, "
            "désinstallation de paquets, etc.), appelle quand même cet outil : il affiche lui-même une carte "
            "Confirmer/Annuler sûre dans ANO-GPT. Ne refuse pas à la place de l'outil et ne rappelle jamais "
            "l'outil avec 'confirm': true ; seul le clic humain valide réellement l'exécution."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "command":     {"type": "STRING", "description": "Commande bash/shell directe à exécuter (ex: 'ls -la ~/Documents', 'pacman -Q | grep firefox', 'systemctl status ollama')"},
                "description": {"type": "STRING", "description": "Description en langage naturel de l'action à effectuer (ex: 'liste les processus python', 'quelle est la taille du dossier Téléchargements', 'mets à jour les paquets')"},
                "cwd":         {"type": "STRING", "description": "Répertoire de travail (défaut: home de l'utilisateur)"},
                "timeout":     {"type": "INTEGER", "description": "Délai maximum en secondes (défaut: 20)"},
                "confirm":     {"type": "BOOLEAN", "description": "true uniquement quand l'utilisateur vient de confirmer une commande risquée signalée au tour précédent"},
            },
            "required": []
        }
    },
    {
        "name": "hypr_control",
        "description": (
            "Contrôle natif du bureau Hyprland/Wayland. Utiliser pour: changer de workspace, "
            "déplacer des fenêtres, mettre en plein écran, mode flottant, lister les fenêtres ouvertes, "
            "saisir du texte dans n'importe quelle fenêtre (wtype), envoyer des raccourcis clavier, "
            "gérer le presse-papiers (wl-copy/wl-paste), prendre des captures d'écran (grim), "
            "donner le focus à une application. Plus précis que shell_exec pour les opérations Hyprland. "
            "Règle impérative : action='workspace' navigue seulement (ne déplace jamais une fenêtre) ; "
            "action='move_to_workspace' exige une demande explicite de déplacer/envoyer une fenêtre."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": (
                        "workspace | move_to_workspace | focus_app | close_active | fullscreen | "
                        "float | list_windows | type | keys | clipboard_set | clipboard_get | "
                        "screenshot"
                    )
                },
                "value": {"type": "STRING", "description": "Valeur associée: numéro workspace, nom app, texte à saisir, raccourci clavier, chemin screenshot"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "devsecops",
        "description": (
            "Pilotage complet DevSecOps et Maître du Système Linux. Utiliser pour: "
            "1. Docker & Stacks: redémarrer/lancer/arrêter une stack Docker Compose (action='restart_stack'), "
            "purger les conteneurs morts et images inutilisées (action='purge_dead'), inspecter les logs d'accès HTTP "
            "ou d'erreur (action='logs', access_logs=True), lister les conteneurs (action='list').\n"
            "2. Systemd & Journalctl: vérifier l'état ou redémarrer un service (action='status'|'restart', target='service'), "
            "lister et diagnostiquer les services en échec (action='list_failed'|'diagnose').\n"
            "3. Paquets & Mises à jour: vérifier les mises à jour (action='check_updates'), chercher des paquets (action='search'), "
            "nettoyer les paquets orphelins (action='clean_orphans').\n"
            "4. Git Intelligent: commits conventionnels vocaux propres (action='commit', message='description'), rebase avec auto-stash (action='rebase'), "
            "statut synthétique (action='status') avec scan pré-commit anti-fuite de secrets (clés API/tokens).\n"
            "5. Sécurité Linux: audit des ports ouverts et de la sécurité système (action='audit')."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "domain": {"type": "STRING", "description": "Domaine d'action: docker | systemd | package | git | security"},
                "action": {"type": "STRING", "description": "Action: restart_stack | purge_dead | logs | list | status | restart | list_failed | diagnose | check_updates | search | clean_orphans | commit | rebase | audit"},
                "target": {"type": "STRING", "description": "Cible: nom de service, conteneur, paquet, branche, etc."},
                "message": {"type": "STRING", "description": "Message de commit pour Git ou instruction vocale"},
                "access_logs": {"type": "BOOLEAN", "description": "true pour filtrer spécifiquement les logs d'accès HTTP"},
                "error_logs": {"type": "BOOLEAN", "description": "true pour filtrer spécifiquement les erreurs / crashs"},
                "description": {"type": "STRING", "description": "Description en langage naturel de la demande DevSecOps"},
            },
            "required": []
        }
    },
    {
        "name": "hypr_orchestrator",
        "description": (
            "Orchestrateur dynamique de fenêtres et d'espaces de travail Hyprland. Utiliser pour: "
            "reclasser automatiquement toutes les fenêtres ouvertes sur leurs workspaces dédiés selon leur rôle "
            "(1: Code/IDE, 2: Web/Docs, 3: Terminal/DevSecOps, 4: Comms, 5: Média, 6: Monitoring), "
            "appliquer des presets de travail (preset='devsecops' | 'coding' | 'monitoring' | 'web'), "
            "ou déplacer dynamiquement une fenêtre spécifique vers son bureau dédié."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "Action: organize | preset | move_window | list"},
                "preset": {"type": "STRING", "description": "Nom du preset: devsecops | coding | monitoring | web | comms"},
                "target": {"type": "STRING", "description": "Nom ou classe de l'application à déplacer"},
                "workspace": {"type": "STRING", "description": "Numéro ou nom du workspace cible (1 à 6, 'dev', 'web', etc.)"},
                "description": {"type": "STRING", "description": "Description en langage naturel (ex: 'organise mon espace de travail', 'preset devsecops')"},
            },
            "required": []
        }
    },
    {
        "name": "self_repair",
        "description": (
            "Tes propres erreurs et leur réparation automatique. "
            "action='repair' OBLIGATOIRE dès que l'utilisateur dit « corrige », « répare », "
            "« corrige ça », « corrige l'erreur », « répare-toi » : l'erreur la plus récente "
            "(pile d'appel enregistrée) est confiée à un agent de code qui modifie le fichier "
            "fautif, lance les tests et commite ; ça tourne EN FOND, tu rends la phrase renvoyée "
            "et l'annonce du résultat arrive toute seule (ne relance pas l'outil). "
            "action='last_error' pour « c'était quoi l'erreur ? », « qu'est-ce qui a planté ? ». "
            "action='restart' pour « redémarre », « redémarre-toi », « applique le correctif ». "
            "action='diagnose' pour le bilan de santé des outils (« est-ce que tout va bien ? »). "
            "N'invente jamais un diagnostic : appelle cet outil et rapporte ce qu'il répond."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "repair | last_error | restart | diagnose"},
                "tool": {"type": "STRING", "description": "Outil ou erreur visé si l'utilisateur en nomme un (ex. météo, tiktok)"},
            },
            "required": [],
        }
    },
    {
        "name": "voice_id",
        "description": (
            "Empreinte vocale de l'utilisateur. action='enroll' quand il demande "
            "de retenir/apprendre sa voix (« apprends ma voix », « reconnais-moi ») "
            "— l'empreinte est calculée sur ce qu'il vient de dire, il n'y a rien "
            "à enregistrer de plus. action='status' pour savoir qui parle, "
            "action='forget' pour tout effacer. Ne l'appelle jamais de toi-même : "
            "seulement s'il le demande."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "enroll | status | forget"},
                "name": {"type": "STRING", "description": "Prénom associé à la voix (défaut : Anonymous)"},
            },
            "required": [],
        }
    },
    {
        "name": "voice_style",
        "description": (
            "Choisit durablement le style d'élocution de l'assistant. Utilise-le "
            "quand l'utilisateur demande un ton professionnel, Tony Stark / "
            "sarcastique, ou ultra-synthétique. Sans style, retourne la préférence "
            "actuelle. L'adaptation acoustique urgence/fatigue reste prioritaire."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "style": {
                    "type": "STRING",
                    "description": "professional | stark | synthetic",
                },
            },
            "required": [],
        },
    },
    {
        "name": "routine",
        "description": (
            "Exécute une routine : un enchaînement d'actions défini par "
            "l'utilisateur lui-même dans config/routines.yaml (« mode travail », "
            "« mode nuit », « je pars »…). Appelle cet outil dès que l'utilisateur "
            "prononce le nom d'une routine, au lieu de refaire les actions une par "
            "une : l'ordre et le contenu des étapes lui appartiennent. Sans nom, "
            "l'outil rend la liste des routines existantes."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "name": {
                    "type": "STRING",
                    "description": "Nom ou phrase de la routine (ex. 'mode travail')",
                },
            },
            "required": [],
        }
    },
    {
        "name": "save_memory",
        "description": (
            "Save an important personal fact about the user to long-term memory. "
            "Call this silently whenever the user reveals something worth remembering: "
            "name, age, city, job, preferences, hobbies, relationships, projects, or future plans. "
            "Do NOT call for: weather, reminders, searches, or one-time commands. "
            "Do NOT announce that you are saving — just call it silently. "
            "Values must be in English regardless of the conversation language."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "category": {
                    "type": "STRING",
                    "description": (
                        "identity — name, age, birthday, city, job, language, nationality | "
                        "preferences — favorite food/color/music/film/game/sport, hobbies | "
                        "projects — active projects, goals, things being built | "
                        "relationships — friends, family, partner, colleagues | "
                        "wishes — future plans, things to buy, travel dreams | "
                        "notes — habits, schedule, anything else worth remembering"
                    )
                },
                "key":   {"type": "STRING", "description": "Short snake_case key (e.g. name, favorite_food, sister_name)"},
                "value": {"type": "STRING", "description": "Concise value in English (e.g. Fatih, pizza, older sister)"},
            },
            "required": ["category", "key", "value"]
        }
    },
    {
        "name": "second_brain",
        "description": (
            "Recherche associative universelle dans l'historique local : conversations, "
            "fichiers et notes, commandes terminal, contacts, projets, dates, e-mails "
            "déjà consultés et souvenirs. Utilise TOUJOURS cet outil pour une demande "
            "comme « qu'avait-on utilisé », « le mois dernier », « qui était le contact "
            "du projet X » ou toute information passée pouvant relier plusieurs sources. "
            "action=search recherche, status donne l'état, reindex resynchronise les sources."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "search | status | reindex"},
                "query": {"type": "STRING", "description": "Question ou association à retrouver"},
                "limit": {"type": "INTEGER", "description": "Nombre de résultats, 1 à 20"},
            },
            "required": [],
        },
    },
    {
        "name": "deep_think",
        "description": (
            "Delegate a hard question to the user's coding agent (Antigravity), a much "
            "stronger reasoning model that can also search and read files, and get an "
            "answer back to speak aloud. Use it when the request needs real thinking "
            "rather than an action: analysis, comparison, planning, research, explaining "
            "a concept, writing or debugging code, choosing between options, estimating, "
            "or any 'why / how / what would happen if' question. "
            "Do NOT use it for things you can simply do: launching an app, running a "
            "shell command, playing music, taking a screenshot — act instead. "
            "Do NOT use it for small talk or simple factual chat, which you answer yourself. "
            "The call takes a few seconds, so say a short filler sentence first "
            "('Je réfléchis un instant…') before calling it."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "question": {
                    "type": "STRING",
                    "description": "The question to think about, self-contained and in French",
                },
                "context": {
                    "type": "STRING",
                    "description": (
                        "Useful facts already known: command output, file contents, "
                        "earlier conversation details. Optional but improves the answer."
                    ),
                },
            },
            "required": ["question"],
        },
    },
    {
        "name": "simulate_decision",
        "description": (
            "Lance une simulation stratégique what-if asynchrone. Utilise-la quand "
            "l'utilisateur dit « simule ma décision », « compare ces options » ou "
            "demande de choisir entre plusieurs scénarios importants. Elle consulte "
            "le Second Brain, recherche des données web récentes, confronte un avocat "
            "par option puis un arbitre, et archive le résultat."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "decision": {"type": "STRING", "description": "Décision complète à simuler."},
                "options": {"type": "ARRAY", "items": {"type": "STRING"}, "description": "Au moins deux options explicites."},
            },
            "required": ["decision"],
        },
    },
    {
        "name": "capture_control",
        "description": (
            "Screenshots and screen recording on Wayland/Hyprland. This is the ONLY correct "
            "tool for capturing the screen or recording video — never use shell_exec, "
            "computer_control or browser_control for this. "
            "Use action='screenshot' for a full-screen capture, 'region' to let the user "
            "drag-select an area, 'window' for the focused window, 'start_recording' to begin "
            "a screen recording and 'stop_recording' to finish it and save the video file. "
            "Full screenshots use the installed Caelestia Shell capture backend (its configured "
            "folder, notification and clipboard behavior); "
            "recordings are saved to ~/Vidéos/Enregistrements."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": "screenshot | region | window | start_recording | stop_recording | status | monitors",
                },
                "path": {"type": "STRING", "description": "Optional output file path"},
                "fps": {"type": "INTEGER", "description": "Recording frame rate (default 30)"},
                "audio": {
                    "type": "STRING",
                    "description": "Recording audio: 'system' (default), 'mic', 'both', or 'none'",
                },
                "quality": {"type": "STRING", "description": "medium (default) | high | ultra"},
                "delay": {"type": "NUMBER", "description": "Seconds to wait before the screenshot"},
                "annotate": {
                    "type": "BOOLEAN",
                    "description": "Open the screenshot in swappy for annotation",
                },
                "freeze": {
                    "type": "BOOLEAN",
                    "description": "Freeze the screen while opening Caelestia's region picker",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "music_control",
        "description": (
            "Recherche et lit la musique, les morceaux audio et les vidéos (en local ou en ligne). "
            "Recherche d'abord dans la bibliothèque locale. Si le morceau n'y est pas, demande "
            "toujours si l'utilisateur préfère Spotify ou YouTube ; ne choisis jamais la source à sa place. "
            "Pour une musique, lecture audio en arrière-plan avec la carte HUD interactive (titre, artiste, progression, contrôles). "
            "Pour une vidéo locale ou clip, passer kind='video' : affichage direct dans le lecteur vidéo intégré. "
            "Handles playback control: pause, resume, next, previous, stop, now_playing, seek, volume, shuffle. "
            "Pour toute demande de volume de la musique, Spotify ou du morceau en cours, utiliser "
            "action='volume' et value='+10', '-10' ou '50'. Ne jamais utiliser shell_exec, amixer, "
            "pactl ou wpctl : ils modifient le volume de tout le système. "
            "Ne jamais utiliser cet outil pour télécharger ou enregistrer un fichier : "
            "utiliser download_music."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": "play (default) | search | select | pause | resume | next | previous | stop | now_playing | list_players | seek | volume | shuffle",
                },
                "query": {"type": "STRING", "description": "Titre, artiste, album ou nom de vidéo locale, même approximatif"},
                "kind": {"type": "STRING", "description": "audio (défaut) | video pour un fichier vidéo local"},
                "index": {"type": "INTEGER", "description": "Numéro d'un résultat local précédemment affiché"},
                "result": {"type": "STRING", "description": "Numéro ou partie du titre local à sélectionner"},
                "player": {
                    "type": "STRING",
                    "description": (
                        "LEAVE EMPTY in almost every case. Only set this when the user "
                        "NAMES a player out loud ('joue ça dans VLC'). Setting it opens a "
                        "separate player window and DISABLES the in-app player card with "
                        "its controls — which is not what the user wants by default. "
                        "Accepted values: 'vlc', 'mpv', 'audacious', 'lollypop', 'google-chrome-stable'."
                    ),
                },
                "source": {"type": "STRING", "description": "auto (default) | local | spotify | youtube"},
                "confirm": {
                    "type": "BOOLEAN",
                    "description": "Set true when the user just confirmed the YouTube search this tool asked to do last turn",
                },
                "value": {
                    "type": "STRING",
                    "description": "Pour volume : '50', '+10' ou '-10'; pour seek : position.",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "download_music",
        "description": (
            "Télécharge le meilleur morceau YouTube correspondant dans le dossier Musique. "
            "OBLIGATOIRE dès que l'utilisateur dit « télécharge [titre] », « download [musique] », "
            "« récupère [chanson] en mp3/m4a ». Ne jamais passer par music_control, youtube_video, "
            "shell_exec ou yt-dlp : cet outil choisit la version officielle (pas un cover, un live "
            "ni un mix d'une heure), extrait l'audio en meilleure qualité (m4a) et affiche une carte "
            "de progression. Il lance le transfert EN FOND et rend tout de suite une phrase à dire "
            "(patienter, ce n'est pas encore fini). L'utilisateur peut continuer à parler. "
            "Une annonce arrive toute seule à la fin : ne relance pas l'outil, ne dis pas que "
            "c'est déjà téléchargé, ne cherche pas le fichier pendant le transfert. "
            "Transmettre le titre entendu tel quel dans query."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {
                    "type": "STRING",
                    "description": "Titre et artiste entendus, même approximatifs, ou URL YouTube",
                },
                "url": {
                    "type": "STRING",
                    "description": "URL YouTube optionnelle si l'utilisateur en a donné une",
                },
                "action": {
                    "type": "STRING",
                    "description": "download (défaut) | cancel (annule le téléchargement en cours, query facultatif) | status (progression) | list (derniers morceaux téléchargés)",
                },
            },
            "required": [],
        },
    },
    {
        "name": "background_tasks",
        "description": (
            "Crée et gère des veilles persistantes qui survivent au redémarrage. "
            "Utiliser pour : surveiller une page jusqu'à une baisse de prix, "
            "prévenir à la fin d'un build/commande longue, ou rappeler quelque "
            "chose lors de l'arrivée à la maison. L'action delegate active le "
            "Mode Agent Fantôme : un sous-agent autonome travaille pendant que "
            "la conversation continue, puis ANO-GPT annonce son résultat. Pour "
            "'cette page', laisser url vide : l'URL de la session navigateur "
            "pilotée sera utilisée."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": "delegate | result | history | watch_price | wait_build | wait_arrival | list | cancel",
                },
                "url": {"type": "STRING", "description": "URL http(s) à surveiller"},
                "target_price": {"type": "NUMBER", "description": "Prix seuil facultatif"},
                "interval_minutes": {
                    "type": "INTEGER",
                    "description": "Intervalle web, minimum 5 minutes (défaut 15)",
                },
                "selector": {"type": "STRING", "description": "Sélecteur CSS du prix, facultatif"},
                "label": {"type": "STRING", "description": "Nom court du produit ou du lieu"},
                "command_contains": {
                    "type": "STRING",
                    "description": "Fragment de commande à reconnaître; vide = prochaine commande longue",
                },
                "message": {"type": "STRING", "description": "Phrase à prononcer au déclenchement"},
                "task_id": {"type": "STRING", "description": "Identifiant à annuler"},
                "radius_m": {"type": "NUMBER", "description": "Rayon d'arrivée, défaut 250 m"},
                "mission": {
                    "type": "STRING",
                    "description": "Mission autonome complète à confier au sous-agent",
                },
                "workspace": {
                    "type": "STRING",
                    "description": "Chemin absolu du projet. Si omis, ANO-GPT utilise son propre dépôt, jamais tout le dossier personnel",
                },
                "timeout_minutes": {
                    "type": "INTEGER",
                    "description": "Durée maximale de la mission, défaut 60 minutes, maximum 480",
                },
                "show_terminal": {
                    "type": "BOOLEAN",
                    "description": "Afficher facultativement le journal en direct dans Kitty ; faux par défaut pour un travail invisible",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "proactive_mode",
        "description": (
            "Active ou met en silence les annonces spontanées de JARVIS. "
            "Utiliser quand l'utilisateur dit mode silence, ne me préviens plus, "
            "réactive les alertes proactives, demande leur état, ou dit de "
            "considérer sa position GPS actuelle comme son domicile. Ce mode ne "
            "coupe ni le microphone ni les réponses aux demandes explicites."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": "silence | on | status | set_home",
                },
                "radius_m": {
                    "type": "NUMBER",
                    "description": "Rayon du domicile en mètres pour set_home (défaut 250)",
                },
            },
            "required": ["action"],
        },
    },
]

# ── Boîte à outils réduite : l'IA compose, elle n'aiguille plus ───────────────
# Tout ce qu'un shell sait faire est retiré de la boîte à outils : c'est au
# modèle de réfléchir à la commande et de l'exécuter via shell_exec, au lieu de
# choisir dans un menu figé. On ne conserve que deux familles d'outils :
#   1. ce que le shell ne peut pas faire (vision, mémoire, Playwright, arrêt) ;
#   2. ce qui encode un piège machine déjà payé cher — capture (mss/pyautogui
#      rendent une image noire sur Wayland), musique (DISPLAY et casque BT en
#      mains-libres), souris (ydotool doit être calibré), fermeture de fenêtre
#      (seul window.kill ferme sur Hyprland 0.56).
# Les handlers correspondants restent dans _execute_tool : réactiver un outil
# ne demande que de retirer son nom d'ici.
_RETIRED_TOOLS = {
    "desktop_control",   # hyprctl
    "hypr_control",      # déjà couvert par shell_exec
    # computer_settings est volontairement exposé : il porte le volume
    # système explicite, distinct du volume de la musique.
    "file_processor",    # pdftotext, file, exiftool
    "code_helper",       # le modèle écrit le code lui-même
    "dev_agent",         # idem, en plusieurs fichiers
    "media_control",     # playerctl, pactl
    "game_updater",
    "flight_finder",
    # open_app : réactivé — lancement caché (workspace special:hidden) avec
    # relecture d'état fiable, plus sûr qu'une composition hyprctl par le
    # modèle à chaque fois (cf. piège « ok ne prouve rien »).
    # weather_report : réactivé — carte météo visuelle avec données
    # structurées réelles, plus riche qu'une recherche web générique.
    # send_message : réactivé — carte de prévisualisation + confirmation.
}
TOOL_DECLARATIONS = [t for t in TOOL_DECLARATIONS if t["name"] not in _RETIRED_TOOLS]
TOOL_DECLARATIONS.append({
    "name": "undo_action",
    "description": (
        "Annule la dernière modification réversible effectuée par ANO-GPT "
        "(fichier, volume, luminosité). Utilise action='list' pour afficher "
        "l'historique sans rien modifier."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "undo | list"},
        },
        "required": [],
    },
})
TOOL_DECLARATIONS.append({
    "name": "plugin_manager",
    "description": "Liste, inspecte, active, désactive ou recharge les plugins locaux de confiance d'ANO-GPT.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "list | enable | disable | reload"},
            "name": {"type": "STRING", "description": "Nom du plugin"},
        },
        "required": ["action"],
    },
})
TOOL_DECLARATIONS.append({
    "name": "auto_extension_control",
    "description": (
        "Journal et contrôle des extensions autonomes. list affiche les besoins observés, "
        "propositions, refus et modules actifs. disable ou delete exige l'identifiant affiché."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "list | reject | disable | delete"},
            "id": {"type": "STRING", "description": "Identifiant ou nom de l'extension"},
        },
        "required": ["action"],
    },
})
TOOL_DECLARATIONS.append({
    "name": "report_capability_gap",
    "description": (
        "À appeler quand aucune capacité ou aucun outil existant ne peut satisfaire une demande "
        "utilisateur récurrente. Ne l'appelle jamais pour une panne temporaire, un refus de sécurité "
        "ou un manque de paramètre. Il journalise la demande pour l'auto-extension isolée."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "request": {"type": "STRING", "description": "Demande utilisateur exacte non satisfaite"},
            "reason": {"type": "STRING", "description": "Pourquoi aucun outil existant ne convient"},
        },
        "required": ["request", "reason"],
    },
})
TOOL_DECLARATIONS.append({
    "name": "search_personal_docs",
    "description": (
        "Recherche chirurgicale et sémantique dans les documents personnels et code source "
        "(~/Documents, ~/OUTILS, dépôts git). Découpe syntaxique Tree-sitter (.py, .js, .ts, .sh, .rs) "
        "et hiérarchique avec fil d'Ariane (.md, .txt, .pdf). "
        "Retourne des extraits précis avec liens cliquables 'file:///...' et numéros de lignes."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "query": {
                "type": "STRING",
                "description": "Question, concept ou symbole recherché (ex: 'gestion du buffer audio', 'def launch_worker')",
            },
            "file_pattern": {
                "type": "STRING",
                "description": "Filtre optionnel sur le fichier ou dossier (ex: '*.py', 'core/*', 'ANO-GPT')",
            },
            "max_results": {
                "type": "INTEGER",
                "description": "Nombre maximum d'extraits retournés (défaut: 5)",
            },
        },
        "required": ["query"],
    },
})

# Libellés humains pour la carte « tâche en cours » (latence perçue > 1 s).
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
}




def _render_phone_outcome(outcome: dict, fallback: str,
                          choices_hint: str = "Demande lequel utiliser.") -> str:
    """Met en phrase la réponse d'ANO-Remote, listes de contacts comprises."""
    message = str(outcome.get("message") or fallback)
    choices = outcome.get("choices") or []
    if choices:
        rendered = ", ".join(
            f"choix {item.get('index', '?')} : {item.get('name', 'contact')} "
            f"({item.get('number_hint', 'numéro')})"
            for item in choices if isinstance(item, dict)
        )
        message += f" Choix Android : {rendered}. {choices_hint}"
    return message


class ToolHost(Protocol):
    """Contrat que l'orchestrateur expose au répartiteur d'outils."""

    ui: Any
    session: Any
    _action_runtime: Any
    _tool_session_memory: dict
    _plugins: Any
    _interrupted: bool
    _is_thinking: bool

    def speak(self, text: str) -> None: ...
    def _voice_is_stranger(self) -> bool: ...
    def _submit_text_turn(self, text: str, timeout_s: float = 90.0) -> Any: ...


class ToolDispatcher:
    """Déclarations d'outils, ``_execute_tool_impl``, lease, plugins.

    Les méthodes sont liées à l'hôte ``JarvisLive``.
    """
    _tool_session_memory: dict = {}
    _agent_tool_table: dict | None = None

    def _on_plugin_toggle(self, name: str, enabled: bool) -> None:
        if self._plugins.set_enabled(name, enabled):
            self._refresh_plugin_runtime()
            state = "activé" if enabled else "désactivé"
            self.ui.write_log(
                f"SYS : plugin {name} {state} — effectif à la prochaine reconnexion."
            )

    def _refresh_plugin_runtime(self) -> None:
        """Publie atomiquement la nouvelle table d'outils pour la prochaine session."""
        self._action_runtime = ActionRuntime(
            getattr(self, "_tool_declarations", TOOL_DECLARATIONS)
            + self._plugins.declarations()
        )
    async def _send_phone_sms(self, args: dict) -> str:
        """SMS par la SIM du téléphone, derrière la barrière de confirmation.

        L'envoi réel n'a lieu qu'après le clic humain ; la barrière refuse
        d'elle-même une demande identique déjà exécutée, ce qui empêche un
        rappel du modèle de faire partir deux fois le même message.
        """
        from core import human_confirmation

        target = str(args.get("target") or "").strip()
        body = str(args.get("body") or "").strip()
        selection = int(args.get("selection") or 0)
        auto_reply_authorized = args.get("auto_reply_authorized") is True
        if not target:
            return "Indiquez le destinataire du SMS."
        if not body:
            return "Indiquez le texte du SMS."
        if self._dashboard is None:
            return "ANO-Remote est indisponible : aucun SMS n'a été envoyé."

        loop = asyncio.get_running_loop()

        def _send() -> str:
            # Appelé depuis le thread de la confirmation : la requête repasse
            # par la boucle asyncio qui détient la socket du téléphone.
            future = asyncio.run_coroutine_threadsafe(
                self._dashboard.request_phone_sms(target, body, selection), loop
            )
            return _render_phone_outcome(future.result(timeout=40.0),
                                         "Commande SMS traitée.")

        # Le troisième mode de réponse est volontairement étroit : ce drapeau
        # ne peut être posé par le modèle qu'après l'autorisation verbale
        # explicite de l'utilisateur pour le SMS qui vient d'être annoncé.
        # Il ne mémorise aucune préférence et ne transforme donc jamais les
        # futurs messages en réponses automatiques.
        if auto_reply_authorized:
            return _send()

        return human_confirmation.request(
            "phone:sms",
            "Envoyer un SMS depuis le téléphone",
            f"Destinataire : {target}\n\n{body}",
            _send,
        )

    async def _execute_tool(self, fc) -> types.FunctionResponse:
        """Valide, borne et observe chaque action avant son exécution réelle."""
        name = str(getattr(fc, "name", "") or "")
        if self._interrupted or getattr(self, "_noise_turn", False):
            return types.FunctionResponse(
                id=fc.id,
                name=name or "unknown",
                response={"result": "Action annulée par interruption utilisateur", "ok": False},
            )
        raw_args = getattr(fc, "args", None)
        started = time.perf_counter()
        prepared: dict = {}
        bus = getattr(self, "_event_bus", None)
        if bus is not None:
            bus.publish_sync(ToolExecutionRequestedEvent(
                tool_name=name or "unknown",
                call_id=str(getattr(fc, "id", "") or ""),
                params=dict(raw_args) if isinstance(raw_args, dict) else {},
            ))

        try:
            prepared = self._action_runtime.prepare(name, raw_args)
            verification = await self._verify_sensitive_voice_command(name)
            if verification:
                response = types.FunctionResponse(
                    id=fc.id, name=name,
                    response={"result": verification, "ok": False},
                )
            else:
                async with self._action_runtime.lease(name) as policy:
                    if self._interrupted or getattr(self, "_noise_turn", False):
                        return types.FunctionResponse(
                            id=fc.id, name=name,
                            response={"result": "Action annulée par interruption utilisateur", "ok": False},
                        )
                    timeout_s = policy.timeout_s
                    if name == "email_control" and str(prepared.get("action", "")).lower() in {
                        "connect", "login", "authorize",
                    }:
                        # OAuth attend jusqu'à 180 s la validation dans Chrome,
                        # puis échange le code et vérifie le profil Gmail.
                        timeout_s = max(timeout_s, 210.0)
                    if name == "github_control" and str(prepared.get("action", "")).lower() in {
                        "connect", "login", "authorize",
                    }:
                        # Device Flow GitHub : le code peut être validé jusqu'à 15 min dans Chrome.
                        timeout_s = max(timeout_s, 920.0)
                    async with asyncio.timeout(timeout_s):
                        response = await self._execute_tool_impl(fc, prepared)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Une erreur de paramètres ou un circuit déjà ouvert n'est pas une
            # panne de l'outil et ne doit pas prolonger sa suspension.
            if not isinstance(exc, ActionRuntimeError):
                self._action_runtime.note_failure(name)
            message = friendly_runtime_error(name or "inconnue", exc)
            # Le modèle a cherché une capacité absente : conserver la demande
            # utilisateur, pas l'hallucination de nom d'outil, pour l'analyse
            # de lacunes. Les pannes d'outils existants restent exclues.
            if "Action inconnue" in str(exc):
                try:
                    from core.auto_extension import get_auto_extension_manager
                    get_auto_extension_manager().record_unmet(
                        str(getattr(self, "_live_user_text", "") or ""),
                        "aucun outil correspondant dans le répartiteur central",
                    )
                except Exception as exc:
                    print(f"[Dispatcher] Besoin non couvert non enregistré : {exc}")
            duration_ms = (time.perf_counter() - started) * 1000.0
            tool_stats.record(name or "unknown", ok=False, duration_ms=duration_ms, error=message)
            self._action_runtime.remember(
                self._tool_session_memory,
                name=name or "unknown",
                args=prepared or (dict(raw_args) if isinstance(raw_args, dict) else {}),
                result=message,
                ok=False,
                duration_ms=duration_ms,
            )
            # L'utilisateur entend une phrase courte ; la pile d'appel, elle,
            # doit survivre quelque part — sinon « l'outil a rencontré une
            # erreur » est tout ce qui reste pour diagnostiquer.
            from core.observability import tool_failure
            tool_failure(name or "unknown", exc, message=message,
                         duration_ms=duration_ms,
                         args=prepared or (dict(raw_args) if isinstance(raw_args, dict) else {}))
            self.ui.write_log(f"ERR: {message}")
            if bus is not None:
                bus.publish_sync(ToolExecutionFinishedEvent(
                    call_id=str(getattr(fc, "id", "") or ""),
                    result=message,
                    duration_ms=duration_ms,
                    error=f"{type(exc).__name__}: {exc}",
                ))
            if not self.ui.muted:
                self.ui.set_state("LISTENING")
            return types.FunctionResponse(
                id=fc.id,
                name=name or "unknown",
                response={"result": message, "ok": False,
                          "retryable": isinstance(exc, ActionRuntimeError),
                          "error_type": type(exc).__name__},
            )

        payload = getattr(response, "response", {}) or {}
        result = payload.get("result", "") if isinstance(payload, dict) else str(payload)
        failed = (
            isinstance(payload, dict) and payload.get("ok") is False
        ) or self._action_runtime.looks_failed(result)
        if isinstance(payload, dict):
            payload["ok"] = not failed
        if self._action_runtime.should_trip_from_result(result):
            self._action_runtime.note_failure(name)
        elif not failed:
            self._action_runtime.note_success(name)
        duration_ms = (time.perf_counter() - started) * 1000.0
        tool_stats.record(
            name,
            ok=not failed,
            duration_ms=duration_ms,
            error=str(result)[:240] if failed else "",
        )
        # Seules les actions réellement réussies alimentent les habitudes.
        # open_app est capturé plus précisément par launch_tracker (nom de
        # l'application, sans titre ni document ouvert).
        if not failed and name == "music_control":
            self._habits.record("music")
        self._action_runtime.remember(
            self._tool_session_memory,
            name=name,
            args=prepared,
            result=result,
            ok=not failed,
            duration_ms=duration_ms,
        )
        if bus is not None:
            bus.publish_sync(ToolExecutionFinishedEvent(
                call_id=str(getattr(fc, "id", "") or ""),
                result=result,
                duration_ms=duration_ms,
                error=str(result)[:240] if failed else None,
            ))
        return response

    async def _verify_sensitive_voice_command(self, tool_name: str) -> str:
        """Fait confirmer les mots par un second ASR avant une action sensible."""
        if (not getattr(self, "_precision_stt_enabled", True)
                or tool_name not in _DESTRUCTIVE_TOOLS):
            return ""
        clip = self._last_voice_clip
        main_text = str(self._live_user_text or "").strip()
        if (clip is None or not main_text
                or time.monotonic() - self._last_voice_clip_at > 20.0):
            return ""
        if self._precision_stt is None:
            vocabulary = [
                *FRENCH_TECH_PHRASES, self._asst_name,
                str(_voice_engine_settings().get("user_name", "Anonymous")),
            ]
            from core.precision_stt import PrecisionTranscriber
            self._precision_stt = PrecisionTranscriber(
                _get_api_key(), model=self._precision_stt_model,
                vocabulary=vocabulary,
            )
        result = await self._precision_stt.transcribe(clip, SEND_SAMPLE_RATE)
        if not result.available:
            if not self._precision_stt_warning_logged:
                self._precision_stt_warning_logged = True
                self.ui.write_log(
                    "WARN: vérification Gemini 3.5 indisponible — "
                    f"protection locale conservée ({result.error})."
                )
            return ""
        self._precision_stt_warning_logged = False
        self._tool_session_memory["precision_transcript"] = result.text
        from core.precision_stt import transcripts_conflict
        if not transcripts_conflict(main_text, result.text):
            print(f"[STT précision] ✅ {result.text}")
            return ""
        print(
            f"[STT précision] ⛔ divergence: Live={main_text!r} / "
            f"Transcribe={result.text!r}"
        )
        try:
            self.ui.show_card(
                "confirmation", "Commande vocale incertaine",
                "Deux moteurs ont compris des phrases différentes. "
                "L'action a été bloquée ; répète-la clairement.",
            )
        except Exception:
            pass
        return (
            "ACTION NON EXÉCUTÉE : la transcription haute précision ne confirme "
            "pas la phrase entendue par l'agent vocal. Demande simplement à "
            "l'utilisateur de répéter sa commande ; ne tente aucun autre outil."
        )

    async def _execute_tool_batch(self, function_calls) -> list[types.FunctionResponse]:
        """Exécute ensemble les lectures indépendantes, sinon conserve l'ordre."""
        calls = list(function_calls or ())
        self._is_thinking = True
        active = getattr(self, "_active_tool_tasks", None)
        if active is None:
            active = self._active_tool_tasks = set()
        started: set[asyncio.Task] = set()

        def cancelled_responses() -> list[types.FunctionResponse]:
            return [
                types.FunctionResponse(
                    id=fc.id,
                    name=str(getattr(fc, "name", "") or "unknown"),
                    response={
                        "result": "Action annulée par interruption utilisateur",
                        "ok": False,
                    },
                )
                for fc in calls
            ]

        def start(fc) -> asyncio.Task:
            task = asyncio.create_task(
                self._execute_tool(fc),
                name=f"live-tool-{str(getattr(fc, 'name', 'unknown'))}",
            )
            started.add(task)
            active.add(task)
            task.add_done_callback(active.discard)
            return task

        try:
            if len(calls) > 1 and all(
                self._action_runtime.can_run_in_parallel(
                    str(getattr(fc, "name", "") or ""), getattr(fc, "args", None)
                )
                for fc in calls
            ):
                for fc in calls:
                    print(f"[JARVIS] 📞 {fc.name} (parallèle)")
                return list(await asyncio.gather(*(start(fc) for fc in calls)))

            responses = []
            for fc in calls:
                if self._interrupted or getattr(self, "_noise_turn", False):
                    responses.extend(cancelled_responses()[len(responses):])
                    return responses
                print(f"[JARVIS] 📞 {fc.name}")
                responses.append(await start(fc))
            return responses
        except asyncio.CancelledError:
            # `interrupt()` n'annule que ces enfants. Transformer leur
            # annulation en réponse d'outil garde la connexion Live intacte et
            # rend immédiatement le micro à l'utilisateur.
            for task in started:
                if not task.done():
                    task.cancel()
            if started:
                await asyncio.gather(*started, return_exceptions=True)
            return cancelled_responses()
        finally:
            active.difference_update(started)
            self._is_thinking = False

    @staticmethod
    def _compute_deep_research(question: str, context: str = "") -> str:
        """Calcul bloquant isolé du canal Gemini Live."""
        from core import agent_brain
        from core.llm_client import think_deep, BRAIN_UNCONFIGURED

        result = think_deep(question, context)
        if result != BRAIN_UNCONFIGURED:
            return result or "La recherche approfondie n'a rien renvoyé."
        if agent_brain.available():
            return agent_brain.think(question, context)
        return (
            "Aucun agent n'est installé et aucune clé DeepSeek, Grok, OpenAI "
            "ou Claude n'est enregistrée. Réponds toi-même à la demande."
        )

    async def _deliver_deep_research(self, question: str, context: str = "") -> None:
        """Calcule puis livre le résultat sans retenir un tool call Live."""
        self._ui_card("show_card", "task", "Réflexion approfondie",
                      f"Recherche en cours…\n\n{question[:500]}")

        try:
            if hasattr(self, "thought_streamer"):
                self.thought_streamer.feed_tool_start("agent_brain", {"question": question})
            result = await asyncio.to_thread(
                self._compute_deep_research, question, context
            )
            if hasattr(self, "thought_streamer"):
                self.thought_streamer.feed_tool_end("agent_brain")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            result = f"La recherche approfondie a échoué : {str(exc)[:300]}"

        result = str(result or "La recherche approfondie n'a rien renvoyé.").strip()
        # Une sortie anormalement énorme peut elle aussi faire refuser le tour
        # Live. La réponse vocale reste dense ; la carte conserve l'essentiel.
        result = result[:12000]
        self._ui_card("dismiss_cards", "task", "Réflexion approfondie")
        self._ui_card("show_card", "result", "Réflexion approfondie", result)

        # La session peut être brièvement en reprise pour une autre raison.
        # Attendre son retour évite de perdre un calcul déjà terminé.
        for _ in range(120):
            if self.session is not None:
                break
            await asyncio.sleep(0.25)
        if self.session is None:
            self.ui.write_log(
                "ERR: résultat de recherche disponible dans la carte, "
                "mais la session vocale est hors ligne."
            )
            return

        prompt = (
            "[RÉSULTAT DE RECHERCHE APPROFONDIE TERMINÉ]\n"
            f"Question originale : {question}\n\n"
            f"Résultat : {result}\n\n"
            "Présente maintenant ce résultat directement en français, sans "
            "annoncer un nouveau délai et sans appeler aucun outil."
        )
        try:
            delivered = await self._submit_text_turn(prompt)
        except Exception as exc:
            delivered = False
            print(f"[DeepResearch] Livraison vocale différée : {exc}")
        if not delivered:
            self.ui.write_log(
                "WARN: résultat vocal différé ; il reste disponible dans la carte."
            )

    def _start_deep_research(self, question: str, context: str = "") -> bool:
        """Lance au plus une recherche lourde à la fois."""
        tasks = getattr(self, "_deep_research_tasks", None)
        if tasks is None:
            tasks = self._deep_research_tasks = set()
        tasks.intersection_update({task for task in tasks if not task.done()})
        if tasks:
            return False
        task = asyncio.create_task(
            self._deliver_deep_research(question, context),
            name="deep-research-background",
        )
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return True

    async def _deliver_image_generation(self, args: dict) -> None:
        """Génère l'image hors du tour Live et annonce chaque étape en français."""
        from actions.image_generation import generate_image

        prompt = str(args.get("prompt") or "")
        self._ui_card("show_card", "task", "Création d'image", "Génération Azure en cours…")
        worker = asyncio.create_task(
            asyncio.to_thread(generate_image, args, self.ui),
            name="azure-image-worker",
        )
        try:
            try:
                result = await asyncio.wait_for(asyncio.shield(worker), timeout=20.0)
            except asyncio.TimeoutError:
                # Le premier accusé de réception vient du résultat immédiat de
                # l'outil. Cette seconde phrase n'est envoyée que si Azure
                # travaille toujours, et passe par Gemini Live : même voix.
                if self.session is not None:
                    await self._submit_text_turn(
                        "[AVANCEMENT IMAGE] Dis uniquement en français : « Monsieur, "
                        "la création de l'image est toujours en cours. Je vous préviens dès qu'elle est prête. »",
                        timeout_s=25.0,
                    )
                result = await worker
        except asyncio.CancelledError:
            worker.cancel()
            raise
        except Exception as exc:
            result = f"Génération Azure échouée : {exc}"

        result = str(result or "La génération d'image n'a rien renvoyé.").strip()
        self._ui_card("dismiss_cards", "task", "Création d'image")
        self._ui_card("show_card", "result", "Création d'image", result)
        if self.session is not None:
            await self._submit_text_turn(
                "[RÉSULTAT DE GÉNÉRATION D'IMAGE]\n"
                f"Demande : {prompt}\nRésultat : {result}\n\n"
                "Annonce uniquement le résultat en français, avec le ton Majeur. "
                "Si l'image est créée, confirme qu'elle est prête et indique son chemin. "
                "Si elle a échoué, explique la raison en français sans proposer de la relancer automatiquement.",
                timeout_s=35.0,
            )

    def _start_image_generation(self, args: dict) -> bool:
        """Une image Azure à la fois : elle peut prendre plusieurs minutes."""
        tasks = getattr(self, "_image_generation_tasks", None)
        if tasks is None:
            tasks = self._image_generation_tasks = set()
        tasks.intersection_update({task for task in tasks if not task.done()})
        if tasks:
            return False
        task = asyncio.create_task(
            self._deliver_image_generation(dict(args)), name="azure-image-background",
        )
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return True

    async def _deliver_video_generation(self, args: dict) -> None:
        """Sora travaille plusieurs minutes : tout se passe hors du tour Live."""
        from actions.video_generation import generate_video

        prompt = str(args.get("prompt") or "")
        title = "Création vidéo"
        self._ui_card("show_card", "task", title, "Génération Azure Sora en cours…")

        loop = asyncio.get_running_loop()

        def progress(status: str) -> None:
            # Appelé depuis le thread worker : on repasse par la boucle Qt.
            loop.call_soon_threadsafe(
                lambda: self._safe_update_card("task", title, f"Sora : {status}…")
            )

        worker = asyncio.create_task(
            asyncio.to_thread(generate_video, args, self.ui, progress),
            name="azure-video-worker",
        )
        try:
            try:
                result = await asyncio.wait_for(asyncio.shield(worker), timeout=25.0)
            except asyncio.TimeoutError:
                if self.session is not None:
                    await self._submit_text_turn(
                        "[AVANCEMENT VIDÉO] Dis uniquement en français : « Monsieur, "
                        "la vidéo est toujours en cours de génération. Je vous préviens dès qu'elle est prête. »",
                        timeout_s=25.0,
                    )
                result = await worker
        except asyncio.CancelledError:
            worker.cancel()
            raise
        except Exception as exc:
            result = f"Génération vidéo Azure échouée : {exc}"

        result = str(result or "La génération vidéo n'a rien renvoyé.").strip()
        self._ui_card("dismiss_cards", "task", title)
        self._ui_card("show_card", "result", title, result)
        if self.session is not None:
            await self._submit_text_turn(
                "[RÉSULTAT DE GÉNÉRATION VIDÉO]\n"
                f"Demande : {prompt}\nRésultat : {result}\n\n"
                "Annonce uniquement le résultat en français, avec le ton Majeur. "
                "Si la vidéo est créée, confirme qu'elle est prête et indique son chemin. "
                "Si elle a échoué, explique la raison sans relancer la génération.",
                timeout_s=35.0,
            )

    def _ui_card(self, method: str, *args) -> None:
        """Appelle `show_card` / `update_card` / `dismiss_cards` sans jamais
        interrompre l'outil : une carte est décorative, mais son échec doit
        laisser une trace lisible plutôt que disparaître en silence."""
        try:
            getattr(self.ui, method)(*args)
        except Exception as exc:
            print(f"[Dispatcher] Carte UI ignorée ({method}) : {type(exc).__name__}: {exc}")

    def _safe_update_card(self, card_type: str, title: str, body: str) -> None:
        self._ui_card("update_card", card_type, title, body)

    def _start_video_generation(self, args: dict) -> bool:
        """Une vidéo Azure à la fois : Sora est lent et coûteux."""
        tasks = getattr(self, "_video_generation_tasks", None)
        if tasks is None:
            tasks = self._video_generation_tasks = set()
        tasks.intersection_update({task for task in tasks if not task.done()})
        if tasks:
            return False
        task = asyncio.create_task(
            self._deliver_video_generation(dict(args)), name="azure-video-background",
        )
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return True

    async def _deliver_decision_simulation(self, decision: str, options: list[str]) -> None:
        """Produit une carte complète et ne prononce que la recommandation."""
        from core.decision_simulator import DecisionSimulationError, run_simulation

        title = "Simulation stratégique"
        try:
            self.ui.show_card("task", title, "Préparation des scénarios…")

            def progress(message: str) -> None:
                self.ui.update_card("task", title, message)

            simulation = await asyncio.to_thread(
                run_simulation, decision, options, progress=progress
            )
        except asyncio.CancelledError:
            self.ui.dismiss_cards("task", title)
            raise
        except DecisionSimulationError as exc:
            self.ui.dismiss_cards("task", title)
            self.ui.show_card("error", title, str(exc))
            return
        except Exception as exc:
            self.ui.dismiss_cards("task", title)
            self.ui.show_card("error", title, f"Simulation interrompue : {str(exc)[:400]}")
            return

        self.ui.dismiss_cards("task", title)
        body = (
            f"**Décision :** {simulation.decision}\n\n"
            f"**Options simulées :** {', '.join(simulation.options)}\n"
            f"**Archivée :** {simulation.saved_at}\n\n{simulation.synthesis}"
        )
        self.ui.show_card("result", title, body[:12_000])
        # Le résultat détaillé appartient à la carte. Cette instruction assure
        # une restitution vocale courte, sans relancer un autre outil.
        try:
            await self._submit_text_turn(
                "[SIMULATION STRATÉGIQUE TERMINÉE] Résumé vocal en deux phrases "
                "maximum : annonce seulement la recommandation principale et son "
                "risque décisif, sans outil ni préambule.\n\n"
                + simulation.synthesis[:3_500]
            )
        except Exception:
            self.ui.write_log("SYS : simulation terminée ; le détail est dans la carte.")

    def _start_decision_simulation(self, decision: str, options: list[str] | None = None) -> bool:
        tasks = getattr(self, "_decision_simulation_tasks", None)
        if tasks is None:
            tasks = self._decision_simulation_tasks = set()
        tasks.intersection_update({task for task in tasks if not task.done()})
        if tasks:
            return False
        task = asyncio.create_task(
            self._deliver_decision_simulation(decision, list(options or [])),
            name="decision-simulation-background",
        )
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return True

    async def _execute_tool_impl(self, fc, prepared_args: dict | None = None) -> types.FunctionResponse:
        name = fc.name
        args = dict(prepared_args if prepared_args is not None else (fc.args or {}))

        print(f"[JARVIS] 🔧 {name}  {args}")
        self.ui.set_state("THINKING")

        if name == "save_memory":
            category = args.get("category", "notes")
            key      = args.get("key", "")
            value    = args.get("value", "")
            if key and value:
                update_memory({category: {key: {"value": value}}})
                # Le JSON reste la source du briefing et de la proactivité ;
                # la base longue durée est ce qui se cherche et se rappelle.
                self._store_memory(category, key, value)
                print(f"[Memory] 💾 save_memory: {category}/{key} = {value}")
            if not self.ui.muted:
                self.ui.set_state("LISTENING")
            return types.FunctionResponse(
                id=fc.id, name=name,
                response={"result": "ok", "silent": True}
            )

        if name == "voice_id":
            # Apprendre une voix demande trois empreintes : c'est court, mais
            # assez pour ne pas le faire sur la boucle qui porte l'audio.
            answer = await asyncio.get_running_loop().run_in_executor(
                None, self._agent_voice_id, args
            )
            if not self.ui.muted:
                self.ui.set_state("LISTENING")
            return types.FunctionResponse(id=fc.id, name=name,
                                          response={"result": answer})

        # Une voix inconnue ne commande pas la machine. Le refus part d'ici et
        # non du modèle : une consigne dans le prompt se contourne en insistant,
        # un verrou dans le code, non.
        if _is_destructive(name, args) and self._voice_is_stranger():
            print(f"[Voix] ⛔ {name} refusé — voix non reconnue")
            return types.FunctionResponse(
                id=fc.id, name=name,
                response={"result": (
                    "REFUSÉ : cette voix n'est pas celle de l'utilisateur "
                    "enregistré, et cette action est irréversible. Dis-le "
                    "simplement et poliment, propose de demander à l'utilisateur "
                    "lui-même. N'essaie aucun autre outil pour y arriver."
                )},
            )

        if name == "routine":
            # Rien à déléguer à un exécuteur : la routine part dans son propre
            # thread et rend la main tout de suite.
            answer = self._agent_routine(args)
            if not self.ui.muted:
                self.ui.set_state("LISTENING")
            return types.FunctionResponse(id=fc.id, name=name,
                                          response={"result": answer})

        loop   = asyncio.get_event_loop()
        result = "Done."

        # Latence perçue : si l'outil prend plus d'1 s, une carte « tâche »
        # apparaît — l'utilisateur ne doit jamais se demander si ça a marché.
        label = _TOOL_LABELS.get(name, name)

        tool_finished = False

        async def _slow_task_card():
            try:
                await asyncio.sleep(1.0)
                # Le résultat peut arriver exactement à la frontière d'une
                # seconde. Ne jamais ajouter une carte « en cours » après la
                # fin de l'outil : elle ne recevrait plus de mise à jour et
                # resterait visuellement bloquée.
                if tool_finished:
                    return
                self.ui.show_card("task", label, f"{label} en cours…")
            except asyncio.CancelledError:
                pass

        slow_card_task = asyncio.ensure_future(_slow_task_card())
        try:
            if name == "consult_brain":
                # Le fournisseur choisi par l'utilisateur pense et agit ; la
                # voix ne fait que lire ce qu'il renvoie.
                from core import brain_relay
                from core.llm_client import main_brain_label

                try:
                    result = await brain_relay.run_turn(
                        self,
                        str(args.get("question") or ""),
                        context=str(args.get("context") or ""),
                        declarations=self._relay_declarations(),
                        base_prompt=self._relay_base_prompt(),
                    )
                    print(f"[Relais] Réponse fournie par {main_brain_label()}.")
                except Exception as exc:
                    # Le cerveau externe est injoignable : plutôt qu'un
                    # silence, Gemini reprend la main pour ce tour-là.
                    print(f"[Relais] ⚠️ échec du cerveau externe : {exc}")
                    result = (
                        "Le cerveau externe n'a pas répondu "
                        f"({str(exc)[:120]}). Réponds toi-même à la demande, "
                        "sans rappeler cet outil."
                    )

            elif name == "deep_think":
                question = args.get("question", "")
                context = args.get("context", "")
                if self._start_deep_research(question, context):
                    # Répondre au tool call tout de suite. Le résultat réel
                    # arrivera dans un nouveau tour lorsqu'il sera prêt.
                    result = (
                        "La recherche approfondie est lancée en arrière-plan. "
                        "Dis simplement à l'utilisateur que tu recherches et "
                        "que le résultat arrivera automatiquement. Ne rappelle "
                        "pas cet outil."
                    )
                else:
                    result = (
                        "Une recherche approfondie est déjà en cours. Dis-le "
                        "brièvement et ne rappelle pas cet outil."
                    )

            elif name == "simulate_decision":
                decision = str(args.get("decision") or "")
                options = args.get("options") if isinstance(args.get("options"), list) else []
                if self._start_decision_simulation(decision, options):
                    result = (
                        "La simulation stratégique est lancée en arrière-plan. "
                        "Dis à l'utilisateur que la carte affichera le débat, les risques "
                        "et la recommandation dès qu'ils seront prêts."
                    )
                else:
                    result = "Une simulation stratégique est déjà en cours."

            elif name == "open_app":
                r = await loop.run_in_executor(None, lambda: open_app(parameters=args, response=None, player=self.ui, session_memory=self._tool_session_memory))
                result = r or f"Opened {args.get('app_name')}."

            elif name == "close_app":
                r = await loop.run_in_executor(None, lambda: close_app(parameters=args, response=None, player=self.ui, session_memory=self._tool_session_memory))
                result = r or f"Closed {args.get('app_name')}."

            elif name == "weather_report":
                r = await loop.run_in_executor(None, lambda: weather_action(parameters=args, player=self.ui))
                result = r or "Weather delivered."
                from actions.weather_report import get_last_weather_card
                _card = get_last_weather_card()
                if _card:
                    self.ui.show_card("result", "Météo", _card)

            elif name == "location":
                # Résultat structuré du GPS/config, jamais une supposition à
                # partir de la langue ou de l'adresse du fournisseur IA.
                result = await loop.run_in_executor(
                    None, lambda: self._agent_location({"refresh": False})
                )

            elif name == "browser_control":
                r = await loop.run_in_executor(None, lambda: browser_control(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "file_controller":
                r = await loop.run_in_executor(
                    None,
                    lambda: file_controller(
                        parameters=args, player=self.ui,
                        session_memory=self._tool_session_memory,
                    ),
                )
                result = r or "Done."

            elif name == "send_message":
                r = await loop.run_in_executor(None, lambda: send_message(parameters=args, response=None, player=self.ui, session_memory=None))
                result = r or f"Message sent to {args.get('receiver')}."
                if result.startswith("[NEEDS_CONFIRM] "):
                    _plat, _recv, _txt = result[len("[NEEDS_CONFIRM] "):].split("|", 2)
                    _confirm_args = dict(args)
                    _confirm_args["confirm"] = True

                    def _resend(a=_confirm_args):
                        if self.ui.on_text_command:
                            self.ui.on_text_command(
                                f"envoie ce message maintenant : « {a.get('message_text')} » "
                                f"à {a.get('receiver')} sur {a.get('platform')}, confirme envoi"
                            )

                    self.ui.show_card(
                        "confirmation", f"Envoyer sur {_plat} ?",
                        f"**À :** {_recv}\n\n{_txt}",
                        [
                            {"label": "Envoyer", "primary": True, "callback": _resend},
                            {"label": "Annuler",
                             "callback": (lambda: self.ui.on_text_command("annule cet envoi")
                                          if self.ui.on_text_command else None)},
                        ],
                    )
                    result = (f"Aperçu affiché à l'utilisateur pour confirmation avant envoi "
                              f"(ne dis pas que c'est envoyé) : {_plat} → {_recv} : {_txt}")

            elif name == "reminder":
                r = await loop.run_in_executor(None, lambda: reminder(parameters=args, response=None, player=self.ui))
                result = r or "Reminder set."

                _action = str(args.get("action", "set") or "set").lower()
                if _action in ("set", "add", "create", "list") and r and not r.startswith(("Aucun", "La date")):
                    await loop.run_in_executor(None, self._show_active_reminders_card)

            elif name == "timer":
                action = str(args.get("action") or "set").lower()
                if action in {"set", "add", "create"}:
                    seconds = int(float(args.get("minutes") or 0) * 60)
                    if seconds < 1:
                        result = "Indique une durée positive en minutes."
                    else:
                        item = self._timers.create(str(args.get("name") or "Minuteur"), seconds)
                        result = f"Minuteur {item['name']} lancé pour {seconds // 60} minutes."
                elif action == "list":
                    active = self._timers.active()
                    result = "Aucun minuteur actif." if not active else "; ".join(
                        f"{t['name']} : {t['remaining'] // 60:02d}:{t['remaining'] % 60:02d}" for t in active)
                elif action == "cancel":
                    result = "Minuteur annulé." if self._timers.cancel(str(args.get("value") or args.get("name") or "")) else "Minuteur introuvable."
                else:
                    result = "Action minuteur inconnue."

            elif name == "youtube_video":
                r = await loop.run_in_executor(
                    None,
                    lambda: youtube_video(
                        parameters=args,
                        response=None,
                        player=self.ui,
                        session_memory=self._tool_session_memory,
                        speak=self.speak,
                    ),
                )
                result = r or "Done."

            elif name == "screen_process":
                import time as _t_mod
                _now = _t_mod.monotonic()
                _cooldown = 4.0  # seconds — covers echo window after speaking ends
                if self._vision_busy or (_now - self._vision_last_time) < _cooldown:
                    _wait = max(0, _cooldown - (_now - self._vision_last_time))
                    print(f"[Vision] ⏳ Cooldown active ({_wait:.1f}s remaining) — ignoring duplicate call")
                    result = "Vision is still processing the previous request. I will not call this again."
                else:
                    self._vision_busy      = True
                    self._vision_last_time = _now
                    angle     = args.get("angle", "screen").lower()
                    user_text = args.get("text", "What do you see?")
                    _skip_vision_pipeline = False
                    _meta = {}
                    if angle == "camera":
                        img_b, mime_t = await loop.run_in_executor(
                            None, self._grab_camera_still
                        )
                        self._vision_cam_active = True
                        print(f"[Vision] 📷 Camera: {len(img_b):,} bytes")
                        _stall = "camera"
                    else:
                        from core import screen_capture
                        img_b = mime_t = _meta = None
                        mind = getattr(self, "_screen_mind", None)
                        cached = mind.cached_capture(max_age_s=30.0) if mind is not None else None
                        if cached is not None and cached.webp_bytes:
                            img_b, mime_t, _meta = (
                                cached.webp_bytes, cached.mime_type, cached.as_meta()
                            )
                            print(
                                f"[Vision] 🖥️  Veille ({_meta.get('window_class', 'screen')}, "
                                f"{cached.age_s:.1f}s): {len(img_b):,} bytes"
                            )
                            if cached.ocr_usable and not screen_reader.wants_image(user_text):
                                self._pending_vision = None
                                self._vision_busy = False
                                result = cached.as_tool_result(user_text)
                                _skip_vision_pipeline = True
                        if not _skip_vision_pipeline:
                            if img_b is None:
                                img_b, mime_t, _meta = await loop.run_in_executor(
                                    None, lambda: screen_capture.capture_window_or_screen(target="active_window")
                                )
                            _win_cls = _meta.get("window_class", "screen")
                            print(f"[Vision] 🖥️  Active Window ({_win_cls}): {len(img_b):,} bytes")
                            _stall = "screen"

                    if not _skip_vision_pipeline:
                        def _show_vision_thumb():
                            try:
                                import base64, io
                                from PIL import Image
                                im = Image.open(io.BytesIO(img_b)).convert("RGB")
                                im.thumbnail((320, 200))
                                buf = io.BytesIO()
                                im.save(buf, format="JPEG", quality=70)
                                b64 = base64.b64encode(buf.getvalue()).decode("ascii")
                                label = "Caméra" if angle == "camera" else f"Écran ({_win_cls})"
                                body = (f"![aperçu](data:image/jpeg;base64,{b64})\n\n"
                                        f"_Ce que je regarde en ce moment — {user_text[:80]}_")
                                self.ui.show_card("info", f"👁 {label}", body)
                            except Exception as e:
                                print(f"[Vision] Miniature indisponible : {e}")

                        await loop.run_in_executor(None, _show_vision_thumb)

                        # Si la question porte sur un bug, une erreur ou un build échoué
                        _is_debug_q = any(k in user_text.lower() for k in ("bug", "erreur", "error", "plante", "crash", "traceback", "panic", "build", "echec", "échec"))
                        if angle != "camera" and _is_debug_q:
                            from core import auto_debug
                            _spoken, _diag = await loop.run_in_executor(
                                None, lambda: auto_debug.auto_debug_live(user_query=user_text, target_window="active_window", player=self.ui)
                            )
                            self._pending_vision = None
                            self._vision_busy    = False
                            result = f"[AUTO_DEBUG_DIRECT]\n{_diag.full_explanation}\n\nSynthèse vocale : {_spoken}"
                        else:
                            from core.multimodal_vision import (
                                detect_visual_domain,
                                inspect_screen_live,
                                should_use_expert_vision,
                            )
                            _read = None
                            if angle != "camera":
                                _read = await loop.run_in_executor(
                                    None, screen_reader.read, img_b, user_text
                                )
                            _win_info = None
                            try:
                                from core import screen_capture as _sc
                                _win_info = _sc.get_active_window(skip_anogpt=True)
                            except Exception:
                                _win_info = None
                            _domain = detect_visual_domain(user_text, _win_info)
                            _ocr_ok = bool(_read is not None and _read.usable)
                            if (
                                not should_use_expert_vision(
                                    user_text,
                                    ocr_usable=_ocr_ok,
                                    domain=_domain,
                                    angle=angle,
                                )
                                and _ocr_ok
                            ):
                                self._pending_vision = None
                                self._vision_busy = False
                                result = _read.as_tool_result(user_text)
                            else:
                                def _expert():
                                    return inspect_screen_live(
                                        user_query=user_text,
                                        target="active_window",
                                        domain=_domain,
                                        player=self.ui,
                                        image_bytes=img_b,
                                        mime_type=mime_t,
                                        window_info=_win_info,
                                        metadata=_meta if isinstance(_meta, dict) else {},
                                        extracted_text=_read.text if _read is not None else None,
                                    )

                                try:
                                    _spoken, _diag = await loop.run_in_executor(None, _expert)
                                except Exception as _vis_exc:
                                    print(f"[Vision] analyse experte échouée : {_vis_exc}")
                                    _spoken, _diag = "", None
                                if _diag is not None and _diag.spoken_summary and "clé api" not in _diag.spoken_summary.casefold():
                                    self._pending_vision = None
                                    self._vision_busy = False
                                    result = _diag.as_tool_result(user_text)
                                else:
                                    self._pending_vision = (img_b, mime_t, user_text, angle)
                                    result = (
                                        f"[VISION_ACTIVE] {_stall.capitalize()} captured. "
                                        f"Immediately say ONE short natural sentence in the user's own language, "
                                        f"telling them you are looking at their {_stall} right now. "
                                        f"Do NOT describe or guess content — the actual image arrives in the NEXT message."
                                    )

            elif name == "camera_control":
                result = await loop.run_in_executor(
                    None,
                    self._camera_tool,
                    (args.get("action") or "open").strip().lower(),
                    (args.get("source") or "").strip().lower(),
                    (args.get("lens") or "").strip().lower(),
                )

            elif name == "point_on_screen":
                desc = str(args.get("description") or "Élément ciblé")
                coords = args.get("coordinates") or []
                mode = str(args.get("mode") or "auto")
                dur = float(args.get("duration") or 3.0)
                result = self._agent_point_on_screen(desc, coords, mode, dur)

            elif name == "self_repair":
                # Une réparation peut réveiller dev_agent : elle a sa place dans
                # un exécuteur, pas sur la boucle qui porte la voix.
                result = await loop.run_in_executor(
                    None, self._agent_self_repair, args
                )

            elif name == "close_camera":
                self.ui.stop_camera_stream()
                if hasattr(self, "stop_continuous_vision"):
                    try:
                        self.stop_continuous_vision()
                    except Exception as exc:
                        print(f"[Dispatcher] Arrêt de la vision continue : {exc}")
                result = "Camera closed."

            elif name == "show_map":
                query = (args.get("query") or "").strip()
                radius_km = float(args.get("radius_km") or 3.0)
                lat_arg = args.get("lat")
                lon_arg = args.get("lon")

                # « Ma position » exige une mesure actuelle : demander le GPS
                # au téléphone connecté avant de lire le fichier partagé.
                if not query and lat_arg is None and lon_arg is None and self._dashboard:
                    await self._dashboard.request_fresh_location(timeout=10.0)

                def _do_show_map():
                    if lat_arg is not None and lon_arg is not None:
                        lat, lon = float(lat_arg), float(lon_arg)
                        label = query or "position"
                    else:
                        from core.geolocation import geocode, get_precise_user_coords
                        if query:
                            coords = geocode(query)
                            label = query
                        else:
                            coords = get_precise_user_coords()
                            label = "votre position"
                        if not coords:
                            if not query:
                                return (
                                    "Je ne peux pas afficher votre position réelle sans un relevé GPS "
                                    "précis. Ouvrez le contrôle à distance sur votre téléphone "
                                    "et autorisez la localisation, puis réessayez. Je n'utiliserai pas "
                                    "Conakry ou la position IP comme si c'était votre position."
                                )
                            return f"Impossible de localiser « {query} » sur la carte."
                        lat, lon = coords
                    self.ui.show_map(label, lat, lon, radius_km)
                    return f"Carte affichée, centrée sur {label} ({lat:.4f}, {lon:.4f})."

                result = await loop.run_in_executor(None, _do_show_map)

            elif name == "navigate":
                r = await loop.run_in_executor(
                    None,
                    lambda: navigation_action(
                        parameters=args,
                        player=self.ui,
                        speak=self.speak,
                    ),
                )
                result = r or "Navigation initialisée."

            elif name == "find_nearby":
                # Chercher « autour de moi » exige de savoir où l'on est
                # maintenant : on redemande le GPS au téléphone avant de
                # chercher, comme pour « montre ma position ». Sans cela, des
                # distances au mètre près sont calculées depuis une position IP
                # vieille de plusieurs heures.
                around_user = not (args.get("near") or "").strip()
                fresh_location = False
                if around_user and self._dashboard:
                    fresh_location = await self._dashboard.request_fresh_location(timeout=8.0)

                if around_user and not fresh_location:
                    result = (
                        "Je n'ai reçu aucun relevé GPS frais. Connecte ANO Remote sur "
                        "ton téléphone, ouvre le contrôle à distance et autorise la "
                        "localisation, puis redemande. Je n'utiliserai ni Kouriah, ni "
                        "Conakry, ni la position IP comme position fixe."
                    )
                else:
                    lookup_args = dict(args)
                    if around_user:
                        lookup_args["_require_precise_gps"] = True

                    def _do_find_nearby():
                        return find_nearby(
                            parameters=lookup_args,
                            session_memory=self._tool_session_memory,
                            ui=self.ui,
                        )
                    result = await loop.run_in_executor(None, _do_find_nearby)

            elif name == "close_map":
                self.ui.close_map()
                result = "Carte fermée."

            elif name == "email_control":
                def _do_email_control():
                    return email_control(parameters=args, session_memory=self._tool_session_memory, ui=self.ui)
                result = await loop.run_in_executor(None, _do_email_control)

            elif name == "github_control":
                result = await loop.run_in_executor(None, lambda: github_control(args, ui=self.ui))

            elif name == "calendar_control":
                result = await loop.run_in_executor(None, lambda: calendar_control(args))

            elif name == "cloud_integrations_control":
                result = await loop.run_in_executor(None, lambda: cloud_integrations_control(args))

            elif name == "tiktok_tracker":
                result = await loop.run_in_executor(
                    None,
                    lambda: tiktok_tracker(parameters=args, player=self.ui, speak=self.speak),
                )

            elif name == "tiktok_coach":
                result = await loop.run_in_executor(
                    None,
                    lambda: tiktok_coach(parameters=args, player=self.ui, speak=self.speak),
                )

            elif name in ("prayer", "prayer_control"):
                result = await loop.run_in_executor(None, lambda: prayer_control(args))

            elif name == "contacts_control":
                result = await loop.run_in_executor(None, lambda: contacts_control(args))

            elif name == "sparring_partner":
                result = sparring_partner(args, self._tool_session_memory)

            elif name == "focus_guard":
                result = await loop.run_in_executor(None, lambda: self._focus_guard.control(args))

            elif name == "phone_call":
                target = str(args.get("target") or "").strip()
                if not target:
                    result = "Indiquez le nom ou le numéro à appeler."
                elif self._dashboard is None:
                    result = "ANO-Remote est indisponible : aucun appel n'a été lancé."
                else:
                    outcome = await self._dashboard.request_phone_call(
                        target, selection=int(args.get("selection") or 0)
                    )
                    result = _render_phone_outcome(outcome, "Commande téléphonique traitée.")

            elif name == "phone_hangup":
                if self._dashboard is None:
                    result = "ANO-Remote est indisponible : impossible de raccrocher."
                else:
                    outcome = await self._dashboard.request_phone_hangup()
                    result = _render_phone_outcome(outcome, "Demande de raccrochage traitée.")

            elif name == "phone_contacts":
                query = str(args.get("query") or "").strip()
                if not query:
                    result = "Indiquez le nom ou la fin de numéro à chercher."
                elif self._dashboard is None:
                    result = ("ANO-Remote est indisponible : le carnet du téléphone "
                              "n'est pas consultable. Ne conclus pas que le contact "
                              "n'existe pas.")
                else:
                    outcome = await self._dashboard.request_phone_contacts(query)
                    result = _render_phone_outcome(
                        outcome, "Recherche de contacts traitée.",
                        choices_hint="Ce sont les contacts du téléphone.",
                    )

            elif name == "phone_sms":
                result = await self._send_phone_sms(args)

            elif name == "second_brain":
                result = await loop.run_in_executor(
                    None, lambda: self._agent_second_brain(args)
                )

            elif name == "computer_settings":
                r = await loop.run_in_executor(None, lambda: computer_settings(parameters=args, response=None, player=self.ui, session_memory=self._tool_session_memory))
                result = r or "Done."

            elif name == "desktop_control":
                r = await loop.run_in_executor(None, lambda: desktop_control(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "live_auto_debug":
                r = await loop.run_in_executor(
                    None, lambda: auto_debug_action(parameters=args, player=self.ui, speak=self.speak)
                )
                result = r or "Diagnostic d'erreur terminé."

            elif name == "code_helper":
                r = await loop.run_in_executor(None, lambda: code_helper(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "dev_agent":
                r = await loop.run_in_executor(None, lambda: dev_agent(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "web_search":
                r = await loop.run_in_executor(None, lambda: web_search_action(parameters=args, player=self.ui))
                result = r or "Done."
                # Mirror results to the on-screen content panel
                _mode = args.get("mode", "search")
                if r and not r.startswith("No results") and not r.startswith("Search failed"):
                    _query = args.get("query") or ", ".join(args.get("items", []))
                    _label = f"{_mode.upper()} — {_query[:38]}" if _query else _mode.upper()
                    self.ui.show_content(_label, r)
                    from actions.web_search import get_last_card_markdown
                    _md = get_last_card_markdown()
                    if _md:
                        self.ui.show_card("result", _label, _md)
            elif name == "image_search":
                r = await loop.run_in_executor(
                    None,
                    lambda: image_search_action(parameters=args, player=self.ui),
                )
                result = r or "Aucune image trouvée."
            elif name == "generate_image":
                if self._start_image_generation(args):
                    result = (
                        "La génération de l'image est lancée. Dis-le immédiatement "
                        "en français à l'utilisateur ; il recevra un point d'avancement "
                        "puis une annonce lorsque le fichier sera prêt."
                    )
                else:
                    result = (
                        "Une image est déjà en cours de création. Dis-le en français, "
                        "sans relancer la génération."
                    )
            elif name == "show_last_generated_image":
                result = ("J'ouvre l'image créée dans le visionneur intégré."
                          if self.ui.show_last_generated_image()
                          else "Aucune image créée n'est disponible dans cette session.")
            elif name == "generate_video":
                if self._start_video_generation(args):
                    result = (
                        "La génération de la vidéo est lancée. Dis-le immédiatement "
                        "en français à l'utilisateur ; il recevra un point d'avancement "
                        "puis une annonce lorsque le fichier sera prêt."
                    )
                else:
                    result = (
                        "Une vidéo est déjà en cours de création. Dis-le en français, "
                        "sans relancer la génération."
                    )
            elif name == "generate_document":
                r = await loop.run_in_executor(
                    None,
                    lambda: generate_document_action(parameters=args, player=self.ui),
                )
                result = r or "Document généré."
            elif name == "close_image_gallery":
                self.ui.close_image_gallery()
                result = "Galerie d'images fermée."
            elif name == "file_processor":
                if not args.get("file_path") and self.ui.current_file:
                    args["file_path"] = self.ui.current_file
                r = await loop.run_in_executor(
                    None,
                    lambda: file_processor(parameters=args, player=self.ui, speak=self.speak)
                )
                result = r or "Done."

            elif name == "computer_control":
                r = await loop.run_in_executor(None, lambda: computer_control(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "game_updater":
                r = await loop.run_in_executor(None, lambda: game_updater(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "flight_finder":
                r = await loop.run_in_executor(None, lambda: flight_finder(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "media_control":
                r = await loop.run_in_executor(None, lambda: media_control(parameters=args, player=self.ui))
                result = r or "Contrôle média exécuté."

            elif name == "capture_control":
                capture_action = str(args.get("action") or "").casefold()
                spoken = getattr(self, "_live_user_text", "")
                if (capture_action == "start_recording"
                        and not _has_explicit_capture_intent(spoken)):
                    result = (
                        "Enregistrement non lancé : la phrase reconnue ne demande pas "
                        "clairement une capture d'écran. Demande à l'utilisateur de "
                        "préciser s'il veut réellement enregistrer l'écran."
                    )
                else:
                    r = await loop.run_in_executor(
                        None,
                        lambda: capture_control(parameters=args, player=self.ui,
                                                session_memory=self._tool_session_memory),
                    )
                    result = r or "Capture effectuée."

            elif name == "music_control":
                r = await loop.run_in_executor(
                    None,
                    lambda: music_control(parameters=args, player=self.ui,
                                          session_memory=self._tool_session_memory),
                )
                result = r or "Lecture lancée."

            elif name == "download_music":
                r = await loop.run_in_executor(
                    None,
                    lambda: download_music(
                        parameters=args, player=self.ui, speak=self.speak,
                    ),
                )
                result = r or "Téléchargement lancé."

            elif name == "background_tasks":
                result = self._background_tasks_control(args)

            elif name == "voice_style":
                result = self._agent_voice_style(args)

            elif name == "proactive_mode":
                action = str(args.get("action") or "status").strip().casefold()
                if action in {"silence", "silent", "off", "0"}:
                    state = self._proactive.set_silent(True)
                elif action in {"on", "actif", "active", "1"}:
                    state = self._proactive.set_silent(False)
                elif action in {"set_home", "home", "maison", "domicile"}:
                    from core.geolocation import get_live_position

                    position = get_live_position(resolve_place=False)
                    if not position:
                        result = (
                            "Position GPS indisponible. Ouvre ANO Remote sur le "
                            "téléphone puis réessaie."
                        )
                    else:
                        self._proactive.set_home(
                            position["lat"], position["lon"],
                            float(args.get("radius_m") or 250),
                        )
                        result = "Cette position est maintenant ton domicile."
                    state = None
                else:
                    state = "silence" if self._proactive.silent else "actif"
                if state is not None:
                    result = f"Mode proactif : {state}."

            elif name == "system_status":
                r = await loop.run_in_executor(None, get_system_status)
                result = str(r)

            elif name == "undo_action":
                if str(args.get("action") or "undo").casefold() == "list":
                    entries = undo_stack.history()
                    result = ("Actions annulables, de la plus récente à la plus ancienne :\n- "
                              + "\n- ".join(entries)) if entries else "Aucune action annulable."
                else:
                    result = await loop.run_in_executor(None, undo_stack.undo_last)

            elif name == "plugin_manager":
                action = str(args.get("action") or "list").casefold()
                plugin_name = str(args.get("name") or "").strip()
                if action == "reload":
                    self._plugins.discover()
                    self._refresh_plugin_runtime()
                    result = "Plugins rescannés. Les déclarations seront actualisées à la prochaine reconnexion."
                elif action in {"enable", "disable"}:
                    changed = self._plugins.set_enabled(plugin_name, action == "enable")
                    if changed:
                        self._refresh_plugin_runtime()
                    result = (("Plugin activé" if action == "enable" else "Plugin désactivé")
                              + f" : {plugin_name}. La modification prendra effet à la prochaine reconnexion.") \
                             if changed else f"Plugin inconnu : {plugin_name}."
                else:
                    rows = self._plugins.status()
                    if not rows:
                        result = "Aucun plugin installé dans le dossier plugins/."
                    else:
                        lines = []
                        for row in rows:
                            state = "ERREUR" if row["error"] else ("ACTIF" if row["enabled"] else "INACTIF")
                            lines.append(f"- {row['name']} — {state}" + (f" : {row['error']}" if row["error"] else ""))
                        result = "Plugins ANO-GPT :\n" + "\n".join(lines)
                        self.ui.show_card("info", "Plugins ANO-GPT", result)

            elif name == "auto_extension_control":
                from core.auto_extension import get_auto_extension_manager
                result = get_auto_extension_manager().control(
                    str(args.get("action") or "list").casefold(),
                    str(args.get("id") or ""),
                    plugins=self._plugins,
                    refresh=self._refresh_plugin_runtime,
                )
                self.ui.show_card("info", "Extensions autonomes", result)

            elif name == "report_capability_gap":
                request_text = str(args.get("request") or "")
                # Repêchage des outils par contexte : le modèle se croit démuni
                # alors que l'outil existe, simplement dans un paquet fermé.
                # Rouvrir vaut mieux que journaliser une lacune imaginaire.
                extend = getattr(self, "_extend_toolkit", None)
                if callable(extend) and extend(request_text, origin="lacune signalée"):
                    result = ("[OUTILS_ELARGIS] Les outils de ce domaine viennent d'être "
                              "activés. La demande est relancée automatiquement : ne "
                              "signale aucune lacune.")
                else:
                    from core.auto_extension import get_auto_extension_manager
                    need = get_auto_extension_manager().record_unmet(
                        request_text, str(args.get("reason") or "outil absent"),
                    )
                    result = (f"Lacune enregistrée dans le groupe {need.id}." if need
                              else "Lacune non enregistrée : demande insuffisante.")

            elif name == "shell_exec":
                if _is_global_volume_shell_command(args):
                    requested = str(getattr(self, "_live_user_text", "") or "")
                    if _media_volume_request(requested) and not _explicit_system_volume_request(requested):
                        relative = _relative_volume_value_from_shell(args)
                        if re.search(r"\b(?:vid[eé]o|youtube|film|clip)\b", requested, re.IGNORECASE):
                            result = await loop.run_in_executor(
                                None,
                                lambda: youtube_video(
                                    {"action": "volume", "volume": relative},
                                    player=self.ui, session_memory=self._tool_session_memory,
                                ),
                            )
                        else:
                            result = await loop.run_in_executor(
                                None,
                                lambda: music_control(
                                    {"action": "volume", "value": relative},
                                    player=self.ui, session_memory=self._tool_session_memory,
                                ),
                            )
                    else:
                        system_args = _system_volume_args_from_shell(args)
                        result = await loop.run_in_executor(
                            None,
                            lambda: computer_settings(
                                parameters=system_args, response=None, player=self.ui,
                                session_memory=self._tool_session_memory,
                            ),
                        )
                else:
                    r = await loop.run_in_executor(None, lambda: shell_exec(parameters=args, player=self.ui))
                    result = r or "Commande exécutée."
                if result.startswith("[NEEDS_CONFIRM] "):
                    _cmd = result[len("[NEEDS_CONFIRM] "):]
                    self.ui.show_card(
                        "confirmation", "Commande risquée",
                        f"```\n{_cmd}\n```\nCette commande peut avoir des effets difficiles à annuler. L'exécuter ?",
                        [
                            {"label": "Oui, exécuter", "primary": True,
                             "callback": (lambda: self.ui.on_text_command(f"confirme la commande : {_cmd}")
                                          if self.ui.on_text_command else None)},
                            {"label": "Annuler",
                             "callback": (lambda: self.ui.on_text_command("annule, ne fais pas ça")
                                          if self.ui.on_text_command else None)},
                        ],
                    )
                    result = (f"Commande risquée détectée, confirmation demandée à l'utilisateur "
                              f"via une carte (ne l'exécute pas sans son accord explicite) : {_cmd}")

            elif name == "hypr_control":
                r = await loop.run_in_executor(None, lambda: hypr_control(parameters=args, player=self.ui))
                result = r or "Action Hyprland effectuée."

            elif name == "devsecops":
                r = await loop.run_in_executor(None, lambda: devsecops_control(parameters=args, player=self.ui))
                result = r or "Opération DevSecOps effectuée."

            elif name == "hypr_orchestrator":
                r = await loop.run_in_executor(None, lambda: hypr_orchestrator_control(parameters=args, player=self.ui))
                result = r or "Organisation Hyprland terminée."

            elif name == "shutdown_jarvis":
                requested = str(getattr(self, "_live_user_text", "") or "").casefold()
                if "veille" in requested or "repos" in requested:
                    # Filet final, y compris si une transcription a contourné
                    # l'interception locale : « veille » ne ferme jamais ANO.
                    sleeper = getattr(self, "_sleep", None)
                    if callable(sleeper):
                        sleeper("commande vocale")
                    result = "ANO-GPT est en veille ; l'application reste ouverte."
                    return types.FunctionResponse(
                        id=fc.id, name=name, response={"result": result}
                    )
                self.ui.write_log("SYS: Shutdown requested.")
                from core.personality_modes import user_address
                self.speak(f"Au revoir, {user_address()}.")
                def _shutdown():
                    import time, os
                    time.sleep(1)
                    os._exit(0)
                threading.Thread(target=_shutdown, daemon=True).start()

            else:
                if name in self._plugins.plugins:
                    result = await loop.run_in_executor(
                        None,
                        lambda: self._plugins.run(
                            name, args, player=self.ui,
                            session_memory=self._tool_session_memory,
                        ),
                    )
                else:
                    result = f"Outil inconnu: {name}"

        except asyncio.CancelledError:
            raise
        except Exception as e:
            result = f"Tool '{name}' failed: {e}"
            traceback.print_exc()
            self.speak_error(name, e)
        finally:
            tool_finished = True
            slow_card_task.cancel()
            # Attend la fin effective de la petite tâche : sans cela, son
            # signal Qt pouvait être traité après dismiss_cards et recréer une
            # carte périmée (« Capture d'écran en cours… »).
            await asyncio.gather(slow_card_task, return_exceptions=True)
            # Une carte « tâche » décrit uniquement l'opération en cours. Dès
            # que l'outil rend la main — succès, erreur ou annulation — elle
            # doit quitter l'interface. Les cartes de résultat/confirmation
            # restent, elles, disponibles pour être lues ou actionnées.
            self._ui_card("dismiss_cards", "task")

        if not self.ui.muted:
            self.ui.set_state("LISTENING")

        print(f"[JARVIS] 📤 {name} → {str(result)[:80]}")
        return types.FunctionResponse(
            id=fc.id, name=name,
            response={"result": result}
        )

    def _agent_tools(self) -> dict:
        """Table des outils qu'un agent externe peut exécuter.

        Chaque valeur prend le dictionnaire d'arguments et rend le texte que
        l'agent lira. Ces fonctions tournent dans un fil d'exécution (voir
        `_tool` dans `_start_control_server`), jamais sur la boucle asyncio :
        elles ont donc le droit de bloquer.

        Le contrat est volontairement plat — des chaînes en entrée, une chaîne
        en sortie. Un agent lit du texte ; lui rendre une structure l'obligerait
        à la reformater avant de la restituer.
        """
        if self._agent_tool_table is None:
            self._agent_tool_table = {
                # ── le cœur visuel ──────────────────────────────────────────
                "find_nearby": self._agent_find_nearby,
                "show_map": self._agent_show_map,
                "close_map": lambda a: (self.ui.close_map(), "Carte fermée.")[1],
                "camera": self._agent_camera,
                "show_card": self._agent_show_card,
                # ── voix et état ────────────────────────────────────────────
                "speak": self._agent_speak,
                "ask": self._agent_ask,
                "assistant_status": self._agent_status,
                # ── données personnelles ────────────────────────────────────
                "email": self._agent_email,
                "calendar": lambda a: calendar_control(a),
                "cloud_integrations": lambda a: cloud_integrations_control(a),
                "contacts": lambda a: contacts_control(a),
                "memory_save": self._agent_memory_save,
                "memory_search": self._agent_memory_search,
                "second_brain": self._agent_second_brain,
                # ── machine et médias ───────────────────────────────────────
                "weather": self._agent_weather,
                "web_search": self._agent_web_search,
                "image_search": self._agent_image_search,
                "close_image_gallery": lambda a: (
                    self.ui.close_image_gallery(), "Galerie d'images fermée."
                )[1],
                "screenshot": self._agent_screenshot,
                "open_app": self._agent_open_app,
                "close_app": self._agent_close_app,
                "computer_settings": self._agent_computer_settings,
                "file_search": self._agent_file_search,
                "location": self._agent_location,
                "youtube": self._agent_youtube,
                "reminder": self._agent_reminder,
                "system_status": self._agent_system_status,
                "music": self._agent_music,
                "download_music": self._agent_download_music,
                "routine": self._agent_routine,
                "voice_id": self._agent_voice_id,
                "voice_style": self._agent_voice_style,
                "self_repair": self._agent_self_repair,
                "background_tasks": self._background_tasks_control,
                "devsecops": lambda a: devsecops_control(a),
                "hypr_orchestrator": lambda a: hypr_orchestrator_control(a),
                "auto_debug": lambda a: auto_debug_action(a, player=self.ui, speak=self.speak),
                "inspect_screen": lambda a: self._agent_inspect_screen(a),
                "navigate": lambda a: navigation_action(a, player=self.ui, speak=self.speak),
                "point_on_screen": lambda a: self._agent_point_on_screen(a),
                "search_personal_docs": self._agent_search_personal_docs,
                "prayer": lambda a: prayer_control(a),
            }
        return self._agent_tool_table

    @staticmethod
    def _arg(args: dict, key: str, default: str = "") -> str:
        value = args.get(key, default)
        return "" if value is None else str(value).strip()

    def _agent_find_nearby(self, args: dict) -> str:
        """Cherche des lieux ET les épingle dans la grande carte."""
        from actions.find_nearby import find_nearby

        return find_nearby(
            {
                "query": self._arg(args, "query"),
                "near": self._arg(args, "near"),
                "radius_km": args.get("radius_km") or 5.0,
            },
            session_memory=self._tool_session_memory,
            ui=self.ui,
        )

    def _agent_show_map(self, args: dict) -> str:
        query = self._arg(args, "query")
        radius = float(args.get("radius_km") or 3.0)
        lat, lon = args.get("lat"), args.get("lon")
        if lat is not None and lon is not None:
            self.ui.show_map(query or "Position", float(lat), float(lon), radius)
            return f"Carte centrée sur {lat}, {lon}."
        if not query:
            return "Précisez un lieu, ou des coordonnées lat/lon."
        from core.geolocation import geocode

        coords = geocode(query)
        if not coords:
            return f"Impossible de situer « {query} »."
        self.ui.show_map(query, coords[0], coords[1], radius)
        return f"Carte centrée sur {query}."

    def _agent_camera(self, args: dict) -> str:
        return self._camera_tool(
            self._arg(args, "action", "open").lower(),
            self._arg(args, "source").lower(),
            self._arg(args, "lens").lower(),
        )

    def _agent_show_card(self, args: dict) -> str:
        title = self._arg(args, "title") or "Agent"
        self.ui.show_card(
            self._arg(args, "type", "info").lower() or "info",
            title,
            self._arg(args, "body"),
        )
        return f"Carte « {title} » affichée."

    def _agent_speak(self, args: dict) -> str:
        """Fait prononcer une phrase par ANO-GPT.

        Le texte traverse la session Gemini Live, qui possède la voix : il est
        donc reformulé au passage, pas lu mot pour mot.
        """
        text = self._arg(args, "text")
        if not text:
            return "Rien à dire : le texte est vide."
        if not self.session:
            return "La session vocale n'est pas active : rien n'a été prononcé."
        self.speak(
            "[AGENT] Dis ceci à l'utilisateur, dans sa langue et sans appeler "
            f"le moindre outil : {text}"
        )
        return "Énoncé transmis à la voix."

    def _agent_ask(self, args: dict) -> str:
        """Envoie une demande à l'assistant vocal, qui décidera lui-même."""
        text = self._arg(args, "text")
        if not text:
            return "Rien à demander : le texte est vide."
        if not self.session:
            return "La session vocale n'est pas active."
        self._wake_up("agent")
        self._on_text_command(text)
        return f"Demande transmise à l'assistant : {text[:80]}"

    def _agent_status(self, args: dict) -> str:
        camera = self._camera
        continuous_active = bool(getattr(self, "_continuous", None) and self._continuous.is_active)
        if self.ui.muted:
            micro = "coupé"
        elif continuous_active:
            micro = "écoute continue"
        else:
            micro = "à l'écoute"
        session = "active" if self.session else "inactive"
        if camera is not None and camera.active:
            optique = f"ouverte ({camera.source}/{camera.lens})"
        else:
            optique = "fermée"
        policy = getattr(self, "_live_models", None)
        model = str(getattr(policy, "current", LIVE_MODEL)).removeprefix("models/")
        precision = (
            "active" if getattr(self, "_precision_stt_enabled", True)
            else "désactivée"
        )
        return (
            f"micro={micro} · session={session} · caméra={optique} · "
            f"Gemini Live={model} · vérification 3.5={precision}"
        )

    def _agent_email(self, args: dict) -> str:
        from actions.email import email_control

        parameters = dict(args)
        parameters.update({
            "action": self._arg(args, "action", "unread").lower() or "unread",
            "query": self._arg(args, "query"),
            "id": self._arg(args, "id"),
            "max_results": args.get("max_results") or 10,
        })
        return email_control(
            parameters,
            session_memory=self._tool_session_memory,
            ui=self.ui,
        )

    def _agent_memory_save(self, args: dict) -> str:
        from memory.memory_manager import remember

        key = self._arg(args, "key")
        value = self._arg(args, "value")
        if not key or not value:
            return "Précisez la clé et la valeur à mémoriser."
        category = self._arg(args, "category", "notes") or "notes"
        remember(key, value, category)
        return self._store_memory(category, key, value)

    def _agent_memory_search(self, args: dict) -> str:
        query = self._arg(args, "query")
        if query:
            rows = memory_store.search(query, limit=6)
            if rows:
                return "\n".join(
                    f"- {(r['key'] or '').replace('_', ' ') or r['kind']} : {r['value']}"
                    for r in rows
                )
            return f"Rien en mémoire à propos de « {query} »."

        # Sans terme de recherche, on rend le socle : profil, faits récents et
        # dernières conversations — de quoi situer sans tout déverser.
        summary = memory_store.session_block(max_chars=2500)
        return summary.strip() or "Aucun souvenir enregistré."

    def _agent_second_brain(self, args: dict) -> str:
        from actions.second_brain import second_brain_action

        return second_brain_action(args, player=self.ui, speak=self.speak)

    def _agent_voice_id(self, args: dict) -> str:
        """Outil `voice_id` : apprendre, oublier ou interroger l'empreinte."""
        action = (self._arg(args, "action", "status") or "status").lower()
        if getattr(self, "_speaker", None) is None:
            self._speaker = speaker_id.SpeakerID()
        speaker = self._speaker

        if not speaker.available:
            return ("La reconnaissance vocale n'est pas installée : il manque "
                    "le modèle (scripts/install_speaker_id.sh).")

        if action == "forget":
            self._speaker_verdict = None
            self._speaker_announced = None
            return speaker.forget()

        if action in ("enroll", "learn", "apprendre"):
            clips = list(self._voice_clips)
            if len(clips) < 2:
                return ("Il me faut deux ou trois phrases avant d'apprendre "
                        "une voix. Parle-moi encore un peu, puis redemande.")
            name = self._arg(args, "name") or "Anonymous"
            answer = speaker.enroll(clips[-3:], name)
            self._speaker_verdict = None
            self._speaker_announced = None
            return answer

        if not speaker.enrolled:
            return "Aucune voix n'est enregistrée pour l'instant."
        verdict = self._speaker_verdict
        if verdict is None:
            return f"Voix enregistrée : {speaker.name}. Rien de vérifié pour l'instant."
        return (f"Voix enregistrée : {speaker.name}. Dernier tour : "
                f"{'reconnu' if verdict.known else 'voix inconnue'} "
                f"(score {verdict.score:.2f}).")

    def _agent_voice_style(self, args: dict) -> str:
        """Lit ou change la préférence durable d'élocution."""
        from core.prosody import get_prosody_manager

        manager = get_prosody_manager()
        style = self._arg(args, "style")
        if not style:
            labels = {
                "professional": "professionnel",
                "stark": "Tony Stark",
                "synthetic": "ultra-synthétique",
            }
            label = labels[manager.preferred_style()]
            return f"Style vocal actuel : {label}."
        try:
            label = manager.set_preferred_style(style)
        except ValueError as exc:
            return str(exc)
        # Force la prochaine directive, même si le profil acoustique ne change pas.
        self._prosody_mode = ""
        return f"Style vocal {label} activé."
    def _dispatch_agent_tool(self, name: str, args: dict) -> str:
        """Appelle un outil de l'assistant par son nom, comme le ferait l'agent."""
        handler = self._agent_tools().get(name)
        if handler is None:
            return f"outil inconnu : {name}"
        return handler(dict(args or {}))

    def _agent_routine(self, args: dict) -> str:
        """Outil `routine` : exécute une routine nommée, ou liste les routines."""
        name = self._arg(args, "name")
        if not name:
            available = routines.names()
            return ("Routines disponibles : " + ", ".join(available)
                    if available else "Aucune routine définie.")
        found = routines.match(name)
        if found is None:
            available = routines.names()
            return (f"Aucune routine « {name} ». "
                    + ("Disponibles : " + ", ".join(available) if available
                       else "Le fichier config/routines.yaml est vide."))
        # La voix appelle déjà cet outil : elle annoncera elle-même le résultat,
        # inutile de lui faire répéter la phrase de la routine.
        self._run_routine(found, announce=False)
    def _agent_self_repair(self, args: dict) -> str:
        from actions.self_repair import self_repair

        return self_repair(
            parameters={
                "action": self._arg(args, "action", "diagnose") or "diagnose",
                "tool": self._arg(args, "tool"),
            },
            player=self.ui,
            # La réparation tourne en fond : c'est par `speak` que son résultat
            # est annoncé plus tard. Le diagnostic, lui, ne parle pas deux fois.
            speak=self.speak if (self._arg(args, "action", "") or "").lower() in
                  {"repair", "reparer", "fix", "corriger", "corrige", "restart"} else None,
            session_memory=self._tool_session_memory,
        )
    def _agent_weather(self, args: dict) -> str:
        from actions.weather_report import get_last_weather_card, weather_report

        result = weather_report({
            "city": self._arg(args, "city"),
            "time": self._arg(args, "time", "aujourd'hui") or "aujourd'hui",
        })
        # Ce chemin est employé par les raccourcis/agents locaux ; il doit
        # offrir la même carte que l'appel d'outil Gemini classique.
        card = get_last_weather_card()
        if card and getattr(self, "ui", None) is not None:
            self.ui.show_card("result", "Météo", card)
        return result

    def _agent_web_search(self, args: dict) -> str:
        from actions.web_search import web_search

        return web_search({
            "query": self._arg(args, "query"),
            "mode": self._arg(args, "mode", "search") or "search",
            "platform": self._arg(args, "platform"),
            "items": args.get("items") or [],
            "aspect": self._arg(args, "aspect", "general") or "general",
            "count": args.get("count") or 5,
        }, player=self.ui, session_memory=self._tool_session_memory)

    def _agent_image_search(self, args: dict) -> str:
        from actions.image_search import image_search

        return image_search({
            "query": self._arg(args, "query"),
            "limit": args.get("limit") or 6,
        }, player=self.ui)

    def _agent_screenshot(self, args: dict) -> str:
        from actions.capture import capture_control

        return capture_control({
            "action": self._arg(args, "action", "screenshot") or "screenshot",
            "path": self._arg(args, "path") or None,
            "monitor": self._arg(args, "monitor") or None,
            "window": self._arg(args, "window") or None,
            "copy_clipboard": args.get("copy_clipboard", True),
            "annotate": args.get("annotate", False),
            "delay": args.get("delay") or 0,
        }, player=self.ui, session_memory=self._tool_session_memory)

    def _agent_inspect_screen(self, args: dict) -> str:
        query = self._arg(args, "query") or "Analyse ce qui est affiché à l'écran."
        domain = self._arg(args, "domain", "auto") or "auto"
        target = self._arg(args, "target", "active_window") or "active_window"
        spoken, diag = inspect_screen_live(
            user_query=query,
            target=target,
            domain=None if domain == "auto" else domain,
            player=self.ui,
        )
        return spoken

    def _agent_point_on_screen(
        self,
        args_or_desc: Union[dict, str],
        coordinates: list | None = None,
        mode: str = "auto",
        duration: float = 3.0,
    ) -> str:
        """Active l'overlay d'annotation visuelle sur écran (rectangle, laser ou trajectoire)."""
        from ui.visual_pointer import get_visual_pointer

        target = ""
        if isinstance(args_or_desc, dict):
            desc = str(args_or_desc.get("description") or "Élément ciblé")
            target = str(args_or_desc.get("target") or args_or_desc.get("widget") or "").strip()
            raw_coords = args_or_desc.get("coordinates")
            if isinstance(raw_coords, str):
                try:
                    import json
                    coords = json.loads(raw_coords)
                except Exception:
                    coords = []
            else:
                coords = list(raw_coords) if raw_coords else []
            m = str(args_or_desc.get("mode") or "auto")
            dur = float(args_or_desc.get("duration") or 3.0)

            # Les coordonnées générées par un modèle sont souvent relatives à
            # une image recadrée, pas au bureau virtuel. Les afficher telles
            # quelles pouvait placer le laser sur le mauvais écran ou dans un
            # coin sans rapport avec l'élément cité. Une cible textuelle est
            # donc toujours relocalisée sur une capture fraîche avant dessin.
            if coords and not target:
                target = desc
                coords = []
        else:
            desc = str(args_or_desc or "Élément ciblé")
            coords = coordinates or []
            m = mode
            dur = duration

        vp = getattr(self.ui, "visual_pointer", None) or get_visual_pointer()
        return vp.point_on_screen(desc, coords, mode=m, duration=dur, target=target or None)

    def _agent_open_app(self, args: dict) -> str:
        from actions.open_app import open_app

        return open_app(parameters={
            "app_name": self._arg(args, "app_name"),
            "command": self._arg(args, "command"),
            "target": self._arg(args, "target"),
            "description": self._arg(args, "description"),
            "workspace": self._arg(args, "workspace") or None,
            "hidden": bool(args.get("hidden", False)),
            "count": args.get("count"),
            "instance_name": self._arg(args, "instance_name"),
        }, response=None, player=self.ui,
            session_memory=getattr(self, "_tool_session_memory", {}))

    def _agent_close_app(self, args: dict) -> str:
        from actions.close_app import close_app

        return close_app(parameters={
            "app_name": self._arg(args, "app_name"),
            "description": self._arg(args, "description"),
            "workspace": self._arg(args, "workspace") or None,
            "force": bool(args.get("force", False)),
        }, response=None, player=self.ui,
            session_memory=getattr(self, "_tool_session_memory", {}))

    def _agent_computer_settings(self, args: dict) -> str:
        from actions.computer_settings import computer_settings

        return computer_settings(parameters={
            "action": self._arg(args, "action"),
            "description": self._arg(args, "description"),
            "value": self._arg(args, "value") or None,
            "confirmed": self._arg(args, "confirmed"),
        }, response=None, player=self.ui,
            session_memory=getattr(self, "_tool_session_memory", {}))

    def _agent_file_search(self, args: dict) -> str:
        from actions.file_controller import file_controller

        return file_controller(parameters={
            "action": "find",
            "name": self._arg(args, "name"),
            "extension": self._arg(args, "extension"),
            "path": self._arg(args, "path", "home") or "home",
            "kind": self._arg(args, "kind"),
            "max_results": args.get("max_results") or 20,
        }, response=None, player=self.ui,
            session_memory=getattr(self, "_tool_session_memory", {}))

    def _agent_search_personal_docs(self, args: dict) -> str:
        """Recherche sémantique et syntaxique dans les documents personnels et code source."""
        from core.personal_rag import search_personal_docs

        query = self._arg(args, "query")
        pattern = self._arg(args, "file_pattern")
        try:
            max_results = int(args.get("max_results") or 5)
        except (ValueError, TypeError):
            max_results = 5

        return search_personal_docs(
            query=query,
            file_pattern=pattern,
            max_results=max_results,
        )

    def _agent_location(self, args: dict) -> str:
        import json
        from core.geolocation import get_user_location

        refresh = self._arg(args, "refresh").lower() in {
            "1", "true", "yes", "oui",
        }
        location = get_user_location(force_refresh=refresh)
        return json.dumps(location, ensure_ascii=False, sort_keys=True)

    def _agent_youtube(self, args: dict) -> str:
        from actions.youtube_video import youtube_video

        return youtube_video(parameters={
            "action": self._arg(args, "action"),
            "query": self._arg(args, "query"),
            "url": self._arg(args, "url"),
            "region": self._arg(args, "region", "FR") or "FR",
            "value": self._arg(args, "value"),
        }, response=None, player=self.ui,
            session_memory=self._tool_session_memory, speak=self.speak)

    def _agent_reminder(self, args: dict) -> str:
        from actions.reminder import reminder

        return reminder(parameters={
            "action": self._arg(args, "action", "set") or "set",
            "description": self._arg(args, "description"),
            "date": self._arg(args, "date"),
            "time": self._arg(args, "time"),
            "message": self._arg(args, "message", "Rappel") or "Rappel",
            "value": self._arg(args, "value"),
        }, response=None, player=self.ui,
            session_memory=self._tool_session_memory)

    def _agent_system_status(self, args: dict) -> str:
        from actions.system_monitor import system_status_tool

        return system_status_tool({
            "action": self._arg(args, "action", "status") or "status",
            "component": self._arg(args, "component"),
        })

    def _agent_music(self, args: dict) -> str:
        from actions.music import music_control

        return music_control(
            {
                "action": self._arg(args, "action", "play").lower() or "play",
                "query": self._arg(args, "query"),
                "value": self._arg(args, "value"),
                "kind": self._arg(args, "kind", "audio") or "audio",
            },
            player=self.ui, session_memory=self._tool_session_memory,
        )

    def _agent_download_music(self, args: dict) -> str:
        from actions.download_music import download_music as _download

        return _download(
            {
                "query": self._arg(args, "query") or self._arg(args, "url"),
                "url": self._arg(args, "url"),
            },
            player=self.ui,
            speak=self.speak,
        )

    def _background_tasks_control(self, args: dict) -> str:
        if getattr(self, "_background_tasks", None) is None:
            publisher = (
                self._proactive.publish
                if hasattr(self, "_proactive") else (lambda *a, **k: False)
            )
            self._background_tasks = BackgroundTaskService(
                publisher, on_task_update=self._render_background_task,
            )
        action = self._arg(args, "action", "list").casefold() or "list"
        if action in {"list", "status", "statut"}:
            return format_tasks(self._background_tasks.list_tasks())
        if action in {"history", "historique"}:
            return format_tasks(self._background_tasks.list_tasks(include_finished=True))
        if action in {"result", "rapport", "résultat", "resultat"}:
            task_id = self._arg(args, "task_id") or self._arg(args, "id")
            return format_agent_result(self._background_tasks.agent_result(task_id))
        if action in {"cancel", "annuler", "delete"}:
            task_id = self._arg(args, "task_id") or self._arg(args, "id")
            return (
                f"Tâche {task_id} annulée."
                if self._background_tasks.cancel(task_id)
                else "Tâche active introuvable."
            )
        try:
            if action in {"watch_price", "price", "surveille_prix"}:
                url = self._arg(args, "url")
                if not url:
                    from actions.browser_control import current_browser_url

                    url = current_browser_url()
                if not url:
                    return (
                        "Je ne connais pas l'URL de cette page. Donne-moi son "
                        "adresse ou ouvre-la avec mon outil navigateur."
                    )
                target = args.get("target_price")
                task = self._background_tasks.add_price_watch(
                    url,
                    target_price=float(target) if target is not None else None,
                    interval_seconds=int(args.get("interval_minutes") or 15) * 60,
                    selector=self._arg(args, "selector"),
                    label=self._arg(args, "label"),
                )
                return f"Je surveille ce prix en arrière-plan ({task['id']})."
            if action in {"wait_build", "build", "wait_command"}:
                task = self._background_tasks.add_build_wait(
                    self._arg(args, "command_contains"),
                    message=self._arg(args, "message"),
                )
                return f"Je te préviendrai à la fin du build ({task['id']})."
            if action in {"wait_arrival", "arrival", "arrivee"}:
                task = self._background_tasks.add_arrival_wait(
                    self._arg(args, "message", "Tu es arrivé à la maison."),
                    radius_m=float(args.get("radius_m") or 250),
                    label=self._arg(args, "label", "maison"),
                )
                return f"Je te le rappellerai à l'arrivée ({task['id']})."
            if action in {"delegate", "ghost", "agent", "fantome", "fantôme"}:
                workspace = self._arg(args, "workspace") or str(BASE_DIR)
                task = self._background_tasks.add_agent_mission(
                    self._arg(args, "mission") or self._arg(args, "message"),
                    workspace=workspace,
                    timeout_minutes=int(args.get("timeout_minutes") or 60),
                    show_terminal=bool(args.get("show_terminal", False)),
                )
                return (
                    "Mode Agent Fantôme activé. Je continue de t'écouter pendant "
                    f"que le sous-agent travaille ({task['id']})."
                    + (" Son journal est visible dans Kitty." if args.get("show_terminal") else "")
                )
        except (TypeError, ValueError) as exc:
            return f"Impossible de créer cette veille : {exc}."
        return "Action de tâche de fond inconnue."

    @staticmethod
    def _background_card_title(task: dict) -> str:
        return f"Tâche en arrière-plan · {str(task.get('id') or '')[-8:]}"

    @staticmethod
    def _background_progress_bar(progress: int | None) -> str:
        if progress is None:
            return "[░░░░░░░░░░] En attente"
        value = max(0, min(100, int(progress)))
        filled = round(value / 10)
        return f"[{'█' * filled}{'░' * (10 - filled)}] {value}%"

    def _background_card_body(self, task: dict, *, detail: bool = False) -> str:
        spec = task.get("spec") if isinstance(task.get("spec"), dict) else {}
        state = task.get("state") if isinstance(task.get("state"), dict) else {}
        kind = str(task.get("kind") or "tâche")
        status = str(task.get("status") or "active")
        phase = str(state.get("phase") or "queued")
        mission = str(spec.get("mission") or spec.get("label") or spec.get("message") or kind)
        progress = state.get("progress") if kind == "agent" else None
        lines = [
            f"**{mission[:300]}**",
            f"État : **{phase if status == 'active' else status}**",
            self._background_progress_bar(progress),
        ]
        if detail:
            journal = str(state.get("log_tail") or "").strip()
            if journal:
                lines += ["", "**Sortie en temps réel**", "```text", journal[-6000:], "```"]
            elif kind == "agent":
                lines += ["", "L'agent prépare encore sa première sortie."]
            if task.get("summary"):
                lines += ["", "**Résultat**", str(task["summary"])]
            if task.get("report_path"):
                lines += ["", f"Rapport : `{task['report_path']}`"]
        return "\n".join(lines)

    def _render_background_task(self, task: dict) -> None:
        """Met à jour une carte persistante sans jamais bloquer le worker agy."""
        task_id = str(task.get("id") or "")
        if not task_id or not hasattr(self, "ui"):
            return
        title = self._background_card_title(task)
        status = str(task.get("status") or "active")
        if status != "active":
            # La carte de suivi ne doit vivre que pendant le travail. À la
            # fin, la sortie cyberpunk de CardManager libère la place pour la
            # tâche suivante ; le résultat arrive séparément dans le journal.
            self.ui.dismiss_cards("agent-task", title)
            self.ui.dismiss_cards("agent-task-detail", f"Détails · {task_id[-8:]}")
            getattr(self, "_background_cards", set()).discard(task_id)
            getattr(self, "_background_detail_cards", set()).discard(task_id)
            return
        body = self._background_card_body(task)
        shown = getattr(self, "_background_cards", set())
        if task_id not in shown:
            shown.add(task_id)
            self._background_cards = shown
            actions = [
                {
                    "label": "Détails",
                    "dismiss": False,
                    "callback": lambda ident=task_id: self._show_background_task_details(ident),
                },
                {
                    "label": "Annuler",
                    "callback": lambda ident=task_id: self._background_tasks.cancel(ident),
                },
            ]
            self.ui.show_card("agent-task", title, body, actions)
        else:
            self.ui.update_card("agent-task", title, body)
        if task_id in getattr(self, "_background_detail_cards", set()):
            self.ui.update_card("agent-task-detail", f"Détails · {task_id[-8:]}",
                                self._background_card_body(task, detail=True))

    def _show_background_task_details(self, task_id: str) -> None:
        service = getattr(self, "_background_tasks", None)
        if service is None:
            return
        task = next((item for item in service.list_tasks(include_finished=True)
                     if str(item.get("id")) == task_id), None)
        if task is None:
            return
        shown = getattr(self, "_background_detail_cards", set())
        title = f"Détails · {task_id[-8:]}"
        body = self._background_card_body(task, detail=True)
        if task_id in shown:
            self.ui.update_card("agent-task-detail", title, body)
            return
        shown.add(task_id)
        self._background_detail_cards = shown
        self.ui.show_card("agent-task-detail", title, body)
