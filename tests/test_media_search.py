from pathlib import Path

import actions.music as music
from actions.smart_search import match_score
from core import media_search


def test_voice_typo_finds_real_local_filename(tmp_path):
    intended = tmp_path / "(R)KANDA.BONGOM.mp3"
    distractor = tmp_path / "Banlieuz Art - Andé.mp3"
    intended.touch()
    distractor.touch()

    rows = media_search.rank_media_paths(
        "anda ongo", [distractor, intended], limit=2, enrich_if_below=0
    )

    assert Path(rows[0]["path"]) == intended
    assert rows[0]["score"] >= 0.80


def test_generic_file_search_uses_same_voice_tolerance():
    assert match_score("anda ongo", "(R)KANDA.BONGOM.mp3") >= 0.80


def test_url_encoded_filename_is_decoded():
    assert media_search.readable_media_name(
        "MHD%20-%20AFRO%20TRAP%20Part.7%20(La%20Puissance)%20-%20YouTube"
    ) == "MHD - AFRO TRAP Part.7 La Puissance - YouTube"


def test_metadata_title_artist_and_album_are_searchable(tmp_path, monkeypatch):
    path = tmp_path / "track-00493.mp3"
    path.touch()
    monkeypatch.setattr(
        media_search,
        "media_tags",
        lambda candidate, probe=False: {
            "title": "Mon morceau secret",
            "artist": "Artiste Test",
            "album": "Album Unique",
        },
    )

    by_title = media_search.rank_media_paths("morceau secret", [path])
    by_artist = media_search.rank_media_paths("artiste teste", [path])
    by_album = media_search.rank_media_paths("album unique", [path])

    assert by_title[0]["matched_on"] == "title"
    assert by_artist[0]["matched_on"] == "artist"
    assert by_album[0]["matched_on"] == "album"


def test_confident_voice_match_is_played_directly(monkeypatch):
    hit = {
        "title": "R KANDA BONGOM",
        "artist": "",
        "path": "/music/kanda.mp3",
        "score": 0.84,
    }
    monkeypatch.setattr(music, "search_local", lambda query: [hit])
    played = []
    monkeypatch.setattr(
        music,
        "_play_result",
        lambda *args: played.append(args) or "lecture locale",
    )

    response = music.music_control(
        {"action": "play", "query": "anda ongo", "source": "local"},
        session_memory={},
    )

    assert response == "lecture locale"
    assert played[0][0] == "/music/kanda.mp3"


def test_ambiguous_results_are_remembered_then_selectable(monkeypatch):
    hits = [
        {"title": "Version studio", "artist": "A", "path": "/a.mp3", "score": 0.90},
        {"title": "Version live", "artist": "A", "path": "/b.mp3", "score": 0.89},
    ]
    monkeypatch.setattr(music, "search_local", lambda query: hits)
    memory = {}

    first = music.music_control(
        {"action": "play", "query": "version", "source": "local"},
        session_memory=memory,
    )
    assert "plusieurs morceaux proches" in first
    assert memory["music_local_results"] == hits

    played = []
    monkeypatch.setattr(
        music,
        "_play_result",
        lambda *args: played.append(args) or "deuxième lancé",
    )
    second = music.music_control(
        {"action": "select", "index": 2}, session_memory=memory
    )

    assert second == "deuxième lancé"
    assert played[0][0] == "/b.mp3"


def test_search_action_searches_locally_not_youtube(monkeypatch):
    hit = {"title": "Local", "artist": "", "path": "/local.mp3", "score": 0.95}
    monkeypatch.setattr(music, "search_local", lambda query: [hit])
    monkeypatch.setattr(
        music,
        "search_youtube",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("YouTube appelé")),
    )

    response = music.music_control(
        {"action": "search", "query": "local"}, session_memory={}
    )

    assert "Résultats dans ta bibliothèque" in response


def test_auto_mode_demande_la_source_quand_rien_nest_local(monkeypatch):
    # Quand le fichier n'est pas trouvé en local, il doit lancer directement en ligne
    monkeypatch.setattr(music, "search_local", lambda query, media_kind="audio": [])
    yt_hit = {
        "id": "vid123",
        "title": "Kanda Bongo Man - Isambe Monie",
        "url": "https://www.youtube.com/watch?v=vid123",
        "uploader": "Kanda Bongo Man",
        "thumbnail_url": "https://i.ytimg.com/vi/vid123/hqdefault.jpg",
    }
    monkeypatch.setattr(music, "search_youtube", lambda query, limit=6: [yt_hit])
    monkeypatch.setattr(music, "resolve_stream_url", lambda url: "http://stream.direct/audio.m4a")

    class FakeIPC:
        def __init__(self):
            self.played = []

        def play(self, target, title="", artist="", thumbnail="", thumbnail_bytes=b""):
            self.played.append((target, title, artist, thumbnail))

    fake_ipc = FakeIPC()
    monkeypatch.setattr(music, "_HAS_IPC_PLAYER", True)
    monkeypatch.setattr(music, "get_player", lambda: fake_ipc)

    # Rien en local : ANO-GPT ne part plus en ligne de sa propre initiative.
    # Il demande la source, et la question doit nommer les deux possibilités —
    # sans elle, l'utilisateur ne sait pas quoi répondre.
    session_memory: dict = {}
    question = music.music_control(
        {"action": "play", "query": "kanda bongo man isambe"},
        session_memory=session_memory,
    )

    assert "bibliothèque locale" in question
    assert "Spotify" in question and "YouTube" in question
    assert fake_ipc.played == [], "aucune lecture avant la réponse de l'utilisateur"
    assert session_memory[music._PENDING_SOURCE_KEY]["query"] == "kanda bongo man isambe"


def test_une_demande_de_clip_attend_aussi_le_choix_de_la_source(monkeypatch):
    # Quand la requête demande un clip/vidéo, elle doit ouvrir le lecteur vidéo intégré
    monkeypatch.setattr(music, "search_local", lambda query, media_kind="audio": [])
    yt_hit = {
        "id": "clip456",
        "title": "Kanda Bongo Man - Clip Officiel",
        "url": "https://www.youtube.com/watch?v=clip456",
        "uploader": "Kanda Vevo",
        "duration": "4:20",
        "views": 150000,
        "thumbnail_url": "https://i.ytimg.com/vi/clip456/hqdefault.jpg",
    }
    monkeypatch.setattr(music, "search_youtube", lambda query, limit=6: [yt_hit])

    class FakePlayerUI:
        def __init__(self):
            self.video_played = []

        def write_log(self, text):
            pass

        def play_video(self, video, playlist):
            self.video_played.append((video, playlist))

    ui = FakePlayerUI()
    session_mem = {}

    question = music.music_control(
        {"action": "play", "query": "lance le clip de kanda bongo"},
        player=ui,
        session_memory=session_mem,
    )

    # Même une demande de clip attend le feu vert : rien ne part vers le
    # lecteur vidéo tant que la source n'est pas choisie.
    assert "bibliothèque locale" in question
    assert ui.video_played == []
    assert session_mem[music._PENDING_SOURCE_KEY]["query"] == "lance le clip de kanda bongo"

