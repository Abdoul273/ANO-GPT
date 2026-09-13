#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
open_app.py — Lanceur d'applications ultra-robuste pour Jarvis.
Optimisé Arch Linux / Hyprland (Wayland), multi-OS conservé.

Capacités :
- ouvre une application, un fichier ou un dossier avec le bon programme ;
- quantité : « ouvre 2 fenêtres de kitty » (paramètre count ou parsing) ;
- bureau cible : chiffres ET ordinaux (« bureau 4 », « deuxième bureau »,
  « workspace n°3 »), déplacement silencieux de la fenêtre sans voler le
  focus, avec repli si la classe ne correspond pas au nom demandé ;
- surnoms d'instances (« appelle-le main-term ») via window_instances ;
- recherche de fichiers locale sûre (profondeur limitée, symlinks ignorés,
  scoring) au lieu d'un rglob illimité sur tout $HOME ;
- commandes composées gérées (« libreoffice --writer », « gtk-launch … ») ;
- restauration DISPLAY/WAYLAND_DISPLAY depuis les processus de la session ;
- journalisation launch_tracker pour le ciblage « celle que tu viens
  d'ouvrir » dans close_app.
"""
import os
import platform
import re
import json
import shlex
import subprocess
import time
import unicodedata
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple, Set

from core import action_kit as kit
from core.live_model_policy import FAST_MODEL

try:
    import psutil
    _PSUTIL = True
except ImportError:
    _PSUTIL = False

try:
    from actions.desktop_apps import find_app as _find_desktop_app
except ImportError:
    _find_desktop_app = None

try:
    from actions.computer_control import computer_control
except ImportError:
    computer_control = None

_SYSTEM = platform.system()  # "Linux" | "Darwin" | "Windows"
_WAYLAND = bool(os.environ.get("WAYLAND_DISPLAY"))
DEVNULL = subprocess.DEVNULL


# ════════════════════════════════════════════════════════════════════════════
# Environnement d'affichage (Wayland/X11) restauré depuis la session
# ════════════════════════════════════════════════════════════════════════════

def _restore_display_env(env: dict) -> None:
    """Si le processus Jarvis n'a ni DISPLAY ni WAYLAND_DISPLAY (service
    systemd, ssh…), on les récupère depuis un processus de la session
    graphique de l'utilisateur."""
    if _SYSTEM != "Linux":
        return
    if env.get("WAYLAND_DISPLAY") or env.get("DISPLAY"):
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
                for var in ("WAYLAND_DISPLAY", "DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR"):
                    if var in proc_env and not env.get(var):
                        env[var] = proc_env[var]
                break
        except Exception:
            continue


def _linux_env() -> Optional[dict]:
    if _SYSTEM != "Linux":
        return None
    env = {**os.environ}
    _restore_display_env(env)
    return env


try:
    from actions.window_instances import (
        register_instance,
        title_flag_for as _title_flag_for,
        dispatch_hyprland as _hypr_dispatch_hyprland,
        move_window_to_workspace as _hypr_move_window_to_workspace,
    )
    _HAS_WINDOW_INSTANCES = True
except ImportError:
    _HAS_WINDOW_INSTANCES = False

try:
    from actions import launch_tracker as _tracker
    _HAS_TRACKER = True
except ImportError:
    _HAS_TRACKER = False


# ════════════════════════════════════════════════════════════════════════════
# Surnoms d'instances
# ════════════════════════════════════════════════════════════════════════════

def _extract_instance_name(description: str) -> Tuple[str, Optional[str]]:
    """Retire et retourne un surnom d'instance depuis une phrase naturelle.
    Ex: 'ouvre kitty et appelle-le main-term' -> ('ouvre kitty', 'main-term')."""
    patterns = [
        r"\s*(?:et\s+)?(?:appelle[- ]?(?:le|la)|nomme[- ]?(?:le|la)|sous le nom de?)\s+['\"«]?([\w-]+)['\"»]?\s*$",
        r"\s*(?:(?:nom|surnom|instance)\s*[:=]\s*['\"«]?([\w-]+)['\"»]?)\s*$",
    ]
    for pat in patterns:
        m = re.search(pat, description, re.IGNORECASE)
        if m:
            name = m.group(1).strip()
            return description[:m.start()].strip(), name
    return description, None


# ════════════════════════════════════════════════════════════════════════════
# Hyprland : helpers
# ════════════════════════════════════════════════════════════════════════════

def _hyprctl_json(*args):
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


def _focus_workspace(ws_num: int) -> None:
    """Bascule sur le bureau demandé avant lancement. Le module
    window_instances est privilégié ; repli direct sur hyprctl."""
    try:
        if _HAS_WINDOW_INSTANCES:
            _hypr_dispatch_hyprland(
                legacy_cmd="workspace",
                legacy_args=str(ws_num),
                lua_cmd=f'hl.dsp.focus({{ workspace = "{ws_num}" }})',
            )
        else:
            _hypr_dispatch("workspace", str(ws_num))
    except Exception as e:
        print(f"[open_app] Erreur lors du switch de workspace : {e}")


def _focus_window(target_window: str) -> bool:
    """Focalise la fenêtre cible (adresse ou nom). Le module
    window_instances est privilégié ; repli direct sur hyprctl."""
    if not target_window:
        return False
    if _HAS_WINDOW_INSTANCES:
        try:
            from actions.window_instances import focus_window as _wi_focus_window
            if _wi_focus_window(target_window):
                return True
        except Exception:
            pass
    arg = target_window
    if not arg.startswith("address:") and arg.startswith("0x"):
        arg = f"address:{arg}"
    return _hypr_dispatch("focuswindow", arg)


def _window_ready(target_window: str, timeout: float = 3.5) -> bool:
    """Attend qu'une fenêtre soit réellement prête à recevoir du texte.

    Remplace une attente fixe de 3,5 secondes payée à chaque « ouvre X et
    tape Y » : la plupart des applications sont prêtes en quelques centaines
    de millisecondes, et l'assistant restait muet tout ce temps. La sonde
    s'arrête dès que la fenêtre est mappée et dimensionnée ; si Hyprland n'est
    pas là pour répondre, on retombe sur l'attente d'origine.
    """
    if _SYSTEM != "Linux" or not kit.which("hyprctl"):
        time.sleep(min(timeout, 1.5))
        return False

    addr = target_window[len("address:"):] if target_window.startswith("address:") else ""
    needle = "" if addr else (target_window or "").lower().strip()

    def _ready() -> bool:
        for c in (kit.hypr_json("clients", default=[], ttl=0) or []):
            if addr:
                if c.get("address") != addr:
                    continue
            elif needle:
                blob = " ".join(str(c.get(k) or "") for k in
                                ("class", "initialClass", "title")).lower()
                if needle not in blob:
                    continue
            else:
                continue
            size = c.get("size") or [0, 0]
            if c.get("mapped", True) and len(size) == 2 and size[0] > 1 and size[1] > 1:
                return True
        return False

    ready = kit.wait_until(_ready, timeout=timeout, interval=0.08, max_interval=0.25)
    # Une fenêtre mappée n'a pas toujours fini de construire son champ de
    # saisie : un souffle court évite d'écrire dans le vide.
    time.sleep(0.25 if ready else 0.0)
    return ready


def _do_move_window(addr: str, workspace: int) -> bool:
    if _HAS_WINDOW_INSTANCES:
        try:
            _hypr_move_window_to_workspace(f"address:{addr}", workspace, follow=False)
            return True
        except Exception:
            pass
    return _hypr_dispatch("movetoworkspacesilent", f"{workspace},address:{addr}")


def _move_new_window_to_workspace(app_name: str, workspace: int,
                                  before_addrs: set, timeout: float = 4.0) -> bool:
    """Attend qu'une nouvelle fenêtre apparaisse après le lancement et la
    déplace silencieusement vers le bureau demandé, sans changer le focus.
    Préfère une fenêtre dont classe/titre correspond au nom demandé, puis
    se rabat sur la première nouvelle fenêtre (les classes ne collent pas
    toujours au nom d'usage, ex: alias ou apps Electron)."""
    if _SYSTEM != "Linux" or not kit.which("hyprctl"):
        return False
    needle = (app_name or "").lower()
    deadline = time.monotonic() + timeout
    named_deadline = time.monotonic() + max(1.5, timeout * 0.6)
    fallback_addr = None
    while time.monotonic() < deadline:
        clients = _hyprctl_json("clients") or []
        new = [c for c in clients
               if c.get("address") and c["address"] not in before_addrs]
        if needle:
            for c in new:
                cls = (c.get("class") or "").lower()
                title = (c.get("title") or "").lower()
                if needle in cls or needle in title:
                    return _do_move_window(c["address"], workspace)
        if fallback_addr is None and new:
            fallback_addr = new[0]["address"]
        if fallback_addr and time.monotonic() >= named_deadline:
            return _do_move_window(fallback_addr, workspace)
        time.sleep(0.15)
    if fallback_addr:
        return _do_move_window(fallback_addr, workspace)
    return False


def _wait_for_new_window(app_name: str, before_addrs: Set[str], timeout: float = 6.0) -> Optional[dict]:
    """Interroge _hyprctl_json("clients"), extrait les clients apparus depuis before_addrs,
    sélectionne en priorité le client correspondant à l'application ou le premier nouveau client."""
    if _SYSTEM != "Linux" or not kit.which("hyprctl"):
        return None
    needle = (app_name or "").lower().strip()
    deadline = time.monotonic() + timeout
    named_deadline = time.monotonic() + max(1.5, timeout * 0.6)
    fallback_client = None
    polls = 0
    max_polls = max(10, int(timeout / 0.1))
    while time.monotonic() < deadline and polls < max_polls:
        polls += 1
        clients = _hyprctl_json("clients") or []
        new = [c for c in clients if c.get("address") and c["address"] not in before_addrs]
        if needle:
            for c in new:
                cls = (c.get("class") or "").lower()
                title = (c.get("title") or "").lower()
                initial_cls = (c.get("initialClass") or "").lower()
                initial_title = (c.get("initialTitle") or "").lower()
                if needle in cls or needle in title or needle in initial_cls or needle in initial_title:
                    return c
        if fallback_client is None and new:
            fallback_client = new[0]
        if fallback_client and (time.monotonic() >= named_deadline or polls >= 5):
            return fallback_client
        time.sleep(0.1)
    if fallback_client:
        return fallback_client
    return None


def _verify_hidden(before_addrs: set, timeout: float = 1.5) -> bool:
    """Relit l'état réel Hyprland : une fenêtre apparue depuis `before_addrs`
    est-elle vraiment sur le bureau spécial caché ? Un dispatch hyprctl
    répondant « ok » ne garantit pas que la fenêtre y a atterri."""
    if _SYSTEM != "Linux" or not kit.which("hyprctl"):
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        clients = _hyprctl_json("clients") or []
        for c in clients:
            if c.get("address") in before_addrs:
                continue
            ws_name = ((c.get("workspace") or {}).get("name") or "")
            if "special:hidden" in ws_name or ws_name == "special:hidden":
                return True
        time.sleep(0.15)
    return False


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

_WS_MENTION_PATTERNS = [
    rf"{_PREP}{_WS}\s*(?:n[°o]?\s*|num[ée]ro\s+)?\d+",
    rf"{_PREP}\d+\s*(?:er|ère|ere|[èe]me|eme|e)?\s*{_WS}",
    rf"{_PREP}(?:{_ORD_FR})\s*{_WS}",
    rf"{_PREP}{_WS}\s*(?:n[°o]?\s*|num[ée]ro\s+)?(?:{_ORD_FR})",
]


def _ordinal_to_int(word: str) -> Optional[int]:
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


def _extract_workspace(text: str) -> Tuple[Optional[int], str]:
    """Retourne (numéro de workspace, texte nettoyé de la mention)."""
    if not text:
        return None, text or ""
    ws = None
    m = re.search(rf"{_PREP}{_WS}\s*(?:n[°o]?\s*|num[ée]ro\s+)?(\d+)", text, re.I)
    if m:
        ws = int(m.group(1))
    if ws is None:
        m = re.search(rf"{_PREP}(\d+)\s*(?:er|ère|ere|[èe]me|eme|e)?\s*{_WS}", text, re.I)
        if m:
            ws = int(m.group(1))
    if ws is None:
        m = re.search(rf"{_PREP}({_ORD_FR})\s*{_WS}", text, re.I)
        if m:
            ws = _ordinal_to_int(m.group(1))
    if ws is None:
        m = re.search(rf"{_PREP}{_WS}\s*(?:n[°o]?\s*|num[ée]ro\s+)?({_ORD_FR})", text, re.I)
        if m:
            ws = _ordinal_to_int(m.group(1))
    if ws is None:
        return None, text
    cleaned = text
    for pat in _WS_MENTION_PATTERNS:
        cleaned = re.sub(pat, " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.;:")
    return ws, cleaned


# ════════════════════════════════════════════════════════════════════════════
# Extraction de quantité (« ouvre 2 fenêtres de kitty »)
# ════════════════════════════════════════════════════════════════════════════

_COUNT_WORDS = {"deux": 2, "trois": 3, "quatre": 4, "cinq": 5, "six": 6,
                "sept": 7, "huit": 8, "neuf": 9, "dix": 10}

_RE_COUNT = re.compile(
    r"\b(\d+)\s*(?:fen[êe]tres?|fentres?|fenetres?|instances?|copies?|onglets?|fois)\b"
    r"|\b(deux|trois|quatre|cinq|six|sept|huit|neuf|dix)\s+"
    r"(?:fen[êe]tres?|fentres?|instances?|copies?|onglets?)\b",
    re.IGNORECASE,
)


def _extract_count(text: str) -> Tuple[Optional[int], str]:
    if not text:
        return None, text or ""
    m = _RE_COUNT.search(text)
    if not m:
        return None, text
    count = int(m.group(1)) if m.group(1) else _COUNT_WORDS.get(m.group(2).lower())
    cleaned = text[:m.start()] + " " + text[m.end():]
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.;:")
    return (count or 1), cleaned


# ════════════════════════════════════════════════════════════════════════════
# Processus : détection et attente de lancement
# ════════════════════════════════════════════════════════════════════════════

_MEDIA_EXTS = {
    ".mp3", ".wav", ".flac", ".aac", ".ogg", ".opus", ".m4a", ".wma",
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".wmv", ".flv",
}

# Les notes Markdown sont un format de travail à part entière dans ANO-GPT :
# lorsqu'aucune application n'est demandée, elles doivent toujours s'ouvrir
# dans Markdown Studio, indépendamment de l'association MIME de xdg-open.
_MARKDOWN_STUDIO_SUFFIXES = {".md"}
_MARKDOWN_STUDIO_COMMAND = "markdown-studio"


def _norm_text(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[\s_.\-()\[\]]+", " ", s.lower()).strip()


def _proc_names(app_name: str) -> Set[str]:
    names = {app_name.lower()}
    normalized = _normalize(app_name).lower()
    if normalized:
        names.add(normalized)
        names.add(normalized.split()[0])
    for part in (app_name, normalized):
        stem = Path(part.split()[0]).stem.lower() if part else ""
        if stem:
            names.add(stem)
    if "vlc" in names:
        names.add("vlc")
    if "google-chrome" in names or "chrome" in names:
        names.update({"chrome", "google-chrome", "chrome.exe"})
    return names


def _is_process_running(app_name: str) -> bool:
    if not _PSUTIL:
        return False
    targets = _proc_names(app_name)
    my_pid = os.getpid()
    try:
        for proc in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
            if proc.pid == my_pid:
                continue
            try:
                tokens = {(proc.info.get("name") or "").lower()}
                exe_name = Path(proc.info.get("exe") or "").name.lower()
                if exe_name:
                    tokens.add(exe_name)
                for arg in proc.info.get("cmdline") or []:
                    tokens.add(Path(str(arg)).name.lower())
                if targets & tokens:
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:
        return False
    return False


def _snapshot_pids() -> Set[int]:
    if not _PSUTIL:
        return set()
    try:
        return set(psutil.pids())
    except Exception:
        return set()


def _wait_for_launch(app_name: str, before_pids: Optional[Set[int]] = None,
                     timeout: float = 4.0) -> bool:
    deadline = time.time() + timeout
    before_pids = before_pids or set()
    while time.time() < deadline:
        if app_name and _is_process_running(app_name):
            return True
        if _PSUTIL:
            try:
                current = set(psutil.pids())
                if current - before_pids:
                    return True
            except Exception:
                pass
        time.sleep(0.25)
    return False


def _popen_detached(argv: List[str], cwd: Optional[str] = None) -> subprocess.Popen:
    """Lance un processus complètement détaché du processus parent."""
    proc = subprocess.Popen(
        argv,
        stdin=DEVNULL,
        stdout=DEVNULL,
        stderr=DEVNULL,
        cwd=cwd,
        start_new_session=True,
        close_fds=True,
        env=_linux_env(),
    )
    # Le processus est volontairement détaché : on ne l'attend jamais.
    proc.__del__ = lambda self=proc: None  # anti ResourceWarning best-effort
    return proc


def _confirm_started(proc: subprocess.Popen, settle: float = 0.4) -> bool:
    """Vérifie que le processus a démarré : s'il tient plus de `settle`
    secondes il est lancé ; s'il sort avant, on exige un code 0 (cas des
    clients multi-fenêtres comme kitty ou des wrappers gtk-launch)."""
    deadline = time.monotonic() + settle
    while time.monotonic() < deadline:
        rc = proc.poll()
        if rc is not None:
            return rc == 0
        time.sleep(0.05)
    return True


def _split_command(cmd: str) -> List[str]:
    try:
        return shlex.split(cmd)
    except ValueError:
        return cmd.split()


def _command_argv(name: str) -> Optional[List[str]]:
    """Transforme un nom d'app éventuellement composé (« libreoffice
    --writer », « gtk-launch org.gnome.Lollypop », « kitty ») en argv
    exécutable, en vérifiant que le binaire existe."""
    if not name:
        return None
    argv = _split_command(name)
    if not argv:
        return None
    exe = kit.which(argv[0]) or kit.which(argv[0].lower())
    if not exe:
        return None
    return [exe] + argv[1:]


# ════════════════════════════════════════════════════════════════════════════
# Recherche de fichiers locale (sûre : profondeur limitée, pas de symlinks)
# ════════════════════════════════════════════════════════════════════════════

def _candidate_dirs() -> List[Path]:
    home = Path.home()
    dirs: List[Path] = []
    if _SYSTEM == "Linux":
        for var in ("XDG_MUSIC_DIR", "XDG_DOWNLOAD_DIR", "XDG_VIDEOS_DIR",
                    "XDG_DESKTOP_DIR", "XDG_DOCUMENTS_DIR"):
            v = os.environ.get(var, "")
            if v:
                p = Path(v).expanduser()
                try:
                    if p.is_dir():
                        dirs.append(p)
                except OSError:
                    pass
    names = ["Music", "Musique", "Downloads", "Téléchargements",
             "Telechargements", "Videos", "Vidéos", "Desktop", "Bureau",
             "Documents"]
    dirs += [home / n for n in names]
    dirs.append(home)
    out: List[Path] = []
    seen: Set[Path] = set()
    for d in dirs:
        try:
            if d.is_dir():
                rp = d.resolve()
                if rp not in seen:
                    seen.add(rp)
                    out.append(d)
        except OSError:
            continue
    return out


def _walk(base: Path, max_depth: int = 5):
    """Parcours sûr : ignore les symlinks, garde-fou de profondeur et
    d'erreurs de permission."""
    stack: List[Tuple[Path, int]] = [(base, 0)]
    seen: Set[Path] = set()
    try:
        seen.add(base.resolve())
    except OSError:
        return
    while stack:
        d, depth = stack.pop()
        if depth > max_depth:
            continue
        try:
            entries = sorted(d.iterdir())
        except (PermissionError, OSError):
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_file():
                    yield entry, depth
                elif entry.is_dir():
                    rp = entry.resolve()
                    if rp not in seen:
                        seen.add(rp)
                        stack.append((entry, depth + 1))
            except OSError:
                continue


def _resolve_target(raw: str) -> Optional[Path]:
    raw = (raw or "").strip().strip("'\"")
    if not raw:
        return None
    shortcuts = {
        "home": Path.home(),
        "music": Path.home() / "Music",
        "musique": Path.home() / "Musique",
        "downloads": Path.home() / "Downloads",
        "téléchargements": Path.home() / "Téléchargements",
        "telechargements": Path.home() / "Téléchargements",
        "videos": Path.home() / "Videos",
        "vidéos": Path.home() / "Vidéos",
        "bureau": Path.home() / "Bureau",
        "desktop": Path.home() / "Desktop",
        "documents": Path.home() / "Documents",
    }
    low = raw.lower()
    if low in shortcuts:
        return shortcuts[low].expanduser()
    direct = Path(raw).expanduser()
    if direct.exists():
        return direct

    # Recherche fuzzy bornée (30 000 entrées max, profondeur 5).
    nraw = _norm_text(raw)
    matches: List[Tuple[float, Path]] = []
    scanned = 0
    for base in _candidate_dirs():
        for item, _depth in _walk(base, max_depth=5):
            scanned += 1
            if scanned > 30000:
                break
            try:
                name_l = item.name.lower()
                if low in name_l or low == item.stem.lower():
                    matches.append((1.0, item))
                elif nraw and nraw in _norm_text(item.stem):
                    matches.append((0.8, item))
            except OSError:
                continue
            if len(matches) >= 12:
                break
        if len(matches) >= 12 or scanned > 30000:
            break
    if not matches:
        return None
    media = [(s, p) for s, p in matches if p.suffix.lower() in _MEDIA_EXTS]
    pool = media or matches
    pool.sort(key=lambda t: -t[0])
    return pool[0][1]


def _open_path_default(path: Path) -> bool:
    from core.browser_policy import WEB_DOCUMENT_SUFFIXES, open_chrome
    if path.suffix.lower() in WEB_DOCUMENT_SUFFIXES:
        return open_chrome(path.resolve().as_uri())
    try:
        if _SYSTEM == "Windows":
            os.startfile(str(path))
        elif _SYSTEM == "Darwin":
            kit.spawn(['open', str(path)])
        else:
            kit.spawn(['xdg-open', str(path)], env=_linux_env())
        return True
    except Exception as e:
        print(f"[open_app] open path failed: {e}")
        return False


def _launch_with_target(app_name: str, target: Path) -> bool:
    """Ouvre `target` avec `app_name` (ou le programme par défaut si vide)."""
    before = _snapshot_pids()
    normalized = _normalize(app_name) if app_name else ""
    try:
        # Pas d'app demandée : Markdown Studio pour les notes Markdown,
        # lecteur média direct pour les médias, sinon programme par défaut.
        if not app_name or not normalized:
            if (_SYSTEM == "Linux" and
                    target.suffix.lower() in _MARKDOWN_STUDIO_SUFFIXES):
                markdown_studio = kit.which(_MARKDOWN_STUDIO_COMMAND)
                if markdown_studio:
                    proc = _popen_detached([markdown_studio, str(target)])
                    return _confirm_started(proc)
                print("[open_app] Markdown Studio est introuvable ; repli xdg-open.")
            if _SYSTEM == "Linux" and target.suffix.lower() in _MEDIA_EXTS:
                for player in ("vlc", "mpv"):
                    if kit.which(player):
                        _popen_detached([kit.which(player), target.name],
                                        cwd=str(target.parent))
                        return _wait_for_launch(player, before, timeout=5.0)
            return _open_path_default(target)

        if _SYSTEM == "Windows":
            binary = kit.which(normalized) or kit.which(app_name) or normalized
            kit.spawn([binary, str(target)])
        elif _SYSTEM == "Darwin":
            kit.spawn(['open', '-a', normalized, str(target)])
        else:
            argv = _command_argv(normalized) or _command_argv(app_name)
            if not argv:
                # App introuvable : on ouvre quand même avec le programme
                # par défaut plutôt que de répondre « impossible ».
                return _open_path_default(target)
            exe = Path(argv[0]).name.lower()
            if exe == "vlc":
                _popen_detached(argv + [target.name], cwd=str(target.parent))
            else:
                _popen_detached(argv + [str(target)])
        return _wait_for_launch(app_name or target.name, before, timeout=5.0)
    except Exception as e:
        print(f"[open_app] launch with target failed: {e}")
        return False


# ════════════════════════════════════════════════════════════════════════════
# Alias d'applications par OS
# ════════════════════════════════════════════════════════════════════════════

_APP_ALIASES: Dict[str, Dict[str, str]] = {
    "chrome":             {"Windows": "chrome",                  "Darwin": "Google Chrome",        "Linux": "google-chrome"},
    "google chrome":      {"Windows": "chrome",                  "Darwin": "Google Chrome",        "Linux": "google-chrome"},
    "firefox":            {"Windows": "firefox",                 "Darwin": "Firefox",              "Linux": "google-chrome-stable"},
    "edge":               {"Windows": "msedge",                  "Darwin": "Microsoft Edge",       "Linux": "microsoft-edge"},
    "brave":              {"Windows": "brave",                   "Darwin": "Brave Browser",        "Linux": "brave-browser"},
    "safari":             {"Windows": "msedge",                  "Darwin": "Safari",               "Linux": "google-chrome-stable"},
    "opera":              {"Windows": "opera",                   "Darwin": "Opera",                "Linux": "opera"},
    "whatsapp":           {"Windows": "WhatsApp",                "Darwin": "WhatsApp",             "Linux": "whatsapp"},
    "telegram":           {"Windows": "Telegram",                "Darwin": "Telegram",             "Linux": "telegram"},
    "discord":            {"Windows": "Discord",                 "Darwin": "Discord",              "Linux": "discord"},
    "slack":              {"Windows": "Slack",                   "Darwin": "Slack",                "Linux": "slack"},
    "zoom":               {"Windows": "Zoom",                    "Darwin": "zoom.us",              "Linux": "zoom"},
    "teams":              {"Windows": "msteams",                 "Darwin": "Microsoft Teams",      "Linux": "teams"},
    "skype":              {"Windows": "skype",                   "Darwin": "Skype",                "Linux": "skype"},
    "signal":             {"Windows": "signal",                  "Darwin": "Signal",               "Linux": "signal"},
    "spotify":            {"Windows": "Spotify",                 "Darwin": "Spotify",              "Linux": "spotify"},
    "vlc":                {"Windows": "vlc",                     "Darwin": "VLC",                  "Linux": "vlc"},
    "netflix":            {"Windows": "Netflix",                 "Darwin": "Netflix",              "Linux": "google-chrome-stable"},
    "vscode":             {"Windows": "code",                    "Darwin": "Visual Studio Code",   "Linux": "code"},
    "visual studio code": {"Windows": "code",                    "Darwin": "Visual Studio Code",   "Linux": "code"},
    "code":               {"Windows": "code",                    "Darwin": "Visual Studio Code",   "Linux": "code"},
    "terminal":           {"Windows": "wt",                      "Darwin": "Terminal",             "Linux": "x-terminal-emulator"},
    "cmd":                {"Windows": "cmd.exe",                 "Darwin": "Terminal",             "Linux": "bash"},
    "powershell":         {"Windows": "powershell.exe",          "Darwin": "Terminal",             "Linux": "bash"},
    "postman":            {"Windows": "Postman",                 "Darwin": "Postman",              "Linux": "postman"},
    "git":                {"Windows": "git-bash",                "Darwin": "Terminal",             "Linux": "bash"},
    "figma":              {"Windows": "Figma",                   "Darwin": "Figma",                "Linux": "figma"},
    "blender":            {"Windows": "blender",                 "Darwin": "Blender",              "Linux": "blender"},
    "word":               {"Windows": "winword",                 "Darwin": "Microsoft Word",       "Linux": "libreoffice --writer"},
    "excel":              {"Windows": "excel",                   "Darwin": "Microsoft Excel",      "Linux": "libreoffice --calc"},
    "powerpoint":         {"Windows": "powerpnt",                "Darwin": "Microsoft PowerPoint", "Linux": "libreoffice --impress"},
    "libreoffice":        {"Windows": "soffice",                 "Darwin": "LibreOffice",          "Linux": "libreoffice"},
    "notepad":            {"Windows": "notepad.exe",             "Darwin": "TextEdit",             "Linux": "gedit"},
    "textedit":           {"Windows": "notepad.exe",             "Darwin": "TextEdit",             "Linux": "gedit"},
    "explorer":           {"Windows": "explorer.exe",            "Darwin": "Finder",               "Linux": "nautilus"},
    "file explorer":      {"Windows": "explorer.exe",            "Darwin": "Finder",               "Linux": "nautilus"},
    "finder":             {"Windows": "explorer.exe",            "Darwin": "Finder",               "Linux": "nautilus"},
    "task manager":       {"Windows": "taskmgr.exe",             "Darwin": "Activity Monitor",     "Linux": "gnome-system-monitor"},
    "settings":           {"Windows": "ms-settings:",            "Darwin": "System Preferences",   "Linux": "gnome-control-center"},
    "calculator":         {"Windows": "calc.exe",                "Darwin": "Calculator",           "Linux": "gnome-calculator"},
    "paint":              {"Windows": "mspaint.exe",             "Darwin": "Preview",              "Linux": "gimp"},
    "instagram":          {"Windows": "Instagram",               "Darwin": "Instagram",            "Linux": "google-chrome-stable"},
    "tiktok":             {"Windows": "TikTok",                  "Darwin": "TikTok",               "Linux": "google-chrome-stable"},
    "notion":             {"Windows": "Notion",                  "Darwin": "Notion",               "Linux": "notion"},
    "obsidian":           {"Windows": "Obsidian",                "Darwin": "Obsidian",             "Linux": "obsidian"},
    "capcut":             {"Windows": "CapCut",                  "Darwin": "CapCut",               "Linux": "capcut"},
    "steam":              {"Windows": "steam",                   "Darwin": "Steam",                "Linux": "steam"},
    "epic":               {"Windows": "EpicGamesLauncher",       "Darwin": "Epic Games Launcher",  "Linux": "legendary"},
    "epic games":         {"Windows": "EpicGamesLauncher",       "Darwin": "Epic Games Launcher",  "Linux": "legendary"},
    # Extensions issues de /usr/share/applications (Arch/EndeavourOS)
    "audacious":          {"Windows": "", "Darwin": "", "Linux": "audacious"},
    "easyeffects":        {"Windows": "", "Darwin": "", "Linux": "gtk-launch com.github.wwmm.easyeffects"},
    "mpv":                {"Windows": "", "Darwin": "", "Linux": "mpv"},
    "pavucontrol":        {"Windows": "", "Darwin": "", "Linux": "pavucontrol"},
    "totem":              {"Windows": "", "Darwin": "", "Linux": "gtk-launch org.gnome.Totem"},
    "soundrecorder":      {"Windows": "", "Darwin": "", "Linux": "gtk-launch org.gnome.SoundRecorder"},
    "music":              {"Windows": "", "Darwin": "", "Linux": "gtk-launch org.gnome.Music"},
    "lollypop":           {"Windows": "", "Darwin": "", "Linux": "gtk-launch org.gnome.Lollypop"},
    "qv4l2":              {"Windows": "", "Darwin": "", "Linux": "qv4l2"},
    "qvidcap":            {"Windows": "", "Darwin": "", "Linux": "qvidcap"},
    "foot":               {"Windows": "", "Darwin": "", "Linux": "foot"},
    "footclient":         {"Windows": "", "Darwin": "", "Linux": "footclient"},
    "foot-server":        {"Windows": "", "Darwin": "", "Linux": "gtk-launch foot-server"},
    "kitty":              {"Windows": "", "Darwin": "", "Linux": "kitty"},
    "xterm":              {"Windows": "", "Darwin": "", "Linux": "xterm"},
    "uxterm":             {"Windows": "", "Darwin": "", "Linux": "uxterm"},
    "yazi":               {"Windows": "", "Darwin": "", "Linux": "yazi"},
    "sublime":            {"Windows": "", "Darwin": "", "Linux": "sublime_text"},
    "sublime text":       {"Windows": "", "Darwin": "", "Linux": "sublime_text"},
    "kate":               {"Windows": "", "Darwin": "", "Linux": "kate"},
    "kwrite":             {"Windows": "", "Darwin": "", "Linux": "kwrite"},
    "marknote":           {"Windows": "", "Darwin": "", "Linux": "gtk-launch org.kde.marknote"},
    "vim":                {"Windows": "", "Darwin": "", "Linux": "vim"},
    "micro":              {"Windows": "", "Darwin": "", "Linux": "micro"},
    "jetbrains-studio":   {"Windows": "", "Darwin": "", "Linux": "gtk-launch jetbrains-studio"},
    "zcode":              {"Windows": "", "Darwin": "", "Linux": "gtk-launch zcode"},
    "codex":              {"Windows": "", "Darwin": "", "Linux": "gtk-launch codex-desktop"},
    "claude":             {"Windows": "", "Darwin": "", "Linux": "gtk-launch com.anthropic.Claude"},
    "cmake-gui":          {"Windows": "", "Darwin": "", "Linux": "cmake-gui"},
    "assistant":          {"Windows": "", "Darwin": "", "Linux": "assistant"},
    "designer":           {"Windows": "", "Darwin": "", "Linux": "designer"},
    "linguist":           {"Windows": "", "Darwin": "", "Linux": "linguist"},
    "qdbusviewer":        {"Windows": "", "Darwin": "", "Linux": "qdbusviewer"},
    "qt6ct":              {"Windows": "", "Darwin": "", "Linux": "qt6ct"},
    "darklystyleconfig":  {"Windows": "", "Darwin": "", "Linux": "darklystyleconfig"},
    "kvantummanager":     {"Windows": "", "Darwin": "", "Linux": "kvantummanager"},
    "gnome-system-monitor": {"Windows": "", "Darwin": "", "Linux": "gnome-system-monitor"},
    "gnome-calculator":     {"Windows": "", "Darwin": "", "Linux": "gnome-calculator"},
    "gnome-clocks":         {"Windows": "", "Darwin": "", "Linux": "gnome-clocks"},
    "gnome-chess":          {"Windows": "", "Darwin": "", "Linux": "gnome-chess"},
    "gnome-contacts":       {"Windows": "", "Darwin": "", "Linux": "gnome-contacts"},
    "gnome-mahjongg":       {"Windows": "", "Darwin": "", "Linux": "gnome-mahjongg"},
    "gnome-maps":           {"Windows": "", "Darwin": "", "Linux": "gnome-maps"},
    "gnome-music":          {"Windows": "", "Darwin": "", "Linux": "gnome-music"},
    "gnome-notes":          {"Windows": "", "Darwin": "", "Linux": "gnome-notes"},
    "gnome-photos":         {"Windows": "", "Darwin": "", "Linux": "gnome-photos"},
    "gnome-texteditor":     {"Windows": "", "Darwin": "", "Linux": "gnome-text-editor"},
    "gnome-totem":          {"Windows": "", "Darwin": "", "Linux": "totem"},
    "gnome-loupe":          {"Windows": "", "Darwin": "", "Linux": "loupe"},
    "gnome-extensions":     {"Windows": "", "Darwin": "", "Linux": "gnome-extensions"},
    "gnome-seahorse":       {"Windows": "", "Darwin": "", "Linux": "seahorse"},
    "gnome-zenity":         {"Windows": "", "Darwin": "", "Linux": "zenity"},
    "gnome-meld":           {"Windows": "", "Darwin": "", "Linux": "meld"},
    "gnome-2048":           {"Windows": "", "Darwin": "", "Linux": "gnome-2048"},
    "evolution-alarm":      {"Windows": "", "Darwin": "", "Linux": "evolution-alarm-notify"},
    "btop":                 {"Windows": "", "Darwin": "", "Linux": "btop"},
    "blueman-manager":      {"Windows": "", "Darwin": "", "Linux": "blueman-manager"},
    "blueman-adapters":     {"Windows": "", "Darwin": "", "Linux": "blueman-adapters"},
    "firewall-config":      {"Windows": "", "Darwin": "", "Linux": "firewall-config"},
    "gparted":              {"Windows": "", "Darwin": "", "Linux": "gparted"},
    "lstopo":               {"Windows": "", "Darwin": "", "Linux": "lstopo"},
    "reflector-simple":     {"Windows": "", "Darwin": "", "Linux": "reflector-simple"},
    "stoken-gui":           {"Windows": "", "Darwin": "", "Linux": "stoken-gui"},
    "stoken-gui-small":     {"Windows": "", "Darwin": "", "Linux": "stoken-gui-small"},
    "swappy":               {"Windows": "", "Darwin": "", "Linux": "swappy"},
    "ventoy":               {"Windows": "", "Darwin": "", "Linux": "ventoy"},
    "uuctl":                {"Windows": "", "Darwin": "", "Linux": "uuctl"},
    "yad-icon-browser":     {"Windows": "", "Darwin": "", "Linux": "yad-icon-browser"},
    "yad-settings":         {"Windows": "", "Darwin": "", "Linux": "yad-settings"},
    "user-dirs-update-gtk": {"Windows": "", "Darwin": "", "Linux": "xdg-user-dirs-gtk-update"},
    "kded5":                {"Windows": "", "Darwin": "", "Linux": "kded5"},
    "kded6":                {"Windows": "", "Darwin": "", "Linux": "kded6"},
    "kiod6":                {"Windows": "", "Darwin": "", "Linux": "kiod6"},
    "knewstuff-dialog6":    {"Windows": "", "Darwin": "", "Linux": "knewstuff-dialog6"},
    "ksecretd":             {"Windows": "", "Darwin": "", "Linux": "ksecretd"},
    "kcm-proxy":            {"Windows": "", "Darwin": "", "Linux": "kcmshell6 proxy"},
    "kcm-trash":            {"Windows": "", "Darwin": "", "Linux": "kcmshell6 trash"},
    "kcm-netpref":          {"Windows": "", "Darwin": "", "Linux": "kcmshell6 netpref"},
    "kcm-webshortcuts":     {"Windows": "", "Darwin": "", "Linux": "kcmshell6 webshortcuts"},
    "kcm-darklydecoration": {"Windows": "", "Darwin": "", "Linux": "kcmshell6 darklydecoration"},
    "ktelnetservice5":      {"Windows": "", "Darwin": "", "Linux": "ktelnetservice5"},
    "ktelnetservice6":      {"Windows": "", "Darwin": "", "Linux": "ktelnetservice6"},
    "avahi-discover":       {"Windows": "", "Darwin": "", "Linux": "avahi-discover"},
    "bssh":                 {"Windows": "", "Darwin": "", "Linux": "bssh"},
    "bvnc":                 {"Windows": "", "Darwin": "", "Linux": "bvnc"},
    "nm-applet":            {"Windows": "", "Darwin": "", "Linux": "nm-applet"},
    "nm-connection-editor": {"Windows": "", "Darwin": "", "Linux": "nm-connection-editor"},
    "moonlight":            {"Windows": "", "Darwin": "", "Linux": "gtk-launch moonlight-stable"},
    "sunshine":             {"Windows": "", "Darwin": "", "Linux": "gtk-launch dev.lizardbyte.app.Sunshine"},
    "scrcpy":               {"Windows": "", "Darwin": "", "Linux": "scrcpy"},
    "scrcpy-console":       {"Windows": "", "Darwin": "", "Linux": "gtk-launch scrcpy-console"},
    "localsend":            {"Windows": "", "Darwin": "", "Linux": "localsend"},
    "zapzap":               {"Windows": "", "Darwin": "", "Linux": "gtk-launch com.rtosta.zapzap"},
    "libreoffice-base":     {"Windows": "", "Darwin": "", "Linux": "libreoffice --base"},
    "libreoffice-draw":     {"Windows": "", "Darwin": "", "Linux": "libreoffice --draw"},
    "libreoffice-math":     {"Windows": "", "Darwin": "", "Linux": "libreoffice --math"},
    "libreoffice-startcenter": {"Windows": "", "Darwin": "", "Linux": "libreoffice"},
    "java":                 {"Windows": "", "Darwin": "", "Linux": "java"},
    "jconsole":             {"Windows": "", "Darwin": "", "Linux": "jconsole"},
    "jshell":               {"Windows": "", "Darwin": "", "Linux": "jshell"},
    "eos-welcome":          {"Windows": "", "Darwin": "", "Linux": "eos-welcome"},
    "eos-apps-info":        {"Windows": "", "Darwin": "", "Linux": "eos-apps-info"},
    "eos-log-tool":         {"Windows": "", "Darwin": "", "Linux": "eos-log-tool"},
    "eos-quickstart":       {"Windows": "", "Darwin": "", "Linux": "eos-quickstart"},
    "eos-update":           {"Windows": "", "Darwin": "", "Linux": "eos-update"},
    "geoclue-demo-agent":   {"Windows": "", "Darwin": "", "Linux": "gtk-launch geoclue-demo-agent"},
    "geoclue-where-am-i":   {"Windows": "", "Darwin": "", "Linux": "gtk-launch geoclue-where-am-i"},
    "gcr-prompter":         {"Windows": "", "Darwin": "", "Linux": "gcr-prompter"},
    "gcr-viewer":           {"Windows": "", "Darwin": "", "Linux": "gcr-viewer"},
    "pinentry-qt":          {"Windows": "", "Darwin": "", "Linux": "pinentry-qt"},
    "quickshell":           {"Windows": "", "Darwin": "", "Linux": "gtk-launch org.quickshell"},
    "terax":                {"Windows": "", "Darwin": "", "Linux": "gtk-launch Terax"},
    "gradia":               {"Windows": "", "Darwin": "", "Linux": "gtk-launch be.alexandervanhee.gradia"},
    "google-maps-geo":      {"Windows": "", "Darwin": "", "Linux": "gtk-launch google-maps-geo-handler"},
    "openstreetmap-geo":    {"Windows": "", "Darwin": "", "Linux": "gtk-launch openstreetmap-geo-handler"},
    "wheelmap-geo":         {"Windows": "", "Darwin": "", "Linux": "gtk-launch wheelmap-geo-handler"},
}

# Choix explicite du propriétaire : sous Linux, tout gestionnaire de fichiers
# passe par Nautilus. Le modèle peut proposer Dolphin/Thunar selon les paquets
# présents ; ne jamais laisser ce hasard changer l'application demandée.
_FILE_MANAGER_NAMES = {
    "dolphin", "nautilus", "thunar", "nemo", "pcmanfm", "files",
    "fichiers", "gestionnaire de fichiers", "gestionnaire fichier",
    "file manager", "file explorer", "explorateur", "explorer",
}


def _force_nautilus_for_file_manager(app_name: str) -> str:
    normalized = " ".join(str(app_name or "").casefold().split())
    return "nautilus" if normalized in _FILE_MANAGER_NAMES else app_name


def _normalize(raw: str) -> str:
    """Nom d'usage → commande réelle pour l'OS courant. Ne renvoie JAMAIS
    une chaîne vide : si l'alias est vide pour cet OS, on garde le nom
    demandé (il sera résolu par which/desktop-entry/xdg-open)."""
    key = (raw or "").lower().strip()
    from core.browser_policy import BROWSER_ALIASES
    if key in BROWSER_ALIASES:
        key = "chrome"
    if key in _APP_ALIASES:
        val = (_APP_ALIASES[key].get(_SYSTEM, "") or "").strip()
        return val or raw
    for alias_key, os_map in _APP_ALIASES.items():
        if alias_key in key or key in alias_key:
            val = (os_map.get(_SYSTEM, "") or "").strip()
            if val:
                return val
    return raw


# ════════════════════════════════════════════════════════════════════════════
# Lanceurs par OS
# ════════════════════════════════════════════════════════════════════════════

def _launch_windows(app_name: str, instance_name: Optional[str] = None) -> bool:
    before = _snapshot_pids()
    if kit.which(app_name) or kit.which(app_name.split(".")[0]):
        try:
            proc = subprocess.Popen(app_name, shell=True,
                                    stdout=DEVNULL, stderr=DEVNULL)
            ok = _wait_for_launch(app_name, before)
            if ok and instance_name and _HAS_WINDOW_INSTANCES:
                register_instance(instance_name, app=app_name,
                                  initial_title=None, pid=proc.pid)
            return ok
        except Exception as e:
            print(f"[open_app] subprocess failed: {e}")
    if ":" in app_name:
        try:
            kit.spawn(f"start {app_name}", shell=True)
            return _wait_for_launch(app_name, before, timeout=2.5)
        except Exception:
            pass
    try:
        import pyautogui
        pyautogui.PAUSE = 0.1
        pyautogui.press("win")
        time.sleep(0.7)
        pyautogui.write(app_name, interval=0.05)
        time.sleep(0.9)
        pyautogui.press("enter")
        return _wait_for_launch(app_name, before, timeout=5.0)
    except Exception as e:
        print(f"[open_app] Start Menu search failed: {e}")
    return False


def _launch_macos(app_name: str, instance_name: Optional[str] = None) -> bool:
    before = _snapshot_pids()
    for candidate in (app_name, f"{app_name}.app"):
        if kit.run(["open", "-a", candidate], timeout=8):
            return _wait_for_launch(app_name, before)
    binary = kit.which(app_name) or kit.which(app_name.lower())
    if binary:
        try:
            kit.spawn([binary])
            return _wait_for_launch(app_name, before)
        except Exception:
            pass
    try:
        import pyautogui
        pyautogui.hotkey("command", "space")
        time.sleep(0.6)
        pyautogui.write(app_name, interval=0.05)
        time.sleep(0.8)
        pyautogui.press("enter")
        return _wait_for_launch(app_name, before, timeout=5.0)
    except Exception as e:
        print(f"[open_app] Spotlight failed: {e}")
    return False


_LINUX_TERMINAL_FALLBACKS = [
    "x-terminal-emulator", "kitty", "foot", "alacritty", "gnome-terminal",
    "konsole", "xfce4-terminal", "wezterm", "tilix", "mate-terminal",
    "lxterminal", "xterm",
]


def _launch_linux(app_name: str, instance_name: Optional[str] = None) -> bool:
    # ── 1. Instance nommée (terminaux avec flag de titre) ────────────────
    if instance_name and _HAS_WINDOW_INSTANCES:
        binary_candidate = (
            kit.which(app_name) or
            kit.which(app_name.lower()) or
            kit.which(app_name.lower().replace(" ", "-"))
        )
        flag = (_title_flag_for(app_name) or
                (binary_candidate and _title_flag_for(binary_candidate)))
        if flag and binary_candidate:
            try:
                proc = _popen_detached([binary_candidate, flag, instance_name])
                if _confirm_started(proc):
                    register_instance(instance_name, app=app_name,
                                      initial_title=instance_name, pid=proc.pid)
                    return True
            except Exception as e:
                print(f"[open_app] lancement nommé '{instance_name}' échoué : {e}")

    # ── 2. Demande générique « terminal » ────────────────────────────────
    if app_name in ("x-terminal-emulator", "gnome-terminal", "terminal"):
        for term in _LINUX_TERMINAL_FALLBACKS:
            if kit.which(term):
                try:
                    if instance_name and _HAS_WINDOW_INSTANCES and _title_flag_for(term):
                        flag = _title_flag_for(term)
                        proc = _popen_detached([term, flag, instance_name])
                        if _confirm_started(proc):
                            register_instance(instance_name, app=term,
                                              initial_title=instance_name,
                                              pid=proc.pid)
                            return True
                        continue
                    proc = _popen_detached([term])
                    return _confirm_started(proc)
                except Exception:
                    continue

    # ── 3. Entrée .desktop connue ────────────────────────────────────────
    if _find_desktop_app is not None:
        try:
            entry = _find_desktop_app(app_name)
        except Exception:
            entry = None
        if entry is not None:
            argv = entry.exec_argv
            if argv:
                if entry.terminal:
                    term = next((t for t in _LINUX_TERMINAL_FALLBACKS
                                 if kit.which(t)), None)
                    if term:
                        argv = [term, "-e"] + argv
                try:
                    proc = _popen_detached(argv, cwd=str(Path.home()))
                    if _confirm_started(proc):
                        return True
                except (FileNotFoundError, OSError) as e:
                    print(f"[open_app] desktop entry '{entry.id}' exec failed: {e}")

    # ── 4. Commande composée ou binaire simple ───────────────────────────
    argv = _command_argv(app_name) or _command_argv(app_name.lower())
    if argv:
        try:
            proc = _popen_detached(argv)
            if _confirm_started(proc):
                if instance_name and _HAS_WINDOW_INSTANCES:
                    # Pas de flag de titre : enregistrement best-effort par PID.
                    register_instance(instance_name, app=app_name,
                                      initial_title=None, pid=proc.pid)
                return True
        except Exception:
            pass

    # ── 5. Dernier recours : xdg-open ────────────────────────────────────
    # xdg-open peut rester attaché au programme qu'il ouvre : on ne lit pas sa
    # sortie, on veut seulement savoir qu'il a accepté la demande.
    return kit.run(["xdg-open", app_name], timeout=5, env=_linux_env(),
                   capture=False).ok


_OS_LAUNCHERS = {
    "Windows": _launch_windows,
    "Darwin": _launch_macos,
    "Linux": _launch_linux,
}


# ════════════════════════════════════════════════════════════════════════════
# Parsing local des commandes naturelles
# ════════════════════════════════════════════════════════════════════════════

def _parse_open_command_locally(text: str) -> Optional[Dict[str, Any]]:
    """Interprète les commandes naturelles pour ouvrir une application ou
    un fichier. NB : workspace et quantité ont déjà été extraits en amont."""
    text = text.lower().strip()
    text = re.sub(r"\b(s'il te pla[iî]t|stp|please|peux[- ]tu|tu peux|tu pourrais)\b",
                  "", text).strip()

    # Pattern 0 : commande composée — lance/ouvre <app> et (tu )?(tape|écris|lance|exécute|mets)( la commande)? <cmd>
    m_compound = re.search(
        r"^(?:ouvre|lance|démarre|demarre|exécute|execute|start|open|run|launch)\s+"
        r"(?:(?:l['’]|le\s+|la\s+|les\s+)?(?:application\s+|app\s+|programme\s+)?)?"
        r"(?P<app>[a-zA-Z0-9_\-\.\s]+?)\s+"
        r"et\s+(?:tu\s+)?"
        r"(?:tape[rs]?(?:-y)?|écri[ts]|ecri[ts]|écrire|ecrire|lance[rs]?|exécute[rs]?|execute[rs]?|exécuter|executer|mets?|mettre|saisi[ts]|saisir|entre[rs]?)\s+"
        r"(?:(?:(?:la|une|le)\s+)?(?:commande|ligne\s+de\s+commande|instruction|texte)\s*(?:suivante\s*)?:?\s*)?"
        r"(?P<cmd>.+)$",
        text,
        re.IGNORECASE,
    )
    if m_compound:
        raw_app = m_compound.group("app").strip().strip("'\"")
        raw_app = re.sub(r"^(?:l['’]|le\s+|la\s+|les\s+)", "", raw_app, flags=re.I).strip()
        raw_app = re.sub(r"^(?:application|app|programme)\s+", "", raw_app, flags=re.I).strip()
        raw_cmd = m_compound.group("cmd").strip().strip("'\"")
        raw_cmd = re.sub(r"^(?:(?:la|une|le)\s+)?(?:commande|instruction|texte)\s*:?\s*", "", raw_cmd, flags=re.I).strip().strip("'\"")
        if raw_app and raw_cmd:
            return {
                "action": "launch",
                "app_name": raw_app,
                "command": raw_cmd,
            }

    # Pattern 1 : verbe + cible [+ avec <app/fichier>]
    m = re.search(
        r"(?:ouvre|lance|démarre|demarre|exécute|execute|start|open|run|launch|play|joue|écoute|ecoute)\s+"
        r"(?:le |la |les |l'|l’)?(?:application |app |programme |musique |chanson |vidéo |video |fichier )?"
        r"(.+?)(?:\s+avec\s+(.+))?$", text)
    if m:
        target = m.group(1).strip().strip("'\"")
        app_with = m.group(2).strip() if m.lastindex and m.lastindex >= 2 and m.group(2) else None
        if re.search(r"\.\w{2,4}$", target) or "/" in target or "\\" in target:
            if app_with:
                return {"action": "launch_with_target", "app_name": app_with, "target": target}
            return {"action": "launch_with_target", "app_name": "", "target": target}
        if app_with:
            return {"action": "launch_with_target", "app_name": target, "target": app_with}
        return {"action": "launch", "app_name": target}

    # Pattern 2 : juste un nom d'app
    if re.match(r"^[\w\s-]+$", text) and len(text.split()) <= 3:
        return {"action": "launch", "app_name": text}

    # Pattern 3 : « joue <morceau> »
    m = re.search(r"(?:joue|play|écoute|ecoute)\s+(?:le |la |les |l'|l’)?"
                  r"(?:musique |chanson |vidéo |video |fichier )?(.+)", text)
    if m:
        return {"action": "launch_with_target", "app_name": "",
                "target": m.group(1).strip()}

    # Pattern 4 : « ouvre le dossier <nom> »
    m = re.search(r"(?:ouvre|affiche|montre)\s+(?:le |la |les |l'|l’)?"
                  r"(?:dossier |répertoire |repertoire )?(.+?)(?:\s+dossier)?$", text)
    if m:
        return {"action": "launch_with_target", "app_name": "explorer",
                "target": m.group(1).strip()}
    return None


def _get_api_key() -> str:
    try:
        base = Path(__file__).resolve().parent.parent
        config = base / "config" / "api_keys.json"
        with open(config, "r", encoding="utf-8") as f:
            return json.load(f).get("gemini_api_key", "")
    except Exception:
        return ""


def _detect_open_intent_ai(description: str) -> Optional[Dict]:
    api_key = _get_api_key()
    if not api_key:
        return None
    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        prompt = (
            f"Analyse la phrase suivante et retourne UNIQUEMENT un objet JSON avec l'action et les paramètres.\n"
            f"Actions possibles : launch (app_name), launch_with_target (app_name + target).\n"
            f"Exemples :\n"
            f'"lance firefox" -> {{"action": "launch", "app_name": "firefox"}}\n'
            f'"ouvre mon fichier rapport.docx avec word" -> {{"action": "launch_with_target", "app_name": "word", "target": "rapport.docx"}}\n'
            f'"joue ma chanson préférée" -> {{"action": "launch_with_target", "app_name": "", "target": "ma chanson préférée"}}\n'
            f'Phrase : "{description}"\n'
            "Réponds uniquement avec le JSON."
        )
        resp = client.models.generate_content(model=FAST_MODEL,
                                              contents=prompt)
        json_match = re.search(r"\{.*\}", resp.text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group(0))
    except Exception as e:
        print(f"[open_app] AI detection error: {e}")
    return None


# ════════════════════════════════════════════════════════════════════════════
# Point d'entrée principal
# ════════════════════════════════════════════════════════════════════════════

_HIDDEN_RE = re.compile(
    r"\b(?:en\s+cach[ée]|cach[ée]e?|sans\s+(?:l['e]\s*)?afficher|"
    r"sans\s+montrer|invisible|en\s+arri[èe]re[-\s]plan|discr[èe]tement|"
    r"hidden|in\s+the\s+background|silently)\b",
    re.IGNORECASE,
)

@kit.action("open_app")
def open_app(parameters=None, response=None, player=None, session_memory=None) -> str:
    """
    Ouvre une application, un fichier ou un dossier.
    Paramètres acceptés :
      app_name     : nom de l'application
      target       : fichier/dossier à ouvrir (optionnel)
      description  : phrase naturelle (prioritaire si aucune action explicite)
      workspace    : bureau cible (chiffre ou texte ordinal)
      hidden       : lance l'app sur le bureau spécial invisible Hyprland
                     (special:hidden) — jamais de focus dessus, état relu
                     après coup (un dispatch « ok » ne prouve rien)
      count        : nombre de fenêtres à ouvrir (défaut 1, max 5)
      instance_name: surnom d'instance (« main-term »)
    """
    params = parameters or {}
    app_name = str(params.get("app_name", "") or "").strip()
    target_raw = str(params.get("target", "") or "").strip()
    description = str(params.get("description", "") or "").strip()
    command = str(params.get("command") or params.get("type_text") or "").strip()
    workspace = params.get("workspace")
    hidden = bool(params.get("hidden", False))
    instance_name = str(params.get("instance_name", "") or "").strip()
    count = params.get("count")
    try:
        count = int(count) if count is not None else None
    except (TypeError, ValueError):
        count = None

    # ── Lancement caché depuis la phrase naturelle ────────────────────────
    if not hidden and description and _HIDDEN_RE.search(description):
        hidden = True
        description = _HIDDEN_RE.sub(" ", description).strip()

    # ── Surnom d'instance depuis la phrase naturelle ─────────────────────
    if not instance_name and description:
        description, extracted = _extract_instance_name(description)
        if extracted:
            instance_name = extracted

    # ── Workspace : paramètre explicite OU extraction chiffres/ordinaux ──
    ws_num: Optional[int] = None
    if workspace is not None:
        try:
            ws_num = int(str(workspace).strip())
        except (TypeError, ValueError):
            ws_num, _ = _extract_workspace(str(workspace))
    if ws_num is None and app_name:
        w, app_name = _extract_workspace(app_name)
        if w is not None:
            ws_num = w
    if ws_num is None and description:
        w, description = _extract_workspace(description)
        if w is not None:
            ws_num = w

    # ── Quantité : paramètre ou extraction de la phrase ──────────────────
    if count is None and app_name:
        c, app_name = _extract_count(app_name)
        if c:
            count = c
    if count is None and description:
        c, description = _extract_count(description)
        if c:
            count = c

    # ── Interprétation d'une description en langage naturel ──────────────
    if description and (not app_name or not command) and not target_raw:
        local = _parse_open_command_locally(description)
        if local:
            action = local.get("action")
            if action == "launch":
                if not app_name:
                    app_name = local.get("app_name", "")
                if not command and local.get("command"):
                    command = local.get("command", "")
            elif action == "launch_with_target":
                if not app_name:
                    app_name = local.get("app_name", "")
                if not target_raw:
                    target_raw = local.get("target", "")
        else:
            ai = _detect_open_intent_ai(description)
            if ai:
                if not app_name:
                    app_name = ai.get("app_name", "") or ""
                if not target_raw:
                    target_raw = ai.get("target", "") or ""
                if not command and ai.get("command"):
                    command = ai.get("command", "")
            elif not app_name and not target_raw:
                return "Je n'ai pas compris quelle application ou fichier ouvrir. Pouvez-vous reformuler ?"

    if not app_name and not target_raw:
        return "Aucune application ou fichier indiqué."

    app_name = _force_nautilus_for_file_manager(app_name)

    launcher = _OS_LAUNCHERS.get(_SYSTEM)
    if launcher is None:
        return f"Système d'exploitation non supporté : {_SYSTEM}"

    normalized = _normalize(app_name) if app_name else ""
    print(f"[open_app] Lancement : '{app_name}' → '{normalized}' ({_SYSTEM})")
    if player:
        try:
            player.write_log(f"[open_app] {app_name or target_raw}")
        except Exception:
            pass

    try:
        already_running = False
        if app_name and _PSUTIL:
            already_running = _is_process_running(app_name)

        # ── Ouverture d'un fichier/dossier avec une app ──────────────────
        if target_raw:
            target = _resolve_target(target_raw)
            if target is None:
                return f"Je n'ai pas trouvé le fichier ou dossier : {target_raw}"
            if not target.exists():
                return f"Ce chemin n'existe pas : {target}"
            if _launch_with_target(normalized or app_name, target):
                if app_name:
                    return f"{target.name} est ouvert avec {app_name}."
                return f"{target.name} est ouvert."
            return f"Impossible de confirmer l'ouverture de {target.name}."

        # ── Lancement d'application(s), éventuellement en N exemplaires ──
        n = max(1, min(count or 1, 5))  # garde-fou : jamais plus de 5
        if count and count > 5:
            print(f"[open_app] count={count} plafonné à 5.")

        # Cible de déplacement : bureau spécial invisible si hidden=True
        # (jamais focus dessus — sinon l'utilisateur le verrait apparaître),
        # sinon le bureau numérique demandé le cas échéant.
        move_target = "special:hidden" if hidden else ws_num

        if ws_num is not None and not hidden and _SYSTEM == "Linux":
            _focus_workspace(ws_num)

        attempts = ([normalized, app_name] if normalized.lower() == app_name.lower()
                    else ([app_name, normalized] if _SYSTEM == "Linux"
                          else [normalized, app_name]))
        attempts = [a for a in attempts if a]

        launched = 0
        moved = 0
        for i in range(n):
            # Snapshot des adresses AVANT chaque lancement, pour le
            # déplacement de la bonne fenêtre et le journal de lancement.
            before_addrs: Set[str] = set()
            if _SYSTEM == "Linux" and kit.which("hyprctl"):
                before_addrs = {c.get("address", "") for c in
                                (_hyprctl_json("clients") or [])}

            success = False
            for candidate in attempts:
                # Le surnom n'a de sens que pour la première instance :
                # deux fenêtres ne peuvent pas porter le même surnom.
                iname = instance_name if i == 0 else None
                if launcher(candidate, instance_name=iname):
                    success = True
                    break
            if not success:
                break
            launched += 1

            if _HAS_TRACKER and _SYSTEM == "Linux":
                try:
                    _tracker.record_launch(app_name or normalized, before_addrs,
                                          nickname=iname)
                except Exception as e:
                    print(f"[open_app] journalisation du lancement échouée : {e}")

            if move_target is not None:
                if _move_new_window_to_workspace(app_name or normalized,
                                                 move_target, before_addrs):
                    moved += 1
                    if hidden and not _verify_hidden(before_addrs):
                        # Le dispatch a répondu "ok" mais hyprctl "ok" ne
                        # prouve rien : on relit l'état réel avant d'annoncer
                        # une réussite (piège déjà rencontré sur ce projet).
                        moved -= 1
            if i < n - 1:
                time.sleep(0.35)

        if launched == 0:
            return (f"Impossible de confirmer que {app_name} s'est lancé. "
                    f"L'application est peut-être absente ou encore en cours de chargement.")

        # ── Saisie automatique de la commande demandée ──────────────────
        target_window = None
        if command:
            target_window = params.get("target_window") or params.get("window")
            if not target_window:
                new_win = _wait_for_new_window(app_name or normalized, before_addrs, timeout=6.0)
                if new_win:
                    new_addr = new_win.get("address")
                    win_ws = (new_win.get("workspace") or {}).get("id")
                    active_ws_info = _hyprctl_json("activeworkspace") or {}
                    active_ws = active_ws_info.get("id") if isinstance(active_ws_info, dict) else None
                    if win_ws is not None and active_ws is not None and win_ws != active_ws:
                        _focus_workspace(win_ws)
                    target_window = f"address:{new_addr}" if new_addr else (app_name or normalized)
                else:
                    target_window = app_name or normalized

            wait_ready = float(params.get("wait_functional") or params.get("wait_seconds") or 3.5)
            _window_ready(target_window, timeout=wait_ready)

            _focus_window(target_window)

            cc_payload = {
                "action": "type",
                "text": command,
                "window": target_window,
                "press_enter": True,
            }
            if computer_control is not None:
                computer_control(cc_payload)
            else:
                try:
                    from actions.computer_control import computer_control as _cc
                    _cc(cc_payload)
                except Exception as e:
                    print(f"[open_app] Erreur lors de la saisie de la commande '{command}' : {e}")

        # ── Message de synthèse ──────────────────────────────────────────
        ws_note = ""
        if hidden:
            if moved == launched:
                ws_note = " en arrière-plan, hors de vue (bureau caché confirmé)"
            else:
                ws_note = (" — lancé, mais je n'ai pas pu confirmer qu'il est bien "
                           "passé en arrière-plan : vérifie qu'il n'est pas visible")
        elif ws_num is not None:
            if moved == launched:
                ws_note = f" sur le bureau {ws_num}"
            elif moved > 0:
                ws_note = (f" ({moved}/{launched} fenêtre(s) déplacée(s) "
                           f"vers le bureau {ws_num})")
            else:
                ws_note = (f" — le déplacement vers le bureau {ws_num} "
                           f"n'a pas pu être confirmé")

        name_note = ""
        if instance_name:
            if _HAS_WINDOW_INSTANCES:
                flag_supported = bool(_title_flag_for(normalized) or
                                      _title_flag_for(app_name))
                if flag_supported:
                    name_note = f" — enregistré sous le surnom '{instance_name}'."
                else:
                    name_note = (f" — surnom '{instance_name}' enregistré, mais "
                                 f"{app_name} ne supporte pas de titre fixe : "
                                 f"le ciblage restera approximatif (par PID).")
            else:
                name_note = f" (surnom '{instance_name}' demandé mais module de nommage indisponible)"

        cmd_note = ""
        if command:
            if target_window:
                cmd_note = f" avec exécution de '{command}' sur cette même fenêtre ({target_window})"
            else:
                cmd_note = f" avec exécution de '{command}'"

        if n > 1:
            plural = f"{launched} fenêtres {app_name} ouvertes"
            if launched < n:
                plural = (f"seulement {launched} fenêtre(s) {app_name} ouverte(s) "
                          f"sur {n} demandées")
            return f"{plural}{ws_note}{cmd_note}."

        if already_running:
            return (f"{app_name} est déjà ouvert (une nouvelle fenêtre a "
                    f"peut-être été lancée){ws_note}{name_note}{cmd_note}.")
        return f"{app_name} est ouvert{ws_note}{name_note}{cmd_note}."

    except Exception as e:
        print(f"[open_app] Erreur : {e}")
        return f"Échec de l'ouverture de {app_name or target_raw} : {e}"


# ════════════════════════════════════════════════════════════════════════════
# Test direct
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        print(open_app({"description": " ".join(sys.argv[1:])}))
    else:
        print("Usage: open_app.py <phrase ou nom d'app>")
