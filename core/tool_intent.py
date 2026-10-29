"""Lecture de l'intention dans une demande (volume, capture, clic, Bluetooth, preset).

Fonctions pures, sans état : le répartiteur s'en sert pour refuser ce que l'énoncé
reconnu ne justifie pas (capture déduite par le modèle, volume système demandé pour
un morceau, clic sur des coordonnées devinées).
"""
from __future__ import annotations

import re

# Une commande brute de volume agit sur la sortie PipeWire/ALSA entière. Elle
# ne doit jamais pouvoir être utilisée à la place du contrôle du morceau.
_GLOBAL_VOLUME_SHELL_RE = re.compile(
    r"\b(?:amixer\s+(?:-D\s+\S+\s+)?(?:s?set\s+)?master|"
    r"pactl\s+set-sink-volume|wpctl\s+set-volume\s+@DEFAULT_AUDIO_SINK@)",
    re.IGNORECASE,
)

# Une commande brute de luminosité lancée via shell_exec contournerait la
# lecture d'état avant/après (« déjà à 100 % »). On la redirige vers
# computer_settings.
_BRIGHTNESS_SHELL_RE = re.compile(
    r"\b(?:brightnessctl\s+(?:-\S+\s+)*set|light\s+-[ASU]|xbacklight\s+-(?:set|inc|dec)|"
    r"/sys/class/backlight/)",
    re.IGNORECASE,
)

# L'enregistrement d'écran peut ouvrir le portail Wayland et capter des
# contenus privés. Une inférence isolée du modèle (« fais… » mal transcrit)
# ne suffit donc jamais : l'énoncé reconnu doit nommer cette intention.
_CAPTURE_INTENT_RE = re.compile(
    r"\b(?:capture(?:r)?|capture d.?ecran|capture ecran|"
    r"capture d.?écran|enregistre(?:r|ment)?|enregistrement|"
    r"filme(?:r)?|video d.?ecran|vidéo d.?écran|screenshot|screen record)\b",
    re.IGNORECASE,
)


def _has_explicit_capture_intent(transcript: object) -> bool:
    """True seulement si l'utilisateur a clairement demandé une capture."""
    return bool(_CAPTURE_INTENT_RE.search(str(transcript or "")))


def _is_global_volume_shell_command(args: dict) -> bool:
    raw = " ".join(str(args.get(key) or "") for key in ("command", "description"))
    return bool(_GLOBAL_VOLUME_SHELL_RE.search(raw))


def _is_brightness_shell_command(args: dict) -> bool:
    raw = " ".join(str(args.get(key) or "") for key in ("command", "description"))
    return bool(_BRIGHTNESS_SHELL_RE.search(raw))


def _brightness_args_from_shell(args: dict, request: str = "") -> dict:
    """Convertit une commande shell de luminosité en action computer_settings."""
    raw = " ".join([
        str(args.get("command") or ""),
        str(args.get("description") or ""),
        str(request or ""),
    ])
    if re.search(r"\b(?:[àa]\s+fond|au\s+max(?:imum)?|maxi(?:mum)?|100\s*%)", raw, re.IGNORECASE):
        return {"action": "brightness_set", "value": 100}
    if re.search(r"\b(?:au\s+min(?:imum)?|mini(?:mum)?)\b", raw, re.IGNORECASE):
        return {"action": "brightness_set", "value": 1}
    cmd_text = " ".join(str(args.get(key) or "") for key in ("command", "description"))
    delta_up = re.search(r"(?:\+\s*(\d{1,3})\s*%?|(\d{1,3})\s*%\s*\+|light\s+-A\s+(\d{1,3}))", cmd_text, re.IGNORECASE)
    if delta_up:
        val = int(next(g for g in delta_up.groups() if g is not None))
        return {"action": "brightness_up", "value": max(1, min(100, val))}
    delta_down = re.search(r"(?:-\s*(\d{1,3})\s*%?|(\d{1,3})\s*%\s*-|light\s+-U\s+(\d{1,3}))", cmd_text, re.IGNORECASE)
    if delta_down:
        val = int(next(g for g in delta_down.groups() if g is not None))
        return {"action": "brightness_down", "value": max(1, min(100, val))}
    pct = re.search(r"(?<![\w.])(\d{1,3})\s*%", cmd_text)
    if pct:
        return {"action": "brightness_set", "value": max(0, min(100, int(pct.group(1))))}
    if re.search(r"\b(?:augmente|monte|hausse|plus)\b", raw, re.IGNORECASE):
        return {"action": "brightness_up", "value": 10}
    if re.search(r"\b(?:diminue|baisse|r[ée]duis|moins)\b", raw, re.IGNORECASE):
        return {"action": "brightness_down", "value": 10}
    return {"action": "brightness_set", "value": 100}


def _explicit_system_volume_request(text: str) -> bool:
    """Distingue « la musique » de « le système » à partir de la phrase dite."""
    normalized = str(text or "").casefold()
    return bool(
        "volume" in normalized
        and re.search(r"\b(?:syst[eè]me|global|ordinateur|pc|toutes? les applications)\b", normalized)
    )


def _media_volume_request(text: str) -> bool:
    """Vrai uniquement quand l'utilisateur nomme le média à régler."""
    normalized = str(text or "").casefold()
    return bool(re.search(
        r"\b(?:musique|morceau|chanson|spotify|audio|vid[eé]o|youtube|film|clip|lecteur)\b",
        normalized,
    ))


def _system_volume_args_from_shell(args: dict, request: str = "") -> dict:
    """Convertit une commande brute déjà proposée en action sûre et dédiée."""
    raw = " ".join([
        str(args.get("command") or ""),
        str(args.get("description") or ""),
        str(request or ""),
    ])
    if re.search(r"\b(?:[àa]\s+fond|au\s+max(?:imum)?|maxi(?:mum)?)\b", raw, re.IGNORECASE):
        return {"action": "volume_set", "value": "100"}
    if re.search(r"\b(?:au\s+min(?:imum)?|mini(?:mum)?)\b", raw, re.IGNORECASE):
        return {"action": "volume_set", "value": "0"}
    cmd_raw = " ".join(str(args.get(key) or "") for key in ("command", "description"))
    value = re.search(r"(?<![\w.])(\d{1,3})\s*%", cmd_raw)
    amount = max(0, min(100, int(value.group(1)))) if value else 10
    if re.search(r"(?:\+\s*\d+\s*%|\d+%\+|volume_up|augment)", cmd_raw, re.IGNORECASE):
        return {"action": "volume_up", "value": str(max(1, amount))}
    if re.search(r"(?:-\s*\d+\s*%|\d+%-|volume_down|diminu)", cmd_raw, re.IGNORECASE):
        return {"action": "volume_down", "value": str(max(1, amount))}
    return {"action": "volume_set", "value": str(amount)}


def _relative_volume_value_from_shell(args: dict) -> str:
    """Préserve le signe (+/-) d'une commande modèle pour un lecteur média."""
    raw = " ".join(str(args.get(key) or "") for key in ("command", "description"))
    match = re.search(r"([+-]?)\s*(\d{1,3})\s*%", raw)
    if match and not match.group(1):
        # ALSA formule les deltas comme « 10%- » plutôt que « -10% ».
        suffix = re.search(r"\d{1,3}\s*%\s*([+-])", raw)
        if suffix:
            return f"{suffix.group(1)}{max(0, min(100, int(match.group(2))))}"
    if not match:
        return "-10" if re.search(r"diminu|baisse|moins", raw, re.IGNORECASE) else "+10"
    sign, amount = match.groups()
    if not sign:
        sign = "-" if re.search(r"diminu|baisse|moins", raw, re.IGNORECASE) else "+"
    return f"{sign}{max(0, min(100, int(amount)))}"


_CURSOR_ONLY_TARGETS = frozenset({
    "ici", "la", "là", "dessus", "ca", "ça", "maintenant",
    "a la position actuelle", "à la position actuelle",
    "la ou est la souris", "là où est la souris",
    "ou est le curseur", "où est le curseur",
})


def _has_explicit_coordinates(request: str) -> bool:
    """True uniquement si l'utilisateur a dicté des coordonnées x,y explicites."""
    text = str(request or "").strip()
    if not text:
        return False
    return bool(
        re.search(r"\b\d{1,4}\s*[,;]\s*\d{1,4}\b", text)
        or re.search(r"\bx\s*[:=]?\s*\d{1,4}\b.*\by\s*[:=]?\s*\d{1,4}\b", text, re.I)
        or re.search(r"\b(?:en|[àa]|au|aux|coordonn[ée]es?)\s+\d{1,4}\s+\d{1,4}\b", text, re.I)
    )


def _click_action_from_request(request: str) -> str:
    """Détecte si la phrase demande un double-clic ou un clic droit."""
    text = str(request or "").casefold()
    if re.search(r"\b(?:double[- ]?cliqu\w*|double[- ]?clic|double[- ]?click|cliqu\w*\s+deux\s+fois)\b", text):
        return "double_click"
    if re.search(r"\b(?:clic\s+droit|cliqu\w*\s+droit|right[- ]?click|menu\s+contextuel)\b", text):
        return "right_click"
    return ""


def _click_target_from_request(request: str) -> str:
    """Repère une cible nommée pour empêcher un clic sur des coordonnées devinées."""
    raw = str(request or "").strip()
    if not raw:
        return ""
    patterns = (
        r"\b(?:double[- ]?cliqu(?:e|er|ez)?|fais\s+un\s+double[- ]?clic|double[- ]?click)\s+(?:sur\s+)?(.+)$",
        r"\b(?:fais\s+un\s+)?(?:clic\s+droit|cliqu(?:e|er|ez)?\s+droit|right[- ]?click)\s+(?:sur\s+)?(.+)$",
        r"\b(?:clique|cliquer|cliquez|click|fais\s+un\s+clic)\s+(?:gauche\s+)?(?:sur\s+|dans\s+|au\s+niveau\s+de\s+|l[àa]\s+o[ùu]\s+(?:il\s+y\s+a\s+)?(?:[ée]crit\s+)?)?(.+)$",
        r"\b(?:appuie|appuyer|appuies|presse|presser|touche|toucher|s[ée]lectionne|s[ée]lectionner)\s+(?:sur\s+)?"
        r"((?:le\s+|la\s+|l['’]\s*)?(?:bouton|ic[ôo]ne|onglet|menu|lien|case|croix|[ée]l[ée]ment|champ|option)\b.+)$",
    )
    target = ""
    for pat in patterns:
        match = re.search(pat, raw, re.IGNORECASE)
        if match:
            target = match.group(1).strip()
            break
    if not target:
        return ""
    target = re.sub(
        r"\s+(?:sur|dans|de)\s+(?:mon|l['’]|le|cet)\s*[ée]cran\s*$",
        "", target.strip(" .!?\"'«»"), flags=re.IGNORECASE,
    ).strip(" .!?\"'«»")
    target = re.sub(
        r"\s+(?:s['’]il\s+te\s+pla[îi]t|stp|merci|maintenant)\s*$",
        "", target, flags=re.IGNORECASE,
    ).strip(" .!?\"'«»")
    if not target or target.casefold() in _CURSOR_ONLY_TARGETS:
        return ""
    if re.fullmatch(r"\d+\s*[,;\s]\s*\d+", target):
        return ""
    return target


def _bluetooth_action_from_request(request: str) -> str:
    text = str(request or "").casefold()
    if ("bluetooth" not in text
            and not re.search(r"\b(?:rallume|r[ée]active|[ée]teins|d[ée]sactive)[- ]le\b", text)):
        return ""
    if re.search(r"\b(d[ée]sactive|[ée]teins|coupe|arr[êe]te)\b", text):
        return "bluetooth_off"
    if re.search(r"\b(active|allume|rallume|r[ée]active|mets en marche)\b", text):
        return "bluetooth_on"
    return ""


def _preset_requested(request: str) -> bool:
    return bool(re.search(
        r"\b(?:preset|mode\s+(?:coding|code|dev|devsecops|monitoring|web|focus))\b",
        str(request or ""), re.IGNORECASE,
    ))
