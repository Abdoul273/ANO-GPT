"""Render the native briefing and notification layout without starting services."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import QApplication, QLabel, QMainWindow, QScrollArea, QWidget
from ui.orb.arc_core import HudCanvas
from ui.panels.floating_panel import FloatingPanel
from ui.panels.log_widget import LogWidget
from ui.panels.rich_card_system import CardManager
from ui.styles.qss import get_global_style
from ui.window.chrome import ChromeMixin
from ui.window.overlay_layout import HudOverlayLayout


class Preview(ChromeMixin, QMainWindow):
    def __init__(self):
        super().__init__()
        central = QWidget()
        self.setCentralWidget(central)
        layout = HudOverlayLayout(central)
        self.orb = HudCanvas("", "ANO-GPT")
        layout.add_fill(self.orb)
        header = FloatingPanel()
        header.add_widget(QLabel("ANO-GPT  /  ESPACE PERSONNEL"))
        layout.add_role(header, "header")
        left = FloatingPanel("Conversation")
        log = LogWidget()
        log.setPlainText("ANO-GPT\nVotre espace, simplement.\n\nVoix · Contexte · Actions\n\nLes réponses et les informations utiles restent accessibles sans interrompre votre lecture.")
        left.add_widget(log)
        layout.add_role(left, "telemetry")
        command = FloatingPanel()
        command.add_widget(QLabel("Écrivez votre demande…                         ↵"))
        layout.add_role(command, "cmd")
        briefing = self._build_content_panel()
        self._content_ts_lbl.setText("APERÇU · DONNÉES DE DÉMONSTRATION")
        self._content_display.setPlainText(
            "Votre journée, en un regard\n\n"
            "01  LES PRIORITÉS\nPrenez le temps de lire ce qui compte. Le briefing reste à sa place, même lorsqu’une notification arrive.\n\n"
            "02  VOTRE ESPACE\nLes informations longues se parcourent à la molette, au pavé tactile ou au clavier.\n\n"
            "03  LA CONVERSATION\nUne saisie claire, des surfaces sobres et des accents lumineux qui guident le regard.\n\n"
            "04  POUR LA SUITE\nLes cartes possèdent leur propre défilement. Votre contenu reste accessible.\n\n" * 2)
        layout.add_role(briefing, "content")
        manager = CardManager()
        rail = QScrollArea()
        rail.setWidgetResizable(True)
        rail.setWidget(manager)
        rail.setFixedWidth(manager.width() + 12)
        rail.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        rail.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        rail.setStyleSheet("""
            QScrollArea { background: transparent; border: none; }
            QScrollBar:vertical, QScrollBar:horizontal { width: 0px; height: 0px; background: transparent; border: none; }
        """)
        rail.viewport().setStyleSheet("background: transparent;")
        layout.add_role(rail, "cards")
        manager.add_card("info", "Un espace plus lisible", "Les détails restent disponibles. **Faites défiler** pour parcourir une carte longue.")
        manager.add_card("task", "Tout à sa place", "Le briefing et les notifications partagent une colonne sans se recouvrir.")
        briefing.show()
        self.resize(1440, 960)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="/tmp/ano-gpt-interface.png")
    args = parser.parse_args()
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(get_global_style())
    window = Preview()
    window.show()
    def save():
        window.grab().save(args.output)
        print(args.output)
        app.quit()
    QTimer.singleShot(600, save)
    app.exec()


if __name__ == "__main__":
    main()
