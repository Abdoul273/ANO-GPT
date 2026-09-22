# ANO-GPT — Plan d'améliorations « Ultra JARVIS »

> **Document destiné à une IA de développement.** Il décrit, étape par étape, toutes les
> améliorations à apporter à l'application ANO-GPT (`~/OUTILS/ANO-GPT`) pour en faire
> l'assistant le plus puissant, beau, fluide et complet possible — style JARVIS réel.
> Aucun code ici : uniquement l'analyse de l'existant, ce qu'il faut construire, comment,
> et dans quel ordre. Chaque amélioration porte une **Priorité** (P0 = à faire en premier),
> un **Impact** (ce que ça change pour l'utilisateur) et une **Difficulté** (facile /
> moyenne / difficile).

---

## 0. État des lieux — ce que l'IA doit savoir avant de toucher au code

Architecture actuelle (fork profondément modifié de « Mark XLIX ») :

| Fichier | Rôle | Taille |
|---|---|---|
| `main.py` | Boucle cœur : session **Gemini Live** (audio natif bidirectionnel), dispatch des ~30 outils, capture micro via `sounddevice`, wake word Vosk, socket IPC (`anogpt-ctl`) | ~2 000 lignes |
| `ui.py` | Toute l'interface PyQt6 : orbe (`HudCanvas`, rendu logiciel QPainter), panneaux gauche/droite fixes, journal d'activité, zone de dépôt fichier, carte OSM (iframe), caméra, overlays de config | ~4 500 lignes |
| `actions/*.py` | 30+ outils : musique, navigateur, apps, fichiers, shell, recherche web multi-modes, météo, rappels, messages… | ~800 Ko |
| `core/` | STT de secours, TTS, wake word, géolocalisation IP, client LLM multi-fournisseurs, IPC | — |
| `memory/` | Mémoire persistante JSON + config | — |
| `dashboard/` | Serveur web pour le contrôle depuis le téléphone (QR code) | — |

**Contraintes machine à respecter absolument** (déjà payées cher) :
- **Wayland/Hyprland** : pas de raccourcis globaux applicatifs (le compositeur les possède et les forwarde via le socket) ; `mss`/`pyautogui` rendent une image noire ; la config Hyprland est en **Lua**.
- **Voix = Gemini Live natif.** Changer de LLM supprime la voix temps réel. Ne jamais casser ce chemin.
- **Bluetooth HFP** : si l'assistant ouvre le micro d'un casque Bluetooth, le casque bascule en profil HFP 16 kHz mono → la musique devient inaudible. Toute amélioration micro doit gérer ce piège explicitement (voir §D).
- `hyprctl` répond « ok » même quand rien ne s'est passé : toujours relire l'état réel après une action fenêtre.

**Ce qui existe déjà** (à améliorer, pas à recréer) :
- Une carte OSM (`show_map` / `_on_show_map` dans `ui.py`) — mais c'est une simple iframe `openstreetmap.org/export/embed.html` avec un marqueur, dans la pile centrale, sans points d'intérêt ni prix.
- La musique (`actions/music.py`) — recherche locale + YouTube via yt-dlp, lecture VLC/mpv, contrôle playerctl — mais **le lecteur ouvre sa propre fenêtre**.
- Un « panneau de contenu » bas (`_build_content_panel`) qui affiche du texte brut.
- Le journal d'activité (`LogWidget`) dans le panneau droit — c'est là que sort le texte de l'assistant aujourd'hui.
- La géolocalisation IP + geocoding (`core/geolocation.py`).

---

## 1. Tableau récapitulatif des priorités

| # | Amélioration | Priorité | Impact | Difficulté |
|---|---|---|---|---|
| A1 | Texte de l'IA au centre, style JARVIS (supprimer la div activité) | **P0** | Énorme — c'est le visage de l'app | Moyenne |
| A2 | Interface 100 % flottante (HUD en couches, plus de colonnes fixes) | **P0** | Énorme | Moyenne–difficile |
| A3 | Boutons icônes (micro, interrompre, upload) | **P0** | Fort | Facile |
| A4 | Orbe GPU réactif et organique | **P0** | Énorme | Moyenne–difficile |
| B | Musique sans fenêtre + carte lecteur intégrée (mpv IPC) | **P0** | Énorme | Moyenne |
| A5 | Div message flottante à droite (emails, notifications, réponses longues) | **P1** | Fort | Facile–moyenne |
| C | Carte « où acheter X » : lieux + prix + itinéraire | **P1** | Fort | Moyenne |
| D | Micro intelligent (choix périphérique, AirPods, anti-HFP) | **P1** | Fort | Moyenne |
| E | Emails Gmail sécurisés (lecture, notifications, résumé) | **P1** | Fort | Moyenne |
| F | Apps « intégrées » dans JARVIS (cartes dédiées, miroir de fenêtre) | **P2** | Moyen | Difficile |
| G | Amélioration transverse de toutes les actions (feedback visuel riche) | **P1** | Fort | Moyenne (long) |
| H | Robustesse & architecture (découpage, tests, watchdog) | **P2** | Moyen (mais conditionne tout le reste) | Moyenne |

**Ordre conseillé : A3 → B → A1 → A4 → A2 → A5 → D → C → E → G → H → F.**
(A3 et B d'abord : gains visibles immédiats et peu risqués ; A2 en dernier du bloc UI
car c'est la refonte de layout la plus invasive.)

---

## A. Refonte UI « vrai JARVIS » — la priorité absolue

### A1. Le texte de l'IA sort au centre de l'écran, pas dans une div latérale — P0

**Aujourd'hui** : les réponses partent dans le `LogWidget` (« Journal d'activité ») du
panneau droit, avec un effet machine à écrire dans une petite boîte de texte. C'est un
log de console, pas un JARVIS.

**Cible** : quand l'assistant parle, ses phrases apparaissent **au centre de la fenêtre,
superposées sous/autour de l'orbe**, en grand, avec une entrée cinématique.

**Étapes :**
1. Créer un nouveau widget overlay transparent, enfant du widget central, positionné
   au-dessus du HUD (comme le sont déjà `_CameraPreview` et `ClipboardPanel` — le motif
   existe dans le code, s'en inspirer).
2. Y afficher la transcription sortante de Gemini Live (elle existe déjà :
   `output_audio_transcription` est activée dans `_build_config` de `main.py`) —
   phrase par phrase, synchronisée avec la voix.
3. Style : typographie grande (18–24 px), fine, majuscules légères ou casse normale,
   couleur accent (`C.PRI`) avec halo/glow léger, fond **aucun** (transparent) ou un
   dégradé radial très sombre derrière le texte pour la lisibilité.
4. Animation d'entrée : révélation mot par mot ou lettre par lettre avec un léger fondu
   + montée de 6–10 px ; animation de sortie : fondu vers le haut après 4–6 s ou quand
   la phrase suivante arrive. Ne garder à l'écran que 2–3 lignes maximum.
5. Effet « JARVIS » optionnel : un fin trait horizontal animé (scanline) qui balaie le
   texte à l'apparition, ou des crochets décoratifs `[ ]` qui s'écartent.
6. **Supprimer le `LogWidget` du panneau droit.** Garder toutefois un log technique
   accessible (raccourci ou tiroir) pour le débogage — le déplacer dans le quick drawer,
   pas le détruire : c'est précieux quand un outil échoue.
7. Le texte tapé par l'utilisateur peut aussi apparaître brièvement au centre, plus
   petit et dans une teinte neutre, pour donner la sensation de dialogue.

**Pièges** : le rendu doit rester fluide pendant que l'orbe anime à côté — utiliser des
`QPropertyAnimation`/opacité, pas de re-layout complet à chaque mot. Penser au
word-wrap sur les longues phrases (limiter la largeur à ~60 % de la fenêtre).

---

### A2. Interface 100 % flottante — plus de colonnes fixes — P0

**Aujourd'hui** : layout rigide trois colonnes (gauche 
télémétrie / centre HUD / droite journal+commande) + header + footer. C'est un
dashboard, pas un HUD.

**Cible** : **une seule scène plein cadre** : l'orbe et le fond occupent tout, et tous
les éléments (télémétrie, lecteur musique, carte, messages, champ de commande) sont des
**panneaux flottants translucides** posés par-dessus, avec coins arrondis, bordure
hairline, léger flou/glow, apparition/disparition animées.

**Étapes :**
1. Remplacer le `QHBoxLayout` trois-colonnes de `MainWindow.__init__` par : le
   `HudCanvas` en widget central plein cadre, et chaque panneau en widget enfant
   flottant repositionné dans `resizeEvent` (le code fait déjà exactement ça pour
   `ClipboardPanel` et `_CameraPreview` — généraliser ce motif).
2. Créer une classe de base `FloatingPanel` : fond translucide (`rgba` sombre ~85 %),
   bordure 1 px accent atténué, rayon 14–16 px, ombre portée douce, titre en petites
   capitales avec puce `◈`, bouton fermer discret, animations d'entrée (fondu + slide
   8 px) et de sortie. Tous les panneaux existants (télémétrie, contenu, carte, caméra)
   deviennent des instances de cette base.
3. Positions par défaut : télémétrie compacte en haut-gauche ; lecteur musique en
   **milieu-gauche** (voir §B) ; messages/notifications en **haut-droite → milieu-droite**
   (voir §A5) ; champ de commande en **bas-centre**, flottant, style « barre spotlight » ;
   horloge/météo en haut-centre ou haut-droite.
4. Chaque panneau doit être **masquable** et n'apparaître que quand il a quelque chose à
   dire (le lecteur musique n'existe à l'écran que si de la musique joue ; la carte que
   si une carte est demandée). L'état de repos idéal = **l'orbe seul + la barre de
   commande**, rien d'autre. C'est ça, l'effet JARVIS.
5. Bonus immersion : rendre les panneaux déplaçables à la souris (drag sur le titre) et
   mémoriser leurs positions dans la config.
6. Fond de scène : garder le fond très sombre actuel mais l'enrichir — grille
   perspective subtile, vignettage, particules lentes déjà présentes dans `HudCanvas`
   (les « motes ») à étendre à tout le cadre.

**Pièges** : sous Wayland la fenêtre ne se positionne pas elle-même (le code le sait
déjà : il saute le `move()`). La translucidité *interne* des panneaux (peints par Qt)
fonctionne partout ; ne pas dépendre de la transparence de la fenêtre système. Vérifier
le z-order : orbe < panneaux < overlays de config < texte central.

---

### A3. Boutons icônes compacts (micro, interrompre, upload) — P0, gain rapide

**Aujourd'hui** : « Interrompre » et « Micro actif » sont des boutons texte 34 px ;
l'upload est une grosse zone de dépôt (`FileDropZone`) qui mange le panneau droit.

**Cible** : une rangée de **petits boutons ronds à icône** (36–40 px) intégrés à la
barre de commande flottante : 🎤 micro (état actif/muet visible : couleur + petite onde
animée quand il écoute), ⏹/✕ interrompre (n'apparaît **que** pendant que l'IA parle),
📎 trombone pour joindre un fichier, ➜ envoyer.

**Étapes :**
1. Utiliser une vraie banque d'icônes vectorielles cohérente — recommandation :
   **Lucide** ou **Phosphor** (SVG, licence libre, style fin qui colle au thème) rendues
   en `QIcon` teintées à la couleur d'accent. Bannir les emojis système actuels (`➜`,
   `✕`) qui rendent différemment selon la police.
2. Le clic sur 📎 ouvre le sélecteur de fichier ; le **drag & drop doit rester possible
   sur toute la fenêtre** (déplacer la gestion de drop de `FileDropZone` vers la
   `MainWindow` entière, avec un overlay plein écran « Déposez votre fichier » pendant
   le survol). Une fois un fichier chargé, afficher une petite pastille au-dessus de la
   barre (nom + taille + croix pour retirer) au lieu de la grosse zone.
3. États visuels : micro actif = anneau pulsant accent ; muet = icône barrée rouge
   atténué ; interruption au survol = rouge. Tooltips avec les raccourcis (F4, Échap).
4. Supprimer `FileDropZone` et les boutons texte du panneau droit une fois la barre en
   place (le panneau droit disparaît de toute façon avec A2).

**Impact/Difficulté** : fort/facile — c'est le premier chantier à faire, il rend
l'app immédiatement plus propre sans rien casser.

---

### A4. Orbe nouvelle génération — réactif, organique, GPU — P0

**Aujourd'hui** : l'orbe est impressionnant sur le papier (sphère fil de fer 3D,
plasma, étincelles, arcs) mais tout est **dessiné au CPU avec QPainter** à chaque tick,
avec un limiteur adaptatif de framerate — d'où un rendu qui peut saccader et des
réactions « mécaniques ». Il réagit au volume (`set_volume`) mais linéairement.

**Cible** : un orbe qui semble **vivant** : il respire au repos, frémit quand il
entend, s'illumine et ondule en parlant, avec des transitions douces entre états.

**Étapes :**
1. **Passer le rendu au GPU.** Deux options :
   - Option recommandée : remplacer le `HudCanvas` par un `QOpenGLWidget` (ou une
     scène QML `ShaderEffect` embarquée via `QQuickWidget`) avec un **fragment shader**
     plein cadre : sphère/noyau en raymarching léger ou superposition de bruits
     (FBM/simplex) déformant des anneaux — c'est le standard des orbes « Siri/JARVIS ».
   - Option minimale : garder QPainter mais pré-rendre davantage en pixmaps et réduire
     le travail par frame. (Moins bien : plafond de qualité vite atteint.)
2. **Réactivité audio réelle** : au lieu du simple RMS, calculer une petite **FFT** du
   flux (entrée micro quand il écoute, sortie TTS quand il parle — les deux flux
   existent déjà dans `main.py`) et piloter le shader avec 3–4 bandes (graves →
   pulsation du noyau, médiums → amplitude des ondulations, aigus → scintillement).
   Lisser avec une attaque rapide / relâche lente pour un mouvement naturel.
3. **Machine à états visuelle** avec transitions interpolées (jamais de saut sec) :
   `repos` (respiration lente 0,1 Hz, teinte calme) → `écoute` (anneau qui s'ouvre,
   suit la voix de l'utilisateur) → `réflexion` (rotation accélérée, particules qui
   convergent) → `parole` (ondulations amples synchronisées à la voix) → `action/outil`
   (flash bref + arc électrique vers le panneau concerné — détail qui fait « waouh ») →
   `erreur` (pulsation rouge brève). Les états existent déjà dans le code (`state`),
   il manque les transitions et la richesse.
4. Micro-détails organiques : bruit de Perlin sur le rayon (jamais parfaitement rond),
   dérive lente de teinte, rares « glitchs » d'étincelles au repos.
5. Conserver l'API publique actuelle (`set_state`, `set_volume`, `muted`, `speaking`)
   pour ne pas toucher `main.py`.

**Pièges** : le shader `.frag` du projet voisin `~/OUTILS/jarvis` (Qt Quick,
`gui/qml/shaders/orb.frag`) peut servir de référence de pipeline de compilation
(qsb). Vérifier la charge GPU sur batterie — prévoir un mode économie (framerate réduit
sur batterie, cf. le profil énergie de la machine).

---

### A5. Div message flottante à droite (notifications, emails, réponses riches) — P1

**Cible** : à l'emplacement de l'ancien journal d'activité, une **pile de cartes
flottantes** à droite : chaque événement (nouvel email §E, résultat de recherche,
rappel, confirmation d'action longue) apparaît comme une carte translucide qui glisse
depuis le bord droit, reste tant que c'est pertinent, et se réduit/disparaît.

**Étapes :**
1. Créer un conteneur vertical flottant côté droit (basé sur `FloatingPanel` de A2)
   avec pile de cartes : titre + icône + corps (texte riche, pas du plain text — le
   `_show_content` actuel fait du `setPlainText`, passer en rendu Markdown/HTML léger).
2. Types de cartes : `message` (email, notification), `résultat` (recherche web, météo
   détaillée), `tâche` (progression d'une action longue avec spinner), `confirmation`
   (demande oui/non avec deux boutons — utile pour le shell risqué et le « chercher sur
   YouTube ? » de la musique).
3. Chaque carte : bouton fermer, timestamp, et pour certaines des actions rapides
   (répondre, ouvrir dans l'app, copier).
4. Exposer une API interne unique `ui.show_card(type, title, body, actions)` que
   **tous les outils** de `actions/` utiliseront (voir §G) — remplacera peu à peu
   `show_content`.
5. Limiter à ~4 cartes visibles, les plus anciennes se compactent en une ligne.

---

### A6. Cohérence design globale — P1 (en continu)

- **Typographie** : une seule famille (Inter est déjà là) mais hiérarchie claire —
  définir 4 tailles nominales et s'y tenir ; les tailles 7–8 pt actuelles sont trop
  petites, remonter la base.
- **Couleurs** : conserver le système d'accent dynamique (`apply_ui_accent` + HueWheel,
  très bien fait) mais définir des rôles sémantiques fixes (succès, danger, info) qui
  ne bougent pas avec l'accent.
- **Animations** : règle unique — tout ce qui apparaît/disparaît le fait en 150–250 ms
  avec easing `OutCubic` ; rien ne « pop » brutalement.
- Supprimer le footer « By FatihMakes » et le header dense au profit d'éléments
  flottants discrets (l'app est un fork personnel, l'espace est précieux).

---

## B. Musique sans interface + carte lecteur intégrée — P0

**Aujourd'hui** : `music_control` marche bien (locale + YouTube, repli VLC→mpv,
playerctl) **mais** le lecteur ouvre sa fenêtre (VLC, ou mpv avec
`--force-window=yes`), ce qui casse l'immersion.

**Cible** : « lance la musique » → la musique joue **sans aucune fenêtre**, et une
**carte lecteur** apparaît à gauche dans ANO-GPT : pochette, titre/artiste, barre de
progression cliquable, boutons précédent/pause-lecture/suivant, volume, shuffle, et
bouton stop. Contrôle total à la voix **et** à la souris.

**Le meilleur choix technique : mpv en mode headless piloté par socket IPC.**
Pourquoi mpv et pas VLC : mpv accepte `--no-video` / `--force-window=no` (aucune
fenêtre, jamais), expose un **socket JSON IPC** (`--input-ipc-server`) qui donne un
contrôle total et instantané (position, pause, seek, volume, playlist, métadonnées,
événements de fin de piste), démarre instantanément, et lit aussi bien un fichier local
qu'une URL de flux YouTube résolue par yt-dlp. VLC reste utile **uniquement** quand
l'utilisateur veut explicitement une fenêtre vidéo.

**Étapes :**
1. Dans `actions/music.py` : ajouter un mode de lancement « headless » par défaut pour
   l'audio — mpv avec vidéo désactivée et fenêtre interdite + socket IPC dans
   `$XDG_RUNTIME_DIR`. Ne garder le lancement fenêtré que si le contenu est une vidéo
   **et** que l'utilisateur veut la regarder (« mets la vidéo » vs « mets la musique » :
   pour une vidéo YouTube demandée en audio, mpv sait ne lire que la piste audio).
2. Créer un petit module `core/player_ipc.py` (client du socket mpv) : envoyer les
   commandes (pause, seek, volume, next/prev sur playlist), lire les propriétés
   (position, durée, titre, pause), s'abonner aux événements (fin de piste →
   enchaînement, mise à jour de la carte).
3. **Carte lecteur UI** (nouvelle `FloatingPanel`, milieu-gauche) : pochette (thumbnail
   YouTube via yt-dlp ou tag du fichier local), titre défilant si trop long, progression
   mise à jour ~2 fois/s (via le socket, pas de polling process), boutons icônes
   (Lucide/Phosphor, cf. A3). Elle apparaît quand la lecture démarre, disparaît en
   fondu quelques secondes après l'arrêt.
4. Rebrancher l'outil `music_control` (pause/resume/next/previous/now_playing) sur le
   socket IPC en priorité, avec playerctl en secours pour les lecteurs externes déjà
   ouverts (Spotify, navigateur). Garder la logique existante très bonne de
   `_ensure_audible` (vérification sink PulseAudio/PipeWire) — elle protège du piège
   Bluetooth.
5. File d'attente : « mets aussi… » ajoute à la playlist mpv au lieu de relancer un
   process ; « musique suivante » = next de playlist.
6. Mettre à jour la description de l'outil dans `main.py` pour que le modèle sache que
   la lecture est silencieuse/sans fenêtre et qu'une carte s'affiche.

**Impact/Difficulté** : énorme/moyenne. C'est le chantier au meilleur ratio.

**Pièges** : conserver les variables d'environnement de lancement déjà gérées
(`_build_env` gère DISPLAY et les cas Bluetooth) ; un seul process mpv persistant
géré par l'app (le tuer proprement à la fermeture — le motif `_kill_tree` existe).

---

## C. « Où trouver X » → carte + lieux + prix — P1

**Aujourd'hui** : `show_map` affiche une iframe OSM avec **un seul marqueur** ; la
géoloc IP donne la ville ; `web_search` a déjà un mode `price`. Les briques existent
mais ne sont pas assemblées.

**Cible** : « où est-ce que je peux trouver une PS5 ? » → carte flottante centrée sur
la position de l'utilisateur avec des **épingles de magasins à proximité**, et une
liste latérale : nom, distance, horaires si connus, **prix trouvé en ligne**, bouton
itinéraire.

**Étapes :**
1. **Trouver les lieux** : nouvel outil `find_nearby` (ou extension de `show_map`)
   qui interroge l'**API Overpass** (OpenStreetMap, gratuite, sans clé) autour de la
   position (`lat/lon` déjà fournis par `core/geolocation.py`) : filtrer par tags
   (`shop=electronics`, `shop=supermarket`, `amenity=pharmacy`…). Faire mapper le
   produit → catégorie de magasin par le LLM lui-même (c'est son travail), l'outil ne
   reçoit que la catégorie et le rayon.
2. **Trouver les prix** : réutiliser `web_search` en mode `price` sur le produit
   (les enseignes locales trouvées + le produit), en parallèle de la requête Overpass ;
   fusionner : chaque lieu affiche le prix en ligne de l'enseigne quand il existe,
   sinon la fourchette de prix générale du produit.
3. **Affichage** : remplacer l'iframe OSM par une **page Leaflet locale** chargée dans
   le `QWebEngineView` existant (Leaflet embarqué dans les assets de l'app, tuiles OSM) :
   plusieurs marqueurs, popups riches (nom, distance, prix), cercle de rayon, thème
   sombre assorti à l'accent, zoom fluide. Le pont Qt↔JS (`QWebChannel`) permet de
   cliquer une épingle → l'IA en parle, et inversement.
4. La carte devient une `FloatingPanel` (grande, centre-droit) au lieu d'écraser l'orbe
   dans la pile centrale.
5. **Itinéraire** : bouton par lieu → ouvrir OSM/Google Maps dans le navigateur avec
   l'itinéraire pré-rempli (pas besoin de routage intégré au début) ; plus tard, tracé
   OSRM (API publique gratuite) directement sur la carte Leaflet.
6. Précision de la position : la géoloc IP est approximative (ville). Ajouter dans les
   réglages un champ « adresse/quartier » optionnel géocodé une fois (le geocoding et
   son cache existent déjà) pour des distances réalistes.
7. Déclaration outil dans `main.py` : bien décrire quand l'utiliser (« où trouver /
   acheter / le plus proche ») et exiger la relance de la position à chaque session
   plutôt que de deviner.

**Pièges** : Overpass a des limites de débit — mettre en cache par (catégorie, zone) ;
prévoir un timeout court et un repli « recherche web classique + carte centrée ville ».

---

## D. Micro intelligent — P1

**Aujourd'hui** : `sd.InputStream` ouvre le **périphérique par défaut** du système,
point. Si l'utilisateur branche ses AirPods, rien ne change (ou pire : PipeWire bascule
tout seul et le casque tombe en HFP 16 kHz mono → la musique devient inaudible — piège
déjà rencontré sur cette machine avec les Buds2 Pro). Le prétraitement (VAD, filtre,
pré-roll anti-première-syllabe) est déjà très bon.

**Cible** : l'app choisit **elle-même le meilleur micro disponible**, suit les
branchements/débranchements à chaud, et n'abîme jamais la qualité audio de sortie.

**Étapes :**
1. **Énumération et politique de choix** : au démarrage et à chaque changement de
   périphérique, lister les sources (via `sounddevice` ou mieux : `pactl`/PipeWire
   pour connaître les profils). Politique par défaut : micro casque/écouteurs
   Bluetooth **seulement si** son profil micro est utilisable sans dégrader la sortie
   (voir point 3) > micro USB/jack externe > micro interne du PC.
2. **Hot-plug** : s'abonner aux événements PipeWire (`pactl subscribe` en sous-process,
   ou bibliothèque `pulsectl`) ; quand la source choisie disparaît ou qu'une meilleure
   apparaît, **fermer et rouvrir l'InputStream sans redémarrer l'app**, avec un message
   discret (« Micro basculé sur AirPods »). Le flux Gemini Live continue, seul le
   producteur change.
3. **Piège HFP — règle d'or** : sur du Bluetooth classique, activer le micro du casque
   force le profil mains-libres → sortie 16 kHz mono. Deux stratégies à implémenter :
   - Si le casque supporte un profil moderne (mSBC/LC3, visible dans les profils
     PipeWire), l'utiliser : qualité correcte des deux côtés.
   - Sinon, **garder la sortie en A2DP (haute qualité) et prendre le micro du PC** —
     c'est presque toujours le meilleur compromis, et c'est ce que font les assistants
     sérieux. Ne jamais laisser PipeWire décider seul : figer explicitement le profil
     du casque pendant que l'assistant écoute.
4. **Réglages UI** : dans le tiroir de config, une section « Audio » : liste déroulante
   des micros (avec « Automatique » par défaut), vu-mètre en direct pour tester,
   curseur de sensibilité (existe déjà côté VAD), et un test d'écho.
5. **Qualité** : activer le module d'annulation d'écho de PipeWire
   (`module-echo-cancel`) pour la source utilisée — élimine le retour de la voix TTS
   dans le micro (le code compense aujourd'hui avec un VAD « strict » pendant que
   l'assistant parle : l'écho annulé à la source rendra l'interruption vocale bien
   plus fiable).
6. Journaliser clairement le micro effectivement ouvert (nom + taux) au démarrage —
   c'est le premier réflexe de diagnostic.

---

## E. Emails Gmail — lecture sécurisée, notifications, cartes — P1

**Aujourd'hui** : rien (prévu « Mark LI+ »).

**Cible** : « il y a du nouveau dans mes mails ? » → l'assistant lit les nouveaux
messages, les résume à voix haute, et affiche chaque email dans une **carte flottante à
droite** (expéditeur, objet, extrait, heure). Notification proactive à l'arrivée d'un
mail important. Sécurité niveau Gemini : jamais de mot de passe stocké, accès minimal.

**Étapes :**
1. **Authentification** : API Gmail officielle avec **OAuth 2.0** (flux « installed
   app » : le navigateur s'ouvre une fois, l'utilisateur consent, l'app reçoit un
   refresh token). **Scope minimal : `gmail.readonly`** pour commencer — pas d'envoi,
   pas de suppression. C'est exactement le modèle de sécurité de Gemini : consentement
   explicite, périmètre borné, révocable depuis le compte Google.
2. **Stockage des jetons** : dans le trousseau système via la bibliothèque `keyring`
   (Secret Service sous Linux) — pas en JSON en clair. Le `client_secret` OAuth peut
   rester dans `config/` avec permissions 600 (comme les clés actuelles).
3. **Nouvel outil `email_control`** (dans `actions/`) : actions `unread` (nouveaux
   messages), `search` (requête Gmail : de qui, sujet, période), `read` (corps d'un
   message précis, nettoyé du HTML), `summary` (l'outil renvoie les corps, le LLM
   résume — ne pas résumer dans l'outil). Déclarer l'outil dans `main.py` avec une
   description claire des cas d'usage.
4. **Notifications de nouveaux mails** : boucle de fond légère (polling
   `history.list` de l'API toutes les 60–120 s — suffisant et simple ; le push Pub/Sub
   est excessif pour un poste de travail). À l'arrivée : carte flottante (§A5) + phrase
   proactive courte **seulement si** l'utilisateur a activé l'option (réutiliser le
   mécanisme proactif existant de `actions/proactive.py` et son étiquette de silence).
5. **Confidentialité vis-à-vis du LLM** : n'envoyer au modèle que ce qui est nécessaire
   (en-têtes + extraits pour un tri ; corps complet uniquement sur demande explicite de
   lecture). Ajouter un réglage « emails : envoyer le contenu au modèle : jamais / sur
   demande / auto ».
6. Plus tard (P2) : scope `gmail.compose` pour dicter des **brouillons** (jamais d'envoi
   direct sans confirmation vocale explicite), et d'autres comptes (IMAP générique).

---

## F. Applications « intégrées » dans JARVIS — P2 (le plus difficile)

**Souhait** : lancer une app sans afficher sa fenêtre, dans une « belle div » à côté.

**Réalité technique à connaître avant de promettre** : sous **Wayland**, une
application ne peut pas capturer ni ré-embarquer la fenêtre d'une autre (pas de XEmbed,
pas de réparentage). Il faut donc être malin. Trois niveaux, du plus utile au plus
spectaculaire :

1. **Cartes de contrôle dédiées (recommandé, faisable, le plus « JARVIS »)** : pour
   les apps qui ont une API ou un protocole, ne pas afficher l'app du tout — afficher
   une carte ANO-GPT qui la **représente** : musique = carte lecteur (§B, déjà prévu) ;
   navigateur = carte « onglets ouverts » (le contrôle Playwright existe déjà dans
   `browser_control`) ; téléchargements = carte de progression ; terminal = §2.
   L'app tourne en arrière-plan sur un workspace spécial Hyprland
   (`hyprctl dispatch movetoworkspacesilent special:hidden`) — lancée mais invisible,
   et ANO-GPT en est la télécommande. **C'est la bonne interprétation du besoin.**
2. **Terminal intégré (faisable)** : embarquer un vrai émulateur de terminal dans une
   `FloatingPanel` (widget VTE via GTK est pénible en Qt ; préférer un petit terminal
   maison basé sur `pyte` + QPlainTextEdit pour l'affichage des sorties de
   `shell_exec` — suffisant pour voir ce que fait l'assistant).
3. **Miroir de fenêtre (spectaculaire, coûteux)** : Hyprland/wlroots exposent le
   protocole de **capture d'écran par fenêtre** (`hyprland-toplevel-export` /
   xdg-desktop-portal). On peut afficher un **flux vidéo miniature en direct** d'une
   fenêtre tournant sur le workspace caché, dans une carte ANO-GPT (lecture seule ;
   les clics ne sont pas transmis — l'interaction reste vocale via les outils existants
   `computer_control`/ydotool après focus). À ne tenter qu'après tout le reste.

**Étapes** (niveau 1, celui à faire) :
1. Ajouter à `open_app` un paramètre `hidden` : lancement + déplacement immédiat sur le
   workspace spécial (relire l'état réel après le dispatch — piège `hyprctl` « ok »).
2. Définir 3–4 cartes de contrôle prioritaires : lecteur (fait en §B), navigateur
   (onglets + page courante + bouton « montrer »), téléchargements/fichiers récents.
3. Bouton « faire apparaître » sur chaque carte : ramène la vraie fenêtre au premier
   plan quand l'utilisateur veut la voir.

---

## G. Améliorer toutes les actions — feedback riche et fiabilité — P1

Passer chaque outil de `actions/` au niveau supérieur, avec un fil conducteur : **chaque
action doit avoir un retour visuel dans l'UI** (carte §A5) en plus de la voix.

| Outil | Amélioration |
|---|---|
| `web_search` | Rendre les résultats en carte riche (titres cliquables, favicons, images) au lieu du texte brut dans le panneau bas ; garder le fallback DDG. |
| `weather_report` | Carte météo visuelle (icônes conditions, prévision 3 jours, min/max) — les données existent déjà. |
| `reminder` | Carte listant les rappels actifs avec suppression au clic ; confirmation visuelle à la création. |
| `system_monitor` | Fusionner avec la télémétrie flottante (§A2) ; alertes sous forme de carte + phrase courte. |
| `capture_control` | Après capture/vision : afficher la miniature de ce que l'IA « regarde » dans une petite carte (transparence sur ce qui est envoyé). |
| `shell_exec` | Les confirmations de commandes risquées passent par une carte oui/non (§A5) au lieu du seul canal vocal ; afficher la sortie dans le terminal intégré (§F.2). |
| `browser_control` | Carte d'état (page courante, action en cours) pendant les automatisations Playwright — l'utilisateur voit ce que fait l'IA. |
| `file_processor` | Progression visible pour les gros fichiers (carte tâche avec pourcentage). |
| `send_message` | Toujours une carte de prévisualisation + confirmation avant envoi. |
| `open_app` / `close_app` | Ajouter le mode `hidden` (§F) ; consolider — il y a 4 fichiers qui se recouvrent (`open_app`, `close_app`, `close_app_smart`, `app_control`, `desktop_apps`) : fusionner en un module unique avec une seule source de vérité. |
| `music_control` | §B. |
| `show_map` | §C. |
| *(nouveau)* `email_control` | §E. |

**Transverse** :
- **Latence perçue** : dès qu'un outil > 1 s démarre, l'orbe passe en état « action » et
  une carte tâche apparaît — l'utilisateur ne doit jamais se demander si ça a marché.
- **Vérification post-action** : généraliser le principe « relire l'état réel » (déjà
  appliqué aux fenêtres Hyprland) à toutes les actions système : volume, luminosité,
  lancement d'app (vérifier que le process/la fenêtre existe avant de dire « c'est fait »).
- **Erreurs** : chaque échec d'outil → carte d'erreur claire + suggestion, jamais un
  silence ni un simple log console.

---

## H. Robustesse & architecture — P2 (mais commencer tôt)

1. **Découper `ui.py` (4 500 lignes) en paquet `ui/`** : `theme.py` (couleurs, styles,
   accent), `orb.py`, `panels/` (un fichier par panneau flottant), `overlays/`
   (setup, customize, ai_config, remote), `cards.py` (§A5), `main_window.py`.
   Même chose pour `main.py` : extraire les déclarations d'outils (~700 lignes) dans
   `core/tool_schemas.py` et le dispatch dans `core/dispatcher.py`. **À faire au début
   du chantier UI (A1–A5), pas après** : refondre l'UI dans un monolithe de 4 500
   lignes multiplierait les conflits.
2. **Tests** : le dossier `tests/` du projet est vide de tests UI ; ajouter au minimum
   des tests des modules purs (music IPC, geolocation, email parsing, politique micro)
   — pas besoin de tester Qt.
3. **Watchdog session** : la session Gemini Live peut tomber (réseau, quota) ;
   reconnexion automatique avec backoff **et** signal visuel (orbe en état « hors
   ligne », carte explicative) au lieu d'un crash ou d'un silence.
4. **Config unifiée** : `api_keys.json` mélange clés, préférences UI et identité —
   séparer secrets (keyring/`secrets.env` 600) et préférences (`config.toml`).
5. **Nettoyage** : supprimer `ui.py.bak` (157 Ko) du dépôt ; consolider les 5 fichiers
   d'apps qui se chevauchent (cf. tableau §G) ; les `*.md` de log historiques
   (FIXES_LOG, POWER_UPGRADE…) peuvent aller dans `docs/`.
6. **Démarrage** : viser < 2 s avant l'orbe à l'écran — différer les imports lourds
   (Playwright, yt-dlp) au premier usage réel s'ils ne le sont pas déjà.
7. **Énergie** : sur batterie, réduire le framerate de l'orbe et l'intervalle de
   télémétrie (la machine a déjà un profil énergie soigné — ne pas le ruiner).

---

## I. Feuille de route condensée

**Phase 1 — « Ça a de la gueule » (1 sprint)**
A3 boutons icônes → B musique headless + carte lecteur → A1 texte au centre.

**Phase 2 — « C'est JARVIS » (1–2 sprints)**
H1 découpage `ui.py` (préalable) → A4 orbe GPU → A2 tout flottant → A5 cartes droite → A6 polissage.

**Phase 3 — « Il sait tout faire » (2 sprints)**
D micro intelligent → C carte lieux+prix → E emails → G refonte du feedback de toutes les actions.

**Phase 4 — « Au-delà »**
F apps intégrées (cartes de contrôle puis miroir de fenêtre) → H reste (watchdog, tests, config).

---

## J. Ce qu'il ne faut PAS faire

- **Ne pas changer de LLM pour la voix** : la voix temps réel EST Gemini Live. Les
  autres fournisseurs restent des secours texte.
- **Ne pas réécrire l'app en web/Electron** : PyQt6 est le bon choix ici (accès système
  direct, latence, intégration Hyprland). La refonte est visuelle, pas de plateforme.
- **Ne pas casser les chemins Wayland durement acquis** : socket IPC pour les
  raccourcis, capture d'écran spécifique, lancement audio avec l'environnement préparé
  (`_build_env` de music.py), vérification post-`hyprctl`.
- **Ne pas stocker de secrets en clair** (jetons Gmail → keyring).
- **Ne pas afficher de fenêtre de lecteur** pour de l'audio — plus jamais.

---

## K. Fonctionnalités nouvelle génération — ce qui fera d'ANO-GPT le meilleur JARVIS actuel

> Idées au-delà du plan principal. Chacune est réalisable sur cette machine
> (Arch + Hyprland + Gemini Live) et pensée pour s'appuyer sur ce qui existe déjà.
> À implémenter **après** les phases 1–3 de la feuille de route, sauf mention contraire.

### K1. Conscience d'écran permanente (« il voit ce que je fais ») — P1, impact énorme

Aujourd'hui la vision n'existe que sur demande (`capture_control`). Le vrai JARVIS sait
en permanence dans quel contexte tu es.

1. Boucle de fond légère qui relève toutes les ~10 s le **contexte actif** sans capture
   d'image : fenêtre focalisée, titre, workspace, app (via `hyprctl` — coût quasi nul).
2. Injecter ce contexte dans le prompt système dynamique (le mécanisme « Dynamic live
   system context » existe déjà dans `main.py`) : l'assistant peut alors répondre à
   « c'est quoi ce fichier ? », « aide-moi avec ça » sans qu'on lui explique où on est.
3. Sur demande explicite seulement, monter d'un cran : capture d'écran + OCR local
   (l'envoi d'image à Gemini existe déjà) — jamais de capture continue sans accord,
   et un indicateur visuel « l'IA regarde » (petite pastille œil) obligatoire.
4. Croiser avec le presse-papiers (le `ClipboardPanel` existe) : « traduis ce que je
   viens de copier » fonctionne déjà, généraliser à « de quoi ça parle ? ».

### K2. Mémoire vectorielle + continuité de session (« il se souvient de tout ») — P1

La mémoire actuelle est un JSON clé-valeur. Le meilleur JARVIS se souvient des
conversations, des projets et des habitudes.

1. Ajouter une **mémoire sémantique** : embeddings locaux (modèle sentence-transformers
   léger ou API Gemini embeddings) + base SQLite avec extension vectorielle
   (`sqlite-vec` — zéro serveur, un seul fichier).
2. Y verser automatiquement : résumés de fin de session (générés par le LLM), faits
   appris (« l'utilisateur travaille sur E.A.S Sarlu »), préférences corrigées.
3. Au début de chaque tour, récupérer les 3–5 souvenirs les plus pertinents pour la
   requête et les injecter dans le contexte — l'assistant « se souvient » sans gonfler
   le prompt.
4. **Continuité quotidienne** : au premier lancement du jour, un mot sur ce qui était
   en cours hier (« On en était à la refonte de l'orbe, tu veux continuer ? »).
5. Commande vocale de gestion : « oublie ça », « souviens-toi que… », « qu'est-ce que
   tu sais sur moi ? » (transparence totale sur ce qui est stocké).

### K3. Mode agent autonome multi-étapes (« fais-le, débrouille-toi ») — P1

Aujourd'hui chaque outil est un coup unique. Le niveau supérieur : « trouve-moi un
resto italien ouvert ce soir, regarde les avis, et mets un rappel à 19 h » exécuté en
chaîne sans re-solliciter l'utilisateur.

1. Ajouter un outil `run_plan` : le LLM soumet un plan (liste d'étapes outils), le
   moteur les exécute séquentiellement avec le résultat de chaque étape réinjecté.
2. Affichage : carte tâche (§A5) avec les étapes qui se cochent en direct — on **voit**
   l'assistant travailler, comme un vrai JARVIS.
3. Garde-fous : nombre d'étapes plafonné, toute étape destructive (suppression, envoi,
   achat) suspend le plan et demande confirmation vocale, bouton stop sur la carte.
4. Les longues tâches tournent en fond : l'utilisateur peut continuer à parler d'autre
   chose pendant que le plan avance (l'architecture asyncio de `main.py` le permet).

### K4. Routines & automatisations (« tous les matins à 8 h… ») — P1, facile

1. Nouvel outil `routine_control` : créer/lister/supprimer des routines déclenchées par
   l'heure (systemd timers — l'infra `reminder.py` en fait déjà), par un événement
   (démarrage, retour de veille, connexion d'un périphérique) ou à la voix
   (« routine travail »).
2. Une routine = une séquence d'actions existantes : « le matin » = briefing + musique
   douce + ouvrir les apps de travail sur les bons workspaces.
3. Création **par la voix** : « chaque vendredi à 17 h, rappelle-moi de sauvegarder et
   mets ma playlist » — le LLM traduit en routine, carte de confirmation avant
   enregistrement.

### K5. Téléphone intégré via KDE Connect (« mon téléphone dans JARVIS ») — P2

KDE Connect tourne très bien sous Hyprland et expose un bus D-Bus complet.

1. Lire les **notifications du téléphone** → cartes flottantes à droite (mêmes cartes
   que les emails) : « tu as reçu un SMS de… ».
2. Actions : répondre à un SMS à la voix, retrouver le téléphone (sonnerie), voir la
   batterie, envoyer/recevoir des fichiers.
3. Présence : téléphone qui se déconnecte du réseau = utilisateur parti → passer en
   veille/muet automatiquement ; il revient = accueil.

### K6. Traduction et transcription en direct — P2

1. **Mode interprète** : « traduis ce que je dis en anglais » → l'assistant répète en
   direct dans l'autre langue (Gemini Live le fait nativement, c'est presque gratuit à
   implémenter — c'est surtout un mode de prompt + une carte UI dédiée).
2. **Transcription de réunion** : « prends des notes » → capture de l'audio système
   (moniteur PipeWire, la brique existe dans `capture_control`), transcription
   continue, et à la fin résumé + points d'action dans une carte et un fichier Markdown.

### K7. Contrôle domotique (Home Assistant) — P2

Si un jour des ampoules/prises connectées arrivent : l'API REST/WebSocket de
**Home Assistant** est le standard. Un outil `home_control` (allumer, éteindre, scènes,
température) + cartes d'état des pièces. À ne construire que si l'équipement existe —
mais prévoir la place dans l'architecture des outils (c'est un plugin naturel, cf. K10).

### K8. Identification du locuteur & mode invité — P2, différenciant

1. **Empreinte vocale locale** (bibliothèques de speaker-embedding légères type
   resemblyzer) : l'assistant sait si c'est TOI qui parles.
2. Si voix inconnue : mode invité — pas d'accès aux emails, à la mémoire personnelle ni
   au shell ; il le dit poliment (« je ne reconnais pas votre voix »).
3. Répond au piège du micro ouvert : la télé ou un ami ne peuvent pas déclencher
   d'actions sensibles. C'est LE genre de détail qui rend l'assistant crédible.

### K9. Présence & réactions ambiantes (« il sent que tu es là ») — P2

1. Détection de retour de veille / verrouillage (D-Bus logind) : accueil au réveil de la
   machine (« Bon retour. Pendant ton absence : 2 emails, 1 rappel »).
2. Résumé d'absence : ce qui s'est passé (notifications, mails, fin de tâches) pendant
   que la session était verrouillée, livré en une phrase + cartes.
3. Ambiance sonore optionnelle : sons d'interface discrets (activation, confirmation,
   erreur) façon film — un pack de 5–6 sons courts, désactivable, volume lié au mixeur.

### K10. Système de plugins (« ajouter un pouvoir sans toucher au cœur ») — P2, stratégique

1. Formaliser ce que `actions/` fait déjà implicitement : un plugin = un dossier avec
   un manifeste (nom, description pour le LLM, schéma des paramètres, permissions
   requises) + son module Python. Chargement automatique au démarrage, activable dans
   les réglages.
2. Ajouter le support **MCP client** (Model Context Protocol) : des centaines de
   serveurs d'outils existants (GitHub, Notion, Spotify, bases de données…) deviennent
   branchables sans écrire de code — c'est le standard qui s'impose en 2026, et c'est
   le chemin le plus court vers « un assistant qui peut tout faire ».
3. Chaque plugin déclare ses permissions (réseau, fichiers, shell) et l'utilisateur
   les voit à l'activation — même philosophie que les scopes Gmail.

### K11. Tableau de bord vocal du développeur — P3 (très « toi »)

1. Outil `dev_status` : état des dépôts de `~/OUTILS` (branche, fichiers modifiés,
   derniers commits), « où j'en étais sur E.A.S ? » répondu depuis git + mémoire K2.
2. Lancement de tâches projet à la voix (« lance les tests de l'app web ») avec sortie
   dans le terminal intégré (§F.2) et verdict vocal court (« 35 tests, tout passe »).
3. Le `dev_agent.py` existant peut devenir le moteur ; il lui manque surtout le
   feedback visuel et l'ancrage sur tes projets réels.

### K12. Générations visuelles et audio dans les cartes — P3, effet waouh

1. **Images** : « génère-moi un logo » → appel API d'images (Gemini/Imagen) → la carte
   affiche le résultat, boutons enregistrer/régénérer/fond d'écran.
2. **Graphiques** : « montre-moi l'utilisation CPU de la journée » → graphe tracé dans
   une carte (les données de `system_monitor` peuvent être historisées en SQLite).
3. **Musique d'ambiance générée** ou radios par humeur (« mets quelque chose de
   calme ») — simple mapping humeur → playlists/flux, joué par le lecteur headless (§B).

### Priorisation de la section K

| Rang | Fonction | Pourquoi d'abord |
|---|---|---|
| 1 | K1 conscience d'écran | Coût faible, transforme chaque conversation |
| 2 | K2 mémoire vectorielle | Rend tout le reste plus intelligent |
| 3 | K3 mode agent | Le saut « assistant → majordome » |
| 4 | K4 routines | Facile, utilité quotidienne immédiate |
| 5 | K10 plugins + MCP | Démultiplie les capacités sans effort ensuite |
| 6 | K5 téléphone | Très concret, dépend d'installer KDE Connect |
| 7 | K8 voix reconnue | Sécurité + crédibilité |
| 8 | K6, K9, K7, K11, K12 | Selon l'envie du moment |

---

## L. Encore plus loin — deuxième vague de fonctionnalités

> Complément de la section K. Toujours la même règle : s'appuyer sur les briques
> existantes, respecter les contraintes machine (§0), aucun doublon avec A–K.

### L1. Calendrier Google + planification intelligente — P1 (le grand absent)

Les emails sont couverts (§E) mais pas l'agenda — c'est pourtant la moitié d'un
majordome.

1. Même modèle qu'§E : API Google Calendar, OAuth scope lecture seule d'abord, jetons
   dans le keyring. Un seul consentement peut couvrir Gmail + Calendar.
2. Outil `calendar_control` : agenda du jour/semaine, prochain événement, recherche
   (« c'est quand mon rendez-vous chez le dentiste ? »), créneaux libres
   (« quand suis-je libre jeudi ? »).
3. Intégrer au briefing du matin (« deux réunions aujourd'hui, la première à 10 h »)
   et aux cartes flottantes : rappel visuel + vocal 10 min avant un événement.
4. Phase 2 : création d'événements à la voix avec carte de confirmation avant écriture
   (scope écriture séparé, ajouté seulement à ce moment-là).

### L2. Mini-orbe permanent (mode compagnon) — P1, très « JARVIS »

Aujourd'hui l'assistant vit dans sa grande fenêtre. Le compagnon idéal reste visible
en permanence sans gêner.

1. Une deuxième fenêtre minuscule (~90 px) : l'orbe seul, sans bordure, positionnée
   en coin d'écran **au-dessus de tout** via le protocole **wlr-layer-shell**
   (le standard Wayland des overlays — c'est ainsi que fonctionnent les barres et
   widgets Hyprland/Caelestia de cette machine).
2. Elle reflète l'état en temps réel (écoute/parle/travaille) même quand la fenêtre
   principale est fermée ou sur un autre workspace ; clic = micro ; double-clic =
   ouvrir la grande fenêtre.
3. Les phrases courtes de l'assistant peuvent s'afficher en bulle éphémère à côté du
   mini-orbe — répondre à une question rapide sans jamais ouvrir l'interface.
4. Techniquement : la même app Qt, une seconde `QWindow` ; le layer-shell s'obtient
   par le plugin Qt `qt6-wayland` + règles Hyprland si besoin.

### L3. Dictée universelle système — P1, facile et très utile

« Écris pour moi » dans **n'importe quelle application** (navigateur, éditeur, chat).

1. Mode dictée activable à la voix ou raccourci : la transcription (le pipeline STT
   existe) est **tapée dans la fenêtre focalisée** via `ydotool` — déjà installé et
   calibré sur cette machine (voir la mémoire : coordonnées à corriger en 2x+1 pour la
   souris ; la frappe clavier, elle, est fiable).
2. Ponctuation dictée (« virgule », « à la ligne ») et commandes (« efface la dernière
   phrase », « envoie » = touche Entrée).
3. Indicateur clair du mode (mini-orbe en teinte spéciale) pour toujours savoir si on
   dicte ou si on parle à l'assistant.

### L4. « Lis ce qui est à l'écran » — OCR à la demande — P1

1. Outil `read_screen` : capture (le chemin Wayland fiable existe dans
   `capture_control`) + OCR local (**Tesseract**, léger, hors-ligne) → le texte de la
   fenêtre ou d'une zone.
2. Usages : « copie le code d'erreur », « lis-moi ce paragraphe », « traduis ce qui est
   affiché » — sans envoyer d'image au cloud quand un simple texte suffit
   (plus rapide et plus privé que la vision Gemini pour du texte).
3. Combiner avec la sélection : « lis la fenêtre de gauche » → cibler par géométrie
   Hyprland.

### L5. Recherche documentaire locale (RAG personnel) — P1

« Retrouve le PDF où on parle du contrat serveur » — sur **tes** fichiers, en local.

1. Indexation en tâche de fond des dossiers choisis (Documents, OUTILS…) : extraction
   texte (PDF/DOCX/MD/TXT — `file_processor` sait déjà lire tout ça) + embeddings dans
   la même base vectorielle que K2.
2. Outil `find_document` : recherche sémantique + mots-clés, résultats en carte
   (fichier, extrait pertinent, boutons ouvrir/dossier).
3. Enchaînement naturel avec `file_processor` : « ouvre-le et résume la section 3 ».
4. Respect batterie : indexer seulement sur secteur, la nuit ou à la demande.

### L6. Résumé de vidéos YouTube et d'articles — P2, facile

1. « Résume cette vidéo » : récupérer les **sous-titres** via yt-dlp (déjà présent pour
   la musique) — pas besoin de télécharger la vidéo — et faire résumer par le LLM ;
   carte avec les points clés + horodatages cliquables.
2. Même chose pour un article/URL (« résume la page ouverte » : l'URL vient du contrôle
   navigateur existant).
3. Mode « écoute » : le résumé peut être lu à voix haute pendant qu'on fait autre chose.

### L7. Veille et alertes de prix généralisées — P2

`flight_finder` surveille les vols ; généraliser à tout produit.

1. Outil `price_watch` : « préviens-moi si la PS5 passe sous 400 € » → vérification
   périodique (mode `price` de `web_search`, via les timers de `reminder.py`), alerte
   en carte + voix quand le seuil est franchi.
2. Carte « mes veilles » : liste, dernier prix relevé, tendance, suppression au clic.
3. Même mécanique pour la veille d'actualité : « tiens-moi au courant des news sur X »
   → vérification quotidienne, digest en carte.

### L8. Santé du système & maintenance proactive — P2 (très Arch)

L'assistant devient l'administrateur bienveillant de la machine.

1. Vérifications périodiques discrètes : mises à jour pacman en attente (et alertes
   `archlinux.org` critiques), disque qui se remplit (> 90 %), santé SMART, services
   systemd en échec, température anormale prolongée.
2. Signalement en carte + une phrase, **jamais d'action automatique** : les mises à
   jour et nettoyages (cache pacman, journaux, corbeille) se font sur confirmation via
   `shell_exec` et sa gestion du risque existante.
3. « Fais le ménage » : séquence guidée nettoyage cache/orphelins avec bilan d'espace
   récupéré.

### L9. Historique des conversations consultable — P2

1. Enregistrer chaque échange (horodaté, avec les outils appelés) en SQLite locale —
   complément conversationnel de la mémoire sémantique K2.
2. « Qu'est-ce que tu m'as dit hier sur les shaders ? » → recherche dans l'historique
   et réponse sourcée (« hier à 22 h 14, je t'avais proposé… »).
3. Carte « historique » navigable + export Markdown d'une conversation ; commande
   « efface l'historique de la journée » (contrôle total de l'utilisateur).

### L10. Mode hors-ligne dégradé — P2

Sans réseau, l'assistant actuel est muet. Objectif : qu'il reste utile.

1. Détection de perte réseau → bascule annoncée (« mode local : je t'entends toujours,
   mais je réfléchis moins vite »).
2. STT local (Vosk est déjà là pour le wake word ; ou whisper.cpp petit modèle),
   LLM local via Ollama (installé sur la machine — faible, mais suffisant pour les
   commandes : ouvrir des apps, musique locale, volume, minuteurs), TTS local (Piper,
   voix française correcte, hors-ligne).
3. Les outils purs système (apps, fenêtres, musique locale, fichiers, shell) marchent à
   100 % hors-ligne — c'est surtout un problème de routage, pas de capacités.
4. Retour du réseau → retour à Gemini Live annoncé d'une phrase.

### L11. Minuteurs, alarmes et compte à rebours visibles — P2, facile

Basique mais utilisé tous les jours, et distinct des rappels (`reminder.py` = notification
système à une date ; ici = compte à rebours vivant dans l'UI).

1. « Minuteur 10 minutes », « réveille-moi dans 20 minutes » → carte flottante avec
   compte à rebours animé, pause/annulation au clic ou à la voix.
2. Fin de minuteur : son + voix + l'orbe pulse jusqu'à accusé de réception ; baisser
   automatiquement la musique (§B) pendant l'annonce.
3. Multiples minuteurs nommés (« minuteur pâtes », « minuteur machine ») — c'est le
   détail cuisine qui rend un assistant vocal réellement adopté.

### L12. Macros vocales personnalisées — P2

Différent des routines planifiées (K4) : ici des **raccourcis d'expression**.

1. « Quand je dis "mode ciné", éteins les notifications, mets le son à 60 % et ouvre
   le vidéoprojecteur de fenêtre » — enregistré comme macro déclenchée par la phrase.
2. Stockage simple (JSON/TOML) ; le LLM fait la correspondance phrase → macro avec
   tolérance de formulation (pas de correspondance exacte requise).
3. Gestion : « liste mes macros », « supprime mode ciné » ; carte récapitulative.

### L13. Mode focus / ne pas déranger — P3

1. « Mode focus 1 h » : couper les cartes non urgentes, silence proactif total, statut
   dans le mini-orbe ; option blocage de sites distrayants (fichier hosts ou extension
   navigateur via `browser_control`) sur confirmation.
2. À la fin : bilan discret (« 1 h de focus, 3 notifications retenues, les voici »).
3. S'active aussi automatiquement en plein écran/réunion (fenêtre visioconf détectée
   via K1).

### L14. Bilan de fin de journée — P3, gratifiant

1. Sur demande le soir (« ma journée ? ») : synthèse croisée — apps les plus utilisées
   (K1 en donne la matière), tâches/rappels accomplis, mails traités, musique écoutée,
   temps de focus.
2. Une carte élégante + trois phrases à voix haute, ton majordome (« Journée dense,
   monsieur. Demain : deux réunions. »).
3. Archivage optionnel en Markdown quotidien — journal de bord automatique.

### Priorisation de la section L

| Rang | Fonction | Pourquoi |
|---|---|---|
| 1 | L1 calendrier | Complète §E — le duo email+agenda est le cœur d'un majordome |
| 2 | L2 mini-orbe permanent | Présence constante = sensation JARVIS maximale |
| 3 | L3 dictée universelle | Facile (ydotool prêt), utile dix fois par jour |
| 4 | L4 OCR écran | Rapide, privé, débloque plein de micro-usages |
| 5 | L5 RAG local | Puissant, s'appuie sur K2 |
| 6 | L11 minuteurs | Facile, adoption quotidienne immédiate |
| 7 | L6, L7, L8, L9 | Confort et puissance, ordre au choix |
| 8 | L10 hors-ligne | Filet de sécurité, à faire quand le reste est stable |
| 9 | L12, L13, L14 | Finitions de caractère |
