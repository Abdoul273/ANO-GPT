# Créer un plugin ANO-GPT

Le format recommandé est un dossier `plugins/mon_plugin/`, avec `plugin.json`
et `main.py`. Il ne demande aucune connaissance interne d'ANO-GPT : toute IA
capable de générer du Python peut le produire.

```text
plugins/
  mon_plugin/
    plugin.json
    main.py
    tests/             # recommandé
```

## `plugin.json`

```json
{
  "api_version": "1",
  "name": "mon_plugin",
  "version": "1.0.0",
  "description": "Explique précisément à ANO-GPT quand appeler cet outil.",
  "entrypoint": "main.py:run",
  "permissions": [],
  "parameters": {
    "type": "OBJECT",
    "properties": {"texte": {"type": "STRING", "description": "Texte à traiter"}},
    "required": ["texte"]
  }
}
```

Le nom suit `[a-z][a-z0-9_]{1,63}` et la version le format SemVer (`1.0.0`).
Permissions possibles : `filesystem_read`, `filesystem_write`, `network`,
`subprocess`, `clipboard`. Elles documentent et rendent auditable l'accès
demandé, mais ne sont pas une sandbox : n'installe que du code fiable.

## `main.py`

```python
from core.plugin_sdk import required_text

def run(parameters: dict, player=None, session_memory=None) -> str:
    texte = required_text(parameters, "texte")
    session_memory["dernier_texte"] = texte
    return f"Reçu : {texte}"
```

`run` reçoit un dictionnaire et retourne une réponse courte. Il peut aussi être
`async def`. `player` et `session_memory` sont facultatifs ; ce dernier conserve
un état seulement durant la session vocale. Valide et borne toujours les entrées.

Copie le dossier dans `plugins/`, puis utilise **Menu → Plugins** ou dis
« recharge les plugins ». La prochaine reconnexion vocale publie le nouvel outil
à Gemini. L'ancien format d'un fichier `.py` avec `PLUGIN` et `run` reste pris
en charge pour ne rien casser.

Le prompt prêt à l'emploi pour une autre IA est dans `PLUGIN_CREATOR_PROMPT.md`.
