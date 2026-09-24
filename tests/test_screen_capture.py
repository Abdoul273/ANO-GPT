"""tests/test_screen_capture.py — Tests unitaires pour core/screen_capture.py."""

import pytest
from unittest.mock import patch
from core.screen_capture import (
    WindowInfo,
    get_all_clients,
    get_active_window,
    find_window_by_query,
    compress_image_bytes,
    capture_window_or_screen,
    capture_target_for_query,
)


def test_window_info_properties():
    w = WindowInfo(
        address="0x1234",
        window_class="kitty",
        title="zsh - python main.py",
        at=(100, 200),
        size=(800, 600),
    )
    assert w.geometry_str == "100,200 800x600"
    assert w.is_terminal is True
    assert w.is_ide is False

    w_ide = WindowInfo(window_class="code", title="main.py - Visual Studio Code")
    assert w_ide.is_ide is True

    w_browser = WindowInfo(window_class="firefox", title="Mozilla Firefox")
    assert w_browser.is_browser is True

    w_pdf = WindowInfo(window_class="zathura", title="paper.pdf")
    assert w_pdf.is_doc_or_pdf is True


def test_get_all_clients():
    mock_clients = [
        {
            "address": "0xabc",
            "class": "kitty",
            "title": "terminal",
            "at": [0, 0],
            "size": [1920, 1080],
            "workspace": {"id": 1, "name": "1"},
        },
        {
            "address": "0xdef",
            "class": "firefox",
            "title": "Web",
            "at": [1920, 0],
            "size": [1920, 1080],
            "workspace": {"id": 2, "name": "2"},
        },
    ]

    with patch("core.screen_capture._hyprctl_json", return_value=mock_clients):
        clients = get_all_clients()
        assert len(clients) == 2
        assert clients[0].window_class == "kitty"
        assert clients[0].geometry_str == "0,0 1920x1080"
        assert clients[1].window_class == "firefox"


def test_get_active_window_skip_anogpt():
    mock_active = {
        "address": "0xjarvis",
        "class": "jarvis-dashboard",
        "title": "ANO-GPT HUD",
        "at": [500, 300],
        "size": [400, 400],
    }
    mock_clients = [
        mock_active,
        {
            "address": "0xkitty",
            "class": "kitty",
            "title": "zsh",
            "at": [0, 0],
            "size": [1920, 1080],
        },
    ]

    def _mock_hypr(cmd):
        if cmd == "activewindow":
            return mock_active
        if cmd == "clients":
            return mock_clients
        return None

    with patch("core.screen_capture._hyprctl_json", side_effect=_mock_hypr):
        win = get_active_window(skip_anogpt=True)
        assert win is not None
        assert win.window_class == "kitty"
        assert win.address == "0xkitty"


def test_find_window_by_query():
    mock_clients = [
        WindowInfo(address="0x1", window_class="code", title="editor.py"),
        WindowInfo(address="0x2", window_class="kitty", title="server log"),
    ]

    with patch("core.screen_capture.get_all_clients", return_value=mock_clients):
        found = find_window_by_query("editor")
        assert found is not None
        assert found.window_class == "code"

        found_addr = find_window_by_query("0x2")
        assert found_addr is not None
        assert found_addr.window_class == "kitty"


def test_compress_image_bytes():
    # Image PNG factice
    try:
        import PIL.Image
        import io
        img = PIL.Image.new("RGB", (100, 100), color="blue")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        raw = buf.getvalue()

        compressed, mime = compress_image_bytes(raw, max_dim=(50, 50), quality=75)
        assert mime == "image/jpeg"
        assert len(compressed) > 0
    except ImportError:
        pytest.skip("PIL indisponible")


def test_capture_window_or_screen():
    fake_png = b"\x89PNG\r\n\x1a\nfake_image_data"
    with patch("core.screen_capture.capture_raw_geometry", return_value=fake_png), \
         patch("core.screen_capture.compress_image_bytes", return_value=(fake_png, "image/jpeg")), \
         patch("core.screen_capture.get_active_window", return_value=WindowInfo(address="0x1", window_class="kitty", at=(0,0), size=(100,100))):
        img_bytes, mime, meta = capture_window_or_screen(target="active_window")
        assert img_bytes == fake_png
        assert mime == "image/jpeg"
        assert meta["window_class"] == "kitty"
        assert meta["is_terminal"] is True


def test_system_question_captures_full_screen_not_active_window():
    from core import screen_capture

    assert capture_target_for_query("Où se trouve l'heure sur mon système ?") == "screen"
    assert capture_target_for_query("Pointe l'horloge dans la barre du haut") == "screen"
    assert capture_target_for_query("Lis cette erreur dans le terminal") == "active_window"

    with patch.object(screen_capture, "capture_raw_geometry", return_value=b"image") as capture, \
         patch.object(screen_capture, "get_active_window", side_effect=AssertionError("fenêtre recadrée")):
        data, mime, metadata = capture_window_or_screen(target="screen", compress=False)

    assert data == b"image"
    assert metadata["target"] == "screen"
    capture.assert_called_once_with(monitor=None)


def test_monitor_capture_uses_requested_output():
    from core import screen_capture

    with patch.object(screen_capture, "capture_raw_geometry", return_value=b"image") as capture, \
         patch.object(screen_capture, "get_monitor_geometry", return_value=(1920, 0, 1280, 720)):
        _, _, metadata = capture_window_or_screen(
            target="monitor", monitor_name="DP-2", compress=False)

    capture.assert_called_once_with(monitor="DP-2")
    assert metadata["capture_origin"] == (1920, 0)
    assert metadata["capture_size"] == (1280, 720)


def test_full_screen_single_monitor_keeps_pointing_geometry():
    from core import screen_capture

    with patch.object(screen_capture, "capture_raw_geometry", return_value=b"image"), \
         patch.object(screen_capture, "monitor_names_focused_first", return_value=["eDP-1"]), \
         patch.object(screen_capture, "get_monitor_geometry", return_value=(0, 0, 1920, 1080)):
        _, _, metadata = capture_window_or_screen(target="screen", compress=False)

    assert metadata["capture_origin"] == (0, 0)
    assert metadata["capture_size"] == (1920, 1080)
