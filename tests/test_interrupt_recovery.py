"""Une interruption ne doit jamais rendre l'assistant muet pour toujours.

`_interrupted` fait jeter l'audio, les transcriptions et jusqu'aux appels
d'outils du modèle. Il n'était effacé qu'à la réception du `turn_complete` du
tour coupé — un message qui n'arrive pas toujours. L'assistant restait alors
« à l'écoute » sans plus jamais répondre : on parlait, rien ne venait.
"""

import inspect
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MAIN = (ROOT / "core" / "session_manager.py").read_text(encoding="utf-8")


class _UI:
    def __init__(self):
        self.logs = []

    def write_log(self, text):
        self.logs.append(text)


@pytest.fixture
def assistant():
    """Instance nue portant seulement l'état d'interruption."""
    import main as main_module

    obj = object.__new__(main_module.JarvisLive)
    obj.ui = _UI()
    obj._interrupted = False
    obj._speech_display_open = False
    obj._speech_next_text_at = 0.0
    obj._activity_open = False
    obj._drain_spoken_text_queue = lambda: None
    return obj


def test_le_drapeau_se_referme_et_rend_la_parole(assistant):
    assistant._interrupted = True
    assistant._speech_display_open = True

    assistant._clear_interrupted()

    assert assistant._interrupted is False
    assert assistant._speech_display_open is False
    assert "[INLINE_END]" in assistant.ui.logs


def test_refermer_deux_fois_ne_fait_rien_de_plus(assistant):
    """Appelé à chaque nouveau tour, il doit rester sans effet au repos."""
    assistant._clear_interrupted()
    assert assistant.ui.logs == []


def test_un_tour_texte_bloque_l_ouverture_stt(assistant):
    """Pendant le briefing, le VAD ne doit pas ouvrir un tour sur l'écho."""
    import main as main_module

    class _Locked:
        def locked(self):
            return True

    assistant._interrupted = False
    assistant._is_speaking = False
    assistant._is_thinking = False
    assistant._model_turn_active = False
    assistant._audio_turn_active = False
    assistant._activity_open = False
    assistant._turn_submit_lock = _Locked()
    assistant.out_queue = None

    main_module.JarvisLive._activity_start(assistant)

    assert assistant._activity_open is False


def test_la_reprise_de_parole_referme_une_interruption_bloquee(assistant):
    """Le filet de sécurité : l'utilisateur reparle, donc c'est fini."""
    import main as main_module

    assistant._interrupted = True
    assistant._activity_since = 0.0
    assistant._voice_evidence_ms = 0.0
    assistant._last_voice_evidence_ms = 0.0
    assistant._last_voice_audio_ms = 0.0
    assistant.out_queue = None          # `_enqueue_out` sort aussitôt

    main_module.JarvisLive._activity_start(assistant)

    assert assistant._interrupted is False, (
        "une interruption survit à la reprise de parole de l'utilisateur"
    )
    assert assistant._activity_open is True


# ── les points de fermeture, dans la boucle de réception ────────────────────

def test_un_nouveau_tour_du_modele_referme_linterruption():
    """C'est la garantie principale : le modèle reparle, donc c'est terminé."""
    start = MAIN.index("if audio_data:")
    block = MAIN[start:start + 500]
    assert "if not self._model_turn_active:" in block
    assert "_clear_interrupted()" in block


def test_la_transcription_de_sortie_referme_aussi():
    """Certains tours n'arrivent que par la transcription, sans audio brut."""
    start = MAIN.index("sc.output_transcription and sc.output_transcription.text")
    block = MAIN[start:start + 300]
    assert "_clear_interrupted()" in block


def test_le_drapeau_nest_arme_que_pendant_un_tour_reel():
    """Armé hors tour, c'est la réponse SUIVANTE qui était jetée."""
    body = inspect.getsource(
        __import__("main").JarvisLive.interrupt
    )
    assert "if self._model_turn_active:" in body
    guard = body.index("if self._model_turn_active:")
    arm = body.index("self._interrupted = True")
    assert guard < arm
