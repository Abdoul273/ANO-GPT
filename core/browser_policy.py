"""Choix permanent du navigateur d'ANO-GPT : Google Chrome."""
from __future__ import annotations

import os
import shutil
import subprocess
import webbrowser

WEB_DOCUMENT_SUFFIXES = frozenset({".html", ".htm", ".xhtml", ".mhtml", ".mht"})

BROWSER_ALIASES = frozenset({
    "browser", "navigateur", "internet", "web", "chrome", "google chrome",
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
    "firefox", "mozilla firefox", "firefox-esr", "firefox-developer-edition",
    "edge", "msedge", "brave", "brave-browser", "opera", "operagx", "vivaldi", "safari",
})


def chrome_binary() -> str | None:
    for name in ("google-chrome-stable", "google-chrome", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


def open_chrome(url: str = "", *, env: dict | None = None) -> bool:
    """Lance Chrome sans shell, avec son profil normal, sans autre navigateur en repli."""
    executable = chrome_binary()
    if not executable:
        return False
    try:
        subprocess.Popen(
            [executable, url] if url else [executable],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, env=env,
        )
        return True
    except OSError:
        return False


class ChromeBrowser(webbrowser.BaseBrowser):
    def open(self, url, new=0, autoraise=True):
        if not open_chrome(url):
            raise webbrowser.Error("Impossible de lancer Google Chrome. Aucun autre navigateur n’a été ouvert.")
        return True


def register_chrome() -> str:
    name = "anogpt-chrome"
    webbrowser.register(name, None, ChromeBrowser(), preferred=True)
    return name


def prefer_chrome() -> None:
    """Couvre aussi les bibliothèques qui utilisent webbrowser sans contrôleur explicite."""
    os.environ["BROWSER"] = "google-chrome-stable"
    register_chrome()
