"""Repli heuristique de la détection de voix (pluie, ventilateur, trafic).

Ces tests portent sur le chemin de SECOURS, utilisé quand le VAD neuronal est
absent — voir `tests/test_vad_silero.py` pour le chemin nominal. Ils forcent
donc ce repli, et emploient des signaux synthétiques : un empilement
d'harmoniques n'est pas de la parole pour un modèle entraîné, mais c'est
exactement ce que ces critères faits main savent mesurer.


`has_speech()` est hystérétique par conception : une fois la parole confirmée,
il la maintient pour ne pas couper les consonnes sourdes. C'est indispensable
pour transcrire, mais inutilisable pour décider qu'une phrase est FINIE — sous
une averse que webrtcvad tient pour voisée, le verrou ne retombe jamais, le tour
reste ouvert, le serveur n'a jamais la main et PLUS AUCUNE voix ne sort.

Une version antérieure de ce correctif ne regardait que l'énergie. Ces tests
montrent pourquoi c'était insuffisant : la pluie est plus FORTE que la voix.
"""

import numpy as np
import pytest

from core.stt import AudioPreprocessor

SR = 16000
N = 1600  # 100 ms


@pytest.fixture(autouse=True)
def _force_heuristics(monkeypatch):
    """Neutralise le VAD neuronal : c'est le repli qu'on teste ici."""
    monkeypatch.setattr(AudioPreprocessor, "_silero", staticmethod(lambda: None))


def _voice(amp: float, seed: int = 0) -> np.ndarray:
    """Voyelle voisée : fondamentale variable + harmoniques, comme des cordes
    vocales, avec du souffle ajouté pour ne pas tester un signal irréellement
    propre."""
    rng = np.random.default_rng(seed)
    t = np.arange(N) / SR
    f0 = 90 + rng.random() * 120
    s = sum((1.0 / k) * np.sin(2 * np.pi * f0 * k * t + rng.random() * 6)
            for k in range(1, 12))
    s = s + 0.25 * rng.standard_normal(N)
    return (amp * s / np.max(np.abs(s))).astype(np.float32)


def _rain(amp: float, seed: int = 0) -> np.ndarray:
    """Bruit rose : spectre proche d'une averse. Fort, continu, apériodique."""
    rng = np.random.default_rng(seed)
    w = rng.standard_normal(N)
    spec = np.fft.rfft(w)
    f = np.fft.rfftfreq(N, 1 / SR)
    f[0] = f[1]
    spec = spec / np.sqrt(f)
    x = np.fft.irfft(spec, N)
    return (amp * x / np.max(np.abs(x))).astype(np.float32)


def _pink(rng) -> np.ndarray:
    w = rng.standard_normal(N)
    spec = np.fft.rfft(w)
    f = np.fft.rfftfreq(N, 1 / SR)
    f[0] = f[1]
    return np.fft.irfft(spec / np.sqrt(f), N)


def _fan(amp: float, seed: int = 0, f0: float = 110.0,
         whoosh: float = 0.4) -> np.ndarray:
    """Ventilateur : ronflement tonal (passage de pales) + souffle rose.

    Contrairement à la pluie, c'est TRÈS périodique (harmonicité ≈ 0.9) : la
    périodicité seule ne peut pas le rejeter. Ce qui le trahit, c'est que son
    énergie reste tassée sous 300 Hz.
    """
    rng = np.random.default_rng(seed)
    t = np.arange(N) / SR
    s = (np.sin(2 * np.pi * f0 * t)
         + 0.5 * np.sin(2 * np.pi * 2 * f0 * t)
         + 0.3 * np.sin(2 * np.pi * 3 * f0 * t))
    x = s + whoosh * _pink(rng) * 3.0
    return (amp * x / np.max(np.abs(x))).astype(np.float32)


def _rms(a):
    return float(np.sqrt(np.mean(a ** 2)))


@pytest.mark.parametrize("amp", [0.05, 0.15, 0.35])
def test_la_voix_est_reconnue_a_tout_volume(amp):
    p = AudioPreprocessor()
    hits = sum(p.is_voice_like(_voice(amp, seed=i)) for i in range(20))
    assert hits == 20, f"seulement {hits}/20 chunks de voix reconnus"


@pytest.mark.parametrize("amp", [0.05, 0.15, 0.35])
def test_la_pluie_nest_pas_prise_pour_de_la_voix(amp):
    p = AudioPreprocessor()
    # Trois secondes d'averse : aucun chunk ne doit être retenu, sans quoi le
    # tour ne se refermerait jamais et l'assistant resterait muet.
    hits = sum(p.is_voice_like(_rain(amp, seed=i)) for i in range(30))
    assert hits == 0, f"{hits}/30 chunks de pluie pris pour de la voix"


def test_une_averse_plus_forte_que_la_voix_est_bien_rejetee():
    """Le cas qui condamnait l'approche par énergie seule."""
    rain, voice = _rain(0.35), _voice(0.05)
    assert _rms(rain) > _rms(voice), "prémisse du test invalide"

    p = AudioPreprocessor()
    assert p.is_voice_like(rain) is False
    assert AudioPreprocessor().is_voice_like(voice) is True


@pytest.mark.parametrize("whoosh", [0.8, 0.4, 0.2, 0.05])
def test_le_ventilateur_nest_pas_pris_pour_de_la_voix(whoosh):
    p = AudioPreprocessor()
    hits = sum(p.is_voice_like(_fan(0.2, seed=i, whoosh=whoosh)) for i in range(30))
    assert hits == 0, f"{hits}/30 chunks de ventilateur pris pour de la voix"


def test_le_ventilateur_est_periodique_donc_la_bande_vocale_est_indispensable():
    """Garde-fou : si quelqu'un retire le critère de bande en pensant que
    l'harmonicité suffit, ce test rappelle pourquoi elle ne suffit pas."""
    fan = _fan(0.2, seed=3, whoosh=0.4)

    assert AudioPreprocessor._harmonicity(fan) > AudioPreprocessor._VOICE_HARMONICITY, \
        "prémisse : un ventilateur est très périodique"
    assert AudioPreprocessor._voice_band_ratio(fan) < AudioPreprocessor._MIN_VOICE_BAND
    assert AudioPreprocessor().is_voice_like(fan) is False


def test_la_voix_reste_audible_malgre_le_ventilateur():
    """Le ventilateur tourne depuis un moment, puis l'utilisateur parle.

    C'est ce cas qui a imposé `_VOICE_SNR` : avec la marge de has_speech (4.0),
    le plancher ambiant ayant appris le ronflement, il aurait fallu parler
    quatre fois plus fort que lui — donc crier.
    """
    p = AudioPreprocessor()
    for i in range(40):
        p.is_voice_like(_fan(0.15, seed=i))
    assert p.is_voice_like(_voice(0.35) + _fan(0.15, seed=99)) is True


@pytest.mark.xfail(
    reason="Limite connue des critères spectraux : pluie + ventilateur "
           "simultanés présentent la même signature que voix + ventilateur "
           "(périodicité moyenne, bande vocale peuplée, spectre non plat). "
           "C'est précisément ce que le VAD neuronal vient résoudre — voir "
           "test_vad_silero.py, où ce même cas passe.",
    strict=True,
)
def test_pluie_et_ventilateur_ensemble_limite_connue():
    p = AudioPreprocessor()
    hits = sum(p.is_voice_like(_rain(0.2, seed=i) + _fan(0.2, seed=i))
               for i in range(30))
    assert hits == 0


def test_le_silence_nest_pas_de_la_voix():
    p = AudioPreprocessor()
    rng = np.random.default_rng(1)
    assert p.is_voice_like((rng.standard_normal(N) * 0.0005).astype(np.float32)) is False


def test_le_plancher_ambiant_apprend_le_bruit():
    """Sans cet apprentissage, la barre d'énergie resterait calée sur une pièce
    silencieuse alors que l'averse monte."""
    p = AudioPreprocessor()
    before = p._ambient_floor
    for i in range(60):
        p.is_voice_like(_rain(0.35, seed=i))
    assert p._ambient_floor > before


def test_la_voix_reste_audible_pendant_la_pluie():
    """Le vrai scénario : il pleut, et l'utilisateur parle par-dessus."""
    p = AudioPreprocessor()
    for i in range(40):
        p.is_voice_like(_rain(0.15, seed=i))       # le fond s'installe
    assert p.is_voice_like(_voice(0.35)) is True   # il parle


def test_absence_dentree_ne_casse_rien():
    p = AudioPreprocessor()
    assert p.is_voice_like(np.array([], dtype=np.float32)) is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
