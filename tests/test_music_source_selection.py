from actions import music


def test_missing_local_track_requires_a_spotify_or_youtube_choice(monkeypatch):
    monkeypatch.setattr(music, "_search_local_for_kind", lambda *_args: [])
    memory = {}
    result = music.music_control({"action": "play", "query": "un titre absent"}, session_memory=memory)
    assert "Spotify ou YouTube" in result
    assert memory["music_pending_source"]["query"] == "un titre absent"


def test_spotify_choice_reuses_pending_query(monkeypatch):
    played = []
    monkeypatch.setattr(music, "_play_spotify", lambda query, *_args, **_kwargs: played.append(query) or "Spotify")
    memory = {"music_pending_source": {"query": "un titre absent"}}
    assert music.music_control({"action": "play", "query": "Spotify"}, session_memory=memory) == "Spotify"
    assert played == ["un titre absent"]
