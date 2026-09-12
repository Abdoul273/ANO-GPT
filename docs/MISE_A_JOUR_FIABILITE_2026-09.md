# ANO-GPT — mise à jour du socle, septembre 2026

Cette mise à jour renforce l'exécution des outils, la configuration, le contrôle
à distance et les diagnostics. Elle conserve les développements déjà présents
dans le dépôt. Elle ne remplace pas les modèles IA et ne prétend pas améliorer
leurs capacités de raisonnement sans mesure.

## Utilisation

Après redémarrage d'ANO-GPT, ouvrez les paramètres du dashboard puis
**Santé et performances → Vérifier maintenant**.

Depuis le dossier du projet, même lorsque l'assistant est arrêté :

```bash
./anogpt-ctl doctor
./anogpt-ctl doctor --json
python -m core.diagnostics
```

Le rapport vérifie Python, plusieurs dépendances locales, la présence d'une clé
Gemini dans un fichier JSON valide et l'espace disque. Il affiche les statistiques
des outils : appels, taux d'erreurs, moyenne, médiane, p95 et maximum. Le p95 est
le délai sous lequel se trouvent au moins 95 % des appels de la fenêtre retenue.
Ce rapport ne teste ni la validité distante de la clé, ni le microphone, ni les
services connectés. Un statut local `ok` n'atteste pas leur fonctionnement.

Le même rapport est exposé par `GET /api/diagnostics`, avec un jeton de session
obtenu par l'appairage existant. La sonde publique `/api/health` reste légère.

## Changements

- Validation des objets imbriqués, tableaux, valeurs autorisées et tailles
  maximales des champs déclarés ; traitement contrôlé des débordements numériques.
- File d'attente des actions limitée, délai d'attente de dix secondes par défaut,
  libération des places après annulation et vérification du disjoncteur après
  l'attente. Ces limites sont ajustables dans `ActionPolicy`.
- Vérification de l'interruption avant de commencer une action qui attendait.
- Respect explicite des résultats `ok: false` dans l'historique et les statistiques.
- Les dépassements de délai n'invitent plus à relancer aveuglément une action :
  un appel externe ou un thread peut continuer après l'annulation de l'attente.
- Fusion atomique des réglages, verrou partagé par chemin canonique entre threads,
  copies indépendantes du cache et protection contre l'écrasement d'un JSON corrompu.
- Un échec du coffre de secrets interrompt la sauvegarde sans effacer la clé du
  fichier. La synchronisation couvre les threads du même processus ; les éditeurs
  ou processus externes ne participent pas à ce verrou.
- Suppression du jeton d'accès fixe `local_ui_token`. Les clients doivent utiliser
  les sessions réellement créées par l'appairage.
- Commandes distantes limitées à 16 000 caractères, 64 commandes en attente et
  corps HTTP limité à 128 000 octets. Réponses 400, 413 ou 429 explicites.
- Les messages WebSocket de structure invalide sont rejetés sans fermer la
  connexion ; les commandes sont soumises à la même limite de file.
- Le dashboard affiche les refus du serveur et conserve la saisie lorsque
  l'envoi est impossible ou la connexion interrompue.
- Rotation du journal d'outils à 5 Mio avec une archive. Les nouveaux messages
  d'erreur persistants ne contiennent plus le texte brut des exceptions.
  Les archives antérieures ne sont pas réécrites.
- Lecture des statistiques bornée à 5 Mio et 20 000 entrées du journal courant ;
  lignes invalides et durées non finies ignorées. L'archive n'entre pas dans les
  statistiques de cette fenêtre.
- Détection des déclarations d'outils adaptée à leur emplacement actuel,
  y compris les déclarations ajoutées avec `append`.

## Validation effectuée

133 tests passent dans la sélection couvrant les changements et leurs intégrations :
runtime, dashboard, configuration, statistiques, outils, pools de threads, bus
d'événements, sélection vocale, routage, plugins, interruption, contexte, agenda
et téléphonie. Deux avertissements de la suite restent présents.

Compilation Python des modules modifiés, analyse syntaxique des quatre scripts
du dashboard et contrôle des espaces du diff réussis. `doctor --json` s'exécute
sur cette machine et détecte les dépendances locales vérifiées.

Le rendu visuel du nouveau panneau n'a pas été vérifié : aucun navigateur intégré
n'était disponible. Les appels réels aux fournisseurs, la voix en direct et
l'application Android sur appareil n'ont pas été testés. L'application en cours
n'a pas été redémarrée automatiquement.

Commande de reproduction :

```bash
python -m pytest \
  tests/test_reliability_upgrade.py tests/test_dashboard_reliability.py \
  tests/test_action_runtime.py tests/test_tool_registry.py \
  tests/test_thread_pool.py tests/test_event_bus.py tests/test_voice_selection.py \
  tests/test_llm_brain_routing.py tests/test_tool_bridge.py \
  tests/test_plugin_registry.py tests/test_speaker_security.py \
  tests/test_interrupt_recovery.py tests/test_live_model_policy.py \
  tests/test_context_probe.py tests/test_calendar_service.py \
  tests/test_phone_calls.py tests/test_phone_media_channels.py -q --disable-warnings
```
