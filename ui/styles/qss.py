from __future__ import annotations

from ui.styles.theme import C

def get_global_style() -> str:
    """QSS global.

    Parti pris : la hiérarchie vient des surfaces (élévations) et de l'espace,
    pas des bordures. On ne garde qu'un liseré très fin, et l'accent ne sert
    qu'à ce qui est actif ou survolé — c'est ce qui rend l'ensemble vif sans
    être bruyant.
    """
    return f"""
    /* --- Global --- */
    QWidget {{
        background: {C.BG};
        color: {C.TEXT};
        font-family: "Inter", "DejaVu Sans", sans-serif;
        font-size: 10pt;
    }}
    QMainWindow {{
        background: {C.BG};
    }}
    QToolTip {{
        background: {C.ELEV2};
        color: {C.WHITE};
        border: 1px solid {C.HAIRLINE_S};
        border-radius: 2px;
        padding: 5px 9px;
    }}
    /* --- Boutons : angles coupés en biais, comme les panneaux peints ---
       Qt ne sait pas découper un widget en polygone par feuille de style : on
       en approche la silhouette avec un rayon quasi nul et un liseré net.
       Les boutons du HUD (HudButton) sont, eux, réellement biseautés. --- */
    QPushButton {{
        background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
            stop:0 rgba(2, 24, 37, 0.96), stop:1 rgba(11, 8, 26, 0.96));
        border: 1px solid rgba(0, 212, 255, 0.30);
        border-radius: 4px;
        color: {C.TEXT};
        padding: 6px 14px;
        font-weight: 600;
    }}
    QPushButton:hover {{
        background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
            stop:0 rgba(0, 212, 255, 0.26), stop:1 rgba(255, 43, 214, 0.18));
        border-color: rgba(0, 212, 255, 0.62);
        color: {C.WHITE};
    }}
    QPushButton:pressed {{
        background: {C.PRI_GHO};
        border-color: {C.PRI_DIM};
    }}
    QPushButton:checked {{
        background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
            stop:0 rgba(0, 212, 255, 0.30), stop:1 rgba(255, 43, 214, 0.14));
        border-color: {C.PRI};
        color: {C.PRI};
    }}
    QPushButton:disabled {{
        color: {C.TEXT_DIM};
        background: {C.PANEL};
        border-color: transparent;
    }}
    /* --- Champs de saisie : soulignés à l'accent quand ils ont le focus --- */
    QLineEdit, QTextEdit {{
        background: rgba(1, 13, 22, 0.92);
        color: {C.WHITE};
        border: 1px solid rgba(0, 212, 255, 0.26);
        border-radius: 4px;
        padding: 8px 12px;
        selection-background-color: {C.PRI_DIM};
        selection-color: {C.DARK};
    }}
    QLineEdit:hover, QTextEdit:hover {{
        border-color: {C.HAIRLINE_S};
    }}
    QLineEdit:focus, QTextEdit:focus {{
        border: 1px solid {C.PRI};
        border-bottom: 2px solid {C.PRI};
        background: rgba(4, 24, 37, 0.98);
    }}
    /* --- Listes déroulantes, accordées aux champs --- */
    QComboBox {{
        background: rgba(1, 13, 22, 0.92);
        color: {C.WHITE};
        border: 1px solid rgba(0, 212, 255, 0.26);
        border-radius: 4px;
        padding: 5px 10px;
    }}
    QComboBox:hover {{ border-color: rgba(0, 212, 255, 0.55); }}
    QComboBox::drop-down {{ border: none; width: 18px; }}
    QComboBox QAbstractItemView {{
        background: {C.ELEV1};
        color: {C.TEXT};
        border: 1px solid rgba(0, 212, 255, 0.35);
        selection-background-color: {C.PRI_GHO};
        selection-color: {C.PRI};
        outline: none;
    }}
    /* --- Ascenseurs : fins, sans piste, visibles au survol --- */
    QScrollBar:vertical {{
        background: transparent;
        width: 10px;
        border: none;
        margin: 2px 0;
    }}
    QScrollBar::handle:vertical {{
        background: {C.TEXT_DIM};
        border-radius: 4px;
        min-height: 32px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: {C.PRI_DIM};
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0;
        border: none;
    }}
    QScrollBar::add-page, QScrollBar::sub-page {{
        background: transparent;
    }}
    QScrollBar:horizontal {{
        background: transparent;
        height: 8px;
        border: none;
        margin: 0 2px;
    }}
    QScrollBar::handle:horizontal {{
        background: {C.BORDER};
        border-radius: 2px;
        min-width: 28px;
    }}
    QScrollBar::handle:horizontal:hover {{
        background: {C.PRI_DIM};
    }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
        width: 0;
        border: none;
    }}
    /* --- ProgressBar --- */
    QProgressBar {{
        border: none;
        background: {C.BAR_BG};
        height: 4px;
        border-radius: 1px;
    }}
    QProgressBar::chunk {{
        background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
            stop:0 {C.PRI}, stop:1 {C.NEON_PINK});
        border-radius: 1px;
    }}
    /* --- divers --- */
    QLabel {{
        background: transparent;
    }}
    QFrame {{
        background: transparent;
    }}
    QSplitter::handle {{
        background: transparent;
        height: 9px;
    }}
    QSplitter::handle:hover {{
        background: {C.PRI_GHO};
    }}
    """
