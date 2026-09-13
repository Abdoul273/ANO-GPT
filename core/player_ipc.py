#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
core/player_ipc.py — Client IPC MPV Headless (aucun affichage, aucune fenêtre).
Permet le contrôle total de la lecture audio locale et streaming (YouTube via yt-dlp)
sans jamais ouvrir de fenêtre vidéo/graphique.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Any

_SOCKET_PATH = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "anogpt_mpv.sock"


def _socket_alive(timeout: float = 1.0) -> bool:
    """Y a-t-il un mpv vivant au bout du socket ?

    Un fichier de socket qui traîne ne prouve rien : mpv peut avoir été tué
    sans le nettoyer. Seule une connexion réussie le prouve.
    """
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(str(_SOCKET_PATH))
        s.close()
        return True
    except Exception:
        return False


def derive_state(idle: Any, paused: Any, current: str) -> str:
    """État de lecture à partir des propriétés mpv.

    mpv tourne en `--idle=yes` : après un `stop` il reste vivant, simplement
    sans fichier chargé. `pause` reste alors parfaitement lisible et vaut False.
    En déduire « playing » — ce que faisait l'ancienne version — ramenait la
    carte lecteur à l'écran une demi-seconde après chaque arrêt, et comme elle
    se recachait en fin d'animation, elle clignotait indéfiniment.

    Vérifié sur un vrai mpv :
        au démarrage    idle-active=True   pause=False
        fichier chargé  idle-active=False  pause=False
        après stop      idle-active=True   pause=False
    """
    if idle:
        return "stopped"
    if paused is not None:
        return "paused" if paused else "playing"
    return current


class MPVPlayerIPC:
    """Gestionnaire de processus et client IPC socket pour mpv headless."""

    def __init__(self, callback: Optional[Callable[[Dict[str, Any]], None]] = None):
        self._callback = callback
        self._proc: Optional[subprocess.Popen] = None
        self._sock: Optional[socket.socket] = None
        self._lock = threading.Lock()
        self._monitor_thread: Optional[threading.Thread] = None
        self._running = False
        # ``quit`` coupe les boucles de surveillance et ferme mpv : à
        # l'extinction, aucun thread ne doit rester suspendu sur son socket.
        from core.thread_pool import register_shutdown_hook
        register_shutdown_hook(f"mpv-ipc-{id(self)}", self.quit)

        self._current_track = {
            "title": "Aucune lecture",
            "artist": "JARVIS Media",
            "thumbnail": "",
            "duration": 0.0,
            "pos": 0.0,
            "volume": 80,
            "state": "stopped",  # playing | paused | stopped
            "shuffle": False,
        }
        self._playlist: List[Dict[str, str]] = []
        self._external_player = ""  # lecteur MPRIS, par exemple Spotify
        self._external_monitor_thread: Optional[threading.Thread] = None

    def watch_mpris_player(self, player_name: str) -> bool:
        """Affiche un lecteur MPRIS externe dans la carte musique ANO-GPT."""
        if not shutil.which("playerctl"):
            return False
        self._external_player = str(player_name or "").strip()
        if not self._external_player:
            return False
        if self._external_monitor_thread is None or not self._external_monitor_thread.is_alive():
            self._external_monitor_thread = threading.Thread(
                target=self._external_monitor_loop, daemon=True,
            )
            self._external_monitor_thread.start()
        return True

    def _external_command(self, *args: str) -> str:
        try:
            result = subprocess.run(
                ["playerctl", "--player", self._external_player, *args],
                capture_output=True, text=True, timeout=0.7,
            )
            return result.stdout.strip() if result.returncode == 0 else ""
        except Exception:
            return ""

    def _external_monitor_loop(self) -> None:
        """Interroge MPRIS hors du thread Qt ; la carte reste fluide."""
        while self._external_player:
            try:
                status = self._external_command("status").casefold()
                metadata = self._external_command(
                    "metadata", "--format", "{{title}}\x1f{{artist}}\x1f{{mpris:length}}\x1f{{mpris:artUrl}}",
                ).split("\x1f")
                if status and metadata and metadata[0]:
                    duration = 0.0
                    try:
                        duration = float(metadata[2]) / 1_000_000
                    except (IndexError, ValueError):
                        pass
                    try:
                        pos = float(self._external_command("position") or 0)
                    except ValueError:
                        pos = 0.0
                    self._current_track.update({
                        "title": metadata[0], "artist": metadata[1] if len(metadata) > 1 else "Spotify",
                        "duration": duration, "pos": pos,
                        "thumbnail": metadata[3] if len(metadata) > 3 else "",
                        "state": "playing" if status == "playing" else "paused",
                        "source": self._external_player,
                    })
                    self._notify()
                elif self._current_track.get("source") == self._external_player and self._current_track.get("state") != "stopped":
                    # Spotify fermé (ou MPRIS absent) : ne jamais laisser une
                    # carte « pause » fantôme à l'écran.
                    self._current_track.update({"state": "stopped", "pos": 0.0})
                    self._notify()
            except Exception:
                pass
            time.sleep(1.0)

    def _ensure_mpv_running(self) -> bool:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None and _SOCKET_PATH.exists():
                return True

            # Un mpv d'une exécution PRÉCÉDENTE de l'app peut être encore vivant :
            # `self._proc` est None dans ce processus-ci, mais son socket répond
            # toujours. L'ancienne version supprimait ce socket et lançait un
            # nouveau mpv — l'ancien restait alors orphelin pour toujours. Mesuré
            # sur la machine : deux mpv abandonnés, 2 h et 50 min d'ancienneté,
            # 393 Mo à eux deux. Chaque redémarrage de l'app en laissait un.
            if _SOCKET_PATH.exists():
                if _socket_alive():
                    self._running = True
                    if (self._monitor_thread is None
                            or not self._monitor_thread.is_alive()):
                        self._monitor_thread = threading.Thread(
                            target=self._monitor_loop, daemon=True)
                        self._monitor_thread.start()
                    return True
                try:
                    _SOCKET_PATH.unlink()   # socket mort : plus personne au bout
                except Exception:
                    pass

            cmd = [
                "mpv",
                "--no-video",
                "--force-window=no",
                "--idle=yes",
                f"--input-ipc-server={_SOCKET_PATH}",
                "--volume=80",
            ]
            try:
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                )
            except Exception as e:
                print(f"[MPV-IPC] Erreur de lancement mpv: {e}", file=sys.stderr)
                return False

            for _ in range(30):
                if _SOCKET_PATH.exists():
                    break
                time.sleep(0.05)

            if not _SOCKET_PATH.exists():
                return False

            self._running = True
            if self._monitor_thread is None or not self._monitor_thread.is_alive():
                self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
                self._monitor_thread.start()

            return True

    def _send_command(self, cmd: List[Any]) -> Optional[Any]:
        if not self._ensure_mpv_running():
            return None
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(2.0)
            s.connect(str(_SOCKET_PATH))
            req = json.dumps({"command": cmd}) + "\n"
            s.sendall(req.encode("utf-8"))
            resp_raw = s.recv(4096).decode("utf-8", errors="ignore")
            s.close()
            for line in resp_raw.splitlines():
                if line.strip():
                    try:
                        data = json.loads(line)
                        if "data" in data:
                            return data["data"]
                        if data.get("error") == "success":
                            return True
                    except Exception:
                        pass
        except Exception:
            pass
        return None

    def play(self, url_or_path: str, title: str = "", artist: str = "", thumbnail: str = "", thumbnail_bytes: bytes = b""):
        """Joue un fichier local ou une URL (audio direct ou YouTube)."""
        if not self._ensure_mpv_running():
            return

        # Le lecteur interne reprend la main : ses contrôles ne doivent pas
        # continuer à piloter Spotify après une lecture YouTube ou locale.
        self._external_player = ""

        self._current_track["title"] = title or Path(url_or_path).stem
        self._current_track["artist"] = artist or "Musique"
        self._current_track["thumbnail"] = thumbnail
        self._current_track["thumbnail_bytes"] = thumbnail_bytes
        self._current_track["state"] = "playing"

        self._send_command(["loadfile", url_or_path, "replace"])
        self._send_command(["set_property", "pause", False])
        self._notify()

        if thumbnail and not thumbnail_bytes and (thumbnail.startswith("http://") or thumbnail.startswith("https://")):
            def _fetch_thumb():
                try:
                    import urllib.request
                    req = urllib.request.Request(thumbnail, headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(req, timeout=3.5) as resp:
                        data = resp.read()
                        if data and len(data) < 2_500_000:
                            self._current_track["thumbnail_bytes"] = data
                            self._notify()
                except Exception:
                    pass
            threading.Thread(target=_fetch_thumb, daemon=True).start()

    def add_to_queue(self, url_or_path: str, title: str = "", artist: str = "", thumbnail: str = ""):
        """Ajoute un morceau à la playlist courante sans couper la lecture."""
        if not self._ensure_mpv_running():
            return
        self._playlist.append({
            "url": url_or_path, "title": title, "artist": artist, "thumbnail": thumbnail,
        })
        self._send_command(["loadfile", url_or_path, "append"])

    def pause(self):
        if self._external_player:
            self._external_command("pause")
            # Le poll MPRIS suivant arrive au plus tard une seconde après. Mettre
            # la carte à jour tout de suite évite qu'un garde audio pense encore
            # que Spotify joue et bloque la phrase réveillée.
            self._current_track["state"] = "paused"
            self._notify()
            return
        self._send_command(["set_property", "pause", True])
        self._current_track["state"] = "paused"
        self._notify()

    def resume(self):
        if self._external_player:
            self._external_command("play")
            self._current_track["state"] = "playing"
            self._notify()
            return
        self._send_command(["set_property", "pause", False])
        self._current_track["state"] = "playing"
        self._notify()

    def toggle_pause(self):
        if self._external_player:
            self._external_command("play-pause")
            return
        is_paused = self._send_command(["get_property", "pause"])
        if is_paused:
            self.resume()
        else:
            self.pause()

    def stop(self):
        if self._external_player:
            self._external_command("stop")
            return
        self._send_command(["stop"])
        self._current_track["state"] = "stopped"
        self._current_track["pos"] = 0.0
        self._notify()

    def next(self):
        if self._external_player:
            self._external_command("next")
            return
        self._send_command(["playlist-next"])

    def prev(self):
        if self._external_player:
            self._external_command("previous")
            return
        self._send_command(["playlist-prev"])

    def seek(self, seconds: float):
        if self._external_player:
            self._external_command("position", str(seconds))
            return
        self._send_command(["seek", seconds, "absolute"])

    def set_volume(self, vol: int) -> bool:
        vol = max(0, min(100, int(vol)))
        if self._external_player:
            # MPRIS/playerctl attend un volume 0..1 et ne touche qu'au
            # lecteur ciblé (notamment Spotify), jamais à PipeWire global.
            self._external_command("volume", f"{vol / 100:.2f}")
            if not self._external_command("status"):
                return False
        elif self._send_command(["set_property", "volume", vol]) is None:
            return False
        self._current_track["volume"] = vol
        self._notify()
        return True

    def set_shuffle(self, enable: bool):
        self._send_command(["playlist-shuffle"] if enable else ["playlist-unshuffle"])
        self._current_track["shuffle"] = enable
        self._notify()

    def get_status(self) -> Dict[str, Any]:
        return dict(self._current_track)

    def _notify(self):
        if self._callback:
            try:
                self._callback(dict(self._current_track))
            except Exception:
                pass

    def _monitor_loop(self):
        """Boucle de surveillance d'état (~2 fois par seconde)."""
        while self._running:
            try:
                if _SOCKET_PATH.exists():
                    pos = self._send_command(["get_property", "time-pos"])
                    dur = self._send_command(["get_property", "duration"])
                    paused = self._send_command(["get_property", "pause"])
                    vol = self._send_command(["get_property", "volume"])
                    title = self._send_command(["get_property", "media-title"])
                    # mpv tourne en --idle=yes : après un stop il reste vivant,
                    # sans fichier chargé. La propriété `pause` reste alors
                    # lisible et vaut False, ce qui faisait repasser l'état à
                    # « playing » une demi-seconde après chaque arrêt — la carte
                    # lecteur revenait donc toute seule. `idle-active` dit s'il
                    # y a réellement quelque chose à lire.
                    idle = self._send_command(["get_property", "idle-active"])

                    if pos is not None and isinstance(pos, (int, float)):
                        self._current_track["pos"] = float(pos)
                    if dur is not None and isinstance(dur, (int, float)):
                        self._current_track["duration"] = float(dur)
                    new_state = derive_state(idle, paused,
                                             self._current_track["state"])
                    self._current_track["state"] = new_state
                    if new_state == "stopped":
                        self._current_track["pos"] = 0.0
                    if vol is not None and isinstance(vol, (int, float)):
                        self._current_track["volume"] = int(vol)
                    if title and isinstance(title, str) and not self._current_track["title"]:
                        self._current_track["title"] = title

                    self._notify()
            except Exception:
                pass
            time.sleep(0.5)

    def quit(self):
        self._running = False
        if self._proc:
            try:
                self._send_command(["quit"])
                self._proc.terminate()
            except Exception:
                pass


_player_instance: Optional[MPVPlayerIPC] = None


def get_player(callback: Optional[Callable[[Dict[str, Any]], None]] = None) -> MPVPlayerIPC:
    global _player_instance
    if _player_instance is None:
        _player_instance = MPVPlayerIPC(callback=callback)
    elif callback is not None:
        _player_instance._callback = callback
    return _player_instance
