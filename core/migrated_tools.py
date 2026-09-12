"""core/migrated_tools.py — Démonstration de migration de 5 outils représentatifs ANO-GPT.

Illustre la puissance du registre déclaratif @tool :
1. system_status : Lecture seule, modèle de retour Pydantic, Literal options, parallel_safe
2. open_app      : Action GUI, arguments typés avec valeurs par défaut, alias, injection ExecutionContext
3. close_app     : Action sensible/destructrice, Literal scope, confirmation vocale obligatoire
4. weather_report: Requête externe, rate limiting "30/minute", alias multilingues, affichage de carte UI
5. shell_exec    : Exécution système critique, destructive=True, requires_voice_id=True, timeout 120s
"""

from __future__ import annotations

import asyncio
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from core.tool_registry import ExecutionContext, tool


# ══════════════════════════════════════════════════════════════════════════════
# 1. OUTIL 1 : SYSTEM_STATUS (Pydantic return model & Literal arguments)
# ══════════════════════════════════════════════════════════════════════════════

class SystemStatusReport(BaseModel):
    """Rapport structuré des métriques système matérielles et OS."""
    model_config = ConfigDict(extra="ignore")

    cpu_percent: float = Field(..., description="Pourcentage d'utilisation instantanée du CPU (0-100)")
    ram_percent: float = Field(..., description="Pourcentage de RAM occupée (0-100)")
    ram_used_gb: float = Field(default=0.0, description="RAM occupée en Gigaoctets")
    ram_total_gb: float = Field(default=0.0, description="RAM totale installée en Gigaoctets")
    disk_percent: float | None = Field(default=None, description="Pourcentage d'espace disque utilisé (0-100)")
    gpu_percent: float | None = Field(default=None, description="Pourcentage d'utilisation du GPU principal si présent")
    temperature_c: float | None = Field(default=None, description="Température mesurée du processeur en °C")
    uptime_hours: float | None = Field(default=None, description="Heures écoulées depuis le démarrage du système")
    summary: str = Field(default="", description="Résumé textuel destiné à la synthèse vocale")


@tool(
    name="system_status",
    description="Retourne les métriques CPU, RAM, GPU et disques en temps réel.",
    destructive=False,
    rate_limit="10/minute",
    requires_voice_id=False,
    parallel_safe=True,
    timeout_s=15.0,
)
async def get_system_status(
    detail_level: Literal["summary", "full"] = "summary"
) -> SystemStatusReport:
    """Retourne les métriques CPU, RAM, GPU et disques en temps réel.

    Args:
        detail_level: Niveau de détail attendu : 'summary' pour un condensé CPU/RAM rapide,
            'full' pour un relevé complet incluant températures, GPU et disques.

    Returns:
        SystemStatusReport contenant les métriques mesurées.
    """
    def _collect_metrics() -> SystemStatusReport:
        import psutil
        import time

        cpu = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory()
        uptime_h = (time.time() - psutil.boot_time()) / 3600.0

        disk_pct = None
        temp_c = None
        gpu_pct = None

        if detail_level == "full":
            try:
                disk = psutil.disk_usage("/")
                disk_pct = round(disk.percent, 1)
            except Exception:
                pass

            try:
                temps = psutil.sensors_temperatures()
                if temps:
                    first_sensor = next(iter(temps.values()))
                    if first_sensor:
                        temp_c = round(first_sensor[0].current, 1)
            except Exception:
                pass

            try:
                from actions.system_monitor import _get_gpu_usage
                gpu = _get_gpu_usage()
                if gpu >= 0:
                    gpu_pct = round(gpu, 1)
            except Exception:
                pass

        summary_parts = [f"CPU: {cpu}%", f"RAM: {mem.percent}%"]
        if gpu_pct is not None:
            summary_parts.append(f"GPU: {gpu_pct}%")
        if temp_c is not None:
            summary_parts.append(f"Temp: {temp_c}°C")
        if disk_pct is not None:
            summary_parts.append(f"Disque: {disk_pct}%")

        return SystemStatusReport(
            cpu_percent=cpu,
            ram_percent=mem.percent,
            ram_used_gb=round(mem.used / (1024 ** 3), 1),
            ram_total_gb=round(mem.total / (1024 ** 3), 1),
            disk_percent=disk_pct,
            gpu_percent=gpu_pct,
            temperature_c=temp_c,
            uptime_hours=round(uptime_h, 1),
            summary=" | ".join(summary_parts),
        )

    return await asyncio.to_thread(_collect_metrics)


# ══════════════════════════════════════════════════════════════════════════════
# 2. OUTIL 2 : OPEN_APP (Action GUI avec alias et injection de contexte)
# ══════════════════════════════════════════════════════════════════════════════

@tool(
    name="open_app",
    description=(
        "Opens any application on the computer. "
        "Use this whenever the user asks to open, launch, or start any app, "
        "website, or program. Always call this tool — never just say you opened it. "
        "Set hidden=true when the user wants an app running without seeing its window."
    ),
    destructive=False,
    rate_limit="30/minute",
    requires_voice_id=False,
    aliases={"app": "app_name", "application": "app_name", "name": "app_name"},
    timeout_s=20.0,
)
async def open_app_tool(
    app_name: str,
    workspace: int | None = None,
    hidden: bool = False,
    context: ExecutionContext | None = None,
) -> str:
    """Ouvre une application ou un programme sur le poste de travail.

    Args:
        app_name: Nom exact ou usuel de l'application (ex: 'Chrome', 'Kitty', 'Spotify').
        workspace: Numéro de bureau Hyprland (1-10) dans lequel positionner la fenêtre.
        hidden: Si True, lance l'application sur le bureau spécial invisible en arrière-plan.

    Returns:
        Message de confirmation de lancement.
    """
    from actions.open_app import open_app as raw_open_app

    ctx = context or ExecutionContext()
    params = {
        "app_name": app_name,
        "workspace": workspace,
        "hidden": hidden,
    }

    result = await asyncio.to_thread(
        raw_open_app,
        parameters=params,
        response=None,
        player=ctx.ui,
        session_memory=ctx.session_memory,
    )
    return result or f"Application '{app_name}' lancée avec succès."


# ══════════════════════════════════════════════════════════════════════════════
# 3. OUTIL 3 : CLOSE_APP (Action sensible/destructrice avec confirmation vocale)
# ══════════════════════════════════════════════════════════════════════════════

@tool(
    name="close_app",
    description=(
        "Closes/quits a running application. Use whenever the user asks to "
        "close, quit, stop, or kill an app that is currently open. "
        "Pass target='all' only if the user explicitly asked to close every window."
    ),
    destructive=True,
    requires_voice_id=True,
    rate_limit="20/minute",
    aliases={"app": "app_name", "application": "app_name", "name": "app_name"},
    timeout_s=20.0,
)
async def close_app_tool(
    app_name: str,
    target: Literal["last", "active", "all"] = "active",
    workspace: int | None = None,
    description: str = "",
    list_instances: bool = False,
    context: ExecutionContext | None = None,
) -> str:
    """Ferme une application ou liste ses fenêtres actives.

    Args:
        app_name: Nom de l'application à fermer (ex: 'VLC', 'Chrome', 'Kitty').
        target: Portée de la fermeture : 'last' pour celle ouverte récemment,
            'active' pour la fenêtre courante, 'all' pour toutes les fenêtres.
        workspace: Numéro de bureau spécifique pour restreindre la fermeture.
        description: Formulation verbatim de l'utilisateur pour désambiguïser.
        list_instances: Si True, liste les instances sans les fermer.

    Returns:
        Compte-rendu de fermeture ou liste des instances trouvées.
    """
    from actions.close_app import close_app as raw_close_app

    ctx = context or ExecutionContext()
    params = {
        "app_name": app_name,
        "target": target,
        "workspace": workspace,
        "description": description,
        "list_instances": list_instances,
    }

    result = await asyncio.to_thread(
        raw_close_app,
        parameters=params,
        response=None,
        player=ctx.ui,
        session_memory=ctx.session_memory,
    )
    return result or f"Application '{app_name}' fermée."


# ══════════════════════════════════════════════════════════════════════════════
# 4. OUTIL 4 : WEATHER_REPORT (Requête externe avec rate limiting et carte UI)
# ══════════════════════════════════════════════════════════════════════════════

@tool(
    name="weather_report",
    description="Fournit le bulletin météorologique et les prévisions pour une ville.",
    destructive=False,
    rate_limit="30/minute",
    requires_voice_id=False,
    parallel_safe=True,
    aliases={"ville": "city", "location": "city", "commune": "city"},
    timeout_s=25.0,
)
async def weather_report_tool(
    city: str,
    context: ExecutionContext | None = None,
) -> str:
    """Obtient le bulletin météo pour une localité donnée.

    Args:
        city: Nom de la ville ou localité (ex: 'Paris', 'Lyon', 'Tokyo').

    Returns:
        Description textuelle complète des conditions météo.
    """
    from actions.weather_report import get_last_weather_card, weather_action

    ctx = context or ExecutionContext()
    params = {"city": city}

    report = await asyncio.to_thread(
        weather_action,
        parameters=params,
        player=ctx.ui,
    )

    # Affichage de la carte météo sur l'interface graphique si UI disponible
    card_data = get_last_weather_card()
    if card_data and ctx.ui and hasattr(ctx.ui, "show_card"):
        try:
            ctx.ui.show_card("result", "Météo", card_data)
        except Exception:
            pass

    return report or f"Météo pour {city} transmise."


# ══════════════════════════════════════════════════════════════════════════════
# 5. OUTIL 5 : SHELL_EXEC (Exécution système sensible, timeout 120s, voice lock)
# ══════════════════════════════════════════════════════════════════════════════

@tool(
    name="shell_exec",
    description=(
        "Exécute une commande système bash sur la machine locale. "
        "Action sensible et réservée à l'utilisateur authentifié."
    ),
    destructive=True,
    requires_voice_id=True,
    rate_limit="15/minute",
    timeout_s=120.0,
)
async def shell_exec_tool(
    command: str,
    context: ExecutionContext | None = None,
) -> str:
    """Exécute une commande en ligne de commande avec contrôle de sécurité.

    Args:
        command: La commande shell exacte à exécuter dans l'interpréteur bash.

    Returns:
        Sortie standard ou résultat de la commande.
    """
    from actions.shell_exec import shell_exec as raw_shell_exec

    ctx = context or ExecutionContext()
    params = {"command": command}

    output = await asyncio.to_thread(
        raw_shell_exec,
        parameters=params,
        player=ctx.ui,
    )

    result = output or "Commande exécutée."

    # Gestion des commandes nécessitant une confirmation interactive par carte UI
    if result.startswith("[NEEDS_CONFIRM] ") and ctx.ui and hasattr(ctx.ui, "show_card"):
        cmd_risky = result[len("[NEEDS_CONFIRM] "):]
        try:
            ctx.ui.show_card(
                "confirmation",
                "Commande risquée détectée",
                f"```bash\n{cmd_risky}\n```\nCette action peut modifier le système. Confirmer l'exécution ?",
                [
                    {
                        "label": "Confirmer",
                        "primary": True,
                        "callback": lambda: getattr(ctx.ui, "on_text_command", lambda _: None)(
                            f"confirme la commande : {cmd_risky}"
                        ),
                    },
                    {
                        "label": "Annuler",
                        "callback": lambda: getattr(ctx.ui, "on_text_command", lambda _: None)(
                            "annule l'exécution"
                        ),
                    },
                ],
            )
            return f"Commande risquée détectée. Demande de confirmation envoyée à l'écran : {cmd_risky}"
        except Exception:
            pass

    return result


# ══════════════════════════════════════════════════════════════════════════════
# 6. OUTIL 6 : POINT_ON_SCREEN (Overlay Visuel Wayland)
# ══════════════════════════════════════════════════════════════════════════════

@tool(
    name="point_on_screen",
    description=(
        "Visually highlights, points out, or traces a trajectory directly on the user's screen "
        "using a futuristic neon HUD overlay instead of just describing locations in words. "
        "Use whenever explaining where an element, button, menu item, or region is located."
    ),
    destructive=False,
    rate_limit="30/minute",
    requires_voice_id=False,
    parallel_safe=True,
    timeout_s=10.0,
)
async def point_on_screen(
    description: str,
    coordinates: list[int],
    mode: Literal["auto", "highlight", "laser", "path"] = "auto",
    duration: float = 3.0,
    context: ExecutionContext | None = None,
) -> str:
    """Active le pointeur visuel sur l'écran de l'utilisateur.

    Args:
        description: Description courte ou libellé de l'élément ciblé.
        coordinates: Coordonnées en pixels ou normalisées : [x, y], [x, y, w, h] ou [x1, y1, x2, y2, ...].
        mode: Mode de rendu ('auto', 'highlight', 'laser', 'path').
        duration: Durée d'affichage en secondes (défaut 3.0s).
        context: Contexte d'exécution injecté automatiquement.

    Returns:
        Confirmation de l'annotation visuelle affichée.
    """
    from ui.visual_pointer import get_visual_pointer

    ctx = context or ExecutionContext()
    vp = getattr(ctx.ui, "visual_pointer", None) if ctx.ui else get_visual_pointer()
    return vp.point_on_screen(description, coordinates, mode=mode, duration=duration)

