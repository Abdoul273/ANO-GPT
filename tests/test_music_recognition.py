"""Reconnaissance musicale : lecteur d'abord, empreinte ensuite, jamais de devinette muette."""

from __future__ import annotations

import struct
import wave

from actions import music_recognition as mr
from core import action_kit as kit


def _proc(text: str, ok: bool = True):
    return kit.ProcResult(cmd=("x",), code=0 if ok else 1, out=text)


def test_mpris_playing_track_is_returned_without_listening(monkeypatch):
    monkeypatch.setattr(kit, "which", lambda c: "/usr/bin/" + c)
    line = "spotify\tPlaying\tDaft Punk\tOne More Time\tDiscovery\thttps://i/cover.jpg\tspotify:track:1"
    monkeypatch.setattr(kit, "run", lambda cmd, **kw: _proc(line))
    track = mr._playing_from_mpris()
    assert track["title"] == "One More Time" and track["artist"] == "Daft Punk"
    assert track["method"] == "mpris" and track["confidence"] == "exacte"


def test_mpris_paused_or_pageless_browser_is_ignored(monkeypatch):
    monkeypatch.setattr(kit, "which", lambda c: "/usr/bin/" + c)
    lines = "spotify\tPaused\tA\tB\t\t\t\nfirefox\tPlaying\t\tPage web\t\t\t"
    monkeypatch.setattr(kit, "run", lambda cmd, **kw: _proc(lines))
    assert mr._playing_from_mpris() is None


def _write_wav(path, amplitude: int, seconds: float = 0.5):
    n = int(44100 * seconds)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(44100)
        w.writeframes(struct.pack(f"<{n}h", *([amplitude, -amplitude] * (n // 2))))


def test_level_detects_silence_and_signal(tmp_path):
    _write_wav(tmp_path / "quiet.wav", 3)
    _write_wav(tmp_path / "loud.wav", 8000)
    assert mr._level_dbfs(tmp_path / "quiet.wav") < mr.SILENCE_DBFS
    assert mr._level_dbfs(tmp_path / "loud.wav") > mr.SILENCE_DBFS


def test_capture_falls_back_to_microphone_when_speakers_are_silent(tmp_path, monkeypatch):
    monkeypatch.setattr(mr, "_default_monitor", lambda: "sink.monitor")
    monkeypatch.setattr(mr, "_default_source", lambda: "mic")
    monkeypatch.setattr(mr.tempfile, "mkdtemp", lambda prefix: str(tmp_path))

    def fake_capture(device, seconds, out):
        _write_wav(out, 2 if device == "sink.monitor" else 9000)
        return True

    monkeypatch.setattr(mr, "_capture", fake_capture)
    path, origin = mr.capture_playing_audio(3.0)
    assert origin == "micro" and path.name == "mic.wav"


def test_parse_shazam_extracts_metadata_and_spotify_uri():
    data = {"track": {
        "title": "Blinding Lights", "subtitle": "The Weeknd", "url": "https://www.shazam.com/track/1",
        "isrc": "USUG11904206", "genres": {"primary": "Pop"},
        "images": {"coverart": "https://c/1.jpg", "coverarthq": "https://c/hq.jpg"},
        "sections": [{"metadata": [{"title": "Album", "text": "After Hours"},
                                   {"title": "Released", "text": "2019"},
                                   {"title": "Label", "text": "XO"}]}],
        "hub": {"providers": [{"type": "SPOTIFY", "actions": [{"uri": "spotify:track:0VjIjW4GlUZAMYd2vXMi3b"}]}],
                "options": [{"actions": [{"uri": "https://music.apple.com/x"}]}]},
    }}
    t = mr._parse_shazam(data, "sortie système")
    assert (t["title"], t["artist"], t["album"], t["year"], t["label"], t["genre"]) == \
        ("Blinding Lights", "The Weeknd", "After Hours", "2019", "XO", "Pop")
    assert t["spotify_uri"].startswith("spotify:track:") and t["cover_url"].endswith("hq.jpg")
    assert t["apple_url"].startswith("https://music.apple.com")
    assert mr._parse_shazam({"track": {}}, "x") is None


def test_identify_retries_a_longer_window_then_reports_honestly(monkeypatch):
    monkeypatch.setattr(kit, "which", lambda c: "/usr/bin/" + c)
    monkeypatch.setattr(mr, "_playing_from_mpris", lambda: None)
    monkeypatch.setattr(mr, "fingerprint_engines", lambda: ["shazamio"])
    monkeypatch.setattr(mr.time, "sleep", lambda s: None)
    windows = []
    monkeypatch.setattr(mr, "capture_playing_audio",
                        lambda seconds, progress=None: (windows.append(seconds) or (mr.Path("/tmp/x.wav"), "micro")))
    monkeypatch.setattr(mr, "identify_file", lambda p: None)
    result = mr.identify_now_playing()
    assert windows == [mr.CAPTURE_S, mr.SECOND_WINDOW_S]
    assert result["found"] is False and "micro" in result["reason"]


def test_tool_identify_remembers_and_offers_spotify(monkeypatch):
    track = {"title": "Bad Guy", "artist": "Billie Eilish", "album": "WWWY", "year": "2019",
             "method": "shazam", "source": "Shazam, sortie système", "confidence": "haute", "cover_url": ""}
    monkeypatch.setattr(mr, "identify_now_playing", lambda progress=None, allow_mpris=True: {"found": True, "track": track, "reason": ""})
    monkeypatch.setattr(mr, "_HISTORY_FILE", mr.Path("/nonexistent/dir/h.jsonl"))
    cards = []

    class Player:
        on_text_command = None

        def show_card(self, kind, title, body, actions=None):
            cards.append((kind, title, body))

    sm = {}
    said = mr.music_recognition({"action": "identify"}, player=Player(), session_memory=sm)
    assert "Bad Guy" in said and "Billie Eilish" in said and "Spotify" in said
    assert sm[mr.LAST_KEY]["title"] == "Bad Guy"
    assert cards and cards[0][0] == "music" and "Bad Guy" in cards[0][2]


def test_tool_play_uses_direct_spotify_uri(monkeypatch):
    from actions import music as music_action
    spawned = []
    monkeypatch.setattr(music_action, "_spotify_binary", lambda: "spotify")
    monkeypatch.setattr(music_action, "_HAS_IPC_PLAYER", False)
    monkeypatch.setattr(kit, "spawn", lambda cmd, **kw: spawned.append(cmd) or object())
    sm = {mr.LAST_KEY: {"title": "T", "artist": "A", "spotify_uri": "spotify:track:abc"}}
    out = mr.music_recognition({"action": "play", "target": "spotify"}, session_memory=sm)
    assert spawned == [["spotify", "spotify:track:abc"]] and "Spotify" in out


def test_tool_play_without_memory_explains(monkeypatch):
    monkeypatch.setattr(mr, "_HISTORY_FILE", mr.Path("/nonexistent/h.jsonl"))
    assert "aucune musique" in mr.music_recognition({"action": "play"}, session_memory={})


def test_tool_is_declared_and_bounded():
    from core.action_runtime import ActionRuntime
    from core.tool_dispatcher import TOOL_DECLARATIONS
    names = {d["name"] for d in TOOL_DECLARATIONS}
    assert "music_recognition" in names
    assert ActionRuntime(TOOL_DECLARATIONS).policy_for("music_recognition").timeout_s >= 45
