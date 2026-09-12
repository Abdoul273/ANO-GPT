# Azure OpenAI dans ANO-GPT — guide pas à pas

ANO-GPT emploie Azure OpenAI pour les tâches profondes (raisonnement, code,
analyse et plans) tout en gardant Gemini Live pour la conversation vocale. Cela
préserve la réactivité, le barge-in et la protection half-duplex de la voix.

> Important : ne transmettez jamais votre clé à ANO‑GPT par chat, Git, capture
> ou message. Vous la saisissez vous-même dans l'interface locale, où elle est
> enregistrée dans `config/api_keys.json`, ignoré par Git.

## Avant de commencer

- Connectez-vous avec le compte de votre abonnement Azure Student :
  [portal.azure.com](https://portal.azure.com/).
- Vérifiez que l'abonnement **Azure for Students** apparaît dans
  **Subscriptions**. Son crédit est limité ; surveillez son solde dans le
  portail. [FAQ Azure for Students](https://learn.microsoft.com/en-us/azure/education-hub/faq)
- Ouvrez Microsoft Foundry dans un nouvel onglet :
  [ai.azure.com](https://ai.azure.com/).

Si Azure refuse la création de la ressource ou du déploiement, ce n'est pas une
erreur d'ANO‑GPT : le modèle ou la région peut ne pas être autorisé(e) pour
l'abonnement étudiant. Continuez avec une autre région proposée par Foundry, ou
dites-moi le texte exact de l'erreur **sans clé ni endpoint**.

## Étape 1 — Créer la ressource Azure AI Foundry

1. Ouvrez le raccourci officiel :
   [Créer une ressource Foundry](https://portal.azure.com/#create/Microsoft.CognitiveServicesAIFoundry).
2. Dans **Subscription**, sélectionnez votre abonnement Student.
3. À **Resource group**, cliquez **Create new**, puis donnez par exemple le nom
   `anogpt-rg`.
4. À **Region**, choisissez la région qui propose le modèle désiré. Laissez le
   portail vous signaler si le modèle n'est pas disponible ; on pourra changer
   de région à l'étape suivante.
5. À **Name**, entrez un nom simple, par exemple `anogpt-ai-<vos-initiales>`.
6. Cliquez **Review + create**, puis **Create**. Attendez la confirmation
   « Deployment succeeded ».

La procédure officielle détaillée est disponible ici :
[créer une ressource Foundry](https://learn.microsoft.com/en-us/azure/ai-services/multi-service-resource).

## Étape 2 — Créer un projet et déployer le modèle

1. Ouvrez [Microsoft Foundry](https://ai.azure.com/) et sélectionnez la
   ressource créée. Créez un projet, par exemple `anogpt`.
2. Cliquez **Discover** en haut à droite, puis **Models** à gauche.
3. Pour la qualité maximale, choisis `gpt-6-astra` dans ton catalogue. Si tu
   veux préserver plus longtemps le crédit Student, `gpt-5.6-terra` est le
   meilleur compromis ; `gpt-5.6-luna` privilégie surtout la vitesse. Ces noms
   correspondent au modèle Azure sélectionné, pas nécessairement au nom final
   du déploiement.
4. Ouvrez sa fiche, cliquez **Deploy** → **Custom settings**.
5. Dans **Deployment name**, entrez `anogpt-brain`. Retenez ce nom exactement :
   c'est celui qu'ANO‑GPT demande, même si le modèle s'appelle autrement.
6. Laissez le type de déploiement et la capacité proposés par Azure pour le
   premier essai, puis cliquez **Deploy**.
7. À la fin, utilisez le Playground affiché pour demander « Réponds seulement
   par OK ». Une réponse confirme que le modèle est déployé.

La documentation Microsoft confirme que le **nom de déploiement** route les
requêtes et décrit cette séquence :
[déployer un modèle dans Foundry](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/how-to/deploy-foundry-models).

## Étape 3 — Récupérer les trois valeurs nécessaires

1. Dans [Azure Portal](https://portal.azure.com/), ouvrez votre ressource
   `anogpt-ai-…`.
2. Dans le menu gauche, ouvrez **Keys and Endpoint**.
3. Copiez dans un bloc-notes temporaire, jamais dans Git :

   | Valeur | Format attendu dans ANO‑GPT |
   |---|---|
   | Endpoint | `https://<nom-ressource>.openai.azure.com` |
   | Key 1 ou Key 2 | une longue clé secrète Azure |
   | Deployment name | `anogpt-brain` (le nom choisi à l'étape 2) |

Utilisez l'un de ces deux formats selon ce que Foundry affiche :

| Type de ressource | Endpoint à coller |
|---|---|
| Azure OpenAI classique | `https://<nom-ressource>.openai.azure.com` |
| Microsoft Foundry v1 | `https://<nom-ressource>.services.ai.azure.com/openai/v1` |
| Foundry Model Inference | `https://<nom-ressource>.services.ai.azure.com/models` |

Ne copiez jamais l'URL complète qui contient `chat/completions`,
`/deployments` ou `api-version` : ANO‑GPT les construit automatiquement.

## Étape 4 — Connecter ANO‑GPT

1. Lancez ANO‑GPT.
2. Ouvrez **⚙ Réglages** → **Configurer l'IA**.
3. Cliquez **Azure OpenAI**.
4. Collez la clé dans **Clé API**.
5. Collez uniquement l'endpoint dans **URL / Endpoint**.
6. Dans **Modèle**, entrez le nom de déploiement : `anogpt-brain`.
7. Cliquez **Tester**. Le résultat attendu est : « Endpoint, déploiement et clé
   Azure valides. »
8. Cliquez **Enregistrer**. Cela définit Azure comme cerveau des demandes
   profondes tout en gardant Gemini comme fournisseur vocal et conversationnel
   par défaut. N'utilisez **Appliquer ce provider** que si vous voulez remplacer
   volontairement Gemini dans les appels texte classiques.

### Réglages Azure Speech : F0 ou S0

Dans le même panneau Azure OpenAI, saisissez séparément la clé et la région de
votre ressource **Azure Speech**. Cette clé n'est pas la clé Azure OpenAI.

- Choisissez **F0** pour démarrer : gratuit et suffisant pour le second avis de
  transcription d'ANO-GPT, qui reste occasionnel.
- Choisissez **S0** seulement si vous avez créé une ressource Speech au SKU S0
  et que vous avez besoin de davantage de capacité ou de Fast Transcription.

Le choix dans ANO-GPT ne transforme pas une ressource F0 en S0 : le SKU réel
se choisit lors de la création de la ressource Azure. Gardez « Activer Azure
Speech comme second avis STT » désactivé tant que la clé et la région n'ont pas
été enregistrées. Gemini Transcribe reste toujours le STT principal.

### Profils recommandés

Les cinq champs de déploiement spécialisés attendent les **noms de vos
déploiements**, jamais le nom commercial seul du modèle.

| Usage | Modèle à déployer | Nom de déploiement conseillé |
|---|---|---|
| Raisonnement profond | `gpt-6-astra` | `anogpt-brain` |
| Code | `gpt-5.3-codex` | `anogpt-code` |
| Documents | `gpt-6-astra` | `anogpt-docs` |
| Images | `gpt-image-2` | `anogpt-image` |
| Vidéo | aucun dans la liste actuelle | laissez vide |

`gpt-realtime-2.1` produit de l'audio, pas de la vidéo. ANO-GPT conserve
Gemini Live pour la voix, donc ne le déployez pas pour cette intégration.
Les profils raisonnement profond et Speech sont opérationnels dans cette
version ; les champs code/documents/images préparent les déploiements dédiés
pour leur connecteur de génération respectif.

Azure OpenAI utilise une route liée au déploiement et l'en-tête `api-key` ;
l'implémentation ANO‑GPT respecte la référence officielle :
[API REST Azure OpenAI](https://learn.microsoft.com/fr-fr/azure/foundry/openai/reference).

## Étape 5 — Vérifier le comportement et les coûts

1. Demandez à ANO‑GPT une question qui exige une analyse, par exemple :
   « Compare ces deux solutions et donne-moi le meilleur plan. »
2. Dans les logs, cherchez « Réflexion fournie par Azure OpenAI ».
3. Dans le portail Azure, ouvrez **Cost Management + Billing** → **Cost
   analysis** après quelques minutes pour voir la consommation.
4. Si vous voulez privilégier le crédit étudiant, gardez `gpt-5.6-terra` et des
   réponses concises. Pour le maximum de qualité, gardez `gpt-6-astra`, puis
   remplacez uniquement le **nom de déploiement** dans ANO‑GPT si vous en créez
   un second.

En mode « cerveau Auto », Azure OpenAI devient prioritaire uniquement après la
configuration de votre clé ; les fournisseurs déjà configurés restent des replis.
