"""NEBULA — nébuleuse de poussière lumineuse en spirale.

À RÉALISER (Codex) — voir ``ui/orb/styles/README.md`` et l'exemple ``pulse.py``.
Une fois fini : ``ready=True`` pour « nebula » dans ``ui/orb/registry.py``.

Vision :
* 300 à 600 grains de poussière sur 2 ou 3 bras de spirale logarithmique,
  rotation différentielle (le centre tourne plus vite que les bords) ;
* le volume gonfle les bras et éclaire le cœur ; ``thinking`` resserre la
  spirale, ``acting`` l'accélère, ``error`` la fait vaciller ;
* dessin : un sprite radial pré-rendu (QPixmap, recréé dans
  ``on_palette_changed`` et au redimensionnement) posé avec ``drawPixmap``,
  mode de composition ``Plus`` pour l'additif — jamais un QRadialGradient par
  grain, c'est trop cher sur 2 cœurs.
"""
from __future__ import annotations

from PyQt6.QtGui import QPainter

from ui.orb.base import BaseOrb


class NebulaOrb(BaseOrb):
    def __init__(self, face_path: str = "", assistant_name: str = "ANO-GPT", parent=None):
        super().__init__(face_path, assistant_name, parent)

    def advance(self, dt: float, t: float) -> None:
        pass

    def paint_orb(self, p: QPainter, cx: float, cy: float, radius: float, t: float) -> None:
        raise NotImplementedError("style « nebula » pas encore réalisé")
