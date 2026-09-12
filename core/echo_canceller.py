"""Annulation d'écho acoustique (AEC) en temps réel via libspeexdsp.

Ce module remplace le half-duplex strict de ``AudioEngine`` par une
soustraction continue de l'écho du haut-parleur dans le signal micro,
permettant un **full-duplex** véritable : le micro reste ouvert en
permanence, la voix de l'assistant est effacée, et seule la voix de
l'utilisateur parvient au modèle.

Architecture
------------
Le moteur reçoit deux flux synchronisés :

* **Render** — l'audio 24 kHz joué dans les haut-parleurs, rééchantillonné
  à 16 kHz avant de servir de référence à l'AEC.
* **Capture** — l'entrée micro 16 kHz brute (voix utilisateur + écho).

Il produit un flux **Cleaned** : le micro débarrassé de l'écho, prêt pour
Gemini Live et le VAD Silero.

Implémentation
--------------
Liaison directe à ``libspeexdsp.so`` via :mod:`ctypes` — aucun wheel
Python tierce n'est requis, le `.so` est fourni par le paquet système
``speexdsp``. Le filtre Speex (NLMS + double-talk detector intégré)
traite des trames de 10 ms à 16 kHz (160 échantillons int16), avec un
tail de 200 ms (3200 échantillons) couvrant les réflexions d'un bureau
ou salon standard.

Latence
-------
Le traitement d'une trame de 10 ms prend < 0,3 ms sur un cœur moderne.
Le pipeline complet (resample + AEC + VAD) vise < 8 ms de latence ajoutée.

Résiliency
----------
* Si ``libspeexdsp.so`` est absente, le module passe en mode **passthrough**
  (le micro brut est transmis tel quel — retour au half-duplex).
* Si la sortie audio passe par un casque (Bluetooth headset ou USB headset),
  le bypass est activé : pas d'écho acoustique à supprimer.
* La compensation de jitter est assurée par un ring-buffer de référence
  dont la profondeur couvre le jitter ALSA/PipeWire mesuré.
"""
from __future__ import annotations

import collections
import ctypes
import ctypes.util
import logging
import struct
import threading
import time
from typing import Optional

import numpy as np

from core.double_talk_detector import DoubleTalkDetector, FRAME_SAMPLES as DTD_FRAME_SAMPLES

logger = logging.getLogger("anogpt.aec")

# ═══════════════════════════════════════════════════════════════════════════════
# Constantes DSP
# ═══════════════════════════════════════════════════════════════════════════════

AEC_SAMPLE_RATE = 16_000       # Tout l'AEC travaille à 16 kHz mono int16
RENDER_SAMPLE_RATE = 24_000    # Sortie native vers les haut-parleurs
AEC_FRAME_MS = 10              # Taille de trame SpeexDSP — 10 ms
AEC_FRAME_SAMPLES = AEC_SAMPLE_RATE * AEC_FRAME_MS // 1000  # 160
AEC_TAIL_MS = 200              # Queue du filtre adaptatif (200 ms)
AEC_TAIL_SAMPLES = AEC_SAMPLE_RATE * AEC_TAIL_MS // 1000    # 3200

# Ring-buffer de référence : couvre 500 ms de jitter max (8 trames = 80 ms
# minimum utile ; 50 trames = 500 ms pour les pires cas PipeWire).
_RENDER_RING_FRAMES = 50       # 50 × 160 = 8000 échantillons = 500 ms

# Speex echo_ctl constants
SPEEX_ECHO_SET_SAMPLING_RATE = 24
SPEEX_ECHO_GET_SAMPLING_RATE = 25

# Speex preprocessor ctl constants
SPEEX_PREPROCESS_SET_DENOISE = 0
SPEEX_PREPROCESS_SET_AGC = 2
SPEEX_PREPROCESS_SET_NOISE_SUPPRESS = 8
SPEEX_PREPROCESS_SET_ECHO_STATE = 24
SPEEX_PREPROCESS_SET_ECHO_SUPPRESS = 20
SPEEX_PREPROCESS_SET_ECHO_SUPPRESS_ACTIVE = 22

# ═══════════════════════════════════════════════════════════════════════════════
# Bindings ctypes — libspeexdsp
# ═══════════════════════════════════════════════════════════════════════════════

_lib: Optional[ctypes.CDLL] = None
_load_lock = threading.Lock()


def _load_speexdsp() -> Optional[ctypes.CDLL]:
    """Charge libspeexdsp.so une seule fois, thread-safe."""
    global _lib
    if _lib is not None:
        return _lib
    with _load_lock:
        if _lib is not None:
            return _lib
        path = ctypes.util.find_library("speexdsp")
        if not path:
            # Essai direct — Arch / Fedora n'enregistrent pas toujours le .so
            for candidate in ("libspeexdsp.so", "libspeexdsp.so.1"):
                try:
                    _lib = ctypes.CDLL(candidate)
                    return _lib
                except OSError:
                    continue
            return None
        try:
            _lib = ctypes.CDLL(path)
            return _lib
        except OSError:
            return None


def _setup_signatures(lib: ctypes.CDLL) -> None:
    """Définit les prototypes C pour la vérification des appels."""
    # speex_echo_state_init(frame_size, filter_length) -> SpeexEchoState*
    lib.speex_echo_state_init.argtypes = [ctypes.c_int, ctypes.c_int]
    lib.speex_echo_state_init.restype = ctypes.c_void_p

    # speex_echo_state_destroy(st)
    lib.speex_echo_state_destroy.argtypes = [ctypes.c_void_p]
    lib.speex_echo_state_destroy.restype = None

    # speex_echo_cancellation(st, rec, play, out)
    lib.speex_echo_cancellation.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
    ]
    lib.speex_echo_cancellation.restype = None

    # speex_echo_playback(st, play) — enregistre la référence pour l'AEC
    lib.speex_echo_playback.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    lib.speex_echo_playback.restype = None

    # speex_echo_capture(st, rec, out) — version split
    lib.speex_echo_capture.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
    ]
    lib.speex_echo_capture.restype = None

    # speex_echo_ctl(st, request, ptr)
    lib.speex_echo_ctl.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p
    ]
    lib.speex_echo_ctl.restype = ctypes.c_int

    # speex_echo_state_reset(st)
    lib.speex_echo_state_reset.argtypes = [ctypes.c_void_p]
    lib.speex_echo_state_reset.restype = None

    # Preprocessor
    lib.speex_preprocess_state_init.argtypes = [ctypes.c_int, ctypes.c_int]
    lib.speex_preprocess_state_init.restype = ctypes.c_void_p

    lib.speex_preprocess_state_destroy.argtypes = [ctypes.c_void_p]
    lib.speex_preprocess_state_destroy.restype = None

    lib.speex_preprocess_run.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    lib.speex_preprocess_run.restype = ctypes.c_int

    lib.speex_preprocess_ctl.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p
    ]
    lib.speex_preprocess_ctl.restype = ctypes.c_int


# ═══════════════════════════════════════════════════════════════════════════════
# Rééchantillonnage linéaire 24 kHz → 16 kHz (sans scipy)
# ═══════════════════════════════════════════════════════════════════════════════

def resample_24k_to_16k(pcm_24k: np.ndarray) -> np.ndarray:
    """Convertit un buffer int16 de 24 kHz vers 16 kHz par interpolation linéaire.

    Ratio exact 2:3 — chaque groupe de 3 échantillons à 24 kHz produit
    2 échantillons à 16 kHz. L'interpolation linéaire suffit pour la
    référence AEC : le filtre adaptatif compense les imperfections.
    """
    if pcm_24k.size == 0:
        return np.empty(0, dtype=np.int16)
    n_out = int(pcm_24k.size * 16_000 / 24_000)
    if n_out == 0:
        return np.empty(0, dtype=np.int16)
    # Indices fractionnaires dans le buffer source
    indices = np.linspace(0, pcm_24k.size - 1, n_out, endpoint=True)
    idx_floor = np.floor(indices).astype(np.intp)
    idx_ceil = np.minimum(idx_floor + 1, pcm_24k.size - 1)
    frac = (indices - idx_floor).astype(np.float32)
    # Interpolation
    result = pcm_24k[idx_floor].astype(np.float32) * (1.0 - frac) + \
             pcm_24k[idx_ceil].astype(np.float32) * frac
    return np.clip(result, -32768, 32767).astype(np.int16)


# ═══════════════════════════════════════════════════════════════════════════════
# Détection casque / écouteurs (bypass AEC automatique)
# ═══════════════════════════════════════════════════════════════════════════════

def detect_headphones() -> bool:
    """Détecte si la sortie audio courante est un casque/écouteurs.

    Quand un casque est utilisé, il n'y a pas d'écho acoustique à supprimer :
    le son ne revient pas dans le micro. On peut alors bypasser l'AEC pour
    préserver la qualité maximale du micro.

    Vérifie :
    1. Le ``device.form_factor`` du sink par défaut PipeWire/Pulse
    2. Le bus (bluetooth → souvent casque si la source mic est aussi BT)
    3. Les profils de carte (headset, headphone)
    """
    try:
        import json
        import subprocess
        r = subprocess.run(
            ["pactl", "-f", "json", "list", "sinks"],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode != 0:
            return False
        sinks = json.loads(r.stdout)

        # Trouver le sink par défaut
        r2 = subprocess.run(
            ["pactl", "get-default-sink"],
            capture_output=True, text=True, timeout=2,
        )
        default_sink = r2.stdout.strip() if r2.returncode == 0 else ""

        for sink in sinks:
            name = sink.get("name", "")
            if default_sink and name != default_sink:
                continue
            props = sink.get("properties", {}) or {}
            form = (props.get("device.form_factor") or "").lower()
            desc = (sink.get("description") or "").lower()

            if form in ("headset", "headphone", "headphones"):
                return True
            if any(kw in desc for kw in ("headset", "headphone", "écouteur",
                                          "casque", "earphone", "earbud")):
                return True
            # Bluetooth sink qui a aussi un profil casque actif
            bus = (props.get("device.bus") or "").lower()
            if bus == "bluetooth":
                profile = (sink.get("active_port") or "").lower()
                if any(h in profile for h in ("headset", "headphone", "a2dp")):
                    return True
            # Si on a trouvé le sink par défaut, pas besoin de continuer
            if default_sink and name == default_sink:
                break
        return False
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# Classe principale : EchoCanceller
# ═══════════════════════════════════════════════════════════════════════════════

class EchoCanceller:
    """Moteur AEC temps réel basé sur libspeexdsp.

    Thread-safety
    -------------
    Le verrou ``_lock`` protège l'état interne. Les méthodes ``feed_render``
    et ``process_capture`` peuvent être appelées depuis des threads différents
    (callback PortAudio micro vs. boucle de lecture asyncio).

    Usage
    -----
    ::

        aec = EchoCanceller()
        if aec.available:
            # Chaque chunk de sortie haut-parleur (24 kHz int16) :
            aec.feed_render(speaker_pcm_24k)

            # Chaque chunk de capture micro (16 kHz int16) :
            cleaned = aec.process_capture(mic_pcm_16k)
            # → cleaned est le micro sans l'écho, prêt pour le VAD et Gemini
    """

    def __init__(
        self,
        frame_ms: int = AEC_FRAME_MS,
        tail_ms: int = AEC_TAIL_MS,
        sample_rate: int = AEC_SAMPLE_RATE,
        enable_preprocess: bool = True,
    ) -> None:
        self.available = False
        self.bypassed = False          # True si casque détecté → passthrough
        self._lock = threading.Lock()
        self._echo_state: Optional[ctypes.c_void_p] = None
        self._preprocess_state: Optional[ctypes.c_void_p] = None
        self._lib: Optional[ctypes.CDLL] = None

        self._sample_rate = sample_rate
        self._frame_samples = sample_rate * frame_ms // 1000
        self._tail_samples = sample_rate * tail_ms // 1000
        self._frame_ms = frame_ms

        # Ring-buffer de référence pour la synchronisation render/capture.
        # Chaque entrée est un np.ndarray int16 de _frame_samples échantillons.
        self._render_ring: collections.deque = collections.deque(
            maxlen=_RENDER_RING_FRAMES
        )
        # Résidu de render non encore découpé en trames
        self._render_residual = np.empty(0, dtype=np.int16)
        # Résidu de capture non encore découpé en trames
        self._capture_residual = np.empty(0, dtype=np.int16)

        # Statistiques temps réel
        self._frames_processed = 0
        self._max_process_us = 0
        self._total_process_us = 0
        self._render_underruns = 0

        # Silence de référence quand le render ring est vide
        self._silence_frame = np.zeros(self._frame_samples, dtype=np.int16)
        self._last_render_frame = self._silence_frame.copy()

        # DTD indépendant de Speex : gel du NLMS + barge-in, même si
        # libspeexdsp manque. Le chemin Speex le consulte à chaque trame.
        self.dtd = DoubleTalkDetector(frame_samples=min(DTD_FRAME_SAMPLES, self._frame_samples))
        self._dtd_frozen = False

        # Tentative d'initialisation
        lib = _load_speexdsp()
        if lib is None:
            logger.warning(
                "libspeexdsp introuvable — AEC désactivé, repli half-duplex."
            )
            return

        try:
            _setup_signatures(lib)
            self._lib = lib

            # Créer l'état d'annulation d'écho
            self._echo_state = lib.speex_echo_state_init(
                self._frame_samples, self._tail_samples
            )
            if not self._echo_state:
                logger.error("speex_echo_state_init a retourné NULL")
                return

            # Configurer le taux d'échantillonnage
            rate = ctypes.c_int(sample_rate)
            lib.speex_echo_ctl(
                self._echo_state,
                SPEEX_ECHO_SET_SAMPLING_RATE,
                ctypes.byref(rate),
            )

            # Préprocesseur optionnel (suppression de bruit résiduel + AGC)
            if enable_preprocess:
                self._preprocess_state = lib.speex_preprocess_state_init(
                    self._frame_samples, sample_rate
                )
                if self._preprocess_state:
                    # Activer le débruitage
                    val = ctypes.c_int(1)
                    lib.speex_preprocess_ctl(
                        self._preprocess_state,
                        SPEEX_PREPROCESS_SET_DENOISE,
                        ctypes.byref(val),
                    )
                    # Suppression du bruit : -40 dB
                    val = ctypes.c_int(-40)
                    lib.speex_preprocess_ctl(
                        self._preprocess_state,
                        SPEEX_PREPROCESS_SET_NOISE_SUPPRESS,
                        ctypes.byref(val),
                    )
                    # Lier au filtre d'écho
                    lib.speex_preprocess_ctl(
                        self._preprocess_state,
                        SPEEX_PREPROCESS_SET_ECHO_STATE,
                        self._echo_state,
                    )
                    # Suppression de l'écho résiduel : -50 dB
                    val = ctypes.c_int(-50)
                    lib.speex_preprocess_ctl(
                        self._preprocess_state,
                        SPEEX_PREPROCESS_SET_ECHO_SUPPRESS,
                        ctypes.byref(val),
                    )
                    # Suppression écho actif (double-talk) : -50 dB
                    val = ctypes.c_int(-50)
                    lib.speex_preprocess_ctl(
                        self._preprocess_state,
                        SPEEX_PREPROCESS_SET_ECHO_SUPPRESS_ACTIVE,
                        ctypes.byref(val),
                    )

            self.available = True
            logger.info(
                "AEC SpeexDSP initialisé : frame=%d ms, tail=%d ms, %d Hz",
                frame_ms, tail_ms, sample_rate,
            )
        except Exception as exc:
            logger.error("Échec initialisation AEC : %s", exc)
            self._cleanup()

    def _cleanup(self) -> None:
        """Libère les ressources natives."""
        if self._lib is None:
            return
        if self._preprocess_state:
            try:
                self._lib.speex_preprocess_state_destroy(self._preprocess_state)
            except Exception:
                pass
            self._preprocess_state = None
        if self._echo_state:
            try:
                self._lib.speex_echo_state_destroy(self._echo_state)
            except Exception:
                pass
            self._echo_state = None

    def __del__(self) -> None:
        self._cleanup()

    def destroy(self) -> None:
        """Destruction explicite — à appeler en fin de session."""
        with self._lock:
            self._cleanup()
            self.available = False

    # ── Bypass automatique casque ──────────────────────────────────────────

    def check_headphone_bypass(self) -> bool:
        """Vérifie si un casque est branché et active/désactive le bypass.

        Retourne True si le bypass est actif (casque détecté).
        """
        is_headphone = detect_headphones()
        if is_headphone != self.bypassed:
            self.bypassed = is_headphone
            if is_headphone:
                logger.info("Casque détecté — AEC bypassed (pas d'écho acoustique)")
            else:
                logger.info("Haut-parleurs détectés — AEC actif")
                self.reset()
        return self.bypassed

    # ── Alimentation du render (référence haut-parleur) ───────────────────

    def feed_render(self, pcm_24k: np.ndarray | bytes) -> None:
        """Enregistre le signal de sortie comme référence pour l'AEC.

        Accepte du PCM int16 à 24 kHz, le rééchantillonne à 16 kHz, puis le
        découpe en trames de ``_frame_samples`` et les empile dans le ring-buffer.

        Cette méthode est appelée depuis la boucle de lecture ``_play_audio``
        (thread asyncio/executor), juste avant ou après ``stream.write()``.
        """
        if not self.available or self.bypassed:
            return

        if isinstance(pcm_24k, (bytes, bytearray)):
            pcm_24k = np.frombuffer(pcm_24k, dtype=np.int16)

        # Rééchantillonner 24 kHz → 16 kHz
        pcm_16k = resample_24k_to_16k(pcm_24k)
        if pcm_16k.size == 0:
            return

        with self._lock:
            # Concaténer avec le résidu précédent
            if self._render_residual.size > 0:
                pcm_16k = np.concatenate([self._render_residual, pcm_16k])

            # Découper en trames et enregistrer dans l'état AEC
            n_frames = pcm_16k.size // self._frame_samples
            for i in range(n_frames):
                frame = pcm_16k[
                    i * self._frame_samples : (i + 1) * self._frame_samples
                ]
                # Copie contiguë pour ctypes
                frame_c = np.ascontiguousarray(frame, dtype=np.int16)
                self._render_ring.append(frame_c)
                self._last_render_frame = frame_c

            # Conserver le résidu
            consumed = n_frames * self._frame_samples
            self._render_residual = pcm_16k[consumed:].copy()

    # ── Traitement de la capture (micro) ──────────────────────────────────

    def process_capture(self, mic_pcm_16k: np.ndarray | bytes) -> np.ndarray:
        """Soustrait l'écho du signal micro et retourne l'audio nettoyé.

        Entrée  : PCM int16 mono 16 kHz (le micro brut)
        Sortie  : PCM int16 mono 16 kHz (la voix utilisateur sans écho)

        Si l'AEC n'est pas disponible ou est bypassed, retourne le signal
        d'entrée tel quel (passthrough).
        """
        if isinstance(mic_pcm_16k, (bytes, bytearray)):
            mic_pcm_16k = np.frombuffer(mic_pcm_16k, dtype=np.int16)

        if not self.available or self.bypassed:
            return mic_pcm_16k.copy()

        with self._lock:
            # Concaténer avec le résidu de capture
            if self._capture_residual.size > 0:
                mic_pcm_16k = np.concatenate(
                    [self._capture_residual, mic_pcm_16k]
                )

            n_frames = mic_pcm_16k.size // self._frame_samples
            if n_frames == 0:
                self._capture_residual = mic_pcm_16k.copy()
                return np.empty(0, dtype=np.int16)

            output_frames = []
            for i in range(n_frames):
                frame = mic_pcm_16k[
                    i * self._frame_samples : (i + 1) * self._frame_samples
                ]
                frame_c = np.ascontiguousarray(frame, dtype=np.int16)
                out_frame = np.empty(self._frame_samples, dtype=np.int16)

                t0 = time.monotonic()

                # L'API Speex « split » (playback/capture) exige exactement
                # un appel playback pour chaque appel capture. Dans ANO-GPT,
                # le micro tourne en continu alors que le haut-parleur est
                # intermittent : ses files internes débordaient/se vidaient
                # donc sans arrêt et imprimaient des milliers d'avertissements
                # xrun. La variante synchrone reçoit toujours une référence
                # explicite et convient à notre ring-buffer applicatif.
                if self._render_ring:
                    far = self._render_ring.popleft()
                else:
                    far = self._silence_frame
                    self._render_underruns += 1
                frozen, dtd_residual, dtd_echo = self._run_dtd(frame_c, far)
                self._dtd_frozen = frozen

                if frozen:
                    # Speex n'a pas d'API de gel : on lui injecte l'estimée
                    # d'écho figée pour que l'erreur reste petite (peu
                    # d'adaptation), avec la référence courante explicite.
                    dummy = np.ascontiguousarray(dtd_echo, dtype=np.int16)
                    discarded = np.empty(self._frame_samples, dtype=np.int16)
                    self._lib.speex_echo_cancellation(
                        self._echo_state,
                        dummy.ctypes.data,
                        far.ctypes.data,
                        discarded.ctypes.data,
                    )
                    # Sortie = résidu NLMS × gain de confort. Le préprocesseur
                    # Speex (ECHO_SUPPRESS_ACTIVE −50 dB) dévorerait la voix.
                    out_frame = np.ascontiguousarray(dtd_residual, dtype=np.int16)
                else:
                    # AEC synchrone avec la trame haut-parleur correspondante.
                    self._lib.speex_echo_cancellation(
                        self._echo_state,
                        frame_c.ctypes.data,
                        far.ctypes.data,
                        out_frame.ctypes.data,
                    )

                    # Post-traitement : suppression de bruit résiduel
                    if self._preprocess_state:
                        self._lib.speex_preprocess_run(
                            self._preprocess_state,
                            out_frame.ctypes.data,
                        )

                elapsed_us = int((time.monotonic() - t0) * 1_000_000)
                self._frames_processed += 1
                self._total_process_us += elapsed_us
                self._max_process_us = max(self._max_process_us, elapsed_us)

                output_frames.append(out_frame)

            # Conserver le résidu de capture
            consumed = n_frames * self._frame_samples
            self._capture_residual = mic_pcm_16k[consumed:].copy()

            if output_frames:
                return np.concatenate(output_frames)
            return np.empty(0, dtype=np.int16)

    # ── Commandes ─────────────────────────────────────────────────────────

    def _run_dtd(
        self, mic_frame: np.ndarray, far_frame: np.ndarray
    ) -> tuple[bool, np.ndarray, np.ndarray]:
        """Analyse la trame Speex en sous-trames DTD de 5 ms.

        Retourne ``(frozen, residual_int16, echo_int16)`` sur la trame entière.
        """
        n = mic_frame.size
        step = max(1, min(self.dtd.frame_samples, n))
        residuals = []
        echoes = []
        frozen = False
        for start in range(0, n, step):
            end = min(start + step, n)
            result = self.dtd.process_int16(mic_frame[start:end], far_frame[start:end])
            frozen = frozen or result.frozen
            residuals.append(result.residual_int16)
            echoes.append(result.echo_estimate_int16)
        residual = np.concatenate(residuals) if residuals else np.empty(0, dtype=np.int16)
        echo = np.concatenate(echoes) if echoes else np.empty(0, dtype=np.int16)
        if residual.size < n:
            residual = np.pad(residual, (0, n - residual.size))
        elif residual.size > n:
            residual = residual[:n]
        if echo.size < n:
            echo = np.pad(echo, (0, n - echo.size))
        elif echo.size > n:
            echo = echo[:n]
        return frozen, residual, echo

    def reset(self) -> None:
        """Réinitialise le filtre adaptatif (changement de micro, reconnexion)."""
        with self._lock:
            if self._echo_state and self._lib:
                self._lib.speex_echo_state_reset(self._echo_state)
            self._render_ring.clear()
            self._render_residual = np.empty(0, dtype=np.int16)
            self._capture_residual = np.empty(0, dtype=np.int16)
            self._last_render_frame = self._silence_frame.copy()
            self._dtd_frozen = False
            self.dtd.reset()
            self._frames_processed = 0
            self._max_process_us = 0
            self._total_process_us = 0
            self._render_underruns = 0

    @property
    def stats(self) -> dict:
        """Statistiques de performance pour le monitoring."""
        with self._lock:
            avg_us = (
                self._total_process_us / max(1, self._frames_processed)
            )
            dtd_stats = self.dtd.stats
            return {
                "available": self.available,
                "bypassed": self.bypassed,
                "frames_processed": self._frames_processed,
                "avg_latency_us": round(avg_us, 1),
                "max_latency_us": self._max_process_us,
                "render_underruns": self._render_underruns,
                "render_ring_depth": len(self._render_ring),
                "dtd_state": dtd_stats["state"],
                "dtd_frozen": self._dtd_frozen,
                "dtd_ncc": dtd_stats["ncc"],
                "dtd_geigel": dtd_stats["geigel"],
            }


# ═══════════════════════════════════════════════════════════════════════════════
# FullDuplexFilter — le remplacement de HalfDuplexGate
# ═══════════════════════════════════════════════════════════════════════════════

class FullDuplexFilter:
    """Pipeline complet : AEC + Silero VAD pour barge-in en full-duplex.

    Remplace le blocage ``if jarvis_speaking: return`` du callback micro par
    un traitement continu :

    1. Le micro passe par l'AEC (suppression de l'écho du haut-parleur)
    2. Le Silero VAD analyse l'audio nettoyé
    3. Si une voix utilisateur est détectée pendant que l'assistant parle →
       barge-in instantané sans faux positif

    Le filtre maintient un compteur de trames vocales confirmées pour éviter
    les déclenchements parasites sur un bruit transitoire.
    """

    # Seuils de décision barge-in sur l'audio AEC (repli si DTD absent)
    _BARGE_VAD_THRESHOLD = 0.55   # Silero probability minimale
    _BARGE_CONFIRM_FRAMES = 2     # 2 trames neurales consécutives (DTD)
    _BARGE_ENERGY_FLOOR = 0.04    # RMS minimale pour considérer un signal

    def __init__(self, echo_canceller: Optional[EchoCanceller] = None) -> None:
        self.aec = echo_canceller or EchoCanceller()
        self._vad = None
        self._vad_loaded = False
        self._barge_voice_count = 0

        # Charger Silero VAD en différé
        try:
            from core.vad_silero import get_vad
            self._vad = get_vad()
            self._vad_loaded = self._vad.available
        except Exception as exc:
            logger.warning("Silero VAD indisponible pour le full-duplex : %s", exc)

    @property
    def aec_available(self) -> bool:
        return self.aec.available and not self.aec.bypassed

    def feed_speaker(self, pcm_24k: np.ndarray | bytes) -> None:
        """Alimente le render AEC avec le flux haut-parleur."""
        self.aec.feed_render(pcm_24k)

    def process_mic(
        self,
        mic_pcm_int16: np.ndarray,
        jarvis_speaking: bool = False,
    ) -> tuple[np.ndarray, bool, float]:
        """Traite un chunk micro et décide du barge-in.

        Paramètres
        ----------
        mic_pcm_int16 : np.ndarray
            Audio micro brut, int16 mono 16 kHz.
        jarvis_speaking : bool
            True si l'assistant est en train de parler.

        Retourne
        --------
        (cleaned_float32, should_barge_in, vad_probability)
            * cleaned_float32 : audio nettoyé en float32 [-1, 1]
            * should_barge_in : True si une voix utilisateur est confirmée
              pendant que l'assistant parle
            * vad_probability : probabilité de parole Silero sur l'audio nettoyé
        """
        # AEC : supprimer l'écho du haut-parleur
        if self.aec.available and not self.aec.bypassed:
            cleaned_int16 = self.aec.process_capture(mic_pcm_int16)
        else:
            cleaned_int16 = mic_pcm_int16
            # Casque / passthrough : pas d'écho acoustique, le DTD voit
            # uniquement la parole proche (far-end = silence).
            dtd = getattr(self.aec, "dtd", None)
            if dtd is not None and cleaned_int16.size:
                silence = np.zeros(cleaned_int16.size, dtype=np.int16)
                step = max(1, dtd.frame_samples)
                for start in range(0, cleaned_int16.size, step):
                    end = start + step
                    dtd.process_int16(cleaned_int16[start:end], silence[start:end])

        if cleaned_int16.size == 0:
            return (
                np.empty(0, dtype=np.float32),
                False,
                0.0,
            )

        # Conversion float32 pour le VAD et la sortie
        cleaned_float = cleaned_int16.astype(np.float32) / 32768.0

        # VAD Silero sur le signal nettoyé
        vad_prob = 0.0
        if self._vad_loaded and self._vad is not None:
            try:
                vad_prob = self._vad.probability(cleaned_float)
            except Exception:
                vad_prob = 0.0

        # Barge-in : DTD (double parole / near-end) + 2 trames VAD neurales.
        # Un écho résiduel, même énergétique, ne suffit plus : le DTD doit
        # d'abord avoir tranché « l'utilisateur parle », puis Silero confirme.
        should_barge = False
        dtd = getattr(self.aec, "dtd", None)
        if dtd is not None:
            should_barge = dtd.update_barge_in(vad_prob, jarvis_speaking)
            self._barge_voice_count = dtd.stats["vad_streak"]
        elif jarvis_speaking:
            rms = float(np.sqrt(np.mean(cleaned_float ** 2))) if cleaned_float.size else 0.0
            if vad_prob >= self._BARGE_VAD_THRESHOLD and rms >= self._BARGE_ENERGY_FLOOR:
                self._barge_voice_count += 1
                if self._barge_voice_count >= self._BARGE_CONFIRM_FRAMES:
                    should_barge = True
                    self._barge_voice_count = 0
            else:
                self._barge_voice_count = max(0, self._barge_voice_count - 1)
        else:
            self._barge_voice_count = 0

        return cleaned_float, should_barge, vad_prob

    def reset(self) -> None:
        """Réinitialise tout l'état (changement de session, reconnexion)."""
        self.aec.reset()
        self._barge_voice_count = 0
        if self._vad is not None:
            try:
                self._vad.reset()
            except Exception:
                pass

    def check_headphones(self) -> bool:
        """Vérifie et met à jour le bypass casque."""
        return self.aec.check_headphone_bypass()


# ═══════════════════════════════════════════════════════════════════════════════
# Singleton / factory
# ═══════════════════════════════════════════════════════════════════════════════

_instance: Optional[FullDuplexFilter] = None
_instance_lock = threading.Lock()


def get_full_duplex_filter() -> FullDuplexFilter:
    """Instance partagée — un seul état AEC par processus."""
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = FullDuplexFilter()
        return _instance


def destroy_full_duplex_filter() -> None:
    """Libère les ressources natives en fin de vie du processus."""
    global _instance
    with _instance_lock:
        if _instance is not None:
            _instance.aec.destroy()
            _instance = None
