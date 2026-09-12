**\# ANO-GPT — ce qui manque pour que ce soit vraiment JARVIS**

**Classement par priorité : ce qui change le plus la sensation d'avoir un**

**assistant vivant, en premier. Chaque entrée dit ce que c'est, pourquoi ça**

**compte, et ce que ça coûte sur une machine à 2 cœurs.**

**Le socle est déjà là (STT, VAD, wake word, TTS, ~35 outils, carte, caméra,**

**Gmail, MCP, appli mobile). Ce qui manque n'est presque jamais un outil de plus**

**: c'est de la \**continuité**, de la \**conscience du contexte** et de l'****

**initiative**. Un assistant qui répond bien mais repart de zéro à chaque phrase**

**reste une télécommande vocale.**

**\- - -**

**\## Niveau 0 — le saut qualitatif (à faire en premier)**

**\### 1\\. Mémoire longue durée réelle**

**\**Quoi.** Une base SQLite (FTS5, pas de vecteurs : trop lourd ici) qui retient**

**trois choses distinctes :**

**\- \**profil** : qui il est, ses habitudes, son matériel, ses préférences (« il**

**  déteste qu'on lui lise les mails en entier ») ;**

**\- \**faits datés** : « projet X livré le 12 août », « mot de passe wifi chez Y
»,**

**  « il a rendez-vous jeudi » ;**

**\- **épisodes** : résumé en deux lignes de chaque conversation, horodaté.**

**Avant chaque tour, une recherche FTS5 sur la phrase de l'utilisateur injecte 3**

**à 5 souvenirs pertinents dans le prompt. Après chaque conversation, un résumé**

**est écrit en tâche de fond.**

**\**Impact : maximal.** C'est la différence entre un chatbot et quelqu'un qui te**

**connaît. Tout le reste de cette liste gagne en valeur une fois la mémoire en**

**place.**

**\**Coût CPU : négligeable.** SQLite FTS5, quelques millisecondes. Le résumé de
fin**

**de conversation part au modèle, pas au CPU local.**

**\**Où.** Nouveau \`core/memory_store.py`, branché dans \`core/agent_brain.py`**

**(injection) et à la fin de session dans \`main.py`. Les outils MCP
\`memory_save`**

**/ \`memory_search` existent déjà : les faire pointer dessus.**

**\- - -**

**\### 2\\. Conscience du contexte ambiant**

**\**Quoi.** Une ligne d'état recalculée à chaque tour et collée en tête du
prompt :**

**fenêtre active et titre (\`hyprctl activewindow`), heure, batterie, réseau,**

**musique en cours, dernier fichier ouvert, espace disque, présence du téléphone.**

**\**Pourquoi ça change tout.** Sans ça, il faut tout dire. Avec ça :**

**\- « ferme ça » → il sait ce qu'est *ça* ;**

**\- « c'est quoi cette erreur » → il sait que Kitty est devant ;**

**\- « mets la suite » → il sait ce qui jouait ;**

**\- « je sors » → il sait qu'il est 23 h et qu'il pleut.**

**\**Impact : très élevé.** C'est ce qui donne l'impression qu'il \*regarde*.**

**Deuxième plus gros levier après la mémoire.**

**\**Coût CPU : quasi nul** si on met un cache de 2 secondes sur les sondes et
qu'on**

**ne rappelle \`hyprctl` qu'à chaque tour, jamais en boucle.**

**\**Où.** \`core/context_probe.py`, appelé par \`agent_brain` juste avant
l'envoi.**

**\- - -**

**\### 3\\. Proactivité pilotée par événements**

**\**Quoi.** Un démon léger qui surveille quelques signaux et \**parle sans qu'on
lui**

**demande** :**

**\- batterie < 15 % → « il te reste vingt minutes » ;**

**\- disque > 93 % (déjà à 90 %) → « je peux libérer 4 Go dans les caches » ;**

**\- mail marqué important reçu → une phrase, pas la lecture ;**

**\- rappel arrivé à échéance ;**

**\- build/commande longue terminée dans un terminal ;**

**\- il rentre à la maison (géoloc) → briefing court.**

**Règles indispensables : jamais pendant qu'il parle, jamais pendant un appel ou**

**un plein écran, jamais deux fois le même sujet dans l'heure, et un mode silence.**

**\**Impact : très élevé — c'est le trait JARVIS par excellence.** Un assistant
qui**

**n'ouvre jamais la bouche le premier n'est qu'un outil.**

**\**Coût CPU : faible** si le démon dort et se réveille sur événement
(\`inotify`,**

**D-Bus UPower, IMAP IDLE) plutôt qu'en sondant en boucle.**

**\**Où.** \`actions/proactive.py` existe déjà en germe — le transformer en
service**

**branché sur \`core/ipc.py`, qui pousse une phrase vers le TTS.**

**\- - -**

**\### 4\\. Conversation continue sans répéter le wake word**

**\**Quoi.** Après une réponse, la fenêtre d'écoute reste ouverte 20–30 secondes
et**

**il enchaîne sans « ANO ». Fermeture immédiate si rien n'est dit, ou sur « merci**

**», « c'est bon ».**

**\**Impact : élevé, et immédiatement sensible.** Le wake word à chaque phrase
casse**

**toute illusion de dialogue.**

**\**Coût CPU : modéré** — le VAD Silero tourne plus longtemps. Compensé en
coupant**

**le wake word pendant la fenêtre ouverte.**

**\**Attention.** Le half-duplex reste absolu : pendant qu'il parle, rien ne part
au**

**modèle. La fenêtre d'écoute s'ouvre \*après* la fin de la parole.**

**\- - -**

**\## Niveau 1 — la puissance réelle**

**\### 5\\. Routines et macros vocales (zéro LLM)**

**\**Quoi.** Des enchaînements déterministes déclenchés par une phrase exacte : «**

**mode travail » → VS Code + terminal + Spotify focus + Do Not Disturb +**

**luminosité 70 % ; « mode nuit » → tout fermer, veille, réveil armé ; « je pars**

**» → verrouiller, couper la musique, résumé des tâches restantes.**

**\**Impact : élevé au quotidien.** Et c'est \**instantané** : pas d'aller-retour**

**modèle, la latence tombe à zéro sur les commandes les plus fréquentes.**

**\**Coût CPU : nul.** Un fichier YAML de routines, un matcher par phrase exacte
en**

**amont du LLM.**

**\- - -**

### 6. Briefing quotidien parlé ✅ (Implémenté)

**Quoi.** Au premier « bonjour » de la journée (ou demande explicite) : météo du jour,
repère de localisation, e-mails importants (comptés, pas lus), rappels du jour,
deux titres d'actu tech/cyber, état de la machine. Trente secondes chrono.

**Impact : élevé.** Le rituel indispensable au quotidien, avec affichage d'une
carte récapitulative dans le HUD et enchaînement direct sur l'écoute continue.

**Coût : faible & ultra-rapide.** Collecte multi-sources en parallèle (< 1.5 s)
et restitution vocale en un seul tour direct.

**\- - -**

**\### 8\\. Vision ambiante à la demande, avec cache**

**\**Quoi.** « Regarde », « c'est quoi ce message », « lis-moi ça » → capture via
`**

**capture_control`, OCR local d'abord (rapide, gratuit), image envoyée au modèle**

**seulement si l'OCR ne suffit pas. Cache : si l'écran n'a pas changé, on**

**réutilise l'analyse précédente au lieu de repayer.**

**\**Impact : élevé.** Le débogage à voix haute (« pourquoi ça plante ? ») devient**

**possible sans copier-coller.**

**\**Coût CPU : réel** — c'est le point à surveiller. Une capture par requête**

**maximum, jamais de flux continu.**

**\- - -**

**\### 10\\. Tâches de fond persistantes**

**\**Quoi.** « Surveille cette page et préviens-moi si le prix baisse », « dis-moi**

**quand le build est fini », « rappelle-moi quand j'arrive à la maison ». Une**

**file de tâches durables, survivant au redémarrage, qui déclenchent la**

**proactivité du point 3.**

**\**Impact : élevé.** C'est le passage de « il exécute » à « il veille ».**

**\**Coût : faible** si les tâches sont événementielles et non pollées.**

**\- - -**

**\## Niveau 2 — le raffinement qui impressionne**

**\### 11\\. Reconnaissance du locuteur**

**Empreinte vocale (ECAPA / Resemblyzer, calculée une fois puis comparée en**

**quelques ms) : il sait que c'est bien lui, salue par le prénom, et \**refuse les**

**commandes destructrices d'une voix inconnue**. Impact : moyen sur l'usage, fort**

**sur la sécurité et l'effet « il me reconnaît ». Coût CPU faible en comparaison,**

**modéré au calcul de l'empreinte.**

### 12. Prosodie adaptative ✅ (Implémenté)

**Quoi.** Débit et ton qui suivent le contexte :
- **Urgence** : bref, vif (+18%) et incisif quand c'est urgent ou sur alerte matérielle.
- **Soirée / Nuit (21h-7h)** : voix douce, posée (-12%), feutrée et discrète.
- **Répétition / Confirmation** : réponses ultra-courtes (2 à 5 mots) dès qu'une même question revient.
- **Focus & Dev** : ton technique, sobre et net.

**Impact : moyen à élevé.** C'est ce qui transforme une voix d'automate en véritable compagnon réactif et contextuel.

**Coût : nul.** Ajustement dynamique des métadonnées de prosodie pour Gemini Live et des paramètres acoustiques TTS (vitesse, pitch, volume).

### 13. Auto-réparation ✅ (Implémenté)

**Quoi.** Surveillance de santé continue basée sur `core/tool_stats.py` :
- Détection proactive des outils dégradés ou en échec consécutif (`core/self_healing.py`).
- Rapport vocal et visuel sur la santé du système (`actions/self_repair.py`).
- Diagnostic approfondi avec localisation du fichier source et formulation d'une tâche de patch pour `dev_agent`.

**Impact : moyen à élevé.** Donne au système la capacité d'auto-diagnostic et d'auto-entretien sans intervention manuelle complexe.

**Coût : nul.** Analyse asynchrone à froid sur les métriques déjà journalisées.

**\### 14\\. Index personnel de fichiers**

**Indexation incrémentale de `~/Documents`, `~/development`, `~/Bureau` (nom +**

**extraits texte, FTS5) pour « retrouve le PDF de l'assurance », « ce script où**

**je faisais du ffmpeg ». Impact : moyen-élevé, très concret. Coût : première**

**indexation lourde (à faire de nuit), puis quasi nul via \`inotify`.**

**\### 15\\. Calendrier et contacts**

**CalDAV / Google Calendar en lecture-écriture, plus un carnet de contacts qui**

**alimente \`send_message` et \`email`. Impact : moyen-élevé — sans agenda, le**

**briefing du point 6 est amputé de sa moitié utile.**

**\### 16\\. Latence perçue**

**Trois leviers cumulables : réponse d'accusé immédiate (« je regarde ») pendant**

**que l'outil tourne ; TTS phrase par phrase dès le premier point ; pré-chauffage**

**du modèle sur les intentions fréquentes. Impact : moyen mais permanent — la**

**latence est ce qu'on ressent à chaque interaction. Coût : nul à faible.**

**\- - -**

**\## Ce qu'il ne faut PAS faire ici**

**\- **Écoute vidéo ou audio permanente analysée** : le CPU n'existe pas pour ça,
et**

**  la voix en pâtirait directement (GIL partagé).**

**\- \**Une deuxième carte, ou des animations d'interface** : chaque frame se paie**

**  sur la parole.**

**\- \**Un modèle local de conversation** (7B et plus) : 11 Go et 2 cœurs, la
réponse**

**  arriverait après la question suivante.**

**\- \**Empiler des outils** : 35 outils suffisent largement. Le gain est
maintenant**

**  dans la mémoire, le contexte et l'initiative — pas dans le 36ᵉ.**

**\- - -**

**\## Ordre d'attaque conseillé**

**1\.  Mémoire (1) — tout le reste s'appuie dessus.**

**2\.  Contexte ambiant (2) — deux jours de travail, effet immédiat.**

**3\.  Conversation continue (4) — le plus sensible à l'usage.**

**4\.  Proactivité (3) — une fois 1 et 2 en place, elle devient pertinente au lieu**

**    d'être bruyante.**

**5\.  Routines (5) et briefing (6) — gains quotidiens, coût faible.**

**Le reste se choisit selon l'envie : les points 7 à 16 sont indépendants.**

