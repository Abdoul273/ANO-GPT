"""Fin de phrase sémantique et session Transcribe pré-chauffée."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from core.audio_engine import (
    _END_SILENCE_S,
    _FAST_END_SILENCE_S,
    preview_looks_complete,
)


def test_la_fin_rapide_reste_bien_plus_courte_que_le_maintien():
    assert 0.3 <= _FAST_END_SILENCE_S <= 0.6
    assert _FAST_END_SILENCE_S < _END_SILENCE_S / 1.5


def test_une_phrase_conclue_par_le_serveur_autorise_la_fin_rapide():
    now = 100.0
    assert preview_looks_complete(("Ouvre Firefox.", now - 0.4, "t1"), now, 98.0)
    assert preview_looks_complete(("Tu m'entends ?", now - 0.4, "t1"), now, 98.0)


def test_une_phrase_ouverte_garde_le_delai_long():
    now = 100.0
    # Pas de ponctuation finale : hésitation possible (« ouvre… euh… »).
    assert not preview_looks_complete(("Ouvre", now - 0.4, "t1"), now, 98.0)
    assert not preview_looks_complete(("Ouvre Firefox,", now - 0.4, "t1"), now, 98.0)
    # Aperçu trop frais : le dernier mot peut encore arriver.
    assert not preview_looks_complete(("Ouvre Firefox.", now - 0.05, "t1"), now, 98.0)
    # Aperçu d'un tour précédent.
    assert not preview_looks_complete(("Ouvre Firefox.", 97.0, "t0"), now, 98.0)
    assert not preview_looks_complete(None, now, 98.0)
    assert not preview_looks_complete(("", now - 1.0, "t1"), now, 98.0)


def test_la_session_transcribe_est_ouverte_avant_la_premiere_phrase():
    import core.gemini_transcribe_stt as stt

    started = []

    class Turn:
        def __init__(self, host, on_partial):
            self.pcm = bytearray()

        async def start(self):
            started.append(True)

        async def begin(self, _on_partial):
            return None

        async def close(self):
            return None

    host = SimpleNamespace(
        out_queue=asyncio.Queue(), ui=SimpleNamespace(write_log=lambda *_: None, muted=False),
        _speech_output_epoch=0, _audio_drop_count=0, _precision_turn_text="",
    )

    async def scenario():
        task = asyncio.create_task(stt.run_gemini_transcribe(host, turn_factory=Turn))
        for _ in range(10):
            if started:
                break
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    # Aucun marqueur « start » n'a été reçu : la connexion est pourtant prête.
    assert started == [True]
    assert host._stt_live_preview == ("", 0.0, "")


def test_le_pool_ne_double_jamais_la_connexion_ni_ne_perd_le_tour():
    import core.gemini_transcribe_stt as stt

    started, closed = [], []

    class Turn:
        def __init__(self, host, on_partial):
            self.pcm = bytearray()
            self.preview = ""

        async def start(self):
            await asyncio.sleep(0)
            started.append(self)

        async def begin(self, on_partial):
            on_partial("Ouvre Firefox.")

        async def feed(self, data):
            self.pcm.extend(data)

        async def finish(self):
            return "Ouvre Firefox."

        async def close(self):
            closed.append(self)

    accepted = []

    async def accept(host, text, *args, **kwargs):
        accepted.append(text)
        return True

    import pytest as _pytest
    mp = _pytest.MonkeyPatch()
    mp.setattr(stt, "accept_transcript", accept)
    mp.setattr(stt, "input_allowed", lambda *args: True)
    try:
        host = SimpleNamespace(
            out_queue=asyncio.Queue(), ui=SimpleNamespace(write_log=lambda *_: None, muted=False,
                                                          set_user_transcript=lambda *a, **k: None),
            _speech_output_epoch=0, _audio_drop_count=0, _precision_turn_text="",
            _last_voice_evidence_ms=300.0,
        )

        async def scenario():
            task = asyncio.create_task(stt.run_gemini_transcribe(host, turn_factory=Turn))
            # « start » arrive pendant le chauffage : on attend, on ne double pas.
            await host.out_queue.put({"activity": "start", "_audio_epoch": 0, "turn_id": "t1"})
            await host.out_queue.put({"data": b"\x00\x01" * 4800})
            await host.out_queue.put({"activity": "end", "_voice_evidence_ms": 300.0})
            for _ in range(50):
                if accepted:
                    break
                await asyncio.sleep(0)
            task.cancel()
            with _pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())
    finally:
        mp.undo()
    assert accepted == ["Ouvre Firefox."]
    assert len(started) == 1
    assert closed == started
