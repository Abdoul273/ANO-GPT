"""
🌐 WEB_CONTROL — Ultra-powerful web automation
Instant Google search, URL open, page navigation with intelligent routing.
Optimisé pour Arch Linux / Hyprland (Wayland), multi-moteurs, workspace-aware.
"""
import os
import re
from core import action_kit as kit
import time
from pathlib import Path
from typing import Optional, Dict, Any
from urllib.parse import quote_plus, urlparse

try:
    from core.tool_utils import (
        retry_on_failure,
        tracked_tool,
        CommandExecutor,
        handle_tool_error,
        check_command_exists,
    )
    _HAS_TOOL_UTILS = True
except Exception:
    _HAS_TOOL_UTILS = False

    def retry_on_failure(max_retries: int = 2, delay_ms: int = 50):
        def deco(fn):
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
        def wrapper(*args, **kwargs):
            return fn(*args, **kwargs)
        wrapper.__name__ = getattr(fn, "__name__", "tool")
        return wrapper

    class CommandExecutor:
        @staticmethod
        def run(cmd: str, timeout: float = 5.0):
            p = kit.run(cmd, shell=True, timeout=timeout)
            if p.timed_out:
                return False, "", "timeout"
            return p.ok, p.out, p.err

    def handle_tool_error(e: Exception, ctx: str) -> str:
        return f"❌ Erreur ({ctx}) : {e}"

    def check_command_exists(cmd: str) -> bool:
        return kit.which(cmd) is not None

# ── Hyprland / Wayland helpers ─────────────────────────────────────────────
_WAYLAND = bool(os.environ.get("WAYLAND_DISPLAY"))


def _hypr_env() -> dict:
    """Environnement avec DISPLAY/WAYLAND_DISPLAY/XDG_RUNTIME_DIR/
    HYPRLAND_INSTANCE_SIGNATURE restaurés si absents."""
    env = {**os.environ}
    if not (env.get("DISPLAY") and env.get("WAYLAND_DISPLAY")):
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


def _move_new_browser_window(needle: Optional[str], workspace,
                             before: set, timeout: float = 4.0) -> bool:
    """Attend qu'une nouvelle fenêtre navigateur apparaisse et la déplace
    silencieusement vers le bureau demandé."""
    if not (_WAYLAND and kit.which("hyprctl")):
        return False
    try:
        ws = str(int(workspace))
    except (TypeError, ValueError):
        ws = str(workspace)
    deadline = time.monotonic() + timeout
    fallback = None
    needle_l = (needle or "").lower()
    while time.monotonic() < deadline:
        clients = _hyprctl_json("clients") or []
        for c in clients:
            addr = c.get("address", "")
            if not addr or addr in before:
                continue
            blob = (c.get("class", "") + " " + c.get("title", "") +
                    " " + c.get("initialClass", "")).lower()
            if needle_l and needle_l in blob:
                return _hypr_dispatch("movetoworkspacesilent",
                                      f"{ws},address:{addr}")
            if fallback is None:
                fallback = addr
        if fallback and time.monotonic() > deadline - 1.0:
            return _hypr_dispatch("movetoworkspacesilent",
                                  f"{ws},address:{fallback}")
        time.sleep(0.15)
    if fallback:
        return _hypr_dispatch("movetoworkspacesilent",
                              f"{ws},address:{fallback}")
    return False

# ────────────────────────────────────────────────────────────────────────────
# 🔍 BROWSER DETECTION
# ────────────────────────────────────────────────────────────────────────────
BROWSERS = {
    "chrome": ["google-chrome-stable", "google-chrome", "chromium"],
    "firefox": ["firefox"],
    "edge": ["microsoft-edge-stable", "microsoft-edge"],
    "brave": ["brave-browser", "brave"],
    "opera": ["opera"],
    "vivaldi": ["vivaldi-stable", "vivaldi"],
}

_DEFAULT_BROWSER_CACHE: Optional[str] = None


def _resolve_browser_binary(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    key = name.lower().strip()
    if kit.which(key):
        return key
    for alias, bins in BROWSERS.items():
        if key == alias or key in bins:
            for b in bins:
                if kit.which(b):
                    return b
    return None


def _get_default_browser() -> str:
    from core.browser_policy import chrome_binary
    return chrome_binary() or ""


def _has_any_browser() -> bool:
    return bool(_get_default_browser())

# ────────────────────────────────────────────────────────────────────────────
# 🌐 SEARCH ENGINES & SHORTCUTS
# ────────────────────────────────────────────────────────────────────────────
_SEARCH_ENGINES: Dict[str, str] = {
    "google": "https://www.google.com/search?q=",
    "bing": "https://www.bing.com/search?q=",
    "ecosia": "https://www.ecosia.org/search?q=",
    "yahoo": "https://search.yahoo.com/search?p=",
    "qwant": "https://www.qwant.com/?q=",
    "startpage": "https://www.startpage.com/sp/search?query=",
}

SHORTCUTS = {
    "gmail": "https://mail.google.com",
    "courriel": "https://mail.google.com",
    "mail": "https://mail.google.com",
    "google": "https://google.com",
    "youtube": "https://youtube.com",
    "github": "https://github.com",
    "stackoverflow": "https://stackoverflow.com",
    "reddit": "https://reddit.com",
    "linkedin": "https://linkedin.com",
    "twitter": "https://twitter.com",
    "x": "https://twitter.com",
    "facebook": "https://facebook.com",
    "instagram": "https://instagram.com",
    "netflix": "https://netflix.com",
    "spotify": "https://open.spotify.com",
    "drive": "https://drive.google.com",
    "docs": "https://docs.google.com",
    "cartes": "https://maps.google.com",
    "maps": "https://maps.google.com",
    "agenda": "https://calendar.google.com",
    "calendar": "https://calendar.google.com",
    "wikipedia": "https://fr.wikipedia.org",
    "twitch": "https://twitch.tv",
    "discord": "https://discord.com/app",
}

# ────────────────────────────────────────────────────────────────────────────
# 🔗 URL helpers
# ────────────────────────────────────────────────────────────────────────────
def _normalize_url(url: str) -> Optional[str]:
    """Retourne une URL valide, ou None si ça ressemble à une requête texte."""
    url = (url or "").strip().strip("'\"")
    if not url:
        return None
    if url.startswith(("http://", "https://", "file://", "ftp://")):
        return url
    if url.startswith("www."):
        return "https://" + url
    if " " in url:
        return None  # ressemble à une requête de recherche
    return "https://" + url


def _open_and_maybe_move(url: str, browser: Optional[str] = None,
                         workspace=None) -> str:
    """Ouvre une URL dans le navigateur choisi/par défaut, puis déplace la
    fenêtre vers le bureau demandé si spécifié."""
    if not _has_any_browser():
        return "❌ Google Chrome est introuvable. Aucun autre navigateur n’a été ouvert."
    binary = _get_default_browser()
    before: set = set()
    if _WAYLAND and kit.which("hyprctl"):
        before = {c.get("address", "") for c in (_hyprctl_json("clients") or [])}
    if kit.spawn([binary, url], env=_hypr_env()) is None:
        return handle_tool_error(RuntimeError(f"lancement de {binary} refusé"), "open_url")
    ws_note = ""
    if workspace is not None:
        needle = Path(binary).name.lower() if binary else None
        if _move_new_browser_window(needle, workspace, before):
            ws_note = f" (bureau {workspace})"
    try:
        domain = urlparse(url).netloc or url
    except Exception:
        domain = url
    return f"✅ Ouverture : {domain}{ws_note}"

# ────────────────────────────────────────────────────────────────────────────
# 🌐 WEB OPERATIONS
# ────────────────────────────────────────────────────────────────────────────
@tracked_tool
def open_url(url: str, browser: Optional[str] = None, workspace=None) -> str:
    """Ouvre une URL. Si l'entrée contient des espaces, la traite comme une recherche."""
    normalized = _normalize_url(url)
    if normalized is None:
        return web_search(url, engine="google", browser=browser, workspace=workspace)
    return _open_and_maybe_move(normalized, browser=browser, workspace=workspace)


@tracked_tool
def web_search(query: str, engine: str = "google",
               browser: Optional[str] = None, workspace=None) -> str:
    """Recherche sur le moteur choisi (google, bing, ecosia…)."""
    query = (query or "").strip()
    if not query:
        return "❌ Veuillez spécifier une requête"
    base = _SEARCH_ENGINES.get(engine.lower(), _SEARCH_ENGINES["google"])
    return _open_and_maybe_move(base + quote_plus(query),
                                browser=browser, workspace=workspace)


@tracked_tool
def google_search(query: str, browser: Optional[str] = None, workspace=None) -> str:
    return web_search(query, engine="google", browser=browser, workspace=workspace)


@tracked_tool
def youtube_search(query: str, browser: Optional[str] = None, workspace=None) -> str:
    query = (query or "").strip()
    if not query:
        return "❌ Veuillez spécifier une requête"
    url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
    return _open_and_maybe_move(url, browser=browser, workspace=workspace)


@tracked_tool
def wikipedia_search(query: str, lang: str = "fr",
                     browser: Optional[str] = None, workspace=None) -> str:
    query = (query or "").strip()
    if not query:
        return "❌ Veuillez spécifier une requête"
    url = f"https://{lang}.wikipedia.org/wiki/{quote_plus(query.replace(' ', '_'))}"
    return _open_and_maybe_move(url, browser=browser, workspace=workspace)


@tracked_tool
def github_search(query: str, browser: Optional[str] = None, workspace=None) -> str:
    query = (query or "").strip()
    if not query:
        return "❌ Veuillez spécifier une requête"
    url = f"https://github.com/search?q={quote_plus(query)}"
    return _open_and_maybe_move(url, browser=browser, workspace=workspace)


@tracked_tool
def open_shortcut(shortcut_name: str, browser: Optional[str] = None,
                  workspace=None) -> str:
    shortcut = (shortcut_name or "").lower().strip()
    if shortcut in SHORTCUTS:
        return _open_and_maybe_move(SHORTCUTS[shortcut],
                                    browser=browser, workspace=workspace)
    available = ", ".join(sorted(SHORTCUTS.keys()))
    return f"❓ Raccourci inconnu. Disponible : {available}"

# ────────────────────────────────────────────────────────────────────────────
# 📱 SMART ROUTING (understand user intent)
# ────────────────────────────────────────────────────────────────────────────
def _parse_web_command_locally(text: str) -> Optional[Dict[str, Any]]:
    """Interprète une phrase naturelle pour le web."""
    text = (text or "").lower().strip()
    text = re.sub(r"\b(s'il te pla[iî]t|stp|please|peux-tu|tu peux|je veux|j'aimerais)\b",
                  "", text).strip()

    # 1. URL explicite avec un point
    m = re.search(
        r"(?:ouvre|va\s+sur|affiche|charge|navigue\s+vers|go\s+to|open|visit)\s+"
        r"(?:le\s+site\s+)?(?:la\s+page\s+)?['\"]?"
        r"([a-zA-Z0-9.-]+\.[a-zA-Z]{2,}(?:/[^\s'\"]*)?)['\"]?",
        text,
    )
    if m:
        return {"action": "open_url", "value": m.group(1).strip()}

    # 2. Nom de site connu (sans point) après un verbe d'ouverture
    m = re.search(
        r"(?:ouvre|va\s+sur|affiche|charge|navigue\s+vers|go\s+to|open|visit)\s+"
        r"(?:le\s+site\s+)?(?:la\s+page\s+)?['\"]?([a-z0-9-]+)['\"]?$",
        text,
    )
    if m:
        name = m.group(1).strip()
        if name in SHORTCUTS:
            return {"action": "shortcut", "value": name}
        if "." in name:
            return {"action": "open_url", "value": name}
        # sinon on laisse tomber -> recherche plus bas

    # 3. Recherche (avec moteur optionnel)
    m = re.search(
        r"(?:cherche|recherche|rechercher|search|google|trouve)\s+(?:pour\s+)?"
        r"(?:sur\s+(google|bing|ecosia|yahoo|qwant|startpage)\s+)?(.+)",
        text,
    )
    if m:
        engine = m.group(1) or "google"
        query = m.group(2).strip()
        return {"action": "search", "value": query, "engine": engine}

    # 4. Raccourci mentionné n'importe où
    for key in SHORTCUTS:
        if re.search(rf"\b{re.escape(key)}\b", text):
            return {"action": "shortcut", "value": key}

    return None


@kit.action("web_control")
@tracked_tool
def web_control(parameters: dict, player=None, **kwargs) -> str:
    """
    Ultra-powerful web control routing.
    Actions supportées :
      - open_url(url, browser?, workspace?)
      - search / google(query, engine?, browser?, workspace?)
      - youtube(query)
      - wikipedia(query, lang?)
      - github(query)
      - shortcut(name)
      - description : phrase naturelle
    """
    params = parameters or {}
    action = (params.get("action") or "").lower().strip()
    value = (params.get("value") or "").strip()
    description = (params.get("description") or "").strip()
    browser = params.get("browser")
    workspace = params.get("workspace")
    engine = params.get("engine", "google")
    lang = params.get("lang", "fr")

    if player:
        try:
            player.write_log(f"[web] {action or description}")
        except Exception:
            pass

    # Interprétation d'une description naturelle
    if description and not action:
        local = _parse_web_command_locally(description)
        if local:
            action = local.get("action", "")
            value = local.get("value", value)
            engine = local.get("engine", engine)
        else:
            # Repli intelligent : URL ou recherche
            guess = _normalize_url(description)
            if guess:
                return open_url(description, browser=browser, workspace=workspace)
            return web_search(description, engine="google",
                              browser=browser, workspace=workspace)

    try:
        if action in ("open_url", "open", "go_to"):
            return open_url(value, browser=browser, workspace=workspace)
        elif action in ("search", "google"):
            return web_search(value, engine=engine, browser=browser, workspace=workspace)
        elif action == "youtube":
            return youtube_search(value, browser=browser, workspace=workspace)
        elif action == "wikipedia":
            return wikipedia_search(value, lang=lang, browser=browser, workspace=workspace)
        elif action == "github":
            return github_search(value, browser=browser, workspace=workspace)
        elif action == "shortcut":
            return open_shortcut(value, browser=browser, workspace=workspace)
        else:
            return f"❓ Action inconnue : {action}"
    except Exception as e:
        return handle_tool_error(e, "web_control")

# ────────────────────────────────────────────────────────────────────────────
# Test direct
# ────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        print(web_control({"description": " ".join(sys.argv[1:])}))
    else:
        print("Usage: python web_control.py \"<commande naturelle>\"")
