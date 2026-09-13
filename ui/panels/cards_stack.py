from __future__ import annotations

import math
import random
import time


from PyQt6.QtCore import (
    QEasingCurve, QPointF, QRectF, Qt,
    QTimer, pyqtSignal, QPropertyAnimation,
)
from PyQt6.QtGui import (
    QBrush, QColor, QFont, QPainter,
    QPen,
)
from PyQt6.QtWidgets import (
    QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QTextBrowser, QVBoxLayout, QWidget,
)

from ui.core.hud_button import HudButton
from ui.core.hud_paint import Hud
from ui.styles.theme import C, make_svg_icon, qcol

class _HexGlyph(QWidget):
    """Icône enfermée dans un hexagone cerclé d'un arc tournant.

    C'est la pastille des points de la grande carte, ramenée à la taille d'une
    icône : elle relie visuellement les cartes d'information aux lieux
    épinglés, qui appartiennent au même appareil.
    """

    _SIZE = 26

    def __init__(self, icon_name: str, color: str, parent=None):
        super().__init__(parent)
        self.setFixedSize(self._SIZE, self._SIZE)
        self._color = qcol(color)
        # Le QSS global donne un fond à tout QWidget : sans cette règle, ce
        # widget peint un rectangle opaque qui perfore le panneau sous-jacent.
        self.setStyleSheet("background: transparent;")
        self._pix = make_svg_icon(icon_name, color, 13).pixmap(13, 13)
        self._angle = random.random() * 360.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._spin)
        self._timer.start(90)

    def _spin(self):
        if not self.isVisible():
            return
        self._angle = (self._angle + 3.6) % 360.0
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        center = QPointF(self._SIZE / 2, self._SIZE / 2)

        hexagon = Hud.hex_path(center, 10.0)
        p.setPen(QPen(QColor(self._color.red(), self._color.green(),
                             self._color.blue(), 110), 1.1))
        p.setBrush(QBrush(QColor(self._color.red(), self._color.green(),
                                 self._color.blue(), 28)))
        p.drawPath(hexagon)

        # Arc tournant : le mouvement le plus discret qui dise « en service ».
        p.save()
        p.translate(center)
        p.rotate(self._angle)
        p.translate(-center)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(self._color.red(), self._color.green(),
                             self._color.blue(), 190), 1.4))
        span = QRectF(center.x() - 12, center.y() - 12, 24, 24)
        p.drawArc(span, 0, 100 * 16)
        p.restore()

        p.drawPixmap(QPointF(center.x() - 6.5, center.y() - 6.5), self._pix)
        p.end()


class RichCardWidget(QFrame):
    """Carte de notification flottante riche (style JARVIS HUD).
    Types : message | result | task | confirmation | info
    Rendu : Markdown / HTML avec icône Lucide vectorielle, timestamp,
    bouton fermer et boutons d'actions configurables.
    """
    closed = pyqtSignal()
    action_triggered = pyqtSignal(dict)

    CARD_ICONS = {
        "message": ("mail", C.PRI),
        "result": ("search", C.PRI),
        "task": ("activity", C.TEXT_MED),
        "confirmation": ("help-circle", C.WHITE),
        "info": ("info", C.TEXT_MED),
        "error": ("alert-triangle", C.RED),
    }

    # Étiquette de catégorie affichée en micro-capitales au-dessus du titre.
    CARD_TAGS = {
        "message": "MESSAGE",
        "result": "RÉSULTAT",
        "task": "TÂCHE",
        "confirmation": "CONFIRMATION",
        "info": "SYSTÈME",
        "error": "ERREUR",
    }

    def __init__(self, card_type: str, title: str, body: str, actions: list[dict] = None, parent=None):
        super().__init__(parent)
        self.card_type = card_type.lower()
        self.card_title = title
        self.setObjectName("RichCard")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setFixedWidth(330)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        icon_name, icon_col = self.CARD_ICONS.get(self.card_type, ("info", C.PRI))
        self._accent = qcol(icon_col)
        self._scan = random.random() * Hud.SCAN_PERIOD
        self._pulse = random.random() * math.tau
        # Décalage d'entrée : la carte se pose depuis la droite. Une pile qui
        # apparaît d'un bloc ne se lit pas ; une pile qui se pose se lit.
        self._slide = 34.0
        self._closing = False

        self._opacity = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._opacity)
        self._opacity.setOpacity(0.0)

        self._anim = QPropertyAnimation(self._opacity, b"opacity")
        self._anim.setDuration(240)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._fx = QTimer(self)
        self._fx.timeout.connect(self._tick_fx)
        # 90 ms pendant l'entrée glissée, puis on relâche : voir _tick_fx.
        self._fx.start(90)

        # Le fond est peint, pas mis en feuille de style : Qt ne sait pas
        # découper un widget suivant un polygone.
        self.setStyleSheet("QFrame { background: transparent; border: none; }")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 11, 13, 12)
        layout.setSpacing(7)

        hdr = QHBoxLayout()
        hdr.setContentsMargins(0, 0, 0, 0)
        hdr.setSpacing(8)

        self._glyph = _HexGlyph(icon_name, icon_col)
        hdr.addWidget(self._glyph, alignment=Qt.AlignmentFlag.AlignTop)

        title_col = QVBoxLayout()
        title_col.setContentsMargins(0, 0, 0, 0)
        title_col.setSpacing(1)

        tag_lbl = QLabel(self.CARD_TAGS.get(self.card_type, "SYSTÈME"))
        tag_lbl.setFont(Hud.micro_font(6, 2.0))
        tag_lbl.setStyleSheet(
            "color: rgba(0, 212, 255, 0.55); background: transparent; border: none;")
        title_col.addWidget(tag_lbl)

        title_lbl = QLabel(title.upper()[:44])
        title_lbl.setFont(Hud.micro_font(8, 0.9))
        title_lbl.setStyleSheet(
            f"color: {icon_col}; background: transparent; border: none;")
        title_col.addWidget(title_lbl)
        hdr.addLayout(title_col, stretch=1)

        ts_lbl = QLabel(time.strftime("%H:%M"))
        ts_lbl.setFont(QFont("Inter", 7, QFont.Weight.Bold))
        ts_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent; border: none;")
        hdr.addWidget(ts_lbl, alignment=Qt.AlignmentFlag.AlignTop)

        close_btn = HudButton(icon="x", accent=C.TEXT_DIM, hover_accent=C.RED, size=11)
        close_btn.setFixedSize(20, 18)
        close_btn.clicked.connect(self.close_card)
        hdr.addWidget(close_btn, alignment=Qt.AlignmentFlag.AlignTop)

        layout.addLayout(hdr)

        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet(
            "QFrame { border: none; background: qlineargradient(x1:0, y1:0, x2:1, y2:0,"
            f" stop:0 {icon_col}, stop:0.5 rgba(255, 43, 214, 0.35), stop:1 transparent); }}")
        layout.addWidget(sep)

        self.body_view = QTextBrowser()
        from core.browser_policy import open_chrome
        self.body_view.setOpenExternalLinks(False)
        self.body_view.setOpenLinks(False)
        self.body_view.anchorClicked.connect(lambda url: open_chrome(url.toString()))
        self.body_view.setReadOnly(True)
        self.body_view.setFont(QFont("Inter", 9))
        self.body_view.setMarkdown(body)
        self.body_view.setStyleSheet(f"""
            QTextBrowser {{
                background: transparent; color: {C.TEXT};
                border: none; padding: 0;
            }}
            QScrollBar:vertical, QScrollBar:horizontal {{
                width: 0px; height: 0px; background: transparent; border: none;
            }}
        """)
        layout.addWidget(self.body_view)

        if actions:
            act_lay = QHBoxLayout()
            act_lay.setContentsMargins(0, 4, 0, 0)
            act_lay.setSpacing(6)
            act_lay.addStretch()

            for act in actions:
                btn = HudButton(
                    act.get("label", "Action"),
                    primary=act.get("primary", False),
                    accent=icon_col,
                )
                btn.setFixedHeight(25)
                btn.setMinimumWidth(84)
                btn.clicked.connect(lambda _, a=act: self._on_action_clicked(a))
                act_lay.addWidget(btn)

            layout.addLayout(act_lay)

        self._fit_body()
        self.fade_in()

    _BODY_MAX_H = 150

    def _fit_body(self) -> None:
        """Cale la hauteur du corps sur son texte.

        Sans cela, la vue s'étirait jusqu'à sa hauteur maximale : une carte de
        deux lignes occupait autant qu'un rapport complet, et la pile perdait
        toute lisibilité.
        """
        document = self.body_view.document()
        document.setDocumentMargin(0)
        document.setTextWidth(self.width() - 30)
        height = int(math.ceil(document.size().height())) + 2
        capped = max(18, min(height, self._BODY_MAX_H))
        self.body_view.setFixedHeight(capped)
        # Un rapport plus haut que le plafond était simplement coupé : la fin
        # devenait inaccessible. On rend la barre dès que le texte déborde,
        # sans la faire apparaître sur les cartes courtes.
        self.body_view.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded if height > capped
            else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.body_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def set_body(self, body: str) -> None:
        self.body_view.setMarkdown(body)
        self._fit_body()

    def _tick_fx(self):
        if not self.isVisible() and self._slide <= 0.2:
            return
        # À la fermeture, le balayage accélère et la carte est aspirée vers la
        # droite pendant que son signal se dissout. Le mouvement reste piloté
        # par le même timer léger que l'entrée, sans animation permanente.
        self._scan = (self._scan + (4.8 if self._closing else 1.3)) % Hud.SCAN_PERIOD
        self._pulse = (self._pulse + (0.32 if self._closing else 0.07)) % math.tau
        if self._closing:
            self._slide = min(float(self.width() - 8), self._slide * 1.18 + 12.0)
        elif self._slide > 0.2:
            # Glissement amorti : la carte arrive vite puis se pose.
            self._slide *= 0.78
        elif self._slide:
            self._slide = 0.0
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        if W < 8 or H < 8:
            p.end()
            return
        rect = QRectF(2 + self._slide, 2, W - 4 - self._slide, H - 4)
        Hud.chassis(p, rect, accent=self._accent, scan=self._scan, pulse=self._pulse)
        Hud.tick(p, rect, 46)
        p.end()
        super().paintEvent(event)

    def _on_action_clicked(self, act: dict):
        self.action_triggered.emit(act)
        cb = act.get("callback")
        if callable(cb):
            cb()
        self.close_card()

    def fade_in(self):
        self._closing = False
        self.show()
        self._anim.stop()
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.start()

    def close_card(self):
        if self._closing:
            return
        self._closing = True
        if not self._fx.isActive():
            self._fx.start(24)
        else:
            self._fx.setInterval(24)
        self._anim.stop()
        self._anim.setDuration(280)
        self._anim.setEasingCurve(QEasingCurve.Type.InCubic)
        self._anim.setStartValue(self._opacity.opacity())
        self._anim.setEndValue(0.0)
        try:
            self._anim.finished.disconnect()
        except Exception:
            pass
        self._anim.finished.connect(self._on_closed)
        self._anim.start()

    def _on_closed(self):
        self._fx.stop()
        self.hide()
        self.closed.emit()
        self.deleteLater()

class RightCardStack(QWidget):
    """Pile de cartes de notification flottante à droite de l'écran.
    Gère jusqu'à 4 cartes simultanées, empilées verticalement.
    """
    MAX_CARDS = 4

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(340)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(8)
        self._layout.addStretch()

        self._cards: list[RichCardWidget] = []

    def add_card(self, card_type: str, title: str, body: str, actions: list[dict] = None) -> RichCardWidget:
        if len(self._cards) >= self.MAX_CARDS:
            oldest = self._cards.pop(0)
            oldest.close_card()

        card = RichCardWidget(card_type, title, body, actions, parent=self)
        card.closed.connect(lambda: self._remove_card(card))
        self._layout.insertWidget(self._layout.count() - 1, card)
        self._cards.append(card)
        self.adjustSize()
        return card

    def _remove_card(self, card: RichCardWidget):
        if card in self._cards:
            self._cards.remove(card)
        self.adjustSize()

    def dismiss_cards(self, card_type: str = "", title: str = "") -> int:
        """Ferme les cartes correspondant à une action qui vient de finir.

        La copie protège l'itération : chaque animation retirera ensuite sa
        carte de ``_cards`` via le signal ``closed``.
        """
        wanted_type = card_type.strip().lower()
        wanted_title = title.strip().casefold()
        matches = [
            card for card in tuple(self._cards)
            if (not wanted_type or card.card_type == wanted_type)
            and (not wanted_title or card.card_title.strip().casefold() == wanted_title)
        ]
        for card in matches:
            card.close_card()
        return len(matches)

    def update_card(self, card_type: str, title: str, body: str) -> bool:
        for card in self._cards:
            if card.card_type == card_type.lower() and card.card_title == title:
                card.set_body(body)
                self.adjustSize()
                return True
        return False
