"""ElevenLabs PCM through Jarvis' existing output and half-duplex gate."""
from __future__ import annotations

import asyncio
import aiohttp

DEFAULT_VOICE = "JBFqnCBsd6RMkjVDRZzb"  # George
DEFAULT_MODEL = "eleven_multilingual_v2"
MODEL_OPTIONS = (
    ("eleven_multilingual_v2", "Multilingual v2 — naturel et régulier (conseillé)"),
    ("eleven_turbo_v2_5", "Turbo v2.5 — équilibre qualité / rapidité"),
    ("eleven_flash_v2_5", "Flash v2.5 — réponse rapide"),
)


async def list_voices(settings: dict) -> list[dict]:
    """Catalogue du compte, paginé, sans synthèse facturable."""
    key = settings.get("elevenlabs_api_key", "")
    if not key:
        raise RuntimeError("Ajoutez votre clé ElevenLabs dans Configurer l’IA.")
    voices = {}
    page_token = None
    seen = set()
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            for _ in range(30):
                params = {"page_size": 100}
                if page_token:
                    params["next_page_token"] = page_token
                async with session.get(
                    "https://api.elevenlabs.io/v2/voices",
                    headers={"xi-api-key": key}, params=params,
                ) as response:
                    if response.status in (401, 403):
                        detail = (await response.json()).get("detail") or {}
                        code = detail.get("status") if isinstance(detail, dict) else ""
                        if code == "missing_permissions":
                            raise RuntimeError(
                                "La clé ElevenLabs ne permet pas de lire les voix. "
                                "Activez voices_read dans les réglages de cette clé, puis actualisez. "
                            )
                        raise RuntimeError("Clé ElevenLabs refusée. Vérifiez sa validité et ses permissions.")
                    if response.status != 200:
                        raise RuntimeError(f"Catalogue ElevenLabs indisponible (HTTP {response.status}).")
                    payload = await response.json()
                for item in payload.get("voices", []):
                    vid = str(item.get("voice_id") or "")
                    if vid and vid.isascii() and vid.isalnum():
                        labels = item.get("labels") or {}
                        detail = " · ".join(str(labels[k]) for k in ("language", "accent", "gender", "description") if labels.get(k))
                        name = str(item.get("name") or vid)
                        voices[vid] = {"voice_id": vid, "name": name,
                                       "label": f"{name} — {detail}" if detail else name}
                if not payload.get("has_more"):
                    return sorted(voices.values(), key=lambda v: v["name"].casefold())
                page_token = payload.get("next_page_token")
                if not page_token or page_token in seen:
                    raise RuntimeError("Catalogue ElevenLabs incomplet. Cliquez sur Actualiser les voix.")
                seen.add(page_token)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        raise RuntimeError("Connexion ElevenLabs indisponible. Réessayez avec Actualiser les voix.") from None
    raise RuntimeError("Catalogue trop volumineux. Réduisez les voix de votre bibliothèque ElevenLabs.")



async def stream_pcm(text: str, settings: dict):
    """Yield aligned 24 kHz s16le frames; never retry billable requests."""
    key = settings.get("elevenlabs_api_key", "")
    if not key:
        raise RuntimeError("Clé ElevenLabs absente")
    voice = settings.get("elevenlabs_voice_id") or DEFAULT_VOICE
    if not str(voice).isalnum():
        raise RuntimeError("Identifiant de voix ElevenLabs invalide")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60, sock_read=15)) as session:
        async with session.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice}/stream",
            params={"output_format": "pcm_24000"},
            headers={"xi-api-key": key},
            json={"text": text, "model_id": settings.get("elevenlabs_model_id") or DEFAULT_MODEL,
                  "voice_settings": {"stability": 0.6, "similarity_boost": 0.75, "style": 0.0}},
        ) as response:
            if response.status != 200:
                # Do not expose response bodies, headers or credentials in logs.
                raise RuntimeError(f"ElevenLabs HTTP {response.status} : vérifier clé, autorisations et crédits")
            pending = b""
            async for data in response.content.iter_chunked(960):
                pending += data
                while len(pending) >= 960:
                    yield pending[:960]
                    pending = pending[960:]
            if len(pending) % 2:
                raise RuntimeError("Flux PCM ElevenLabs incomplet")
            if pending:
                yield pending


async def speak_live_turn(host, text: str, settings: dict):
    """Keep the microphone closed from synthesis until hardware playback drains."""
    if not text.strip() or host._interrupted:
        return
    host.set_speaking(True)
    if host._turn_done_event:
        host._turn_done_event.clear()
    stream = stream_pcm(text, settings)
    try:
        async for chunk in stream:
            if host._interrupted:
                break
            await host.audio_in_queue.put(chunk)
            host._audio_enqueued_sec += len(chunk) / 48000
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        message = str(exc) if isinstance(exc, RuntimeError) else "Connexion ElevenLabs indisponible"
        host.ui.write_log(f"ERR : {message}. Vous pouvez sélectionner Gemini dans Audio.")
    finally:
        await stream.aclose()
        if host._turn_done_event:
            host._turn_done_event.set()
