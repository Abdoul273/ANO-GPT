#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
capture.py — Captures d'écran et enregistrement vidéo sous Wayland/Hyprland,
version corrigée et renforcée.

Pourquoi ce module existe :
    L'ancien chemin de capture passait par `mss`, qui repose sur X11. Sous
    Wayland, `mss` renvoie une image entièrement noire sans lever d'erreur —
    l'assistant croyait donc réussir alors qu'il envoyait une image vide.
    Ici on passe par `grim`, le seul outil qui parle au compositeur Wayland.

Corrections par rapport à la version précédente :
    - `stamp()` était définie mais tous les appels utilisaient `_stamp()`
      → NameError garanti sur chaque capture/enregistrement ;
    - `wl-copy` fuitait un descripteur de fichier ouvert ;
    - aucun environnement Wayland restauré : grim/slurp/swappy/
      Caelestia/hyprctl échouaient quand l'assistant tourne en
      service → WAYLAND_DISPLAY, XDG_RUNTIME_DIR et
      HYPRLAND_INSTANCE_SIGNATURE sont désormais restaurés systématiquement ;
    - les enregistrements vocaux conservent leur propre processus GSR,
      indispensable pour capter simultanément le son système et le micro ;
      ils enregistrent toutefois dans le même dossier que Caelestia ;
    - la capture de fenêtre n'était possible que par adresse Hyprland →
      elle fonctionne aussi par nom/classe de fenêtre.

Ajouts :
    - pause / reprise de l'enregistrement (SIGUSR2, supporté par
      gpu-screen-recorder) ;
    - statut avec durée ET taille déjà écrite ;
    - messages d'erreur avec conseils d'installation précis.

Captures : plein écran via Caelestia Shell quand il est disponible (même
comportement que le raccourci du shell), puis grim + slurp pour les modes
avancés (région, fenêtre, écran précis).
fenêtre ciblée par adresse OU par nom, écran précis.
Enregistrement : GSR persistant, dans le dossier configuré de Caelestia.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
from core import action_kit as kit
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── Emplacements de sortie ──────────────────────────────────────────────────
SHOT_DIR = Path.home() / "Images" / "Captures"
VIDEO_DIR = Path.home() / "Vidéos" / "Enregistrements"

# État de l'enregistrement en cours (PID + fichier), persistant entre appels.
_STATE_PATH = Path.home() / ".config" / "jarvis" / "recording.json"
# Session portail réutilisée : évite de redemander l'autorisation à chaque fois.
_PORTAL_TOKEN = Path.home() / ".config" / "jarvis" / "gsr_portal_token"


def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d%Hh%Mm%Ss")


# Compatibilité : l'ancienne fonction s'appelait stamp()
stamp = _stamp


def _have(binary: str) -> bool:
    return shutil.which(binary) is not None


def _caelestia_screenshots_dir(env: dict) -> Path:
    """Retourne le dossier réellement configuré par Caelestia Shell.

    ANO-GPT peut être démarré par systemd et ne pas hériter des variables de
    la session graphique. On récupère donc CAELESTIA_SCREENSHOTS_DIR depuis
    un processus de cette session avant de reprendre le défaut de Caelestia.
    """
    configured = env.get("CAELESTIA_SCREENSHOTS_DIR")
    if configured:
        return Path(configured).expanduser()
    try:
        uid = os.getuid()
        for pid_dir in Path("/proc").glob("[0-9]*"):
            try:
                if pid_dir.stat().st_uid != uid:
                    continue
                raw = (pid_dir / "environ").read_bytes()
                for item in raw.split(b"\0"):
                    if item.startswith(b"CAELESTIA_SCREENSHOTS_DIR="):
                        return Path(item.split(b"=", 1)[1].decode()).expanduser()
            except (OSError, UnicodeDecodeError):
                continue
    except OSError:
        pass
    return Path.home() / "Pictures" / "Screenshots"


# ════════════════════════════════════════════════════════════════════════════
# Environnement Wayland/Hyprland restauré
# ════════════════════════════════════════════════════════════════════════════

def _restore_display_env(env: dict) -> None:
    """Si l'assistant n'a ni DISPLAY ni WAYLAND_DISPLAY (service systemd,
    ssh…), on les récupère depuis un processus de la session graphique."""
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


def _hypr_env() -> dict:
    """Environnement complet pour grim/slurp/swappy/gpu-screen-recorder/
    hyprctl : sans XDG_RUNTIME_DIR le portail xdg-desktop-portal ne répond
    pas, sans HYPRLAND_INSTANCE_SIGNATURE hyprctl répond « no instance »."""
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


def _hyprctl_json(cmd: str) -> Any:
    """Lecture Hyprland partagée (socle : délai, reprise, cache court).

    Chaque module gardait sa copie de cette fonction et relançait un processus
    par question. Le cache du socle fusionne les appels d'un même tour de
    parole : plusieurs actions qui listent les fenêtres n'en paient qu'un.
    """
    return kit.hypr_json(*cmd.split(), default=None)


# ════════════════════════════════════════════════════════════════════════════
# Géométries de fenêtres
# ════════════════════════════════════════════════════════════════════════════

def _client_geometry(client: dict) -> Optional[str]:
    """Géométrie 'X,Y WxH' d'un client Hyprland, au format attendu par grim -g."""
    if not isinstance(client, dict):
        return None
    at, size = client.get("at"), client.get("size")
    if not (isinstance(at, list) and isinstance(size, list)
            and len(at) == 2 and len(size) == 2):
        return None
    return f"{at[0]},{at[1]} {size[0]}x{size[1]}"


def _active_window_geometry() -> Optional[str]:
    try:
        from core import screen_capture
        win = screen_capture.get_active_window(skip_anogpt=True)
        if win and win.geometry_str:
            return win.geometry_str
    except Exception:
        pass
    win = _hyprctl_json("activewindow")
    return _client_geometry(win) if isinstance(win, dict) else None


def _find_window(query: str) -> Optional[dict]:
    """Retrouve une fenêtre par adresse Hyprland exacte, sinon par
    sous-chaîne dans classe/titre (« kitty », « youtube »…)."""
    q = (query or "").strip()
    if not q:
        return None
    clients = _hyprctl_json("clients")
    if not isinstance(clients, list):
        return None
    for c in clients:
        if isinstance(c, dict) and c.get("address") == q:
            return c
    ql = q.lower()
    for c in clients:
        if not isinstance(c, dict):
            continue
        blob = " ".join(str(c.get(k) or "") for k in
                        ("class", "initialClass", "title", "initialTitle")).lower()
        if ql in blob:
            return c
    return None


def _window_geometry_by_address(address: str) -> Optional[str]:
    c = _find_window(address)
    return _client_geometry(c) if c else None


def _current_monitor() -> Optional[str]:
    monitors = _hyprctl_json("monitors")
    if not isinstance(monitors, list):
        return None
    for m in monitors:
        if isinstance(m, dict) and m.get("focused"):
            return m.get("name")
    for m in monitors:
        if isinstance(m, dict) and m.get("name"):
            return m.get("name")
    return None


def list_monitors() -> str:
    monitors = _hyprctl_json("monitors")
    if not isinstance(monitors, list) or not monitors:
        return "Aucun écran détecté."
    lines = []
    for m in monitors:
        if not isinstance(m, dict):
            continue
        lines.append(
            f"- {m.get('name')} : {m.get('width')}x{m.get('height')}"
            f"@{round(m.get('refreshRate', 0))}Hz"
            f"{' (actif)' if m.get('focused') else ''}"
        )
    return "Écrans détectés :\n" + "\n".join(lines) if lines else "Aucun écran détecté."


# ════════════════════════════════════════════════════════════════════════════
# Captures d'écran (grim)
# ════════════════════════════════════════════════════════════════════════════

def _take_screenshot_caelestia(
    output: Optional[str],
    copy_clipboard: bool,
    annotate: bool,
    env: dict,
) -> Dict[str, Any]:
    """Utilise le point d'entrée officiel de Caelestia pour le plein écran.

    La commande ne prend pas de chemin de sortie : elle choisit son dossier,
    copie l'image dans le presse-papiers et affiche sa notification. Si un
    chemin a été demandé à ANO-GPT, une copie est ensuite placée à cet endroit
    sans contourner le backend de capture de Caelestia.
    """
    target_dir = _caelestia_screenshots_dir(env)
    # ANO-GPT peut être lancé hors de la session graphique ; propager la
    # valeur découverte fait écrire le CLI dans le même dossier que le shell.
    env["CAELESTIA_SCREENSHOTS_DIR"] = str(target_dir)
    before = {p.resolve() for p in target_dir.glob("*.png")} if target_dir.exists() else set()
    started = time.time()
    try:
        result = subprocess.run(
            ["caelestia", "screenshot"], capture_output=True, text=True,
            timeout=30, env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "path": "", "message": f"Échec de Caelestia : {exc}"}
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "commande interrompue").strip()
        return {"ok": False, "path": "", "message": f"Échec de Caelestia : {detail}"}

    # Le nom est horodaté à la seconde : utiliser aussi la date de modification
    # pour éviter de confondre une ancienne capture réalisée le même jour.
    candidates = [
        p for p in target_dir.glob("*.png")
        if p.resolve() not in before and p.is_file() and p.stat().st_size > 0
    ]
    if not candidates:
        candidates = [
            p for p in target_dir.glob("*.png")
            if p.is_file() and p.stat().st_size > 0 and p.stat().st_mtime >= started - 2
        ]
    if not candidates:
        return {
            "ok": False, "path": "",
            "message": "Caelestia n'a produit aucune image exploitable.",
        }
    source = max(candidates, key=lambda p: p.stat().st_mtime)
    path = Path(output).expanduser() if output else source
    try:
        if path != source:
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, path)
    except OSError as exc:
        return {"ok": False, "path": "", "message": f"Capture Caelestia créée, mais copie impossible : {exc}"}

    extras = ["copiée dans le presse-papiers par Caelestia"]
    if not copy_clipboard:
        extras.append("Caelestia impose néanmoins la copie dans le presse-papiers")
    if annotate and _have("swappy"):
        try:
            subprocess.Popen(["swappy", "-f", str(path)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             env=env, start_new_session=True)
            extras.append("ouverte dans swappy pour annotation")
        except OSError:
            pass
    return {
        "ok": True, "path": str(path),
        "message": f"Capture Caelestia enregistrée : {path} — {path.stat().st_size // 1024} Ko "
                   f"({', '.join(extras)}).",
    }


def _open_caelestia_region(freeze: bool, env: dict) -> Dict[str, Any]:
    """Ouvre le picker QS de Caelestia, jamais slurp directement.

    ``caelestia screenshot -r`` délègue à ``qs -c caelestia … picker`` ; le
    shell dessine donc la même zone de sélection que son propre panneau.
    """
    cmd = ["caelestia", "screenshot", "-r"]
    if freeze:
        cmd.append("-f")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "path": "", "message": f"Échec de Caelestia : {exc}"}
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "commande interrompue").strip()
        return {"ok": False, "path": "", "message": f"Échec de Caelestia : {detail}"}
    return {"ok": True, "path": "", "message": "Sélection de région ouverte dans Caelestia Shell."}


def take_screenshot(
    mode: str = "full",
    output: Optional[str] = None,
    monitor: Optional[str] = None,
    address: Optional[str] = None,
    window: Optional[str] = None,
    copy_clipboard: bool = True,
    annotate: bool = False,
    delay: float = 0.0,
    freeze: bool = False,
) -> Dict[str, Any]:
    """
    Prend une capture d'écran.
    mode    : 'full' | 'region' (sélection souris) | 'window' | 'monitor'
    window  : nom/classe de la fenêtre à capturer (si mode='window')
    address : adresse Hyprland exacte (prioritaire sur window)
    Renvoie {'ok': bool, 'path': str, 'message': str}.
    """
    env = _hypr_env()
    if delay > 0:
        time.sleep(min(delay, 60))

    # Le plein écran et la sélection de région doivent donner exactement la
    # même expérience que Caelestia : dossier configuré, presse-papiers et
    # notification du shell. Les cibles non gérées par Caelestia restent sur
    # le chemin grim existant, qui est nécessaire pour fenêtre/écran nommé.
    if _have("caelestia"):
        if mode == "full":
            return _take_screenshot_caelestia(output, copy_clipboard, annotate, env)
        if mode == "region":
            return _open_caelestia_region(freeze, env)

    if not _have("grim"):
        return {
            "ok": False, "path": "",
            "message": "grim n'est pas installé — impossible de capturer l'écran "
                       "sous Wayland. Installe-le avec : sudo pacman -S grim",
        }
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = Path(output).expanduser() if output else SHOT_DIR / f"capture_{_stamp()}.png"
    path.parent.mkdir(parents=True, exist_ok=True)

    cmd = ["grim"]
    label = "Capture plein écran"
    if mode == "region":
        if not _have("slurp"):
            return {"ok": False, "path": "",
                    "message": "slurp n'est pas installé — impossible de sélectionner "
                               "une région. Installe-le avec : sudo pacman -S slurp"}
        try:
            sel = subprocess.run(["slurp"], capture_output=True, text=True,
                                 timeout=120, env=env)
        except subprocess.TimeoutExpired:
            return {"ok": False, "path": "",
                    "message": "Sélection de région abandonnée (délai dépassé)."}
        geom = (sel.stdout or "").strip()
        if sel.returncode != 0 or not geom:
            return {"ok": False, "path": "", "message": "Sélection de région annulée."}
        cmd += ["-g", geom]
        label = "Capture de la région sélectionnée"
    elif mode == "window":
        target = None
        if address:
            target = _find_window(address)
        if target is None and window:
            target = _find_window(window)
        geom = _client_geometry(target) if target else _active_window_geometry()
        if not geom:
            hint = ""
            if address or window:
                hint = f" Fenêtre introuvable : '{address or window}'."
            return {"ok": False, "path": "",
                    "message": "Impossible de déterminer la fenêtre à capturer." + hint}
        cmd += ["-g", geom]
        label = "Capture de la fenêtre"
    elif mode == "monitor":
        target = monitor or _current_monitor()
        if not target:
            return {"ok": False, "path": "", "message": "Aucun écran identifié."}
        cmd += ["-o", target]
        label = f"Capture de l'écran {target}"

    cmd.append(str(path))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=130, env=env)
    except Exception as e:
        return {"ok": False, "path": "", "message": f"Échec de la capture : {e}"}
    if r.returncode != 0 or not path.exists() or path.stat().st_size == 0:
        err = (r.stderr or "").strip() or "grim n'a produit aucune image"
        return {"ok": False, "path": "", "message": f"Échec de la capture : {err}"}

    extras = []
    if copy_clipboard and _have("wl-copy"):
        try:
            with open(path, "rb") as img:
                subprocess.run(["wl-copy", "-t", "image/png"], stdin=img,
                               timeout=10, env=env)
            extras.append("copiée dans le presse-papiers")
        except Exception:
            pass
    if annotate and _have("swappy"):
        try:
            subprocess.Popen(["swappy", "-f", str(path)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             env=env, start_new_session=True)
            extras.append("ouverte dans swappy pour annotation")
        except Exception:
            pass

    size_kb = path.stat().st_size // 1024
    suffix = f" ({', '.join(extras)})" if extras else ""
    return {
        "ok": True, "path": str(path),
        "message": f"{label} enregistrée : {path} — {size_kb} Ko{suffix}.",
    }


# ════════════════════════════════════════════════════════════════════════════
# Enregistrement vidéo (gpu-screen-recorder, mode portal)
# ════════════════════════════════════════════════════════════════════════════

def _read_state() -> Optional[Dict[str, Any]]:
    if not _STATE_PATH.exists():
        return None
    try:
        st = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    pid = st.get("pid")
    if not pid:
        return None
    if not _recording_process_alive(pid):
        try:
            _STATE_PATH.unlink(missing_ok=True)
        except Exception:
            pass
        return None
    return st


def _write_state(pid: int, path: str, paused: bool = False) -> None:
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _STATE_PATH.write_text(
        json.dumps({"pid": pid, "path": path, "paused": paused,
                    "started_at": time.time()}),
        encoding="utf-8",
    )


def _update_state(**fields) -> None:
    st = _read_state()
    if not st:
        return
    st.update(fields)
    try:
        _STATE_PATH.write_text(json.dumps(st), encoding="utf-8")
    except Exception:
        pass


def _default_mic_source() -> Optional[str]:
    """Nom réel de la source de capture par défaut de PipeWire (le micro).
    gpu-screen-recorder n'accepte pas « default_input » : il faut le nom de
    la source (ex: alsa_input.pci-0000_00_1f.3.analog-stereo)."""
    env = _hypr_env()
    if _have("pactl"):
        try:
            r = subprocess.run(["pactl", "get-default-source"],
                               capture_output=True, text=True, timeout=3, env=env)
            src = (r.stdout or "").strip()
            if r.returncode == 0 and src:
                return src
        except Exception:
            pass
        try:
            r = subprocess.run(["pactl", "list", "short", "sources"],
                               capture_output=True, text=True, timeout=3, env=env)
            for line in (r.stdout or "").splitlines():
                parts = line.split("\t")
                if len(parts) >= 2 and ".monitor" not in parts[1]:
                    return parts[1]
        except Exception:
            pass
    return None


def _audio_args(audio: str) -> List[str]:
    """Sources à passer chacune dans un `-a` distinct.
    'default_output' est la valeur spéciale de gsr pour le son système ;
    plusieurs -a sont mixés ensemble. Le micro exige le vrai nom de source."""
    audio = (audio or "system").lower()
    if audio == "none":
        return []
    if audio == "mic":
        mic = _default_mic_source()
        return [mic] if mic else []
    if audio == "both":
        mic = _default_mic_source()
        return ["default_output"] + ([mic] if mic else [])
    return ["default_output"]


# gpu-screen-recorder n'accepte que ses propres valeurs de qualité :
# very_slow, slow, medium, fast, ultrafast (plus lent = meilleure qualité).
_QUALITY_MAP = {
    "medium": "medium",
    "high": "slow",
    "ultra": "very_slow",
    "low": "fast",
    "fast": "ultrafast",
}


def _legacy_start_recording(
    output: Optional[str] = None,
    fps: int = 30,
    audio: str = "system",
    quality: str = "medium",
) -> Dict[str, Any]:
    """
    Démarre un enregistrement d'écran.
    audio   : 'system' | 'mic' | 'both' | 'none'
    quality : 'medium' | 'high' | 'ultra'
    """
    running = _read_state()
    if running:
        return {"ok": False, "path": running.get("path", ""),
                "message": f"Un enregistrement est déjà en cours : {running.get('path')}. "
                           f"Dis-moi d'arrêter l'enregistrement d'abord."}
    if not _have("gpu-screen-recorder"):
        return {"ok": False, "path": "",
                "message": "gpu-screen-recorder n'est pas installé. "
                           "Installe-le avec : yay -S gpu-screen-recorder"}

    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    path = Path(output).expanduser() if output else VIDEO_DIR / f"ecran_{_stamp()}.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)

    # Sur cette configuration la capture DRM directe échoue : le portail
    # xdg-desktop-portal-hyprland est le seul chemin fiable.
    _PORTAL_TOKEN.parent.mkdir(parents=True, exist_ok=True)

    audio_sources = _audio_args(audio)
    audio_warn = ""
    if (audio or "system").lower() in ("mic", "both") and not audio_sources:
        audio_warn = " (micro introuvable : l'enregistrement continue sans cette source)"
    elif (audio or "system").lower() == "mic" and not audio_sources:
        audio_warn = " (micro introuvable : enregistrement sans audio)"

    q = _QUALITY_MAP.get((quality or "medium").lower(), "medium")
    cmd = [
        "gpu-screen-recorder",
        "-w", "portal",
        "-restore-portal-session", "yes",
        "-portal-session-token-filepath", str(_PORTAL_TOKEN),
        "-f", str(max(10, min(int(fps or 30), 120))),
        "-q", q,
        "-fallback-cpu-encoding", "yes",   # iGPU Intel : bascule CPU si VAAPI refuse
        "-cursor", "yes",
        "-o", str(path),
    ]
    for src in audio_sources:
        cmd += ["-a", src]

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            start_new_session=True, env=_hypr_env(),
        )
    except Exception as e:
        return {"ok": False, "path": "",
                "message": f"Impossible de démarrer l'enregistrement : {e}"}

    # Laisse le temps au portail de s'ouvrir et à l'encodeur de s'initialiser.
    time.sleep(2.5)
    if proc.poll() is not None:
        err = ""
        try:
            err = (proc.stderr.read() or b"").decode("utf-8", "replace").strip()
        except Exception:
            pass
        low = err.lower()
        hint = ""
        if "portal" in low or "denied" in low or "session" in low:
            hint = (" Le partage d'écran a été refusé, annulé, ou la session "
                    "portail mémorisée n'est plus valide.")
            # Token périmé : on le nettoie pour que la prochaine tentative
            # rouvre proprement le sélecteur de partage au lieu d'échouer
            # silencieusement à chaque fois.
            try:
                _PORTAL_TOKEN.unlink(missing_ok=True)
            except Exception:
                pass
            hint += " J'ai nettoyé la session : réessaie."
        return {"ok": False, "path": "",
                "message": f"L'enregistrement n'a pas démarré.{hint} {err[:300]}".strip()}

    _write_state(proc.pid, str(path), paused=False)
    audio_label = {"system": "son du système", "mic": "micro",
                   "both": "son du système + micro",
                   "none": "sans audio"}.get((audio or "system").lower(), audio)
    return {"ok": True, "path": str(path),
            "message": f"Enregistrement démarré ({fps} img/s, {audio_label}){audio_warn} "
                       f"→ {path}. Dis « arrête l'enregistrement » quand tu veux, "
                       f"ou « pause » / « reprends » pour suspendre."}


def _legacy_pause_recording() -> Dict[str, Any]:
    """Met l'enregistrement en pause (SIGUSR2, supporté par GSR)."""
    st = _read_state()
    if not st:
        return {"ok": False, "path": "", "message": "Aucun enregistrement en cours."}
    if st.get("paused"):
        return {"ok": True, "path": st.get("path", ""),
                "message": "L'enregistrement est déjà en pause."}
    try:
        os.kill(st["pid"], signal.SIGUSR2)
    except OSError:
        try:
            _STATE_PATH.unlink(missing_ok=True)
        except Exception:
            pass
        return {"ok": False, "path": st.get("path", ""),
                "message": "L'enregistreur s'était déjà arrêté."}
    _update_state(paused=True)
    return {"ok": True, "path": st.get("path", ""),
            "message": "Enregistrement en pause. Dis « reprends l'enregistrement » pour continuer."}


def _legacy_resume_recording() -> Dict[str, Any]:
    """Reprend un enregistrement en pause (SIGUSR2 est un toggle)."""
    st = _read_state()
    if not st:
        return {"ok": False, "path": "", "message": "Aucun enregistrement en cours."}
    if not st.get("paused"):
        return {"ok": True, "path": st.get("path", ""),
                "message": "L'enregistrement n'est pas en pause."}
    try:
        os.kill(st["pid"], signal.SIGUSR2)
    except OSError:
        try:
            _STATE_PATH.unlink(missing_ok=True)
        except Exception:
            pass
        return {"ok": False, "path": st.get("path", ""),
                "message": "L'enregistreur s'était déjà arrêté."}
    _update_state(paused=False)
    return {"ok": True, "path": st.get("path", ""),
            "message": "Enregistrement repris."}


def _legacy_stop_recording() -> Dict[str, Any]:
    """Arrête proprement l'enregistrement en cours et finalise le fichier."""
    st = _read_state()
    if not st:
        return {"ok": False, "path": "", "message": "Aucun enregistrement n'est en cours."}
    pid, path = st["pid"], Path(st["path"])
    try:
        # SIGINT : gpu-screen-recorder ferme le conteneur proprement.
        # Un SIGKILL laisserait un MP4 illisible.
        os.kill(pid, signal.SIGINT)
    except OSError:
        try:
            _STATE_PATH.unlink(missing_ok=True)
        except Exception:
            pass
        return {"ok": False, "path": str(path),
                "message": "L'enregistreur s'était déjà arrêté."}

    for _ in range(60):                    # jusqu'à 6 s pour finaliser
        time.sleep(0.1)
        try:
            os.kill(pid, 0)
        except OSError:
            break
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    try:
        _STATE_PATH.unlink(missing_ok=True)
    except Exception:
        pass

    if not path.exists() or path.stat().st_size == 0:
        return {"ok": False, "path": str(path),
                "message": "L'enregistrement s'est arrêté mais le fichier est vide."}
    size_mb = path.stat().st_size / (1024 * 1024)
    duration = time.time() - st.get("started_at", time.time())
    return {"ok": True, "path": str(path),
            "message": f"Enregistrement terminé : {path} — "
                       f"{size_mb:.1f} Mo, {int(duration // 60)} min {int(duration % 60)} s."}


def _legacy_recording_status() -> Dict[str, Any]:
    st = _read_state()
    if not st:
        return {"ok": True, "recording": False,
                "message": "Aucun enregistrement en cours."}
    elapsed = time.time() - st.get("started_at", time.time())
    paused = " (en pause)" if st.get("paused") else ""
    size_txt = ""
    try:
        p = Path(st.get("path", ""))
        if p.exists():
            size_txt = f" — {p.stat().st_size / (1024 * 1024):.1f} Mo écrits"
    except Exception:
        pass
    return {"ok": True, "recording": True, "path": st.get("path", ""),
            "message": f"Enregistrement en cours{paused} depuis "
                       f"{int(elapsed // 60)} min {int(elapsed % 60)} s"
                       f"{size_txt} → {st.get('path')}."}


# ── Enregistreur vocal persistant ──────────────────────────────────────────
#
# ``caelestia record`` ne possède *pas* d'option microphone : son CLI 1.1
# accepte seulement ``-s``.  Lui passer ``-m`` faisait donc quitter argparse
# immédiatement (code 2), tandis que le pont vocal annonçait un démarrage.
#
# On lance GSR directement, avec un PID et un fichier d'état propres à ANO-GPT.
# Le fichier est enregistré dans le dossier Caelestia afin que les deux modes
# restent cohérents, mais aucun appel long ne retient la boucle vocale.  Ainsi
# le micro ANO-GPT reste disponible pour « arrête l'enregistrement ».
def _caelestia_recordings_dir(env: dict) -> Path:
    configured = env.get("CAELESTIA_RECORDINGS_DIR")
    if configured:
        return Path(configured).expanduser()
    try:
        uid = os.getuid()
        for pid_dir in Path("/proc").glob("[0-9]*"):
            try:
                if pid_dir.stat().st_uid != uid:
                    continue
                for item in (pid_dir / "environ").read_bytes().split(b"\0"):
                    if item.startswith(b"CAELESTIA_RECORDINGS_DIR="):
                        return Path(item.split(b"=", 1)[1].decode()).expanduser()
            except (OSError, UnicodeDecodeError):
                continue
    except OSError:
        pass
    return Path.home() / "Videos" / "Recordings"


def _caelestia_recording_running(env: dict) -> bool:
    """Détecte un enregistreur externe lancé depuis le panneau Caelestia."""
    try:
        return subprocess.run(
            ["pidof", "gpu-screen-recorder"], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=3, env=env,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _recording_process_alive(pid: object) -> bool:
    """Contrairement à ``pidof``, vérifie le PID précis que nous avons lancé."""
    try:
        os.kill(int(pid), 0)
        # Un zombie répond encore à kill(pid, 0), mais n'enregistre plus.
        stat = Path(f"/proc/{int(pid)}/stat")
        return not stat.exists() or ") Z " not in stat.read_text(errors="ignore")
    except (OSError, ValueError, TypeError):
        return False


def _recording_error(state: Dict[str, Any]) -> str:
    log = Path(str(state.get("stderr_log") or ""))
    try:
        return log.read_text(encoding="utf-8", errors="replace").strip()[-500:]
    except OSError:
        return ""


def start_recording(
    output: Optional[str] = None,
    fps: int = 30,
    audio: str = "system",
    quality: str = "medium",
) -> Dict[str, Any]:
    """Démarre GSR détaché ; l'appel rend la main immédiatement à la voix."""
    env = _hypr_env()
    recordings_dir = _caelestia_recordings_dir(env)
    running = _read_state()
    if running:
        return {"ok": False, "path": str(running.get("path") or ""),
                "message": "Un enregistrement vocal est déjà en cours."}
    if _caelestia_recording_running(env):
        return {"ok": False, "path": str(recordings_dir),
                "message": "Un enregistrement Caelestia est déjà en cours ; arrête-le avant d'en démarrer un autre."}
    if not _have("gpu-screen-recorder"):
        return {"ok": False, "path": "", "message": "gpu-screen-recorder est indisponible. Installe-le avec : yay -S gpu-screen-recorder"}

    recordings_dir.mkdir(parents=True, exist_ok=True)
    path = Path(output).expanduser() if output else recordings_dir / f"recording_{_stamp()}.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = (audio or "system").lower()
    audio_sources = _audio_args(mode)
    if mode in {"mic", "both"} and not any(src != "default_output" for src in audio_sources):
        return {"ok": False, "path": "", "message": "Microphone PipeWire introuvable : l'enregistrement n'a pas été lancé."}
    target = _current_monitor() or "focused"
    cmd = ["gpu-screen-recorder", "-w", target,
           "-f", str(max(10, min(int(fps or 30), 120))),
           "-q", _QUALITY_MAP.get((quality or "medium").lower(), "medium"),
           "-cursor", "yes", "-fallback-cpu-encoding", "yes"]
    for source in audio_sources:
        cmd += ["-a", source]
    cmd += ["-o", str(path)]
    log_path = _STATE_PATH.parent / "recording-gsr.log"
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(log_path, "wb") as err_log:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=err_log,
                                    start_new_session=True, env=env)
    except OSError as exc:
        return {"ok": False, "path": "", "message": f"Impossible de démarrer l'enregistreur : {exc}"}
    time.sleep(0.35)  # Validation courte, hors callback micro (executor).
    if proc.poll() is not None:
        detail = _recording_error({"stderr_log": str(log_path)})
        return {"ok": False, "path": "", "message": f"L'enregistreur s'est arrêté au démarrage. {detail[:300]}"}
    _write_state(proc.pid, str(path), paused=False)
    _update_state(stderr_log=str(log_path), audio=mode)
    label = {"system": "son système", "mic": "micro", "both": "son système et micro", "none": "sans audio"}.get(mode, "son système")
    return {"ok": True, "path": str(path),
            "message": f"Enregistrement démarré ({label}) : {path}. Tu peux continuer à parler ; dis « arrête l'enregistrement » pour le sauvegarder."}


def pause_recording() -> Dict[str, Any]:
    return _legacy_pause_recording()


def resume_recording() -> Dict[str, Any]:
    return _legacy_resume_recording()


def stop_recording() -> Dict[str, Any]:
    return _legacy_stop_recording()


def recording_status() -> Dict[str, Any]:
    state = _read_state()
    if state:
        return _legacy_recording_status()
    env = _hypr_env()
    directory = _caelestia_recordings_dir(env)
    running = _caelestia_recording_running(env)
    return {"ok": True, "recording": running, "path": str(directory),
            "message": (f"Enregistrement Caelestia externe en cours ; sortie dans {directory}."
                        if running else "Aucun enregistrement en cours.")}


# ════════════════════════════════════════════════════════════════════════════
# Point d'entrée outil
# ════════════════════════════════════════════════════════════════════════════

_ACTION_ALIASES = {
    "screenshot": "screenshot", "capture": "screenshot", "capture_ecran": "screenshot",
    "region": "region", "selection": "region", "zone": "region",
    "window": "window", "fenetre": "window",
    "start_recording": "start_recording", "record": "start_recording",
    "enregistrer": "start_recording", "start_record": "start_recording",
    "stop_recording": "stop_recording", "stop_record": "stop_recording",
    "arreter": "stop_recording", "stop": "stop_recording",
    "pause_recording": "pause_recording", "pause": "pause_recording",
    "resume_recording": "resume_recording", "resume": "resume_recording",
    "reprendre": "resume_recording", "reprends": "resume_recording",
    "status": "status", "etat": "status",
    "monitors": "monitors", "ecrans": "monitors",
}


@kit.action("capture_control")
def capture_control(parameters: dict = None, response=None, player=None,
                    session_memory=None) -> str:
    """
    Outil unique pour capture d'écran et enregistrement vidéo.
    Paramètres utiles :
      action        : screenshot | region | window | start_recording |
                      stop_recording | pause_recording | resume_recording |
                      status | monitors
      path          : chemin de sortie (capture ou vidéo)
      monitor       : nom d'écran pour une capture monitor
      window        : nom/classe de fenêtre pour une capture window
      address       : adresse Hyprland exacte pour une capture window
      fps/audio/quality : réglages d'enregistrement
      copy_clipboard / annotate / delay / freeze : options de capture
    """
    p = parameters or {}
    action = _ACTION_ALIASES.get(str(p.get("action", "screenshot")).strip().lower(),
                                 "screenshot")
    if player:
        try:
            player.write_log(f"[capture] {action}")
        except Exception:
            pass

    if action == "monitors":
        return list_monitors()
    if action == "status":
        return recording_status()["message"]
    if action == "start_recording":
        return start_recording(
            output=p.get("path"),
            fps=int(p.get("fps", 30) or 30),
            audio=str(p.get("audio", "system") or "system").lower(),
            quality=str(p.get("quality", "medium") or "medium").lower(),
        )["message"]
    if action == "stop_recording":
        return stop_recording()["message"]
    if action == "pause_recording":
        return pause_recording()["message"]
    if action == "resume_recording":
        return resume_recording()["message"]

    mode = {"screenshot": "full", "region": "region", "window": "window"}[action]
    if p.get("monitor"):
        mode = "monitor"
    return take_screenshot(
        mode=mode,
        output=p.get("path"),
        monitor=p.get("monitor"),
        address=p.get("address"),
        window=p.get("window"),
        copy_clipboard=bool(p.get("copy_clipboard", True)),
        annotate=bool(p.get("annotate", False)),
        delay=float(p.get("delay", 0) or 0),
        freeze=bool(p.get("freeze", False)),
    )["message"]


# ════════════════════════════════════════════════════════════════════════════
# Test direct
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    act = sys.argv[1] if len(sys.argv) > 1 else "screenshot"
    extra = {}
    if len(sys.argv) > 2:
        extra["window"] = " ".join(sys.argv[2:])
    print(capture_control({"action": act, **extra}))
