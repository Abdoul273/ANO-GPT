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
        return capture.kit.ProcResult(cmd=tuple(cmd), code=0)

    monkeypatch.setattr(capture.kit, "run", fake_run)
    result = capture.take_screenshot(output=str(output))

    assert result["ok"] is True
    assert output.read_bytes() == b"png-data"
    assert "Caelestia" in result["message"]


def test_caelestia_capture_file_is_success_even_if_cli_does_not_exit(monkeypatch, tmp_path):
    """Le shell peut laisser son CLI vivant après avoir écrit l'image."""
    caelestia_dir = tmp_path / "caelestia"
    monkeypatch.setattr(capture, "_hypr_env", lambda: {})
    monkeypatch.setattr(capture, "_have", lambda binary: binary == "caelestia")
    monkeypatch.setattr(capture, "_caelestia_screenshots_dir", lambda env: caelestia_dir)

    def fake_run(cmd, **kwargs):
        assert kwargs["timeout"] == 7
        caelestia_dir.mkdir()
        (caelestia_dir / "completed.png").write_bytes(b"png-data")
        return capture.kit.ProcResult(cmd=tuple(cmd), code=-9, timed_out=True)

    monkeypatch.setattr(capture.kit, "run", fake_run)
    result = capture.take_screenshot()

    assert result["ok"] is True
    assert "completed.png" in result["message"]


def test_capture_policy_leaves_margin_after_caelestia_timeout():
    """Le CLI Caelestia est tué à 7 s, le tour Live garde sa marge."""
    from core.action_runtime import ActionRuntime
    from core.tool_dispatcher import TOOL_DECLARATIONS

    assert ActionRuntime(TOOL_DECLARATIONS).policy_for("capture_control").timeout_s == 10.0


def test_region_uses_caelestia_native_picker(monkeypatch):
    seen = []
    monkeypatch.setattr(capture, "_hypr_env", lambda: {})
    monkeypatch.setattr(capture, "_have", lambda binary: binary == "caelestia")
    monkeypatch.setattr(
        capture.kit, "run",
        lambda cmd, **kwargs: seen.append(cmd) or capture.kit.ProcResult(cmd=tuple(cmd), code=0),
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
