# Reconnaissance vocale d’ANO-GPT

## Gemini Transcribe uniquement — septembre 2026

La reconnaissance vocale utilise exclusivement `models/gemini-3.5-transcribe-live`.
ElevenLabs Scribe est désactivé pour la reconnaissance : aucune clé ni aucun
crédit ElevenLabs n'est sollicité par un énoncé micro. Toute ancienne préférence
`stt_provider=elevenlabs` est ignorée au démarrage.

Chaque phrase détectée par le VAD local est diffusée à Gemini Transcribe
pendant la parole, en PCM mono 16 kHz conservé sans filtre spectral ni AGC, avec
français (`fr-FR`), lexique métier et mode `VERBATIM` (les mots prononcés,
jamais une reformulation SMART). Les révisions provisoires s'affichent en
direct ; les segments finalisés sont conservés dans leur ordre. Seule une
fin explicite du serveur (`turn_complete` ou `voice_activity.ACTIVITY_END`)
après la fin du micro autorise leur soumission. Un délai de quatre secondes
sans cette confirmation fait refuser le tour, même si un début de phrase est
déjà disponible. Il n'y a plus de seconde connexion Live : c'était le même moteur
avec 8–15 s de trop. Le modèle conversationnel ne reçoit pas l'audio microphone.

Cette architecture privilégie la fidélité plutôt que l'immédiateté : si Gemini
ne confirme pas une phrase, si l'audio est incomplet, trop long ou jugé bruité,
la demande est refusée et doit être répétée. Une transcription parfaite ne peut
pas être garantie, mais aucun repli silencieux vers un autre moteur n'est fait.

### Correction après le signalement « imprimer l'heure »

Les essais synthétiques précédents ne reproduisaient pas la capture réelle.
Le callback micro empilait un traitement spectral annoncé DeepFilterNet,
le débruitage spectral et l'AGC d'AudioPreprocessor, puis le flux STT appliquait
encore RNNoise et un autre AGC. Le premier module ne faisait pas d'inférence
DeepFilterNet : sa fonction de traitement utilisait le DSP maison même avec
l'indication `libdf_native`. Son détecteur de clics atténuait fortement les
trames dominées par les hautes fréquences, y compris des signaux non voisés.

Le chemin de capture principal conserve désormais le PCM original, pré-roll
compris. Le VAD décide du début et de la fin d'une phrase sans modifier les
échantillons envoyés. Gemini ne leur applique plus de second prétraitement
local. Les moteurs de débruitage restent disponibles pour leurs usages
séparés ; ils ne sont plus empilés sur la capture principale.

Une conversion PCM16 exacte évite aussi les modifications dues aux conversions
float/int successives. Les débordements PortAudio alimentent le compteur de
pertes pour refuser un énoncé incomplet. Une ligne `MIC : signal ...` indique
la durée, le niveau RMS, la crête et la proportion d'échantillons écrêtés pour
chaque tour. Ces mesures ne sont pas des scores de confiance linguistique.
L'application n'enregistre pas automatiquement le micro sur disque.

Pour comparer une vraie phrase avec son texte attendu, sans exécuter de commande :

```bash
python scripts/diagnose_stt.py --record 8 --output /tmp/ma-voix.wav \
  --transcribe --expected "Il fait quelle heure ?"
```

L'assistant doit être silencieux pendant cet essai. Le WAV est créé avec des
permissions privées, sans écraser de fichier existant. `--transcribe` envoie
uniquement ce clip à Gemini ; le texte attendu n'est jamais fourni au modèle.
Sans ce drapeau, le diagnostic reste local. Un WAV existant mono PCM16/16 kHz
peut être réutilisé avec `--wav /tmp/ma-voix.wav`. Le niveau matériel ne doit
pas être augmenté arbitrairement sur cette carte ALC3246, dont le pilote
annonce un volume de base inhabituel : mesurer la parole et l'écrêtage d'abord.

Les tests vérifient maintenant le callback réel, notamment la conservation
des faibles échantillons non voisés et du pré-roll, ainsi que l'absence
d'émission pendant la réponse. La précision sur la voix réelle doit encore
être validée avec un enregistrement contenant effectivement la phrase.

### Renforcement de la fidélité — 6 septembre 2026

- Suppression de la réécriture phonétique après transcription : « discorde »
  reste « discorde », « dockeur » reste « dockeur ». Le vocabulaire technique
  est fourni au moteur acoustique, sans remplacer arbitrairement ses mots.
- Lecture persistante : le SDK termine `receive()` après un `turn_complete` ;
  le lecteur reprend pour recevoir la phrase suivante sur la même connexion.
- Une phrase annulée invalide sa connexion STT, renouvelée au prochain tour.
  Les résultats tardifs de l'ancienne phrase sont ignorés. La boucle audio et
  la synthèse vocale continuent indépendamment.
- Une erreur réseau ou une fin de flux inattendue ne valide plus le dernier
  fragment reçu. Le sous-titre provisoire est effacé en cas de rejet.
- Le filtre de secours sans RNNoise retire le DC sans écraser les syllabes
  sous un seuil fixe de volume : un micro faible n'est pas du silence.
- Le VAD du pipeline de précision conserve les premières trames vocales
  pendant sa confirmation et exige une séquence soutenue (96 ms par défaut),
  au lieu de valider un clip entier sur un seul pic. Une panne VAD bloque
  la vérification ; un résultat mis en cache sans VAD ne contourne pas ce contrôle.

Essai réel de l'API après correction, sans capture du micro ni exécution de
commandes : deux phrases espeak-ng françaises, PCM mono 16 kHz, envoyées en
temps réel sur une seule connexion. Résultats :

| Audio synthétique attendu | Résultat | Délai après fin d'envoi |
| --- | --- | --- |
| Bonjour, quelle heure est-il ? | Identique | 1,606 s |
| Ne ferme pas Google Chrome. (amplitude × 0,2) | Identique, négation conservée | 0,380 s |

La connexion initiale a pris 3,88 s. Un essai préalable avait dépassé dix
secondes pendant la connexion : la latence dépend aussi du réseau. La trace
réelle a confirmé la séquence texte final → `generation_complete` →
`ACTIVITY_END`, sans `turn_complete`, désormais couverte par un test dédié.
Ces deux phrases valident le protocole et un cas de faible amplitude ; elles
ne mesurent ni le taux d'erreur sur la voix réelle ni la résistance au bruit
de la pièce. Les changements prennent effet après redémarrage d'ANO-GPT.

## Historique : vérification double moteur

Le chemin Scribe utilise désormais `no_verbatim=false` : les hésitations,
répétitions et corrections prononcées sont conservées. Au démarrage avec Scribe,
la vérification Gemini Transcribe est activée pour chaque phrase par défaut
(`precision_stt_enabled=true`, `precision_stt_mode=all`), y compris les mots
courts. Un réglage explicite `off` ou `auto` reste respecté.

Les deux moteurs doivent retrouver la même suite de mots, en tolérant seulement
casse, accents et ponctuation. « salut » / « allo », un nom différent, une
négation ou un nombre remplacé entraînent une demande de répétition affichée,
sans exécution. Une panne de vérification ne valide pas le premier résultat.
La répétition peut commencer immédiatement après le message, sans délai de
reconnexion imposé. Un résultat provisoire Gemini ne sert jamais de résultat
final et un audio trop long est refusé plutôt que tronqué en début de phrase.

Ce choix privilégie la fidélité : il ajoute un appel réseau et son coût à chaque
tour, peut demander de répéter une phrase correcte et ne garantit pas que deux
moteurs ne commettent jamais la même erreur. Les délais de finalisation sont
bornés à 15 secondes par moteur (hors connexion et fermeture).

Le barge-in reste local. Le réseau demeure bloqué tant que l’assistant parle,
même si une interruption a déjà été demandée et que la lecture finit de s’arrêter.
Les changements de code prennent effet après redémarrage d’ANO-GPT.

### Choix des moteurs et limites des essais

- [Gemini Transcribe Live](https://ai.google.dev/gemini-api/docs/live-api/live-transcribe)
  fournit un mode `VERBATIM` et une indication de langue française. Il est
  distinct de la transcription intégrée au modèle conversationnel Live.
- [ElevenLabs](https://help.elevenlabs.io/hc/en-us/articles/33053029255697-What-is-Speech-to-Text)
  recommande Scribe v2 pour la précision et v2 Realtime pour le temps réel.
  L’application conserve son intégration Realtime, désormais littérale,
  vérifiée par Gemini ; Scribe v2 batch n’a pas été mesuré ici.
- [Azure Custom Speech](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/custom-speech-overview)
  permet d’évaluer et d’adapter le modèle. Aucun identifiant Azure Speech
  n’est présent dans la configuration examinée ; aucun essai Azure effectué.

Essai réseau du 6 septembre 2026, voix synthétique française espeak-ng,
PCM mono 16 kHz, sans capture du micro : « oui » a été transcrit « Oui. » par
les deux moteurs ; « salut » et « ne ferme pas Chrome » ont rencontré des
erreurs Scribe et des délais dépassés Gemini. Les durées observées étaient
d’environ 9,8 s pour Scribe et 8,5 s pour Gemini sur le seul essai réussi.
Ces résultats ont motivé le passage des anciens délais de 10/9 s à 15 s ;
ils ne constituent pas une mesure de précision sur la voix de l’utilisateur.
Pour choisir le meilleur fournisseur, il faut comparer les mêmes enregistrements
réels avec leur texte attendu (taux d’erreur par mot et phrases exactes).

## Fonctionnement historique

Dans **Audio → Reconnaissance de votre voix**, choisir **ElevenLabs Scribe**.
Le choix est conservé et appliqué après reconnexion. Gemini reste disponible
comme choix manuel de secours ; une panne de Scribe ne déclenche pas de
bascule silencieuse vers une autre reconnaissance.

Scribe Realtime reçoit le PCM 16 kHz des énoncés détectés localement, avec le
français imposé et le filtrage des conversations de fond activé.
Le WebSocket est fermé après chaque énoncé. Les transcriptions partielles
servent uniquement à l’affichage ; le texte confirmé passe par le parcours
habituel des commandes, confirmations, routines et du contexte écran.
Gemini Live comprend et exécute la demande ; ElevenLabs prononce sa réponse.
Les protections supplémentaires des actions sensibles restent présentes.

## Interruptions

Un niveau sonore, même élevé, ne constitue plus un ordre d’interruption.
Pendant la réponse, ni le micro du PC ni celui du téléphone ne transmettent
leur son aux services distants. Les fragments en attente et transcriptions
tardives sont invalidés au début d’une réponse ou à l’arrêt utilisateur.

**« ANO stop »** et **« ANO attends »** sont reconnus localement sur la source
avec annulation d’écho, si celle-ci est disponible. Sur Python 3.14, Vosk
tourne dans un sous-processus isolé, avec un vocabulaire limité, une commande
complète adressée à ANO et une confiance minimale. Aucun repli sur le volume
n’est autorisé si ce détecteur échoue. **Échap / Arrêter** restent disponibles.

## Voix du propriétaire

Le filtrage des conversations de fond ne garantit pas l’identité de la personne.
Quand une empreinte est enregistrée, Scribe fait vérifier l’énoncé avant son
exécution. Pour enregistrer la vôtre, prononcez deux ou trois phrases puis
demandez **« apprends ma voix »**. Sans empreinte, le filtrage de fond reste
actif mais une autre voix clairement audible peut encore être transcrite.

## Vérifications effectuées

- Connexion réelle à Scribe avec les options françaises et le filtrage confirmé.
- « Bonjour, quelle heure est-il ? » généré par espeak-ng : transcription correcte.
- Souffle et impulsions synthétiques de type clavier : transcription vide.
- « ANO stop » synthétique : interruption locale détectée.
- Phrase ordinaire synthétique : aucune interruption locale.
- Tests automatiques : blocage pendant la réponse, mute, ancien énoncé,
  rejet d’un locuteur inconnu enregistré, téléphone et transmission de texte.

Ces essais ne remplacent pas une validation avec la voix, le micro et la pièce
de l’utilisateur. Aucun audio privé n’a été utilisé pour les essais réseau.

Référence : [API Scribe Realtime](https://elevenlabs.io/docs/api-reference/speech-to-text/v-1-speech-to-text-realtime).
