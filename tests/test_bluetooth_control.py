"""Une commande Bluetooth explicite fixe un état et vérifie le contrôleur."""

import actions.computer_settings as settings
from core.action_kit import ProcResult
from core.tool_dispatcher import _bluetooth_action_from_request


def test_spoken_bluetooth_intent_is_idempotent():
    assert _bluetooth_action_from_request("Active le Bluetooth") == "bluetooth_on"
    assert _bluetooth_action_from_request("Rallume le Bluetooth") == "bluetooth_on"
    assert _bluetooth_action_from_request("C'est bon, rallume-le") == "bluetooth_on"
    assert _bluetooth_action_from_request("Désactive le Bluetooth") == "bluetooth_off"


def test_bluetooth_on_checks_powered_after_command(monkeypatch):
    state = {"powered": False}
    commands = []
    monkeypatch.setattr(settings, "_OS", "Linux")
    monkeypatch.setattr(settings.kit, "which", lambda name: name in {"bluetoothctl", "rfkill"})
    monkeypatch.setattr(settings, "_rfkill_state", lambda kind: True)

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        if cmd[:2] == ["bluetoothctl", "power"]:
            state["powered"] = True
        output = ("Powered: yes" if state["powered"] else "Powered: no") if cmd[-1] == "show" else ""
        return ProcResult(cmd=tuple(cmd), code=0, out=output)

    monkeypatch.setattr(settings, "run", fake_run)
    assert settings.bluetooth_on() == "Bluetooth activé."
    assert ["bluetoothctl", "power", "on"] in commands
    commands.clear()
    assert settings.bluetooth_on() == "Bluetooth déjà activé."
    assert not any(cmd[:2] == ["bluetoothctl", "power"] for cmd in commands)


def test_bluetooth_does_not_claim_success_when_controller_stays_off(monkeypatch):
    monkeypatch.setattr(settings, "_OS", "Linux")
    monkeypatch.setattr(settings.kit, "which", lambda name: name == "bluetoothctl")
    monkeypatch.setattr(settings, "_rfkill_state", lambda kind: True)
    monkeypatch.setattr(settings.time, "sleep", lambda seconds: None)

    def fake_run(cmd, **kwargs):
        output = "Powered: no" if cmd[-1] == "show" else "Changing power on succeeded"
        return ProcResult(cmd=tuple(cmd), code=0, out=output)

    monkeypatch.setattr(settings, "run", fake_run)
    result = settings.bluetooth_on()
    assert "Échec" in result
    assert "toujours désactivé" in result


def test_confirmed_bluetooth_action_returns_verified_result_and_allows_retry(monkeypatch):
    captured = {}
    monkeypatch.setattr(settings, "_bluetooth_powered", lambda: False)
    monkeypatch.setitem(settings.ACTION_MAP, "bluetooth_on",
                        lambda: "Échec : Bluetooth toujours désactivé.")

    def request(*args, **kwargs):
        captured.update(kwargs)
        return kwargs["callback"]() if "callback" in kwargs else args[3]()

    monkeypatch.setattr(settings.human_confirmation, "request", request)
    result = settings.computer_settings({"action": "bluetooth_on"})
    assert result == "Échec : Bluetooth toujours désactivé."
    assert captured["dedupe"] is False
