"""SPECTRE — onde radiale pilotée par le spectre audio.

À RÉALISER (Codex) — voir ``ui/orb/styles/README.md`` et l'exemple ``pulse.py``.
Une fois fini : ``ready=True`` pour « radial » dans ``ui/orb/registry.py``.

Vision :
* 64 à 96 barres rayonnantes autour d'un cercle ; leur longueur suit les 8
  bandes FFT (``self.bands``) interpolées sur le tour, symétrie miroir ;
* ``speaking`` : barres vers l'extérieur ; ``listening`` : vers l'intérieur ;
  ``idle`` : respiration lente ;
* ``ui/orb/radial_waveform.py`` contient déjà un moteur FFT et un rendu radial
  complets : s'en inspirer, mais ne rien y instancier de lourd (aucune
  minuterie propre, tout passe par ``advance``/``paint_orb``).
"""
from __future__ import annotations

from PyQt6.QtGui import QPainter

from ui.orb.base import BaseOrb


class RadialOrb(BaseOrb):
    def __init__(self, face_path: str = "", assistant_name: str = "ANO-GPT", parent=None):
        super().__init__(face_path, assistant_name, parent)

    def advance(self, dt: float, t: float) -> None:
        pass

    def paint_orb(self, p: QPainter, cx: float, cy: float, radius: float, t: float) -> None:
        raise NotImplementedError("style « radial » pas encore réalisé")
