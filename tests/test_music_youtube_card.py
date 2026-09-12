"""La bascule musique → YouTube conserve le lecteur vidéo ANO-GPT."""

from actions import music


def test_musique_youtube_explicite_lance_dans_le_lecteur_video(monkeypatch):
    import actions.youtube_video as youtube

    calls = []
    monkeypatch.setattr(
        youtube, "youtube_video",
        lambda parameters, **kwargs: calls.append((parameters, kwargs)) or "lecture intégrée",
    )

    result = music.music_control(
        {"action": "play", "query": "Daft Punk One More Time", "source": "youtube"},
        player=object(), session_memory={},
    )

    assert result == "lecture intégrée"
    assert calls[0][0]["action"] == "play"
    assert calls[0][0]["limit"] == 8
    assert calls[0][1]["player"] is not None


def test_musique_absente_demande_la_source_avant_toute_recherche(monkeypatch):
    """Rien en local : on demande, on ne lance pas une recherche en ligne.

    Partir seul sur YouTube consommait du réseau et ouvrait parfois autre
    chose que ce que l'utilisateur voulait. La question coûte une phrase.
    """
    import actions.youtube_video as youtube

    calls = []
    monkeypatch.setattr(music, "_search_local_for_kind", lambda *_args: [])
    monkeypatch.setattr(
        youtube, "youtube_video",
        lambda parameters, **kwargs: calls.append((parameters, kwargs)) or "choisis une vidéo",
    )

    session_memory: dict = {}
    result = music.music_control(
        {"action": "play", "query": "titre absent"},
        player=object(), session_memory=session_memory,
    )

    assert "bibliothèque locale" in result
    assert "Spotify" in result and "YouTube" in result
    assert calls == []
    assert session_memory[music._PENDING_SOURCE_KEY]["query"] == "titre absent"


def test_musique_au_hasard_lance_un_vrai_morceau_rap_sans_selection(monkeypatch):
    calls = []
    monkeypatch.setattr(music, "search_youtube", lambda *_args, **_kwargs: [{
        "title": "Artiste — Le Banger (Official Audio)",
        "url": "https://youtube.example/watch?v=track",
        "duration": "3:12",
        "uploader": "Artiste",
        "thumbnail_url": "",
    }])
    monkeypatch.setattr(music, "resolve_stream_url", lambda _url: "https://stream.example/audio")
    monkeypatch.setattr(
        music, "_play_result",
        lambda *args, **kwargs: calls.append(args) or "lecture directe",
    )

    result = music.music_control(
        {"action": "play", "query": "lance une musique au hasard"},
        session_memory={},
    )

    assert result == "lecture directe"
    assert calls[0][0] == "https://stream.example/audio"
    assert calls[0][2] == "Artiste — Le Banger (Official Audio)"
