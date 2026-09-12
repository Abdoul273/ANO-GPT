"""Génération vidéo native (Sora sur Azure Foundry) pour ANO-GPT."""
from __future__ import annotations

import re
import time
from pathlib import Path

from core.azure_specialists import video

from core import action_kit as kit

# Le dossier utilisateur francophone est la destination canonique des vidéos
# créées par ANO-GPT (et non le dossier anglais ``Videos``).
OUTPUT_DIR = Path.home() / "Vidéos" / "ANO-GPT"

# Formats acceptés par l'API Videos de Foundry.
SIZES = {"1280x720", "720x1280", "1792x1024", "1024x1792"}
# Sora ne rend que des durées discrètes.
DURATIONS = (4, 8, 12)


@kit.action("generate_video")
def generate_video(parameters: dict | None = None, player=None, progress=None) -> str:
    parameters = parameters or {}
    prompt = " ".join(str(parameters.get("prompt") or "").split())
    if not prompt:
        return "Décrivez la vidéo à créer."
    size = str(parameters.get("size") or "1280x720").strip().lower()
    if size not in SIZES:
        size = "1280x720"
    try:
        seconds = int(parameters.get("seconds") or 8)
    except (TypeError, ValueError):
        seconds = 8
    seconds = min(DURATIONS, key=lambda value: abs(value - seconds))

    safe = re.sub(r"[^\w-]+", "_", prompt, flags=re.UNICODE).strip("_")[:48] or "video"
    path = OUTPUT_DIR / f"{safe}_{int(time.time())}.mp4"
    try:
        video(prompt, path, seconds=seconds, size=size, progress=progress)
    except Exception as exc:
        return f"Génération vidéo Azure échouée : {exc}"

    if player is not None and hasattr(player, "show_generated_artifact_preview"):
        player.show_generated_artifact_preview("video", prompt[:80] or "Vidéo créée", str(path))
    return f"Vidéo créée avec Azure et enregistrée dans : {path}"
