---
name: ruff
description: Vérification et amélioration du code Python d'ANO-GPT avec ruff — barrière obligatoire après chaque modification, audit de qualité sur les fichiers touchés, jamais de reformatage en masse. À utiliser après toute édition d'un .py, avant de commiter, et quand l'utilisateur demande « lint », « ruff », « nettoie », « qualité du code ».
---

# Ruff sur ANO-GPT

Ruff (`~/.local/bin/ruff`, 0.16+) est configuré dans `pyproject.toml` en deux niveaux.
Le dépôt est **partagé entre plusieurs agents** en parallèle : on ne touche qu'aux
fichiers qu'on modifie déjà, et on ne stage que ses propres hunks.

## 1. Barrière — après CHAQUE modification, avant CHAQUE commit

```bash
ruff check .
```

Doit rendre `All checks passed!`. Ce niveau ne contient que des règles qui
signalent de vrais bugs (nom indéfini, variable de boucle capturée par une
lambda, `# noqa` mort, défaut mutable de dataclass, `exec`/`eval`). Toute
erreur se corrige immédiatement, dans le même commit ; on n'ajoute jamais de
`# noqa` ni d'exclusion pour faire passer la barrière — sauf `S102`/`S307`
dans un bac à sable explicite, déjà listé dans `per-file-ignores`.

Fixes automatiques sûrs, limités aux fichiers touchés :

```bash
ruff check --fix chemin/du/fichier.py
```

## 2. Audit — sur les fichiers qu'on touche

```bash
ruff check --extend-select B,ASYNC,RUF006,RUF012,RUF013,RUF059,PLW1510,PGH003,SIM,UP --output-format concise chemin/du/fichier.py
```

Règles de qualité, non bloquantes. Quand on édite un fichier, on traite ce
qui concerne les lignes qu'on écrit ou leur voisinage immédiat, en priorité :

| Règle | Pourquoi ça compte ici |
|---|---|
| `RUF006` tâche asyncio non gardée | la tâche peut être ramassée en plein vol — utiliser `core.background_task.spawn_logged` |
| `ASYNC110` `sleep` en boucle `while` | boucle d'attente : préférer un `asyncio.Event` ; sinon laisser, c'est un choix de conception documenté |
| `ASYNC230/240` E/S bloquante dans une coroutine | Qt et l'audio partagent le GIL : passer par `asyncio.to_thread` / le pool |
| `PLW1510` `subprocess.run` sans `check=` | tout appel externe passe par `core/action_kit.py` de toute façon |
| `B904` `raise` sans `from` dans un `except` | conserve la cause dans les rapports de gel |
| `RUF012` attribut de classe mutable | état partagé entre instances à l'insu de tous |
| `RUF013` `Optional` implicite | `x: str = None` → `x: str \| None = None` |
| `SIM105` `try/except/pass` | `contextlib.suppress(...)` si c'est vraiment volontaire |

Vue d'ensemble du dépôt (pour choisir un chantier, pas pour tout corriger) :

```bash
ruff check --extend-select B,ASYNC,RUF006,RUF012,RUF013,RUF059,PLW1510,PGH003,SIM,UP --statistics .
```

## 3. Formatage — uniquement les fichiers créés

```bash
ruff format chemin/du/nouveau_fichier.py
```

**Jamais `ruff format .`** : ~5 000 lignes de plus de 110 caractères et des
alignements volontaires (déclarations d'outils, tableaux de constantes) — un
reformatage global produirait un diff illisible et entrerait en collision avec
le travail des autres agents. Sur un fichier existant, on garde son style.

## 4. Ce qu'on ne fait pas

- Pas de `# noqa` sans règle précise (`# noqa: F401`) ni sans raison en commentaire.
- Pas de modification de `pyproject.toml` pour faire taire une règle de la barrière.
- Pas de passe `--unsafe-fixes` sur des fichiers qu'on ne relit pas.
- `ruff check --fix .` sur tout le dépôt seulement si `git status` est propre
  (sinon on écrase le travail non commité des autres) — et on relit le diff.

## Ordre après une modification

1. `ruff check .` → 0 erreur.
2. audit sur le(s) fichier(s) touché(s) → corriger ce qui touche les lignes écrites.
3. vérification rapide existante (`pytest` ciblé, `python -m pyflakes` n'est plus nécessaire).
4. commit par fichier / par hunk, message en français, push.
