"""Contrats du registre de modes de ton."""
from __future__ import annotations

from core.personality_modes import (
    PersonalityMode,
    active_mode,
    choose_elevenlabs_mode_voices,
    detect_mode_command,
    set_active_mode,
    user_address,
    voice_settings_for_mode,
)
from memory import config_manager


def test_astro_est_un_pote_qui_vanne_pas_un_jarvis():
    from core.personality_modes import MODE_SPECS, identity_address_line
    prompt = MODE_SPECS[PersonalityMode.ASTRO].prompt.casefold()
    assert "tutoiement" in prompt
    assert "vanne" in prompt
    assert "pote" in prompt
    assert "monsieur" in prompt
    assert "jarvis" in prompt
    assert "coquin" in prompt
    assert "mémoire émotionnelle" in prompt
    assert "répétitions" in prompt
    assert "bug" in prompt
    assert "contrat non négociable" in prompt
    assert "putain" in prompt
    assert "dit tout" in prompt
    assert "blague" in prompt
    assert "avis" in prompt
    assert "callback" in prompt
    assert "générateur de slang" in prompt
    assert detect_mode_command("Passe en mode vannes") is PersonalityMode.ASTRO
    assert detect_mode_command("passe en mode pote") is PersonalityMode.ASTRO
    addr = identity_address_line(MODE_SPECS[PersonalityMode.ASTRO]).casefold()
    assert "always call the user" not in addr
    assert "frérot" in addr
    assert "monsieur" in addr


def test_normal_coquin_majeur_ont_une_voix_nette():
    from core.personality_modes import MODE_SPECS
    normal = MODE_SPECS[PersonalityMode.NORMAL].prompt.casefold()
    assert "vouvoiement" in normal
    assert "frérot" in normal
    assert "monsieur" in normal

    coquin = MODE_SPECS[PersonalityMode.COQUIN].prompt.casefold()
    assert "tutoiement" in coquin
    assert "consent" in coquin
    assert "astro" in coquin
    assert detect_mode_command("Passe en mode complice") is PersonalityMode.COQUIN

    majeur = MODE_SPECS[PersonalityMode.MAJEUR].prompt.casefold()
    assert "monsieur" in majeur
    assert "tutoiement" in majeur
    assert "coquin" in majeur
    assert MODE_SPECS[PersonalityMode.MAJEUR].user_address == "Monsieur"


def test_commandes_de_mode_acceptent_les_aliases_et_exigent_un_ordre_explicite():
    assert detect_mode_command("Passe en mode Astro") is PersonalityMode.ASTRO
    assert detect_mode_command("reviens en mode normal") is PersonalityMode.NORMAL
    assert detect_mode_command("Mets-toi en mode majeur d'homme") is PersonalityMode.MAJEUR
    assert detect_mode_command("Le mode astro me fait rire") is None


def test_mode_et_appellation_sont_persistes(tmp_path, monkeypatch):
    manager = config_manager.ConfigManager(tmp_path / "settings.json", use_env=False)
    monkeypatch.setattr(config_manager, "_default_manager", manager)

    set_active_mode("majeur")

    assert active_mode() is PersonalityMode.MAJEUR
    assert user_address() == "Monsieur"
    assert manager._file.read()["personality_mode"] == "majeur"


def test_chaque_mode_a_une_voix_gemini_distincte():
    from core.personality_modes import MODE_SPECS
    voices = {mode: spec.gemini_voice for mode, spec in MODE_SPECS.items()}
    assert voices[PersonalityMode.NORMAL] == "Charon"
    assert voices[PersonalityMode.ASTRO] == "Fenrir"
    assert voices[PersonalityMode.COQUIN] == "Sulafat"
    assert voices[PersonalityMode.MAJEUR] == "Charon"
    named = [voices[PersonalityMode.ASTRO], voices[PersonalityMode.COQUIN], voices[PersonalityMode.MAJEUR]]
    assert len(set(named)) == 3


def test_normal_ne_change_pas_la_voix_choisie_et_astro_prend_sa_voix_gemini(tmp_path, monkeypatch):
    manager = config_manager.ConfigManager(tmp_path / "settings.json", use_env=False)
    monkeypatch.setattr(config_manager, "_default_manager", manager)
    settings = {"voice_provider": "gemini", "live_voice": "Achird"}

    set_active_mode("normal")
    assert voice_settings_for_mode(settings)["live_voice"] == "Achird"

    set_active_mode("astro")
    assert voice_settings_for_mode(settings)["live_voice"] == "Fenrir"

    set_active_mode("coquin")
    assert voice_settings_for_mode(settings)["live_voice"] == "Sulafat"

    set_active_mode("majeur")
    assert voice_settings_for_mode(settings)["live_voice"] == "Kore"


def test_elevenlabs_proposals_are_distinct_when_catalogue_le_permet():
    voices = [
        {"voice_id": "VoiceAstro", "name": "Casual FR", "label": "casual energetic French"},
        {"voice_id": "VoiceCoquin", "name": "Warm FR", "label": "warm intimate French"},
        {"voice_id": "VoiceMajeur", "name": "Formal FR", "label": "authoritative deep French"},
    ]
    selected = choose_elevenlabs_mode_voices(voices)

    assert selected == {
        "astro": "VoiceAstro",
        "coquin": "VoiceCoquin",
        "majeur": "VoiceMajeur",
    }
