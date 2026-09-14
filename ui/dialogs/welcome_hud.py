"""Écran de bienvenue J.A.R.V.I.S. — CINEMATIC EDITION.

Philosophie : Chaque pixel a un but. Chaque animation raconte une histoire.
Pas de décorations gratuites. Un vrai boot cinématique en 7 phases.

Phase 0 : Noir total → Ignition (point lumineux)
Phase 1 : Lignes de construction se déploient depuis le centre
Phase 2 : Boot texte tapé caractère par caractère à des positions stratégiques
Phase 3 : Cadre HUD angulaire se dessine lui-même
Phase 4 : Nom de l'assistant révélé dramatiquement
Phase 5 : Indicateurs s'allument un par un
Phase 6 : Système vivant, prêt à l'engagement
"""

from __future__ import annotations

import math
import time

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QBrush, QColor, QFont, QPainter, QPainterPath, QPen,
    QRadialGradient,
)
from PyQt6.QtWidgets import QApplication, QVBoxLayout, QWidget

from ui.paths import _read_full_config
from ui.sound.hud_sound import get_hud_sound


# ── EASING FUNCTIONS ─────────────────────────────────────────────────────────

def ease_out_quart(x: float) -> float:
    return 1.0 - (1.0 - x) ** 4

def ease_out_expo(x: float) -> float:
    return 1.0 if x >= 1.0 else 1.0 - 2.0 ** (-10.0 * x)

def ease_in_quint(x: float) -> float:
    return x ** 5


# ── TIMELINE ELEMENT ─────────────────────────────────────────────────────────

class _TimelineEntry:
    """Un élément animé avec un temps d'entrée et une durée."""
    __slots__ = ("start", "duration", "data")

    def __init__(self, start: float, duration: float, data: dict):
        self.start = start
        self.duration = duration
        self.data = data

    def progress(self, elapsed: float) -> float:
        if elapsed < self.start:
            return 0.0
        raw = (elapsed - self.start) / self.duration
        return min(1.0, raw)


# ── BOOT MESSAGES (position relative, texte, timing) ─────────────────────────

_BOOT_MSGS = [
    # (start_sec, rel_x, rel_y, text, is_highlight)
    (1.0, 0.08, 0.18, "KERNEL    QUANTUM NEURAL OS v9.0.1", False),
    (1.2, 0.08, 0.22, "ARCH      EndeavourOS // Wayland Compositor", False),
    (1.5, 0.08, 0.26, "CPU       Dual-Core Pipeline Allocated", False),
    (1.8, 0.08, 0.30, "MEMORY    11 GB Mapped // Zero-Copy DMA", False),
    (2.1, 0.08, 0.34, "CIPHER    AES-256-GCM Trust Chain Verified", False),
    (2.4, 0.08, 0.38, "AUDIO     DSP Engine Online // Half-Duplex Lock", False),
    (2.7, 0.08, 0.42, "IPC       Socket Bridge Connected", False),
    (3.0, 0.08, 0.46, "NEURAL    Gemini Multi-Modal Link Established", True),
]

_STATUS_INDICATORS = [
    # (start_sec, rel_x, rel_y, label)
    (3.8, 0.72, 0.20, "CORE"),
    (3.9, 0.72, 0.25, "DSP"),
    (4.0, 0.72, 0.30, "NET"),
    (4.1, 0.72, 0.35, "IPC"),
    (4.2, 0.72, 0.40, "CIPHER"),
    (4.3, 0.72, 0.45, "VISION"),
]


class WelcomeHudOverlay(QWidget):
    """Écran de bienvenue cinématique J.A.R.V.I.S."""

    dismissed = pyqtSignal()
    engaged = pyqtSignal()

    def __init__(self, parent: QWidget | None = None, assistant_name: str = "ANO-GPT"):
        super().__init__(parent)
        self._name = (assistant_name or "ANO-GPT").strip().upper()
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)

        self._sound = get_hud_sound()
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick_frame)

        self._elapsed = 0.0
        self._start_time = 0.0
        self._tick = 0

        self._is_engaging = False
        self._engage_t = 0.0
        self._has_emitted = False

        # Parallaxe souris
        self._mouse_x = 0.5
        self._mouse_y = 0.5
        self._smooth_mx = 0.5
        self._smooth_my = 0.5

        self._btn_rect = QRectF()
        self._btn_hovered = False

        # Soundstep tracker (pour ne jouer chaque son qu'une fois)
        self._sound_played: set[int] = set()

    # ── LIFECYCLE ────────────────────────────────────────────────────────────

    def showEvent(self, ev):
        super().showEvent(ev)
        self._start_time = time.monotonic()
        self._elapsed = 0.0
        self._tick = 0
        self._is_engaging = False
        self._engage_t = 0.0
        self._has_emitted = False
        self._sound_played.clear()
        self._timer.start()
        self._sound.play_boot_surge()

    def hideEvent(self, ev):
        super().hideEvent(ev)
        self._timer.stop()

    def closeEvent(self, ev):
        self._timer.stop()
        super().closeEvent(ev)

    def _tick_frame(self):
        self._tick += 1
        self._elapsed = time.monotonic() - self._start_time

        # Lissage de la souris
        self._smooth_mx += (self._mouse_x - self._smooth_mx) * 0.08
        self._smooth_my += (self._mouse_y - self._smooth_my) * 0.08

        # Sons des étapes de boot
        for i, (start, *_) in enumerate(_BOOT_MSGS):
            if self._elapsed >= start and i not in self._sound_played:
                self._sound_played.add(i)
                self._sound.play_chirp(i + 1)

        # Auto-engage
        if not self._is_engaging and self._elapsed > 10.0:
            self._engage()

        if self._is_engaging:
            self._engage_t = min(1.0, (time.monotonic() - self._engage_started_at) / 0.66)
            if self._engage_t >= 1.0 and not self._has_emitted:
                self._has_emitted = True
                self._timer.stop()
                self.hide()
                self.engaged.emit()
                self.dismissed.emit()
                return

        self.update()

    def _engage(self):
        if self._is_engaging:
            return
        self._is_engaging = True
        self._engage_started_at = time.monotonic()
        self._sound.play_access_granted()

    # ── INPUT ────────────────────────────────────────────────────────────────

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key.Key_Escape, Qt.Key.Key_Return, Qt.Key.Key_Space):
            self._engage()
        elif ev.key() == Qt.Key.Key_M:
            self._sound.toggle_mute()
            self.update()

    def mouseMoveEvent(self, ev):
        w, h = self.width(), self.height()
        if w > 0 and h > 0:
            self._mouse_x = ev.position().x() / w
            self._mouse_y = ev.position().y() / h

        was = self._btn_hovered
        self._btn_hovered = self._btn_rect.contains(ev.position())
        if self._btn_hovered and not was:
            self._sound.play_hover()
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        elif not self._btn_hovered and was:
            self.setCursor(Qt.CursorShape.ArrowCursor)

        super().mouseMoveEvent(ev)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            if self._btn_hovered or self._elapsed > 5.0:
                self._engage()
        super().mousePressEvent(ev)

    # ── RENDER ───────────────────────────────────────────────────────────────

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        w = self.width()
        h = self.height()
        cx = w * 0.5
        cy = h * 0.5
        t = self._elapsed
        mx = self._smooth_mx - 0.5  # -0.5..0.5
        my = self._smooth_my - 0.5

        # ── FOND ──
        bg = QRadialGradient(QPointF(cx - mx * 60, cy - my * 60), max(w, h) * 0.8)
        bg.setColorAt(0.0, QColor(4, 12, 24))
        bg.setColorAt(0.6, QColor(1, 4, 10))
        bg.setColorAt(1.0, QColor(0, 0, 0))
        p.fillRect(0, 0, w, h, bg)

        # ── PHASE 0 : IGNITION (0 → 0.6s) ──
        if t < 0.8:
            self._draw_ignition(p, cx, cy, t)
            p.end()
            return  # Rien d'autre pendant l'ignition

        # Parallaxe globale
        p.save()
        p.translate(-mx * 25, -my * 25)

        # ── PHASE 1 : LIGNES DE CONSTRUCTION (0.6 → 1.5s) ──
        self._draw_construction_lines(p, w, h, cx, cy, t)

        # ── PHASE 2 : BOOT TEXTE TYPEWRITER (1.0 → 3.5s) ──
        self._draw_boot_text(p, w, h, t)

        # ── PHASE 3 : CADRE HUD ANGULAIRE (2.0 → 3.5s) ──
        self._draw_hud_frame(p, w, h, t)

        # ── PHASE 4 : NOM DE L'ASSISTANT (3.2 → 4.0s) ──
        self._draw_name_reveal(p, w, h, cx, cy, t)

        # ── PHASE 5 : INDICATEURS DE STATUT (3.8 → 4.5s) ──
        self._draw_status_indicators(p, w, h, t)

        # ── PHASE 6 : SYSTÈME VIVANT (4.5+) ──
        if t > 4.5:
            self._draw_living_system(p, w, h, cx, cy, t, mx, my)

        # ── BOUTON D'ENGAGEMENT ──
        if t > 4.0:
            self._draw_engage_button(p, w, h, cx, cy, t)

        p.restore()

        # ── TRANSITION D'ENGAGEMENT ──
        if self._is_engaging:
            self._draw_engage_transition(p, w, h, cx, cy)

        p.end()

    # ── PHASE 0 : IGNITION ───────────────────────────────────────────────────

    def _draw_ignition(self, p: QPainter, cx: float, cy: float, t: float):
        """Un point de lumière apparaît au centre et pulse une fois."""
        if t < 0.2:
            return  # Noir total

        # Le point apparaît (0.2 → 0.5)
        appear = ease_out_expo(min(1.0, (t - 0.2) / 0.3))
        r = 3 + appear * 8

        # Flash initial puis stabilisation
        if t < 0.5:
            intensity = int(appear * 255)
        else:
            # Pulse unique
            pulse_t = (t - 0.5) / 0.3
            intensity = int(255 * (1.0 - pulse_t * 0.3)) if pulse_t < 1.0 else int(255 * 0.7)

        # Halo
        grad = QRadialGradient(QPointF(cx, cy), r * 15)
        grad.setColorAt(0.0, QColor(0, 200, 255, intensity))
        grad.setColorAt(0.3, QColor(0, 100, 200, intensity // 3))
        grad.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(grad))
        p.drawEllipse(QPointF(cx, cy), r * 15, r * 15)

        # Point central blanc
        p.setBrush(QColor(255, 255, 255, intensity))
        p.drawEllipse(QPointF(cx, cy), r, r)

    # ── PHASE 1 : LIGNES DE CONSTRUCTION ─────────────────────────────────────

    def _draw_construction_lines(self, p: QPainter, w: int, h: int, cx: float, cy: float, t: float):
        """Lignes qui partent du centre vers les bords — le squelette du HUD."""
        if t < 0.6:
            return

        prog = ease_out_quart(min(1.0, (t - 0.6) / 0.8))

        p.setPen(QPen(QColor(0, 180, 255, 60), 1))

        # Ligne horizontale centrale
        half_w = (w * 0.45) * prog
        p.drawLine(QPointF(cx - half_w, cy), QPointF(cx + half_w, cy))

        # Ligne verticale centrale
        half_h = (h * 0.45) * prog
        p.drawLine(QPointF(cx, cy - half_h), QPointF(cx, cy + half_h))

        # Diagonales fines
        diag = min(w, h) * 0.3 * prog
        p.setPen(QPen(QColor(0, 180, 255, 25), 1))
        p.drawLine(QPointF(cx - diag, cy - diag), QPointF(cx + diag, cy + diag))
        p.drawLine(QPointF(cx + diag, cy - diag), QPointF(cx - diag, cy + diag))

        # Cercle de calibrage (apparaît après les lignes)
        if t > 1.0:
            circle_p = ease_out_quart(min(1.0, (t - 1.0) / 0.6))
            r_cal = 100 * circle_p
            p.setPen(QPen(QColor(0, 200, 255, int(40 * circle_p)), 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(cx, cy), r_cal, r_cal)
            p.drawEllipse(QPointF(cx, cy), r_cal * 0.6, r_cal * 0.6)

    # ── PHASE 2 : BOOT TEXTE TYPEWRITER ──────────────────────────────────────

    def _draw_boot_text(self, p: QPainter, w: int, h: int, t: float):
        """Texte tapé caractère par caractère à des positions stratégiques."""
        p.setFont(QFont("Consolas, DejaVu Sans Mono, monospace", 9))

        for start, rx, ry, text, is_hl in _BOOT_MSGS:
            if t < start:
                continue

            # Nombre de caractères visibles (typewriter)
            chars_elapsed = (t - start) * 80  # 80 chars/sec
            visible = min(len(text), int(chars_elapsed))

            if visible <= 0:
                continue

            shown = text[:visible]
            x = int(w * rx)
            y = int(h * ry)

            # Tag coloré (les 10 premiers caractères)
            tag = shown[:min(10, len(shown))]
            rest = shown[10:] if len(shown) > 10 else ""

            p.setPen(QColor(0, 255, 200, 220) if is_hl else QColor(0, 200, 255, 180))
            p.drawText(x, y, tag)

            if rest:
                p.setPen(QColor(180, 210, 230, 200) if is_hl else QColor(120, 150, 180, 180))
                fm = p.fontMetrics()
                tag_w = fm.horizontalAdvance(tag)
                p.drawText(x + tag_w, y, rest)

            # Curseur clignotant si la ligne est encore en cours de frappe
            if visible < len(text):
                if (self._tick // 8) % 2 == 0:
                    fm = p.fontMetrics()
                    cursor_x = x + fm.horizontalAdvance(shown)
                    p.setPen(QColor(0, 255, 255))
                    p.drawText(cursor_x, y, "█")

            # Checkmark quand la ligne est complète
            elif t > start + len(text) / 80 + 0.2:
                fm = p.fontMetrics()
                end_x = x + fm.horizontalAdvance(text) + 10
                p.setPen(QColor(0, 255, 150, 200))
                p.drawText(end_x, y, "✓")

    # ── PHASE 3 : CADRE HUD ANGULAIRE ────────────────────────────────────────

    def _draw_hud_frame(self, p: QPainter, w: int, h: int, t: float):
        """Cadre HUD qui se dessine lui-même depuis les coins."""
        if t < 2.0:
            return

        prog = ease_out_quart(min(1.0, (t - 2.0) / 1.2))
        pad = 30
        arm = 60 * prog
        a = int(180 * prog)

        # Couleur du cadre
        pen = QPen(QColor(0, 200, 255, a), 2)
        p.setPen(pen)

        # Coin Haut-Gauche (L-shape avec biseau)
        p.drawLine(int(pad), int(pad), int(pad + arm), int(pad))
        p.drawLine(int(pad), int(pad), int(pad), int(pad + arm))
        p.drawLine(int(pad + 5), int(pad + 12), int(pad + 12), int(pad + 5))

        # Coin Haut-Droit
        p.drawLine(int(w - pad), int(pad), int(w - pad - arm), int(pad))
        p.drawLine(int(w - pad), int(pad), int(w - pad), int(pad + arm))
        p.drawLine(int(w - pad - 5), int(pad + 12), int(w - pad - 12), int(pad + 5))

        # Coin Bas-Gauche
        p.drawLine(int(pad), int(h - pad), int(pad + arm), int(h - pad))
        p.drawLine(int(pad), int(h - pad), int(pad), int(h - pad - arm))

        # Coin Bas-Droit
        p.drawLine(int(w - pad), int(h - pad), int(w - pad - arm), int(h - pad))
        p.drawLine(int(w - pad), int(h - pad), int(w - pad), int(h - pad - arm))

        # Bandeau supérieur (ligne fine reliant les coins)
        if t > 2.5:
            line_p = ease_out_quart(min(1.0, (t - 2.5) / 0.8))
            half = (w - pad * 2 - arm * 2) * 0.5 * line_p
            mid = w * 0.5
            p.setPen(QPen(QColor(0, 180, 255, int(80 * line_p)), 1))
            p.drawLine(int(mid - half), int(pad), int(mid + half), int(pad))

            # Bandeau inférieur
            p.drawLine(int(mid - half), int(h - pad), int(mid + half), int(h - pad))

    # ── PHASE 4 : RÉVÉLATION DU NOM ──────────────────────────────────────────

    def _draw_name_reveal(self, p: QPainter, w: int, h: int, cx: float, cy: float, t: float):
        """Le nom de l'assistant apparaît avec un effet de balayage horizontal."""
        if t < 3.2:
            return

        reveal_p = ease_out_quart(min(1.0, (t - 3.2) / 0.6))

        # Grande police pour le nom
        font = QFont("Inter, Segoe UI, Arial", 36, QFont.Weight.Black)
        p.setFont(font)
        fm = p.fontMetrics()
        text_w = fm.horizontalAdvance(self._name)
        text_h = fm.height()

        tx = cx - text_w / 2.0
        ty = cy + text_h / 3.0

        # Clip de révélation (balayage gauche → droite)
        p.save()
        clip_w = text_w * reveal_p + 20
        p.setClipRect(QRectF(tx - 10, ty - text_h, clip_w, text_h * 1.5))

        # Texte principal
        p.setPen(QColor(255, 255, 255, int(255 * min(1.0, reveal_p * 1.5))))
        p.drawText(QPointF(tx, ty), self._name)

        # Soulignement animé
        if reveal_p > 0.3:
            line_p = ease_out_quart(min(1.0, (reveal_p - 0.3) / 0.7))
            line_w = text_w * line_p
            p.setPen(QPen(QColor(0, 220, 255, 200), 2))
            p.drawLine(QPointF(tx, ty + 8), QPointF(tx + line_w, ty + 8))

        p.restore()

        # Sous-titre
        if t > 3.8:
            sub_p = ease_out_quart(min(1.0, (t - 3.8) / 0.4))
            p.setFont(QFont("Inter, Segoe UI", 10, QFont.Weight.Bold))
            sub = "NEURAL COGNITION SYSTEM"
            sub_w = p.fontMetrics().horizontalAdvance(sub)
            p.setPen(QColor(0, 200, 255, int(180 * sub_p)))
            p.drawText(QPointF(cx - sub_w / 2.0, ty + 30), sub)

    # ── PHASE 5 : INDICATEURS DE STATUT ──────────────────────────────────────

    def _draw_status_indicators(self, p: QPainter, w: int, h: int, t: float):
        """Indicateurs qui s'allument un par un sur le côté droit."""
        for start, rx, ry, label in _STATUS_INDICATORS:
            if t < start:
                continue

            prog = ease_out_expo(min(1.0, (t - start) / 0.3))
            x = int(w * rx)
            y = int(h * ry)

            # Pastille
            dot_r = 4 * prog
            dot_col = QColor(0, 255, 150, int(255 * prog))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(dot_col)
            p.drawEllipse(QPointF(x, y), dot_r, dot_r)

            # Halo
            if prog > 0.5:
                halo_r = 8
                p.setBrush(QColor(0, 255, 150, int(40 * prog)))
                p.drawEllipse(QPointF(x, y), halo_r, halo_r)

            # Label
            p.setFont(QFont("Consolas, DejaVu Sans Mono", 9, QFont.Weight.Bold))
            p.setPen(QColor(0, 200, 255, int(220 * prog)))
            p.drawText(x + 14, y + 4, label)

            # Barre de connexion
            bar_w = 80 * prog
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 200, 255, int(60 * prog)))
            p.drawRect(QRectF(x + 70, y - 2, bar_w, 4))

            # Valeur
            p.setFont(QFont("Consolas", 8))
            p.setPen(QColor(0, 255, 150, int(200 * prog)))
            p.drawText(int(x + 160), y + 4, "ONLINE")

    # ── PHASE 6 : SYSTÈME VIVANT ─────────────────────────────────────────────

    def _draw_living_system(self, p: QPainter, w: int, h: int, cx: float, cy: float, t: float, mx: float, my: float):
        """Animations subtiles de fond qui donnent vie au HUD."""
        # Respiration du cercle central
        breath = 0.5 + 0.5 * math.sin(t * 1.5)
        r_breath = 100 + breath * 10

        grad = QRadialGradient(QPointF(cx, cy), r_breath)
        grad.setColorAt(0.0, QColor(0, 200, 255, int(30 + breath * 20)))
        grad.setColorAt(0.5, QColor(0, 100, 200, int(10 + breath * 10)))
        grad.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(grad))
        p.drawEllipse(QPointF(cx, cy), r_breath, r_breath)

        # Scanline verticale lente
        scan_x = int(w * ((t * 0.05) % 1.0))
        p.setPen(QPen(QColor(0, 200, 255, 15), 1))
        p.drawLine(scan_x, 0, scan_x, h)

        # Données vivantes dans les coins (horloge, coordonnées)
        p.setFont(QFont("Consolas, DejaVu Sans Mono", 7))
        p.setPen(QColor(0, 180, 255, 120))

        # Horloge temps réel (bas-gauche)
        from datetime import datetime
        now = datetime.now().strftime("%H:%M:%S.%f")[:-4]
        p.drawText(40, h - 50, f"SYS.TIME  {now}")
        p.drawText(40, h - 36, f"ELAPSED   {t:.1f}s")

        # Coordonnées souris (bas-droite)
        p.drawText(w - 200, h - 50, f"CURSOR    {mx + 0.5:.3f} / {my + 0.5:.3f}")
        p.drawText(w - 200, h - 36, f"FRAME     {self._tick}")

    # ── BOUTON D'ENGAGEMENT ──────────────────────────────────────────────────

    def _draw_engage_button(self, p: QPainter, w: int, h: int, cx: float, cy: float, t: float):
        """Bouton d'activation avec animation pulsante."""
        appear = ease_out_quart(min(1.0, (t - 4.0) / 0.6))

        bw = 320
        bh = 50
        bx = cx - bw / 2.0
        by = h - 100
        self._btn_rect = QRectF(bx, by, bw, bh)

        # Le bouton se dessine
        path = QPainterPath()
        c = 12  # Biseau
        path.moveTo(bx + c, by)
        path.lineTo(bx + bw - c, by)
        path.lineTo(bx + bw, by + c)
        path.lineTo(bx + bw, by + bh - c)
        path.lineTo(bx + bw - c, by + bh)
        path.lineTo(bx + c, by + bh)
        path.lineTo(bx, by + bh - c)
        path.lineTo(bx, by + c)
        path.closeSubpath()

        alpha = int(255 * appear)

        if self._btn_hovered:
            p.setPen(QPen(QColor(0, 255, 255, alpha), 2))
            p.setBrush(QColor(0, 200, 255, int(120 * appear)))
        elif self._elapsed > 5.0:
            # Pulse quand prêt
            pulse = 0.5 + 0.5 * math.sin(t * 2.5)
            p.setPen(QPen(QColor(0, 220, 255, int(alpha * (0.5 + pulse * 0.5))), 2))
            p.setBrush(QColor(0, 150, 255, int(30 + pulse * 50)))
        else:
            p.setPen(QPen(QColor(0, 180, 255, int(alpha * 0.6)), 1))
            p.setBrush(QColor(0, 50, 100, int(40 * appear)))

        p.drawPath(path)

        # Texte
        p.setFont(QFont("Inter, Segoe UI", 12, QFont.Weight.Black))
        if self._btn_hovered:
            p.setPen(QColor(255, 255, 255, alpha))
        else:
            p.setPen(QColor(0, 220, 255, alpha))

        if self._elapsed > 5.0:
            p.drawText(self._btn_rect, Qt.AlignmentFlag.AlignCenter, "ENGAGE SYSTEM")
        else:
            # Barre de progression dans le bouton
            fill = min(1.0, (self._elapsed - 4.0) / 1.0)
            p.fillRect(QRectF(bx + 2, by + 2, (bw - 4) * fill, bh - 4), QColor(0, 180, 255, int(60 * appear)))
            p.drawText(self._btn_rect, Qt.AlignmentFlag.AlignCenter, f"LOADING {int(fill * 100)}%")

    # ── TRANSITION D'ENGAGEMENT ──────────────────────────────────────────────

    def _draw_engage_transition(self, p: QPainter, w: int, h: int, cx: float, cy: float):
        """Transition cinématique : les lignes du HUD convergent vers le centre puis flash."""
        et = self._engage_t

        if et < 0.5:
            # Phase 1 : Contraction (tout le HUD se rétracte vers le centre)
            contract = ease_in_quint(et / 0.5)
            # Lignes convergentes
            p.setPen(QPen(QColor(0, 255, 255, int(255 * contract)), 2))
            for angle_deg in range(0, 360, 30):
                angle = math.radians(angle_deg)
                outer_r = max(w, h) * (1.0 - contract)
                inner_r = 20 * (1.0 - contract)
                p.drawLine(
                    QPointF(cx + inner_r * math.cos(angle), cy + inner_r * math.sin(angle)),
                    QPointF(cx + outer_r * math.cos(angle), cy + outer_r * math.sin(angle)),
                )
        else:
            # Phase 2 : Flash blanc depuis le centre
            flash_p = (et - 0.5) / 0.5
            alpha = int(255 * min(1.0, flash_p * 2))

            grad = QRadialGradient(QPointF(cx, cy), max(w, h) * flash_p)
            grad.setColorAt(0.0, QColor(255, 255, 255, alpha))
            grad.setColorAt(0.6, QColor(0, 200, 255, alpha // 2))
            grad.setColorAt(1.0, QColor(0, 0, 0, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(grad))
            p.drawRect(0, 0, w, h)


# ── PREVIEW ──────────────────────────────────────────────────────────────────

def preview_welcome_screen():
    import sys
    app = QApplication.instance() or QApplication(sys.argv)
    cfg = _read_full_config()
    name = cfg.get("assistant_name", "ANO-GPT") or "ANO-GPT"

    win = QWidget()
    win.setWindowTitle(f"{name.upper()} — CINEMATIC HUD")
    win.resize(1280, 800)
    layout = QVBoxLayout(win)
    layout.setContentsMargins(0, 0, 0, 0)
    welcome = WelcomeHudOverlay(win, assistant_name=name)
    layout.addWidget(welcome)
    welcome.engaged.connect(lambda: print("[HUD] System Activated."))
    welcome.dismissed.connect(win.close)
    win.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    preview_welcome_screen()
