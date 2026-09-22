"""Contrôle de santé non bloquant affiché au lancement d'ANO-GPT."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CONFIG = Path(__file__).resolve().parent.parent / "config" / "api_keys.json"


def _gemini_key_status(key: str) -> str:
    """Valide la clé par un appel de lecture borné, sans afficher le secret."""
    request = urllib.request.Request(
        "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1",
        headers={"x-goog-api-key": key, "User-Agent": "ANO-GPT/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=3.0) as response:
            return "valide" if response.status == 200 else f"réponse HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 401, 403):
            return "refusée par Google"
        if exc.code == 429:
            return "quota atteint (clé non invalidée)"
        return f"vérification impossible (HTTP {exc.code})"
    except Exception:
        return "vérification impossible (réseau indisponible)"


def _serpapi_key_status(key: str) -> str:
    """L'API de compte SerpApi valide la clé sans dépenser de recherche."""
    url = "https://serpapi.com/account.json?" + urllib.parse.urlencode({"api_key": key})
    try:
        with urllib.request.urlopen(url, timeout=3.0) as response:
            account = json.loads(response.read().decode("utf-8"))
        if account.get("error"):
            return "refusée par SerpApi"
        if not account.get("account_id"):
            return "réponse de compte incomplète"
        account_status = str(account.get("account_status") or "Active").casefold()
        if account_status != "active":
            return "compte inactif"
        left = account.get("total_searches_left")
        return "valide (quota épuisé)" if left == 0 else "valide"
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 401, 403):
            return "refusée par SerpApi"
        return f"vérification impossible (HTTP {exc.code})"
    except Exception:
        return "vérification impossible (réseau indisponible)"


def collect_startup_health(phone_connected: bool = False) -> tuple[list[str], list[str]]:
    """Retourne (états, manques) sans ouvrir les périphériques audio."""
    states: list[str] = []
    missing: list[str] = []
    try:
        config = json.loads(CONFIG.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            config = {}
    except Exception:
        config = {}

    primary = str(config.get("gemini_api_key") or "").strip()
    live = str(config.get("gemini_live_api_key") or "").strip()
    if not primary:
        missing.append("clé Gemini principale absente")
    for label, key in (("Gemini", primary), ("Gemini Live", live)):
        if key and (label == "Gemini" or key != primary):
            status = _gemini_key_status(key)
            states.append(f"{label} : {status}")
            if status == "refusée par Google":
                missing.append(f"clé {label} refusée")
            elif status == "quota atteint (clé non invalidée)":
                missing.append(f"quota {label} atteint")
    serpapi = (os.environ.get("SERPAPI_API_KEY")
               or str(config.get("serpapi_api_key") or "").strip())
    if serpapi:
        status = _serpapi_key_status(serpapi)
        states.append(f"SerpApi : {status}")
        if status == "refusée par SerpApi":
            missing.append("clé SerpApi refusée")
        elif status in ("compte inactif", "valide (quota épuisé)"):
            missing.append("SerpApi : " + status)
    else:
        states.append("SerpApi absent : recherche Gemini disponible")

    states.append("téléphone connecté" if phone_connected else "téléphone non connecté")
    try:
        import sounddevice as sd
        devices = sd.query_devices()
        has_input = any(int(device.get("max_input_channels") or 0) > 0 for device in devices)
        has_output = any(int(device.get("max_output_channels") or 0) > 0 for device in devices)
    except Exception:
        has_input = has_output = False
    if has_input:
        states.append("micro détecté")
    else:
        missing.append("aucun micro PC détecté")
    if has_output:
        states.append("sortie audio détectée")
    else:
        missing.append("aucune sortie audio détectée")
    return states, missing
