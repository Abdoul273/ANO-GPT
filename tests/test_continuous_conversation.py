"""Tests pour le gestionnaire de conversation continue (core/continuous_conversation.py)."""

import asyncio
import pytest

from core.continuous_conversation import (
    ContinuousConversationManager,
    is_assistant_sleep_request,
    is_closing_statement,
    normalize_text_for_intent,
)


def test_veille_de_lassistant_ne_signifie_pas_veille_du_pc():
    assert is_assistant_sleep_request("ANO mets-toi en veille")
    assert is_assistant_sleep_request("passe en veille")
    assert is_assistant_sleep_request("Tu peux te mettre en veille.")
    assert not is_assistant_sleep_request("mets le PC en veille")


def test_normalize_text_for_intent():
    assert normalize_text_for_intent("Merci Beaucoup !") == "merci beaucoup"
    assert normalize_text_for_intent("C'est bon, merci.") == "c est bon merci"
    assert normalize_text_for_intent("À bientôt ANO !") == "a bientot ano"
    assert normalize_text_for_intent("") == ""


@pytest.mark.parametrize(
    "phrase,expected",
    [
        # Formules de clôture pures
        ("merci", True),
        ("Merci !", True),
        ("merci beaucoup", True),
        ("merci ano", True),
        ("merci jarvis", True),
        ("je te remercie", True),
        ("mille mercis", True),
        ("c'est bon", True),
        ("c'est bon merci", True),
        ("c'est bon pour moi", True),
        ("c'est tout", True),
        ("c'est tout merci", True),
        ("c'est tout pour l'instant", True),
        ("c'est parfait merci", True),
        ("c'est nickel", True),
        ("ça ira merci", True),
        ("ce sera tout", True),
        ("au revoir", True),
        ("au revoir ano", True),
        ("bonne nuit", True),
        ("bonne soirée", True),
        ("bonne journée", True),
        ("à plus", True),
        ("à bientôt", True),
        ("ciao", True),
        ("stop", True),
        ("arrête", True),
        ("arrête-toi", True),
        ("tais-toi", True),
        ("en veille", True),
        ("mets-toi en veille", True),
        ("non merci", True),
        ("non c'est bon", True),
        ("non c'est tout", True),
        ("rien d'autre merci", True),
        ("non rien de plus", True),
        ("ok merci beaucoup", True),
        ("d'accord merci", True),
        # Phrases avec demande d'action (NE DOIVENT PAS clore)
        ("merci de m'ouvrir le terminal", False),
        ("merci d'afficher la météo", False),
        ("merci cherche la météo de demain", False),
        ("c'est bon ouvre firefox", False),
        ("ouvre firefox s'il te plaît", False),
        ("mets de la musique", False),
        ("lance le lecteur", False),
        ("arrête la musique", False),  # commande multimédia, pas une mise en veille générale
        ("stop la lecture", False),
        ("ferme la fenêtre de dolphin", False),
        ("quelle heure est-il", False),
        ("rappelle-moi d'acheter du pain", False),
    ],
)
def test_is_closing_statement(phrase, expected):
    assert is_closing_statement(phrase) is expected


def test_continuous_conversation_timeout():
    async def _test():
        sleep_calls = []

        def fake_sleep(reason: str):
            sleep_calls.append(reason)

        manager = ContinuousConversationManager(
            timeout_s=0.1,  # timeout court pour le test
            on_sleep=fake_sleep,
        )

        # Fin de parole de l'assistant -> ouvre la fenêtre
        manager.on_assistant_speech_end()
        assert manager.is_active is True
        assert len(sleep_calls) == 0

        # Attente de l'expiration du timeout
        await asyncio.sleep(0.15)
        assert manager.is_active is False
        assert len(sleep_calls) == 1
        assert sleep_calls[0] == "inactivité conversation"

    asyncio.run(_test())


def test_continuous_conversation_user_interruption():
    async def _test():
        sleep_calls = []

        def fake_sleep(reason: str):
            sleep_calls.append(reason)

        manager = ContinuousConversationManager(
            timeout_s=0.2,
            on_sleep=fake_sleep,
        )

        manager.on_assistant_speech_end()
        assert manager.is_active is True

        # L'utilisateur commence à parler après 0.05s
        await asyncio.sleep(0.05)
        manager.on_user_speech_detected()
        assert manager.is_active is False

        # On attend au-delà de la durée du timeout initial : aucun sleep ne doit être appelé
        await asyncio.sleep(0.2)
        assert len(sleep_calls) == 0

    asyncio.run(_test())


def test_continuous_conversation_closing_statement_flow():
    async def _test():
        sleep_calls = []

        def fake_sleep(reason: str):
            sleep_calls.append(reason)

        manager = ContinuousConversationManager(
            timeout_s=10.0,
            on_sleep=fake_sleep,
        )

        # Tour 1 normal : question / réponse
        manager.on_assistant_speech_end()
        assert manager.is_active is True

        # Tour 2 : L'utilisateur prend la parole et dit "merci c'est bon"
        manager.on_user_speech_detected()
        is_closing = manager.on_user_transcript("Merci beaucoup, c'est bon !")
        assert is_closing is True
        assert manager.is_closing_turn is True

        # L'assistant commence sa réponse brève de politesse
        manager.on_assistant_speech_start()
        assert manager.is_active is False

        # L'assistant termine de parler -> fermeture immédiate !
        manager.on_assistant_speech_end()
        assert manager.is_active is False
        assert manager.is_closing_turn is False
        assert len(sleep_calls) == 1
        assert sleep_calls[0] == "clôture utilisateur"

    asyncio.run(_test())


def test_continuous_conversation_reset_and_cancel():
    manager = ContinuousConversationManager(timeout_s=5.0)
    manager.on_assistant_speech_end()
    manager.reset()
    assert manager.is_active is False
    assert manager.is_closing_turn is False


def test_jarvis_live_continuous_lifecycle():
    class StubUI:
        def __init__(self):
            self.muted = False
            self.state = "LISTENING"
            self.logs = []

        def set_state(self, s):
            self.state = s

        def write_log(self, msg):
            self.logs.append(msg)

    async def _test():
        ui = StubUI()
        manager = ContinuousConversationManager(
            timeout_s=0.1,
            on_sleep=lambda r: setattr(ui, "muted", True),
            on_log=lambda m: ui.write_log(m),
        )

        # 1. Réveil
        manager.on_wake_up("hotkey")
        ui.muted = False
        ui.set_state("LISTENING")

        # 2. Utilisateur parle
        manager.on_user_speech_detected()
        assert manager.is_active is False

        # 3. Assistant parle (half-duplex)
        manager.on_assistant_speech_start()
        ui.set_state("SPEAKING")
        assert manager.is_active is False

        # 4. Assistant termine de parler -> fenêtre continue s'ouvre
        manager.on_assistant_speech_end()
        ui.set_state("LISTENING")
        assert manager.is_active is True
        assert ui.muted is False

        # 5. Attente du timeout -> mise en veille automatique
        await asyncio.sleep(0.15)
        assert manager.is_active is False
        assert ui.muted is True

    asyncio.run(_test())


def test_inactivite_ne_coupe_pas_un_micro_impossible_a_reveiller():
    from main import JarvisLive

    class UI:
        muted = False

        def __init__(self):
            self.logs = []

        def write_log(self, text):
            self.logs.append(text)

    jarvis = JarvisLive.__new__(JarvisLive)
    jarvis.ui = UI()
    jarvis._wake = None

    result = JarvisLive._sleep(jarvis, "inactivité conversation")

    assert result == "listening"
    assert jarvis.ui.muted is False
    assert "micro maintenu actif" in jarvis.ui.logs[-1]


def test_tache_longue_video_garde_la_conversation_eveillee():
    """Images, vidéos et travaux différés empêchent tous la veille automatique."""
    from main import JarvisLive

    class Task:
        def __init__(self, done):
            self._done = done

        def done(self):
            return self._done

    jarvis = JarvisLive.__new__(JarvisLive)
    jarvis._image_generation_tasks = set()
    jarvis._video_generation_tasks = {Task(False)}
    jarvis._deep_research_tasks = set()
    jarvis._decision_simulation_tasks = set()

    assert jarvis._has_active_long_task() is True
    jarvis._video_generation_tasks = {Task(True)}
    assert jarvis._has_active_long_task() is False


def test_une_commande_ecrite_reveille_l_assistant_en_veille():
    """Un message chat ne doit jamais être accepté alors que la sortie reste muette."""
    from main import JarvisLive

    class UI:
        muted = True

    jarvis = JarvisLive.__new__(JarvisLive)
    jarvis.ui = UI()
    jarvis._loop = None
    jarvis.session = None
    calls = []
    jarvis._wake_up = lambda reason: calls.append(reason) or setattr(jarvis.ui, "muted", False)
    jarvis._try_switch_conversation_language = lambda text: False
    jarvis._try_switch_personality_mode = lambda text: False
    jarvis._observe_habit_reply = lambda text: None
    jarvis._maybe_routine = lambda text, source: False
    jarvis.detect_contextual_persona = lambda text: None
    jarvis.check_persona_voice_trigger = lambda text: None

    jarvis._on_text_command("où en est la génération ?")

    assert calls == ["commande texte"]
    assert jarvis.ui.muted is False


def test_no_idle_timer_when_sleep_is_not_allowed():
    """Sans réveil vocal, aucun minuteur : ni « passage en veille » ni
    « veille différée » à chaque fin de réponse."""
    from core.continuous_conversation import ContinuousConversationManager

    logs, slept = [], []
    mgr = ContinuousConversationManager(
        timeout_s=0.05, on_sleep=slept.append, on_log=logs.append,
        sleep_allowed=lambda: False,
    )
    mgr.on_assistant_speech_end(loop=None)
    mgr.on_assistant_speech_end(loop=None)
    assert mgr._timer_task is None
    assert slept == []
    assert len([m for m in logs if "permanente" in m]) == 1
    assert not any("25s" in m or "veille" in m.lower() and "permanente" not in m for m in logs)
