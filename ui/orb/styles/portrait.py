"""HUMAIN — un vrai visage photo, animé en temps réel par la voix.

Le portrait (``ui/assets/face/portrait.png``, personne fictive) est déformé à
chaque image, sans modèle 3D ni réseau de neurones, en deux passes ``cv2``
(qui calculent en C sans le GIL — la voix n'en souffre pas) :

1. **traits**, dans l'espace de la photo, sur trois petites zones fixes (deux
   yeux avec leur sourcil, la bouche avec le menton) : sourcils, paupières
   (la peau de la paupière s'étire sur l'œil, le trait de cils reste net),
   iris, commissures, sourire, mâchoire. L'ouverture de la bouche laisse un
   vide que l'on remplit d'une cavité peinte (dents du haut, fond sombre) ;
2. **tête**, sur l'image affichée : roulis, respiration, translation, plus un
   champ de relief centré sur le visage (plus fort sur le nez) pour le lacet
   et le tangage.

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
# smile : sourire (+) / moue (−) ; gaze : regard de base ; roll (degrés),
# pitch, yaw : port de tête.
_EXPRESSIONS = {
    "idle":      dict(brow=0.0, sad=0.0, squint=.05, smile=.12, gaze=(0.0, 0.0),
                      roll=0.0, pitch=0.0, yaw=0.0),
    "listening": dict(brow=.55, sad=0.0, squint=0.0, smile=.28, gaze=(0.0, -.05),
                      roll=2.2, pitch=-.15, yaw=.10),
    "thinking":  dict(brow=.25, sad=-.35, squint=.18, smile=-.10, gaze=(.75, -.85),
                      roll=-1.6, pitch=-.35, yaw=.35),
    "speaking":  dict(brow=.20, sad=0.0, squint=.04, smile=.18, gaze=(0.0, 0.0),
                      roll=0.0, pitch=0.0, yaw=0.0),
    "acting":    dict(brow=-.45, sad=-.55, squint=.32, smile=0.0, gaze=(0.0, .25),
                      roll=0.0, pitch=.20, yaw=-.10),
    "error":     dict(brow=.10, sad=.85, squint=.12, smile=-.75, gaze=(-.2, .55),
                      roll=-1.2, pitch=.45, yaw=-.15),
}

JAW_MAX = 34.0          # ouverture maximale de la mâchoire (px source)
YAW_PX = 16.0           # déplacement du visage à lacet = 1 (px source)
GAZE_PX = (9.0, 5.0)    # course de l'iris (px source)
LASH_PX = 4.0           # liseré de cils gardé intact au bord de la paupière
LASH_TIPS = 11.0        # hauteur des pointes de cils, jamais étirées
# Limites entre dents (fraction de la demi-largeur de bouche).
_TOOTH_EDGES = (0.0, .19, .35, .50, .63)


def _smoothstep(e0: float, e1: float, x: np.ndarray) -> np.ndarray:
    t = np.clip((x - e0) / (e1 - e0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


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
        self._lid_src = cv2.GaussianBlur(self._src, (0, 0), sigmaX=4.0, sigmaY=1.2)
        self._build_eyes()
        self._build_mouth()
        self._rng = random.Random()

        self._p = {key: val for key, val in _EXPRESSIONS["idle"].items() if key != "gaze"}
        self._jaw = 0.0
        self._wide = 0.0
        self._blink = 0.0
        self._blink_t = -1.0
        self._next_blink = 2.0
        self._look = [0.0, 0.0]
        self._next_saccade = .8
        self._gaze = [0.0, 0.0]
        self._nod = 0.0
        self._nod_v = 0.0
        self._last_volume = 0.0
        self._brow_kick = 0.0
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
            x0, x1, y0, y1 = _box(min(cx - hw, bx - 75) - 30, max(cx + hw, bx + 75) + 30,
                                  by - 50, cy + eye["down"] + 14, size)
            qx, qy = _grid(x0, x1, y0, y1)
            ix = bx + eye["inner"] * 50.0
            gin = np.exp(-(((qx-ix)/34.0)**2 + ((qy-by-4)/24.0)**2))
            u = (qx - cx) / hw
            env = np.sqrt(np.clip(1.0 - u*u, 0.0, 1.0))
            y_up = cy - eye["up"] * env
            self._eyes.append(dict(
                eye=eye, rows=slice(y0, y1), cols=slice(x0, x1), qx=qx, qy=qy,
                brow=np.exp(-(((qx-bx)/75.0)**2 + ((qy-by)/32.0)**2)),
                sad_y=8.0 * gin, sad_x=-eye["inner"] * 4.0 * gin,
                u=u, y_up=y_up, cover=(eye["up"] + eye["down"]) * env,
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
        # Sous-zone où la cavité peut apparaître (bouche grande ouverte + sourire).
        c_rows = slice(max(0, int(y0 - .25*JAW_MAX - 14 - ry0)),
                       min(ry1 - ry0, int(y0 + JAW_MAX + 14 - ry0)))
        c_cols = slice(max(0, int(mcx - 1.05*mw - x0)), min(x1 - x0, int(mcx + 1.05*mw - x0)))
        au = np.abs(u[c_rows, c_cols])
        gaps = sum(np.exp(-((au - edge) / .012)**2) for edge in _TOOTH_EDGES)
        self._mouth = dict(
            rows=slice(ry0, ry1), cols=slice(x0, x1), qx=qx, qy=qy,
            wide_x=-7.0 * u * np.exp(-(u / 1.4)**2) * band_y,
            smile_y=9.0 * (left + right), smile_x=4.0 * (left - right),
            jaw_y=np.where(qy > y0, -drop, lift).astype(np.float32),
            c_rows=c_rows, c_cols=c_cols, c_lens=lens[c_rows, c_cols],
            c_teeth_x=np.clip((.75 - au) / .15, 0.0, 1.0),
            c_teeth_shade=(.80 + .20 * np.clip(1.0 - au * 1.3, 0.0, 1.0)) * (1.0 - .35 * gaps),
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
    def advance(self, dt: float, t: float) -> None:
        state = self.visual_state
        expr = _EXPRESSIONS.get(state, _EXPRESSIONS["idle"])
        p = self._p
        for key in p:
            p[key] = ease(p[key], expr[key], dt, 4.5)

        volume = self.volume
        bands = self.bands
        speaking = state == "speaking"
        onset = max(0.0, volume - self._last_volume)
        self._last_volume = volume

        # Mâchoire : suit l'enveloppe de la voix, attaque vive, relâche douce.
        low = (bands[0] + bands[1] + bands[2]) / 3.0
        high = (bands[5] + bands[6] + bands[7]) / 3.0
        target = 0.0
        if speaking:
            target = min(1.0, max(0.0, volume - .04) * 1.7) ** .8
            target *= .75 + .45 * min(1.0, low * 1.5)
        self._jaw = ease(self._jaw, min(1.0, target), dt, 26.0 if target > self._jaw else 13.0)
        wide = max(-1.0, min(1.0, (high - low) * 2.2)) * .7 if speaking else 0.0
        self._wide = ease(self._wide, wide, dt, 10.0)

        # Hochements sur les attaques de syllabes, sourcils qui ponctuent.
        if speaking and onset > .09:
            self._nod_v += min(1.0, onset * 3.0) * 2.4
            if onset > .16:
                self._brow_kick = min(1.0, self._brow_kick + .6)
        self._nod_v += (-self._nod * 38.0 - self._nod_v * 9.0) * dt
        self._nod += self._nod_v * dt
        self._brow_kick *= math.exp(-dt * 3.0)

        # Regard : direction de l'état + saccades ; l'écoute fixe l'utilisateur.
        self._next_saccade -= dt
        if self._next_saccade <= 0.0:
            spread = .18 if state in ("listening", "speaking") else .5
            self._look = [self._rng.uniform(-spread, spread),
                          self._rng.uniform(-spread*.6, spread*.6)]
            self._next_saccade = self._rng.uniform(.6, 2.8)
        for i in (0, 1):
            goal = expr["gaze"][i] + self._look[i] * (1.0 - abs(expr["gaze"][i]))
            self._gaze[i] = ease(self._gaze[i], goal, dt, 18.0)

        # Clignements (parfois doublés), plus fréquents en réflexion :
        # fermeture rapide, réouverture un peu plus lente.
        self._next_blink -= dt
        if self._blink_t < 0.0 and self._next_blink <= 0.0:
            self._blink_t = 0.0
            lo, hi = (1.5, 3.5) if state == "thinking" else (2.5, 6.0)
            self._next_blink = .32 if self._rng.random() < .15 else self._rng.uniform(lo, hi)
        if self._blink_t >= 0.0:
            self._blink_t += dt
            k = self._blink_t / .17
            self._blink = min(1.0, k / .4) if k < .4 else max(0.0, 1.0 - (k - .4) / .6)
            if k >= 1.0:
                self._blink_t = -1.0
                self._blink = 0.0
        self._t = t

    # ── Passe 1 : traits, dans l'espace de la photo ─────────────────────────
    def _render_eyes(self) -> None:
        p = self._p
        raise_px = (p["brow"] + .5 * self._brow_kick) * 9.0
        close = max(0.0, min(1.0, max(self._blink, p["squint"])))
        gx, gy = self._gaze[0] * GAZE_PX[0], self._gaze[1] * GAZE_PX[1]
        for z in self._eyes:
            eye = z["eye"]
            qx = z["qx"] + z["sad_x"] * min(0.0, p["sad"])
            qy = z["qy"] + z["brow"] * raise_px + z["sad_y"] * p["sad"]
            # Paupière : la peau au-dessus des cils s'étire vers le bas et le
            # liseré de cils (LASH_PX) glisse sans se déformer. Plus elle ferme,
            # plus la peau vient d'au-dessus des pointes de cils (jamais
            # étirées) et d'une version adoucie de la photo (pas de traînées).
            lid = None
            if close > .01:
                band = float(eye["band"])
                top = z["y_up"] - band
                cover = close * z["cover"]
                k = _smoothstep(0.0, 14.0, cover)
                length = band + cover
                rel = qy - top
                inside = (rel > 0.0) & (rel < length)
                skin = band - LASH_PX - (LASH_TIPS - LASH_PX) * k
                edge = length - LASH_PX
                on_skin = rel < edge
                stretched = np.where(on_skin, top + rel * skin / np.maximum(edge, 1e-3),
                                     z["y_up"] - (length - rel))
                np.copyto(qy, stretched.astype(np.float32), where=inside)
                lid = (inside & on_skin) * k * .8 * _smoothstep(0.0, 7.0, rel)
                depth = np.clip(rel / np.maximum(edge, 1e-3), 0.0, 1.0)
                lid_shade = 1.0 - .14 * depth * depth
            # Iris : il glisse dans l'ouverture, les paupières restent en place.
            if abs(gx) + abs(gy) > .05:
                cx, cy = eye["cx"], eye["cy"]
                dx, dy = qx - (cx + gx), qy - (cy + gy)
                ri = float(eye["iris"])
                w = np.clip(1.0 - (np.sqrt(dx*dx + dy*dy) - ri) / (ri * .8), 0.0, 1.0)
                vy = (qy - cy) / np.where(qy < cy, eye["up"], eye["down"])
                w *= np.clip((1.0 - (z["u"]**2 + vy*vy)) / .35, 0.0, 1.0)
                qx = qx - gx * w
                qy = qy - gy * w
            qx = qx.astype(np.float32, copy=False)
            qy = qy.astype(np.float32, copy=False)
            cv2.remap(self._src, qx, qy, cv2.INTER_LINEAR, dst=z["buf"])
            if lid is not None and lid.any():
                soft = cv2.remap(self._lid_src, qx, qy, cv2.INTER_LINEAR)
                m = lid[..., None]
                shade = np.ones_like(soft, dtype=np.float32)
                shade[..., :3] = lid_shade[..., None]
                z["buf"][...] = (z["buf"] * (1.0 - m) + soft * shade * m).astype(np.uint8)
            self._face[z["rows"], z["cols"]] = z["buf"]

    def _render_mouth(self) -> None:
        z = self._mouth
        p = self._p
        smile = p["smile"]
        wide = self._wide - .15 * min(0.0, smile)
        j = self._jaw * JAW_MAX
        qx = z["qx"] + z["wide_x"] * wide + z["smile_x"] * max(0.0, smile)
        qs = z["qy"] + z["smile_y"] * smile
        qy = qs + z["jaw_y"] * j if j > .4 else qs
        cv2.remap(self._src, qx.astype(np.float32, copy=False),
                  qy.astype(np.float32, copy=False), cv2.INTER_LINEAR, dst=z["buf"])
        if j > .4:
            self._paint_cavity(qs[z["c_rows"], z["c_cols"]], j)
        self._face[z["rows"], z["cols"]] = z["buf"]

    def _paint_cavity(self, qy: np.ndarray, j: float) -> None:
        z = self._mouth
        y0 = self._spec["mouth"]["y"]
        lens = z["c_lens"]
        top = y0 - .25 * j * lens
        bottom = y0 + j * lens
        depth = np.minimum(qy - top, bottom - qy)
        aa = np.clip(depth / 1.3 + .5, 0.0, 1.0) * (lens > .02)
        if not aa.any():
            return
        rel = qy - top
        frac = rel / np.maximum(bottom - top, 1.0)
        # Fond : noir sous les dents, rougeâtre vers la langue ; ombre aux bords.
        dark = .55 + .45 * np.clip(depth / 5.0, 0.0, 1.0)
        tongue = _smoothstep(.55, 1.0, frac) * min(1.0, j / 20.0)
        r = (26.0 + 80.0 * tongue) * dark
        g = (9.0 + 26.0 * tongue) * dark
        b = (11.0 + 30.0 * tongue) * dark
        # Dents du haut : bande ivoire accrochée à la lèvre, ombrée sous la lèvre.
        teeth_h = min(10.0, .5 * j)
        tooth = np.clip((teeth_h - rel) / 1.2, 0.0, 1.0) * z["c_teeth_x"]
        shade = z["c_teeth_shade"] * (.62 + .38 * np.clip(rel / 4.0, 0.0, 1.0))
        r += tooth * (208.0 * shade - r)
        g += tooth * (198.0 * shade - g)
        b += tooth * (184.0 * shade - b)
        out = z["buf"][z["c_rows"], z["c_cols"]]
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
        roll = math.radians(prm["roll"] + .9 * math.sin(tt * .21) + .5 * math.sin(tt * .53 + 1.3))
        breath = 1.0 + .006 * math.sin(tt * 1.25)
        yaw = prm["yaw"] + .22 * math.sin(tt * .17 + .4) + .10 * math.sin(tt * .47)
        pitch = prm["pitch"] + .12 * math.sin(tt * .29 + 2.0) + self._nod * .35
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
