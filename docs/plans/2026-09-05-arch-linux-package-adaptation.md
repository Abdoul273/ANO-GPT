# Plan d'Implémentation Atomique TDD : Adaptation Stricte des Commandes à Arch Linux

## 1. Contexte & Objectif
Assurer que l'assistant ANO-GPT (et ses sous-systèmes) utilise exclusivement l'écosystème **Arch Linux** (`pacman`, `yay`), ne génère jamais de commandes Debian/Ubuntu (`apt`, `apt-get`, `dpkg`) ni de chaînes de repli multi-distribution (`|| sudo apt install -y ...`), et adapte automatiquement toute commande de gestion de paquets reçue vers les commandes natives Arch Linux.

## 2. Découpage Atomique des Tâches (Cycle TDD)

### Tâche 1 : Tests unitaires de normalisation & adaptation Arch (Red)
- **Fichier** : `tests/test_shell_exec_safety.py`
- **Actions** :
  - Ajouter des tests unitaires pour `adapt_command_for_arch` :
    - Élimination des replis multi-distro : `"sudo pacman -S nmap || sudo apt install -y nmap"` -> `"sudo pacman -S nmap"`
    - Traduction `apt install` : `"sudo apt install -y nmap"` -> `"sudo pacman -S --needed nmap"`
    - Traduction `apt-get install` : `"apt-get install -y wireshark-qt"` -> `"pacman -S --needed wireshark-qt"`
    - Traduction `apt update` : `"sudo apt update"` -> `"sudo pacman -Sy"`
    - Traduction `apt upgrade` : `"sudo apt upgrade -y"` -> `"sudo pacman -Syu"`
    - Traduction `apt remove` : `"sudo apt remove --purge nmap"` -> `"sudo pacman -Rns nmap"`
    - Traduction `apt search` : `"apt search ripgrep"` -> `"pacman -Ss ripgrep"`
  - Ajouter un test d'intégration dans `run_shell` vérifiant que l'appel avec `"sudo apt install -y nmap"` génère une demande de confirmation portant sur la commande adaptée `"sudo pacman -S --needed nmap"`.
- **Validation** : Exécuter `pytest tests/test_shell_exec_safety.py` et constater l'échec (Red).

### Tâche 2 : Implémentation de `adapt_command_for_arch` dans `actions/shell_exec.py` (Green)
- **Fichier** : `actions/shell_exec.py`
- **Actions** :
  - Définir `adapt_command_for_arch(command: str) -> str` :
    1. Nettoyage des fallbacks conditionnels (`|| sudo apt ...`, `|| apt-get ...`, `|| dnf ...`, etc.) si un gestionnaire Arch (`pacman`, `yay`, `paru`) est déjà en tête de chaîne.
    2. Conversion regex des commandes `apt` / `apt-get` / `dnf` / `yum` vers les équivalents `pacman -S --needed`, `pacman -Sy`, `pacman -Syu`, `pacman -Rns`, `pacman -Ss`.
    3. Nettoyage des drapeaux non applicables sous Arch (`-y`, `--yes`, `-q`, `--quiet`, `--assume-yes`).
  - Intégrer l'appel à `adapt_command_for_arch` dès le début de `run_shell(parameters, ...)` avant les vérifications de sécurité et de confirmation humaine.
- **Validation** : Exécuter `pytest tests/test_shell_exec_safety.py` et vérifier le passage au vert (Green).

### Tâche 3 : Mise à jour du Prompt Système ANO-GPT
- **Fichier** : `core/prompt.txt`
- **Actions** :
  - Mettre à jour la section `ENVIRONNEMENT SYSTÈME` et `RÈGLES D'EXÉCUTION` pour interdire explicitement `apt` / `apt-get` et les chaînes de fallback multi-distros.
  - Exiger l'adaptation systématique de toute demande utilisateur formulée en syntaxe Debian vers `pacman` / `yay`.
- **Validation** : Inspection textuelle du prompt.

### Tâche 4 : Mise à jour des Consignes Agents de Développement
- **Fichier** : `AGENTS.md`
- **Actions** :
  - Ajouter la règle impérative : Système hôte Arch Linux (EndeavourOS) — toute commande proposée ou exécutée doit employer exclusivement `pacman` / `yay`.
- **Validation** : Relecture du fichier `AGENTS.md`.

### Tâche 5 : Double Revue (Conformité & Qualité) et Preuves Tangibles
- **Actions** :
  - Porte 1 : Revue de conformité aux exigences.
  - Porte 2 : Revue de qualité et robustesse.
  - Exécution complète des tests automatisés avec code de retour 0.
