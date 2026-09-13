"""Le bouton micro est l'unique moyen de lever une coupure volontaire."""

from main import JarvisLive
from ui.window.dialogs_host import DialogsHostMixin


class _UI:
    def __init__(self, *, locked: bool):
        self.muted = True
        self.microphone_locked = locked
        self.logs: list[str] = []

    def write_log(self, message: str) -> None:
        self.logs.append(message)


def test_reveils_automatiques_ne_rouvrent_pas_un_micro_verrouille():
    jarvis = JarvisLive.__new__(JarvisLive)
    jarvis.ui = _UI(locked=True)
    jarvis._wake = None
    jarvis._last_user_speech = 0.0

    assert jarvis._wake_up("mot d'activation") == "locked"
    assert jarvis.ui.muted is True
    assert "bouton micro" in jarvis.ui.logs[-1]


def test_un_mute_non_verrouille_garde_le_reveil_habituel():
    jarvis = JarvisLive.__new__(JarvisLive)
    jarvis.ui = _UI(locked=False)
    jarvis._wake = None
    jarvis._last_user_speech = 0.0

    assert jarvis._wake_up("mot d'activation") == "listening"
    assert jarvis.ui.muted is False


def test_seul_le_bouton_micro_leve_le_verrou_manuel():
    class Log:
        def append_log(self, _message: str) -> None:
            pass

    class Window(DialogsHostMixin):
        def __init__(self):
            self._muted = False
            self._manual_mic_lock = False
            self.hud = type("Hud", (), {"muted": False, "state": "LISTENING"})()
            self._log = Log()

        def _style_mute_btn(self) -> None:
            pass

        def _apply_state(self, _state: str) -> None:
            pass

    window = Window()
    window._toggle_mute()  # clic : coupe et verrouille
    assert window._muted is True and window._manual_mic_lock is True

    window._set_muted(False)  # réveil automatique : refusé
    assert window._muted is True

    window._toggle_mute()  # seul le clic explicite déverrouille
    assert window._muted is False and window._manual_mic_lock is False
