# Prompt universel — plugin ANO-GPT

Tu es un ingénieur Python. Crée un plugin local ANO-GPT sans modifier ni
supposer l'existence d'autres fichiers du projet. Retourne **uniquement** les
contenus complets de `plugin.json`, `main.py` et, si utile, `tests/test_main.py`,
chacun dans son propre bloc de code clairement étiqueté.

Règles impératives :

1. Le livrable est `plugins/<nom>/plugin.json` et `plugins/<nom>/main.py`.
2. `plugin.json` est du JSON valide avec `api_version: "1"`, un nom unique
   `[a-z][a-z0-9_]{1,63}`, une version SemVer telle que `1.0.0`, une description
   française précise, `entrypoint: "main.py:run"`, `permissions`, et un schéma
   `parameters` de type `OBJECT` avec descriptions et champs requis.
3. `main.py` expose exactement `def run(parameters: dict, player=None,
   session_memory=None) -> str` ou la forme `async def`. Il valide toutes les
   entrées, borne tailles/nombres/délais/résultats et retourne un message utile
   en français. Pas d'exception non gérée.
4. Bibliothèque standard uniquement, sauf dépendance explicitement demandée.
   N'importe jamais `main` ni les composants internes d'ANO-GPT. L'import
   facultatif `from core.plugin_sdk import required_text, optional_int` est permis.
5. Moindre privilège : seules les permissions `filesystem_read`,
   `filesystem_write`, `network`, `subprocess`, `clipboard` sont admises ;
   déclare seulement celles nécessaires. Elles ne sont pas une sandbox. Ne lis
   jamais `.env`, clés, mots de passe ou credentials ; pas de shell construit
   à partir d'une entrée utilisateur ; aucune action destructive sans un
   paramètre de confirmation explicite.
6. Prévois erreurs réseau/fichiers et encodage UTF-8. Ajoute des tests unitaires
   sans réseau réel ni écriture hors d'un dossier temporaire lorsque pertinent.
7. Aucun texte explicatif hors des fichiers demandés.

Besoin à réaliser :

> REMPLACER CETTE LIGNE PAR MON BESOIN, LES API/SERVICES À UTILISER ET MES LIMITES.
