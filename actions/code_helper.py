"""
code_helper.py – JARVIS Code Engine
Ultra‑optimisé pour Linux (Hyprland/ambxst). 
Détection intelligente des interpréteurs, screenshot Wayland‑native,
auto‑fix itératif, et sandboxing virtuel.
"""

import sys
import json
import re
import time
import shutil
import os
from pathlib import Path
from typing import Optional, List, Tuple, Dict

from core import action_kit as kit
from core.live_model_policy import BALANCED_MODEL

# ── Base Paths ──────────────────────────────────────────────────────────────────
def get_base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

BASE_DIR = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
DESKTOP = Path.home() / "Desktop"

MAX_BUILD_ATTEMPTS = 5
GEMINI_MODEL = BALANCED_MODEL

# ── API Key / Client ─────────────────────────────────────────────────────────────
def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]

def _get_gemini(model: str = GEMINI_MODEL):
    # Gemini pilote le reste de l'assistant ; le travail de code est confié au
    # modèle Azure explicitement choisi dans les réglages.
    from core.azure_specialists import text
    class _Response:
        def __init__(self, value): self.text = value
    class _Wrapper:
        def generate_content(self, contents, **_kwargs):
            return _Response(text("code", str(contents), system=(
                "Tu es un ingénieur logiciel rigoureux. Réponds exactement au format demandé. "
                "Le code reste dans sa langue d'origine, mais toute explication en prose "
                "est rédigée en français : elle est lue à voix haute par un assistant français."
            )))
    return _Wrapper()

def _get_legacy_gemini(model: str = GEMINI_MODEL):
    from google import genai
    client = genai.Client(api_key=_get_api_key())
    # Wrapper léger pour uniformiser l'appel
    class _Wrapper:
        def generate_content(self, contents, **kwargs):
            return client.models.generate_content(model=model, contents=contents, **kwargs)
    return _Wrapper()

# ── Code Cleaning ────────────────────────────────────────────────────────────────
def _clean_code(text: str) -> str:
    text = text.strip()
    # Retirer les marqueurs markdown ``` éventuels
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    return text.strip()

# ── File Utilities ───────────────────────────────────────────────────────────────
def _read_file(file_path: str) -> Tuple[str, str]:
    if not file_path:
        return "", "No file path provided."
    p = Path(file_path)
    if not p.exists():
        return "", f"File not found: {file_path}"
    try:
        return p.read_text(encoding="utf-8"), ""
    except Exception as e:
        return "", f"Read error: {e}"

def _save_file(path: Path, content: str) -> str:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"Saved to: {path}"
    except Exception as e:
        return f"Save error: {e}"

def _preview(code: str, lines: int = 12) -> str:
    all_lines = code.splitlines()
    preview = "\n".join(all_lines[:lines])
    if len(all_lines) > lines:
        preview += f"\n... ({len(all_lines) - lines} more lines)"
    return preview

# ── Interpreter Detection (cross‑platform) ──────────────────────────────────────
_INTERPRETER_MAP: Dict[str, List[str]] = {
    ".py":  [sys.executable],
    ".js":  ["node"],
    ".ts":  ["ts-node"],
    ".sh":  ["bash"],
    ".zsh": ["zsh"],
    ".bash": ["bash"],
    ".ps1": ["pwsh", "powershell"],
    ".rb":  ["ruby"],
    ".php": ["php"],
    ".rs":  ["cargo", "run"],
    ".go":  ["go", "run"],
    ".cs":  ["dotnet", "run"],
    ".kt":  ["kotlin"],
    ".swift": ["swift"],
    ".lua": ["lua"],
    ".R":   ["Rscript"],
}

def _find_interpreter(ext: str) -> Optional[List[str]]:
    """Retourne la liste [interpréteur, ...] ou None si introuvable."""
    candidates = _INTERPRETER_MAP.get(ext, [])
    for cmd in candidates:
        # On vérifie que le premier élément est accessible
        if shutil.which(cmd):
            return [cmd] + (candidates[1:] if len(candidates) > 1 else [])
    return None

# ── Screenshot (Wayland/Hyprland native) ────────────────────────────────────────
def _take_screenshot() -> Optional[Path]:
    """Capture d'écran adaptée à Hyprland/Wayland, fallback X11/pyautogui."""
    path = DESKTOP / f"jarvis_debug_{int(time.time())}.png"

    # 1) Hyprland / wlroots (grim + slurp pour sélection, sinon fullscreen)
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") or os.environ.get("WAYLAND_DISPLAY"):
        try:
            # Plein écran directement : cette capture sert au diagnostic
            # automatique. L'ancienne version passait par `slurp` avec un délai
            # de 5 secondes — l'utilisateur n'avait pas le temps de tracer une
            # zone, la commande expirait à chaque fois, et le tube shell mourait
            # sans que personne ne le sache.
            if kit.which("grim"):
                if kit.run(["grim", str(path)], timeout=8).ok and path.exists():
                    return path
        except Exception:
            pass

    # 2) X11 (import/gnome-screenshot)
    if os.environ.get("DISPLAY"):
        try:
            if shutil.which("gnome-screenshot"):
                kit.run(["gnome-screenshot", "-f", str(path)], timeout=3)
                if path.exists():
                    return path
            if shutil.which("import"):
                kit.run(["import", "-window", "root", str(path)], timeout=3)
                if path.exists():
                    return path
        except Exception:
            pass

    # 3) Fallback pyautogui (X11 obligatoire, risque d'échec sous Wayland)
    try:
        import pyautogui
        img = pyautogui.screenshot()
        img.save(str(path))
        return path
    except Exception:
        pass

    return None

def _image_to_base64(path: Path) -> str:
    import base64
    return base64.b64encode(path.read_bytes()).decode("utf-8")

# ── Intent Detection ────────────────────────────────────────────────────────────
_VALID_INTENTS = {"write", "edit", "explain", "run", "build", "screen_debug", "optimize"}

def _detect_intent(description: str, file_path: str, code: str) -> str:
    """Détection d'intention via Gemini (multilingue)."""
    desc = (description or "").strip()
    file_exists = bool(file_path) and Path(file_path).exists()

    if desc:
        try:
            ctx = []
            if file_path:
                ctx.append(f"a file path is provided (exists on disk: {file_exists})")
            if code:
                ctx.append("an inline code snippet is provided")
            prompt = (
                "Classify a coding assistant request into exactly ONE intent word.\n"
                "The request may be written in ANY language.\n\n"
                f"Request: {desc}\n"
                + (f"Context: {'; '.join(ctx)}\n" if ctx else "")
                + "\nIntents:\n"
                "  write        = create new code from scratch\n"
                "  edit         = modify an existing file\n"
                "  explain      = describe what given code/file does\n"
                "  run          = execute an existing file\n"
                "  build        = write code, run it, and iterate until it works\n"
                "  screen_debug = analyze an error currently visible on the user's screen\n"
                "  optimize     = refactor / clean up / speed up existing code\n\n"
                "Reply with ONLY the intent word, nothing else."
            )
            ans = _get_gemini().generate_content(prompt).text.strip().lower()
            ans = ans.strip("`'\". \n")
            if ans in _VALID_INTENTS:
                return ans
        except Exception as e:
            print(f"[Code] Intent classification failed ({e}) — structural fallback")

    # Fallback structurel
    if file_exists:
        return "edit" if desc else "explain"
    if code:
        return "explain"
    return "write"

# ── Core Actions ────────────────────────────────────────────────────────────────
def _resolve_save_path(output_path: str, language: str) -> Path:
    ext_map = {
        "python": ".py", "py": ".py",
        "javascript": ".js", "js": ".js",
        "typescript": ".ts", "ts": ".ts",
        "html": ".html", "css": ".css",
        "java": ".java", "cpp": ".cpp", "c": ".c",
        "bash": ".sh", "shell": ".sh", "powershell": ".ps1",
        "sql": ".sql", "json": ".json", "rust": ".rs", "go": ".go",
        "csharp": ".cs", "cs": ".cs", "php": ".php", "ruby": ".rb",
        "swift": ".swift", "kotlin": ".kt",
    }
    if output_path:
        p = Path(output_path)
        return p if p.is_absolute() else DESKTOP / p
    ext = ext_map.get((language or "python").lower(), ".py")
    return DESKTOP / f"jarvis_code{ext}"

def _run_file(path: Path, args: list, timeout: int, env=None, shell: str = None) -> str:
    """Exécute un fichier avec l'interpréteur approprié ou via un shell spécifique."""
    # Si un shell est explicitement demandé (ambxst, zsh, etc.)
    if shell:
        shell_path = shutil.which(shell)
        if not shell_path:
            return f"Shell '{shell}' introuvable."
        cmd = [shell_path, str(path)] + (args or [])
    else:
        interp = _find_interpreter(path.suffix)
        if not interp:
            return f"No interpreter for {path.suffix}. Specify a shell or install required tools."
        cmd = interp + [str(path)] + (args or [])

    try:
        result = kit.run(
            cmd,
            timeout=timeout, cwd=str(path.parent),
            env=env or os.environ,
        )
        if result.timed_out:
            return f"Timeout after {timeout}s."
        if result.not_found:
            return f"Interpreter missing: {cmd[0]}"
        out = result.stdout.strip()
        err = result.stderr.strip()
        parts = []
        if out:
            parts.append(out)
        if err:
            parts.append(f"Stderr:\n{err}")
        return "\n".join(parts) if parts else "Executed (no output)."
    except Exception as e:
        return f"Execution error: {e}"

def _has_error(output: str) -> bool:
    return any(s in output.lower() for s in (
        "error", "exception", "traceback", "syntaxerror",
        "nameerror", "typeerror", "failed", "cannot find",
    ))

def _write(description: str, language: str, output_path: str, player=None) -> Tuple[str, Path]:
    lang  = language or "python"
    model = _get_gemini()

    prompt = f"""You are an expert {lang} developer.
Write clean, working, well-commented {lang} code for the description below.

Rules:
- Output ONLY the code. No explanation, no markdown, no backticks.
- Add helpful inline comments.
- Handle errors and edge cases properly.
- Use modern best practices.
- If the code is meant to be run, include a __main__ block or equivalent entry point.

Description: {description}

Code:"""

    response = model.generate_content(prompt)
    code     = _clean_code(response.text)
    path     = _resolve_save_path(output_path, lang)
    _save_file(path, code)
    return code, path

def _fix_code(code: str, error_output: str, description: str, language: str) -> str:
    model  = _get_gemini()
    prompt = f"""You are an expert debugger in {language or 'python'}.
The code below failed with the following error. Fix it.
Return ONLY the corrected code — no explanation, no markdown, no backticks.

Original goal: {description}

Error:
{error_output[:2000]}

Broken code:
{code}

Fixed code:"""
    response = model.generate_content(prompt)
    return _clean_code(response.text)

def _build(description, language, output_path, args, timeout, speak=None, player=None, shell=None) -> str:
    if not description:
        return "Please describe what you want me to build."

    lang = language or "python"
    code, path = None, None
    last_output = ""

    for attempt in range(1, MAX_BUILD_ATTEMPTS + 1):
        try:
            if attempt == 1:
                code, path = _write(description, lang, output_path, player)
            else:
                code = _fix_code(code, last_output, description, lang)
                _save_file(path, code)

            if player:
                player.write_log(f"[Code] Attempt {attempt}")
            print(f"[Code] ✅ Written: {path}")

        except Exception as e:
            msg = f"Error on attempt {attempt}: {e}"
            if speak: speak(msg)
            return msg

        # Exécution (peut utiliser un shell personnalisé)
        last_output = _run_file(path, args, timeout, shell=shell)

        if not _has_error(last_output):
            msg = (
                f"Build successful after {attempt} attempt(s).\n"
                f"Saved to {path}.\nOutput:\n{last_output}"
            )
            if speak: speak(msg)
            return msg

        print(f"[Code] ⚠️ Error on attempt {attempt}, fixing...")

    return (
        f"I was unable to build a working version after {MAX_BUILD_ATTEMPTS} attempts.\n"
        f"Last error: {last_output[:300]}\n"
        f"Code saved at: {path}"
    )

def _write_action(description, language, output_path, player) -> str:
    if not description:
        return "Please describe what you want me to write."
    if player:
        player.write_log("[Code] Writing code...")
    try:
        code, path = _write(description, language, output_path, player)
        return f"Code written. Saved to: {path}\n\nPreview:\n{_preview(code)}"
    except Exception as e:
        return f"Could not generate code: {e}"

def _edit_action(file_path, instruction, player) -> str:
    if not file_path or not instruction:
        return "Provide both file path and edit instruction."
    content, err = _read_file(file_path)
    if err:
        return err

    if player:
        player.write_log("[Code] Editing file...")
    model = _get_gemini()
    prompt = f"""You are an expert code editor.
Apply the following change to the code below.
Return ONLY the complete updated code — no explanation, no markdown, no backticks.

Change: {instruction}

Original code:
{content}

Updated code:"""
    try:
        edited = _clean_code(model.generate_content(prompt).text)
    except Exception as e:
        return f"Edit failed: {e}"
    status = _save_file(Path(file_path), edited)
    return f"File edited. {status}\n\nPreview:\n{_preview(edited)}"

def _explain_action(file_path, code, player) -> str:
    if file_path and not code:
        code, err = _read_file(file_path)
        if err:
            return err
    if not code:
        return "Provide code or file path to explain."
    if player:
        player.write_log("[Code] Analyzing...")
    model = _get_gemini()
    prompt = f"""Explain what this code does in simple, clear language.
Be concise — 3 to 6 sentences maximum.

Code:
{code[:4000]}

Explanation:"""
    try:
        return model.generate_content(prompt).text.strip()
    except Exception as e:
        return f"Explain error: {e}"

def _run_action(file_path, args, timeout, player, shell=None) -> str:
    if not file_path:
        return "No file path provided."
    p = Path(file_path)
    if not p.exists():
        return f"File not found: {file_path}"
    if player:
        player.write_log(f"[Code] Running {p.name}...")
    return _run_file(p, args, timeout, shell=shell)

def _optimize_action(file_path, code, language, output_path, player) -> str:
    if file_path and not code:
        code, err = _read_file(file_path)
        if err:
            return err
    if not code:
        return "Provide code or file path to optimize."

    if player:
        player.write_log("[Code] Optimizing...")
    lang = language or "python"
    model = _get_gemini()
    prompt = f"""You are an expert {lang} developer.
Optimize the following code for performance, readability, and best practices.
Return ONLY the optimized code — no explanation, no markdown, no backticks.

Original code:
{code[:6000]}

Optimized code:"""
    try:
        optimized = _clean_code(model.generate_content(prompt).text)
    except Exception as e:
        return f"Optimization failed: {e}"

    save_path = Path(file_path) if file_path else _resolve_save_path(output_path, lang)
    _save_file(save_path, optimized)

    orig_lines = len(code.splitlines())
    opt_lines  = len(optimized.splitlines())
    diff = orig_lines - opt_lines
    return (
        f"Code optimized. Saved to {save_path}\n"
        f"Lines: {orig_lines} → {opt_lines} ({'+' if diff < 0 else ''}{-diff if diff < 0 else diff} lines)\n"
        f"Preview:\n{_preview(optimized)}"
    )

def _screen_debug_action(description, file_path, player, speak=None,
                         apply_fix: bool = False) -> str:
    if player:
        try:
            player.write_log("[Code] 🔍 Analyse visuelle et auto-debug de l'écran...")
        except Exception:
            pass

    try:
        from core import auto_debug
        spoken_msg, diag = auto_debug.auto_debug_live(
            user_query=description or "Analyse l'erreur à l'écran et propose la solution.",
            target_window="active_window",
            player=player,
        )

        # L'analyse d'écran est en lecture seule par défaut. L'ancienne
        # version remplaçait silencieusement le fichier par n'importe quel bloc
        # renvoyé par le modèle, y compris un diff unifié : un diagnostic
        # pouvait donc détruire un fichier valide.
        target_f = file_path or (diag.parsed_error.file_path if diag.parsed_error else None)
        if apply_fix and target_f and diag.code_diff:
            code_match = re.search(r"```[a-zA-Z]*\n(.*?)```", diag.code_diff, re.DOTALL)
            fixed_code = code_match.group(1).strip() if code_match else diag.code_diff.strip()
            if fixed_code.startswith(("diff ", "--- ", "+++ ")) or "\n@@" in fixed_code:
                return spoken_msg + "\n\nCorrectif non appliqué : le modèle a fourni un diff, pas un fichier complet sûr."
            if len(fixed_code.splitlines()) <= 3:
                return spoken_msg + "\n\nCorrectif non appliqué : contenu proposé trop court."
            p = Path(target_f).expanduser().resolve()
            if p.is_file():
                if p.suffix == ".py":
                    try:
                        compile(fixed_code, str(p), "exec")
                    except SyntaxError as exc:
                        return spoken_msg + f"\n\nCorrectif non appliqué : Python invalide ({exc.msg}, ligne {exc.lineno})."
                backup = p.with_suffix(p.suffix + ".bak")
                backup.write_bytes(p.read_bytes())
                result = _save_file(p, fixed_code)
                if result.startswith("Save error"):
                    return spoken_msg + f"\n\n{result}"
                spoken_msg += f"\n\n✅ Correctif appliqué à {target_f} (sauvegarde : {backup.name})"

        return spoken_msg
    except Exception as e:
        return f"Échec de l'auto-debug d'écran : {e}"

# ── Main Dispatcher ──────────────────────────────────────────────────────────────
@kit.action("code_helper")
def code_helper(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
    speak=None
) -> str:
    """
    Action principale de code_helper.

    Parameters:
        action      : write | edit | explain | run | build | optimize | screen_debug | auto
        description : description de la tâche
        language    : langage (python, js, ...)
        output_path : chemin de sortie
        file_path   : chemin d'un fichier existant
        code        : extrait de code inline
        args        : arguments pour l'exécution
        timeout     : timeout en secondes (défaut 30)
        shell       : shell à utiliser pour l'exécution (ex: ambxst, zsh)
    """
    p           = parameters or {}
    action      = p.get("action", "auto").lower().strip()
    description = p.get("description", "").strip()
    language    = p.get("language", "python").strip()
    output_path = p.get("output_path", "").strip()
    file_path   = p.get("file_path", "").strip()
    code        = p.get("code", "").strip()
    args        = p.get("args", [])
    timeout     = int(p.get("timeout", 30))
    shell       = p.get("shell", None)  # Nouveau : permet d'exécuter dans un shell spécifique
    apply_fix   = bool(p.get("apply_fix", False))

    if action == "auto":
        action = _detect_intent(description, file_path, code)
        print(f"[Code] 🤖 Auto-detected: {action}")

    if action == "write":
        return _write_action(description, language, output_path, player)
    elif action == "edit":
        return _edit_action(file_path, description, player)
    elif action == "explain":
        return _explain_action(file_path, code, player)
    elif action == "run":
        return _run_action(file_path, args, timeout, player, shell)
    elif action == "build":
        return _build(description, language, output_path, args, timeout, speak, player, shell)
    elif action == "optimize":
        return _optimize_action(file_path, code, language, output_path, player)
    elif action == "screen_debug":
        return _screen_debug_action(description, file_path, player, speak, apply_fix)
    else:
        return f"Unknown action: '{action}'. Use write, edit, explain, run, build, optimize, or screen_debug."

# ── Quick test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(code_helper({"action": "explain", "code": "print('hello')"}))
