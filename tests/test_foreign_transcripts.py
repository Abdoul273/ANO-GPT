"""Paroles d'une musique en fond prises pour des demandes (23/09) :
« invecchiando », « ¿Y qué más queda? », « music only » — ce dernier a ouvert
le paquet musique et lancé une écoute de 50 s."""
import pytest

from core.ai_stt_corrector import TranscriptGuard, foreign_phrase_reason
from core.session_manager import SessionManager


@pytest.mark.parametrize("text", [
    "invecchiando", "¿Y qué más queda?", "music only", "I love you baby",
    "che cosa vuoi", "no me digas nada pero",
])
def test_foreign_phrases_are_flagged(text):
    assert foreign_phrase_reason(text)
    assert not TranscriptGuard().assess(text).accepted


@pytest.mark.parametrize("text", [
    "Salut.", "Efface ce que j'ai écrit.", "toute la ligne", "ouvre Firefox",
    "lance Spotify", "M'amène vers le bureau 2.", "play music", "YouTube music",
    "mets de la musique", "Git status", "stop", "bien", "non merci", "commande",
])
def test_french_and_tech_words_pass(text):
    assert foreign_phrase_reason(text) == ""


class _UI:
    def __init__(self):
        self.logs = []

    def write_log(self, text):
        self.logs.append(text)


class _Host:
    _screen_foreign_transcript = SessionManager._screen_foreign_transcript

    def __init__(self, language="fr-FR"):
        self.ui = _UI()
        self._conversation_language = language
        self._noise_turn = False
        self._live_user_text = "music only"


def test_a_foreign_turn_is_muted_and_never_resent():
    host = _Host()
    assert host._screen_foreign_transcript("music only")
    assert host._noise_turn is True
    assert host._live_user_text == ""
    assert len(host.ui.logs) == 1


def test_a_french_word_that_follows_gives_the_turn_back():
    host = _Host()
    host._screen_foreign_transcript("music")
    host._screen_foreign_transcript("music only")
    assert not host._screen_foreign_transcript("music only non ouvre Spotify")
    assert host._noise_turn is False


def test_the_filter_is_off_when_the_conversation_is_not_french():
    host = _Host(language="es-ES")
    assert not host._screen_foreign_transcript("¿Y qué más queda?")
    assert host._noise_turn is False
