"""
computer_settings.py — Réglages système & raccourcis, version réparée et
renforcée pour Arch Linux / Hyprland (Wayland), avec replis X11/macOS/Windows.

Corrections par rapport à l'ancienne version :
    - `_detect_action` utilisait la variable `client` qui n'existait pas
      (NameError systématique du fallback IA) ;
    - `_get_base_dir` utilisait `Path(file)` au lieu de `Path(__file__)` ;
    - `close_app` local faisait un matching substring sur toute la ligne de
      commande et un SIGTERM sans vérification : réécrit avec correspondance
      par mots entiers, fermeture de fenêtre Hyprland vérifiée
      (closewindow → killwindow), pgrep borné et SIGTERM→SIGKILL contrôlé ;
    - tous les raccourcis passaient par pyautogui, qui ne fonctionne PAS
      sous Wayland/Hyprland : ils sont désormais routés via wtype
      (-M mod -k touche -m mod) pour le clavier et hyprctl dispatch pour
      les fenêtres (killactive, fullscreen, cyclenext, dpms,
      movetoworkspace special…) ;
    - sleep_display utilisait xset (X11) : remplacé par `hyprctl dispatch
      dpms off` sous Wayland ;
    - capture d'écran : grim sous Wayland (flameshot/gnome-screenshot en repli) ;
    - les avertissements (volume 0/100, luminosité 0) bloquaient l'exécution
      sans aucun moyen de confirmer : le paramètre `confirmed` permet
      désormais de valider explicitement ;
    - dark_mode était codé en dur (`if True`) : vrai toggle gsettings ;
    - volume_up/down & brightness_up/down acceptent un delta personnalisé ;
    - ajout : suspend, volume_get, brightness_get, task manager terminal+btop.
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
from typing import Callable, Dict, List, Optional

from core import action_kit as kit
from core.action_kit import memo, run, spawn
from core import human_confirmation
from core.undo_stack import push as push_undo
from core.live_model_policy import FAST_MODEL

# Fermeture intelligente déléguée (désambiguïsation multi-instances).
try:
    from actions.close_app import close_app as _smart_close_app
    _HAS_SMART_CLOSE = True
except ImportError:
    _HAS_SMART_CLOSE = False


class _LazyPyAutoGUI:
    _module = None

    def __getattr__(self, name):
        if self._module is None:
            self._module = importlib.import_module("pyautogui")
            self._module.FAILSAFE = True
            self._module.PAUSE = 0.05
        return getattr(self._module, name)


pyautogui = _LazyPyAutoGUI()
_PYAUTOGUI = importlib.util.find_spec("pyautogui") is not None

try:
    import pyperclip
    _PYPERCLIP = True
except ImportError:
    _PYPERCLIP = False

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"
_WAYLAND = bool(os.environ.get("WAYLAND_DISPLAY"))
_X11 = bool(os.environ.get("DISPLAY") and not _WAYLAND)

if _OS == "Windows":
    _WIN_HIDE: dict = {"creationflags": subprocess.CREATE_NO_WINDOW}
else:
    _WIN_HIDE: dict = {}


# ════════════════════════════════════════════════════════════════════════════
# Utilitaires
# ════════════════════════════════════════════════════════════════════════════

def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def _get_api_key() -> str:
    try:
        path = _get_base_dir() / "config" / "api_keys.json"
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f).get("gemini_api_key", "")
    except Exception:
        return ""


def _run_ok(argv: List[str], timeout: float = 5.0, retries: int = 0) -> bool:
    """Vrai si la commande a réussi. Jamais d'exception, jamais d'attente infinie."""
    return run(argv, timeout=timeout, retries=retries, env=_hypr_env()).ok


@memo(ttl=300.0)
def _get_brightness_cmd() -> Optional[str]:
    # `shutil.which` touche le disque : inutile de le refaire à chaque réglage.
    if kit.which("brightnessctl"):
        return "brightnessctl"
    if kit.which("light"):
        return "light"
    return None


# ════════════════════════════════════════════════════════════════════════════
# Environnement Hyprland restauré (indispensable en service systemd)
# ════════════════════════════════════════════════════════════════════════════

@memo(ttl=60.0)
def _hypr_env() -> dict:
    """Environnement Hyprland reconstitué (utile en service systemd).

    Mémorisé une minute : la signature d'instance ne change qu'au redémarrage
    du compositeur, et la découverte listait un répertoire à chaque raccourci.
    """
    env = {**os.environ}
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
    """Lecture Hyprland partagée avec le reste des actions.

    Le cache du socle fusionne les rafales : plusieurs réglages enchaînés dans
    un même tour de parole ne lancent plus qu'un seul `hyprctl`.
    """
    return kit.hypr_json(*args, default=None)


def _hypr_dispatch_ok(dispatcher: str, arg: str = "") -> bool:
    """Envoie un dispatch et vérifie qu'Hyprland l'a réellement accepté.

    Hyprland répond « ok » avec un code de sortie nul même pour un dispatcher
    inconnu : le texte de sortie fait donc partie du test.
    """
    res = kit.hypr("dispatch", dispatcher, *( [arg] if arg else [] ))
    if not res.ok:
        return False
    out = res.out.lower()
    return not any(bad in out for bad in ("error", "unknown", "invalid"))


# ════════════════════════════════════════════════════════════════════════════
# Clavier : wtype sous Wayland, pyautogui en repli X11
# ════════════════════════════════════════════════════════════════════════════

_WTYPE_MODS = {"ctrl": "ctrl", "control": "ctrl", "shift": "shift",
               "alt": "alt", "meta": "alt", "super": "super",
               "win": "super", "logo": "super", "hyper": "hyper"}


def _hotkey(*keys: str) -> bool:
    """Combinaison de touches. Sous Wayland, wtype exige la séquence
    -M mod … -k touche -m mod (pas « ctrl+v » en un bloc)."""
    keys = [k.strip().lower() for k in keys if k and k.strip()]
    if not keys:
        return False
    if _WAYLAND and kit.which("wtype"):
        mods = [k for k in keys if k in _WTYPE_MODS]
        nonmods = [k for k in keys if k not in _WTYPE_MODS]
        cmd = ["wtype"]
        for m in mods:
            cmd += ["-M", _WTYPE_MODS[m]]
        for k in nonmods or ["space"]:
            cmd += ["-k", k]
        for m in reversed(mods):
            cmd += ["-m", _WTYPE_MODS[m]]
        return run(cmd, timeout=2, env=_hypr_env()).ok
    if _PYAUTOGUI:
        try:
            pyautogui.hotkey(*keys)
            return True
        except Exception:
            return False
    return False


def _press(key: str) -> bool:
    key = (key or "").strip()
    if not key:
        return False
    if "+" in key:
        return _hotkey(*[k for k in key.replace("-", "+").split("+") if k.strip()])
    if _WAYLAND and kit.which("wtype"):
        return run(["wtype", "-k", key], timeout=1.5, env=_hypr_env()).ok
    if _PYAUTOGUI:
        try:
            pyautogui.press(key)
            return True
        except Exception:
            return False
    return False


def _scroll(amount: int) -> bool:
    if _WAYLAND and kit.which("ydotool"):
        # ydotool dépend d'un démon : un échec ponctuel se retente une fois.
        if run(["ydotool", "scroll", "--", "0", str(amount)],
               timeout=2, retries=1).ok:
            return True
    if _PYAUTOGUI:
        try:
            pyautogui.scroll(amount)
            return True
        except Exception:
            return False
    return False


# ════════════════════════════════════════════════════════════════════════════
# État actuel (volume & luminosité)
# ════════════════════════════════════════════════════════════════════════════

@memo(ttl=300.0)
def _linux_volume_cmd() -> Optional[str]:
    if kit.which("wpctl"):
        return "wpctl"
    if kit.which("pactl"):
        return "pactl"
    return None


def get_current_volume() -> Optional[int]:
    if _OS == "Windows":
        try:
            from ctypes import cast, POINTER
            from comtypes import CLSCTX_ALL
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
            devices = AudioUtilities.GetSpeakers()
            interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            volume = cast(interface, POINTER(IAudioEndpointVolume))
            return round(volume.GetMasterVolumeLevelScalar() * 100)
        except Exception:
            return None
    elif _OS == "Darwin":
        try:
            result = subprocess.run(["osascript", "-e",
                                     "output volume of (get volume settings)"],
                                    capture_output=True, text=True, timeout=3)
            return int(result.stdout.strip()) if result.returncode == 0 else None
        except Exception:
            return None
    backend = _linux_volume_cmd()
    # Le volume est relu avant chaque hausse ou baisse, et l'interface le
    # réaffiche : une seconde de cache supprime la moitié des processus sans
    # jamais afficher une valeur périmée à l'oreille.
    if backend == "wpctl":
        res = run(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"],
                  timeout=3, cache_ttl=1.0)
        match = re.search(r"Volume:\s*([0-9.]+)", res.out) if res.ok else None
        if match:
            return round(float(match.group(1)) * 100)
    elif backend == "pactl":
        res = run(["pactl", "get-sink-volume", "@DEFAULT_SINK@"],
                  timeout=3, cache_ttl=1.0)
        match = re.search(r"(\d+)%", res.out) if res.ok else None
        if match:
            return int(match.group(1))
    return None


def get_current_brightness() -> Optional[int]:
    if _OS == "Windows":
        try:
            res = subprocess.run(
                ["powershell", "-Command",
                 "(Get-WmiObject -Namespace root/wmi -Class WmiMonitorBrightness).CurrentBrightness"],
                capture_output=True, text=True, timeout=5, **_WIN_HIDE)
            return int(res.stdout.strip()) if res.stdout.strip() else None
        except Exception:
            return None
    elif _OS == "Darwin":
        return None
    cmd = _get_brightness_cmd()
    if cmd == "brightnessctl":
        # `brightnessctl -m` donne courant et maximum en une seule ligne :
        # un processus au lieu de deux, et la valeur reste bonne une seconde.
        res = run(["brightnessctl", "-m", "get"], timeout=3, cache_ttl=1.0)
        if res.ok:
            fields = res.out.strip().split(",")
            try:
                # device,classe,valeur,pourcentage,maximum
                if len(fields) >= 5:
                    cur, max_val = int(fields[2]), int(fields[4])
                    return round((cur / max_val) * 100) if max_val > 0 else None
            except (ValueError, IndexError):
                pass
        cur_res = run(["brightnessctl", "get"], timeout=3, cache_ttl=1.0)
        max_res = run(["brightnessctl", "max"], timeout=3, cache_ttl=30.0)
        if cur_res.ok and max_res.ok:
            try:
                cur, max_val = int(cur_res.out.strip()), int(max_res.out.strip())
                return round((cur / max_val) * 100) if max_val > 0 else None
            except ValueError:
                return None
    elif cmd == "light":
        res = run(["light", "-G"], timeout=3, cache_ttl=1.0)
        if res.ok:
            try:
                return round(float(res.out.strip()))
            except ValueError:
                return None
    return None


# ════════════════════════════════════════════════════════════════════════════
# Contrôle volume / luminosité
# ════════════════════════════════════════════════════════════════════════════

def volume_up(amount: int = 10):
    amount = max(1, min(50, int(amount or 10)))
    if _OS == "Windows":
        if _PYAUTOGUI:
            for _ in range(amount // 2):
                pyautogui.press("volumeup")
    elif _OS == "Darwin":
        subprocess.run(["osascript", "-e",
                        f"set volume output volume ((output volume of (get volume settings)) + {amount})"],
                       capture_output=True, timeout=15)
    else:
        backend = _linux_volume_cmd()
        if backend == "wpctl":
            _run_ok(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{amount}%+"])
        elif backend == "pactl":
            _run_ok(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"+{amount}%"])
        else:
            raise RuntimeError("Aucun backend volume (installez wireplumber ou pulseaudio-utils).")


def volume_down(amount: int = 10):
    amount = max(1, min(50, int(amount or 10)))
    if _OS == "Windows":
        if _PYAUTOGUI:
            for _ in range(amount // 2):
                pyautogui.press("volumedown")
    elif _OS == "Darwin":
        subprocess.run(["osascript", "-e",
                        f"set volume output volume ((output volume of (get volume settings)) - {amount})"],
                       capture_output=True, timeout=15)
    else:
        backend = _linux_volume_cmd()
        if backend == "wpctl":
            _run_ok(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{amount}%-"])
        elif backend == "pactl":
            _run_ok(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"-{amount}%"])
        else:
            raise RuntimeError("Aucun backend volume (installez wireplumber ou pulseaudio-utils).")


def volume_mute():
    if _OS == "Windows":
        if _PYAUTOGUI:
            pyautogui.press("volumemute")
    elif _OS == "Darwin":
        subprocess.run(["osascript", "-e",
                        "set volume output muted not (output muted of (get volume settings))"],
                       capture_output=True, timeout=15)
    else:
        backend = _linux_volume_cmd()
        if backend == "wpctl":
            _run_ok(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "toggle"])
        elif backend == "pactl":
            _run_ok(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "toggle"])
        else:
            raise RuntimeError("Aucun backend volume.")


def volume_unmute():
    if _OS == "Darwin":
        subprocess.run(["osascript", "-e", "set volume output muted false"],
                       capture_output=True, timeout=15)
    elif _OS == "Linux":
        backend = _linux_volume_cmd()
        if backend == "wpctl":
            _run_ok(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "off"])
        elif backend == "pactl":
            _run_ok(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "0"])


def volume_set(value: int):
    value = max(0, min(100, int(value)))
    if _OS == "Windows":
        try:
            import math
            from ctypes import cast, POINTER
            from comtypes import CLSCTX_ALL
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
            devices = AudioUtilities.GetSpeakers()
            interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            vol = cast(interface, POINTER(IAudioEndpointVolume))
            vol_db = -65.25 if value == 0 else max(-65.25, 20 * math.log10(value / 100))
            vol.SetMasterVolumeLevel(vol_db, None)
            return
        except Exception as e:
            print(f"[Settings] pycaw failed, using keypress fallback: {e}")
            if _PYAUTOGUI:
                pyautogui.press("volumemute")
                pyautogui.press("volumemute")
    elif _OS == "Darwin":
        subprocess.run(["osascript", "-e", f"set volume output volume {value}"],
                       capture_output=True, timeout=15)
        return
    else:
        backend = _linux_volume_cmd()
        if backend == "wpctl":
            _run_ok(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{value}%"])
        elif backend == "pactl":
            _run_ok(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{value}%"])
        else:
            raise RuntimeError("Aucun backend volume.")


def brightness_up(amount: int = 10):
    amount = max(1, min(50, int(amount or 10)))
    if _OS != "Linux":
        print(f"[Settings] brightness_up non implémenté sur {_OS}")
        return
    cmd = _get_brightness_cmd()
    if cmd == "brightnessctl":
        _run_ok(["brightnessctl", "set", f"{amount}%+"])
    elif cmd == "light":
        _run_ok(["light", "-A", str(amount)])
    else:
        raise RuntimeError("Aucun outil de luminosité trouvé (installez brightnessctl).")


def brightness_down(amount: int = 10):
    amount = max(1, min(50, int(amount or 10)))
    if _OS != "Linux":
        print(f"[Settings] brightness_down non implémenté sur {_OS}")
        return
    cmd = _get_brightness_cmd()
    if cmd == "brightnessctl":
        _run_ok(["brightnessctl", "set", f"{amount}%-"])
    elif cmd == "light":
        _run_ok(["light", "-U", str(amount)])
    else:
        raise RuntimeError("Aucun outil de luminosité trouvé (installez brightnessctl).")


def brightness_set(value: int):
    value = max(0, min(100, int(value)))
    print(f"[Settings] brightness_set({value}%)")
    if _OS == "Windows":
        try:
            subprocess.run(
                ["powershell", "-Command",
                 f"(Get-WmiObject -Namespace root/wmi -Class WmiMonitorBrightnessMethods).WmiSetBrightness(1,{value})"],
                capture_output=True, timeout=5, **_WIN_HIDE)
        except Exception:
            print("[Settings] Windows brightness set failed")
    elif _OS == "Darwin":
        print("[Settings] macOS brightness set non implémenté")
    else:
        cmd = _get_brightness_cmd()
        if cmd == "brightnessctl":
            _run_ok(["brightnessctl", "set", f"{value}%"])
        elif cmd == "light":
            _run_ok(["light", "-S", str(value)])
        else:
            raise RuntimeError("Aucun outil de luminosité trouvé (installez brightnessctl).")


# ════════════════════════════════════════════════════════════════════════════
# Fenêtres : hyprctl dispatch sous Wayland, raccourcis en repli
# ════════════════════════════════════════════════════════════════════════════

def close_window():
    if _WAYLAND and _hypr_dispatch_ok("killactive"):
        return
    if _OS == "Darwin":
        _hotkey("command", "w")
    else:
        _hotkey("alt", "f4")


def close_app(app_name: str = ""):
    """Ferme une application par nom (fenêtres Hyprland d'abord, processus
    sans fenêtre ensuite). Sans nom : ferme la fenêtre active."""
    app_name = (app_name or "").strip()
    if app_name and _OS == "Linux":
        needle = app_name.lower()
        # 1. Fenêtres Hyprland, fermées une par une avec vérification.
        if _WAYLAND and shutil.which("hyprctl"):
            clients = _hyprctl_json("clients") or []
            closed = 0
            for c in clients:
                blob = " ".join(str(c.get(k) or "") for k in
                                ("class", "initialClass", "title", "initialTitle")).lower()
                if not (re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", blob)
                        or needle in blob):
                    continue
                addr = c.get("address")
                if not addr:
                    continue
                for disp in ("closewindow", "killwindow"):
                    _hypr_dispatch_ok(disp, f"address:{addr}")
                    time.sleep(0.15)
                    if not any(x.get("address") == addr
                               for x in (_hyprctl_json("clients") or [])):
                        closed += 1
                        break
            if closed:
                return
        # 2. Processus sans fenêtre : pgrep borné + SIGTERM→SIGKILL vérifié.
        for pid in _find_pids_by_name(app_name):
            _terminate_pid(pid)
        return
    # Pas de nom : fermer la fenêtre active.
    if _WAYLAND and _hypr_dispatch_ok("killactive"):
        return
    if _OS == "Darwin":
        _hotkey("command", "q")
    else:
        _hotkey("alt", "f4")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _terminate_pid(pid: int) -> bool:
    if pid <= 1 or pid == os.getpid():
        return False
    if not _pid_alive(pid):
        return True
    try:
        os.kill(pid, 15)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    for _ in range(8):
        time.sleep(0.1)
        if not _pid_alive(pid):
            return True
    try:
        os.kill(pid, 9)
    except Exception:
        return False
    time.sleep(0.15)
    return not _pid_alive(pid)


def _find_pids_by_name(name: str) -> List[int]:
    """pgrep -f borné (^|/)nom( |$) + repli /proc sur le basename.
    Jamais soi-même ni PID 1."""
    pids = set()
    my_pid = os.getpid()
    low = name.lower().strip()
    if shutil.which("pgrep"):
        pat = rf"(^|/){re.escape(low)}( |$)"
        try:
            r = subprocess.run(["pgrep", "-f", pat], capture_output=True,
                               text=True, timeout=3)
            for line in (r.stdout or "").splitlines():
                if line.strip().isdigit():
                    pids.add(int(line.strip()))
        except Exception:
            pass
    if not pids:
        proc_root = Path("/proc")
        if proc_root.is_dir():
            for pid_dir in proc_root.iterdir():
                if not pid_dir.is_dir() or not pid_dir.name.isdigit():
                    continue
                pid = int(pid_dir.name)
                if pid in (my_pid, 1):
                    continue
                try:
                    cmdline = (pid_dir / "cmdline").read_text(errors="replace")
                    args = [a for a in cmdline.split("\x00") if a.strip()]
                    if args and os.path.basename(args[0]).lower() == low:
                        pids.add(pid)
                except Exception:
                    continue
    return [p for p in pids if p not in (my_pid, 1)]


def full_screen():
    if _WAYLAND and _hypr_dispatch_ok("fullscreen"):
        return
    if _OS == "Darwin":
        _hotkey("command", "ctrl", "f")
    else:
        _press("f11")


def minimize_window():
    if _WAYLAND and _hypr_dispatch_ok("movetoworkspace", "special"):
        return  # Hyprland : envoyé vers le scratchpad
    if _OS == "Darwin":
        _hotkey("command", "m")
    else:
        _hotkey("win", "down")


def maximize_window():
    if _WAYLAND and _hypr_dispatch_ok("fullscreen", "1"):
        return
    if _OS == "Darwin":
        _hotkey("command", "option", "f")
    else:
        _hotkey("win", "up")


def snap_left():
    if _WAYLAND:
        print("[Settings] snap_left non supporté nativement sous Hyprland")
        return
    if _OS == "Darwin":
        _hotkey("command", "option", "left")
    else:
        _hotkey("win", "left")


def snap_right():
    if _WAYLAND:
        print("[Settings] snap_right non supporté nativement sous Hyprland")
        return
    if _OS == "Darwin":
        _hotkey("command", "option", "right")
    else:
        _hotkey("win", "right")


def switch_window():
    if _WAYLAND and _hypr_dispatch_ok("cyclenext"):
        return
    if _OS == "Darwin":
        _hotkey("command", "tab")
    else:
        _hotkey("alt", "tab")


def show_desktop():
    if _WAYLAND:
        print("[Settings] show_desktop non supporté nativement sous Hyprland")
        return
    if _OS == "Darwin":
        _hotkey("command", "f3")
    else:
        _hotkey("win", "d")


def open_task_manager():
    """Ouvre un moniteur système, en détaché.

    `Popen` héritait des flux d'ANO-GPT et n'était jamais attendu : la sortie
    de btop se mélangeait aux journaux et le processus restait zombie. `spawn`
    ouvre une session dédiée, flux vers /dev/null.
    """
    if _OS == "Windows":
        spawn("taskmgr")
    elif _OS == "Darwin":
        spawn(["open", "-a", "Activity Monitor"])
    else:
        if kit.which("gnome-system-monitor"):
            spawn(["gnome-system-monitor"], env=_hypr_env())
            return
        term = next((t for t in ("kitty", "alacritty", "foot", "gnome-terminal",
                                 "konsole", "xterm") if kit.which(t)), None)
        tool = next((t for t in ("btop", "htop", "top") if kit.which(t)), None)
        if term and tool:
            spawn([term, "-e", tool], env=_hypr_env())
        elif kit.which("xterm"):
            spawn(["xterm", "-e", "htop"], env=_hypr_env())


# ════════════════════════════════════════════════════════════════════════════
# Raccourcis navigateur / édition (routés via wtype sous Wayland)
# ════════════════════════════════════════════════════════════════════════════

def focus_search():
    _hotkey("command" if _OS == "Darwin" else "ctrl", "f")


def refresh_page():
    _hotkey("command" if _OS == "Darwin" else "ctrl", "r")


def close_tab():
    _hotkey("command" if _OS == "Darwin" else "ctrl", "w")


def new_tab():
    _hotkey("command" if _OS == "Darwin" else "ctrl", "t")


def next_tab():
    if _OS == "Darwin":
        _hotkey("command", "option", "right")
    else:
        _hotkey("ctrl", "tab")


def prev_tab():
    if _OS == "Darwin":
        _hotkey("command", "option", "left")
    else:
        _hotkey("ctrl", "shift", "tab")


def go_back():
    _hotkey("command" if _OS == "Darwin" else "alt", "left")


def go_forward():
    _hotkey("command" if _OS == "Darwin" else "alt", "right")


def zoom_in():
    _hotkey("command" if _OS == "Darwin" else "ctrl", "=")


def zoom_out():
    _hotkey("command" if _OS == "Darwin" else "ctrl", "-")


def zoom_reset():
    _hotkey("command" if _OS == "Darwin" else "ctrl", "0")


def find_on_page():
    focus_search()


def scroll_up(amount: int = 500):
    _scroll(abs(int(amount or 500)))


def scroll_down(amount: int = 500):
    _scroll(-abs(int(amount or 500)))


def scroll_top():
    if _OS == "Darwin":
        _hotkey("command", "up")
    else:
        _hotkey("ctrl", "home")


def scroll_bottom():
    if _OS == "Darwin":
        _hotkey("command", "down")
    else:
        _hotkey("ctrl", "end")


def page_up():
    _press("pageup")


def page_down():
    _press("pagedown")


def copy():
    _hotkey("command" if _OS == "Darwin" else "ctrl", "c")


def paste():
    _hotkey("command" if _OS == "Darwin" else "ctrl", "v")


def cut():
    _hotkey("command" if _OS == "Darwin" else "ctrl", "x")


def undo():
    _hotkey("command" if _OS == "Darwin" else "ctrl", "z")


def redo():
    if _OS == "Darwin":
        _hotkey("command", "shift", "z")
    else:
        _hotkey("ctrl", "y")


def select_all():
    _hotkey("command" if _OS == "Darwin" else "ctrl", "a")


def save_file():
    _hotkey("command" if _OS == "Darwin" else "ctrl", "s")


def press_enter():
    _press("enter")


def press_escape():
    _press("esc")


def press_key(key: str):
    _press(key)


def type_text(text: str, press_enter_after: bool = False):
    typed = False
    if _WAYLAND and shutil.which("wtype"):
        try:
            subprocess.run(["wtype", text], timeout=max(2, len(text) // 20 + 2),
                           env=_hypr_env())
            typed = True
        except Exception:
            pass
    if not typed and _PYPERCLIP and (_PYAUTOGUI or _WAYLAND):
        try:
            pyperclip.copy(text)
            _hotkey("command" if _OS == "Darwin" else "ctrl", "v")
            typed = True
        except Exception:
            pass
    if not typed and _PYAUTOGUI:
        pyautogui.write(text)
    if press_enter_after:
        press_enter()


# ════════════════════════════════════════════════════════════════════════════
# Système : capture, verrouillage, énergie, réglages
# ════════════════════════════════════════════════════════════════════════════

def take_screenshot() -> Optional[str]:
    if _OS == "Windows":
        spawn("snippingtool")
        return None
    if _OS == "Darwin":
        _hotkey("command", "shift", "4")
        return None
    # Linux / Wayland : grim d'abord, flameshot/gnome-screenshot en repli.
    if _WAYLAND and kit.which("grim"):
        out = Path.home() / "Pictures" / f"screenshot_{int(time.time())}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        # Ne pas annoncer un fichier que grim n'a pas écrit : l'ancien code
        # confirmait la capture même quand la commande échouait.
        if run(["grim", str(out)], timeout=8, env=_hypr_env()).ok and out.exists():
            return f"Capture enregistrée : {out}"
    if kit.which("flameshot"):
        spawn(["flameshot", "gui"], env=_hypr_env())
    elif kit.which("gnome-screenshot"):
        spawn(["gnome-screenshot", "-i"])
    return None


def lock_screen():
    if _OS == "Windows":
        run(["rundll32.exe", "user32.dll,LockWorkStation"], timeout=10)
    elif _OS == "Darwin":
        run(["pmset", "displaysleepnow"], timeout=10)
    else:
        # `loginctl` peut rester suspendu sur une demande polkit : sans borne,
        # le fil du pool était perdu pour le reste de la session.
        if not run(["loginctl", "lock-session"], timeout=8).ok:
            for locker in ("hyprlock", "swaylock", "waylock"):
                if kit.which(locker):
                    spawn([locker], env=_hypr_env())
                    break


def sleep_display():
    if _OS == "Windows":
        subprocess.run(
            ["powershell", "-Command",
             "(Add-Type -MemberDefinition '[DllImport(\"user32.dll\")]public static extern int SendMessage(int hWnd,int hMsg,int wParam,int lParam);' -Name a -Pas)::SendMessage(-1,0x0112,0xF170,2)"],
            **_WIN_HIDE, timeout=15)
    elif _OS == "Darwin":
        subprocess.run(["pmset", "displaysleepnow"], timeout=15)
    else:
        if _WAYLAND and kit.which("hyprctl"):
            _hypr_dispatch_ok("dpms", "off")
        elif kit.which("xset"):
            run(["xset", "dpms", "force", "off"], timeout=5)


def open_system_settings():
    if _OS == "Windows":
        spawn("ms-settings:")
    elif _OS == "Darwin":
        spawn(["open", "/System/Applications/System Settings.app"])
    else:
        for app in ("gnome-control-center", "systemsettings5", "systemsettings",
                    "xfce4-settings-manager"):
            if kit.which(app):
                spawn([app], env=_hypr_env())
                return


def open_file_explorer():
    if _OS == "Windows":
        spawn("explorer")
    elif _OS == "Darwin":
        spawn(["open", "/System/Volumes/Data"])
    else:
        # Un gestionnaire natif d'abord : `xdg-open` sur un dossier retombe
        # parfois sur un navigateur web quand aucune association n'existe.
        for fm in ("nautilus", "thunar", "dolphin", "nemo", "pcmanfm"):
            if kit.which(fm):
                spawn([fm, str(Path.home())], env=_hypr_env())
                return
        spawn(["xdg-open", str(Path.home())], env=_hypr_env())


def open_run():
    if _OS == "Windows":
        _hotkey("win", "r")
    elif _OS == "Darwin":
        _hotkey("command", "space")
    else:
        _hotkey("alt", "f2")


def dark_mode():
    if _OS == "Windows":
        spawn("ms-settings:colors")
    elif _OS == "Darwin":
        run(["osascript", "-e",
             'tell app "System Events" to tell appearance preferences to set dark mode to not dark mode'],
            timeout=8)
    else:
        if kit.which("gsettings"):
            cur = run(["gsettings", "get", "org.gnome.desktop.interface",
                       "color-scheme"], timeout=2).out.strip()
            new = "prefer-light" if "dark" in cur else "prefer-dark"
            run(["gsettings", "set", "org.gnome.desktop.interface",
                 "color-scheme", new], timeout=2)
            # GTK3 ignore color-scheme : le thème doit suivre, sinon la moitié
            # des applications reste dans l'ancien mode.
            run(["gsettings", "set", "org.gnome.desktop.interface",
                 "gtk-theme", "Adwaita-dark" if new == "prefer-dark" else "Adwaita"],
                timeout=2, quiet=True)


def toggle_wifi():
    if _OS == "Windows":
        _hotkey("win", "a")
    elif _OS == "Darwin":
        print("[Settings] toggle_wifi non supporté directement sur macOS")
    else:
        if kit.which("nmcli"):
            # nmcli attend le démon NetworkManager : borné, et réessayé une
            # fois car il refuse parfois pendant une transition d'état.
            run(["nmcli", "radio", "wifi", "toggle"], timeout=10, retries=1)


def pause_video():
    _press("space")


def reload_page_n(times: int = 1):
    for _ in range(max(1, int(times or 1))):
        refresh_page()
        time.sleep(0.5)


def suspend_computer():
    if _OS == "Windows":
        run(["rundll32.exe", "powrprof.dll,SetSuspendState", "0", "1", "0"], timeout=15)
    elif _OS == "Darwin":
        run(["pmset", "sleepnow"], timeout=15)
    else:
        run(["systemctl", "suspend"], timeout=15)


def restart_computer():
    if _OS == "Windows":
        run(["shutdown", "/r", "/t", "0"], timeout=15)
    elif _OS == "Darwin":
        run(["osascript", "-e", 'tell app "System Events" to restart'], timeout=15)
    else:
        run(["systemctl", "reboot"], timeout=15)


def shutdown_computer():
    if _OS == "Windows":
        run(["shutdown", "/s", "/t", "0"], timeout=15)
    elif _OS == "Darwin":
        run(["osascript", "-e", 'tell app "System Events" to shut down'], timeout=15)
    else:
        run(["systemctl", "poweroff"], timeout=15)


def volume_get() -> str:
    v = get_current_volume()
    return f"Volume actuel : {v}%." if v is not None else "Impossible de lire le volume."


def brightness_get() -> str:
    b = get_current_brightness()
    return f"Luminosité actuelle : {b}%." if b is not None else "Impossible de lire la luminosité."


# ════════════════════════════════════════════════════════════════════════════
# Parsing local (fr/en)
# ════════════════════════════════════════════════════════════════════════════

_SET_PATTERNS = {
    "volume_set": [
        r"(?:régler?|mets?|mettez|mettre|fixer?|définir?|ajuster?)\s+(?:le\s+)?(?:volume|son|audio)\s+(?:à|sur)\s+(\d+)\s*%?",
        r"(?:set|put|change)\s+(?:the\s+)?(?:volume|sound|audio)\s+(?:to|at)\s+(\d+)\s*%?",
        r"(?:volume|son|audio)\s+(?:à|sur)\s+(\d+)\s*%?",
    ],
    "brightness_set": [
        r"(?:régler?|mets?|mettez|mettre|fixer?|définir?|ajuster?)\s+(?:la\s+)?(?:luminosité|brillance|écran)\s+(?:à|sur)\s+(\d+)\s*%?",
        r"(?:set|put|change)\s+(?:the\s+)?(?:brightness|screen|display)\s+(?:to|at)\s+(\d+)\s*%?",
        r"(?:luminosité|brillance|écran|brightness)\s+(?:à|sur)\s+(\d+)\s*%?",
    ],
}

_DELTA_PATTERNS = {
    "volume_up": [
        r"(?:augmente|monte|hausse|boost)\s+(?:le\s+)?(?:volume|son|audio)\s+(?:de\s+)?(\d+)\s*%?",
        r"(?:increase|raise|turn\s+up)\s+(?:the\s+)?(?:volume|sound|audio)\s+(?:by\s+)?(\d+)\s*%?",
        r"(?:plus\s+fort)\s+(?:de\s+)?(\d+)\s*%?",
    ],
    "volume_down": [
        r"(?:diminue|baisse|rédui[st]|descend)\s+(?:le\s+)?(?:volume|son|audio)\s+(?:de\s+)?(\d+)\s*%?",
        r"(?:decrease|lower|turn\s+down)\s+(?:the\s+)?(?:volume|sound|audio)\s+(?:by\s+)?(\d+)\s*%?",
        r"(?:moins\s+fort)\s+(?:de\s+)?(\d+)\s*%?",
    ],
    "brightness_up": [
        r"(?:augmente|monte|hausse|boost)\s+(?:la\s+)?(?:luminosité|brillance|écran)\s+(?:de\s+)?(\d+)\s*%?",
        r"(?:increase|brighten)\s+(?:the\s+)?(?:brightness|screen)\s+(?:by\s+)?(\d+)\s*%?",
    ],
    "brightness_down": [
        r"(?:diminue|baisse|rédui[st]|descend)\s+(?:la\s+)?(?:luminosité|brillance|écran)\s+(?:de\s+)?(\d+)\s*%?",
        r"(?:decrease|dim|lower)\s+(?:the\s+)?(?:brightness|screen)\s+(?:by\s+)?(\d+)\s*%?",
    ],
}

_SPECIAL_PATTERNS = {
    "volume_max": [
        r"(?:volume|son|audio)\s+(?:à\s+fond|au\s+max|maxi|maximum|100\s*%?)",
        r"(?:set|put)\s+(?:the\s+)?(?:volume|sound|audio)\s+(?:to\s+)?(?:max|full)",
    ],
    "volume_min": [
        r"(?:volume|son|audio)\s+(?:au\s+min|mini|minimum|0\s*%?)",
        r"(?:set|put)\s+(?:the\s+)?(?:volume|sound|audio)\s+(?:to\s+)?(?:min|mute|0)",
    ],
    "mute": [
        r"(?:coupe|muet|mute|silencieux)\s+(?:le\s+)?(?:son|volume|audio)",
        r"(?:mute|silence)\s+(?:the\s+)?(?:sound|audio|volume)",
    ],
    "unmute": [
        r"(?:remets?\s+le\s+son|réactive?\s+le\s+son|annule\s+mute|unmute)",
        r"(?:unmute|restore\s+sound)",
    ],
}


def _parse_command_locally(text: str) -> Optional[dict]:
    """Parse local (fr/en) : renvoie {action, value, confidence, warn} ou None."""
    text_lower = re.sub(r"\s+", " ", text.lower().strip())

    for action, patterns in _SPECIAL_PATTERNS.items():
        for pat in patterns:
            if re.search(pat, text_lower):
                if action == "volume_max":
                    return {"action": "volume_set", "value": 100, "confidence": 1.0}
                elif action == "volume_min":
                    return {"action": "volume_set", "value": 0, "confidence": 1.0}
                elif action == "mute":
                    return {"action": "mute", "value": None, "confidence": 1.0}
                elif action == "unmute":
                    return {"action": "unmute", "value": None, "confidence": 1.0}

    for category, patterns in _SET_PATTERNS.items():
        for pat in patterns:
            match = re.search(pat, text_lower)
            if match:
                val = max(0, min(100, int(match.group(1))))
                return {"action": category, "value": val, "confidence": 0.95}

    for category, patterns in _DELTA_PATTERNS.items():
        for pat in patterns:
            match = re.search(pat, text_lower)
            if match:
                return {"action": category, "value": int(match.group(1)),
                        "confidence": 0.95}

    if re.search(r"(augmente|monte|hausse|plus fort)\s+(?:le |la )?(?:volume|son|audio)", text_lower) or \
       re.search(r"(increase|raise|turn up|up)\s+(?:the )?(?:volume|sound|audio)", text_lower):
        return {"action": "volume_up", "value": 10, "confidence": 0.9}
    if re.search(r"(diminue|baisse|réduis|moins fort)\s+(?:le |la )?(?:volume|son|audio)", text_lower) or \
       re.search(r"(decrease|lower|turn down|down)\s+(?:the )?(?:volume|sound|audio)", text_lower):
        return {"action": "volume_down", "value": 10, "confidence": 0.9}
    if re.search(r"(augmente|monte|hausse)\s+(?:la )?(?:luminosité|brillance|écran)", text_lower) or \
       re.search(r"(increase|brighten|up)\s+(?:the )?(?:brightness|screen)", text_lower):
        return {"action": "brightness_up", "value": 10, "confidence": 0.9}
    if re.search(r"(diminue|baisse|réduis)\s+(?:la )?(?:luminosité|brillance|écran)", text_lower) or \
       re.search(r"(decrease|dim|lower|down)\s+(?:the )?(?:brightness|screen)", text_lower):
        return {"action": "brightness_down", "value": 10, "confidence": 0.9}
    return None


def _validate_command(parsed: dict, current_val: Optional[int],
                      target_type: str = "volume") -> dict:
    action = parsed["action"]
    value = parsed.get("value")
    warn_msg = None
    if action in ("volume_set", "brightness_set") and value is not None:
        if value == 0:
            warn_msg = ("Le volume sera coupé (0%)." if target_type == "volume"
                        else "La luminosité sera éteinte (écran noir).")
        elif value == 100:
            warn_msg = ("Le volume sera réglé au maximum (100%)."
                        if target_type == "volume" else "Luminosité maximale.")
    if action in ("volume_up", "volume_down", "brightness_up", "brightness_down") \
            and current_val is not None:
        delta = value or 10
        target = current_val + delta if "_up" in action else current_val - delta
        clamped = max(0, min(100, target))
        if clamped != target:
            warn_msg = f"La valeur sera plafonnée à {clamped}%."
    return {**parsed, "warn": warn_msg}


# ════════════════════════════════════════════════════════════════════════════
# Détection d'intention par IA (fallback)
# ════════════════════════════════════════════════════════════════════════════

def _detect_action(description: str) -> dict:
    try:
        from google import genai as _genai
        _client = _genai.Client(api_key=_get_api_key())
    except Exception as e:
        return {"action": None, "value": None, "error": f"IA indisponible: {e}"}

    available = ", ".join(sorted(ACTION_MAP.keys()) +
                          ["volume_set", "brightness_set", "type_text",
                           "press_key", "reload_n"])
    prompt = f"""You are an ultra-precise intent detector for a system assistant.
The user speaks French or English. Command: "{description}"
Available actions: {available}
Return ONLY a JSON object: {{"action": "...", "value": ...}}
Rules:
For volume_set / brightness_set: value integer 0-100.
For volume_up/down, brightness_up/down: value is the increment as integer (default 10 if not specified).
For mute/unmute: value null.
For type_text: value is the string to type.
For press_key: value is key name.
Prefer set over delta when a target percentage is explicitly given (e.g. "à 20%" → volume_set 20).
If no clear match, return the closest action and best guess value.
ONLY JSON, no markdown, no explanation."""
    try:
        resp = _client.models.generate_content(model=FAST_MODEL,
                                               contents=prompt)
        text = re.sub(r"```(?:json)?", "", resp.text).strip().rstrip("`").strip()
        return json.loads(text)
    except Exception as e:
        return {"action": description.lower().replace(" ", "_"),
                "value": None, "error": str(e)}


# ════════════════════════════════════════════════════════════════════════════
# Dispatch et point d'entrée
# ════════════════════════════════════════════════════════════════════════════

ACTION_MAP: Dict[str, Callable] = {
    "volume_up": volume_up,
    "volume_down": volume_down,
    "mute": volume_mute,
    "unmute": volume_unmute,
    "toggle_mute": volume_mute,
    "volume_get": volume_get,
    "brightness_up": brightness_up,
    "brightness_down": brightness_down,
    "brightness_set": brightness_set,
    "brightness_get": brightness_get,
    "sleep_display": sleep_display,
    "screen_off": sleep_display,
    "suspend": suspend_computer,
    "pause_video": pause_video,
    "play_pause": pause_video,
    "close_app": close_app,
    "close_window": close_window,
    "full_screen": full_screen,
    "fullscreen": full_screen,
    "minimize": minimize_window,
    "maximize": maximize_window,
    "snap_left": snap_left,
    "snap_right": snap_right,
    "switch_window": switch_window,
    "show_desktop": show_desktop,
    "task_manager": open_task_manager,
    "focus_search": focus_search,
    "refresh_page": refresh_page,
    "reload": refresh_page,
    "close_tab": close_tab,
    "new_tab": new_tab,
    "next_tab": next_tab,
    "prev_tab": prev_tab,
    "go_back": go_back,
    "go_forward": go_forward,
    "zoom_in": zoom_in,
    "zoom_out": zoom_out,
    "zoom_reset": zoom_reset,
    "find_on_page": find_on_page,
    "scroll_up": scroll_up,
    "scroll_down": scroll_down,
    "scroll_top": scroll_top,
    "scroll_bottom": scroll_bottom,
    "page_up": page_up,
    "page_down": page_down,
    "copy": copy,
    "paste": paste,
    "cut": cut,
    "undo": undo,
    "redo": redo,
    "select_all": select_all,
    "save": save_file,
    "enter": press_enter,
    "escape": press_escape,
    "screenshot": take_screenshot,
    "lock_screen": lock_screen,
    "open_settings": open_system_settings,
    "file_explorer": open_file_explorer,
    "open_run": open_run,
    "dark_mode": dark_mode,
    "toggle_wifi": toggle_wifi,
    "restart": restart_computer,
    "shutdown": shutdown_computer,
}

_ACTION_ALIASES: Dict[str, str] = {
    "volume": "volume_set", "set_volume": "volume_set", "sound": "volume_set",
    "sound_set": "volume_set", "set_sound": "volume_set", "audio": "volume_set",
    "increase_volume": "volume_up", "raise_volume": "volume_up", "louder": "volume_up",
    "decrease_volume": "volume_down", "lower_volume": "volume_down", "quieter": "volume_down",
    "silence": "mute", "toggle_sound": "toggle_mute",
    "get_volume": "volume_get", "current_volume": "volume_get",
    "get_brightness": "brightness_get", "current_brightness": "brightness_get",
    "type_text": "type_text", "write_text": "type_text",
    "screenshot_screen": "screenshot", "take_screenshot": "screenshot",
    "close_current_window": "close_window",
    "quit_app": "close_app", "kill_app": "close_app",
    "wifi": "toggle_wifi", "toggle_wi_fi": "toggle_wifi",
    "power_off": "shutdown", "turn_off": "shutdown", "reboot": "restart",
    "dark_theme": "dark_mode", "night_mode": "dark_mode",
    "luminosity": "brightness_set", "brightness": "brightness_set",
    "screen_brightness": "brightness_set",
    "dim": "brightness_down", "dimmer": "brightness_down",
    "brighten": "brightness_up", "brighter": "brightness_up",
    "veille": "suspend", "suspendre": "suspend",
}

_DANGEROUS_ACTIONS = {"restart", "shutdown", "suspend", "toggle_wifi"}
_TRUTHY = {"yes", "true", "1", "confirm", "oui", "confirme", "ok"}


@kit.action("computer_settings")
def computer_settings(parameters: dict = None, response=None, player=None,
                      session_memory=None) -> str:
    """
    Point d'entrée principal — réglages système & raccourcis.
    Paramètres : action, description, value, confirmed (pour les actions
    à risque ou les valeurs extrêmes).
    """
    params = parameters or {}
    raw_action = str(params.get("action", "") or "").strip()
    description = str(params.get("description", "") or "").strip()
    value = params.get("value", None)
    confirmed = str(params.get("confirmed", "")).lower() in _TRUTHY

    # ── Étape 1 : parsing local si aucune action explicite ───────────────
    parsed = None
    if not raw_action and description:
        parsed = _parse_command_locally(description)
        if parsed and parsed.get("confidence", 0) >= 0.9:
            raw_action = parsed["action"]
            if value is None:
                value = parsed.get("value")
            if raw_action in ("volume_set", "brightness_set", "volume_up",
                              "volume_down", "brightness_up", "brightness_down"):
                target = "volume" if "volume" in raw_action else "brightness"
                current = get_current_volume() if target == "volume" else get_current_brightness()
                parsed = _validate_command(parsed, current, target)
                if parsed.get("warn") and not confirmed:
                    return (f"⚠️ {parsed['warn']} "
                            f"Je peux exécuter « {raw_action} {value} » si tu confirmes.")
        else:
            detected = _detect_action(description)
            if detected.get("error"):
                return f"Erreur IA : {detected['error']}"
            raw_action = detected.get("action", "") or ""
            if value is None:
                value = detected.get("value")

    action = raw_action.lower().strip().replace(" ", "_").replace("-", "_")
    action = _ACTION_ALIASES.get(action, action)

    # Extraction de valeur depuis la description si toujours absente
    if action == "brightness_set" and value is None and description:
        match = re.search(r"(\d+)\s*%?", description)
        value = int(match.group(1)) if match else 50

    if not action:
        return "Aucune action n'a pu être déterminée."

    print(f"[Settings] Action: {action}  Value: {value}  OS: {_OS}")
    if player:
        try:
            player.write_log(f"[Settings] {action}")
        except Exception:
            pass

    # ── Sécurité pour les actions dangereuses ────────────────────────────
    if action in _DANGEROUS_ACTIONS:
        func = ACTION_MAP.get(action)
        titles = {
            "restart": "Redémarrer l'ordinateur",
            "shutdown": "Éteindre l'ordinateur",
            "suspend": "Mettre l'ordinateur en veille",
            "toggle_wifi": "Modifier l'état du Wi-Fi",
        }
        return human_confirmation.request(
            f"computer:{action}", titles[action],
            "Cette opération peut interrompre ANO-GPT ou la connexion avec AnoRemote.",
            lambda f=func, title=titles[action]: (f(), f"{title} : commande envoyée.")[1],
        )

    # ── Exécution ────────────────────────────────────────────────────────
    try:
        if action == "volume_set":
            before = get_current_volume()
            volume_set(int(value if value is not None else 50))
            if before is not None:
                push_undo(
                    f"réglage du volume à {value}%",
                    lambda old=before: (volume_set(old), f"Volume restauré à {old}%.")[1],
                )
            vol_after = get_current_volume()
            return (f"Volume réglé à {vol_after}%." if vol_after is not None
                    else f"Volume réglé à {value}%.")
        if action == "brightness_set":
            before = get_current_brightness()
            brightness_set(int(value if value is not None else 50))
            if before is not None:
                push_undo(
                    f"réglage de la luminosité à {value}%",
                    lambda old=before: (brightness_set(old), f"Luminosité restaurée à {old}%.")[1],
                )
            return f"Luminosité réglée à {value}%."
        if action in ("volume_up", "volume_down"):
            before = get_current_volume()
            delta = int(value or 10)
            if action == "volume_up":
                volume_up(delta)
            else:
                volume_down(delta)
            v = get_current_volume()
            if before is not None:
                push_undo(
                    f"modification du volume ({action})",
                    lambda old=before: (volume_set(old), f"Volume restauré à {old}%.")[1],
                )
            label = "augmenté" if action == "volume_up" else "diminué"
            return f"Volume {label} de {delta}%{f' → {v}%' if v is not None else ''}."
        if action in ("brightness_up", "brightness_down"):
            before = get_current_brightness()
            delta = int(value or 10)
            if action == "brightness_up":
                brightness_up(delta)
            else:
                brightness_down(delta)
            b = get_current_brightness()
            if before is not None:
                push_undo(
                    f"modification de la luminosité ({action})",
                    lambda old=before: (brightness_set(old), f"Luminosité restaurée à {old}%.")[1],
                )
            label = "augmentée" if action == "brightness_up" else "diminuée"
            return f"Luminosité {label} de {delta}%{f' → {b}%' if b is not None else ''}."
        if action in ("type_text", "write_on_screen", "type", "write"):
            text = str(value or params.get("text", "")).strip()
            if not text:
                return "Aucun texte à taper."
            enter_after = str(params.get("press_enter", "false")).lower() in ("true", "1", "yes")
            type_text(text, press_enter_after=enter_after)
            return f"Texte tapé : {text[:80]}"
        if action == "press_key":
            key = str(value or params.get("key", "")).strip()
            if not key:
                return "Aucune touche spécifiée."
            press_key(key)
            return f"Touche pressée : {key}"
        if action in ("reload_n", "refresh_n", "reload_page_n"):
            reload_page_n(int(value or 1))
            return f"Page rechargée {value or 1} fois."
        if action == "scroll_up":
            scroll_up(int(value or 500))
            return "Défilement vers le haut."
        if action == "scroll_down":
            scroll_down(int(value or 500))
            return "Défilement vers le bas."
        if action == "screenshot":
            res = take_screenshot()
            return res or "Capture d'écran lancée."

        # Cas spécial close_app : délégation à la fermeture intelligente.
        if action in ("close_app", "quit_app", "kill_app"):
            app_name = (str(value or "").strip()
                        or str(params.get("app_name", "")).strip())
            if not app_name and description:
                m = re.search(
                    r"(?:ferme?r?|quitte?r?|tue?r?|close|kill|stop)\s+"
                    r"(?:l(?:a |e |'|’ )?)?(?:application |app |programme |fenêtre )?"
                    r"([a-z0-9\-_]+)", description.lower())
                if m:
                    app_name = m.group(1).strip()
            if app_name and _HAS_SMART_CLOSE:
                try:
                    return _smart_close_app(
                        parameters={"app_name": app_name, "description": description},
                        response=response, player=player, session_memory=session_memory)
                except Exception as e:
                    return f"Échec fermeture ({app_name}) : {e}"
            close_app(app_name)
            return (f"Action réalisée : fermeture de {app_name}."
                    if app_name else "Action réalisée : fenêtre active fermée.")

        func = ACTION_MAP.get(action)
        if not func:
            return f"Action inconnue : '{raw_action}'."
        res = func()
        if action == "dark_mode":
            push_undo(
                "basculement du thème sombre",
                lambda: (dark_mode(), "Thème précédent restauré.")[1],
            )
        if isinstance(res, str):
            return res
        return f"Action réalisée : {action}."
    except Exception as e:
        print(f"[Settings] Échec de l'action ({action}): {e}")
        return f"Échec de l'action ({action}) : {e}"


# ════════════════════════════════════════════════════════════════════════════
# Test direct
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        print(computer_settings({"description": " ".join(sys.argv[1:])}))
    else:
        print("Usage: python computer_settings.py <commande naturelle>")
