from actions import music
from core import local_video


class FakeUI:
    def __init__(self):
        self.results = []
        self.played = []
        self.controls = []

    def write_log(self, _text):
        pass

    def show_video_results(self, query, results):
        self.results.append((query, results))

    def play_video(self, video, playlist):
        self.played.append((video, playlist))

    def control_video(self, action, value=None):
        self.controls.append((action, value))

    def video_status(self):
        return "Vidéo locale active."


def _prepared(path="/videos/vacances.mp4", title="Vacances"):
    return {
        "path": path, "title": title, "folder": "Vidéos",
        "source": "local", "kind": "video", "duration": "1:30",
    }


def test_local_video_search_opens_native_card_grid(monkeypatch):
    raw = {"path": "/videos/vacances.mp4", "title": "Vacances", "score": .8}
    monkeypatch.setattr(
        music, "search_local",
        lambda query, media_kind: [raw] if media_kind == "video" else [],
    )
    monkeypatch.setattr(local_video, "prepare_local_videos", lambda items, limit=12: [_prepared()])
    ui = FakeUI()
    memory = {}

    response = music.music_control(
        {"action": "search", "query": "vacances", "kind": "video"},
        player=ui, session_memory=memory,
    )

    assert ui.results[0][0] == "vacances"
    assert ui.results[0][1][0]["path"].endswith("vacances.mp4")
    assert memory["music_local_results"][0]["kind"] == "video"
    assert "Vacances" in response


def test_local_video_selection_uses_integrated_player(monkeypatch):
    video = _prepared()
    monkeypatch.setattr(local_video, "prepare_local_video", lambda item: dict(video))
    monkeypatch.setattr(local_video, "prepare_local_videos", lambda items, limit=12: [dict(video)])
    ui = FakeUI()
    memory = {"music_local_results": [video]}

    response = music.music_control(
        {"action": "select", "index": 1, "kind": "video"},
        player=ui, session_memory=memory,
    )

    assert ui.played[0][0]["title"] == "Vacances"
    assert memory["music_local_video_current"]["path"].endswith("vacances.mp4")
    assert memory["music_local_results"][0]["title"] == "Vacances"
    assert "lecteur vidéo intégré" in response


def test_local_video_voice_controls_stay_in_native_player():
    ui = FakeUI()
    memory = {"music_local_video_current": _prepared()}

    response = music.music_control(
        {"action": "pause", "kind": "video"}, player=ui, session_memory=memory
    )

    assert ui.controls == [("pause", None)]
    assert response == "Vidéo locale mise en pause."


def test_stop_video_returns_to_results_without_forgetting_them():
    ui = FakeUI()
    video = _prepared()
    memory = {
        "music_local_video_current": video,
        "music_local_results": [video],
    }

    response = music.music_control(
        {"action": "stop", "kind": "video"}, player=ui, session_memory=memory
    )

    assert ui.controls == [("stop", None)]
    assert memory["music_local_video_current"] is None
    assert memory["music_local_results"] == [video]
    assert "sélection reste affichée" in response
