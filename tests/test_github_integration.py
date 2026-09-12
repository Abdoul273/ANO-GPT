"""Contrats de sécurité du contrôle GitHub."""
from __future__ import annotations

import subprocess

from actions import github


def _git(path, *args):
    subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True)


def test_commit_est_bloque_par_le_scan_devsecops(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    _git(project, "init")
    _git(project, "config", "user.name", "Test")
    _git(project, "config", "user.email", "test@example.invalid")
    (project / "settings.py").write_text('api_key = "AIza' + "a" * 35 + '"\n', encoding="utf-8")

    result = github.github_control({"action": "commit", "project": str(project)})

    assert "COMMIT BLOQUÉ PAR SÉCURITÉ" in result


def test_create_repo_sans_projet_ne_touche_pas_au_disque(monkeypatch):
    class Service:
        def api(self, method, path, payload=None):
            assert (method, path, payload["name"]) == ("POST", "/user/repos", "remote-only")
            return {"full_name": "anonymous/remote-only"}

    monkeypatch.setattr(github, "get_github_service", lambda: Service())

    result = github.github_control({"action": "create_repo", "repo_name": "remote-only", "private": True})

    assert result == "Dépôt GitHub créé : anonymous/remote-only."


def test_connexion_affiche_le_code_device_dans_le_hud(monkeypatch):
    class Service:
        def connect(self, *, on_progress, on_device_code):
            on_device_code("ABCD-EFGH", "https://github.com/login/device")
            return {"login": "Abdoul273"}

    class UI:
        def __init__(self):
            self.card = None
            self.dismissed = None

        def show_card(self, *args):
            self.card = args

        def dismiss_cards(self, *args):
            self.dismissed = args

        def write_log(self, _message):
            pass

    monkeypatch.setattr(github, "get_github_service", lambda: Service())
    ui = UI()

    result = github.github_control({"action": "connect"}, ui=ui)

    assert "Abdoul273" in result
    assert ui.card[0:2] == ("github-oauth", "Autorisation GitHub")
    assert "ABCD-EFGH" in ui.card[2]
    assert ui.dismissed == ("github-oauth", "Autorisation GitHub")


def test_push_refuse_propose_une_resolution_sans_pull_force(monkeypatch, tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    monkeypatch.setattr(github, "_remote", lambda _: "https://github.com/a/demo.git")
    monkeypatch.setattr(github, "_scan_outgoing", lambda _: None)
    monkeypatch.setattr(github, "_askpass_env", lambda _: ({}, tmp_path / "askpass"))
    monkeypatch.setattr(github, "_run", lambda *a, **k: (1, "", "rejected non-fast-forward fetch first"))
    monkeypatch.setattr(github, "get_github_service", lambda: type("S", (), {"token": lambda self: "x" * 30})())

    result = github._push(project, "main")

    assert "Push refusé" in result and "Aucun pull forcé" in result
