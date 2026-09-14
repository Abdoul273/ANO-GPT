"""Sound design engine pour l'interface HUD Hacker de ANO-GPT.

Synthétise et joue des effets sonores cybernétiques / futuristes haute-fidélité
sans bloquer la boucle d'événements Qt ni la boucle audio principale.
"""

from __future__ import annotations

import math
import logging
import threading
import wave
from pathlib import Path
from typing import Optional

import numpy as np

# Répertoire de stockage des effets sonores pré-générés
SOUNDS_DIR = Path(__file__).resolve().parent.parent / "assets" / "sounds"
SAMPLE_RATE = 44100


class HudSoundEngine:
    """Moteur audio dédié aux effets sonores du HUD Hacker / Écran de bienvenue."""

    _instance: Optional[HudSoundEngine] = None
    _lock = threading.Lock()

    def __init__(self):
        self._muted = False
        self._volume = 0.85
        self._buffers: dict[str, np.ndarray] = {}
        self._sd = None
        self._has_audio_device = False
        self._play_lock = threading.Lock()
        self._init_audio_device()

    @classmethod
    def get(cls) -> HudSoundEngine:
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def _init_audio_device(self) -> None:
        try:
            import sounddevice as sd
            self._sd = sd
            self._has_audio_device = True
        except Exception:
            self._sd = None
            self._has_audio_device = False

    @property
    def muted(self) -> bool:
        return self._muted

    @muted.setter
    def muted(self, val: bool) -> None:
        self._muted = bool(val)

    def toggle_mute(self) -> bool:
        self._muted = not self._muted
        return self._muted

    @property
    def volume(self) -> float:
        return self._volume

    @volume.setter
    def volume(self, val: float) -> None:
        self._volume = max(0.0, min(1.0, float(val)))

    # ── Synthèse des sons futuristes haute-fidélité ───────────────────────────

    def _save_wav(self, path: Path, left: np.ndarray, right: np.ndarray) -> None:
        left = np.clip(left, -0.98, 0.98)
        right = np.clip(right, -0.98, 0.98)
        stereo = np.column_stack([left, right])
        int_samples = (stereo * 32767).astype(np.int16)
        with wave.open(str(path), "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(int_samples.tobytes())

    def _generate_boot_surge(self) -> tuple[np.ndarray, np.ndarray]:
        """Monstrueux boot surge cybernétique : sub-bass rumble + turbine spin-up + sparkle bloom."""
        dur = 2.4
        t = np.linspace(0, dur, int(SAMPLE_RATE * dur), endpoint=False)

        # 1. Sub-bass cinematic rumble (35 Hz -> 75 Hz)
        f_sub = 36 + 34 * np.sin(np.pi * t / dur)
        sub = 0.40 * np.sin(2 * np.pi * f_sub * t) * np.exp(-1.1 * t)

        # 2. Turbine cyber rotor sweep (90 Hz -> 1750 Hz)
        t_spin = np.clip(t / 1.6, 0, 1)
        f_spin = 95 + 1650 * (t_spin ** 2.2)
        amp_mod = 1.0 + 0.3 * np.sin(2 * np.pi * 32 * t)
        turbine = 0.28 * np.sin(2 * np.pi * f_spin * t) * amp_mod * np.exp(-1.4 * t)

        # 3. Holographic resonant spark at t=1.1s
        spark_t = np.maximum(0, t - 1.1)
        spark = (
            0.18 * np.sin(2 * np.pi * 2400 * spark_t) * np.exp(-16 * spark_t) +
            0.12 * np.sin(2 * np.pi * 3600 * spark_t) * np.exp(-22 * spark_t)
        ) * (t >= 1.1)

        total = sub + turbine + spark
        total = np.tanh(total * 1.35) * 0.82

        pan = np.linspace(-0.6, 0.6, len(t))
        left = total * (0.5 - 0.5 * pan)
        right = total * (0.5 + 0.5 * pan)
        return left, right

    def _generate_data_chirp(self, seed: int = 1) -> tuple[np.ndarray, np.ndarray]:
        """Micro-blip hacker / décodage de terminal digital."""
        dur = 0.045
        t = np.linspace(0, dur, int(SAMPLE_RATE * dur), endpoint=False)
        base_f = 1600 + (seed * 420)
        mod_f = 600 + (seed * 180)
        # Modulation FM
        modulator = 0.4 * np.sin(2 * np.pi * mod_f * t)
        carrier = 0.22 * np.sin(2 * np.pi * (base_f + modulator * 800) * t)
        # Noise burst
        noise = (np.random.RandomState(seed).rand(len(t)) - 0.5) * 0.06
        env = np.exp(-48 * t)
        total = (carrier + noise) * env
        pan = 0.2 * (seed - 2)
        left = total * (0.5 - 0.5 * pan)
        right = total * (0.5 + 0.5 * pan)
        return left, right

    def _generate_radar_ping(self) -> tuple[np.ndarray, np.ndarray]:
        """Sonar radar HUD futuriste à résonance harmonique cristalline."""
        dur = 0.42
        t = np.linspace(0, dur, int(SAMPLE_RATE * dur), endpoint=False)
        f0 = 1180.0
        wave1 = 0.32 * np.sin(2 * np.pi * f0 * t)
        wave2 = 0.16 * np.sin(2 * np.pi * (f0 * 2) * t)
        wave3 = 0.08 * np.sin(2 * np.pi * (f0 * 3) * t)
        env = np.exp(-12.5 * t)
        total = (wave1 + wave2 + wave3) * env

        # Léger délai stéréo pour effet spatial
        left = total
        delay = int(0.015 * SAMPLE_RATE)
        right = np.zeros_like(left)
        right[delay:] = left[:-delay] * 0.85
        right[:delay] = left[:delay] * 0.5
        return left, right

    def _generate_hud_hover(self) -> tuple[np.ndarray, np.ndarray]:
        """Bip tactile ultraléger au survol d'un élément HUD."""
        dur = 0.035
        t = np.linspace(0, dur, int(SAMPLE_RATE * dur), endpoint=False)
        f = np.linspace(2400, 3600, len(t))
        wave_sig = 0.14 * np.sin(2 * np.pi * f * t)
        env = np.exp(-55 * t)
        total = wave_sig * env
        return total, total

    def _generate_access_granted(self) -> tuple[np.ndarray, np.ndarray]:
        """Accord polyphonique triomphal cybernétique (D Maj9) + chime cristallin."""
        dur = 2.0
        t = np.linspace(0, dur, int(SAMPLE_RATE * dur), endpoint=False)

        # Fréquences de l'accord cybernétique D Maj9
        # D3 (146.83), A3 (220.0), F#4 (369.99), C#5 (554.37), E5 (659.25), A5 (880.0)
        chord_freqs = [146.83, 220.0, 369.99, 554.37, 659.25, 880.0]
        weights = [0.28, 0.22, 0.20, 0.18, 0.15, 0.12]

        chord_l = np.zeros_like(t)
        chord_r = np.zeros_like(t)

        for i, (freq, w) in enumerate(zip(chord_freqs, weights)):
            # Léger arpège temporel (stagger 30ms)
            offset = int(i * 0.030 * SAMPLE_RATE)
            t_sub = t[offset:] if offset < len(t) else np.array([])
            if len(t_sub) == 0:
                continue

            # Harmoniques analogiques
            layer = (
                w * np.sin(2 * np.pi * freq * t_sub) +
                (w * 0.35) * np.sin(2 * np.pi * (freq * 2) * t_sub)
            )
            # Enveloppe avec attaque douce et extinction longue
            att = np.minimum(1.0, np.linspace(0, 4, len(t_sub)))
            dec = np.exp(-2.4 * t_sub)
            voice = layer * att * dec

            pan = math.sin(i * 1.2) * 0.4
            seg_l = voice * (0.5 - 0.5 * pan)
            seg_r = voice * (0.5 + 0.5 * pan)

            chord_l[offset:offset + len(seg_l)] += seg_l
            chord_r[offset:offset + len(seg_r)] += seg_r

        # Sub-bass punch at start (52 Hz)
        sub_env = np.exp(-6 * t)
        sub = 0.32 * np.sin(2 * np.pi * 52 * t) * sub_env
        chord_l += sub
        chord_r += sub

        total_l = np.tanh(chord_l * 1.2) * 0.85
        total_r = np.tanh(chord_r * 1.2) * 0.85
        return total_l, total_r

    def _ensure_sound_assets(self) -> None:
        """Génère les fichiers WAV s'ils sont absents, et pré-charge les tampons."""
        SOUNDS_DIR.mkdir(parents=True, exist_ok=True)
        assets = {
            "boot_surge.wav": self._generate_boot_surge,
            "data_chirp_1.wav": lambda: self._generate_data_chirp(1),
            "data_chirp_2.wav": lambda: self._generate_data_chirp(2),
            "data_chirp_3.wav": lambda: self._generate_data_chirp(3),
            "radar_ping.wav": self._generate_radar_ping,
            "hud_hover.wav": self._generate_hud_hover,
            "access_granted.wav": self._generate_access_granted,
        }

        for fname, gen_func in assets.items():
            path = SOUNDS_DIR / fname
            if not path.exists() or path.stat().st_size == 0:
                try:
                    left, right = gen_func()
                    self._save_wav(path, left, right)
                except Exception as exc:
                    print(f"[HudSound] Erreur génération {fname}: {exc}")

            # Charger en mémoire (float32 numpy array normalisé)
            try:
                with wave.open(str(path), "rb") as w:
                    n_channels = w.getnchannels()
                    n_frames = w.getnframes()
                    raw = w.readframes(n_frames)
                    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                    if n_channels == 2:
                        audio = audio.reshape(-1, 2)
                    name = fname.replace(".wav", "")
                    self._buffers[name] = audio
            except Exception as exc:
                print(f"[HudSound] Erreur lecture tampon {fname}: {exc}")

    # ── Lecture audio non-bloquante ──────────────────────────────────────────

    def play(self, sound_name: str, volume_mult: float = 1.0) -> None:
        """Joue un son de façon entièrement asynchrone et non-bloquante."""
        if self._muted or not self._has_audio_device or self._sd is None:
            return

        # Les effets sont facultatifs : ignorer les bips superposés plutôt que
        # créer des threads et interrompre l'enregistrement sounddevice global.
        if not self._play_lock.acquire(blocking=False):
            return

        def _worker():
            try:
                if not self._buffers:
                    self._ensure_sound_assets()
                buf = self._buffers.get(sound_name)
                if buf is None or self._muted:
                    return
                gain = self._volume * volume_mult
                samples = buf * gain
                with self._sd.OutputStream(samplerate=SAMPLE_RATE, channels=2, dtype="float32") as stream:
                    stream.write(samples)
            except Exception as exc:
                logging.getLogger(__name__).debug("Effet HUD indisponible : %s", exc)
            finally:
                self._play_lock.release()

        try:
            threading.Thread(target=_worker, name="hud-sfx", daemon=True).start()
        except Exception as exc:
            self._play_lock.release()
            logging.getLogger(__name__).debug("Worker HUD indisponible : %s", exc)

    def play_boot_surge(self) -> None:
        self.play("boot_surge", volume_mult=1.0)

    def play_chirp(self, seed: int = 1) -> None:
        s_id = ((abs(seed) % 3) + 1)
        self.play(f"data_chirp_{s_id}", volume_mult=0.75)

    def play_radar_ping(self) -> None:
        self.play("radar_ping", volume_mult=0.85)

    def play_hover(self) -> None:
        self.play("hud_hover", volume_mult=0.5)

    def play_access_granted(self) -> None:
        self.play("access_granted", volume_mult=1.0)


_sound_engine: Optional[HudSoundEngine] = None


def get_hud_sound() -> HudSoundEngine:
    global _sound_engine
    if _sound_engine is None:
        _sound_engine = HudSoundEngine.get()
    return _sound_engine
