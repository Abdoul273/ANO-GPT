#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
computer_control.py — JARVIS Computer Control, version réparée et renforcée.
Optimisé pour Arch Linux / Hyprland (Wayland), avec replis X11/macOS/Windows.

Corrections par rapport à l'ancienne version :
    - `_screen_find` appelait `_take_screenshot()`, qui n'existait pas
      (la vraie fonction est `take_screenshot`) : NameError sur chaque
      clic par vision ;
    - `_brightness_set` n'était définie nulle part : l'action brightness_set
      crashait systématiquement → implémentée avec brightnessctl + repli
      sysfs (+ nouvelle action brightness_get) ;
    - le presse-papiers était lu sur la sélection PRIMAIRE
      (`wl-paste --primary`) au lieu du vrai presse-papiers ;
    - les combos clavier étaient envoyés tels quels à wtype (« ctrl+v »),
      ce qui est invalide : wtype exige -M ctrl -k v -m ctrl ;
    - `ydotool click left` est invalide (ydotool attend un masque hex de
      bouton, ex. 0xC0) : mapping ajouté, mousemove passé en absolu ;
    - le repli du switch de workspace dispatchait la chaîne Lua comme nom
      de dispatcher (forcément muet) : Lua via window_instances d'abord,
      repli legacy correct ensuite ;
    - aucun environnement Hyprland restauré : HYPRLAND_INSTANCE_SIGNATURE,
      XDG_RUNTIME_DIR et WAYLAND_DISPLAY le sont désormais pour hyprctl,
      grim et slurp ;
    - les bureaux acceptent les ordinaux (« deuxième bureau », « bureau 3 ») ;
    - le focus ramène la fenêtre sur le bureau courant si elle est ailleurs.
"""
import json
import os
import platform
import random
import re
import string
from core import action_kit as kit
import sys
import time
from pathlib import Path
from typing import Optional, Tuple, Dict, Any
from core.live_model_policy import FAST_MODEL

try:
    from actions.window_instances import (
        focus_window as _hypr_focus_window,
        move_window_to_workspace as _hypr_move_window_to_workspace,
        dispatch_hyprland as _hypr_dispatch_hyprland,
        close_window as _hypr_close_window,
    )
    _HAS_WINDOW_INSTANCES = True
except ImportError:
    _HAS_WINDOW_INSTANCES = False


# ════════════════════════════════════════════════════════════════════════════
# Configuration & helpers
# ════════════════════════════════════════════════════════════════════════════

def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


_BASE = _base_dir()
_CONFIG_PATH = _BASE / "config" / "api_keys.json"
_MEMORY_PATH = _BASE / "memory" / "long_term.json"


def _load_config() -> dict:
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


_OS = _load_config().get("os_system", platform.system().lower())
_WAYLAND = bool(os.environ.get("WAYLAND_DISPLAY"))
_X11 = bool(os.environ.get("DISPLAY") and not _WAYLAND)


def _get_api_key() -> str:
    return _load_config().get("gemini_api_key", "")


_TOOLS: Dict[str, Optional[str]] = {}
_MAX_VOLUME_PERCENT = 100


def _which(cmd: str) -> Optional[str]:
    return kit.which(cmd)


def _have(cmd: str) -> bool:
    return kit.have(cmd)


def _run(cmd: list, timeout: float = 5.0, check: bool = False,
         env: Optional[dict] = None) -> kit.ProcResult:
    """Appel externe borné : le groupe de processus est tué au délai, rien ne
    lève sauf `check=True` (comportement `subprocess.run` conservé)."""
    res = kit.run(cmd, timeout=timeout, env=env)
    if check and not res.ok:
        raise RuntimeError(res.reason())
    return res


# ════════════════════════════════════════════════════════════════════════════
# Environnement Wayland/Hyprland restauré
# ════════════════════════════════════════════════════════════════════════════

def _restore_display_env(env: dict) -> None:
    """Si l'assistant n'a ni DISPLAY ni WAYLAND_DISPLAY (service systemd,
    ssh…), on les récupère depuis un processus de la session graphique."""
    if env.get("DISPLAY") and env.get("WAYLAND_DISPLAY"):
        return
    try:
        uid = os.getuid()
    except AttributeError:
        return
    for pid_dir in Path("/proc").glob("[0-9]*"):
        try:
            if pid_dir.stat().st_uid != uid:
                continue
            env_file = pid_dir / "environ"
            if not env_file.exists():
                continue
            proc_env = {}
            for line in env_file.read_text(errors="ignore").split("\x00"):
                if "=" in line:
                    k, v = line.split("=", 1)
                    proc_env[k] = v
            if "WAYLAND_DISPLAY" in proc_env or "DISPLAY" in proc_env:
                for var in ("WAYLAND_DISPLAY", "DISPLAY", "XAUTHORITY",
                            "XDG_RUNTIME_DIR"):
                    if var in proc_env and not env.get(var):
                        env[var] = proc_env[var]
                break
        except Exception:
            continue


def _hypr_env() -> dict:
    """Environnement complet pour hyprctl/grim/slurp : sans
    HYPRLAND_INSTANCE_SIGNATURE hyprctl répond « no running instance »."""
    env = {**os.environ}
    _restore_display_env(env)
    if not env.get("XDG_RUNTIME_DIR"):
        try:
            cand = Path(f"/run/user/{os.getuid()}")
            if cand.exists():
                env["XDG_RUNTIME_DIR"] = str(cand)
        except Exception:
            pass
    if not env.get("HYPRLAND_INSTANCE_SIGNATURE"):
        try:
            rd = Path(env.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
            hypr_dir = rd / "hypr"
            if hypr_dir.exists():
                inst = sorted((d for d in hypr_dir.iterdir() if d.is_dir()),
                              key=lambda d: d.stat().st_mtime, reverse=True)
                if inst:
                    env["HYPRLAND_INSTANCE_SIGNATURE"] = inst[0].name
        except Exception:
            pass
    return env


def _hyprctl_json(*args: str):
    """Lecture Hyprland partagée (socle : délai, reprise, cache court).

    La copie locale relançait un processus par question ; le cache du socle
    fusionne les appels d'un même tour de parole entre toutes les actions.
    """
    return kit.hypr_json(*args, default=None)


def _hypr_dispatch(dispatcher: str, arg: str = "") -> bool:
    """Hyprland répond 0 même pour un dispatcher inconnu : on lit la sortie."""
    res = kit.hypr("dispatch", dispatcher, *([arg] if arg else []))
    if not res.ok:
        return False
    out = res.out.lower()
    return not any(bad in out for bad in ("error", "unknown", "invalid"))


def _active_workspace_id() -> str:
    """Lit l'identifiant du bureau réellement affiché par Hyprland."""
    data = _hyprctl_json("activeworkspace")
    if not isinstance(data, dict):
        return ""
    return str(data.get("id") or data.get("name") or "").strip()


def _confirm_workspace(workspace: str, previous: str = "") -> bool:
    """Un succès de dispatcher n'est pas une preuve que le focus a changé."""
    kit.hypr_invalidate()
    target = str(workspace)
    relative = bool(re.fullmatch(r"e[+-]\d+", target))
    return kit.wait_until(
        lambda: (_active_workspace_id() != previous if relative and previous else
                 _active_workspace_id() == target),
        timeout=0.65, interval=0.04, max_interval=0.12,
    )


def _confirm_window_workspace(address: str, workspace: str) -> bool:
    """Vérifie la fenêtre ciblée, plutôt que d'annoncer un déplacement."""
    kit.hypr_invalidate()

    def moved() -> bool:
        for client in (_hyprctl_json("clients") or []):
            if str(client.get("address") or "") != address:
                continue
            current = client.get("workspace") or {}
            return str(current.get("id") or current.get("name") or "") == str(workspace)
        return False

    return kit.wait_until(moved, timeout=0.65, interval=0.04, max_interval=0.12)


# ════════════════════════════════════════════════════════════════════════════
# Extraction workspace — chiffres ET ordinaux français
# ════════════════════════════════════════════════════════════════════════════

_WS = r"(?:bureau|workspace|ws|espace\s+de\s+travail|desktop)"
_PREP = r"(?:dans\s+(?:le|la)?|au|sur\s+(?:le|la)?|du|de\s+|le|la)?\s*"
_ORD_FR = (
    r"premi(?:er|ère|ere|re)|deuxi[èe]me|second[e]?|troisi[èe]me|quatri[èe]me|"
    r"cinqui[èe]me|sixi[èe]me|septi[èe]me|huiti[èe]me|neuvi[èe]me|dixi[èe]me"
)


def _ordinal_to_int(word: str) -> Optional[int]:
    import unicodedata
    w = unicodedata.normalize("NFKD", (word or "").lower().replace("-", " "))
    w = "".join(c for c in w if not unicodedata.combining(c))
    checks = [
        (r"premi", 1), (r"deux|second", 2), (r"trois", 3), (r"quatr", 4),
        (r"cinqu", 5), (r"six", 6), (r"sept", 7), (r"huit", 8), (r"neuv", 9),
        (r"dix", 10),
    ]
    for rx, val in checks:
        if re.search(rx, w):
            return val
    return None


def _parse_workspace_value(value) -> Optional[str]:
    """« 4 », « e+1 », « bureau 4 », « deuxième bureau » → cible Hyprland.
    None si incompréhensible."""
    if value is None:
        return None
    v = str(value).strip()
    if not v:
        return None
    if re.fullmatch(r"[+-]?\d+", v) or re.fullmatch(r"e[+-]\d+", v):
        return v
    m = re.search(rf"{_PREP}{_WS}\s*(?:n[°o]?\s*|num[ée]ro\s+)?(\d+)", v, re.I) \
        or re.search(rf"{_PREP}(\d+)\s*(?:er|ère|ere|[èe]me|eme|e)?\s*{_WS}", v, re.I)
    if m:
        return m.group(1)
    m = re.search(rf"{_PREP}({_ORD_FR})\s*{_WS}", v, re.I) \
        or re.search(rf"{_PREP}{_WS}\s*(?:n[°o]?\s*|num[ée]ro\s+)?({_ORD_FR})", v, re.I)
    if m:
        n = _ordinal_to_int(m.group(1))
        if n:
            return str(n)
    return None


# ════════════════════════════════════════════════════════════════════════════
# État système : presse-papiers, fenêtre active, écran
# ════════════════════════════════════════════════════════════════════════════

def _clipboard_current_content() -> str:
    """Contenu actuel du VRAI presse-papiers (pas la sélection primaire)."""
    if _WAYLAND and _have("wl-paste"):
        try:
            res = _run(["wl-paste"], timeout=1)
            return res.stdout.strip() if res.returncode == 0 else ""
        except Exception:
            pass
    if _X11 and _have("xclip"):
        try:
            res = _run(["xclip", "-selection", "c", "-o"], timeout=1)
            return res.stdout.strip() if res.returncode == 0 else ""
        except Exception:
            pass
    try:
        import pyperclip
        return pyperclip.paste().strip()
    except ImportError:
        return ""


def _active_window_title() -> str:
    if _WAYLAND and _have("hyprctl"):
        win = _hyprctl_json("activewindow")
        if isinstance(win, dict):
            return win.get("title", "") or ""
    if _X11 and _have("xdotool"):
        try:
            res = _run(["xdotool", "getactivewindow", "getwindowname"], timeout=1)
            return res.stdout.strip()
        except Exception:
            pass
    return ""


def _screen_size() -> Tuple[int, int]:
    if _WAYLAND and _have("hyprctl"):
        monitors = _hyprctl_json("monitors")
        if isinstance(monitors, list) and monitors:
            m = monitors[0]
            return int(m.get("width", 1920)), int(m.get("height", 1080))
    if _X11 and _have("xrandr"):
        try:
            out = _run(["xrandr"], timeout=2).stdout
            match = re.search(r"current\s+(\d+)\s*x\s*(\d+)", out)
            if match:
                return int(match.group(1)), int(match.group(2))
        except Exception:
            pass
    try:
        import pyautogui
        size = pyautogui.size()
        return size.width, size.height
    except ImportError:
        return (1920, 1080)


# ════════════════════════════════════════════════════════════════════════════
# Capture d'écran robuste
# ════════════════════════════════════════════════════════════════════════════

def take_screenshot(save_path: Optional[str] = None) -> Path:
    """Capture d'écran native (grim sous Wayland), sauvegardée dans un
    fichier temporaire ou donné."""
    if save_path:
        out = Path(save_path).expanduser().resolve()
    else:
        out = Path.home() / "Desktop" / f"jarvis_screenshot_{int(time.time())}.png"
    out.parent.mkdir(parents=True, exist_ok=True)

    if _WAYLAND and _have("grim"):
        try:
            _run(["grim", str(out)], timeout=4)
            if out.exists():
                return out
        except Exception as e:
            print(f"[ComputerControl] grim failed: {e}")
    if _X11:
        for cmd in (["maim", str(out)], ["import", "-window", "root", str(out)],
                    ["scrot", str(out)], ["gnome-screenshot", "-f", str(out)]):
            if _have(cmd[0]):
                try:
                    _run(cmd, timeout=4)
                    if out.exists():
                        return out
                except Exception:
                    continue
    try:
        import pyautogui
        img = pyautogui.screenshot()
        img.save(str(out))
        return out
    except ImportError:
        raise RuntimeError("Aucun outil de capture disponible. Installez grim, maim ou pyautogui.")


# ════════════════════════════════════════════════════════════════════════════
# Presse-papiers
# ════════════════════════════════════════════════════════════════════════════

def _clipboard_copy(text: str) -> bool:
    if _WAYLAND and _have("wl-copy"):
        try:
            # wl-copy se démonise en gardant ses tubes : ne pas attendre sa sortie.
            if kit.run(["wl-copy", "--trim-newline"], stdin=text, timeout=2,
                       env=_hypr_env(), capture=False):
                return True
        except Exception:
            pass
    if _X11 and _have("xclip"):
        try:
            if kit.run(["xclip", "-selection", "c"], stdin=text, timeout=2,
                       capture=False):
                return True
        except Exception:
            pass
    try:
        import pyperclip
        pyperclip.copy(text)
        return True
    except ImportError:
        pass
    return False


def _clipboard_paste() -> str:
    if _WAYLAND and _have("wtype"):
        if _run(["wtype", "-M", "ctrl", "-k", "v", "-m", "ctrl"],
                timeout=1.5, env=_hypr_env()):
            return "Collé (wtype)"
    if _X11 and _have("xdotool"):
        if _run(["xdotool", "key", "ctrl+v"], timeout=1.5):
            return "Collé (xdotool)"
    try:
        import pyautogui
        pyautogui.hotkey("ctrl", "v")
        return "Collé (pyautogui)"
    except ImportError:
        return "Impossible de coller : installez wtype, xdotool ou pyautogui."


# ════════════════════════════════════════════════════════════════════════════
# Saisie texte / touches (ydotool Linux keycodes + wtype fallback)
# ════════════════════════════════════════════════════════════════════════════

_LINUX_KEYCODES: Dict[str, int] = {
    "esc": 1, "escape": 1,
    "1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7, "7": 8, "8": 9, "9": 10, "0": 11,
    "backspace": 14,
    "tab": 15,
    "q": 16, "w": 17, "e": 18, "r": 19, "t": 20, "y": 21, "u": 22, "i": 23, "o": 24, "p": 25,
    "enter": 28, "return": 28,
    "ctrl": 29, "control": 29, "leftctrl": 29,
    "a": 30, "s": 31, "d": 32, "f": 33, "g": 34, "h": 35, "j": 36, "k": 37, "l": 38,
    "shift": 42, "leftshift": 42,
    "z": 44, "x": 45, "c": 46, "v": 47, "b": 48, "n": 49, "m": 50,
    "alt": 56, "leftalt": 56,
    "space": 57, "spacebar": 57,
    "up": 103,
    "left": 105,
    "right": 106,
    "down": 108,
    "delete": 111,
    "super": 125, "meta": 125, "win": 125, "logo": 125,
}

_WTYPE_MODS = {"ctrl": "ctrl", "control": "ctrl", "shift": "shift",
               "alt": "alt", "meta": "alt", "super": "super",
               "logo": "super", "hyper": "hyper", "win": "super"}


def _type_text(text: str, interval: float = 0.03) -> str:
    if _WAYLAND:
        if _have("ydotool"):
            try:
                delay_ms = max(5, int(interval * 1000))
                r = _run(["ydotool", "type", "-d", str(delay_ms), "--", text],
                         timeout=max(3.0, len(text) * 0.05 + 2.0))
                if r.returncode == 0:
                    return f"Texte tapé : «{text[:60]}{'…' if len(text) > 60 else ''}»"
            except Exception:
                pass
        if _have("wtype"):
            try:
                r = _run(["wtype", text], timeout=max(2.0, len(text) // 20 + 2.0),
                         env=_hypr_env())
                if r.returncode == 0:
                    return f"Texte tapé : «{text[:60]}{'…' if len(text) > 60 else ''}»"
            except Exception:
                pass
        # Repli presse-papiers sous Wayland
        try:
            if _clipboard_copy(text):
                pasted = _clipboard_paste()
                if not pasted.startswith("Collé"):
                    return f"Échec de saisie : {pasted}"
                return f"Texte tapé : «{text[:60]}{'…' if len(text) > 60 else ''}»"
        except Exception:
            pass

    if _X11 and _have("xdotool"):
        try:
            if _run(["xdotool", "type", "--delay", str(int(interval * 1000)),
                     text], timeout=max(2.0, len(text) // 10 + 2.0)):
                return f"Texte tapé : «{text[:60]}{'…' if len(text) > 60 else ''}»"
        except Exception:
            pass
    try:
        import pyautogui
        pyautogui.typewrite(text, interval=interval)
        return f"Texte tapé : «{text[:60]}{'…' if len(text) > 60 else ''}»"
    except (ImportError, Exception):
        return "Aucun outil de saisie disponible (ydotool, wtype, xdotool ou pyautogui)."


def _terminal_command_from_voice_text(text: str) -> str:
    """Extrait une commande si le modèle a recopié toute la consigne vocale."""
    raw = str(text or "").strip()
    match = re.search(
        r"\b(?:tu\s+)?(?:tape(?:s|r)?|écri(?:s|re)|saisi(?:s|r)|entre(?:s|r))\s+"
        r"(?:(?:la|une)\s+)?commande\s+(?P<command>.+)$",
        raw,
        flags=re.IGNORECASE,
    )
    if not match:
        return raw
    command = match.group("command").strip().strip("'\"«» ")
    # L'indication « dans Kitty sur le bureau 3 » vise la fenêtre, jamais la
    # commande à taper. On ne retire que les noms de terminaux connus.
    command = re.sub(
        r"\s+(?:dans|sur)\s+(?:kitty|le\s+terminal|terminal|konsole|alacritty|foot)\b.*$",
        "",
        command,
        flags=re.IGNORECASE,
    ).strip().rstrip(".?!")
    return command or raw


def _press_key(key: str) -> str:
    key = (key or "").strip()
    if not key:
        return "Aucune touche fournie."
    if "+" in key:
        return _hotkey(*[k for k in key.replace("-", "+").split("+") if k.strip()])
    k_clean = key.lower().strip()
    if _WAYLAND:
        if _have("ydotool") and k_clean in _LINUX_KEYCODES:
            code = _LINUX_KEYCODES[k_clean]
            try:
                r = _run(["ydotool", "key", f"{code}:1", f"{code}:0"], timeout=2)
                if r.returncode == 0:
                    return f"Touche pressée : {key}"
            except Exception:
                pass
        if _have("wtype"):
            try:
                r = _run(["wtype", "-k", key], timeout=1.5, env=_hypr_env())
                if r.returncode == 0:
                    return f"Touche pressée : {key}"
            except Exception:
                pass
    if _X11 and _have("xdotool"):
        try:
            r = _run(["xdotool", "key", key], timeout=1.5)
            if r.returncode == 0:
                return f"Touche pressée : {key}"
        except Exception:
            pass
    try:
        import pyautogui
        pyautogui.press(key)
        return f"Touche pressée : {key}"
    except (ImportError, Exception):
        return "Aucun outil de clavier (ydotool, wtype, xdotool ou pyautogui)."


def _hotkey(*keys: str) -> str:
    """Combinaison de touches. Sous Wayland, utilise ydotool key en priorité,
    avec repli sur wtype (-M/-k/-m)."""
    keys = [k.strip().lower() for k in keys if k and k.strip()]
    if not keys:
        return "Aucune combinaison de touches fournie."
    combo = "+".join(keys)
    if _WAYLAND:
        if _have("ydotool") and all(k in _LINUX_KEYCODES for k in keys):
            events = [f"{_LINUX_KEYCODES[k]}:1" for k in keys] + [f"{_LINUX_KEYCODES[k]}:0" for k in reversed(keys)]
            try:
                r = _run(["ydotool", "key", *events], timeout=2)
                if r.returncode == 0:
                    return f"Combinaison envoyée : {combo}"
            except Exception:
                pass
        if _have("wtype"):
            mods = [k for k in keys if k in _WTYPE_MODS]
            nonmods = [k for k in keys if k not in _WTYPE_MODS]
            cmd = ["wtype"]
            for m in mods:
                cmd += ["-M", _WTYPE_MODS[m]]
            for k in nonmods or ["space"]:
                cmd += ["-k", k]
            for m in reversed(mods):
                cmd += ["-m", _WTYPE_MODS[m]]
            try:
                r = _run(cmd, timeout=2, env=_hypr_env())
                if r.returncode == 0:
                    return f"Combinaison envoyée : {combo}"
            except Exception:
                pass
    if _X11 and _have("xdotool"):
        try:
            r = _run(["xdotool", "key", combo], timeout=2)
            if r.returncode == 0:
                return f"Combinaison envoyée : {combo}"
        except Exception:
            pass
    try:
        import pyautogui
        pyautogui.hotkey(*keys)
        return f"Combinaison envoyée : {combo}"
    except (ImportError, Exception):
        return "Aucun outil de clavier (ydotool, wtype, xdotool ou pyautogui)."


# ════════════════════════════════════════════════════════════════════════════
# Effacement
# ════════════════════════════════════════════════════════════════════════════

# Dernier texte tapé par ANO : « efface » retire exactement ces caractères, à
# coups de Retour arrière — seule méthode qui marche partout (terminal,
# navigateur, éditeur) sans toucher au reste de la ligne.
_last_typed: Dict[str, Any] = {}
_LAST_TYPED_TTL_S = 600.0
_MAX_ERASE = 500

# Dans un terminal, Ctrl+A ramène en début de ligne au lieu de tout
# sélectionner : Ctrl+A puis Suppr effaçait la première lettre (« salut »
# devenait « alut »). Readline : Ctrl+E fin de ligne, Ctrl+U efface avant,
# Ctrl+W efface le mot précédent.
_TERMINAL_CLASSES = (
    "kitty", "foot", "alacritty", "wezterm", "konsole", "terminal", "xterm",
    "ghostty", "terminator", "tilix", "urxvt", "st-256color",
)


def _active_window_info() -> Dict[str, str]:
    win = _hyprctl_json("activewindow") if (_WAYLAND and _have("hyprctl")) else None
    if isinstance(win, dict):
        return {"address": str(win.get("address") or ""),
                "class": str(win.get("class") or win.get("initialClass") or "")}
    return {"address": "", "class": ""}


def _is_terminal(window_class: str) -> bool:
    cls = (window_class or "").casefold()
    return any(name in cls for name in _TERMINAL_CLASSES)


def _remember_typed(text: str, submitted: bool) -> None:
    kit.hypr_invalidate()
    _last_typed.clear()
    _last_typed.update(text=text, submitted=submitted, at=time.monotonic(),
                       **_active_window_info())


def _press_repeat(key: str, count: int) -> bool:
    """Une touche répétée en un seul processus (et non N lancements)."""
    count = max(0, min(int(count), _MAX_ERASE))
    if not count:
        return True
    k = key.lower()
    if _WAYLAND:
        if _have("ydotool") and k in _LINUX_KEYCODES:
            code = _LINUX_KEYCODES[k]
            events = [f"{code}:{state}" for _ in range(count) for state in (1, 0)]
            try:
                if _run(["ydotool", "key", *events], timeout=2 + count * 0.02).returncode == 0:
                    return True
            except Exception:
                pass
        if _have("wtype"):
            name = {"backspace": "BackSpace", "delete": "Delete"}.get(k, key)
            cmd = ["wtype"] + ["-k", name] * count
            try:
                if _run(cmd, timeout=2 + count * 0.02, env=_hypr_env()).returncode == 0:
                    return True
            except Exception:
                pass
    return all(_press_key(key).startswith("Touche pressée") for _ in range(count))


def _clear_line(terminal: bool) -> str:
    if terminal:
        _hotkey("ctrl", "e")
        return _hotkey("ctrl", "u")
    _hotkey("ctrl", "a")
    return _press_key("backspace")


def _erase(scope: str = "last", count: Any = None) -> str:
    scope = (scope or "last").strip().casefold()
    info = _active_window_info()
    terminal = _is_terminal(info["class"])

    if count not in (None, "", 0, "0"):
        try:
            n = int(count)
        except (TypeError, ValueError):
            return f"Nombre de caractères invalide : {count!r}."
        if n <= 0:
            return "Rien à effacer."
        n = min(n, _MAX_ERASE)
        if not _press_repeat("backspace", n):
            return "Effacement impossible : aucun outil de clavier n'a répondu."
        return f"{n} caractère(s) effacé(s)."

    if scope in ("all", "tout", "field", "champ", "line", "ligne"):
        res = _clear_line(terminal)
        _last_typed.clear()
        if res.startswith(("Touche pressée", "Combinaison envoyée")):
            return "Ligne effacée." if terminal else "Champ effacé."
        return f"Effacement impossible : {res}"

    if scope in ("word", "mot"):
        res = _hotkey("ctrl", "w") if terminal else _hotkey("ctrl", "backspace")
        if res.startswith("Combinaison envoyée"):
            return "Dernier mot effacé."
        return f"Effacement impossible : {res}"

    # « efface » : ce qu'ANO vient de taper, rien de plus.
    last = dict(_last_typed)
    if not last or time.monotonic() - float(last.get("at", 0)) > _LAST_TYPED_TTL_S:
        return ("Je n'ai rien tapé récemment. Si c'est l'utilisateur qui a écrit, "
                "rappelle erase avec scope 'all' (toute la ligne) ou 'word' (dernier "
                "mot) selon sa demande ; sinon demande-lui quoi effacer.")
    if last.get("submitted"):
        return ("Le texte a déjà été validé par Entrée : je ne peux plus l'effacer "
                "au clavier.")
    if last.get("address") and info["address"] and last["address"] != info["address"]:
        return ("La fenêtre active n'est plus celle où j'ai tapé : je n'efface rien "
                "pour ne pas toucher à un autre texte. Reviens sur cette fenêtre ou "
                "dis-moi quoi effacer.")
    text = str(last.get("text") or "")
    if not _press_repeat("backspace", len(text)):
        return "Effacement impossible : aucun outil de clavier n'a répondu."
    _last_typed.clear()
    return f"Texte effacé : «{text[:60]}{'…' if len(text) > 60 else ''}»"


# ════════════════════════════════════════════════════════════════════════════
# Souris (ydotool sous Wayland — attend des masques hex de boutons)
# ════════════════════════════════════════════════════════════════════════════

_YDOTOOL_BTN = {"left": "0xC0", "right": "0xC1", "middle": "0xC2"}


def _click(x=None, y=None, button="left", clicks=1) -> str:
    button = (button or "left").lower()
    clicks = max(1, int(clicks or 1))
    if _WAYLAND and _have("ydotool"):
        try:
            if x is not None and y is not None:
                mv = _run(["ydotool", "mousemove", "-a",
                           str(int(x)), str(int(y))], timeout=2)
                if not mv:
                    raise RuntimeError(mv.reason())
            mask = _YDOTOOL_BTN.get(button, "0xC0")
            for _ in range(clicks):
                ck = _run(["ydotool", "click", mask], timeout=2)
                if not ck:
                    raise RuntimeError(ck.reason())
            return (f"Clic {button} à ({x},{y})" if x is not None
                    else f"Clic {button} à la position actuelle")
        except Exception as e:
            return f"Échec du clic (ydotool) : {e}"
    try:
        import pyautogui
        if x is not None and y is not None:
            pyautogui.click(x, y, button=button, clicks=clicks)
            return f"Clic {button} à ({x},{y})"
        pyautogui.click(button=button, clicks=clicks)
        return f"Clic {button} à la position actuelle"
    except ImportError:
        return "Aucun outil de souris (ydotool ou pyautogui)."


def _move(x: int, y: int) -> str:
    ix, iy = int(x), int(y)
    if _WAYLAND:
        if _have("hyprctl"):
            lua = f"hl.dsp.cursor.move({{ x = {ix}, y = {iy} }})"
            if _HAS_WINDOW_INSTANCES:
                try:
                    if _hypr_dispatch_hyprland(legacy_cmd="movecursor", legacy_args=f"{ix} {iy}", lua_cmd=lua):
                        return f"Souris → ({ix},{iy})"
                except Exception:
                    pass
            elif _hypr_dispatch(lua) or _hypr_dispatch("movecursor", f"{ix} {iy}"):
                return f"Souris → ({ix},{iy})"
        if _have("ydotool"):
            try:
                r = _run(["ydotool", "mousemove", "-a", str(ix), str(iy)], timeout=2)
                if r.returncode == 0:
                    return f"Souris → ({ix},{iy})"
            except Exception as e:
                return f"Échec du mouvement (ydotool) : {e}"
    try:
        import pyautogui
        pyautogui.moveTo(ix, iy, duration=0.2)
        return f"Souris → ({ix},{iy})"
    except (ImportError, Exception):
        return "Mouvement souris impossible (hyprctl, ydotool ou pyautogui requis)."


def _scroll(direction: str, amount: int = 3) -> str:
    direction = (direction or "down").lower()
    amount = max(1, int(amount or 3))
    if _WAYLAND and _have("ydotool"):
        dy = amount if direction == "up" else -amount if direction == "down" else 0
        dx = amount if direction == "right" else -amount if direction == "left" else 0
        try:
            r = _run(["ydotool", "scroll", "--", str(dx), str(dy)], timeout=2)
            if r.returncode == 0:
                return f"Défilement {direction} x{amount}"
        except Exception:
            pass
    try:
        import pyautogui
        if direction in ("up", "down"):
            pyautogui.scroll(amount if direction == "up" else -amount)
        else:
            pyautogui.hscroll(amount if direction == "right" else -amount)
        return f"Défilement {direction} x{amount}"
    except ImportError:
        return "Défilement impossible (ydotool ou pyautogui requis)."


# ════════════════════════════════════════════════════════════════════════════
# Fenêtres : focus, liste, bureaux
# ════════════════════════════════════════════════════════════════════════════

def _focus_window(title: str) -> str:
    if _WAYLAND and _have("hyprctl"):
        try:
            clients = _hyprctl_json("clients") or []
            needle = (title or "").lower().strip()
            target = None

            # Support des sélecteurs préfixés address:0x... et adresses hexadécimales brutes 0x...
            query_addr = None
            if needle.startswith("address:"):
                query_addr = needle[8:].strip()
            elif needle.startswith("0x"):
                query_addr = needle

            for c in clients:
                c_addr = str(c.get("address") or "").lower().strip()
                if query_addr and (c_addr == query_addr or c_addr.lstrip("0x") == query_addr.lstrip("0x")):
                    target = c
                    break
                blob = f"{(c.get('title') or '')} {(c.get('class') or '')}".lower()
                if needle and needle in blob:
                    target = c
                    break
            if not target:
                open_titles = ", ".join(
                    f"«{c.get('title') or c.get('class')}»" for c in clients[:10]
                ) or "aucune"
                return (f"Aucune fenêtre correspondant à «{title}» trouvée. "
                        f"Fenêtres ouvertes : {open_titles}")
            addr = target.get("address")
            # Ramener la fenêtre sur le bureau courant si elle est ailleurs,
            # sinon le focus partirait dans le vide.
            try:
                active_ws = _hyprctl_json("activeworkspace") or {}
                current = active_ws.get("id")
                win_ws = (target.get("workspace") or {}).get("id")
                if (current is not None and win_ws is not None
                        and win_ws != current and addr):
                    _hypr_dispatch("movetoworkspacesilent",
                                   f"{current},address:{addr}")
            except Exception:
                pass
            if _HAS_WINDOW_INSTANCES and addr:
                _hypr_focus_window(f"address:{addr}")
            elif addr:
                _hypr_dispatch("focuswindow", f"address:{addr}")
            time.sleep(0.2)
            return f"Fenêtre focalisée : «{target.get('title') or target.get('class')}»"
        except Exception as e:
            return f"Erreur hyprctl lors du focus : {e}"
    if _have("wmctrl"):
        r = _run(["wmctrl", "-a", title], timeout=2)
        if r.returncode == 0:
            return f"Fenêtre focalisée : «{title}»"
        return f"Aucune fenêtre correspondant à «{title}» (wmctrl)."
    if _have("xdotool"):
        r = _run(["xdotool", "search", "--name", title, "windowactivate"],
                 timeout=2)
        if r.returncode == 0:
            return f"Fenêtre focalisée : «{title}»"
        return f"Aucune fenêtre correspondant à «{title}» (xdotool)."
    return "Aucun outil de focus disponible (hyprctl, wmctrl ou xdotool)."


def _list_windows() -> str:
    if _WAYLAND and _have("hyprctl"):
        try:
            clients = _hyprctl_json("clients") or []
            if not clients:
                return "Aucune fenêtre ouverte."
            lines = [
                f"  - [bureau {(c.get('workspace') or {}).get('id', '?')}] "
                f"{c.get('title') or c.get('class') or 'sans titre'}"
                for c in clients
            ]
            return "Fenêtres ouvertes :\n" + "\n".join(lines)
        except Exception as e:
            return f"Impossible de lister les fenêtres : {e}"
    if _have("wmctrl"):
        r = _run(["wmctrl", "-l"], timeout=2)
        return ("Fenêtres ouvertes :\n" + r.stdout.strip()) \
            if r.returncode == 0 and r.stdout.strip() \
            else "Impossible de lister les fenêtres (wmctrl)."
    return "Aucun outil de liste de fenêtres disponible (hyprctl ou wmctrl)."


def _move_to_workspace(title: str, workspace) -> str:
    ws = _parse_workspace_value(workspace)
    if ws is None:
        return f"Numéro de bureau invalide : '{workspace}'."
    if _WAYLAND and _have("hyprctl"):
        try:
            target = None
            if title:
                needle = title.lower().strip()
                for c in (_hyprctl_json("clients") or []):
                    blob = f"{(c.get('title') or '')} {(c.get('class') or '')}".lower()
                    if needle and needle in blob:
                        target = c
                        break
                if not target:
                    open_titles = ", ".join(
                        f"«{c.get('title') or c.get('class')}»"
                        for c in (_hyprctl_json("clients") or [])[:10]
                    ) or "aucune"
                    return (f"Aucune fenêtre correspondant à «{title}» trouvée. "
                            f"Fenêtres ouvertes : {open_titles}")
            else:
                active = _hyprctl_json("activewindow")
                target = active if isinstance(active, dict) and active.get("address") else None
            if not target:
                return "Aucune fenêtre active à déplacer."
            addr = target.get("address")
            if _HAS_WINDOW_INSTANCES:
                _hypr_move_window_to_workspace(f"address:{addr}", ws, follow=False)
            else:
                _hypr_dispatch("movetoworkspacesilent", f"{ws},address:{addr}")
            label = target.get("title") or target.get("class") or "active"
            if _confirm_window_workspace(str(addr), ws):
                return f"Déplacement confirmé : fenêtre «{label}» sur le bureau {ws}."
            return (f"Déplacement envoyé pour «{label}» vers le bureau {ws}, "
                    "mais Hyprland ne l'a pas confirmé.")
        except Exception as e:
            return f"Échec du déplacement vers le bureau {ws} : {e}"
    if _have("wmctrl"):
        try:
            ws_int = int(ws)
            _run(["wmctrl", "-r", title or ":ACTIVE:", "-t",
                  str(ws_int - 1)], timeout=2, check=True)
            return f"Fenêtre déplacée vers le bureau {ws} (wmctrl)."
        except Exception as e:
            return f"Échec du déplacement : {e}"
    return "Déplacement de fenêtre non supporté sur ce système (hyprctl ou wmctrl requis)."


def _switch_workspace(workspace) -> str:
    ws = _parse_workspace_value(workspace)
    if ws is None:
        return f"Numéro de bureau invalide : '{workspace}'."
    if _WAYLAND and _have("hyprctl"):
        previous = _active_workspace_id()
        dispatched = False
        if _HAS_WINDOW_INSTANCES:
            try:
                dispatched = bool(_hypr_dispatch_hyprland(
                    legacy_cmd="workspace",
                    legacy_args=ws,
                    lua_cmd=f'hl.dsp.focus({{ workspace = "{ws}" }})',
                ))
            except Exception:
                pass
        if not dispatched:
            dispatched = _hypr_dispatch("workspace", ws)
        if dispatched and _confirm_workspace(ws, previous=previous):
            return f"Navigation confirmée : bureau {_active_workspace_id()}. Aucune fenêtre n'a été déplacée."
        if dispatched:
            return (f"Navigation envoyée vers le bureau {ws}, mais Hyprland "
                    "ne l'a pas confirmée. Aucune fenêtre n'a été déplacée.")
        return f"Échec du basculement vers le bureau {ws}."
    if _have("wmctrl"):
        try:
            ws_int = int(ws)
            if _run(["wmctrl", "-s", str(ws_int - 1)], timeout=2):
                return f"Basculé vers le bureau {ws}."
        except ValueError:
            pass
    return "Changement de bureau non supporté sur ce système (hyprctl ou wmctrl requis)."


def _toggle_fullscreen() -> str:
    if _WAYLAND and _have("hyprctl"):
        ok = False
        if _HAS_WINDOW_INSTANCES:
            try:
                ok = _hypr_dispatch_hyprland(
                    legacy_cmd="fullscreen",
                    legacy_args="",
                    lua_cmd="hl.dsp.window.fullscreen()",
                )
            except Exception:
                pass
        if not ok:
            ok = _hypr_dispatch("hl.dsp.window.fullscreen()") or _hypr_dispatch("fullscreen")
        if ok:
            return "Plein écran basculé."
        return "Échec du basculement plein écran (Hyprland)."
    if _have("wmctrl"):
        r = _run(["wmctrl", "-r", ":ACTIVE:", "-b", "toggle,fullscreen"], timeout=2)
        if r.returncode == 0:
            return "Plein écran basculé (wmctrl)."
    return "Action plein écran non supportée."


def _toggle_float() -> str:
    if _WAYLAND and _have("hyprctl"):
        ok = False
        if _HAS_WINDOW_INSTANCES:
            try:
                ok = _hypr_dispatch_hyprland(
                    legacy_cmd="togglefloating",
                    legacy_args="",
                    lua_cmd="hl.dsp.window.float()",
                )
            except Exception:
                pass
        if not ok:
            ok = _hypr_dispatch("hl.dsp.window.float()") or _hypr_dispatch("togglefloating")
        if ok:
            return "Mode flottant basculé."
        return "Échec du basculement mode flottant (Hyprland)."
    return "Action mode flottant non supportée."


def _center_window() -> str:
    if _WAYLAND and _have("hyprctl"):
        ok = False
        if _HAS_WINDOW_INSTANCES:
            try:
                ok = _hypr_dispatch_hyprland(
                    legacy_cmd="centerwindow",
                    legacy_args="",
                    lua_cmd="hl.dsp.window.center()",
                )
            except Exception:
                pass
        if not ok:
            ok = _hypr_dispatch("hl.dsp.window.center()") or _hypr_dispatch("centerwindow")
        if ok:
            return "Fenêtre centrée."
        return "Échec du centrage de la fenêtre (Hyprland)."
    return "Action centrage non supportée."


def _close_window(target: str = "") -> str:
    target = (target or "").strip()
    if _HAS_WINDOW_INSTANCES:
        try:
            if target:
                ok = _hypr_close_window(target)
            else:
                active = _hyprctl_json("activewindow")
                if isinstance(active, dict) and active.get("address"):
                    ok = _hypr_close_window(f"address:{active['address']}")
                else:
                    ok = _hypr_dispatch("killactive")
            if ok:
                return f"Fenêtre{' «' + target + '»' if target else ''} fermée."
        except Exception:
            pass
    elif _WAYLAND and _have("hyprctl"):
        if target:
            ok = _hypr_dispatch("closewindow", target)
        else:
            ok = _hypr_dispatch("killactive")
        if ok:
            return f"Fenêtre{' «' + target + '»' if target else ''} fermée."
    if _have("wmctrl"):
        r = _run(["wmctrl", "-c", target or ":ACTIVE:"], timeout=2)
        if r.returncode == 0:
            return f"Fenêtre{' «' + target + '»' if target else ''} fermée (wmctrl)."
    return f"Impossible de fermer la fenêtre{' «' + target + '»' if target else ''}."


# ════════════════════════════════════════════════════════════════════════════
# Luminosité (brightnessctl, repli sysfs)
# ════════════════════════════════════════════════════════════════════════════

def _brightness_set(value: int) -> str:
    try:
        value = max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return "Valeur de luminosité invalide."
    if _have("brightnessctl"):
        try:
            r = _run(["brightnessctl", "set", f"{value}%"], timeout=3)
            if r.returncode == 0:
                return f"Luminosité réglée à {value}%."
        except Exception:
            pass
    try:
        base = Path("/sys/class/backlight")
        devs = [d for d in base.iterdir() if d.is_dir()]
        if devs:
            dev = devs[0]
            mx = int((dev / "max_brightness").read_text().strip())
            (dev / "brightness").write_text(str(int(mx * value / 100)))
            return f"Luminosité réglée à {value}%."
    except Exception:
        pass
    return ("Impossible de régler la luminosité : installez brightnessctl "
            "(sudo pacman -S brightnessctl) et vérifiez que votre utilisateur "
            "est dans le groupe video.")


def _brightness_get() -> str:
    if _have("brightnessctl"):
        try:
            cur = _run(["brightnessctl", "get"], timeout=2).stdout.strip()
            mx = _run(["brightnessctl", "max"], timeout=2).stdout.strip()
            if cur.isdigit() and mx.isdigit() and int(mx) > 0:
                pct = round(int(cur) * 100 / int(mx))
                return f"Luminosité actuelle : {pct}%."
        except Exception:
            pass
    try:
        base = Path("/sys/class/backlight")
        devs = [d for d in base.iterdir() if d.is_dir()]
        if devs:
            dev = devs[0]
            cur = int((dev / "brightness").read_text().strip())
            mx = int((dev / "max_brightness").read_text().strip())
            return f"Luminosité actuelle : {round(cur * 100 / mx)}%."
    except Exception:
        pass
    return "Impossible de lire la luminosité."


# ════════════════════════════════════════════════════════════════════════════
# État machine et audio PipeWire (lecture légère, sans poller en continu)
# ════════════════════════════════════════════════════════════════════════════

def _system_status() -> str:
    """Instantané borné de la machine, adapté aux deux cœurs disponibles."""
    parts = []
    try:
        load = os.getloadavg()
        parts.append(f"charge 1 min : {load[0]:.2f}")
    except OSError:
        pass
    try:
        memory = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, value = line.split(":", 1)
            memory[key] = int(value.strip().split()[0])
        total = memory.get("MemTotal", 0)
        available = memory.get("MemAvailable", 0)
        if total:
            used_pct = round((total - available) * 100 / total)
            parts.append(f"RAM : {used_pct}% utilisée")
    except (OSError, ValueError):
        pass
    active = _active_window_title()
    if active:
        parts.append(f"fenêtre active : {active[:100]}")
    if _WAYLAND and _have("hyprctl"):
        try:
            workspace = _hyprctl_json("activeworkspace") or {}
            if workspace.get("id") is not None:
                parts.append(f"bureau : {workspace['id']}")
        except Exception:
            pass
    if not parts:
        return "État système indisponible."
    return "État ordinateur — " + " · ".join(parts) + "."


def _workspace_overview() -> str:
    """Inventaire compact et déterministe des fenêtres, regroupé par bureau."""
    if not (_WAYLAND and _have("hyprctl")):
        return _list_windows()
    try:
        grouped: Dict[str, list[str]] = {}
        for client in _hyprctl_json("clients") or []:
            workspace = str((client.get("workspace") or {}).get("id", "?"))
            label = str(client.get("title") or client.get("class") or "sans titre").strip()
            grouped.setdefault(workspace, []).append(label[:80])
        if not grouped:
            return "Aucune fenêtre ouverte."
        lines = [f"Bureau {workspace} ({len(windows)}) : " + ", ".join(windows[:8])
                 for workspace, windows in sorted(grouped.items(), key=lambda row: row[0])]
        return "Vue des bureaux :\n" + "\n".join(lines)
    except Exception as exc:
        return f"Vue des bureaux indisponible : {exc}"


def _clipboard_status() -> str:
    """Indique l'état du presse-papiers sans jamais en révéler le contenu."""
    content = _clipboard_current_content()
    if not content:
        return "Presse-papiers vide."
    lines = content.count("\n") + 1
    return f"Presse-papiers prêt : {len(content)} caractères sur {lines} ligne(s)."


def _volume_get() -> str:
    if not _have("wpctl"):
        return "Audio PipeWire indisponible : installez wireplumber et wpctl."
    try:
        result = _run(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"], timeout=2)
        match = re.search(r"Volume:\s*([0-9.]+)", result.stdout)
        if result.returncode == 0 and match:
            muted = " [MUTED]" in result.stdout.upper()
            return f"Volume système : {round(float(match.group(1)) * 100)}%{' (muet)' if muted else ''}."
    except Exception:
        pass
    return "Impossible de lire le volume système."


def _volume_set(value: Any) -> str:
    try:
        percent = max(0, min(_MAX_VOLUME_PERCENT, int(value)))
    except (TypeError, ValueError):
        return "Valeur de volume invalide : utilisez 0 à 100."
    if not _have("wpctl"):
        return "Audio PipeWire indisponible : installez wireplumber et wpctl."
    try:
        result = _run(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{percent}%"], timeout=2)
        if result.returncode == 0:
            return f"Volume système réglé à {percent}%."
    except Exception:
        pass
    return "Impossible de régler le volume système."


def _volume_mute(mode: str = "toggle") -> str:
    mode = str(mode or "toggle").lower()
    if mode not in {"toggle", "on", "off"}:
        return "Mode muet invalide : toggle, on ou off."
    if not _have("wpctl"):
        return "Audio PipeWire indisponible : installez wireplumber et wpctl."
    try:
        result = _run(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", mode], timeout=2)
        if result.returncode == 0:
            return "Muet audio basculé." if mode == "toggle" else ("Audio coupé." if mode == "on" else "Audio rétabli.")
    except Exception:
        pass
    return "Impossible de modifier le mode muet."


# ════════════════════════════════════════════════════════════════════════════
# IA Screen Find (Gemini Vision)
# ════════════════════════════════════════════════════════════════════════════

def _screen_find(description: str) -> Optional[Tuple[int, int]]:
    """Centre réel d'un élément décrit, ou None. Jamais une approximation.

    Un clic mal placé n'est pas une imprécision, c'est une action destructrice :
    il peut tomber sur « Supprimer » ou « Acheter ». On réutilise donc la
    détection du pointeur visuel — boîte normalisée 0-1000, modèle vision
    configuré, boîte invraisemblable rejetée, gros plan de contre-vérification
    quand la confiance est moyenne — plutôt que de demander « x,y » à un modèle
    léger et de cliquer sur le premier nombre trouvé dans sa phrase.
    """
    try:
        from ui.visual_pointer import detect_screen_target_live
    except Exception as exc:
        print(f"[ComputerControl] Détection visuelle indisponible : {exc}")
        return None
    try:
        box = detect_screen_target_live(description)
    except Exception as exc:
        print(f"[ComputerControl] Erreur screen_find : {exc}")
        return None
    if box is None:
        return None
    ymin, xmin, ymax, xmax = box.box
    origin_x, origin_y = box.origin
    width, height = box.size
    if width <= 0 or height <= 0:
        return None
    # La boîte est exprimée dans le repère de la capture ; l'origine du
    # moniteur la ramène dans le bureau virtuel, sinon le clic part sur
    # l'écran principal quel que soit l'écran réellement regardé.
    center_x = origin_x + ((xmin + xmax) / 2.0) / 1000.0 * width
    center_y = origin_y + ((ymin + ymax) / 2.0) / 1000.0 * height
    if not (origin_x <= center_x <= origin_x + width
            and origin_y <= center_y <= origin_y + height):
        print(f"[ComputerControl] Cible « {description} » hors capture, clic annulé.")
        return None
    return int(round(center_x)), int(round(center_y))


# ════════════════════════════════════════════════════════════════════════════
# Parsing local des commandes de contrôle
# ════════════════════════════════════════════════════════════════════════════

def _parse_control_locally(text: str) -> Optional[Dict[str, Any]]:
    """Interprète une commande de contrôle en langage naturel.
    Renvoie un dict {action, params} ou None si non reconnu."""
    t = re.sub(r"\s+", " ", (text or "").lower().strip())
    t = re.sub(r"\b(s'il te pla[iî]t|stp|please|peux[- ]tu|tu peux|tu pourrais|"
               r"je veux|j'aimerais)\b", " ", t).strip()
    if not t:
        return None

    # Bureaux : suivant / précédent / numéro (ordinaux gérés en aval)
    if re.search(r"\b(?:bureau|workspace)\s+suivant\b|\bnext\s+workspace\b", t):
        return {"action": "switch_workspace", "params": {"workspace": "e+1"}}
    if re.search(r"\b(?:bureau|workspace)\s+pr[ée]c[ée]dent\b|\bprevious\s+workspace\b", t):
        return {"action": "switch_workspace", "params": {"workspace": "e-1"}}
    m = re.search(r"(?:passe|va|bascule|change|navigue|rends?-toi)\s+(?:au|sur|vers)?\s*"
                  r"(?:le\s+)?(?:bureau|workspace)\s+(\S+)", t)
    if m:
        return {"action": "switch_workspace", "params": {"workspace": m.group(1)}}
    m = re.search(r"d[ée]place\s+(?:la\s+fen[êe]tre\s+|cette\s+fen[êe]tre\s+)?"
                  r"(.+?)\s+(?:vers|au|sur)\s+(?:le\s+)?(?:bureau|workspace)\s+(\S+)", t)
    if m:
        return {"action": "move_to_workspace",
                "params": {"title": m.group(1).strip(), "workspace": m.group(2)}}

    # Fenêtrage : Plein écran / Flottant / Centrer / Fermer
    if re.search(r"\b(?:plein\s+[ée]cran|fullscreen)\b", t):
        return {"action": "fullscreen", "params": {}}
    if re.search(r"\b(?:centre|centrer)\s*(?:la\s+fen[êe]tre|l['\s]application|l['\s]app)?\b", t):
        return {"action": "center", "params": {}}
    if re.search(r"\b(?:rend\s+la\s+fen[êe]tre\s+flottante|mode\s+flottant|flottant[e]?|toggle\s+float)\b", t):
        return {"action": "float", "params": {}}
    m_close = re.search(r"^(?:ferme|fermer|close)\s+(?:la\s+fen[êe]tre\s+|l['\s]application\s+|l['\s]app\s+)?(.*)", t)
    if m_close:
        win = m_close.group(1).strip()
        win = re.sub(r"^(?:cette\s+fen[êe]tre|la\s+fen[êe]tre|l['\s]app)\b", "", win).strip()
        return {"action": "close", "params": {"window": win} if win else {}}

    # Déplacement souris
    m_move = re.search(r"(?:d[ée]place\s+(?:la\s+souris|le\s+curseur)|curseur|souris|bouge\s+la\s+souris)\s+(?:[àa]|en|vers)?\s*(\d+)\s*[,;\s]\s*(\d+)", t)
    if m_move:
        return {"action": "move", "params": {"x": int(m_move.group(1)), "y": int(m_move.group(2))}}

    # Saisie
    m = re.search(
        r"^(?:tape|[ée]cris?|saisis?|entre|type)\s+"
        r"(?:le\s+texte\s+|le\s+mot\s+|la\s+phrase\s+|la\s+commande\s+)?"
        r"[\"']?(.+?)[\"']?"
        r"(?:\s+(?:dans|sur)\s+(?:la\s+fen[êe]tre\s+|l['\s]application\s+|l['\s]app\s+|le\s+programme\s+)?[\"']?(.+?)[\"']?)?$",
        t
    )
    if m:
        text_val = m.group(1).strip()
        win_val = m.group(2).strip() if m.group(2) else ""
        win_val = re.sub(r"^(?:le\s+|la\s+|l['\s]|les\s+)", "", win_val).strip()
        press_enter = False
        if re.search(r"\s+et\s+(?:appuie\s+sur\s+entr[ée]e|valide)\b", text_val):
            text_val = re.sub(r"\s+et\s+(?:appuie\s+sur\s+entr[ée]e|valide)\b", "", text_val).strip()
            press_enter = True
        params_dict = {"text": text_val}
        if win_val:
            params_dict["window"] = win_val
        if press_enter:
            params_dict["press_enter"] = True
        return {"action": "type", "params": params_dict}

    # Effacement (avant « Touches » : « efface » n'est pas une touche)
    m = re.search(r"^(?:efface|effacer|supprime|enl[èe]ve)\b(.*)$", t)
    if m:
        rest = m.group(1)
        n = re.search(r"(\d+)\s*(?:caract[èe]res?|lettres?)", rest)
        if n:
            return {"action": "erase", "params": {"count": int(n.group(1))}}
        # « efface ce que j'ai écrit » : texte de l'utilisateur, pas d'ANO —
        # il n'y a rien à retirer caractère par caractère, c'est la ligne.
        if re.search(r"\b(?:tout|toute\s+la\s+ligne|la\s+ligne|le\s+champ)\b", rest) or \
                re.search(r"\bj\W?ai\s+(?:[ée]crit|tap[ée])", rest):
            return {"action": "erase", "params": {"scope": "all"}}
        if re.search(r"\b(?:dernier\s+)?mot\b", rest):
            return {"action": "erase", "params": {"scope": "word"}}
        return {"action": "erase", "params": {"scope": "last"}}

    # Presse-papiers
    if re.search(r"\bcolle\b|\bpaste\b|ctrl\+v", t):
        return {"action": "paste", "params": {}}
    if re.search(r"\bcopie\b|\bcopy\b|ctrl\+c", t):
        return {"action": "copy", "params": {}}

    # Clic (coordonnées ou description)
    m = re.search(r"(?:clique|click)\s+(?:sur\s+|[àa]\s+|au\s+|en\s+)?[\"']?(.+?)[\"']?$", t)
    if m:
        target = m.group(1).strip()
        cm = re.match(r"(\d+)\s*[,;]\s*(\d+)", target)
        if cm:
            return {"action": "click",
                    "params": {"x": int(cm.group(1)), "y": int(cm.group(2))}}
        return {"action": "click", "params": {"description": target}}

    # Défilement
    m = re.search(r"(?:d[ée]file|fais\s+d[ée]filer|scroll)\s+(?:vers\s+(?:le\s+|la\s+)?|vers\s+|en\s+)?"
                  r"(haut|bas|gauche|droite|up|down|left|right)"
                  r"(?:\s+de\s+(\d+))?", t)
    if m:
        dm = {"haut": "up", "bas": "down", "gauche": "left", "droite": "right",
              "up": "up", "down": "down", "left": "left", "right": "right"}
        return {"action": "scroll",
                "params": {"direction": dm.get(m.group(1), "down"),
                           "amount": int(m.group(2)) if m.group(2) else 3}}

    # Touches
    m = re.search(r"(?:appuie\s+sur|presse?|press)\s+(?:la\s+touche\s+)?([\w+]+)", t)
    if m:
        return {"action": "press", "params": {"keys": m.group(1)}}

    # Focus fenêtre
    m = re.search(r"(?:focalise|bascule\s+sur|va\s+sur|focus)\s+"
                  r"(?:la\s+fen[êe]tre\s+|l'application\s+|l'app\s+|le\s+programme\s+)?"
                  r"(.+)$", t)
    if m:
        return {"action": "focus", "params": {"title": m.group(1).strip()}}

    # Divers
    if re.search(r"\bliste\s+les\s+fen[êe]tres\b|\blist\s+windows\b", t):
        return {"action": "list_windows", "params": {}}
    if re.search(r"\b(capture|screenshot)\b", t):
        return {"action": "screenshot", "params": {}}
    m = re.search(r"attends?\s+(\d+)\s*s?|pause\s+(\d+)", t)
    if m:
        return {"action": "wait",
                "params": {"seconds": int(m.group(1) or m.group(2))}}
    m = re.search(r"(?:r[èe]gle|mets?|ajuste|d[ée]finis?)\s+(?:la\s+)?"
                  r"(?:luminosit[ée]|brillance)\s+(?:[àa]|sur)\s+(\d+)\s*%?", t)
    if m:
        return {"action": "brightness_set", "params": {"value": int(m.group(1))}}
    if re.search(r"\b(?:[ée]tat|statut|sant[ée])\s+(?:du\s+)?(?:pc|syst[èe]me|ordinateur)\b", t):
        return {"action": "system_status", "params": {}}
    if re.search(r"\b(?:vue|[ée]tat|liste)\s+(?:des\s+)?(?:bureaux|workspaces)\b", t):
        return {"action": "workspace_overview", "params": {}}
    if re.search(r"\b(?:statut|[ée]tat)\s+(?:du\s+)?presse[- ]papiers\b", t):
        return {"action": "clipboard_status", "params": {}}
    m = re.search(r"(?:r[èe]gle|mets?|baisse|monte)\s+(?:le\s+)?volume\s+(?:[àa]|sur)\s*(\d+)\s*%?", t)
    if m:
        return {"action": "volume_set", "params": {"value": int(m.group(1))}}
    if re.search(r"\b(?:coupe|active|d[ée]sactive|mute|muet)\s+(?:le\s+)?(?:son|audio|volume)\b", t):
        return {"action": "volume_mute", "params": {"mode": "toggle"}}
    if re.search(r"\b(?:quel\s+est\s+le\s+)?volume\b", t):
        return {"action": "volume_get", "params": {}}
    return None


def _detect_action_via_ai(description: str) -> Optional[Dict]:
    """Fallback IA si le parsing local échoue."""
    api_key = _get_api_key()
    if not api_key:
        return None
    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        prompt = f"""Tu es un assistant de contrôle d'ordinateur. L'utilisateur a dit : "{description}"
Actions possibles : type, paste, copy, click, scroll, press, focus, screenshot, wait, brightness_set, switch_workspace, move_to_workspace, list_windows.
Pour 'switch_workspace', si l'utilisateur demande le bureau suivant, renvoie {{"action":"switch_workspace","params":{{"workspace":"e+1"}}}}. S'il demande le précédent, renvoie {{"action":"switch_workspace","params":{{"workspace":"e-1"}}}}. S'il demande un bureau spécifique (ex: 2), renvoie {{"action":"switch_workspace","params":{{"workspace":"2"}}}}.
Pour 'type', renvoie {{"action":"type","params":{{"text":"..."}}}}.
Pour 'click', si c'est un texte à trouver à l'écran, utilise {{"action":"click","params":{{"description":"..."}}}}, si ce sont des coordonnées, utilise {{"action":"click","params":{{"x":..,"y":..}}}}.
Réponds UNIQUEMENT par un objet JSON valide."""
        resp = client.models.generate_content(model=FAST_MODEL,
                                              contents=prompt)
        text = (resp.text or "").strip()
        json_match = re.search(r'\{.*\}', text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group(0))
    except Exception as e:
        print(f"[ComputerControl] Erreur IA : {e}")
    return None


# ════════════════════════════════════════════════════════════════════════════
# Données aléatoires / utilisateur
# ════════════════════════════════════════════════════════════════════════════

_FIRST_NAMES = ["Alex", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Drew", "Quinn"]
_LAST_NAMES = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller"]
_DOMAINS = ["gmail.com", "yahoo.com", "outlook.com", "proton.me"]


def _random_data(dtype: str) -> str:
    dt = (dtype or "name").lower().strip()
    if dt == "name":
        return f"{random.choice(_FIRST_NAMES)} {random.choice(_LAST_NAMES)}"
    if dt == "email":
        return (f"{random.choice(_FIRST_NAMES).lower()}."
                f"{random.choice(_LAST_NAMES).lower()}"
                f"{random.randint(10, 99)}@{random.choice(_DOMAINS)}")
    if dt == "password":
        return ''.join(random.choices(string.ascii_letters + string.digits + "!@#", k=12))
    if dt == "phone":
        return f"+1{random.randint(200, 999)}{random.randint(1000000, 9999999)}"
    return f"random {dt} {random.randint(1000, 9999)}"


def _user_data(field: str) -> str:
    try:
        mem = json.loads(_MEMORY_PATH.read_text(encoding="utf-8"))
        return mem.get("identity", {}).get(field, {}).get("value", "")
    except Exception:
        return ""


# ════════════════════════════════════════════════════════════════════════════
# Point d'entrée
# ════════════════════════════════════════════════════════════════════════════

_CANONICAL_MAP = {
    "type": "type", "tape": "type", "écris": "type", "ecris": "type",
    "saisis": "type", "entre": "type", "type_text": "type",
    "erase": "erase", "efface": "erase", "effacer": "erase", "delete_text": "erase",
    "backspace": "erase", "undo_type": "erase", "supprime": "erase",
    "paste": "paste", "colle": "paste",
    "copy": "copy", "copie": "copy",
    "click": "click", "clique": "click",
    "move": "move", "déplace_souris": "move", "deplace_souris": "move",
    "mouse_move": "move", "souris": "move",
    "scroll": "scroll", "défile": "scroll", "defile": "scroll", "défiler": "scroll",
    "press": "press", "appuie": "press", "hotkey": "hotkey",
    "focus": "focus", "focus_window": "focus", "window_focus": "focus",
    "focalise": "focus", "bascule": "focus",
    "fullscreen": "fullscreen", "plein_écran": "fullscreen", "plein_ecran": "fullscreen", "pleinecran": "fullscreen",
    "float": "float", "flottant": "float", "flottante": "float", "toggle_float": "float",
    "center": "center", "centrer": "center", "centre": "center",
    "close": "close", "ferme": "close", "fermer": "close", "close_window": "close",
    "move_to_workspace": "move_to_workspace", "déplace_bureau": "move_to_workspace",
    "switch_workspace": "switch_workspace", "change_bureau": "switch_workspace",
    "list_windows": "list_windows", "liste_fenetres": "list_windows",
    "screenshot": "screenshot", "capture": "screenshot",
    "wait": "wait", "attends": "wait", "pause": "wait",
    "brightness_set": "brightness_set", "brightness_get": "brightness_get",
    "system_status": "system_status", "status": "system_status", "état": "system_status", "etat": "system_status",
    "workspace_overview": "workspace_overview", "vue_bureaux": "workspace_overview",
    "clipboard_status": "clipboard_status",
    "volume_get": "volume_get", "volume": "volume_get",
    "volume_set": "volume_set", "volume_mute": "volume_mute", "mute": "volume_mute",
}


@kit.action("computer_control")
def computer_control(parameters: dict, **kwargs) -> str:
    """
    Point d'entrée principal. Accepte soit une action explicite, soit une
    description en langage naturel.
    parameters :
      action      : nom canonique de l'action
      description : commande en français/anglais
      valeur(s) selon l'action (text, x, y, direction, amount, title,
      keys, seconds, workspace, value, ...)
    """
    params = parameters or {}
    action_raw = str(params.get("action", "") or "").strip().lower()
    description = str(params.get("description", "") or "").strip()

    # Interprétation locale puis IA d'une description
    if description and not action_raw:
        local = _parse_control_locally(description)
        if local:
            action_raw = local["action"]
            for k, v in local["params"].items():
                if k not in params or params[k] is None:
                    params[k] = v
        else:
            ai_detected = _detect_action_via_ai(description)
            if ai_detected and ai_detected.get("action"):
                action_raw = ai_detected["action"]
                for k, v in ai_detected.get("params", {}).items():
                    if k not in params or params[k] is None:
                        params[k] = v
            else:
                return "Je n'ai pas compris cette commande de contrôle. Pouvez-vous reformuler ?"
    if not action_raw:
        return "Aucune action demandée."

    action = _CANONICAL_MAP.get(action_raw, action_raw)
    print(f"[ComputerControl] ▶ {action} {params}")
    try:
        if action == "type":
            text = _terminal_command_from_voice_text(params.get("text", ""))
            if not text:
                return "Aucun texte à taper."
            target_win = params.get("window") or params.get("title")
            if target_win:
                focused = _focus_window(str(target_win))
                if not (focused.startswith("Fenêtre") and "focalisée" in focused):
                    return f"Saisie annulée : {focused}"
                time.sleep(0.15)
            res = _type_text(text)
            if not res.startswith("Texte tapé"):
                return res
            press_enter_val = params.get("press_enter")
            if press_enter_val is None:
                press_enter_val = params.get("enter")
            submit = press_enter_val is True or str(press_enter_val).lower() in ("true", "1", "yes")
            _remember_typed(text, submitted=False)
            if submit:
                pressed = _press_key("enter")
                if not pressed.startswith("Touche pressée"):
                    return f"Commande non validée : {pressed}. Le texte a été saisi, sans confirmation d'Entrée."
                _last_typed["submitted"] = True
                res = f"{res} (validé par Entrée)"
            return res
        elif action == "paste":
            content = _clipboard_current_content()
            if not content:
                return "Le presse-papiers est vide. Utilisez 'copie' d'abord ou 'tape <texte>'."
            return _clipboard_paste()
        elif action == "copy":
            _hotkey("ctrl", "c")
            return "Contenu copié."
        elif action == "click":
            if params.get("description"):
                desc = params["description"]
                coords = _screen_find(desc)
                if coords:
                    _click(x=coords[0], y=coords[1])
                    return f"J'ai cliqué sur «{desc}» aux coordonnées {coords}."
                return f"Je n'ai pas trouvé «{desc}» à l'écran."
            x = params.get("x")
            y = params.get("y")
            button = params.get("button", "left")
            clicks = int(params.get("clicks", 1))
            return _click(x=x, y=y, button=button, clicks=clicks)
        elif action == "scroll":
            return _scroll(params.get("direction", "down"),
                           int(params.get("amount", 3)))
        elif action == "press":
            return _press_key(params.get("keys", ""))
        elif action == "hotkey":
            keys = params.get("keys", "") or params.get("key", "")
            if not keys:
                return "Aucune combinaison de touches fournie."
            parts = [k.strip() for k in re.split(r"[+\s]+", keys) if k.strip()]
            return _hotkey(*parts) if parts else "Combinaison de touches invalide."
        elif action == "double_click":
            return _click(x=params.get("x"), y=params.get("y"),
                          button=params.get("button", "left"), clicks=2)
        elif action == "right_click":
            return _click(x=params.get("x"), y=params.get("y"),
                          button="right", clicks=1)
        elif action == "move":
            x, y = params.get("x"), params.get("y")
            if x is None or y is None:
                return "Coordonnées x/y requises pour déplacer la souris."
            return _move(int(x), int(y))
        elif action == "clear_field":
            return _erase("all")
        elif action == "erase":
            return _erase(str(params.get("scope") or "last"), params.get("count"))
        elif action == "smart_type":
            text = params.get("text", "")
            if not text:
                return "Aucun texte à taper."
            if params.get("clear_first", True):
                _clear_line(_is_terminal(_active_window_info()["class"]))
            res = _type_text(text)
            if res.startswith("Texte tapé"):
                _remember_typed(text, submitted=False)
            return res
        elif action == "screen_find":
            desc = params.get("description", "")
            if not desc:
                return "Aucune description fournie pour la recherche à l'écran."
            coords = _screen_find(desc)
            return (f"Trouvé «{desc}» à {coords}." if coords
                    else f"Je n'ai pas trouvé «{desc}» à l'écran.")
        elif action == "screen_click":
            desc = params.get("description", "")
            if not desc:
                return "Aucune description fournie."
            coords = _screen_find(desc)
            if coords:
                _click(x=coords[0], y=coords[1])
                return f"J'ai cliqué sur «{desc}» aux coordonnées {coords}."
            return f"Je n'ai pas trouvé «{desc}» à l'écran."
        elif action == "move_to_workspace":
            ws = params.get("workspace", params.get("value"))
            if ws is None:
                return "Numéro de bureau manquant."
            return _move_to_workspace(params.get("title", ""), ws)
        elif action == "switch_workspace":
            ws = params.get("workspace", params.get("value"))
            if ws is None:
                return "Numéro de bureau manquant."
            return _switch_workspace(ws)
        elif action == "list_windows":
            return _list_windows()
        elif action == "focus":
            title = params.get("title", "") or params.get("window", "")
            if not title:
                return "Aucun titre de fenêtre fourni."
            return _focus_window(title)
        elif action == "fullscreen":
            return _toggle_fullscreen()
        elif action == "float":
            return _toggle_float()
        elif action == "center":
            return _center_window()
        elif action == "close":
            target = params.get("window") or params.get("title", "")
            return _close_window(str(target) if target else "")
        elif action == "screenshot":
            path = take_screenshot(params.get("path"))
            return f"Capture d'écran sauvegardée : {path}"
        elif action == "wait":
            secs = min(float(params.get("seconds", 1)), 30)
            time.sleep(secs)
            return f"Pause de {secs} seconde(s)."
        elif action == "brightness_set":
            return _brightness_set(int(params.get("value", 50)))
        elif action == "brightness_get":
            return _brightness_get()
        elif action == "system_status":
            return _system_status()
        elif action == "workspace_overview":
            return _workspace_overview()
        elif action == "clipboard_status":
            return _clipboard_status()
        elif action == "volume_get":
            return _volume_get()
        elif action == "volume_set":
            return _volume_set(params.get("value", params.get("volume", 50)))
        elif action == "volume_mute":
            return _volume_mute(params.get("mode", "toggle"))
        elif action == "random_data":
            return _random_data(params.get("type", "name"))
        elif action == "user_data":
            return _user_data(params.get("field", "name"))
        else:
            return f"Action inconnue : '{action_raw}'."
    except Exception as e:
        print(f"[ComputerControl] ❌ {e}")
        return f"Échec de l'action '{action_raw}' : {e}"


# ════════════════════════════════════════════════════════════════════════════
# Test direct
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        print(computer_control({"description": " ".join(sys.argv[1:])}))
    else:
        print("Usage: python computer_control.py <commande naturelle>")
