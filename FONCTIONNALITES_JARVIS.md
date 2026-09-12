# ⚡ ANO-GPT (JARVIS) — Inventaire Exhaustif de Toutes les Fonctionnalités

> **Document de référence absolue**  
> Ce document recense **l'intégralité sans exception** des fonctionnalités, outils, modules, automatismes, capacités vocales, visuelles, système et réseau intégrés dans l'assistant **ANO-GPT (JARVIS)**.  
> *Dernière révision : Septembre 2026 — Socle stabilisé, 697 tests passés, 57 outils internes, 40 outils MCP exposés.*

---

## 📑 Sommaire Général

1. [Architecture & Principes Fondamentaux](#1-architecture--principes-fondamentaux)
2. [Cœur Audio, Voix & Élocution](#2-cœur-audio-voix--élocution)
3. [Perception Visuelle, Écran & Caméras](#3-perception-visuelle-écran--caméras)
4. [Contrôle Système, Bureau & Hyprland](#4-contrôle-système-bureau--hyprland)
5. [DevSecOps & Administration Linux Arch](#5-devsecops--administration-linux-arch)
6. [Mémoire Longue Durée, Second Brain & RAG](#6-mémoire-longue-durée-second-brain--rag)
7. [Productivité, Agenda, E-mails & Contacts](#7-productivité-agenda-e-mails--contacts)
8. [Navigation GPS, Cartographie & Lieux](#8-navigation-gps-cartographie--lieux)
9. [Médias, Musique, Vidéo & YouTube](#9-médias-musique-vidéo--youtube)
10. [Agents Autonomes, Tâches de Fond & Proactivité](#10-agents-autonomes-tâches-de-fond--proactivité)
11. [Modules Spécialisés (Sparring Partner, Bouclier Focus...)](#11-modules-spécialisés)
12. [Interface HUD PyQt6, Mini-Orbe & Dashboard Web](#12-interface-hud-pyqt6-mini-orbe--dashboard-web)
13. [Écosystème Mobile Android (ANO-Remote)](#13-écosystème-mobile-android-ano-remote)
14. [Serveur MCP (Model Context Protocol) & Outils Exposés](#14-serveur-mcp-model-context-protocol--outils-exposés)
15. [Catalogue Exhaustif des 57 Outils d'Exécution](#15-catalogue-exhaustif-des-57-outils-dexécution)
16. [Raccourcis Clavier, Contrôle CLI & Vocabulaire Vocal](#16-raccourcis-clavier-contrôle-cli--vocabulaire-vocal)

---

## 1. Architecture & Principes Fondamentaux

- **IA Temps Réel Native (Gemini Live API)** : Connexion WebSocket bidirectionnelle continue avec streaming audio natif PCM 16/24 kHz, garantissant une latence d'interaction vocale ultra-faible (< 500 ms).
- **Conception Machine Modeste** :
  - Optimisé pour 2 cœurs / 4 threads, 11 Go de RAM sous **Arch Linux (EndeavourOS)** avec **Hyprland / Wayland**.
  - Gestion stricte du GIL (Global Interpreter Lock) partagé entre l'interface Qt6 et la boucle audio `asyncio`.
  - Pas d'animations superflues bloquantes ni d'inférences de gros LLMs locaux gourmands sur la voix.
- **Règle du Half-Duplex Protégé** : L'assistant ne peut jamais s'entendre parler lui-même. Pendant l'émission vocale, l'entrée micro vers le modèle distant est hermétiquement coupée dans le callback audio.
- **Règle de la Carte Unique (`core/map_render.py`)** : Une seule et unique carte plein écran dans l'application pour préserver les ressources graphiques et la mémoire.
- **Navigateur Unique Dédié** : Politique stricte imposant **Google Chrome** (`core/browser_policy.py`) pour toutes les ouvertures web, recherches et flux OAuth, sans repli divergent vers Firefox.
- **Sécurité & Trousseau Système** : Tokens d'accès et clés chiffrés via le trousseau `keyring`, confirmation visuelle obligatoire pour les commandes système destructrices.

---

## 2. Cœur Audio, Voix & Élocution

### 2.1 Moteur de Synthèse Vocale (TTS)
- **Voix Native Gemini Live** : Voix fluide et ultra-expressive avec modulation émotionnelle native.
- **Voix ElevenLabs HD (`core/elevenlabs_voice.py`)** : Intégration haute fidélité pour une voix ultra-réaliste avec gestion de cache audio.
- **Piper TTS Hors-Ligne** : Synthèse vocale locale rapide de secours en cas de perte de connexion réseau.

### 2.2 Traitement Acoustique & Débruitage Avancé
- **DeepFilterNet 3 (`libdf.so`, `core/noise_suppressor.py`)** : Réduction neuronale du bruit de fond en temps réel (suppression du vent, ventilateur PC, pluie, bruits de clavier).
- **Annulation d'Écho Matérielle PipeWire (`core/echo_canceller.py`)** : Exploitation de `module-echo-cancel` pour éliminer le retour des haut-parleurs dans le microphone.
- **Silero Neural VAD (`core/vad_silero.py`)** : Détection d'activité vocale basée sur un réseau de neurones profond pour discriminer la voix humaine des bruits ambiants.
- **Détecteur Double-Talk (`core/double_talk_detector.py`)** : Détection précise des chevauchements de parole pour empêcher les lancements intempestifs.

### 2.3 Interruption Vocale Instantanée (Barge-In Local)
- **Détection Locale Vosk sans Latence Cloud (`core/barge_in.py`, `core/local_barge_in.py`)** :
  - Pendant qu'ANO parle, un modèle Vosk léger tourne localement sur le flux micro filtré.
  - Coupe immédiatement la synthèse vocale sur des mots-clés d'arrêt : *« ANO stop »*, *« Stop »*, *« Attends »*, *« Tais-toi »*.
  - Rétablissement instantané du micro en écoute sans clic ni intervention manuelle.
- **Interruption Matérielle Dédiée** : Déclenchable via raccourci global Hyprland (`SUPER + SHIFT + X`) ou commande `./anogpt-ctl interrupt`.

### 2.4 Wake Word Hors-Ligne & Respect de la Vie Privée
- **Détection Locale Vosk (`core/wake_word.py`)** :
  - Tant que le micro est en veille, aucun flux audio n'est transmis à Google.
  - Détection locale du mot déclencheur (« ANO ») avec dictionnaire phonétique adapté à l'accent français.
  - Script de calibration personnelle (`scripts/calibrate_wake_word.py`) pour affiner la détection selon le timbre de la voix.

### 2.5 Conversation Continue (Follow-up Window)
- **Dialogue Sans Répétition (`core/continuous_conversation.py`)** :
  - Après chaque réponse de l'assistant, la fenêtre d'écoute reste active pendant 20 à 30 secondes.
  - Permet d'enchaîner les questions et ordres sans devoir répéter le wake word.
  - Fermeture immédiate et élégante de la session sur des formules de conclusion (*« Merci »*, *« C'est bon »*, *« C'est tout »*) ou après silence prolongé.

### 2.6 Prosodie Adaptative & Reconnaissance Émotionnelle
- **Analyseur de Prosodie Acoustique (`core/prosody_analyzer.py`, `core/prosody.py`)** :
  - Mesure en direct du pitch, de l'énergie et de la vitesse de la voix de l'utilisateur.
  - **Mode Urgence** : Réponses courtes, vives (+18% de débit) et concises dès qu'une panique ou une alerte critique est détectée.
  - **Mode Nocturne / Calme (21h-7h)** : Voix douce, posée (-12% de débit), feutrée et prévenante.
  - **Mode Focus / Développement** : Réponses ultra-techniques, sobres et directes sans bavardage.
  - **Gestion des Répétitions** : Raccourcissement drastique (2 à 5 mots) si l'utilisateur reformule la même question.
- **Sélecteur de Styles Vocaux (`voice_style`)** : Choix entre style *professionnel*, *stark* (ironique et vif), ou *synthétique*.

### 2.7 Sécurité Biométrique du Locuteur (Voice ID / Speaker ID)
- **Empreinte Vocale Locale (`core/speaker_id.py`, `actions/voice_id.py`)** :
  - Modèle d'extraction d'embeddings vocaux ECAPA-TDNN / Resemblyzer.
  - Enrôlement vocal : *« ANO, apprends ma voix, mon nom est Anonymous »*.
  - **Anneau de Confiance (Security Ring)** : Blocage catégorique des actions sensibles (suppression de fichiers, extinction, arrêts de services, commandes root) si la voix détectée est inconnue ou suspecte.

### 2.8 Audio Spatial 3D & Routage PipeWire Intelligent
- **Binaural Audio Engine (`core/spatial_audio.py`)** : Rendu audio spatialisé 3D positionnant les notifications et avertissements dans l'espace sonore.
- **Protection Anti-HFP Bluetooth (`core/audio_router.py`)** : Maintien du codec haute fidélité A2DP sur casque Bluetooth lors de l'activation micro, en déroutant la captation micro sur le PC ou micro externe. Détection et bascule à chaud des périphériques USB/Bluetooth.

---

## 3. Perception Visuelle, Écran & Caméras

### 3.1 Conscience d'Écran Continue (Screen Consciousness)
- **Sonde Ambiante Active (`core/screen_consciousness.py`, `core/context_probe.py`)** :
  - Interrogation légère et mise en cache de l'état de la fenêtre active sous Hyprland (`hyprctl activewindow`).
  - L'assistant sait en permanence quelle application est ouverte (Kitty, VS Code, Chrome), son titre et son bureau.
  - Permet de comprendre instantanément les déictiques : *« Ferme ça »*, *« C'est quoi ce bug ? »*, *« Résume cette page »*.

### 3.2 Auto-Debug & Analyse d'Erreurs en Direct (`auto_debug`, `live_auto_debug`)
- **Interception Visuelle des Pannes (`actions/auto_debug.py`, `core/auto_debug.py`)** :
  - Capture automatique de la fenêtre de code ou du terminal actif.
  - Détection et extraction des tracebacks Python, panics Rust, erreurs de compilation C++/C, exceptions JavaScript.
  - Recherche du fichier source correspondant sur disque.
  - Affichage d'une carte HUD de diagnostic avec explication de la cause racine.
  - Proposition de correctif automatisé avec création préalable d'un fichier de sauvegarde `.bak` (soumis à validation).

### 3.3 Vision Multimodale Dédiée (`inspect_screen`, `core/multimodal_vision.py`)
- **Inspection Experte d'Écran** :
  - Analyse d'images fixes ou de fenêtres selon plusieurs domaines spécialisés :
    - `architecture` : Compréhension de diagrammes UML, schémas réseau, flux de données.
    - `chart` : Lecture et interprétation de graphiques, courbes et dashboards boursiers/serveurs.
    - `document` : Lecture et extraction de texte de documents PDF ou scans.
    - `code` : Revue de syntaxe et détection de failles.
  - Pré-filtrage par OCR local pour minimiser le coût en jetons et optimiser la latence.

### 3.4 Pointeur Visuel Interactif & Surlignage (`point_on_screen`, `ui/visual_pointer.py`)
- **Guidage Visuel Direct sur l'Écran de l'Utilisateur** :
  - **Mode Laser** : Point rouge pulsant avec ondes de choc concentriques aux coordonnées `[x, y]`.
  - **Mode Highlight** : Encadrement néon cyberpunk pulsant avec flèche directionnelle sur une boîte `[x, y, w, h]` ou coordonnées normalisées (0-1000).
  - **Mode Trajectoire** : Ligne animée indiquant un cheminement de clics ou de navigation.

### 3.5 Studio Caméra Multi-Sources (`camera_control`, `camera`, `core/camera_studio.py`)
- **Pilotage Caméra Intégré** :
  - Gestion de la webcam intégrée/USB du PC et des caméras du smartphone Android relié (ANO-Remote).
  - Bascule transparente entre objectif frontal (selfie) et objectif dorsal (arrière).
  - Déclenchement de clichés photo et enregistrement vidéo directement affichés dans le HUD.

### 3.6 Capture & Enregistrement Vidéo Wayland (`capture_control`, `screenshot`, `actions/capture.py`)
- **Capture Conforme Wayland/Hyprland** :
  - Exploitation des outils natifs Wayland (`grim`, `slurp`, `gpu-screen-recorder`).
  - Capture de l'écran entier, d'une fenêtre spécifique ou d'une région délimitée à la souris.
  - Enregistrement vidéo d'écran avec choix des pistes audio (son système, micro ou mixte).
  - Annotation immédiate via `swappy` et copie directe dans le presse-papiers (`wl-copy`).
  - Sauvegarde organisée dans `~/Images/Captures` et `~/Vidéos/Enregistrements`.

---

## 4. Contrôle Système, Bureau & Hyprland

### 4.1 Lanceur d'Applications Intelligent (`open_app`, `actions/app_control.py`)
- **Lancement Accéléré (< 100 ms)** : Lancement direct de n'importe quelle application installée sur Arch Linux.
- **Routage de Bureau Hyprland** : Placement automatique sur un espace de travail précis (ex: *« Lance Kitty sur le bureau 3 »*).
- **Injection de Commandes Initiales** : Exécution de commandes dans l'application au démarrage (ex: *« Lance Kitty et tape codex »*).
- **Lancement Invisible Dédié** : Paramètre `hidden=True` propulsant l'application sur le workspace spécial caché de Hyprland (`special:hidden`), sans afficher la fenêtre.

### 4.2 Fermeture & Gestion Intelligente des Fenêtres (`close_app`, `actions/close_app.py`)
- **Fermeture Ciblée** : Terminaison propre (SIGTERM) ou forcée (SIGKILL) via `hyprctl dispatch closewindow`.
- **Désambiguïsation d'Instances** : Si plusieurs fenêtres du même type sont ouvertes, ANO demande laquelle fermer ou cible précisément celle qui vient d'être utilisée.
- **Protection par Défaut** : Ne ferme qu'une seule fenêtre à la fois, sauf mention explicite (*« Ferme toutes les fenêtres Chrome »*).

### 4.3 Orchestrateur Hyprland Dynamique (`hypr_orchestrator`, `actions/hypr_orchestrator.py`)
- **Agencement Automatique d'Espace de Travail** :
  - `organize` : Répartit automatiquement les fenêtres ouvertes vers leurs workspaces désignés.
  - `preset` : Applique des configurations complètes de fenêtres prédéfinies :
    - `devsecops` : Terminaux de surveillance, logs et Docker.
    - `coding` : IDE VS Code + terminal Kitty côte à côte.
    - `monitoring` : `btop`, sondes réseau et status.
    - `web` : Navigateur Google Chrome plein écran.
  - Déplacement unitaire de fenêtres et gestion du focus (`core/hypr_focus.py`).

### 4.4 Réglages Matériels & Raccourcis (`computer_settings`, `computer_control`)
- **Contrôle Audio** : Volume système (réglage 0-100%, incrément, sourdine/mute) via `pactl`.
- **Contrôle Luminosité** : Ajustement de la luminosité de l'écran via `brightnessctl`.
- **Gestion Énergie & Session** : Verrouillage immédiat de l'écran (`lock`), mise en veille, bascule thème sombre/clair.
- **Émulation Clavier & Souris Universelle** : Envoi de raccourcis globaux, clics souris et saisies textuelles automatisées via `ydotool` et `wtype`.

### 4.5 Surveillance Télémétrique du Matériel (`system_status`, `actions/system_monitor.py`)
- **Mesure Temps Réel** :
  - Utilisation CPU par cœur et fréquence.
  - Consommation mémoire vive (RAM) et Swap.
  - Charge et température GPU Intel HD Graphics.
  - Espace disque restant et partitions saturées.
  - Niveau de batterie et état de charge.
  - Processus les plus gourmands (`top processes`).
- **Alertes Spontanées** : Notifications vocales dès qu'un composant dépasse un seuil critique (disque > 90%, batterie < 15%, surchauffe CPU).

### 4.6 Exécution Shell Sécurisée (`shell_exec`, `actions/shell_exec.py`)
- **Terminal Bash Sécurisé** : Exécution de commandes Linux avec contrôle strict du timeout et gestion des sorties.
- **Filtre Anti-Destruction** : Blocage catégorique des commandes mortelles (`rm -rf /`, écriture brute sur disques `/dev/sd*`, fork bombs).
- **Disjoncteur & Cartes de Confirmation** : Les actions à risque déclenchent une carte de sécurité sur le HUD exigeant une confirmation explicite (clic ou validation vocale).

### 4.7 Pile d'Annulation Universelle (`undo_action`, `core/undo_stack.py`)
- **Rollback d'Actions** : Annulation de la dernière opération réversible exécutée par ANO (restauration d'un fichier écrasé depuis sa copie `.bak`, rétablissement d'un niveau de volume ou de luminosité).

### 4.8 Auto-Réparation & Diagnostics (`self_repair`, `core/self_healing.py`, `core/diagnostics.py`)
- **Surveillance Continue des Outils** : Calcul des métriques de fiabilité (taux d'échec, latence p95) par outil dans `core/tool_stats.py`.
- **Circuit Breaker Intégré** : Désactivation temporaire d'un outil en panne pour éviter le blocage du système.
- **Diagnostic CLI `./anogpt-ctl doctor`** : Vérification complète hors-ligne des dépendances, de l'espace disque, des clés d'API et de l'intégrité de la configuration.

---

## 5. DevSecOps & Administration Linux Arch

### 5.1 Gestion des Conteneurs Docker
- **Supervision & Cycle de Vie** :
  - Listage des conteneurs actifs et arrêtés.
  - Redémarrage de stacks complètes (`restart_stack`).
  - Purge des conteneurs éteints, réseaux inutilisés et images dangling (`purge_dead`).
  - Consultation et streaming des journaux de conteneurs (`logs`).

### 5.2 Contrôle des Services Systemd
- **Gestion des Unités** :
  - Inspection de l'état d'un service (`status`).
  - Redémarrage propre d'unités défaillantes (`restart`).
  - Détection automatique et diagnostic immédiat des services en panne (`list_failed`, `diagnose`).

### 5.3 Gestionnaire de Paquets Arch Linux (`pacman` & `yay`)
- **Maintenance Système Conforme Arch** :
  - Vérification des mises à jour système en attente sans installation risquée à l'aveugle (`check_updates`).
  - Recherche de paquets officiels et paquets AUR (`search`).
  - Nettoyage des paquets orphelins et résidus de compilation (`clean_orphans`).

### 5.4 Audit Sécurisé Git & Prévention des Fuites
- **Contrôle de Version Intelligent** :
  - Analyse du statut de travail Git (`status`).
  - **Scan Anti-Fuite de Secrets** : Analyse statique préventive détectant clés d'API, tokens JWT, clés privées SSH avant tout enregistrement.
  - Création de commits conventionnels automatisés (`git commit`) avec synthèse des modifications.
  - Rebase et gestion des branches locales.

### 5.5 Audit Réseau & Sécurité de l'Hôte
- **Sentinelle Réseau** :
  - Audit complet des ports TCP/UDP ouverts et sockets en écoute (`ss`, `nmap`).
  - Identification des démons exposés sur l'extérieur.

---

## 6. Mémoire Longue Durée, Second Brain & RAG

### 6.1 Mémoire Sémantique Longue Durée (`memory_save`, `memory_search`, `core/memory_store.py`)
- **Base SQLite FTS5 Optimisée** : Stockage persistant ultra-léger (temps de requête < 1 ms sur 2 cœurs, sans base vectorielle lourde).
- **Catégories Dédiées** : `identity` (identité de l'utilisateur), `preferences` (goûts, habitudes), `projects` (projets de code), `relationships` (proches), `wishes` (envies), `notes`.
- **Enrichissement Sémantique à l'Écriture (`core/semantic_enricher.py`)** : Génération asynchrone d'alias et synonymes par l'IA lors de l'enregistrement d'un souvenir (ex: « Peugeot » indexé avec « voiture », « garage », « auto »).
- **Rappel Dynamique Invisible** : Injection automatique des 3 à 5 souvenirs les plus pertinents dans le prompt de chaque tour.

### 6.2 Second Brain Associatif (`second_brain`, `actions/second_brain.py`)
- **Recherche Croisée Multi-Sources** : Recherche associative traversant l'historique des conversations, les souvenirs, les fichiers indexés, les commandes passées et les contacts.
- **Mémorisation de Commandes Utiles (`save_command`)** : Stockage et rappel de syntaxes complexes (ex: commandes FFmpeg ou Docker).
- **Indexation de Projets (`index_project`)** : Cartographie des répertoires de développement.
- **Générateur de Graphes de Connaissances (`core/knowledge_graph.py`)** : Génération de diagrammes Mermaid et Graphviz illustrant les relations entre concepts, projets et outils.

### 6.3 Personal RAG Syntaxique (`search_personal_docs`, `core/personal_rag.py`)
- **Découpage Syntaxique Tree-Sitter** :
  - Analyse structurelle du code source (`.py`, `.js`, `.ts`, `.sh`, `.rs`) découpé par classes et fonctions.
- **Découpage Hiérarchique Markdown / PDF** :
  - Découpage par titres avec fil d'Ariane contextuel pour documents de documentation et notes.
- **Citations Chirurgicales** : Restitution d'extraits exacts avec numéros de lignes et liens cliquables directs au format `file:///home/...`.

### 6.4 Mémoire Épisodique & Continuité de Session (`core/memory_episode.py`)
- **Résumés de Sessions Automatiques** : À chaque fin de session de dialogue, un résumé en deux lignes est synthétisé et horodaté en arrière-plan, garantissant une continuité parfaite d'un jour sur l'autre.

---

## 7. Productivité, Agenda, E-mails & Contacts

### 7.1 Intégration Gmail OAuth 2.0 Sécurisée (`email`, `core/email_service.py`)
- **Accès Restreint en Lecture Seule (`gmail.readonly`)** : Tokens OAuth stockés dans le trousseau système.
- **Consultation Rapide** : Compte des e-mails non lus, aperçu des messages récents, résumés de fils de discussion.
- **Recherche Avancée Multi-Critères** : Filtrage par expéditeur (`from`), destinataire (`to`), objet (`subject`), intervalle de dates (`after`, `before`), pièces jointes (`has_attachment`), libellés (`label`), statut important/étoilé.
- **Notification Non-Intrusive** : Présentation sous forme de carte HUD concise avec objet et expéditeur, sans lecture brute interminable.

### 7.2 Google Calendar & Agendas CalDAV (`calendar`, `core/calendar_service.py`)
- **Gestion Complète d'Agenda** :
  - Consultation des rendez-vous et des créneaux libres.
  - Création, modification et suppression d'événements (formats ISO 8601 ou journées entières).
  - Gestion des participants (invités) couplée au carnet de contacts local.
- **Veilleur d'Événements Proactif (`core/calendar_watcher.py`)** : Surveillance d'arrière-plan alertant vocalement 10 minutes avant le début d'une réunion.

### 7.3 Carnet de Contacts Multi-Plateformes (`contacts`, `core/contacts.py`)
- **Base de Contacts Unifiée** :
  - Gestion des identités, surnoms (alias), numéros de téléphone et adresses e-mail.
  - Enregistrement des identifiants de messagerie : WhatsApp, Telegram, Signal, Discord, Instagram, Messenger.

### 7.4 Passerelle de Messagerie Instantanée (`send_message`, `actions/send_message.py`)
- **Envoi de Messages Vocalement** :
  - Composition de messages à destination des plateformes sociales courantes.
  - Affichage préalable d'une carte de prévisualisation dans le HUD avant transmission effective.

### 7.5 Rappels Natifs Persistants (`reminder`, `actions/reminder.py`)
- **Gestion des Échéances** :
  - Définition d'alarmes et rappels programmés basés sur des timers natifs `systemd` (résistant au redémarrage de la machine).
  - Gestion en langage naturel (*« Rappelle-moi de sortir le pain dans 20 minutes »*).

### 7.6 Météo Temps Réel & Prévisions (`weather`, `actions/weather_report.py`)
- **Bulletin Météorologique Complet** :
  - Températures actuelles et ressenties, vitesse du vent, probabilité de précipitations.
  - Prévisions pour la journée ou les jours à venir avec carte visuelle illustrée.

### 7.7 Recherche Web Multi-Modes (`web_search`, `actions/web_search.py`)
- **Recherche Structurée sans Ouvrir de Navigateur** :
  - `search` : Recherche d'informations générale avec synthèse de réponses.
  - `news` : Dernières actualités chaudes.
  - `research` : Synthèse documentaire approfondie croisant plusieurs sources.
  - `price` : Recherche et extraction de tarifs de produits.
  - `compare` : Comparatif de caractéristiques entre plusieurs produits.
  - `headlines` : Grands titres de la presse tech et internationale.

### 7.8 Briefing Matinal Condensé (`core/daily_briefing.py`)
- **Rituel Quotidien Parlé (30 secondes chrono)** :
  - Déclenché au premier démarrage ou à la salutation du matin.
  - Synthèse vocale fluide regroupant l'heure, la météo locale, les rendez-vous du calendrier, le nombre de mails importants, l'état de santé du PC et deux actualités technologiques pré-chargées en tâche de fond.

---

## 8. Navigation GPS, Cartographie & Lieux

### 8.1 Carte Interactive Plein Écran (`core/map_render.py`)
- **Affichage Cartographique Vectoriel Unique** :
  - Fond de carte Leaflet / OpenStreetMap fluide et interactif plein cadre.
  - Conforme à la règle de la carte unique (aucun dédoublement de vue).

### 8.2 Recherche de Lieux & Commerces (« Où Trouver X ») (`find_nearby`, `actions/find_nearby.py`)
- **Recherche Spatiale Overpass OpenStreetMap** :
  - Recherche locale sans API propriétaire payante : pharmacies de garde, restaurants, distributeurs, stations, supermarchés.
  - Épinglage automatique de tous les résultats sur la carte avec fiches commerces (adresse, distance, horaires).

### 8.3 Guidage GPS Parlé Pas-à-Pas (`navigate`, `actions/navigation.py`, `core/navigation.py`)
- **Navigation Routière & Piétonne Complète** :
  - Calcul d'itinéraires via les moteurs de routage **Valhalla** et **OSRM** (modes voiture, piéton, vélo).
  - Instructions de guidage pas-à-pas traduites et prononcées vocalement par le TTS local (*« Dans 150 mètres, tournez à gauche sur Boulevard Diallo »*).
  - Suivi en temps réel de la position GPS poussée par l'application smartphone ANO-Remote.
  - **Recalcul Automatique d'Itinéraire** : Détection de sortie de route (> 60 mètres pendant 10 secondes) et recalcul immédiat.

### 8.4 Géolocalisation Ambiante Multi-Sources (`location`, `core/geolocation.py`)
- **Résolution de Position** :
  - Priorité absolue aux coordonnées GPS ultra-précises transmises par le téléphone portable.
  - Repli automatique sur la géolocalisation IP et le cache géographique.

---

## 9. Médias, Musique, Vidéo & YouTube

### 9.1 Lecteur Musical Headless Intégré (`music`, `actions/music.py`, `core/player_ipc.py`)
- **Lecteur MPV Headless Invisible** :
  - Lecture en tâche de fond sans aucune fenêtre parasite ouverte (`--no-video`, socket JSON IPC).
  - Indexation et exploration de la bibliothèque locale (`~/Musique`, 1000+ morceaux).
  - Recherche floue par artiste, titre, album.
  - **Bascule Automatique sur YouTube** : Si un morceau demandé n'est pas présent sur le disque local, bascule immédiate en streaming audio YouTube sans friction.
  - Carte multimédia HUD interactive : pochette d'album, scrub bar interactive, volume, pause/reprise, suivant/précédent, mode aléatoire (shuffle).

### 9.2 Lecteur Vidéo Local Intégré (`core/local_video.py`)
- **Visionnage Direct de Médias Vidéo** :
  - Affichage direct de clips et vidéos locales dans le lecteur natif sans lancer VLC.

### 9.3 Contrôle Avancé de YouTube (`youtube`, `actions/youtube_video.py`, `core/youtube_service.py`)
- **Pilotage Vidéo Universel** :
  - Recherche et lancement de vidéos en 150 ms.
  - Contrôles de lecture instantanés : play, pause (50 ms), avance/retour rapide, plein écran, sous-titres, ajustement de vitesse (1.25x, 1.5x).
  - Récupération de la transcription textuelle (`transcript`).
  - **Synthèse & Résumé Vidéo à la Volée (`summarize`)** : Téléchargement et analyse des sous-titres pour produire un résumé structuré avec chapitrage cliquable.

### 9.4 Recherche d'Images & Galerie Plein Écran Native (`image_search`, `actions/image_search.py`)
- **Visualiseur d'Images Local Dédié** :
  - Recherche d'images sur le web en arrière-plan.
  - Rendu immédiat dans la galerie plein écran native d'ANO-GPT, évitant l'ouverture d'onglets de navigateur superflus.

---

## 10. Agents Autonomes, Tâches de Fond & Proactivité

### 10.1 Mode Agent Fantôme (`ghost_agent.py`, `actions/background_tasks.py`, `background_tasks`)
- **Délégation Asynchrone à un Sous-Agent MCP / Antigravity (`agy`)** :
  - Permet de confier des missions complexes de développement, d'analyse de code ou de refactoring en tâche de fond.
  - L'agent fantôme travaille de manière autonome sans monopoliser la conversation vocale principale.
  - Affichage facultatif du journal d'exécution dans un terminal Kitty dédié.
  - Synthèse vocale annonçant les fichiers créés ou modifiés une fois la mission achevée.

### 10.2 Veilles & Surveillances Persistantes
- **Surveillance de Prix Web (`watch_price`)** : Scrutation périodique d'une page produit et alerte vocale dès qu'un prix seuil est atteint.
- **Attente de Fin de Build / Processus (`wait_build`)** : Surveillance d'une commande longue dans un terminal et notification vocale à la complétion.
- **Rappels Géolocalisés d'Arrivée (`wait_arrival`)** : Déclenchement d'un message lorsque le smartphone entre dans le périmètre du domicile (rayon de 250 m).

### 10.3 Moteur de Routines & Macros Déterministes (`routine`, `core/routines.py`)
- **Enchaînements d'Actions sans Latence IA** :
  - Configuration déclarative dans `config/routines.yaml`.
  - Déclenchement par phrase clé (ex: *« Mode travail »*, *« Je pars »*, *« Mode nuit »*).
  - Exécution instantanée en local (lancement d'IDE, agencement d'espaces de travail Hyprland, lancement de playlist, verrouillage).

### 10.4 Moteur Prédictif d'Habitudes (`core/habit_model.py`)
- **Apprentissage Statistique des Usages** :
  - Enregistrement discret des actions réussies par tranche de 30 minutes et jour de la semaine.
  - Suggestion proactive d'actions récurrentes (ex: *« Tu lances souvent VS Code à cette heure-ci, je l'ouvre ? »*).
  - Politique stricte anti-spam : 1 suggestion max par jour, apprentissage sur 4 jours distincts minimum.
  - Prise en compte immédiate des refus : un « Non » bloque la suggestion pendant 30 jours.

### 10.5 Raisonnement Profond Multi-Tours (`deep_think`, `core/agent_brain.py`)
- **Mode Réflexion Analytique** : Activation d'une chaîne de pensée approfondie avec sous-agents spécialisés pour les problèmes d'architecture et de logique complexe, protégée contre les boucles infinies (`LOOP_GUARD_ENV`).

---

## 11. Modules Spécialisés

### 11.1 Sparring Partner & Simulateur d'Entraînement (`sparring_partner`, `actions/sparring_partner.py`)
- **Coach Vocal pour Entretiens & Négociations** :
  - Simulation de jeux de rôles interactifs : entretiens d'embauche techniques (junior à expert), soutenances, négociations commerciales.
  - Respect scrupuleux du personnage : pose une seule question à la fois, formule des contre-arguments et objections adaptées.
  - **Rapport d'Élocution & Débriefing Complet** : Décompte des tics verbaux (*« euh »*, *« en fait »*), analyse de la vitesse d'élocution (mots/minute) et conseils de concision.

### 11.2 Bouclier Anti-Distraction & Gardien de Dopamine (`focus_guard`, `core/distraction_guard.py`)
- **Session de Concentration (Flow State)** :
  - Minuteur de travail (ex: 50 min) avec pauses programmées (10 min).
  - Surveillance des onglets du navigateur via le protocole Chrome DevTools (CDP).
  - **Blocage Chirurgical du Doomscrolling** : Fermeture automatique des flux de vidéos courtes (YouTube Shorts, flux infinis) sans bloquer les vidéos de travail ni la documentation.
  - Calcul d'un score de dispersion cognitive et possibilité de restauration automatique des onglets à la fin de la session.

### 11.3 Contrôle Gestuel par Caméra (`core/gesture_control.py`)
- **Pilotage par Gestes Visuels** : Reconnaissance de mouvements de la main via la webcam pour mettre en pause la musique ou monter le volume sans toucher le clavier.

### 11.4 Gestionnaire Dynamique de Plugins (`plugin_manager`, `core/plugin_registry.py`)
- **Extensibilité Sans Redémarrage** : Découverte, chargement à chaud, activation et désactivation de modules d'extension Python placés dans le dossier `plugins/`.

---

## 12. Interface HUD PyQt6, Mini-Orbe & Dashboard Web

### 12.1 Interface Holographique Principale (`ui/jarvis_ui.py`, `ui/main_window.py`)
- **Orbe Réactif GPU (Fragment Shader GLSL + Analyse FFT)** :
  - Orbe central tridimensionnel calculé sur GPU (`ui/orb/glsl_orb.py`).
  - Réactivité biologique aux fréquences de la voix (graves, médiums, aigus) via transformée de Fourier rapide (FFT).
  - États visuels dynamiques : *veille/respiration*, *écoute attentive*, *réflexion/rotation*, *parole/ondulation*, *action outil/arcs électriques*.
- **Panneaux Translucides Flottants** : Apparition fluide et temporaire des panneaux (lecteur de musique, météo, surveillance système, cartes d'action).
- **Affichage Typewriter Synchronisé** : Retranscription textuelle des réponses sous l'orbe avec effet machine à écrire synchronisé au millième de seconde avec la voix.

### 12.2 Mini-Orbe Permanent Épinglé (`ui/orb/mini_orb.py`)
- **Compagnon Discret en Coin d'Écran** :
  - Bulle compacte sans bordure (~80 pixels) épinglée au-dessus de toutes les fenêtres via Hyprland (`windowrulev2 = float, pin`).
  - Témoin d'état permanent même lorsque la fenêtre principale de l'assistant est minimisée.

### 12.3 Système de Cartes Riches Flottantes (`show_card`)
- **Présentation Visuelle Structurée** :
  - Rendu de cartes interactives spécialisées : *info*, *result*, *message*, *task*, *error*.
  - Cartes de confirmation d'opérations sensibles avec boutons d'acceptation et de rejet.

### 12.4 Intelligence du Presse-Papiers
- **Barre d'Action Rapide sur Copie de Texte** :
  - Détection automatique de tout texte copié (> 10 caractères).
  - Apparition d'un panneau flottant éphémère proposant 4 actions directes à l'assistant : **Traduire**, **Résumer**, **Expliquer**, **Corriger**.

### 12.5 Tableau de Bord Web & Télécommande Réseau (`dashboard/server.py`)
- **Serveur Web Local & WebSocket Réactif** :
  - Accès distant depuis un smartphone ou un autre ordinateur du réseau local.
  - Appairage sécurisé par scan de QR Code générant un jeton de session chiffré.
  - Télécommande vocale et écrite en temps réel avec affichage de l'orbe et streaming audio.
  - Section d'administration et de diagnostic matériel en direct.

---

## 13. Écosystème Mobile Android (ANO-Remote)

- **Application Android Dédiée (`ANO-Remote.apk`, `mobile/`)** :
  - **Relais GPS Temps Réel** : Envoi continu de la position géographique du smartphone au PC pour alimenter la navigation cartographique et les rappels d'arrivée.
  - **Relais Caméras Smartphone (`core/phone_relay.py`)** : Streaming vidéo haute définition des caméras avant et arrière du téléphone vers le PC, transformant le mobile en caméra sans fil pour JARVIS.
  - **Relais Téléphonique** : Déclenchement d'appels téléphoniques vocaux via le smartphone depuis le PC (`phone_call`).
  - **Synchronisation Audio & Médias** : Passerelle de communication audio bidirectionnelle entre le téléphone et le PC.

---

## 14. Serveur MCP (Model Context Protocol) & Outils Exposés

Le fichier `anogpt_mcp.py` transforme ANO-GPT en serveur **FastMCP** standardisé sur flux `stdio`. Il permet à des agents externes (**Antigravity `agy`**, **Claude Code**, **Codex CLI**, **Claude Desktop**) de prendre le contrôle complet de l'environnement physique et logiciel de la machine.

### Liste des 40 Outils MCP Exposés

| # | Nom de l'Outil MCP | Rôle & Description | Mode Hors-Ligne |
|---|---|---|:---:|
| 1 | `find_nearby` | Recherche de lieux/commerces et épinglage sur la grande carte | ✅ |
| 2 | `show_map` | Affiche un point géographique précis sur la carte plein écran | ❌ |
| 3 | `close_map` | Referme la carte et restitue la vue normale de l'assistant | ❌ |
| 4 | `camera` | Pilote l'affichage caméra (PC ou smartphone, avant/arrière) | ❌ |
| 5 | `show_card` | Affiche une carte visuelle translucide dans le HUD | ❌ |
| 6 | `speak` | Fait prononcer une phrase à voix haute à l'assistant | ❌ |
| 7 | `ask_assistant` | Délègue une question en langage naturel à l'assistant vocal | ❌ |
| 8 | `assistant_status` | Répète l'état actuel (micro, session vocale, caméra) | ✅ |
| 9 | `email` | Recherche, lit et résume les e-mails de la boîte Gmail | ✅ |
| 10 | `calendar` | Lit, crée, modifie et supprime des rendez-vous d'agenda | ✅ |
| 11 | `contacts` | Gère le carnet d'adresses (recherche, ajout, mise à jour) | ✅ |
| 12 | `memory_save` | Enregistre durablement un fait ou une préférence personnelle | ✅ |
| 13 | `memory_search` | Recherche sémantique dans la mémoire longue durée | ✅ |
| 14 | `second_brain` | Recherche associative transversale dans toutes les connaissances | ✅ |
| 15 | `weather` | Bulletin météo actuel et prévisions pour une localité | ✅ |
| 16 | `web_search` | Recherche d'informations, d'actualités et de prix sur le web | ✅ |
| 17 | `image_search` | Recherche et affichage d'images dans la galerie native | ❌ |
| 18 | `close_image_gallery`| Ferme la galerie d'images plein écran | ❌ |
| 19 | `screenshot` | Capture l'écran entier, une fenêtre ou une région | ✅ |
| 20 | `open_app` | Ouvre une application, commande ou fichier sur le bureau | ❌ |
| 21 | `close_app` | Ferme une application ou une fenêtre ciblée | ❌ |
| 22 | `computer_settings` | Ajuste volume, luminosité, veille, verrouillage | ❌ |
| 23 | `file_search` | Recherche rapide de fichiers locaux par nom ou catégorie | ✅ |
| 24 | `search_personal_docs`| Recherche RAG sémantique dans le code et la documentation | ✅ |
| 25 | `location` | Retourne la localisation géographique actuelle connue | ✅ |
| 26 | `youtube` | Recherche et contrôle la lecture de vidéos YouTube | ❌ |
| 27 | `reminder` | Crée, liste ou annule des rappels programmés persistants | ✅ |
| 28 | `system_status` | Télémétrie complète (CPU, RAM, GPU, disques, batterie) | ✅ |
| 29 | `music` | Contrôle la musique locale et le streaming sans fenêtre | ❌ |
| 30 | `routine` | Exécute une routine automatisée multi-actions | ✅ |
| 31 | `voice_id` | Analyse l'empreinte vocale du locuteur ou enregistre sa voix | ❌ |
| 32 | `voice_style` | Consulte ou modifie le style d'élocution (pro, stark, etc.) | ✅ |
| 33 | `self_repair` | Diagnostique et répare les outils défaillants | ✅ |
| 34 | `background_tasks` | Lance des veilles durables (prix, build, sous-agents) | ✅ |
| 35 | `devsecops` | Maître Linux (Docker, systemd, paquets Arch, Git, ports) | ✅ |
| 36 | `hypr_orchestrator` | Agence et déplace les fenêtres sur les bureaux Hyprland | ✅ |
| 37 | `auto_debug` | Analyse une erreur affichée à l'écran et propose un patch | ✅ |
| 38 | `inspect_screen` | Analyse multimodale experte de l'écran (schémas, code) | ✅ |
| 39 | `navigate` | Lance le guidage GPS parlé pas-à-pas avec carte interactive | ✅ |
| 40 | `point_on_screen` | Affiche un pointeur laser ou encadrement néon à l'écran | ✅ |

---

## 15. Catalogue Exhaustif des 57 Outils d'Exécution

Voici la liste complète et ordonnée des **57 outils internes** déclarés et gérés par le répartiteur central (`core/tool_dispatcher.py`) :

```text
 1. open_app              20. contacts_control      39. hypr_control
 2. close_app             21. phone_call            40. devsecops
 3. web_search            22. sparring_partner      41. hypr_orchestrator
 4. image_search          23. focus_guard           42. self_repair
 5. close_image_gallery   24. email_control         43. voice_id
 6. system_status         25. computer_settings     44. voice_style
 7. weather_report        26. browser_control       45. routine
 8. location              27. file_controller       46. save_memory
 9. send_message          28. desktop_control       47. second_brain
10. reminder              29. live_auto_debug       48. deep_think
11. youtube_video         30. code_helper           49. capture_control
12. screen_process        31. dev_agent             50. music_control
13. point_on_screen       32. computer_control      51. background_tasks
14. camera_control        33. game_updater          52. proactive_mode
15. close_camera          34. flight_finder         53. timer
16. show_map              35. shutdown_jarvis       54. undo_action
17. navigate              36. file_processor        55. plugin_manager
18. find_nearby           37. media_control         56. search_personal_docs
19. calendar_control      38. shell_exec            57. close_map
```

### Détail des Spécifications de Chaque Outil

#### 1. `open_app`
- **Description** : Lancement d'applications sur Arch Linux.
- **Paramètres** : `app_name` (string, obligatoire), `command` (string, texte ou commande à injecter), `workspace` (integer, bureau Hyprland visé), `hidden` (boolean, si `true`, lance de façon invisible sur `special:hidden`).

#### 2. `close_app`
- **Description** : Fermeture propre ou forcée d'applications.
- **Paramètres** : `app_name` (string, obligatoire), `list_instances` (boolean, liste les instances en cours), `workspace` (integer), `description` (string, formulation exacte de l'utilisateur pour cibler l'instance).

#### 3. `web_search`
- **Description** : Recherche web multi-modes (Google Grounding / DuckDuckGo).
- **Paramètres** : `query` (string), `mode` (`search` | `news` | `research` | `price` | `compare` | `headlines`), `items` (liste de chaînes), `aspect` (string), `count` (integer).

#### 4. `image_search`
- **Description** : Recherche d'images et affichage direct dans la galerie native.
- **Paramètres** : `query` (string, obligatoire), `limit` (integer, de 1 à 8 images).

#### 5. `close_image_gallery`
- **Description** : Fermeture de la galerie d'images plein écran.
- **Paramètres** : Aucun.

#### 6. `system_status`
- **Description** : Télémétrie matérielle complète de la machine.
- **Paramètres** : `component` (`cpu` | `ram` | `temp` | `gpu` | `disk` | `battery`), `action` (`status` | `uptime` | `processes` | `top` | `component`).

#### 7. `weather_report`
- **Description** : Météo actuelle et prévisions avec carte HUD dédiée.
- **Paramètres** : `city` (string), `time` (string, ex: *« aujourd'hui »*, *« demain »*).

#### 8. `location`
- **Description** : Obtention des coordonnées GPS et adresse actuelle de l'utilisateur.
- **Paramètres** : `refresh` (boolean, force l'actualisation GPS).

#### 9. `send_message`
- **Description** : Envoi de messages vers des messageries sociales.
- **Paramètres** : `platform` (`whatsapp` | `telegram` | `signal` | `discord`), `recipient` (string), `message` (string).

#### 10. `reminder`
- **Description** : Gestion des rappels programmés `systemd`.
- **Paramètres** : `action` (`set` | `list` | `cancel`), `date` (AAAA-MM-JJ), `time` (HH:MM), `message` (string), `value` (identifiant pour annulation), `description` (langage naturel).

#### 11. `youtube_video`
- **Description** : Contrôleur complet du lecteur et service YouTube.
- **Paramètres** : `action` (`search` | `play` | `pause` | `resume` | `speed` | `fullscreen` | `subtitles` | `next` | `previous` | `transcript` | `summarize` | `trending`), `query` (string), `url` (string), `value` (string).

#### 12. `screen_process`
- **Description** : Analyse visuelle ponctuelle de l'écran par Gemini.
- **Paramètres** : `query` (string, question sur ce qui est affiché), `target` (`active_window` | `screen`).

#### 13. `point_on_screen`
- **Description** : Affichage d'un pointeur visuel laser ou encadrement néon à l'écran.
- **Paramètres** : `description` (string), `coordinates` (liste d'entiers `[x, y]` ou `[x, y, w, h]`), `mode` (`auto` | `highlight` | `laser` | `path`), `duration` (float, en secondes).

#### 14. `camera_control`
- **Description** : Pilotage du flux caméra (PC webcam ou smartphone Android).
- **Paramètres** : `action` (`open` | `photo` | `video_start` | `video_stop` | `switch` | `lens` | `flip` | `close`), `source` (`pc` | `phone`), `lens` (`front` | `back`).

#### 15. `close_camera`
- **Description** : Fermeture immédiate du flux caméra affiché.
- **Paramètres** : Aucun.

#### 16. `show_map`
- **Description** : Affichage d'un lieu ou coordonnées sur la carte plein écran unique.
- **Paramètres** : `query` (string), `lat` (float), `lon` (float), `radius_km` (float).

#### 17. `navigate`
- **Description** : Lancement du guidage GPS parlé pas-à-pas avec Valhalla/OSRM.
- **Paramètres** : `destination` (string), `action` (`start` | `stop` | `status`), `mode` (`driving` | `walking` | `cycling`).

#### 18. `find_nearby`
- **Description** : Recherche de commerces et services à proximité (Overpass OSM).
- **Paramètres** : `query` (string), `near` (string), `radius_km` (float).

#### 19. `close_map`
- **Description** : Fermeture de la carte et retour à la vue HUD normale.
- **Paramètres** : Aucun.

#### 20. `calendar_control`
- **Description** : Gestion complète de l'agenda Google Calendar / CalDAV.
- **Paramètres** : `action` (`status` | `connect` | `list` | `create` | `update` | `delete`), `provider` (`auto` | `google` | `caldav`), `title` (string), `start` (ISO 8601), `end` (ISO 8601), `attendees` (liste), `location` (string).

#### 21. `contacts_control`
- **Description** : Gestion du carnet d'adresses personnel unifié.
- **Paramètres** : `action` (`list` | `search` | `add` | `update` | `delete`), `name` (string), `aliases` (liste), `phone` (string), `emails` (liste), identifiants messageries (`whatsapp`, `telegram`, `signal`, etc.).

#### 22. `phone_call`
- **Description** : Lancement d'un appel téléphonique via le relais Android ANO-Remote.
- **Paramètres** : `action` (`call` | `end`), `contact` (string ou numéro de téléphone).

#### 23. `sparring_partner`
- **Description** : Simulateur de jeux de rôles et d'entraînement d'entretiens.
- **Paramètres** : `action` (`start` | `respond` | `feedback` | `stop`), `topic` (string), `role` (string), `difficulty` (`junior` | `intermediate` | `senior` | `expert`), `questions_count` (integer).

#### 24. `focus_guard`
- **Description** : Bouclier anti-distraction et minuteur de session de flow.
- **Paramètres** : `action` (`start` | `status` | `stop` | `restore_tabs`), `duration_minutes` (integer), `break_minutes` (integer), `target_goal` (string).

#### 25. `email_control`
- **Description** : Consultation et recherche avancée dans la boîte Gmail.
- **Paramètres** : `action` (`status` | `unread` | `recent` | `search` | `read` | `summary`), `query` (string), `id` (string), `sender` (string), `recipient` (string), `subject` (string), `after` (date), `before` (date), `has_attachment` (boolean).

#### 26. `computer_settings`
- **Description** : Réglages matériels du bureau Linux.
- **Paramètres** : `action` (`volume_get` | `volume_set` | `volume_up` | `volume_down` | `volume_mute` | `brightness_get` | `brightness_set` | `lock` | `dark_mode` | `show_desktop`), `value` (string), `confirmed` (string).

#### 27. `browser_control`
- **Description** : Navigation web automatisée dans Google Chrome.
- **Paramètres** : `action` (`open` | `search` | `new_tab` | `close_tab` | `next_tab` | `prev_tab` | `history` | `bookmarks`), `url` (string), `query` (string).

#### 28. `file_controller`
- **Description** : Opérations locales sur le système de fichiers.
- **Paramètres** : `action` (`find` | `list` | `info` | `read` | `write` | `delete` | `move` | `copy` | `mkdir`), `path` (string), `name` (string), `extension` (string), `kind` (string).

#### 29. `desktop_control`
- **Description** : Gestion du bureau et des écrans.
- **Paramètres** : `action` (`minimize_all` | `toggle_fullscreen` | `next_workspace` | `prev_workspace`).

#### 30. `live_auto_debug`
- **Description** : Interception en temps réel des plantages à l'écran et génération de diagnostic.
- **Paramètres** : `query` (string), `target` (`active_window`), `auto_apply` (boolean).

#### 31. `code_helper`
- **Description** : Analyse, revue et génération de code source assistée.
- **Paramètres** : `action` (`review` | `explain` | `refactor` | `generate`), `file_path` (string), `instructions` (string).

#### 32. `dev_agent`
- **Description** : Agent autonome pour opérations multi-fichiers de programmation.
- **Paramètres** : `mission` (string), `workspace` (string).

#### 33. `computer_control`
- **Description** : Contrôle bas niveau des entrées clavier et curseur souris (`ydotool`).
- **Paramètres** : `action` (`type` | `key_press` | `mouse_click` | `mouse_move`), `text` (string), `key` (string), `x` (integer), `y` (integer).

#### 34. `game_updater`
- **Description** : Détection et déclenchement des mises à jour Steam et Epic Games.
- **Paramètres** : `platform` (`steam` | `epic`), `action` (`check` | `update`), `game` (string).

#### 35. `flight_finder`
- **Description** : Recherche de vols, prix et disponibilités en temps réel.
- **Paramètres** : `origin` (string), `destination` (string), `date` (string).

#### 36. `shutdown_jarvis`
- **Description** : Arrêt complet et propre de l'assistant ANO-GPT.
- **Paramètres** : Aucun (requiert confirmation explicite).

#### 37. `file_processor`
- **Description** : Extraction de texte et métadonnées de documents complexes (PDF, Office, images EXIF).
- **Paramètres** : `file_path` (string), `action` (`extract_text` | `summarize` | `metadata`).

#### 38. `media_control`
- **Description** : Contrôle des touches multimédias globales du système (`playerctl`).
- **Paramètres** : `action` (`play_pause` | `next` | `previous` | `stop` | `volume_up` | `volume_down`).

#### 39. `shell_exec`
- **Description** : Exécution de commandes Bash sécurisées dans l'environnement Arch Linux.
- **Paramètres** : `command` (string, obligatoire), `timeout` (integer, secondes), `confirm` (boolean pour actions sensibles).

#### 40. `hypr_control`
- **Description** : Commandes directes au compositeur Hyprland (`hyprctl`).
- **Paramètres** : `command` (string), `args` (string).

#### 41. `devsecops`
- **Description** : Suite DevSecOps complète (Docker, Systemd, Paquets, Git, Ports).
- **Paramètres** : `domain` (`docker` | `systemd` | `packages` | `git` | `security`), `action` (string), `target` (string), `message` (string).

#### 42. `hypr_orchestrator`
- **Description** : Agencement dynamique des espaces de travail et fenêtres.
- **Paramètres** : `action` (`organize` | `preset` | `move_window`), `preset` (`devsecops` | `coding` | `monitoring` | `web`), `target` (string), `workspace` (string).

#### 43. `self_repair`
- **Description** : Bilan de santé et auto-réparation des outils logiciels.
- **Paramètres** : `action` (`diagnose` | `repair`), `tool` (string, nom de l'outil cible).

#### 44. `voice_id`
- **Description** : Gestion de l'authentification et de l'empreinte biométrique vocale.
- **Paramètres** : `action` (`status` | `enroll` | `forget`), `name` (string).

#### 45. `voice_style`
- **Description** : Sélection du style d'élocution et d'expression de l'assistant.
- **Paramètres** : `style` (`professional` | `stark` | `synthetic`).

#### 46. `routine`
- **Description** : Exécution instantanée de macros et routines utilisateur prédéfinies.
- **Paramètres** : `name` (string, nom de la routine, ex: *« mode travail »*).

#### 47. `save_memory`
- **Description** : Enregistrement manuel d'un fait durable en mémoire longue durée.
- **Paramètres** : `key` (string), `value` (string), `category` (`identity` | `preferences` | `projects` | `relationships` | `wishes` | `notes`).

#### 48. `second_brain`
- **Description** : Recherche et indexation dans le Second Cerveau local.
- **Paramètres** : `action` (`search` | `status` | `reindex` | `save_command` | `index_project` | `note` | `visualize`), `query` (string), `command` (string), `project` (string).

#### 49. `deep_think`
- **Description** : Déclenchement d'une réflexion analytique approfondie multi-tours.
- **Paramètres** : `question` (string).

#### 50. `capture_control`
- **Description** : Gestion complète des captures d'écran et enregistrements vidéo Wayland.
- **Paramètres** : `action` (`screenshot` | `region` | `window` | `start_recording` | `stop_recording` | `status` | `monitors`), `fps` (integer), `audio` (`system` | `mic` | `both` | `none`), `annotate` (boolean).

#### 51. `music_control`
- **Description** : Lecteur musical headless MPV avec repli YouTube streaming.
- **Paramètres** : `action` (`play` | `pause` | `resume` | `next` | `previous` | `stop` | `now_playing` | `shuffle` | `seek` | `volume`), `query` (string), `kind` (`audio` | `video`), `source` (`auto` | `local` | `youtube`).

#### 52. `background_tasks`
- **Description** : Gestion des veilles persistantes et missions d'agents fantômes.
- **Paramètres** : `action` (`delegate` | `watch_price` | `wait_build` | `wait_arrival` | `list` | `cancel`), `url` (string), `target_price` (float), `command_contains` (string), `mission` (string), `workspace` (string).

#### 53. `proactive_mode`
- **Description** : Contrôle des annonces vocales spontanées de l'assistant.
- **Paramètres** : `action` (`silence` | `on` | `status` | `set_home`), `radius_m` (float).

#### 54. `timer`
- **Description** : Gestion de minuteurs simultanés avec bannières HUD animées.
- **Paramètres** : `action` (`set` | `list` | `cancel`), `duration` (en secondes ou minutes), `label` (string).

#### 55. `undo_action`
- **Description** : Annulation de la dernière opération réversible exécutée par ANO.
- **Paramètres** : `action` (`undo` | `list`).

#### 56. `plugin_manager`
- **Description** : Administration des plugins tiers locaux.
- **Paramètres** : `action` (`list` | `enable` | `disable` | `reload`), `name` (string).

#### 57. `search_personal_docs`
- **Description** : Recherche RAG sémantique dans les dépôts git et documents personnels.
- **Paramètres** : `query` (string, obligatoire), `file_pattern` (string, ex: `*.py`), `max_results` (integer).

---

## 16. Raccourcis Clavier, Contrôle CLI & Vocabulaire Vocal

### 16.1 Raccourcis Clavier Hyprland (Configurés dans le Système)
- `SUPER + SHIFT + Espace` : Bascule micro (Écoute active ⇄ Veille) même sur écran verrouillé.
- `SUPER + SHIFT + X` : Interruption vocale instantanée d'urgence (coupe la parole d'ANO).
- `SUPER + SHIFT + W` : Activation / Désactivation du wake word hors-ligne Vosk.

### 16.2 Commandes Terminal (`./anogpt-ctl`)
- `./anogpt-ctl toggle` : Active ou coupe l'écoute du micro.
- `./anogpt-ctl ask "..."` : Envoie une question ou commande textuelle à l'assistant.
- `./anogpt-ctl speak "..."` : Fait prononcer une phrase par la synthèse vocale.
- `./anogpt-ctl status` : Affiche l'état courant de l'assistant (session, micro, caméra).
- `./anogpt-ctl interrupt` : Interrompt la parole en cours.
- `./anogpt-ctl doctor` (ou `--json`) : Lance l'audit complet de santé et de diagnostic local.

### 16.3 Exemples de Formulations Vocales Réelles

#### Contrôle Quotidien & Multimédia
- *« ANO, mets un morceau de jazz »* $\rightarrow$ Recherche locale puis bascule MPV/YouTube sans fenêtre.
- *« Mets pause »* / *« Reprends la musique »* / *« Monte le volume à 70 »*.
- *« Cherche des photos de nébuleuses sur le web »* $\rightarrow$ Galerie native plein écran.
- *« Lance Chrome et ouvre YouTube »* $\rightarrow$ Lancement parallèle optimisé en 150 ms.
- *« Ferme le terminal Kitty que tu viens d'ouvrir »* $\rightarrow$ Fermeture chirurgicale ciblée.

#### Vision & Auto-Debug
- *« ANO, regarde mon écran et explique-moi cette erreur »* $\rightarrow$ Capture, analyse du traceback et fiche diagnostic HUD.
- *« Indique-moi le bouton des paramètres sur l'écran »* $\rightarrow$ Pointeur laser rouge avec ondes concentriques.
- *« Passe sur la caméra de mon téléphone »* $\rightarrow$ Bascule vidéo en direct vers le mobile Android.

#### Système & DevSecOps
- *« Donne-moi le statut Git de mon projet »* $\rightarrow$ Scan anti-fuite de secrets et état du dépôt.
- *« Quels sont les services systemd en panne ? »* $\rightarrow$ Diagnostic immédiat des unités en échec.
- *« Vérifie les mises à jour pacman disponibles sans rien installer »* $\rightarrow$ Consultation sécurisée sans danger.
- *« Applique le preset de fenêtres coding »* $\rightarrow$ Répartition automatique IDE + Terminal sur Hyprland.

#### Mémoire & Productivité
- *« Retiens que le code du portail est 4589 »* $\rightarrow$ Sauvegarde sémantique SQLite avec enrichissement.
- *« Retrouve mon histoire de voiture »* $\rightarrow$ Rappel sémantique de la réparation de la Peugeot.
- *« Quels sont mes rendez-vous prévus aujourd'hui ? »* $\rightarrow$ Consultation Google Calendar.
- *« Ai-je reçu des e-mails importants récents ? »* $\rightarrow$ Consultation Gmail OAuth filtrée.

#### Guidage GPS & Veille
- *« Guide-moi vers la pharmacie la plus proche »* $\rightarrow$ Recherche Overpass, carte unique et navigation Valhalla pas-à-pas avec annonces vocales en français.
- *« Surveille cette page et préviens-moi si le prix passe sous les 200 euros »* $\rightarrow$ Veille en tâche de fond persistante.
- *« Active le bouclier anti-distraction pendant 45 minutes »* $\rightarrow$ Blocage CDP des YouTube Shorts et calcul du score de dispersion.
