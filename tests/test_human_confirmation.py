import threading

from core import human_confirmation


def setup_function():
    human_confirmation.clear()


def test_only_matching_interface_token_executes():
    shown = []
    ran = threading.Event()
    human_confirmation.bind(show=shown.append)
    result = human_confirmation.request("power", "Éteindre", "Test", lambda: ran.set())
    assert result.startswith("[CONFIRMATION_HUMAINE_EN_ATTENTE]")
    assert not human_confirmation.resolve("inventé-par-le-modèle", True)
    assert not ran.wait(0.05)
    assert human_confirmation.resolve(shown[0].token, True, source="test")
    assert ran.wait(1.0)


def test_rejection_never_runs_action():
    shown = []
    ran = threading.Event()
    human_confirmation.bind(show=shown.append)
    human_confirmation.request("wifi", "Wi-Fi", "Test", lambda: ran.set())
    assert human_confirmation.resolve(shown[0].token, False)
    assert not ran.wait(0.05)


def test_a_second_request_cannot_replace_the_visible_token():
    shown = []
    human_confirmation.bind(show=shown.append)
    human_confirmation.request("one", "Première", "", lambda: None)
    first = shown[0]
    human_confirmation.request("two", "Seconde", "", lambda: None)
    assert len(shown) == 1
    assert human_confirmation.current().token == first.token


def test_une_demande_identique_ne_cree_pas_une_seconde_carte():
    shown = []
    human_confirmation.bind(show=shown.append)
    first = human_confirmation.request("message:send", "SMS", "Maman\n\nSalut", lambda: None)
    second = human_confirmation.request("message:send", "SMS", "Maman\n\nSalut", lambda: None)
    assert first.startswith("[CONFIRMATION_HUMAINE_EN_ATTENTE]")
    assert second.startswith("[CONFIRMATION_HUMAINE_EN_ATTENTE]")
    assert len(shown) == 1
    assert human_confirmation.current().token == shown[0].token


def test_une_correction_du_canal_remplace_la_carte_visible():
    shown, hidden = [], []
    human_confirmation.bind(show=shown.append, hide=hidden.append)
    human_confirmation.request("message:send", "Envoyer via WhatsApp", "Maman", lambda: None)
    human_confirmation.request("message:send", "Envoyer via SMS", "Maman", lambda: None)
    assert len(shown) == 2
    assert hidden == [shown[0].token]
    assert human_confirmation.current().token == shown[1].token


def test_une_action_deja_executee_ne_repart_pas():
    shown = []
    runs = []
    human_confirmation.bind(show=shown.append)
    human_confirmation.request("message:send", "SMS", "Maman\n\nSalut",
                               lambda: runs.append(1))
    assert human_confirmation.resolve(shown[0].token, True)
    replay = human_confirmation.request("message:send", "SMS", "Maman\n\nSalut",
                                        lambda: runs.append(1))
    assert replay.startswith("[ACTION_DEJA_EXECUTEE]")
    assert len(shown) == 1
    for _ in range(50):
        if runs:
            break
        threading.Event().wait(0.01)
    assert len(runs) == 1


def test_callback_failure_is_not_announced_as_success():
    shown, notifications = [], []
    completed = threading.Event()
    def notify(text):
        notifications.append(text)
        if len(notifications) == 2:
            completed.set()
    human_confirmation.bind(show=shown.append, notify=notify)
    human_confirmation.request("sms", "SMS", "simulation", lambda: "Téléphone absent : SMS non envoyé.")
    assert human_confirmation.resolve(shown[0].token, True)
    assert completed.wait(1)
    assert notifications[-1] == "Résultat de l'action : Téléphone absent : SMS non envoyé."
    assert "Action terminée" not in notifications[-1]
    human_confirmation.bind(show=shown.append)


def test_callback_exception_notifies_user():
    shown = []
    failed = threading.Event()
    def fail(): raise TimeoutError("simulation")
    def notify(text):
        if "pas rendu de résultat confirmé" in text:
            failed.set()
    human_confirmation.bind(show=shown.append, notify=notify)
    human_confirmation.request("sms", "SMS", "simulation", fail)
    assert human_confirmation.resolve(shown[0].token, True)
    assert failed.wait(1)
    human_confirmation.bind(show=shown.append)
