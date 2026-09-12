"""Reconnaissance locale des arrêts et redirections pendant la voix."""

import numpy as np

from core.barge_in import InterruptPhraseDetector, LocalBargeInListener


def test_seuls_les_mots_cles_coupent_la_parole():
    assert InterruptPhraseDetector.classify("stop") == "stop"
    assert InterruptPhraseDetector.classify("arrête-toi") == "stop"
    assert InterruptPhraseDetector.classify("écoute") == "stop"
    assert InterruptPhraseDetector.classify("ano") == "stop"
    assert InterruptPhraseDetector.classify("ANO stop") == "stop"
    assert not InterruptPhraseDetector._is_command("tais toi")
    assert InterruptPhraseDetector.classify("attends") is None
    assert InterruptPhraseDetector.classify("une seconde") is None
    assert InterruptPhraseDetector.classify("chut") is None
    assert InterruptPhraseDetector.classify("1 2 3") is None
    assert InterruptPhraseDetector.classify("Yo no hablo español") is None


def test_les_transcriptions_phonetiques_d_ano_sont_acceptees():
    assert InterruptPhraseDetector._is_command("ANO stop")
    assert InterruptPhraseDetector._is_command("anneau stop")
    assert InterruptPhraseDetector._is_command("anno arrête-toi")


def test_ecoute_suivi_dune_commande_redirige():
    assert InterruptPhraseDetector.classify("écoute, cherche Firefox") == "redirect"
    assert InterruptPhraseDetector.classify("ANO ouvre Firefox") == "redirect"
    assert InterruptPhraseDetector.classify("la météo est agréable") is None
    assert InterruptPhraseDetector.classify("Non, cherche plutôt sur le web") is None


def test_le_flux_aec_transmet_la_nature_de_linterruption():
    calls = []

    class Detector:
        available = True
        last_kind = "redirect"
        last_text = "non cherche plutôt"

        def process(self, _pcm):
            return True

        def reset(self):
            return None

    class Stream:
        def __init__(self, **kwargs):
            self.callback = kwargs["callback"]

        def start(self):
            self.callback(np.zeros((320, 1), dtype=np.int16), 320, None, None)

        def stop(self):
            return None

        def close(self):
            return None

    class SoundDevice:
        InputStream = Stream

    listener = LocalBargeInListener(
        SoundDevice, Detector(), lambda: True,
        lambda kind, text: calls.append((kind, text)), quiet_arm_ms=0,
    )
    assert listener.start()
    assert calls == [("redirect", "non cherche plutôt")]


def test_micro_coupe_desactive_totalement_le_flux_de_barge_in():
    """Le second flux AEC ne doit jamais contourner le bouton mute."""
    calls = []
    detector_calls = []

    class Detector:
        available = True

        def process(self, _pcm):
            detector_calls.append(True)
            return True

        def reset(self):
            return None

    class Stream:
        def __init__(self, **kwargs):
            self.callback = kwargs["callback"]

        def start(self):
            self.callback(np.ones((320, 1), dtype=np.int16), 320, None, None)

        def stop(self):
            return None

        def close(self):
            return None

    class SoundDevice:
        InputStream = Stream

    listener = LocalBargeInListener(
        SoundDevice, Detector(), lambda: True,
        lambda *args: calls.append(args),
        is_enabled=lambda: False,
    )

    assert listener.start()
    assert detector_calls == []
    assert calls == []


def test_mute_active_pendant_vosk_annule_linterruption():
    """Couvre la course où F4 est pressé durant l'inférence locale."""
    calls = []
    enabled = {"value": True}

    class Detector:
        available = True
        last_kind = "stop"
        last_text = "stop"

        def process(self, _pcm):
            enabled["value"] = False
            return True

        def reset(self):
            return None

    class Stream:
        def __init__(self, **kwargs):
            self.callback = kwargs["callback"]

        def start(self):
            self.callback(np.ones((320, 1), dtype=np.int16), 320, None, None)

        def stop(self):
            return None

        def close(self):
            return None

    class SoundDevice:
        InputStream = Stream

    listener = LocalBargeInListener(
        SoundDevice, Detector(), lambda: True,
        lambda *args: calls.append(args),
        is_enabled=lambda: enabled["value"],
    )

    assert listener.start()
    assert calls == []


def test_formules_hors_liste_ne_coupent_plus():
    assert InterruptPhraseDetector.classify("chut") is None
    assert InterruptPhraseDetector.classify("coupe") is None
    assert InterruptPhraseDetector.classify("annule") is None
    assert InterruptPhraseDetector.classify("laisse tomber") is None
    assert InterruptPhraseDetector.classify("ça suffit") is None
    assert InterruptPhraseDetector.classify("du calme") is None
    assert InterruptPhraseDetector.classify("ANO tais-toi") == "redirect"
    assert InterruptPhraseDetector.classify("stop s'il te plaît") == "stop"
    assert InterruptPhraseDetector.classify("arrête") == "stop"
    assert InterruptPhraseDetector.classify("écoute, cherche plutôt à Lyon") == "redirect"
    assert InterruptPhraseDetector.classify("attends non, c'est pas ça") is None
    assert InterruptPhraseDetector.classify("non en fait je voulais dire demain") is None
    assert InterruptPhraseDetector.classify("rectification, ouvre chrome") is None



def test_un_bruit_fort_ne_remplace_jamais_un_detecteur_de_mots():
    calls = []
    class Stream:
        def __init__(self, **kwargs): self.callback = kwargs["callback"]
        def start(self):
            for _ in range(20):
                self.callback(np.full((320, 1), 20000, dtype=np.int16), 320, None, None)
        def stop(self): pass
        def close(self): pass
    listener = LocalBargeInListener(
        type("SD", (), {"InputStream": Stream}),
        type("Detector", (), {"available": False})(),
        lambda: True, lambda *args: calls.append(args),
    )
    listener.start()
    assert calls == []
    listener.stop()


def test_mode_strict_exige_une_commande_adressee_a_ano():
    detector = InterruptPhraseDetector.__new__(InterruptPhraseDetector)
    detector.addressed_only = True
    assert detector._classify_detected("stop") is None
    assert detector._classify_detected("attends") is None
    assert detector._classify_detected("non cherche plutôt") is None
    assert detector._classify_detected("ANO stop") == "stop"
    assert detector._classify_detected("ANO écoute") == "stop"


def test_mode_strict_naccepte_que_les_formules_exactes():
    strict = InterruptPhraseDetector.classify_strict_interrupt
    for phrase in ("Ano stop", "arrête-toi", "écoute"):
        assert strict(phrase) == "stop"
    for phrase in ("anneau", "stop", "stop tu", "ano", "Ano ouvre Firefox",
                   "écoute cherche Firefox", "arrête toi maintenant"):
        assert strict(phrase) is None


def test_worker_isole_respecte_aussi_le_mode_strict():
    """Le worker Vosk ne doit pas contourner addressed_only avec « anneau »."""
    class Isolated:
        available = True
        last_text = "anneau"
        last_kind = "stop"
        def process(self, _pcm): return True
        def reset(self): self.was_reset = True

    detector = InterruptPhraseDetector.__new__(InterruptPhraseDetector)
    detector.addressed_only = True
    detector.available = True
    detector._isolated = Isolated()
    assert detector.process(np.zeros(320, dtype=np.int16)) is False
    assert detector._isolated.was_reset is True

    detector._isolated.last_text = "anneau stop"
    assert detector.process(np.zeros(320, dtype=np.int16)) is True
    assert detector.last_kind == "stop"


def test_le_reseau_est_coupe_pendant_la_reponse_hors_mot_cle():
    from core.barge_in import hold_live_audio
    assert hold_live_audio(
        speaking=True, model_turn_active=False, thinking=False, interrupted=False,
    ) is True
    assert hold_live_audio(
        speaking=False, model_turn_active=True, thinking=False, interrupted=False,
    ) is True
    assert hold_live_audio(
        speaking=False, model_turn_active=False, thinking=True, interrupted=False,
    ) is True
    assert hold_live_audio(
        speaking=True, model_turn_active=True, thinking=False, interrupted=True,
    ) is True
    assert hold_live_audio(
        speaking=False, model_turn_active=False, thinking=False, interrupted=False,
    ) is False
    assert hold_live_audio(
        speaking=False, model_turn_active=False, thinking=False, interrupted=True,
        noise_turn=True,
    ) is True
    assert hold_live_audio(
        speaking=False, model_turn_active=False, thinking=False, interrupted=False,
        text_turn_pending=True,
    ) is True
