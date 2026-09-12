#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
shell_exec.py — Accès complet au terminal + contrôle natif Hyprland/Wayland,
avec parsing local intelligent, sécurité renforcée et réponses en français.

Capacités :
- exécution de commandes shell (timeout réel avec nettoyage du groupe de
  processus, sortie tronquée intelligemment) ;
- détachement automatique des applications graphiques/lecteurs : plus de
  blocage de l'assistant ni de processus tué en fin de tour ;
- contrôle Hyprland : workspaces (chiffres ET ordinaux), focus, fermeture,
  plein écran, flottant, épinglage, redimensionnement, déplacement,
  saisie clavier (wtype + repli hyprctl), presse-papiers, captures grim/slurp ;
- blocage des commandes réellement destructrices via regex à mots entiers
  (insensible aux espaces multiples / tabulations).
"""
import json
import os
import re
import shlex
import shutil
import subprocess
from core import action_kit as kit
import time
from pathlib import Path
from typing import Optional, Dict, Any, List

from core import human_confirmation
from core.live_model_policy import FAST_MODEL

_HUMAN_APPROVED = object()

try:
    from actions.devsecops import devsecops_control, parse_devsecops_intent
    from actions.hypr_orchestrator import hypr_orchestrator_control, parse_hypr_orchestrator_intent
    from actions.second_brain import second_brain_action, parse_second_brain_intent
    _HAS_DEVSECOPS_MODULES = True
except ImportError:
    _HAS_DEVSECOPS_MODULES = False

_LOG_PATH = Path.home() / ".cache" / "ano-gpt" / "shell_exec.log"


# ════════════════════════════════════════════════════════════════════════════
# Sécurité — commandes réellement destructrices (regex à mots entiers)
# ════════════════════════════════════════════════════════════════════════════
# Insensible aux variantes d'espacement (« rm   -rf   / ») que l'ancienne
# liste par sous-chaînes laissait passer.

_BLOCK_PATTERNS: List[str] = [
    r"\brm\s+(?:-[a-z]*r[a-z]*f|--recursive\b)[^\n]*\s(?:/|/\*|~|\$HOME|\$\{HOME\})(?:\s|$|/\*)",
    r"\brm\s+(?:-[a-z]*f[a-z]*r)[^\n]*\s(?:/|/\*|~|\$HOME)(?:\s|$)",
    r"--no-preserve-root",
    r"\bmkfs(?:\.[a-z0-9]+)?\b",
    r"\bdd\s+(?:\S+\s+)*of=/dev/",
    r":\(\)\s*\{\s*:\|:\s*&\s*\}\s*;\s*:",          # fork bomb
    r"\bwipefs\b",
    r"\bshred\s+.*\s/dev/",
    r"\bchmod\s+(?:-[a-z]*\s+)*(?:777|000)\s+(?:/|~|\$HOME)\b",
    r"\bchown\s+(?:-[a-z]*\s+)*\S+\s+(?:/|~|\$HOME)\b",
    r">\s*/dev/(?:sd[a-z]|nvme\d+n\d+)\b",
]
_BLOCK_RE = re.compile("|".join(_BLOCK_PATTERNS), re.IGNORECASE)


def _is_blocked(command: str) -> bool:
    return bool(_BLOCK_RE.search(command or ""))


# Dangereux mais pas assez pour un refus pur et dur : demande confirmation
# (carte oui/non côté UI) plutôt qu'exécution silencieuse ou blocage total.
_RISKY_PATTERNS: List[str] = [
    r"\bsudo\b", r"\bsu\s+-", r"\bdoas\b",
    r"\brm\s+-[a-z]*r[a-z]*f?\b", r"\brm\s+-[a-z]*f[a-z]*r\b",
    r"\bpkill\b", r"\bkillall\b",
    r"\bsystemctl\s+(?:stop|disable|mask|restart)\b",
    r"\bsystemctl\s+(?:poweroff|reboot|suspend|hibernate)\b",
    r"(?:^|[;&|]\s*)(?:sudo\s+)?(?:shutdown|reboot|poweroff|halt)(?:\s|$)",
    r"\bnmcli\s+(?:radio\s+wifi|networking)\s+(?:off|on)\b",
    r"\bgit\s+reset\s+--hard\b", r"\bgit\s+push\s+(?:.*\s)?--force\b",
    r"\bgit\s+clean\s+-[a-z]*d[a-z]*f\b",
    r"\b(?:pacman\s+-R|apt(?:-get)?\s+(?:remove|purge)|dnf\s+remove|yum\s+remove)\b",
    r"\bcrontab\s+-r\b", r"\buserdel\b", r"\bgroupdel\b", r"\bpasswd\b",
    r"\bfdisk\b", r"\bparted\b", r"\bmount\b", r"\bumount\b",
    r"\bchmod\s+-R\b", r"\bchown\s+-R\b",
    r">\s*/(?:etc|boot|usr|lib|var)/\S",
]
_RISKY_RE = re.compile("|".join(_RISKY_PATTERNS), re.IGNORECASE)


def _is_risky(command: str) -> bool:
    return bool(_RISKY_RE.search(command or ""))


# ════════════════════════════════════════════════════════════════════════════
# Helpers généraux
# ════════════════════════════════════════════════════════════════════════════

def _base_dir() -> Path:
    import sys
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def _get_api_key() -> str:
    try:
        cfg_path = _base_dir() / "config" / "api_keys.json"
        with open(cfg_path, "r", encoding="utf-8") as f:
            return json.load(f).get("gemini_api_key", "")
    except Exception:
        return ""


def _log(entry: dict) -> None:
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _restore_display_env(env: dict) -> None:
    """Récupère DISPLAY/WAYLAND_DISPLAY depuis un processus de la session
    graphique si l'assistant en est dépourvu (service systemd, ssh…)."""
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
            content = env_file.read_text(errors="ignore")
            proc_env = {}
            for line in content.split("\x00"):
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
    """Environnement avec XDG_RUNTIME_DIR et HYPRLAND_INSTANCE_SIGNATURE
    restaurés si absents — indispensable quand l'assistant tourne hors de
    la session (sinon hyprctl répond « no running instance »)."""
    env = {**os.environ}
    _restore_display_env(env)
    if not env.get("XDG_RUNTIME_DIR"):
        candidate = Path(f"/run/user/{os.getuid()}")
        if candidate.exists():
            env["XDG_RUNTIME_DIR"] = str(candidate)
    if not env.get("HYPRLAND_INSTANCE_SIGNATURE"):
        runtime_dir = Path(env.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
        hypr_dir = runtime_dir / "hypr"
        try:
            if hypr_dir.exists():
                instances = sorted(
                    (d for d in hypr_dir.iterdir() if d.is_dir()),
                    key=lambda d: d.stat().st_mtime,
                    reverse=True,
                )
                if instances:
                    env["HYPRLAND_INSTANCE_SIGNATURE"] = instances[0].name
        except Exception:
            pass
    return env


def _first_binary(command: str) -> str:
    """Premier vrai binaire de la commande (ignore env VAR=x, sudo, nohup…)."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    for tok in tokens:
        if tok in ("sudo", "nohup", "env", "setsid", "time"):
            continue
        if "=" in tok and not tok.startswith("-"):
            continue
        return Path(tok).name.lower()
    return ""


# ════════════════════════════════════════════════════════════════════════════
# Extraction workspace — chiffres ET ordinaux français
# ════════════════════════════════════════════════════════════════════════════

_WS = r"(?:bureau|workspace|ws|espace\s+de\s+travail|desktop)"
_PREP = r"(?:dans\s+(?:le|la)?|au|sur\s+(?:le|la)?|du|de\s+|le|la)?\s*"
_ORD_FR = (
    r"premi(?:er|ère|ere|re)|deuxi[èe]me|second[e]?|troisi[èe]me|quatri[èe]me|"
    r"cinqui[èe]me|sixi[èe]me|septi[èe]me|huiti[èe]me|neuvi[èe]me|dixi[èe]me|"
    r"onzi[èe]me|douzi[èe]me|treizi[èe]me|quatorzi[èe]me|quinzi[èe]me|seizi[èe]me|"
    r"dix[- ]septi[èe]me|dix[- ]huiti[èe]me|dix[- ]neuvi[èe]me|vingti[èe]me"
)


def _ordinal_to_int(word: str) -> Optional[int]:
    import unicodedata
    w = unicodedata.normalize("NFKD", (word or "").lower().replace("-", " "))
    w = "".join(c for c in w if not unicodedata.combining(c))
    checks = [
        (r"premi", 1), (r"deux|second", 2), (r"trois", 3), (r"quatr", 4),
        (r"cinqu", 5), (r"six", 6), (r"sept", 7), (r"huit", 8), (r"neuv", 9),
        (r"dix\s*sept", 17), (r"dix\s*huit", 18), (r"dix\s*neuf", 19),
        (r"dix", 10), (r"onz", 11), (r"douz", 12), (r"treiz", 13),
        (r"quatorz", 14), (r"quinz", 15), (r"seiz", 16), (r"vingt", 20),
    ]
    for rx, val in checks:
        if re.search(rx, w):
            return val
    return None


def _parse_workspace_value(value: str) -> Optional[str]:
    """« 4 », « bureau 4 », « deuxième bureau », « bureau n°3 » → cible
    Hyprland (« 4 »). None si incompréhensible."""
    v = (value or "").strip()
    if not v:
        return None
    if re.fullmatch(r"[+-]?\d+", v):
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
# Détection des apps graphiques → détachement automatique
# ════════════════════════════════════════════════════════════════════════════

_GUI_APPS = {
    # Lecteurs média
    "vlc", "cvlc", "mpv", "audacious", "lollypop", "rhythmbox", "totem",
    "spotify", "elisa", "clementine", "strawberry", "celluloid",
    # Navigateurs
    "firefox", "chromium", "chrome", "google-chrome", "google-chrome-stable",
    "brave", "vivaldi", "opera", "microsoft-edge", "epiphany",
    # Terminaux & gestionnaires de fichiers
    "kitty", "alacritty", "foot", "wezterm", "gnome-terminal", "konsole",
    "xterm", "tilix", "nautilus", "thunar", "dolphin", "pcmanfm", "yazi",
    # Éditeurs / IDE
    "code", "codium", "zcode", "sublime_text", "subl", "kate", "kwrite",
    "gedit", "mousepad", "atom", "neovide",
    # Outils graphiques divers
    "gimp", "inkscape", "blender", "krita", "obs", "pavucontrol",
    "easyeffects", "gnome-system-monitor", "btop", "kcalc",
    "gnome-calculator", "seahorse", "meld", "qt6ct", "kvantummanager",
    "localsend", "scrcpy", "telegram", "discord", "slack", "signal",
    "whatsapp", "zapzap", "marknote", "gradia", "swappy", "ventoy",
    "gparted", "blueman-manager", "nm-connection-editor",
}


# Les gestionnaires de paquets ont souvent besoin d'un TTY : mot de passe
# sudo, confirmation « [O/n] », progression ncurses, etc. Les exécuter avec
# stdout/stderr capturés donne une commande qui paraît démarrer, puis échoue
# ou est tuée au timeout sans que l'utilisateur voie pourquoi.
_PACKAGE_INSTALL_RE = re.compile(
    r"(?:^|[;&|]\s*)(?:(?:sudo|doas)\s+)?(?:"
    r"pacman\s+-S(?:\s|$)|yay\s+-S(?:\s|$)|paru\s+-S(?:\s|$)|"
    r"apt(?:-get)?\s+install(?:\s|$)|dnf\s+install(?:\s|$)|"
    r"yum\s+install(?:\s|$)|zypper\s+install(?:\s|$)|"
    r"apk\s+add(?:\s|$)|flatpak\s+install(?:\s|$)|"
    r"snap\s+install(?:\s|$)"
    r")",
    re.IGNORECASE,
)


def _needs_interactive_terminal(command: str) -> bool:
    """Vrai pour les commandes qu'il ne faut jamais cacher derrière des pipes."""
    stripped = (command or "").strip()
    if re.match(r"^(?:sudo|doas|su\s+-c\b)", stripped, re.IGNORECASE):
        return True
    return bool(_PACKAGE_INSTALL_RE.search(stripped))


def _terminal_argv(command: str) -> Optional[List[str]]:
    """Construit l'argv du premier terminal disponible, sans shell parent."""
    # Le shell interactif reste ouvert après succès comme après erreur : le
    # résultat ne disparaît donc plus avant d'avoir pu être lu.
    script = (
        f"{command}\n"
        "_ano_status=$?\n"
        "printf '\\n[ANO-GPT] Commande terminée (code %s).\\n' \"$_ano_status\"\n"
        "printf 'Appuyez sur Entrée pour fermer cette fenêtre…'\n"
        "read -r _ano_reply\n"
        "exit \"$_ano_status\""
    )
    candidates = (
        ("kitty", ["kitty", "--title", "ANO-GPT — Installation", "--hold", "bash", "-lc", script]),
        ("foot", ["foot", "--title=ANO-GPT — Installation", "--hold", "bash", "-lc", script]),
        ("alacritty", ["alacritty", "-T", "ANO-GPT — Installation", "-e", "bash", "-lc", script]),
        ("wezterm", ["wezterm", "start", "--always-new-process", "--", "bash", "-lc", script]),
        ("konsole", ["konsole", "--hold", "-p", "tabtitle=ANO-GPT — Installation", "-e", "bash", "-lc", script]),
        ("gnome-terminal", ["gnome-terminal", "--title=ANO-GPT — Installation", "--", "bash", "-lc", script]),
        ("xterm", ["xterm", "-T", "ANO-GPT — Installation", "-hold", "-e", "bash", "-lc", script]),
    )
    for binary, argv in candidates:
        if shutil.which(binary):
            return argv
    return None


def _is_gui_command(command: str) -> bool:
    return _first_binary(command) in _GUI_APPS


# ════════════════════════════════════════════════════════════════════════════
# Nettoyage spécifique VLC (chemin relatif au dossier du média)
# ════════════════════════════════════════════════════════════════════════════

def _prepare_vlc(command: str) -> str:
    """VLC lance mieux avec le média en chemin relatif depuis son dossier.
    Corrige l'ancien bug d'index décalé après retrait de nohup/&."""
    command = re.sub(r"-{1,2}platform\s+\S+", "", command)
    try:
        tokens = [t for t in shlex.split(command) if t not in ("&", "nohup")]
    except ValueError:
        return command
    media_exts = (".mp3", ".wav", ".flac", ".aac", ".ogg", ".opus", ".m4a",
                  ".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".flv")
    file_idx = -1
    for i, tok in enumerate(tokens):
        if tok.startswith("-"):
            continue
        base = Path(tok).name.lower()
        if "/" in tok or base.endswith(media_exts):
            file_idx = i
            break
    if file_idx == -1:
        return command
    path = Path(os.path.expanduser(tokens[file_idx])).resolve()
    if path.exists() and path.is_file():
        tokens[file_idx] = path.name
        quoted = " ".join(shlex.quote(t) for t in tokens)
        return f"cd {shlex.quote(str(path.parent))} && {quoted}"
    return command


# ════════════════════════════════════════════════════════════════════════════
# Exécution — timeout propre + détachement des apps graphiques
# ════════════════════════════════════════════════════════════════════════════

def _run_normal(command: str, cwd: str, timeout: float) -> str:
    """Exécution classique en groupe de processus : au timeout, TOUT le
    groupe est tué (l'ancienne version laissait les enfants orphelins)."""
    start = time.time()
    try:
        proc = subprocess.Popen(
            command, shell=True, cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=_hypr_env(), start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, 9)
            except Exception:
                proc.kill()
            try:
                proc.communicate(timeout=2)
            except Exception:
                pass
            return (f"La commande a dépassé le délai de {timeout:.0f}s et a été "
                    f"interrompue (processus enfants inclus) : {command}")
    except Exception as e:
        return f"Échec d'exécution de la commande : {e}"

    duration = time.time() - start
    stdout = (stdout or "").strip()
    stderr = (stderr or "").strip()
    _log({
        "ts": time.time(), "command": command, "cwd": cwd,
        "returncode": proc.returncode, "duration_s": round(duration, 2),
    })
    parts = [f"[exit {proc.returncode}]"]
    if stdout:
        if len(stdout) > 6000:
            parts.append(stdout[:6000] + f"\n… (tronqué, {len(stdout)} caractères au total)")
        else:
            parts.append(stdout)
    if stderr:
        parts.append(f"stderr: {stderr[:2000]}")
    return "\n".join(parts) if len(parts) > 1 else f"[exit {proc.returncode}] (aucune sortie)"


def _run_detached(command: str, cwd: str) -> str:
    """Lance en arrière-plan totalement détaché (setsid + session neuve) :
    la commande survit à la fin du tour de l'assistant et ne bloque jamais
    l'écoute. Vérifie que le lancement a bien pris."""
    try:
        proc = subprocess.Popen(
            command, shell=True, cwd=cwd,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, env=_hypr_env(), start_new_session=True,
        )
    except Exception as e:
        return f"Échec du lancement en arrière-plan : {e}"
    # On guette un échec immédiat (binaire absent, syntaxe refusée) plutôt que
    # d'attendre systématiquement : une commande qui part bien rend la main
    # tout de suite, et l'erreur d'une commande qui tombe est signalée aussitôt.
    kit.wait_until(lambda: proc.poll() is not None, timeout=0.6, interval=0.03)
    if proc.poll() is not None and proc.returncode not in (0, None):
        return (f"Le lancement en arrière-plan a échoué "
                f"(code {proc.returncode}) : {command}")
    _log({"ts": time.time(), "command": command, "cwd": cwd,
          "returncode": None, "detached": True})
    return f"Lancé en arrière-plan : {command}"


def _run_interactive(command: str, cwd: str) -> str:
    """Lance une commande privilégiée dans un terminal visible et persistant."""
    argv = _terminal_argv(command)
    if argv is None:
        return (
            "Impossible de lancer la commande interactive : aucun terminal "
            "compatible n'est installé (kitty, foot, alacritty, wezterm, "
            "konsole, gnome-terminal ou xterm)."
        )
    try:
        proc = subprocess.Popen(
            argv, cwd=cwd, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=_hypr_env(), start_new_session=True,
        )
    except Exception as e:
        return f"Échec de l'ouverture du terminal d'installation : {e}"
    time.sleep(0.6)
    if proc.poll() is not None and proc.returncode not in (0, None):
        return f"Le terminal d'installation n'a pas pu démarrer (code {proc.returncode})."
    _log({
        "ts": time.time(), "command": command, "cwd": cwd,
        "returncode": None, "interactive_terminal": Path(argv[0]).name,
    })
    return (
        "Installation lancée dans un terminal visible. Saisissez le mot de "
        "passe si demandé ; la fenêtre restera ouverte sur le résultat."
    )


def adapt_command_for_arch(command: str) -> str:
    """Adapte intelligemment toute commande vers l'écosystème Arch Linux (pacman/yay)."""
    if not command or not isinstance(command, str):
        return ""
    cmd = command.strip()
    if not cmd:
        return ""

    # a. Nettoyage des replis multi-distribution
    if "||" in cmd:
        parts = cmd.split("||")
        first = parts[0].strip()
        rest = "||".join(parts[1:])
        is_arch = bool(re.search(r"^(?:sudo\s+)?(?:pacman|yay|paru)\b", first))
        has_non_arch = bool(re.search(r"\b(?:apt(?:-get)?|dnf|yum|zypper|apk)\b", rest))
        if is_arch and has_non_arch:
            cmd = first

    # b. Traduction des commandes Debian/Ubuntu (apt/apt-get)
    if re.search(r"(?:sudo\s+)?apt(?:-get)?\s+install\b", cmd):
        cmd = re.sub(
            r"(sudo\s+)?apt(?:-get)?\s+install\s+(?:-[a-zA-Z0-9_-]+\s+)*",
            r"\1pacman -S --needed ",
            cmd,
        )
        cmd = re.sub(r"\s+(?:-y|--yes|-q|--quiet|--assume-yes)\b", "", cmd)
    elif re.search(r"(?:sudo\s+)?apt(?:-get)?\s+update\b", cmd):
        cmd = re.sub(
            r"(sudo\s+)?apt(?:-get)?\s+update\b.*",
            r"\1pacman -Sy",
            cmd,
        )
    elif re.search(r"(?:sudo\s+)?apt(?:-get)?\s+upgrade\b", cmd):
        cmd = re.sub(
            r"(sudo\s+)?apt(?:-get)?\s+upgrade(?:\s+-[a-zA-Z0-9_-]+)*\b.*",
            r"\1pacman -Syu",
            cmd,
        )
    elif re.search(r"(?:sudo\s+)?apt(?:-get)?\s+(?:remove|purge)\b", cmd):
        cmd = re.sub(
            r"(sudo\s+)?apt(?:-get)?\s+(?:remove|purge)(?:\s+--purge)?\s+",
            r"\1pacman -Rns ",
            cmd,
        )
    elif re.search(r"(?:sudo\s+)?apt(?:-cache)?\s+search\b", cmd):
        cmd = re.sub(
            r"(?:sudo\s+)?apt(?:-cache)?\s+search\s+",
            "pacman -Ss ",
            cmd,
        )

    return cmd.strip()


# ════════════════════════════════════════════════════════════════════════════
# run_shell — exécution de commandes shell
# ════════════════════════════════════════════════════════════════════════════

def run_shell(parameters=None, player=None, **_kwargs) -> str:
    """Exécute une commande shell arbitraire et retourne le résultat.
    Les applications graphiques/lecteurs sont automatiquement détachés en
    arrière-plan pour ne jamais bloquer l'assistant."""
    params = parameters or {}
    raw_command = (params.get("command") or "").strip()
    command = adapt_command_for_arch(raw_command)
    # Le répartiteur central accorde 120 s à shell_exec. Garder une petite
    # marge permet à _run_normal de nettoyer son groupe avant ce délai.
    timeout = float(params.get("timeout") or 110)
    cwd = params.get("cwd") or str(Path.home())

    if not command:
        return "Aucune commande fournie."
    if _is_blocked(command):
        return f"Commande refusée par sécurité (potentiellement destructrice) : {command}"
    if _is_risky(command) and params.get("_human_approval") is not _HUMAN_APPROVED:
        approved = dict(params)
        approved["command"] = command
        # Objet sentinelle non sérialisable et impossible à fabriquer dans un
        # appel d'outil Gemini : seul le callback du bouton peut l'injecter.
        approved["_human_approval"] = _HUMAN_APPROVED
        approved.pop("confirm", None)
        return human_confirmation.request(
            "shell:risky",
            "Exécuter une commande système sensible",
            f"{command}\n\nDossier : {cwd}",
            lambda p=approved, ui=player: run_shell(p, player=ui),
        )

    # Préparation VLC : média en chemin relatif depuis son dossier.
    if _first_binary(command) in ("vlc", "cvlc"):
        command = _prepare_vlc(command)

    # Détachement automatique des apps graphiques/lecteurs : un `mpv` ou un
    # `firefox` lancé ici bloquait l'assistant jusqu'au timeout, puis le
    # processus était tué. Basé sur le premier binaire réel (pas une
    # sous-chaîne : « improve » ne doit plus matcher « mpv »).
    detached = (
        command.rstrip().endswith("&")
        or "nohup" in command.split()[:1]
        or _is_gui_command(command)
    )

    if player:
        try:
            player.write_log(f"[shell_exec] $ {command}")
        except Exception:
            pass

    resolved_cwd = cwd if Path(cwd).exists() else str(Path.home())

    # Une commande qui attend sudo ou une confirmation de paquet doit posséder
    # un vrai TTY. Après confirmation de sécurité, elle vit indépendamment de
    # la carte « Commande en cours » et de la session vocale.
    if _needs_interactive_terminal(command):
        return _run_interactive(command, resolved_cwd)

    # Interception « find » → recherche floue (repli : find original).
    find_match = re.search(
        r"^find\s+(.+?)\s+(?:-type\s+[fd]\s+)?-(?:i)?name\s+['\"]?([^'\"]+)['\"]?",
        command,
    )
    if find_match:
        try:
            paths_str = find_match.group(1).strip()
            pattern = find_match.group(2).strip()
            paths_str = re.sub(r"-type\s+[fd]", "", paths_str)
            raw_paths = [p.strip() for p in paths_str.split()
                         if p.strip() and not p.startswith("-")]
            resolved_paths = []
            for p in raw_paths:
                resolved_p = Path(os.path.expanduser(p)).resolve()
                if resolved_p.exists():
                    resolved_paths.append(resolved_p)
            if not resolved_paths:
                resolved_paths = [Path.home()]
            query = pattern.replace("*", "").replace("?", "").strip()
            from actions.smart_search import smart_search_files
            matches = smart_search_files(query=query, search_paths=resolved_paths,
                                         max_results=15)
            if matches:
                stdout_lines = [str(path) for _score, path in matches]
                return "[exit 0]\n" + "\n".join(stdout_lines)
            return "[exit 0] (aucune sortie)"
        except Exception:
            pass  # smart_search absent/HS → on exécute le find original

    if detached:
        if not command.rstrip().endswith("&"):
            command = f"nohup {command} >/dev/null 2>&1 &"
        return _run_detached(command, resolved_cwd)

    return _run_normal(command, resolved_cwd, timeout)


# ════════════════════════════════════════════════════════════════════════════
# hyprctl bas niveau
# ════════════════════════════════════════════════════════════════════════════

def _hyprctl(*args: str) -> str:
    if not kit.which("hyprctl"):
        return "hyprctl introuvable — cette machine ne semble pas tourner sous Hyprland."
    env = _hypr_env()
    if not env.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return ("Impossible de trouver l'instance Hyprland en cours "
                "(HYPRLAND_INSTANCE_SIGNATURE introuvable). Vérifiez que "
                "l'assistant tourne bien dans la session Hyprland.")
    res = kit.run(["hyprctl", *args], timeout=8, retries=1, env=env)
    if res.timed_out:
        return "hyprctl n'a pas répondu — le compositeur est peut-être occupé."
    if not res.ok:
        return f"[hyprctl exit {res.code}] {res.err.strip() or res.out.strip() or 'erreur inconnue'}"
    kit.hypr_invalidate()
    return res.out.strip() or "OK"


def _ok(res: str) -> bool:
    low = (res or "").lower()
    return "exit" not in low and "error" not in low and "unknown" not in low


def _hyprctl_dispatch(legacy_cmd: str, legacy_args: str = "",
                      lua_cmd: str = "") -> str:
    """Dispatch robuste : syntaxe Lua (hyprctl >= 0.55) d'abord, repli sur
    la syntaxe legacy. L'ancienne version faisait l'inverse et envoyait la
    chaîne Lua comme nom de dispatcher en repli — forcément muet."""
    if lua_cmd:
        res = _hyprctl("dispatch", lua_cmd)
        if _ok(res):
            return res
    payload = f"{legacy_cmd} {legacy_args}".strip()
    return _hyprctl("dispatch", payload)


# ════════════════════════════════════════════════════════════════════════════
# Actions unitaires Hyprland
# ════════════════════════════════════════════════════════════════════════════

def _hypr_type(value: str) -> str:
    if not value:
        return "Rien à saisir."
    if kit.which("wtype"):
        # Le code de retour comptait : l'ancienne version annonçait la saisie
        # même quand wtype échouait (pas de compositeur, texte refusé).
        res = kit.run(["wtype", value], timeout=10, env=_hypr_env())
        if res.ok:
            return f"Texte saisi : {value[:60]}"
        return f"Échec de la saisie : {res.reason()}"
    return _hyprctl_dispatch(
        "keyword", "", f'hl.dsp.keyboard.type({{ text = {json.dumps(value)} }})')


def _hypr_keys(value: str) -> str:
    """« super+k », « ctrl shift t »… wtype d'abord, repli via
    `hyprctl dispatch keyword` (bind éphémère) si wtype est absent."""
    if not value:
        return "Aucun raccourci fourni."
    if kit.which("wtype"):
        keys = [k.strip() for k in value.replace("+", " ").split() if k.strip()]
        if not keys:
            return "Aucun raccourci fourni."
        cmd = ["wtype"]
        for k in keys[:-1]:
            cmd += ["-M", k]
        cmd += ["-k", keys[-1]]
        for k in reversed(keys[:-1]):
            cmd += ["-m", k]
        res = kit.run(cmd, timeout=5, env=_hypr_env())
        if res.ok:
            return f"Raccourci envoyé : {value}"
        return f"Échec du raccourci : {res.reason()}"
    combo = value.replace(" ", "_").upper()
    res = _hyprctl("dispatch", "keyword",
                   f"bind,{combo},exec,")
    return f"Raccourci envoyé via Hyprland : {combo}" if _ok(res) else res


def _hypr_clipboard_set(value: str, primary: bool = False) -> str:
    if not kit.which("wl-copy"):
        return "wl-copy introuvable (installez 'wl-clipboard')."
    cmd = ["wl-copy"]
    if primary:
        cmd.append("--primary")
    # wl-copy se démonise pour servir le presse-papiers : capturer sa sortie
    # ferait attendre jusqu'au délai une copie déjà effectuée.
    res = kit.run(cmd, stdin=value, timeout=5, env=_hypr_env(), capture=False)
    if not res.ok:
        return f"Échec de la copie : {res.reason()}"
    return ("Copié dans la sélection primaire." if primary
            else "Copié dans le presse-papiers.")


def _hypr_clipboard_get(primary: bool = False) -> str:
    if not kit.which("wl-paste"):
        return "wl-paste introuvable (installez 'wl-clipboard')."
    cmd = ["wl-paste", "--no-newline"]
    if primary:
        cmd.append("--primary")
    res = kit.run(cmd, timeout=5, env=_hypr_env())
    if not res.ok and res.timed_out:
        return "Échec de la lecture : le presse-papiers n'a pas répondu."
    return res.out.strip() or "(presse-papiers vide)"


def _hypr_screenshot(value: str = "", target: str = "") -> str:
    """Capture grim. target : 'clipboard' → wl-copy ; 'zone' → sélection
    slurp ; sinon chemin PNG (défaut ~/Pictures).

    La sélection de zone était inopérante : `subprocess.run(["grim", "-g",
    "$(slurp)", path], shell=True)` ne transmet au shell que le premier
    élément de la liste — seul `grim` s'exécutait, sans argument, et l'action
    annonçait malgré tout un fichier qui n'existait pas. slurp est désormais
    lancé séparément et sa géométrie passée à grim.
    """
    if not kit.which("grim"):
        return "grim introuvable (installez 'grim', et 'slurp' pour la sélection de zone)."
    env = _hypr_env()
    mode = (target or "").strip().lower()

    if mode == "clipboard":
        if not kit.which("wl-copy"):
            return "wl-copy introuvable (installez 'wl-clipboard')."
        # Le PNG ne doit jamais transiter par une chaîne de caractères : le
        # tube reste dans le shell, et le socle borne l'ensemble (le groupe
        # entier est tué au délai, grim comme wl-copy).
        res = kit.run("grim - | wl-copy --type image/png",
                      shell=True, timeout=15, env=env)
        return ("Capture copiée dans le presse-papiers." if res.ok
                else f"Échec de la capture : {res.reason()}")

    out_path = value or str(Path.home() / "Pictures" /
                            f"screenshot_{int(time.time())}.png")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)

    if mode in ("zone", "region", "selection"):
        if not kit.which("slurp"):
            return "slurp introuvable (installez 'slurp')."
        # slurp attend le tracé de l'utilisateur : délai large, mais borné.
        sel = kit.run(["slurp"], timeout=60, env=env)
        if not sel.ok or not sel.out.strip():
            return "Sélection annulée."
        shot = kit.run(["grim", "-g", sel.out.strip(), out_path],
                       timeout=15, env=env)
    else:
        shot = kit.run(["grim", out_path], timeout=10, env=env)

    if not shot.ok:
        return f"Échec de la capture : {shot.reason()}"
    if not Path(out_path).exists():
        return "La capture n'a pas produit de fichier."
    return f"Capture enregistrée : {out_path}"


# ════════════════════════════════════════════════════════════════════════════
# hypr_control — contrôle du bureau Hyprland
# ════════════════════════════════════════════════════════════════════════════

@kit.action("hypr_control")
def hypr_control(parameters=None, player=None, **_kwargs) -> str:
    """
    Contrôle du bureau Hyprland natif Wayland.
    Actions : workspace, next_workspace, move_to_workspace, focus_app,
              close_active, fullscreen, float, pin, list_windows,
              resize, move_window, type, keys,
              clipboard_set, clipboard_get, screenshot.
    value   : cible (numéro/ordinal de bureau, texte, raccourci, chemin…)
    target  : optionnel (screenshot : 'clipboard' | 'zone' | chemin ;
              resize : 'up|down|left|right' ; clipboard : 'primary').
    """
    params = parameters or {}
    action = (params.get("action") or "").strip().lower()
    value = (params.get("value") or "").strip()
    target = (params.get("target") or "").strip()

    # Les bureaux acceptent chiffres ET ordinaux (« deuxième bureau »).
    ws_value = _parse_workspace_value(value) if action in (
        "workspace", "move_to_workspace") else None

    if action == "workspace":
        return _hyprctl_dispatch(
            "workspace", ws_value or value or "1",
            f'hl.dsp.focus({{ workspace = "{ws_value or value or 1}" }})')
    if action == "next_workspace":
        return _hyprctl_dispatch(
            "workspace", value or "e+1",
            f'hl.dsp.focus({{ workspace = "{value or "e+1"}" }})')
    if action == "move_to_workspace":
        return _hyprctl_dispatch(
            "movetoworkspace", ws_value or value or "1",
            f'hl.dsp.window.move({{ workspace = "{ws_value or value or 1}" }})')
    if action == "focus_app":
        return _hyprctl_dispatch(
            "focuswindow", f"class:(?i){value}" if value else "",
            f'hl.dsp.focus({{ window = "class:(?i){value}" }})' if value else "")
    if action == "close_active":
        return _hyprctl_dispatch("killactive", "", "hl.dsp.window.close()")
    if action == "fullscreen":
        return _hyprctl_dispatch("fullscreen", "1",
                                 'hl.dsp.window.fullscreen({ action = "toggle" })')
    if action == "float":
        return _hyprctl_dispatch("togglefloating", "",
                                 'hl.dsp.window.float({ action = "toggle" })')
    if action == "pin":
        return _hyprctl_dispatch("pin", "",
                                 'hl.dsp.window.pin({ action = "toggle" })')
    if action == "list_windows":
        return _hyprctl("clients")
    if action == "resize":
        direction = (target or value or "right").strip().lower()
        if direction not in ("up", "down", "left", "right"):
            direction = "right"
        return _hyprctl_dispatch(
            "resizewindow", direction,
            f'hl.dsp.window.resize({{ direction = "{direction}" }})')
    if action == "move_window":
        direction = (value or "right").strip().lower()
        if direction not in ("up", "down", "left", "right"):
            direction = "right"
        return _hyprctl_dispatch(
            "movewindow", direction,
            f'hl.dsp.window.move({{ direction = "{direction}" }})')
    if action == "type":
        return _hypr_type(value)
    if action == "keys":
        return _hypr_keys(value)
    if action == "clipboard_set":
        return _hypr_clipboard_set(value, primary=(target.lower() == "primary"))
    if action == "clipboard_get":
        return _hypr_clipboard_get(primary=(target.lower() == "primary"))
    if action == "screenshot":
        return _hypr_screenshot(value, target)
    return f"Action Hyprland inconnue : {action}"


# ════════════════════════════════════════════════════════════════════════════
# Parsing local — shell & Hyprland
# ════════════════════════════════════════════════════════════════════════════

def _clean_cmd(cmd: str) -> str:
    cmd = re.sub(r"\s+", " ", cmd or "").strip().strip("'\"` ")
    return cmd


# (pattern, action Hyprland, regex d'extraction de la valeur)
_HYPR_PATTERNS = [
    (r"d[ée]place\s+(?:cette\s+)?(?:la\s+)?fen[êe]tre[^\n]*?\b(?:vers|au|sur|dans)\b[^\n]*", "move_to_workspace", None),
    (r"envoie\s+(?:cette\s+)?(?:la\s+)?fen[êe]tre[^\n]*?\b(?:vers|au|sur|dans)\b[^\n]*", "move_to_workspace", None),
    (r"(?:passe|va|aller)\s+(?:au|sur|vers)\s+(?:bureau|workspace)[^\n]*?(suivant|pr[ée]c[ée]dent)", "next_workspace", None),
    (r"(?:passe|va|aller)\s+(?:au|sur|vers)\s+(?:bureau|workspace)\s+(.+)", "workspace", None),
    (r"(?:change|passe)[^\n]*?workspace\s+(.+)", "workspace", None),
    (r"workspace\s+(\S+)", "workspace", None),
    (r"ferme\s+(?:cette\s+)?(?:la\s+)?fen[êe]tre(?:\s+active)?", "close_active", None),
    (r"plein\s*[ée]cran|fullscreen", "fullscreen", None),
    (r"(?:mode\s+)?flottant", "float", None),
    (r"[ée]pingle\s+(?:cette\s+)?(?:la\s+)?fen[êe]tre", "pin", None),
    (r"(?:liste|affiche)\s+(?:les\s+)?fen[êe]tres|fen[êe]tres\s+ouvertes", "list_windows", None),
    (r"(?:tape|[ée]cris|saisis)\s+(.+)", "type", None),
    (r"(?:envoie|fais|simule)\s+(?:le\s+)?raccourci\s+(.+)", "keys", None),
    (r"raccourci\s+(.+)", "keys", None),
    (r"(?:copie|mets?)\s+(?:dans\s+le|au)\s+presse[- ]papiers?\s+(.+)", "clipboard_set", None),
    (r"(?:lis|lis-moi|affiche|montre)\s+(?:le\s+)?presse[- ]papiers?", "clipboard_get", None),
    (r"(?:fais\s+)?(?:une\s+)?capture\s+d['’]?[ée]cran\s*(?:de\s+)?(?:la\s+)?zone\s*(.*)", "screenshot", "zone"),
    (r"(?:fais\s+)?(?:une\s+)?capture\s+d['’]?[ée]cran\s*(.*)", "screenshot", None),
    (r"screenshot\s*(.*)", "screenshot", None),
    (r"(?:focus|mets\s+le\s+focus\s+sur|active\s+la\s+fen[êe]tre)\s+(.+)", "focus_app", None),
]

_COMMON_SHELL_BINARIES = (
    "ls|cd|cat|echo|mkdir|rmdir|rm|cp|mv|grep|egrep|find|ps|top|htop|df|du|"
    "free|uname|which|whereis|whoami|pwd|touch|chmod|chown|ln|tar|gzip|gunzip|"
    "zip|unzip|curl|wget|ping|ip|nmcli|bluetoothctl|pactl|playerctl|"
    "systemctl|journalctl|pacman|yay|paru|git|docker|podman|python|python3|"
    "node|npm|cargo|make|cmake|kill|pkill|pgrep|sed|awk|sort|uniq|head|tail|"
    "wc|date|cal|uptime|ssh|scp|rsync|mount|umount|lsblk|lspci|lsusb|"
    "hyprctl|wl-copy|wl-paste|grim|slurp|wtype|yt-dlp|mpv|vlc"
)
_SHELL_CMD_RE = re.compile(rf"^({_COMMON_SHELL_BINARIES})(\s.*)?$")
_SHELL_TRIGGER_RES = [
    r"^(?:ex[ée]cute|ex[ée]cuter|lance|lancer|run|fais|effectue|tape)\s+(?:la\s+commande\s+)?['\"`]?(.+?)['\"`]?$",
    r"^(?:commande|shell|cmd)\s*:\s*(.+)$",
    r"^['\"`](.+)['\"`]$",
]


def _parse_shell_request_locally(text: str) -> Optional[Dict[str, Any]]:
    """Détecte si la phrase est une commande shell ou une action Hyprland.
    Retourne {'target': 'shell'|'hypr', …} ou None (→ repli IA)."""
    text = re.sub(r"\s+", " ", (text or "").strip().lower())
    text = re.sub(
        r"\b(s'il te pla[iî]t|stp|please|peux[- ]tu|tu peux|tu pourrais|"
        r"je veux|j'aimerais|est[- ]ce que tu peux)\b",
        "", text).strip()
    if not text:
        return None

    # 1. Actions Hyprland
    for pattern, action, extra in _HYPR_PATTERNS:
        m = re.search(pattern, text)
        if not m:
            continue
        value = None
        if action == "next_workspace":
            word = (m.group(1) or "").lower()
            value = "e-1" if re.search(r"pr[ée]c[ée]dent", word) else "e+1"
        elif m.groups() and m.group(1):
            value = m.group(1).strip()
        if action == "screenshot":
            target = "zone" if (extra == "zone" or
                                re.search(r"\bzone\b|\bs[ée]lection\b", text)) else ""
            if value and re.search(r"\b(copie|presse[- ]papiers?)\b", value):
                target = "clipboard"
                value = ""
            return {"target": "hypr", "action": action,
                    "value": _clean_cmd(value or ""), "target_extra": target}
        if action == "type":
            return {"target": "hypr", "action": action,
                    "value": (text or "").strip()}
        return {"target": "hypr", "action": action, "value": value}

    # 2. Commandes shell explicites ou évidentes
    for trig in _SHELL_TRIGGER_RES:
        m = re.search(trig, text)
        if m:
            cmd = _clean_cmd(m.group(1))
            if cmd:
                return {"target": "shell", "command": cmd}
    if _SHELL_CMD_RE.match(text):
        return {"target": "shell", "command": _clean_cmd(text)}
    return None


def _detect_intent_ai(description: str) -> Optional[Dict]:
    """Repli IA (Gemini) pour les demandes ambiguës."""
    api_key = _get_api_key()
    if not api_key:
        return None
    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        prompt = (
            "Analyse la phrase suivante et détermine si l'utilisateur veut "
            "exécuter une commande shell ou une action de contrôle de bureau "
            "Hyprland. Retourne UNIQUEMENT un JSON.\n"
            "Si c'est une commande shell : {\"target\": \"shell\", \"command\": \"...\"}\n"
            "Si c'est une action Hyprland : {\"target\": \"hypr\", \"action\": \"...\", \"value\": \"...\"}\n"
            "Actions Hyprland possibles : workspace, next_workspace, "
            "move_to_workspace, close_active, fullscreen, float, pin, "
            "list_windows, type, keys, clipboard_set, clipboard_get, "
            "screenshot, focus_app, resize, move_window.\n"
            f"Phrase : \"{description}\"\n"
            "Réponds uniquement avec le JSON."
        )
        resp = client.models.generate_content(model=FAST_MODEL,
                                              contents=prompt)
        json_match = re.search(r"\{.*\}", resp.text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group(0))
    except Exception as e:
        print(f"[ShellExec] Erreur IA : {e}")
    return None


# ════════════════════════════════════════════════════════════════════════════
# Point d'entrée unifié
# ════════════════════════════════════════════════════════════════════════════

@kit.action("shell_exec")
def shell_exec(parameters: dict = None, player=None,
               session_memory=None, speak=None) -> str:
    """
    Interprète une demande en langage naturel et la redirige vers run_shell
    ou hypr_control selon l'intention.
    Paramètres acceptés :
      description : phrase naturelle
      command     : commande shell explicite
      action      : action Hyprland explicite
      value       : valeur associée à l'action Hyprland
      target      : optionnel (screenshot / resize / clipboard)
    """
    params = parameters or {}
    description = (params.get("description") or "").strip()

    if description:
        if _HAS_DEVSECOPS_MODULES:
            sb_intent = parse_second_brain_intent(description)
            if sb_intent:
                return second_brain_action(parameters=params, player=player)
            hypr_intent = parse_hypr_orchestrator_intent(description)
            if hypr_intent:
                return hypr_orchestrator_control(parameters=params, player=player)
            devsec_intent = parse_devsecops_intent(description)
            if devsec_intent:
                return devsecops_control(parameters=params, player=player)

        local = _parse_shell_request_locally(description)
        if not local:
            local = _detect_intent_ai(description)
        if local:
            target = local.get("target")
            if target == "shell":
                params["command"] = local.get("command", "")
                return run_shell(params, player=player)
            if target == "hypr":
                params["action"] = local.get("action", "")
                params["value"] = local.get("value", "") or ""
                if local.get("target_extra"):
                    params["target"] = local["target_extra"]
                return hypr_control(params, player=player)
        # Rien compris : on tente quand même la commande shell verbatim.
        params["command"] = params.get("command") or description
        return run_shell(params, player=player)

    if params.get("command"):
        return run_shell(params, player=player)
    if params.get("action"):
        return hypr_control(params, player=player)
    return "Aucune commande ou action demandée."


# ════════════════════════════════════════════════════════════════════════════
# Test direct
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        print(shell_exec({"description": " ".join(sys.argv[1:])}))
    else:
        print("Usage: shell_exec.py <phrase ou commande>")
