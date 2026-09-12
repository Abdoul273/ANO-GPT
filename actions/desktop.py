#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
desktop.py — Bureau ultra-robuste : fonds d'écran, organisation, nettoyage.
Optimisé Arch Linux / Hyprland (Wayland), compatible GNOME/KDE/XFCE/macOS/Windows.

Corrections par rapport à l'ancienne version :
    - sous Hyprland, le fond d'écran tombait sur feh (outil X11) qui ne
      fonctionne pas sous Wayland : chaîne complète ajoutée
      swww → hyprctl hyprpaper → swaybg → feh, avec démarrage automatique
      de swww-daemon et gestion multi-écrans ;
    - `Path(file)` → `Path(__file__)` ;
    - get_current_wallpaper savait lire GNOME uniquement : ajouté swww query ;
    - parsing local réécrit (doublons de motifs, pattern wallpaper fragile)
      + compréhension de « fond d'écran aléatoire » ;
    - le nettoyage des blocs markdown du code IA testait "`" au lieu de
      "```" : le code généré échouait systématiquement ;
    - le dossier Bureau est résolu via XDG_DESKTOP_DIR, xdg-user-dir,
      « Bureau » français puis Desktop ;
    - organize/clean prennent un snapshot du dossier avant de le modifier.

Ajouts :
    - action random_wallpaper (fonds depuis ~/.config/jarvis/wallpapers,
      ~/Pictures, ~/Images) ;
    - téléchargement d'image avec User-Agent et timeout ;
    - environnement Wayland/Hyprland restauré pour tous les appels.
"""
import json
import importlib
import importlib.util
import os
import platform
import random
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List

from core import action_kit as kit
from core.live_model_policy import FAST_MODEL

class _LazyPyAutoGUI:
    _module = None

    def __getattr__(self, name):
        if self._module is None:
            self._module = importlib.import_module("pyautogui")
        return getattr(self._module, name)


pyautogui = _LazyPyAutoGUI()
_PYAUTOGUI = importlib.util.find_spec("pyautogui") is not None

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"
_WAYLAND = bool(os.environ.get("WAYLAND_DISPLAY"))
DEVNULL = subprocess.DEVNULL


# ════════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════════

def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def _get_api_key() -> str:
    try:
        path = _get_base_dir() / "config" / "api_keys.json"
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f).get("gemini_api_key", "")
    except Exception:
        return ""


def _hypr_env() -> dict:
    """Environnement complet pour swww/hyprctl/swaybg : sans
    HYPRLAND_INSTANCE_SIGNATURE hyprctl répond « no running instance »."""
    env = {**os.environ}
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


def _get_desktop() -> Path:
    """Dossier Bureau : XDG_DESKTOP_DIR → xdg-user-dir → Bureau/Desktop."""
    if _OS == "Linux":
        xdg = os.environ.get("XDG_DESKTOP_DIR", "")
        if xdg and Path(xdg).expanduser().is_dir():
            return Path(xdg).expanduser()
        if shutil.which("xdg-user-dir"):
            try:
                r = subprocess.run(["xdg-user-dir", "DESKTOP"],
                                   capture_output=True, text=True, timeout=2)
                p = Path(r.stdout.strip()).expanduser()
                if r.returncode == 0 and p.is_dir():
                    return p
            except Exception:
                pass
        for cand in (Path.home() / "Desktop", Path.home() / "Bureau"):
            if cand.is_dir():
                return cand
        return Path.home() / "Desktop"
    if _OS == "Darwin":
        return Path.home() / "Desktop"
    return Path.home() / "Desktop"


def _get_user_downloads() -> Path:
    if _OS == "Linux":
        xdg = os.environ.get("XDG_DOWNLOAD_DIR", "")
        if xdg and Path(xdg).expanduser().is_dir():
            return Path(xdg).expanduser()
        if shutil.which("xdg-user-dir"):
            try:
                r = subprocess.run(["xdg-user-dir", "DOWNLOAD"],
                                   capture_output=True, text=True, timeout=2)
                p = Path(r.stdout.strip()).expanduser()
                if r.returncode == 0 and p.is_dir():
                    return p
            except Exception:
                pass
    return Path.home() / "Downloads"


# ════════════════════════════════════════════════════════════════════════════
# Fond d'écran — chaîne multi-backend (Wayland/Hyprland d'abord)
# ════════════════════════════════════════════════════════════════════════════

def _swww_set(path: Path, env: dict) -> bool:
    """swww img ; si le daemon n'est pas lancé, on le démarre et on retente."""
    try:
        r = subprocess.run(["swww", "img", str(path)], capture_output=True,
                           text=True, timeout=8, env=env)
        if r.returncode == 0:
            return True
        subprocess.Popen(["swww-daemon"], env=env, stdout=DEVNULL,
                         stderr=DEVNULL, start_new_session=True)
        import time
        time.sleep(1.2)
        r = subprocess.run(["swww", "img", str(path)], capture_output=True,
                           text=True, timeout=8, env=env)
        return r.returncode == 0
    except Exception:
        return False


def _hyprpaper_set(path: Path, env: dict) -> bool:
    """hyprctl hyprpaper : preload puis wallpaper sur tous les moniteurs."""
    if not shutil.which("hyprctl"):
        return False
    try:
        mons = json.loads(subprocess.check_output(
            ["hyprctl", "-j", "monitors"], text=True, timeout=3, env=env))
        names = [m.get("name") for m in mons
                 if isinstance(m, dict) and m.get("name")]
    except Exception:
        return False
    if not names:
        return False
    try:
        pre = subprocess.run(["hyprctl", "hyprpaper", "preload", str(path)],
                             capture_output=True, timeout=8, env=env)
        if pre.returncode != 0:
            return False
        ok = True
        for name in names:
            r = subprocess.run(["hyprctl", "hyprpaper", "wallpaper",
                                f"{name},{path}"],
                               capture_output=True, timeout=8, env=env)
            if r.returncode != 0:
                ok = False
        return ok
    except Exception:
        return False


def _swaybg_set(path: Path, env: dict) -> bool:
    if not shutil.which("swaybg"):
        return False
    try:
        subprocess.run(["pkill", "-x", "swaybg"], timeout=2)
    except Exception:
        pass
    try:
        subprocess.Popen(["swaybg", "-i", str(path), "-m", "fill"],
                         env=env, stdout=DEVNULL, stderr=DEVNULL,
                         start_new_session=True)
        return True
    except Exception:
        return False


def set_wallpaper(image_path: str) -> str:
    path = Path(image_path).expanduser().resolve()
    if not path.exists():
        return f"Image introuvable : {image_path}"
    ext = path.suffix.lower()
    if ext not in {".jpg", ".jpeg", ".png", ".bmp", ".webp"}:
        return f"Format non supporté ({ext}). Utilisez jpg, png, bmp ou webp."
    try:
        if _OS == "Windows":
            import ctypes
            if ext in {".webp", ".png"}:
                try:
                    from PIL import Image
                    bmp_path = Path(tempfile.mktemp(suffix=".bmp"))
                    Image.open(path).convert("RGB").save(bmp_path, "BMP")
                    path = bmp_path
                except ImportError:
                    pass
            ctypes.windll.user32.SystemParametersInfoW(20, 0, str(path), 3)
            return f"Fond d'écran changé : {path.name}"
        elif _OS == "Darwin":
            script = (
                f'tell application "System Events" to tell every desktop to '
                f'set picture to POSIX file "{path}"'
            )
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=15)
            return f"Fond d'écran changé : {path.name}"
        else:  # Linux
            desktop_env = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
            uri = f"file://{path}"
            if "gnome" in desktop_env or "unity" in desktop_env:
                subprocess.run(["gsettings", "set", "org.gnome.desktop.background",
                                "picture-uri", uri], capture_output=True, timeout=15)
                subprocess.run(["gsettings", "set", "org.gnome.desktop.background",
                                "picture-uri-dark", uri], capture_output=True, timeout=15)
                return f"Fond d'écran changé : {path.name}"
            elif "kde" in desktop_env:
                script = f"""
var allDesktops = desktops();
for (var i = 0; i < allDesktops.length; i++) {{
    d = allDesktops[i];
    d.wallpaperPlugin = "org.kde.image";
    d.currentConfigGroup = ["Wallpaper", "org.kde.image", "General"];
    d.writeConfig("Image", "file://{path}");
}}
"""
                subprocess.run(["qdbus", "org.kde.plasmashell", "/PlasmaShell",
                                "org.kde.PlasmaShell.evaluateScript", script],
                               capture_output=True, timeout=15)
                return f"Fond d'écran changé : {path.name}"
            elif "xfce" in desktop_env:
                subprocess.run(["xfconf-query", "-c", "xfce4-desktop",
                                "-p", "/backdrop/screen0/monitor0/workspace0/last-image",
                                "-s", str(path)], capture_output=True, timeout=15)
                return f"Fond d'écran changé : {path.name}"
            # ── Wayland/Hyprland : chaîne moderne ─────────────────────────
            env = _hypr_env()
            if _WAYLAND and shutil.which("swww") and _swww_set(path, env):
                return f"Fond d'écran changé (swww) : {path.name}"
            if _hyprpaper_set(path, env):
                return f"Fond d'écran changé (hyprpaper) : {path.name}"
            if _WAYLAND and _swaybg_set(path, env):
                return f"Fond d'écran changé (swaybg) : {path.name}"
            # ── Dernier recours : feh (X11/Xwayland) ─────────────────────
            r = subprocess.run(["feh", "--bg-scale", str(path)],
                               capture_output=True, env=env, timeout=15)
            if r.returncode == 0:
                return f"Fond d'écran changé (feh) : {path.name}"
            return ("Impossible de changer le fond d'écran automatiquement. "
                    "Installez swww, swaybg ou feh : sudo pacman -S swww")
    except Exception as e:
        return f"Erreur lors du changement du fond d'écran : {e}"


def set_wallpaper_from_url(url: str) -> str:
    try:
        import urllib.request
        suffix = Path(url.split("?")[0]).suffix or ".jpg"
        tmp = Path(tempfile.mktemp(suffix=suffix))
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp, open(tmp, "wb") as f:
            f.write(resp.read())
        result = set_wallpaper(str(tmp))
        try:
            tmp.unlink()
        except Exception:
            pass
        return result
    except Exception as e:
        return f"Impossible de télécharger l'image : {e}"


def set_random_wallpaper(directory: Optional[str] = None) -> str:
    """Choisit une image au hasard dans le dossier indiqué (ou les dossiers
    de fonds habituels) et l'applique."""
    dirs: List[Path] = []
    if directory:
        dirs.append(Path(directory).expanduser())
    dirs += [
        Path.home() / ".config" / "jarvis" / "wallpapers",
        Path.home() / "Pictures",
        Path.home() / "Images" / "Fonds d'écran",
        Path.home() / "Images",
    ]
    exts = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
    imgs: List[Path] = []
    for d in dirs:
        if not d.is_dir():
            continue
        try:
            imgs = [p for p in d.iterdir()
                    if p.is_file() and p.suffix.lower() in exts]
        except Exception:
            continue
        if imgs:
            break
    if not imgs:
        return ("Aucune image de fond d'écran trouvée. Ajoutez-en dans "
                "~/.config/jarvis/wallpapers ou indiquez un dossier.")
    pick = random.choice(imgs)
    return set_wallpaper(str(pick))


def get_current_wallpaper() -> str:
    try:
        if _OS == "Windows":
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop")
            val, _ = winreg.QueryValueEx(key, "Wallpaper")
            winreg.CloseKey(key)
            return f"Fond d'écran actuel : {val}"
        elif _OS == "Darwin":
            script = 'tell application "System Events" to get picture of desktop 1'
            result = subprocess.run(["osascript", "-e", script],
                                    capture_output=True, text=True, timeout=15)
            return f"Fond d'écran actuel : {result.stdout.strip()}"
        else:
            desktop_env = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
            if "gnome" in desktop_env or "unity" in desktop_env:
                result = subprocess.run(
                    ["gsettings", "get", "org.gnome.desktop.background", "picture-uri"],
                    capture_output=True, text=True, timeout=15)
                return f"Fond d'écran actuel : {result.stdout.strip()}"
            if shutil.which("swww"):
                try:
                    r = subprocess.run(["swww", "query"], capture_output=True,
                                       text=True, timeout=3, env=_hypr_env())
                    m = re.search(r"image:\s*(\S+)", r.stdout or "")
                    if m:
                        return f"Fond d'écran actuel : {m.group(1).rstrip(',')}"
                except Exception:
                    pass
            return ("Impossible de récupérer le fond d'écran sur cet environnement "
                    "(ni gsettings ni swww).")
    except Exception as e:
        return f"Erreur : {e}"


# ════════════════════════════════════════════════════════════════════════════
# Organisation / nettoyage du bureau
# ════════════════════════════════════════════════════════════════════════════

FILE_TYPE_MAP = {
    "Images":      {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg", ".ico", ".heic"},
    "Documents":   {".pdf", ".doc", ".docx", ".txt", ".xls", ".xlsx", ".ppt", ".pptx", ".csv", ".odt", ".ods", ".odp"},
    "Vidéos":      {".mp4", ".avi", ".mkv", ".mov", ".wmv", ".flv", ".webm", ".m4v"},
    "Musique":     {".mp3", ".wav", ".flac", ".aac", ".ogg", ".wma", ".m4a"},
    "Archives":    {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"},
    "Code":        {".py", ".js", ".ts", ".html", ".css", ".json", ".xml", ".cpp", ".java", ".cs", ".go", ".rs", ".sh", ".php"},
    "Exécutables": {".exe", ".msi", ".bat", ".cmd", ".sh", ".appimage", ".deb", ".rpm"},
}

_SKIP_EXTENSIONS = {
    "Windows": {".lnk", ".url"},
    "Darwin":  {".webloc"},
    "Linux":   {".desktop"},
}


def _organization_plan(mode: str = "by_type") -> tuple[Path, list[tuple[Path, Path]], list[str]]:
    """Prépare les déplacements sans modifier le Bureau."""
    desktop = _get_desktop()
    if not desktop.is_dir():
        return desktop, [], ["Le dossier Bureau est introuvable."]
    skip_exts = _SKIP_EXTENSIONS.get(_OS, set())
    moves, skipped = [], []
    for item in list(desktop.iterdir()):
        if item.is_dir() or item.name.startswith("."):
            continue
        if item.suffix.lower() in skip_exts:
            continue
        if mode == "by_date":
            mtime = datetime.fromtimestamp(item.stat().st_mtime)
            folder_name = mtime.strftime("%Y-%m")
        else:  # by_type
            ext = item.suffix.lower()
            folder_name = "Autres"
            for folder, exts in FILE_TYPE_MAP.items():
                if ext in exts:
                    folder_name = folder
                    break
        target_dir = desktop / folder_name
        new_path = target_dir / item.name
        # Il ne faut jamais écraser un fichier déjà rangé (ni déplacer un
        # fichier sur un nom présent dans le plan courant).
        if new_path.exists() or any(dest == new_path for _, dest in moves):
            skipped.append(item.name)
            continue
        moves.append((item, new_path))
    return desktop, moves, skipped


def organize_desktop(mode: str = "by_type", dry_run: bool = False) -> str:
    if mode not in {"by_type", "by_date"}:
        return "Mode d'organisation invalide : utilisez by_type ou by_date."
    desktop, moves, skipped = _organization_plan(mode)
    if skipped == ["Le dossier Bureau est introuvable."]:
        return f"{skipped[0]} ({desktop})"
    moved = []
    if not dry_run:
        for source, destination in moves:
            try:
                destination.parent.mkdir(exist_ok=True)
                shutil.move(str(source), str(destination))
                moved.append(f"{source.name} → {destination.parent.name}/")
            except OSError:
                skipped.append(source.name)
    else:
        moved = [f"{source.name} → {destination.parent.name}/" for source, destination in moves]
    result = (f"Bureau organisé ({'par date' if mode == 'by_date' else 'par type'}) : "
              f"{len(moved)} fichier(s) {'à déplacer' if dry_run else 'déplacé(s)'}." )
    if moved:
        result += "\n" + "\n".join(moved[:8])
        if len(moved) > 8:
            result += f"\n... et {len(moved) - 8} de plus."
    if skipped:
        result += f"\n{len(skipped)} fichier(s) ignoré(s) (conflit de nom)."
    return result


def clean_desktop(dry_run: bool = False) -> str:
    desktop = _get_desktop()
    if not desktop.is_dir():
        return f"Le dossier Bureau est introuvable : {desktop}"
    skip_exts = _SKIP_EXTENSIONS.get(_OS, set())
    today = datetime.now().strftime("%Y-%m-%d")
    archive_dir = desktop / f"Archive Bureau {today}"
    planned = []
    for item in list(desktop.iterdir()):
        if item.is_dir() or item.name.startswith("."):
            continue
        if item.suffix.lower() in skip_exts:
            continue
        new_path = archive_dir / item.name
        if not new_path.exists():
            planned.append((item, new_path))
    if dry_run:
        return (f"Aperçu du nettoyage : {len(planned)} fichier(s) seraient archivés dans "
                f"'{archive_dir.name}'.")
    moved = 0
    archive_dir.mkdir(exist_ok=True)
    for item, new_path in planned:
        try:
            shutil.move(str(item), str(new_path))
            moved += 1
        except OSError:
            continue
    return f"Bureau nettoyé : {moved} fichier(s) archivé(s) dans '{archive_dir.name}'."


def restore_desktop_archive(archive: Optional[str] = None, dry_run: bool = False) -> str:
    """Restaure la dernière archive du Bureau, sans écraser les nouveaux fichiers."""
    desktop = _get_desktop()
    if not desktop.is_dir():
        return f"Le dossier Bureau est introuvable : {desktop}"
    if archive:
        candidate = (desktop / archive).resolve()
        if candidate.parent != desktop.resolve():
            return "Archive invalide : elle doit se trouver directement sur le Bureau."
    else:
        archives = sorted(
            (p for p in desktop.glob("Archive Bureau *") if p.is_dir()),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
        candidate = archives[0] if archives else None
    if candidate is None or not candidate.is_dir():
        return "Aucune archive de Bureau à restaurer."
    entries = [p for p in candidate.iterdir() if p.is_file() and not (desktop / p.name).exists()]
    if dry_run:
        return f"Aperçu : {len(entries)} fichier(s) seraient restaurés depuis '{candidate.name}'."
    restored = 0
    for source in entries:
        try:
            shutil.move(str(source), str(desktop / source.name))
            restored += 1
        except OSError:
            continue
    return f"{restored} fichier(s) restauré(s) depuis '{candidate.name}'."


def list_desktop() -> str:
    desktop = _get_desktop()
    items = []
    for item in sorted(desktop.iterdir()):
        if item.name.startswith("."):
            continue
        if item.is_dir():
            try:
                count = len(list(item.iterdir()))
            except PermissionError:
                count = "?"
            items.append(f"📁 {item.name}/ ({count} éléments)")
        else:
            size = item.stat().st_size
            size_str = f"{size / 1024:.1f} Ko" if size < 1024 * 1024 else f"{size / 1024 / 1024:.1f} Mo"
            items.append(f"📄 {item.name} ({size_str})")
    if not items:
        return "Le bureau est vide."
    return f"Bureau ({len(items)} éléments) :\n" + "\n".join(items)


def get_desktop_stats() -> str:
    desktop = _get_desktop()
    files = [i for i in desktop.iterdir() if i.is_file()]
    folders = [i for i in desktop.iterdir() if i.is_dir()]
    total_size = sum(f.stat().st_size for f in files if f.exists())
    size_str = f"{total_size / 1024:.1f} Ko" if total_size < 1024 * 1024 else f"{total_size / 1024 / 1024:.1f} Mo"
    return (
        f"Statistiques du bureau ({_OS}) :\n"
        f"  Fichiers : {len(files)}\n"
        f"  Dossiers : {len(folders)}\n"
        f"  Taille   : {size_str}\n"
        f"  Emplacement : {desktop}"
    )


# ════════════════════════════════════════════════════════════════════════════
# Parsing local des commandes naturelles
# ════════════════════════════════════════════════════════════════════════════

def _parse_desktop_command_locally(text: str) -> Optional[Dict[str, Any]]:
    """Interprète les phrases naturelles concernant le bureau."""
    text = re.sub(r"\s+", " ", (text or "").lower().strip())
    text = re.sub(r"\b(s'il te pla[iî]t|stp|please|peux[- ]tu|tu peux|je veux|j'aimerais)\b",
                  " ", text).strip()

    # 1. Fond d'écran aléatoire
    if re.search(r"\bfond d'[ée]cran al[ée]atoire\b|\brandom\s+wallpaper\b|"
                 r"\bwallpaper al[ée]atoire\b|\bun\s+wallpaper au hasard\b", text):
        return {"action": "random_wallpaper"}

    # 2. Fond d'écran depuis une URL
    m = re.search(r"(?:fond d'[ée]cran|wallpaper)\s+(?:depuis|via|à partir de|avec l'url)\s+(https?://\S+)", text)
    if m:
        return {"action": "wallpaper_url", "url": m.group(1).strip()}

    # 3. Changer le fond d'écran avec un fichier (ou une URL brute)
    m = re.search(r"(?:change|défini[s]?|mets?|applique|utilise)\s+.*?"
                  r"(?:fond d'[ée]cran|wallpaper)\s+(?:avec|sur|par|en utilisant)?\s*(.+)", text)
    if m:
        target = m.group(1).strip().strip("'\"")
        if target.startswith("http"):
            return {"action": "wallpaper_url", "url": target}
        if target:
            return {"action": "wallpaper", "path": target}

    # 4. Connaître le fond d'écran actuel
    if re.search(r"\bquel\s+est\b.*\b(?:fond d'[ée]cran|wallpaper)\b|"
                 r"\b(?:fond d'[ée]cran|wallpaper)\s+actuel\b|"
                 r"\baffiche\s+le\s+(?:fond d'[ée]cran|wallpaper)\b", text):
        return {"action": "current_wallpaper"}

    # 5. Aperçu avant une opération de rangement
    if re.search(r"\b(aperçu|apercu|prévisualise|previsualise)\b.*\b(?:range|organise|nettoie)\b.*\b(?:bureau|desktop)\b", text):
        return {"action": "preview"}

    # 6. Organiser le bureau
    if re.search(r"\b(organise|range|ordonne|classe|trie)\s+(?:le |mon )?(?:bureau|desktop)\b", text):
        mode = "by_date" if re.search(r"\b(par\s+)?date\b|\bchronologique\b", text) else "by_type"
        return {"action": "organize", "mode": mode}

    # 7. Nettoyer le bureau
    if re.search(r"\b(nettoie|nettoyage|archive)\s+(?:le |mon )?(?:bureau|desktop)\b", text):
        return {"action": "clean"}

    if re.search(r"\b(restaure|annule)\b.*\b(?:nettoyage|archive|bureau|desktop)\b", text):
        return {"action": "restore"}

    # 8. Lister le contenu
    if re.search(r"\b(liste|montre|affiche|qu'y a[- ]t[- ]il)\b.*\b(?:bureau|desktop)\b|"
                 r"\b(?:bureau|desktop)\b.*\b(liste|contenu)\b", text):
        return {"action": "list"}

    # 9. Statistiques
    if re.search(r"\b(statistiques?|stats?|taille|occupation)\b.*\b(?:bureau|desktop)\b", text):
        return {"action": "stats"}
    return None


def _detect_desktop_action_via_ai(description: str) -> Optional[Dict]:
    api_key = _get_api_key()
    if not api_key:
        return None
    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        prompt = f"""Tu es un assistant de bureau. Analyse la phrase et retourne UNIQUEMENT un objet JSON avec l'action et les paramètres nécessaires.
Actions possibles : wallpaper (path), wallpaper_url (url), current_wallpaper, random_wallpaper, organize (mode: "by_type" ou "by_date"), clean, list, stats, task (si l'action est trop complexe pour être classée).
Phrase : "{description}"
Exemples :
"change le fond d'écran avec /home/user/image.png" -> {{"action":"wallpaper","path":"/home/user/image.png"}}
"affiche le fond d'écran actuel" -> {{"action":"current_wallpaper"}}
"range le bureau par date" -> {{"action":"organize","mode":"by_date"}}
"nettoie le bureau" -> {{"action":"clean"}}
"mets un fond d'écran aléatoire" -> {{"action":"random_wallpaper"}}
"crée un dossier Projets sur le bureau" -> {{"action":"task","task":"crée un dossier Projets sur le bureau"}}
Retourne uniquement le JSON, sans commentaire."""
        resp = client.models.generate_content(model=FAST_MODEL, contents=prompt)
        json_match = re.search(r'\{.*\}', resp.text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group(0))
    except Exception as e:
        print(f"[Desktop] AI detection error: {e}")
    return None


# ════════════════════════════════════════════════════════════════════════════
# Sandbox pour l'exécution sécurisée de code généré par l'IA
# ════════════════════════════════════════════════════════════════════════════

def _build_sandbox() -> dict:
    import time
    safe_builtins = {
        "print": print, "len": len, "str": str, "int": int, "float": float,
        "bool": bool, "list": list, "dict": dict, "tuple": tuple,
        "range": range, "enumerate": enumerate, "sorted": sorted,
        "isinstance": isinstance, "hasattr": hasattr,
        "max": max, "min": min, "sum": sum, "abs": abs,
        "zip": zip, "map": map, "filter": filter,
    }
    sandbox = {
        "__builtins__": safe_builtins,
        "Path": Path,
        "time": time,
        "shutil": type("shutil", (), {
            "copy2": shutil.copy2,
            "copytree": shutil.copytree,
            "disk_usage": shutil.disk_usage,
        })(),
        "os_path": os.path,
    }
    if _PYAUTOGUI:
        sandbox["pyautogui"] = pyautogui
    if _OS == "Windows":
        try:
            import ctypes, winreg
            sandbox["ctypes"] = ctypes
            sandbox["winreg"] = type("winreg", (), {
                "OpenKey": winreg.OpenKey,
                "QueryValueEx": winreg.QueryValueEx,
                "HKEY_CURRENT_USER": winreg.HKEY_CURRENT_USER,
            })()
        except ImportError:
            pass
    return sandbox


def _strip_code_fences(code: str) -> str:
    """Retire proprement les clôtures markdown ``` du code généré."""
    code = (code or "").strip()
    if code.startswith("```"):
        lines = code.split("\n")
        end = len(lines)
        for i in range(len(lines) - 1, 0, -1):
            if lines[i].strip().startswith("```"):
                end = i
                break
        code = "\n".join(lines[1:end]).strip()
    return code


def _execute_generated_code(code: str, player=None) -> str:
    if not code or code.strip() == "UNSAFE":
        return "Cette action ne peut pas être exécutée en toute sécurité."
    code = _strip_code_fences(code)
    if not code:
        return "Code vide généré par l'IA."
    sandbox = _build_sandbox()
    output_lines = []
    sandbox["__builtins__"]["print"] = lambda *a: output_lines.append(" ".join(str(x) for x in a))
    try:
        exec(compile(code, "<jarvis_desktop>", "exec"), sandbox)
        return "\n".join(output_lines) if output_lines else "Action réalisée avec succès."
    except Exception as e:
        print(f"[Desktop] Exec error: {e}\nCode:\n{code[:300]}")
        return f"Erreur d'exécution : {e}"


def _ask_gemini_for_desktop_action(task: str) -> str:
    api_key = _get_api_key()
    if not api_key:
        return "Clé API indisponible pour l'IA."
    try:
        from google import genai as _genai
        _client = _genai.Client(api_key=api_key)
    except Exception as e:
        return f"IA indisponible : {e}"
    desktop = str(_get_desktop())
    os_specific = ("- ctypes (appels API Windows, lecture seule)\n- winreg (registre, lecture seule)"
                   if _OS == "Windows" else
                   "- subprocess n'est pas disponible ; utilisez pyautogui ou Path uniquement")
    prompt = f"""Tu es un assistant d'automatisation du bureau.
OS actuel : {_OS}
Chemin du bureau : {desktop}
Génère un code Python SÛR pour accomplir la tâche ci-dessous.
Modules autorisés UNIQUEMENT :
pyautogui (souris, clavier - si nécessaire)
pathlib.Path (inspection de fichiers/dossiers, PAS de suppression)
shutil.copy2, shutil.copytree, shutil.disk_usage (PAS move, PAS rmtree)
os_path (équivalent os.path, lecture seule)
time.sleep
{os_specific}
Règles strictes :
AUCUNE suppression de fichier (pas de unlink, rmtree, remove)
AUCUN appel subprocess
AUCUN exec() ou eval() dans le code
AUCUNE instruction import (les modules sont déjà injectés)
AUCUNE écriture de fichier sauf demande explicite
Si la tâche ne peut pas être accomplie en sécurité avec ces outils, réponds exactement : UNSAFE
Renvoyer UNIQUEMENT le code Python. Aucune explication, aucun markdown.
Tâche : {task}"""
    try:
        response = _client.models.generate_content(model=FAST_MODEL, contents=prompt)
        return _strip_code_fences(response.text)
    except Exception as e:
        return f"Erreur IA : {e}"


# ════════════════════════════════════════════════════════════════════════════
# Point d'entrée public
# ════════════════════════════════════════════════════════════════════════════

_KNOWN_ACTIONS = {"wallpaper", "wallpaper_url", "current_wallpaper",
                  "random_wallpaper", "organize", "preview", "clean", "restore",
                  "list", "stats", "task"}


@kit.action("desktop_control")
def desktop_control(
    parameters: dict = None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    """
    Gère le bureau : fond d'écran, organisation réversible, nettoyage, liste et stats.
    Accepte soit une action explicite, soit une description en langage naturel.
    """
    params = parameters or {}
    action = str(params.get("action", "") or "").lower().strip()
    description = str(params.get("description", "") or "").strip()

    # Interprétation locale puis IA d'une description
    if description and not action:
        local_parsed = _parse_desktop_command_locally(description)
        if local_parsed:
            action = local_parsed.pop("action", "")
            for k, v in local_parsed.items():
                if k not in params or params[k] is None:
                    params[k] = v
        else:
            ai_detected = _detect_desktop_action_via_ai(description)
            if ai_detected:
                action = str(ai_detected.pop("action", "") or "")
                for k, v in ai_detected.items():
                    if k not in params or params[k] is None:
                        params[k] = v
            else:
                return "Je n'ai pas compris cette commande concernant le bureau. Pouvez-vous reformuler ?"
    if not action:
        return "Aucune action bureau demandée."

    if player:
        try:
            player.write_log(f"[desktop] {action}")
        except Exception:
            pass

    try:
        if action == "wallpaper":
            path = params.get("path", "")
            if not path:
                return "Aucun chemin d'image fourni."
            return set_wallpaper(path)
        elif action == "wallpaper_url":
            url = params.get("url", "")
            if not url:
                return "Aucune URL fournie."
            return set_wallpaper_from_url(url)
        elif action == "current_wallpaper":
            return get_current_wallpaper()
        elif action == "random_wallpaper":
            return set_random_wallpaper(params.get("directory"))
        elif action == "organize":
            return organize_desktop(params.get("mode", "by_type"),
                                    dry_run=bool(params.get("dry_run", False)))
        elif action == "preview":
            return organize_desktop(params.get("mode", "by_type"), dry_run=True)
        elif action == "clean":
            return clean_desktop(dry_run=bool(params.get("dry_run", False)))
        elif action == "restore":
            return restore_desktop_archive(params.get("archive"),
                                           dry_run=bool(params.get("dry_run", False)))
        elif action == "list":
            return list_desktop()
        elif action == "stats":
            return get_desktop_stats()
        elif action == "task" or params.get("task"):
            return ("Cette tâche n'est pas une opération Bureau sûre reconnue. Utilisez "
                    "file_controller pour les fichiers précis, ou wallpaper, organize, "
                    "clean, restore, list et stats ici.")
        elif action not in _KNOWN_ACTIONS:
            return f"Action Bureau inconnue : {action}."
        else:
            return "Aucune action bureau demandée."
    except Exception as e:
        print(f"[Desktop] Erreur : {e}")
        return f"Erreur de contrôle du bureau : {e}"


# ════════════════════════════════════════════════════════════════════════════
# Test direct
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        print(desktop_control({"description": " ".join(sys.argv[1:])}))
    else:
        print("Usage: python desktop.py <commande naturelle>")
