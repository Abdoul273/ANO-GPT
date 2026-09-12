# Localisation et recherche de lieux

## Une seule carte

ANO-GPT n'affiche qu'**une** carte : le conteneur plein cadre. Elle sert à la
fois pour un point unique (« montre-moi Kaloum ») et pour une liste de
résultats, épinglés et numérotés.

Auparavant deux cartes coexistaient : le conteneur plein cadre affichait une
iframe OpenStreetMap claire avec un seul marqueur, tandis que les résultats de
recherche ouvraient un second panneau flottant de 520 × 420. Le petit
recouvrait le grand, et les deux rendus n'avaient rien en commun.

| Outil vocal | Effet |
| --- | --- |
| `show_map` | centre la grande carte sur un point |
| `find_nearby` | épingle des résultats dans la même carte |
| `close_map` | referme le conteneur |

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

`core/geolocation.py`, de la plus précise à la plus vague :

1. GPS du téléphone via ANO Remote (~10 m) ;
2. position IP (quartier, souvent fausse en mobile) ;
3. ville configurée dans `api_keys.json` ;
4. pays.

Aucun repli codé en dur : sans position exploitable, l'assistant le dit. Une
position inventée renverrait des commerces à des milliers de kilomètres sans
que rien ne le signale.

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
