"""Base des styles d'orbe : tout le commun, rien que le dessin à écrire.

Un style hérite de ``BaseOrb`` et implémente deux méthodes :

* ``advance(dt, t)`` — fait évoluer sa simulation (appelé à chaque image) ;
* ``paint_orb(p, cx, cy, radius, t)`` — dessine l'orbe centré en (cx, cy).

``BaseOrb`` s'occupe du reste, identique pour tous :

* contrat de ``ui.orb.contract`` (état, parole, muet, volume, bandes FFT,
  couleur d'accent, geste, vision continue, calque photo) ;
* lissage du volume, énergie par état, palette vivante interpolée ;
* cadence adaptative : coût mesuré de chaque image, ralentissement pendant la
  voix et dès que la boucle audio prend du retard (thread Qt et voix partagent
  le GIL), plafond sur batterie ;
* sommeil : caché, réduit ou ``set_low_power`` → la minuterie tombe à 2 Hz et
  ``paint_orb`` n'est plus appelé ; ``shutdown`` arrête tout ;
* coupe-circuit : une exception dans ``advance`` ou ``paint_orb`` suspend
  l'animation de ce style sans jamais tuer la session vocale.
"""
from __future__ import annotations

import math
import sys
import time

import psutil
from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QPainter
from PyQt6.QtWidgets import QSizePolicy, QWidget

from core import freeze_watch
from ui.orb.contract import ORB_STATES, clamp_bands, visual_state


def _c(hex_color: str) -> QColor:
    return QColor(hex_color)


# core : cœur lumineux · halo : lueur externe · wire : traits · hot : points chauds
DEFAULT_PALETTES: dict[str, dict] = {
    "idle":      {"core": _c("#4ca8e8"), "halo": _c("#4ca8e8"), "wire": _c("#4ca8e8"),
                  "hot": _c("#b8eeff"), "pulse_speed": 0.85, "spin": 1.0},
    "listening": {"core": _c("#64ffc3"), "halo": _c("#00c387"), "wire": _c("#00f5b4"),
                  "hot": _c("#e8fff6"), "pulse_speed": 1.9, "spin": 1.7},
    "thinking":  {"core": _c("#b48cff"), "halo": _c("#7a5cff"), "wire": _c("#9d7bff"),
                  "hot": _c("#f0e8ff"), "pulse_speed": 1.4, "spin": 2.2},
    "speaking":  {"core": _c("#5ab8f0"), "halo": _c("#5ab8f0"), "wire": _c("#00d4ff"),
                  "hot": _c("#e1faff"), "pulse_speed": 2.4, "spin": 1.5},
    "acting":    {"core": _c("#ffc864"), "halo": _c("#ff9d00"), "wire": _c("#ffb200"),
                  "hot": _c("#fff4dc"), "pulse_speed": 2.0, "spin": 2.6},
    "error":     {"core": _c("#ff5a6e"), "halo": _c("#ff3355"), "wire": _c("#ff3355"),
                  "hot": _c("#ffe1e6"), "pulse_speed": 1.2, "spin": 0.6},
}

# Énergie cible par état : 0 = repos, 1 = pleinement actif.
STATE_ENERGY = {
    "idle": 0.15, "listening": 0.7, "thinking": 0.85,
    "speaking": 1.0, "acting": 1.2, "error": 0.6,
}


def ease(current: float, target: float, dt: float, rate: float) -> float:
    """Approche exponentielle indépendante de la cadence."""
    return current + (target - current) * (1.0 - math.exp(-rate * dt))


def mix(a: QColor, b: QColor, f: float) -> QColor:
    f = max(0.0, min(1.0, f))
    return QColor(
        int(a.red() + (b.red() - a.red()) * f),
        int(a.green() + (b.green() - a.green()) * f),
        int(a.blue() + (b.blue() - a.blue()) * f),
        int(a.alpha() + (b.alpha() - a.alpha()) * f),
    )


def with_alpha(color: QColor, alpha: float) -> QColor:
    out = QColor(color)
    out.setAlphaF(max(0.0, min(1.0, alpha)))
    return out


class BaseOrb(QWidget):
    """Orbe plein cadre transparent : le fond (couleur ou photo) reste visible."""

    # ── Réglages de cadence, surchargeables par style ───────────────────────
    FRAME_MS = 33              # ~30 i/s au repos
    FRAME_MS_VOICE = 40        # pendant écoute/parole : laisser le GIL à la voix
    FRAME_MS_BATTERY = 40
    FRAME_MS_MAX = 80
    FRAME_MS_SLEEP = 500       # caché ou en veille : battement lent seulement
    FRAME_BUDGET = 0.40        # part max du thread Qt qu'une image peut prendre
    AUDIO_LAG_S = 0.8          # retard de la boucle audio qui déclenche le frein
    AUDIO_LAG_HOLD_S = 2.0
    # Rayon de l'orbe rapporté au plus petit côté du widget.
    RADIUS_RATIO = 0.22

    def __init__(self, face_path: str = "", assistant_name: str = "ANO-GPT", parent=None):
        super().__init__(parent)
        self.setMinimumSize(300, 300)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)
        self.setAutoFillBackground(False)

        self._face_path = face_path
        self._assistant_name = assistant_name
        self._muted = False
        self._speaking = False
        self._state = "IDLE"
        self._ws = "idle"
        self._background_photo_active = False
        self._continuous_vision_active = False

        # Valeurs lues aussi par MiniOrbOverlay / CompanionOrb.
        self._PALETTES = {
            state: {k: QColor(v) if isinstance(v, QColor) else v for k, v in pal.items()}
            for state, pal in DEFAULT_PALETTES.items()
        }
        self._volume = 0.0
        self._target_vol = 0.0
        self._energy = STATE_ENERGY["idle"]
        self._last_ext_vol_t = 0.0

        # Bandes FFT lissées (8) ; ``bands_live`` indique un vrai spectre.
        self._bands = [0.0] * 8
        self._target_bands = [0.0] * 8
        self._last_bands_t = 0.0

        # Palette interpolée vers l'état courant : jamais de saut de couleur.
        idle = self._PALETTES["idle"]
        self._pal_live = {k: QColor(idle[k]) for k in ("core", "halo", "wire", "hot")}

        self._gesture_icon: str | None = None
        self._gesture_label = ""
        self._gesture_val = 0.0
        self._gesture_expires = 0.0

        self._t0 = time.monotonic()
        self._last_tick = self._t0
        self._low_power = False
        self._on_battery = False
        self._throttle_until = 0.0
        self._sim_ms = 1.0
        self._paint_ms = 2.0
        self._broken = False
        self._alive = True

        self._interval = self.FRAME_MS
        self._anim_tmr = QTimer(self)
        self._anim_tmr.setTimerType(Qt.TimerType.PreciseTimer)
        self._anim_tmr.timeout.connect(self._tick)
        self._anim_tmr.start(self._interval)

        self._batt_tmr = QTimer(self)
        self._batt_tmr.timeout.connect(self._check_battery)
        self._batt_tmr.start(15000)
        self._check_battery()

    # ══ À implémenter par chaque style ══════════════════════════════════════
    def advance(self, dt: float, t: float) -> None:
        """Fait évoluer la simulation propre au style (pas de dessin ici)."""

    def paint_orb(self, p: QPainter, cx: float, cy: float, radius: float, t: float) -> None:
        raise NotImplementedError

    # ══ Accès utiles aux styles ═════════════════════════════════════════════
    @property
    def palette_live(self) -> dict[str, QColor]:
        return self._pal_live

    @property
    def visual_state(self) -> str:
        return self._ws

    @property
    def volume(self) -> float:
        return self._volume

    @property
    def energy(self) -> float:
        return self._energy

    @property
    def bands(self) -> list[float]:
        return self._bands

    @property
    def spin(self) -> float:
        return float(self._PALETTES.get(self._ws, self._PALETTES["idle"]).get("spin", 1.0))

    @property
    def pulse_speed(self) -> float:
        pal = self._PALETTES.get(self._ws, self._PALETTES["idle"])
        return float(pal.get("pulse_speed", 1.0))

    # ══ Contrat ═════════════════════════════════════════════════════════════
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
        self._state = str(v or "IDLE")
        self._update_ws()

    def _update_ws(self) -> None:
        ws = visual_state(self._state, self._speaking, self._muted)
        self._ws = ws if ws in ORB_STATES else "idle"
        self._apply_interval()

    def set_assistant_name(self, name: str) -> None:
        self._assistant_name = str(name or "")

    def set_volume(self, v: float) -> None:
        try:
            v = float(v)
        except (TypeError, ValueError):
            return
        if not math.isfinite(v):
            return
        self._target_vol = max(0.0, min(1.0, v))
        self._last_ext_vol_t = time.monotonic()
        if self._dormant():
            # La bulle compagnon lit ``_volume`` même quand l'orbe dort.
            self._volume = self._target_vol

    def set_audio_bands(self, bands) -> None:
        self._target_bands = clamp_bands(bands, 8)
        self._last_bands_t = time.monotonic()
        self._target_vol = max(self._target_vol, sum(self._target_bands) / 8.0)
        self._last_ext_vol_t = self._last_bands_t

    def set_low_power(self, low: bool) -> None:
        low = bool(low)
        if low != self._low_power:
            self._low_power = low
            self._apply_interval()

    def set_background_image_active(self, active: bool) -> None:
        self._background_photo_active = bool(active)
        self.update()

    def set_accent_color(self, accent_hex: str, custom_palette: dict | None = None) -> None:
        """Même règle que l'orbe principal : l'accent teinte les états calmes."""
        states = ("idle", "listening", "acting", "thinking")
        if custom_palette:
            for state in states:
                for key in ("core", "halo", "wire", "hot"):
                    if key in custom_palette:
                        color = QColor(custom_palette[key])
                        if color.isValid():
                            self._PALETTES[state][key] = color
            for key in ("pulse_speed", "spin"):
                if key in custom_palette:
                    try:
                        self._PALETTES["idle"][key] = float(custom_palette[key])
                    except (TypeError, ValueError):
                        pass
        elif accent_hex:
            base = QColor(accent_hex)
            if base.isValid():
                r, g, b = base.red(), base.green(), base.blue()
                for state in states:
                    pal = self._PALETTES[state]
                    pal["wire"] = QColor(r, g, b)
                    pal["halo"] = QColor(max(0, r - 35), max(0, g - 35), max(0, b - 35))
                    pal["core"] = QColor(min(255, r + 50), min(255, g + 50), min(255, b + 50))
                    pal["hot"] = QColor(min(255, r + 90), min(255, g + 90), min(255, b + 90))
        self.on_palette_changed()
        self.update()

    def on_palette_changed(self) -> None:
        """À surcharger pour invalider un cache de sprites teintés."""

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

    def shutdown(self) -> None:
        """Arrêt définitif : plus aucune minuterie, plus aucun calcul."""
        self._alive = False
        self._anim_tmr.stop()
        self._batt_tmr.stop()

    # ══ Cadence ═════════════════════════════════════════════════════════════
    def _check_battery(self) -> None:
        try:
            battery = psutil.sensors_battery()
            self._on_battery = bool(battery and not battery.power_plugged)
        except Exception:
            self._on_battery = False
        self._apply_interval()

    def _dormant(self) -> bool:
        return self._low_power or not self.isVisible()

    def _desired_interval(self) -> int:
        if self._dormant():
            return self.FRAME_MS_SLEEP
        voice = self._ws in ("listening", "speaking")
        base = self.FRAME_MS
        if self._on_battery:
            base = max(base, self.FRAME_MS_BATTERY)
        if voice:
            base = max(base, self.FRAME_MS_VOICE)
        cost = self._sim_ms + self._paint_ms
        want = max(base, int(cost / self.FRAME_BUDGET))
        now = time.monotonic()
        if freeze_watch.lag("boucle audio") > self.AUDIO_LAG_S:
            self._throttle_until = now + self.AUDIO_LAG_HOLD_S
        if now < self._throttle_until:
            return self.FRAME_MS_MAX
        return min(self.FRAME_MS_MAX, want)

    def _apply_interval(self) -> None:
        if not self._alive or self._broken:
            return
        want = self._desired_interval()
        if abs(want - self._interval) >= 3 or want == self.FRAME_MS_SLEEP:
            self._interval = want
            self._anim_tmr.setInterval(want)
        if not self._anim_tmr.isActive():
            self._anim_tmr.start(self._interval)

    def showEvent(self, event) -> None:
        self._last_tick = time.monotonic()
        super().showEvent(event)
        self._apply_interval()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._apply_interval()

    # ══ Boucle ══════════════════════════════════════════════════════════════
    def _suspend(self, where: str, exc: BaseException) -> None:
        # PyQt tue le processus si une exception sort d'un slot : on isole.
        self._broken = True
        self._anim_tmr.stop()
        print(
            f"[Orbe:{type(self).__name__}] {where} suspendu : {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )

    def _tick(self) -> None:
        if self._broken or not self._alive:
            return
        try:
            started = time.monotonic()
            dt = started - self._last_tick
            self._last_tick = started
            if dt <= 0.0 or dt > 0.25:
                dt = self._interval / 1000.0
            self._step_common(dt, started)
            if not self._dormant():
                self.advance(dt, started - self._t0)
                self.update()
            self._sim_ms += ((time.monotonic() - started) * 1000.0 - self._sim_ms) * 0.1
            self._apply_interval()
        except Exception as exc:
            self._suspend("animation", exc)

    def _step_common(self, dt: float, now: float) -> None:
        live = (now - self._last_ext_vol_t) < 0.3
        if not live:
            # Pas de flux audio : volume de synthèse cohérent avec l'état.
            if self._ws == "speaking":
                self._target_vol = 0.30 + 0.70 * (0.5 + 0.5 * math.sin(now * 7.1))
            elif self._ws == "listening":
                self._target_vol = 0.06 + 0.14 * (0.5 + 0.5 * math.sin(now * 3.4))
            else:
                self._target_vol = max(0.0, self._target_vol - 0.05)
        attack = 0.74 if self._target_vol > self._volume else 0.14
        self._volume += (self._target_vol - self._volume) * (1.0 - (1.0 - attack) ** (dt * 50.0))
        self._energy = ease(self._energy, STATE_ENERGY.get(self._ws, 0.15), dt, 3.2)

        if (now - self._last_bands_t) >= 0.3:
            v = self._volume
            self._target_bands = [
                max(0.0, min(1.0, v * (1.0 - i * 0.09)
                             * (0.55 + 0.45 * math.sin(now * (2.4 + i * 0.47) + i * 1.31))))
                for i in range(8)
            ]
        for i in range(8):
            cur, tgt = self._bands[i], self._target_bands[i]
            k = 0.65 if tgt > cur else 0.22
            self._bands[i] = cur + (tgt - cur) * (1.0 - (1.0 - k) ** (dt * 50.0))

        if self._dormant():
            return
        target = self._PALETTES.get(self._ws, self._PALETTES["idle"])
        f = 1.0 - math.exp(-4.0 * dt)
        for key in ("core", "halo", "wire", "hot"):
            self._pal_live[key] = mix(self._pal_live[key], target[key], f)

    # ══ Peinture ════════════════════════════════════════════════════════════
    def paintEvent(self, _event) -> None:
        if self._broken or self._dormant():
            return
        started = time.monotonic()
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            w, h = self.width(), self.height()
            cx, cy = w / 2.0, h / 2.0
            radius = min(w, h) * self.RADIUS_RATIO
            t = started - self._t0
            self.paint_orb(p, cx, cy, radius, t)
            self._paint_overlays(p, cx, cy, radius, started)
        except Exception as exc:
            self._suspend("peinture", exc)
        finally:
            p.end()
        ms = (time.monotonic() - started) * 1000.0
        self._paint_ms += (ms - self._paint_ms) * 0.1

    def _paint_overlays(self, p: QPainter, cx: float, cy: float, radius: float,
                        now: float) -> None:
        """Badges communs à tous les styles : geste reconnu, vision continue."""
        wire = self._pal_live["wire"]
        if self._gesture_icon and now < self._gesture_expires:
            fade = min(1.0, (self._gesture_expires - now) / 0.4)
            p.setPen(with_alpha(self._pal_live["hot"], 0.95 * fade))
            p.setFont(QFont("Inter", max(10, int(radius * 0.16)), QFont.Weight.Bold))
            text = f"{self._gesture_icon}  {self._gesture_label}".strip()
            p.drawText(QRectF(cx - radius * 1.5, cy + radius * 1.12, radius * 3.0, radius * 0.4),
                       Qt.AlignmentFlag.AlignCenter, text)
        if self._continuous_vision_active:
            r = max(5.0, radius * 0.05)
            center = QPointF(cx + radius * 0.92, cy - radius * 0.92)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(with_alpha(wire, 0.25))
            p.drawEllipse(center, r * 2.0, r * 2.0)
            p.setBrush(with_alpha(wire, 0.95))
            p.drawEllipse(center, r, r)
