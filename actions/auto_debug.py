"""actions/auto_debug.py — Action d'Auto-Debug Live et Perception d'Erreurs.

Permet à l'assistant de répondre instantanément aux demandes telles que :
- « C'est quoi ce bug dans mon terminal ? »
- « Pourquoi mon build C++ échoue ? »
- « Analyse ce traceback Python et corrige-le »
- « Explique cette panique Rust »
"""

from __future__ import annotations

from typing import Any, Dict, Optional
from pathlib import Path
import shutil
from core.auto_debug import auto_debug_live, generate_debug_diagnostic

from core import action_kit as kit


def _apply_verified_unified_patch(target: Path, diff: str) -> str:
    """Applique seulement un unified diff validé, jamais une réponse brute du modèle."""
    if not target.is_file() or not diff.lstrip().startswith("---") or "\n+++" not in diff or "\n@@" not in diff:
        return "Le correctif proposé n'est pas un patch unified diff valide ; aucun fichier n'a été modifié."
    if not shutil.which("patch"):
        return "L'outil patch n'est pas disponible ; aucun fichier n'a été modifié."
    # Les en-têtes fournis par le modèle ne choisissent jamais la cible : nous
    # les remplaçons par le fichier explicitement diagnostiqué.
    hunks_at = diff.find("\n@@")
    if hunks_at < 0:
        return "Le patch ne contient aucun hunk ; aucun fichier n'a été modifié."
    normalized = f"--- {target.name}\n+++ {target.name}" + diff[hunks_at:]
    dry_run = kit.run(
        ["patch", "--dry-run", "--batch", "--forward", target.name],
        cwd=target.parent, stdin=normalized, timeout=8,
    )
    if dry_run.returncode != 0:
        return "Le patch ne s'applique pas proprement au fichier actuel ; aucun fichier n'a été modifié."
    backup = target.with_suffix(target.suffix + ".bak")
    shutil.copy2(target, backup)
    result = kit.run(
        ["patch", "--batch", "--forward", target.name],
        cwd=target.parent, stdin=normalized, timeout=8,
    )
    if result.returncode != 0:
        shutil.copy2(backup, target)
        return "L'application du patch a échoué et la sauvegarde a été restaurée."
    return f"Correctif appliqué à {target.name} ; sauvegarde créée : {backup.name}."


@kit.action("auto_debug_action")
def auto_debug_action(
    parameters: Optional[Dict[str, Any]] = None,
    response=None,
    player=None,
    session_memory=None,
    speak=None,
) -> str:
    """
    Action principale d'auto-debug live.
    Parameters:
      query         : Question de l'utilisateur (ex: 'Pourquoi mon script plante ?')
      target        : 'active_window' | 'screen' (défaut : active_window)
      input_text    : Texte ou log d'erreur optionnel déjà fourni
      auto_apply    : Applique seulement un unified diff explicitement demandé,
                      validé par dry-run et sauvegardé en .bak
    """
    params = parameters or {}
    query = (
        params.get("query")
        or params.get("description")
        or params.get("text")
        or "Analyse l'erreur affichée et donne la solution."
    ).strip()

    target = params.get("target", "active_window")
    input_text = params.get("input_text") or params.get("error_text") or None
    auto_apply = bool(params.get("auto_apply", False))

    if speak:
        try:
            speak("J'analyse l'erreur sur ton écran.")
        except Exception:
            pass

    try:
        spoken_msg, diag = auto_debug_live(
            user_query=query,
            target_window=target,
            input_text=input_text,
            player=player,
        )
    except Exception as exc:
        from core.observability import tool_failure
        tool_failure(
            "live_auto_debug",
            exc,
            message=str(exc)[:300],
            args={"query": query[:80], "target": target},
        )
        return (
            f"Je n'ai pas pu analyser l'erreur ({type(exc).__name__}). "
            "Montre-moi le terminal et redemande."
        )

    # L'application reste un opt-in explicite. Le modèle doit fournir un patch
    # unifié qui passe d'abord un dry-run ; un bloc de code complet ne peut plus
    # écraser un fichier par erreur.
    if auto_apply and diag.code_diff and diag.parsed_error and diag.parsed_error.file_path:
        try:
            target_p = Path(diag.parsed_error.file_path)
            spoken_msg += " " + _apply_verified_unified_patch(target_p, diag.code_diff)
        except Exception as e:
            spoken_msg += f" (Correctif non appliqué : {e})"

    return spoken_msg


# Alias d'appel
auto_debug = auto_debug_action
