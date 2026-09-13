"""
Text-to-Speech engines for MARK XL — Ultra‑robust & High‑performance edition.

EdgeTTS     – free Microsoft TTS (internet required)
               • Async streaming synthesis with retries
               • Voice listing, configurable timeout
Kokoro      – fully offline neural TTS (~330 MB)
               • Auto‑download & cache, GPU/CPU auto‑detection
               • Concurrent synthesis & playback
               • Async interface, streaming audio chunks
ElevenLabs  – cloud API (API key required, best quality)
               • Async, retries, voice listing
"""

from __future__ import annotations

import asyncio
import logging
import os
import queue as _queue
import sys
import threading
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Tuple,
)

import numpy as np
import sounddevice as sd
from core import action_kit as kit

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger("tts.mark_xl")
logger.setLevel(logging.INFO)
if not logger.handlers:
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    formatter = logging.Formatter("[%(levelname)s] %(name)s - %(message)s")
    ch.setFormatter(formatter)
    logger.addHandler(ch)

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
# Stop transformers from importing TensorFlow (saves startup time)
os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------
def _to_numpy(samples: Any) -> np.ndarray:
    """Convert samples to float32 numpy array (handles PyTorch tensors)."""
    if hasattr(samples, "detach"):  # PyTorch tensor
        t = samples.detach().cpu().float()
        try:
            return t.numpy()
        except RuntimeError:
            # PyTorch/numpy version mismatch fallback
            return np.asarray(t.tolist(), dtype=np.float32)
    return np.asarray(samples, dtype=np.float32)


def _compress_silence(
    arr: np.ndarray,
    sample_rate: int = 24_000,
    max_silence_ms: int = 500,
    threshold: float = 0.003,
) -> np.ndarray:
    """Shorten very long punctuation pauses (>500 ms) for Kokoro."""
    max_samp = int(max_silence_ms * sample_rate / 1000)
    frame_len = 240  # ~10 ms
    out: list[np.ndarray] = []
    silent_acc = 0

    for i in range(0, len(arr), frame_len):
        chunk = arr[i : i + frame_len]
        if np.sqrt(np.mean(chunk**2) + 1e-12) < threshold:
            silent_acc += len(chunk)
            if silent_acc <= max_samp:
                out.append(chunk)
        else:
            silent_acc = 0
            out.append(chunk)
    return np.concatenate(out) if out else arr


def _play_np(samples: np.ndarray, sample_rate: int) -> None:
    """Play float32 audio via sounddevice."""
    sd.play(samples, sample_rate)
    sd.wait()


def _play_audio_bytes(audio_bytes: bytes) -> None:
    """Decode MP3/WAV/OGG bytes and play via sounddevice.

    ``miniaudio`` is the fast in-process decoder.  On Arch it is not always
    packaged alongside the application, so retain a small ``ffmpeg`` fallback
    rather than dropping the entire spoken response with ImportError.
    """
    try:
        import miniaudio
    except ModuleNotFoundError:
        # ffmpeg is part of the normal ANO-GPT media stack.  Decode to a fixed
        # mono 24 kHz float stream: no temporary file and no extra UI work.
        try:
            result = kit.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-i", "pipe:0", "-f", "f32le", "-acodec", "pcm_f32le",
                    "-ac", "1", "-ar", "24000", "pipe:1",
                ],
                stdin=audio_bytes, timeout=30, binary=True,
            )
            if not result.ok:
                raise RuntimeError(result.reason())
            decoded = result.stdout
        except OSError as exc:
            raise RuntimeError(f"Décodage audio impossible (miniaudio/ffmpeg) : {exc}") from exc
        samples = np.frombuffer(decoded, dtype=np.float32)
        sample_rate = 24_000
    else:
        decoded = miniaudio.decode(
            audio_bytes,
            output_format=miniaudio.SampleFormat.FLOAT32,
            nchannels=1,
        )
        samples = np.array(decoded.samples, dtype=np.float32)
        sample_rate = decoded.sample_rate
    if not len(samples):
        raise RuntimeError("Le décodeur audio n'a produit aucun échantillon.")
    sd.play(samples, sample_rate)
    sd.wait()


# ---------------------------------------------------------------------------
# Base engine interface
# ---------------------------------------------------------------------------
class BaseTTSEngine(ABC):
    """Abstract TTS engine that must implement synchronous and async speak."""

    @abstractmethod
    def speak(self, text: str) -> None:
        """Synthesise and play audio (blocking)."""
        ...

    async def speak_async(self, text: str) -> None:
        """Non‑blocking version of speak (default runs in thread)."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.speak, text)

    @abstractmethod
    def voices(self) -> List[Dict[str, str]]:
        """Return available voices as list of {id, name, ...}."""
        ...

    def stop(self) -> None:
        """Stop any ongoing playback."""
        sd.stop()


# ---------------------------------------------------------------------------
# Configuration dataclasses
# ---------------------------------------------------------------------------
@dataclass
class EdgeTTSConfig:
    voice: str = "en-US-GuyNeural"
    rate: str = "+0%"          # speed modifier, e.g. "+20%"
    pitch: str = "+0Hz"
    volume: str = "+0%"
    timeout: float = 15.0      # seconds
    max_retries: int = 2
    proxy: Optional[str] = None


@dataclass
class KokoroConfig:
    voice: str = "af_heart"    # voice name, e.g. "af_heart"
    speed: float = 1.0
    lang_code: Optional[str] = None   # auto‑detect from voice prefix
    device: str = "auto"       # "auto", "cuda", "cpu"
    sample_rate: int = 24000
    max_silence_ms: int = 500
    silence_threshold: float = 0.003
    download_timeout: int = 120
    warmup_text: str = "Hello."
    # Advanced: split long text into sentences for lower latency
    split_pattern: str = r'(?<=[.!?])\s+'


@dataclass
class ElevenLabsConfig:
    api_key: str = ""
    voice_id: str = "pNInz6obpgDQGcFmaJgB"
    model_id: str = "eleven_multilingual_v2"
    stability: float = 0.5
    similarity_boost: float = 0.75
    timeout: float = 30.0
    max_retries: int = 1


def _run_sync(coro_factory, executor: ThreadPoolExecutor):
    """Exécute une coroutine depuis du code synchrone, sans boucle jetable.

    ``asyncio.run`` gère lui-même la boucle et sa fermeture (générateurs
    asynchrones, executor par défaut compris) — ce que ``new_event_loop()`` +
    ``run_until_complete`` ne faisait pas, en laissant une boucle orpheline par
    phrase. Appelé depuis un thread qui a déjà une boucle en cours, le travail
    part dans le worker du moteur pour ne pas la bloquer ni la réentrer.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro_factory())
    return executor.submit(lambda: asyncio.run(coro_factory())).result()


# ---------------------------------------------------------------------------
# EdgeTTS – ultra‑robust with retries & async
# ---------------------------------------------------------------------------
class EdgeTTSEngine(BaseTTSEngine):
    """Free Microsoft TTS – internet required."""

    _missing_dependency_warned = False

    def __init__(self, config: Optional[EdgeTTSConfig] = None, **kwargs):
        self._cfg = config or EdgeTTSConfig(**kwargs)
        self._executor = ThreadPoolExecutor(max_workers=1)

    def speak(self, text: str) -> None:
        """Synthesise and play (blocking)."""
        audio_bytes = _run_sync(lambda: self._synth(text), self._executor)
        if audio_bytes:
            _play_audio_bytes(audio_bytes)

    async def speak_async(self, text: str) -> None:
        """Async synthesis + playback without blocking the event loop."""
        audio_bytes = await self._synth(text)
        if audio_bytes:
            # Play in executor so audio doesn't block async tasks
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(self._executor, _play_audio_bytes, audio_bytes)

    async def _synth(self, text: str) -> bytes:
        """Synthesise with retries and timeout."""
        try:
            import edge_tts
        except ModuleNotFoundError:
            # La voix principale provient de Gemini Live. Les murmures locaux
            # sont optionnels : leur dépendance absente ne doit produire ni
            # traceback ni carte d'erreur pendant le briefing.
            if not self.__class__._missing_dependency_warned:
                logger.warning("edge-tts absent : synthèse locale optionnelle désactivée")
                self.__class__._missing_dependency_warned = True
            return b""

        last_err = None
        for attempt in range(self._cfg.max_retries + 1):
            try:
                comm = edge_tts.Communicate(
                    text,
                    self._cfg.voice,
                    rate=self._cfg.rate,
                    pitch=self._cfg.pitch,
                    volume=self._cfg.volume,
                    proxy=self._cfg.proxy,
                )
                buf = bytearray()
                async for chunk in comm.stream():
                    if chunk["type"] == "audio":
                        buf.extend(chunk["data"])
                return bytes(buf)
            except Exception as e:
                last_err = e
                logger.warning("EdgeTTS attempt %d failed: %s", attempt + 1, e)
                if attempt < self._cfg.max_retries:
                    await asyncio.sleep(2 ** attempt)
        raise RuntimeError(
            f"EdgeTTS synthesis failed after {self._cfg.max_retries + 1} attempts. "
            f"Check internet connection. Last error: {last_err}"
        )

    def voices(self) -> List[Dict[str, str]]:
        """Return list of available voices (short list of common ones)."""
        # EdgeTTS voice list is huge; we provide a curated selection.
        return [
            {"id": "en-US-GuyNeural", "name": "Guy (US male)"},
            {"id": "en-US-JennyNeural", "name": "Jenny (US female)"},
            {"id": "en-GB-RyanNeural", "name": "Ryan (UK male)"},
            {"id": "en-GB-SoniaNeural", "name": "Sonia (UK female)"},
            {"id": "fr-FR-HenriNeural", "name": "Henri (FR male)"},
            {"id": "fr-FR-DeniseNeural", "name": "Denise (FR female)"},
            {"id": "de-DE-ConradNeural", "name": "Conrad (DE male)"},
            {"id": "de-DE-KatjaNeural", "name": "Katja (DE female)"},
            {"id": "ja-JP-NanamiNeural", "name": "Nanami (JP female)"},
            {"id": "zh-CN-XiaoxiaoNeural", "name": "Xiaoxiao (CN female)"},
        ]


# ---------------------------------------------------------------------------
# Kokoro – fully offline, GPU‑accelerated, concurrent
# ---------------------------------------------------------------------------
# Language code mapping from voice prefix
_KOKORO_LANG_CODES = {
    "a": "a",  # American English
    "b": "b",  # British English
    "j": "j",  # Japanese
    "z": "z",  # Mandarin Chinese
    "s": "s",  # Spanish
    "f": "f",  # French
    "h": "h",  # Hindi
    "i": "i",  # Italian
    "p": "p",  # Brazilian Portuguese
    "r": "r",  # Russian
    "e": "e",  # German
}

# Model cache: (lang_code, device) → KPipeline instance
_KOKORO_PIPELINE_CACHE: Dict[Tuple[str, str], Any] = {}


def _import_kokoro_pipeline():
    """Import KPipeline, auto‑upgrade kokoro on version mismatch."""
    def _try_import():
        from kokoro import KPipeline
        return KPipeline

    try:
        return _try_import()
    except Exception as first_err:
        err_msg = str(first_err)
        if not any(marker in err_msg for marker in ("AlbertModel", "AutoModel", "cannot import name")):
            raise RuntimeError(f"Kokoro import failed: {first_err}\nRun: pip install kokoro>=0.9 soundfile") from first_err

        logger.warning("Kokoro/transformers version mismatch — upgrading kokoro…")
        result = kit.run(
            [sys.executable, "-m", "pip", "install", "kokoro>=0.9",
             "--upgrade", "--quiet", "--disable-pip-version-check"],
            timeout=900,
        )
        if result.returncode != 0:
            stderr = str(result.stderr).strip()
            raise RuntimeError(f"Kokoro auto‑upgrade failed: {stderr[:200]}") from first_err

        # Flush import cache
        stale = [k for k in sys.modules if k == "kokoro" or k.startswith("kokoro.")]
        for key in stale:
            del sys.modules[key]

        logger.info("Retrying Kokoro import…")
        try:
            return _try_import()
        except Exception as retry_err:
            raise RuntimeError(f"Kokoro still broken after upgrade: {retry_err}") from retry_err


class KokoroTTSEngine(BaseTTSEngine):
    """
    Offline Kokoro neural TTS (~330 MB model). First use downloads model.
    Subsequent starts load from cache. GPU inference is ~10× faster.
    """

    def __init__(self, config: Optional[KokoroConfig] = None, **kwargs):
        self._cfg = config or KokoroConfig(**kwargs)
        self._lock = threading.Lock()
        self._pipeline = None
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._init()

    @property
    def _lang_code(self) -> str:
        if self._cfg.lang_code:
            return self._cfg.lang_code
        prefix = self._cfg.voice[0].lower() if self._cfg.voice else "a"
        return _KOKORO_LANG_CODES.get(prefix, "a")

    @property
    def _device(self) -> str:
        """Resolve device string."""
        if self._cfg.device != "auto":
            return self._cfg.device
        try:
            import torch
            return "cuda" if torch.cuda.is_available() else "cpu"
        except ImportError:
            return "cpu"

    def _load_pipeline(self):
        """Load or retrieve cached KPipeline."""
        lang = self._lang_code
        device = self._device
        cache_key = (lang, device)
        if cache_key in _KOKORO_PIPELINE_CACHE:
            self._pipeline = _KOKORO_PIPELINE_CACHE[cache_key]
            return

        KPipeline = _import_kokoro_pipeline()
        logger.info("Loading Kokoro (lang=%s, device=%s)…", lang, device)

        # Customise thread count for CPU
        if device == "cpu":
            import torch
            try:
                n_threads = max(1, min(4, (os.cpu_count() or 4) // 2))
                torch.set_num_threads(n_threads)
                torch.set_num_interop_threads(2)
            except RuntimeError:
                pass

        def _create():
            try:
                return KPipeline(lang_code=lang, device=device)
            except TypeError:
                return KPipeline(lang_code=lang)  # older kokoro

        for attempt in range(2):  # one download retry
            try:
                self._pipeline = _create()
                _KOKORO_PIPELINE_CACHE[cache_key] = self._pipeline
                break
            except Exception as e:
                if attempt == 0 and any(k in str(e).lower() for k in (
                        "offline", "not found", "cache", "localentry",
                        "does not exist", "outgoing", "local_files_only")):
                    logger.warning("Kokoro model not cached – downloading (~330 MB)…")
                    os.environ.pop("HF_HUB_OFFLINE", None)
                    os.environ.pop("TRANSFORMERS_OFFLINE", None)
                    os.environ.pop("HF_DATASETS_OFFLINE", None)
                    continue
                raise RuntimeError(f"Kokoro pipeline creation failed: {e}") from e

        # Warmup: compile JIT graph
        logger.info("Compiling Kokoro (first‑time)…")
        try:
            for _, _, audio in self._pipeline(
                self._cfg.warmup_text,
                voice=self._cfg.voice,
                speed=self._cfg.speed,
            ):
                pass
            logger.info("Kokoro ready.")
        except Exception as e:
            logger.warning("Kokoro warmup issue (non‑fatal): %s", e)

    def _init(self) -> None:
        with self._lock:
            if self._pipeline is None:
                self._load_pipeline()

    def speak(self, text: str) -> None:
        """Synthesise and play (blocking)."""
        self._init()
        # Producer‑consumer threading for concurrent synth & playback
        audio_q: _queue.Queue[Optional[np.ndarray]] = _queue.Queue(maxsize=4)
        synth_error: list[Exception] = []

        def _synth():
            try:
                # Optionally split long text for lower latency
                if len(text) > 200:
                    import re
                    sentences = re.split(self._cfg.split_pattern, text)
                else:
                    sentences = [text]
                for sentence in sentences:
                    for _, _, audio in self._pipeline(
                        sentence,
                        voice=self._cfg.voice,
                        speed=self._cfg.speed,
                    ):
                        if audio is not None:
                            arr = _to_numpy(audio)
                            arr = _compress_silence(
                                arr,
                                self._cfg.sample_rate,
                                self._cfg.max_silence_ms,
                                self._cfg.silence_threshold,
                            )
                            if arr.size > 0:
                                audio_q.put(arr)
            except Exception as exc:
                synth_error.append(exc)
            finally:
                audio_q.put(None)

        synth_thread = threading.Thread(target=_synth, daemon=True)
        synth_thread.start()

        while True:
            arr = audio_q.get()
            if arr is None:
                break
            _play_np(arr, self._cfg.sample_rate)

        synth_thread.join()
        if synth_error:
            raise synth_error[0]

    async def speak_async(self, text: str) -> None:
        """Async synthesis and playback using asyncio queues."""
        self._init()
        loop = asyncio.get_running_loop()
        audio_q: asyncio.Queue[Optional[np.ndarray]] = asyncio.Queue(maxsize=4)

        def _synth():
            try:
                import re
                sentences = re.split(self._cfg.split_pattern, text) if len(text) > 200 else [text]
                for sentence in sentences:
                    for _, _, audio in self._pipeline(
                        sentence,
                        voice=self._cfg.voice,
                        speed=self._cfg.speed,
                    ):
                        if audio is not None:
                            arr = _to_numpy(audio)
                            arr = _compress_silence(
                                arr,
                                self._cfg.sample_rate,
                                self._cfg.max_silence_ms,
                                self._cfg.silence_threshold,
                            )
                            if arr.size > 0:
                                # Put into queue; run in executor to not block event loop
                                asyncio.run_coroutine_threadsafe(
                                    audio_q.put(arr), loop
                                )
                asyncio.run_coroutine_threadsafe(audio_q.put(None), loop)
            except Exception:
                logger.exception("Kokoro synth error")
                asyncio.run_coroutine_threadsafe(audio_q.put(None), loop)

        # Start synthesis in a thread
        await loop.run_in_executor(self._executor, _synth)

        # Playback directly on event loop (sounddevice blocks, so run in executor)
        while True:
            arr = await audio_q.get()
            if arr is None:
                break
            await loop.run_in_executor(
                self._executor, _play_np, arr, self._cfg.sample_rate
            )

    def voices(self) -> List[Dict[str, str]]:
        """Return available Kokoro voices (list of known voice IDs)."""
        return [
            {"id": "af_heart", "name": "Heart (US female, warm)"},
            {"id": "af_bella", "name": "Bella (US female, expressive)"},
            {"id": "am_adam", "name": "Adam (US male)"},
            {"id": "bf_emma", "name": "Emma (UK female)"},
            {"id": "bm_george", "name": "George (UK male)"},
            {"id": "jf_alpha", "name": "Alpha (JP female)"},
            {"id": "zf_xiaobei", "name": "Xiaobei (CN female)"},
        ]

    def __del__(self):
        self._executor.shutdown(wait=False)


# ---------------------------------------------------------------------------
# ElevenLabs – cloud API with retries & async
# ---------------------------------------------------------------------------
class ElevenLabsTTSEngine(BaseTTSEngine):
    """ElevenLabs cloud TTS – best quality, API key required."""

    def __init__(self, config: Optional[ElevenLabsConfig] = None, **kwargs):
        self._cfg = config or ElevenLabsConfig(**kwargs)
        self._executor = ThreadPoolExecutor(max_workers=1)

    def speak(self, text: str) -> None:
        audio_bytes = _run_sync(lambda: self._synth(text), self._executor)
        if audio_bytes:
            _play_audio_bytes(audio_bytes)

    async def speak_async(self, text: str) -> None:
        audio_bytes = await self._synth(text)
        if audio_bytes:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(self._executor, _play_audio_bytes, audio_bytes)

    async def _synth(self, text: str) -> bytes:
        import aiohttp

        url = f"https://api.elevenlabs.io/v1/text-to-speech/{self._cfg.voice_id}"
        headers = {
            "xi-api-key": self._cfg.api_key,
            "Content-Type": "application/json",
        }
        payload = {
            "text": text,
            "model_id": self._cfg.model_id,
            "voice_settings": {
                "stability": self._cfg.stability,
                "similarity_boost": self._cfg.similarity_boost,
            },
        }

        last_err = None
        for attempt in range(self._cfg.max_retries + 1):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        url,
                        json=payload,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=self._cfg.timeout),
                    ) as resp:
                        resp.raise_for_status()
                        return await resp.read()
            except Exception as e:
                last_err = e
                logger.warning("ElevenLabs attempt %d failed: %s", attempt + 1, e)
                if attempt < self._cfg.max_retries:
                    await asyncio.sleep(2 ** attempt)
        raise RuntimeError(
            f"ElevenLabs synthesis failed after {self._cfg.max_retries + 1} attempts. "
            f"Check API key and connectivity. Last error: {last_err}"
        )

    def voices(self) -> List[Dict[str, str]]:
        return [
            {"id": "pNInz6obpgDQGcFmaJgB", "name": "Adam (US male)"},
            {"id": "21m00Tcm4TlvDq8ikWAM", "name": "Rachel (US female)"},
            {"id": "AZnzlk1XvdvUeBnXmlld", "name": "Domi (US female)"},
        ]

    def __del__(self):
        self._executor.shutdown(wait=False)


# ---------------------------------------------------------------------------
# Thread‑safe player wrapper (async compatible)
# ---------------------------------------------------------------------------
class TTSPlayer:
    """
    Wraps any BaseTTSEngine. Exposes blocking and async speak methods,
    plus callbacks for start/done.
    """

    def __init__(self, engine: BaseTTSEngine):
        self._engine = engine
        self._playing = False
        self._lock = threading.Lock()

    @property
    def is_playing(self) -> bool:
        return self._playing

    def speak(
        self,
        text: str,
        on_start: Optional[Callable[[], None]] = None,
        on_done: Optional[Callable[[], None]] = None,
    ) -> None:
        """Blocking synthesis + playback."""
        try:
            with self._lock:
                self._playing = True
            if on_start:
                on_start()
            self._engine.speak(text)
        except Exception:
            logger.exception("TTS playback error")
        finally:
            with self._lock:
                self._playing = False
            if on_done:
                on_done()

    async def speak_async(
        self,
        text: str,
        on_start: Optional[Callable[[], None]] = None,
        on_done: Optional[Callable[[], None]] = None,
    ) -> None:
        """Non‑blocking synthesis + playback."""
        try:
            with self._lock:
                self._playing = True
            if on_start:
                on_start()
            await self._engine.speak_async(text)
        except Exception:
            logger.exception("TTS async error")
        finally:
            with self._lock:
                self._playing = False
            if on_done:
                on_done()

    def stop(self) -> None:
        self._engine.stop()
        with self._lock:
            self._playing = False


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def create_tts_player(config: dict) -> TTSPlayer:
    """Instantiate the appropriate engine from a configuration dictionary."""
    engine_name = config.get("tts_engine", "edgetts").lower()


    if engine_name == "kokoro":
        engine = KokoroTTSEngine(
            KokoroConfig(
                voice=config.get("tts_voice", "af_heart"),
                speed=float(config.get("tts_speed", 1.0)),
            )
        )
    elif engine_name == "elevenlabs":
        engine = ElevenLabsTTSEngine(
            ElevenLabsConfig(
                api_key=config.get("elevenlabs_api_key", ""),
                voice_id=config.get("tts_voice", "pNInz6obpgDQGcFmaJgB"),
            )
        )
    else:  # edgetts
        engine = EdgeTTSEngine(
            EdgeTTSConfig(
                voice=config.get("tts_voice", "en-US-GuyNeural"),
            )
        )
    return TTSPlayer(engine)


def apply_prosody_to_tts_config(config: dict, profile_mode: str = "standard") -> dict:
    """Adapte les paramètres de vitesse/pitch/volume du dictionnaire de configuration TTS."""
    from core.prosody_analyzer import TTS_MODULATIONS
    from core.prosody import PROSODY_PROFILES
    updated = dict(config)
    if profile_mode in TTS_MODULATIONS:
        mod = TTS_MODULATIONS[profile_mode]
        updated["tts_rate"] = mod.tts_rate
        updated["tts_pitch"] = mod.tts_pitch
        updated["tts_volume"] = mod.tts_volume
        updated["tts_speed"] = float(config.get("tts_speed", 1.0)) * mod.speed_factor
    else:
        profile = PROSODY_PROFILES.get(profile_mode, PROSODY_PROFILES["standard"])
        updated["tts_rate"] = profile.tts_rate
        updated["tts_pitch"] = profile.tts_pitch
        updated["tts_volume"] = profile.tts_volume
        updated["tts_speed"] = float(config.get("tts_speed", 1.0)) * profile.speed_factor
    return updated
