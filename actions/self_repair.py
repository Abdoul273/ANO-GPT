"""actions/self_repair.py — Diagnostic et auto-réparation des outils ANO-GPT."""

from __future__ import annotations

from typing import Any, Dict, Optional

from core.self_healing import (
    analyze_tool_health,
    generate_system_health_report,
    launch_auto_repair,
)


def self_repair(
    parameters: Optional[Dict[str, Any]] = None,
    player: Optional[Any] = None,
    speak: Optional[Any] = None,
    session_memory: Optional[Any] = None,
) -> str:
    """Diagnostique la santé des outils et lance une réparation si demandée.

    Parameters:
      action: "diagnose" (par défaut) | "repair"
      tool: nom de l'outil à réparer (ex: "weather", "email", "reminder")
    """
    p = parameters or {}
    action = str(p.get("action", "diagnose")).strip().lower()
    tool_name = str(p.get("tool", "")).strip().lower()
    from core import auto_fix, incident_log

    if action in {"last_error", "error", "erreur", "status_error", "what_happened"}:
        inc = incident_log.last(unresolved_only=False)
        if inc is None:
            return "Aucune erreur enregistrée récemment."
        text = incident_log.describe(inc)
        if inc.fixed:
            text += " Elle a déjà été corrigée."
        else:
            text += " Dis « corrige » et je m'en occupe."
        if player and hasattr(player, "show_card"):
            player.show_card("error", f"Dernière erreur — {inc.source}",
                             f"{text}\n\n```\n{inc.traceback[-900:]}\n```")
        return text

    if action in {"restart", "redemarre", "redémarre", "reload"}:
        return auto_fix.request_restart(speak)

    if action in {"repair", "reparer", "fix", "corriger", "corrige"}:
        # Sans outil nommé, ou avec un outil qui correspond à un incident
        # réel : réparation du code à partir de la pile d'appel enregistrée.
        inc = incident_log.find(tool_name or str(p.get("query") or ""), strict=bool(tool_name))
        if inc is not None:
            if inc.fixed:
                return (f"Cette erreur ({inc.source}) est déjà corrigée. "
                        "Dis « redémarre » si ce n'est pas encore appliqué.")
            return auto_fix.repair(inc, player=player, speak=speak)
        latest = incident_log.last(unresolved_only=True)
        if latest is not None:
            return (f"Je n'ai aucune erreur enregistrée pour « {tool_name} ». La dernière erreur "
                    f"concerne {latest.source} : {latest.message} Dis « corrige » pour celle-là.")
        if not tool_name:
            return "Aucune erreur enregistrée récemment : il n'y a rien à corriger."
        # Outil nommé sans incident enregistré : l'ancien parcours par les
        # statistiques d'appels reste disponible.
        if player and hasattr(player, "write_log"):
            player.write_log(f"SYS : Lancement de l'auto-réparation pour '{tool_name}'...")

        return launch_auto_repair(
            tool_name=tool_name,
            speak=speak,
            player=player,
        )

    # Mode par défaut : diagnostic / bilan de santé
    report = generate_system_health_report()
    if player and hasattr(player, "show_card"):
        failing = analyze_tool_health()
        status_kind = "warning" if failing else "success"
        player.show_card(status_kind, "Diagnostic Outils & Auto-Réparation", report)

    if speak:
        failing = analyze_tool_health()
        if failing:
            speak(f"J'ai détecté des erreurs récentes sur {len(failing)} outil : {failing[0].tool}.")
        else:
            speak("Tous les outils fonctionnent nominalement.")

    return report
