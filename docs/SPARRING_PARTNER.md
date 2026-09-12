# ANO-GPT — Sparring Partner vocal

## Ce qui est réellement implémenté

Le mode Sparring Partner est une session structurée, conservée entre les tours
de parole. ANO-GPT adopte un rôle, pose une seule question, attend la réponse,
analyse celle-ci, formule une objection réaliste puis poursuit jusqu'au rapport.

Scénarios disponibles :

- recruteur technique exigeant ;
- recruteur d'embauche ;
- client difficile, sceptique et pressé ;
- examinateur technique rigoureux.

La difficulté `débutant`, `intermédiaire`, `avancé` ou `expert` change réellement
la pression des relances. Une session contient de 3 à 8 réponses et peut être
mise en pause, reprise ou terminée immédiatement.

## Analyse d'élocution

Pour chaque transcription finale, le moteur mesure :

- le nombre de mots et la longueur de réponse ;
- les hésitations comme « euh », « hum », « du coup » ou « en fait » ;
- les répétitions immédiates ;
- les mots excessivement répétés ;
- les marqueurs de structure comme « d'abord », « par exemple » et « résultat » ;
- le débit en mots par minute lorsque la durée vocale réelle est disponible.

Le rapport distingue volontairement la forme et le fond. Le score numérique
porte sur l'élocution mesurable. Gemini évalue la pertinence métier à partir du
rôle, de l'objectif et du critère caché de chaque question. En mode texte, ANO
indique que le débit n'est pas mesuré au lieu d'en inventer un.

## Phrases pour le tester

### Entretien technique

```text
ANO, entraîne-moi pour mon entretien technique de demain, niveau expert, en cinq questions.
```

Répondre naturellement à chaque question, puis dire :

```text
Termine l'entraînement et donne-moi mon rapport complet.
```

### Entretien d'embauche

```text
ANO, joue un recruteur exigeant pour un poste d'administrateur système Linux.
```

### Client difficile

```text
ANO, entraîne-moi face à un client très mécontent à cause d'une livraison en retard.
```

### Oral technique

```text
ANO, fais-moi passer un oral technique sur les réseaux informatiques, niveau avancé.
```

### Commandes pendant une session

```text
Mets l'entraînement en pause.
Reprends la simulation.
Où en est la session ?
Arrête et donne-moi mon analyse d'élocution.
```

## Comportements à vérifier

1. ANO pose une seule question à la fois.
2. Il attend réellement la réponse avant la suivante.
3. Après chaque réponse, il donne un retour court puis une relance ou objection.
4. Une hésitation volontaire comme « euh, en fait » apparaît dans le rapport.
5. Une session vocale affiche un débit ; une session texte indique « non mesuré ».
6. La dernière manche produit automatiquement le rapport final.

## Fichiers concernés

- `actions/sparring_partner.py`
- `main.py`
- `tests/test_sparring_partner.py`
- `JARVIS_FEATURES_ROADMAP.html`

