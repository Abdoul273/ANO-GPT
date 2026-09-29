"""Sonde d'état système : rubriques, alias et robustesse aux échecs."""

from actions import system_probe


def test_les_alias_menent_a_la_bonne_rubrique():
    assert system_probe.resolve_topics("le casque") == ["bluetooth"]
    assert system_probe.resolve_topics("VPN wifi") == ["reseau", "wifi"]
    assert system_probe.resolve_topics("micro") == ["audio"]


def test_sujet_vide_ou_inconnu_donne_le_releve_complet():
    complet = list(system_probe._ORDER)
    assert system_probe.resolve_topics("") == complet
    assert system_probe.resolve_topics("zzz") == complet


def test_une_sonde_qui_plante_ne_fait_pas_tomber_le_releve(monkeypatch):
    def boom():
        raise RuntimeError("x")

    monkeypatch.setitem(system_probe._PROBES, "bluetooth", boom)
    monkeypatch.setitem(system_probe._PROBES, "wifi", lambda: "Wi-Fi : ACTIVÉ.")
    out = system_probe.system_state("bluetooth wifi")
    assert "indisponible" in out and "Wi-Fi : ACTIVÉ." in out


def test_bluetooth_reflete_bluetoothctl(monkeypatch):
    monkeypatch.setattr(system_probe, "_out", lambda cmd, timeout=3.0: (
        "Powered: no" if cmd[:2] == ["bluetoothctl", "show"] else ""))
    monkeypatch.setattr(system_probe, "_rfkill", lambda: {"bluetooth": True})
    assert "DÉSACTIVÉ" in system_probe.probe_bluetooth()
    monkeypatch.setattr(system_probe, "_out", lambda cmd, timeout=3.0: (
        "Powered: yes" if cmd[:2] == ["bluetoothctl", "show"] else ""))
    assert "ACTIVÉ" in system_probe.probe_bluetooth()
