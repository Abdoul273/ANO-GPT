"""
AI Post-STT Contextual Corrector for MARK XL / ANO-GPT.

Provides ultra-fast phonetic normalization for technical terms & application names,
plus LLM-assisted context correction for complex voice input.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Dict, List, Optional
from core.live_model_policy import PINNED_FLASH_MODEL

logger = logging.getLogger("stt.ai_corrector")
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Phonetic & Domain Dictionary for High-Accuracy Command Correction
# ---------------------------------------------------------------------------
# Règle de fond : une transcription doit dire ce qui a été dit. On ne corrige
# donc QUE ce que la reconnaissance a réellement mal entendu — des noms propres
# techniques sans équivalent français — jamais le vocabulaire courant.
#
# Les anciennes « normalisations de verbes » faisaient exactement l'inverse et
# abîmaient des phrases correctes. Trois exemples relevés tels quels :
#   « tu peux ouvrir Firefox »      → « TUE peux ouvrir Firefox »  (\btue?s?\b
#                                      attrape le mot « tu »)
#   « ma recherche sur… »           → « ma CHERCHE sur… »
#   « je suis en terminale »        → « je suis en Terminal »
# Elles sont supprimées : le modèle comprend « efface » sans qu'on le réécrive
# en « supprime », et il ne comprend rien à « tue peux ».
PHONETIC_REPLACEMENTS: List[tuple[re.Pattern, str]] = [
    # Applications & éditeurs — noms propres, aucune ambiguïté avec du français
    (re.compile(r"\b(v[eé]\s*(?:es+e|s[eé]?)\s*code|visuel\s*studio\s*code|v\s*s\s*code)\b", re.IGNORECASE), "VS Code"),
    (re.compile(r"\b(fi[eè]re?\s*foxe?|fire\s*foxe?|fayeur\s*fox)\b", re.IGNORECASE), "Firefox"),
    (re.compile(r"\b(krome)\b", re.IGNORECASE), "Chrome"),
    (re.compile(r"\b(you\s*tube|yutube|utube)\b", re.IGNORECASE), "YouTube"),
    (re.compile(r"\b(pie\s*thon|pithon)\b", re.IGNORECASE), "Python"),
    (re.compile(r"\b(gitte|gitt)\b", re.IGNORECASE), "Git"),
    # Ne jamais utiliser "d'auteur" ici : c'est une expression française
    # courante, et l'ancienne règle transformait "droits d'auteur" en Docker.
    (re.compile(r"\b(dokere?|dockeur|doqueur)\b", re.IGNORECASE), "Docker"),
    (re.compile(r"\b(disicord|discorde)\b", re.IGNORECASE), "Discord"),
    (re.compile(r"\b(spotifi|spotifeille)\b", re.IGNORECASE), "Spotify"),
    (re.compile(r"\b(v\s+l\s+c)\b", re.IGNORECASE), "VLC"),
    (re.compile(r"\b(hyperland|hyper\s*land|hipreland)\b", re.IGNORECASE), "Hyprland"),
    (re.compile(r"\b(guemini|jemini|g[eé]mini)\b", re.IGNORECASE), "Gemini"),
    (re.compile(r"\b(ouatsap|watsap|whatsap)\b", re.IGNORECASE), "WhatsApp"),

    # Extensions et commandes dictées à voix haute
    (re.compile(r"\b(point\s*py|dot\s*py)\b", re.IGNORECASE), ".py"),
    (re.compile(r"\b(point\s*txt|dot\s*txt)\b", re.IGNORECASE), ".txt"),
    (re.compile(r"\b(point\s*j\s*s|dot\s*js)\b", re.IGNORECASE), ".js"),
    (re.compile(r"\b(point\s*json)\b", re.IGNORECASE), ".json"),
    (re.compile(r"\b(l\s*s\s*-?\s*la)\b", re.IGNORECASE), "ls -la"),
]


# Hésitations et respirations : seules, elles ne sont jamais une demande. Une
# phrase qui en contient en garde le sens ; une « phrase » qui n'est QUE ça
# vient d'un raclement de gorge ou d'un bruit de clavier.
_FILLERS = frozenset({
    "euh", "eu", "heu", "hmm", "hm", "mmh", "mm", "hein", "ah", "ahh", "oh",
    "eh", "beh", "ben", "bah", "pff", "tss", "hum", "han", "mh", "mhm",
    "ok", "okay", "hop", "voila", "voilà", "bon",
})

# Mots isolés qui, eux, veulent dire quelque chose. Sans cette liste, une
# réponse d'un seul mot à une question de l'assistant serait rejetée comme du
# bruit — précisément quand il attend « oui » ou « non ».
_MEANINGFUL_SINGLETONS = frozenset({
    "oui", "non", "stop", "attends", "attend", "annule", "pause", "continue",
    "suivant", "precedent", "monte", "baisse", "coupe", "ferme", "ouvre",
    "merci", "salut", "bonjour", "bonsoir", "coucou", "ano", "jarvis", "repete",
    "plus", "moins", "encore", "vas-y", "ok", "allo", "allô", "aide", "dis",
    "qui", "quoi", "yo", "re", "aide-moi",
})

_KNOWN_NOISE_HALLUCINATIONS = (
    "merci d avoir regarde cette video",
    "merci d avoir regarde",
    "sous titres realises par la communaute",
    "sous titres par la communaute",
    "thanks for watching",
    "thank you for watching",
    "amara org",
    "subtitle by",
    "n hesitez pas a vous abonner",
    "abonnez vous a la chaine",
    "activez la cloche",
    "a bientot pour une nouvelle video",
    "transcription par",
    "sous titrage societe radio canada",
    "yo no hablo",
    "no hablo espanol",
    "hola que tal",
    "como estas",
    "buenos dias",
    "buenas noches",
    "mucho gusto",
    "hasta luego",
)

_SPANISH_TOKENS = frozenset({
    "hola", "hablo", "hablas", "espanol", "gracias", "adios", "quiero",
    "tienes", "usted", "ustedes", "senor", "senora", "sabes", "estas",
    "estoy", "donde", "cuando", "tambien", "despues", "siempre",
})

_FRENCH_ANCHORS = frozenset({
    "je", "tu", "il", "elle", "on", "nous", "vous", "les", "une", "des",
    "est", "pas", "dans", "pour", "avec", "ouvre", "ferme", "lance",
    "cherche", "montre", "bonjour", "bonsoir", "merci", "peux", "peut",
    "vais", "fait", "faire", "ano", "jarvis", "mets", "allume", "eteins",
    "aujourd", "hui", "cest",
})

# Musique ou vidéo en fond : le micro capte des paroles étrangères que Live
# transcrit comme des demandes (« invecchiando », « ¿Y qué más queda? »,
# « music only » a ouvert le paquet musique et lancé une écoute de 50 s).
# Listes volontairement limitées aux mots absents du français courant.
_SPANISH_ONLY = frozenset({
    "queda", "mas", "pero", "porque", "esta", "esto", "eso", "muy", "nada",
    "todo", "vamos", "tengo", "hay", "yo", "quien", "ahora", "nunca", "corazon",
    "amor", "vida", "contigo", "mi", "tu", "te", "bien",
}) | _SPANISH_TOKENS
_ITALIAN_ONLY = frozenset({
    "che", "non", "sono", "questo", "questa", "perche", "anche", "ancora",
    "piu", "molto", "della", "nella", "gli", "cosa", "voglio", "ciao",
    "grazie", "amore", "cuore", "tutto", "niente", "sempre", "sei", "invecchiando",
})
_ENGLISH_FUNCTION = frozenset({
    "only", "just", "the", "you", "your", "me", "my", "i", "we", "it", "is",
    "are", "and", "of", "to", "in", "for", "with", "all", "so", "now", "im",
    "don", "t", "be", "been", "this", "that", "what", "no", "not", "oh",
})
_ENGLISH_LYRICS = _ENGLISH_FUNCTION | frozenset({
    "music", "love", "baby", "song", "night", "time", "feel", "know", "want",
    "need", "never", "ever", "yeah", "like", "can", "go", "get", "got", "one",
    "more", "up", "down", "back", "heart", "life", "way", "say", "tonight",
    "girl", "boy", "world", "dance", "yes", "hey", "hold", "tell",
})
# Français en cours : les petits mots qui n'existent que chez nous. Les
# pronoms « tu » / « te » / « mi » sont communs à l'espagnol, donc absents.
_FRENCH_ONLY = _FRENCH_ANCHORS - {"tu", "on", "est"} | frozenset({
    "le", "la", "et", "de", "du", "un", "ce", "ca", "moi", "mon", "ma",
    "efface", "ecris", "tape", "mais", "oui", "non", "salut", "quoi",
})


def foreign_phrase_reason(text: str) -> str:
    """Motif si la phrase est manifestement étrangère à une session française.

    Conservateur : un seul mot français suffit à accepter, et les noms
    techniques anglais isolés (« Firefox », « play ») ne déclenchent rien.
    """
    raw = (text or "").strip()
    if not raw:
        return ""
    folded = _fold(raw)
    tokens = folded.split()
    if not tokens or any(token in _FRENCH_ONLY for token in tokens):
        return ""
    if any(c in raw for c in "¿¡ñÑ"):
        return "langue étrangère (espagnol)"
    if any(token in _ITALIAN_ONLY for token in tokens) or (
        len(tokens) == 1 and len(tokens[0]) >= 7 and tokens[0].endswith(("ando", "endo"))
    ):
        return "langue étrangère (italien)"
    if sum(token in _SPANISH_ONLY for token in tokens) >= 2:
        return "langue étrangère (espagnol)"
    if (len(tokens) >= 2 and all(token in _ENGLISH_LYRICS for token in tokens)
            and any(token in _ENGLISH_FUNCTION for token in tokens)):
        return "langue étrangère (anglais)"
    return ""


_ENGLISH_NOISE = frozenset({
    "start", "hello", "hey", "yeah", "yep", "please", "thanks", "thank",
    "the", "this", "that", "what", "how", "yes", "okay", "okey", "hi",
})

_DIGIT_WORDS = frozenset({
    "zero", "un", "deux", "trois", "quatre", "cinq", "six", "sept",
    "huit", "neuf", "dix",
})


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


@dataclass(frozen=True)
class TranscriptAssessment:
    accepted: bool
    reason: str = ""
    deferred: bool = False


class TranscriptGuard:
    """Rejette les transcriptions typiques d'un bruit pris pour une phrase.

    Ce garde-fou reste volontairement conservateur : il bloque les signatures
    connues d'hallucination et les alphabets incompatibles avec une session
    française, mais accepte les noms techniques anglais comme Firefox ou Git.
    """

    def assess(
        self,
        text: str,
        *,
        acoustic_voice_ms: float | None = None,
        audio_duration_ms: float | None = None,
        partial: bool = False,
    ) -> TranscriptAssessment:
        text = (text or "").strip()
        if not text:
            return TranscriptAssessment(False, "transcription vide")

        folded = _fold(text)
        if any(phrase in folded for phrase in _KNOWN_NOISE_HALLUCINATIONS):
            return TranscriptAssessment(False, "hallucination audio connue")

        tokens = folded.split()
        spanish_hits = sum(1 for token in tokens if token in _SPANISH_TOKENS)
        french_hits = sum(1 for token in tokens if token in _FRENCH_ANCHORS)
        if spanish_hits >= 2 or (spanish_hits >= 1 and french_hits == 0 and len(tokens) >= 2):
            return TranscriptAssessment(False, "langue étrangère (espagnol)")

        foreign = foreign_phrase_reason(text)
        if foreign:
            return TranscriptAssessment(False, foreign)

        if tokens and all(token.isdigit() or token in _DIGIT_WORDS for token in tokens):
            if any(token.isdigit() for token in tokens) or len(tokens) >= 3:
                return TranscriptAssessment(False, "chiffres sans ordre")

        if 1 <= len(tokens) <= 2 and all(token in _ENGLISH_NOISE for token in tokens):
            return TranscriptAssessment(False, "anglais isolé hors lexique")

        letters = [c for c in text if c.isalpha()]
        if letters:
            non_latin = sum(
                "LATIN" not in unicodedata.name(c, "") for c in letters
            )
            if len(letters) >= 4 and non_latin / len(letters) > 0.35:
                return TranscriptAssessment(False, "alphabet inattendu pour le français")

        # Bruit transformé en une longue répétition du même token.
        if len(tokens) >= 5 and len(set(tokens)) <= 1:
            return TranscriptAssessment(False, "répétition sans contenu")

        if len(tokens) >= 7 and len(set(tokens)) / len(tokens) < 0.35:
            return TranscriptAssessment(False, "répétition artificielle")

        # Rien que des hésitations : un raclement de gorge, un soupir, une
        # chaise qui grince. Le modèle, lui, répondrait « oui ? » et relancerait
        # une conversation que personne n'a ouverte.
        if tokens and all(token in _FILLERS for token in tokens):
            if partial:
                return TranscriptAssessment(False, "fragment d'hésitation incomplet", True)
            return TranscriptAssessment(False, "hésitation sans contenu")

        # Le texte seul ne permet pas de distinguer une phrase française
        # plausible d'une hallucination. Quand le portier local fournit sa
        # preuve acoustique, on refuse une transcription sans assez de voix et
        # une quantité de mots physiquement impossible dans la durée envoyée.
        if acoustic_voice_ms is not None and acoustic_voice_ms < 110.0:
            if partial:
                return TranscriptAssessment(False, "preuve vocale encore incomplète", True)
            return TranscriptAssessment(False, "preuve vocale locale insuffisante")

        # Un mot unique et court sur un souffle d'audio : c'est la signature
        # d'un claquement de porte ou d'un choc sur le bureau. On ne l'accepte
        # que s'il veut réellement dire quelque chose — « oui », « stop ».
        if (len(tokens) == 1 and len(tokens[0]) <= 4
                and tokens[0] not in _MEANINGFUL_SINGLETONS
                and (audio_duration_ms or 0) < 500.0):
            if partial:
                return TranscriptAssessment(False, "mot encore incomplet", True)
            return TranscriptAssessment(False, "mot isolé sans support audio")

        if audio_duration_ms is not None and audio_duration_ms > 0:
            plausible_words = 3 + int((audio_duration_ms / 1000.0) * 5.5)
            if len(tokens) > max(4, plausible_words):
                if partial:
                    return TranscriptAssessment(False, "fragment en cours de synchronisation", True)
                return TranscriptAssessment(False, "texte trop long pour l'audio reçu")

            # Débit impossible dans l'autre sens : une transcription bavarde
            # sur très peu de voix confirmée. La télévision en fond produit
            # exactement ça — quelques dizaines de millisecondes de voix
            # « nettes » et une phrase entière derrière.
            if (acoustic_voice_ms is not None and len(tokens) >= 4
                    and acoustic_voice_ms < len(tokens) * 45.0
                    and audio_duration_ms < len(tokens) * 120.0):
                if partial:
                    return TranscriptAssessment(False, "preuve vocale encore incomplète", True)
                return TranscriptAssessment(False, "voix confirmée trop courte pour la phrase")

        return TranscriptAssessment(True)


class TranscriptAssembler:
    """Fusionne les fragments STT incrémentaux sans inventer de doublons.

    Gemini peut envoyer successivement « ouvre », puis « ouvre Firefox » : ce
    sont des révisions cumulatives, pas deux morceaux à concaténer. Il peut aussi
    envoyer de vrais morceaux distincts. Cette classe traite les deux formes.
    """

    def __init__(self) -> None:
        self.text = ""

    def reset(self) -> None:
        self.text = ""

    def add(self, fragment: str) -> str:
        fragment = " ".join((fragment or "").split()).strip()
        if not fragment:
            return self.text
        if not self.text:
            self.text = fragment
            return self.text

        old_tokens = self.text.split()
        new_tokens = fragment.split()
        old_fold = [_fold(token) for token in old_tokens]
        new_fold = [_fold(token) for token in new_tokens]

        if old_fold == new_fold:
            return self.text
        if len(new_fold) >= len(old_fold) and new_fold[:len(old_fold)] == old_fold:
            self.text = fragment
            return self.text
        if len(old_fold) >= len(new_fold) and old_fold[:len(new_fold)] == new_fold:
            return self.text

        # Révision cumulative dont le dernier mot a été corrigé : un préfixe
        # commun substantiel indique qu'il faut remplacer, pas concaténer.
        common_prefix = 0
        for old, new in zip(old_fold, new_fold):
            if old != new:
                break
            common_prefix += 1
        if common_prefix and len(new_fold) >= len(old_fold):
            self.text = fragment
            return self.text

        # Sinon, retirer le chevauchement suffixe/préfixe entre deux vrais
        # fragments (« cherche la » + « la météo »).
        overlap = 0
        for size in range(min(len(old_fold), len(new_fold)), 0, -1):
            if old_fold[-size:] == new_fold[:size]:
                overlap = size
                break
        self.text = " ".join(old_tokens + new_tokens[overlap:])
        return self.text


class STTCorrector:
    """
    Post-processing pipeline for Speech-to-Text outputs.

    Applies deterministic phonetic dictionary rules first, followed by optional
    LLM contextual correction if enabled.
    """

    def __init__(self, use_llm_correction: bool = True, custom_dictionary: Optional[Dict[str, str]] = None):
        self.use_llm_correction = use_llm_correction
        self.custom_dictionary = custom_dictionary or {}
        self._compiled_custom = [
            (re.compile(r"\b" + re.escape(k) + r"\b", re.IGNORECASE), v)
            for k, v in self.custom_dictionary.items()
        ]

    def correct(self, text: str, context_hint: Optional[str] = None) -> str:
        """
        Synchronously correct transcribed text using phonetic rules.
        Fast execution (< 1ms).
        """
        if not text or not text.strip():
            return ""

        corrected = text.strip()

        # Apply custom dictionary first
        for pat, replacement in self._compiled_custom:
            corrected = pat.sub(replacement, corrected)

        # Apply built-in phonetic replacements
        for pat, replacement in PHONETIC_REPLACEMENTS:
            corrected = pat.sub(replacement, corrected)

        # Clean multiple spaces or weird capitalization artifact from speech
        corrected = re.sub(r"\s+", " ", corrected).strip()
        return corrected

    async def correct_async(self, text: str, context_hint: Optional[str] = None, client: Optional[object] = None) -> str:
        """
        Asynchronously correct text, with optional Gemini LLM context refinement.
        """
        fast_corrected = self.correct(text, context_hint)

        # If text is very short or LLM disabled, return fast correction
        if not self.use_llm_correction or not client or len(fast_corrected.split()) < 3:
            return fast_corrected

        try:
            # LLM prompt for intelligent post-STT refinement
            prompt = (
                "Tu es un puissant module de correction post-transcription vocale (STT).\n"
                "La phrase suivante a été captée par un microphone et contient peut-être des mots mal entendus "
                "ou des approximations phonétiques de noms de logiciels, commandes système ou fichiers.\n"
                f"Contexte système optionnel : {context_hint or 'Commande assistant ordinateur'}\n\n"
                f"Texte brut transcrit : \"{fast_corrected}\"\n\n"
                "Corrige UNIQUEMENT la phrase pour qu'elle soit naturelle et techniquement exacte. "
                "Ne rajoute aucun commentaire, restitue seulement la phrase corrigée."
            )

            # Support Google GenAI client if passed
            if hasattr(client, "aio") and hasattr(client.aio, "models"):
                resp = await client.aio.models.generate_content(
                    model=PINNED_FLASH_MODEL,
                    contents=prompt,
                )
                if resp and resp.text:
                    cleaned_llm = resp.text.strip().strip('"').strip()
                    if cleaned_llm:
                        logger.debug("LLM STT correction: '%s' -> '%s'", fast_corrected, cleaned_llm)
                        return cleaned_llm
        except Exception as e:
            logger.warning("LLM STT correction fallback to rule-based: %s", e)

        return fast_corrected
