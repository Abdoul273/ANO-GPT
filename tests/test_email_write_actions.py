"""Les écritures Gmail (envoi, réponse, corbeille) passent par une confirmation
humaine ; le marquage et l'archivage s'appliquent directement."""

from actions import email as email_action
from core import human_confirmation


class _Service:
    can_write = True

    def __init__(self):
        self.calls = []

    def read_email(self, msg_id):
        return {"id": msg_id, "sender": "Alice <alice@example.org>", "subject": "Devis",
                "date": "", "body": "Bonjour", "reply_to": ""}

    def get_email_header(self, msg_id):
        return {"sender": "Alice", "subject": "Devis"}

    def send_email(self, to, subject, body, *, cc="", reply_to_id=""):
        self.calls.append(("send", to, subject, body, reply_to_id))
        return {"id": "sent"}

    def mark_read(self, msg_id, read=True):
        self.calls.append(("mark_read", msg_id, read))

    def archive(self, msg_id):
        self.calls.append(("archive", msg_id))

    def trash(self, msg_id):
        self.calls.append(("trash", msg_id))


def _install(monkeypatch):
    service = _Service()
    monkeypatch.setattr(email_action, "_HAS_GMAIL", True)
    monkeypatch.setattr(email_action, "get_gmail_service", lambda: service)
    requests = []

    def fake_request(key, title, detail, callback):
        requests.append((key, title, detail))
        return callback()

    monkeypatch.setattr(human_confirmation, "request", fake_request)
    return service, requests


def test_reply_is_previewed_then_sent_in_thread(monkeypatch):
    service, requests = _install(monkeypatch)
    memory = {"email_results": ["m1"]}
    out = email_action.email_control(
        {"action": "reply", "id": "1", "body": "Merci, c'est validé."}, session_memory=memory)
    assert "Réponse envoyée à alice@example.org" in out
    assert requests and requests[0][0] == "email:send"
    assert service.calls == [("send", "alice@example.org", "Re: Devis", "Merci, c'est validé.", "m1")]


def test_send_requires_recipient_and_body(monkeypatch):
    _install(monkeypatch)
    assert "Destinataire" in email_action.email_control({"action": "send", "body": "x"})
    assert "vide" in email_action.email_control({"action": "send", "to": "a@b.c"})


def test_mark_read_and_archive_apply_directly(monkeypatch):
    service, requests = _install(monkeypatch)
    memory = {"email_results": ["m7"]}
    assert "lu" in email_action.email_control({"action": "mark_read", "id": "1"}, session_memory=memory)
    assert "archivé" in email_action.email_control({"action": "archive", "id": "1"}, session_memory=memory)
    assert service.calls == [("mark_read", "m7", True), ("archive", "m7")]
    assert not requests


def test_trash_is_confirmed(monkeypatch):
    service, requests = _install(monkeypatch)
    out = email_action.email_control({"action": "trash", "id": "m9"})
    assert "corbeille" in out
    assert requests[0][0] == "email:trash"
    assert service.calls == [("trash", "m9")]
