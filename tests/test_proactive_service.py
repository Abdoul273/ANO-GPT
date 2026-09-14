"""Service proactif : file événementielle, garde-fous et persistance."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from actions import proactive


def test_un_evenement_attend_sans_boucle_de_polling(tmp_path):
    async def scenario():
        service = proactive.ProactiveService(tmp_path / "state.json")
        service.bind()
        assert service.publish("mail", "Un mail important.", dedupe_key="mail:42")
        event = await asyncio.wait_for(service.next_event(), timeout=0.1)
        service.mark_delivered(event)
        return service, event

    service, event = asyncio.run(scenario())
    assert event.topic == "mail"
    assert not service.publish("mail", "Le même mail.", dedupe_key="mail:42")


def test_le_mode_silence_survit_au_redemarrage(tmp_path):
    path = tmp_path / "state.json"
    first = proactive.ProactiveService(path)
    assert first.set_silent(True) == "silence"
    assert proactive.ProactiveService(path).silent is True
    assert proactive.ProactiveService(path).set_silent(False) == "actif"


def test_deux_sujets_differents_restent_independants(tmp_path):
    service = proactive.ProactiveService(tmp_path / "state.json")
    assert service.publish("mail", "Premier", dedupe_key="mail:premier")
    assert service.publish("mail", "Deuxième", dedupe_key="mail:deuxieme")
    assert not service.publish("mail", "Doublon", dedupe_key="mail:premier")


def test_batterie_faible_donne_le_temps_restant(tmp_path, monkeypatch):
    import psutil

    service = proactive.ProactiveService(tmp_path / "state.json")
    monkeypatch.setattr(
        psutil, "sensors_battery",
        lambda: SimpleNamespace(percent=12, power_plugged=False, secsleft=20 * 60),
    )
    assert service.evaluate_battery()

    async def receive():
        service.bind()
        return await asyncio.wait_for(service.next_event(), 0.1)

    event = asyncio.run(receive())
    assert "12 %" in event.message
    assert "20 minutes" in event.message


def test_disque_ne_parle_quau_dela_de_93_pourcent(tmp_path, monkeypatch):
    service = proactive.ProactiveService(tmp_path / "state.json")
    monkeypatch.setattr(
        proactive.shutil, "disk_usage",
        lambda _path: SimpleNamespace(total=100, used=93, free=7),
    )
    assert service.evaluate_disk() is False
    monkeypatch.setattr(
        proactive.shutil, "disk_usage",
        lambda _path: SimpleNamespace(total=100, used=94, free=6),
    )
    monkeypatch.setattr(proactive, "_cache_reclaimable_gib", lambda: 4.2)
    assert service.evaluate_disk() is True


def test_retour_maison_exige_une_transition(tmp_path, monkeypatch):
    service = proactive.ProactiveService(tmp_path / "state.json")
    monkeypatch.setattr(proactive, "_home_coordinates", lambda: (9.5, -13.7, 300.0))
    monkeypatch.setattr(proactive, "_arrival_briefing", lambda: "Bon retour.")

    assert not service.observe_location({"lat": 9.6, "lon": -13.7})
    assert not service.observe_location({"lat": 9.61, "lon": -13.7})
    assert service.observe_location({"lat": 9.5, "lon": -13.7})


def test_le_domicile_ne_peut_etre_enregistre_quexplicitement(tmp_path, monkeypatch):
    actions_dir = tmp_path / "actions"
    config_dir = tmp_path / "config"
    actions_dir.mkdir()
    config_dir.mkdir()
    config_file = config_dir / "api_keys.json"
    config_file.write_text('{"gemini_api_key":"conservee"}', encoding="utf-8")
    monkeypatch.setattr(proactive, "__file__", str(actions_dir / "proactive.py"))

    service = proactive.ProactiveService(tmp_path / "state.json")
    service.set_home(9.5, -13.7, 300)

    content = config_file.read_text(encoding="utf-8")
    assert '"gemini_api_key": "conservee"' in content
    assert '"radius_m": 300.0' in content


def test_plein_ecran_bloque_la_prise_de_parole(monkeypatch):
    monkeypatch.setattr(
        proactive.shutil, "which",
        lambda name: f"/usr/bin/{name}" if name == "hyprctl" else None,
    )
    monkeypatch.setattr(
        proactive.kit, "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout='{"class":"mpv","fullscreen":2}'
        ),
    )
    assert proactive.desktop_blocks_proactivity() == "plein écran"


def test_un_appel_pipewire_bloque_la_prise_de_parole(monkeypatch):
    monkeypatch.setattr(
        proactive.shutil, "which",
        lambda name: f"/usr/bin/{name}" if name == "pactl" else None,
    )
    monkeypatch.setattr(
        proactive.kit, "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=(
                "Source Output #81\n"
                "Corked: no\n"
                'application.name = "Google Chrome input"\n'
            ),
        ),
    )
    assert proactive.desktop_blocks_proactivity() == "appel"


def test_echo_cancel_passif_nest_pas_pris_pour_un_appel(monkeypatch):
    monkeypatch.setattr(
        proactive.shutil, "which",
        lambda name: f"/usr/bin/{name}" if name == "pactl" else None,
    )
    monkeypatch.setattr(
        proactive.kit, "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=(
                "Source Output #12\n"
                "Corked: no\n"
                'media.name = "Echo-Cancel Capture"\n'
                'node.name = "echo-cancel-capture"\n'
                'node.passive = "true"\n'
            ),
        ),
    )
    assert proactive.desktop_blocks_proactivity() == ""


def test_un_vrai_appel_reste_detecte_avec_echo_cancel_present(monkeypatch):
    monkeypatch.setattr(
        proactive.shutil, "which",
        lambda name: f"/usr/bin/{name}" if name == "pactl" else None,
    )
    monkeypatch.setattr(
        proactive.kit, "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=(
                "Source Output #12\n"
                "Corked: no\n"
                'media.name = "Echo-Cancel Capture"\n'
                'node.passive = "true"\n'
                "Source Output #13\n"
                "Corked: no\n"
                'application.name = "Google Chrome input"\n'
            ),
        ),
    )
    assert proactive.desktop_blocks_proactivity() == "appel"
