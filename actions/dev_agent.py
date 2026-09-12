# dev_agent.py — Agent de développement ultra‑réaliste
# Parsing local avancé, planification intelligente, itérations de correction.
# Interprète le langage naturel avant de lancer le moteur de génération.

import subprocess
import sys
import json
import re
import time
from pathlib import Path
from typing import Optional, Dict, Any

from core import action_kit as kit
from core.live_model_policy import BALANCED_MODEL

# ── Configuration ───────────────────────────────────────────────────────────
def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

BASE_DIR         = get_base_dir()
API_CONFIG_PATH  = BASE_DIR / "config" / "api_keys.json"
PROJECTS_DIR     = Path.home() / "Desktop" / "JarvisProjects"
MAX_FIX_ATTEMPTS = 5
MODEL_PLANNER    = BALANCED_MODEL
MODEL_WRITER     = BALANCED_MODEL

def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]

_CODE_SYSTEM = (
    "Tu es un ingénieur logiciel rigoureux. Réponds exactement au format demandé, "
    "sans commentaire sur ton propre travail. Le code reste dans sa langue d'origine, "
    "mais toute explication en prose est rédigée en français."
)


def _get_model(model_name: str = MODEL_PLANNER):
    """Le travail de code passe par le modèle Azure choisi dans les réglages.

    ``code_helper`` fonctionne ainsi depuis sa migration ; ``dev_agent`` appelait
    encore Gemini en direct, ce qui ignorait ``azure_code_model``. On garde
    Gemini en repli tant qu'aucun spécialiste Azure n'est configuré.
    """
    class _Response:
        def __init__(self, value): self.text = value

    class _Azure:
        def generate_content(self, contents):
            from core.azure_specialists import text
            return _Response(text("code", str(contents), system=_CODE_SYSTEM, timeout=180))

    class _Gemini:
        def generate_content(self, contents):
            from google import genai
            client = genai.Client(api_key=_get_api_key())
            return client.models.generate_content(model=model_name, contents=contents)

    try:
        from core.azure_specialists import _settings
        _settings("code")
        return _Azure()
    except Exception:
        return _Gemini()

def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\r?\n?", "", text)
    text = re.sub(r"\r?\n?```\s*$", "", text)
    return text.strip()

def _is_rate_limit(error: Exception) -> bool:
    msg = str(error).lower()
    return "429" in msg or "quota" in msg or "resource_exhausted" in msg

class RateLimitError(Exception):
    pass

# ── Parsing local des demandes de développement ─────────────────────────────
def _parse_dev_request_locally(text: str) -> Optional[Dict[str, Any]]:
    """
    Détecte le langage, le type de projet et extrait une description claire.
    Retourne un dict avec 'language', 'project_type' et une 'description' nettoyée.
    """
    text = text.lower().strip()
    text = re.sub(r"\b(s'il te pla[iî]t|stp|please|peux-tu|tu peux|je veux|j'aimerais)\b", "", text).strip()

    # Détection du langage
    language = "python"  # défaut
    for lang_word, lang in [
        ("python", "python"), ("javascript", "javascript"), ("js", "javascript"),
        ("typescript", "typescript"), ("ts", "typescript"), ("rust", "rust"),
        ("go", "go"), ("c++", "cpp"), ("java", "java"), ("html", "html"),
        ("site web", "html"), ("page web", "html"), ("script bash", "bash"),
        ("shell", "bash")
    ]:
        if re.search(rf"\b{lang_word}\b", text):
            language = lang
            break

    # Type de projet (aide à la planification)
    project_type = "script"
    if re.search(r"\b(jeu|game|snake|pong|tic[- ]?tac[- ]?toe|morpion)\b", text):
        project_type = "game"
    elif re.search(r"\b(cli|commande|terminal|outil|tool)\b", text):
        project_type = "cli_tool"
    elif re.search(r"\b(api|serveur|server|rest)\b", text):
        project_type = "api"
    elif re.search(r"\b(site|web|page|html|frontend|front end)\b", text):
        project_type = "web"
    elif re.search(r"\b(gui|interface|fen[eê]tre|tkinter|pyqt|electron)\b", text):
        project_type = "gui"
    elif re.search(r"\b(calculatrice|convertisseur|m[eé]t[eé]o|password|mot de passe)\b", text):
        project_type = "utility"

    # Nettoyage de la description : on garde l'essentiel après "crée", "développe", etc.
    m = re.search(r"(?:cr[eé]er?|d[eé]veloppe?|code|programme|build|make|write|g[eé]n[eè]re?)\s+(?:un |une |le |la |les |l'|l’)?(.+)", text)
    if m:
        description = m.group(1).strip()
        # Reformulation concise
        description = re.sub(r"\s+", " ", description)
        description = description[:200]  # limite
        return {
            "language": language,
            "project_type": project_type,
            "description": description
        }

    # Si pas de verbe, on prend toute la phrase
    return {
        "language": language,
        "project_type": project_type,
        "description": text.strip()[:200]
    }

def _detect_dev_intent_ai(description: str) -> Optional[Dict]:
    """Fallback IA pour les demandes complexes."""
    try:
        prompt = f"""Tu es un assistant développeur. Analyse la demande suivante et retourne UNIQUEMENT un JSON avec :
- "language" : le langage principal (python, javascript, typescript, html, bash, etc.)
- "project_type" : un parmi "script", "game", "cli_tool", "api", "web", "gui", "utility"
- "description" : description claire en anglais (max 200 caractères).

Exemple : "crée un jeu de snake en python" → {{"language":"python","project_type":"game","description":"snake game"}}

Demande : "{description}"
JSON :"""
        resp = _get_model(MODEL_PLANNER).generate_content(prompt)
        json_match = re.search(r'\{.*\}', resp.text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group(0))
    except Exception as e:
        print(f"[DevAgent] AI intent error: {e}")
    return None

# ── Fonctions de génération (inchangées, juste adaptées aux nouveaux params) ──
def _plan_project(description: str, language: str) -> dict:
    model = _get_model(MODEL_PLANNER)
    prompt = f"""You are a senior software architect. Create a minimal, complete file plan for this project.

Language: {language}
Description: {description}

Return ONLY valid JSON — no markdown, no explanation:
{{
  "project_name": "snake_case_name",
  "entry_point": "main.py",
  "files": [
    {{
      "path": "main.py",
      "description": "Entry point — what it does and which modules it imports",
      "imports": ["utils.helpers", "core.engine"]
    }},
    {{
      "path": "utils/helpers.py",
      "description": "Helper utilities — what functions it exposes",
      "imports": []
    }}
  ],
  "run_command": "python main.py",
  "dependencies": ["requests"]
}}

Critical rules:
1. List files in DEPENDENCY ORDER — files with no imports come first, entry point comes last.
2. The "imports" field must list every other project module this file imports (dot-notation, e.g. "utils.helpers").
3. Keep it minimal — only files truly needed.
4. Entry point must be in the files list.
5. Use relative paths only (e.g. "utils/helpers.py", not absolute paths).
6. Standard library modules (os, sys, json, etc.) do NOT go in "dependencies".

JSON:"""
    try:
        response = model.generate_content(prompt)
        raw = _strip_fences(response.text)
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Planner returned invalid JSON: {e}\nRaw: {response.text[:300]}")
    except Exception as e:
        if _is_rate_limit(e):
            raise RateLimitError(str(e))
        raise

def _write_file(
    file_info: dict, project_description: str, all_files: list[dict],
    language: str, project_dir: Path, already_written: dict[str, str],
) -> str:
    model = _get_model(MODEL_WRITER)
    file_path = file_info["path"]
    file_desc = file_info.get("description", "")
    file_imports = file_info.get("imports", [])

    file_list = "\n".join(
        f"  [{i+1}] {f['path']}: {f.get('description', '')}"
        for i, f in enumerate(all_files)
    )

    dependency_context = ""
    for dep_dotted in file_imports:
        dep_path = dep_dotted.replace(".", "/") + ".py"
        if dep_path in already_written:
            code_snippet = already_written[dep_path][:2000]
            dependency_context += f"\n\n--- {dep_path} (you must import from this) ---\n{code_snippet}"

    lang_rules = ""
    if language.lower() == "python":
        lang_rules = """
Python-specific rules:
- Use type hints for all function signatures.
- Add docstrings for all public functions and classes.
- Use if __name__ == "__main__": guard in the entry point.
- For relative imports within the project, use: from utils.helpers import foo  (match the project structure exactly).
- Do NOT use implicit relative imports (from . import ...) unless it's a proper package with __init__.py.
- If this is a package subdirectory, create __init__.py files where needed."""
    elif language.lower() in ("javascript", "typescript"):
        lang_rules = """
JS/TS-specific rules:
- Use ES modules (import/export), not CommonJS (require).
- Add JSDoc comments for all exported functions.
- Handle promise rejections with try/catch in async functions."""

    prompt = f"""You are a senior {language} developer writing production-quality code for a real project.

Project goal: {project_description}

Complete project file structure (in dependency order):
{file_list}

{f"Dependencies this file must import from other project files:{dependency_context}" if dependency_context else ""}

Your task: Write the complete, working code for: {file_path}
Purpose of this file: {file_desc}
{f"This file imports from: {', '.join(file_imports)}" if file_imports else "This file has no project-internal imports."}

{lang_rules}

General rules:
- Output ONLY raw code. Absolutely no explanation, no markdown, no triple backticks.
- Write COMPLETE, RUNNABLE code — no placeholders, no "# TODO", no "pass" stubs.
- Every import must either be from the standard library, listed dependencies, or the project files shown above.
- Match import paths EXACTLY to the file paths in the project structure (e.g. if file is "utils/helpers.py", import as "from utils.helpers import ...").
- Use proper error handling (try/except) where I/O or network calls are made.
- The code must work correctly when the project entry point is run from the project root directory.

Code for {file_path}:"""
    try:
        response = model.generate_content(prompt)
        code = _strip_fences(response.text)
        full_path = project_dir / file_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(code, encoding="utf-8")
        print(f"[DevAgent] ✅ Written: {file_path} ({len(code)} chars)")
        return code
    except Exception as e:
        if _is_rate_limit(e):
            raise RateLimitError(str(e))
        raise

def _install_dependencies(dependencies: list[str], project_dir: Path) -> str:
    if not dependencies:
        return "Aucune dépendance externe nécessaire."
    to_install = []
    for dep in dependencies:
        pkg_name = re.split(r"[>=<!]", dep)[0].strip()
        result = subprocess.run(
            [sys.executable, "-m", "pip", "show", pkg_name],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            to_install.append(dep)
        else:
            print(f"[DevAgent] ✓ Déjà installé : {pkg_name}")
    if not to_install:
        return f"Toutes les dépendances déjà installées : {', '.join(dependencies)}"
    print(f"[DevAgent] 📦 Installation : {to_install}")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install"] + to_install,
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=120, cwd=str(project_dir)
        )
        if result.returncode == 0:
            return f"Installé : {', '.join(to_install)}"
        return f"Avertissement installation (non bloquant) : {result.stderr[:200]}"
    except subprocess.TimeoutExpired:
        return "Installation des dépendances expirée (non bloquant)."
    except Exception as e:
        return f"Erreur d'installation (non bloquant) : {e}"

def _open_vscode(project_dir: Path) -> bool:
    """Ouvre le projet dans VS Code, et dit la vérité sur le résultat.

    Deux défauts corrigés ici. `Popen([cmd, dossier], shell=True)` ne transmet
    au shell que le premier élément de la liste : VS Code s'ouvrait sans le
    projet. Et comme le shell absorbe « command not found », la fonction
    annonçait une réussite dès le premier candidat, même sans VS Code installé.
    """
    candidates = [
        "code", "codium", "code-insiders",
        rf"C:\Users\{Path.home().name}\AppData\Local\Programs\Microsoft VS Code\bin\code.cmd",
        r"C:\Program Files\Microsoft VS Code\bin\code.cmd",
    ]
    for cmd in candidates:
        if kit.which(cmd) is None and not Path(cmd).exists():
            continue
        if kit.spawn([cmd, str(project_dir)]) is not None:
            print(f"[DevAgent] 💻 VS Code ouvert : {project_dir}")
            return True
    print("[DevAgent] VS Code introuvable sur cette machine.")
    return False

def _run_project(run_command: str, project_dir: Path, timeout: int = 30) -> str:
    print(f"[DevAgent] 🚀 Exécution : {run_command}")
    try:
        parts = run_command.split()
        if parts[0].lower() == "python":
            parts[0] = sys.executable
        result = subprocess.run(
            parts,
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=timeout,
            cwd=str(project_dir)
        )
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        parts_out = []
        if stdout:
            parts_out.append(f"SORTIE:\n{stdout}")
        if stderr:
            parts_out.append(f"ERREUR:\n{stderr}")
        return "\n\n".join(parts_out) if parts_out else "Exécuté sans sortie."
    except subprocess.TimeoutExpired:
        return f"Expiré après {timeout}s — l'application (serveur/GUI) fonctionne probablement."
    except FileNotFoundError as e:
        return f"Commande introuvable : {e}"
    except Exception as e:
        return f"Erreur d'exécution : {e}"

def _parse_traceback(output: str, project_files: list[str]) -> tuple[str | None, int | None]:
    pattern = re.compile(r'File ["\']([^"\']+\.py)["\'],\s+line\s+(\d+)', re.IGNORECASE)
    matches = pattern.findall(output)
    for raw_path, line_str in reversed(matches):
        raw_name = Path(raw_path).name
        for pf in project_files:
            if Path(pf).name == raw_name or pf == raw_path or raw_path.endswith(pf):
                return pf, int(line_str)
    return None, None

def _classify_error(output: str) -> str:
    low = output.lower()
    if any(x in low for x in ("no module named", "modulenotfounderror", "importerror")):
        return "dependency_error"
    if "syntaxerror" in low or "invalid syntax" in low:
        return "syntax_error"
    if "cannot import" in low or "importerror" in low:
        return "import_error"
    if any(x in low for x in (
        "traceback", "exception", "error:", "nameerror", "typeerror",
        "attributeerror", "valueerror", "keyerror", "indexerror",
        "zerodivisionerror", "filenotfounderror", "permissionerror",
    )):
        return "runtime_error"
    return "none"

def _has_error(output: str, run_command: str) -> bool:
    low = output.lower()
    if "timed out" in low:
        return False
    if not output.strip():
        return False
    return _classify_error(output) != "none"

def _try_auto_install(error_output: str, project_dir: Path) -> bool:
    pattern = re.compile(r"No module named ['\"]([a-zA-Z0-9_\-\.]+)['\"]", re.IGNORECASE)
    match = pattern.search(error_output)
    if not match:
        return False
    pkg = match.group(1).replace("_", "-").split(".")[0]
    print(f"[DevAgent] 🔧 Installation automatique du module manquant : {pkg}")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", pkg],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=60, cwd=str(project_dir)
        )
        return result.returncode == 0
    except Exception:
        return False

def _fix_files(
    error_output: str, project_description: str, all_files: list[dict],
    file_codes: dict[str, str], language: str, project_dir: Path,
    entry_point: str,
) -> dict[str, str]:
    model = _get_model(MODEL_PLANNER)
    error_file, error_line = _parse_traceback(error_output, list(file_codes.keys()))
    error_type = _classify_error(error_output)

    files_to_fix: list[str] = []
    if error_file:
        files_to_fix.append(error_file)
        if error_type == "import_error":
            for fi in all_files:
                if error_file.replace("/", ".").replace(".py", "") in fi.get("imports", []):
                    p = fi["path"]
                    if p not in files_to_fix:
                        files_to_fix.append(p)
    else:
        files_to_fix.append(entry_point)

    updated_codes: dict[str, str] = {}
    for fix_path in files_to_fix:
        current_code = file_codes.get(fix_path, "")
        other_ctx = ""
        for fp, code in file_codes.items():
            if fp != fix_path and code:
                snippet = code[:1500] + ("..." if len(code) > 1500 else "")
                other_ctx += f"\n--- {fp} ---\n{snippet}\n"

        line_hint = f"\nL'erreur semble proche de la ligne {error_line} dans ce fichier." if (
            error_line and fix_path == error_file
        ) else ""

        prompt = f"""You are an expert {language} debugger. Fix the broken file below.

Project goal: {project_description}

All project files:
{chr(10).join(f"  - {f['path']}: {f.get('description', '')}" for f in all_files)}

Other files for context (read-only — fix only the target file):
{other_ctx[:3500]}

File to fix: {fix_path}{line_hint}
Error type: {error_type}

Error output:
{error_output[:2500]}

Current (broken) code:
{current_code}

Rules:
- Output ONLY the complete fixed code. No explanation, no markdown, no backticks.
- Fix ALL errors visible in the error output.
- Keep all existing correct logic — do not remove working features.
- Ensure import paths match the actual project file structure exactly.
- Do NOT introduce new bugs or remove error handling.

Fixed code for {fix_path}:"""
        try:
            response = model.generate_content(prompt)
            fixed = _strip_fences(response.text)
            full_path = project_dir / fix_path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(fixed, encoding="utf-8")
            updated_codes[fix_path] = fixed
            print(f"[DevAgent] 🔧 Corrigé : {fix_path}")
        except Exception as e:
            if _is_rate_limit(e):
                raise RateLimitError(str(e))
            print(f"[DevAgent] ⚠️ Impossible de corriger {fix_path}: {e}")
    return updated_codes

def _build_project(
    description: str, language: str, project_name: str,
    timeout: int, speak=None, player=None,
) -> str:
    def log(msg: str):
        print(f"[DevAgent] {msg}")
        if player:
            player.write_log(f"[DevAgent] {msg}")

    log("Planification de la structure du projet...")
    try:
        plan = _plan_project(description, language)
    except RateLimitError:
        msg = "Limite de taux atteinte, veuillez réessayer dans un instant."
        if speak: speak(msg)
        return msg
    except ValueError as e:
        msg = f"Échec de la planification : {e}"
        if speak: speak(msg)
        return msg

    proj_name    = project_name or plan.get("project_name", "jarvis_project")
    proj_name    = re.sub(r"[^\w\-]", "_", proj_name)
    project_dir  = PROJECTS_DIR / proj_name
    project_dir.mkdir(parents=True, exist_ok=True)

    files        = plan.get("files", [])
    entry_point  = plan.get("entry_point", "main.py")
    run_command  = plan.get("run_command", f"python {entry_point}")
    dependencies = plan.get("dependencies", [])

    log(f"Projet : {proj_name} | Fichiers : {len(files)} | Point d'entrée : {entry_point}")

    def _dep_sort_key(fi: dict) -> int:
        return len(fi.get("imports", []))

    sorted_files = sorted(files, key=_dep_sort_key)

    file_codes: dict[str, str] = {}
    for file_info in sorted_files:
        file_path = file_info.get("path", "")
        if not file_path:
            continue

        log(f"Écriture de {file_path}...")
        for attempt in range(2):
            try:
                code = _write_file(
                    file_info=file_info,
                    project_description=description,
                    all_files=files,
                    language=language,
                    project_dir=project_dir,
                    already_written=file_codes,
                )
                file_codes[file_path] = code
                time.sleep(0.4)
                break
            except RateLimitError:
                if attempt == 0:
                    log("Limite de taux — pause 20s...")
                    time.sleep(20)
                else:
                    log(f"Échec limite de taux pour {file_path}, ignoré.")
            except Exception as e:
                log(f"Impossible d'écrire {file_path}: {e}")
                break

    if not file_codes:
        msg = "Je n'ai pu écrire aucun fichier du projet."
        if speak: speak(msg)
        return msg

    if dependencies:
        install_result = _install_dependencies(dependencies, project_dir)
        log(install_result)

    _open_vscode(project_dir)

    last_output   = ""
    auto_installs = 0
    for attempt in range(1, MAX_FIX_ATTEMPTS + 1):
        log(f"Exécution du projet (tentative {attempt}/{MAX_FIX_ATTEMPTS})...")
        last_output = _run_project(run_command, project_dir, timeout)
        log(f"Aperçu : {last_output[:150]}")

        if not _has_error(last_output, run_command):
            msg = (
                f"Le projet '{proj_name}' fonctionne, monsieur. "
                f"Construit en {attempt} tentative(s). "
                f"Enregistré dans : {project_dir}"
            )
            if speak: speak(msg)
            return f"{msg}\n\nSortie :\n{last_output}"

        if attempt == MAX_FIX_ATTEMPTS:
            break

        error_type = _classify_error(last_output)
        if error_type == "dependency_error" and auto_installs < 3:
            installed = _try_auto_install(last_output, project_dir)
            if installed:
                auto_installs += 1
                log("Dépendance manquante installée, nouvelle tentative...")
                time.sleep(1)
                continue

        log(f"Correction des erreurs (type : {error_type})...")
        try:
            updated = _fix_files(
                error_output=last_output,
                project_description=description,
                all_files=files,
                file_codes=file_codes,
                language=language,
                project_dir=project_dir,
                entry_point=entry_point,
            )
            file_codes.update(updated)
            time.sleep(1)
        except RateLimitError:
            msg = "Limite de taux atteinte pendant la correction. Projet sauvegardé, vérifiez dans VS Code."
            if speak: speak(msg)
            return msg
        except Exception as e:
            log(f"Échec de l'étape de correction : {e}")

    msg = (
        f"Je n'ai pas pu corriger entièrement '{proj_name}' après {MAX_FIX_ATTEMPTS} tentatives. "
        f"Le projet est sauvegardé dans {project_dir} — ouvrez‑le dans VS Code pour vérifier."
    )
    if speak: speak(msg)
    return f"{msg}\n\nDernière erreur :\n{last_output[:600]}"

# ── Point d'entrée principal ────────────────────────────────────────────────
@kit.action("dev_agent")
def dev_agent(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
    speak=None,
) -> str:
    """
    Agent de développement intelligent.
    Accepte soit des paramètres classiques, soit une description en langage naturel.
    """
    p = parameters or {}
    description  = p.get("description", "").strip()
    # Si pas de description, on essaie d'utiliser 'task' ou 'action' comme fallback
    if not description:
        description = p.get("task", "") or p.get("action", "")
    
    language     = p.get("language", "python").strip()
    project_name = p.get("project_name", "").strip()
    timeout      = int(p.get("timeout", 30))

    # Interprétation naturelle si la description semble être une phrase libre
    if description and not re.match(r"^[a-zA-Z0-9_]+$", description):
        # Tentative de parsing local
        local = _parse_dev_request_locally(description)
        if local and local.get("description"):
            # On remplace la description par la version nettoyée
            description = local["description"]
            # Ne pas écraser le langage s'il était déjà précisé
            if language == "python" and local.get("language"):
                language = local["language"]
        else:
            # Fallback IA pour clarifier
            ai = _detect_dev_intent_ai(description)
            if ai:
                if ai.get("description"):
                    description = ai["description"]
                if language == "python" and ai.get("language"):
                    language = ai["language"]

    if not description:
        return "Veuillez décrire le projet que vous souhaitez que je construise."

    # Reformulation éventuelle pour cohérence (très courte)
    if len(description) < 10:
        description += f" (a {language} project)"

    # Lancement de la génération
    return _build_project(
        description  = description,
        language     = language,
        project_name = project_name,
        timeout      = timeout,
        speak        = speak,
        player       = player,
    )
