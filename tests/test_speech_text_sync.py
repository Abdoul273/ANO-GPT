"""Sous-titres : le texte doit rattraper une transcription Gemini tardive."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from core.audio_engine import AudioEngine


def _host(*, played: float = 0.0):
    logs: list[str] = []
    return SimpleNamespace(
        speech_text_queue=asyncio.Queue(),
        _audio_played_sec=played,
        _audio_output_latency=0.06,
        _speech_display_open=False,
        _asst_name="ANO-GPT",
        ui=SimpleNamespace(write_log=logs.append),
        logs=logs,
    )


def test_transcription_tardive_rattrape_huit_groupes_par_cycle():
    host = _host(played=3.0)
    for index in range(10):
        host.speech_text_queue.put_nowait((index * 0.1, f"mot {index}"))

    AudioEngine._flush_synced_speech_text(host)

    assert len(host.logs) == 8
    assert host.logs[0] == "[INLINE_START]ANO-GPT: mot 0"
    assert host.speech_text_queue.qsize() == 2


def test_ne_montre_jamais_un_groupe_avant_son_audio():
    host = _host(played=0.30)  # 240 ms réellement entendues après latence
    host.speech_text_queue.put_nowait((0.20, "déjà entendu"))
    host.speech_text_queue.put_nowait((0.50, "pas encore"))

    AudioEngine._flush_synced_speech_text(host)

    assert host.logs == ["[INLINE_START]ANO-GPT: déjà entendu"]
    assert host.speech_text_queue.qsize() == 1
