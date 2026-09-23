"""Catalogue des styles d'orbe — le pendant des fonds de ``background/``.

Chaque style est déclaré par un ``OrbSpec`` : son module n'est importé qu'au
moment où il devient l'orbe actif. Un style non choisi ne coûte donc ni
import, ni widget, ni minuterie, ni contexte GPU.

Ajouter un style : écrire ``ui/orb/styles/<id>.py`` (voir
``ui/orb/styles/README.md``), puis une ligne ``register(OrbSpec(...))`` ici.
"""
from __future__ import annotations

import importlib
import importlib.util
import os
from dataclasses import dataclass
from typing import Callable

CONFIG_KEY = "orb_style"
DEFAULT_ORB = "arc"


@dataclass(frozen=True)
class OrbSpec:
    id: str
    label: str
    tagline: str
    # « module:attribut » ; l'attribut est une classe ou une usine appelée
    # avec (face_path, assistant_name, parent) et qui rend un QWidget.
    target: str
    # « qpainter » (CPU, thread Qt) ou « gpu » (QOpenGLWidget).
    engine: str = "qpainter"
    # False : style en chantier, invisible dans la galerie et jamais chargé.
    ready: bool = True
    # Couleur de la vignette dans la galerie.
    swatch: str = "#00d4ff"


_SPECS: dict[str, OrbSpec] = {}


def register(spec: OrbSpec) -> OrbSpec:
    if spec.engine not in ("qpainter", "gpu"):
        raise ValueError(f"moteur d'orbe inconnu : {spec.engine!r}")
    if ":" not in spec.target:
        raise ValueError(f"cible d'orbe invalide : {spec.target!r}")
    _SPECS[spec.id] = spec
    return spec


def get(style_id: str) -> OrbSpec | None:
    return _SPECS.get(str(style_id or "").strip().lower())


def all_specs() -> list[OrbSpec]:
    return list(_SPECS.values())


def _gpu_available() -> bool:
    try:
        return importlib.util.find_spec("PyQt6.QtOpenGLWidgets") is not None
    except (ImportError, ValueError):
        return False


def is_usable(spec: OrbSpec | None) -> bool:
    if spec is None or not spec.ready:
        return False
    return spec.engine != "gpu" or _gpu_available()


def selectable_specs() -> list[OrbSpec]:
    """Styles proposés dans la galerie, orbe principal en tête."""
    return [spec for spec in _SPECS.values() if is_usable(spec)]


def resolve(configured: str | None) -> str:
    """Style à charger au démarrage.

    Priorité : choix enregistré s'il est utilisable, puis l'ancien drapeau
    ``ANOGPT_GLSL_ORB=1`` (compatibilité), puis l'orbe principal.
    """
    spec = get(configured or "")
    if is_usable(spec):
        return spec.id
    if os.environ.get("ANOGPT_GLSL_ORB", "").strip().lower() in {"1", "true", "yes", "on"}:
        if is_usable(get("glsl")):
            return "glsl"
    return DEFAULT_ORB


def load_factory(spec: OrbSpec) -> Callable:
    module_name, _, attr = spec.target.partition(":")
    module = importlib.import_module(module_name)
    factory = getattr(module, attr, None)
    if not callable(factory):
        raise ImportError(f"{spec.target} introuvable ou non appelable")
    return factory


def create(style_id: str, face_path: str, assistant_name: str, parent=None):
    """Instancie un style ; lève une exception si le style ne peut pas naître."""
    spec = get(style_id)
    if spec is None:
        raise KeyError(f"style d'orbe inconnu : {style_id!r}")
    if not is_usable(spec):
        raise RuntimeError(f"style d'orbe indisponible : {spec.id}")
    widget = load_factory(spec)(face_path, assistant_name, parent)
    if widget is None:
        raise RuntimeError(f"l'usine {spec.target} n'a rien rendu")
    return widget


# ── Catalogue ────────────────────────────────────────────────────────────────
# L'ordre de déclaration est l'ordre de la galerie.

register(OrbSpec(
    id="arc", label="ARC CORE",
    tagline="Nuage de particules 3D — l'orbe principal",
    target="ui.orb.arc_core:HudCanvas", swatch="#00d4ff",
))
register(OrbSpec(
    id="glsl", label="HOLO GPU",
    tagline="Sphère plasma en shader, calcul sur la carte graphique",
    target="ui.orb.glsl_orb:GLSLOrbWidget", engine="gpu", swatch="#7a5cff",
))
register(OrbSpec(
    id="pulse", label="PULSE",
    tagline="Anneaux concentriques minimalistes — le plus léger",
    target="ui.orb.styles.pulse:PulseOrb", swatch="#00ff9d",
))
# Styles en chantier (ready=False) : passer à True une fois terminés.
register(OrbSpec(
    id="nebula", label="NEBULA",
    tagline="Nébuleuse de poussière lumineuse en spirale",
    target="ui.orb.styles.nebula:NebulaOrb", ready=False, swatch="#ff4fd8",
))
register(OrbSpec(
    id="wireframe", label="GÉODÉSIQUE",
    tagline="Sphère filaire géodésique en rotation",
    target="ui.orb.styles.wireframe:WireframeOrb", ready=False, swatch="#ffb200",
))
register(OrbSpec(
    id="radial", label="SPECTRE",
    tagline="Onde radiale pilotée par le spectre audio",
    target="ui.orb.styles.radial:RadialOrb", ready=False, swatch="#ff3355",
))
