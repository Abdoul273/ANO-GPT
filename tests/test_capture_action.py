from pathlib import Path
from types import SimpleNamespace

from actions import capture


def test_full_capture_uses_caelestia_and_preserves_requested_output(monkeypatch, tmp_path):
    caelestia_dir = tmp_path / "caelestia"
    output = tmp_path / "export" / "shot.png"

    monkeypatch.setattr(capture, "_hypr_env", lambda: {})
    monkeypatch.setattr(capture, "_have", lambda binary: binary == "caelestia")
    monkeypatch.setattr(capture, "_caelestia_screenshots_dir", lambda env: caelestia_dir)

    def fake_run(cmd, **kwargs):
        assert cmd == ["caelestia", "screenshot"]
        caelestia_dir.mkdir()
        (caelestia_dir / "20260910120000.png").write_bytes(b"png-data")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(capture.subprocess, "run", fake_run)
    result = capture.take_screenshot(output=str(output))

    assert result["ok"] is True
    assert output.read_bytes() == b"png-data"
    assert "Caelestia" in result["message"]


def test_region_uses_caelestia_native_picker(monkeypatch):
    seen = []
    monkeypatch.setattr(capture, "_hypr_env", lambda: {})
    monkeypatch.setattr(capture, "_have", lambda binary: binary == "caelestia")
    monkeypatch.setattr(
        capture.subprocess, "run",
        lambda cmd, **kwargs: seen.append(cmd) or SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    result = capture.take_screenshot(mode="region", freeze=True)

    assert result["ok"] is True
    assert seen == [["caelestia", "screenshot", "-r", "-f"]]


def test_caelestia_directory_uses_session_configuration(tmp_path):
    configured = tmp_path / "Captures"
    assert capture._caelestia_screenshots_dir(
        {"CAELESTIA_SCREENSHOTS_DIR": str(configured)}
    ) == configured


def test_voice_recording_starts_persistent_gsr_with_sound_and_microphone(monkeypatch, tmp_path):
    commands = []

    monkeypatch.setattr(capture, "_hypr_env", lambda: {})
    monkeypatch.setattr(capture, "_have", lambda binary: binary == "gpu-screen-recorder")
    monkeypatch.setattr(capture, "_caelestia_recordings_dir", lambda env: tmp_path)
    monkeypatch.setattr(capture, "_current_monitor", lambda: "eDP-1")
    monkeypatch.setattr(capture, "_default_mic_source", lambda: "alsa_input.test")
    monkeypatch.setattr(capture, "_read_state", lambda: None)
    monkeypatch.setattr(capture, "_write_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(capture, "_update_state", lambda **kwargs: None)
    monkeypatch.setattr(capture.time, "sleep", lambda _: None)

    monkeypatch.setattr(capture, "_caelestia_recording_running", lambda env: False)

    def fake_popen(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(pid=4242, poll=lambda: None)

    monkeypatch.setattr(capture.subprocess, "Popen", fake_popen)
    result = capture.start_recording(audio="both")

    assert result["ok"] is True
    assert commands[0][:3] == ["gpu-screen-recorder", "-w", "eDP-1"]
    assert commands[0].count("-a") == 2
    assert "default_output" in commands[0]
    assert "alsa_input.test" in commands[0]
