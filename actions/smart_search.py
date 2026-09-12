"""
smart_search.py — Recherche floue de fichiers ultra‑rapide et robuste.
Utilisé par file_controller.py et shell_exec.py pour trouver des fichiers
par nom approximatif (tolérant aux fautes de frappe, accents, majuscules).
Fonctionne sous Linux/macOS/Windows, avec :
- arrêt propre si trop de fichiers analysés
- gestion des symlinks, permissions, profondeur
- filtrage par extension(s)
- scoring amélioré (tokens, sous‑chaîne, dossier parent)
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import unicodedata
from pathlib import Path
from typing import List, Tuple, Iterable, Union, Optional, Set, Dict

from core.media_search import intelligent_score, readable_media_name


def _plocate_candidates(
    query: str,
    paths: List[Path],
    ext_set: Set[str],
    include_hidden: bool,
    max_results: int,
) -> List[Path]:
    """Pré-sélection instantanée via l'index disque, si disponible."""
    if not query or not shutil.which("plocate"):
        return []
    terms = [token for token in get_tokens(query) if len(token) >= 3]
    if not terms:
        return []
    candidates: Dict[Path, None] = {}
    for term in terms[:3]:
        try:
            process = subprocess.run(
                ["plocate", "-i", "--existing", "--limit", "800", term],
                capture_output=True, text=True, timeout=2,
            )
        except Exception:
            continue
        for raw in (process.stdout or "").splitlines():
            candidate = Path(raw)
            try:
                if not candidate.is_file():
                    continue
                resolved = candidate.resolve()
                relative = next(
                    (resolved.relative_to(root.resolve()) for root in paths
                     if resolved.is_relative_to(root.resolve())),
                    None,
                )
                if relative is None:
                    continue
                if not include_hidden and any(part.startswith(".") for part in relative.parts):
                    continue
                if ext_set and resolved.suffix.lower() not in ext_set:
                    continue
                candidates[resolved] = None
                if len(candidates) >= max(max_results * 20, 100):
                    break
            except OSError:
                continue
    return list(candidates)


def _fd_candidates(
    query: str,
    paths: List[Path],
    ext_set: Set[str],
    include_hidden: bool,
    max_results: int,
) -> List[Path]:
    """Second chemin rapide quand l'index plocate est absent ou périmé.

    ``fd`` parcourt les répertoires en Rust, en parallèle, respecte les
    exclusions usuelles et est borné par un délai court. Il sert aux
    correspondances exactes/sous-chaînes ; le parcours Python ne reste que
    pour la recherche phonétique et floue.
    """
    binary = shutil.which("fd") or shutil.which("fdfind")
    if not query or not binary:
        return []
    candidates: Dict[Path, None] = {}
    for root in paths:
        command = [binary, "--type", "f", "--ignore-case", "--fixed-strings"]
        if include_hidden:
            command.append("--hidden")
        for ext in sorted(ext_set):
            command.extend(["--extension", ext.lstrip(".")])
        command.extend([query, str(root)])
        try:
            process = subprocess.run(
                command, capture_output=True, text=True, timeout=3,
            )
        except Exception:
            continue
        for raw in (process.stdout or "").splitlines():
            candidate = Path(raw)
            try:
                if candidate.is_file():
                    candidates[candidate.resolve()] = None
            except OSError:
                continue
            if len(candidates) >= max(max_results * 20, 100):
                break
        if len(candidates) >= max(max_results * 20, 100):
            break
    return list(candidates)

# ── Normalisation ────────────────────────────────────────────────────────────
def normalize_string(s: str) -> str:
    """Minuscules, sans accents, uniquement caractères alphanumériques."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s).encode("ASCII", "ignore").decode("utf-8")
    s = s.lower()
    return re.sub(r"[^a-z0-9]", "", s)


def get_tokens(s: str) -> List[str]:
    """Découpe en tokens alphanumériques minuscules."""
    if not s:
        return []
    s = unicodedata.normalize("NFKD", s).encode("ASCII", "ignore").decode("utf-8")
    s = s.lower()
    return [w for w in re.split(r"[^a-z0-9]", s) if w]


# ── Distance de Levenshtein ─────────────────────────────────────────────────
def lev_dist(s1: str, s2: str) -> int:
    """Distance d'édition standard."""
    if len(s1) < len(s2):
        return lev_dist(s2, s1)
    if len(s2) == 0:
        return len(s1)
    prev = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        curr = [i + 1]
        for j, c2 in enumerate(s2):
            insert = prev[j + 1] + 1
            delete = curr[j] + 1
            subst = prev[j] + (0 if c1 == c2 else 1)
            curr.append(min(insert, delete, subst))
        prev = curr
    return prev[-1]


def token_similarity(t1: str, t2: str) -> float:
    """Similarité entre deux tokens (0.0 → 1.0)."""
    if not t1 or not t2:
        return 0.0
    if t1 == t2:
        return 1.0
    if t1 in t2 or t2 in t1:
        return 0.95 * (min(len(t1), len(t2)) / max(len(t1), len(t2)))
    max_l = max(len(t1), len(t2))
    if max_l == 0:
        return 0.0
    return 1.0 - (lev_dist(t1, t2) / max_l)


def match_score(query: str, target: str, parent: str = "") -> float:
    """
    Score global (0.0 → 1.0) entre une requête et un nom de fichier.
    `parent` (nom du dossier parent) peut ajouter un petit bonus.
    """
    target = readable_media_name(target)
    q_norm = normalize_string(query)
    t_norm = normalize_string(target)
    if not q_norm or not t_norm:
        return 0.0
    if q_norm == t_norm:
        return 1.0

    # Sous-chaîne
    sub_score = 0.0
    if q_norm in t_norm:
        sub_score = 0.9 * (len(q_norm) / len(t_norm))

    # Tokens
    q_tokens = get_tokens(query)
    t_tokens = get_tokens(target)
    token_score = 0.0
    if q_tokens and t_tokens:
        total = 0.0
        exact_matches = 0
        for qt in q_tokens:
            best = 0.0
            for tt in t_tokens:
                s = token_similarity(qt, tt)
                if s > best:
                    best = s
            if best >= 0.70:
                total += best
                if best >= 0.99:
                    exact_matches += 1
        token_score = total / len(q_tokens)
        if exact_matches == len(q_tokens):
            token_score = 1.0

    # Levenshtein global
    max_len = max(len(q_norm), len(t_norm))
    lev_score = 0.0
    if max_len:
        lev_score = 1.0 - (lev_dist(q_norm, t_norm) / max_len)

    # Bonus dossier parent
    parent_bonus = 0.0
    if parent:
        p_norm = normalize_string(parent)
        if p_norm and q_norm in p_norm:
            parent_bonus = 0.05

    voice_score = intelligent_score(query, f"{target} {parent}".strip())
    score = max(sub_score, token_score * 0.95, lev_score * 0.75,
                voice_score) + parent_bonus
    return min(1.0, score)


# ── Extension(s) ────────────────────────────────────────────────────────────
def _normalize_ext(extension: str) -> Set[str]:
    """Accepte '.jpg', 'jpg', ou 'jpg,png' et renvoie un set d'extensions."""
    if not extension:
        return set()
    exts: Set[str] = set()
    for part in re.split(r"[,;\s]+", extension.strip().lower()):
        if not part:
            continue
        if not part.startswith("."):
            part = "." + part
        exts.add(part)
    return exts


# ── Recherche principale ────────────────────────────────────────────────────
def smart_search_files(
    query: str,
    search_paths: Union[Path, str, Iterable[Union[Path, str]]],
    extension: str = "",
    max_results: int = 20,
    min_score: float = 0.45,
    include_hidden: bool = False,
    follow_symlinks: bool = False,
    max_depth: Optional[int] = None,
    max_scan: int = 200_000,
    max_duration_s: float = 4.0,
) -> List[Tuple[float, Path]]:
    """
    Recherche récursive floue de fichiers.
    Retourne une liste de tuples (score, Path) triés par score décroissant.
    """
    # Normaliser search_paths en liste de Path existants
    if isinstance(search_paths, (str, Path)):
        search_paths = [search_paths]
    paths: List[Path] = []
    for p in search_paths:
        try:
            pp = Path(p).expanduser()
            if pp.exists():
                paths.append(pp)
        except Exception:
            continue
    if not paths:
        return []

    ext_set = _normalize_ext(extension)
    ignored = {
        "node_modules", "venv", ".venv", "__pycache__", "build", "dist",
        ".git", ".cache", ".mozilla", ".config", ".local", ".vscode",
        ".npm", ".cargo", ".rustup", ".gradle", ".m2", "site-packages",
        ".tox", ".mypy_cache", ".pytest_cache", ".idea", "Trash",
    }

    results: List[Tuple[float, Path]] = []
    scanned = 0
    stop = False
    deadline = time.monotonic() + max(0.25, float(max_duration_s))

    def consider(file_path: Path) -> None:
        nonlocal scanned, stop
        if stop:
            return
        # Compter tout fichier examiné, pas seulement une correspondance.
        # L'ancien compteur ne progressait jamais lors d'une recherche sans
        # résultat, ce qui transformait le dernier recours en scan illimité.
        scanned += 1
        if scanned > max_scan or time.monotonic() >= deadline:
            stop = True
            return
        try:
            if not follow_symlinks and file_path.is_symlink():
                return
            if not file_path.is_file():
                return
        except OSError:
            return
        if ext_set and file_path.suffix.lower() not in ext_set:
            return
        if query:
            # Essayer le stem puis le nom complet (utile si la requête contient l'extension)
            score = match_score(query, file_path.stem, parent=file_path.parent.name)
            if score < min_score:
                score2 = match_score(query, file_path.name, parent=file_path.parent.name)
                if score2 > score:
                    score = score2
            if score < min_score:
                return
            results.append((score, file_path))
        else:
            results.append((1.0, file_path))

    # L'index plocate évite un os.walk de tout le disque dans le cas courant.
    indexed = _plocate_candidates(
        query, paths, ext_set, include_hidden, max_results
    )
    for candidate in indexed:
        consider(candidate)
    if results:
        unique_indexed: Dict[Path, Tuple[float, Path]] = {}
        for score, candidate in results:
            unique_indexed[candidate] = (score, candidate)
        return sorted(
            unique_indexed.values(), key=lambda item: (-item[0], str(item[1]).lower())
        )[:max_results]

    direct = _fd_candidates(query, paths, ext_set, include_hidden, max_results)
    for candidate in direct:
        consider(candidate)
    if results:
        unique_direct = {candidate.resolve(): (score, candidate)
                         for score, candidate in results}
        return sorted(
            unique_direct.values(), key=lambda item: (-item[0], str(item[1]).lower())
        )[:max_results]

    for base in paths:
        if stop:
            break
        try:
            if base.is_file():
                consider(base)
                continue
            if not base.is_dir():
                continue
        except OSError:
            continue

        for root, dirs, files in os.walk(base, topdown=True, followlinks=follow_symlinks):
            if stop:
                break
            # Limite de profondeur
            if max_depth is not None:
                try:
                    depth = len(Path(root).relative_to(base).parts)
                except ValueError:
                    depth = 0
                if depth > max_depth:
                    dirs[:] = []
                    continue
            # Prune des dossiers
            if not include_hidden:
                dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ignored]
            else:
                dirs[:] = [d for d in dirs if d not in ignored]
            # Éviter les dossiers symlinkés si on ne les suit pas
            if not follow_symlinks:
                dirs[:] = [d for d in dirs if not (Path(root) / d).is_symlink()]

            for fname in files:
                if stop:
                    break
                if not include_hidden and fname.startswith("."):
                    continue
                consider(Path(root) / fname)

    # Dédoublonner par chemin résolu, garder le meilleur score
    unique: Dict[Path, Tuple[float, Path]] = {}
    for score, p in results:
        try:
            key = p.resolve()
        except OSError:
            key = p
        if key not in unique or score > unique[key][0]:
            unique[key] = (score, p)

    ranked = sorted(unique.values(), key=lambda x: (-x[0], str(x[1]).lower()))
    return ranked[:max_results]


# ── Test direct ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        q = sys.argv[1]
        hits = smart_search_files(q, [Path.home()], max_results=10)
        if not hits:
            print(f"Aucun fichier trouvé pour '{q}'.")
        else:
            for score, path in hits:
                print(f"{int(score*100):3d}%  {path}")
    else:
        print("Usage: python smart_search.py <requête>")
