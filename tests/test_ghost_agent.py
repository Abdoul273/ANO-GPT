"""Isolation et contrat du processus Agent Fantôme."""

from core import ghost_agent


class _FakeProcess:
    def __init__(self, command, **kwargs):
        self.command = command
        self.kwargs = kwargs
        self.pid = 12345
        kwargs["stdout"].write("Audit terminé : 12 tests passent.\n")
        kwargs["stdout"].flush()

    def poll(self):
        return 0


def test_agent_fantome_herite_mcp_sans_parler_et_ecrit_un_rapport(tmp_path, monkeypatch):
    captured = {}

    def fake_popen(command, **kwargs):
        process = _FakeProcess(command, **kwargs)
        captured["process"] = process
        return process

    monkeypatch.setattr(ghost_agent.agent_brain, "agent_binary", lambda: "/faux/agy")
    monkeypatch.setattr(ghost_agent.agent_brain, "_config", lambda: {})
    monkeypatch.setattr(ghost_agent.subprocess, "Popen", fake_popen)
    report = tmp_path / "rapport.md"

    result = ghost_agent.run_mission(
        "Analyse le projet.", tmp_path, report, timeout_seconds=60
    )

    process = captured["process"]
    prompt = process.command[process.command.index("-p") + 1]
    assert "outils MCP" in prompt
    assert "ne devine jamais" in prompt
    assert "dépôt canonique" in prompt
    assert "n'appelle pas l'outil speak" in prompt
    assert process.command[process.command.index("--mode") + 1] == "accept-edits"
    assert process.kwargs["env"][ghost_agent.GHOST_MODE_ENV] == "1"
    assert process.kwargs["env"][ghost_agent.agent_brain.LOOP_GUARD_ENV] == "1"
    assert result.status == "completed"
    assert "12 tests passent" in result.summary
    assert report.is_file()
    assert "Analyse le projet" in report.read_text(encoding="utf-8")


def test_agent_fantome_verifie_les_fichiers_crees_modifies_et_supprimes(tmp_path, monkeypatch):
    existing = tmp_path / "existant.py"
    deleted = tmp_path / "ancien.txt"
    existing.write_text("avant", encoding="utf-8")
    deleted.write_text("à supprimer", encoding="utf-8")

    class EditingProcess(_FakeProcess):
        def __init__(self, command, **kwargs):
            super().__init__(command, **kwargs)
            existing.write_text("après avec une taille différente", encoding="utf-8")
            deleted.unlink()
            (tmp_path / "nouveau.py").write_text("print('créé')", encoding="utf-8")
            generated = tmp_path / "node_modules"
            generated.mkdir()
            (generated / "cache.js").write_text("ignoré", encoding="utf-8")

    monkeypatch.setattr(ghost_agent.agent_brain, "agent_binary", lambda: "/faux/agy")
    monkeypatch.setattr(ghost_agent.agent_brain, "_config", lambda: {})
    monkeypatch.setattr(
        ghost_agent.subprocess, "Popen",
        lambda command, **kwargs: EditingProcess(command, **kwargs),
    )

    report = tmp_path.parent / "rapport-changements.md"
    result = ghost_agent.run_mission("Modifie le projet.", tmp_path, report)

    assert "créé: nouveau.py" in result.changed_files
    assert "modifié: existant.py" in result.changed_files
    assert "supprimé: ancien.txt" in result.changed_files
    assert all("node_modules" not in item for item in result.changed_files)
    content = report.read_text(encoding="utf-8")
    assert "Fichiers détectés par ANO-GPT" in content
    assert "nouveau.py" in content


def test_journal_kitty_est_optionnel_et_ne_porte_pas_le_processus_agy(tmp_path, monkeypatch):
    commands = []

    def fake_popen(command, **kwargs):
        commands.append((command, kwargs))
        if command[0] == "/faux/agy":
            return _FakeProcess(command, **kwargs)

        class KittyProcess:
            pid = 54321

        return KittyProcess()

    monkeypatch.setattr(ghost_agent.agent_brain, "agent_binary", lambda: "/faux/agy")
    monkeypatch.setattr(ghost_agent.agent_brain, "_config", lambda: {})
    monkeypatch.setattr(ghost_agent.shutil, "which", lambda name: "/usr/bin/kitty")
    monkeypatch.setattr(ghost_agent.subprocess, "Popen", fake_popen)

    ghost_agent.run_mission(
        "Analyse.", tmp_path, tmp_path / "rapport.md", show_terminal=True,
    )

    assert commands[0][0][0] == "/faux/agy"
    kitty = commands[1][0]
    assert kitty[0] == "kitty"
    assert "--pid=12345" in kitty
    assert commands[1][1]["start_new_session"] is True


def test_journal_en_cours_est_transmis_a_linterface(tmp_path, monkeypatch):
    class ProgressProcess(_FakeProcess):
        def __init__(self, command, **kwargs):
            super().__init__(command, **kwargs)
            self._polls = 0

        def poll(self):
            self._polls += 1
            return None if self._polls == 1 else 0

    monkeypatch.setattr(ghost_agent.agent_brain, "agent_binary", lambda: "/faux/agy")
    monkeypatch.setattr(ghost_agent.agent_brain, "_config", lambda: {})
    monkeypatch.setattr(
        ghost_agent.subprocess, "Popen",
        lambda command, **kwargs: ProgressProcess(command, **kwargs),
    )
    updates = []

    ghost_agent.run_mission(
        "Analyse.", tmp_path, tmp_path / "rapport.md", progress=updates.append,
    )

    assert updates
    assert "Audit terminé" in updates[-1]
