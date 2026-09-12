# ANO-GPT — repères techniques

Les règles de travail (périmètre, tests, quota) sont globales et vivent dans
`~/.claude/CLAUDE.md`. Ce fichier ne garde que ce qui est propre à ce projet.

- **Machine modeste** : 2 cœurs, 11 Go, Wayland/Hyprland. Qt tourne sur le
  thread principal et l'audio dans un thread asyncio — **ils partagent le
  GIL**. Toute animation ou tout traitement lourd côté interface se paie
  directement sur la voix.
- **Une seule carte** : `core/map_render.py`, plein cadre. Ne jamais en
  introduire une seconde.
- **L'assistant ne doit jamais s'entendre lui-même** : pendant qu'il parle,
  rien ne part au modèle (half-duplex, dans le callback micro de `main.py`).
  Aucun réglage de seuil ne remplace ça — sa voix est une vraie voix humaine.
- **Tout appel externe d'une action passe par `core/action_kit.py`** : délai
  obligatoire (le groupe de processus entier est tué), lancements détachés,
  lectures Hyprland mises en cache et fusionnées. Le cache Hyprland est
  volontairement plus court (0,12 s) que l'intervalle des boucles d'attente des
  actions — l'allonger ferait lire un état figé à ces boucles. `core/action_runtime.py`
  borne l'outil vu du répartiteur ; le kit borne l'intérieur.
- Le socket de contrôle (`core/ipc.py`) est le pont vers les agents MCP
  (`anogpt_mcp.py`, `core/tool_bridge.py`).
- Antigravity (`agy`) lit `~/.gemini/config/mcp_config.json`, **pas** celui de
  `~/.gemini/antigravity-cli/`.
