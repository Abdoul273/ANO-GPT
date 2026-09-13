from __future__ import annotations

import pytest

from core import zapzap_controller as zapzap


def test_chat_url_encodes_text_and_normalizes_e164():
    url = zapzap.chat_url("+224 600 00 00 00", "Bonjour & à bientôt")
    assert "phone=224600000000" in url
    assert "text=Bonjour+%26+%C3%A0+bient%C3%B4t" in url


@pytest.mark.parametrize("phone", ["0600000000", "+12", "+1234567890123456", "abc"])
def test_phone_requires_unambiguous_international_number(phone):
    with pytest.raises(zapzap.ZapZapError):
        zapzap.normalize_phone(phone)


def test_open_chat_uses_zapzap_deeplink_without_shell(monkeypatch):
    monkeypatch.setattr(zapzap, "status", lambda: zapzap.ZapZapStatus(True, False, True))
    captured = []
    monkeypatch.setattr(zapzap.kit, "spawn", lambda command: captured.append(command) or object())
    window = {"address": "0xabc"}
    monkeypatch.setattr(zapzap.kit, "wait_until", lambda predicate, **kwargs: True)
    monkeypatch.setattr(zapzap, "_zapzap_window", lambda: window)
    monkeypatch.setattr(zapzap, "_focus", lambda candidate: candidate == window)

    result = zapzap.open_chat("+224600000000", "bonjour")

    assert result.startswith("Conversation ouverte")
    assert captured[0][0] == "zapzap"
    assert captured[0][1].startswith("https://web.whatsapp.com/send?")


def test_control_status_needs_no_desktop(monkeypatch):
    monkeypatch.setattr(zapzap, "status", lambda: zapzap.ZapZapStatus(True, True, True))
    assert "installé" in zapzap.control("status")


def test_direct_send_requires_the_ui_confirmation_flow(monkeypatch):
    monkeypatch.setattr(zapzap, "resolve_phone", lambda _: "224600000000")
    assert "Aucun message n'a été envoyé" in zapzap.control("send", "Alice", "bonjour")
