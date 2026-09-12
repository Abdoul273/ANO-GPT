# Plan d'Implémentation TDD : Confirmation Humaine sur Carte HUD & Encadrement Écran Temps Réel

**Date** : 2026-09-05  
**Sujet** : Carte de confirmation de sécurité interactive sur le rail de droite avec feedback vocal et déblocage, couplée au système de ciblage temps réel précis sans cadre fantôme.

---

## Tâche 1 : Tests TDD pour la Carte de Confirmation Humaine (`tests/test_human_confirmation_card.py`)

- **Fichiers** : `tests/test_human_confirmation_card.py` (création)
- **Interfaces** :
  - *Consomme* : `core.human_confirmation`, `ui.panels.rich_card_system.GlassCard`, `ui.panels.rich_card_system.CardManager`
  - *Produit* : Suite de tests automatisés validant :
    1. L'apparition d'une carte de confirmation dédiée avec style de sécurité (`Theme.NEON_AMBER`, icône bouclier/sécurité, `pinned=True`).
    2. Le bouton [Confirmer] : exécution de l'action, état visuel de désactivation, résolution avec `accepted=True`.
    3. Le bouton [Annuler] : annulation immédiate, libération du verrou `_pending`, résolution avec `accepted=False`.
    4. La fermeture via la croix [X] (`dismiss()`) : résolution propre avec `accepted=False` pour ne jamais laisser le système bloqué.
    5. Le retour vocal / notification de l'assistant lors de la confirmation et de l'annulation.
- **Validation** :
  - Lancer `pytest tests/test_human_confirmation_card.py` et vérifier l'échec initial (RED).

---

## Tâche 2 : Implémentation de la Carte de Confirmation sur le Rail de Droite
- **Fichiers** :
  - `ui/panels/rich_card_system.py` (modification)
  - `core/phone_relay.py` (modification)
  - `core/human_confirmation.py` (modification)
  - `ui/window/positions.py` (modification)
- **Détails d'implémentation** :
  - Dans `ui/panels/rich_card_system.py` :
    - Traiter `"confirmation"` en tant que carte haute priorité :
      - Accent : `Theme.NEON_AMBER` (`#ffb300`).
      - Icône : `"shield-alert"`.
      - Catégorie : `"SÉCURITÉ"`.
      - `pinned = True` (ne disparaît jamais par auto-dismiss).
      - Bouton primaire [ ✓ Confirmer ] : vert néon, désactivé au premier clic avec libellé `Validation...`.
      - Bouton secondaire [ ✗ Annuler ] : rouge néon `Theme.RED`.
      - Connexion de `dismiss` / fermeture pour résoudre `resolve(token, False)` si non encore résolue.
    - Forcer l'insertion en haut de pile pour une visibilité immédiate.
  - Dans `ui/window/positions.py` :
    - S'assurer que lors d'une carte de confirmation, `self._card_scroll.show()` et `self._card_scroll.raise_()` sont invoqués.
  - Dans `core/human_confirmation.py` :
    - Ajouter le support d'un callback de notification vocale / feedback `notify: Callable[[str], None]`.
    - Annoncer la prise en compte de la confirmation et le résultat de l'exécution, ou l'annulation.
- **Validation** :
  - Lancer `pytest tests/test_human_confirmation_card.py` et constater le passage au vert (GREEN).

---

## Tâche 3 : Tests TDD pour le Ciblage et l'Encadrement Temps Réel (`tests/test_smart_screen_pointer.py`)
- **Fichiers** : `tests/test_smart_screen_pointer.py` (création)
- **Interfaces** :
  - *Consomme* : `ui.visual_pointer.VisualPointerOverlay`, `core.tool_dispatcher.ToolDispatcher`
  - *Produit* : Tests validant :
    1. Résolution de widget interne (ex: `"confirmation"`, `"carte de confirmation"`, `"cmd"`, `"music"`) :
       - Si le widget n'est pas affiché (`isVisible() == False`), aucun cadre n'est tracé dans `active_annotations`, et un message d'absence clair est retourné.
       - Si le widget est affiché, ses coordonnées globales exactes `mapToGlobal` sont calculées et un cadre néon `HighlightAnnotation` est tracé à sa géométrie exacte.
    2. Détection en temps réel pour éléments d'écran externes :
       - Si la cible n'est pas vue sur la capture d'écran, aucun cadre n'est tracé et un message de non-détection est renvoyé.
       - Si la cible est localisée, conversion précise des coordonnées normalisées en pixels réels.
- **Validation** :
  - Lancer `pytest tests/test_smart_screen_pointer.py` (RED).

---

## Tâche 4 : Implémentation du Résolveur de Cibles et de l'Encadrement Temps Réel
- **Fichiers** :
  - `ui/visual_pointer.py` (modification)
  - `core/tool_dispatcher.py` (modification)
  - `core/prompt.txt` (modification)
- **Détails d'implémentation** :
  - Dans `ui/visual_pointer.py` :
    - Ajouter `resolve_widget_geometry(target_name: str)` inspectant les widgets Qt de l'application (`MainWindow`, `CardManager`, cartes actives).
    - Vérifier `widget.isVisible() and widget.width() > 0 and widget.height() > 0`.
    - Méthode `point_on_target(target: str, ...)` : n'affiche de cadre QUE si la cible est réellement résolue et visible.
  - Dans `core/tool_dispatcher.py` :
    - `_agent_point_on_screen` : si la description ou la cible est un nom textuel (ex: "carte de confirmation"), interroger le résolveur de widget. Si externe, capturer l'écran et vérifier la présence via vision avant d'encadrer.
  - Dans `core/prompt.txt` :
    - Consignes strictes pour le modèle : ne jamais inventer de coordonnées numériques ; passer le nom de la cible pour vérification en temps réel.
- **Validation** :
  - Lancer `pytest tests/test_smart_screen_pointer.py` (GREEN).

---

## Tâche 5 : Résolution Conversationnelle des Confirmations en Attente (Voix & Chat)
- **Fichiers** :
  - `main.py` (modification)
- **Détails d'implémentation** :
  - Intercepter les réponses d'approbation (« oui », « confirme », « vas-y », etc.) ou de rejet (« non », « annule », « stop », etc.) lorsque `human_confirmation.current()` est actif.
  - Résoudre directement la confirmation en cours avec notification vocale immédiate de JARVIS.
- **Validation** :
  - Test d'intégration unitaire simulant une saisie utilisateur "oui" pendant une confirmation pendante.

---

## Tâche 6 : Vérification Finale & Double Revue (Validée)
- Exécution de l'ensemble des suites de tests du projet :
  `pytest tests/test_human_confirmation.py tests/test_human_confirmation_card.py tests/test_visual_pointer.py tests/test_smart_screen_pointer.py tests/test_rich_card_system.py tests/test_shell_exec_safety.py`
- Résultat : **40/40 tests passés avec succès (100% GREEN, 0 échec)** en 2.28s.
- Respect strict des critères d'acceptation :
  1. Carte de confirmation dorée affichée à droite avec boutons interactifs [Confirmer] et [Annuler].
  2. Résolution vocale et textuelle immédiate ("oui" / "non").
  3. Annulation propre en cas de fermeture [X] ou clic sur [Annuler].
  4. Encadrement d'écran en temps réel avec zéro cadre fantôme : refus net d'encadrer si le widget/élément n'est pas physiquement visible à l'écran.

