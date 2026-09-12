# Plan d'Implémentation TDD : YouTube sous Chrome avec Contrôle Total & Gmail OAuth Chrome

**Date** : 2026-09-05  
**Statut** : ✅ **ENTIÈREMENT IMPLÉMENTÉ ET VALIDÉ** (66/66 tests réussis, 0 régression)  
**Sujet** : Ouverture et contrôle total de YouTube dans Google Chrome, résilience de la recherche YouTube (fallback scraping), et forçage de Google Chrome pour l'autorisation OAuth de Gmail.

---

## Tâche 1 : Tests TDD pour le Lancement Chrome, la Résilience YouTube et le Contrôle Média (`tests/test_youtube_chrome_routing.py`) — ✅ FAIT
- **Double Revue** : Porte 1 validée, Porte 2 validée.
- **Résultats** : 4 tests TDD rigoureux créés et validés au vert.

---

## Tâche 2 : Résilience YouTube & Ouverture dans Google Chrome (`actions/youtube_video.py` & `core/youtube_service.py`) — ✅ FAIT
- **Double Revue** : Porte 1 validée, Porte 2 validée.
- **Résultats** : Fallback direct par scraping HTML/JSON ajouté, détection et ouverture automatique de Google Chrome, routage des commandes vocales vers `media_control` lorsque la vidéo est dans le navigateur.

---

## Tâche 3 : Forçage de Google Chrome pour Gmail OAuth & Priorité Système (`core/email_service.py`, `actions/desktop_apps.py`, `actions/media_control.py`) — ✅ FAIT
- **Double Revue** : Porte 1 validée, Porte 2 validée.
- **Résultats** : Détection du binaire Google Chrome et passage de `browser=chrome_bin` à `flow.run_local_server(...)` avec message prompt `{url}`, alias navigateurs réordonnés (Chrome > Firefox), priorisation Chrome absolue dans `_focus_browser()` et `_browser_mpris()`.

---

## Tâche 4 : Vérification Globale & Preuves Tangibles — ✅ FAIT
- **Tests unitaires et d'intégration** : 66 tests exécutés, 66 réussis (100% GREEN) en 9.33s.
- **Test direct YouTube** : `search_youtube('kanda bongo')` résout en direct les vidéos (`CZDNoV0DQ5I`, `7iHJu3gsXDE`) sans aucune erreur.
- **Test repli scraping** : `_search_youtube_scraping('kanda bongo')` résout les vidéos même en cas d'inaccessibilité de `yt-dlp`.
- **Test détection binaire** : `/usr/bin/google-chrome-stable` détecté et priorisé sur tout le système.

---

## Tâche 1 : Tests TDD pour le Lancement Chrome, la Résilience YouTube et le Contrôle Média (`tests/test_youtube_chrome_routing.py`)

- **Fichiers** : `tests/test_youtube_chrome_routing.py` (création)
- **Interfaces** :
  - *Consomme* : `actions.youtube_video.youtube_video`, `core.youtube_service.search_youtube`, `core.email_service.GmailService`
  - *Produit* : Suite de tests automatisés validant :
    1. L'ouverture préférentielle de Google Chrome pour les vidéos YouTube.
    2. La résilience de `search_youtube` : si `yt-dlp` échoue ou est absent, le fallback de scraping direct extrait la vidéo sans lever d'exception bloquante.
    3. Le routage de `_youtube_control` vers `media_control` lorsque la vidéo est lue dans le navigateur Chrome.
    4. La transmission de `browser="google-chrome-stable"` (ou `google-chrome`) dans `InstalledAppFlow.run_local_server`.
- **Étapes TDD** :
  1. Écrire les 4 cas de tests unitaires avec mocks explicites de `subprocess.Popen`, `subprocess.run` et `requests`.
  2. Lancer `pytest tests/test_youtube_chrome_routing.py` et constater les échecs (Phase RED).

---

## Tâche 2 : Résilience YouTube & Ouverture dans Google Chrome (`actions/youtube_video.py` & `core/youtube_service.py`)

- **Fichiers** :
  - `core/youtube_service.py` (modification)
  - `actions/youtube_video.py` (modification)
- **Interfaces** :
  - *Consomme* : `shutil.which`, `subprocess.Popen`, `actions.media_control.media_control`
  - *Produit* : Résolution garantie de vidéo YouTube + lancement sous Google Chrome + routage des commandes vocales vers `media_control`.
- **Détails d'implémentation** :
  1. Dans `core/youtube_service.py` :
     - Si `yt-dlp` n'est pas disponible ou échoue, basculer sur `search_youtube_scraping` (extraction regex de `videoId`, titre, chaîne depuis `https://www.youtube.com/results?search_query=...`) pour renvoyer des `YouTubeResult` valides au lieu d'interrompre l'utilisateur.
  2. Dans `actions/youtube_video.py` :
     - Ajouter une fonction `_open_in_chrome(url: str) -> bool` recherchant `google-chrome-stable`, `google-chrome` ou `chromium`, avec fallback `xdg-open`.
     - Dans `_open_selected` : permettre l'ouverture directe dans Google Chrome lorsque demandé ou en mode lecture web, et ne jamais bloquer avec « Aucun navigateur externe n'a été ouvert ».
     - Dans `_youtube_control` : si le lecteur intégré Qt n'est pas actif (ou si la vidéo a été lancée dans Chrome), transférer systématiquement l'action à `media_control` pour piloter Chrome via MPRIS et `wtype`.
- **Validation** :
  - Lancer `pytest tests/test_youtube_chrome_routing.py tests/test_youtube_control.py` et constater le passage au vert (GREEN).

---

## Tâche 3 : Forçage de Google Chrome pour Gmail OAuth & Priorité Système (`core/email_service.py`, `actions/desktop_apps.py`, `actions/media_control.py`)

- **Fichiers** :
  - `core/email_service.py` (modification)
  - `actions/desktop_apps.py` (modification)
  - `actions/media_control.py` (modification)
- **Interfaces** :
  - *Consomme* : `google_auth_oauthlib.flow.InstalledAppFlow`, `actions.media_control._focus_browser`
  - *Produit* : Ouverture exclusive de Google Chrome lors de `connecte Gmail` et focalisation prioritaire de Chrome.
- **Détails d'implémentation** :
  1. Dans `core/email_service.py` :
     - Résoudre le binaire Chrome disponible (`google-chrome-stable`, `google-chrome`, `chromium`).
     - Passer l'argument `browser=chrome_bin` dans `flow.run_local_server(...)` pour forcer l'ouverture du consentement Google dans Chrome.
  2. Dans `actions/desktop_apps.py` :
     - Inverser l'ordre des alias pour `"navigateur"`, `"browser"`, `"web"`, `"internet"` afin de placer `google-chrome-stable` et `google-chrome` en tête de liste devant Firefox.
  3. Dans `actions/media_control.py` :
     - Mettre à jour `_focus_browser()` et `_browser_mpris()` pour prioriser les instances Chrome (`google-chrome-stable`, `google-chrome`, `chromium`) sur Wayland/Hyprland.
- **Validation** :
  - Lancer `pytest tests/test_youtube_chrome_routing.py tests/test_email_service.py` et constater le succès.

---

## Tâche 4 : Vérification Globale & Preuves Tangibles

- **Fichiers** : `tests/`
- **Actions** :
  - Exécuter la suite complète :
    `pytest tests/test_youtube_chrome_routing.py tests/test_youtube_control.py tests/test_email_service.py tests/test_media_control.py -v`
  - Tester en condition réelle la commande :
    `python3 -c "from actions.youtube_video import youtube_video; print(youtube_video({'action': 'play', 'query': 'kanda bongo'}))"`
  - Vérifier l'absence de régression.
