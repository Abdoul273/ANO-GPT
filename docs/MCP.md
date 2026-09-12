# Connecter un agent à ANO-GPT avec MCP

Le serveur MCP permet à Antigravity, Claude Code, Codex CLI ou Claude Desktop
d'utiliser les outils réels d'ANO-GPT. L'agent réfléchit ; l'application qui
tourne exécute l'action sur sa carte, sa caméra, sa voix et la machine.

## Mode Agent Fantôme

ANO-GPT peut déléguer une mission longue à Antigravity sans fermer le tour de
conversation. Une demande telle que « analyse ce dépôt, écris les tests
manquants et préviens-moi » appelle `background_tasks` avec
`action="delegate"`. La mission est persistée, exécutée dans un processus de
priorité basse et reprise après un redémarrage si elle avait été interrompue.

Chaque mission possède un identifiant `task-…`. `background_tasks(action="list")`
affiche les missions actives et `background_tasks(action="cancel",
task_id="task-…")` annule tout le groupe de processus. Le rapport Markdown est
archivé dans `~/.config/jarvis/ghost_reports/`. À la fin, le bus proactif attend
que l'utilisateur et l'assistant aient cessé de parler, affiche une carte puis
annonce vocalement le résumé.

ANO-GPT prend un instantané léger du projet avant et après la mission. Le
rapport et l'annonce distinguent donc les fichiers réellement créés, modifiés
ou supprimés pendant l'exécution, indépendamment de ce qu'`agy` affirme dans sa
sortie. Les dossiers générés (`.git`, `node_modules`, `build`, environnements
virtuels, caches) sont exclus. Par défaut, une mission vocale sans chemin vise
le dépôt ANO-GPT, jamais l'ensemble du dossier personnel.

Le paramètre `show_terminal=true` ouvre une fenêtre Kitty qui suit la sortie
d'`agy`. Cette fenêtre est seulement une vue : la fermer n'annule pas la mission
et `agy` continue en arrière-plan. Sans ce paramètre, aucun terminal n'est
ouvert et la voix reste entièrement disponible.

Le sous-agent hérite du serveur MCP configuré pour `agy`, mais l'outil `speak`
est neutralisé pendant sa mission : seule l'annonce finale, ordonnée par
ANO-GPT, peut interrompre discrètement l'utilisateur. Les missions sont
exécutées une par une sur cette machine à deux cœurs afin de préserver l'audio.

Les réglages facultatifs suivants peuvent être ajoutés à `config/api_keys.json` :

```json
{
  "ghost_agent_model": "",
  "ghost_agent_effort": ""
}
```

Quand ils sont vides ou absents, le Mode Fantôme reprend `agent_model` et
`agent_effort`, puis les valeurs par défaut de la CLI.

Le serveur utilisé sur cette machine est :

```text
/usr/bin/python /home/anonymous/OUTILS/ANO-GPT/anogpt_mcp.py
```

Lancez ANO-GPT avant l'agent pour les actions visuelles. La météo, la recherche
web et de fichiers, les captures, Gmail, la mémoire, la localisation, les
rappels et l'état système disposent d'un repli quand l'application est éteinte.

## Antigravity (`agy`)

> **Attention au fichier.** Antigravity possède deux `mcp_config.json` au
> contenu identique en apparence. Le seul que la CLI lit réellement est
> **`~/.gemini/config/mcp_config.json`**. Celui de
> `~/.gemini/antigravity-cli/` ne fait rien — il porte pourtant le nom du
> dossier de la CLI, ce qui en fait le mauvais choix évident. Vérifié le
> 30 août 2026 sur Antigravity CLI 1.1.19 : une entrée ajoutée uniquement
> dans le second n'a jamais été démarrée, alors que la même entrée dans le
> premier a été chargée immédiatement.

Éditez **`~/.gemini/config/mcp_config.json`** en conservant les serveurs
déjà présents :

```json
{
  "mcpServers": {
    "jarvis-brain": {
      "command": "/home/anonymous/.jarvis-orb/bin/jarvis-brain",
      "disabled": true
    },
    "ano-gpt": {
      "command": "/usr/bin/python",
      "args": ["/home/anonymous/OUTILS/ANO-GPT/anogpt_mcp.py"]
    }
  }
}
```

Le chemin absolu `/usr/bin/python` n'est pas une coquetterie : `agy` lance les
serveurs sans passer par un shell, donc sans garantie de retrouver `python`
dans son `PATH`.

Redémarrez ensuite `agy` **complètement** — quittez le processus et relancez-le.
N'utilisez pas `/mcp` pour recharger : sur cette version, le rechargement à
chaud échoue (`failed to stop mcp instance: … signal: terminated`) et laisse la
liste vide, ce qui affiche « No MCP servers configured » alors que la
configuration est correcte.

### Vérifier qu'`agy` a bien démarré le serveur

Antigravity met en cache le schéma de chaque outil découvert. Après un
redémarrage, ce dossier doit exister et contenir un fichier par outil :

```bash
ls ~/.gemini/antigravity-cli/mcp/ano-gpt/
```

S'il est absent, `agy` n'a pas tenté de lancer le serveur : c'est un problème de
configuration, pas de serveur.

## Claude Code

Pour une configuration locale au projet courant :

```bash
claude mcp add --scope local ano-gpt -- /usr/bin/python /home/anonymous/OUTILS/ANO-GPT/anogpt_mcp.py
```

L'équivalent, déjà présent dans le dépôt sous `.mcp.json`, est :

```json
{
  "mcpServers": {
    "ano-gpt": {
      "type": "stdio",
      "command": "/usr/bin/python",
      "args": ["/home/anonymous/OUTILS/ANO-GPT/anogpt_mcp.py"]
    }
  }
}
```

Utilisez `--scope user` à la place de `--scope local` si le serveur doit être
accessible dans tous les projets Claude Code.

## Codex CLI

Dans `~/.codex/config.toml`, ajoutez :

```toml
[mcp_servers.ano_gpt]
args = ["/home/anonymous/OUTILS/ANO-GPT/anogpt_mcp.py"]
command = "/usr/bin/python"
startup_timeout_sec = 120
```

La commande équivalente est :

```bash
codex mcp add ano_gpt -- /usr/bin/python /home/anonymous/OUTILS/ANO-GPT/anogpt_mcp.py
```

Relancez Codex après avoir modifié sa configuration.

## Claude Desktop

Sur cette machine Linux, le fichier est
`~/.config/Claude/claude_desktop_config.json`. Ajoutez `mcpServers` à l'objet
existant sans supprimer les préférences déjà présentes :

```json
{
  "mcpServers": {
    "ano-gpt": {
      "command": "/usr/bin/python",
      "args": ["/home/anonymous/OUTILS/ANO-GPT/anogpt_mcp.py"]
    }
  }
}
```

Fermez complètement Claude Desktop puis relancez-le.

## Vérification

Vérifiez d'abord le serveur sans agent :

```bash
cd /home/anonymous/OUTILS/ANO-GPT
python anogpt_mcp.py --selftest
```

Le résultat doit lister les outils. Si ANO-GPT est lancé, le test appelle aussi
`assistant_status`; sinon il vérifie `system_status` avec le repli hors ligne.

Redémarrez ensuite l'agent et demandez-lui un appel réel, par exemple :

```text
Utilise l'outil ANO-GPT system_status pour me donner l'utilisation du CPU.
```

Pour vérifier le pont visuel, lancez ANO-GPT puis demandez :

```text
Utilise show_card pour afficher une fiche intitulée « Test MCP ».
```

## Dépannage

### L'agent ne voit pas `ano-gpt`

Commencez par déterminer **de quel côté** est la panne, sinon on corrige au
hasard un serveur qui fonctionne très bien :

```bash
python /home/anonymous/OUTILS/ANO-GPT/anogpt_mcp.py --selftest
```

S'il liste les outils, le serveur est hors de cause et le problème est dans la
configuration de l'agent. Dans ce cas :

- **Pour `agy`, vérifiez d'abord le fichier.** C'est
  `~/.gemini/config/mcp_config.json`, pas celui de `~/.gemini/antigravity-cli/`
  (voir la section Antigravity). C'est de loin la cause la plus fréquente.
- Relancez l'agent **complètement**. Les serveurs MCP sont découverts au
  démarrage, et le rechargement à chaud d'`agy` est cassé.
- Validez le JSON : `python -m json.tool < fichier.json`.
- Utilisez un chemin absolu vers l'interpréteur (`/usr/bin/python`) : les agents
  lancent les serveurs sans shell.
- Vérifiez le cache de découverte de l'agent
  (`~/.gemini/antigravity-cli/mcp/ano-gpt/` pour Antigravity) : son absence
  signifie que l'agent n'a même pas essayé de lancer le serveur.

Pour reproduire exactement ce que fait l'agent, avec un environnement dépouillé :

```bash
cd ~ && env -i PATH=/usr/bin HOME="$HOME" \
  /usr/bin/python /home/anonymous/OUTILS/ANO-GPT/anogpt_mcp.py --selftest
```

### ANO-GPT est éteint

Les outils sans interface s'exécutent dans le processus MCP et commencent leur
réponse par `[ANO-GPT éteint, exécuté hors application]`. Les outils visuels
refusent clairement l'action. Lancez ANO-GPT, attendez son démarrage, puis
réessayez.

### Un socket est resté après un crash

Le socket se trouve normalement dans `$XDG_RUNTIME_DIR/anogpt.sock`. Relancez
d'abord ANO-GPT : son serveur vérifie qu'aucune autre instance n'écoute avant
de retirer automatiquement un socket résiduel. Ne supprimez le fichier à la
main que si ANO-GPT est arrêté et que la relance échoue encore :

```bash
test -S "${XDG_RUNTIME_DIR}/anogpt.sock" && rm "${XDG_RUNTIME_DIR}/anogpt.sock"
```

Ne faites jamais cette suppression pendant qu'une autre instance fonctionne.

## Limites de sécurité

`shell_exec`, `shutdown_jarvis`, `computer_control` et `dev_agent` ne sont pas
exposés. Ils permettraient des actions trop larges sans consentement dédié.
L'outil `clipboard` n'est pas exposé non plus : l'action annoncée dans
`actions/desktop.py` n'existe pas actuellement, et le serveur MCP ne duplique
pas de logique métier.
