"""`to` est le destinataire d'un envoi ; le filtre de recherche est `to_filter`."""

from __future__ import annotations

from core import tool_dispatcher
from core.action_runtime import ActionRuntime


def _email_schema() -> dict:
    for decl in tool_dispatcher.TOOL_DECLARATIONS:
        if decl["name"] == "email_control":
            return decl["parameters"]
    raise AssertionError("email_control absent")


def test_declaration_has_distinct_recipient_and_filter_keys():
    props = _email_schema()["properties"]
    assert "send" in props["to"]["description"]
    assert "search" in props["to_filter"]["description"]


def test_runtime_aliases_map_recipient_and_message_to_send_fields():
    runtime = ActionRuntime([{"name": "email_control", "parameters": _email_schema()}])
    prepared = runtime.prepare(
        "email_control",
        {"action": "send", "recipient": "maman", "message": "Bonjour", "recipient_filter": "x"},
    )
    assert prepared["to"] == "maman"
    assert prepared["body"] == "Bonjour"
    assert prepared["to_filter"] == "x"
