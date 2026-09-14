"""core/screen_reader.py — Lire l'écran localement avant d'y envoyer un modèle.

La plupart des questions posées à un écran portent sur du texte : un message
d'erreur, une notification, une ligne de log, un prix. Envoyer l'image entière
à Gemini pour qu'il relise ce texte coûte un aller-retour, des jetons, et
introduit un risque particulier : un modèle qui « lit » une trace d'erreur peut
en inventer un mot. Tesseract, lui, ne devine pas — il rend ce qui est écrit,
ou rien.

D'où l'ordre : OCR local d'abord, image seulement si l'OCR ne suffit pas.

L'OCR n'est pas gratuit sur cette machine (une à trois secondes sur un écran
1080p) ; c'est le poste à surveiller. Deux garde-fous :

* **le cache** — l'écran est empreinté avant d'être lu. Reposer une question
  sur un écran inchangé ne relance ni la capture ni Tesseract : la lecture
  précédente est réutilisée, la réponse est immédiate ;
* **jamais en continu** — rien ici ne tourne tout seul. Ce module ne fait
  quelque chose que lorsqu'une question a été posée.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
from pathlib import Path
import time
from dataclasses import dataclass

from core import action_kit as kit

# Sous ce volume, ce que Tesseract a trouvé ne fait pas une réponse : c'est un
# écran graphique où traînent trois libellés de boutons. L'image vaut mieux.
MIN_CHARS = 45
MIN_WORDS = 8

# Une trace d'erreur complète tient là-dedans ; au-delà, on encombre le
# contexte du modèle avec le reste de l'écran.
MAX_CHARS = 4000

CACHE_TTL_S = 600.0
CACHE_SIZE = 8

# Une seule langue : « fra » lit aussi l'anglais des interfaces, et combiner
# eng+fra triple le temps de lecture (4,9 s contre 1,8 s sur cette machine).
LANGUAGES = "fra"
FALLBACK_LANGUAGE = "eng"
OCR_TIMEOUT_S = 8
OCR_MIN_SIDE = 900
# Au-delà, Tesseract passe de ~1,5 s à ~5 s sur cette machine sans lire plus
# de mots : un écran 1800 px reste lisible réduit à 1400.
OCR_MAX_SIDE = 1400
DARK_MEAN = 90.0

# Questions qui portent sur l'aspect, pas sur le texte : là, l'OCR ne répondra
# jamais, quelle que soit la quantité de mots trouvés.
_VISUAL_WORDS = (
    "couleur", "couleurs", "ressemble", "dessin", "image", "photo", "logo",
    "graphique", "courbe", "schema", "schéma", "design", "mise en page",
    "interface", "capture", "video", "vidéo", "visage", "personne", "vetement",
    "vêtement", "decris", "décris", "description", "a quoi", "à quoi",
    "architecture", "diagramme", "diagram", "topologie", "caméra", "camera",
)

_cache: dict[str, tuple[float, str]] = {}

_available: bool | None = None

# Données de langue : le paquet Arch système d'abord, sinon les modèles
# téléchargés dans le dossier utilisateur (installables sans droits root).
_USER_TESSDATA = Path.home() / ".local" / "share" / "tessdata"
_SYSTEM_TESSDATA = Path("/usr/share/tessdata")


def _tessdata_dir() -> Path | None:
    """Premier dossier qui contient toutes les langues demandées."""
    wanted = [f"{lang}.traineddata" for lang in LANGUAGES.split("+")]
    for base in (_SYSTEM_TESSDATA, _USER_TESSDATA):
        if all((base / name).exists() for name in wanted):
            return base
    for base in (_SYSTEM_TESSDATA, _USER_TESSDATA):
        if (base / f"{FALLBACK_LANGUAGE}.traineddata").exists():
            return base
    return None


def _ocr_env() -> dict[str, str]:
    base = _tessdata_dir()
    return {"TESSDATA_PREFIX": str(base)} if base else {}


def available() -> bool:
    """Vrai si l'OCR local est utilisable ici."""
    global _available
    if _available is None:
        # Le binaire Arch suffit : le petit wrapper Python est facultatif.
        # Cela garde l'OCR utilisable dans l'environnement Python système
        # protégé par PEP 668.
        _available = (
            shutil.which("tesseract") is not None and _tessdata_dir() is not None
        )
        if not _available:
            print("[Vision] OCR local indisponible (binaire ou données eng/fra "
                  "absents) — les captures partiront en image.")
    return _available


@dataclass
class ScreenRead:
    text: str
    usable: bool
    cached: bool
    seconds: float

    def as_tool_result(self, question: str) -> str:
        source = "déjà lu, écran inchangé" if self.cached else "OCR local"
        return (
            f"[TEXTE DE L'ÉCRAN — {source}, aucune image envoyée]\n"
            f"{self.text}\n\n"
            "Ce texte est la lecture exacte de l'écran : réponds à partir de "
            "lui, sans rien inventer et sans rappeler l'outil. Si la réponse à "
            f"« {question[:120]} » ne s'y trouve pas, dis simplement ce que tu "
            "vois d'utile."
        )


def fingerprint(image_bytes: bytes) -> str:
    """Empreinte de l'écran, insensible au bruit d'un pixel près.

    L'image est réduite à 32×32 en niveaux de gris : deux captures du même
    écran donnent la même empreinte même si l'horloge a changé de seconde,
    alors qu'une fenêtre ouverte ou un texte différent la change.
    """
    from PIL import Image

    with Image.open(io.BytesIO(image_bytes)) as im:
        small = im.convert("L").resize((32, 32))
        return hashlib.blake2b(small.tobytes(), digest_size=16).hexdigest()


def wants_image(question: str) -> bool:
    """Vrai quand la question porte sur l'aspect et non sur le texte."""
    lowered = " ".join((question or "").lower().split())
    return any(word in lowered for word in _VISUAL_WORDS)


def prepare_for_ocr(image):
    """Prépare une capture d'écran pour Tesseract.

    Les terminaux et les HUD sombres inversés deviennent de l'encre noire
    sur fond clair. Une image trop petite est agrandie : Tesseract lit mal
    en dessous de ~12 px de glyphe.
    """
    from PIL import Image, ImageOps

    gray = image.convert("L")
    width, height = gray.size
    side = max(width, height)
    scale = 1.0
    if side and side < OCR_MIN_SIDE:
        scale = OCR_MIN_SIDE / side
    elif side > OCR_MAX_SIDE:
        scale = OCR_MAX_SIDE / side
    if scale != 1.0:
        try:
            resample = Image.Resampling.LANCZOS
        except AttributeError:
            resample = getattr(Image, "LANCZOS", Image.BILINEAR)
        gray = gray.resize((max(1, int(width * scale)), max(1, int(height * scale))), resample)
    probe = gray.resize((32, 32))
    pixels = list(probe.get_flattened_data()) if hasattr(probe, "get_flattened_data") else list(probe.getdata())
    mean = sum(pixels) / max(1, len(pixels))
    if mean < DARK_MEAN:
        gray = ImageOps.invert(gray)
    return ImageOps.autocontrast(gray, cutoff=2)


def _clean(text: str) -> str:
    """Nettoie la sortie de Tesseract sans en altérer le contenu.

    L'OCR d'un écran produit des lignes vides par paquets et des lignes d'un
    seul caractère parasite (bords de fenêtre, icônes). Les retirer réduit de
    moitié ce qui part au modèle, sans perdre une seule ligne réelle.
    """
    lines = []
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if len(line.strip()) < 2 and not line.strip().isalnum():
            continue
        lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _is_readable(text: str) -> bool:
    return len(text) >= MIN_CHARS and len(text.split()) >= MIN_WORDS


def _ocr_with_tesseract(image, *, lang: str, psm: int) -> str:
    """OCR borné, avec pytesseract si présent ou le binaire Arch directement."""
    config = f"--psm {psm} --oem 1"
    env = _ocr_env()
    try:
        import pytesseract
        for key, value in env.items():
            os.environ.setdefault(key, value)
        return str(pytesseract.image_to_string(image, lang=lang, config=config) or "")
    except ImportError:
        buf = io.BytesIO()
        image.save(buf, format="PNG", optimize=False)
        result = kit.run(
            ["tesseract", "stdin", "stdout", "-l", lang, "--psm", str(psm), "--oem", "1"],
            stdin=buf.getvalue(), binary=True, timeout=OCR_TIMEOUT_S,
            env={**os.environ, **env},
        )
        if result.timed_out:
            raise TimeoutError(result.reason())
        if not result.ok:
            raise RuntimeError(result.reason())
        return bytes(result.out).decode("utf-8", errors="replace")


def read(image_bytes: bytes, question: str = "") -> ScreenRead | None:
    """Lit l'écran. Rend None si l'OCR n'a pas lieu d'être ou n'aboutit pas.

    Bloquant : à lancer depuis un exécuteur, jamais sur la boucle audio.
    """
    if not image_bytes or not available() or wants_image(question):
        return None

    started = time.monotonic()
    try:
        key = fingerprint(image_bytes)
    except Exception as exc:
        print(f"[Vision] empreinte impossible : {exc}")
        return None

    now = time.monotonic()
    hit = _cache.get(key)
    if hit and (now - hit[0]) < CACHE_TTL_S:
        # L'écran n'a pas bougé : ni capture relue, ni Tesseract relancé.
        _cache[key] = (now, hit[1])
        text = hit[1]
        print(f"[Vision] 📄 lecture réutilisée ({len(text)} caractères)")
        return ScreenRead(text, _is_readable(text), True,
                          time.monotonic() - started)

    try:
        from PIL import Image

        with Image.open(io.BytesIO(image_bytes)) as im:
            enhanced = prepare_for_ocr(im)
            try:
                text = _ocr_with_tesseract(enhanced, lang=LANGUAGES, psm=6)
            except TimeoutError:
                # La machine est saturée : une seconde langue mettrait aussi
                # longtemps. Le modèle distant prend le relais sans attendre.
                raise
            except Exception:
                text = _ocr_with_tesseract(enhanced, lang=FALLBACK_LANGUAGE, psm=6)
            if not _is_readable(text) and (time.monotonic() - started) < OCR_TIMEOUT_S:
                try:
                    retry = _ocr_with_tesseract(enhanced, lang=LANGUAGES, psm=4)
                    if len(retry.strip()) > len(text.strip()):
                        text = retry
                except Exception:
                    pass
    except Exception as exc:
        print(f"[Vision] OCR impossible : {exc}")
        return None

    text = _clean(text)[:MAX_CHARS]
    _cache[key] = (now, text)
    if len(_cache) > CACHE_SIZE:
        oldest = min(_cache.items(), key=lambda kv: kv[1][0])[0]
        _cache.pop(oldest, None)

    elapsed = time.monotonic() - started
    usable = _is_readable(text)
    print(f"[Vision] 📄 OCR {elapsed:.1f}s — {len(text)} caractères, "
          f"{'suffisant' if usable else 'trop maigre → image'}")
    return ScreenRead(text, usable, False, elapsed)
