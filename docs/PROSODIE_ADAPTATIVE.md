# Prosodie adaptative

ANO-GPT analyse localement le PCM du microphone à la fin de chaque énoncé. Les
mesures portent sur le rythme syllabique, l'énergie, la proportion de voix, la
hauteur et sa variation. Aucun service d'analyse émotionnelle séparé n'est
appelé : seul le libellé du profil rejoint la session Gemini Live déjà active.
Aucune émotion médicale ou psychologique n'est inférée.

Les états utilisés sont `urgent`, `focused`, `enthusiastic`, `tired` et
`neutral`. La directive correspondante emprunte le canal temps réel Gemini Live
entre le dernier bloc audio et `activity_end`; elle s'applique donc à la réponse
courante. Elle est explicitement marquée comme non prononçable.

La préférence esthétique est indépendante de l'état détecté :

- `professional` : précis, assuré et chaleureux ;
- `stark` : assurance et ironie légère ;
- `synthetic` : résultat d'abord, une phrase si possible.

Elle peut être changée à la voix (« adopte le style Tony Stark », « sois
ultra-synthétique ») grâce à l'outil `voice_style`, ou dans
`config/api_keys.json` avec `"prosody_style": "professional"`. L'urgence et la
fatigue restent prioritaires : le sarcasme est automatiquement neutralisé dans
les situations sensibles.
