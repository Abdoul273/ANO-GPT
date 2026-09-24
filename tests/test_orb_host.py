"""Système d'orbes interchangeables : registre, hôte, base commune."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dataclasses import replace

import pytest
from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtWidgets import QApplication

from ui.orb import registry
from ui.orb.base import BaseOrb
from ui.orb.host import OrbHost


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def broken_style():
    registry.register(registry.OrbSpec(
        id="_casse", label="X", tagline="", target="ui.orb.styles.nexiste_pas:Rien",
    ))
    yield "_casse"
    registry._SPECS.pop("_casse", None)


def test_catalogue_propose_au_moins_trois_styles_et_cache_les_chantiers():
    ids = [spec.id for spec in registry.selectable_specs()]
    assert ids[0] == registry.DEFAULT_ORB
    assert {"arc", "pulse"} <= set(ids)
    for spec in registry.all_specs():
        if not spec.ready:
            assert spec.id not in ids
    assert len(registry.all_specs()) >= 6


def test_resolve_retombe_sur_l_orbe_principal(monkeypatch):
    monkeypatch.delenv("ANOGPT_GLSL_ORB", raising=False)
    assert registry.resolve("") == "arc"
    assert registry.resolve("inconnu") == "arc"
    assert registry.resolve("nebula") == "nebula"
    assert registry.resolve("PULSE") == "pulse"


def test_hote_bascule_et_detruit_l_ancien_orbe(qapp):
    host = OrbHost("", "ANO", "pulse")
    first = host.orb
    assert isinstance(first, BaseOrb)
    timers = first.findChildren(QTimer)
    assert any(t.isActive() for t in timers)

    host.state = "THINKING"
    host.muted = False
    host.set_accent_color("#00ff88")
    host.set_continuous_vision(True)
    assert host.set_style("arc")
    assert host.style_id == "arc"
    assert host.orb is not first
    # L'ancien ne consomme plus rien.
    assert not any(t.isActive() for t in timers)
    assert first.parent() is None
    # L'état a été rejoué sur le nouvel orbe.
    assert host.orb.state == "THINKING"
    assert host.orb.continuous_vision is True
    # Un seul orbe dans l'hôte.
    assert len([c for c in host.children() if c is host.orb or isinstance(c, BaseOrb)]) == 1


def test_style_en_echec_garde_l_orbe_actuel(qapp, broken_style):
    host = OrbHost("", "ANO", "pulse")
    current = host.orb
    assert host.set_style(broken_style) is False
    assert host.orb is current and host.style_id == "pulse"
    pending = replace(registry.get("nebula"), id="_chantier", ready=False)
    registry.register(pending)
    try:
        assert host.set_style("_chantier") is False
        assert host.orb is current
    finally:
        registry._SPECS.pop("_chantier", None)


def test_demarrage_sur_style_casse_replie_sur_le_principal(qapp, broken_style):
    host = OrbHost("", "ANO", broken_style)
    assert host.style_id == "arc"


def test_attributs_legacy_lus_sur_l_orbe_actif(qapp):
    host = OrbHost("", "ANO", "pulse")
    host.set_volume(0.7)
    assert host._target_vol == pytest.approx(0.7)
    assert "idle" in host._PALETTES
    assert host._ws == "idle"
    with pytest.raises(AttributeError):
        host.attribut_qui_n_existe_pas


def test_veille_coupe_la_peinture(qapp):
    host = OrbHost("", "ANO", "pulse")
    host.resize(400, 400)
    host.show()
    host.set_low_power(True)
    assert host.orb._dormant()
    assert host.orb._anim_tmr.interval() == BaseOrb.FRAME_MS_SLEEP
    host.set_low_power(False)
    host.hide()


@pytest.mark.parametrize("style_id", [s.id for s in registry.all_specs()
                                      if s.engine == "qpainter"])
def test_chaque_style_qpainter_se_construit(qapp, style_id):
    """Les styles prêts peignent sans erreur ; les chantiers au moins s'importent."""
    spec = registry.get(style_id)
    widget = registry.load_factory(spec)("", "ANO", None)
    try:
        if spec.ready and isinstance(widget, BaseOrb):
            widget.resize(320, 320)
            widget.show()
            widget.state = "SPEAKING"
            widget.speaking = True
            widget._tick()
            image = QImage(320, 320, QImage.Format.Format_ARGB32_Premultiplied)
            painter = QPainter(image)
            widget.paint_orb(painter, 160, 160, 70, 1.0)
            painter.end()
            assert not widget._broken
    finally:
        getattr(widget, "shutdown", lambda: None)()
        widget.deleteLater()


def test_preview_all_ne_modifie_pas_les_specs_par_effet_de_bord():
    spec = registry.get("nebula")
    assert not replace(spec, ready=False).ready and registry.get("nebula").ready
