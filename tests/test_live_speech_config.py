from core.ai_stt_corrector import STTCorrector, TranscriptAssembler, TranscriptGuard
from core.live_speech_config import (
    DEFAULT_LIVE_VOICE,
    FRENCH_TECH_PHRASES,
    FRENCH_MAIL_PHRASES,
    LIVE_VOICE_OPTIONS,
    build_input_transcription_config,
    build_output_transcription_config,
    full_duplex_aec_is_validated,
    live_captions_provider,
    live_end_silence_ms,
    normalise_language_code,
    normalise_live_voice,
)
from google.genai import types
from core.gemini_connection import (
    is_invalid_api_key_error,
    is_invalid_live_setup_error,
    is_quota_exhausted_error,
)


def test_la_langue_micro_suit_le_mode_de_conversation_explicite():
    assert normalise_language_code(None) == "fr-FR"
    assert normalise_language_code("en") == "en-US"
    cfg = build_input_transcription_config("fr")
    assert cfg.language_hints.language_codes == ["fr-FR"]
    assert "Firefox" in cfg.adaptation_phrases
    assert cfg.adaptation_phrases == [*FRENCH_TECH_PHRASES, *FRENCH_MAIL_PHRASES]
    assert "e-mail" in cfg.adaptation_phrases


def test_les_noms_personnels_enrichissent_le_lexique_sans_doublons():
    cfg = build_input_transcription_config(
        "fr",
        extra_phrases=("ANO", "Mamadou Diallo", " firefox ", ""),
    )
    assert "ANO" in cfg.adaptation_phrases
    assert "Mamadou Diallo" in cfg.adaptation_phrases
    assert sum(p.casefold() == "firefox" for p in cfg.adaptation_phrases) == 1


def test_la_sortie_suit_elle_aussi_la_langue_active():
    cfg = build_output_transcription_config()
    assert cfg.language_hints.language_codes == ["fr-FR"]
    english_cfg = build_output_transcription_config("anglais")
    assert english_cfg.language_hints.language_codes == ["en-US"]


def test_les_voix_live_sont_valides_et_normalisees():
    names = [name for name, _description in LIVE_VOICE_OPTIONS]
    assert len(names) == 30
    assert len(set(names)) == 30
    assert DEFAULT_LIVE_VOICE in names
    assert normalise_live_voice("  sulafat ") == "Sulafat"
    assert normalise_live_voice("voix-inconnue") == DEFAULT_LIVE_VOICE


def test_migration_audio_sure_par_defaut():
    assert live_captions_provider({}) == "gemini_live"
    assert live_end_silence_ms({}) is None
    assert live_end_silence_ms({"live_end_silence_ms": 700}) == 700
    assert not full_duplex_aec_is_validated({})
    assert not full_duplex_aec_is_validated({
        "voice_barge_in_enabled": True,
        "full_duplex_aec_enabled": True,
    })
    assert full_duplex_aec_is_validated({
        "voice_barge_in_enabled": True,
        "full_duplex_aec_enabled": True,
        "full_duplex_aec_validated": True,
    })


def test_la_configuration_live_complete_est_serialisable():
    cfg = types.LiveConnectConfig(
        temperature=0.2,
        input_audio_transcription=build_input_transcription_config("fr"),
        output_audio_transcription=build_output_transcription_config(),
    )
    payload = cfg.model_dump(by_alias=True, exclude_none=True)
    assert payload["inputAudioTranscription"]["languageHints"]["languageCodes"] == ["fr-FR"]
    assert "proactivity" not in payload


def test_une_nouvelle_activite_interrompt_explicitement_la_reponse():
    cfg = types.RealtimeInputConfig(
        automatic_activity_detection=types.AutomaticActivityDetection(disabled=True),
        activity_handling=types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS,
    )
    payload = cfg.model_dump(by_alias=True, exclude_none=True)
    assert payload["activityHandling"] == "START_OF_ACTIVITY_INTERRUPTS"


def test_websocket_1007_ne_signifie_pas_automatiquement_cle_invalide():
    error = (
        'APIError 1007 Invalid JSON payload received. Unknown name "proactivity" '
        "at 'setup': Cannot find field."
    )
    assert is_invalid_api_key_error(error) is False
    assert is_invalid_live_setup_error(error) is True


def test_une_vraie_erreur_de_cle_est_reconnue():
    assert is_invalid_api_key_error("400 API key not valid. Please pass a valid API key.")


def test_un_refus_de_quota_n_est_ni_une_cle_invalide_ni_un_modele_absent():
    error = "1011 You exceeded your current quota, please check your plan and billing details"
    assert is_quota_exhausted_error(error)
    assert is_quota_exhausted_error("429 Too Many Requests")
    assert is_quota_exhausted_error("quota_exceeded")
    assert not is_invalid_api_key_error(error)


def test_les_hallucinations_classiques_du_bruit_sont_rejetees():
    guard = TranscriptGuard()
    assert not guard.assess("Merci d'avoir regardé cette vidéo").accepted
    assert not guard.assess("Sous-titres réalisés par la communauté").accepted
    assert not guard.assess("bla bla bla bla bla bla").accepted
    assert not guard.assess("N'hésitez pas à vous abonner et activez la cloche").accepted


def test_une_phrase_sans_preuve_vocale_locale_est_rejetee():
    guard = TranscriptGuard()
    result = guard.assess(
        "Ouvre Firefox",
        acoustic_voice_ms=80,
        audio_duration_ms=900,
    )
    assert result.accepted is False
    assert "preuve vocale" in result.reason


def test_un_fragment_court_est_differe_tant_que_la_phrase_continue():
    guard = TranscriptGuard()
    result = guard.assess(
        "de",
        acoustic_voice_ms=70,
        audio_duration_ms=250,
        partial=True,
    )
    assert result.accepted is False
    assert result.deferred is True


def test_une_hallucination_connue_est_rejetee_meme_si_le_flux_continue():
    result = TranscriptGuard().assess(
        "Merci d'avoir regardé cette vidéo",
        acoustic_voice_ms=500,
        audio_duration_ms=1200,
        partial=True,
    )
    assert result.accepted is False
    assert result.deferred is False


def test_une_phrase_trop_longue_pour_le_son_est_rejetee():
    guard = TranscriptGuard()
    result = guard.assess(
        "ouvre le navigateur puis cherche la météo et donne moi tous les détails",
        acoustic_voice_ms=300,
        audio_duration_ms=300,
    )
    assert result.accepted is False
    assert "trop long" in result.reason


def test_les_revisions_cumulatives_ne_sont_pas_dupliquees():
    transcript = TranscriptAssembler()
    assert transcript.add("ouvre") == "ouvre"
    assert transcript.add("ouvre Firefox") == "ouvre Firefox"
    assert transcript.add("Firefox et cherche") == "ouvre Firefox et cherche"
    assert transcript.add("cherche la météo") == "ouvre Firefox et cherche la météo"
    assert transcript.add("cherche la météo") == "ouvre Firefox et cherche la météo"


def test_un_alphabet_inattendu_est_rejete_mais_les_termes_techniques_passent():
    guard = TranscriptGuard()
    assert not guard.assess("你好世界").accepted
    assert guard.assess("Ouvre VS Code et cherche GitHub").accepted
    assert guard.assess("Lance Firefox").accepted


def test_les_hallucinations_espagnoles_et_chiffres_sont_rejetees():
    guard = TranscriptGuard()
    assert not guard.assess("Yo no hablo español").accepted
    assert not guard.assess("Hola, ¿qué tal?").accepted
    assert not guard.assess("nada, sabes que sí").accepted
    assert not guard.assess("1 2 3").accepted
    assert not guard.assess("Start").accepted
    assert guard.assess("Ouvre Chrome").accepted
    assert guard.assess("Lance Android Studio").accepted
    assert guard.assess("ouvre kitty").accepted


def test_droits_dauteur_ne_devient_plus_docker():
    corrected = STTCorrector().correct("explique-moi les droits d'auteur")
    assert "Docker" not in corrected
    assert "d'auteur" in corrected
