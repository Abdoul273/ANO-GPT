"""SDK public, léger et facultatif pour les plugins ANO-GPT."""
from __future__ import annotations
from typing import Any

PERMISSIONS = ("filesystem_read", "filesystem_write", "network", "subprocess", "clipboard")

def required_text(parameters: dict[str, Any], name: str, *, max_length: int = 500) -> str:
    value = str(parameters.get(name, "")).strip()
    if not value: raise ValueError(f"Le paramètre « {name} » est obligatoire.")
    if len(value) > max_length: raise ValueError(f"Le paramètre « {name} » dépasse {max_length} caractères.")
    return value

def optional_int(parameters: dict[str, Any], name: str, default: int, *, minimum: int, maximum: int) -> int:
    try: value = int(parameters.get(name, default))
    except (TypeError, ValueError): return default
    return max(minimum, min(maximum, value))
