"""HUMAIN — un vrai visage photo, animé en temps réel par la voix.

Le portrait (``ui/assets/face/portrait.png``, personne fictive) est déformé à
chaque image, sans modèle 3D ni réseau de neurones, en deux passes ``cv2``
(qui calculent en C sans le GIL — la voix n'en souffre pas) :

1. **traits**, dans l'espace de la photo, sur trois petites zones fixes (deux
   yeux avec leur sourcil et le haut de la joue, la bouche avec le menton) :
   sourcils, paupières (la peau de la paupière s'étire sur l'œil, le trait de
   cils reste net), paupière qui se relève, joue qui remonte au sourire, iris
   et pupille, commissures, lèvres, mâchoire. L'ouverture de la bouche laisse
   un vide que l'on remplit d'une cavité peinte (dents, langue, fond sombre) ;
2. **tête**, sur l'image affichée : roulis, respiration, translation, plus un
   champ de relief centré sur le visage (plus fort sur le nez) pour le lacet
   et le tangage.

La bouche suit des **visèmes** tirés du spectre de la voix (bandes
logarithmiques 20 Hz–22 kHz : b3 ≈ 280–660 Hz, b4 ≈ 660–1600 Hz, b5 ≈
1,6–3,8 kHz, b6-7 = sifflantes) : voyelle ouverte (« a »), arrondie
(« o », « ou » : lèvres qui avancent et se resserrent), étirée (« i », « é »),
sifflante (« s », « ch » : dents presque jointes) et lèvres pincées aux
silences (« m », « b », « p »).

Le reste vient de l'observation d'un vrai visage : saccades rapides suivies
par la tête, micro-saccades, paupière qui suit le regard, clignements aux
changements de regard et aux pauses, pupille qui s'ouvre à l'écoute, joues qui
remontent au vrai sourire, légère asymétrie, micro-expressions au repos.

Les champs de déformation sont précalculés : une image ne coûte que quelques
combinaisons linéaires sur de petits tableaux, puis deux ``remap``.

Les repères (yeux, bouche, menton) viennent de ``portrait.json`` : changer de
visage = une photo de face, bouche fermée, fond noir, et ses repères.
"""
from __future__ import annotations

import json
import math
import random
from pathlib import Path

import cv2
import numpy as np
from PyQt6.QtGui import QImage, QPainter

from ui.orb.base import BaseOrb, ease

ASSET_DIR = Path(__file__).resolve().parents[2] / "assets" / "face"

# Expression par état. brow : sourcils levés (+) ou baissés (−) ; sad : bout
# intérieur relevé (+, tristesse) ou froncé (−) ; squint : paupières mi-closes ;
# widen : paupière supérieure relevée (attention) ; pupil : dilatation ;
# smile : sourire (+) / moue (−) ; gaze : regard de base ; roll (degrés),
# pitch, yaw : port de tête.
_EXPRESSIONS = {
    "idle":      dict(brow=0.0, sad=0.0, squint=.05, widen=0.0, pupil=0.0, smile=.12,
                      gaze=(0.0, 0.0), roll=0.0, pitch=0.0, yaw=0.0),
    "listening": dict(brow=.45, sad=0.0, squint=0.0, widen=.45, pupil=.16, smile=.26,
                      gaze=(0.0, -.05), roll=2.2, pitch=-.15, yaw=.10),
    "thinking":  dict(brow=.25, sad=-.35, squint=.18, widen=0.0, pupil=.20, smile=-.10,
                      gaze=(.75, -.85), roll=-1.6, pitch=-.35, yaw=.35),
    "speaking":  dict(brow=.15, sad=0.0, squint=.04, widen=.12, pupil=.08, smile=.16,
                      gaze=(0.0, 0.0), roll=0.0, pitch=0.0, yaw=0.0),
    "acting":    dict(brow=-.45, sad=-.55, squint=.32, widen=0.0, pupil=.05, smile=0.0,
                      gaze=(0.0, .25), roll=0.0, pitch=.20, yaw=-.10),
    "error":     dict(brow=.10, sad=.85, squint=.12, widen=.15, pupil=.10, smile=-.75,
                      gaze=(-.2, .55), roll=-1.2, pitch=.45, yaw=-.15),
}

# Micro-expressions spontanées : amplitude par type.
_MICRO = {"smile": .16, "press": .7, "brow": .45, "squint": .22}

JAW_MAX = 38.0          # ouverture maximale de la mâchoire (px source)
YAW_PX = 16.0           # déplacement du visage à lacet = 1 (px source)
GAZE_PX = (9.0, 5.0)    # course de l'iris (px source)
LASH_PX = 4.0           # liseré de cils gardé intact au bord de la paupière
LASH_TIPS = 11.0        # hauteur des pointes de cils, jamais étirées
WIDEN_PX = 3.2          # recul de la paupière supérieure (attention)
CHEEK_PX = 5.0          # montée de la paupière inférieure au vrai sourire


def _smoothstep(e0: float, e1: float, x: np.ndarray) -> np.ndarray:
    t = np.clip((x - e0) / (e1 - e0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


def _grid(x0: int, x1: int, y0: int, y1: int) -> tuple[np.ndarray, np.ndarray]:
    return np.meshgrid(np.arange(x0, x1, dtype=np.float32),
                       np.arange(y0, y1, dtype=np.float32))


def _box(x0: float, x1: float, y0: float, y1: float, size: int) -> tuple[int, int, int, int]:
    return (max(0, int(x0)), min(size, int(x1)), max(0, int(y0)), min(size, int(y1)))


class PortraitOrb(BaseOrb):
    RADIUS_RATIO = .36
    MAX_SIDE = 720          # plafond de l'image affichée (coût ∝ côté²)

    def __init__(self, face_path: str = "", assistant_name: str = "ANO-GPT", parent=None):
        super().__init__(face_path, assistant_name, parent)
        spec = json.loads((ASSET_DIR / "portrait.json").read_text(encoding="utf-8"))
        self._spec = spec
        self._src, cols = self._load_source(ASSET_DIR / spec["image"])
        self._src_size = self._src.shape[0]
        self._src_cols = cols
        self._face = self._src.copy()
        # Peau de paupière : photo adoucie surtout à l'horizontale, pour
        # qu'une bande étirée ne montre pas de traînées colonne par colonne.
        self._lid_src = cv2.GaussianBlur(self._src, (0, 0), sigmaX=2.5, sigmaY=1.0)
        self._build_eyes()
        self._build_mouth()
        self._rng = random.Random()

        self._p = {key: val for key, val in _EXPRESSIONS["idle"].items() if key != "gaze"}
        self._prev_state = "idle"
        # Bouche : visèmes lissés.
        self._jaw = 0.0
        self._wide = 0.0
        self._round = 0.0
        self._fric = 0.0
        self._press = 0.0
        self._level = 0.0
        self._emph = 0.0
        self._peak = .15
        self._floor = 0.0
        # Yeux.
        self._blink = 0.0
        self._blink_t = -1.0
        self._blink_dur = .25
        self._blink_amp = 1.0
        self._next_blink = 2.0
        self._look = [0.0, 0.0]
        self._jitter = [0.0, 0.0]
        self._next_saccade = .8
        self._next_jitter = .3
        self._gaze = [0.0, 0.0]
        # Tête : cibles lentes (lacet, tangage, roulis en degrés) + hochement.
        self._head = [0.0, 0.0, 0.0]
        self._head_goal = [0.0, 0.0, 0.0]
        self._next_head = 1.5
        self._nod = 0.0
        self._nod_v = 0.0
        self._brow_kick = 0.0
        # Micro-expressions : [type, amplitude, âge, durée].
        self._events: list[list] = []
        self._micro = dict.fromkeys(_MICRO, 0.0)
        self._next_micro = 4.0
        self._asym = 0.0
        self._t = 0.0

        self._side = 0
        self._image: QImage | None = None

    # ── Préparation ─────────────────────────────────────────────────────────
    @staticmethod
    def _load_source(path: Path) -> tuple[np.ndarray, tuple[int, int]]:
        bgr = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if bgr is None:
            raise FileNotFoundError(f"portrait introuvable : {path}")
        h, w = bgr.shape[:2]
        if h != w:
            side = min(h, w)
            bgr = bgr[(h-side)//2:(h-side)//2+side, (w-side)//2:(w-side)//2+side]
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        # Fond noir → transparent ; épaules fondues vers le bas.
        alpha = _smoothstep(.035, .11, rgb.max(axis=2))
        alpha = cv2.GaussianBlur(alpha, (0, 0), 2.0)
        size = rgb.shape[0]
        rows = np.linspace(0.0, 1.0, size, dtype=np.float32)[:, None]
        alpha *= 1.0 - _smoothstep(.80, .97, rows)
        rgba = np.empty((size, size, 4), np.uint8)
        rgba[..., :3] = np.clip(rgb * alpha[..., None] * 255.0 + .5, 0, 255)
        rgba[..., 3] = np.clip(alpha * 255.0 + .5, 0, 255)
        # Colonnes réellement occupées : le reste n'est jamais calculé.
        used = np.flatnonzero(alpha.max(axis=0) > .02)
        cols = (int(used[0]), int(used[-1]) + 1) if used.size else (0, size)
        return rgba, cols

    def _build_eyes(self) -> None:
        size = self._src.shape[0]
        self._eyes = []
        for eye, (bx, by) in zip(self._spec["eyes"], self._spec["brows"]):
            cx, cy, hw = eye["cx"], eye["cy"], eye["half_width"]
            # La zone descend sous l'œil : la joue y pousse la paupière du bas.
            x0, x1, y0, y1 = _box(min(cx - hw, bx - 75) - 30, max(cx + hw, bx + 75) + 30,
                                  by - 50, cy + eye["down"] + 46, size)
            qx, qy = _grid(x0, x1, y0, y1)
            ix = bx + eye["inner"] * 50.0
            gin = np.exp(-(((qx-ix)/34.0)**2 + ((qy-by-4)/24.0)**2))
            u = (qx - cx) / hw
            env = np.sqrt(np.clip(1.0 - u*u, 0.0, 1.0))
            y_up = cy - eye["up"] * env
            # Recul de la paupière supérieure : centré sur son bord, nul sur
            # l'iris au centre de l'œil (qui ne doit pas monter avec elle).
            lid_up = (np.exp(-(((qx-cx)/(hw*.95))**2 + ((qy-(cy-eye["up"]))/10.0)**2))
                      * (1.0 - _smoothstep(cy - eye["up"] * .4, cy, qy)))
            # Joue : pousse la paupière du bas vers le haut, jamais au-dessus
            # du centre de l'œil.
            cheek = (np.exp(-(((qx-cx)/(hw*1.1))**2 + ((qy-(cy+eye["down"]+4))/13.0)**2))
                     * _smoothstep(cy, cy + eye["down"], qy))
            # Cœur de l'œil : seule partie où paupière, iris et pupille bougent
            # (marge pour le sourcil qui soulève la paupière).
            core = (slice(max(0, int(cy - eye["up"] - eye["band"] - 12 - y0)),
                          min(y1 - y0, int(cy + eye["down"] + 6 - y0))),
                    slice(max(0, int(cx - hw - 3 - x0)), min(x1 - x0, int(cx + hw + 3 - x0))))
            self._eyes.append(dict(
                eye=eye, rows=slice(y0, y1), cols=slice(x0, x1), qx=qx, qy=qy, core=core,
                brow=np.exp(-(((qx-bx)/75.0)**2 + ((qy-by)/32.0)**2)),
                sad_y=8.0 * gin, sad_x=-eye["inner"] * 4.0 * gin,
                lid_up=lid_up.astype(np.float32), cheek=cheek.astype(np.float32),
                u=u[core], y_up=y_up[core], cover=((eye["up"] + eye["down"]) * env)[core],
                buf=np.empty((y1 - y0, x1 - x0, 4), np.uint8),
            ))

    def _build_mouth(self) -> None:
        m = self._spec["mouth"]
        mcx, y0, mw = m["cx"], m["y"], m["half_width"]
        chin = self._spec["chin"]
        size = self._src.shape[0]
        x0, x1, ry0, ry1 = _box(mcx - 1.9*mw, mcx + 1.9*mw, y0 - 70, chin + 85, size)
        qx, qy = _grid(x0, x1, ry0, ry1)
        u = (qx - mcx) / mw
        band_y = np.exp(-((qy - y0) / 30.0)**2)
        left = np.exp(-(((qx - (mcx - mw)) / 30.0)**2 + ((qy - y0) / 26.0)**2))
        right = np.exp(-(((qx - (mcx + mw)) / 30.0)**2 + ((qy - y0) / 26.0)**2))
        lens = np.clip(1.0 - u*u, 0.0, 1.0) ** .75
        t = _smoothstep(y0 + 25.0, y0 + 80.0, qy)
        jaw_x = np.exp(-((qx - mcx) / 150.0)**2)
        fall = 1.0 - _smoothstep(chin - 10.0, chin + 80.0, qy)
        drop = (lens * (1.0 - t) + .92 * jaw_x * t) * fall
        lift = .25 * lens * _smoothstep(y0 - 60.0, y0, qy)
        # Lèvres : bande verticale autour de la fente, étroite en largeur.
        mid = y0 + (m["lower"] - m["upper"]) * .25
        lips = (np.exp(-((qy - mid) / (.9 * (m["upper"] + m["lower"])))**2)
                * np.clip(1.0 - (u / 1.15)**2, 0.0, 1.0) ** .5)
        # Sous-zone où la cavité peut apparaître (bouche grande ouverte + sourire).
        c_rows = slice(max(0, int(y0 - .25*JAW_MAX - 18 - ry0)),
                       min(ry1 - ry0, int(y0 + JAW_MAX + 14 - ry0)))
        c_cols = slice(max(0, int(mcx - 1.05*mw - x0)), min(x1 - x0, int(mcx + 1.05*mw - x0)))
        self._mouth = dict(
            rows=slice(ry0, ry1), cols=slice(x0, x1), qx=qx, qy=qy,
            wide_x=-7.0 * u * np.exp(-(u / 1.4)**2) * band_y,
            # Arrondi : commissures ramenées vers le centre.
            round_x=20.0 * u * np.exp(-(u / 1.2)**2) * band_y,
            # Moue : lèvres agrandies verticalement autour de la fente (−) ;
            # le signe inverse les amincit (lèvres pincées).
            pout_y=(-(qy - y0 - 4.0) * lips).astype(np.float32),
            smile_yl=13.0 * left, smile_yr=13.0 * right,
            smile_xl=6.0 * left, smile_xr=6.0 * right,
            jaw_y=np.where(qy > y0, -drop, lift).astype(np.float32),
            c_rows=c_rows, c_cols=c_cols, c_u=u[c_rows, c_cols],
            buf=np.empty((ry1 - ry0, x1 - x0, 4), np.uint8),
        )

    def _rebuild(self, side: int) -> None:
        """Grilles et champs à la taille d'affichage — seulement au redimensionnement."""
        self._side = side
        s = self._src_size / side
        self._scale = s
        # Colonnes affichées : la tête et une marge pour ses mouvements.
        margin = int(side * .06)
        c0 = max(0, int(self._src_cols[0] / s) - margin)
        c1 = min(side, int(self._src_cols[1] / s) + margin)
        self._c0 = c0
        self._X, self._Y = _grid(c0, c1, 0, side)
        self._mx = np.empty_like(self._X)
        self._my = np.empty_like(self._X)
        # Champ de relief : visage large + nez qui avance.
        fx, fy = self._spec["face_center"]
        nx, ny = self._spec["nose"]
        sx, sy = self._X * s, self._Y * s
        field = np.exp(-(((sx-fx)/210.0)**2 + ((sy-fy)/260.0)**2))
        field += .9 * np.exp(-(((sx-nx)/55.0)**2 + ((sy-ny)/70.0)**2))
        self._relief = (field * YAW_PX).astype(np.float32)
        self._out = np.zeros((side, c1 - c0, 4), np.uint8)
        self._image = QImage(self._out.data, c1 - c0, side, (c1 - c0) * 4,
                             QImage.Format.Format_RGBA8888_Premultiplied)

    # ── Simulation ──────────────────────────────────────────────────────────
    def _start_blink(self, amp: float = 1.0) -> None:
        if self._blink_t >= 0.0:
            return
        self._blink_t = 0.0
        self._blink_dur = self._rng.uniform(.21, .30)
        self._blink_amp = amp

    def _add_micro(self, kind: str, amp: float, dur: float) -> None:
        self._events.append([kind, amp, 0.0, dur])

    def _on_state_change(self, old: str, new: str) -> None:
        rng = self._rng
        if new == "listening":
            # « Je t'écoute » : sourcils flashés, regard qui se pose.
            self._brow_kick = min(1.0, self._brow_kick + .8)
            self._look = [0.0, 0.0]
            if rng.random() < .5:
                self._start_blink()
        elif new == "thinking":
            self._next_saccade = 0.0
            if rng.random() < .7:
                self._start_blink()
        elif new == "speaking":
            self._next_head = .2
        elif new == "error":
            self._start_blink()
        elif old == "speaking" and rng.random() < .6:
            self._start_blink()

    def _advance_mouth(self, dt: float, speaking: bool) -> None:
        bands = self.bands
        volume = self.volume
        # Le volume reçu reste haut pendant toute une phrase (il est gonflé
        # par le spectre) : la mâchoire suit donc surtout sa dynamique — creux
        # et pics récents — pour se refermer entre les syllabes.
        self._peak = max(volume, .15, self._peak * math.exp(-dt / 1.2))
        if volume < self._floor:
            self._floor = volume
        else:
            self._floor += (volume - self._floor) * (1.0 - math.exp(-dt * 2.0))
        level = 0.0
        if speaking and volume > .03:
            norm = (volume - self._floor) / max(.05, self._peak - self._floor)
            level = _clamp(.88 * norm + .12 * min(1.0, volume * 2.0)) ** .85
        prev = self._level
        self._level = level
        onset = max(0.0, level - prev)

        jaw = wide = rnd = fric = 0.0
        if level > .02:
            lo, mid, hi = bands[3], bands[4], bands[5]
            sib = .5 * (bands[6] + bands[7])
            voiced = lo + mid + hi + 1e-3
            # Sifflante : l'aigu domine → dents presque jointes, lèvres tirées.
            fric = float(_smoothstep(.35, .65, sib / (voiced + sib)))
            front = hi / (mid + hi + 1e-3)       # i/é (haut) contre o/ou (bas)
            opening = mid / voiced               # a : premier formant haut
            jaw = level * (.8 + .6 * opening) * (1.0 - .55 * fric)
            wide = level * max(-.4, min(1.0, (front - .45) * 2.6)) + .45 * fric
            rnd = level * _clamp((.42 - front) * 3.0) * (1.0 - .8 * opening) * (1.0 - fric)
        # Attaque vive, relâche plus douce : la bouche ne « flotte » pas.
        self._jaw = ease(self._jaw, min(1.0, jaw), dt, 28.0 if jaw > self._jaw else 14.0)
        self._wide = ease(self._wide, wide, dt, 14.0)
        self._round = ease(self._round, rnd, dt, 12.0)
        self._fric = ease(self._fric, fric, dt, 18.0)
        # Occlusive (m, b, p) : la voix retombe net après une syllabe ouverte →
        # les lèvres se pincent un instant.
        if speaking and level < .06 and prev > .22:
            self._press = 1.0
            if self._rng.random() < .12:
                self._start_blink()
        self._press *= math.exp(-dt * 7.0)
        self._emph = ease(self._emph, level, dt, 3.0)

        # Hochements sur les attaques de syllabes, sourcils qui ponctuent.
        if onset > .10:
            self._nod_v += min(1.0, onset * 3.0) * 2.4
            if onset > .22 and self._rng.random() < .5:
                self._brow_kick = min(1.0, self._brow_kick + .5)
        self._nod_v += (-self._nod * 38.0 - self._nod_v * 9.0) * dt
        self._nod += self._nod_v * dt
        self._brow_kick *= math.exp(-dt * 3.0)

    def _advance_eyes(self, dt: float, state: str, expr: dict) -> None:
        rng = self._rng
        engaged = state in ("listening", "speaking")
        # Saccades : saut rapide (≈ 50 ms), la tête suit à moitié, clignement
        # fréquent après un grand déplacement.
        self._next_saccade -= dt
        if self._next_saccade <= 0.0:
            spread = .2 if engaged else .55
            # Face à l'utilisateur, le regard revient souvent au centre.
            if engaged and rng.random() < .45:
                look = [0.0, 0.0]
            else:
                look = [rng.uniform(-spread, spread), rng.uniform(-spread*.6, spread*.6)]
            jump = math.hypot(look[0] - self._look[0], look[1] - self._look[1])
            self._look = look
            if jump > .3 and rng.random() < .35:
                self._start_blink()
            self._head_goal[0] = .5 * self._head_goal[0] + .3 * look[0]
            self._head_goal[1] = .5 * self._head_goal[1] + .2 * look[1]
            self._next_saccade = rng.uniform(1.0, 3.4) if engaged else rng.uniform(.6, 2.6)
        # Micro-saccades : l'œil vivant n'est jamais parfaitement immobile.
        self._next_jitter -= dt
        if self._next_jitter <= 0.0:
            self._jitter = [rng.gauss(0.0, .035), rng.gauss(0.0, .025)]
            self._next_jitter = rng.uniform(.2, .7)
        for i in (0, 1):
            base = expr["gaze"][i]
            goal = base + self._look[i] * (1.0 - abs(base)) + self._jitter[i]
            self._gaze[i] = ease(self._gaze[i], goal, dt, 42.0)

        # Clignements (parfois doublés, parfois partiels), plus fréquents en
        # réflexion : fermeture rapide, réouverture plus lente.
        self._next_blink -= dt
        if self._blink_t < 0.0 and self._next_blink <= 0.0:
            self._start_blink(1.0 if rng.random() > .1 else rng.uniform(.6, .8))
            lo, hi = (1.5, 3.5) if state == "thinking" else (2.5, 6.0)
            self._next_blink = .35 if rng.random() < .12 else rng.uniform(lo, hi)
        if self._blink_t >= 0.0:
            self._blink_t += dt
            k = self._blink_t / self._blink_dur
            if k < .32:
                x = k / .32
                b = x * x * (3.0 - 2.0 * x)
            elif k < .40:
                b = 1.0
            else:
                b = (1.0 - min(1.0, (k - .40) / .60)) ** 2
            self._blink = b * self._blink_amp
            if k >= 1.0:
                self._blink_t = -1.0
                self._blink = 0.0

    def _advance_head(self, dt: float, state: str) -> None:
        rng = self._rng
        self._next_head -= dt
        if self._next_head <= 0.0:
            amp = {"speaking": 1.0, "listening": .55}.get(state, .35)
            self._head_goal = [rng.gauss(0.0, .16) * amp, rng.gauss(0.0, .09) * amp,
                               rng.gauss(0.0, 1.3) * amp]
            self._next_head = rng.uniform(1.2, 3.2) if state == "speaking" else rng.uniform(2.5, 6.0)
        for i, rate in enumerate((2.4, 2.4, 1.8)):
            self._head[i] = ease(self._head[i], self._head_goal[i], dt, rate)

    def _advance_micro(self, dt: float, state: str) -> None:
        rng = self._rng
        self._next_micro -= dt
        if self._next_micro <= 0.0:
            self._next_micro = rng.uniform(3.5, 9.0)
            kinds = ["press", "brow", "squint"]
            if state != "error":
                kinds.append("smile")
            if state == "speaking":
                kinds.remove("press")      # la bouche parle déjà
            kind = rng.choice(kinds)
            self._add_micro(kind, _MICRO[kind] * rng.uniform(.6, 1.0), rng.uniform(.7, 2.0))
        micro = dict.fromkeys(_MICRO, 0.0)
        alive = []
        for ev in self._events:
            ev[2] += dt
            x = ev[2] / ev[3]
            if x < 1.0:
                micro[ev[0]] += ev[1] * math.sin(math.pi * x) ** 2
                alive.append(ev)
        self._events = alive
        self._micro = micro

    def advance(self, dt: float, t: float) -> None:
        state = self.visual_state
        if state != self._prev_state:
            self._on_state_change(self._prev_state, state)
            self._prev_state = state
        expr = _EXPRESSIONS.get(state, _EXPRESSIONS["idle"])
        p = self._p
        for key in p:
            p[key] = ease(p[key], expr[key], dt, 4.5)
        self._advance_mouth(dt, state == "speaking")
        self._advance_eyes(dt, state, expr)
        self._advance_head(dt, state)
        self._advance_micro(dt, state)
        # Asymétrie lente : un visage n'est jamais parfaitement symétrique.
        self._asym = .18 * math.sin(t * .11 + .7) + .08 * math.sin(t * .37)
        self._t = t

    # ── Passe 1 : traits, dans l'espace de la photo ─────────────────────────
    def _render_eyes(self) -> None:
        p, mc = self._p, self._micro
        raise_px = (p["brow"] + .5 * self._brow_kick + mc["brow"]) * 9.0
        smile = p["smile"] + mc["smile"]
        look_down = max(0.0, self._gaze[1])
        look_up = max(0.0, -self._gaze[1])
        # La paupière suit le regard vers le bas ; le vrai sourire plisse l'œil.
        rest = p["squint"] + mc["squint"] + .3 * look_down + .2 * max(0.0, smile - .15)
        close = _clamp(max(self._blink, rest))
        widen_px = WIDEN_PX * _clamp(p["widen"] + .5 * self._brow_kick + .6 * look_up) * (1.0 - close)
        cheek_px = CHEEK_PX * _clamp((smile - .1) * 1.4)
        pupil = p["pupil"] + .03 * math.sin(self._t * .9)
        gx, gy = self._gaze[0] * GAZE_PX[0], self._gaze[1] * GAZE_PX[1]
        for z in self._eyes:
            eye = z["eye"]
            qx = z["qx"] + z["sad_x"] * min(0.0, p["sad"])
            qy = z["qy"] + z["brow"] * raise_px + z["sad_y"] * p["sad"]
            if widen_px > .05:
                qy += z["lid_up"] * widen_px
            if cheek_px > .05:
                qy += z["cheek"] * cheek_px
            # Paupière : la peau au-dessus des cils s'étire vers le bas et le
            # liseré de cils (LASH_PX) glisse sans se déformer. Plus elle ferme,
            # plus la peau vient d'au-dessus des pointes de cils (jamais
            # étirées) et d'une version adoucie de la photo (pas de traînées).
            # Paupière et iris ne touchent que le cœur de l'œil : vues sur qx/qy.
            core = z["core"]
            cqx, cqy = qx[core], qy[core]
            lid = None
            if close > .01:
                band = float(eye["band"])
                top = z["y_up"] - band
                cover = close * z["cover"]
                k = _smoothstep(0.0, 14.0, cover)
                length = band + cover
                rel = cqy - top
                inside = (rel > 0.0) & (rel < length)
                skin = band - LASH_PX - (LASH_TIPS - LASH_PX) * k
                edge = length - LASH_PX
                on_skin = rel < edge
                stretched = np.where(on_skin, top + rel * skin / np.maximum(edge, 1e-3),
                                     z["y_up"] - (length - rel))
                np.copyto(cqy, stretched.astype(np.float32), where=inside)
                lid = (inside & on_skin) * k * .65 * _smoothstep(0.0, 7.0, rel)
                depth = np.clip(rel / np.maximum(edge, 1e-3), 0.0, 1.0)
                # Paupière bombée : pli d'ombre en haut, reflet au milieu,
                # s'assombrit vers les cils.
                lid_shade = ((1.0 - .16 * depth * depth) * (1.0 + .07 * np.sin(np.pi * depth))
                             * (1.0 - .12 * np.exp(-((rel - 3.0) / 2.5) ** 2) * k))
            # Iris et pupille : ils glissent dans l'ouverture, les paupières
            # restent en place ; la pupille s'ouvre en agrandissant le centre.
            if abs(gx) + abs(gy) > .05 or abs(pupil) > .01:
                cx, cy = eye["cx"], eye["cy"]
                dx, dy = cqx - (cx + gx), cqy - (cy + gy)
                ri = float(eye["iris"])
                r = np.sqrt(dx*dx + dy*dy)
                w = np.clip(1.0 - (r - ri) / (ri * .8), 0.0, 1.0)
                vy = (cqy - cy) / np.where(cqy < cy, eye["up"], eye["down"])
                w *= np.clip((1.0 - (z["u"]**2 + vy*vy)) / .35, 0.0, 1.0)
                dil = pupil * np.clip(1.0 - r / (ri * .8), 0.0, 1.0) ** 1.5 * w
                cqx -= gx * w + dx * dil
                cqy -= gy * w + dy * dil
            cv2.remap(self._src, qx, qy, cv2.INTER_LINEAR, dst=z["buf"])
            if lid is not None and lid.any():
                soft = cv2.remap(self._lid_src, np.ascontiguousarray(cqx),
                                 np.ascontiguousarray(cqy), cv2.INTER_LINEAR)
                m = lid[..., None]
                shade = np.ones_like(soft, dtype=np.float32)
                shade[..., :3] = lid_shade[..., None]
                out = z["buf"][core]
                out[...] = (out * (1.0 - m) + soft * shade * m).astype(np.uint8)
            self._face[z["rows"], z["cols"]] = z["buf"]

    def _render_mouth(self) -> None:
        z = self._mouth
        p, mc = self._p, self._micro
        smile = p["smile"] + mc["smile"]
        press = min(1.0, self._press + mc["press"])
        rnd = self._round
        a = self._asym
        wide = self._wide - .15 * min(0.0, smile)
        j = self._jaw * JAW_MAX * (1.0 - .8 * press)
        pos = max(0.0, smile)
        # Seuls les termes actifs sont calculés (zone de ~100 000 px).
        qx = z["qx"] + z["wide_x"] * wide if abs(wide) > .005 else z["qx"].copy()
        if rnd > .005:
            qx += z["round_x"] * rnd
        if pos > .005:
            qx += z["smile_xl"] * ((1.0 + a) * pos)
            qx -= z["smile_xr"] * ((1.0 - a) * pos)
        qs = z["qy"].copy()
        if abs(smile) > .005:
            qs += z["smile_yl"] * ((1.0 + a) * smile)
            qs += z["smile_yr"] * ((1.0 - a) * smile)
        pout = .26 * rnd - .40 * press
        if abs(pout) > .005:
            qs += z["pout_y"] * pout
        qy = qs + z["jaw_y"] * j if j > .4 else qs
        cv2.remap(self._src, qx.astype(np.float32, copy=False),
                  qy.astype(np.float32, copy=False), cv2.INTER_LINEAR, dst=z["buf"])
        if j > .4:
            self._paint_cavity(qs[z["c_rows"], z["c_cols"]], j, rnd, wide)
        self._face[z["rows"], z["cols"]] = z["buf"]

    def _paint_cavity(self, qy: np.ndarray, j: float, rnd: float, wide: float) -> None:
        z = self._mouth
        y0 = self._spec["mouth"]["y"]
        fric = self._fric
        # Bouche arrondie : ouverture plus étroite et plus ronde.
        ws = max(.45, 1.0 - .50 * rnd + .08 * max(0.0, wide))
        u = z["c_u"] / ws
        au = np.abs(u)
        lens = np.clip(1.0 - u*u, 0.0, 1.0) ** (.75 - .25 * rnd)
        # La lèvre du haut se relève un peu sur les sifflantes (dents visibles).
        top = y0 - (.25 * j + 2.5 * fric) * lens
        bottom = y0 + j * lens
        depth = np.minimum(qy - top, bottom - qy)
        aa = np.clip(depth / 1.6 + .45, 0.0, 1.0) * (lens > .02)
        # Tout le reste ne se calcule que sur le cadre où la bouche est ouverte.
        rows = np.flatnonzero(aa.any(axis=1))
        if not rows.size:
            return
        cols = np.flatnonzero(aa.any(axis=0))
        box = (slice(rows[0], rows[-1] + 1), slice(cols[0], cols[-1] + 1))
        qy, au, top, bottom, depth, aa = (arr[box] for arr in (qy, au, top, bottom, depth, aa))
        rel = qy - top
        relb = bottom - qy
        frac = rel / np.maximum(bottom - top, 1.0)
        # Fond de bouche : jamais noir pur, plus sombre au fond et vers les
        # coins (la joue le cache), langue rosée en bas.
        corner = _smoothstep(.35, .95, au)
        dark = (.45 + .55 * np.clip(depth / 6.0, 0.0, 1.0)) * (1.0 - .45 * corner)
        tongue = (_smoothstep(.55, 1.0, frac) * min(1.0, j / 20.0) * (1.0 - .6 * fric)
                  * (1.0 - corner))
        r = (40.0 + 100.0 * tongue) * dark
        g = (15.0 + 36.0 * tongue) * dark
        b = (17.0 + 38.0 * tongue) * dark
        # Face interne humide des lèvres : liseré rouge sombre sur les bords
        # de la lèvre du bas, qui fond la cavité dans la photo.
        wet = .7 * np.clip(1.0 - relb / 3.0, 0.0, 1.0) ** 1.5
        r += wet * (122.0 - r)
        g += wet * (52.0 - g)
        b += wet * (54.0 - b)
        # Dents : une par une (bord libre arrondi, interstices doux), plus
        # courtes et plus sombres en suivant l'arcade vers les coins.
        pos = np.interp(au, (0.0, .19, .35, .50, .63, .75, .87), (0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0))
        f = pos - np.floor(pos)
        edge_c = (2.0 * f - 1.0) ** 4           # 1 entre deux dents, 0 au milieu
        gaps = np.exp(-(np.minimum(f, 1.0 - f) / .06) ** 2)
        arch = 1.0 - .55 * _smoothstep(.2, .85, au)
        tone = (.72 + .28 * min(1.0, j / 14.0)) * (1.0 - .22 * gaps) * (1.0 - .8 * corner)
        teeth_x = np.clip((.82 - au) / .2, 0.0, 1.0)
        teeth_h = min(10.0, .5 * j + 4.0 * fric)
        if teeth_h > .3:
            h = teeth_h * arch - 1.4 * edge_c
            tooth = np.clip((h - rel) / 1.1, 0.0, 1.0) * teeth_x
            v = np.clip(rel / np.maximum(h, 1.0), 0.0, 1.0)
            # Ombre portée de la lèvre en haut, bord libre un peu translucide.
            shade = tone * (.50 + .50 * _smoothstep(0.0, .45, v)) * (1.0 - .14 * _smoothstep(.7, 1.0, v))
            r += tooth * (226.0 * shade - r)
            g += tooth * (214.0 * shade - g)
            b += tooth * (194.0 * shade - b)
        # Dents du bas : plus en retrait, plus sombres, cachées par la lèvre
        # tant que la bouche n'est pas bien ouverte (sauf sur les sifflantes).
        low_h = min(6.0, max(0.0, .3 * j - 9.0) + 3.5 * fric)
        if low_h > .3:
            hb = low_h * arch - 1.0 * edge_c
            lip = 2.5                             # la lèvre du bas les recouvre
            low = (np.clip((hb + lip - relb) / 1.1, 0.0, 1.0)
                   * _smoothstep(lip - 1.0, lip + 1.0, relb)
                   * np.clip((.6 - au) / .18, 0.0, 1.0))
            lshade = tone * .58 * (.7 + .3 * np.clip((relb - lip) / np.maximum(hb, 1.0), 0.0, 1.0))
            r += low * (216.0 * lshade - r)
            g += low * (204.0 * lshade - g)
            b += low * (186.0 * lshade - b)
        out = z["buf"][z["c_rows"], z["c_cols"]][box]
        a = aa[..., None]
        color = np.stack((r, g, b, np.full_like(r, 255.0)), axis=-1)
        out[...] = (out * (1.0 - a) + color * a).astype(np.uint8)

    # ── Passe 2 : tête, à la taille d'affichage ─────────────────────────────
    def paint_orb(self, p: QPainter, cx: float, cy: float, radius: float, t: float) -> None:
        side = min(self.MAX_SIDE, int(radius * 2.8)) & ~1
        if side < 64:
            return
        if side != self._side:
            self._rebuild(side)
        self._render_eyes()
        self._render_mouth()

        prm, tt, s = self._p, self._t, self._scale
        hy, hp, hr = self._head
        roll = math.radians(prm["roll"] + hr + .5 * math.sin(tt * .21) + .3 * math.sin(tt * .53 + 1.3))
        breath = 1.0 + .006 * math.sin(tt * 1.25)
        yaw = prm["yaw"] + hy + .08 * math.sin(tt * .17 + .4) + .04 * math.sin(tt * .47)
        pitch = (prm["pitch"] + hp + .06 * math.sin(tt * .29 + 2.0)
                 + self._nod * .35 - .14 * self._emph)
        px, py = self._spec["pivot"]
        dst_x = px / s + side * .006 * math.sin(tt * .13)
        dst_y = py / s + side * (.004 * math.sin(tt * .31 + 1.0) + .012 * self._nod)
        # Affinité inverse : pixel affiché → pixel photo.
        a = s / breath
        c, sn = math.cos(roll) * a, math.sin(roll) * a
        ox = px - (c * dst_x + sn * dst_y)
        oy = py - (-sn * dst_x + c * dst_y)
        mx, my = self._mx, self._my
        cv2.addWeighted(self._X, c, self._Y, sn, ox, dst=mx)
        cv2.addWeighted(self._X, -sn, self._Y, c, oy, dst=my)
        if abs(yaw) > .005:
            cv2.scaleAdd(self._relief, -yaw, mx, dst=mx)
        if abs(pitch) > .005:
            cv2.scaleAdd(self._relief, -.8 * pitch, my, dst=my)
        cv2.remap(self._face, mx, my, cv2.INTER_LINEAR, dst=self._out,
                  borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))
        p.drawImage(int(cx - side / 2) + self._c0, int(cy - side / 2), self._image)
