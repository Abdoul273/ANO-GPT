"""Gestionnaire de conversation continue pour ANO-GPT.

Ce module maintient la fenêtre d'écoute ouverte après chaque réponse de
l'assistant sans nécessiter la répétition du mot d'activation (« ANO »).

Règles de fonctionnement :
1. Après chaque fin de parole de l'assistant, une fenêtre d'écoute continue de
   20 à 30 secondes (25 s par défaut) s'ouvre.
2. Si l'utilisateur reprend la parole dans ce délai, son énoncé est traité
   directement et la fenêtre se réarmera à la fin de la nouvelle réponse.
3. Si aucun son vocal n'est détecté avant l'expiration du délai, le micro est
   automatiquement coupé et le système repasse en veille (détection locale du
   wake word Vosk réactivée).
4. Si l'utilisateur prononce une formule de clôture (« merci », « c'est bon »,
   « au revoir », « stop », etc.), la conversation se ferme immédiatement à
   la fin du tour.
5. Half-duplex absolu : pendant que l'assistant parle, la fenêtre n'est pas
   armée et le micro ne transmet rien au modèle.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import unicodedata
from typing import Callable, Optional

logger = logging.getLogger("anogpt.continuous")

# Durée par défaut de la fenêtre d'écoute continue en secondes (20–30 s)
DEFAULT_FOLLOW_UP_TIMEOUT_S: float = 25.0


def normalize_text_for_intent(text: str) -> str:
    """Normalise une chaîne pour l'analyse d'intention (minuscules, sans accents ni ponctuation)."""
    if not text:
        return ""
    # Décomposition Unicode pour supprimer les diacritiques (accents)
    nfkd = unicodedata.normalize("NFKD", text)
    no_accents = "".join(c for c in nfkd if not unicodedata.combining(c)).lower()
    # Remplacement des apostrophes et tirets par des espaces
    cleaned = re.sub(r"['’`\-_]", " ", no_accents)
    # Suppression de la ponctuation résiduelle
    cleaned = re.sub(r"[^\w\s]", "", cleaned)
    # Compression des espaces
    return re.sub(r"\s+", " ", cleaned).strip()


# Formules de clôture strictes (exactes ou en fin/début de phrase sans demande d'action)
CLOSING_PATTERNS = [
    # Remerciements
    r"^merci( (beaucoup|infiniment|bien|ano|jarvis))?$",
    r"^je te remercie( beaucoup)?$",
    r"^mille mercis$",
    r"^c est (tres )?gentil( merci)?$",
    # Fin de besoin / satisfaction
    r"^c est bon( (pour moi|merci|ano|jarvis|pour l instant|pour le moment))?$",
    r"^c est tout( (pour moi|merci|ano|jarvis|pour l instant|pour le moment))?$",
    r"^c est parfait( merci)?$",
    r"^c est nickel( merci)?$",
    r"^ca ira( merci)?$",
    r"^(ce|ca) sera tout( merci)?$",
    # Salutations de fin
    r"^au revoir( (ano|jarvis|a bientot))?$",
    r"^bonne (nuit|soiree|journee|fin de journee)( (ano|jarvis))?$",
    r"^a (plus|plus tard|bientot|la prochaine)( (ano|jarvis))?$",
    r"^ciao( (ano|jarvis))?$",
    r"^adieu$",
    # Arrêt explicite
    r"^(stop|arrete|arrete toi|ferme toi|tais toi|silence|repos|en veille|mets toi en veille)$",
    r"^(passe en veille|verrouille|termine|termine)$",
    # Réponses négatives de clôture
    r"^non( merci| c est bon| c est tout| c est bon merci| c est tout merci)?$",
    r"^rien( d autre| de plus)?( merci)?$",
    r"^non rien( d autre| de plus)?( merci)?$",
    r"^rien pour l instant( merci)?$",
]


def is_assistant_sleep_request(text: str) -> bool:
    """Vrai seulement pour la veille d'ANO-GPT, jamais celle du PC."""
    normalized = normalize_text_for_intent(text)
    normalized = re.sub(r"^(ano|ano gpt|jarvis)\s+", "", normalized)
    if normalized in {
        "mets toi en veille", "met toi en veille", "passe en veille",
        "mets toi au repos", "met toi au repos", "va au repos",
        "dors", "repose toi",
    }:
        return True
    # Formulation naturelle entendue dans la transcription Live. On cible
    # uniquement l'assistant (« te »), jamais « mets le PC en veille ».
    return bool(re.fullmatch(
        r"(?:(?:tu peux|peux tu|est ce que tu peux)\s+)?"
        r"(?:te\s+)?(?:mettre|mets|met)\s+(?:en veille|au repos)",
        normalized,
    ))

# Mots-clés qui indiquent une demande d'action et invalident une fausse clôture
ACTION_KEYWORDS = {
    "ouvre", "ouvrir", "lance", "lancer", "cherche", "chercher", "trouve",
    "trouver", "mets", "mettre", "joue", "jouer", "affiche", "afficher",
    "montre", "montrer", "regle", "regler", "augmente", "augmenter", "baisse",
    "baisser", "supprime", "supprimer", "cree", "creer", "envoie", "envoyer",
    "rappelle", "rappeler", "calcule", "calculer", "meteo", "musique",
    "volume", "ecran", "camera", "fenetre", "application", "navigateur",
    "terminal", "youtube", "gmail", "mail", "courriel", "carte",
}


def is_closing_statement(text: str) -> bool:
    """Détermine si l'énoncé de l'utilisateur est une formule de fin ou de clôture de conversation.
    
    Retourne True si l'utilisateur exprime la fin du dialogue (« merci », « c'est bon »,
    « au revoir », « non c'est tout », etc.) sans formuler une nouvelle commande d'action.
    """
    normalized = normalize_text_for_intent(text)
    if not normalized:
        return False

    # Si la phrase commence par "merci de" ou "merci d'", c'est une requête polie, pas une clôture
    if re.match(r"^merci\s+(de|d)\s+", normalized):
        return False

    # Vérification des correspondances de motifs de clôture
    for pattern in CLOSING_PATTERNS:
        if re.match(pattern, normalized):
            return True

    # Vérification des composés avec remerciement (ex: "super merci", "ok c'est bon merci")
    tokens = normalized.split()
    if len(tokens) <= 5:
        has_closing = any(
            phrase in normalized
            for phrase in [
                "merci", "c est bon", "c est tout", "au revoir",
                "bonne nuit", "bonne journee", "bonne soiree", "a plus",
                "a bientot", "non merci", "rien d autre", "ca ira"
            ]
        )
        has_action = any(tok in ACTION_KEYWORDS for tok in tokens)
        if has_closing and not has_action:
            # Exemples valides : "d'accord merci", "ok merci beaucoup", "parfait c'est bon"
            return True

    return False


class ContinuousConversationManager:
    """Gère le cycle de vie de la conversation continue sans mot d'activation.

    Cette classe coordonne la temporisation d'inactivité, l'interception des
    formules de clôture et la transition fluide vers le mode veille.
    """

    def __init__(
        self,
        timeout_s: float = DEFAULT_FOLLOW_UP_TIMEOUT_S,
        on_sleep: Optional[Callable[[str], None]] = None,
        on_log: Optional[Callable[[str], None]] = None,
        sleep_allowed: Optional[Callable[[], bool]] = None,
    ):
        self.timeout_s = max(0.05, float(timeout_s))
        self._on_sleep = on_sleep
        self._on_log = on_log
        # Sans réveil vocal, une mise en veille automatique est un cul-de-sac :
        # le minuteur n'est alors même pas armé, au lieu d'annoncer une veille
        # puis de la « différer » à chaque fin de réponse.
        self._sleep_allowed = sleep_allowed
        self._no_sleep_logged = False
        self._is_active = False
        self._is_closing_turn = False
        self._timer_task: Optional[asyncio.Task] = None
        self._last_speech_time = time.monotonic()

    @property
    def is_active(self) -> bool:
        """Indique si la fenêtre de relance est actuellement ouverte."""
        return self._is_active

    @property
    def is_closing_turn(self) -> bool:
        """Indique si le tour en cours se terminera par une fermeture immédiate."""
        return self._is_closing_turn

    def set_closing_turn(self, value: bool) -> None:
        """Définit si le tour actuel doit clore la conversation."""
        self._is_closing_turn = bool(value)

    def on_wake_up(self, reason: str = "") -> None:
        """Appelé lors d'un réveil (wake word, raccourci, etc.)."""
        self.cancel_timer()
        self._is_closing_turn = False
        self._last_speech_time = time.monotonic()

    def on_user_speech_detected(self) -> None:
        """Appelé dès que le VAD confirme la prise de parole de l'utilisateur."""
        self.cancel_timer()
        self._is_active = False
        self._last_speech_time = time.monotonic()

    def on_user_transcript(self, text: str) -> bool:
        """Analyse la transcription de l'utilisateur pour détecter une intention de clôture.
        
        Retourne True si l'énoncé est identifié comme une formule de clôture.
        """
        # La transcription finale est une preuve plus fiable que le VAD dans
        # certaines piles PipeWire. Sans ceci, le minuteur de 25 secondes issu
        # de la réponse précédente pouvait expirer pendant que l'utilisateur
        # décrivait justement son image.
        self.cancel_timer()
        self._is_active = False
        self._last_speech_time = time.monotonic()
        if is_assistant_sleep_request(text) or is_closing_statement(text):
            self._is_closing_turn = True
            logger.info("Intention de clôture détectée: %r", text)
            if self._on_log:
                self._on_log("SYS : formule de clôture détectée — fermeture en fin de tour.")
            return True
        return False

    def on_assistant_speech_start(self) -> None:
        """Appelé quand l'assistant commence à parler (respect du half-duplex)."""
        self.cancel_timer()
        self._is_active = False

    def on_assistant_speech_end(
        self,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        """Appelé dès que la lecture audio de l'assistant est totalement achevée.
        
        Déclenche soit la fermeture immédiate si le tour était une clôture,
        soit l'ouverture de la fenêtre d'écoute continue de 20-30s.
        """
        self.cancel_timer()

        if self._is_closing_turn:
            self._is_closing_turn = False
            self._is_active = False
            if self._on_log:
                self._on_log("SYS : conversation terminée sur demande — mise en veille.")
            if self._on_sleep:
                self._on_sleep("clôture utilisateur")
            return

        self._is_active = True
        self._last_speech_time = time.monotonic()
        if self._sleep_allowed is not None:
            try:
                allowed = bool(self._sleep_allowed())
            except Exception:
                allowed = True
            if not allowed:
                if self._on_log and not self._no_sleep_logged:
                    self._no_sleep_logged = True
                    self._on_log(
                        "SYS : écoute continue permanente — réveil « ANO » indisponible, "
                        "le micro reste ouvert."
                    )
                return
        self._no_sleep_logged = False
        if self._on_log:
            self._on_log(f"SYS : écoute continue active ({self.timeout_s:.0f}s sans « ANO »).")

        # Démarrage du compte à rebours d'inactivité
        target_loop = loop
        if target_loop is None:
            try:
                target_loop = asyncio.get_running_loop()
            except RuntimeError:
                target_loop = None

        if target_loop is not None and target_loop.is_running():
            self._timer_task = target_loop.create_task(self._timeout_worker())

    async def _timeout_worker(self) -> None:
        """Tâche asynchrone qui attend l'expiration du délai d'inactivité."""
        try:
            await asyncio.sleep(self.timeout_s)
            self._is_active = False
            if self._on_log:
                self._on_log(
                    f"SYS : délai d'inactivité dépassé ({self.timeout_s:.0f}s) — passage en veille."
                )
            if self._on_sleep:
                self._on_sleep("inactivité conversation")
        except asyncio.CancelledError:
            pass

    def cancel_timer(self) -> None:
        """Annule le timer de fermeture s'il est en cours."""
        if self._timer_task is not None and not self._timer_task.done():
            self._timer_task.cancel()
        self._timer_task = None

    def reset(self) -> None:
        """Réinitialise tous les états et annule les temporisations."""
        self.cancel_timer()
        self._is_active = False
        self._is_closing_turn = False
