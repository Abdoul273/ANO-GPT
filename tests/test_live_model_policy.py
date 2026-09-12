from core.live_model_policy import (
    DEFAULT_FALLBACK_MODEL,
    DEFAULT_PRIMARY_MODEL,
    LiveModelPolicy,
)


def test_gemini_31_est_le_moteur_principal():
    policy = LiveModelPolicy()
    assert policy.current == DEFAULT_PRIMARY_MODEL
    assert "gemini-3.1-flash-live" in policy.current


def test_un_modele_indisponible_active_le_repli_25():
    policy = LiveModelPolicy()
    assert policy.should_fallback("404 model not found for Live API")
    assert policy.activate_fallback() == DEFAULT_FALLBACK_MODEL
    assert policy.using_fallback
    assert not policy.should_fallback("model not found")


def test_une_simple_coupure_reseau_ne_change_pas_le_modele():
    policy = LiveModelPolicy()
    assert not policy.should_fallback("TimeoutError: network unavailable")
    assert policy.current == DEFAULT_PRIMARY_MODEL

