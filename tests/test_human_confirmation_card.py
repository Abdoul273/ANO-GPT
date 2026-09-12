"""tests/test_human_confirmation_card.py — Tests TDD pour la carte de confirmation humaine.

Couvre la Tâche 1 du plan docs/plans/2026-09-05-confirmation-card-and-live-pointer.md :
1. Création de la carte de confirmation via CardManager.add_card('confirmation', ...)
   (Catégorie 'SÉCURITÉ', Theme.NEON_AMBER, pinned=True, auto_dismiss_s=0, boutons Confirmer/Annuler).
2. Clic sur 'Confirmer' : resolve(token, True), désactivation anti-double-clic, exécution du callback.
3. Clic sur 'Annuler' : resolve(token, False), current() redevient None, action non exécutée.
4. Fermeture directe via dismiss() ou clic sur [X] : résolution automatique avec resolve(token, False).
5. Notification vocale / feedback via human_confirmation.bind(notify=...) lors de la confirmation/annulation.
"""

from __future__ import annotations

import threading
import time
import pytest
from PyQt6.QtWidgets import QApplication, QPushButton

from core import human_confirmation
from ui.panels.rich_card_system import CardManager, GlassCard, Theme


@pytest.fixture(scope="module")
def qapp():
    """Initialise QApplication pour les tests Qt."""
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture(autouse=True)
def reset_human_confirmation():
    """Réinitialise l'état de confirmation humaine avant et après chaque test."""
    human_confirmation.clear()
    human_confirmation.bind(show=lambda p: None)
    yield
    human_confirmation.clear()
    human_confirmation.bind(show=lambda p: None)


# ══════════════════════════════════════════════════════════════════════════════
# 1. Création de la carte de confirmation et attributs spécifiques
# ══════════════════════════════════════════════════════════════════════════════

def test_confirmation_card_creation_and_attributes(qapp):
    """Vérifie la création d'une carte de confirmation via CardManager.add_card('confirmation', ...).

    Doit posséder :
    - Catégorie : 'SÉCURITÉ'
    - Accent : Theme.NEON_AMBER (#ffb300 ou #ffc857)
    - pinned : True (priorité absolue, ne s'efface pas)
    - auto_dismiss_s : 0.0 (reste affichée tant que l'humain n'a pas répondu)
    - 2 boutons d'action : 'Confirmer' et 'Annuler'
    """
    manager = CardManager()
    actions = [
        {"label": "Confirmer", "primary": True, "callback": lambda: None},
        {"label": "Annuler", "callback": lambda: None},
    ]
    card = manager.add_card(
        "confirmation",
        "Installation Sublime Text",
        "sudo apt install sublime-text",
        actions=actions,
    )

    # 1. Vérification des attributs de la carte
    assert card.category == "SÉCURITÉ"
    assert card.accent_color in (Theme.NEON_AMBER, "#ffb300")
    assert card.pinned is True
    assert card.auto_dismiss_s == 0.0

    # 2. Vérification de la présence des 2 boutons d'action
    action_buttons = [b for b in card.findChildren(QPushButton) if b is not card._close_btn]
    button_labels = [b.text() for b in action_buttons]

    confirm_btn = next((b for b in action_buttons if "confirmer" in b.text().lower()), None)
    cancel_btn = next((b for b in action_buttons if "annuler" in b.text().lower()), None)

    assert confirm_btn is not None, f"Bouton 'Confirmer' introuvable parmi {button_labels}"
    assert cancel_btn is not None, f"Bouton 'Annuler' introuvable parmi {button_labels}"
    assert len(action_buttons) == 2


# ══════════════════════════════════════════════════════════════════════════════
# 2. Clic sur le bouton 'Confirmer'
# ══════════════════════════════════════════════════════════════════════════════

def test_confirmation_card_confirm_click(qapp):
    """Vérifie que le clic sur 'Confirmer' :

    - Déclenche resolve(token, True).
    - Désactive le bouton pour empêcher le double-clic (anti-double-clic).
    - Exécute le callback associé de l'opération protégée.
    - Purge la confirmation en attente (current() == None).
    """
    action_executed = threading.Event()

    def do_action():
        action_executed.set()
        return "succès"

    manager = CardManager()
    shown_cards = []

    def show_handler(pending):
        card = manager.add_card(
            "confirmation",
            pending.title,
            pending.detail,
            actions=[
                {
                    "label": "Confirmer",
                    "primary": True,
                    "callback": lambda token=pending.token: human_confirmation.resolve(
                        token, True, source="HUD"
                    ),
                },
                {
                    "label": "Annuler",
                    "callback": lambda token=pending.token: human_confirmation.resolve(
                        token, False, source="HUD"
                    ),
                },
            ],
        )
        shown_cards.append(card)

    human_confirmation.bind(show=show_handler)
    human_confirmation.request(
        "install", "Installation Sublime Text", "sudo apt install sublime-text", do_action
    )
    assert len(shown_cards) == 1
    card = shown_cards[0]

    confirm_btn = next(
        (b for b in card.findChildren(QPushButton) if "confirmer" in b.text().lower()),
        None,
    )
    assert confirm_btn is not None

    # Clic sur le bouton 'Confirmer'
    confirm_btn.click()

    # 1. Le bouton passe à l'état désactivé pour empêcher le double-clic
    assert not confirm_btn.isEnabled(), "Le bouton 'Confirmer' doit être désactivé après le clic"

    # 2. L'action protégée est exécutée
    assert action_executed.wait(1.0), "Le callback de l'action confirmée n'a pas été exécuté"

    # 3. La demande de confirmation est résolue
    assert human_confirmation.current() is None


# ══════════════════════════════════════════════════════════════════════════════
# 3. Clic sur le bouton 'Annuler'
# ══════════════════════════════════════════════════════════════════════════════

def test_confirmation_card_cancel_click(qapp):
    """Vérifie que le clic sur 'Annuler' :

    - Déclenche resolve(token, False).
    - Résout la demande de confirmation et remet current() à None.
    - N'exécute JAMAIS le callback protégé.
    """
    action_executed = threading.Event()

    def do_action():
        action_executed.set()
        return "succès"

    manager = CardManager()
    shown_cards = []

    def show_handler(pending):
        card = manager.add_card(
            "confirmation",
            pending.title,
            pending.detail,
            actions=[
                {
                    "label": "Confirmer",
                    "primary": True,
                    "callback": lambda token=pending.token: human_confirmation.resolve(
                        token, True, source="HUD"
                    ),
                },
                {
                    "label": "Annuler",
                    "callback": lambda token=pending.token: human_confirmation.resolve(
                        token, False, source="HUD"
                    ),
                },
            ],
        )
        shown_cards.append(card)

    human_confirmation.bind(show=show_handler)
    human_confirmation.request(
        "install", "Installation Sublime Text", "sudo apt install sublime-text", do_action
    )
    assert len(shown_cards) == 1
    card = shown_cards[0]

    cancel_btn = next(
        (b for b in card.findChildren(QPushButton) if "annuler" in b.text().lower()),
        None,
    )
    assert cancel_btn is not None

    # Clic sur le bouton 'Annuler'
    cancel_btn.click()

    # 1. current() redevient None
    assert human_confirmation.current() is None

    # 2. Le callback protégé n'a jamais été exécuté
    assert not action_executed.wait(0.05), "Le callback protégé ne doit pas s'exécuter après Annuler"


# ══════════════════════════════════════════════════════════════════════════════
# 4. Fermeture directe de la carte via dismiss() ou clic sur [X]
# ══════════════════════════════════════════════════════════════════════════════

def test_confirmation_card_dismiss_via_x_button_resolves_false(qapp):
    """Vérifie que cliquer sur le bouton de fermeture [X] de la carte

    résout automatiquement resolve(token, False) pour purger _pending.
    """
    action_executed = threading.Event()

    def do_action():
        action_executed.set()
        return "succès"

    manager = CardManager()
    shown_cards = []

    def show_handler(pending):
        card = manager.add_card(
            "confirmation",
            pending.title,
            pending.detail,
            actions=[
                {
                    "label": "Confirmer",
                    "primary": True,
                    "callback": lambda token=pending.token: human_confirmation.resolve(
                        token, True, source="HUD"
                    ),
                },
                {
                    "label": "Annuler",
                    "callback": lambda token=pending.token: human_confirmation.resolve(
                        token, False, source="HUD"
                    ),
                },
            ],
        )
        shown_cards.append(card)

    human_confirmation.bind(show=show_handler)
    human_confirmation.request(
        "install", "Installation Sublime Text", "sudo apt install sublime-text", do_action
    )
    assert len(shown_cards) == 1
    card = shown_cards[0]

    # Clic sur le bouton [X] de fermeture sans confirmer
    card._close_btn.click()

    # Doit purger _pending et résoudre en rejet
    assert human_confirmation.current() is None, "La fermeture via [X] doit purger _pending"
    assert not action_executed.wait(0.05)


def test_confirmation_card_dismiss_call_resolves_false(qapp):
    """Vérifie qu'un appel direct à card.dismiss() résout automatiquement

    resolve(token, False) pour libérer le verrou.
    """
    action_executed = threading.Event()

    def do_action():
        action_executed.set()
        return "succès"

    manager = CardManager()
    shown_cards = []

    def show_handler(pending):
        card = manager.add_card(
            "confirmation",
            pending.title,
            pending.detail,
            actions=[
                {
                    "label": "Confirmer",
                    "primary": True,
                    "callback": lambda token=pending.token: human_confirmation.resolve(
                        token, True, source="HUD"
                    ),
                },
                {
                    "label": "Annuler",
                    "callback": lambda token=pending.token: human_confirmation.resolve(
                        token, False, source="HUD"
                    ),
                },
            ],
        )
        shown_cards.append(card)

    human_confirmation.bind(show=show_handler)
    human_confirmation.request(
        "install", "Installation Sublime Text", "sudo apt install sublime-text", do_action
    )
    assert len(shown_cards) == 1
    card = shown_cards[0]

    # Appel direct à dismiss()
    card.dismiss()

    # Doit purger _pending et résoudre en rejet
    assert human_confirmation.current() is None, "card.dismiss() doit purger _pending"
    assert not action_executed.wait(0.05)


# ══════════════════════════════════════════════════════════════════════════════
# 5. Système de notification vocale / feedback utilisateur dans bind(notify=...)
# ══════════════════════════════════════════════════════════════════════════════

def test_human_confirmation_voice_notification():
    """Vérifie l'émission de retours vocaux/notifications lors de la confirmation et de l'annulation."""
    notifications = []
    ran = threading.Event()

    # Enregistrement du listener de notification vocale
    human_confirmation.bind(
        show=lambda p: None,
        notify=notifications.append,
    )

    # 1. Cas Confirmation (resolve avec True)
    human_confirmation.request(
        "update", "Mise à jour système", "Détails maj", lambda: ran.set()
    )
    pending = human_confirmation.current()
    assert pending is not None
    token = pending.token

    assert human_confirmation.resolve(token, True, source="HUD")
    assert ran.wait(1.0)
    assert len(notifications) >= 1, "Une notification vocale doit être émise lors de la confirmation"
    confirm_msg = notifications[-1].lower()
    assert any(w in confirm_msg for w in ["confirm", "valid", "exécut", "effectu"]), (
        f"Message inattendu : {notifications[-1]}"
    )

    # 2. Cas Annulation (resolve avec False)
    notifications.clear()
    human_confirmation.request(
        "delete", "Suppression fichier", "rm -rf /tmp/test", lambda: None
    )
    pending_cancel = human_confirmation.current()
    assert pending_cancel is not None
    cancel_token = pending_cancel.token

    assert human_confirmation.resolve(cancel_token, False, source="HUD")
    assert len(notifications) >= 1, "Une notification vocale doit être émise lors de l'annulation"
    cancel_msg = notifications[-1].lower()
    assert any(w in cancel_msg for w in ["annul", "refus", "rejet"]), (
        f"Message inattendu : {notifications[-1]}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# 6. Résolution conversationnelle dans le chat / voix ("oui" / "non")
# ══════════════════════════════════════════════════════════════════════════════

def test_conversational_confirmation_in_on_text_command():
    """Vérifie que taper ou dire 'oui' ou 'non' valide ou annule directement

    la confirmation en attente sans nécessiter de redirection complexe.
    """
    from main import JarvisLive
    ran = threading.Event()

    class MockJarvis(JarvisLive):
        def __init__(self):
            pass
        def _observe_habit_reply(self, t): pass
        def _maybe_routine(self, t, s): return False
        def check_persona_voice_trigger(self, t): return None

    agent = MockJarvis()

    # 1. Cas "oui" -> validation automatique
    human_confirmation.request("install", "Install test", "détail", lambda: ran.set())
    assert human_confirmation.current() is not None

    agent._on_text_command("oui")
    assert ran.wait(1.0), "La confirmation vocale/chat 'oui' doit exécuter l'action"
    assert human_confirmation.current() is None

    # 2. Cas "non" -> annulation automatique
    action_ran2 = threading.Event()
    human_confirmation.request("danger", "Danger test", "détail", lambda: action_ran2.set())
    assert human_confirmation.current() is not None

    agent._on_text_command("non, annule")
    assert not action_ran2.wait(0.05)
    assert human_confirmation.current() is None

