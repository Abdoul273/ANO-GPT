"""Analyseur Prosodique Audio et Traitement du Signal Vocal pour ANO-GPT.

Module d'analyse acoustique temps réel et de modulation comportementale pour Jarvis :
1. Analyse acoustique 16kHz :
   - Hauteur fondamentale (F0 pitch via algorithme Yin vectorisé ou PyWorld)
   - Énergie / volume RMS et dynamique d'amplitude (dBFS, dynamic range, crest factor)
   - Débit de parole (syllabes par seconde estimées via les pics d'énergie de l'enveloppe)
   - Taux de voisement et score de chuchotement (ZCR, spectral balance, aperiodicité)
2. Classification contextuelle de l'humeur :
   - États qualifiés : 'calme', 'agacé/pressé', 'chuchoté/nuit', 'fatigué', 'neutre'
   - Filtrage temporel glissant (EMA + hystérésis) pour éviter les sauts brusques sur un mot isolé
3. Adaptation de Jarvis :
   - Génération de directives dynamiques pour le WebSocket Gemini Live
   - Modulation de vitesse et de volume pour la synthèse vocale (TTS) locale
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np

logger = logging.getLogger("anogpt.prosody_analyzer")

# Vérification optionnelle de PyWorld
try:
    import pyworld as pw
    _PYWORLD_AVAILABLE = True
except ImportError:
    _PYWORLD_AVAILABLE = False


# ─────────────────────────────────────────────────────────────────────────────
# Structures de données
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AcousticFeatures:
    """Mesures acoustiques extraites d'une trame ou d'un énoncé audio 16 kHz."""
    duration_s: float
    rms_dbfs: float
    dynamic_range_db: float
    syllables_per_second: float
    f0_mean: float
    f0_median: float
    f0_min: float
    f0_max: float
    pitch_variation: float          # std(F0) / mean(F0) sur les trames voisées
    mean_zcr: float                 # Zero Crossing Rate moyen
    voicing_ratio: float            # Ratio de trames voisées (0.0 à 1.0)
    whisper_score: float            # Indice de chuchotement (0.0 à 1.0)
    spectral_ratio: float           # Ratio énergie HF (>2kHz) / LF (<2kHz)


@dataclass(frozen=True)
class TTSModulation:
    """Paramètres acoustiques modulés pour le moteur TTS local."""
    state: str
    speed_factor: float             # multiplicateur de vitesse (ex: 0.85, 1.20)
    volume_factor: float            # multiplicateur de volume linéaire (ex: 0.65, 1.05)
    tts_rate: str                   # paramètre chaîne (ex: "-15%", "+20%")
    tts_volume: str                 # paramètre chaîne (ex: "-30%", "+5%")
    tts_pitch: str                  # paramètre chaîne (ex: "-2Hz", "+2Hz")


@dataclass
class MoodClassification:
    """Résultat de classification d'humeur avec niveau de confiance et historique."""
    state: str                      # 'calme' | 'agacé/pressé' | 'chuchoté/nuit' | 'fatigué' | 'neutre'
    confidence: float
    probabilities: Dict[str, float]
    features: AcousticFeatures
    is_night: bool = False
    smoothed: bool = False
    timestamp: float = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Constantes & Profils
# ─────────────────────────────────────────────────────────────────────────────

QUALIFIED_MOODS: Tuple[str, ...] = (
    "calme",
    "agacé/pressé",
    "chuchoté/nuit",
    "fatigué",
    "neutre",
)

MOOD_SYSTEM_INSTRUCTIONS: Dict[str, str] = {
    "chuchoté/nuit": (
        "[Contexte : Utilisateur chuchote la nuit. Réponds doucement, de manière très courte et apaisante.]"
    ),
    "agacé/pressé": (
        "[Contexte : Utilisateur agacé ou pressé. Réponds immédiatement, va droit au but, "
        "de manière très concise, directe et efficace, sans bavardage.]"
    ),
    "fatigué": (
        "[Contexte : Utilisateur fatigué. Parle plus doucement, avec bienveillance, "
        "posément et simplement, sans phrases complexes.]"
    ),
    "calme": (
        "[Contexte : Utilisateur calme et posé. Réponds avec une voix sereine, posée, "
        "naturelle et équilibrée.]"
    ),
    "neutre": (
        "[Contexte : Conversation standard. Réponds de façon concise, naturelle, "
        "professionnelle et efficace.]"
    ),
}

TTS_MODULATIONS: Dict[str, TTSModulation] = {
    "chuchoté/nuit": TTSModulation(
        state="chuchoté/nuit",
        speed_factor=0.85,
        volume_factor=0.65,
        tts_rate="-15%",
        tts_volume="-30%",
        tts_pitch="-2Hz",
    ),
    "agacé/pressé": TTSModulation(
        state="agacé/pressé",
        speed_factor=1.20,
        volume_factor=1.05,
        tts_rate="+20%",
        tts_volume="+5%",
        tts_pitch="+2Hz",
    ),
    "fatigué": TTSModulation(
        state="fatigué",
        speed_factor=0.85,
        volume_factor=0.85,
        tts_rate="-15%",
        tts_volume="-15%",
        tts_pitch="-2Hz",
    ),
    "calme": TTSModulation(
        state="calme",
        speed_factor=0.95,
        volume_factor=0.90,
        tts_rate="-5%",
        tts_volume="-10%",
        tts_pitch="-1Hz",
    ),
    "neutre": TTSModulation(
        state="neutre",
        speed_factor=1.00,
        volume_factor=1.00,
        tts_rate="+0%",
        tts_volume="+0%",
        tts_pitch="+0Hz",
    ),
}


# ─────────────────────────────────────────────────────────────────────────────
# Algorithme Yin pour F0
# ─────────────────────────────────────────────────────────────────────────────

def compute_yin_pitch(
    frame: np.ndarray,
    search_window: np.ndarray,
    sample_rate: int = 16000,
    min_f0: float = 60.0,
    max_f0: float = 500.0,
    threshold: float = 0.15,
) -> float:
    """Calcule la fréquence fondamentale F0 d'une trame audio via l'algorithme YIN.

    Étapes :
    1. Fonction de différence quadratique d(tau)
    2. Fonction de différence cumulée normalisée d'(tau)
    3. Seuil absolu
    4. Interpolation parabolique du minimum
    """
    frame_len = len(frame)
    tau_min = max(1, int(sample_rate / max_f0))
    tau_max = min(len(search_window) - frame_len - 1, int(sample_rate / min_f0))
    if tau_max <= tau_min or len(search_window) < frame_len + tau_max + 1:
        return 0.0

    # 1. Calcul rapide de d(tau) par FFT et sommes cumulées
    x = search_window[: frame_len + tau_max + 1]
    w = frame

    x2 = x ** 2
    cum_x2 = np.pad(np.cumsum(x2), (1, 0))
    terms2 = cum_x2[frame_len : frame_len + tau_max + 1] - cum_x2[0 : tau_max + 1]
    term0 = float(np.sum(w ** 2))

    # Corrélation croisée par FFT (zéro-paddée à la puissance de 2 suivante)
    nfft = 1
    while nfft < frame_len + tau_max + 1:
        nfft <<= 1
    X = np.fft.rfft(x, n=nfft)
    W = np.fft.rfft(w[::-1], n=nfft)
    corr = np.fft.irfft(X * W, n=nfft)[frame_len - 1 : frame_len + tau_max]

    d = np.maximum(0.0, term0 + terms2[: len(corr)] - 2.0 * corr)
    d[0] = 0.0

    # 2. Cumulative mean normalized difference function (CMNDF)
    cum_d = np.cumsum(d)
    denom = cum_d[1:] / np.arange(1, len(cum_d))
    cmndf = np.ones_like(d)
    nonzero = denom > 1e-12
    cmndf[1:][nonzero] = d[1:][nonzero] / denom[nonzero]

    # 3. Seuil absolu
    tau_chosen: Optional[int] = None
    for tau in range(tau_min, min(tau_max, len(cmndf) - 1)):
        if cmndf[tau] < threshold:
            while tau + 1 < tau_max and cmndf[tau + 1] < cmndf[tau]:
                tau += 1
            tau_chosen = tau
            break

    # Si aucun creux ne passe sous le seuil, recherche du minimum global sous seuil de repli
    if tau_chosen is None and tau_max > tau_min:
        sub_region = cmndf[tau_min:tau_max]
        if len(sub_region) > 0:
            min_rel = int(np.argmin(sub_region))
            if sub_region[min_rel] < 0.35:
                tau_chosen = tau_min + min_rel

    if tau_chosen is None or tau_chosen <= 1 or tau_chosen >= len(cmndf) - 1:
        return 0.0

    # 4. Interpolation parabolique
    alpha = cmndf[tau_chosen - 1]
    beta = cmndf[tau_chosen]
    gamma = cmndf[tau_chosen + 1]
    denom_parab = 2.0 * (alpha - 2.0 * beta + gamma)
    delta = (alpha - gamma) / denom_parab if abs(denom_parab) > 1e-8 else 0.0
    refined_tau = tau_chosen + delta
    if refined_tau <= 0:
        return 0.0

    f0 = sample_rate / refined_tau
    return float(f0) if (min_f0 * 0.9 <= f0 <= max_f0 * 1.1) else 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Filtrage temporel glissant (Sliding Window & EMA)
# ─────────────────────────────────────────────────────────────────────────────

class TemporalMoodFilter:
    """Filtre temporel glissant avec inertie et hystérésis.

    Évite les basculements intempestifs de l'assistant sur un mot isolé,
    un toussotement ou une hésitation brève.
    """

    def __init__(
        self,
        history_size: int = 5,
        alpha_ema: float = 0.45,
        hysteresis_margin: float = 0.10,
        min_consecutive_switches: int = 2,
    ):
        self._history_size = max(2, history_size)
        self._alpha = max(0.05, min(0.95, alpha_ema))
        self._hysteresis_margin = hysteresis_margin
        self._min_consecutive_switches = min_consecutive_switches

        self._history: List[MoodClassification] = []
        self._smoothed_probs: Dict[str, float] = {m: 0.20 for m in QUALIFIED_MOODS}
        self._smoothed_probs["neutre"] = 0.60
        self._current_state: str = "neutre"
        self._candidate_state: str = "neutre"
        self._candidate_count: int = 0

    @property
    def current_state(self) -> str:
        return self._current_state

    @property
    def smoothed_probabilities(self) -> Dict[str, float]:
        return dict(self._smoothed_probs)

    def reset(self, initial_state: str = "neutre") -> None:
        """Réinitialise l'état interne du filtre."""
        state = initial_state if initial_state in QUALIFIED_MOODS else "neutre"
        self._history.clear()
        self._smoothed_probs = {m: 0.05 for m in QUALIFIED_MOODS}
        self._smoothed_probs[state] = 0.80
        self._current_state = state
        self._candidate_state = state
        self._candidate_count = 0

    def update(self, instant: MoodClassification) -> MoodClassification:
        """Met à jour le filtre avec une nouvelle classification instantanée."""
        self._history.append(instant)
        if len(self._history) > self._history_size:
            self._history.pop(0)

        # 1. Lissage exponentiel des probabilités (EMA)
        norm_sum = 0.0
        for m in QUALIFIED_MOODS:
            inst_p = instant.probabilities.get(m, 0.0)
            self._smoothed_probs[m] = self._alpha * inst_p + (1.0 - self._alpha) * self._smoothed_probs.get(m, 0.0)
            norm_sum += self._smoothed_probs[m]

        if norm_sum > 0:
            for m in QUALIFIED_MOODS:
                self._smoothed_probs[m] /= norm_sum

        # 2. Détermination de l'état candidat
        best_candidate = max(self._smoothed_probs.items(), key=lambda kv: kv[1])[0]
        best_prob = self._smoothed_probs[best_candidate]
        current_prob = self._smoothed_probs.get(self._current_state, 0.0)

        # 3. Mécanisme d'hystérésis : basculement uniquement si confirmé
        # Exception réactive : une requête réellement pressée ne doit pas
        # attendre deux tours. On se base sur la probabilité de cette classe,
        # plutôt que sur la confiance normalisée globale (qui est forcément
        # diluée entre les cinq humeurs).
        is_high_urgency = (
            best_candidate == "agacé/pressé"
            and instant.probabilities.get("agacé/pressé", 0.0) >= 0.55
        )

        if best_candidate == self._candidate_state:
            self._candidate_count += 1
        else:
            self._candidate_state = best_candidate
            self._candidate_count = 1

        should_switch = False
        if best_candidate == self._current_state:
            self._candidate_count = 0
        elif is_high_urgency:
            should_switch = True
        elif (
            best_prob >= current_prob + self._hysteresis_margin
            and self._candidate_count >= self._min_consecutive_switches
        ):
            should_switch = True
        elif self._candidate_count >= self._history_size // 2 + 1 and best_prob > current_prob:
            should_switch = True

        if should_switch:
            self._current_state = best_candidate
            self._candidate_count = 0

        final_confidence = self._smoothed_probs.get(self._current_state, best_prob)
        return MoodClassification(
            state=self._current_state,
            confidence=float(final_confidence),
            probabilities=dict(self._smoothed_probs),
            features=instant.features,
            is_night=instant.is_night,
            smoothed=True,
            timestamp=instant.timestamp,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Analyseur Prosodique Principal
# ─────────────────────────────────────────────────────────────────────────────

class ProsodyAnalyzer:
    """Analyseur acoustique temps réel et classifieur d'humeur vocale."""

    def __init__(
        self,
        sample_rate: int = 16000,
        pitch_method: str = "yin",     # 'yin' ou 'pyworld'
        filter_history_size: int = 5,
    ):
        self._sample_rate = sample_rate
        self._pitch_method = pitch_method
        self._filter = TemporalMoodFilter(history_size=filter_history_size)
        self._last_features: Optional[AcousticFeatures] = None
        self._last_instant_classification: Optional[MoodClassification] = None
        self._last_classification: Optional[MoodClassification] = None

    @property
    def last_classification(self) -> Optional[MoodClassification]:
        return self._last_classification

    @property
    def last_instant_classification(self) -> Optional[MoodClassification]:
        """Dernière lecture brute, avant le lissage temporel."""
        return self._last_instant_classification

    @property
    def current_mood(self) -> str:
        return self._filter.current_state

    # ── Normalisation du signal audio ────────────────────────────────────────

    @staticmethod
    def _to_float32(samples: Union[np.ndarray, bytes, list]) -> np.ndarray:
        """Convertit l'entrée (PCM 16 bits bytes ou int16/float32) en ndarray float32 normalisé [-1.0, 1.0]."""
        if isinstance(samples, bytes):
            audio = np.frombuffer(samples, dtype=np.int16).astype(np.float32) / 32768.0
            return audio
        arr = np.asarray(samples)
        if arr.dtype == np.int16:
            return (arr.astype(np.float32) / 32768.0).reshape(-1)
        if arr.dtype in (np.float32, np.float64):
            return arr.astype(np.float32).reshape(-1)
        return arr.astype(np.float32).reshape(-1)

    # ── Extraction des caractéristiques acoustiques ──────────────────────────

    def extract_features(
        self,
        samples: Union[np.ndarray, bytes],
        sample_rate: Optional[int] = None,
    ) -> AcousticFeatures:
        """Extrait l'ensemble des descripteurs acoustiques sur le signal 16 kHz :

        - Hauteur fondamentale F0 (Yin ou PyWorld)
        - Volume RMS et dynamique d'amplitude (dBFS, dynamic range)
        - Débit de parole (syllabes par seconde via pics d'énergie)
        - Taux de voisement et indicateurs de chuchotement (ZCR, HF/LF spectral)
        """
        sr = sample_rate or self._sample_rate
        audio = self._to_float32(samples)

        duration = float(audio.size / max(1, sr))
        if audio.size < int(sr * 0.15):  # Signal trop court (< 150 ms)
            return AcousticFeatures(
                duration_s=duration,
                rms_dbfs=-120.0,
                dynamic_range_db=0.0,
                syllables_per_second=0.0,
                f0_mean=0.0,
                f0_median=0.0,
                f0_min=0.0,
                f0_max=0.0,
                pitch_variation=0.0,
                mean_zcr=0.0,
                voicing_ratio=0.0,
                whisper_score=0.0,
                spectral_ratio=0.0,
            )

        # Suppression composante continue
        audio = np.clip(audio - float(np.mean(audio)), -1.0, 1.0)

        # Découpage en trames (ex: 32 ms frame, 16 ms hop)
        frame_len = 512
        hop_len = 256
        if audio.size < frame_len:
            frame_len = max(128, audio.size)
            hop_len = max(64, frame_len // 2)

        num_frames = (audio.size - frame_len) // hop_len + 1
        frames = np.lib.stride_tricks.as_strided(
            audio,
            shape=(num_frames, frame_len),
            strides=(audio.strides[0] * hop_len, audio.strides[0]),
            writeable=False,
        )

        # 1. Énergie RMS par trame et globale
        frame_rms = np.sqrt(np.mean(frames * frames, axis=1) + 1e-12)
        noise_floor = max(0.003, float(np.percentile(frame_rms, 15)) * 1.5)
        active_mask = frame_rms >= noise_floor
        if not np.any(active_mask):
            active_mask = np.ones(num_frames, dtype=bool)

        active_rms = frame_rms[active_mask]
        mean_rms = float(np.mean(active_rms)) if active_rms.size else 1e-6
        rms_dbfs = float(20.0 * np.log10(max(1e-6, mean_rms)))

        # Dynamique d'amplitude (écart percentile 90 vs 10)
        p90 = float(np.percentile(frame_rms, 90))
        p10 = float(np.percentile(frame_rms, 10))
        dynamic_range_db = float(20.0 * np.log10(max(1e-6, p90) / max(1e-6, p10)))

        # 2. Débit de parole (syllabes par seconde via pics d'énergie)
        smooth_len = min(5, len(frame_rms))
        if smooth_len > 1:
            smooth = np.convolve(frame_rms, np.ones(smooth_len, dtype=np.float32) / float(smooth_len), mode="same")
        else:
            smooth = frame_rms.copy()

        peak_floor = max(noise_floor * 1.25, float(np.median(smooth)) * 1.08)
        cand_peaks = np.flatnonzero(
            (smooth[1:-1] > smooth[:-2]) & (smooth[1:-1] >= smooth[2:]) & (smooth[1:-1] >= peak_floor)
        ) + 1

        # Réfraction physiologique syllabique (~100 ms minimum entre deux noyaux)
        min_gap = max(1, round(0.10 / (hop_len / sr)))
        peaks: List[int] = []
        for idx in cand_peaks:
            if not peaks or idx - peaks[-1] >= min_gap:
                peaks.append(int(idx))
            elif smooth[idx] > smooth[peaks[-1]]:
                peaks[-1] = int(idx)

        syllables_per_second = float(len(peaks) / max(0.35, duration))

        # 3. Taux de passage par zéro (ZCR)
        signs = np.sign(frames)
        zcr_frames = np.mean(np.abs(signs[:, 1:] - signs[:, :-1]) > 0, axis=1) / 2.0
        mean_zcr = float(np.mean(zcr_frames[active_mask])) if np.any(active_mask) else float(np.mean(zcr_frames))

        # 4. Équilibre spectral HF/LF
        spec = np.abs(np.fft.rfft(frames, axis=1))
        freqs = np.fft.rfftfreq(frame_len, 1.0 / sr)
        high_mask = freqs >= 2000.0
        low_mask = (freqs < 2000.0) & (freqs >= 100.0)

        hf_energy = np.sum(spec[:, high_mask] ** 2, axis=1)
        lf_energy = np.sum(spec[:, low_mask] ** 2, axis=1) + 1e-12
        spec_ratios = hf_energy / lf_energy
        spectral_ratio = float(np.mean(spec_ratios[active_mask])) if np.any(active_mask) else 0.0

        # 5. Extraction F0 (PyWorld ou Yin)
        pitches: List[float] = []
        use_pyworld = (self._pitch_method == "pyworld" and _PYWORLD_AVAILABLE)

        if use_pyworld:
            try:
                _f0, _time = pw.dio(audio.astype(np.float64), sr, frame_period=hop_len / sr * 1000.0)
                _f0 = pw.stonemask(audio.astype(np.float64), _f0, _time, sr)
                voiced_pw = _f0[_f0 > 0]
                pitches = [float(p) for p in voiced_pw if 60.0 <= p <= 500.0]
            except Exception as e:
                logger.debug("Échec PyWorld F0, bascule sur Yin: %s", e)
                use_pyworld = False

        if not use_pyworld:
            tau_max = int(sr / 60.0)  # ~266 à 16kHz
            active_indices = np.flatnonzero(active_mask)
            step = max(1, len(active_indices) // 40)
            target_indices = active_indices[::step]

            for idx in target_indices:
                offset = idx * hop_len
                search_region = audio[offset : offset + frame_len + tau_max + 1]
                if search_region.size < frame_len + 32:
                    continue
                w_frame = search_region[:frame_len]
                f0_val = compute_yin_pitch(w_frame, search_region, sample_rate=sr)
                if f0_val > 0.0:
                    pitches.append(f0_val)

        # Statistiques F0
        if pitches:
            f0_mean = float(np.mean(pitches))
            f0_median = float(np.median(pitches))
            f0_min = float(np.min(pitches))
            f0_max = float(np.max(pitches))
            f0_std = float(np.std(pitches)) if len(pitches) >= 2 else 0.0
            pitch_variation = float(f0_std / max(1.0, f0_mean))
            voicing_ratio = float(min(1.0, len(pitches) / max(1, len(target_indices) if not use_pyworld else len(_f0))))
        else:
            f0_mean = 0.0
            f0_median = 0.0
            f0_min = 0.0
            f0_max = 0.0
            pitch_variation = 0.0
            voicing_ratio = 0.0

        # 6. Score composite de chuchotement (Whisper Score)
        whisper_voicing = max(0.0, 1.0 - voicing_ratio * 1.5)
        whisper_zcr = min(1.0, mean_zcr / 0.18)
        whisper_spec = min(1.0, spectral_ratio / 1.2)
        whisper_energy = 1.0 if rms_dbfs < -28.0 else max(0.0, (-rms_dbfs - 16.0) / 16.0)

        whisper_score = float(
            np.clip(
                0.35 * whisper_voicing
                + 0.30 * whisper_zcr
                + 0.20 * whisper_spec
                + 0.15 * whisper_energy,
                0.0,
                1.0,
            )
        )

        features = AcousticFeatures(
            duration_s=duration,
            rms_dbfs=rms_dbfs,
            dynamic_range_db=dynamic_range_db,
            syllables_per_second=syllables_per_second,
            f0_mean=f0_mean,
            f0_median=f0_median,
            f0_min=f0_min,
            f0_max=f0_max,
            pitch_variation=pitch_variation,
            mean_zcr=mean_zcr,
            voicing_ratio=voicing_ratio,
            whisper_score=whisper_score,
            spectral_ratio=spectral_ratio,
        )
        self._last_features = features
        return features

    # ── Classification instantanée ───────────────────────────────────────────

    def classify_instant(
        self,
        features: AcousticFeatures,
        is_night: bool = False,
    ) -> MoodClassification:
        """Classifie l'humeur instantanée sur la base des indices acoustiques extraits."""
        # Repli direct pour signal trop court ou silence absolu
        if features.duration_s < 0.15 or features.rms_dbfs <= -80.0:
            import time
            return MoodClassification(
                state="neutre",
                confidence=0.60,
                probabilities={m: (0.80 if m == "neutre" else 0.05) for m in QUALIFIED_MOODS},
                features=features,
                is_night=is_night,
                smoothed=False,
                timestamp=time.monotonic(),
            )

        scores: Dict[str, float] = {m: 0.05 for m in QUALIFIED_MOODS}

        rate = features.syllables_per_second
        rms = features.rms_dbfs
        dyn = features.dynamic_range_db
        p_var = features.pitch_variation
        f0 = features.f0_mean
        whisper = features.whisper_score
        voicing = features.voicing_ratio

        # 1. Chuchoté / Nuit
        if whisper >= 0.45:
            scores["chuchoté/nuit"] = 0.75 + 0.25 * whisper
        elif is_night and (rms <= -25.0 or whisper >= 0.30 or voicing <= 0.40):
            scores["chuchoté/nuit"] = 0.70 + 0.20 * max(0.0, (-rms - 25.0) / 20.0)
        elif whisper >= 0.35 and rms <= -28.0:
            scores["chuchoté/nuit"] = 0.65

        # 2. Agacé / Pressé
        if rate >= 4.5 and rms >= -22.0:
            scores["agacé/pressé"] = 0.85
        elif rate >= 4.2 and (rms >= -22.0 or p_var >= 0.10 or dyn >= 16.0):
            scores["agacé/pressé"] = 0.78
        elif rate >= 4.8:
            scores["agacé/pressé"] = 0.72

        # 3. Fatigué
        if rate <= 2.2 and rms <= -26.0:
            scores["fatigué"] = 0.80 if (p_var <= 0.08 or is_night) else 0.70
        elif rate <= 2.0 and rms <= -24.0:
            scores["fatigué"] = 0.65

        # 4. Calme
        if 2.0 <= rate <= 3.0 and -34.0 <= rms <= -22.0 and p_var <= 0.08:
            scores["calme"] = 0.75
        elif 2.0 <= rate <= 3.0 and -32.0 <= rms <= -22.0:
            scores["calme"] = 0.65

        # 5. Neutre
        if 2.8 <= rate <= 4.2 and -24.0 <= rms <= -14.0:
            scores["neutre"] = 0.75
        elif not any(s >= 0.65 for s in scores.values()):
            scores["neutre"] = 0.60

        # Normalisation Softmax / somme
        total = sum(scores.values())
        probabilities = {m: scores[m] / total for m in QUALIFIED_MOODS}

        best_state = max(probabilities.items(), key=lambda kv: kv[1])[0]
        confidence = probabilities[best_state]

        import time
        return MoodClassification(
            state=best_state,
            confidence=float(confidence),
            probabilities=probabilities,
            features=features,
            is_night=is_night,
            smoothed=False,
            timestamp=time.monotonic(),
        )

    # ── Analyse globale avec filtrage temporel ────────────────────────────────

    def analyze(
        self,
        samples: Union[np.ndarray, bytes],
        sample_rate: Optional[int] = None,
        *,
        is_night: Optional[bool] = None,
    ) -> MoodClassification:
        """Analyse complète : extraction de descripteurs + classification + lissage temporel."""
        sr = sample_rate or self._sample_rate
        if is_night is None:
            hour = datetime.now().hour
            night = (hour >= 21 or hour < 7)
        else:
            night = is_night

        features = self.extract_features(samples, sample_rate=sr)
        instant = self.classify_instant(features, is_night=night)
        self._last_instant_classification = instant
        smoothed = self._filter.update(instant)
        self._last_classification = smoothed
        return smoothed

    # ── Helpers d'adaptation Jarvis ───────────────────────────────────────────

    def get_gemini_instruction(self, state: Optional[str] = None) -> str:
        """Retourne la directive système à injecter dans le WebSocket Gemini Live."""
        st = state or self._filter.current_state
        return MOOD_SYSTEM_INSTRUCTIONS.get(st, MOOD_SYSTEM_INSTRUCTIONS["neutre"])

    def get_tts_modulation(self, state: Optional[str] = None) -> TTSModulation:
        """Retourne la modulation de vitesse, volume et pitch pour le TTS local."""
        st = state or self._filter.current_state
        return TTS_MODULATIONS.get(st, TTS_MODULATIONS["neutre"])

    def apply_tts_modulation(
        self,
        tts_config: Dict[str, Any],
        state: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Applique la modulation prosodique à un dictionnaire de configuration TTS."""
        mod = self.get_tts_modulation(state)
        updated = dict(tts_config)
        updated["tts_rate"] = mod.tts_rate
        updated["tts_pitch"] = mod.tts_pitch
        updated["tts_volume"] = mod.tts_volume
        base_speed = float(tts_config.get("tts_speed", 1.0))
        updated["tts_speed"] = base_speed * mod.speed_factor
        return updated

    def reset_temporal_filter(self, initial_state: str = "neutre") -> None:
        """Réinitialise la mémoire temporelle du filtre."""
        self._filter.reset(initial_state=initial_state)


# ─────────────────────────────────────────────────────────────────────────────
# Instance globale & Fonctions d'accès
# ─────────────────────────────────────────────────────────────────────────────

_global_analyzer = ProsodyAnalyzer()


def get_prosody_analyzer() -> ProsodyAnalyzer:
    """Retourne l'instance globale de ProsodyAnalyzer."""
    return _global_analyzer


def analyze_user_speech(
    samples: Union[np.ndarray, bytes],
    sample_rate: int = 16000,
    *,
    is_night: Optional[bool] = None,
) -> MoodClassification:
    """Fonction utilitaire rapide pour analyser un signal vocal."""
    return _global_analyzer.analyze(samples, sample_rate, is_night=is_night)


def build_prosody_prompt(state: str) -> str:
    """Construit la consigne pour Gemini Live selon l'humeur spécifiée."""
    return MOOD_SYSTEM_INSTRUCTIONS.get(state, MOOD_SYSTEM_INSTRUCTIONS["neutre"])


def apply_prosody_tts(config: Dict[str, Any], state: str) -> Dict[str, Any]:
    """Applique les réglages de vitesse et de volume TTS selon l'état."""
    return _global_analyzer.apply_tts_modulation(config, state)
