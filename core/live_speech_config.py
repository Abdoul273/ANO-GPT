"""Configuration de transcription de la session Gemini Live.

La langue ne change que sur ordre explicite de l'utilisateur. La détection
automatique reste évitée : un son ambigu ne doit jamais déplacer la langue de
conversation.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


DEFAULT_LIVE_VOICE = "Charon"

# Noms acceptés par ``speech_config.voice_config.prebuilt_voice_config``.
# Le qualificatif sert uniquement à l'interface ; seul le nom canonique est
# envoyé à Gemini.
LIVE_VOICE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Achird", "amicale"),
    ("Sulafat", "chaleureuse"),
    ("Gacrux", "mature"),
    ("Kore", "ferme"),
    ("Charon", "informative"),
    ("Iapetus", "claire"),
    ("Erinome", "claire"),
    ("Aoede", "légère"),
    ("Vindemiatrix", "douce"),
    ("Achernar", "feutrée"),
    ("Algieba", "fluide"),
    ("Despina", "fluide"),
    ("Schedar", "équilibrée"),
    ("Sadaltager", "savante"),
    ("Rasalgethi", "informative"),
    ("Puck", "enjouée"),
    ("Laomedeia", "dynamique"),
    ("Sadachbia", "vive"),
    ("Fenrir", "enthousiaste"),
    ("Zephyr", "lumineuse"),
    ("Autonoe", "lumineuse"),
    ("Leda", "jeune"),
    ("Callirrhoe", "décontractée"),
    ("Umbriel", "décontractée"),
    ("Zubenelgenubi", "informelle"),
    ("Enceladus", "soufflée"),
    ("Algenib", "grave"),
    ("Alnilam", "ferme"),
    ("Orus", "ferme"),
    ("Pulcherrima", "directe"),
)


def normalise_live_voice(value: str | None) -> str:
    """Return a canonical supported Live voice, or the safe default."""
    requested = str(value or "").strip().casefold()
    for name, _description in LIVE_VOICE_OPTIONS:
        if requested == name.casefold():
            return name
    return DEFAULT_LIVE_VOICE


FRENCH_TECH_PHRASES = [
    "ANO-GPT",
    "Jarvis",
    "Firefox",
    "Google Chrome",
    "YouTube",
    "Visual Studio Code",
    "VS Code",
    "Terminal",
    "Python",
    "Git",
    "GitHub",
    "Docker",
    "Discord",
    "Spotify",
    "VLC",
    "WhatsApp",
    "Hyprland",
    "EndeavourOS",
    "Conakry",
]

# Les demandes de messagerie sont courantes et particulièrement pénibles à
# faire répéter quand un seul mot est mal décodé ("e-mail" → "elle est").
# Ce sont des indices acoustiques, jamais des règles de réécriture : Gemini
# conserve donc ce qui a réellement été dit autour de ces termes.
FRENCH_MAIL_PHRASES = [
    "e-mail",
    "courriel",
    "Gmail",
    "boîte de réception",
    "message reçu",
    "messages reçus",
    "expéditeur",
    "objet du message",
    "quel est l'e-mail que je viens de recevoir",
]


def normalise_language_code(value: str | None) -> str:
    """Retourne un code BCP-47 stable pour la reconnaissance audio."""
    from core.conversation_language import normalise_conversation_language
    return normalise_conversation_language(value).code


def _adaptation_phrases(extra_phrases: Iterable[str] = ()) -> list[str]:
    """Construit un lexique stable, dédupliqué et sûr pour Gemini Live."""
    phrases: list[str] = []
    seen: set[str] = set()
    for raw in (*FRENCH_TECH_PHRASES, *FRENCH_MAIL_PHRASES, *tuple(extra_phrases)):
        phrase = " ".join(str(raw or "").split()).strip()
        # Les noms sont des indices acoustiques, pas un second prompt. Borner
        # leur taille empêche une valeur de configuration corrompue d'alourdir
        # ou de biaiser toute la reconnaissance.
        if not phrase or len(phrase) > 80:
            continue
        key = phrase.casefold()
        if key in seen:
            continue
        seen.add(key)
        phrases.append(phrase)
    return phrases[:64]


def build_input_transcription_config(
    language: str | None = "fr",
    *,
    extra_phrases: Iterable[str] = (),
) -> Any:
    # google.genai est volontairement importé à la demande. Son module
    # ``types`` est très volumineux et retardait l'affichage de la fenêtre de
    # plusieurs secondes au simple import de main.py.
    from google.genai import types

    return types.AudioTranscriptionConfig(
        language_hints=types.LanguageHints(
            language_codes=[normalise_language_code(language)],
        ),
        adaptation_phrases=_adaptation_phrases(extra_phrases),
    )


def build_output_transcription_config(language: str | None = "fr") -> Any:
    from google.genai import types

    return types.AudioTranscriptionConfig(
        language_hints=types.LanguageHints(
            language_codes=[normalise_language_code(language)],
        ),
    )
