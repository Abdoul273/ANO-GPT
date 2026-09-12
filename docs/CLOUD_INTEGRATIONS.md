# Connexions cloud

## Notion et Figma : API personnelle gratuite

ANO-GPT utilise l'API officielle uniquement pour Notion et Figma, car elle est
plus fiable que le contrôle d'une page web pour créer/rechercher des notes ou
lire des fichiers. Aucun abonnement API ni crédit API n'est acheté : le jeton
personnel donne seulement à ANO-GPT les droits de votre compte actuel.

1. Dites « connecte Notion » ou « connecte Figma » : la page officielle de
   création de jeton s'ouvre dans Google Chrome.
2. Créez un jeton personnel avec le minimum de permissions utile.
3. Dans un terminal, lancez l'une de ces commandes puis collez le jeton lorsque
   le terminal le demande (il ne sera pas affiché) :

   ```bash
   python scripts/setup_cloud_token.py notion
   python scripts/setup_cloud_token.py figma
   ```

Le jeton est conservé dans le trousseau système, pas dans un fichier du projet.
Pour Notion, créez une page `Notes ANO-GPT`, partagez-la avec votre connexion
Notion, puis collez son URL lorsque le script la demande. Ce choix est fait une
seule fois : après cela, « crée une note sur Python » suffit.

Si le jeton Notion est déjà enregistré, ne le recréez pas : définissez seulement
la page par défaut avec `python scripts/setup_cloud_token.py notion --parent-only`.

Votre plan Notion Student ou Figma Education ne change pas : il est associé à
votre compte/espace de travail. L'API n'active pas de fonction payante et ne
donne pas de crédits Figma Make ou Notion AI supplémentaires.

## Services utilisés dans Chrome

Google Calendar, NotebookLM, Gemini, Stitch et Figma Make restent dans le
profil Google Chrome normal. Connectez-vous une fois avec les comptes qui
possèdent vos abonnements ; Chrome garde ensuite la session.
