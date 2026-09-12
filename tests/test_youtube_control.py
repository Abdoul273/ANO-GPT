import json
import subprocess
from types import SimpleNamespace

import pytest

from actions import youtube_video as youtube_action
from core import youtube_service
from core.youtube_service import YouTubeResult


def _result(video_id: str, title: str) -> YouTubeResult:
    return YouTubeResult(
        id=video_id,
        title=title,
        url=f"https://www.youtube.com/watch?v={video_id}",
        channel="Chaîne test",
        duration_seconds=125,
        views=1200,
    )


def test_structured_search_parses_and_deduplicates(monkeypatch):
    monkeypatch.setattr(youtube_service.shutil, "which", lambda _: "/usr/bin/yt-dlp")
    rows = [
        {"id": "abc", "title": "Vidéo A", "channel": "Canal A", "duration": 61, "view_count": 10},
        {"id": "abc", "title": "Doublon"},
        {"id": "def", "title": "Direct", "uploader": "Canal B", "live_status": "is_live"},
    ]

    def runner(command, **kwargs):
        assert command[1] == "ytsearch4:python robuste"
        assert kwargs["timeout"] == 35
        return SimpleNamespace(
            returncode=0,
            stdout="\n".join(json.dumps(row) for row in rows),
            stderr="",
        )

    results = youtube_service.search_youtube("python robuste", limit=4, runner=runner)

    assert [r.id for r in results] == ["abc", "def"]
    assert results[0].duration == "1:01"
    assert results[1].duration == "EN DIRECT"
    assert results[1].channel == "Canal B"


def test_structured_search_reports_timeout(monkeypatch):
    monkeypatch.setattr(youtube_service.shutil, "which", lambda _: "/usr/bin/yt-dlp")

    def runner(*args, **kwargs):
        raise subprocess.TimeoutExpired("yt-dlp", 35)

    with pytest.raises(youtube_service.YouTubeUnavailable, match="expiré"):
        youtube_service.search_youtube("test", runner=runner)


def test_result_resolution_supports_number_id_and_title():
    results = [_result("abc", "Apprendre Python").to_dict(), _result("def", "API moderne").to_dict()]

    assert youtube_service.resolve_result(results, 2)["id"] == "def"
    assert youtube_service.resolve_result(results, "abc")["title"] == "Apprendre Python"
    assert youtube_service.resolve_result(results, "moderne")["id"] == "def"
    assert youtube_service.resolve_result(results, 9) is None


def test_natural_search_never_starts_playback():
    parsed = youtube_action._parse_youtube_command_locally(
        "recherche une vidéo python sur youtube"
    )
    assert parsed == {"action": "search", "query": "python"}


def test_search_remembers_results_without_opening(monkeypatch):
    monkeypatch.setattr(
        youtube_action,
        "search_youtube_structured",
        lambda query, limit: [_result("abc", "Première"), _result("def", "Deuxième")],
    )
    opened = []
    monkeypatch.setattr(youtube_action, "_open_url", lambda url: opened.append(url) or True)
    memory = {}

    response = youtube_action.youtube_video(
        {"action": "search", "query": "test", "limit": 2},
        session_memory=memory,
    )

    assert "1. Première" in response
    assert "2. Deuxième" in response
    assert opened == []
    assert [item["id"] for item in memory["youtube_results"]] == ["abc", "def"]


def test_search_populates_native_video_gallery(monkeypatch):
    monkeypatch.setattr(
        youtube_action,
        "search_youtube_structured",
        lambda query, limit: [_result("abcdefghijk", "Première"), _result("defghijklmn", "Deuxième")],
    )
    monkeypatch.setattr(youtube_action, "_attach_thumbnails", lambda results: results)

    class Player:
        def __init__(self):
            self.calls = []
        def write_log(self, _text):
            pass
        def show_video_results(self, query, results):
            self.calls.append((query, results))

    player = Player()
    memory = {}
    response = youtube_action.youtube_video(
        {"action": "search", "query": "robotique"},
        player=player, session_memory=memory,
    )

    assert "Première" in response
    assert player.calls[0][0] == "robotique"
    assert len(player.calls[0][1]) == 2
    assert memory["youtube_last_query"] == "robotique"


def test_select_opens_remembered_result(monkeypatch):
    memory = {
        "youtube_results": [
            _result("abc", "Première").to_dict(),
            _result("def", "Deuxième").to_dict(),
        ]
    }
    opened = []
    monkeypatch.setattr(youtube_action, "_open_url", lambda url: opened.append(url) or True)

    response = youtube_action.youtube_video(
        {"action": "select", "index": 2}, session_memory=memory
    )

    assert opened == ["https://www.youtube.com/watch?v=def"]
    assert memory["youtube_current"]["title"] == "Deuxième"
    assert "Deuxième" in response


def test_play_searches_then_opens_first_result(monkeypatch):
    monkeypatch.setattr(
        youtube_action,
        "search_youtube_structured",
        lambda query, limit: [_result("abc", "Premier choix")],
    )
    opened = []
    monkeypatch.setattr(youtube_action, "_open_url", lambda url: opened.append(url) or True)

    response = youtube_action.youtube_video(
        {"action": "play", "query": "cours python"}, session_memory={}
    )

    assert opened == ["https://www.youtube.com/watch?v=abc"]
    assert "Premier choix" in response


def test_selection_and_replacement_use_integrated_player(monkeypatch):
    playlist = [
        _result("abcdefghijk", "Première").to_dict(),
        _result("defghijklmn", "Deuxième").to_dict(),
    ]
    memory = {"youtube_results": playlist}
    opened = []
    monkeypatch.setattr(youtube_action, "_open_url", lambda url: opened.append(url) or True)

    class Player:
        def __init__(self):
            self.played = []
        def write_log(self, _text):
            pass
        def play_video(self, video, videos):
            self.played.append((video, videos))

    player = Player()
    response = youtube_action.youtube_video(
        {"action": "select", "index": 2}, player=player, session_memory=memory
    )

    assert opened == []
    assert player.played[-1][0]["title"] == "Deuxième"
    assert player.played[-1][1] == playlist
    assert "lecteur vidéo intégré" in response


def test_youtube_controls_delegate_to_media_controller(monkeypatch):
    calls = []
    monkeypatch.setattr(
        youtube_action,
        "media_control",
        lambda parameters: calls.append(parameters) or "contrôle effectué",
    )

    response = youtube_action.youtube_video({"action": "back", "seconds": 25})

    assert response == "contrôle effectué"
    assert calls == [{"action": "youtube_seek", "value": -25}]


def test_youtube_controls_target_integrated_player_when_available(monkeypatch):
    media_calls = []
    monkeypatch.setattr(
        youtube_action, "media_control",
        lambda parameters: media_calls.append(parameters) or "externe",
    )

    class Player:
        def __init__(self):
            self.calls = []
        def write_log(self, _text):
            pass
        def control_video(self, action, value):
            self.calls.append((action, value))

    player = Player()
    response = youtube_action.youtube_video(
        {"action": "back", "seconds": 25}, player=player
    )

    assert player.calls == [("back", -25)]
    assert media_calls == []
    assert "lecteur vidéo intégré" in response


def test_stop_youtube_returns_to_results_in_native_player():
    class Player:
        def __init__(self):
            self.calls = []
        def write_log(self, _text):
            pass
        def control_video(self, action, value):
            self.calls.append((action, value))

    player = Player()
    response = youtube_action.youtube_video({"action": "stop"}, player=player)

    assert player.calls == [("stop", None)]
    assert "résultats restent affichés" in response


def test_search_never_opens_browser_when_structured_backend_times_out(monkeypatch):
    monkeypatch.setattr(
        youtube_action,
        "search_youtube_structured",
        lambda query, limit: (_ for _ in ()).throw(
            youtube_service.YouTubeUnavailable("délai dépassé")
        ),
    )
    opened = []
    monkeypatch.setattr(youtube_action, "_open_url", lambda url: opened.append(url) or True)
    memory = {}

    response = youtube_action.youtube_video(
        {"action": "search", "query": "robotique"}, session_memory=memory
    )

    assert "aucun navigateur externe" in response.lower()
    assert opened == []
    assert memory["youtube_last_query"] == "robotique"


def test_search_failure_never_opens_browser_when_native_ui_exists(monkeypatch):
    monkeypatch.setattr(
        youtube_action,
        "search_youtube_structured",
        lambda query, limit: (_ for _ in ()).throw(
            youtube_service.YouTubeUnavailable("délai dépassé")
        ),
    )
    opened = []
    monkeypatch.setattr(youtube_action, "_open_url", lambda url: opened.append(url) or True)

    class Player:
        def write_log(self, _text):
            pass
        def show_video_results(self, _query, _results):
            pass

    response = youtube_action.youtube_video(
        {"action": "search", "query": "robotique"},
        player=Player(), session_memory={},
    )

    assert opened == []
    assert "aucun navigateur externe" in response.lower()


def test_local_parser_extracts_numbered_selection():
    from actions.youtube_video import _parse_youtube_command_locally

    cmd1 = _parse_youtube_command_locally("ouvre le résultat YouTube numéro 1")
    assert cmd1 is not None
    assert cmd1.get("action") == "select"
    assert cmd1.get("index") in (1, "1")

    cmd2 = _parse_youtube_command_locally("lis la deuxième vidéo")
    assert cmd2 is not None
    assert cmd2.get("action") == "select"
    assert cmd2.get("index") in (2, "2", "deuxième")

    cmd3 = _parse_youtube_command_locally("lance la vidéo 3")
    assert cmd3 is not None
    assert cmd3.get("action") == "select"
    assert cmd3.get("index") in (3, "3")


def test_play_resolves_remembered_number_or_ordinal(monkeypatch):
    playlist = [
        _result("vid1", "Vidéo Une").to_dict(),
        _result("vid2", "Vidéo Deux").to_dict(),
    ]
    memory = {"youtube_results": playlist}

    class MockPlayer:
        def __init__(self):
            self.played = []
        def play_video(self, video, videos):
            self.played.append((video, videos))

    player = MockPlayer()
    response = youtube_action.youtube_video(
        {"action": "play", "query": "2"},
        player=player, session_memory=memory,
    )
    assert "Vidéo Deux" in response
    assert len(player.played) == 1
    assert player.played[0][0]["id"] == "vid2"
