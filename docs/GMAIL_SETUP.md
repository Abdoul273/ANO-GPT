# Connexion Gmail à ANO-GPT

ANO-GPT utilise OAuth 2.0 en lecture seule. Le mot de passe Google n'est jamais
demandé ni stocké par l'application.

## Installation, une fois pour toutes

Google exige une clé OAuth propre à votre compte : personne d'autre ne peut la
créer à votre place. Les quatre premières étapes se passent sur le site de
Google, la cinquième dans ANO-GPT.

1. Ouvrez [console.cloud.google.com](https://console.cloud.google.com), créez
   (ou choisissez) un projet.
2. **API et services → Bibliothèque** → activez l'**API Gmail**.
3. **Écran de consentement OAuth** : type *Externe*, puis ajoutez votre adresse
   Gmail dans **Utilisateurs de test**. Sans cela Google refusera l'accès.
4. **Identifiants → Créer des identifiants → ID client OAuth** → type
   **Application de bureau** → **Télécharger le JSON**.
5. Lancez :

   ```
   python scripts/setup_gmail.py
   ```

   Le script retrouve seul le fichier dans vos téléchargements, l'installe dans
   `memory/credentials/gmail_client_secret.json`, puis ouvre l'autorisation
   Google directement dans Chrome. Si le fichier est ailleurs, donnez son chemin en
   argument.

Vous pouvez aussi tout faire à la voix après l'étape 4 : dites
**« connecte Gmail »**. ANO-GPT récupère la clé téléchargée et ouvre
l'autorisation dans Chrome. Si l'autorisation expire, répétez cette commande :
la clé existante est réutilisée, sans recréer de projet Google.

Le jeton obtenu est enregistré dans `memory/gmail_token.json` avec des droits
fichier limités à l'utilisateur (`0600`). Les deux fichiers sont exclus de Git.

## Lecture en temps réel

Dès que Gmail est relié, ANO-GPT surveille la boîte en continu et **annonce à
voix haute chaque nouveau message** dès son arrivée, avec une fiche à l'écran et
une notification sur le téléphone appairé.

La veille interroge l'historique Gmail, qui ne renvoie que les nouveautés : elle
tourne toutes les 25 secondes sans peser sur le quota. Deux conséquences
volontaires :

- au démarrage, les messages non lus déjà présents ne sont **pas** annoncés —
  seuls ceux qui arrivent ensuite le sont. Pour les anciens, demandez
  « lis mes e-mails non lus » ;
- après plusieurs jours d'arrêt, la veille repart d'un point neuf plutôt que de
  rejouer la boîte entière.

Gmail ne propose de vraie notification poussée qu'avec Google Pub/Sub et une
adresse publique : hors de portée d'une application de bureau. Vingt-cinq
secondes est ce qui s'en approche le plus.

## Commandes disponibles

- « Vérifie la connexion Gmail »
- « Comment je termine la configuration Gmail ? » (donne les étapes)
- « Lis mes e-mails non lus »
- « Montre mes dix derniers e-mails »
- « Cherche les e-mails de Mamadou des trente derniers jours »
- « Retrouve les factures PDF non lues de Alice entre le 01/08/2026 et le 15/08/2026 »
- « Cherche le contrat budget dans les messages envoyés »
- « Lis le deuxième »
- « Résume mes messages non lus »

## Recherche avancée

Vous pouvez parler naturellement en français. ANO-GPT reconnaît notamment :

- l'expéditeur et le destinataire ;
- les mots de l'objet et le texte libre ;
- aujourd'hui, hier, cette semaine, ce mois, une période relative ou deux dates ;
- non lu, déjà lu, suivi, important ;
- présence, absence, nom ou format d'une pièce jointe ;
- boîte de réception, messages envoyés, brouillons, spam et corbeille ;
- les libellés et les limites de taille.

La requête interprétée est affichée avec les résultats afin de pouvoir vérifier
exactement ce qui a été cherché. Le moteur parcourt automatiquement plusieurs
pages, élimine les doublons, puis reclasse les candidats selon les correspondances
dans l'objet, l'expéditeur et l'extrait. Les recherches sont bornées pour rester
rapides même sur une très grande boîte et peuvent retourner jusqu'à 100 messages.

La syntaxe Gmail native reste disponible et n'est jamais reformulée. Par exemple :

- `from:alice@example.com`
- `newer_than:30d`
- `has:attachment facture`
- `is:unread in:inbox`
- `{from:alice from:bob} after:2026/08/01`

La recherche exclut le spam et la corbeille par défaut. Ils ne sont inclus que si
la demande les vise explicitement ou active l'option correspondante.

## Dépannage

- **« La configuration n'est pas terminée »** : demandez « comment je termine la
  configuration Gmail ». ANO-GPT dit précisément l'étape qui manque.
- **Clé absente** : vérifiez que le JSON téléchargé est bien de type
  *Application de bureau*. Une clé *Application Web* ne peut pas ouvrir
  l'autorisation locale et est ignorée.
- **Accès bloqué** : ajoutez le compte aux utilisateurs de test de l'écran de
  consentement OAuth (étape 3).
- **Autorisation expirée/révoquée** : dites de nouveau « connecte Gmail ».
- **Rien n'est annoncé** : la veille ne démarre qu'avec une autorisation valide.
  « Vérifie la connexion Gmail » indique si elle tourne.
- **Boîte vide** : le module distingue une vraie boîte vide d'une erreur
  d'authentification et affiche le diagnostic correspondant.
