from core.live_model_policy import (
    DEFAULT_FALLBACK_MODEL,
    DEFAULT_PRIMARY_MODEL,
    LiveModelPolicy,
)


def test_le_modele_live_historique_est_le_moteur_principal():
    policy = LiveModelPolicy()
    assert policy.current == DEFAULT_PRIMARY_MODEL
    assert policy.current == "models/gemini-3.8-live"


def test_un_modele_indisponible_active_le_repli_historique():
    policy = LiveModelPolicy()
    assert policy.should_fallback("404 model not found for Live API")
    assert policy.activate_fallback() == DEFAULT_FALLBACK_MODEL
    assert "gemini-3.1-flash-live-preview" in policy.current
    assert policy.using_fallback
    assert not policy.should_fallback("model not found")


def test_repli_en_chaine_si_le_quota_du_second_modele_est_epuise():
    policy = LiveModelPolicy(emergency="models/gemini-2.5-flash-native-audio-latest")
    assert policy.can_fallback()
    assert policy.activate_fallback() == DEFAULT_FALLBACK_MODEL
    assert policy.can_fallback()
    assert policy.activate_fallback() == policy.emergency
    assert not policy.can_fallback()


def test_une_simple_coupure_reseau_ne_change_pas_le_modele():
    policy = LiveModelPolicy()
    assert not policy.should_fallback("TimeoutError: network unavailable")
    assert policy.current == DEFAULT_PRIMARY_MODEL


def test_une_erreur_de_configuration_ne_change_pas_le_modele():
    policy = LiveModelPolicy()
    assert not policy.should_fallback("Invalid JSON payload: unknown setup field")
    assert policy.current == DEFAULT_PRIMARY_MODEL
