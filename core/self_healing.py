"""core/self_healing.py — Auto-réparation et diagnostic proactif des outils.

Analyse le journal de métriques d'appels (`core/tool_stats.py`) pour détecter les
outils en panne ou qui échouent fréquemment, générer un diagnostic clair, et
orchestrer une proposition de correctif via `dev_agent`.

Fonctionnement :
1. Détection : Analyse des échecs consécutifs ou du taux d'erreur sur une fenêtre glissante.
2. Rapport : Formulation naturelle (« Mon outil météo échoue depuis hier (HTTP 403)... »).
3. Proposition : Préparation d'une tâche de réparation ciblée pour `dev_agent`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from core import tool_stats

logger = logging.getLogger("anogpt.self_healing")

ACTIONS_DIR = Path(__file__).resolve().parent.parent / "actions"
CORE_DIR = Path(__file__).resolve().parent.parent / "core"


@dataclass
class ToolDiagnostic:
    tool: str
    total_calls: int
    failed_calls: int
    error_rate: float
    recent_errors: List[str] = field(default_factory=list)
    last_error_time: Optional[str] = None
    target_file: Optional[str] = None
    status: str = "healthy"  # "healthy", "degraded", "failing"

    def summary_phrase(self) -> str:
        """Phrase naturelle pour restitution vocale ou log."""
        if self.status == "healthy":
            return f"L'outil {self.tool} fonctionne normalement."
        err_hint = f" (dernière erreur : {self.recent_errors[-1]})" if self.recent_errors else ""
        return (
            f"L'outil {self.tool} est instable ({self.failed_calls}/{self.total_calls} échecs, "
            f"taux d'erreur {self.error_rate:.0%}){err_hint}."
        )


def _resolve_tool_source_file(tool_name: str) -> Optional[Path]:
    """Trouve le fichier Python principal associé à un outil."""
    candidates = [
        ACTIONS_DIR / f"{tool_name}.py",
        ACTIONS_DIR / f"{tool_name}_report.py",
        ACTIONS_DIR / f"{tool_name}_video.py",
        ACTIONS_DIR / f"{tool_name}_controller.py",
        CORE_DIR / f"{tool_name}.py",
        CORE_DIR / f"{tool_name}_service.py",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def analyze_tool_health(
    log_path: Optional[Path] = None,
    min_calls: int = 3,
    error_rate_threshold: float = 0.4,
    window_hours: float = 48.0,
) -> List[ToolDiagnostic]:
    """Analyse les logs d'appels pour détecter les outils en état critique."""
    entries = tool_stats.load(log_path)
    if not entries:
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    
    # Agrégation par outil
    tool_entries: Dict[str, List[dict]] = {}
    for e in entries:
        tool = e.get("tool")
        if not tool:
            continue
        ts_str = e.get("ts")
        if ts_str:
            try:
                # Format ISO UTC
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts < cutoff:
                    continue
            except Exception:
                pass
        tool_entries.setdefault(tool, []).append(e)

    diagnostics: List[ToolDiagnostic] = []

    for tool, logs in tool_entries.items():
        total = len(logs)
        if total < min_calls:
            continue

        errors = [e for e in logs if not e.get("ok", True)]
        failed = len(errors)
        error_rate = failed / total if total else 0.0

        # Vérification des 3 derniers appels (échecs consécutifs récents)
        recent_3 = logs[-3:]
        consecutive_failures = sum(1 for e in recent_3 if not e.get("ok", True)) == len(recent_3)

        status = "healthy"
        if error_rate >= error_rate_threshold or consecutive_failures:
            status = "failing" if (error_rate >= 0.7 or consecutive_failures) else "degraded"

        recent_err_msgs = [e.get("error", "") for e in errors if e.get("error")][-3:]
        last_time = errors[-1].get("ts") if errors else None
        source_path = _resolve_tool_source_file(tool)

        diag = ToolDiagnostic(
            tool=tool,
            total_calls=total,
            failed_calls=failed,
            error_rate=error_rate,
            recent_errors=recent_err_msgs,
            last_error_time=last_time,
            target_file=str(source_path) if source_path else None,
            status=status,
        )
        if diag.status != "healthy":
            diagnostics.append(diag)

    diagnostics.sort(key=lambda d: d.error_rate, reverse=True)
    return diagnostics


def generate_system_health_report(log_path: Optional[Path] = None) -> str:
    """Génère un rapport textuel complet sur la santé des outils."""
    failing = analyze_tool_health(log_path=log_path)
    if not failing:
        return "✅ Tous les outils fonctionnent normalement (aucun taux d'échec anormal)."

    lines = [f"⚠️ {len(failing)} outil(s) nécessitant une attention :"]
    for diag in failing:
        lines.append(f"- {diag.summary_phrase()}")
        if diag.target_file:
            lines.append(f"  Fichier cible : {diag.target_file}")
    return "\n".join(lines)


def prepare_healing_task(diagnostic: ToolDiagnostic) -> Dict[str, Any]:
    """Prépare les paramètres d'instruction pour dev_agent."""
    tool_file = diagnostic.target_file or f"actions/{diagnostic.tool}.py"
    errors_str = "\n".join(f"- {err}" for err in diagnostic.recent_errors) or "Erreurs d'exécution non spécifiées"
    
    prompt = (
        f"Diagnostiquer et corriger le dysfonctionnement de l'outil '{diagnostic.tool}'.\n"
        f"Fichier cible : {tool_file}\n"
        f"Statut : {diagnostic.status} (taux d'échec : {diagnostic.error_rate:.0%})\n"
        f"Erreurs récentes constatées :\n{errors_str}\n\n"
        "Vérifie le code source, identifie la cause (changement d'API, parsing, timeout, exception non gérée) "
        "et applique un correctif robuste avec gestion des erreurs et repli."
    )

    return {
        "description": prompt,
        "project_name": f"fix_{diagnostic.tool}",
        "language": "python",
        "tool": diagnostic.tool,
        "target_file": tool_file,
    }


def launch_auto_repair(
    tool_name: str,
    log_path: Optional[Path] = None,
    speak: Optional[Any] = None,
    player: Optional[Any] = None,
) -> str:
    """Déclenche la réparation assistée par dev_agent pour un outil donné."""
    from actions.dev_agent import dev_agent

    source = _resolve_tool_source_file(tool_name)
    target_file = str(source) if source else f"actions/{tool_name}.py"
    
    # Recherche des diagnostics récents
    diags = [d for d in analyze_tool_health(log_path=log_path, min_calls=1, error_rate_threshold=0.0) if d.tool == tool_name]
    recent_errors = diags[0].recent_errors if diags else ["Dysfonctionnement signalé par l'utilisateur"]

    diag = ToolDiagnostic(
        tool=tool_name,
        total_calls=1,
        failed_calls=1,
        error_rate=1.0,
        recent_errors=recent_errors,
        target_file=target_file,
        status="failing",
    )

    task_params = prepare_healing_task(diag)

    if speak:
        speak(f"J'analyse le code de {tool_name} pour préparer un correctif.")

    result = dev_agent(
        parameters=task_params,
        player=player,
        speak=speak,
    )
    return result
