# Localisation et recherche de lieux

## Une seule carte, deux rendus

ANO-GPT n'affiche qu'**une** carte : le conteneur plein cadre. Elle sert à la
fois pour un point unique (« montre-moi Kaloum ») et pour une liste de
résultats, épinglés et numérotés — et depuis 2026-09-14, elle a deux moteurs
de rendu choisis par contexte, jamais deux cartes séparées.

Auparavant deux cartes coexistaient : le conteneur plein cadre affichait une
iframe OpenStreetMap claire avec un seul marqueur, tandis que les résultats de
recherche ouvraient un second panneau flottant de 520 × 420. Le petit
recouvrait le grand, et les deux rendus n'avaient rien en commun.

| Outil vocal | Effet |
| --- | --- |
| `show_map` | centre la grande carte sur un point, nomme le quartier réel |
| `show_country_info` | fiche flottante pour n'importe quel pays du monde |
| `find_nearby` | épingle des résultats dans la même carte |
| `navigate` | guidage GPS pas-à-pas parlé |
| `close_map` | referme le conteneur |

## Carte (Leaflet) ou globe (globe.gl) — au choix

Par défaut, `show_map` affiche la carte de rues (Leaflet) — comme avant.
Un second rendu existe : un globe 3D « world monitor » (textures nuit/relief/
étoiles réelles, tir radar à l'arrivée), pour l'aperçu et les recherches.
Il **ne remplace jamais** la carte de rues pour le guidage pas-à-pas : un
globe n'a pas de tuiles de rue, donc dès qu'un guidage démarre, la page
recharge automatiquement en Leaflet, quelle que soit la vue affichée avant.

Trois façons de basculer entre les deux, toutes équivalentes :

- **bouton d'en-tête** — 🌐 GLOBE / 🗺️ CARTE, en haut à droite de la carte ;
  rejoue la même position/les mêmes lieux dans l'autre rendu, sans nouvelle
  requête GPS ;
- **à la voix, en le demandant** — « montre ça en globe », « passe en
  carte », « vue satellite » (paramètre `view` de `show_map` /
  `show_country_info` : `"carte"` ou `"globe"`) ;
- **à la voix, implicitement** — sans rien préciser, la vue déjà affichée à
  l'écran est conservée d'un appel à l'autre.

Le globe ne tourne jamais tout seul en continu : une animation en boucle
coûte le CPU au thread audio sur une machine à deux cœurs (même règle que la
grille CSS de la carte Leaflet, coupée pour la même raison). Il reste
manipulable à la souris/au doigt à tout moment (glisser pour orbiter,
molette pour zoomer) — seul le mouvement *automatique* est absent.

## Fiche pays — n'importe lequel, pas seulement la Guinée

« montre-moi les infos sur le Japon », « météo au Sénégal en ce moment »,
« capitale du Brésil » → `show_country_info(country=...)`. Sans pays précisé,
la fiche porte sur la Guinée par défaut.

Affichée en panneau flottant en haut à droite (carte ou globe, même style) :
drapeau, capitale, population, monnaie, langues (en français), fuseau
horaire, superficie, pays frontaliers, indicatif téléphonique, météo actuelle
à la capitale.

Sources, sans clé API : identité du pays (capitale, monnaie, langues,
frontières…) depuis un jeu de données local vendorisé
(`config/countries.json`, 250 pays, licence ODbL — voir
`config/countries.json.LICENSE`), parce que restcountries.com a fermé son
accès libre en 2026. Population (Banque mondiale) et météo (Open-Meteo) sont
les seules données récupérées en direct — la fiche reste utile même si l'une
des deux échoue.

## Sources de recherche

`core/places.py` interroge, dans cet ordre :

1. **SerpAPI, moteur Google Maps** — note, nombre d'avis, adresse, téléphone,
   horaires, type d'établissement. Indispensable là où OpenStreetMap est
   clairsemé, Conakry la première.
2. **Overpass (OpenStreetMap)** — gratuit, sans clé, interrogé **en plus** de
   SerpAPI : il connaît des commerces de quartier absents de Google, et
   l'inverse est vrai aussi.
3. **Nominatim** — uniquement pour situer une zone nommée (`near`).

Les doublons entre sources sont fusionnés (même nom à moins de 50 m), la fiche
la plus complète l'emportant. Les résultats sont triés par distance réelle.

## Activer SerpAPI

Sans clé, seul OpenStreetMap répond, et l'assistant le dit explicitement au
lieu de laisser croire que le lieu n'existe pas.

```json
// config/api_keys.json
{
  "serpapi_api_key": "votre-clé"
}
```

La clé se crée sur <https://serpapi.com/>. Le champ existait déjà dans
`api_keys.example.json` mais **n'était lu par aucun code** avant cette version.

## Recherche en mots simples

Il n'y a plus de liste de catégories imposée. `core/places.py` traduit les
mots courants — en français comme en anglais — vers les étiquettes
OpenStreetMap : pharmacie, hôpital, médecin, restaurant, café, bar, hôtel,
banque, distributeur, essence, supermarché, boulangerie, marché, électronique,
vêtements, librairie, école, police, poste, lieu de culte, quincaillerie,
coiffeur, garage, transport. Un mot inconnu déclenche un ratissage large,
filtré ensuite sur le terme cherché.

L'ancienne version n'acceptait que huit catégories anglaises (`electronics`,
`supermarket`, `pharmacy`, `books`, `clothing`, `bakery`, `restaurant`,
`hardware`), ce qui rendait muette la moitié des demandes réelles.

## Chercher ailleurs qu'autour de soi

```
« trouve-moi des hôtels à Kindia »   → find_nearby(query="hôtel", near="Kindia")
```

## D'où vient la position

`core/geolocation.py` connaît deux niveaux de précision, et ne les confond
jamais :

- **`get_precise_user_coords()`** — GPS réel du téléphone via ANO Remote
  uniquement (~10 m), jamais plus vieux que l'âge demandé. C'est la seule
  source acceptée pour `show_map` (« ma position »), `find_nearby` (« autour
  de moi ») et `navigate` (point de départ du guidage). Sans relevé frais,
  ces trois outils **refusent** et le disent explicitement — ils ne
  retombent plus jamais sur une position IP ou une ville configurée. Une
  navigation « guide-moi vers Kaloum » démarrée depuis une position IP a
  déjà annoncé 2900 km d'écart : ce n'est pas une dégradation acceptable.
- **`get_user_coords()`** — position IP ou ville configurée en repli, pour
  des usages où l'approximation est admissible (météo par défaut). Jamais
  utilisée pour dire à l'utilisateur où il est.

`navigate` redemande activement un relevé GPS au téléphone avant de démarrer
(sauf pour un statut ou un arrêt), avec le même refus explicite si rien
d'assez frais n'arrive.

## Le nom du lieu, pas juste les coordonnées

`reverse_geocode(lat, lon)` (Nominatim/OSM, sans clé) traduit des
coordonnées en quartier + pays — utilisé par `show_map` pour que « montre ma
position » nomme le quartier réel plutôt qu'une ville devinée par le modèle
depuis sa culture générale. Deux garde-fous, après qu'un mauvais résultat mis
en cache soit resté faux indéfiniment :

- **TTL de 30 jours** sur le cache (`config/geocode_cache.json`) — un
  résultat erroné peut désormais se corriger tout seul ;
- **rejet des réponses trop éloignées** — si Nominatim recale sur une
  feature à plus de 25 km du point demandé (index clairsemé en zone rurale),
  la réponse est écartée plutôt que d'annoncer un lieu faux.

Le géocodage direct (`geocode(place, country_code=...)`, un nom → des
coordonnées) accepte un biais pays : sans lui, un nom générique comme
« Kaloum » (qui existe aussi ailleurs dans le monde) a déjà répondu un
village à des milliers de km plutôt que le quartier de Conakry. `navigate`
déduit ce pays de la position de départ déjà connue. Repli sur Nominatim si
Open-Meteo (le géocodeur principal, orienté villes) ne connaît pas le lieu à
cette échelle — un quartier ou une commune, typiquement.

## Rendu

`core/map_render.py` produit la page Leaflet, hors de l'interface, donc
testable sans lancer Qt :

- fond sombre CARTO, assorti au HUD ;
- pins numérotés correspondant à l'ordre de la réponse parlée ;
- fiche au clic : note, avis, distance, adresse, horaires, téléphone,
  itinéraire ;
- cadrage automatique sur l'ensemble des résultats — un lieu hors écran est un
  lieu qu'on croit absent ;
- échappement JSON des noms : « L'Escale » cassait autrefois le script.
