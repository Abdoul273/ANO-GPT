"""Sous-titres instantanés : affichage seul, jamais bloquant, Live reste juge."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from core.live_captions import LiveCaptions


class _UI:
    def __init__(self):
        self.shown = []
        self.muted = False

    def set_user_transcript(self, text, final=False, turn_id=""):
        self.shown.append((text, final, turn_id))

    def write_log(self, *_):
        pass


class _Turn:
    instances = []

    def __init__(self, host, on_partial):
        self.pcm = bytearray()
        self.on_partial = on_partial
        self.aborted = False
        self.preview = ""
        _Turn.instances.append(self)

    async def start(self):
        return None

    async def begin(self, on_partial):
        self.on_partial = on_partial
        self.on_partial("Ouvre")

    async def feed(self, data):
        self.pcm.extend(data)
        self.on_partial("Ouvre Firefox.")

    async def finish(self):
        return "Ouvre Firefox."

    async def abort(self):
        self.aborted = True

    async def close(self):
        return None


def _host():
    return SimpleNamespace(ui=_UI(), _gemini_api_key="k", _stt_live_preview=("", 0.0, ""))


def test_sans_cle_les_sous_titres_sont_inactifs_et_push_ne_bloque_pas():
    host = SimpleNamespace(ui=_UI())
    captions = LiveCaptions(host, turn_factory=_Turn)
    assert not captions.enabled
    captions.start()
    captions.push({"activity": "start", "turn_id": "t"})
    assert host.ui.shown == []


def test_les_hypotheses_apparaissent_pendant_la_parole(monkeypatch):
    monkeypatch.setattr("core.live_captions.captions_enabled", lambda: True)
    _Turn.instances.clear()
    host = _host()

    async def scenario():
        captions = LiveCaptions(host, turn_factory=_Turn)
        captions.start()
        captions.push({"activity": "start", "turn_id": "t1"})
        captions.push({"data": b"\x00\x01" * 512, "mime_type": "audio/pcm;rate=16000"})
        captions.push({"activity": "end", "turn_id": "t1"})
        for _ in range(40):
            if any(text == "Ouvre Firefox." and tid == "t1" for text, final, tid in host.ui.shown):
                break
            await asyncio.sleep(0)
        await captions.close()

    asyncio.run(scenario())
    assert ("Ouvre", False, "t1") in host.ui.shown
    assert ("Ouvre Firefox.", False, "t1") in host.ui.shown
    # Tout est affiché comme hypothèse : rien n'est « final » côté sous-titres.
    assert all(final is False for _text, final, _tid in host.ui.shown)
    # L'aperçu a été publié pour la fin de phrase sémantique, puis effacé.
    assert host._stt_live_preview == ("", 0.0, "")
    assert len(_Turn.instances) == 1


def test_la_transcription_de_live_fait_taire_les_sous_titres(monkeypatch):
    monkeypatch.setattr("core.live_captions.captions_enabled", lambda: True)
    host = _host()

    async def scenario():
        captions = LiveCaptions(host, turn_factory=_Turn)
        captions.start()
        captions.push({"activity": "start", "turn_id": "t2"})
        for _ in range(20):
            if host.ui.shown:
                break
            await asyncio.sleep(0)
        captions.live_transcript_seen()
        before = len(host.ui.shown)
        captions.push({"data": b"\x00\x01" * 512, "mime_type": "audio/pcm;rate=16000"})
        captions.push({"activity": "end", "turn_id": "t2"})
        for _ in range(20):
            await asyncio.sleep(0)
        await captions.close()
        return before

    before = asyncio.run(scenario())
    assert len(host.ui.shown) == before


def test_un_tour_annule_abandonne_la_session_transcribe(monkeypatch):
    monkeypatch.setattr("core.live_captions.captions_enabled", lambda: True)
    _Turn.instances.clear()
    host = _host()

    async def scenario():
        captions = LiveCaptions(host, turn_factory=_Turn)
        captions.start()
        captions.push({"activity": "start", "turn_id": "t3"})
        captions.push({"activity": "cancel", "turn_id": "t3"})
        for _ in range(20):
            if _Turn.instances and _Turn.instances[0].aborted:
                break
            await asyncio.sleep(0)
        await captions.close()

    asyncio.run(scenario())
    assert _Turn.instances[0].aborted
    assert host._stt_live_preview == ("", 0.0, "")


def test_pcm_pc_ouvre_et_clot_un_tour_sans_marqueur(monkeypatch):
    """Le micro PC Mark-LII livre du PCM continu, sans activity start/end."""
    monkeypatch.setattr("core.live_captions.captions_enabled", lambda: True)
    _Turn.instances.clear()
    host = _host()

    async def scenario():
        captions = LiveCaptions(host, turn_factory=_Turn)
        captions.start()
        # 128 ms de voix : au-delà de l'attaque de 90 ms.
        captions.push({"data": b"\x00\x10" * 2048, "mime_type": "audio/pcm;rate=16000"})
        for _ in range(40):
            if _Turn.instances:
                break
            await asyncio.sleep(0)
        assert _Turn.instances
        await asyncio.sleep(0.9)
        for _ in range(40):
            if not captions._local_owns_turn:
                break
            await asyncio.sleep(0.05)
        await captions.close()

    asyncio.run(scenario())
    assert _Turn.instances[0].pcm


def test_gemini_live_est_le_defaut_et_ne_demarre_pas_transcribe():
    from core.live_captions import captions_enabled

    assert captions_enabled({}) is False
    assert captions_enabled({"live_captions_provider": "gemini_live"}) is False
    assert captions_enabled({"live_captions_provider": "gemini_transcribe"}) is True
