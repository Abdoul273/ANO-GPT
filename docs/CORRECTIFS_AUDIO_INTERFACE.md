# Audio et interface — septembre 2026

## Corrections audio

La lecture utilisait des tranches de 20 ms avec seulement 20 ms de réserve
matérielle. Une courte pause de la boucle Python pouvait donc épuiser le tampon,
même avec une bonne connexion.

- Réserve matérielle demandée portée à 60 ms.
- Préchargement initial borné à 80 ms, interrompu dès que suffisamment de blocs
  arrivent ou que la réponse est terminée. Il reste annulable et ne rejoue pas
  le bloc en attente après une interruption.
- Mises à jour visuelles audio limitées à 25/s ; l'orbe QPainter limite également
  sa cadence à 25 images/s pendant la parole.
- Compteur de sous-alimentations audio dans les messages de diagnostic.
- Réverbération utilisant des filtres SciPy natifs lorsqu'ils sont disponibles,
  avec conservation des états entre blocs et repli sur le calcul existant.

Mesure locale sur 100 blocs de 480 échantillons à 24 kHz : **4,31 ms/bloc** pour
le calcul original, **0,71 ms/bloc** pour le nouveau chemin natif. Il s'agit du
calcul de réverbération, pas de la latence totale ni d'une mesure de qualité perçue.
Le signal a été comparé au calcul original sur des blocs de tailles variables.

Compromis assumé : quelques dizaines de millisecondes supplémentaires au démarrage
de la parole pour mieux absorber la gigue. Le budget nominal tranche + réserve
matérielle devient 80 ms ; ce n'est pas une garantie temps réel du système audio.

## Défilement et présentation

- Correction de `set_body` dans l'ancienne carte, qui utilisait deux variables
  locales inexistantes.
- Corps Markdown défilant dans les cartes génériques, borné à 200 px ; les cartes
  courtes gardent une hauteur adaptée au contenu.
- Colonne de notifications dans une zone défilante, limitée à l'espace au-dessus
  du briefing. Elle ne recouvre plus sa surface de lecture.
- Briefing agrandi, texte de 10 points, marges plus généreuses et barre de
  défilement visible. Molette, clavier et sélection du texte conservés.
- Le journal ne retourne plus automatiquement en bas si l'utilisateur relit
  des messages plus anciens ; historique visuel borné à 1 500 blocs.
- Palette commune plus sobre : texte clair, surfaces bleu nuit, accents cyan et
  violet conservés. Bordures moins lumineuses et animations de panneaux ralenties.

## Vérification

**162 tests passent**, couvrant audio, interruption, écho, spatialisation,
défilement réel avec événements de molette Qt, superposition des panneaux,
cartes et interfaces. Un avertissement de la suite reste présent.

L'aperçu natif a été généré et inspecté sans démarrer les services, sans lire de
documents personnels et sans appeler un fournisseur IA :

```bash
python scripts/preview_interface.py --output /tmp/ano-gpt-interface.png
```

Il utilise des données de démonstration et l'orbe QPainter ; le moteur OpenGL
de la fenêtre réelle peut différer. La validation auditive sur les périphériques
réels reste à faire lors du prochain lancement. Ces changements concernent
l'application native sur PC, pas une refonte du client Android.
