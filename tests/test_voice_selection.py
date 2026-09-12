"""Sélection persistante de la voix native Gemini Live."""

import asyncio
import json

import pytest

import main
from main import JarvisLive
from memory import config_manager


def test_un_changement_de_voix_declenche_la_reconnexion(monkeypatch):
    async def scenario():
        saved = []
        logs = []
        monkeypatch.setattr(main, "save_live_voice", saved.append)

        jarvis = JarvisLive.__new__(JarvisLive)
        jarvis._live_voice = "Achird"
        jarvis._voice_change_event = asyncio.Event()
        jarvis._voice_reconnect_requested = False
        jarvis._loop = asyncio.get_running_loop()
        jarvis.ui = type("UI", (), {"write_log": logs.append})()

        jarvis._on_live_voice_change("sulafat")
        await asyncio.sleep(0)

        assert jarvis._live_voice == "Sulafat"
        assert saved == ["Sulafat"]
        assert jarvis._voice_reconnect_requested is True
        assert jarvis._voice_change_event.is_set()
        with pytest.raises(RuntimeError, match="voice changed"):
            await jarvis._watch_live_voice_change()

    asyncio.run(scenario())


def test_la_sauvegarde_de_voix_preserve_les_autres_reglages(tmp_path, monkeypatch):
    path = tmp_path / "api_keys.json"
    path.write_text(json.dumps({
        "gemini_api_key": "secret-conserve",
        "live_model": "modele-personnalise",
        "ui_color": "#123456",
    }), encoding="utf-8")
    monkeypatch.setattr(
        config_manager._default_manager,
        "_file",
        config_manager.AtomicJSONFile(path),
    )

    config_manager.save_live_voice("Kore")

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["live_voice"] == "Kore"
    assert saved["gemini_api_key"] == "secret-conserve"
    assert saved["live_model"] == "modele-personnalise"
    assert saved["ui_color"] == "#123456"
