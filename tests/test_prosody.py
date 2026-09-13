"""Tests pour la prosodie adaptative (core/prosody.py)."""

from datetime import datetime
import pytest

from core.prosody import (
    AcousticProsodyAnalyzer,
    ProsodyManager,
    current_prosody_instruction,
)

import numpy as np


def test_prosody_night_time():
    mgr = ProsodyManager()

    # Soirée (22h30) -> calm_night
    night_dt = datetime(2026, 8, 16, 22, 30)
    assert mgr.is_night_time(night_dt) is True
    profile = mgr.evaluate_profile(query="Quelle heure est-il ?", current_time=night_dt)
    assert profile.mode == "calm_night"
    assert profile.speed_factor < 1.0
    assert "POSÉ" in profile.prompt_instruction

    # Journée (14h00) -> standard
    day_dt = datetime(2026, 8, 16, 14, 0)
    assert mgr.is_night_time(day_dt) is False
    profile = mgr.evaluate_profile(query="Quelle heure est-il ?", current_time=day_dt)
    assert profile.mode == "standard"


def test_prosody_urgent():
    mgr = ProsodyManager()
    day_dt = datetime(2026, 8, 16, 14, 0)

    # Mot-clé urgent
    profile = mgr.evaluate_profile(query="Alerte, coupe tout vite !", current_time=day_dt)
    assert profile.mode == "urgent"
    assert profile.speed_factor > 1.1
    assert "URGENT" in profile.prompt_instruction

    # État critique matériel (batterie faible)
    profile = mgr.evaluate_profile(
        query="Dis-moi",
        current_time=day_dt,
        system_status={"battery_percent": 5, "battery_plugged": False},
    )
    assert profile.mode == "urgent"


def test_prosody_repetition():
    mgr = ProsodyManager(history_window_s=10.0)
    day_dt = datetime(2026, 8, 16, 14, 0)

    # Requête 1
    mgr.record_user_query("baisse le son")
    profile1 = mgr.evaluate_profile("baisse le son", current_time=day_dt)
    assert profile1.mode == "standard"

    # Requête 2 (répétée)
    mgr.record_user_query("baisse le son")
    profile2 = mgr.evaluate_profile("baisse le son", current_time=day_dt)
    assert profile2.mode == "concise_repeat"
    assert "RÉPÉTITION" in profile2.prompt_instruction


def test_prosody_focus_application():
    mgr = ProsodyManager()
    day_dt = datetime(2026, 8, 16, 14, 0)

    profile = mgr.evaluate_profile(
        query="Comment compiler ce fichier ?",
        active_window="kitty — 'fish ~'",
        current_time=day_dt,
    )
    assert profile.mode == "focus"
    assert "FOCUS" in profile.prompt_instruction


def test_current_prosody_instruction():
    inst = current_prosody_instruction(query="urgent vite")
    assert "[PROSODIE ADAPTATIVE — Mode: urgent]" in inst


def _synthetic_voice(duration, syllables_s, amplitude, *, varying_pitch=False):
    sample_rate = 16000
    time = np.arange(int(sample_rate * duration), dtype=np.float32) / sample_rate
    phase = 2 * np.pi * (
        150 * time
        + (20 * np.sin(2 * np.pi * 0.7 * time) if varying_pitch else 0)
    )
    envelope = np.full(time.shape, 0.08, dtype=np.float32)
    for center in np.arange(0.15, duration, 1 / syllables_s):
        envelope += np.exp(-((time - center) / 0.055) ** 2)
    return (amplitude * np.clip(envelope, 0, 1) * np.sin(phase)).astype(np.float32)


@pytest.mark.parametrize(
    ("expected", "audio", "night"),
    [
        ("urgent", _synthetic_voice(3, 5, 0.5), False),
        ("tired", _synthetic_voice(4, 1.5, 0.025), True),
        ("enthusiastic", _synthetic_voice(4, 3, 0.5, varying_pitch=True), False),
        ("focused", _synthetic_voice(4, 3, 0.15), False),
    ],
)
def test_analyse_acoustique_distingue_les_etats(expected, audio, night):
    assessment = AcousticProsodyAnalyzer().analyze(audio, night=night)
    assert assessment.state == expected
    assert assessment.confidence >= 0.6


def test_le_style_prefere_est_persistant_et_lempathie_prioritaire(tmp_path):
    config = tmp_path / "api_keys.json"
    config.write_text("{}", encoding="utf-8")
    manager = ProsodyManager(config_path=config)

    assert manager.set_preferred_style("Tony Stark") == "Tony Stark"
    assert ProsodyManager(config_path=config).preferred_style() == "stark"

    tired = manager.evaluate_profile(
        current_time=datetime(2026, 8, 16, 14), acoustic_state="tired"
    )
    instruction = manager.format_prosody_instruction(tired)
    assert tired.mode == "tired"
    assert "jamais moqueuse" in instruction


def test_directive_live_contient_etat_et_interdit_de_le_reveler(tmp_path):
    config = tmp_path / "api_keys.json"
    config.write_text('{"prosody_style":"synthetic"}', encoding="utf-8")
    manager = ProsodyManager(config_path=config)
    directive, profile = manager.live_turn_instruction(
        _synthetic_voice(3, 5, 0.5), current_time=datetime(2026, 8, 16, 14)
    )

    assert profile.mode == "urgent"
    assert "NON PRONONÇABLE" in directive
    assert "ne révèle" in directive
    assert "ULTRA-SYNTHÉTIQUE" in directive
