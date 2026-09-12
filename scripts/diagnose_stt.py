#!/usr/bin/env python3
"""Diagnostic volontaire : WAV local, puis transcription seule sur demande.

Exemple : python scripts/diagnose_stt.py --record 8 --output /tmp/ma-voix.wav
          --transcribe --expected "Il fait quelle heure ?"
Aucune transcription n'est transmise au moteur de commandes d'ANO-GPT.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.stt_audio import signal_metrics


def read_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 16000):
            raise ValueError("Le WAV doit être mono PCM16 à 16 kHz.")
        if audio.getnframes() > 25 * 16000:
            raise ValueError("Le diagnostic accepte au maximum 25 secondes.")
        return audio.readframes(audio.getnframes())


def record(seconds: float, output: Path) -> bytes:
    import sounddevice as sd
    from core.audio_router import portaudio_input_device
    if not 1 <= seconds <= 25:
        raise ValueError("Durée de capture : entre 1 et 25 secondes.")
    # Création exclusive et privée : ne jamais écraser un enregistrement.
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as dest:
        print("Enregistrement dans 3 secondes ; l'assistant doit être silencieux.", flush=True)
        time.sleep(3)
        print(f"Parle maintenant ({seconds:g} secondes).", flush=True)
        device = portaudio_input_device(sd)
        samples = sd.rec(int(seconds * 16000), samplerate=16000, channels=1,
                         dtype="int16", device=device, blocking=True)
        pcm = samples.astype("<i2").tobytes()
        with wave.open(dest, "wb") as audio:
            audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            audio.writeframes(pcm)
    print(f"Capture terminée : {output}", flush=True)
    return pcm


async def transcribe(pcm: bytes) -> dict:
    from core.gemini_transcribe_stt import GeminiTranscribeTurn
    from core.precision_stt import DEFAULT_MODEL
    settings = json.loads((ROOT / "config/api_keys.json").read_text())
    host = SimpleNamespace(_gemini_api_key=settings.get("gemini_api_key", ""),
                           _precision_stt_model=DEFAULT_MODEL)
    turn = GeminiTranscribeTurn(host)
    try:
        await asyncio.wait_for(turn.start(), timeout=15)
        await turn.begin()
        for offset in range(0, len(pcm), 3200):
            await turn.feed(pcm[offset:offset + 3200])
            await asyncio.sleep(.1)
        started = time.monotonic()
        text = await turn.finish()
        return {"transcript": text, "finalization_s": round(time.monotonic() - started, 3)}
    finally:
        await turn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--wav", type=Path)
    source.add_argument("--record", type=float, metavar="SECONDS")
    parser.add_argument("--output", type=Path, help="WAV privé à créer pour --record")
    parser.add_argument("--transcribe", action="store_true", help="Envoyer ce clip à Gemini")
    parser.add_argument("--expected", help="Texte réellement prononcé, jamais fourni au modèle")
    args = parser.parse_args()
    if args.record is not None and args.output is None:
        parser.error("--record exige --output ; aucun enregistrement implicite")
    try:
        pcm = record(args.record, args.output) if args.record is not None else read_wav(args.wav)
        report = {"audio": signal_metrics(pcm)}
        if args.transcribe:
            report.update(asyncio.run(transcribe(pcm)))
        if args.expected is not None:
            report["expected"] = args.expected
            if "transcript" in report:
                from core.precision_stt import transcripts_agree
                report["same_words"] = transcripts_agree(args.expected, report["transcript"])
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        # Les exceptions du SDK peuvent contenir des détails de connexion.
        print(f"Diagnostic non terminé : {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
