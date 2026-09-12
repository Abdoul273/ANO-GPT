"""Chemin STT unique : Gemini Transcribe, du PCM au texte exécutable.

Le modèle conversationnel Gemini Live ne reçoit jamais l'audio du micro dans
ce chemin. Il ne reçoit que la transcription finale, afin qu'une hypothèse de
conversation ne puisse pas réécrire une commande prononcée.
"""
from __future__ import annotations

import asyncio
import json
import time

import numpy as np

from core.live_speech_config import FRENCH_MAIL_PHRASES, FRENCH_TECH_PHRASES
from core.precision_stt import MAX_AUDIO_SECONDS, PrecisionTranscriber
from core.stt_audio import signal_metrics
from core.azure_speech_stt import AzureSpeechVerifier, agree as azure_agrees
# Ces garde-fous sont communs au STT historique et à Gemini Transcribe. Ils
# n'ouvrent aucune connexion ElevenLabs.
from core.scribe_stt import accept_transcript, input_allowed


class GeminiTranscribeError(RuntimeError):
    """Énoncé non exploitable : rien ne doit être exécuté."""


TRANSCRIBE_MODE = "VERBATIM"
# Borne réseau uniquement : un délai écoulé ne confirme jamais une phrase.
_FINALIZE_TIMEOUT_S = 4.0
# Au-delà de cette fenêtre le PCM ne correspond plus à ce que l'utilisateur
# vient de dire. Refuser le tour est toujours préférable à une commande ou une
# réponse décalée (file réseau ou boucle événementielle momentanément chargée).
_MAX_QUEUED_AUDIO_AGE_S = 2.0


def _set_transcript(ui, text: str, *, final: bool = False, turn_id: str = "") -> None:
    """Passe l'id de tour à Qt, en restant compatible avec les faux UI de tests."""
    try:
        ui.set_user_transcript(text, final=final, turn_id=turn_id)
    except TypeError:
        ui.set_user_transcript(text, final=final)


def _log_turn(ui, **fields) -> None:
    """Trace structurée par tour ; aucun PCM, endpoint ou secret n'est journalisé."""
    if ui is not None:
        ui.write_log("STT_METRICS " + json.dumps(fields, ensure_ascii=False, sort_keys=True))


def _is_stale_audio_message(msg: object, now: float | None = None) -> bool:
    """True si un paquet PCM a attendu trop longtemps dans la file locale.

    Les anciens messages de tests, ou les messages non-audio, restent valides :
    seuls les paquets explicitement horodatés par ``AudioEngine`` sont soumis à
    cette barrière de fraîcheur.
    """
    if not isinstance(msg, dict):
        return False
    captured_at = msg.get("_captured_at")
    if not isinstance(captured_at, (int, float)):
        return False
    return (time.monotonic() if now is None else now) - captured_at > _MAX_QUEUED_AUDIO_AGE_S


def select_consensus_text(gemini_final: str, gemini_preview: str, azure_text: str) -> str:
    """Choisit la transcription que confirme Azure.

    Gemini peut afficher une hypothèse temps réel excellente puis émettre une
    finale révisée et sans rapport. Lorsque Azure confirme l'hypothèse visible
    mais pas cette finale, préserver l'hypothèse rend l'interface stable et
    évite d'exécuter une phrase inventée. Une absence d'accord laisse une
    chaîne vide : l'appelant demandera simplement de répéter.
    """
    final = " ".join(str(gemini_final or "").split())
    preview = " ".join(str(gemini_preview or "").split())
    azure = " ".join(str(azure_text or "").split())
    if not azure:
        return final
    if azure_agrees(final, azure):
        return final
    if preview and azure_agrees(preview, azure):
        return preview
    return ""


class GeminiTranscribeTurn:
    """Session Transcribe ouverte pendant la parole, jamais après coup.

    Les hypothèses ``interim`` sont uniquement affichées. La valeur renvoyée
    par ``finish`` est le champ ``input_transcription`` confirmé par le serveur.
    """

    def __init__(self, host, on_partial=None):
        self.host = host
        self.on_partial = on_partial
        self.client = self.session = self._connection = self.reader = None
        self.final = None
        self.pcm = bytearray()
        self._last_confirmed = ""
        self.preview = ""
        self._accepting = False
        self._ending = False
        self._reusable = True
        self._failure = ""

    async def start(self):
        key = str(getattr(self.host, "_gemini_api_key", "") or "")
        if not key:
            raise GeminiTranscribeError("Clé Gemini absente.")
        try:
            from google import genai
            from google.genai import types

            vocabulary = [
                *FRENCH_TECH_PHRASES, *FRENCH_MAIL_PHRASES,
                getattr(self.host, "_asst_name", "ANO-GPT"),
                str(getattr(self.host, "_user_name", "") or ""),
            ]
            vocabulary = list(dict.fromkeys(item for item in vocabulary if item))[:100]
            config = types.LiveConnectConfig(
                response_modalities=["TEXT"],
                input_audio_transcription=types.AudioTranscriptionConfig(
                    language_codes=["fr-FR"], custom_vocabulary=vocabulary,
                    # VERBATIM : les mots prononcés, pas une reformulation.
                    # SMART réécrivait les commandes (« rappelle-moi la facture »
                    # → autre phrase plausible) et faisait exécuter autre chose.
                    mode=TRANSCRIBE_MODE,
                ),
                realtime_input_config=types.RealtimeInputConfig(
                    automatic_activity_detection=types.AutomaticActivityDetection(disabled=True),
                ),
            )
            self.client = genai.Client(api_key=key, http_options={"api_version": "v1beta"})
            self._connection = self.client.aio.live.connect(
                model=str(getattr(self.host, "_precision_stt_model", "") or ""),
                config=config,
            )
            self.session = await self._connection.__aenter__()
            self.reader = asyncio.create_task(self._read(), name="gemini-transcribe-reader")
        except GeminiTranscribeError:
            await self.close()
            raise
        except Exception as exc:
            await self.close()
            raise GeminiTranscribeError(f"Gemini Transcribe indisponible : {exc}") from None

    async def begin(self, on_partial=None):
        """Ouvre un tour sur la connexion persistante déjà établie."""
        if (self.session is None or not self._reusable or self._failure
                or (self.reader is not None and self.reader.done())):
            raise GeminiTranscribeError("Session Gemini Transcribe à renouveler.")
        if self.final is not None and not self.final.done():
            raise GeminiTranscribeError("Le tour précédent est encore ouvert.")
        from google.genai import types
        self.on_partial = on_partial
        self.pcm.clear()
        self._last_confirmed = ""
        self.preview = ""
        self.final = asyncio.get_running_loop().create_future()
        self._accepting = True
        self._ending = False
        await self.session.send_realtime_input(activity_start=types.ActivityStart())

    async def _read(self):
        try:
            # Le SDK termine receive() à CHAQUE turn_complete. La connexion,
            # elle, reste ouverte : reprendre la lecture pour la phrase suivante.
            while True:
                received = False
                async for message in self.session.receive():
                    received = True
                    server = getattr(message, "server_content", None)
                    if not self._accepting:
                        continue
                    interim = getattr(server, "interim_input_transcription", None)
                    interim_text = " ".join(str(getattr(interim, "text", "") or "").split())
                    if interim_text and self.on_partial:
                        self.preview = " ".join(filter(None, [self._last_confirmed, interim_text]))
                        self.on_partial(self.preview)
                    confirmed = getattr(server, "input_transcription", None)
                    text = " ".join(str(getattr(confirmed, "text", "") or "").split())
                    if text:
                        # L'API émet des SEGMENTS finalisés, les révisions sont
                        # dans interim_input_transcription. Garder chaque segment,
                        # notamment les négations et corrections après une pause.
                        self._last_confirmed = " ".join(filter(None, [self._last_confirmed, text]))
                        self.preview = self._last_confirmed
                        if self.on_partial:
                            self.on_partial(self._last_confirmed)
                    # Transcribe peut terminer par voice_activity ACTIVITY_END
                    # après generation_complete, sans turn_complete (observé
                    # sur l'API réelle). Les deux sont des bornes explicites ;
                    # un délai ou un simple segment de texte ne l'est pas.
                    activity = getattr(message, "voice_activity", None)
                    ended = getattr(activity, "voice_activity_type", None) == "ACTIVITY_END"
                    if getattr(server, "turn_complete", False) or ended:
                        if not self._ending:
                            raise GeminiTranscribeError("Transcription terminée avant la fin du micro.")
                        self._accepting = False
                        if self.final is not None and not self.final.done():
                            self.final.set_result(self._last_confirmed)
                if not received:
                    raise GeminiTranscribeError("Connexion de transcription fermée.")
                # Évite une boucle chaude même avec un transport synthétique.
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failure = str(exc) or "Réponse Gemini invalide."
            self._reusable = False
            self._accepting = False
            if self.final is not None and not self.final.done():
                self.final.set_result("")

    async def feed(self, data: bytes):
        if len(data) % 2:
            raise GeminiTranscribeError("Audio micro incomplet ; répétez votre demande.")
        # PCM capturé intact : aucun second débruiteur/AGC ici.
        if len(self.pcm) + len(data) > 16000 * 2 * MAX_AUDIO_SECONDS:
            raise GeminiTranscribeError("Phrase trop longue ; faites une courte pause.")
        from google.genai import types
        self.pcm.extend(data)
        await self.session.send_realtime_input(audio=types.Blob(
            data=data, mime_type="audio/pcm;rate=16000",
        ))

    async def finish(self) -> str:
        if len(self.pcm) < int(16000 * 0.15) * 2:
            await self.abort()
            return ""
        from google.genai import types
        self._ending = True
        try:
            await self.session.send_realtime_input(activity_end=types.ActivityEnd())
            text = await asyncio.wait_for(
                asyncio.shield(self.final), timeout=_FINALIZE_TIMEOUT_S,
            )
            if self._failure:
                raise GeminiTranscribeError(self._failure)
            return text
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._reusable = False
            self._accepting = False
            if self.final is not None and not self.final.done():
                self.final.set_result("")
            raise GeminiTranscribeError(
                "Phrase non confirmée ; répétez votre demande."
            ) from exc

    async def abort(self) -> None:
        """Invalide le tour ; ses réponses tardives ne doivent pas devenir le suivant."""
        self._accepting = False
        self._reusable = False
        self.on_partial = None
        if self.final is not None and not self.final.done():
            self.final.set_result("")
        self.pcm.clear()
        self._last_confirmed = ""
        try:
            from google.genai import types
            if self.session is not None:
                await self.session.send_realtime_input(activity_end=types.ActivityEnd())
        except Exception:
            pass
        # Le consommateur rouvrira la connexion au prochain start. Sans id de
        # tour côté protocole, réutiliser une session annulée mélange les textes.

    async def close(self):
        if self.reader:
            self.reader.cancel()
            try:
                await self.reader
            except (asyncio.CancelledError, Exception):
                pass
            self.reader = None
        if self._connection:
            try:
                await self._connection.__aexit__(None, None, None)
            except Exception:
                pass
            self._connection = self.session = None
        if self.client:
            try:
                await self.client.aio.aclose()
            except Exception:
                pass
            self.client = None


def _engine(host) -> PrecisionTranscriber:
    engine = getattr(host, "_precision_stt", None)
    if engine is None:
        vocabulary = [
            *FRENCH_TECH_PHRASES,
            *FRENCH_MAIL_PHRASES,
            getattr(host, "_asst_name", "ANO-GPT"),
            str(getattr(host, "_user_name", "") or ""),
        ]
        engine = PrecisionTranscriber(
            str(getattr(host, "_gemini_api_key", "") or ""),
            model=str(getattr(host, "_precision_stt_model", "") or ""),
            vocabulary=vocabulary,
        )
        host._precision_stt = engine
    return engine


async def run_gemini_transcribe(host, settings=None, turn_factory=GeminiTranscribeTurn):
    """Consomme les tours VAD locaux et soumet uniquement du texte confirmé.

    Le flux PCM est expédié pendant la parole. Gemini Transcribe reçoit donc le
    contexte acoustique complet sans ajouter un second temps d'envoi après le
    marqueur ``end``.
    """
    del settings  # Les réglages sont déjà figés sur l'hôte au démarrage.
    stream = None
    turn = None
    epoch = 0
    source = "pc"
    prosody = ""
    drop_count = 0
    retry_after = 0.0
    capture_allowed = False
    turn_id = ""
    turn_started_at = 0.0
    max_packet_age_ms = 0.0
    ui = getattr(host, "ui", None)
    if ui is not None:
        ui.write_log("SYS : écoute Gemini Transcribe — français, transcription littérale.")

    try:
        while True:
            msg = await host.out_queue.get()
            marker = msg.get("activity") if isinstance(msg, dict) else None
            if marker == "video":
                # La vision reste reliée à Gemini Live, sans mélanger son flux avec
                # le tour STT audio.
                try:
                    await host.session.send_realtime_input(video={
                        "data": msg["data"], "mime_type": msg.get("mime_type", "image/webp"),
                    })
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    print(f"[Gemini Transcribe] envoi vidéo ignoré : {exc}")
                continue
            try:
                if _is_stale_audio_message(msg):
                    if turn is not None:
                        await turn.abort()
                        turn = None
                    capture_allowed = False
                    if ui is not None:
                        ui.write_log(
                            "STT : audio trop ancien ignoré ; répète ta phrase."
                        )
                    continue
                if marker == "start":
                    if turn is not None:
                        await turn.abort()
                    epoch = msg.get("_audio_epoch", getattr(host, "_speech_output_epoch", 0))
                    turn_id = str(msg.get("turn_id") or "")
                    turn_started_at = time.monotonic()
                    max_packet_age_ms = 0.0
                    source = msg.get("_audio_source", "pc")
                    prosody = ""
                    drop_count = msg.get("_audio_drop_count", 0)
                    turn = None
                    capture_allowed = (
                        input_allowed(host, epoch, source)
                        and time.monotonic() >= retry_after
                    )
                    if not capture_allowed:
                        continue
                    def partial(text, turn_epoch=epoch, turn_source=source, current_turn_id=turn_id):
                        if input_allowed(host, turn_epoch, turn_source):
                            _set_transcript(host.ui, text, turn_id=current_turn_id)
                    # Une session Transcribe est réutilisée jusqu'à dix minutes.
                    # Cela supprime un handshake WebSocket à chaque phrase, qui
                    # était la première source des échecs aléatoires observés.
                    if stream is None:
                        last_error = None
                        for _ in range(3):
                            candidate = turn_factory(host, partial)
                            try:
                                await asyncio.wait_for(candidate.start(), timeout=6.0)
                                stream = candidate
                                break
                            except Exception as exc:
                                last_error = exc
                                await candidate.close()
                        if stream is None:
                            raise GeminiTranscribeError(
                                f"Connexion Gemini Transcribe impossible : {last_error}"
                            )
                    try:
                        await stream.begin(partial)
                    except Exception:
                        # Une session longue peut être fermée côté serveur entre
                        # deux phrases. Réouvrir immédiatement ici évite de perdre
                        # le tour qui vient juste de commencer.
                        await stream.close()
                        stream = turn_factory(host, partial)
                        try:
                            await asyncio.wait_for(stream.start(), timeout=6.0)
                            await stream.begin(partial)
                        except Exception as exc:
                            await stream.close()
                            stream = None
                            raise GeminiTranscribeError(
                                f"Reconnexion Gemini Transcribe impossible : {exc}"
                            ) from None
                    turn = stream
                    _set_transcript(host.ui, "…", turn_id=turn_id)
                    continue
                if marker == "prosody":
                    prosody = str(msg.get("text") or "")
                    continue
                if marker == "cancel":
                    # Invalider ce tour sans interrompre la boucle audio. La
                    # connexion STT sera renouvelée avant la prochaine phrase.
                    capture_allowed = False
                    if turn is not None:
                        try:
                            await turn.abort()
                        except Exception:
                            pass
                    turn = None
                    _log_turn(ui, turn_id=turn_id, decision="cancelled", duration_ms=round((time.monotonic() - turn_started_at) * 1000))
                    continue
                if not capture_allowed or not input_allowed(host, epoch, source):
                    if turn is not None:
                        try:
                            await turn.abort()
                        except Exception:
                            pass
                    turn = None
                    continue
                if marker is None and msg.get("data"):
                    data = bytes(msg["data"])
                    captured_at = msg.get("_captured_at")
                    if isinstance(captured_at, (float, int)):
                        max_packet_age_ms = max(max_packet_age_ms, (time.monotonic() - captured_at) * 1000)
                    if turn is not None:
                        await turn.feed(data)
                    continue
                if marker != "end":
                    continue
                evidence_ms = msg.get("_voice_evidence_ms", getattr(host, "_last_voice_evidence_ms", None))
                if getattr(host, "_audio_drop_count", 0) != drop_count:
                    raise GeminiTranscribeError("Audio incomplet : répétez votre demande.")
                if turn is None:
                    continue
                metrics = signal_metrics(bytes(turn.pcm))
                host._last_stt_audio_metrics = metrics
                host.ui.write_log(
                    f"MIC : signal {metrics['duration_s']:.2f}s | "
                    f"niveau {metrics['rms_dbfs']:.1f} dBFS | "
                    f"crête {metrics['peak_dbfs']:.1f} dBFS | "
                    f"écrêtage {metrics['clipped_percent']:.2f} %."
                )
                text = await turn.finish()
                pcm = bytes(turn.pcm)
                preview = str(getattr(turn, "preview", "") or "")
                turn = None
                if not text:
                    # Tour confirmé vide : effacer aussi le sous-titre provisoire.
                    _set_transcript(host.ui, "", final=True, turn_id=turn_id)
                    _log_turn(ui, turn_id=turn_id, decision="empty", max_packet_age_ms=round(max_packet_age_ms, 1), **metrics)
                    capture_allowed = False
                    continue
                if not capture_allowed or not input_allowed(host, epoch, source):
                    continue
                verifier = getattr(host, "_azure_speech_verifier", None)
                azure_text = ""
                azure_status = "disabled"
                if getattr(host, "_azure_speech_enabled", False) and verifier is None:
                    vocabulary = [
                        *FRENCH_TECH_PHRASES, *FRENCH_MAIL_PHRASES,
                        getattr(host, "_asst_name", "ANO-GPT"),
                        str(getattr(host, "_user_name", "") or ""),
                    ]
                    verifier = AzureSpeechVerifier(
                        vocabulary,
                        use_fast_phrases=bool(getattr(host, "_azure_fast_phrases", False)),
                    )
                    host._azure_speech_verifier = verifier
                if verifier is not None and verifier.available():
                    azure = await asyncio.to_thread(verifier.transcribe, pcm)
                    azure_text = azure.text
                    azure_status = "ok" if azure.text else ("error" if azure.error else "empty")
                    selected = select_consensus_text(text, preview, azure.text)
                    if azure.text and not selected:
                        _set_transcript(host.ui, "", final=True, turn_id=turn_id)
                        _log_turn(
                            ui, turn_id=turn_id, decision="rejected_disagreement",
                            started_at=round(turn_started_at, 3), max_packet_age_ms=round(max_packet_age_ms, 1),
                            queue_depth_end=msg.get("_queue_depth_at_end", 0), vad_evidence_ms=evidence_ms,
                            gemini_preview=preview, gemini_final=text, azure_text=azure_text, azure_status=azure_status,
                            **metrics,
                        )
                        host.ui.write_log(
                            "STT : Gemini et Azure n'ont pas compris la même phrase ; répète."
                        )
                        capture_allowed = False
                        continue
                    if selected:
                        text = selected
                    if azure.error:
                        host.ui.write_log("STT : vérification Azure indisponible ; Gemini utilisé seul.")
                # Affichage confirmé avant toute exécution : le même tour passe
                # de live à final, sans attendre la réponse d'ANO-GPT.
                _set_transcript(host.ui, text, final=True, turn_id=turn_id)
                _log_turn(
                    ui, turn_id=turn_id, decision="consensus" if azure_text else "gemini_only",
                    started_at=round(turn_started_at, 3), max_packet_age_ms=round(max_packet_age_ms, 1),
                    queue_depth_end=msg.get("_queue_depth_at_end", 0), vad_evidence_ms=evidence_ms,
                    gemini_preview=preview, gemini_final=text, azure_text=azure_text, azure_status=azure_status,
                    **metrics,
                )
                # Une seule passe : le flux live EST déjà Gemini Transcribe VERBATIM.
                # Une seconde connexion Live (refine_transcript) ajoutait 8–15 s
                # pour le même modèle, sans gain réel.
                host._precision_turn_text = text
                accepted = await accept_transcript(
                    host, text, pcm, epoch, prosody, source, acoustic_voice_ms=evidence_ms,
                )
                capture_allowed = False
                if not accepted:
                    _set_transcript(host.ui, "", final=True, turn_id=turn_id)
            except asyncio.CancelledError:
                raise
            except GeminiTranscribeError as exc:
                _set_transcript(host.ui, "", final=True, turn_id=turn_id)
                host.ui.write_log(f"STT : {exc}")
                turn = None
                if stream is not None:
                    try:
                        await stream.close()
                    except Exception:
                        pass
                    stream = None
                retry_after = time.monotonic() + 1.0
            except Exception as exc:
                _set_transcript(host.ui, "", final=True, turn_id=turn_id)
                host.ui.write_log("STT : Gemini Transcribe indisponible ; aucune demande n'a été exécutée.")
                print(f"[Gemini Transcribe] {exc}")
                turn = None
                if stream is not None:
                    try:
                        await stream.close()
                    except Exception:
                        pass
                    stream = None
                retry_after = time.monotonic() + 3.0
    finally:
        if stream is not None:
            await stream.close()
