from __future__ import annotations

import math
import random
import sys
import time
from datetime import datetime

import psutil
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QSizePolicy

from ui.core.qtflags import _GL_BASE
from ui.orb.arc_paint import _HudPaintMixin
from ui.orb.arc_sprites import _HudSpritesMixin
from ui.styles.theme import C

# ── HUD Orb natif QPainter — « ARC CORE » (moteur 3D temps réel) ─────────────

class HudCanvas(_HudPaintMixin, _HudSpritesMixin, _GL_BASE):
    """Orb JARVIS 3D réactif et organique, dessiné avec QPainter.

    Rendu : volume de particules en projection perspective. Les photons font
    des allers-retours et ne créent que des filaments temporaires lorsqu'ils se
    rapprochent ; l'heure peut être formée par ce même nuage.
    États : idle | listening | speaking | thinking | acting | error
    """

    # ─── Palettes par état ────────────────────────────────────────────────────
    # core : émission du noyau — halo : aura — wire : filaire — hot : incandescence
    _PALETTES = {
        "idle": {
            "core": QColor(76, 168, 232), "halo": QColor(76, 168, 232),
            "wire": QColor(76, 168, 232), "hot": QColor(184, 238, 255),
            "pulse_speed": 0.85, "spin": 1.0,
        },
        "listening": {
            "core": QColor(100, 255, 195), "halo": QColor(0, 195, 135),
            "wire": QColor(0, 245, 180), "hot": QColor(232, 255, 246),
            "pulse_speed": 1.9,  "spin": 1.7,
        },
        "speaking": {
            "core": QColor(90, 184, 240), "halo": QColor(90, 184, 240),
            "wire": QColor(90, 184, 240), "hot": QColor(184, 238, 255),
            "pulse_speed": 3.4,  "spin": 2.4,
        },
        "thinking": {
            "core": QColor(185, 115, 255), "halo": QColor(118, 65, 230),
            "wire": QColor(195, 125, 255), "hot": QColor(246, 235, 255),
            "pulse_speed": 2.1,  "spin": 3.1,
        },
        "acting": {
            "core": QColor(0, 245, 255),   "halo": QColor(0, 180, 255),
            "wire": QColor(0, 255, 220),   "hot": QColor(240, 255, 255),
            "pulse_speed": 4.0,  "spin": 3.8,
        },
        "error": {
            # Une erreur reste lisible comme JARVIS : le cyan ne disparaît pas.
            # Les rares alertes ambre/rouge sont dessinées comme impulsions par
            # le peintre, plutôt que de transformer tout le coeur en voyant.
            "core": QColor(255, 105, 125),  "halo": QColor(210, 35, 65),
            "wire": QColor(255, 100, 120),  "hot": QColor(255, 230, 235),
            "pulse_speed": 4.5,  "spin": 1.2,
        },
    }

    _LATS = 5        # parallèles de la sphère filaire
    _LONS = 12       # méridiens
    _SEG  = 32       # segments par courbe
    _ZBUCKETS = 4    # niveaux de profondeur (1 tracé groupé par niveau)
    _CAM  = 3.1      # distance caméra (perspective)
    _SPEC_N = 48     # barres du spectre radial
    _MOTES  = 52     # poussières libres : proches, lointaines et orbitales
    # Un canevas Qt/Python ne bénéficie pas du parallélisme du WebGL : limiter
    # aussi le *calcul* (pas seulement le dessin) est indispensable au micro.
    _PARTICLE_N = 240
    # L'horloge concentre les photons dans une zone réduite. Ce budget supérieur
    # rend chaque chiffre immédiatement lisible, même à travers la lueur du HUD.
    _CLOCK_PARTICLE_BUDGET = 240
    # Les figures concentrent les photons dans moins de pixels ; ce plafond
    # évite un blend additif coûteux pendant l'écoute et la réponse, là où la
    # priorité absolue reste la voix.
    # Le HUD Qt et l'audio Python partagent le GIL. Deux mille particules à
    # 50 Hz font monopoliser un coeur entier, même quand l'assistant ne parle
    # pas. Ce budget garde la forme organique tout en laissant la priorité à
    # la capture et à la voix sur une machine deux coeurs.
    _IDLE_PARTICLE_BUDGET = 160
    _ACTIVE_PARTICLE_BUDGET = 240
    # +45 % : augmentation volontairement visible, demandée pour donner au
    # nuage une présence forte même derrière les panneaux de l'interface.
    # écran haute définition sans augmenter le coût CPU du nombre de points.
    # Photons volontairement grands et très lisibles, y compris derrière les
    # filaments : ce réglage n'ajoute aucune boucle ni coût CPU.
    _PARTICLE_POINT_SCALE = 2.20
    _FORMATION_SECONDS = 7.0
    _FORMATION_NAMES = ("sphere",)
    _STATE_FORMATION = {
        "idle": "sphere", "listening": "sphere", "thinking": "sphere",
        "speaking": "sphere", "acting": "sphere", "error": "sphere",
    }
    _DIGIT_SEGMENTS = {
        "0": (0, 1, 2, 3, 4, 5), "1": (1, 2), "2": (0, 1, 6, 4, 3),
        "3": (0, 1, 6, 2, 3), "4": (5, 6, 1, 2), "5": (0, 5, 6, 2, 3),
        "6": (0, 5, 6, 4, 2, 3), "7": (0, 1, 2), "8": (0, 1, 2, 3, 4, 5, 6),
        "9": (0, 1, 2, 3, 5, 6),
    }
    # Les filaments ne sont pas une grille permanente : ce sont de rares
    # rapprochements entre photons. Le plafond les garde précieux et, surtout,
    # évite de prendre du temps au moteur audio sur les petites machines.
    _LINK_CELL = 0.34
    _MAX_LINKS_PER_PARTICLE = 1
    _FILAMENT_BUDGET = 360
    # orb.ts garde ses segments côté GPU. Ici, on garde la géométrie cinq
    # images : le rendu reste identique à l'œil sans refaire la grille Python
    # à chaque frame (critique sur deux cœurs).
    _FILAMENT_REFRESH_FRAMES = 8
    _CLOUD_PROFILES = {
        # rayon, agitation, taille, lumière, densité des liens, électrons, vortex
        # Rayon constant : chaque état se distingue par le mouvement, la
        # couleur et les échanges, jamais par une sphère qui se contracte.
        "idle":      (1.00, 0.12, 0.35, 0.42, 0.12, 0.00, 0.00),
        "listening": (1.00, 0.22, 0.42, 0.72, 0.38, 0.00, 0.00),
        "thinking":  (1.00, 0.28, 0.40, 0.74, 1.00, 0.015, 0.00),
        "speaking": (1.00, 0.32, 0.50, 0.86, 0.90, 0.010, 1.40),
        "acting":   (1.00, 0.38, 0.52, 0.92, 0.76, 0.010, 1.65),
        "error":    (1.00, 0.30, 0.48, 0.82, 0.62, 0.00, 0.55),
    }

    def __init__(self, face_path: str, assistant_name: str = "ANO-GPT", parent=None):
        super().__init__(parent)
        self.setMinimumSize(300, 300)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self._background_photo_active = False
        self._assistant_name = assistant_name
        self._muted    = False
        self._speaking = False
        self._state    = "idle"     # JARVIS state string
        self._ws       = "idle"     # mapped web-state

        # ── Horloge / dynamique ─────────────────────────────────────────────
        self._t0     = time.monotonic()
        self._volume = 0.0
        self._target_vol = 0.0
        self._last_ext_vol_t = 0.0
        self._energy = 0.0

        # ── Bandes d'analyse audio 3-bandes (Bass / Mid / Treble) ───────────
        self._bass = 0.0
        self._mid = 0.0
        self._treble = 0.0
        self._target_bass = 0.0
        self._target_mid = 0.0
        self._target_treble = 0.0
        self._audio_bands = [0.0] * 8
        self._audio_bands_live = False

        self._yaw, self._pitch, self._roll = 0.0, -0.35, 0.0
        self._sweep  = 0.0

        # ── Retour visuel gestuel holographique ─────────────────────────────
        self._gesture_icon: str | None = None
        self._gesture_label: str = ""
        self._gesture_val: float = 0.0
        self._gesture_expires: float = 0.0

        # ── Indicateur visuel vision continue (icône œil néon) ─────────────
        self._continuous_vision_active: bool = False

        # ── Nuage de particules volumétrique 3D ─────────────────────────────
        # Un pool fixe : aucune allocation de particules dans la boucle Qt.
        self._particles = self._init_particles()
        # Un filament n'existe que le temps d'un rapprochement : aucune cage,
        # aucun maillage fixe autour du volume.
        self._particle_links: list[tuple[int, int, float, float]] = []
        self._particle_screen: list[tuple[float, float, float]] = []
        self._cloud_frame = 0
        self._filament_frame = 0
        self._cloud_live = list(self._CLOUD_PROFILES["idle"])
        self._cloud_pulse = 0.0
        self._last_bass = 0.0
        self._shockwave = 0.0  # choc amorti, inspiré de la physique orb.ts
        self._last_motion_drive = 0.0
        self._motion_impulse = 0.0
        self._next_speaking_surge = 0.0
        self._clock_particles_until = 0.0
        self._clock_particles_started_at = 0.0
        self._clock_display_active = False
        self._clock_digits = ""
        self._clock_segments: list[tuple[int, int]] = []

        # ── Systèmes dynamiques ─────────────────────────────────────────────
        # harmoniques du contour du noyau : (ordre, amplitude, phase, vitesse)
        self._core_harm = [
            (k, a, random.uniform(0, 6.28318), sp)
            for k, a, sp in ((2, 0.055, 1.3), (3, 0.038, -1.9),
                             (5, 0.020, 2.4), (7, 0.011, -3.1))
        ]
        self._spec       = [0.0] * self._SPEC_N
        self._spec_seed  = [random.uniform(0, 6.28318) for _ in range(self._SPEC_N)]
        self._spec_dir   = [(math.cos(2 * math.pi * i / self._SPEC_N - math.pi / 2),
                             math.sin(2 * math.pi * i / self._SPEC_N - math.pi / 2))
                            for i in range(self._SPEC_N)]
        self._motes      = self._init_motes()
        self._arcs: list[dict] = []
        self._sparks: list[dict] = []

        # ── Palette vivante : interpole en douceur vers l'état cible au lieu
        # de basculer d'un coup — évite le « saut » de couleur qui casse
        # l'illusion de vie quand l'état change (idle → listening, etc.)
        self._PALETTES = {
            k: {prop: QColor(val) if isinstance(val, QColor) else val for prop, val in pal.items()}
            for k, pal in HudCanvas._PALETTES.items()
        }
        _p0 = self._PALETTES["idle"]
        self._pal_live = {
            "core": QColor(_p0["core"]), "halo": QColor(_p0["halo"]),
            "wire": QColor(_p0["wire"]), "hot": QColor(_p0["hot"]),
            "pulse_speed": _p0["pulse_speed"], "spin": _p0["spin"],
        }

        # ── Cache de sprites ────────────────────────────────────────────────
        self._cache_key = None
        self._pm: dict[str, QPixmap] = {}

        # Deux images/s au repos, 15 FPS pendant la voix : l'orbe reste
        # présent sans déclencher un repaint XWayland permanent.
        self._frame_ms = 500.0
        self._interval = 500
        self._anim_tmr = QTimer(self)
        self._anim_tmr.timeout.connect(self._tick)
        self._anim_tmr.start(self._interval)

        # Sur batterie, on plafonne la cadence même si le rendu suit très
        # bien : pas la peine de dessiner l'orbe à 50 fps sur secteur externe
        # débranché (le profil énergie de la machine ne doit pas être ruiné).
        self._on_battery = False
        self._batt_tmr = QTimer(self)
        self._batt_tmr.timeout.connect(self._check_battery)
        self._batt_tmr.start(15000)
        self._check_battery()

    def _check_battery(self) -> None:
        try:
            b = psutil.sensors_battery()
            self._on_battery = bool(b and not b.power_plugged)
        except Exception:
            self._on_battery = False

    # ══ Géométrie ════════════════════════════════════════════════════════════
    def _init_particles(self) -> list[dict]:
        """Points répartis dans un volume, chacun avec mouvement propre."""
        out = []
        for _ in range(self._PARTICLE_N):
            z = random.uniform(-1.0, 1.0)
            a = random.uniform(0.0, math.tau)
            radial = math.sqrt(max(0.0, 1.0 - z * z))
            dx, dy, dz = radial * math.cos(a), z, radial * math.sin(a)
            # Même loi radiale que orb.ts : sqrt(random) donne un volume plus
            # dense vers l'enveloppe, signature visuelle du nuage Three.js.
            home_r = random.random() ** 0.5
            out.append({
                "x": dx * home_r, "y": dy * home_r, "z": dz * home_r,
                "vx": 0.0, "vy": 0.0, "vz": 0.0,
                "dx": dx, "dy": dy, "dz": dz, "home_r": home_r,
                "phase": random.uniform(0.0, math.tau),
                # Valeurs immuables : elles distribuent chaque particule sur
                # les figures sans allocation ni tirage aléatoire par frame.
                "form_phase": random.uniform(0.0, math.tau),
                # Permutation uniforme : les premiers N points visibles
                # représentent déjà toute la figure lorsque le LOD réduit le
                # budget pendant une réponse vocale.
                "form_u": (((_ * 2081) % self._PARTICLE_N) + 0.5) / self._PARTICLE_N,
                "lane": _ % 3,
                "segment_u": random.random(),
                "size": random.uniform(0.65, 1.35),
                # Déplacement alternatif individuel : les photons ne tournent
                # pas simplement en rond, ils font des allers-retours vivants.
                "shuttle": random.uniform(0.028, 0.115),
                "shuttle_speed": random.uniform(0.72, 1.55),
            })
        return out

    def _init_particle_links(self) -> list[tuple[int, int, float]]:
        """Filaments stables entre voisins 3D, calculés une seule fois.

        Une grille locale donne un réseau lisible et irrégulier, contrairement
        à des points voisins dans une permutation qui traverseraient la sphère.
        """
        cell = self._LINK_CELL
        grid: dict[tuple[int, int, int], list[int]] = {}
        limit = self._ACTIVE_PARTICLE_BUDGET
        for i, pt in enumerate(self._particles[:limit]):
            key = (int(math.floor(pt["x"] / cell)), int(math.floor(pt["y"] / cell)),
                   int(math.floor(pt["z"] / cell)))
            grid.setdefault(key, []).append(i)
        links: list[tuple[int, int, float]] = []
        degree = [0] * limit
        for i, pt in enumerate(self._particles[:limit]):
            if degree[i] or len(links) >= 145:
                continue
            gx, gy, gz = (int(math.floor(pt["x"] / cell)), int(math.floor(pt["y"] / cell)),
                          int(math.floor(pt["z"] / cell)))
            candidate = None
            for ox in (-1, 0, 1):
                for oy in (-1, 0, 1):
                    for oz in (-1, 0, 1):
                        for j in grid.get((gx + ox, gy + oy, gz + oz), ()):
                            if j <= i or degree[j]:
                                continue
                            other = self._particles[j]
                            dx, dy, dz = pt["x"] - other["x"], pt["y"] - other["y"], pt["z"] - other["z"]
                            d2 = dx * dx + dy * dy + dz * dz
                            if d2 < 0.19 and (candidate is None or d2 < candidate[0]):
                                candidate = (d2, j)
            if candidate is not None:
                d2, j = candidate
                degree[i] = degree[j] = 1
                links.append((i, j, math.sqrt(d2)))
        return links

    @staticmethod
    def _init_surface_mesh() -> tuple[list[tuple[float, float, float, float]], list[tuple[int, int]]]:
        """Réseau de voisinage réparti sur une vraie sphère, sans pôles lourds."""
        count = 420
        nodes: list[tuple[float, float, float, float]] = []
        golden = math.pi * (3.0 - math.sqrt(5.0))
        for i in range(count):
            y = 1.0 - 2.0 * (i + .5) / count
            radial = math.sqrt(max(0.0, 1.0 - y * y))
            phase = (i * 2.39996323) % math.tau
            angle = i * golden + .035 * math.sin(i * 2.17)
            radius = .94 + .045 * math.sin(i * 1.91)
            nodes.append((math.cos(angle) * radial * radius, y * radius,
                          math.sin(angle) * radial * radius, phase))
        # Trois voisins géométriques : aucun fil traversant arbitrairement le
        # volume, seulement des facettes locales organiques.
        edges: set[tuple[int, int]] = set()
        for i, (x, y, z, _phase) in enumerate(nodes):
            nearest = sorted(
                ((x - ox) ** 2 + (y - oy) ** 2 + (z - oz) ** 2, j)
                for j, (ox, oy, oz, _other_phase) in enumerate(nodes) if j != i
            )[:4]
            for _distance, j in nearest:
                edges.add((i, j) if i < j else (j, i))
        return nodes, sorted(edges)

    def _particle_budget(self) -> int:
        if self._clock_display_active:
            return self._CLOCK_PARTICLE_BUDGET
        if self._ws == "thinking":
            # La réflexion est le seul état sans flux vocal prioritaire : le
            # réseau peut alors gagner en densité et en circulation visible.
            return min(self._ACTIVE_PARTICLE_BUDGET, 2450)
        if self._ws == "acting":
            return min(self._ACTIVE_PARTICLE_BUDGET, 2250)
        if self._ws in {"listening", "speaking"}:
            # Le pool garde sa densité maximale, mais ce LOD borne le travail
            # Python pendant micro/TTS sur la machine deux coeurs.
            return min(self._ACTIVE_PARTICLE_BUDGET, 2100)
        return min(self._IDLE_PARTICLE_BUDGET, 2050)

    def show_clock_particles(self, duration: float = 8.5) -> None:
        """Fait former HH:MM au nuage quand l'assistant donne l'heure."""
        now = datetime.now()
        digits = f"{now.hour:02d}{now.minute:02d}"
        clock_is_already_visible = (
            bool(self._clock_segments)
            and time.monotonic() < self._clock_particles_until
        )
        # Une réponse arrive parfois par fragments. Ne jamais relancer la
        # convergence à chaque fragment : on prolonge l'affichage déjà formé.
        if clock_is_already_visible and digits == self._clock_digits:
            self._clock_particles_until = max(
                self._clock_particles_until,
                time.monotonic() + max(2.0, min(15.0, float(duration))),
            )
            self.update()
            return
        self._clock_digits = digits
        segments: list[tuple[int, int]] = []
        for digit_index, digit in enumerate(self._clock_digits):
            segments.extend((digit_index, segment) for segment in self._DIGIT_SEGMENTS[digit])
        # Les deux points du séparateur sont eux-mêmes dessinés par particules.
        segments.extend(((4, 7), (4, 8)))
        self._clock_segments = segments
        self._clock_particles_started_at = time.monotonic()
        self._clock_particles_until = self._clock_particles_started_at + max(2.0, min(15.0, float(duration)))
        self.update()

    @staticmethod
    def _clock_segment_target(digit_index: int, segment: int, u: float) -> tuple[float, float, float]:
        """Position locale d'un point du cadran à sept segments.

        La caméra de l'orbe regarde l'axe local depuis l'autre côté : l'abscisse
        est donc inversée ici, une seule fois, pour que HH:MM reste lisible de
        gauche à droite à l'écran.
        """
        if digit_index == 4:  # séparateur « : »
            return 0.0, (-0.12 if segment == 7 else 0.12), 0.0
        center_x = (-0.56, -0.19, 0.19, 0.56)[digit_index]
        line = (u - 0.5)
        if segment in (0, 3, 6):
            x = center_x + line * 0.23
            y = (-0.31, 0.31, 0.0)[(0, 3, 6).index(segment)]
        elif segment == 1:
            x, y = center_x + 0.125, -0.155 + line * 0.27
        elif segment == 2:
            x, y = center_x + 0.125, 0.155 + line * 0.27
        elif segment == 4:
            x, y = center_x - 0.125, 0.155 + line * 0.27
        else:
            x, y = center_x - 0.125, -0.155 + line * 0.27
        return -x, y, 0.0

    def _update_particle_links(self, t: float = 0.0) -> None:
        """Crée de brefs filaments seulement entre voisins réellement proches.

        La grille locale évite le coût quadratique. Les liens sont rafraîchis
        toutes les quelques images : ils naissent, changent et meurent avec le
        mouvement des photons, sans jamais devenir une sphère filaire.
        """
        if self._clock_display_active:
            self._particle_links = []
            return
        cell = self._LINK_CELL
        grid: dict[tuple[int, int, int], list[int]] = {}
        limit = self._particle_budget()
        for i, pt in enumerate(self._particles[:limit]):
            key = (int(math.floor(pt["x"] / cell)), int(math.floor(pt["y"] / cell)),
                   int(math.floor(pt["z"] / cell)))
            grid.setdefault(key, []).append(i)
        density = self._cloud_live[4]
        reach = 0.19 + density * 0.12
        reach2 = reach * reach
        degree = [0] * limit
        links: list[tuple[int, int, float, float]] = []
        for i, pt in enumerate(self._particles[:limit]):
            if len(links) >= self._FILAMENT_BUDGET or degree[i] >= self._MAX_LINKS_PER_PARTICLE:
                continue
            gx, gy, gz = (int(math.floor(pt["x"] / cell)), int(math.floor(pt["y"] / cell)),
                          int(math.floor(pt["z"] / cell)))
            nearest: tuple[float, int] | None = None
            for ox in (-1, 0, 1):
                for oy in (-1, 0, 1):
                    for oz in (-1, 0, 1):
                        for j in grid.get((gx + ox, gy + oy, gz + oz), ()):
                            if j <= i or degree[i] >= self._MAX_LINKS_PER_PARTICLE:
                                continue
                            if degree[j] >= self._MAX_LINKS_PER_PARTICLE:
                                continue
                            other = self._particles[j]
                            dx, dy, dz = pt["x"] - other["x"], pt["y"] - other["y"], pt["z"] - other["z"]
                            d2 = dx * dx + dy * dy + dz * dz
                            if d2 < reach2 and (nearest is None or d2 < nearest[0]):
                                nearest = (d2, j)
            if nearest is not None:
                d2, j = nearest
                degree[i] += 1
                degree[j] += 1
                # proximité, puis phase : le filament s'allume et s'éteint
                # organiquement au lieu d'apparaître comme une ligne statique.
                closeness = 1.0 - math.sqrt(d2) / reach
                links.append((i, j, max(0.08, closeness), t + pt["phase"]))
        self._particle_links = links

    def _update_particles(self, dt: float, t: float) -> None:
        """Physique souple et chorégraphies de figures sans coût GPU/Qt élevé."""
        target = self._CLOUD_PROFILES.get(self._ws, self._CLOUD_PROFILES["idle"])
        for i, value in enumerate(target):
            self._cloud_live[i] += (value - self._cloud_live[i]) * 0.075
        radius, noise, _size, _light, _density, _electrons, vortex = self._cloud_live
        # Pic réel + filet de sécurité périodique : une voix plate garde vie.
        bass_delta = self._bass - self._last_bass
        self._last_bass = self._bass
        bass_shock = max(0.0, bass_delta - 0.04) * 5.0
        self._shockwave = max(self._shockwave * 0.82, bass_shock)
        if self._ws == "speaking":
            if self._next_speaking_surge <= 0.0:
                self._next_speaking_surge = t + random.uniform(1.3, 1.8)
            if bass_delta > 0.085 or t >= self._next_speaking_surge:
                self._cloud_pulse = 1.0
                self._shockwave = max(self._shockwave, 0.28)
                self._next_speaking_surge = t + random.uniform(1.3, 1.8)
        else:
            self._next_speaking_surge = 0.0
        self._cloud_pulse *= 0.86
        pulse = self._cloud_pulse * (0.055 + self._bass * 0.050)
        shockwave = max(self._shockwave, pulse * 0.55)
        # Taille globale stable : les impulsions déplacent les photons, elles
        # ne compressent jamais l'orbe sur lui-même.
        target_radius = radius
        vortex_strength = vortex * (0.05 + 0.26 * self._volume)
        # Une figure par état : l'utilisateur lit immédiatement l'activité de
        # l'assistant. Au repos, les quatre figures se relaient lentement.
        if self._muted:
            formation = 0
        elif self._ws == "idle":
            formation = int(t / self._FORMATION_SECONDS) % len(self._FORMATION_NAMES)
        else:
            formation = self._FORMATION_NAMES.index(self._STATE_FORMATION.get(self._ws, "sphere"))
        clock_active = bool(self._clock_segments and t + self._t0 < self._clock_particles_until)
        self._clock_display_active = clock_active
        simulation_budget = self._particle_budget()
        # Énergie sonore unifiée. C'est ce signal qui différencie vraiment une
        # écoute silencieuse d'une personne qui parle, et une réponse calme
        # d'une réponse énergique.
        motion_drive = min(1.0, max(self._volume, self._bass, self._mid, self._treble))
        onset = max(0.0, motion_drive - self._last_motion_drive)
        self._last_motion_drive = motion_drive
        self._motion_impulse = max(self._motion_impulse * 0.78, min(1.0, onset * 4.5))
        if self._ws == "listening":
            formation_speed = 0.75 + motion_drive * 5.2
        elif self._ws == "speaking":
            formation_speed = 1.45 + motion_drive * 7.0
        elif self._ws == "thinking":
            formation_speed = 0.55 + self._mid * 3.4
        elif self._ws == "acting":
            formation_speed = 1.4 + motion_drive * 5.8
        else:
            formation_speed = 0.38 + motion_drive * 1.2
        for particle_index, pt in enumerate(self._particles):
            # Les photons non visibles pendant une figure dense restent en
            # réserve. Ne pas les simuler économise directement le GIL pour la
            # voix ; ils reprennent leur place, via ressort, au retour idle.
            if particle_index >= simulation_budget:
                continue
            ph = pt["phase"]
            # 0 sphère, 1 tore, 2 triple hélice, 3 boucle/infinity. Les
            # amplitudes restent sous le rayon du nuage, donc aucune figure ne
            # sort du HUD pendant une voix énergique.
            if clock_active:
                digit_index, segment = self._clock_segments[particle_index % len(self._clock_segments)]
                tx, ty, tz = self._clock_segment_target(digit_index, segment, pt["segment_u"])
                tx, ty, tz = tx * target_radius * 1.12, ty * target_radius * 1.12, tz
            elif formation == 0:
                home = pt["home_r"] * target_radius
                tx, ty, tz = pt["dx"] * home, pt["dy"] * home, pt["dz"] * home
            elif formation == 1:
                a = math.tau * pt["form_u"] * 9.0 + t * 0.34 * formation_speed
                b = pt["form_phase"] + t * 0.56 * formation_speed
                ring = 0.66 + 0.24 * math.cos(b)
                tx = math.cos(a) * ring * target_radius
                ty = math.sin(b) * 0.28 * target_radius
                tz = math.sin(a) * ring * target_radius
            elif formation == 2:
                a = math.tau * pt["form_u"] * 5.0 + t * 0.62 * formation_speed + pt["lane"] * (math.tau / 3.0)
                tx = math.cos(a) * 0.52 * target_radius
                ty = ((pt["form_u"] - 0.5) * 1.62 + 0.08 * math.sin(a * 3.0)) * target_radius
                tz = math.sin(a) * 0.52 * target_radius
            elif formation == 3:
                a = math.tau * pt["form_u"] + t * 0.46 * formation_speed
                tx = math.sin(a) * 0.78 * target_radius
                ty = math.sin(a * 2.0) * 0.42 * target_radius
                tz = (math.cos(a) * 0.48 + math.sin(pt["form_phase"]) * 0.045) * target_radius
            else:
                # Trois bras spirales : la figure d'action / exécution.
                arm = pt["lane"] * (math.tau / 3.0)
                a = math.tau * pt["form_u"] * 2.2 + arm + t * 0.72 * formation_speed
                spiral_r = 0.16 + 0.72 * pt["form_u"]
                tx = math.cos(a) * spiral_r * target_radius
                ty = math.sin(pt["form_phase"] + t * 0.9) * 0.20 * target_radius
                tz = math.sin(a) * spiral_r * target_radius
            # Les états modulent l'organisation du réseau, pas la silhouette
            # fondamentale : JARVIS reste une sphère holographique dans tous
            # les cas, même lorsqu'il réfléchit ou exécute une action.
            if not clock_active and formation:
                spherical = (pt["dx"] * pt["home_r"] * target_radius,
                             pt["dy"] * pt["home_r"] * target_radius,
                             pt["dz"] * pt["home_r"] * target_radius)
                form_weight = (0.48, 0.34, 0.46, 0.52)[formation - 1]
                tx = spherical[0] * (1.0 - form_weight) + tx * form_weight
                ty = spherical[1] * (1.0 - form_weight) + ty * form_weight
                tz = spherical[2] * (1.0 - form_weight) + tz * form_weight
            # Réactivité propre à chaque état. Ces déformations arrivent
            # avant le ressort, donc elles se propagent en vagues fluides au
            # lieu de faire vibrer chaque point indépendamment.
            if not clock_active and self._ws == "listening":
                ripple = math.sin(t * (4.0 + motion_drive * 12.0) + ph * 3.0)
                # Onde qui traverse le volume, sans réduire son diamètre.
                tx += pt["dz"] * ripple * target_radius * (0.04 + motion_drive * 0.12)
                ty += ripple * target_radius * (0.03 + motion_drive * 0.12)
            elif not clock_active and self._ws == "speaking":
                twist = t * (0.7 + motion_drive * 3.8) + ph * 0.20
                ct, st = math.cos(twist), math.sin(twist)
                tx, tz = (tx * ct - tz * st), (tx * st + tz * ct)
                burst = 1.0 + self._motion_impulse * 0.12 + motion_drive * 0.06
                tx, ty, tz = tx * burst, ty * burst, tz * burst
                # Les gestes signature d'orb.ts pendant une réponse vocale.
                if shockwave > .005:
                    tx += pt["dx"] * shockwave * .18
                    ty += pt["dy"] * shockwave * .09
                    tz += pt["dz"] * shockwave * .18
                breath = math.sin(t * 7.5 + ph * .4) * .018
                tx += pt["dx"] * breath
                ty += pt["dy"] * breath
                tz += pt["dz"] * breath
                if self._treble > .08:
                    flutter = math.sin(t * 17.0 + ph * 5.3) * self._treble * .018
                    tx += flutter
                    ty += flutter * .5
                    tz += flutter
            elif not clock_active and self._ws == "thinking":
                breathe_think = math.sin(t * (1.3 + self._mid * 3.0) + ph * 1.5)
                ty += breathe_think * target_radius * (0.04 + self._mid * 0.15)
            elif not clock_active and self._ws == "acting":
                sweep = math.sin(t * (3.0 + motion_drive * 5.0) + ph)
                tx += sweep * target_radius * (0.04 + motion_drive * 0.13)
            wobx = math.sin(t * 1.11 + ph) * noise * 0.050
            woby = math.sin(t * 0.83 + ph * 1.71) * noise * 0.050
            wobz = math.sin(t * 1.37 + ph * 0.63) * noise * 0.050
            # Forces multi-fréquences d'orb.ts, ajoutées autour d'un volume
            # constant : agitation organique, poussée basse et pulsation médium.
            ts_speed = {
                "idle": .20, "listening": .30, "thinking": .50,
                "speaking": .45, "acting": .55, "error": .35,
            }.get(self._ws, .20)
            drift = 0.018 * ts_speed
            tx += (math.sin(t * .05 + ph) + math.sin(t * .02 + ph * 2.1 + pt["y"] * .1) * .8) * drift
            ty += (math.cos(t * .06 + ph * 1.3) + math.cos(t * .025 + ph * 1.7 + pt["z"] * .1) * .8) * drift
            tz += (math.sin(t * .055 + ph * .7) + math.sin(t * .022 + ph * .9 + pt["x"] * .1) * .8) * drift
            if self._bass > .05:
                bass_push = self._bass * (.050 if self._ws in {"speaking", "acting"} else .032)
                tx += pt["dx"] * bass_push
                ty += pt["dy"] * bass_push
                tz += pt["dz"] * bass_push
            if self._mid > .10:
                mid_pulse = math.sin(t * 8.0 + ph) * self._mid * (.034 if self._ws in {"speaking", "acting"} else .018)
                tx += pt["dx"] * mid_pulse
                ty += pt["dy"] * mid_pulse
                tz += pt["dz"] * mid_pulse
            # Voyage sur son axe personnel, puis retour : c'est le geste de
            # respiration qui fait se rencontrer les photons et naître les fils.
            shuttle_boost = {
                "idle": 1.35, "listening": 2.35, "thinking": 3.10,
                "speaking": 4.20, "acting": 4.85, "error": 3.65,
            }.get(self._ws, 1.35)
            shuttle = math.sin(t * pt["shuttle_speed"] * shuttle_boost + ph) * pt["shuttle"] * shuttle_boost
            tx += pt["dx"] * shuttle
            ty += pt["dy"] * shuttle
            tz += pt["dz"] * shuttle
            # Vortex autour de Y seulement pendant la parole / action.
            vx_force = -pt["z"] * vortex_strength
            vz_force = pt["x"] * vortex_strength
            pt["vx"] = (pt["vx"] + ((tx + wobx - pt["x"]) * 2.6 + vx_force) * dt) * 0.91
            pt["vy"] = (pt["vy"] + ((ty + woby - pt["y"]) * 2.6) * dt) * 0.91
            pt["vz"] = (pt["vz"] + ((tz + wobz - pt["z"]) * 2.6 + vz_force) * dt) * 0.91
            pt["x"] += pt["vx"] * dt * 50.0
            pt["y"] += pt["vy"] * dt * 50.0
            pt["z"] += pt["vz"] * dt * 50.0
        self._filament_frame = (self._filament_frame + 1) % self._FILAMENT_REFRESH_FRAMES
        if self._filament_frame == 0:
            self._update_particle_links(t)

    @staticmethod
    def _build_circle(n: int) -> list[tuple[float, float, float]]:
        return [(math.cos(2 * math.pi * k / n), 0.0, math.sin(2 * math.pi * k / n))
                for k in range(n + 1)]

    def _init_motes(self) -> list[dict]:
        """Poussières lumineuses sur des orbites 3D quelconques."""
        out = []
        for _ in range(self._MOTES):
            tx, ty = random.uniform(-1.4, 1.4), random.uniform(-1.4, 1.4)
            out.append({
                "r": random.uniform(1.05, 2.05), "ang": random.uniform(0, 6.28318),
                "ctx": math.cos(tx), "stx": math.sin(tx),
                "cty": math.cos(ty), "sty": math.sin(ty),
                "spd": random.uniform(0.25, 1.25) * (1 if random.random() > 0.4 else -1),
                "size": random.uniform(1.2, 3.0), "ph": random.uniform(0, 6.28318),
            })
        return out

    @staticmethod
    def _matrix(yaw: float, pitch: float, roll: float):
        cy, sy = math.cos(yaw), math.sin(yaw)
        cp, sp = math.cos(pitch), math.sin(pitch)
        cr, sr = math.cos(roll), math.sin(roll)
        return (
            (cy * cr + sy * sp * sr, -cy * sr + sy * sp * cr, sy * cp),
            (cp * sr,                 cp * cr,               -sp),
            (-sy * cr + cy * sp * sr, sy * sr + cy * sp * cr, cy * cp),
        )

    def _project(self, pts, m, cx: float, cy: float, R: float, scale: float = 1.0):
        """Projette des points locaux → [(x_écran, y_écran, z)] en perspective."""
        (a0, a1, a2), (b0, b1, b2), (c0, c1, c2) = m
        cam = self._CAM
        out = []
        for x, y, z in pts:
            x *= scale; y *= scale; z *= scale
            rz = c0 * x + c1 * y + c2 * z
            k = (cam / (cam - rz)) * R
            out.append((cx + (a0 * x + a1 * y + a2 * z) * k,
                        cy + (b0 * x + b1 * y + b2 * z) * k, rz))
        return out


    # ══ Couleur vivante ══════════════════════════════════════════════════════
    @staticmethod
    def _lerp_color(a: QColor, b: QColor, f: float) -> QColor:
        return QColor(
            int(a.red()   + (b.red()   - a.red())   * f),
            int(a.green() + (b.green() - a.green()) * f),
            int(a.blue()  + (b.blue()  - a.blue())  * f),
        )

    # ══ Tick d'animation ═════════════════════════════════════════════════════
    def set_low_power(self, low: bool) -> None:
        """Met le grand orbe en sommeil de calcul quand il n'est plus visible.

        Le `_tick` complet — sphère, anneaux, étincelles, arcs — tournait à
        50 Hz même fenêtre réduite, pour un rendu que personne ne regardait.
        Deux cœurs partagés avec la voix ne peuvent pas se le permettre pendant
        que la bulle compagnon anime, elle, ce qui est réellement à l'écran.
        Seuls l'énergie et le volume continuent d'évoluer : ce sont les deux
        valeurs que la bulle lit.
        """
        low = bool(low)
        if low == getattr(self, "_low_power", False):
            return
        self._low_power = low
        self._anim_tmr.setInterval(500 if low else self._interval)

    def _tick(self):
        """Frontière de sûreté du slot Qt.

        PyQt 6.11 termine volontairement le processus lorsqu'une exception
        Python s'échappe d'un slot de QTimer. Une image d'animation défectueuse
        ne doit jamais pouvoir arrêter toute la session vocale : on désactive
        donc uniquement l'animation concernée et on conserve l'interface.
        """
        try:
            # Le rendu Qt et le flux audio se partagent le GIL. La parole
            # obtient au maximum 15 FPS; le visuel cède le processeur entre
            # chaque image.
            voice_active = self._ws in {"listening", "speaking"}
            desired_interval = 67 if voice_active else self._interval
            if not getattr(self, "_low_power", False) and self._anim_tmr.interval() != desired_interval:
                self._anim_tmr.setInterval(desired_interval)
            self._tick_frame()
        except Exception as exc:
            self._anim_tmr.stop()
            print(
                f"[HUD] Animation suspendue après une erreur récupérable : "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            self.update()

    def _tick_frame(self):
        dt = 0.020
        pal = self._PALETTES.get(self._ws, self._PALETTES["idle"])

        if getattr(self, "_low_power", False):
            # Le strict nécessaire pour que la bulle compagnon reste vivante.
            self._energy += ((0.15 if self._ws == "idle" else 1.0) - self._energy) * 0.15
            if (time.monotonic() - self._last_ext_vol_t) >= 0.3:
                self._target_vol = max(0.0, self._target_vol - 0.12)
            self._volume += (self._target_vol - self._volume) * 0.5
            return

        # Fondu doux vers la palette cible (≈1.2 s) : le noyau change d'humeur
        # comme il respire, jamais par un « clic » de couleur.
        lf = 1 - 0.90 ** (dt * 50)
        live = self._pal_live
        for k in ("core", "halo", "wire", "hot"):
            live[k] = self._lerp_color(live[k], pal[k], lf)
        live["pulse_speed"] += (pal["pulse_speed"] - live["pulse_speed"]) * lf
        live["spin"]        += (pal["spin"]        - live["spin"])        * lf
        spin = live["spin"]
        t = time.monotonic() - self._t0

        # Énergie : monte hors veille, redescend en veille
        self._energy += ((0.15 if self._ws == "idle" else 1.0) - self._energy) * 0.06

        # Volume : piloté par le vrai son (micro / flux TTS) quand il arrive
        # (set_volume() appelé il y a moins de 300 ms) — sinon on retombe sur
        # une simulation pour que l'orbe reste vivant même sans flux audio
        # câblé (ancien comportement, gardé en filet de sécurité).
        live_audio = (time.monotonic() - self._last_ext_vol_t) < 0.3
        if not live_audio:
            if self._ws == "speaking":
                self._target_vol = random.uniform(0.30, 1.0)
            elif self._ws == "listening":
                self._target_vol = random.uniform(0.05, 0.24)
            else:
                self._target_vol = max(0.0, self._target_vol - 0.05)
        elif self._ws not in ("speaking", "listening"):
            # Un flux audio traîne encore mais l'état a changé (ex: fin de
            # parole) : on laisse retomber au lieu de rester bloqué en l'air.
            self._target_vol = max(0.0, self._target_vol - 0.08)
        # Attaque rapide (le son monte) / retombée plus douce (le son descend)
        atk = 0.55 if self._target_vol > self._volume else 0.22
        self._volume += (self._target_vol - self._volume) * atk

        # Enveloppes dynamiques par bandes de fréquence (Bass/Mid/Treble)
        self._bass += (self._target_bass - self._bass) * (0.65 if self._target_bass > self._bass else 0.15)
        self._mid += (self._target_mid - self._mid) * (0.55 if self._target_mid > self._mid else 0.18)
        self._treble += (self._target_treble - self._treble) * (0.75 if self._target_treble > self._treble else 0.20)

        # Rotation douce du nuage de particules.
        self._yaw   = (self._yaw + dt * 0.30 * spin) % 6.28318
        self._pitch = -0.34 + 0.16 * math.sin(t * 0.31)
        self._roll  = 0.10 * math.sin(t * 0.23)
        self._sweep = (self._sweep + dt * 55 * spin) % 360

        self._update_particles(dt, t)

        self.update()

    # ══ State API (compatible avec l'ancien HudCanvas) ═══════════════════════
    @property
    def muted(self) -> bool:
        return self._muted

    @muted.setter
    def muted(self, v: bool):
        self._muted = v
        self._update_ws()

    @property
    def speaking(self) -> bool:
        return self._speaking

    @speaking.setter
    def speaking(self, v: bool):
        self._speaking = v
        self._update_ws()

    @property
    def state(self) -> str:
        return self._state

    @state.setter
    def state(self, v: str):
        self._state = v
        self._update_ws()

    def set_volume(self, v: float):
        """Injecte un niveau audio réel (0.0–1.0) et décompose en 3 bandes d'énergie (Bass, Mid, Treble)."""
        v = max(0.0, min(1.0, float(v)))
        self._target_vol = v
        self._last_ext_vol_t = time.monotonic()
        if not self.isVisible():
            self._volume = v

        # 3 bandes d'analyse dynamique
        self._target_bass = v ** 1.3
        self._target_mid = math.sin(v * math.pi * 0.5) * v
        self._target_treble = (v ** 0.7) * random.uniform(0.75, 1.0)

    def set_audio_bands(self, bands) -> None:
        """Injecte les huit bandes FFT réelles calculées par le pont audio."""
        seq = [max(0.0, min(1.0, float(value))) for value in bands]
        seq.extend([0.0] * (8 - len(seq)))
        self._audio_bands = seq[:8]
        self._audio_bands_live = True
        self._target_bass = max(self._audio_bands[:2])
        self._target_mid = max(self._audio_bands[2:5])
        self._target_treble = max(self._audio_bands[5:])
        self._target_vol = max(self._target_vol, sum(self._audio_bands) / 8.0)
        self._last_ext_vol_t = time.monotonic()

    def _update_ws(self):
        if self._muted:
            ws = "idle"
        elif self._speaking:
            ws = "speaking"
        elif self._state == "SPEAKING":
            ws = "speaking"
        elif self._state == "LISTENING":
            ws = "listening"
        elif self._state in ("THINKING", "PROCESSING"):
            ws = "thinking"
        elif self._state in ("ACTING", "EXECUTING", "RUNNING"):
            ws = "acting"
        elif self._state == "ERROR":
            ws = "error"
        else:
            ws = "idle"
        self._ws = ws

    def show_gesture_feedback(
        self, icon: str, label: str = "", value: float = 0.0, duration: float = 1.6
    ) -> None:
        """Affiche une mini-icône holographique du geste reconnu au centre de l'orbe."""
        self._gesture_icon = icon
        self._gesture_label = label
        self._gesture_val = value
        self._gesture_expires = time.monotonic() + max(0.4, duration)
        self.update()

    @property
    def continuous_vision(self) -> bool:
        return getattr(self, "_continuous_vision_active", False)

    @continuous_vision.setter
    def continuous_vision(self, active: bool) -> None:
        self.set_continuous_vision(active)

    def set_continuous_vision(self, active: bool) -> None:
        """Active ou désactive l'icône œil néon de vision continue sur l'orbe."""
        self._continuous_vision_active = bool(active)
        self.update()

    def set_background_image_active(self, active: bool) -> None:
        """Laisse apparaître le calque photo, sans toucher à l'orbe."""
        active = bool(active)
        if self._background_photo_active == active:
            return
        self._background_photo_active = active
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, not active)
        self._cache_key = None
        self.update()

    def set_accent_color(self, accent_hex: str, custom_palette: dict | None = None) -> None:
        """Ajuste dynamiquement les accents de couleur et la palette de l'orbe 3D.
        
        Permet la métamorphose fluide vers les teintes des modes métiers :
        - Cyan (#00d4ff) pour Jarvis
        - Vert (#00ff88) pour Senior DevOps
        - Rouge (#ff3355) pour Cyber Sentinel
        - Violet/Indigo (#8f5cff) pour Zen Focus
        """
        if custom_palette:
            for state_key in ("idle", "listening", "acting", "thinking"):
                if state_key in self._PALETTES:
                    for k in ("core", "halo", "wire", "hot"):
                        if k in custom_palette:
                            val = custom_palette[k]
                            self._PALETTES[state_key][k] = QColor(val) if isinstance(val, str) else val
            if "pulse_speed" in custom_palette:
                self._PALETTES["idle"]["pulse_speed"] = float(custom_palette["pulse_speed"])
            if "spin" in custom_palette:
                self._PALETTES["idle"]["spin"] = float(custom_palette["spin"])
        elif accent_hex:
            base = QColor(accent_hex)
            if base.isValid():
                r, g, b = base.red(), base.green(), base.blue()
                for state_key in ("idle", "listening", "acting", "thinking"):
                    if state_key in self._PALETTES:
                        self._PALETTES[state_key]["wire"] = QColor(r, g, b)
                        self._PALETTES[state_key]["halo"] = QColor(max(0, r - 35), max(0, g - 35), max(0, b - 35))
                        self._PALETTES[state_key]["core"] = QColor(min(255, r + 50), min(255, g + 50), min(255, b + 50))
                        self._PALETTES[state_key]["hot"] = QColor(min(255, r + 90), min(255, g + 90), min(255, b + 90))
        self._cache_key = None
        self.update()
