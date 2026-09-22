# dependency_manager.py — MARK XL : Gestionnaire de dépendances ultra‑réaliste
# Parsing local avancé, installation intelligente, vérification, liste.
# Capable de comprendre des commandes naturelles pour installer, vérifier ou
# lister les packages Python nécessaires au fonctionnement de JARVIS.

from __future__ import annotations

import importlib.util
import json
import platform
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional, Dict, Any
from core.live_model_policy import FAST_MODEL

# ── Listes de packages (inchangées) ─────────────────────────────────────────
_CORE: list[tuple[str, str]] = [
    ("psutil",             "psutil"),
    ("PIL",                "pillow"),
    ("sounddevice",        "sounddevice"),
    ("numpy",              "numpy"),
    ("requests",           "requests"),
    ("bs4",                "beautifulsoup4"),
    ("pyautogui",          "pyautogui"),
    ("pyperclip",          "pyperclip"),
    ("pygetwindow",        "pygetwindow"),
    ("mss",                "mss"),
    ("cv2",                "opencv-python"),
    ("soundfile",          "soundfile"),
    ("miniaudio",          "miniaudio"),
    ("send2trash",         "send2trash"),
    ("pptx",               "python-pptx"),
    ("youtube_transcript_api", "youtube-transcript-api"),
]

_WINDOWS: list[tuple[str, str]] = [
    ("comtypes",   "comtypes"),
    ("pycaw",      "pycaw"),
    ("win10toast", "win10toast"),
    ("pywinauto",  "pywinauto"),
]

_STT: dict[str, list[tuple[str, str]]] = {
    "whisper": [("faster_whisper", "faster-whisper")],
    "vosk":    [("vosk",           "vosk")],
}

_TTS: dict[str, list[tuple[str, str]]] = {
    "edgetts":    [("edge_tts", "edge-tts")],
    "kokoro":     [("kokoro",   "kokoro>=0.9"), ("soundfile", "soundfile")],
    "elevenlabs": [],
}

# ── Helpers ─────────────────────────────────────────────────────────────────
def _available(module: str) -> bool:
    return importlib.util.find_spec(module) is not None

def _pip(package: str, log: Callable | None = None) -> bool:
    if log:
        log(f"SYS: pip install {package} …")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", package,
         "--quiet", "--disable-pip-version-check"],
        capture_output=True,
        # Généreux : une roue lourde se compile parfois plusieurs minutes. Mais
        # borné : un miroir pip muet ne doit pas geler l'installation à vie.
        timeout=900,
    )
    ok = result.returncode == 0
    if not ok and log:
        stderr = result.stderr.decode(errors="replace").strip()
        log(f"ERR: {package} install failed — {stderr[:140]}")
    return ok

def _get_api_key() -> str:
    try:
        base = Path(__file__).resolve().parent.parent
        config = base / "config" / "api_keys.json"
        with open(config, "r", encoding="utf-8") as f:
            return json.load(f)["gemini_api_key"]
    except Exception:
        return ""

# ── Fonction d’installation originale (adaptée) ────────────────────────────
def install_for_config(config: dict, log: Callable | None = None) -> None:
    """Installe tous les packages manquants selon la configuration."""
    stt = config.get("stt_engine", "whisper").lower()
    tts = config.get("tts_engine", "edgetts").lower()

    needed: list[tuple[str, str]] = list(_CORE)
    needed += _STT.get(stt, [])
    needed += _TTS.get(tts, [])
    if platform.system() == "Windows":
        needed += _WINDOWS

    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for mod, pkg in needed:
        if pkg not in seen:
            seen.add(pkg)
            unique.append((mod, pkg))

    missing = [(mod, pkg) for mod, pkg in unique if not _available(mod)]

    if not missing:
        if log:
            log("SYS: Toutes les dépendances sont déjà installées ✓")
        return

    pkg_names = ", ".join(p for _, p in missing)
    if log:
        log(f"SYS: Installation de {len(missing)} paquet(s) : {pkg_names}")

    for _mod, pkg in missing:
        _pip(pkg, log)

    # Playwright : installer le package + navigateur Chromium
    if not _available("playwright"):
        _pip("playwright", log)
        if log:
            log("SYS: Téléchargement du navigateur Playwright (Chromium, ~150 Mo — une seule fois)…")
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True,
            timeout=1800,  # ~150 Mo à télécharger, une seule fois.
        )
        if log:
            log("SYS: Navigateur Playwright prêt.")

    if log:
        log("SYS: Toutes les dépendances sont prêtes ✓")

# ── Nouvelles fonctions de vérification / liste ────────────────────────────
def _list_core_status() -> str:
    """Retourne un résumé des packages principaux (installé ou manquant)."""
    lines = ["État des packages principaux :"]
    for mod, pkg in _CORE:
        if _available(mod):
            lines.append(f"  ✅ {pkg}")
        else:
            lines.append(f"  ❌ {pkg} (manquant)")
    # Ajouter les packages spécifiques à l'OS
    if platform.system() == "Windows":
        lines.append("Packages Windows supplémentaires :")
        for mod, pkg in _WINDOWS:
            if _available(mod):
                lines.append(f"  ✅ {pkg}")
            else:
                lines.append(f"  ❌ {pkg} (manquant)")
    return "\n".join(lines)

def _check_install(package_name: str) -> str:
    """Vérifie si un package spécifique est installé et propose de l'installer."""
    # Recherche dans toutes les listes
    for mod, pkg in _CORE + _WINDOWS:
        if package_name.lower() in (mod.lower(), pkg.lower()):
            if _available(mod):
                return f"Le package '{pkg}' est déjà installé."
            else:
                return f"Le package '{pkg}' n'est pas installé. Vous pouvez l'installer avec la commande 'installe {pkg}'."
    # Sinon, tentative générique
    if _available(package_name):
        return f"Le module '{package_name}' est déjà installé."
    else:
        return f"Le module '{package_name}' n'est pas trouvé. Vérifiez le nom exact."

def _install_package(package_name: str, log: Callable | None = None) -> str:
    """Installe un package par son nom pip."""
    # On peut tenter un mapping approximatif
    for mod, pkg in _CORE + _WINDOWS:
        if package_name.lower() in (mod.lower(), pkg.lower()):
            if _available(mod):
                return f"'{pkg}' est déjà installé."
            success = _pip(pkg, log)
            return f"Installation de '{pkg}' réussie." if success else f"L'installation de '{pkg}' a échoué."
    # Sinon, on tente directement avec pip
    success = _pip(package_name, log)
    return f"Installation de '{package_name}' réussie." if success else f"L'installation de '{package_name}' a échoué."

# ── Parsing local pour les commandes de dépendances ────────────────────────
def _parse_dependency_command_locally(text: str) -> Optional[Dict[str, Any]]:
    """
    Extrait l'action (install, check, list) et éventuellement le nom d'un package.
    Exemples :
        "installe les dépendances manquantes" → action="install"
        "vérifie si requests est installé" → action="check", package="requests"
        "liste tous les packages" → action="list"
    """
    text = text.lower().strip()
    text = re.sub(r"\b(s'il te pla[iî]t|stp|please|peux-tu|tu peux|je veux|j'aimerais)\b", "", text).strip()

    # Action "install"
    if re.search(r"\b(installe|réinstalle|met[s]? à jour|télécharge)\b", text):
        # Détection d'un package spécifique
        m = re.search(r"(?:installe|réinstalle|met[s]? à jour|télécharge)\s+(?:le |la |les |l'|le package |le paquet )?([a-zA-Z0-9_\-]+)", text)
        if m:
            pkg = m.group(1).strip().lower()
            # Éviter les mots parasites
            if pkg not in {"les", "des", "tous", "tout", "dépendances", "manquantes"}:
                return {"action": "install", "package": pkg}
        # Sinon, installation générale (tous les manquants)
        return {"action": "install"}

    # Action "check"
    if re.search(r"\b(vérifie|check|est-ce que|est[\s-]ce que)\b", text):
        m = re.search(r"(?:vérifie|check|est-ce que)\s+(?:si |que |le |la |les |l'|le package |le paquet )?([a-zA-Z0-9_\-]+)\s+(?:est |soit )?installé", text)
        if m:
            return {"action": "check", "package": m.group(1).strip()}
        # Vérification générale (on peut lister)
        return {"action": "list"}

    # Action "list"
    if re.search(r"\b(liste|affiche|montre|quels|quelles|quelles sont|quels sont)\b", text):
        return {"action": "list"}

    return None

def _detect_dependency_intent_ai(description: str) -> Optional[Dict]:
    api_key = _get_api_key()
    if not api_key:
        return None
    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        prompt = (
            f"Analyse la phrase suivante et détermine l'action à effectuer concernant les dépendances Python.\n"
            f"Actions possibles : install (tout ou avec un package), check (package spécifique), list.\n"
            f"Retourne UNIQUEMENT un objet JSON avec 'action' et 'package' (si applicable).\n"
            f"Phrase : \"{description}\""
        )
        resp = client.models.generate_content(model=FAST_MODEL, contents=prompt)
        json_match = re.search(r'\{.*\}', resp.text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group(0))
    except Exception as e:
        print(f"[DependencyManager] Erreur IA : {e}")
    return None

# ── Point d’entrée public unifié ──────────────────────────────────────────
def dependency_control(
    parameters: dict = None,
    player=None,
    speak=None,
    session_memory=None,
) -> str:
    """
    Gère les dépendances Python de JARVIS.

    Paramètres acceptés :
        description : phrase naturelle (ex: "installe les dépendances manquantes")
        action      : "install", "check", "list"
        package     : nom du package (pour install/check)
    """
    params = parameters or {}
    description = params.get("description", "").strip()
    action = params.get("action", "").strip().lower()
    package = params.get("package", "").strip()

    # Interprétation naturelle
    if description and not action:
        local = _parse_dependency_command_locally(description)
        if local:
            action = local.get("action", action)
            package = local.get("package", package)
        else:
            ai = _detect_dependency_intent_ai(description)
            if ai:
                action = ai.get("action", action)
                package = ai.get("package", package)
            else:
                # Par défaut, on peut proposer d'installer tous les manquants
                return "Je n'ai pas compris. Voulez-vous installer les dépendances manquantes ? (installe, liste, vérifie...)"

    if not action:
        return "Action non spécifiée. Utilisez 'install', 'check' ou 'list'."

    # Fonction de log pour l'installation
    def log(msg: str):
        if player:
            player.write_log(msg)

    try:
        if action == "install":
            if package:
                return _install_package(package, log)
            else:
                # On utilise une configuration par défaut (stt=whisper, tts=edgetts)
                default_config = {"stt_engine": "whisper", "tts_engine": "edgetts"}
                install_for_config(default_config, log)
                return "Installation des dépendances terminée. Tous les packages manquants ont été installés."

        elif action == "check":
            if package:
                return _check_install(package)
            else:
                return _list_core_status()

        elif action == "list":
            return _list_core_status()

        else:
            return f"Action inconnue : '{action}'. Utilisez 'install', 'check' ou 'list'."

    except Exception as e:
        return f"Erreur lors de la gestion des dépendances : {e}"
