"""Toutes les ouvertures ANO-GPT restent dans Chrome, même après une session Firefox."""
from unittest.mock import Mock

from core import browser_policy as policy


def test_chrome_launch_uses_normal_profile_and_literal_url(monkeypatch):
    launch = Mock()
    monkeypatch.setattr(policy, "chrome_binary", lambda: "/opt/Google Chrome/chrome")
    monkeypatch.setattr(policy.subprocess, "Popen", launch)
    url = "https://example.com/?q=a&next=b"
    assert policy.open_chrome(url)
    assert launch.call_args.args[0] == ["/opt/Google Chrome/chrome", url]
    assert "shell" not in launch.call_args.kwargs


def test_chrome_failure_never_launches_another_browser(monkeypatch):
    from actions import browser_control, youtube_video
    launch = Mock(side_effect=OSError("unavailable"))
    monkeypatch.setattr(policy, "chrome_binary", lambda: "/usr/bin/google-chrome-stable")
    monkeypatch.setattr(policy.subprocess, "Popen", launch)
    assert not youtube_video._open_url("https://example.com", prefer_browser="firefox")
    assert "Impossible" in browser_control._open_native("https://example.com", "firefox")
    assert all(call.args[0][0] == "/usr/bin/google-chrome-stable" for call in launch.call_args_list)


def test_missing_chrome_does_not_fall_back_to_firefox(monkeypatch):
    launch = Mock()
    monkeypatch.setattr(policy, "chrome_binary", lambda: None)
    monkeypatch.setattr(policy.subprocess, "Popen", launch)
    assert not policy.open_chrome("https://example.com")
    launch.assert_not_called()


def test_webbrowser_library_default_is_chrome(monkeypatch):
    import webbrowser
    monkeypatch.setattr(webbrowser, "_browsers", {})
    monkeypatch.setattr(webbrowser, "_tryorder", [])
    monkeypatch.setenv("BROWSER", "firefox")
    opener = Mock(return_value=True)
    monkeypatch.setattr(policy, "open_chrome", opener)
    policy.prefer_chrome()
    assert webbrowser.open("https://example.com")
    opener.assert_called_once_with("https://example.com")


def test_browser_manager_ignores_previous_firefox_session(monkeypatch):
    from actions import browser_control
    monkeypatch.setattr(browser_control, "_HAS_PLAYWRIGHT", True)
    manager = browser_control.SessionManager()
    chrome, firefox = Mock(), Mock()
    chrome._closed = firefox._closed = False
    manager._sessions = {"chrome": chrome, "firefox": firefox}
    manager._active_browser = "firefox"
    assert manager.get() is chrome
    assert manager.get("firefox") is chrome
    assert manager._active_browser == "chrome"


def test_legacy_web_opener_ignores_firefox_argument(monkeypatch):
    from actions import web_control
    monkeypatch.setattr(policy, "chrome_binary", lambda: "/usr/bin/google-chrome-stable")
    launch = Mock()
    monkeypatch.setattr(web_control.subprocess, "Popen", launch)
    monkeypatch.setattr(web_control, "_WAYLAND", False)
    result = web_control.open_url("https://example.com", browser="firefox")
    assert launch.call_args.args[0] == ["/usr/bin/google-chrome-stable", "https://example.com"]


def test_app_aliases_resolve_to_chrome():
    from actions.open_app import _normalize
    assert _normalize("firefox") == _normalize("chrome")
    assert _normalize("navigateur") == _normalize("chrome")


def test_browser_session_cannot_start_firefox():
    from actions.browser_control import BrowserSession
    session = BrowserSession("firefox")
    assert session.name == "chrome"
    assert session._resolve_engine() == "chromium"


def test_local_web_documents_open_in_chrome(tmp_path, monkeypatch):
    from actions import open_app, file_processor, file_controller
    document = tmp_path / "rapport avec espaces.HTML"
    document.write_text("<html><body>Rapport</body></html>")
    opener = Mock(return_value=True)
    monkeypatch.setattr(policy, "open_chrome", opener)
    monkeypatch.setattr(file_controller, "_is_safe_path", lambda path: True)
    assert open_app._open_path_default(document)
    assert "Chrome" in file_processor._open_file(document)
    assert "Chrome" in file_controller.open_path(str(document))
    assert opener.call_count == 3
    assert all(c.args == (document.resolve().as_uri(),) for c in opener.call_args_list)


def test_failed_web_document_open_does_not_use_default_app(tmp_path, monkeypatch):
    from actions import open_app, file_processor, file_controller
    document = tmp_path / "rapport.html"
    document.write_text("<html></html>")
    monkeypatch.setattr(policy, "open_chrome", lambda url: False)
    launch = Mock()
    monkeypatch.setattr(policy.subprocess, "Popen", launch)
    monkeypatch.setattr(file_controller, "_is_safe_path", lambda path: True)
    assert not open_app._open_path_default(document)
    assert "Impossible" in file_processor._open_file(document)
    assert "Impossible" in file_controller.open_path(str(document))
    launch.assert_not_called()
