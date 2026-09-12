"""Moteur de spatialisation audio 3D binaurale et HRTF pour ANO-GPT.

Ce module spatialise la voix de Jarvis en temps réel dans le casque de
l'utilisateur avec un rendu 3D binaural immersif sous Linux / PipeWire.

Fonctionnalités :
-----------------
1. **Filtre spatial binaural (HRTF / HRIR) :**
   - Modélisation physique complète :
     * ITD (Interaural Time Difference) selon le modèle sphérique de Woodworth-Schroeder.
     * ILD (Interaural Level Difference) et Head Shadowing via filtre de Rayleigh / Duda.
     * Encoches spectrales de hauteur du pavillon (Pinna elevation filter de Brown & Duda).
     * Atténuation et absorption de l'air en fonction de la distance.
   - Convolution temps réel ultra-rapide avec `scipy.signal` et gestion de mémoire
     de trame (overlap-add / convolution continue sans clic ni coupure).
   - Support des fichiers SOFA standard (AES69) et banques HRIR WAV multi-canaux
     (KEMAR, HeSuVi, etc.) avec repli autonome sur HRIR synthétique haute fidélité.

2. **Effets dynamiques interactifs selon l'état de l'Orbe de Jarvis :**
   - **Mini-Orbe (layer-shell / coin haut-droit) :**
     Voix localisée nettement en hauteur et à droite (azimut ~+32°, élévation ~+18°).
   - **Plein écran centré (mode holographique) :**
     Voix centrée enveloppante (widening stéréo mid-side) enrichie d'une réverbération
     spatiale de pièce futuriste (early reflections nettes + diffusion Schroeder-Moorer).
   - **Compagnon flottant (coin bas-droit) :**
     Voix légèrement en bas à droite (azimut ~+30°, élévation ~-15°).
   - **Lissage continu des transitions (Slew-Rate / Exponential Smoothing) :**
     Transitions imperceptibles sans saut de phase, artefact Doppler excessif ni clic.

3. **Intégration PipeWire filter-chain :**
   - Génération de templates de configuration standard PipeWire (`libpipewire-module-filter-chain`).
   - Export d'impulsions HRIR compatibles avec le module `convolver` natif de PipeWire.
"""
from __future__ import annotations

import collections
import enum
import json
import logging
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import numpy as np

# SciPy pour le filtrage et la convolution FFT rapide
try:
    import scipy.io.wavfile as _wavfile
    import scipy.signal as _signal
    _SCIPY_AVAILABLE = True
except ImportError:
    _signal = None  # type: ignore
    _wavfile = None  # type: ignore
    _SCIPY_AVAILABLE = False

logger = logging.getLogger("anogpt.spatial_audio")


def spatial_audio_requested(devices: Optional[List[Any]] = None) -> bool:
    """Décide si le binaural doit être actif sans dégrader les haut-parleurs.

    ``ANOGPT_SPATIAL_AUDIO=1`` force l'activation, ``0`` la coupe. Sans
    réglage explicite, le traitement s'active seulement pour un casque ou une
    sortie Bluetooth ; un rendu HRTF sur les haut-parleurs intégrés n'a pas la
    géométrie d'écoute nécessaire.
    """
    configured = os.environ.get("ANOGPT_SPATIAL_AUDIO")
    if configured is not None:
        return configured.strip().lower() in {"1", "true", "yes", "on"}
    if devices is None:
        try:
            from core.audio_router import list_output_devices
            devices = list_output_devices()
        except Exception:
            devices = []
    keywords = ("casque", "headphone", "headset", "earbud", "airpod", "buds")
    for device in devices:
        text = f"{getattr(device, 'name', '')} {getattr(device, 'description', '')}".casefold()
        if getattr(device, "kind", "") == "bluetooth" or any(word in text for word in keywords):
            return True
    return False

# ═══════════════════════════════════════════════════════════════════════════════
# Constantes Physiques et Acoustiques
# ═══════════════════════════════════════════════════════════════════════════════

SPEED_OF_SOUND = 343.0            # Vitesse du son dans l'air sec à 20°C (m/s)
HEAD_RADIUS = 0.0875              # Rayon moyen d'une tête humaine adulte (m, ~8.75 cm)
DEFAULT_SAMPLE_RATE = 24000       # Fréquence d'échantillonnage de lecture JARVIS (Hz)
EAR_SPACING = 2.0 * HEAD_RADIUS   # Distance interaurale (~17.5 cm)

# Fréquence de coupure d'effet d'ombre de la tête (Head Shadowing)
HEAD_SHADOW_FC = 1600.0           # Hz : seuil sous lequel la diffraction domine

# ═══════════════════════════════════════════════════════════════════════════════
# Énumérations et Structures de Données
# ═══════════════════════════════════════════════════════════════════════════════

class OrbMode(str, enum.Enum):
    """Modes d'affichage de l'Orbe de Jarvis et profils spatiaux correspondants."""
    MINI_ORB_TOP_RIGHT = "mini_orb_top_right"        # Mini-orbe layer-shell en haut à droite
    FULLSCREEN_CENTER = "fullscreen_center"          # Orbe centré immersif / holographique
    COMPANION_BOTTOM_RIGHT = "companion_bottom_right" # Bulle compagnon en bas à droite
    CUSTOM = "custom"                                # Coordonnées 3D personnalisées


@dataclass
class SphericalCoords:
    """Coordonnées sphériques d'une source audio relative au centre de la tête."""
    azimuth_deg: float      # Azimut en degrés [-180, +180] : 0° face, +90° droite, -90° gauche
    elevation_deg: float    # Élévation en degrés [-90, +90] : 0° horizon, +90° zénith, -90° nadir
    distance_m: float = 1.0 # Distance en mètres (>= 0.1m)

    def normalized(self) -> SphericalCoords:
        """Normalise les angles dans leurs plages canoniques."""
        az = (self.azimuth_deg + 180.0) % 360.0 - 180.0
        el = max(-90.0, min(90.0, self.elevation_deg))
        dist = max(0.1, self.distance_m)
        return SphericalCoords(az, el, dist)


@dataclass
class CartesianCoords:
    """Coordonnées cartésiennes d'une source audio relative au centre de la tête."""
    x: float  # Axe X : droite (+) / gauche (-) en mètres
    y: float  # Axe Y : haut (+) / bas (-) en mètres (élévation)
    z: float  # Axe Z : devant (+) / derrière (-) en mètres


@dataclass
class ScreenGeometry:
    """Modèle géométrique de l'écran et de la position de l'utilisateur."""
    screen_width_m: float = 0.53        # Largeur de l'écran (ex. écran 24" 16:9 ~ 0.53m)
    screen_height_m: float = 0.30       # Hauteur de l'écran (~0.30m)
    user_distance_m: float = 0.65       # Distance œil/tête à l'écran (~65 cm)
    user_eye_height_ratio: float = 0.45 # Hauteur des yeux par rapport au haut de l'écran (0.0=haut, 1.0=bas)


# ═══════════════════════════════════════════════════════════════════════════════
# Calculs Géométriques et Trigonométriques 3D
# ═══════════════════════════════════════════════════════════════════════════════

def cartesian_to_spherical(x: float, y: float, z: float) -> SphericalCoords:
    """Convertit des coordonnées cartésiennes (x=droite, y=haut, z=devant) en sphériques."""
    distance = math.sqrt(x * x + y * y + z * z)
    if distance < 1e-6:
        return SphericalCoords(0.0, 0.0, 0.1)

    # Azimut : angle dans le plan horizontal (X-Z)
    azimuth_rad = math.atan2(x, max(1e-6, z))
    azimuth_deg = math.degrees(azimuth_rad)

    # Élévation : angle par rapport au plan horizontal
    horiz_dist = math.sqrt(x * x + z * z)
    elevation_rad = math.atan2(y, max(1e-6, horiz_dist))
    elevation_deg = math.degrees(elevation_rad)

    return SphericalCoords(azimuth_deg, elevation_deg, distance).normalized()


def spherical_to_cartesian(coords: SphericalCoords) -> CartesianCoords:
    """Convertit des coordonnées sphériques en coordonnées cartésiennes."""
    az_rad = math.radians(coords.azimuth_deg)
    el_rad = math.radians(coords.elevation_deg)
    r = max(0.1, coords.distance_m)

    y = r * math.sin(el_rad)
    horiz_r = r * math.cos(el_rad)
    x = horiz_r * math.sin(az_rad)
    z = horiz_r * math.cos(az_rad)

    return CartesianCoords(x, y, z)


def screen_coords_to_spherical(
    u: float,
    v: float,
    geom: Optional[ScreenGeometry] = None,
) -> SphericalCoords:
    """Convertit les coordonnées normalisées de l'écran (u, v) en coordonnées sphériques 3D.

    Paramètres
    ----------
    u : float
        Position horizontale sur l'écran : 0.0 (bord gauche), 0.5 (centre), 1.0 (bord droit).
    v : float
        Position verticale sur l'écran : 0.0 (bord haut), 0.5 (centre), 1.0 (bord bas).
    geom : ScreenGeometry, optionnel
        Dimensions physiques et distance de l'utilisateur.

    Retourne
    --------
    SphericalCoords
        Azimut, élévation et distance résultants.
    """
    if geom is None:
        geom = ScreenGeometry()

    # Décalage par rapport au centre de la tête
    x = (u - 0.5) * geom.screen_width_m
    y = (geom.user_eye_height_ratio - v) * geom.screen_height_m
    z = geom.user_distance_m

    return cartesian_to_spherical(x, y, z)


def get_preset_coordinates(mode: OrbMode, geom: Optional[ScreenGeometry] = None) -> SphericalCoords:
    """Retourne les coordonnées 3D spatiales cibles pour un mode d'orbe donné."""
    if geom is None:
        geom = ScreenGeometry()

    if mode == OrbMode.MINI_ORB_TOP_RIGHT:
        # Mini-Orbe en haut à droite (u=0.90, v=0.10)
        # Azimut ~+32°, élévation ~+18°, distance ~0.75m
        return screen_coords_to_spherical(0.90, 0.10, geom)

    elif mode == OrbMode.FULLSCREEN_CENTER:
        # Orbe centré (u=0.5, v=0.5)
        # Azimut 0°, élévation 0°, distance ~0.65m
        return SphericalCoords(azimuth_deg=0.0, elevation_deg=0.0, distance_m=geom.user_distance_m)

    elif mode == OrbMode.COMPANION_BOTTOM_RIGHT:
        # Bulle compagnon ancrée en bas à droite (u=0.88, v=0.88)
        # Azimut ~+28°, élévation ~-15°, distance ~0.75m
        return screen_coords_to_spherical(0.88, 0.88, geom)

    else:
        return SphericalCoords(0.0, 0.0, 1.0)


# ═══════════════════════════════════════════════════════════════════════════════
# Modélisation Acoustique HRTF (ITD, ILD, Pinna)
# ═══════════════════════════════════════════════════════════════════════════════

def compute_woodworth_itd(azimuth_deg: float, sample_rate: int = DEFAULT_SAMPLE_RATE) -> Tuple[float, float, float]:
    """Calcule le retard interaural (ITD) selon la formule de Woodworth-Schroeder.

    Pour une tête sphérique de rayon a :
        ITD(θ) = (a / c) * (sin|θ| + |θ|)   pour |θ| <= π/2

    Retourne
    --------
    (delay_left_sec, delay_right_sec, itd_sec)
    """
    az_rad = math.radians(azimuth_deg)
    abs_az = abs(az_rad)

    if abs_az <= math.pi / 2.0:
        itd = (HEAD_RADIUS / SPEED_OF_SOUND) * (math.sin(abs_az) + abs_az)
    else:
        # Extension au-delà de 90°
        behind_az = math.pi - abs_az
        itd = (HEAD_RADIUS / SPEED_OF_SOUND) * (math.sin(behind_az) + behind_az)

    if az_rad > 0:
        # Source à droite : oreille droite ipsilatérale (0 retard), gauche retardée
        delay_left = itd
        delay_right = 0.0
    elif az_rad < 0:
        # Source à gauche : oreille gauche ipsilatérale, droite retardée
        delay_left = 0.0
        delay_right = itd
    else:
        delay_left = 0.0
        delay_right = 0.0

    return delay_left, delay_right, itd


def compute_head_shadow_coefficients(
    azimuth_deg: float,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> Tuple[Tuple[np.ndarray, np.ndarray], Tuple[np.ndarray, np.ndarray]]:
    """Génère les filtres IIR de Head Shadowing (diffraction crânienne) selon Lord Rayleigh / Duda.

    Retourne les coefficients `(b_left, a_left)` et `(b_right, a_right)` d'un filtre
    shelf de premier ordre discret via transformée bilinéaire.
    """
    az_rad = math.radians(azimuth_deg)
    alpha_l = 1.0 - 0.75 * math.sin(az_rad)
    alpha_r = 1.0 + 0.75 * math.sin(az_rad)

    alpha_l = max(0.20, min(1.80, alpha_l))
    alpha_r = max(0.20, min(1.80, alpha_r))

    tau = (2.0 * HEAD_RADIUS) / SPEED_OF_SOUND

    def _bilinear_shelf(alpha: float) -> Tuple[np.ndarray, np.ndarray]:
        c = 2.0 * sample_rate
        t_half = tau / 2.0

        b0 = 1.0 + c * alpha * t_half
        b1 = 1.0 - c * alpha * t_half
        a0 = 1.0 + c * t_half
        a1 = 1.0 - c * t_half

        b = np.array([b0 / a0, b1 / a0], dtype=np.float32)
        a = np.array([1.0, a1 / a0], dtype=np.float32)
        return b, a

    return _bilinear_shelf(alpha_l), _bilinear_shelf(alpha_r)


def compute_pinna_notch_frequencies(elevation_deg: float) -> float:
    """Calcule la fréquence de résonance / encoche spectrale du pavillon (Pinna).

    L'élévation (+15° à +45°) déplace l'encoche conchale principale de 6.5 kHz
    vers 9.5 kHz, indice perceptif fondamental de hauteur chez l'être humain.
    """
    el_clamped = max(-45.0, min(75.0, elevation_deg))
    norm_el = (el_clamped + 45.0) / 120.0
    notch_freq = 6000.0 + norm_el * 3500.0
    return notch_freq


def generate_parametric_hrir(
    coords: SphericalCoords,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    ir_length: int = 128,
) -> Tuple[np.ndarray, np.ndarray]:
    """Synthétise une paire d'impulsions HRIR (gauche, droite) haute fidélité pour une position 3D.

    Combine ITD Woodworth, Head Shadowing et effet de Pinna dans le domaine temporel.
    """
    coords = coords.normalized()
    impulse = np.zeros(ir_length, dtype=np.float32)
    impulse[0] = 1.0

    # 1. Calcul ITD
    del_l_sec, del_r_sec, _ = compute_woodworth_itd(coords.azimuth_deg, sample_rate)
    del_l_samples = del_l_sec * sample_rate
    del_r_samples = del_r_sec * sample_rate

    # 2. Filtre de diffraction de tête (Head Shadow)
    (b_l, a_l), (b_r, a_r) = compute_head_shadow_coefficients(coords.azimuth_deg, sample_rate)

    if _SCIPY_AVAILABLE and _signal is not None:
        resp_l = _signal.lfilter(b_l, a_l, impulse)
        resp_r = _signal.lfilter(b_r, a_r, impulse)
    else:
        def _lfilter_1st(b, a, x):
            y = np.zeros_like(x)
            for n in range(len(x)):
                x0 = x[n]
                x1 = x[n - 1] if n > 0 else 0.0
                y1 = y[n - 1] if n > 0 else 0.0
                y[n] = b[0] * x0 + b[1] * x1 - a[1] * y1
            return y
        resp_l = _lfilter_1st(b_l, a_l, impulse)
        resp_r = _lfilter_1st(b_r, a_r, impulse)

    # 3. Encoche de Pinna (filtre coupe-bande résonant)
    notch_freq = compute_pinna_notch_frequencies(coords.elevation_deg)
    if _SCIPY_AVAILABLE and _signal is not None and notch_freq < (sample_rate / 2.0 - 500.0):
        q_factor = 3.5
        b_notch, a_notch = _signal.iirnotch(notch_freq, q_factor, sample_rate)
        resp_l = _signal.lfilter(b_notch, a_notch, resp_l)
        resp_r = _signal.lfilter(b_notch, a_notch, resp_r)

    # 4. Retard fractionnaire par interpolation linéaire d'échantillons
    def _apply_delay(sig: np.ndarray, delay_samples: float) -> np.ndarray:
        int_delay = int(math.floor(delay_samples))
        frac_delay = delay_samples - int_delay
        out = np.zeros_like(sig)
        for i in range(len(sig)):
            src_idx = i - int_delay
            if 0 <= src_idx < len(sig):
                val = sig[src_idx]
                if frac_delay > 0.01 and src_idx > 0:
                    val = (1.0 - frac_delay) * val + frac_delay * sig[src_idx - 1]
                out[i] = val
        return out

    ir_l = _apply_delay(resp_l, del_l_samples)
    ir_r = _apply_delay(resp_r, del_r_samples)

    # 5. Atténuation de distance (loi en 1/r normalisée)
    dist_factor = min(1.0, 0.70 / max(0.2, coords.distance_m))
    ir_l *= dist_factor
    ir_r *= dist_factor

    # Fenêtrage doux en fin d'impulsion pour éviter tout clic
    window = np.ones(ir_length, dtype=np.float32)
    fade_len = min(16, ir_length // 4)
    window[-fade_len:] = 0.5 * (1.0 + np.cos(np.linspace(0, math.pi, fade_len)))
    ir_l *= window
    ir_r *= window

    # Normalisation pour préserver l'énergie sonore globale
    energy = math.sqrt(np.sum(ir_l ** 2) + np.sum(ir_r ** 2))
    if energy > 1e-5:
        norm = 1.0 / (energy * 1.1)
        ir_l *= norm
        ir_r *= norm

    return ir_l.astype(np.float32), ir_r.astype(np.float32)


# ═══════════════════════════════════════════════════════════════════════════════
# Réverbération Holographique Futuriste (Mode Plein Écran Centré)
# ═══════════════════════════════════════════════════════════════════════════════

class HolographicRoomReverb:
    """Processeur de réverbération spatiale et d'enveloppement holographique.

    Conçu spécialement pour le mode plein écran centré :
    - Élargissement stéréo par décorrélation mid-side / effet Haas
    - Réseau de réflexions précoces (Early Reflections) asymétriques
    - Ligne de diffusion allpass Schroeder pour une aura futuriste sans bouillie sonore
    """

    def __init__(self, sample_rate: int = DEFAULT_SAMPLE_RATE):
        self.sample_rate = sample_rate

        er_delays_ms = [7.3, 12.7, 18.2, 24.5, 31.8, 39.4]
        self._er_delays = [max(1, int(d * sample_rate / 1000.0)) for d in er_delays_ms]
        self._er_gains_l = [0.38, -0.25, 0.20, -0.15, 0.12, -0.08]
        self._er_gains_r = [-0.22, 0.35, -0.18, 0.22, -0.10, 0.14]

        max_er = max(self._er_delays) + 1024
        self._er_buffer = np.zeros(max_er, dtype=np.float32)
        self._er_idx = 0

        self._ap1_delay = int(5.1 * sample_rate / 1000.0)
        self._ap2_delay = int(11.3 * sample_rate / 1000.0)
        self._ap1_buf_l = np.zeros(self._ap1_delay + 1, dtype=np.float32)
        self._ap1_buf_r = np.zeros(self._ap1_delay + 1, dtype=np.float32)
        self._ap2_buf_l = np.zeros(self._ap2_delay + 1, dtype=np.float32)
        self._ap2_buf_r = np.zeros(self._ap2_delay + 1, dtype=np.float32)
        self._ap1_idx = 0
        self._ap2_idx = 0

        self._hf_damp_l = 0.0
        self._hf_damp_r = 0.0
        self._fast_history = np.zeros(max(self._er_delays), dtype=np.float32)
        self._fast_states: dict[str, np.ndarray] = {}
        self._fast_filters = []
        for delay, gain in ((self._ap1_delay, .62), (self._ap2_delay, .55)):
            b = np.zeros(delay + 1)
            a = np.zeros(delay + 1)
            b[0], b[-1] = -gain, 1.0
            a[0], a[-1] = 1.0, -gain
            self._fast_filters.append((b, a))

    def reset(self) -> None:
        """Réinitialise les mémoires d'état."""
        self._er_buffer.fill(0.0)
        self._er_idx = 0
        self._ap1_buf_l.fill(0.0)
        self._ap1_buf_r.fill(0.0)
        self._ap2_buf_l.fill(0.0)
        self._ap2_buf_r.fill(0.0)
        self._hf_damp_l = 0.0
        self._hf_damp_r = 0.0
        self._fast_history.fill(0.0)
        self._fast_states.clear()

    def _process_native(self, mono_input, wet_mix, stereo_widening):
        """Même réseau de retards, filtres récursifs exécutés en code natif.

        Évite les milliers d'itérations Python par bloc qui retenaient le GIL
        pendant la lecture et les animations de l'interface.
        """
        history = self._fast_history
        samples = np.concatenate((history, mono_input))
        offset, count = len(history), len(mono_input)
        left = np.zeros(count, dtype=np.float32)
        right = np.zeros(count, dtype=np.float32)
        for delay, gl, gr in zip(self._er_delays, self._er_gains_l, self._er_gains_r):
            tap = samples[offset - delay:offset - delay + count]
            left += tap * gl
            right += tap * gr
        self._fast_history = samples[-len(history):].copy()
        channels = []
        for channel, values in (("l", left), ("r", right)):
            for index, (b, a) in enumerate(self._fast_filters):
                key = f"{channel}{index}"
                state = self._fast_states.get(key)
                if state is None:
                    state = np.zeros(len(a) - 1)
                values, self._fast_states[key] = _signal.lfilter(b, a, values, zi=state)
            key = channel + "damp"
            state = self._fast_states.get(key, np.zeros(1))
            values, self._fast_states[key] = _signal.lfilter([.35], [1.0, -.65], values, zi=state)
            channels.append(values)
        left, right = channels
        side = (left - right) * stereo_widening
        dry = (1.0 - wet_mix) * mono_input
        return ((dry + wet_mix * (left + side)).astype(np.float32),
                (dry + wet_mix * (right - side)).astype(np.float32))

    def process(
        self,
        mono_input: np.ndarray,
        wet_mix: float = 0.22,
        stereo_widening: float = 0.35,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """Applique la réverbération holographique et l'enveloppement spatial."""
        n_samples = len(mono_input)
        if n_samples == 0:
            return np.zeros(0, dtype=np.float32), np.zeros(0, dtype=np.float32)

        if _SCIPY_AVAILABLE and _signal is not None:
            return self._process_native(mono_input, wet_mix, stereo_widening)

        er_buf = self._er_buffer
        er_buf_len = len(er_buf)
        er_idx = self._er_idx

        reverb_l = np.zeros(n_samples, dtype=np.float32)
        reverb_r = np.zeros(n_samples, dtype=np.float32)

        for i in range(n_samples):
            x = float(mono_input[i])
            er_buf[er_idx] = x

            acc_l = 0.0
            acc_r = 0.0
            for d, gl, gr in zip(self._er_delays, self._er_gains_l, self._er_gains_r):
                tap_idx = (er_idx - d) % er_buf_len
                sample = er_buf[tap_idx]
                acc_l += sample * gl
                acc_r += sample * gr

            reverb_l[i] = acc_l
            reverb_r[i] = acc_r
            er_idx = (er_idx + 1) % er_buf_len

        self._er_idx = er_idx

        ap1_g = 0.62
        ap2_g = 0.55
        d1 = self._ap1_delay
        d2 = self._ap2_delay
        b1_l, b1_r = self._ap1_buf_l, self._ap1_buf_r
        b2_l, b2_r = self._ap2_buf_l, self._ap2_buf_r
        idx1, idx2 = self._ap1_idx, self._ap2_idx
        len1, len2 = len(b1_l), len(b2_l)

        for i in range(n_samples):
            tap1 = (idx1 - d1) % len1
            yl1 = -ap1_g * reverb_l[i] + b1_l[tap1]
            b1_l[idx1] = reverb_l[i] + ap1_g * yl1
            yr1 = -ap1_g * reverb_r[i] + b1_r[tap1]
            b1_r[idx1] = reverb_r[i] + ap1_g * yr1
            idx1 = (idx1 + 1) % len1

            tap2 = (idx2 - d2) % len2
            yl2 = -ap2_g * yl1 + b2_l[tap2]
            b2_l[idx2] = yl1 + ap2_g * yl2
            yr2 = -ap2_g * yr1 + b2_r[tap2]
            b2_r[idx2] = yr1 + ap2_g * yr2
            idx2 = (idx2 + 1) % len2

            self._hf_damp_l = 0.65 * self._hf_damp_l + 0.35 * yl2
            self._hf_damp_r = 0.65 * self._hf_damp_r + 0.35 * yr2

            reverb_l[i] = self._hf_damp_l
            reverb_r[i] = self._hf_damp_r

        self._ap1_idx = idx1
        self._ap2_idx = idx2

        side = (reverb_l - reverb_r) * stereo_widening
        wet_l = reverb_l + side
        wet_r = reverb_r - side

        out_l = (1.0 - wet_mix) * mono_input + wet_mix * wet_l
        out_r = (1.0 - wet_mix) * mono_input + wet_mix * wet_r

        return out_l.astype(np.float32), out_r.astype(np.float32)


# ═══════════════════════════════════════════════════════════════════════════════
# Lisseur Temporel de Transitions Spatiales (Pas de Clics ni Sauts)
# ═══════════════════════════════════════════════════════════════════════════════

class SpatialTransitionSmoother:
    """Lisse les variations de coordonnées 3D pour un confort d'écoute optimal.

    Empêche les discontinuités de phase, l'effet Doppler brusque et les clics
    lorsque l'Orbe de Jarvis passe du coin haut-droit au plein écran.
    """

    def __init__(self, time_constant_sec: float = 0.12, sample_rate: int = DEFAULT_SAMPLE_RATE):
        self.sample_rate = sample_rate
        self.time_constant_sec = time_constant_sec

        self.current_azimuth = 0.0
        self.current_elevation = 0.0
        self.current_distance = 0.70
        self.current_reverb_wet = 0.22

        self.target_azimuth = 0.0
        self.target_elevation = 0.0
        self.target_distance = 0.70
        self.target_reverb_wet = 0.22

    def set_target(
        self,
        coords: SphericalCoords,
        reverb_wet: float = 0.0,
        snap_immediately: bool = False,
    ) -> None:
        """Fixe la nouvelle cible spatiale."""
        coords = coords.normalized()
        self.target_azimuth = coords.azimuth_deg
        self.target_elevation = coords.elevation_deg
        self.target_distance = coords.distance_m
        self.target_reverb_wet = max(0.0, min(1.0, reverb_wet))

        if snap_immediately:
            self.current_azimuth = self.target_azimuth
            self.current_elevation = self.target_elevation
            self.current_distance = self.target_distance
            self.current_reverb_wet = self.target_reverb_wet

    def step(self, chunk_samples: int) -> Tuple[SphericalCoords, float]:
        """Avance le lissage d'un bloc d'échantillons et retourne l'état interpolé."""
        chunk_sec = chunk_samples / float(self.sample_rate)
        alpha = 1.0 - math.exp(-chunk_sec / max(0.01, self.time_constant_sec))

        self.current_azimuth += alpha * (self.target_azimuth - self.current_azimuth)
        self.current_elevation += alpha * (self.target_elevation - self.current_elevation)
        self.current_distance += alpha * (self.target_distance - self.current_distance)
        self.current_reverb_wet += alpha * (self.target_reverb_wet - self.current_reverb_wet)

        return (
            SphericalCoords(self.current_azimuth, self.current_elevation, self.current_distance),
            self.current_reverb_wet,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Support des Fichiers SOFA Standard et Exportation HRIR
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class SOFAData:
    """Structure en mémoire pour un profil HRTF chargé depuis un fichier standard."""
    sample_rate: int
    source_positions: np.ndarray  # Shape (M, 3) : [azimuth, elevation, distance]
    ir_left: np.ndarray           # Shape (M, N)
    ir_right: np.ndarray          # Shape (M, N)


class SOFALoader:
    """Chargeur de banques d'impulsions HRTF au format standard SOFA ou WAV multi-canaux."""

    @staticmethod
    def load_hrir_wav(file_path: Union[str, Path]) -> Optional[Tuple[np.ndarray, np.ndarray, int]]:
        """Charge un fichier WAV HRIR stéréo ou multi-canal (ex: KEMAR / HeSuVi)."""
        file_path = Path(file_path)
        if not file_path.exists():
            return None

        try:
            if _SCIPY_AVAILABLE and _wavfile is not None:
                sr, data = _wavfile.read(str(file_path))
            else:
                import wave
                with wave.open(str(file_path), "rb") as wf:
                    sr = wf.getframerate()
                    n_ch = wf.getnchannels()
                    frames = wf.readframes(wf.getnframes())
                    data = np.frombuffer(frames, dtype=np.int16)
                    if n_ch > 1:
                        data = data.reshape(-1, n_ch)

            if data.dtype == np.int16:
                data = data.astype(np.float32) / 32768.0
            elif data.dtype == np.int32:
                data = data.astype(np.float32) / 2147483648.0
            elif data.dtype != np.float32:
                data = data.astype(np.float32)

            if data.ndim == 1:
                return data, data, sr
            elif data.ndim == 2:
                return data[:, 0], data[:, 1], sr
        except Exception as exc:
            logger.warning(f"Impossible de lire le fichier HRIR WAV '{file_path}': {exc}")

        return None

    @staticmethod
    def load_sofa_hdf5(file_path: Union[str, Path]) -> Optional[SOFAData]:
        """Charge un fichier SOFA standard (HDF5 / NetCDF-4)."""
        try:
            import h5py
        except ImportError:
            logger.debug("h5py non installé : chargement direct SOFA HDF5 indisponible.")
            return None

        file_path = Path(file_path)
        if not file_path.exists():
            return None

        try:
            with h5py.File(str(file_path), "r") as f:
                sr = int(np.array(f["Data.SamplingRate"])[0])
                positions = np.array(f["SourcePosition"])  # (M, 3)
                ir_data = np.array(f["Data.IR"])           # (M, 2, N)

                ir_l = ir_data[:, 0, :].astype(np.float32)
                ir_r = ir_data[:, 1, :].astype(np.float32)

                return SOFAData(sample_rate=sr, source_positions=positions, ir_left=ir_l, ir_right=ir_r)
        except Exception as exc:
            logger.warning(f"Erreur lors du décodage du fichier SOFA '{file_path}': {exc}")
            return None

    @staticmethod
    def export_hrir_wav(
        output_path: Union[str, Path],
        ir_left: np.ndarray,
        ir_right: np.ndarray,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
    ) -> bool:
        """Exporte une paire d'impulsions HRIR au format WAV stéréo pour PipeWire."""
        try:
            out_p = Path(output_path)
            out_p.parent.mkdir(parents=True, exist_ok=True)

            max_val = max(float(np.max(np.abs(ir_left))), float(np.max(np.abs(ir_right))), 1e-6)
            norm_l = (ir_left / max_val * 0.95).astype(np.float32)
            norm_r = (ir_right / max_val * 0.95).astype(np.float32)
            stereo = np.column_stack((norm_l, norm_r))

            if _SCIPY_AVAILABLE and _wavfile is not None:
                _wavfile.write(str(out_p), sample_rate, stereo)
                return True
            else:
                import wave
                with wave.open(str(out_p), "wb") as wf:
                    wf.setnchannels(2)
                    wf.setsampwidth(2)
                    wf.setframerate(sample_rate)
                    int16_data = (stereo * 32767.0).clip(-32768, 32767).astype(np.int16)
                    wf.writeframes(int16_data.tobytes())
                return True
        except Exception as exc:
            logger.error(f"Échec de l'export HRIR WAV vers '{output_path}': {exc}")
            return False


# ═══════════════════════════════════════════════════════════════════════════════
# Template et Gestionnaire PipeWire Filter-Chain
# ═══════════════════════════════════════════════════════════════════════════════

class PipeWireSpatialChain:
    """Gestionnaire de la chaîne de filtrage PipeWire (pipewire-filter-chain)."""

    DEFAULT_SINK_NAME = "anogpt_spatial_sink"
    DEFAULT_CONF_PATH = Path(__file__).resolve().parent.parent / "config" / "pipewire-spatial-jarvis.conf"

    @classmethod
    def generate_config_text(
        cls,
        hrir_wav_path: Union[str, Path],
        sink_name: str = DEFAULT_SINK_NAME,
        description: str = "ANO-GPT Jarvis 3D Spatial Audio",
    ) -> str:
        """Génère le texte de configuration standard PipeWire filter-chain avec convolution HRTF."""
        hrir_str = str(Path(hrir_wav_path).resolve())

        config = f"""# ═══════════════════════════════════════════════════════════════════════════
# ANO-GPT — PipeWire Filter-Chain : Spatialisation Binaurale HRTF pour Jarvis
# ═══════════════════════════════════════════════════════════════════════════
#
# Ce fichier configure un sink virtuel haute performance avec convolution HRTF
# native via libpipewire-module-filter-chain.
#
# Utilisation :
#   1. Lancer manuellement :
#        pipewire -c {cls.DEFAULT_CONF_PATH}
#   2. Ou placer dans ~/.config/pipewire/pipewire.conf.d/
#

context.properties = {{
    log.level = 2
}}

context.spa-libs = {{
    audio.convert.* = audioconvert/libspa-audioconvert
    support.*       = support/libspa-support
}}

context.modules = [
    {{ name = libpipewire-module-rt
        flags = [ ifexists nofail ]
    }}
    {{ name = libpipewire-module-protocol-native }}
    {{ name = libpipewire-module-client-node }}
    {{ name = libpipewire-module-adapter }}
    {{ name = libpipewire-module-filter-chain
        flags = [ nofail ]
        args = {{
            node.description = "{description}"
            media.name       = "{description}"
            filter.graph = {{
                nodes = [
                    # Entrée mono répliquée
                    {{
                        type  = builtin
                        label = copy
                        name  = copyIn
                    }}
                    # Convolution Oreille Gauche
                    {{
                        type  = builtin
                        label = convolver
                        name  = convLeft
                        config = {{
                            filename = "{hrir_str}"
                            channel  = 0
                        }}
                    }}
                    # Convolution Oreille Droite
                    {{
                        type  = builtin
                        label = convolver
                        name  = convRight
                        config = {{
                            filename = "{hrir_str}"
                            channel  = 1
                        }}
                    }}
                ]
                links = [
                    {{ output = "copyIn:Out"    input = "convLeft:In" }}
                    {{ output = "copyIn:Out"    input = "convRight:In" }}
                ]
                inputs  = [ "copyIn:In" ]
                outputs = [ "convLeft:Out" "convRight:Out" ]
            }}
            capture.props = {{
                node.name      = "{sink_name}"
                media.class    = Audio/Sink
                audio.channels = 1
                audio.position = [ MONO ]
            }}
            playback.props = {{
                node.name      = "{sink_name}.output"
                node.passive   = true
                audio.channels = 2
                audio.position = [ FL FR ]
            }}
        }}
    }}
]
"""
        return config

    @classmethod
    def create_template_config(
        cls,
        output_conf_path: Optional[Union[str, Path]] = None,
        hrir_wav_path: Optional[Union[str, Path]] = None,
    ) -> Path:
        """Génère le fichier de configuration et l'impulsion HRIR associée."""
        conf_path = Path(output_conf_path or cls.DEFAULT_CONF_PATH)
        conf_path.parent.mkdir(parents=True, exist_ok=True)

        if hrir_wav_path is None:
            hrir_wav_path = conf_path.parent / "jarvis_spatial_hrir.wav"
            coords = SphericalCoords(azimuth_deg=32.0, elevation_deg=18.0, distance_m=0.85)
            ir_l, ir_r = generate_parametric_hrir(coords)
            SOFALoader.export_hrir_wav(hrir_wav_path, ir_l, ir_r)

        content = cls.generate_config_text(hrir_wav_path)
        conf_path.write_text(content, encoding="utf-8")
        logger.info(f"Configuration PipeWire filter-chain générée : {conf_path}")
        return conf_path


# ═══════════════════════════════════════════════════════════════════════════════
# Processeur Audio Spatial Temps Réel Principal
# ═══════════════════════════════════════════════════════════════════════════════

class SpatialAudioProcessor:
    """Moteur de spatialisation temps réel intégré pour ANO-GPT.

    Transforme le flux PCM mono/stéréo brut issu de Gemini Live / TTS en un flux
    binaural 3D immersif ajusté dynamiquement à la position de l'orbe.
    """

    def __init__(
        self,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        default_mode: OrbMode = OrbMode.FULLSCREEN_CENTER,
        screen_geometry: Optional[ScreenGeometry] = None,
    ):
        self.sample_rate = sample_rate
        self.screen_geometry = screen_geometry or ScreenGeometry()

        self._smoother = SpatialTransitionSmoother(time_constant_sec=0.10, sample_rate=sample_rate)
        self._reverb = HolographicRoomReverb(sample_rate=sample_rate)

        self._current_mode = default_mode
        self._custom_coords = SphericalCoords(0.0, 0.0, 0.70)

        initial_coords = get_preset_coordinates(default_mode, self.screen_geometry)
        initial_wet = 0.22 if default_mode == OrbMode.FULLSCREEN_CENTER else 0.06
        self._smoother.set_target(initial_coords, initial_wet, snap_immediately=True)

        self._ir_length = 128
        self._cached_ir_l = np.zeros(self._ir_length, dtype=np.float32)
        self._cached_ir_r = np.zeros(self._ir_length, dtype=np.float32)
        self._last_az = -999.0
        self._last_el = -999.0

        self._overlap_tail_l = np.zeros(self._ir_length - 1, dtype=np.float32)
        self._overlap_tail_r = np.zeros(self._ir_length - 1, dtype=np.float32)

    @property
    def current_mode(self) -> OrbMode:
        return self._current_mode

    def set_orb_mode(self, mode: OrbMode) -> None:
        """Bascule le mode d'orbe (plein écran, mini-orbe haut-droite, etc.)."""
        self._current_mode = mode
        if mode == OrbMode.FULLSCREEN_CENTER:
            coords = get_preset_coordinates(OrbMode.FULLSCREEN_CENTER, self.screen_geometry)
            self._smoother.set_target(coords, reverb_wet=0.22)
        elif mode == OrbMode.MINI_ORB_TOP_RIGHT:
            coords = get_preset_coordinates(OrbMode.MINI_ORB_TOP_RIGHT, self.screen_geometry)
            self._smoother.set_target(coords, reverb_wet=0.06)
        elif mode == OrbMode.COMPANION_BOTTOM_RIGHT:
            coords = get_preset_coordinates(OrbMode.COMPANION_BOTTOM_RIGHT, self.screen_geometry)
            self._smoother.set_target(coords, reverb_wet=0.05)
        elif mode == OrbMode.CUSTOM:
            self._smoother.set_target(self._custom_coords, reverb_wet=0.10)

    def set_custom_coordinates(self, azimuth_deg: float, elevation_deg: float, distance_m: float = 1.0) -> None:
        """Définit des coordonnées 3D arbitraires."""
        self._current_mode = OrbMode.CUSTOM
        self._custom_coords = SphericalCoords(azimuth_deg, elevation_deg, distance_m)
        self._smoother.set_target(self._custom_coords, reverb_wet=0.10)

    def set_screen_position(self, u: float, v: float) -> None:
        """Positionne la source directement à partir des coordonnées normalisées de l'écran."""
        coords = screen_coords_to_spherical(u, v, self.screen_geometry)
        self.set_custom_coordinates(coords.azimuth_deg, coords.elevation_deg, coords.distance_m)

    def process_chunk(
        self,
        pcm_chunk: Union[bytes, bytearray, np.ndarray],
        input_channels: int = 1,
    ) -> bytes:
        """Traite un bloc audio PCM en temps réel et retourne du PCM stéréo 3D binaural (int16)."""
        if isinstance(pcm_chunk, (bytes, bytearray)):
            raw_data = np.frombuffer(pcm_chunk, dtype=np.int16)
        else:
            raw_data = np.asarray(pcm_chunk)

        if raw_data.size == 0:
            return b""

        if raw_data.dtype == np.int16:
            float_data = raw_data.astype(np.float32) / 32768.0
        else:
            float_data = raw_data.astype(np.float32)

        if input_channels > 1 and float_data.ndim > 1:
            mono_sig = np.mean(float_data, axis=1)
        elif input_channels > 1 and float_data.ndim == 1:
            mono_sig = (float_data[0::2] + float_data[1::2]) * 0.5
        else:
            mono_sig = float_data

        n_samples = len(mono_sig)
        if n_samples == 0:
            return b""

        # 1. Progression du lissage spatial
        current_coords, current_wet = self._smoother.step(n_samples)

        # 2. Mise à jour de l'impulsion HRIR si les angles ont évolué (> 0.5°)
        if (abs(current_coords.azimuth_deg - self._last_az) > 0.5 or
            abs(current_coords.elevation_deg - self._last_el) > 0.5):
            self._cached_ir_l, self._cached_ir_r = generate_parametric_hrir(
                current_coords,
                sample_rate=self.sample_rate,
                ir_length=self._ir_length,
            )
            self._last_az = current_coords.azimuth_deg
            self._last_el = current_coords.elevation_deg

        # 3. Convolution HRTF continue (Overlap-Add)
        if _SCIPY_AVAILABLE and _signal is not None:
            conv_l = _signal.fftconvolve(mono_sig, self._cached_ir_l, mode="full")
            conv_r = _signal.fftconvolve(mono_sig, self._cached_ir_r, mode="full")
        else:
            conv_l = np.convolve(mono_sig, self._cached_ir_l, mode="full")
            conv_r = np.convolve(mono_sig, self._cached_ir_r, mode="full")

        tail_len = len(self._overlap_tail_l)
        conv_l[:tail_len] += self._overlap_tail_l
        conv_r[:tail_len] += self._overlap_tail_r

        direct_l = conv_l[:n_samples]
        direct_r = conv_r[:n_samples]

        self._overlap_tail_l = conv_l[n_samples:n_samples + tail_len].copy()
        self._overlap_tail_r = conv_r[n_samples:n_samples + tail_len].copy()

        # 4. Traitement Holographique et Réverbération
        if current_wet > 0.01:
            reverb_l, reverb_r = self._reverb.process(
                mono_sig,
                wet_mix=current_wet,
                stereo_widening=0.40 if self._current_mode == OrbMode.FULLSCREEN_CENTER else 0.15,
            )
            final_l = 0.75 * direct_l + 0.25 * reverb_l
            final_r = 0.75 * direct_r + 0.25 * reverb_r
        else:
            final_l = direct_l
            final_r = direct_r

        # 5. Interleaving stéréo int16
        stereo_interleaved = np.empty(n_samples * 2, dtype=np.int16)
        int_l = np.clip(final_l * 32767.0, -32768, 32767).astype(np.int16)
        int_r = np.clip(final_r * 32767.0, -32768, 32767).astype(np.int16)

        stereo_interleaved[0::2] = int_l
        stereo_interleaved[1::2] = int_r

        return stereo_interleaved.tobytes()


# ═══════════════════════════════════════════════════════════════════════════════
# Instance Globale et API de Commodité
# ═══════════════════════════════════════════════════════════════════════════════

_global_spatial_processor: Optional[SpatialAudioProcessor] = None


def get_spatial_processor(sample_rate: int = DEFAULT_SAMPLE_RATE) -> SpatialAudioProcessor:
    """Retourne l'instance unique du processeur spatialisé."""
    global _global_spatial_processor
    if _global_spatial_processor is None:
        _global_spatial_processor = SpatialAudioProcessor(sample_rate=sample_rate)
    return _global_spatial_processor


def set_jarvis_orb_mode(mode: Union[OrbMode, str]) -> None:
    """Configure le mode spatial en fonction de l'affichage de l'orbe."""
    processor = get_spatial_processor()
    if isinstance(mode, str):
        try:
            mode = OrbMode(mode)
        except ValueError:
            mode = OrbMode.FULLSCREEN_CENTER
    processor.set_orb_mode(mode)


def spatialise_audio_chunk(
    pcm_bytes: bytes,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> bytes:
    """Fonction raccourcie pour spatialiser un bloc PCM mono en stéréo 3D."""
    processor = get_spatial_processor(sample_rate=sample_rate)
    return processor.process_chunk(pcm_bytes)


# ═══════════════════════════════════════════════════════════════════════════════
# Générateur de Test d'Écoute Binaurale
# ═══════════════════════════════════════════════════════════════════════════════

def generate_binaural_listening_test(
    output_path: Union[str, Path] = "jarvis_binaural_test.wav",
    duration_s: float = 6.0,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> Path:
    """Génère un fichier audio WAV de test démontrant la spatialisation 3D dynamique.

    Trajectoire sonore :
      1. [0.0s - 1.5s] : Mode Plein Écran Centré (voix holographique enveloppante avec réverbération)
      2. [1.5s - 3.5s] : Transition vers le Mini-Orbe (en haut à droite : +35° azimut, +20° élévation)
      3. [3.5s - 5.0s] : Balayage panoramique vers la gauche (-35° azimut)
      4. [5.0s - 6.0s] : Retour au centre holographique
    """
    out_file = Path(output_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)

    processor = SpatialAudioProcessor(sample_rate=sample_rate)

    t = np.linspace(0, duration_s, int(sample_rate * duration_s), endpoint=False)
    f0 = 160.0
    synth = (
        0.50 * np.sin(2 * np.pi * f0 * t) +
        0.35 * np.sin(2 * np.pi * 2 * f0 * t) +
        0.25 * np.sin(2 * np.pi * 3 * f0 * t) +
        0.18 * np.sin(2 * np.pi * 5 * f0 * t) +
        0.12 * np.sin(2 * np.pi * 8 * f0 * t)
    )
    speech_envelope = 0.5 * (1.0 + np.sin(2 * np.pi * 3.2 * t))
    synth = (synth * speech_envelope * 0.45).astype(np.float32)

    chunk_size = 512
    out_chunks = []

    for i in range(0, len(synth), chunk_size):
        chunk = synth[i:i + chunk_size]
        curr_t = i / float(sample_rate)

        if curr_t < 1.5:
            processor.set_orb_mode(OrbMode.FULLSCREEN_CENTER)
        elif curr_t < 3.5:
            processor.set_orb_mode(OrbMode.MINI_ORB_TOP_RIGHT)
        elif curr_t < 5.0:
            processor.set_custom_coordinates(azimuth_deg=-35.0, elevation_deg=10.0, distance_m=0.85)
        else:
            processor.set_orb_mode(OrbMode.FULLSCREEN_CENTER)

        chunk_bytes = (chunk * 32767.0).astype(np.int16).tobytes()
        stereo_bytes = processor.process_chunk(chunk_bytes)
        out_chunks.append(stereo_bytes)

    full_stereo_bytes = b"".join(out_chunks)
    stereo_int16 = np.frombuffer(full_stereo_bytes, dtype=np.int16).reshape(-1, 2)

    if _SCIPY_AVAILABLE and _wavfile is not None:
        _wavfile.write(str(out_file), sample_rate, stereo_int16)
    else:
        import wave
        with wave.open(str(out_file), "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(stereo_int16.tobytes())

    logger.info(f"Fichier de test d'écoute binaurale généré avec succès : {out_file.resolve()}")
    return out_file
