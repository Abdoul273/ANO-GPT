"""
close_app.py – Ultimate Process & Window Killer, version ultra robuste.
Fermeture intelligente sans pkill, multi‑OS, compatible JARVIS.

Améliorations clés :
- Extraction du workspace par chiffres ET ordinaux (« au deuxième bureau »,
  « 2e bureau », « bureau n°4 », « workspace numéro trois »…).
- Réponse intelligente quand la fenêtre n'est pas sur le bureau demandé :
  localisation réelle des fenêtres + proposition de fermeture.
- Scan natif hyprctl clients (indépendant de window_instances) avec
  matching par mots entiers (pas de faux positifs).
- Fermeture vérifiée : une fenêtre n'est déclarée fermée que si son
  adresse a réellement disparu de Hyprland, avec escalade
  closewindow → killwindow.
- Une app précise (« kitty ») ne peut plus déclencher la fermeture de
  toute une catégorie (« terminal »).
- pgrep borné par début/fin de nom d'exécutable (plus de massacre
  collatéral).
"""
import os
import platform
from core import action_kit as kit
import time
import logging
import re
import json
import shutil
import unicodedata
from pathlib import Path
from typing import Tuple, Set, List, Optional, Dict
from core.live_model_policy import FAST_MODEL

logger = logging.getLogger("JARVIS.close_app")
_SYSTEM = platform.system()

try:
    from actions.window_instances import (
        get_instance,
        unregister_instance,
        find_windows as _hypr_find_windows,
        describe_window as _hypr_describe_window,
        close_window as _hypr_close_window,
    )
    _HAS_WINDOW_INSTANCES = True
except ImportError:
    _HAS_WINDOW_INSTANCES = False
    logger.warning("window_instances introuvable : ciblage par surnom et désambiguïsation désactivés.")

try:
    from actions import launch_tracker as _tracker
    _HAS_TRACKER = True
except ImportError:
    _HAS_TRACKER = False
    logger.warning("launch_tracker introuvable : ciblage « celle que tu viens d'ouvrir » désactivé.")

_PENDING_KEY = "close_app_pending_candidates"


# ════════════════════════════════════════════════════════════════════════════
# Déixis : à quelle fenêtre l'utilisateur fait-il référence ?
# ════════════════════════════════════════════════════════════════════════════

# « ferme kitty que tu viens d'ouvrir » → une seule fenêtre, la dernière lancée.
_RE_LAST = re.compile(
    r"\b(?:que\s+)?(?:tu\s+)?(?:viens|vient)\s+d[e']\s*(?:ouvrir|lancer|d[ée]marrer)\b"
    r"|\b(?:que\s+)?tu\s+(?:as\s+|viens\s+d[e']\s*)?(?:ouvert|ouvrir|lanc[ée]|lancer)[es]*\b"
    r"|\b(?:le|la)\s+derni[èe]re?\b"
    r"|\bderni[èe]re?\s+(?:fen[êe]tre|instance|onglet)\b"
    r"|\bnouvelle?\s+(?:fen[êe]tre|instance)\b"
    r"|\bcelle?\s+que\s+tu\b",
    re.IGNORECASE,
)

# « ferme cette fenêtre », « ferme ça » → la fenêtre actuellement focalisée.
_RE_ACTIVE = re.compile(
    r"\b(?:cette|ce|cet)\s+(?:fen[êe]tre|application|appli|programme|onglet)\b"
    r"|\b(?:celle|celui)[-\s]?(?:ci|l[àa])\b"
    r"|\bfen[êe]tre\s+(?:active|courante|actuelle|au\s+premier\s+plan)\b"
    r"|\bferme\s+[çc]a\b"
    r"|\bici\b",
    re.IGNORECASE,
)

# « ferme toutes les fenêtres kitty » → comportement de masse, explicitement demandé.
_RE_ALL = re.compile(
    r"\btoutes?\s+les\b|\btous\s+les\b|\bchaque\b|\bcompl[èe]tement\b"
    r"|\bpartout\b|\bl[ea]s\s+\d+\s+fen[êe]tres?\b|\ball\b",
    re.IGNORECASE,
)


def _detect_scope(*texts: str) -> Optional[str]:
    """Détermine la portée voulue : 'all', 'last', 'active', ou None (ambigu).
    'all' est testé en premier : « ferme toutes les fenêtres » est une demande
    explicite de fermeture de masse et prime sur le reste."""
    blob = " ".join(t for t in texts if t)
    if not blob.strip():
        return None
    if _RE_ALL.search(blob):
        return "all"
    if _RE_LAST.search(blob):
        return "last"
    if _RE_ACTIVE.search(blob):
        return "active"
    return None


def _strip_deixis(text: str) -> str:
    """Retire les tournures de déixis pour ne garder que le nom de l'app."""
    if not text:
        return text
    for rx in (_RE_ALL, _RE_LAST, _RE_ACTIVE):
        text = rx.sub(" ", text)
    text = re.sub(
        r"\b(?:la|le|les|l['’]|fen[êe]tres?|instances?|application|appli|programme)\b",
        " ", text, flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", text).strip(" ,.;:'\"")


# ════════════════════════════════════════════════════════════════════════════
# Utilitaires généraux
# ════════════════════════════════════════════════════════════════════════════

def _strip_accents(s: str) -> str:
    if not s:
        return ""
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(c))


def _run(cmd: List[str], timeout: float = 4.0) -> kit.ProcResult:
    return kit.run(cmd, timeout=timeout)


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ════════════════════════════════════════════════════════════════════════════
# Extraction du workspace — chiffres ET ordinaux français
# (« au deuxième bureau », « 2e bureau », « bureau n°4 », « workspace trois »)
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
    w = _strip_accents((word or "").lower().replace("-", " "))
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
    """Retourne (numéro de workspace, texte nettoyé de la mention).
    Comprend : « bureau 4 », « 4e bureau », « deuxième bureau »,
    « bureau n°4 », « workspace numéro trois », etc."""
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
# Adaptateurs session_memory (tolère dict, objet .get/.set, ou None)
# ════════════════════════════════════════════════════════════════════════════

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


_NEGATIVE = {"non", "non merci", "annule", "annuler", "laisse", "laisse tomber",
             "rien", "stop", "arrete", "pas la peine", "aucune"}
_NEGATIVE_NORM = {_strip_accents(x) for x in _NEGATIVE}


def _is_negative(text: str) -> bool:
    return _strip_accents((text or "").strip().lower()) in _NEGATIVE_NORM


def _resolve_pending_choice(query: str, pending: List[Dict]) -> Optional[Dict]:
    """Fait correspondre la réponse de l'utilisateur ('2', 'le deuxième',
    'la dernière', 'kitty — titre…') à un des candidats proposés au tour
    précédent."""
    q = (query or "").strip().lower()
    if not q or not pending:
        return None
    if "derni" in q:
        return pending[-1]
    if re.search(r"premi", q):
        return pending[0]
    m = re.search(r"\b(\d+)\b", q)
    if m:
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(pending):
            return pending[idx]
    ordinals = {
        "deuxieme": 1, "second": 1, "seconde": 1,
        "troisieme": 2, "quatrieme": 3,
        "cinquieme": 4, "sixieme": 5, "septieme": 6,
    }
    for word, idx in ordinals.items():
        if re.search(rf"(?<!\w){word}(?!\w)", _strip_accents(q)):
            try:
                return pending[idx]
            except IndexError:
                continue
    for c in pending:
        if q in c.get("label", "").lower():
            return c
    return None


# ════════════════════════════════════════════════════════════════════════════
# Hyprland : lecture d'état native (indépendante de window_instances)
# ════════════════════════════════════════════════════════════════════════════

def _hypr_clients() -> List[dict]:
    """Fenêtres Hyprland, via le socle (cache partagé, délai garanti)."""
    if _SYSTEM != "Linux":
        return []
    return kit.hypr_clients()


def _win_workspace_id(win: dict) -> Optional[int]:
    ws = win.get("workspace") or {}
    wid = ws.get("id")
    if isinstance(wid, int):
        return wid
    try:
        return int(str(ws.get("name", "")).strip())
    except (TypeError, ValueError):
        return None


def _match_window(win: dict, tokens: Set[str]) -> bool:
    """Correspondance par MOTS entiers (classe/titre), puis par sous-chaîne
    bornée pour les tokens longs — évite les faux positifs du substring brut."""
    parts = [win.get("class", ""), win.get("initialClass", ""),
             win.get("title", ""), win.get("initialTitle", "")]
    blob = _strip_accents(" ".join(p or "" for p in parts)).lower()
    if not blob.strip():
        return False
    words = set(re.findall(r"[a-z0-9._\-]+", blob))
    for tok in tokens:
        t = _strip_accents((tok or "").lower()).strip()
        if not t:
            continue
        if t in words:
            return True
        if len(t) >= 4 and re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", blob):
            return True
    return False


def _find_windows_for(tokens: Set[str], ws_num: Optional[int] = None) -> List[dict]:
    out = []
    for c in _hypr_clients():
        if not _match_window(c, tokens):
            continue
        if ws_num is not None and _win_workspace_id(c) != ws_num:
            continue
        out.append(c)
    return out


def _active_hypr_window() -> Optional[dict]:
    win = kit.hypr_activewindow()
    return win if win.get("address") else None


def _window_exists(addr: str) -> bool:
    return any(c.get("address") == addr for c in _hypr_clients())


def _hyprctl_dispatch(dispatcher: str, arg: str) -> bool:
    """Hyprland répond 0 même pour un dispatcher inconnu : on lit la sortie."""
    res = kit.hypr("dispatch", dispatcher, *([arg] if arg else []))
    if not res.ok:
        return False
    out = res.out.lower()
    return not any(bad in out for bad in ("error", "unknown", "invalid"))


def _close_single_window(win: dict, force: bool = False) -> bool:
    """Ferme UNE fenêtre par son adresse Hyprland, avec vérification réelle :
    la fenêtre n'est déclarée fermée que si son adresse a disparu.
    Escalade : module window_instances → closewindow → killwindow (force)."""
    addr = win.get("address")
    if not addr:
        return False
    if not _window_exists(addr):
        if _HAS_TRACKER:
            try:
                _tracker.forget(addr)
            except Exception:
                pass
        return True

    attempts = []
    if _HAS_WINDOW_INSTANCES:
        attempts.append("module")
    attempts.append("closewindow")
    if force:
        attempts.append("killwindow")

    for how in attempts:
        try:
            if how == "module":
                _hypr_close_window(f"address:{addr}", force=force)
            else:
                _hyprctl_dispatch(how, f"address:{addr}")
        except Exception:
            pass
        time.sleep(0.12)
        if not _window_exists(addr):
            if _HAS_TRACKER:
                try:
                    _tracker.forget(addr)
                except Exception:
                    pass
            return True
    return not _window_exists(addr)


def _label(win: dict) -> str:
    """Libellé humain d'une fenêtre, avec son bureau — indispensable pour
    la désambiguïsation multi-workspaces."""
    base = None
    if _HAS_WINDOW_INSTANCES:
        try:
            base = _hypr_describe_window(win)
        except Exception:
            base = None
    if not base:
        cls = win.get("class") or win.get("initialClass") or "?"
        title = (win.get("title") or win.get("initialTitle") or "").strip()
        base = f"{cls} — « {title[:60]} »" if title else cls
    ws = _win_workspace_id(win)
    if ws is not None and "bureau" not in base.lower():
        base += f" (bureau {ws})"
    return base


def _close_hypr_windows(match_tokens: Set[str], ws_num: Optional[int] = None) -> int:
    closed = 0
    for w in _find_windows_for(match_tokens, ws_num):
        if _close_single_window(w, force=False):
            closed += 1
            time.sleep(0.05)
    return closed


# ════════════════════════════════════════════════════════════════════════════
# Récupération des PIDs (sans pkill, patterns bornés)
# ════════════════════════════════════════════════════════════════════════════

def _pids_from_pgrep(pattern: str) -> List[int]:
    if not shutil.which("pgrep"):
        return []
    try:
        r = _run(["pgrep", "-f", pattern], timeout=3)
        pids = [int(x) for x in (r.stdout or "").splitlines() if x.strip().isdigit()]
        return [p for p in pids if p != os.getpid()]
    except Exception:
        return []


def _pids_from_proc(name_tokens: Set[str]) -> List[int]:
    """Cherche dans /proc (Linux) les processus dont la commande contient un token."""
    pids = []
    my_pid = os.getpid()
    if not Path("/proc").is_dir():
        return []
    for pid_dir in Path("/proc").iterdir():
        if not pid_dir.is_dir() or not pid_dir.name.isdigit():
            continue
        pid = int(pid_dir.name)
        if pid == my_pid:
            continue
        try:
            cmdline = (pid_dir / "cmdline").read_text(errors="replace")
            args = cmdline.split("\x00")
            tokens = set()
            for a in args:
                a = a.strip()
                if not a:
                    continue
                base = os.path.basename(a).lower()
                tokens.add(base)
                tokens.add(a.lower())
            try:
                exe = os.readlink(str(pid_dir / "exe"))
                base_exe = os.path.basename(exe).lower()
                tokens.add(base_exe)
                tokens.add(exe.lower())
            except Exception:
                pass
            if tokens & name_tokens:
                pids.append(pid)
        except Exception:
            continue
    return pids


def _collect_pids(tokens: Set[str]) -> List[int]:
    """pgrep borné par début/fin de nom d'exécutable, puis repli /proc."""
    pids: Set[int] = set()
    for tok in tokens:
        tok = (tok or "").strip().lower()
        if not tok:
            continue
        if " " in tok:
            pat = re.escape(tok)
        else:
            pat = rf"(^|/){re.escape(tok)}( |$)"
        pids.update(_pids_from_pgrep(pat))
    if not pids:
        pids.update(_pids_from_proc({t.lower() for t in tokens if t}))
    return [p for p in pids if p != os.getpid()]


def _kill_pids(pids: List[int], graceful: bool = True) -> Tuple[bool, int]:
    if not pids:
        return False, 0
    if graceful:
        for pid in pids:
            try:
                os.kill(pid, 15)
            except ProcessLookupError:
                continue
            except PermissionError:
                pass
        time.sleep(0.8)
        survivors = [p for p in pids if _pid_exists(p)]
        if not survivors:
            return True, len(pids)
        pids = survivors
    for pid in pids:
        try:
            os.kill(pid, 9)
        except Exception:
            pass
    return True, len(pids)


# ════════════════════════════════════════════════════════════════════════════
# Applications réellement lancées (détection live)
# ════════════════════════════════════════════════════════════════════════════

def _get_running_app_names() -> Set[str]:
    running = set()
    if _SYSTEM == "Linux":
        for c in _hypr_clients():
            cls = (c.get("class", "") or "").lower()
            if cls:
                running.add(cls)
            initial = (c.get("initialClass", "") or "").lower()
            if initial:
                running.add(initial)
        try:
            out = _run(["ps", "-eo", "comm"], timeout=2)
            for line in (out.stdout or "").splitlines()[1:]:
                comm = line.strip().lower()
                if comm:
                    running.add(comm)
        except Exception:
            pass
    if _SYSTEM == "Darwin":
        try:
            out = _run(
                ["osascript", "-e",
                 'tell application "System Events" to get name of every process '
                 'whose background only is false'],
                timeout=3,
            )
            names = (out.stdout or "").strip().split(", ")
            running.update(name.lower() for name in names)
        except Exception:
            pass
    if _SYSTEM == "Windows":
        try:
            out = kit.run(["tasklist", "/NH", "/FO", "CSV"], timeout=4)
            for line in (out.stdout or "").splitlines():
                parts = line.strip().strip('"').split('","')
                if parts:
                    name = parts[0].lower()
                    if name.endswith(".exe"):
                        name = name[:-4]
                    running.add(name)
        except Exception:
            pass
    clean = set()
    for r in running:
        clean.add(r.split("/")[-1].split("\\")[-1].strip())
    return clean


# ════════════════════════════════════════════════════════════════════════════
# Alias de noms d'apps (extensible)
# ════════════════════════════════════════════════════════════════════════════

_ALIAS_MAP: Dict[str, List[str]] = {
    "chrom": ["chrome", "google-chrome-stable", "chromium", "chromium-browser"],
    "firefox": ["mozilla firefox", "firefox-esr", "firefox"],
    "edge": ["microsoft-edge", "msedge"],
    "brave": ["brave-browser", "brave"],
    "vivaldi": ["vivaldi-stable", "vivaldi"],
    "opera": ["opera", "opera-gx", "opera-stable"],
    "terminal": ["gnome-terminal", "konsole", "xfce4-terminal", "kitty", "alacritty", "foot", "wezterm"],
    "explorer": ["nautilus", "dolphin", "thunar", "pcmanfm", "nemo"],
    "code": ["code", "codium", "visual-studio-code"],
    "spotify": ["spotify-launcher", "spotify-client", "spotify"],
    "discord": ["discord-canary", "discord-ptb", "discord"],
    "slack": ["slack-desktop", "slack"],
    "teams": ["teams-for-linux", "teams-insiders", "teams"],
    "whatsapp": ["whatsapp-nativefier", "whatsapp-linux", "whatsapp-desktop"],
    "steam": ["steam-native", "steam-runtime", "steam"],
    "epic": ["epic-games-launcher", "heroic"],
    "telegram": ["telegram-desktop", "telegram"],
    "signal": ["signal-desktop", "signal"],
    "vlc": ["vlc"],
    # Catégories communes
    "navigateur": ["chrome", "chromium", "firefox", "edge", "brave", "vivaldi", "opera"],
    "browser": ["chrome", "chromium", "firefox", "edge", "brave", "vivaldi", "opera"],
    "éditeur": ["code", "gedit", "kate", "subl", "atom", "notepadqq", "notepad"],
    "editor": ["code", "gedit", "kate", "subl", "atom", "notepadqq", "notepad"],
    "musique": ["spotify", "rhythmbox", "clementine", "amarok", "audacious", "lollypop"],
    "music": ["spotify", "rhythmbox", "clementine", "amarok", "audacious", "lollypop"],
    "chat": ["discord", "slack", "teams", "telegram", "signal", "whatsapp", "zapzap"],
    # Extensions issues de /usr/share/applications (Arch/EndeavourOS)
    "audacious": ["audacious"],
    "easyeffects": ["gtk-launch com.github.wwmm.easyeffects"],
    "mpv": ["mpv"],
    "pavucontrol": ["pavucontrol"],
    "totem": ["gtk-launch org.gnome.Totem"],
    "soundrecorder": ["gtk-launch org.gnome.SoundRecorder"],
    "lollypop": ["gtk-launch org.gnome.Lollypop"],
    "qv4l2": ["qv4l2"],
    "qvidcap": ["qvidcap"],
    "foot": ["foot"],
    "footclient": ["footclient"],
    "foot-server": ["gtk-launch foot-server"],
    "kitty": ["kitty"],
    "xterm": ["xterm"],
    "uxterm": ["uxterm"],
    "yazi": ["yazi"],
    "sublime": ["sublime_text", "subl"],
    "sublime text": ["sublime_text", "subl"],
    "kate": ["kate"],
    "kwrite": ["kwrite"],
    "marknote": ["gtk-launch org.kde.marknote"],
    "vim": ["vim", "gvim"],
    "micro": ["micro"],
    "jetbrains-studio": ["gtk-launch jetbrains-studio"],
    "zcode": ["gtk-launch zcode"],
    "codex": ["gtk-launch codex-desktop"],
    "claude": ["gtk-launch com.anthropic.Claude"],
    "cmake-gui": ["cmake-gui"],
    "assistant": ["assistant"],
    "designer": ["designer"],
    "linguist": ["linguist"],
    "qdbusviewer": ["qdbusviewer"],
    "qt6ct": ["qt6ct"],
    "darklystyleconfig": ["darklystyleconfig"],
    "kvantummanager": ["kvantummanager"],
    "gnome-system-monitor": ["gnome-system-monitor"],
    "gnome-calculator": ["gnome-calculator"],
    "gnome-clocks": ["gnome-clocks"],
    "gnome-chess": ["gnome-chess"],
    "gnome-contacts": ["gnome-contacts"],
    "gnome-mahjongg": ["gnome-mahjongg"],
    "gnome-maps": ["gnome-maps"],
    "gnome-notes": ["gnome-notes"],
    "gnome-photos": ["gnome-photos"],
    "gnome-texteditor": ["gnome-text-editor"],
    "gnome-loupe": ["loupe"],
    "gnome-extensions": ["gnome-extensions"],
    "gnome-seahorse": ["seahorse"],
    "gnome-zenity": ["zenity"],
    "gnome-meld": ["meld"],
    "gnome-2048": ["gnome-2048"],
    "evolution-alarm": ["evolution-alarm-notify"],
    "btop": ["btop"],
    "blueman-manager": ["blueman-manager"],
    "blueman-adapters": ["blueman-adapters"],
    "firewall-config": ["firewall-config"],
    "gparted": ["gparted", "gparted-pkexec"],
    "lstopo": ["lstopo"],
    "reflector-simple": ["reflector-simple"],
    "stoken-gui": ["stoken-gui"],
    "stoken-gui-small": ["stoken-gui-small"],
    "swappy": ["swappy"],
    "ventoy": ["ventoy"],
    "uuctl": ["uuctl"],
    "yad-icon-browser": ["yad-icon-browser"],
    "yad-settings": ["yad-settings"],
    "user-dirs-update-gtk": ["xdg-user-dirs-gtk-update"],
    "kded5": ["kded5"],
    "kded6": ["kded6"],
    "kiod6": ["kiod6"],
    "knewstuff-dialog6": ["knewstuff-dialog6"],
    "ksecretd": ["ksecretd"],
    "kcm-proxy": ["kcmshell6 proxy"],
    "kcm-trash": ["kcmshell6 trash"],
    "kcm-netpref": ["kcmshell6 netpref"],
    "kcm-webshortcuts": ["kcmshell6 webshortcuts"],
    "kcm-darklydecoration": ["kcmshell6 darklydecoration"],
    "ktelnetservice5": ["ktelnetservice5"],
    "ktelnetservice6": ["ktelnetservice6"],
    "avahi-discover": ["avahi-discover"],
    "bssh": ["bssh"],
    "bvnc": ["bvnc"],
    "nm-applet": ["nm-applet"],
    "nm-connection-editor": ["nm-connection-editor"],
    "moonlight": ["gtk-launch moonlight-stable"],
    "sunshine": ["gtk-launch dev.lizardbyte.app.Sunshine"],
    "scrcpy": ["scrcpy"],
    "scrcpy-console": ["gtk-launch scrcpy-console"],
    "localsend": ["localsend"],
    "zapzap": ["gtk-launch com.rtosta.zapzap"],
    "libreoffice-base": ["libreoffice --base"],
    "libreoffice-draw": ["libreoffice --draw"],
    "libreoffice-math": ["libreoffice --math"],
    "libreoffice-startcenter": ["libreoffice"],
    "java": ["java"],
    "jconsole": ["jconsole"],
    "jshell": ["jshell"],
    "eos-welcome": ["eos-welcome"],
    "eos-apps-info": ["eos-apps-info"],
    "eos-log-tool": ["eos-log-tool"],
    "eos-quickstart": ["eos-quickstart"],
    "eos-update": ["eos-update"],
    "geoclue-demo-agent": ["gtk-launch geoclue-demo-agent"],
    "geoclue-where-am-i": ["gtk-launch geoclue-where-am-i"],
    "gcr-prompter": ["gcr-prompter"],
    "gcr-viewer": ["gcr-viewer"],
    "pinentry-qt": ["pinentry-qt"],
    "quickshell": ["gtk-launch org.quickshell"],
    "terax": ["gtk-launch Terax"],
    "gradia": ["gtk-launch be.alexandervanhee.gradia"],
    "google-maps-geo": ["gtk-launch google-maps-geo-handler"],
    "openstreetmap-geo": ["gtk-launch openstreetmap-geo-handler"],
    "wheelmap-geo": ["gtk-launch wheelmap-geo-handler"],
}

# Clés « catégorie » : si l'utilisateur nomme une app précise qui est aussi
# membre d'une catégorie (« kitty » dans « terminal »), on ne ferme QUE
# cette app, jamais toute la catégorie.
_CATEGORY_KEYS = {"terminal", "explorer", "navigateur", "browser",
                  "éditeur", "editor", "musique", "music", "chat"}


def _resolve_app_name(name: str) -> Tuple[str, Set[str]]:
    """Renvoie (nom_canonique, set_de_tokens) pour chercher fenêtres/processus.
    Règle de sécurité : une app précise demandée explicitement reste précise,
    même si elle appartient aussi à une catégorie."""
    original = re.sub(r"\s+", " ", (name or "").strip().lower())
    # 1) le nom est lui-même une clé (app précise ou catégorie assumée)
    if original in _ALIAS_MAP:
        return original, {original} | set(_ALIAS_MAP[original])
    # 2) le nom est un alias listé dans un groupe
    for key, aliases in _ALIAS_MAP.items():
        if original in aliases:
            if key in _CATEGORY_KEYS:
                # « konsole » ne doit pas fermer tous les terminaux
                return original, {original}
            return key, set(aliases) | {key}
    # 3) inconnu : variantes typographiques
    matching = {original}
    matching.add(original.replace(" ", ""))
    matching.add(original.replace(" ", "-"))
    matching.add(original.replace("-", " "))
    return original, matching


# ════════════════════════════════════════════════════════════════════════════
# Liste d'instances Linux
# ════════════════════════════════════════════════════════════════════════════

def _list_linux_instances(app_name: str) -> str:
    _, tokens = _resolve_app_name(app_name)
    lines: List[str] = []
    clients = _hypr_clients()
    for c in clients:
        if _match_window(c, tokens):
            title = (c.get("title") or "").strip() or "sans titre"
            cls = c.get("class") or "?"
            pid = c.get("pid", "?")
            addr = c.get("address", "?")
            ws = _win_workspace_id(c)
            ws_txt = f", Bureau: {ws}" if ws is not None else ""
            lines.append(f"- [Fenêtre] Titre: '{title}', Classe: '{cls}', "
                         f"PID: {pid}, Adresse: {addr}{ws_txt}")
    known_pids = {str(c.get("pid")) for c in clients if c.get("pid")}
    for pid in _collect_pids(tokens):
        if str(pid) in known_pids:
            continue
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_text(errors="replace").replace("\x00", " ")
            lines.append(f"- [Processus] PID: {pid}, Cmd: '{cmdline.strip()}'")
        except Exception:
            lines.append(f"- [Processus] PID: {pid}")
    if not lines:
        return f"Aucune instance trouvée pour '{app_name}'."
    return f"Instances trouvées pour '{app_name}':\n" + "\n".join(lines)


# ════════════════════════════════════════════════════════════════════════════
# Fermeture Linux (Hyprland + processus) — chemin historique conservé
# ════════════════════════════════════════════════════════════════════════════

def _close_linux(app_name: str, ws_num: Optional[int] = None) -> Tuple[bool, int]:
    canonical, tokens = _resolve_app_name(app_name)
    logger.debug(f"Resolved: canonical={canonical}, tokens={tokens}")
    hypr_closed = _close_hypr_windows(tokens, ws_num=ws_num)
    if hypr_closed > 0:
        logger.info(f"Closed {hypr_closed} Hyprland windows for '{canonical}'")
        time.sleep(0.5)
    if ws_num is not None:
        return (hypr_closed > 0, hypr_closed)
    pids = _collect_pids(tokens)
    if not pids:
        return (hypr_closed > 0, hypr_closed)
    _, killed = _kill_pids(pids, graceful=True)
    total_closed = hypr_closed + killed
    return (total_closed > 0, total_closed)


# ════════════════════════════════════════════════════════════════════════════
# macOS / Windows
# ════════════════════════════════════════════════════════════════════════════

def _close_macos(app_name: str) -> Tuple[bool, int]:
    _, tokens = _resolve_app_name(app_name)
    for token in tokens:
        try:
            _run(["osascript", "-e", f'quit app "{token}"'], timeout=3)
            return True, 1
        except Exception:
            continue
    for token in tokens:
        try:
            _run(["killall", "-9", token], timeout=3)
            return True, 1
        except Exception:
            continue
    return False, 0


def _close_windows(app_name: str) -> Tuple[bool, int]:
    _, tokens = _resolve_app_name(app_name)
    killed = 0
    for token in tokens:
        exe = token if token.endswith(".exe") else f"{token}.exe"
        for force in (False, True):
            args = ["taskkill", "/IM", exe]
            if force:
                args.append("/F")
            try:
                _run(args, timeout=3)
                killed += 1
                break
            except Exception:
                continue
    return killed > 0, killed


_OS_CLOSERS = {
    "Windows": _close_windows,
    "Darwin": _close_macos,
    "Linux": _close_linux,
}


# ════════════════════════════════════════════════════════════════════════════
# Analyse des commandes naturelles
# ════════════════════════════════════════════════════════════════════════════

_CLOSE_PATTERNS = [
    r"^(?:ferme|fermer|quitte|quitter|tue|tuer|close|kill|stoppe|stop)\s+(?P<app>.+)$",
    r"^(?P<app>.+?)\s+(?:ferme|fermer|quitte|quitter|tue|tuer|close|kill|stoppe|stop)$",
    r"^(?:je veux|je voudrais|peux[- ]tu|tu peux|tu pourrais)\s+"
    r"(?:fermer|quitter|tuer|kill|stopper)\s+(?P<app>.+)$",
]


def _clean_app_name(raw: str) -> str:
    app = (raw or "").strip()
    app = re.sub(r"[?.!,;:]+$", "", app).strip()
    for _ in range(2):
        app = re.sub(r"^(?:l['’]|la|le|les|un|une|des)\s+", "", app)
        app = re.sub(
            r"^(?:fen[êe]tre|instance|onglet|application|app|programme|processus)\s+(?:de\s+|d['’])?",
            "", app, flags=re.IGNORECASE)
    return app.strip()


def _parse_close_command(text: str) -> Optional[Dict]:
    t = (text or "").lower().strip()
    for pat in _CLOSE_PATTERNS:
        m = re.search(pat, t)
        if m:
            app = _clean_app_name(m.group("app"))
            if app:
                return {"app_name": app}
    return None


def _get_api_key() -> str:
    try:
        config_path = Path(__file__).resolve().parent.parent / "config" / "api_keys.json"
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        return config.get("gemini_api_key", "")
    except Exception:
        return ""


def _detect_close_via_ai(description: str) -> Optional[Dict]:
    """Fallback IA (Gemini) pour les phrases complexes."""
    api_key = _get_api_key()
    if not api_key:
        return None
    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        prompt = (
            "Extrais le nom de l'application à fermer de la phrase suivante. "
            'Renvoie UNIQUEMENT un objet JSON {"app_name": "..."}. '
            f"Phrase : « {description} »"
        )
        resp = client.models.generate_content(model=FAST_MODEL,
                                              contents=prompt)
        json_match = re.search(r"\{.*\}", resp.text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group(0))
    except Exception as e:
        logger.debug(f"AI detection failed: {e}")
    return None


# ════════════════════════════════════════════════════════════════════════════
# API publique – close_app()
# ════════════════════════════════════════════════════════════════════════════

@kit.action("close_app")
def close_app(parameters: dict = None, response=None, player=None, session_memory=None) -> str:
    """
    Ferme une application en utilisant son nom (précis ou catégorie).
    Paramètres acceptés :
      app_name    : nom de l'application (ex: "kitty")
      description : phrase naturelle ("ferme la fenêtre de kitty au deuxième bureau")
      workspace   : bureau ciblé (chiffre ou texte)
      force       : True pour une fermeture forcée (killwindow)
    """
    params = parameters or {}
    app_name = str(params.get("app_name", "") or "").strip()
    description = str(params.get("description", "") or "").strip()
    force = bool(params.get("force", False))

    # ── 0. Extraction du workspace (chiffres ET ordinaux) ────────────────
    ws_num: Optional[int] = None
    pw = params.get("workspace")
    if pw is not None:
        try:
            ws_num = int(str(pw).strip())
        except (TypeError, ValueError):
            ws_num, _ = _extract_workspace(str(pw))
    if ws_num is None:
        w1, app_name = _extract_workspace(app_name)
        w2, description = _extract_workspace(description)
        ws_num = w1 if w1 is not None else w2

    # ── 1. Réponse à une désambiguïsation posée au tour précédent ? ──────
    pending = _sm_get(session_memory, _PENDING_KEY)
    if pending:
        query = app_name or description
        if _is_negative(query):
            _sm_set(session_memory, _PENDING_KEY, None)
            return "D'accord, je ne ferme rien."
        if _RE_ALL.search(query or ""):
            _sm_set(session_memory, _PENDING_KEY, None)
            closed = 0
            for c in pending:
                if _close_single_window({"address": c.get("address")}, force=force):
                    closed += 1
                time.sleep(0.05)
            return f"{closed} fenêtre(s) fermée(s)."
        chosen = _resolve_pending_choice(query, pending)
        if chosen:
            _sm_set(session_memory, _PENDING_KEY, None)
            ok = _close_single_window({"address": chosen.get("address")}, force=force)
            if ok and chosen.get("nickname") and _HAS_WINDOW_INSTANCES:
                try:
                    unregister_instance(chosen["nickname"])
                except Exception:
                    pass
            return f"{chosen['label']} fermé." if ok else f"Impossible de fermer {chosen['label']}."
        # La réponse ne correspond à aucun candidat : on abandonne et on
        # retraite la demande normalement.
        _sm_set(session_memory, _PENDING_KEY, None)

    # ── 2. Portée demandée : dernière, active, ou toutes ? ───────────────
    scope = params.get("target") or _detect_scope(app_name, description)

    if scope == "last" and _SYSTEM == "Linux":
        hint = _strip_deixis(app_name) or _strip_deixis(description)
        entry = None
        if _HAS_TRACKER:
            try:
                entry = (_tracker.last_launched(app=hint) if hint else None) or _tracker.last_launched()
            except Exception:
                entry = None
        if entry:
            win = {"address": entry["address"], "class": entry.get("class", ""),
                   "title": entry.get("title", ""),
                   "initialTitle": entry.get("initialTitle", "")}
            if _close_single_window(win, force=force):
                name = entry.get("app") or entry.get("class") or "la fenêtre"
                return f"{name} que je venais d'ouvrir est fermé — les autres instances sont intactes."
            return f"Impossible de fermer la fenêtre {entry.get('app', '')} ciblée."
        # Repli sans journal : dernière fenêtre correspondante visible.
        cand: List[dict] = []
        if hint:
            _, hint_tokens = _resolve_app_name(hint)
            cand = _find_windows_for(hint_tokens, ws_num)
            if not cand and ws_num is not None:
                cand = _find_windows_for(hint_tokens)
        else:
            cand = _hypr_clients()
        if cand:
            win = cand[-1]
            if _close_single_window(win, force=force):
                return f"{_label(win)} fermé — c'était la dernière ouverte."
        if hint:
            app_name = hint
            logger.info("Journal de lancement vide pour '%s' — résolution normale.", hint)

    if scope == "active" and _SYSTEM == "Linux":
        win = None
        if _HAS_TRACKER:
            try:
                win = _tracker.active_window()
            except Exception:
                win = None
        if not win:
            win = _active_hypr_window()
        # La fenêtre « active » peut être celle de l'assistant lui-même
        # (l'utilisateur vient de lui parler) : ne jamais la fermer par
        # accident, on retombe alors sur le ciblage par nom d'app.
        if win and str(win.get("pid")) == str(os.getpid()):
            win = None
        if win:
            label = _label(win)
            if _close_single_window(win, force=force):
                return f"{label} fermé."
            return f"Impossible de fermer {label}."
        if not app_name:
            return ("Je ne peux pas fermer ma propre fenêtre. "
                    "Dis-moi plutôt le nom de l'application à fermer.")

    if scope in ("last", "active", "all"):
        cleaned_name = _strip_deixis(app_name)
        if cleaned_name:
            app_name = cleaned_name

    # ── 3. Ciblage direct par surnom enregistré (le plus fiable) ─────────
    if _HAS_WINDOW_INSTANCES and app_name and get_instance(app_name):
        windows = _hypr_find_windows(app_name)
        if not windows:
            unregister_instance(app_name)
            return f"'{app_name}' n'est plus ouvert (fenêtre introuvable, surnom retiré du registre)."
        if ws_num is not None:
            windows = [w for w in windows if _win_workspace_id(w) == ws_num]
            if not windows:
                return f"'{app_name}' n'a aucune fenêtre sur le bureau {ws_num}."
        if len(windows) == 1:
            addr = windows[0]["address"]
            ok = _close_single_window({"address": addr}, force=force)
            if ok:
                unregister_instance(app_name)
            return f"'{app_name}' fermé." if ok else f"Impossible de fermer '{app_name}'."
        addr = windows[0]["address"]
        ok = _close_single_window({"address": addr}, force=force)
        return (f"'{app_name}' fermé (1 sur {len(windows)} instances identiques)."
                if ok else f"Impossible de fermer '{app_name}'.")

    # ── 4. Parser la description si pas de nom direct ────────────────────
    if not app_name and description:
        parsed = _parse_close_command(description)
        if parsed:
            app_name = parsed.get("app_name", "")
        else:
            ai_parsed = _detect_close_via_ai(description)
            if ai_parsed:
                app_name = ai_parsed.get("app_name", "")
            else:
                return "Je n'ai pas compris quelle application fermer. Pouvez-vous préciser ?"

    if not app_name:
        return "Aucune application spécifiée."

    canonical, tokens = _resolve_app_name(app_name)
    running_apps = _get_running_app_names()

    found_match = None
    for token in tokens:
        if token in running_apps:
            found_match = token
            break
        for running in running_apps:
            if token in running or running in token:
                found_match = running
                break
        if found_match:
            break
    if not found_match and canonical in _ALIAS_MAP:
        candidates = []
        for app_candidate in _ALIAS_MAP[canonical]:
            if app_candidate in running_apps:
                candidates.append(app_candidate)
        if len(candidates) == 1:
            found_match = candidates[0]
        elif len(candidates) > 1:
            return (f"Plusieurs applications de type '{canonical}' sont lancées : "
                    f"{', '.join(candidates)}. Laquelle fermer ?")
        else:
            for cand in _ALIAS_MAP[canonical]:
                for run in running_apps:
                    if cand in run or run in cand:
                        candidates.append(run)
                        break
            candidates = list(dict.fromkeys(candidates))
            if len(candidates) == 1:
                found_match = candidates[0]
            elif len(candidates) > 1:
                return (f"Plusieurs applications de type '{canonical}' sont lancées : "
                        f"{', '.join(candidates)}. Laquelle fermer ?")
            else:
                return f"Aucune application correspondant à '{app_name}' n'est lancée actuellement."

    target_app = found_match if found_match else app_name
    if player:
        try:
            player.write_log(f"[close_app] {target_app}")
        except Exception:
            pass

    if params.get("list_instances", False):
        if _SYSTEM == "Linux":
            return _list_linux_instances(target_app)
        return (f"La liste des instances n'est supportée que sous Linux pour le moment. "
                f"Impossible de lister {target_app}.")

    # ── 5. Ciblage fin Linux : fenêtres Hyprland, workspace-aware ────────
    if _SYSTEM == "Linux":
        all_matches = _find_windows_for(tokens)
        matches = [w for w in all_matches
                   if ws_num is None or _win_workspace_id(w) == ws_num]

        # Le bureau demandé ne contient aucune fenêtre, mais il y en a
        # ailleurs : réponse intelligente + proposition, jamais un échec sec.
        if ws_num is not None and not matches and all_matches:
            candidates = []
            for w in all_matches:
                candidates.append({
                    "selector": f"address:{w.get('address')}",
                    "address": w.get("address"),
                    "label": _label(w),
                    "nickname": None,
                })
            _sm_set(session_memory, _PENDING_KEY, candidates)
            listing = "\n".join(f"{i}. {c['label']}" for i, c in enumerate(candidates, 1))
            return (f"Aucune fenêtre « {app_name} » sur le bureau {ws_num}.\n"
                    f"Elles sont ici :\n{listing}\n"
                    f"Veux-tu que j'en ferme une ? (numéro, « la dernière », ou « toutes »)")

        # Aucune fenêtre nulle part.
        if not all_matches:
            if ws_num is not None:
                return (f"« {app_name} » tourne mais n'a aucune fenêtre ouverte "
                        f"(ni sur le bureau {ws_num}, ni ailleurs). Rien à fermer.")
            pids = _collect_pids(tokens)
            if pids:
                _, killed = _kill_pids(pids, graceful=True)
                if killed:
                    return f"{target_app.title()} fermé ({killed} processus, aucune fenêtre détectée)."
            return f"Aucune fenêtre ni processus trouvé pour « {app_name} »."

        # Fermeture de masse explicitement demandée.
        if scope == "all":
            target_matches = matches if ws_num is not None else all_matches
            closed = 0
            for w in target_matches:
                if _close_single_window(w, force=force):
                    closed += 1
                time.sleep(0.05)
            leftover = 0
            if ws_num is None:
                time.sleep(0.3)
                pids = _collect_pids(tokens)
                if pids:
                    _, leftover = _kill_pids(pids, graceful=True)
            total = closed + leftover
            ws_txt = f" du bureau {ws_num}" if ws_num is not None else ""
            if total:
                return f"{target_app.title()}{ws_txt} complètement fermé ({total} fenêtre(s)/processus)."
            return f"Rien à fermer pour {target_app}{ws_txt}."

        # Plusieurs fenêtres sur le bureau ciblé : on demande laquelle.
        if not params.get("_skip_disambiguation") and len(matches) > 1:
            recent_addr = None
            if _HAS_TRACKER:
                try:
                    entry = _tracker.last_launched(app=target_app)
                    recent_addr = entry.get("address") if entry else None
                except Exception:
                    recent_addr = None
            candidates = []
            for w in matches:
                label = _label(w)
                if w.get("address") == recent_addr:
                    label += "  ← celle que je viens d'ouvrir"
                candidates.append({
                    "selector": f"address:{w.get('address')}",
                    "address": w.get("address"),
                    "label": label,
                    "nickname": None,
                })
            _sm_set(session_memory, _PENDING_KEY, candidates)
            listing = "\n".join(f"{i}. {c['label']}" for i, c in enumerate(candidates, 1))
            ws_txt = f" sur le bureau {ws_num}" if ws_num is not None else ""
            return (f"{len(candidates)} fenêtres « {app_name} » sont ouvertes{ws_txt}, "
                    f"laquelle veux-tu fermer ?\n{listing}\n"
                    f"(réponds par le numéro, « la dernière », ou « toutes »)")

        # 1 fenêtre (ou plusieurs avec _skip_disambiguation) : on ferme.
        closed = 0
        for w in matches:
            if _close_single_window(w, force=force):
                closed += 1
            time.sleep(0.05)
        if closed:
            ws_txt = f" (bureau {ws_num})" if ws_num is not None else ""
            if closed == 1:
                return f"{_label(matches[0])} fermé{ws_txt}."
            return f"{closed} fenêtres « {app_name} » fermées{ws_txt}."
        return f"Impossible de fermer « {app_name} »."

    # ── 6. Autres OS ──────────────────────────────────────────────────────
    closer = _OS_CLOSERS.get(_SYSTEM)
    if not closer:
        return f"Système non supporté : {_SYSTEM}"
    try:
        ok, count = closer(target_app)
    except Exception as e:
        logger.exception("close_app failed")
        return f"Erreur lors de la fermeture de {target_app} : {e}"
    if ok:
        return f"{target_app.title()} fermé{'' if count == 1 else f' ({count} fenêtres/processus)'}."
    return f"Impossible de fermer {target_app} (peut-être déjà fermé ?)."


# ════════════════════════════════════════════════════════════════════════════
# Test direct
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.DEBUG)
    if len(sys.argv) > 1:
        print(close_app({"description": " ".join(sys.argv[1:])}))
    else:
        print("Usage: close_app.py <phrase ou nom d'app>")
