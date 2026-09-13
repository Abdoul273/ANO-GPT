"""Tests unitaires et d'intégration pour actions/hypr_orchestrator.py."""

from actions.hypr_orchestrator import (
    HyprOrchestrator,
    parse_hypr_orchestrator_intent,
    hypr_orchestrator_control,
)


# ════════════════════════════════════════════════════════════════════════════
# 1. Parsing vocal local de l'Orchestrateur Hyprland
# ════════════════════════════════════════════════════════════════════════════

def test_parse_hypr_orchestrator_phrases():
    p_org1 = parse_hypr_orchestrator_intent("organise mon espace de travail")
    assert p_org1 is not None
    assert p_org1["action"] == "organize"

    p_org2 = parse_hypr_orchestrator_intent("range les fenêtres sur leurs workspaces dédiés")
    assert p_org2 is not None
    assert p_org2["action"] == "organize"

    p_preset = parse_hypr_orchestrator_intent("active le preset devsecops")
    assert p_preset is not None
    assert p_preset["action"] == "preset"
    assert p_preset["preset"] == "devsecops"

    p_move = parse_hypr_orchestrator_intent("déplace la fenêtre VS Code sur le bureau 1")
    assert p_move is not None
    assert p_move["action"] == "move_window"
    assert "vs code" in p_move["target"].lower()
    assert p_move["workspace"] == "1"


# ════════════════════════════════════════════════════════════════════════════
# 2. Classification intelligente des fenêtres par rôle
# ════════════════════════════════════════════════════════════════════════════

def test_hypr_window_classification():
    # Dev / IDE -> Workspace 1
    assert HyprOrchestrator.classify_window({"class": "Code", "title": "main.py - ANO-GPT"}) == 1
    assert HyprOrchestrator.classify_window({"class": "neovide", "title": "init.lua"}) == 1
    assert HyprOrchestrator.classify_window({"class": "zed", "title": "Rust project"}) == 1

    # Web & Docs -> Workspace 2
    assert HyprOrchestrator.classify_window({"class": "firefox", "title": "GitHub - Google DeepMind"}) == 2
    assert HyprOrchestrator.classify_window({"class": "google-chrome", "title": "Documentation Python"}) == 2
    assert HyprOrchestrator.classify_window({"class": "zen-browser", "title": "StackOverflow"}) == 2

    # Terminal & DevSecOps -> Workspace 3
    assert HyprOrchestrator.classify_window({"class": "kitty", "title": "bash"}) == 3
    assert HyprOrchestrator.classify_window({"class": "alacritty", "title": "fish"}) == 3
    assert HyprOrchestrator.classify_window({"class": "foot", "title": "top"}) == 3

    # Comms & Chat -> Workspace 4
    assert HyprOrchestrator.classify_window({"class": "discord", "title": "Discord"}) == 4
    assert HyprOrchestrator.classify_window({"class": "telegramdesktop", "title": "Telegram"}) == 4
    assert HyprOrchestrator.classify_window({"class": "slack", "title": "Workplace Slack"}) == 4

    # Média & Création -> Workspace 5
    assert HyprOrchestrator.classify_window({"class": "Spotify", "title": "Spotify Free"}) == 5
    assert HyprOrchestrator.classify_window({"class": "vlc", "title": "VLC media player"}) == 5
    assert HyprOrchestrator.classify_window({"class": "gimp-2.10", "title": "GNU Image Manipulation Program"}) == 5

    # Monitoring -> Workspace 6
    assert HyprOrchestrator.classify_window({"class": "btop", "title": "btop resource monitor"}) == 6
    assert HyprOrchestrator.classify_window({"class": "jarvis-dashboard", "title": "ANO-GPT Dashboard"}) == 6


# ════════════════════════════════════════════════════════════════════════════
# 3. Réorganisation dynamique de toutes les fenêtres
# ════════════════════════════════════════════════════════════════════════════

def test_hypr_organize_workspaces(monkeypatch):
    mock_clients = [
        {"address": "0x123", "class": "Code", "title": "VS Code", "workspace": {"id": 3}},
        {"address": "0x456", "class": "firefox", "title": "Firefox Web Browser", "workspace": {"id": 1}},
        {"address": "0x789", "class": "kitty", "title": "Terminal", "workspace": {"id": 3}},
        {"address": "0xabc", "class": "spotify", "title": "Spotify", "workspace": {"id": 1}},
    ]
    dispatched_moves = []

    monkeypatch.setattr(HyprOrchestrator, "get_clients", lambda: mock_clients)
    monkeypatch.setattr(
        "actions.hypr_orchestrator._hyprctl_dispatch",
        lambda disp, args: dispatched_moves.append((disp, args)) or True
    )

    summary = HyprOrchestrator.organize_workspaces()
    assert "Organisation dynamique terminée" in summary
    assert "Bureau 1 [💻 Dev & IDE]" in summary
    assert "Bureau 2 [🌐 Web & Docs]" in summary
    assert "Bureau 5 [🎨 Média & Création]" in summary

    # Code (0x123) était sur WS 3 -> doit aller sur WS 1
    assert ("movetoworkspacesilent", "1,address:0x123") in dispatched_moves
    # Firefox (0x456) était sur WS 1 -> doit aller sur WS 2
    assert ("movetoworkspacesilent", "2,address:0x456") in dispatched_moves
    # Kitty (0x789) était déjà sur WS 3 -> pas besoin de déplacement
    assert ("movetoworkspacesilent", "3,address:0x789") not in dispatched_moves
    # Spotify (0xabc) était sur WS 1 -> doit aller sur WS 5
    assert ("movetoworkspacesilent", "5,address:0xabc") in dispatched_moves


# ════════════════════════════════════════════════════════════════════════════
# 4. Presets et contrôleur unifié
# ════════════════════════════════════════════════════════════════════════════

def test_hypr_orchestrator_presets(monkeypatch):
    monkeypatch.setattr(HyprOrchestrator, "organize_workspaces", lambda: "Organisation OK")
    monkeypatch.setattr("actions.hypr_orchestrator._hyprctl_dispatch", lambda *a, **k: True)

    res_dev = hypr_orchestrator_control({"preset": "devsecops"})
    assert "Preset DevSecOps activé" in res_dev

    res_mon = hypr_orchestrator_control({"preset": "monitoring"})
    assert "Preset Monitoring activé" in res_mon


def test_hypr_orchestrator_control_empty_args():
    res = hypr_orchestrator_control({})
    assert isinstance(res, str)
    assert res
