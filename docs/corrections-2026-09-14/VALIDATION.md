# Corrections de fiabilité du 14 septembre 2026

## Périmètre

Corrections réalisées sur le code présent au début de l'intervention, avec une copie de validation `/tmp/anogpt-fix-T4f4EJ`. Les tests sont lancés avec Qt hors écran. Le programme principal n'a pas été démarré et aucune recette avec envoi réel de SMS, appels ou commande système destructive n'a été effectuée.

La configuration personnelle `config/azure_catalog.json`, la roadmap et l'audit préexistants sont conservés. Des fichiers d'interface ont été créés/modifiés en parallèle pendant l'intervention ; ils ne font pas partie de la copie validée. Le fichier global indiqué par le projet, `~/.Codex/AGENTS.md`, est absent.

## Changements

- **Audio Live** : rejeter avant les sous-titres et le transport les paquets PCM âgés de plus d'une seconde, d'une ancienne époque audio, capturés alors que la session est interrompue, ou consommés pendant la parole de l'assistant. Respecter également le micro PC coupé ; le canal téléphone conserve son bouton indépendant. Le canal vidéo reste indépendant. Le garde-fou du callback micro est conservé.
- **Changement de voix** : restaurer l'accès à la sauvegarde de configuration pour permettre la reconnexion. Restaurer aussi l'export du paramètre de tranche audio utilisé par le contrôle de latence ; aucun réglage matériel n'est changé.
- **SMS** : supprimer du schéma le drapeau d'auto-autorisation du modèle. Toute demande passe par la confirmation existante. Le callback tourne dans le thread de confirmation et programme la coroutine sur la boucle audio, sans y bloquer une attente synchrone. En cas de délai dépassé, annuler le futur et annoncer un statut inconnu, sans affirmer que le SMS n'est pas parti.
- **Résultats confirmés** : restituer le résultat réel du callback sans le préfixer d'une déclaration automatique de succès. Une exception déclenche également une notification. Le garde-fou anti-rejeu ne prétend plus qu'une opération déjà déclenchée a nécessairement réussi.
- **Accès distant** : authentifier les routes de position/navigation ; refuser les corps JSON d'authentification de type incorrect avec HTTP 400 ; invalider les jetons persistants, les sessions HTTP et les caches de clés lors d'une révocation, fermer les trois types de WebSockets et recontrôler leur autorisation après réception.
- **Reconnaissance visuelle** : supprimer le cache de rapport basé uniquement sur la question. Une demande identique vérifie désormais la scène actuelle et tient compte d'un visage qui vient d'être appris. Le test de recherche de prix demande explicitement un prix ; un autre test garantit qu'une identification simple ne déclenche pas de recherche web.
- **Validation des actions** : soumettre un texte récupéré depuis un champ inconnu aux mêmes contraintes de longueur et d'énumération qu'un champ déclaré. Les erreurs de délai ne prétendent plus garantir l'absence d'effet réel.
- **Mémoire documentaire** : supprimer les fragments d'un document dont l'extraction réussit avec un contenu vide. Une erreur de lecture/extraction, ou un extracteur PDF absent, conserve la version précédente. L'état vide est mémorisé pour éviter une réindexation inutile.
- **Politique Chrome** : le suivi TikTok utilise `core.browser_policy.chrome_binary`, refuse l'absence de Chrome et ne désactive plus explicitement son sandbox. L'installation ne télécharge plus tous les navigateurs Playwright.
- **Arch** : la traduction d'une demande de rafraîchissement étrangère utilise `checkupdates` au lieu de produire `pacman -Sy`. Cela ne lance aucune mise à niveau ; `checkupdates` doit être disponible sur la machine.
- **Diagnostic vocal manuel** : l'import/collecte du script ne lance plus de micro, de synthèse ou de changement de routage. Les sous-processus sont bornés ; le processus de parole est lancé via `action_kit` et son groupe est arrêté à la fin.
- **Visibilité des erreurs** : 22 échecs auxiliaires auparavant silencieux dans les chemins session, outils, confirmation, reconnaissance et caméra distante produisent désormais une trace. Le plafond de dette du test n'a pas été augmenté.

## Vérification

- État initial : **7 échecs, 1 661 réussites, 23 ignorés, 1 échec attendu** ; 144,11 s.
- Première validation ciblée isolée : **152 réussites**, 53,93 s.
- Suite complète isolée : **1 697 réussites, 23 ignorés, 1 échec attendu**, 4 avertissements, 157,51 s ; aucun échec inattendu.
- Ruff sur la copie isolée : **aucune erreur**. `git diff --check` sur le dossier partagé : aucun défaut d'espacement.
- Après cette suite, un dernier ajustement des notifications de confirmation et la suppression d'un appel shell non borné dans le diagnostic manuel ont été validés par **44 tests ciblés réussis**, 18,42 s (confirmation humaine, SMS, e-mails, contrats de sous-processus et visibilité des erreurs).
- Ruff sur le dossier partagé a signalé **21 erreurs** au dernier relevé, toutes dans les nouveaux fichiers d'interface/aperçu/test ajoutés en parallèle (`welcome_hud`, `hud_sound`, aperçu et test associés). Ces fichiers ne sont pas modifiés par ce lot ; ce nombre peut évoluer avec le travail concurrent.

Commandes de vérification dans la copie :

```bash
ruff check .
QT_QPA_PLATFORM=offscreen timeout 900s python -m pytest -q --disable-warnings --tb=short
```

## Limites et suite

Ce lot ne signifie pas que toutes les fiches de la roadmap du 13 septembre sont résolues. Restent notamment à traiter séparément : identité TLS Android, isolation réelle de l'exécution Python et des plugins, couverture du chemin MCP par les protections d'action, accusés de réception SMS durables, transaction complète d'indexation et cohérence des effacements mémoire. Aucun nouveau taux de reconnaissance vocale, gain CPU, score d'intelligence ou taux de succès réel des services externes n'a été mesuré.

Les tests ignorés et l'échec attendu ne constituent pas des fonctions validées. La recette sur micro/haut-parleurs, Google Chrome connecté et téléphone Android reste nécessaire ; la suite hors écran ne la remplace pas. Les modifications d'interface concurrentes demandent leur propre validation avant de considérer tout le dossier partagé comme validé.

## Correctif Hyprland et détection d'appel — 07:29

Un essai réel sur Hyprland 0.56.2 a reproduit le refus de la commande historique
`hyprctl dispatch workspace 2`. Le chemin `hypr_control` avec
`hl.dsp.focus({ workspace = "2" })` a ensuite basculé réellement vers le bureau
2, confirmé par `hyprctl activeworkspace -j`, puis le bureau 1 a été restauré.
`run_shell` intercepte désormais la forme historique lorsqu'elle est fournie
directement par le modèle et la route vers cette action vérifiée.

La fausse annonce « différée (appel) » venait du flux PipeWire passif
`Echo-Cancel Capture`, créé par ANO-GPT lui-même et dépourvu d'identité de
processus. Ce flux est maintenant exclu, tandis qu'un flux Chrome actif reste
détecté comme un véritable appel. Sur les flux actifs de la session, le portier
renvoie maintenant une chaîne vide. Les messages identiques de report sont
aussi limités à une occurrence par cinq minutes.

Validation ciblée : Ruff sans erreur sur les fichiers concernés, compilation
Python réussie, **73 tests réussis** (navigation Hyprland, shell, contrôle du
bureau, proactivité et interruption vocale), et `git diff --check` sans erreur.
