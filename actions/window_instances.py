"""
window_instances.py – Registre partagé des instances nommées + helpers Hyprland
pour open_app.py et close_app.py.

Permet de cibler une fenêtre PRÉCISE (pas juste « une fenêtre kitty parmi 4 »)
via un surnom donné par l'utilisateur au lancement (« main-term », « notes », …),
et centralise le dialogue avec hyprctl en gérant automatiquement :
  - la nouvelle syntaxe Lua (hl.dsp.*) de Hyprland >= 0.55
  - un repli sur l'ancienne syntaxe (closewindow/focuswindow/killwindow) si besoin

Fichier de registre : ~/.config/jarvis/named_windows.json
Format :
{
  "main-term": { "app": "kitty", "initial_title": "main-term", "pid": 12345, "launched_at": 1732000000.0 }
}

Robustesse :
  - écriture atomique du registre (fichier temporaire + os.replace) + verrou ;
  - tolérance aux fichiers corrompus (backup automatique) ;
  - environnement Hyprland restauré (HYPRLAND_INSTANCE_SIGNATURE) pour que
    hyprctl fonctionne même quand l'assistant tourne en service ;
  - close_window résout les fenêtres par surnom/mot-clé avant de fermer par
    adresse, avec vérification réelle de la disparition.
"""
from __future__ import annotations

import json
import os
from core import action_kit as kit
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

REGISTRY_PATH = Path.home() / ".config" / "jarvis" / "named_windows.json"
_REG_LOCK = threading.Lock()

# Binaires terminal connus supportant un flag de titre fixe au lancement.
# Utilisé par open_app.py pour injecter le bon flag quand on nomme une instance.
TITLE_CAPABLE_BINARIES: Dict[str, str] = {
    "kitty":      "--title",
    "alacritty":  "--title",
    "foot":       "--title",
    "footclient": "--title",
    "xterm":      "-T",
    "uxterm":     "-T",
    "st":         "-t",
    "urxvt":      "-title",
    "rxvt":       "-title",
}

# ═══════════════════════════════════════════════════════════════════════════
# Environnement Hyprland (indispensable en service systemd)
# ═══════════════════════════════════════════════════════════════════════════
def _hypr_env() -> dict:
    """Environnement avec XDG_RUNTIME_DIR et HYPRLAND_INSTANCE_SIGNATURE
    restaurés si absents (sinon hyprctl répond « no running instance »)."""
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
                instances = sorted(
                    (os.path.join(hypr_dir, d) for d in os.listdir(hypr_dir)
                     if os.path.isdir(os.path.join(hypr_dir, d))),
                    key=lambda p: os.path.getmtime(p),
                    reverse=True,
                )
                if instances:
                    env["HYPRLAND_INSTANCE_SIGNATURE"] = os.path.basename(instances[0])
        except Exception:
            pass
    return env

# ═══════════════════════════════════════════════════════════════════════════
# Registre des instances nommées (persistant sur disque)
# ═══════════════════════════════════════════════════════════════════════════
def _ensure_registry_dir() -> None:
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)

def load_registry() -> Dict[str, Any]:
    _ensure_registry_dir()
    if not REGISTRY_PATH.exists():
        return {}
    try:
        data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        # Fichier corrompu : on l'archive pour ne plus bloquer les lectures.
        try:
            REGISTRY_PATH.rename(REGISTRY_PATH.with_suffix(".corrupt.json"))
        except Exception:
            pass
        return {}

def save_registry(data: Dict[str, Any]) -> None:
    _ensure_registry_dir()
    with _REG_LOCK:
        try:
            payload = json.dumps(data, indent=2, ensure_ascii=False)
            fd, tmp = tempfile.mkstemp(
                dir=str(REGISTRY_PATH.parent),
                prefix=".named_windows.",
                suffix=".tmp",
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, REGISTRY_PATH)
        except Exception:
            pass

def register_instance(
    nickname: str, app: str, initial_title: Optional[str], pid: Optional[int]
) -> None:
    if not nickname:
        return
    reg = load_registry()
    reg[nickname.strip().lower()] = {
        "app": app,
        "initial_title": initial_title,
        "pid": pid,
        "launched_at": time.time(),
    }
    save_registry(reg)

def unregister_instance(nickname: str) -> None:
    reg = load_registry()
    if reg.pop(nickname.strip().lower(), None) is not None:
        save_registry(reg)

def get_instance(nickname: str) -> Optional[Dict[str, Any]]:
    if not nickname:
        return None
    return load_registry().get(nickname.strip().lower())

def instance_exists(nickname: str) -> bool:
    return get_instance(nickname) is not None

def list_instances() -> Dict[str, Any]:
    """Retourne une copie du registre des instances nommées."""
    return dict(load_registry())

def title_flag_for(binary: str) -> Optional[str]:
    return TITLE_CAPABLE_BINARIES.get(Path(binary).name.lower())

# ═══════════════════════════════════════════════════════════════════════════
# IPC Hyprland (hyprctl) — lecture d'état
# ═══════════════════════════════════════════════════════════════════════════
def hyprctl_json(cmd: str) -> Optional[List[dict]]:
    """Lecture Hyprland partagée, toujours rendue sous forme de liste.

    Le cache du socle est ce qui compte ici : `list_windows()` est appelée par
    presque toutes les actions de fenêtrage, souvent plusieurs fois dans le
    même tour de parole.
    """
    data = kit.hypr_json(*cmd.split(), default=None)
    if data is None:
        return None
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return [data]
    return None

def list_windows() -> List[dict]:
    return hyprctl_json("clients") or []

# ═══════════════════════════════════════════════════════════════════════════
# IPC Hyprland — dispatch (Lua >= 0.55, repli legacy < 0.55)
# ═══════════════════════════════════════════════════════════════════════════
def _dispatch_raw(payload: str) -> bool:
    res = kit.hypr("dispatch", payload)
    if not res.ok:
        return False
    stdout = res.out.strip().lower()
    stderr = res.err.strip().lower()
    # Hyprland renvoie « unknown dispatcher » pour une commande Lua invalide
    # sur les anciennes versions : il faut alors tenter le repli legacy.
    if "unknown" in stdout or "unknown" in stderr:
        return False
    return "error" not in stdout and "error" not in stderr

def dispatch_hyprland(legacy_cmd: str, legacy_args: str = "", lua_cmd: str = "") -> bool:
    """Centralise les appels hyprctl dispatch en gérant le support Lua
    (Hyprland >= 0.55) et le repli legacy (< 0.55)."""
    if lua_cmd:
        if _dispatch_raw(lua_cmd):
            return True
        # Repli legacy
    legacy_payload = f"{legacy_cmd} {legacy_args}".strip()
    return _dispatch_raw(legacy_payload)

def _lua_selector(selector: str) -> str:
    """Échappe le sélecteur pour l'injecter dans une table Lua."""
    return selector.replace("\\", "\\\\").replace('"', '\\"')

def _address_of(selector: str) -> Optional[str]:
    """Extrait l'adresse d'un sélecteur 'address:0x..' (sinon None)."""
    if selector.lower().startswith("address:"):
        return selector.split(":", 1)[1].strip()
    return None

def _window_exists(address: str) -> bool:
    return any(w.get("address") == address for w in list_windows())

def _close_by_address(address: str, force: bool = False) -> bool:
    """Ferme une fenêtre par son adresse avec vérification réelle.

    Sur Hyprland 0.56, `hl.dsp.window.close()` est accepté mais sans effet,
    seul `hl.dsp.window.kill()` ferme réellement la fenêtre. On ne se fie
    donc jamais au code de retour : on relit la liste des fenêtres pour
    confirmer la disparition.
    """
    if not _window_exists(address):
        return True  # déjà fermée
    sel = _lua_selector(f"address:{address}")
    attempts = [
        f'hl.dsp.window.kill({{ window = "{sel}" }})',    # Hyprland >= 0.55 : le seul qui agit
        f'hl.dsp.window.close({{ window = "{sel}" }})',   # accepté mais no-op sur 0.56
        f"closewindow address:{address}",                  # legacy < 0.55
        f"killwindow address:{address}",
    ]
    for payload in attempts:
        _dispatch_raw(payload)
        # Jusqu'à ~600 ms pour que la fenêtre disparaisse, mais on rend la
        # main dès qu'elle n'est plus là : la plupart ferment en 50 ms.
        if kit.wait_until(lambda: not _window_exists(address),
                          timeout=0.6, interval=0.04, max_interval=0.15):
            return True
    # Dernier recours : signal au processus, mais uniquement s'il ne possède
    # que cette fenêtre (sinon on fermerait les autres fenêtres de la même
    # application, ce qu'on cherche justement à éviter).
    return _kill_owning_process(address, force=force)

def close_window(selector: str, force: bool = False) -> bool:
    """Ferme une fenêtre et VÉRIFIE qu'elle a bien disparu.

    selector: 'address:0x..', 'initialtitle:xxx', 'class:xxx', 'title:xxx',
    ou un surnom/mot-clé du registre.
    """
    addr = _address_of(selector)
    if addr:
        return _close_by_address(addr, force=force)

    # Pas d'adresse : on essaie de résoudre les fenêtres correspondantes
    # (surnom du registre ou mot-clé) puis de les fermer une par une.
    matches = find_windows(selector)
    if matches:
        ok = True
        for w in matches:
            a = w.get("address")
            if a:
                ok = _close_by_address(a, force=force) and ok
            else:
                ok = False
        return ok

    # Aucune correspondance : tentative en aveugle (dispatch direct).
    sel = _lua_selector(selector)
    for lua in (
        f'hl.dsp.window.kill({{ window = "{sel}" }})',
        f'hl.dsp.window.close({{ window = "{sel}" }})',
    ):
        if _dispatch_raw(lua):
            return True
    return _dispatch_raw(f"{'killwindow' if force else 'closewindow'} {selector}")

def _kill_owning_process(address: str, force: bool = False) -> bool:
    """SIGTERM/SIGKILL du process d'une fenêtre, s'il n'en possède qu'une seule."""
    import os as _os
    import signal
    windows = list_windows()
    target = next((w for w in windows if w.get("address") == address), None)
    if not target:
        return True
    pid = target.get("pid")
    if not pid or pid <= 0:
        return False
    if sum(1 for w in windows if w.get("pid") == pid) != 1:
        return False  # process multi-fenêtres : trop risqué
    try:
        _os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
    except Exception:
        return False
    return kit.wait_until(lambda: not _window_exists(address),
                          timeout=1.0, interval=0.05, max_interval=0.2)

def focus_window(selector: str) -> bool:
    legacy_cmd = "focuswindow"
    sel = _lua_selector(selector)
    return dispatch_hyprland(
        legacy_cmd=legacy_cmd,
        legacy_args=selector,
        lua_cmd=f'hl.dsp.focus({{ window = "{sel}" }})'
    )

def move_window_to_workspace(selector: str, workspace: str | int, follow: bool = False) -> bool:
    """Déplace une fenêtre vers un bureau donné (silencieusement par défaut
    ou avec focus si follow=True)."""
    ws = str(workspace).strip()
    sel = _lua_selector(selector)
    # Syntaxe Lua
    follow_lua = "true" if follow else "false"
    lua_cmd = f'hl.dsp.window.move({{ workspace = "{ws}", window = "{sel}", follow = {follow_lua} }})'
    # Syntaxe Legacy
    legacy_cmd = "movetoworkspace" if follow else "movetoworkspacesilent"
    legacy_args = f"{ws},{selector}"
    return dispatch_hyprland(
        legacy_cmd=legacy_cmd,
        legacy_args=legacy_args,
        lua_cmd=lua_cmd
    )

# ═══════════════════════════════════════════════════════════════════════════
# Recherche de fenêtres (par surnom enregistré OU par mot-clé brut)
# ═══════════════════════════════════════════════════════════════════════════
def find_windows(query: str) -> List[dict]:
    """
    Cherche les fenêtres Hyprland correspondant à `query`.
    Priorité 1 : surnom enregistré dans le registre -> match exact sur initialTitle.
    Priorité 2 : recherche floue sur class / initialClass / title / initialTitle.
    """
    windows = list_windows()
    inst = get_instance(query)
    if inst and inst.get("initial_title"):
        needle = inst["initial_title"].strip().lower()
        matches = [w for w in windows if (w.get("initialTitle") or "").strip().lower() == needle]
        if matches:
            return matches
        # Le surnom existe mais la fenêtre n'est plus là -> registre obsolète
        return []
    needle = query.strip().lower()
    return [
        w
        for w in windows
        if needle in (w.get("class") or "").lower()
        or needle in (w.get("initialClass") or "").lower()
        or needle in (w.get("title") or "").lower()
        or needle in (w.get("initialTitle") or "").lower()
    ]

def describe_window(w: dict) -> str:
    """Description lisible pour poser une question de désambiguïsation à l'utilisateur."""
    reg = load_registry()
    init_title = w.get("initialTitle") or ""
    for nick, data in reg.items():
        if data.get("initial_title") and init_title and data["initial_title"].lower() == init_title.lower():
            return f"'{nick}' (bureau {w.get('workspace', {}).get('name', '?')})"
    title = w.get("title") or w.get("initialTitle") or "sans titre"
    return f"{w.get('class', '?')} — «{title}» (bureau {w.get('workspace', {}).get('name', '?')}, PID {w.get('pid', '?')})"

def distinct_signature(w: dict) -> str:
    """Signature utilisée pour détecter si plusieurs fenêtres sont réellement
    des instances DIFFÉRENTES (titres différents) ou juste des doublons identiques."""
    return (w.get("initialTitle") or w.get("title") or w.get("class") or "").strip().lower()

