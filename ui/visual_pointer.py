"""ui/visual_pointer.py — Système d'annotation visuelle sur écran pour ANO-GPT.

Fournit un overlay plein écran non-bloquant sous Wayland/Hyprland (et X11),
permettant à l'assistant d'indiquer visuellement à l'utilisateur des zones de l'écran,
des points d'intérêt ou des trajectoires au lieu de descriptions textuelles vagues.

Fonctionnalités clés :
1. Overlay transparent Wayland non-bloquant :
   - Fenêtres sans bordure (Qt.WindowType.FramelessWindowHint)
   - Clics et événements souris traversants (Qt.WindowType.WindowTransparentForInput &
     Qt.WidgetAttribute.WA_TransparentForMouseEvents)
   - Transparence matérielle native (Qt.WidgetAttribute.WA_TranslucentBackground)
   - Prise en charge native multi-écrans via fenêtres dédiées synchronisées par translation
     de coordonnées globales du bureau virtuel.

2. Primitives de guidage visuel animées :
   - highlight_region(x, y, w, h, label="Ici", duration=3.0) : rectangle néon pulsant avec
     coins cyberpunk, lueur diffuse et flèche animée oscillante pointant vers la cible.
   - laser_point(x, y, duration=2.0) : point rouge laser ultra-brillant avec ondes concentriques
     (ondes de choc radar) et réticule tournant.
   - draw_path(points, duration=3.0, label="") : trajectoire fluide montrant un déplacement
     recommandé avec pointillés néon en flux continu, comète d'énergie mobile et flèche d'arrivée.

3. Déclenchement automatique par outil :
   - point_on_screen(description: str, coordinates: list[int | float], ...) : outil Gemini
     permettant au modèle de déclencher directement le pointeur adapté.

4. Performance & Économie GIL :
   - Timer d'animation actif UNIQUEMENT pendant les animations.
   - Dès l'expiration des annotations, timer coupé et fenêtres masquées (0% CPU au repos).
   - Thread-safe : invocable depuis les boucles asyncio ou threads de travail via signaux Qt.
"""

from __future__ import annotations

import math
import sys
import time
from dataclasses import dataclass, field
from typing import Any, List, Literal, Optional, Sequence, Tuple, Union

from PyQt6.QtCore import (
    QObject,
    QPoint,
    QPointF,
    QRectF,
    Qt,
    QTimer,
    pyqtSignal,
    pyqtSlot,
)
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
    QScreen,
)
from PyQt6.QtWidgets import QApplication, QWidget


# ══════════════════════════════════════════════════════════════════════════════
# 1. PALETTE HUD & THÈME CYBERPUNK ANO-GPT
# ══════════════════════════════════════════════════════════════════════════════

class PointerTheme:
    """Couleurs et styles néon pour les annotations visuelles."""
    CYAN_CORE = QColor(220, 250, 255)
    CYAN_NEON = QColor(0, 212, 255)
    CYAN_GLOW = QColor(0, 212, 255, 60)
    CYAN_FILL = QColor(0, 212, 255, 24)

    LASER_CORE = QColor(255, 255, 255)
    LASER_RED = QColor(255, 38, 70)
    LASER_GLOW = QColor(255, 30, 60, 80)
    LASER_RIPPLE = QColor(255, 45, 75)

    AMBER_NEON = QColor(255, 185, 0)
    AMBER_GLOW = QColor(255, 185, 0, 80)

    BADGE_BG = QColor(2, 10, 18, 230)
    BADGE_BORDER = QColor(0, 212, 255, 200)
    BADGE_TEXT = QColor(160, 245, 255)


# ══════════════════════════════════════════════════════════════════════════════
# 2. MODÈLES DE DONNÉES DES PRIMITIVES D'ANNOTATION
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class VisualAnnotation:
    """Classe de base pour toute primitive visuelle éphémère."""
    start_time: float = field(default_factory=time.monotonic)
    duration: float = 3.0
    fade_in: float = 0.25
    fade_out: float = 0.45

    def is_expired(self, now: float) -> bool:
        return (now - self.start_time) >= self.duration

    def alpha(self, now: float) -> float:
        elapsed = now - self.start_time
        if elapsed < 0:
            return 0.0
        if elapsed >= self.duration:
            return 0.0

        # Fondu d'entrée
        if elapsed < self.fade_in and self.fade_in > 0:
            a_in = elapsed / self.fade_in
        else:
            a_in = 1.0

        # Fondu de sortie
        remaining = self.duration - elapsed
        if remaining < self.fade_out and self.fade_out > 0:
            a_out = remaining / self.fade_out
        else:
            a_out = 1.0

        return max(0.0, min(1.0, min(a_in, a_out)))

    def paint(self, painter: QPainter, now: float) -> None:
        raise NotImplementedError


@dataclass(frozen=True)
class ScreenTargetBox:
    """Boîte Gemini liée au cliché qui l'a produite.

    Les quatre valeurs sont normalisées 0..1000 dans le repère de la capture.
    ``origin`` et ``size`` sont les coordonnées réelles du moniteur dans le
    bureau virtuel : ne jamais les remplacer par l'écran principal.
    """
    box: Tuple[float, float, float, float]
    origin: Tuple[float, float]
    size: Tuple[float, float]
    monitor: str = ""


class HighlightRegionItem(VisualAnnotation):
    """Rectangle néon pulsant avec coins high-tech et flèche oscillante."""

    def __init__(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        label: str = "Ici",
        duration: float = 3.0,
        color: QColor = PointerTheme.CYAN_NEON,
    ) -> None:
        super().__init__(start_time=time.monotonic(), duration=duration)
        self.x = float(x)
        self.y = float(y)
        self.w = max(10.0, float(w))
        self.h = max(10.0, float(h))
        self.label = (label or "Ici").strip()
        self.color = color

    def paint(self, painter: QPainter, now: float) -> None:
        master_alpha = self.alpha(now)
        if master_alpha <= 0.001:
            return

        elapsed = now - self.start_time
        # Pulsation sinusoïdale fluide (2.5 cycles par seconde)
        pulse = 0.5 + 0.5 * math.sin(elapsed * 5.0)

        rect = QRectF(self.x, self.y, self.w, self.h)
        cx = rect.center().x()
        cy = rect.center().y()

        # ── 1. Remplissage holographique doux ───────────────────────────────
        fill_alpha = int(24 * master_alpha * (0.8 + 0.3 * pulse))
        fill_col = QColor(self.color.red(), self.color.green(), self.color.blue(), fill_alpha)
        painter.setBrush(QBrush(fill_col))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(rect, 8.0, 8.0)

        # ── 2. Bordure néon avec halo multicouche ───────────────────────────
        # Halo externe diffus
        halo_alpha = int(50 * master_alpha * pulse)
        halo_pen = QPen(
            QColor(self.color.red(), self.color.green(), self.color.blue(), halo_alpha),
            8.0 + pulse * 4.0,
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        )
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(halo_pen)
        painter.drawRoundedRect(rect.adjusted(-2, -2, 2, 2), 10.0, 10.0)

        # Ligne médiane néon
        mid_alpha = int(140 * master_alpha)
        mid_pen = QPen(
            QColor(self.color.red(), self.color.green(), self.color.blue(), mid_alpha),
            3.0,
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        )
        painter.setPen(mid_pen)
        painter.drawRoundedRect(rect, 8.0, 8.0)

        # Ligne de cœur lumineuse
        core_alpha = int(230 * master_alpha)
        core_pen = QPen(
            QColor(230, 250, 255, core_alpha),
            1.5,
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        )
        painter.setPen(core_pen)
        painter.drawRoundedRect(rect, 8.0, 8.0)

        # ── 3. Coins High-Tech (Corner Brackets) ────────────────────────────
        bracket_len = min(22.0, self.w * 0.35, self.h * 0.35)
        b_offset = 3.0  # décalage légèrement extérieur
        bracket_pen = QPen(
            QColor(240, 255, 255, int(240 * master_alpha)),
            2.5,
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.SquareCap,
        )
        painter.setPen(bracket_pen)

        # Haut-gauche
        bx1, by1 = self.x - b_offset, self.y - b_offset
        painter.drawLine(QPointF(bx1, by1), QPointF(bx1 + bracket_len, by1))
        painter.drawLine(QPointF(bx1, by1), QPointF(bx1, by1 + bracket_len))

        # Haut-droite
        bx2, by2 = self.x + self.w + b_offset, self.y - b_offset
        painter.drawLine(QPointF(bx2, by2), QPointF(bx2 - bracket_len, by2))
        painter.drawLine(QPointF(bx2, by2), QPointF(bx2, by2 + bracket_len))

        # Bas-gauche
        bx3, by3 = self.x - b_offset, self.y + self.h + b_offset
        painter.drawLine(QPointF(bx3, by3), QPointF(bx3 + bracket_len, by3))
        painter.drawLine(QPointF(bx3, by3), QPointF(bx3, by3 - bracket_len))

        # Bas-droite
        bx4, by4 = self.x + self.w + b_offset, self.y + self.h + b_offset
        painter.drawLine(QPointF(bx4, by4), QPointF(bx4 - bracket_len, by4))
        painter.drawLine(QPointF(bx4, by4), QPointF(bx4, by4 - bracket_len))

        # ── 4. Flèche animée oscillante ─────────────────────────────────────
        # Positionnement intelligent : au-dessus si l'espace le permet, sinon au-dessous
        bounce = math.sin(elapsed * 7.0) * 8.0
        arrow_w = 26.0
        arrow_h = 24.0

        if self.y >= 90.0:
            # Pointe vers le bas, vers le haut du rectangle
            tip_y = self.y - 8.0 + bounce
            base_y = tip_y - arrow_h

            arrow_path = QPainterPath()
            arrow_path.moveTo(cx, tip_y)
            arrow_path.lineTo(cx - arrow_w / 2.0, base_y)
            arrow_path.lineTo(cx - arrow_w * 0.2, base_y + 4.0)
            arrow_path.lineTo(cx - arrow_w * 0.2, base_y - 8.0)
            arrow_path.lineTo(cx + arrow_w * 0.2, base_y - 8.0)
            arrow_path.lineTo(cx + arrow_w * 0.2, base_y + 4.0)
            arrow_path.lineTo(cx + arrow_w / 2.0, base_y)
            arrow_path.closeSubpath()

            badge_center_y = base_y - 20.0
        else:
            # Pointe vers le haut, vers le bas du rectangle
            tip_y = self.y + self.h + 8.0 - bounce
            base_y = tip_y + arrow_h

            arrow_path = QPainterPath()
            arrow_path.moveTo(cx, tip_y)
            arrow_path.lineTo(cx - arrow_w / 2.0, base_y)
            arrow_path.lineTo(cx - arrow_w * 0.2, base_y - 4.0)
            arrow_path.lineTo(cx - arrow_w * 0.2, base_y + 8.0)
            arrow_path.lineTo(cx + arrow_w * 0.2, base_y + 8.0)
            arrow_path.lineTo(cx + arrow_w * 0.2, base_y - 4.0)
            arrow_path.lineTo(cx + arrow_w / 2.0, base_y)
            arrow_path.closeSubpath()

            badge_center_y = base_y + 20.0

        # Rendu de la flèche avec dégradé néon
        arrow_grad = QLinearGradient(cx, tip_y, cx, base_y)
        arrow_grad.setColorAt(0.0, QColor(255, 255, 255, int(240 * master_alpha)))
        arrow_grad.setColorAt(0.5, QColor(0, 212, 255, int(220 * master_alpha)))
        arrow_grad.setColorAt(1.0, QColor(0, 160, 220, int(150 * master_alpha)))

        painter.setBrush(QBrush(arrow_grad))
        painter.setPen(QPen(QColor(0, 230, 255, int(230 * master_alpha)), 1.5))
        painter.drawPath(arrow_path)

        # ── 5. Badge d'étiquette textuelle ──────────────────────────────────
        if self.label:
            font = QFont("Inter", 10, QFont.Weight.Bold)
            font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
            painter.setFont(font)
            fm = QFontMetricsF(font)

            badge_text = f"◈ {self.label}"
            text_w = fm.horizontalAdvance(badge_text)
            text_h = fm.height()
            pad_x, pad_y = 12.0, 6.0
            bw = text_w + pad_x * 2.0
            bh = text_h + pad_y * 2.0
            bx = cx - bw / 2.0
            by = badge_center_y - bh / 2.0

            badge_rect = QRectF(bx, by, bw, bh)

            # Fond glassmorphe
            bg_col = QColor(
                PointerTheme.BADGE_BG.red(),
                PointerTheme.BADGE_BG.green(),
                PointerTheme.BADGE_BG.blue(),
                int(220 * master_alpha),
            )
            border_col = QColor(
                PointerTheme.BADGE_BORDER.red(),
                PointerTheme.BADGE_BORDER.green(),
                PointerTheme.BADGE_BORDER.blue(),
                int(190 * master_alpha),
            )
            painter.setBrush(QBrush(bg_col))
            painter.setPen(QPen(border_col, 1.2))
            painter.drawRoundedRect(badge_rect, 6.0, 6.0)

            # Texte néon
            text_col = QColor(
                PointerTheme.BADGE_TEXT.red(),
                PointerTheme.BADGE_TEXT.green(),
                PointerTheme.BADGE_TEXT.blue(),
                int(250 * master_alpha),
            )
            painter.setPen(QPen(text_col))
            painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, badge_text)


class LaserPointItem(VisualAnnotation):
    """Point rouge laser animé avec onde de choc concentrique et réticule tournant."""

    def __init__(
        self,
        x: float,
        y: float,
        duration: float = 2.0,
        color: QColor = PointerTheme.LASER_RED,
    ) -> None:
        super().__init__(start_time=time.monotonic(), duration=duration, fade_in=0.15, fade_out=0.35)
        self.x = float(x)
        self.y = float(y)
        self.color = color

    def paint(self, painter: QPainter, now: float) -> None:
        master_alpha = self.alpha(now)
        if master_alpha <= 0.001:
            return

        elapsed = now - self.start_time
        center = QPointF(self.x, self.y)

        # ── 1. Ondes concentriques (3 anneaux déphasés) ──────────────────────
        painter.setBrush(Qt.BrushStyle.NoBrush)
        waves_count = 3
        max_wave_r = 75.0

        for i in range(waves_count):
            # Phase d'expansion de 0.0 à 1.0
            phase = (elapsed * 1.6 + i / float(waves_count)) % 1.0
            r = 10.0 + phase * (max_wave_r - 10.0)
            wave_alpha = int((1.0 - phase) * master_alpha * 210)
            pen_width = max(1.0, 2.4 * (1.0 - phase * 0.4))

            wave_pen = QPen(
                QColor(self.color.red(), self.color.green(), self.color.blue(), wave_alpha),
                pen_width,
            )
            painter.setPen(wave_pen)
            painter.drawEllipse(center, r, r)

        # ── 2. Halo rouge diffus radiant ───────────────────────────────────
        halo_r = 28.0
        halo_grad = QRadialGradient(center, halo_r)
        halo_grad.setColorAt(0.0, QColor(255, 60, 90, int(190 * master_alpha)))
        halo_grad.setColorAt(0.4, QColor(255, 20, 50, int(100 * master_alpha)))
        halo_grad.setColorAt(1.0, QColor(255, 0, 40, 0))

        painter.setBrush(QBrush(halo_grad))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(center, halo_r, halo_r)

        # ── 3. Cœur laser ultra-brillant (Point d'impact) ────────────────────
        core_r = 5.5
        core_grad = QRadialGradient(center, core_r)
        core_grad.setColorAt(0.0, QColor(255, 255, 255, int(255 * master_alpha)))
        core_grad.setColorAt(0.5, QColor(255, 120, 150, int(240 * master_alpha)))
        core_grad.setColorAt(1.0, QColor(255, 30, 70, int(200 * master_alpha)))

        painter.setBrush(QBrush(core_grad))
        painter.drawEllipse(center, core_r, core_r)

        # ── 4. Réticule rotatif haute précision ─────────────────────────────
        rot_angle = elapsed * 55.0  # Degrés par seconde
        painter.save()
        painter.translate(center)
        painter.rotate(rot_angle)

        reticle_pen = QPen(
            QColor(255, 160, 180, int(210 * master_alpha)),
            1.5,
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
        )
        painter.setPen(reticle_pen)

        r_inner = 13.0
        r_outer = 22.0
        # 4 tirets cardinaux
        painter.drawLine(QPointF(r_inner, 0), QPointF(r_outer, 0))
        painter.drawLine(QPointF(-r_inner, 0), QPointF(-r_outer, 0))
        painter.drawLine(QPointF(0, r_inner), QPointF(0, r_outer))
        painter.drawLine(QPointF(0, -r_inner), QPointF(0, -r_outer))

        painter.restore()


class DrawPathItem(VisualAnnotation):
    """Trajectoire recommandée avec flux néon continu, comète et flèche de fin."""

    def __init__(
        self,
        points: Sequence[Tuple[float, float]],
        duration: float = 3.5,
        label: str = "",
        color: QColor = PointerTheme.CYAN_NEON,
    ) -> None:
        super().__init__(start_time=time.monotonic(), duration=duration)
        self.points = [QPointF(float(p[0]), float(p[1])) for p in points]
        self.label = (label or "").strip()
        self.color = color

        self._path = QPainterPath()
        if self.points:
            self._path.moveTo(self.points[0])
            for pt in self.points[1:]:
                self._path.lineTo(pt)
        self._length = max(1.0, self._path.length())

    def paint(self, painter: QPainter, now: float) -> None:
        if len(self.points) < 2:
            return

        master_alpha = self.alpha(now)
        if master_alpha <= 0.001:
            return

        elapsed = now - self.start_time

        # ── 1. Faisceau néon diffus ─────────────────────────────────────────
        halo_pen = QPen(
            QColor(self.color.red(), self.color.green(), self.color.blue(), int(60 * master_alpha)),
            9.0,
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        )
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(halo_pen)
        painter.strokePath(self._path, halo_pen)

        # ── 2. Trait continu néon pulsé ─────────────────────────────────────
        flow_pen = QPen(
            QColor(self.color.red(), self.color.green(), self.color.blue(), int(200 * master_alpha)),
            3.0,
            Qt.PenStyle.CustomDashLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        )
        flow_pen.setDashPattern([12.0, 9.0])
        # Défilement dynamique des tirets montrant le sens du mouvement
        flow_pen.setDashOffset(-elapsed * 60.0)
        painter.strokePath(self._path, flow_pen)

        # ── 3. Cœur lumineux ────────────────────────────────────────────────
        core_pen = QPen(
            QColor(230, 250, 255, int(220 * master_alpha)),
            1.5,
            Qt.PenStyle.CustomDashLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        )
        core_pen.setDashPattern([12.0, 9.0])
        core_pen.setDashOffset(-elapsed * 60.0)
        painter.strokePath(self._path, core_pen)

        # ── 4. Comète d'énergie mobile le long de la trajectoire ────────────
        cycle_period = max(1.4, min(3.5, self._length / 350.0))
        comet_progress = (elapsed / cycle_period) % 1.0
        comet_pos = self._path.pointAtPercent(comet_progress)

        # Halo comète
        comet_grad = QRadialGradient(comet_pos, 16.0)
        comet_grad.setColorAt(0.0, QColor(255, 255, 255, int(250 * master_alpha)))
        comet_grad.setColorAt(0.3, QColor(0, 220, 255, int(200 * master_alpha)))
        comet_grad.setColorAt(1.0, QColor(0, 200, 255, 0))

        painter.setBrush(QBrush(comet_grad))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(comet_pos, 16.0, 16.0)

        # Cœur blanc de la comète
        painter.setBrush(QBrush(QColor(255, 255, 255, int(255 * master_alpha))))
        painter.drawEllipse(comet_pos, 4.5, 4.5)

        # ── 5. Marqueur Départ (Point A) ────────────────────────────────────
        start_pt = self.points[0]
        start_pulse = 0.5 + 0.5 * math.sin(elapsed * 6.0)
        start_r = 7.0 + start_pulse * 3.0

        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(0, 212, 255, int(180 * master_alpha)), 2.0))
        painter.drawEllipse(start_pt, start_r, start_r)
        painter.setBrush(QBrush(QColor(0, 240, 255, int(220 * master_alpha))))
        painter.drawEllipse(start_pt, 3.5, 3.5)

        # ── 6. Marqueur Cible d'Arrivée (Point B avec flèche orientée) ──────
        end_pt = self.points[-1]
        prev_pt = self.points[-2]
        dx = end_pt.x() - prev_pt.x()
        dy = end_pt.y() - prev_pt.y()
        target_angle = math.degrees(math.atan2(dy, dx))

        # Réticule de destination
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(255, 200, 0, int(220 * master_alpha)), 2.0))
        painter.drawEllipse(end_pt, 12.0, 12.0)
        painter.drawEllipse(end_pt, 5.0, 5.0)

        # Tête de flèche d'arrivée
        painter.save()
        painter.translate(end_pt)
        painter.rotate(target_angle)

        arr_len = 16.0
        arr_path = QPainterPath()
        arr_path.moveTo(4.0, 0)
        arr_path.lineTo(-arr_len, -arr_len * 0.55)
        arr_path.lineTo(-arr_len * 0.65, 0)
        arr_path.lineTo(-arr_len, arr_len * 0.55)
        arr_path.closeSubpath()

        painter.setBrush(QBrush(QColor(255, 210, 0, int(230 * master_alpha))))
        painter.setPen(QPen(QColor(255, 255, 255, int(240 * master_alpha)), 1.2))
        painter.drawPath(arr_path)
        painter.restore()

        # ── 7. Libellé optionnel de trajectoire ──────────────────────────────
        if self.label:
            mid_pt = self._path.pointAtPercent(0.5)
            font = QFont("Inter", 9, QFont.Weight.Bold)
            painter.setFont(font)
            fm = QFontMetricsF(font)
            text_w = fm.horizontalAdvance(self.label)
            text_h = fm.height()
            pad_x, pad_y = 10.0, 5.0
            bw = text_w + pad_x * 2.0
            bh = text_h + pad_y * 2.0
            badge_rect = QRectF(mid_pt.x() - bw / 2.0, mid_pt.y() - bh - 10.0, bw, bh)

            painter.setBrush(QBrush(QColor(2, 10, 18, int(220 * master_alpha))))
            painter.setPen(QPen(QColor(0, 212, 255, int(180 * master_alpha)), 1.0))
            painter.drawRoundedRect(badge_rect, 5.0, 5.0)

            painter.setPen(QPen(QColor(160, 245, 255, int(240 * master_alpha))))
            painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, self.label)


# ══════════════════════════════════════════════════════════════════════════════
# 3. FENÊTRE OVERLAY INDÉPENDANTE PAR ÉCRAN (WAYLAND / HYPRLAND COMPLIANT)
# ══════════════════════════════════════════════════════════════════════════════

class ScreenOverlayWindow(QWidget):
    """Fenêtre plein écran transparente et non-bloquante attachée à un écran précis.

    Sous Wayland, une surface xdg_toplevel appartient à un écran (output).
    Afin de couvrir parfaitement des configurations multi-moniteurs hétérogènes
    (ex: résolutions différentes, orientations, offsets virtuels), une instance
    ScreenOverlayWindow est créée par écran physique.

    Le gestionnaire central VisualPointerOverlay synchronise toutes les fenêtres
    via une translation de coordonnées globales (painter.translate(-screen_x, -screen_y)),
    ce qui permet à toutes les primitives de travailler en coordonnées absolues
    du bureau virtuel sans se soucier du découpage physique des écrans.
    """

    def __init__(self, screen: QScreen, manager: VisualPointerOverlay) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self._target_screen = screen
        self._manager = manager

        self.setWindowTitle(f"ANO Visual Pointer [{screen.name()}]")

        # Configuration critique Wayland / X11 : clics 100% traversants
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setStyleSheet("background: transparent; border: none;")

        # Ajuster géométrie à l'écran
        self.setScreen(screen)
        geom = screen.geometry()
        self.setGeometry(geom)

        # Masqué par défaut (0% CPU, pas d'affichage)
        self.hide()

    def sync_geometry(self) -> None:
        """Recale la géométrie si la résolution ou position d'écran a changé."""
        if self._target_screen:
            self.setGeometry(self._target_screen.geometry())

    def paintEvent(self, _event) -> None:
        """Rendu des annotations actives de la scène globale."""
        items = self._manager.active_annotations
        if not items:
            return

        now = time.monotonic()
        screen_geom = self._target_screen.geometry()

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        # Translation globale -> locale : permet d'écrire du code de dessin
        # directement en coordonnées absolues multi-écrans !
        painter.translate(-screen_geom.x(), -screen_geom.y())

        for item in items:
            item.paint(painter, now)

        painter.end()


# Un pointeur qui se trompe est pire qu'un pointeur absent : l'utilisateur
# regarde là où on lui dit de regarder. Ces bornes écartent les réponses qui
# ne sont pas des localisations mais des aveux d'ignorance déguisés.
_MIN_BOX_AREA = 0.00012   # plus petit qu'une icône : le modèle a visé au hasard
_MAX_BOX_AREA = 0.55      # « c'est quelque part par là » n'est pas une réponse
_MIN_BOX_SPAN = 4.0       # sur 1000 : en deçà, la boîte est dégénérée
_MIN_CONFIDENCE = 55.0
_VERIFY_BELOW = 85.0      # sous ce seuil, on recoupe avant d'afficher


def _valid_target_box(values: Tuple[float, float, float, float]) -> bool:
    """Rejette les boîtes géométriquement absurdes avant tout affichage."""
    ymin, xmin, ymax, xmax = values
    if not all(0.0 <= value <= 1000.0 for value in values):
        return False
    height, width = ymax - ymin, xmax - xmin
    if height < _MIN_BOX_SPAN or width < _MIN_BOX_SPAN:
        # Inclut le cas inversé (ymin > ymax), signature classique d'invention.
        return False
    area = (height / 1000.0) * (width / 1000.0)
    return _MIN_BOX_AREA <= area <= _MAX_BOX_AREA


def _confirm_target_box(client, gtypes, img_bytes: bytes, mime: str,
                        values: Tuple[float, float, float, float],
                        target: str, models) -> bool:
    """Recadre la zone trouvée et demande si elle contient vraiment la cible.

    C'est la seule vérification qui attrape un pointage au hasard : le modèle
    ne revoit plus tout l'écran, seulement ce qu'il a désigné. Une invention ne
    survit pas au gros plan.
    """
    try:
        import io
        from PIL import Image
    except Exception:
        return True  # Sans Pillow, on ne bloque pas : on n'a rien à opposer.
    try:
        image = Image.open(io.BytesIO(img_bytes))
        width, height = image.size
        ymin, xmin, ymax, xmax = values
        pad_x = max(12.0, (xmax - xmin) * 0.25 / 1000.0 * width)
        pad_y = max(12.0, (ymax - ymin) * 0.25 / 1000.0 * height)
        left = max(0, int(xmin / 1000.0 * width - pad_x))
        top = max(0, int(ymin / 1000.0 * height - pad_y))
        right = min(width, int(xmax / 1000.0 * width + pad_x))
        bottom = min(height, int(ymax / 1000.0 * height + pad_y))
        if right - left < 8 or bottom - top < 8:
            return False
        buffer = io.BytesIO()
        image.crop((left, top, right, bottom)).convert("RGB").save(
            buffer, format="JPEG", quality=92)
        crop_bytes = buffer.getvalue()
    except Exception:
        return True

    from core.multimodal_vision import _call_gemini_vision
    prompt = (
        f"Ce gros plan est extrait d'une capture d'écran. Contient-il « {target} » ?\n"
        "Réponds UNIQUEMENT par un JSON strict : {\"match\": true} ou {\"match\": false}.\n"
        "En cas de doute, réponds false."
    )
    try:
        resp, _model = _call_gemini_vision(
            client, gtypes,
            [gtypes.Part.from_bytes(data=crop_bytes, mime_type="image/jpeg"), prompt],
            models,
        )
        import json as _json
        import re as _re
        text = (getattr(resp, "text", "") or "").strip()
        match = _re.search(r"\{.*\}", text, _re.DOTALL)
        if not match:
            return False
        return bool(_json.loads(match.group(0)).get("match"))
    except Exception as exc:
        print(f"[VisualPointer] Contre-vérification impossible : {exc}")
        return True


def detect_screen_target_live(target: str, query: str = "") -> Optional[ScreenTargetBox]:
    """Capture l'écran en direct et localise la cible, ou n'affiche rien.

    Le contrat est asymétrique et volontairement : ne rien montrer coûte une
    phrase, montrer le mauvais endroit coûte la confiance.
    """
    try:
        import json
        import re
        from core import screen_capture
        from core.multimodal_vision import (
            _call_gemini_vision, _get_api_key, capture_policy,
            vision_model_cascade,
        )

        api_key = _get_api_key()
        if not api_key:
            return None

        # Le moniteur qui contient la fenêtre active est la seule référence
        # utile pour « regarde ici ». Une capture de tout le bureau virtuel
        # puis une projection sur primaryScreen décalait le pointeur dès que
        # l'écran actif n'était pas l'écran principal.
        # Localiser une icône ou un libellé de menu se joue sur quelques
        # pixels : la politique par défaut (1600x1000, qualité 85) les efface.
        # On reprend celle réservée au texte, déjà calibrée pour ça.
        policy = capture_policy(domain="document")
        img_bytes, mime, metadata = screen_capture.capture_window_or_screen(
            target="monitor", compress=True,
            max_dim=policy["max_dim"], quality=policy["quality"],
        )
        if not img_bytes:
            return None

        from google import genai
        from google.genai import types as gtypes

        client = genai.Client(api_key=api_key)
        # Le modèle vision configuré (Pro par défaut) au lieu de Flash en dur :
        # la localisation fine est exactement ce que Pro fait mieux.
        models = vision_model_cascade()
        prompt = (
            f"Analyse cette capture d'écran et localise précisément l'élément « {target} ».\n"
            f"Contexte additionnel : {query}\n"
            "Retourne UNIQUEMENT un JSON strict :\n"
            '{"box": [ymin, xmin, ymax, xmax], "label": "ce qui se trouve vraiment '
            'à cet endroit", "confidence": 0-100}\n'
            "Coordonnées entières normalisées de 0 à 1000, dans le repère de cette image.\n"
            "La boîte doit serrer l'élément au plus près, jamais toute la fenêtre.\n"
            "Si l'élément n'est PAS VISIBLE sur cette capture, retourne exactement :\n"
            '{"box": null, "label": "", "confidence": 0}\n'
            "Ne devine jamais une position : l'absence de réponse est préférable "
            "à une position approximative."
        )
        resp, _model = _call_gemini_vision(
            client, gtypes,
            [gtypes.Part.from_bytes(data=img_bytes, mime_type=mime), prompt],
            models,
        )
        text = (getattr(resp, "text", "") or "").strip()
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        data = json.loads(match.group(0))
        box = data.get("box")
        if not (isinstance(box, list) and len(box) >= 4):
            return None
        values = tuple(float(v) for v in box[:4])
        if not _valid_target_box(values):
            print(f"[VisualPointer] Boîte rejetée pour « {target} » : {values}")
            return None
        try:
            confidence = float(data.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence and confidence < _MIN_CONFIDENCE:
            print(f"[VisualPointer] « {target} » abandonné : confiance {confidence:.0f}.")
            return None
        if confidence < _VERIFY_BELOW and not _confirm_target_box(
                client, gtypes, img_bytes, mime, values, target, models):
            print(f"[VisualPointer] « {target} » infirmé par la contre-vérification.")
            return None

        origin = metadata.get("capture_origin") or (0, 0)
        size = metadata.get("capture_size") or (0, 0)
        if len(origin) != 2 or len(size) != 2 or size[0] <= 0 or size[1] <= 0:
            return None
        return ScreenTargetBox(
            box=values,
            origin=(float(origin[0]), float(origin[1])),
            size=(float(size[0]), float(size[1])),
            monitor=str(metadata.get("monitor") or ""),
        )
    except Exception as exc:
        print(f"[VisualPointer] Détection écran temps réel ignorée : {exc}")
    return None


# ══════════════════════════════════════════════════════════════════════════════
# 4. GESTIONNAIRE D'OVERLAY CENTRALISÉ & THREAD-SAFE (VisualPointerOverlay)
# ══════════════════════════════════════════════════════════════════════════════

class VisualPointerOverlay(QObject):
    """Contrôleur global des annotations visuelles sur écran pour ANO-GPT.

    Gère :
    - La liste des overlays par écran physique (ScreenOverlayWindow)
    - Le cycle de vie des primitives (highlight, laser, path)
    - La boucle d'animation à faible consommation (active seulement si besoin)
    - La sécurité multi-thread via signaux Qt.
    """

    # Signaux pour appels sécurisés depuis n'importe quel thread d'arrière-plan
    sig_highlight = pyqtSignal(float, float, float, float, str, float)
    sig_laser = pyqtSignal(float, float, float)
    sig_path = pyqtSignal(list, float, str)
    sig_clear = pyqtSignal()

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._annotations: List[VisualAnnotation] = []
        self._windows: List[ScreenOverlayWindow] = []
        self._target_registry: dict[str, Any] = {}

        # Boucle d'animation fluide (~45 FPS, 22ms)
        self._timer = QTimer(self)
        self._timer.setInterval(22)
        self._timer.timeout.connect(self._on_tick)

        # Connexion des signaux inter-threads
        self.sig_highlight.connect(self._do_highlight_region)
        self.sig_laser.connect(self._do_laser_point)
        self.sig_path.connect(self._do_draw_path)
        self.sig_clear.connect(self._do_clear)

        # Surveillance dynamique des écrans connectés
        app = QApplication.instance()
        if app:
            app.screenAdded.connect(self._rebuild_screen_overlays)
            app.screenRemoved.connect(self._rebuild_screen_overlays)
            self._rebuild_screen_overlays()

    @property
    def active_annotations(self) -> List[VisualAnnotation]:
        return self._annotations

    def _rebuild_screen_overlays(self) -> None:
        """Recrée ou adapte les fenêtres d'overlay pour chaque écran physique."""
        # Fermeture propre des anciennes fenêtres
        for win in self._windows:
            win.hide()
            win.deleteLater()
        self._windows.clear()

        app = QApplication.instance()
        if not app:
            return

        screens = app.screens()
        for scr in screens:
            try:
                win = ScreenOverlayWindow(scr, self)
                self._windows.append(win)
            except Exception as exc:
                print(f"[VisualPointer] ⚠️ Impossible de créer l'overlay pour {scr.name()}: {exc}")

    def _ensure_active(self) -> None:
        """Démarre le timer et affiche les overlays si des annotations sont actives."""
        if not self._windows:
            self._rebuild_screen_overlays()

        for win in self._windows:
            win.sync_geometry()
            if not win.isVisible():
                win.show()
                win.raise_()

        if not self._timer.isActive():
            self._timer.start()

    def _on_tick(self) -> None:
        """Étape d'animation : purge les éléments expirés et redessine."""
        now = time.monotonic()
        # Filtre les éléments encore en vie
        self._annotations = [item for item in self._annotations if not item.is_expired(now)]

        if not self._annotations:
            # Plus aucune animation : on coupe immédiatement le timer et masque les fenêtres
            # afin de garantir 0% CPU au repos (respect de la contrainte GIL/audio) !
            self._timer.stop()
            for win in self._windows:
                win.hide()
            return

        # Redessine toutes les fenêtres d'écran
        for win in self._windows:
            if win.isVisible():
                win.update()

    # ── Implémentations privées sur le thread GUI ─────────────────────────────

    @pyqtSlot(float, float, float, float, str, float)
    def _do_highlight_region(
        self, x: float, y: float, w: float, h: float, label: str, duration: float
    ) -> None:
        item = HighlightRegionItem(x, y, w, h, label=label, duration=duration)
        self._annotations.append(item)
        self._ensure_active()
        for win in self._windows:
            win.update()

    @pyqtSlot(float, float, float)
    def _do_laser_point(self, x: float, y: float, duration: float) -> None:
        item = LaserPointItem(x, y, duration=duration)
        self._annotations.append(item)
        self._ensure_active()
        for win in self._windows:
            win.update()

    @pyqtSlot(list, float, str)
    def _do_draw_path(self, points: list, duration: float, label: str) -> None:
        item = DrawPathItem(points, duration=duration, label=label)
        self._annotations.append(item)
        self._ensure_active()
        for win in self._windows:
            win.update()

    @pyqtSlot()
    def _do_clear(self) -> None:
        self._annotations.clear()
        self._timer.stop()
        for win in self._windows:
            win.hide()

    # ── API Publique Thread-Safe ──────────────────────────────────────────────

    def highlight_region(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        label: str = "Ici",
        duration: float = 3.0,
    ) -> None:
        """Dessine un rectangle néon pulsant autour de la zone cible avec flèche animée."""
        self.sig_highlight.emit(float(x), float(y), float(w), float(h), str(label), float(duration))

    def laser_point(self, x: float, y: float, duration: float = 2.0) -> None:
        """Affiche un point rouge laser animé avec ondes concentriques."""
        self.sig_laser.emit(float(x), float(y), float(duration))

    def draw_path(
        self,
        points: Sequence[Tuple[float, float]] | Sequence[Sequence[float]],
        duration: float = 3.5,
        label: str = "",
    ) -> None:
        """Affiche une trajectoire montrant un déplacement recommandé."""
        clean_pts = [[float(p[0]), float(p[1])] for p in points if len(p) >= 2]
        self.sig_path.emit(clean_pts, float(duration), str(label))

    def clear(self) -> None:
        """Efface immédiatement toutes les annotations et masque les overlays."""
        self.sig_clear.emit()

    # ── Résolution Dynamique de Cibles et Détection Réelle ─────────────────────

    def register_target_widget(self, name: str, widget: Any) -> None:
        """Enregistre un widget cible de l'interface pour localisation géométrique directe."""
        self._target_registry[name.strip().lower()] = widget

    def unregister_target_widget(self, name: str) -> None:
        """Désenregistre un widget cible."""
        self._target_registry.pop(name.strip().lower(), None)

    def resolve_widget_geometry(self, target_name: str) -> Optional[Tuple[QWidget, float, float, float, float]]:
        """Résout un widget nommé et vérifie sa visibilité effective."""
        key = (target_name or "").strip().lower()
        widget = self._target_registry.get(key)

        # Si non trouvé exactement, recherche souple dans le registre
        if widget is None:
            for k, w in self._target_registry.items():
                if key in k or k in key:
                    widget = w
                    break

        # Recherche dans QApplication.allWidgets()
        if widget is None:
            app = QApplication.instance()
            if app:
                for w in app.allWidgets():
                    obj_name = (w.objectName() or "").lower()
                    if key in obj_name or (hasattr(w, "card_type") and key in str(getattr(w, "card_type", "")).lower()):
                        widget = w
                        break

        if widget is None:
            return None

        # Vérification stricte de visibilité
        if not widget.isVisible() or widget.width() <= 0 or widget.height() <= 0:
            return (widget, 0.0, 0.0, 0.0, 0.0)

        parent = widget.parentWidget()
        while parent is not None:
            if not parent.isVisible():
                return (widget, 0.0, 0.0, 0.0, 0.0)
            parent = parent.parentWidget()

        pos = widget.mapToGlobal(QPoint(0, 0))
        return (widget, float(pos.x()), float(pos.y()), float(widget.width()), float(widget.height()))

    def point_on_target(
        self,
        target: str,
        description: str = "",
        duration: float = 3.0,
    ) -> str:
        """Résout une cible par son nom (widget interne ou élément d'écran externe)
        et refuse catégoriquement d'encadrer si la cible n'est pas visible.
        """
        clean_target = str(target or "").strip()
        if not clean_target:
            return "Erreur : aucune cible spécifiée pour le pointage."

        # 1. Résolution widget interne
        resolved = self.resolve_widget_geometry(clean_target)
        if resolved is not None:
            widget, gx, gy, gw, gh = resolved
            if gw <= 0 or gh <= 0 or not widget.isVisible():
                return f"Le widget '{clean_target}' n'est pas affiché actuellement à l'écran."

            pad = 4.0
            rx = gx - pad
            ry = gy - pad
            rw = gw + pad * 2.0
            rh = gh + pad * 2.0
            lbl = description or f"Cible : {clean_target}"
            self.highlight_region(rx, ry, rw, rh, label=lbl, duration=duration)
            return (
                f"Zone '{clean_target}' encadrée avec succès à ({rx:.0f}, {ry:.0f}, {rw:.0f}x{rh:.0f}) "
                f"pendant {duration:.1f}s."
            )

        # 2. Détection en direct sur l'écran capturé pour les éléments externes
        box = detect_screen_target_live(clean_target, query=description)
        if box is None:
            return f"L'élément '{clean_target}' n'est pas visible sur votre écran actuellement."

        # Tolérance de compatibilité pour les extensions qui renvoyaient
        # historiquement une simple liste. Le chemin natif retourne toujours
        # ScreenTargetBox et est donc attaché à la capture réellement analysée.
        if isinstance(box, ScreenTargetBox):
            ymin, xmin, ymax, xmax = box.box
            screen_x, screen_y = box.origin
            screen_w, screen_h = box.size
            # Qt est l'autorité finale pour l'overlay : sur un écran HiDPI
            # les dimensions Hyprland de capture peuvent être physiques alors
            # que les coordonnées de QWidget sont logiques.
            app = QApplication.instance()
            if app is not None and box.monitor:
                matched = next((screen for screen in app.screens()
                                if screen.name() == box.monitor), None)
                if matched is not None:
                    geometry = matched.geometry()
                    screen_x, screen_y = geometry.x(), geometry.y()
                    screen_w, screen_h = geometry.width(), geometry.height()
        else:
            app = QApplication.instance()
            prim = app.primaryScreen() if app else None
            screen_w = prim.geometry().width() if prim else 1920
            screen_h = prim.geometry().height() if prim else 1080
            screen_x = prim.geometry().x() if prim else 0
            screen_y = prim.geometry().y() if prim else 0
            ymin, xmin, ymax, xmax = box[0], box[1], box[2], box[3]

        bx = screen_x + (xmin / 1000.0) * screen_w
        by = screen_y + (ymin / 1000.0) * screen_h
        bw = max(20.0, ((xmax - xmin) / 1000.0) * screen_w)
        bh = max(20.0, ((ymax - ymin) / 1000.0) * screen_h)

        lbl = description or clean_target
        self.highlight_region(bx, by, bw, bh, label=lbl, duration=duration)
        return (
            f"Élément '{clean_target}' localisé et encadré à ({bx:.0f}, {by:.0f}, {bw:.0f}x{bh:.0f}) "
            f"pendant {duration:.1f}s."
        )

    # ── Outil Gemini & Conversion Intelligente de Coordonnées ─────────────────

    @staticmethod
    def _box_within_screen(bx: float, by: float, bw: float, bh: float,
                           sx: float, sy: float, sw: float, sh: float) -> bool:
        """La zone recouvre-t-elle réellement une part visible de l'écran ?

        On tolère qu'un élément soit rogné par un bord : c'est fréquent et
        légitime. On refuse ce qui ne touche presque pas l'écran, signature
        d'une coordonnée inventée ou d'un repère mal converti.
        """
        if bw <= 0 or bh <= 0:
            return False
        overlap_w = max(0.0, min(bx + bw, sx + sw) - max(bx, sx))
        overlap_h = max(0.0, min(by + bh, sy + sh) - max(by, sy))
        return (overlap_w * overlap_h) >= 0.25 * (bw * bh)

    def point_on_screen(
        self,
        description: str,
        coordinates: Optional[Sequence[Union[int, float]]] = None,
        mode: Literal["auto", "highlight", "laser", "path", "gemini_box"] = "auto",
        duration: float = 3.0,
        normalized: bool = False,
        target: Optional[str] = None,
    ) -> str:
        """Point d'entrée principal pour les outils IA (Gemini Vision / MCP).
        Prend en charge soit une cible nommée/visuelle (recommandé), soit des coordonnées explicites.
        """
        target_name = (target or "").strip()
        if not target_name and coordinates is not None and len(coordinates) == 0:
            return "Erreur : aucune coordonnée fournie pour point_on_screen."

        if target_name or coordinates is None:
            candidate = target_name or description
            if candidate and candidate != "Élément ciblé":
                return self.point_on_target(candidate, description=description, duration=duration)
            if not coordinates:
                return "Erreur : aucune coordonnée ni cible fournie pour point_on_screen."

        # Résolution des dimensions de l'écran principal ou actif
        app = QApplication.instance()
        prim_screen = app.primaryScreen() if app else None
        screen_w = prim_screen.geometry().width() if prim_screen else 1920
        screen_h = prim_screen.geometry().height() if prim_screen else 1080
        screen_x = prim_screen.geometry().x() if prim_screen else 0
        screen_y = prim_screen.geometry().y() if prim_screen else 0

        coords = [float(c) for c in coordinates]

        # ── Cas 1 : Point Laser [x, y] ──────────────────────────────────────
        if (len(coords) == 2 and mode != "path") or mode == "laser":
            raw_x, raw_y = coords[0], coords[1]
            if 0.0 <= raw_x <= 1.0 and 0.0 <= raw_y <= 1.0 and any(isinstance(c, float) for c in coordinates[:2]):
                px = screen_x + raw_x * screen_w
                py = screen_y + raw_y * screen_h
            elif normalized:
                px = screen_x + (raw_x / 1000.0) * screen_w
                py = screen_y + (raw_y / 1000.0) * screen_h
            else:
                px, py = raw_x, raw_y

            dur = duration if duration > 0 else 2.0
            self.laser_point(px, py, duration=dur)
            return (
                f"Pointeur laser activé à ({px:.0f}, {py:.0f}) "
                f"pendant {dur:.1f}s pour '{description}'."
            )

        # ── Cas 2 : Trajectoire / Path ──────────────────────────────────────
        if mode == "path" or (len(coords) >= 6 and len(coords) % 2 == 0):
            points = []
            for i in range(0, len(coords), 2):
                rx, ry = coords[i], coords[i + 1]
                if 0.0 <= rx <= 1.0 and 0.0 <= ry <= 1.0 and any(isinstance(c, float) for c in coordinates[i:i+2]):
                    pt_x = screen_x + rx * screen_w
                    pt_y = screen_y + ry * screen_h
                elif normalized:
                    pt_x = screen_x + (rx / 1000.0) * screen_w
                    pt_y = screen_y + (ry / 1000.0) * screen_h
                else:
                    pt_x, pt_y = rx, ry
                points.append((pt_x, pt_y))

            dur = duration if duration > 0 else 3.5
            self.draw_path(points, duration=dur, label=description)
            return (
                f"Trajectoire visuelle tracée ({len(points)} points) "
                f"pendant {dur:.1f}s pour '{description}'."
            )

        # ── Cas 3 : Rectangle Cible / Highlight [x, y, w, h] ou Gemini [ymin, xmin, ymax, xmax]
        if len(coords) >= 4 or mode in ("highlight", "gemini_box"):
            c0, c1, c2, c3 = coords[0], coords[1], coords[2], coords[3]

            is_unit_norm = all(0.0 <= val <= 1.0 for val in (c0, c1, c2, c3)) and any(isinstance(c, float) for c in coordinates[:4])
            is_explicit_gemini = (mode == "gemini_box") or normalized or is_unit_norm

            if is_explicit_gemini:
                norm_base = 1.0 if is_unit_norm else 1000.0
                top = screen_y + (c0 / norm_base) * screen_h
                left = screen_x + (c1 / norm_base) * screen_w
                bottom = screen_y + (c2 / norm_base) * screen_h
                right = screen_x + (c3 / norm_base) * screen_w

                bx = left
                by = top
                bw = max(20.0, right - left)
                bh = max(20.0, bottom - top)
            else:
                # Coordonnées directes en pixels [x, y, w, h]
                bx, by = c0, c1
                bw = max(15.0, c2)
                bh = max(15.0, c3)
                # Une boîte qui tombe hors de l'écran n'est pas une position
                # imprécise, c'est une position inventée — souvent une boîte
                # Gemini 0-1000 passée sans mode='gemini_box'. Mieux vaut le
                # dire au modèle que de dessiner un cadre invisible ou à côté.
                if not self._box_within_screen(bx, by, bw, bh,
                                               screen_x, screen_y, screen_w, screen_h):
                    return (
                        f"Coordonnées refusées pour '{description}' : la zone "
                        f"({bx:.0f}, {by:.0f}, {bw:.0f}x{bh:.0f}) sort de l'écran "
                        f"({screen_w:.0f}x{screen_h:.0f}). Utilise le paramètre "
                        "'target' pour que l'élément soit localisé et vérifié, "
                        "ou mode='gemini_box' si ces valeurs sont normalisées 0-1000."
                    )

            dur = duration if duration > 0 else 3.0
            self.highlight_region(bx, by, bw, bh, label=description or "Ici", duration=dur)
            return (
                f"Zone mise en valeur à ({bx:.0f}, {by:.0f}, {bw:.0f}x{bh:.0f}) "
                f"pendant {dur:.1f}s pour '{description}'."
            )

        return f"Format de coordonnées non reconnu ({coords}) pour description='{description}'."


# ══════════════════════════════════════════════════════════════════════════════
# 5. SINGLETON & INSTANCIATION GLOBALE
# ══════════════════════════════════════════════════════════════════════════════

_global_pointer_overlay: Optional[VisualPointerOverlay] = None


def get_visual_pointer() -> VisualPointerOverlay:
    """Retourne l'instance unique du gestionnaire de pointeur visuel d'ANO-GPT."""
    global _global_pointer_overlay
    if _global_pointer_overlay is None:
        _global_pointer_overlay = VisualPointerOverlay()
    return _global_pointer_overlay


# ══════════════════════════════════════════════════════════════════════════════
# 6. DÉMONSTRATION INTERACTIVE AUTONOME
# ══════════════════════════════════════════════════════════════════════════════

def _run_demo() -> None:
    """Lance une démonstration interactive des trois primitives visuelles."""
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("ano-visual-pointer-demo")

    pointer = get_visual_pointer()
    screen = app.primaryScreen()
    geom = screen.geometry()
    w, h = geom.width(), geom.height()

    print("\n" + "═" * 70)
    print("  [DEMO] ANO-GPT Visual Pointer Overlay (Wayland / PyQt6)")
    print(f"  Écran détecté : {screen.name()} ({w}x{h})")
    print("═" * 70)

    # 1. Étape 1 : highlight_region
    print("  [1/3] Affichage d'un rectangle néon pulsant avec flèche...")
    pointer.highlight_region(
        w * 0.25,
        h * 0.25,
        w * 0.25,
        h * 0.15,
        label="Bouton Paramètres",
        duration=3.5,
    )

    # 2. Étape 2 : laser_point après 2.0s
    def step_laser():
        print("  [2/3] Affichage d'un point laser avec ondes de choc...")
        pointer.laser_point(w * 0.70, h * 0.40, duration=3.0)

    QTimer.singleShot(2000, step_laser)

    # 3. Étape 3 : draw_path après 4.0s
    def step_path():
        print("  [3/3] Affichage d'une trajectoire animée...")
        trajectory = [
            (w * 0.15, h * 0.75),
            (w * 0.35, h * 0.65),
            (w * 0.55, h * 0.80),
            (w * 0.80, h * 0.70),
        ]
        pointer.draw_path(trajectory, duration=4.0, label="Glisser ici")

    QTimer.singleShot(4000, step_path)

    # Arrêt de la démo après 9 secondes
    QTimer.singleShot(9000, app.quit)

    print("  Overlay actif. Vous pouvez cliquer librement à travers l'écran.")
    print("  La démo se terminera automatiquement dans 9 secondes.\n")

    sys.exit(app.exec())


if __name__ == "__main__":
    _run_demo()
