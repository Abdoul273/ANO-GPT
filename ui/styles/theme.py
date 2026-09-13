from __future__ import annotations



from PyQt6.QtCore import (
    Qt,
)
from PyQt6.QtGui import (
    QColor, QFont, QFontDatabase, QIcon, QPainter,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QApplication, QFrame, QLabel,
)

from ui.paths import BASE_DIR, CONFIG_DIR, API_FILE  # noqa: F401

class C:
    # Fondations — hiérarchie par élévation, pas par bordure
    BG        = "#01050a"          # fond HUD légèrement plus profond
    PANEL     = "#07101a"
    PANEL2    = "#0a1621"
    SURFACE   = "#060e16"          # nouvelles surfaces
    SURFACE2  = "#0a141e"
    CHROME    = "#010409"          # barres (header / footer / panneaux latéraux)
    ELEV1     = "#0a1520"          # carte posée sur le chrome
    ELEV2     = "#102131"          # carte survolée / active

    # Bordures — n'en garder qu'une, très fine
    BORDER    = "#0d3347"
    BORDER_B  = "#1a5c7a"
    BORDER_A  = "#0f4060"
    BORDER_GLASS = "rgba(0, 200, 255, 0.15)"
    HAIRLINE  = "rgba(0, 190, 255, 0.10)"
    HAIRLINE_S = "rgba(0, 190, 255, 0.18)"   # variante marquée

    # Accents
    PRI       = "#00d4ff"
    PRI_DIM   = "#007a99"
    PRI_GHO   = "#001f2e"
    PRI_GLOW  = "rgba(0, 212, 255, 0.25)"
    ACC       = "#ff6b00"
    ACC2      = "#ffcc00"
    GREEN     = "#00ff88"
    GREEN_D   = "#00aa55"
    RED       = "#ff3355"
    MUTED_C   = "#ff3366"

    # Rôles sémantiques fixes (ne changent pas avec l'accent dynamique)
    SUCCESS   = "#00e699"   # Vert succès / validation
    WARNING   = "#ffb300"   # Ambre alerte / avertissement
    DANGER    = "#ff3366"   # Rouge danger / erreur / interruption
    INFO      = "#00d4ff"   # Bleu information / neutre

    # Hiérarchie typographique (4 tailles nominales Inter)
    FONT_XS   = 8    # Micro-tags, horodatage
    FONT_SM   = 9    # Tooltips, sous-titres, étiquettes secondaires
    FONT_BASE = 10   # Corps de texte principal, champs de saisie, boutons
    FONT_LG   = 13   # Titres de panneaux, en-têtes

    # Textes
    TEXT      = "#d2e1eb"
    TEXT_DIM  = "#7893a6"
    TEXT_MED  = "#a1b8c9"
    WHITE     = "#eef7fc"
    DARK      = "#000d14"
    BAR_BG    = "#011520"

    NEON_PINK = "#ff2bd6"
    NEON_VIO  = "#8f5cff"
    NEON_AMBER = "#ffc857"
    GRID      = "rgba(0, 212, 255, 0.07)"
    SCANLINE  = "rgba(143, 252, 255, 0.045)"

    # Nouveaux pour glassmorphism
    GLASS_BG      = "rgba(10, 20, 30, 0.65)"
    GLASS_SHADOW  = "0 8px 32px rgba(0, 0, 0, 0.6)"
    ACCENT_GRAD   = "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #00d4ff, stop:1 #007aff)"


# ── Gestion des couleurs et thème ─────────────────────────────────────────────
_HUE_LINKED = (
    "BG", "PANEL", "PANEL2", "SURFACE", "SURFACE2", "BORDER", "BORDER_B", "BORDER_A",
    "PRI", "PRI_DIM", "PRI_GHO", "TEXT", "TEXT_DIM", "TEXT_MED",
    "WHITE", "DARK", "BAR_BG", "BORDER_GLASS",
    "CHROME", "ELEV1", "ELEV2",
)
_PALETTE_DEFAULTS: dict[str, str] = {k: getattr(C, k) for k in _HUE_LINKED}

DEFAULT_UI_COLOR = _PALETTE_DEFAULTS["PRI"]


def apply_ui_accent(accent_hex: str) -> bool:
    import colorsys
    accent_hex = (accent_hex or "").strip().lower()
    if not (accent_hex.startswith("#") and len(accent_hex) == 7):
        return False
    try:
        int(accent_hex[1:], 16)
    except ValueError:
        return False

    def _hsv(h: str) -> tuple[float, float, float]:
        r = int(h[1:3], 16) / 255
        g = int(h[3:5], 16) / 255
        b = int(h[5:7], 16) / 255
        return colorsys.rgb_to_hsv(r, g, b)

    base_h            = _hsv(_PALETTE_DEFAULTS["PRI"])[0]
    acc_h, acc_s, _av = _hsv(accent_hex)
    dh   = acc_h - base_h
    grey = acc_s < 0.08

    for key, hex0 in _PALETTE_DEFAULTS.items():
        # La palette contient aussi des couleurs en rgba() : elles n'ont pas
        # de forme hexadécimale et feraient échouer la conversion.
        if not (hex0.startswith("#") and len(hex0) == 7):
            continue
        h, s, v = _hsv(hex0)
        if grey:
            s *= 0.15
        r, g, b = colorsys.hsv_to_rgb((h + dh) % 1.0, s, v)
        setattr(C, key, "#{:02x}{:02x}{:02x}".format(
            int(r * 255 + 0.5), int(g * 255 + 0.5), int(b * 255 + 0.5)))
    return True


def current_palette() -> dict[str, str]:
    return {k: getattr(C, k) for k in _HUE_LINKED}


def retheme_all_widgets(old: dict[str, str], new: dict[str, str]) -> None:
    mapping = {old[k].lower(): new[k].lower()
               for k in old if old[k].lower() != new.get(k, old[k]).lower()}
    if not mapping:
        return
    app = QApplication.instance()
    if app is None:
        return
    for w in app.allWidgets():
        try:
            ss = w.styleSheet()
            if ss:
                s2 = ss
                for o, n in mapping.items():
                    if o in s2:
                        s2 = s2.replace(o, n)
                if s2 != ss:
                    w.setStyleSheet(s2)
            w.update()
        except Exception:
            pass


def qcol(h: str, a: int = 255) -> QColor:
    c = QColor(h); c.setAlpha(a); return c


# ── Police personnalisée ──────────────────────────────────────────────────────
def load_custom_font() -> QFont:
    """Charge Inter (ou JetBrains Mono) depuis assets/fonts/ si présent, sinon fallback."""
    font_path = BASE_DIR / "assets" / "fonts" / "Inter-VariableFont_opsz,wght.ttf"
    if font_path.exists():
        font_id = QFontDatabase.addApplicationFont(str(font_path))
        if font_id != -1:
            family = QFontDatabase.applicationFontFamilies(font_id)[0]
            return QFont(family, 9)
    # fallback
    return QFont("Courier New", 9)


# ── Banques d'icônes vectorielles (Lucide SVG) ───────────────────────────────
SVG_LUCIDE = {
    "mic": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" x2="12" y1="19" y2="22"/></svg>""",
    "mic-off": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="2" x2="22" y1="2" y2="22"/><path d="M18.89 13.23A7.12 7.12 0 0 0 19 12v-2"/><path d="M5 10v2a7 7 0 0 0 11.57 5.34"/><path d="M15 9.34V5a3 3 0 0 0-5.68-1.33"/><path d="M9 9v3a3 3 0 0 0 3.82 2.85"/><line x1="12" x2="12" y1="19" y2="22"/></svg>""",
    "paperclip": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m21.44 11.05-9.19 9.19a6 6 0 0 1-8.49-8.49l8.57-8.57A4 4 0 1 1 18 8.84l-8.59 8.57a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>""",
    "send": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/></svg>""",
    "square": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="18" height="18" x="3" y="3" rx="3"/></svg>""",
    "x": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>""",
    "chevron-left": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m15 18-6-6 6-6"/></svg>""",
    "chevron-right": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m9 18 6-6-6-6"/></svg>""",
    "settings": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.38a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/></svg>""",
    "upload": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" x2="12" y1="3" y2="15"/></svg>""",
    "mail": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="20" height="16" x="2" y="4" rx="2"/><path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7"/></svg>""",
    "alert-triangle": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg>""",
    "search": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>""",
    "activity": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>""",
    "help-circle": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" x2="12.01" y1="17" y2="17"/></svg>""",
    "info": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/></svg>""",
    "music": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>""",
    "play": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="6 3 20 12 6 21 6 3"/></svg>""",
    "pause": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>""",
    "skip-back": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="19 20 9 12 19 4 19 20"/><line x1="5" x2="5" y1="19" y2="5"/></svg>""",
    "skip-forward": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="5 4 15 12 5 20 5 4"/><line x1="19" x2="19" y1="5" y2="19"/></svg>""",
    "volume-2": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/></svg>""",
    "shuffle": """<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 18h1.4c1.3 0 2.5-.6 3.3-1.7l6.1-8.6c.7-1.1 2-1.7 3.3-1.7H22"/><path d="m18 2 4 4-4 4"/><path d="M2 6h1.4c1.3 0 2.5.6 3.3 1.7l6.1 8.6c.7 1.1 2 1.7 3.3 1.7H22"/><path d="m18 14 4 4-4 4"/></svg>""",
}

def make_svg_icon(name: str, color_hex: str, size: int = 24) -> QIcon:
    tpl = SVG_LUCIDE.get(name)
    if not tpl:
        return QIcon()
    try:
        from PyQt6.QtSvg import QSvgRenderer
        xml = tpl.format(color=color_hex)
        renderer = QSvgRenderer(xml.encode("utf-8"))
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        renderer.render(p)
        p.end()
        return QIcon(pm)
    except Exception:
        return QIcon()


# ── Petits composants de mise en page réutilisables ──────────────────────────
def section_label(text: str, color: str | None = None) -> QLabel:
    """Intitulé de section : petites capitales espacées, discrètes."""
    l = QLabel(text.upper())
    f = QFont("Inter", 7, QFont.Weight.Bold)
    f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.6)
    l.setFont(f)
    l.setStyleSheet(f"color: {color or C.TEXT_DIM}; background: transparent;")
    return l


def hairline(margin: str = "0") -> QFrame:
    """Séparateur d'un pixel, à peine visible."""
    f = QFrame()
    f.setFixedHeight(1)
    f.setStyleSheet(f"background: {C.HAIRLINE}; border: none; margin: {margin};")
    return f
