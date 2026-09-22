"""« Coupe le micro » : ANO répondait « je ne peux pas ». Il pilote désormais
sa propre interface (micro, cartes, écran, fenêtre)."""
import asyncio

from core import tool_dispatcher as td
from core.tool_packs import CORE


class _UI:
    def __init__(self):
        self.muted = False
        self.calls = []

    def __getattr__(self, name):
        return lambda *a, **k: self.calls.append(name)


class _Host:
    _interface_control = td.ToolDispatcher._interface_control

    def __init__(self):
        self.ui = _UI()
        self.slept = []
        self.woke = []
        self._is_speaking = False

    async def _wait_voice_silence(self, **_k):
        pass

    def _sleep(self, reason):
        self.slept.append(reason)
        self.ui.muted = True
        return "muted"

    def _wake_up(self, reason):
        self.woke.append(reason)
        self.ui.muted = False


def test_the_tool_is_declared_and_always_available():
    names = {d["name"] for d in td.TOOL_DECLARATIONS}
    assert "interface_control" in names
    assert "interface_control" in CORE


def test_mute_waits_for_the_spoken_confirmation_then_cuts_the_mic():
    host = _Host()

    async def scenario():
        out = host._interface_control("mute_mic")
        assert host.slept == []  # la phrase de confirmation passe d'abord
        await asyncio.sleep(0.05)
        return out

    out = asyncio.run(scenario())
    assert "bouton micro" in out
    assert host.slept == ["demande vocale"]
    assert host.ui.muted is True


def test_unmute_and_status():
    host = _Host()
    host.ui.muted = True
    assert "coupé" in host._interface_control("mic_status")
    assert "réactivé" in host._interface_control("unmute_mic")
    assert host.woke == ["demande"]
    assert "actif" in host._interface_control("mic_status")


def test_clear_screen_closes_everything():
    host = _Host()
    out = host._interface_control("clear_screen")
    for step in ("close_map", "close_image_gallery", "close_video",
                 "clear_visual_pointers", "dismiss_cards"):
        assert step in host.ui.calls
    assert "nettoyé" in out


def test_unknown_action_lists_the_choices():
    assert "mute_mic" in _Host()._interface_control("danse")
