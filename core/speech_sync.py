"""Découpage des sous-titres pour une apparition calée sur la voix."""

from __future__ import annotations

import re


def split_caption_units(text: str, words_per_unit: int = 2) -> list[str]:
    """Découpe en petits groupes lisibles sans perdre la ponctuation.

    Afficher un paragraphe entier au premier son donne un sous-titre en avance
    sur l'audio. Des groupes de deux mots font grandir la carte au rythme de la
    diction tout en évitant un changement de géométrie à chaque syllabe.
    """
    tokens = re.findall(r"\S+", text or "")
    if not tokens:
        return []
    size = max(1, int(words_per_unit))
    return [" ".join(tokens[i:i + size]) for i in range(0, len(tokens), size)]


def caption_targets(start: float, duration: float, count: int) -> list[float]:
    """Positions temporelles régulières dans l'audio de la transcription."""
    if count <= 0:
        return []
    start = max(0.0, float(start))
    usable = max(0.0, float(duration) - 0.12)
    return [start + usable * i / count for i in range(count)]


# Une unité de deux mots ne dure pas toujours le même temps : « j'ai » se dit
# en un souffle, « extraordinairement » non. Répartir les sous-titres à
# intervalles égaux fait donc dériver le texte à l'intérieur d'une même phrase.
# Ce poids approche la durée réelle : les lettres portent la diction, les
# chiffres se disent bien plus lentement qu'ils ne s'écrivent (« 22h30 » vaut
# cinq syllabes pour cinq caractères), et une ponctuation forte impose un
# silence que l'œil attend aussi.
_DIGIT_WEIGHT = 2.6
_STRONG_PAUSE = 3.0
_WEAK_PAUSE = 1.0


def caption_weight(unit: str) -> float:
    """Durée relative approximative d'un fragment prononcé."""
    weight = 0.0
    for char in unit or "":
        if char.isdigit():
            weight += _DIGIT_WEIGHT
        elif char.isalpha():
            weight += 1.0
        elif char in ".!?…":
            weight += _STRONG_PAUSE
        elif char in ",;:":
            weight += _WEAK_PAUSE
    return max(1.0, weight)


def caption_targets_weighted(start: float, duration: float,
                             units: list[str]) -> list[float]:
    """Positions calées sur le poids parlé de chaque fragment.

    Même contrat que ``caption_targets`` — la première position vaut ``start``,
    aucune ne dépasse la fin utile — mais l'espacement suit la diction au lieu
    d'un pas constant.
    """
    if not units:
        return []
    start = max(0.0, float(start))
    usable = max(0.0, float(duration) - 0.12)
    weights = [caption_weight(unit) for unit in units]
    total = sum(weights)
    if total <= 0:
        return [start] * len(units)
    targets: list[float] = []
    elapsed = 0.0
    for weight in weights:
        targets.append(start + usable * (elapsed / total))
        elapsed += weight
    return targets
