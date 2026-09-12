"""VAD neuronal (Silero) — détection de parole robuste au bruit réel.

Les critères faits main de `AudioPreprocessor` (énergie, planéité spectrale,
périodicité, bande vocale) traitent correctement une averse SEULE ou un
ventilateur SEUL, mais pas les deux ensemble : cette combinaison présente la
même signature qu'une voix devant un ventilateur. Aucun réglage de seuil ne
sépare ces deux cas, parce qu'ils ne diffèrent pas sur ces mesures.

Silero VAD est un petit réseau récurrent entraîné sur de la parole réelle dans
du bruit réel. Il tourne sur `onnxruntime` (déjà installé), en ~0,1 ms par
trame de 32 ms — négligeable devant le budget d'un callback audio.

Le modèle est versionné dans `models/silero_vad.onnx`. S'il est absent ou
qu'onnxruntime manque, `available` vaut False et l'appelant retombe sur les
heuristiques : l'assistant continue de fonctionner, un peu moins bien.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Optional

import numpy as np

MODEL_PATH = Path(__file__).resolve().parent.parent / "models" / "silero_vad.onnx"

# Silero v5 exige exactement 512 échantillons par trame à 16 kHz (32 ms).
FRAME_SAMPLES = 512
SAMPLE_RATE = 16000

# ATTENTION : le modèle attend 64 échantillons de CONTEXTE concaténés devant la
# trame, soit 576 valeurs en entrée — la fin de la trame précédente. Ce détail
# n'apparaît pas dans la signature ONNX, qui accepte n'importe quelle longueur.
# Sans lui le modèle tourne sans erreur mais renvoie ~0 partout : de la vraie
# voix humaine était mesurée à 0.13, du silence à 0.001. Vérifié : avec le
# contexte, la même voix passe à 0.95 de moyenne.
CONTEXT_SAMPLES = 64


def onnx_native_allowed(version_info=None) -> bool:
    """Protect the long-lived audio process from unsafe native runtimes.

    ONNX Runtime is normally reliable, but the Python 3.14 process on this
    machine repeatedly reports glibc heap corruption while inference is driven
    by PortAudio's callback thread.  Falling back to WebRTC/spectral detection
    costs some noise robustness; it is preferable to terminating all voice and
    reminder services.  A future verified wheel can be tested explicitly.
    """
    override = os.environ.get("ANOGPT_ENABLE_ONNX_VAD", "").strip().lower()
    if override in {"1", "true", "yes", "on"}:
        return True
    if override in {"0", "false", "no", "off"}:
        return False
    version = version_info if version_info is not None else sys.version_info
    return tuple(version[:2]) < (3, 14)


class SileroVAD:
    """Enveloppe minimale et sûre autour du modèle ONNX.

    L'état récurrent est conservé entre les appels : c'est ce qui permet au
    modèle de suivre le contexte et de ne pas se laisser piéger par une syllabe
    isolée ou un claquement. `reset()` le remet à zéro entre deux sessions.
    """

    def __init__(self, model_path: Optional[Path] = None, threshold: float = 0.5):
        self.threshold = threshold
        self.available = False
        self._session = None
        self._lock = threading.Lock()
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._tail = np.zeros(0, dtype=np.float32)
        self._context = np.zeros(CONTEXT_SAMPLES, dtype=np.float32)

        path = Path(model_path) if model_path else MODEL_PATH
        if not path.exists():
            return
        if not onnx_native_allowed():
            print(
                "[SileroVAD] ONNX natif désactivé sous Python "
                f"{sys.version_info.major}.{sys.version_info.minor} pour stabilité ; "
                "repli VAD local actif."
            )
            return
        try:
            import onnxruntime as ort

            opts = ort.SessionOptions()
            # Un seul thread : on est appelé depuis le callback audio, où créer
            # des threads de calcul provoquerait des à-coups.
            opts.inter_op_num_threads = 1
            opts.intra_op_num_threads = 1
            self._session = ort.InferenceSession(
                str(path), sess_options=opts, providers=["CPUExecutionProvider"]
            )
            self.available = True
        except Exception as e:  # onnxruntime absent ou modèle illisible
            print(f"[SileroVAD] indisponible : {e}")

    def reset(self) -> None:
        with self._lock:
            self._state = np.zeros((2, 1, 128), dtype=np.float32)
            self._tail = np.zeros(0, dtype=np.float32)
            self._context = np.zeros(CONTEXT_SAMPLES, dtype=np.float32)

    def probability(self, audio: np.ndarray) -> float:
        """Probabilité robuste de parole sur le chunk.

        Une seule pointe était auparavant suffisante (`max`) : un claquement ou
        une rafale dans 32 ms pouvait donc valider tout un chunk de 64–100 ms.
        On moyenne maintenant les trois meilleures trames au maximum. La parole
        soutenue reste haute, tandis qu'un pic acoustique isolé est dilué.
        """
        if not self.available or audio.size == 0:
            return 0.0

        with self._lock:
            buf = np.concatenate((self._tail, audio.astype(np.float32)))
            n_frames = buf.size // FRAME_SAMPLES
            if n_frames == 0:
                self._tail = buf
                return 0.0

            probabilities = []
            for i in range(n_frames):
                frame = buf[i * FRAME_SAMPLES:(i + 1) * FRAME_SAMPLES]
                inp = np.concatenate((self._context, frame)).reshape(1, -1)
                try:
                    out, self._state = self._session.run(
                        None,
                        {
                            "input": inp.astype(np.float32),
                            "state": self._state,
                            "sr": np.array(SAMPLE_RATE, dtype=np.int64),
                        },
                    )
                except Exception:
                    # Un échec d'inférence ne doit jamais couper le micro.
                    return 0.0
                self._context = frame[-CONTEXT_SAMPLES:]
                probabilities.append(float(out[0][0]))

            # Garder la queue, bornée : au-delà d'une trame, c'est du retard.
            self._tail = buf[n_frames * FRAME_SAMPLES:][-FRAME_SAMPLES:]
            strongest = sorted(probabilities, reverse=True)[:3]
            if not strongest:
                return 0.0
            # Conserver la sensibilité aux syllabes faibles sans revenir au
            # « maximum gagne tout » : la moyenne stabilise le score et le pic
            # ne pèse que 70 %. La fenêtre d'attaque élimine ensuite les pics
            # réellement isolés.
            return float(0.70 * strongest[0] + 0.30 * np.mean(strongest))

    def is_speech(self, audio: np.ndarray) -> bool:
        return self.probability(audio) >= self.threshold


_instance: Optional[SileroVAD] = None
_instance_lock = threading.Lock()


def get_vad() -> SileroVAD:
    """Instance partagée : charger le modèle coûte ~50 ms, une fois suffit."""
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = SileroVAD()
        return _instance
