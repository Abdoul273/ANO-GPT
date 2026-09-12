"""Capture micro native 16 kHz mono PCM 16-bit pour le pipeline STT.

``AudioCaptureStream`` est le point d'entrée audio de la chaîne studio-grade :

    AudioCaptureStream → AudioDenoiser → VoiceActivityDetector → Gemini

Backends, dans l'ordre pour ``backend="auto"`` :

1. ``python-pipewire`` s'il est importable (rare sous Arch).
2. ``sounddevice`` ouvert explicitement en 16 kHz / mono / int16, en
   ciblant le pont PortAudio ``pulse`` puis ``pipewire`` — le format est
   imposé à l'ouverture, PipeWire n'a plus à deviner.
3. ``pw-record`` (PipeWire CLI, format forcé, ``--raw``).

Rien n'est rééchantillonné dans ce module : on demande le format natif
attendu par Gemini Transcribe et RNNoise (après upsampling dédié).
"""

from __future__ import annotations

import asyncio
import logging
import math
import queue
import shutil
import subprocess
import threading
from dataclasses import dataclass
from typing import AsyncIterator, Iterator, Optional, Union

import numpy as np

logger = logging.getLogger("anogpt.audio_capture")

DEFAULT_SAMPLE_RATE = 16000
DEFAULT_FRAME_MS = 20  # 20 ms → 320 échantillons à 16 kHz (aligné RNNoise 10 ms × 2)
DEFAULT_CHANNELS = 1


def pcm16_rms_dbfs(frame: bytes) -> float:
    """RMS d'une trame PCM 16-bit, en dBFS. Silence → -100 dBFS."""
    if not frame:
        return -100.0
    audio = np.frombuffer(frame, dtype=np.int16)
    if audio.size == 0:
        return -100.0
    rms = float(np.sqrt(np.mean(audio.astype(np.float32) ** 2))) + 1e-12
    return 20.0 * math.log10(max(1e-5, rms / 32768.0))


@dataclass
class AudioCaptureConfig:
    """Configuration du flux de capture audio."""

    sample_rate: int = DEFAULT_SAMPLE_RATE
    channels: int = DEFAULT_CHANNELS
    frame_ms: int = DEFAULT_FRAME_MS
    dtype: str = "int16"  # "int16" ou "float32"
    device: Optional[Union[int, str]] = None  # None = auto (pulse/pipewire)
    backend: str = "auto"  # "auto", "pipewire", "sounddevice", "pw-record"
    queue_maxsize: int = 100
    latency: str = "high"  # "high" protège le callback sur une machine 2 cœurs

    @property
    def frame_samples(self) -> int:
        return int(self.sample_rate * self.frame_ms / 1000)

    @property
    def frame_bytes(self) -> int:
        bytes_per_sample = 2 if self.dtype == "int16" else 4
        return self.frame_samples * self.channels * bytes_per_sample


class AudioCaptureStream:
    """Flux de capture micro en trames homogènes (sync / async).

    Le callback PortAudio ne fait que copier des bytes dans une file : aucun
    log, aucun resampling, aucun VAD. Le GIL reste disponible pour Qt et
    l'étage de débruitage qui tourne hors du callback.
    """

    def __init__(self, config: Optional[AudioCaptureConfig] = None):
        self.config = config or AudioCaptureConfig()
        self._is_running = False
        self._queue: queue.Queue[bytes] = queue.Queue(maxsize=self.config.queue_maxsize)
        self._async_queue: Optional[asyncio.Queue[bytes]] = None
        self._async_loop: Optional[asyncio.AbstractEventLoop] = None
        self._stream = None
        self._process: Optional[subprocess.Popen] = None
        self._worker_thread: Optional[threading.Thread] = None
        self._selected_backend = "none"
        self._frames_captured = 0
        self._overflow_count = 0
        self._lock = threading.Lock()
        self._log_every = 50  # ~1 s à 20 ms — jamais dans le callback

    @property
    def is_active(self) -> bool:
        return self._is_running

    @property
    def sample_rate(self) -> int:
        return self.config.sample_rate

    @property
    def frame_samples(self) -> int:
        return self.config.frame_samples

    @property
    def frame_bytes(self) -> int:
        return self.config.frame_bytes

    @property
    def backend(self) -> str:
        return self._selected_backend

    @staticmethod
    def find_pipewire_device_index() -> Optional[int]:
        """Index PortAudio du pont PipeWire / Pulse, pas d'un micro ALSA brut.

        On cherche d'abord ``pulse`` (suit la source PipeWire par défaut),
        puis ``pipewire``. Un micro ALSA direct forcerait un second
        rééchantillonnage et ignorerait le routage WirePlumber.
        """
        try:
            import sounddevice as sd

            devices = sd.query_devices()
            names: list[tuple[int, str]] = []
            for idx, dev in enumerate(devices):
                if int(dev.get("max_input_channels") or 0) <= 0:
                    continue
                name = str(dev.get("name") or "").strip().casefold()
                names.append((idx, name))
            for candidate in ("pulse", "pipewire"):
                for idx, name in names:
                    if candidate == name or candidate in name:
                        logger.debug(
                            "Périphérique PortAudio PipeWire détecté : [%d] %s",
                            idx, name,
                        )
                        return idx
        except Exception as exc:
            logger.debug("Erreur lors de la recherche du device PipeWire : %s", exc)
        return None

    def _resolve_sounddevice_device(self, sd) -> Optional[Union[int, str]]:
        if self.config.device is not None:
            return self.config.device
        try:
            from core.audio_router import portaudio_input_device

            chosen = portaudio_input_device(sd)
            if chosen is not None:
                return chosen
        except Exception as exc:
            logger.debug("audio_router indisponible pour le device : %s", exc)
        return self.find_pipewire_device_index()

    def start(self) -> AudioCaptureStream:
        """Démarre le flux de capture."""
        with self._lock:
            if self._is_running:
                return self
            self._is_running = True
            self._frames_captured = 0
            self._overflow_count = 0

            backend = self.config.backend.lower().strip()
            errors: list[str] = []

            if backend in ("auto", "pipewire"):
                if self._start_python_pipewire():
                    return self
                errors.append("python-pipewire")

            if backend in ("auto", "sounddevice"):
                if self._start_sounddevice():
                    return self
                errors.append("sounddevice")
                if backend == "sounddevice":
                    self._is_running = False
                    raise RuntimeError("Échec de l'initialisation du backend sounddevice")

            if backend in ("auto", "pipewire", "pw-record"):
                if self._start_pw_record():
                    return self
                errors.append("pw-record")
                if backend in ("pw-record", "pipewire"):
                    self._is_running = False
                    raise RuntimeError("Échec de l'initialisation du backend pw-record / PipeWire")

            self._is_running = False
            raise RuntimeError(
                "Aucun backend audio n'a pu démarrer (essayé : "
                + ", ".join(errors) + ")"
            )

    def _start_python_pipewire(self) -> bool:
        """Tente le binding Python PipeWire s'il est installé.

        Le paquet ``python-pipewire`` n'offre pas encore d'API de capture
        stable comparable à ``pw-record`` ; on le détecte pour le journal
        et on laisse le relais aux backends suivants.
        """
        try:
            import pipewire  # noqa: F401
        except ImportError:
            return False
        except Exception as exc:
            logger.debug("python-pipewire importable mais inutilisable : %s", exc)
            return False
        logger.info(
            "python-pipewire est installé mais n'expose pas de stream capture "
            "stable ; repli sounddevice / pw-record."
        )
        return False

    def _enqueue(self, data_bytes: bytes) -> None:
        """Dépose une trame, en évinçant la plus ancienne si la file est pleine."""
        try:
            self._queue.put_nowait(data_bytes)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            self._overflow_count += 1
            try:
                self._queue.put_nowait(data_bytes)
            except queue.Full:
                pass

        self._frames_captured += 1
        if self._async_queue is not None and self._async_loop is not None:
            try:
                self._async_loop.call_soon_threadsafe(
                    self._async_queue.put_nowait, data_bytes
                )
            except Exception:
                pass

    def _start_sounddevice(self) -> bool:
        """Ouvre un flux PortAudio en 16 kHz mono PCM, ciblant PipeWire."""
        try:
            import sounddevice as sd

            device = self._resolve_sounddevice_device(sd)
            np_dtype = np.int16 if self.config.dtype == "int16" else np.float32

            def _audio_callback(indata, frames, time_info, status):
                if status and status.input_overflow:
                    self._overflow_count += 1
                if not self._is_running:
                    return
                self._enqueue(indata.tobytes())

            kwargs = {
                "samplerate": self.config.sample_rate,
                "channels": self.config.channels,
                "dtype": np_dtype,
                "blocksize": self.config.frame_samples,
                "device": device,
                "callback": _audio_callback,
            }
            # latency n'est pas forcément honoré par tous les hostapis.
            try:
                self._stream = sd.InputStream(latency=self.config.latency, **kwargs)
            except TypeError:
                self._stream = sd.InputStream(**kwargs)
            self._stream.start()
            self._selected_backend = f"sounddevice (dev: {device})"
            logger.info(
                "Capture audio démarrée via sounddevice "
                "(dev=%s, %d Hz, %d ch, %s, %d ms/frame)",
                device,
                self.config.sample_rate,
                self.config.channels,
                self.config.dtype,
                self.config.frame_ms,
            )
            return True
        except Exception as exc:
            logger.warning("sounddevice indisponible ou en échec : %s", exc)
            return False

    def _start_pw_record(self) -> bool:
        """Capture brute PipeWire, format imposé à la source (pas de Pulse)."""
        if shutil.which("pw-record") is None:
            logger.debug("pw-record absent du PATH")
            return False
        try:
            fmt = "s16" if self.config.dtype == "int16" else "f32"
            cmd = [
                "pw-record",
                "--raw",
                f"--rate={self.config.sample_rate}",
                f"--channels={self.config.channels}",
                f"--format={fmt}",
                f"--latency={self.config.frame_ms}ms",
                "--quality=10",
                "-",
            ]
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=self.config.frame_bytes * 4,
            )

            def _pw_reader():
                frame_bytes = self.config.frame_bytes
                stdout = self._process.stdout if self._process is not None else None
                if stdout is None:
                    return
                while self._is_running and self._process and self._process.poll() is None:
                    try:
                        raw = stdout.read(frame_bytes)
                        if not raw or len(raw) < frame_bytes:
                            break
                        self._enqueue(raw)
                    except Exception as err:
                        logger.debug("Erreur lecture pw-record: %s", err)
                        break

            self._worker_thread = threading.Thread(
                target=_pw_reader, name="pw-record-reader", daemon=True,
            )
            self._worker_thread.start()
            # Échec immédiat (binaire présent mais session PipeWire morte).
            if self._process.poll() is not None:
                self._process = None
                return False
            self._selected_backend = "pw-record (pipewire direct)"
            logger.info(
                "Capture audio démarrée via pw-record direct (%d Hz, %s, %d ms)",
                self.config.sample_rate, fmt, self.config.frame_ms,
            )
            return True
        except Exception as exc:
            logger.warning("pw-record en échec : %s", exc)
            return False

    def stop(self) -> None:
        """Arrête le flux de capture et libère les ressources."""
        with self._lock:
            self._is_running = False

            if self._stream is not None:
                try:
                    self._stream.stop()
                    self._stream.close()
                except Exception as exc:
                    logger.debug("Erreur fermeture stream sounddevice: %s", exc)
                self._stream = None

            if self._process is not None:
                try:
                    self._process.terminate()
                    self._process.wait(timeout=0.5)
                except Exception:
                    try:
                        self._process.kill()
                    except Exception:
                        pass
                self._process = None

            if self._worker_thread is not None and self._worker_thread.is_alive():
                self._worker_thread.join(timeout=0.5)
                self._worker_thread = None

            logger.info(
                "Capture audio arrêtée (%d trames capturées, %d débordements)",
                self._frames_captured, self._overflow_count,
            )

    def read(self, timeout: float = 1.0) -> Optional[bytes]:
        """Lit une trame brute depuis la file (synchrone)."""
        if not self._is_running:
            return None
        try:
            frame = self._queue.get(timeout=timeout)
        except queue.Empty:
            return None
        if logger.isEnabledFor(logging.DEBUG) and (
            self._frames_captured % self._log_every == 1
        ):
            logger.debug(
                "[AudioCaptureStream] RMS = %5.1f dBFS (backend=%s, n=%d)",
                pcm16_rms_dbfs(frame), self._selected_backend, self._frames_captured,
            )
        return frame

    def read_numpy(self, timeout: float = 1.0) -> Optional[np.ndarray]:
        """Lit une trame et la renvoie sous forme de numpy ndarray."""
        raw = self.read(timeout=timeout)
        if raw is None:
            return None
        dtype = np.int16 if self.config.dtype == "int16" else np.float32
        return np.frombuffer(raw, dtype=dtype)

    def iter_frames(self, max_frames: Optional[int] = None) -> Iterator[bytes]:
        """Générateur synchrone de trames audio brutes."""
        count = 0
        while self._is_running:
            frame = self.read(timeout=0.5)
            if frame is not None:
                yield frame
                count += 1
                if max_frames is not None and count >= max_frames:
                    break
            elif not self._is_running:
                break

    def get_async_queue(self) -> asyncio.Queue[bytes]:
        """Retourne ou instancie la file asynchrone pour la boucle courante."""
        if self._async_queue is None:
            self._async_loop = asyncio.get_running_loop()
            self._async_queue = asyncio.Queue(maxsize=self.config.queue_maxsize)
        return self._async_queue

    async def async_frames(self, max_frames: Optional[int] = None) -> AsyncIterator[bytes]:
        """Générateur asynchrone de trames audio brutes pour asyncio."""
        q = self.get_async_queue()
        count = 0
        try:
            while self._is_running:
                try:
                    frame = await asyncio.wait_for(q.get(), timeout=0.5)
                    yield frame
                    count += 1
                    if max_frames is not None and count >= max_frames:
                        break
                except asyncio.TimeoutError:
                    if not self._is_running:
                        break
        finally:
            self._async_queue = None
            self._async_loop = None

    def __enter__(self) -> AudioCaptureStream:
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()

    async def __aenter__(self) -> AudioCaptureStream:
        self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()
