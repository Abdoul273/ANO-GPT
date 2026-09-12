"""
Speech-to-Text engines for MARK XL — Ultra‑robust & High‑performance edition.

Whisper  – offline transcription via faster-whisper
           • VAD‑buffered streaming for real‑time use
           • GPU/CPU auto‑detection with fallback
           • Async interface, graceful model sharing
Vosk     – offline streaming transcription (lightweight)
           • Async‑compatible chunk processing
           • Robust model loading, language auto‑mapping
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import AsyncIterator, Iterator, Optional, Tuple

import numpy as np

# Coefficient du bloqueur DC (passe-haut ~80 Hz à 16 kHz).
_DC_R = 0.968

# scipy vectorise ce filtre récursif ~60x, mais son import prend plusieurs
# secondes. On le charge lorsque le premier préprocesseur est construit : la
# fenêtre apparaît immédiatement et la boucle Python identique sert de repli
# pendant les toutes premières tranches audio.
_LFILTER = None
_LFILTER_LOAD_STARTED = False
_LFILTER_LOAD_LOCK = threading.Lock()


def _load_lfilter_in_background() -> None:
    global _LFILTER
    try:
        from scipy.signal import lfilter
        _LFILTER = lfilter
    except ImportError:
        pass


def _ensure_lfilter_loading() -> None:
    global _LFILTER_LOAD_STARTED
    if _LFILTER is not None or _LFILTER_LOAD_STARTED:
        return
    with _LFILTER_LOAD_LOCK:
        if _LFILTER is not None or _LFILTER_LOAD_STARTED:
            return
        _LFILTER_LOAD_STARTED = True
        threading.Thread(
            target=_load_lfilter_in_background,
            daemon=True,
            name="scipy-audio-filter-import",
        ).start()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger("stt.mark_xl")
logger.setLevel(logging.INFO)
if not logger.handlers:
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    formatter = logging.Formatter("[%(levelname)s] %(name)s - %(message)s")
    ch.setFormatter(formatter)
    logger.addHandler(ch)

# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------
def _ensure_float32_mono_16k(audio: np.ndarray, sample_rate: int = 16000) -> np.ndarray:
    """Convert any audio array to float32, mono, 16 kHz (if scipy available)."""
    if audio.ndim > 1:
        audio = audio.mean(axis=1)  # simple stereo→mono
    # Tester le type AVANT la conversion. L'ancienne version convertissait
    # d'abord en float32, puis demandait si le résultat était entier : du PCM
    # int16 restait donc dans [-32768, 32767] au lieu de [-1, 1].
    original_dtype = audio.dtype
    if np.issubdtype(original_dtype, np.integer):
        info = np.iinfo(original_dtype)
        scale = float(max(abs(info.min), info.max))
        audio = audio.astype(np.float32) / scale
    elif audio.dtype != np.float32:
        audio = audio.astype(np.float32)
    # Resample if needed
    if sample_rate != 16000:
        try:
            from scipy.signal import resample
            new_len = int(len(audio) * 16000 / sample_rate)
            audio = resample(audio, new_len).astype(np.float32)
        except ImportError:
            logger.warning("scipy not installed – cannot resample audio; proceeding with original sample rate.")
    return audio

# ---------------------------------------------------------------------------
# Audio preprocessor & Signal enhancement filter
# ---------------------------------------------------------------------------
class AudioPreprocessor:
    """
    Real-time voice pipeline hardening for the mic → Gemini Live path.

    - WebRTC VAD (frame-accurate voice activity detection) with an attack/
      hangover debounce, so single noise blips or short speaker-echo spikes
      never get latched in as "speech". Falls back to an adaptive RMS
      heuristic if the optional `webrtcvad` package isn't installed.
    - Adaptive noise-floor tracking + noise gate: audio during confirmed
      silence (fan noise, room hiss, speaker bleed) is attenuated instead of
      boosted — the previous fixed-gain AGC amplified ANY input (including
      pure noise) up to 30x, which is what caused garbled/hallucinated
      transcriptions on a laptop's built-in mic.
    - Bounded AGC (max 12x) applied only to confirmed speech, so quiet/tired
      voices are still boosted without also amplifying background noise.
    - `strict=True` (pass while the assistant itself is speaking) requires a
      longer sustained-voice window before latching "speech", cutting down
      false barge-in triggers from the assistant's own TTS bleeding back
      into the mic (no hardware AEC on shared speaker/mic laptops — for the
      cleanest experience, headphones remove this class of issue entirely).
    """

    _FRAME_SAMPLES    = 320   # 20ms @ 16kHz — required frame size for webrtcvad
    _MAX_VAD_BUF      = 320 * 8

    # Fenêtres de décision, exprimées en MILLISECONDES et converties en nombre
    # d'appels d'après la taille réelle du chunk (voir has_speech).
    #
    # Elles étaient auparavant écrites en « frames » comptées à chaque appel de
    # has_speech(), c'est-à-dire une par CHUNK (64 ms), alors que les commentaires
    # les décrivaient en trames de 20 ms : toutes les durées réelles valaient donc
    # le triple de l'intention. En mode strict, l'écart devenait franchement
    # bloquant — 3 × 25 = 75 chunks, soit 4,8 s de parole continue exigées pour
    # interrompre l'assistant au lieu des ~500 ms annoncées, ce qui rendait
    # l'interruption impossible en pratique.
    # En environnement bruyant, 110 ms ouvrait un tour sur une syllabe distante
    # ou une meuleuse. 220 ms reste naturel pour une vraie phrase au micro,
    # mais demande une parole soutenue avant tout trafic réseau.
    _ATTACK_MS        = 220
    _STRICT_ATTACK_MS = 420   # Interruption volontaire rapide, toujours anti-écho
    # Maintien après la dernière trame voisée. C'est aussi, depuis que le VAD
    # pilote les tours côté client, ce qui définit la FIN d'une phrase : trop
    # court, une hésitation en plein milieu (« ouvre… euh… Firefox ») coupe la
    # phrase en deux et l'assistant répond à la moitié. Mesuré sur une phrase
    # marquant une pause d'environ 800 ms : 750 ms la coupait en deux, 950 ms
    # la garde entière. On reprend donc la valeur que visait déjà la config
    # serveur précédente (silence_duration_ms=900).
    _HANGOVER_MS      = 950

    # Seuils de rejet du bruit ---------------------------------------------
    # Marge de rapport signal/bruit exigée au-dessus du plancher mesuré : la
    # parole proche du micro dépasse largement ce facteur, la pluie ou une
    # conversation lointaine non.
    _SNR_MARGIN       = 3.0
    # Le plancher absolu (contre le souffle en pièce très silencieuse) est
    # désormais `self.noise_threshold` — réglable en direct par le slider
    # de sensibilité du panneau Audio, voir has_speech().
    # Niveau exigé pendant que l'assistant parle (interruption volontaire).
    _MIN_RMS_STRICT   = 0.15
    # Décroissance du pic glissant par chunk (~0.85 ≈ 400 ms de mémoire).
    _PEAK_DECAY       = 0.85
    # Planéité spectrale maximale tolérée pour de la parole. La voix est très
    # structurée (harmoniques + formants) et se situe typiquement sous 0.35 ;
    # un bruit large bande (souffle, pluie, ventilateur, statique) tend vers 1.
    # C'est le seul critère qui écarte un bruit blanc FORT, que ni l'énergie ni
    # webrtcvad ne distinguent de la parole.
    _MAX_FLATNESS     = 0.45
    # Périodicité minimale exigée pour ouvrir un tour de parole. La voix voisée
    # dépasse largement ce seuil, un bruit continu reste en dessous.
    _MIN_HARMONICITY  = 0.25

    # Le chemin nominal est Silero, entraîné sur de la parole dans du bruit.
    # WebRTC et les critères spectraux ne servent que de repli. Utiliser deux
    # seuils crée une hystérésis naturelle : seuil franc pour ouvrir, plus bas
    # pour conserver consonnes et fins de mots une fois la phrase commencée.
    _NEURAL_START_THRESHOLD = 0.62
    _NEURAL_KEEP_THRESHOLD = 0.18
    _NEURAL_STRICT_THRESHOLD = 0.94
    # Une probabilité neuronale isolée ne suffit pas à ouvrir un tour : les
    # faux positifs réels observés sous pluie/ventilateur sont éliminés par une
    # seconde preuve spectrale, sans toucher au maintien d'une phrase en cours.
    _START_MAX_FLATNESS = 0.38
    _MIN_VOICE_BAND = 0.05

    def __init__(self, target_rms: float = 0.22, noise_threshold_db: float = -50.0,
                 vad_aggressiveness: int = 3):
        _ensure_lfilter_loading()
        self.target_rms = target_rms
        self.noise_threshold = 10 ** (noise_threshold_db / 20.0)

        # Stateful DC-blocker filter variables across streaming chunks
        self._prev_x = 0.0
        self._prev_y = 0.0

        self._vad = None
        try:
            import webrtcvad
            self._vad = webrtcvad.Vad(vad_aggressiveness)
        except Exception:
            logger.warning(
                "webrtcvad not installed — falling back to RMS-based speech "
                "detection (less accurate). Install with: pip install webrtcvad-wheels"
            )
        self._vad_buf = np.empty(0, dtype=np.int16)

        self._voiced_run   = 0
        self._silence_run  = 0
        self._speech_active = False
        self._noise_floor  = 0.003   # adaptive ambient-noise RMS estimate
        self._recent_peak  = 0.0     # pic de niveau à décroissance lente
        # Plancher appris par is_voice_like(), indépendant du verrou de
        # has_speech() : lui continue d'apprendre sous une pluie battante.
        self._ambient_floor = 0.003
        # Garder la référence (pas seulement id(audio)) empêche Python de
        # réutiliser l'identifiant mémoire du chunk précédent et de servir une
        # probabilité périmée au callback suivant.
        self._last_audio_ref = None
        self._last_neural_probability = 0.0
        self._last_neural_available = False
        self._noise_spectrum = None

    def reset_stream_state(self) -> None:
        """Oublie l'ancien périphérique sans perdre le réglage utilisateur.

        Un changement micro est une discontinuité complète : niveau, souffle,
        composante continue et historique récurrent de Silero n'ont plus aucun
        rapport avec la nouvelle source. Les conserver pouvait rendre un micro
        USB faible invisible après un micro interne bruyant, ou maintenir une
        probabilité de parole sur les premières trames du nouveau flux.

        ``noise_threshold`` et ``target_rms`` sont volontairement conservés :
        ils portent le choix de sensibilité de l'utilisateur, pas une mesure du
        périphérique précédent.
        """
        self._prev_x = 0.0
        self._prev_y = 0.0
        self._vad_buf = np.empty(0, dtype=np.int16)
        self._voiced_run = 0
        self._silence_run = 0
        self._speech_active = False
        self._noise_floor = 0.003
        self._recent_peak = 0.0
        self._ambient_floor = 0.003
        self._last_audio_ref = None
        self._last_neural_probability = 0.0
        self._last_neural_available = False
        self._noise_spectrum = None

        vad = self._silero()
        if vad is not None:
            try:
                vad.reset()
            except Exception:
                # Le repli WebRTC reste disponible si le backend ONNX vient de
                # disparaître ; une réinitialisation ne doit jamais tuer le mic.
                pass

    @staticmethod
    def _harmonicity(audio: np.ndarray, sample_rate: int = 16000) -> float:
        """
        Périodicité du signal : pic d'autocorrélation normalisée dans la plage
        de hauteur de la voix humaine (70–350 Hz).

        La parole voisée est quasi périodique (cordes vocales) et produit un pic
        marqué. Les bruits — blanc comme rose (pluie, ventilateur, trafic) — sont
        apériodiques et n'en produisent aucun. C'est le critère qui sépare la voix
        d'un bruit de fond continu, là où l'énergie et la planéité spectrale
        échouent toutes les deux.
        """
        n = audio.size
        if n < 512:
            return 0.0
        x = audio - np.mean(audio)
        energy = np.dot(x, x)
        if energy <= 1e-12:
            return 0.0

        # Autocorrélation par FFT (O(n log n) plutôt que O(n²))
        size = 1 << int(np.ceil(np.log2(2 * n)))
        spec = np.fft.rfft(x, size)
        ac = np.fft.irfft(spec * np.conj(spec), size)[:n]

        min_lag = int(sample_rate / 350)   # 350 Hz : voix aiguë
        max_lag = int(sample_rate / 70)    # 70 Hz  : voix grave
        if max_lag >= n:
            max_lag = n - 1
        if min_lag >= max_lag:
            return 0.0

        return float(np.max(ac[min_lag:max_lag]) / energy)

    @staticmethod
    def _spectral_flatness(audio: np.ndarray) -> float:
        """
        Planéité spectrale (entropie de Wiener) : moyenne géométrique sur
        moyenne arithmétique du spectre de puissance.

        ≈ 1.0 pour un bruit large bande, nettement plus bas pour la parole
        dont l'énergie se concentre sur les harmoniques et les formants.
        """
        if audio.size < 256:
            return 0.0
        spec = np.abs(np.fft.rfft(audio * np.hanning(audio.size))) ** 2
        # On ignore le continu et les très basses fréquences (ronflements secteur)
        spec = spec[3:]
        spec = spec[spec > 0]
        if spec.size < 16:
            return 0.0
        geo = np.exp(np.mean(np.log(spec)))
        arith = np.mean(spec)
        return float(geo / arith) if arith > 0 else 0.0

    # -- run WebRTC VAD over as many complete 20ms frames as are buffered --
    def _vad_frames(self, pcm_int16: np.ndarray) -> list:
        self._vad_buf = np.concatenate([self._vad_buf, pcm_int16])
        if len(self._vad_buf) > self._MAX_VAD_BUF:
            self._vad_buf = self._vad_buf[-self._MAX_VAD_BUF:]
        flags = []
        n = self._FRAME_SAMPLES
        while len(self._vad_buf) >= n:
            frame = self._vad_buf[:n]
            self._vad_buf = self._vad_buf[n:]
            try:
                flags.append(self._vad.is_speech(frame.tobytes(), 16000))
            except Exception:
                flags.append(False)
        return flags

    # Vitesse d'apprentissage du plancher ambiant dans is_voice_like().
    # ~0.02 par chunk ≈ quelques secondes pour absorber une averse qui monte.
    _AMBIENT_ALPHA = 0.02

    # Périodicité exigée par is_voice_like(), nettement plus stricte que le
    # _MIN_HARMONICITY (0.30) de has_speech(). Les deux répondent à des
    # questions différentes : has_speech() cherche à ne RIEN rater d'une phrase
    # en cours, is_voice_like() doit au contraire pouvoir affirmer « ceci n'est
    # pas de la voix » sous une averse. Mesuré sur 200 tirages de bruit rose et
    # de voix bruitée : pluie max 0.49, voix min 0.82 — 0.60 tombe dans le creux.
    # Périodicité minimale. Volontairement bien plus basse que la voix seule
    # (0.82) : dès que l'utilisateur parle DEVANT un ventilateur, les deux
    # sources périodiques de hauteurs différentes cassent mutuellement leur pic
    # d'autocorrélation, et la mesure retombe vers 0.30. Un seuil serré coupait
    # donc la parole exactement quand la pièce est bruyante. La pluie n'est pas
    # rattrapée ici mais par la planéité spectrale ci-dessous.
    _VOICE_HARMONICITY = 0.40

    # Planéité spectrale maximale. C'est LE critère qui écarte la pluie.
    _VOICE_FLATNESS = 0.15

    # Énergie minimale dans la bande vocale (300–3400 Hz) rapportée aux graves (<300 Hz).
    _MIN_VOICE_BAND = 0.09

    # Marge d'énergie au-dessus du plancher ambiant, pour is_voice_like().
    _VOICE_SNR = 1.2

    @staticmethod
    def _voice_band_ratio(audio: np.ndarray, sample_rate: int = 16000) -> float:
        """Énergie 300–3400 Hz rapportée à l'énergie sous 300 Hz.

        Sépare la voix (formants étalés jusqu'à plusieurs kHz) des ronflements
        de machine (ventilateur, moteur, ronflement secteur), qui restent
        concentrés dans les graves quelle que soit leur périodicité.
        """
        if audio.size < 256:
            return 0.0
        spec = np.abs(np.fft.rfft(audio * np.hanning(audio.size))) ** 2
        freqs = np.fft.rfftfreq(audio.size, 1.0 / sample_rate)
        low = spec[(freqs > 20) & (freqs < 300)].sum()
        voice = spec[(freqs >= 300) & (freqs < 3400)].sum()
        if low <= 1e-12:
            return float("inf") if voice > 0 else 0.0
        return float(voice / low)

    @staticmethod
    def _silero():
        """VAD neuronal partagé, chargé à la première utilisation.

        Import différé : `core.vad_silero` tire onnxruntime, qu'on ne veut pas
        payer à l'import de ce module ni rendre obligatoire.
        """
        if AudioPreprocessor._silero_vad is None:
            try:
                from core.vad_silero import get_vad
                AudioPreprocessor._silero_vad = get_vad()
            except Exception as e:
                print(f"[AudioPreprocessor] VAD neuronal indisponible : {e}")
                AudioPreprocessor._silero_vad = False
        return AudioPreprocessor._silero_vad or None

    _silero_vad = None   # None = pas encore tenté, False = indisponible

    def _neural_probability(self, audio: np.ndarray) -> tuple[Optional[float], bool]:
        """Probabilité Silero, calculée une seule fois par chunk.

        `has_speech()` et `is_voice_like()` sont appelés successivement avec le
        même tableau dans le callback micro. Faire deux inférences avançait deux
        fois l'état récurrent du modèle avec le même son et dégradait ses
        décisions. Le cache par identité du tableau évite ce doublon.
        """
        if self._last_audio_ref is audio:
            value = self._last_neural_probability
            return (value if self._last_neural_available else None,
                    self._last_neural_available)

        vad = self._silero()
        available = bool(vad is not None and vad.available)
        probability = float(vad.probability(audio)) if available else 0.0
        self._last_audio_ref = audio
        self._last_neural_probability = probability
        self._last_neural_available = available
        return (probability if available else None), available

    def _suppress_stationary_noise(self, audio: np.ndarray,
                                   speech_probability: float) -> np.ndarray:
        """Réduction spectrale adaptative, prudente et sans dépendance lourde.

        Le profil du bruit est appris uniquement lorsque Silero est sûr qu'il
        n'y a pas de voix. Pendant la parole, un gain de Wiener borné retire la
        part stationnaire (ventilateur, pluie régulière, souffle du micro), tout
        en conservant au moins 22 % de chaque bande afin de ne pas mutiler les
        consonnes. Un mélange sec final limite les artefacts musicaux typiques
        d'une suppression spectrale trop agressive.
        """
        if audio.size < 256:
            return audio

        spectrum = np.fft.rfft(audio)
        power = (spectrum.real ** 2 + spectrum.imag ** 2).astype(np.float64)

        if speech_probability < 0.08:
            if self._noise_spectrum is None or self._noise_spectrum.shape != power.shape:
                self._noise_spectrum = power.copy()
            else:
                # Apprentissage rapide au démarrage, lent ensuite pour suivre
                # une averse qui monte sans absorber une syllabe accidentelle.
                alpha = 0.08 if not self._speech_active else 0.015
                self._noise_spectrum = (
                    (1.0 - alpha) * self._noise_spectrum + alpha * power
                )
            return audio

        if self._noise_spectrum is None or self._noise_spectrum.shape != power.shape:
            return audio

        gain = 1.0 - 1.15 * self._noise_spectrum / (power + 1e-12)
        gain = np.clip(gain, 0.22, 1.0)
        # Lisser entre bandes évite les petits pics isolés responsables du
        # bruit "métallique". Les bords sont conservés par padding.
        padded = np.pad(gain, (2, 2), mode="edge")
        kernel = np.array([1, 2, 3, 2, 1], dtype=np.float64) / 9.0
        gain = np.convolve(padded, kernel, mode="valid")
        denoised = np.fft.irfft(spectrum * gain, n=audio.size).astype(np.float32)
        return (0.82 * denoised + 0.18 * audio).astype(np.float32)

    def is_voice_like(
        self,
        audio: np.ndarray,
        strict: bool = False,
        opening: bool = False,
    ) -> bool:
        """Ce chunk ressemble-t-il à de la voix humaine ? Décision SANS verrou."""
        if audio.size == 0:
            return False

        rms = float(np.sqrt(np.mean(audio ** 2)))
        probability, neural_available = self._neural_probability(audio)
        if neural_available:
            assert probability is not None
            threshold = (
                self._NEURAL_STRICT_THRESHOLD if strict
                else (self._NEURAL_START_THRESHOLD if opening
                      else self._NEURAL_KEEP_THRESHOLD)
            )
            neural_pass = probability >= threshold

            # Repli hybride pour voix douce, fatiguée ou grave (probabilité modérée 0.20+)
            if not neural_pass and not strict and probability >= 0.45:
                flatness = self._spectral_flatness(audio)
                harmonicity = self._harmonicity(audio)
                band_ratio = self._voice_band_ratio(audio)
                if (
                    harmonicity >= 0.30
                    and flatness <= self._START_MAX_FLATNESS
                    and band_ratio >= 0.05
                    and rms >= self.noise_threshold
                ):
                    neural_pass = True

            voice = neural_pass
            if (strict or opening) and voice:
                if strict:
                    level_floor = min(
                        self._MIN_RMS_STRICT,
                        max(self.noise_threshold * 3.0,
                            self._ambient_floor * 1.8, 0.025),
                    )
                else:
                    level_floor = max(
                        # Pour ouvrir une phrase, une voix doit dépasser
                        # franchement le bruit ambiant.  Le plancher reste
                        # assez bas pour une voix proche mais douce.
                        self.noise_threshold * 1.3,
                        self._noise_floor * 2.0,
                        0.008,
                    )

                band_req = self._MIN_VOICE_BAND if probability < 0.65 else 0.03
                flat_req = self._START_MAX_FLATNESS if probability < 0.65 else 0.45

                voice = (
                    rms >= level_floor
                    and self._voice_band_ratio(audio) >= band_req
                    and self._spectral_flatness(audio) <= flat_req
                )
            if not voice:
                a = self._AMBIENT_ALPHA
                self._ambient_floor = (1.0 - a) * self._ambient_floor + a * rms
            return voice

        gate = max(self._ambient_floor * self._VOICE_SNR, self.noise_threshold)
        voice = (
            rms >= gate
            and self._spectral_flatness(audio) <= self._VOICE_FLATNESS
            and self._harmonicity(audio) >= self._VOICE_HARMONICITY
            and self._voice_band_ratio(audio) >= self._MIN_VOICE_BAND
        )

        if not voice:
            a = self._AMBIENT_ALPHA
            self._ambient_floor = (1.0 - a) * self._ambient_floor + a * rms

        return voice

    def has_speech(self, audio: np.ndarray, strict: bool = False) -> bool:
        """
        Debounced speech-activity decision for this chunk (updates internal state).
        Pass strict=True while the assistant is speaking to require a longer
        sustained-voice window before confirming (reduces echo false-positives).

        In strict mode: requires BOTH VAD AND high RMS to reject background noise (rain, wind, etc).
        """
        if audio.size == 0:
            return self._speech_active

        # Fenêtres exprimées en durée réelle : indépendantes de CHUNK_SIZE.
        chunk_ms  = max(1.0, audio.size / 16.0)          # 16 échantillons/ms à 16 kHz
        attack    = max(1, round((self._STRICT_ATTACK_MS if strict
                                  else self._ATTACK_MS) / chunk_ms))
        hangover  = max(1, round(self._HANGOVER_MS / chunk_ms))

        rms = float(np.sqrt(np.mean(audio ** 2)))

        # Décision neuronale en priorité avec fusion acoustique pour voix douce/fatiguée
        neural_probability, neural_available = self._neural_probability(audio)
        if neural_available:
            threshold = (
                self._NEURAL_STRICT_THRESHOLD if strict
                else (self._NEURAL_KEEP_THRESHOLD if self._speech_active
                      else self._NEURAL_START_THRESHOLD)
            )
            voiced = bool(neural_probability is not None
                          and neural_probability >= threshold
                          and rms >= self.noise_threshold)

            # Repli hybride si probabilité modérée (0.20+) sur voix fatiguée / douce
            if not voiced and not strict and neural_probability is not None and neural_probability >= 0.20:
                if (self._harmonicity(audio) >= 0.30
                        and self._spectral_flatness(audio) <= self._START_MAX_FLATNESS
                        and self._voice_band_ratio(audio) >= 0.05
                        and rms >= self.noise_threshold):
                    voiced = True

            if voiced and not self._speech_active:
                # Double accord pour DÉMARRER : réseau neuronal + signature
                # spectrale humaine. Une fois la phrase ouverte, seul le seuil
                # neuronal bas conserve les consonnes et fins de mots.
                # Une voix lointaine peut être très bien classée « parole »
                # par Silero. Pour OUVRIR un tour, elle doit aussi dominer le
                # niveau ambiant : ce test est la barrière contre les autres
                # personnes et les machines d'atelier entendues au loin.
                start_floor = max(
                    self.noise_threshold * 1.3,
                    self._noise_floor * 2.0,
                    0.008,
                )
                band_req = self._MIN_VOICE_BAND if (neural_probability or 0) < 0.65 else 0.03
                flat_req = self._START_MAX_FLATNESS if (neural_probability or 0) < 0.65 else 0.45
                voiced = (
                    rms >= start_floor
                    and self._voice_band_ratio(audio) >= band_req
                    and self._spectral_flatness(audio) <= flat_req
                )
        else:
            voiced = False

        if not neural_available and self._vad is not None:
            pcm16 = (np.clip(audio, -1.0, 1.0) * 32767.0).astype(np.int16)
            frames = self._vad_frames(pcm16)
            # Majorité de trames voisées, pas « au moins une » : webrtcvad
            # déclenche sur une seule trame pour un claquement de porte ou un
            # choc clavier. Exiger la majorité impose une continuité que le
            # bruit impulsionnel n'a pas.
            voiced = (sum(frames) * 2 > len(frames)) if frames else False
        elif not neural_available:
            voiced = rms > max(self._noise_floor * 10.0, self.noise_threshold)

        # Pic glissant : le RMS instantané s'effondre entre deux syllabes et
        # entre les mots. Comparer le seuil à ce creux couperait la parole en
        # morceaux ; on le compare donc à un pic qui décroît lentement.
        self._recent_peak = max(rms, self._recent_peak * self._PEAK_DECAY)

        # Porte d'énergie adaptative : la parole utile domine nettement le bruit
        # de fond mesuré. C'est ce qui écarte la pluie, un ventilateur, une télé
        # ou des voix lointaines, qui passent le VAD mais restent au niveau du
        # plancher de bruit.
        if voiced and not strict and not neural_available:
            # `self.noise_threshold` est réglable en direct par le slider de
            # sensibilité du panneau Audio (_on_mic_sensitivity_change côté
            # main.py) — avant ce correctif, cet attribut n'était jamais lu
            # ici : le curseur ne changeait donc RIEN à la détection réelle,
            # seul le plancher fixe _MIN_SPEECH_RMS comptait.
            floor_gate = max(self._noise_floor * self._SNR_MARGIN, self.noise_threshold)
            voiced = self._recent_peak >= floor_gate

        # Rejet du bruit large bande, quel que soit son volume.
        if (voiced and not neural_available
                and self._spectral_flatness(audio) > self._MAX_FLATNESS):
            voiced = False

        # Rejet des bruits continus non périodiques (pluie, ventilateur, trafic).
        # Une fois la parole verrouillée on relâche ce critère : les consonnes
        # sourdes (s, f, ch) ne sont pas périodiques et seraient coupées.
        if voiced and not neural_available and not self._speech_active:
            if self._harmonicity(audio) < self._MIN_HARMONICITY:
                voiced = False

        # En mode STRICT (l'assistant parle) : exiger VAD ET un niveau élevé,
        # pour que sa propre voix renvoyée par le haut-parleur ne déclenche pas
        # une fausse interruption.
        if strict and voiced:
            # Silero apporte déjà une forte preuve vocale ; un plancher absolu
            # de 0.15 obligeait à crier pour interrompre. On conserve une porte
            # anti-écho, mais adaptée au niveau ambiant et plafonnée.
            strict_floor = min(
                self._MIN_RMS_STRICT,
                max(self.noise_threshold * 3.0, self._noise_floor * 2.5, 0.025),
            )
            voiced = rms >= strict_floor

        if voiced:
            self._voiced_run += 1
            self._silence_run = 0
        else:
            self._silence_run += 1
            # Une voix fatiguée alterne souvent voyelles reconnues et consonnes
            # faibles. Remettre la preuve à zéro au moindre trou exigeait une
            # suite de chunks parfaits et rendait les phrases douces invisibles.
            # La preuve décroît désormais progressivement ; un bruit isolé ne
            # peut toujours pas atteindre la fenêtre d'attaque.
            self._voiced_run = max(0, self._voiced_run - 1)
            if not self._speech_active:
                # Calibration rapide au démarrage et suivi lent ensuite. Le
                # précédent apprentissage attendait 900 ms avant de commencer,
                # laissant justement à un ventilateur le temps d'ouvrir un faux
                # tour pendant la première seconde.
                alpha = 0.12 if self._silence_run <= 16 else 0.025
                self._noise_floor = (
                    (1.0 - alpha) * self._noise_floor + alpha * rms
                )
            # Only learn the noise floor once we're confidently past any speech tail
            elif self._silence_run > hangover:
                self._noise_floor = 0.98 * self._noise_floor + 0.02 * rms

        if self._voiced_run >= attack:
            self._speech_active = True
        elif self._silence_run >= hangover:
            self._speech_active = False

        return self._speech_active

    def process(self, audio: np.ndarray, sample_rate: int = 16000,
                attenuate_silence: bool = True) -> np.ndarray:
        """
        Filtre un chunk audio (bloqueur DC + AGC bornée sur parole confirmée).

        `attenuate_silence=False` désactive l'écrasement du non-parole. À
        utiliser quand l'appelant garde ces chunks dans un tampon de pré-amorce :
        un début de mot atténué 20x serait inexploitable une fois réinjecté.
        L'écrasement n'a de sens que si le chunk est envoyé tel quel au modèle.
        """
        if audio.size == 0:
            return audio

        audio = audio.astype(np.float32)

        # Stateful 1-pole DC Blocker (High-pass filter at ~80Hz, stateful across chunks)
        # y[n] = x[n] - x[n-1] + R * y[n-1]  (R = 0.968 for 16kHz)
        #
        # Cette boucle s'exécutait en Python pur, échantillon par échantillon :
        # 3 ms par chunk, dans le callback audio. Qt tournant dans le thread
        # principal et l'audio dans un thread asyncio, tout temps CPU pris ici
        # l'est au GIL, donc à la fluidité de la voix. La forme vectorisée donne
        # le même résultat à 5e-7 près, en ~0,05 ms.
        R = _DC_R
        if _LFILTER is not None:
            # État initial équivalent à (px, py) : y[0] = x[0] - px + R*py.
            zi = np.array([-self._prev_x + R * self._prev_y], dtype=np.float64)
            out, zf = _LFILTER([1.0, -1.0], [1.0, -R], audio.astype(np.float64), zi=zi)
            out = out.astype(np.float32)
            if audio.size:
                self._prev_x, self._prev_y = float(audio[-1]), float(out[-1])
        else:
            out = np.empty_like(audio)
            px, py = self._prev_x, self._prev_y
            for i in range(len(audio)):
                x = audio[i]
                y = x - px + R * py
                out[i] = y
                px, py = x, y
            self._prev_x, self._prev_y = px, py
        audio = out

        # Le VAD a déjà analysé ce chunk dans le callback. Sa probabilité
        # pilote ici l'apprentissage du bruit, sans seconde inférence.
        speech_probability = (
            self._last_neural_probability if self._last_neural_available
            else (1.0 if self._speech_active else 0.0)
        )
        audio = self._suppress_stationary_noise(audio, speech_probability)

        rms = float(np.sqrt(np.mean(audio ** 2))) if audio.size > 0 else 0.0

        # `_speech_active` reste vrai pendant tout le maintien de fin de phrase
        # (_HANGOVER_MS). Sans la condition de niveau ci-dessous, ces chunks de
        # queue — du silence — se faisaient remonter 12x, si bien que chaque
        # phrase envoyée se terminait par ~750 ms de souffle amplifié.
        if self._speech_active and rms > max(self._noise_floor * 1.5, 1e-4):
            # Confirmed voice only: bounded AGC boost (max 16x) for soft/tired voices.
            gain = min(self.target_rms / max(rms, 1e-4), 16.0)
            audio = audio * gain
            audio = np.tanh(audio).astype(np.float32)  # soft limiter, no harsh clipping
        elif attenuate_silence:
            # Confirmed silence/noise: attenuate hard instead of boosting it, so
            # room hiss / fan noise / speaker bleed never gets sent at meaningful volume.
            audio = audio * 0.05

        return audio


DEFAULT_CONTEXT_PROMPT = (
    "Commandes vocales et assistant informatique en français et anglais : "
    "VS Code, Visual Studio Code, Firefox, Chrome, YouTube, Terminal, Python, Git, Docker, Discord, Spotify, VLC. "
    "Actions système : ouvre, ferme, lance, tue le processus, cherche sur Google, supprime, crée le fichier, "
    "monte le volume, règle le son. Commandes CLI : ls -la, cd, git push, git commit, python main.py."
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
@dataclass
class WhisperConfig:
    model_name: str = "large-v3-turbo"
    language: Optional[str] = "fr"          # Force French for better accuracy
    device: str = "auto"
    compute_type: str = "auto"             # "auto", "float16", "int8", "int8_float16"
    beam_size: int = 10                     # Increased for better accuracy with noise
    best_of: int = 5
    condition_on_previous_text: bool = True
    vad_filter: bool = True
    vad_min_silence_ms: int = 500           # Increased silence detection for robust VAD
    no_speech_threshold: float = 0.35       # Lower threshold = better detection in noisy env
    log_prob_threshold: float = -0.5        # More lenient for background noise robustness
    temperature: float = 0.0
    initial_prompt: Optional[str] = DEFAULT_CONTEXT_PROMPT
    enable_ai_corrector: bool = True
    enable_noise_reduction: bool = True
    download_timeout: int = 120            # seconds
    max_retries: int = 1
    num_threads: int = 4
    # Streaming VAD buffer config
    streaming_vad_window_samples: int = 512   # for 16 kHz
    streaming_vad_threshold: float = 0.35     # Lower = more sensitive to speech
    streaming_speech_pad_ms: int = 300        # Increased padding for better speech detection
    streaming_min_speech_ms: int = 400        # Minimum speech length to avoid micro-noises
    streaming_max_speech_ms: int = 15000


@dataclass
class VoskConfig:
    model_path: Optional[str] = None
    language: str = "en-us"                # "en-us", "fr", etc.
    sample_rate: int = 16000

# ---------------------------------------------------------------------------
# Shared model cache
# ---------------------------------------------------------------------------
_WHISPER_MODEL_CACHE = {}
_VOSK_MODEL_CACHE = {}

# ---------------------------------------------------------------------------
# Whisper STT – fully upgraded
# ---------------------------------------------------------------------------
class WhisperSTT:
    """
    Offline & streaming transcription using faster-whisper.

    Features:
      - Auto device selection (CUDA/CPU) with graceful fallback.
      - VAD‑buffered streaming via ``transcribe_stream()`` (generator).
      - Synchronous ``transcribe()`` for whole audio arrays.
      - Async wrappers for both modes.
      - Shared model instance across multiple objects (same model_name/device).
    """

    def __init__(self, config: Optional[WhisperConfig] = None, **kwargs):
        self._cfg = config or WhisperConfig(**kwargs)
        self._model = None
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._vad_model = None  # Silero VAD (lazy load)
        self._preprocessor = AudioPreprocessor()

        # Import STTCorrector
        try:
            from core.ai_stt_corrector import STTCorrector
            self._corrector = STTCorrector(use_llm_correction=self._cfg.enable_ai_corrector)
        except Exception as e:
            logger.warning("Could not import STTCorrector: %s", e)
            self._corrector = None

        # Resolve device/compute_type
        self._device, self._compute_type = self._resolve_device()

        # Load model (will download if needed)
        self._load_model()

    # ------------------------------------------------------------------
    # Internal: device resolution
    # ------------------------------------------------------------------
    def _resolve_device(self) -> Tuple[str, str]:
        if self._cfg.device != "auto":
            return self._cfg.device, self._cfg.compute_type
        try:
            import torch
            if torch.cuda.is_available():
                device = "cuda"
                compute = "float16" if self._cfg.compute_type == "auto" else self._cfg.compute_type
            else:
                device = "cpu"
                compute = "int8" if self._cfg.compute_type == "auto" else self._cfg.compute_type
        except ImportError:
            device = "cpu"
            compute = "int8"
        logger.info("Whisper device=%s, compute_type=%s", device, compute)
        return device, compute

    # ------------------------------------------------------------------
    # Model loading with caching and retry
    # ------------------------------------------------------------------
    def _load_model(self):
        cache_key = (self._cfg.model_name, self._device, self._compute_type)
        if cache_key in _WHISPER_MODEL_CACHE:
            self._model = _WHISPER_MODEL_CACHE[cache_key]
            return

        from faster_whisper import WhisperModel

        retries = self._cfg.max_retries + 1
        last_err = None
        for attempt in range(retries):
            try:
                logger.info("Loading Whisper '%s' (attempt %d/%d) …", self._cfg.model_name, attempt+1, retries)
                self._model = WhisperModel(
                    self._cfg.model_name,
                    device=self._device,
                    compute_type=self._compute_type,
                    cpu_threads=self._cfg.num_threads,
                    num_workers=1,
                    download_root=None,  # use default cache
                )
                _WHISPER_MODEL_CACHE[cache_key] = self._model
                logger.info("Whisper '%s' ready.", self._cfg.model_name)
                return
            except Exception as e:
                last_err = e
                msg = str(e).lower()
                if any(k in msg for k in ("offline", "not found", "cache", "localentry",
                                          "does not exist", "outgoing", "local_files_only")):
                    logger.warning("Whisper model not cached – forcing online download.")
                    # Clear offline env flags for this process
                    for var in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE"):
                        os.environ.pop(var, None)
                    # Retry immediately with download allowed
                    try:
                        self._model = WhisperModel(
                            self._cfg.model_name,
                            device=self._device,
                            compute_type=self._compute_type,
                            cpu_threads=self._cfg.num_threads,
                        )
                        _WHISPER_MODEL_CACHE[cache_key] = self._model
                        logger.info("Whisper '%s' downloaded and ready.", self._cfg.model_name)
                        return
                    except Exception as dl_err:
                        last_err = dl_err
                        # If still failing, maybe network issue – retry loop will continue
                # Wait before next attempt (except last)
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
        # All attempts exhausted
        raise RuntimeError(
            f"Failed to load Whisper model '{self._cfg.model_name}' after {retries} attempt(s). "
            f"Last error: {last_err}"
        ) from last_err

    # ------------------------------------------------------------------
    # Whole-array transcription (sync)
    # ------------------------------------------------------------------
    def transcribe(self, audio: np.ndarray, sample_rate: int = 16000) -> str:
        """
        Transcribe a complete audio array.

        Args:
            audio: numpy array (any shape/dtype). Will be converted to float32 mono 16kHz.
            sample_rate: original sample rate (ignored if already 16k).

        Returns:
            Clean transcript string.
        """
        audio = _ensure_float32_mono_16k(audio, sample_rate)
        if self._cfg.enable_noise_reduction:
            audio = self._preprocessor.process(audio, sample_rate=16000)

        lang = self._cfg.language
        if lang and lang.strip().lower() == "auto":
            lang = None

        try:
            segments, info = self._model.transcribe(
                audio,
                language=lang,
                beam_size=self._cfg.beam_size,
                best_of=self._cfg.best_of,
                temperature=self._cfg.temperature,
                condition_on_previous_text=self._cfg.condition_on_previous_text,
                initial_prompt=self._cfg.initial_prompt,
                vad_filter=self._cfg.vad_filter,
                vad_parameters={"min_silence_duration_ms": self._cfg.vad_min_silence_ms} if self._cfg.vad_filter else None,
                no_speech_threshold=self._cfg.no_speech_threshold,
                log_prob_threshold=self._cfg.log_prob_threshold,
                compression_ratio_threshold=2.4,
            )
            raw_text = " ".join(seg.text for seg in segments).strip()

            # Post-STT AI & Phonetic correction
            if self._corrector:
                raw_text = self._corrector.correct(raw_text)

            return raw_text
        except Exception as e:
            logger.error("Whisper transcription failed: %s", e)
            raise

    # ------------------------------------------------------------------
    # Async whole-array transcription
    # ------------------------------------------------------------------
    async def transcribe_async(self, audio: np.ndarray, sample_rate: int = 16000) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self.transcribe, audio, sample_rate)

    # ------------------------------------------------------------------
    # VAD‑buffered streaming transcription (sync generator)
    # ------------------------------------------------------------------
    def transcribe_stream(self, sample_rate: int = 16000) -> Iterator[Tuple[str, bool]]:
        """
        Return a generator that accepts audio chunks (np.float32) and yields (text, is_final).
        Uses Silero VAD to detect speech segments and transcribes them with Whisper.

        Usage::

            stream = whisper_stt.transcribe_stream()
            for chunk in audio_source:
                text, is_final = stream.send(chunk)

        The generator must be primed with ``stream.send(None)`` once.
        """
        try:
            import torch
            import torchaudio
        except ImportError:
            raise ImportError("streaming requires torch and torchaudio (Silero VAD)")

        # Lazy load Silero VAD model
        if self._vad_model is None:
            self._vad_model, utils = torch.hub.load(
                repo_or_dir="snakers4/silero-vad",
                model="silero_vad",
                force_reload=False,
                onnx=False,
            )
            self._vad_get_speech_ts = utils[0]  # get_speech_timestamps

        # Audio buffer as float32
        buffer = np.empty(0, dtype=np.float32)
        vad_sr = 16000  # Silero expects 16k
        min_speech_samples = int(self._cfg.streaming_min_speech_ms * vad_sr / 1000)
        max_speech_samples = int(self._cfg.streaming_max_speech_ms * vad_sr / 1000)
        pad_samples = int(self._cfg.streaming_speech_pad_ms * vad_sr / 1000)

        # Generator initialisation
        chunk = yield ("", False)  # prime
        while True:
            if chunk is not None:
                # Resample if needed and append
                if sample_rate != vad_sr:
                    from scipy.signal import resample as sp_resample
                    new_len = int(len(chunk) * vad_sr / sample_rate)
                    chunk = sp_resample(chunk, new_len).astype(np.float32)
                buffer = np.concatenate([buffer, chunk])

            # Run VAD on buffer
            vad_tensor = torch.from_numpy(buffer).unsqueeze(0)
            speech_ts = self._vad_get_speech_ts(
                vad_tensor,
                self._vad_model,
                threshold=self._cfg.streaming_vad_threshold,
                min_speech_duration_ms=self._cfg.streaming_min_speech_ms,
                max_speech_duration_s=self._cfg.streaming_max_speech_ms / 1000,
                min_silence_duration_ms=self._cfg.vad_min_silence_ms,
                speech_pad_ms=self._cfg.streaming_speech_pad_ms,
                return_seconds=False,
            )

            if speech_ts:
                # Transcribe each segment, keep last one as non-final
                for i, ts in enumerate(speech_ts):
                    seg_audio = buffer[ts["start"]: ts["end"]]
                    # Only process if segment is long enough
                    if len(seg_audio) < min_speech_samples:
                        continue
                    # Last segment in list may still be growing -> not final
                    is_final = (i < len(speech_ts) - 1) or (buffer.size > max_speech_samples)
                    text = self.transcribe(seg_audio.astype(np.float32))
                    # After transcribing, we can drop processed audio up to the end of the final segment
                    if i == len(speech_ts) - 1:
                        buffer = buffer[ts["end"]:]
                    new_chunk = yield (text, is_final)
                    if new_chunk is not None:
                        if sample_rate != vad_sr:
                            from scipy.signal import resample as sp_resample2
                            new_len = int(len(new_chunk) * vad_sr / sample_rate)
                            new_chunk = sp_resample2(new_chunk, new_len).astype(np.float32)
                        buffer = np.concatenate([buffer, new_chunk])
                # If no speech at all, drop old data and wait
            else:
                # No speech – drop buffer except last padding window to catch start of speech
                if buffer.size > 2 * vad_sr:
                    buffer = buffer[-vad_sr:]  # keep 1 sec
                chunk = yield ("", False)

    # ------------------------------------------------------------------
    # Async streaming wrapper
    # ------------------------------------------------------------------
    async def transcribe_stream_async(self, sample_rate: int = 16000) -> AsyncIterator[Tuple[str, bool]]:
        """Async version of transcribe_stream. Use as ``async for text, is_final in ...``."""
        # We'll run the sync generator in an executor and queue results
        # This is a simplified implementation using a thread and queue.
        import queue
        import threading

        q = queue.Queue()
        stop_event = threading.Event()
        gen = self.transcribe_stream(sample_rate)
        next(gen)  # prime

        def _runner():
            while not stop_event.is_set():
                try:
                    chunk = q.get(timeout=0.1)
                    if chunk is None:
                        break
                    text, is_final = gen.send(chunk)
                    q.put(("result", text, is_final))
                except StopIteration:
                    break
                except Exception as e:
                    logger.exception("Streaming thread error")
                    q.put(("error", str(e)))
                    break

        thread = threading.Thread(target=_runner, daemon=True)
        thread.start()

        try:
            while True:
                item = q.get()
                if item[0] == "error":
                    raise RuntimeError(f"Streaming error: {item[1]}")
                elif item[0] == "result":
                    yield item[1], item[2]
        finally:
            stop_event.set()
            thread.join(timeout=2)

    def __del__(self):
        self._executor.shutdown(wait=False)

# ---------------------------------------------------------------------------
# Vosk STT – robust & streaming
# ---------------------------------------------------------------------------
class VoskSTT:
    """
    Streaming transcription using Vosk.
    """

    def __init__(self, config: Optional[VoskConfig] = None, **kwargs):
        self._cfg = config or VoskConfig(**kwargs)
        self._model = None
        self._rec = None
        self._load_model()

    def _load_model(self):
        model_id = self._cfg.model_path or self._cfg.language
        if model_id in _VOSK_MODEL_CACHE:
            self._model, self._rec = _VOSK_MODEL_CACHE[model_id]
            return

        from vosk import Model, KaldiRecognizer

        # Resolve language if no explicit path
        if self._cfg.model_path:
            model_path = self._cfg.model_path
        else:
            # Map language to default Vosk model names (common ones)
            lang = self._cfg.language.strip().lower()
            # Known small models
            language_map = {
                "en-us": "vosk-model-small-en-us-0.15",
                "en": "vosk-model-small-en-us-0.15",
                "fr": "vosk-model-small-fr-0.22",
                "de": "vosk-model-small-de-0.15",
                "es": "vosk-model-small-es-0.42",
                "ru": "vosk-model-small-ru-0.22",
                "cn": "vosk-model-small-cn-0.22",
                "ja": "vosk-model-small-ja-0.22",
                "pt": "vosk-model-small-pt-0.3",
                "it": "vosk-model-small-it-0.22",
                "nl": "vosk-model-small-nl-0.22",
                "tr": "vosk-model-small-tr-0.3",
                "ar": "vosk-model-small-ar-0.22",
                "fa": "vosk-model-small-fa-0.4",
                "ko": "vosk-model-small-ko-0.22",
                "hi": "vosk-model-small-hi-0.22",
                "vi": "vosk-model-small-vi-0.4",
            }
            model_name = language_map.get(lang, f"vosk-model-small-{lang}-0.15")
            # Vosk downloads automatically if not present; we just give the name
            model_path = model_name

        logger.info("Loading Vosk model: %s", model_path)
        try:
            model = Model(model_path)
        except Exception as e:
            raise RuntimeError(
                f"Failed to load Vosk model '{model_path}'. "
                f"Ensure it is installed or available in the Vosk model directory. "
                f"Details: {e}"
            ) from e

        rec = KaldiRecognizer(model, self._cfg.sample_rate)
        self._model = model
        self._rec = rec
        _VOSK_MODEL_CACHE[model_id] = (model, rec)
        logger.info("Vosk ready (model=%s)", model_path)

    def process_chunk(self, audio_bytes: bytes) -> Tuple[str, bool]:
        """
        Feed raw int16 LE PCM bytes.

        Returns (text, is_final).
        """
        if self._rec.AcceptWaveform(audio_bytes):
            result = json.loads(self._rec.Result())
            return result.get("text", ""), True
        partial = json.loads(self._rec.PartialResult())
        return partial.get("partial", ""), False

    async def process_chunk_async(self, audio_bytes: bytes) -> Tuple[str, bool]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.process_chunk, audio_bytes)

    def reset(self):
        """Reset the recognizer for a new utterance."""
        from vosk import KaldiRecognizer
        self._rec = KaldiRecognizer(self._model, self._cfg.sample_rate)
