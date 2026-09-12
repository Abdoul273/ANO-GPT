"""Tests unitaires pour AudioCaptureStream (capture audio PipeWire directe)."""

import asyncio
from unittest.mock import MagicMock, patch

import numpy as np

from core.audio_capture import AudioCaptureConfig, AudioCaptureStream, pcm16_rms_dbfs


def test_capture_config_defaults_and_properties():
    cfg = AudioCaptureConfig()
    assert cfg.sample_rate == 16000
    assert cfg.channels == 1
    assert cfg.frame_ms == 20
    assert cfg.dtype == "int16"
    assert cfg.frame_samples == 320  # 16000 * 20 / 1000
    assert cfg.frame_bytes == 640   # 320 * 1 * 2


def test_capture_config_custom_rates():
    cfg = AudioCaptureConfig(sample_rate=48000, frame_ms=10, dtype="float32")
    assert cfg.frame_samples == 480
    assert cfg.frame_bytes == 1920  # 480 * 1 * 4


def test_pcm16_rms_dbfs_silence_and_full_scale():
    silence = np.zeros(320, dtype=np.int16).tobytes()
    assert pcm16_rms_dbfs(silence) <= -90.0
    full = np.full(320, 32767, dtype=np.int16).tobytes()
    assert pcm16_rms_dbfs(full) > -1.0


def test_find_pipewire_device_index():
    fake_devices = [
        {"name": "HDA Intel Analog", "max_input_channels": 2},
        {"name": "pipewire", "max_input_channels": 128},
    ]
    with patch("sounddevice.query_devices", return_value=fake_devices):
        idx = AudioCaptureStream.find_pipewire_device_index()
        assert idx == 1

    fake_no_pw = [
        {"name": "HDA Intel Analog", "max_input_channels": 2},
    ]
    with patch("sounddevice.query_devices", return_value=fake_no_pw):
        idx = AudioCaptureStream.find_pipewire_device_index()
        assert idx is None


def test_find_pipewire_prefers_pulse_bridge():
    fake_devices = [
        {"name": "HDA Intel Analog", "max_input_channels": 2},
        {"name": "pulse", "max_input_channels": 128},
        {"name": "pipewire", "max_input_channels": 128},
    ]
    with patch("sounddevice.query_devices", return_value=fake_devices):
        assert AudioCaptureStream.find_pipewire_device_index() == 1


def test_audio_capture_lifecycle_and_reading():
    config = AudioCaptureConfig(frame_ms=20, queue_maxsize=10)
    stream = AudioCaptureStream(config)

    fake_stream_instance = MagicMock()
    with patch("sounddevice.InputStream", return_value=fake_stream_instance):
        stream.start()
        assert stream.is_active
        assert "sounddevice" in stream.backend

        dummy_pcm = (np.ones(320, dtype=np.int16) * 1000).tobytes()
        stream._queue.put(dummy_pcm)

        read_bytes = stream.read(timeout=0.1)
        assert read_bytes == dummy_pcm

        stream._queue.put(dummy_pcm)
        arr = stream.read_numpy(timeout=0.1)
        assert isinstance(arr, np.ndarray)
        assert arr.shape == (320,)
        assert arr.dtype == np.int16
        assert arr[0] == 1000

        stream.stop()
        assert not stream.is_active
        fake_stream_instance.stop.assert_called_once()
        fake_stream_instance.close.assert_called_once()


def test_audio_capture_context_manager():
    config = AudioCaptureConfig()
    stream = AudioCaptureStream(config)

    fake_sd = MagicMock()
    with patch("sounddevice.InputStream", return_value=fake_sd):
        with stream as s:
            assert s.is_active
        assert not stream.is_active


def test_audio_capture_sync_iter_frames():
    config = AudioCaptureConfig()
    stream = AudioCaptureStream(config)

    with patch("sounddevice.InputStream", return_value=MagicMock()):
        stream.start()
        for i in range(3):
            stream._queue.put(f"frame_{i}".encode())

        frames = list(stream.iter_frames(max_frames=3))
        assert len(frames) == 3
        assert frames[0] == b"frame_0"
        assert frames[2] == b"frame_2"
        stream.stop()


def test_audio_capture_async_frames():
    config = AudioCaptureConfig()
    stream = AudioCaptureStream(config)

    async def scenario():
        with patch("sounddevice.InputStream", return_value=MagicMock()):
            async with stream:
                q = stream.get_async_queue()
                await q.put(b"async_0")
                await q.put(b"async_1")

                gen = stream.async_frames(max_frames=2)
                results = []
                async for f in gen:
                    results.append(f)
                assert results == [b"async_0", b"async_1"]

    asyncio.run(scenario())


def test_audio_capture_queue_overflow():
    config = AudioCaptureConfig(queue_maxsize=2)
    stream = AudioCaptureStream(config)

    with patch("sounddevice.InputStream") as mock_input:
        stream.start()
        cb = mock_input.call_args[1]["callback"]

        dummy_indata = np.zeros(320, dtype=np.int16)
        for _ in range(4):
            cb(dummy_indata, 320, None, None)

        assert stream._queue.qsize() == 2
        assert stream._overflow_count >= 2
        stream.stop()


def test_audio_capture_pw_record_fallback():
    config = AudioCaptureConfig(backend="pw-record")
    stream = AudioCaptureStream(config)
    fake_proc = MagicMock()
    fake_proc.poll.return_value = None
    fake_proc.stdout = MagicMock()
    fake_proc.stdout.read.return_value = b""

    with patch("core.audio_capture.shutil.which", return_value="/usr/bin/pw-record"), \
         patch("core.audio_capture.subprocess.Popen", return_value=fake_proc) as popen:
        stream.start()
        assert stream.is_active
        assert "pw-record" in stream.backend
        cmd = popen.call_args[0][0]
        assert "--rate=16000" in cmd
        assert "--channels=1" in cmd
        assert "--format=s16" in cmd
        assert "--raw" in cmd
        stream.stop()
        fake_proc.terminate.assert_called()
