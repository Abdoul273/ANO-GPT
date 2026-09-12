"""VAD neuronal : chemin nominal de la détection de parole.

Les critères faits main (voir `test_voice_like.py`) traitent une averse SEULE
ou un ventilateur SEUL, mais acceptaient les deux ensemble 100 fois sur 100 :
cette combinaison a la même signature qu'une voix devant un ventilateur. C'est
ce cas que le modèle vient résoudre.

La parole de test est synthétisée par espeak-ng plutôt qu'écrite à la main : un
empilement d'harmoniques n'est pas de la parole pour un modèle entraîné.
"""

import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
import pytest

from core.vad_silero import CONTEXT_SAMPLES, MODEL_PATH, SileroVAD
from tests.test_voice_like import _fan, _rain

N = 1600  # 100 ms, comme les chunks du micro

pytestmark = pytest.mark.skipif(
    not MODEL_PATH.exists(),
    reason=f"modèle absent ({MODEL_PATH}) — le repli heuristique prend le relais",
)


@pytest.fixture(scope="module")
def speech() -> np.ndarray:
    """Parole réelle (formants, prosodie), synthétisée à la volée."""
    if not shutil.which("espeak-ng"):
        pytest.skip("espeak-ng absent : pas de matière de parole")
    with tempfile.TemporaryDirectory() as d:
        wav = Path(d) / "p.wav"
        subprocess.run(
            ["espeak-ng", "-v", "fr", "-s", "150", "-w", str(wav),
             "Bonjour, ouvre le navigateur et cherche la météo de Conakry."],
            check=True, capture_output=True,
        )
        if not shutil.which("ffmpeg"):
            pytest.skip("ffmpeg absent : conversion impossible")
        raw = subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-i", str(wav),
             "-ar", "16000", "-ac", "1", "-f", "f32le", "-"],
            check=True, capture_output=True,
        ).stdout
    audio = np.frombuffer(raw, dtype=np.float32)
    n = (audio.size // N) * N
    return audio[:n].copy()


def _noise(gen, amp, n_chunks, seed0=0):
    return np.concatenate([gen(amp, seed=seed0 + i) for i in range(n_chunks)])


def _rate(vad, signal) -> float:
    vad.reset()
    n = len(signal) // N
    hits = sum(vad.is_speech(signal[i * N:(i + 1) * N]) for i in range(n))
    return hits / n


@pytest.fixture
def vad(monkeypatch):
    # Le test valide le modèle lui-même. En production sous Python 3.14, ce
    # runtime natif est volontairement isolé après les crashs observés dans le
    # callback PortAudio ; l'opt-in reste réservé à ce processus de test.
    monkeypatch.setenv("ANOGPT_ENABLE_ONNX_VAD", "1")
    return SileroVAD()


def test_le_modele_se_charge(vad):
    assert vad.available is True


def test_la_parole_est_reconnue(vad, speech):
    assert _rate(vad, speech) > 0.7


def test_la_parole_faible_reste_reconnue(vad, speech):
    assert _rate(vad, speech * 0.15) > 0.55


@pytest.mark.parametrize("bruit", ["pluie", "ventilateur", "les deux"])
def test_la_parole_reste_reconnue_dans_le_bruit(vad, speech, bruit):
    n = len(speech) // N
    fond = np.zeros_like(speech)
    if bruit in ("pluie", "les deux"):
        fond = fond + _noise(_rain, 0.15, n)[:len(speech)]
    if bruit in ("ventilateur", "les deux"):
        fond = fond + _noise(_fan, 0.15, n)[:len(speech)]
    assert _rate(vad, speech + fond) > 0.7


def test_pluie_et_ventilateur_ensemble_sont_rejetes(vad, speech):
    """Le cas que les heuristiques acceptaient 100 fois sur 100."""
    n = len(speech) // N
    fond = _noise(_rain, 0.15, n)[:len(speech)] + _noise(_fan, 0.15, n)[:len(speech)]
    assert _rate(vad, fond) <= 0.02


@pytest.mark.parametrize("amp", [0.15, 0.5])
def test_la_pluie_seule_est_rejetee(vad, amp):
    assert _rate(vad, _noise(_rain, amp, 60)) <= 0.02


def test_le_ventilateur_seul_est_rejete(vad):
    assert _rate(vad, _noise(_fan, 0.2, 60)) <= 0.02


def test_le_silence_est_rejete(vad):
    rng = np.random.default_rng(0)
    assert _rate(vad, (rng.standard_normal(60 * N) * 0.0005).astype(np.float32)) == 0.0


def test_le_contexte_de_64_echantillons_est_indispensable(vad, speech):
    """Régression sur un piège coûteux.

    Le modèle attend 64 échantillons de la trame précédente concaténés devant
    la trame courante. Ce détail n'apparaît pas dans la signature ONNX, qui
    accepte n'importe quelle longueur : sans lui le modèle tourne sans erreur
    et renvoie ~0 PARTOUT. De la vraie voix humaine était mesurée à 0.13, ce
    qui ressemble à un VAD très sévère plutôt qu'à un bug.
    """
    assert CONTEXT_SAMPLES == 64

    def _probs(use_context: bool) -> np.ndarray:
        session = vad._session
        state = np.zeros((2, 1, 128), dtype=np.float32)
        context = np.zeros(CONTEXT_SAMPLES, dtype=np.float32)
        out = []
        for i in range(0, len(speech) - 512, 512):
            frame = speech[i:i + 512]
            inp = np.concatenate((context, frame)) if use_context else frame
            p, state = session.run(
                None,
                {"input": inp.reshape(1, -1).astype(np.float32),
                 "state": state,
                 "sr": np.array(16000, dtype=np.int64)},
            )
            if use_context:
                context = frame[-CONTEXT_SAMPLES:]
            out.append(float(p[0][0]))
        return np.array(out)

    avec, sans = _probs(True), _probs(False)

    assert avec.mean() > 0.7, "avec le contexte, la parole doit être franche"
    assert sans.mean() < 0.1, (
        "sans le contexte, le modèle tourne sans erreur mais ne détecte plus "
        "rien — c'est ce silence trompeur qu'il faut empêcher de revenir"
    )


def test_un_modele_absent_ne_casse_rien(tmp_path):
    """L'assistant doit continuer de fonctionner sans le modèle."""
    v = SileroVAD(model_path=tmp_path / "inexistant.onnx")
    assert v.available is False
    assert v.probability(np.zeros(N, dtype=np.float32)) == 0.0
    assert v.is_speech(np.zeros(N, dtype=np.float32)) is False


def test_entree_vide_ne_casse_rien(vad):
    assert vad.probability(np.array([], dtype=np.float32)) == 0.0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
