"""
test_open_app_command.py — Tests unitaires et TDD pour actions/open_app.py
portant sur le lancement d'applications avec exécution de commande (type_text / command).

Couvre :
  a) _parse_open_command_locally avec phrases composées :
     - « lance kitty et tape la commande codex » -> app_name: "kitty", command: "codex"
     - « ouvre kitty et écris codex » -> app_name: "kitty", command: "codex"
     - « lance le terminal et exécute btop » -> app_name: "terminal", command: "btop"
     - « ouvre foot et tape ls -la » -> app_name: "foot", command: "ls -la"
     - « lance firefox » -> app_name: "firefox", pas de commande
  b) open_app({"app_name": "kitty", "command": "codex"}) :
     - Vérifie que l'application se lance et que la commande est tapée via computer_control (press_enter=True).
  c) open_app({"description": "lance kitty et tu tape la commande codex"}) :
     - Vérifie l'exécution de bout en bout (extraction de 'kitty' et 'codex', lancement et saisie).
"""
import pytest
from unittest.mock import MagicMock

import actions.open_app as oa
from actions.open_app import _parse_open_command_locally, open_app
import actions.computer_control as cc


# ════════════════════════════════════════════════════════════════════════════
# a) Tests unitaires du parsing local (_parse_open_command_locally)
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("phrase, expected_app, expected_cmd", [
    ("lance kitty et tape la commande codex", "kitty", "codex"),
    ("ouvre kitty et écris codex", "kitty", "codex"),
    ("lance le terminal et exécute btop", "terminal", "btop"),
    ("ouvre foot et tape ls -la", "foot", "ls -la"),
])
def test_parse_open_command_locally_compound_phrases(phrase, expected_app, expected_cmd):
    """Vérifie l'extraction conjointe de l'application et de la commande dans les phrases composées."""
    res = _parse_open_command_locally(phrase)
    assert res is not None, f"Le parsing a renvoyé None pour : '{phrase}'"
    assert res.get("app_name") == expected_app, f"Mauvais app_name pour '{phrase}' : {res.get('app_name')} != {expected_app}"
    assert res.get("command") == expected_cmd, f"Mauvaise command pour '{phrase}' : {res.get('command')} != {expected_cmd}"


def test_parse_open_command_locally_simple():
    """Vérifie qu'un lancement simple ne produit aucune commande."""
    res = _parse_open_command_locally("lance firefox")
    assert res is not None
    assert res.get("app_name") == "firefox"
    assert res.get("command") is None


# ════════════════════════════════════════════════════════════════════════════
# b) Test open_app({"app_name": "kitty", "command": "codex"})
# ════════════════════════════════════════════════════════════════════════════

def test_open_app_with_command_param(monkeypatch):
    """Vérifie que spécifier app_name et command lance l'app puis invoque computer_control avec press_enter=True."""
    mock_launcher = MagicMock(return_value=True)
    monkeypatch.setitem(oa._OS_LAUNCHERS, oa._SYSTEM, mock_launcher)
    monkeypatch.setattr(oa, "_is_process_running", lambda app: False)
    monkeypatch.setattr(oa, "_HAS_TRACKER", False)
    monkeypatch.setattr(oa, "_hyprctl_json", lambda *args: [])

    mock_cc = MagicMock(return_value="Texte tapé : codex (validé par Entrée)")
    if hasattr(oa, "computer_control"):
        monkeypatch.setattr(oa, "computer_control", mock_cc)
    monkeypatch.setattr(cc, "computer_control", mock_cc)

    # Réduire le délai de stabilisation pendant les tests pour aller vite
    monkeypatch.setattr(oa.time, "sleep", lambda s: None)

    res = open_app({"app_name": "kitty", "command": "codex"})

    assert mock_launcher.called, "Le launcher d'application n'a pas été appelé."
    assert mock_cc.called, "computer_control n'a pas été appelé pour taper la commande."
    mock_cc.assert_called_once_with({"action": "type", "text": "codex", "window": "kitty", "press_enter": True})
    assert "kitty" in res
    assert "codex" in res


# ════════════════════════════════════════════════════════════════════════════
# c) Test bout en bout : open_app({"description": "lance kitty et tu tape..."})
# ════════════════════════════════════════════════════════════════════════════

def test_open_app_end_to_end_from_description(monkeypatch):
    """Vérifie l'exécution de bout en bout depuis la phrase naturelle avec tu tape."""
    mock_launcher = MagicMock(return_value=True)
    monkeypatch.setitem(oa._OS_LAUNCHERS, oa._SYSTEM, mock_launcher)
    monkeypatch.setattr(oa, "_is_process_running", lambda app: False)
    monkeypatch.setattr(oa, "_HAS_TRACKER", False)
    monkeypatch.setattr(oa, "_hyprctl_json", lambda *args: [])

    mock_cc = MagicMock(return_value="Texte tapé : codex (validé par Entrée)")
    if hasattr(oa, "computer_control"):
        monkeypatch.setattr(oa, "computer_control", mock_cc)
    monkeypatch.setattr(cc, "computer_control", mock_cc)

    monkeypatch.setattr(oa.time, "sleep", lambda s: None)

    phrase = "lance kitty et tu tape la commande codex"
    res = open_app({"description": phrase})

    assert mock_launcher.called, "Le launcher d'application n'a pas été appelé."
    assert any("kitty" in str(arg).lower() for arg in mock_launcher.call_args[0]), (
        f"L'application extraite et lancée devrait être 'kitty', appel : {mock_launcher.call_args}"
    )
    assert mock_cc.called, "computer_control n'a pas été appelé pour saisir la commande."
    mock_cc.assert_called_once_with({"action": "type", "text": "codex", "window": "kitty", "press_enter": True})
    assert "kitty" in res
    assert "codex" in res


# ════════════════════════════════════════════════════════════════════════════
# d) Test ciblage de la nouvelle fenêtre apparue (TDD Tâche 1 & 2)
# ════════════════════════════════════════════════════════════════════════════

def test_open_app_targets_newly_opened_window(monkeypatch):
    """Vérifie le ciblage précis de la nouvelle fenêtre lors d'un lancement avec commande :
    a) Détection de la nouvelle fenêtre apparue (diff d'adresses hyprctl)
    b) Activation du workspace si différent du workspace actif
    c) Focus explicite sur la nouvelle fenêtre
    d) Temporisation fonctionnelle de 3.5s (wait_functional / time.sleep)
    e) Appel de computer_control avec window="address:0x..."
    f) Prise en compte prioritaire de target_window si spécifié
    """
    mock_launcher = MagicMock(return_value=True)
    monkeypatch.setitem(oa._OS_LAUNCHERS, oa._SYSTEM, mock_launcher)
    monkeypatch.setattr(oa, "_is_process_running", lambda app: False)
    monkeypatch.setattr(oa, "_HAS_TRACKER", False)
    monkeypatch.setattr(oa.kit, "which", lambda cmd: "/usr/bin/" + cmd)

    initial_clients = [{"address": "0x1111", "workspace": {"id": 1}, "class": "foot"}]
    new_clients = [
        {"address": "0x1111", "workspace": {"id": 1}, "class": "foot"},
        {"address": "0x55aabbcc", "workspace": {"id": 2}, "class": "kitty", "title": "kitty"},
    ]

    query_count = {"clients": 0}
    def mock_hyprctl_json(cmd, *args):
        if cmd == "clients":
            query_count["clients"] += 1
            if query_count["clients"] <= 1:
                return initial_clients
            return new_clients
        if cmd == "activeworkspace":
            return {"id": 1}
        return None

    monkeypatch.setattr(oa, "_hyprctl_json", mock_hyprctl_json)

    switched_workspaces = []
    monkeypatch.setattr(oa, "_focus_workspace", lambda ws: switched_workspaces.append(ws))

    focused_windows = []
    if hasattr(oa, "_focus_window"):
        monkeypatch.setattr(oa, "_focus_window", lambda win: focused_windows.append(win) or True)
    monkeypatch.setattr("actions.window_instances.focus_window", lambda win: focused_windows.append(win) or True, raising=False)

    sleep_calls = []
    monkeypatch.setattr(oa.time, "sleep", lambda s: sleep_calls.append(s))
    # L'attente fixe a été remplacée par une sonde de fenêtre prête, bornée
    # par le même délai : c'est cette borne que l'on vérifie.
    ready_timeouts = []
    monkeypatch.setattr(
        oa, "_window_ready",
        lambda target, timeout=3.5: ready_timeouts.append(timeout) or True,
    )

    cc_calls = []
    mock_cc = lambda payload: cc_calls.append(payload) or f"Texte tapé : {payload.get('text')}"
    if hasattr(oa, "computer_control"):
        monkeypatch.setattr(oa, "computer_control", mock_cc)
    monkeypatch.setattr(cc, "computer_control", mock_cc)

    # 1. Cas automatique : détection du diff d'adresses
    open_app({"app_name": "kitty", "command": "codex"})

    # a) et e) Détection de la nouvelle fenêtre (0x55aabbcc) et appel computer_control avec window="address:0x55aabbcc"
    assert len(cc_calls) == 1, "computer_control aurait dû être appelé exactement une fois."
    assert cc_calls[0].get("action") == "type"
    assert cc_calls[0].get("text") == "codex"
    assert cc_calls[0].get("press_enter") is True
    assert cc_calls[0].get("window") == "address:0x55aabbcc", f"La fenêtre ciblée devrait être 'address:0x55aabbcc', reçu : {cc_calls[0].get('window')}"

    # b) Workspace 2 activé car la nouvelle fenêtre est sur le workspace 2 et l'actif est 1
    assert (2 in switched_workspaces or "2" in [str(ws) for ws in switched_workspaces]), (
        f"Le bureau 2 aurait dû être activé. Appels reçus : {switched_workspaces}"
    )

    # c) La nouvelle fenêtre a été explicitement focalisée
    assert any("0x55aabbcc" in str(win) for win in focused_windows), (
        f"La nouvelle fenêtre 0x55aabbcc aurait dû être focalisée. Fenêtres focalisées : {focused_windows}"
    )

    # d) Temporisation fonctionnelle bornée à 3.5s appliquée
    assert 3.5 in ready_timeouts, (
        f"Une attente de fenêtre prête bornée à 3.5s aurait dû être appliquée. Bornes : {ready_timeouts}"
    )

    # f) Prise en compte de target_window personnalisé et wait_functional
    cc_calls.clear()
    sleep_calls.clear()
    ready_timeouts.clear()
    focused_windows.clear()
    switched_workspaces.clear()

    open_app({
        "app_name": "kitty",
        "command": "codex",
        "target_window": "address:0x999999",
        "wait_functional": 4.5,
    })

    assert len(cc_calls) == 1
    assert cc_calls[0].get("window") == "address:0x999999"
    assert 4.5 in ready_timeouts
    assert any("0x999999" in str(win) for win in focused_windows)
