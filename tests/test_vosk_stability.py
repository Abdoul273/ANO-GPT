"""Barrière de stabilité autour de la bibliothèque native Vosk."""

from core import wake_word
from core import vad_silero


def test_vosk_est_desactive_par_defaut_sous_python_314(monkeypatch):
    monkeypatch.delenv("ANOGPT_ENABLE_VOSK_NATIVE", raising=False)
    assert wake_word.vosk_native_allowed((3, 13, 9)) is True
    assert wake_word.vosk_native_allowed((3, 14, 0)) is False
    assert wake_word.vosk_native_allowed((3, 15, 0)) is False


def test_override_permet_de_tester_une_future_version_sure(monkeypatch):
    monkeypatch.setenv("ANOGPT_ENABLE_VOSK_NATIVE", "1")
    assert wake_word.vosk_native_allowed((3, 14, 0)) is True

    monkeypatch.setenv("ANOGPT_ENABLE_VOSK_NATIVE", "0")
    assert wake_word.vosk_native_allowed((3, 13, 0)) is False


def test_detecteur_ne_charge_pas_libvosk_quand_interdit(monkeypatch, tmp_path):
    monkeypatch.setattr(wake_word, "vosk_native_allowed", lambda: False)
    detector = wake_word.WakeWordDetector(model_path=tmp_path)
    assert detector.available is False
    assert detector._model is None
    assert detector._rec is None


def test_veille_privilegie_le_petit_modele_et_garde_le_grand_en_repli(monkeypatch, tmp_path):
    small = tmp_path / "small"
    large = tmp_path / "large"
    for path in (small, large):
        (path / "am").mkdir(parents=True)
    monkeypatch.delenv("ANOGPT_VOSK_MODEL", raising=False)
    monkeypatch.setattr(wake_word, "SMALL_MODEL", small)
    monkeypatch.setattr(wake_word, "LARGE_MODEL", large)

    assert wake_word.resolve_vosk_model_path() == small

    # Sans le modèle léger, la reconnaissance locale reste disponible grâce
    # au modèle complet que l'utilisateur a installé.
    for child in small.iterdir():
        child.rmdir()
    small.rmdir()
    assert wake_word.resolve_vosk_model_path() == large


def test_onnx_vad_est_isole_sous_python_314(monkeypatch):
    monkeypatch.delenv("ANOGPT_ENABLE_ONNX_VAD", raising=False)
    assert vad_silero.onnx_native_allowed((3, 13, 9)) is True
    assert vad_silero.onnx_native_allowed((3, 14, 0)) is False


def test_onnx_vad_respecte_override(monkeypatch):
    monkeypatch.setenv("ANOGPT_ENABLE_ONNX_VAD", "1")
    assert vad_silero.onnx_native_allowed((3, 14, 0)) is True
    monkeypatch.setenv("ANOGPT_ENABLE_ONNX_VAD", "off")
    assert vad_silero.onnx_native_allowed((3, 13, 0)) is False
