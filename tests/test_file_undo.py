from actions import file_controller as files
from core import undo_stack


def setup_function():
    undo_stack.clear()


def test_create_and_write_are_reversible(tmp_path, monkeypatch):
    monkeypatch.setattr(files, "_SAFE_ROOTS", [tmp_path])
    assert "créé" in files.create_file(str(tmp_path), "note.txt", "un")
    assert (tmp_path / "note.txt").read_text() == "un"
    undo_stack.undo_last()
    assert not (tmp_path / "note.txt").exists()

    target = tmp_path / "existant.txt"
    target.write_text("avant")
    files.write_file(str(target), content="après")
    assert target.read_text() == "après"
    undo_stack.undo_last()
    assert target.read_text() == "avant"


def test_move_and_rename_are_reversible(tmp_path, monkeypatch):
    monkeypatch.setattr(files, "_SAFE_ROOTS", [tmp_path])
    source = tmp_path / "source.txt"
    destination = tmp_path / "dest"
    destination.mkdir()
    source.write_text("contenu")
    files.move_file(str(source), destination=str(destination))
    assert (destination / "source.txt").exists()
    undo_stack.undo_last()
    assert source.exists()

    files.rename_file(str(source), new_name="nouveau.txt")
    assert (tmp_path / "nouveau.txt").exists()
    undo_stack.undo_last()
    assert source.exists()


def test_copy_refuses_to_overwrite_existing_destination(tmp_path, monkeypatch):
    monkeypatch.setattr(files, "_SAFE_ROOTS", [tmp_path])
    source = tmp_path / "source.txt"
    destination = tmp_path / "dest.txt"
    source.write_text("source")
    destination.write_text("utilisateur")
    result = files.copy_file(str(source), destination=str(destination))
    assert "refusée" in result
    assert destination.read_text() == "utilisateur"
