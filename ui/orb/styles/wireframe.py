"""GÉODÉSIQUE — sphère filaire géodésique en rotation.

À RÉALISER (Codex) — voir ``ui/orb/styles/README.md`` et l'exemple ``pulse.py``.
Une fois fini : ``ready=True`` pour « wireframe » dans ``ui/orb/registry.py``.

Vision :
* icosphère subdivisée une ou deux fois (sommets et arêtes calculés une fois
  dans ``__init__``), projetée en perspective à chaque image ;
* arêtes arrière atténuées, arêtes avant nettes ; les sommets s'écartent du
  centre selon les bandes FFT (``self.bands``), comme une membrane ;
* dessin : toutes les arêtes d'une même opacité dans un seul ``drawLines``
  (liste de QLineF), pas un ``drawLine`` par arête.
"""
from __future__ import annotations

from PyQt6.QtGui import QPainter

from ui.orb.base import BaseOrb


class WireframeOrb(BaseOrb):
    def __init__(self, face_path: str = "", assistant_name: str = "ANO-GPT", parent=None):
        super().__init__(face_path, assistant_name, parent)

    def advance(self, dt: float, t: float) -> None:
        pass

    def paint_orb(self, p: QPainter, cx: float, cy: float, radius: float, t: float) -> None:
        raise NotImplementedError("style « wireframe » pas encore réalisé")
