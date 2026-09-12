"""Régressions du relais vocal ANO Remote vers la session Live."""

import asyncio
import contextlib
import threading
import types

import numpy as np

from main import JarvisLive


class _RelayJarvis:
    _relay_phone_audio = JarvisLive._relay_phone_audio

    def __init__(self, *, muted: bool, speaking: bool = False):
        self._dashboard = types.SimpleNamespace(_phone_audio_queue=asyncio.Queue())
        self.ui = types.SimpleNamespace(muted=muted)
        self._phone_active = False
        self._speaking_lock = threading.Lock()
        self._is_speaking = speaking
        self._model_turn_active = False
        self._is_thinking = False
        self._activity_open = False
        self.sent = []
        self.starts = 0
        self.ends = 0
        self.interrupts = 0

    def _enqueue_out(self, message):
        self.sent.append(message)

    def _activity_start(self):
        self.starts += 1
        self._activity_open = True

    def _activity_end(self):
        self.ends += 1
        self._activity_open = False

    def interrupt(self):
        self.interrupts += 1
        self._is_speaking = False


def _pcm(level: float = 0.25) -> dict:
    samples = np.full(1024, int(level * 32767), dtype=np.int16)
    return {"data": samples.tobytes(), "mime_type": "audio/pcm;rate=16000"}


async def _run_packets(jarvis, packets):
    task = asyncio.create_task(jarvis._relay_phone_audio())
    try:
        for packet in packets:
            await jarvis._dashboard._phone_audio_queue.put(packet)
        for _ in range(20):
            if jarvis._dashboard._phone_audio_queue.empty():
                await asyncio.sleep(0)
                break
            await asyncio.sleep(0)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def test_phone_reste_audible_quand_le_micro_pc_est_coupe():
    jarvis = _RelayJarvis(muted=True)

    asyncio.run(_run_packets(jarvis, [_pcm(), _pcm(), {"activity": "phone_stream_end"}]))

    assert jarvis.starts == 1
    assert jarvis.ends == 1
    assert len(jarvis.sent) == 2
    assert jarvis.ui.muted is True, "le relais ne doit pas rallumer le micro du PC"


def test_le_telephone_ne_transmet_pas_lecho_et_ninterrompt_pas_sur_un_bruit():
    jarvis = _RelayJarvis(muted=True, speaking=True)

    asyncio.run(_run_packets(jarvis, [_pcm(), _pcm(), {"activity": "phone_stream_end"}]))

    assert jarvis.interrupts == 0
    assert jarvis.starts == 0
    assert jarvis.ends == 0
    assert jarvis.sent == []


def test_une_voix_android_faible_est_quand_meme_envoyee_au_modele():
    """Le VAD Live, pas le seuil visuel local, décide de la parole.

    Plusieurs téléphones avec AGC matériel livrent une voix utile sous 0,01
    RMS. La laisser tomber avant Gemini faisait paraître ANO Remote muet.
    """
    jarvis = _RelayJarvis(muted=True)

    asyncio.run(_run_packets(
        jarvis, [_pcm(0.004), _pcm(0.004), {"activity": "phone_stream_end"}]
    ))

    assert jarvis.starts == 0  # pas assez fort pour l'indicateur local
    assert len(jarvis.sent) == 2  # mais le PCM atteint bien Gemini Live


def test_le_telephone_clot_le_tour_pc_avant_de_prendre_la_main():
    jarvis = _RelayJarvis(muted=False)
    jarvis._activity_open = True

    asyncio.run(_run_packets(jarvis, [_pcm(), _pcm(), {"activity": "phone_stream_end"}]))

    assert jarvis.starts == 1
    assert jarvis.ends == 2  # ancien tour PC, puis tour téléphone
    assert len(jarvis.sent) == 2
