# 🤖 ANO-GPT

<div align="center">

### Votre assistant personnel vocal, visuel et connecté à votre ordinateur.

**Voix en direct · Bureau intelligent · Mémoire · Téléphone Android · Agents MCP**

[🚀 Installation](#-installation) · [✨ Capacités](#-ce-que-peut-faire-ano-gpt) · [📱 ANO Remote](#-ano-remote--le-téléphone-comme-extension) · [🧩 MCP](#-vos-agents-mcp-sur-votre-ordinateur) · [📚 Documentation](#-documentation)

</div>

---

## 👋 Bienvenue dans ANO-GPT

**ANO-GPT** transforme une demande en langage naturel en action visible : ouvrir une application, examiner l'écran, chercher un document, afficher une carte, gérer un rappel ou confier une tâche à un agent de développement. Il rassemble une conversation vocale avec **Gemini Live**, un cerveau conversationnel configurable, une interface **PyQt6**, des outils locaux, un tableau de bord et une application Android.

Le projet est développé et utilisé principalement sur **Arch Linux / EndeavourOS avec Hyprland**. Il vise une expérience d'assistant personnel complète, utilisable aussi bien à la voix qu'au clavier ou depuis un téléphone appairé.

> 💡 **Parlez à votre ordinateur, voyez ce qu'il fait, reprenez la main à tout moment.**

| Domaine | Ce qu'ANO-GPT peut faire |
| --- | --- |
| 🎙️ **Conversation** | Écouter, répondre à voix haute, afficher les échanges et accepter une interruption. |
| 🖥️ **Bureau** | Ouvrir des applications, gérer des fenêtres, agir sur certains réglages et travailler avec les fichiers. |
| 👁️ **Vision** | Capturer l'écran, utiliser une caméra, analyser une image et pointer un élément visible. |
| 🌐 **Recherche** | Explorer le web, afficher des résultats, des vidéos, des lieux et une carte. |
| 🧠 **Mémoire** | Retrouver des préférences, notes et documents personnels entre les sessions. |
| 📱 **Mobilité** | Recevoir voix, caméra et position depuis **ANO Remote**. |
| 🧩 **Agents** | Exposer ses outils via **MCP** et déléguer des missions longues. |

Les fonctions qui dépendent d'un compte, d'une clé API, d'un périphérique ou d'une application externe demandent leur configuration respective.

## ✨ Ce que peut faire ANO-GPT

### 🎙️ Parler, écouter et s'adapter

- **Conversation vocale en direct** via Gemini Live, avec transcription dans l'interface et saisie au clavier.
- **Cerveau conversationnel au choix** dans **Réglages → Configuration IA** : Gemini, OpenRouter, OpenAI, Anthropic, DeepSeek, Grok, Azure OpenAI, Groq ou Ollama, selon les accès configurés. OpenRouter peut charger son catalogue de modèles dans l'interface.
- **Voix et raisonnement séparés** : Gemini Live gère le canal vocal ; le fournisseur choisi peut traiter la demande et rédiger la réponse. Une synthèse ElevenLabs peut être configurée séparément.
- **Personnalisation** du nom de l'assistant, de l'apparence du HUD, des styles d'orbe, de la voix et des modes de personnalité disponibles.
- **Interruption** de la réponse en cours et contrôle de l'écoute depuis l'interface, le terminal ou des raccourcis Hyprland.
- **Mot d'activation local** avec Vosk en option, après installation et calibration.

La chaîne audio comprend la capture, le débruitage et la détection de parole disponibles sur la machine. Pendant que l'assistant parle, le micro ne transmet pas sa propre voix au modèle : le chemin vocal applique un comportement **half-duplex**. Détails et limites propres à Python 3.14 : [guide audio Arch](docs/audio_setup_arch.md).

### 🖥️ Agir sur le bureau et les fichiers

- Ouvrir et fermer des applications, gérer des fenêtres et des espaces de travail, utiliser des raccourcis et contrôler certains réglages système.
- Chercher, lire, résumer et traiter des fichiers ; rechercher du contenu dans des documents personnels.
- Capturer l'écran, analyser son contenu et montrer visuellement où cliquer.
- Utiliser la caméra du PC ou celle du téléphone appairé ; décrire et reconnaître des éléments visuels selon les modèles configurés.
- Piloter la lecture multimédia, la musique et YouTube ; reconnaître un morceau si le service nécessaire est disponible.
- Aider sur le code, le débogage et des tâches de développement avec les outils et agents installés.

### 🌍 Explorer et rester organisé

- Rechercher sur le web, dans l'actualité et parmi des images ; afficher les résultats dans des cartes de l'interface.
- Voir sa position, trouver des lieux à proximité, afficher **une carte plein cadre** ou un globe et lancer une navigation guidée. Voir [Localisation](docs/LOCALISATION.md).
- Consulter la météo et des informations de voyage selon les sources accessibles.
- Gérer rappels, agenda, contacts et e-mails après connexion des comptes correspondants.
- Connecter **Notion** et **Figma**, ou utiliser des services web dans la session Google Chrome. Voir [Connexions cloud](docs/CLOUD_INTEGRATIONS.md).
- Suivre des statistiques TikTok publiques et demander une analyse de contenu avec les outils dédiés.

### 🧠 Garder le contexte et anticiper

- Enregistrer des informations utiles et les retrouver en langage naturel.
- Interroger une base de connaissances personnelle et des documents indexés.
- Faire remonter des événements, des rappels et des tâches en arrière-plan dans l'interface.
- Utiliser des routines, un briefing et des modes proactifs configurables.
- Lancer une mission longue avec le **Mode Agent Fantôme** : ANO-GPT garde la conversation disponible pendant que l'agent travaille, puis affiche et annonce un résumé. Voir le [guide MCP](docs/MCP.md#mode-agent-fantôme).

### 🛡️ Garder le contrôle

- Instance unique et commandes locales via socket Unix.
- Diagnostics locaux : environnement Python, dépendances, configuration et statistiques des actions.
- Tableau de bord distant avec appairage et panneau **Santé et performances**.
- Journalisation, reprise de certaines pannes et gestion des interruptions.
- Plugins chargeables depuis `plugins/`, avec un format documenté et une interface de gestion.

> 🔐 Les outils d'automatisation et les plugins agissent avec les droits de l'utilisateur qui lance ANO-GPT. Vérifiez le code des extensions et les accès accordés aux services connectés.

## 🎨 Une interface qui donne vie à l'assistant

L'interface PyQt6 réunit un **orbe animé**, des états d'écoute, les transcriptions, un journal, des cartes de résultats, des médias et les réglages. Plusieurs styles visuels d'orbe sont présents dans [`ui/orb/styles/`](ui/orb/styles/). La carte géographique occupe un **seul conteneur plein cadre**.

ANO-GPT est conçu pour rester utilisable sur une machine modeste : l'interface et la voix partagent les ressources du processus, ce qui rend la fluidité de l'affichage importante pour la conversation.

## 💬 Essayez de lui demander

> « Ouvre Chrome et cherche les dernières nouvelles sur Python. »
>
> « Résume ce document et retrouve les passages sur le budget. »
>
> « Montre les restaurants près de moi sur la carte. »
>
> « Qu'est-ce qui est affiché à l'écran ? Montre-moi où cliquer. »
>
> « Rappelle-moi mon rendez-vous demain à 9 h. »
>
> « Analyse ce dépôt en arrière-plan et préviens-moi quand c'est terminé. »

Ces exemples dépendent des modèles, services, permissions et agents configurés. Le choix du cerveau se fait dans **Réglages → Configuration IA** ; le changement déclenche une reconnexion vocale. Le mode **Automatique** choisit un fournisseur configuré selon l'ordre de priorité enregistré. Les modèles des rôles spécialisés (documents, images, vidéos, réflexion approfondie) se règlent séparément.

## 📱 ANO Remote : le téléphone comme extension

L'application Android dans [`mobile/ano_remote/`](mobile/ano_remote/) se connecte au PC après appairage. Elle peut :

- 🎤 transmettre le micro du téléphone à ANO-GPT ;
- 📷 diffuser sa caméra vers l'interface du PC et prendre des captures ;
- 📍 partager sa position avec un service Android visible et désactivable ;
- 🖼️ consulter les captures reçues ;
- 🔗 découvrir le PC sur le réseau local ou se connecter par QR code ou adresse.

Un tableau de bord web se trouve dans [`dashboard/`](dashboard/). Le téléphone et le PC doivent pouvoir communiquer sur le réseau, et les autorisations Android correspondantes doivent être accordées. Installation et dépannage : [guide ANO Remote](mobile/ano_remote/README.md).

## 🧩 Vos agents MCP sur votre ordinateur

[`anogpt_mcp.py`](anogpt_mcp.py) expose des outils d'ANO-GPT aux clients **Model Context Protocol**, notamment Antigravity, Claude Code, Codex CLI et Claude Desktop. Le serveur MCP transmet les actions à l'application en cours d'exécution par le socket de contrôle. Certains outils non visuels disposent d'un mode de repli lorsque l'interface est arrêtée.

Un agent peut par exemple **afficher une carte**, **parler**, **chercher un fichier**, **consulter la météo**, **prendre une capture**, **interroger la mémoire** ou **afficher une fiche** sur le HUD.

```bash
python anogpt_mcp.py --selftest
```

Le [guide MCP](docs/MCP.md) donne les configurations de chaque client, les limites du mode hors application et le dépannage. Pour Antigravity, le fichier lu par `agy` est **`~/.gemini/config/mcp_config.json`**.

## 🚀 Installation

### Prérequis

| Élément | Détail |
| --- | --- |
| 🐧 **Système principal** | Arch Linux / EndeavourOS, avec développement et utilisation sur Wayland / Hyprland. Le code contient aussi des adaptations Windows et macOS ; ce guide couvre Arch. |
| 🐍 **Python** | 3.13 ou plus récent. |
| 🎤 **Audio** | Micro, sortie audio et pile PipeWire fonctionnelle. |
| 🌐 **Navigateur** | Google Chrome pour les fonctions navigateur et OAuth. |
| 🔑 **IA** | Clé Gemini pour la voix ; autres clés et comptes selon les fournisseurs et services activés. |

### 1. Installer les dépendances système

```bash
sudo pacman -S --needed python python-pip pipewire pipewire-pulse pipewire-alsa pipewire-audio wireplumber rnnoise webrtc-audio-processing
yay -S google-chrome
```

Le [guide audio](docs/audio_setup_arch.md) explique le réglage de PipeWire, la vérification du micro et les dépendances facultatives.

### 2. Cloner et préparer Python

```bash
git clone https://github.com/Abdoul273/ANO-GPT.git
cd ANO-GPT
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

`setup.py` peut également installer les dépendances Python. Certaines intégrations demandent des composants supplémentaires décrits dans leur guide.

### 3. Configurer la clé vocale

```bash
cp config/api_keys.example.json config/api_keys.json
```

Renseignez au minimum **`gemini_api_key`** dans `config/api_keys.json`. L'écran de première configuration peut aussi guider cette étape. Une clé vocale distincte peut être renseignée dans **Configuration IA** pour séparer les accès.

> 🔒 `config/api_keys.json` contient des secrets personnels. Ne le publiez pas et ne le commitez pas sur GitHub.

### 4. Démarrer et vérifier

```bash
python main.py
```

Si l'interface est déjà ouverte, ANO-GPT réactive l'instance existante. Pour un diagnostic local, même lorsque l'assistant est arrêté :

```bash
./anogpt-ctl doctor
./anogpt-ctl doctor --json
```

Le diagnostic ne valide pas à lui seul une clé API distante, le microphone ou chaque service connecté. Le panneau **Santé et performances** du tableau de bord affiche aussi les informations disponibles.

## ⌨️ Contrôle depuis le terminal

[`anogpt-ctl`](anogpt-ctl) communique avec l'application via `$XDG_RUNTIME_DIR/anogpt.sock` :

```bash
./anogpt-ctl status                 # état actuel
./anogpt-ctl listen                 # activer l'écoute
./anogpt-ctl mute                   # mettre en veille
./anogpt-ctl toggle                 # basculer entre écoute et veille
./anogpt-ctl interrupt              # interrompre la réponse vocale
./anogpt-ctl ask "ouvre Chrome"     # envoyer une demande écrite
./anogpt-ctl action-stats           # statistiques des actions
```

Sous Hyprland, le compositeur peut lier ces commandes à des raccourcis globaux. Le mot d'activation local repose sur Vosk et demande une calibration ; les scripts correspondants sont dans [`scripts/`](scripts/).

## 🏗️ Architecture du projet

```text
ANO-GPT/
├── main.py                 # démarrage, boucle vocale et interface
├── anogpt-ctl              # commandes locales via socket
├── anogpt_mcp.py           # serveur MCP pour les agents externes
├── actions/                # bureau, web, médias et services
├── core/                   # audio, IA, mémoire, outils et IPC
│   ├── audio_engine.py     # capture et lecture de la voix
│   ├── llm_client.py       # fournisseurs de modèles
│   ├── ipc.py              # socket de contrôle local
│   └── map_render.py       # carte plein cadre
├── ui/                     # interface PyQt6, orbe, panneaux et dialogues
├── dashboard/              # serveur et tableau de bord distant
├── mobile/ano_remote/      # application Android
├── plugins/                # extensions utilisateur
├── config/                 # exemples et réglages
├── docs/                   # guides et dépannage
└── tests/                  # tests automatisés
```

La boucle vocale, l'interface Qt et les actions vivent dans des composants distincts. Le serveur MCP réutilise le pont d'outils de l'application.

## 🔌 Étendre ANO-GPT

Les plugins recommandés prennent la forme d'un dossier avec `plugin.json` et `main.py`. Ils apparaissent dans **Menu → Plugins** et leurs outils deviennent disponibles à la prochaine connexion vocale. Consultez le [guide de création](plugins/PLUGIN_AUTHORING_GUIDE.md) et le [résumé du système de plugins](plugins/README.md).

Les développeurs peuvent aussi enrichir `actions/`, les déclarations d'outils de `core/tool_dispatcher.py` et les composants visuels de `ui/`.

## 📚 Documentation

| Guide | Sujet |
| --- | --- |
| [Audio sur Arch](docs/audio_setup_arch.md) | PipeWire, débruitage, VAD et diagnostics micro. |
| [Reconnaissance vocale](docs/RECONNAISSANCE_VOCALE.md) | Transcription et fidélité des commandes. |
| [Azure OpenAI](docs/AZURE_OPENAI.md) | Configuration des modèles et rôles spécialisés. |
| [MCP](docs/MCP.md) | Connexion des agents et Mode Agent Fantôme. |
| [ANO Remote](mobile/ano_remote/README.md) | Installation et usage de l'application Android. |
| [Téléphonie Android](docs/TELEPHONIE_ANDROID.md) | Appels et messages depuis le téléphone. |
| [Localisation](docs/LOCALISATION.md) | Carte, globe, recherche locale et navigation. |
| [Connexions cloud](docs/CLOUD_INTEGRATIONS.md) | Notion, Figma et services dans Chrome. |
| [Fiabilité et diagnostics](docs/MISE_A_JOUR_FIABILITE_2026-09.md) | Exécution des outils, contrôle distant et vérifications. |
| [Plugins](plugins/PLUGIN_AUTHORING_GUIDE.md) | Développer une extension. |

## 🤝 Contribuer

Les contributions sont les bienvenues : signalez un problème avec ses étapes de reproduction, proposez une amélioration ou ouvrez une pull request ciblée. Pour vérifier les changements Python :

```bash
python -m pytest -q
ruff check .
```

Les fonctions vocales, visuelles et Android demandent aussi une vérification sur les appareils et services concernés.

### 🙌 Origine et crédits

Le projet s'appuie sur une base d'assistant personnel présentée auparavant sous le nom **MARK XLIX**, créée par [FatihMakes](https://www.youtube.com/@FatihMakes). ANO-GPT poursuit son développement avec ses propres intégrations, son interface, son contrôle distant et ses outils. Merci aux auteurs des bibliothèques et projets libres utilisés par l'application.

---

<div align="center">

**ANO-GPT — votre assistant, votre bureau, votre façon de travailler.** 🚀

Si le projet vous plaît, une ⭐ sur le dépôt aide à le faire découvrir.

</div>
