# Nouveautés et correctifs du 14 septembre 2026

Guide d'usage des fonctionnalités ajoutées aujourd'hui (carte, globe, fiche
pays, navigation) et des correctifs de fiabilité qui les accompagnent. Pour
le détail technique/architecture, voir `docs/LOCALISATION.md`.

## Comment tester

```fish
cd ~/OUTILS/ANO-GPT
python main.py
```

Dans un second terminal, commandes texte sans passer par la voix — pratique
pour vérifier vite :

```fish
./anogpt-ctl ask "montre ma position"
```

---

## 1. Carte ou globe, au choix

La grande carte a toujours un seul conteneur, mais deux rendus au choix :

- **carte** (Leaflet, rues) — le rendu par défaut, celui d'avant ;
- **globe** (globe.gl, 3D) — nouveau : textures satellite nuit/relief, ciel
  étoilé, un tir radar à l'arrivée sur chaque lieu.

**Trois façons de basculer :**

| Comment | Exemple |
| --- | --- |
| Bouton d'en-tête | 🌐 GLOBE / 🗺️ CARTE, en haut à droite de la carte |
| À la voix, explicitement | « montre ça en globe », « repasse en carte » |
| À la voix, implicitement | rien à dire — la vue déjà affichée est gardée |

Le globe reste manipulable à tout moment (glisser pour orbiter, molette pour
zoomer) mais **ne tourne jamais tout seul** — une rotation continue coûterait
le CPU au thread audio sur cette machine à deux cœurs.

Dès qu'un guidage démarre (`navigate`), la page recharge automatiquement en
carte de rues, même si le globe était affiché : un globe n'a pas de tuiles de
rue pour un pas-à-pas.

**⚠️ À vérifier en priorité** : ouvre le globe, laisse-le affiché 15-20 s, et
essaie de parler ou de taper une commande. Si la voix devient sourde ou lente
pendant que le globe est ouvert, dis-le — le rendu WebGL peut faire tourner
une boucle de rendu au niveau du moteur que je n'ai pas pu vérifier sans un
vrai test.

## 2. Fiche pays — n'importe lequel

```
« montre-moi les infos sur le Japon »
« météo au Sénégal en ce moment »
« la capitale du Brésil, c'est quoi ? »
```

Sans préciser de pays, c'est la Guinée par défaut. Panneau flottant en haut à
droite (carte ou globe) : drapeau, capitale, population, monnaie, langues (en
français), fuseau horaire, superficie, pays frontaliers, indicatif
téléphonique, météo actuelle à la capitale.

Fonctionne pour les 250 pays du monde, sans clé API — voir
`docs/LOCALISATION.md` pour les sources de données.

## 3. Navigation guidée — corrigée en profondeur

```
« guide-moi vers Kaloum »
« navigue vers la pharmacie la plus proche »
« où en est l'itinéraire ? »
« arrête la navigation »
```

**Condition** : ANO Remote connecté sur le téléphone, localisation autorisée.
Sans GPS précis de moins de 5 minutes, le guidage refuse de démarrer et le
dit clairement — il ne se rabat plus jamais sur une position IP ou une ville
configurée (c'est ce qui causait des distances à 2000+ km).

**Ce qui a changé aujourd'hui**, dans l'ordre où les bugs ont été trouvés :

1. Le guidage pouvait démarrer depuis une position IP au lieu du GPS réel.
2. Même avec le GPS réel, le nom de destination (« Kaloum ») pouvait être mal
   géocodé — trouvé dans un autre pays, faute de biais géographique.
3. Le chien de garde audio (protection contre une voix bloquée) coupait le
   micro à 5 secondes alors que l'outil travaillait encore en tâche de fond
   (relevé GPS, calcul d'itinéraire) — d'où des réponses incohérentes.

Les trois sont corrigés. Si « guide-moi vers X » échoue encore, le message
doit maintenant être clair et net (« aucune position GPS précise ») plutôt
qu'une réponse improvisée par le modèle.

## 4. « Montre ma position » nomme le quartier

Avant : « t'es à Conakry en Guinée » (deviné par le modèle depuis les
coordonnées, pas une donnée précise) puis une question de suivi (« dans quel
quartier ? ») partait en recherche web — qui ne peut évidemment pas savoir où
tu es.

Maintenant : la réponse nomme le quartier réel (ex. « Camayenne, Guinée »),
résolu depuis les coordonnées GPS exactes. Une question de suivi sur le
quartier se répond depuis cette même donnée, sans nouvel outil ni recherche
web.

## 5. Voix fatiguée / basse — plus permissif

Sous Python 3.14 (cette machine), le détecteur de voix principal (Silero, un
modèle IA) est désactivé par un contournement documenté d'un bug d'ONNX
Runtime — un détecteur de repli moins précis (webrtcvad) tournait seul, réglé
sur son niveau le plus strict. Une voix moins énergique (fatigue, murmure)
tombait sous ce seuil et n'était jamais transcrite, alors que le texte tapé
marchait toujours.

Le réglage est descendu d'un cran (toujours un vrai filtre de bruit, moins
strict sur une voix simplement calme). **Non vérifié en conditions réelles**
faute de micro dans cet environnement — à tester en priorité, et à signaler
dans les deux sens : si une voix basse passe enfin, mais aussi si du bruit de
fond se met à déclencher le micro à tort (signe qu'il faudrait remonter d'un
cran, `core/audio_vad.py` → `VADConfig.webrtc_mode`).

---

## Résumé des fichiers touchés

| Domaine | Fichiers |
| --- | --- |
| Carte / globe | `core/map_render.py`, `ui/window/media_host.py`, `ui/window/scene.py` |
| Fiche pays | `core/country_info.py`, `config/countries.json` |
| Navigation / GPS | `core/navigation.py`, `core/geolocation.py` |
| Chien de garde audio | `core/tool_dispatcher.py` (`_execute_tool`) |
| Détection de voix | `core/audio_vad.py` |

Tous les tests automatisés passent (`pytest`, Qt hors écran) ; les points
marqués « non vérifié » ci-dessus demandent un vrai test sur cette machine.

## 6. Couper le barge-in (interruption automatique à la voix)

« stop », « écoute », « arrête-toi » coupaient ANO en pleine phrase dès que
ces mots étaient (ou semblaient) entendus — y compris parfois à tort. Un
réglage permet de désactiver cette coupure automatique et de garder
uniquement Échap / le bouton Interrompre comme moyens volontaires de couper
la voix :

```json
// config/api_keys.json
{
  "voice_barge_in_enabled": false
}
```

**Déjà mis à `false` dans ta config actuelle.** Redémarre l'appli pour que ça
prenne effet ; le journal affiche alors « coupure automatique à la voix
désactivée » au démarrage. Pour la réactiver, remettre `true` ou supprimer la
ligne (le défaut est activé).
