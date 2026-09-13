"""send_message : le texte est retrouvé même s'il est arrivé au mauvais endroit."""

from __future__ import annotations

import pytest

from core import tool_dispatcher
from core.action_runtime import ActionRuntime, ActionValidationError


def _runtime() -> ActionRuntime:
    decl = next(d for d in tool_dispatcher.TOOL_DECLARATIONS if d["name"] == "send_message")
    return ActionRuntime([decl])


def test_alias_wins_over_empty_target():
    prepared = _runtime().prepare(
        "send_message",
        {"receiver": "Maman", "message_text": "", "text": "Bonjour", "platform": "whatsapp"},
    )
    assert prepared["message_text"] == "Bonjour"


@pytest.mark.parametrize("alias", ["content", "body", "msg", "contenu"])
def test_new_aliases_map_to_message_text(alias):
    prepared = _runtime().prepare(
        "send_message", {"receiver": "Maman", alias: "Salut", "platform": "whatsapp"}
    )
    assert prepared["message_text"] == "Salut"


def test_single_stray_text_recovers_single_missing_field():
    prepared = _runtime().prepare(
        "send_message",
        {"receiver": "Maman", "message_text": "", "list_instances": "Je rentre à 19h", "platform": "whatsapp"},
    )
    assert prepared["message_text"] == "Je rentre à 19h"


def test_ambiguous_stray_text_is_suggested_in_error():
    with pytest.raises(ActionValidationError) as exc:
        _runtime().prepare(
            "send_message",
            {"receiver": "Maman", "message_text": "", "foo": "a", "bar": "b", "platform": "whatsapp"},
        )
    text = str(exc.value)
    assert "message_text" in text and "`foo`" in text and "`bar`" in text
