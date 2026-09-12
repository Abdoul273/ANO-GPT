#!/usr/bin/env python3
"""Diagnostic reproductible du micro ANO-GPT, sans sauvegarder la voix.

Capture par défaut cinq secondes avec le même pont PortAudio/Pulse que
ANO-GPT, puis affiche niveau et énergie spectrale. Le code retourne 2 si la
grave bande (0-250 Hz) dépasse 50 % : cela signale typiquement un rumble.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

# Permet `python scripts/check_audio_chain.py` depuis n'importe quel dossier.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import audio_router
from core.stt_audio import signal_metrics

SAMPLE_RATE = 16000
BANDS = ((0, 250), (250, 500), (500, 2000), (2000, 4000), (4000, 8000))


def band_energy_percent(samples: np.ndarray, sample_rate: int = SAMPLE_RATE) -> dict[str, float]:
    samples = np.asarray(samples, dtype=np.float64)
    if samples.size < 2:
        return {f"{lo}-{hi}Hz": 0.0 for lo, hi in BANDS}
    # Fenêtre Hann : évite que les bords de capture artificiels ne gonflent les graves.
    spectrum = np.fft.rfft(samples * np.hanning(samples.size))
    power = np.abs(spectrum) ** 2
    freqs = np.fft.rfftfreq(samples.size, 1.0 / sample_rate)
    total = float(power.sum())
    if total <= 0:
        return {f"{lo}-{hi}Hz": 0.0 for lo, hi in BANDS}
    return {
        f"{lo}-{hi}Hz": round(float(power[(freqs >= lo) & (freqs < hi)].sum()) / total * 100, 1)
        for lo, hi in BANDS
    }


def _read_wav(path: Path) -> tuple[np.ndarray, int]:
    import wave
    with wave.open(str(path), "rb") as stream:
        if stream.getnchannels() != 1 or stream.getsampwidth() != 2:
            raise ValueError("WAV PCM16 mono requis")
        rate = stream.getframerate()
        audio = np.frombuffer(stream.readframes(stream.getnframes()), dtype="<i2")
    return audio, rate


def _capture(seconds: float) -> tuple[np.ndarray, int]:
    import sounddevice as sd
    device = audio_router.portaudio_input_device(sd)
    frames = int(seconds * SAMPLE_RATE)
    print(f"Capture {seconds:.1f}s via {device or 'défaut PortAudio'} — parlez normalement.")
    data = sd.rec(frames, samplerate=SAMPLE_RATE, channels=1, dtype="int16", device=device)
    sd.wait()
    return data.reshape(-1), SAMPLE_RATE


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=5.0)
    parser.add_argument("--wav", type=Path, help="analyse un WAV PCM16 mono, sans capturer")
    args = parser.parse_args()
    if args.seconds <= 0 or args.seconds > 30:
        parser.error("--seconds doit être entre 0 et 30")
    try:
        audio, rate = _read_wav(args.wav) if args.wav else _capture(args.seconds)
    except Exception as exc:
        print(f"ERREUR capture : {exc}", file=sys.stderr)
        return 1
    if rate != SAMPLE_RATE:
        print(f"ERREUR : fréquence {rate} Hz ; 16000 Hz attendu.", file=sys.stderr)
        return 1
    pcm = np.asarray(audio, dtype="<i2").tobytes()
    metrics = signal_metrics(pcm)
    bands = band_energy_percent(audio, rate)
    default = audio_router._system_default_source_name() or "inconnue"
    print(f"source Pulse : {default}")
    print("signal : " + " | ".join(f"{key}={value}" for key, value in metrics.items()))
    print("énergie : " + " | ".join(f"{key}={value:.1f}%" for key, value in bands.items()))
    low = bands["0-250Hz"]
    if low > 50:
        print(f"ALERTE : {low:.1f}% sous 250 Hz (>50%) — rumble probable.")
        return 2
    print("OK : répartition grave compatible avec une parole exploitable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
