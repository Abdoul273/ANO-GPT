# ANO-GPT — Bouclier Anti-Distraction

## Fonctionnement réel

Le gardien est désactivé par défaut. Il commence uniquement après une demande
explicite contenant un objectif concret.

Pendant la session, il :

- observe localement les changements de fenêtre ou de titre actif via Hyprland ;
- conserve seulement des empreintes temporaires, jamais les titres observés ;
- calcule les changements et contextes distincts sur une fenêtre de dix minutes ;
- intervient après 12 changements sans progression récente, seuil configurable ;
- considère huit minutes stables comme un indice de progression sans lire le contenu ;
- bloque les flux infinis dont l'URL correspond exactement aux règles actives ;
- programme les cycles de travail et de pause ;
- protège cinq minutes supplémentaires lorsqu'un état de flow stable est détecté.

## Flux bloqués

- YouTube Shorts ;
- Instagram Reels ;
- Facebook Reels ;
- accueil et exploration de X/Twitter ;
- flux principal TikTok.

Une vidéo YouTube normale, un profil Instagram ou un profil X ne sont pas
bloqués. Le filtrage automatique d'URL nécessite que le navigateur expose son
canal CDP, comme le contrôleur d'onglets existant. Sans CDP, ANO continue à
mesurer les changements Hyprland mais ne ferme jamais une fenêtre entière en
devinant son contenu.

Les onglets bloqués peuvent être rouverts avec la commande `restore` pendant le
même lancement. Leurs URL restent seulement en mémoire et ne sont pas écrites
dans le fichier d'état.

## Pauses intelligentes

La durée par défaut est de 50 minutes de travail et 10 minutes de pause.

- Si le contexte est stable au moment prévu, ANO protège cinq minutes de flow.
- Sinon, la pause commence et ANO conseille de quitter l'écran.
- À la fin, ANO annonce la reprise avec l'objectif initial.
- Une pause manuelle peut être demandée à tout moment.

Les durées sont bornées à 15–120 minutes de travail et 3–30 minutes de pause.

## Phrases de test

### Démarrer

```text
ANO, active le bouclier anti-distraction pendant 50 minutes pour terminer mon rapport, avec une pause de 10 minutes.
```

### Consulter l'état

```text
ANO, où en est ma session Focus ?
```

### Enregistrer une avancée

```text
ANO, j'ai terminé le plan du rapport, enregistre ma progression.
```

### Pause et reprise

```text
ANO, démarre ma pause Focus.
ANO, reprends ma session de concentration.
```

### Contrôler les blocages

```text
ANO, autorise temporairement les flux infinis.
ANO, rebloque les Shorts et les Reels.
ANO, restaure les onglets que le bouclier a bloqués.
```

### Arrêter immédiatement

```text
ANO, arrête complètement le bouclier anti-distraction.
```

## Vérification pratique

1. Démarrer une session Focus avec un objectif.
2. Ouvrir un navigateur lancé avec CDP puis visiter un lien `/shorts/...`.
3. Vérifier que seul cet onglet est fermé et qu'une page YouTube `/watch?...` reste ouverte.
4. Dire « restaure les onglets bloqués » et vérifier sa réouverture.
5. Arrêter le bouclier puis rouvrir Shorts : aucune action ne doit se produire.
6. Alterner rapidement entre des fenêtres pour vérifier le compteur dans `status`.

## Sécurité et confidentialité

- Aucun blocage hors session explicite.
- Arrêt et suspension du blocage disponibles immédiatement.
- Aucune fermeture en mode Hyprland dégradé sans URL certaine.
- Aucun titre de fenêtre enregistré sur disque.
- Aucune URL bloquée enregistrée sur disque.
- État expiré automatiquement après quatre heures d'inactivité.

## Fichiers concernés

- `core/distraction_guard.py`
- `main.py`
- `tests/test_distraction_guard.py`
- `JARVIS_FEATURES_ROADMAP.html`

