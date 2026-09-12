"""Normalisation prudente des petits nombres dictés pour ANO-Remote.

Le modèle retranscrit parfois « soixante-dix » là où le protocole téléphone
attend ``70``.  Cette conversion est volontairement limitée à une expression
composée exclusivement de mots-nombres français : un nom de contact ne peut
donc pas être transformé par erreur.
"""
from __future__ import annotations

import re
import unicodedata


_UNITS = {
    "zero": 0, "un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4,
    "cinq": 5, "six": 6, "sept": 7, "huit": 8, "neuf": 9,
    "dix": 10, "onze": 11, "douze": 12, "treize": 13, "quatorze": 14,
    "quinze": 15, "seize": 16,
}
_TENS = {
    "vingt": 20, "trente": 30, "quarante": 40, "cinquante": 50,
    "soixante": 60,
}
_ALLOWED = set(_UNITS) | set(_TENS) | {"et", "cent", "cents", "mille"}
_TRAILING_SUFFIX = re.compile(r"\s+(?:a|au|aux)\s+la\s+fin\s*$")


def _fold(value: str) -> str:
    return "".join(
        char for char in unicodedata.normalize("NFKD", value.casefold())
        if not unicodedata.combining(char)
    )


def spoken_number_to_digits(value: object) -> str:
    """Return a 2–5 digit suffix spoken in French, otherwise the input.

    Examples: ``soixante-dix`` → ``70`` and ``quatre vingt onze`` → ``91``.
    Existing digits and arbitrary contact names are kept exactly as supplied.
    """
    original = str(value or "").strip()
    if not original or re.search(r"\d", original):
        return original
    text = _TRAILING_SUFFIX.sub("", _fold(original).replace("-", " "))
    tokens = [token for token in text.split() if token != "et"]
    if not tokens or any(token not in _ALLOWED for token in tokens):
        return original
    # Quand les chiffres sont dictés séparément (« sept zéro »), les additionner
    # donnerait 7. On conserve alors exactement la suite de chiffres entendue.
    if 2 <= len(tokens) <= 5 and all(token in _UNITS and _UNITS[token] < 10 for token in tokens):
        return "".join(str(_UNITS[token]) for token in tokens)

    total = current = 0
    index = 0
    while index < len(tokens):
        token = tokens[index]
        # « quatre-vingt » est 80, pas 24. Les unités suivantes s'ajoutent.
        if token == "quatre" and index + 1 < len(tokens) and tokens[index + 1] == "vingt":
            current += 80
            index += 2
            continue
        if token in _UNITS:
            current += _UNITS[token]
        elif token in _TENS:
            current += _TENS[token]
        elif token in {"cent", "cents"}:
            current = max(1, current) * 100
        elif token == "mille":
            total += max(1, current) * 1000
            current = 0
        index += 1
    number = total + current
    return str(number) if 10 <= number <= 99999 else original
