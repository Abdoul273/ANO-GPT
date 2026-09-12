"""
🚀 APP_CONTROL — Contrôle d'applications ultra-robuste pour Jarvis.
Lancement, fermeture, focus, statut, restart — avec détection réelle des
processus, détachement correct (Wayland/Hyprland), fermeture chirurgicale
(fenêtres Hyprland d'abord, PIDs bornés ensuite — JAMAIS de pkill -f brut)
et déplacement optionnel de la nouvelle fenêtre vers un bureau.

Sécurité clé : l'ancien `pkill -f {binary}` tuait tout processus dont la
ligne de commande contenait le mot (ex: « terminal » tuait le terminal de
l'assistant lui-même). La fermeture passe désormais par :
  1. fermeture des fenêtres Hyprland correspondantes (closewindow, puis
     killwindow si nécessaire, avec vérification de disparition) ;
  2. kill des PIDs restants uniquement, trouvés par pgrep borné
     (^|/)nom( |$) ou scan /proc, en SIGTERM puis SIGKILL.
"""
import functools
import os
import re
import subprocess
from core import action_kit as kit
import time
from pathlib import Path
from typing import Optional, Dict, List

# ────────────────────────────────────────────────────────────────────────────
# core.tool_utils : utilisé si présent, sinon fallbacks locaux autonomes
# ────────────────────────────────────────────────────────────────────────────
try:
    from core.tool_utils import (
        retry_on_failure,
        tracked_tool,
        cached_tool,
        CommandExecutor,
        handle_tool_error,
        check_command_exists,
    )
    _HAS_TOOL_UTILS = True
except Exception:
    _HAS_TOOL_UTILS = False

    def retry_on_failure(max_retries: int = 2, delay_ms: int = 50):
        def deco(fn):
            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                last = None
                for _ in range(max_retries + 1):
                    try:
                        return fn(*args, **kwargs)
                    except Exception as e:
                        last = e
                        time.sleep(delay_ms / 1000.0)
                raise last
            return wrapper
        return deco

    def tracked_tool(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            return fn(*args, **kwargs)
        return wrapper

    def cached_tool(ttl_seconds: int = 300):
        def deco(fn):
            cache: Dict = {}
            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                key = (args, tuple(sorted(kwargs.items())))
                now = time.time()
                hit = cache.get(key)
                if hit and (now - hit[0]) < ttl_seconds:
                    return hit[1]
                val = fn(*args, **kwargs)
                cache[key] = (now, val)
                return val
            return wrapper
        return deco

    class CommandExecutor:
        @staticmethod
        def run(cmd: str, timeout: float = 5):
            p = kit.run(cmd, shell=True, timeout=timeout)
            if p.timed_out:
                return False, "", "timeout"
            return p.ok, p.out, p.err

    def handle_tool_error(e: Exception, ctx: str) -> str:
        return f"❌ Erreur ({ctx}) : {e}"

    def check_command_exists(cmd: str) -> bool:
        return kit.which(cmd) is not None


# ────────────────────────────────────────────────────────────────────────────
# 🗂️ BASE D'APPLICATIONS (alias → binaires)
# ────────────────────────────────────────────────────────────────────────────
APP_ALIASES: Dict[str, List[str]] = {
    # Navigateurs
    "chrome": ["google-chrome-stable", "google-chrome"],
    "firefox": ["firefox"],
    "edge": ["microsoft-edge-stable", "microsoft-edge"],
    "brave": ["brave", "brave-browser"],
    "navigateur": ["google-chrome-stable", "google-chrome"],
    "browser": ["google-chrome-stable", "google-chrome"],
    # Terminaux & éditeurs
    "terminal": ["kitty", "alacritty", "foot", "wezterm", "gnome-terminal",
                 "konsole", "xfce4-terminal", "xterm"],
    "kitty": ["kitty"],
    "alacritty": ["alacritty"],
    "foot": ["foot"],
    "wezterm": ["wezterm"],
    "konsole": ["konsole"],
    "code": ["code", "code-oss"],
    "vscode": ["code", "code-oss"],
    "zcode": ["zcode"],
    "vim": ["vim", "nvim"],
    "neovim": ["nvim"],
    "sublime": ["sublime_text", "subl"],
    "kate": ["kate"],
    "éditeur": ["code", "code-oss", "kate", "sublime_text", "gedit"],
    "editor": ["code", "code-oss", "kate", "sublime_text", "gedit"],
    # Communication
    "telegram": ["telegram-desktop"],
    "discord": ["discord"],
    "slack": ["slack"],
    "whatsapp": ["whatsapp-nativefier", "zapzap"],
    "signal": ["signal-desktop", "signal"],
    "zoom": ["zoom"],
    "teams": ["teams-for-linux", "teams"],
    "chat": ["discord", "telegram-desktop", "slack", "signal-desktop", "zapzap"],
    # Média
    "vlc": ["vlc"],
    "mpv": ["mpv"],
    "spotify": ["spotify", "spotify-launcher"],
    "audacious": ["audacious"],
    "lollypop": ["lollypop"],
    "musique": ["vlc", "mpv", "spotify", "audacious", "lollypop"],
    "music": ["vlc", "mpv", "spotify", "audacious", "lollypop"],
    "youtube": ["google-chrome-stable", "google-chrome"],
    "gimp": ["gimp"],
    "inkscape": ["inkscape"],
    "blender": ["blender"],
    "obs": ["obs"],
    # Productivité
    "libreoffice": ["libreoffice", "soffice"],
    "writer": ["libreoffice", "soffice"],
    "calc": ["libreoffice", "soffice"],
    "impress": ["libreoffice", "soffice"],
    "notion": ["notion"],
    "obsidian": ["obsidian"],
    # Gestionnaires de fichiers
    "file explorer": ["nautilus"],
    "explorateur": ["nautilus"],
    "gestionnaire de fichiers": ["nautilus"],
    "dolphin": ["nautilus"],
    "nautilus": ["nautilus"],
    "thunar": ["thunar"],
    "yazi": ["yazi"],
    # Utilitaires
    "settings": ["gnome-control-center", "systemsettings"],
    "pavucontrol": ["pavucontrol"],
    "easyeffects": ["easyeffects"],
    "btop": ["btop"],
    "steam": ["steam", "steam-native"],
}


# ────────────────────────────────────────────────────────────────────────────
# 🖥️ Environnement de lancement (Wayland/Hyprland)
# ────────────────────────────────────────────────────────────────────────────

def _launch_env() -> Dict[str, str]:
    """Environnement complet pour lancer une app graphique : restaure
    DISPLAY/WAYLAND_DISPLAY/XDG_RUNTIME_DIR/HYPRLAND_INSTANCE_SIGNATURE
    si l'assistant en est dépourvu (service systemd, ssh…)."""
    env = {**os.environ}
    if not (env.get("WAYLAND_DISPLAY") or env.get("DISPLAY")):
        try:
            uid = os.getuid()
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
                        for var in ("WAYLAND_DISPLAY", "DISPLAY",
                                    "XAUTHORITY", "XDG_RUNTIME_DIR"):
                            if var in proc_env and not env.get(var):
                                env[var] = proc_env[var]
                        break
                except Exception:
                    continue
        except Exception:
            pass
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


def _hyprctl_json(*args: str) -> List[dict]:
    """Lecture Hyprland partagée (socle : délai, reprise, cache court).

    Chaque module gardait sa copie de cette fonction et relançait un processus
    par question. Le cache du socle fusionne les appels d'un même tour de
    parole : plusieurs actions qui listent les fenêtres n'en paient qu'un.
    """
    data = kit.hypr_json(*args, default=None)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return [data]
    return []


def _hypr_dispatch(dispatcher: str, arg: str = "") -> str:
    """Dispatch Hyprland. Le socle invalide le cache des fenêtres après coup."""
    res = kit.hypr("dispatch", dispatcher, *([arg] if arg else []))
    if res.not_found:
        return "error: hyprctl introuvable"
    if not res.ok:
        return f"error: {res.err.strip() or 'échec'}"
    return res.out.strip() or "ok"


def _ok(res: str) -> bool:
    low = (res or "").lower()
    return "error" not in low and "unknown" not in low and "invalid" not in low


# ────────────────────────────────────────────────────────────────────────────
# 🔍 Correspondance noms ↔ fenêtres / processus
# ────────────────────────────────────────────────────────────────────────────

def _name_tokens(app_name: str) -> set:
    """Étend un nom d'app en tous les tokens à chercher (alias inclus),
    avec variantes tiret/underscore."""
    low = (app_name or "").lower().strip()
    tokens = {low}
    if low in APP_ALIASES:
        tokens.update(APP_ALIASES[low])
    else:
        for alias, binaries in APP_ALIASES.items():
            if low == alias or low in binaries:
                tokens.add(alias)
                tokens.update(binaries)
                break
    out = set()
    for t in tokens:
        t = t.strip().lower()
        if not t:
            continue
        out.add(t)
        out.add(t.replace(" ", "-"))
        out.add(t.replace(" ", "_"))
    return out


def _match_client(client: dict, tokens: set) -> bool:
    """Correspondance par mots entiers sur classe/titre — pas de substring
    brut (« code » ne doit pas matcher n'importe quoi)."""
    blob = " ".join(str(client.get(k) or "") for k in
                    ("class", "initialClass", "title", "initialTitle")).lower()
    if not blob.strip():
        return False
    for t in tokens:
        if len(t) < 2:
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", blob):
            return True
    return False


@cached_tool(ttl_seconds=300)
def _find_app_binary(app_name: str) -> Optional[str]:
    """Trouve le binaire réel d'une app. Retourne None si rien d'installé —
    plus jamais de « lancement fantôme » sur un binaire inexistant."""
    low = (app_name or "").lower().strip()
    ordered = [low] + [t for t in sorted(_name_tokens(low)) if t != low]
    for cand in ordered:
        if check_command_exists(cand):
            return cand
    return None


# ────────────────────────────────────────────────────────────────────────────
# ⚙️ Processus : recherche bornée + terminaison propre
# ────────────────────────────────────────────────────────────────────────────

def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _pids_from_pgrep(tokens: set) -> List[int]:
    """pgrep -f avec pattern BORNÉ (^|/)nom( |$) : « kitty » ne matche plus
    une ligne de commande qui contiendrait le mot par hasard."""
    if not kit.which("pgrep"):
        return []
    pids = set()
    my_pid = os.getpid()
    for t in tokens:
        t = t.strip().lower()
        if not t:
            continue
        pat = re.escape(t) if " " in t else rf"(^|/){re.escape(t)}( |$)"
        try:
            r = kit.run(["pgrep", "-f", pat], timeout=3)
            for line in (r.stdout or "").splitlines():
                if line.strip().isdigit():
                    pids.add(int(line.strip()))
        except Exception:
            continue
    return [p for p in pids if p not in (my_pid, 1)]


def _pids_from_proc(tokens: set) -> List[int]:
    """Repli : scan /proc (égalité exacte sur les noms d'arguments)."""
    pids = []
    my_pid = os.getpid()
    if not Path("/proc").is_dir():
        return []
    for pid_dir in Path("/proc").iterdir():
        if not pid_dir.is_dir() or not pid_dir.name.isdigit():
            continue
        pid = int(pid_dir.name)
        if pid in (my_pid, 1):
            continue
        try:
            cmdline = (pid_dir / "cmdline").read_text(errors="replace")
            names = set()
            for a in cmdline.split("\x00"):
                a = a.strip()
                if not a:
                    continue
                names.add(os.path.basename(a).lower())
                names.add(a.lower())
            try:
                names.add(os.path.basename(os.readlink(str(pid_dir / "exe"))).lower())
            except Exception:
                pass
            if names & tokens:
                pids.append(pid)
        except Exception:
            continue
    return pids


def _find_pids(app_name: str) -> List[int]:
    tokens = _name_tokens(app_name)
    pids = _pids_from_pgrep(tokens)
    if not pids:
        pids = _pids_from_proc(tokens)
    return pids


def _terminate_pid(pid: int) -> bool:
    """SIGTERM, attente, SIGKILL si besoin. Jamais sur soi-même ou PID 1."""
    if pid in (os.getpid(), 1):
        return False
    try:
        os.kill(pid, 15)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    for _ in range(8):
        time.sleep(0.1)
        if not _alive(pid):
            return True
    try:
        os.kill(pid, 9)
    except Exception:
        return False
    time.sleep(0.15)
    return not _alive(pid)


def _hypr_close_window(addr: str) -> bool:
    """Ferme une fenêtre par adresse : closewindow d'abord, escalade
    killwindow, avec vérification réelle de disparition."""
    if not addr:
        return False
    for dispatcher in ("closewindow", "killwindow"):
        _hypr_dispatch(dispatcher, f"address:{addr}")
        time.sleep(0.15)
        if not any(c.get("address") == addr for c in _hyprctl_json("clients")):
            return True
    return False


# ────────────────────────────────────────────────────────────────────────────
# 🚀 LANCEMENT (détaché, vérifié, workspace-aware)
# ────────────────────────────────────────────────────────────────────────────

def _snapshot_addresses() -> set:
    return {c.get("address", "") for c in _hyprctl_json("clients")
            if c.get("address")}


def _wait_process_or_window(binary: str, before: set, timeout: float = 4.0) -> bool:
    """Preuve réelle de démarrage : une nouvelle fenêtre Hyprland OU un
    processus correspondant au binaire."""
    base = os.path.basename(binary).lower()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for c in _hyprctl_json("clients"):
            if c.get("address") and c["address"] not in before:
                return True
        if kit.which("pgrep"):
            try:
                r = kit.run(
                    ["pgrep", "-f", rf"(^|/){re.escape(base)}( |$)"], timeout=2)
                for line in (r.stdout or "").splitlines():
                    if line.strip().isdigit() and int(line.strip()) != os.getpid():
                        return True
            except Exception:
                pass
        time.sleep(0.2)
    return False


def _do_move(addr: str, workspace: int) -> bool:
    return _ok(_hypr_dispatch("movetoworkspacesilent",
                              f"{workspace},address:{addr}"))


def _move_new_window(binary: str, workspace: int, before: set,
                     timeout: float = 4.0) -> bool:
    """Déplace silencieusement la fenêtre apparue après lancement vers le
    bureau demandé. Préfère une fenêtre dont classe/titre correspond au
    binaire, se rabat sur la première nouvelle fenêtre après 2,4 s."""
    if not kit.which("hyprctl"):
        return False
    needle = os.path.basename(binary).lower()
    deadline = time.monotonic() + timeout
    named_deadline = time.monotonic() + max(1.5, timeout * 0.6)
    fallback_addr = None
    while time.monotonic() < deadline:
        clients = _hyprctl_json("clients")
        new = [c for c in clients
               if c.get("address") and c["address"] not in before]
        if needle:
            for c in new:
                blob = " ".join(str(c.get(k) or "") for k in
                                ("class", "initialClass", "title")).lower()
                if needle in blob:
                    return _do_move(c["address"], workspace)
        if fallback_addr is None and new:
            fallback_addr = new[0]["address"]
        if fallback_addr and time.monotonic() >= named_deadline:
            return _do_move(fallback_addr, workspace)
        time.sleep(0.15)
    if fallback_addr:
        return _do_move(fallback_addr, workspace)
    return False


@tracked_tool
@retry_on_failure(max_retries=2, delay_ms=50)
def launch_app(app_name: str, workspace: Optional[int] = None,
               target: Optional[str] = None) -> str:
    """Lance une application de façon détachée, vérifie réellement le
    démarrage, et déplace la fenêtre vers `workspace` si demandé."""
    app_name = (app_name or "").strip()
    from core.browser_policy import BROWSER_ALIASES
    if app_name.lower() in BROWSER_ALIASES:
        app_name = "chrome"
    binary = _find_app_binary(app_name)
    if not binary:
        return (f"❌ Application '{app_name}' introuvable "
                f"(aucun binaire correspondant installé).")

    before = _snapshot_addresses()
    argv = [binary]
    if target:
        argv.append(str(Path(target).expanduser()))
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=_launch_env(),
        )
    except Exception as e:
        return handle_tool_error(e, app_name)

    if not _wait_process_or_window(binary, before, timeout=4.0):
        rc = proc.poll()
        if rc is not None and rc != 0:
            return f"❌ {app_name} a échoué au démarrage (code {rc})."
        return (f"⚠️ {app_name} lancé mais démarrage non confirmé "
                f"(application lente ou sans fenêtre).")

    note = ""
    if workspace is not None:
        try:
            ws = int(workspace)
            if _move_new_window(binary, ws, before):
                note = f" (bureau {ws})"
            else:
                note = f" — déplacement vers le bureau {ws} non confirmé"
        except (TypeError, ValueError):
            pass
    return f"✅ {app_name.capitalize()} lancé{note}."


# ────────────────────────────────────────────────────────────────────────────
# 🔒 FERMETURE (fenêtres d'abord, PIDs bornés ensuite — jamais de pkill -f)
# ────────────────────────────────────────────────────────────────────────────

@tracked_tool
@retry_on_failure(max_retries=2, delay_ms=50)
def close_app(app_name: str) -> str:
    """Ferme proprement : fenêtres Hyprland correspondantes d'abord, puis
    les processus restants uniquement (PIDs précis, jamais de pkill -f)."""
    app_name = (app_name or "").strip()
    tokens = _name_tokens(app_name)

    # 1. Fenêtres Hyprland
    clients = _hyprctl_json("clients")
    targets = [c for c in clients if _match_client(c, tokens)]
    closed_windows = 0
    for c in targets:
        if _hypr_close_window(c.get("address", "")):
            closed_windows += 1
        time.sleep(0.05)
    if closed_windows:
        # Laisse les processus propriétaires se terminer, sans attendre plus
        # longtemps que nécessaire : on relit la liste des fenêtres.
        addrs = {c.get("address") for c in targets}
        kit.wait_until(
            lambda: not any(c.get("address") in addrs for c in kit.hypr_clients()),
            timeout=1.0, interval=0.08)

    # 2. Processus restants (têteless/tray, ou apps sans fenêtre)
    killed = 0
    for pid in _find_pids(app_name):
        if _terminate_pid(pid):
            killed += 1

    total = closed_windows + killed
    if total == 0:
        if not targets and not _find_pids(app_name):
            return f"⚠️ {app_name} ne semble pas ouvert."
        return f"❌ Impossible de fermer {app_name}."
    if total == 1 and closed_windows == 1:
        return f"✅ {app_name.capitalize()} fermé."
    return f"✅ {app_name.capitalize()} fermé ({closed_windows} fenêtre(s), {killed} processus)."


# ────────────────────────────────────────────────────────────────────────────
# 👁️ FOCUS (par adresse réelle de fenêtre, réponse vérifiée)
# ────────────────────────────────────────────────────────────────────────────

@tracked_tool
def focus_app(app_name: str) -> str:
    """Donne le focus à la première fenêtre correspondante, via son adresse
    Hyprland exacte (fiable même avec plusieurs apps aux classes proches)."""
    app_name = (app_name or "").strip()
    tokens = _name_tokens(app_name)
    clients = _hyprctl_json("clients")
    target = next((c for c in clients if _match_client(c, tokens)), None)

    if target and target.get("address"):
        if _ok(_hypr_dispatch("focuswindow", f"address:{target['address']}")):
            return f"✅ Focus sur {app_name}."
    # Repli par classe (regex insensible à la casse)
    if _ok(_hypr_dispatch("focuswindow", f"class:(?i){app_name}")):
        return f"✅ Focus sur {app_name}."
    # Dernier recours (X11/Xwayland uniquement)
    if kit.which("xdotool"):
        ok, _, _ = CommandExecutor.run(
            f"xdotool search --class {app_name} windowactivate", timeout=2)
        if ok:
            return f"✅ Focus sur {app_name}."
    if target is None:
        return f"⚠️ Aucune fenêtre '{app_name}' à focaliser."
    return f"⚠️ Impossible de donner le focus à {app_name}."


# ────────────────────────────────────────────────────────────────────────────
# 📋 STATUT (détection réelle, alias inclus)
# ────────────────────────────────────────────────────────────────────────────

def app_is_running(app_name: str) -> bool:
    """Vrai si une fenêtre correspond OU si un processus borné correspond."""
    tokens = _name_tokens(app_name)
    for c in _hyprctl_json("clients"):
        if _match_client(c, tokens):
            return True
    return bool(_find_pids(app_name))


@tracked_tool
def app_status(app_name: str) -> str:
    app_name = (app_name or "").strip()
    if app_is_running(app_name):
        return f"✅ {app_name} est ouvert."
    return f"❌ {app_name} n'est pas en cours d'exécution."


# ────────────────────────────────────────────────────────────────────────────
# 🎮 ROUTAGE PRINCIPAL
# ────────────────────────────────────────────────────────────────────────────

def _as_int(value) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


@kit.action("app_control")
def app_control(parameters: dict, player=None) -> str:
    """
    Contrôle d'applications unifié.
    Actions :
      - launch(app_name, workspace?, target?) — ouvre, vérifie, déplace
      - close(app_name)  — fenêtres Hyprland d'abord, PIDs bornés ensuite
      - focus(app_name)  — focus par adresse de fenêtre
      - status(app_name) — ouvert ou non
      - restart(app_name, workspace?) — fermeture complète puis relance
    """
    params = parameters or {}
    action = str(params.get("action", "") or "").lower().strip()
    app_name = str(params.get("app_name", "") or "").strip()
    workspace = _as_int(params.get("workspace"))
    target = str(params.get("target", "") or "").strip() or None

    if not app_name:
        return "❌ Veuillez spécifier un nom d'application."
    if player:
        try:
            player.write_log(f"[app_control] {action} {app_name}")
        except Exception:
            pass

    try:
        if action == "launch":
            return launch_app(app_name, workspace=workspace, target=target)
        elif action == "close":
            return close_app(app_name)
        elif action == "focus":
            return focus_app(app_name)
        elif action == "status":
            return app_status(app_name)
        elif action == "restart":
            close_app(app_name)
            # Attendre la mort effective avant de relancer (sinon la
            # relance peut se greffer sur l'instance mourante).
            deadline = time.monotonic() + 2.0
            while app_is_running(app_name) and time.monotonic() < deadline:
                time.sleep(0.15)
            return launch_app(app_name, workspace=workspace, target=target)
        else:
            return f"❓ Action inconnue : {action}"
    except Exception as e:
        return handle_tool_error(e, "app_control")


# ────────────────────────────────────────────────────────────────────────────
# Test direct : python app_control.py <action> <app>
# ────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3:
        print(app_control({"action": sys.argv[1], "app_name": sys.argv[2]}))
    else:
        print("Usage: app_control.py <launch|close|focus|status|restart> <app>")
