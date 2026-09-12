"""Débruitage RNNoise + contrôle automatique de gain pour le pipeline STT.

Chaîne interne d'un chunk :

1. Conversion float32 mono [-1, 1]
2. RNNoise à 48 kHz (frames de 480 échantillons = 10 ms), via ``librnnoise``
   ctypes. Rééchantillonnage entier 16 kHz ↔ 48 kHz (ratio exact 3:1),
   ``scipy.signal.resample_poly`` si SciPy est présent, interpolation
   linéaire sinon.
3. AGC vers -18 dBFS + limiteur anti-clipping. Si un binding WebRTC AGC
   est importable, il est utilisé ; sinon l'AGC logiciel ci-dessous.

Repli si ``librnnoise`` manque : retrait de la composante continue (pas de
crash, le STT continue avec un signal un peu plus sale).
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import math
import os
import threading
from dataclasses import dataclass
from typing import Optional, Union

import numpy as np

logger = logging.getLogger("anogpt.audio_denoise")

RNNOISE_FRAME_48K = 480          # 10 ms à 48 kHz
RNNOISE_FRAME_16K = 160          # 10 ms à 16 kHz (ratio exact 3:1)
RNNOISE_RATE = 48000

# SciPy coûte à lui seul une seconde d'import, et il n'est utile qu'au premier
# rééchantillonnage réel. Le charger au niveau module retardait le démarrage de
# tout ce qui touche à l'audio — donc la voix — sans rien apporter avant.
_RESAMPLE_POLY = None          # None = pas encore cherché ; False = absent
_RESAMPLE_LOCK = threading.Lock()


def _resample_poly_fn():
    """``scipy.signal.resample_poly`` s'il existe, sinon ``None``."""
    global _RESAMPLE_POLY
    if _RESAMPLE_POLY is None:
        with _RESAMPLE_LOCK:
            if _RESAMPLE_POLY is None:
                try:
                    from scipy.signal import resample_poly
                    _RESAMPLE_POLY = resample_poly
                except Exception:  # SciPy optionnel — repli interpolation linéaire
                    logger.info("SciPy absent : rééchantillonnage par interpolation.")
                    _RESAMPLE_POLY = False
    return _RESAMPLE_POLY or None


# ═══════════════════════════════════════════════════════════════════════════════
# Rééchantillonnage 16 kHz ↔ 48 kHz (ratio entier 3)
# ═══════════════════════════════════════════════════════════════════════════════

def upsample_16k_to_48k(x: np.ndarray) -> np.ndarray:
    """Interpole ×3. Longueur de sortie = 3 × len(x)."""
    if x.size == 0:
        return np.zeros(0, dtype=np.float32)
    x = np.ascontiguousarray(x, dtype=np.float32)
    target = x.size * 3
    resample = _resample_poly_fn()
    if resample is not None:
        y = np.asarray(resample(x, 3, 1), dtype=np.float32)
        if y.size < target:
            y = np.pad(y, (0, target - y.size))
        elif y.size > target:
            y = y[:target]
        return y
    out = np.empty(target, dtype=np.float32)
    nxt = np.empty_like(x)
    nxt[:-1] = x[1:]
    nxt[-1] = x[-1]
    out[0::3] = x
    out[1::3] = x * (2.0 / 3.0) + nxt * (1.0 / 3.0)
    out[2::3] = x * (1.0 / 3.0) + nxt * (2.0 / 3.0)
    return out


def downsample_48k_to_16k(x: np.ndarray) -> np.ndarray:
    """Décime ÷3 après moyenne 3-tap (anti-repliement Nyquist 16 kHz)."""
    n = (x.size // 3) * 3
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    x = np.ascontiguousarray(x[:n], dtype=np.float32)
    resample = _resample_poly_fn()
    if resample is not None:
        y = np.asarray(resample(x, 1, 3), dtype=np.float32)
        target = n // 3
        if y.size < target:
            y = np.pad(y, (0, target - y.size))
        elif y.size > target:
            y = y[:target]
        return y
    return x.reshape(-1, 3).mean(axis=1).astype(np.float32, copy=False)


def rms_dbfs(audio: np.ndarray) -> float:
    """RMS d'un signal float32 [-1, 1], en dBFS."""
    if audio.size == 0:
        return -100.0
    rms = float(np.sqrt(np.mean(np.square(audio, dtype=np.float64)))) + 1e-12
    return 20.0 * math.log10(max(1e-5, rms))


# ═══════════════════════════════════════════════════════════════════════════════
# Liaison ctypes avec librnnoise
# ═══════════════════════════════════════════════════════════════════════════════

def _find_librnnoise() -> Optional[str]:
    """Localise ``librnnoise.so`` (paquet Arch ``rnnoise``)."""
    candidates = (
        "/usr/lib/librnnoise.so",
        "/usr/lib/librnnoise.so.0",
        "/usr/lib/librnnoise.so.0.4.1",
        "/usr/local/lib/librnnoise.so",
    )
    for path in candidates:
        if os.path.isfile(path):
            return path
    return ctypes.util.find_library("rnnoise")


class RNNoiseCtypes:
    """Interface bas-niveau vers la bibliothèque C RNNoise.

    API C : ``float rnnoise_process_frame(DenoiseState *st, float *out, const float *in)``.
    Les échantillons sont des PCM 16-bit exprimés en float32 (pas [-1, 1]).
    """

    def __init__(self, lib_path: Optional[str] = None):
        self.available = False
        self._lib = None
        path = lib_path or _find_librnnoise()
        if not path:
            logger.warning(
                "librnnoise non trouvée ; repli spectral actif "
                "(pacman -S rnnoise)."
            )
            return
        try:
            self._lib = ctypes.CDLL(path)
            self._lib.rnnoise_create.argtypes = [ctypes.c_void_p]
            self._lib.rnnoise_create.restype = ctypes.c_void_p
            self._lib.rnnoise_destroy.argtypes = [ctypes.c_void_p]
            self._lib.rnnoise_destroy.restype = None
            self._lib.rnnoise_process_frame.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_float),
                ctypes.POINTER(ctypes.c_float),
            ]
            self._lib.rnnoise_process_frame.restype = ctypes.c_float
            self.available = True
            logger.debug("librnnoise chargée depuis %s", path)
        except Exception as exc:
            logger.warning("Impossible de charger librnnoise (%s) : %s", path, exc)

    def create_state(self) -> Optional[ctypes.c_void_p]:
        if not self.available or self._lib is None:
            return None
        return self._lib.rnnoise_create(None)

    def destroy_state(self, state: Optional[ctypes.c_void_p]) -> None:
        if self.available and self._lib is not None and state is not None:
            self._lib.rnnoise_destroy(state)

    def process_frame(
        self,
        state: ctypes.c_void_p,
        out_buf: ctypes.Array,
        in_buf: ctypes.Array,
    ) -> float:
        if not self.available or self._lib is None or not state:
            return 0.0
        return float(self._lib.rnnoise_process_frame(state, out_buf, in_buf))


class _DisabledRNNoise:
    """Sentinelle quand RNNoise est volontairement coupé (tests, repli)."""

    available = False

    def create_state(self):
        return None

    def destroy_state(self, state) -> None:
        return None

    def process_frame(self, *args, **kwargs) -> float:
        return 0.0


_global_rnnoise: Optional[RNNoiseCtypes] = None
_rnnoise_lock = threading.Lock()


def get_rnnoise_ctypes() -> RNNoiseCtypes:
    """Instance partagée : ``CDLL`` une seule fois."""
    global _global_rnnoise
    with _rnnoise_lock:
        if _global_rnnoise is None:
            _global_rnnoise = RNNoiseCtypes()
        return _global_rnnoise


# ═══════════════════════════════════════════════════════════════════════════════
# AGC WebRTC (optionnel) + AGC logiciel
# ═══════════════════════════════════════════════════════════════════════════════

def _try_webrtc_agc(sample_rate: int):
    """Charge un AGC WebRTC s'il existe, sinon ``None``.

    Les bindings Python de ``webrtc-audio-processing`` ne sont pas un paquet
    pip stable. On tente les noms connus et on se rabat sur l'AGC maison.
    """
    for mod_name, factory in (
        ("webrtc_audio_processing", lambda m: m.AudioProcessingModule(
            enable_agc=True, enable_ns=False, enable_aec=False,
        )),
        ("webrtc_noise_gain", None),
    ):
        try:
            module = __import__(mod_name)
        except ImportError:
            continue
        except Exception as exc:
            logger.debug("Binding WebRTC %s inutilisable : %s", mod_name, exc)
            continue
        if factory is None:
            logger.debug("Module WebRTC %s présent sans factory AGC connue", mod_name)
            continue
        try:
            apm = factory(module)
            logger.info("AGC WebRTC actif (%s, %d Hz)", mod_name, sample_rate)
            return apm
        except Exception as exc:
            logger.debug("AGC WebRTC %s refusé : %s", mod_name, exc)
    return None


@dataclass
class AGCConfig:
    """Configuration de l'AGC logiciel (cible STT : -18 dBFS)."""

    target_rms_dbfs: float = -18.0
    min_gain_db: float = -6.0
    max_gain_db: float = +18.0
    attack_ms: float = 12.0
    release_ms: float = 350.0
    noise_gate_dbfs: float = -46.0
    limiter_ceiling: float = 0.98


class AutomaticGainControl:
    """Contrôle de gain avec attaque rapide, relâchement lent et limiteur doux."""

    def __init__(self, sample_rate: int = 16000, config: Optional[AGCConfig] = None):
        self.sample_rate = sample_rate
        self.config = config or AGCConfig()
        self._current_gain = 1.0
        self._target_linear = 10.0 ** (self.config.target_rms_dbfs / 20.0)
        self._noise_linear = 10.0 ** (self.config.noise_gate_dbfs / 20.0)
        self._min_linear = 10.0 ** (self.config.min_gain_db / 20.0)
        self._max_linear = 10.0 ** (self.config.max_gain_db / 20.0)
        self._attack_sec = max(0.001, self.config.attack_ms * 0.001)
        self._release_sec = max(0.001, self.config.release_ms * 0.001)
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self._current_gain = 1.0

    @property
    def current_gain_db(self) -> float:
        return 20.0 * math.log10(max(1e-5, self._current_gain))

    def process(self, audio: np.ndarray) -> np.ndarray:
        """Applique l'AGC sur un bloc float32 [-1.0, 1.0]."""
        if audio.size == 0:
            return audio
        with self._lock:
            rms = float(np.sqrt(np.mean(audio ** 2))) + 1e-12
            dt = audio.size / max(1, self.sample_rate)
            if rms >= self._noise_linear:
                desired_gain = self._target_linear / rms
                desired_gain = max(self._min_linear, min(self._max_linear, desired_gain))
                if desired_gain < self._current_gain:
                    alpha = math.exp(-dt / self._attack_sec)
                else:
                    alpha = math.exp(-dt / self._release_sec)
                self._current_gain = alpha * self._current_gain + (1.0 - alpha) * desired_gain
            else:
                alpha = math.exp(-dt / (self._release_sec * 2.0))
                self._current_gain = alpha * self._current_gain + (1.0 - alpha) * 1.0

            scaled = audio * self._current_gain
            ceiling = self.config.limiter_ceiling
            over = np.abs(scaled) > (ceiling * 0.85)
            if np.any(over):
                knee = ceiling * 0.85
                width = ceiling * 0.15 + 1e-8
                scaled = scaled.copy()
                scaled[over] = np.sign(scaled[over]) * (
                    knee + width * np.tanh((np.abs(scaled[over]) - knee) / width)
                )
            np.clip(scaled, -1.0, 1.0, out=scaled)
            return scaled


# ═══════════════════════════════════════════════════════════════════════════════
# AudioDenoiser
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class DenoiserMetrics:
    rms_in_db: float = -100.0
    rms_out_db: float = -100.0
    snr_gain_db: float = 0.0
    agc_gain_db: float = 0.0
    last_vad_prob: float = 0.0


class AudioDenoiser:
    """Débruiteur RNNoise → AGC, à 16 kHz.

    ``process()`` traite un clip complet (longueur conservée). L'état RNN
    est conservé d'un appel à l'autre pour un flux de trames 20 ms.
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        enable_rnnoise: bool = True,
        enable_agc: bool = True,
        agc_config: Optional[AGCConfig] = None,
        rnnoise: Optional[RNNoiseCtypes] = None,
    ):
        if sample_rate != 16000:
            logger.warning(
                "AudioDenoiser est calé sur 16 kHz (reçu %d) ; RNNoise sera "
                "bypassé si le taux n'est pas 16000.",
                sample_rate,
            )
        self.sample_rate = sample_rate
        self.enable_rnnoise = enable_rnnoise and sample_rate == 16000
        self.enable_agc = enable_agc

        if rnnoise is not None:
            self._rnnoise = rnnoise
        elif self.enable_rnnoise:
            self._rnnoise = get_rnnoise_ctypes()
        else:
            self._rnnoise = _DisabledRNNoise()

        self._rnnoise_state = (
            self._rnnoise.create_state()
            if (self._rnnoise and self._rnnoise.available)
            else None
        )
        self._agc = (
            AutomaticGainControl(sample_rate=sample_rate, config=agc_config)
            if enable_agc else None
        )
        if enable_agc:
            _try_webrtc_agc(sample_rate)

        self._c_in = (ctypes.c_float * RNNOISE_FRAME_48K)()
        self._c_out = (ctypes.c_float * RNNOISE_FRAME_48K)()
        self._metrics = DenoiserMetrics()
        self._lock = threading.Lock()

    def __del__(self):
        state = getattr(self, "_rnnoise_state", None)
        rnnoise = getattr(self, "_rnnoise", None)
        if rnnoise and state:
            try:
                rnnoise.destroy_state(state)
            except Exception:
                pass

    def reset(self) -> None:
        """Réinitialise l'état RNN et l'AGC (nouveau locuteur / nouvelle session)."""
        with self._lock:
            if self._rnnoise and self._rnnoise_state:
                self._rnnoise.destroy_state(self._rnnoise_state)
                self._rnnoise_state = self._rnnoise.create_state()
            if self._agc:
                self._agc.reset()
            self._metrics = DenoiserMetrics()

    @property
    def metrics(self) -> DenoiserMetrics:
        return self._metrics

    def process(self, audio: Union[np.ndarray, bytes]) -> Union[np.ndarray, bytes]:
        """Débruite et normalise un clip (int16, float32 ou bytes PCM16)."""
        is_bytes = isinstance(audio, (bytes, bytearray))
        is_int16 = False

        if is_bytes:
            audio_arr = np.frombuffer(audio, dtype=np.int16)
            is_int16 = True
        elif isinstance(audio, np.ndarray):
            audio_arr = audio
            if np.issubdtype(audio_arr.dtype, np.integer):
                is_int16 = True
        else:
            return audio

        if audio_arr.size == 0:
            return audio

        if audio_arr.ndim > 1:
            raw_1d = audio_arr[:, 0] if audio_arr.shape[-1] > 1 else audio_arr.squeeze()
        else:
            raw_1d = audio_arr

        if is_int16:
            in_float = raw_1d.astype(np.float32) / 32768.0
        else:
            in_float = raw_1d.astype(np.float32, copy=False)

        rms_in_db = rms_dbfs(in_float)

        denoised_16k = self._process_rnnoise(in_float)

        if self._agc is not None:
            leveled_16k = self._agc.process(denoised_16k)
            agc_gain_db = self._agc.current_gain_db
        else:
            leveled_16k = denoised_16k
            agc_gain_db = 0.0

        rms_out_db = rms_dbfs(leveled_16k)

        with self._lock:
            self._metrics = DenoiserMetrics(
                rms_in_db=round(rms_in_db, 2),
                rms_out_db=round(rms_out_db, 2),
                snr_gain_db=round(max(0.0, rms_out_db - rms_in_db), 2),
                agc_gain_db=round(agc_gain_db, 2),
                last_vad_prob=self._metrics.last_vad_prob,
            )

        logger.debug(
            "[AudioDenoiser] RMS in: %5.1f dBFS | RMS out: %5.1f dBFS | AGC: %+5.1f dB",
            rms_in_db, rms_out_db, agc_gain_db,
        )

        if is_int16:
            out_pcm = (np.clip(leveled_16k, -1.0, 1.0) * 32767.0).astype(np.int16)
            if is_bytes:
                return out_pcm.tobytes()
            return out_pcm
        return leveled_16k

    def _process_rnnoise(self, audio_16k: np.ndarray) -> np.ndarray:
        """RNNoise frame-par-frame ; repli spectral si la lib est absente."""
        if (
            not self.enable_rnnoise
            or not self._rnnoise
            or not self._rnnoise.available
            or not self._rnnoise_state
            or audio_16k.size == 0
        ):
            return self._spectral_fallback(audio_16k)

        n_in = audio_16k.size
        pad = (RNNOISE_FRAME_16K - (n_in % RNNOISE_FRAME_16K)) % RNNOISE_FRAME_16K
        work = np.pad(audio_16k, (0, pad)) if pad else audio_16k
        n_frames = work.size // RNNOISE_FRAME_16K
        out_16k = np.empty(n_frames * RNNOISE_FRAME_16K, dtype=np.float32)
        vad_probs: list[float] = []

        with self._lock:
            for i in range(n_frames):
                chunk = work[i * RNNOISE_FRAME_16K:(i + 1) * RNNOISE_FRAME_16K]
                pcm_48k = upsample_16k_to_48k(chunk) * 32767.0
                if pcm_48k.size < RNNOISE_FRAME_48K:
                    pcm_48k = np.pad(pcm_48k, (0, RNNOISE_FRAME_48K - pcm_48k.size))
                self._c_in[:] = pcm_48k[:RNNOISE_FRAME_48K]
                vad = self._rnnoise.process_frame(
                    self._rnnoise_state, self._c_out, self._c_in,
                )
                vad_probs.append(vad)
                den_48k = np.ctypeslib.as_array(self._c_out).copy() / 32767.0
                den_16k = downsample_48k_to_16k(den_48k)
                if den_16k.size < RNNOISE_FRAME_16K:
                    den_16k = np.pad(den_16k, (0, RNNOISE_FRAME_16K - den_16k.size))
                out_16k[i * RNNOISE_FRAME_16K:(i + 1) * RNNOISE_FRAME_16K] = (
                    den_16k[:RNNOISE_FRAME_16K]
                )
            if vad_probs:
                self._metrics.last_vad_prob = float(np.mean(vad_probs))

        if out_16k.size < n_in:
            out_16k = np.pad(out_16k, (0, n_in - out_16k.size))
        return out_16k[:n_in]

    def _spectral_fallback(self, audio: np.ndarray) -> np.ndarray:
        """Retire le DC sans écraser les syllabes faibles déjà admises par le VAD."""
        if audio.size < 8:
            return audio
        hp = audio - float(np.mean(audio))
        return hp.astype(np.float32, copy=False)
