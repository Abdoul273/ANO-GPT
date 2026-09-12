"""La carte lecteur ne doit pas clignoter après l'arrêt de la musique.

Deux bugs se combinaient :

1. `FloatingPanel.fade_out()` branche `_anim.finished` sur `hide()`. `fade_in()`
   ne débranchait pas ce signal : le panneau réapparaissait, montait jusqu'à
   l'opacité 1, puis se cachait tout seul en fin d'animation.

2. mpv tourne en `--idle=yes`. Après un `stop` il reste vivant sans fichier, et
   sa propriété `pause` vaut toujours False — la boucle de surveillance en
   déduisait « playing » une demi-seconde après chaque arrêt, ce qui relançait
   le fade_in du point 1. D'où un clignotement sans fin.
"""

import os

import pytest

from core.player_ipc import derive_state

# Doit être posé avant toute création de QApplication.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


# ── Bug 2 : l'état déduit des propriétés mpv ─────────────────────────────────

def test_apres_stop_letat_est_arrete():
    """Le cas exact mesuré sur un vrai mpv : idle-active=True, pause=False."""
    assert derive_state(idle=True, paused=False, current="playing") == "stopped"


def test_lecture_en_cours():
    assert derive_state(idle=False, paused=False, current="stopped") == "playing"


def test_pause():
    assert derive_state(idle=False, paused=True, current="playing") == "paused"


def test_proprietes_illisibles_conservent_letat():
    """Une lecture ratée ne doit pas faire sauter la carte."""
    assert derive_state(idle=None, paused=None, current="playing") == "playing"


# ── Bug 1 : le panneau qui se cache tout seul ────────────────────────────────

@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _run_animation(qapp, panel):
    """Laisse l'animation aller jusqu'à son terme, signaux compris."""
    from PyQt6.QtCore import QEventLoop, QTimer
    loop = QEventLoop()
    QTimer.singleShot(panel._anim.duration() + 150, loop.quit)
    loop.exec()


def test_le_panneau_reste_visible_apres_un_retour(qapp):
    """Le cœur du bug : masquer puis réafficher devait laisser le panneau là."""
    import ui
    panel = ui.FloatingPanel()

    panel.fade_in()
    _run_animation(qapp, panel)
    assert panel.isVisible() is True

    panel.fade_out()
    _run_animation(qapp, panel)
    assert panel.isVisible() is False, "fade_out doit bien cacher"

    panel.fade_in()
    _run_animation(qapp, panel)
    assert panel.isVisible() is True, (
        "le panneau s'est recaché tout seul : le signal finished->hide posé par "
        "fade_out n'a pas été débranché"
    )


def test_pas_de_clignotement_sur_plusieurs_cycles(qapp):
    """Reproduit ce que voit l'utilisateur : des retours répétés."""
    import ui
    panel = ui.FloatingPanel()

    panel.fade_out()
    _run_animation(qapp, panel)

    for cycle in range(3):
        panel.fade_in()
        _run_animation(qapp, panel)
        assert panel.isVisible() is True, f"disparu au cycle {cycle}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
