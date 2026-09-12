# Serveur MCP d'ANO-GPT — passation à Codex

Ce document décrit un travail **déjà commencé**. L'architecture, le protocole et
un exemple par catégorie existent, sont testés et fonctionnent. Ta part est le
volume : compléter les outils sur le motif établi, écrire les configurations
d'agents et la documentation utilisateur.

**Ne redessine pas l'architecture.** Elle a été choisie après avoir lu le code
existant, et un changement de protocole casserait ce qui marche déjà.

---

## 1. Ce que ça fait, en une phrase

Un agent externe (Antigravity `agy`, Claude Code, Codex CLI, Claude Desktop)
devient le cerveau d'ANO-GPT : il appelle par MCP les outils de l'assistant —
carte, caméra, voix, Gmail, machine, musique — qui s'exécutent dans
l'application réellement affichée devant l'utilisateur.

```
agy / claude / codex
        │  stdio (MCP)
        ▼
  anogpt_mcp.py                    ← serveur MCP, ne contient aucune logique métier
        │  socket Unix, une ligne JSON aller / retour
        ▼
  core/tool_bridge.py              ← protocole + client + répartiteur
        │
        ▼
  main.py : JarvisLive._agent_tools()   ← les outils réels, dans l'app qui tourne
```

## 2. Ce qui existe déjà (ne pas refaire)

| Fichier | Rôle | État |
|---|---|---|
| `core/tool_bridge.py` | Protocole, client `call_app`, répartiteur `dispatch` | **Terminé** |
| `main.py` → `_start_control_server` | Commande IPC `tool` branchée sur le socket | **Terminé** |
| `main.py` → `_agent_tools()` | Table des outils exposés | **11 outils, à compléter** |
| `anogpt_mcp.py` | Serveur MCP stdio | **11 outils, à compléter** |
| `tests/test_tool_bridge.py` | 12 tests du protocole, dont un tour complet sur socket réel | **Terminé** |

Vérifié de bout en bout : `python anogpt_mcp.py --selftest`, plus une poignée
de main MCP réelle par stdio (11 outils listés, appels exécutés). La suite
complète passe : **311 tests**.

### Décisions déjà prises, et pourquoi

**Le socket Unix, pas le tableau de bord HTTP.** ANO-GPT expose déjà
`/api/command`, mais cette route *fait parler l'assistant* : elle passe par
Gemini. Or ici c'est l'agent qui réfléchit — il veut exécuter un outil, pas
demander à un autre modèle s'il faut l'exécuter. Le socket de contrôle
(`core/ipc.py`, `$XDG_RUNTIME_DIR/anogpt.sock`, 0600) est local, sans
authentification à inventer, et vit aussi longtemps que le processus.

**JSON sur une seule ligne.** Le protocole d'origine est « une ligne entrante,
une ligne sortante » et sert déjà `toggle`, `ask`, `status`. On le garde
intact : le JSON échappe lui-même les retours à la ligne, donc une liste de
pharmacies tient sur une ligne réseau. C'est testé (`test_un_resultat_
multiligne_survit_a_laller_retour`).

**Les outils tournent dans un fil.** Le socket est servi par la boucle asyncio
qui porte aussi l'audio. Un `find_nearby` de trois secondes exécuté là
hacherait la voix. D'où `await asyncio.to_thread(dispatch, ...)` dans `_tool`.
**Tes handlers ont donc le droit de bloquer** — c'est même attendu.

**Entrée et sortie en texte plat.** Un agent lit du texte ; lui rendre une
structure JSON l'obligerait à la reformater avant de la restituer à
l'utilisateur. Les handlers renvoient la même chaîne que celle que l'assistant
vocal prononcerait.

**Repli hors-ligne sélectif.** Quand ANO-GPT est éteint, les outils qui n'ont
pas besoin de l'écran (`find_nearby`, `email`, `system_status`) s'exécutent
dans le processus MCP et le disent. Ceux qui ont besoin de l'écran (caméra,
carte, voix) refusent clairement plutôt que de faire croire à une action
invisible.

---

## 3. Le motif à suivre

Ajouter un outil, c'est **deux fonctions**, toujours les mêmes.

### a) Côté application — `main.py`

Une méthode `_agent_<nom>`, qui prend le dictionnaire d'arguments et rend une
chaîne. Elle réutilise l'action existante du projet, jamais une réimplémentation.

```python
def _agent_weather(self, args: dict) -> str:
    from actions.weather_report import weather_report

    return weather_report({"city": self._arg(args, "city")})
```

Puis l'inscrire dans `_agent_tools()` :

```python
"weather": self._agent_weather,
```

`self._arg(args, "clé", "défaut")` normalise en chaîne nettoyée — utilise-le,
les agents envoient parfois `None` ou des nombres.

### b) Côté MCP — `anogpt_mcp.py`

```python
@mcp.tool()
def weather(city: str = "") -> str:
    """Météo actuelle et prévisions courtes.

    city : ville visée. Vide = la position de l'utilisateur.
    """
    return _run("weather", city=city)
```

Avec repli hors-ligne quand l'outil n'a pas besoin de l'écran :

```python
    def _offline() -> str:
        from actions.weather_report import weather_report
        return weather_report({"city": city})

    return _run("weather", offline=_offline, city=city)
```

Enfin, ajoute le nom dans le tuple `_TOOL_NAMES` (il sert au `--selftest`).

### La docstring est l'interface

C'est le seul texte que l'agent lit pour décider s'il appelle ton outil. Elle
doit dire **quand l'utiliser**, pas seulement ce qu'il fait, et lister les
valeurs acceptées. Compare :

- ❌ « Contrôle la caméra. »
- ✅ « Pilote la caméra affichée dans ANO-GPT (jamais une application externe).
  action : open | photo | video_start | … lens : 'front' (selfie…) ou 'back'.
  Demander l'objectif frontal bascule automatiquement sur le téléphone : lui
  seul possède deux objectifs. »

Regarde `find_nearby` et `camera` dans `anogpt_mcp.py` : c'est le niveau visé.

---

## 4. Ta part

### Tâche A — compléter les outils

Les 11 outils actuels couvrent un représentant par catégorie. À ajouter, en
réutilisant les actions existantes du projet (ne réécris aucune logique) :

| Outil MCP | Action existante à appeler | Écran requis |
|---|---|---|
| `weather` | `actions/weather_report.py` | non |
| `web_search` | `actions/smart_search.py` | non |
| `screenshot` | `actions/capture.py` → `capture_control` | non |
| `open_app` / `close_app` | `actions/desktop.py` | oui |
| `computer_settings` | `actions/computer_settings.py` | oui |
| `file_search` | `actions/file_controller.py` | non |
| `clipboard` | `actions/desktop.py` (presse-papiers) | oui |
| `memory_save` / `memory_search` | `memory/memory_manager.py` | non |
| `location` | `core/geolocation.py` | non |
| `youtube` | `actions/youtube_video.py` | oui |
| `reminder` | `actions/reminder.py` si présent | non |

Lis la signature réelle de chaque action avant de l'appeler : elles ne
partagent pas toutes la même forme (certaines prennent `session_memory`,
d'autres `player`, `ui` ou `speak`). Utilise `self._tool_session_memory` et
`self.ui` comme le font les handlers déjà écrits.

**Attention à `shell_exec`, `shutdown_jarvis`, `computer_control` et
`dev_agent`.** Ne les expose pas sans réfléchir : un agent qui pilote un shell
sur la machine de l'utilisateur mérite une décision explicite de sa part, pas
un ajout silencieux dans une liste. Laisse-les de côté et signale-le.

### Tâche B — les configurations d'agents

Écrire `docs/MCP.md` (documentation utilisateur, en français) avec la
configuration exacte pour les quatre agents. Chemins réels de cette machine :

- Le serveur : `/home/anonymous/OUTILS/ANO-GPT/anogpt_mcp.py`
- Python : `python` (le SDK `mcp` 1.29.0 est installé par pacman, à l'échelle système)

**Antigravity (`agy`)** — ⚠️ **`~/.gemini/config/mcp_config.json`**, et pas
`~/.gemini/antigravity-cli/mcp_config.json`.

Les deux fichiers existent, portent le même nom et contiennent tous deux
`jarvis-brain`. Celui du dossier `antigravity-cli/` semble évidemment être le
bon — il ne l'est pas. La CLI ne lit que le premier. Cette erreur a déjà été
commise sur ce projet : la configuration était valide, le serveur fonctionnait,
et `agy` répondait « No MCP servers configured » pendant une heure.

Le fichier contient déjà `jarvis-brain` : **ajoute** la clé, ne remplace pas.

```json
{
  "mcpServers": {
    "jarvis-brain": { "command": "/home/anonymous/.jarvis-orb/bin/jarvis-brain", "disabled": true },
    "ano-gpt": {
      "command": "/usr/bin/python",
      "args": ["/home/anonymous/OUTILS/ANO-GPT/anogpt_mcp.py"]
    }
  }
}
```

Chemin absolu vers l'interpréteur : `agy` lance sans shell, donc sans garantie
sur le `PATH`.

**Comment vérifier qu'une configuration d'agent marche vraiment.** Ne te fie
pas au fait que le JSON soit valide. Antigravity met en cache le schéma de
chaque outil découvert dans `~/.gemini/antigravity-cli/mcp/<serveur>/` — après
un redémarrage complet de l'agent, ce dossier doit exister et contenir un
fichier par outil. C'est la seule preuve que le serveur a réellement été lancé.

**Claude Code** — aucun serveur MCP global n'est déclaré aujourd'hui. Donne la
commande `claude mcp add` et l'équivalent en `.mcp.json` de projet.

**Codex CLI** — `~/.codex/config.toml`, section `[mcp_servers.ano_gpt]`.
Un exemple existe déjà dans ce fichier (`[mcp_servers.node_repl]`) : suis son
format exact.

**Claude Desktop** — `claude_desktop_config.json`, même schéma qu'Antigravity.

Ajoute dans `docs/MCP.md` une section de vérification (`--selftest`, puis un
appel réel depuis l'agent) et une section dépannage : que faire si l'agent ne
voit pas le serveur, si ANO-GPT est éteint, si le socket est resté après un
crash.

### Tâche C — les tests

Un fichier `tests/test_mcp_tools.py`. **Ne reteste pas le protocole** :
`tests/test_tool_bridge.py` s'en charge déjà. Teste ce qui t'est propre :

- chaque outil MCP existe et porte une docstring non vide (c'est l'interface) ;
- chaque nom exposé dans `anogpt_mcp.py` a bien son handler dans
  `_agent_tools()` — le décalage entre les deux listes est l'erreur la plus
  facile à commettre ici ;
- `_TOOL_NAMES` est synchronisé avec les outils réellement décorés ;
- les outils marqués « écran requis » ne définissent pas de repli hors-ligne,
  et inversement ;
- les handlers `_agent_*` de `main.py` sont appelables avec un dictionnaire
  vide sans lever d'exception (les agents oublient des arguments).

Le style des tests du projet : noms de fonctions en français décrivant le
comportement, docstring expliquant *pourquoi* la régression compte. Regarde
`tests/test_tool_bridge.py`.

---

## 5. Règles du projet

- **Français** pour les commentaires, docstrings et noms de tests. Le code
  (variables, fonctions) reste en anglais quand c'est l'usage du fichier.
- **Les commentaires expliquent le pourquoi**, jamais le quoi. Le projet en est
  plein d'excellents exemples — lis `core/ipc.py` ou `core/map_render.py`.
- **Aucune réimplémentation.** Chaque outil MCP appelle une action existante.
  Si une action manque, signale-le plutôt que d'en écrire une deuxième version.
- Lancer `python -m pytest tests/ -q` avant de conclure. Référence actuelle :
  **311 réussis, 1 xfail**. Ce nombre ne doit que monter.
- `python anogpt_mcp.py --selftest` doit rester vert.

## 6. Pièges connus

**La boucle agent ↔ assistant.** `ask_assistant` envoie une demande à Gemini,
qui peut appeler des outils, qui peuvent revenir vers l'agent. N'ajoute aucun
outil qui relance l'agent externe : le seul point d'entrée vers le cerveau
vocal doit rester `ask_assistant`, et il ne doit jamais être appelé depuis un
handler.

**L'apostrophe dans une f-string.** Python refuse `\'` dans une expression de
f-string. Un `_agent_status` a déjà cassé la compilation de `main.py` pour
cette raison : calcule les morceaux avant, puis assemble.

**Le rechargement à chaud d'`agy` est cassé.** `/mcp` tente d'arrêter les
serveurs en place, échoue (`failed to stop mcp instance: … signal: terminated`)
et laisse la liste **vide** — l'agent affiche alors « No MCP servers
configured » alors que tout est correct. Toujours quitter et relancer l'agent,
jamais recharger.

**Le socket survit à un crash.** `ControlServer.start()` teste si quelqu'un
écoute avant de supprimer un fichier de socket résiduel — ne contourne pas ce
garde-fou, il évite de tuer le socket d'une seconde instance légitime.

**Les quotas.** L'utilisateur pilote ANO-GPT depuis un abonnement Antigravity,
pas une clé API. Chaque outil bavard consomme du contexte à chaque tour :
préfère des retours courts et denses aux dumps complets.
