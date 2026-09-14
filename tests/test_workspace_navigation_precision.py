"""Régressions : naviguer dans Hyprland ne doit jamais déplacer une fenêtre."""

from actions import computer_control, shell_exec


def test_shell_parser_recognises_navigate_to_workspace_locally():
    parsed = shell_exec._parse_shell_request_locally("navigue vers le bureau 1")
    assert parsed == {"target": "hypr", "action": "workspace", "value": "1"}


def test_computer_parser_recognises_navigate_to_workspace_locally():
    parsed = computer_control._parse_control_locally("navigue vers le bureau 1")
    assert parsed == {
        "action": "switch_workspace",
        "params": {"workspace": "1"},
    }


def test_shell_workspace_navigation_is_confirmed_without_window_move(monkeypatch):
    dispatches = []
    monkeypatch.setattr(
        shell_exec, "_hyprctl_dispatch",
        lambda action, value, lua: dispatches.append((action, value, lua)) or "ok",
    )
    monkeypatch.setattr(
        shell_exec.kit, "hypr_json",
        lambda endpoint, default=None: {"id": 1} if endpoint == "activeworkspace" else default,
    )
    monkeypatch.setattr(shell_exec.kit, "hypr_invalidate", lambda: None)
    monkeypatch.setattr(shell_exec.kit, "wait_until", lambda predicate, **_kwargs: predicate())

    result = shell_exec.hypr_control({"action": "workspace", "value": "1"})

    assert dispatches[0][:2] == ("workspace", "1")
    assert "Navigation confirmée" in result
    assert "Aucune fenêtre n'a été déplacée" in result


def test_raw_legacy_hyprctl_workspace_is_routed_to_verified_action(monkeypatch):
    calls = []
    monkeypatch.setattr(
        shell_exec,
        "hypr_control",
        lambda parameters, player=None: calls.append(parameters) or "Navigation confirmée",
    )

    result = shell_exec.run_shell({"command": "hyprctl dispatch workspace 2"})

    assert result == "Navigation confirmée"
    assert calls == [{"action": "workspace", "value": "2"}]


def test_move_to_workspace_needs_an_identifiable_active_window(monkeypatch):
    monkeypatch.setattr(shell_exec, "_hypr_active_window", lambda: {})

    result = shell_exec.hypr_control({"action": "move_to_workspace", "value": "1"})

    assert "Déplacement annulé" in result
