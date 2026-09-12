# Handoff Claude — reconnaissance vocale ANO-GPT

## But du système

ANO-GPT doit comprendre le français parlé avec une latence faible, afficher la phrase immédiatement, puis n'exécuter une commande que si la transcription est suffisamment fiable. La priorité est la fidélité : une phrase incertaine doit être redemandée, jamais transformée en action arbitraire.

## Environnement audio réel

- Système : Arch/EndeavourOS, Hyprland, PipeWire + pipewire-pulse.
- Micro matériel : `HDA Intel PCH / ALC3246 Analog Stereo` (micro interne).
- La capture applicative est en PCM signé 16 bits, mono, 16 kHz.
- Le périphérique Pulse/PortAudio utilisé comme source par ANO-GPT doit rester le micro matériel : `alsa_input.pci-0000_00_1f.3.analog-stereo`.

### Invariant important : ne pas définir EasyEffects Source comme source Pulse par défaut

EasyEffects crée `easyeffects_source`, mais un client PortAudio qui ouvre directement cette source peut recevoir du silence dans cette configuration PipeWire. Le bon montage est : source Pulse par défaut = micro matériel ; EasyEffects redirige alors la capture de l'application vers sa sortie virtuelle traitée. `core/audio_router.py` exclut donc explicitement `easyeffects_source` des sources candidates.

Le choix persistant est dans `config/audio_devices.json`.

## Traitement du son avant STT

Préréglage EasyEffects actif :

`~/.local/share/easyeffects/input/Voix nette - micro interne.json`

Ordre des effets :

1. Filtre passe-haut EasyEffects : 100 Hz, pente 12 dB/octave, IIR, avant tout autre effet. Il élimine le rumble stable proche de 90 Hz observé sur le micro interne.
2. RNNoise : réduction de bruit constante.
3. Compresseur : rapproche les voix faibles sans empiler de gain logiciel dans ANO-GPT.
4. Limiteur : évite l'écrêtage.

ANO-GPT ne doit pas rajouter un autre denoiseur, AGC ou normaliseur sur le PCM déjà traité : plusieurs traitements successifs dégradent les consonnes et faussent la reconnaissance.

## Architecture de transcription

```text
Micro ALC3246
  -> PipeWire / EasyEffects (RNNoise -> compresseur -> limiteur)
  -> PortAudio sounddevice, PCM 16 kHz mono
  -> AudioEngine : VAD, pré-roll, horodatage des paquets
  -> Gemini 3.5 Transcribe Live
       -> aperçu partiel immédiat dans l'interface
       -> résultat final
  -> vérification Azure Speech du même PCM finalisé
  -> consensus ou rejet
  -> commande ANO-GPT seulement après acceptation
```

Les trois moteurs n'ont pas le même rôle :

| Moteur | Rôle | Quand il travaille |
|---|---|---|
| Vosk small fr 0.22 | Mot de réveil local (« Ano »), interruption (« Ano stop »), secours léger | En continu, modèle léger uniquement |
| Gemini 3.5 Transcribe Live | Transcription principale, aperçu et résultat final | Pendant une phrase vocale |
| Azure Speech-to-Text | Vérification indépendante du résultat Gemini | À la fin d'une vraie phrase seulement |

## Vosk

Le modèle complet est installé dans `~/.local/share/vosk-models/vosk-model-fr-0.22`. Il est valide mais trop lourd pour une écoute permanente sur cette machine : environ 3,3 Go de mémoire et une charge CPU notable observés. Il reste disponible pour de la dictée locale volontaire, via `ANOGPT_VOSK_MODEL`.

L'écoute continue utilise par défaut le petit modèle `~/.cache/anogpt/models/vosk-model-small-fr-0.22`, beaucoup plus adapté au mot de réveil. La résolution du modèle est dans `core/wake_word.py`.

Sous Python 3.14, Vosk est exécuté dans le sous-processus isolé `core.interrupt_worker`, car son chargement natif dans le processus principal était instable. Les threads OpenMP/BLAS sont limités à 1 pour ne pas monopoliser le processeur.

## Gemini : source principale

La configuration actuelle choisit `models/gemini-3.5-transcribe-live`. Le module central est `core/gemini_transcribe_stt.py`.

- Langue : `fr-FR`.
- Mode demandé : transcription littérale (`VERBATIM`).
- Vocabulaire métier français, noms et commandes ANO-GPT fournis au modèle.
- Le VAD local détermine début et fin de parole ; l'activité audio serveur est désactivée.
- Le PCM est envoyé intact après EasyEffects.
- Les résultats partiels ne servent qu'à l'affichage ; le résultat confirmé est normalement celui qui peut déclencher une commande.

## Azure : vérificateur de précision

`core/azure_speech_stt.py` réalise la reconnaissance courte Azure par REST, sans SDK Python. Il convertit le PCM final en WAV mono 16 kHz et interroge la reconnaissance conversationnelle en `fr-FR`, format détaillé.

Le mode par défaut conserve l'endpoint de reconnaissance courte. Le mode opt-in `azure_fast_phrases` (ou `ANOGPT_AZURE_FAST_PHRASES=on`) utilise l'API Azure Fast Transcription 2025-10-15 et transmet une liste de phrases ANO-GPT (commandes, termes métier, noms) pour les booster. Microsoft limite cette liste à 500 phrases ; ce mode doit être comparé sur de vraies phrases avant de devenir le défaut, car il modifie l'endpoint et peut modifier la latence.

Le Microsoft Audio Stack (suppression de bruit, AGC, AEC) est une fonction du Speech SDK côté client, pas une option d'amélioration distante de l'endpoint REST court. Il n'est pas activé : EasyEffects reste l'unique traitement audio en amont, afin d'éviter un second débruiteur/AGC.

La configuration est lue localement, sans recopier de secret dans le dépôt :

`~/Documents/Projet_Compresser/DEO_DUB/data/settings.json`

ou, au besoin, via `ANOGPT_AZURE_SPEECH_SETTINGS`. Les champs attendus sont `azure_profil`, puis `azure_<profil>_key` et `azure_<profil>_region`. Ne jamais placer la valeur d'une clé dans le code, dans un log ou dans un document de handoff.

Azure est activé seulement dans l'application réelle par `main.py` (`_azure_speech_enabled`). Les tests ne doivent pas appeler le service réel.

`ANOGPT_AZURE_VERIFY=off` désactive complètement Azure : aucune phrase n'est transmise à Azure, Gemini reste le moteur principal. La même préférence peut être enregistrée dans `voice_settings.azure_verify`. C'est le mode à choisir pour réduire un aller-retour réseau, les coûts Azure et l'exposition supplémentaire des phrases vocales.

Coût/latence : Azure facture le Speech-to-Text à la durée audio ; l'offre F0 affiche actuellement 5 heures audio gratuites partagées par mois pour la transcription standard/custom, puis il faut vérifier le tarif S0 et la région dans le portail Azure. Fast Transcription renvoie une réponse synchrone à latence prévisible, mais reste un appel après la fin de phrase : il ne remplace pas l'aperçu Gemini live. La liste de phrases elle-même n'est pas un second traitement DSP ; elle ajoute seulement le vocabulaire envoyé avec la requête Fast.

### Règle de consensus actuelle

Pour une phrase terminée :

1. Gemini produit un aperçu live et un résultat final.
2. Azure retranscrit le même segment audio.
3. Si Azure valide le final Gemini, ce final est retenu.
4. Si Azure valide l'aperçu Gemini mais pas le final, l'aperçu est retenu : cela protège contre un final Gemini qui dérive après avoir affiché une bonne phrase.
5. Si les deux moteurs divergent réellement, la phrase est rejetée, aucun outil/commande n'est exécuté et ANO-GPT demande de répéter.

Cette stratégie est volontairement conservatrice : mieux vaut une répétition qu'une réponse à une phrase inventée.

## Temps réel et interface

La petite zone « Votre voix » reçoit les aperçus de Gemini. La grande carte de scène était auparavant affichée seulement après validation, ce qui créait l'impression qu'elle arrivait après la réponse d'ANO-GPT.

`ui/window/scene.py`, méthode `_on_user_transcript`, affiche maintenant le même aperçu dès qu'il arrive, avec l'état :

- `● VOUS · ÉCOUTE EN DIRECT` : texte partiel.
- `✓ VOUS · TRANSCRIPTION CONFIRMÉE` : texte retenu après final/consensus.

Le texte de la grande carte doit donc être visible pendant la parole, puis devenir confirmé avant que l'assistant traite la commande. Toute modification future doit préserver cet ordre.

## Protections contre les phrases anciennes ou mauvaises

`core/audio_engine.py` horodate chaque bloc audio placé dans la file et crée un identifiant aléatoire pour chaque phrase. `core/gemini_transcribe_stt.py` propage cet identifiant vers l'aperçu Gemini, le final, Azure et Qt. La scène ignore donc un résultat tardif appartenant à une ancienne phrase. Les blocs vieux de plus de 2 secondes sont également ignorés. Cette protection corrige le cas où une file en retard envoyait une ancienne phrase et faisait répondre ANO-GPT à autre chose que ce que l'utilisateur venait de dire.

Une ligne `STT_METRICS` est journalisée par tour avec : id de tour, timestamps, profondeur de file, âge maximal de paquet, preuve VAD, RMS/crête, aperçu/final Gemini, texte Azure et décision. Elle ne contient ni PCM brut, ni URL Azure, ni clé/secrets.

Si une phrase est trop vieille ou si le consensus échoue, elle est annulée avant exécution. Le message UI doit demander de répéter au lieu de simuler une compréhension.

## Fichiers essentiels

- `main.py` : activation des vérifications Azure dans l'application.
- `core/audio_engine.py` : capture, VAD, file audio et horodatage.
- `core/audio_router.py` : sélection sûre de la source PipeWire/Pulse.
- `core/wake_word.py` : choix Vosk petit/grand modèle.
- `core/interrupt_worker.py` : isolation Vosk sous Python 3.14.
- `core/gemini_transcribe_stt.py` : session Gemini, aperçu, final, protection contre backlog, consensus Azure.
- `core/azure_speech_stt.py` : client Azure REST.
- `ui/window/scene.py` : carte de transcription live/confirmée.
- `tests/test_gemini_transcribe_stt.py` et `tests/test_azure_speech_stt.py` : règles de fiabilité.
- `scripts/check_audio_chain.py` : capture ou analyse WAV, niveaux et énergie par bandes. Il retourne 2 lorsque plus de 50 % de l'énergie est sous 250 Hz.

## Vérifications déjà effectuées

- Le modèle Vosk français complet est présent et lisible.
- Le petit modèle Vosk est chargé par défaut pour l'écoute continue.
- La source audio traitée produit un signal sans écrêtage lors des mesures.
- Azure est joignable avec la configuration locale ; un test silencieux ne produit volontairement aucun texte.
- Les suites de tests STT/audio concernées passent, notamment les tests Gemini, Azure et capture fidèle.

Commandes de régression utiles depuis la racine ANO-GPT :

```bash
python -m pytest -q tests/test_audio_visual_regressions.py tests/test_gemini_transcribe_stt.py
python -m pytest -q tests/test_azure_speech_stt.py tests/test_gemini_transcribe_stt.py tests/test_stt_capture_fidelity.py
python scripts/check_audio_chain.py --seconds 5
```

## Limites et travail restant pour Claude

1. **Charge CPU élevée** : le processus principal ANO-GPT a encore été observé à plus de 100 % d'un cœur sur une machine à deux cœurs. Cela peut provoquer une latence ou une file audio. Il faut profiler cette charge avant d'ajouter d'autres modèles, en mesurant l'âge des paquets, la taille des files, le VAD et les threads, sans enregistrer de clés ni de contenu audio privé.
2. **Test humain final requis** : les changements de consensus et d'affichage ont des tests automatisés, mais doivent être validés avec de vraies phrases variées : salutations, heure, commandes courtes, phrases longues, bruit ambiant et voix faible.
3. **Identification de tour** : il serait utile d'ajouter un identifiant unique par phrase et de l'associer à l'aperçu, au final, à Azure et à la carte UI. Ainsi, un résultat tardif ne pourra jamais écraser la carte d'une phrase plus récente.
4. **Mesures structurées** : journaliser par tour les timestamps, l'âge maximal des paquets, RMS/crête, texte Gemini aperçu/final, texte Azure et décision — mais jamais le PCM brut ni les secrets. Cela permet de prouver précisément où une phrase dérive.
5. **Confidentialité, latence et coût** : chaque phrase finalisée est actuellement envoyée à Gemini puis à Azure. C'est intentionnel pour la vérification, mais l'utilisateur doit pouvoir désactiver Azure ou choisir « confidentialité locale » ; Azure ajoute aussi un aller-retour réseau.

## Critère de réussite

Pour chaque phrase : le texte live apparaît en premier, le texte confirmé conserve ce qu'a réellement dit l'utilisateur, puis seulement ANO-GPT répond. En cas de doute, l'application demande de répéter. Elle ne doit jamais exécuter une commande issue d'un texte tardif, silencieux ou en désaccord entre moteurs.
