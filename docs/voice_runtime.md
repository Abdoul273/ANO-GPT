# État vocal ANO-GPT

Configuration par défaut et migration des anciens `api_keys.json` :

- Mode vocal : half-duplex strict. Le micro n'est jamais transmis pendant la sortie audio de l'assistant ; le barge-in local accepte seulement les ordres courts tels que « ANO stop ».
- Modèle conversationnel : `models/gemini-3.8-live`, avec transcription d'entrée et de sortie Gemini Live. En cas de quota propre au modèle ou d'indisponibilité, ANO-GPT essaie `gemini-3.1-flash-live-preview`, puis `gemini-2.5-flash-native-audio-latest`. Si tous sont refusés, il attend avant de réessayer : les quotas du projet ou de facturation ne peuvent pas être contournés par un repli. Le VAD serveur utilise ses valeurs par défaut ; `live_end_silence_ms` reste facultatif.
- Sous-titres : `gemini_live`. Gemini 3.5 Transcribe ne démarre que si `live_captions_provider` vaut explicitement `gemini_transcribe` (diagnostic), jamais en conversation normale.
- Vérification sensible : `sensitive_command_verification: true` utilise `sensitive_command_transcribe_model` uniquement avant les outils destructeurs ou irréversibles.
- AEC / full-duplex : désactivé par défaut. Il exige simultanément `voice_barge_in_enabled`, `full_duplex_aec_enabled` et `full_duplex_aec_validated` à `true`. L'absence de l'une de ces preuves, d'une référence de sortie ou une anomalie AEC impose le repli half-duplex.

Avant toute activation full-duplex sur haut-parleurs, valider la chaîne PipeWire (micro et sortie réellement jouée) avec les scénarios synthétiques : assistant seul, utilisateur seul, double parole et écho résiduel. Une validation logicielle de Speex seule ne suffit pas.
