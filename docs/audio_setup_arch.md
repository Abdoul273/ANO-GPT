# Configuration audio studio-grade — ANO-GPT (JARVIS)

Système hôte : **Arch Linux / EndeavourOS**. Environnement : **Hyprland (Wayland)**.
Serveur multimédia : **PipeWire** (pas PulseAudio autonome).

Ce guide décrit les paquets et vérifications à faire **à la main**.
ANO-GPT n'applique aucune de ces commandes tout seul.

---

## 1. Paquets système

```bash
sudo pacman -S pipewire pipewire-pulse pipewire-alsa pipewire-audio \
    wireplumber rnnoise webrtc-audio-processing
```

| Paquet | Rôle |
|---|---|
| `pipewire` | Serveur multimédia. Fournit `pw-record`, `pw-top`, `pw-cli`. |
| `pipewire-alsa` | Pont ALSA → PipeWire. C'est par lui que `sounddevice` parle à PipeWire **sans** passer par l'émulation Pulse. |
| `pipewire-pulse` | Compatibilité pour les clients Pulse (`pactl`, `wpctl`). |
| `pipewire-audio` | Codecs et modules audio PipeWire. |
| `wireplumber` | Session manager / routage. |
| `rnnoise` | `librnnoise.so` — débruitage neuronal temps réel (ctypes dans `core/audio_denoise.py`). |
| `webrtc-audio-processing` | Bibliothèque C WebRTC (AGC / AEC Speex). Ce n'est **pas** le VAD Python. |

Python **3.13+** (cette machine : **3.14.7**), déjà listé dans `requirements.txt` :

- `sounddevice` — capture 16 kHz mono PCM 16-bit
- `onnxruntime` + `models/silero_vad.onnx` — **Silero VAD**, remplaçant de `webrtcvad-wheels`
- `webrtcvad-wheels` — repli seulement ; importe `pkg_resources` (setuptools), retiré des Python récents
- `numpy`

Sous Python 3.13, Silero est le VAD par défaut. Sous **3.14+** il est **opt-in** (`ANOGPT_ENABLE_ONNX_VAD=1`) : ONNX Runtime dans le callback PortAudio a corrompu le tas glibc ici.

**Ne pas installer** `silero-vad` via pip : ce paquet tire PyTorch, trop lourd pour une machine 2 cœurs / 11 Go. Le modèle ONNX local suffit.

**Ne pas compiler** RNNoise à la main : le paquet `rnnoise` d'Arch fournit `librnnoise.so`.

---

## 2. Taux d'échantillonnage 16 kHz sans resampling parasite

PipeWire tourne par défaut à 48 kHz. Quand un client demande du 16 kHz,
il faut que 16 000 figure dans `allowed-rates`, sinon PipeWire rééchantillonne
avec un convertisseur générique.

Créer `~/.config/pipewire/pipewire.conf.d/10-rates.conf` :

```conf
context.properties = {
    default.clock.rate          = 48000
    default.clock.allowed-rates = [ 16000 44100 48000 ]
    default.clock.quantum       = 512
    default.clock.min-quantum   = 256
    default.clock.max-quantum   = 1024
}
```

Recharger (session utilisateur, pas root) :

```bash
systemctl --user restart pipewire wireplumber pipewire-pulse
systemctl --user status pipewire wireplumber
```

ANO-GPT impose ensuite le format à l'ouverture du flux
(`samplerate=16000`, `channels=1`, `dtype=int16` via `sounddevice`, ou
`pw-record --rate=16000 --channels=1 --format=s16 --raw`).

---

## 3. Vérifier que le micro tourne bien en 16 kHz mono

### `pw-top`

Dans un terminal, pendant qu'ANO-GPT écoute :

```bash
pw-top
```

1. Ligne **ANO-GPT** / `python` / `pw-record` / `alsa_input…`.
2. Colonne **RATE** : `16000` (ou `48000` si PipeWire convertit en interne —
   acceptable si `allowed-rates` contient 16000).
3. Colonne **ERR** : doit rester à `0` (xruns).
4. **QUANTUM** : 256–512 (latence vocale < 16 ms à 48 kHz).

### `pw-cli`

```bash
pw-cli list-objects Node
pw-cli info <id-du-noeud-micro>
```

Propriétés attendues côté client ANO-GPT :

- `audio.format = "S16LE"`
- `audio.rate = 16000`
- `audio.channels = 1`

### `wpctl`

```bash
wpctl status
wpctl set-volume @DEFAULT_AUDIO_SOURCE@ 1.0
```

Volume micro à 100 % sans saturation (les crêtes doivent rester sous 0 dBFS ;
l'AGC logiciel d'ANO-GPT vise ensuite **-18 dBFS**).

---

## 4. Chaîne audio ANO-GPT

```
    [ Micro / PipeWire ]
              │
              ▼  16 kHz mono PCM 16-bit, format forcé à la source
     [ AudioCaptureStream ]     core/audio_capture.py
              │
              ▼
       [ AudioDenoiser ]        core/audio_denoise.py
       ├─ upsample 16 → 48 kHz (ratio entier 3)
       ├─ RNNoise (frames 480 échantillons / 10 ms)
       ├─ downsample 48 → 16 kHz
       └─ AGC logiciel → -18 dBFS + limiteur
              │
              ▼
   [ VoiceActivityDetector ]    core/audio_vad.py
       ├─ Silero VAD v5 ONNX (nominal 3.13 ; opt-in 3.14)
       ├─ sinon webrtcvad (repli, pkg_resources — à abandonner)
       ├─ preroll 250 ms / hangover 350 ms
       └─ repli énergie + planéité spectrale
              │
              ▼
  [ Gemini 3.5 Transcribe Live ] core/precision_stt.py
              │
              ▼
     [ TranscriptGuard ]        core/ai_stt_corrector.py
              │
              ▼
          commande
```

`TranscriptGuard` n'est pas remplacé : c'est le filet textuel (hallucinations,
hésitations, langue étrangère) après un signal déjà propre.

La seconde passe Gemini est également fermée par défaut lorsqu'aucune parole
n'est confirmée par le VAD. Ainsi, un bruit de fond ou un clip vide n'entraîne
ni appel réseau ni transcription spéculative. Cette protection ne se désactive
que pour un diagnostic explicite (`require_speech=False`).

L'annulation d'écho (AEC) du chemin live full-duplex reste dans
`core/echo_canceller.py` (SpeexDSP, référence haut-parleur obligatoire).
Elle n'est pas dupliquée dans `AudioDenoiser`, qui traite des clips déjà
capturés sans flux de référence.

---

## 5. Python 3.13+ et Silero ONNX

`webrtcvad-wheels` n'est plus le VAD visé : il importe `pkg_resources`,
API setuptools en cours de suppression. Le remplacement est **Silero VAD
ONNX** (`core/vad_silero.py`, `models/silero_vad.onnx`), déjà dans le dépôt.

Sur **cette** machine (Python 3.14.7), ONNX Runtime a corrompu le tas glibc
dans le **callback PortAudio**. Silero est donc **opt-in** sous 3.14+
(`ANOGPT_ENABLE_ONNX_VAD=1`). Sans le drapeau, le repli `webrtcvad` tourne
tant qu'il s'installe encore.

Sous 3.13, Silero est actif par défaut.

```bash
export ANOGPT_ENABLE_ONNX_VAD=1
```

---

## 6. Journaux DEBUG

```bash
python main.py --log-level DEBUG
```

Exemples :

```
Capture audio démarrée via sounddevice (dev=pulse, 16000 Hz, 1 ch, int16, 20 ms/frame)
[AudioDenoiser] RMS in: -32.4 dBFS | RMS out: -18.2 dBFS | AGC: +14.2 dB
[PrecisionSTT] Denoise: RMS in = -31.0 dBFS, RMS out = -18.0 dBFS, AGC gain = +13.0 dB
[PrecisionSTT] VAD: confidence = 0.998 | speech = True (threshold=0.50)
```

Les logs RMS / VAD sont émis **par énoncé** (ou une fois par seconde pour la
capture), jamais dans le callback PortAudio : Qt et l'audio partagent le GIL.

---

## 7. Diagnostic rapide

| Symptôme | Piste |
|---|---|
| `librnnoise non trouvée` | `sudo pacman -S rnnoise` puis redémarrer ANO-GPT. |
| `RATE` à 44100 / 48000 uniquement | Ajouter 16000 dans `allowed-rates`, relancer PipeWire. |
| `ERR` qui monte dans `pw-top` | Quantum trop petit, CPU saturé (Hyprland + Qt). Remettre `quantum = 512`. |
| Fausses transcriptions sur le ventilo | VAD trop permissif. Vérifier les logs `[PrecisionSTT] VAD`. Forcer Silero (`ANOGPT_ENABLE_ONNX_VAD=1`) si le repli webrtcvad laisse passer le bruit. |
| L'assistant s'entend lui-même | Ce n'est **pas** un problème de ce pipeline. Le half-duplex vit dans le callback micro de `main.py` / `audio_engine.py`. |
