# ANO-GPT

Mise à jour du socle : exécution des outils renforcée, configuration protégée,
contrôle distant authentifié et panneau **Santé et performances** dans les paramètres.

Diagnostic local : `./anogpt-ctl doctor` (ou `--json`).
Voir les [changements et vérifications de septembre 2026](docs/MISE_A_JOUR_FIABILITE_2026-09.md).

## Choisir le cerveau

Réglages → **Configuration IA**. Le fournisseur sélectionné (« Appliquer ce
fournisseur ») devient le cerveau conversationnel : compréhension, décision,
appels d'outils et rédaction des réponses. **OpenRouter** accepte sa clé API et
charge automatiquement son catalogue complet de modèles ; le choix se fait
dans cette liste, sans avoir à connaître ni saisir les identifiants techniques.

Gemini Live conserve le micro et la voix, sauf si vous choisissez explicitement
ElevenLabs pour la synthèse. Les rôles Azure spécialisés restent séparés : les
modèles Azure configurés pour la réflexion approfondie, les documents, les
images et les vidéos ne sont pas remplacés lorsque vous choisissez OpenRouter
ou un autre cerveau conversationnel.

Gemini Live garde le micro et la voix, quel que soit le cerveau : c'est lui qui
entend et qui parle, il transmet chaque demande au cerveau choisi et prononce sa
réponse mot pour mot. « Automatique » laisse la main au premier fournisseur de
la liste de priorité dont une clé est enregistrée ; choisir Gemini rend le
raisonnement à Gemini Live lui-même.

Le changement prend effet à la reconnexion vocale, déclenchée automatiquement.
Un cerveau à raisonnement lent (GPT-5.x, o-series) se paie en silence entre la
question et la réponse : `brain_timeout_s` dans `config/api_keys.json` borne
cette attente (45 s par défaut, 90 s au maximum).

La documentation d'origine du projet est conservée ci-dessous.

# ⚙️ MARK XLIX
### The Ultimate Cross-Platform Personal AI Assistant — By FatihMakes

> 📺 **[Watch the full setup video on YouTube](https://youtu.be/CiGdcIlnXb8))**

A real-time voice AI that can hear, see, understand, and control your computer — on any OS. Supports Windows, macOS, and Linux. Built on the Gemini Live API for native audio streaming, delivering zero subscriptions and total digital autonomy.

---

## ✨ Overview

MARK XLIX deepens the personal assistant foundation. Rather than adding more tools, this build focused on making the assistant truly *yours*: it starts with your computer, learns your name, and pays attention to what you're doing. The goal before the plugin era begins is a core that feels alive — not just reactive.

---

## 🚀 Capabilities

### Core Features
| Feature | Description |
|---|---|
| 🎙️ Real-time Voice | Ultra-low latency conversation in any language via Gemini Live API |
| 🖥️ System Control | Launch apps, adjust volume/brightness, WiFi, shortcuts, power — all by voice |
| 🧩 Autonomous Tasks | High-level planning for complex multi-step goals via agent mode |
| 👁️ Visual Awareness | Real-time screen capture and webcam vision piped into your main Gemini session |
| 🧠 Persistent Memory | Deeply remembers projects, preferences, and personal context across sessions |
| ⌨️ Hybrid Input | Seamlessly switch between keyboard typing and voice commands |
| 🌅 Morning Briefing | On first boot: greets you, reads the time, fetches live news headlines, and checks weather |
| 🔔 Proactive Check-ins | After 15 minutes of silence, checks context and offers something genuinely useful |
| 📊 Hardware Monitoring | Continuous CPU, RAM, GPU and temperature telemetry with localized voice alerts |
| 🌤️ Weather Report | Live weather data for your city, personalized from memory |
| 🗺️ Dynamic Content Panel | Scrollable display layer beneath the HUD that renders web results, news, and search data |
| 🔍 Multi-Mode Web Search | `news` / `research` / `price` / `compare` / `search` — Gemini Grounded first, DDG fallback |
| ⏰ Smart Reminders | OS-native scheduled notifications (Windows Task Scheduler / macOS LaunchAgent / Linux systemd) |
| ✈️ Flight Finder | Live flight price and availability lookup |
| 🎮 Game Updater | Checks and triggers game updates on Steam and Epic Games on demand |
| 📂 File Processor | Read, summarize, and answer questions about local files |
| 💻 Code Helper | Inline code review, debugging, and generation |
| 🌐 Browser Control | Open URLs, navigate tabs, and interact with the browser by voice |
| 📨 Send Message | Compose and send messages through WhatsApp, Telegram, and more |
| 🎬 YouTube Control | Search, play, and control YouTube playback by voice |
| 🖱️ Desktop Control | Taskbar, window management, and desktop-level operations |
| 🧑‍💻 Silent Language Memory | Detects spoken language on first use and saves it — all future sessions adapt automatically |
| 📱 Remote Dashboard | Control the assistant from your phone via QR code pairing |
| 📈 TikTok Tracker | « Suis mon TikTok » : abonnés, j'aime, vues par vidéo relus toutes les deux minutes (page publique via Chrome headless, comme Blow) ; carte à l'écran et annonces vocales des nouveaux abonnés, paliers et vidéos qui décollent |
| 🎯 TikTok Coach | « Pourquoi ma vidéo n'a pas marché ? » : chiffres passés au crible + la vidéo visionnée par Gemini (accroche, rythme, texte, son) avec causes et corrections ; bilan du compte et plan ; « analyse cette vidéo avant que je la poste » : avis, montage, description, hashtags, couverture, meilleure heure |

---

## 🎙️ Voice activation (Linux / Wayland)

### Global hotkeys

Wayland gives applications no global keyboard access, so the compositor owns the
hotkey and forwards it to the running app through a Unix control socket
(`$XDG_RUNTIME_DIR/anogpt.sock`). Hyprland binds are installed in
`~/.config/hypr/hyprland/keybinds.lua`:

| Shortcut | Action |
|---|---|
| `SUPER + SHIFT + Space` | Mic: listening ⇄ standby (works on the lock screen) |
| `SUPER + SHIFT + X` | Interrupt the assistant mid-sentence |
| `SUPER + SHIFT + W` | Toggle the offline wake word |

The same commands work from any terminal or script:

```bash
./anogpt-ctl toggle
./anogpt-ctl ask "ouvre firefox"
./anogpt-ctl status
```

### Offline wake word

While the mic is on standby, audio is scanned **locally** by Vosk — nothing is
sent to Gemini until the wake phrase is heard, so standby is genuinely private.

```bash
bash scripts/install_wake_word.sh          # vosk + small French model (~40 MB)
python scripts/calibrate_wake_word.py      # tune it to your own voice
```

> ⚠️ **Calibration is required.** "Ano" is not in the French model's vocabulary,
> so it is transcribed as whichever real words sound closest — and *which* words
> depends on your voice, accent and microphone. The calibration tool prints what
> Vosk actually hears when you speak; add those transcriptions to
> `DEFAULT_PHRASES` in `core/wake_word.py`. Without this step, detection is
> unreliable. The hotkey above always works regardless.

---

## 🆕 What's New in XLIX

### ⚡ Auto-Start on Boot
The assistant now registers itself with the operating system's startup system. One click in the UI toggles it on or off. On Windows, it writes to the registry using `pythonw.exe` so no console window ever appears. On macOS it installs a LaunchAgent plist; on Linux a `.desktop` autostart entry. The button reflects the current state every time the app launches.

### 🎨 Assistant Customization
The assistant is no longer locked to the name "JARVIS". Click `⚙ CUSTOMISE ASSISTANT` in the right panel to change:
- **Assistant name** — displayed everywhere in the UI (title bar, header, HUD, log, footer) and injected into the Gemini system prompt so the AI knows its own name
- **Your name** — how the assistant addresses you. Leave blank for the default language-aware addressing (`sir` / `efendim`), or set your actual name for a more personal feel

Changes take effect immediately without restarting.

### 📋 Clipboard Intelligence
Copy any text of 10 or more characters and a floating panel appears at the bottom of the window. Four quick actions — **TRANSLATE**, **SUMMARISE**, **EXPLAIN**, **FIX** — send the copied content directly to the assistant with one click. The panel auto-dismisses after 8 seconds. This turns the clipboard into a silent command channel for anything on your screen.

### ☀ Morning Brief Toggle + Speed Optimization
The morning briefing can now be turned on or off with one click from the settings drawer (`⚙` → `☀ MORNING BRIEF: ON/OFF`). Users who don't want a startup briefing can disable it permanently; the setting survives restarts. The briefing itself was also re-engineered: news is now pre-fetched in a background thread the moment the session starts, running in parallel while the greeting plays. By the time the greeting finishes, the results are already ready — no extra Gemini tool-call round-trip needed. Briefing delivery is noticeably faster as a result.

---

## 🗺️ Mark Roadmap

| Mark | Focus |
|---|---|
| **XLVIII** | Instant interrupt · parallel news · two-phase briefing · exponential backoff · vision cooldown |
| **XLIX** | Auto-start · clipboard intelligence · assistant customization |
| **L** | Wake word · proactive system 2.0 · session memory / daily continuity |
| **LI+** | Plugin system · email · quiz mode · calorie counter · and more |

---

## ⚡ Quick Start

```bash
git clone https://github.com/Abdoul273/ANO-GPT.git
cd ANO-GPT
pip install -r requirements.txt
python main.py
```

> ⚠️ **Installation Note:** Some OS-specific dependencies are not bundled in `requirements.txt` to keep the repo lightweight. If you hit a `ModuleNotFoundError`, install the missing package with `pip install <module_name>`.

---

## 📋 Requirements

| Requirement | Details |
| --- | --- |
| **OS** | Windows 10/11, macOS, or Linux (Arch / EndeavourOS + Hyprland on this machine) |
| **Python** | **3.13 or newer** (developed on **3.14.7**) |
| **Microphone** | Required for voice interaction |
| **API Key** | Free Gemini API key (`config/api_keys.json`) |

### Voice activity (VAD)

`webrtcvad-wheels` is a **legacy fallback**. It imports `pkg_resources` (setuptools), which recent Python versions remove. The intended detector is **Silero VAD ONNX** (`core/vad_silero.py`, model `models/silero_vad.onnx` via `onnxruntime`) — already in the tree, no PyTorch.

- Python **3.13** : Silero is used by default.
- Python **3.14+** : Silero is **opt-in** (`ANOGPT_ENABLE_ONNX_VAD=1`) so ONNX Runtime does not run inside the PortAudio callback (glibc heap corruption observed here). Without the flag, WebRTC VAD is used until it disappears.

Do **not** `pip install silero-vad` : that package pulls PyTorch, too heavy for a 2-core / 11 GB machine.

---

## 🗂️ Project Structure

```
ANO-GPT/
├── main.py                  # Boucle Live — Gemini, audio, répartition d'outils
├── anogpt-ctl               # Socket de contrôle (toggle, ask, doctor)
├── anogpt_mcp.py            # Pont MCP vers les agents (Antigravity, Claude, Codex)
├── setup.py                 # Première installation (pip + Playwright)
├── ui/                      # HUD PyQt6 — plus de ui.py monolithique
│   ├── jarvis_ui.py         # Façade publique JarvisUI
│   ├── main_window.py       # Fenêtre principale
│   ├── orb/                 # Orbe, waveform radial, mini-orbe
│   ├── panels/              # Journal, cartes, musique, télémétrie
│   ├── dialogs/             # Réglages IA, audio, mémoire, plugins
│   ├── media/               # Caméra, galerie, carte, vidéo
│   └── window/              # Chrome, tiroir, scène
├── actions/                 # Outils vocaux (recherche, bureau, mail, code, …)
├── core/
│   ├── audio_engine.py      # Capture / lecture, half-duplex
│   ├── audio_vad.py         # Silero ONNX (opt-in 3.14) ; webrtcvad en repli
│   ├── vad_silero.py        # models/silero_vad.onnx via onnxruntime
│   ├── map_render.py        # Carte unique plein cadre
│   ├── llm_client.py        # Cerveau (Azure, OpenRouter, DeepSeek, …)
│   └── prompt.txt           # Personnalité et routage d'outils
├── dashboard/               # Serveur HTTPS + app téléphone (ANO Remote)
├── memory/                  # RAG, habitudes, journal d'outils, config
├── models/                  # silero_vad.onnx, embeddings
├── plugins/                 # Extensions utilisateur
├── tests/
└── config/
    └── api_keys.json        # Clés, cerveau, nom de l'assistant
```

---

## ⚠️ License

Personal and non-commercial use only.
Licensed under **[Creative Commons BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/)**.

---

## 👤 Connect with the Creator

Engineered by a developer building a real-world JARVIS-style assistant.
⭐ **Star the repository to support the journey to Mark 100.**

| Platform | Link |
| --- | --- |
| YouTube | [@FatihMakes](https://www.youtube.com/@FatihMakes) |
| Instagram | [@fatihmakes](https://www.instagram.com/fatihmakes) |
