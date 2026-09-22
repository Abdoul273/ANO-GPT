"""Une tâche terminée pendant la coupure doit être annoncée après reprise."""

import asyncio

import main


class _UI:
    def __init__(self):
        self.logs = []
        self.cards = []

    def write_log(self, message):
        self.logs.append(message)

    def show_card(self, *args):
        self.cards.append(args)

    def task_card(self, *args):
        self.cards.append(args)


def _host():
    host = main.JarvisLive.__new__(main.JarvisLive)
    host.ui = _UI()
    host.session = None
    host._deferred_turns = []
    return host


def test_deep_research_survives_disconnect(monkeypatch):
    async def scenario():
        host = _host()
        monkeypatch.setattr(host, "_compute_deep_research", lambda *_: "réponse calculée")

        async def disconnected(_text, **_kwargs):
            raise ConnectionError("socket fermé")

        host._submit_text_turn = disconnected
        await host._deliver_deep_research("ma question")
        assert len(host._deferred_turns) == 1
        assert "réponse calculée" in host._deferred_turns[0]

        delivered = []
        host.session = object()

        async def reconnected(text, **_kwargs):
            delivered.append(text)
            return True

        host._submit_text_turn = reconnected
        await host._flush_deferred_turns()
        assert len(delivered) == 1
        assert not host._deferred_turns

    asyncio.run(scenario())


def test_video_result_survives_disconnect(monkeypatch):
    from actions import video_generation

    monkeypatch.setattr(video_generation, "generate_video", lambda *_args: "vidéo créée : /tmp/video.mp4")

    async def scenario():
        host = _host()

        async def disconnected(_text, **_kwargs):
            raise ConnectionError("socket fermé")

        host._submit_text_turn = disconnected
        await host._deliver_video_generation({"prompt": "une scène"})
        assert len(host._deferred_turns) == 1
        assert "/tmp/video.mp4" in host._deferred_turns[0]
        assert any("vidéo prête" in line for line in host.ui.logs)

    asyncio.run(scenario())
