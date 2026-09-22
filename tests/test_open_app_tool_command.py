"""La façade typée conserve la commande et ne fabrique pas de succès."""
import asyncio
from unittest.mock import patch

from core.migrated_tools import open_app_tool


def test_command_and_workspace_reach_application():
    with patch("actions.open_app.open_app", return_value="Commande transmise") as action:
        result = asyncio.run(open_app_tool(app_name="kitty", workspace=1, command="codex"))
    assert action.call_args.kwargs["parameters"] == {
        "app_name": "kitty", "workspace": 1, "hidden": False, "command": "codex",
    }
    assert result == "Commande transmise"


def test_missing_result_does_not_claim_success():
    with patch("actions.open_app.open_app", return_value=None):
        result = asyncio.run(open_app_tool(app_name="kitty", command="codex"))
    assert "non confirmé" in result
