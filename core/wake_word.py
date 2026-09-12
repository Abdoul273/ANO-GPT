"""
Offline wake-word detection for ANO-GPT.

Runs entirely locally (Vosk, ~40 MB French model) so that while the assistant is
muted, nothing at all is streamed to Gemini — the mic audio only ever leaves the
machine after the wake word has been heard. That privacy property is the whole
point of doing detection locally rather than letting the Live API listen.

Detection deliberately uses *open* transcription rather than Vosk's restricted
grammar mode. "ano" and "gpt" are not in the French model's vocabulary, and a
grammar built from out-of-vocabulary words collapses to whatever words remain —
Vosk then force-maps every utterance onto them, so unrelated speech ("le chat
dort sur le canapé") fires the wake word. Open transcription lets unrelated
speech decode as itself and simply score low.

Matching is therefore fuzzy by design: the model transcribes the spoken name
into whatever French words sound closest ("anneau", "a no", "annot"), so we
compare against a list of expected mis-hearings and require a high similarity
plus a word-boundary check.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable, Optional

import numpy as np

logger = logging.getLogger("anogpt.wake")

MODEL_DIR = Path.home() / ".cache" / "anogpt" / "models"
SMALL_MODEL = MODEL_DIR / "vosk-model-small-fr-0.22"
LARGE_MODEL = Path.home() / ".local" / "share" / "vosk-models" / "vosk-model-fr-0.22"

# Le microphone reste ouvert toute la journée et Vosk ne sert ici qu'au mot
# d'activation / aux commandes locales très courtes. Le grand modèle français
# est excellent pour de la dictée hors-ligne, mais mobilise plusieurs Go de RAM
# et peut affamer le callback 16 kHz sur une machine à deux cœurs. Gemini
# Transcribe est le moteur de dictée final ; le petit modèle est donc le choix
# fiable et à faible latence pour la veille locale. Le grand modèle reste
# entièrement disponible via ANOGPT_VOSK_MODEL si l'utilisateur veut un mode
# dictée hors-ligne explicite.
DEFAULT_MODEL = SMALL_MODEL if SMALL_MODEL.is_dir() else LARGE_MODEL


def resolve_vosk_model_path(model_path: Optional[Path | str] = None) -> Path:
    """Résout le modèle Vosk de veille : petit modèle rapide, puis grand repli.

    ``ANOGPT_VOSK_MODEL`` est volontairement prioritaire : il permet de lancer
    le grand modèle français pour une session de dictée hors-ligne, sans le
    rendre coûteux pour chaque réveil quotidien.
    """
    if model_path:
        return Path(model_path)
    env_path = os.environ.get("ANOGPT_VOSK_MODEL")
    if env_path:
        return Path(env_path)
    if SMALL_MODEL.is_dir() and ((SMALL_MODEL / "am").is_dir() or (SMALL_MODEL / "conf").is_dir()):
        return SMALL_MODEL
    if LARGE_MODEL.is_dir() and ((LARGE_MODEL / "am").is_dir() or (LARGE_MODEL / "conf").is_dir()):
        return LARGE_MODEL
    return DEFAULT_MODEL


def vosk_native_allowed(version_info=None) -> bool:
    """Whether libvosk may be loaded inside the assistant process.

    Both isolated launch tests and core dumps show native heap corruption when
    libvosk runs under CPython 3.14.  This failure bypasses Python exception
    handling, so keep continuous listening alive and avoid loading it there.
    """
    override = os.environ.get("ANOGPT_ENABLE_VOSK_NATIVE", "").strip().lower()
    if override in {"1", "true", "yes", "on"}:
        return True
    if override in {"0", "false", "no", "off"}:
        return False
    version = version_info if version_info is not None else sys.version_info
    return tuple(version[:2]) < (3, 14)

# Spoken forms that should wake the assistant. Kept phonetically loose because a
# small model rarely returns the "correct" spelling of a made-up name.
DEFAULT_PHRASES = [
    "ano",
    "anno",
    "anneau",
    "ano gpt",
    "anno gpt",
    "anneau gpt",
    "ano jpt",
    "ano g p t",
    "hey ano",
    "hé ano",
    "ok ano",
    "salut ano",
    "dis ano",
    "jarvis",
]


def _normalise(text: str) -> str:
    """Lowercase, strip accents and punctuation — comparison-friendly form."""
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return " ".join("".join(c if c.isalnum() else " " for c in text).split())


class WakeWordDetector:
    """
    Feed it 16 kHz mono int16 PCM; it calls `on_detect()` when a wake phrase is
    heard. Detection is debounced so one utterance cannot fire twice.

    `available` is False when the model or the vosk package is missing — callers
    should degrade to hotkey-only activation rather than crash.
    """

    def __init__(
        self,
        phrases: Optional[Iterable[str]] = None,
        model_path: Optional[Path] = None,
        sample_rate: int = 16000,
        threshold: float = 0.78,
        isolated: Optional[Any] = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.threshold = threshold
        self.phrases = [_normalise(p) for p in (phrases or DEFAULT_PHRASES)]
        self._rec = None
        self._model = None
        self._isolated = isolated
        self.available = False
        self._last_partial = ""
        # Dernier énoncé complet décodé, que le mot d'activation y soit ou non.
        # Vosk transcrit de toute façon tout ce qui est dit pendant que le micro
        # est coupé : le reste de l'application peut y lire un ordre complet
        # (« mode travail ») sans rien décoder de plus, donc sans un cycle CPU
        # supplémentaire ni un octet envoyé sur le réseau.
        self.last_text = ""
        self.last_final = ""

        path = resolve_vosk_model_path(model_path)
        if not path.is_dir() or not ((path / "am").is_dir() or (path / "conf").is_dir()):
            logger.warning(
                "Wake-word disabled: Vosk model not found at %s. "
                "Run scripts/install_wake_word.sh to fetch it.", path
            )
            return

        if not vosk_native_allowed():
            if self._isolated is None:
                from core.isolated_interrupt import IsolatedInterruptDetector
                self._isolated = IsolatedInterruptDetector(path, sample_rate)
            self.available = getattr(self._isolated, "available", False)
            return

        try:
            from vosk import Model, KaldiRecognizer, SetLogLevel

            SetLogLevel(-1)  # silence Kaldi's very chatty stderr output
            self._model = Model(str(path))
            # Open transcription (no grammar) — see module docstring for why a
            # restricted grammar produces false positives with this vocabulary.
            self._rec = KaldiRecognizer(self._model, sample_rate)
            self.available = True
            logger.info("Wake-word ready (model=%s)", path.name)
        except Exception as e:
            logger.warning("Wake-word disabled: could not load Vosk model: %s", e)

    def _score(self, text: str) -> float:
        """
        Best similarity between heard text and any configured wake phrase.

        Only whole-word windows are compared. Scoring raw substrings would match
        the name inside longer unrelated words (the "canapé" ≈ "ano" problem),
        which is exactly the false-positive class we need to avoid.
        """
        text = _normalise(text)
        if not text:
            return 0.0

        tokens = text.split()
        best = 0.0
        for phrase in self.phrases:
            p_len = len(phrase.split())
            # Compare only equal-length word windows, anchored on word boundaries.
            for i in range(len(tokens) - p_len + 1):
                window = " ".join(tokens[i:i + p_len])
                best = max(best, SequenceMatcher(None, window, phrase).ratio())
        return best

    def wait_ready(self, timeout: float = 45.0) -> bool:
        if self._isolated is None:
            return self.available
        ready = self._isolated.wait_ready(timeout)
        self.available = getattr(self._isolated, "available", False)
        return ready

    def process(self, pcm_int16: np.ndarray | bytes) -> bool:
        """
        Feed one chunk of 16 kHz mono int16 PCM. Returns True exactly once per
        detected wake phrase.
        """
        if not self.available:
            return False

        if self._isolated is not None:
            found = self._isolated.process(pcm_int16)
            self.available = getattr(self._isolated, "available", False)
            if found:
                text = str(getattr(self._isolated, "last_text", "") or "")
                self.last_final = text or ""
                if text and self._score(text) >= self.threshold:
                    logger.info("Wake word detected (isolated): %r", text)
                    self.last_text = text
                    self.last_final = ""
                    self._isolated.reset()
                    return True
            return False

        data = pcm_int16.tobytes() if isinstance(pcm_int16, np.ndarray) else pcm_int16

        try:
            if self._rec.AcceptWaveform(data):
                text = json.loads(self._rec.Result()).get("text", "")
                self._last_partial = ""
                self.last_final = text or ""
                if text and self._score(text) >= self.threshold:
                    logger.info("Wake word detected: %r", text)
                    self.last_text = text
                    # Le réveil l'emporte : cet énoncé ne doit pas être relu
                    # ensuite comme s'il s'agissait d'un ordre indépendant.
                    self.last_final = ""
                    self._rec.Reset()
                    return True
            else:
                partial = json.loads(self._rec.PartialResult()).get("partial", "")
                # Only evaluate when the partial actually changed, so a stable
                # partial can't fire repeatedly for a single utterance.
                if partial and partial != self._last_partial:
                    self._last_partial = partial
                    if self._score(partial) >= self.threshold:
                        logger.info("Wake word detected (partial): %r", partial)
                        # Un partiel s'arrête au mot d'activation : ce qui suit
                        # n'est pas encore décodé, donc pas d'ordre exploitable.
                        self.last_text = partial
                        self._rec.Reset()
                        self._last_partial = ""
                        return True
        except Exception as e:
            logger.debug("Wake-word processing error: %s", e)

        return False

    def take_final(self) -> str:
        """Rend le dernier énoncé complet décodé, une seule fois."""
        text, self.last_final = self.last_final, ""
        return text

    def reset(self) -> None:
        if self._isolated is not None:
            self._isolated.reset()
        elif self.available and self._rec is not None:
            try:
                self._rec.Reset()
            except Exception:
                pass
        self._last_partial = ""
        self.last_final = ""

    def close(self) -> None:
        if self._isolated is not None:
            self._isolated.close()
            self._isolated = None
        self.available = False


class MusicWakeWordDetector:
    """Détecteur Porcupine réservé à la musique.

    Porcupine reconnait un mot-clé acoustiquement au lieu de transcrire la
    chanson. C'est essentiel : une transcription (Vosk ou modèle distant)
    prend les paroles pour une commande et provoque les pauses fantômes.
    Cette classe n'est appelée que lorsque le lecteur est réellement en cours
    de lecture.
    """

    def __init__(
        self,
        access_key: str = "",
        keyword_paths: Optional[Iterable[str]] = None,
    ) -> None:
        self.available = False
        self.frame_length = 0
        self.sample_rate = 16000
        self._engine = None
        self._pending = np.empty(0, dtype=np.int16)

        access_key = (access_key or os.environ.get("MARK_XL_PICOVOICE_ACCESS_KEY", "")).strip()
        paths = [str(Path(path).expanduser()) for path in (keyword_paths or []) if str(path).strip()]
        if not access_key:
            logger.info("Music wake word disabled: Picovoice access key is not configured")
            return
        if paths and not all(Path(path).is_file() for path in paths):
            logger.warning("Music wake word disabled: configured .ppn file is missing")
            return
        try:
            import pvporcupine

            options = {"access_key": access_key, "sensitivities": [0.6] * (len(paths) or 1)}
            if paths:
                options["keyword_paths"] = paths
            else:
                # Mot intégré officiellement fourni par Porcupine.
                options["keywords"] = ["jarvis"]
            self._engine = pvporcupine.create(**options)
            self.frame_length = int(self._engine.frame_length)
            self.sample_rate = int(self._engine.sample_rate)
            self.available = self.frame_length > 0 and self.sample_rate == 16000
            if self.available:
                logger.info("Music wake word ready (%s)", "custom ANO" if paths else "Jarvis")
        except Exception as exc:
            logger.warning("Music wake word disabled: %s", exc)
            self.close()

    def process(self, pcm_int16: np.ndarray | bytes) -> bool:
        """Retourne True une seule fois lorsque Porcupine entend le mot-clé."""
        if not self.available or self._engine is None:
            return False
        try:
            incoming = (np.frombuffer(pcm_int16, dtype=np.int16)
                        if isinstance(pcm_int16, bytes) else np.asarray(pcm_int16, dtype=np.int16).reshape(-1))
            if not incoming.size:
                return False
            samples = np.concatenate((self._pending, incoming))
            while samples.size >= self.frame_length:
                frame, samples = samples[:self.frame_length], samples[self.frame_length:]
                if self._engine.process(frame.tolist()) >= 0:
                    self._pending = np.empty(0, dtype=np.int16)
                    return True
            self._pending = samples.copy()
        except Exception as exc:
            logger.debug("Music wake word processing error: %s", exc)
        return False

    def reset(self) -> None:
        self._pending = np.empty(0, dtype=np.int16)

    def close(self) -> None:
        if self._engine is not None:
            try:
                self._engine.delete()
            except Exception:
                pass
        self._engine = None
        self.available = False
        self._pending = np.empty(0, dtype=np.int16)
