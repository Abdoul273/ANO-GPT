"""Détection locale des ordres d'interruption pendant le TTS.

Ce module ne communique avec aucun service réseau. Il transcrit uniquement le
flux reçu pendant que l'assistant parle et ne déclenche que sur un petit jeu de
préfixes d'arrêt ou de correction, après nettoyage par ``module-echo-cancel``.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np

from core.wake_word import (
    DEFAULT_MODEL,
    _normalise,
    resolve_vosk_model_path,
    vosk_native_allowed,
)

logger = logging.getLogger("anogpt.barge_in")

# Seuls ces mots (et leurs variantes phonétiques) peuvent couper la voix.
# Tout le reste — conversation, télévision, espagnol halluciné, « 1 2 3 » —
# est du bruit et ne doit jamais interrompre.
_ANO_FORMS = (
    "ano gpt", "anno gpt", "anneau gpt",
    "hey ano", "he ano", "ok ano", "salut ano", "dis ano",
    "ano", "anno", "anneau", "jarvis",
)
_STOP_PREFIXES = (
    "stoppe", "stop",
    "arrete toi", "arretez vous", "arretez", "arretes", "arrete",
)
_LISTEN_PREFIXES = ("ecoutez", "ecoutes", "ecoute")

# Phrases in-vocab pour Vosk (grammaire fermée + [unk]). « ano » est hors
# lexique : on utilise « anneau », qui se prononce pareil.
INTERRUPT_GRAMMAR = (
    "stop",
    "stoppe",
    "arrête",
    "arrête toi",
    "écoute",
    "anneau",
    "anneau stop",
    "anneau arrête",
    "anneau arrête toi",
    "anneau écoute",
    "[unk]",
)

INTERRUPT_PHRASES = tuple(
    f"{name} {cmd}".strip()
    for name in ("ano", "anno", "anneau", "jarvis")
    for cmd in ("", "stop", "arrete", "arrete toi", "ecoute")
) + ("stop", "stoppe", "arrete", "arrete toi", "ecoute")


def hold_live_audio(
    *,
    speaking: bool,
    model_turn_active: bool,
    thinking: bool,
    interrupted: bool,
    noise_turn: bool = False,
    text_turn_pending: bool = False,
) -> bool:
    """True : aucun PCM ne doit partir vers Gemini (mot-clé local seulement)."""
    if noise_turn:
        return True
    # Briefing / rappel / commande texte : le modèle n'a pas encore parlé,
    # `_model_turn_active` est encore faux. Sans ce verrou, le VAD ouvrait un
    # tour sur l'écho naissant, Transcribe échouait, et la session tombait.
    if text_turn_pending:
        return True
    # Une interruption demandée n'implique pas que les haut-parleurs ont fini.
    return speaking or ((model_turn_active or thinking) and not interrupted)


class InterruptPhraseDetector:
    """Vosk local dédié aux seuls débuts de phrases d'interruption."""

    def __init__(self, model=None, model_path: Optional[Path] = None,
                 sample_rate: int = 16000, addressed_only: bool = False,
                 isolated: Optional[Any] = None) -> None:
        self.addressed_only = addressed_only
        self.available = False
        self.fail_reason = ""
        self._rec = None
        self._last_partial = ""
        self.last_text = ""
        self.last_kind = ""
        self._isolated = isolated
        self._owned_isolated = False
        if not vosk_native_allowed():
            if self._isolated is None:
                from core.isolated_interrupt import IsolatedInterruptDetector
                resolved = resolve_vosk_model_path(model_path)
                self._isolated = IsolatedInterruptDetector(resolved, sample_rate)
                self._owned_isolated = True
            self.available = getattr(self._isolated, "available", False)
            self.fail_reason = getattr(self._isolated, "fail_reason", "")
            return
        try:
            from vosk import Model, KaldiRecognizer, SetLogLevel

            SetLogLevel(-1)
            if model is None:
                path = resolve_vosk_model_path(model_path)
                if not path.is_dir():
                    return
                model = Model(str(path))
            # Grammaire fermée + [unk] : hors des mots-clés, Vosk refuse de
            # forcer un ordre. Si le grand modèle ne supporte pas la grammaire,
            # KaldiRecognizer sans grammaire est utilisé, classify() filtre les commandes.
            try:
                self._rec = KaldiRecognizer(
                    model, sample_rate, json.dumps(list(INTERRUPT_GRAMMAR)),
                )
            except Exception:
                self._rec = KaldiRecognizer(model, sample_rate)
            self._rec.SetWords(True)
            self.available = True
        except Exception as exc:
            logger.warning("Interruption vocale indisponible: %s", exc)

    @staticmethod
    def _is_command(text: str) -> bool:
        return InterruptPhraseDetector.classify(text) is not None

    @staticmethod
    def classify(text: str) -> str | None:
        """Retourne ``stop`` ou ``redirect`` pour une transcription locale.

        Hors de cette liste, la transcription n'interrompt jamais :
        ano / anneau / jarvis, stop, arrête-toi, écoute.
        """
        value = _normalise(text)
        if not value:
            return None

        for name in sorted(_ANO_FORMS, key=len, reverse=True):
            if value == name:
                return "stop"
            if value.startswith(name + " "):
                rest = value[len(name):].strip()
                if not rest:
                    return "stop"
                if any(rest == p or rest.startswith(p + " ") for p in _STOP_PREFIXES):
                    return "stop"
                if any(rest == p or rest.startswith(p + " ") for p in _LISTEN_PREFIXES):
                    rest_after = rest
                    for p in _LISTEN_PREFIXES:
                        if rest_after == p:
                            return "stop"
                        if rest_after.startswith(p + " "):
                            return "redirect"
                    return "redirect"
                return "redirect"

        for prefix in _STOP_PREFIXES:
            if value == prefix or value.startswith(prefix + " "):
                return "stop"

        for prefix in _LISTEN_PREFIXES:
            if value == prefix:
                return "stop"
            if value.startswith(prefix + " "):
                return "redirect"

        return None

    @staticmethod
    def classify_addressed(text: str) -> str | None:
        value = _normalise(text)
        if not any(value.startswith(name + " ") for name in _ANO_FORMS):
            return None
        return InterruptPhraseDetector.classify(text)

    @staticmethod
    def classify_strict_interrupt(text: str) -> str | None:
        """Commandes de coupure, sans approximation ni redirection.

        Cette fonction est le contrat utilisateur du barge-in : une phrase
        reconnue approximativement par Vosk ne doit jamais couper ANO. Les
        seuls ordres admis sont « Ano stop », « arrête-toi » et « écoute »,
        sans mot supplémentaire. Tout le reste continue la lecture.
        """
        value = _normalise(text)
        if value in {"arrete toi", "arretez vous", "ecoute"}:
            return "stop"
        for name in _ANO_FORMS:
            if value in {
                f"{name} stop", f"{name} stoppe",
                f"{name} arrete toi", f"{name} arretez vous",
                f"{name} ecoute",
            }:
                return "stop"
        return None

    def _classify_detected(self, text: str) -> str | None:
        if getattr(self, "addressed_only", False):
            return self.classify_strict_interrupt(text)
        return self.classify(text)

    def wait_ready(self, timeout: float = 45.0) -> bool:
        isolated = getattr(self, "_isolated", None)
        if isolated is None:
            return self.available
        ready = isolated.wait_ready(timeout)
        self.available = isolated.available
        self.fail_reason = getattr(isolated, "fail_reason", "")
        return ready

    def process(self, pcm_int16: np.ndarray | bytes) -> bool:
        """Retourne True une fois pour une formule complète reconnue."""
        isolated = getattr(self, "_isolated", None)
        if isolated is not None:
            found = isolated.process(pcm_int16)
            self.available = isolated.available
            if found:
                # Le worker isolé connaît la grammaire courte mais pas le
                # contexte ``addressed_only`` de cette instance. Sans ce
                # second filtrage, « anneau » seul contournait la protection
                # anti-écho et coupait la réponse — exactement le faux positif
                # observé dans le journal `local/stop — anneau`.
                text = str(getattr(isolated, "last_text", "") or "")
                kind = self._classify_detected(text)
                if kind:
                    self.last_text, self.last_kind = text, kind
                    return True
                isolated.reset()
            return False
        if not self.available or self._rec is None:
            return False
        data = pcm_int16.tobytes() if isinstance(pcm_int16, np.ndarray) else pcm_int16
        try:
            if self._rec.AcceptWaveform(data):
                self._last_partial = ""
                result = json.loads(self._rec.Result()).get("text", "")
                kind = self._classify_detected(result)
                if kind:
                    self.last_text = result
                    self.last_kind = kind
                    self._rec.Reset()
                    return True
            else:
                partial = json.loads(self._rec.PartialResult()).get("partial", "")
                if partial and partial != self._last_partial:
                    self._last_partial = partial
                    kind = None if getattr(self, "addressed_only", False) else self._classify_detected(partial)
                    if kind:
                        self.last_text = partial
                        self.last_kind = kind
                        self._rec.Reset()
                        self._last_partial = ""
                        return True
        except Exception as exc:
            logger.debug("Erreur Vosk d'interruption: %s", exc)
        return False

    def close(self) -> None:
        isolated = getattr(self, "_isolated", None)
        if isolated is not None and getattr(self, "_owned_isolated", False):
            isolated.close()
        self.available = False

    def reset(self) -> None:
        isolated = getattr(self, "_isolated", None)
        if isolated is not None:
            isolated.reset()
        if self._rec is not None:
            try:
                self._rec.Reset()
            except Exception:
                pass
        self._last_partial = ""


class LocalBargeInListener:
    """Second consommateur PortAudio : seulement l'AEC → Vosk → callback.

    Il est ouvert sur la source Pulse par défaut *temporairement* orientée vers
    l'AEC par l'appelant. Le flux principal garde son micro brut déjà ouvert.
    """

    def __init__(self, sounddevice, detector: InterruptPhraseDetector,
                 is_speaking: Callable[[], bool], on_interrupt: Callable[..., None],
                 sample_rate: int = 16000, blocksize: int = 320,
                 input_device: str | None = None,
                 is_enabled: Callable[[], bool] | None = None,
                 quiet_arm_ms: int = 320) -> None:
        self._sd = sounddevice
        self._detector = detector
        self._is_speaking = is_speaking
        self._on_interrupt = on_interrupt
        self._sample_rate = sample_rate
        self._blocksize = blocksize
        self._input_device = input_device
        # Le flux AEC peut rester ouvert pour éviter une coûteuse réouverture
        # PortAudio à chaque bascule. Cela ne l'autorise pas à analyser quoi
        # que ce soit lorsque le micro utilisateur est coupé.
        self._is_enabled = is_enabled or (lambda: True)
        # Le début d'une réponse contient encore la fin de phrase utilisateur
        # et le transitoire des haut-parleurs. On exige un bref silence AEC
        # avant d'écouter une interruption : sans cela ANO coupe sa propre
        # première syllabe ou transforme un « oui » en « stop » Vosk.
        self._quiet_arm_s = max(0.0, float(quiet_arm_ms) / 1000.0)
        self._quiet_s = 0.0
        self._was_speaking = False
        self._stream = None
        self._fired = False

    @property
    def active(self) -> bool:
        return self._stream is not None

    def start(self) -> bool:
        if self._stream is not None:
            return True
        if not hasattr(self._sd, "InputStream"):
            return False

        def callback(indata, _frames, _time_info, _status) -> None:
            try:
                speaking = self._is_enabled() and self._is_speaking()
                if not speaking:
                    self._fired = False
                    self._was_speaking = False
                    self._quiet_s = 0.0
                    if getattr(self._detector, "available", False):
                        self._detector.reset()
                    return
                pcm = indata[:, 0] if indata.ndim > 1 else indata
                if self._fired:
                    return

                if not self._was_speaking:
                    self._was_speaking = True
                    self._quiet_s = 0.0
                    if getattr(self._detector, "available", False):
                        self._detector.reset()

                # Le flux est la source AEC. Si elle contient encore une voix
                # forte, ce n'est pas un moment sûr pour interpréter Vosk.
                rms = float(np.sqrt(np.mean(pcm.astype(np.float32) ** 2))) if pcm.size else 0.0
                if self._quiet_s < self._quiet_arm_s:
                    if rms < 550.0:
                        self._quiet_s += len(pcm) / float(self._sample_rate)
                    else:
                        self._quiet_s = 0.0
                    if getattr(self._detector, "available", False):
                        self._detector.reset()
                    return

                # Chemin 1 : Vosk local
                if getattr(self._detector, "available", False) and self._detector.process(np.ascontiguousarray(pcm)):
                    # L'état peut changer pendant l'inférence Vosk. Revalider
                    # juste avant le callback ferme cette fenêtre de course.
                    if not self._is_enabled():
                        self._detector.reset()
                        return
                    self._fired = True
                    try:
                        self._on_interrupt(
                            getattr(self._detector, "last_kind", "redirect"),
                            getattr(self._detector, "last_text", ""),
                        )
                    except TypeError:
                        self._on_interrupt()
                    return

                # Aucun repli sur le volume : vent, clavier et voix lointaines
                # peuvent être forts sans être une commande d'interruption.
            except Exception as exc:
                logger.debug("Callback interruption ignoré: %s", exc)

        try:
            self._stream = self._sd.InputStream(
                samplerate=self._sample_rate, channels=1, dtype="int16",
                blocksize=self._blocksize, callback=callback,
                device=self._input_device,
            )
            self._stream.start()
            return True
        except Exception as exc:
            logger.warning("Flux AEC d'interruption indisponible: %s", exc)
            self._stream = None
            return False

    def stop(self) -> None:
        close_detector = getattr(self._detector, "close", None)
        if close_detector:
            close_detector()
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
