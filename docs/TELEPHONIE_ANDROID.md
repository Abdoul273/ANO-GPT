# ANO-GPT — Téléphonie native via ANO-Remote

## Principe

ANO-GPT ne consulte aucun contact sur le PC et ne passe aucun appel depuis le PC.

```text
« ANO, appelle le garage »
          │
          ▼
ANO-GPT transmet seulement « garage » sur le canal appairé
          │
          ▼
ANO-Remote cherche « garage » dans Contacts Android
          │
          ├─ absent      → aucun appel, erreur renvoyée au PC
          ├─ ambigu      → aucun appel, choix numérotés renvoyés au PC
          └─ unique      → ACTION_CALL avec la carte SIM du téléphone
```

## Sécurité

- Le téléphone doit être appairé avec le serveur ANO-GPT.
- Seul un client déclaré `ano_remote_android` reçoit les ordres d'appel.
- Les commandes sont transitoires et ne sont jamais rejouées après reconnexion.
- Chaque demande possède un identifiant aléatoire et attend son retour associé.
- Android exige `READ_CONTACTS` et `CALL_PHONE`.
- L'utilisateur doit en plus activer **Appels automatiques par ANO** dans l'application.
- L'outil `phone_call` est classé sensible côté PC et passe par Voice ID lorsqu'une empreinte est configurée.
- Un numéro direct est nettoyé et validé (`+` optionnel, 6 à 15 chiffres).
- Le numéro complet n'est jamais renvoyé au PC : seuls les quatre derniers chiffres sont affichés.

## Configuration dans ANO-Remote

Après avoir produit et installé la prochaine version de l'application :

1. Ouvrir l'onglet **Téléphone**.
2. Appuyer sur **Autoriser Contacts et Appels**.
3. Accepter les deux boîtes de dialogue Android.
4. Activer **Appels automatiques par ANO**.
5. Vérifier que l'état affiche **PRÊT À APPELER**.

## Phrases de test

```text
ANO, appelle Maman.
ANO, appelle le garage.
ANO, appelle le +224 600 00 00 00.
```

En cas de plusieurs numéros :

```text
ANO, appelle le garage.
→ « Choix 1… choix 2… Lequel ? »
Appelle le choix 2.
```

## Retours possibles

| État | Signification |
|---|---|
| `started` | Android a lancé l'appel |
| `not_found` | Aucun contact Android correspondant |
| `ambiguous` | Plusieurs contacts ou numéros, aucun appel lancé |
| `permission_required` | Permissions Android absentes |
| `disabled` | Opt-in des appels automatiques désactivé |
| `phone_offline` | ANO-Remote Android non connecté |
| `multiple_phones` | Plusieurs téléphones sont connectés ; aucun n'est appelé pour éviter un double appel |
| `no_telephony` | Appareil sans téléphonie cellulaire |
| `timeout` | Téléphone connecté mais sans réponse |
| `failed` | Android n'a pas pu lancer l'intent d'appel |

## Portée de cette version

Cette version implémente de manière complète le **déclenchement sécurisé des appels sortants depuis le téléphone** et la résolution locale des contacts Android.

Le déclenchement automatique lorsque l'application est en arrière-plan dépend
des restrictions du constructeur et de la version Android. Il doit être validé
sur le téléphone réel après compilation ; garder ANO-Remote visible constitue
le mode de test de référence. Une éventuelle restriction Android doit produire
`failed` et ne provoque jamais un appel depuis le PC.

Elle ne prétend pas encore réaliser les deux éléments suivants :

- répondre et converser automatiquement lors des appels entrants ;
- écouter/transcrire une conversation cellulaire et créer seule un rendez-vous Calendar.

Ces fonctions exigent une architecture Android de composeur par défaut (`ROLE_DIALER`, `InCallService`) et une stratégie audio compatible avec les restrictions d'enregistrement des appels. Elles restent donc marquées « En cours » dans la roadmap plutôt que simulées.

## Fichiers concernés

- `android/app/src/main/AndroidManifest.xml`
- `android/app/src/main/kotlin/com/anogpt/ano_remote/MainActivity.kt`
- `lib/phone_link.dart`
- `lib/session.dart`
- `lib/home_page.dart`
- `dashboard/server.py`
- `main.py`
- `tests/test_phone_calls.py`

## Compilation

Conformément à la demande initiale, **aucune commande Flutter, Gradle ou compilation APK n'a été exécutée pendant l'implémentation**.
