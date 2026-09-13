#!/usr/bin/env python3
"""
Wake-word calibration for ANO-GPT.

A small offline model transcribes an out-of-vocabulary name like "Ano" into
whatever real French words sound closest, and which words those are depends on
your voice, accent and microphone. That mapping cannot be guessed — it has to be
measured on your actual hardware, which is what this tool does.

Run it, say the wake phrase a few times, and it reports what Vosk actually heard
and the similarity score each attempt produced. Add the recurring transcriptions
to DEFAULT_PHRASES in core/wake_word.py (or lower the threshold) so detection
matches your real voice.

    python scripts/calibrate_wake_word.py            # listen and report
    python scripts/calibrate_wake_word.py --seconds 20
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import sounddevice as sd

from core.wake_word import DEFAULT_MODEL, WakeWordDetector, _normalise

SAMPLE_RATE = 16000
CHUNK = 1024


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=25, help="listening duration")
    ap.add_argument("--threshold", type=float, default=None,
                    help="override detection threshold for this run")
    args = ap.parse_args()

    if not Path(DEFAULT_MODEL).is_dir():
        print(f"❌ Vosk model missing: {DEFAULT_MODEL}")
        print("   Run: bash scripts/install_wake_word.sh")
        return 1

    det = WakeWordDetector(threshold=args.threshold) if args.threshold \
        else WakeWordDetector()
    if not det.available:
        print("❌ Wake-word detector unavailable (vosk not importable?)")
        return 1

    from vosk import KaldiRecognizer

    # Independent open recogniser so we can show the raw transcription
    # alongside the detector's verdict.
    probe = KaldiRecognizer(det._model, SAMPLE_RATE)

    print("=" * 62)
    print("  Calibration — parle maintenant. Dis « Ano » plusieurs fois.")
    print(f"  Seuil actuel : {det.threshold}   Durée : {args.seconds}s")
    print("=" * 62)

    heard: list[tuple[str, float, bool]] = []
    deadline = time.monotonic() + args.seconds

    def report(text: str) -> None:
        if not text.strip():
            return
        score = det._score(text)
        fired = score >= det.threshold
        heard.append((text, score, fired))
        mark = "🔔 DÉTECTÉ" if fired else "  ignoré "
        print(f"{mark}  score={score:.2f}  « {text} »")

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                        blocksize=CHUNK) as stream:
        while time.monotonic() < deadline:
            data, overflowed = stream.read(CHUNK)
            if overflowed:
                continue
            pcm = np.ascontiguousarray(data[:, 0])
            if probe.AcceptWaveform(pcm.tobytes()):
                report(json.loads(probe.Result()).get("text", ""))

    report(json.loads(probe.FinalResult()).get("text", ""))

    print("\n" + "=" * 62)
    if not heard:
        print("Rien n'a été entendu. Vérifie le micro (wpctl status) et le volume.")
        return 1

    fired = [h for h in heard if h[2]]
    print(f"Segments entendus : {len(heard)}   déclenchements : {len(fired)}")

    best = sorted(heard, key=lambda h: -h[1])[:6]
    print("\nMeilleures correspondances :")
    for text, score, ok in best:
        print(f"   score={score:.2f} {'✓' if ok else ' '}  {_normalise(text)!r}")

    if not fired:
        print("\n⚠️  Aucun déclenchement. Ajoute les transcriptions ci-dessus à")
        print("   DEFAULT_PHRASES dans core/wake_word.py — ce sont les mots que")
        print("   Vosk entend réellement quand TU prononces le nom.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
