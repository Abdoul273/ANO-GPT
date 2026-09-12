"""Tests de la détection de lancement caché (§F.1) dans actions/open_app.py."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from actions.open_app import _HIDDEN_RE


def test_detects_various_hidden_phrasings():
    for phrase in [
        "lance kitty en arrière-plan",
        "ouvre firefox caché",
        "lance discord sans l'afficher",
        "démarre spotify discrètement",
        "ouvre-le en cachée",
    ]:
        assert _HIDDEN_RE.search(phrase), phrase


def test_normal_launch_not_flagged_hidden():
    assert not _HIDDEN_RE.search("lance kitty")
    assert not _HIDDEN_RE.search("ouvre firefox sur le bureau 3")
