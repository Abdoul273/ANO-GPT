"""Second avis Azure Speech pour les tours STT courts d'ANO-GPT.

Les identifiants restent dans la configuration locale DEO_DUB que l'utilisateur
a explicitement placée à disposition. Ils ne sont ni copiés, ni journalisés.
L'API REST officielle accepte un WAV PCM 16 kHz mono, format déjà produit par
le pipeline ANO-GPT.
"""
from __future__ import annotations

import io
import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
import wave
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable, Optional


_DEFAULT_SETTINGS = (
    Path.home() / "Documents" / "Projet_Compresser" / "DEO_DUB" / "data" / "settings.json"
)
_ANOGPT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "api_keys.json"
_MAX_SECONDS = 60
_FAST_API_VERSION = "2025-10-15"

# Le palier gratuit F0 n'accepte qu'une transcription à la fois : deux tours qui
# se chevauchent se répondaient en 429, et l'assistant perdait la phrase sans
# jamais dire pourquoi. On les met donc à la queue leu leu sur ce palier.
_F0_GATE = threading.Lock()
_F0_WAIT_S = 8.0


@dataclass(frozen=True)
class AzureSpeechResult:
    text: str = ""
    available: bool = False
    error: str = ""


def azure_verify_enabled(default: bool = True) -> bool:
    """Retourne le choix confidentialité/latence, sans consulter de secret.

    ``ANOGPT_AZURE_VERIFY=off`` est intentionnellement prioritaire sur la
    configuration : aucune phrase ne part alors vers Azure et Gemini reste le
    seul moteur distant de la chaîne existante.
    """
    raw = os.environ.get("ANOGPT_AZURE_VERIFY", "").strip().casefold()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off"}


def azure_fast_phrases_enabled(default: bool = False) -> bool:
    """Active opt-in l'endpoint Fast qui accepte une liste de phrases.

    L'ancien endpoint court reste le défaut : migration réseau et latence
    doivent être validées avec les vraies phrases de l'utilisateur avant une
    activation générale.
    """
    raw = os.environ.get("ANOGPT_AZURE_FAST_PHRASES", "").strip().casefold()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _settings_path() -> Path:
    value = os.environ.get("ANOGPT_AZURE_SPEECH_SETTINGS", "").strip()
    return Path(value).expanduser() if value else _DEFAULT_SETTINGS


def _credentials() -> tuple[str, str]:
    """Retourne clé/région sans jamais exposer la clé dans une erreur.

    Les réglages intégrés à ANO-GPT sont prioritaires. L'ancien fichier externe
    reste lu seulement pour ne pas casser une installation déjà configurée.
    """
    try:
        data = json.loads(_ANOGPT_CONFIG.read_text(encoding="utf-8"))
        key = str(data.get("azure_speech_key", "") or "").strip()
        region = "".join(str(data.get("azure_speech_region", "") or "").split()).lower()
        if key and region:
            return key, region
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    try:
        data = json.loads(_settings_path().read_text(encoding="utf-8"))
        profile = str(data.get("azure_profil", "f0")).lower()
        if profile not in {"f0", "s0"}:
            profile = "f0"
        key = str(data.get(f"azure_{profile}_key", "") or "").strip()
        region = "".join(str(data.get(f"azure_{profile}_region", "") or "").split()).lower()
        return key, region
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return "", ""


def speech_tier() -> str:
    """Palier Speech retenu : « f0 » (gratuit) ou « s0 » (standard)."""
    try:
        data = json.loads(_ANOGPT_CONFIG.read_text(encoding="utf-8"))
        tier = str(data.get("azure_speech_tier", "f0") or "f0").lower()
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        tier = "f0"
    return tier if tier in {"f0", "s0"} else "f0"


def _http_reason(status: int) -> str:
    """Explique un refus Azure sans jamais citer la clé, la région ni la réponse."""
    if status in (401, 403):
        return "Azure Speech a refusé la clé ou la région."
    if status == 429:
        return ("Quota Azure Speech atteint — le palier F0 est limité "
                "(≈5 h/mois, une requête à la fois).")
    if status == 400:
        return "Azure Speech a rejeté l'audio envoyé."
    if 500 <= status < 600:
        return "Azure Speech est momentanément en panne."
    return f"Azure Speech a répondu {status}."


def _wav(pcm: bytes) -> bytes:
    if not pcm or len(pcm) % 2:
        raise ValueError("PCM16 mono attendu")
    if len(pcm) > _MAX_SECONDS * 16000 * 2:
        raise ValueError("phrase Azure trop longue")
    out = io.BytesIO()
    with wave.open(out, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(pcm)
    return out.getvalue()


def _clean_phrases(phrases: Iterable[str]) -> list[str]:
    """Liste courte, déterministe et sans contenu de configuration sensible."""
    clean: list[str] = []
    seen: set[str] = set()
    for phrase in phrases:
        value = " ".join(str(phrase or "").split())[:160]
        key = value.casefold()
        if value and key not in seen:
            clean.append(value)
            seen.add(key)
        if len(clean) >= 500:  # limite documentée pour Azure Phrase List
            break
    return clean


def _multipart_fast_request(wav: bytes, phrases: Iterable[str]) -> tuple[bytes, str]:
    """Construit le formulaire Fast REST sans dépendance SDK ni fichier temporaire."""
    boundary = "----ANOGPTAzureSpeechBoundary"
    definition = json.dumps({
        "locales": ["fr-FR"],
        "phraseList": {"phrases": _clean_phrases(phrases)},
    }, ensure_ascii=False).encode("utf-8")
    parts = [
        b"--" + boundary.encode() + b"\r\n"
        b'Content-Disposition: form-data; name="audio"; filename="turn.wav"\r\n'
        b"Content-Type: audio/wav\r\n\r\n" + wav + b"\r\n",
        b"--" + boundary.encode() + b"\r\n"
        b'Content-Disposition: form-data; name="definition"\r\n'
        b"Content-Type: application/json; charset=utf-8\r\n\r\n" + definition + b"\r\n",
        b"--" + boundary.encode() + b"--\r\n",
    ]
    return b"".join(parts), boundary


def _fast_text(payload: dict) -> str:
    """Lit la réponse Fast REST tout en restant compatible avec ses segments."""
    combined = payload.get("combinedPhrases") or []
    if combined:
        return " ".join(str(item.get("text", "") or "").strip() for item in combined).strip()
    phrases = payload.get("phrases") or []
    return " ".join(str(item.get("text", "") or "").strip() for item in phrases).strip()


def normalise(text: str) -> str:
    return " ".join("".join(c.lower() if c.isalnum() else " " for c in text).split())


def agree(first: str, second: str) -> bool:
    """Accord robuste à la ponctuation, mais pas aux phrases sans rapport."""
    first, second = normalise(first), normalise(second)
    if not first or not second:
        return False
    if first == second or first in second or second in first:
        return True
    return SequenceMatcher(None, first, second).ratio() >= 0.68


class AzureSpeechVerifier:
    def __init__(self, phrases: Iterable[str] = (), *, use_fast_phrases: bool = False):
        self.phrases = _clean_phrases(phrases)
        self.use_fast_phrases = bool(use_fast_phrases)

    def available(self) -> bool:
        key, region = _credentials()
        return bool(key and region)

    def transcribe(self, pcm: bytes) -> AzureSpeechResult:
        key, region = _credentials()
        if not key or not region:
            return AzureSpeechResult(error="Azure Speech non configuré.")
        try:
            wav = _wav(pcm)
            if self.use_fast_phrases:
                endpoint = (
                    f"https://{region}.api.cognitive.microsoft.com/"
                    "speechtotext/transcriptions:transcribe?"
                    + urllib.parse.urlencode({"api-version": _FAST_API_VERSION})
                )
                body, boundary = _multipart_fast_request(wav, self.phrases)
                content_type = f"multipart/form-data; boundary={boundary}"
            else:
                endpoint = (
                    f"https://{region}.stt.speech.microsoft.com/"
                    "speech/recognition/conversation/cognitiveservices/v1?"
                    + urllib.parse.urlencode({"language": "fr-FR", "format": "detailed"})
                )
                body, content_type = wav, "audio/wav; codecs=audio/pcm; samplerate=16000"
            request = urllib.request.Request(
                endpoint,
                data=body,
                headers={
                    "Ocp-Apim-Subscription-Key": key,
                    "Content-Type": content_type,
                    "Accept": "application/json",
                },
                method="POST",
            )
            # Sur F0, une seule requête peut être en vol : on attend son tour
            # plutôt que de récolter un 429. Au-delà du délai, on renonce — mieux
            # vaut perdre le second avis que retarder la réponse vocale.
            serialise = speech_tier() == "f0"
            if serialise and not _F0_GATE.acquire(timeout=_F0_WAIT_S):
                return AzureSpeechResult(
                    available=True,
                    error="Azure Speech (F0) occupé par la requête précédente.")
            try:
                with urllib.request.urlopen(request, timeout=12) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            finally:
                if serialise:
                    _F0_GATE.release()
            text = _fast_text(payload) if self.use_fast_phrases else str(payload.get("DisplayText", "") or "").strip()
            return AzureSpeechResult(text=text, available=True)
        except urllib.error.HTTPError as exc:
            # Le statut suffit à diagnostiquer ; le corps de la réponse, non.
            return AzureSpeechResult(available=True, error=_http_reason(exc.code))
        except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError):
            # L'échec Azure ne doit pas révéler endpoint, clé ou réponse serveur.
            return AzureSpeechResult(available=True, error="Azure Speech indisponible.")
