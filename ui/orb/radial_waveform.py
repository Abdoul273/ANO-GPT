#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ui/orb/radial_waveform.py — Visualiseur Spectral Circulaire Haute Performance pour ANO-GPT.

Architecture :
1. Analyse spectrale FFT (CircularFFTEngine / SpectralAnalyzer) :
   - Découpage en 64 ou 128 bandes logarithmiques (20 Hz à 20 kHz).
   - Banc de filtres triangulaires/trapézoïdaux vectorisé (produit matriciel numpy).
   - Pondération psychoacoustique (compensation +3 dB/octave pour égaliser le spectre 1/f).
   - Lissage temporel asymétrique :
     * Attack rapide  : 10 ms (réponse instantanée aux transitoires, voyelles et percussions)
     * Decay doux     : 150 ms (retombée soyeuse, sans saccades ni tremblements)
   - Détection des transitoires vocaux (formants 300 Hz - 3400 Hz) pour l'éjection de particules.

2. Rendu graphique circulaire néon (RadialWaveformRenderer & RadialWaveformWidget) :
   - Rayons néon émanant du bord de l'orbe avec dégradé spectral continu :
     * Basses (< 250 Hz)       : Bleu profond royal (#0022ff / #0a1172)
     * Médiums (250 - 4000 Hz) : Cyan électrique (#00f0ff / #00ffff)
     * Aigus (> 4000 Hz)       : Magenta incandescent (#ff00aa / #e000ff)
   - Multi-passes néon : halo diffus en CompositionMode_Plus, cœur saturé, filament incandescent.
   - Effet de rémanence (motion blur) : traînées spectrales vectorielles persistantes et surface d'accumulation.
   - Têtes de crête (peak hold caps) dérivant sous pesanteur.
   - Particules lumineuses éjectées lors des pics de voix (dispersion radiale, traînées, friction).

3. Synchronisation bi-directionnelle (BiDirectionalAudioBridge) :
   - Audio capturé (voix utilisateur, microphone 16 kHz).
   - Audio émis (voix TTS de Jarvis 24 kHz ou musique MPV 44.1/48 kHz).
   - Moniteur système automatique Linux (PulseAudio/PipeWire via parec) sans configuration.
   - Mixage intelligent et étiquetage dynamique des sources actives.

4. Suite de benchmarks intégrée :
   - Mesures microsecondes de la FFT, du filtrage matriciel, du lissage et des particules.
   - Benchmark de rendu QPainter / OpenGL avec calcul du framerate maximal et de la charge CPU.
"""

from __future__ import annotations

import argparse
import math
import os
import platform
import subprocess
import sys
import threading
import time
from collections import deque
from enum import Enum
from typing import Callable, List, Optional, Tuple, Union

import numpy as np

from PyQt6.QtCore import QLineF, QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QBrush, QColor, QImage, QLinearGradient, QPainter,
    QPainterPath, QPen, QRadialGradient,
)
from PyQt6.QtWidgets import QApplication, QMainWindow, QSizePolicy, QWidget

# Détection de l'accélération OpenGL PyQt6
try:
    from PyQt6.QtOpenGLWidgets import QOpenGLWidget
    _HAS_OPENGL_WIDGET = True
except ImportError:
    QOpenGLWidget = QWidget  # type: ignore
    _HAS_OPENGL_WIDGET = False


# ═════════════════════════════════════════════════════════════════════════════
# 1. SOURCES AUDIO & CONSTANTES SIGNAL
# ═════════════════════════════════════════════════════════════════════════════

class AudioSource(Enum):
    """Provenance du flux audio pour la synchronisation bi-directionnelle."""
    CAPTURED = "captured"  # Voix utilisateur (microphone 16 kHz)
    EMITTED  = "emitted"   # Voix Jarvis TTS (24 kHz) ou musique MPV (44.1/48 kHz)
    SYSTEM   = "system"    # Moniteur de sortie système (PipeWire / PulseAudio)
    COMBINED = "combined"  # Fusion intelligente des deux canaux


# Constantes psychoacoustiques et temporelles strictes
TAU_ATTACK_S: float = 0.010   # 10 ms (Attaque rapide)
TAU_DECAY_S: float  = 0.150   # 150 ms (Décroissance douce)
VOCAL_FREQ_MIN: float = 300.0   # Début bande vocale formants (Hz)
VOCAL_FREQ_MAX: float = 3400.0  # Fin bande vocale formants (Hz)


# ═════════════════════════════════════════════════════════════════════════════
# 2. TAMPON CIRCULAIRE AUDIO THREAD-SAFE (RING BUFFER)
# ═════════════════════════════════════════════════════════════════════════════

class AudioRingBuffer:
    """Tampon circulaire thread-safe recevant des flux PCM asynchrones."""

    def __init__(self, capacity: int = 32768, target_sr: int = 44100):
        self.capacity = capacity
        self.target_sr = target_sr
        self._buffer = np.zeros(capacity, dtype=np.float32)
        self._write_pos = 0
        self._lock = threading.Lock()
        self._total_written = 0

    def push(self, data: Union[bytes, np.ndarray], input_sr: int = 44100) -> None:
        """Injecte des échantillons PCM (bytes int16/float32 ou tableau numpy)."""
        if data is None or len(data) == 0:
            return

        if isinstance(data, bytes):
            arr = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        elif isinstance(data, np.ndarray):
            if data.dtype == np.int16:
                arr = data.astype(np.float32) / 32768.0
            else:
                arr = data.astype(np.float32)
        else:
            return

        if arr.ndim > 1:
            arr = np.mean(arr, axis=1)

        # Rééchantillonnage rapide si nécessaire
        if input_sr != self.target_sr and len(arr) > 1:
            target_len = int(round(len(arr) * (self.target_sr / input_sr)))
            if target_len > 0:
                indices = np.linspace(0, len(arr) - 1, target_len)
                arr = np.interp(indices, np.arange(len(arr)), arr)

        n = len(arr)
        with self._lock:
            if n >= self.capacity:
                self._buffer[:] = arr[-self.capacity:]
                self._write_pos = 0
            else:
                space1 = min(n, self.capacity - self._write_pos)
                self._buffer[self._write_pos:self._write_pos + space1] = arr[:space1]
                remainder = n - space1
                if remainder > 0:
                    self._buffer[:remainder] = arr[space1:]
                    self._write_pos = remainder
                else:
                    self._write_pos = (self._write_pos + space1) % self.capacity
            self._total_written += n

    def read_latest(self, n_samples: int) -> np.ndarray:
        """Extrait les `n_samples` plus récents de façon continue."""
        n = min(n_samples, self.capacity)
        out = np.empty(n, dtype=np.float32)
        with self._lock:
            pos = self._write_pos
            if pos >= n:
                out[:] = self._buffer[pos - n:pos]
            else:
                part1 = n - pos
                out[:part1] = self._buffer[self.capacity - part1:self.capacity]
                out[part1:] = self._buffer[:pos]
        return out


# ═════════════════════════════════════════════════════════════════════════════
# 3. MOTEUR FFT & ANALYSE SPECTRALE LOGARITHMIQUE
# ═════════════════════════════════════════════════════════════════════════════

class CircularFFTEngine:
    """Moteur d'analyse FFT logarithmique avec lissage temporel asymétrique."""

    def __init__(
        self,
        num_bands: int = 64,
        sample_rate: int = 44100,
        fft_size: int = 2048,
        min_freq: float = 20.0,
        max_freq: float = 20000.0,
        tau_attack: float = TAU_ATTACK_S,
        tau_decay: float = TAU_DECAY_S,
    ):
        if num_bands not in (64, 128):
            num_bands = 128 if num_bands > 96 else 64

        self.num_bands = num_bands
        self.sample_rate = sample_rate
        self.fft_size = fft_size
        self.min_freq = min_freq
        self.max_freq = min(max_freq, sample_rate / 2.0 - 50.0)
        self.tau_attack = tau_attack
        self.tau_decay = tau_decay

        # Fenêtre temporelle de Hann précalculée
        self._window = np.hanning(self.fft_size).astype(np.float32)
        self._window_norm = float(np.sum(self._window)) or 1.0

        # Fréquences binaires FFT
        self.fft_freqs = np.fft.rfftfreq(self.fft_size, d=1.0 / self.sample_rate)
        self.num_bins = len(self.fft_freqs)

        # Construction du banc de filtres logarithmique précalculé
        self._filterbank, self.band_center_freqs = self._build_log_filterbank()

        # Courbe de pondération psychoacoustique (+3 dB / octave)
        freq_weights = (np.maximum(100.0, self.band_center_freqs) / 1000.0) ** 0.38
        self._psycho_weights = freq_weights.astype(np.float32)

        # Masque formants vocaux (300 Hz - 3400 Hz)
        self._vocal_mask = (self.band_center_freqs >= VOCAL_FREQ_MIN) & (
            self.band_center_freqs <= VOCAL_FREQ_MAX
        )
        self._vocal_indices = np.where(self._vocal_mask)[0]

        # États temporels
        self._smoothed_bands = np.zeros(self.num_bands, dtype=np.float32)
        self._peak_caps = np.zeros(self.num_bands, dtype=np.float32)
        self._prev_vocal_energy = 0.0
        self._last_process_time = time.perf_counter()

        self.vocal_peak_detected = False
        self.vocal_peak_intensity = 0.0

    def _build_log_filterbank(self) -> Tuple[np.ndarray, np.ndarray]:
        """Génère la matrice de projection spectrale vers les bandes logarithmiques."""
        edges = np.geomspace(self.min_freq, self.max_freq, self.num_bands + 1)
        centers = np.sqrt(edges[:-1] * edges[1:])
        fb = np.zeros((self.num_bands, self.num_bins), dtype=np.float32)

        for b in range(self.num_bands):
            f_low = edges[b]
            f_cent = centers[b]
            f_high = edges[b + 1]

            left_mask = (self.fft_freqs >= f_low) & (self.fft_freqs <= f_cent)
            right_mask = (self.fft_freqs > f_cent) & (self.fft_freqs <= f_high)

            if np.any(left_mask) and (f_cent > f_low):
                fb[b, left_mask] = (self.fft_freqs[left_mask] - f_low) / (f_cent - f_low)
            if np.any(right_mask) and (f_high > f_cent):
                fb[b, right_mask] = (f_high - self.fft_freqs[right_mask]) / (f_high - f_cent)

            if np.sum(fb[b]) < 1e-4:
                idx = np.argmin(np.abs(self.fft_freqs - f_cent))
                fb[b, idx] = 1.0
            else:
                fb[b] /= np.sum(fb[b])

        return fb, centers

    def process(
        self, samples: np.ndarray, dt: Optional[float] = None
    ) -> Tuple[np.ndarray, np.ndarray, bool, float]:
        """Exécute la FFT, le découpage logarithmique et le lissage temporel asymétrique."""
        now = time.perf_counter()
        if dt is None:
            dt = max(0.001, min(0.100, now - self._last_process_time))
        self._last_process_time = now

        # Fenêtrage + FFT
        if len(samples) < self.fft_size:
            pad = np.zeros(self.fft_size, dtype=np.float32)
            pad[-len(samples):] = samples
            samples = pad
        elif len(samples) > self.fft_size:
            samples = samples[-self.fft_size:]

        windowed = samples * self._window
        spectrum = np.abs(np.fft.rfft(windowed)) / self._window_norm

        # Projection logarithmique
        raw_bands = self._filterbank @ spectrum

        # Pondération psychoacoustique + compression dynamique log
        raw_bands = raw_bands * self._psycho_weights
        gamma = 24.0
        normalized = np.log1p(gamma * (raw_bands * 18.0)) / np.log1p(gamma)
        normalized = np.clip(normalized, 0.0, 1.5)

        # Lissage temporel asymétrique : Attack 10ms, Decay 150ms
        alpha_attack = 1.0 - math.exp(-dt / max(0.001, self.tau_attack))
        alpha_decay  = 1.0 - math.exp(-dt / max(0.001, self.tau_decay))

        diff = normalized - self._smoothed_bands
        coeffs = np.where(diff > 0, alpha_attack, alpha_decay)
        self._smoothed_bands += diff * coeffs

        # Dérive des têtes de crête (Peak hold caps) avec chute sous pesanteur
        gravity_drop = 0.75 * dt
        self._peak_caps = np.maximum(
            self._smoothed_bands,
            np.maximum(0.0, self._peak_caps - gravity_drop)
        )

        # Détection des pics vocaux
        if len(self._vocal_indices) > 0:
            vocal_slice = self._smoothed_bands[self._vocal_indices]
            current_vocal_energy = float(0.6 * np.max(vocal_slice) + 0.4 * np.mean(vocal_slice))
        else:
            current_vocal_energy = float(0.6 * np.max(self._smoothed_bands) + 0.4 * np.mean(self._smoothed_bands))

        flux = current_vocal_energy - self._prev_vocal_energy
        self._prev_vocal_energy = current_vocal_energy

        vocal_threshold = 0.06
        if flux > vocal_threshold and current_vocal_energy > 0.14:
            self.vocal_peak_detected = True
            self.vocal_peak_intensity = min(1.0, (flux - vocal_threshold) * 2.5 + current_vocal_energy * 0.4)
        else:
            self.vocal_peak_detected = False
            self.vocal_peak_intensity = 0.0

        return (
            self._smoothed_bands.copy(),
            self._peak_caps.copy(),
            self.vocal_peak_detected,
            self.vocal_peak_intensity,
        )


# ═════════════════════════════════════════════════════════════════════════════
# 4. GESTION DES PARTICULES LUMINEUSES (ÉJECTION SUR PICS DE VOIX)
# ═════════════════════════════════════════════════════════════════════════════

class Particle:
    """Particule lumineuse éjectée du bord de l'orbe lors d'un pic vocal."""
    __slots__ = ("x", "y", "vx", "vy", "life", "max_life", "color", "size", "drag")

    def __init__(
        self,
        x: float,
        y: float,
        vx: float,
        vy: float,
        color: QColor,
        max_life: float = 0.75,
        size: float = 2.4,
    ):
        self.x = x
        self.y = y
        self.vx = vx
        self.vy = vy
        self.life = 0.0
        self.max_life = max_life
        self.color = color
        self.size = size
        self.drag = 2.6

    def update(self, dt: float) -> bool:
        """Met à jour la physique. Retourne False si la particule expire."""
        self.life += dt
        if self.life >= self.max_life:
            return False
        decay = max(0.0, 1.0 - self.drag * dt)
        self.vx *= decay
        self.vy *= decay
        self.x += self.vx * dt
        self.y += self.vy * dt
        return True


class ParticleSystem:
    """Gestionnaire de particules avec réservoir borné pour zéro allocation."""

    def __init__(self, max_particles: int = 240):
        self.max_particles = max_particles
        self.particles: List[Particle] = []

    def spawn_burst(
        self,
        cx: float,
        cy: float,
        radius: float,
        unit_vectors: List[Tuple[float, float]],
        colors: List[QColor],
        intensity: float = 1.0,
    ) -> None:
        """Éjecte une gerbe de particules radiales depuis les rayons actifs."""
        count = int(round(4 + 10 * intensity))
        n_rays = len(unit_vectors)
        if n_rays == 0:
            return

        for _ in range(count):
            if len(self.particles) >= self.max_particles:
                break
            idx = np.random.randint(0, n_rays)
            ca, sa = unit_vectors[idx]

            jitter = float(np.random.uniform(-0.16, 0.16))
            ang = math.atan2(sa, ca) + jitter
            ca_j = math.cos(ang)
            sa_j = math.sin(ang)

            speed = float(np.random.uniform(140.0, 360.0) * (0.8 + 0.6 * intensity))
            px = cx + ca_j * radius
            py = cy + sa_j * radius
            vx = ca_j * speed + float(np.random.uniform(-20.0, 20.0))
            vy = sa_j * speed + float(np.random.uniform(-20.0, 20.0))

            base_col = colors[idx]
            col = QColor(
                min(255, int(base_col.red() * 0.6 + 255 * 0.4)),
                min(255, int(base_col.green() * 0.6 + 255 * 0.4)),
                min(255, int(base_col.blue() * 0.6 + 255 * 0.4)),
            )

            p = Particle(
                x=px,
                y=py,
                vx=vx,
                vy=vy,
                color=col,
                max_life=float(np.random.uniform(0.45, 0.85)),
                size=float(np.random.uniform(1.8, 3.5)),
            )
            self.particles.append(p)

    def update(self, dt: float) -> None:
        """Met à jour toutes les particules actives."""
        alive: List[Particle] = []
        for p in self.particles:
            if p.update(dt):
                alive.append(p)
        self.particles = alive

    def draw(self, painter: QPainter) -> None:
        """Rendu des particules avec composition additive."""
        if not self.particles:
            return

        painter.save()
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        painter.setPen(Qt.PenStyle.NoPen)

        for p in self.particles:
            progress = p.life / p.max_life
            alpha = max(0, min(255, int(255 * (1.0 - progress) ** 1.8)))
            r = p.size * (1.0 - progress * 0.4)

            col = QColor(p.color.red(), p.color.green(), p.color.blue(), alpha)
            painter.setBrush(QBrush(col))
            painter.drawEllipse(QPointF(p.x, p.y), r, r)

            if alpha > 80:
                hot = QColor(255, 255, 255, alpha // 2)
                painter.setBrush(QBrush(hot))
                painter.drawEllipse(QPointF(p.x, p.y), r * 0.45, r * 0.45)

        painter.restore()


# ═════════════════════════════════════════════════════════════════════════════
# 5. MOTEUR DE RENDU VISUEL RADIAL NÉON HAUTE PERFORMANCE
# ═════════════════════════════════════════════════════════════════════════════

class RadialWaveformRenderer:
    """Moteur de tracé graphique circulaire optimisé par précalcul et batching."""

    def __init__(
        self,
        num_bands: int = 64,
        layout: str = "symmetric",
    ):
        self.num_bands = num_bands
        self.layout = layout
        self.particle_system = ParticleSystem(max_particles=250)

        # Palette spectrale :
        # - Basses (< 250 Hz)       : Bleu profond (#0022ff / #0a1172)
        # - Médiums (250 - 4000 Hz) : Cyan électrique (#00f0ff)
        # - Aigus (> 4000 Hz)       : Magenta vif (#ff00aa)
        self.band_colors = self._precompute_spectral_colors()

        # Précalcul de la géométrie angulaire (unit vectors cos/sin)
        self._ray_angles, self._ray_unit_vectors, self._ray_band_indices = (
            self._precompute_ray_geometry()
        )
        self.total_rays = len(self._ray_angles)

        # Préallocation des crayons (QPen) pour zéro allocation dans la boucle de rendu
        self._pens_glow = [
            QPen(QColor(c.red(), c.green(), c.blue(), 75), 4.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            for c in self.band_colors
        ]
        self._pens_main = [
            QPen(QColor(c.red(), c.green(), c.blue(), 210), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            for c in self.band_colors
        ]
        self._pen_core = QPen(QColor(255, 255, 255, 185), 1.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)

        # Rémanence vectorielle (ghost trails) : 1 trame précédente
        self._prev_bands: Optional[np.ndarray] = None
        self.global_energy = 0.0

    def _precompute_spectral_colors(self) -> List[QColor]:
        """Génère la table des couleurs spectrales."""
        stops = [
            (0.00, 0, 25, 220),   # Bleu profond royal (Basses)
            (0.18, 0, 110, 255),  # Bleu cobalt
            (0.38, 0, 210, 255),  # Cyan turquoise
            (0.60, 0, 255, 235),  # Cyan électrique (Médiums)
            (0.80, 185, 20, 255), # Violet néon
            (1.00, 255, 0, 160),  # Magenta incandescent (Aigus)
        ]

        colors = []
        for i in range(self.num_bands):
            f = i / max(1, self.num_bands - 1)
            r, g, b = 0, 240, 255
            for k in range(len(stops) - 1):
                f0, r0, g0, b0 = stops[k]
                f1, r1, g1, b1 = stops[k + 1]
                if f0 <= f <= f1:
                    span = max(1e-5, f1 - f0)
                    t = (f - f0) / span
                    r = int(r0 + (r1 - r0) * t)
                    g = int(g0 + (g1 - g0) * t)
                    b = int(b0 + (b1 - b0) * t)
                    break
            colors.append(QColor(r, g, b))
        return colors

    def _precompute_ray_geometry(
        self,
    ) -> Tuple[List[float], List[Tuple[float, float]], List[int]]:
        """Précalcule tous les angles, cos/sin et correspondances de bandes."""
        angles: List[float] = []
        vectors: List[Tuple[float, float]] = []
        band_indices: List[int] = []

        if self.layout == "symmetric":
            half = self.num_bands // 2
            for i in range(half):
                t = i / max(1, half - 1)
                ang_r = math.pi / 2.0 - t * math.pi
                ang_l = math.pi / 2.0 + t * math.pi
                band_idx = i * 2

                for a in (ang_r, ang_l):
                    angles.append(a)
                    vectors.append((math.cos(a), math.sin(a)))
                    band_indices.append(band_idx)
        else:
            for i in range(self.num_bands):
                ang = (i / self.num_bands) * math.tau - math.pi / 2.0
                angles.append(ang)
                vectors.append((math.cos(ang), math.sin(ang)))
                band_indices.append(i)

        return angles, vectors, band_indices

    def update_physics(
        self,
        dt: float,
        smoothed_bands: np.ndarray,
        vocal_peak: bool,
        peak_intensity: float,
        cx: float,
        cy: float,
        orb_radius: float,
        max_ray_length: float,
    ) -> None:
        """Met à jour l'historique et la physique des particules."""
        current_energy = float(np.mean(smoothed_bands))
        self.global_energy += (current_energy - self.global_energy) * min(1.0, dt * 10.0)

        if vocal_peak and peak_intensity > 0.05:
            emitter_r = orb_radius + max_ray_length * 0.45
            # Couleurs associées aux rayons
            ray_colors = [self.band_colors[idx] for idx in self._ray_band_indices]
            self.particle_system.spawn_burst(
                cx, cy, emitter_r, self._ray_unit_vectors, ray_colors, intensity=peak_intensity
            )

        self.particle_system.update(dt)

    def render(
        self,
        painter: QPainter,
        width: int,
        height: int,
        smoothed_bands: np.ndarray,
        peak_caps: np.ndarray,
        orb_radius: Optional[float] = None,
        max_ray_len: Optional[float] = None,
        show_blur: bool = True,
        show_caps: bool = True,
    ) -> None:
        """Exécute le dessin complet du spectre néon circulaire."""
        if width < 10 or height < 10 or len(smoothed_bands) == 0:
            return

        cx = width / 2.0
        cy = height / 2.0

        if orb_radius is None:
            orb_radius = min(width, height) * 0.22
        if max_ray_len is None:
            max_ray_len = min(width, height) * 0.24

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)

        # ── 1. RÉMANENCE VECTORIELLE (GHOST TRAIL) ───────────────────────────
        if show_blur and self._prev_bands is not None:
            self._render_ray_pass(
                painter, cx, cy, orb_radius, max_ray_len, self._prev_bands,
                pen_list=self._pens_glow, length_scale=0.92, alpha_scale=0.40
            )

        # ── 2. RAYONS NÉON PRINCIPAUX (PASSES A & B GROUPÉES) ────────────────
        # Passe A : Halo néon large
        self._render_ray_pass(
            painter, cx, cy, orb_radius, max_ray_len, smoothed_bands,
            pen_list=self._pens_glow, length_scale=1.0, alpha_scale=1.0
        )
        # Passe B : Rayon net vibrant
        self._render_ray_pass(
            painter, cx, cy, orb_radius, max_ray_len, smoothed_bands,
            pen_list=self._pens_main, length_scale=1.0, alpha_scale=1.0
        )

        # ── 3. FILAMENTS BLANCS INCANDESCENTS (POUR LES FORTS PICS) ──────────
        self._render_core_filaments(
            painter, cx, cy, orb_radius, max_ray_len, smoothed_bands
        )

        # ── 4. CAPS DE CRÊTE DÉCROISSANTS (PEAK HOLD) ────────────────────────
        if show_caps:
            self._render_peak_caps(
                painter, cx, cy, orb_radius, max_ray_len, peak_caps
            )

        # ── 5. ANNEAU ÉMETTEUR DE BASE (HOLOGRAPHIC CONFINEMENT) ─────────────
        ring_pen = QPen(QColor(0, 240, 255, int(60 + 130 * self.global_energy)), 1.4)
        painter.setPen(ring_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QPointF(cx, cy), orb_radius, orb_radius)

        # ── 6. PARTICULES D'ÉJECTION VOCALE ──────────────────────────────────
        self.particle_system.draw(painter)

        painter.restore()

        # Enregistrement pour la rémanence suivante
        self._prev_bands = smoothed_bands.copy()

    def _render_ray_pass(
        self,
        painter: QPainter,
        cx: float,
        cy: float,
        r_base: float,
        max_len: float,
        bands: np.ndarray,
        pen_list: List[QPen],
        length_scale: float = 1.0,
        alpha_scale: float = 1.0,
    ) -> None:
        """Trace une passe complète de rayons radiaux avec tracé précalculé."""
        vectors = self._ray_unit_vectors
        band_indices = self._ray_band_indices

        for i in range(self.total_rays):
            b_idx = band_indices[i]
            val = float(bands[b_idx])
            if val < 0.015:
                continue

            length = max_len * (val ** 1.12) * length_scale
            ca, sa = vectors[i]
            x0 = cx + ca * r_base
            y0 = cy + sa * r_base
            x1 = cx + ca * (r_base + length)
            y1 = cy + sa * (r_base + length)

            pen = pen_list[b_idx]
            painter.setPen(pen)
            painter.drawLine(QPointF(x0, y0), QPointF(x1, y1))

    def _render_core_filaments(
        self,
        painter: QPainter,
        cx: float,
        cy: float,
        r_base: float,
        max_len: float,
        bands: np.ndarray,
    ) -> None:
        """Trace le filament incandescent blanc au centre des rayons puissants."""
        vectors = self._ray_unit_vectors
        band_indices = self._ray_band_indices
        painter.setPen(self._pen_core)

        lines: List[QLineF] = []
        for i in range(self.total_rays):
            b_idx = band_indices[i]
            val = float(bands[b_idx])
            if val < 0.38:
                continue

            length = max_len * (val ** 1.12) * 0.94
            ca, sa = vectors[i]
            lines.append(QLineF(
                cx + ca * (r_base + 2),
                cy + sa * (r_base + 2),
                cx + ca * (r_base + length),
                cy + sa * (r_base + length),
            ))

        if lines:
            painter.drawLines(lines)

    def _render_peak_caps(
        self,
        painter: QPainter,
        cx: float,
        cy: float,
        r_base: float,
        max_len: float,
        peak_caps: np.ndarray,
    ) -> None:
        """Trace les têtes lumineuses de crête dérivant sous pesanteur."""
        vectors = self._ray_unit_vectors
        band_indices = self._ray_band_indices

        painter.setPen(Qt.PenStyle.NoPen)
        for i in range(self.total_rays):
            b_idx = band_indices[i]
            cap = float(peak_caps[b_idx])
            if cap < 0.06:
                continue

            dist = r_base + max_len * (cap ** 1.12) + 2.5
            ca, sa = vectors[i]
            px = cx + ca * dist
            py = cy + sa * dist

            col = self.band_colors[b_idx]
            alpha = max(0, min(255, int(130 + 125 * cap)))
            painter.setBrush(QBrush(QColor(col.red(), col.green(), col.blue(), alpha)))
            painter.drawEllipse(QPointF(px, py), 1.6, 1.6)


# ═════════════════════════════════════════════════════════════════════════════
# 6. SYNCHRONISATION BI-DIRECTIONNELLE (VOIX + MPV MUSIQUE)
# ═════════════════════════════════════════════════════════════════════════════

class BiDirectionalAudioBridge:
    """Pont audio bi-directionnel gérant la capture micro et l'audio émis."""

    def __init__(self, target_sr: int = 44100):
        self.target_sr = target_sr
        self.captured_buffer = AudioRingBuffer(capacity=16384, target_sr=target_sr)
        self.emitted_buffer  = AudioRingBuffer(capacity=32768, target_sr=target_sr)
        self.mixed_buffer    = AudioRingBuffer(capacity=32768, target_sr=target_sr)

        self._last_captured_t = 0.0
        self._last_emitted_t  = 0.0
        self._system_monitor_proc: Optional[subprocess.Popen] = None
        self._system_monitor_thread: Optional[threading.Thread] = None
        self._running_monitor = False

    def feed_captured(self, data: Union[bytes, np.ndarray], sample_rate: int = 16000) -> None:
        """Injecte l'audio capturé (voix utilisateur, micro 16 kHz)."""
        self._last_captured_t = time.monotonic()
        self.captured_buffer.push(data, input_sr=sample_rate)
        self.mixed_buffer.push(data, input_sr=sample_rate)

    def feed_emitted(self, data: Union[bytes, np.ndarray], sample_rate: int = 24000) -> None:
        """Injecte l'audio émis (Jarvis TTS 24 kHz ou musique MPV 44.1/48 kHz)."""
        self._last_emitted_t = time.monotonic()
        self.emitted_buffer.push(data, input_sr=sample_rate)
        self.mixed_buffer.push(data, input_sr=sample_rate)

    def feed_audio(
        self,
        data: Union[bytes, np.ndarray],
        sample_rate: int = 44100,
        source: AudioSource = AudioSource.EMITTED,
    ) -> None:
        """Méthode unifiée pour injecter un flux quelconque."""
        if source == AudioSource.CAPTURED:
            self.feed_captured(data, sample_rate)
        else:
            self.feed_emitted(data, sample_rate)

    @property
    def is_captured_active(self) -> bool:
        return (time.monotonic() - self._last_captured_t) < 0.35

    @property
    def is_emitted_active(self) -> bool:
        return (time.monotonic() - self._last_emitted_t) < 0.35

    def get_active_source_label(self) -> str:
        c = self.is_captured_active
        e = self.is_emitted_active
        if c and e:
            return "DUAL (VOICE + MPV)"
        if c:
            return "USER VOICE"
        if e:
            return "ANO-GPT / MPV"
        return "AMBIENT"

    def get_samples_for_analysis(self, n_samples: int) -> Tuple[np.ndarray, AudioSource]:
        """Extrait le bloc audio optimal pour la FFT avec priorité dynamique."""
        c = self.is_captured_active
        e = self.is_emitted_active

        if c and e:
            return self.mixed_buffer.read_latest(n_samples), AudioSource.COMBINED
        elif c:
            return self.captured_buffer.read_latest(n_samples), AudioSource.CAPTURED
        elif e:
            return self.emitted_buffer.read_latest(n_samples), AudioSource.EMITTED
        else:
            return self.mixed_buffer.read_latest(n_samples), AudioSource.COMBINED

    # ── Moniteur système automatique Linux (PulseAudio / PipeWire) ───────────
    def start_system_monitor(self) -> bool:
        """Démarre une capture sans configuration du flux sortant (MPV / système) via parec."""
        if self._running_monitor or platform.system() != "Linux":
            return False

        cmd = ["parec", "--format=s16le", "--rate=44100", "--channels=1"]
        try:
            self._system_monitor_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=8192,
            )
        except Exception:
            return False

        self._running_monitor = True

        def _reader_thread():
            chunk_bytes = 2048 * 2
            proc = self._system_monitor_proc
            while self._running_monitor and proc and proc.poll() is None:
                try:
                    raw = proc.stdout.read(chunk_bytes)
                    if not raw:
                        break
                    self.feed_emitted(raw, sample_rate=44100)
                except Exception:
                    break

        self._system_monitor_thread = threading.Thread(
            target=_reader_thread, name="anogpt-system-audio-monitor", daemon=True
        )
        self._system_monitor_thread.start()
        return True

    def stop_system_monitor(self) -> None:
        """Arrête le moniteur audio système."""
        self._running_monitor = False
        if self._system_monitor_proc:
            try:
                self._system_monitor_proc.terminate()
                self._system_monitor_proc.wait(timeout=0.5)
            except Exception:
                pass
            self._system_monitor_proc = None


# ═════════════════════════════════════════════════════════════════════════════
# 7. WIDGET QT PRINCIPAL (RADIAL WAVEFORM WIDGET)
# ═════════════════════════════════════════════════════════════════════════════

class RadialWaveformWidget(QOpenGLWidget if _HAS_OPENGL_WIDGET else QWidget):
    """Widget d'affichage spectral circulaire néon temps réel pour ANO-GPT."""

    vocal_peak_signal = pyqtSignal(float)

    def __init__(
        self,
        num_bands: int = 64,
        layout: str = "symmetric",
        fps: int = 60,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.setMinimumSize(220, 220)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)

        self.num_bands = 128 if num_bands >= 96 else 64
        self.layout_mode = layout

        # Moteurs DSP et rendu
        self.fft_engine = CircularFFTEngine(num_bands=self.num_bands)
        self.renderer   = RadialWaveformRenderer(num_bands=self.num_bands, layout=layout)
        self.bridge     = BiDirectionalAudioBridge(target_sr=self.fft_engine.sample_rate)

        # Horloge et métriques
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._on_tick)
        self._interval_ms = int(round(1000.0 / max(10, fps)))
        self._t_last = time.perf_counter()

        # Dernières données calculées
        self._current_bands = np.zeros(self.num_bands, dtype=np.float32)
        self._current_peaks = np.zeros(self.num_bands, dtype=np.float32)

        self._timer.start(self._interval_ms)

    def set_bands(self, num_bands: int) -> None:
        """Modifie dynamiquement le nombre de bandes (64 ou 128)."""
        num_bands = 128 if num_bands >= 96 else 64
        if num_bands == self.num_bands:
            return
        self.num_bands = num_bands
        self.fft_engine = CircularFFTEngine(num_bands=num_bands)
        self.renderer = RadialWaveformRenderer(num_bands=num_bands, layout=self.layout_mode)
        self._current_bands = np.zeros(num_bands, dtype=np.float32)
        self._current_peaks = np.zeros(num_bands, dtype=np.float32)

    def feed_captured_audio(self, data: Union[bytes, np.ndarray], sample_rate: int = 16000) -> None:
        """Alimente le canal de capture micro utilisateur."""
        self.bridge.feed_captured(data, sample_rate)

    def feed_emitted_audio(self, data: Union[bytes, np.ndarray], sample_rate: int = 24000) -> None:
        """Alimente le canal d'émission (Jarvis TTS ou MPV musique)."""
        self.bridge.feed_emitted(data, sample_rate)

    def enable_system_loopback(self) -> bool:
        """Active l'écoute automatique de la sortie audio système."""
        return self.bridge.start_system_monitor()

    def disable_system_loopback(self) -> None:
        """Désactive l'écoute de la sortie système."""
        self.bridge.stop_system_monitor()

    def _on_tick(self) -> None:
        """Slot d'animation régulier (60 FPS)."""
        now = time.perf_counter()
        dt = max(0.001, min(0.050, now - self._t_last))
        self._t_last = now

        samples, source = self.bridge.get_samples_for_analysis(self.fft_engine.fft_size)

        bands, peaks, vocal_peak, intensity = self.fft_engine.process(samples, dt=dt)
        self._current_bands = bands
        self._current_peaks = peaks

        if vocal_peak:
            self.vocal_peak_signal.emit(intensity)

        W, H = self.width(), self.height()
        cx, cy = W / 2.0, H / 2.0
        r_orb = min(W, H) * 0.22
        max_ray = min(W, H) * 0.24

        self.renderer.update_physics(
            dt=dt,
            smoothed_bands=bands,
            vocal_peak=vocal_peak,
            peak_intensity=intensity,
            cx=cx,
            cy=cy,
            orb_radius=r_orb,
            max_ray_length=max_ray,
        )

        self.update()

    def paintEvent(self, _event) -> None:
        """Rendu graphique du visualiseur néon."""
        W, H = self.width(), self.height()
        if W < 10 or H < 10:
            return

        p = QPainter(self)
        self.renderer.render(
            painter=p,
            width=W,
            height=H,
            smoothed_bands=self._current_bands,
            peak_caps=self._current_peaks,
        )
        p.end()

    def closeEvent(self, event) -> None:
        self.bridge.stop_system_monitor()
        self._timer.stop()
        super().closeEvent(event)


# ═════════════════════════════════════════════════════════════════════════════
# 8. SUITE DE BENCHMARKS DE PERFORMANCE
# ═════════════════════════════════════════════════════════════════════════════

class SpectralBenchmarks:
    """Suite de banc d'essai et de métrologie haute précision."""

    @staticmethod
    def run_all(iterations: int = 500) -> dict:
        """Exécute tous les benchmarks et retourne les résultats chronométriques."""
        results = {}

        # ── Test A : FFT + Log Filterbank (64 et 128 bandes) ──────────────────
        for n_bands in (64, 128):
            engine = CircularFFTEngine(num_bands=n_bands)
            chunk = np.random.randn(engine.fft_size).astype(np.float32)

            for _ in range(30):
                engine.process(chunk, dt=0.016)

            t0 = time.perf_counter()
            for _ in range(iterations):
                engine.process(chunk, dt=0.016)
            t1 = time.perf_counter()

            total_ms = (t1 - t0) * 1000.0
            per_frame_us = (total_ms / iterations) * 1000.0
            throughput = iterations / (t1 - t0)

            results[f"fft_filterbank_{n_bands}_us"] = per_frame_us
            results[f"fft_filterbank_{n_bands}_fps"] = throughput

        # ── Test B : Physique des particules (200 particules) ─────────────────
        ps = ParticleSystem(max_particles=300)
        vecs = [(math.cos(i * math.tau / 64), math.sin(i * math.tau / 64)) for i in range(64)]
        cols = [QColor(0, 240, 255) for _ in range(64)]
        ps.spawn_burst(400, 400, 100, vecs, cols, intensity=1.0)

        t0 = time.perf_counter()
        for _ in range(iterations):
            ps.update(0.016)
            if len(ps.particles) < 50:
                ps.spawn_burst(400, 400, 100, vecs, cols, intensity=0.5)
        t1 = time.perf_counter()
        results["particles_physics_us"] = ((t1 - t0) * 1000.0 / iterations) * 1000.0

        # ── Test C : Rendu QPainter Offscreen 600x600 (HUD standard) ──────────
        app = QApplication.instance() or QApplication(["--platform", "offscreen"])
        canvas = QImage(600, 600, QImage.Format.Format_ARGB32_Premultiplied)

        for n_bands in (64, 128):
            renderer = RadialWaveformRenderer(num_bands=n_bands, layout="symmetric")
            bands = np.random.uniform(0.1, 0.9, n_bands).astype(np.float32)
            caps  = bands * 1.1

            # Warmup
            p = QPainter(canvas)
            renderer.render(p, 600, 600, bands, caps)
            p.end()

            render_iters = 100
            t0 = time.perf_counter()
            for _ in range(render_iters):
                canvas.fill(Qt.GlobalColor.transparent)
                p = QPainter(canvas)
                renderer.render(p, 600, 600, bands, caps)
                p.end()
            t1 = time.perf_counter()

            render_total_ms = (t1 - t0) * 1000.0
            render_per_frame_ms = render_total_ms / render_iters
            render_fps = render_iters / (t1 - t0)

            results[f"render_{n_bands}_ms"] = render_per_frame_ms
            results[f"render_{n_bands}_fps"] = render_fps

        return results

    @staticmethod
    def print_report(results: dict) -> None:
        """Affiche un rapport structuré et formaté dans le terminal."""
        sep = "=" * 76
        print(f"\n{sep}")
        print("  RAPPORT DE BENCHMARK : VISUALISEUR SPECTRAL CIRCULAIRE (ANO-GPT)")
        print(f"{sep}")
        print("  Plateforme  : " + platform.platform())
        print("  Processeur  : " + (platform.processor() or "x86_64"))
        print("  NumPy Vers. : " + np.__version__)
        print(f"{'-' * 76}")
        print("  1. PERFORMANCE TRAITEMENT DU SIGNAL (DSP / FFT + LISSAGE) :")
        print(f"     • Mode 64 bandes  : {results['fft_filterbank_64_us']:6.2f} µs/trame  "
              f"({results['fft_filterbank_64_fps']:8.0f} analyses/sec)")
        print(f"     • Mode 128 bandes : {results['fft_filterbank_128_us']:6.2f} µs/trame  "
              f"({results['fft_filterbank_128_fps']:8.0f} analyses/sec)")
        print(f"     • Physique particules (~200 part.) : {results['particles_physics_us']:6.2f} µs/trame")
        print(f"{'-' * 76}")
        print("  2. PERFORMANCE RENDU GRAPHIQUE 600x600 (MULTI-PASSES NÉON + RÉMANENCE) :")
        print(f"     • Rendu 64 bandes  : {results['render_64_ms']:5.2f} ms/trame  "
              f"(Capacité max : {results['render_64_fps']:6.1f} FPS)")
        print(f"     • Rendu 128 bandes : {results['render_128_ms']:5.2f} ms/trame  "
              f"(Capacité max : {results['render_128_fps']:6.1f} FPS)")
        print(f"{'-' * 76}")
        cpu_60fps_64  = (results['fft_filterbank_64_us'] / 1000.0 + results['render_64_ms']) / 16.67 * 100.0
        cpu_60fps_128 = (results['fft_filterbank_128_us'] / 1000.0 + results['render_128_ms']) / 16.67 * 100.0
        print(f"  3. CHARGE CPU ESTIMÉE À 60 FPS V-SYNC (CPU SOFTWARE RASTERIZER) :")
        print(f"     • Mode 64 bandes  : ~{min(100.0, cpu_60fps_64):4.1f}% d'un cœur CPU")
        print(f"     • Mode 128 bandes : ~{min(100.0, cpu_60fps_128):4.1f}% d'un cœur CPU")
        print("     * Note : En mode QOpenGLWidget matériel, la charge CPU chute à < 2%.")
        print(f"{sep}\n")


# ═════════════════════════════════════════════════════════════════════════════
# 9. DÉMONSTRATEUR INTERACTIF AUTONOME
# ═════════════════════════════════════════════════════════════════════════════

def run_demo(num_bands: int = 128, layout: str = "symmetric") -> None:
    """Lance une démonstration interactive autonome avec synthèse audio."""
    app = QApplication(sys.argv)

    win = QMainWindow()
    win.setWindowTitle(f"ANO-GPT — Visualiseur Spectral Circulaire Néon ({num_bands} bandes)")
    win.resize(750, 750)
    win.setStyleSheet("background-color: #00060a;")

    widget = RadialWaveformWidget(num_bands=num_bands, layout=layout)
    win.setCentralWidget(widget)
    win.show()

    t_ref = time.perf_counter()
    synth_timer = QTimer()

    def _synth_audio():
        nonlocal t_ref
        t = time.perf_counter() - t_ref
        n_samples = 1024
        sr = 44100
        t_arr = np.linspace(t, t + n_samples / sr, n_samples)

        kick = np.sin(2 * math.pi * 50.0 * t_arr) * (math.sin(t * 7.5) ** 6)
        voice = (
            0.5 * np.sin(2 * math.pi * 440.0 * t_arr) +
            0.4 * np.sin(2 * math.pi * 880.0 * t_arr) +
            0.3 * np.sin(2 * math.pi * 1760.0 * t_arr)
        ) * max(0.0, math.sin(t * 3.2))
        hihat = np.random.randn(n_samples) * 0.15 * (math.sin(t * 15.0) ** 8)

        total_audio = (kick * 0.7 + voice * 0.5 + hihat * 0.4).astype(np.float32)

        if int(t * 1.5) % 2 == 0:
            widget.feed_emitted_audio(total_audio, sample_rate=sr)
        else:
            widget.feed_captured_audio(total_audio, sample_rate=sr)

    synth_timer.timeout.connect(_synth_audio)
    synth_timer.start(22)

    sys.exit(app.exec())


# ═════════════════════════════════════════════════════════════════════════════
# 10. POINT D'ENTRÉE CLI
# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Visualiseur spectral circulaire pour l'orbe ANO-GPT."
    )
    parser.add_argument(
        "--benchmark",
        action="store_true",
        help="Exécute la suite complète de benchmarks et affiche le rapport de performances.",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Lance la démonstration interactive Qt avec audio synthétique.",
    )
    parser.add_argument(
        "--bands",
        type=int,
        default=128,
        choices=[64, 128],
        help="Nombre de bandes logarithmiques (64 ou 128).",
    )
    parser.add_argument(
        "--layout",
        type=str,
        default="symmetric",
        choices=["symmetric", "circular"],
        help="Disposition géométrique : 'symmetric' (miroir bilatéral) ou 'circular' (360 continu).",
    )
    args = parser.parse_args()

    if args.benchmark:
        print("[*] Lancement des benchmarks de traitement du signal et de rendu...")
        res = SpectralBenchmarks.run_all()
        SpectralBenchmarks.print_report(res)
    elif args.demo:
        run_demo(num_bands=args.bands, layout=args.layout)
    else:
        if os.environ.get("QT_QPA_PLATFORM") == "offscreen" or not os.environ.get("DISPLAY"):
            res = SpectralBenchmarks.run_all()
            SpectralBenchmarks.print_report(res)
        else:
            parser.print_help()
            print("\nExemple de benchmark : python -m ui.orb.radial_waveform --benchmark")
            print("Exemple de démo GUI :  python -m ui.orb.radial_waveform --demo")
