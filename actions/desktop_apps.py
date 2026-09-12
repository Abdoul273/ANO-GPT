"""
desktop_apps.py — Indexeur d'applications & lanceur intelligent.
Parsing local avancé, reconnaissance des apps ouvertes, fallback IA.
Optimisé Arch Linux / Hyprland (Wayland), compatible macOS/Windows.

Corrections par rapport à l'ancienne version :
    - `_get_api_key` utilisait `Path(file)` au lieu de `Path(__file__)` :
      NameError garanti au moindre fallback IA ;
    - les applications sans fichier .desktop (btop, apps CLI/GUI…) étaient
      déclarées « introuvable » alors que leur binaire existe : repli
      shutil.which ajouté ;
    - le lancement d'apps terminal utilisait x-terminal-emulator (absent
      sous Arch) et le flag -e, que kitty ne supporte pas : table de
      terminaux avec le bon flag pour chacun (kitty sans flag, foot/alacritty
      -e, gnome-terminal --, konsole -e…), kitty/foot/alacritty en tête ;
    - aucun environnement Wayland restauré : les apps graphiques échouaient
      quand l'assistant tourne en service systemd ou via ssh →
      WAYLAND_DISPLAY, DISPLAY, XDG_RUNTIME_DIR et
      HYPRLAND_INSTANCE_SIGNATURE sont restaurés avant chaque lancement ;
    - aucune vérification réelle du démarrage : le message « Lancement de X »
      était renvoyé même si le processus mourait immédiatement →
      vérification du code de sortie avec _confirm_started ;
    - les alias pointaient vers une seule cible (« fichiers » → nautilus) :
      si elle était absente, tout échouait → listes de candidats essayées
      dans l'ordre ;
    - le champ TryExec des .desktop était ignoré : les entrées d'apps non
      installées polluaient l'index et échouaient au lancement ;
    - stdin n'était pas détaché (apps suspendues en attente d'entrée).

Ajouts :
    - action « status » (l'application est-elle ouverte ?) ;
    - alias d'actions (lance/ouvre/open/find/liste/etat) ;
    - note « déjà ouvert » dans les messages de lancement ;
    - API find_app/AppEntry inchangée pour les modules qui l'importent.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import time
import json
import platform
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Dict, List, Tuple, Set

from core import action_kit as kit
from core.live_model_policy import FAST_MODEL

# ── Configuration ────────────────────────────────────────────────────────────
CACHE_TTL = 60.0               # secondes avant re-vérification des .desktop
_OS = platform.system()        # "Linux", "Windows", "Darwin"
DEVNULL = subprocess.DEVNULL


# ════════════════════════════════════════════════════════════════════════════
# Parsing des fichiers .desktop (Linux)
# ════════════════════════════════════════════════════════════════════════════

_FIELD_CODE_RE = re.compile(r"%[fFuUick%dDnNvm]")


def _desktop_dirs() -> List[Path]:
    dirs: List[Path] = []
    xdg_data_dirs = os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share")
    for base in xdg_data_dirs.split(":"):
        if base:
            dirs.append(Path(base) / "applications")
    xdg_data_home = os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
    dirs.append(Path(xdg_data_home) / "applications")
    dirs.append(Path("/usr/share/applications"))
    seen, out = set(), []
    for d in dirs:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


@dataclass
class AppEntry:
    id: str                  # identifiant unique (nom du .desktop)
    name: str                # Name=
    generic_name: str = ""   # GenericName=
    exec_raw: str = ""       # Exec= avec codes de champs
    icon: str = ""
    terminal: bool = False
    keywords: List[str] = field(default_factory=list)
    wm_class: str = ""
    path: Path = None

    @property
    def exec_argv(self) -> List[str]:
        """Nettoie les codes %f, %U, etc. pour Popen."""
        cleaned = _FIELD_CODE_RE.sub("", self.exec_raw).strip()
        try:
            return shlex.split(cleaned)
        except ValueError:
            return cleaned.split()

    @property
    def binary(self) -> str:
        argv = self.exec_argv
        return Path(argv[0]).name if argv else ""


def _parse_desktop_file(fp: Path) -> Optional[AppEntry]:
    try:
        text = fp.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    in_section = False
    fields: Dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            in_section = (line == "[Desktop Entry]")
            continue
        if not in_section or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key not in fields:
            fields[key] = val.strip()

    if fields.get("Type", "Application") != "Application":
        return None
    if fields.get("NoDisplay", "false").lower() == "true":
        return None
    if fields.get("Hidden", "false").lower() == "true":
        return None
    exec_raw = fields.get("Exec", "")
    if not exec_raw:
        return None

    # TryExec : le fichier .desktop déclare lui-même qu'il ne sert à rien
    # tant que ce binaire n'est pas installé. On le retire de l'index au
    # lieu de le laisser échouer au lancement.
    try_exec = fields.get("TryExec", "")
    if try_exec:
        bin_name = try_exec.split()[0] if try_exec.split() else ""
        if bin_name and not kit.which(bin_name):
            return None

    keywords = [k for k in fields.get("Keywords", "").split(";") if k]
    return AppEntry(
        id=fp.stem.lower(),
        name=fields.get("Name", fp.stem),
        generic_name=fields.get("GenericName", ""),
        exec_raw=exec_raw,
        icon=fields.get("Icon", ""),
        terminal=fields.get("Terminal", "false").lower() == "true",
        keywords=keywords,
        wm_class=fields.get("StartupWMClass", ""),
        path=fp,
    )


class _Index:
    def __init__(self) -> None:
        self._entries: List[AppEntry] = []
        self._built_at = 0.0
        self._dir_signature: Tuple = ()

    def _signature(self) -> Tuple:
        sig = []
        for d in _desktop_dirs():
            try:
                sig.append((str(d), d.stat().st_mtime))
            except OSError:
                sig.append((str(d), -1))
        return tuple(sig)

    def _rebuild(self) -> None:
        entries: List[AppEntry] = []
        seen_ids: Set[str] = set()
        for d in _desktop_dirs():
            if not d.is_dir():
                continue
            try:
                files = sorted(d.glob("*.desktop"))
            except OSError:
                continue
            for fp in files:
                if fp.stem.lower() in seen_ids:
                    continue
                entry = _parse_desktop_file(fp)
                if entry is not None:
                    entries.append(entry)
                    seen_ids.add(entry.id)
        self._entries = entries
        self._built_at = time.time()
        self._dir_signature = self._signature()

    def get(self, force: bool = False) -> List[AppEntry]:
        now = time.time()
        stale = force or (now - self._built_at > CACHE_TTL)
        if stale and self._signature() != self._dir_signature:
            self._rebuild()
        elif not self._entries and stale:
            self._rebuild()
        elif stale:
            self._built_at = now
        return self._entries


_index = _Index()


def all_apps(force: bool = False) -> List[AppEntry]:
    return _index.get(force=force)


def _tokens(s: str) -> Set[str]:
    return set(re.findall(r"[a-z0-9]+", s.lower()))


def find_app(query: str, min_score: float = 0.35) -> Optional[AppEntry]:
    """Recherche floue dans l'index des .desktop.
    Signature conservée : ce point d'entrée est importé par d'autres modules
    (open_app) qui attendent un AppEntry ou None."""
    q = (query or "").strip().lower()
    if not q:
        return None
    q_tokens = _tokens(q)
    apps = all_apps()
    if not apps:
        return None
    best: Optional[AppEntry] = None
    best_score = 0.0
    for app in apps:
        haystacks = [app.name, app.generic_name, app.id, app.binary,
                     app.wm_class] + app.keywords
        haystacks = [h for h in haystacks if h]
        score = 0.0
        for h in haystacks:
            hl = h.lower()
            if hl == q:
                score = max(score, 1.0)
            elif q == app.id or q == app.binary.lower():
                score = max(score, 0.98)
            elif hl.startswith(q) or q.startswith(hl):
                score = max(score, 0.85)
            elif q in hl or hl in q:
                score = max(score, 0.65)
            else:
                h_tokens = _tokens(h)
                if q_tokens and h_tokens:
                    overlap = len(q_tokens & h_tokens) / len(q_tokens | h_tokens)
                    score = max(score, overlap * 0.6)
        if score > best_score:
            best_score, best = score, app
    return best if best_score >= min_score else None


# ════════════════════════════════════════════════════════════════════════════
# Alias communs (français/anglais) → listes de candidats essayés en ordre
# ════════════════════════════════════════════════════════════════════════════

_APP_ALIASES: Dict[str, List[str]] = {
    # Navigateurs
    "navigateur": ["google-chrome-stable", "google-chrome"],
    "browser": ["google-chrome-stable", "google-chrome"],
    "web": ["google-chrome-stable", "google-chrome"],
    "internet": ["google-chrome-stable", "google-chrome"],
    # Mail
    "mail": ["thunderbird", "geary"],
    "email": ["thunderbird", "geary"],
    "courriel": ["thunderbird", "geary"],
    # Fichiers
    "fichiers": ["nautilus"],
    "explorateur": ["nautilus"],
    "explorer": ["nautilus"],
    "dossiers": ["nautilus"],
    # Terminaux (kitty en tête : le flag -e n'existe pas chez lui)
    "terminal": ["kitty", "foot", "alacritty", "gnome-terminal", "konsole"],
    "console": ["kitty", "foot", "alacritty"],
    "shell": ["kitty", "foot"],
    # Éditeurs
    "éditeur": ["code", "gedit", "kate", "mousepad"],
    "editeur": ["code", "gedit", "kate", "mousepad"],
    "bloc-notes": ["gedit", "mousepad", "kate"],
    "notepad": ["gedit", "mousepad", "kate"],
    # Utilitaires
    "calculatrice": ["gnome-calculator", "kcalc"],
    "calculette": ["gnome-calculator", "kcalc"],
    "calculator": ["gnome-calculator", "kcalc"],
    "paramètres": ["gnome-control-center", "systemsettings"],
    "parametres": ["gnome-control-center", "systemsettings"],
    "settings": ["gnome-control-center", "systemsettings"],
    # Média
    "musique": ["lollypop", "audacious", "spotify"],
    "music": ["lollypop", "audacious", "spotify"],
    "vidéo": ["vlc", "mpv", "totem"],
    "video": ["vlc", "mpv", "totem"],
    "player": ["vlc", "mpv"],
    "lecteur": ["vlc", "mpv"],
    # Système
    "moniteur": ["gnome-system-monitor", "btop"],
    "task manager": ["gnome-system-monitor", "btop"],
    "gestionnaire de tâches": ["gnome-system-monitor", "btop"],
    "bureau": ["dolphin", "thunar", "nautilus"],
}


def _resolve_aliases(app_name: str) -> List[str]:
    """Nom demandé + tous ses alias, sans doublons, dans l'ordre de priorité."""
    low = (app_name or "").lower().strip()
    seen: List[str] = [app_name]
    for cand in _APP_ALIASES.get(low, []):
        if cand not in seen:
            seen.append(cand)
    return seen


# ════════════════════════════════════════════════════════════════════════════
# Environnement de lancement (Wayland/Hyprland restauré)
# ════════════════════════════════════════════════════════════════════════════

def _launch_env() -> dict:
    """Environnement complet pour lancer une app graphique : restaure
    WAYLAND_DISPLAY/DISPLAY/XDG_RUNTIME_DIR/HYPRLAND_INSTANCE_SIGNATURE
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


# ════════════════════════════════════════════════════════════════════════════
# Détection des apps en cours d'exécution
# ════════════════════════════════════════════════════════════════════════════

def _get_running_app_binaries() -> Set[str]:
    """Ensemble des noms de binaires en cours d'exécution."""
    running: Set[str] = set()
    if _OS in ("Linux", "Darwin"):
        try:
            out = kit.run(["ps", "-eo", "comm"], timeout=2)
            for line in out.stdout.splitlines()[1:]:
                comm = line.strip().lower()
                if comm:
                    running.add(comm)
        except Exception:
            pass
    elif _OS == "Windows":
        try:
            out = kit.run(["tasklist", "/NH", "/FO", "CSV"], timeout=4)
            for line in out.stdout.splitlines():
                parts = line.strip().strip('"').split('","')
                if parts:
                    name = parts[0].lower()
                    if name.endswith(".exe"):
                        name = name[:-4]
                    running.add(name)
        except Exception:
            pass
    return running


def _is_app_running(entry: AppEntry) -> bool:
    """Vérifie si l'application correspondant à l'AppEntry est déjà lancée."""
    binaries = {entry.binary.lower(), entry.id.lower()}
    if entry.wm_class:
        binaries.add(entry.wm_class.lower())
    running = _get_running_app_binaries()
    return bool(binaries & running)


def app_running(app_name: str) -> bool:
    """L'application (ou l'un de ses alias) est-elle ouverte ?"""
    running = _get_running_app_binaries()
    for name in _resolve_aliases(app_name):
        if name.lower() in running:
            return True
        entry = find_app(name)
        if entry and _is_app_running(entry):
            return True
    return False


# ════════════════════════════════════════════════════════════════════════════
# Lancement (vérifié, détaché, Wayland-aware)
# ════════════════════════════════════════════════════════════════════════════

# Terminaux préférés avec leur flag d'exécution :
# kitty n'accepte PAS -e (commande en arguments directs),
# gnome-terminal/wezterm utilisent --, les autres -e.
_TERMINAL_CANDIDATES: List[Tuple[str, Optional[str]]] = [
    ("kitty", None),
    ("foot", "-e"),
    ("alacritty", "-e"),
    ("wezterm", "--"),
    ("gnome-terminal", "--"),
    ("konsole", "-e"),
    ("xfce4-terminal", "-e"),
    ("mate-terminal", "-e"),
    ("xterm", "-e"),
]


def _run_in_terminal(argv: List[str], env: dict) -> bool:
    for term, flag in _TERMINAL_CANDIDATES:
        path = kit.which(term)
        if not path:
            continue
        cmd = [path] + ([flag] if flag else []) + argv
        if kit.spawn(cmd, env=env) is not None:
            return True
    return False


def _confirm_started(proc: subprocess.Popen, settle: float = 0.6) -> bool:
    """Vérifie que le processus a réellement démarré : s'il tient plus de
    `settle` secondes il est lancé ; s'il sort avant, on exige un code 0
    (les wrappers comme gtk-launch sortent immédiatement avec 0)."""
    deadline = time.monotonic() + settle
    while time.monotonic() < deadline:
        rc = proc.poll()
        if rc is not None:
            return rc == 0
        time.sleep(0.05)
    return True


def _launch_entry(entry: AppEntry, no_wait: bool = True) -> str:
    env = _launch_env()
    already = _is_app_running(entry)
    argv = entry.exec_argv
    if entry.terminal:
        if not _run_in_terminal(argv, env):
            return (f"Impossible de lancer {entry.name} : aucun terminal "
                    f"installé (kitty, foot, alacritty…).")
        return f"Lancement de {entry.name} dans un terminal."
    try:
        if no_wait:
            proc = subprocess.Popen(argv, cwd=str(Path.home()), stdin=DEVNULL,
                                    stdout=DEVNULL, stderr=DEVNULL,
                                    start_new_session=True, env=env)
        else:
            res = kit.run(argv, cwd=str(Path.home()), env=env, timeout=15)
            if not res:
                raise RuntimeError(res.reason())
            return f"Lancement de {entry.name}."
    except Exception as e:
        return f"Erreur au lancement de {entry.name} : {e}"
    if not _confirm_started(proc):
        return (f"Échec du lancement de {entry.name} "
                f"(arrêt immédiat, code {proc.returncode}).")
    note = " (déjà ouvert)" if already else ""
    return f"Lancement de {entry.name}{note}."


def _launch_binary(binary: str, name: str, no_wait: bool = True) -> str:
    env = _launch_env()
    try:
        if no_wait:
            proc = subprocess.Popen([binary], cwd=str(Path.home()),
                                    stdin=DEVNULL, stdout=DEVNULL,
                                    stderr=DEVNULL, start_new_session=True,
                                    env=env)
        else:
            res = kit.run([binary], cwd=str(Path.home()), env=env, timeout=15)
            if not res:
                raise RuntimeError(res.reason())
            return f"Lancement de {name}."
    except Exception as e:
        return f"Erreur au lancement de {name} : {e}"
    if not _confirm_started(proc):
        return (f"Échec du lancement de {name} "
                f"(arrêt immédiat, code {proc.returncode}).")
    return f"Lancement de {name}."


def launch_app(app_name: str, no_wait: bool = True) -> str:
    """
    Lance une application par son nom (recherche intelligente).
    Chaîne : alias → index .desktop → binaire en PATH.
    Retourne un message de confirmation.
    """
    from core.browser_policy import BROWSER_ALIASES
    if app_name.lower().strip() in BROWSER_ALIASES:
        app_name = "navigateur"
    resolved_names = _resolve_aliases(app_name)
    for name in resolved_names:
        # 1. Index .desktop (riche : terminal, arguments, gtk-launch…)
        entry = find_app(name)
        if entry:
            return _launch_entry(entry, no_wait=no_wait)
        # 2. Binaire directement dans le PATH (apps sans .desktop : btop…)
        binary = kit.which(name) or kit.which(name.lower())
        if binary:
            return _launch_binary(binary, name, no_wait=no_wait)

    if _OS == "Darwin":
        try:
            kit.spawn(["open", "-a", resolved_names[0]])
            return f"Lancement de {resolved_names[0]} (via open)."
        except Exception:
            pass
    elif _OS == "Windows":
        for name in resolved_names:
            exe = kit.which(name) or kit.which(name + ".exe")
            if exe:
                flags = getattr(subprocess, "DETACHED_PROCESS", 0)
                subprocess.Popen([exe], creationflags=flags)
                return f"Lancement de {name}."

    tried = ", ".join(resolved_names)
    return (f"Application '{app_name}' introuvable (essayé : {tried}). "
            f"Vérifiez qu'elle est installée.")


# ════════════════════════════════════════════════════════════════════════════
# Parsing local des commandes de lancement
# ════════════════════════════════════════════════════════════════════════════

def _parse_launch_command_locally(text: str) -> Optional[Dict]:
    """
    Extrait le nom de l'application à lancer d'une phrase naturelle.
    Ex : « lance firefox », « ouvre le terminal », « joue spotify ».
    Retourne {'app_name': '...'} ou None.
    """
    text = re.sub(r"\s+", " ", (text or "").lower().strip())
    text = re.sub(r"\b(s'il te pla[iî]t|stp|please|peux[- ]tu|tu peux|"
                  r"je veux|je voudrais|j'aimerais)\b", " ", text).strip()
    patterns = [
        r"(?:lance|ouvre|démarre|demarre|exécute|execute|start|open|run|"
        r"launch|play|joue)\s+(?:l'application\s+|l'app\s+|le\s+programme\s+|"
        r"la\s+|le\s+|les\s+)?(.+?)$",
        r"^(.+?)\s+(?:lance|ouvre|start|open)$",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            app = m.group(1).strip().strip("'\"")
            app = re.sub(r"\s+(?:stp|s'il te pla[iî]t|please)$", "", app)
            if app:
                return {"app_name": app}
    if len(text.split()) == 1 and text:
        return {"app_name": text}
    return None


def _get_base_dir() -> Path:
    import sys
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def _get_api_key() -> str:
    try:
        base = _get_base_dir()
        config = base / "config" / "api_keys.json"
        with open(config, "r", encoding="utf-8") as f:
            return json.load(f).get("gemini_api_key", "")
    except Exception:
        return ""


def _detect_launch_intent_ai(description: str) -> Optional[Dict]:
    api_key = _get_api_key()
    if not api_key:
        return None
    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        prompt = (
            f"Extrais le nom de l'application à lancer de la phrase suivante. "
            f"Réponds UNIQUEMENT par un objet JSON avec la clé 'app_name'. "
            f"Phrase : \"{description}\""
        )
        resp = client.models.generate_content(model=FAST_MODEL,
                                              contents=prompt)
        match = re.search(r'\{.*\}', resp.text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
    except Exception as e:
        print(f"[desktop_apps] AI error: {e}")
    return None


# ════════════════════════════════════════════════════════════════════════════
# Point d'entrée principal
# ════════════════════════════════════════════════════════════════════════════

_ACTION_ALIASES = {
    "launch": "launch", "open": "launch", "lance": "launch",
    "ouvre": "launch", "start": "launch",
    "find": "find", "trouve": "find", "cherche": "find", "search": "find",
    "list": "list", "liste": "list",
    "status": "status", "etat": "status", "état": "status",
}


def app_control(parameters: dict = None, response=None, player=None,
                session_memory=None) -> str:
    """
    Contrôle des applications : lancement, recherche, liste, statut.
    Paramètres acceptés :
      action      : "launch" (défaut) | "find" | "list" | "status"
      app_name    : nom de l'application
      description : phrase naturelle
    """
    params = parameters or {}
    raw_action = str(params.get("action", "launch") or "launch").lower().strip()
    action = _ACTION_ALIASES.get(raw_action, raw_action)
    description = str(params.get("description", "") or "").strip()
    app_name = str(params.get("app_name", "") or "").strip()

    # Interprétation d'une description en langage naturel
    if description and not app_name:
        local = _parse_launch_command_locally(description)
        if local:
            app_name = local["app_name"]
        else:
            ai = _detect_launch_intent_ai(description)
            if ai:
                app_name = ai.get("app_name", "") or ""
            else:
                return ("Je n'ai pas compris quelle application lancer. "
                        "Pouvez-vous reformuler ?")

    if action in ("launch", "find", "status") and not app_name:
        return "Aucun nom d'application fourni."

    try:
        if action == "launch":
            result = launch_app(app_name)
            if player:
                try:
                    player.write_log(f"[app] lancement {app_name}")
                except Exception:
                    pass
            return result
        elif action == "find":
            entry = find_app(app_name)
            if entry:
                return (f"Application trouvée : {entry.name} ({entry.id}) "
                        f"— {entry.exec_raw}")
            binary = kit.which(app_name) or kit.which(app_name.lower())
            if binary:
                return f"Application trouvée dans le PATH : {binary}"
            return f"Aucune application trouvée pour '{app_name}'."
        elif action == "list":
            apps = all_apps()
            lines = [f"{a.name} ({a.id})" for a in apps[:20]]
            return (f"Applications installées ({len(apps)} au total, "
                    f"20 premières) :\n" + "\n".join(lines))
        elif action == "status":
            if app_running(app_name):
                return f"✅ {app_name} est ouvert."
            return f"❌ {app_name} n'est pas lancé."
        else:
            return (f"Action '{raw_action}' non supportée. "
                    f"Utilisez 'launch', 'find', 'list' ou 'status'.")
    except Exception as e:
        return f"Erreur : {e}"


# ════════════════════════════════════════════════════════════════════════════
# Tests directs
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        print(app_control({"description": " ".join(sys.argv[1:])}))
    else:
        print("Usage: python desktop_apps.py \"lance firefox\"")
