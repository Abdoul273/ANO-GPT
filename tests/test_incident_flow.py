"""Erreur vécue → carte dédiée → « je corrige ? » → réparation ou rapport."""
import time

import pytest

from core import human_confirmation, incident_flow, incident_log


class _UI:
    def __init__(self):
        self.cards = []

    def show_card(self, card_type, title, body, actions=None):
        self.cards.append((card_type, title, body, actions))


@pytest.fixture
def ui(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(incident_flow, "_REPORT_DIR", tmp_path / "reports")
    monkeypatch.setattr(incident_flow, "_copy", lambda text: True)
    fake = _UI()
    notified = []
    human_confirmation.clear()
    human_confirmation.bind(
        show=lambda p: fake.show_card("confirmation", p.title, p.detail, ["Confirmer", "Annuler"]),
        hide=lambda token: None, log=lambda m: None, notify=notified.append,
    )
    incident_flow.bind(fake, None)
    yield fake, notified
    incident_flow.bind(None, None)
    human_confirmation.clear()


def _incident():
    def meteo(data):
        return f"{data['temp']:.1f}°C"   # ligne fautive : data['temp'] vaut None
    try:
        meteo({"temp": None})
    except TypeError as exc:
        return incident_log.record("weather_report", exc, announce=False)


def _wait(pred, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_error_card_then_confirmation_card(ui):
    fake, _ = ui
    inc = _incident()
    spoken = incident_flow.propose([inc])
    kinds = [c[0] for c in fake.cards]
    assert kinds == ["error", "confirmation"]
    error_body = fake.cards[0][2]
    assert "tests/test_incident_flow.py" in error_body and "▶" in error_body
    assert "None" in error_body          # la cause est expliquée
    assert spoken.endswith("Je la corrige ? Dis oui ou non.")


def test_voice_no_gives_lines_and_why_without_touching_code(ui, monkeypatch):
    fake, notified = ui
    monkeypatch.setattr("core.auto_fix.repair", lambda *a, **k: pytest.fail("aucune réparation"))
    incident_flow.propose([_incident()])
    assert human_confirmation.voice_answer("non laisse") is False
    assert human_confirmation.resolve(human_confirmation.current().token, False, source="voix")
    assert _wait(lambda: notified)
    assert "je ne touche à rien" in notified[0] and "test_incident_flow.py ligne" in notified[0]
    report_card = fake.cards[-1]
    assert report_card[1].startswith("📋 Rapport") and "Pourquoi ça a calé" in report_card[2]
    assert list(incident_flow._REPORT_DIR.glob("*.md"))


def test_voice_yes_launches_repair(ui, monkeypatch):
    _, notified = ui
    calls = []
    monkeypatch.setattr("core.auto_fix.repair",
                        lambda inc, player=None, speak=None: calls.append(inc) or "Je répare.")
    incident_flow.propose([_incident()])
    assert human_confirmation.voice_answer("oui vas-y") is True
    human_confirmation.resolve(human_confirmation.current().token, True, source="voix")
    assert _wait(lambda: calls)


def test_voice_answer_ignored_for_other_confirmations(ui):
    human_confirmation.request("sms", "Envoyer le SMS ?", "à Maman", lambda: "ok")
    assert human_confirmation.voice_answer("oui") is None


def test_long_sentence_is_not_an_answer(ui):
    incident_flow.propose([_incident()])
    assert human_confirmation.voice_answer("oui mais d'abord dis moi quelle heure il est") is None
