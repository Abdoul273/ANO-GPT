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
"""
from __future__ import annotations

import asyncio
import os
import time

from core.gemini_transcribe_stt import GeminiTranscribeError, GeminiTranscribeTurn, _StreamPool

_QUEUE_MAX = 600  # ~40 s de PCM par blocs de 64 ms : jamais atteint en pratique


def captions_enabled() -> bool:
    return os.environ.get("ANOGPT_LIVE_CAPTIONS", "").strip().lower() not in {"0", "false", "no", "off"}


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

    # ── Côté producteur (lecteur PCM → Gemini Live) ─────────────────────────
    def start(self) -> None:
        if not self.enabled or self._worker is not None:
            return
        self._pool = _StreamPool(self.host, self._factory)
        self._pool.warm()
        self._worker = asyncio.create_task(self._run(), name="live-captions")

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
            await self._abort_turn()
            self.host._stt_live_preview = ("", 0.0, "")
            return
        if marker == "end":
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
        if marker is None and self._turn is not None and msg.get("data"):
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
