"""Régressions du portier neuronal utilisé par le callback micro."""

import numpy as np

from core.stt import AudioPreprocessor, _ensure_float32_mono_16k


class _FakeNeuralVAD:
    available = True

    def __init__(self, probabilities):
        self._probabilities = iter(probabilities)
        self.calls = 0

    def probability(self, _audio):
        self.calls += 1
        return next(self._probabilities)


def _chunk(rms=0.08):
    t = np.arange(1024, dtype=np.float32) / 16000.0
    return (rms * np.sqrt(2.0) * np.sin(2 * np.pi * 180 * t)).astype(np.float32)


def _voice_chunk(rms=0.08):
    """Signal structuré avec harmoniques dans la bande des formants."""
    t = np.arange(1024, dtype=np.float32) / 16000.0
    signal = (
        np.sin(2 * np.pi * 180 * t)
        + 0.7 * np.sin(2 * np.pi * 540 * t)
        + 0.45 * np.sin(2 * np.pi * 900 * t)
    )
    signal *= rms / np.sqrt(np.mean(signal ** 2))
    return signal.astype(np.float32)


def test_pcm_int16_est_reellement_normalise():
    pcm = np.array([-32768, 0, 32767], dtype=np.int16)
    result = _ensure_float32_mono_16k(pcm)
    assert result.dtype == np.float32
    assert np.max(np.abs(result)) <= 1.0
    assert result[0] == -1.0


def test_silero_est_le_portier_nominal_et_rejette_un_bruit_energetique(monkeypatch):
    vad = _FakeNeuralVAD([0.01] * 8)
    monkeypatch.setattr(AudioPreprocessor, "_silero", staticmethod(lambda: vad))
    p = AudioPreprocessor()

    # Signal fort, tonal et périodique : les seules heuristiques pourraient le
    # prendre pour de la voix, mais le VAD entraîné le rejette.
    assert not any(p.has_speech(_chunk(0.35).copy()) for _ in range(8))


def test_silero_ouvre_apres_une_preuve_vocale_soutenue(monkeypatch):
    vad = _FakeNeuralVAD([0.95] * 5)
    monkeypatch.setattr(AudioPreprocessor, "_silero", staticmethod(lambda: vad))
    p = AudioPreprocessor()
    decisions = [p.has_speech(_voice_chunk().copy()) for _ in range(5)]
    assert decisions[-1] is True


def test_une_voix_faible_et_irreguliere_finit_par_ouvrir(monkeypatch):
    # Alternance réaliste d'une voix fatiguée : les voyelles passent, certaines
    # consonnes restent sous le seuil. La preuve doit survivre à ces petits trous.
    probabilities = [0.70, 0.50, 0.71, 0.72, 0.48, 0.70, 0.73, 0.74]
    vad = _FakeNeuralVAD(probabilities)
    monkeypatch.setattr(AudioPreprocessor, "_silero", staticmethod(lambda: vad))
    p = AudioPreprocessor()
    decisions = [p.has_speech(_voice_chunk(0.012).copy()) for _ in probabilities]
    assert decisions[-1] is True


def test_des_pics_neuronaux_isoles_nouvrent_jamais_un_tour(monkeypatch):
    vad = _FakeNeuralVAD([0.99, 0.01] * 10)
    monkeypatch.setattr(AudioPreprocessor, "_silero", staticmethod(lambda: vad))
    p = AudioPreprocessor()
    assert not any(p.has_speech(_voice_chunk().copy()) for _ in range(20))


def test_un_unique_bruit_bref_ne_survit_pas_a_la_fenetre_dattaque(monkeypatch):
    vad = _FakeNeuralVAD([0.01] * 3 + [0.99] + [0.01] * 8)
    monkeypatch.setattr(AudioPreprocessor, "_silero", staticmethod(lambda: vad))
    p = AudioPreprocessor()
    chunks = [_voice_chunk().copy() for _ in range(12)]
    assert not any(p.has_speech(chunk) for chunk in chunks)


def test_un_chunk_nest_infere_quune_fois(monkeypatch):
    vad = _FakeNeuralVAD([0.95])
    monkeypatch.setattr(AudioPreprocessor, "_silero", staticmethod(lambda: vad))
    p = AudioPreprocessor()
    audio = _chunk()

    p.has_speech(audio)
    assert p.is_voice_like(audio) is True
    assert vad.calls == 1


def test_mode_strict_rejette_lecho_probable(monkeypatch):
    vad = _FakeNeuralVAD([0.70] * 10)
    monkeypatch.setattr(AudioPreprocessor, "_silero", staticmethod(lambda: vad))
    p = AudioPreprocessor()
    assert not any(p.has_speech(_chunk(0.10).copy(), strict=True) for _ in range(10))


def test_preuve_barge_in_rejette_un_ventilateur_meme_si_silero_se_trompe(monkeypatch):
    vad = _FakeNeuralVAD([0.99])
    monkeypatch.setattr(AudioPreprocessor, "_silero", staticmethod(lambda: vad))
    p = AudioPreprocessor()

    # Ton grave périodique : énergique, mais sans formants dans la bande vocale.
    fan = _chunk(0.35)
    assert p.is_voice_like(fan, strict=True) is False


def test_le_cache_ne_reutilise_pas_la_decision_du_chunk_precedent(monkeypatch):
    vad = _FakeNeuralVAD([0.95, 0.01])
    monkeypatch.setattr(AudioPreprocessor, "_silero", staticmethod(lambda: vad))
    p = AudioPreprocessor()
    first = _chunk()
    second = _chunk().copy()
    p.has_speech(first)
    p.has_speech(second)
    assert vad.calls == 2


def test_reduction_adaptative_ameliore_le_rapport_voix_bruit():
    p = AudioPreprocessor()
    rng = np.random.default_rng(7)
    n = 1024
    t = np.arange(n) / 16000.0
    voice = (0.12 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    noise = (0.05 * rng.standard_normal(n)).astype(np.float32)

    # Le même type de fond est appris sur plusieurs chunks silencieux.
    for _ in range(30):
        learned_noise = (0.05 * rng.standard_normal(n)).astype(np.float32)
        p._suppress_stationary_noise(learned_noise, speech_probability=0.0)

    mixed = voice + noise
    cleaned = p._suppress_stationary_noise(mixed, speech_probability=0.95)
    before = float(np.mean((mixed - voice) ** 2))

    # Comparaison à gain optimal : la suppression peut légèrement modifier le
    # niveau global, ce qui n'est pas une dégradation de l'intelligibilité.
    scale = float(np.dot(cleaned, voice) / (np.dot(cleaned, cleaned) + 1e-12))
    after = float(np.mean((cleaned * scale - voice) ** 2))
    assert after < before


def test_changement_de_micro_reinitialise_tout_letat_acoustique(monkeypatch):
    vad = _FakeNeuralVAD([0.95])
    vad.reset = lambda: setattr(vad, "reset_called", True)
    vad.reset_called = False
    monkeypatch.setattr(AudioPreprocessor, "_silero", staticmethod(lambda: vad))
    p = AudioPreprocessor()
    p._speech_active = True
    p._noise_floor = 0.2
    p._ambient_floor = 0.15
    p._prev_x = 0.4
    p._noise_spectrum = np.ones(12)

    threshold = p.noise_threshold
    p.reset_stream_state()

    assert p._speech_active is False
    assert p._noise_floor == 0.003
    assert p._ambient_floor == 0.003
    assert p._prev_x == 0.0
    assert p._noise_spectrum is None
    assert p.noise_threshold == threshold
    assert vad.reset_called is True
