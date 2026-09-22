"""Déclarations des outils envoyées au modèle Live.

Données pures, sans import : ``core/tool_stats.py`` les lit par analyse
syntaxique, et les tests par simple lecture du texte. Le répartiteur
(``core/tool_dispatcher.py``) les ré-exporte pour les appelants existants.
"""
from __future__ import annotations

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
            "pass the command in the 'command' parameter and the requested workspace together. "
            "Never use browser tools to type into a terminal. "
            "Report only the outcome confirmed by the tool; an error is not a successful launch. "
            "If the app was ALREADY opened moments ago (e.g. 'ouvre kitty' then 'tape la commande claude'), "
            "do NOT open it again: call computer_control with action='type', text='<command>', "
            "window='<app>', press_enter=true — the existing window is reused. "
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
            "dessiner ou imaginer une IMAGE inédite. JAMAIS quand il dit « vidéo », « clip » "
            "ou « film » : c'est generate_video (ou tiktok_coach action='viral_video' pour TikTok)."
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
            "Utilise-le quand l'utilisateur demande de créer, générer, faire ou animer une "
            "vidéo inédite — une demande de VIDÉO ne se traite jamais avec generate_image ni par un "
            "simple script écrit. Pour « une vidéo virale pour mon TikTok », préfère tiktok_coach "
            "action='viral_video' qui conçoit le concept d'après le compte puis lance cette génération. "
            "Ne l'utilise jamais pour chercher une vidéo existante "
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
        "name": "whatsapp_control",
        "description": (
            "Contrôle ZapZap, le client WhatsApp installé : état, ouverture d'une "
            "conversation et composition. Pour envoyer, utilise send_message avec "
            "confirmation utilisateur (action='send' rappelle cette règle). "
            "Ne prétends jamais qu'une livraison est confirmée : ZapZap ne fournit pas cette API."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "status | open | compose | send"},
                "receiver": {"type": "STRING", "description": "Contact ANO-GPT ou numéro international +indicatif"},
                "message": {"type": "STRING", "description": "Texte pour compose ou send"},
            },
            "required": ["action"],
        },
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
            "Actions: 'open' (ONLY shows the live view — never take a photo nor call "
            "visual_recognition/screen_process afterwards unless the user explicitly asks "
            "a question about what the camera shows), 'photo' (save a still), 'video_start', "
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
            "the user's own position — the result already names the precise "
            "neighbourhood/quartier, not just the city. A follow-up like 'dans quel "
            "quartier ?' right after showing the user's own position is answered from "
            "THAT result, already in context — never call web_search for it, a search "
            "engine cannot know where the user is standing right now. "
            "Speak about what's shown on the map in your reply (distance, address, etc.) "
            "— don't just open it silently. "
            "Two render styles exist, switchable anytime by voice: 'carte' (street-level "
            "Leaflet map — the default) and 'globe' (rotating 3D world view). Only set "
            "'view' when the user explicitly names one ('montre ça en globe', 'passe en "
            "carte', 'vue satellite/globe') — otherwise omit it and the map keeps "
            "whichever style is already on screen."
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
                "view": {
                    "type": "STRING",
                    "description": "'carte' for the street-level map, 'globe' for the 3D world view. "
                                    "Omit to keep the current style on screen.",
                },
            },
            "required": []
        }
    },
    {
        "name": "show_country_info",
        "description": (
            "Displays a floating info card for a country on the map/globe — capital, "
            "population, currency, languages, timezone, current weather at the capital. "
            "Works for ANY country in the world, not just Guinea. Use when the user asks "
            "about a country (« montre-moi les infos sur le Japon », « c'est quoi la "
            "capitale du Brésil », « météo au Sénégal en ce moment »). Centers the map on "
            "that country and speaks the key facts back — don't just open it silently."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "country": {
                    "type": "STRING",
                    "description": "Country name, in French or English (e.g. 'Japon', 'Brésil', "
                                    "'Sénégal'). Omit for Guinée, the default.",
                },
                "view": {
                    "type": "STRING",
                    "description": "'carte' or 'globe'. Omit to keep the current style on screen.",
                },
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
            "avec anticipation (seuils 500m / 150m / immédiat) synchronisé avec le GPS du smartphone Android (ANO-Remote). "
            "N'appelle JAMAIS web_search pour une demande de guidage/itinéraire, même après un premier essai "
            "infructueux ou si l'utilisateur répète sa demande à l'identique — rappelle navigate, pas une recherche."
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
            "Coach TikTok personnel (comme Blow Up) pour aider l'utilisateur à percer. Le compte "
            "mélange plusieurs genres : coulisses d'ANO-GPT ET sketches générés par IA (personnages, "
            "fruits animés, humour) — chaque vidéo est jugée selon les codes de son genre, jamais "
            "sur son lien avec ANO-GPT. "
            "Actions : 'diagnose' — « pourquoi ma vidéo n'a pas marché », « pourquoi elle est bloquée à "
            "300 vues », « pourquoi si peu de likes sur ma dernière vidéo » : lit les chiffres, télécharge "
            "la vidéo, la visionne et explique les vraies causes + quoi changer (query = quelle vidéo : "
            "« la dernière », « l'avant-dernière », « la plus vue », des mots du titre, ou une URL). "
            "'review' — bilan du compte : ce qui marche, ce qui bloque, plan et idées de vidéos. "
            "'list' — « qu'est-ce que j'ai à poster », « quelles vidéos sont prêtes », « je veux poster » : "
            "liste les vidéos du dossier ~/Vidéos/ANO-GPT/TIKTOK et demande laquelle publier. "
            "'draft' — « analyse cette vidéo avant que je la poste », « la 2 », « la dernière du dossier » : "
            "visionne le fichier choisi (query = numéro, ordinal, mots du nom ou chemin ; par défaut "
            "la plus récente du dossier TikTok), visionne la vidéo entière et des images repères HD ; "
            "il donne un verdict publie/corrige fondé sur des timecodes, une grille accroche/lisibilité/"
            "rythme/son/chute/boucle, les blocages P0, les retouches P1/P2 et trois accroches alternatives "
            "propres à cette vidéo — jamais des conseils génériques. Il prépare aussi la description, les "
            "hashtags, le son, la couverture, le commentaire à épingler et la meilleure heure (note = "
            "précisions de l'utilisateur sur son intention). "
            "'best_time' — meilleure heure pour poster. diagnose et draft lancent le visionnage EN FOND "
            "et rendent tout de suite un premier constat à dire ; l'avis complet est annoncé tout seul "
            "environ une minute plus tard : ne relance pas l'outil, ne dis pas que c'est fini. Après un "
            "diagnostic terminé, l'utilisateur se voit proposer un rapport complet. S'il répond oui ou "
            "demande le fichier, appelle tiktok_coach avec action='report' (query facultative) : cela crée "
            "un .md ultra-complet (diagnostic, corrections, plan de montage, et deux prompts vidéo prêts à coller : "
            "Grok Imagine 15 s et Gemini Veo 10 s) dans ~/Documents/ANO-GPT/Diagnostics TikTok et L'OUVRE aussitôt dans "
            "Markdown Studio (ne jamais l'ouvrir toi-même via shell_exec ou un éditeur). Ne crée jamais ce "
            "fichier avant cet accord explicite. « Ouvre le rapport » ⇒ action='open_report'. "
            "« Génère-moi / fais-moi une vidéo (virale) pour mon TikTok », « crée une vidéo qui va percer » ⇒ "
            "action='viral_video' (query = contrainte éventuelle) : conçoit le concept d'après le compte ET "
            "produit la vidéo avec Sora — c'est une VIDÉO qui est demandée, jamais une image ni un simple script."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "diagnose | report (après oui explicite, crée ET ouvre le .md) | open_report | viral_video (concept + génération Sora) | review | list | draft | best_time"},
                "query": {"type": "STRING", "description": "Vidéo visée (diagnose), fichier (draft : numéro de la liste, ordinal, mots du nom, chemin) ou idée/contrainte (viral_video), tel que dit"},
                "seconds": {"type": "INTEGER", "description": "viral_video : durée de la vidéo (4, 8 ou 12 s ; défaut 12)"},
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
                "to": {"type": "STRING", "description": "send: recipient address or a saved contact name (required for send)"},
                "body": {"type": "STRING", "description": "send/reply: the message text to send, written out in full"},
                "cc": {"type": "STRING", "description": "send: optional carbon-copy addresses"},
                "query": {"type": "STRING", "description": "Natural-language request, native Gmail query (e.g. 'from:alice newer_than:30d'), or displayed number for read"},
                "id": {"type": "STRING", "description": "Gmail message ID or displayed result number for read"},
                "max_results": {"type": "INTEGER", "description": "Number of messages, from 1 to 100 (default 10)"},
                "from": {"type": "STRING", "description": "Exact sender name, address, or domain filter"},
                "to_filter": {"type": "STRING", "description": "search: exact recipient name or address filter (never the send recipient — that is `to`)"},
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
                "engine":      {"type": "STRING", "description": "Search engine: google | bing | yandex (default: google)"},
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
            "Performs forensic live debugging with the configured brain (Azure if available, else the "
            "selected provider): exact error parsing, source correlation, evidence, confidence, "
            "safe verification commands and an optional validated unified patch. "
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
        "name": "visual_recognition",
        "description": (
            "Reconnaissance de PERSONNES et d'OBJETS montrés à la caméra (ou à l'écran) avec mémoire "
            "permanente des visages. OBLIGATOIRE pour : « c'est qui ? », « tu le/la connais ? », "
            "« qui est cette personne », « c'est quoi ça / cet objet ? », « regarde ce que je te montre », "
            "« retiens son visage », « c'est Karim, mon frère », « c'est moi », « oublie X », "
            "« qui connais-tu ? », « prends une photo et dis-moi qui c'est / ce que c'est ». "
            "JAMAIS sur un simple « ouvre la caméra » (c'est camera_control, sans photo) : "
            "seulement quand l'utilisateur pose une question sur ce qu'il montre. "
            "AVANT l'appel, dis une phrase courte du type « Je prends la photo et je regarde » puis appelle "
            "l'outil UNE SEULE FOIS ; ne le rappelle jamais pour la même question (la photo est déjà prise). "
            "action='identify' (défaut) : ouvre la caméra si besoin (ne pas appeler camera avant), PREND LA "
            "PHOTO, l'enregistre et l'affiche à l'écran, puis reconnaît les visages avec la "
            "mémoire locale (connu ⇒ nom + lien ; inconnu ⇒ dossier en attente, DEMANDE qui c'est) et, "
            "sans visage, identifie l'objet (Gemini et Azure en parallèle, réponse en quelques secondes) ; recherche web seulement si search=true ou si la question parle de prix/avis/infos. "
            "Quand l'utilisateur répond au « c'est qui ? » ⇒ action='remember_person' avec name, relation "
            "(frère, collègue, amie…), pending_id du dossier et notes éventuelles ; name='moi' pour "
            "l'utilisateur lui-même. action='remember_object' (name, notes) retient le dernier objet identifié. "
            "action='forget_person' (name), 'update_person' (name, new_name, relation, notes, alias), "
            "'list_people', 'watch' / 'stop_watch' (annonce qui apparaît à la caméra), 'status'. "
            "Le résultat est un compte-rendu terminé : réponds à partir de lui, ne rappelle pas l'outil."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "identify (défaut) | remember_person | remember_object | forget_person | update_person | list_people | watch | stop_watch | status"},
                "source": {"type": "STRING", "description": "camera (défaut) | screen"},
                "expect": {"type": "STRING", "description": "auto (défaut) | person | object — ce que l'utilisateur montre, si évident"},
                "question": {"type": "STRING", "description": "La question exacte de l'utilisateur sur ce qu'il montre"},
                "name": {"type": "STRING", "description": "Nom de la personne / de l'objet (remember, forget, update)"},
                "relation": {"type": "STRING", "description": "Lien avec l'utilisateur : frère, mère, collègue, ami d'enfance…"},
                "notes": {"type": "STRING", "description": "Détail à retenir sur la personne ou l'objet"},
                "pending_id": {"type": "STRING", "description": "Dossier d'attente cité dans le compte-rendu (V1, V2…)"},
                "new_name": {"type": "STRING", "description": "Pour update_person : nouveau nom"},
                "alias": {"type": "STRING", "description": "Pour update_person : surnom supplémentaire"},
                "is_owner": {"type": "BOOLEAN", "description": "Vrai si le visage est celui de l'utilisateur"},
                "search": {"type": "BOOLEAN", "description": "identify : vrai si l'utilisateur veut aussi des infos en ligne (prix, avis, où acheter) — sinon la réponse est immédiate"},
            },
            "required": [],
        },
    },
    {
        "name": "music_recognition",
        "description": (
            "Reconnaît la musique qui joue EN CE MOMENT (Shazam intégré) : « tu connais cette musique ? », "
            "« c'est quoi cette chanson », « qui chante ça », « c'est quel titre », « shazam ». "
            "action='identify' (défaut) : lit d'abord le lecteur en cours (Spotify, mpv, navigateur), sinon "
            "écoute ~10 s la sortie audio du PC puis le micro, compare l'empreinte à la base Shazam et donne "
            "titre, artiste, album, année, genre, pochette. Réponds AVANT l'appel par une phrase courte du "
            "type « J'écoute » puis reste silencieux : l'outil enregistre le son pendant quelques secondes. "
            "Rends le résultat tel quel (il se termine par la proposition Spotify/YouTube). "
            "action='play' avec target='spotify'|'youtube'|'local' : lance la dernière musique reconnue "
            "(« oui, sur Spotify », « mets-la », « lance-la »). action='history' : dernières reconnues."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "identify (défaut) | play | history"},
                "target": {"type": "STRING", "description": "Pour play : spotify (défaut) | youtube | local"},
                "listen_only": {"type": "BOOLEAN", "description": "Ignorer le lecteur en cours et écouter vraiment le son (ex. musique dans la pièce)"},
            },
            "required": [],
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
    "name": "capability_guide",
    "description": (
        "OBLIGATOIRE dès que l'utilisateur demande tes compétences, ce que tu "
        "peux faire, tes fonctionnalités, tes outils, un guide d'utilisation, "
        "ou de noter / ouvrir / mettre à jour ce guide. Ne récite JAMAIS la "
        "liste de mémoire et ne l'invente pas. action='brief' (défaut) : "
        "résumé parlé des meilleures fonctions, puis la question de tout noter "
        "dans un Markdown. action='write' si l'utilisateur accepte, ou demande "
        "d'écrire / rafraîchir le fichier. action='open' pour ouvrir le guide "
        "déjà prêt. Le fichier n'est recréé que s'il manque ou n'est plus à "
        "jour. Passe toujours la phrase entendue dans query."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "brief (défaut) | write | open | status",
            },
            "query": {
                "type": "STRING",
                "description": "Phrase exacte de l'utilisateur",
            },
            "open_after": {
                "type": "BOOLEAN",
                "description": "Ouvrir le fichier après écriture (défaut : oui s'il vient d'être écrit)",
            },
        },
        "required": [],
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
