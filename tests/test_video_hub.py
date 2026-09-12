import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QColor, QImage
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

import ui


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _video(video_id, title):
    return {
        "id": video_id,
        "title": title,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "channel": "Canal test",
        "duration": "2:05",
    }


def test_video_hub_builds_cards_and_switches_to_player(app, monkeypatch):
    monkeypatch.setattr(ui, "_WEBENGINE", False)
    hub = ui.VideoHubOverlay()
    videos = [_video("abcdefghijk", "Première"), _video("defghijklmn", "Deuxième")]

    assert hub.show_results("robotique", videos)
    assert hub._grid.count() == 2
    assert hub._results.isVisible()

    assert not hub.play_video(videos[1], videos)  # WebEngine absent, UI de repli visible.
    assert hub._mode == "player"
    assert hub._index == 1
    assert "Deuxième" in hub._now_title.text()
    hub.close_video()


def test_video_hub_next_replaces_current_video(app, monkeypatch):
    monkeypatch.setattr(ui, "_WEBENGINE", False)
    hub = ui.VideoHubOverlay()
    videos = [_video("abcdefghijk", "Première"), _video("defghijklmn", "Deuxième")]
    hub.play_video(videos[0], videos)

    hub.control("next")
    assert hub._index == 1
    assert "Deuxième" in hub.status_text()

    hub.control("previous")
    assert hub._index == 0
    assert "Première" in hub.status_text()
    hub.close_video()


def test_player_html_uses_iframe_api_and_safe_video_id():
    html = ui.VideoHubOverlay._player_html("abcdefghijk")

    assert "youtube.com/iframe_api" in html
    assert 'videoId:"abcdefghijk"' in html
    assert "autoplay:1" in html
    assert "controls:1" in html
    assert "origin:'http://localhost'" in html
    assert "origin:'https://www.youtube.com'" not in html
    assert "origin=https%3A%2F%2Fwww.youtube.com" not in html


def test_player_html_has_resilient_controls_and_fallback():
    html = ui.VideoHubOverlay._player_html("abcdefghijk")
    assert "function ctl(action,value)" in html
    assert "allow='autoplay; encrypted-media; picture-in-picture'" in html or 'allow="autoplay; encrypted-media; picture-in-picture"' in html
    assert "fallback" in html


def test_play_video_sets_localhost_base_url(app, monkeypatch):
    calls = []

    class MockWeb:
        def __init__(self):
            self._html = ""
            self._url = None
        def show(self):
            pass
        def setHtml(self, html, url=None):
            calls.append((html, url))
        def hide(self):
            pass

    hub = ui.VideoHubOverlay()
    mock_web = MockWeb()
    monkeypatch.setattr(hub, "_ensure_web", lambda: True)
    hub._web = mock_web

    video = _video("abcdefghijk", "Première")
    hub.play_video(video, [video])

    assert len(calls) == 1
    assert calls[0][1] is not None
    assert calls[0][1].toString() == "http://localhost/"
    hub.close_video()


def test_video_hub_accepts_local_files_in_same_surface(app, monkeypatch, tmp_path):
    movie = tmp_path / "vacances.mp4"
    movie.write_bytes(b"not-decoded-in-this-test")
    monkeypatch.setattr(ui, "_QTMULTIMEDIA", False)
    hub = ui.VideoHubOverlay()
    local = {
        "path": str(movie), "title": "Vacances", "folder": "Vidéos",
        "source": "local", "kind": "video", "duration": "1:20",
    }

    assert hub.show_results("vacances", [local])
    assert hub.selection_is_local(0)
    assert not hub.play_video(local, [local])
    assert hub._mode == "player"
    assert hub._current_source == "local"
    assert "Vacances" in hub.status_text()
    hub.close_video()


def test_local_video_canvas_paints_decoded_frames(app):
    canvas = ui._VideoFrameCanvas()
    image = QImage(320, 180, QImage.Format.Format_RGB32)
    image.fill(QColor("#123456"))

    class Frame:
        def toImage(self):
            return image

    canvas._on_frame(Frame())

    assert not canvas._image.isNull()
    assert canvas._image.pixelColor(10, 10) == QColor("#123456")


def test_seek_slider_accepts_click_and_drag(app):
    slider = ui._SeekSlider(Qt.Orientation.Horizontal)
    slider.setRange(0, 1000)
    slider.resize(400, 30)
    slider.show()
    requested = []
    slider.sliderMoved.connect(requested.append)

    QTest.mouseClick(slider, Qt.MouseButton.LeftButton, pos=QPoint(300, 15))

    assert 730 <= slider.value() <= 770
    assert requested and requested[-1] == slider.value()


def test_local_seek_updates_player_position(app):
    hub = ui.VideoHubOverlay()

    class Player:
        def __init__(self):
            self.positions = []
        def setPosition(self, value):
            self.positions.append(value)

    hub._current_source = "local"
    hub._local_player = Player()
    hub._duration_seconds = 200.0

    hub._seek_to_progress_value(750)

    assert hub._local_player.positions == [150_000]


def test_stop_returns_to_previous_video_selection(app, monkeypatch, tmp_path):
    first = tmp_path / "une.mp4"
    second = tmp_path / "deux.mp4"
    first.write_bytes(b"video")
    second.write_bytes(b"video")
    monkeypatch.setattr(ui, "_QTMULTIMEDIA", False)
    hub = ui.VideoHubOverlay()
    videos = [
        {"path": str(first), "title": "Une", "kind": "video"},
        {"path": str(second), "title": "Deux", "kind": "video"},
    ]
    hub.show_results("mes vidéos", videos)
    hub.play_video(videos[0], videos)

    hub.control("stop")

    assert hub.isVisible()
    assert hub._mode == "results"
    assert hub._results.isVisible()
    assert not hub._player_panel.isVisible()
    assert hub._grid.count() == 2
    assert "LECTURE ARRÊTÉE" in hub._badge.text()
    hub.close_video()


def test_close_still_exits_video_surface(app, monkeypatch):
    monkeypatch.setattr(ui, "_WEBENGINE", False)
    hub = ui.VideoHubOverlay()
    video = _video("abcdefghijk", "Première")
    hub.show_results("test", [video])
    hub.play_video(video, [video])
    hub.close_video()

    assert not hub.isVisible()


def test_cyberpunk_control_deck_structure_and_buttons(app, monkeypatch):
    monkeypatch.setattr(ui, "_WEBENGINE", False)
    hub = ui.VideoHubOverlay()
    video = _video("abcdefghijk", "Première")
    hub.play_video(video, [video])

    assert hasattr(hub, "_btn_toggle")
    assert hasattr(hub, "_volume_label")
    assert hasattr(hub, "_btn_mute")
    assert "80%" in hub._volume_label.text()

    # Tester le toggle muet
    hub._toggle_mute()
    assert "0%" in hub._volume_label.text() or "MUTÉ" in hub._volume_label.text()
    hub._toggle_mute()
    assert "80%" in hub._volume_label.text()
    hub.close_video()


def test_cyberpunk_card_badge_duration(app):
    from ui.media.video_widgets import VideoResultCard

    video = {
        "id": "abc12345678",
        "title": "Cyberpunk 2077 Night City",
        "channel": "CD Projekt",
        "duration": "03:45",
        "views": 250000,
    }
    card = VideoResultCard(0, video)
    assert hasattr(card, "_duration_badge")
    assert card._duration_badge.text() == "03:45"
    assert not card._duration_badge.isHidden()
