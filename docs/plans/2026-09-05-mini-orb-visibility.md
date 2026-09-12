# Plan d'Implémentation TDD : Agrandissement et Visibilité du Mini Orb ANO-GPT

**Date** : 2026-09-05  
**Sujet** : Épaississement des traits vectoriels, amplification lumineuse/contraste, zoom d'échelle interne et agrandissement du composant compact.

---

## Tâche 1 : Tests TDD pour la Visibilité, le Contraste et les Marges (`tests/test_mini_orb.py`)

- **Fichiers** : `tests/test_mini_orb.py` (modification)
- **Interfaces** :
  - *Consomme* : `ui.orb.mini_orb.MiniOrbOverlay`, `ui.orb.mini_orb.paint_reactor`
  - *Produit* : Nouveaux tests de non-régression validant la densité photonique accrue (contraste/visibilité) et le respect des marges strictes (pas de débordement).
- **Étapes TDD** :
  1. Ajouter un test `test_mini_orb_enhanced_stroke_density_and_contrast` vérifiant que :
     - La densité lumineuse totale en mode idle avec les traits épaissis est significativement supérieure à l'ancienne densité filiforme (> 15% d'augmentation de luminance).
     - La cage interne et les arcs orbitaux présentent un signal alpha moyen accru.
  2. Ajouter une vérification stricte que les coins et bords extérieurs immédiats (points de contrôle de transparence (2, 2), (118, 2), (2, 118), (118, 118), (60, 2), (2, 60)) restent strictement transparents (`alpha == 0`).
  3. Lancer `pytest tests/test_mini_orb.py` et constater les résultats.

---

## Tâche 2 : Refonte du Moteur Vectoriel de l'Orbe (`ui/orb/mini_orb.py`)

- **Fichiers** : `ui/orb/mini_orb.py` (modification)
- **Interfaces** :
  - *Consomme* : `source._ws`, `source._volume`, `source._energy`, `source._PALETTES`
  - *Produit* : `paint_reactor` avec traits épaissis (1.3px à 2.0px), saturation lumineuse accrue, noyau agrandi et zoom interne `/ 92.`.
- **Détails d'implémentation** :
  - Ajuster l'échelle de dessin : `scale = min(bounds.width(), bounds.height()) / 92.` pour occuper pleinement le champ visible sans écrêter.
  - Épaissir les arcs orbitaux de `0.8px` à `1.8px` avec halos multi-passes `((width + 3.5, 30), (width + 1.5, 70), (width, 240))`.
  - Épaissir les graduations de télémétrie de `0.8px` à `1.5px` avec crans allongés et opacités franches (`(210 if major else 120) + int(energy * 45)`).
  - Épaissir la cage sphérique (équateur à `1.5px` alpha 200, méridiens à `1.2px` alpha 110-220, parallèles à `1.1px` alpha 130).
  - Agrandir le noyau central (`2.8 + volume * 1.5`), son halo radial (`glow(22 + volume * 6, core, 175 + int(energy * 80))`) et les satellites (`2.0 - j * .15`).
  - Épaissir le ruban de plasma central (`(5.5, 35), (2.8, 85), (1.5, 255)`).
- **Validation** :
  - Lancer `pytest tests/test_mini_orb.py` et constater le passage au vert (GREEN).

---

## Tâche 3 : Agrandissement de l'Incrustation dans les Vues Immersives (`ui/window/media_host.py` & `tests/test_camera_overlay.py`)

- **Fichiers** :
  - `ui/window/media_host.py` (modification)
  - `tests/test_camera_overlay.py` (modification)
- **Interfaces** :
  - *Consomme* : `self._fullscreen_surface_visible()`, `self.centralWidget()`
  - *Produit* : Géométrie `_mini_orb` agrandie de 108-136px à 128-160px.
- **Détails d'implémentation** :
  - Dans `_sync_fullscreen_orb` :
    ```python
    size = max(128, min(160, int(min(W, H) * 0.22)))
    margin = 16
    ```
  - Mettre à jour `tests/test_camera_overlay.py` pour valider `128 <= mini.width() <= 160`.
- **Validation** :
  - Lancer `pytest tests/test_camera_overlay.py` et valider le code retour 0.

---

## Tâche 4 : Vérification Globale & Contrôle Visuel

- **Fichiers** : `tests/`
- **Actions** :
  - Exécuter la suite complète de tests impactés :
    `pytest tests/test_mini_orb.py tests/test_camera_overlay.py tests/test_glsl_orb.py`
  - Effectuer un rendu visuel témoin de l'orbe dans `/tmp/preview_mini_orb.png` pour confirmer le résultat esthétique.
