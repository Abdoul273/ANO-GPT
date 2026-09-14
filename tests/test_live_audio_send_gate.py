"""Le consommateur Live ne rejoue ni l'écho ni une ancienne phrase."""
import asyncio
import contextlib
import time
from types import SimpleNamespace

import pytest

from core.session_manager import SessionManager


@pytest.mark.parametrize("condition", ["speaking", "interrupted", "muted", "old_epoch", "stale", "fresh", "phone", "video"])
def test_live_audio_is_checked_before_captions_and_transport(monkeypatch, condition):
    async def scenario():
        sent, captioned = [], []
        class Captions:
            def __init__(self, host): pass
            def start(self): pass
            def push(self, msg): captioned.append(msg)
            async def close(self): pass
        async def send(**kwargs): sent.append(kwargs)
        monkeypatch.setattr("core.live_captions.LiveCaptions", Captions)
        host = SimpleNamespace(out_queue=asyncio.Queue(), session=SimpleNamespace(send_realtime_input=send),
                               ui=SimpleNamespace(muted=condition in {"muted", "phone"}),
                               _is_speaking=condition in {"speaking", "video"},
                               _interrupted=condition == "interrupted", _speech_output_epoch=2)
        msg = {"data": b"pcm", "mime_type": "audio/pcm;rate=16000", "_audio_epoch": 2,
               "_captured_at": time.monotonic()}
        if condition == "old_epoch": msg["_audio_epoch"] = 1
        if condition == "stale": msg["_captured_at"] -= 30
        if condition == "phone": msg["_audio_source"] = "phone"
        if condition == "video": msg["activity"] = "video"
        await host.out_queue.put(msg)
        task = asyncio.create_task(SessionManager._send_realtime(host))
        await asyncio.sleep(0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError): await task
        assert len(sent) == int(condition in {"fresh", "phone", "video"})
        assert len(captioned) == int(condition in {"fresh", "phone"})
    asyncio.run(scenario())
