"""Les outils longs (vision, coach TikTok) ne gardent plus le micro fermé :
le travail part en arrière-plan et son résultat revient dans un nouveau tour."""
import asyncio
import sys
import types

from core import tool_dispatcher as td


def test_only_identifications_are_deferred():
    assert td._vision_is_deferred({})
    assert td._vision_is_deferred({"action": "identify"})
    assert td._vision_is_deferred({"action": "Regarde"})
    assert not td._vision_is_deferred({"action": "list_people"})
    assert not td._vision_is_deferred({"action": "forget_person"})


class _UI:
    def write_log(self, *_a):
        pass


class _Fake:
    def __init__(self):
        self.ui = _UI()
        self.session = object()
        self.speak = lambda *_a: None
        self._tool_session_memory = {}
        self._grab_camera_still = lambda: None
        self._save_capture = lambda *_a, **_k: ""
        self.cards, self.turns, self.deferred = [], [], []

    def _task_card(self, *a):
        self.cards.append(a)

    def _ui_card(self, *a):
        pass

    async def _submit_text_turn(self, prompt, timeout_s=0):
        self.turns.append(prompt)
        return True

    def _defer_turn(self, prompt):
        self.deferred.append(prompt)


def test_the_vision_result_is_announced_in_a_new_turn(monkeypatch):
    calls = {}

    def fake_vision(**kwargs):
        calls.update(kwargs)
        return "C'est un Arduino Uno R3."

    monkeypatch.setitem(sys.modules, "actions.visual_recognition",
                        types.SimpleNamespace(visual_recognition=fake_vision))
    fake = _Fake()

    asyncio.run(td.ToolDispatcher._deliver_deferred_tool(fake, "visual_recognition", {"action": "identify"}))

    assert calls["parameters"] == {"action": "identify"}
    assert calls["grab_frame"] is fake._grab_camera_still
    assert fake.turns and "Arduino Uno R3" in fake.turns[0]
    assert fake.cards[-1][-1] == "done"


def test_tiktok_video_analyses_are_deferred_but_local_reads_are_not():
    assert td._tiktok_is_deferred({})
    assert td._tiktok_is_deferred({"action": "review"})
    assert td._tiktok_is_deferred({"action": "draft"})
    assert not td._tiktok_is_deferred({"action": "list"})
    assert not td._tiktok_is_deferred({"action": "best_time"})
    assert not td._tiktok_is_deferred({"action": "report"})
