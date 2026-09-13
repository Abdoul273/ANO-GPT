from __future__ import annotations

import platform
import threading
import time
from typing import Callable, Optional

import psutil

_OS = platform.system()

# Un tick trop serré rechargeait NVML (échec) toutes les 1,5 s sur une
# machine sans NVIDIA : deux CDLL ratés, GIL disputé avec la voix.
INTERVAL_S = 3.0

_nvml_ok: Optional[bool] = None
_nvml_read: Optional[Callable[[], float]] = None
_probe_lock = threading.Lock()


def reset_gpu_probe() -> None:
    """Remet le cache NVML à zéro (tests)."""
    global _nvml_ok, _nvml_read
    with _probe_lock:
        _nvml_ok = None
        _nvml_read = None


def _open_nvml() -> Optional[Callable[[], float]]:
    """Ouvre NVML une fois. ``None`` = pas de GPU NVIDIA sur cette machine."""
    try:
        import pynvml
        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)

        def _read_pynvml() -> float:
            return float(pynvml.nvmlDeviceGetUtilizationRates(handle).gpu)

        return _read_pynvml
    except Exception:
        pass

    try:
        import ctypes

        if _OS == "Windows":
            lib = None
            for dll_name in ("nvml", r"C:\Windows\System32\nvml.dll"):
                try:
                    candidate = ctypes.WinDLL(dll_name)
                    candidate.nvmlInit_v2()
                    lib = candidate
                    break
                except Exception:
                    continue
            if lib is None:
                return None
        else:
            soname = "libnvidia-ml.so.1" if _OS == "Linux" else "libnvidia-ml.dylib"
            lib = ctypes.CDLL(soname)
            lib.nvmlInit_v2()

        class _Util(ctypes.Structure):
            _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]

        def _read_ctypes() -> float:
            dev = ctypes.c_void_p()
            lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(dev))
            util = _Util()
            lib.nvmlDeviceGetUtilizationRates(dev, ctypes.byref(util))
            return float(util.gpu)

        return _read_ctypes
    except Exception:
        return None


def _gpu_util() -> float:
    """Utilisation GPU 0–100, ou -1. L'absence de NVIDIA n'est sondée qu'une fois."""
    global _nvml_ok, _nvml_read
    with _probe_lock:
        if _nvml_ok is False:
            return -1.0
        if _nvml_read is not None:
            try:
                return float(_nvml_read())
            except Exception:
                _nvml_ok = False
                _nvml_read = None
                return -1.0
        reader = _open_nvml()
        if reader is None:
            _nvml_ok = False
            return -1.0
        try:
            value = float(reader())
        except Exception:
            _nvml_ok = False
            return -1.0
        _nvml_ok = True
        _nvml_read = reader
        return value


def _nvml_gpu_windows() -> float:
    """Ancien nom conservé : le cache NVML vaut pour Linux aussi."""
    return _gpu_util()


class _SysMetrics:
    def __init__(self):
        self.cpu  = 0.0
        self.mem  = 0.0
        self.net  = 0.0
        self.gpu  = -1.0
        self.tmp  = -1.0
        self._lock = threading.Lock()
        self._last_net = psutil.net_io_counters()
        self._last_net_t = time.time()
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, name="ano-sys-metrics", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def _loop(self):
        while self._running:
            try:
                self._update()
            except Exception:
                pass
            time.sleep(INTERVAL_S)

    def _update(self):
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory().percent
        nc  = psutil.net_io_counters()
        now = time.time()
        dt  = now - self._last_net_t
        if dt > 0:
            sent = (nc.bytes_sent - self._last_net.bytes_sent) / dt
            recv = (nc.bytes_recv - self._last_net.bytes_recv) / dt
            net  = (sent + recv) / (1024 * 1024)
        else:
            net = 0.0
        self._last_net   = nc
        self._last_net_t = now
        gpu = self._get_gpu()
        tmp = self._get_temp()
        with self._lock:
            self.cpu = cpu
            self.mem = mem
            self.net = net
            self.gpu = gpu
            self.tmp = tmp

    def _get_gpu(self) -> float:
        return _gpu_util()

    def _get_temp(self) -> float:
        try:
            temps = psutil.sensors_temperatures()
            for name in ["coretemp", "k10temp", "cpu_thermal", "acpitz",
                         "cpu-thermal", "zenpower", "it8688"]:
                if name in temps and temps[name]:
                    return temps[name][0].current
            for entries in temps.values():
                if entries:
                    return entries[0].current
        except Exception:
            pass
        if _OS == "Windows":
            try:
                import wmi
                w = wmi.WMI(namespace="root/wmi")
                tz = w.MSAcpi_ThermalZoneTemperature()
                if tz:
                    return (tz[0].CurrentTemperature / 10.0) - 273.15
            except Exception:
                pass
        return -1.0

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "cpu": self.cpu,
                "mem": self.mem,
                "net": self.net,
                "gpu": self.gpu,
                "tmp": self.tmp,
            }

_metrics = _SysMetrics()
