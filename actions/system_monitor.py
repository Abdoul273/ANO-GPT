"""
system_monitor.py — Moniteur système ultra‑réaliste
Parsing local des demandes, retour d'état enrichi, alertes proactives.
Utilise psutil, ctypes, pynvml — zéro sous‑processus.
"""

import ctypes
import platform
import time
import re
import json
from pathlib import Path
from typing import Optional, Dict, Any

from core import action_kit as kit
from core.live_model_policy import FAST_MODEL

try:
    import psutil
    _PSUTIL = True
except ImportError:
    _PSUTIL = False

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"

DEFAULT_THRESHOLDS = {
    "cpu":     90.0,
    "ram":     90.0,
    "temp":    85.0,
    "gpu":     95.0,
    "disk":    90.0,
    "battery": 15.0,
}

_COOLDOWN   = 300
_CPU_STREAK = 3

# ── NVML DLL cache ──────────────────────────────────────────────────────────
_nvml_lib: object = None
_nvml_ok:  object = None


def _nvml_gpu() -> float:
    global _nvml_lib, _nvml_ok
    if _nvml_ok is False:
        return -1.0
    try:
        class _Util(ctypes.Structure):
            _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]

        if _nvml_lib is None:
            if _OS == "Windows":
                candidates = ("nvml", r"C:\Windows\System32\nvml.dll")
                _load = ctypes.WinDLL
            else:
                candidates = (
                    "libnvidia-ml.so.1",
                    "libnvidia-ml.so",
                    "libnvidia-ml.dylib",
                )
                _load = ctypes.CDLL
            for name in candidates:
                try:
                    lib = _load(name)
                    lib.nvmlInit_v2()
                    # Sécuriser les signatures d'appel
                    try:
                        lib.nvmlDeviceGetHandleByIndex_v2.argtypes = [ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)]
                        lib.nvmlDeviceGetUtilizationRates.argtypes = [ctypes.c_void_p, ctypes.POINTER(_Util)]
                    except Exception:
                        pass
                    _nvml_lib = lib
                    break
                except Exception:
                    continue
        if _nvml_lib is None:
            _nvml_ok = False
            return -1.0

        dev = ctypes.c_void_p()
        if _nvml_lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(dev)) != 0:
            _nvml_ok = False
            return -1.0
        u = _Util()
        if _nvml_lib.nvmlDeviceGetUtilizationRates(dev, ctypes.byref(u)) != 0:
            _nvml_ok = False
            return -1.0
        _nvml_ok = True
        return float(u.gpu)
    except Exception:
        _nvml_ok = False
        return -1.0


def _get_gpu_usage() -> float:
    try:
        import pynvml
        pynvml.nvmlInit()
        h = pynvml.nvmlDeviceGetHandleByIndex(0)
        return float(pynvml.nvmlDeviceGetUtilizationRates(h).gpu)
    except Exception:
        pass
    return _nvml_gpu()


def _get_cpu_temp() -> float:
    if not _PSUTIL:
        return -1.0
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


def _get_disk_usage() -> Optional[Dict[str, float]]:
    if not _PSUTIL:
        return None
    try:
        du = psutil.disk_usage("/")
        return {"total": du.total, "used": du.used, "percent": du.percent}
    except Exception:
        return None


def _get_battery() -> Optional[Dict[str, Any]]:
    if not _PSUTIL:
        return None
    try:
        b = psutil.sensors_battery()
        if b:
            return {"percent": b.percent, "plugged": b.power_plugged}
    except Exception:
        pass
    return None


def get_top_processes(n: int = 5, sort_by: str = "cpu") -> str:
    """Liste les n processus les plus gourmands (CPU ou RAM)."""
    if not _PSUTIL:
        return "psutil n'est pas installé. Exécutez : pip install psutil"
    try:
        # Premier passage pour amorcer la mesure CPU
        for _ in psutil.process_iter(["pid"]):
            pass
        time.sleep(0.2)
        procs = []
        for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
            try:
                procs.append(p.info)
            except Exception:
                continue
        if sort_by == "mem":
            procs.sort(key=lambda x: x.get("memory_percent") or 0, reverse=True)
        else:
            procs.sort(key=lambda x: x.get("cpu_percent") or 0, reverse=True)
        lines = ["Top processus :"]
        for p in procs[:n]:
            name = (p.get("name") or "?")[:30]
            cpu = p.get("cpu_percent") or 0.0
            mem = p.get("memory_percent") or 0.0
            lines.append(f"  {p['pid']:>6}  {name:30}  CPU {cpu:5.1f}%  RAM {mem:5.1f}%")
        return "\n".join(lines)
    except Exception as e:
        return f"Erreur lors de la récupération des processus : {e}"


@kit.action("get_system_status")
def get_system_status(speak_lang: str = "fr") -> str:
    """Retourne un résumé textuel de l'état système."""
    if not _PSUTIL:
        return "psutil n'est pas installé. Exécutez : pip install psutil"
    cpu  = psutil.cpu_percent(interval=0.2)
    ram  = psutil.virtual_memory()
    temp = _get_cpu_temp()
    gpu  = _get_gpu_usage()
    disk = _get_disk_usage()
    batt = _get_battery()
    boot_time   = psutil.boot_time()
    uptime_secs = time.time() - boot_time
    uptime_h    = int(uptime_secs // 3600)
    uptime_m    = int((uptime_secs % 3600) // 60)
    parts = []
    fr = speak_lang.startswith("fr")
    if fr:
        parts.append("État du système :")
        parts.append(f"CPU : {cpu:.0f}% utilisé")
        parts.append(f"RAM : {ram.percent:.0f}% utilisé ({ram.used / 1024**3:.1f} Go / {ram.total / 1024**3:.1f} Go)")
        if temp > 0:
            parts.append(f"Température CPU : {temp:.0f}°C")
        if gpu >= 0:
            parts.append(f"GPU : {gpu:.0f}% utilisé")
        if disk:
            parts.append(f"Disque : {disk['percent']:.0f}% utilisé ({disk['used'] / 1024**3:.1f} Go / {disk['total'] / 1024**3:.1f} Go)")
        if batt:
            state = "en charge" if batt["plugged"] else "sur batterie"
            parts.append(f"Batterie : {batt['percent']:.0f}% ({state})")
        parts.append(f"Disponibilité : {uptime_h}h {uptime_m}m")
        parts.append(f"Processus : {len(psutil.pids())}")
    else:
        parts.append("System status:")
        parts.append(f"CPU: {cpu:.0f}% used")
        parts.append(f"RAM: {ram.percent:.0f}% used ({ram.used / 1024**3:.1f} GB / {ram.total / 1024**3:.1f} GB)")
        if temp > 0:
            parts.append(f"CPU Temp: {temp:.0f}°C")
        if gpu >= 0:
            parts.append(f"GPU: {gpu:.0f}% used")
        if disk:
            parts.append(f"Disk: {disk['percent']:.0f}% used ({disk['used'] / 1024**3:.1f} GB / {disk['total'] / 1024**3:.1f} GB)")
        if batt:
            state = "charging" if batt["plugged"] else "on battery"
            parts.append(f"Battery: {batt['percent']:.0f}% ({state})")
        parts.append(f"Uptime: {uptime_h}h {uptime_m}m")
        parts.append(f"Processes: {len(psutil.pids())}")
    return "\n".join(parts)


class SystemMonitor:
    """Moniteur avec alertes, cooldown, et état persistant."""

    def __init__(self, thresholds: dict | None = None):
        self.thresholds   = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
        self._last_alert: dict[str, float] = {}
        self._cpu_streak  = 0

    def _can_alert(self, key: str) -> bool:
        return (time.monotonic() - self._last_alert.get(key, 0)) > _COOLDOWN

    def _record(self, key: str):
        self._last_alert[key] = time.monotonic()

    def check(self) -> str | None:
        if not _PSUTIL:
            return None
        try:
            cpu  = psutil.cpu_percent(interval=None)
            ram  = psutil.virtual_memory().percent
            temp = _get_cpu_temp()
            gpu  = _get_gpu_usage()
            disk = _get_disk_usage()
            batt = _get_battery()
        except Exception:
            return None

        alerts: list[str] = []

        if cpu >= self.thresholds["cpu"]:
            self._cpu_streak += 1
            if self._cpu_streak >= _CPU_STREAK and self._can_alert("cpu"):
                alerts.append(
                    f"[SYSTEM_ALERT] CPU usage has been critically high ({cpu:.0f}%) "
                    "for several seconds. Warn the user in their language and suggest "
                    "closing heavy applications."
                )
                self._record("cpu")
                self._cpu_streak = 0
        else:
            self._cpu_streak = 0

        if ram >= self.thresholds["ram"] and self._can_alert("ram"):
            alerts.append(
                f"[SYSTEM_ALERT] RAM is at {ram:.0f}% — nearly exhausted. "
                "Warn the user in their language and suggest freeing memory."
            )
            self._record("ram")

        if temp > 0 and temp >= self.thresholds["temp"] and self._can_alert("temp"):
            alerts.append(
                f"[SYSTEM_ALERT] CPU temperature is {temp:.0f}°C — above the safe limit. "
                "Warn the user in their language and advise reducing system load "
                "or checking cooling."
            )
            self._record("temp")

        if gpu >= 0 and gpu >= self.thresholds["gpu"] and self._can_alert("gpu"):
            alerts.append(
                f"[SYSTEM_ALERT] GPU load is at {gpu:.0f}%. "
                "Briefly inform the user in their language."
            )
            self._record("gpu")

        if disk and disk["percent"] >= self.thresholds["disk"] and self._can_alert("disk"):
            alerts.append(
                f"[SYSTEM_ALERT] Disk usage is at {disk['percent']:.0f}% — nearly full. "
                "Warn the user in their language and suggest freeing space."
            )
            self._record("disk")

        if batt and not batt["plugged"] and batt["percent"] <= self.thresholds["battery"] and self._can_alert("battery"):
            alerts.append(
                f"[SYSTEM_ALERT] Battery is low ({batt['percent']:.0f}%). "
                "Inform the user in their language and suggest plugging in."
            )
            self._record("battery")

        return " ".join(alerts) if alerts else None


# ── Parsing local pour commandes naturelles ──────────────────────────────────
def _parse_monitor_request_locally(text: str) -> Optional[Dict[str, Any]]:
    """
    Détecte une demande d'état du système et retourne l'action correspondante.
    Les composants spécifiques sont détectés AVANT le statut général.
    """
    if not _PSUTIL:
        return None
    text = text.lower().strip()
    text = re.sub(r"\b(s'il te pla[iî]t|stp|please|peux-tu|tu peux|je veux|j'aimerais|dis-moi)\b", "", text).strip()

    # Composants spécifiques d'abord
    if re.search(r"\b(cpu|processeur|charge cpu)\b", text):
        return {"action": "component", "component": "cpu"}
    if re.search(r"\b(ram|mémoire|memoire)\b", text):
        return {"action": "component", "component": "ram"}
    if re.search(r"\b(temp[eé]rature|chauffe|chaud)\b", text):
        return {"action": "component", "component": "temp"}
    if re.search(r"\b(gpu|carte graphique|graphique)\b", text):
        return {"action": "component", "component": "gpu"}
    if re.search(r"\b(disque|disk|stockage|espace)\b", text):
        return {"action": "component", "component": "disk"}
    if re.search(r"\b(batterie|battery|charge)\b", text):
        return {"action": "component", "component": "battery"}

    # Top processus / nombre de processus
    if re.search(r"\b(processus|process|tâches|taches)\b", text):
        if re.search(r"\b(lourd|top|plus|gourmand)\b", text):
            return {"action": "top"}
        return {"action": "processes"}
    if re.search(r"\btop\b", text):
        return {"action": "top"}

    # Uptime
    if re.search(r"\b(uptime|disponibilité|depuis quand|allumé depuis)\b", text):
        return {"action": "uptime"}

    # Statut général
    if re.search(r"\b(état|etat|status|santé|sante|health|comment va|comment se porte|système|systeme|ressources?|performances?)\b", text):
        return {"action": "status"}

    return None


def _detect_monitor_intent_ai(description: str) -> Optional[Dict]:
    api_key = _get_api_key()
    if not api_key:
        return None
    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        prompt = (
            f"Analyse cette phrase et retourne UNIQUEMENT un JSON avec 'action' "
            f"(status, uptime, processes, top, component) et optionnellement "
            f"'component' (cpu, ram, temp, gpu, disk, battery).\n"
            f"Phrase : \"{description}\"\n"
            f"Exemple : \"quel est l'état du système ?\" → {{\"action\":\"status\"}}\n"
            f"\"combien de processus tournent ?\" → {{\"action\":\"processes\"}}\n"
            f"\"quel est le cpu ?\" → {{\"action\":\"component\",\"component\":\"cpu\"}}\n"
            f"\"montre les processus lourds\" → {{\"action\":\"top\"}}\n"
            f"Réponds uniquement avec le JSON."
        )
        resp = client.models.generate_content(model=FAST_MODEL, contents=prompt)
        json_match = re.search(r'{.*}', resp.text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group(0))
    except Exception as e:
        print(f"[SystemMonitor] Erreur IA intent: {e}")
    return None


def _get_api_key() -> str:
    try:
        base = Path(__file__).resolve().parent.parent
        config = base / "config" / "api_keys.json"
        with open(config, "r", encoding="utf-8") as f:
            return json.load(f).get("gemini_api_key", "")
    except Exception:
        return ""


# ── Point d'entrée public ───────────────────────────────────────────────────
def system_status_tool(
    parameters: dict = None,
    player=None,
    speak=None,
) -> str:
    """
    Outil de monitoring système.
    Accepte une description naturelle ou des paramètres explicites.
    Paramètres :
        description : phrase naturelle (ex: "quel est l'état du système ?")
        action      : "status", "uptime", "processes", "top", "component"
        component   : "cpu", "ram", "temp", "gpu", "disk", "battery" (optionnel)
    """
    if not _PSUTIL:
        return "psutil n'est pas installé. Exécutez : pip install psutil"

    params = parameters or {}
    description = params.get("description", "").strip()
    action = params.get("action", "").strip().lower()
    component = params.get("component", "").strip().lower()

    # Interprétation naturelle
    if description and not action:
        local = _parse_monitor_request_locally(description)
        if local:
            action = local.get("action", action)
            component = local.get("component", component)
        else:
            ai = _detect_monitor_intent_ai(description)
            if ai:
                action = ai.get("action", action)
                component = ai.get("component", component)
            else:
                action = "status"
    if not action:
        action = "status"

    try:
        # Composant spécifique prioritaire
        if component:
            if component == "cpu":
                return f"Utilisation CPU : {psutil.cpu_percent(interval=0.2):.0f}%"
            elif component == "ram":
                ram = psutil.virtual_memory()
                return f"RAM : {ram.percent:.0f}% utilisé ({ram.used / 1024**3:.1f} Go / {ram.total / 1024**3:.1f} Go)"
            elif component == "temp":
                t = _get_cpu_temp()
                return f"Température CPU : {t:.0f}°C" if t > 0 else "Température CPU indisponible."
            elif component == "gpu":
                g = _get_gpu_usage()
                return f"Utilisation GPU : {g:.0f}%" if g >= 0 else "GPU indisponible."
            elif component == "disk":
                d = _get_disk_usage()
                if d:
                    return f"Disque : {d['percent']:.0f}% utilisé ({d['used'] / 1024**3:.1f} Go / {d['total'] / 1024**3:.1f} Go)"
                return "Utilisation du disque indisponible."
            elif component == "battery":
                b = _get_battery()
                if b:
                    state = "en charge" if b["plugged"] else "sur batterie"
                    return f"Batterie : {b['percent']:.0f}% ({state})"
                return "Batterie indisponible."
            else:
                return get_system_status()

        if action == "status":
            return get_system_status(speak_lang="fr")
        elif action == "uptime":
            boot = psutil.boot_time()
            uptime_secs = time.time() - boot
            h = int(uptime_secs // 3600)
            m = int((uptime_secs % 3600) // 60)
            return f"Le système est allumé depuis {h}h {m}m."
        elif action == "processes":
            count = len(psutil.pids())
            return f"{count} processus sont en cours d'exécution."
        elif action == "top":
            return get_top_processes(n=5, sort_by="cpu")
        elif action == "component":
            # Déjà traité ci-dessus si component est fourni
            return get_system_status()
        else:
            return get_system_status()
    except Exception as e:
        return f"Erreur lors de la récupération de l'état système : {e}"


# ── Test direct ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        print(system_status_tool({"description": " ".join(sys.argv[1:])}))
    else:
        print(system_status_tool({"action": "status"}))
