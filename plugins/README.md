# Plugins ANO-GPT

Le format recommandé est désormais un dossier avec `plugin.json` et `main.py` :
consultez `PLUGIN_AUTHORING_GUIDE.md`. Le modèle historique à fichier unique
(`_template.py`) reste compatible. Les plugins valides apparaissent dans
**Menu → Plugins** et deviennent des outils Gemini à la prochaine connexion vocale.

Un plugin est exécuté avec les mêmes droits que l'assistant : n'installez que
du code que vous avez lu ou qui vient d'une source fiable. Les collisions avec
les outils internes sont refusées et une exception de plugin est transformée
en message d'erreur au lieu de fermer la session.
