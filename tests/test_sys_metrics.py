"""Le sondage GPU ne doit pas recharger NVML toutes les 1,5 s sans carte NVIDIA."""

from __future__ import annotations

import ui.core.metrics as metrics


def setup_function():
    metrics._metrics.stop()
    metrics.reset_gpu_probe()


def test_interval_is_three_seconds():
    assert metrics.INTERVAL_S == 3.0


def test_missing_nvidia_is_cached_on_linux(monkeypatch):
    monkeypatch.setattr(metrics, "_OS", "Linux")
    calls = {"n": 0}

    def absent():
        calls["n"] += 1
        return None

    monkeypatch.setattr(metrics, "_open_nvml", absent)
    assert metrics._gpu_util() == -1.0
    assert metrics._gpu_util() == -1.0
    assert metrics._nvml_gpu_windows() == -1.0
    assert calls["n"] == 1
    assert metrics._nvml_ok is False


def test_missing_nvidia_is_cached_on_windows_too(monkeypatch):
    monkeypatch.setattr(metrics, "_OS", "Windows")
    calls = {"n": 0}

    def absent():
        calls["n"] += 1
        return None

    monkeypatch.setattr(metrics, "_open_nvml", absent)
    assert metrics._gpu_util() == -1.0
    assert metrics._gpu_util() == -1.0
    assert calls["n"] == 1


def test_successful_probe_reuses_the_reader(monkeypatch):
    opens = {"n": 0}
    reads = {"n": 0}

    def reader():
        reads["n"] += 1
        return 42.0

    def open_nvml():
        opens["n"] += 1
        return reader

    monkeypatch.setattr(metrics, "_open_nvml", open_nvml)
    assert metrics._gpu_util() == 42.0
    assert metrics._gpu_util() == 42.0
    assert opens["n"] == 1
    assert reads["n"] == 2
    assert metrics._nvml_ok is True


def test_sysmetrics_get_gpu_uses_the_cache(monkeypatch):
    calls = {"n": 0}

    def absent():
        calls["n"] += 1
        return None

    monkeypatch.setattr(metrics, "_open_nvml", absent)
    probe = metrics._SysMetrics.__new__(metrics._SysMetrics)
    assert probe._get_gpu() == -1.0
    assert probe._get_gpu() == -1.0
    assert calls["n"] == 1
