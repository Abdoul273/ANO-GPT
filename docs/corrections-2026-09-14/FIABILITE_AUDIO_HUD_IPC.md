# Fiabilité audio, accueil et pont MCP — 14 septembre 2026

## Périmètre et machine

Intervention sur le dossier de travail existant, avec conservation des modifications antérieures. Machine observée : EndeavourOS, Intel i5-6300U, 2 cœurs physiques / 4 threads, 11 Gio de RAM. Aucune mise à niveau système, modification de clé, publication ou modification de Codex dans ce lot. Le fichier global annoncé `~/.Codex/AGENTS.md` est absent.

## Corrections de ce lot

- `core/audio_engine.py` : chargement de SciPy, découverte de sortie et préparation HRTF déportés sur le worker audio réservé. Un rapport de gel du 14 septembre à 10:37:49 montrait cet import sur la boucle asyncio. La préparation est désormais couverte par le nettoyage de fin de tâche ; un test bloque volontairement la découverte et vérifie que l'annulation reste possible. Trois erreurs de fermeture/référence audio disposent désormais de traces. Le filtrage half-duplex du micro et du consommateur Live reste conservé.
- `core/spatial_audio.py` : les coordonnées situées derrière l'auditeur gardent leur azimut ; le dénominateur de `atan2` n'est plus artificiellement rendu positif. Six positions arrière/latérales sont vérifiées par aller-retour géométrique.
- `ui/sound/hud_sound.py` : génération et lecture des fichiers sonores hors du constructeur Qt ; un seul effet en cours, sans accumulation de threads. Utilisation d'un flux de sortie dédié au lieu de `sounddevice.play`, qui intervient sur les fonctions globales de lecture/enregistrement. Une erreur de création de thread libère le verrou et ne remonte pas dans Qt. Les bips simultanés sont ignorés pendant l'effet en cours.
- `ui/dialogs/welcome_hud.py` : chronomètre monotone et transition basée sur le temps écoulé, indépendamment du nombre de frames rendues. Tests adaptés aux méthodes actuelles, sortie audio simulée et ressources temporaires.
- `core/tool_packs.py` : rattachement de `show_country_info` au noyau carte/position ; tous les outils déclarés sont à nouveau couverts par le classement.
- `core/ipc.py` : une réponse sans fin de ligne, coupée ou expirée n'est plus renvoyée comme un succès. Le budget de réception est global et monotone ; le message d'erreur indique que le résultat de la commande est inconnu. Aucun rejeu automatique n'est ajouté.
- `core/tool_bridge.py` : les arguments invalides `[]`, `""`, `0`, `false` et `null` ne deviennent plus silencieusement `{}`. Les handlers ne sont pas exécutés pour ces requêtes.
- Sept erreurs Ruff corrigées dans les fichiers d'accueil/aperçu/tests ; commentaire d'installation SpeexDSP limité à Arch.

## Validation

Copie finale de vérification : `/tmp/anogpt-verified-Z7lkuV`, incluant les fichiers suivis et les nouveaux fichiers non ignorés, sans `config/azure_catalog.json` ni les bases/identifiants ignorés. Qt hors écran, aucun démarrage de l'assistant principal, aucun SMS ou appel réel.

- Première suite sur l'état initial : trois tests en échec (dette de silence, classement des outils, cycle de vie de l'accueil), puis crash natif à la sortie, code 139. Le crash n'a pas été attribué avec certitude à une cause unique.
- Suite Python complète après corrections audio/HUD/classement : **1 774 réussites, 23 ignorés, 1 échec attendu**, quatre avertissements, 95,86 s, code de sortie 0.
- Après ajout des corrections IPC/arguments : **88 tests ciblés réussis**, 8,31 s.
- Après les derniers ajustements de message de délai et de panne de thread HUD : **26 tests ciblés réussis**, 2,54 s.
- `ruff check .` : aucune erreur ; `git diff --check` : aucun défaut d'espacement. La compétence Ruff Quality a guidé ce contrôle et le nettoyage ciblé, sans reformater le dépôt.
- Accueil rendu hors écran en 1280 × 720 et image inspectée : rendu réussi. Ce contrôle ne mesure pas les FPS sur Hyprland.
- Android : dépendances résolues depuis le cache avec `flutter pub get --offline` ; `flutter analyze --no-pub` sans erreur, 5,9 s ; **20 tests réussis** avec `flutter test --no-pub --reporter expanded`.

Les nombres de tests ciblés recouvrent des tests de la suite complète : ils ne doivent pas être additionnés pour annoncer un total distinct. Les derniers changements IPC ont été validés de manière ciblée après la suite complète.

## Limites explicites

Ce lot ne certifie pas l'absence de tout défaut et ne clôture pas toute la roadmap. Restent notamment les points déjà documentés sur l'identité TLS Android, l'isolation de l'exécution Python/plugins, les protections d'actions sur tous les chemins MCP, les accusés de réception SMS durables et les transactions mémoire. Aucune de ces fonctions n'est déclarée corrigée ici.

La recette micro/haut-parleurs, Chrome connecté et téléphone réel reste à faire. Aucun gain chiffré de CPU, de latence vocale ou de reconnaissance n'est annoncé. Les tests ignorés et l'échec attendu ne sont pas des fonctionnalités validées. L'application en cours n'a pas été redémarrée ; les modifications seront chargées au prochain lancement.
