"""Intégration de l'orbe holographique GLSL (GPU) — compilation, API, perf."""
from __future__ import annotations

import os
import time

import pytest

from PyQt6.QtWidgets import QApplication

from ui.orb.arc_core import HudCanvas
from ui.orb.glsl_orb import (
    FRAGMENT_BODY,
    STATE_VALUE,
    VERTEX_BODY,
    GLSLOrbWidget,
    cinematic_step,
    create_hud_orb,
    fragment_source,
    glsl_orb_requested,
    probe_gl_orb,
    vertex_source,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_les_shaders_exposent_audio_etat_et_early_out():
    """Le fragment doit porter le contrat demandé : FFT 8 bandes + u_state."""
    assert "u_audio[8]" in FRAGMENT_BODY
    assert "uniform float u_state" in FRAGMENT_BODY
    assert "uniform float u_energy" in FRAGMENT_BODY
    assert "uniform float u_volume" in FRAGMENT_BODY
    for i in range(8):
        assert f"u_audio[{i}]" in FRAGMENT_BODY
    assert "if (r > 0.78)" in FRAGMENT_BODY
    assert "layout(location = 0) in vec2 a_pos" in VERTEX_BODY
    desktop = fragment_source(False)
    gles = fragment_source(True)
    assert desktop.startswith("#version 330 core")
    assert gles.startswith("#version 300 es")
    assert "precision highp float" in gles
    assert vertex_source(False).startswith("#version 330 core")
    assert vertex_source(True).startswith("#version 300 es")


def test_u_state_interpolé_de_maniere_cinematique():
    """IDLE→SPEAKING ne saute pas : ~250 ms pour 95 % du chemin."""
    value = 0.0
    for _ in range(8):
        value = cinematic_step(value, STATE_VALUE["speaking"], 0.016, 4.2)
        assert 0.0 < value < STATE_VALUE["speaking"]
    for _ in range(400):
        value = cinematic_step(value, STATE_VALUE["speaking"], 0.016, 4.2)
    assert abs(value - STATE_VALUE["speaking"]) < 0.05
    # Un dt aberrant (pause, freeze) ne fait pas exploser l'état.
    jumped = cinematic_step(0.0, 4.0, 5.0, 4.2)
    assert jumped < 4.0


def test_create_hud_orb_reste_qpainter_par_defaut(monkeypatch, qapp):
    monkeypatch.delenv("ANOGPT_GLSL_ORB", raising=False)
    assert glsl_orb_requested() is False
    orb = create_hud_orb("config/jarvis.png")
    try:
        assert isinstance(orb, HudCanvas)
        assert not isinstance(orb, GLSLOrbWidget)
    finally:
        orb.close()


def test_glsl_ne_remplace_jamais_lorbe_pyqt_implicitement(monkeypatch):
    monkeypatch.delenv("ANOGPT_GLSL_ORB", raising=False)
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
    assert glsl_orb_requested() is False
    monkeypatch.setenv("QT_QPA_PLATFORM", "wayland")
    assert glsl_orb_requested() is False


def test_create_hud_orb_honore_le_drapeau_glsl(monkeypatch, qapp):
    monkeypatch.setenv("ANOGPT_GLSL_ORB", "1")
    assert glsl_orb_requested() is True
    orb = create_hud_orb("config/jarvis.png")
    try:
        assert isinstance(orb, GLSLOrbWidget)
    finally:
        orb.close()


def _show(widget, qapp, width=320, height=320):
    widget.resize(width, height)
    widget.show()
    qapp.processEvents()
    # initializeGL n'est invoqué qu'après un vrai show + paint.
    for _ in range(12):
        widget.update()
        qapp.processEvents()
        if widget.ready or widget.init_error:
            break
        time.sleep(0.02)
    return widget


@pytest.fixture
def gl_orb(qapp):
    widget = GLSLOrbWidget("")
    _show(widget, qapp)
    if not widget.ready:
        widget.close()
        pytest.skip(f"contexte GLSL indisponible : {widget.init_error}")
    yield widget
    widget.close()
    qapp.processEvents()


def test_le_widget_compile_le_programme_glsl(gl_orb):
    assert gl_orb.ready
    assert gl_orb.init_error is None
    info = gl_orb.gl_info
    assert info.get("renderer")
    assert info.get("alpha") not in {"-1", ""}
    # Desktop 3.3+ ou GLES 3.0 — les deux préambules sont prévus.
    assert info.get("version") or info.get("profile")


def test_api_compatible_avec_le_mini_orbe(gl_orb):
    """MiniOrbOverlay lit _ws, _volume, _energy, _PALETTES, _target_vol."""
    for name in ("_ws", "_volume", "_energy", "_PALETTES", "_target_vol", "_anim_tmr"):
        assert hasattr(gl_orb, name)
    gl_orb.state = "LISTENING"
    assert gl_orb._ws == "listening"
    assert gl_orb._target_state == pytest.approx(STATE_VALUE["listening"])
    gl_orb.speaking = True
    assert gl_orb._ws == "speaking"
    gl_orb.speaking = False
    gl_orb.muted = True
    assert gl_orb._ws == "idle"
    gl_orb.muted = False
    gl_orb.state = "THINKING"
    assert gl_orb._ws == "thinking"
    gl_orb.state = "ERROR"
    assert gl_orb._ws == "error"
    gl_orb.set_volume(0.73)
    assert gl_orb._target_vol == pytest.approx(0.73)
    gl_orb.set_continuous_vision(True)
    assert gl_orb.continuous_vision is True
    gl_orb.set_low_power(True)
    assert gl_orb._low_power is True
    gl_orb.set_low_power(False)


def test_set_audio_bands_clamp_a_huit(gl_orb):
    gl_orb.set_audio_bands([0.1, 1.4, -0.2])
    assert len(gl_orb._target_audio) == 8
    assert gl_orb._target_audio[0] == pytest.approx(0.1)
    assert gl_orb._target_audio[1] == pytest.approx(1.0)
    assert gl_orb._target_audio[2] == pytest.approx(0.0)
    assert gl_orb._target_audio[7] == pytest.approx(0.0)
    gl_orb.set_audio_bands([0.1] * 12)
    assert len(gl_orb._target_audio) == 8


def test_une_erreur_danimation_ne_termine_pas_lapplication(qapp, monkeypatch):
    canvas = GLSLOrbWidget("")
    monkeypatch.setattr(
        canvas,
        "_tick_frame",
        lambda: (_ for _ in ()).throw(RuntimeError("image invalide")),
    )
    canvas._tick()
    assert not canvas._anim_tmr.isActive()
    canvas.close()


def test_le_fond_reste_transparent_hors_de_lorbe(gl_orb, qapp):
    gl_orb.state = "LISTENING"
    gl_orb.set_audio_bands([0.8, 0.6, 0.5, 0.4, 0.3, 0.3, 0.2, 0.2])
    gl_orb._u_state = STATE_VALUE["listening"]
    gl_orb._energy = 1.0
    gl_orb._volume = 0.6
    gl_orb._audio = list(gl_orb._target_audio)
    gl_orb.update()
    qapp.processEvents()
    image = gl_orb.grabFramebuffer()
    assert not image.isNull()
    w, h = image.width(), image.height()
    assert w > 8 and h > 8
    corner = image.pixelColor(2, 2)
    # Coin : alpha nul ou quasi (early-out). Sur certains compositeurs Wayland
    # le grab pré-multiplie un fond noir : on exige surtout un coin sombre.
    assert corner.alpha() < 40 or (corner.red() + corner.green() + corner.blue()) < 30
    cx, cy = w // 2, h // 2
    center = image.pixelColor(cx, cy)
    # Le noyau doit émettre (cyan / magenta / blanc).
    assert center.red() + center.green() + center.blue() > 40


def test_la_sonde_materiel_renvoie_un_contexte_reel(qapp):
    info = probe_gl_orb()
    if not info.get("ready"):
        pytest.skip(f"sonde GL en échec : {info.get('error')}")
    assert "Intel" in info.get("renderer", "") or info.get("renderer")
    assert info.get("version")


def test_tick_ne_bloque_pas_la_boucle_qt(gl_orb, qapp):
    """Le slot d'animation ne doit pas appeler glFinish ni tourner en dur."""
    import inspect
    from ui.orb import glsl_orb as mod
    src = inspect.getsource(mod.GLSLOrbWidget.paintGL)
    assert "glFinish" not in src
    src_tick = inspect.getsource(mod.GLSLOrbWidget._tick_frame)
    assert "sleep" not in src_tick
    gl_orb._anim_tmr.setInterval(16)
    t0 = time.perf_counter()
    for _ in range(8):
        gl_orb._tick_frame()
        qapp.processEvents()
    elapsed = time.perf_counter() - t0
    # 8 ticks + processEvents : du budget, pas un bench fps (l'iGPU peut
    # être occupé par Hyprland). L'absence de glFinish/sleep est le vrai contrat.
    assert elapsed < 0.80
