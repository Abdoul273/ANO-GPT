"""Régression : `interrupt()` ne doit pas armer le rejet hors tour du modèle.

Bug corrigé ici : `_is_speaking` reste vrai tant que la file de lecture locale
se vide, donc bien après le `turn_complete` du modèle. Parler sur la fin d'une
réponse déclenchait `interrupt()` alors qu'aucun tour n'était en vol, et
`_interrupted` — qui n'est remis à False qu'au `turn_complete` suivant —
faisait jeter la réponse SUIVANTE, la vraie. Son `turn_complete` partait alors
dans un `continue` qui saute l'exécution des outils.

Symptôme vécu : la phrase s'affiche bien dans le panneau, puis rien ne se passe.
"""

import asyncio
import contextlib
import queue
import threading
import types

import pytest

import main
from main import JarvisLive, _resume_would_replay_turn


def test_reprise_recente_ne_peut_pas_rejouer_le_briefing():
    assert _resume_would_replay_turn(
        model_turn_active=False,
        audio_turn_pending=False,
        audio_playing=False,
        audio_queued=False,
        last_turn_complete_at=100.0,
        now=105.0,
    ) is True


def test_reprise_inactive_ancienne_peut_conserver_le_contexte():
    assert _resume_would_replay_turn(
        model_turn_active=False,
        audio_turn_pending=False,
        audio_playing=False,
        audio_queued=False,
        last_turn_complete_at=100.0,
        now=130.0,
    ) is False


def test_reconnexion_doutils_jette_une_reprise_recente_ambiguë():
    """Les outils changent : le contexte local est plus sûr qu'une poignée Live."""
    assert _resume_would_replay_turn(
        model_turn_active=False,
        audio_turn_pending=False,
        audio_playing=False,
        audio_queued=False,
        last_turn_complete_at=100.0,
        now=105.0,
    ) is True


class _StubUI:
    def __init__(self):
        self.logs = []

    def write_log(self, msg):
        self.logs.append(msg)


class _StateUI(_StubUI):
    def __init__(self):
        super().__init__()
        self.states = []
        self.muted = False

    def set_state(self, state):
        self.states.append(state)


class _StubJarvis:
    """Porte le strict minimum utilisé par `interrupt()`."""

    def __init__(self, *, model_turn_active):
        self._model_turn_active = model_turn_active
        self._interrupted = False
        self.audio_in_queue = queue.Queue()
        self._turn_done_event = None
        self.ui = _StubUI()
        self.speaking_calls = []
        # File de texte parlé, synchronisée avec la lecture audio.
        self.speech_text_queue = queue.Queue()
        self._speech_display_open = False
        self._speech_next_text_at = 0.0
        self._audio_turn_active = False
        self._audio_enqueued_sec = 0.0
        self._audio_played_sec = 0.0
        self._speech_last_target_sec = 0.0

    def set_speaking(self, value):
        self.speaking_calls.append(value)

    # Les méthodes réelles, liées à ce stub.
    interrupt = JarvisLive.interrupt
    discard_model_audio = JarvisLive.discard_model_audio
    _end_discarded_turn = JarvisLive._end_discarded_turn
    _clear_interrupted = JarvisLive._clear_interrupted
    _DISCARD_TURN_MAX_S = JarvisLive._DISCARD_TURN_MAX_S
    _drain_spoken_text_queue = JarvisLive._drain_spoken_text_queue
    _reset_speech_sync = JarvisLive._reset_speech_sync


def test_reparler_apres_stop_ne_fait_pas_reprendre_la_reponse_coupee():
    """Symptôme vécu : clic Arrêter, puis un mot ou un bruit rouvre la porte
    micro (`_clear_interrupted`) et la réponse coupée repart en plein milieu,
    bouton Arrêter réaffiché. Le rejet de l'audio du tour coupé doit survivre
    à la réouverture de la porte, jusqu'au turn_complete de CE tour."""
    j = _StubJarvis(model_turn_active=True)
    j.interrupt()
    assert j._interrupted is True and j.discard_model_audio() is True

    j._clear_interrupted()          # l'utilisateur reparle
    assert j._interrupted is False
    assert j.discard_model_audio() is True, "la suite du tour coupé doit rester muette"

    j._end_discarded_turn()         # turn_complete du tour coupé
    assert j.discard_model_audio() is False


def test_le_rejet_du_tour_coupe_expire_sans_turn_complete():
    j = _StubJarvis(model_turn_active=True)
    j.interrupt()
    j._clear_interrupted()
    j._discard_turn_audio_since -= j._DISCARD_TURN_MAX_S + 1.0
    assert j.discard_model_audio() is False, "un tour sans fin explicite ne doit pas rendre le suivant muet"


def test_stop_hors_tour_narme_pas_le_rejet_du_tour():
    j = _StubJarvis(model_turn_active=False)
    j.interrupt()
    assert j.discard_model_audio() is False


def test_barge_in_pendant_un_tour_arme_le_rejet():
    j = _StubJarvis(model_turn_active=True)
    j.audio_in_queue.put(b"tail")

    j.interrupt()

    assert j._interrupted is True, "un vrai barge-in doit jeter la suite du tour"
    assert j.audio_in_queue.empty(), "l'audio en attente doit être purgé"
    assert j.speaking_calls == [False]


def test_parler_sur_la_fin_de_lecture_narme_pas_le_rejet():
    # Le modèle a fini son tour ; seule la file locale se vide encore.
    j = _StubJarvis(model_turn_active=False)
    j.audio_in_queue.put(b"tail")

    j.interrupt()

    assert j._interrupted is False, (
        "hors tour du modèle, armer le rejet ferait jeter la réponse suivante"
    )
    # La coupure de la lecture en cours, elle, doit rester immédiate.
    assert j.audio_in_queue.empty()
    assert j.speaking_calls == [False]


def test_le_rejet_ne_survit_pas_a_la_fin_du_tour():
    """`_interrupted` doit être relâché par le turn_complete du tour interrompu,
    sans quoi il fuit sur le tour suivant."""
    j = _StubJarvis(model_turn_active=True)
    j.interrupt()
    assert j._interrupted is True

    # Ce que fait `_receive_audio` en recevant turn_complete.
    j._model_turn_active = False
    j._interrupted = False

    # Un nouveau barge-in hors tour ne doit plus rien armer.
    j.interrupt()
    assert j._interrupted is False


def test_interrupt_signale_la_coupure_sans_toucher_portaudio_concurremment():
    class _Stream:
        def __init__(self):
            self.aborts = 0

        def abort(self):
            self.aborts += 1

    j = _StubJarvis(model_turn_active=True)
    stream = _Stream()
    j._audio_abort_event = threading.Event()
    j._output_stream_lock = threading.Lock()
    j._active_output_stream = stream

    j.interrupt()

    assert j._audio_abort_event.is_set()
    assert stream.aborts == 0


def test_le_budget_de_coupure_audio_reste_sous_100_ms():
    """Une tranche en vol + le tampon demandé doivent rester sous la cible."""
    assert main._OUTPUT_SLICE_MS / 1000 + main._OUTPUT_LATENCY_S < 0.1


@pytest.mark.parametrize("interrupt_prefill", [False, True])
def test_prefill_absorbe_les_rafales_et_reste_interruptible(monkeypatch, interrupt_prefill):
    writes = []
    monkeypatch.setattr("core.spatial_audio.spatial_audio_requested", lambda: False)
    class Stream:
        def start(self): pass
        def stop(self): pass
        def abort(self): pass
        def close(self): pass
        def write(self, chunk):
            writes.append(chunk)
            return False
    monkeypatch.setattr(main, "sd", types.SimpleNamespace(RawOutputStream=lambda **kw: Stream()))

    async def scenario():
        j = JarvisLive.__new__(JarvisLive)
        j.ui = _StateUI()
        j._is_speaking = False
        j._speaking_lock = threading.Lock()
        j._audio_abort_event = threading.Event()
        j.audio_in_queue = asyncio.Queue()
        j.speech_text_queue = asyncio.Queue()
        j._turn_done_event = asyncio.Event()
        j._speech_display_open = False
        j._audio_played_sec = 0.0
        j._flush_synced_speech_text = lambda **kw: None
        chunks = [bytes([i, 0]) * 480 for i in range(1, 5)]
        j.audio_in_queue.put_nowait(chunks[0])
        task = asyncio.create_task(j._play_audio())
        try:
            await asyncio.sleep(.015)
            assert not writes
            if interrupt_prefill:
                j._audio_abort_event.set()
                await asyncio.sleep(.03)
                assert not writes
            else:
                for chunk in chunks[1:]:
                    j.audio_in_queue.put_nowait(chunk)
                for _ in range(100):
                    if len(writes) == 4:
                        break
                    await asyncio.sleep(.005)
                assert writes == chunks
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    asyncio.run(scenario())


def test_la_prosodie_acoustique_est_mise_avant_la_fin_du_tour(monkeypatch):
    class Manager:
        def live_turn_instruction(self, *_args, **_kwargs):
            return "directive locale", types.SimpleNamespace(mode="urgent")

    monkeypatch.setattr("core.prosody.get_prosody_manager", lambda: Manager())
    j = JarvisLive.__new__(JarvisLive)
    j._activity_open = True
    j._activity_since = main.time.monotonic() - 1
    j._voice_evidence_ms = 500
    j._last_voice_evidence_ms = 0
    j._last_voice_audio_ms = 0
    j._voice_chunks = [main.np.ones(1600, dtype=main.np.float32) * 0.1]
    j._ambient_fields = {"Fenêtre": "kitty"}
    j._prosody_mode = ""
    j.out_queue = asyncio.Queue(maxsize=8)
    j._check_speaker = lambda: None

    j._activity_end()

    queued = list(j.out_queue._queue)
    # Chaque marqueur porte désormais l'identité de son tour : on vérifie le
    # sens (la prosodie précède la fin), pas la forme exacte du dictionnaire.
    assert queued[-2]["activity"] == "prosody"
    assert queued[-2]["text"] == "directive locale"
    assert queued[-1]["activity"] == "end"
    assert queued[-1]["_voice_evidence_ms"] == 500
    assert j._prosody_mode == "urgent"


def test_interrupt_nannule_pas_la_tache_outil_porteuse_de_session():
    class _Task:
        def __init__(self):
            self.cancelled = False

        def done(self):
            return False

        def cancel(self):
            self.cancelled = True

    j = _StubJarvis(model_turn_active=True)
    task = _Task()
    j._active_tool_task = task

    j.interrupt()

    assert task.cancelled is False


def test_un_marqueur_de_fin_ne_jette_jamais_le_marqueur_de_debut():
    j = JarvisLive.__new__(JarvisLive)
    j.out_queue = asyncio.Queue(maxsize=2)
    j.out_queue.put_nowait({"activity": "start"})
    j.out_queue.put_nowait({"data": b"audio"})

    JarvisLive._enqueue_out(j, {"activity": "end"})

    assert [msg["activity"] for msg in j.out_queue._queue] == ["start", "end"]


def test_le_pcm_temps_reel_part_directement_vers_gemini_live():
    class Session:
        def __init__(self):
            self.calls = []

        async def send_realtime_input(self, **kwargs):
            self.calls.append(kwargs)

    async def scenario():
        j = JarvisLive.__new__(JarvisLive)
        j.out_queue = asyncio.Queue()
        j.session = Session()
        j.out_queue.put_nowait({
            "data": b"\x01\x02",
            "mime_type": "audio/pcm;rate=16000",
        })
        j.ui = types.SimpleNamespace(write_log=lambda _text: None)
        task = asyncio.create_task(j._send_realtime())
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return j.session.calls

    calls = asyncio.run(scenario())
    assert calls == [{"audio": {
        "data": b"\x01\x02", "mime_type": "audio/pcm;rate=16000",
    }}]


def test_deux_tours_texte_attendent_leur_fin_au_lieu_de_se_superposer():
    class Session:
        def __init__(self):
            self.calls = []

        async def send_realtime_input(self, **kwargs):
            self.calls.append(kwargs)

    async def scenario():
        j = JarvisLive.__new__(JarvisLive)
        j.session = Session()
        j._turn_done_event = asyncio.Event()
        j._turn_submit_lock = asyncio.Lock()

        first = asyncio.create_task(j._submit_text_turn("briefing"))
        await asyncio.sleep(0)
        second = asyncio.create_task(j._submit_text_turn("salut"))
        await asyncio.sleep(0)
        assert len(j.session.calls) == 1

        j._turn_done_event.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(j.session.calls) == 2

        j._turn_done_event.set()
        await asyncio.gather(first, second)
        return j.session.calls

    calls = asyncio.run(scenario())
    assert calls[0]["text"] == "briefing"
    assert calls[1]["text"] == "salut"


def test_un_tour_texte_attend_la_fin_du_tour_vocal():
    class Session:
        def __init__(self):
            self.calls = []

        async def send_realtime_input(self, **kwargs):
            self.calls.append(kwargs)

    async def scenario():
        j = JarvisLive.__new__(JarvisLive)
        j.session = Session()
        j._turn_done_event = asyncio.Event()
        j._turn_submit_lock = asyncio.Lock()
        j._audio_turn_pending = True

        task = asyncio.create_task(j._submit_text_turn("après la voix"))
        await asyncio.sleep(0)
        assert j.session.calls == []

        j._audio_turn_pending = False
        j._turn_done_event.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(j.session.calls) == 1

        j._turn_done_event.set()
        await task

    asyncio.run(scenario())


def test_un_tour_vocal_bloque_ne_perd_plus_la_commande_texte():
    class Session:
        def __init__(self):
            self.calls = []

        async def send_realtime_input(self, **kwargs):
            self.calls.append(kwargs)

    async def scenario():
        j = JarvisLive.__new__(JarvisLive)
        j.session = Session()
        j.ui = _StubUI()
        j._turn_done_event = asyncio.Event()
        j._turn_submit_lock = asyncio.Lock()
        j._audio_turn_pending = True
        j._activity_open = False
        j._interrupted = False
        j._deferred_voice_note = ""
        j._deferred_context = ""
        j._live_send_trace = []

        task = asyncio.create_task(
            j._submit_text_turn(
                "connecte Gmail", timeout_s=1.0, pending_timeout_s=0.01
            )
        )
        await asyncio.sleep(0.03)

        assert j.session.calls == [{"text": "connecte Gmail"}]
        assert j._audio_turn_pending is False
        assert any("commande texte envoyée" in line for line in j.ui.logs)

        j._turn_done_event.set()
        assert await task is True

    asyncio.run(scenario())


def test_les_tranches_pcm_ne_saturent_pas_la_file_detat_ui():
    j = JarvisLive.__new__(JarvisLive)
    j.ui = _StateUI()
    j._is_speaking = False
    j._speaking_lock = __import__("threading").Lock()

    for _ in range(50):
        JarvisLive.set_speaking(j, True)

    assert j.ui.states == ["SPEAKING"]
    JarvisLive.set_speaking(j, False)
    assert j.ui.states[-1] == "LISTENING"


def test_un_arret_audio_deja_signale_ne_rearme_pas_lecoute_continue():
    class Continuous:
        def __init__(self):
            self.ends = 0

        def on_assistant_speech_end(self, loop=None):
            self.ends += 1

    j = JarvisLive.__new__(JarvisLive)
    j.ui = _StateUI()
    j._is_speaking = False
    j._speaking_lock = __import__("threading").Lock()
    j._continuous = Continuous()
    j._loop = None

    JarvisLive.set_speaking(j, False)
    JarvisLive.set_speaking(j, False)

    assert j._continuous.ends == 0
    assert j.ui.states == []


def test_un_hotplug_rejoue_le_bloc_pcm_au_lieu_de_couper_un_mot(monkeypatch):
    streams = []

    class FakeStream:
        def __init__(self, fail=False):
            self.fail = fail
            self.writes = []

        def start(self):
            return None

        def write(self, chunk):
            self.writes.append(chunk)
            if self.fail:
                self.fail = False
                raise RuntimeError("device unplugged")
            return False

        def stop(self):
            return None

        def abort(self):
            return None

        def close(self):
            return None

    def make_stream(**_kwargs):
        stream = FakeStream(fail=not streams)
        streams.append(stream)
        return stream

    monkeypatch.setattr(
        main,
        "sd",
        types.SimpleNamespace(RawOutputStream=make_stream),
    )

    async def scenario():
        j = JarvisLive.__new__(JarvisLive)
        j.ui = _StateUI()
        j._is_speaking = False
        j._speaking_lock = __import__("threading").Lock()
        j.audio_in_queue = asyncio.Queue(maxsize=4)
        j.speech_text_queue = asyncio.Queue()
        j._turn_done_event = asyncio.Event()
        j._turn_done_event.set()
        j._speech_display_open = False
        j._speech_next_text_at = 0.0
        j._audio_turn_active = True
        j._audio_enqueued_sec = 0.05
        j._audio_played_sec = 0.0
        j._speech_last_target_sec = 0.0
        j._asst_name = "ANO-GPT"
        pcm = b"\x01\x00" * 1200
        await j.audio_in_queue.put(pcm)

        task = asyncio.create_task(j._play_audio())
        for _ in range(100):
            if len(streams) >= 2 and streams[1].writes:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert streams[0].writes == [pcm]
        assert streams[1].writes == [pcm]

    asyncio.run(scenario())


def test_une_erreur_portaudio_pendant_la_coupure_ne_ferme_pas_lapp(monkeypatch):
    write_started = threading.Event()
    release_write = threading.Event()

    class PortAudioAbortError(Exception):
        pass

    class FakeStream:
        def start(self):
            return None

        def write(self, _chunk):
            write_started.set()
            release_write.wait(2.0)
            raise PortAudioAbortError("stream intentionally interrupted")

        def stop(self):
            return None

        def abort(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr(
        main, "sd", types.SimpleNamespace(RawOutputStream=lambda **_kw: FakeStream())
    )

    async def scenario():
        j = JarvisLive.__new__(JarvisLive)
        j.ui = _StateUI()
        j._is_speaking = False
        j._speaking_lock = threading.Lock()
        j._audio_abort_event = threading.Event()
        j.audio_in_queue = asyncio.Queue(maxsize=4)
        j.speech_text_queue = asyncio.Queue()
        j._turn_done_event = asyncio.Event()
        j._speech_display_open = False
        j._speech_next_text_at = 0.0
        j._audio_turn_active = True
        j._audio_enqueued_sec = 0.05
        j._audio_played_sec = 0.0
        j._speech_last_target_sec = 0.0
        j._asst_name = "ANO-GPT"
        await j.audio_in_queue.put(b"\x01\x00" * 1200)

        task = asyncio.create_task(j._play_audio())
        assert await asyncio.to_thread(write_started.wait, 2.0)
        j._audio_abort_event.set()
        release_write.set()
        await asyncio.sleep(0.15)
        assert not task.done(), "la panne de coupure a fermé la tâche audio"

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_spatial_initialization_keeps_event_loop_responsive(monkeypatch):
    from core import spatial_audio

    started, release = threading.Event(), threading.Event()

    def slow_device_detection():
        started.set()
        assert release.wait(2)
        return False

    monkeypatch.setattr(spatial_audio, "spatial_audio_requested", slow_device_detection)

    async def scenario():
        host = types.SimpleNamespace(
            audio_in_queue=asyncio.Queue(),
            set_speaking=lambda value: None,
            reset_audio_and_turn_state=lambda reason: None,
        )
        task = asyncio.create_task(JarvisLive._play_audio(host))
        try:
            assert await asyncio.to_thread(started.wait, 1)
            # Le worker attend encore : la boucle doit pouvoir annuler l'audio.
            assert not release.is_set()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, .5)
        finally:
            release.set()
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    asyncio.run(scenario())


def test_interrupt_pendant_reflexion_arme_le_rejet_et_remet_en_ecoute():
    j = _StubJarvis(model_turn_active=False)
    j.ui = _StateUI()
    j._is_thinking = True

    j.interrupt()

    assert j._interrupted is True, "interrompre pendant la réflexion doit armer le rejet"
    assert j._is_thinking is False
    assert j.ui.states[-1] == "LISTENING", "l'UI doit repasser immédiatement en écoute"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
