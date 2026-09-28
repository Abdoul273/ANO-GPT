"""Une couleur choisie recolore aussi les cartes déjà visibles."""

from PyQt6.QtWidgets import QApplication, QLabel


def test_accent_rethemes_existing_rich_card(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    from ui.styles.theme import C, apply_ui_accent, current_palette, retheme_all_widgets
    from ui.panels.rich_card_system import GlassCard, Theme
    from ui.styles.qss import get_global_style
    from ui.styles.cyber import cyber_qss

    card = GlassCard(accent_color=C.PRI)
    label = QLabel("Accent", card)
    label.setStyleSheet(f"color: {C.PRI};")
    old = current_palette()
    try:
        assert apply_ui_accent("#ab34ef")
        retheme_all_widgets(old, current_palette())
        assert C.PRI == "#ab34ef"
        assert Theme.PRI == C.PRI
        assert card.accent_color == C.PRI
        assert "#ab34ef" in label.styleSheet()
        assert "rgba(171, 52, 239," in get_global_style()
        assert "rgba(171, 52, 239," in cyber_qss()
        new_card = GlassCard(accent_color="#ff2bd6")
        assert new_card.accent_color == C.PRI
        new_card.deleteLater()
    finally:
        previous = current_palette()
        apply_ui_accent("#00d4ff")
        retheme_all_widgets(previous, current_palette())
        card.deleteLater()
        app.processEvents()


def test_new_card_content_uses_selected_accent(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    from ui.styles.theme import C, apply_ui_accent, current_palette, retheme_all_widgets
    from ui.panels.rich_card_system import CardManager, GlassCard

    old = current_palette()
    manager = None
    try:
        assert apply_ui_accent("#ad39e8")
        retheme_all_widgets(old, current_palette())
        manager = CardManager()
        card = GlassCard(category="INFO", title="Carte")
        neon = QLabel("Décor", card)
        neon.setStyleSheet("color: rgba(0, 212, 255, 0.7); border: 1px solid #ff2bd6;")
        card.add_widget(neon)
        manager.add_card(card)
        assert card.accent_color == C.PRI
        assert "rgba(173, 57, 232," in neon.styleSheet()
        assert "#ad39e8" in neon.styleSheet()
    finally:
        if manager is not None:
            manager.deleteLater()
        previous = current_palette()
        apply_ui_accent("#00d4ff")
        retheme_all_widgets(previous, current_palette())
        app.processEvents()
