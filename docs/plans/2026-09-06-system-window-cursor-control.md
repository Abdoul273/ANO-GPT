# Plan d'Implémentation TDD — Contrôle Total Système, Fenêtres, Curseur et Saisie

**Date** : 2026-09-06  
**Objectif** : Permettre à l'assistant ANO-GPT d'écrire dans les fenêtres, contrôler les fenêtres (focus, plein écran, mode flottant, centrage, déplacement de bureau), déplacer le curseur au pixel près, et exécuter des commandes dans les applications au lancement (ex: « lance kitty et tape la commande codex »).

---

## Tâche 1 (TDD Red) : Écriture des tests pour `computer_control`
- **Fichier cible** : `tests/test_computer_control.py`
- **Comportements testés** :
  1. `type` avec texte et validation optionnelle `press_enter=True`.
  2. `type` avec ciblage `window` (vérifie que le focus est appelé avant la saisie).
  3. `move` avec déplacement curseur exact en coordonnées pixels via Hyprland Lua `hl.dsp.cursor.move`.
  4. Actions de fenêtrage : `fullscreen`, `float`, `center`, `close`, `focus`, `list_windows`.
  5. `press` et `hotkey` avec keycodes Linux / ydotool.
  6. Parsing local de commandes naturelles (« tape codex dans le terminal », « déplace la souris en 500, 300 », « plein écran »).
- **Vérification** : `pytest tests/test_computer_control.py` échoue sur les nouvelles méthodes non encore implémentées.

---

## Tâche 2 (TDD Green) : Implémentation Wayland / Hyprland dans `actions/computer_control.py`
- **Fichier cible** : `actions/computer_control.py`
- **Implémentations** :
  1. `_type_text` : `ydotool type` en priorité sous Wayland avec délai paramétrable, repli `wtype` puis clipboard paste (`wl-copy` + paste).
  2. `_press_key` & `_hotkey` : mapping des touches courantes vers keycodes Linux pour `ydotool key`, repli `wtype`.
  3. `_move` : positionnement absolu exact via Hyprland Lua IPC `hl.dsp.cursor.move({ x = x, y = y })`, repli `ydotool mousemove`.
  4. Nouvelles actions de fenêtres :
     - `fullscreen` via `hl.dsp.window.fullscreen()`.
     - `float` via `hl.dsp.window.float()`.
     - `center` via `hl.dsp.window.center()`.
     - `close` via `actions.window_instances.close_window`.
  5. Extension de l'action `type` : prise en charge des paramètres `window` / `title` (focus automatique préalable) et `press_enter` (envoi automatique de la touche Entrée après le texte).
  6. Enrichissement de `_parse_control_locally` pour détecter les expressions de frappe ciblée, curseur et fenêtres.
- **Vérification** : `pytest tests/test_computer_control.py` passe à 100%.

---

## Tâche 3 (TDD Red) : Écriture des tests pour `open_app` avec commande / saisie
- **Fichier cible** : `tests/test_open_app_command.py`
- **Comportements testés** :
  1. Extraction de commande dans la phrase naturelle : `_parse_open_command_locally("lance kitty et tape la commande codex")` extrait `app_name="kitty"` et `command="codex"`.
  2. Paramètre explicite `command` ou `type_text` transmis à `open_app`.
  3. Lancement d'un terminal avec commande directe ou frappe différée après stabilisation.
- **Vérification** : `pytest tests/test_open_app_command.py` échoue.

---

## Tâche 4 (TDD Green) : Implémentation de `command` et parsing composé dans `actions/open_app.py`
- **Fichier cible** : `actions/open_app.py`
- **Implémentations** :
  1. Ajout de `command: Optional[str] = None` et `type_text: Optional[str] = None` dans `open_app`.
  2. Mise à jour de `_parse_open_command_locally` pour reconnaître les requêtes composées (« lance X et tape Y », « ouvre X et exécute Y »).
  3. Exécution de la commande dans l'application :
     - Si terminal (`kitty`, `foot`, `alacritty`...) : injection dans l'argv ou saisie après focus.
     - Si application graphique standard : attente de fenêtre active, focus, puis saisie via `computer_control(action='type', text=command, press_enter=True)`.
- **Vérification** : `pytest tests/test_open_app_command.py` passe à 100%.

---

## Tâche 5 : Intégration Dispatcher, Prompt Système & Outil MCP
- **Fichiers cibles** :
  1. `core/tool_dispatcher.py` :
     - Mettre à jour `TOOL_DECLARATIONS` pour `open_app` (ajout propriété `command`).
     - Mettre à jour `TOOL_DECLARATIONS` pour `computer_control` (ajout actions `fullscreen`, `float`, `center`, `close`, et propriétés `window`, `press_enter`).
     - Mettre à jour `_execute_tool` / lambda pour passer `command` à `open_app`.
  2. `core/prompt.txt` :
     - Mettre à jour la section `computer_control` et les consignes pour `open_app`.
     - Préciser au modèle Gemini qu'il peut exécuter des commandes dans les fenêtres, déplacer le curseur, et contrôler l'ensemble des fenêtres.
  3. `anogpt_mcp.py` :
     - Déclarer `@mcp.tool() def computer_control(...)` avec toutes les actions.
- **Vérification** : Exécution de tests unitaires de non-régression sur le dispatcher et MCP.

---

## Tâche 6 : Vérification Finale & Preuves Tangibles (Phase 4)
- **Vérification** :
  1. Exécution de toute la suite de tests pertinents (`pytest tests/test_computer_control.py tests/test_open_app_command.py tests/test_mcp_tools.py`).
  2. Test réel d'écriture/focus et curseur sous Hyprland.
  3. Validation code de retour 0.
