"""core/auto_fix.py — Réparation automatique du code d'ANO-GPT sur demande.

« Corrige » : l'incident le plus récent (``core.incident_log``) est confié à
un agent de code qui travaille DANS ce dépôt — d'abord Claude Code
(``claude -p``), sinon Codex (``codex exec``), sinon l'ancien ``dev_agent``.
L'agent reçoit la pile d'appel, le fichier fautif et la consigne de corriger
la cause, de lancer les tests rapides concernés et de commiter en français.
Le tout part en tâche de fond ; l'assistant annonce le résultat, puis peut se
redémarrer lui-même (code de sortie ``RESTART_EXIT_CODE``, relevé par le
superviseur de ``main.py``) pour charger le code corrigé.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from core import incident_log

RESTART_EXIT_CODE = 75
AGENT_TIMEOUT_S = 900
CARD_TYPE = "agent"
_LOCK = threading.Lock()
_RUNNING: dict[str, float] = {}
_LAST_RESULT: dict[str, Any] = {}


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _card(player: Any, body: str, title: str = "Auto-réparation") -> None:
    try:
        update = getattr(player, "update_card", None)
        if callable(update) and update(CARD_TYPE, title, body):
            return
        show = getattr(player, "show_card", None)
        if callable(show):
            show(CARD_TYPE, title, body)
    except Exception:
        pass


def _announce(speak: Any, text: str) -> None:
    if callable(speak) and text:
        try:
            speak("[RÉPARATION] Dis exactement ceci, sans rien ajouter et sans appeler "
                  f"le moindre outil : {text}")
        except Exception:
            pass


def build_prompt(inc: incident_log.Incident) -> str:
    stem = Path(inc.file).stem if inc.file else inc.source
    tests_hint = (f"Tests rapides à lancer après correction : "
                  f"`python -m pytest tests -q -x -k \"{stem}\"` "
                  "(ou les tests du module touché) ; jamais la suite complète.\n")
    return (
        "Tu répares ANO-GPT, un assistant vocal Python (PyQt, Gemini Live) sur une machine "
        "modeste. Une erreur réelle vient de se produire pendant l'utilisation :\n\n"
        f"{incident_log.describe(inc)}\n\n"
        f"Pile d'appel :\n```\n{inc.traceback.strip() or '(pas de pile disponible)'}\n```\n\n"
        "Consignes :\n"
        "1. Trouve la CAUSE (pas un try/except qui cache le symptôme) en lisant le code concerné "
        "et ses appelants.\n"
        "2. Corrige-la proprement, dans le style du code existant, commentaires en français.\n"
        f"3. {tests_hint}"
        "4. Ne lance jamais main.py ni de build complet.\n"
        "5. Commite avec un message court en français décrivant la correction, puis `git push`.\n"
        "6. Termine par un résumé de 2 à 3 phrases en français, sans markdown, destiné à être lu "
        "à voix haute : ce qui cassait, ce que tu as changé, dans quel fichier.\n"
    )


def _run(cmd: list[str], cwd: Path, timeout: int) -> tuple[int, str]:
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)      # un agent lancé depuis Claude Code refuse sinon de démarrer
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, env=env)
    out = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    return proc.returncode, out.strip()


def available_engines() -> list[str]:
    engines = []
    if shutil.which("claude"):
        engines.append("claude")
    if shutil.which("codex"):
        engines.append("codex")
    engines.append("dev_agent")
    return engines


def _engine_claude(prompt: str, cwd: Path) -> tuple[int, str]:
    allowed = ("Read,Edit,Write,Grep,Glob,Bash(python -m pytest*),Bash(pytest*),Bash(git add*),"
               "Bash(git commit*),Bash(git push*),Bash(git diff*),Bash(git status*),Bash(git log*),"
               "Bash(python -c*),Bash(sed -n*),Bash(cat*),Bash(ls*),Bash(grep*),Bash(rg*)")
    return _run(["claude", "-p", prompt, "--output-format", "text", "--allowedTools", allowed],
                cwd, AGENT_TIMEOUT_S)


def _engine_codex(prompt: str, cwd: Path) -> tuple[int, str]:
    return _run(["codex", "exec", "--full-auto", prompt], cwd, AGENT_TIMEOUT_S)


def _engine_dev_agent(prompt: str, cwd: Path, player: Any, speak: Any) -> tuple[int, str]:
    from actions.dev_agent import dev_agent
    text = dev_agent(parameters={"description": prompt, "project_name": "fix_anogpt",
                                 "language": "python"}, player=player, speak=None)
    return 0, str(text or "")


def _head(cwd: Path) -> str:
    code, out = _run(["git", "rev-parse", "HEAD"], cwd, 10)
    return out if code == 0 else ""


def _changed_files(cwd: Path, since: str) -> list[str]:
    if not since:
        return []
    code, out = _run(["git", "diff", "--name-only", since, "HEAD"], cwd, 10)
    files = [l for l in out.splitlines() if l.strip()] if code == 0 else []
    code, dirty = _run(["git", "status", "--porcelain"], cwd, 10)
    files += [l[3:] for l in dirty.splitlines() if l.strip()] if code == 0 else []
    return sorted(set(files))


def _summary_from_output(out: str) -> str:
    lines = [l.strip() for l in out.strip().splitlines() if l.strip()]
    tail = " ".join(lines[-6:])
    return tail[-600:]


def repair(inc: incident_log.Incident, player: Any = None, speak: Any = None,
           on_done: Optional[Callable[[dict], None]] = None) -> str:
    """Lance la réparation en fond. Retourne la phrase à dire tout de suite."""
    with _LOCK:
        if inc.key in _RUNNING and time.monotonic() - _RUNNING[inc.key] < AGENT_TIMEOUT_S:
            return "Je suis déjà en train de réparer cette erreur ; je te préviens dès que c'est fait."
        _RUNNING[inc.key] = time.monotonic()

    engines = available_engines()
    cwd = repo_root()
    prompt = build_prompt(inc)

    def _work() -> None:
        result: dict[str, Any] = {"incident": inc.key, "ok": False, "engine": "", "files": [], "summary": ""}
        try:
            before = _head(cwd)
            _card(player, f"**{inc.source}** — {inc.message[:120]}\n\n"
                          f"Fichier : `{inc.file or '?'}`\n\n⏳ Analyse et correction par {engines[0]}…")
            last_out = ""
            for engine in engines:
                started = time.monotonic()
                try:
                    if engine == "claude":
                        code, out = _engine_claude(prompt, cwd)
                    elif engine == "codex":
                        code, out = _engine_codex(prompt, cwd)
                    else:
                        code, out = _engine_dev_agent(prompt, cwd, player, speak)
                except subprocess.TimeoutExpired:
                    code, out = 124, f"{engine} : délai dépassé"
                except Exception as exc:
                    code, out = 1, f"{engine} : {exc}"
                last_out = out
                print(f"[AutoFix] {engine} terminé en {time.monotonic() - started:.0f}s (code {code})")
                files = _changed_files(cwd, before)
                if files:
                    result.update(ok=True, engine=engine, files=files, summary=_summary_from_output(out))
                    break
                if code == 0 and engine != "dev_agent":
                    # L'agent a répondu sans modifier de fichier : il a jugé
                    # qu'il n'y avait rien à corriger. On le rapporte tel quel.
                    result.update(ok=False, engine=engine, summary=_summary_from_output(out))
                    break
            if not result["summary"]:
                result["summary"] = _summary_from_output(last_out) or "aucune réponse de l'agent"
        finally:
            with _LOCK:
                _RUNNING.pop(inc.key, None)
                _LAST_RESULT.clear()
                _LAST_RESULT.update(result)

        if result["ok"]:
            incident_log.mark_fixed(inc)
            files = ", ".join(Path(f).name for f in result["files"][:4])
            _card(player, f"✅ Correctif appliqué par {result['engine']} — {files}\n\n{result['summary']}")
            _announce(speak, f"C'est corrigé. {result['summary']} Dis « redémarre » pour que "
                             "j'applique le correctif.")
        else:
            _card(player, f"⚠️ Pas de correctif appliqué.\n\n{result['summary']}")
            _announce(speak, f"Je n'ai pas pu appliquer de correctif automatiquement. {result['summary']}")
        if on_done:
            try:
                on_done(result)
            except Exception:
                pass

    try:
        from core.thread_pool import get_thread_pool
        get_thread_pool().submit("network-heavy", _work, task_name="auto-fix", stall_timeout=AGENT_TIMEOUT_S + 60)
    except Exception:
        threading.Thread(target=_work, daemon=True, name="auto-fix").start()
    return (f"Je répare : {incident_log.describe(inc)}. Un agent de code ({engines[0]}) analyse le "
            "fichier et corrige la cause ; ça prend une à cinq minutes, je te préviens dès que c'est fait.")


def last_result() -> dict[str, Any]:
    return dict(_LAST_RESULT)


def request_restart(speak: Any = None, delay_s: float = 1.5) -> str:
    """Quitte avec le code de redémarrage : le superviseur relance aussitôt."""
    def _exit() -> None:
        time.sleep(delay_s)
        os._exit(RESTART_EXIT_CODE)
    threading.Thread(target=_exit, daemon=True, name="restart").start()
    return "Je redémarre pour charger le code corrigé ; je reviens dans quelques secondes."
