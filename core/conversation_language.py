"""Sélection explicite et persistante de la langue de conversation."""
from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class ConversationLanguage:
    code: str
    label_fr: str
    label_native: str


LANGUAGES: tuple[ConversationLanguage, ...] = (
    ConversationLanguage("fr-FR", "français", "French"),
    ConversationLanguage("en-US", "anglais", "English"),
    ConversationLanguage("es-ES", "espagnol", "Spanish"),
    ConversationLanguage("de-DE", "allemand", "German"),
    ConversationLanguage("it-IT", "italien", "Italian"),
    ConversationLanguage("pt-BR", "portugais", "Portuguese"),
    ConversationLanguage("tr-TR", "turc", "Turkish"),
    ConversationLanguage("ar", "arabe", "Arabic"),
    ConversationLanguage("ja-JP", "japonais", "Japanese"),
)

_BY_CODE = {language.code.casefold(): language for language in LANGUAGES}
_ALIASES = {
    "fr": "fr-FR", "fr-fr": "fr-FR", "french": "fr-FR", "francais": "fr-FR", "français": "fr-FR",
    "en": "en-US", "en-us": "en-US", "en-gb": "en-US", "english": "en-US", "anglais": "en-US",
    "es": "es-ES", "spanish": "es-ES", "espagnol": "es-ES",
    "de": "de-DE", "german": "de-DE", "allemand": "de-DE",
    "it": "it-IT", "italian": "it-IT", "italien": "it-IT",
    "pt": "pt-BR", "portuguese": "pt-BR", "portugais": "pt-BR",
    "tr": "tr-TR", "turkish": "tr-TR", "turc": "tr-TR",
    "ar": "ar", "arabic": "ar", "arabe": "ar",
    "ja": "ja-JP", "japanese": "ja-JP", "japonais": "ja-JP",
}
def normalise_conversation_language(value: str | None) -> ConversationLanguage:
    key = str(value or "").strip().casefold().replace("_", "-")
    code = _ALIASES.get(key, key).casefold()
    return _BY_CODE.get(code, _BY_CODE["fr-fr"])


def detect_language_switch(text: str) -> ConversationLanguage | None:
    """Retourne une langue uniquement pour un ordre explicite de bascule."""
    folded = " ".join(str(text or "").casefold().split())
    if not folded:
        return None
    if re.search(r"\b(?:reviens?|retourne|passe|mets?|reste|rester)\s+(?:en\s+)?(?:mode\s+)?normal\b", folded):
        return _BY_CODE["fr-fr"]
    for alias, code in _ALIASES.items():
        language = re.escape(alias)
        french_order = rf"\b(?:passe|passer|bascule|basculer|parlons?|parler|parle|réponds?|répondre|reste|rester|reviens?|retourne|mets?|mettre)\s+(?:en|au|sur)\s+{language}(?!\w)"
        english_order = rf"\b(?:switch|change|return|go\s+back)\s+(?:to\s+)?{language}(?!\w)"
        english_speech = rf"\b(?:speak|talk|reply|answer|stay)\s+(?:in\s+)?{language}(?!\w)"
        if re.search(french_order, folded) or re.search(english_order, folded) or re.search(english_speech, folded):
            return _BY_CODE[code.casefold()]
    return None
