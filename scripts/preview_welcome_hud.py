#!/usr/bin/env python3
"""Script de prévisualisation directe de l'écran de bienvenue HUD Hacker avec sound design.

Usage:
    python3 scripts/preview_welcome_hud.py
"""

import sys
from pathlib import Path

# Ajouter le répertoire racine au PYTHONPATH
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from PyQt6.QtWidgets import QApplication, QVBoxLayout, QWidget

from ui.dialogs.welcome_hud import WelcomeHudOverlay
from ui.paths import _read_full_config
from ui.styles.qss import get_global_style
from ui.styles.theme import C, load_custom_font


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("ano-gpt-welcome-preview")
    app.setStyle("Fusion")
    app.setFont(load_custom_font())
    app.setStyleSheet(get_global_style())

    cfg = _read_full_config()
    assistant_name = cfg.get("assistant_name", "ANO-GPT") or "ANO-GPT"

    win = QWidget()
    win.setWindowTitle(f"{assistant_name.upper()} — NEURAL HUD WELCOME INTERFACE")
    win.resize(1280, 800)
    win.setStyleSheet(f"background: {C.BG};")

    layout = QVBoxLayout(win)
    layout.setContentsMargins(0, 0, 0, 0)

    welcome = WelcomeHudOverlay(win, assistant_name=assistant_name)
    layout.addWidget(welcome)

    welcome.engaged.connect(lambda: print("◈ [ACCÈS ACCORDÉ] Le noyau neural a été initialisé avec succès !"))
    welcome.dismissed.connect(win.close)

    win.show()
    print("◈ Lancement de l'écran de bienvenue HUD Hacker (Appuyez sur Entrée / Espace / Échap ou cliquez pour tester)...")
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
