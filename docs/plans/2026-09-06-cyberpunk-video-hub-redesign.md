# Plan d'Implémentation TDD : Refonte Cyberpunk du Lecteur Vidéo & Galerie ANO-GPT

**Date** : 2026-09-06  
**Statut** : 🔄 EN COURS  
**Périmètre** : `ui/media/video_hub.py`, `ui/media/video_widgets.py`, `ui/media/video_playback.py`, `tests/test_video_hub.py`

---

## Tâche 1 : Tests TDD pour les Nouveaux Composants et Contrôles Cyberpunk (`tests/test_video_hub.py`)
- **Objectif** : Écrire les assertions validant :
  1. Présence du dock de contrôle cyberpunk avec ses boutons (`previous`, `back`, `toggle`, `forward`, `next`, `fullscreen`, `mute`, `results`).
  2. Présence du slider de volume avec affichage du pourcentage et bouton muet interactif.
  3. Mise à jour dynamique du bouton lecture/pause (`▶ LECTURE` / `⏸ PAUSE`).
  4. Rendu des cartes `VideoResultCard` avec badge de durée flottant et métadonnées cyber.
- **Cycle TDD** :
  - Ajouter les tests unitaires.
  - Exécuter `python3 -m pytest tests/test_video_hub.py` (Phase RED sur les nouveaux éléments).

---

## Tâche 2 : Refonte Cyberpunk du Lecteur Vidéo & Dock Flottant (`ui/media/video_hub.py`, `ui/media/video_playback.py`)
- **Objectif** :
  1. Construire le dock de contrôle cyberpunk horizontal avec style glassmorphism et touches néon cyan.
  2. Mettre en valeur le bouton central Play/Pause avec changement d'icône/label selon l'état de lecture.
  3. Ajouter la jauge de volume cyberpunk avec toggle muet instantané et affichage du pourcentage.
  4. Styliser la timeline HUD et les horodatages en police micro-HUD.
  5. Harmoniser le cadre vidéo avec contour néon et placeholder stylisé.
- **Cycle TDD** :
  - Exécuter `python3 -m pytest tests/test_video_hub.py` (Phase GREEN).

---

## Tâche 3 : Modernisation des Cartes Résultats Vidéo (`ui/media/video_widgets.py`)
- **Objectif** :
  1. Sublimer `VideoResultCard` avec effet glassmorphism profond, bordure cyan 1px, coins biseautés.
  2. Ajouter le badge de durée flottant superposé sur la miniature.
  3. Intégrer les étiquettes de métadonnées chaîne/vues avec typographie cyber propre.
- **Cycle TDD** :
  - Exécuter `python3 -m pytest tests/test_video_hub.py` (Phase GREEN).

---

## Tâche 4 : Vérification Globale & Preuves Tangibles
- **Objectif** :
  1. Exécuter l'ensemble de la suite de tests (`python3 -m pytest tests/test_video_hub.py tests/test_youtube_control.py -v`).
  2. Valider le rendu graphique réel et la réactivité des contrôles.
  3. Documenter les résultats dans `walkthrough.md`.
