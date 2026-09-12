#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
launch_tracker.py — Journal des fenêtres ouvertes par Jarvis/ANO-GPT.

Résout le problème « ferme kitty que tu viens d'ouvrir » : sans ce journal,
close_app ne peut que faire correspondre un nom d'app à toutes les fenêtres
qui matchent, et les ferme donc toutes. Ici on retient l'adresse Hyprland
exacte de chaque fenêtre lancée par l'assistant, ce qui permet un ciblage
chirurgical sur une seule fenêtre.

Robustesse :
- autonome : si actions.window_instances est absent ou en panne, bascule
  sur `hyprctl -j` natif — le journal reste pleinement utilisable ;
- écritures atomiques (fichier temporaire + os.replace) + verrou thread :
  le journal ne peut plus être corrompu par deux appels simultanés ;
- dédoublonnage par adresse Hyprland ;
- détection de la nouvelle fenêtre avec préférence pour l'app demandée
  (évite d'attribuer la fenêtre d'une autre app lors de lancements
  simultanés), repli après 2 s sur la première nouvelle fenêtre ;
- surnom d'instance et workspace mémorisés dans chaque entrée ;
- respect de XDG_CONFIG_HOME.

Fichier : $XDG_CONFIG_HOME/jarvis/launch_journal.json
Format :
[
  { "app": "kitty", "nickname": "main-term", "address": "0x55f...",
    "pid": 12345, "class": "kitty", "initialClass": "kitty",
    "title": "~", "initialTitle": "~", "workspace": 4,
    "opened_at": 1738000000.0 }
]
Le plus récent est en fin de liste. Les entrées dont la fenêtre a disparu
sont purgées à chaque lecture : le journal ne ment jamais sur l'état réel.
"""
from __future__ import annotations

import json
import os
from core import action_kit as kit
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

# ════════════════════════════════════════════════════════════════════════════
# Accès Hyprland — module compagnon optionnel, repli natif
# ════════════════════════════════════════════════════════════════════════════

try:
    from actions.window_instances import hyprctl_json as _wi_hyprctl_json
    from actions.window_instances import list_windows as _wi_list_windows
    _HAS_WINDOW_INSTANCES = True
except Exception:
    _wi_hyprctl_json = None
    _wi_list_windows = None
    _HAS_WINDOW_INSTANCES = False


def _native_hypr_json(cmd: str):
    """hyprctl -j natif, via le socle (délai, reprise, cache partagé)."""
    return kit.hypr_json(*cmd.split(), default=None)


def hyprctl_json(cmd: str):
    """Accès JSON à Hyprland : window_instances d'abord, repli natif."""
    if _HAS_WINDOW_INSTANCES:
        try:
            data = _wi_hyprctl_json(cmd)
            if data is not None:
                return data
        except Exception:
            pass
    return _native_hypr_json(cmd)


def list_windows() -> List[dict]:
    """Toutes les fenêtres Hyprland visibles."""
    if _HAS_WINDOW_INSTANCES:
        try:
            wins = _wi_list_windows()
            if isinstance(wins, list):
                return wins
        except Exception:
            pass
    data = _native_hypr_json("clients")
    return data if isinstance(data, list) else []


# ════════════════════════════════════════════════════════════════════════════
# Configuration & persistance atomique
# ════════════════════════════════════════════════════════════════════════════

_CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
JOURNAL_PATH = _CONFIG_HOME / "jarvis" / "launch_journal.json"

# Au-delà de cette durée, une fenêtre n'est plus « celle que tu viens d'ouvrir ».
RECENT_WINDOW_SECONDS = 1800.0
# Nombre max d'entrées conservées (le journal reste petit et rapide à lire).
_MAX_ENTRIES = 60
_LOCK = threading.Lock()


def _load_raw_unlocked() -> List[Dict[str, Any]]:
    if not JOURNAL_PATH.exists():
        return []
    try:
        data = json.loads(JOURNAL_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_raw_unlocked(entries: List[Dict[str, Any]]) -> None:
    """Écriture atomique : fichier temporaire puis os.replace.
    Un lecteur ne voit jamais un JSON à moitié écrit."""
    try:
        JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(entries[-_MAX_ENTRIES:], indent=2, ensure_ascii=False)
        fd, tmp = tempfile.mkstemp(dir=str(JOURNAL_PATH.parent),
                                   prefix=".launch_journal.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, JOURNAL_PATH)
        except Exception:
            try:
                os.unlink(tmp)
            except Exception:
                pass
    except Exception:
        pass


def _load_raw() -> List[Dict[str, Any]]:
    with _LOCK:
        return _load_raw_unlocked()


def _save_raw(entries: List[Dict[str, Any]]) -> None:
    with _LOCK:
        _save_raw_unlocked(entries)


# ════════════════════════════════════════════════════════════════════════════
# Snapshot / détection de la fenêtre nouvellement apparue
# ════════════════════════════════════════════════════════════════════════════

def snapshot_addresses() -> Set[str]:
    """Adresses Hyprland actuellement ouvertes. À appeler AVANT un lancement."""
    return {w.get("address", "") for w in list_windows() if w.get("address")}


def _window_blob(win: dict) -> str:
    return " ".join(str(win.get(k) or "") for k in
                    ("class", "initialClass", "title", "initialTitle")).lower()


def detect_new_window(before: Set[str], timeout: float = 8.0, poll: float = 0.15,
                      app_hint: Optional[str] = None) -> Optional[dict]:
    """
    Attend qu'une fenêtre absente de `before` apparaisse, et la renvoie.

    - si `app_hint` est fourni, on préfère une fenêtre dont classe/titre
      correspond à l'app demandée : sans cela, un Chrome qui finit de
      mapper pendant que kitty démarre se fait enregistrer à la place de
      kitty, et close_app ferme ensuite la mauvaise fenêtre ;
    - à défaut de correspondance, on se rabat sur la première nouvelle
      fenêtre après 2 s d'observation (les classes ne collent pas toujours
      au nom d'usage : alias, apps Electron…) ;
    - beaucoup d'applications (Electron, navigateurs) mettent plusieurs
      secondes à mapper leur fenêtre : on sonde jusqu'à `timeout` plutôt
      que de tester une seule fois juste après le Popen.
    """
    before = before or set()
    deadline = time.monotonic() + timeout
    hint = (app_hint or "").strip().lower()
    fallback: Optional[dict] = None
    fallback_at: Optional[float] = None

    while time.monotonic() < deadline:
        for w in list_windows():
            addr = w.get("address")
            if not addr or addr in before:
                continue
            if hint and hint in _window_blob(w):
                return w
            if fallback is None:
                fallback = w
                fallback_at = time.monotonic()
        if fallback is not None and fallback_at is not None \
                and (time.monotonic() - fallback_at) >= 2.0:
            return fallback
        time.sleep(poll)
    return fallback


def record_launch(app: str, before: Set[str], pid: Optional[int] = None,
                  timeout: float = 8.0, nickname: Optional[str] = None,
                  workspace: Optional[int] = None) -> Optional[dict]:
    """
    Enregistre la fenêtre apparue après le lancement de `app`.
    Renvoie le dict de la fenêtre Hyprland, ou None si aucune n'est apparue
    (app sans interface graphique, ou lancement échoué).

    `nickname` : surnom d'instance (« main-term ») — permet ensuite
    « ferme main-term que tu viens d'ouvrir ».
    """
    win = detect_new_window(before, timeout=timeout,
                            app_hint=nickname or app)
    if not win:
        return None

    addr = win.get("address")
    ws = win.get("workspace") or {}
    ws_id = ws.get("id")
    try:
        ws_id = int(ws_id)
    except (TypeError, ValueError):
        ws_id = None

    entry = {
        "app": (app or "").strip().lower(),
        "nickname": (nickname or "").strip().lower() or None,
        "address": addr,
        "pid": win.get("pid") or pid,
        "class": win.get("class") or "",
        "initialClass": win.get("initialClass") or "",
        "title": win.get("title") or "",
        "initialTitle": win.get("initialTitle") or "",
        "workspace": workspace if workspace is not None else ws_id,
        "opened_at": time.time(),
    }
    with _LOCK:
        # Dédoublonnage : une adresse ne doit apparaître qu'une fois
        # (les relances d'open_app ne doivent pas accumuler d'entrées).
        entries = [e for e in _load_raw_unlocked() if e.get("address") != addr]
        entries.append(entry)
        _save_raw_unlocked(entries)
    # L'historique d'habitudes ne contient que le nom de l'application et le
    # créneau, jamais titre de fenêtre, PID ou chemin de document.
    try:
        from core.habit_model import HabitModel
        HabitModel().record(f"app:{entry['app']}")
    except Exception:
        pass
    return win


def forget(address: str) -> None:
    """Retire une entrée du journal (après fermeture réussie)."""
    if not address:
        return
    with _LOCK:
        entries = [e for e in _load_raw_unlocked()
                   if e.get("address") != address]
        _save_raw_unlocked(entries)


# ════════════════════════════════════════════════════════════════════════════
# Lecture — uniquement des fenêtres réellement vivantes
# ════════════════════════════════════════════════════════════════════════════

def _entry_matches(entry: Dict[str, Any], needle: str) -> bool:
    """Correspondance tolérante : contenance dans les deux sens sur
    app/surnom/classe/titre (« chrome » retrouve « google-chrome »)."""
    n = (needle or "").strip().lower()
    if not n:
        return True
    hay = " ".join(str(entry.get(k) or "").lower() for k in
                   ("app", "nickname", "class", "initialClass",
                    "title", "initialTitle"))
    if n in hay:
        return True
    for key in ("app", "class", "initialClass", "nickname"):
        tok = str(entry.get(key) or "").lower()
        if tok and (tok in n or n in tok):
            return True
    return False


def live_entries(app: Optional[str] = None, max_age: Optional[float] = None,
                 nickname: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Entrées du journal dont la fenêtre existe encore, plus récente en premier.
    `app` filtre sur le nom demandé, `nickname` sur le surnom d'instance,
    `max_age` écarte les fenêtres ouvertes il y a trop longtemps.
    Les entrées mortes sont purgées du disque à l'occasion.
    """
    with _LOCK:
        entries = _load_raw_unlocked()
        alive = snapshot_addresses()
        kept = [e for e in entries if e.get("address") in alive]
        if len(kept) != len(entries):
            _save_raw_unlocked(kept)

    now = time.time()
    result = []
    for e in kept:
        if max_age is not None and (now - e.get("opened_at", 0)) > max_age:
            continue
        if nickname and str(e.get("nickname") or "").lower() != nickname.strip().lower():
            continue
        if app and not _entry_matches(e, app):
            continue
        result.append(e)
    result.sort(key=lambda e: e.get("opened_at", 0), reverse=True)
    return result


def last_launched(app: Optional[str] = None,
                  max_age: float = RECENT_WINDOW_SECONDS,
                  nickname: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """La fenêtre vivante la plus récemment ouverte par l'assistant."""
    entries = live_entries(app=app, max_age=max_age, nickname=nickname)
    return entries[0] if entries else None


def is_tracked(address: str) -> bool:
    """Vrai si cette fenêtre a été ouverte par l'assistant et vit toujours."""
    return any(e.get("address") == address for e in live_entries())


def cleanup() -> int:
    """Purge les entrées mortes ; renvoie le nombre d'entrées retirées."""
    with _LOCK:
        entries = _load_raw_unlocked()
        alive = snapshot_addresses()
        kept = [e for e in entries if e.get("address") in alive]
        removed = len(entries) - len(kept)
        if removed:
            _save_raw_unlocked(kept)
    return removed


# ════════════════════════════════════════════════════════════════════════════
# Fenêtre active (pour « ferme cette fenêtre », « ferme ça »)
# ════════════════════════════════════════════════════════════════════════════

def active_window() -> Optional[dict]:
    """La fenêtre actuellement focalisée sous Hyprland."""
    data = hyprctl_json("activewindow")
    if not data:
        return None
    win = data[0] if isinstance(data, list) else data
    return win if isinstance(win, dict) and win.get("address") else None


# ════════════════════════════════════════════════════════════════════════════
# Test direct : python launch_tracker.py [app]
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    cleanup()
    print(f"Journal : {JOURNAL_PATH}")
    entries = live_entries(app=arg)
    if not entries:
        print("Aucune fenêtre suivie actuellement"
              + (f" pour '{arg}'." if arg else "."))
    for e in entries:
        nick = f" [{e['nickname']}]" if e.get("nickname") else ""
        print(f"- {e.get('app')}{nick} @ {e.get('address')} "
              f"(ws {e.get('workspace')}, {e.get('class')})")
    aw = active_window()
    if aw:
        print(f"Fenêtre active : {aw.get('class')} — {aw.get('title')}")
