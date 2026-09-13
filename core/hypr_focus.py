"""core/hypr_focus.py — Savoir, en direct, si l'utilisateur est sur ANO-GPT.

Hyprland range les fenêtres en mosaïque : rien ne se minimise, on change de
bureau. « L'assistant n'est plus à l'écran » n'a donc rien à voir avec une
fenêtre réduite — c'est simplement que la fenêtre focalisée est une autre, ou
que le bureau affiché n'est pas le sien.

Hyprland publie ces changements sur une socket d'évènements (`.socket2.sock`).
On l'écoute plutôt que d'interroger `hyprctl` en boucle : c'est instantané et
ça ne coûte rien entre deux évènements — sur deux cœurs partagés avec la voix,
un sondage à la seconde se paierait pour rien.

Le module ne dépend ni de Qt ni du reste d'ANO-GPT : il appelle un callback
depuis son fil, à charge de l'appelant de repasser dans son thread d'interface.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

# Les évènements qui changent ce qui est réellement sous les yeux. Tout le
# reste (ouverture de calque, changement de disposition, plein écran…) est
# ignoré : réagir à tout ferait clignoter la bulle.
_WATCHED = (
    "activewindow>>",      # class,title de la fenêtre focalisée
    "closewindow>>",       # plus rien de focalisé après une fermeture
    "workspace>>",         # changement de bureau
    "focusedmon>>",        # changement d'écran
)


def socket_path() -> Path | None:
    """Chemin de la socket d'évènements de l'instance Hyprland courante."""
    signature = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    if not signature:
        return None
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    candidates = [
        Path(runtime) / "hypr" / signature / ".socket2.sock" if runtime else None,
        Path("/tmp/hypr") / signature / ".socket2.sock",   # Hyprland < 0.40
    ]
    for candidate in candidates:
        if candidate is not None and candidate.exists():
            return candidate
    return None


def available() -> bool:
    return socket_path() is not None


def active_window() -> tuple[str, str] | None:
    """Retourne ``(classe, titre)`` depuis l'état réel de Hyprland.

    Les évènements ``workspace`` et ``activewindow`` ne sont pas garantis dans
    le même ordre. Relire l'état courant lors des transitions évite donc qu'un
    ancien évènement de bureau laisse la bulle affichée au-dessus d'ANO-GPT.
    ``None`` signifie que la lecture a échoué ; une fenêtre vide est, elle,
    représentée par ``("", "")``.
    """
    try:
        result = subprocess.run(
            ["hyprctl", "-j", "activewindow"],
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
        if result.returncode != 0:
            return None
        payload = json.loads(result.stdout or "{}")
        if not isinstance(payload, dict):
            return None
        return str(payload.get("class") or ""), str(payload.get("title") or "")
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return None


class FocusWatcher:
    """Suit la fenêtre focalisée et prévient quand ANO-GPT perd (ou reprend) l'écran.

    `on_change(mine: bool)` est appelé uniquement sur un vrai changement, avec
    `True` quand la fenêtre focalisée appartient à ANO-GPT.
    """

    def __init__(
        self,
        on_change: Callable[[bool], None],
        *,
        app_classes: tuple[str, ...] = ("jarvis-dashboard",),
        ignore_titles: tuple[str, ...] = (),
    ) -> None:
        self._on_change = on_change
        self._classes = tuple(c.casefold() for c in app_classes)
        self._ignore_titles = tuple(t.casefold() for t in ignore_titles)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._mine: bool | None = None

    # ── cycle de vie ────────────────────────────────────────────────────────

    def start(self) -> bool:
        if self._thread is not None or not available():
            return self._thread is not None
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="hypr-focus", daemon=True
        )
        self._thread.start()
        from core.thread_pool import register_shutdown_hook
        register_shutdown_hook(f"hypr-focus-{id(self)}", self.stop)
        return True

    def stop(self) -> None:
        self._stop.set()
        self._thread = None

    # ── lecture des évènements ──────────────────────────────────────────────

    def _run(self) -> None:
        while not self._stop.is_set():
            path = socket_path()
            if path is None:
                time.sleep(2.0)
                continue
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                    sock.settimeout(1.0)
                    sock.connect(str(path))
                    # La socket ne rejoue pas le dernier évènement à la
                    # connexion. Sans cet instantané initial, l'état pouvait
                    # rester faux jusqu'au prochain changement de fenêtre.
                    self.refresh()
                    self._pump(sock)
            except Exception:
                # Hyprland redémarre, la socket disparaît : on repasse plus
                # tard plutôt que d'abandonner la bulle pour le reste de la
                # session.
                time.sleep(2.0)

    def _pump(self, sock: socket.socket) -> None:
        buffer = b""
        while not self._stop.is_set():
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                return                      # socket fermée : on rouvrira
            buffer += chunk
            # Une lecture peut couper une ligne en deux : le reste est gardé
            # pour le tour suivant, sinon un évènement sur deux est perdu.
            *lines, buffer = buffer.split(b"\n")
            for line in lines:
                self._handle(line.decode("utf-8", "replace"))

    def _handle(self, line: str) -> None:
        if not line.startswith(_WATCHED):
            return
        event, _, payload = line.partition(">>")
        if event == "activewindow":
            window_class, _, title = payload.partition(",")
            mine = self._is_ours(window_class, title)
        else:
            # Hyprland 0.56 peut publier `activewindow` avant `workspace`. Si
            # l'on forçait False ici, ce second évènement écraserait le bon
            # état et la bulle resterait au-dessus de la fenêtre principale.
            self.refresh()
            return
        self._emit(mine)

    def refresh(self) -> None:
        """Resynchronise le watcher avec la fenêtre réellement active."""
        current = active_window()
        if current is None:
            # Une panne ponctuelle de hyprctl ne doit pas inventer une perte
            # de focus et faire apparaître la bulle dans ANO-GPT.
            return
        self._emit(self._is_ours(*current))

    def _is_ours(self, window_class: str, title: str) -> bool:
        if window_class.casefold() not in self._classes:
            return False
        # La bulle compagnon partage l'app_id de l'application : sans cette
        # exception, elle se prendrait elle-même pour la fenêtre principale et
        # se cacherait au premier clic.
        return title.casefold() not in self._ignore_titles

    def _emit(self, mine: bool) -> None:
        if mine == self._mine:
            return
        self._mine = mine
        try:
            self._on_change(mine)
        except Exception:
            pass
