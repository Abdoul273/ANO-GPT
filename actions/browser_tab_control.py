"""
🔥 BROWSER_TAB_CONTROL — Gestion d'onglets ultra-puissante pour Jarvis.
Lister, fermer, basculer des ONGLETS réels — pas des fenêtres OS.

Stratégie multi-niveaux (du plus précis au plus dégradé) :
  1. CDP (Chrome DevTools Protocol) : si le navigateur tourne avec
     --remote-debugging-port (ou écoute sur 9222-9225), on pilote les
     vrais onglets via /json/list, /json/close, /json/activate —
     fonctionne sous Wayland/Hyprland, sans wmctrl ni xdotool ;
  2. Fenêtres Hyprland : si pas de CDP, on liste/ferme/active les
     FENÊTRES navigateur via hyprctl (un onglet par fenêtre au mieux),
     avec information de bureau ;
  3. wmctrl/xdotool : dernier recours X11, avec le parsing corrigé
     (l'ancien exigeait >= 9 champs pour une sortie qui en a 4).

Désambiguïsation persistante via session_memory : quand plusieurs onglets
correspondent, la question est posée et « ferme 2 » est résolu au tour
suivant.
"""
import json
import os
import re
import shutil
import subprocess
from core import action_kit as kit
import time
import urllib.request
from typing import Dict, List, Optional
from urllib.parse import urlparse

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


_PENDING_TABS_KEY = "browser_tab_pending_candidates"

# Navigateurs pilotables par CDP (famille Chromium + Firefox récent)
_BROWSER_CLASSES = ("chrome", "chromium", "brave", "microsoft-edge", "msedge",
                    "vivaldi", "opera", "firefox")

# Processus dont la ligne de commande révèle le port de debug
_CDP_PROCS = ("chrome", "chromium", "brave", "msedge", "microsoft-edge",
              "vivaldi", "opera", "firefox")

# Ports CDP habituels à sonder quand le flag n'apparaît pas dans cmdline
# (Firefox expose souvent --remote-debugging-port sans argument explicite).
_CDP_DEFAULT_PORTS = (9222, 9223, 9224, 9225)


# ────────────────────────────────────────────────────────────────────────────
# 🖥️ Environnement Hyprland (DISPLAY/WAYLAND/instance restaurés)
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

    Chaque module gardait sa copie de cette fonction et relançait un processus
    par question. Le cache du socle fusionne les appels d'un même tour de
    parole : plusieurs actions qui listent les fenêtres n'en paient qu'un.
    """
    return kit.hypr_json(*args, default=None)


def _hypr_dispatch(dispatcher: str, arg: str = "") -> bool:
    """Hyprland renvoie 0 même pour un dispatcher inconnu : on lit la sortie."""
    res = kit.hypr("dispatch", dispatcher, *([arg] if arg else []))
    if not res.ok:
        return False
    out = res.out.lower()
    return not any(bad in out for bad in ("error", "unknown", "invalid"))


def _is_browser_class(cls: str) -> bool:
    c = (cls or "").lower()
    return any(b in c for b in _BROWSER_CLASSES)


def _read_cmdline(pid: int) -> List[str]:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return [a.decode(errors="replace") for a in f.read().split(b"\x00") if a]
    except Exception:
        return []


def _pid_is_browser(pid: int) -> Optional[str]:
    args = _read_cmdline(pid)
    if not args:
        return None
    base = os.path.basename(args[0]).lower()
    for name in _CDP_PROCS:
        if name in base:
            return name
    return None


# ────────────────────────────────────────────────────────────────────────────
# 🌐 CDP : détection du port + requêtes HTTP
# ────────────────────────────────────────────────────────────────────────────

def _cdp_ports_for_browser(browser: str) -> List[int]:
    """Ports CDP d'un navigateur : extraits de la ligne de commande des
    processus, puis ports habituels sondés."""
    ports: List[int] = []
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            if _pid_is_browser(int(entry)) != browser and \
                    not (_pid_is_browser(int(entry)) or "").startswith(browser):
                continue
            for arg in _read_cmdline(int(entry)):
                m = re.match(r"--remote-debugging-port=(\d+)", arg)
                if m:
                    p = int(m.group(1))
                    if p not in ports:
                        ports.append(p)
    except Exception:
        pass
    if not ports:
        ports = list(_CDP_DEFAULT_PORTS)
    return ports


def _cdp_get(url: str, timeout: float = 1.5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


def _cdp_probe(port: int) -> bool:
    return _cdp_get(f"http://localhost:{port}/json/version") is not None


def _cdp_list_pages(browser: str) -> List[dict]:
    for port in _cdp_ports_for_browser(browser):
        pages = _cdp_get(f"http://localhost:{port}/json/list")
        if pages is None:
            pages = _cdp_get(f"http://localhost:{port}/json")
        if isinstance(pages, list):
            for p in pages:
                p["_port"] = port
            return pages
    return []


def _cdp_close(port: int, tab_id: str) -> bool:
    try:
        with urllib.request.urlopen(
                f"http://localhost:{port}/json/close/{tab_id}", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def _cdp_activate(port: int, tab_id: str) -> bool:
    try:
        with urllib.request.urlopen(
                f"http://localhost:{port}/json/activate/{tab_id}", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def _browser_pids() -> Dict[str, List[int]]:
    """Navigateurs réellement lancés → liste de PIDs."""
    out: Dict[str, List[int]] = {}
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            name = _pid_is_browser(int(entry))
            if name:
                out.setdefault(name, []).append(int(entry))
    except Exception:
        pass
    return out


def _detect_cdp() -> Dict[str, int]:
    """{navigateur: port} pour chaque navigateur dont le CDP répond."""
    found: Dict[str, int] = {}
    for name in _browser_pids():
        for port in _cdp_ports_for_browser(name):
            if _cdp_probe(port):
                found[name] = port
                break
    return found


# ────────────────────────────────────────────────────────────────────────────
# 📋 Énumération des onglets (CDP → fenêtres Hyprland → wmctrl corrigé)
# ────────────────────────────────────────────────────────────────────────────

def get_open_tabs() -> List[Dict[str, str]]:
    """Tous les « onglets » visibles, du plus précis au plus dégradé :
    - CDP : vrais onglets (id, title, url, port, browser) ;
    - Hyprland : fenêtres navigateur (titre = dernier onglet visible) ;
    - wmctrl : X11, parsing corrigé (4 champs, pas 9)."""
    tabs: List[Dict[str, str]] = []

    # ── 1. CDP : les vrais onglets ────────────────────────────────────────
    for browser, port in _detect_cdp().items():
        for p in _cdp_list_pages(browser):
            if p.get("type") not in (None, "page"):
                continue
            tabs.append({
                "name": (p.get("title") or "sans titre")[:120],
                "url": p.get("url", ""),
                "id": p.get("id", ""),
                "browser": browser,
                "port": str(port),
                "window": "",
                "workspace": "",
                "source": "cdp",
            })
    if tabs:
        return tabs[:50]

    # ── 2. Fenêtres navigateur sous Hyprland ─────────────────────────────
    clients = _hyprctl_json("clients")
    if isinstance(clients, list):
        for c in clients:
            cls = (c.get("class") or c.get("initialClass") or "")
            if not _is_browser_class(cls):
                continue
            title = (c.get("title") or c.get("initialTitle") or "").strip()
            ws = (c.get("workspace") or {}).get("id")
            tabs.append({
                "name": (title or cls)[:120],
                "url": "",
                "id": c.get("address", ""),
                "browser": cls.lower(),
                "port": "",
                "window": c.get("address", ""),
                "workspace": str(ws) if ws is not None else "",
                "source": "hyprland",
            })
        if tabs:
            return tabs[:50]

    # ── 3. wmctrl (X11) — parsing CORRIGÉ ────────────────────────────────
    # Ancien bug : « len(parts) >= 9 » alors que `wmctrl -l` produit
    # 4 champs : <window-id> <desktop> <host> <titre…> → jamais de match.
    if shutil.which("wmctrl"):
        try:
            r = subprocess.run(["wmctrl", "-l"], capture_output=True,
                               text=True, timeout=2)
            if r.returncode == 0:
                for line in r.stdout.splitlines():
                    parts = line.split(None, 3)
                    if len(parts) < 4:
                        continue
                    win_id, desktop, _host, title = (
                        parts[0], parts[1], parts[2], parts[3])
                    tl = title.lower()
                    if any(b in tl for b in _BROWSER_CLASSES) or \
                            _is_browser_class(tl):
                        url_m = re.search(r"(https?://\S+|www\.\S+)", title)
                        tabs.append({
                            "name": title[:120],
                            "url": url_m.group(1) if url_m else "",
                            "id": win_id,
                            "browser": "",
                            "port": "",
                            "window": win_id,
                            "workspace": desktop,
                            "source": "wmctrl",
                        })
        except Exception:
            pass
    return tabs[:50]


def find_tabs(keyword: str) -> List[Dict[str, str]]:
    """Tous les onglets correspondant au mot-clé (titre, URL, domaine)."""
    keyword = (keyword or "").strip().lower()
    if not keyword:
        return []
    matches = []
    for tab in get_open_tabs():
        hay = f"{tab.get('name','')} {tab.get('url','')}".lower()
        if keyword in hay:
            matches.append(tab)
    return matches


def find_tab(keyword: str) -> Optional[Dict[str, str]]:
    m = find_tabs(keyword)
    return m[0] if m else None


def _domain_of(url: str) -> str:
    if not url:
        return ""
    try:
        netloc = urlparse(url).netloc
        return netloc or url[:40]
    except Exception:
        return url[:40]


def _tab_label(tab: Dict[str, str]) -> str:
    dom = _domain_of(tab.get("url", ""))
    name = tab.get("name", "sans titre")
    if dom and dom not in name.lower():
        return f"{name} ({dom})"
    return name


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


def _resolve_pending_choice(query: str, pending: List[Dict]) -> Optional[Dict]:
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
    ordinals = {"deuxieme": 1, "second": 1, "troisieme": 2,
                "quatrieme": 3, "cinquieme": 4}
    for word, idx in ordinals.items():
        if word in q.replace("è", "e").replace("é", "e"):
            try:
                return pending[idx]
            except IndexError:
                continue
    for c in pending:
        if q and q in _tab_label(c).lower():
            return c
    return None


# ────────────────────────────────────────────────────────────────────────────
# 🎯 Fermeture / activation d'un onglet
# ────────────────────────────────────────────────────────────────────────────

def _close_one_tab(tab: Dict[str, str]) -> bool:
    """Ferme UN onglet : CDP d'abord (le vrai onglet), repli fenêtre."""
    if tab.get("source") == "cdp" and tab.get("id") and tab.get("port"):
        return _cdp_close(int(tab["port"]), tab["id"])
    # Repli : pas de CDP → on ne peut fermer que la fenêtre entière.
    addr = tab.get("window") or tab.get("id")
    if addr and addr.startswith("0x") and _hypr_dispatch("closewindow",
                                                          f"address:{addr}"):
        return True
    if addr and not addr.startswith("0x") and shutil.which("wmctrl"):
        ok, _, _ = CommandExecutor.run(f"wmctrl -ic {addr}", timeout=2)
        if ok:
            return True
    return False


def _activate_one_tab(tab: Dict[str, str]) -> bool:
    """Active un onglet : CDP + focus de la fenêtre navigateur ; sinon
    focus/déplacement de la fenêtre Hyprland, sinon wmctrl/xdotool."""
    ok = False
    if tab.get("source") == "cdp" and tab.get("id") and tab.get("port"):
        ok = _cdp_activate(int(tab["port"]), tab["id"])
    addr = tab.get("window")
    if addr and addr.startswith("0x"):
        # Amener la fenêtre sur le bureau courant puis la focaliser.
        try:
            active = _hyprctl_json("activeworkspace")
            current_ws = None
            if isinstance(active, dict):
                current_ws = active.get("id")
            tab_ws = tab.get("workspace")
            if current_ws is not None and tab_ws and str(tab_ws) != str(current_ws):
                _hypr_dispatch("movetoworkspacesilent",
                               f"{current_ws},address:{addr}")
        except Exception:
            pass
        if _hypr_dispatch("focuswindow", f"address:{addr}"):
            return True
    win_id = tab.get("window")
    if win_id and not str(win_id).startswith("0x"):
        if shutil.which("wmctrl"):
            ok2, _, _ = CommandExecutor.run(f"wmctrl -ia {win_id}", timeout=2)
            if ok2:
                return True
        if shutil.which("xdotool"):
            ok2, _, _ = CommandExecutor.run(
                f"xdotool windowactivate {win_id}", timeout=2)
            if ok2:
                return True
    return ok


@tracked_tool
def close_tab(keyword: str, session_memory=None) -> str:
    """Ferme un onglet par mot-clé (titre, URL ou domaine)."""
    keyword = (keyword or "").strip()
    if not keyword:
        return "❌ Veuillez spécifier un onglet à fermer."
    matching = find_tabs(keyword)
    if not matching:
        return f"❓ Onglet « {keyword} » non trouvé."
    if len(matching) == 1:
        tab = matching[0]
        if _close_one_tab(tab):
            note = "" if tab.get("source") == "cdp" else \
                " (fenêtre entière fermée — pas de CDP)"
            return f"✅ Onglet fermé : {_tab_label(tab)[:80]}{note}"
        return f"⚠️ Impossible de fermer l'onglet « {keyword} »."
    # Plusieurs correspondances → question + mémorisation du choix.
    _sm_set(session_memory, _PENDING_TABS_KEY, matching)
    listing = "\n".join(f"  {i}. {_tab_label(t)[:90]}"
                        for i, t in enumerate(matching, 1))
    return (f"❓ {len(matching)} onglets correspondent à « {keyword} », "
            f"lequel fermer ?\n{listing}\n"
            f"(réponds « ferme 1 », « ferme 2 », « la dernière » ou « toutes »)")


@tracked_tool
def close_tab_by_number(tab_number: int, session_memory=None) -> str:
    """Ferme l'onglet n°N de la liste courante."""
    try:
        tab_number = int(tab_number)
    except (TypeError, ValueError):
        return "❌ Numéro d'onglet invalide."
    tabs = get_open_tabs()
    if tab_number < 1 or tab_number > len(tabs):
        return f"❌ Onglet {tab_number} invalide (il y en a {len(tabs)})."
    tab = tabs[tab_number - 1]
    if _close_one_tab(tab):
        return f"✅ Onglet fermé : {_tab_label(tab)[:80]}"
    return "⚠️ Impossible de fermer cet onglet."


@tracked_tool
def close_all_tabs(keyword: Optional[str], session_memory=None) -> str:
    """Ferme tous les onglets (ou tous ceux correspondant au mot-clé)."""
    targets = find_tabs(keyword) if keyword else get_open_tabs()
    if not targets:
        return "❓ Aucun onglet à fermer."
    closed = 0
    for tab in targets:
        if _close_one_tab(tab):
            closed += 1
        time.sleep(0.05)
    if closed:
        _sm_set(session_memory, _PENDING_TABS_KEY, None)
        return f"✅ {closed} onglet(s) fermé(s)."
    return "⚠️ Aucun onglet n'a pu être fermé."


@tracked_tool
def list_tabs() -> str:
    """Liste tous les onglets/fenêtres navigateur ouverts."""
    tabs = get_open_tabs()
    if not tabs:
        return "❌ Aucun onglet ouvert trouvé."
    lines = [f"📑 {len(tabs)} onglet(s)/fenêtre(s) navigateur :"]
    for i, tab in enumerate(tabs, 1):
        name = tab.get("name", "?")[:70]
        dom = _domain_of(tab.get("url", ""))
        extra = dom or ""
        ws = tab.get("workspace")
        if ws:
            extra = f"{extra} — bureau {ws}" if extra else f"bureau {ws}"
        if tab.get("source") != "cdp":
            extra = f"{extra} (fenêtre)" if extra else "(fenêtre)"
        lines.append(f"  {i}. {name}" + (f" [{extra}]" if extra else ""))
    return "\n".join(lines)


@tracked_tool
def switch_to_tab(keyword: str) -> str:
    """Bascule vers un onglet (CDP) ou sa fenêtre (Hyprland/X11)."""
    keyword = (keyword or "").strip()
    if not keyword:
        return "❌ Veuillez spécifier un onglet."
    tab = find_tab(keyword)
    if not tab:
        return f"❓ Onglet « {keyword} » non trouvé."
    if _activate_one_tab(tab):
        return f"✅ Basculé vers : {_tab_label(tab)[:80]}"
    return "⚠️ Impossible de basculer vers cet onglet."


# ────────────────────────────────────────────────────────────────────────────
# 🌐 Commandes en langage naturel
# ────────────────────────────────────────────────────────────────────────────

@tracked_tool
def close_tab_smart(description: str, session_memory=None) -> str:
    """Fermeture intelligente : « ferme claude », « ferme la page youtube »,
    « ferme l'onglet 2 » (réponse à une question précédente)."""
    description = (description or "").lower().strip()
    if not description:
        return "❌ Veuillez spécifier quel onglet fermer."

    # Réponse à une désambiguïsation en attente (« ferme 2 », « la dernière »,
    # « toutes ») — avec mémoire entre deux tours.
    pending = _sm_get(session_memory, _PENDING_TABS_KEY)
    if pending:
        if re.search(r"\btoutes?\b", description):
            return close_all_tabs(None, session_memory)
        chosen = _resolve_pending_choice(description, pending)
        if chosen:
            _sm_set(session_memory, _PENDING_TABS_KEY, None)
            if _close_one_tab(chosen):
                return f"✅ Onglet fermé : {_tab_label(chosen)[:80]}"
            return "⚠️ Impossible de fermer cet onglet."
        _sm_set(session_memory, _PENDING_TABS_KEY, None)

    # « ferme l'onglet 3 » direct
    m_num = re.search(r"\b(?:onglet|tab|num[ée]ro)\s+(\d+)\b", description)
    if m_num:
        return close_tab_by_number(int(m_num.group(1)), session_memory)

    # Extraction du mot-clé
    keyword = description
    for kw in ("ferme la page", "ferme l'onglet", "ferme onglet", "ferme la",
               "ferme le", "ferme", "close tab", "close page", "close",
               "la page", "l'onglet", "onglet", "page", "tab"):
        keyword = keyword.replace(kw, " ")
    keyword = re.sub(r"\s+", " ", keyword).strip(" '\".,;:!?")
    if not keyword:
        return "❌ Veuillez spécifier quel onglet fermer (ex : « ferme claude »)."
    return close_tab(keyword, session_memory)


# ────────────────────────────────────────────────────────────────────────────
# 🎮 ROUTAGE PRINCIPAL
# ────────────────────────────────────────────────────────────────────────────

@kit.action("browser_tab_control")
def browser_tab_control(parameters: dict, player=None, session_memory=None) -> str:
    """
    Contrôle d'onglets navigateur ultra-puissant.
    Actions :
      - list                          — liste les onglets/fenêtres navigateur
      - close(keyword)                — ferme l'onglet correspondant
      - close_by_number(n)            — ferme l'onglet n°n de la liste
      - close_all                     — ferme tout
      - switch(keyword)               — bascule vers l'onglet
      - close_smart(description)      — langage naturel (« ferme claude »)
    """
    params = parameters or {}
    action = str(params.get("action", "") or "").lower().strip()
    value = str(params.get("value", "") or params.get("keyword", "") or "").strip()

    if player:
        try:
            player.write_log(f"[browser_tab] {action} {value}")
        except Exception:
            pass

    try:
        if action == "list":
            return list_tabs()
        if action == "close":
            return close_tab(value, session_memory=session_memory)
        if action in ("close_by_number", "close_number"):
            return close_tab_by_number(int(value) if value else 1,
                                       session_memory=session_memory)
        if action == "close_all":
            return close_all_tabs(value or None, session_memory=session_memory)
        if action == "switch":
            return switch_to_tab(value)
        if action in ("close_smart", "smart"):
            return close_tab_smart(value, session_memory=session_memory)
        return f"❓ Action inconnue : {action}"
    except Exception as e:
        return handle_tool_error(e, "browser_tab_control")


# ────────────────────────────────────────────────────────────────────────────
# Test direct
# ────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3:
        print(browser_tab_control({"action": sys.argv[1],
                                   "value": " ".join(sys.argv[2:])}))
    elif len(sys.argv) == 2 and sys.argv[1] == "list":
        print(browser_tab_control({"action": "list"}))
    else:
        print("Usage: browser_tab_control.py <list|close|switch|close_smart> [mot-clé]")
