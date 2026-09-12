# ANO-GPT — Ce qui reste à construire pour l'ultime

> Écrit le 16 août 2026, après relecture du code réel.
> `CATALOGUE_AMELIORATIONS.md` listait la vision de départ ; l'essentiel est
> aujourd'hui **fait** (musique MPV/IPC, HUD flottant, orbe GPU, conscience
> d'écran, mémoire SQLite, Calendar, Gmail, routines, MCP, speaker ID, GPS du
> téléphone, auto-réparation, carte unique + itinéraire OSRM).
> Ce document-ci ne recense donc **que ce qui manque encore**, classé par
> impact réel sur l'usage quotidien, pas par élégance technique.

**Contrainte qui gouverne tout** : 2 cœurs, 11 Go, Qt sur le thread principal
et l'audio dans un thread asyncio — ils partagent le GIL. Chaque proposition
ci-dessous est chiffrée en coût CPU, parce qu'une idée qui rend la voix
saccadée est une mauvaise idée, quelle que soit sa beauté.

---

## Matrice de décision

| # | Amélioration | Impact | Effort | CPU | Nature |
| :- | :--- | :--- | :--- | :--- | :--- |
| 1 | **Interruption vocale pendant qu'il parle** | 🔴 Énorme | 🟡 Moyen | 🟢 Faible | Amélioration |
| 2 | **Navigation guidée pas-à-pas (suite OSRM)** | 🔴 Énorme | 🟡 Moyen | 🟢 Faible | Amélioration |
| 3 | **Reprise transparente de la session Gemini** | 🔴 Énorme | 🟢 Faible | 🟢 Nul | Amélioration |
| 4 | **Mémoire : rappel par le sens, pas par les mots** | 🔴 Énorme | 🟡 Moyen | 🟢 Faible | Amélioration |
| 5 | **Mini-orbe permanent au coin de l'écran** | 🟠 Fort | 🟡 Moyen | 🟢 Faible | Nouveau |
| 6 | **Anticipation : le modèle des habitudes** | 🟠 Fort | 🔴 Élevé | 🟢 Faible | Amélioration |
| 7 | **Repli hors-ligne orchestré** | 🟠 Fort | 🔴 Élevé | 🟡 Moyen | Nouveau |
| 8 | **Anneau de confiance (voix + actions destructrices)** | 🟠 Fort | 🟢 Faible | 🟢 Nul | Amélioration |
| 9 | **Carte : piéton, vélo, isochrones, « en chemin »** | 🟠 Fort | 🟡 Moyen | 🟢 Faible | Amélioration |
| 10 | **Panneau des tâches de fond** | 🟡 Moyen | 🟢 Faible | 🟢 Nul | Amélioration |
| 11 | **Présence caméra : il sait si tu es là** | 🟡 Moyen | 🟡 Moyen | 🟡 Moyen | Nouveau |
| 12 | **Interprète en direct** | 🟡 Moyen | 🟢 Faible | 🟢 Nul | Nouveau |
| 13 | **Compteur de jetons et de coût** | 🟡 Moyen | 🟢 Faible | 🟢 Nul | Nouveau |
| 14 | **Journal de bord du soir** | 🟡 Moyen | 🟢 Faible | 🟢 Nul | Nouveau |
| 15 | **Minuteurs visuels multiples** | 🟡 Moyen | 🟢 Faible | 🟢 Nul | Amélioration |

---

# NIVEAU 1 — Les quatre qui changent tout

## 1. L'interrompre pendant qu'il parle

**Aujourd'hui** : half-duplex strict. Pendant qu'ANO-GPT parle, le callback
micro de `main.py` jette tout — c'est la règle qui l'empêche de s'entendre
lui-même, et elle est juste : sa voix est une vraie voix humaine, aucun seuil
ne la distingue de la tienne. Conséquence : quand il part sur une tirade de
quarante secondes, tu attends, ou tu cliques.

**Ce qu'on gagne** : le réflexe JARVIS. « ANO, stop » et il se tait au milieu
d'un mot. C'est la différence entre parler *à* une machine et parler *avec*
elle — probablement le seul point où l'on sent encore que c'est un logiciel.

**Comment, sans casser la règle** : le flux micro n'est pas rouvert vers le
modèle. Il est branché sur un second consommateur, purement local :

1. `module-echo-cancel` de PipeWire (déjà connu de `core/audio_router.py`)
   fournit un flux micro nettoyé de ce que sortent les enceintes ;
2. sur ce flux, **Vosk seul** (déjà présent pour le wake word) écoute un
   vocabulaire d'une poignée de mots : `stop`, `attends`, `ANO stop`,
   `tais-toi`. Vosk avec grammaire restreinte coûte ~2 % d'un cœur ;
3. un mot reconnu ⇒ on coupe le TTS et on rouvre l'écoute normale. **Rien
   n'est jamais transmis au modèle** pendant qu'il parle : la règle tient.

**Pièges** : sans echo-cancel actif, il s'interrompra lui-même en disant
« stop » ; exiger le préfixe « ANO » ou vérifier que la voix correspond à ton
empreinte (`core/speaker_id.py` est déjà là) lève l'ambiguïté.

---

## 2. La navigation guidée pas-à-pas

**Aujourd'hui** : l'itinéraire OSRM s'affiche sur la carte avec distance et
durée (fait aujourd'hui, `core/map_render.py`). C'est une image : il te montre
la route, il ne t'y emmène pas. Et le tracé est figé au moment du clic.

**Ce qu'on gagne** : le téléphone pousse déjà sa position GPS toutes les
quelques secondes (`core/geolocation.py::set_live_position`, appli
`mobile/ano_remote`). Il ne manque presque rien pour que la carte du PC
devienne un GPS parlant : le point avance sur la route, la manœuvre suivante
s'affiche en grand, et la voix annonce « dans 200 mètres, à droite ».

**Comment** :
* rappeler OSRM avec `steps=true&annotations=true` — les manœuvres arrivent
  déjà avec `maneuver.type`, `modifier` et la distance de chaque tronçon ;
* traduire les manœuvres en français une fois côté Python (table de ~15
  libellés), pas à chaque tour ;
* la carte reçoit la position live par le canal déjà utilisé pour
  `set_live_position` et fait avancer un marqueur + recalcule la distance à la
  prochaine manœuvre ;
* annonce vocale à trois seuils (500 m / 150 m / « maintenant ») en passant par
  le TTS, jamais par le modèle : zéro latence, zéro jeton ;
* écart > 60 m de la polyligne pendant 10 s ⇒ recalcul silencieux.

**Piège** : le serveur de démonstration OSRM est limité en débit et sans
garantie. Pour de la navigation réelle, viser une instance publique plus
généreuse (Valhalla d'OSM) ou accepter un recalcul toutes les 30 s maximum.

---

## 3. La reprise transparente de session

**Aujourd'hui** : `core/gemini_connection.py` sait *diagnostiquer* une panne
(clé invalide, quota, réseau), et c'est déjà mieux que rien. Mais quand la
session Live tombe — coupure Wi-Fi de dix secondes, quota momentané — la
conversation en cours est perdue et il faut relancer.

**Ce qu'on gagne** : un assistant qui ne « meurt » jamais. La session se
rouvre toute seule, réinjecte le profil, les trois derniers épisodes et le
contexte ambiant, et reprend au milieu de la phrase avec un simple
« excuse-moi, je reprends ».

**Comment** : une machine à états de connexion avec repli exponentiel (1 s, 2,
4, 8, plafond 30 s), un tampon des deux derniers tours utilisateur non
répondus, et la réémission du prompt système (`core/prompt.txt` + profil de
`core/memory_store.py`) à chaque réouverture. L'interface montre un état
« reconnexion » sur l'orbe plutôt qu'une erreur.

---

## 4. La mémoire qui rappelle par le sens

**Aujourd'hui** : `core/memory_store.py` est du SQLite + FTS5, sans embeddings
— un choix assumé et bon sur deux cœurs. Sa limite est réelle : la recherche
est lexicale. « Mon histoire de voiture » ne retrouve pas « la Peugeot du
garage de Matam » ; les mots ne se touchent pas.

**Ce qu'on gagne** : le rappel qui impressionne — « comme la fois où tu m'avais
parlé de… » — sans installer de base vectorielle ni faire tourner un modèle
d'embedding sur le CPU de la voix.

**Comment (l'astuce)** : enrichir **à l'écriture**, pas à la lecture. Au moment
où un souvenir est enregistré, `core/agent_brain.py` (Antigravity, processus
séparé, priorité basse, facturé à l'abonnement) génère 5 à 10 mots-clés élargis
— synonymes, entités, thème — stockés dans une colonne `aliases` indexée par
FTS5. La recherche reste une requête FTS5 à une milliseconde, mais elle porte
désormais sur le sens écrit une fois pour toutes.

**Bonus** : même mécanique pour `core/file_indexer.py` (aujourd'hui plein texte
FTS5 pur) — un résumé de trois lignes par document indexé, et « retrouve la
note sur l'architecture réseau » fonctionne enfin.

---

# NIVEAU 2 — Ce qui le rend présent et fiable

## 5. Le mini-orbe permanent

**Aujourd'hui** : rien. L'assistant existe quand sa fenêtre est ouverte. Le
reste du temps il n'est nulle part, et le rappeler demande un raccourci.

**Ce qu'on gagne** : une bulle de 80 px épinglée dans un coin, au-dessus de
tout, qui respire au repos, s'anime à l'écoute et affiche une réponse courte en
bulle éphémère. C'est ce qui fait passer d'« une application que je lance » à
« quelqu'un qui est là ».

**Comment** : `LayerShellQt` existe mais ajoute une dépendance fragile. Plus
simple sur Hyprland : une seconde `QWidget` sans décoration + une règle
`windowrulev2 = float, pin, noborder, noblur, class:^(ano-orb)$`. Elle réutilise
le rendu GPU de l'orbe existant, à taille réduite, avec la même source FFT.

**Piège** : deux `QOpenGLWidget` = deux contextes GL. Sur cette machine, ne
jamais les animer tous les deux — la grande fenêtre se met en pause quand la
bulle est visible, et inversement.

---

## 6. L'anticipation : le modèle des habitudes

**Aujourd'hui** : `actions/proactive.py` réagit au contexte *instantané*
(fenêtre active, silence, heure) et `core/daily_briefing.py` parle le matin.
Il réagit bien ; il ne prévoit rien.

**Ce qu'on gagne** : « tu ouvres toujours ce projet vers 21 h, je te mets ta
playlist ? » — la proactivité qui tombe juste parce qu'elle s'appuie sur ce que
tu fais vraiment, pas sur une règle écrite d'avance.

**Comment** : les données existent déjà en partie
(`actions/launch_tracker.py`, `core/tool_stats.py`, historique musique). Les
agréger dans une table `habitudes(jour, créneau_30min, événement, occurrences)`
et n'émettre une suggestion qu'au-delà de 4 occurrences sur 3 semaines. Une
suggestion à la fois, jamais deux fois la même journée, et un « non » enregistré
tue la règle pour un mois. La proactivité mal réglée devient insupportable en
trois jours : le garde-fou compte plus que le modèle.

---

## 7. Le repli hors-ligne orchestré

**Aujourd'hui** : les briques locales sont là — Vosk et faster-whisper dans
`core/stt.py`, Kokoro dans `core/tts.py` — mais elles servent séparément. Coupe
le Wi-Fi : plus de Gemini Live, donc plus d'assistant, alors que tout ce qui
est local (ouvrir une appli, la musique, les minuteurs, le système) pourrait
continuer.

**Ce qu'on gagne** : il ne tombe jamais complètement. En mode dégradé, il
annonce « je suis hors ligne, je garde le contrôle de la machine » et continue
d'exécuter les intentions simples.

**Comment** : un routeur d'intentions local en amont du modèle — les routines
de `core/routines.py` font déjà exactement ça pour les phrases exactes ;
l'étendre à une grammaire de commandes (« ouvre X », « joue Y », « minuteur N
minutes ») reconnue par Vosk. Un LLM local (Ollama, 3 B quantifié) n'est **pas**
recommandé sur cette machine : il mangerait les deux cœurs et rendrait le TTS
haché. Mieux vaut un assistant hors ligne franchement plus bête que lent.

---

## 8. L'anneau de confiance

**Aujourd'hui** : `core/speaker_id.py` sait reconnaître ta voix (CAM++, 512
dimensions, quelques microsecondes par comparaison). Mais la vérification n'est
pas la condition d'exécution des actions dangereuses : `actions/shell_exec.py`,
la suppression de fichiers, l'extinction obéissent à qui parle.

**Ce qu'on gagne** : un assistant vocal obéit à toute voix dans la pièce, y
compris à une voix qui sort d'une vidéo YouTube. Trois niveaux suffisent :
lecture libre, action système = voix reconnue, action destructrice = voix
reconnue **et** confirmation orale explicite.

**Comment** : un décorateur sur le dispatcher d'`core/action_runtime.py`, une
liste de familles d'actions par niveau, et le repli « je ne reconnais pas ta
voix » plutôt qu'un refus muet. Effort faible, tout est déjà écrit.

---

## 9. La carte : au-delà de la voiture

**Aujourd'hui** : itinéraire OSRM en voiture uniquement (le serveur public de
démonstration ne déploie que ce profil), et le seul objet affichable est un
lieu ou une liste de lieux.

**Ce qu'on gagne** :
* **piéton et vélo** — via l'instance publique Valhalla d'OpenStreetMap, qui
  expose les trois profils. En ville, « à pied » est souvent la vraie réponse ;
* **isochrone** — « ce que j'atteins en 15 min à pied » dessiné en zone néon.
  C'est spectaculaire, c'est utile, et c'est un seul appel d'API ;
* **« sur le chemin »** — croiser l'itinéraire tracé avec la recherche de lieux
  existante (`core/places.py`) : « une pharmacie sur ma route ».

**Comment** : la couche de routage vient d'être isolée dans la page Leaflet ;
ajouter un sélecteur de profil dans le panneau d'itinéraire et un second
fournisseur derrière la même fonction `drawRoute`.

---

# NIVEAU 3 — Le confort qui se remarque

## 10. Le panneau des tâches de fond

**Aujourd'hui** : `actions/background_tasks.py` surveille des pages, des
builds, la position. Ça tourne, mais c'est invisible : impossible de savoir ce
qu'il surveille sans le lui demander.

**Ce qu'on gagne** : une pile de lignes discrètes en bas d'écran (ce qui est
surveillé, depuis quand, prochain contrôle) et un clic pour arrêter. Ce qui est
invisible finit par être oublié, puis par surprendre.

## 11. La présence par la caméra

**Aujourd'hui** : `core/camera_studio.py` capture à la demande. Il ne sait pas
si tu es devant la machine.

**Ce qu'on gagne** : il se tait quand tu n'es pas là (et garde la remarque pour
ton retour), il salue quand tu reviens, il baisse la musique quand tu te lèves.

**Comment** : une image toutes les 5 s, détection de visage Haar d'OpenCV en
160×120 — quelques millisecondes. **Jamais** d'image envoyée nulle part : seule
la valeur « présent / absent » quitte le module. À rendre coupable d'un
interrupteur visible dans les réglages, sinon c'est une caméra qui tourne en
permanence, et ça ne se fait pas sans le dire.

## 12. L'interprète en direct

**Nouveau** : mode « traduis ce qu'on se dit » — il écoute une phrase en
français, la restitue en anglais, et l'inverse, en boucle jusqu'à « stop ».
Gemini Live le fait nativement, sans surcoût technique : c'est essentiellement
une consigne de prompt et un état d'interface. Impact énorme le jour où ça
sert, nul les autres jours — d'où le niveau 3.

## 13. Le compteur de jetons et de coût

**Aujourd'hui** : rien ne dit ce que consomme une session Gemini Live. On
découvre le coût sur la facture.

**Ce qu'on gagne** : un chiffre en coin d'écran (minutes de session, jetons,
coût estimé du jour) et une alerte au seuil. Sur un assistant qui écoute en
continu, savoir ce qu'on dépense change la façon de s'en servir.

## 14. Le journal de bord du soir

**Nouveau** : `core/memory_episode.py` fabrique déjà des résumés de
conversation. Les agréger le soir en un Markdown daté (ce qui a été fait,
projets touchés, points laissés en suspens) donne, sans effort supplémentaire,
une mémoire consultable à la main — et un excellent carburant pour le briefing
du lendemain.

## 15. Les minuteurs visuels multiples

**Aujourd'hui** : `actions/reminder.py` gère les rappels datés. Le minuteur de
cuisine — nommé, simultané, visible qui décompte — n'existe pas vraiment.

**Ce qu'on gagne** : « minuteur pâtes 9 minutes » + « minuteur thé 4 minutes »
en même temps, deux cartes qui décomptent, la musique qui baisse à l'échéance
et une annonce vocale. C'est le genre de fonction qu'on utilise trois fois par
jour sans y penser.

---

# Ordre d'attaque conseillé

1. **§3 reprise de session** — le plus petit effort pour le plus gros gain de
   fiabilité. Rien ne sert d'ajouter des fonctions à un assistant qui tombe.
2. **§8 anneau de confiance** — quelques heures, et le risque disparaît.
3. **§2 navigation guidée** — dans la continuité directe de l'itinéraire OSRM
   posé aujourd'hui, pendant que le sujet est frais.
4. **§1 interruption vocale** — la plus grosse marche d'immersion ; à faire
   avec soin, c'est le cœur audio.
5. **§4 mémoire par le sens**, puis **§5 mini-orbe**, puis le reste selon
   l'envie.

# Ce qu'il ne faut pas faire

* **Un LLM local en secours permanent** : deux cœurs partagés avec Qt et
  l'audio. Le résultat serait une voix hachée pour une intelligence moindre.
* **Une seconde carte**, sous quelque prétexte que ce soit.
* **Rouvrir le micro vers le modèle pendant qu'il parle** : l'interruption du
  §1 reste locale, ou elle ne se fait pas.
* **Des embeddings calculés à la volée** : le rappel sémantique se paie à
  l'écriture, une fois, dans un processus séparé.
* **Empiler la proactivité** : chaque nouvelle source de suggestion doit passer
  par le même verrou « une seule à la fois, et un refus fait taire la règle ».
