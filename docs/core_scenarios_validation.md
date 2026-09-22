# Validation des parcours principaux — 22 septembre 2026

## Corrections

- Saisie : une fenêtre introuvable interrompt l'action ; un échec de saisie
  n'envoie pas Entrée ; un échec d'Entrée ou de collage n'est plus annoncé
  comme une commande validée.
- Rappels : l'annulation numérique suit l'ordre chronologique affiché.
  Arrêt du timer et du service systemd, puis lecture de leur état réel.
  En cas d'échec, le registre et les fichiers sont conservés pour réessayer.
- Chrome : `Page.evaluate` utilise une borne asyncio (son API n'accepte pas
  `timeout`). Les erreurs de création/sonde de page entraînent au maximum une
  récupération. La récupération d'une connexion CDP ne ferme pas le contexte
  utilisateur.
- Mémoire : aucune modification nécessaire pour les scénarios vérifiés.

## Vérifications exécutées

109 tests distincts réussis : suites computer_control, browser_policy,
reminder_system, reminder_parsing, semantic_memory, knowledge_graph,
vector_memory_batching, open_app_command, shell_exec_safety,
workspace_navigation_precision et verified_action_results.

Tests réels supplémentaires, avec données temporaires :

1. Kitty lancé sur le bureau actif par shell_exec ; `codex --version` exécuté
   dans le terminal ; sortie vérifiée dans un fichier témoin.
2. Information enregistrée dans une base temporaire du graphe mémoire, puis
   retrouvée depuis un autre processus Python.
3. Timer systemd utilisateur créé pour le lendemain, vérifié actif, annulé
   via l'action, puis vérifié inactif. Unités de test nettoyées.
4. Google Chrome headless isolé : champ rempli, bouton cliqué, résultat DOM
   vérifié ; onglet fermé puis remplacé par une page valide via BrowserSession.

## Portée

Ces essais ne mesurent pas la reconnaissance vocale, le choix d'outil par le
modèle, la pertinence des embeddings locaux ni toutes les interactions avec
les sites web. La preuve Codex concerne son exécution et sa version, pas une
session interactive complète. La mémoire personnelle et les rappels de
l'utilisateur n'ont pas servi de données de test. Redémarrer ANO-GPT pour
charger les corrections dans l'application en cours.
