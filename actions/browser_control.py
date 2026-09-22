#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
browser_control.py — JARVIS Browser Engine, version ultra-robuste.
Parsing local avancé, gestion intelligente des sessions, orchestration
multi-navigateur, ouverture native rapide, déplacement de fenêtre vers un
bureau Hyprland, purge automatique des sessions inactives.

Robustesse clé :
- Playwright optionnel : absent, tout bascule en ouverture native au lieu
  de faire mourir le module ;
- démarrage de navigateur hors verrou (pas de blocage global de 30 s) ;
- erreurs d'initialisation propagées (plus de timeouts fantômes) ;
- boucle asyncio réellement arrêtée à la fermeture (pas de fuite de thread) ;
- URL invalides repliées en recherche Google ; requêtes encodées quote_plus ;
- fenêtre du navigateur déplacée silencieusement vers le bureau demandé.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import platform
import re
import shutil
from core import action_kit as kit
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Optional, Any, Dict, List, Tuple, Callable

# Playwright coûte ~170ms à importer (bootstrap de son driver) — un import
# au niveau module retarderait l'affichage de l'orbe à chaque démarrage,
# même quand aucune automatisation navigateur n'est jamais utilisée. On ne
# vérifie que sa présence (quasi gratuit) et on charge le vrai module au
# premier usage réel, dans _init_playwright() — seul point d'entrée qui
# instancie jamais un objet Playwright dans ce fichier.
import importlib.util
from core.live_model_policy import FAST_MODEL

_HAS_PLAYWRIGHT = importlib.util.find_spec("playwright") is not None
async_playwright = None
BrowserContext = Page = Playwright = None
_playwright_import_attempted = False


class PlaywrightTimeout(Exception):
    pass


class PlaywrightError(Exception):
    pass


def _ensure_playwright_imported() -> bool:
    global _playwright_import_attempted, _HAS_PLAYWRIGHT
    global async_playwright, BrowserContext, Page, Playwright
    global PlaywrightTimeout, PlaywrightError
    if _playwright_import_attempted:
        return _HAS_PLAYWRIGHT
    _playwright_import_attempted = True
    if not _HAS_PLAYWRIGHT:
        return False
    try:
        from playwright.async_api import (
            async_playwright as _ap, BrowserContext as _bc, Page as _pg,
            Playwright as _pw, TimeoutError as _pt, Error as _pe,
        )
        async_playwright, BrowserContext, Page, Playwright = _ap, _bc, _pg, _pw
        PlaywrightTimeout, PlaywrightError = _pt, _pe
        return True
    except Exception:
        _HAS_PLAYWRIGHT = False
        return False


logger = logging.getLogger("JARVIS.browser")
_OS = platform.system()  # "Windows" | "Darwin" | "Linux"

# ── Performance / Fiabilité ─────────────────────────────────────────────────
RETRY_COUNT = 2
PAGE_WAIT_TIMEOUT = 5000   # ms
SESSION_KEEP_ALIVE = 300   # secondes d'inactivité avant purge de session


# ════════════════════════════════════════════════════════════════════════════
# Environnement Wayland/Hyprland
# ════════════════════════════════════════════════════════════════════════════

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


def _wayland_env() -> dict:
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


def _hypr_clients() -> List[dict]:
    """Fenêtres ouvertes, via le socle (cache partagé avec les autres actions)."""
    return kit.hypr_clients()


def _hypr_dispatch(dispatcher: str, arg: str = "") -> str:
    res = kit.hypr("dispatch", dispatcher, *([arg] if arg else []))
    if res.not_found:
        return "error: hyprctl introuvable"
    if not res.ok:
        return f"error: {res.err.strip() or 'échec'}"
    return res.out.strip() or "ok"


def _ok(res: str) -> bool:
    low = (res or "").lower()
    return "error" not in low and "unknown" not in low and "invalid" not in low


def _snapshot_addrs() -> set:
    return {c.get("address", "") for c in _hypr_clients() if c.get("address")}


_BROWSER_WIN_CLASSES: Dict[str, Tuple[str, ...]] = {
    "chrome": ("google-chrome", "chromium", "chrome"),
    "firefox": ("firefox",),
    "edge": ("microsoft-edge", "msedge"),
    "brave": ("brave-browser", "brave"),
    "vivaldi": ("vivaldi", "vivaldi-stable"),
    "opera": ("opera",),
    "operagx": ("opera", "opera-gx"),
}


def _move_new_browser_window(browser_name: str, workspace: int,
                             before: set, timeout: float = 6.0) -> bool:
    """Déplace silencieusement la fenêtre du navigateur apparue après
    l'ouverture vers le bureau demandé. Préfère une correspondance de
    classe WM, se rabat sur la première nouvelle fenêtre après ~3,5 s."""
    if not shutil.which("hyprctl"):
        return False
    needles = [n.lower() for n in
               _BROWSER_WIN_CLASSES.get((browser_name or "").lower(),
                                        (browser_name or "",))]
    deadline = time.monotonic() + timeout
    named_deadline = time.monotonic() + max(2.0, timeout * 0.6)
    fallback_addr = None
    while time.monotonic() < deadline:
        clients = _hypr_clients()
        new = [c for c in clients
               if c.get("address") and c["address"] not in before]
        for c in new:
            cls = (c.get("class") or "").lower()
            title = (c.get("title") or "").lower()
            if any(n in cls or n in title for n in needles):
                return _ok(_hypr_dispatch("movetoworkspacesilent",
                                          f"{workspace},address:{c['address']}"))
        if fallback_addr is None and new:
            fallback_addr = new[0]["address"]
        if fallback_addr and time.monotonic() >= named_deadline:
            return _ok(_hypr_dispatch("movetoworkspacesilent",
                                      f"{workspace},address:{fallback_addr}"))
        time.sleep(0.2)
    if fallback_addr:
        return _ok(_hypr_dispatch("movetoworkspacesilent",
                                  f"{workspace},address:{fallback_addr}"))
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


def _extract_workspace(text: str) -> Tuple[Optional[int], str]:
    """Retourne (numéro de workspace, texte nettoyé de la mention)."""
    if not text:
        return None, text or ""
    ws = None
    m = re.search(rf"{_PREP}{_WS}\s*(?:n[°o]?\s*|num[ée]ro\s+)?(\d+)", text, re.I)
    if m:
        ws = int(m.group(1))
    if ws is None:
        m = re.search(rf"{_PREP}(\d+)\s*(?:er|ère|ere|[èe]me|eme|e)?\s*{_WS}",
                      text, re.I)
        if m:
            ws = int(m.group(1))
    if ws is None:
        m = re.search(rf"{_PREP}({_ORD_FR})\s*{_WS}", text, re.I)
        if m:
            ws = _ordinal_to_int(m.group(1))
    if ws is None:
        m = re.search(rf"{_PREP}{_WS}\s*(?:n[°o]?\s*|num[ée]ro\s+)?({_ORD_FR})",
                      text, re.I)
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
# Helpers URL
# ════════════════════════════════════════════════════════════════════════════

def _normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return "about:blank"
    url = url.strip(' \t\'".,;:!?')
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        return url
    # Mot unique sans point → on suppose un .com (« youtube » → youtube.com)
    if " " not in url and "/" not in url and "." not in url:
        url += ".com"
    if " " not in url:
        url = "https://" + url
    return url


def _is_valid_url(url: str) -> bool:
    return bool(re.match(r"^(https?|ftp|file)://[^\s/$.?#].[^\s]*$", url or ""))


_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
    if _OS == "Linux" else
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
    if _OS == "Windows" else
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


# ════════════════════════════════════════════════════════════════════════════
# Résolution de profils (conservée pour référence / usage futur)
# ════════════════════════════════════════════════════════════════════════════

def _real_profile_dir(browser: str) -> str:
    home = Path.home()
    base = None
    if _OS == "Windows":
        local = os.environ.get("LOCALAPPDATA", "")
        roam = os.environ.get("APPDATA", "")
        maps = {
            "chrome": Path(local) / "Google" / "Chrome" / "User Data",
            "edge": Path(local) / "Microsoft" / "Edge" / "User Data",
            "brave": Path(local) / "BraveSoftware" / "Brave-Browser" / "User Data",
            "vivaldi": Path(local) / "Vivaldi" / "User Data",
            "opera": Path(roam) / "Opera Software" / "Opera Stable",
            "operagx": Path(roam) / "Opera Software" / "Opera GX Stable",
        }
        base = maps.get(browser)
    elif _OS == "Darwin":
        lib = home / "Library" / "Application Support"
        maps = {
            "chrome": lib / "Google" / "Chrome",
            "edge": lib / "Microsoft Edge",
            "brave": lib / "BraveSoftware" / "Brave-Browser",
            "vivaldi": lib / "Vivaldi",
            "opera": lib / "com.operasoftware.Opera",
            "operagx": lib / "com.operasoftware.OperaGX",
        }
        base = maps.get(browser)
    else:
        cfg = home / ".config"
        maps = {
            "chrome": cfg / "google-chrome",
            "chromium": cfg / "chromium",
            "edge": cfg / "microsoft-edge",
            "brave": cfg / "BraveSoftware" / "Brave-Browser",
            "vivaldi": cfg / "vivaldi",
            "opera": cfg / "opera",
            "operagx": cfg / "opera-gx",
        }
        candidates = []
        if browser in maps:
            candidates.append(maps[browser])
        if browser == "chrome" and "chromium" in maps:
            candidates.append(maps["chromium"])
        for p in candidates:
            if p.exists():
                return str(p)
    if base and base.exists():
        return str(base)
    iso = home / ".jarvis_profiles" / browser
    iso.mkdir(parents=True, exist_ok=True)
    return str(iso)


def _firefox_profile_dir() -> Optional[str]:
    home = Path.home()
    if _OS == "Windows":
        base = Path(os.environ.get("APPDATA", "")) / "Mozilla" / "Firefox"
    elif _OS == "Darwin":
        base = home / "Library" / "Application Support" / "Firefox"
    else:
        base = home / ".mozilla" / "firefox"
    ini = base / "profiles.ini"
    if not ini.exists():
        return None
    text = ini.read_text(encoding="utf-8", errors="ignore")
    current: Dict[str, str] = {}
    default_path = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            if current.get("Default") == "1" and "Path" in current:
                path_val = current["Path"]
                default_path = (str(base / path_val)
                                if current.get("IsRelative", "1") == "1"
                                else path_val)
            current = {}
        elif "=" in line:
            k, _, v = line.partition("=")
            current[k.strip()] = v.strip()
    if current.get("Default") == "1" and "Path" in current:
        path_val = current["Path"]
        default_path = (str(base / path_val)
                        if current.get("IsRelative", "1") == "1"
                        else path_val)
    if default_path and Path(default_path).exists():
        return default_path
    return None


_BROWSER_BIN_MAP: Dict[str, Dict[str, List[str]]] = {
    "Linux": {
        "chrome": ["google-chrome-stable", "google-chrome"],
        "edge": ["microsoft-edge-stable", "microsoft-edge"],
        "firefox": ["firefox"],
        "opera": ["opera"],
        "operagx": ["opera-gx", "opera"],
        "brave": ["brave-browser", "brave"],
        "vivaldi": ["vivaldi-stable", "vivaldi"],
    },
    "Windows": {
        "chrome": ["chrome.exe"],
        "edge": ["msedge.exe"],
        "firefox": ["firefox.exe"],
        "opera": ["opera.exe"],
        "operagx": ["opera.exe"],
        "brave": ["brave.exe"],
        "vivaldi": ["vivaldi.exe"],
    },
    "Darwin": {
        "chrome": ["Google Chrome"],
        "edge": ["Microsoft Edge"],
        "firefox": ["Firefox"],
        "opera": ["Opera"],
        "operagx": ["Opera GX"],
        "brave": ["Brave Browser"],
        "vivaldi": ["Vivaldi"],
    },
}


def _find_binary(browser: str) -> Optional[str]:
    names = _BROWSER_BIN_MAP.get(_OS, {}).get(browser, [])
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    if _OS == "Darwin":
        app_names = {
            "chrome": "Google Chrome", "edge": "Microsoft Edge",
            "firefox": "Firefox", "opera": "Opera", "operagx": "Opera GX",
            "brave": "Brave Browser", "vivaldi": "Vivaldi",
        }
        app = app_names.get(browser)
        if app:
            app_path = Path("/Applications") / f"{app}.app" / "Contents" / "MacOS"
            if app_path.exists():
                for exe in app_path.iterdir():
                    if exe.is_file():
                        return str(exe)
    if _OS == "Windows" and names:
        try:
            import winreg
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                for key in [
                    rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{names[0]}",
                    rf"SOFTWARE\Clients\StartMenuInternet\{browser}\shell\open\command",
                ]:
                    try:
                        with winreg.OpenKey(hive, key) as reg:
                            val = winreg.QueryValue(reg, None)
                        exe = val.strip().strip('"').split('"')[0].split(" --")[0].strip()
                        if exe and Path(exe).exists():
                            return exe
                    except Exception:
                        continue
        except Exception:
            pass
    return None


_SEARCH_ENGINES: Dict[str, str] = {
    "google": "https://www.google.com/search?q=",
    "bing": "https://www.bing.com/search?q=",
    "yandex": "https://yandex.com/search/?text=",
    "yahoo": "https://search.yahoo.com/search?p=",
    "ecosia": "https://www.ecosia.org/search?q=",
}


def _build_search_url(query: str, engine: str = "google") -> str:
    base = _SEARCH_ENGINES.get((engine or "google").lower(),
                               _SEARCH_ENGINES["google"])
    return base + urllib.parse.quote_plus(query or "")


# ════════════════════════════════════════════════════════════════════════════
# Ouverture native (rapide, sans Playwright)
# ════════════════════════════════════════════════════════════════════════════

def _open_native(url: str, browser_name: Optional[str] = None) -> str:
    from core.browser_policy import open_chrome
    url = _normalize_url(url)
    if open_chrome(url, env=_wayland_env()):
        return f"Ouvert dans chrome : {url}" if url else "chrome lancé."
    return "Impossible de lancer Google Chrome. Aucun autre navigateur n’a été ouvert."


# ════════════════════════════════════════════════════════════════════════════
# Session Playwright
# ════════════════════════════════════════════════════════════════════════════

class BrowserSession:
    def __init__(self, name: str, headless: bool = False):
        self.name = "chrome"
        self.headless = headless
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._init_error: Optional[BaseException] = None
        self._pw: Optional[Playwright] = None
        self._browser = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._closed = False
        self._is_cdp = False
        self._last_used = time.monotonic()

    # ── Cycle de vie ──────────────────────────────────────────────────────
    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self, timeout: float = 30.0):
        if self.is_alive():
            return
        self._closed = False
        self._init_error = None
        self._ready.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True,
                                        name=f"browser-{self.name}")
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError(f"Le navigateur {self.name} n'a pas démarré dans les {timeout}s")
        if self._init_error:
            raise RuntimeError(f"Échec d'initialisation de {self.name} : {self._init_error}")

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._init_playwright())
        except Exception as e:
            self._init_error = e
            self._ready.set()
            return
        self._ready.set()
        self._loop.run_forever()

    async def _init_playwright(self):
        if not _ensure_playwright_imported():
            raise RuntimeError("Playwright n'est pas installé.")
        self._pw = await async_playwright().start()

    def _browser_is_disconnected(self) -> bool:
        """Vrai si Chrome a été fermé hors du contrôle de Playwright."""
        browser = self._browser
        if browser is None and self._context is not None:
            browser = getattr(self._context, "browser", None)
        if browser is None:
            return False
        try:
            return not browser.is_connected()
        except Exception:
            # Un handle Playwright devenu invalide équivaut à un navigateur
            # déconnecté ; le relancer est plus sûr que réutiliser ce contexte.
            return True

    async def _relaunch_disconnected_browser(self) -> None:
        """Purge un contexte mort puis redémarre Playwright, une seule fois."""
        logger.info("Chrome fermé manuellement : réinitialisation Playwright.")
        try:
            # Une connexion CDP emprunte le navigateur de l'utilisateur :
            # le reconnecter ne doit pas fermer ses onglets.
            if self._context is not None and not getattr(self, "_is_cdp", False):
                await self._context.close()
        except Exception:
            logger.debug("Fermeture du contexte Playwright déjà indisponible", exc_info=True)
        try:
            if self._pw is not None:
                await self._pw.stop()
        except Exception:
            logger.debug("Arrêt Playwright déjà indisponible", exc_info=True)
        self._pw = self._browser = self._context = self._page = None
        self._is_cdp = False
        await self._init_playwright()

    def run(self, coro, timeout: float = 60.0) -> str:
        if self._closed:
            raise RuntimeError("Session fermée")
        if not self._loop or self._loop.is_closed():
            raise RuntimeError("Session non démarrée")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            result = future.result(timeout=timeout)
            self._last_used = time.monotonic()
            return result
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError(f"L'opération a expiré après {timeout}s")

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._loop and self._loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(self._destroy(), self._loop).result(10)
            except Exception:
                pass
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:
                pass

    async def _destroy(self):
        if getattr(self, "_is_cdp", False):
            logger.info("Déconnexion de la session CDP utilisateur.")
        else:
            try:
                if self._context:
                    await self._context.close()
            except Exception:
                pass
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass

    # ── Lancement / contexte ──────────────────────────────────────────────
    async def _ensure_context(self, *, relaunch_attempted: bool = False):
        # Chrome peut être fermé manuellement entre deux commandes. Ne jamais
        # laisser « Target page, context or browser has been closed » fuiter :
        # une reconnexion est tentée, exactement une fois par action.
        if self._browser_is_disconnected():
            if relaunch_attempted:
                raise RuntimeError("Chrome est resté fermé après la relance Playwright.")
            await self._relaunch_disconnected_browser()
            return await self._ensure_context(relaunch_attempted=True)
        if self._context is None:
            await self._launch(retry=RETRY_COUNT)
        try:
            if self._page is None or self._page.is_closed():
                pages = [page for page in self._context.pages if not page.is_closed()]
                self._page = pages[-1] if pages else await self._context.new_page()
            # Page.evaluate ne possède pas de paramètre timeout : borner
            # l'attente asyncio sans fermer une page saine par erreur.
            await asyncio.wait_for(self._page.evaluate("1"), timeout=3.0)
        except Exception as exc:
            if relaunch_attempted:
                raise RuntimeError("Chrome inaccessible après une tentative de récupération.") from exc
            await self._relaunch_disconnected_browser()
            return await self._ensure_context(relaunch_attempted=True)

    async def _launch(self, retry: int = 0):
        engine = self._resolve_engine()
        # Tentative CDP : se connecter au Chrome/Brave/Edge déjà ouvert sur 9222
        if engine == "chromium" and self.name in ("chrome", "brave", "edge"):
            try:
                logger.info("Tentative de connexion CDP port 9222...")
                self._browser = await self._pw.chromium.connect_over_cdp(
                    "http://localhost:9222", timeout=3000)
                if self._browser.contexts:
                    self._context = self._browser.contexts[0]
                else:
                    self._context = await self._browser.new_context()
                self._is_cdp = True
                self._page = await self._adopt_page()
                self._last_used = time.monotonic()
                logger.info("Connexion CDP réussie.")
                return
            except Exception as cdp_err:
                logger.info(f"CDP non dispo ({cdp_err}). Lancement Playwright avec profil isolé.")
                self._is_cdp = False

        executable = _find_binary(self.name)
        if not executable:
            raise RuntimeError(f"Exécutable introuvable pour {self.name}")

        common_args = [
            "--start-maximized",
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--disable-default-apps",
            "--no-default-browser-check",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ]
        if self.headless:
            common_args.append("--headless=new")

        jarvis_profile = str(Path.home() / ".jarvis_profiles" / self.name)
        Path(jarvis_profile).mkdir(parents=True, exist_ok=True)
        kwargs: Dict[str, Any] = {
            "headless": self.headless,
            "slow_mo": 0,
            "viewport": None,
            "no_viewport": True,
            "timeout": 30000,
            "args": common_args,
            "executable_path": executable,
        }
        if engine == "firefox":
            ff_profile = _firefox_profile_dir() or jarvis_profile
            try:
                self._context = await self._pw.firefox.launch_persistent_context(
                    ff_profile, **kwargs)
            except Exception:
                jarvis_ff = str(Path.home() / ".jarvis_profiles" / "firefox_jarvis")
                Path(jarvis_ff).mkdir(parents=True, exist_ok=True)
                self._context = await self._pw.firefox.launch_persistent_context(
                    jarvis_ff, **kwargs)
        else:
            engine_obj = getattr(self._pw, engine)
            try:
                self._context = await engine_obj.launch_persistent_context(
                    jarvis_profile, **kwargs)
            except Exception as e:
                logger.warning(f"Échec lancement Playwright ({e}). Nouvelle tentative avec profil vierge.")
                clean = tempfile.mkdtemp(prefix="jarvis_browser_")
                try:
                    self._context = await engine_obj.launch_persistent_context(
                        clean, **kwargs)
                except Exception as e2:
                    raise RuntimeError(
                        f"Impossible de lancer {self.name}: {e2}. "
                        "Essayez de fermer Chrome manuellement ou de relancer JARVIS.")
        self._page = await self._adopt_page()
        self._last_used = time.monotonic()

    def _resolve_engine(self) -> str:
        if self.name in ("chrome", "edge", "brave", "vivaldi", "opera", "operagx"):
            return "chromium"
        if self.name == "firefox":
            return "firefox"
        if self.name == "safari":
            return "webkit"
        raise ValueError(f"Navigateur non supporté : {self.name}")

    async def _adopt_page(self) -> Page:
        await asyncio.sleep(0.3)
        pages = self._context.pages
        return pages[0] if pages else await self._context.new_page()

    async def get_page(self) -> Page:
        await self._ensure_context()
        if getattr(self, "_is_cdp", False) and self._context:
            try:
                pages = self._context.pages
                if not self._page or self._page.is_closed():
                    self._page = pages[-1] if pages else await self._context.new_page()
            except Exception:
                pass
        return self._page

    # ── Navigation ────────────────────────────────────────────────────────
    async def go_to(self, url: str, wait_until: str = "domcontentloaded") -> str:
        raw = (url or "").strip()
        if not raw:
            return "Aucune URL fournie."
        normalized = _normalize_url(raw)
        if not _is_valid_url(normalized) and normalized != "about:blank":
            # Pas une URL → on cherche au lieu de répondre « invalide ».
            return await self.search(raw, "google")
        page = await self.get_page()
        try:
            await page.goto(normalized, wait_until=wait_until, timeout=30000)
            try:
                await page.wait_for_load_state("networkidle",
                                               timeout=PAGE_WAIT_TIMEOUT)
            except PlaywrightTimeout:
                pass
            return f"Ouvert : {page.url}"
        except PlaywrightTimeout:
            return f"Délai d'attente dépassé pour {normalized}"
        except Exception as e:
            return f"Erreur de navigation : {e}"

    async def search(self, query: str, engine: str = "google") -> str:
        return await self.go_to(_build_search_url(query, engine))

    async def click(self, selector: str = None, text: str = None) -> str:
        page = await self.get_page()
        try:
            if text:
                await page.get_by_text(text, exact=False).first.click(timeout=8000)
                return f"Cliqué sur le texte : '{text}'"
            elif selector:
                await page.click(selector, timeout=8000)
                return f"Cliqué sur le sélecteur : {selector}"
            return "Aucune cible spécifiée."
        except PlaywrightTimeout:
            return "Élément introuvable."
        except Exception as e:
            return f"Erreur de clic : {e}"

    async def type_text(self, selector: str, text: str, clear_first: bool = True) -> str:
        page = await self.get_page()
        try:
            if not selector or not str(selector).strip():
                await page.keyboard.type(text, delay=50)
                return f"Texte saisi (champ actif) : '{text}'"
            el = page.locator(selector).first
            if clear_first:
                try:
                    await el.clear()
                except Exception:
                    pass
            await el.type(text, delay=50)
            return f"Texte tapé : '{text}'"
        except Exception as e:
            return f"Erreur de saisie : {e}"

    async def scroll(self, direction: str = "down", amount: int = 500) -> str:
        page = await self.get_page()
        y = amount if direction == "down" else -amount
        await page.mouse.wheel(0, y)
        return f"Défilement {direction} de {amount}px"

    async def press(self, key: str) -> str:
        page = await self.get_page()
        await page.keyboard.press(key)
        return f"Touche pressée : {key}"

    async def get_text(self, selector: str = "body", max_chars: int = 6000) -> str:
        """Texte lisible de la page : contenu principal d'abord, blanc replié,
        longueur bornée — une page entière ferait déborder le tour de parole."""
        page = await self.get_page()
        try:
            text = ""
            if selector in ("", "body"):
                # Le contenu principal sans menus ni pieds de page, quand la
                # page le balise ; sinon le corps entier.
                for candidate in ("main", "article", "[role=main]", "body"):
                    try:
                        loc = page.locator(candidate).first
                        if await loc.count() == 0:
                            continue
                        text = await loc.inner_text(timeout=4000)
                        if len(text.strip()) > 200 or candidate == "body":
                            break
                    except Exception:
                        continue
            else:
                text = await page.inner_text(selector)
            text = re.sub(r"[ \t\r\f\v]+", " ", text)
            text = re.sub(r"\n\s*\n+", "\n", text).strip()
            if len(text) > max_chars:
                text = text[:max_chars] + f"\n… (tronqué, {len(text)} caractères au total)"
            return text or "La page ne contient aucun texte lisible."
        except Exception as e:
            return f"Erreur de lecture texte : {e}"

    async def get_url(self) -> str:
        page = await self.get_page()
        return page.url

    async def fill_form(self, fields: dict) -> str:
        page = await self.get_page()
        results = []
        for selector, value in (fields or {}).items():
            try:
                el = page.locator(selector).first
                await el.clear()
                await el.type(str(value), delay=40)
                results.append(f"✓ {selector}")
            except Exception as e:
                results.append(f"✗ {selector}: {e}")
        return "Formulaire rempli : " + ", ".join(results)

    async def smart_click(self, description: str) -> str:
        page = await self.get_page()
        # Nom accessible insensible à la casse et aux accents approximatifs :
        # « Se connecter » doit trouver le bouton « SE CONNECTER ».
        pattern = re.compile(re.escape(description.strip()), re.I)
        for role in ("button", "link", "searchbox", "textbox", "menuitem", "tab",
                     "checkbox", "radio", "option"):
            try:
                loc = page.get_by_role(role, name=pattern)
                if await loc.count() > 0:
                    await loc.first.scroll_into_view_if_needed(timeout=3000)
                    await loc.first.click(timeout=5000)
                    return f"Cliqué sur le {role} : '{description}'"
            except Exception:
                pass
        for finder in (lambda: page.get_by_label(pattern),
                       lambda: page.get_by_placeholder(pattern),
                       lambda: page.get_by_title(pattern),
                       lambda: page.get_by_alt_text(pattern),
                       lambda: page.get_by_text(pattern)):
            try:
                loc = finder()
                if await loc.count() > 0:
                    await loc.first.scroll_into_view_if_needed(timeout=3000)
                    await loc.first.click(timeout=5000)
                    return f"Cliqué sur : '{description}'"
            except Exception:
                continue
        return f"Élément introuvable : '{description}'"

    async def smart_type(self, description: str, text: str) -> str:
        page = await self.get_page()
        if description:
            candidates = [
                page.get_by_placeholder(description, exact=False),
                page.get_by_label(description, exact=False),
                page.get_by_role("textbox", name=description),
                page.get_by_role("searchbox"),
                page.get_by_role("combobox", name=description),
            ]
            for loc in candidates:
                try:
                    el = loc.first
                    if await el.count() == 0:
                        continue
                    await el.scroll_into_view_if_needed(timeout=3000)
                    await el.click(timeout=3000)
                    try:
                        await el.clear()
                    except Exception:
                        pass
                    await el.type(text, delay=40)
                    role = await el.get_attribute("type") or ""
                    aria = await el.get_attribute("role") or ""
                    if role in ("search", "text") or aria == "searchbox" \
                            or "search" in description.lower():
                        await page.keyboard.press("Enter")
                    return f"Saisi dans '{description}' : {text!r}"
                except Exception:
                    continue
        # Repli : champ actuellement actif
        try:
            await page.keyboard.type(text, delay=40)
            return f"Saisi (champ actif) : {text!r}"
        except Exception as e:
            return f"Champ de saisie introuvable pour '{description}' : {e}"

    # ── Onglets ───────────────────────────────────────────────────────────
    async def new_tab(self, url: str = "") -> str:
        page = await self.get_page()
        ctx = page.context
        new = await ctx.new_page()
        self._page = new
        if url:
            return await self.go_to(url)
        return "Nouvel onglet ouvert."

    async def close_tab(self, target: Optional[str] = None) -> str:
        if target:
            target_str = str(target).lower().strip()
            if self._context:
                for p in list(self._context.pages):
                    try:
                        url = p.url.lower()
                        title = (await p.title()).lower() if not p.is_closed() else ""
                        if target_str in url or target_str in title:
                            await p.close()
                            if self._page == p:
                                remaining = self._context.pages
                                self._page = remaining[-1] if remaining else None
                            return f"Onglet '{target}' fermé."
                    except Exception:
                        continue
                return f"Aucun onglet correspondant à '{target}' n'a été trouvé."
        page = self._page
        if page and not page.is_closed():
            ctx = page.context
            await page.close()
            pages = ctx.pages
            self._page = pages[-1] if pages else None
            return "Onglet fermé."
        return "Aucun onglet actif."

    async def list_tabs(self) -> str:
        await self.get_page()
        if not self._context:
            return "Aucun onglet ouvert."
        lines = []
        for i, p in enumerate(self._context.pages, 1):
            try:
                title = await p.title()
            except Exception:
                title = "?"
            lines.append(f"{i}. {title} — {p.url}")
        return "Onglets ouverts :\n" + "\n".join(lines) if lines else "Aucun onglet ouvert."

    # ── Divers ────────────────────────────────────────────────────────────
    async def screenshot(self, path: str = None, selector: str = None) -> str:
        page = await self.get_page()
        save_path = path or str(Path.home() / "Pictures" / "jarvis_screenshot.png")
        try:
            Path(save_path).parent.mkdir(parents=True, exist_ok=True)
            if selector:
                await page.locator(selector).first.screenshot(path=save_path)
            else:
                await page.screenshot(path=save_path, full_page=False)
            return f"Capture d'écran sauvegardée : {save_path}"
        except Exception as e:
            return f"Erreur capture : {e}"

    async def back(self) -> str:
        page = await self.get_page()
        await page.go_back(timeout=10000)
        return f"Retour : {page.url}"

    async def forward(self) -> str:
        page = await self.get_page()
        await page.go_forward(timeout=10000)
        return f"Avant : {page.url}"

    async def reload(self) -> str:
        page = await self.get_page()
        await page.reload(timeout=15000)
        return f"Rechargé : {page.url}"

    async def download(self, url: str, save_dir: str = None) -> str:
        page = await self.get_page()
        try:
            async with page.expect_download() as download_info:
                # evaluate paramétré : pas d'injection JS via l'URL
                await page.evaluate("(u) => { window.location.href = u; }", url)
            download = await download_info.value
            save_dir = save_dir or str(Path.home() / "Downloads")
            dest = Path(save_dir) / download.suggested_filename
            dest.parent.mkdir(parents=True, exist_ok=True)
            await download.save_as(dest)
            return f"Téléchargé : {dest}"
        except Exception as e:
            return f"Erreur de téléchargement : {e}"

    async def upload_file(self, selector: str, file_path: str) -> str:
        page = await self.get_page()
        try:
            await page.set_input_files(selector, file_path)
            return f"Fichier téléversé : {file_path}"
        except Exception as e:
            return f"Erreur d'upload : {e}"

    async def execute_js(self, script: str) -> str:
        page = await self.get_page()
        try:
            result = await page.evaluate(script)
            return f"Résultat JS : {result}"
        except Exception as e:
            return f"Erreur JS : {e}"

    async def get_cookies(self) -> str:
        page = await self.get_page()
        cookies = await page.context.cookies()
        return json.dumps(cookies, indent=2, ensure_ascii=False)


# ════════════════════════════════════════════════════════════════════════════
# Session Manager (verrou relâché pendant les démarrages + reaper d'inactivité)
# ════════════════════════════════════════════════════════════════════════════

class SessionManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._sessions: Dict[str, BrowserSession] = {}
        self._active_browser: Optional[str] = None
        self._last_native_url: str = ""
        self._reaper_stop = threading.Event()
        t = threading.Thread(target=self._reaper_loop, daemon=True,
                             name="browser-reaper")
        t.start()

    def _reaper_loop(self):
        """Ferme les sessions inactives depuis plus de SESSION_KEEP_ALIVE."""
        while not self._reaper_stop.wait(60):
            now = time.monotonic()
            with self._lock:
                idle = [n for n, s in self._sessions.items()
                        if not s._closed and
                        (now - s._last_used) > SESSION_KEEP_ALIVE]
            for n in idle:
                try:
                    self.close(n)
                    logger.info(f"Session '{n}' fermée (inactivité).")
                except Exception:
                    pass

    def has_sessions(self) -> bool:
        with self._lock:
            return any(not s._closed for s in self._sessions.values())

    def get(self, browser_name: Optional[str] = None,
            headless: bool = False) -> BrowserSession:
        if not _HAS_PLAYWRIGHT:
            raise RuntimeError("Playwright n'est pas installé.")
        name = "chrome"
        with self._lock:
            sess = self._sessions.get(name)
            if sess is None or sess._closed:
                sess = BrowserSession(name, headless=headless)
                self._sessions[name] = sess
            self._active_browser = name
        # Démarrage HORS verrou : un lancement de navigateur peut prendre
        # plusieurs dizaines de secondes sans bloquer les autres commandes.
        if not sess.is_alive():
            sess.start()
        return sess

    def switch(self, browser_name: str) -> str:
        name = "chrome"
        self.get(name)
        return f"Navigateur actif → {name}"

    def close(self, browser_name: Optional[str] = None) -> str:
        name = self._resolve_name(browser_name or self._active_browser)
        with self._lock:
            sess = self._sessions.pop(name, None)
            if self._active_browser == name:
                self._active_browser = None
        if sess:
            sess.close()
            return f"{name} fermé."
        return f"Aucune session pour {name}."

    def close_all(self) -> str:
        with self._lock:
            names = list(self._sessions.keys())
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._active_browser = None
        for s in sessions:
            try:
                s.close()
            except Exception:
                pass
        return "Tous les navigateurs fermés : " + (", ".join(names) if names else "aucun")

    def list_sessions(self) -> str:
        with self._lock:
            if not self._sessions:
                return "Aucun navigateur actif."
            lines = []
            for name in self._sessions:
                marker = " ◀ actif" if name == self._active_browser else ""
                lines.append(f"  • {name}{marker}")
        return "Navigateurs ouverts :\n" + "\n".join(lines)

    def note_native_url(self, url: str):
        self._last_native_url = _normalize_url(url)

    def pop_native_url(self) -> str:
        url, self._last_native_url = self._last_native_url, ""
        return url

    @staticmethod
    def _resolve_name(name: str) -> str:
        name = (name or "").lower().strip()
        aliases = {
            "google chrome": "chrome",
            "ms edge": "edge",
            "msedge": "edge",
            "mozilla firefox": "firefox",
            "opera gx": "operagx",
        }
        return aliases.get(name, name)


_manager = SessionManager()


def current_browser_url() -> str:
    """URL connue sans lancer ni focaliser un nouveau navigateur."""
    with _manager._lock:
        native = _manager._last_native_url
        active = _manager._active_browser
        session = _manager._sessions.get(active) if active else None
    if session is not None and not session._closed and session.is_alive():
        try:
            return str(session.run(session.get_url(), timeout=3.0)).strip()
        except Exception:
            pass
    return str(native or "").strip()


# ════════════════════════════════════════════════════════════════════════════
# Parsing local des commandes de navigation
# ════════════════════════════════════════════════════════════════════════════

def _parse_browser_command_locally(text: str) -> Optional[Dict[str, Any]]:
    """Interprète les commandes naturelles de navigation web."""
    text = (text or "").lower().strip()
    text = re.sub(r"\b(s'il te pla[iî]t|stp|please|peux[- ]tu|tu peux|je veux|peux tu)\b",
                  " ", text).strip()
    text = re.sub(r"\s+", " ", text)

    # 1a. URL explicite (« ouvre google.com/maps », « va sur https://… »)
    m = re.search(
        r"(?:va\s+sur|ouvre|affiche|charge|navigue\s+vers|go\s+to|open)\s+"
        r"(?:le\s+site\s+)?(?:la\s+page\s+)?[\"']?"
        r"([a-zA-Z0-9.-]+\.[a-zA-Z]{2,}(?:/[^\s\"']*)?)[\"']?", text)
    if m:
        return {"action": "go_to", "url": m.group(1)}

    # 1b. Nom de site simple (« va sur youtube » → .com ajouté par normalize)
    m = re.search(
        r"(?:va\s+sur|ouvre|navigue\s+vers)\s+(?:le\s+site\s+)?[\"']?"
        r"([a-z0-9][a-z0-9-]*)[\"']?\s*$", text)
    if m:
        return {"action": "go_to", "url": m.group(1)}

    # 2. Recherche
    m = re.search(r"(?:cherche|recherche|search|trouve)\s+(?:pour\s+)?(.+)", text)
    if m:
        query = m.group(1).strip()
        engine = "google"
        eng = re.search(r"(?:sur|avec|using)\s+(google|bing|yahoo|ecosia|yandex)", text)
        if eng:
            engine = eng.group(1).lower()
            query = re.sub(rf"\s*(?:sur|avec|using)\s+{engine}\s*$", "", query).strip()
        return {"action": "search", "query": query, "engine": engine}

    # 3. Clic
    m = re.search(
        r"(?:clique|click)\s+(?:sur\s+)?(?:le\s+)?(?:bouton\s+|lien\s+|l'[ée]l[ée]ment\s+)?"
        r"['\"]?(.+?)['\"]?$", text)
    if m:
        return {"action": "smart_click", "description": m.group(1).strip()}

    # 4. Saisie (« tape X dans Y » ou « tape X » → champ actif)
    m = re.search(
        r"(?:tape|[ée]cris|saisis|entre|remplis)\s+(?:le\s+texte\s+|le\s+champ\s+)?"
        r"['\"]?(.+?)['\"]?(?:\s+dans\s+(?:le\s+)?(.+))?$", text)
    if m:
        content = m.group(1).strip().strip("'\"")
        field = m.group(2).strip() if m.lastindex and m.lastindex >= 2 and m.group(2) else None
        if field:
            return {"action": "smart_type", "description": field, "text": content}
        return {"action": "smart_type", "description": "", "text": content}

    # 5. Navigation simple
    if re.search(r"\b(rafra[îi]chis?|recharge|reload|actualise)\b", text):
        return {"action": "reload"}
    if re.search(r"\b(reviens\s+en\s+arri[èe]re|retour|page\s+pr[ée]c[ée]dente|back)\b", text):
        return {"action": "back"}
    if re.search(r"\b(avance|page\s+suivante|forward)\b", text):
        return {"action": "forward"}
    if re.search(r"\b(nouvel\s+onglet|ouvre\s+un\s+onglet|new\s+tab)\b", text):
        url_m = re.search(r"(?:avec|sur|à)\s+(.+)$", text)
        return {"action": "new_tab",
                "url": url_m.group(1).strip() if url_m else ""}
    if re.search(r"\bliste\s+(?:les\s+)?onglets\b|\bquels\s+onglets\b|\bmes\s+onglets\b", text):
        return {"action": "list_tabs"}

    m_close = re.search(r"\b(?:ferme\s+l'onglet|close\s+tab)\s+(.+)", text)
    if m_close:
        return {"action": "close_tab", "url": m_close.group(1).strip()}
    if re.search(r"\b(ferme\s+l'onglet|close\s+tab)\b", text):
        m_tag = re.search(r"l'onglet\s+(\S+)\s+ferme\s+le", text)
        if m_tag:
            return {"action": "close_tab", "url": m_tag.group(1).strip()}
        return {"action": "close_tab"}

    if re.search(r"\b(capture\s+d'[ée]cran|screenshot)\b", text):
        return {"action": "screenshot"}

    if re.search(r"\b(d[ée]file\s+vers\s+le\s+(haut|bas)|scroll\s+(up|down))", text):
        dir_m = re.search(r"(haut|bas|up|down)", text)
        amt_m = re.search(r"(\d+)\s*(?:px|pixels)?", text)
        direction = "up" if dir_m and dir_m.group(1) in ("haut", "up") else "down"
        amount = int(amt_m.group(1)) if amt_m else 500
        return {"action": "scroll", "direction": direction, "amount": amount}

    # 6. Changer de navigateur
    m = re.search(r"(?:utilise|passe\s+[àa]|bascule\s+sur|switch\s+to)\s+"
                  r"(?:le\s+navigateur\s+)?(\S+)", text)
    if m:
        return {"action": "switch", "target": m.group(1).strip().lower()}

    # 7/8. Fermer / lister
    if re.search(r"\b(ferme\s+le\s+navigateur|quitte\s+le\s+navigateur|close\s+browser)\b", text):
        return {"action": "close"}
    if re.search(r"\b(liste\s+des\s+navigateurs|quels\s+navigateurs|open\s+browsers)\b", text):
        return {"action": "list_browsers"}
    return None


def _get_api_key() -> str:
    try:
        config_path = Path(__file__).resolve().parent.parent / "config" / "api_keys.json"
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        return config.get("gemini_api_key", "")
    except Exception:
        return ""


def _detect_browser_action_via_ai(description: str) -> Optional[Dict]:
    """Fallback IA pour commandes complexes."""
    api_key = _get_api_key()
    if not api_key:
        return None
    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        prompt = f"""Tu es un assistant de navigation web. L'utilisateur a dit : "{description}"
Actions possibles : go_to, search, smart_click, smart_type, type, reload, back, forward, new_tab, close_tab, list_tabs, screenshot, scroll, switch, close, list_browsers.
Renvoie UNIQUEMENT un objet JSON avec le nom de l'action et les paramètres nécessaires.
Exemples :
"va sur https://example.com" -> {{"action":"go_to","url":"https://example.com"}}
"cherche des chats" -> {{"action":"search","query":"chats"}}
"clique sur le bouton valider" -> {{"action":"smart_click","description":"valider"}}
"tape bonjour" -> {{"action":"smart_type","description":"","text":"bonjour"}}
"utilise le navigateur" -> {{"action":"switch","target":"chrome"}}
"ferme l'onglet youtube" -> {{"action":"close_tab","url":"youtube"}}
"liste les onglets" -> {{"action":"list_tabs"}}
Réponds uniquement avec le JSON."""
        resp = client.models.generate_content(model=FAST_MODEL,
                                              contents=prompt)
        json_match = re.search(r"\{.*\}", resp.text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group(0))
    except Exception as e:
        logger.debug(f"AI detection failed: {e}")
    return None


# ════════════════════════════════════════════════════════════════════════════
# Point d'entrée public
# ════════════════════════════════════════════════════════════════════════════

_ACTION_ALIASES = {
    "open": "go_to", "navigate": "go_to", "goto": "go_to",
    "chercher": "search", "rechercher": "search",
    "tape": "type", "ecris": "type", "clic": "click",
    "onglets": "list_tabs",
}

_FAST_NATIVE_ACTIONS = ("go_to", "search", "new_tab")


@kit.action("browser_control")
def browser_control(parameters: dict = None, response=None, player=None,
                    session_memory=None) -> str:
    """Contrôle du navigateur web. Accepte des paramètres classiques ou une
    description en langage naturel. Paramètre optionnel `workspace` (chiffre
    ou ordinal) pour ouvrir la fenêtre sur un bureau précis."""
    params = parameters or {}
    action = str(params.get("action", "") or "").lower().strip()
    description = str(params.get("description", "") or "").strip()
    action = _ACTION_ALIASES.get(action, action)

    # ── Extraction du workspace (chiffres ET ordinaux) ───────────────────
    ws_num: Optional[int] = None
    pw = params.get("workspace")
    if pw is not None:
        try:
            ws_num = int(str(pw).strip())
        except (TypeError, ValueError):
            ws_num, _ = _extract_workspace(str(pw))
    if ws_num is None and description:
        w, description = _extract_workspace(description)
        if w is not None:
            ws_num = w

    # ── Interprétation locale / IA d'une description ─────────────────────
    if description and not action:
        parsed = _parse_browser_command_locally(description)
        if parsed:
            action = parsed.pop("action", "")
            for k, v in parsed.items():
                if k not in params or params[k] is None:
                    params[k] = v
        else:
            ai = _detect_browser_action_via_ai(description)
            if ai:
                action = ai.pop("action", "")
                for k, v in ai.items():
                    if k not in params or params[k] is None:
                        params[k] = v
            else:
                return "Je n'ai pas compris cette commande de navigation. Pouvez-vous reformuler ?"
    if not action:
        return "Aucune action demandée."

    browser_name = "chrome"  # Préférence permanente de l’utilisateur.
    headless = bool(params.get("headless", False))

    # Carte d'état pendant l'automatisation (l'utilisateur voit ce que fait l'IA).
    if player and hasattr(player, "show_card"):
        try:
            detail = params.get("url") or params.get("query") or ""
            body = f"**Action :** {action}" + (f"\n\n{detail}" if detail else "")
            player.show_card("task", "Navigateur", body)
        except Exception:
            pass

    # ── Commandes de session ──────────────────────────────────────────────
    if action == "switch":
        if not _HAS_PLAYWRIGHT:
            return ("Playwright n'est pas installé : impossible de gérer des "
                    "sessions navigateur. Utilisez « va sur … » pour ouvrir en natif.")
        return _manager.switch(browser_name or params.get("target", ""))
    if action == "list_browsers":
        return _manager.list_sessions()
    if action == "close":
        return _manager.close(browser_name)
    if action == "close_all":
        return _manager.close_all()

    if not browser_name:
        browser_name = _manager._active_browser or "chrome"

    # ── Ouverture native rapide (pas de session, ou Playwright absent) ───
    fast_native = action in _FAST_NATIVE_ACTIONS and \
        (not _HAS_PLAYWRIGHT or not _manager.has_sessions())
    if fast_native:
        url = ""
        if action == "search":
            url = _build_search_url(params.get("query", ""),
                                    params.get("engine", "google"))
        else:
            url = params.get("url", "")
        before = _snapshot_addrs() if ws_num is not None else set()
        result = _open_native(url, browser_name)
        if url:
            _manager.note_native_url(url)
        if ws_num is not None:
            if _move_new_browser_window(browser_name, ws_num, before):
                result += f" (bureau {ws_num})"
            else:
                result += f" — déplacement vers le bureau {ws_num} non confirmé"
        _log(player, result)
        return result

    if not _HAS_PLAYWRIGHT:
        return ("Playwright n'est pas installé — impossible d'exécuter "
                f"'{action}'. Installez-le : pip install playwright && "
                "playwright install chromium. Les commandes simples "
                "(« va sur… », « cherche… ») restent possibles en ouverture native.")

    # ── Session Playwright ────────────────────────────────────────────────
    before = _snapshot_addrs() if ws_num is not None else set()
    try:
        sess = _manager.get(browser_name, headless=headless)
    except Exception as e:
        err_msg = str(e)
        if "SingletonLock" in err_msg or "already" in err_msg.lower():
            return ("Chrome est déjà ouvert — le profil est verrouillé. "
                    "JARVIS ouvre une fenêtre isolée. Réessayez dans quelques secondes.")
        if "Target closed" in err_msg or "Tab closed" in err_msg:
            return (f"La page {browser_name} s'est fermée de façon inattendue. "
                    f"Relancez la commande pour ouvrir une nouvelle fenêtre.")
        return f"Impossible de lancer {browser_name}: {e}"

    # Synchroniser la dernière URL ouverte en natif
    last_url = _manager.pop_native_url()
    if last_url:
        try:
            sess.run(sess.go_to(last_url))
        except Exception:
            pass

    method_map: Dict[str, Callable] = {
        "go_to": lambda: sess.run(sess.go_to(params.get("url", ""))),
        "search": lambda: sess.run(sess.search(params.get("query", ""),
                                               params.get("engine", "google"))),
        "click": lambda: sess.run(sess.click(params.get("selector"),
                                             params.get("text"))),
        "smart_click": lambda: sess.run(sess.smart_click(params.get("description", ""))),
        "type": lambda: sess.run(sess.type_text(params.get("selector", ""),
                                                params.get("text", ""),
                                                params.get("clear_first", True))),
        "smart_type": lambda: sess.run(sess.smart_type(params.get("description", ""),
                                                       params.get("text", ""))),
        "scroll": lambda: sess.run(sess.scroll(params.get("direction", "down"),
                                               int(params.get("amount", 500)))),
        "fill_form": lambda: sess.run(sess.fill_form(params.get("fields", {}))),
        "get_text": lambda: sess.run(sess.get_text(params.get("selector", "body"))),
        "get_url": lambda: sess.run(sess.get_url()),
        "press": lambda: sess.run(sess.press(params.get("key", "Enter"))),
        "new_tab": lambda: sess.run(sess.new_tab(params.get("url", ""))),
        "close_tab": lambda: sess.run(sess.close_tab(params.get("url") or
                                                     params.get("query") or
                                                     params.get("description"))),
        "list_tabs": lambda: sess.run(sess.list_tabs()),
        "screenshot": lambda: sess.run(sess.screenshot(params.get("path"),
                                                       params.get("selector"))),
        "back": lambda: sess.run(sess.back()),
        "forward": lambda: sess.run(sess.forward()),
        "reload": lambda: sess.run(sess.reload()),
        "execute_js": lambda: sess.run(sess.execute_js(params.get("script", ""))),
        "download": lambda: sess.run(sess.download(params.get("url", ""),
                                                   params.get("save_dir"))),
        "upload_file": lambda: sess.run(sess.upload_file(params.get("selector", ""),
                                                         params.get("file_path", ""))),
        "get_cookies": lambda: sess.run(sess.get_cookies()),
    }
    handler = method_map.get(action)
    if not handler:
        return f"Action navigateur inconnue : '{action}'."
    try:
        result = handler()
    except (TimeoutError, concurrent.futures.TimeoutError):
        result = f"L'action '{action}' a expiré (60s)."
    except Exception as e:
        result = f"Erreur navigateur ({action}) : {e}"

    if ws_num is not None:
        if _move_new_browser_window(browser_name, ws_num, before):
            result += f" (bureau {ws_num})"
        else:
            result += f" — déplacement vers le bureau {ws_num} non confirmé"

    _log(player, result)
    return result


def _log(player, text: str):
    short = str(text)[:120]
    logger.info(f"[Browser] {short}")
    if player:
        try:
            player.write_log(f"[browser] {short[:60]}")
        except Exception:
            pass


# ════════════════════════════════════════════════════════════════════════════
# Test direct
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.DEBUG)
    if len(sys.argv) > 1:
        phrase = " ".join(sys.argv[1:])
        print(browser_control({"description": phrase}))
    else:
        print("Usage: python browser_control.py <commande naturelle>")
