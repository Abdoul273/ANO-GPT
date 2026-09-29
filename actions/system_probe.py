"""system_probe.py — l'état réel de la machine, lu à la source.

Un seul point d'entrée, :func:`system_state`, pour répondre à toute question du
type « est-ce que X est actif ? » sans jamais deviner : chaque valeur vient de
la commande qui fait autorité (bluetoothctl, nmcli, wpctl, hyprctl,
brightnessctl, powerprofilesctl, playerctl, l'état de Caelestia…).

Toutes les sondes passent par ``core.action_kit`` (délai obligatoire, cache
Hyprland partagé) et tournent en parallèle : un relevé complet coûte le temps
de la plus lente, pas la somme. Une sonde qui échoue dit « indisponible » —
elle ne renvoie jamais une valeur plausible inventée.
"""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from core import action_kit as kit

_C = {"LC_ALL": "C", "LANG": "C"}
_CAELESTIA_STATE = Path.home() / ".local/state/caelestia"
_UNKNOWN = "indisponible"


# ── Petits utilitaires ──────────────────────────────────────────────────────

def _out(cmd: list[str], timeout: float = 3.0) -> str:
    """Sortie standard d'une commande, ou chaîne vide si elle échoue."""
    if not kit.which(cmd[0]):
        return ""
    res = kit.run(cmd, timeout=timeout, env=_C, quiet=True)
    return str(res.out).strip() if res.ok else ""


def _state_file(name: str) -> dict:
    try:
        data = json.loads((_CAELESTIA_STATE / name).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _onoff(value: bool | None) -> str:
    return _UNKNOWN if value is None else ("activé" if value else "désactivé")


# ── Sondes ──────────────────────────────────────────────────────────────────

def _rfkill() -> dict[str, bool]:
    """True si la radio est utilisable (ni bloquée logiciellement ni matériellement)."""
    raw = _out(["rfkill", "-J"])
    states: dict[str, bool] = {}
    try:
        devices = (json.loads(raw) or {}).get("rfkilldevices") or []
    except ValueError:
        return states
    for dev in devices:
        kind = str(dev.get("type", "")).lower()
        free = (str(dev.get("soft", "")).lower() == "unblocked"
                and str(dev.get("hard", "")).lower() == "unblocked")
        states[kind] = states.get(kind, False) or free
    return states


def probe_bluetooth() -> str:
    show = _out(["bluetoothctl", "show"], timeout=4)
    if not show:
        return f"Bluetooth : {_UNKNOWN} (bluetoothctl ne répond pas)."
    powered = re.search(r"(?m)^\s*Powered:\s*(yes|no)", show)
    if not powered or powered.group(1) != "yes":
        radio = _rfkill().get("bluetooth")
        blocked = " (radio bloquée par rfkill)" if radio is False else ""
        return f"Bluetooth : DÉSACTIVÉ{blocked}."
    discoverable = "oui" if re.search(r"(?m)^\s*Discoverable:\s*yes", show) else "non"
    connected = []
    for line in _out(["bluetoothctl", "devices", "Connected"], timeout=4).splitlines():
        parts = line.split(" ", 2)
        if len(parts) == 3 and parts[0] == "Device":
            battery = ""
            info = _out(["bluetoothctl", "info", parts[1]], timeout=3)
            match = re.search(r"Battery Percentage:.*\((\d+)\)", info)
            if match:
                battery = f" {match.group(1)} %"
            connected.append(f"{parts[2]}{battery}")
    devices = ("connecté à : " + ", ".join(connected)) if connected else "aucun appareil connecté"
    return f"Bluetooth : ACTIVÉ, {devices} (visible : {discoverable})."


def probe_wifi() -> str:
    radio = _out(["nmcli", "-t", "radio", "wifi"]).lower()
    if not radio:
        state = _rfkill().get("wlan")
        return f"Wi-Fi : {_onoff(state).upper()}."
    if not radio.startswith("enabled"):
        return "Wi-Fi : DÉSACTIVÉ."
    for line in _out(["nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL", "dev", "wifi"], 6).splitlines():
        parts = line.split(":")
        if len(parts) >= 3 and parts[0] == "yes":
            return f"Wi-Fi : ACTIVÉ, connecté à « {parts[1]} » (signal {parts[2]} %)."
    return "Wi-Fi : ACTIVÉ, aucun réseau connecté."


def probe_network() -> str:
    lines = []
    for line in _out(["nmcli", "-t", "-f", "TYPE,NAME,DEVICE", "con", "show", "--active"]).splitlines():
        kind, _, rest = line.partition(":")
        name, _, dev = rest.rpartition(":")
        if kind in {"loopback", "bridge"}:
            continue
        lines.append(f"{kind} « {name} » ({dev})")
    vpn = [ln for ln in lines if ln.startswith(("vpn", "wireguard", "tun"))]
    online = _out(["nmcli", "-t", "-g", "CONNECTIVITY", "general"]).lower() or _UNKNOWN
    labels = {"full": "internet OK", "limited": "connexion limitée", "portal": "portail captif",
              "none": "hors ligne"}
    text = f"Réseau : {labels.get(online, online)} ; connexions actives : "
    text += ", ".join(lines) if lines else "aucune"
    text += ". VPN : " + ("ACTIF (" + ", ".join(vpn) + ")" if vpn else "déconnecté") + "."
    return text


def _wpctl(target: str) -> tuple[int | None, bool | None]:
    raw = _out(["wpctl", "get-volume", target])
    match = re.search(r"Volume:\s*([\d.]+)", raw)
    if not match:
        return None, None
    return round(float(match.group(1)) * 100), "[MUTED]" in raw


def _default_name(target: str) -> str:
    """Nom lisible du périphérique par défaut, lu chez PipeWire."""
    match = re.search(r'node\.description\s*=\s*"([^"]+)"',
                      _out(["wpctl", "inspect", target]))
    return match.group(1) if match else ""


def probe_audio() -> str:
    volume, muted = _wpctl("@DEFAULT_AUDIO_SINK@")
    mic, mic_muted = _wpctl("@DEFAULT_AUDIO_SOURCE@")
    if volume is None:
        return f"Audio : {_UNKNOWN}."
    sink = _default_name("@DEFAULT_AUDIO_SINK@")
    source = _default_name("@DEFAULT_AUDIO_SOURCE@")
    text = f"Son : {volume} %{' (COUPÉ)' if muted else ''}" + (f" sur {sink}" if sink else "")
    if mic is not None:
        text += f". Micro : {mic} %{' (COUPÉ)' if mic_muted else ''}" + (f" ({source})" if source else "")
    return text + "."


def probe_brightness() -> str:
    raw = _out(["brightnessctl", "-m"])
    parts = raw.split(",")
    if len(parts) >= 4:
        return f"Luminosité : {parts[3]} ({parts[0]})."
    return f"Luminosité : {_UNKNOWN}."


def probe_power() -> str:
    battery = "aucune batterie"
    for bat in sorted(Path("/sys/class/power_supply").glob("BAT*")):
        try:
            level = (bat / "capacity").read_text().strip()
            status = (bat / "status").read_text().strip().lower()
        except OSError:
            continue
        labels = {"charging": "en charge", "discharging": "sur batterie",
                  "full": "pleine", "not charging": "branchée, ne charge pas"}
        battery = f"{level} % ({labels.get(status, status)})"
        break
    profile = _out(["powerprofilesctl", "get"]) or _UNKNOWN
    caffeine = _state_file("caffeine.json")
    veille = "veille bloquée (mode sans limite)" if caffeine.get("enabled") else "veille normale"
    return f"Batterie : {battery}. Profil d'énergie : {profile}. {veille.capitalize()}."


def probe_comfort() -> str:
    """Modes de confort gérés par le shell Caelestia."""
    night = _state_file("nightlight.json")
    focus = _state_file("focus.json")
    scheme = _state_file("scheme.json")
    parts = []
    if night:
        temp = f" ({night.get('temperature')} K)" if night.get("enabled") else ""
        parts.append(f"lumière nocturne {_onoff(bool(night.get('enabled')))}{temp}")
    if focus:
        mode = focus.get("mode") or ""
        parts.append(f"concentration : {mode if mode else 'désactivée'}")
    if scheme:
        parts.append(f"apparence {scheme.get('mode', '?')}")
    return ("Confort : " + " ; ".join(parts) + ".") if parts else f"Confort : {_UNKNOWN}."


def probe_hypr() -> str:
    if not kit.have("hyprctl"):
        return f"Hyprland : {_UNKNOWN}."
    active = kit.hypr_activewindow() or {}
    clients = [c for c in kit.hypr_clients(default=[]) if c.get("mapped", True)]
    monitors = kit.hypr_monitors() or []
    workspaces = kit.hypr_workspaces() or []
    focus = "aucune"
    if active.get("class"):
        focus = f"{active.get('class')} — « {str(active.get('title', ''))[:80]} »"
    mon = ", ".join(
        f"{m.get('name')} {m.get('width')}x{m.get('height')}@{round(m.get('refreshRate', 0))} Hz"
        f"{' (actif)' if m.get('focused') else ''}" for m in monitors) or _UNKNOWN
    current = next((m.get("activeWorkspace", {}).get("name") for m in monitors if m.get("focused")), "?")
    per_ws: dict[str, list[str]] = {}
    for c in clients:
        ws = str((c.get("workspace") or {}).get("name", "?"))
        per_ws.setdefault(ws, []).append(str(c.get("class") or "?"))
    layout = " ; ".join(f"{ws}: {', '.join(apps)}" for ws, apps in sorted(per_ws.items()))
    return (f"Bureau : espace {current}, fenêtre active {focus}. "
            f"{len(clients)} fenêtre(s) sur {len(workspaces)} espace(s) [{layout or 'vide'}]. "
            f"Écrans : {mon}.")


def probe_media() -> str:
    raw = _out(["playerctl", "-a", "metadata", "--format",
                "{{playerName}}\t{{status}}\t{{artist}}\t{{title}}"])
    rows = []
    for line in raw.splitlines():
        player, status, artist, title = (line.split("\t") + ["", "", "", ""])[:4]
        track = " — ".join(p for p in (artist, title) if p) or "titre inconnu"
        rows.append(f"{player} : {track} ({status.lower()})")
    return "Média : " + ("; ".join(rows) if rows else "aucun lecteur actif") + "."


def probe_resources() -> str:
    try:
        import psutil
    except ImportError:
        return f"Ressources : {_UNKNOWN}."
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage(str(Path.home()))
    temps = ""
    try:
        readings = [t.current for group in psutil.sensors_temperatures().values() for t in group]
        if readings:
            temps = f", température max {max(readings):.0f} °C"
    except (AttributeError, OSError):
        pass
    load = psutil.getloadavg()[0]
    uptime = (time.time() - psutil.boot_time()) / 3600
    return (f"Ressources : CPU {psutil.cpu_percent(interval=0.2):.0f} % (charge {load:.1f}), "
            f"RAM {mem.percent:.0f} % ({mem.used / 2**30:.1f}/{mem.total / 2**30:.1f} Gio), "
            f"disque {disk.percent:.0f} % ({disk.free / 2**30:.0f} Gio libres){temps}, "
            f"allumé depuis {uptime:.1f} h.")


def probe_airplane() -> str:
    states = _rfkill()
    if not states:
        return f"Mode avion : {_UNKNOWN}."
    return "Mode avion : " + ("ACTIF (toutes les radios coupées)" if not any(states.values()) else "inactif") + "."


def probe_shell() -> str:
    running = bool(_out(["pgrep", "-f", "qs -c caelestia"]))
    return f"Shell Caelestia : {'en cours' if running else 'ARRÊTÉ'}."


# ── Rubriques ───────────────────────────────────────────────────────────────

_PROBES: dict[str, Callable[[], str]] = {
    "bluetooth": probe_bluetooth,
    "wifi": probe_wifi,
    "reseau": probe_network,
    "audio": probe_audio,
    "luminosite": probe_brightness,
    "energie": probe_power,
    "confort": probe_comfort,
    "bureau": probe_hypr,
    "media": probe_media,
    "ressources": probe_resources,
    "avion": probe_airplane,
    "shell": probe_shell,
}

_ALIASES: dict[str, str] = {
    "bt": "bluetooth", "casque": "bluetooth", "ecouteurs": "bluetooth", "airpods": "bluetooth",
    "wi-fi": "wifi", "wlan": "wifi", "vpn": "reseau", "network": "reseau", "internet": "reseau",
    "connexion": "reseau", "ethernet": "reseau",
    "son": "audio", "volume": "audio", "micro": "audio", "microphone": "audio", "mute": "audio",
    "sound": "audio", "brightness": "luminosite", "ecran": "luminosite", "screen": "luminosite",
    "batterie": "energie", "battery": "energie", "power": "energie", "veille": "energie",
    "charge": "energie", "profil": "energie",
    "nuit": "confort", "night": "confort", "nightlight": "confort", "concentration": "confort",
    "dnd": "confort", "focus": "confort", "sombre": "confort", "theme": "confort",
    "hyprland": "bureau", "fenetre": "bureau", "fenetres": "bureau", "workspace": "bureau",
    "moniteur": "bureau", "desktop": "bureau", "apps": "bureau",
    "musique": "media", "spotify": "media", "lecture": "media", "player": "media",
    "cpu": "ressources", "ram": "ressources", "memoire": "ressources", "disque": "ressources",
    "temperature": "ressources", "systeme": "ressources", "gpu": "ressources",
    "airplane": "avion", "caelestia": "shell", "quickshell": "shell",
}

# Lignes d'un relevé complet : ce qui répond à « comment va ma machine ? ».
_ORDER = ("bluetooth", "wifi", "reseau", "audio", "luminosite", "energie", "confort",
          "bureau", "media", "ressources", "avion", "shell")


def _fold(text: str) -> str:
    import unicodedata
    lowered = unicodedata.normalize("NFKD", str(text or "").casefold())
    return "".join(c for c in lowered if not unicodedata.combining(c)).strip()


def resolve_topics(topic: str) -> list[str]:
    """Rubriques visées par une demande libre ; toutes si rien ne correspond."""
    folded = _fold(topic)
    if not folded or folded in {"all", "tout", "complet", "full", "etat", "status"}:
        return list(_ORDER)
    found: list[str] = []
    for token in re.split(r"[\s,;/+]+", folded):
        key = _ALIASES.get(token, token)
        if key in _PROBES and key not in found:
            found.append(key)
    return found or list(_ORDER)


def system_state(topic: str = "all") -> str:
    """Relevé réel de la machine pour les rubriques demandées (en parallèle)."""
    topics = resolve_topics(topic)

    def _one(name: str) -> str:
        try:
            return _PROBES[name]()
        except Exception as exc:  # une sonde ne doit jamais faire tomber le relevé
            return f"{name} : {_UNKNOWN} ({type(exc).__name__})."

    with ThreadPoolExecutor(max_workers=min(6, len(topics))) as pool:
        lines = list(pool.map(_one, topics))
    return "\n".join(lines)
