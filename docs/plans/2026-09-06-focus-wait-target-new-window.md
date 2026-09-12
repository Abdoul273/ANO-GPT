# Plan d'Implémentation TDD — Attente Fonctionnelle et Ciblage de la Nouvelle Fenêtre

**Date** : 2026-09-06  
**Objectif** : Corriger le ciblage de la saisie lors du lancement d'application avec commande (« lance kitty et tape codex »). Attendre 3 à 5 secondes pour que la fenêtre soit pleinement prête, et cibler obligatoirement CETTE même fenêtre nouvelle (par son adresse Wayland), sauf si une fenêtre spécifique a été demandée.

---

## Tâche 1 (TDD Red) : Écriture des tests pour le ciblage par adresse et la temporisation
- **Fichiers cibles** :
  - `tests/test_computer_control.py` : test de `_focus_window` ciblant une adresse `address:0x...`.
  - `tests/test_open_app_command.py` : test de `open_app` vérifiant que :
    1. La nouvelle fenêtre est identifiée par son adresse `address:0x...`.
    2. Le bureau de la fenêtre est activé et la fenêtre est focalisée.
    3. Le délai de préparation (3.5s) est appliqué avant la frappe.
    4. `computer_control` est appelé avec `window=f"address:{addr}"` (ciblant CETTE même fenêtre).
    5. Si `target_window` est explicitement fourni, cette cible personnalisée est respectée.
- **Vérification** : `pytest tests/test_open_app_command.py` échoue sur les nouvelles assertions (Red).

---

## Tâche 2 (TDD Green) : Implémentation de la détection, de l'attente et du focus
- **Fichiers cibles** :
  - `actions/computer_control.py` :
    - Mise à jour de `_focus_window(title)` pour supporter les sélecteurs `address:0x...` et les correspondances directes sur l'adresse hexadécimale.
  - `actions/open_app.py` :
    - Implémentation de `_wait_for_new_window(app_name, before_addrs, timeout=6.0)`.
    - Temporisation fonctionnelle de 3,5s (`wait_ready=3.5` par défaut).
    - Basculement de bureau si la fenêtre est née sur un autre bureau.
    - Focus explicite sur l'adresse de la nouvelle fenêtre.
    - Appel de `computer_control` avec `window=f"address:{new_addr}"` et `press_enter=True`.
- **Vérification** : `pytest tests/test_open_app_command.py tests/test_computer_control.py` passe à 100% (Green).

---

## Tâche 3 : Vérification Finale & Preuves Tangibles (Phase 4)
- **Vérification** :
  - Exécution de toute la suite de tests (`pytest tests/test_computer_control.py tests/test_open_app_command.py tests/test_mcp_tools.py tests/test_hypr_orchestrator.py`).
  - Validation du code de retour 0.
