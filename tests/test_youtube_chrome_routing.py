"""Tests TDD pour le lancement Chrome, la résilience YouTube et le contrôle média.

Cette suite valide :
1. La résilience de search_youtube : fallback vers le scraping direct si yt-dlp échoue ou est absent.
2. L'ouverture préférentielle de Google Chrome pour la lecture des vidéos YouTube.
3. Le routage du contrôle YouTube vers media_control lorsque la lecture est dans le navigateur (ou lecteur Qt inactif).
4. La transmission explicite du navigateur Chrome dans InstalledAppFlow.run_local_server pour Gmail OAuth.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from actions import youtube_video as youtube_action
from core import youtube_service
from core.email_service import GmailService
from core.youtube_service import YouTubeResult, YouTubeUnavailable


MOCK_YOUTUBE_SCRAPE_HTML = (
    "<!DOCTYPE html><html><body><script>"
    'var ytInitialData = {"contents":{"twoColumnSearchResultsRenderer":{"primaryContents":'
    '{"sectionListRenderer":{"contents":[{"itemSectionRenderer":{"contents":['
    '{"videoRenderer":{"videoId":"kanda_fallback_01",'
    '"title":{"runs":[{"text":"Kanda Bongo Man - Sai"}]},'
    '"ownerText":{"runs":[{"text":"Kanda Bongo Official"}]},'
    '"lengthText":{"simpleText":"4:15"},'
    '"viewCountText":{"simpleText":"980,000 vues"}'
    "}}]}}]}}}}};"
    "</script>"
    '"videoId":"kanda_fallback_01",'
    '"title":{"runs":[{"text":"Kanda Bongo Man - Sai"}]},'
    '"ownerText":{"runs":[{"text":"Kanda Bongo Official"}]}'
    "</body></html>"
)


class MockHttpResponse:
    """Mock léger pour les requêtes HTTP de scraping."""

    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code
        self.ok = status_code == 200
        self.content = text.encode("utf-8")
        self.headers = {"content-type": "text/html; charset=utf-8"}


def test_search_youtube_fallback_scraping_when_ytdlp_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Quand yt-dlp est absent ou en échec, search_youtube bascule sur le scraping web direct.

    La fonction doit renvoyer au moins un YouTubeResult valide sans lever YouTubeUnavailable.
    """
    import requests

    def mock_get(url: str, *args, **kwargs) -> MockHttpResponse:
        assert "youtube.com" in url
        return MockHttpResponse(MOCK_YOUTUBE_SCRAPE_HTML)

    monkeypatch.setattr(requests, "get", mock_get)

    # Cas 1 : yt-dlp n'est pas installé sur le système
    monkeypatch.setattr(
        youtube_service.shutil,
        "which",
        lambda cmd: None if cmd == "yt-dlp" else f"/usr/bin/{cmd}",
    )

    results = youtube_service.search_youtube("kanda bongo", limit=2)
    assert isinstance(results, list), "search_youtube doit retourner une liste"
    assert len(results) >= 1, "Le scraping direct doit retourner au moins un résultat valide"
    assert isinstance(results[0], YouTubeResult)
    assert results[0].id == "kanda_fallback_01"
    assert "Kanda Bongo" in results[0].title
    assert results[0].url == "https://www.youtube.com/watch?v=kanda_fallback_01"

    # Cas 2 : yt-dlp est présent mais son exécution échoue (erreur processus / timeout)
    monkeypatch.setattr(
        youtube_service.shutil,
        "which",
        lambda cmd: "/usr/bin/yt-dlp" if cmd == "yt-dlp" else f"/usr/bin/{cmd}",
    )

    def failing_runner(*args, **kwargs):
        raise subprocess.SubprocessError("yt-dlp crash inattendu")

    results_fallback = youtube_service.search_youtube(
        "kanda bongo", limit=2, runner=failing_runner
    )
    assert len(results_fallback) >= 1, "Le fallback doit s'activer même en cas d'erreur de runner"
    assert results_fallback[0].id == "kanda_fallback_01"


def test_youtube_video_play_opens_in_chrome(monkeypatch: pytest.MonkeyPatch) -> None:
    """Quand youtube_video play est exécuté avec browser='chrome' ou par défaut,

    il invoque Google Chrome via subprocess.Popen plutôt qu'un lecteur externe ou un blocage.
    """
    popen_calls: list[list[str]] = []

    def mock_popen(cmd: list[str], *args, **kwargs) -> MagicMock:
        popen_calls.append(list(cmd) if isinstance(cmd, (list, tuple)) else [str(cmd)])
        proc = MagicMock()
        proc.pid = 4242
        proc.returncode = 0
        return proc

    monkeypatch.setattr(subprocess, "Popen", mock_popen)

    def mock_which(cmd: str) -> str | None:
        if cmd in ("google-chrome-stable", "google-chrome", "chromium"):
            return f"/usr/bin/{cmd}"
        return f"/usr/bin/{cmd}"

    monkeypatch.setattr(youtube_action.shutil, "which", mock_which)

    mock_result = YouTubeResult(
        id="kanda_bongo_01",
        title="Kanda Bongo Man - Lubaki",
        url="https://www.youtube.com/watch?v=kanda_bongo_01",
        channel="Kanda Bongo",
        duration_seconds=280,
        views=150000,
    )
    monkeypatch.setattr(
        youtube_action,
        "search_youtube_structured",
        lambda query, limit: [mock_result],
    )

    # Cas 1 : Lancement standard avec browser="chrome" (ou cible Chrome) sans lecteur Qt
    session_memory: dict = {}
    response = youtube_action.youtube_video(
        {"action": "play", "query": "kanda bongo", "browser": "chrome"},
        session_memory=session_memory,
    )

    assert popen_calls, "subprocess.Popen doit être appelé pour lancer le navigateur"
    chrome_call = popen_calls[-1]
    binary = str(chrome_call[0]).lower()
    assert any(
        name in binary
        for name in ("google-chrome-stable", "google-chrome", "chromium", "chrome")
    ), f"La commande doit invoquer Chrome, reçu : {binary}"
    assert "https://www.youtube.com/watch?v=kanda_bongo_01" in chrome_call
    assert "aucun navigateur externe" not in response.lower()
    assert session_memory.get("youtube_playback_target") in ("chrome", "browser")

    # Cas 2 : Si browser="chrome" est demandé alors qu'un player Qt est présent,
    # le choix Chrome doit être respecté (le player Qt n'intercepte pas la lecture)
    class MockIntegratedPlayer:
        def __init__(self) -> None:
            self.played: list[tuple] = []

        def play_video(self, video: dict, playlist: list[dict]) -> None:
            self.played.append((video, playlist))

    qt_player = MockIntegratedPlayer()
    popen_calls.clear()
    res_player = youtube_action.youtube_video(
        {"action": "play", "query": "kanda bongo", "browser": "chrome"},
        player=qt_player,
        session_memory={},
    )
    assert len(qt_player.played) == 0, "Le lecteur Qt ne doit pas intercepter la demande explicite de Chrome"
    assert popen_calls, "Chrome doit être lancé via subprocess.Popen"


def test_youtube_control_routes_to_media_control_when_browser_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Quand une commande de contrôle YouTube (pause, resume, volume, seek, next, previous) est envoyée

    et que le lecteur intégré Qt n'est pas actif (ou que player=None),
    l'action est transférée à media_control pour piloter Chrome via MPRIS/wtype.
    """
    recorded_media_calls: list[dict] = []
    monkeypatch.setattr(
        youtube_action,
        "media_control",
        lambda params: recorded_media_calls.append(dict(params)) or f"handled:{params.get('action')}",
    )

    # 1. Validation avec player=None pour chaque action clé
    control_scenarios = [
        ("pause", {"action": "pause"}, "youtube_pause", None),
        ("resume", {"action": "resume"}, "youtube_play", None),
        ("volume", {"action": "volume", "volume": 75}, "youtube_volume", 75),
        ("seek", {"action": "seek", "seconds": 15}, "youtube_seek", 15),
        ("next", {"action": "next"}, "youtube_next", None),
        ("previous", {"action": "previous"}, "youtube_previous", None),
    ]

    for name, params, expected_action, expected_value in control_scenarios:
        recorded_media_calls.clear()
        res = youtube_action.youtube_video(params, player=None)
        assert len(recorded_media_calls) == 1, f"media_control doit être appelé pour l'action {name}"
        assert recorded_media_calls[0]["action"] == expected_action
        if expected_value is not None:
            assert recorded_media_calls[0].get("value") == expected_value
        assert "handled:" in res

    # 2. Validation avec un lecteur Qt inactif ou session_memory indiquant une lecture navigateur
    class MockInactivePlayer:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object]] = []

        def write_log(self, text: str) -> None:
            pass

        def is_video_active(self) -> bool:
            return False

        def is_active(self) -> bool:
            return False

        def control_video(self, action: str, value: object = None) -> None:
            self.calls.append((action, value))

    inactive_player = MockInactivePlayer()
    recorded_media_calls.clear()
    res_inactive = youtube_action.youtube_video(
        {"action": "pause"},
        player=inactive_player,
        session_memory={"youtube_playback_target": "browser"},
    )
    assert len(inactive_player.calls) == 0, (
        "Le lecteur Qt inactif ne doit pas recevoir control_video quand la lecture est dans le navigateur"
    )
    assert len(recorded_media_calls) == 1, "La commande de contrôle doit être redirigée vers media_control"
    assert recorded_media_calls[0]["action"] == "youtube_pause"


def test_gmail_connect_specifies_chrome_browser(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Vérifie que GmailService.connect(interactive=True) transmet browser='google-chrome-stable'

    (ou 'google-chrome') à InstalledAppFlow.run_local_server pour forcer l'ouverture du consentement dans Chrome.
    """
    client_secret_file = tmp_path / "client_secret.json"
    client_secret_file.write_text(
        json.dumps({
            "installed": {
                "client_id": "mock_id.apps.googleusercontent.com",
                "client_secret": "mock_secret_abc",
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        }),
        encoding="utf-8",
    )
    token_file = tmp_path / "gmail_token.json"

    # Forcer la détection des dépendances Gmail
    monkeypatch.setattr(GmailService, "dependencies_available", staticmethod(lambda: True))

    # Mock de shutil.which pour simuler la présence de google-chrome-stable
    def mock_which(cmd: str) -> str | None:
        if cmd in ("google-chrome-stable", "google-chrome", "chromium"):
            return f"/usr/bin/{cmd}"
        return None

    monkeypatch.setattr(shutil, "which", mock_which)

    captured_server_kwargs: dict = {}

    class MockFlow:
        def run_local_server(self, **kwargs) -> MagicMock:
            captured_server_kwargs.update(kwargs)
            mock_creds = MagicMock()
            mock_creds.to_json.return_value = json.dumps({"token": "fake_oauth_token"})
            mock_creds.valid = True
            mock_creds.expired = False
            mock_creds.refresh_token = "fake_refresh_token"
            return mock_creds

    import google_auth_oauthlib.flow

    monkeypatch.setattr(
        google_auth_oauthlib.flow.InstalledAppFlow,
        "from_client_secrets_file",
        lambda *args, **kwargs: MockFlow(),
    )

    # Mock de googleapiclient.discovery.build et requête getProfile
    mock_profile_req = MagicMock()
    mock_profile_req.execute.return_value = {"emailAddress": "test.user@gmail.com"}
    mock_users = MagicMock()
    mock_users.getProfile.return_value = mock_profile_req
    mock_service = MagicMock()
    mock_service.users.return_value = mock_users

    import googleapiclient.discovery

    monkeypatch.setattr(
        googleapiclient.discovery, "build", lambda *args, **kwargs: mock_service
    )

    service = GmailService(token_file=token_file, client_secret_file=client_secret_file)
    profile = service.connect(interactive=True)

    assert profile.get("emailAddress") == "test.user@gmail.com"
    assert "browser" in captured_server_kwargs, (
        "run_local_server doit recevoir l'argument 'browser' pour forcer Google Chrome"
    )
    chosen_browser = str(captured_server_kwargs.get("browser", "")).lower()
    assert any(
        name in chosen_browser
        for name in ("google-chrome-stable", "google-chrome", "chromium", "chrome")
    ), f"Le navigateur transmis à run_local_server doit être Chrome, reçu : {chosen_browser!r}"
