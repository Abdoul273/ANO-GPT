"""core/thought_streamer.py — Gestionnaire de flux de pensée (Thinking Tokens) & VUI Multimodale.

Ce module résout le problème du silence de l'assistant lors des opérations longues
(recherche complexe, agent planner, réflexion Gemini 2.0 Flash Thinking / Claude 3.7 Thinking) :
1. Extraction asynchrone des tokens de pensée (Thinking Tokens) :
   - Flux Gemini 2.0 Flash Thinking (part.thought == True)
   - Flux Claude 3.7 Thinking (content_block_delta thinking_delta, thinking blocks)
   - Flux DeepSeek-R1 / OpenAI reasoning_content
   - Balises textuelles <thought>, <thinking>, <think> en streaming avec tampon partiel
   - Événements d'exécution d'outils et planification d'agents
2. Restitution multimodale :
   - Visuel : affichage en direct dans une bulle translucide 'Pensée en cours...' sous l'orbe
     avec texte gris doux défilant (micro-étapes nettoyées)
   - Audio : énonciation vocale courte et chuchotée à bas volume (ex: 'Un instant, j'analyse vos fichiers...')
     déclenchée UNIQUEMENT si l'opération dépasse 1.5 seconde
3. Filtrage du bruit mental :
   - Heuristiques de suppression du monologue interne, bégaiements, syntaxe JSON
   - Extraction des micro-étapes signifiantes
   - Synthèse de jalons clés (1 phrase max par phase) pour ne jamais submerger l'utilisateur
   - Annulation instantanée dès que la parole commence (barge-in / turn complete)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import logging
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("anogpt.thought_streamer")

# Une réflexion sans outil reçoit un court jalon après 0,75 s. Les opérations
# explicitement longues sont, elles, annoncées dès leur lancement ci-dessous.
DEFAULT_AUDIO_DELAY_SECONDS: float = 0.75

# Volume et débit pour la voix chuchotée / bas volume
WHISPER_VOLUME_FACTOR: float = 0.35
WHISPER_SPEED_FACTOR: float = 0.92
WHISPER_TTS_RATE: str = "-10%"
WHISPER_TTS_VOLUME: str = "-45%"


def mood_aware_waiting_phrase(base: str, tool_name: Optional[str] = None) -> str:
    """Donne une vraie présence vocale pendant une attente, selon le mode."""
    try:
        from core.personality_modes import active_mode
        mode = active_mode()
    except Exception:
        mode = None

    if mode is not None and str(mode) == "astro":
        if tool_name == "music_control":
            return "Attends deux secondes, je te déniche un son qui cogne."
        if tool_name == "download_music":
            return "Attends, je te choppe le son. Patienter, je te préviens dès que c'est dans Musique."
        if tool_name in {"web_search", "smart_search", "youtube_video"}:
            return "Bouge pas, je fouille ça. Le web rame un peu, ce feignant."
        if tool_name in {"agent_brain", "deep_think"}:
            return "Laisse-moi réfléchir deux secondes, je range le bordel dans ma tête."
        if tool_name in {"weather_report", "weather"}:
            return "Attends, je sors la tête par la fenêtre. Enfin, presque."
        if tool_name in {"find_nearby", "navigation"}:
            return "Bouge pas, je cale ça sur la carte."
        if tool_name in {"email", "gmail", "calendar"}:
            return "Deux secondes, je jette un œil à tes trucs."
        if tool_name in {"image_search"}:
            return "Attends, je te sors les photos. Pas de navigateur, promis."
        return "Bouge pas, je m'en occupe. Ça charge."
    if mode is not None and str(mode) == "majeur":
        if tool_name == "download_music":
            return "Un instant, Monsieur. Je lance le téléchargement, je vous préviens ensuite."
        return "Un instant, Monsieur. Je m'en occupe."
    if mode is not None and str(mode) == "coquin":
        if tool_name == "download_music":
            return "Patiente un instant. Je récupère le morceau, je te le dis dès que c'est prêt."
        return "Patiente un instant. Je m'en occupe."
    if tool_name == "download_music":
        return "Un instant, je lance le téléchargement. Je vous préviens dès que c'est dans Musique."
    return base


class ThoughtType(str, Enum):
    """Types d'événements de pensée."""
    TOKEN = "token"
    MICRO_STEP = "micro_step"
    KEY_MILESTONE = "key_milestone"
    TOOL_EXECUTION = "tool_execution"


@dataclass
class ThoughtEvent:
    """Événement extrait du flux de pensée."""
    text: str
    raw_tokens: str = ""
    thought_type: ThoughtType = ThoughtType.MICRO_STEP
    timestamp: float = field(default_factory=time.monotonic)
    source: str = "generic"
    tool_name: Optional[str] = None
    is_vocalized: bool = False


# ─────────────────────────────────────────────────────────────────────────────
# 1. Analyseur de flux de tokens de raisonnement (Reasoning Stream Parser)
# ─────────────────────────────────────────────────────────────────────────────

class ReasoningStreamParser:
    """Parseur haute performance de flux de pensée multi-fournisseurs.

    Extrait les tokens de réflexion depuis :
    - Gemini 2.0 Flash Thinking (`part.thought == True` ou `thought_delta`)
    - Claude 3.7 Thinking (`thinking_delta`, `thinking` block)
    - DeepSeek-R1 / OpenAI reasoning (`reasoning_content`)
    - Balises XML en streaming (<thought>, <thinking>, <think>) avec gestion
      des fragments de balises coupés entre plusieurs chunks réseau.
    """

    _TAG_PAIRS: List[Tuple[str, str]] = [
        ("<thought>", "</thought>"),
        ("<thinking>", "</thinking>"),
        ("<think>", "</think>"),
    ]

    def __init__(self) -> None:
        self._inside_tag: bool = False
        self._current_close_tag: str = ""
        self._tag_buffer: str = ""

    def reset(self) -> None:
        """Réinitialise l'état de l'automate à états du flux."""
        self._inside_tag = False
        self._current_close_tag = ""
        self._tag_buffer = ""

    def parse_gemini_part(self, part: Any) -> Optional[str]:
        """Extrait les tokens de pensée d'une partie Gemini 2.0 Flash Thinking."""
        if part is None:
            return None

        # 1. Attribut thought booléen natif google.genai
        if getattr(part, "thought", False) is True:
            text = getattr(part, "text", "")
            return text if isinstance(text, str) and text else None

        # 2. Attribut thought_delta ou type explicite
        td = getattr(part, "thought_delta", None)
        if isinstance(td, str) and td:
            return td

        # 3. Vérification si part est un dict (sérialisation JSON ou mock)
        if isinstance(part, dict):
            if part.get("thought") is True:
                text = part.get("text")
                return text if isinstance(text, str) and text else None
            td_val = part.get("thought_delta")
            if isinstance(td_val, str) and td_val:
                return td_val

        return None

    def parse_claude_chunk(self, chunk: Any) -> Optional[str]:
        """Extrait les tokens de pensée d'un fragment Claude 3.7 Thinking."""
        if chunk is None:
            return None

        # Objet SDK Anthropic ou dict compatible
        if isinstance(chunk, dict):
            c_type = chunk.get("type")
            if c_type == "content_block_delta":
                delta = chunk.get("delta", {})
                if delta.get("type") == "thinking_delta":
                    return delta.get("thinking") or None
            elif c_type == "content_block_start":
                block = chunk.get("content_block", {})
                if block.get("type") == "thinking":
                    return block.get("thinking") or None

            # DeepSeek-R1 / OpenAI / Ollama reasoning format
            choices = chunk.get("choices")
            if isinstance(choices, list) and choices:
                delta = choices[0].get("delta", {})
                if "thinking" in delta and delta["thinking"]:
                    return delta["thinking"]
                if "reasoning_content" in delta and delta["reasoning_content"]:
                    return delta["reasoning_content"]

        # Objet avec attributs (ex: chunk.delta.thinking)
        delta = getattr(chunk, "delta", None)
        if delta is not None:
            if getattr(delta, "type", "") == "thinking_delta":
                return getattr(delta, "thinking", None)
            if hasattr(delta, "thinking") and delta.thinking:
                return delta.thinking
            if hasattr(delta, "reasoning_content") and delta.reasoning_content:
                return delta.reasoning_content

        return None

    def parse_text_stream(self, chunk: str) -> Tuple[str, str]:
        """Scinde un fragment de texte brut en (thinking_tokens, regular_tokens).

        Gère les coupures de balises à cheval entre deux paquets réseau.
        """
        if not chunk:
            return "", ""

        # Préfixe avec le reliquat potentiel d'une balise en cours de détection
        text = self._tag_buffer + chunk
        self._tag_buffer = ""

        thought_parts: List[str] = []
        regular_parts: List[str] = []

        while text:
            if not self._inside_tag:
                # Cherche l'ouverture d'une des balises
                earliest_pos = -1
                found_open = ""
                found_close = ""

                for open_tag, close_tag in self._TAG_PAIRS:
                    pos = text.lower().find(open_tag)
                    if pos != -1 and (earliest_pos == -1 or pos < earliest_pos):
                        earliest_pos = pos
                        found_open = open_tag
                        found_close = close_tag

                if earliest_pos != -1:
                    # Texte régulier avant la balise
                    if earliest_pos > 0:
                        regular_parts.append(text[:earliest_pos])
                    self._inside_tag = True
                    self._current_close_tag = found_close
                    text = text[earliest_pos + len(found_open):]
                else:
                    # Vérifier si la fin du texte ressemble à un début partiel de balise
                    partial_match = False
                    for open_tag, _ in self._TAG_PAIRS:
                        for i in range(1, len(open_tag)):
                            if text.lower().endswith(open_tag[:i]):
                                self._tag_buffer = text[-i:]
                                text = text[:-i]
                                partial_match = True
                                break
                        if partial_match:
                            break
                    regular_parts.append(text)
                    text = ""
            else:
                # Nous sommes dans une balise de pensée, on cherche sa fermeture
                pos = text.lower().find(self._current_close_tag)
                if pos != -1:
                    thought_parts.append(text[:pos])
                    text = text[pos + len(self._current_close_tag):]
                    self._inside_tag = False
                    self._current_close_tag = ""
                else:
                    # Vérifier si la fin ressemble à un fragment partiel du tag de fermeture
                    partial_match = False
                    close_tag = self._current_close_tag
                    for i in range(1, len(close_tag)):
                        if text.lower().endswith(close_tag[:i]):
                            self._tag_buffer = text[-i:]
                            text = text[:-i]
                            partial_match = True
                            break
                    thought_parts.append(text)
                    text = ""

        return "".join(thought_parts), "".join(regular_parts)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Filtrage du bruit mental & Synthèse de jalons clés (Noise Filter)
# ─────────────────────────────────────────────────────────────────────────────

class ThoughtNoiseFilter:
    """Filtre le 'bruit mental' des modèles pour une restitution épurée.

    Les LLMs en mode réflexion débitent un flot verbeux de monologue intérieur :
    hésitations, répétitions, tests de syntaxe, balises JSON brutes.
    Ce filtre :
    - Élimine le bavardage méta et le monologue interne
    - Extrait et normalise les micro-étapes pour l'affichage visuel
    - Génère un jalon audio concis et poli (1 phrase max) pour la vocalisation
    """

    # Monologues internes et tics de raisonnement à nettoyer
    _FILLER_PATTERNS = [
        re.compile(r"^(okay|ok|hmm+|voyons|attends|alors|wait|let'?s see|let me see|let me check|let me think|i need to|i should|je dois|il faut|je vais)\b[,\.\s:]*", re.IGNORECASE),
        re.compile(r"^(so first|first,|tout d'abord|d'abord,|en premier lieu)\b[,\.\s:]*", re.IGNORECASE),
        re.compile(r"^(actually|en fait|wait no|non attendez)\b[,\.\s:]*", re.IGNORECASE),
    ]

    # Détection de code, JSON ou syntaxe brute
    _SYNTAX_PATTERN = re.compile(r"```.*?```|`.*?`|\{.*?\}|\[.*?\]", re.DOTALL)

    # Catégorisation thématique des actions
    _DOMAIN_RULES: List[Tuple[re.Pattern, str, str]] = [
        # (Pattern regex, Micro-étape visuelle, Jalon vocal audio)
        (
            re.compile(r"\b(file|fichier|document|pdf|read|lecture|recherche_fichier|indexer|dossier)\b", re.IGNORECASE),
            "Analyse et consultation des fichiers...",
            "Un instant, j'analyse vos fichiers...",
        ),
        (
            re.compile(r"\b(search|recherche|google|web|en ligne|duckduckgo|internet|source)\b", re.IGNORECASE),
            "Recherche d'informations sur le web...",
            "Un instant, je recherche sur le web...",
        ),
        (
            re.compile(r"\b(calendar|agenda|événement|planning|rendez-vous|disponibilité|calendrier)\b", re.IGNORECASE),
            "Consultation de votre agenda...",
            "Je consulte votre agenda...",
        ),
        (
            re.compile(r"\b(email|mail|gmail|message|boîte|courriel|contact)\b", re.IGNORECASE),
            "Vérification de vos messages et e-mails...",
            "Je vérifie vos e-mails...",
        ),
        (
            re.compile(r"\b(code|python|script|bug|debug|erreur|syntaxe|fonction|terminal)\b", re.IGNORECASE),
            "Analyse du code source et débogage...",
            "Un instant, j'examine le code...",
        ),
        (
            re.compile(r"\b(system|système|batterie|processus|hyprland|volume|disque|paramètre)\b", re.IGNORECASE),
            "Vérification de l'état système...",
            "Un instant, je vérifie les paramètres système...",
        ),
        (
            re.compile(r"\b(map|carte|itinéraire|navigation|gps|distance|trajet|lieu)\b", re.IGNORECASE),
            "Calcul de l'itinéraire et repérage...",
            "Je consulte la carte pour l'itinéraire...",
        ),
        (
            re.compile(r"\b(plan|étape|stratégie|synthèse|résumé|structure)\b", re.IGNORECASE),
            "Planification et structuration de la réponse...",
            "Un instant, je structure votre réponse...",
        ),
    ]

    # Dictionnaire de correspondance directe pour les noms d'outils ANO-GPT
    _TOOL_MAPPING: Dict[str, Tuple[str, str]] = {
        "visual_recognition": ("Reconnaissance visuelle...", "Je prends la photo et je regarde de près, un instant."),
        "music_recognition": ("Reconnaissance musicale...", "J'écoute, quelques secondes."),
        "file_search": ("Recherche de fichiers locaux...", "Un instant, je recherche dans vos fichiers..."),
        "file_processor": ("Traitement du document...", "Un instant, je traite le fichier..."),
        "web_search": ("Recherche web en cours...", "Un instant, je vérifie sur le web..."),
        "smart_search": ("Recherche avancée...", "Je recherche les informations..."),
        "calendar": ("Consultation de votre agenda...", "Je consulte votre calendrier..."),
        "email": ("Vérification de votre messagerie...", "Je vérifie vos e-mails..."),
        "contacts": ("Recherche dans vos contacts...", "Je cherche dans vos contacts..."),
        "code_helper": ("Analyse du code informatique...", "J'analyse le code source..."),
        "auto_debug": ("Diagnostic et débogage automatique...", "J'examine l'erreur pour la corriger..."),
        "system_status": ("Inspection des ressources système...", "Je vérifie l'état du système..."),
        "computer_settings": ("Réglage des paramètres...", "Je règle les paramètres demandés..."),
        "navigation": ("Calcul de l'itinéraire...", "Je consulte l'itinéraire..."),
        "find_nearby": ("Recherche des lieux à proximité...", "Je cherche les lieux aux alentours..."),
        "weather": ("Consultation des prévisions météo...", "Je vérifie les prévisions météo..."),
        "download_music": ("Téléchargement du morceau...", "Un instant, je récupère le morceau..."),
        "tiktok_tracker": ("Lecture du profil TikTok...", "Je regarde ton TikTok..."),
        "tiktok_coach": ("Analyse TikTok en cours...", "Je regarde ça de près..."),
        "agent_brain": ("Réflexion approfondie en cours...", "Un instant, je réfléchis à votre demande..."),
        "second_brain": ("Consultation de la mémoire persistante...", "Je consulte mes souvenirs enregistrés..."),
        "memory_search": ("Recherche dans la mémoire...", "Je cherche dans vos notes..."),
    }

    @classmethod
    def clean_raw_thought(cls, text: str) -> str:
        """Nettoie le texte brut des tokens de réflexion."""
        if not text:
            return ""

        # 1. Retrait des balises de code ou JSON
        cleaned = cls._SYNTAX_PATTERN.sub(" ", text)

        # 2. Remplacement des sauts de ligne multiples et espaces superflus
        cleaned = re.sub(r"\s+", " ", cleaned).strip()

        # 3. Retrait des tics de langage initiaux
        for pat in cls._FILLER_PATTERNS:
            cleaned = pat.sub("", cleaned).strip()

        return cleaned

    @classmethod
    def extract_micro_step(cls, text: str, max_chars: int = 85) -> str:
        """Extrait une micro-étape lisible et concise pour le HUD visuel."""
        cleaned = cls.clean_raw_thought(text)
        if not cleaned:
            return "Pensée en cours..."

        # Vérification par règles de domaine
        for pat, visual_label, _ in cls._DOMAIN_RULES:
            if pat.search(cleaned):
                return visual_label

        # Si le texte propre est une phrase courte et propre, l'adapter
        # Extraire la première phrase
        first_sentence = re.split(r"[.!?\n]", cleaned)[0].strip()
        if len(first_sentence) > 8:
            formatted = first_sentence[0].upper() + first_sentence[1:]
            if len(formatted) > max_chars:
                formatted = formatted[:max_chars - 3].rstrip() + "..."
            elif not formatted.endswith((".", "...", "…")):
                formatted += "..."
            return formatted

        return "Analyse de la demande en cours..."

    @classmethod
    def get_tool_milestone(cls, tool_name: str) -> Tuple[str, str]:
        """Retourne (micro_étape_visuelle, jalon_vocal_audio) pour un outil."""
        normalized = (tool_name or "").lower().strip()
        if normalized in cls._TOOL_MAPPING:
            return cls._TOOL_MAPPING[normalized]

        # Règle générique
        clean_name = normalized.replace("_", " ").title()
        return (f"Exécution de {clean_name}...", "Un instant, j'exécute l'action...")

    @classmethod
    def generate_audio_milestone(cls, text: str, tool_name: Optional[str] = None) -> str:
        """Produit exactement 1 phrase courte pour la vocalisation chuchotée."""
        if tool_name:
            _, audio_phrase = cls.get_tool_milestone(tool_name)
            return audio_phrase

        cleaned = cls.clean_raw_thought(text)
        for pat, _, audio_phrase in cls._DOMAIN_RULES:
            if pat.search(cleaned):
                return audio_phrase

        return "Un instant, je vérifie votre demande..."


# ─────────────────────────────────────────────────────────────────────────────
# 3. Moteur ThoughtStreamer (Orchestrateur asynchrone VUI)
# ─────────────────────────────────────────────────────────────────────────────

class ThoughtStreamer:
    """Orchestrateur multimodal de streaming de pensée pour ANO-GPT.

    Rôles :
    1. Collecte et analyse en direct les tokens de réflexion (Gemini / Claude / DeepSeek / Outils).
    2. Alerte l'UI via `ui_callback` avec des micro-étapes propres et fluides.
    3. Si la réflexion ou l'action dure plus de `audio_delay_sec` (1.5s par défaut),
       vocalise un jalon audio court et chuchoté à bas volume.
    4. Garantit un silence absolu pour les requêtes rapides (< 1.5s).
    5. Coupe instantanément tout chuchotement dès que le modèle commence à parler
       ou dès que le tour se termine.
    """

    def __init__(
        self,
        session_manager: Any = None,
        ui_callback: Optional[Callable[[str, bool], None]] = None,
        audio_delay_sec: float = DEFAULT_AUDIO_DELAY_SECONDS,
    ) -> None:
        self._session_manager = session_manager
        self._ui_callback = ui_callback
        self._audio_delay_sec = float(audio_delay_sec)

        self._parser = ReasoningStreamParser()
        self._filter = ThoughtNoiseFilter()

        self._lock = threading.RLock()
        self._is_thinking: bool = False
        self._thinking_start_time: float = 0.0
        self._vocalized_for_turn: bool = False
        self._current_micro_step: str = ""
        self._accumulated_thought: List[str] = []
        self._current_tool: Optional[str] = None

        # Minuteur pour le déclenchement audio à 1.5s
        self._audio_timer: Optional[threading.Timer] = None

        # Suivi du lecteur TTS chuchoté pour coupure immédiate
        self._active_tts_stop_cb: Optional[Callable[[], None]] = None

    @property
    def is_thinking(self) -> bool:
        """Indique si une phase de réflexion ou d'exécution d'outil est en cours."""
        with self._lock:
            return self._is_thinking

    @property
    def current_step(self) -> str:
        """Retourne la micro-étape textuelle en cours."""
        with self._lock:
            return self._current_micro_step

    def set_ui_callback(self, cb: Optional[Callable[[str, bool], None]]) -> None:
        """Définit le rappel pour l'affichage visuel sous l'orbe."""
        with self._lock:
            self._ui_callback = cb

    def set_session_manager(self, session_manager: Any) -> None:
        """Associe le SessionManager pour coordination TTS et WebSocket."""
        with self._lock:
            self._session_manager = session_manager

    # ── Gestion du cycle de vie de la pensée ─────────────────────────────────

    def start_thinking(self, initial_step: str = "Pensée en cours...", tool_name: Optional[str] = None) -> None:
        """Enclenche une session de réflexion et arme le minuteur audio de 1.5s."""
        with self._lock:
            if not self._is_thinking:
                self._is_thinking = True
                self._thinking_start_time = time.monotonic()
                self._vocalized_for_turn = False
                self._accumulated_thought.clear()
                self._parser.reset()
                self._arm_audio_timer()

            self._current_tool = tool_name
            self._current_micro_step = initial_step

        self._notify_ui(initial_step, is_active=True)

    def feed_thought_token(self, token: str, source: str = "gemini") -> None:
        """Injecte un token de pensée brut provenant du modèle."""
        if not token:
            return

        with self._lock:
            if not self._is_thinking:
                self.start_thinking("Pensée en cours...")

            self._accumulated_thought.append(token)
            full_thought = "".join(self._accumulated_thought)
            step = self._filter.extract_micro_step(full_thought)
            self._current_micro_step = step

        self._notify_ui(step, is_active=True)

    def feed_gemini_part(self, part: Any) -> bool:
        """Tente d'extraire et d'injecter un token depuis une Part Gemini 2.0."""
        text = self._parser.parse_gemini_part(part)
        if text:
            self.feed_thought_token(text, source="gemini_thinking")
            return True
        return False

    def feed_claude_chunk(self, chunk: Any) -> bool:
        """Tente d'extraire et d'injecter un token depuis un chunk Claude 3.7."""
        text = self._parser.parse_claude_chunk(chunk)
        if text:
            self.feed_thought_token(text, source="claude_thinking")
            return True
        return False

    def feed_stream_chunk(self, text_chunk: str) -> Tuple[str, str]:
        """Parse un chunk texte scindé en (thought_tokens, regular_tokens)."""
        thought_tokens, regular_tokens = self._parser.parse_text_stream(text_chunk)
        if thought_tokens:
            self.feed_thought_token(thought_tokens, source="text_stream")
        return thought_tokens, regular_tokens

    def feed_tool_start(self, tool_name: str, args: Any = None) -> None:
        """Notifie le démarrage d'une exécution d'outil et annonce les attentes longues."""
        visual_step, _ = self._filter.get_tool_milestone(tool_name)
        self.start_thinking(initial_step=visual_step, tool_name=tool_name)
        # Ces opérations sont suffisamment longues et visibles pour mériter
        # une présence vocale tout de suite. Les petites actions restent
        # silencieuses ; elles ne doivent pas interrompre une conversation.
        long_operations = {
            "agent_brain", "deep_think", "simulate_decision", "web_search",
            "smart_search", "image_search", "youtube_video", "file_search",
            "screen_analysis", "analyze_screen", "analyze_image",
            "download_music", "visual_recognition", "music_recognition",
            "tiktok_coach", "screen_process", "generate_video", "generate_image",
        }
        if tool_name not in long_operations:
            return
        with self._lock:
            if self._vocalized_for_turn:
                return
            _visual, phrase = self._filter.get_tool_milestone(tool_name)
            phrase = mood_aware_waiting_phrase(phrase, tool_name)
            self._vocalized_for_turn = True
        self.vocalize_whisper(phrase)

    def feed_tool_end(self, tool_name: str, result: Any = None) -> None:
        """Notifie la fin de l'exécution d'un outil."""
        with self._lock:
            if self._current_tool == tool_name:
                self._current_tool = None
                # Met à jour la micro-étape vers la synthèse
                step = "Synthèse des résultats..."
                self._current_micro_step = step
                self._notify_ui(step, is_active=True)

    def on_speaking_start(self) -> None:
        """Appelé dès que le modèle commence à émettre sa réponse réelle.

        Action immédiate :
        1. Annule le minuteur audio s'il n'avait pas encore tiré.
        2. Interrompt tout chuchotement en cours.
        3. Masque la bulle visuelle sous l'orbe.
        """
        with self._lock:
            self._disarm_audio_timer()
            self._stop_active_whisper()
            self._is_thinking = False
            self._accumulated_thought.clear()
            self._current_tool = None

        self._notify_ui("", is_active=False)

    def on_turn_complete(self) -> None:
        """Clôture du tour de dialogue : réinitialise proprement l'état."""
        self.on_speaking_start()
        with self._lock:
            self._vocalized_for_turn = False
            self._parser.reset()

    def interrupt(self) -> None:
        """Interruption utilisateur immédiate (barge-in)."""
        self.on_turn_complete()

    def reset(self) -> None:
        """Réinitialise totalement le composant."""
        self.on_turn_complete()

    # ── Minuteur et Vocalisation Chuchotée ───────────────────────────────────

    def _arm_audio_timer(self) -> None:
        """Arme le minuteur déclenchant la vocalisation à 1.5s."""
        self._disarm_audio_timer()
        t = threading.Timer(self._audio_delay_sec, self._on_audio_timer_fired)
        t.daemon = True
        self._audio_timer = t
        t.start()

    def _disarm_audio_timer(self) -> None:
        """Désarme le minuteur audio."""
        if self._audio_timer is not None:
            try:
                self._audio_timer.cancel()
            except Exception:
                pass
            self._audio_timer = None

    def _on_audio_timer_fired(self) -> None:
        """Callback exécuté si la réflexion dépasse 1.5 seconde."""
        phrase_to_speak = ""
        with self._lock:
            if not self._is_thinking:
                return
            if self._vocalized_for_turn:
                return

            # Calcul du temps écoulé effectif
            elapsed = time.monotonic() - self._thinking_start_time
            if elapsed < (self._audio_delay_sec - 0.05):
                return

            full_thought = "".join(self._accumulated_thought)
            phrase_to_speak = self._filter.generate_audio_milestone(
                full_thought, tool_name=self._current_tool
            )
            phrase_to_speak = mood_aware_waiting_phrase(
                phrase_to_speak, self._current_tool
            )
            self._vocalized_for_turn = True

        if phrase_to_speak:
            logger.info("🗣️ [ThoughtStreamer] Vocalisation chuchotée (>1.5s) : %s", phrase_to_speak)
            self.vocalize_whisper(phrase_to_speak)

    def vocalize_whisper(self, phrase: str) -> None:
        """Énonce une phrase courte et chuchotée à bas volume sans bloquer."""
        if not phrase:
            return
        # Avec Gemini Live, la voix audible doit être sa voix native choisie
        # par le mode (Charon pour « Majeur d'homme »). Le vieux repli EdgeTTS
        # utilisait une voix anglaise différente pour les messages d'attente.
        # La carte de progression reste visible ; on ne mélange plus les voix.
        try:
            from core.session_manager import _voice_engine_settings
            if _voice_engine_settings().get("voice_provider", "gemini") == "gemini":
                return
        except Exception:
            return

        def _run_whisper() -> None:
            try:
                # 1. Tenter via le moteur TTS local du SessionManager
                sm = self._session_manager
                tts_player = getattr(sm, "tts_player", None) if sm else None

                if tts_player is not None and hasattr(tts_player, "speak"):
                    # Ajuster temporairement les paramètres pour un chuchotement
                    cfg = getattr(tts_player, "_cfg", None)
                    orig_volume = getattr(cfg, "volume", "0%") if cfg else None
                    orig_rate = getattr(cfg, "rate", "0%") if cfg else None

                    if cfg is not None:
                        if hasattr(cfg, "volume"):
                            cfg.volume = WHISPER_TTS_VOLUME
                        if hasattr(cfg, "rate"):
                            cfg.rate = WHISPER_TTS_RATE

                    with self._lock:
                        self._active_tts_stop_cb = getattr(tts_player, "stop", None)

                    try:
                        tts_player.speak(phrase)
                    finally:
                        if cfg is not None:
                            if orig_volume is not None and hasattr(cfg, "volume"):
                                cfg.volume = orig_volume
                            if orig_rate is not None and hasattr(cfg, "rate"):
                                cfg.rate = orig_rate
                        with self._lock:
                            self._active_tts_stop_cb = None
                    return

                # 2. Repli : synthèse Kokoro ou EdgeTTS directe modulée si disponible
                try:
                    from core.tts import create_tts_player
                    config = {
                        "tts_engine": "edgetts",
                        "tts_volume": WHISPER_TTS_VOLUME,
                        "tts_rate": WHISPER_TTS_RATE,
                        "tts_speed": WHISPER_SPEED_FACTOR,
                    }
                    player = create_tts_player(config)
                    with self._lock:
                        self._active_tts_stop_cb = player.stop
                    try:
                        player.speak(phrase)
                    finally:
                        with self._lock:
                            self._active_tts_stop_cb = None
                    return
                except Exception as e:
                    logger.debug("Repli TTS direct non disponible : %s", e)

            except Exception as exc:
                logger.warning("Erreur lors de la vocalisation chuchotée : %s", exc)
            finally:
                with self._lock:
                    self._active_tts_stop_cb = None

        from core.thread_pool import get_thread_pool
        get_thread_pool().submit(
            "network-heavy", _run_whisper,
            task_name="thought-whisper-speech", stall_timeout=120.0,
        )

    def _stop_active_whisper(self) -> None:
        """Interrompt la synthèse vocale chuchotée immédiatement."""
        stop_fn = None
        with self._lock:
            stop_fn = self._active_tts_stop_cb
            self._active_tts_stop_cb = None

        if stop_fn:
            try:
                stop_fn()
            except Exception:
                pass

    def _notify_ui(self, text: str, is_active: bool) -> None:
        """Transmet l'état et le texte à l'interface graphique de manière sûre."""
        cb = self._ui_callback
        if cb is not None:
            try:
                cb(text, is_active)
            except Exception as exc:
                logger.debug("Erreur notification UI ThoughtStreamer : %s", exc)

    def get_state(self) -> Dict[str, Any]:
        """Retourne un instantané diagnostique de l'état du streamer."""
        with self._lock:
            return {
                "is_thinking": self._is_thinking,
                "current_step": self._current_micro_step,
                "vocalized_for_turn": self._vocalized_for_turn,
                "current_tool": self._current_tool,
                "accumulated_tokens_count": len(self._accumulated_thought),
                "elapsed_seconds": (
                    time.monotonic() - self._thinking_start_time
                    if self._is_thinking else 0.0
                ),
            }


# ─────────────────────────────────────────────────────────────────────────────
# Instance Singleton / Fabrique
# ─────────────────────────────────────────────────────────────────────────────

_GLOBAL_THOUGHT_STREAMER: Optional[ThoughtStreamer] = None
_GLOBAL_LOCK = threading.Lock()


def get_thought_streamer(
    session_manager: Any = None,
    ui_callback: Optional[Callable[[str, bool], None]] = None,
    audio_delay_sec: float = DEFAULT_AUDIO_DELAY_SECONDS,
) -> ThoughtStreamer:
    """Récupère ou initialise l'instance globale de ThoughtStreamer."""
    global _GLOBAL_THOUGHT_STREAMER
    with _GLOBAL_LOCK:
        if _GLOBAL_THOUGHT_STREAMER is None:
            _GLOBAL_THOUGHT_STREAMER = ThoughtStreamer(
                session_manager=session_manager,
                ui_callback=ui_callback,
                audio_delay_sec=audio_delay_sec,
            )
        else:
            if session_manager is not None:
                _GLOBAL_THOUGHT_STREAMER.set_session_manager(session_manager)
            if ui_callback is not None:
                _GLOBAL_THOUGHT_STREAMER.set_ui_callback(ui_callback)
        return _GLOBAL_THOUGHT_STREAMER
