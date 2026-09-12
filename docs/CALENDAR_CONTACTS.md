# Agenda et contacts

ANO-GPT expose `calendar_control`/`calendar` en lecture-écriture et
`contacts_control`/`contacts` pour le carnet local. Le briefing quotidien lit
automatiquement les événements du jour.

## Google Calendar

Le client OAuth de bureau déjà placé dans
`memory/credentials/gmail_client_secret.json` est réutilisé, mais l'autorisation
agenda est conservée séparément dans `memory/calendar_token.json`. Une connexion
Gmail existante n'est donc pas modifiée.

1. Activez **Google Calendar API** dans le projet Google Cloud.
2. Demandez à ANO-GPT : « connecte mon agenda Google ».
3. Acceptez l'autorisation dans le navigateur.

## CalDAV

Ajoutez ces clés à `config/api_keys.json` :

```json
{
  "caldav_calendar_url": "https://serveur.example/dav/calendars/utilisateur/principal/",
  "caldav_username": "utilisateur",
  "caldav_password": "mot-de-passe-application"
}
```

Utilisez l'URL exacte de la collection calendrier. Un mot de passe d'application
est recommandé lorsque le fournisseur le permet.

## Carnet de contacts

Les fiches sont stockées avec des permissions privées dans
`memory/contacts.json`. Exemple vocal : « ajoute Alice, alice@example.com,
@alice sur Telegram ». Les noms sont ensuite résolus dans `send_message`, les
filtres expéditeur/destinataire Gmail et les invités d'un rendez-vous.

