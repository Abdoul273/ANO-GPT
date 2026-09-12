#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
devsecops.py — Master DevSecOps & Pilote Avancé du Système Linux pour ANO-GPT.

Capacités :
1. Orchestration Docker & Docker-Compose :
   - Redémarrage / cycle de vie de stacks (compose up/down/restart)
   - Purge sécurisée des conteneurs morts et images inutilisées (prune)
   - Inspection des logs d'accès, d'erreurs et des diagnostics de conteneurs
   - Gestion des conteneurs (liste, statut de santé, inspection, redémarrage)

2. Maître Systemd & Journalctl :
   - Pilotage des services (status, start, stop, restart, enable, disable)
   - Détection et diagnostic automatique des services en échec (systemctl --failed)
   - Analyse causale des crashs via journalctl avec explications en français clair

3. Gestionnaire de Paquets Universel :
   - Détection multi-distributions (Arch: pacman/yay/paru, Debian/Ubuntu: apt, Fedora: dnf, Flatpak, Snap)
   - Recherche, vérification des mises à jour, installation sécurisée
   - Nettoyage des paquets orphelins et caches

4. Automatisation Git Intelligente :
   - Commits vocaux formulés proprement en Conventional Commits (feat, fix, refactor, docs, chore...)
   - Scanner de secrets pré-commit (bloque les fuites de clés API, tokens, clés privées)
   - Rebases assistés avec auto-stash et gestion de conflits
   - Gestion des branches, statut synthétique et log graphique

5. DevSecOps & Audit de Sécurité Linux :
   - Audit des ports réseau en écoute (ss / lsof) avec alerte sur les ports exposés
   - Détection des processus zombies et consommateurs anormaux
   - Rapport de sécurité global et recommandations de durcissement
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core import action_kit as kit

# ════════════════════════════════════════════════════════════════════════════
# Configuration & Helpers
# ════════════════════════════════════════════════════════════════════════════

def _base_dir() -> Path:
    import sys
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def _run_cmd(
    cmd: str | list[str],
    cwd: str | Path | None = None,
    timeout: float = 30.0,
    check: bool = False,
    env: dict | None = None,
) -> Tuple[int, str, str]:
    """Exécute une commande avec capture propre et timeout garanti."""
    work_dir = str(cwd) if cwd else str(Path.home())
    run_env = {**os.environ, **(env or {})}
    
    # Assurer que PATH contient les binaires standards
    extra_paths = ["/usr/local/bin", "/usr/bin", "/bin", "/usr/local/sbin", "/usr/sbin", "/sbin", str(Path.home() / ".local" / "bin")]
    current_path = run_env.get("PATH", "")
    for ep in extra_paths:
        if ep not in current_path:
            current_path = f"{ep}:{current_path}"
    run_env["PATH"] = current_path

    try:
        if isinstance(cmd, str):
            proc = subprocess.run(
                cmd,
                shell=True,
                cwd=work_dir,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=run_env,
            )
        else:
            proc = subprocess.run(
                cmd,
                shell=False,
                cwd=work_dir,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=run_env,
            )
        return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return -1, "", f"Délai d'exécution dépassé ({timeout}s)"
    except Exception as e:
        return -1, "", str(e)


# ════════════════════════════════════════════════════════════════════════════
# 1. ORCHESTRATEUR DOCKER
# ════════════════════════════════════════════════════════════════════════════

class DockerManager:
    """Gestionnaire complet et sécurisé de Docker et Docker-Compose."""

    @staticmethod
    def is_available() -> bool:
        return shutil.which("docker") is not None

    @staticmethod
    def find_compose_file(start_path: Path | str | None = None) -> Optional[Path]:
        """Localise un fichier compose.yaml / docker-compose.yml."""
        candidates = ["compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml"]
        search_dirs = []
        
        if start_path:
            p = Path(start_path).resolve()
            if p.is_file():
                return p
            search_dirs.append(p)
        
        search_dirs.extend([
            Path.cwd(),
            Path.home() / "OUTILS" / "ANO-GPT",
            Path.home() / "development",
            Path.home() / "Desktop",
            Path.home() / "projects",
            Path.home() / "docker",
        ])

        for d in search_dirs:
            if d.is_dir():
                for c in candidates:
                    target = d / c
                    if target.is_file():
                        return target
        return None

    @classmethod
    def restart_stack(cls, project_dir: str | Path | None = None, service: str | None = None) -> str:
        """Relance une stack Docker Compose avec vérification de santé."""
        if not cls.is_available():
            return "Docker n'est pas installé ou n'est pas dans le PATH."

        compose_file = cls.find_compose_file(project_dir)
        compose_cmd = "docker compose"
        # Test compatibilité docker-compose legacy
        code, out, _ = _run_cmd("docker compose version")
        if code != 0:
            if shutil.which("docker-compose"):
                compose_cmd = "docker-compose"
            else:
                return "Ni 'docker compose' ni 'docker-compose' ne sont disponibles."

        target_dir = compose_file.parent if compose_file else (Path(project_dir) if project_dir else Path.cwd())
        
        svc_arg = f" {shlex.quote(service)}" if service else ""
        file_arg = f" -f {shlex.quote(str(compose_file))}" if compose_file else ""
        
        # Redémarrage propre
        cmd = f"{compose_cmd}{file_arg} restart{svc_arg}"
        code, out, err = _run_cmd(cmd, cwd=target_dir, timeout=60.0)
        
        if code != 0:
            # Si restart échoue car conteneurs non démarrés, tentative up -d
            up_cmd = f"{compose_cmd}{file_arg} up -d{svc_arg}"
            code_up, out_up, err_up = _run_cmd(up_cmd, cwd=target_dir, timeout=90.0)
            if code_up == 0:
                return f"Stack Docker lancée avec succès dans {target_dir.name} ({compose_file.name if compose_file else 'compose'})."
            return f"Échec du redémarrage de la stack Docker : {err or err_up or out}"

        # Récupération rapide des services actifs
        ps_cmd = f"{compose_cmd}{file_arg} ps --format '{{{{.Name}}}} ({{{{.Status}}}})'"
        _, ps_out, _ = _run_cmd(ps_cmd, cwd=target_dir, timeout=10.0)
        services_summary = f"\nServices :\n{ps_out}" if ps_out else ""

        return f"Stack Docker redémarrée avec succès dans {target_dir.name}.{services_summary}"

    @classmethod
    def purge_dead_containers(cls, prune_images: bool = True, prune_volumes: bool = False) -> str:
        """Purge les conteneurs arrêtés/morts et nettoie les ressources orphelines."""
        if not cls.is_available():
            return "Docker n'est pas installé."

        results = []
        # 1. Purge des conteneurs arrêtés
        code, out, err = _run_cmd("docker container prune -f", timeout=30.0)
        if code == 0:
            reclaimed_match = re.search(r"Total reclaimed space:\s*([^\n]+)", out)
            reclaimed = reclaimed_match.group(1) if reclaimed_match else "0B"
            deleted = [line for line in out.splitlines() if line and not line.startswith("Deleted") and not line.startswith("Total")]
            count = len(deleted)
            results.append(f"Conteneurs morts purgés : {count} ({reclaimed} libérés)")
        else:
            results.append(f"Purge conteneurs : {err or 'aucun conteneur à purger'}")

        # 2. Purge des images pendantes (dangling) si demandé
        if prune_images:
            code_img, out_img, _ = _run_cmd("docker image prune -f", timeout=40.0)
            if code_img == 0:
                reclaimed_match = re.search(r"Total reclaimed space:\s*([^\n]+)", out_img)
                reclaimed_img = reclaimed_match.group(1) if reclaimed_match else "0B"
                results.append(f"Images orphelines purgées ({reclaimed_img} libérés)")

        # 3. Purge volumes non utilisés si explicitement demandé
        if prune_volumes:
            code_vol, out_vol, _ = _run_cmd("docker volume prune -f", timeout=30.0)
            if code_vol == 0:
                results.append("Volumes inutilisés purgés")

        return " | ".join(results)

    @classmethod
    def get_logs(
        cls,
        target: str | None = None,
        tail: int = 50,
        access_logs: bool = False,
        error_logs: bool = False,
        project_dir: str | Path | None = None,
    ) -> str:
        """Récupère et filtre intelligemment les logs de conteneurs ou de stack."""
        if not cls.is_available():
            return "Docker n'est pas installé."

        cmd = ""
        if target:
            cmd = f"docker logs --tail {tail} {shlex.quote(target)}"
        else:
            compose_file = cls.find_compose_file(project_dir)
            if compose_file:
                cmd = f"docker compose -f {shlex.quote(str(compose_file))} logs --tail {tail}"
            else:
                _, out_ps, _ = _run_cmd("docker ps --format '{{.Names}}'")
                containers = [c.strip() for c in out_ps.splitlines() if c.strip()]
                if not containers:
                    return "Aucun conteneur Docker en cours d'exécution."
                
                chosen = containers[0]
                if access_logs:
                    for c in containers:
                        if any(k in c.lower() for k in ["nginx", "caddy", "apache", "web", "http", "api", "gateway", "proxy"]):
                            chosen = c
                            break
                cmd = f"docker logs --tail {tail} {shlex.quote(chosen)}"

        code, out, err = _run_cmd(cmd, timeout=20.0)
        logs = out or err or "Aucun log produit."
        lines = logs.splitlines()

        # Filtrage spécifique access logs
        if access_logs:
            http_pattern = re.compile(r'(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\s+\S+|\bHTTP/\d\.\d\b|\b(?:200|201|204|301|302|304|400|401|403|404|500|502|503)\b')
            filtered = [l for l in lines if http_pattern.search(l)]
            if filtered:
                lines = filtered[-tail:]
            else:
                lines = lines[-tail:]

        # Filtrage spécifique error logs
        if error_logs:
            err_pattern = re.compile(r'\b(error|fatal|exception|panic|failed|crit|emerg|traceback)\b', re.IGNORECASE)
            filtered = [l for l in lines if err_pattern.search(l)]
            if filtered:
                lines = filtered[-tail:]

        output = "\n".join(lines[-tail:])
        if len(output) > 4000:
            output = output[-4000:]
        return f"=== Logs Docker ({target or 'Stack'}) ===\n{output}"

    @classmethod
    def list_containers(cls, show_all: bool = True) -> str:
        """Liste les conteneurs avec formatage lisible."""
        if not cls.is_available():
            return "Docker n'est pas disponible sur ce système."

        flag = "-a" if show_all else ""
        fmt = "table {{.Names}}\t{{.Status}}\t{{.Image}}\t{{.Ports}}"
        code, out, err = _run_cmd(f"docker ps {flag} --format '{fmt}'", timeout=15.0)
        
        if code != 0:
            return f"Erreur lors de la lecture des conteneurs : {err}"
        if not out.strip():
            return "Aucun conteneur Docker présent."
        return out


# ════════════════════════════════════════════════════════════════════════════
# 2. MAÎTRE SYSTEMD & JOURNALCTL
# ════════════════════════════════════════════════════════════════════════════

class SystemdMaster:
    """Gestionnaire de services Systemd et inspecteur de pannes journalctl."""

    @staticmethod
    def is_available() -> bool:
        return shutil.which("systemctl") is not None

    @classmethod
    def service_action(cls, action: str, service: str, user_mode: bool = False) -> str:
        """Effectue une action systemd (status, start, stop, restart, enable, disable)."""
        if not cls.is_available():
            return "Systemd n'est pas présent sur ce système."

        action = action.lower().strip()
        allowed = {"status", "start", "stop", "restart", "reload", "enable", "disable", "is-active", "is-failed"}
        if action not in allowed:
            return f"Action systemd non reconnue : {action}. Actions autorisées : {', '.join(allowed)}"

        user_flag = "--user " if user_mode else ""
        service_name = service if service.endswith((".service", ".timer", ".socket", ".target")) else f"{service}.service"
        
        cmd = f"systemctl {user_flag}{action} {shlex.quote(service_name)}"
        code, out, err = _run_cmd(cmd, timeout=20.0)

        if action == "status":
            if out:
                active_match = re.search(r"Active:\s*([^\n]+)", out)
                active_str = active_match.group(1) if active_match else ("actif" if code == 0 else "inactif")
                main_pid = re.search(r"Main PID:\s*([^\n]+)", out)
                pid_str = f" | PID: {main_pid.group(1)}" if main_pid else ""
                
                log_lines = [l for l in out.splitlines() if re.search(r"^[A-Z][a-z]{2}\s+\d+", l) or "systemd[" in l]
                recent_logs = "\n".join(log_lines[-5:]) if log_lines else ""
                
                res = f"Service {service_name} : {active_str}{pid_str}"
                if recent_logs:
                    res += f"\nLogs récents :\n{recent_logs}"
                return res
            return f"Service {service_name} : statut inconnu (code {code}) - {err}"

        if code == 0:
            return f"Service {service_name} : action '{action}' réussie."
        return f"Échec de l'action '{action}' sur {service_name} : {err or out}"

    @classmethod
    def list_failed(cls) -> str:
        """Trouve tous les services en échec (système et utilisateur)."""
        if not cls.is_available():
            return "Systemd n'est pas disponible."

        results = []
        code, out, _ = _run_cmd("systemctl --failed --no-legend --plain", timeout=10.0)
        failed_system = [l.split()[0] for l in out.splitlines() if l.strip()] if code == 0 else []

        code_u, out_u, _ = _run_cmd("systemctl --user --failed --no-legend --plain", timeout=10.0)
        failed_user = [l.split()[0] for l in out_u.splitlines() if l.strip()] if code_u == 0 else []

        if not failed_system and not failed_user:
            return "Aucun service en échec sur le système (tout est nominal)."

        if failed_system:
            results.append(f"Services système en échec ({len(failed_system)}) : {', '.join(failed_system)}")
        if failed_user:
            results.append(f"Services utilisateur en échec ({len(failed_user)}) : {', '.join(failed_user)}")

        return "\n".join(results)

    @classmethod
    def diagnose_service(cls, service: str, user_mode: bool = False) -> str:
        """Analyse la cause d'un crash de service via journalctl et synthétise l'erreur."""
        if not shutil.which("journalctl"):
            return "journalctl introuvable."

        user_flag = "--user " if user_mode else ""
        service_name = service if service.endswith((".service", ".timer", ".socket")) else f"{service}.service"
        
        cmd = f"journalctl {user_flag}-u {shlex.quote(service_name)} -n 30 --no-pager"
        code, out, err = _run_cmd(cmd, timeout=15.0)

        if not out.strip():
            return f"Aucun log trouvé pour le service {service_name}."

        lines = out.splitlines()
        error_lines = [
            l for l in lines
            if re.search(r'\b(error|failed|fault|crash|fatal|exception|denied|exit-code|oom)\b', l, re.IGNORECASE)
        ]

        summary = f"=== Diagnostic Systemd : {service_name} ===\n"
        if error_lines:
            summary += "Lignes d'erreurs identifiées :\n" + "\n".join(error_lines[-6:])
        else:
            summary += "Derniers événements :\n" + "\n".join(lines[-6:])

        return summary


# ════════════════════════════════════════════════════════════════════════════
# 3. GESTIONNAIRE DE PAQUETS UNIVERSEL
# ════════════════════════════════════════════════════════════════════════════

class PackageManager:
    """Gestionnaire unifié de paquets pour Arch, Debian, Fedora, Flatpak."""

    @classmethod
    def detect_backend(cls) -> str:
        if shutil.which("pacman"):
            if shutil.which("yay"):
                return "yay"
            if shutil.which("paru"):
                return "paru"
            return "pacman"
        if shutil.which("apt"):
            return "apt"
        if shutil.which("dnf"):
            return "dnf"
        if shutil.which("flatpak"):
            return "flatpak"
        return "unknown"

    @classmethod
    def check_updates(cls) -> str:
        backend = cls.detect_backend()
        
        if backend in ("pacman", "yay", "paru"):
            if shutil.which("checkupdates"):
                code, out, _ = _run_cmd("checkupdates", timeout=25.0)
            elif backend in ("yay", "paru"):
                code, out, _ = _run_cmd(f"{backend} -Qu", timeout=30.0)
            else:
                code, out, _ = _run_cmd("pacman -Qu", timeout=20.0)

            if code == 0 and out.strip():
                pkgs = [l.split()[0] for l in out.splitlines() if l.strip()]
                return f"{len(pkgs)} mise(s) à jour disponible(s) ({backend}) : {', '.join(pkgs[:10])}{'...' if len(pkgs) > 10 else ''}"
            return "Système entièrement à jour (aucun paquet en attente)."

        if backend == "apt":
            code, out, _ = _run_cmd("apt list --upgradable", timeout=25.0)
            lines = [l for l in out.splitlines() if "Listing..." not in l and "/" in l]
            if lines:
                return f"{len(lines)} mise(s) à jour disponible(s) (apt) : {', '.join([l.split('/')[0] for l in lines[:10]])}"
            return "Système apt à jour."

        if backend == "dnf":
            code, out, _ = _run_cmd("dnf check-update -q", timeout=30.0)
            lines = [l for l in out.splitlines() if l.strip()]
            if lines:
                return f"{len(lines)} mise(s) à jour disponible(s) (dnf)."
            return "Système dnf à jour."

        return f"Vérification non supportée pour le backend : {backend}"

    @classmethod
    def search_package(cls, query: str) -> str:
        backend = cls.detect_backend()
        query_safe = shlex.quote(query)

        if backend in ("yay", "paru"):
            code, out, err = _run_cmd(f"{backend} -Ss {query_safe}", timeout=25.0)
        elif backend == "pacman":
            code, out, err = _run_cmd(f"pacman -Ss {query_safe}", timeout=20.0)
        elif backend == "apt":
            code, out, err = _run_cmd(f"apt search {query_safe}", timeout=20.0)
        elif backend == "dnf":
            code, out, err = _run_cmd(f"dnf search {query_safe}", timeout=25.0)
        else:
            return "Gestionnaire de paquets inconnu."

        if code != 0 or not out.strip():
            return f"Aucun paquet trouvé pour '{query}'."

        lines = out.splitlines()[:15]
        return f"Résultats de recherche ({backend}) pour '{query}' :\n" + "\n".join(lines)

    @classmethod
    def clean_orphans(cls) -> str:
        backend = cls.detect_backend()
        
        if backend in ("pacman", "yay", "paru"):
            code, out, _ = _run_cmd("pacman -Qtdq", timeout=15.0)
            orphans = [l.strip() for l in out.splitlines() if l.strip()]
            if not orphans:
                return "Aucun paquet orphelin détecté sur le système."
            return f"{len(orphans)} paquet(s) orphelin(s) détecté(s) : {', '.join(orphans)}. Commande pour nettoyer : sudo pacman -Rns $(pacman -Qtdq)"

        if backend == "apt":
            return "Nettoyage des orphelins : exécuter 'sudo apt autoremove --purge'."

        return "Nettoyage des orphelins non requis ou non supporté."


# ════════════════════════════════════════════════════════════════════════════
# 4. AUTOMATISATION GIT INTELLIGENTE & PRE-COMMIT SECRET SCANNER
# ════════════════════════════════════════════════════════════════════════════

class GitMaster:
    """Automatisation Git intelligente avec formattage sémantique et scan de sécurité."""

    SECRET_PATTERNS = [
        (r'AIza[0-9A-Za-z-_]{35}', "Clé API Google Gemini"),
        (r'sk-(?:proj-)?[a-zA-Z0-9_\-]{20,}', "Clé API OpenAI"),
        (r'ghp_[0-9a-zA-Z]{36}', "GitHub Personal Access Token"),
        (r'gho_[0-9a-zA-Z]{36}', "GitHub OAuth Token"),
        (r'glpat-[0-9a-zA-Z-_]{20,}', "GitLab Personal Access Token"),
        (r'-----BEGIN\s+(?:RSA|OPENSSH|EC|DSA)?\s*PRIVATE KEY-----', "Clé privée SSH / RSA"),
        (r'AKIA[0-9A-Z]{16}', "Clé d'accès AWS (Access Key ID)"),
        (r'(?i)(?:api_key|secret_key|auth_token|bearer|password)\s*[:=]\s*["\'][a-zA-Z0-9_\-\.]{12,}["\']', "Token / Secret en clair"),
    ]

    @classmethod
    def scan_diff_for_secrets(cls, diff_content: str) -> List[Tuple[str, str]]:
        detected = []
        added_lines = [
            line for line in diff_content.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        ]
        
        for line in added_lines:
            for pattern, name in cls.SECRET_PATTERNS:
                if re.search(pattern, line):
                    detected.append((name, line.strip()[:80]))
        return detected

    @classmethod
    def smart_commit(
        cls,
        user_message: str | None = None,
        stage_all: bool = True,
        repo_path: str | Path | None = None,
    ) -> str:
        target_dir = Path(repo_path).resolve() if repo_path else Path.cwd()
        
        code, _, _ = _run_cmd("git rev-parse --is-inside-work-tree", cwd=target_dir)
        if code != 0:
            return f"Le dossier {target_dir} n'est pas un dépôt Git."

        if stage_all:
            _run_cmd("git add -u", cwd=target_dir)
            _run_cmd("git add .", cwd=target_dir)

        code_diff, diff_out, _ = _run_cmd("git diff --cached", cwd=target_dir, timeout=20.0)
        if not diff_out.strip():
            code_st, st_out, _ = _run_cmd("git status --porcelain", cwd=target_dir)
            if not st_out.strip():
                return "Rien à commiter : l'arbre de travail est parfaitement propre."
            return "Aucune modification préparée (staged) pour le commit."

        # DevSecOps Guard : Scan de sécurité pré-commit
        secrets = cls.scan_diff_for_secrets(diff_out)
        if secrets:
            secret_types = ", ".join({s[0] for s in secrets})
            return (
                f"🚨 COMMIT BLOQUÉ PAR SÉCURITÉ (DevSecOps) : "
                f"Un secret potentiel a été détecté dans les fichiers préparés ({secret_types}) ! "
                f"Supprimez le token ou la clé avant de commiter."
            )

        commit_msg = cls._generate_conventional_message(user_message, diff_out, target_dir)

        code_c, out_c, err_c = _run_cmd(["git", "commit", "-m", commit_msg], cwd=target_dir, timeout=15.0)
        if code_c != 0:
            return f"Échec du commit Git : {err_c or out_c}"

        _, branch, _ = _run_cmd("git rev-parse --abbrev-ref HEAD", cwd=target_dir)
        _, commit_hash, _ = _run_cmd("git rev-parse --short HEAD", cwd=target_dir)

        return f"✅ Commit réussi [{branch or 'main'} {commit_hash}] : {commit_msg.splitlines()[0]}"

    @classmethod
    def _generate_conventional_message(
        cls,
        user_intent: str | None,
        diff_content: str,
        repo_dir: Path,
    ) -> str:
        if user_intent and len(user_intent.strip()) > 5:
            text = user_intent.strip()
            text = re.sub(r'^(commit|fais un commit|enregistre|commit en disant|disant que|en disant)\s+', '', text, flags=re.IGNORECASE)
            
            t_lower = text.lower()
            prefix = "feat"
            if any(w in t_lower for w in ["fix", "corrige", "corrigé", "bug", "erreur", "patch"]):
                prefix = "fix"
            elif any(w in t_lower for w in ["refactor", "nettoie", "réorganise", "clean"]):
                prefix = "refactor"
            elif any(w in t_lower for w in ["doc", "readme", "commentaire"]):
                prefix = "docs"
            elif any(w in t_lower for w in ["test", "tests", "pytest"]):
                prefix = "test"
            elif any(w in t_lower for w in ["style", "format", "lint"]):
                prefix = "style"
            
            if re.match(r'^(feat|fix|refactor|docs|test|style|chore|perf)(\([^)]+\))?:', text):
                return text
            return f"{prefix}: {text}"

        files_changed = re.findall(r'diff --git a/(\S+)', diff_content)
        if any("test" in f.lower() for f in files_changed):
            return "test: update and add automated tests"
        if any("doc" in f.lower() or f.endswith(".md") for f in files_changed):
            return "docs: update documentation and roadmaps"
        if any(f.endswith((".py", ".js", ".ts", ".rs", ".go")) for f in files_changed):
            return "feat: enhance system controllers and action runtimes"
        return "chore: update project files and configurations"

    @classmethod
    def get_status_summary(cls, repo_path: str | Path | None = None) -> str:
        target_dir = Path(repo_path).resolve() if repo_path else Path.cwd()
        code, branch, _ = _run_cmd("git rev-parse --abbrev-ref HEAD", cwd=target_dir)
        if code != 0:
            return f"{target_dir.name} n'est pas un dépôt Git."

        _, status_out, _ = _run_cmd("git status --short", cwd=target_dir)
        _, ahead_behind, _ = _run_cmd("git rev-list --left-right --count HEAD...@{u}", cwd=target_dir)
        
        lines = [l.strip() for l in status_out.splitlines() if l.strip()]
        staged = [l for l in lines if l[0] in "MADRC"]
        unstaged = [l for l in lines if l[0] == " " or (len(l) > 1 and l[1] in "MD")]
        untracked = [l for l in lines if l.startswith("??")]

        ahead_str = ""
        if ahead_behind and "\t" in ahead_behind:
            ahead, behind = ahead_behind.split("\t")
            if int(ahead) > 0:
                ahead_str += f" | {ahead} commit(s) d'avance"
            if int(behind) > 0:
                ahead_str += f" | {behind} commit(s) de retard"

        return (
            f"Branche : {branch}{ahead_str}\n"
            f"Fichiers préparés (staged) : {len(staged)} | "
            f"Modifiés non indexés : {len(unstaged)} | "
            f"Non suivis : {len(untracked)}"
        )

    @classmethod
    def rebase_branch(cls, target: str = "main", auto_stash: bool = True, repo_path: str | Path | None = None) -> str:
        target_dir = Path(repo_path).resolve() if repo_path else Path.cwd()
        stash_flag = "--autostash" if auto_stash else ""
        
        cmd = f"git rebase {stash_flag} {shlex.quote(target)}"
        code, out, err = _run_cmd(cmd, cwd=target_dir, timeout=40.0)

        if code == 0:
            return f"Rebase sur '{target}' terminé avec succès."
        
        if "conflict" in (out + err).lower():
            return (
                f"⚠️ Conflit de rebase détecté sur '{target}'. "
                f"Fichiers en conflit signalés par Git. "
                f"Pour annuler : 'git rebase --abort'."
            )
        return f"Échec du rebase : {err or out}"


# ════════════════════════════════════════════════════════════════════════════
# 5. LINUX DEVSECOPS & AUDIT DE SÉCURITÉ
# ════════════════════════════════════════════════════════════════════════════

class SecurityAuditor:
    """Auditeur DevSecOps pour l'OS Linux : ports, processus, droits et secrets."""

    @classmethod
    def audit_listening_ports(cls) -> str:
        cmd = "ss -tulpn" if shutil.which("ss") else "netstat -tulpn"
        code, out, err = _run_cmd(cmd, timeout=10.0)

        if code != 0 or not out.strip():
            return "Impossible de scanner les ports (commande 'ss' ou 'netstat' requise)."

        lines = out.splitlines()
        listening = []
        exposed_public = []

        for line in lines[1:]:
            parts = line.split()
            if len(parts) >= 5:
                proto = parts[0]
                local_addr = parts[4]
                proc_info = parts[-1] if len(parts) >= 7 and ("users:" in line or "pid" in line) else ""
                
                listening.append(f"{proto.upper()} {local_addr} {proc_info}".strip())
                if local_addr.startswith("0.0.0.0:") or local_addr.startswith("[::]:") or local_addr.startswith("*:") :
                    port = local_addr.split(":")[-1]
                    exposed_public.append(f"Port {port} ({proto}) ouvert publiquement {proc_info}")

        report = f"Audit Réseau : {len(listening)} service(s) en écoute.\n"
        if exposed_public:
            report += "⚠️ Ports ouverts sur l'extérieur (0.0.0.0 / [::]) :\n" + "\n".join(exposed_public[:8])
        else:
            report += "✅ Tous les services écoutent exclusivement sur localhost (127.0.0.1)."
        return report

    @classmethod
    def audit_system_security(cls) -> str:
        reports = []
        reports.append(cls.audit_listening_ports())

        if shutil.which("ufw"):
            _, ufw_out, _ = _run_cmd("sudo -n ufw status 2>/dev/null || ufw status", timeout=5.0)
            if "active" in ufw_out.lower():
                reports.append("✅ Pare-feu UFW : Actif.")
            else:
                reports.append("⚠️ Pare-feu UFW : Inactif ou désactivé.")
        elif shutil.which("iptables"):
            reports.append("ℹ️ iptables est installé.")

        try:
            import psutil
            zombies = [p.pid for p in psutil.process_iter(['pid', 'status']) if p.info['status'] == psutil.STATUS_ZOMBIE]
            if zombies:
                reports.append(f"⚠️ {len(zombies)} processus zombie(s) détecté(s) (PIDs: {zombies[:5]}).")
            else:
                reports.append("✅ Aucun processus zombie détecté.")
        except Exception:
            pass

        return "\n\n".join(reports)


# ════════════════════════════════════════════════════════════════════════════
# PARSING VOCAL LOCAL & DISPATCHER UNIFIÉ
# ════════════════════════════════════════════════════════════════════════════

def parse_devsecops_intent(text: str) -> Optional[Dict[str, Any]]:
    """Détecte localement en zéro latence l'intention DevSecOps."""
    text_clean = text.lower().strip()
    text_clean = re.sub(r"\b(s'il te pla[iî]t|stp|please|peux-tu|tu peux|je veux|j'aimerais|fais|lance)\b", "", text_clean).strip()

    # 1. Docker : Relance stack / purge conteneurs / logs
    if "docker" in text_clean or "conteneur" in text_clean or "compose" in text_clean:
        if any(k in text_clean for k in ["purge", "nettoie", "prune", "supprime"]) and any(k in text_clean for k in ["mort", "inutile", "arret", "arrêt", "orphelin", "conteneur"]):
            return {"domain": "docker", "action": "purge_dead"}
        
        if any(k in text_clean for k in ["relance", "redemarre", "redémarre", "restart", "up"]) and any(k in text_clean for k in ["stack", "compose", "docker"]):
            return {"domain": "docker", "action": "restart_stack"}
        
        if any(k in text_clean for k in ["log", "logs", "journal"]) and "docker" in text_clean:
            is_access = "acces" in text_clean or "accès" in text_clean or "http" in text_clean
            is_error = "erreur" in text_clean or "error" in text_clean or "crash" in text_clean
            return {"domain": "docker", "action": "logs", "access_logs": is_access, "error_logs": is_error}
        
        if any(k in text_clean for k in ["liste", "affiche", "quels", "statut"]) and any(k in text_clean for k in ["conteneur", "docker"]):
            return {"domain": "docker", "action": "list"}

    # 2. Systemd
    if "systemd" in text_clean or "service" in text_clean or "systemctl" in text_clean or "journalctl" in text_clean:
        if any(k in text_clean for k in ["echec", "échec", "failed", "plante", "crash"]):
            return {"domain": "systemd", "action": "list_failed"}
        
        m_svc = re.search(r"(?:statut|etat|état|info|status|relance|redemarre|redémarre|restart|stop|arrete|arrête)\s+(?:du\s+service\s+|le\s+service\s+|service\s+|du\s+|le\s+|la\s+)?([a-zA-Z0-9_\-\.]+)", text_clean)
        if m_svc:
            svc_name = m_svc.group(1)
            act = "restart" if any(k in text_clean for k in ["relance", "redemarre", "redémarre", "restart"]) else "status"
            return {"domain": "systemd", "action": act, "service": svc_name}

    # 3. Paquets & Mises à jour
    if any(k in text_clean for k in ["paquet", "paquets", "update", "upgrade", "mise a jour", "mises à jour", "pacman", "yay", "apt"]):
        if any(k in text_clean for k in ["verifie", "vérifie", "check", "cherche", "y a-t-il", "disponible"]):
            return {"domain": "package", "action": "check_updates"}
        if any(k in text_clean for k in ["orphelin", "orphelins", "inutilise", "inutilisés"]):
            return {"domain": "package", "action": "clean_orphans"}

    # 4. Git
    if "git" in text_clean or "commit" in text_clean or "rebase" in text_clean or "branche" in text_clean:
        if "commit" in text_clean:
            return {"domain": "git", "action": "commit", "message": text}
        if "rebase" in text_clean:
            return {"domain": "git", "action": "rebase"}
        if any(k in text_clean for k in ["statut", "status", "etat", "état"]):
            return {"domain": "git", "action": "status"}

    # 5. Audit de Sécurité / DevSecOps
    if any(k in text_clean for k in ["audit", "securite", "sécurité", "port", "ports", "devsecops", "vulnerabilite", "vulnérabilité"]):
        return {"domain": "security", "action": "audit"}

    return None


@kit.action("devsecops_control")
def devsecops_control(parameters: dict | None = None, player=None, **_kwargs) -> str:
    """
    Point d'entrée principal pour Voice DevSecOps & Maître du Système Linux.
    
    Paramètres :
      domain      : 'docker' | 'systemd' | 'package' | 'git' | 'security'
      action      : action spécifique au domaine
      target      : cible (nom de conteneur, service, paquet, branche)
      message     : message de commit ou description
      access_logs : bool (filtrage des logs d'accès)
      error_logs  : bool (filtrage des logs d'erreur)
      description : phrase en langage naturel
    """
    params = parameters or {}
    description = (params.get("description") or "").strip()

    domain = (params.get("domain") or "").strip().lower()
    action = (params.get("action") or "").strip().lower()

    if description and not domain:
        intent = parse_devsecops_intent(description)
        if intent:
            domain = intent.get("domain", "")
            action = intent.get("action", "")
            if "service" in intent:
                params["target"] = intent["service"]
            if "message" in intent:
                params["message"] = intent["message"]
            if "access_logs" in intent:
                params["access_logs"] = intent["access_logs"]

    # 1. Domaine DOCKER
    if domain == "docker":
        if action in ("restart_stack", "restart", "relance"):
            return DockerManager.restart_stack(project_dir=params.get("cwd"), service=params.get("target"))
        if action in ("purge_dead", "prune", "purge", "clean"):
            return DockerManager.purge_dead_containers(prune_images=True)
        if action in ("logs", "log"):
            return DockerManager.get_logs(
                target=params.get("target"),
                tail=int(params.get("tail") or 40),
                access_logs=bool(params.get("access_logs", False)),
                error_logs=bool(params.get("error_logs", False)),
            )
        if action in ("list", "ps", "status"):
            return DockerManager.list_containers(show_all=True)
        return DockerManager.list_containers()

    # 2. Domaine SYSTEMD
    if domain == "systemd":
        target = params.get("target") or "nginx"
        user_mode = bool(params.get("user", False))
        if action in ("list_failed", "failed"):
            return SystemdMaster.list_failed()
        if action == "diagnose":
            return SystemdMaster.diagnose_service(target, user_mode=user_mode)
        if action in ("status", "restart", "start", "stop", "enable", "disable", "reload"):
            return SystemdMaster.service_action(action, target, user_mode=user_mode)
        return SystemdMaster.service_action("status", target, user_mode=user_mode)

    # 3. Domaine PAQUETS
    if domain == "package":
        if action in ("check_updates", "updates", "upgrade"):
            return PackageManager.check_updates()
        if action in ("search", "find"):
            return PackageManager.search_package(params.get("target") or "")
        if action in ("clean_orphans", "orphans", "clean"):
            return PackageManager.clean_orphans()
        return PackageManager.check_updates()

    # 4. Domaine GIT
    if domain == "git":
        if action in ("commit", "smart_commit"):
            return GitMaster.smart_commit(user_message=params.get("message") or description)
        if action in ("status", "summary"):
            return GitMaster.get_status_summary(repo_path=params.get("cwd"))
        if action in ("rebase", "rebase_branch"):
            return GitMaster.rebase_branch(target=params.get("target") or "main", repo_path=params.get("cwd"))
        return GitMaster.get_status_summary()

    # 5. Domaine SÉCURITÉ
    if domain == "security" or action in ("audit", "scan"):
        return SecurityAuditor.audit_system_security()

    if description:
        return f"Commande DevSecOps reçue : '{description}'. Spécifiez le domaine (docker, systemd, package, git, security)."

    return "Aucune action DevSecOps spécifiée."


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        print(devsecops_control({"description": " ".join(sys.argv[1:])}))
    else:
        print("DevSecOps Controller prêt.")
