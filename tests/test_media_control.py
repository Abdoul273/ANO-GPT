"""Tests unitaires pour la priorité Google Chrome dans media_control, desktop_apps et youtube_service.

Valide :
1. L'ordonnancement de _BROWSER_TOKENS et la détection prioritaire de Chrome dans _focus_browser et _browser_mpris.
2. La priorité des binaires Chrome devant Firefox dans les alias de desktop_apps.
3. La résilience de _search_youtube_scraping face aux erreurs réseau et la conformité du paramètre sp.
"""

from __future__ import annotations

from unittest.mock import MagicMock
import pytest

from actions import desktop_apps
from actions import media_control
from core import youtube_service


def test_browser_tokens_chrome_priority() -> None:
    """Vérifie que les tokens Chrome/Chromium figurent en tête de _BROWSER_TOKENS."""
    assert hasattr(media_control, "_BROWSER_TOKENS")
    tokens = list(media_control._BROWSER_TOKENS)
    # Les premiers éléments doivent être les tokens Chrome
    first_four = tokens[:4]
    assert "google-chrome-stable" in first_four
    assert "google-chrome" in first_four
    assert "chrome" in first_four
    assert "chromium" in first_four
    # Firefox doit être positionné après les variantes Chrome
    assert tokens.index("firefox") > tokens.index("google-chrome-stable")
    assert tokens.index("firefox") > tokens.index("chromium")


def test_focus_browser_prioritizes_chrome_over_firefox(monkeypatch: pytest.MonkeyPatch) -> None:
    """Vérifie que _focus_browser cible Chrome en priorité quand Chrome et Firefox sont ouverts simultanément."""
    mock_clients = [
        {"class": "firefox", "address": "0x1234firefox", "title": "Mozilla Firefox"},
        {"class": "google-chrome", "address": "0x5678chrome", "title": "Google Chrome"},
    ]
    focused_windows: list[str] = []

    monkeypatch.setattr(media_control, "_hyprctl_json", lambda cmd: mock_clients if cmd == "clients" else None)

    def mock_focus(addr: str) -> bool:
        focused_windows.append(f"address:{addr}")
        return True

    monkeypatch.setattr(media_control, "_focus_address", mock_focus)

    success = media_control._focus_browser()
    assert success is True
    assert len(focused_windows) == 1
    assert focused_windows[0] == "address:0x5678chrome", (
        f"Chrome aurait dû recevoir le focus en priorité, reçu : {focused_windows[0]}"
    )


def test_focus_browser_falls_back_to_firefox_when_no_chrome(monkeypatch: pytest.MonkeyPatch) -> None:
    """Vérifie que _focus_browser focalise Firefox si aucun navigateur Chrome n'est présent."""
    mock_clients = [
        {"class": "kitty", "address": "0xterminal", "title": "Terminal"},
        {"class": "firefox", "address": "0x1234firefox", "title": "Mozilla Firefox"},
    ]
    focused_windows: list[str] = []

    monkeypatch.setattr(media_control, "_hyprctl_json", lambda cmd: mock_clients if cmd == "clients" else None)

    def mock_focus(addr: str) -> bool:
        focused_windows.append(f"address:{addr}")
        return True

    monkeypatch.setattr(media_control, "_focus_address", mock_focus)

    success = media_control._focus_browser()
    assert success is True
    assert len(focused_windows) == 1
    assert focused_windows[0] == "address:0x1234firefox"


def test_browser_mpris_prioritizes_chrome_over_firefox(monkeypatch: pytest.MonkeyPatch) -> None:
    """Vérifie que _browser_mpris sélectionne le lecteur playerctl Chrome avant Firefox."""
    mock_players = ["firefox.instance1", "chromium.instance2", "vlc"]
    monkeypatch.setattr(media_control, "_playerctl_players", lambda: mock_players)

    selected = media_control._browser_mpris()
    assert selected == "chromium.instance2", (
        f"Le lecteur Chrome/Chromium aurait dû être sélectionné en premier, obtenu : {selected}"
    )


def test_browser_mpris_falls_back_to_firefox(monkeypatch: pytest.MonkeyPatch) -> None:
    """Vérifie que _browser_mpris sélectionne Firefox si aucun lecteur Chrome/Chromium n'est actif."""
    mock_players = ["vlc", "firefox.instance1"]
    monkeypatch.setattr(media_control, "_playerctl_players", lambda: mock_players)

    selected = media_control._browser_mpris()
    assert selected == "firefox.instance1"


def test_desktop_apps_browser_aliases_chrome_priority() -> None:
    """« Ouvre le navigateur » désigne Chrome, et seulement Chrome.

    Le projet a fait de Chrome son unique navigateur (core/browser_policy) :
    les sessions, les cookies et l'automatisation y vivent. Ouvrir Firefox
    au hasard rendait ces états introuvables.
    """
    aliases = desktop_apps._APP_ALIASES
    for key in ("navigateur", "browser", "web", "internet"):
        candidates = aliases[key]
        assert candidates[0] == "google-chrome-stable"
        assert "google-chrome" in candidates
        assert "firefox" not in candidates


def test_search_youtube_scraping_resilience_on_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Vérifie que _search_youtube_scraping gère proprement une exception réseau et retourne une liste vide."""
    import requests

    def failing_get(*args, **kwargs):
        raise requests.RequestException("Connection timeout")

    monkeypatch.setattr(requests, "get", failing_get)

    results = youtube_service._search_youtube_scraping("test query")
    assert results == [], "Une erreur réseau dans le scraping direct doit retourner [] sans lever d'exception"


def test_search_youtube_scraping_sp_parameter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Vérifie que l'URL construite pour le scraping utilise le bon paramètre sp=EgIQAQ%3D%3D."""
    import requests

    captured_urls: list[str] = []

    class DummyResponse:
        ok = False
        text = ""

    def mock_get(url: str, *args, **kwargs):
        captured_urls.append(url)
        return DummyResponse()

    monkeypatch.setattr(requests, "get", mock_get)

    youtube_service._search_youtube_scraping("test query")
    assert captured_urls, "requests.get aurait dû être appelé"
    assert "sp=EgIQAQ%3D%3D" in captured_urls[0]
    assert "sp=EgIQAQ%253D%253D" not in captured_urls[0]
