"""Module de Prosodie Adaptative pour ANO-GPT.

Adapte dynamiquement le débit, le ton, l'intonation et la concision de l'assistant
selon le contexte réel :
1. Urgence / Alerte : débit vif (+15-20%), ton direct, phrases courtes et percutantes.
2. Soirée / Nuit (21h-07h) : débit posé (-10-15%), ton calme, intonation feutrée.
3. Répétition / Récidive : réponse ultra-courte (2-5 mots), sans fioritures.
4. Focus / Travail (Code, Terminal) : ton technique, précis, concis.
5. Standard : style JARVIS équilibré et naturel.

Ce module pilote à la fois les consignes stylistiques pour Gemini Live et les
paramètres acoustiques (vitesse, pitch, volume) pour les moteurs TTS.
"""

from __future__ import annotations

import logging
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from core.prosody_analyzer import (
    MoodClassification,
    ProsodyAnalyzer,
    get_prosody_analyzer,
)

logger = logging.getLogger("anogpt.prosody")


@dataclass(frozen=True)
class ProsodyProfile:
    mode: str                  # "urgent" | "calm_night" | "concise_repeat" | "focus" | "standard"
    speed_factor: float        # 1.0 = normal, 1.15 = rapide, 0.9 = posé
    tts_rate: str              # e.g. "+15%", "-10%", "+0%"
    tts_pitch: str             # e.g. "+0Hz", "-2Hz"
    tts_volume: str            # e.g. "+0%", "-15%"
    prompt_instruction: str    # consigne vocale pour le modèle


@dataclass(frozen=True)
class AcousticFeatures:
    duration_s: float
    rms_dbfs: float
    voiced_ratio: float
    syllables_per_second: float
    pitch_hz: float
    pitch_variation: float


@dataclass(frozen=True)
class AcousticAssessment:
    state: str                 # urgent | focused | enthusiastic | tired | neutral
    confidence: float
    features: AcousticFeatures


PROSODY_PROFILES: Dict[str, ProsodyProfile] = {
    "urgent": ProsodyProfile(
        mode="urgent",
        speed_factor=1.18,
        tts_rate="+18%",
        tts_pitch="+1Hz",
        tts_volume="+5%",
        prompt_instruction=(
            "TON URGENT & INCISIF : Débit vif, réponse directe et sans préliminaires. "
            "Va droit au but en une seule phrase courte."
        ),
    ),
    "calm_night": ProsodyProfile(
        mode="calm_night",
        speed_factor=0.88,
        tts_rate="-12%",
        tts_pitch="-2Hz",
        tts_volume="-15%",
        prompt_instruction=(
            "TON NOCTURNE & POSÉ : C'est le soir/la nuit. Voix calme, posée, "
            "douce et feutrée. Évite toute exclamation vive."
        ),
    ),
    "concise_repeat": ProsodyProfile(
        mode="concise_repeat",
        speed_factor=1.12,
        tts_rate="+12%",
        tts_pitch="+0Hz",
        tts_volume="+0%",
        prompt_instruction=(
            "RÉPÉTITION / CONFIRMATION : Réponse ultra-courte (2 à 5 mots max). "
            "Inutile de réexpliquer, confirme directement l'essentiel."
        ),
    ),
    "focus": ProsodyProfile(
        mode="focus",
        speed_factor=1.05,
        tts_rate="+5%",
        tts_pitch="+0Hz",
        tts_volume="+0%",
        prompt_instruction=(
            "TON TRAVAIL & FOCUS : Précis, net, technique et sobre. Pas de bavardage."
        ),
    ),
    "enthusiastic": ProsodyProfile(
        mode="enthusiastic",
        speed_factor=1.08,
        tts_rate="+8%",
        tts_pitch="+1Hz",
        tts_volume="+3%",
        prompt_instruction=(
            "TON ENTHOUSIASTE : Énergie chaleureuse, rythme vivant et intonation "
            "expressive, sans devenir théâtral ni bavard."
        ),
    ),
    "tired": ProsodyProfile(
        mode="tired",
        speed_factor=0.86,
        tts_rate="-14%",
        tts_pitch="-2Hz",
        tts_volume="-10%",
        prompt_instruction=(
            "TON FATIGUÉ ACCOMPAGNÉ : L'utilisateur semble fatigué. Parle plus "
            "lentement, doucement et avec bienveillance. Simplifie sans infantiliser."
        ),
    ),
    "standard": ProsodyProfile(
        mode="standard",
        speed_factor=1.0,
        tts_rate="+0%",
        tts_pitch="+0Hz",
        tts_volume="+0%",
        prompt_instruction=(
            "TON STANDARD JARVIS : Naturel, professionnel, équilibré et chaleureux."
        ),
    ),
}

URGENT_KEYWORDS = {
    "urgent", "urgence", "vite", "rapidement", "alerte", "danger",
    "critique", "bloque", "panique", "secours", "alarme", "immédiat",
    "attention", "sauve", "brûle",
}

FOCUS_APP_CLASSES = {
    "kitty", "foot", "xterm", "code", "vscodium", "subl", "kate",
    "neovim", "nvim", "alacritty", "wezterm", "terminal",
}

STYLE_INSTRUCTIONS = {
    "professional": (
        "STYLE PROFESSIONNEL : précis, naturel, assuré et chaleureux."
    ),
    "stark": (
        "STYLE TONY STARK : assurance élégante et pointe d'ironie intelligente, "
        "jamais moqueuse en cas d'urgence, de fatigue ou de sujet sensible."
    ),
    "synthetic": (
        "STYLE ULTRA-SYNTHÉTIQUE : résultat d'abord, une phrase si possible, "
        "aucune formule ou explication non demandée."
    ),
}

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "api_keys.json"


class AcousticProsodyAnalyzer:
    """Mesure hors-ligne débit, énergie et hauteur sur du PCM 16 kHz.

    L'analyse ne prétend pas diagnostiquer une émotion. Elle classe uniquement
    des indices d'élocution utiles au dialogue, avec des seuils conservateurs.
    """

    def analyze(self, samples: np.ndarray, sample_rate: int = 16000,
                *, night: bool = False) -> AcousticAssessment:
        audio = np.asarray(samples, dtype=np.float32).reshape(-1)
        if audio.size < int(sample_rate * 0.25):
            return self._neutral(audio.size / max(1, sample_rate))
        audio = np.clip(audio - float(np.mean(audio)), -1.0, 1.0)
        frame_len = max(160, int(sample_rate * 0.04))
        hop = max(80, int(sample_rate * 0.02))
        if audio.size < frame_len:
            return self._neutral(audio.size / max(1, sample_rate))
        count = 1 + (audio.size - frame_len) // hop
        frames = np.lib.stride_tricks.as_strided(
            audio,
            shape=(count, frame_len),
            strides=(audio.strides[0] * hop, audio.strides[0]),
            writeable=False,
        )
        rms = np.sqrt(np.mean(frames * frames, axis=1) + 1e-12)
        noise = max(0.004, float(np.percentile(rms, 20)) * 1.6)
        voiced = rms >= noise
        voiced_rms = rms[voiced]
        median_rms = float(np.median(voiced_rms)) if voiced_rms.size else 1e-6
        rms_dbfs = float(20.0 * np.log10(max(1e-6, median_rms)))

        # Les noyaux syllabiques suivent les maxima de l'enveloppe avec au
        # moins 100 ms d'écart. C'est indépendant de la transcription.
        smooth = np.convolve(rms, np.ones(3, dtype=np.float32) / 3.0, mode="same")
        peak_floor = max(noise * 1.25, float(np.median(smooth)) * 1.12)
        candidates = np.flatnonzero(
            (smooth[1:-1] > smooth[:-2])
            & (smooth[1:-1] >= smooth[2:])
            & (smooth[1:-1] >= peak_floor)
        ) + 1
        min_gap = max(1, round(0.10 / (hop / sample_rate)))
        peaks: list[int] = []
        for index in candidates.tolist():
            if not peaks or index - peaks[-1] >= min_gap:
                peaks.append(index)
            elif smooth[index] > smooth[peaks[-1]]:
                peaks[-1] = index
        duration = audio.size / sample_rate
        syllable_rate = len(peaks) / max(0.35, duration)

        pitches: list[float] = []
        voiced_indices = np.flatnonzero(voiced)
        if voiced_indices.size:
            step = max(1, voiced_indices.size // 24)
            min_lag = max(1, sample_rate // 400)
            max_lag = min(frame_len - 2, sample_rate // 65)
            for index in voiced_indices[::step][:24]:
                frame = frames[index] * np.hanning(frame_len)
                corr = np.correlate(frame, frame, mode="full")[frame_len - 1:]
                if corr[0] <= 1e-8:
                    continue
                region = corr[min_lag:max_lag]
                lag = int(np.argmax(region)) + min_lag
                if corr[lag] / corr[0] >= 0.30:
                    pitches.append(sample_rate / lag)
        pitch = float(np.median(pitches)) if pitches else 0.0
        pitch_variation = (
            float(np.std(pitches) / max(1.0, pitch)) if len(pitches) >= 3 else 0.0
        )
        features = AcousticFeatures(
            duration_s=duration,
            rms_dbfs=rms_dbfs,
            voiced_ratio=float(np.mean(voiced)),
            syllables_per_second=syllable_rate,
            pitch_hz=pitch,
            pitch_variation=pitch_variation,
        )

        # Un pré-roll silencieux, une coupure micro ou quelques bruits de fond
        # ne sont pas une voix fatiguée. Ce garde-fou évite une modulation TTS
        # douce injustifiée avant même qu'une parole exploitable soit présente.
        if rms_dbfs <= -60.0 or features.voiced_ratio < 0.12:
            return AcousticAssessment("neutral", 0.0, features)

        fast = syllable_rate >= 4.2
        energetic = rms_dbfs >= -24.0
        expressive = pitch_variation >= 0.13
        if fast and (energetic or expressive):
            state, confidence = "urgent", 0.82
        elif energetic and expressive and syllable_rate >= 2.4:
            state, confidence = "enthusiastic", 0.76
        elif syllable_rate <= 2.1 and rms_dbfs <= (-24.0 if night else -28.0):
            state, confidence = "tired", 0.72
        elif (2.0 <= syllable_rate <= 4.1 and pitch_variation <= 0.10
              and duration >= 0.8):
            state, confidence = "focused", 0.66
        else:
            state, confidence = "neutral", 0.55
        return AcousticAssessment(state, confidence, features)

    @staticmethod
    def _neutral(duration: float) -> AcousticAssessment:
        return AcousticAssessment(
            "neutral", 0.0,
            AcousticFeatures(duration, -120.0, 0.0, 0.0, 0.0, 0.0),
        )


class ProsodyManager:
    """Évalue le contexte en direct pour déterminer le profil de prosodie optimal."""

    def __init__(self, history_window_s: float = 60.0,
                 config_path: str | Path | None = None):
        self._history_window_s = history_window_s
        self._recent_queries: List[Tuple[float, str]] = []
        self._config_path = Path(config_path) if config_path else _CONFIG_PATH
        self._analyzer = AcousticProsodyAnalyzer()
        # L'analyse légère réagit vite ; celle-ci ajoute F0, spectre,
        # chuchotement et hystérésis. Elles ne tournent qu'en fin de tour,
        # jamais dans le callback micro temps réel.
        self._advanced_analyzer: ProsodyAnalyzer = get_prosody_analyzer()
        self._last_acoustic: AcousticAssessment | None = None
        self._last_advanced: MoodClassification | None = None
        self._last_acoustic_at = 0.0

    @property
    def last_acoustic(self) -> AcousticAssessment | None:
        return self._last_acoustic

    @property
    def last_advanced(self) -> MoodClassification | None:
        """Dernière lecture complète, partagée avec la modulation TTS."""
        return self._last_advanced

    def analyze_audio(self, samples: np.ndarray, sample_rate: int = 16000,
                      current_time: Optional[datetime] = None) -> AcousticAssessment:
        assessment = self._analyzer.analyze(
            samples, sample_rate,
            night=self.is_night_time(current_time),
        )
        try:
            advanced = self._advanced_analyzer.analyze(
                samples, sample_rate,
                is_night=self.is_night_time(current_time),
            )
            assessment = self._fuse_assessments(
                assessment,
                advanced,
                self._advanced_analyzer.last_instant_classification,
            )
            self._last_advanced = advanced
        except Exception as exc:
            # La prosodie ne doit jamais retarder la fin du tour vocal.
            logger.debug("Analyse prosodique avancée indisponible: %s", exc)
        self._last_acoustic = assessment
        self._last_acoustic_at = time.monotonic()
        return assessment

    @staticmethod
    def _fuse_assessments(
        baseline: AcousticAssessment,
        smoothed: MoodClassification,
        instant: MoodClassification | None,
    ) -> AcousticAssessment:
        """Allie réactivité sur l'urgence et stabilité du ton sur la durée."""
        direct = instant or smoothed
        state = baseline.state
        confidence = baseline.confidence

        # Une urgence manifeste est immédiatement prise en compte. Les états
        # doux passent par le filtre temporel afin d'éviter les oscillations.
        if baseline.state == "urgent" or (
            direct.state == "agacé/pressé" and direct.confidence >= 0.42
        ):
            state = "urgent"
            confidence = max(confidence, direct.confidence)
        elif (
            direct.state == "chuchoté/nuit" and direct.confidence >= 0.42
        ) or (
            smoothed.state == "chuchoté/nuit" and smoothed.confidence >= 0.38
        ):
            state = "whispered"
            confidence = max(confidence, direct.confidence, smoothed.confidence)
        elif baseline.state == "tired" or (
            smoothed.state == "fatigué" and smoothed.confidence >= 0.42
        ):
            state = "tired"
            confidence = max(confidence, smoothed.confidence)
        elif baseline.state in {"enthusiastic", "focused"}:
            state = baseline.state
        else:
            state = "neutral"

        return AcousticAssessment(state, min(1.0, confidence), baseline.features)

    def preferred_style(self) -> str:
        try:
            config = json.loads(self._config_path.read_text(encoding="utf-8"))
            style = str(config.get("prosody_style") or "professional").strip().lower()
        except Exception:
            style = "professional"
        aliases = {
            "professionnel": "professional", "pro": "professional",
            "tony": "stark", "tony_stark": "stark", "sarcastic": "stark",
            "sarcastique": "stark", "ultra-synthetique": "synthetic",
            "ultra-synthétique": "synthetic", "synthetique": "synthetic",
            "synthétique": "synthetic", "court": "synthetic",
        }
        return aliases.get(style, style) if aliases.get(style, style) in STYLE_INSTRUCTIONS else "professional"

    def set_preferred_style(self, style: str) -> str:
        normalized = str(style or "").strip().lower().replace(" ", "_")
        aliases = {
            "professionnel": "professional", "pro": "professional",
            "tony": "stark", "tony_stark": "stark", "sarcastique": "stark",
            "ultra_synthetique": "synthetic", "ultra-synthétique": "synthetic",
            "synthetique": "synthetic", "synthétique": "synthetic",
        }
        normalized = aliases.get(normalized, normalized)
        if normalized not in STYLE_INSTRUCTIONS:
            raise ValueError("style inconnu : professionnel, Tony Stark ou ultra-synthétique")
        try:
            config = json.loads(self._config_path.read_text(encoding="utf-8"))
            if not isinstance(config, dict):
                config = {}
        except Exception:
            config = {}
        config["prosody_style"] = normalized
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._config_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._config_path)
        labels = {
            "professional": "professionnel",
            "stark": "Tony Stark",
            "synthetic": "ultra-synthétique",
        }
        return labels[normalized]

    def record_user_query(self, text: str) -> None:
        """Enregistre un énoncé pour la détection de répétition."""
        if not text:
            return
        now = time.monotonic()
        normalized = self._normalize_query(text)
        self._recent_queries.append((now, normalized))
        # Nettoyage des requêtes hors de la fenêtre glissante
        cutoff = now - self._history_window_s
        self._recent_queries = [(t, q) for t, q in self._recent_queries if t >= cutoff]

    @staticmethod
    def _normalize_query(text: str) -> str:
        cleaned = re.sub(r"[^\w\s]", "", (text or "").lower().strip())
        return re.sub(r"\s+", " ", cleaned)

    def is_query_repeated(self, text: str) -> bool:
        """Vérifie si la même intention a été demandée récemment."""
        if not text:
            return False
        normalized = self._normalize_query(text)
        now = time.monotonic()
        cutoff = now - self._history_window_s
        # Compte les occurrences récentes similaires
        count = sum(
            1 for t, q in self._recent_queries
            if t >= cutoff and (q == normalized or (len(q) > 4 and q in normalized))
        )
        return count >= 2

    def is_night_time(self, current_time: Optional[datetime] = None) -> bool:
        """Indique si l'heure courante correspond à la soirée ou la nuit (21h - 7h)."""
        dt = current_time or datetime.now()
        hour = dt.hour
        return hour >= 21 or hour < 7

    def is_urgent(self, text: str, system_status: Optional[Dict[str, Any]] = None) -> bool:
        """Détecte une urgence dans la formulation ou l'état matériel."""
        normalized = self._normalize_query(text)
        tokens = set(normalized.split())
        if tokens.intersection(URGENT_KEYWORDS):
            return True

        if system_status:
            # Surchauffe ou batterie critique
            temp = system_status.get("cpu_temp", 0)
            batt_percent = system_status.get("battery_percent", 100)
            batt_plugged = system_status.get("battery_plugged", True)
            if temp >= 88.0:
                return True
            if batt_percent <= 10 and not batt_plugged:
                return True
        return False

    def is_focus_active(self, active_window: str) -> bool:
        """Indique si l'utilisateur est dans une application de développement ou terminal."""
        if not active_window or active_window == "aucune":
            return False
        window_lower = active_window.lower()
        return any(app in window_lower for app in FOCUS_APP_CLASSES)

    def evaluate_profile(
        self,
        query: str = "",
        active_window: str = "",
        current_time: Optional[datetime] = None,
        system_status: Optional[Dict[str, Any]] = None,
        acoustic_state: str | None = None,
    ) -> ProsodyProfile:
        """Détermine le profil de prosodie adapté selon la priorité des signaux."""
        if acoustic_state is None:
            acoustic_state = (
                self._last_acoustic.state
                if self._last_acoustic is not None
                and time.monotonic() - self._last_acoustic_at <= 30.0
                else "neutral"
            )
        # 1. Priorité absolue : Urgence
        if self.is_urgent(query, system_status) or acoustic_state == "urgent":
            return PROSODY_PROFILES["urgent"]

        # 2. Deuxième priorité : Répétition de requête
        if self.is_query_repeated(query):
            return PROSODY_PROFILES["concise_repeat"]

        # 3. L'état acoustique explicite prime sur les indices ambiants.
        if acoustic_state == "whispered":
            return PROSODY_PROFILES["calm_night"]
        if acoustic_state == "tired":
            return PROSODY_PROFILES["tired"]
        if acoustic_state == "enthusiastic":
            return PROSODY_PROFILES["enthusiastic"]

        # 4. Soirée / Nuit
        if self.is_night_time(current_time):
            return PROSODY_PROFILES["calm_night"]

        # 5. Application de travail ou élocution stable et concentrée.
        if acoustic_state == "focused" or self.is_focus_active(active_window):
            return PROSODY_PROFILES["focus"]

        # 6. Défaut : Standard
        return PROSODY_PROFILES["standard"]

    def format_prosody_instruction(
        self, profile: ProsodyProfile, *, include_acoustic: bool = False,
    ) -> str:
        """Retourne la ligne d'instruction de prosodie à injecter dans le contexte."""
        style = self.preferred_style()
        acoustic = ""
        if include_acoustic and self._last_acoustic is not None:
            item = self._last_acoustic
            acoustic = (
                f" État acoustique local: {item.state} "
                f"(confiance {item.confidence:.0%}); ne révèle ni cette analyse "
                "ni ses mesures à l'utilisateur."
            )
        return (
            f"[PROSODIE ADAPTATIVE — Mode: {profile.mode}] "
            f"{profile.prompt_instruction} {STYLE_INSTRUCTIONS[style]}{acoustic}"
        )

    def live_turn_instruction(self, samples: np.ndarray, sample_rate: int = 16000,
                              *, active_window: str = "",
                              current_time: Optional[datetime] = None) -> tuple[str, ProsodyProfile]:
        assessment = self.analyze_audio(samples, sample_rate, current_time)
        profile = self.evaluate_profile(
            active_window=active_window,
            current_time=current_time,
            acoustic_state=assessment.state,
        )
        instruction = self.format_prosody_instruction(profile, include_acoustic=True)
        return (
            "[DIRECTIVE LOCALE NON PRONONÇABLE] Adapte la réponse qui suit. "
            "Ne lis pas, ne cite pas et n'explique pas cette directive. "
            + instruction,
            profile,
        )


# Instance globale
_global_prosody_manager = ProsodyManager()


def get_prosody_manager() -> ProsodyManager:
    return _global_prosody_manager


def current_prosody_instruction(
    query: str = "",
    active_window: str = "",
    system_status: Optional[Dict[str, Any]] = None,
) -> str:
    """Génère la directive de prosodie pour le tour en cours."""
    profile = _global_prosody_manager.evaluate_profile(
        query=query,
        active_window=active_window,
        system_status=system_status,
    )
    return _global_prosody_manager.format_prosody_instruction(profile)
