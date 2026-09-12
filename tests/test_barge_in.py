"""Régression : l'assistant ne doit pas s'interrompre lui-même.

Bug corrigé ici : `AudioPreprocessor._speech_active` est un verrou maintenu
900 ms (_HANGOVER_MS) après la dernière trame voisée. L'assistant commence à
répondre bien avant la fin de ce maintien, si bien que le détecteur de parole
était encore vrai à cause de la phrase que l'utilisateur venait de finir. Le
barge-in se déclenchait alors dès le premier mot de CHAQUE réponse, `interrupt()`
armait le rejet, et le tour partait dans le `continue` de `_receive_audio`.

Micro coupé, le callback micro sort avant d'atteindre ce test — d'où le symptôme
rapporté : « il ne parle que quand je coupe le micro », briefing compris.
"""

import pytest

from main import _BARGE_ARM_S, _BARGE_CONFIRM_S, _update_barge_in


def _state():
    return {"was_speaking": False, "barge_armed": False,
            "barge_quiet_s": 0.0, "barge_voice_s": 0.0}


def _chunks(seconds):
    return int(seconds / 0.064) + 1


def test_le_verrou_residuel_ne_coupe_pas_la_reponse():
    """Le cas du bug : l'utilisateur vient de parler, le verrou est encore
    chaud, et l'assistant commence à répondre."""
    st = _state()

    # Le verrou est encore vrai (maintien de 900 ms) quand la réponse démarre.
    assert _update_barge_in(st, jarvis_speaking=True, speech_detected=True) is False
    # Et il le reste sur les chunks suivants tant qu'il n'est jamais retombé.
    for _ in range(20):
        assert _update_barge_in(st, jarvis_speaking=True, speech_detected=True) is False


def test_une_vraie_reprise_de_parole_interrompt_bien():
    st = _state()

    # Début de réponse, verrou encore chaud → pas d'interruption.
    assert _update_barge_in(st, jarvis_speaking=True, speech_detected=True) is False
    # Le verrou retombe : le silence de l'utilisateur est constaté.
    for _ in range(_chunks(_BARGE_ARM_S)):
        assert _update_barge_in(st, True, False) is False
    # L'utilisateur reprend la parole de façon soutenue → interruption.
    for _ in range(_chunks(_BARGE_CONFIRM_S) - 1):
        assert _update_barge_in(st, True, True, voice_confirmed=True) is False
    assert _update_barge_in(st, True, True, voice_confirmed=True) is True


def test_chaque_nouvelle_reponse_redesarme():
    """Sans ce ré-armement, la réponse N+1 hériterait de l'état de la N."""
    st = _state()
    for _ in range(_chunks(_BARGE_ARM_S)):
        _update_barge_in(st, jarvis_speaking=True, speech_detected=False)
    assert st["barge_armed"] is True

    # Fin de la réponse, puis début de la suivante.
    _update_barge_in(st, jarvis_speaking=False, speech_detected=True)
    assert _update_barge_in(st, jarvis_speaking=True, speech_detected=True) is False, (
        "une nouvelle réponse doit repartir désarmée"
    )


def test_pas_dinterruption_quand_lassistant_se_tait():
    st = _state()
    for _ in range(5):
        assert _update_barge_in(st, jarvis_speaking=False, speech_detected=True) is False


def test_un_vad_verrouille_par_le_ventilateur_ne_coupe_pas_la_voix():
    st = _state()
    for _ in range(_chunks(_BARGE_ARM_S)):
        _update_barge_in(st, True, False)
    assert st["barge_armed"] is True

    # Le détecteur hystérétique reste vrai, mais la preuve vocale instantanée
    # rejette le ventilateur : aucune coupure, même pendant plusieurs secondes.
    for _ in range(80):
        assert _update_barge_in(
            st, True, True, voice_confirmed=False
        ) is False


def test_un_pic_vocal_isole_ne_coupe_pas_la_reponse():
    st = _state()
    for _ in range(_chunks(_BARGE_ARM_S)):
        _update_barge_in(st, True, False)
    assert _update_barge_in(st, True, True, voice_confirmed=True) is False
    assert _update_barge_in(st, True, False, voice_confirmed=False) is False


def test_lecho_hysteretique_narme_jamais_linterruption():
    """Même si une mesure instantanée ressemble à une voix, le détecteur
    strict doit d'abord être réellement retombé avant toute coupure."""
    st = _state()
    for _ in range(_chunks(_BARGE_ARM_S)):
        assert _update_barge_in(
            st, True, speech_detected=True, voice_confirmed=False
        ) is False
    assert st["barge_armed"] is False


def test_une_interruption_confirmee_prend_moins_de_250_ms():
    st = _state()
    for _ in range(_chunks(_BARGE_ARM_S)):
        _update_barge_in(st, True, speech_detected=False, voice_confirmed=False)

    elapsed = 0.0
    while True:
        elapsed += 0.064
        if _update_barge_in(
            st, True, speech_detected=True, voice_confirmed=True,
            chunk_seconds=0.064,
        ):
            break
    assert elapsed <= 0.25


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
