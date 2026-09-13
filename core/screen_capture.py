"""core/screen_capture.py — Moteur de capture d'écran et perception de fenêtre active sous Wayland/Hyprland.

Conçu pour la perception instantanée (sub-15ms) sans aucune interaction manuelle
(pas de slurp bloquant lors d'une question vocale).
Gère la géométrie exacte des fenêtres sous Hyprland, les facteurs d'échelle HiDPI,
le basculement intelligent si la fenêtre active est ANO-GPT elle-même, et les
replis robustes (moniteur actif, plein écran, X11).
"""

from __future__ import annotations

import io
import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core import action_kit as kit

try:
    import PIL.Image
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False


@dataclass
class WindowInfo:
    """Informations complètes sur une fenêtre sous Hyprland / Wayland."""
    address: str = ""
    window_class: str = ""
    title: str = ""
    at: Tuple[int, int] = (0, 0)
    size: Tuple[int, int] = (0, 0)
    workspace_id: int = 1
    workspace_name: str = ""
    monitor: str = ""
    is_fullscreen: bool = False
    is_floating: bool = False

    @property
    def geometry_str(self) -> str:
        """Format géométrie pour grim : 'X,Y WxH'."""
        if self.size[0] > 0 and self.size[1] > 0:
            return f"{self.at[0]},{self.at[1]} {self.size[0]}x{self.size[1]}"
        return ""

    @property
    def is_terminal(self) -> bool:
        """Vrai si l'application est un émulateur de terminal."""
        c = (self.window_class or "").casefold()
        terminal_classes = {
            "kitty", "alacritty", "foot", "wezterm", "ghostty",
            "xterm", "gnome-terminal", "konsole", "terminator",
            "urxvt", "st", "tilix", "xfce4-terminal"
        }
        return c in terminal_classes or any(term in c for term in ("term", "kitty", "foot", "console"))

    @property
    def is_ide(self) -> bool:
        """Vrai si l'application est un IDE ou éditeur de code."""
        c = (self.window_class or "").casefold()
        ide_classes = {
            "code", "vscode", "vscodium", "cursor", "sublime_text",
            "subl", "neovim", "nvim", "emacs", "kate", "zed",
            "clion", "pycharm", "intellij", "webstorm", "rustrover"
        }
        return c in ide_classes or any(ide in c for ide in ("code", "studio", "dev", "editor"))

    @property
    def is_browser(self) -> bool:
        """Vrai si l'application est un navigateur web."""
        c = (self.window_class or "").casefold()
        return any(b in c for b in ("firefox", "chrome", "chromium", "brave", "edge", "zen", "opera"))

    @property
    def is_doc_or_pdf(self) -> bool:
        """Vrai si l'application est un lecteur de document ou PDF."""
        c = (self.window_class or "").casefold()
        t = (self.title or "").casefold()
        return any(d in c for d in ("zathura", "evince", "okular", "mupdf", "pdf")) or ".pdf" in t


# Classes associées à l'interface d'ANO-GPT / Jarvis (à ignorer pour capturer la fenêtre sous-jacente)
_ANOGPT_CLASSES = {
    "jarvis-dashboard", "ano-gpt", "anogpt", "anogpt-hud", "python3", "org.kde.kdialog"
}


def _hypr_env() -> dict:
    """Restaure l'environnement Wayland/Hyprland complet même en service."""
    env = {**os.environ}
    if not env.get("WAYLAND_DISPLAY") and not env.get("DISPLAY"):
        try:
            uid = os.getuid()
            for pid_dir in Path("/proc").glob("[0-9]*"):
                try:
                    if pid_dir.stat().st_uid != uid:
                        continue
                    env_file = pid_dir / "environ"
                    if not env_file.exists():
                        continue
                    content = env_file.read_text(errors="ignore")
                    for line in content.split("\x00"):
                        if "=" in line:
                            k, v = line.split("=", 1)
                            if k in ("WAYLAND_DISPLAY", "DISPLAY", "XDG_RUNTIME_DIR", "HYPRLAND_INSTANCE_SIGNATURE"):
                                if not env.get(k):
                                    env[k] = v
                    if env.get("WAYLAND_DISPLAY") or env.get("DISPLAY"):
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


def _hyprctl_json(cmd: str) -> Any:
    """Exécute hyprctl -j avec gestion de timeout et d'environnement."""
    if not shutil.which("hyprctl"):
        return None
    try:
        out = kit.run(
            ["hyprctl", "-j"] + cmd.split(),
            timeout=1.5,
            env=_hypr_env(),
        )
        if out.returncode != 0 or not out.stdout.strip():
            return None
        return json.loads(out.stdout)
    except Exception:
        return None


def get_all_clients() -> List[WindowInfo]:
    """Récupère la liste de toutes les fenêtres ouvertes dans Hyprland."""
    raw_clients = _hyprctl_json("clients")
    if not isinstance(raw_clients, list):
        return []

    windows = []
    for c in raw_clients:
        if not isinstance(c, dict):
            continue
        at = c.get("at") or [0, 0]
        size = c.get("size") or [0, 0]
        ws = c.get("workspace") or {}
        ws_id = ws.get("id", 1) if isinstance(ws, dict) else 1
        ws_name = ws.get("name", "") if isinstance(ws, dict) else ""

        w = WindowInfo(
            address=str(c.get("address") or ""),
            window_class=str(c.get("class") or c.get("initialClass") or ""),
            title=str(c.get("title") or c.get("initialTitle") or ""),
            at=(int(at[0]), int(at[1])) if len(at) >= 2 else (0, 0),
            size=(int(size[0]), int(size[1])) if len(size) >= 2 else (0, 0),
            workspace_id=ws_id,
            workspace_name=ws_name,
            monitor=str(c.get("monitor") or ""),
            is_fullscreen=bool(c.get("fullscreen", False)),
            is_floating=bool(c.get("floating", False)),
        )
        windows.append(w)
    return windows


def get_active_window(skip_anogpt: bool = True) -> Optional[WindowInfo]:
    """
    Récupère la fenêtre active Hyprland.
    Si skip_anogpt est True et que la fenêtre active est ANO-GPT/Jarvis,
    recherche automatiquement la dernière fenêtre utilisateur active ou visible.
    """
    raw_active = _hyprctl_json("activewindow")
    clients = get_all_clients()

    current: Optional[WindowInfo] = None
    if isinstance(raw_active, dict) and raw_active.get("address"):
        addr = str(raw_active.get("address"))
        for c in clients:
            if c.address == addr:
                current = c
                break
        if not current and raw_active.get("at") and raw_active.get("size"):
            at = raw_active.get("at", [0, 0])
            size = raw_active.get("size", [0, 0])
            current = WindowInfo(
                address=addr,
                window_class=str(raw_active.get("class") or ""),
                title=str(raw_active.get("title") or ""),
                at=(int(at[0]), int(at[1])),
                size=(int(size[0]), int(size[1])),
            )

    if current is not None:
        c_class = current.window_class.casefold()
        if not skip_anogpt or (c_class not in _ANOGPT_CLASSES and "ano-gpt" not in c_class):
            return current

    # Si la fenêtre active est ANO-GPT ou inexistante, trouver la fenêtre utilisateur la plus pertinente
    # Priorité : terminal, IDE, navigateur ou client le plus récemment focalisé
    for c in clients:
        c_class = c.window_class.casefold()
        if c_class in _ANOGPT_CLASSES or "ano-gpt" in c_class:
            continue
        if c.size[0] > 100 and c.size[1] > 100:
            return c

    return current


def find_window_by_query(query: str) -> Optional[WindowInfo]:
    """Recherche une fenêtre par adresse exacte ou par correspondance dans le titre/classe."""
    q = (query or "").strip().lower()
    if not q:
        return None

    clients = get_all_clients()
    for c in clients:
        if c.address.lower() == q:
            return c

    for c in clients:
        blob = f"{c.window_class} {c.title}".lower()
        if q in blob:
            return c
    return None


def get_focused_monitor_name() -> Optional[str]:
    """Nom du moniteur actuellement focalisé."""
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


def get_monitor_geometry(name: Optional[str] = None) -> Optional[Tuple[int, int, int, int]]:
    """Géométrie logique Hyprland d'un moniteur (x, y, largeur, hauteur).

    Ces coordonnées sont celles du bureau virtuel utilisées par les overlays
    Qt. Elles permettent de replacer une boîte normalisée provenant d'une
    capture ``grim -o`` sans la projeter par erreur sur l'écran principal.
    """
    monitors = _hyprctl_json("monitors")
    if not isinstance(monitors, list):
        return None
    wanted = name or get_focused_monitor_name()
    selected = next(
        (m for m in monitors if isinstance(m, dict) and m.get("name") == wanted),
        None,
    )
    if selected is None:
        selected = next((m for m in monitors if isinstance(m, dict) and m.get("focused")), None)
    if not isinstance(selected, dict):
        return None
    try:
        width, height = int(selected["width"]), int(selected["height"])
        if width <= 0 or height <= 0:
            return None
        return (int(selected.get("x") or 0), int(selected.get("y") or 0), width, height)
    except (KeyError, TypeError, ValueError):
        return None


def compress_image_bytes(
    img_bytes: bytes,
    max_dim: Tuple[int, int] = (1920, 1080),
    quality: int = 85,
    output_format: str = "JPEG",
) -> Tuple[bytes, str]:
    """Compresse une image brute en mémoire (PNG/JPEG)."""
    if not _PIL_AVAILABLE:
        return img_bytes, "image/png"
    try:
        with PIL.Image.open(io.BytesIO(img_bytes)) as img:
            img = img.convert("RGB")
            img.thumbnail(max_dim, PIL.Image.Resampling.LANCZOS if hasattr(PIL.Image, "Resampling") else PIL.Image.BILINEAR)
            buf = io.BytesIO()
            img.save(buf, format=output_format, quality=quality, optimize=True)
            return buf.getvalue(), f"image/{output_format.lower()}"
    except Exception:
        return img_bytes, "image/png"


def capture_raw_geometry(geometry: Optional[str] = None, monitor: Optional[str] = None) -> bytes:
    """
    Capture instantanée via grim vers bytes mémoire.
    geometry : 'X,Y WxH'
    monitor  : 'DP-1', 'eDP-1', etc.
    """
    env = _hypr_env()
    if not shutil.which("grim"):
        raise RuntimeError("grim n'est pas installé. Installez-le avec : sudo pacman -S grim")

    cmd = ["grim"]
    if geometry:
        cmd += ["-g", geometry]
    elif monitor:
        cmd += ["-o", monitor]

    # '-' envoie directement l'image PNG sur stdout
    cmd.append("-")

    proc = kit.run(
        cmd,
        timeout=10,
        env=env,
        binary=True,
    )
    if proc.returncode != 0 or not proc.stdout:
        err = (proc.stderr or b"").decode("utf-8", "ignore").strip() or "Aucune image générée"
        raise RuntimeError(f"Échec capture grim : {err}")
    return proc.stdout


def capture_window_or_screen(
    target: str = "active_window",
    window_query: Optional[str] = None,
    compress: bool = True,
    max_dim: Tuple[int, int] = (1920, 1080),
    quality: int = 85,
) -> Tuple[bytes, str, Dict[str, Any]]:
    """
    Point d'entrée principal pour la perception visuelle.
    target : 'active_window' | 'screen' | 'monitor' | 'named_window'
    Renvoie (image_bytes, mime_type, metadata_dict).
    """
    metadata: Dict[str, Any] = {
        "target": target,
        "timestamp": time.time(),
        "geometry": None,
        "window_class": "",
        "window_title": "",
        "is_terminal": False,
        "is_ide": False,
        "capture_origin": (0, 0),
        "capture_size": None,
    }

    geom: Optional[str] = None
    win_info: Optional[WindowInfo] = None

    if target in ("active_window", "named_window"):
        if target == "named_window" and window_query:
            win_info = find_window_by_query(window_query)
        if win_info is None:
            win_info = get_active_window(skip_anogpt=True)

        if win_info is not None:
            geom = win_info.geometry_str
            metadata["geometry"] = geom
            metadata["window_class"] = win_info.window_class
            metadata["window_title"] = win_info.title
            metadata["is_terminal"] = win_info.is_terminal
            metadata["is_ide"] = win_info.is_ide
            metadata["is_browser"] = win_info.is_browser
            metadata["is_doc_or_pdf"] = win_info.is_doc_or_pdf
            metadata["window_address"] = win_info.address

    try:
        if geom:
            raw_bytes = capture_raw_geometry(geometry=geom)
            if win_info is not None:
                metadata["capture_origin"] = win_info.at
                metadata["capture_size"] = win_info.size
        else:
            mon = get_focused_monitor_name() if target == "monitor" else None
            raw_bytes = capture_raw_geometry(monitor=mon)
            if mon:
                metadata["monitor"] = mon
                monitor_geometry = get_monitor_geometry(mon)
                if monitor_geometry is not None:
                    mx, my, mw, mh = monitor_geometry
                    metadata["capture_origin"] = (mx, my)
                    metadata["capture_size"] = (mw, mh)
    except Exception as exc:
        # Repli de secours : capture de tout l'écran
        raw_bytes = capture_raw_geometry()
        metadata["fallback"] = str(exc)

    if compress:
        img_bytes, mime = compress_image_bytes(raw_bytes, max_dim=max_dim, quality=quality)
    else:
        img_bytes, mime = raw_bytes, "image/png"

    metadata["byte_size"] = len(img_bytes)
    metadata["mime_type"] = mime
    return img_bytes, mime, metadata
