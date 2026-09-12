"""Langage visuel unique : ce que peint `GlassCard`, appliqué partout.

Les panneaux de réglages avaient chacun leur fond, leurs rayons et leurs
boutons. Ils partagent désormais la feuille de style et l'en-tête définis ici,
donc la même silhouette biseautée, le même liseré cyan → magenta et la même
croix de fermeture que les cartes du HUD.
"""

from __future__ import annotations

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QFont, QIcon, QPainter, QPixmap
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

from ui.styles.theme import C

# ── Icônes ───────────────────────────────────────────────────────────────────
_SVG = {
    "x": '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24"'
         ' fill="none" stroke="{color}" stroke-width="2.4" stroke-linecap="round"'
         ' stroke-linejoin="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>',
}


def cyber_pixmap(name: str, color_hex: str, size: int = 14) -> QPixmap:
    tpl = _SVG.get(name, _SVG["x"])
    renderer = QSvgRenderer(tpl.format(color=color_hex).encode("utf-8"))
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter)
    painter.end()
    return pm


# ── Feuille de style commune aux panneaux de réglages ────────────────────────
def cyber_qss() -> str:
    """QSS appliqué à la racine de chaque panneau ; les enfants en héritent.

    Le fond du panneau reste transparent : c'est `Hud.chassis` qui le peint,
    et deux fonds superposés annuleraient le biseau.
    """
    return f"""
    QWidget {{
        background: transparent;
        color: {C.TEXT};
        font-family: "Inter", "DejaVu Sans", sans-serif;
    }}
    QLabel {{ background: transparent; }}
    QScrollArea, QScrollArea > QWidget > QWidget {{
        background: transparent;
        border: none;
    }}

    /* Boutons : liseré néon, remplissage en dégradé cyan → magenta au survol */
    QPushButton {{
        background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
            stop:0 rgba(14, 23, 36, 0.92), stop:1 rgba(9, 13, 26, 0.94));
        border: 1px solid rgba(0, 212, 255, 0.30);
        border-radius: 4px;
        color: {C.TEXT};
        padding: 6px 14px;
        font-weight: 600;
    }}
    QPushButton:hover {{
        background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
            stop:0 rgba(0, 212, 255, 0.22), stop:1 rgba(255, 43, 214, 0.16));
        border-color: rgba(0, 212, 255, 0.75);
        color: {C.WHITE};
    }}
    QPushButton:pressed {{
        background: rgba(0, 212, 255, 0.10);
        border-color: {C.PRI};
    }}
    QPushButton:checked {{
        background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
            stop:0 rgba(0, 212, 255, 0.30), stop:1 rgba(255, 43, 214, 0.14));
        border-color: {C.PRI};
        color: {C.PRI};
    }}
    QPushButton:disabled {{
        color: {C.TEXT_DIM};
        background: rgba(8, 14, 22, 0.85);
        border-color: rgba(0, 212, 255, 0.10);
    }}

    /* Bouton d'accent : l'action principale de chaque panneau */
    QPushButton#CyberPrimary {{
        background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
            stop:0 rgba(0, 212, 255, 0.26), stop:1 rgba(143, 92, 255, 0.20));
        border: 1px solid {C.PRI};
        color: {C.WHITE};
    }}
    QPushButton#CyberPrimary:hover {{
        background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
            stop:0 rgba(0, 212, 255, 0.42), stop:1 rgba(255, 43, 214, 0.28));
    }}
    QPushButton#CyberDanger {{
        border-color: rgba(255, 51, 85, 0.55);
        color: {C.RED};
    }}
    QPushButton#CyberDanger:hover {{
        background: rgba(255, 51, 85, 0.20);
        border-color: {C.RED};
        color: {C.WHITE};
    }}

    /* Croix de fermeture, identique sur cartes et panneaux */
    QPushButton#CyberClose {{
        background: rgba(255, 255, 255, 0.04);
        border: 1px solid rgba(255, 255, 255, 0.10);
        border-radius: 4px;
        padding: 0px;
    }}
    QPushButton#CyberClose:hover {{
        background: rgba(255, 51, 85, 0.25);
        border: 1px solid {C.RED};
    }}

    /* Saisie */
    QLineEdit, QTextEdit, QPlainTextEdit {{
        background: rgba(1, 10, 18, 0.94);
        color: {C.WHITE};
        border: 1px solid rgba(0, 212, 255, 0.26);
        border-radius: 4px;
        padding: 5px 10px;
        selection-background-color: {C.PRI_DIM};
        selection-color: {C.DARK};
    }}
    QLineEdit:hover, QTextEdit:hover {{ border-color: rgba(0, 212, 255, 0.50); }}
    QLineEdit:focus, QTextEdit:focus {{
        border: 1px solid {C.PRI};
        border-bottom: 2px solid {C.PRI};
        background: rgba(4, 20, 32, 0.98);
    }}

    QComboBox {{
        background: rgba(1, 10, 18, 0.94);
        color: {C.WHITE};
        border: 1px solid rgba(0, 212, 255, 0.26);
        border-radius: 4px;
        padding: 5px 10px;
    }}
    QComboBox:hover {{ border-color: rgba(0, 212, 255, 0.60); }}
    QComboBox:focus {{ border: 1px solid {C.PRI}; }}
    QComboBox::drop-down {{ border: none; width: 20px; }}
    QComboBox QAbstractItemView {{
        background: #06111c;
        color: {C.TEXT};
        border: 1px solid rgba(0, 212, 255, 0.40);
        selection-background-color: rgba(0, 212, 255, 0.18);
        selection-color: {C.PRI};
        outline: none;
        padding: 2px;
    }}

    QCheckBox {{ color: {C.TEXT_MED}; background: transparent; spacing: 7px; }}
    QCheckBox::indicator {{
        width: 14px; height: 14px;
        border: 1px solid rgba(0, 212, 255, 0.45);
        border-radius: 3px;
        background: rgba(1, 10, 18, 0.9);
    }}
    QCheckBox::indicator:checked {{
        background: {C.PRI};
        border: 1px solid {C.PRI};
    }}

    QSlider::groove:horizontal {{
        height: 3px; background: rgba(0, 212, 255, 0.18); border-radius: 1px;
    }}
    QSlider::sub-page:horizontal {{ background: {C.PRI}; border-radius: 1px; }}
    QSlider::handle:horizontal {{
        background: {C.WHITE}; width: 10px; margin: -5px 0; border-radius: 5px;
    }}

    QProgressBar {{
        border: 1px solid rgba(0, 212, 255, 0.22);
        background: rgba(1, 10, 18, 0.9);
        border-radius: 2px;
        text-align: center;
        color: {C.TEXT_DIM};
    }}
    QProgressBar::chunk {{
        background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
            stop:0 {C.PRI}, stop:1 {C.NEON_PINK});
    }}

    QScrollBar:vertical {{
        background: transparent; width: 8px; border: none; margin: 2px 0;
    }}
    QScrollBar::handle:vertical {{
        background: rgba(0, 212, 255, 0.30); border-radius: 4px; min-height: 30px;
    }}
    QScrollBar::handle:vertical:hover {{ background: {C.PRI}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; border: none; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

    /* Encarts internes : une élévation, jamais un second cadre concurrent */
    QFrame#CyberSection {{
        background: rgba(8, 16, 26, 0.55);
        border: 1px solid rgba(0, 212, 255, 0.16);
        border-left: 2px solid rgba(0, 212, 255, 0.45);
        border-radius: 4px;
    }}
    QFrame#CyberHairline {{
        background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
            stop:0 {C.PRI}, stop:0.4 rgba(143, 92, 255, 0.45),
            stop:0.8 rgba(0, 212, 255, 0.25), stop:1 transparent);
        border: none;
    }}
    """


def apply_cyber_style(widget: QWidget) -> None:
    """Rend le panneau transparent et lui applique la feuille commune."""
    widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    widget.setStyleSheet(cyber_qss())


def cyber_hairline(parent: QWidget | None = None) -> QFrame:
    """Le séparateur en dégradé des cartes."""
    line = QFrame(parent)
    line.setObjectName("CyberHairline")
    line.setFixedHeight(1)
    return line


def cyber_section(parent: QWidget | None = None) -> QFrame:
    frame = QFrame(parent)
    frame.setObjectName("CyberSection")
    return frame


def cyber_close_button(parent: QWidget | None = None, size: int = 22) -> QPushButton:
    """La croix de fermeture : même dessin, même survol rouge que les cartes."""
    btn = QPushButton(parent)
    btn.setObjectName("CyberClose")
    btn.setFixedSize(size, size)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setIcon(QIcon(cyber_pixmap("x", C.TEXT_DIM, size - 10)))
    btn.setIconSize(QSize(size - 10, size - 10))
    btn.setToolTip("Fermer")
    return btn


def micro_label(text: str, color: str | None = None,
                parent: QWidget | None = None) -> QLabel:
    """Micro-intitulé : petites capitales très espacées, comme sur les cartes."""
    label = QLabel(text.upper(), parent)
    font = QFont("Inter", 7, QFont.Weight.Bold)
    font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.7)
    label.setFont(font)
    # En capitales espacées, un intitulé un peu long réclamait plus de largeur
    # que le panneau entier et poussait le contenu hors du cadre. Il passe donc
    # à la ligne au lieu d'imposer sa mesure.
    label.setWordWrap(True)
    label.setStyleSheet(f"color: {color or C.TEXT_DIM}; background: transparent;")
    return label


class CyberHeader(QWidget):
    """En-tête de panneau : catégorie, titre, croix — le pendant de `GlassCard`."""

    close_clicked = pyqtSignal()

    def __init__(self, title: str, category: str = "PARAMÈTRES",
                 accent: str | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        accent = accent or C.PRI
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        titles = QVBoxLayout()
        titles.setContentsMargins(0, 0, 0, 0)
        titles.setSpacing(1)
        self._category = micro_label(category, accent, self)
        titles.addWidget(self._category)
        self._title = QLabel(title, self)
        self._title.setFont(QFont("Inter", 11, QFont.Weight.Bold))
        self._title.setStyleSheet(f"color: {C.WHITE}; background: transparent;")
        titles.addWidget(self._title)
        row.addLayout(titles, stretch=1)

        self.close_btn = cyber_close_button(self)
        self.close_btn.clicked.connect(self.close_clicked.emit)
        row.addWidget(self.close_btn, alignment=Qt.AlignmentFlag.AlignTop)

        column.addLayout(row)
        column.addWidget(cyber_hairline(self))

    def set_title(self, text: str) -> None:
        self._title.setText(text)
