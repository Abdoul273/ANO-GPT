"""
send_message.py — Envoi de messages ultra‑réaliste (Wayland/Hyprland ready)
Parsing local avancé, détection de plateforme, backend d'entrée auto-détecté.
Supporte WhatsApp, Telegram, Signal, Discord, Instagram, Messenger, etc.
"""
import json
import importlib
import importlib.util
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

from core import human_confirmation

from core import action_kit as kit
from core.live_model_policy import FAST_MODEL

_HUMAN_APPROVED = object()

# ── Backends optionnels ─────────────────────────────────────────────────────
class _LazyPyAutoGUI:
    _module = None

    def __getattr__(self, name):
        if self._module is None:
            self._module = importlib.import_module("pyautogui")
            self._module.FAILSAFE = True
            self._module.PAUSE = 0.06
        return getattr(self._module, name)


pyautogui = _LazyPyAutoGUI()
_PYAUTOGUI = importlib.util.find_spec("pyautogui") is not None

try:
    import pyperclip
    _PYPERCLIP = True
except ImportError:
    _PYPERCLIP = False

# ── Configuration ───────────────────────────────────────────────────────────
def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

def _get_os() -> str:
    s = platform.system().lower()
    if s == "darwin":
        return "mac"
    if s == "windows":
        return "windows"
    return "linux"

def _get_api_key() -> str:
    try:
        cfg = json.loads(
            (_base_dir() / "config" / "api_keys.json").read_text(encoding="utf-8")
        )
        return cfg.get("gemini_api_key", "")
    except Exception:
        return ""

# ── Détection Wayland / outils ──────────────────────────────────────────────
def _is_wayland() -> bool:
    return bool(os.environ.get("WAYLAND_DISPLAY")) or \
           os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"

def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None

def _input_available() -> bool:
    if _is_wayland() and _have("wtype"):
        return True
    if _have("xdotool"):
        return True
    return _PYAUTOGUI

# ── Clavier / presse-papiers multi-backend ──────────────────────────────────
_KEY_MAP = {
    "enter": "Return", "return": "Return",
    "tab": "Tab", "esc": "Escape", "escape": "Escape",
    "space": "space", "backspace": "BackSpace",
    "delete": "Delete", "del": "Delete",
}

def _copy_text(text: str) -> bool:
    if _is_wayland() and _have("wl-copy"):
        try:
            subprocess.run(["wl-copy"], input=text, text=True, timeout=3)
            return True
        except Exception:
            pass
    if _have("xclip"):
        try:
            subprocess.run(["xclip", "-selection", "clipboard"],
                           input=text, text=True, timeout=3)
            return True
        except Exception:
            pass
    if _PYPERCLIP:
        try:
            pyperclip.copy(text)
            return True
        except Exception:
            pass
    return False

def _press(key: str) -> bool:
    k = _KEY_MAP.get(key.lower(), key)
    if _is_wayland() and _have("wtype"):
        try:
            subprocess.run(["wtype", "-k", k], timeout=2)
            return True
        except Exception:
            pass
    if _have("xdotool"):
        try:
            subprocess.run(["xdotool", "key", k], timeout=2)
            return True
        except Exception:
            pass
    if _PYAUTOGUI:
        try:
            pyautogui.press(key)
            return True
        except Exception:
            pass
    return False

def _hotkey(*keys: str) -> bool:
    keys = [k.lower() for k in keys if k]
    mods = [k for k in keys if k in ("ctrl", "shift", "alt", "super", "meta")]
    nonmods = [_KEY_MAP.get(k, k) for k in keys if k not in mods]
    if _is_wayland() and _have("wtype"):
        cmd = ["wtype"]
        for m in mods:
            cmd += ["-M", "ctrl" if m in ("ctrl", "meta") else m]
        for k in nonmods or ["space"]:
            cmd += ["-k", k]
        for m in reversed(mods):
            cmd += ["-m", "ctrl" if m in ("ctrl", "meta") else m]
        try:
            subprocess.run(cmd, timeout=2)
            return True
        except Exception:
            pass
    if _have("xdotool"):
        try:
            subprocess.run(["xdotool", "key", "+".join(keys)], timeout=2)
            return True
        except Exception:
            pass
    if _PYAUTOGUI:
        try:
            pyautogui.hotkey(*keys)
            return True
        except Exception:
            pass
    return False

def _type_text(text: str) -> bool:
    if _is_wayland() and _have("wtype"):
        try:
            subprocess.run(["wtype", text], timeout=max(2, len(text) // 20 + 2))
            return True
        except Exception:
            pass
    if _have("xdotool"):
        try:
            subprocess.run(["xdotool", "type", "--clearmodifiers", text],
                           timeout=max(2, len(text) // 20 + 2))
            return True
        except Exception:
            pass
    if _PYAUTOGUI:
        try:
            pyautogui.write(text, interval=0.03)
            return True
        except Exception:
            pass
    return False

def _clear_field() -> None:
    _hotkey("ctrl", "a")
    time.sleep(0.1)
    _press("delete")
    time.sleep(0.1)

def _paste_hotkey() -> bool:
    if _is_wayland() and _have("wtype"):
        try:
            subprocess.run(["wtype", "-M", "ctrl", "-k", "v", "-m", "ctrl"], timeout=2)
            return True
        except Exception:
            pass
    if _have("xdotool"):
        try:
            subprocess.run(["xdotool", "key", "ctrl+v"], timeout=2)
            return True
        except Exception:
            pass
    if _PYAUTOGUI:
        try:
            pyautogui.hotkey("ctrl", "v")
            return True
        except Exception:
            pass
    return False

def _paste_text(text: str) -> None:
    if _copy_text(text):
        time.sleep(0.15)
        _paste_hotkey()
        time.sleep(0.1)
    else:
        _type_text(text)

def _clear_and_paste(text: str) -> None:
    _clear_field()
    _paste_text(text)

# ── Lancement d'applications (Linux-first, repli web) ──────────────────────
_APP_LAUNCH_MAP: Dict[str, Dict[str, Any]] = {
    "whatsapp":  {"ids": ["whatsapp", "whatsapp-nativefier", "com.whatsapp"],
                  "web": "https://web.whatsapp.com"},
    "telegram":  {"ids": ["telegramdesktop", "org.telegram.desktop", "telegram"],
                  "web": "https://web.telegram.org"},
    "signal":    {"ids": ["signal-desktop", "org.signal.Signal", "signal"],
                  "web": None},
    "discord":   {"ids": ["discord", "com.discordapp.Discord"],
                  "web": "https://discord.com/app"},
    "instagram": {"ids": [], "web": "https://www.instagram.com/direct/new/"},
    "messenger": {"ids": [], "web": "https://www.messenger.com/"},
    "facebook":  {"ids": [], "web": "https://www.messenger.com/"},
}

def _find_desktop_by_name(name: str) -> Optional[Path]:
    search_dirs = [Path("/usr/share/applications"),
                   Path.home() / ".local/share/applications"]
    for d in search_dirs:
        if not d.is_dir():
            continue
        for fp in d.glob("*.desktop"):
            try:
                text = fp.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for line in text.splitlines():
                if line.startswith("Name=") and name.lower() in line[5:].lower():
                    return fp
    return None

def _open_app(app_name: str) -> bool:
    name_l = app_name.lower().strip()
    info = _APP_LAUNCH_MAP.get(name_l, {"ids": [name_l], "web": None})
    if _have("gtk-launch"):
        for app_id in info.get("ids", []):
            try:
                r = subprocess.run(["gtk-launch", app_id],
                                   capture_output=True, timeout=5)
                if r.returncode == 0:
                    time.sleep(2.5)
                    return True
            except Exception:
                continue
    found = _find_desktop_by_name(app_name)
    if found and _have("gtk-launch"):
        try:
            subprocess.run(["gtk-launch", found.stem],
                           capture_output=True, timeout=5)
            time.sleep(2.5)
            return True
        except Exception:
            pass
    if info.get("web"):
        try:
            from core.browser_policy import open_chrome
            if not open_chrome(info["web"]):
                return False
            time.sleep(4.0)
            return True
        except Exception:
            pass
    return False

def _focus_app_window(app_name: str) -> bool:
    if _is_wayland() and _have("hyprctl"):
        try:
            clients = json.loads(subprocess.check_output(
                ["hyprctl", "-j", "clients"], text=True, timeout=3
            ))
            needle = app_name.lower()
            for c in clients:
                blob = (c.get("class", "") + " " + c.get("title", "")).lower()
                if needle in blob:
                    subprocess.run(
                        ["hyprctl", "dispatch", "focuswindow", f"address:{c['address']}"],
                        timeout=2,
                    )
                    time.sleep(0.3)
                    return True
        except Exception:
            pass
    return False

# ── Recherche dans l'app + envoi ────────────────────────────────────────────
def _search_in_app(query: str) -> None:
    _hotkey("ctrl", "f")
    time.sleep(0.5)
    _clear_and_paste(query)
    time.sleep(1.0)

def _desktop_send(app_name: str, receiver: str, message: str) -> str:
    if not _input_available():
        return "Aucun outil de contrôle clavier disponible. Installez wtype, xdotool ou pyautogui."
    if not _open_app(app_name):
        return f"Impossible d'ouvrir {app_name}."
    _focus_app_window(app_name)
    time.sleep(0.5)
    try:
        _search_in_app(receiver)
        _press("enter")
        time.sleep(0.8)
        _clear_field()
        _paste_text(message)
        time.sleep(0.2)
        _press("enter")
        time.sleep(0.3)
        return f"Message envoyé à {receiver} via {app_name}."
    except Exception as e:
        return f"Erreur pendant l'envoi : {e}"

# ── Handlers par plateforme ─────────────────────────────────────────────────
def _send_whatsapp(receiver: str, message: str) -> str:
    return _desktop_send("whatsapp", receiver, message)

def _send_telegram(receiver: str, message: str) -> str:
    return _desktop_send("telegram", receiver, message)

def _send_signal(receiver: str, message: str) -> str:
    return _desktop_send("signal", receiver, message)

def _send_discord(receiver: str, message: str) -> str:
    return _desktop_send("discord", receiver, message)

def _send_instagram(receiver: str, message: str) -> str:
    return _desktop_send("instagram", receiver, message)

def _send_messenger(receiver: str, message: str) -> str:
    return _desktop_send("messenger", receiver, message)

_PLATFORM_MAP: List[Tuple[set, Any]] = [
    ({"whatsapp", "wp", "wapp"}, _send_whatsapp),
    ({"telegram", "tg"}, _send_telegram),
    ({"instagram", "ig", "insta"}, _send_instagram),
    ({"signal"}, _send_signal),
    ({"discord"}, _send_discord),
    ({"messenger", "facebook", "fb"}, _send_messenger),
]

def _resolve_platform(platform_str: str):
    key = platform_str.lower().strip()
    for keywords, handler in _PLATFORM_MAP:
        if any(k in key for k in keywords):
            return handler
    return lambda r, m: _desktop_send(platform_str.strip().title(), r, m)

# ── Parsing local intelligent ───────────────────────────────────────────────
_PLATFORM_KEYWORDS = {
    "whatsapp": ["whatsapp", "wp", "wapp"],
    "telegram": ["telegram", "tg"],
    "instagram": ["instagram", "ig", "insta"],
    "signal": ["signal"],
    "discord": ["discord"],
    "messenger": ["messenger", "facebook", "fb"],
}

_STOP_WORDS = {
    "le", "la", "les", "un", "une", "des", "mon", "ma", "ton", "ta",
    "son", "sa", "ses", "à", "au", "aux", "pour", "de", "sur",
    "via", "par", "contact", "message", "sms", "envoie", "envoyer",
    "dis", "disant", "dit", "que", "a", "et",
}

def _parse_message_request_locally(text: str) -> Optional[Dict[str, Any]]:
    """Extrait platform, receiver, message_text d'une phrase naturelle."""
    text = text.strip()
    text_lower = text.lower()
    text_lower = re.sub(
        r"\b(s'il te pla[iî]t|stp|please|peux-tu|tu peux|je veux|j'aimerais)\b",
        "", text_lower
    ).strip()

    # Plateforme
    platform = None
    for plat, keys in _PLATFORM_KEYWORDS.items():
        for k in keys:
            if re.search(rf"\b{k}\b", text_lower):
                platform = plat
                break
        if platform:
            break

    # Destinataire
    receiver = None
    receiver_patterns = [
        r"(?:à|pour|au|aux|contact)\s+([^\s,;]+(?:\s+[^\s,;]+)?)",
        r"(?:envoie|envoyer|dis|message|sms)\s+(?:à|pour)?\s+([^\s,;]+(?:\s+[^\s,;]+)?)",
    ]
    for pat in receiver_patterns:
        m = re.search(pat, text_lower)
        if m:
            candidate = m.group(1).strip()
            words = candidate.split()
            words = [w for w in words
                     if w not in _STOP_WORDS
                     and w not in [k for keys in _PLATFORM_KEYWORDS.values() for k in keys]]
            if words:
                receiver = " ".join(words).title()
                break

    # Message
    message = None
    msg_patterns = [
        r"['\"«]([^'\"»]+)['\"»]",
        r"(?:disant|dit|que|message\s*:\s*|sms\s*:\s*)\s*(.+)",
        r"(?:que|disant|dit)\s+(.+)",
    ]
    for pat in msg_patterns:
        m = re.search(pat, text, re.DOTALL)
        if m:
            groups = [g for g in m.groups() if g is not None]
            if groups:
                message = groups[-1].strip()
                message = re.sub(r"\s*(sur|via|par)\s+\w+\s*$", "", message).strip()
                break

    if not message and receiver:
        rest = text_lower
        if platform:
            rest = rest.replace(platform, "").strip()
        rest = rest.replace(receiver.lower(), "").strip()
        rest = re.sub(r"^(à|pour|au|aux|de|sur|via|par)\s+", "", rest).strip()
        if rest:
            message = rest

    return {
        "platform": platform or "whatsapp",
        "receiver": receiver or "",
        "message_text": message or "",
    }

def _detect_message_intent_ai(description: str) -> Optional[Dict]:
    api_key = _get_api_key()
    if not api_key:
        return None
    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        prompt = (
            "Analyse la phrase et extrait les informations pour envoyer un message.\n"
            "Renvoie UNIQUEMENT un JSON : "
            "{'platform':'whatsapp','receiver':'Nom','message_text':'texte'}\n"
            f"Phrase : \"{description}\"\n"
            "Si pas de plateforme, mets 'whatsapp' par défaut."
        )
        resp = client.models.generate_content(model=FAST_MODEL,
                                              contents=prompt)
        match = re.search(r'{.*}', resp.text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
    except Exception as e:
        print(f"[SendMessage] Erreur IA : {e}")
    return None

# ── Point d'entrée principal ────────────────────────────────────────────────
@kit.action("send_message")
def send_message(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    """
    Envoie un message via une application de messagerie.
    Paramètres :
        description  : phrase naturelle
        platform     : whatsapp, telegram, instagram, signal, discord, messenger
        receiver     : nom du destinataire
        message_text : contenu du message
        dry_run      : si vrai, prévisualise sans envoyer
    """
    params = parameters or {}
    description = params.get("description", "").strip()
    platform_  = params.get("platform", "").strip()
    receiver   = params.get("receiver", "").strip()
    message_text = params.get("message_text", "").strip()
    dry_run = bool(params.get("dry_run", False))

    if description and not (receiver and message_text):
        local = _parse_message_request_locally(description)
        if local:
            platform_ = local.get("platform", platform_)
            receiver = local.get("receiver", receiver)
            message_text = local.get("message_text", message_text)
        else:
            ai = _detect_message_intent_ai(description)
            if ai:
                platform_ = ai.get("platform", platform_)
                receiver = ai.get("receiver", receiver)
                message_text = ai.get("message_text", message_text)
            else:
                return ("Je n'ai pas compris à qui ni comment envoyer ce message. "
                        "Pouvez-vous reformuler ?")

    # Un SMS ne part jamais du PC : il n'y a ni carte SIM ni carnet ici. Taper
    # dans une fenêtre nommée « Sms » ne pouvait qu'échouer — c'est le trajet
    # du téléphone qu'il faut prendre.
    if re.fullmatch(r"\s*(sms|texto|message texte|text)\s*", platform_, re.I):
        return ("[MAUVAIS_OUTIL] Un SMS s'envoie uniquement depuis le téléphone : "
                "utilise l'outil phone_sms avec target et body. Aucun message "
                "n'a été envoyé.")

    if not receiver:
        return "Veuillez préciser le destinataire."
    if not message_text:
        return "Veuillez préciser le contenu du message."
    if not platform_:
        platform_ = "whatsapp"

    # Un nom du carnet devient l'identifiant attendu par la plateforme. La
    # résolution est répétée après confirmation : aucun état fragile à garder.
    try:
        from core.contacts import ContactError, get_contacts_book
        resolved = get_contacts_book().resolve(receiver, platform_)
        if resolved:
            receiver = resolved.value
    except ContactError as exc:
        return f"Erreur contact : {exc}"

    if dry_run:
        return f"[Aperçu] Envoi via {platform_} à {receiver} : {message_text}"

    # Toujours prévisualiser avant l'envoi réel — jamais d'envoi silencieux.
    if params.get("_human_approval") is not _HUMAN_APPROVED:
        approved = dict(params)
        approved.update({
            "platform": platform_, "receiver": receiver,
            "message_text": message_text, "description": "",
            "_human_approval": _HUMAN_APPROVED,
        })
        approved.pop("confirm", None)
        return human_confirmation.request(
            "message:send",
            f"Envoyer un message via {platform_}",
            f"Destinataire : {receiver}\n\n{message_text}",
            lambda p=approved, ui=player: send_message(p, player=ui),
        )

    if not _input_available():
        return ("Aucun outil de contrôle clavier disponible. "
                "Installez wtype, xdotool ou pyautogui.")

    preview = message_text[:50] + ("…" if len(message_text) > 50 else "")
    print(f"[SendMessage] 📨 {platform_} → {receiver}: {preview}")
    if player:
        try:
            player.write_log(f"[msg] {platform_} → {receiver}")
        except Exception:
            pass

    handler = _resolve_platform(platform_)
    try:
        result = handler(receiver, message_text)
    except Exception as e:
        result = f"Impossible d'envoyer le message : {e}"

    ok = "envoyé" in result.lower()
    print(f"[SendMessage] {'✅' if ok else '❌'} {result}")
    if player:
        try:
            player.write_log(f"[msg] {result}")
        except Exception:
            pass
    return result
