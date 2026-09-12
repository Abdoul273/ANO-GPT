# Plan d'Implémentation TDD : Refonte Cyberpunk du Mini Orb ANO-GPT

**Date** : 2026-09-05  
**Sujet** : Refonte du Mini Orb (Suppression du fond noir/div, moteur holographique FUI cyberpunk, anneaux 3D, égaliseur radial, motes d'énergie, polissage de la bulle compagnon).

---

## Tâche 1 : Suite de Tests TDD pour le Mini Orb (`tests/test_mini_orb.py`)

- **Fichiers** : `tests/test_mini_orb.py` (création)
- **Interfaces** :
  - *Consomme* : `ui.orb.mini_orb.MiniOrbOverlay`, `ui.orb.arc_core.HudCanvas`, `ui.orb.glsl_orb.GLSLOrbWidget`.
  - *Produit* : suite de tests automatisés pytest.
- **Étapes TDD** :
  1. Écrire le test vérifiant :
     - La transparence native (aucun disque de fond sombre opaque couvrant le widget).
     - Le rendu sans crash sous tous les états (`idle`, `listening`, `thinking`, `acting`, `speaking`, `error`).
     - La réaction dynamique aux paramètres `volume`, `energy` et `_ws`.
     - La tolérance aux dimensions variables (y compris W < 2 ou H < 2).
     - La synchronisation avec un mock ou une instance `HudCanvas` / `GLSLOrbWidget`.
  2. Lancer `python3 -m pytest tests/test_mini_orb.py` et constater l'échec ou le statut initial.

---

## Tâche 2 : Moteur de Rendu Holographique Cyberpunk (`ui/orb/mini_orb.py`)

- **Fichiers** : `ui/orb/mini_orb.py` (modification)
- **Interfaces** :
  - *Consomme* : `source._ws`, `source._volume`, `source._energy`, `source._PALETTES`, palettes d'accent de `ui.styles.theme.C`.
  - *Produit* : `MiniOrbOverlay` avec rendu vectoriel cyberpunk sans fond noir.
- **Détails d'implémentation** :
  - Retirer le disque sombre opaque (`plate = QRadialGradient...`).
  - Implémenter le cœur incandescent avec plasma radial multi-couches et respiration harmonique.
  - Implémenter les 3 anneaux gyroscopiques inclinés en rotation différentielle selon `spin` et l'état.
  - Implémenter le réticule FUI (arcs segmentés, graduations angulaires et crochets technologiques).
  - Implémenter l'égaliseur radial audio dynamique (barres néon réagissant à `volume` et fréquence).
  - Implémenter le système de particules/motes orbitales légères.
- **Validation** :
  - Lancer `python3 -m pytest tests/test_mini_orb.py` et constater le passage au vert (GREEN).

---

## Tâche 3 : Intégration & Polissage du CompanionOrb (`ui/orb/companion.py`)

- **Fichiers** : `ui/orb/companion.py` (modification)
- **Interfaces** :
  - *Consomme* : `MiniOrbOverlay`, `ui.styles.theme.C`.
  - *Produit* : `CompanionOrb` avec bulle éphémère glassmorphic cyberpunk et gestion de région propre.
- **Détails d'implémentation** :
  - Ajuster le style de `_bubble` pour un look glassmorphic cyberpunk (fond translucide sombre fumé, bordure néon fine, typographie nette).
  - Vérifier la géométrie et la transmission des clics.
- **Validation** :
  - Lancer `python3 -m pytest tests/test_camera_overlay.py tests/test_mini_orb.py tests/test_glsl_orb.py`.
  - Valider le code retour 0.

## Reprise complète après reproduction sur le bureau

Le rectangle noir a été reproduit sous XWayland avec le backend `xcb` utilisé
par `main.py`. Le buffer exporté par Qt avait pourtant un alpha nul : les tests
hors écran seuls ne détectaient pas le défaut. Réappliquer `QWidget.setMask`
sur le nouveau compagnon suffisait à reproduire le rectangle noir à l'écran.

Le compagnon peint maintenant directement le nouveau réacteur et son message
sur une seule surface ARGB, sans widget enfant ni conteneur stylé. Sous X11,
`input_shape.py` utilise uniquement `XShapeCombineRectangles(ShapeInput)` pour
laisser passer les clics dans les espaces vides, sans toucher à `ShapeBounding`.
Sous Wayland natif, le masque d'entrée est confié au `QWindow`. Les dimensions
et le titre restent compatibles avec les règles de placement existantes.

Vérification réelle reproductible, sans lancer les services de l'assistant :

```sh
QT_QPA_PLATFORM=xcb python3 scripts/preview_mini_orb.py
```

Ce contrôle capture le résultat du compositeur avec `grim` sur un damier,
compare les pixels transparents au fond de référence, vérifie le réacteur
visible, l'affichage / effacement du message, l'absence de masque visuel X11
et les zones cliquables natives. Les captures vont dans `/tmp/ano-orb-check`.
