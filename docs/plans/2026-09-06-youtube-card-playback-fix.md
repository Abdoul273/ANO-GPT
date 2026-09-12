# Plan d'Implémentation TDD : Correction et Amélioration du Lecteur Vidéo YouTube dans la Carte Dédiée

**Date** : 2026-09-06  
**Statut** : 🔄 EN COURS  
**Périmètre** : `ui/media/video_playback.py`, `ui/media/video_hub.py`, `ui/window/media_host.py`, `actions/youtube_video.py`, `tests/`

---

## Tâche 1 : Tests TDD pour le Template HTML, la Suppression de l'Erreur 152 et l'Autoplay (`tests/test_video_hub.py`)
- **Objectif** : Écrire les tests unitaires vérifiant :
  1. Le template `_player_html` utilise l'origine et le base URL adaptés (`origin: 'http://localhost'`, pas d'usurpation youtube.com provoquant l'erreur 152).
  2. La présence de la configuration autoplay Chromium `--autoplay-policy=no-user-gesture-required`.
  3. La robustesse des fonctions de contrôle JavaScript `ctl` et du polling `_poll_progress`.
- **Fichiers** : `tests/test_video_hub.py`
- **Cycle TDD** :
  - Écrire les assertions de test.
  - Lancer `python3 -m pytest tests/test_video_hub.py` -> échec vérifié (Phase RED).

---

## Tâche 2 : Correction du Template HTML, de l'Origin et de l'Autoplay (`ui/media/video_playback.py`, `ui/media/video_hub.py`)
- **Objectif** :
  1. Dans `ui/media/video_playback.py` : mettre à jour `_player_html` avec `origin: 'http://localhost'`, charger `setHtml` avec `QUrl("http://localhost/")` au lieu de `https://www.youtube.com/`, sécuriser les contrôles JS `ctl(action, value)` et le polling `_poll_progress`.
  2. Dans `ui/media/video_playback.py` / `ui/media/video_hub.py` : injecter le flag Chromium `QTWEBENGINE_CHROMIUM_FLAGS="--autoplay-policy=no-user-gesture-required"` avant l'initialisation de WebEngine si non présent.
- **Cycle TDD** :
  - Exécuter `python3 -m pytest tests/test_video_hub.py` -> succès vérifié (Phase GREEN).

---

## Tâche 3 : Tests TDD et Correction du Clic sur Carte et Parsing Vocale (`ui/window/media_host.py`, `actions/youtube_video.py`, `tests/test_youtube_control.py`)
- **Objectif** :
  1. Écrire des tests dans `tests/test_youtube_control.py` pour le parsing des commandes de sélection numérotées ("ouvre le résultat YouTube numéro 1", "lis la 2ème vidéo", "mets la vidéo 3").
  2. Vérifier l'échec initial (Phase RED).
  3. Dans `actions/youtube_video.py` : enrichir `_parse_youtube_command_locally` pour reconnaître les requêtes de sélection avec index.
  4. Dans `ui/window/media_host.py` : dans `_on_video_selected(index)`, lancer directement la vidéo sélectionnée via `self.play_video(self._video_hub._videos[index], self._video_hub._videos)` sans latence ni réinterprétation textuelle.
- **Cycle TDD** :
  - Exécuter `python3 -m pytest tests/test_youtube_control.py` -> succès vérifié (Phase GREEN).

---

## Tâche 4 : Vérification Finale E2E & Preuves Tangibles
- **Objectif** :
  1. Exécuter l'ensemble des suites de tests vidéo et YouTube.
  2. Valider le comportement du lecteur WebEngine réel via un script de test complet (chargement, onReady, état PLAYING, progression du timer).
  3. Vérifier l'absence totale de régression.
