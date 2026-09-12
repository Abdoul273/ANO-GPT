#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
file_controller.py — Gestionnaire de fichiers robuste.
Parsing local avancé, compréhension naturelle, sécurité renforcée.
Compatible Linux (Arch/Hyprland), macOS, Windows. Fallback IA si nécessaire.

Corrections par rapport à l'ancienne version :
    - `_get_api_key` utilisait `Path(file)` au lieu de `Path(__file__)` et
      levait une exception si la clé était absente : corrigé + try/except ;
    - les valeurs par défaut `name=" "`, `content=" "` (un espace) créaient
      des fichiers nommés « » ou écrivaient un espace par défaut :
      remplacées par des chaînes vides ;
    - `_resolve_path` : le `lstrip` était une classe de caractères erronée et
      les noms français (bureau, téléchargements, images…) n'étaient pas
      compris : résolution via XDG_* → xdg-user-dir → dossiers usuels FR/EN ;
    - `find_files` dépendait uniquement de actions.smart_search : ajout d'un
      repli local (parcours sûr + filtre extension + pertinence) si le
      module est absent ou échoue ;
    - `organize_desktop` : la condition de saut des dossiers cibles était
      cassée : corrigée ;
    - regex de parsing nettoyées (guillemets dupliqués, extraction
      extension/nom fiable) ;
    - `create_file`/`write_file` signalent désormais quand un fichier
      existant est remplacé.

Ajouts :
    - action `open` : ouvrir un fichier/dossier avec l'application par défaut ;
    - recherche repli sans smart_search ;
    - aliases d'actions (ouvre, liste, supprime, déplace…).
"""
import os
import re
import json
import shutil
import time
import platform
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any, List

from core.undo_stack import push as push_undo

from core import action_kit as kit
from core.live_model_policy import FAST_MODEL

# send2trash tire tout un support de bureau à l'import (~120 ms) pour une
# fonction appelée au plus quelques fois par session.
_SEND2TRASH_MODULE = None      # None = pas encore cherché ; False = absent


def _send2trash_module():
    global _SEND2TRASH_MODULE
    if _SEND2TRASH_MODULE is None:
        try:
            import send2trash
            _SEND2TRASH_MODULE = send2trash
        except ImportError:
            _SEND2TRASH_MODULE = False
    return _SEND2TRASH_MODULE or None

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"

_MEDIA_EXTENSIONS = {
    "video": ".mp4,.avi,.mkv,.mov,.wmv,.flv,.webm,.m4v,.mpeg,.mpg",
    "audio": ".mp3,.m4a,.wav,.flac,.ogg,.opus,.aac,.wma",
    "image": ".jpg,.jpeg,.png,.webp,.gif,.bmp,.tiff,.svg",
}


def _remove_created_path(path: Path) -> str:
    """Retire uniquement un élément créé par ANO-GPT, sans écraser de données."""
    if not path.exists():
        return "L'élément n'existait déjà plus."
    if path.is_dir():
        path.rmdir()  # refuse si l'utilisateur y a ajouté quelque chose
    else:
        path.unlink()
    return f"{path.name} retiré."


def _restore_bytes(path: Path, data: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return f"Ancien contenu de {path.name} restauré."


def _move_back(current: Path, original: Path) -> str:
    if not current.exists():
        raise FileNotFoundError(f"{current.name} n'existe plus")
    if original.exists():
        raise FileExistsError(f"{original.name} existe déjà")
    original.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(current), str(original))
    return f"{original.name} remis à sa place."


def _restore_organized_files(entries: tuple[tuple[Path, Path], ...]) -> str:
    restored = 0
    for original, current in reversed(entries):
        if not current.exists() or original.exists():
            continue
        shutil.move(str(current), str(original))
        restored += 1
        try:
            current.parent.rmdir()
        except OSError:
            pass
    return f"{restored} fichier(s) remis sur le bureau."


# ════════════════════════════════════════════════════════════════════════════
# Sécurité : confinement au répertoire utilisateur
# ════════════════════════════════════════════════════════════════════════════

_SAFE_ROOTS: List[Path] = [Path.home()]


def _is_safe_path(target: Path) -> bool:
    """Vérifie que le chemin résolu (symlinks compris) est dans $HOME."""
    try:
        resolved = target.resolve()
        return any(
            resolved == root.resolve() or resolved.is_relative_to(root.resolve())
            for root in _SAFE_ROOTS
        )
    except Exception:
        return False


# ════════════════════════════════════════════════════════════════════════════
# Dossiers standards (XDG → xdg-user-dir → noms usuels FR/EN)
# ════════════════════════════════════════════════════════════════════════════

def _xdg_dir(env_var: str, fallbacks: List[str]) -> Path:
    if _OS == "Linux":
        val = os.environ.get(env_var, "")
        if val and Path(val).expanduser().is_dir():
            return Path(val).expanduser()
        if shutil.which("xdg-user-dir"):
            try:
                key = env_var.replace("XDG_", "").replace("_DIR", "")
                r = kit.run(["xdg-user-dir", key], timeout=2)
                p = Path(r.stdout.strip()).expanduser()
                if r.returncode == 0 and p.is_dir():
                    return p
            except Exception:
                pass
    for name in fallbacks:
        p = Path.home() / name
        if p.is_dir():
            return p
    return Path.home() / fallbacks[0]


def _get_desktop() -> Path:
    return _xdg_dir("XDG_DESKTOP_DIR", ["Desktop", "Bureau"])


def _get_downloads() -> Path:
    return _xdg_dir("XDG_DOWNLOAD_DIR", ["Downloads", "Téléchargements", "Telechargements"])


def _get_documents() -> Path:
    return _xdg_dir("XDG_DOCUMENTS_DIR", ["Documents"])


def _get_pictures() -> Path:
    return _xdg_dir("XDG_PICTURES_DIR", ["Pictures", "Images", "Photos"])


def _get_music() -> Path:
    return _xdg_dir("XDG_MUSIC_DIR", ["Music", "Musique"])


def _get_videos() -> Path:
    return _xdg_dir("XDG_VIDEOS_DIR", ["Videos", "Vidéos"])


_SHORTCUTS: Optional[Dict[str, Path]] = None


def _get_shortcuts() -> Dict[str, Path]:
    global _SHORTCUTS
    if _SHORTCUTS is None:
        _SHORTCUTS = {
            "desktop": _get_desktop(), "bureau": _get_desktop(),
            "downloads": _get_downloads(), "téléchargements": _get_downloads(),
            "telechargements": _get_downloads(),
            "documents": _get_documents(),
            "pictures": _get_pictures(), "images": _get_pictures(), "photos": _get_pictures(),
            "music": _get_music(), "musique": _get_music(),
            "videos": _get_videos(), "vidéos": _get_videos(),
            "home": Path.home(), "accueil": Path.home(),
        }
    return _SHORTCUTS


def _resolve_path(raw: str) -> Path:
    """Convertit un nom court (desktop, bureau, downloads…) ou un chemin
    absolu/relatif en Path. Supporte les préfixes (« documents/rapport.txt »)."""
    raw = (raw or "").strip()
    if not raw:
        return Path.home()
    sc = _get_shortcuts()
    lower = raw.lower()
    if lower in sc:
        return sc[lower]
    for key, val in sc.items():
        for sep in ("/", "\\"):
            pref = key + sep
            if lower.startswith(pref):
                rest = raw[len(key):].lstrip("/\\")
                return val / rest
    return Path(raw).expanduser()


def _format_size(b: int) -> str:
    size = float(b)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _safe_trash(target: Path) -> str:
    if _send2trash_module() is None:
        return (
            "send2trash n'est pas installé. La suppression définitive est "
            "désactivée pour votre sécurité.\nInstallez-le avec : pip install send2trash"
        )
    _send2trash_module().send2trash(str(target))
    return f"Déplacé vers la corbeille : {target.name}"


# ════════════════════════════════════════════════════════════════════════════
# Opérations de base
# ════════════════════════════════════════════════════════════════════════════

def list_files(path: str = "desktop", show_hidden: bool = False) -> str:
    try:
        target = _resolve_path(path)
        if not _is_safe_path(target):
            return f"Accès refusé : {target}"
        if not target.exists():
            return f"Chemin introuvable : {target}"
        if not target.is_dir():
            return f"Ce n'est pas un dossier : {target}"
        items = []
        for item in sorted(target.iterdir()):
            if not show_hidden and item.name.startswith("."):
                continue
            if item.is_dir():
                items.append(f"📁 {item.name}/")
            else:
                size = _format_size(item.stat().st_size)
                items.append(f"📄 {item.name} ({size})")
        if not items:
            return f"Le dossier {target.name}/ est vide."
        return f"Contenu de {target.name}/ ({len(items)} éléments) :\n" + "\n".join(items)
    except PermissionError:
        return f"Permission refusée : {path}"
    except Exception as e:
        return f"Erreur lors du listage : {e}"


def create_file(path: str, name: str = "", content: str = "") -> str:
    try:
        base = _resolve_path(path)
        target = (base / name) if name else base
        if not _is_safe_path(target):
            return f"Accès refusé : {target}"
        replaced = target.exists()
        previous = (target.read_bytes() if replaced and target.is_file()
                    and target.stat().st_size <= 1_048_576 else None)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content or "", encoding="utf-8")
        if not replaced:
            push_undo(f"création du fichier {target.name}",
                      lambda p=target: _remove_created_path(p))
        elif previous is not None:
            push_undo(f"remplacement du fichier {target.name}",
                      lambda p=target, data=previous: _restore_bytes(p, data))
        note = " (remplacé)" if replaced else ""
        return f"Fichier créé : {target.name}{note}"
    except Exception as e:
        return f"Impossible de créer le fichier : {e}"


def create_folder(path: str, name: str = "") -> str:
    try:
        base = _resolve_path(path)
        target = (base / name) if name else base
        if not _is_safe_path(target):
            return f"Accès refusé : {target}"
        existed = target.exists()
        target.mkdir(parents=True, exist_ok=True)
        if not existed:
            push_undo(f"création du dossier {target.name}",
                      lambda p=target: _remove_created_path(p))
        return f"Dossier créé : {target.name}"
    except Exception as e:
        return f"Impossible de créer le dossier : {e}"


def delete_file(path: str, name: str = "") -> str:
    try:
        base = _resolve_path(path)
        target = (base / name) if name else base
        if not _is_safe_path(target):
            return f"Accès refusé : {target}"
        if not target.exists():
            return f"Introuvable : {target.name}"
        protected = {
            _get_desktop(), _get_downloads(), _get_documents(),
            _get_pictures(), _get_music(), _get_videos(), Path.home()
        }
        if target.resolve() in {p.resolve() for p in protected}:
            return f"Dossier protégé, suppression impossible : {target.name}"
        return _safe_trash(target)
    except PermissionError:
        return f"Permission refusée : {path}"
    except Exception as e:
        return f"Impossible de supprimer : {e}"


def move_file(path: str, name: str = "", destination: str = "") -> str:
    try:
        base = _resolve_path(path)
        src = (base / name) if name else base
        dst = _resolve_path(destination) if destination else None
        if not src.exists():
            return f"Source introuvable : {src.name}"
        if dst is None:
            return "Aucune destination spécifiée."
        if not _is_safe_path(src):
            return f"Accès refusé (source) : {src}"
        if not _is_safe_path(dst):
            return f"Accès refusé (destination) : {dst}"
        if dst.is_dir():
            dst = dst / src.name
        if dst.exists():
            return f"Destination déjà existante, déplacement refusé : {dst.name}"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        push_undo(f"déplacement de {src.name} vers {dst.parent.name}",
                  lambda old=src, new=dst: _move_back(new, old))
        return f"Déplacé : {src.name} → {dst.parent.name}/"
    except Exception as e:
        return f"Impossible de déplacer : {e}"


def copy_file(path: str, name: str = "", destination: str = "") -> str:
    try:
        base = _resolve_path(path)
        src = (base / name) if name else base
        dst = _resolve_path(destination) if destination else None
        if not src.exists():
            return f"Source introuvable : {src.name}"
        if dst is None:
            return "Aucune destination spécifiée."
        if not _is_safe_path(src):
            return f"Accès refusé (source) : {src}"
        if not _is_safe_path(dst):
            return f"Accès refusé (destination) : {dst}"
        if dst.is_dir():
            dst = dst / src.name
        if dst.exists():
            return f"Destination déjà existante, copie refusée : {dst.name}"
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(str(src), str(dst))
        else:
            shutil.copy2(str(src), str(dst))
        push_undo(f"copie de {src.name} vers {dst.parent.name}",
                  lambda p=dst: _remove_created_path(p))
        return f"Copié : {src.name} → {dst.parent.name}/"
    except Exception as e:
        return f"Impossible de copier : {e}"


def rename_file(path: str, name: str = "", new_name: str = "") -> str:
    try:
        base = _resolve_path(path)
        target = (base / name) if name else base
        if not _is_safe_path(target):
            return f"Accès refusé : {target}"
        if not target.exists():
            return f"Introuvable : {target.name}"
        if not new_name:
            return "Aucun nouveau nom fourni."
        new_path = target.parent / new_name
        if new_path.exists():
            return f"Un fichier nommé '{new_name}' existe déjà."
        target.rename(new_path)
        push_undo(f"renommage de {target.name} en {new_name}",
                  lambda old=target, new=new_path: _move_back(new, old))
        return f"Renommé : {target.name} → {new_name}"
    except Exception as e:
        return f"Impossible de renommer : {e}"


def read_file(path: str, name: str = "", max_chars: int = 4000) -> str:
    try:
        base = _resolve_path(path)
        target = (base / name) if name else base
        if not _is_safe_path(target):
            return f"Accès refusé : {target}"
        if not target.exists():
            return f"Fichier introuvable : {target.name}"
        if not target.is_file():
            return f"Ce n'est pas un fichier : {target.name}"
        content = target.read_text(encoding="utf-8", errors="ignore")
        if len(content) > max_chars:
            content = content[:max_chars] + f"\n\n[Tronqué — {len(content)} caractères au total]"
        return content
    except Exception as e:
        return f"Impossible de lire le fichier : {e}"


def write_file(path: str, name: str = "", content: str = "", append: bool = False) -> str:
    try:
        base = _resolve_path(path)
        target = (base / name) if name else base
        if not _is_safe_path(target):
            return f"Accès refusé : {target}"
        target.parent.mkdir(parents=True, exist_ok=True)
        existed = target.exists()
        previous = (target.read_bytes() if existed and target.is_file()
                    and target.stat().st_size <= 1_048_576 else None)
        mode = "a" if append else "w"
        with open(target, mode, encoding="utf-8") as f:
            f.write(content or "")
        if not existed:
            push_undo(f"écriture du nouveau fichier {target.name}",
                      lambda p=target: _remove_created_path(p))
        elif previous is not None:
            push_undo(f"écriture dans {target.name}",
                      lambda p=target, data=previous: _restore_bytes(p, data))
        action = "Texte ajouté à" if append else ("Écrit dans" if not existed else "Remplacé dans")
        return f"{action} : {target.name}"
    except Exception as e:
        return f"Impossible d'écrire : {e}"


def open_path(path: str, name: str = "") -> str:
    """Ouvre un fichier/dossier avec l'application par défaut."""
    try:
        base = _resolve_path(path)
        target = (base / name) if name else base
        if not _is_safe_path(target):
            return f"Accès refusé : {target}"
        if not target.exists():
            return f"Introuvable : {target.name}"
        from core.browser_policy import WEB_DOCUMENT_SUFFIXES, open_chrome
        if target.suffix.lower() in WEB_DOCUMENT_SUFFIXES:
            if open_chrome(target.resolve().as_uri()):
                return f"Ouvert dans Chrome : {target.name}"
            return "Impossible de lancer Google Chrome."
        if _OS == "Windows":
            os.startfile(str(target))
        elif _OS == "Darwin":
            kit.spawn(["open", str(target)])
        else:
            kit.spawn(['xdg-open', str(target)])
        return f"Ouvert : {target.name}"
    except Exception as e:
        return f"Impossible d'ouvrir : {e}"


# ════════════════════════════════════════════════════════════════════════════
# Recherche
# ════════════════════════════════════════════════════════════════════════════

# Dossiers qui gonflent un parcours sans jamais contenir ce que l'utilisateur
# cherche à la voix : caches, dépendances, dépôts.
_NOISY_DIRS = frozenset({
    ".cache", ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv",
    "venv", ".npm", ".cargo", ".rustup", ".gradle", ".m2", ".local", ".var",
    ".mozilla", ".config", ".steam", ".wine", "site-packages", ".trash",
    "Trash", ".thumbnails", "build", "dist", "target",
})


def _iter_files(root: Path, *, budget_s: float = 4.0):
    """Parcours borné dans le temps, en élaguant les dossiers bruyants.

    `rglob` sur le dossier personnel pouvait durer une minute entière en
    traversant `.cache` et `node_modules` : ici on saute ces dossiers et on
    s'arrête quand le budget est consommé — mieux vaut un résultat partiel
    rapide qu'un tour de parole gelé.
    """
    deadline = time.monotonic() + budget_s
    stack = [root]
    while stack:
        current = stack.pop()
        if time.monotonic() > deadline:
            return
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name not in _NOISY_DIRS:
                                stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            yield Path(entry.path)
                    except OSError:
                        continue
        except (PermissionError, FileNotFoundError, NotADirectoryError, OSError):
            continue


def _fallback_find(query: str, extension: str, search_path: Path,
                   max_results: int) -> List[tuple]:
    """Repli local si actions.smart_search est absent/échoue."""
    q = (query or "").lower()
    ext = (extension or "").lower().lstrip(".")
    matches: List[tuple] = []
    try:
        for item in _iter_files(search_path):
            name_l = item.name.lower()
            if ext and not name_l.endswith("." + ext):
                continue
            score = 0.0
            if q:
                if q in name_l:
                    score = 0.9
                else:
                    continue
            else:
                score = 0.5
            matches.append((score, item))
            if len(matches) >= max_results * 3:
                break
    except Exception:
        pass
    matches.sort(key=lambda t: -t[0])
    return matches[:max_results]


def search_content(query: str, max_results: int = 15) -> str:
    """Recherche plein texte FTS5 dans le contenu des documents et scripts personnels."""
    try:
        from core.file_indexer import search_personal_files
        results = search_personal_files(query, limit=max_results)
        if not results:
            return f"Aucun document trouvé contenant « {query} » dans vos dossiers personnels."
        lines = [f"{len(results)} document(s) trouvé(s) pour « {query} » (nom & contenu) :"]
        for r in results:
            snippet_clean = r.snippet.replace("<b>", "« ").replace("</b>", " »").strip()
            size_str = _format_size(r.size)
            lines.append(f"📄 {r.filename} ({size_str}) — {Path(r.path).parent}")
            if snippet_clean:
                lines.append(f"   Extrait : {snippet_clean}")
        return "\n".join(lines)
    except Exception as e:
        return f"Erreur de recherche plein texte : {e}"


def find_files(name: str = "", extension: str = "", path: str = "home",
               max_results: int = 20, kind: str = "", player=None,
               session_memory=None) -> str:
    try:
        search_path = _resolve_path(path)
        if not _is_safe_path(search_path):
            return f"Accès refusé : {search_path}"
        if not search_path.exists():
            return f"Chemin introuvable : {path}"
        kind = (kind or "").strip().lower()
        extension = extension or _MEDIA_EXTENSIONS.get(kind, "")
        matches = None
        try:
            from actions.smart_search import smart_search_files
            matches = smart_search_files(
                query=name, search_paths=[search_path],
                extension=extension, max_results=max_results
            )
        except Exception:
            matches = None
        if matches is None:
            matches = _fallback_find(name, extension, search_path, max_results)

        # Repli FTS5 : si aucun fichier trouvé par nom seul, chercher par contenu plein texte
        if not matches and name and len(name) >= 3 and not extension:
            try:
                from core.file_indexer import search_personal_files
                fts_results = search_personal_files(name, limit=max_results)
                if fts_results:
                    lines = [f"Aucun fichier nommé exactement « {name} ». Voici {len(fts_results)} document(s) trouvé(s) par contenu :"]
                    for r in fts_results:
                        snippet_clean = r.snippet.replace("<b>", "« ").replace("</b>", " »").strip()
                        lines.append(f"📄 {r.filename} ({_format_size(r.size)}) — {Path(r.path).parent}")
                        if snippet_clean:
                            lines.append(f"   Extrait : {snippet_clean}")
                    return "\n".join(lines)
            except Exception:
                pass

        if not matches:
            query = name or extension or "fichiers"
            return f"Aucun {query} trouvé dans {search_path.name}/"
        results = []
        local_videos = []
        exact_found = False
        from core.media_search import normalize_text, readable_media_name
        query_tokens = normalize_text(name).split()
        for score, item in matches:
            try:
                size = _format_size(item.stat().st_size)
            except Exception:
                size = "?"
            match_indicator = f" (pertinence: {int(score*100)}%)" if name else ""
            results.append(f"📄 {item.name} ({size}){match_indicator} — {item.parent}")
            if kind == "video":
                local_videos.append({
                    "path": str(item),
                    "title": readable_media_name(item.stem) or item.stem,
                    "folder": item.parent.name,
                    "score": round(float(score), 3),
                    "source": "local",
                    "kind": "video",
                })
            target_tokens = normalize_text(readable_media_name(item.stem)).split()
            if query_tokens and any(
                target_tokens[index:index + len(query_tokens)] == query_tokens
                for index in range(max(0, len(target_tokens) - len(query_tokens) + 1))
            ):
                exact_found = True
        if name and not exact_found:
            heading = (
                f"Aucun fichier nommé exactement « {name} ». "
                f"Voici {len(results)} correspondance(s) proche(s) :"
            )
        else:
            heading = f"{len(results)} fichier(s) trouvé(s) :"
        if local_videos:
            from core.local_video import prepare_local_videos
            prepared = prepare_local_videos(local_videos, limit=min(max_results, 12))
            if session_memory is not None:
                session_memory["music_local_results"] = prepared
            if player is not None and hasattr(player, "show_video_results"):
                player.show_video_results(name or "vidéos locales", prepared)
        return heading + "\n" + "\n".join(results)
    except Exception as e:
        return f"Erreur de recherche : {e}"


def get_largest_files(path: str = "downloads", count: int = 10) -> str:
    count = min(max(1, int(count or 10)), 50)
    try:
        search_path = _resolve_path(path)
        if not _is_safe_path(search_path):
            return f"Accès refusé : {search_path}"
        if not search_path.exists():
            return f"Chemin introuvable : {path}"
        files = []
        for item in _iter_files(search_path, budget_s=8.0):
            try:
                files.append((item.stat().st_size, item))
            except Exception:
                continue
        files.sort(reverse=True)
        top = files[:count]
        if not top:
            return "Aucun fichier trouvé."
        lines = [f"Les {len(top)} plus gros fichiers de {search_path.name}/ :"]
        for size, f in top:
            lines.append(f"  {_format_size(size):>10}  {f.name}  ({f.parent})")
        return "\n".join(lines)
    except Exception as e:
        return f"Erreur : {e}"


def get_disk_usage(path: str = "home") -> str:
    try:
        target = _resolve_path(path)
        usage = shutil.disk_usage(target)
        pct = usage.used / usage.total * 100
        return (
            f"Utilisation du disque ({target}) :\n"
            f"  Total : {_format_size(usage.total)}\n"
            f"  Utilisé : {_format_size(usage.used)} ({pct:.1f}%)\n"
            f"  Libre : {_format_size(usage.free)}"
        )
    except Exception as e:
        return f"Impossible d'obtenir l'utilisation du disque : {e}"


def organize_desktop() -> str:
    type_map = {
        "Images":    {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg", ".ico", ".heic"},
        "Documents": {".pdf", ".doc", ".docx", ".txt", ".xls", ".xlsx", ".ppt", ".pptx", ".csv", ".odt", ".ods", ".odp"},
        "Vidéos":    {".mp4", ".avi", ".mkv", ".mov", ".wmv", ".flv", ".webm", ".m4v"},
        "Musique":   {".mp3", ".wav", ".flac", ".aac", ".ogg", ".wma", ".m4a"},
        "Archives":  {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"},
        "Code":      {".py", ".js", ".ts", ".html", ".css", ".json", ".xml", ".cpp", ".java", ".cs", ".go", ".rs", ".sh"},
    }
    desktop = _get_desktop()
    moved, skipped = [], []
    journal: list[tuple[Path, Path]] = []
    try:
        for item in list(desktop.iterdir()):
            if item.is_dir() or item.name.startswith("."):
                continue
            ext = item.suffix.lower()
            target_dir = desktop / "Autres"
            for folder, exts in type_map.items():
                if ext in exts:
                    target_dir = desktop / folder
                    break
            target_dir.mkdir(exist_ok=True)
            new_path = target_dir / item.name
            if new_path.exists():
                skipped.append(item.name)
                continue
            shutil.move(str(item), str(new_path))
            journal.append((item, new_path))
            moved.append(f"{item.name} → {target_dir.name}/")
        if journal:
            push_undo(f"organisation du bureau ({len(journal)} fichiers)",
                      lambda entries=tuple(journal): _restore_organized_files(entries))
        result = f"Bureau organisé : {len(moved)} fichier(s) déplacé(s)."
        if moved:
            result += "\n" + "\n".join(moved[:8])
            if len(moved) > 8:
                result += f"\n... et {len(moved) - 8} de plus."
        if skipped:
            result += f"\n{len(skipped)} fichier(s) ignoré(s) (conflit de nom)."
        return result
    except Exception as e:
        return f"Impossible d'organiser le bureau : {e}"


def get_file_info(path: str, name: str = "") -> str:
    try:
        base = _resolve_path(path)
        target = (base / name) if name else base
        if not _is_safe_path(target):
            return f"Accès refusé : {target}"
        if not target.exists():
            return f"Introuvable : {target.name}"
        stat = target.stat()
        info = {
            "Nom": target.name,
            "Type": "Dossier" if target.is_dir() else "Fichier",
            "Taille": _format_size(stat.st_size),
            "Emplacement": str(target.parent),
            "Créé le": datetime.fromtimestamp(stat.st_ctime).strftime("%Y-%m-%d %H:%M"),
            "Modifié le": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
            "Extension": target.suffix or "—",
        }
        return "\n".join(f"  {k}: {v}" for k, v in info.items())
    except Exception as e:
        return f"Impossible d'obtenir les informations : {e}"


# ════════════════════════════════════════════════════════════════════════════
# Parsing local pour commandes naturelles
# ════════════════════════════════════════════════════════════════════════════

def _parse_file_command_locally(text: str) -> Optional[Dict[str, Any]]:
    """Analyse une phrase en français/anglais et retourne les paramètres de l'action."""
    if not text:
        return None

    raw_lower = text.lower().strip()
    compact = re.sub(r"[\s\-_'’]+", "", raw_lower)

    # 1. Questions naturelles d'existence (avec ou sans espaces / STT)
    m_exist = re.search(
        r"(?:est\s*-?\s*ce\s+qu(?:e|\s*['’])?\s*(?:j\s*['’]?\s*ai|on\s+a)|y\s+a\s*-?\s*t\s*-?\s*il)\s+(?:un\s+)?fichier\s+(?:nomm[eé]|appel[eé])?\s*['\"]?([a-zA-Z0-9_\-\.]+?)['\"]?(?:\s+(?:dans|sur)\s+(?:mon\s+)?disque)?\s*\??$",
        raw_lower, re.IGNORECASE
    )
    if not m_exist:
        m_compact = re.search(
            r"estcequ?e?jaiunfichiernomm[eé]([a-zA-Z0-9_\-\.]+?)(?:dansmondisque|\?|$)",
            compact
        )
        if m_compact:
            return {"action": "find", "path": "home", "name": m_compact.group(1).strip().lower()}
    if m_exist:
        return {"action": "find", "path": "home", "name": m_exist.group(1).strip().lower()}

    text = re.sub(r"\s+", " ", raw_lower)
    text = re.sub(r"\b(s'il te pla[iî]t|stp|please|peux-tu|tu peux)\b", " ", text).strip()

    # Dossier cible (FR/EN)
    path = "home"
    folder_map = {
        "desktop": "desktop", "bureau": "desktop",
        "downloads": "downloads", "téléchargements": "downloads", "telechargements": "downloads",
        "documents": "documents",
        "pictures": "pictures", "images": "pictures", "photos": "pictures",
        "music": "music", "musique": "music",
        "videos": "videos", "vidéos": "videos",
        "home": "home", "accueil": "home",
    }
    for folder, key in folder_map.items():
        if folder in text:
            path = key
            break

    # list
    if re.search(r"\b(montre|affiche|liste|qu'y a-t-il|contenu|afficher|list|show|ls)\b", text):
        return {"action": "list", "path": path}

    # open
    m = re.search(r"(?:ouvre|open|lance)\s+(?:le |la |les |l'|l’)?(?:fichier |dossier )?['\"]?(.+?)['\"]?$", text)
    if m and not re.search(r"\b(crée|cr[eé]er?|supprime|déplace|copie|renomme|lis|écris)\b", text):
        return {"action": "open", "name": m.group(1).strip().strip("'\""), "path": path}

    # create_file
    m = re.search(r"(?:cr[eé]er?|crée|nouveau)\s+(?:un\s+)?fichier\s+(?:nomm[eé]|appel[eé])?\s*['\"]?(.+?)['\"]?(?:\s+dans\s+(.+))?", text)
    if m:
        name = m.group(1).strip().strip("'\"")
        folder = m.group(2).strip() if m.lastindex and m.lastindex >= 2 and m.group(2) else path
        return {"action": "create_file", "name": name, "path": folder}

    # create_folder
    m = re.search(r"(?:cr[eé]er?|crée|nouveau)\s+(?:un\s+)?(?:dossier|r[eé]pertoire)\s+(?:nomm[eé]|appel[eé])?\s*['\"]?(.+?)['\"]?(?:\s+dans\s+(.+))?", text)
    if m:
        name = m.group(1).strip().strip("'\"")
        folder = m.group(2).strip() if m.lastindex and m.lastindex >= 2 and m.group(2) else path
        return {"action": "create_folder", "name": name, "path": folder}

    # delete
    m = re.search(r"(?:supprime|efface|enl[eè]ve|d[eé]trui[st]|delete|remove)\s+(?:le |la |les |l'|l’)?(?:fichier |dossier )?['\"]?(.+?)['\"]?$", text)
    if m:
        return {"action": "delete", "name": m.group(1).strip().strip("'\""), "path": path}

    # move
    m = re.search(r"(?:d[eé]place|bouge|move)\s+(?:le |la |les |l'|l’)?(?:fichier |dossier )?['\"]?(.+?)['\"]?\s+(?:vers|dans|à|to)\s+(.+)$", text)
    if m:
        return {"action": "move", "name": m.group(1).strip().strip("'\""),
                "destination": m.group(2).strip().strip("'\""), "path": path}

    # copy
    m = re.search(r"(?:copie|duplique|copy)\s+(?:le |la |les |l'|l’)?(?:fichier |dossier )?['\"]?(.+?)['\"]?\s+(?:vers|dans|à|to)\s+(.+)$", text)
    if m:
        return {"action": "copy", "name": m.group(1).strip().strip("'\""),
                "destination": m.group(2).strip().strip("'\""), "path": path}

    # rename
    m = re.search(r"(?:renomme|renommer|rename)\s+(?:le |la |les |l'|l’)?(?:fichier |dossier )?['\"]?(.+?)['\"]?\s+(?:en |vers |to )?['\"]?(.+?)['\"]?$", text)
    if m:
        return {"action": "rename", "name": m.group(1).strip().strip("'\""),
                "new_name": m.group(2).strip().strip("'\""), "path": path}

    # read
    m = re.search(r"(?:lis|affiche le contenu de|montre le contenu de|read|cat)\s+(?:le |la |les |l'|l’)?(?:fichier )?['\"]?(.+?)['\"]?$", text)
    if m:
        return {"action": "read", "name": m.group(1).strip().strip("'\""), "path": path}

    # write
    m = re.search(r"(?:[ée]cris|ajoute|write|append)\s+(?:dans |au |le |la |les |l'|l’)?(?:fichier )?['\"]?(.+?)['\"]?\s+(?:le texte |le contenu |:)?\s*['\"]?(.+?)['\"]?$", text)
    if m:
        return {"action": "write", "name": m.group(1).strip().strip("'\""),
                "content": m.group(2).strip().strip("'\""), "path": path}

    # questions naturelles d'existence de fichier
    m_exist = re.search(
        r"(?:est\s*-?\s*ce\s+qu\s*['’]?\s*(?:j\s*['’]?\s*ai|on\s+a)|y\s+a\s*-?\s*t\s*-?\s*il)\s+(?:un\s+)?fichier\s+(?:nomm[eé]|appel[eé])?\s*['\"]?([a-zA-Z0-9_\-\.]+?)['\"]?(?:\s+dans\s+(?:mon\s+)?disque|\s+sur\s+(?:mon\s+)?disque|\s*\?)?$",
        text, re.IGNORECASE
    )
    if not m_exist:
        # Version compacte sans espaces (STT)
        m_exist = re.search(
            r"estcequ['’]?aiunfichiernomm[eé]([a-zA-Z0-9_\-\.]+)dansmondisque",
            re.sub(r"\s+", "", text.lower())
        )
    if m_exist:
        return {"action": "find", "path": "home", "name": m_exist.group(1).strip().lower()}

    # find
    m = re.search(r"(?:cherche|trouve|recherche|find|search)\s+(?:des |les |mes |tous les )?(?:fichiers )?(.*)$", text)
    if m:
        rest = m.group(1).strip()
        params = {"action": "find", "path": path}
        media_match = re.search(r"\b(vid[ée]os?|films?|clips?)\b", rest)
        if media_match:
            params["kind"] = "video"
            if path == "videos" and not re.search(
                r"\b(?:dans|sur)\s+(?:mon|mes|le dossier)?\s*vid[ée]os?\b", text
            ):
                params["path"] = "home"
            rest = re.sub(r"\b(?:une?|des|les|mes)?\s*(?:vid[ée]os?|films?|clips?)\b", " ", rest)
            rest = re.sub(
                r"^\s*(?:qui|dont|ayant|avec)?\s*(?:parle\s+de|contient|avec|ayant)?\s*",
                "", rest,
            )
            rest = re.sub(r"\s+(?:dans|sur)\s+(?:son|le)\s+nom\s*$", "", rest)
        ext_match = re.search(r"\.([a-z0-9]+)\b", rest)
        if ext_match:
            params["extension"] = "." + ext_match.group(1).lower()
        # Nom = ce qui reste après retrait de l'extension
        name_part = re.sub(r"\.([a-z0-9]+)\b", "", rest).strip()
        if name_part and name_part not in (".", "..", ""):
            params["name"] = name_part.strip("'\"")
        return params

    # largest
    if re.search(r"(?:plus gros|plus grands|les plus lourds|largest|biggest)", text):
        count_match = re.search(r"(\d+)", text)
        return {"action": "largest", "path": path,
                "count": int(count_match.group(1)) if count_match else 10}

    # disk_usage
    if re.search(r"(?:espace disque|disque|stockage|place|disk|usage)", text):
        return {"action": "disk_usage", "path": path}

    # organize_desktop
    if re.search(r"(?:organise|range|nettoie|ordonne|organize|clean)\s+(?:le |mon )?(?:bureau|desktop)", text):
        return {"action": "organize_desktop"}

    # info
    m = re.search(r"(?:infos?|d[eé]tails|propri[eé]t[eé]s|info|details)\s+(?:du |de la |de l'|de |le |la |les |l'|l’)?(?:fichier |dossier )?['\"]?(.+?)['\"]?$", text)
    if m:
        return {"action": "info", "name": m.group(1).strip().strip("'\""), "path": path}

    return None


def _get_api_key() -> str:
    try:
        base = Path(__file__).resolve().parent.parent
        config = base / "config" / "api_keys.json"
        with open(config, "r", encoding="utf-8") as f:
            return json.load(f).get("gemini_api_key", "")
    except Exception:
        return ""


def _detect_file_action_via_ai(description: str) -> Optional[Dict]:
    api_key = _get_api_key()
    if not api_key:
        return None
    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        prompt = (
            f"Analyse la phrase suivante et retourne UNIQUEMENT un objet JSON avec l'action et les paramètres nécessaires.\n"
            f"Actions possibles : list, create_file, create_folder, delete, move, copy, rename, read, write, find, largest, disk_usage, organize_desktop, info, open.\n"
            f"Phrase : \"{description}\"\n"
            f"Exemple : pour \"crée un fichier test.txt dans documents\" → {{\"action\":\"create_file\",\"name\":\"test.txt\",\"path\":\"documents\"}}\n"
            f"Pour \"montre le bureau\" → {{\"action\":\"list\",\"path\":\"desktop\"}}\n"
            f"Réponds uniquement avec le JSON."
        )
        resp = client.models.generate_content(model=FAST_MODEL, contents=prompt)
        json_match = re.search(r'\{.*\}', resp.text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group(0))
    except Exception as e:
        print(f"[file_controller] AI error: {e}")
    return None


# ════════════════════════════════════════════════════════════════════════════
# Point d'entrée principal
# ════════════════════════════════════════════════════════════════════════════

_ACTION_ALIASES = {
    "ls": "list", "afficher": "list", "montre": "list",
    "supprime": "delete", "efface": "delete",
    "deplace": "move", "déplace": "move", "bouge": "move",
    "copie": "copy", "duplique": "copy",
    "renommer": "rename", "ouvre": "open",
    "créer_fichier": "create_file", "creer_fichier": "create_file",
    "créer_dossier": "create_folder", "creer_dossier": "create_folder",
}


@kit.action("file_controller")
def file_controller(parameters: dict = None, response=None, player=None,
                    session_memory=None) -> str:
    """
    Gestionnaire de fichiers robuste.
    Accepte une action explicite ou une description en langage naturel.
    """
    params = parameters or {}
    action = str(params.get("action", "") or "").lower().strip()
    description = str(params.get("description", "") or "").strip()
    path = params.get("path", "desktop")
    name = params.get("name", "")

    # Interprétation d'une phrase naturelle si aucune action explicite
    if description and not action:
        local = _parse_file_command_locally(description)
        if local:
            action = local.pop("action", "")
            for k, v in local.items():
                if k not in params or params[k] is None:
                    params[k] = v
        else:
            ai = _detect_file_action_via_ai(description)
            if ai:
                action = ai.pop("action", "")
                for k, v in ai.items():
                    if k not in params or params[k] is None:
                        params[k] = v
            else:
                return "Je n'ai pas compris cette commande concernant les fichiers. Pouvez-vous reformuler ?"

    action = _ACTION_ALIASES.get(action, action)
    name = params.get("name", "")
    path = params.get("path") or ("home" if action == "find" else "desktop")
    if player:
        try:
            player.write_log(f"[file] {action or ''} {name or path or ''}")
        except Exception:
            pass

    try:
        if action == "list":
            return list_files(path, show_hidden=bool(params.get("show_hidden", False)))
        elif action == "create_file":
            return create_file(path, name=name, content=params.get("content", ""))
        elif action == "create_folder":
            return create_folder(path, name=name)
        elif action == "delete":
            return delete_file(path, name=name)
        elif action == "move":
            return move_file(path, name=name, destination=params.get("destination", ""))
        elif action == "copy":
            return copy_file(path, name=name, destination=params.get("destination", ""))
        elif action == "rename":
            return rename_file(path, name=name, new_name=params.get("new_name", ""))
        elif action == "read":
            return read_file(path, name=name)
        elif action == "write":
            return write_file(path, name=name, content=params.get("content", ""),
                              append=bool(params.get("append", False)))
        elif action == "open":
            return open_path(path, name=name)
        elif action == "find":
            return find_files(
                name=name or params.get("name", ""),
                extension=params.get("extension", ""),
                path=path,
                max_results=min(int(params.get("max_results", 20) or 20), 50),
            )
        elif action in {"search_content", "fts", "contenu"}:
            return search_content(
                query=name or params.get("query", "") or params.get("description", ""),
                max_results=min(int(params.get("max_results", 15) or 15), 50),
            )
        elif action in {"index_status", "index_scan", "scan_index"}:
            from core.file_indexer import get_file_indexer
            indexer = get_file_indexer()
            stats = indexer.scan_directory_incremental()
            total = indexer.count_indexed_files()
            return f"Index personnel mis à jour : {stats.get('indexed', 0)} fichiers modifiés indexés. Total dans l'index : {total} fichiers."
        elif action == "largest":
            return get_largest_files(path, count=int(params.get("count", 10) or 10))
        elif action == "disk_usage":
            return get_disk_usage(path)
        elif action == "organize_desktop":
            return organize_desktop()
        elif action == "info":
            return get_file_info(path, name=name)
        else:
            return f"Action inconnue : '{action}'"
    except Exception as e:
        return f"Erreur du gestionnaire de fichiers ({action}) : {e}"


# ════════════════════════════════════════════════════════════════════════════
# Test direct
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        print(file_controller({"description": " ".join(sys.argv[1:])}))
    else:
        print("Usage: python file_controller.py \"<commande naturelle>\"")
