# ANO-GPT — Guide de test des fonctionnalités implémentées

> Guide de validation vocale des 11 fonctionnalités déclarées comme implémentées dans la roadmap ANO-GPT.
>
> Dernière vérification technique : **30 août 2026** — **697 tests réussis**, 1 limite connue couverte par Silero VAD.

---

## Sommaire

1. [Interruption vocale instantanée](#1-interruption-vocale-instantanée)
2. [Perception d'écran et auto-debug](#2-perception-décran-et-auto-debug)
3. [Voice DevSecOps et contrôle Linux](#3-voice-devsecops-et-contrôle-linux)
4. [Agent Fantôme](#4-agent-fantôme)
5. [Prosodie adaptative](#5-prosodie-adaptative)
6. [Second Brain et mémoire sémantique](#6-second-brain-et-mémoire-sémantique)
7. [Navigation GPS parlée](#7-navigation-gps-parlée)
8. [Moteur d'habitudes](#8-moteur-dhabitudes)
9. [Voice ID et anneau de sécurité](#9-voice-id-et-anneau-de-sécurité)
10. [Sparring Partner](#10-sparring-partner-et-simulateur-dentraînement)
11. [Bouclier Anti-Distraction](#11-bouclier-anti-distraction-et-gardien-de-dopamine)
12. [Fiche de résultats](#fiche-de-résultats)

---

## Avant de commencer

### Préparation recommandée

- [ ] ANO-GPT est démarré et affiche l'état **ÉCOUTE**.
- [ ] Le microphone fonctionne correctement.
- [ ] Le volume permet d'entendre clairement ANO.
- [ ] ANO-Remote est connecté pour les essais GPS/téléphone.
- [ ] Un terminal et un petit projet de test sont disponibles.
- [ ] Les essais dangereux sont réalisés uniquement sur des données non importantes.

### Règle de sécurité

Ne teste jamais une fonction de sécurité avec une suppression importante, un formatage de disque ou une commande irréversible. Utilise une calculatrice, un fichier temporaire ou un projet de démonstration.

### Notation

| Symbole | Signification |
|---|---|
| ✅ | Test réussi |
| ⚠️ | Résultat partiel ou comportement à surveiller |
| ❌ | Test échoué |
| 🗣️ | Phrase exacte à prononcer |
| 👀 | Élément à observer |

---

## 1. Interruption vocale instantanée

### Objectif

Vérifier qu'ANO détecte localement une demande d'arrêt pendant qu'il parle et coupe immédiatement sa synthèse vocale.

### Test principal

1. Demande une réponse volontairement longue :

   > 🗣️ « ANO, explique-moi en détail comment fonctionne Linux. »

2. Pendant qu'ANO parle, dis clairement :

   > 🗣️ « ANO stop. »

3. Recommence avec :

   > 🗣️ « Attends. »

### Résultat attendu

- [ ] La parole d'ANO s'arrête immédiatement.
- [ ] La coupure intervient sans clic sur l'interface.
- [ ] ANO revient ensuite en état d'écoute.
- [ ] Sa propre voix ne provoque pas de fausse interruption.

### Validation

**Statut :** ☐ ✅ Réussi — ☐ ⚠️ Partiel — ☐ ❌ Échoué

**Notes :**

> 

---

## 2. Perception d'écran et auto-debug

### Objectif

Vérifier la capture de la fenêtre active, l'identification d'une erreur et la génération d'un diagnostic sans modification silencieuse du code.

### Préparation

Affiche cette erreur dans un terminal :

```bash
python -c "print(10 / 0)"
```

### Phrases de test

> 🗣️ « ANO, regarde mon terminal et explique-moi cette erreur. »

> 🗣️ « C'est quoi ce bug affiché à l'écran ? »

> 🗣️ « Analyse ce traceback et propose-moi une correction, mais ne modifie aucun fichier. »

### Test d'application contrôlée

Utilise uniquement un fichier temporaire ou un projet de test :

> 🗣️ « Analyse cette erreur et applique le correctif au fichier après validation et sauvegarde. »

### Résultat attendu

- [ ] La fenêtre active est capturée automatiquement.
- [ ] `ZeroDivisionError` est correctement identifié.
- [ ] Une explication compréhensible est donnée.
- [ ] Une carte de diagnostic apparaît dans l'interface.
- [ ] Aucun fichier n'est modifié sans autorisation explicite.
- [ ] En cas d'application, une sauvegarde `.bak` est créée.
- [ ] Un diff ou du code Python invalide est refusé.

### Validation

**Statut :** ☐ ✅ Réussi — ☐ ⚠️ Partiel — ☐ ❌ Échoué

**Notes :**

> 

---

## 3. Voice DevSecOps et contrôle Linux

### Objectif

Vérifier les commandes vocales Docker, systemd, paquets, Git, sécurité et Hyprland sans effectuer d'action destructive.

### Tests sans danger

#### Git

> 🗣️ « Donne-moi le statut Git de mon projet actuel. »

#### Docker

> 🗣️ « Liste les conteneurs Docker. »

#### systemd

> 🗣️ « Affiche les services systemd en échec. »

#### Paquets

> 🗣️ « Vérifie les mises à jour disponibles, sans rien installer. »

#### Audit réseau

> 🗣️ « Fais un audit des ports réseau ouverts. »

#### Hyprland

> 🗣️ « Liste les fenêtres ouvertes et leurs bureaux. »

> 🗣️ « Applique le preset de travail coding. »

### Résultat attendu

- [ ] ANO exécute réellement les commandes demandées.
- [ ] Les résultats correspondent à l'état réel de la machine.
- [ ] Une commande indisponible produit un message clair.
- [ ] Une opération risquée demande une confirmation.
- [ ] Les fenêtres sont placées sur les bons workspaces Hyprland.

### Précaution

N'utilise pas encore « purge Docker », « supprime les paquets » ou une commande équivalente pour ce premier test.

### Validation

**Statut :** ☐ ✅ Réussi — ☐ ⚠️ Partiel — ☐ ❌ Échoué

**Notes :**

> 

---

## 4. Agent Fantôme

### Objectif

Vérifier qu'ANO peut déléguer une mission persistante en arrière-plan tout en gardant la conversation principale disponible.

### Test principal

Depuis un projet de démonstration :

> 🗣️ « Délègue en arrière-plan l'analyse de ce dépôt et prépare un rapport sur son architecture. »

### Mission de programmation invisible

> 🗣️ « ANO, donne à agy la tâche d'analyser mon projet ANO-GPT, de corriger les erreurs et d'exécuter les tests en arrière-plan. Préviens-moi quand il termine. »

### Mission avec journal visible dans Kitty

> 🗣️ « Donne à agy la tâche de vérifier les tests de mon projet ANO-GPT en arrière-plan et montre son journal dans Kitty. »

La fenêtre Kitty est seulement un moniteur : la fermer ne doit pas interrompre
la mission `agy`.

### Travailler dans un autre projet

> 🗣️ « ANO, confie à agy la correction des tests dans le projet situé dans `/chemin/absolu/du/projet`, puis préviens-moi avec les fichiers modifiés. »

Remplace le chemin d'exemple par le chemin absolu réel. Sans chemin indiqué,
ANO utilise son propre dépôt et jamais l'ensemble du dossier personnel.

### Suivre les missions actives

Puis demande :

> 🗣️ « Liste les tâches de fond en cours. »

### Consulter le dernier résultat

> 🗣️ « Donne-moi le résultat de la mission de l'agent fantôme. »

Variantes acceptées :

> 🗣️ « Donne-moi le résultat de la dernière mission Agent Fantôme. »

> 🗣️ « Quels fichiers agy a-t-il réellement créés ou modifiés ? »

> 🗣️ « Où est le rapport complet de la dernière mission agy ? »

### Consulter l'historique

> 🗣️ « Affiche l'historique des tâches de fond. »

> 🗣️ « Donne-moi le rapport de la tâche `task-xxxxxxxx`. »

Remplace `task-xxxxxxxx` par l'identifiant annoncé lors du lancement.

### Test d'annulation

> 🗣️ « Annule la tâche de fond numéro [IDENTIFIANT]. »

Variante directe :

> 🗣️ « ANO, annule la tâche `task-xxxxxxxx` et laisse les autres missions continuer. »

Remplace `[IDENTIFIANT]` par l'identifiant réellement annoncé par ANO.

### Résultat attendu

- [ ] La mission apparaît dans la liste des tâches.
- [ ] ANO reste disponible pendant son exécution.
- [ ] La tâche survit au fonctionnement normal du service de fond.
- [ ] Un rapport est produit lorsque la mission se termine.
- [ ] L'annonce nomme les fichiers réellement créés ou modifiés.
- [ ] Si demandé, Kitty affiche le journal mais sa fermeture ne tue pas `agy`.
- [ ] L'annulation interrompt uniquement la tâche ciblée.

### Validation

**Statut :** ☐ ✅ Réussi — ☐ ⚠️ Partiel — ☐ ❌ Échoué

**Notes :**

> 

---

## 5. Prosodie adaptative

### Objectif

Vérifier qu'ANO adapte son débit, son ton et la longueur de ses réponses au contexte et à l'état acoustique de l'utilisateur.

### Mode urgence

> 🗣️ « ANO, urgence : mon service principal vient de tomber, donne-moi les trois premières vérifications. »

### Mode concentration

> 🗣️ « Je suis en train de coder. Réponds de façon technique, courte et précise. »

### Mode fatigue

> 🗣️ « Je suis fatigué, explique-moi calmement ce qu'il reste à faire. »

### Détection d'une répétition

Dis deux fois exactement la même phrase :

> 🗣️ « Quelle commande affiche les services systemd en échec ? »

> 🗣️ « Quelle commande affiche les services systemd en échec ? »

### Résultat attendu

- [ ] Le mode urgence produit une réponse courte et vive.
- [ ] Le mode focus produit une réponse technique et sobre.
- [ ] Le mode fatigue produit une réponse plus calme.
- [ ] La deuxième réponse répétée est plus concise.
- [ ] Le journal peut indiquer le profil de prosodie sélectionné.

### Validation

**Statut :** ☐ ✅ Réussi — ☐ ⚠️ Partiel — ☐ ❌ Échoué

**Notes :**

> 

---

## 6. Second Brain et mémoire sémantique

### Objectif

Vérifier l'enregistrement d'informations, la recherche par le sens et la génération d'un graphe de connaissances.

### Enregistrement de souvenirs

> 🗣️ « Mémorise que la Peugeot du garage de Matam a été réparée samedi. »

> 🗣️ « Ajoute dans mon second cerveau que la commande de compression vidéo est ffmpeg moins i entrée moins crf 28 sortie point mp4. »

### Recherche sémantique

Utilise volontairement des mots différents :

> 🗣️ « Retrouve mon histoire de voiture. »

> 🗣️ « Quelle commande avions-nous mémorisée pour réduire la taille d'une vidéo ? »

### État et graphe

> 🗣️ « Affiche le statut de mon second cerveau. »

> 🗣️ « Génère un graphe Mermaid autour de mes projets et de mes commandes Linux. »

### Résultat attendu

- [ ] Le souvenir est enregistré durablement.
- [ ] « voiture » retrouve le souvenir contenant « Peugeot ».
- [ ] « réduire la taille » retrouve la commande de compression.
- [ ] Le statut indique les éléments réellement indexés.
- [ ] Le graphe Mermaid contient des relations pertinentes.
- [ ] La fermeture d'ANO n'est pas bloquée par l'enrichissement sémantique.

### Validation

**Statut :** ☐ ✅ Réussi — ☐ ⚠️ Partiel — ☐ ❌ Échoué

**Notes :**

> 

---

## 7. Navigation GPS parlée

### Objectif

Vérifier le calcul d'itinéraire, la réception du GPS du téléphone, les annonces vocales et le recalcul de route.

### Préparation

- [ ] ANO-Remote est connecté.
- [ ] La localisation du téléphone est autorisée.
- [ ] La position affichée correspond à la position réelle.

### Phrases de test

> 🗣️ « ANO, lance la navigation vers l'aéroport international de Conakry. »

> 🗣️ « Guide-moi à pied vers la pharmacie la plus proche. »

> 🗣️ « Quelle est la prochaine manœuvre ? »

> 🗣️ « Où en est mon itinéraire ? »

> 🗣️ « Arrête la navigation. »

### Résultat attendu

- [ ] Une carte et un itinéraire sont affichés.
- [ ] Le marqueur évolue avec le GPS du téléphone.
- [ ] Les noms de rues sont présents dans les instructions Valhalla.
- [ ] Le guidage ne reste pas bloqué sur l'étape de départ.
- [ ] Les annonces sont produites avant les manœuvres.
- [ ] Un écart prolongé entraîne un recalcul.
- [ ] « Arrête la navigation » termine proprement la session.

### Précaution

Effectue le premier essai à pied dans une zone sûre. Ne manipule pas ANO-GPT pendant la conduite.

### Validation

**Statut :** ☐ ✅ Réussi — ☐ ⚠️ Partiel — ☐ ❌ Échoué

**Notes :**

> 

---

## 8. Moteur d'habitudes

### Objectif

Vérifier l'apprentissage local des habitudes, les seuils anti-spam et la prise en compte d'un refus.

### Important

Cette fonctionnalité ne peut pas être validée en une seule session. Une suggestion nécessite au moins cinq occurrences réparties sur quatre jours distincts, dans le même créneau d'environ 30 minutes.

### Routine de test sur plusieurs jours

À un horaire similaire, prononce pendant au moins quatre jours :

> 🗣️ « Ouvre Visual Studio Code. »

ou :

> 🗣️ « Mets ma musique. »

### Suggestion attendue

Après apprentissage, ANO devrait proposer une formulation proche de :

> « Tu ouvres souvent Visual Studio Code vers cette heure-ci. Je le lance ? »

### Test du refus

> 🗣️ « Non, ne me le propose plus. »

### Résultat attendu

- [ ] Seules les actions réellement réussies sont apprises.
- [ ] Aucune suggestion n'apparaît avant le seuil requis.
- [ ] Une seule suggestion maximum est produite par jour.
- [ ] Un refus bloque la suggestion correspondante pendant environ 30 jours.
- [ ] Les habitudes restent stockées localement.

### Journal de suivi

| Jour | Heure | Action réussie | Suggestion reçue |
|---|---:|---|---|
| Jour 1 |  |  |  |
| Jour 2 |  |  |  |
| Jour 3 |  |  |  |
| Jour 4 |  |  |  |
| Jour 5 |  |  |  |

### Validation

**Statut :** ☐ ✅ Réussi — ☐ ⚠️ Partiel — ☐ ❌ Échoué

**Notes :**

> 

---

## 9. Voice ID et anneau de sécurité

### Objectif

Vérifier l'apprentissage de la voix et le blocage des actions sensibles lorsque le verdict vocal est absent, expiré, en cours ou inconnu.

### Inscription de la voix

Parle normalement à ANO pendant plusieurs phrases, puis dis :

> 🗣️ « Apprends ma voix. Mon nom est Anonymous. »

Si ANO demande d'autres échantillons, prononce deux ou trois phrases normales avant de répéter la commande.

### Vérification du statut

> 🗣️ « Quel est le statut de mon identification vocale ? »

### Test avec la voix du propriétaire

Ouvre une calculatrice, puis dis :

> 🗣️ « Ferme la calculatrice. »

### Test avec une autre personne

Demande à une autre personne de dire :

> 🗣️ « Ferme mon terminal. »

Utilise un terminal sans travail important non enregistré.

### Résultat attendu

- [ ] L'empreinte du propriétaire est enregistrée.
- [ ] La voix du propriétaire est reconnue.
- [ ] Une action sensible autorisée par le propriétaire fonctionne.
- [ ] Une voix inconnue ne peut pas exécuter l'action sensible.
- [ ] Un verdict en cours ou absent bloque également l'action.
- [ ] Un ancien verdict expiré ne sert jamais de passe-partout.
- [ ] Une action risquée peut encore demander une confirmation supplémentaire.

### Précaution

Ne teste jamais Voice ID avec une suppression de fichiers importants, une extinction forcée ou une commande irréversible.

### Validation

**Statut :** ☐ ✅ Réussi — ☐ ⚠️ Partiel — ☐ ❌ Échoué

**Notes :**

> 

---

## 10. Sparring Partner et simulateur d'entraînement

Le protocole détaillé, les variantes et les résultats attendus sont dans
[`docs/SPARRING_PARTNER.md`](../SPARRING_PARTNER.md).

### Test principal

> 🗣️ « ANO, entraîne-moi pour mon entretien technique de demain, niveau expert, en cinq questions. »

Répondre naturellement à chaque question. Placer volontairement une ou deux
hésitations (« euh », « en fait »), puis demander :

> 🗣️ « Termine l'entraînement et donne-moi mon rapport complet. »

### Résultat attendu

- ANO reste dans le rôle et pose une seule question à la fois.
- Il formule des objections adaptées au niveau expert.
- Le rapport compte les hésitations et donne des conseils de clarté.
- Le débit est affiché pour la voix et déclaré non mesuré pour une session texte.

### Validation

**Statut :** ☐ ✅ Réussi — ☐ ⚠️ Partiel — ☐ ❌ Échoué

**Notes :**

> 

---

## 11. Bouclier Anti-Distraction et gardien de dopamine

Le protocole complet et les règles précises sont dans
[`docs/BOUCLIER_ANTI_DISTRACTION.md`](../BOUCLIER_ANTI_DISTRACTION.md).

### Test principal

> 🗣️ « ANO, active le bouclier anti-distraction pendant 50 minutes pour terminer mon rapport, avec une pause de 10 minutes. »

Avec un navigateur exposant CDP, ouvrir successivement une vidéo YouTube
normale puis un lien YouTube Shorts.

### Résultat attendu

- La vidéo normale reste ouverte et seul le flux Shorts est bloqué.
- La commande d'état annonce l'objectif, le temps et le score de dispersion.
- « J'ai terminé le plan, enregistre ma progression » remet le compteur à zéro.
- « Restaure les onglets bloqués » rouvre le flux fermé.
- « Arrête le bouclier » désactive immédiatement observation et blocage.

### Validation

**Statut :** ☐ ✅ Réussi — ☐ ⚠️ Partiel — ☐ ❌ Échoué

**Notes :**

> 

---

## Fiche de résultats

| Nº | Fonctionnalité | Statut | Observations |
|---:|---|:---:|---|
| 1 | Interruption vocale instantanée | ☐ | |
| 2 | Perception d'écran et auto-debug | ☐ | |
| 3 | Voice DevSecOps et contrôle Linux | ☐ | |
| 4 | Agent Fantôme | ☐ | |
| 5 | Prosodie adaptative | ☐ | |
| 6 | Second Brain et mémoire sémantique | ☐ | |
| 7 | Navigation GPS parlée | ☐ | |
| 8 | Moteur d'habitudes | ☐ | |
| 9 | Voice ID et anneau de sécurité | ☐ | |
| 10 | Sparring Partner et analyse d'élocution | ☐ | |
| 11 | Bouclier Anti-Distraction et pauses Flow | ☐ | |

### Interprétation finale

- **11/11 réussis :** les fonctionnalités implémentées sont opérationnelles en usage réel.
- **9–10 réussis :** ensemble utilisable, avec quelques intégrations à corriger.
- **7–8 réussis :** validation partielle ; ne pas considérer la roadmap comme entièrement vérifiée.
- **Moins de 7 :** revoir la configuration, les dépendances et les journaux avant utilisation quotidienne.

### Informations de la session

- **Date du test :**
- **Version/commit :**
- **Machine :**
- **Microphone :**
- **Téléphone/ANO-Remote :**
- **Testeur :**

### Problèmes rencontrés

1. 
2. 
3. 

---

## Vérification automatisée complémentaire

Après les essais vocaux, lance également :

```fish
cd /home/anonymous/OUTILS/ANO-GPT
python -m pip check
python -m pytest -q -rsx
```

Résultat de référence après les dernières corrections :

```text
697 passed, 1 xfailed
```

Le test `xfail` documente une limite connue du filtre spectral classique pour le mélange pluie + ventilateur. Le VAD neuronal Silero couvre ce scénario.
