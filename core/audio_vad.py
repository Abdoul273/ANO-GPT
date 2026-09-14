"""Détection d'activité vocale pour le pipeline STT studio-grade.

Ordre des moteurs :

1. Silero VAD v5 ONNX (``models/silero_vad.onnx``), si le runtime natif est
   autorisé. Sous Python 3.14, ONNX Runtime a corrompu le tas lorsqu'il
   tournait dans le callback PortAudio : on respecte ``onnx_native_allowed()``
   (opt-in ``ANOGPT_ENABLE_ONNX_VAD=1``).
2. ``webrtcvad`` (déjà dans ``requirements.txt``) — trames 10/20/30 ms.
3. Heuristique énergie + planéité spectrale.

Le filtre ``filter_speech_frames`` ajoute un preroll et un hangover pour
ne pas couper le début ni la fin des mots. Les trames de capture (20 ms /
320 échantillons) sont acceptées : elles sont accumulées jusqu'à 512
échantillons pour Silero, ou scorées nativement par WebRTC.
"""

from __future__ import annotations

import collections
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional, Union

import numpy as np

from core.vad_silero import (
    CONTEXT_SAMPLES,
    FRAME_SAMPLES as SILERO_FRAME_SAMPLES,
    MODEL_PATH,
    onnx_native_allowed,
)

logger = logging.getLogger("anogpt.audio_vad")

DEFAULT_SAMPLE_RATE = 16000


@dataclass
class VADConfig:
    """Configuration du détecteur de voix."""

    threshold: float = 0.50
    sample_rate: int = DEFAULT_SAMPLE_RATE
    preroll_ms: int = 250
    hangover_ms: int = 350
    min_speech_ms: int = 96
    # 0 = doux, 3 = très restrictif. Un essai en mode 1 (pour laisser passer
    # une voix fatiguée/basse) a fait pire que le problème qu'il corrigeait :
    # sans Silero (ML, désactivé sous Python 3.14 — voir _init_silero) pour
    # distinguer une vraie phrase adressée d'un bruit de parole quelconque,
    # webrtcvad ne fait AUCUNE identification de locuteur — assoupli, il a
    # capté la voix de tiers à côté et exécuté des commandes non voulues
    # (« il m'a dit qu'il se fermait, bye, comme si je le lui avais dit »).
    # Remis au réglage d'origine, le seul qui évite ça : mieux vaut rater une
    # voix trop basse que réagir à celle de quelqu'un d'autre.
    webrtc_mode: int = 3
    model_path: Optional[Union[str, Path]] = None


def _to_float_mono(audio: Union[np.ndarray, bytes]) -> np.ndarray:
    if isinstance(audio, (bytes, bytearray)):
        arr = np.frombuffer(audio, dtype=np.int16)
    else:
        arr = np.asarray(audio)
    if arr.size == 0:
        return np.zeros(0, dtype=np.float32)
    if arr.ndim > 1:
        arr = arr[:, 0] if arr.shape[-1] > 1 else arr.squeeze()
    if np.issubdtype(arr.dtype, np.integer):
        return arr.astype(np.float32) / 32768.0
    return arr.astype(np.float32, copy=False)


class VoiceActivityDetector:
    """VAD Silero / WebRTC / heuristique, avec padding preroll + hangover."""

    def __init__(self, config: Optional[VADConfig] = None):
        self.config = config or VADConfig()
        self.sample_rate = self.config.sample_rate
        self.threshold = self.config.threshold

        self.frame_samples = SILERO_FRAME_SAMPLES
        self.frame_ms = (self.frame_samples * 1000) // max(1, self.sample_rate)
        capture_frame_ms = 20
        self.preroll_frames = max(1, self.config.preroll_ms // capture_frame_ms)
        self.hangover_frames = max(1, self.config.hangover_ms // capture_frame_ms)
        # Le filtre travaille sur les trames réellement fournies par la
        # capture (20 ms), pas sur les fenêtres internes Silero (32 ms).
        # Une division entière faisait valider 60 ms pour une configuration
        # demandant 96 ms, laissant passer certains clics ou fins de bruit.
        self.min_speech_frames = max(
            1, math.ceil(self.config.min_speech_ms / capture_frame_ms)
        )

        self._preroll_buffer: collections.deque[Union[np.ndarray, bytes]] = collections.deque(
            maxlen=max(self.preroll_frames, 1)
        )
        self._hangover_counter = 0
        self._speech_streak = 0
        self._in_speech = False
        self._last_prob = 0.0

        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros(CONTEXT_SAMPLES, dtype=np.float32)
        self._tail = np.zeros(0, dtype=np.float32)

        self.backend = "none"
        self._session = None
        self._webrtc_vad = None
        self._webrtc_kind = ""

        self._init_silero()
        if self._session is None:
            self._init_webrtc()

    def _init_silero(self) -> None:
        path = Path(self.config.model_path or MODEL_PATH)
        if not path.is_file():
            logger.debug("Modèle Silero introuvable à : %s", path)
            return
        if not onnx_native_allowed():
            logger.info(
                "Silero VAD ONNX désactivé (Python ≥ 3.14) ; "
                "repli webrtcvad. Export ANOGPT_ENABLE_ONNX_VAD=1 pour forcer."
            )
            return
        try:
            import onnxruntime as ort

            opts = ort.SessionOptions()
            opts.inter_op_num_threads = 1
            opts.intra_op_num_threads = 1
            opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            self._session = ort.InferenceSession(
                str(path), sess_options=opts, providers=["CPUExecutionProvider"]
            )
            self.backend = "silero_onnx"
            logger.info("Silero VAD v5 ONNX activé (%s)", path.name)
        except Exception as exc:
            logger.warning("Erreur chargement Silero ONNX (%s) : %s", path, exc)
            self._session = None

    def _init_webrtc(self) -> None:
        try:
            import webrtcvad

            self._webrtc_vad = webrtcvad.Vad(int(self.config.webrtc_mode))
            self._webrtc_kind = "module"
            self.backend = "webrtcvad"
            logger.info("Repli WebRTC VAD actif (mode %d)", self.config.webrtc_mode)
            return
        except Exception as exc:
            logger.debug("webrtcvad module indisponible : %s", exc)
        try:
            import _webrtcvad

            self._webrtc_vad = _webrtcvad.create()
            _webrtcvad.init(self._webrtc_vad)
            _webrtcvad.set_mode(self._webrtc_vad, self.config.webrtc_mode)
            self._webrtc_kind = "native"
            self.backend = "webrtcvad_native"
            logger.info("Repli WebRTC VAD natif actif (mode %d)", self.config.webrtc_mode)
        except Exception as exc:
            logger.debug("WebRTC VAD indisponible : %s. Repli heuristique.", exc)
            self.backend = "energy_heuristic"

    def reset(self) -> None:
        """Réinitialise l'état récurrent et les tampons (nouvelle phrase)."""
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros(CONTEXT_SAMPLES, dtype=np.float32)
        self._tail = np.zeros(0, dtype=np.float32)
        self._preroll_buffer.clear()
        self._hangover_counter = 0
        self._speech_streak = 0
        self._in_speech = False
        self._last_prob = 0.0

    def _silero_infer(self, frame: np.ndarray) -> float:
        """Une trame Silero exacte (512 échantillons float32)."""
        if self._session is None or frame.size < SILERO_FRAME_SAMPLES:
            return 0.0
        target = frame[:SILERO_FRAME_SAMPLES].astype(np.float32, copy=False)
        inp = np.concatenate((self._context, target)).reshape(1, -1).astype(np.float32)
        try:
            out, self._state = self._session.run(
                None,
                {
                    "input": inp,
                    "state": self._state,
                    "sr": np.array(self.sample_rate, dtype=np.int64),
                },
            )
            self._context = target[-CONTEXT_SAMPLES:]
            return float(out[0][0])
        except Exception as exc:
            logger.debug("Erreur inférence Silero VAD: %s", exc)
            return self._heuristic_prob(target)

    def process_frame(self, frame: np.ndarray) -> tuple[bool, float]:
        """Score une trame de longueur quelconque.

        * Silero : accumule jusqu'à 512 échantillons (32 ms).
        * WebRTC : 10/20/30 ms nativement.
        * Heuristique : n'importe quelle longueur.
        """
        target = _to_float_mono(frame)
        if target.size == 0:
            return False, 0.0

        if self._session is not None:
            combined = np.concatenate((self._tail, target)) if self._tail.size else target
            if combined.size < SILERO_FRAME_SAMPLES:
                self._tail = combined
                is_voice = self._last_prob >= self.threshold
                return is_voice, self._last_prob
            n_frames = combined.size // SILERO_FRAME_SAMPLES
            last = self._last_prob
            for i in range(n_frames):
                last = self._silero_infer(
                    combined[i * SILERO_FRAME_SAMPLES:(i + 1) * SILERO_FRAME_SAMPLES]
                )
            self._tail = combined[n_frames * SILERO_FRAME_SAMPLES:]
            self._last_prob = last
            return last >= self.threshold, last

        if self._webrtc_vad is not None:
            prob = self._webrtc_prob(target)
        else:
            prob = self._heuristic_prob(target)
        self._last_prob = prob
        return prob >= self.threshold, prob

    def _webrtc_prob(self, frame_float: np.ndarray) -> float:
        pcm16 = (np.clip(frame_float, -1.0, 1.0) * 32767.0).astype(np.int16)
        hop = 320  # 20 ms à 16 kHz — taille native webrtcvad
        if pcm16.size < 160:
            return 0.0
        if pcm16.size >= 480 and pcm16.size % 480 == 0:
            hop = 480
        elif pcm16.size >= 320:
            hop = 320
        else:
            hop = 160
        n_chunks = pcm16.size // hop
        if n_chunks == 0:
            return 0.0
        votes = 0
        for i in range(n_chunks):
            sub = pcm16[i * hop:(i + 1) * hop].tobytes()
            try:
                if self._webrtc_kind == "module":
                    if self._webrtc_vad.is_speech(sub, self.sample_rate):
                        votes += 1
                else:
                    import _webrtcvad

                    if _webrtcvad.process(
                        self._webrtc_vad, self.sample_rate, sub, hop,
                    ):
                        votes += 1
            except Exception:
                pass
        return float(votes / n_chunks)

    def _heuristic_prob(self, frame_float: np.ndarray) -> float:
        rms = float(np.sqrt(np.mean(frame_float ** 2)))
        if rms < 0.015:
            return 0.0
        spectrum = np.abs(np.fft.rfft(frame_float)) + 1e-12
        geom_mean = float(np.exp(np.mean(np.log(spectrum))))
        arith_mean = float(np.mean(spectrum))
        flatness = geom_mean / arith_mean
        if flatness < 0.45 and rms > 0.03:
            return min(1.0, 0.4 + rms * 8.0)
        return min(0.6, rms * 5.0)

    def is_speech(self, audio: Union[np.ndarray, bytes]) -> bool:
        return self.confidence(audio) >= self.threshold

    def confidence(self, audio: Union[np.ndarray, bytes]) -> float:
        """Score d'une séquence vocale soutenue, jamais celui d'un pic isolé."""
        audio_float = _to_float_mono(audio)
        if audio_float.size == 0:
            return 0.0

        hop = SILERO_FRAME_SAMPLES if self._session is not None else int(
            self.sample_rate * 0.02
        )
        hop = max(1, hop)
        n_frames = audio_float.size // hop
        minimum_frames = max(1, math.ceil(
            self.config.min_speech_ms * self.sample_rate / (1000 * hop)
        ))
        if n_frames < minimum_frames:
            return 0.0

        # Clip complet : on ne pollue pas le tampon de streaming.
        saved_tail = self._tail
        saved_state = self._state
        saved_context = self._context
        saved_last = self._last_prob
        self._tail = np.zeros(0, dtype=np.float32)

        probs: list[float] = []
        try:
            for i in range(n_frames):
                frame = audio_float[i * hop:(i + 1) * hop]
                if self._session is not None:
                    probs.append(self._silero_infer(frame))
                elif self._webrtc_vad is not None:
                    probs.append(self._webrtc_prob(frame))
                else:
                    probs.append(self._heuristic_prob(frame))
        finally:
            self._tail = saved_tail
            self._state = saved_state
            self._context = saved_context
            self._last_prob = saved_last

        if not probs:
            return 0.0
        # L'ancien maximum donnait presque 1.0 à un seul clic de 20 ms.
        # Exiger au moins min_speech_ms de scores vocaux consécutifs.
        run: collections.deque[float] = collections.deque(maxlen=minimum_frames)
        score = 0.0
        for prob in probs:
            if not math.isfinite(prob) or prob < self.threshold:
                run.clear()
                continue
            run.append(prob)
            if len(run) == minimum_frames:
                score = max(score, float(np.mean(run)))
        logger.debug(
            "[VAD] Confidence: %5.3f | Speech: %s | backend=%s | frames=%d",
            score, score >= self.threshold, self.backend, len(probs),
        )
        return score

    def filter_speech_frames(
        self, frames: Iterator[Union[np.ndarray, bytes]]
    ) -> Iterator[Union[np.ndarray, bytes]]:
        """Ne laisse passer que la parole, avec preroll et hangover.

        N'émet rien pendant le silence. Dès que la parole est confirmée,
        injecte le tampon de preroll, puis les trames courantes, et maintient
        le hangover à la fin pour ne pas scinder les mots.
        """
        for frame in frames:
            if isinstance(frame, (bytes, bytearray)):
                f_arr = np.frombuffer(frame, dtype=np.int16)
            else:
                f_arr = frame

            is_voice, prob = self.process_frame(f_arr)

            if is_voice:
                self._speech_streak += 1
                self._hangover_counter = self.hangover_frames
                if not self._in_speech and self._speech_streak >= self.min_speech_frames:
                    self._in_speech = True
                    logger.debug(
                        "[VAD] début de parole (confidence=%.3f, preroll=%d)",
                        prob, len(self._preroll_buffer),
                    )
                    while self._preroll_buffer:
                        yield self._preroll_buffer.popleft()
                if self._in_speech:
                    yield frame
                else:
                    # La phase de confirmation contient déjà les premières
                    # syllabes : les conserver avec le preroll, sans doublon.
                    self._preroll_buffer.append(frame)
            else:
                self._speech_streak = 0
                if self._in_speech:
                    if self._hangover_counter > 0:
                        self._hangover_counter -= 1
                        yield frame
                    else:
                        logger.debug("[VAD] fin de parole (hangover écoulé)")
                        self._in_speech = False
                        self._preroll_buffer.clear()
                else:
                    self._preroll_buffer.append(frame)
