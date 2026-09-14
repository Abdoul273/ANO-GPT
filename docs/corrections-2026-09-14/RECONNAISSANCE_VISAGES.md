# Reconnaissance des visages — précision et robustesse

## Changements

- Comparaison du meilleur candidat au meilleur **autre individu**, avec une marge minimale de similarité de 0,08. Plusieurs photos d'une même personne ne créent pas une fausse ambiguïté. Une correspondance trop proche d'un autre profil demande confirmation.
- Contrôle de taille, netteté et score de détection avant toute identification affirmative ou mise en attente pour apprentissage. Les captures inutilisables demandent une meilleure image, sans proposer un nom.
- Suppression de l'enrichissement automatique des profils à partir d'une simple reconnaissance. Ajouter un angle ou une apparence passe désormais par un apprentissage explicite du même nom : une erreur ponctuelle ne devient pas une nouvelle référence.
- Refus d'un apprentissage automatique du plus grand visage lorsque plusieurs personnes sont dans le cadre. Refus d'un lot d'empreintes incohérentes. Deux visages de la même image reçoivent des dossiers distincts ; inscrire l'un ne supprime pas le dossier de l'autre.
- Validation des 128 valeurs finies et normalisation des empreintes avant calcul/écriture. Les références corrompues sont ignorées avec une trace lors du chargement du cache, sans effacer les données originales.
- Suppression des nouvelles empreintes quasi identiques (cosinus ≥ 0,9999). La limite existante de 40 références par personne reste conservée.
- Attentes plafonnées à 32 dossiers, avec 8 empreintes par dossier et expiration après 15 minutes. Comparaison à toutes les références du dossier pour éviter une fusion par dérive successive ; contrôle de marge entre dossiers. Qualité conservée par empreinte.
- Une inscription échouée conserve le dossier en attente ; expiration vérifiée avant inscription.
- Veille : deux observations consécutives avant annonce ; aucune annonce affirmative des résultats ambigus. L'absence ou une erreur casse la séquence de confirmation. Les apparitions sont enregistrées à l'annonce, sous le délai anti-répétition existant, au lieu d'écrire à chaque image.
- Un worker de veille encore en arrêt garde sa référence : un second worker ne peut pas démarrer pendant qu'il est encore vivant.
- Chargement YuNet/SFace protégé contre les appels concurrents ; publication des deux modèles seulement lorsque leur chargement a réussi.
- Les cartes et les réponses parlent de similarité, pas d'un pourcentage de certitude. Une absence de correspondance ne signifie plus « personne jamais vue ».

## Adaptation à la machine

YuNet/SFace conservés ; aucun modèle supplémentaire téléchargé, aucun GPU requis. Dimension maximale de détection maintenue à 640 pixels, alignement sur l'image originale, OpenCV limité à un thread. Comparaison des empreintes dans le cache matriciel RAM. Pas de modification de la carte, du micro ou du filtrage half-duplex.

La confirmation temporelle concerne la veille automatique : avec son intervalle par défaut de 1,5 s, elle ajoute une observation avant l'annonce. Une identification demandée directement garde son traitement immédiat avec refus des correspondances ambiguës.

## Vérification

Copie isolée : `/tmp/anogpt-faces-Qy8g5H`. Les tests utilisent des empreintes synthétiques et des bases temporaires, sans lire/modifier les personnes mémorisées ni ouvrir la caméra.

- Suite Python complète : **1 802 réussites, 23 ignorés, 1 échec attendu**, quatre avertissements, 92,82 s ; sortie normale.
- Cette suite précède les derniers ajustements sur la conservation des dossiers voisins, la qualité par empreinte et le test de chargement concurrent, vérifiés ensuite de manière ciblée.
- Ruff : aucune erreur ; `git diff --check` : aucun défaut d'espacement.
- Modèles réels installés, OpenCV 5.0.0 : chargement natif réussi, aucun visage sur une image noire synthétique, extraction SFace de 128 valeurs et normalisation réussies. OpenCV confirme un thread. Deux avertissements internes concernant les cibles du nouveau moteur apparaissent, sans échec de ces contrôles.

## Mesures et utilisation

Les seuils historiques 0,42 / 0,34 sont conservés ; la marge 0,08 est une protection conservatrice ajoutée, **pas une calibration mesurée sur les personnes de l'utilisateur**. Le tutoriel [OpenCV YuNet/SFace](https://docs.opencv.org/4.13.0/d0/dd4/tutorial_dnn_face.html) publie des seuils différents selon les jeux de données : son résultat LFW n'est pas un taux de réussite garanti dans une pièce réelle.

Pour enrichir une personne : présenter une personne à la fois, avec un visage net et bien éclairé, puis demander explicitement de mémoriser le même nom sous un autre angle. Les données déjà présentes sont conservées ; d'anciens profils éventuellement mal étiquetés ne sont pas corrigés automatiquement.

Il reste à mesurer les fausses acceptations et les refus sur des prises réelles représentatives (personnes connues et inconnues, lumière et angles variés). Aucun gain chiffré de précision, de FPS ou de CPU n'est revendiqué. Ces modifications n'ajoutent pas une détection de vivacité et ne transforment pas cette reconnaissance en authentification. Elles seront chargées au prochain démarrage d'ANO-GPT.
