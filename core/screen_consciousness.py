"""core/screen_consciousness.py — Veille visuelle continue et sobre sous Hyprland.

Sur deux cœurs partagés avec la voix, une capture 4K en boucle tuerait
l'audio. Ce démon ne photographie donc que la fenêtre active (ROI grim),
compare un perceptual hash et un SSIM minuscules, et ne réveille Tesseract
que lorsqu'un vrai changement le justifie. Rien ne part au réseau tant que
l'utilisateur n'a pas posé une question sur l'écran.

Concurrence
-----------
* **asyncio** — ``ScreenConsciousness.run`` est une tâche de la session
  (ou du process). Le tick bloquant (grim, DCT, WebP, OCR) quitte la
  boucle via le pool ``compute-light`` ou ``asyncio.to_thread``.
  WebP : une passe ``method=0`` à 1024 px, ``stall_timeout=8 s`` — plus
  de boucle qualité qui prenait un cœur pendant 3 s et faisait bégayer
  la voix.
* **thread Hyprland** — la socket ``.socket2.sock`` réveille le démon au
  changement de fenêtre, sans sondage ``hyprctl`` entre deux ticks.
* **Qt / audio** — aucune image n'est décodée sur le thread principal, et
  aucun octet n'est envoyé à Gemini pendant que l'assistant parle
  (half-duplex, même règle que le callback micro).
"""

from __future__ import annotations

import asyncio
import collections
import io
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Optional, Protocol

import numpy as np

from core.screen_capture import WindowInfo, get_active_window, _hypr_env

try:
    import PIL.Image
    import PIL.ImageDraw
    import PIL.ImageOps
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False


# ── Budget machine modeste ───────────────────────────────────────────────────

DEFAULT_INTERVAL_S = 8.0
STATIC_INTERVAL_S = 20.0
QUIET_APP_INTERVAL_S = 30.0
CHANGE_INTERVAL_S = 6.0
SPEAKING_INTERVAL_S = 15.0

PROBE_SCALE = 0.20
SNAPSHOT_SCALE = 0.50
OCR_MAX_SIDE = 1024

PHASH_SIZE = 8
PHASH_DCT = 32
PHASH_SOFT = 8          # Hamming < 8 / 64 → écran statique
PHASH_HARD = 22         # Hamming ≥ 22     → changement visuel
SSIM_SIZE = 64
SSIM_THRESHOLD = 0.90   # en dessous → changement

WEBP_QUALITY = 70
WEBP_MAX_BYTES = 40_000
WEBP_MAX_SIDE = 1024
WEBP_METHOD = 0          # 0 = le plus rapide ; method=4 prenait ~3 s sur 2 cœurs
SCREEN_TICK_STALL_S = 8.0

OCR_TIMEOUT_S = 0.80
GRIM_PROBE_TIMEOUT_S = 1.2
GRIM_SNAP_TIMEOUT_S = 2.5

MIN_WINDOW_PX = 80

# Au-delà, un cliché n'est plus « l'écran sous les yeux ».
SNAPSHOT_MAX_AGE_S = 45.0
INJECT_DEDUP_S = 18.0


# ── Signaux extraits du texte ────────────────────────────────────────────────

_ERROR_RE = re.compile(
    r"(?i)\b(?:traceback|exception|error|erreur|panic|fatal|failed|failure|"
    r"échec|echec|segfault|undefined|denied|not found|eaddrinuse|"
    r"npm err|cargo(?:\s+error)?|rustc|error\[e\d+\]|compilation|"
    r"build failed|undefined reference|segmentation fault)\b"
    r"|Error:|ERROR:|FATAL:|FAILED|PANIC"
)

_FILE_RE = re.compile(
    r"(?:[/~.][\w./-]+|[\w.-]+)\."
    r"(?:py|rs|js|mjs|ts|tsx|jsx|go|c|cc|cpp|h|hpp|java|kt|rb|"
    r"sh|zsh|bash|yaml|yml|json|toml|md|vue|svelte|zig|lua)\b",
    re.IGNORECASE,
)

_SCREEN_PHRASES = (
    "qu'est-ce qui cloche",
    "qu est-ce qui cloche",
    "quest-ce qui cloche",
    "qu’est-ce qui cloche",
    "c'est quoi ce",
    "cest quoi ce",
    "c'est quoi l'erreur",
    "cest quoi l'erreur",
    "que vois-tu",
    "que vois tu",
    "qu'est-ce que tu vois",
    "quest-ce que tu vois",
    "regarde l'écran",
    "regarde l'ecran",
    "regarde l ecran",
    "à l'écran",
    "a l'écran",
    "a l'ecran",
    "sur l'écran",
    "sur l'ecran",
    "analyse l'écran",
    "lis l'écran",
    "lis l'ecran",
    "ce bug",
    "cette erreur",
    "cette trace",
    "ce traceback",
    "ça plante",
    "ca plante",
    "what's wrong",
    "what is wrong",
    "what do you see",
    "look at the screen",
    "look at my screen",
    "explique ce que tu vois",
    "tu vois ça",
    "tu vois ca",
)

_DEICTIC_RE = re.compile(
    r"(?i)\b(?:ici|ça|ca|cela|ceci|this|that|here|écran|ecran|"
    r"terminal|code|bug|erreur|error)\b"
)


class ConsciousnessHost(Protocol):
    """Sous-ensemble de JarvisLive utilisé par le démon."""

    session: Any
    _is_speaking: bool
    _model_turn_active: bool
    _interrupted: bool
    _activity_open: bool
    ui: Any


# ════════════════════════════════════════════════════════════════════════════
# Structures
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class TickStats:
    """Coût d'un tick, en millisecondes. Sert au budget CPU et aux tests."""

    capture_ms: float = 0.0
    phash_ms: float = 0.0
    ssim_ms: float = 0.0
    ocr_ms: float = 0.0
    webp_ms: float = 0.0
    total_ms: float = 0.0
    action: str = "skip"          # skip | static | change | first
    bytes_in: int = 0
    bytes_out: int = 0
    window_class: str = ""
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "capture_ms": round(self.capture_ms, 2),
            "phash_ms": round(self.phash_ms, 2),
            "ssim_ms": round(self.ssim_ms, 2),
            "ocr_ms": round(self.ocr_ms, 2),
            "webp_ms": round(self.webp_ms, 2),
            "total_ms": round(self.total_ms, 2),
            "action": self.action,
            "bytes_in": self.bytes_in,
            "bytes_out": self.bytes_out,
            "window_class": self.window_class,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class DiffResult:
    changed: bool
    reason: str                   # first | static | app_switch | visual
    phash_distance: int = 0
    ssim: float = 1.0


@dataclass(frozen=True)
class ScreenSignals:
    keywords: tuple[str, ...] = ()
    filenames: tuple[str, ...] = ()
    has_error: bool = False

    @property
    def empty(self) -> bool:
        return not self.keywords and not self.filenames


@dataclass
class ScreenSnapshot:
    """Dernier cliché utile de la fenêtre utilisateur."""

    window: WindowInfo
    phash: int
    webp_bytes: bytes
    mime_type: str = "image/webp"
    ocr_text: str = ""
    signals: ScreenSignals = field(default_factory=ScreenSignals)
    captured_at: float = field(default_factory=time.monotonic)
    gray64: Optional[np.ndarray] = None
    stats: Optional[TickStats] = None

    @property
    def age_s(self) -> float:
        return max(0.0, time.monotonic() - self.captured_at)

    @property
    def ocr_usable(self) -> bool:
        return len(self.ocr_text) >= 40 and len(self.ocr_text.split()) >= 6

    def as_meta(self) -> dict[str, Any]:
        return {
            "window_class": self.window.window_class,
            "window_title": self.window.title,
            "window_address": self.window.address,
            "geometry": self.window.geometry_str,
            "is_terminal": self.window.is_terminal,
            "is_ide": self.window.is_ide,
            "is_browser": self.window.is_browser,
            "byte_size": len(self.webp_bytes),
            "mime_type": self.mime_type,
            "age_s": round(self.age_s, 2),
            "keywords": list(self.signals.keywords),
            "filenames": list(self.signals.filenames),
            "has_error": self.signals.has_error,
            "source": "screen_consciousness",
        }

    def context_text(self, query: str = "") -> str:
        """Bloc textuel à coller au tour Live. Jamais lu à voix haute."""
        win = self.window
        lines = [
            "[CONTEXTE ÉCRAN — capture locale proactive, ne pas lire à voix haute]",
            f'Fenêtre: {win.window_class} — "{win.title}"',
        ]
        if self.signals.keywords:
            lines.append("Signaux: " + ", ".join(self.signals.keywords[:12]))
        if self.signals.filenames:
            lines.append("Fichiers: " + ", ".join(self.signals.filenames[:8]))
        if query:
            lines.append(f'Question: "{query[:180]}"')
        if self.ocr_text:
            excerpt = self.ocr_text.strip()[:1200]
            lines.append("Texte OCR (extrait):")
            lines.append(excerpt)
        return "\n".join(lines)

    def as_tool_result(self, question: str) -> str:
        source = f"veille visuelle, {self.age_s:.0f}s"
        body = self.ocr_text.strip() or "(aucun texte exploitable)"
        return (
            f"[TEXTE DE L'ÉCRAN — {source}, aucune image envoyée]\n"
            f"{body}\n\n"
            "Ce texte est la lecture exacte de l'écran : réponds à partir de "
            "lui, sans rien inventer et sans rappeler l'outil. Si la réponse à "
            f"« {question[:120]} » ne s'y trouve pas, dis simplement ce que tu "
            "vois d'utile."
        )


# ════════════════════════════════════════════════════════════════════════════
# Image : pHash, SSIM, WebP
# ════════════════════════════════════════════════════════════════════════════

def _open_image(src: Any) -> tuple[Any, bool]:
    """Ouvre une image. Le booléen dit si l'appelant doit la fermer."""
    if not _PIL_AVAILABLE:
        raise RuntimeError("Pillow est requis pour la veille d'écran")
    if isinstance(src, PIL.Image.Image):
        return src, False
    if isinstance(src, (bytes, bytearray, memoryview)):
        return PIL.Image.open(io.BytesIO(bytes(src))), True
    raise TypeError(f"image inattendue: {type(src)!r}")


def _to_gray_array(image: Any, size: int) -> np.ndarray:
    gray = image.convert("L").resize((size, size), PIL.Image.Resampling.BILINEAR)
    return np.asarray(gray, dtype=np.float64)


def _dct_matrix(n: int) -> np.ndarray:
    """Matrice DCT-II orthonormée n×n, calculée une fois par taille."""
    x = np.arange(n, dtype=np.float64)
    k = x.reshape(-1, 1)
    m = np.cos(np.pi * (2.0 * x + 1.0) * k / (2.0 * n))
    m[0] *= 1.0 / np.sqrt(2.0)
    m *= np.sqrt(2.0 / n)
    return m


_DCT32 = _dct_matrix(PHASH_DCT)


def perceptual_hash(src: Any, hash_size: int = PHASH_SIZE) -> int:
    """pHash DCT 64 bits. Insensible à un décalage d'horloge d'une seconde."""
    im, owned = _open_image(src)
    try:
        pixels = _to_gray_array(im, PHASH_DCT)
    finally:
        if owned:
            im.close()
    dct = _DCT32 @ pixels @ _DCT32.T
    low = dct[:hash_size, :hash_size].copy()
    low[0, 0] = 0.0                       # ignore la composante DC
    median = float(np.median(low))
    bits = (low > median).astype(np.uint8).flatten()
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return int(value)


def hamming_distance(a: int, b: int) -> int:
    return int((int(a) ^ int(b)).bit_count())


def ssim_score(a: np.ndarray, b: np.ndarray) -> float:
    """SSIM global sur deux matrices [0, 255] ou [0, 1] de même forme."""
    if a.shape != b.shape:
        raise ValueError("SSIM: formes incompatibles")
    x = a.astype(np.float64)
    y = b.astype(np.float64)
    if x.max() > 1.5:
        x = x / 255.0
        y = y / 255.0
    mu_x = float(x.mean())
    mu_y = float(y.mean())
    var_x = float(x.var())
    var_y = float(y.var())
    cov = float(((x - mu_x) * (y - mu_y)).mean())
    c1 = 0.01 ** 2
    c2 = 0.03 ** 2
    num = (2.0 * mu_x * mu_y + c1) * (2.0 * cov + c2)
    den = (mu_x ** 2 + mu_y ** 2 + c1) * (var_x + var_y + c2)
    if den <= 1e-12:
        return 1.0 if abs(num) <= 1e-12 else 0.0
    return float(max(0.0, min(1.0, num / den)))


def snapshot_capture_scale(
    size: tuple[int, int],
    *,
    max_side: int = WEBP_MAX_SIDE,
    cap: float = SNAPSHOT_SCALE,
) -> float:
    """Échelle grim : jamais plus que ``max_side`` px, déjà assez pour WebP et OCR."""
    longest = max(int(size[0]), int(size[1]), 1)
    return float(min(cap, max_side / float(longest)))


def compress_webp(
    src: Any,
    *,
    quality: int = WEBP_QUALITY,
    max_bytes: int = WEBP_MAX_BYTES,
    max_side: int = WEBP_MAX_SIDE,
) -> tuple[bytes, str]:
    """WebP q70, 1024 px, une seule passe. Repli JPEG si le codec WebP manque.

    ``method=0`` et le redimensionnement *avant* l'encode tiennent le budget
    CPU (< 300 ms sur deux cœurs). Si le fichier dépasse encore ``max_bytes``
    (scène très bruitée), on le rend tel quel : reboucler jusqu'à cinq fois
    bloquait le pool ``compute-light`` et faisait bégayer la voix.
    """
    if not _PIL_AVAILABLE:
        raw = bytes(src) if isinstance(src, (bytes, bytearray)) else b""
        return raw, "image/png"

    original, owned = _open_image(src)
    try:
        try:
            # JPEG : décoder déjà à 1024 px (DCT 1/2, 1/4, 1/8) au lieu
            # de matérialiser le plein cadre puis de le réduire.
            original.draft("RGB", (max_side, max_side))
        except Exception:
            pass
        img = original.convert("RGB")
        if img is original:
            img = original.copy()
    finally:
        if owned:
            original.close()

    try:
        if max(img.size) > max_side:
            img.thumbnail((max_side, max_side), PIL.Image.Resampling.BOX)

        q = int(quality)

        def _save(fmt: str) -> bytes:
            buf = io.BytesIO()
            if fmt == "WEBP":
                img.save(
                    buf,
                    format="WEBP",
                    quality=q,
                    method=WEBP_METHOD,
                )
            else:
                img.save(
                    buf,
                    format="JPEG",
                    quality=min(q, 75),
                    optimize=False,
                    progressive=False,
                    subsampling="4:2:0",
                )
            return buf.getvalue()

        try:
            payload = _save("WEBP")
            mime = "image/webp"
        except Exception:
            payload = _save("JPEG")
            mime = "image/jpeg"
        # max_bytes : cible, pas une boucle. Au-delà on rend le fichier tel quel
        # (un bureau photographique à 1024 px / q70 peut dépasser 40 Ko).
        return payload, mime
    finally:
        img.close()


def diff_snapshots(
    previous: Optional[ScreenSnapshot],
    window: WindowInfo,
    new_hash: int,
    new_gray64: Optional[np.ndarray] = None,
) -> DiffResult:
    """Décide si le cliché mérite d'être promu (OCR + WebP + éventuellement réseau)."""
    if previous is None:
        return DiffResult(True, "first", 64, 0.0)

    if (
        previous.window.address
        and window.address
        and previous.window.address != window.address
    ) or (previous.window.window_class or "").casefold() != (
        window.window_class or ""
    ).casefold():
        return DiffResult(True, "app_switch", 64, 0.0)

    distance = hamming_distance(previous.phash, new_hash)
    if distance < PHASH_SOFT:
        return DiffResult(False, "static", distance, 1.0)
    if distance >= PHASH_HARD:
        return DiffResult(True, "visual", distance, 0.0)

    ssim = 1.0
    if previous.gray64 is not None and new_gray64 is not None:
        ssim = ssim_score(previous.gray64, new_gray64)
        if ssim >= SSIM_THRESHOLD:
            return DiffResult(False, "static", distance, ssim)
        return DiffResult(True, "visual", distance, ssim)

    # Zone grise sans SSIM : on reste prudent, on ne déclenche pas.
    return DiffResult(False, "static", distance, 1.0)


# ════════════════════════════════════════════════════════════════════════════
# OCR local ultra-léger (Tesseract CLI, jamais PaddleOCR en continu)
# ════════════════════════════════════════════════════════════════════════════

def extract_signals(text: str) -> ScreenSignals:
    """Mots-clés d'erreur et noms de fichiers, sans appel réseau."""
    raw = (text or "").strip()
    if not raw:
        return ScreenSignals()
    keywords: list[str] = []
    seen: set[str] = set()
    for match in _ERROR_RE.finditer(raw):
        token = re.sub(r"\s+", " ", match.group(0)).strip().casefold()
        if token and token not in seen:
            seen.add(token)
            keywords.append(token)
    files: list[str] = []
    fseen: set[str] = set()
    for match in _FILE_RE.finditer(raw):
        name = match.group(0).strip()
        key = name.casefold()
        if key and key not in fseen:
            fseen.add(key)
            files.append(name)
    return ScreenSignals(
        keywords=tuple(keywords[:16]),
        filenames=tuple(files[:12]),
        has_error=bool(keywords),
    )


def _tesseract_languages() -> tuple[str, ...]:
    """Langues réellement installées : eng+fra si présentes, sinon rien."""
    tessdata = os.environ.get("TESSDATA_PREFIX", "/usr/share/tessdata")
    available: list[str] = []
    for lang in ("eng", "fra"):
        if os.path.exists(os.path.join(tessdata, f"{lang}.traineddata")):
            available.append(lang)
    if available:
        return ("+".join(available),)
    return ()


def ocr_tesseract(
    image_bytes: bytes,
    *,
    timeout_s: float = OCR_TIMEOUT_S,
) -> str:
    """OCR CLI. Rend '' si tesseract, les langues ou le délai manquent."""
    if not image_bytes or not shutil.which("tesseract"):
        return ""
    langs = _tesseract_languages()
    if not langs:
        return ""
    if not _PIL_AVAILABLE:
        return ""

    png = image_bytes
    try:
        im, owned = _open_image(image_bytes)
        try:
            try:
                im.draft("L", (OCR_MAX_SIDE, OCR_MAX_SIDE))
            except Exception:
                pass
            gray = im.convert("L")
            gray.thumbnail((OCR_MAX_SIDE, OCR_MAX_SIDE), PIL.Image.Resampling.BILINEAR)
            enhanced = PIL.ImageOps.autocontrast(gray, cutoff=2)
            buf = io.BytesIO()
            enhanced.save(buf, format="PNG", optimize=False)
            png = buf.getvalue()
        finally:
            if owned:
                im.close()
    except Exception:
        return ""

    tmp_path = ""
    try:
        fd, tmp_path = tempfile.mkstemp(prefix="ano-ocr-", suffix=".png")
        os.close(fd)
        with open(tmp_path, "wb") as handle:
            handle.write(png)
        proc = subprocess.run(
            [
                "tesseract", tmp_path, "stdout",
                "-l", langs[0],
                "--psm", "6",
                "--oem", "1",
            ],
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
        if proc.returncode != 0:
            return ""
        text = proc.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired):
        return ""
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    return _clean_ocr(text)


def _clean_ocr(text: str) -> str:
    lines: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if len(stripped) < 2 and not stripped.isalnum():
            continue
        lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()[:4000]


def wants_screen_context(
    query: str,
    snapshot: Optional[ScreenSnapshot] = None,
) -> bool:
    """Vrai si la phrase porte sur ce qui est affiché, pas sur le monde."""
    lowered = " ".join((query or "").casefold().split())
    if not lowered:
        return False
    if any(phrase in lowered for phrase in _SCREEN_PHRASES):
        return True
    if snapshot is not None and snapshot.signals.has_error and _DEICTIC_RE.search(lowered):
        return True
    if snapshot is not None and (snapshot.window.is_terminal or snapshot.window.is_ide):
        if _DEICTIC_RE.search(lowered) and any(
            token in lowered
            for token in ("quoi", "pourquoi", "aide", "bug", "erreur", "cloche", "plante")
        ):
            return True
    return False


# ════════════════════════════════════════════════════════════════════════════
# Capture ROI Hyprland
# ════════════════════════════════════════════════════════════════════════════

def grim_roi(
    geometry: str,
    *,
    scale: float = 1.0,
    fmt: str = "jpeg",
    quality: int = 70,
    timeout: float = GRIM_PROBE_TIMEOUT_S,
) -> bytes:
    """`grim -g 'X,Y WxH' -s SCALE -t jpeg` → bytes mémoire."""
    if not geometry:
        raise RuntimeError("géométrie de fenêtre vide")
    if not shutil.which("grim"):
        raise RuntimeError("grim n'est pas installé")
    cmd = ["grim", "-s", f"{scale:.3f}", "-t", fmt]
    if fmt in {"jpeg", "jpg"}:
        cmd += ["-q", str(int(quality))]
    cmd += ["-g", geometry, "-"]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        timeout=timeout,
        env=_hypr_env(),
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout:
        err = (proc.stderr or b"").decode("utf-8", "ignore").strip() or "vide"
        raise RuntimeError(f"grim ROI: {err}")
    return proc.stdout


def _should_ocr(window: WindowInfo, diff: DiffResult) -> bool:
    if diff.reason == "static":
        return False
    if window.is_terminal or window.is_ide:
        return True
    title = (window.title or "").casefold()
    return any(token in title for token in ("error", "erreur", "failed", "cargo", "pytest"))


# ════════════════════════════════════════════════════════════════════════════
# Réveil Hyprland (socket2) — zéro hyprctl entre deux évènements
# ════════════════════════════════════════════════════════════════════════════

class _HyprWake:
    """Écoute ``activewindow`` / ``workspace`` et réveille le démon."""

    def __init__(self, on_wake: Callable[[], None]) -> None:
        self._on_wake = on_wake
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> bool:
        if self._thread is not None:
            return True
        try:
            from core.hypr_focus import available
            if not available():
                return False
        except Exception:
            return False
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="ano-scrn-hypr", daemon=True
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        self._thread = None

    def _run(self) -> None:
        from core.hypr_focus import socket_path
        import socket as _socket

        while not self._stop.is_set():
            path = socket_path()
            if path is None:
                time.sleep(2.0)
                continue
            try:
                with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as sock:
                    sock.settimeout(1.0)
                    sock.connect(str(path))
                    self._pump(sock)
            except Exception:
                time.sleep(2.0)

    def _pump(self, sock: Any) -> None:
        buffer = b""
        while not self._stop.is_set():
            try:
                chunk = sock.recv(4096)
            except TimeoutError:
                continue
            except OSError:
                return
            if not chunk:
                return
            buffer += chunk
            *lines, buffer = buffer.split(b"\n")
            for line in lines:
                text = line.decode("utf-8", "replace")
                if text.startswith(("activewindow>>", "workspace>>", "focusedmon>>")):
                    try:
                        self._on_wake()
                    except Exception:
                        pass


# ════════════════════════════════════════════════════════════════════════════
# Démon
# ════════════════════════════════════════════════════════════════════════════

class ScreenConsciousness:
    """Veille visuelle : capture ROI, diff local, injection Live à la demande."""

    def __init__(
        self,
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
        capture_fn: Optional[Callable[..., bytes]] = None,
        window_fn: Optional[Callable[..., Optional[WindowInfo]]] = None,
        ocr_fn: Optional[Callable[..., str]] = None,
        enabled: bool = True,
    ) -> None:
        self.interval_s = float(interval_s)
        self._capture_fn = capture_fn or grim_roi
        self._window_fn = window_fn or (lambda: get_active_window(skip_anogpt=True))
        self._ocr_fn = ocr_fn or ocr_tesseract
        self.enabled = enabled

        self._snapshot: Optional[ScreenSnapshot] = None
        self._lock = threading.RLock()
        self._stats: Deque[TickStats] = collections.deque(maxlen=40)
        self._last_stats: Optional[TickStats] = None
        self._ticks = 0
        self._network_sends = 0

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._wake: Optional[asyncio.Event] = None
        self._hypr: Optional[_HyprWake] = None
        self._running = False
        self._host: Any = None

        self._last_injected_hash: Optional[int] = None
        self._last_injected_at = 0.0
        self._pending_query: str = ""
        self._injecting = False
        self._next_interval = float(interval_s)

    # ── état public ────────────────────────────────────────────────────────

    def snapshot(self) -> Optional[ScreenSnapshot]:
        with self._lock:
            return self._snapshot

    def cached_capture(
        self,
        max_age_s: float = 8.0,
        window_class: Optional[str] = None,
    ) -> Optional[ScreenSnapshot]:
        snap = self.snapshot()
        if snap is None or snap.age_s > max_age_s:
            return None
        if window_class:
            current = (snap.window.window_class or "").casefold()
            if current and current != window_class.casefold():
                return None
        return snap

    def last_stats(self) -> Optional[TickStats]:
        return self._last_stats

    def cpu_budget(self) -> dict[str, float]:
        """Estimation : (ms/tick) / intervalle → % d'un cœur."""
        samples = [s.total_ms for s in self._stats if s.total_ms > 0]
        if not samples:
            return {"tick_ms_p50": 0.0, "tick_ms_p95": 0.0, "cpu_pct_one_core": 0.0}
        ordered = sorted(samples)
        p50 = ordered[len(ordered) // 2]
        p95 = ordered[int(len(ordered) * 0.95)]
        interval_ms = max(1.0, self.interval_s * 1000.0)
        return {
            "tick_ms_p50": round(p50, 2),
            "tick_ms_p95": round(p95, 2),
            "cpu_pct_one_core": round((p50 / interval_ms) * 100.0, 3),
            "ticks": float(self._ticks),
            "network_sends": float(self._network_sends),
        }

    def wants_context(self, query: str) -> bool:
        return wants_screen_context(query, self.snapshot())

    # ── tick synchrone (pool compute-light) ────────────────────────────────

    def tick(self, *, force: bool = False) -> TickStats:
        """Un cycle : ROI minuscule, hash, éventuellement WebP + OCR.

        Jamais d'envoi réseau ici. ``force`` ignore le seuil de stabilité
        (utilisé pour un premier cliché ou un réveil fenêtre).
        """
        started = time.perf_counter()
        stats = TickStats()
        if not self.enabled or not _PIL_AVAILABLE:
            stats.action = "skip"
            stats.reason = "disabled"
            stats.total_ms = (time.perf_counter() - started) * 1000.0
            self._commit_stats(stats)
            return stats

        try:
            window = self._window_fn()
        except Exception as exc:
            stats.action = "skip"
            stats.reason = f"window:{exc}"
            stats.total_ms = (time.perf_counter() - started) * 1000.0
            self._commit_stats(stats)
            return stats

        if window is None or not window.geometry_str:
            stats.action = "skip"
            stats.reason = "no-window"
            stats.total_ms = (time.perf_counter() - started) * 1000.0
            self._commit_stats(stats)
            return stats

        if window.size[0] < MIN_WINDOW_PX or window.size[1] < MIN_WINDOW_PX:
            stats.action = "skip"
            stats.reason = "tiny-window"
            stats.total_ms = (time.perf_counter() - started) * 1000.0
            self._commit_stats(stats)
            return stats

        stats.window_class = window.window_class
        previous = self.snapshot()
        if (
            not force
            and previous is not None
            and previous.window.address == window.address
            and previous.window.title == window.title
            and not window.is_terminal
            and not window.is_ide
            and previous.age_s < QUIET_APP_INTERVAL_S
        ):
            # Navigateur, PDF, lecteur : le titre n'a pas bougé, grim peut
            # attendre. Un terminal/IDE, lui, peut afficher une erreur rouge
            # sans changer de titre.
            stats.action = "skip"
            stats.reason = "quiet-app"
            stats.total_ms = (time.perf_counter() - started) * 1000.0
            self._next_interval = QUIET_APP_INTERVAL_S
            self._commit_stats(stats)
            return stats

        t0 = time.perf_counter()
        try:
            probe = self._capture_fn(
                window.geometry_str,
                scale=PROBE_SCALE,
                fmt="jpeg",
                quality=55,
                timeout=GRIM_PROBE_TIMEOUT_S,
            )
        except TypeError:
            probe = self._capture_fn(window.geometry_str)
        except Exception as exc:
            stats.action = "skip"
            stats.reason = f"capture:{exc}"
            stats.total_ms = (time.perf_counter() - started) * 1000.0
            self._commit_stats(stats)
            return stats
        stats.capture_ms = (time.perf_counter() - t0) * 1000.0
        stats.bytes_in = len(probe)

        try:
            probe_im, probe_owned = _open_image(probe)
            try:
                t1 = time.perf_counter()
                new_hash = perceptual_hash(probe_im)
                stats.phash_ms = (time.perf_counter() - t1) * 1000.0
                gray64 = _to_gray_array(probe_im, SSIM_SIZE)
            finally:
                if probe_owned:
                    probe_im.close()
        except Exception as exc:
            stats.action = "skip"
            stats.reason = f"hash:{exc}"
            stats.total_ms = (time.perf_counter() - started) * 1000.0
            self._commit_stats(stats)
            return stats

        t2 = time.perf_counter()
        diff = diff_snapshots(previous, window, new_hash, gray64)
        stats.ssim_ms = (time.perf_counter() - t2) * 1000.0
        stats.reason = diff.reason

        if not diff.changed and not force:
            stats.action = "static"
            stats.total_ms = (time.perf_counter() - started) * 1000.0
            self._next_interval = STATIC_INTERVAL_S
            self._commit_stats(stats)
            return stats

        # Changement : cliché plus lisible, WebP, OCR éventuel.
        t3 = time.perf_counter()
        try:
            raw = self._capture_fn(
                window.geometry_str,
                scale=snapshot_capture_scale(window.size),
                fmt="jpeg",
                quality=WEBP_QUALITY,
                timeout=GRIM_SNAP_TIMEOUT_S,
            )
        except TypeError:
            raw = probe
        except Exception:
            raw = probe
        stats.capture_ms += (time.perf_counter() - t3) * 1000.0
        t4 = time.perf_counter()
        webp, mime = compress_webp(raw)
        stats.webp_ms = (time.perf_counter() - t4) * 1000.0
        stats.bytes_out = len(webp)

        ocr_text = ""
        signals = ScreenSignals()
        if _should_ocr(window, diff):
            t5 = time.perf_counter()
            try:
                ocr_text = self._ocr_fn(raw) or ""
            except Exception:
                ocr_text = ""
            stats.ocr_ms = (time.perf_counter() - t5) * 1000.0
            signals = extract_signals(ocr_text) if ocr_text else extract_signals(window.title)

        snap = ScreenSnapshot(
            window=window,
            phash=new_hash,
            webp_bytes=webp,
            mime_type=mime,
            ocr_text=ocr_text,
            signals=signals,
            captured_at=time.monotonic(),
            gray64=gray64,
            stats=stats,
        )
        with self._lock:
            self._snapshot = snap

        stats.action = "first" if diff.reason == "first" else "change"
        stats.total_ms = (time.perf_counter() - started) * 1000.0
        self._next_interval = CHANGE_INTERVAL_S if diff.reason == "app_switch" else self.interval_s
        self._commit_stats(stats)
        self._publish_change(snap, diff)
        if stats.action == "change":
            print(
                f"[Écran] Δ {diff.reason} {window.window_class} "
                f"d={diff.phash_distance} ssim={diff.ssim:.2f} "
                f"{len(webp)} o webp={stats.webp_ms:.0f}ms tot={stats.total_ms:.0f}ms"
                + (" ⚠" if signals.has_error else "")
            )
        return stats

    def _commit_stats(self, stats: TickStats) -> None:
        self._last_stats = stats
        self._stats.append(stats)
        self._ticks += 1
        if self._ticks % 20 == 0:
            budget = self.cpu_budget()
            print(
                f"[Écran] veille {budget['tick_ms_p50']:.1f} ms/tick "
                f"(p95 {budget['tick_ms_p95']:.1f}) "
                f"≈ {budget['cpu_pct_one_core']:.2f} % d'un cœur "
                f"@ {self.interval_s:.1f}s — réseau={self._network_sends}"
            )

    def _publish_change(self, snap: ScreenSnapshot, diff: DiffResult) -> None:
        try:
            from core.event_bus import AsyncEventBus, ScreenChangeEvent

            event = ScreenChangeEvent(
                window_class=snap.window.window_class,
                window_title=snap.window.title,
                reason=diff.reason,
                phash_distance=diff.phash_distance,
                ssim=diff.ssim,
                keywords=snap.signals.keywords,
                has_error=snap.signals.has_error,
            )
            AsyncEventBus.get_instance().publish_sync(event)
        except Exception:
            pass

    # ── pause / half-duplex ────────────────────────────────────────────────

    def _should_pause(self, host: Any) -> bool:
        if host is None:
            return False
        if (
            getattr(host, "_is_speaking", False)
            or getattr(host, "_model_turn_active", False)
            or getattr(host, "_is_thinking", False)
            or getattr(host, "_activity_open", False)
        ):
            return True
        return False

    # ── injection Gemini Live ──────────────────────────────────────────────

    async def inject_into_live(self, host: Any, query: str) -> bool:
        """Pousse le WebP courant dans le WebSocket Live.

        Zéro envoi si l'assistant parle. Une demande explicite (« regarde mon
        écran ») force un cliché neuf : une image de veille peut être statique
        mais ne plus représenter précisément ce que l'utilisateur désigne.
        Ne déclenche pas de tour vocal supplémentaire : l'image s'attache à la
        question en cours.
        """
        if not self.wants_context(query):
            return False
        if getattr(host, "_is_speaking", False):
            self._pending_query = query
            return False
        session = getattr(host, "session", None)
        if session is None:
            return False
        if self._injecting:
            return False

        # La capture est faite hors de la boucle asyncio/audio. Elle est
        # volontairement fraîche pour que le modèle voie l'écran au moment
        # exact de la question, pas le cliché du démon de veille.
        try:
            await asyncio.to_thread(self.tick, force=True)
        except Exception as exc:
            print(f"[Écran] tick forcé impossible : {exc}")
        snap = self.snapshot()
        if snap is None or not snap.webp_bytes:
            return False

        now = time.monotonic()
        self._injecting = True
        self._pending_query = ""
        try:
            await session.send_realtime_input(
                video={"data": snap.webp_bytes, "mime_type": snap.mime_type}
            )
            note = snap.context_text(query)
            await session.send_realtime_input(text=note)
            self._last_injected_hash = snap.phash
            self._last_injected_at = now
            self._network_sends += 1
            print(
                f"[Écran] 📤 {len(snap.webp_bytes)} o {snap.mime_type} "
                f"({snap.window.window_class}) → Live"
            )
            return True
        except Exception as exc:
            print(f"[Écran] injection Live refusée : {exc}")
            return False
        finally:
            self._injecting = False

    # ── démon async ────────────────────────────────────────────────────────

    async def run(self, host: Any = None) -> None:
        """Boucle de veille. Annulable. Capture hors de la boucle audio."""
        if self._running:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise
            return

        self._running = True
        self._host = host
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self._hypr = _HyprWake(self._wake_from_thread)
        self._hypr.start()
        print(
            f"[Écran] veille visuelle active (ROI Hyprland, {self.interval_s:.1f}s, "
            "zéro réseau tant que l'écran est statique)."
        )
        try:
            # Premier cliché tout de suite, pour qu'une question précoce
            # trouve déjà une image — mais pas pendant que l'assistant parle :
            # grim + DCT partagent le GIL avec l'audio.
            if not self._should_pause(host):
                await self._run_tick(force=True)
            while True:
                if self._should_pause(host):
                    timeout = SPEAKING_INTERVAL_S
                else:
                    timeout = max(0.4, self._next_interval)
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    pass
                if self._wake.is_set():
                    self._wake.clear()
                    # Un changement de fenêtre Hyprland force le cliché.
                    if not self._should_pause(host):
                        await self._run_tick(force=True)
                        continue
                if self._should_pause(host):
                    continue
                await self._run_tick(force=False)
                pending = self._pending_query
                if pending and host is not None and not getattr(host, "_is_speaking", False):
                    await self.inject_into_live(host, pending)
        except asyncio.CancelledError:
            raise
        finally:
            if self._hypr is not None:
                self._hypr.stop()
            self._running = False
            print("[Écran] veille visuelle arrêtée.")

    def _wake_from_thread(self) -> None:
        loop = self._loop
        wake = self._wake
        if loop is None or wake is None:
            return
        try:
            loop.call_soon_threadsafe(wake.set)
        except RuntimeError:
            pass

    def wake(self) -> None:
        """Réveil manuel (tests, changement de bureau forcé)."""
        if self._wake is not None:
            self._wake.set()

    async def _run_tick(self, *, force: bool = False) -> TickStats:
        try:
            from core.thread_pool import get_thread_pool

            future = get_thread_pool().submit(
                "compute-light",
                self.tick,
                task_name="scrn",
                stall_timeout=SCREEN_TICK_STALL_S,
                force=force,
            )
            return await future
        except Exception:
            return await asyncio.to_thread(self.tick, force=force)


# Instance de process : JarvisLive y accroche son démon.
_default: Optional[ScreenConsciousness] = None
_default_lock = threading.Lock()


def get_screen_consciousness() -> ScreenConsciousness:
    global _default
    with _default_lock:
        if _default is None:
            _default = ScreenConsciousness()
        return _default


# ════════════════════════════════════════════════════════════════════════════
# Benchmark CPU
# ════════════════════════════════════════════════════════════════════════════

def _synthetic_terminal(width: int = 1920, height: int = 1080, seed: int = 0) -> bytes:
    if not _PIL_AVAILABLE:
        raise RuntimeError("Pillow requis")
    rng = np.random.default_rng(seed)
    img = PIL.Image.new("RGB", (width, height), (12, 12, 16))
    draw = PIL.ImageDraw.Draw(img)
    for row, line in enumerate((
        "anonymous@arch ~/OUTILS/ANO-GPT % cargo build",
        "   Compiling ano-gpt v0.1.0",
        "error[E0308]: mismatched types",
        "  --> src/main.rs:42:5",
        "error: could not compile `ano-gpt` due to previous error",
        "FAILED tests/test_math.py::test_division",
    )):
        color = (220, 70, 70) if "error" in line.casefold() or "fail" in line.casefold() else (180, 220, 180)
        draw.text((24, 40 + row * 28), line, fill=color)
    noise = rng.integers(0, 8, size=(height, width, 3), dtype=np.uint8)
    arr = np.asarray(img, dtype=np.uint8)
    mixed = np.clip(arr.astype(np.int16) + noise, 0, 255).astype(np.uint8)
    out = PIL.Image.fromarray(mixed, "RGB")
    buf = io.BytesIO()
    out.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def benchmark_cpu(rounds: int = 40, include_live: bool = False) -> dict[str, Any]:
    """Mesure pHash / SSIM / WebP / tick synthétique. Rien n'est envoyé au réseau."""
    if not _PIL_AVAILABLE:
        raise RuntimeError("Pillow requis pour le benchmark")

    frame_a = _synthetic_terminal(seed=1)
    frame_b = _synthetic_terminal(seed=2)
    im_a, owned_a = _open_image(frame_a)
    im_b, owned_b = _open_image(frame_b)
    try:
        img_a = im_a.convert("RGB")
        img_b = im_b.convert("RGB")
    finally:
        if owned_a:
            im_a.close()
        if owned_b:
            im_b.close()

    def _timed(fn: Callable[[], Any], n: int) -> float:
        # Amorce
        fn()
        t0 = time.perf_counter()
        for _ in range(n):
            fn()
        return (time.perf_counter() - t0) / n * 1000.0

    phash_ms = _timed(lambda: perceptual_hash(img_a), rounds)
    gray_a = _to_gray_array(img_a, SSIM_SIZE)
    gray_b = _to_gray_array(img_b, SSIM_SIZE)
    ssim_ms = _timed(lambda: ssim_score(gray_a, gray_b), rounds)
    webp_ms = _timed(lambda: compress_webp(img_a), max(8, rounds // 2))
    webp_bytes, mime = compress_webp(img_a)
    hash_a = perceptual_hash(img_a)
    hash_b = perceptual_hash(img_b)

    # Tick synthétique : capture déjà en mémoire, donc le coût mesuré ici
    # est le plafond CPU *après* grim (le grim réel s'ajoute).
    captured = {"n": 0}

    def _fake_capture(geometry: str, **kwargs: Any) -> bytes:
        captured["n"] += 1
        return frame_a

    mind = ScreenConsciousness(
        interval_s=DEFAULT_INTERVAL_S,
        capture_fn=_fake_capture,
        window_fn=lambda: WindowInfo(
            address="0xterm",
            window_class="kitty",
            title="cargo build",
            at=(0, 0),
            size=(1920, 1080),
        ),
        ocr_fn=lambda _b: "error[E0308]: mismatched types\n  --> src/main.rs:42:5",
    )
    first = mind.tick(force=True)
    static_samples = [mind.tick(force=False).total_ms for _ in range(rounds)]
    static_p50 = sorted(static_samples)[len(static_samples) // 2]

    live: dict[str, Any] = {}
    if include_live and shutil.which("grim") and shutil.which("hyprctl"):
        live_mind = ScreenConsciousness(interval_s=DEFAULT_INTERVAL_S)
        try:
            live_first = live_mind.tick(force=True)
            live_static = live_mind.tick(force=False)
            live = {
                "first": live_first.as_dict(),
                "static": live_static.as_dict(),
            }
        except Exception as extra:
            live = {"error": str(extra)}

    interval_ms = DEFAULT_INTERVAL_S * 1000.0
    report = {
        "phash_ms": round(phash_ms, 3),
        "ssim_ms": round(ssim_ms, 3),
        "webp_ms": round(webp_ms, 3),
        "webp_bytes": len(webp_bytes),
        "webp_mime": mime,
        "hamming_same_seed_noise": hamming_distance(hash_a, hash_b),
        "tick_first_ms": round(first.total_ms, 2),
        "tick_static_p50_ms": round(static_p50, 3),
        "cpu_pct_static_one_core": round((static_p50 / interval_ms) * 100.0, 4),
        "cpu_pct_first_one_core": round((first.total_ms / interval_ms) * 100.0, 4),
        "interval_s": DEFAULT_INTERVAL_S,
        "probe_scale": PROBE_SCALE,
        "snapshot_scale": SNAPSHOT_SCALE,
        "live_tick": live,
        "notes": (
            "Le chemin statique (pHash + éventuellement SSIM, zéro OCR, zéro "
            "réseau) est le régime normal. grim ROI s'ajoute en réel : "
            "PROBE_SCALE=0.20 limite le décodage à ~1/25e des pixels 4K."
        ),
    }
    return report


def _print_benchmark(report: dict[str, Any]) -> None:
    print("=" * 72)
    print("  BENCHMARK CPU — screen_consciousness (ANO-GPT, machine 2 cœurs)")
    print("=" * 72)
    print(f"  pHash DCT 32×32          : {report['phash_ms']:.3f} ms")
    print(f"  SSIM 64×64               : {report['ssim_ms']:.3f} ms")
    print(f"  WebP q70 une passe (<40 Ko visé) : {report['webp_ms']:.3f} ms  → {report['webp_bytes']} o ({report['webp_mime']})")
    print(f"  Hamming 2 frames bruitées: {report['hamming_same_seed_noise']}")
    print(f"  Tick FIRST (WebP+OCR)    : {report['tick_first_ms']:.2f} ms  ≈ {report['cpu_pct_first_one_core']:.3f} % d'un cœur")
    print(f"  Tick STATIC p50          : {report['tick_static_p50_ms']:.3f} ms  ≈ {report['cpu_pct_static_one_core']:.4f} % d'un cœur")
    print(f"  Intervalle               : {report['interval_s']:.1f}s   probe×{report['probe_scale']}  snap×{report['snapshot_scale']}")
    if report.get("live_tick"):
        print(f"  Tick LIVE grim           : {report['live_tick']}")
    print()
    print("  " + report["notes"])
    print("=" * 72)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Veille d'écran ANO-GPT")
    parser.add_argument("--benchmark", action="store_true", help="Mesure CPU synthétique")
    parser.add_argument("--live", action="store_true", help="Inclut un tick grim réel")
    parser.add_argument("--rounds", type=int, default=40)
    args = parser.parse_args()
    _print_benchmark(benchmark_cpu(rounds=args.rounds, include_live=args.live))
