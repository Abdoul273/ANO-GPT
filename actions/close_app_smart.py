"""
🔒 CLOSE_APP_SMART — Fermeture intelligente d'instances d'applications,
version renforcée pour Hyprland/Wayland.

Liste les instances, demande laquelle fermer, ferme de façon chirurgicale.

Corrections clés par rapport à l'ancienne version :
- l'ancienne version « fermait une instance » en tuant son PID : pour les
  apps multi-fenêtres mono-processus (kitty, navigateurs…), cela fermait
  TOUTES les fenêtres — l'inverse du but. On ferme désormais la FENÊTRE
  par son adresse Hyprland (closewindow → vérification → killwindow),
  et on ne tue le processus que s'il n'a aucune fenêtre (ou en escalade) ;
- « ✅ Fermé (forcé) » était annoncé même quand kill échouait : chaque
  fermeture est maintenant vérifiée réellement (la fenêtre a disparu /
  le processus est mort) ;
- hyprctl était appelé dans le mauvais ordre d'arguments et sans
  HYPRLAND_INSTANCE_SIGNATURE : corrigé, avec restauration d'env ;
- matching par mots entiers sur classe/initialClass/titre + alias
  (« chrome » retrouve « google-chrome-stable ») ;
- sélection robuste : « 1 », « 1-3 », « 2,4 », « la dernière »,
  « le deuxième », « toutes » — avec mémoire des candidats entre deux
  tours via session_memory ;
- SIGTERM d'abord, SIGKILL seulement ensuite ; jamais sur PID 1 ni sur
  le processus de l'assistant ;
- filtre optionnel par bureau (« ferme les kitty du bureau 2 »).
"""
import os
import re
import shutil
import subprocess
from core import action_kit as kit
import time
import unicodedata
from typing import Dict, List, Optional, Tuple

# ────────────────────────────────────────────────────────────────────────────
# core.tool_utils : utilisé si présent, sinon fallbacks locaux autonomes
# ────────────────────────────────────────────────────────────────────────────
try:
    from core.tool_utils import (
        tracked_tool,
        CommandExecutor,
        handle_tool_error,
    )
    _HAS_TOOL_UTILS = True
except Exception:
    _HAS_TOOL_UTILS = False

    def tracked_tool(fn):
        def wrapper(*args, **kwargs):
            return fn(*args, **kwargs)
        wrapper.__name__ = getattr(fn, "__name__", "tool")
        return wrapper

    class CommandExecutor:
        @staticmethod
        def run(cmd: str, timeout: float = 5):
            try:
                p = subprocess.run(cmd, shell=True, capture_output=True,
                                   text=True, timeout=timeout)
                return p.returncode == 0, p.stdout or "", p.stderr or ""
            except subprocess.TimeoutExpired:
                return False, "", "timeout"
            except Exception as e:
                return False, "", str(e)

    def handle_tool_error(e: Exception, ctx: str) -> str:
        return f"❌ Erreur ({ctx}) : {e}"


_PENDING_KEY = "close_app_smart_pending"


# ────────────────────────────────────────────────────────────────────────────
# 🖥️ Environnement Hyprland restauré
# ────────────────────────────────────────────────────────────────────────────

def _hypr_env() -> dict:
    env = {**os.environ}
    if not env.get("XDG_RUNTIME_DIR"):
        try:
            cand = f"/run/user/{os.getuid()}"
            if os.path.isdir(cand):
                env["XDG_RUNTIME_DIR"] = cand
        except Exception:
            pass
    if not env.get("HYPRLAND_INSTANCE_SIGNATURE"):
        try:
            rd = env.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
            hypr_dir = os.path.join(rd, "hypr")
            if os.path.isdir(hypr_dir):
                inst = sorted(
                    (os.path.join(hypr_dir, d) for d in os.listdir(hypr_dir)
                     if os.path.isdir(os.path.join(hypr_dir, d))),
                    key=lambda p: os.path.getmtime(p), reverse=True)
                if inst:
                    env["HYPRLAND_INSTANCE_SIGNATURE"] = os.path.basename(inst[0])
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


def _window_exists(addr: str) -> bool:
    clients = _hyprctl_json("clients")
    if not isinstance(clients, list):
        return False
    return any(c.get("address") == addr for c in clients)


# ────────────────────────────────────────────────────────────────────────────
# 🗂️ Alias & correspondance de noms
# ────────────────────────────────────────────────────────────────────────────

_APP_ALIASES: Dict[str, List[str]] = {
    "chrome": ["google-chrome-stable", "google-chrome", "chromium"],
    "firefox": ["firefox", "firefox-esr"],
    "edge": ["microsoft-edge-stable", "microsoft-edge", "msedge"],
    "brave": ["brave-browser", "brave"],
    "opera": ["opera", "opera-gx"],
    "vivaldi": ["vivaldi", "vivaldi-stable"],
    "terminal": ["kitty", "alacritty", "foot", "wezterm", "gnome-terminal",
                 "konsole", "xfce4-terminal", "xterm"],
    "kitty": ["kitty"],
    "code": ["code", "code-oss", "visual-studio-code"],
    "vscode": ["code", "code-oss"],
    "telegram": ["telegram-desktop"],
    "discord": ["discord"],
    "slack": ["slack"],
    "whatsapp": ["whatsapp-nativefier", "zapzap"],
    "spotify": ["spotify"],
    "vlc": ["vlc"],
    "mpv": ["mpv"],
    "dolphin": ["dolphin"],
    "nautilus": ["nautilus"],
    "thunar": ["thunar"],
    "explorateur": ["dolphin", "nautilus", "thunar", "pcmanfm"],
    "navigateur": ["google-chrome-stable", "chromium", "firefox",
                   "microsoft-edge", "brave-browser"],
}


def _name_tokens(app_name: str) -> set:
    """Étend un nom d'app en tous les tokens à chercher (alias inclus)."""
    low = (app_name or "").lower().strip()
    tokens = {low}
    if low in _APP_ALIASES:
        tokens.update(_APP_ALIASES[low])
    else:
        for alias, binaries in _APP_ALIASES.items():
            if low in binaries:
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
    """Correspondance par MOTS entiers sur classe/titre — pas de substring
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


# ────────────────────────────────────────────────────────────────────────────
# 🌐 Extraction workspace (chiffres ET ordinaux)
# ────────────────────────────────────────────────────────────────────────────

_WS = r"(?:bureau|workspace|ws|espace\s+de\s+travail|desktop)"
_PREP = r"(?:dans\s+(?:le|la)?|au|sur\s+(?:le|la)?|du|de\s+|le|la)?\s*"
_ORD_FR = (
    r"premi(?:er|ère|ere|re)|deuxi[èe]me|second[e]?|troisi[èe]me|quatri[èe]me|"
    r"cinqui[èe]me|sixi[èe]me|septi[èe]me|huiti[èe]me|neuvi[èe]me|dixi[èe]me"
)


def _ordinal_to_int(word: str) -> Optional[int]:
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
    """« 4 », « bureau 4 », « deuxième bureau » → « 4 » (ou None)."""
    if value is None:
        return None
    v = str(value).strip()
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


# ────────────────────────────────────────────────────────────────────────────
# ⚙️ Processus : recherche bornée + terminaison propre et vérifiée
# ────────────────────────────────────────────────────────────────────────────

def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _terminate_pid(pid: int, force: bool = False) -> bool:
    """SIGTERM → attente → SIGKILL, avec vérification réelle de la mort.
    Jamais sur PID 1 ni sur le processus de l'assistant."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 1 or pid == os.getpid():
        return False
    if not _pid_alive(pid):
        return True
    if not force:
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


def _find_pids(tokens: set) -> List[int]:
    """pgrep -f borné (^|/)nom( |$), repli scan /proc. Jamais soi-même/1."""
    pids = set()
    my_pid = os.getpid()
    if shutil.which("pgrep"):
        for t in tokens:
            t = t.strip().lower()
            if not t:
                continue
            pat = re.escape(t) if " " in t else rf"(^|/){re.escape(t)}( |$)"
            try:
                r = subprocess.run(["pgrep", "-f", pat], capture_output=True,
                                   text=True, timeout=3)
                for line in (r.stdout or "").splitlines():
                    if line.strip().isdigit():
                        pids.add(int(line.strip()))
            except Exception:
                continue
    if not pids:
        # Repli : scan /proc (égalité exacte sur les noms d'arguments)
        proc_root = "/proc"
        if os.path.isdir(proc_root):
            for entry in os.listdir(proc_root):
                if not entry.isdigit():
                    continue
                pid = int(entry)
                if pid in (my_pid, 1):
                    continue
                try:
                    with open(f"{proc_root}/{entry}/cmdline", "rb") as f:
                        args = [a.decode(errors="replace") for a in
                                f.read().split(b"\x00") if a]
                    names = set()
                    for a in args:
                        names.add(os.path.basename(a).lower())
                        names.add(a.lower())
                    if names & tokens:
                        pids.add(pid)
                except Exception:
                    continue
    return [p for p in pids if p not in (my_pid, 1)]


# ────────────────────────────────────────────────────────────────────────────
# 🔍 Détection des instances (fenêtres Hyprland + processus sans fenêtre)
# ────────────────────────────────────────────────────────────────────────────

def get_app_instances(app_name: str, workspace=None) -> List[Dict[str, str]]:
    """Toutes les instances d'une app : fenêtres Hyprland d'abord (avec leur
    adresse pour une fermeture chirurgicale), puis processus sans fenêtre."""
    app_name_lower = (app_name or "").lower().strip()
    tokens = _name_tokens(app_name_lower)
    ws_filter = _parse_workspace_value(workspace)

    instances: List[Dict[str, str]] = []

    # 1. Fenêtres Hyprland (le bon ordre d'arguments, cette fois)
    clients = _hyprctl_json("clients")
    if isinstance(clients, list):
        for client in clients:
            if not isinstance(client, dict) or not _match_client(client, tokens):
                continue
            c_ws = (client.get("workspace") or {}).get("id")
            if ws_filter is not None and str(c_ws) != str(ws_filter):
                continue
            instances.append({
                "class": client.get("class", ""),
                "title": client.get("title", "") or client.get("initialTitle", ""),
                "pid": str(client.get("pid", "")),
                "workspace": str(c_ws) if c_ws is not None else "?",
                "address": client.get("address", ""),
                "source": "window",
            })

    # 2. Repli : processus sans fenêtre (apps headless, tray…)
    if not instances and ws_filter is None:
        for pid in _find_pids(tokens):
            title = ""
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    args = [a.decode(errors="replace") for a in
                            f.read().split(b"\x00") if a]
                title = " ".join(args)[:80]
            except Exception:
                pass
            instances.append({
                "class": app_name,
                "title": title or app_name,
                "pid": str(pid),
                "workspace": "?",
                "address": "",
                "source": "process",
            })
    return instances


def _instance_label(inst: Dict[str, str]) -> str:
    title = (inst.get("title") or inst.get("class") or "?").strip()
    ws = inst.get("workspace")
    label = title[:70]
    if ws and ws != "?":
        label += f" (bureau {ws})"
    return label


# ────────────────────────────────────────────────────────────────────────────
# ❌ Fermeture chirurgicale (fenêtre d'abord, processus en dernier recours)
# ────────────────────────────────────────────────────────────────────────────

def _close_window_by_address(addr: str, force: bool = False) -> bool:
    """Ferme UNE fenêtre avec vérification réelle : elle n'est déclarée
    fermée que si son adresse a disparu de Hyprland."""
    if not addr:
        return False
    if not _window_exists(addr):
        return True
    order = ["killwindow"] if force else ["closewindow", "killwindow"]
    for dispatcher in order:
        _hypr_dispatch(dispatcher, f"address:{addr}")
        time.sleep(0.15)
        if not _window_exists(addr):
            return True
    return False


def _close_instance(inst: Dict[str, str], force: bool = False) -> Tuple[bool, str]:
    """Ferme UNE instance. Les apps multi-fenêtres mono-processus (kitty,
    navigateurs…) sont fermées PAR FENÊTRE : tuer le PID aurait fermé toutes
    les fenêtres, l'inverse du but. Le PID n'est tué que pour les processus
    sans fenêtre ou en escalade d'une fenêtre récalcitrante."""
    label = _instance_label(inst)
    addr = inst.get("address", "")
    if addr:
        if _close_window_by_address(addr, force=force):
            return True, label
        # Fenêtre récalcitrante : escalade via le processus (dernier
        # recours — emporte les autres fenêtres du même processus).
    pid = inst.get("pid", "")
    if pid and str(pid).isdigit():
        if _terminate_pid(int(pid), force=force):
            return True, label
    return False, label


# ────────────────────────────────────────────────────────────────────────────
# 🧠 Mémoire de désambiguïsation (entre deux tours)
# ────────────────────────────────────────────────────────────────────────────

def _sm_get(session_memory, key, default=None):
    if session_memory is None:
        return default
    try:
        return session_memory.get(key, default)
    except Exception:
        try:
            return getattr(session_memory, key, default)
        except Exception:
            return default


def _sm_set(session_memory, key, value):
    if session_memory is None:
        return
    try:
        session_memory[key] = value
        return
    except Exception:
        pass
    try:
        session_memory.set(key, value)
    except Exception:
        pass


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s or "")
                   if not unicodedata.combining(c))


_ORDINALS = {
    "premier": 0, "premiere": 0, "1er": 0, "1ere": 0,
    "deuxieme": 1, "second": 1, "seconde": 1, "deux": 1,
    "troisieme": 2, "trois": 2,
    "quatrieme": 3, "quatre": 3,
    "cinquieme": 4, "cinq": 4,
}


def _parse_selection(query: str, count: int) -> Optional[List[int]]:
    """Transforme une réponse utilisateur en indices (0-based) valides :
    « 1 », « 1-3 », « 2,4 », « 1 et 3 », « la dernière », « le deuxième »,
    « toutes ». Renvoie None si incompréhensible."""
    if not query or count <= 0:
        return None
    q = _strip_accents(str(query).lower())
    if re.search(r"\btous?\b|\ball\b|\bchaque\b", q):
        return list(range(count))
    if "derni" in q:
        return [count - 1]
    for word, idx in _ORDINALS.items():
        if re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", q):
            return [idx] if idx < count else None
    indices: List[int] = []
    # Plages « 1-3 » (tolère espaces, tiret long, « à »)
    for m in re.finditer(r"(\d+)\s*[-–à]\s*(\d+)", q):
        a, b = int(m.group(1)), int(m.group(2))
        lo, hi = min(a, b), max(a, b)
        indices.extend(range(lo - 1, hi))
    without_ranges = re.sub(r"(\d+)\s*[-–à]\s*(\d+)", " ", q)
    for m in re.finditer(r"\b(\d+)\b", without_ranges):
        indices.append(int(m.group(1)) - 1)
    out: List[int] = []
    for i in indices:
        if 0 <= i < count and i not in out:
            out.append(i)
    return out or None


# ────────────────────────────────────────────────────────────────────────────
# 📋 API : lister / fermer
# ────────────────────────────────────────────────────────────────────────────

@tracked_tool
def list_instances(app_name: str, workspace=None) -> str:
    """Liste toutes les instances d'une app en cours."""
    app_name = app_name.strip()
    instances = get_app_instances(app_name, workspace)
    if not instances:
        ws_note = f" sur le bureau {workspace}" if workspace is not None else ""
        return f"❌ Aucune instance de '{app_name}' trouvée{ws_note}"
    if len(instances) == 1:
        return f"✅ 1 instance de '{app_name}' trouvée (fermable directement)"
    result = f"📋 {len(instances)} instance(s) de '{app_name}':\n"
    for i, inst in enumerate(instances, 1):
        title = (inst.get("title") or inst.get("class") or "?")[:60]
        workspace_txt = inst.get("workspace", "?")
        pid = inst.get("pid", "?")
        result += f"\n  {i}. {title}\n     (PID: {pid}, Bureau: {workspace_txt})"
    result += "\n\nLaquelle/Lesquelles fermer ? (ex: '1', '1-2', '2,3', 'la dernière' ou 'toutes')"
    return result


@tracked_tool
def close_instance(app_name: str, instance_number, session_memory=None,
                   force: bool = False) -> str:
    """Ferme une instance précise par son numéro."""
    app_name = app_name.strip()
    pending = _sm_get(session_memory, _PENDING_KEY)
    if pending and pending.get("app", "").lower() == app_name.lower() \
            and pending.get("instances"):
        instances = pending["instances"]
    else:
        instances = get_app_instances(app_name)
    if not instances:
        return f"❌ Aucune instance de '{app_name}'"
    try:
        n = int(instance_number)
    except (TypeError, ValueError):
        return "❌ Numéro d'instance invalide"
    if n < 1 or n > len(instances):
        return f"❌ Instance {n} invalide (max: {len(instances)})"
    ok, label = _close_instance(instances[n - 1], force=force)
    if ok:
        _sm_set(session_memory, _PENDING_KEY, None)
        return f"✅ Fermé : {label}"
    return f"⚠️ Impossible de fermer : {label}"


@tracked_tool
def close_instances_by_range(app_name: str, instance_range: str,
                             session_memory=None, force: bool = False) -> str:
    """Ferme plusieurs instances : '1-3', '1,3', 'la dernière', 'toutes'…"""
    app_name = app_name.strip()
    pending = _sm_get(session_memory, _PENDING_KEY)
    if pending and pending.get("app", "").lower() == app_name.lower() \
            and pending.get("instances"):
        instances = pending["instances"]
    else:
        instances = get_app_instances(app_name)
    if not instances:
        return f"❌ Aucune instance de '{app_name}'"
    indices = _parse_selection(instance_range, len(instances))
    if indices is None:
        return (f"❌ Format invalide : {instance_range} "
                f"(ex: '1-2', '1,3', 'la dernière' ou 'toutes')")
    return _close_selected(instances, indices, session_memory, force)


def _close_selected(instances: List[Dict[str, str]], indices: List[int],
                    session_memory=None, force: bool = False) -> str:
    """Ferme les instances sélectionnées et rapporte précisément le résultat."""
    closed: List[str] = []
    failed: List[str] = []
    for idx in indices:
        if 0 <= idx < len(instances):
            ok, label = _close_instance(instances[idx], force=force)
            (closed if ok else failed).append(label)
        time.sleep(0.05)
    if closed:
        _sm_set(session_memory, _PENDING_KEY, None)
    parts = []
    if closed:
        parts.append(f"✅ Fermé {len(closed)} instance(s) :\n  " + "\n  ".join(closed))
    if failed:
        parts.append(f"⚠️ {len(failed)} instance(s) n'ont pas pu être fermée(s) :\n  "
                     + "\n  ".join(failed))
    return "\n".join(parts) if parts else "❌ Aucune instance fermée"


# ────────────────────────────────────────────────────────────────────────────
# 🎯 SMART CLOSE (décision automatique ou question)
# ────────────────────────────────────────────────────────────────────────────

@tracked_tool
def close_app_smart(app_name: str, workspace=None, session_memory=None,
                    force: bool = False) -> str:
    """
    Fermeture intelligente :
    - 0 instance → message clair ;
    - 1 instance → fermée directement, sans question ;
    - plusieurs  → liste + question, avec mémorisation des candidats pour
                   que « 2 » / « 1-3 » / « toutes » soit résolu au tour suivant.
    """
    app_name = app_name.strip()
    instances = get_app_instances(app_name, workspace)
    if not instances:
        ws_note = f" sur le bureau {workspace}" if workspace is not None else ""
        return f"❌ Application '{app_name}' non trouvée{ws_note}"
    if len(instances) == 1:
        ok, label = _close_instance(instances[0], force=force)
        if ok:
            _sm_set(session_memory, _PENDING_KEY, None)
            return f"✅ Fermé : {label}"
        return f"⚠️ Impossible de fermer : {label}"
    # Plusieurs instances : on pose la question et on mémorise les candidats.
    _sm_set(session_memory, _PENDING_KEY,
            {"app": app_name, "instances": instances})
    result = f"📋 {len(instances)} instance(s) de '{app_name}':\n"
    for i, inst in enumerate(instances, 1):
        title = (inst.get("title") or inst.get("class") or "?")[:60]
        workspace_txt = inst.get("workspace", "?")
        pid = inst.get("pid", "?")
        result += f"\n  {i}. {title}\n     (PID: {pid}, Bureau: {workspace_txt})"
    result += "\n\nLaquelle/Lesquelles fermer ? (ex: '1', '1-2', '2,3', 'la dernière' ou 'toutes')"
    return result


# ────────────────────────────────────────────────────────────────────────────
# 🎮 ROUTAGE PRINCIPAL
# ────────────────────────────────────────────────────────────────────────────

_ACTION_ALIASES = {
    "close": "close", "fermer": "close", "ferme": "close", "quit": "close",
    "list": "list", "liste": "list", "lister": "list",
    "close_instance": "close_instance", "ferme_instance": "close_instance",
    "close_range": "close_range", "ferme_range": "close_range",
    "range": "close_range",
}


def close_app_smart_main(parameters: dict, player=None, session_memory=None) -> str:
    """
    Fermeture intelligente d'applications avec gestion d'instances.
    Actions :
      - close(app_name)                    — fermeture smart (question si multiple)
      - list(app_name)                     — liste toutes les instances
      - close_instance(app_name, number)   — ferme une instance précise
      - close_range(app_name, range)       — ferme '1-3', '1,3', 'toutes'…
    Options : workspace (chiffre ou ordinal), force (fermeture forcée).
    """
    params = parameters or {}
    raw_action = str(params.get("action", "close") or "close").lower().strip()
    action = _ACTION_ALIASES.get(raw_action, raw_action)
    app_name = str(params.get("app_name", "") or "").strip()
    value = str(params.get("value", "") or "").strip()
    workspace = params.get("workspace")
    force = bool(params.get("force", False))

    if not app_name:
        return "❌ Veuillez spécifier une application"
    if player:
        try:
            player.write_log(f"[close_app_smart] {action} {app_name} {value}")
        except Exception:
            pass

    # ── Réponse à une question posée au tour précédent ? ─────────────────
    # (« 2 », « 1-3 », « la dernière », « toutes » pour les candidats en attente)
    pending = _sm_get(session_memory, _PENDING_KEY)
    if pending and pending.get("app", "").lower() == app_name.lower() \
            and pending.get("instances"):
        sel = value or str(params.get("description", "") or "").strip()
        if sel:
            indices = _parse_selection(sel, len(pending["instances"]))
            if indices is not None:
                return _close_selected(pending["instances"], indices,
                                       session_memory, force)

    try:
        if action == "close":
            return close_app_smart(app_name, workspace, session_memory, force)
        elif action == "list":
            return list_instances(app_name, workspace)
        elif action == "close_instance":
            try:
                instance_num = int(value) if value else 1
            except ValueError:
                return "❌ Numéro d'instance invalide"
            return close_instance(app_name, instance_num, session_memory, force)
        elif action == "close_range":
            return close_instances_by_range(app_name, value or "1",
                                            session_memory, force)
        else:
            return f"❓ Action inconnue: {action}"
    except Exception as e:
        return handle_tool_error(e, "close_app_smart")


# ────────────────────────────────────────────────────────────────────────────
# Test direct
# ────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3:
        print(close_app_smart_main({"action": sys.argv[1],
                                    "app_name": sys.argv[2]}))
    elif len(sys.argv) == 2:
        print(close_app_smart_main({"action": "close", "app_name": sys.argv[1]}))
    else:
        print("Usage: close_app_smart.py <close|list|close_instance|close_range> <app>")
