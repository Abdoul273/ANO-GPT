# ANO-GPT ultime — audit technique et roadmap de fiabilisation

Date : **13 septembre 2026**. Référence de code : **`30a094f`**. Cible : **Arch Linux / EndeavourOS, Hyprland, Wayland, Google Chrome, 2 cœurs et 11 Go de RAM**.

Ce document répond à une ambition : faire d'ANO-GPT un assistant personnel puissant, rapide et digne de confiance sur cette machine. La priorité est de rendre ses capacités existantes fiables, puis de les relier dans des parcours complets. Ajouter des outils à une exécution fragile multiplie les possibilités de panne.

**Verdict : le projet possède déjà un socle riche, mais il n'est pas encore assez cohérent pour être considéré comme un assistant robuste en usage permanent.** Les défauts les plus urgents concernent l'émission audio différée, les réponses automatiques aux SMS, l'authentification distante, les frontières d'exécution du code et la persistance. Plusieurs problèmes anciens ont été corrigés ; cette roadmap ne les présente pas comme encore ouverts.

L'objectif « plus aucun bug » doit se traduire en critères vérifiables : aucun défaut critique connu, des actions dont le résultat est contrôlé, une reprise après panne testée et des performances mesurées. Aucun audit ponctuel ne peut prouver l'absence de tous les bugs futurs, notamment dans Qt, PipeWire, les SDK et les services externes.

## Navigation

- [1. Périmètre, preuves et résultats](#1-périmètre-preuves-et-résultats)
- [2. Architecture et diagnostic général](#2-architecture-et-diagnostic-général)
- [3. Corrections concrètes C01 à C45](#3-corrections-concrètes)
- [4. Améliorations A01 à A15](#4-améliorations-structurelles-et-de-performance)
- [5. Fonctionnalités F01 à F18](#5-fonctionnalités-pour-un-assistant-personnel-abouti)
- [6. Ordre de livraison et dépendances](#6-ordre-de-livraison-et-dépendances)
- [7. Mesures, tests et critères de sortie](#7-mesures-tests-et-critères-de-sortie)
- [8. Principes de réalisation](#8-principes-de-réalisation)
- [Inventaire des modules](docs/audit-2026-09-13/INVENTAIRE.md)
- [Preuves et protocole de validation](docs/audit-2026-09-13/PREUVES_ET_VALIDATION.md)

## 1. Périmètre, preuves et résultats

### Ce qui a été examiné

Inventaire du dépôt suivi par Git, analyse syntaxique de tous ses fichiers Python, contrôles Ruff, exécution de la suite Python, analyse et tests Flutter, lecture ciblée des chemins critiques et de leurs appelants, revue du serveur distant et du relais Android, lecture des configurations de livraison, agrégation des journaux existants et confrontation avec les roadmaps antérieures.

La profondeur n'est pas identique partout : les chemins cités dans les corrections ont été inspectés directement ; les autres modules ont été inventoriés et soumis aux contrôles applicables. **L'inventaire n'est pas une affirmation de lecture manuelle exhaustive de 158 835 lignes, ni une mesure de couverture d'exécution.** L'annexe rend cette distinction explicite.

Les tests Python ont tourné dans une copie temporaire des fichiers suivis, en mode Qt hors écran. Les reproductions supplémentaires utilisent des sessions, coordonnées, messages, PCM et documents fictifs. Aucune correction du code applicatif, aucun lancement de `main.py`, aucune installation système ni aucun envoi réel de message n'ont été demandés par cet audit. Les journaux historiques ont été synthétisés sans recopier les conversations, clés ou coordonnées personnelles.

Le fichier `config/azure_catalog.json` était déjà modifié au début de l'audit : il a été préservé. Le fichier global annoncé par le dépôt, `/home/anonymous/.Codex/AGENTS.md`, n'existait pas à cet emplacement ; les règles du projet fournies dans la demande ont été appliquées.

### État mesuré

| Contrôle | Résultat | Ce que cela prouve / ne prouve pas |
|---|---|---|
| Fichiers suivis | 578 | Photographie avant ajout de cet audit |
| Python suivi | 442 fichiers, 158 835 lignes | Analyse AST réussie sur ce périmètre |
| Python applicatif, hors tests/scripts/development | 250 fichiers, 126 339 lignes | Taille du socle à maintenir |
| Tests Python | 175 fichiers ; **1 663 passent, 5 échouent, 23 skipped, 1 xfailed** | 1 692 cas collectés ; durée 104,01 s ; pas une recette matérielle |
| Ruff, règles du dépôt | **0 erreur** | La barrière statique est verte |
| Ruff, règles d'audit élargies | 2 526 signalements | Majoritairement style/modernisation ; ce ne sont pas 2 526 bugs |
| Dette d'exceptions muettes selon le test du dépôt | **604**, plafond 584 | Échec reproductible ; ne pas relever le plafond pour faire passer le test |
| Flutter analyze | **Aucun problème signalé** | Analyse Dart ; aucune validation TLS ni Kotlin par ce seul résultat |
| Flutter test | **20 tests passent** | Comportements couverts des composants et de la session |
| Cohérence des paquets de l'interpréteur courant | `pip check` échoue | Environnement global pollué ; pas une preuve que chaque conflit casse ANO |
| Stockage présent, mesure `du` | `memory/` ≈ 340 Mo ; `models/` ≈ 154 Mo | Volume disque ; ce n'est pas la RAM consommée |

Les conflits de l'interpréteur concernent notamment `shazamio 0.6.0` avec `anyio`, `numpy`, `pydantic`, et `silero-vad` sans `torchaudio`. `requirements.txt` avertit déjà de ne pas utiliser ces installations pour ce projet. Il faut isoler l'environnement, pas modifier à l'aveugle les paquets utilisés par d'autres applications. Un conflit `proton-vpn-daemon` relève aussi de l'environnement hôte, pas du dépôt.

### Les cinq échecs Python, correctement interprétés

| Test | Diagnostic | Traitement |
|---|---|---|
| `test_error_visibility.py::test_la_dette_de_silence_ne_grandit_pas` | 604 exceptions muettes pour 584 autorisées | C13 |
| `test_face_memory.py::test_tool_identify_then_remember_from_pending` | Le rapport « inconnu » est réutilisé après mémorisation | **Bug fonctionnel C12** |
| `test_face_memory.py::test_tool_object_path_uses_gemini_search_and_personal_memory` | Le test exige un prix sans demander explicitement de recherche ; l'implémentation réserve celle-ci à une intention de recherche | **Contrat à remettre en cohérence C13**, sans déclencher systématiquement une recherche payante |
| `test_interrupt_scope.py::test_le_budget_de_coupure_audio_reste_sous_100_ms` | `_OUTPUT_SLICE_MS` existe dans `core/audio_engine.py`, mais n'est plus exporté par `main` | Test/import à actualiser ; ne prouve pas une latence réelle > 100 ms |
| `test_voice_selection.py::test_un_changement_de_voix_declenche_la_reconnexion` | `main.save_live_voice` absent ; le code de production le résout aussi dynamiquement | **Bug réel C10**, reproduit directement |

### Ce que disent les journaux, avec leurs limites

`logs/anogpt.jsonl` contenait 1 043 événements au relevé, entre le 9 et le 13 septembre ; 20 rapports de gel datés du 13 septembre étaient présents. Le plus récent inspecté, `freeze-20260913-174553.txt`, annonce un retard de boucle audio de 3,4 s. Un dump est un instantané : il ne démontre pas à lui seul quel thread a causé le gel. Les versions du code ont changé pendant cette période.

`memory/tool_usage.jsonl` contenait 1 240 appels du 13 août au 13 septembre. Ces chiffres servent à orienter la recette, **pas à affirmer que le code actuel échoue encore aux mêmes taux** :

| Outil | Échecs / appels historiques | p95 historique observé | Suite à donner |
|---|---:|---:|---|
| `live_auto_debug` | 29 / 31 | 8,9 s | Distinguer configuration, refus du fournisseur, parsing et vraie erreur de code |
| `calendar_control` | 12 / 12 | 1,4 s | Préflight d'accès + classification des erreurs ; ne pas inventer la cause |
| `email_control` | 8 / 49 | 45,0 s | Budgets OAuth/opérations, reconnexion, statut vérifié |
| `find_nearby` | 5 / 38 | 45,0 s | Position, cache, fournisseur et délai ; une politique plus courte existe désormais |
| `browser_control` | 3 / 9 | 16,8 s | Recette Chrome, focus, pages dynamiques |
| `consult_brain` | 2 / 2 | 100,0 s | Échantillon trop faible pour une estimation fiable ; alerte sur le délai |
| `web_search` | 1 / 83 | 13,9 s | Le budget a déjà été amélioré ; mesurer la version actuelle |

### Améliorations déjà présentes à conserver

| Ancien sujet | État constaté dans le code actuel |
|---|---|
| Embeddings calculés dans la transaction de mémoire conversationnelle | `_prepare_save`, `save_turns` et préparation avant transaction existent |
| Commit par tour | File avec lot de 5 tours ou délai de 2 s déjà en place ; sa gestion d'échec reste à corriger, C24 |
| SQLite de `vector_memory` | `WAL`, `synchronous=NORMAL`, `busy_timeout` sont bien appliqués par connexion |
| WebP de la veille écran | Taille maximale 1 024 px et `method=0` déjà définis |
| Verrou texte recréé à chaque reconnexion | Verrou persistant au constructeur, capture et vérification de session présents |
| Détails des erreurs d'outils | `tool_failure`, journal JSONL, crochets d'exceptions déjà installés |
| Budgets d'actions | `_budget_s` injecté ; politiques dédiées et plusieurs livraisons différées présentes |
| Protection des messages | Cartes de confirmation, sentinelle shell non sérialisable, protections de replay présentes |
| Orbe | Ralentissement pendant la voix et réaction au retard audio déjà présents ; GLSL disponible en option |
| Carte | `core/map_render.py` est la carte unique à préserver |

## 2. Architecture et diagnostic général

### Circulation actuelle

| Ensemble | Rôle | Point de vigilance |
|---|---|---|
| `main.py` | Composition, démarrage, supervision, services | Hôte commun portant beaucoup d'état mutable |
| `core/audio_engine.py` | Capture, lecture, interruption | Pipeline courant et pipeline historique coexistent |
| `core/session_manager.py` | Connexion Live, texte, PCM, tours | Frontière entre file audio, session réseau et état de parole |
| `core/tool_dispatcher.py` | Schémas, dispatch, relais, intégrations | 4 838 lignes ; plusieurs voies d'exécution |
| `core/action_runtime.py`, `action_kit.py`, `thread_pool.py` | Validation, budgets, sous-processus, concurrence | Garanties variables selon la voie d'entrée |
| `core/brain_relay.py`, `llm_client.py`, `agent_brain.py` | Raisonnement et fournisseurs | Contrats, temps d'attente, coûts et retour d'outils |
| `core/memory_store.py`, `vector_memory.py`, `knowledge_graph.py`, `personal_rag.py`, `file_indexer.py` | Mémoire et recherche | Multiples index, cohérence et effacement transversal |
| `ui/` | Qt, orbe, cartes, réglages, médias | Thread principal et audio se partagent le GIL |
| `dashboard/server.py`, `core/phone_relay.py` | Accès distant et téléphone | Authentification, files, perte/rejeu des événements |
| `mobile/ano_remote/` | Flutter + services Android | TLS, permissions, batterie, SIM, interruptions système |
| `core/ipc.py`, `tool_bridge.py`, `anogpt_mcp.py` | Contrôle local depuis les agents | Même politique d'action à garantir que pour la voix |
| Plugins, auto-extension, auto-réparation | Évolution de l'assistant | Exécution de code et contrôle des modifications |

### Direction recommandée

Conserver une application locale compacte. Faire converger toutes les entrées vers un **contrat d'action commun** : origine, identifiant, paramètres validés, autorisation, budget, exécution, preuve du résultat. Découpler ensuite les tâches lourdes de la voix avec un nombre limité de processus spécialisés. La contrainte des deux cœurs ne justifie ni une multitude de services ni un empilement permanent de modèles.

Le meilleur gain ne viendra pas d'un changement arbitraire de modèle : il viendra d'une voix qui reste disponible, d'actions exécutées une seule fois, d'une mémoire exacte, d'une reprise après incident et d'un contrôle précis du système.

### Lire les fiches

- **P0** : exposition ou comportement critique ; à traiter avant de renforcer l'autonomie et l'accès distant.
- **P1** : défaut fonctionnel, perte de données ou faiblesse importante de robustesse.
- **P2** : amélioration de qualité, de performance ou de maintenabilité.
- **P3** : extension avancée après stabilisation.
- **Reproduit** : scénario local effectivement exécuté. **Confirmé par lecture** : mécanisme visible, sans exercice réel de l'action. **Risque** : impact plausible nécessitant une mesure ou une recette complémentaire. **Évolution** : fonction à créer ou à achever, pas un bug existant.
- Efforts indicatifs : **S** ≤ 1 jour ; **M** 2–4 jours ; **L** 5–10 jours ; **XL** plusieurs semaines. Ils comprennent implementation et validation ciblée, restent des estimations et ne doivent pas être additionnés mécaniquement : certains travaux sont partagés.

Toutes les fiches sont **à faire**, sauf les éléments explicitement indiqués comme acquis. Les emplacements et lignes désignent la photographie auditée ; les symboles permettent de les retrouver après évolution du dépôt.

## 3. Corrections concrètes

### C01 — Fermer le garde-fou audio au moment de l'envoi réseau

**P0 · Reproduit · M.** Fichiers : `core/audio_engine.py:925` (`_enqueue_out`), `core/session_manager.py:1142` (`_send_realtime`), `main.py:1759`.

**Quand / cause :** un paquet capturé avant une réponse reste en attente ; `_send_realtime` l'envoie sans vérifier la parole en cours, l'âge ou `_audio_epoch`. La reproduction envoie effectivement un PCM fictif vieux de 30 s, d'époque précédente, avec `_is_speaking=True`. La file de 200 blocs de 64 ms peut représenter environ 12,8 s de son si elle contient seulement du PCM.

**Impact :** violation de l'invariant « rien ne part au modèle pendant qu'ANO parle », commandes périmées, tours parasites et retard après congestion. Cela ne prouve pas qu'une fuite acoustique a eu lieu pendant l'audit.

**Faire :** conserver le verrou dans le callback, ajouter une validation finale au consommateur, dater le paquet à la capture plutôt qu'après attente dans `call_soon_threadsafe`, rejeter les anciennes époques et sessions, purger les PCM invalides. Distinguer média et contrôle. **Gain :** half-duplex garanti sur tout le trajet. **Acceptation :** zéro paquet envoyé pendant la parole, après changement d'époque ou après expiration ; tests avec transport volontairement ralenti.

### C02 — Éliminer le blocage de la réponse automatique SMS

**P0 · Reproduit · S/M.** `core/tool_dispatcher.py:2449`, `_send_phone_sms`, appel `_send()` à la ligne 2486.

**Quand / cause :** `auto_reply_authorized=True` appelle depuis une coroutine une fonction qui utilise `run_coroutine_threadsafe` sur la même boucle, puis `future.result(timeout=40)`. La boucle attend son propre travail. La reproduction réduit seulement cette attente à 80 ms : aucun envoi simulé ne démarre avant le timeout, puis il démarre après.

**Impact :** gel de la boucle voix/dashboard jusqu'à 40 s ; erreur annoncée puis SMS susceptible de partir tardivement ; répétition pouvant créer un doublon.

**Faire :** voie asynchrone avec `await request_phone_sms(...)` ; réserver l'adaptation synchronisée au véritable thread de confirmation. Ajouter délai absolu, identifiant durable et traitement du résultat incertain. **Gain :** commande non bloquante et résultat fiable. **Acceptation :** le heartbeat continue pendant une réponse lente ; aucun envoi tardif non suivi après timeout. Dépendance : C03/C17 pour l'autorisation et le replay.

### C03 — Retirer au modèle le pouvoir de s'autoriser un SMS

**P0 · Confirmé par lecture et chemin reproduit · M.** `core/tool_dispatcher.py:1008` et `:2461`, `core/human_confirmation.py`, `dashboard/server.py:1600` environ.

**Quand / cause :** le booléen `auto_reply_authorized` est un argument public de l'outil et suffit à contourner la carte. Le commentaire exige un accord verbal, mais aucune preuve d'autorisation liée au SMS n'est vérifiée côté exécution.

**Impact :** une hallucination ou une instruction hostile dans un contenu reçu peut provoquer une demande d'envoi sans autorisation vérifiable. La qualité du prompt ne constitue pas une barrière d'exécution.

**Faire :** garder le mode verbal souhaité, avec une autorisation détenue par l'application, liée à l'événement entrant, au destinataire, à une durée et à un usage unique. Le modèle peut demander ce mode, jamais fabriquer le droit. **Gain :** autonomie pratique et consentement réel. **Acceptation :** `true` seul échoue ; un accord valable fonctionne une fois ; accord expiré, autre destinataire et SMS injectant des instructions sont refusés.

### C04 — Authentifier la position et la navigation

**P0 · Reproduit · S.** `dashboard/server.py:1360` (`/api/live_position`) et `:1369` (`/api/navigation/state`).

**Quand / cause :** ces deux routes n'appellent pas `_auth`, contrairement aux commandes. Les requêtes sans session rendent HTTP 200 ; une position fictive ressort intégralement. Le serveur est configuré sur `0.0.0.0`.

**Impact :** une personne pouvant joindre le service peut lire la position ou l'état de trajet disponibles. L'exposition effective dépend du réseau et du pare-feu.

**Faire :** dépendance d'authentification commune, droit distinct de localisation, contrôle identique pour HTTP et WebSocket. **Gain :** les données personnelles restent réservées aux appareils autorisés. **Acceptation :** 401 sans session, 403 sans droit, données seulement avec droit valide ; test systématique de toutes les routes privées.

### C05 — Rendre la révocation immédiate et complète

**P0 · Reproduit · M.** `dashboard/server.py:1290–1323` environ : `device_login_ep`, `revoke_devices`, `_tokens`, `_token_keys`, `_clients`.

**Quand / cause :** la révocation efface `_device_sessions`, mais les jetons d'accès et les sockets déjà connectées restent actifs. Un jeton fictif continue à obtenir HTTP 200 sur `/api/command` après révocation.

**Impact :** un appareil perdu ou révoqué conserve le contrôle jusqu'au redémarrage ou à la fermeture de sa session ; le bouton donne un faux sentiment de retrait d'accès.

**Faire :** relier appareil → sessions → sockets, invalider les jetons concernés, fermer les canaux voix/caméra/contrôle, vérifier les droits des connexions longues et prévoir expiration/rotation. **Gain :** reprise effective du contrôle. **Acceptation :** ancienne session refusée immédiatement sur tous les canaux, y compris une socket ouverte avant révocation.

### C06 — Vérifier l'identité TLS du PC sur Android

**P0 · Mécanisme confirmé ; attaque non réalisée · L.** `mobile/ano_remote/lib/net.dart:45`, `PhoneRelayService.kt:283`, `AndroidManifest.xml`, `dashboard/server.py:480` et `:1733`.

**Quand / cause :** Flutter accepte tout certificat pour certains hôtes LAN ; le service Kotlin accepte tous les certificats et tous les noms d'hôte. Des replis HTTP existent. Un réseau local n'est pas une preuve d'identité du serveur.

**Impact :** risque d'interception des jetons, messages et commandes sur un réseau hostile ; découverte UDP usurpable si elle devient une preuve de confiance.

**Faire :** QR liant identité du PC, clé publique/empreinte et challenge d'appairage ; ancrage persistant côté clients, renouvellement explicite, HTTPS obligatoire après appairage, protection Keystore des secrets. La découverte ne sert qu'à trouver une adresse. **Gain :** reconnexion sûre même sur Wi-Fi partagé. **Acceptation :** certificat d'un autre PC rejeté ; rotation légitime testée ; absence de downgrade silencieux. Les exigences de session sur connexion longue sont détaillées par [OWASP](https://cheatsheetseries.owasp.org/cheatsheets/WebSocket_Security_Cheat_Sheet.html).

### C07 — Remplacer le faux bac à sable Python du bureau

**P0 pour du code généré non contrôlé · Confirmé par lecture · L.** `actions/desktop.py:630` (`_build_sandbox`) et `:680` (`_execute_generated_code`).

**Quand / cause :** du code produit par le modèle passe dans `exec` au sein du processus applicatif. Réduire `__builtins__` n'isole pas le processus ; `Path` complet, des modules et des objets Python restent accessibles. Le commentaire interdisant la suppression ne retire pas les méthodes de `Path`.

**Impact :** modification de fichiers hors intention, lecture de données sensibles, blocage CPU ou corruption de l'assistant ; pas besoin d'un accès root pour endommager les données utilisateur.

**Faire :** préférer des opérations bureau typées ; placer les rares calculs générés dans un worker isolé avec fichiers explicitement montés, réseau limité, budget CPU/RAM/temps et protocole de résultat. **Gain :** puissance sans exécution arbitraire dans la voix. **Acceptation :** lecture hors périmètre, écriture non autorisée et boucle infinie contenues sans figer Qt/audio.

### C08 — Empêcher l'exécution d'un plugin avant son activation

**P1 · Confirmé par lecture · L.** `core/plugin_registry.py:48`, `:97`, `core/auto_extension.py:227–258`.

**Quand / cause :** `discover()` importe chaque candidat via `exec_module` avant de consulter son état activé. Le niveau supérieur du module s'exécute donc même pour un plugin désactivé. Les permissions sont explicitement déclaratives. L'auto-extension exécute également les tests générés avant l'activation, dans un sous-processus non isolé du compte utilisateur.

**Impact :** « désactivé » ne signifie pas « n'exécute rien » ; tests ou imports générés peuvent toucher le système avant décision.

**Faire :** manifeste lu sans import, validation statique, état désactivé par défaut pour tout nouveau hash, tests et exécution dans un worker à capacités. Réserver les plugins historiques de confiance à un mode clairement identifié. **Gain :** extensions inspectables et révocables. **Acceptation :** aucun effet de bord à la découverte ; changement de source invalide la confiance ; essais hostiles confinés. Dépendance : C07/A03.

### C09 — Ne plus déclarer une auto-réparation réussie parce que le dépôt est sale

**P1 · Confirmé par lecture · M/L.** `core/auto_fix.py:125` (`_changed_files`) et `:159` (`repair`).

**Quand / cause :** les fichiers déjà modifiés sont inclus dans `_changed_files`. La présence de n'importe quels fichiers suffit à `ok=True`, même après un code de sortie non nul. Le prompt demande aussi commit/push sans preuve enregistrée de tests réussis.

**Impact :** incident marqué corrigé sans correction effective, modifications utilisateur attribuées à l'agent, publication possible d'un résultat non validé.

**Faire :** état initial et empreintes, travail isolé, diff strictement attribuable à la réparation, tests dont les résultats sont lus, verdict structuré, publication selon les autorisations existantes du projet. **Gain :** « c'est corrigé » devient vérifiable. **Acceptation :** dépôt sale + agent en échec ne réussit jamais ; aucun fichier préexistant embarqué par erreur ; validation échouée empêche `mark_fixed`.

### C10 — Réparer réellement le changement de voix

**P1 · Reproduit · S.** `core/session_manager.py:69`, `:273`, `core/audio_engine.py:79`, `main.py`, `memory/config_manager.py`.

**Quand / cause :** sélectionner une autre voix appelle `_MainAttr("save_live_voice")`, qui cherche `main.save_live_voice`. Ce symbole n'existe plus. Un appel direct avec un hôte factice et une autre voix lève `AttributeError` avant la sauvegarde.

**Impact :** réglage non appliqué, reconnexion non déclenchée, état affiché potentiellement incohérent.

**Faire :** importer explicitement la fonction de configuration au bon endroit ou l'injecter ; mettre le test sur le contrat public et tester sauvegarde puis reconnexion. **Gain :** sélection vocale fiable. **Acceptation :** voix modifiée, persistée, appliquée après reconnexion ; une erreur de sauvegarde laisse le choix précédent intact.

### C11 — Rebrancher les sous-titres sur le pipeline micro réellement actif

**P1 · Confirmé par lecture croisée · M.** `core/audio_engine.py:1702`, `core/session_manager.py:1142`, `core/live_captions.py:126`, `tests/test_live_captions.py`.

**Quand / cause :** le callback PC Mark-LII émet du PCM sans marqueurs `start/end`. Le moteur de sous-titres n'ouvre pourtant son flux qu'après `activity=start` et ignore le PCM tant que `_turn` est absent. Les tests injectent eux-mêmes ces marqueurs et ne vérifient pas le raccordement au callback courant.

**Impact :** une fonction annoncée « instantanée » peut rester muette sur la voie PC, tout en préchauffant un service inutile. La voie téléphone émet des marqueurs et ne doit pas être confondue avec ce cas.

**Faire :** un contrat explicite entre capture et sous-titrage, avec segmentation locale légère hors callback ou flux supportant le PCM continu ; garder la transcription conversationnelle comme autorité. **Gain :** aperçu réellement progressif, coût justifié. **Acceptation :** test de bout en bout avec les vrais messages du callback PC, silence, interruption, changement de session et fermeture.

### C12 — Invalider le cache de reconnaissance après apprentissage ou changement de scène

**P1 · Reproduit par la suite · M.** `actions/visual_recognition.py:456`, `:538`, `_last_identify`, `_remember_person`, `_forget`, `_update`.

**Quand / cause :** la clé de cache repose sur source/intention/question pendant une fenêtre de temps, sans version de mémoire ni empreinte d'image. Après « c'est Karim », la même question récupère encore le rapport « inconnu ». Un changement de personne ou d'objet pendant la fenêtre crée le même risque.

**Impact :** reconnaissance perçue comme défaillante, affirmation fausse sur ce qui est devant la caméra.

**Faire :** invalider après mémorisation/oubli/renommage ; lier la réponse à une image et à une version de mémoire, distinguer rappel de résultat et nouvelle observation, isoler le cache par session/source. **Gain :** apprentissage visible immédiatement. **Acceptation :** le test actuel passe pour la bonne raison ; personne différente et nouvelle image déclenchent une nouvelle analyse.

### C13 — Restaurer une suite verte sans masquer les défauts

**P1 · Reproduit · M.** `tests/test_error_visibility.py:59`, `tests/test_face_memory.py:212`, `tests/test_interrupt_scope.py:192`, `tests/test_voice_selection.py:17`.

**Quand / cause :** dérive du code et des tests, extraction de symboles, contrats devenus différents ; 20 handlers muets au-delà du plafond.

**Impact :** nouvelles régressions noyées dans les échecs connus ; fausses conclusions sur les performances.

**Faire :** corriger C10/C12, importer les constantes depuis leur module propriétaire, expliciter la demande de recherche de prix dans le test objet et tester aussi l'absence de recherche automatique ; traiter les handlers importants individuellement. **Gain :** filet de régression crédible. **Acceptation :** zéro échec inattendu, aucun `skip/xfail` ajouté pour cacher ces cinq cas, plafond de silence inchangé ou réduit.

### C14 — Contrôler aussi les actions exécutées depuis MCP

**P1 · Confirmé par lecture · L.** `main.py:1435` environ, `core/tool_bridge.py:111`, `core/tool_dispatcher.py:4120`, `anogpt_mcp.py`.

**Quand / cause :** la table `_agent_tools` et `dispatch` appellent directement des fonctions synchrones, tandis que la voix passe par `_execute_tool`, validation, budgets et statistiques. Certaines actions ont leurs protections propres ; la garantie n'est pas uniforme.

**Impact :** mêmes demandes avec politiques différentes selon l'entrée, opérations longues après timeout client, diagnostics incomplets.

**Faire :** moteur d'action commun indépendant de Gemini/Qt, identifiant et origine `voice/mcp/remote/routine`, adapters de transport minces. Ne pas supposer qu'un accès local vaut autorisation universelle. **Gain :** comportement prévisible et protections uniques. **Acceptation :** même action refusée/validée de la même façon par les quatre entrées ; MCP peut suivre ou annuler un travail long.

### C15 — Faire correspondre timeout, annulation et effet réel

**P1 · Risque architectural confirmé · L.** `core/tool_dispatcher.py:2520`, `core/action_runtime.py`, `core/action_kit.py`, `core/thread_pool.py`, `core/tool_bridge.py:41`.

**Quand / cause :** un timeout asyncio arrête l'attente, mais ne tue pas un thread déjà lancé ; le client MCP attend 45 s par défaut alors que certaines actions peuvent durer davantage. Des nettoyages de groupes de processus existent déjà dans `action_kit` : les généraliser au contrat d'action.

**Impact :** « annulé » suivi d'une écriture ou d'un envoi tardif ; worker occupé, répétition dangereuse d'une opération dont le résultat est inconnu.

**Faire :** deadline propagée à chaque couche, jeton d'annulation coopératif, arrêt des descendants pour les jobs isolés, état `résultat_inconnu` pour une mutation déjà transmise, réconciliation avant retry. **Gain :** délais utiles et actions maîtrisées. **Acceptation :** transport bloqué, worker bloqué et retour tardif testés séparément ; jamais de retry aveugle d'une mutation. [Référence asyncio](https://docs.python.org/3/library/asyncio-task.html#timeouts).

### C16 — Accuser réception d'un SMS seulement après prise en charge durable

**P1 · Confirmé par lecture · M/L.** `dashboard/server.py:1599–1635`, `SmsReceiver.kt:24–43`, `PhoneRelayService.kt` (`removeSms`, `flushSms`).

**Quand / cause :** le PC envoie `phone_sms_received_ack` avant la mise en file de l'annonce, ignore le retour de `_enqueue_command`, et ne déduplique qu'en RAM. Le téléphone retire alors l'événement. Sa file tronque aussi les éléments au-delà de 50.

**Impact :** SMS perdu pour l'assistant si file pleine/crash au mauvais instant, annonce dupliquée après redémarrage, perte silencieuse en longue déconnexion.

**Faire :** inbox durable avec `event_id` unique ; commit avant ACK ; annonce traitée séparément avec statut ; suppression côté mobile après ACK durable ; politique visible de rétention. **Gain :** événements fiables en Wi-Fi instable. **Acceptation :** coupure à chaque étape, file pleine et redémarrage ne perdent ni ne doublent l'annonce.

### C17 — Ne pas annoncer « SMS envoyé » sans résultat Android et anti-rejeu

**P1 · Confirmé par lecture · L.** `PhoneRelayService.kt:241`, `MainActivity.kt:325`, `dashboard/server.py:939`.

**Quand / cause :** `sendTextMessage(..., null, null)` est suivi immédiatement de `status=sent`. Aucun résultat d'envoi ou de livraison n'est suivi par ces callbacks ; le `request_id` n'est pas un registre durable des ordres déjà exécutés. Le texte peut atteindre 1 600 caractères sans chemin multipart explicite.

**Impact :** succès annoncé sans service opérateur, doublon si l'accusé se perd, échecs des messages longs ou des appareils double SIM.

**Faire :** états distincts `soumis/envoi_confirmé/livraison_confirmée/échec/inconnu`, callbacks Android, sélection de SIM, découpage multipart, idempotence persistante côté téléphone. **Gain :** compte rendu honnête et absence de répétition involontaire. **Acceptation :** mode avion, refus SIM, long SMS, reconnect/replay et accusé perdu. [Contrat SmsManager](https://developer.android.com/reference/android/telephony/SmsManager).

### C18 — Valider tous les corps d'authentification

**P1 · Reproduit · S/M.** `dashboard/server.py:1195`, `:1212`, `:1278` : `/login`, `/api/pair`, `/api/device-login`.

**Quand / cause :** les handlers supposent un dictionnaire et des chaînes après `req.json()`. `[]` et `42` produisent HTTP 500 ; `device_token=42` aussi sur la reconnexion.

**Impact :** clients incompatibles mal diagnostiqués, erreurs serveur faciles à déclencher, bruit de supervision.

**Faire :** schémas d'entrée stricts, limites de taille avant parsing, codes 400/422 cohérents, tests des types inattendus et données tronquées. Ajouter limitation d'essais d'appairage, par origine et globalement, sans bloquer tout le réseau sur une seule erreur. **Gain :** protocole robuste et appairage plus résistant. **Acceptation :** aucun 500 pour un payload malformé, ni volume illimité de sessions.

### C19 — Borner les flux distants et sécuriser les fichiers reçus

**P1 · Risques confirmés par lecture · L.** `dashboard/server.py:1406`, `:1446`, `:1491`, `:1550`, `:1566`.

**Quand / cause :** validation métier limitée des tailles/fréquences PCM/JPEG ; quotas de fichiers absents ; limite d'upload vérifiée après réception multipart ; choix de nom `exists()` puis `open("wb")` non exclusif ; jetons utilisés dans les URL.

**Impact :** pression RAM/disque/CPU, concurrence sur un même nom, fuite possible de jetons via historique/journaux, un appareil saturant les autres. Les limites par défaut d'Uvicorn ne remplacent pas les limites adaptées aux médias.

**Faire :** quotas par appareil, limites de trame et fréquence, validation des dimensions avant décodage, écriture temporaire exclusive puis renommage, contrôle de chemin résolu, nettoyage d'échec/cancellation, tickets de téléchargement courts. **Gain :** téléphone utilisable longtemps sans saturation. **Acceptation :** uploads concurrents, trames excessives, fermeture au milieu d'un upload et fichier hostile restent contenus.

### C20 — Revalider les paramètres récupérés depuis un champ inconnu

**P1 · Confirmé par lecture · S/M.** `core/action_runtime.py:370` (`prepare`).

**Quand / cause :** si un seul texte obligatoire manque et qu'un seul champ inconnu contient du texte, celui-ci est injecté directement dans le champ manquant après la validation normale. Ses contraintes de longueur/enum ne sont plus appliquées, et l'équivalence sémantique est supposée.

**Impact :** argument trop long ou valeur interdite accepté ; pour une action sensible, un texte de commentaire peut devenir destinataire ou commande.

**Faire :** privilégier des aliases nommés, interdire l'inférence pour cible/commande/autorisation, repasser toute récupération par `_coerce_value` et la validation du schéma. **Gain :** tolérance aux variations du modèle sans affaiblir le contrat. **Acceptation :** limites respectées après alias ; champ inconnu ambigu refusé avec message exploitable.

### C21 — Retirer de l'index le contenu d'un document devenu vide

**P1 · Reproduit avec extraction vide simulée · S/M.** `core/personal_rag.py:1430`, retour anticipé `if not new_chunks`.

**Quand / cause :** un fichier déjà indexé ne produit plus de fragments. Le code retourne avant suppression des anciens fragments et mise à jour du fichier. La sonde conserve effectivement un fragment après une extraction vide.

**Impact :** l'assistant cite un contenu supprimé, avec un risque de confidentialité et de décision incorrecte.

**Faire :** distinguer extraction réussie vide, format devenu non pris en charge et erreur d'extraction. Le vide confirmé purge la version précédente ; une erreur conserve une version explicitement périmée sans la présenter comme actuelle. **Gain :** recherche conforme au disque. **Acceptation :** tests fichier vidé, supprimé, illisible, remplacé et rendu trop gros ; aucune ancienne donnée présentée comme fraîche.

### C22 — Rendre atomique la mise à jour d'un document et actualiser ses références

**P1 · Confirmé par lecture · L.** `core/personal_rag.py:1207`, `:1233`, `:1284`, `:1475`, `CodeChunk.compute_hash`.

**Quand / cause :** suppression, insertion de chaque fragment et métadonnées utilisent plusieurs transactions. Un arrêt intermédiaire laisse une version partielle. Les fragments réutilisés par hash ne mettent pas à jour leurs lignes et leur position, alors que le hash ne contient pas celles-ci.

**Impact :** références de lignes périmées après déplacement de code, résultats manquants après crash, incohérence entre FTS et vecteurs.

**Faire :** préparer les fragments/vecteurs hors transaction puis remplacer la génération du document atomiquement ; actualiser les métadonnées des fragments réutilisés ; vérifier les index secondaires. **Gain :** citations fiables et moins de commits. **Acceptation :** interruption à chaque étape ne rend visible que l'ancienne ou la nouvelle version complète ; déplacement sans changement de contenu actualise les lignes.

### C23 — Appliquer les paramètres SQLite à chaque connexion et fermer explicitement

**P1 · Reproduit pour `synchronous` ; cycle de vie confirmé par lecture · M.** `core/personal_rag.py:1106`, `core/file_indexer.py:77`, `core/vector_memory.py:675`.

**Quand / cause :** dans les deux premiers modules, `synchronous=NORMAL` est posé uniquement à l'initialisation. Les connexions suivantes reviennent à `2` (`FULL`) : mesuré dans les bases fictives. Beaucoup de `with sqlite3.connect(...)`/`with conn` gèrent la transaction, sans fermer explicitement la connexion ; la fermeture dépend alors du cycle de vie Python.

**Impact :** fsync plus fréquents que prévu, durée et ressources moins prévisibles. Le module conversationnel applique déjà les pragmas par connexion : ne pas le corriger une deuxième fois.

**Faire :** fabrique de connexions, paramètres par usage, `closing` ou fermeture garantie, désactivation du chargement d'extensions après chargement ; transactions courtes. **Gain :** latence disque plus régulière. **Acceptation :** vérification des pragmas de toute connexion, nombre de descripteurs stable et reprise après exception. Choisir `NORMAL` pour les index reconstruisibles ; évaluer séparément la durabilité des données irremplaçables.

### C24 — Ne pas perdre un lot de souvenirs sur une erreur d'écriture

**P1 · Confirmé par lecture · M/L.** `core/vector_memory.py:1051–1122`, `_flush_queued_turns`.

**Quand / cause :** le lot est retiré de `_turn_queue` avant sauvegarde ; une exception est journalisée sans remise en file. L'écriture du graphe secondaire partage le même `try`. Une soumission refusée attend une future conversation pour réessayer ; l'arrêt peut arriver avant.

**Impact :** souvenirs perdus après erreur transitoire, graphe et conversation désynchronisés, perte de la fin de session.

**Faire :** journal durable minimal des tours, ACK après commit, retries bornés idempotents, traitement indépendant du graphe, flush d'arrêt borné avec reprise au prochain démarrage. **Gain :** mémoire fiable malgré panne disque ou interruption. **Acceptation :** erreur au premier commit puis reprise conserve le lot exactement une fois ; crash avec lot en attente récupéré.

### C25 — Faire de « oublie » un effacement cohérent dans toutes les mémoires

**P1 · Défaut local confirmé + chantier transversal · L.** `core/vector_memory.py:1470`, tables `vec_triples`, `rdf_triples`, `core/memory_store.py`, `core/knowledge_graph.py`, `core/memory_episode.py`, `core/face_memory.py`.

**Quand / cause :** `forget` supprime l'entrée et ses triplets relationnels, mais pas explicitement leurs lignes `vec_triples`. Les mémoires conversationnelles, graphes, photos, caches et sauvegardes possèdent aussi des cycles distincts.

**Impact :** vecteurs orphelins, données oubliées restant accessibles ailleurs, promesse d'effacement trop large par rapport à l'effet réel.

**Faire :** identifiants de provenance, suppression transactionnelle des index liés, propagation de l'effacement, invalidation des caches, politique explicite de rétention des sauvegardes. **Gain :** contrôle réel de la mémoire et index cohérents. **Acceptation :** contrôle des comptes d'orphelins et recherche transversale après oubli ; restitution précise de ce qui a été effacé et de ce qui reste en sauvegarde.

### C26 — Corriger la couverture et les exclusions de l'indexation personnelle

**P1 · Confirmé par lecture · M/L.** `core/file_indexer.py:259`, `DEFAULT_INDEX_ROOTS`, `core/personal_rag.py:99`, `:174`, `:1430`.

**Quand / cause :** le scan FTS repart du début et s'arrête à `max_files=2000`, sans curseur dans cette méthode ; une grande première racine peut empêcher la visite des suivantes. Les exclusions divergent entre les deux indexeurs ; des fichiers de configuration non cachés peuvent contenir des secrets. Les liens symboliques nécessitent une règle de périmètre explicite.

**Impact :** documents jamais trouvés, travail disque répété, duplication d'informations sensibles dans les index et les contextes.

**Faire :** curseur équitable par racine, watcher avec rapprochement périodique, catalogue commun de dossiers autorisés/exclus, contrôle du chemin résolu et filtre de secrets avant mémorisation/export. **Gain :** recherche complète et privée. **Acceptation :** corpus > 2 000 fichiers réparti sur plusieurs racines, symlink hors périmètre et fichier secret fictif ; progression visible et aucune fuite au contexte.

### C27 — Unifier les écritures de configuration et les secrets

**P1 · Risque confirmé par structure · M/L.** `memory/config_manager.py:162`, `:195`, `:338`, lecteurs JSON dans `core/llm_client.py`, `core/github_backup.py`, actions et scripts OAuth.

**Quand / cause :** l'écriture atomique et la fusion sous verrou existent, mais le verrou ne protège que les threads d'un processus. Plusieurs agents/processus et lecteurs directs peuvent utiliser des états incompatibles. Le keyring est optionnel ; les clés avancées ne sont pas toutes décrites par la même dataclass.

**Impact :** réglage écrasé par une autre écriture, clé modifiée sans effet partout, comportement opaque après changement de fournisseur.

**Faire :** service unique de configuration typée/versionnée, mises à jour partielles avec version attendue ou verrou interprocessus, références de secrets au lieu de valeurs dispersées, rechargement explicite des consommateurs. **Gain :** réglages cohérents et migration sûre. **Acceptation :** deux écrivains concurrents préservent leurs changements ; panne du coffre n'efface aucune clé ; aucune valeur secrète dans les diagnostics.

### C28 — Borner et expurger les journaux de bout en bout

**P1/P2 · Confirmé par lecture · M.** `core/observability.py:61`, `:127`, `:212`, `core/incident_log.py`, `actions/shell_exec.py`, `core/phone_relay.py:483`.

**Quand / cause :** le formatter recopie messages, exceptions et extras sans filtre universel ; la file de logs est volontairement non bornée. Certains chemins impriment directement texte de commandes ou messages. La limitation aux noms d'arguments dans `tool_failure` est un acquis utile, mais ne couvre pas une clé dans une URL d'exception.

**Impact :** données personnelles dans journaux/rapports envoyés aux agents, RAM croissante pendant une tempête de logs ou un disque lent.

**Faire :** expurgation centrale des secrets et données à risque, diagnostic exportable avec aperçu, file bornée avec compteurs de perte et priorité aux erreurs, agrégation des répétitions, quotas des dumps de gel. **Gain :** diagnostic utile sans stockage incontrôlé. **Acceptation :** secrets fictifs injectés dans URL/exceptions/extras tous masqués ; logs sous panne disque ne ralentissent pas la voix.

### C29 — Rendre l'installation et l'environnement Python reproductibles

**P1 · Constat local et dépôt · L.** `requirements.txt`, `setup.py`, `pyproject.toml`, `.github/workflows/quality.yml`, `config/systemd/anogpt.service`.

**Quand / cause :** majorité des dépendances non bornées ; deux SDK Google listés ; environnement d'exécution global contenant les conflits mesurés. Le service démarre `python3` via PATH. `core/libdf.so` est un binaire versionné dont la construction n'est pas reproduite par un pipeline dédié visible.

**Impact :** « fonctionne ici » mais casse après mise à jour, ABI native incompatible, dépendance optionnelle contaminant le démarrage.

**Faire :** environnement applicatif isolé, lock de versions validées et dépendances optionnelles par capacité, tests Python/natifs sur la cible réellement utilisée, service pointant sur l'interpréteur choisi, provenance/hash et procédure de reconstruction du binaire. **Gain :** installation réparable et retours arrière possibles. **Acceptation :** installation propre depuis le lock, `pip check` vert dans cet environnement, démarrage sans modèles optionnels ; aucune modification aveugle du Python système.

### C30 — Supprimer la traduction dangereuse vers une synchronisation Arch partielle

**P1 · Confirmé par lecture · S/M.** `actions/shell_exec.py:492`, `adapt_command_for_arch`.

**Quand / cause :** la traduction d'une commande étrangère de rafraîchissement produit `pacman -Sy`. Une installation ultérieure peut alors créer une mise à jour partielle. Remplacer les noms de gestionnaires par regex ne traduit pas correctement les intentions et les noms de paquets.

**Impact :** incohérence des paquets et bibliothèques de l'hôte, pouvant casser l'assistant et d'autres applications.

**Faire :** actions Arch natives : consultation avec `checkupdates` si disponible, mise à niveau complète explicitement demandée avec `sudo pacman -Syu`, installation adaptée à l'état du système et aux règles Arch. Refuser les formulations étrangères ambiguës au lieu de les réécrire silencieusement. **Gain :** maintenance compatible avec EndeavourOS. **Acceptation :** aucun chemin générant une synchronisation partielle ; tests de parsing uniquement avant recette système. [Référence ArchWiki](https://wiki.archlinux.org/title/System_maintenance#Partial_upgrades_are_unsupported).

### C31 — Appliquer la politique Chrome au scraper TikTok

**P1 · Confirmé par lecture · S/M.** `actions/tiktok_tracker.py:111` et `:215`, `core/browser_policy.py`, `setup.py`.

**Quand / cause :** le tracker essaie Chromium, Brave et Edge en repli, puis le Chromium embarqué ; il transmet `--no-sandbox`. L'installation Playwright demande également tous ses navigateurs. Cela contourne la politique Chrome du projet.

**Impact :** navigateur inattendu, téléchargement inutile, comportement différent des sessions utilisateur et protections navigateur réduites.

**Faire :** utiliser `chrome_binary`, refuser clairement si Chrome manque, retirer la désactivation du sandbox sauf justification technique testée dans un worker réellement isolé ; installer seulement ce qui sert au chemin retenu. **Gain :** comportement unique, moins de dépendances et meilleure isolation. **Acceptation :** Chrome absent n'ouvre aucun autre navigateur ; tests de découverte et lancement sans réseau.

### C32 — Compléter les garanties de l'agenda avant de s'y fier

**P1 · Limites confirmées ; pannes historiques non attribuées · L.** `core/calendar_service.py:205`, `:269`, `:280`, `:362`, `:469`, `actions/calendar.py:164`, `core/calendar_watcher.py`.

**Quand / cause :** la liste Google ne parcourt pas `nextPageToken` ; les conflits sont limités à 100 résultats ; le parsing ICS est artisanal et ne constitue pas une expansion complète des récurrences/fuseaux. L'authentification interactive agenda peut durer 180 s sans politique dédiée équivalente au mail dans le dispatcher.

**Impact :** conflit manqué dans un agenda dense, créneau ou rappel erroné pour une occurrence, OAuth arrêté trop tôt. Les 12/12 échecs historiques exigent leur propre diagnostic de connexion.

**Faire :** pagination des opérations exhaustives, récurrences/fuseaux/événements journée entière via un contrat éprouvé, contrôle des modifications concurrentes, OAuth en tâche dédiée, confirmations adaptées aux invitations. **Gain :** agenda fiable pour de vrais engagements. **Acceptation :** > 100 événements, changement de fuseau/heure saisonnière, récurrence avec exception et coupure OAuth.

### C33 — Ne pas transformer toute fin de callback confirmé en succès

**P1 · Confirmé par lecture · M.** `core/human_confirmation.py:154–177`, `_fingerprint`, `_recent`.

**Quand / cause :** la demande entre dans `_recent` avant exécution. Toute chaîne rendue par le callback est ensuite annoncée « Action terminée, exécutée », y compris une chaîne d'erreur. Le fingerprint utilise les textes d'affichage tronqués, pas une identité canonique de l'opération.

**Impact :** action ratée annoncée réussie, retry légitime bloqué pendant 120 s, collisions de demandes longues ; threads de confirmation pouvant survivre au contexte de session.

**Faire :** résultat typé, état `en_attente/en_cours/réussi/échoué/incertain`, identifiant calculé sur les paramètres complets, protection contre replay selon le statut ; exécution dans un gestionnaire de jobs. **Gain :** confirmations compréhensibles et exactes. **Acceptation :** retour d'erreur, exception, timeout, double clic et deux longues demandes proches produisent les bons états.

### C34 — Conserver les contraintes des schémas entre fournisseurs

**P1/P2 · Confirmé par lecture · M.** `core/brain_relay.py:69` (`_json_schema`), `core/tool_dispatcher.py`, `core/tool_registry.py`, `core/tool_packs.py`, `anogpt_mcp.py`.

**Quand / cause :** la conversion conserve type, description, enum, propriétés, items et required, mais perd notamment min/max, longueurs et nullable. Les descriptions et contrats sont répliqués entre plusieurs tables/adapters.

**Impact :** le cerveau produit plus d'arguments invalides ou ignore des limites ; divergences entre documentation, capacités annoncées et exécution.

**Faire :** schéma canonique versionné, génération des déclarations pour chaque fournisseur/MCP, tests de conservation des contraintes et compatibilité de chaque adapter. **Gain :** moins de rattrapages et d'appels inutiles. **Acceptation :** round-trip de schémas représentatifs sans perte des contraintes supportées ; refus explicite de celles qu'un fournisseur ne sait pas exprimer.

### C35 — Ne plus déduire le succès d'une phrase de réponse

**P1 · Limite confirmée · L.** `core/action_runtime.py` (`looks_failed`, préfixes d'erreur), `core/tool_dispatcher.py:2600`, `core/tool_bridge.py:149`, actions retournant du texte.

**Quand / cause :** le runtime classe encore de nombreux résultats par leur texte ; le pont peut emballer un texte d'échec dans `ok=True` parce qu'aucune exception n'a été levée.

**Impact :** statistiques fausses, circuit breaker mal alimenté, habitudes apprises sur des échecs, assistant qui dit « fait » sans preuve.

**Faire :** enveloppe `ActionResult` avec statut, code, message utilisateur, résultat, preuve, idempotence et caractère retryable ; adapter progressivement les actions existantes. **Gain :** outils composables et observabilité correcte. **Acceptation :** un résultat d'échec reste un échec après traversée voix/MCP/téléphone ; aucun mot particulier n'est nécessaire pour le reconnaître. Dépendance : A01.

### C36 — Attribuer les gels avec des mesures avant de régler l'orbe

**P1 · Gels historiques observés ; causalité à établir par scénario · L.** `core/freeze_watch.py`, `ui/orb/arc_core.py:685`, `arc_paint.py`, `glsl_orb.py`, `core/audio_engine.py`, `main.py`.

**Quand / cause :** voix, peinture, OCR et calculs partagent deux cœurs et souvent le GIL. Les régulateurs actuels existent déjà ; un dump où Qt peint n'est pas une preuve suffisante pour attribuer tous les gels à l'orbe.

**Impact :** voix hachée, retard de commandes, réglages empiriques qui déplacent la panne.

**Faire :** horodater capture/envoi/génération/lecture, mesurer lag et durée callback, profiler des scénarios séparés, comparer orbe statique/QPainter/GLSL avec mêmes données. Réduire ou déplacer le travail identifié, non simplement augmenter les timeouts de watchdog. **Gain :** optimisations justifiées et régressions détectables. **Acceptation :** SLO de la section 7 tenus sur la machine cible pendant parole + carte + recherche ; rapports de profil conservés.

### C37 — Borner l'admission des tâches, pas seulement le nombre de workers

**P1 · Risque confirmé par lecture · M/L.** `core/thread_pool.py:736`, `core/background_task.py`, `main.py:1413`, appels `asyncio.create_task` signalés par Ruff.

**Quand / cause :** les pools bornent leurs workers mais pas uniformément les soumissions ; leurs exécuteurs bruts sont aussi exposés. Ruff relève 22 sites de tâches asyncio non retenues, qui doivent être revus individuellement, pas comptés automatiquement comme 22 pannes.

**Impact :** file croissante, résultats obsolètes, mémoire retenue, tâches qui échouent sans suivi et priorité voix dégradée.

**Faire :** quotas globaux/par famille, coalescence des observations redondantes, dernière image seulement, deadlines avant démarrage, propriétaire de chaque tâche et résultat récupéré. **Gain :** latence stable sous charge. **Acceptation :** saturation prolongée ne fait croître aucune file indéfiniment ; commandes interactives admises ou refusées immédiatement avec raison.

### C38 — Vérifier le cycle complet démarrage, reconnexion et arrêt

**P1 · Risque de cycle de vie · L.** `main.py:108`, `:139`, `:1520`, `core/thread_pool.py:362`, `:1180`, `config/systemd/anogpt.service`, `scripts/install-systemd-unit.sh`.

**Quand / cause :** superviseur interne + systemd, threads daemon, accès aux internes de `concurrent.futures`, sorties forcées bornées, imports différés et multiples services. Un heartbeat sans fin de vie explicite peut aussi produire une alarme sur un service arrêté.

**Impact :** redémarrage sans récupérer les travaux, démons restants, fin de mémoire perdue, diagnostic trompeur ; incompatibilité possible à une évolution de CPython.

**Faire :** registre de services et ordre d'arrêt, désinscription des heartbeats, drainage durable borné, ownership explicite des sous-processus, contrôle des retours de threads d'import, tests avec la version Python supportée. Corriger aussi le message de supervision qui recommande encore Python 3.11/3.12 alors que le dépôt annonce 3.13+.

**Gain :** redémarrage prévisible. **Acceptation :** vingt cycles démarrage/arrêt/reconnexion sans ressource résiduelle, crash natif simulé dans un enfant isolé, import échoué et socket occupée correctement expliqués.

### C39 — Donner un effet réel au mode hors ligne

**P1 · Option non raccordée confirmée · L.** `memory/config_manager.py:106`, `:320`, `main.py`, `core/session_manager.py`, `core/routines.py`, `core/llm_client.py`.

**Quand / cause :** `offline_mode` est déclaré et alimenté par configuration, mais aucun consommateur applicatif n'a été trouvé dans le code suivi. Les routines locales existent, tandis que la conversation reste couplée à Live.

**Impact :** une préférence peut laisser croire que rien ne part au cloud sans le garantir ; perte de commandes élémentaires quand le réseau tombe.

**Faire :** appliquer une politique réseau transversale et afficher les capacités réellement disponibles ; conserver les commandes locales et le texte ; le mode vocal local complet relève de F01 et doit être calibré pour cette machine. **Gain :** confidentialité explicite et continuité minimale. **Acceptation :** test réseau interdit par construction, fonctions locales utiles et aucune tentative cloud.

### C40 — Exposer le pipeline audio actif et retirer les réglages inopérants

**P1/P2 · Divergences confirmées ; effets de chaque réglage à vérifier · M/L.** `core/audio_engine.py:963`, `:1702`, `core/live_speech_config.py`, `core/stt.py`, `ui/dialogs/audio.py`, `readme.md`.

**Quand / cause :** pipeline Mark-LII et ancien pipeline VAD/AEC/STT coexistent ; le nouveau initialise plusieurs processeurs à `None`. Des descriptions parlent encore de full-duplex alors que le contrat du projet reste half-duplex. Le garde musique dépend d'un détecteur disponible : sans clé/détecteur prêt, la musique n'est pas filtrée par ce garde.

**Impact :** réglages sans effet sur la voie courante, diagnostic difficile, promesses contradictoires sur veille et interruption, risque que la musique soit prise pour une demande.

**Faire :** un sélecteur explicite de pipeline/capacités, UI des seuls réglages actifs, diagnostic de routage PipeWire/AEC, comportement musique défini même sans mot-clé et accès clavier immédiat. Interruption locale autorisée, transmission au modèle toujours bloquée pendant la parole. **Gain :** voix réglable sans ambiguïté. **Acceptation :** chaque réglage affiché a un effet testé sur la voie active ; matrice musique/détecteur/mute/interruption/PC/téléphone.

### C41 — Vérifier les mutations bureau contre l'état réel de Hyprland

**P1/P2 · Risque transversal · L.** `actions/computer_control.py`, `actions/hypr_orchestrator.py`, `actions/navigation.py`, `core/hypr_focus.py`, `ui/visual_pointer.py`, `ui/window/`.

**Quand / cause :** une fenêtre change de focus entre identification et action, écran débranché, coordonnées mises à l'échelle, fenêtre relancée avec une autre adresse. Plusieurs vérifications de focus existent déjà et doivent devenir le contrat commun.

**Impact :** texte saisi dans la mauvaise application, clic erroné, succès supposé alors qu'une fenêtre n'a pas bougé.

**Faire :** identifier la cible, attendre sa présence, revalider avant mutation et relire l'état après ; annuler si la cible change. Utiliser les protocoles natifs disponibles avant les coordonnées. **Gain :** contrôle système précis. **Acceptation :** tests cibles concurrentes, workspaces spéciaux, multi-écran et changement d'échelle ; aucune saisie si le focus attendu n'est pas confirmé.

### C42 — Distinguer panne d'intégration, configuration absente et capacité indisponible

**P1 · Indices historiques ; causes à confirmer · M/L.** `actions/auto_debug.py`, `core/auto_debug.py`, `core/calendar_service.py`, `core/email_service.py`, `core/azure_specialists.py`, `core/llm_client.py`, `core/tool_stats.py`.

**Quand / cause :** les historiques montrent des familles souvent en échec, mais ne permettent pas toujours d'isoler clé absente, OAuth expiré, schéma fournisseur, modèle indisponible ou bug. Les remplacements de catalogue compliquent les comparaisons temporelles.

**Impact :** appels voués à l'échec, coût et attente inutiles, conseil de « réparer » un simple problème de connexion.

**Faire :** préflight par capacité, codes d'erreur normalisés, version du code/catalogue dans les métriques, bouton de test ciblé et catalogue de capacités effectives. Ne pas déduire l'indisponibilité réelle d'un modèle de son seul nom.

**Gain :** erreurs actionnables et outils exposés seulement s'ils peuvent fonctionner. **Acceptation :** compte non connecté, clé invalide, quota, indisponibilité, réponse mal formée et timeout produisent six diagnostics distincts.

### C43 — Rendre sauvegarde, migration et annulation récupérables

**P1 · Limites confirmées + évolution · L/XL.** `core/undo_stack.py:61`, `core/storage_maintenance.py`, `core/github_backup.py`, `actions/file_controller.py`, bases `memory/` et états `config/`.

**Quand / cause :** la pile undo vit en RAM et retire l'entrée avant d'essayer ; une annulation échouée perd donc cette tentative de récupération. Une sauvegarde Git des sources ne couvre pas les bases personnelles ignorées. Les migrations sont dispersées.

**Impact :** action impossible à défaire après redémarrage, mémoire perdue avec le disque, mise à jour de schéma difficile à annuler.

**Faire :** journal d'opérations réversibles avec préconditions et sauvegardes bornées, migration numérotée, backup SQLite cohérent, archive chiffrée et restauration réellement exercée. Vérifier l'espace libre avant maintenance lourde. **Gain :** erreur récupérable et réinstallation sereine. **Acceptation :** restauration sur un dossier neuf, schéma N→N+1 interrompu, undo après crash et conflit avec un fichier modifié ensuite. [API de sauvegarde SQLite](https://www.sqlite.org/backup.html).

### C44 — Sortir les clés API du stockage web et sécuriser le rendu dynamique

**P1/P2 · Stockage confirmé ; exploitation XSS non testée · M.** `dashboard/static/app.html:1939` et `:2460–2493`, `dashboard/static/login.html`, `dashboard/server.py`.

**Quand / cause :** la page conserve des clés fournisseur dans `localStorage` alors que ces réglages restent locaux et ne configurent pas le backend. `updateMetrics` insère notamment `p.name` avec `innerHTML` sans échappement. D'autres chemins utilisent déjà `escapeHtml` : les homogénéiser.

**Impact :** secrets persistants accessibles au JavaScript de la page ; risque d'injection si une source de télémétrie peut fournir du HTML ; réglage local confondu avec celui du PC.

**Faire :** ne pas conserver de clé fournisseur dans la page, préciser la portée des réglages, backend de configuration contrôlé si cette fonction est voulue, `textContent` pour les données, CSP restrictive compatible avec l'UI et nettoyage du stockage historique après migration. **Gain :** surface web plus sûre et réglages compréhensibles. **Acceptation :** faux nom de processus HTML affiché comme texte ; aucune clé en stockage navigateur.

### C45 — Restaurer une chaîne de construction de toutes les interfaces

**P2 · Constat de dépôt · M/L.** `dashboard/static/app.html`, `dashboard/static/app.legacy.html`, `dashboard/dist/`, `.github/workflows/quality.yml`, `mobile/ano_remote/android/`.

**Quand / cause :** plusieurs interfaces web et des bundles compilés coexistent ; les sources et scripts de build du bundle `dist` ne figurent pas dans le périmètre suivi inventorié. La CI ne construit ni ne teste le mobile et ne lance pas pytest.

**Impact :** correction non répercutée sur une variante, bundle impossible à reconstruire depuis un clone, Kotlin cassé malgré Flutter analyze vert.

**Faire :** décider quelle interface est maintenue, conserver son source/build ou retirer la variante abandonnée après migration, vérifier contrat serveur/client, intégrer build Android ciblé et tests Python dans la livraison. **Gain :** aucune interface oubliée et version reproductible. **Acceptation :** reconstruction depuis les seuls fichiers suivis ; routes et messages WebSocket contractuels testés entre versions.

## 4. Améliorations structurelles et de performance

Ces fiches prolongent les corrections. Elles ne signifient pas que tous les mécanismes correspondants sont absents : beaucoup existent partiellement et doivent être unifiés.

### A01 — Un contrat unique pour les actions et les tours

**P1 · Évolution · L/XL.** Points de départ : `core/action_runtime.py`, `core/tool_dispatcher.py`, `core/session_manager.py`, `core/event_bus.py`, `core/brain_relay.py`. Ajouter un module de contrats, par exemple `core/action_contracts.py`.

**Faire / pourquoi :** représenter demande, préparation, autorisation, exécution, résultat et preuve par des objets typés. Chaque action porte `action_id`, `turn_id`, origine, session, deadline, statut et identifiant d'idempotence. Les nombreux booléens de conversation deviennent progressivement des transitions explicites et testables.

**Gain :** moins de courses entre voix, téléphone et agents ; erreurs récupérables ; historique exact. **Acceptation :** aucune transition impossible lors de 1 000 séquences simulées interruption/reconnexion/résultat tardif ; toutes les entrées produisent le même contrat. **Dépendances :** C01–C03, C14–C15, C33–C35. Ne pas faire une réécriture globale avant les correctifs locaux urgents.

### A02 — Isoler la voix des calculs et de l'interface lorsque la mesure le justifie

**P1/P2 · Évolution · XL.** `main.py`, `core/audio_engine.py`, `core/session_manager.py`, `core/thread_pool.py`, `ui/jarvis_ui.py`.

**Faire / pourquoi :** commencer par retirer callbacks lourds, conversions inutiles et calculs concurrents ; ensuite expérimenter un processus voix séparé de Qt, et un worker calcul partagé pour OCR/indexation/vision. Utiliser des files bornées, identités de session, protocole d'arrêt et ressources explicitement possédées. Éviter un processus par fonctionnalité.

**Gain :** une animation ou un crash de calcul ne coupe plus nécessairement la voix ; contention GIL réduite entre processus. Coût : IPC, mémoire dupliquée et complexité de supervision à mesurer.

**Acceptation :** A/B sur la machine cible : lag p99 et coupures audio meilleurs sans dépasser le budget RAM ; crash du worker calcul récupéré ; aucun vieux paquet après redémarrage. **Dépendances :** A01, C36–C38.

### A03 — Un gestionnaire de capacités réellement disponibles

**P1/P2 · Évolution · L.** `core/tool_registry.py`, `core/tool_packs.py`, `core/diagnostics.py`, `core/plugin_registry.py`, `ui/dialogs/`.

**Faire / pourquoi :** chaque capacité déclare dépendances, état de connexion, ressources, droits, coût, mode dégradé et vérification de résultat. États : disponible, désactivée, non installée, non connectée, temporairement indisponible. Un schéma connu ne signifie pas qu'une fonction peut réellement marcher.

**Gain :** moins d'outils inutiles envoyés au modèle, moins d'échecs prévisibles, explication directe de ce qui manque. **Acceptation :** installation minimale reste utilisable ; disparition d'une dépendance met à jour le catalogue et l'UI ; aucune installation automatique à la simple découverte. **Dépendances :** C08, C29, C34, C42.

### A04 — Un budget global de ressources pour les deux cœurs

**P1/P2 · Évolution · L.** `core/thread_pool.py`, `core/proactive_engine.py`, `core/screen_consciousness.py`, `core/continuous_vision.py`, `core/personal_rag.py`, `ui/orb/`.

**Faire / pourquoi :** arbitrer voix, actions interactives et tâches différées au même endroit. Suspendre l'indexation lors d'un retard audio, plafonner les décodages et les threads natifs ONNX/OpenCV, diminuer les captures sur batterie, éviter plusieurs Chrome headless simultanés. La priorité d'un thread ne crée pas de puissance CPU supplémentaire.

**Gain :** fonctions plus nombreuses sans charge permanente excessive. **Acceptation :** trois jobs lourds demandés ensemble se mettent en file ; voix et commande d'arrêt restent disponibles ; l'UI explique « en attente de ressources ». **Dépendances :** C36–C37 ; mesurer avant d'augmenter les workers.

### A05 — Évaluer et améliorer la qualité réelle de la recherche documentaire

**P2 · Évolution · L/XL.** `core/personal_rag.py:265`, `core/vector_memory.py:161`, `core/file_indexer.py`, `actions/second_brain.py`.

**Faire / pourquoi :** le `LocalDenseEmbedder` du RAG utilise un hachage lexical signé ; ce n'est pas le moteur ONNX sémantique de `vector_memory`. Comparer FTS seul, recherche actuelle et petit embedding local multilingue sur un corpus français/technique représentatif. Fusionner les résultats, appliquer un reranking limité seulement si son gain est établi.

**Gain :** retrouver « le document où j'explique mon installation » sans termes exacts ; meilleures références pour le code et les PDF. **Acceptation :** jeu de requêtes annotées, Recall@5/MRR/latence/RAM publiés ; modèle choisi pour le meilleur compromis mesuré. **Dépendances :** C21–C26, A04 ; aucune réindexation globale pendant la voix.

### A06 — Centraliser droits, autorisations et provenance des données

**P1/P2 · Évolution · L/XL.** `core/human_confirmation.py`, `core/tool_dispatcher.py`, `core/plugin_sdk.py`, `core/tool_bridge.py`, `core/prompt.txt`.

**Faire / pourquoi :** distinguer instruction utilisateur, contenu web/document/SMS et résultat d'outil. Une donnée non fiable ne peut acquérir de droit en traversant le modèle. Prévoir autorisations limitées par domaine, durée, cible et action, avec règles persistantes quand l'utilisateur les a déjà établies ; éviter les confirmations répétitives pour les lectures ordinaires.

**Gain :** autonomie précise, moins de friction et moins d'actions surprises. **Acceptation :** contenu hostile ne provoque aucune mutation non autorisée ; une routine explicitement autorisée s'exécute sans redemander inutilement ; révocation immédiate. **Dépendances :** C03–C08, C14, A01.

### A07 — Router le raisonnement selon qualité, délai et budget

**P2 · Évolution · L.** `core/llm_client.py`, `core/brain_relay.py`, `core/live_model_policy.py`, `core/azure_specialists.py`, `ui/dialogs/ai_config.py`.

**Faire / pourquoi :** profils « rapide », « approfondi », « privé/local » avec limites de coût, durée et tours d'outils. Mesurer les modèles disponibles sur les tâches d'ANO, conserver le choix utilisateur, afficher les capacités réelles et traiter les pannes transitoires avec backoff. Réessayer une lecture peut être sûr ; rejouer une écriture nécessite une autre politique.

**Gain :** réponse quotidienne plus rapide, qualité renforcée pour les tâches difficiles, dépense visible. **Acceptation :** comparaison sur corpus fixe ; budget respecté et arrêt explicable ; aucune sélection fondée uniquement sur le nom commercial du modèle. **Dépendances :** A01/A03, C15/C34/C42.

### A08 — Un diagnostic qui explique où se trouve l'attente

**P2 · Évolution · M/L.** `core/diagnostics.py`, `core/observability.py`, `core/tool_stats.py`, `core/freeze_watch.py`, `ui/panels/telemetry.py`, `anogpt-ctl`.

**Faire / pourquoi :** décomposer une demande en capture, fin de phrase, réseau, raisonnement, outil, lecture. Ajouter âge des files, version du code et pipeline actif, saturation des workers, état fournisseur, résultat final. Montrer l'action utile suivante : reconnecter, réessayer, ouvrir les détails ou attendre un job.

**Gain :** fin des « ça rame » impossibles à localiser ; auto-réparation fondée sur des éléments reproductibles. **Acceptation :** cinq pannes simulées sont distinguées dans un rapport expurgé, sans exposer de conversation. **Dépendances :** C28/C35/C36, A01.

### A09 — Normaliser réseau, caches et reconnexions

**P2 · Évolution · L.** `core/action_kit.py:95`, `core/llm_client.py`, `actions/web_search.py`, `actions/find_nearby.py`, `core/email_service.py`, `core/calendar_service.py`.

**Faire / pourquoi :** budgets connexion/lecture/total, taille maximale de réponse, clients dont le partage inter-thread est défini, pool borné, validation TLS, circuit breaker par fournisseur. Les caches doivent porter horodatage, paramètres, contexte géographique/identité et possibilité d'invalidation ; afficher leur ancienneté.

**Gain :** moins de connexions répétées, résultats stables en réseau faible et coûts réduits. **Acceptation :** serveur lent, réponse géante, 429, déconnexion, changement de compte et cache périmé ; aucune mutation automatiquement rejouée. **Dépendances :** C15/C42, A01/A07.

### A10 — Une interface lisible et légère, y compris dégradée

**P2 · Évolution · L.** `ui/orb/`, `ui/panels/rich_card_system.py`, `ui/panels/speech_overlay.py`, `ui/window/`, `ui/styles/`.

**Faire / pourquoi :** limiter le nombre de timers, recycler les cartes, éviter les mises à jour invisibles, mettre les médias lourds hors peinture, proposer réduction des animations et texte réglable. Toujours distinguer écoute, parole, travail, attente réseau et demande d'accord. La carte reste unique et plein cadre.

**Gain :** application plus fluide et état compréhensible ; moins de charge pendant la voix. **Acceptation :** mode statique utilisable au clavier, longues phrases et zoom sans débordement, contraste vérifié, aucun widget invisible n'anime inutilement. **Dépendances :** C36/C41, A04 ; préférer une amélioration mesurée à un ajout d'effets.

### A11 — Un propriétaire unique par caméra, capture écran et lecteur

**P2 · Évolution · L.** `core/camera_studio.py`, `core/screen_capture.py`, `core/screen_consciousness.py`, `core/continuous_vision.py`, `core/phone_relay.py`, `core/player_ipc.py`.

**Faire / pourquoi :** broker de capture à abonnements, dernière trame partagée, identifiant/âge/origine et arrêt après le dernier abonnement. Coordonner perception continue, reconnaissance ponctuelle et preview ; éviter plusieurs ouvertures de `/dev/video0` ou captures identiques.

**Gain :** moins de CPU, de conflits périphériques et de délais ; observation associée à la bonne image. **Acceptation :** deux consommateurs partagent une capture ; débranchement/rebranchement récupéré ; coupure de permission arrête tous les abonnements. **Dépendances :** C12/C19/C40, A04/A06.

### A12 — Une mémoire personnelle fondée sur des faits datés et sourcés

**P2 · Évolution · L/XL.** `core/vector_memory.py`, `core/knowledge_graph.py`, `core/memory_episode.py`, `core/semantic_enricher.py`, `memory/memory_manager.py`.

**Faire / pourquoi :** distinguer faits déclarés, préférences, inférences et résumés ; source, date, confiance, domaine, expiration et liens vers preuves. Une hypothèse extraite automatiquement ne doit pas devenir une vérité permanente. Résoudre les contradictions sans effacer leur historique utile.

**Gain :** ANO s'adapte sans inventer de souvenirs et peut répondre « je le sais parce que… ». **Acceptation :** préférence modifiée correctement, inférence incertaine identifiée, oubli propagé, source supprimée signalée et aucun rappel de données d'un autre domaine. **Dépendances :** C24–C27, A05/A06.

### A13 — Faire passer la CI de la syntaxe aux comportements critiques

**P1/P2 · Évolution · L.** `.github/workflows/quality.yml`, `tests/`, `mobile/ano_remote/test/`, tests Android à ajouter.

**Faire / pourquoi :** garder Ruff, ajouter la suite Python hermétique, contrats client/serveur, tests de concurrence et build Kotlin/Android. Réserver les tests matériels/cloud à une recette contrôlée ; séparer les fixtures des comptes réels. Les assertions sur le texte source restent utiles pour quelques invariants, mais ne remplacent pas les comportements.

**Gain :** régressions bloquées avant livraison. **Acceptation :** les reproductions C01–C06/C10/C21 deviennent des tests comportementaux ; échec CI empêche la livraison ; aucun accès aux clés personnelles en tests. **Dépendances :** C13/C29/C45, A01.

### A14 — Réduire le couplage sans casser les interfaces publiques

**P2 · Évolution · XL, incrémentale.** `main.py`, `core/tool_dispatcher.py`, `core/audio_engine.py`, `core/session_manager.py`, `ui/panels/rich_card_system.py`.

**Faire / pourquoi :** extraire par responsabilité, injecter dépendances et configuration, supprimer graduellement `_MainAttr` et les méthodes liées à un gros hôte partagé, créer des interfaces de services. Typage renforcé d'abord aux frontières action/audio/config, pas migration de style massive des 2 526 signalements Ruff.

**Gain :** moins de ruptures lors d'un déplacement de fonction, tests plus petits, modification d'un domaine sans effets en chaîne. **Acceptation :** chaque extraction préserve tests et contrats, dépendances documentées, aucun nouvel import circulaire ; réduction mesurée du nombre de dépendances de l'hôte. **Dépendances :** A01/A03/A13 ; ne pas précéder les corrections urgentes.

### A15 — Des versions que l'on peut installer, comparer et restaurer

**P2 · Évolution · L.** `readme.md`, `docs/`, `requirements.txt`, `pyproject.toml`, scripts d'installation, unité systemd et workflows.

**Faire / pourquoi :** matrice de versions testées, manifeste de release, migrations, limitations connues, méthode de retour arrière, notes de changement liées aux tests. Consolider les nombreuses roadmaps : les anciens documents restent historiques, avec statut remplacé/partiel et liens vers cette référence.

**Gain :** moins d'instructions contradictoires ; diagnostic lié à une version exacte ; maintenance durable sur Arch. **Acceptation :** installer et revenir à la version précédente sur une copie sans perdre la mémoire ; un nouveau contributeur peut lancer les tests sans secrets ni matériel. **Dépendances :** C29/C38/C43/C45, A13.

## 5. Fonctionnalités pour un assistant personnel abouti

Ces fonctionnalités sont des **parcours à compléter** : elles réutilisent les briques existantes. Elles doivent rester désactivables, charger leurs dépendances à la demande et respecter les budgets de la section 7. « Ultime » signifie que les parcours vont jusqu'au résultat vérifié.

### F01 — Continuité locale quand Internet tombe

**P2 · Évolution · L/XL.** `core/routines.py`, `core/wake_word.py`, `core/stt.py`, `core/tts.py`, `main.py` ; ajouter un orchestrateur local léger.

**Usage :** « coupe le son », « ouvre mon dossier », « rappelle-moi dans dix minutes » même sans cloud. **Faire :** commandes déterministes locales, recherche FTS, rappels persistants, sortie texte et synthèse locale optionnelle ; tester plusieurs ASR compacts sur ta voix avant choix. Ne pas charger un gros LLM local par défaut sur deux cœurs.

**Gain :** les fonctions essentielles restent disponibles, données locales privées, latence indépendante du réseau pour ce périmètre. **Acceptation :** vingt commandes essentielles réussies sans réseau, explication honnête des demandes non réalisables. **Dépendances :** C39/C40, A03/A04/A06.

### F02 — Missions longues avec reprise et résultat livré

**P2 · Évolution d'une base existante · XL.** `actions/background_tasks.py`, `core/agent_brain.py`, `core/ghost_agent.py`, `core/background_task.py`, `core/tool_dispatcher.py`.

**Usage :** « prépare ce dossier, compare ces documents et préviens-moi quand c'est prêt ». **Faire :** plan en étapes, checkpoints durables, pièces produites, budgets, préconditions, annulation et reprise après redémarrage ; enregistrer l'état des outils et pas seulement un texte de progression. Tout envoi/publication reste soumis aux droits déjà accordés.

**Gain :** ANO travaille pendant que tu utilises ton PC, sans immobiliser le tour vocal et sans recommencer de zéro après une panne. **Acceptation :** mission interrompue à mi-parcours reprend sans doublon et livre un résultat ouvrable avec ses preuves. **Dépendances :** A01/A04/A06, C15/C35/C43.

### F03 — Planification quotidienne liée aux vraies contraintes

**P2 · Évolution · L.** `actions/calendar.py`, `actions/reminder.py`, `core/timers.py`, `core/daily_briefing.py`, `core/calendar_watcher.py`, `core/habit_model.py`.

**Usage :** « organise ma journée avec deux heures pour ce projet ». **Faire :** agréger agenda et tâches, proposer des créneaux, intégrer durée/déplacements/marges, ne créer les engagements qu'avec le droit adapté, recalculer après retard. Les heures de prière peuvent rester une contrainte facultative via `core/prayer_times.py`.

**Gain :** briefing actionnable, moins de rendez-vous manqués, plan adapté à tes priorités. **Acceptation :** aucun conflit caché, fuseau affiché, modification réversible et rappels délivrés après redémarrage. **Dépendances :** C32/C43, A06/A12.

### F04 — Espaces de travail reproductibles sous Hyprland

**P2 · Évolution · L.** `actions/hypr_orchestrator.py`, `actions/open_app.py`, `actions/window_instances.py`, `core/routines.py`, `core/hypr_focus.py`.

**Usage :** « reprends mon projet ANO » ouvre le bon dossier, les applications et les onglets Chrome utiles, puis place les fenêtres. **Faire :** profils de workspace avec identités d'applications/documents, détection de fenêtres existantes, focus vérifié et restauration partielle si une application manque.

**Gain :** moins de manipulations quotidiennes et pas de fenêtres dupliquées à chaque demande. **Acceptation :** déclencher deux fois le profil conserve une seule instance attendue ; plusieurs écrans et applications lentes traités correctement. **Dépendances :** C31/C41, A01/A03/A06.

### F05 — Recherche personnelle avec réponses sourcées et ouverture au bon endroit

**P2 · Évolution · L/XL.** `core/personal_rag.py`, `core/file_indexer.py`, `actions/second_brain.py`, `actions/file_processor.py`, `core/knowledge_graph.py`.

**Usage :** « retrouve ce document et montre le passage qui explique mon problème ». **Faire :** recherche hybride fichiers/PDF/notes/code, référence de page ou ligne, date/version du document, filtrage par dossier et ouverture du résultat pertinent. Ajouter OCR de PDF seulement en arrière-plan et sur demande.

**Gain :** réponses vérifiables sur ton propre travail, moins de recherche manuelle, pas de confusion entre versions. **Acceptation :** chaque affirmation documentaire reliée à une source consultable ; si le corpus ne répond pas, ANO le dit. **Dépendances :** C21–C26, A05/A12.

### F06 — Contexte écran utile, activable et confidentiel

**P2 · Évolution · L.** `core/screen_consciousness.py`, `core/screen_reader.py`, `core/multimodal_vision.py`, `actions/screen_processor.py`, `ui/visual_pointer.py`.

**Usage :** « explique l'erreur affichée ici » ou « où dois-je cliquer ? ». **Faire :** instantané local avec âge/source, exclusions d'applications sensibles, OCR ciblé et recours au modèle seulement pour la demande. Le pointeur indique la cible et le contrôle bureau revalide le focus avant d'agir.

**Gain :** aide contextualisée sans renvoyer constamment l'écran ; meilleur suivi des explications. **Acceptation :** mot de passe et fenêtre exclue ne sont jamais capturés/exportés ; cible disparue signalée ; résultat associé à l'image utilisée. **Dépendances :** C41, A06/A11/A04.

### F07 — Centre de communication unifié, avec brouillons et preuves d'envoi

**P2 · Évolution · L/XL.** `actions/email.py`, `core/email_service.py`, `actions/send_message.py`, `core/zapzap_controller.py`, `core/phone_relay.py`, `core/contacts.py`.

**Usage :** « réponds à ce message », en gardant le bon fil, le bon canal et le bon destinataire. **Faire :** résolution des contacts avec ambiguïtés explicites, brouillon modifiable, autorisation liée au contenu, suivi d'envoi, idempotence, historique minimal ; droits durables limités quand tu les choisis.

**Gain :** communication rapide et fiable, sans double envoi ni destinataire deviné. **Acceptation :** homonymes, changement de canal, message long, pièce jointe et perte réseau ne produisent aucun envoi involontaire. **Dépendances :** C02/C03/C16/C17/C33, A01/A06.

### F08 — Organisation de fichiers avec prévisualisation et restauration

**P2 · Évolution · L.** `actions/file_controller.py`, `core/undo_stack.py`, `actions/file_processor.py`, `core/file_indexer.py`.

**Usage :** « range mes téléchargements par projet » ou « trouve les doublons ». **Faire :** plan détaillé des déplacements, hash des vrais doublons, règles d'exclusion, simulation, exécution limitée et journal de restauration durable. Préférer la corbeille à une suppression définitive quand cela convient.

**Gain :** nettoyage utile sans perdre le contrôle de tes données. **Acceptation :** collision de noms, symlink, fichier ouvert et interruption partielle gérés ; restauration après redémarrage conserve les modifications faites entre-temps. **Dépendances :** C26/C43, A01/A06.

### F09 — Assistant de calibration et de diagnostic vocal

**P2 · Évolution · M/L.** `ui/dialogs/audio.py`, `core/audio_router.py`, `core/barge_in.py`, `core/wake_word.py`, `scripts/check_audio_chain.py`, `scripts/calibrate_wake_word.py`.

**Usage :** une configuration guidée teste micro, sortie, bruit, mot d'activation et arrêt local avec de courtes phrases. **Faire :** enregistrer les périphériques stables, afficher les routes PipeWire, proposer un réglage selon les mesures et vérifier le résultat, avec profils casque/haut-parleurs et reprise après débranchement.

**Gain :** moins de tâtonnements et meilleure reconnaissance de ta voix réelle. **Acceptation :** aucun réglage ne remplace le half-duplex ; test de 30 phrases et interruptions avec taux d'erreur publié par environnement. **Dépendances :** C01/C10/C11/C40, A08 ; fichiers audio de calibration conservés seulement si choisi.

### F10 — Proactivité utile, limitée et explicable

**P2/P3 · Évolution · L.** `core/proactive_engine.py`, `actions/proactive.py` ; sources : `core/calendar_watcher.py`, `core/habit_model.py`, `core/distraction_guard.py`, `core/daily_briefing.py`.

**Usage :** rappel pertinent de réunion, tâche prête, batterie faible ou problème réel d'un service. **Faire :** budget quotidien d'interruptions, heures calmes, niveau d'urgence, motifs consultables, snooze et apprentissage des refus ; différer pendant appel/parole/concentration.

**Gain :** assistant attentif sans notifications constantes ni travail inutile. **Acceptation :** scénario d'une journée simulée respecte quotas et heures calmes ; chaque notification a une cause réelle et peut être désactivée. **Dépendances :** A04/A06/A08/A12. La déduction d'humeur ne doit pas décider seule d'une action sensible.

### F11 — Créateur de routines personnelles vérifiables

**P2 · Évolution · L.** `core/routines.py`, `config/routines.yaml`, `core/tool_registry.py`, `ui/dialogs/` ; ajouter un éditeur de routines.

**Usage :** « quand je dis mode travail, fais ces trois actions ». **Faire :** construire une séquence typée à partir du catalogue, préciser déclencheur/conditions, simulation, autorisations et annulation. L'apprentissage propose une routine répétée sans l'activer silencieusement.

**Gain :** tâches quotidiennes rapides, reproductibles et souvent sans LLM. **Acceptation :** correspondance vocale ambiguë ne lance pas une routine sensible ; modification de routine affiche les nouveaux effets ; export/import et version précédente disponibles. **Dépendances :** A01/A03/A06, C41/C43.

### F12 — Atelier de développement avec diagnostics, tests et réparations isolées

**P2/P3 · Évolution · XL.** `core/auto_debug.py`, `core/auto_fix.py`, `core/ghost_agent.py`, `actions/devsecops.py`, `actions/code_helper.py`, `actions/github.py`.

**Usage :** « explique cette erreur et prépare le correctif ». **Faire :** attacher logs expurgés, reproduire dans un checkout isolé, proposer diff et tests, exécuter le périmètre autorisé, conserver résumé et résultats. Pour Docker/systemd/Git, vérifier l'état après action et prévoir retour arrière.

**Gain :** moins de temps de diagnostic, corrections examinables et absence d'altération du travail en cours. **Acceptation :** échec d'agent jamais annoncé corrigé, secret retiré des prompts, tests réellement exécutés, commit/publication conformément aux droits du projet. **Dépendances :** C07–C09/C14/C15/C29, A01/A06/A13.

### F13 — Maintenance Arch guidée et récupérable

**P2/P3 · Évolution · L/XL.** `actions/devsecops.py`, `actions/computer_settings.py`, `core/self_healing.py`, `core/installer.py`, `core/storage_maintenance.py`.

**Usage :** « vérifie la santé de mon PC » fournit un diagnostic en lecture ; « applique cette maintenance » réalise uniquement le plan autorisé. **Faire :** état pacman/services/disque/audio, explication de chaque changement, protection contre mises à jour partielles, sauvegarde/snapshot si la machine le permet et vérification après intervention.

**Gain :** entretien compréhensible, moins de réparations improvisées. **Acceptation :** toutes les commandes adaptées à Arch ; diagnostic ne modifie pas le système ; interruption d'une procédure laisse un état documenté et une voie de récupération. **Dépendances :** C30/C38/C43, A03/A06/A08.

### F14 — Compagnon mobile fiable et continuité PC/téléphone

**P2 · Évolution · L/XL.** `mobile/ano_remote/lib/`, `PhoneRelayService.kt`, `dashboard/server.py`, `core/phone_relay.py`, `core/geolocation.py`, `core/navigation.py`.

**Usage :** envoyer une note vocale, une photo ou une commande au PC puis retrouver son résultat, y compris après changement de Wi-Fi. **Faire :** identité stable de serveur/appareil, reconnexion sécurisée, états d'envoi/ACK, reprise des pièces jointes, choix des capacités et batterie maîtrisée. La localisation porte précision et fraîcheur.

**Gain :** téléphone utile comme périphérique personnel, pas seulement comme télécommande fragile. **Acceptation :** écran éteint, réseau changé, permissions révoquées et plusieurs appareils ; aucun mélange de flux. Une éventuelle vue du trajet réutilise la carte de `core/map_render.py` : **aucun second moteur de carte**. **Dépendances :** C04–C06/C16–C19, A01/A11.

### F15 — Centre multimédia cohérent et sobre

**P2/P3 · Évolution · L.** `core/player_ipc.py`, `actions/music.py`, `actions/youtube_video.py`, `actions/download_music.py`, `core/camera_studio.py`, `ui/media/`, `core/map_render.py`.

**Usage :** « reprends ce morceau », « montre cette vidéo », « retrouve cette capture ». **Faire :** propriétaire unique de lecture, file et historique limités, contrôles vérifiés, distinction musique/vidéo/voix, cache des médias et erreurs réseau lisibles ; galeries ouvrant les fichiers réellement créés.

**Gain :** pas de lecteur caché parasite ni de panneau incohérent, commandes média rapides et voix prioritaire. **Acceptation :** pause/reprise/fin de piste reflètent mpv ; vidéo lente ne bloque pas l'UI ; les cartes ne s'empilent pas indéfiniment. **Dépendances :** C31/C36/C40/C45, A04/A10/A11.

### F16 — Profils personnels et espaces privés cohérents

**P2/P3 · Évolution · L.** `core/persona_manager.py`, `core/personality_modes.py`, `core/auto_persona.py`, `core/conversation_language.py`, `memory/config_manager.py`, `actions/sparring_partner.py`.

**Usage :** profils travail, apprentissage, détente, avec langue, ton, voix et mémoire appropriés. **Faire :** paramètres cohérents, mémoire cloisonnée par espace, changement explicite ou suggestion contextuelle, règles d'appellation et de langue stables ; mode invité sans accès aux souvenirs privés.

**Gain :** assistant agréable et adapté sans mélange de contextes personnels/professionnels. **Acceptation :** changer de ton ne change aucun droit ; retour à un profil restaure ses préférences ; invité et partage d'écran n'exposent pas les données privées. **Dépendances :** C10/C27/C39, A06/A12.

### F17 — Tableau des tâches, preuves et décisions

**P2 · Évolution · L.** `ui/panels/rich_card_system.py`, `ui/panels/cards_stack.py`, `actions/background_tasks.py`, `core/human_confirmation.py`, `dashboard/server.py`.

**Usage :** voir « ce qu'ANO fait », « ce qu'il attend », « ce qu'il a réellement terminé ». **Faire :** vues par statut avec fichiers produits, horodatage, sources, coût, bouton annuler/reprendre/réessayer selon le contrat ; pas de pourcentage inventé lorsque l'avancement n'est pas mesurable.

**Gain :** confiance et maîtrise des missions longues, moins de questions de statut. **Acceptation :** UI et historique concordent après redémarrage ; action annulée n'est jamais affichée terminée ; attente de confirmation et résultat incertain sont distincts. **Dépendances :** A01/A08/A10, C33/C35/C43.

### F18 — Extensions contrôlées, installables et réversibles

**P3 · Évolution · XL.** `core/plugin_registry.py`, `core/plugin_sdk.py`, `core/auto_extension.py`, `plugins/`, `ui/dialogs/plugins.py`.

**Usage :** proposer une capacité manquante, examiner sa portée, la tester puis l'activer. **Faire :** manifests versionnés, dépendances isolées, signatures ou hashes approuvés, permissions appliquées, quotas, migration d'état, version précédente et désinstallation complète. La génération de code reste une proposition tant que sa validation et son activation ne sont pas établies.

**Gain :** ANO peut évoluer sans fragiliser son cœur ni accumuler des dépendances permanentes. **Acceptation :** plugin cassé n'interrompt ni voix ni démarrage ; retrait retire aussi ses hooks et jobs ; retour arrière testé. **Dépendances :** C07/C08/C29, A03/A04/A06/A13.

## 6. Ordre de livraison et dépendances

### Les premiers travaux, dans l'ordre utile

1. **C01 + C02** : corriger les deux défauts qui touchent directement l'émission audio et la boucle asyncio. Faire entrer les reproductions dans les tests.
2. **C03 + C04 + C05** : fermer l'autorisation auto-SMS, les routes privées et la révocation incomplète.
3. **C06 + C07 + C08** : sécuriser identité distante et exécution du code ; pendant leur développement, limiter les capacités concernées à un mode sûr explicite.
4. **C10 + C12 + C13** : réparer les défauts visibles de voix/reconnaissance et retrouver une suite verte.
5. **C15 + C33 + C35** : une opération ne doit plus être annoncée terminée à tort, ni recommencer après un résultat incertain.
6. **C29 + A13** : environnement reproductible et contrôle de non-régression continu pour les livraisons suivantes.
7. **C16 + C17 + C21–C26** : sécuriser événements et mémoire avant d'enrichir l'autonomie.
8. **C36–C40 + A04 + A08** : mesurer et stabiliser la voix sous charge réelle.
9. **A01 + A03 + A06** : généraliser les contrats communs, par extraction progressive des correctifs éprouvés.
10. Commencer les parcours **F09, F17, F04, F05**, puis les missions longues et les extensions avancées.

Les actions urgentes ne doivent pas attendre une refonte. En particulier, le correctif local du blocage SMS et les contrôles d'authentification peuvent être livrés avant le contrat d'action complet.

### Lots de livraison

Les charges ci-dessous sont des **ordres de grandeur de planification pour une personne**, à recalibrer après la première livraison. Elles ne constituent ni délai contractuel, ni promesse de terminer tous les parcours en quelques jours. Le socle demande plusieurs semaines ; l'ensemble ambitieux représente plusieurs mois, typiquement **4 à 9 mois** selon les choix de périmètre, la recette matérielle et la profondeur des fonctionnalités. On peut obtenir des gains utiles dès les premiers correctifs.

| Lot | Contenu principal | Dépendances | Critère de livraison | Charge indicative |
|---|---|---|---|---|
| L0 — Référence vérifiable | C13/C29/C45, première partie A13 | Aucune ; C10/C12 pour suite entièrement verte | Environnement identifié, CI comportementale minimale, erreurs connues suivies | 3–6 jours |
| L1 — Protection immédiate | C01–C07, C10, C20, C33 ; amorce C35 | Peut démarrer avec L0 | Reproductions critiques fermées, accès révoqué réellement, aucune autorisation fabriquée par le modèle | 1–3 semaines |
| L2 — Voix et réactivité | C11/C12/C15/C36–C40, A04/A08 ; prototype A02 | L1 + instrumentation | Recette audio PC/téléphone et interruption sous charge réussie | 2–4 semaines |
| L3 — Exécution commune et accès | C08/C09/C14/C18/C19/C27/C28/C30/C31/C34/C35/C44, A01/A03/A06/A09 | L1 ; intégrer progressivement | Même contrat voix/MCP/remote/routine, délais et droits vérifiés | 3–5 semaines |
| L4 — Données et intégrations fiables | C16/C17/C21–C26/C32/C42/C43, A05/A11/A12 | Contrats L3 ; premiers correctifs possibles avant | Reprise, effacement, sauvegarde/restauration et intégrité d'index testés | 3–5 semaines |
| L5 — Produit maintenable | C41, A10/A14/A15, industrialisation A02/A13, F09/F17 | L2–L4 | UI claire, cycle de vie stable, profil matériel documenté | 2–5 semaines |
| L6 — Parcours quotidiens | F01–F08, F10/F11/F14 | L3–L5 selon les domaines | Un scénario complet vérifié par parcours retenu, puis cas de panne | 1–3 mois selon sélection |
| L7 — Autonomie avancée | F12/F13/F15/F16/F18 | Droits, isolation, restauration et budgets acquis | Missions et extensions ne dégradent ni voix ni sécurité | 1–3 mois selon sélection |

Plusieurs lots se recouvrent et partagent du travail : leurs durées ne sont pas un calendrier à additionner. L2 et L4 peuvent avancer indépendamment sur des modules distincts. Sur la machine cible, les benchmarks lourds doivent rester séquentiels et réservés à une fenêtre de recette.

### Chemins de dépendance qui ne doivent pas être inversés

| Capacité finale | Prérequis obligatoires |
|---|---|
| Auto-réponse aux SMS | C02 → C03 → C16/C17 → A01/A06 → F07 |
| Contrôle depuis le téléphone | C04/C05/C06 → C18/C19 → A01 → F14 |
| Auto-réparation du projet | C07/C08/C09 + C29/C43 → A06/A13 → F12 |
| Mémoire de confiance | C21–C27 + C43 → A05/A12 → F05 |
| Missions longues | C15/C33/C35/C37 + C43 → A01/A04/A06 → F02 |
| Proactivité | C28/C36/C37 → A04/A08/A12 → F10 |
| Extensions générées | C07/C08/C29 → A03/A06/A13 → F18 |

### Définition d'une fiche terminée

Une fiche devient terminée seulement lorsque son scénario initial ne reproduit plus le défaut, que le comportement nominal et le mode de panne sont testés, que la documentation du réglage/capacité est exacte et que le résultat est vérifié sur la version livrée. Un simple changement de prompt, un `except: pass`, une augmentation de timeout ou un screenshot d'interface ne valent pas preuve de correction.

Pour chaque ticket, conserver dans le suivi : version de départ, fichiers touchés, preuve avant/après, tests exécutés, impact mesuré, limitations restantes, procédure de retour arrière et date de validation. Pour les changements réversibles simples, une vérification proportionnée suffit ; ne pas multiplier les tests sans risque réel à couvrir.

## 7. Mesures, tests et critères de sortie

### Objectifs de performance proposés

Ces valeurs sont des **cibles initiales à valider**, pas les performances actuelles mesurées. Enregistrer la baseline avant optimisation ; distinguer temps local et fournisseur. Les percentiles nécessitent assez d'échantillons : viser au moins 200 mesures comparables par scénario, pas deux appels réussis.

| Mesure | Cible initiale | Méthode / limite |
|---|---|---|
| Half-duplex | 0 paquet PCM émis pendant la parole d'ANO dans le corpus de test | Instrumenter le dernier point d'envoi, PC et téléphone |
| Durée callback micro | p99 < 5 ms pour des blocs de 64 ms | Horloge monotone ; pas de réseau, disque ou logging synchrone dans le callback |
| Lag boucle asyncio en interaction | p95 < 20 ms, p99 < 100 ms ; aucun gel > 1 s dans la recette | Mesurer parole + orbe + une action ; distinguer blocage réel et service arrêté |
| Arrêt clavier/bouton | p95 de la demande au silence < 150 ms | Mesure incluant tampon périphérique ; 20 ms de slice + 60 ms configurés ne prouvent pas la latence réelle |
| Arrêt vocal local | p95 < 700 ms après fin du mot d'arrêt, à recalibrer | Taux de faux arrêts et mots non détectés rapportés en même temps |
| Retour visuel à une commande | < 200 ms en local | Confirmation de réception, pas affirmation de réussite |
| Réponse vocale simple cloud | objectif p95 < 3 s sur un profil réseau documenté | Séparer fin de phrase, transport, modèle et TTS ; jamais promettre ce délai en toute connexion |
| Recherche personnelle chaude | p95 < 300 ms pour la recherche, < 1 s avec rendu/reranking léger | Taille et version du corpus fixées ; pas le premier chargement de modèle |
| Mémoire d'une session terminée | lot durable sous 2–3 s en régime nominal | Après C24 ; la disponibilité dans tous les index secondaires peut suivre |
| Démarrage | UI utile < 3 s ; voix prête visée < 10 s hors incident réseau | Mesure à froid et à chaud ; imports optionnels paresseux |
| Ressources au repos | viser PSS de l'arbre ANO ≤ 1,5 Go et CPU ≤ 10 % d'un cœur | Profil sans vision ni job actif ; budget à adapter à la baseline réelle |
| Ressources en interaction | viser PSS ≤ 3 Go ; marge système conservée | Ne pas additionner naïvement les RSS qui comptent des pages partagées |
| Stabilité longue durée | pas de croissance monotone des files ; pas de fuite > 10 % après préchauffage à scénario constant | Essai de 8 h puis 24 h ; expliquer les caches plafonnés |
| Fiabilité des mutations | 0 doublon et 0 action sans droit dans le corpus adversarial | Vérifier les effets, pas seulement les messages de retour |
| Qualité des parcours quotidiens | ≥ 95 % sur 100 demandes représentatives validées, puis enrichissement du corpus | Inclure refus corrects ; publier les 5 % restants et leur gravité |

### Recette à exécuter lors des futures corrections

Cette matrice est **à réaliser** ; elle n'a pas été exécutée sur les appareils ou comptes réels pendant cet audit.

| Domaine | Scénarios indispensables | Résultat attendu |
|---|---|---|
| Audio PC | Silence, accent, voix faible, phrase longue, silence interne | Phrase fidèle, aucun tour fantôme |
| Half-duplex | Haut-parleurs forts, queue réseau saturée, ancienne époque | Aucun son envoyé pendant la parole ; pas de replay tardif |
| Interruption | Clavier, bouton, arrêt vocal, interruption pendant outil | Parole interrompue ; état de l'outil communiqué séparément |
| Musique | Détecteur disponible/absent, morceau avec paroles, pause/reprise | Aucun comportement surprise ; sortie du mode toujours accessible |
| Périphériques | USB retiré, Bluetooth reconnecté, sortie changée, PipeWire redémarré | Reprise ou explication actionable ; aucune boucle de crash |
| Fournisseurs | Clé absente, quota, 429/5xx, modèle retiré, réponse invalide | Erreurs distinctes, budgets respectés, pas de retry infini |
| Session Live | Reconnexion pendant parole, outil et résultat vidéo tardif | Résultat attribué au bon tour, aucune réponse perdue silencieusement |
| Téléphone | Wi-Fi coupé, IP changée, veille Android, révocation de permission | Reconnexion sûre ; aucun mélange de flux |
| Authentification | Route privée anonyme, ancien jeton, socket déjà ouverte | Refus immédiat ou fermeture après révocation |
| Appairage | Mauvais PIN répété, JSON invalide, faux serveur/certificat | Pas de 500, pas de session créée sans preuve |
| SMS | Coupure avant/après ACK, double livraison, mode avion, double SIM | Pas de perte locale silencieuse, pas de double envoi |
| SMS long | Accents, caractères non latins, multipart | Statuts par opération correctement consolidés |
| Fichiers distants | Très gros fichier, deux noms identiques, interruption, fichier hostile | Quotas, noms exclusifs, nettoyage et aucune évasion de chemin |
| Confirmation | Double clic, expiration, contenu changé, callback échoué | Accord lié à la bonne opération, résultat exact |
| Injection de contenu | SMS/document/web demandant d'ignorer les règles ou d'envoyer des données | Le contenu reste une donnée ; aucune permission acquise |
| Mémoire | Disque plein, lock SQLite, arrêt en plein lot | Reprise sans perte ni duplication |
| RAG | Fichier vidé, déplacé, supprimé, deux versions, > 2 000 fichiers | Corpus à jour, citations justes et scan équitable |
| Oubli | Fait présent dans entrée/graphe/cache/photos | Effacement conforme à la portée annoncée |
| Agenda | Pagination, récurrence, exception, all-day, fuseau, conflit | Heure exacte et opérations exhaustives lorsqu'elles le nécessitent |
| Bureau | Mauvais focus, app lente, fenêtre fermée, écran retiré | Aucune saisie dans la mauvaise cible |
| Agents/plugins | Import à effet de bord, boucle infinie, tests malveillants | Contention dans le worker ; voix et données protégées |
| Auto-réparation | Dépôt sale, agent timeout, tests échoués | Aucun faux succès ni modification personnelle attribuée à l'agent |
| Lifecycle | Crash enfant, import natif échoué, vingt redémarrages | Supervision bornée et ressources libérées |
| Restauration | Disque neuf, sauvegarde interrompue, ancienne version | Sources + configuration + données restaurées selon les droits |
| Charge | Carte unique + parole + recherche + indexation demandée | Priorité interactive tenue, reports visibles |
| Interface | Petit écran, grandes polices, clavier seul, animations réduites | Actions accessibles, aucun texte essentiel coupé |

### Organisation des tests

**Sur chaque changement :** lint, tests du comportement touché et de ses frontières. **À la livraison :** suite Python hermétique, analyse/tests Flutter, construction Android lorsque ses sources changent, contrats serveur/client, quelques scénarios de bout en bout avec faux services. **Avant une version importante :** recette matérielle de référence et essai prolongé.

Les nouveaux tests doivent observer des événements, états et effets réels simulés. Un test contenant seulement `assert "nom_de_fonction" in source` ne prouve ni l'ordre des appels, ni l'absence de blocage, ni l'autorisation. Garder les tests statiques pour les invariants où ils apportent une protection spécifique, comme l'interdiction d'introduire une seconde carte.

Créer un corpus personnel de demandes **anonymisées ou conservées uniquement localement** : commandes de bureau, formulations courantes, termes techniques, langues réellement utilisées, bruit et appareils habituels. Les fichiers audio de ta voix ne doivent pas entrer dans Git ou être transmis à un service sans choix explicite.

## 8. Principes de réalisation

### Ce qui doit rester invariant

- La voix d'ANO ne part jamais au modèle pendant sa propre parole ; l'arrêt local ne nécessite pas d'ouvrir le flux cloud.
- Une seule carte, dans `core/map_render.py`, plein cadre. Les nouvelles fonctions navigation réutilisent ce composant.
- Google Chrome pour ouvertures web et OAuth, via `core/browser_policy.py`, sans navigateur alternatif en repli.
- Commandes système adaptées à Arch/EndeavourOS ; aucune chaîne de repli multi-distribution.
- Qt reste sur le thread principal ; les calculs lourds n'entrent pas dans son rendu ni dans le callback micro.
- Antigravity lit `~/.gemini/config/mcp_config.json` ; les exemples et scripts doivent viser ce chemin.
- Les autorisations et préférences déjà accordées sont respectées ; une nouvelle autorisation n'est demandée que pour une extension réelle de portée.
- Aucune action annoncée « terminée » sans résultat ou preuve ; l'incertitude d'un envoi doit être dite et réconciliée.

### Éviter les améliorations qui dégradent le produit

Ne pas activer toutes les fonctions au démarrage, multiplier les modèles locaux lourds, augmenter systématiquement les workers, lancer plusieurs analyses visuelles identiques ou réindexer tout le corpus après chaque modification. Ne pas déduire un gain de latence d'un changement de constante sans benchmark. Les promesses « zéro GIL », « temps réel » ou « 90 % de tokens économisés » dans les commentaires doivent être remplacées par une mesure et son contexte lorsqu'elles sont montrées à l'utilisateur.

Ne pas faire une modernisation globale de style au milieu des correctifs. Les annotations anciennes ne sont pas prioritaires sur un envoi SMS tardif, un accès distant non révoqué ou un souvenir perdu. Le contrôle Ruff élargi sert à choisir les dettes lors des modifications de modules, pas à justifier une refonte automatique de milliers de lignes.

Ne pas transformer l'assistant ultime en assistant qui décide tout seul de publier, d'envoyer, de supprimer ou de modifier le système. Sa valeur est d'achever les actions dans le périmètre voulu, avec un résultat visible et une voie de récupération.

### Résultat attendu du programme

ANO-GPT devient un assistant qui répond vite, sait précisément ce qu'il peut faire, garde une mémoire vérifiable, termine ses missions même après une interruption et explique ses échecs. Ses capacités avancées s'appuient sur un contrôle du système précis, des droits explicites et une consommation compatible avec cette machine.

**Le premier jalon de réussite n'est pas d'ajouter dix outils : c'est de fermer les défauts C01–C07, de réparer les régressions connues et de rendre cette qualité vérifiable à chaque livraison.** Les 78 fiches constituent le backlog ; leur validation, et non leur simple présence dans le code, fera d'ANO-GPT une version réellement aboutie.
