"""
screen_process.py — JARVIS Vision ultra‑réaliste (version renforcée Hyprland/Wayland)
Analyse l'écran ou la caméra en temps réel via Gemini Live Audio.
Corrections clés : capture Wayland via grim (mss renvoie du noir sous Hyprland),
chemins config corrigés, gestion d'erreurs robuste, modèle Live configurable.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional, Dict, Any

import numpy as np
from core.live_model_policy import FAST_MODEL, SCREEN_LIVE_MODEL

sd = None


def _load_sounddevice():
    global sd
    if sd is not None:
        return sd
    try:
        import sounddevice as _sd
        sd = _sd
        return sd
    except ImportError:
        return None

# OpenCV coûte ~120 ms d'import et ne sert qu'à une analyse d'image réelle.
# Le charger au démarrage retardait la voix pour une fonction souvent inutile
# dans une session — même schéma que le studio caméra de phone_relay.
_CV2_MODULE = None             # None = pas encore cherché ; False = absent


def _cv2():
    """Module ``cv2`` s'il est installé, sinon ``None``."""
    global _CV2_MODULE
    if _CV2_MODULE is None:
        try:
            import cv2
            _CV2_MODULE = cv2
        except ImportError:
            _CV2_MODULE = False
    return _CV2_MODULE or None

try:
    import mss
    import mss.tools
    _MSS = True
except ImportError:
    _MSS = False

try:
    import PIL.Image
    _PIL = True
except ImportError:
    _PIL = False

def _google_genai():
    """Charge le SDK seulement lorsqu'une session Vision est réellement utilisée.

    L'ancien import au niveau module annulait entièrement le chargement différé
    de main.py et pouvait figer le démarrage pendant plusieurs dizaines de
    secondes sur une machine occupée.
    """
    from google import genai
    from google.genai import types as gtypes
    return genai, gtypes

# ── Configuration ────────────────────────────────────────────────────────────
def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

_BASE        = _base_dir()
_CONFIG_PATH = _BASE / "config" / "api_keys.json"

def _load_config() -> dict:
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _save_config_key(key: str, value) -> None:
    try:
        cfg = _load_config()
        cfg[key] = value
        _CONFIG_PATH.write_text(json.dumps(cfg, indent=4), encoding="utf-8")
    except Exception as e:
        print(f"[Vision] ⚠️  Impossible d'enregistrer la clé '{key}' : {e}")

def _get_api_key() -> str:
    key = _load_config().get("gemini_api_key", "")
    if not key:
        print("[Vision] ⚠️  Clé Gemini introuvable dans la config.")
    return key

def _get_os() -> str:
    # Priorité à la config, sinon détection réelle
    cfg = _load_config().get("os_system", "").lower()
    if cfg:
        return cfg
    import platform
    return platform.system().lower()

# ── Détection Wayland / X11 ──────────────────────────────────────────────────
def _is_wayland() -> bool:
    if os.environ.get("WAYLAND_DISPLAY"):
        return True
    if os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
        return True
    return False

def _is_x11() -> bool:
    return bool(os.environ.get("DISPLAY")) and not _is_wayland()

# ── Gemini Live settings ────────────────────────────────────────────────────
_DEFAULT_LIVE_MODEL = SCREEN_LIVE_MODEL
_LIVE_MODEL = _load_config().get("gemini_live_model", _DEFAULT_LIVE_MODEL)
_CHANNELS = 1
_RECEIVE_SAMPLE_RATE = 24_000
_CHUNK_SIZE = 1_024
_IMG_MAX_W = 1280
_IMG_MAX_H = 720
_JPEG_Q = 82
_SESSION_TIMEOUT = float(_load_config().get("vision_session_timeout", 25))

_SYSTEM_PROMPT = (
    "You are JARVIS, Tony Stark's AI assistant. "
    "You are given an image from either the user's screen or their webcam. "
    "Analyze what you see with detail and intelligence. "
    "Describe objects, text, people, components, and their context clearly. "
    "For technical questions (circuits, code, hardware) give specific, expert answers. "
    "Be concise — 2-4 sentences — unless the question demands more detail. "
    "Speak directly to the user ('I can see...', 'You have...'). "
    "Match the language the user used, and address them as 'sir' when appropriate."
)

# ── Compression d'image ─────────────────────────────────────────────────────
def _compress(img_bytes: bytes, source_format: str = "PNG") -> tuple[bytes, str]:
    if not _PIL:
        return img_bytes, f"image/{source_format.lower()}"
    try:
        img = PIL.Image.open(io.BytesIO(img_bytes)).convert("RGB")
        img.thumbnail((_IMG_MAX_W, _IMG_MAX_H), PIL.Image.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=_JPEG_Q, optimize=False)
        return buf.getvalue(), "image/jpeg"
    except Exception as e:
        print(f"[Vision] ⚠️  Compression d'image échouée : {e}")
        return img_bytes, f"image/{source_format.lower()}"

# ── Capture d'écran (Hyprland / Wayland / X11) ────────────────────────────────
def _capture_screen_grim(target: str = "active_window") -> bytes:
    """Capture haute fidélité via screen_capture (fenêtre active ou écran)."""
    try:
        from core import screen_capture
        raw, _, _ = screen_capture.capture_window_or_screen(target=target, compress=False)
        if raw and len(raw) > 0:
            return raw
    except Exception as e:
        print(f"[Vision] Capture active window repli plein écran : {e}")

    if not shutil.which("grim"):
        raise RuntimeError(
            "grim est requis pour capturer l'écran sous Wayland. "
            "Installe-le avec : sudo pacman -S grim"
        )
    tmp = Path(tempfile.mktemp(suffix=".png"))
    try:
        r = subprocess.run(["grim", str(tmp)], capture_output=True, text=True, timeout=15)
        if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            err = (r.stderr or "").strip() or "image vide"
            raise RuntimeError(f"grim a échoué : {err}")
        return tmp.read_bytes()
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass

def _capture_screen_mss() -> bytes:
    if not _MSS:
        raise RuntimeError(
            "mss n'est pas installé (et grim indisponible). "
            "Installe mss avec : pip install mss, ou grim pour Wayland."
        )
    with mss.mss() as sct:
        monitors = sct.monitors
        target = monitors[1] if len(monitors) > 1 else monitors[0]
        shot = sct.grab(target)
        return mss.tools.to_png(shot.rgb, shot.size)

def _capture_screen(target: str = "active_window") -> tuple[bytes, str]:
    if _is_wayland():
        return _compress(_capture_screen_grim(target=target), "PNG")
    return _compress(_capture_screen_mss(), "PNG")


# ── Utilitaires caméra ──────────────────────────────────────────────────────
def _cv2_backend() -> int:
    if _cv2() is None:
        return 0
    os_name = _get_os()
    if os_name == "windows":
        return _cv2().CAP_DSHOW
    if os_name == "mac":
        return _cv2().CAP_AVFOUNDATION
    return _cv2().CAP_ANY

def _probe_camera(index: int, backend: int, warmup: int = 5) -> bool:
    if _cv2() is None:
        return False
    cap = _cv2().VideoCapture(index, backend)
    if not cap.isOpened():
        cap.release()
        return False
    for _ in range(warmup):
        cap.read()
    ret, frame = cap.read()
    cap.release()
    if not ret or frame is None:
        return False
    return bool(np.mean(frame) > 8)

def _detect_camera_index() -> int:
    backend = _cv2_backend()
    print("[Vision] 🔍 Détection automatique de la caméra...")
    for idx in range(6):
        if _probe_camera(idx, backend):
            print(f"[Vision] ✅ Caméra trouvée à l'index {idx}")
            _save_config_key("camera_index", idx)
            return idx
        print(f"[Vision] ⚠️  Index caméra {idx} : aucune image exploitable")
    print("[Vision] ⚠️  Aucune caméra trouvée — index 0 par défaut")
    _save_config_key("camera_index", 0)
    return 0

def _get_camera_index() -> int:
    cfg = _load_config()
    if "camera_index" in cfg:
        try:
            return int(cfg["camera_index"])
        except Exception:
            pass
    return _detect_camera_index()

def _capture_camera() -> tuple[bytes, str]:
    if _cv2() is None:
        raise RuntimeError("OpenCV (cv2) n'est pas installé. Exécutez : pip install opencv-python")
    index = _get_camera_index()
    backend = _cv2_backend()
    cap = _cv2().VideoCapture(index, backend)
    if not cap.isOpened():
        raise RuntimeError(f"Impossible d'ouvrir la caméra (index {index}).")
    for _ in range(10):
        cap.read()
    ret, frame = cap.read()
    cap.release()
    if not ret or frame is None:
        raise RuntimeError("La caméra n'a renvoyé aucune image.")
    if _PIL:
        rgb = _cv2().cvtColor(frame, _cv2().COLOR_BGR2RGB)
        img = PIL.Image.fromarray(rgb)
        img.thumbnail((_IMG_MAX_W, _IMG_MAX_H), PIL.Image.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=_JPEG_Q)
        return buf.getvalue(), "image/jpeg"
    _, buf = _cv2().imencode(".jpg", frame, [_cv2().IMWRITE_JPEG_QUALITY, _JPEG_Q])
    return buf.tobytes(), "image/jpeg"

# ── Gestion de session Gemini Live ─────────────────────────────────────────
class _VisionSession:
    def __init__(self):
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._session = None
        self._out_queue: Optional[asyncio.Queue] = None
        self._audio_in: Optional[asyncio.Queue] = None
        self._ready_evt: threading.Event = threading.Event()
        self._player = None
        self._lock: threading.Lock = threading.Lock()

    def start(self, player=None, timeout: float = None) -> None:
        timeout = timeout or _SESSION_TIMEOUT
        with self._lock:
            if self._thread and self._thread.is_alive():
                if player is not None:
                    self._player = player
                return
            self._player = player
            self._thread = threading.Thread(
                target=self._run_event_loop, daemon=True, name="VisionSessionThread"
            )
            self._thread.start()
        if not self._ready_evt.wait(timeout=timeout):
            raise RuntimeError(
                f"La session Vision ne s'est pas connectée en {timeout}s. "
                "Vérifie ta clé API et ta connexion."
            )
        print("[Vision] ✅ Session prête")

    def analyze(self, image_bytes: bytes, mime_type: str, user_text: str) -> bool:
        if not self._loop or not self._out_queue:
            print("[Vision] ⚠️  Session non démarrée — requête ignorée")
            return False
        asyncio.run_coroutine_threadsafe(
            self._out_queue.put((image_bytes, mime_type, user_text)), self._loop
        )
        return True

    def is_ready(self) -> bool:
        return self._session is not None

    def _run_event_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._session_loop())

    async def _session_loop(self) -> None:
        self._out_queue = asyncio.Queue(maxsize=30)
        self._audio_in = asyncio.Queue()
        try:
            genai, gtypes = await asyncio.to_thread(_google_genai)
            client = genai.Client(
                api_key=_get_api_key(),
                http_options={"api_version": "v1beta"},
            )
        except Exception as e:
            print(f"[Vision] ❌ Erreur de création du client Gemini : {e}")
            self._ready_evt.set()
            return

        config = gtypes.LiveConnectConfig(
            response_modalities=["AUDIO"],
            output_audio_transcription={},
            system_instruction=_SYSTEM_PROMPT,
            speech_config=gtypes.SpeechConfig(
                voice_config=gtypes.VoiceConfig(
                    prebuilt_voice_config=gtypes.PrebuiltVoiceConfig(voice_name="Charon")
                )
            ),
        )

        backoff = 2.0
        while True:
            try:
                print("[Vision] 🔌 Connexion...")
                async with client.aio.live.connect(model=_LIVE_MODEL, config=config) as session:
                    self._session = session
                    self._ready_evt.set()
                    backoff = 2.0
                    print("[Vision] ✅ Connecté")
                    async with asyncio.TaskGroup() as tg:
                        tg.create_task(self._send_loop())
                        tg.create_task(self._recv_loop())
                        tg.create_task(self._play_loop())
            except Exception as eg:
                # Compatible avec ExceptionGroup (TaskGroup) et exceptions simples
                excs = getattr(eg, "exceptions", [eg])
                for exc in excs:
                    print(f"[Vision] ⚠️  Erreur de session : {exc}")
            finally:
                self._session = None
                self._ready_evt.clear()
            print(f"[Vision] 🔄 Reconnexion dans {backoff:.0f}s...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.5, 30.0)
            self._ready_evt.set()

    async def _send_loop(self) -> None:
        while True:
            image_bytes, mime_type, user_text = await self._out_queue.get()
            if not self._session:
                print("[Vision] ⚠️  Pas de session — image ignorée")
                continue
            try:
                b64 = base64.b64encode(image_bytes).decode("ascii")
                await self._session.send_client_content(
                    turns={
                        "parts": [
                            {"inline_data": {"mime_type": mime_type, "data": b64}},
                            {"text": user_text},
                        ]
                    },
                    turn_complete=True,
                )
                print(f"[Vision] 📤 Envoyé {len(image_bytes):,} octets — '{user_text[:60]}'")
            except Exception as e:
                print(f"[Vision] ⚠️  Erreur d'envoi : {e}")
                raise

    async def _recv_loop(self) -> None:
        transcript: list[str] = []
        try:
            async for response in self._session.receive():
                if response.data:
                    await self._audio_in.put(response.data)
                sc = response.server_content
                if not sc:
                    continue
                if sc.output_transcription and sc.output_transcription.text:
                    chunk = sc.output_transcription.text.strip()
                    if chunk:
                        transcript.append(chunk)
                if sc.turn_complete:
                    if transcript and self._player:
                        full = re.sub(r"\s+", " ", " ".join(transcript)).strip()
                        if full:
                            try:
                                self._player.write_log(f"Jarvis: {full}")
                            except Exception:
                                pass
                            print(f"[Vision] 💬 {full}")
                    transcript = []
                    if self._player and hasattr(self._player, "stop_camera_stream"):
                        async def _deferred_close():
                            await asyncio.sleep(2.0)
                            try:
                                self._player.stop_camera_stream()
                            except Exception:
                                pass
                        asyncio.create_task(_deferred_close())
        except Exception as e:
            print(f"[Vision] ⚠️  Erreur de réception : {e}")
            raise

    async def _play_loop(self) -> None:
        sounddevice = await asyncio.to_thread(_load_sounddevice)
        if sounddevice is None:
            print("[Vision] ⚠️  sounddevice absent — lecture audio désactivée")
            return
        stream = sounddevice.RawOutputStream(
            samplerate=_RECEIVE_SAMPLE_RATE,
            channels=_CHANNELS,
            dtype="int16",
            blocksize=_CHUNK_SIZE,
        )
        stream.start()
        try:
            while True:
                chunk = await self._audio_in.get()
                await asyncio.to_thread(stream.write, chunk)
        except Exception as e:
            print(f"[Vision] ❌ Erreur de lecture audio : {e}")
            raise
        finally:
            stream.stop()
            stream.close()

_session = _VisionSession()
_session_lock = threading.Lock()
_session_up = False

def _ensure_session(player=None) -> None:
    global _session_up
    with _session_lock:
        if not _session_up:
            _session.start(player=player)
            _session_up = True
        elif player is not None:
            _session._player = player

# ── Parsing local des commandes ─────────────────────────────────────────────
def _parse_vision_command_locally(text: str) -> Optional[Dict[str, Any]]:
    """
    Extrait l'angle (écran / caméra) et la question de l'utilisateur.
    Retourne {'angle': 'screen'|'camera', 'text': '...'} ou None.
    """
    text = text.lower().strip()
    text = re.sub(r"\b(s'il te pla[iî]t|stp|please|peux-tu|tu peux|je veux)\b", "", text).strip()

    angle = None
    if re.search(r"\b(cam[eé]ra|webcam|cam|filme)\b", text):
        angle = "camera"
    elif re.search(r"\b([eé]cran|screen|moniteur|desktop|bureau)\b", text):
        angle = "screen"

    question = None
    verbs = (
        r"(regardes?|analyses?|d[eé]cris?|montres?|dis[\s-]moi|expliques?|"
        r"que\s+vois[\s-]tu|qu['e]st[- ]ce\s+que\s+tu\s+vois|"
        r"qu['e]st[- ]ce\s+qu['i]l\s+y\s+a|d[eé]taille|d[eé]cortique)"
    )
    m = re.search(rf"(?:{verbs})\s+(.+?)(?:\?|$)", text)
    if m:
        question = m.group(1).strip()
        if not question or question in ("sur l'écran", "à l'écran", "sur la caméra"):
            question = None
    else:
        rest = re.sub(r"\b(cam[eé]ra|webcam|[eé]cran|screen|moniteur)\b", "", text).strip()
        if rest:
            question = rest

    return {
        "angle": angle or "screen",
        "text": question or "Describe what you see.",
    }

def _detect_vision_intent_ai(description: str) -> Optional[Dict]:
    api_key = _get_api_key()
    if not api_key:
        return None
    try:
        genai, _ = _google_genai()
        client = genai.Client(api_key=api_key)
        prompt = (
            f"Analyse la phrase suivante et retourne UNIQUEMENT un objet JSON avec "
            f"'angle' (valeur: 'screen' ou 'camera') et 'text' (la question à poser "
            f"à propos de l'image).\n"
            f"Phrase : \"{description}\"\n"
            f"Exemple : pour \"regarde l'écran et dis-moi ce que tu vois\" → "
            f"{{\"angle\": \"screen\", \"text\": \"Que vois-tu ?\"}}\n"
            f"Pour \"qu'est-ce que la caméra filme ?\" → "
            f"{{\"angle\": \"camera\", \"text\": \"Qu'est-ce que la caméra filme ?\"}}\n"
            f"Si aucune question explicite, utilise \"Describe what you see.\"\n"
            f"Réponds UNIQUEMENT avec le JSON."
        )
        resp = client.models.generate_content(model=FAST_MODEL, contents=prompt)
        match = re.search(r"\{.*\}", resp.text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
    except Exception as e:
        print(f"[Vision] ⚠️  Erreur IA intent : {e}")
    return None

# ── Point d'entrée principal ────────────────────────────────────────────────
def screen_process(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    """
    Analyse l'écran ou la caméra en réponse à une question.
    Retourne un message d'état (les autres modules renvoient des chaînes).
    """
    params = parameters or {}
    user_text = (params.get("text") or params.get("user_text") or "").strip()
    angle = params.get("angle", "screen").lower().strip()
    description = params.get("description", "").strip()

    if description:
        local = _parse_vision_command_locally(description)
        if local:
            if not angle or angle == "screen":
                angle = local.get("angle", angle)
            if not user_text:
                user_text = local.get("text", user_text)
        else:
            ai = _detect_vision_intent_ai(description)
            if ai:
                angle = ai.get("angle", angle)
                user_text = ai.get("text", user_text)
            else:
                if not user_text:
                    user_text = description if description else "What do you see?"
                angle = angle if angle in ("screen", "camera") else "screen"

    if not user_text:
        user_text = "Describe what you see."
    if angle not in ("screen", "camera"):
        angle = "screen"

    print(f"[Vision] ▶ angle={angle!r}  question='{user_text[:80]}'")

    try:
        _ensure_session(player=player)
    except Exception as e:
        print(f"[Vision] ❌ Impossible de démarrer la session : {e}")
        return f"Je n'ai pas pu démarrer la session Vision : {e}"

    try:
        if angle == "camera":
            image_bytes, mime_type = _capture_camera()
            print(f"[Vision] 📷 Caméra : {len(image_bytes):,} octets")
            if player and hasattr(player, "start_camera_stream"):
                try:
                    player.start_camera_stream()
                except Exception as _e:
                    print(f"[Vision] ⚠️  Flux caméra échoué : {_e}")
            elif player and hasattr(player, "show_camera_frame"):
                try:
                    player.show_camera_frame(image_bytes)
                except Exception as _e:
                    print(f"[Vision] ⚠️  Aperçu caméra échoué : {_e}")
        else:
            image_bytes, mime_type = _capture_screen()
            print(f"[Vision] 🖥️  Écran : {len(image_bytes):,} octets")
    except Exception as e:
        print(f"[Vision] ❌ Erreur de capture : {e}")
        return f"La capture a échoué : {e}"

    queued = _session.analyze(image_bytes, mime_type, user_text)
    if queued:
        return "Analyse en cours — je te réponds à voix haute dans un instant."
    return "La session n'était pas prête, la requête a été ignorée. Réessaie."

def warmup_session(player=None) -> None:
    try:
        _ensure_session(player=player)
    except Exception as e:
        print(f"[Vision] ⚠️  Préchauffage échoué : {e}")

# ── Test direct ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("[TEST] screen_processor.py")
    print("=" * 52)
    mode = input("angle — screen / camera (défaut: screen) : ").strip().lower() or "screen"
    q = input("Question (Entrée = défaut) : ").strip() or "What do you see? Be brief."
    t0 = time.perf_counter()
    warmup_session()
    print(f"Session prête en {time.perf_counter()-t0:.2f}s\n")
    t1 = time.perf_counter()
    msg = screen_process({"angle": mode, "text": q})
    print(f"Statut : {msg} (mis en file en {time.perf_counter()-t1:.3f}s)")
    time.sleep(10)
    print("Terminé.")
