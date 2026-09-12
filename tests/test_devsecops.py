"""Tests unitaires et d'intégration pour actions/devsecops.py."""

from pathlib import Path
import pytest

from actions.devsecops import (
    DockerManager,
    SystemdMaster,
    PackageManager,
    GitMaster,
    SecurityAuditor,
    parse_devsecops_intent,
    devsecops_control,
)


# ════════════════════════════════════════════════════════════════════════════
# 1. Parsing vocal local
# ════════════════════════════════════════════════════════════════════════════

def test_parse_devsecops_phrase_du_catalogue():
    """Vérifie la phrase exacte de la roadmap : « Relance la stack Docker, purge les conteneurs morts et montre les logs d'accès. »"""
    # Test composante par composante et globale
    p1 = parse_devsecops_intent("Relance la stack Docker")
    assert p1 is not None
    assert p1["domain"] == "docker"
    assert p1["action"] == "restart_stack"

    p2 = parse_devsecops_intent("purge les conteneurs morts et les images inutilisées")
    assert p2 is not None
    assert p2["domain"] == "docker"
    assert p2["action"] == "purge_dead"

    p3 = parse_devsecops_intent("montre les logs d'accès docker")
    assert p3 is not None
    assert p3["domain"] == "docker"
    assert p3["action"] == "logs"
    assert p3.get("access_logs") is True


def test_parse_systemd_intents():
    p_status = parse_devsecops_intent("quel est l'état du service nginx ?")
    assert p_status is not None
    assert p_status["domain"] == "systemd"
    assert p_status["action"] == "status"
    assert p_status["service"] == "nginx"

    p_restart = parse_devsecops_intent("relance le service ollama")
    assert p_restart is not None
    assert p_restart["domain"] == "systemd"
    assert p_restart["action"] == "restart"
    assert p_restart["service"] == "ollama"

    p_failed = parse_devsecops_intent("quels sont les services en échec ?")
    assert p_failed is not None
    assert p_failed["domain"] == "systemd"
    assert p_failed["action"] == "list_failed"


def test_parse_package_intents():
    p_up = parse_devsecops_intent("vérifie les mises à jour des paquets")
    assert p_up is not None
    assert p_up["domain"] == "package"
    assert p_up["action"] == "check_updates"

    p_orphans = parse_devsecops_intent("nettoie les paquets orphelins")
    assert p_orphans is not None
    assert p_orphans["domain"] == "package"
    assert p_orphans["action"] == "clean_orphans"


def test_parse_git_intents():
    p_commit = parse_devsecops_intent("fais un commit propre en disant qu'on a réparé l'authentification")
    assert p_commit is not None
    assert p_commit["domain"] == "git"
    assert p_commit["action"] == "commit"

    p_rebase = parse_devsecops_intent("rebase sur main")
    assert p_rebase is not None
    assert p_rebase["domain"] == "git"
    assert p_rebase["action"] == "rebase"


def test_parse_security_intent():
    p_sec = parse_devsecops_intent("fais un audit de sécurité des ports ouverts")
    assert p_sec is not None
    assert p_sec["domain"] == "security"
    assert p_sec["action"] == "audit"


# ════════════════════════════════════════════════════════════════════════════
# 2. Scanner de secrets Git (DevSecOps Guard)
# ════════════════════════════════════════════════════════════════════════════

def test_git_scanner_bloque_secrets_critiques():
    """Vérifie que les clés API et tokens sont détectés avant commit."""
    # Clé Gemini
    diff_gemini = """
+GEMINI_API_KEY = "AIzaSyD-1234567890abcdefghijklmnopqrstuv"
-old_key = None
"""
    secrets = GitMaster.scan_diff_for_secrets(diff_gemini)
    assert len(secrets) >= 1
    assert "Gemini" in secrets[0][0]

    # Clé OpenAI
    diff_openai = """
+sk-proj-abcdefghijklmnopqrstuvwxyz1234567890123456789012
"""
    secrets_oa = GitMaster.scan_diff_for_secrets(diff_openai)
    assert len(secrets_oa) >= 1
    assert "OpenAI" in secrets_oa[0][0]

    # Clé SSH privée
    diff_ssh = """
+-----BEGIN RSA PRIVATE KEY-----
+MIIEowIBAAKCAQEA0Y...
"""
    secrets_ssh = GitMaster.scan_diff_for_secrets(diff_ssh)
    assert len(secrets_ssh) >= 1
    assert "SSH" in secrets_ssh[0][0]

    # Code sain : aucun secret
    diff_clean = """
+def authenticate(user, password_hash):
+    return verify_hash(user, password_hash)
"""
    assert GitMaster.scan_diff_for_secrets(diff_clean) == []


def test_git_conventional_commit_formatting():
    msg_fix = GitMaster._generate_conventional_message(
        "corrige le bug de synchronisation audio",
        "+fixed audio sync",
        Path.cwd()
    )
    assert msg_fix.startswith("fix:")

    msg_feat = GitMaster._generate_conventional_message(
        "ajoute le support de Hyprland 0.56",
        "+added hyprland support",
        Path.cwd()
    )
    assert msg_feat.startswith("feat:")

    msg_already_conv = GitMaster._generate_conventional_message(
        "refactor(core): streamline action runtime",
        "+changes",
        Path.cwd()
    )
    assert msg_already_conv == "refactor(core): streamline action runtime"


# ════════════════════════════════════════════════════════════════════════════
# 3. Docker Manager & Filtrage Logs
# ════════════════════════════════════════════════════════════════════════════

def test_docker_logs_access_filtering(monkeypatch):
    sample_logs = """
2026-08-30 10:00:00 [info] Starting server on port 8080
127.0.0.1 - - [30/Aug/2026:10:01:23] "GET /api/v1/status HTTP/1.1" 200 456
127.0.0.1 - - [30/Aug/2026:10:01:25] "POST /api/v1/auth HTTP/1.1" 201 1024
2026-08-30 10:02:00 [debug] Background worker tick
192.168.1.50 - - [30/Aug/2026:10:03:00] "GET /docs HTTP/1.1" 404 128
"""
    monkeypatch.setattr("actions.devsecops._run_cmd", lambda cmd, **k: (0, sample_logs, ""))
    monkeypatch.setattr(DockerManager, "is_available", lambda: True)

    out = DockerManager.get_logs("my-web-app", access_logs=True, tail=10)
    assert "GET /api/v1/status" in out
    assert "POST /api/v1/auth" in out
    assert "GET /docs" in out
    assert "Background worker tick" not in out


def test_docker_purge_dead_containers_formatting(monkeypatch):
    purge_output = """
Deleted Containers:
c123456789ab
d987654321fe
Total reclaimed space: 154.2MB
"""
    monkeypatch.setattr("actions.devsecops._run_cmd", lambda cmd, **k: (0, purge_output, ""))
    monkeypatch.setattr(DockerManager, "is_available", lambda: True)

    res = DockerManager.purge_dead_containers(prune_images=False)
    assert "Conteneurs morts purgés : 2" in res
    assert "154.2MB libérés" in res


# ════════════════════════════════════════════════════════════════════════════
# 4. Systemd Manager & Diagnostique
# ════════════════════════════════════════════════════════════════════════════

def test_systemd_diagnose_service(monkeypatch):
    journal_output = """
Aug 30 10:00:00 myhost systemd[1]: Starting Nginx Web Server...
Aug 30 10:00:01 myhost nginx[1234]: nginx: [emerg] bind() to 0.0.0.0:80 failed (98: Address already in use)
Aug 30 10:00:02 myhost systemd[1]: nginx.service: Main process exited, code=exited, status=1/FAILURE
Aug 30 10:00:02 myhost systemd[1]: nginx.service: Failed with result 'exit-code'.
"""
    monkeypatch.setattr("actions.devsecops._run_cmd", lambda cmd, **k: (0, journal_output, ""))
    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/journalctl" if cmd == "journalctl" else None)

    diag = SystemdMaster.diagnose_service("nginx")
    assert "Diagnostic Systemd : nginx.service" in diag
    assert "Address already in use" in diag or "Failed with result" in diag


# ════════════════════════════════════════════════════════════════════════════
# 5. Contrôleur DevSecOps unifié
# ════════════════════════════════════════════════════════════════════════════

def test_devsecops_control_empty_args():
    res = devsecops_control({})
    assert isinstance(res, str)
    assert res  # Ne crash jamais sur dictionnaire vide


def test_devsecops_control_natural_language_dispatch(monkeypatch):
    monkeypatch.setattr(DockerManager, "purge_dead_containers", lambda **k: "2 conteneurs purgés (50MB)")
    res = devsecops_control({"description": "purge les conteneurs morts"})
    assert "2 conteneurs purgés" in res
