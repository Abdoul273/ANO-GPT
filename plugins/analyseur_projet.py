import os
import re
import stat
import subprocess
from datetime import datetime


PLUGIN = {
    "name": "analyseur_projet",
    "description": (
        "Analyse un dossier de projet local en lecture seule. "
        "Utilise cet outil lorsque l'utilisateur demande un résumé du projet "
        "(nombre de fichiers, dossiers, taille totale, langages détectés, fichiers volumineux, "
        "fichiers récemment modifiés, présence d'un dépôt Git), "
        "la détection des commentaires TODO/FIXME/HACK/XXX, "
        "une recherche de texte dans les fichiers du projet, "
        "ou des informations Git (branche actuelle, statut, derniers commits). "
        "Actions disponibles : 'resume' pour le résumé, 'todo' pour les tâches, "
        "'rechercher' pour chercher du texte (paramètre recherche obligatoire), "
        "et 'git' pour l'état Git. "
        "Le paramètre chemin doit être un chemin absolu. "
        "Ce plugin ne modifie aucun fichier."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "Action à exécuter. Valeurs possibles : resume, todo, rechercher, git. "
                    "Si l'action est absente, 'resume' est utilisé par défaut."
                ),
                "enum": ["resume", "todo", "rechercher", "git"],
                "default": "resume",
            },
            "chemin": {
                "type": "STRING",
                "description": "Chemin absolu du dossier à analyser. Obligatoire.",
            },
            "recherche": {
                "type": "STRING",
                "description": (
                    "Texte à rechercher dans les fichiers. "
                    "Obligatoire uniquement lorsque action='rechercher'."
                ),
            },
            "max_resultats": {
                "type": "INTEGER",
                "description": (
                    "Nombre maximal de résultats affichés. "
                    "Valeur par défaut : 30. Minimum forcé : 1. Maximum forcé : 100."
                ),
                "default": 30,
                "minimum": 1,
                "maximum": 100,
            },
            "inclure_caches": {
                "type": "BOOLEAN",
                "description": (
                    "Si vrai, inclut les fichiers et dossiers cachés, "
                    "sauf dossiers ignorés et fichiers sensibles. "
                    "Valeur par défaut : false."
                ),
                "default": False,
            },
        },
        "required": ["chemin"],
    },
}


ALLOWED_ACTIONS = {"resume", "todo", "rechercher", "git"}

MAX_FILES = 5000
MAX_FILE_SIZE = 2 * 1024 * 1024
MAX_RESPONSE_CHARS = 12000
MAX_EXTRACT_CHARS = 160
GIT_TIMEOUT = 8
DEFAULT_MAX_RESULTS = 30
MAX_MAX_RESULTS = 100

IGNORED_DIR_NAMES = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
    "target",
    ".cache",
    ".idea",
    ".vscode",
}

SENSITIVE_SUBSTRINGS = (
    "credential",
    "credentials",
    "secret",
    "secrets",
    "password",
)

SENSITIVE_EXTENSIONS = (
    ".pem",
    ".key",
    ".p12",
    ".pfx",
)

SPECIAL_FILE_LANGUAGE = {
    "dockerfile": "Docker",
    "makefile": "Makefile",
    "gnumakefile": "Makefile",
    "cmakelists.txt": "CMake",
    "gemfile": "Ruby",
    "rakefile": "Ruby",
    "procfile": "Procfile",
    "justfile": "Just",
}

LANGUAGE_MAP = {
    ".py": "Python",
    ".pyw": "Python",
    ".pyi": "Python",
    ".js": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".jsx": "JavaScript JSX",
    ".ts": "TypeScript",
    ".tsx": "TypeScript TSX",
    ".html": "HTML",
    ".htm": "HTML",
    ".xhtml": "HTML",
    ".css": "CSS",
    ".scss": "SCSS",
    ".sass": "Sass",
    ".less": "Less",
    ".json": "JSON",
    ".jsonc": "JSON",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".toml": "TOML",
    ".ini": "INI",
    ".cfg": "Configuration",
    ".conf": "Configuration",
    ".xml": "XML",
    ".svg": "SVG",
    ".md": "Markdown",
    ".markdown": "Markdown",
    ".rst": "reStructuredText",
    ".txt": "Texte",
    ".sh": "Shell",
    ".bash": "Shell",
    ".zsh": "Shell",
    ".ksh": "Shell",
    ".fish": "Shell",
    ".ps1": "PowerShell",
    ".psm1": "PowerShell",
    ".psd1": "PowerShell",
    ".bat": "Batch",
    ".cmd": "Batch",
    ".c": "C",
    ".h": "En-tête C/C++",
    ".cpp": "C++",
    ".cc": "C++",
    ".cxx": "C++",
    ".hpp": "En-tête C++",
    ".hxx": "En-tête C++",
    ".cs": "C#",
    ".csproj": "Projet C#",
    ".sln": "Solution Visual Studio",
    ".java": "Java",
    ".kt": "Kotlin",
    ".kts": "Kotlin",
    ".swift": "Swift",
    ".go": "Go",
    ".rs": "Rust",
    ".rb": "Ruby",
    ".php": "PHP",
    ".pl": "Perl",
    ".pm": "Perl",
    ".lua": "Lua",
    ".r": "R",
    ".sql": "SQL",
    ".dart": "Dart",
    ".vue": "Vue",
    ".svelte": "Svelte",
    ".proto": "Protocol Buffers",
    ".graphql": "GraphQL",
    ".gql": "GraphQL",
    ".tf": "Terraform",
    ".gradle": "Gradle",
    ".ipynb": "Jupyter Notebook",
}


def _normalize_bool(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ("1", "true", "oui", "yes", "y", "on")


def _normalize_max_resultats(value):
    try:
        result = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_RESULTS

    if result < 1:
        return 1
    if result > MAX_MAX_RESULTS:
        return MAX_MAX_RESULTS
    return result


def _format_size(size):
    size = float(size)
    for unit in ("o", "Ko", "Mo", "Go", "To"):
        if size < 1024.0 or unit == "To":
            return f"{size:.1f} {unit}".replace(".", ",")
        size /= 1024.0
    return f"{size:.1f} o".replace(".", ",")


def _truncate(value, max_length):
    text = str(value).replace("\x00", " ")
    text = " ".join(text.split())

    if len(text) <= max_length:
        return text

    if max_length <= 3:
        return text[:max_length]

    return text[: max_length - 3].rstrip() + "..."


def _limit_response(text):
    text = str(text)

    if len(text) <= MAX_RESPONSE_CHARS:
        return text

    marker = "\n\n[Réponse tronquée pour respecter la limite de 12000 caractères.]"
    keep = MAX_RESPONSE_CHARS - len(marker)

    if keep <= 0:
        return text[:MAX_RESPONSE_CHARS]

    return text[:keep].rstrip() + marker


def _relative_path(path, root):
    try:
        return os.path.relpath(path, root)
    except ValueError:
        return path


def _display_path(path, root):
    return _truncate(_relative_path(path, root), 180)


def _format_timestamp(timestamp):
    try:
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
    except (OSError, ValueError, OverflowError):
        return "date inconnue"


def _is_sensitive_filename(name):
    lower = str(name).lower()

    if lower == ".env" or lower.startswith(".env.") or lower.endswith(".env") or ".env." in lower:
        return True

    for token in SENSITIVE_SUBSTRINGS:
        if token in lower:
            return True

    for extension in SENSITIVE_EXTENSIONS:
        if lower.endswith(extension):
            return True

    if "id_rsa" in lower or "id_ed25519" in lower:
        return True

    return False


def _language_for_filename(name):
    base = os.path.basename(str(name)).lower()

    if base in SPECIAL_FILE_LANGUAGE:
        return SPECIAL_FILE_LANGUAGE[base]

    extension = os.path.splitext(base)[1]
    if not extension:
        return None

    return LANGUAGE_MAP.get(extension)


def _collect_project_files(root, include_hidden=False, include_sensitive=False, max_files=MAX_FILES):
    files = []
    file_count = 0
    folder_count = 1
    total_size = 0
    truncated = False

    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        kept_dirs = []

        for dirname in dirnames:
            if dirname in IGNORED_DIR_NAMES:
                continue

            full_dir = os.path.join(dirpath, dirname)

            try:
                if os.path.islink(full_dir):
                    continue
            except OSError:
                continue

            if not include_hidden and dirname.startswith("."):
                continue

            kept_dirs.append(dirname)

        dirnames[:] = kept_dirs
        folder_count += len(kept_dirs)

        for filename in filenames:
            if file_count >= max_files:
                truncated = True
                break

            if filename in IGNORED_DIR_NAMES:
                continue

            if not include_hidden and filename.startswith("."):
                continue

            full_path = os.path.join(dirpath, filename)

            try:
                if os.path.islink(full_path):
                    continue

                st = os.lstat(full_path)
                if not stat.S_ISREG(st.st_mode):
                    continue
            except OSError:
                continue

            sensitive = _is_sensitive_filename(filename)
            if sensitive and not include_sensitive:
                continue

            size = int(getattr(st, "st_size", 0))
            mtime = float(getattr(st, "st_mtime", 0.0))

            files.append(
                {
                    "path": full_path,
                    "name": filename,
                    "size": size,
                    "mtime": mtime,
                    "sensitive": sensitive,
                }
            )

            file_count += 1
            total_size += size

            if file_count >= max_files:
                truncated = True
                break

        if truncated:
            break

    return files, file_count, folder_count, total_size, truncated


def _is_binary_data(data):
    if not data:
        return False

    sample = data[:8192]

    if b"\0" in sample:
        return True

    allowed_control = {8, 9, 10, 12, 13, 27}
    bad = 0

    for byte in sample:
        if byte < 32 and byte not in allowed_control:
            bad += 1
        elif byte == 127:
            bad += 1

    return (bad / len(sample)) > 0.10


def _read_text_file(path):
    try:
        if os.path.getsize(path) > MAX_FILE_SIZE:
            return None
    except OSError:
        return None

    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_FILE_SIZE + 1)
    except (OSError, MemoryError):
        return None

    if len(data) > MAX_FILE_SIZE:
        return None

    if _is_binary_data(data):
        return None

    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _search_todo(files, root, max_results):
    results = []
    scanned = 0
    pattern = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b")

    for entry in files:
        if len(results) >= max_results:
            break

        if entry.get("sensitive"):
            continue

        text = _read_text_file(entry["path"])
        if text is None:
            continue

        scanned += 1

        for lineno, line in enumerate(text.splitlines(), start=1):
            match = pattern.search(line)
            if match:
                results.append((entry["path"], lineno, match.group(0), line))

                if len(results) >= max_results:
                    break

    return results, scanned


def _search_text(files, needle, max_results):
    results = []
    scanned = 0
    needle_lower = str(needle).lower()

    for entry in files:
        if len(results) >= max_results:
            break

        if entry.get("sensitive"):
            continue

        text = _read_text_file(entry["path"])
        if text is None:
            continue

        scanned += 1

        for lineno, line in enumerate(text.splitlines(), start=1):
            if needle_lower in line.lower():
                results.append((entry["path"], lineno, line))

                if len(results) >= max_results:
                    break

    return results, scanned


def _format_todo(root, results, scanned, max_results, analysis_truncated):
    lines = []
    lines.append("Analyse des tâches (TODO, FIXME, HACK, XXX)")
    lines.append(f"Dossier : {_truncate(root, 200)}")
    lines.append(f"Fichiers texte lus : {scanned}")
    lines.append(f"Résultats affichés : {len(results)} (limite : {max_results})")

    if analysis_truncated:
        lines.append(f"Analyse limitée à {MAX_FILES} fichiers.")

    if not results:
        lines.append("Aucune tâche trouvée.")
        return "\n".join(lines)

    lines.append("Détails :")

    for path, lineno, marker, line in results:
        lines.append(
            f"- {_display_path(path, root)}:{lineno} [{marker}] "
            f"{_truncate(line, MAX_EXTRACT_CHARS)}"
        )

    if len(results) >= max_results:
        lines.append(
            f"Limite de {max_results} résultats atteinte ; "
            "d'autres correspondances peuvent exister."
        )

    return "\n".join(lines)


def _format_search(root, needle, results, scanned, max_results, analysis_truncated):
    lines = []
    lines.append("Recherche de texte")
    lines.append(f"Dossier : {_truncate(root, 200)}")
    lines.append(f"Texte recherché : {_truncate(needle, 200)}")
    lines.append(f"Fichiers texte lus : {scanned}")
    lines.append(f"Résultats affichés : {len(results)} (limite : {max_results})")

    if analysis_truncated:
        lines.append(f"Analyse limitée à {MAX_FILES} fichiers.")

    if not results:
        lines.append("Aucun résultat.")
        return "\n".join(lines)

    lines.append("Détails :")

    for path, lineno, line in results:
        lines.append(
            f"- {_display_path(path, root)}:{lineno} : "
            f"{_truncate(line, MAX_EXTRACT_CHARS)}"
        )

    if len(results) >= max_results:
        lines.append(
            f"Limite de {max_results} résultats atteinte ; "
            "d'autres correspondances peuvent exister."
        )

    return "\n".join(lines)


def _format_resume(root, files, file_count, folder_count, total_size, truncated, include_hidden):
    languages = {}
    other_files = 0

    for entry in files:
        language = _language_for_filename(entry["name"])
        if language is None:
            other_files += 1
        else:
            languages[language] = languages.get(language, 0) + 1

    git_path = os.path.join(root, ".git")

    try:
        is_git = os.path.isdir(git_path) or os.path.isfile(git_path)
    except OSError:
        is_git = False

    lines = []
    lines.append("Résumé du projet")
    lines.append(f"Dossier : {_truncate(root, 200)}")
    lines.append(f"Fichiers : {file_count}")
    lines.append(f"Dossiers : {folder_count} (racine incluse)")
    lines.append(f"Taille totale : {_format_size(total_size)}")
    lines.append(f"Dépôt Git : {'Oui' if is_git else 'Non'}")
    lines.append(f"Fichiers/dossiers cachés inclus : {'Oui' if include_hidden else 'Non'}")

    if truncated:
        lines.append(f"Analyse limitée à {MAX_FILES} fichiers.")

    lines.append("")
    lines.append("Langages détectés :")

    if languages:
        sorted_languages = sorted(languages.items(), key=lambda item: item[1], reverse=True)

        for language, count in sorted_languages[:15]:
            lines.append(f"- {language} : {count} fichier(s)")

        if len(sorted_languages) > 15:
            lines.append(f"- … et {len(sorted_languages) - 15} autre(s) langage(s)")
    else:
        lines.append("- Aucun langage reconnu.")

    if other_files:
        lines.append(f"- Autres/sans extension reconnue : {other_files} fichier(s)")

    display_files = [entry for entry in files if not entry["sensitive"]]

    lines.append("")
    lines.append("Fichiers les plus volumineux :")

    top_size = sorted(display_files, key=lambda entry: entry["size"], reverse=True)[:5]

    if top_size:
        for entry in top_size:
            lines.append(
                f"- {_display_path(entry['path'], root)} : {_format_size(entry['size'])}"
            )
    else:
        lines.append("- Aucun fichier non sensible à afficher.")

    lines.append("")
    lines.append("Fichiers récemment modifiés :")

    top_recent = sorted(display_files, key=lambda entry: entry["mtime"], reverse=True)[:5]

    if top_recent:
        for entry in top_recent:
            lines.append(
                f"- {_display_path(entry['path'], root)} : {_format_timestamp(entry['mtime'])}"
            )
    else:
        lines.append("- Aucun fichier non sensible à afficher.")

    lines.append("")
    lines.append("Mode : lecture seule. Les fichiers sensibles ne sont pas lus.")

    return "\n".join(lines)


def _execute_git(args, cwd):
    try:
        completed = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT,
            check=False,
        )
        return completed.returncode == 0, completed.stdout or "", completed.stderr or "", None
    except FileNotFoundError:
        return False, "", "", "git_absent"
    except subprocess.TimeoutExpired:
        return False, "", "", "timeout"
    except Exception:
        return False, "", "", "erreur"


def _summarize_git_status(porcelain):
    total = 0
    untracked = 0
    staged = 0
    unstaged = 0

    for raw in porcelain.splitlines():
        if not raw.strip():
            continue

        total += 1

        if raw.startswith("??"):
            untracked += 1
            continue

        if len(raw) >= 2:
            x, y = raw[0], raw[1]

            if x != " ":
                staged += 1

            if y != " ":
                unstaged += 1

    parts = [f"{total} élément(s) au total"]

    if untracked:
        parts.append(f"{untracked} non suivi(s)")

    if staged:
        parts.append(f"{staged} indexé(s)")

    if unstaged:
        parts.append(f"{unstaged} non indexé(s)")

    return ", ".join(parts)


def _action_git(root):
    git_marker = os.path.join(root, ".git")

    try:
        has_git_marker = os.path.isdir(git_marker) or os.path.isfile(git_marker)
    except OSError:
        has_git_marker = False

    if not has_git_marker:
        return (
            "Le dossier indiqué ne semble pas être un dépôt Git "
            "(dossier ou fichier .git introuvable)."
        )

    ok, out, err, info = _execute_git(["rev-parse", "--is-inside-work-tree"], root)

    if info == "git_absent":
        return "Git n'est pas installé ou la commande git est introuvable."

    if info == "timeout":
        return "La commande Git a dépassé le délai maximal de 8 secondes."

    if info is not None:
        return "Impossible d'exécuter Git."

    if not ok or out.strip().lower() != "true":
        return "Le dossier indiqué n'est pas un dépôt Git accessible par Git."

    branch = None

    ok, out, err, info = _execute_git(["rev-parse", "--abbrev-ref", "HEAD"], root)

    if info == "git_absent":
        return "Git n'est pas installé ou la commande git est introuvable."

    if info == "timeout":
        return "La commande Git a dépassé le délai maximal de 8 secondes."

    if info is None and ok and out.strip():
        branch = out.strip()
    else:
        ok2, out2, err2, info2 = _execute_git(["symbolic-ref", "--short", "HEAD"], root)

        if info2 == "git_absent":
            return "Git n'est pas installé ou la commande git est introuvable."

        if info2 == "timeout":
            return "La commande Git a dépassé le délai maximal de 8 secondes."

        if info2 is None and ok2 and out2.strip():
            branch = out2.strip()
        else:
            branch = "inconnue"

    lines = []
    lines.append(f"Branche actuelle : {branch}")

    ok, out, err, info = _execute_git(["status", "--porcelain"], root)

    if info == "git_absent":
        return "Git n'est pas installé ou la commande git est introuvable."

    if info == "timeout":
        return "La commande Git a dépassé le délai maximal de 8 secondes."

    if info is not None or not ok:
        lines.append("Statut Git : indisponible.")
    else:
        status_lines = [line for line in out.splitlines() if line.strip()]

        if not status_lines:
            lines.append("Statut Git : arbre de travail propre.")
        else:
            lines.append(f"Statut Git : {_summarize_git_status(out)}")

            for line in status_lines[:15]:
                lines.append("  " + _truncate(line, 160))

            if len(status_lines) > 15:
                lines.append(f"  … {len(status_lines) - 15} autre(s) élément(s)")

    ok, out, err, info = _execute_git(
        ["log", "-n", "5", "--pretty=format:%h - %ad - %an : %s", "--date=short"],
        root,
    )

    if info == "timeout":
        lines.append("Cinq derniers commits : délai dépassé.")
    elif info is not None:
        lines.append("Cinq derniers commits : indisponibles.")
    elif ok and out.strip():
        lines.append("Cinq derniers commits :")
        for line in out.splitlines()[:5]:
            lines.append("- " + _truncate(line, 180))
    else:
        lines.append("Cinq derniers commits : aucun commit trouvé.")

    return "\n".join(lines)


def run(parameters: dict, player=None, session_memory=None) -> str:
    try:
        if not isinstance(parameters, dict):
            parameters = {}

        raw_path = parameters.get("chemin")

        if raw_path is None or not str(raw_path).strip():
            return _limit_response(
                "Erreur : le paramètre 'chemin' est obligatoire. "
                "Fournissez un chemin absolu de dossier."
            )

        expanded = os.path.expanduser(str(raw_path).strip())

        if not os.path.isabs(expanded):
            return _limit_response(
                "Erreur : le paramètre 'chemin' doit être un chemin absolu."
            )

        root = os.path.abspath(expanded)

        if not os.path.isdir(root):
            return _limit_response(
                f"Erreur : le chemin '{_truncate(root, 300)}' n'est pas un dossier valide."
            )

        if not os.access(root, os.R_OK | os.X_OK):
            return _limit_response(
                "Erreur : le dossier n'est pas accessible en lecture."
            )

        action_value = parameters.get("action", "resume")
        action = str(action_value).strip().lower() if action_value is not None else "resume"

        if not action:
            action = "resume"

        include_hidden = _normalize_bool(parameters.get("inclure_caches", False))
        max_resultats = _normalize_max_resultats(parameters.get("max_resultats", DEFAULT_MAX_RESULTS))

        if action == "resume":
            files, file_count, folder_count, total_size, truncated = _collect_project_files(
                root,
                include_hidden=include_hidden,
                include_sensitive=True,
            )
            result = _format_resume(
                root,
                files,
                file_count,
                folder_count,
                total_size,
                truncated,
                include_hidden,
            )

        elif action == "todo":
            files, file_count, folder_count, total_size, truncated = _collect_project_files(
                root,
                include_hidden=include_hidden,
                include_sensitive=False,
            )
            results, scanned = _search_todo(files, root, max_resultats)
            result = _format_todo(root, results, scanned, max_resultats, truncated)

        elif action == "rechercher":
            raw_search = parameters.get("recherche")

            if raw_search is None or not str(raw_search).strip():
                return _limit_response(
                    "Erreur : pour l'action 'rechercher', le paramètre 'recherche' est obligatoire."
                )

            needle = str(raw_search).strip()

            files, file_count, folder_count, total_size, truncated = _collect_project_files(
                root,
                include_hidden=include_hidden,
                include_sensitive=False,
            )
            results, scanned = _search_text(files, needle, max_resultats)
            result = _format_search(root, needle, results, scanned, max_resultats, truncated)

        elif action == "git":
            result = _action_git(root)

        else:
            result = (
                f"Action inconnue : '{_truncate(action, 80)}'.\n"
                "Actions valides : resume, todo, rechercher, git.\n"
                "Si aucune action n'est précisée, 'resume' est utilisé par défaut."
            )

        return _limit_response(result)

    except Exception as exc:
        return _limit_response(
            f"Erreur interne du plugin analyseur_projet : {exc}"
        )
