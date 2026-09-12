from actions import desktop


def test_organize_preview_does_not_move_files(monkeypatch, tmp_path):
    monkeypatch.setattr(desktop, "_get_desktop", lambda: tmp_path)
    source = tmp_path / "rapport.pdf"
    source.write_text("contenu")

    result = desktop.organize_desktop(dry_run=True)

    assert source.exists()
    assert not (tmp_path / "Documents").exists()
    assert "à déplacer" in result


def test_organize_never_overwrites_an_existing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(desktop, "_get_desktop", lambda: tmp_path)
    (tmp_path / "rapport.pdf").write_text("nouveau")
    destination = tmp_path / "Documents"
    destination.mkdir()
    (destination / "rapport.pdf").write_text("ancien")

    desktop.organize_desktop()

    assert (tmp_path / "rapport.pdf").read_text() == "nouveau"
    assert (destination / "rapport.pdf").read_text() == "ancien"


def test_clean_then_restore_is_reversible_without_overwrite(monkeypatch, tmp_path):
    monkeypatch.setattr(desktop, "_get_desktop", lambda: tmp_path)
    (tmp_path / "note.txt").write_text("à archiver")

    cleaned = desktop.clean_desktop()
    assert "1 fichier" in cleaned
    archive = next(tmp_path.glob("Archive Bureau *"))
    assert (archive / "note.txt").exists()

    restored = desktop.restore_desktop_archive()
    assert "1 fichier" in restored
    assert (tmp_path / "note.txt").read_text() == "à archiver"


def test_unrecognized_task_never_runs_generated_code(monkeypatch):
    monkeypatch.setattr(
        desktop, "_ask_gemini_for_desktop_action",
        lambda task: (_ for _ in ()).throw(AssertionError("must not be called")),
    )

    result = desktop.desktop_control({"action": "task", "task": "fais n'importe quoi"})

    assert "opération Bureau sûre reconnue" in result


def test_natural_language_preview_is_local_and_safe():
    assert desktop._parse_desktop_command_locally("aperçu organise mon bureau") == {
        "action": "preview"
    }
