"""Transcription Scribe en français ; seul le texte validé rejoint Gemini."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import time

import aiohttp
import numpy as np

from core.live_speech_config import FRENCH_MAIL_PHRASES, FRENCH_TECH_PHRASES
from core.precision_stt import (
    PrecisionTranscriber,
    should_refine_live_turn,
    transcripts_agree,
)

PARAMETERS = {
    "model_id": "scribe_v2_realtime",
    "audio_format": "pcm_16000",
    "language_code": "fr",
    "commit_strategy": "manual",
    "filter_background_audio": "true",
    # Garder les hésitations et corrections prononcées, sans réécrire la demande.
    "no_verbatim": "false",
}
MAX_PCM_BYTES = 16000 * 2 * 25


class ScribeError(RuntimeError):
    pass


class UncertainTranscript(ScribeError):
    """Énoncé à répéter immédiatement, sans délai de reconnexion."""


class ScribeTurn:
    """Un WebSocket par énoncé : aucune écoute distante pendant la réponse."""

    def __init__(self, settings, on_partial=None):
        self.key = settings.get("elevenlabs_api_key", "")
        self.on_partial = on_partial
        self.client = self.ws = self.reader = None
        self.final = None
        self.pcm = bytearray()
        self.committing = False
        self.segments = []

    async def start(self):
        if not self.key:
            raise ScribeError("Clé ElevenLabs absente.")
        try:
            self.client = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, connect=8))
            self.ws = await self.client.ws_connect(
                "wss://api.elevenlabs.io/v1/speech-to-text/realtime",
                params=PARAMETERS, headers={"xi-api-key": self.key},
                max_msg_size=1024 * 1024,
            )
            first = await asyncio.wait_for(self.ws.receive_json(), 8)
            if first.get("message_type") != "session_started":
                raise ScribeError("Scribe refuse la connexion : vérifiez la permission speech_to_text et les crédits.")
            self.final = asyncio.get_running_loop().create_future()
            self.reader = asyncio.create_task(self._read(), name="scribe-transcripts")
        except ScribeError:
            await self.close()
            raise
        except Exception:
            await self.close()
            raise ScribeError("Connexion Scribe indisponible.") from None

    async def _read(self):
        try:
            async for message in self.ws:
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                import json
                item = json.loads(message.data)
                kind = item.get("message_type", "")
                text = str(item.get("text") or "").strip()
                if kind == "partial_transcript" and self.on_partial:
                    self.on_partial(text)
                elif kind == "committed_transcript":
                    if text:
                        self.segments.append(text)
                    if self.committing and not self.final.done():
                        self.final.set_result(" ".join(self.segments))
                elif kind.endswith("_error") or kind in {
                    "error", "auth_error", "quota_exceeded", "rate_limited",
                    "insufficient_audio_activity", "input_error",
                }:
                    raise ScribeError("Scribe a refusé la transcription. Vérifiez connexion, permission speech_to_text et crédits.")
            if not self.final.done():
                raise ScribeError("Connexion Scribe fermée avant la fin de la phrase.")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self.final.done():
                message = str(exc) if isinstance(exc, ScribeError) else "Réponse Scribe invalide."
                self.final.set_exception(ScribeError(message))

    async def feed(self, pcm: bytes):
        if len(pcm) % 2:
            raise ScribeError("Audio micro incomplet.")
        if len(self.pcm) + len(pcm) > MAX_PCM_BYTES:
            raise ScribeError("Phrase trop longue ; faites une courte pause.")
        self.pcm.extend(pcm)
        await self.ws.send_json({
            "message_type": "input_audio_chunk",
            "audio_base_64": base64.b64encode(pcm).decode("ascii"),
            "sample_rate": 16000,
        })

    async def finish(self) -> str:
        if len(self.pcm) < 16000 * 2 * 0.15:
            return ""
        self.committing = True
        await self.ws.send_json({
            "message_type": "input_audio_chunk",
            "audio_base_64": "", "sample_rate": 16000, "commit": True,
        })
        try:
            return await asyncio.wait_for(asyncio.shield(self.final), 15)
        except asyncio.TimeoutError:
            raise ScribeError("Scribe n’a pas confirmé la phrase. Répétez votre demande.") from None

    async def close(self):
        if self.reader:
            self.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reader
            self.reader = None
        if self.final and self.final.done() and not self.final.cancelled():
            self.final.exception()  # Consomme aussi une erreur arrivée pendant l'annulation.
        if self.ws:
            await self.ws.close()
            self.ws = None
        if self.client:
            await self.client.close()
            self.client = None


def input_allowed(host, epoch, source="pc"):
    return (
        (source == "phone" or not host.ui.muted)
        and not getattr(host, "_is_speaking", False)
        and not getattr(host, "_interrupted", False)
        and epoch == getattr(host, "_speech_output_epoch", 0)
    )


async def accept_transcript(host, text, pcm, epoch, prosody="", source="pc",
                            acoustic_voice_ms=None):
    text = str(text or "").strip()
    if not text or not input_allowed(host, epoch, source):
        return False
    # Le texte final est littéral. Un dictionnaire phonétique ne possède pas
    # l'audio : « discorde » peut être le nom commun, pas l'application Discord.
    # Le lexique doit aider le moteur acoustique, jamais réécrire sa sortie.
    # Le filtrage des conversations lointaines ne remplace pas une empreinte.
    # Si elle existe, vérifier CET énoncé avant de l'envoyer à Gemini.
    speaker = getattr(host, "_speaker", None)
    if speaker is not None and speaker.enrolled:
        import numpy as np
        clip = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        verdict = await asyncio.to_thread(speaker.verify, clip)
        if verdict is None or not verdict.known or not input_allowed(host, epoch, source):
            return False
    from core.ai_stt_corrector import TranscriptGuard
    # Le texte seul est insuffisant : pour qu'un tour soit exécutable il doit
    # aussi correspondre à une quantité de voix effectivement confirmée par
    # le VAD local. Cela bloque les phrases inventées à partir d'un souffle,
    # d'une télévision ou d'un morceau de signal résiduel.
    assessment = TranscriptGuard().assess(
        text,
        acoustic_voice_ms=(acoustic_voice_ms if acoustic_voice_ms is not None
                           else getattr(host, "_last_voice_evidence_ms", None)),
        audio_duration_ms=len(pcm) / 32.0,
    )
    if not assessment.accepted:
        return False
    from core.barge_in import InterruptPhraseDetector
    busy = (
        getattr(host, "_model_turn_active", False)
        or getattr(host, "_is_thinking", False)
        or getattr(host, "_is_speaking", False)
    )
    # Pendant la voix/réflexion, une commande d'arrêt doit être adressée. Le
    # microphone peut encore contenir l'écho du TTS ; accepter « stop » nu
    # ici recréait exactement l'auto-interruption que le half-duplex évite.
    kind = InterruptPhraseDetector.classify_strict_interrupt(text)
    if busy and kind == "stop":
        host.ui.set_user_transcript(text, final=True)
        host.interrupt()
        return True
    if busy and kind == "redirect":
        host.interrupt()
    host._noise_turn = False
    host._live_user_text = text
    host._last_user_speech = time.monotonic()
    host.ui.set_user_transcript(text, final=True)
    host.ui.write_log(f"Vous : {text}")
    continuous = getattr(host, "_continuous", None)
    if continuous:
        continuous.on_user_transcript(text)
    if prosody:
        host._deferred_context = "\n".join(filter(None, [
            getattr(host, "_deferred_context", ""), prosody,
        ]))
    # Même chemin que les commandes écrites : confirmations, routines,
    # changement de persona, contexte écran et soumission sérialisée.
    await asyncio.to_thread(host._on_text_command, text)
    return True


async def refine_transcript(host, scribe_text: str, pcm: bytes) -> str:
    """Vérifie les mots avant exécution, sans choisir entre deux hypothèses."""
    host._precision_turn_text = ""
    text = " ".join(str(scribe_text or "").split())
    if not text or not getattr(host, "_precision_stt_enabled", True):
        return text
    mode = str(getattr(host, "_precision_stt_mode", "all") or "all").lower()
    if mode == "off":
        return text
    clip = np.frombuffer(pcm, dtype="<i2")
    if mode != "all" and not should_refine_live_turn(clip, 16000):
        return text
    transcriber = getattr(host, "_precision_stt", None)
    if transcriber is None:
        vocabulary = [
            *FRENCH_TECH_PHRASES,
            *FRENCH_MAIL_PHRASES,
            getattr(host, "_asst_name", "ANO-GPT"),
            str(getattr(host, "_user_name", "") or ""),
        ]
        transcriber = PrecisionTranscriber(
            str(getattr(host, "_gemini_api_key", "") or ""),
            model=str(getattr(host, "_precision_stt_model", "") or ""),
            vocabulary=vocabulary,
        )
        # La clé est déjà chargée par l'hôte ; on la conserve dans ce moteur
        # pour son cache très court, jamais dans les journaux.
        host._precision_stt = transcriber
    result = await transcriber.transcribe(
        clip, 16000,
        already_processed=bool(getattr(host, "_stt_denoiser", None)),
    )
    if not result.available or not result.text:
        raise UncertainTranscript(
            "Vérification vocale indisponible : demande non exécutée. Répétez votre demande."
        )
    precise = " ".join(result.text.split())
    if not transcripts_agree(text, precise):
        raise UncertainTranscript(
            "Reconnaissance incertaine : les deux moteurs ont entendu des mots différents. "
            "Demande non exécutée ; répétez votre phrase."
        )
    host._precision_turn_text = text
    return text


async def run_scribe(host, settings, turn_factory=ScribeTurn):
    turn = None
    epoch = 0
    source = "pc"
    prosody = ""
    drop_count = 0
    retry_after = 0.0
    host.ui.write_log("SYS : écoute Scribe Realtime — français, filtrage des conversations de fond.")
    try:
        while True:
            msg = await host.out_queue.get()
            marker = msg.get("activity")
            if marker == "video":
                await host.session.send_realtime_input(video={
                    "data": msg["data"], "mime_type": msg.get("mime_type", "image/webp"),
                })
                continue
            try:
                if marker == "start":
                    if turn:
                        await turn.close()
                        turn = None
                    epoch = msg.get("_audio_epoch", getattr(host, "_speech_output_epoch", 0))
                    source = msg.get("_audio_source", "pc")
                    prosody = ""
                    drop_count = msg.get("_audio_drop_count", 0)
                    if not input_allowed(host, epoch, source) or time.monotonic() < retry_after:
                        continue
                    # Liaison explicite : le rappel d'un tour garde son époque
                    # et sa source même après qu'un nouveau tour les a changées.
                    def partial(text, epoch=epoch, source=source):
                        if text and input_allowed(host, epoch, source):
                            host.ui.set_user_transcript(text)
                    turn = turn_factory(settings, partial)
                    await turn.start()
                elif turn is not None:
                    if not input_allowed(host, epoch, source) or msg.get("_audio_epoch", epoch) != epoch:
                        await turn.close()
                        turn = None
                        continue
                    if marker == "prosody":
                        prosody = str(msg.get("text") or "")
                    elif marker == "end":
                        evidence_ms = msg.get("_voice_evidence_ms",
                                              getattr(host, "_last_voice_evidence_ms", None))
                        if getattr(host, "_audio_drop_count", 0) != drop_count:
                            raise ScribeError("Audio incomplet : répétez votre demande.")
                        text = await turn.finish()
                        pcm = bytes(turn.pcm)
                        await turn.close()
                        turn = None
                        if not input_allowed(host, epoch, source):
                            continue
                        text = await refine_transcript(host, text, pcm)
                        await accept_transcript(host, text, pcm, epoch, prosody, source,
                                                acoustic_voice_ms=evidence_ms)
                    elif not marker and msg.get("data"):
                        await turn.feed(msg["data"])
            except asyncio.CancelledError:
                raise
            except UncertainTranscript as exc:
                host.ui.set_user_transcript("", final=True)
                host.ui.write_log(f"STT : {exc}")
            except Exception as exc:
                if turn:
                    await turn.close()
                    turn = None
                message = str(exc) if isinstance(exc, ScribeError) else "Transcription Scribe indisponible."
                host.ui.write_log(f"ERR : {message} Vous pouvez choisir Gemini dans Audio.")
                retry_after = time.monotonic() + 5.0
    finally:
        if turn:
            await turn.close()
