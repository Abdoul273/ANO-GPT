# GitHub dans ANO-GPT

ANO-GPT utilise le **Device Flow OAuth** de GitHub : il convient à une
application desktop, ouvre uniquement Google Chrome et ne demande pas de
secret OAuth pour la connexion. Le jeton obtenu est enregistré dans le
trousseau système par `keyring`, jamais dans `api_keys.json`, un remote Git ou
les logs.

## Préparation unique

1. Dans GitHub, créez une OAuth App destinée à ANO-GPT.
2. Activez **Device Flow** dans les réglages de cette application.
3. Copiez son *Client ID* public dans `config/api_keys.json` :

   ```json
   { "github_oauth_client_id": "votre-client-id" }
   ```

4. Dites : « ANO, connecte GitHub ».
5. Google Chrome ouvre GitHub ; entrez le code affiché dans le journal ANO-GPT
   si GitHub le demande, puis acceptez l'autorisation.

Les droits demandés sont `repo` (création et push de dépôts) et `read:user`
(identification du compte).

## Commandes

- « liste mes dépôts GitHub »
- « crée un dépôt privé mon-projet »
- « initialise mon projet mon-projet avec Git »
- « commite et pousse mon-projet sur GitHub »
- « active la sauvegarde GitHub automatique pour mon-projet »
- « déconnecte GitHub »

La sauvegarde périodique ne concerne que les projets explicitement activés et
s'exécute toutes les quinze minutes. Elle ne publie jamais un projet découvert
automatiquement.

## Sécurité

Avant chaque commit et chaque push, ANO-GPT emploie le scanner DevSecOps des
diffs ajoutés et bloque les clés API, jetons, mots de passe ou clés privées
potentiels. Le token OAuth est fourni à Git par un script `GIT_ASKPASS`
temporaire, supprimé après le push ; il n'est jamais inséré dans l'URL remote.

« Déconnecte GitHub » supprime le jeton du trousseau local et coupe
immédiatement l'accès d'ANO-GPT. La révocation distante par API nécessite le
secret de l'OAuth App, qui n'est volontairement pas conservé par ANO-GPT ; elle
reste disponible depuis les réglages des applications GitHub si nécessaire.
