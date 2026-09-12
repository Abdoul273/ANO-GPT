"""core/speaker_id.py — Reconnaître la voix de celui qui parle.

Une empreinte vocale est un vecteur de 512 nombres calculé à partir de deux
secondes de parole. Deux enregistrements de la même personne donnent deux
vecteurs voisins ; deux personnes différentes, deux vecteurs éloignés. Comparer
revient à un produit scalaire — quelques microsecondes. Tout le coût est dans
le calcul de l'empreinte, et il reste modeste : le réseau CAM++ est conçu pour
tourner sur un processeur.

Ce que ça apporte ici :

* l'assistant sait que c'est bien lui, et peut le saluer par son prénom ;
* une commande destructrice venue d'une voix inconnue est refusée. Un assistant
  vocal obéit à quiconque se trouve dans la pièce, y compris à une voix qui
  sort d'une vidéo. C'est la seule barrière possible.

Trois principes de mise en œuvre :

* **un seul fil.** `onnxruntime` prendrait les deux cœurs par défaut et
  hacherait la voix. La session est bridée à un fil, priorité à l'audio ;
* **jamais dans le callback micro.** Le calcul dure quelques dizaines de
  millisecondes : il se fait dans un thread, après la fin du tour ;
* **dégradation propre.** Sans le modèle ou sans `onnxruntime`, `available`
  vaut False et l'assistant fonctionne comme avant, sans reconnaissance.

Les caractéristiques d'entrée sont des bancs de filtres mel façon Kaldi, parce
que c'est ce que le réseau a vu à l'entraînement — la formule est reproduite
ici en numpy pour ne pas dépendre de torchaudio, qui n'est pas installé et
qui coûterait plusieurs secondes d'import.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parent.parent
MODEL_PATH = _ROOT / "models" / "speaker_campplus.onnx"
PROFILE_PATH = _ROOT / "memory" / "voiceprint.npz"

SAMPLE_RATE = 16000

# En dessous, l'empreinte est instable : deux secondes de la même voix peuvent
# alors être plus éloignées l'une de l'autre que de celle d'un inconnu.
MIN_SECONDS = 1.5
# Au-delà, la précision ne monte plus et le calcul s'allonge pour rien.
MAX_SECONDS = 8.0

# Similarité cosinus au-dessus de laquelle on considère que c'est lui. Le seuil
# est recalculé à l'inscription en fonction de la cohérence des échantillons.
DEFAULT_THRESHOLD = 0.55

# ── paramètres du banc de filtres (identiques à ceux de l'entraînement) ──────
FRAME_LENGTH = 400        # 25 ms
FRAME_SHIFT = 160         # 10 ms
FFT_SIZE = 512            # puissance de deux immédiatement au-dessus
NUM_MEL_BINS = 80
LOW_FREQ = 20.0
PREEMPHASIS = 0.97
EPSILON = np.finfo(np.float32).eps


def _mel(hz: np.ndarray | float) -> np.ndarray | float:
    return 1127.0 * np.log(1.0 + np.asarray(hz, dtype=np.float64) / 700.0)


def _mel_filterbank() -> np.ndarray:
    """Matrice triangulaire (n_bins_fft × 80), calculée une fois."""
    num_fft_bins = FFT_SIZE // 2
    fft_bin_width = SAMPLE_RATE / FFT_SIZE
    mel_low = _mel(LOW_FREQ)
    mel_high = _mel(SAMPLE_RATE / 2.0)
    mel_delta = (mel_high - mel_low) / (NUM_MEL_BINS + 1)

    bank = np.zeros((num_fft_bins, NUM_MEL_BINS), dtype=np.float32)
    freqs = np.arange(num_fft_bins) * fft_bin_width
    mels = _mel(freqs)
    for m in range(NUM_MEL_BINS):
        left = mel_low + m * mel_delta
        center = left + mel_delta
        right = center + mel_delta
        rising = (mels - left) / (center - left)
        falling = (right - mels) / (right - center)
        weights = np.minimum(rising, falling)
        # Hors du triangle, et sous la fréquence plancher, la contribution est
        # nulle : c'est ce qui écarte le ronflement du secteur et le vent.
        weights[(mels <= left) | (mels >= right) | (freqs < LOW_FREQ)] = 0.0
        bank[:, m] = weights
    return bank


_MEL_BANK = _mel_filterbank()
# Fenêtre « povey » de Kaldi : une Hann élevée à la puissance 0,85.
_WINDOW = (0.5 - 0.5 * np.cos(
    2 * np.pi * np.arange(FRAME_LENGTH) / (FRAME_LENGTH - 1))).astype(
        np.float64) ** 0.85


def fbank(audio: np.ndarray) -> np.ndarray:
    """Bancs de filtres logarithmiques, normalisés en moyenne (CMN).

    `audio` est un flottant mono 16 kHz dans [-1, 1] ; il est remis à l'échelle
    entière parce que c'est celle qu'attend la formule d'origine.
    """
    samples = np.asarray(audio, dtype=np.float64).ravel() * 32768.0
    if samples.size < FRAME_LENGTH:
        return np.zeros((0, NUM_MEL_BINS), dtype=np.float32)

    count = 1 + (samples.size - FRAME_LENGTH) // FRAME_SHIFT
    indices = (np.arange(FRAME_LENGTH)[None, :]
               + FRAME_SHIFT * np.arange(count)[:, None])
    frames = samples[indices]

    # Composante continue, puis préaccentuation : le premier échantillon est
    # traité comme s'il était précédé de lui-même, comme dans Kaldi.
    frames -= frames.mean(axis=1, keepdims=True)
    frames = np.concatenate(
        [frames[:, :1] * (1.0 - PREEMPHASIS),
         frames[:, 1:] - PREEMPHASIS * frames[:, :-1]], axis=1)
    frames *= _WINDOW

    spectrum = np.fft.rfft(frames, n=FFT_SIZE)[:, :FFT_SIZE // 2]
    power = (spectrum.real ** 2 + spectrum.imag ** 2)
    energies = np.maximum(power @ _MEL_BANK, EPSILON)
    feats = np.log(energies).astype(np.float32)
    # Normalisation en moyenne : ce qui compte est le timbre, pas le niveau du
    # micro ni la couleur de la pièce.
    return feats - feats.mean(axis=0, keepdims=True)


@dataclass
class Verdict:
    name: str          # nom du locuteur, ou "" si inconnu
    score: float       # similarité avec l'empreinte enregistrée
    known: bool
    seconds: float     # durée de parole analysée


class SpeakerID:
    """Empreinte vocale : inscription, puis vérification à chaque tour."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._session = None
        self._profile: dict | None = None
        self.available = False
        self._load()

    # ── chargement ──────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not MODEL_PATH.is_file():
            print("[Voix] modèle absent — reconnaissance du locuteur inactive "
                  "(scripts/install_speaker_id.sh)")
            return
        try:
            import onnxruntime as ort

            options = ort.SessionOptions()
            # Un seul fil : la boucle audio a besoin de l'autre cœur.
            options.intra_op_num_threads = 1
            options.inter_op_num_threads = 1
            options.log_severity_level = 3
            self._session = ort.InferenceSession(
                str(MODEL_PATH), sess_options=options,
                providers=["CPUExecutionProvider"])
            self.available = True
        except Exception as exc:
            print(f"[Voix] reconnaissance inactive : {exc}")
            return
        self._load_profile()

    def _load_profile(self) -> None:
        if not PROFILE_PATH.is_file():
            return
        try:
            data = np.load(PROFILE_PATH, allow_pickle=False)
            self._profile = {
                "name": str(data["name"]),
                "centroid": data["centroid"].astype(np.float32),
                "threshold": float(data["threshold"]),
                "samples": int(data["samples"]),
            }
            print(f"[Voix] empreinte chargée : {self._profile['name']} "
                  f"(seuil {self._profile['threshold']:.2f})")
        except Exception as exc:
            print(f"[Voix] empreinte illisible : {exc}")
            self._profile = None

    @property
    def enrolled(self) -> bool:
        return self._profile is not None

    @property
    def name(self) -> str:
        return self._profile["name"] if self._profile else ""

    # ── calcul ──────────────────────────────────────────────────────────────

    def embed(self, audio: np.ndarray) -> np.ndarray | None:
        """Empreinte normalisée d'un extrait de parole, ou None si trop court.

        Bloquant : à appeler depuis un thread, jamais depuis le callback micro.
        """
        if not self.available:
            return None
        audio = np.asarray(audio, dtype=np.float32).ravel()
        if audio.size < MIN_SECONDS * SAMPLE_RATE:
            return None
        # Trop long : on garde le milieu, généralement le plus propre — le début
        # porte l'attaque et la fin la traîne du souffle.
        limit = int(MAX_SECONDS * SAMPLE_RATE)
        if audio.size > limit:
            start = (audio.size - limit) // 2
            audio = audio[start:start + limit]

        feats = fbank(audio)
        if feats.shape[0] < 20:
            return None
        try:
            with self._lock:
                outputs = self._session.run(
                    ["embs"], {"feats": feats[None, :, :]})
        except Exception as exc:
            print(f"[Voix] calcul impossible : {exc}")
            return None
        vector = np.asarray(outputs[0][0], dtype=np.float32)
        norm = float(np.linalg.norm(vector))
        return vector / norm if norm > 0 else None

    # ── inscription ─────────────────────────────────────────────────────────

    def enroll(self, clips: list[np.ndarray], name: str) -> str:
        """Apprend une voix à partir de plusieurs extraits, et fixe son seuil.

        Le seuil n'est pas une constante : il dépend de la régularité des
        extraits fournis. Une voix enregistrée dans des conditions variables
        exige un seuil plus bas, sinon son propriétaire ne se reconnaît plus
        lui-même le lendemain.
        """
        if not self.available:
            return "La reconnaissance vocale n'est pas disponible sur cette machine."
        vectors = [v for v in (self.embed(c) for c in clips) if v is not None]
        if len(vectors) < 2:
            return ("Il me faut au moins deux extraits d'une seconde et demie "
                    "de parole claire pour apprendre une voix.")

        matrix = np.stack(vectors)
        centroid = matrix.mean(axis=0)
        centroid /= max(float(np.linalg.norm(centroid)), 1e-9)

        # Cohérence interne : à quel point ces extraits se ressemblent entre eux.
        coherence = float(np.mean(matrix @ centroid))
        threshold = float(np.clip(coherence - 0.20, 0.40, 0.65))

        self._profile = {"name": name.strip() or "l'utilisateur",
                         "centroid": centroid.astype(np.float32),
                         "threshold": threshold,
                         "samples": len(vectors)}
        self._save_profile()
        return (f"Voix apprise sur {len(vectors)} extraits "
                f"(cohérence {coherence:.2f}, seuil {threshold:.2f}).")

    def _save_profile(self) -> None:
        if not self._profile:
            return
        PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        np.savez(PROFILE_PATH,
                 name=np.array(self._profile["name"]),
                 centroid=self._profile["centroid"],
                 threshold=np.array(self._profile["threshold"]),
                 samples=np.array(self._profile["samples"]))

    def forget(self) -> str:
        self._profile = None
        try:
            PROFILE_PATH.unlink()
        except FileNotFoundError:
            return "Aucune voix n'était enregistrée."
        except Exception as exc:
            return f"Suppression impossible : {exc}"
        return "Empreinte vocale supprimée."

    # ── vérification ────────────────────────────────────────────────────────

    def verify(self, audio: np.ndarray) -> Verdict | None:
        """Compare un extrait à l'empreinte enregistrée."""
        if not self.available or not self._profile:
            return None
        vector = self.embed(audio)
        if vector is None:
            return None
        score = float(vector @ self._profile["centroid"])
        seconds = len(audio) / SAMPLE_RATE
        known = score >= self._profile["threshold"]
        return Verdict(self._profile["name"] if known else "", score, known,
                       seconds)
