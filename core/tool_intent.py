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


def _system_volume_args_from_shell(args: dict) -> dict:
    """Convertit une commande brute déjà proposée en action sûre et dédiée."""
    raw = " ".join(str(args.get(key) or "") for key in ("command", "description"))
    value = re.search(r"(?<![\w.])(\d{1,3})\s*%", raw)
    amount = max(0, min(100, int(value.group(1)))) if value else 10
    if re.search(r"(?:\+\s*\d+\s*%|\d+%\+|volume_up|augment)", raw, re.IGNORECASE):
        return {"action": "volume_up", "value": str(max(1, amount))}
    if re.search(r"(?:-\s*\d+\s*%|\d+%-|volume_down|diminu)", raw, re.IGNORECASE):
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


def _click_target_from_request(request: str) -> str:
    """Repère une cible nommée pour empêcher un clic sur des coordonnées devinées."""
    match = re.search(
        r"\b(?:clique|cliquer|click)\s+(?:sur\s+)?(.+)$",
        str(request or "").strip(), re.IGNORECASE,
    )
    if not match:
        return ""
    target = re.sub(
        r"\s+(?:sur|dans)\s+(?:mon|l['’]|le)\s*[ée]cran\s*$",
        "", match.group(1).strip(" .!?"), flags=re.IGNORECASE,
    ).strip(" .!?")
    if re.fullmatch(r"\d+\s*[,;]\s*\d+", target):
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
