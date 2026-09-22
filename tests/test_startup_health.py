"""Le prévol rend des diagnostics sans ouvrir le micro ni révéler les clés."""

import json
import sys
from types import SimpleNamespace

from core import startup_health as health


def test_missing_keys_and_audio_are_reported(monkeypatch, tmp_path):
    path = tmp_path / "api_keys.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(health, "CONFIG", path)
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(query_devices=lambda: []))

    states, missing = health.collect_startup_health(False)

    assert "téléphone non connecté" in states
    assert "clé Gemini principale absente" in missing
    assert "aucun micro PC détecté" in missing
    assert "aucune sortie audio détectée" in missing


def test_configured_key_is_checked_without_exposing_it(monkeypatch, tmp_path):
    path = tmp_path / "api_keys.json"
    path.write_text(json.dumps({"gemini_api_key": "SECRET"}), encoding="utf-8")
    monkeypatch.setattr(health, "CONFIG", path)
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    monkeypatch.setattr(health, "_gemini_key_status", lambda _: "valide")
    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(query_devices=lambda: [
        {"max_input_channels": 1, "max_output_channels": 0},
        {"max_input_channels": 0, "max_output_channels": 2},
    ]))

    states, missing = health.collect_startup_health(True)

    assert "Gemini : valide" in states
    assert "téléphone connecté" in states
    assert not missing
    assert "SECRET" not in str(states)


def test_serpapi_account_check_reports_rejected_key_without_secret(monkeypatch, tmp_path):
    path = tmp_path / "api_keys.json"
    path.write_text(json.dumps({"gemini_api_key": "GEMINI", "serpapi_api_key": "SECRET"}),
                    encoding="utf-8")
    monkeypatch.setattr(health, "CONFIG", path)
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    monkeypatch.setattr(health, "_gemini_key_status", lambda _: "valide")
    monkeypatch.setattr(health, "_serpapi_key_status", lambda _: "refusée par SerpApi")
    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(query_devices=lambda: [
        {"max_input_channels": 1, "max_output_channels": 2},
    ]))

    states, missing = health.collect_startup_health()

    assert "clé SerpApi refusée" in missing
    assert "SECRET" not in str(states)
