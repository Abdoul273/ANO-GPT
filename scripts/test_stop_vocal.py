#!/usr/bin/env python3
"""Test du « stop » vocal : même câblage que l'appli (AEC + Vosk), 15 s.

ANO-GPT doit être FERMÉ. Le script parle en boucle dans les haut-parleurs via
le puits AEC ; pendant ce temps, dis « stop », « arrête-toi » ou « écoute ».
Il affiche le niveau de la source AEC et ce que Vosk entend, en direct.
"""
import json, os, subprocess, sys, time
import numpy as np
import sounddevice as sd
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("ANOGPT_ENABLE_VOSK_NATIVE", "1")
from core import audio_router as ar
from core.barge_in import INTERRUPT_GRAMMAR, InterruptPhraseDetector, resolve_vosk_model_path
from vosk import KaldiRecognizer, Model, SetLogLevel

SetLogLevel(-1)
chosen = ar.refresh_and_apply(); raw = chosen.device.name if chosen.device else ""
src = ar.enable_echo_cancel(raw)
print(f"micro brut : {raw}\nsource AEC : {src}")
if not src:
    sys.exit("AEC indisponible")
model = Model(str(resolve_vosk_model_path(None)))
rec = KaldiRecognizer(model, 16000, json.dumps(list(INTERRUPT_GRAMMAR), ensure_ascii=False)); rec.SetWords(True)
levels, last_partial = [], ""
def cb(indata, f, t, s):
    pcm = indata[:, 0]
    levels.append(float(np.sqrt(np.mean(pcm.astype(np.float32) ** 2))))
    global last_partial
    if rec.AcceptWaveform(pcm.tobytes()):
        r = json.loads(rec.Result()); txt = r.get("text", "")
        if txt:
            print(f"\n  VOSK final : « {txt} » → {InterruptPhraseDetector.classify_command(txt)}  conf={[round(w.get('conf',0),2) for w in r.get('result',[])]}")
    else:
        p = json.loads(rec.PartialResult()).get("partial", "")
        if p and p != last_partial:
            last_partial = p; print(f"\n  VOSK partiel : « {p} »")
dev = ar.portaudio_input_device(sd)
# ── Chaîne EXACTE de l'appli : worker Vosk isolé + LocalBargeInListener ──
from core.barge_in import LocalBargeInListener
det = InterruptPhraseDetector(sample_rate=16000, mode="commands")
print("worker isolé :", det.available, "prêt :", det.wait_ready(40), det.fail_reason)
fired = []
def on_int(kind="stop", text=""):
    fired.append((time.time(), kind, text)); print(f"\n  >>> ÉCOUTEUR APPLI A TIRÉ : {kind} « {text} »")
os.environ["PULSE_SOURCE"] = src
app_listener = LocalBargeInListener(sd, det, lambda: True, on_int, sample_rate=16000, blocksize=320, input_device=dev)
print("écouteur appli démarré :", app_listener.start())
stream = sd.InputStream(samplerate=16000, channels=1, dtype="int16", blocksize=320, callback=cb, device=dev)
stream.start(); os.environ.pop("PULSE_SOURCE", None)
os.environ["PULSE_SINK"] = "anogpt_speaker_aec"
speaker = subprocess.Popen(["bash", "-c", "for i in 1 2 3 4 5 6; do espeak-ng -v fr -s 150 'Je suis ANO GPT et je parle sans arrêt pour tester la coupure vocale, dis stop maintenant.'; done"])
os.environ.pop("PULSE_SINK", None)
time.sleep(0.8)
# Tout ce qui joue passe par le puits AEC (espeak est un processus enfant).
for line in subprocess.run(["pactl", "list", "sink-inputs", "short"], capture_output=True, text=True).stdout.splitlines():
    subprocess.run(["pactl", "move-sink-input", line.split()[0], "anogpt_speaker_aec"])
print("lecture routée via AEC")
os.system("pactl list source-outputs | grep -E '^\\s*Source:' | tr '\\n' ' '"); print()
print("\n>>> PARLE MAINTENANT : dis « stop » plusieurs fois pendant qu'il parle (15 s)\n")
t0 = time.time()
try:
    while time.time() - t0 < 15:
        time.sleep(0.5)
        if levels:
            print(f"  niveau AEC {np.mean(levels[-25:]):6.0f}   ", end="\r")
finally:
    speaker.terminate(); stream.stop(); stream.close(); app_listener.stop(); ar.disable_echo_cancel()
print(f"\nfin. tirs de l'écouteur appli : {len(fired)}")
