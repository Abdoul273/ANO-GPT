"""Personnalisation de l'écran propre à ANO-GPT, distinct du bureau système."""
from __future__ import annotations

import unicodedata
from pathlib import Path

from ui.orb import registry

BACKGROUND_DIR = Path(__file__).resolve().parent.parent / "background"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
_CLEAR = {"aucun", "aucune", "none", "retirer", "supprimer", "default", "defaut"}


def _key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    return "".join(char for char in normalized if char.isalnum() and not unicodedata.combining(char))


def available_backgrounds() -> list[Path]:
    if not BACKGROUND_DIR.is_dir():
        return []
    return sorted((path for path in BACKGROUND_DIR.iterdir()
                   if path.is_file() and path.suffix.casefold() in IMAGE_SUFFIXES),
                  key=lambda path: path.name.casefold())


def resolve_orb(value: str) -> str:
    wanted = _key(value)
    for spec in registry.selectable_specs():
        if wanted in {_key(spec.id), _key(spec.label)}:
            return spec.id
    known = ", ".join(spec.label for spec in registry.selectable_specs())
    raise ValueError(f"style d'orbe inconnu « {value} ». Choix : {known}")


def resolve_background(value: str) -> str:
    raw = value.strip()
    if _key(raw) in _CLEAR:
        return ""
    gallery = available_backgrounds()
    matches = [path for path in gallery
               if _key(raw) in {_key(path.name), _key(path.stem)}]
    if len(matches) == 1:
        return str(matches[0].resolve())
    if len(matches) > 1:
        raise ValueError("plusieurs fonds portent ce nom : " + ", ".join(p.name for p in matches))
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = BACKGROUND_DIR.parent / path
    if not path.is_file() or path.suffix.casefold() not in IMAGE_SUFFIXES:
        known = ", ".join(p.name for p in gallery) or "aucune image dans background/"
        raise ValueError(f"image introuvable « {value} ». Fonds disponibles : {known}")
    return str(path.resolve())


def hud_appearance(parameters: dict, player) -> str:
    """Liste, consulte ou change immédiatement le fond et l'orbe de l'application."""
    raw_orb = str(parameters.get("orb_style") or "").strip()
    raw_background = str(parameters.get("background_image") or "").strip()
    action = str(parameters.get("action") or (
        "apply" if raw_orb or raw_background else "status"
    )).strip().casefold()
    if action in {"list", "liste", "choices", "choix"}:
        styles = ", ".join(spec.label for spec in registry.selectable_specs())
        backgrounds = ", ".join(path.name for path in available_backgrounds()) or "aucun"
        return (f"Styles d'orbe : {styles}. Fonds du HUD : {backgrounds}. "
                "Utiliser background_image='aucun' pour retirer l'image.")
    if action in {"status", "statut", "current", "actuel"}:
        return player.control_hud_appearance("status")
    if action not in {"apply", "set", "changer", "appliquer"}:
        raise ValueError("action attendue : list, status ou apply")

    if not raw_orb and not raw_background:
        raise ValueError("indique orb_style, background_image, ou les deux")
    orb_style = resolve_orb(raw_orb) if raw_orb else None
    background_path = resolve_background(raw_background) if raw_background else None
    return player.control_hud_appearance("apply", orb_style, background_path)
