"""Régressions de l'anneau de sécurité Voice ID."""

from types import SimpleNamespace
import time

from main import JarvisLive


def _core(*, enrolled=True, pending=False, verdict=None, age=0.0):
    core = JarvisLive.__new__(JarvisLive)
    core._speaker = SimpleNamespace(enrolled=enrolled)
    core._speaker_check_pending = pending
    core._speaker_verdict = verdict
    core._speaker_verified_at = time.monotonic() - age
    return core


def test_voice_id_reste_optionnel_sans_profil():
    assert _core(enrolled=False)._voice_is_stranger() is False


def test_verification_en_cours_bloque_une_action_sensible():
    assert _core(pending=True)._voice_is_stranger() is True


def test_profil_configure_sans_verdict_bloque_par_defaut():
    assert _core(verdict=None)._voice_is_stranger() is True


def test_verdict_proprietaire_recent_autorise():
    verdict = SimpleNamespace(known=True)
    assert _core(verdict=verdict, age=1.0)._voice_is_stranger() is False


def test_verdict_proprietaire_perime_ne_reste_pas_un_passe_partout():
    verdict = SimpleNamespace(known=True)
    assert _core(verdict=verdict, age=31.0)._voice_is_stranger() is True


def test_voix_inconnue_est_bloquee():
    verdict = SimpleNamespace(known=False)
    assert _core(verdict=verdict, age=1.0)._voice_is_stranger() is True
