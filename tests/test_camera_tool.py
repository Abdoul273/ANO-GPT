"""Le routage vocal de la caméra : « ouvre l'appareil photo » ne doit jamais
lancer une application externe, et chaque action doit atteindre le studio."""

import re
from pathlib import Path

import pytest

PROMPT = (Path(__file__).resolve().parent.parent / "core" / "prompt.txt").read_text(
    encoding="utf-8"
)
MAIN = (Path(__file__).resolve().parent.parent / "core" / "tool_dispatcher.py").read_text(encoding="utf-8") + (Path(__file__).resolve().parent.parent / "core" / "tool_declarations.py").read_text(encoding="utf-8")


def test_prompt_forbids_opening_an_external_camera_app():
    assert "camera_control" in PROMPT
    assert "ouvre l'appareil photo" in PROMPT
    # C'est la consigne qui empêche le modèle de retomber sur open_app.
    assert re.search(r"JAMAIS open_app", PROMPT)


def test_camera_control_is_declared_as_a_tool():
    assert '"name": "camera_control"' in MAIN
    for action in ("video_start", "video_stop", "switch", "photo"):
        assert action in MAIN, f"action {action} absente de la déclaration"


class _Studio:
    """Studio factice : enregistre les appels au lieu d'ouvrir une caméra."""

    def __init__(self):
        self.calls: list[str] = []
        self.active = False
        self.recording = False
        self.source = "pc"
        self.lens = "back"
        self._online = False

    def phone_online(self):
        return self._online

    def latest_frame(self, timeout=0.0):
        return b"\xff\xd8jpeg"

    def wait_fresh_frame(self, timeout=4.0, settle=1.0):
        return b"\xff\xd8jpeg"

    def open(self, source, *, lens=None):
        self.calls.append(f"open:{source}" + (f":{lens}" if lens else ""))
        self.active = True
        self.source = source
        if lens:
            self.lens = lens
        return "Caméra ouverte."

    def set_lens(self, lens):
        self.calls.append(f"lens:{lens}")
        self.lens = "front" if "front" in str(lens) else "back"
        self.source = "phone"
        self.active = True
        return f"Caméra {'frontale du' if self.lens == 'front' else 'du'} téléphone."

    def switch_lens(self):
        self.calls.append("flip")
        return self.set_lens("back" if self.lens == "front" else "front")

    def photo(self):
        self.calls.append("photo")
        return Path("ano-photo-test.jpg")

    def start_video(self):
        self.calls.append("start_video")
        self.recording = True
        return Path("ano-video-test.mp4")

    def stop_video(self):
        self.calls.append("stop_video")
        self.recording = False
        return Path("ano-video-test.mp4")

    def switch_source(self):
        self.calls.append("switch")
        self.source = "phone" if self.source == "pc" else "pc"
        return "Caméra du téléphone."

    def close(self):
        self.calls.append("close")
        self.active = False
        return "Caméra fermée."


@pytest.fixture
def assistant():
    """Instance minimale portant seulement _camera_tool et ses dépendances."""
    import main as main_module

    class _UI:
        def __init__(self):
            self.logs: list[str] = []

        def write_log(self, text):
            self.logs.append(text)

    obj = object.__new__(main_module.JarvisLive)
    obj.ui = _UI()
    obj._dashboard = None
    obj._camera = _Studio()
    # `camera` est une propriété paresseuse : ici le studio factice est déjà en
    # place, donc elle le renvoie tel quel.
    return obj


def test_open_uses_the_phone_when_it_is_the_only_camera_filming(assistant):
    class _Dashboard:
        _clients = {"un-telephone"}

    assistant._dashboard = _Dashboard()
    assistant._camera._online = True
    message = assistant._camera_tool("open", "")
    assert "phone" in assistant._camera.calls[0]
    assert "Caméra" in message


def test_photo_opens_the_view_first_if_needed(assistant):
    message = assistant._camera_tool("photo", "pc")
    assert assistant._camera.calls == ["open:pc", "photo"]
    assert "ano-photo-test.jpg" in message


def test_video_start_and_stop_are_routed(assistant):
    assert "ano-video-test.mp4" in assistant._camera_tool("video_start", "")
    assert assistant._camera.recording
    assert "ano-video-test.mp4" in assistant._camera_tool("video_stop", "")
    assert not assistant._camera.recording


def test_stopping_without_recording_says_so(assistant):
    assistant._camera.stop_video = lambda: None
    assert "Aucun enregistrement" in assistant._camera_tool("video_stop", "")


def test_unknown_action_is_reported_not_crashed(assistant):
    assert "inconnue" in assistant._camera_tool("teleporte", "")


def test_camera_failure_becomes_a_spoken_sentence(assistant):
    from core.camera_studio import CameraUnavailable

    def _boom(_source, *, lens=None):
        raise CameraUnavailable("Aucune webcam détectée.")

    assistant._camera.open = _boom
    message = assistant._camera_tool("open", "pc")
    assert message.startswith("Caméra indisponible")
    assert "webcam" in message


def test_phone_camera_without_a_connected_phone_is_explained(assistant):
    """Un conteneur noir sans explication était le pire résultat possible."""
    assistant._camera.latest_frame = lambda timeout=0.0: None
    message = assistant._camera_tool("open", "phone")
    assert "Aucun téléphone connecté" in message


def test_phone_that_never_sends_an_image_is_reported(assistant):
    class _Dashboard:
        _clients = {"un-telephone"}

    assistant._dashboard = _Dashboard()
    assistant._camera.latest_frame = lambda timeout=0.0: None
    message = assistant._camera_tool("open", "phone")
    assert "n'a pas envoyé d'image" in message


def test_phone_camera_that_answers_is_accepted(assistant):
    class _Dashboard:
        _clients = {"un-telephone"}

    assistant._dashboard = _Dashboard()
    assistant._camera.latest_frame = lambda timeout=0.0: b"\xff\xd8jpeg"
    assert assistant._camera_tool("open", "phone") == "Caméra ouverte."


# ── caméra frontale du téléphone ────────────────────────────────────────────

def _with_phone(assistant):
    class _Dashboard:
        _clients = {"un-telephone"}

    assistant._dashboard = _Dashboard()
    return assistant


def test_the_front_lens_is_declared_as_a_tool_parameter():
    assert '"lens"' in MAIN
    assert "front" in MAIN and "flip" in MAIN


def test_the_prompt_routes_a_selfie_to_the_front_lens():
    assert "frontale" in PROMPT
    assert "lens='front'" in PROMPT


def test_asking_for_the_front_camera_switches_the_lens(assistant):
    _with_phone(assistant)
    message = assistant._camera_tool("lens", "", "front")
    assert "lens:front" in assistant._camera.calls
    assert "frontale" in message


def test_a_selfie_word_alone_is_understood(assistant):
    """« prends un selfie » n'emploie jamais le mot « front »."""
    _with_phone(assistant)
    assistant._camera_tool("lens", "", "selfie")
    assert assistant._camera.lens == "front"


def test_the_front_lens_implies_the_phone_without_being_asked(assistant):
    """Seul le téléphone a deux objectifs : la source doit suivre."""
    _with_phone(assistant)
    assistant._camera_tool("open", "", "front")
    assert assistant._camera.calls[0] == "open:phone:front"


def test_flip_toggles_between_the_two_lenses(assistant):
    _with_phone(assistant)
    assistant._camera_tool("flip", "")
    assert assistant._camera.lens == "front"
    assistant._camera_tool("flip", "")
    assert assistant._camera.lens == "back"


def test_a_photo_with_a_lens_change_waits_for_a_fresh_image(assistant):
    """L'image en mémoire vient de l'autre objectif : la reprendre trompe."""
    _with_phone(assistant)
    assistant._camera.active = True
    waited = []
    assistant._camera.wait_fresh_frame = (
        lambda timeout=4.0, settle=1.0: waited.append(timeout) or b"\xff\xd8"
    )
    assistant._camera_tool("photo", "phone", "front")
    assert waited, "la photo a été prise sans attendre la nouvelle caméra"
    assert assistant._camera.calls[-1] == "photo"


def test_an_unrecognised_lens_word_does_not_force_the_phone(assistant):
    assistant._camera_tool("open", "pc", "objectif magique")
    assert assistant._camera.calls[0] == "open:pc"
