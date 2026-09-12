"""Orbe holographique GPU — fragment shader GLSL, zéro trigonométrie Python.

Sonde réelle sur la machine cible (Intel HD Graphics 520 / Skylake GT2,
Mesa iris, OpenGL 4.6 core, GLSL 4.60, GLES 3.2) :

* un triangle plein écran + shader simple tient le VSync (~60 fps) ;
* ``QOpenGLShaderProgram.setUniformValueArray`` abort sur ce binding PyQt6
  — les 8 bandes FFT passent par ``glUniform1fv`` ;
* le widget HUD remplit toute la fenêtre : un raymarching 16+ pas avec
  bruit 3D à chaque pas saturait l'iGPU (24 EU, 300–1000 MHz, mémoire
  unifiée). Le shader ci-dessous est une sphère volumétrique analytique
  (early-out circulaire, 6 échantillons de plasma, bloom intégré).

Activer dans l'UI : ``ANOGPT_GLSL_ORB=1``. Sans ce drapeau, ``HudCanvas``
QPainter reste le moteur par défaut — MiniOrb / Companion n'ouvrent pas
un second contexte GL.
"""
from __future__ import annotations

import ctypes
import math
import os
import sys
import time
from ctypes import CFUNCTYPE, POINTER, c_float, c_int, c_uint, c_void_p
from typing import Iterable, Sequence

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QSurfaceFormat
from PyQt6.QtWidgets import QSizePolicy, QWidget

try:
    from PyQt6.QtOpenGL import (
        QOpenGLBuffer,
        QOpenGLShader,
        QOpenGLShaderProgram,
        QOpenGLVertexArrayObject,
    )
    from PyQt6.QtOpenGLWidgets import QOpenGLWidget
    _HAVE_GL = True
except ImportError:  # pragma: no cover - binding Qt incomplet
    QOpenGLBuffer = QOpenGLShader = QOpenGLShaderProgram = None  # type: ignore
    QOpenGLVertexArrayObject = None  # type: ignore
    QOpenGLWidget = QWidget  # type: ignore
    _HAVE_GL = False


# ── Constantes GL (évite PyOpenGL, absent de la machine) ─────────────────────
_GL_COLOR_BUFFER_BIT = 0x00004000
_GL_BLEND = 0x0BE2
_GL_SRC_ALPHA = 0x0302
_GL_ONE_MINUS_SRC_ALPHA = 0x0303
_GL_ONE = 1
_GL_TRIANGLES = 0x0004
_GL_FLOAT = 0x1406
_GL_RENDERER = 0x1F01
_GL_VERSION = 0x1F02
_GL_SHADING_LANGUAGE_VERSION = 0x8B8C
_GL_NO_ERROR = 0

# u_state : interpolation cinématique, pas un enum discret côté GPU.
STATE_VALUE = {
    "idle": 0.0,
    "listening": 1.0,
    "thinking": 2.0,
    "acting": 2.45,
    "speaking": 3.0,
    "error": 4.0,
}
STATE_FROM_CANONICAL = {
    "IDLE": "idle",
    "LISTENING": "listening",
    "THINKING": "thinking",
    "PROCESSING": "thinking",
    "ACTING": "acting",
    "EXECUTING": "acting",
    "RUNNING": "acting",
    "SPEAKING": "speaking",
    "ERROR": "error",
}

# Palettes QColor : MiniOrbOverlay / CompanionOrb les lisent sur la source.
_PALETTES = {
    "idle": {
        "core": QColor(120, 225, 255), "halo": QColor(0, 170, 255),
        "wire": QColor(0, 190, 255),   "hot": QColor(225, 250, 255),
        "pulse_speed": 0.85, "spin": 1.0,
    },
    "listening": {
        "core": QColor(115, 255, 205), "halo": QColor(0, 205, 142),
        "wire": QColor(0, 245, 178),   "hot": QColor(232, 255, 246),
        "pulse_speed": 1.9,  "spin": 1.7,
    },
    "thinking": {
        "core": QColor(205, 155, 255), "halo": QColor(125, 65, 255),
        "wire": QColor(165, 105, 255), "hot": QColor(246, 235, 255),
        "pulse_speed": 2.1,  "spin": 3.1,
    },
    "acting": {
        "core": QColor(0, 245, 255),   "halo": QColor(0, 180, 255),
        "wire": QColor(0, 255, 220),   "hot": QColor(240, 255, 255),
        "pulse_speed": 4.0,  "spin": 3.8,
    },
    "speaking": {
        "core": QColor(135, 190, 255), "halo": QColor(45, 90, 255),
        "wire": QColor(65, 145, 255),  "hot": QColor(238, 246, 255),
        "pulse_speed": 3.4,  "spin": 2.4,
    },
    "error": {
        "core": QColor(255, 60, 90),    "halo": QColor(220, 20, 50),
        "wire": QColor(255, 90, 110),   "hot": QColor(255, 230, 235),
        "pulse_speed": 4.5,  "spin": 1.2,
    },
}


# ═════════════════════════════════════════════════════════════════════════════
# Shaders — corps sans directive #version (préfixe selon desktop / GLES)
# ═════════════════════════════════════════════════════════════════════════════

VERTEX_BODY = """
layout(location = 0) in vec2 a_pos;
void main() {
    gl_Position = vec4(a_pos, 0.0, 1.0);
}
"""

FRAGMENT_BODY = """
out vec4 fragColor;

uniform vec2  u_resolution;
uniform float u_time;
uniform float u_audio[8];
uniform float u_state;
uniform float u_energy;
uniform float u_volume;
uniform float u_vision;

// ── Bruit 3D value (2 octaves) : nettement moins d'ALU que Simplex, suffisant
// pour un plasma holographique. Hash IQ, interpolation hermitique.
float hash13(vec3 p) {
    p = fract(p * 0.3183099 + vec3(0.11, 0.17, 0.23));
    p *= 17.0;
    return fract(p.x * p.y * p.z * (p.x + p.y + p.z));
}

float vnoise(vec3 p) {
    vec3 i = floor(p);
    vec3 f = fract(p);
    f = f * f * (3.0 - 2.0 * f);
    float n000 = hash13(i);
    float n100 = hash13(i + vec3(1.0, 0.0, 0.0));
    float n010 = hash13(i + vec3(0.0, 1.0, 0.0));
    float n110 = hash13(i + vec3(1.0, 1.0, 0.0));
    float n001 = hash13(i + vec3(0.0, 0.0, 1.0));
    float n101 = hash13(i + vec3(1.0, 0.0, 1.0));
    float n011 = hash13(i + vec3(0.0, 1.0, 1.0));
    float n111 = hash13(i + vec3(1.0, 1.0, 1.0));
    float x00 = mix(n000, n100, f.x);
    float x10 = mix(n010, n110, f.x);
    float x01 = mix(n001, n101, f.x);
    float x11 = mix(n011, n111, f.x);
    return mix(mix(x00, x10, f.y), mix(x01, x11, f.y), f.z);
}

float fbm2(vec3 p) {
    float a = 0.55;
    float s = a * vnoise(p);
    p = p * 2.07 + vec3(0.17, 0.09, 0.13);
    s += 0.5 * a * vnoise(p);
    return s;
}

mat2 rot(float a) {
    float c = cos(a), s = sin(a);
    return mat2(c, -s, s, c);
}

float audioBass() { return u_audio[0] * 0.55 + u_audio[1] * 0.45; }
float audioMid()  { return u_audio[2] * 0.34 + u_audio[3] * 0.33 + u_audio[4] * 0.33; }
float audioHigh() { return u_audio[5] * 0.40 + u_audio[6] * 0.35 + u_audio[7] * 0.25; }

vec3 palette(float s) {
    vec3 idleC  = vec3(0.20, 0.86, 1.00);
    vec3 listC  = vec3(0.18, 1.00, 0.72);
    vec3 thinkC = vec3(0.86, 0.32, 1.00);
    vec3 speakC = vec3(0.25, 0.55, 1.00);
    vec3 errC   = vec3(1.00, 0.16, 0.30);
    s = clamp(s, 0.0, 4.0);
    if (s < 1.0) return mix(idleC,  listC,  smoothstep(0.0, 1.0, s));
    if (s < 2.0) return mix(listC,  thinkC, smoothstep(1.0, 2.0, s));
    if (s < 3.0) return mix(thinkC, speakC, smoothstep(2.0, 3.0, s));
    return mix(speakC, errC, smoothstep(3.0, 4.0, s));
}

void main() {
    vec2 res = u_resolution;
    float m  = min(res.x, res.y);
    vec2 uv  = (gl_FragCoord.xy - 0.5 * res) / max(m, 1.0);

    // Dérive organique : l'orbe flotte de quelques pixels, jamais rivé.
    uv += 0.010 * vec2(sin(u_time * 0.17), sin(u_time * 0.13 + 0.7));

    float r = length(uv);
    // Early-out : hors halo, un pixel = un discard. Sur un HUD plein cadre
    // (cas réel d'ANO-GPT) ça divise le coût par ~8–12 sur HD 520.
    if (r > 0.78) {
        fragColor = vec4(0.0);
        return;
    }

    float bass = audioBass();
    float mid  = audioMid();
    float high = audioHigh();
    float en   = clamp(u_energy, 0.0, 1.5);
    float vol  = clamp(u_volume, 0.0, 1.0);

    vec3 pal    = palette(u_state);
    vec3 palHot = mix(vec3(1.0), pal, 0.22);
    vec3 palMag = mix(pal, vec3(1.00, 0.14, 0.72), 0.48);

    float spd     = 0.85 + en * 1.6;
    float breathe = 0.60 * sin(u_time * spd * 1.10) + 0.40 * sin(u_time * 0.37 * spd + 2.0);
    float radius  = 0.30 * (1.0 + 0.028 * breathe + 0.11 * vol + 0.14 * bass);

    // Silhouette bruitée (1 fbm) — déformation calquée sur les médiums FFT.
    float nSil = fbm2(vec3(uv * 3.1, u_time * 0.22));
    float rad  = radius * (1.0 + (nSil - 0.45) * (0.10 + 0.22 * mid));

    vec3  col   = vec3(0.0);
    float alpha = 0.0;

    // Bloom néon intégré (pas de second passe) : deux gaussiennes additives.
    float gCore = exp(-pow(r / max(rad * 1.65, 0.02), 2.0) * 2.6);
    float gHalo = exp(-pow(r / max(rad * 2.35, 0.02), 2.0) * 2.1);
    col   += pal    * (0.24 * gHalo + 0.32 * gCore) * (0.40 + 0.60 * en);
    col   += palMag * 0.16 * gCore * (0.25 + high);
    alpha += 0.32 * gHalo + 0.42 * gCore;

    // Trois anneaux gyroscopiques 2D (ellipses tournantes) — coût fixe.
    for (int k = 0; k < 3; ++k) {
        float kf  = float(k);
        float ang = u_time * (0.35 + kf * 0.31) * (0.75 + en) + kf * 1.73;
        vec2  euv = rot(ang) * uv;
        float st  = 1.28 + 0.22 * kf;
        float er  = abs(length(euv * vec2(1.0, st)) - (rad * 1.22 + 0.055 * kf));
        float rng = smoothstep(0.016, 0.0, er);
        col   += mix(pal, palMag, kf * 0.35) * rng * (0.16 + 0.14 * en);
        alpha += rng * 0.20;
    }

    // Sphère volumétrique : z = ±sqrt(R² − r²), 6 échantillons de plasma
    // le long de la corde. Pas de SDF itéré — l'iGPU Skylake tient 60 fps.
    if (r < rad) {
        float z = sqrt(max(rad * rad - r * r, 0.0));
        vec3  nrm = normalize(vec3(uv, z));
        vec3  ldir = normalize(vec3(0.42, 0.78, 0.52));
        float ndl  = clamp(dot(nrm, ldir), 0.0, 1.0);
        float fres = pow(1.0 - abs(nrm.z), 2.5);

        float plasma = 0.0;
        vec3  spinP  = vec3(uv, z);
        spinP.xz = rot(u_time * (0.28 + 0.55 * en)) * spinP.xz;
        for (int i = 0; i < 6; ++i) {
            float fi = float(i) / 5.0;
            vec3  sp = vec3(spinP.xy, mix(z, -z, fi));
            plasma += fbm2(sp * 2.55 + vec3(0.0, u_time * 0.41, u_time * 0.27));
        }
        plasma *= (1.0 / 6.0);

        col += palHot * (0.22 + 0.58 * ndl) * (0.42 + 0.75 * plasma);
        col += pal    * fres * (0.50 + 0.45 * en);
        col += palMag * plasma * (0.20 + 0.55 * mid);
        col += vec3(1.0) * pow(ndl, 7.0) * 0.38;

        // Filaire holographique (latitudes / longitudes) sur la sphère.
        float lat  = abs(fract(atan(nrm.y, length(nrm.xz)) * 1.5915 + 0.5) - 0.5);
        float lon  = abs(fract(atan(nrm.x, nrm.z)          * 1.9099 + 0.5) - 0.5);
        float wire = smoothstep(0.040, 0.010, min(lat, lon));
        col += pal * wire * (0.40 + 0.35 * en);

        // Iris de réacteur au centre.
        float iris = smoothstep(rad * 0.42, 0.0, r);
        col += palHot * iris * (0.45 + 0.85 * vol);

        alpha = max(alpha, 0.90);
    }

    // Scanlines holographiques très légères.
    col *= 0.90 + 0.10 * sin((uv.y + u_time * 0.12) * 86.0);

    // Pulse de parole (cœur).
    col += palHot * exp(-r * r * 22.0) * vol * 0.40;

    // Spectre radial 8 bandes — barres hors de la sphère, dans le halo.
    if (r > rad * 1.05 && r < rad * 1.55) {
        float ang = atan(uv.y, uv.x);
        float slot = fract((ang / 6.2831853) * 8.0 + u_time * 0.05);
        int   bi = int(floor(mod((ang / 6.2831853) * 8.0 + 8.0, 8.0)));
        float band = 0.0;
        // Indexation explicite : GLSL 330 n'aime pas u_audio[bi] variable
        // sur certains iris Skylake — on déroule.
        band = (bi == 0) ? u_audio[0] : band;
        band = (bi == 1) ? u_audio[1] : band;
        band = (bi == 2) ? u_audio[2] : band;
        band = (bi == 3) ? u_audio[3] : band;
        band = (bi == 4) ? u_audio[4] : band;
        band = (bi == 5) ? u_audio[5] : band;
        band = (bi == 6) ? u_audio[6] : band;
        band = (bi == 7) ? u_audio[7] : band;
        float bar = smoothstep(0.18, 0.02, abs(slot - 0.5) * 2.0);
        float reach = rad * (1.08 + band * 0.42);
        float along = smoothstep(rad * 1.04, rad * 1.08, r) * smoothstep(reach, reach - 0.02, r);
        col   += pal * bar * along * (0.35 + 0.55 * en);
        alpha += bar * along * 0.25;
    }

    // Trois jets ioniques : des traînées fines et fluides, directement liées
    // aux graves / médiums / aigus. Ils n'utilisent ni texture ni seconde
    // passe, donc restent pratiquement gratuits sur l'iGPU.
    for (int j = 0; j < 3; ++j) {
        float jf = float(j);
        float drive = (j == 0) ? bass : ((j == 1) ? mid : high);
        float jetAng = u_time * (0.48 + 0.17 * jf) * (0.8 + en)
                     + jf * 2.0943951 + 0.23 * sin(u_time * 1.7 + jf);
        vec2 axis = vec2(cos(jetAng), sin(jetAng));
        float forward = dot(uv, axis);
        float sideways = abs(dot(uv, vec2(-axis.y, axis.x)));
        float start = rad * 0.86;
        float reach = rad * (1.42 + drive * 0.92 + 0.10 * en);
        float width = 0.008 + 0.013 * drive;
        float beam = smoothstep(width, 0.0, sideways)
                   * smoothstep(start, start + 0.025, forward)
                   * smoothstep(reach, reach - 0.09, forward);
        float tail = 0.55 + 0.45 * sin(forward * 44.0 - u_time * (8.0 + jf * 2.0));
        col += mix(pal, palHot, 0.58) * beam * tail * (0.18 + 1.05 * drive);
        alpha += beam * (0.10 + 0.22 * drive);
    }

    // Œil néon (vision continue) — badge SDF, coin haut-droit de l'orbe.
    if (u_vision > 0.5) {
        vec2 e = uv - vec2(0.26, 0.26);
        float ringe = abs(length(e) - 0.048);
        float iris  = length(e) - 0.016;
        col   += vec3(0.05, 0.95, 1.00) * smoothstep(0.008, 0.0, ringe) * 0.85;
        col   += vec3(0.70, 1.00, 1.00) * (1.0 - smoothstep(0.0, 0.010, iris));
        alpha  = max(alpha, 0.65 * smoothstep(0.07, 0.04, length(e)));
    }

    col   = 1.0 - exp(-col * 1.28);
    alpha = clamp(alpha, 0.0, 1.0);
    fragColor = vec4(col * alpha, alpha);
}
"""


def shader_preamble(gles: bool) -> str:
    if gles:
        return "#version 300 es\nprecision highp float;\nprecision highp int;\n"
    return "#version 330 core\n"


def vertex_source(gles: bool = False) -> str:
    return shader_preamble(gles) + VERTEX_BODY


def fragment_source(gles: bool = False) -> str:
    return shader_preamble(gles) + FRAGMENT_BODY


def cinematic_step(current: float, target: float, dt: float, rate: float = 4.2) -> float:
    """Approche exponentielle (~250 ms pour 95 % du chemin à rate=4.2)."""
    dt = max(0.0, min(0.08, float(dt)))
    t = 1.0 - math.exp(-rate * dt)
    return current + (target - current) * t


def _env_on(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def glsl_orb_requested() -> bool:
    configured = os.environ.get("ANOGPT_GLSL_ORB")
    if configured is not None:
        return configured.strip().lower() in {"1", "true", "yes", "on"}
    # L'orbe PyQt/QPainter est le visuel principal. Le rendu GLSL reste une
    # expérimentation disponible uniquement sur demande explicite.
    return False


def orb_surface_format() -> QSurfaceFormat:
    fmt = QSurfaceFormat()
    fmt.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    fmt.setVersion(3, 3)
    fmt.setAlphaBufferSize(8)
    fmt.setRedBufferSize(8)
    fmt.setGreenBufferSize(8)
    fmt.setBlueBufferSize(8)
    fmt.setDepthBufferSize(0)
    fmt.setStencilBufferSize(0)
    fmt.setSamples(0)
    fmt.setSwapBehavior(QSurfaceFormat.SwapBehavior.DoubleBuffer)
    fmt.setSwapInterval(1)
    return fmt


class _GLApi:
    """Charge un sous-ensemble GL via getProcAddress — PyQt6 n'expose pas
    ``initializeOpenGLFunctions`` sur QOpenGLFunctions_4_1_Core."""

    __slots__ = (
        "glClearColor", "glClear", "glEnable", "glBlendFunc",
        "glDrawArrays", "glGetString", "glGetError", "glUniform1fv",
        "glViewport",
    )

    def __init__(self, ctx) -> None:
        def load(name: str, restype, *argtypes):
            ptr = ctx.getProcAddress(name.encode("ascii"))
            if not ptr:
                raise RuntimeError(f"entrée GL manquante : {name}")
            return CFUNCTYPE(restype, *argtypes)(int(ptr))

        self.glClearColor = load("glClearColor", None, c_float, c_float, c_float, c_float)
        self.glClear = load("glClear", None, c_uint)
        self.glEnable = load("glEnable", None, c_uint)
        self.glBlendFunc = load("glBlendFunc", None, c_uint, c_uint)
        self.glDrawArrays = load("glDrawArrays", None, c_uint, c_int, c_int)
        self.glGetString = load("glGetString", c_void_p, c_uint)
        self.glGetError = load("glGetError", c_uint)
        self.glUniform1fv = load("glUniform1fv", None, c_int, c_int, POINTER(c_float))
        self.glViewport = load("glViewport", None, c_int, c_int, c_int, c_int)

    def renderer(self) -> str:
        ptr = self.glGetString(_GL_RENDERER)
        return ctypes.string_at(ptr).decode("utf-8", "replace") if ptr else ""

    def version(self) -> str:
        ptr = self.glGetString(_GL_VERSION)
        return ctypes.string_at(ptr).decode("utf-8", "replace") if ptr else ""


def _fill_audio(dst: ctypes.Array, bands: Sequence[float]) -> ctypes.Array:
    n = len(bands)
    for i in range(8):
        v = float(bands[i]) if i < n else 0.0
        dst[i] = 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)
    return dst


class GLSLOrbWidget(QOpenGLWidget):
    """Orbe holographique rendu 100 % GPU, API compatible avec ``HudCanvas``.

    MiniOrbOverlay et CompanionOrb lisent ``_ws``, ``_volume``, ``_energy``
    et ``_PALETTES`` : ces attributs sont maintenus, le grand orbe ne recalcule
    plus de géométrie Python.
    """

    _PALETTES = _PALETTES

    def __init__(self, face_path: str = "", assistant_name: str = "ANO-GPT",
                 parent=None):
        if not _HAVE_GL:
            raise RuntimeError("PyQt6.QtOpenGLWidgets.QOpenGLWidget indisponible")
        super().__init__(parent)
        self.setFormat(orb_surface_format())
        self.setMinimumSize(300, 300)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAutoFillBackground(False)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)
        self.setUpdateBehavior(QOpenGLWidget.UpdateBehavior.NoPartialUpdate)

        self._assistant_name = assistant_name
        self._face_path = face_path
        self._muted = False
        self._speaking = False
        self._state = "idle"
        self._ws = "idle"

        self._t0 = time.monotonic()
        self._last_tick = self._t0
        self._volume = 0.0
        self._target_vol = 0.0
        self._last_ext_vol_t = 0.0
        self._energy = 0.0
        self._u_state = 0.0
        self._target_state = 0.0
        self._audio = [0.0] * 8
        self._target_audio = [0.0] * 8
        self._audio_c = (c_float * 8)()
        self._last_bands_t = 0.0

        self._gesture_icon: str | None = None
        self._gesture_label = ""
        self._gesture_val = 0.0
        self._gesture_expires = 0.0
        self._continuous_vision_active = False

        self._gl: _GLApi | None = None
        self._prog: QOpenGLShaderProgram | None = None
        self._vao: QOpenGLVertexArrayObject | None = None
        self._vbo: QOpenGLBuffer | None = None
        self._locs: dict[str, int] = {}
        self._ready = False
        self._gl_info: dict[str, str] = {}
        self._init_error: str | None = None
        self._gles = False

        self._low_power = False
        self._on_battery = False
        # Le rendu GLSL peut tout de même saturer le thread Qt/XWayland sur
        # une petite machine. La voix a priorité sur une animation 60 Hz.
        self._interval = 100
        self._anim_tmr = QTimer(self)
        self._anim_tmr.setTimerType(Qt.TimerType.PreciseTimer)
        self._anim_tmr.timeout.connect(self._tick)
        self._anim_tmr.start(self._interval)

        self._batt_tmr = QTimer(self)
        self._batt_tmr.timeout.connect(self._check_battery)
        self._batt_tmr.start(15000)
        self._check_battery()

    # ══ Sonde / état GL ══════════════════════════════════════════════════════
    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def gl_info(self) -> dict[str, str]:
        return dict(self._gl_info)

    @property
    def init_error(self) -> str | None:
        return self._init_error

    def initializeGL(self) -> None:
        try:
            ctx = self.context()
            if ctx is None or not ctx.isValid():
                raise RuntimeError("contexte OpenGL invalide")
            self._gles = bool(ctx.isOpenGLES())
            self._gl = _GLApi(ctx)
            self._gl_info = {
                "renderer": self._gl.renderer(),
                "version": self._gl.version(),
                "gles": str(self._gles),
                "profile": f"{ctx.format().majorVersion()}.{ctx.format().minorVersion()}",
                "alpha": str(ctx.format().alphaBufferSize()),
                "swap": str(ctx.format().swapInterval()),
            }
            if not self._compile_program():
                return
            self._build_fullscreen_triangle()
            self._gl.glEnable(_GL_BLEND)
            # Sortie pré-multipliée (voir fragColor = vec4(col*alpha, alpha)).
            self._gl.glBlendFunc(_GL_ONE, _GL_ONE_MINUS_SRC_ALPHA)
            self._ready = True
        except Exception as exc:
            self._init_error = f"{type(exc).__name__}: {exc}"
            self._ready = False
            print(f"[GLSLOrb] initializeGL : {self._init_error}", file=sys.stderr)
            self._anim_tmr.stop()

    def _compile_program(self) -> bool:
        assert self._prog is None
        prog = QOpenGLShaderProgram(self)
        vert = vertex_source(self._gles)
        frag = fragment_source(self._gles)
        if not prog.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex, vert):
            self._init_error = "vertex: " + prog.log()
            print(f"[GLSLOrb] {self._init_error}", file=sys.stderr)
            return False
        if not prog.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Fragment, frag):
            self._init_error = "fragment: " + prog.log()
            print(f"[GLSLOrb] {self._init_error}", file=sys.stderr)
            return False
        if not prog.link():
            self._init_error = "link: " + prog.log()
            print(f"[GLSLOrb] {self._init_error}", file=sys.stderr)
            return False
        self._prog = prog
        self._locs = {
            name: prog.uniformLocation(name)
            for name in (
                "u_resolution", "u_time", "u_state", "u_energy",
                "u_volume", "u_vision", "u_audio",
            )
        }
        return True

    def _build_fullscreen_triangle(self) -> None:
        # PyQt6.allocate n'accepte pas `bytes` : il faut un voidptr + taille.
        self._verts = (c_float * 6)(-1.0, -1.0, 3.0, -1.0, -1.0, 3.0)
        nbytes = ctypes.sizeof(self._verts)
        vao = QOpenGLVertexArrayObject(self)
        if not vao.create():
            raise RuntimeError("VAO: create() a échoué")
        vao.bind()
        vbo = QOpenGLBuffer(QOpenGLBuffer.Type.VertexBuffer)
        if not vbo.create():
            raise RuntimeError("VBO: create() a échoué")
        vbo.bind()
        vbo.allocate(nbytes)
        vbo.write(0, ctypes.addressof(self._verts), nbytes)
        assert self._prog is not None
        self._prog.bind()
        self._prog.enableAttributeArray(0)
        self._prog.setAttributeBuffer(0, _GL_FLOAT, 0, 2)
        self._prog.release()
        vao.release()
        self._vao = vao
        self._vbo = vbo

    def resizeGL(self, w: int, h: int) -> None:
        if self._gl is not None and w > 0 and h > 0:
            self._gl.glViewport(0, 0, int(w), int(h))

    def paintGL(self) -> None:
        gl = self._gl
        if gl is None:
            return
        gl.glClearColor(0.0, 0.0, 0.0, 0.0)
        gl.glClear(_GL_COLOR_BUFFER_BIT)
        if not self._ready or self._prog is None or self._vao is None:
            return
        gl.glEnable(_GL_BLEND)
        gl.glBlendFunc(_GL_ONE, _GL_ONE_MINUS_SRC_ALPHA)
        prog = self._prog
        prog.bind()
        locs = self._locs
        if locs["u_resolution"] >= 0:
            prog.setUniformValue(
                locs["u_resolution"], float(max(self.width(), 1)), float(max(self.height(), 1)),
            )
        now = time.monotonic()
        if locs["u_time"] >= 0:
            prog.setUniformValue(locs["u_time"], float(now - self._t0))
        if locs["u_state"] >= 0:
            prog.setUniformValue(locs["u_state"], float(self._u_state))
        if locs["u_energy"] >= 0:
            prog.setUniformValue(locs["u_energy"], float(self._energy))
        if locs["u_volume"] >= 0:
            prog.setUniformValue(locs["u_volume"], float(self._volume))
        if locs["u_vision"] >= 0:
            vis = 1.0 if self._continuous_vision_active else 0.0
            prog.setUniformValue(locs["u_vision"], vis)
        audio_loc = locs.get("u_audio", -1)
        if audio_loc >= 0:
            gl.glUniform1fv(audio_loc, 8, _fill_audio(self._audio_c, self._audio))
        self._vao.bind()
        gl.glDrawArrays(_GL_TRIANGLES, 0, 3)
        self._vao.release()
        prog.release()

    # ══ Tick (CPU : quelques floats, pas de géométrie) ═══════════════════════
    def _check_battery(self) -> None:
        try:
            import psutil
            b = psutil.sensors_battery()
            self._on_battery = bool(b and not b.power_plugged)
        except Exception:
            self._on_battery = False
        self._apply_interval()

    def _apply_interval(self) -> None:
        if self._low_power:
            ms = 200
        elif self._ws in {"listening", "speaking"}:
            ms = 67
        elif self._on_battery:
            ms = 120
        else:
            ms = 100
        self._interval = ms
        if self._anim_tmr.isActive():
            self._anim_tmr.setInterval(ms)

    def set_low_power(self, low: bool) -> None:
        low = bool(low)
        if low == self._low_power:
            return
        self._low_power = low
        self._apply_interval()

    def hideEvent(self, event) -> None:
        self._anim_tmr.stop()
        super().hideEvent(event)

    def showEvent(self, event) -> None:
        if not self._init_error:
            self._anim_tmr.start(self._interval)
        super().showEvent(event)

    def _tick(self) -> None:
        try:
            self._tick_frame()
        except Exception as exc:
            self._anim_tmr.stop()
            print(
                f"[GLSLOrb] Animation suspendue : {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            self.update()

    def _tick_frame(self) -> None:
        now = time.monotonic()
        dt = now - self._last_tick
        self._last_tick = now
        if dt <= 0.0 or dt > 0.25:
            dt = self._interval / 1000.0

        live_audio = (now - self._last_ext_vol_t) < 0.3
        if not live_audio:
            if self._ws == "speaking":
                self._target_vol = 0.30 + 0.70 * (0.5 + 0.5 * math.sin(now * 7.1))
            elif self._ws == "listening":
                self._target_vol = 0.06 + 0.14 * (0.5 + 0.5 * math.sin(now * 3.4))
            else:
                self._target_vol = max(0.0, self._target_vol - 0.05)
            if (now - self._last_bands_t) >= 0.3:
                self._synthesize_bands(self._target_vol, now)
        elif self._ws not in ("speaking", "listening"):
            self._target_vol = max(0.0, self._target_vol - 0.08)

        # Attaque quasi immédiate, relâchement plus long : les jets suivent
        # chaque syllabe sans l'effet haché ou nerveux d'un suivi brut.
        atk = 0.74 if self._target_vol > self._volume else 0.14
        self._volume += (self._target_vol - self._volume) * (1.0 - (1.0 - atk) ** (dt * 50.0))

        target_en = 0.15 if self._ws == "idle" else 1.0
        if self._ws == "acting":
            target_en = 1.25
        self._energy = cinematic_step(self._energy, target_en, dt, 3.2)
        self._u_state = cinematic_step(self._u_state, self._target_state, dt, 4.2)

        for i in range(8):
            cur, tgt = self._audio[i], self._target_audio[i]
            k = 0.65 if tgt > cur else 0.22
            self._audio[i] = cur + (tgt - cur) * (1.0 - (1.0 - k) ** (dt * 50.0))

        if not self._low_power:
            self.update()

    def _synthesize_bands(self, volume: float, now: float) -> None:
        bands = []
        for i in range(8):
            fall = 1.0 - i * 0.09
            wob = 0.55 + 0.45 * math.sin(now * (2.4 + i * 0.47) + i * 1.31)
            bands.append(max(0.0, min(1.0, volume * fall * wob)))
        self._target_audio = bands

    # ══ API HudCanvas ════════════════════════════════════════════════════════
    @property
    def muted(self) -> bool:
        return self._muted

    @muted.setter
    def muted(self, v: bool) -> None:
        self._muted = bool(v)
        self._update_ws()

    @property
    def speaking(self) -> bool:
        return self._speaking

    @speaking.setter
    def speaking(self, v: bool) -> None:
        self._speaking = bool(v)
        self._update_ws()

    @property
    def state(self) -> str:
        return self._state

    @state.setter
    def state(self, v: str) -> None:
        self._state = str(v)
        self._update_ws()

    def set_volume(self, v: float) -> None:
        v = max(0.0, min(1.0, float(v)))
        self._target_vol = v
        self._last_ext_vol_t = time.monotonic()
        if not self.isVisible():
            self._volume = v
        if (self._last_ext_vol_t - self._last_bands_t) >= 0.05:
            self._synthesize_bands(v, self._last_ext_vol_t)

    def set_audio_bands(self, bands: Iterable[float]) -> None:
        """8 bandes FFT normalisées [0, 1]. Moins de 8 → pad ; plus de 8 → coupe."""
        seq = [max(0.0, min(1.0, float(x))) for x in bands]
        if len(seq) < 8:
            seq.extend([0.0] * (8 - len(seq)))
        self._target_audio = seq[:8]
        self._last_bands_t = time.monotonic()
        self._last_ext_vol_t = self._last_bands_t
        # Le flux FFT est la source autoritaire : ne jamais conserver un pic
        # précédent, sinon l'orbe resterait gonflé après la voix.
        peak = max(self._target_audio)
        average = sum(self._target_audio) / 8.0
        self._target_vol = 0.70 * peak + 0.30 * average

    def _update_ws(self) -> None:
        if self._muted:
            ws = "idle"
        elif self._speaking:
            ws = "speaking"
        else:
            key = str(self._state).upper()
            ws = STATE_FROM_CANONICAL.get(key, str(self._state).lower())
            if ws not in STATE_VALUE:
                ws = "idle"
        self._ws = ws
        self._target_state = STATE_VALUE[ws]

    def show_gesture_feedback(
        self, icon: str, label: str = "", value: float = 0.0, duration: float = 1.6,
    ) -> None:
        self._gesture_icon = icon
        self._gesture_label = label
        self._gesture_val = value
        self._gesture_expires = time.monotonic() + max(0.4, duration)
        self.update()

    @property
    def continuous_vision(self) -> bool:
        return self._continuous_vision_active

    @continuous_vision.setter
    def continuous_vision(self, active: bool) -> None:
        self.set_continuous_vision(active)

    def set_continuous_vision(self, active: bool) -> None:
        self._continuous_vision_active = bool(active)
        self.update()


def create_hud_orb(face_path: str, assistant_name: str = "ANO-GPT", parent=None):
    """Usine HUD : orbe PyQt/QPainter, GLSL seulement sur demande."""
    if glsl_orb_requested() and _HAVE_GL:
        try:
            return GLSLOrbWidget(face_path, assistant_name, parent)
        except Exception as exc:
            print(f"[HUD] Orbe GLSL indisponible ({exc}), repli QPainter", file=sys.stderr)
    from ui.orb.arc_core import HudCanvas
    return HudCanvas(face_path, assistant_name, parent)


def probe_gl_orb(parent=None) -> dict:
    """Sonde non bloquante : crée un widget 64², compile, détruit.

    À n'appeler qu'avec une QApplication déjà vivante.
    """
    info: dict = {
        "have_widget": _HAVE_GL,
        "ready": False,
        "error": None,
        "renderer": "",
        "version": "",
        "gles": False,
    }
    if not _HAVE_GL:
        info["error"] = "QOpenGLWidget indisponible"
        return info
    from PyQt6.QtWidgets import QApplication
    if QApplication.instance() is None:
        info["error"] = "QApplication absente"
        return info
    w = GLSLOrbWidget("", "probe", parent)
    w.resize(64, 64)
    w.show()
    QApplication.instance().processEvents()
    info["ready"] = w.ready
    info["error"] = w.init_error
    info.update(w.gl_info)
    w.close()
    QApplication.instance().processEvents()
    w.deleteLater()
    return info


def _demo() -> int:
    from PyQt6.QtWidgets import QApplication, QMainWindow

    QSurfaceFormat.setDefaultFormat(orb_surface_format())
    app = QApplication(sys.argv)
    win = QMainWindow()
    win.setWindowTitle("ANO-GPT — orbe GLSL")
    orb = GLSLOrbWidget("")
    win.setCentralWidget(orb)
    win.resize(720, 720)
    win.show()

    def on_key(event):
        mapping = {
            Qt.Key.Key_1: "IDLE",
            Qt.Key.Key_2: "LISTENING",
            Qt.Key.Key_3: "THINKING",
            Qt.Key.Key_4: "SPEAKING",
            Qt.Key.Key_5: "ERROR",
            Qt.Key.Key_6: "ACTING",
        }
        key = event.key()
        if key in mapping:
            orb.speaking = mapping[key] == "SPEAKING"
            orb.state = mapping[key]
        elif key == Qt.Key.Key_Space:
            bands = [0.2 + 0.8 * ((i * 17 + int(time.time() * 10)) % 10) / 10.0 for i in range(8)]
            orb.set_audio_bands(bands)
        elif key == Qt.Key.Key_V:
            orb.set_continuous_vision(not orb.continuous_vision)

    win.keyPressEvent = on_key  # type: ignore[method-assign]
    print("1 idle  2 listen  3 think  4 speak  5 error  6 act  espace FFT  V vision")
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(_demo())
