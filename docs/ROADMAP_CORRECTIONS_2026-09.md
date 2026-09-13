# ANO-GPT — Feuille de route des corrections (audit du 13 septembre 2026)

Audit complet du dépôt : journaux (`logs/anogpt.jsonl`, deux rapports de gel),
statistiques d'outils (`memory/tool_usage.jsonl`), suite de tests (1 581 tests),
analyse statique (pyflakes, AST), diff non commité et lecture du code critique
(boucle audio, orbe Qt, répartiteur d'outils, pools de threads).

Chaque fiche donne : le **symptôme** vécu, la **cause** dans le code, ce que
l'erreur **freine** dans l'application, la **correction** à appliquer, et le
**gain** attendu. Les priorités vont de P0 (à faire tout de suite, la voix en
dépend) à P3 (hygiène, dette).

Résumé chiffré de l'état actuel :

| Indicateur | Valeur |
|---|---|
| Tests | 1 570 passent, **4 échouent**, 6 ignorés |
| Gels détectés (boucle audio > 3 s) | 2 en une session de 2 minutes |
| Stalls de pool signalés | 2 (WebP 3,1 s ; SQLite commit 15,3 s) |
| Appels HTTP sans délai | 26 sites |
| Handlers d'exception muets | 598 (plafond du test : 584) |
| Imports inutilisés | ≈ 2 080 |
| Outil le plus lent (p95) | `consult_brain` 100 s, `email_control` 45 s, `find_nearby` 45 s |
| Taux d'échec le plus élevé | `live_auto_debug` 29 échecs / 31 appels |

---

## P0 — La voix se fige (à corriger en premier)

### 1. Gel de la boucle audio pendant la peinture de l'orbe

- **Symptôme** : `logs/freeze-20260913-014309.txt` et `-014339.txt`. La boucle
  asyncio (micro, voix) ne bat plus pendant 3 s. Le thread principal est dans
  `arc_paint.py:127` (`_draw_particle_cloud`), la boucle audio attend le
  sélecteur. Deux gels en 30 s juste après la voix chuchotée
  « Un instant, Monsieur ».
- **Cause** : Qt et asyncio partagent le GIL. Le diff non commité monte le
  nuage de **240 à 300 particules**, le budget actif de 240 à 300 et la taille
  des points de 2,20 à 2,65. Chaque image projette 300 points, construit les
  filaments, trois `drawPoints` plus une aura large. À 25 images/s pendant la
  voix, le thread Qt monopolise le GIL au moment où la synthèse et le micro en
  ont besoin. Le régulateur `_desired_interval` ne réagit qu'à la moyenne
  glissée (`0.1`) et ne descend jamais sous 33 ms : il réagit trop tard.
- **Ce que ça freine** : coupures dans la voix, micro sourd pendant 3 s,
  transcription qui perd des mots, réponses « à retardement ». C'est le
  problème que l'utilisateur ressent le plus.
- **Correction** :
  1. Revenir à `_PARTICLE_N = 240`, `_ACTIVE_PARTICLE_BUDGET = 240`,
     `_IDLE_PARTICLE_BUDGET = 160` (ou garder 300 uniquement en état `idle`).
  2. Dans `_desired_interval`, ajouter un plancher dur pendant la voix :
     si `_ws in ("speaking", "listening")` et `cost > 12 ms`, forcer 60 ms.
  3. Lier l'orbe au battement de cœur : quand `freeze_watch` note que la boucle
     audio a plus de 0,8 s de retard, passer l'orbe en `_FRAME_MS_MAX` pendant
     2 s (méthode `set_low_power(True)` déjà existante).
  4. Mesurer avec `tests/test_orb_particle_cloud.py` et un profil `py-spy top`
     pendant une phrase parlée ; objectif : thread Qt < 30 % d'un cœur.
- **Gain** : plus aucun gel > 1 s, voix continue, micro réactif ; le test
  existant retrouve ses valeurs d'origine.

### 2. Commit SQLite de 15 s sur le pool `disk-io` à chaque tour de parole

- **Symptôme** : `[ANO-POOL STALL DETECTED] ano-disk-io-record-conversation-turn`
  15,26 s dans `vector_memory.py:944 conn.commit()`.
- **Cause** : `save()` ouvre une transaction qui calcule l'embedding ONNX
  (384 d) **dans la transaction**, insère dans `vec_entries`, extrait les
  triplets RDF, puis commit. `memory.db` (30 Mo), `personal_rag.db` (276 Mo)
  et `personal_index.db` (44 Mo) partagent le disque ; avec `journal_mode=WAL`
  sans `synchronous=NORMAL`, chaque commit force un fsync complet. Le pool
  `disk-io` n'a que 2 workers : un commit lent bloque l'indexation et le
  journal d'outils derrière lui.
- **Ce que ça freine** : la mémoire longue durée arrive en retard, les tours
  suivants s'empilent dans le pool, le chien de garde crie, et l'écriture
  concurrente des autres bases (RAG, habitudes) attend.
- **Correction** :
  1. `PRAGMA synchronous=NORMAL` et `PRAGMA busy_timeout=5000` à l'ouverture
     (`vector_memory.py:651`).
  2. Calculer l'embedding **avant** `with conn:`, hors transaction.
  3. Regrouper les tours : file en mémoire vidée toutes les 2 s ou à 5 tours,
     un seul commit.
  4. `stall_timeout=30.0` explicite sur cette tâche pour ne plus polluer le
     journal, et passer `max_workers` du pool `disk-io` à 3.
- **Gain** : commit < 50 ms, plus de stall, mémoire disponible immédiatement
  pour le tour suivant.

### 3. Compression WebP de 3 s sur `compute-light`

- **Symptôme** : stall `ano-compute-light-scrn` 3,14 s dans
  `screen_consciousness.py:399 compress_webp`.
- **Cause** : `Image.save(format="WEBP", method=4)` sur une capture plein
  écran, puis boucle de réduction jusqu'à 40 Ko. `method=4` est lent sur deux
  cœurs ; la boucle recompresse jusqu'à cinq fois. Le pool `compute-light`
  a `min(4, cpu)` = 2 workers, donc un WebP lent bloque aussi la FFT du
  waveform.
- **Ce que ça freine** : la veille visuelle prend un cœur entier pendant 3 s,
  le GIL est disputé, la voix bégaie quand l'écran change.
- **Correction** : `method=0` ou `1`, réduire à 1 024 px **avant** la première
  compression, une seule passe de qualité (q=70) et abandonner si > 40 Ko
  plutôt que reboucler ; `stall_timeout=8.0`.
- **Gain** : compression < 300 ms, un seul essai, pool libre pour l'audio.

### 4. `RuntimeError: Lock is not acquired` sur la livraison vidéo

- **Symptôme** : `Task exception was never retrieved` dans
  `_deliver_video_generation` → `session_manager.py:636 async with lock`.
- **Cause** : la tâche de fond attend un verrou `asyncio.Lock` créé par une
  **session précédente**. À la reconnexion, `main.py:1705` remplace
  `self._turn_submit_lock` par un nouveau verrou ; l'ancien est relâché par
  une tâche qui ne l'avait jamais pris, ou la boucle qui l'a créé n'existe
  plus. Même risque pour toutes les tâches longues (image, recherche
  approfondie, TikTok coach) qui survivent à une reconnexion.
- **Ce que ça freine** : le résultat d'une génération de 2 minutes est perdu
  sans être annoncé ; l'utilisateur pense que l'outil est cassé.
- **Correction** :
  1. Ne créer le verrou qu'une seule fois dans `__init__`, jamais à la
     reconnexion (le verrou n'est pas lié à la session WebSocket).
  2. Dans `_submit_text_turn`, capturer `lock = self._turn_submit_lock` et
     vérifier que `self.session` n'a pas changé après acquisition ; sinon
     renvoyer `False` et remettre le résultat dans `_deferred_context`.
  3. Attacher `add_done_callback` à toutes les tâches de fond pour journaliser
     l'exception au lieu de « never retrieved ».
- **Gain** : livraison fiable des résultats longs même après une coupure réseau.

### 5. Délais d'outils qui bloquent le micro (half-duplex)

- **Symptôme** : `web_search` annulé à 20 s ; `consult_brain` p95 100 s ;
  `email_control` et `find_nearby` p95 45 s ; `deep_think` max 90 s ;
  `music_control` max 45 s.
- **Cause** : pendant qu'un outil tourne, le tour Live reste ouvert et le micro
  n'envoie rien. `web_search` a `timeout_s=15` dans `action_runtime` mais son
  hedge interne attend `_GEMINI_TIMEOUT + 8` : le budget interne dépasse le
  budget externe, donc c'est toujours le répartiteur qui tue. `consult_brain`
  n'a pas de politique dédiée (défaut 20 s) mais le client HTTP attend 100 s.
- **Ce que ça freine** : l'assistant paraît mort, l'utilisateur répète, le
  second tour arrive en plein retour d'outil (source des codes 1007).
- **Correction** :
  1. Règle : **timeout interne = timeout de politique − 2 s**. Passer la valeur
     de `policy_for(name).timeout_s` aux actions via `parameters["_budget_s"]`.
  2. Pour `consult_brain`, `deep_think`, `generate_document`, `download_music`,
     `email_control` : basculer en mode « accusé immédiat + livraison différée »
     comme `_deliver_image_generation` (réponse en < 2 s, résultat poussé
     ensuite par `_submit_text_turn`).
  3. `find_nearby` : cache des géocodages (déjà partiel) et refus immédiat si
     aucune position < 5 min au lieu d'attendre 45 s.
- **Gain** : aucun tour > 20 s, micro rendu en moins de 2 s, plus de doubles
  tours.

---

## P1 — Bugs fonctionnels visibles

### 6. `live_auto_debug` échoue 29 fois sur 31

- **Symptôme** : `tool_execution_failed` en ≈ 1,9 s à chaque appel.
- **Cause probable** : `core/auto_debug.py:608` exige une réponse structurée
  de GPT-5.6 Terra ; sans clé Azure valide ou avec un modèle qui ne renvoie
  pas le JSON attendu, l'outil lève `ValueError`. L'erreur est absorbée en
  `tool_execution_failed` sans détail dans `tool_usage.jsonl`.
- **Ce que ça freine** : l'auto-réparation vocale (« corrige ») ne marche
  jamais ; chaque appel coûte 2 s de silence.
- **Correction** : journaliser la vraie exception dans `tool_failure`
  (`observability.py:204`), replier sur le cerveau configuré
  (`resolve_brain_provider()`) quand Azure n'est pas disponible, accepter une
  réponse texte libre en repli du JSON.
- **Gain** : l'auto-réparation devient utilisable ; on saura enfin pourquoi
  elle échoue.

### 7. Quatre tests en échec

| Test | Cause | Correction |
|---|---|---|
| `test_tool_packs` | `whatsapp_control` déclaré (diff non commité) mais dans aucun paquet | l'ajouter au paquet « messagerie » dans `core/tool_packs.py` |
| `test_human_confirmation_card` | `_on_text_command` lit `self.ui.muted` avant tout garde ; le mock n'a pas `ui` | `ui = getattr(self, "ui", None)` puis `getattr(ui, "muted", False)` |
| `test_durability` | `tiktok_coach.py:45-46` contient des identifiants Gemini en dur | importer `FAST_MODEL`/`BALANCED_MODEL` depuis `core/model_catalog` |
| `test_error_visibility` | 598 handlers `except: pass` contre 584 autorisés | traiter la fiche 12 (14 handlers à journaliser) |

- **Ce que ça freine** : la suite ne peut plus servir de filet ; on ne sait pas
  si une régression est nouvelle.
- **Gain** : suite verte, commit du diff en attente possible.

### 8. Clé `"to"` dupliquée dans la déclaration `email_control`

- **Symptôme** : pyflakes `tool_dispatcher.py:1073 / 1080` : « dictionary key
  'to' repeated ». La première définition (destinataire pour `send`) est
  écrasée par la seconde (filtre de recherche).
- **Ce que ça freine** : le modèle lit une description de filtre pour le champ
  qu'il doit remplir avec le destinataire ; envois manqués, 8 échecs sur 49.
- **Correction** : renommer le filtre en `to_filter` (alias dans
  `_ARG_ALIASES`) et garder `to` pour le destinataire.
- **Gain** : moins d'échecs d'envoi et de « paramètre manquant ».

### 9. `send_message` : argument requis manquant malgré l'alias

- **Symptôme** : `Arguments invalides pour send_message — paramètres requis
  manquants : message_text` alors que les clés reçues incluent `message_text`.
- **Cause** : la valeur était vide (`""`) ; `prepare` traite `""` comme absent.
  Le modèle a passé le texte dans un autre champ (`list_instances`) non déclaré,
  donc supprimé silencieusement.
- **Ce que ça freine** : message WhatsApp/Telegram non envoyé, erreur lue à
  voix haute.
- **Correction** : quand un champ requis est vide et qu'un champ inconnu de
  type texte a été supprimé, le proposer dans le message d'erreur ; ajouter
  les alias `content`, `body`, `msg` → `message_text`.
- **Gain** : un appel sur deux récupéré sans second tour.

### 10. Navigateur Playwright fermé : `Target page, context or browser has been closed`

- **Symptôme** : `browser_control` échoue après une fermeture manuelle de
  Chrome ; le reaper (`browser_control.py:1048`) ne réinitialise pas le
  contexte.
- **Correction** : détecter `browser.is_connected() is False` avant chaque
  action et relancer `_init_playwright()` ; borner le relaunch à 1 essai.
- **Gain** : le contrôle navigateur survit à la fermeture de Chrome.

### 11. Caelestia : capture d'écran attendue 30 s (diff non commité)

- Le diff ramène le délai à 7 s et fait de la présence du fichier la source
  de vérité. Correct. À compléter : `stall_timeout` cohérent dans
  `_POLICIES["capture_control"]` et un test qui simule un CLI qui reste
  attaché. Puis commiter.

---

## P2 — Robustesse, fuites et latence de fond

### 12. 598 handlers d'exception muets

- **Symptôme** : `except Exception: pass` partout ; 563 blocs identiques.
- **Ce que ça freine** : les erreurs réelles (fiche 6, 9, 10) sont invisibles ;
  le diagnostic prend des heures.
- **Correction** : script `scripts/audit_silent_handlers.py` (le test en a déjà
  la logique) ; remplacer par `logger.debug("…", exc_info=True)` dans
  `core/` et `actions/`, garder `pass` seulement dans le callback micro et la
  peinture Qt (chemins temps réel). Objectif : < 400 en deux semaines, puis
  abaisser le plafond du test à chaque passage.
- **Gain** : chaque panne laisse une trace ; l'auto-réparation a de la matière.

### 13. 26 appels HTTP sans délai

- **Fichiers** : `zapzap_controller.py:52`, `elevenlabs_voice.py:30,75`,
  `close_app_smart.py` (7), `computer_control.py` (5), `hypr_orchestrator.py`
  (4), `shell_exec.py` (2), `music.py` (2), `capture.py`, `app_control.py`.
- **Ce que ça freine** : un service local muet (ZapZap, Hyprland IPC, ElevenLabs
  en panne) bloque l'action jusqu'au délai du répartiteur (20 à 120 s), donc
  le micro.
- **Correction** : `timeout=(2, 5)` par défaut sur un client partagé dans
  `action_kit` (`kit.http()`), interdit d'appeler `requests` directement
  (test `test_subprocess_contract` à étendre au HTTP).
- **Gain** : aucune action ne dépasse 5 s sur un service local mort.

### 14. Subprocess hors `action_kit` (63 sites)

- `llm_client.py` (9), `audio_router.py` (6), `agent_brain.py` (3),
  `system_ops.py` (3), `tts.py`, `screen_capture.py`, etc.
- **Ce que ça freine** : processus zombies non tués par groupe, pas de délai,
  cache Hyprland contourné (lectures figées).
- **Correction** : passer par `kit.run` / `kit.launch_detached` ; étendre
  `test_subprocess_contract.py` à `core/`.
- **Gain** : plus de processus orphelins, délais uniformes.

### 15. `asyncio.get_event_loop()` depuis des threads

- `navigation.py:1035,1136`, `dashboard/server.py:838`, `main.py:1669`,
  `tool_dispatcher.py:2908`, `audio_engine.py:930`.
- **Ce que ça freine** : sous Python 3.14, l'appel hors boucle lève
  `RuntimeError` ou crée une boucle fantôme ; la diffusion dashboard de la
  navigation ne part pas.
- **Correction** : `self._loop` (déjà stocké dans `run()`) +
  `run_coroutine_threadsafe` ; dans les coroutines, `get_running_loop()`.
- **Gain** : état navigation synchronisé sur le téléphone, plus d'avertissement
  de dépréciation.

### 16. Avertissement Gemini « Direct use of AFC in generate_content »

- **Cause** : `llm_client.py:1465` passe `tools=` à `generate_content` ; le
  SDK active l'appel automatique de fonctions et, faute de callables, avertit
  8 fois par session.
- **Correction** : `automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)`
  dans `GenerateContentConfig`.
- **Gain** : journal propre, pas de double exécution d'outil par le SDK.

### 17. Threads non-daemon et arrêt propre

- `stt.py:56`, `thread_pool.py:485,1061`, `audio_capture.py:326`,
  `hypr_focus.py:113`, `screen_consciousness.py:654`, `ui/dialogs/audio.py:462`,
  `player_ipc.py:95`, `semantic_enricher.py:140`, `game_updater.py:598`.
- **Ce que ça freine** : à la fermeture, `runtime_thread.join(8.0)` expire, le
  processus reste vivant ; l'utilisateur doit tuer ANO-GPT à la main ; le
  verrou `anogpt-<uid>.lock` et le socket traînent.
- **Correction** : `daemon=True` partout sauf les workers de pool, et un
  `stop()` appelé depuis `shutdown_all`.
- **Gain** : fermeture en < 2 s, relance immédiate possible.

### 18. `ui/core/metrics.py` : `pynvml.nvmlInit()` à chaque tick (1,5 s)

- **Cause** : `_get_gpu` tente `import pynvml` + `nvmlInit()` puis `ctypes.CDLL`
  toutes les 1,5 s sur une machine sans NVIDIA ; l'échec est mis en cache
  seulement pour Windows.
- **Ce que ça freine** : deux chargements de bibliothèque ratés par cycle,
  temps GIL gaspillé sur deux cœurs.
- **Correction** : mémoriser l'échec (`_nvml_ok = False`) sur Linux aussi ;
  passer l'intervalle à 3 s.
- **Gain** : ≈ 1 % de CPU rendu à la voix.

### 19. Bases de mémoire : 276 Mo de RAG jamais compactées

- `personal_rag.db` 276 Mo, `personal_index.db` 44 Mo, `memory.db` 30 Mo ;
  `storage_maintenance` signale mais ne compacte jamais.
- **Correction** : `VACUUM` planifié à la fermeture si > 20 % de pages libres
  et si l'utilisateur est absent depuis 10 min ; `PRAGMA auto_vacuum=INCREMENTAL`
  à la création.
- **Gain** : lectures RAG plus rapides, moins d'I/O disque en concurrence avec
  la fiche 2.

### 20. Dépôt : 60 Mo d'images et binaires suivis

- `background/*.png` (8 fichiers, 60 Mo), `libdf.so` en double (racine et
  `core/`), `jarvis_binaural_test.wav`, `docs/archive/ui.py.pre-split.bak`,
  `DeepFilterLib-0.5.6.tar.gz`, `deepfilternet-0.5.6-py3-none-any.whl`.
- **Ce que ça freine** : clone lent, push lent, GitHub proche de la limite.
- **Correction** : Git LFS pour `background/`, supprimer le `libdf.so` racine,
  ajouter les archives au `.gitignore`.
- **Gain** : dépôt sous 10 Mo hors LFS.

---

## P3 — Hygiène et dette

### 21. ≈ 2 080 imports inutilisés, 21 variables mortes, 6 f-strings sans champ

- **Correction** : installer `ruff` (`pipx install ruff`), `ruff check --fix
  --select F401,F841,F541`, puis ajouter `ruff` au `pre-commit` et à la CI.
- **Gain** : démarrage plus rapide (moins de modules chargés), lecture plus
  claire.

### 22. Sites à corriger un par un

| Fichier | Problème | Correction |
|---|---|---|
| `core/multimodal_vision.py:149` | `global _working_vision_model` jamais assigné | retirer le `global` ou assigner |
| `ui/orb/radial_waveform.py:1121` | `nonlocal t_ref` jamais assigné | retirer |
| `actions/proactive.py:640` | variable de boucle `field` masque l'import `dataclasses.field` | renommer `fld` |
| `core/tts.py:224,570` | `new_event_loop().run_until_complete` par phrase | `asyncio.run()` ou boucle persistante du worker |
| `core/tool_dispatcher.py:3754` | `time.sleep(1)` dans un thread créé depuis une coroutine | acceptable, mais préférer `loop.call_later(1, os._exit, 0)` |
| `core/freeze_watch.py:66` | `_beats[name] = now` réarme même si le gel continue | conserver la première date, n'écrire qu'un rapport toutes les 30 s (déjà le cas) mais signaler la durée cumulée |

### 23. Documentation périmée

- `readme.md` décrit encore `ui.py` monolithique et Python 3.11/3.12 alors que
  la machine tourne en **3.14.7** (et `webrtcvad` importe `pkg_resources`,
  supprimé bientôt).
- **Correction** : mettre à jour la section structure, préciser 3.13+ et le
  remplacement de `webrtcvad-wheels` par Silero ONNX (déjà opt-in).

---

## Ordre d'exécution recommandé

| Semaine | Fiches | Résultat attendu |
|---|---|---|
| 1 | 1, 2, 3, 7 | Plus de gel audio ; suite de tests verte ; diff en attente commité |
| 2 | 4, 5, 6, 8, 9 | Aucun outil > 20 s ; auto-réparation utilisable ; e-mails envoyés |
| 3 | 10, 11, 12 (première moitié), 13, 16 | Navigateur robuste ; 200 handlers muets journalisés ; HTTP borné |
| 4 | 14, 15, 17, 18, 19 | Arrêt propre ; subprocess unifiés ; bases compactées |
| 5 | 20, 21, 22, 23 | Dépôt léger ; lint en CI ; docs à jour |

## Vérification après chaque semaine

```bash
python -m pytest -q -x
python -m pyflakes core actions ui main.py | grep -v "imported but unused"
./anogpt-ctl doctor
./anogpt-ctl action-stats
ls logs/freeze-*.txt 2>/dev/null | wc -l
```

Le dernier compteur doit rester à zéro sur une session de 30 minutes avec
voix, carte et veille visuelle actives.
