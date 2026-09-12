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

    if action in {"repair", "reparer", "fix", "corriger"}:
        if not tool_name:
            # Si pas d'outil spécifié, chercher le premier outil en échec
            failing = analyze_tool_health()
            if not failing:
                msg = "Aucun outil défaillant détecté. Le système est en parfait état de marche."
                if speak:
                    speak(msg)
                return msg
            tool_name = failing[0].tool

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
