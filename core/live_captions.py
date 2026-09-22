"""Sous-titres instantanés pendant que l'utilisateur parle.

Le pipeline Mark-LII envoie le PCM brut à Gemini Live, dont la transcription
d'entrée n'arrive souvent qu'à la fin du tour, avec la réponse : à l'écran,
la phrase « apparaît » en retard. Ce module ouvre en parallèle un flux
Gemini Transcribe (session pré-chauffée, réutilisée entre les phrases) qui
reçoit le même PCM et renvoie des hypothèses mot à mot.

Règles :
- affichage seulement : rien de ce flux n'est jamais exécuté ni envoyé au
  modèle conversationnel ; la transcription de Gemini Live reste la source de
  vérité et remplace les sous-titres dès qu'elle arrive ;
- jamais bloquant : le lecteur qui alimente Gemini Live dépose les messages
  dans une file et continue, un souci réseau côté sous-titres ne retarde
  jamais la voix ;
- l'aperçu et son horodatage sont publiés sur l'hôte (`_stt_live_preview`)
  pour la fin de phrase sémantique du callback micro.

Segmentation :
- le téléphone (`core/phone_relay.py`) a son propre VAD et pousse des
  marqueurs `activity: start/end/cancel` explicites — on les suit tels quels ;
- le callback micro PC (Mark-LII, `core/audio_engine.py:_listen_audio`) ne
  pousse QUE du PCM continu, volontairement : aucun traitement audio dans le
  thread PortAudio. Sans marqueur, ce module restait donc silencieux sur la
  voie PC alors qu'il préchauffait quand même un flux Gemini Transcribe pour
  rien. `_segment_pc_audio` fait cette segmentation localement, sur le PCM
  déjà reçu ici — jamais dans le thread audio.
"""
from __future__ import annotations

import asyncio
import time
import uuid

import numpy as np

from core.gemini_transcribe_stt import GeminiTranscribeError, GeminiTranscribeTurn, _StreamPool
from core.live_speech_config import live_captions_provider

_QUEUE_MAX = 600  # ~40 s de PCM par blocs de 64 ms : jamais atteint en pratique
_LOCAL_ATTACK_MS = 90.0       # voix consécutive avant d'ouvrir un tour local
_LOCAL_SILENCE_CLOSE_S = 0.7  # sans nouvelle voix depuis ce délai, le tour finit
_LOCAL_WATCHDOG_S = 0.2


def captions_enabled(config: dict | None = None) -> bool:
    """Les sous-titres normaux viennent de Gemini Live.

    Le flux Gemini 3.5 reste disponible seulement sur opt-in explicite pour
    diagnostiquer une transcription ; il ne doit jamais accompagner une
    conversation courante.
    """
    if config is None:
        try:
            from config import get_config
            config = get_config()
        except Exception:
            config = {}
    return live_captions_provider(config) == "gemini_transcribe"


class LiveCaptions:
    def __init__(self, host, turn_factory=GeminiTranscribeTurn):
        self.host = host
        self.enabled = captions_enabled() and bool(getattr(host, "_gemini_api_key", ""))
        self._factory = turn_factory
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)
        self._worker: asyncio.Task | None = None
        self._pool: _StreamPool | None = None
        self._turn = None
        self._turn_id = ""
        self._live_seen = False
        self._dropped = 0
        # Segmentation locale de la voie PC (pas de marqueur de la capture).
        self._local_owns_turn = False
        self._local_attack_ms = 0.0
        self._local_noise_floor = 0.003
        self._local_last_voice_at = 0.0
        self._local_close_queued = False
        self._watchdog: asyncio.Task | None = None

    # ── Côté producteur (lecteur PCM → Gemini Live) ─────────────────────────
    def start(self) -> None:
        if not self.enabled or self._worker is not None:
            return
        self._pool = _StreamPool(self.host, self._factory)
        self._pool.warm()
        self._worker = asyncio.create_task(self._run(), name="live-captions")
        self._watchdog = asyncio.create_task(self._watch_local_silence(), name="live-captions-vad")

    def push(self, msg: dict) -> None:
        """Dépose sans jamais bloquer ; l'audio en trop est sacrifié, jamais
        un marqueur de tour."""
        if not self.enabled or self._worker is None:
            return
        try:
            self._queue.put_nowait(msg)
        except asyncio.QueueFull:
            self._dropped += 1
            if msg.get("activity"):
                try:
                    self._queue.get_nowait()
                    self._queue.put_nowait(msg)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    def live_transcript_seen(self) -> None:
        """Gemini Live a affiché sa propre transcription : les sous-titres du
        tour en cours s'effacent devant elle."""
        self._live_seen = True

    async def close(self) -> None:
        watchdog, self._watchdog = self._watchdog, None
        if watchdog is not None:
            watchdog.cancel()
            try:
                await watchdog
            except (asyncio.CancelledError, Exception):
                pass
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.cancel()
            try:
                await worker
            except (asyncio.CancelledError, Exception):
                pass
        turn, self._turn = self._turn, None
        if turn is not None:
            try:
                await turn.abort()
            except Exception as exc:
                print(f"[Sous-titres] abandon du tour à la fermeture : {exc}")
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    # ── Côté consommateur ───────────────────────────────────────────────────
    def _display(self, text: str, final: bool = False) -> None:
        ui = getattr(self.host, "ui", None)
        if ui is None or self._live_seen:
            return
        try:
            ui.set_user_transcript(text, final=final, turn_id=self._turn_id)
        except TypeError:
            ui.set_user_transcript(text, final=final)

    def _partial(self, text: str) -> None:
        self.host._stt_live_preview = (str(text or ""), time.monotonic(), self._turn_id)
        if text:
            self._display(text)

    async def _run(self) -> None:
        while True:
            msg = await self._queue.get()
            try:
                await self._handle(msg)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Les sous-titres sont un confort : on abandonne ce tour et on
                # rouvre une session propre pour le suivant.
                if not isinstance(exc, GeminiTranscribeError):
                    print(f"[Sous-titres] {type(exc).__name__}: {exc}")
                turn, self._turn = self._turn, None
                self.host._stt_live_preview = ("", 0.0, "")
                if turn is not None and self._pool is not None:
                    await self._pool.discard(turn)

    async def _handle(self, msg: dict) -> None:
        marker = msg.get("activity") if isinstance(msg, dict) else None
        if marker == "start":
            self._local_owns_turn = False
            self._local_close_queued = False
            await self._abort_turn()
            self._turn_id = str(msg.get("turn_id") or "")
            self._live_seen = False
            self.host._stt_live_preview = ("", time.monotonic(), self._turn_id)
            assert self._pool is not None
            stream = await self._pool.acquire()
            try:
                await stream.begin(self._partial)
            except Exception:
                await self._pool.discard(stream)
                stream = await self._pool.acquire(fresh=True)
                await stream.begin(self._partial)
            self._turn = stream
            return
        if marker == "cancel":
            self._local_owns_turn = False
            self._local_close_queued = False
            await self._abort_turn()
            self.host._stt_live_preview = ("", 0.0, "")
            return
        if marker == "end":
            self._local_owns_turn = False
            self._local_close_queued = False
            turn, self._turn = self._turn, None
            if turn is None:
                return
            text = await turn.finish()
            self.host._stt_live_preview = ("", 0.0, "")
            if text:
                # Confirmé par Transcribe, mais Live reste juge : `final=False`
                # laisse sa transcription remplacer la nôtre sans heurt.
                self._display(text, final=False)
            if self._pool is not None:
                self._pool.rotate_if_stale()
            return
        if marker is None and msg.get("data"):
            # La capture PC Mark-LII ne crée volontairement pas de marqueurs
            # dans son callback PortAudio. Les construire ici garde le
            # callback minuscule et ne compromet jamais l'envoi Gemini Live.
            if not self._local_owns_turn:
                await self._segment_pc_audio(msg)
            elif self._pc_frame_is_voice(msg):
                self._local_last_voice_at = time.monotonic()
            if self._turn is not None:
                await self._turn.feed(bytes(msg["data"]))

    async def _abort_turn(self) -> None:
        turn, self._turn = self._turn, None
        if turn is None:
            return
        try:
            await turn.abort()
        except Exception as exc:
            print(f"[Sous-titres] abandon du tour : {exc}")
        # `abort` invalide la session côté protocole : la remplacer.
        if self._pool is not None:
            await self._pool.discard(turn)

    def _pc_frame_is_voice(self, msg: dict) -> bool:
        """Porte RMS très légère pour les sous-titres PC uniquement.

        Ce n'est pas le VAD du dialogue : Gemini Live reste l'autorité. Son
        seul rôle est d'éviter d'ouvrir un second flux Transcribe au bruit de
        fond. Les données sont déjà hors du callback PortAudio à ce stade.
        """
        data = msg.get("data")
        if not isinstance(data, (bytes, bytearray, memoryview)) or len(data) < 2:
            return False
        pcm = np.frombuffer(data, dtype="<i2")
        if not pcm.size:
            return False
        values = pcm.astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(values * values)))
        gate = max(0.010, self._local_noise_floor * 2.8)
        voiced = rms >= gate
        if not voiced:
            self._local_noise_floor = 0.96 * self._local_noise_floor + 0.04 * min(rms, 0.02)
        return voiced

    async def _segment_pc_audio(self, msg: dict) -> None:
        """Ouvre un tour Transcribe après une courte preuve de voix PC."""
        data = msg.get("data", b"")
        duration_ms = len(data) / 32.0  # PCM16 mono 16 kHz
        if not self._pc_frame_is_voice(msg):
            self._local_attack_ms = 0.0
            return
        self._local_attack_ms += duration_ms
        if self._local_attack_ms < _LOCAL_ATTACK_MS:
            return
        self._local_attack_ms = 0.0
        self._local_owns_turn = True
        self._local_close_queued = False
        self._local_last_voice_at = time.monotonic()
        await self._handle({"activity": "start", "turn_id": uuid.uuid4().hex})
        # `_handle(start)` remet ce drapeau à False car les marqueurs externes
        # (téléphone) lui appartiennent aussi. Le rétablir est local au PC.
        self._local_owns_turn = True

    async def _watch_local_silence(self) -> None:
        """Clôt le tour PC même si le flux PCM reste silencieux en continu."""
        try:
            while True:
                await asyncio.sleep(_LOCAL_WATCHDOG_S)
                if (not self._local_owns_turn or self._local_close_queued
                        or time.monotonic() - self._local_last_voice_at < _LOCAL_SILENCE_CLOSE_S):
                    continue
                self._local_close_queued = True
                # La file conserve l'ordre avec les derniers échantillons.
                self.push({"activity": "end", "turn_id": self._turn_id})
        except asyncio.CancelledError:
            raise
