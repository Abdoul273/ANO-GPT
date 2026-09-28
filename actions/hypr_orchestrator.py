#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hypr_orchestrator.py — Orchestrateur Dynamique Hyprland pour ANO-GPT.

Capacités :
- Déplacement dynamique et intelligent de fenêtres sur les workspaces dédiés selon leur rôle
- Reclassement en un seul mot (« Organise mon espace de travail » / « Range les fenêtres »)
- Presets d'espaces de travail (« Preset DevSecOps », « Mode Focus », « Preset Dual Code »)
- Cartographie des applications par profils de productivité :
    Workspace 1 : Dev / IDE (VS Code, Neovim, Zed, Codium...)
    Workspace 2 : Web & Docs (Firefox, Chrome, Brave, Zen...)
    Workspace 3 : Terminal & DevSecOps (Kitty, Alacritty, Foot, Wezterm...)
    Workspace 4 : Communication (Discord, Slack, Telegram, Signal...)
    Workspace 5 : Média & Création (Spotify, VLC, MPV, GIMP, OBS...)
    Workspace 6 : Monitoring & Ops (btop, ANO-GPT Dashboard, SysMonitor...)
- Respect des fenêtres épinglées / flottantes et animation fluide Wayland.
"""

from __future__ import annotations

import os
import re
import time
from core import action_kit as kit
from actions.window_instances import dispatch_hyprland, move_window_to_workspace
from pathlib import Path
from typing import Any, Dict, List, Optional

# ════════════════════════════════════════════════════════════════════════════
# Configuration & Cartographie des rôles
# ════════════════════════════════════════════════════════════════════════════

DEFAULT_WORKSPACE_ROLES: Dict[str, int] = {
    # Workspace 1 : Dev & IDE
    "antigravity-ide": 1,
    "antigravity": 1,
    "code": 1,
    "code-oss": 1,
    "codium": 1,
    "vscodium": 1,
    "neovide": 1,
    "nvim": 1,
    "zed": 1,
    "sublime_text": 1,
    "subl": 1,
    "kate": 1,
    "clion": 1,
    "pycharm": 1,
    "intellij": 1,
    "android-studio": 1,
    "cursor": 1,

    # Workspace 2 : Web & Documentation
    "firefox": 2,
    "firefox-developer-edition": 2,
    "chromium": 2,
    "google-chrome": 2,
    "google-chrome-stable": 2,
    "brave": 2,
    "brave-browser": 2,
    "zen": 2,
    "zen-alpha": 2,
    "zen-browser": 2,
    "vivaldi": 2,
    "opera": 2,
    "microsoft-edge": 2,
    "epiphany": 2,

    # Workspace 3 : Terminal & DevSecOps
    "kitty": 3,
    "alacritty": 3,
    "foot": 3,
    "footclient": 3,
    "wezterm": 3,
    "gnome-terminal": 3,
    "konsole": 3,
    "xterm": 3,
    "tilix": 3,
    "urxvt": 3,

    # Workspace 4 : Communication & Messagerie
    "discord": 4,
    "vesktop": 4,
    "slack": 4,
    "telegram": 4,
    "telegram-desktop": 4,
    "telegramdesktop": 4,
    "signal": 4,
    "signal-desktop": 4,
    "whatsapp": 4,
    "zapzap": 4,
    "thunderbird": 4,
    "teams": 4,
    "zoom": 4,
    "element": 4,

    # Workspace 5 : Média & Création
    "spotify": 5,
    "vlc": 5,
    "mpv": 5,
    "celluloid": 5,
    "rhythmbox": 5,
    "lollypop": 5,
    "audacious": 5,
    "gimp": 5,
    "inkscape": 5,
    "blender": 5,
    "krita": 5,
    "obs": 5,
    "audacity": 5,
    "kdenlive": 5,

    # Workspace 6 : Monitoring & Dashboard Ops
    "btop": 6,
    "htop": 6,
    "gnome-system-monitor": 6,
    "jarvis-dashboard": 6,
    "ano-gpt": 6,
    "wireshark": 6,
    "portainer": 6,
}


def _hypr_env() -> dict:
    """Environnement Hyprland restauré."""
    env = {**os.environ}
    if not env.get("XDG_RUNTIME_DIR"):
        candidate = Path(f"/run/user/{os.getuid()}")
        if candidate.exists():
            env["XDG_RUNTIME_DIR"] = str(candidate)
    if not env.get("HYPRLAND_INSTANCE_SIGNATURE"):
        runtime_dir = Path(env.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
        hypr_dir = runtime_dir / "hypr"
        try:
            if hypr_dir.exists():
                instances = sorted(
                    (d for d in hypr_dir.iterdir() if d.is_dir()),
                    key=lambda d: d.stat().st_mtime,
                    reverse=True,
                )
                if instances:
                    env["HYPRLAND_INSTANCE_SIGNATURE"] = instances[0].name
        except Exception:
            pass
    return env


def _hyprctl_json(command: str) -> Any:
    """Interroge hyprctl au format JSON, via le socle partagé."""
    return kit.hypr_json(*command.split(), default=None)


# ════════════════════════════════════════════════════════════════════════════
# Classification & Résolution des fenêtres
# ════════════════════════════════════════════════════════════════════════════

class HyprOrchestrator:
    """Orchestrateur intelligent de fenêtres et d'espaces de travail Hyprland."""

    @classmethod
    def get_clients(cls) -> List[Dict[str, Any]]:
        """Récupère la liste des fenêtres ouvertes avec métadonnées."""
        clients = _hyprctl_json("clients")
        if isinstance(clients, list):
            return clients
        return []

    @classmethod
    def get_active_workspace(cls) -> int:
        """Récupère le numéro du workspace actuellement actif."""
        ws_info = _hyprctl_json("activeworkspace")
        if isinstance(ws_info, dict) and "id" in ws_info:
            return int(ws_info["id"])
        return 1

    @classmethod
    def classify_window(cls, client: Dict[str, Any]) -> int:
        """Détermine le workspace cible optimal pour une fenêtre."""
        app_class = (client.get("class") or "").lower().strip()
        initial_class = (client.get("initialClass") or "").lower().strip()
        title = (client.get("title") or "").lower().strip()

        # 1. Correspondance exacte sur la classe
        for k, ws in DEFAULT_WORKSPACE_ROLES.items():
            if k == app_class or k == initial_class:
                return ws

        # 2. Correspondance floue sur la classe
        for k, ws in DEFAULT_WORKSPACE_ROLES.items():
            if k in app_class or k in initial_class:
                return ws

        # 3. Correspondance contextuelle sur le titre
        if any(w in title for w in ["visual studio code", "vscodium", "neovim", "zed"]):
            return 1
        if any(w in title for w in ["mozilla firefox", "google chrome", "brave", "github", "gitlab", "stackoverflow"]):
            return 2
        if any(w in title for w in ["terminal", "bash", "zsh", "fish", "tmux"]):
            return 3
        if any(w in title for w in ["discord", "slack", "telegram", "whatsapp"]):
            return 4
        if any(w in title for w in ["spotify", "youtube", "vlc", "mpv"]):
            return 5
        if any(w in title for w in ["btop", "htop", "monitoring", "dashboard"]):
            return 6

        # Défaut : workspace courant ou 1
        current_ws = client.get("workspace", {}).get("id")
        return int(current_ws) if current_ws and current_ws > 0 else 1

    @classmethod
    def organize_workspaces(cls) -> str:
        """
        Reclasse dynamiquement TOUTES les fenêtres ouvertes sur leurs workspaces dédiés.
        """
        clients = cls.get_clients()
        if not clients:
            return "Aucune fenêtre ouverte détectée sous Hyprland."

        moved_count = 0
        failed: List[str] = []
        workspace_summary: Dict[int, List[str]] = {1: [], 2: [], 3: [], 4: [], 5: [], 6: []}

        for c in clients:
            addr = c.get("address", "")
            (c.get("title") or "Inconnu")[:30]
            app_class = c.get("class") or "App"
            current_ws = c.get("workspace", {}).get("id", 1)
            target_ws = cls.classify_window(c)
            actual = current_ws

            # Nom court lisible

            if current_ws != target_ws:
                # Hyprland 0.56 emploie le dispatcher Lua. Relire l'adresse
                # après l'ordre : son code 0 n'atteste pas le déplacement.
                sent = bool(addr) and move_window_to_workspace(
                    f"address:{addr}", target_ws, follow=False,
                )
                if sent:
                    for _ in range(4):
                        fresh = next((w for w in cls.get_clients()
                                      if w.get("address") == addr), None)
                        actual = (fresh or {}).get("workspace", {}).get("id")
                        if actual == target_ws:
                            break
                        time.sleep(0.1)
                if sent and actual == target_ws:
                    moved_count += 1
                else:
                    failed.append(f"{app_class} (bureau {current_ws} → {target_ws})")
            
            reported_ws = current_ws if (current_ws != target_ws and actual != target_ws) else target_ws
            if reported_ws in workspace_summary:
                workspace_summary[reported_ws].append(app_class)

        # Construction du rapport
        role_labels = {
            1: "💻 Dev & IDE",
            2: "🌐 Web & Docs",
            3: "⚡ Terminal & DevSecOps",
            4: "💬 Comms & Chat",
            5: "🎨 Média & Création",
            6: "📊 Monitoring & Ops",
        }

        report = [f"🚀 Organisation dynamique terminée ({moved_count} fenêtre(s) replacée(s)) :\n"]
        if failed:
            report[0] = f"Organisation incomplète ({moved_count} déplacée(s), {len(failed)} échec(s)) :\n"
            report.append("  Déplacements non confirmés : " + ", ".join(failed))
        for ws_num in sorted(workspace_summary.keys()):
            apps = workspace_summary[ws_num]
            if apps:
                apps_str = ", ".join(sorted(set(apps)))
                report.append(f"  Bureau {ws_num} [{role_labels.get(ws_num, 'Général')}] : {apps_str}")

        return "\n".join(report)

    @classmethod
    def _focus_workspace(cls, number: int) -> bool:
        sent = dispatch_hyprland(
            legacy_cmd="workspace", legacy_args=str(number),
            lua_cmd=f'hl.dsp.focus({{ workspace = "{number}" }})',
        )
        return bool(sent and cls.get_active_workspace() == number)

    @classmethod
    def _ensure_coding_ide(cls) -> bool:
        def present() -> bool:
            return any("antigravity" in str(c.get("class") or "").casefold()
                       for c in cls.get_clients())

        if present():
            return True
        if not kit.which("antigravity-ide") or kit.spawn(["antigravity-ide"]) is None:
            return False
        for _ in range(20):
            if present():
                return True
            time.sleep(0.25)
        return False

    @classmethod
    def apply_preset(cls, preset_name: str) -> str:
        """Applique un preset d'agencement de bureau."""
        preset = preset_name.lower().strip()
        
        if any(w in preset for w in ["devsecops", "dev", "code", "coding"]):
            # Le preset coding ouvre l'IDE réellement utilisé sur cette machine.
            ide_open = cls._ensure_coding_ide()
            org_res = cls.organize_workspaces()
            focused = cls._focus_workspace(1)
            status = "Focus sur le Bureau 1 confirmé." if focused else "Focus sur le Bureau 1 non confirmé."
            ide_status = ("Antigravity IDE détecté." if ide_open
                          else "Antigravity IDE n'a pas pu être ouvert ; aucune fenêtre IDE à placer.")
            outcome = ("Preset coding appliqué" if focused and ide_open and "incomplète" not in org_res
                       else "Preset coding incomplet")
            return f"{outcome}. {status} {ide_status}\n{org_res}"

        if any(w in preset for w in ["monitoring", "ops", "system"]):
            org_res = cls.organize_workspaces()
            focused = cls._focus_workspace(6)
            return f"Preset Monitoring : focus {'confirmé' if focused else 'non confirmé'} sur le Bureau 6.\n{org_res}"

        if any(w in preset for w in ["web", "docs", "recherche"]):
            org_res = cls.organize_workspaces()
            focused = cls._focus_workspace(2)
            return f"Preset Recherche & Docs : focus {'confirmé' if focused else 'non confirmé'} sur le Bureau 2.\n{org_res}"

        if any(w in preset for w in ["comms", "chat", "message"]):
            org_res = cls.organize_workspaces()
            focused = cls._focus_workspace(4)
            return f"Preset Communication : focus {'confirmé' if focused else 'non confirmé'} sur le Bureau 4.\n{org_res}"

        return cls.organize_workspaces()

    @classmethod
    def move_window_to_ws(cls, target_query: str, workspace: int | str) -> str:
        """Déplace une fenêtre spécifique vers un workspace donné."""
        # Un bureau inconnu ne doit jamais se transformer silencieusement en 1.
        try:
            target_ws_int = int(workspace)
        except (ValueError, TypeError):
            w_str = str(workspace).lower()
            ord_map = {"premier": 1, "deuxieme": 2, "deuxième": 2, "troisieme": 3, "troisième": 3,
                       "quatrieme": 4, "quatrième": 4, "cinquieme": 5, "cinquième": 5, "sixieme": 6, "sixième": 6}
            target_ws_int = next((v for k, v in ord_map.items() if k in w_str), 0)
        if target_ws_int < 1:
            return f"Bureau invalide : {workspace}."

        query_clean = (target_query or "").lower().strip()
        clients = cls.get_clients()
        if not clients:
            return "Aucune fenêtre trouvée."
        matched = None
        current_window = query_clean in ("", "cette fenêtre", "cette fenetre", "la fenêtre",
                                         "la fenetre", "fenêtre", "fenetre", "active", "actuelle")
        if current_window:
            active = _hyprctl_json("activewindow")
            address = active.get("address") if isinstance(active, dict) else None
            matched = next((c for c in clients if c.get("address") == address), None)
        else:
            for c in clients:
                app_class = (c.get("class") or "").lower()
                title = (c.get("title") or "").lower()
                if query_clean in app_class or query_clean in title:
                    matched = c
                    break

        if not matched:
            return ("Aucune fenêtre active à déplacer." if current_window else
                    f"Aucune fenêtre ne correspond à '{target_query}'.")

        addr = matched.get("address", "")
        if not addr:
            return "Adresse de fenêtre introuvable ; déplacement annulé."
        app_name = matched.get("title") or matched.get("class") or "active"
        before = (matched.get("workspace") or {}).get("id")
        if str(before) == str(target_ws_int):
            return f"Fenêtre «{app_name}» déjà sur le bureau {target_ws_int}."
        sent = move_window_to_workspace(f"address:{addr}", target_ws_int, follow=False)
        if sent:
            for _ in range(7):
                current = next((c for c in cls.get_clients() if c.get("address") == addr), None)
                if current and str((current.get("workspace") or {}).get("id")) == str(target_ws_int):
                    return f"Déplacement confirmé : fenêtre «{app_name}» sur le bureau {target_ws_int}."
                time.sleep(0.1)
        return f"Déplacement non confirmé pour «{app_name}» vers le bureau {target_ws_int}."


# ════════════════════════════════════════════════════════════════════════════
# Parsing vocal local & Dispatcher
# ════════════════════════════════════════════════════════════════════════════

def parse_hypr_orchestrator_intent(text: str) -> Optional[Dict[str, Any]]:
    """Analyse locale en langage naturel pour l'orchestration Hyprland."""
    text_clean = text.lower().strip()
    text_clean = re.sub(r"\b(s'il te pla[iî]t|stp|please|peux-tu|tu peux|je veux|j'aimerais)\b", "", text_clean).strip()

    # 1. Organisation globale des fenêtres
    if any(k in text_clean for k in ["organise", "organiser", "range", "ranger", "reorganise", "réorganise", "classe", "classer"]) and any(k in text_clean for k in ["fenetre", "fenêtres", "bureau", "workspaces", "workspace", "espace de travail", "ecran", "écran"]):
        return {"action": "organize"}

    # 2. Presets d'espaces de travail
    m_preset = re.search(r"(?:preset|mode|profil)\s+(devsecops|dev|code|monitoring|web|docs|comms|focus)", text_clean)
    if m_preset:
        return {"action": "preset", "preset": m_preset.group(1)}

    if "preset devsecops" in text_clean or "mode devsecops" in text_clean:
        return {"action": "preset", "preset": "devsecops"}

    # 3. Déplacement dynamique d'une fenêtre vers un workspace
    m_move = re.search(r"(?:d[ée]place|envoie|mets?|bouge)\s+(.+?)\s+(?:sur|vers|au|dans)\s+(?:le\s+)?(?:bureau|workspace|ws|espace)\s+(\d+|[a-zA-Zéèê]+)", text_clean)
    if m_move:
        target_app = m_move.group(1).strip()
        if target_app.lower() in ("cette fenêtre", "cette fenetre", "la fenêtre", "la fenetre", "fenêtre", "fenetre"):
            target_app = ""
        elif target_app.lower().startswith("la fenêtre ") or target_app.lower().startswith("la fenetre "):
            target_app = target_app.split(" ", 2)[2]
        target_ws = m_move.group(2).strip()
        return {"action": "move_window", "target": target_app, "workspace": target_ws}

    return None


@kit.action("hypr_orchestrator_control")
def hypr_orchestrator_control(parameters: dict | None = None, player=None, **_kwargs) -> str:
    """
    Point d'entrée de l'Orchestrateur Hyprland pour ANO-GPT.
    
    Paramètres :
      action      : 'organize' | 'preset' | 'move_window' | 'list'
      preset      : nom du preset ('devsecops', 'coding', 'monitoring', 'web')
      target      : nom de la fenêtre/app cible
      workspace   : numéro ou ordinal du bureau cible
      description : phrase en langage naturel
    """
    params = parameters or {}
    description = (params.get("description") or "").strip()
    action = (params.get("action") or "").strip().lower()

    if description and not action:
        intent = parse_hypr_orchestrator_intent(description)
        if intent:
            action = intent.get("action", "")
            if "preset" in intent:
                params["preset"] = intent["preset"]
            if "target" in intent:
                params["target"] = intent["target"]
            if "workspace" in intent:
                params["workspace"] = intent["workspace"]

    if not action:
        if params.get("preset"):
            action = "preset"
        elif params.get("workspace") or params.get("target"):
            action = "move_window"

    if action in ("organize", "reorganize", "clean_workspaces"):
        return HyprOrchestrator.organize_workspaces()

    if action in ("preset", "apply_preset"):
        preset_name = params.get("preset") or params.get("value") or "devsecops"
        return HyprOrchestrator.apply_preset(preset_name)

    if action in ("move_window", "move_to_workspace"):
        target = params.get("target") or params.get("app") or ""
        ws = params.get("workspace") or params.get("value") or 1
        return HyprOrchestrator.move_window_to_ws(target, ws)

    if action in ("list", "clients", "status"):
        clients = HyprOrchestrator.get_clients()
        if not clients:
            return "Aucune fenêtre ouverte sous Hyprland."
        lines = [f"• {c.get('class', 'App')} (Bureau {c.get('workspace', {}).get('id', 1)}) : {c.get('title', '')[:40]}" for c in clients]
        return "Fenêtres ouvertes :\n" + "\n".join(lines)

    # Si rien de spécifique n'est précisé mais qu'on a appelé l'orchestrateur, organiser
    if description:
        return HyprOrchestrator.organize_workspaces()

    return "Aucune action d'orchestration Hyprland spécifiée."


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        print(hypr_orchestrator_control({"description": " ".join(sys.argv[1:])}))
    else:
        print("Hyprland Orchestrator prêt.")
