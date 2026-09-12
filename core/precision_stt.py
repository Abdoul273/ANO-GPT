"""Vérification ponctuelle des commandes sensibles avec Gemini Transcribe.

Le PCM envoyé à Gemini 3.5 Transcribe Live traverse toujours :

    AudioCaptureStream → AudioDenoiser (RNNoise + AGC)
                       → VoiceActivityDetector (Silero / WebRTC)
                       → Gemini Transcribe Live

``TranscriptGuard`` (``core.ai_stt_corrector``) reste le second filet, en
aval de la transcription : il n'est pas remplacé, il reçoit un signal déjà
nettoyé.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Callable, Iterable

import numpy as np

from core.audio_capture import AudioCaptureStream
from core.audio_denoise import AudioDenoiser
from core.audio_vad import VoiceActivityDetector
from core.live_model_policy import TRANSCRIBE_MODEL

logger = logging.getLogger("anogpt.precision_stt")

DEFAULT_MODEL = TRANSCRIBE_MODEL
MAX_AUDIO_SECONDS = 25


def should_refine_live_turn(audio: np.ndarray | None, sample_rate: int = 16000) -> bool:
    """Évite de retarder les acquiescements, sans sacrifier une vraie phrase.

    Le second moteur est destiné à la fidélité sémantique d'une demande. Un
    "oui" ou "non" très bref n'a rien à gagner à attendre un aller-retour
    réseau ; une phrase de plus d'une demi-seconde, elle, peut contenir une
    demande Gmail, un nom ou une négation qu'il ne faut pas deviner.
    """
    if audio is None or sample_rate <= 0:
        return False
    try:
        return int(np.asarray(audio).size) >= int(sample_rate * 0.55)
    except Exception:
        return False


def _fold(text: str) -> str:
    value = unicodedata.normalize("NFKD", str(text or "").casefold())
    value = "".join(char for char in value if not unicodedata.combining(char))
    return " ".join(re.findall(r"[a-z0-9+._-]+", value))


def transcript_similarity(left: str, right: str) -> float:
    a, b = _fold(left), _fold(right)
    if not a or not b:
        return 0.0
    sequence = SequenceMatcher(None, a, b).ratio()
    a_tokens, b_tokens = set(a.split()), set(b.split())
    overlap = len(a_tokens & b_tokens) / max(1, len(a_tokens | b_tokens))
    return max(sequence, overlap)


def transcripts_agree(left: str, right: str) -> bool:
    """Même suite de mots ; casse, accents et ponctuation sont tolérés.

    Une similarité globale masque les noms, nombres, répétitions ou négations
    remplacés dans une longue phrase. Aucun seuil flou pour exécuter un tour.
    """
    def words(value):
        return re.findall(r"[^\W_]+", _fold(value), flags=re.UNICODE)
    a, b = words(left), words(right)
    return bool(a) and a == b


def transcripts_conflict(main_text: str, precise_text: str,
                         threshold: float = 0.62) -> bool:
    main_folded, precise_folded = _fold(main_text), _fold(precise_text)
    if not main_folded or not precise_folded:
        return False
    negations = {"non", "ne", "pas", "jamais", "annule", "annuler"}
    main_tokens, precise_tokens = set(main_folded.split()), set(precise_folded.split())
    main_neg = bool(main_tokens & negations)
    precise_neg = bool(precise_tokens & negations)
    if main_neg != precise_neg:
        return True
    action_groups = (
        {"supprime", "supprimer", "efface", "effacer", "detruis", "detruire"},
        {"ferme", "fermer", "quitte", "quitter", "arrete", "arreter"},
        {"ouvre", "ouvrir", "lance", "lancer", "demarre", "demarrer"},
        {"envoie", "envoyer", "expedie", "expedier"},
        {"eteins", "eteindre", "redemarre", "redemarrer"},
    )
    main_actions = {i for i, group in enumerate(action_groups) if main_tokens & group}
    precise_actions = {i for i, group in enumerate(action_groups) if precise_tokens & group}
    if main_actions and precise_actions and main_actions != precise_actions:
        return True
    main_numbers = set(re.findall(r"\b\d+(?:[.,]\d+)?\b", main_folded))
    precise_numbers = set(re.findall(r"\b\d+(?:[.,]\d+)?\b", precise_folded))
    if main_numbers and precise_numbers and main_numbers != precise_numbers:
        return True
    return transcript_similarity(main_text, precise_text) < threshold


@dataclass(frozen=True)
class PrecisionResult:
    text: str = ""
    available: bool = True
    error: str = ""


class PrecisionTranscriber:
    """Transcrit un tour borné et met en cache les appels d'un même tour.

    Le prétraitement (RNNoise + AGC + VAD) est obligatoire avant l'envoi
    à Gemini : c'est le nouveau point d'entrée audio du moteur de précision.
    """

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_MODEL,
        vocabulary: Iterable[str] = (),
        client_factory: Callable | None = None,
        denoiser: AudioDenoiser | None = None,
        vad: VoiceActivityDetector | None = None,
        require_speech: bool = True,
        enable_denoiser: bool = True,
    ):
        self.api_key = str(api_key or "").strip()
        self.model = str(model or DEFAULT_MODEL).strip()
        self.vocabulary = list(dict.fromkeys(
            " ".join(str(item or "").split())
            for item in vocabulary if str(item or "").strip()
        ))[:100]
        self._client_factory = client_factory
        self._denoiser = denoiser if denoiser is not None else AudioDenoiser(sample_rate=16000)
        self._vad = vad if vad is not None else VoiceActivityDetector()
        # La seconde passe ne doit jamais envoyer un clip sans parole au
        # service distant.  Les appelants qui ont déjà une preuve fiable de
        # parole peuvent explicitement passer ``require_speech=False`` (cas
        # exceptionnel, principalement pour le diagnostic), mais le chemin
        # normal reste fermé par défaut.
        self.require_speech = bool(require_speech)
        self.enable_denoiser = enable_denoiser
        self._cache: dict[str, PrecisionResult] = {}
        self._lock = asyncio.Lock()

    @property
    def denoiser(self) -> AudioDenoiser:
        return self._denoiser

    @property
    def vad(self) -> VoiceActivityDetector:
        return self._vad

    @staticmethod
    def _pcm(audio: np.ndarray, sample_rate: int) -> bytes:
        values = np.asarray(audio)
        if values.ndim > 1:
            values = values.mean(axis=1)
        if np.issubdtype(values.dtype, np.integer):
            return values.astype("<i2", copy=False).tobytes()
        return (np.clip(values.astype(np.float32), -1.0, 1.0) * 32767.0).astype(
            "<i2"
        ).tobytes()

    def _preprocess(self, audio: np.ndarray) -> tuple[np.ndarray, float, bool]:
        """RNNoise + AGC, puis VAD. Ne lève jamais : repli sur l'audio brut."""
        processed = audio
        if self.enable_denoiser and self._denoiser is not None:
            try:
                processed = self._denoiser.process(audio)
                metrics = self._denoiser.metrics
                logger.debug(
                    "[PrecisionSTT] Denoise: RMS in = %.1f dBFS, RMS out = %.1f dBFS, "
                    "AGC gain = %+.1f dB",
                    metrics.rms_in_db, metrics.rms_out_db, metrics.agc_gain_db,
                )
            except Exception as exc:
                logger.warning("[PrecisionSTT] Avertissement débruiteur: %s", exc)
                processed = audio

        confidence = 0.0
        is_voice = False
        if self._vad is not None:
            try:
                self._vad.reset()
                confidence = float(self._vad.confidence(processed))
                is_voice = confidence >= float(self._vad.threshold)
                logger.debug(
                    "[PrecisionSTT] VAD: confidence = %.3f | speech = %s (threshold=%.2f)",
                    confidence, is_voice, self._vad.threshold,
                )
            except Exception as exc:
                logger.warning("[PrecisionSTT] Avertissement VAD: %s", exc)
                is_voice = False
        return processed, confidence, is_voice

    def _denoise_frame(self, frame: bytes) -> bytes:
        if not (self.enable_denoiser and self._denoiser is not None):
            return frame
        try:
            cleaned = self._denoiser.process(frame)
            if isinstance(cleaned, (bytes, bytearray)):
                return bytes(cleaned)
            return np.asarray(cleaned, dtype=np.int16).tobytes()
        except Exception as exc:
            logger.warning("[PrecisionSTT] Avertissement débruiteur (trame): %s", exc)
            return frame

    async def transcribe(
        self,
        audio: np.ndarray,
        sample_rate: int = 16000,
        *,
        require_speech: bool | None = None,
        already_processed: bool = False,
    ) -> PrecisionResult:
        if sample_rate != 16000:
            return PrecisionResult(available=False, error="PCM 16 kHz requis")
        if len(audio) > sample_rate * MAX_AUDIO_SECONDS:
            return PrecisionResult(available=False, error="phrase trop longue, audio non tronqué")

        raw_pcm = self._pcm(audio, sample_rate)
        if len(raw_pcm) < int(sample_rate * 0.12) * 2:
            return PrecisionResult(available=False, error="audio trop court")

        must_require = self.require_speech if require_speech is None else bool(require_speech)
        # Un résultat de diagnostic sans VAD n'autorise pas un appel strict.
        digest = hashlib.sha256(
            bytes([must_require, bool(already_processed)]) + raw_pcm
        ).hexdigest()
        cached = self._cache.get(digest)
        if cached is not None:
            return cached

        async with self._lock:
            cached = self._cache.get(digest)
            if cached is not None:
                return cached

            if already_processed:
                processed_audio = audio
                vad_confidence = 0.0
                is_voice = False
                if self._vad is not None:
                    try:
                        self._vad.reset()
                        vad_confidence = float(self._vad.confidence(processed_audio))
                        is_voice = vad_confidence >= float(self._vad.threshold)
                    except Exception as exc:
                        logger.warning("[PrecisionSTT] Avertissement VAD: %s", exc)
            else:
                processed_audio, vad_confidence, is_voice = self._preprocess(audio)

            if must_require and not is_voice:
                logger.info(
                    "[PrecisionSTT] Énoncé rejeté : aucun contenu vocal détecté "
                    "(confidence=%.3f)",
                    vad_confidence,
                )
                return PrecisionResult(
                    available=False, error="aucun contenu vocal détecté (VAD)",
                )

            pcm = self._pcm(processed_audio, sample_rate)
            result = await self._request(pcm, sample_rate)
            if result.available and result.text:
                self._cache[digest] = result
            while len(self._cache) > 4:
                self._cache.pop(next(iter(self._cache)))
            return result

    async def transcribe_stream(
        self,
        stream: AudioCaptureStream,
        sample_rate: int = 16000,
        max_duration_seconds: float = MAX_AUDIO_SECONDS,
    ) -> PrecisionResult:
        """Capture → débruitage trame à trame → VAD → Gemini."""
        frames: list[bytes] = []
        max_frames = int(max_duration_seconds * 1000 // max(1, stream.config.frame_ms))

        def _cleaned_frames():
            for raw in stream.iter_frames(max_frames=max_frames):
                yield self._denoise_frame(bytes(raw))

        filtered = self._vad.filter_speech_frames(_cleaned_frames()) if self._vad is not None else _cleaned_frames()

        for frame in filtered:
            frames.append(bytes(frame))
            if len(frames) >= max_frames:
                break

        if not frames:
            return PrecisionResult(
                available=False,
                error="aucun contenu vocal détecté dans le flux",
            )

        combined = b"".join(frames)
        audio_arr = np.frombuffer(combined, dtype=np.int16)
        return await self.transcribe(
            audio_arr, sample_rate=sample_rate,
            require_speech=True, already_processed=True,
        )

    async def _request(self, pcm: bytes, sample_rate: int) -> PrecisionResult:
        if not self.api_key:
            return PrecisionResult(available=False, error="clé Gemini absente")
        client = None
        try:
            if self._client_factory is None:
                from google import genai

                client = genai.Client(
                    api_key=self.api_key,
                    http_options={"api_version": "v1beta"},
                )
            else:
                client = self._client_factory(self.api_key)
            text = await asyncio.wait_for(
                self._live_request(client, pcm, sample_rate), timeout=15.0,
            )
            return PrecisionResult(
                text=text, available=bool(text),
                error="" if text else "transcription vide",
            )
        except Exception as exc:
            return PrecisionResult(
                available=False,
                error=" ".join(str(exc).split())[:240] or type(exc).__name__,
            )
        finally:
            if client is not None:
                try:
                    await client.aio.aclose()
                except Exception:
                    try:
                        client.close()
                    except Exception:
                        pass

    async def _live_request(self, client, pcm: bytes, sample_rate: int) -> str:
        from google.genai import types

        config = types.LiveConnectConfig(
            response_modalities=["TEXT"],
            input_audio_transcription=types.AudioTranscriptionConfig(
                language_codes=["fr-FR"],
                custom_vocabulary=self.vocabulary,
                mode="VERBATIM",
            ),
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    disabled=True,
                )
            ),
        )
        async with client.aio.live.connect(model=self.model, config=config) as session:
            await session.send_realtime_input(activity_start=types.ActivityStart())
            # 100 ms : assez fin pour une hypothèse rapide, sans multiplier
            # les messages WebSocket sur une connexion lente.
            chunk_bytes = max(640, int(sample_rate * 0.1) * 2)
            for offset in range(0, len(pcm), chunk_bytes):
                await session.send_realtime_input(
                    audio=types.Blob(
                        data=pcm[offset:offset + chunk_bytes],
                        mime_type=f"audio/pcm;rate={sample_rate}",
                    )
                )
            await session.send_realtime_input(activity_end=types.ActivityEnd())
            segments = []
            async for message in session.receive():
                server = getattr(message, "server_content", None)
                # Les segments finalisés s'ajoutent ; les hypothèses interim
                # ne sont jamais exécutables. Attendre la fin de TOUT le tour.
                final = getattr(server, "input_transcription", None)
                final_text = getattr(final, "text", "") if final else ""
                if final_text:
                    segments.append(" ".join(str(final_text).split()))
                activity = getattr(message, "voice_activity", None)
                ended = getattr(activity, "voice_activity_type", None) == "ACTIVITY_END"
                if getattr(server, "turn_complete", False) or ended:
                    return " ".join(segments)
            return ""  # fermeture sans fin de tour : audio/texte possiblement tronqué
