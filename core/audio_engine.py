"""Moteur audio d'ANO-GPT : capture 16 kHz, lecture 24 kHz, full-duplex AEC.

Concurrence
-----------
* **asyncio** — ``_listen_audio`` / ``_play_audio`` vivent dans le TaskGroup
  de la session Live.
* **callback PortAudio** — ``InputStream.callback`` tourne dans un thread
  audio natif ; il ne fait que du numpy borné et ``call_soon_threadsafe``.
* **ThreadPoolExecutor** (1 worker ``anogpt-audio-output``) — ``stream.write``
  PCM 24 kHz, isolé du pool asyncio global.
* **threading.Event** — ``_audio_abort_event`` / ``_barge_stop_event`` /
  ``_mic_reopen_evt`` traversent le callback micro et la boucle de lecture.
* **Qt** — ``ui.set_volume`` / ``ui.set_state`` depuis le callback ; jamais
  de traitement lourd ici (GIL partagé avec l'audio).

Full-duplex (AEC)
-----------------
Quand ``libspeexdsp`` est disponible, le moteur fonctionne en full-duplex :
le micro reste OUVERT en permanence, l'écho du haut-parleur est soustrait
en temps réel par ``core.echo_canceller.FullDuplexFilter``, et le Silero VAD
détecte la parole utilisateur sur le signal nettoyé pour déclencher le
barge-in sans faux positif. La boucle de lecture ``_play_audio`` alimente
la référence AEC à chaque chunk envoyé aux haut-parleurs.

Half-duplex (fallback)
----------------------
Si l'AEC est indisponible (pas de libspeexdsp), le moteur retombe sur le
comportement précédent : rien ne part au modèle tant que ``_is_speaking``
est vrai, et l'interruption passe par le second flux Vosk local
(``LocalBargeInListener``). Voir ``HalfDuplexGate``.
"""
from __future__ import annotations

import asyncio
import collections
import concurrent.futures
import re
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np

from core import audio_router
from core.barge_in import InterruptPhraseDetector, LocalBargeInListener, hold_live_audio
from core.echo_canceller import get_full_duplex_filter
from core.event_bus import AudioCaptureFrameEvent, BargeInDetectedEvent
from core.speech_sync import (
    caption_targets_weighted, split_caption_units,
)
from core.stt import AudioPreprocessor
from core.stt_audio import pcm16_bytes
from core.wake_word import MusicWakeWordDetector, WakeWordDetector

class _MainAttr:
    """Lit un global de ``main`` au moment de l'usage.

    Deux raisons : ``google.genai`` et ``sounddevice`` sont importés en différé
    pour ne pas retarder la fenêtre Qt ; les tests patchent ``main.types`` et
    ``main.save_live_voice`` plutôt que le module d'origine de la méthode.
    """

    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, item: str):
        import main as _main
        target = getattr(_main, self._name, None)
        if target is None and self._name == "types":
            try:
                from google.genai import types as _gt
                target = _gt
            except Exception:
                pass
        return getattr(target, item)

    def __call__(self, *args, **kwargs):
        import main as _main
        return getattr(_main, self._name)(*args, **kwargs)

    def __bool__(self) -> bool:
        import main as _main
        return bool(getattr(_main, self._name))

sd = _MainAttr("sd")

CHANNELS = 1
SEND_SAMPLE_RATE = 16000
RECEIVE_SAMPLE_RATE = 24000
CHUNK_SIZE = 1024
_OUTPUT_SLICE_MS = 20
_OUTPUT_LATENCY_S = 0.06
# Réserve au début d'une réponse : absorbe les arrivées en
# rafales sans ajouter une attente à chaque bloc. Attente bornée et annulable.
_OUTPUT_PREFILL_S = 0.08

# Durée maximale d'un tour de parole avant fermeture forcée. Le VAD est piloté
# côté client : si un bruit continu maintenait le portier ouvert, la phrase ne
# serait jamais remise au modèle. Au-delà, on ferme et on rouvre aussitôt.
_MAX_UTTERANCE_S = 20.0

# Fin de phrase mesurée sur l'énergie brute, en secours du VAD qui peut rester
# verrouillé sous un bruit de fond continu. 0,9 s = le silence naturel de fin de
# phrase, aligné sur le hangover du préprocesseur (_HANGOVER_MS).
_END_SILENCE_S = 0.9
# Fin de phrase accélérée : quand la transcription live se termine déjà par
# une ponctuation finale (« . ? ! »), le serveur a lui-même conclu la phrase ;
# attendre 950 ms de plus n'apporte rien. Une virgule ou une phrase ouverte
# (« ouvre… euh… ») garde le délai long : rien n'est coupé en deux.
_FAST_END_SILENCE_S = 0.45
# L'aperçu doit dater d'au moins ce délai : le texte du dernier mot prononcé
# arrive avec un peu de retard, un aperçu trop frais peut encore changer.
_FAST_END_PREVIEW_SETTLE_S = 0.25
_TERMINAL_PUNCTUATION = (".", "?", "!", "…")


def preview_looks_complete(preview, now: float, turn_started_at: float) -> bool:
    """Vrai si l'aperçu STT (texte, horodatage, id) décrit une phrase conclue."""
    try:
        text, updated_at, _turn = preview
    except (TypeError, ValueError):
        return False
    text = str(text or "").rstrip()
    if len(text) < 2 or updated_at < turn_started_at:
        return False
    if (now - float(updated_at)) < _FAST_END_PREVIEW_SETTLE_S:
        return False
    return text.endswith(_TERMINAL_PUNCTUATION)

_BARGE_ARM_S = 0.25
# `has_speech(strict=True)` apporte déjà une confirmation longue et très
# sélective. Cette petite fenêtre ne sert qu'à rejeter un dernier pic isolé.
_BARGE_CONFIRM_S = 0.16


def _update_barge_in(
    state: dict,
    jarvis_speaking: bool,
    speech_detected: bool,
    voice_confirmed: bool | None = None,
    chunk_seconds: float = CHUNK_SIZE / SEND_SAMPLE_RATE,
) -> bool:
    """Ancien portier VAD, conservé pour compatibilité des tests.

    La boucle audio ne l'emploie plus : le VAD du micro brut ne peut pas
    distinguer une voix humaine de la voix TTS. L'interruption réelle passe
    maintenant par ``core.barge_in`` sur la source AEC locale.

    `AudioPreprocessor._speech_active` est un verrou unique, maintenu
    `_HANGOVER_MS` (900 ms) après la dernière trame voisée. L'assistant commence
    à répondre bien avant la fin de ce maintien : le détecteur était donc encore
    vrai à cause de la phrase que l'utilisateur VIENT de terminer, et l'assistant
    s'interrompait lui-même dès le premier mot de chaque réponse. Micro coupé, le
    callback micro sort avant d'arriver ici — d'où le symptôme « il ne parle que
    quand je coupe le micro », briefing compris.

    L'interruption n'est donc armée qu'une fois le verrou retombé au moins une
    fois depuis le début de la réponse : seule une reprise de parole réelle peut
    alors couper l'assistant.

    Une seule transition bruitée ne suffit plus : il faut d'abord une courte
    fenêtre réellement calme pour armer l'interruption, puis une voix humaine
    confirmée et continue. `voice_confirmed` est une preuve acoustique sans
    hystérésis ; elle empêche un ventilateur ayant verrouillé le VAD de couper
    la lecture.
    """
    if voice_confirmed is None:
        voice_confirmed = speech_detected
    chunk_seconds = max(0.001, float(chunk_seconds or 0.0))

    if jarvis_speaking and not state["was_speaking"]:
        state["barge_armed"] = False
        state["barge_quiet_s"] = 0.0
        state["barge_voice_s"] = 0.0
    state["was_speaking"] = jarvis_speaking

    if not jarvis_speaking:
        state["barge_armed"] = False
        state["barge_quiet_s"] = 0.0
        state["barge_voice_s"] = 0.0
        return False

    # Exiger la retombée du détecteur STRICT protège contre la propre voix de
    # l'assistant. Une preuve instantanée seule confond l'écho du haut-parleur
    # avec l'utilisateur et provoque une auto-interruption.
    if not speech_detected:
        state["barge_quiet_s"] = state.get("barge_quiet_s", 0.0) + chunk_seconds
        state["barge_voice_s"] = 0.0
        if state["barge_quiet_s"] >= _BARGE_ARM_S:
            state["barge_armed"] = True
        return False

    state["barge_quiet_s"] = 0.0
    if not state.get("barge_armed") or not voice_confirmed:
        state["barge_voice_s"] = 0.0
        return False

    state["barge_voice_s"] = state.get("barge_voice_s", 0.0) + chunk_seconds
    if state["barge_voice_s"] < _BARGE_CONFIRM_S:
        return False

    # Désarmement immédiat pour éviter plusieurs appels interrupt() sur la même
    # reprise de parole avant que set_speaking(False) soit propagé.
    state["barge_armed"] = False
    state["barge_voice_s"] = 0.0
    return True




class AudioHost(Protocol):
    """Contrat que l'orchestrateur expose au moteur audio."""

    ui: Any
    session: Any
    audio_in_queue: Any
    speech_text_queue: Any
    out_queue: Any
    _loop: Any
    _is_speaking: bool
    _speaking_lock: threading.Lock
    _interrupted: bool
    _model_turn_active: bool
    _is_thinking: bool
    _activity_open: bool
    _phone_active: bool

    def interrupt(self) -> None: ...
    def discard_model_audio(self) -> bool: ...
    def _end_discarded_turn(self) -> None: ...
    def set_speaking(self, value: bool) -> None: ...
    def reset_audio_and_turn_state(self, source: str = "unknown") -> None: ...
    def check_audio_watchdog(self, now: float | None = None) -> bool: ...
    def _activity_start(self) -> None: ...
    def _activity_end(self) -> None: ...
    def _activity_cancel(self) -> None: ...
    def _enqueue_out(self, msg: dict) -> None: ...


@dataclass
class HalfDuplexGate:
    """État half-duplex partagé audio ↔ session, sans accès croisé aux ``_private``.

    Les drapeaux restent aussi posés sur l'hôte (``self._is_speaking``, …) :
    les tests construisent ``JarvisLive.__new__`` puis les positionnent
    directement. Ce dataclass documente la propriété et le thread owner.
    """

    speaking_lock: threading.Lock = field(default_factory=threading.Lock)
    audio_abort: threading.Event = field(default_factory=threading.Event)
    barge_stop: threading.Event = field(default_factory=threading.Event)
    is_speaking: bool = False
    interrupted: bool = False
    model_turn_active: bool = False
    is_thinking: bool = False
    activity_open: bool = False


class AudioCallbacks(Protocol):
    """Callbacks typés AudioEngine → orchestrateur."""

    def on_wake_word(self, reason: str) -> str: ...
    def on_offline_utterance(self, text: str, source: str) -> bool: ...
    def on_user_speech_detected(self) -> None: ...
    def probe_ambient(self) -> None: ...
    def check_speaker(self) -> None: ...


# Silence maximal toléré pendant qu'une réponse est censée sortir.
STALLED_SPEECH_S = 6.0
# ~3 s de niveau vocal (blocs de 64 ms) sans qu'un tour s'ouvre = surdité.
_DEAF_CHUNKS = 45


def _log_watchdog(host: Any, text: str) -> None:
    """Le chien de garde parle dans le journal de l'interface, pas seulement
    dans le terminal : c'est là que les blocages se lisent."""
    ui = getattr(host, "ui", None)
    if ui is not None and hasattr(ui, "write_log"):
        try:
            ui.write_log(f"SYS : chien de garde audio — {text}.")
        except Exception:
            pass


class AudioEngine:
    """Capture InputStream 16 kHz + playback RawOutputStream 24 kHz.

    Les méthodes sont prévues pour être *liées à l'hôte* ``JarvisLive``
    (``JarvisLive.interrupt = AudioEngine.interrupt``) : elles lisent
    ``self._is_speaking`` etc. sur l'objet qui les porte. L'instance
    ``AudioEngine()`` documente le moteur ; le dispatch reste sur l'hôte
    pour préserver le half-duplex atomique et les tests.
    """

    def set_speaking(self, value: bool):
        # Chaque réponse contient des dizaines de tranches PCM. Réémettre le
        # même état Qt à chaque tranche saturait inutilement la file graphique
        # et pouvait voler du temps CPU au flux audio.
        with self._speaking_lock:
            # Cette méthode décrit une TRANSITION, pas un niveau à republier.
            # La boucle de lecture appelle aussi set_speaking(False) lors de
            # son nettoyage et des reconnexions. Réarmer la conversation à
            # chacun de ces appels produisait plusieurs lignes « écoute
            # continue active » et repoussait sans cesse son minuteur, alors
            # qu'aucune parole de l'assistant ne venait de se terminer.
            if self._is_speaking == value:
                return
            self._is_speaking = value
            if value:
                self._speech_output_epoch = getattr(self, "_speech_output_epoch", 0) + 1
        if value:
            if hasattr(self, "_continuous"):
                self._continuous.on_assistant_speech_start()
            self.ui.set_state("SPEAKING")
        elif not self.ui.muted:
            self.ui.set_state("LISTENING")
            if hasattr(self, "_continuous"):
                self._continuous.on_assistant_speech_end(loop=self._loop)
            if hasattr(self, "_proactive"):
                self._proactive.wake()

    @staticmethod
    def _estimate_spoken_seconds(text: str) -> float:
        """Approximate when the next visible phrase should replace this one."""
        words = re.findall(r"[\wÀ-ÖØ-öø-ÿ']+", text or "")
        word_time = len(words) / 2.85
        punct_pause = 0.22 if re.search(r"[.!?…]\s*$", text or "") else 0.06
        return max(0.38, min(4.2, word_time + punct_pause))

    def _queue_spoken_text(self, text: str) -> None:
        q = self.speech_text_queue
        if not q:
            return
        units = split_caption_units(text, words_per_unit=2)
        if not units:
            return
        # Gemini envoie la transcription en parallèle de l'audio : ce fragment
        # correspond au PCM reçu depuis le fragment précédent. Mesurer cette
        # tranche vaut mieux que de deviner un débit — 2,85 mots/seconde est
        # juste en moyenne et faux sur chaque phrase, et l'erreur s'accumule
        # jusqu'à décaler la fin d'une longue réponse.
        cursor = getattr(self, "_speech_audio_cursor", 0.0)
        span = self._audio_enqueued_sec - cursor
        estimate = self._estimate_spoken_seconds(text)
        if span >= 0.12:
            start_sec, duration = cursor, span
        else:
            # Le texte a devancé son audio : on retombe sur l'estimation, en
            # repartant de la fin déjà attribuée pour ne pas revenir en arrière.
            start_sec = max(cursor, self._audio_enqueued_sec - estimate - 0.08)
            duration = estimate
        self._speech_audio_cursor = max(cursor, start_sec + duration)
        targets = caption_targets_weighted(start_sec, duration, units)

        # Chaque fragment apparaît pendant que les mots correspondants sont
        # joués. La carte grandit ainsi réellement avec la voix, et non d'un
        # seul coup au début d'une longue réponse.
        for target_sec, unit in zip(targets, units):
            if self._speech_last_target_sec:
                target_sec = max(target_sec, self._speech_last_target_sec + 0.045)
            self._speech_last_target_sec = target_sec
            try:
                q.put_nowait((target_sec, unit))
            except asyncio.QueueFull:
                break

    def _drain_spoken_text_queue(self) -> None:
        q = self.speech_text_queue
        if not q:
            return
        while True:
            try:
                q.get_nowait()
            except Exception:
                break

    def _flush_synced_speech_text(self, force: bool = False) -> None:
        """Affiche le texte entendu, avec rattrapage borné des transcriptions tardives."""
        q = self.speech_text_queue
        if not q:
            return
        # Gemini peut envoyer quelques secondes de PCM avant sa transcription.
        # Un seul groupe de deux mots par tranche audio (20 ms) entretenait
        # alors un retard très visible. On vide les groupes déjà échus par lot
        # court : l'affichage rattrape immédiatement, sans jamais afficher un
        # mot dont l'instant audio n'est pas encore atteint.
        max_units = 64 if force else 8
        heard_sec = float("inf")
        if not force:
            # ``_audio_played_sec`` compte ce qui a été REMIS à PortAudio, pas
            # ce qui est sorti des haut-parleurs : le tampon du périphérique
            # retarde l'écoute d'autant. Sans cette soustraction, le sous-titre
            # devançait la voix de toute la latence de sortie.
            heard_sec = self._audio_played_sec - getattr(
                self, "_audio_output_latency", _OUTPUT_LATENCY_S)

        for _ in range(max_units):
            try:
                target_sec, _txt = q._queue[0]
            except Exception:
                return
            # La carte texte est mise à jour sur le thread Qt au prochain tour
            # d'événements. Une avance de 35 ms absorbe ce passage sans faire
            # apparaître le mot visiblement avant la voix.
            if not force and target_sec > heard_sec + 0.035:
                return
            try:
                item = q.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                _target_sec, txt = item
            except Exception:
                txt = str(item)
            prefix = "[INLINE]" if self._speech_display_open else f"[INLINE_START]{self._asst_name}: "
            self.ui.write_log(f"{prefix}{txt}")
            self._speech_display_open = True

    def _reset_speech_sync(self) -> None:
        self._drain_spoken_text_queue()
        self._speech_next_text_at = 0.0
        self._audio_turn_active = False
        self._audio_enqueued_sec = 0.0
        self._audio_played_sec = 0.0
        self._speech_last_target_sec = 0.0
        self._speech_audio_cursor = 0.0

    def _clear_interrupted(self) -> None:
        """Referme une interruption et rend la parole au modèle.

        Ce drapeau ne doit JAMAIS rester armé : tant qu'il l'est, l'audio, les
        transcriptions et jusqu'aux appels d'outils du modèle sont jetés en
        silence. Il n'était effacé qu'à la réception du `turn_complete` du tour
        coupé — or ce message n'arrive pas toujours, et l'assistant restait
        alors « à l'écoute » sans plus jamais répondre à quoi que ce soit.
        On le referme donc aussi dès que le modèle recommence à parler, et dès
        que l'utilisateur reprend la parole.
        """
        if not self._interrupted:
            return
        self._interrupted = False
        self._drain_spoken_text_queue()
        if self._speech_display_open:
            self.ui.write_log("[INLINE_END]")
            self._speech_display_open = False
        self._speech_next_text_at = 0.0

    # Un tour coupé côté client continue d'arriver du serveur jusqu'à son
    # propre `turn_complete`. Ce plafond évite qu'un tour sans fin explicite
    # ne rende le tour suivant muet.
    _DISCARD_TURN_MAX_S = 30.0

    def discard_model_audio(self) -> bool:
        """True tant que l'audio du tour coupé doit être jeté.

        Distinct de `_interrupted` : ce dernier sert de porte micro et se
        referme dès que l'utilisateur reparle. Sans cette séparation, un
        simple bruit après « Stop » rouvrait la porte et la réponse coupée
        reprenait en plein milieu, bouton Arrêter réaffiché.
        """
        if getattr(self, "_interrupted", False):
            return True
        if not getattr(self, "_discard_turn_audio", False):
            return False
        if time.monotonic() - getattr(self, "_discard_turn_audio_since", 0.0) > self._DISCARD_TURN_MAX_S:
            self._discard_turn_audio = False
            return False
        return True

    def _end_discarded_turn(self) -> None:
        self._discard_turn_audio = False

    def interrupt(self) -> None:
        """Stop JARVIS mid-speech / mid-thinking: drain queued audio and open mic immediately."""
        self._speech_output_epoch = getattr(self, "_speech_output_epoch", 0) + 1
        # Le rejet du contenu entrant ne s'arme QUE si un tour du modèle est
        # réellement en vol ou si l'IA réfléchit. `_is_speaking` reste vrai tant
        # que la file de lecture locale se vide, donc bien après le turn_complete du modèle.
        if self._model_turn_active:
            self._interrupted = True
            # L'audio de CE tour est jeté jusqu'à son turn_complete, même si
            # la porte micro (`_interrupted`) est refermée entre-temps parce
            # que l'utilisateur a repris la parole.
            self._discard_turn_audio = True
            self._discard_turn_audio_since = time.monotonic()
        elif (
            getattr(self, "_is_thinking", False)
            or getattr(self, "_is_speaking", False)
        ):
            # Avec ElevenLabs, le modèle Live peut avoir fini alors que le
            # flux TTS continue encore à remplir audio_in_queue. Sans ce
            # drapeau, « ANO stop » vidait seulement le tampon actuel puis la
            # lecture repartait avec les paquets arrivés juste après.
            self._interrupted = True
        self._is_thinking = False
        # Un tour audio « en attente de réponse » n'a plus lieu d'être : il
        # retenait la prochaine commande texte jusqu'à un turn_complete qui ne
        # viendrait pas.
        self._audio_turn_pending = False
        self._audio_turn_active = False

        if hasattr(self, "thought_streamer"):
            try:
                self.thought_streamer.interrupt()
            except Exception:
                pass

        # Débloque immédiatement toute attente de soumission de tour texte en vol
        turn_task = getattr(self, "_active_turn_task", None)
        if turn_task is not None and not turn_task.done():
            turn_task.cancel()
            self._active_turn_task = None

        # Les actions sont lancées comme tâches enfants afin de pouvoir les
        # interrompre sans annuler la coroutine qui reçoit la session Live.
        # On ne touche jamais à cette dernière : fermer la session pour un
        # simple clic « Arrêter » rendrait ANO indisponible au tour suivant.
        active_tools = getattr(self, "_active_tool_tasks", ())
        for task in tuple(active_tools):
            if not task.done():
                task.cancel()

        # Signale à la boucle de lecture de fermer son tampon dès la fin du petit
        # bloc courant. Appeler `stream.abort()` depuis un autre thread pendant
        # `stream.write()` faisait remonter PortAudioError au TaskGroup et
        # fermait toute l'application.
        abort_event = getattr(self, "_audio_abort_event", None)
        if abort_event is not None:
            abort_event.set()
        q = self.audio_in_queue
        if q:
            drained = 0
            while True:
                try:
                    q.get_nowait()
                    drained += 1
                except Exception:
                    break
            if drained:
                print(f"[JARVIS] ✋ Interrupted — {drained} audio chunks discarded")
        if self._speech_display_open:
            self.ui.write_log("[INLINE_END]")
            self._speech_display_open = False
        self._reset_speech_sync()
        self.set_speaking(False)
        if self._turn_done_event:
            self._turn_done_event.clear()
        ui = getattr(self, "ui", None)
        if ui is not None and hasattr(ui, "set_state") and not getattr(ui, "muted", False):
            ui.set_state("LISTENING")
        if ui is not None and hasattr(ui, "write_log"):
            if getattr(self, "_phone_active", False):
                ui.write_log("SYS: Interrupted — écoute du téléphone...")
            elif not getattr(ui, "muted", False):
                ui.write_log("SYS: Interrupted — listening...")
        # Filet : si ni turn_complete ni `interrupted` serveur ne viennent clore
        # le tour coupé, on le déclare fini nous-mêmes. Sinon « écoute » restait
        # affiché alors que tout ce que le modèle renvoyait ensuite était jeté.
        loop = getattr(self, "_loop", None)
        epoch = self._speech_output_epoch
        if loop is not None and loop.is_running():
            try:
                loop.call_later(self._INTERRUPT_RELEASE_S, self._release_stuck_interrupt, epoch)
            except Exception:
                pass

    _INTERRUPT_RELEASE_S = 2.5

    def _release_stuck_interrupt(self, epoch: int) -> None:
        if epoch != getattr(self, "_speech_output_epoch", 0):
            return  # une autre interruption a pris le relais
        if not getattr(self, "_interrupted", False):
            return  # le tour coupé s'est clos normalement
        print("[JARVIS] ✋ Tour interrompu sans fin annoncée — libéré de force.")
        self._model_turn_active = False
        self._is_thinking = False
        self._audio_turn_active = False
        self._audio_turn_pending = False
        self._end_discarded_turn()
        self._clear_interrupted()
        if self._turn_done_event:
            self._turn_done_event.set()

    def reset_audio_and_turn_state(self, source: str = "unknown") -> None:
        """Réinitialise complètement l'état du tour et débloque le micro.

        Appelé sur erreur API, fin de tour, timeout, déconnexion ou watchdog
        pour garantir que hold_live_audio ne reste jamais bloqué indéfiniment.
        """
        # Libération synchrone immédiate des drapeaux pour hold_live_audio
        self._model_turn_active = False
        self._is_thinking = False
        self._audio_turn_active = False
        self._audio_turn_pending = False
        self._interrupted = False
        self._noise_turn = False
        self._activity_open = False
        self._last_model_turn_data_at = 0.0
        self._awaiting_server_since = 0.0

        loop = getattr(self, "_loop", None)
        if loop is not None and loop.is_running():
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None
            if running_loop != loop:
                loop.call_soon_threadsafe(self._do_reset_state_async, source)
                return

        self._do_reset_state_async(source)

    def _do_reset_state_async(self, source: str = "unknown") -> None:
        """Nettoyage approfondi sur l'event loop ou en synchrone."""
        self._model_turn_active = False
        self._is_thinking = False
        self._audio_turn_active = False
        self._audio_turn_pending = False
        self._interrupted = False
        self._discard_turn_audio = False
        self._noise_turn = False
        self._activity_open = False
        self._last_model_turn_data_at = 0.0

        # Signaler l'avortement audio aux threads de lecture
        abort_event = getattr(self, "_audio_abort_event", None)
        if abort_event is not None:
            abort_event.set()

        barge_event = getattr(self, "_barge_stop_event", None)
        if barge_event is not None and hasattr(barge_event, "clear"):
            barge_event.clear()

        # Vider les files d'entrée audio
        q_in = getattr(self, "audio_in_queue", None)
        if q_in:
            while True:
                try:
                    q_in.get_nowait()
                except Exception:
                    break

        # Vider la file de sortie Live pour éviter d'émettre des résidus
        q_out = getattr(self, "out_queue", None)
        if q_out:
            while True:
                try:
                    q_out.get_nowait()
                except Exception:
                    break

        if hasattr(self, "_drain_spoken_text_queue"):
            self._drain_spoken_text_queue()

        if getattr(self, "_speech_display_open", False):
            ui = getattr(self, "ui", None)
            if ui is not None and hasattr(ui, "write_log"):
                ui.write_log("[INLINE_END]")
            self._speech_display_open = False

        if hasattr(self, "_reset_speech_sync"):
            self._reset_speech_sync()

        turn_task = getattr(self, "_active_turn_task", None)
        if turn_task is not None and hasattr(turn_task, "done") and not turn_task.done():
            try:
                turn_task.cancel()
            except Exception:
                pass
            self._active_turn_task = None

        turn_done = getattr(self, "_turn_done_event", None)
        if turn_done is not None:
            turn_done.set()
            turn_done.clear()

        self.set_speaking(False)

        # Le verrou de soumission n'est jamais relâché d'ici : il l'est par la
        # tâche annulée ci-dessus, en sortant de son `async with`. Le forcer
        # depuis un tiers laissait cette tâche relâcher un verrou déjà libre
        # (« RuntimeError: Lock is not acquired ») et perdre son résultat.

        if hasattr(self, "_wake") and hasattr(self._wake, "reset"):
            try:
                self._wake.reset()
            except Exception:
                pass

        ui = getattr(self, "ui", None)
        if ui is not None and hasattr(ui, "set_state") and not getattr(ui, "muted", False):
            ui.set_state("LISTENING")

    def check_audio_watchdog(self, now: float | None = None) -> bool:
        """Vérifie si la capture micro est anormalement bloquée sans I/O (>5s).

        Si hold_live_audio bloque le micro (ex: model_turn_active, thinking,
        interrupted, ou turn_submit_lock) pendant plus de 5.0s sans qu'aucune
        donnée audio ni transcription ne soit reçue ou jouée, déclenche
        reset_audio_and_turn_state pour rétablir l'écoute.

        Retourne True si un auto-recovery a été déclenché, False sinon.
        """
        now_mono = time.monotonic() if now is None else now
        submit_lock = getattr(self, "_turn_submit_lock", None)
        jarvis_speaking = getattr(self, "_is_speaking", False)

        held = hold_live_audio(
            speaking=jarvis_speaking,
            model_turn_active=bool(getattr(self, "_model_turn_active", False)),
            thinking=bool(getattr(self, "_is_thinking", False)),
            interrupted=bool(getattr(self, "_interrupted", False)),
            noise_turn=bool(getattr(self, "_noise_turn", False)),
            text_turn_pending=bool(
                submit_lock is not None and submit_lock.locked()
            ),
        )
        if not held:
            self._last_model_turn_data_at = 0.0
            return False

        last_trigger = getattr(self, "_last_watchdog_trigger", 0.0)
        if now_mono - last_trigger < 2.0:
            return False

        last_io = getattr(self, "_last_model_turn_data_at", 0.0)
        if last_io == 0.0:
            self._last_model_turn_data_at = now_mono
            return False

        if not jarvis_speaking and (now_mono - last_io > 5.0):
            self._last_watchdog_trigger = now_mono
            print(
                f"[JARVIS] ⚠️ Watchdog audio : blocage détecté "
                f"({now_mono - last_io:.1f}s sans I/O) — déblocage du micro."
            )
            _log_watchdog(self, f"tour bloqué {now_mono - last_io:.0f} s sans réponse — micro rendu")
            self.reset_audio_and_turn_state(source="watchdog_inactivity")
            return True

        # Voix « fantôme » : le drapeau parlant reste levé alors que plus
        # aucune tranche PCM n'arrive ni ne joue. Cela arrive quand la
        # session Live tombe en pleine réponse — `turn_complete` n'est jamais
        # reçu, donc la boucle de lecture ne rabaisse jamais le drapeau. Dans
        # cet état le micro est retenu (« speaking »), chaque transcription
        # est refusée et l'assistant paraît endormi bien qu'à l'écoute. Aucun
        # vrai tour audio ne laisse un tel silence : Gemini envoie sa voix
        # plus vite que le temps réel.
        q_in = getattr(self, "audio_in_queue", None)
        queue_empty = True
        if q_in is not None:
            try:
                queue_empty = q_in.empty()
            except Exception:
                queue_empty = True
        if (
            jarvis_speaking
            and queue_empty
            and (now_mono - last_io > STALLED_SPEECH_S)
        ):
            self._last_watchdog_trigger = now_mono
            print(
                f"[JARVIS] ⚠️ Watchdog audio : voix restée « active » "
                f"{now_mono - last_io:.1f}s sans aucun son — tour clos, micro rendu."
            )
            _log_watchdog(self, f"voix restée active {now_mono - last_io:.0f} s sans son — tour clos")
            self.reset_audio_and_turn_state(source="watchdog_stalled_speech")
            return True

        return False

    def _on_mic_device_change(self, device_name: str | None) -> bool:
        """Applique un micro explicitement choisi dans le panneau Audio.

        Le choix est sauvegardé et le flux est rouvert immédiatement. Il ne
        sera plus remplacé par le routeur automatique tant que l'appareil est
        présent ; si l'appareil disparaît, ANO-GPT se replie provisoirement
        sur le système au lieu de rester muet.
        """
        if device_name:
            available = {device.name for device in audio_router.list_input_devices()}
            if device_name not in available:
                self.ui.write_log("ERR : micro sélectionné indisponible ; choix précédent conservé.")
                return False
        audio_router.set_manual_override(device_name or None)
        label = device_name or "micro système automatique"
        self.ui.write_log(f"SYS : micro sélectionné — {label} ; application en cours.")
        if self._mic_reopen_evt is not None:
            self._mic_reopen_evt.set()
        return True

    def _on_mic_sensitivity_change(self, value: float) -> None:
        """value : 0.0 (peu sensible) → 1.0 (très sensible). Ajuste le
        seuil de bruit du VAD en direct, sans rouvrir le flux."""
        if self._preprocessor is None:
            return
        value = max(0.0, min(1.0, float(value)))
        db = -35.0 + value * (-60.0 - -35.0)  # 0.0→-35dB (dur) … 1.0→-60dB (sensible)
        self._preprocessor.noise_threshold = 10 ** (db / 20.0)

    def _on_output_device_change(self, device_name: str | None) -> bool:
        if audio_router.set_output_override(device_name or None):
            label = device_name or "sortie système automatique"
            self.ui.write_log(f"SYS : sortie audio sélectionnée — {label}.")
            return True
        else:
            self.ui.write_log("ERR : sortie audio indisponible ; choix précédent conservé.")
            return False
    # ── Marqueurs de tour (VAD client) ──────────────────────────────────────────

    def _activity_start(self) -> None:
        """Ouvre un tour de parole. À appeler depuis la boucle asyncio."""
        if self._activity_open:
            return
        # Filet half-duplex final : une reprise de son captée pendant la voix
        # d'ANO est de l'écho, jamais une nouvelle demande. Sans ce garde, une
        # brève fenêtre entre la génération et le démarrage de PortAudio pouvait
        # ouvrir un tour STT fantôme, puis déstabiliser la session Live.
        submit_lock = getattr(self, "_turn_submit_lock", None)
        # Après « Interrompre », le tour coupé peut ne jamais recevoir de
        # turn_complete : son `_model_turn_active` ne doit pas empêcher la
        # parole suivante d'ouvrir un tour, sinon le micro reste vert et mort.
        interrupted = bool(getattr(self, "_interrupted", False))
        if (
            getattr(self, "_is_speaking", False)
            or (not interrupted and getattr(self, "_is_thinking", False))
            or (not interrupted and getattr(self, "_model_turn_active", False))
            or (not interrupted and getattr(self, "_audio_turn_active", False))
            or (submit_lock is not None and submit_lock.locked())
        ):
            return
        # Second filet contre le drapeau bloqué : si l'utilisateur reprend la
        # parole, l'interruption précédente est close de fait. Sans cela, une
        # seule interruption dont le turn_complete ne revient jamais rendait
        # l'assistant définitivement muet — il écoutait, sans jamais répondre.
        self._clear_interrupted()
        if hasattr(self, "_continuous"):
            self._continuous.on_user_speech_detected()
        self._activity_open = True
        self._activity_since = time.monotonic()
        # Identité immuable du tour : un résultat réseau tardif ne doit jamais
        # pouvoir mettre à jour le texte de la phrase suivante.
        self._stt_turn_id = uuid.uuid4().hex
        self._probe_ambient()
        # has_speech() n'ouvre qu'après cette durée de preuves consécutives ;
        # le pré-roll contient ces trames même si le marqueur part maintenant.
        self._voice_evidence_ms = float(AudioPreprocessor._ATTACK_MS)
        self._last_voice_evidence_ms = 0.0
        self._last_voice_audio_ms = 0.0
        # Chaque activité a sa propre transcription vérifiée. Ne jamais
        # laisser une phrase précise d'un tour précédent guider le suivant.
        self._precision_turn_text = ""
        self._enqueue_out({"activity": "start", "turn_id": self._stt_turn_id})

    def _activity_end(self) -> None:
        """Ferme le tour ouvert : c'est CE message qui déclenche le traitement
        côté modèle. Sans lui, la phrase reste en suspens indéfiniment."""
        if not self._activity_open:
            return
        now = time.monotonic()
        self._last_voice_evidence_ms = self._voice_evidence_ms
        self._last_voice_audio_ms = max(
            0.0,
            (now - self._activity_since) * 1000.0 + AudioPreprocessor._ATTACK_MS,
        )
        self._activity_open = False
        try:
            chunks = list(self._voice_chunks)
            if chunks:
                from core.prosody import get_prosody_manager

                manager = get_prosody_manager()
                instruction, profile = manager.live_turn_instruction(
                    np.concatenate(chunks),
                    SEND_SAMPLE_RATE,
                    active_window=(self._ambient_fields or {}).get("Fenêtre", ""),
                )
                self._enqueue_out({"activity": "prosody", "text": instruction})
                changed = profile.mode != self._prosody_mode
                self._prosody_mode = profile.mode
                if changed and getattr(self, "ui", None) is not None:
                    self.ui.write_log(f"SYS : prosodie adaptative — {profile.mode}.")
        except Exception as exc:
            # La prosodie est un enrichissement : jamais une raison de perdre
            # le marqueur de fin et de bloquer toute la conversation.
            print(f"[Prosodie] analyse acoustique ignorée : {exc}")
        self._enqueue_out({
            "activity": "end",
            "turn_id": getattr(self, "_stt_turn_id", ""),
            "_voice_evidence_ms": self._last_voice_evidence_ms,
            "_queue_depth_at_end": (
                getattr(self, "out_queue", None).qsize()
                if getattr(self, "out_queue", None) is not None else 0
            ),
        })
        # À partir d'ici, le serveur nous doit une réaction (transcription,
        # réponse ou appel d'outil). La veille de vivacité s'en sert.
        self._awaiting_server_since = now
        self._check_speaker()
        if hasattr(self, "_proactive"):
            self._proactive.wake()

    def _activity_cancel(self) -> None:
        """Abandonne un faux départ sans le faire transcrire.

        Une activité confirmée juste avant le premier PCM de la réponse est
        nécessairement un écho ou du bruit. La fermer comme une phrase réelle
        faisait attendre une transcription inexistante, puis provoquait une
        reconnexion vocale inutile.
        """
        if not self._activity_open:
            return
        self._activity_open = False
        self._voice_chunks.clear()
        self._voice_evidence_ms = 0.0
        self._last_voice_evidence_ms = 0.0
        self._last_voice_audio_ms = 0.0
        self._precision_turn_text = ""
        self._enqueue_out({"activity": "cancel", "turn_id": getattr(self, "_stt_turn_id", "")})

    def _enqueue_out(self, msg: dict) -> None:
        """Dépose dans la file de sortie sans jamais bloquer le thread audio."""
        if (hasattr(self, "_speech_output_epoch") and isinstance(msg, dict)
                and (msg.get("activity") or msg.get("mime_type", "").startswith("audio/"))):
            msg = {**msg, "_audio_epoch": getattr(self, "_speech_output_epoch", 0),
                   "_audio_drop_count": getattr(self, "_audio_drop_count", 0),
                   "_audio_source": "phone" if getattr(self, "_phone_active", False) else "pc",
                   # Une file réseau bloquée ne doit jamais transformer une
                   # ancienne phrase en nouvelle commande. Le consommateur STT
                   # rejette explicitement les paquets trop vieux.
                   "_captured_at": time.monotonic()}
        if isinstance(msg, dict) and (msg.get("activity") or msg.get("mime_type", "").startswith("audio/")):
            msg = {**msg, "turn_id": msg.get("turn_id") or getattr(self, "_stt_turn_id", "")}
        q = self.out_queue
        if q is None:
            return
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            self._audio_drop_count = getattr(self, "_audio_drop_count", 0) + 1
            # La file est pleine (réseau à la traîne). On sacrifie de l'audio,
            # jamais un marqueur de tour : le perdre laisserait la phrase
            # bloquée sans réponse.
            if msg.get("activity"):
                try:
                    # Ne jamais jeter `activity_start` pour faire entrer
                    # `activity_end` : le serveur attendrait alors un tour qui
                    # n'a officiellement jamais commencé, donc aucun retour.
                    for index, queued in enumerate(q._queue):
                        if not (isinstance(queued, dict) and queued.get("activity")):
                            del q._queue[index]
                            break
                    else:
                        return
                    q.put_nowait(msg)
                except Exception:
                    pass

    async def _listen_audio_legacy(self):
        print("[JARVIS] 🎤 Mic started (PCM fidèle, VAD séparé)")
        loop = asyncio.get_running_loop()
        preprocessor = AudioPreprocessor()
        self._preprocessor = preprocessor  # réglable en direct par le panneau Audio de l'UI

        # Le VAD décide si une phrase peut partir, sans modifier le signal.
        # Empiler le DSP spectral, l'AGC puis RNNoise détruisait les consonnes.
        # La capture principale transmet désormais les échantillons du micro.
        self._noise_suppressor = None
        self.ui.write_log("MIC : capture fidèle — aucun filtre spectral ni gain logiciel empilé.")

        # ── Full-duplex AEC ───────────────────────────────────────────────
        # Si libspeexdsp est disponible, on fonctionne en full-duplex : le
        # micro reste ouvert en permanence, l'écho du haut-parleur est
        # soustrait en temps réel, et le VAD Silero détecte la voix de
        # l'utilisateur sur le signal nettoyé. Sinon → half-duplex classique.
        _duplex_filter = get_full_duplex_filter()
        _aec_mode = _duplex_filter.aec_available
        self._duplex_filter = _duplex_filter  # accessible par _play_audio
        if _aec_mode:
            print("[ANO-GPT] 🔇 AEC actif — émission micro bloquée pendant la réponse")
        else:
            print("[JARVIS] 🔇↔🎤 Half-duplex (fallback) — micro coupé pendant la parole")

        _cb_state = {"last_err_log": 0.0, "err_count": 0, "level": 0.06,
                     "last_voice": time.monotonic(), "last_voice_raw": time.monotonic(), "barge_stop_quiet": 0,
                     "last_held_log": 0.0, "last_held_state": None,
                     # Le callback PortAudio et la boucle asyncio ne sont pas
                     # atomiques. Ce drapeau interdit d'expédier du PCM entre
                     # la demande d'ouverture et l'ouverture réelle du tour.
                     "activity_opening": False,
                     # Surdité : niveau vocal soutenu sans qu'aucun tour ne
                     # s'ouvre. Compteur à décroissance (+1 fort, −1 sinon).
                     "loud_run": 0, "deaf_at": 0.0}

        # Tampon de pré-amorce : garde les ~400 dernières ms d'audio pour les
        # réinjecter dès que la parole est confirmée. Sans lui, le portier
        # couperait la première syllabe de chaque phrase (« ouvre » → « uvre »).
        _preroll_chunks = max(1, int(0.4 * SEND_SAMPLE_RATE / CHUNK_SIZE))
        preroll = collections.deque(maxlen=_preroll_chunks)

        # Offline wake word: while muted, audio is scanned locally by Vosk and
        # nothing is streamed to Gemini until the wake phrase is heard.
        self._wake = WakeWordDetector()
        if self._wake.available:
            print("[ANO-GPT] 👂 Wake word actif (hors-ligne) — dis « Ano » pour réveiller")
            self.ui.write_log("SYS : mot d'activation actif — dis « Ano » pour réveiller.")
        else:
            print("[ANO-GPT] ⚠️ Vosk natif indisponible — écoute continue maintenue")
            self.ui.write_log(
                "SYS : Vosk natif désactivé pour stabilité — écoute continue active."
            )

        def callback(indata, frames, time_info, status):
            try:
                if self._phone_active:
                    # Le téléphone possède alors le tour audio. Ne surtout pas
                    # le fermer depuis le callback PC : cela tronquait ANO
                    # Remote quelques millisecondes après son activity_start.
                    # Mais si l'utilisateur parle fort dans le micro du PC
                    # pendant ce temps, il doit savoir pourquoi rien ne part.
                    raw_channel = indata[:, 0] if indata.ndim > 1 else indata
                    rms_pc = float(np.sqrt(np.mean((raw_channel.astype(np.float32) / 32768.0) ** 2))) if raw_channel.size else 0.0
                    if rms_pc >= 0.03:
                        _cb_state["phone_loud"] = _cb_state.get("phone_loud", 0) + 1
                    else:
                        _cb_state["phone_loud"] = max(0, _cb_state.get("phone_loud", 0) - 1)
                    now_p = time.monotonic()
                    if (_cb_state["phone_loud"] >= 30
                            and now_p - _cb_state.get("phone_loud_at", 0.0) >= 30.0):
                        _cb_state["phone_loud_at"] = now_p
                        _cb_state["phone_loud"] = 0
                        self.ui.write_log(
                            "SYS : le micro d'ANO Remote (téléphone) est actif — le micro "
                            "du PC est ignoré tant qu'il diffuse. Coupe le micro sur le téléphone."
                        )
                    return

                if getattr(status, "input_overflow", False):
                    # Même compteur que les pertes de file réseau : le tour
                    # concerné sera refusé au lieu de transcrire un son troué.
                    self._audio_drop_count = getattr(self, "_audio_drop_count", 0) + 1

                # Robust channel extraction (1D or 2D)
                raw_channel = indata[:, 0] if indata.ndim > 1 else indata

                if self.ui.muted:
                    # Micro coupé en pleine phrase : on ferme le tour resté
                    # ouvert, sinon ce qui a déjà été envoyé n'est jamais traité.
                    if self._activity_open:
                        loop.call_soon_threadsafe(self._activity_end)
                    # Muted: run only the local wake-word detector. Nothing here
                    # ever reaches the network.
                    if (not getattr(self.ui, "microphone_locked", False)
                            and self._wake_enabled and self._wake is not None
                            and self._wake.available):
                        pcm16 = raw_channel if raw_channel.dtype == np.int16 \
                            else (np.clip(raw_channel, -1, 1) * 32767).astype(np.int16)
                        if self._wake.process(np.ascontiguousarray(pcm16)):
                            loop.call_soon_threadsafe(self._wake_up, "mot d'activation")
                        else:
                            # Vosk transcrit déjà tout ce qui se dit ici : une
                            # routine reconnue dans cet énoncé s'exécute sans
                            # réveiller la session, donc sans réseau ni modèle.
                            heard = self._wake.take_final()
                            if heard:
                                loop.call_soon_threadsafe(
                                    self._maybe_routine, heard, "voix hors-ligne")
                    return

                float_audio = raw_channel.astype(np.float32) / 32768.0

                with self._speaking_lock:
                    jarvis_speaking = self._is_speaking

                # Détection de parole : en mode AEC, le VAD est appliqué sur le
                # signal nettoyé (sans écho) ; en half-duplex, sur le micro brut.
                is_speaking_detected = preprocessor.has_speech(float_audio, strict=jarvis_speaking)

                # « ANO stop » / « attends » sont traités entièrement en local.
                # On vide leur pré-roll et on attend la retombée du VAD avant de
                # rouvrir l'émission ; la commande ne déclenche donc aucune
                # réponse parasite. Une phrase « non, cherche plutôt… » n'arme
                # pas cet événement et poursuit immédiatement vers Gemini.
                stop_event = getattr(self, "_barge_stop_event", None)
                if stop_event is not None and stop_event.is_set():
                    preroll.clear()
                    rms = float(np.sqrt(np.mean(float_audio ** 2))) if float_audio.size else 0.0
                    quiet_floor = max(
                        float(getattr(preprocessor, "noise_threshold", 0.01)) * 1.5,
                        0.012,
                    )
                    if rms < quiet_floor:
                        _cb_state["barge_stop_quiet"] += 1
                    else:
                        _cb_state["barge_stop_quiet"] = 0
                    # Trois chunks bruts silencieux (~192 ms) évitent qu'un
                    # creux entre « ANO » et « stop » rouvre le réseau.
                    if _cb_state["barge_stop_quiet"] >= 3:
                        stop_event.clear()
                        _cb_state["barge_stop_quiet"] = 0
                    if self._activity_open:
                        loop.call_soon_threadsafe(self._activity_end)
                    return

                # Niveau micro réel → l'orbe suit la voix de l'utilisateur.
                #
                # En mode AEC, le niveau est mesuré sur le signal nettoyé pour
                # que l'orbe ne réagisse pas à l'écho du haut-parleur.
                # En half-duplex, même comportement qu'avant.
                if not jarvis_speaking or _aec_mode:
                    mic_rms = float(np.sqrt(np.mean(float_audio ** 2))) if float_audio.size else 0.0
                    target = min(1.0, mic_rms * 8.0)
                    if not is_speaking_detected:
                        target = min(target, 0.18)
                    prev = _cb_state["level"]
                    coeff = 0.55 if target > prev else 0.25
                    level = prev + (target - prev) * coeff
                    _cb_state["level"] = level
                    if not jarvis_speaking:
                        self.ui.set_volume(max(0.05, level))

                # Garder exactement les syllabes et leur enveloppe, y compris
                # les consonnes non voisées et les hésitations du pré-roll.
                # La décision du VAD ne sert pas de masque sur ces échantillons.
                capture_audio = float_audio

                # Rien ne part vers Gemini pendant la voix, la réflexion ou un
                # tour modèle : seul un mot-clé local (ano / stop / arrête-toi /
                # écoute) a le droit de couper, via le flux AEC séparé.
                submit_lock = getattr(self, "_turn_submit_lock", None)
                m_active = bool(getattr(self, "_model_turn_active", False))
                m_thinking = bool(getattr(self, "_is_thinking", False))
                m_interrupted = bool(getattr(self, "_interrupted", False))
                m_noise = bool(getattr(self, "_noise_turn", False))
                m_text_locked = bool(submit_lock is not None and submit_lock.locked())

                is_held = hold_live_audio(
                    speaking=jarvis_speaking,
                    model_turn_active=m_active,
                    thinking=m_thinking,
                    interrupted=m_interrupted,
                    noise_turn=m_noise,
                    text_turn_pending=m_text_locked,
                )

                now_cb = time.monotonic()
                if is_held and hasattr(self, "check_audio_watchdog"):
                    self.check_audio_watchdog(now=now_cb)

                held_reasons = []
                if jarvis_speaking:
                    held_reasons.append("speaking")
                if m_active:
                    held_reasons.append("model_turn_active")
                if m_thinking:
                    held_reasons.append("thinking")
                if m_interrupted:
                    held_reasons.append("interrupted")
                if m_noise:
                    held_reasons.append("noise_turn")
                if m_text_locked:
                    held_reasons.append("text_locked")
                reason_str = "+".join(held_reasons) if held_reasons else "open"

                current_gate_state = (is_held, reason_str)
                if (
                    current_gate_state != _cb_state.get("last_held_state")
                    or (is_held and (now_cb - _cb_state.get("last_held_log", 0.0) >= 4.0))
                ):
                    _cb_state["last_held_log"] = now_cb
                    _cb_state["last_held_state"] = current_gate_state
                    if is_held:
                        mic_level = _cb_state.get("level", 0.0)
                        print(f"[AUDIO-GATE] 🔒 HELD ({reason_str}) | level={mic_level:.3f} | speaking_detected={is_speaking_detected}")
                        # Au-delà de 8 s retenu pendant que l'utilisateur
                        # parle, l'état doit être visible dans le journal de
                        # l'interface : c'est là qu'on lit les blocages.
                        held_since = _cb_state.setdefault("held_since", now_cb)
                        if is_speaking_detected and now_cb - held_since >= 8.0:
                            _cb_state["held_since"] = now_cb
                            self.ui.write_log(
                                f"SYS : micro retenu ({reason_str}) depuis {now_cb - held_since:.0f} s "
                                "alors que vous parlez — déblocage."
                            )
                            self.reset_audio_and_turn_state(source="held_while_speaking")
                if not is_held:
                    _cb_state.pop("held_since", None)

                if is_held:
                    if self._activity_open:
                        loop.call_soon_threadsafe(self._activity_cancel)
                    preroll.clear()
                    return

                # ── PORTIER : rien ne part tant que la parole n'est pas confirmée ──
                # Auparavant, le silence et les bruits de fond étaient envoyés à
                # Gemini (simplement atténués) : son VAD serveur les prenait pour
                # de la parole et le modèle répondait à du vide. Ici le bruit ne
                # quitte jamais la machine.
                if not is_speaking_detected:
                    # Surdité : le micro reçoit un niveau de voix soutenu mais
                    # le portier ne s'ouvre jamais. Les planchers adaptatifs du
                    # VAD (bruit ambiant appris, pic glissant) peuvent s'être
                    # calés sur la propre voix de l'assistant ou sur un bruit
                    # passager ; l'utilisateur parle alors dans le vide, sans
                    # aucune trace. Trois secondes de niveau vocal sans tour
                    # ouvert : on l'écrit dans le journal et on repart d'un
                    # état de VAD neuf, comme après un changement de micro.
                    rms_now = float(np.sqrt(np.mean(float_audio ** 2))) if float_audio.size else 0.0
                    loud_gate = max(0.02, float(getattr(preprocessor, "noise_threshold", 0.01)) * 2.0)
                    if rms_now >= loud_gate and not self._activity_open:
                        _cb_state["loud_run"] += 1
                    else:
                        _cb_state["loud_run"] = max(0, _cb_state["loud_run"] - 1)
                    if (_cb_state["loud_run"] >= _DEAF_CHUNKS
                            and now_cb - _cb_state["deaf_at"] >= 20.0):
                        _cb_state["deaf_at"] = now_cb
                        _cb_state["loud_run"] = 0
                        floor = float(getattr(preprocessor, "_noise_floor", 0.0))
                        ambient = float(getattr(preprocessor, "_ambient_floor", 0.0))
                        prob = float(getattr(preprocessor, "_last_neural_probability", 0.0))
                        self.ui.write_log(
                            f"SYS : le micro entend un niveau vocal ({rms_now:.3f}) mais rien ne "
                            f"s'ouvre (plancher bruit {floor:.3f}, ambiant {ambient:.3f}, "
                            f"VAD {prob:.2f}) — réinitialisation de la détection."
                        )
                        try:
                            preprocessor.reset_stream_state()
                        except Exception:
                            pass
                    # Fin de phrase : on ferme le tour. Ce marqueur est ce qui
                    # dit au modèle « c'est à toi » — c'est exactement ce qui
                    # manquait, et pourquoi la parole n'était jamais traitée.
                    if self._activity_open:
                        loop.call_soon_threadsafe(self._activity_end)
                    # On conserve les derniers chunks : quand la parole sera
                    # confirmée, ils seront réinjectés en tête pour ne pas couper
                    # la première syllabe (le VAD a besoin de ~60 ms pour verrouiller).
                    preroll.append(capture_audio)
                    return

                # ── Fin de phrase, hors du verrou du VAD ───────────────────────
                # `has_speech()` est hystérétique : une fois `_speech_active`
                # vrai, l'harmonicité n'est plus réévaluée (core/stt.py, la
                # condition `not self._speech_active`), pour ne pas couper les
                # consonnes sourdes. Sous un bruit continu que webrtcvad tient
                # pour voisé, ce verrou ne retombe jamais : le tour restait
                # ouvert indéfiniment, le serveur n'avait donc jamais la main et
                # PLUS AUCUNE voix ne sortait — briefing compris.
                #
                # `is_voice_like()` répond sans mémoire, sur périodicité et
                # planéité spectrale en plus de l'énergie. Un simple seuil
                # d'énergie ne suffisait pas : la pluie est forte ET continue,
                # elle le franchit sans peine. Les cordes vocales, elles, sont
                # périodiques — pas une averse.
                now = time.monotonic()
                voice_now = preprocessor.is_voice_like(
                    float_audio,
                    opening=not self._activity_open,
                )
                if voice_now:
                    # Silence « brut », sans le maintien du VAD : c'est lui
                    # qui mesure la vraie pause après le dernier mot.
                    _cb_state["last_voice_raw"] = now
                if voice_now or is_speaking_detected:
                    _cb_state["last_voice"] = now
                    if self._activity_open:
                        self._voice_evidence_ms += (
                            len(float_audio) / SEND_SAMPLE_RATE * 1000.0
                        )

                if (self._activity_open
                        and now - _cb_state["last_voice"] > _END_SILENCE_S):
                    loop.call_soon_threadsafe(self._activity_end)
                    preroll.append(capture_audio)
                    return

                # Fin de phrase sémantique : pause courte + phrase déjà conclue
                # par la transcription live. Le maintien de 950 ms ne sert
                # qu'aux phrases encore ouvertes.
                if (self._activity_open
                        and now - _cb_state.get("last_voice_raw", now) > _FAST_END_SILENCE_S
                        and now - self._activity_since > 0.6
                        and preview_looks_complete(
                            getattr(self, "_stt_live_preview", None), now, self._activity_since,
                        )):
                    self._fast_endpoints = getattr(self, "_fast_endpoints", 0) + 1
                    loop.call_soon_threadsafe(self._activity_end)
                    preroll.append(capture_audio)
                    return

                # Garde-fou ultime : un bruit assez fort pour passer la porte
                # d'énergie ci-dessus (musique, télévision) maintiendrait encore
                # le tour ouvert. On coupe au bout de _MAX_UTTERANCE_S pour que
                # ce qui a été dit soit traité, et un nouveau tour s'ouvre juste
                # après.
                if (self._activity_open
                        and now - self._activity_since > _MAX_UTTERANCE_S):
                    loop.call_soon_threadsafe(self._activity_end)

                # Passage silence → parole : on ouvre le tour, puis on vide le
                # tampon de pré-amorce (dans cet ordre : l'audio d'une phrase ne
                # doit jamais précéder son propre marqueur d'ouverture).
                #
                # La réouverture exige de l'énergie vocale RÉELLE, pas seulement
                # le verrou du VAD : celui-ci restant collé sous un bruit continu,
                # le tour qu'on vient de fermer se rouvrait au chunk suivant et
                # le modèle attendait à nouveau indéfiniment la fin de la phrase.
                if not self._activity_open:
                    # Si l'assistant réfléchit ou attend une fin de tour modèle et que l'utilisateur commence à parler :
                    # Une détection acoustique seule ne doit jamais annuler
                    # une réponse : seule une commande locale reconnue le peut.
                    # Un briefing/rappel/texte possède la session jusqu'à son
                    # turn_complete. Envoyer du PCM pendant cette courte
                    # fenêtre créerait deux tours concurrents et Gemini
                    # fermerait le WebSocket (1007). Le pré-roll est conservé ;
                    # la capture reprendra dès que le tour texte sera fini.
                    submit_lock = getattr(self, "_turn_submit_lock", None)
                    if submit_lock is not None and submit_lock.locked():
                        preroll.append(capture_audio)
                        return
                    if not voice_now:
                        preroll.append(capture_audio)
                        return
                    # Ne jamais mettre le premier PCM dans la file avant que
                    # `_activity_start` ait validé le tour sur la boucle
                    # asyncio. Sinon, un changement d'état (outil/modèle qui
                    # démarre entre les deux threads) faisait entendre une
                    # bribe au modèle sans marqueur `activity:start` : il
                    # pouvait appeler un outil hors sujet puis ne plus répondre.
                    if not _cb_state["activity_opening"]:
                        _cb_state["activity_opening"] = True

                        def _open_activity() -> None:
                            _cb_state["activity_opening"] = False
                            self._activity_start()

                        loop.call_soon_threadsafe(_open_activity)
                    preroll.append(capture_audio)
                    return

                if preroll:
                    lead = np.concatenate(list(preroll))
                    preroll.clear()
                    # Le même pré-roll doit parvenir à la vérification 3.5 et
                    # à l'identification du locuteur. Sans lui, « supprime »
                    # pouvait être réécouté comme « prime » alors que Gemini
                    # Live, lui, avait bien reçu la première syllabe.
                    self._voice_chunks.append(lead.copy())
                    loop.call_soon_threadsafe(
                        self._enqueue_out,
                        {"data": pcm16_bytes(lead),
                         "mime_type": "audio/pcm;rate=16000"}
                    )

                pcm_bytes = pcm16_bytes(capture_audio)
                try:
                    self.ui.feed_audio_spectrum(pcm_bytes, SEND_SAMPLE_RATE, False)
                except Exception:
                    pass
                bus = getattr(self, "_event_bus", None)
                if bus is not None:
                    bus.publish_sync(AudioCaptureFrameEvent(
                        pcm_bytes=pcm_bytes,
                        rms=float(np.sqrt(np.mean(capture_audio ** 2))) if capture_audio.size else 0.0,
                    ))

                # Une copie de la parole confirmée sert à reconnaître la voix.
                # Ici, on ne fait qu'empiler une référence : le calcul, lui,
                # attend la fin du tour et un thread à part.
                self._voice_chunks.append(capture_audio)

                # Queue clean audio PCM for Gemini Live
                loop.call_soon_threadsafe(
                    self._enqueue_out,
                    {"data": pcm_bytes, "mime_type": "audio/pcm;rate=16000"}
                )
            except Exception as e:
                _cb_state["err_count"] += 1
                now = time.monotonic()
                if now - _cb_state["last_err_log"] > 5.0:
                    _cb_state["last_err_log"] = now
                    print(f"[JARVIS] ⚠️ Mic callback error (x{_cb_state['err_count']} since last log): {e}")

        # ── Micro intelligent : choix auto, hot-plug, jamais de piège HFP ────
        # Voir core/audio_router.py. sounddevice/PortAudio ne peut pas cibler
        # une source PulseAudio précise par device= : on oriente PipeWire via
        # `pactl set-default-source` puis on ouvre le flux par défaut, ce qui
        # implique de fermer/rouvrir l'InputStream à chaque changement.
        _mic_reopen_evt = threading.Event()
        self._mic_reopen_evt = _mic_reopen_evt  # réutilisé par _on_mic_device_change (panneau UI)
        _mic_state = {"raw_name": None}
        _barge_listener = None
        _portaudio_input = audio_router.portaudio_input_device(sd)

        def _speaking_now() -> bool:
            # Le barge-in possède son propre flux AEC. Il doit néanmoins
            # respecter exactement le même mute que le micro principal.
            if self.ui.muted:
                return False
            with self._speaking_lock:
                # L'interruption acoustique ne s'arme que lorsqu'un son est
                # réellement diffusé. Armer pendant la réflexion faisait
                # interpréter la fin de la phrase précédente (« oui ») comme
                # une interruption au tout début de la réponse.
                return self._is_speaking

        def _start_local_interrupt_listener(raw_name: str) -> None:
            """Ouvre un second flux, exclusivement AEC → Vosk local.

            Le flux principal est déjà ouvert sur `raw_name`. On bascule la
            source par défaut seulement le temps d'ouvrir ce second flux, puis
            on la remet immédiatement : les deux consommateurs restent isolés.
            L'initialisation s'exécute en arrière-plan pour ne pas bloquer
            l'ouverture du flux microphone principal.
            """
            if not raw_name:
                return

            def _loader() -> None:
                nonlocal _barge_listener
                detector = InterruptPhraseDetector(
                    model=getattr(self._wake, "_model", None),
                    isolated=getattr(self._wake, "_isolated", None),
                    sample_rate=SEND_SAMPLE_RATE,
                    addressed_only=True,
                )
                if detector.available:
                    detector.wait_ready(35.0)
                if not detector.available:
                    reason = getattr(detector, "fail_reason", "") or "détecteur local inactif"
                    self.ui.write_log(
                        "SYS : interruption vocale locale indisponible "
                        f"({reason}) ; seuls Échap ou Arrêter coupent la voix. "
                        "Les bruits et les phrases ordinaires ne déclenchent aucune interruption."
                    )
                    return
                aec_name = audio_router.enable_echo_cancel(raw_name)
                if not aec_name:
                    detector.close()
                    print("[ANO-GPT] ⚠️ Interruption vocale inactive (AEC PipeWire indisponible).")
                    return
                try:
                    if not audio_router.set_default_source(aec_name):
                        detector.close()
                        return

                    def _local_interrupt(kind: str = "redirect", text: str = "") -> None:
                        # Double verrou : le mute peut être activé entre la
                        # détection locale et l'exécution de ce callback.
                        if self.ui.muted:
                            return
                        # Un arrêt pur est consommé localement : il ne doit pas
                        # repartir au modèle et provoquer un « d'accord » inutile.
                        # Une redirection garde son pré-roll et sera transmise dès
                        # que interrupt() a rouvert le chemin micro.
                        if kind == "stop":
                            self._barge_stop_event.set()
                            _cb_state["barge_stop_quiet"] = 0
                        bus = getattr(self, "_event_bus", None)
                        if bus is not None:
                            bus.publish_sync(BargeInDetectedEvent(
                                trigger_type=f"local-{kind}", confidence=1.0,
                            ))
                        loop.call_soon_threadsafe(self.interrupt)
                        loop.call_soon_threadsafe(
                            self.ui.write_log,
                            f"SYS : barge-in local/{kind} — {text or 'commande vocale'}",
                        )

                    _barge_listener = LocalBargeInListener(
                        sd, detector, _speaking_now,
                        _local_interrupt,
                        sample_rate=SEND_SAMPLE_RATE, blocksize=320,
                        input_device=_portaudio_input,
                        is_enabled=lambda: not self.ui.muted,
                    )
                    if _barge_listener.start():
                        print("[ANO-GPT] ✋ Barge-in actif — ano / stop / arrête-toi / écoute.")
                        self.ui.write_log(
                            "SYS : barge-in actif — dis « Ano stop » ou « Ano écoute » "
                            "pour couper la voix (protection anti-écho active)."
                        )
                    else:
                        _barge_listener.stop()
                        _barge_listener = None
                finally:
                    # Les futurs flux micro normaux doivent toujours réouvrir le
                    # matériel brut, jamais la source virtuelle AEC.
                    audio_router.set_default_source(raw_name)

                if _barge_listener is None:
                    detector.close()
                    audio_router.disable_echo_cancel()

            threading.Thread(target=_loader, name="anogpt-barge-loader", daemon=True).start()

        def _apply_mic_policy() -> None:
            warning = audio_router.default_source_warning()
            if warning:
                self.ui.write_log(f"WARN MIC : {warning}")
            chosen = audio_router.refresh_and_apply()
            print(f"[AudioRouter] {audio_router.describe(chosen)}")
            self.ui.write_log(f"MIC : {audio_router.describe(chosen)}")
            raw_name = chosen.device.name if chosen.device else None
            _mic_state["raw_name"] = raw_name
            if not raw_name:
                return
            # Le flux principal reste directement relié au matériel. La source
            # virtuelle AEC, si disponible, sera ouverte après lui par le seul
            # consommateur Vosk d'interruption, sans devenir le micro normal.
            print("[AudioRouter] Micro matériel direct — routage stable.")

        def _on_hotplug() -> None:
            # La création/destruction de la source AEC émet elle aussi un
            # évènement ``source`` PipeWire. Ne rechargeons la politique que
            # si le meilleur micro matériel a réellement changé : sinon
            # refresh_and_apply() déchargerait l'AEC qui vient d'être ouvert.
            candidate = audio_router.probe_choice()
            if (candidate.device and candidate.device.name
                    == _mic_state.get("raw_name")):
                return
            chosen = audio_router.refresh_and_apply()
            new_raw = chosen.device.name if chosen.device else None
            if new_raw and new_raw != _mic_state.get("raw_name"):
                label = chosen.device.description
                self.ui.write_log(f"MIC : basculé sur {label}.")
                print(f"[AudioRouter] Changement détecté → {audio_router.describe(chosen)}")
                loop.call_soon_threadsafe(_mic_reopen_evt.set)

        watcher = audio_router.HotplugWatcher(on_change=_on_hotplug)
        watcher.start()

        try:
            while True:
                _mic_reopen_evt.clear()
                # Un hot-plug au milieu d'une phrase doit fermer proprement le
                # tour côté serveur. Puis on jette le pré-roll et tout l'état
                # acoustique de l'ancien appareil avant d'en ouvrir un autre.
                if self._activity_open:
                    self._activity_end()
                preroll.clear()
                preprocessor.reset_stream_state()

                # Réinitialiser l'AEC et vérifier le bypass casque à chaque
                # changement de micro. Un changement de périphérique invalide
                # complètement le filtre adaptatif — les coefficients appris
                # sur l'ancien micro/haut-parleur ne correspondent plus.
                _duplex_filter.reset()
                _duplex_filter.check_headphones()
                _aec_mode = _duplex_filter.aec_available

                _apply_mic_policy()

                dev_name = "Default Mic"
                dev_rate = SEND_SAMPLE_RATE
                try:
                    dev_info = sd.query_devices(_portaudio_input, kind='input')
                    dev_name = dev_info.get('name', 'Default Mic')
                    dev_rate = int(dev_info.get('default_samplerate') or SEND_SAMPLE_RATE)
                except Exception:
                    pass

                try:
                    with sd.InputStream(
                        samplerate=SEND_SAMPLE_RATE,
                        channels=CHANNELS,
                        dtype="int16",
                        blocksize=CHUNK_SIZE,
                        callback=callback,
                        device=_portaudio_input,
                        # Un peu de marge matérielle protège le callback audio
                        # des pointes CPU de Qt/Chromium sur cette machine à
                        # deux cœurs, sans modifier les chunks de 64 ms du VAD.
                        latency="high",
                    ):
                        _start_local_interrupt_listener(_mic_state["raw_name"])
                        print(f"[JARVIS] 🎤 Mic stream open — {dev_name} "
                              f"(matériel {dev_rate} Hz rééchantillonné vers {SEND_SAMPLE_RATE} Hz mono int16)")
                        while not _mic_reopen_evt.is_set():
                            await asyncio.sleep(0.2)
                        print("[JARVIS] 🔁 Micro changé — réouverture du flux...")
                        if _barge_listener is not None:
                            _barge_listener.stop()
                            _barge_listener = None
                        audio_router.disable_echo_cancel()
                except Exception as e:
                    print(f"[JARVIS] ❌ Mic: {e}")
                    await asyncio.sleep(2.0)
        finally:
            watcher.stop()
            if _barge_listener is not None:
                _barge_listener.stop()
            audio_router.disable_echo_cancel()
    def _start_barge_in(self, raw_name: str, loop) -> None:
        """Interruption vocale locale pour la capture Mark-LII.

        Second flux PortAudio branché sur la source AEC (la voix d'ANO en est
        soustraite), décodé par Vosk dans un processus isolé : « stop »,
        « arrête-toi », « écoute », « Ano stop » coupent la voix aussitôt.
        Le flux principal, lui, reste strictement half-duplex : rien de ce
        qu'ANO dit ne part au modèle. Tout se charge en arrière-plan.
        """
        if not raw_name or getattr(self, "_barge_listener", None) is not None:
            return

        def _speaking_now() -> bool:
            if self.ui.muted:
                return False
            with self._speaking_lock:
                return self._is_speaking

        def _loader() -> None:
            detector = InterruptPhraseDetector(sample_rate=SEND_SAMPLE_RATE, mode="commands")
            if detector.available:
                detector.wait_ready(35.0)
            if not detector.available:
                reason = getattr(detector, "fail_reason", "") or "détecteur local inactif"
                loop.call_soon_threadsafe(
                    self.ui.write_log,
                    f"SYS : interruption vocale locale indisponible ({reason}) ; "
                    "seuls Échap ou le bouton Interrompre coupent la voix.",
                )
                return
            aec_name = audio_router.enable_echo_cancel(raw_name)
            if not aec_name:
                detector.close()
                loop.call_soon_threadsafe(
                    self.ui.write_log,
                    "SYS : interruption vocale inactive (annulation d'écho PipeWire indisponible).",
                )
                return
            listener = None
            # Le flux d'écoute est épinglé sur la source AEC par PULSE_SOURCE :
            # changer la source par défaut déplaçait aussi le micro principal
            # sous PipeWire, et le flux AEC repartait sur le micro brut au
            # moment de la remise en place.
            previous_source = os.environ.get("PULSE_SOURCE")
            os.environ["PULSE_SOURCE"] = aec_name
            try:
                def _on_interrupt(kind: str = "stop", text: str = "") -> None:
                    if self.ui.muted:
                        return
                    if kind == "stop":
                        self._barge_stop_event.set()
                    bus = getattr(self, "_event_bus", None)
                    if bus is not None:
                        bus.publish_sync(BargeInDetectedEvent(
                            trigger_type=f"local-{kind}", confidence=1.0,
                        ))
                    loop.call_soon_threadsafe(self.interrupt)
                    loop.call_soon_threadsafe(
                        self.ui.write_log, f"SYS : stop vocal — « {text or kind} »",
                    )

                listener = LocalBargeInListener(
                    sd, detector, _speaking_now, _on_interrupt,
                    sample_rate=SEND_SAMPLE_RATE, blocksize=320,
                    input_device=audio_router.portaudio_input_device(sd),
                    is_enabled=lambda: not self.ui.muted,
                )
                if listener.start():
                    self._barge_listener = listener
                    # La référence de l'annulation d'écho : la lecture d'ANO
                    # passe par le puits AEC. Sans ça, Vosk entendait ANO.
                    moved = audio_router.route_own_playback_to_aec()
                    print(f"[ANO-GPT] ✋ Barge-in actif — stop / arrête-toi / écoute / Ano stop "
                          f"(lecture routée via AEC : {moved} flux).")
                    loop.call_soon_threadsafe(
                        self.ui.write_log,
                        "SYS : interruption vocale active — dis « stop », « arrête-toi » ou "
                        "« écoute » pendant qu'ANO parle.",
                    )
                else:
                    listener.stop()
                    listener = None
            finally:
                if previous_source is None:
                    os.environ.pop("PULSE_SOURCE", None)
                else:
                    os.environ["PULSE_SOURCE"] = previous_source
            if listener is None:
                detector.close()
                audio_router.disable_echo_cancel()

        threading.Thread(target=_loader, name="anogpt-barge-loader", daemon=True).start()

    def _stop_barge_in(self) -> None:
        listener = getattr(self, "_barge_listener", None)
        self._barge_listener = None
        if listener is not None:
            try:
                listener.stop()
            except Exception:
                pass
            audio_router.disable_echo_cancel()

    async def _listen_audio(self):
        """Capture Mark-LII : PCM brut directement vers Gemini Live.

        Aucun VAD local, pré-roll, AEC, Vosk, wake word ou seconde passe STT
        ne participe à ce chemin.  Gemini Live reçoit le flux microphone et
        décide lui-même des tours et de la transcription, comme Mark-LII.
        Le seul verrou local est volontairement strict : pendant la sortie de
        l'assistant, aucun octet ne quitte le micro (half-duplex).
        """
        print("[ANO-GPT] 🎤 Capture Mark-LII active — PCM direct vers Gemini Live")
        loop = asyncio.get_running_loop()
        self._preprocessor = None
        self._noise_suppressor = None
        self._wake = None
        self._duplex_filter = None

        # Pendant une lecture musique, le flux ne doit *jamais* atteindre
        # Gemini : seules les trames brutes sont proposées à Porcupine. Il ne
        # s'agit pas d'une transcription de paroles, donc pas de faux « ANO ».
        # La clé est lue une fois au démarrage, hors callback PortAudio.
        try:
            from memory.config_manager import load_config
            _music_cfg = load_config()
            self._music_wake = MusicWakeWordDetector(
                access_key=_music_cfg.picovoice_access_key,
                keyword_paths=_music_cfg.picovoice_keyword_paths,
            )
        except Exception as exc:
            self._music_wake = MusicWakeWordDetector()
            print(f"[ANO-GPT] ⚠️ Mot d'activation musique indisponible : {exc}")
        if getattr(self._music_wake, "available", False):
            self.ui.write_log("SYS : veille musique active — dis « Jarvis » pour reprendre la parole.")
        else:
            self.ui.write_log("SYS : veille musique en attente de la clé Picovoice (mot « Jarvis »).")

        music_gate_visible = False
        music_wake_pending = False

        def _music_is_playing() -> bool:
            try:
                from core.player_ipc import get_player
                return get_player().get_status().get("state") == "playing"
            except Exception:
                return False

        async def _pause_music_then_wake() -> None:
            nonlocal music_wake_pending
            try:
                from core.player_ipc import get_player
                await asyncio.to_thread(get_player().pause)
                self._wake_up("mot d'activation pendant musique")
                self.ui.write_log("SYS : musique mise en pause — écoute active.")
            finally:
                music_wake_pending = False

        reopen = threading.Event()
        self._mic_reopen_evt = reopen
        stop_quiet = {"n": 0}

        def callback(indata, _frames, _time_info, status):
            nonlocal music_gate_visible, music_wake_pending
            try:
                if getattr(status, "input_overflow", False):
                    self._audio_drop_count = getattr(self, "_audio_drop_count", 0) + 1
                if self.ui.muted or getattr(self, "_phone_active", False):
                    return
                samples = indata[:, 0] if getattr(indata, "ndim", 1) > 1 else indata
                # Le verrou est intentionnellement placé avant tout envoi vers
                # Gemini : musique, vidéos et paroles restent locales. Après le
                # mot-clé, on met d'abord le lecteur en pause puis seulement on
                # rouvre l'écoute normale.
                detector = getattr(self, "_music_wake", None)
                # Sans détecteur prêt, ne jamais verrouiller le micro : cela
                # laisserait l'utilisateur sans aucune façon vocale de revenir.
                if (getattr(detector, "available", False)
                        and (_music_is_playing() or music_wake_pending)):
                    if not music_gate_visible:
                        music_gate_visible = True
                        loop.call_soon_threadsafe(
                            self.ui.write_log,
                            "SYS : musique détectée — micro verrouillé, dis « Jarvis » pour parler.",
                        )
                        loop.call_soon_threadsafe(self.ui.set_state, "SLEEPING")
                    if (not music_wake_pending and detector is not None
                            and getattr(detector, "available", False)
                            and detector.process(np.ascontiguousarray(samples))):
                        music_wake_pending = True
                        loop.call_soon_threadsafe(
                            lambda: asyncio.create_task(_pause_music_then_wake())
                        )
                    return
                if music_gate_visible:
                    music_gate_visible = False
                    loop.call_soon_threadsafe(self.ui.set_state, "LISTENING")
                with self._speaking_lock:
                    speaking = self._is_speaking
                # Invariant du projet : la voix d'ANO ne doit jamais rentrer
                # dans le modèle. C'est exactement le callback half-duplex de
                # Mark-LII, sans traitement audio dans le thread PortAudio.
                interrupted = bool(getattr(self, "_interrupted", False))
                if speaking or (
                    (getattr(self, "_model_turn_active", False) or getattr(self, "_is_thinking", False))
                    and not interrupted
                ):
                    return
                # « Stop » vient d'être dit : ce mot-là ne doit pas partir au
                # modèle (il répondrait « d'accord »). On jette le son jusqu'à
                # ~400 ms de calme, puis le micro repart normalement.
                stop_event = getattr(self, "_barge_stop_event", None)
                if stop_event is not None and stop_event.is_set():
                    rms_raw = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2))) if samples.size else 0.0
                    if rms_raw < 400.0:
                        stop_quiet["n"] += 1
                    else:
                        stop_quiet["n"] = 0
                    if stop_quiet["n"] >= 6:
                        stop_quiet["n"] = 0
                        stop_event.clear()
                    return
                data = indata.tobytes()
                loop.call_soon_threadsafe(
                    self._enqueue_out,
                    {"data": data, "mime_type": "audio/pcm;rate=16000"},
                )
                try:
                    rms = float(np.sqrt(np.mean((samples.astype(np.float32) / 32768.0) ** 2))) if samples.size else 0.0
                    self.ui.set_volume(min(1.0, rms * 8.0))
                    self.ui.feed_audio_spectrum(data, SEND_SAMPLE_RATE, False)
                except Exception:
                    pass
            except Exception as exc:
                print(f"[ANO-GPT] ⚠️ Callback micro Mark-LII : {exc}")

        while True:
            reopen.clear()
            # Conserve le sélecteur de micro de l'interface sans conserver le
            # routeur/VAD de l'ancien pipeline.
            try:
                chosen = audio_router.refresh_and_apply()
                if getattr(chosen, "device", None):
                    self.ui.write_log(f"MIC : {audio_router.describe(chosen)}")
            except Exception as exc:
                print(f"[ANO-GPT] ⚠️ Politique micro : {exc}")
            device = audio_router.portaudio_input_device(sd)
            raw_name = None
            try:
                raw_name = chosen.device.name if getattr(chosen, "device", None) else None
            except Exception:
                raw_name = None
            try:
                with sd.InputStream(
                    samplerate=SEND_SAMPLE_RATE,
                    channels=CHANNELS,
                    dtype="int16",
                    blocksize=CHUNK_SIZE,
                    callback=callback,
                    device=device,
                    latency="high",
                ):
                    print("[ANO-GPT] 🎤 Flux micro Mark-LII ouvert (16 kHz mono PCM)")
                    self._start_barge_in(raw_name, loop)
                    try:
                        while not reopen.is_set():
                            await asyncio.sleep(0.2)
                    finally:
                        self._stop_barge_in()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[ANO-GPT] ❌ Micro Mark-LII : {exc}")
                await asyncio.sleep(2.0)

    async def _play_audio(self):
        print("[JARVIS] 🔊 Play started")

        loop = asyncio.get_running_loop()
        # Exécuteur réservé au haut-parleur : une recherche, une capture ou un
        # outil lent ne peut plus occuper les workers du pool asyncio global et
        # provoquer un trou audible entre deux blocs PCM.
        audio_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="anogpt-audio-output",
        )
        stream = None
        last_output_error = 0.0
        spatial_processor = None
        output_channels = CHANNELS
        self._audio_output_underflows = 0
        last_visual_update = 0.0
        try:
            from core.spatial_audio import get_spatial_processor, spatial_audio_requested
            if spatial_audio_requested():
                spatial_processor = get_spatial_processor(RECEIVE_SAMPLE_RATE)
                output_channels = 2
                print("[JARVIS] 🎧 Spatialisation binaurale HRTF active")
        except Exception as exc:
            print(f"[JARVIS] ⚠️ Spatialisation en repli mono : {exc}")

        async def _open_stream():
            nonlocal stream, last_output_error
            while stream is None:
                try:
                    candidate = sd.RawOutputStream(
                        samplerate=RECEIVE_SAMPLE_RATE,
                        channels=output_channels,
                        dtype="int16",
                        # Laisse PortAudio choisir la taille native. Le bloc fixe
                        # de 1024 ne correspondait pas aux paquets de 1200 frames.
                        blocksize=0,
                        # Réserve de 60 ms : absorbe la gigue locale ; la coupure
                        # reste sous un budget nominal de 100 ms sans appeler
                        # PortAudio de façon concurrente depuis `interrupt()`.
                        latency=_OUTPUT_LATENCY_S,
                    )
                    # Le démarrage d'un périphérique peut bloquer ; il partage
                    # le worker de sortie, jamais la boucle de réception Live.
                    await loop.run_in_executor(audio_executor, candidate.start)
                    stream = candidate
                    # Un flux de sortie (ré)ouvert repart sur le puits par
                    # défaut : s'il y a une annulation d'écho, il doit passer
                    # par elle pour que « stop » reste audible.
                    if audio_router.echo_cancel_active():
                        loop.run_in_executor(None, audio_router.route_own_playback_to_aec)
                    # PortAudio négocie sa propre latence : le réglage demandé
                    # n'est qu'un souhait. Relever la valeur réelle garde le
                    # sous-titre calé quel que soit le périphérique.
                    try:
                        measured = float(getattr(candidate, "latency", 0.0) or 0.0)
                    except (TypeError, ValueError):
                        measured = 0.0
                    self._audio_output_latency = (
                        measured if 0.0 < measured < 0.5 else _OUTPUT_LATENCY_S
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    now = time.monotonic()
                    if now - last_output_error > 5.0:
                        last_output_error = now
                        print(f"[JARVIS] ⚠️ Sortie audio indisponible : {exc}")
                        self.ui.write_log("ERR: sortie audio indisponible — nouvelle tentative...")
                    await asyncio.sleep(1.0)

        def _close_stream(abort: bool = False):
            nonlocal stream
            current, stream = stream, None
            if current is None:
                return

            def _shutdown_output():
                try:
                    if abort:
                        current.abort()
                    else:
                        current.stop()
                except Exception:
                    pass
                try:
                    current.close()
                except Exception:
                    pass

            # Fermeture synchrone et bornée : pendant l'annulation d'une tâche,
            # déléguer ceci via asyncio.to_thread laisse un Future orphelin et
            # peut bloquer asyncio.run() plusieurs minutes à l'arrêt.
            _shutdown_output()

        async def _write_chunk(chunk: bytes) -> tuple[bool, bytes]:
            """Écrit un bloc et le rejoue une fois après un hot-plug."""
            nonlocal audio_executor, spatial_processor, output_channels
            rendered = chunk
            reference = chunk
            if spatial_processor is not None:
                try:
                    rendered = await loop.run_in_executor(
                        audio_executor, spatial_processor.process_chunk, chunk
                    )
                    stereo = np.frombuffer(rendered, dtype=np.int16).reshape(-1, 2)
                    reference = np.mean(stereo.astype(np.float32), axis=1).astype(np.int16).tobytes()
                except Exception as exc:
                    print(f"[JARVIS] ⚠️ HRTF désactivé après erreur : {exc}")
                    _close_stream(abort=True)
                    spatial_processor = None
                    output_channels = CHANNELS
                    rendered = reference = chunk
            for attempt in range(2):
                await _open_stream()
                assert stream is not None
                try:
                    underflowed = bool(await loop.run_in_executor(
                        audio_executor, stream.write, rendered
                    ))
                    return underflowed, reference
                except Exception as exc:
                    abort_event = getattr(self, "_audio_abort_event", None)
                    if abort_event is not None and abort_event.is_set():
                        return False, reference
                    if not isinstance(exc, RuntimeError):
                        raise
                    print(f"[JARVIS] 🔁 Réouverture de la sortie audio : {exc}")
                    _close_stream(abort=True)
                    # Un backend PortAudio débranché peut laisser son worker
                    # dans un état inutilisable même après la remontée de
                    # l'exception. Le périphérique ET son worker repartent à
                    # neuf, puis le même bloc est rejoué.
                    old_executor = audio_executor
                    audio_executor = concurrent.futures.ThreadPoolExecutor(
                        max_workers=1,
                        thread_name_prefix="anogpt-audio-output",
                    )
                    old_executor.shutdown(wait=False, cancel_futures=True)
                    if attempt:
                        raise
            return False, reference

        try:
            while True:
                abort_event = getattr(self, "_audio_abort_event", None)
                if abort_event is not None and abort_event.is_set():
                    _close_stream(abort=True)
                    abort_event.clear()
                try:
                    chunk = await asyncio.wait_for(
                        self.audio_in_queue.get(),
                        timeout=0.1
                    )
                    self._last_model_turn_data_at = time.monotonic()
                except asyncio.TimeoutError:
                    self.check_audio_watchdog()
                    if (
                        self._turn_done_event
                        and self._turn_done_event.is_set()
                        and self.audio_in_queue.empty()
                    ):
                        # stop() attend que le tampon matériel soit réellement
                        # joué. Le micro ne se rouvre donc jamais sur les 100
                        # dernières ms de la propre voix de l'assistant.
                        _close_stream(abort=False)
                        self.set_speaking(False)
                        if self._speech_display_open:
                            while self.speech_text_queue and not self.speech_text_queue.empty():
                                self._flush_synced_speech_text(force=True)
                            self.ui.write_log("[INLINE_END]")
                            self._speech_display_open = False
                        self._reset_speech_sync()
                        self._turn_done_event.clear()
                    continue

                # Accumuler quelques tranches avant le premier write évite de
                # lancer PortAudio avec 20 ms puis de le laisser affamé entre
                # deux paquets réseau. Ne consomme pas la queue : interrupt()
                # peut toujours la vider sans laisser de PCM local périmé.
                if stream is None:
                    deadline = loop.time() + _OUTPUT_PREFILL_S
                    target_chunks = max(1, int(_OUTPUT_PREFILL_S * 1000 / _OUTPUT_SLICE_MS) - 1)
                    while self.audio_in_queue.qsize() < target_chunks:
                        if (self._turn_done_event and self._turn_done_event.is_set()) or loop.time() >= deadline:
                            break
                        if abort_event is not None and abort_event.is_set():
                            break
                        await asyncio.sleep(0.005)
                if abort_event is not None and abort_event.is_set():
                    continue

                # Une seule tranche (~20 ms) : le signal d'interruption peut
                # reprendre la main entre chaque bloc sans course PortAudio.
                self.set_speaking(True)
                self._flush_synced_speech_text()
                # Niveau réel de cette tranche (~20 ms) → l'orbe réagit à ce
                # qui est effectivement en train de sortir des haut-parleurs,
                # syllabe par syllabe, au lieu d'un pouls simulé indifférent
                # au contenu de la voix.
                now_visual = time.monotonic()
                update_visual = now_visual - last_visual_update >= 0.04
                try:
                    samples = np.frombuffer(chunk, dtype=np.int16)
                    if samples.size and update_visual:
                        rms = float(np.sqrt(np.mean((samples.astype(np.float32) / 32768.0) ** 2)))
                        self.ui.set_volume(min(1.0, rms * 5.0))
                except Exception:
                    pass
                try:
                    try:
                        if update_visual:
                            self.ui.feed_audio_spectrum(chunk, RECEIVE_SAMPLE_RATE, True)
                            last_visual_update = now_visual
                    except Exception:
                        pass
                    underflowed, speaker_reference = await _write_chunk(chunk)
                    # ── AEC : alimenter la référence haut-parleur ──────────
                    # Chaque chunk 24 kHz envoyé aux haut-parleurs sert de
                    # référence à l'AEC pour la soustraction d'écho. On le
                    # fait juste APRÈS l'écriture pour que le timing reflète
                    # l'instant réel de sortie du son.
                    _fdx = getattr(self, "_duplex_filter", None)
                    if _fdx is not None:
                        try:
                            _fdx.feed_speaker(speaker_reference)
                        except Exception:
                            pass  # Ne jamais bloquer la lecture
                    abort_event = getattr(self, "_audio_abort_event", None)
                    if abort_event is not None and abort_event.is_set():
                        _close_stream(abort=True)
                        abort_event.clear()
                        continue
                    if underflowed:
                        self._audio_output_underflows += 1
                        now = time.monotonic()
                        if now - last_output_error > 5.0:
                            last_output_error = now
                            print(f"[JARVIS] ⚠️ Sous-alimentation audio détectée ({self._audio_output_underflows}).")
                    self._audio_played_sec += len(chunk) / (RECEIVE_SAMPLE_RATE * CHANNELS * 2)
                    self._flush_synced_speech_text()
                except asyncio.CancelledError:
                    raise
                except RuntimeError:
                    # Deux échecs consécutifs indiquent un périphérique réellement
                    # indisponible ; la boucle de reconnexion de session prendra
                    # alors le relais avec une erreur explicite.
                    raise
        except asyncio.CancelledError:
            _tool_ok, _tool_err = False, "CancelledError: délai dépassé ou session arrêtée"
            raise
        except Exception as e:
            print(f"[JARVIS] ❌ Play: {e}")
            raise
        finally:
            self.set_speaking(False)
            _close_stream(abort=True)
            self.reset_audio_and_turn_state("play_audio_exit")
            # Ne jamais bloquer la boucle de reconnexion sur la terminaison
            # interne d'un backend PortAudio débranché. Le stream est déjà
            # abort/close et le worker sort dès son appel courant terminé.
            audio_executor.shutdown(wait=False, cancel_futures=True)
