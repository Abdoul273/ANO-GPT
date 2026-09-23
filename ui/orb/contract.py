"""Contrat commun à tous les styles d'orbe.

Le reste de l'interface ne parle qu'à ``OrbHost`` (``window.hud``). L'hôte
relaie ce contrat au seul orbe actif et lui rejoue l'état courant quand on
change de style. Un style n'a donc rien d'autre à connaître que ce fichier.

Attributs lus en direct par ``MiniOrbOverlay`` et ``CompanionOrb`` (ils
dessinent leur propre mini-réacteur à partir de ces valeurs) : ``_ws``,
``_volume``, ``_target_vol``, ``_energy`` et ``_PALETTES``. ``BaseOrb`` les
maintient ; un style écrit sans ``BaseOrb`` doit les fournir lui-même.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Protocol, runtime_checkable

# États visuels, dans l'ordre de priorité d'affichage.
ORB_STATES: tuple[str, ...] = (
    "idle", "listening", "thinking", "speaking", "acting", "error",
)

# États remontés par la boucle vocale → état visuel.
STATE_TO_WS: dict[str, str] = {
    "SPEAKING": "speaking", "LISTENING": "listening",
    "THINKING": "thinking", "PROCESSING": "thinking",
    "ACTING": "acting", "EXECUTING": "acting", "RUNNING": "acting",
    "ERROR": "error",
}


def visual_state(state: str, speaking: bool, muted: bool) -> str:
    """État visuel unique, identique pour tous les styles."""
    if muted:
        return "idle"
    if speaking:
        return "speaking"
    return STATE_TO_WS.get(str(state or "").upper(), "idle")


@dataclass
class OrbSnapshot:
    """Tout ce qu'un orbe neuf doit recevoir pour reprendre sans saut.

    L'hôte le tient à jour à chaque appel du contrat ; au changement de style
    il le rejoue sur le nouvel orbe avant de l'afficher.
    """

    assistant_name: str = "ANO-GPT"
    state: str = "IDLE"
    speaking: bool = False
    muted: bool = False
    volume: float = 0.0
    low_power: bool = False
    background_active: bool = False
    continuous_vision: bool = False
    accent_hex: str = ""
    custom_palette: dict | None = field(default=None)


@runtime_checkable
class OrbSurface(Protocol):
    """Ce que l'hôte appelle sur l'orbe actif.

    Obligatoire : tout ce qui est déclaré ici. Facultatif (appelé seulement
    s'il existe) : ``set_audio_bands``, ``show_clock_particles``,
    ``set_assistant_name``, ``shutdown``.
    """

    muted: bool
    speaking: bool
    state: str

    def set_volume(self, v: float) -> None: ...
    def set_low_power(self, low: bool) -> None: ...
    def set_background_image_active(self, active: bool) -> None: ...
    def set_accent_color(self, accent_hex: str, custom_palette: dict | None = None) -> None: ...
    def set_continuous_vision(self, active: bool) -> None: ...
    def show_gesture_feedback(
        self, icon: str, label: str = "", value: float = 0.0, duration: float = 1.6,
    ) -> None: ...


def clamp_bands(bands: Iterable[float], count: int = 8) -> list[float]:
    """Bandes FFT nettoyées : ``count`` valeurs finies dans [0, 1]."""
    out: list[float] = []
    for value in bands:
        try:
            f = float(value)
        except (TypeError, ValueError):
            f = 0.0
        out.append(max(0.0, min(1.0, f)) if f == f and abs(f) != float("inf") else 0.0)
        if len(out) == count:
            break
    out.extend([0.0] * (count - len(out)))
    return out
