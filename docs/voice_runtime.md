# État vocal ANO-GPT

Configuration par défaut et migration des anciens `api_keys.json` :

- Mode vocal : half-duplex strict. Le micro n'est jamais transmis pendant la sortie audio de l'assistant ; le barge-in local accepte seulement les ordres courts tels que « ANO stop ».
- Modèle conversationnel : celui configuré dans `live_model` (configuration actuelle : `models/gemini-2.5-flash-native-audio-preview-12-2025`), avec transcription d'entrée et de sortie Gemini Live et VAD/fin de tour serveur.
- Sous-titres : `gemini_live`. Gemini 3.5 Transcribe ne démarre que si `live_captions_provider` vaut explicitement `gemini_transcribe` (diagnostic), jamais en conversation normale.
- Vérification sensible : `sensitive_command_verification: true` utilise `sensitive_command_transcribe_model` uniquement avant les outils destructeurs ou irréversibles.
- AEC / full-duplex : désactivé par défaut. Il exige simultanément `voice_barge_in_enabled`, `full_duplex_aec_enabled` et `full_duplex_aec_validated` à `true`. L'absence de l'une de ces preuves, d'une référence de sortie ou une anomalie AEC impose le repli half-duplex.

Avant toute activation full-duplex sur haut-parleurs, valider la chaîne PipeWire (micro et sortie réellement jouée) avec les scénarios synthétiques : assistant seul, utilisateur seul, double parole et écho résiduel. Une validation logicielle de Speex seule ne suffit pas.
