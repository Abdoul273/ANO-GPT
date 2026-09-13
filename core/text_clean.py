"""Nettoyage du texte destiné au modèle vocal.

Gemini Live lit ce qu'on lui donne ; devant une rafale d'émojis (titres
TikTok, descriptions Instagram…) il hésite, bafouille un « euh… » interminable
ou lit les noms des pictogrammes. Ce qui va au modèle est donc débarrassé des
émojis ; l'interface, elle, garde le texte d'origine.
"""
from __future__ import annotations

import re
from typing import Any

# Plages Unicode des pictogrammes et de leurs modificateurs (ZWJ, sélecteurs
# de variante, tons de peau, drapeaux régionaux, symboles divers).
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"   # émoticônes, symboles, transports, suppléments
    "\U00002600-\U000027BF"   # symboles divers et casseau
    "\U00002B00-\U00002BFF"   # flèches et formes
    "\U0001F1E6-\U0001F1FF"   # indicateurs régionaux (drapeaux)
    "\U0000FE00-\U0000FE0F"   # sélecteurs de variante
    "\U0000200D"              # zero width joiner
    "\U000020E3"              # combining enclosing keycap
    "\U00002190-\U000021FF"   # flèches
    "\U00002300-\U000023FF"   # technique divers (⌚ ⏰ …)
    "\U000025A0-\U000025FF"   # formes géométriques
    "\U00003030\U0000303D\U00003297\U00003299\U000000A9\U000000AE\U00002122"
    "\U000E0020-\U000E007F"   # tags (drapeaux régionaux)
    "]+",
    flags=re.UNICODE,
)
_SPACES_RE = re.compile(r"[ \t]{2,}")


def strip_emoji(text: str) -> str:
    """Retire les émojis ; conserve tout le reste, espaces recollés."""
    if not text:
        return text
    cleaned = _EMOJI_RE.sub("", text)
    if cleaned == text:
        return text
    cleaned = _SPACES_RE.sub(" ", cleaned)
    return "\n".join(line.rstrip() for line in cleaned.splitlines())


def strip_emoji_deep(value: Any) -> Any:
    """Même chose sur une structure JSON (dict/list) — pour une réponse d'outil."""
    if isinstance(value, str):
        return strip_emoji(value)
    if isinstance(value, dict):
        return {k: strip_emoji_deep(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(strip_emoji_deep(v) for v in value)
    return value
