"""Exécution isolée des missions longues du mode « Agent Fantôme ».

Le processus agent hérite de la configuration MCP d'Antigravity, mais jamais
de la boucle audio. Sa sortie est capturée dans un rapport durable et la
notification finale est laissée au bus proactif d'ANO-GPT.
"""

from __future__ import annotations

import os
import signal
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from core import agent_brain

GHOST_MODE_ENV = "ANOGPT_GHOST_MODE"
DEFAULT_TIMEOUT_SECONDS = 60 * 60


@dataclass(frozen=True)
class GhostResult:
    status: str
    summary: str
    report_path: str
    exit_code: int | None = None
    changed_files: tuple[str, ...] = ()


_IGNORED_PARTS = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "node_modules", "build",
    "dist", "target", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".dart_tool", ".gradle",
})
_MAX_SNAPSHOT_FILES = 30_000


def _workspace_snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """Instantané métadonnées borné, sans lire le contenu ni suivre les liens."""
    snapshot: dict[str, tuple[int, int]] = {}
    try:
        for path in root.rglob("*"):
            if len(snapshot) >= _MAX_SNAPSHOT_FILES:
                break
            try:
                relative = path.relative_to(root)
                if any(part in _IGNORED_PARTS for part in relative.parts):
                    continue
                if not path.is_file() or path.is_symlink():
                    continue
                stat = path.stat()
                snapshot[relative.as_posix()] = (stat.st_mtime_ns, stat.st_size)
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    return snapshot


def _workspace_changes(
    before: dict[str, tuple[int, int]],
    after: dict[str, tuple[int, int]],
    *,
    limit: int = 100,
) -> tuple[str, ...]:
    created = [f"créé: {path}" for path in sorted(after.keys() - before.keys())]
    modified = [
        f"modifié: {path}"
        for path in sorted(before.keys() & after.keys())
        if before[path] != after[path]
    ]
    deleted = [f"supprimé: {path}" for path in sorted(before.keys() - after.keys())]
    return tuple((created + modified + deleted)[:max(1, limit)])


def _prepare_child() -> None:
    """Crée un groupe annulable et protège la voix avec une priorité basse."""
    try:
        os.setsid()
    except Exception:
        pass
    agent_brain._lower_priority()


def _stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=5)
    except Exception:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except Exception:
            pass


def _prompt(mission: str, workspace: Path, report_path: Path) -> str:
    return (
        "Tu es un sous-agent autonome d'ANO-GPT exécuté en arrière-plan.\n"
        "Accomplis intégralement la mission ci-dessous dans le dossier indiqué. "
        "Tu peux rechercher sur le web et utiliser les outils MCP d'ANO-GPT. "
        "Avant toute commande fondée sur un nom, une URL, une version ou une "
        "source externe, vérifie-la dans une source fiable ; ne devine jamais "
        "une URL, un propriétaire GitHub ou un paquet. Pour un dépôt, identifie "
        "d'abord le dépôt canonique et vérifie qu'il est accessible, puis clone "
        "en HTTPS dans le dossier demandé. Si la source est introuvable, ambiguë "
        "ou privée, n'improvise pas : rapporte précisément ce qui manque. "
        "Après chaque action importante, vérifie son effet réel et indique-le. "
        "Pour du code, inspecte d'abord le dépôt, préserve les changements "
        "existants, exécute les tests pertinents et ne publie rien à distance.\n"
        "Ne parle pas à l'utilisateur et n'appelle pas l'outil speak : ANO-GPT "
        "annoncera lui-même la fin au moment opportun. Termine ta sortie par un "
        "résumé français concret et très court des résultats et fichiers créés.\n"
        f"Dossier de travail : {workspace}\n"
        f"Le journal final sera archivé par ANO-GPT dans : {report_path}\n\n"
        f"MISSION :\n{mission.strip()}"
    )


def run_mission(
    mission: str,
    workspace: str | Path,
    report_path: str | Path,
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    cancelled: Callable[[], bool] | None = None,
    progress: Callable[[str], None] | None = None,
    show_terminal: bool = False,
) -> GhostResult:
    """Exécute une mission, avec annulation du groupe et rapport atomique."""
    binary = agent_brain.agent_binary()
    if binary is None:
        raise agent_brain.AgentUnavailable(
            "Aucun agent CLI disponible pour le Mode Fantôme."
        )
    mission = " ".join(str(mission or "").split())
    if not mission:
        raise ValueError("la mission est vide")
    workspace_path = Path(workspace).expanduser().resolve()
    if not workspace_path.is_dir():
        raise ValueError(f"dossier de travail introuvable : {workspace_path}")
    target = Path(report_path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    stdout_path = target.with_suffix(".stdout.tmp")
    stderr_path = target.with_suffix(".stderr.tmp")
    timeout_seconds = max(60, min(8 * 3600, int(timeout_seconds)))

    settings = agent_brain._config()
    command = [
        binary, "-p", _prompt(mission, workspace_path, target),
        "--mode", "accept-edits", "--output-format", "text",
        "--print-timeout", f"{timeout_seconds}s",
    ]
    model = str(settings.get("ghost_agent_model") or settings.get("agent_model") or "").strip()
    effort = str(settings.get("ghost_agent_effort") or settings.get("agent_effort") or "").strip()
    if model:
        command += ["--model", model]
    if effort:
        command += ["--effort", effort]
    environment = dict(os.environ)
    environment[agent_brain.LOOP_GUARD_ENV] = "1"
    environment[GHOST_MODE_ENV] = "1"

    before = _workspace_snapshot(workspace_path)
    started = time.monotonic()
    process: subprocess.Popen | None = None
    last_log = ""
    last_progress_at = 0.0
    status = "failed"
    exit_code: int | None = None
    try:
        with stdout_path.open("w", encoding="utf-8") as stdout_file, \
                stderr_path.open("w", encoding="utf-8") as stderr_file:
            process = subprocess.Popen(
                command,
                stdout=stdout_file,
                stderr=stderr_file,
                text=True,
                cwd=str(workspace_path),
                env=environment,
                preexec_fn=_prepare_child if os.name == "posix" else None,
            )
            # Vue facultative et non interactive : fermer Kitty ne tue pas agy,
            # et la conversation ANO-GPT continue. `tail --pid` quitte seul à
            # la fin du processus, sans terminal orphelin.
            if show_terminal and shutil.which("kitty"):
                try:
                    subprocess.Popen(
                        [
                            "kitty", "--title", f"ANO Agent · {workspace_path.name}",
                            "tail", f"--pid={process.pid}", "-n", "+1", "-f",
                            str(stdout_path),
                        ],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                except Exception:
                    pass
            while True:
                if cancelled and cancelled():
                    status = "cancelled"
                    _stop_process(process)
                    break
                if time.monotonic() - started >= timeout_seconds + 20:
                    status = "timed_out"
                    _stop_process(process)
                    break
                exit_code = process.poll()
                if exit_code is not None:
                    status = "completed" if exit_code == 0 else "failed"
                    break
                # Le rapport est aussi le journal vivant : le lire à faible
                # cadence évite un pipe bloquant et laisse le thread UI libre.
                now = time.monotonic()
                if progress is not None and now - last_progress_at >= 1.0:
                    try:
                        current_log = stdout_path.read_text(
                            encoding="utf-8", errors="replace"
                        )[-8_000:]
                        if current_log != last_log:
                            last_log = current_log
                            progress(current_log)
                    except OSError:
                        pass
                    last_progress_at = now
                time.sleep(0.25)
    except Exception:
        if process is not None:
            _stop_process(process)
        raise

    stdout = stdout_path.read_text(encoding="utf-8", errors="replace").strip()
    stderr = stderr_path.read_text(encoding="utf-8", errors="replace").strip()
    stdout_path.unlink(missing_ok=True)
    stderr_path.unlink(missing_ok=True)
    changes = _workspace_changes(before, _workspace_snapshot(workspace_path))
    if status == "cancelled":
        summary = "Mission annulée."
    elif status == "timed_out":
        summary = f"Mission interrompue après {timeout_seconds // 60} minutes."
    elif status == "completed":
        summary = _short_summary(stdout) or "Mission terminée sans résumé."
    else:
        summary = _short_summary(stderr) or "L'agent a échoué sans détail."

    report = (
        "# Rapport Agent Fantôme\n\n"
        f"- Statut : {status}\n"
        f"- Dossier : `{workspace_path}`\n"
        f"- Durée : {time.monotonic() - started:.1f} s\n"
        f"- Code de sortie : {exit_code}\n\n"
        f"## Mission\n\n{mission}\n\n"
        f"## Résultat\n\n{stdout or '(aucune sortie)'}\n"
    )
    if changes:
        report += "\n## Fichiers détectés par ANO-GPT\n\n" + "\n".join(
            f"- {item}" for item in changes
        ) + "\n"
    else:
        report += "\n## Fichiers détectés par ANO-GPT\n\nAucun changement détecté.\n"
    if stderr:
        report += f"\n## Erreurs\n\n```text\n{stderr[-8000:]}\n```\n"
    tmp = target.with_suffix(".md.tmp")
    tmp.write_text(report, encoding="utf-8")
    tmp.replace(target)
    return GhostResult(status, summary, str(target), exit_code, changes)


def _short_summary(text: str, limit: int = 300) -> str:
    lines = [" ".join(line.split()) for line in str(text or "").splitlines() if line.strip()]
    value = lines[-1] if lines else ""
    return value[:limit].rstrip()
