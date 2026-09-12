# Voix ElevenLabs

Dans Audio → Voix de l’assistant, sélectionner Gemini ou ElevenLabs.
Le changement est mémorisé et reconnecte la session vocale. Gemini continue
à comprendre les demandes et à produire les réponses ; ElevenLabs lit leur
transcription finale. Cela ajoute une attente avant la parole par rapport
à la voix Gemini native.

Le sélecteur charge les voix du compte en arrière-plan : cliquez sur la liste
déroulante pour choisir une voix, sans saisir de nom ni d’identifiant. Le choix de voix et de modèle est sauvegardé et appliqué après reconnexion.
Multilingual v2 privilégie un rendu naturel et régulier ; Turbo v2.5 équilibre
qualité et rapidité ; Flash v2.5 privilégie la réactivité.
George reste la voix par défaut, pas une limitation du catalogue.

Si la clé refuse le catalogue, activez sa permission `voices_read` dans ElevenLabs
puis cliquez sur « Actualiser les voix ».
La consultation du catalogue ne déclenche pas de synthèse audio.
Les réglages `voice_provider`, `elevenlabs_voice_id`, `elevenlabs_model_id` et
`elevenlabs_api_key` vivent dans `config/api_keys.json`, ignoré par Git.
Ne jamais copier la clé dans le code ou les rapports.

Le son PCM 24 kHz passe par la sortie habituelle : haut-parleurs sélectionnés,
arrêt, visualisation et blocage du micro pendant la parole. Les requêtes
facturables ne sont pas répétées automatiquement. En cas d’erreur de clé,
de quota ou de réseau, un message invite à sélectionner Gemini dans Audio.

Les crédits sont renouvelés mensuellement selon le cycle du compte.
La consultation du solde nécessite la permission API `user_read` ; celle du
catalogue nécessite `voices_read`. La synthèse nécessite `text_to_speech`.
