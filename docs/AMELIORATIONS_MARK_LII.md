# Améliorations inspirées de l'analyse de Mark-LII

ANO-GPT conserve son routeur PipeWire, sa mémoire SQLite, son HUD et AnoRemote.
Les mécanismes suivants ont été ajoutés sans remplacer ces composants :

- confirmation humaine avec jeton aléatoire, disponible dans le HUD et dans
  AnoRemote ; un argument généré par le modèle ne peut pas l'accepter ;
- pile d'annulation bornée pour les fichiers, le volume et la luminosité ;
- compression glissante des longues sessions Gemini avec repli automatique si
  le modèle ou l'endpoint ne la prend pas en charge ;
- panneau visuel de la mémoire SQLite avec suppression ciblée ;
- sélection persistante du microphone et des haut-parleurs PipeWire/Pulse ;
- plugins locaux validés, désactivables et isolés contre les exceptions.

Les commandes catastrophiques restent refusées sans possibilité de
confirmation. Les commandes sensibles et les messages sortants passent par le
jeton produit par l'interface. Le micro du téléphone et le micro du PC gardent
leurs états indépendants.
