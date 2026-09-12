# ANO-GPT — repères techniques

Les règles de travail (périmètre, tests, quota) sont globales et vivent dans
`~/.Codex/AGENTS.md`. Ce fichier ne garde que ce qui est propre à ce projet.

- **Machine modeste** : 2 cœurs, 11 Go, Wayland/Hyprland. Qt tourne sur le
  thread principal et l'audio dans un thread asyncio — **ils partagent le
  GIL**. Toute animation ou tout traitement lourd côté interface se paie
  directement sur la voix.
- **Une seule carte** : `core/map_render.py`, plein cadre. Ne jamais en
  introduire une seconde.
- **L'assistant ne doit jamais s'entendre lui-même** : pendant qu'il parle,
  rien ne part au modèle (half-duplex, dans le callback micro de `main.py`).
  Aucun réglage de seuil ne remplace ça — sa voix est une vraie voix humaine.
- Le socket de contrôle (`core/ipc.py`) est le pont vers les agents MCP
  (`anogpt_mcp.py`, `core/tool_bridge.py`).
- Antigravity (`agy`) lit `~/.gemini/config/mcp_config.json`, **pas** celui de
  `~/.gemini/antigravity-cli/`.

- **Navigateur unique : Google Chrome** pour toutes les ouvertures web et OAuth.
  Utiliser core/browser_policy.py, jamais Firefox ni un autre navigateur en repli.
- **Système hôte : Arch Linux (EndeavourOS)** : Toutes les commandes système demandées ou exécutées doivent impérativement être adaptées pour Arch Linux (`sudo pacman -S`, `yay -S`). Ne jamais utiliser `apt`, `apt-get`, `dpkg`, `dnf` ni des chaînes de repli multi-distribution (`|| sudo apt ...`).
