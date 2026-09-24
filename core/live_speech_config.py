"""Configuration de transcription de la session Gemini Live.

La langue ne change que sur ordre explicite de l'utilisateur. La détection
automatique reste évitée : un son ambigu ne doit jamais déplacer la langue de
conversation.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from core.live_model_policy import TRANSCRIBE_MODEL

DEFAULT_LIVE_VOICE = "Charon"

# Contrat de migration vocal. Ces valeurs sont lues depuis api_keys.json mais
# restent ici afin que le comportement sûr soit testable sans démarrer Qt.
DEFAULT_LIVE_CAPTIONS_PROVIDER = "gemini_live"
DEFAULT_SENSITIVE_COMMAND_TRANSCRIBE_MODEL = TRANSCRIBE_MODEL


def live_captions_provider(config: dict | None = None) -> str:
    """Retourne le fournisseur de sous-titres, avec migration sûre.

    Les anciens fichiers ne contiennent pas cette clé : ils migrent donc vers
    Gemini Live et n'ouvrent plus de deuxième session Transcribe par défaut.
    ``gemini_transcribe`` est conservé comme opt-in de diagnostic seulement.
    """
    value = str((config or {}).get(
        "live_captions_provider", DEFAULT_LIVE_CAPTIONS_PROVIDER
    ) or "").strip().casefold()
    return "gemini_transcribe" if value == "gemini_transcribe" else DEFAULT_LIVE_CAPTIONS_PROVIDER


def full_duplex_aec_is_validated(config: dict | None = None) -> bool:
    """True uniquement après les trois opt-ins explicites nécessaires.

    La présence de Speex/PipeWire n'est jamais une validation acoustique : sur
    haut-parleurs, l'émission reste half-duplex tant que l'utilisateur n'a pas
    validé la chaîne capture micro + sortie réellement jouée.
    """
    cfg = config or {}
    return all(bool(cfg.get(key, False)) for key in (
        "voice_barge_in_enabled",
        "full_duplex_aec_enabled",
        "full_duplex_aec_validated",
    ))

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


# Un réglage explicite reste possible. Sans clé dans la configuration, laisser
# le VAD serveur décider comme dans Mark-LIV : forcer 700 ms change sa façon
# de délimiter les phrases et retarde les réponses.
DEFAULT_LIVE_END_SILENCE_MS = 700
MIN_LIVE_END_SILENCE_MS = 300
MAX_LIVE_END_SILENCE_MS = 2500


def live_end_silence_ms(config: dict | None = None) -> int | None:
    if "live_end_silence_ms" not in (config or {}):
        return None
    raw = (config or {}).get("live_end_silence_ms")
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        value = DEFAULT_LIVE_END_SILENCE_MS
    return max(MIN_LIVE_END_SILENCE_MS, min(MAX_LIVE_END_SILENCE_MS, value))


def build_output_transcription_config(language: str | None = "fr") -> Any:
    from google.genai import types

    return types.AudioTranscriptionConfig(
        language_hints=types.LanguageHints(
            language_codes=[normalise_language_code(language)],
        ),
    )
