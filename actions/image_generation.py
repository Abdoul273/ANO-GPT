"""Native Azure Foundry image generation for the ANO-GPT gallery."""
from __future__ import annotations

import re
import time
from pathlib import Path

from core.azure_specialists import image

from core import action_kit as kit

# Dossier unique des créations visuelles ANO-GPT. Il est créé par le client
# Azure au moment de l'écriture, ce qui évite tout effet de bord au démarrage.
OUTPUT_DIR = Path.home() / "Images" / "ANO-GPT"


@kit.action("generate_image")
def generate_image(parameters: dict | None = None, player=None) -> str:
    parameters = parameters or {}
    prompt = " ".join(str(parameters.get("prompt") or "").split())
    if not prompt:
        return "Décrivez l'image à créer."
    size = str(parameters.get("size") or "1024x1024")
    if size not in {"1024x1024", "1024x1536", "1536x1024"}:
        size = "1024x1024"
    safe = re.sub(r"[^\w-]+", "_", prompt, flags=re.UNICODE).strip("_")[:48] or "image"
    path = OUTPUT_DIR / f"{safe}_{int(time.time())}.png"
    try:
        data = image(prompt, path, size=size)
    except Exception as exc:
        return f"Génération Azure échouée : {exc}"
    if player is not None and hasattr(player, "show_generated_image_preview"):
        player.show_generated_image_preview(prompt, data, str(path))
    return f"Image créée avec Azure et enregistrée dans : {path}"
