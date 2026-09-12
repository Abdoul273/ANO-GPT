from pathlib import Path
from types import SimpleNamespace

from actions import file_controller as files
from actions import smart_search


def test_spaced_existence_question_extracts_only_filename():
    parsed = files._parse_file_command_locally(
        "Est-ce que j'ai un fichier nommé Corin dans mon disque ?"
    )
    assert parsed == {"action": "find", "path": "home", "name": "corin"}


def test_compact_stt_question_is_not_confused_with_disk_usage():
    parsed = files._parse_file_command_locally(
        "Est-cequej'aiunfichiernomméCorindansmondisque ?"
    )
    assert parsed == {"action": "find", "path": "home", "name": "corin"}


def test_file_controller_defaults_find_to_home(monkeypatch):
    calls = []
    monkeypatch.setattr(
        files,
        "find_files",
        lambda **kwargs: calls.append(kwargs) or "ok",
    )
    assert files.file_controller({"action": "find", "name": "Corin"}) == "ok"
    assert calls[0]["path"] == "home"


def test_compact_natural_request_propagates_name_and_home(monkeypatch):
    calls = []
    monkeypatch.setattr(
        files,
        "find_files",
        lambda **kwargs: calls.append(kwargs) or "ok",
    )

    response = files.file_controller({
        "description": "Est-cequej'aiunfichiernomméCorindansmondisque ?"
    })

    assert response == "ok"
    assert calls[0]["name"] == "corin"
    assert calls[0]["path"] == "home"


def test_plocate_fast_path_is_ranked_without_full_walk(tmp_path, monkeypatch):
    candidate = tmp_path / "Coring-notes.txt"
    candidate.touch()
    monkeypatch.setattr(smart_search.shutil, "which", lambda name: "/usr/bin/plocate")
    monkeypatch.setattr(
        smart_search.kit,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout=str(candidate) + "\n", stderr=""
        ),
    )
    monkeypatch.setattr(
        smart_search.os,
        "walk",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("scan lent appelé")),
    )

    rows = smart_search.smart_search_files("Corin", [tmp_path], max_results=5)

    assert rows[0][1] == candidate


def test_close_match_is_not_reported_as_exact(tmp_path, monkeypatch):
    candidate = tmp_path / "scoring.py"
    candidate.touch()
    monkeypatch.setattr(files, "_resolve_path", lambda path: tmp_path)
    monkeypatch.setattr(files, "_is_safe_path", lambda path: True)
    monkeypatch.setattr(
        smart_search,
        "smart_search_files",
        lambda **kwargs: [(0.95, candidate)],
    )

    response = files.find_files(name="Corin", path="home", max_results=5)

    assert "Aucun fichier nommé exactement « Corin »" in response
    assert "scoring.py" in response


def test_natural_video_search_extracts_name_and_media_kind():
    parsed = files._parse_file_command_locally(
        "Trouve une vidéo avec Claude dans son nom"
    )
    assert parsed == {
        "action": "find", "path": "home", "kind": "video", "name": "claude"
    }


def test_video_kind_is_forwarded_to_fast_search(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(files, "_resolve_path", lambda path: tmp_path)
    monkeypatch.setattr(files, "_is_safe_path", lambda path: True)

    def fake_search(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(smart_search, "smart_search_files", fake_search)
    monkeypatch.setattr(
        files, "_fallback_find",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("le scan rglob lent ne doit pas redémarrer")
        ),
    )

    response = files.find_files(name="Claude", path="home", kind="video")

    assert "Aucun Claude trouvé" in response
    assert ".mp4" in captured["extension"]
    assert ".mkv" in captured["extension"]


def test_video_file_search_populates_native_gallery(monkeypatch, tmp_path):
    candidate = tmp_path / "Vacances.mp4"
    candidate.write_bytes(b"video")
    monkeypatch.setattr(files, "_resolve_path", lambda path: tmp_path)
    monkeypatch.setattr(files, "_is_safe_path", lambda path: True)
    monkeypatch.setattr(
        smart_search, "smart_search_files", lambda **kwargs: [(.95, candidate)]
    )
    from core import local_video
    prepared = {
        "path": str(candidate), "title": "Vacances", "folder": tmp_path.name,
        "source": "local", "kind": "video",
    }
    monkeypatch.setattr(local_video, "prepare_local_videos", lambda items, limit=12: [prepared])

    class Player:
        def __init__(self):
            self.calls = []
        def show_video_results(self, query, results):
            self.calls.append((query, results))

    player = Player()
    memory = {}
    response = files.find_files(
        name="Vacances", path="home", kind="video",
        player=player, session_memory=memory,
    )

    assert "Vacances.mp4" in response
    assert player.calls[0][1][0]["kind"] == "video"
    assert memory["music_local_results"][0]["path"] == str(candidate)


def test_last_resort_counts_non_matches_and_stops(monkeypatch, tmp_path):
    monkeypatch.setattr(smart_search.shutil, "which", lambda name: None)
    names = [f"f{i}.txt" for i in range(50)]
    for name in names:
        (tmp_path / name).touch()
    monkeypatch.setattr(
        smart_search.os, "walk",
        lambda *args, **kwargs: [(str(tmp_path), [], names)],
    )
    calls = []
    monkeypatch.setattr(
        smart_search, "match_score",
        lambda *args, **kwargs: calls.append(args) or 0.0,
    )

    rows = smart_search.smart_search_files(
        "introuvable", [tmp_path], max_scan=3, max_duration_s=10
    )

    assert rows == []
    # Deux scores par fichier (stem puis nom complet), donc trois fichiers
    # examinés au total malgré les cinquante candidats.
    assert len(calls) == 6
