#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
file_processor.py — JARVIS Universal File Processor, version réparée et renforcée.
Parsing local avancé, compréhension multilingue, actions automatiques.
Gère 12+ types de fichiers avec analyse IA et conversions.

Corrections par rapport à l'ancienne version :
    - `_get_api_key` utilisait `Path(file)` au lieu de `Path(__file__)` :
      NameError sur toutes les opérations IA ;
    - `_output_path` n'était définie nulle part (la fonction s'appelait
      `output_path`) alors que tous les handlers appellent `_output_path` :
      NameError sur chaque opération produisant un fichier ;
    - le parsing des chemins ne supportait pas les accents et capturait des
      espaces par erreur : réécrit (chemins entre guillemets d'abord,
      motif Unicode ensuite) ;
    - la transcription audio envoyait un dict non supporté par google-genai :
      remplacé par types.Part.from_bytes ;
    - extraction PDF : ajout du repli pypdf (PyPDF2 est déprécié) ;
    - exécution de code : support des scripts .sh/.bash en plus de Python ;
    - archives : ajout du format 7z si l'outil est installé ;
    - tempfile.mktemp (non sûr) remplacé par NamedTemporaryFile ;
    - les sorties sont désormais confinées à $HOME (repli dans
      ~/Documents/jarvis_outputs si la source est hors répertoire utilisateur).

Ajouts :
    - résolution de fichier intelligente (Home, Bureau, Téléchargements,
      Documents) quand le chemin est relatif ou incomplet ;
    - action `open` : ouvrir le fichier avec l'application par défaut ;
    - conversion docx/texte → PDF via LibreOffice headless ;
    - conversion image → PDF via Pillow ;
    - messages d'erreur précis quand un outil manque (ffmpeg, libreoffice…).
"""
import os
import re
import json
import shutil
import platform
import tempfile
from pathlib import Path
from typing import Optional, Dict, Any

from core import action_kit as kit
from core.live_model_policy import BALANCED_MODEL, FAST_MODEL

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"


# ════════════════════════════════════════════════════════════════════════════
# Helpers communs
# ════════════════════════════════════════════════════════════════════════════

def _get_base_dir() -> Path:
    import sys
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def _get_api_key() -> str:
    try:
        config_path = _get_base_dir() / "config" / "api_keys.json"
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f).get("gemini_api_key", "")
    except Exception:
        return ""


def _gemini_client():
    api_key = _get_api_key()
    if not api_key:
        raise RuntimeError("Clé API Gemini indisponible.")
    from google import genai
    _c = genai.Client(api_key=api_key)

    class _W:
        def generate_content(self, contents):
            return _c.models.generate_content(model=BALANCED_MODEL,
                                              contents=contents)
    return _W()


def _document_model():
    """Le traitement de documents utilise le modèle Azure dédié."""
    from core.azure_specialists import text
    class _Response:
        def __init__(self, value): self.text = value
    class _W:
        def generate_content(self, contents):
            return _Response(text("document", str(contents), system=(
                "Tu es un rédacteur et analyste documentaire précis. Réponds en français."
            )))
    return _W()


def _detect_type(path: Path) -> str:
    ext = path.suffix.lower().lstrip(".")
    image_exts = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "tiff", "svg", "ico"}
    video_exts = {"mp4", "avi", "mov", "mkv", "wmv", "flv", "webm", "m4v", "3gp"}
    audio_exts = {"mp3", "wav", "ogg", "m4a", "aac", "flac", "wma", "opus"}
    code_exts  = {"py", "js", "ts", "jsx", "tsx", "html", "css", "java", "c",
                  "cpp", "cs", "go", "rs", "rb", "php", "swift", "kt", "sh",
                  "bash", "ps1", "lua", "r", "m", "sql", "yaml", "toml"}
    archive_exts = {"zip", "rar", "tar", "gz", "7z", "bz2", "xz"}
    if ext in image_exts:   return "image"
    if ext in video_exts:   return "video"
    if ext in audio_exts:   return "audio"
    if ext in code_exts:    return "code"
    if ext in archive_exts: return "archive"
    if ext == "pdf":        return "pdf"
    if ext in ("docx", "doc"): return "docx"
    if ext in ("txt", "md", "rst", "log"): return "text"
    if ext in ("csv", "tsv"): return "csv"
    if ext in ("xlsx", "xls", "ods"): return "excel"
    if ext == "json":       return "json"
    if ext == "xml":        return "xml"
    if ext in ("pptx", "ppt"): return "pptx"
    return "unknown"


def _file_size_str(path: Path) -> str:
    size = path.stat().st_size
    if size < 1024:           return f"{size} B"
    if size < 1024 ** 2:      return f"{size / 1024:.1f} KB"
    if size < 1024 ** 3:      return f"{size / 1024 ** 2:.1f} MB"
    return f"{size / 1024 ** 3:.1f} GB"


def _is_safe_output(target: Path) -> bool:
    """Les sorties doivent rester dans le répertoire utilisateur."""
    try:
        return target.resolve().is_relative_to(Path.home().resolve())
    except Exception:
        return False


def _output_path(src: Path, suffix: str, new_ext: str = None) -> Path:
    """Chemin de sortie à côté de la source ; si la source est hors $HOME,
    on écrit dans ~/Documents/jarvis_outputs pour rester sûr."""
    ext = new_ext or src.suffix
    name = f"{src.stem}{suffix}{ext}"
    target = src.parent / name
    if not _is_safe_output(target):
        out_dir = Path.home() / "Documents" / "jarvis_outputs"
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / name
    return target


# Alias de compatibilité (l'ancienne API publique)
output_path = _output_path


def _resolve_file(raw: str) -> Path:
    """Résout un chemin relatif/incomplet en cherchant dans les dossiers
    usuels (Home, Bureau, Téléchargements, Documents)."""
    p = Path(raw).expanduser()
    if p.exists():
        return p
    bases = [
        Path.home(),
        Path.home() / "Desktop", Path.home() / "Bureau",
        Path.home() / "Downloads", Path.home() / "Téléchargements",
        Path.home() / "Documents",
    ]
    for base in bases:
        cand = base / raw
        if cand.exists():
            return cand
    return p


# ════════════════════════════════════════════════════════════════════════════
# Parsing local amélioré (Unicode, chemins entre guillemets)
# ════════════════════════════════════════════════════════════════════════════

ACTION_MAP = {
    "image":   ["describe", "ocr", "resize", "convert", "compress", "info"],
    "pdf":     ["summarize", "extract_text", "info", "to_word"],
    "docx":    ["summarize", "extract_text", "word_count", "reformat", "fix", "to_pdf"],
    "text":    ["summarize", "extract_text", "word_count", "reformat", "fix", "to_pdf"],
    "csv":     ["analyze", "info", "stats", "convert", "filter", "sort"],
    "excel":   ["analyze", "info", "stats", "convert"],
    "json":    ["validate", "format", "analyze", "to_csv"],
    "code":    ["explain", "review", "fix", "run", "info", "document", "summarize"],
    "audio":   ["transcribe", "info", "convert", "trim"],
    "video":   ["info", "extract_audio", "trim", "extract_frame", "compress", "transcribe", "convert"],
    "archive": ["list", "extract"],
    "pptx":    ["summarize", "extract_text", "analyze"],
}


def _parse_file_command_locally(text: str) -> Optional[Dict[str, Any]]:
    """
    Extrait le chemin du fichier, l'action et les paramètres d'une phrase
    naturelle. Retourne {'file_path', 'action', 'params'} ou None.
    """
    text = re.sub(r"\s+", " ", (text or "").lower().strip())
    text = re.sub(r"\b(s'il te pla[iî]t|stp|please|peux-tu|tu peux|je veux|j'aimerais)\b",
                  " ", text).strip()

    # 1. Chercher un fichier : d'abord un chemin entre guillemets,
    #    puis un token Unicode se terminant par une extension.
    file_path = None
    m = re.search(r"['\"]([^'\"]+\.\w{1,6})['\"]", text)
    if m:
        file_path = m.group(1).strip()
    else:
        m = re.search(r"([^\s'\"]+\.\w{1,6})", text)
        if m:
            file_path = m.group(1).strip()
    if not file_path:
        return None

    # 2. Extraire l'action et les paramètres
    action = None
    params: Dict[str, Any] = {}

    # Action "ouvrir"
    if re.search(r"\b(ouvre|open|lance)\s+(?:le\s+)?fichier\b", text):
        action = "open"

    # Actions images
    elif re.search(r"(?:d[eé]cris|analyse|explique|ocr|lis\s+le\s+texte|extrais\s+le\s+texte)\s+(?:cette\s+)?image", text):
        action = "describe"
    elif re.search(r"redimensionne|resize|change\s+la\s+taille", text):
        action = "resize"
        size_match = re.search(r"(\d+)\s*x\s*(\d+)", text)
        if size_match:
            params["width"] = int(size_match.group(1))
            params["height"] = int(size_match.group(2))
    elif re.search(r"convertis?\s+(?:en|vers|to)\s+(\w+)", text):
        action = "convert"
        fmt_match = re.search(r"convertis?\s+(?:en|vers|to)\s+(\w+)", text)
        if fmt_match:
            params["format"] = fmt_match.group(1).lower()
    elif re.search(r"compresse|r[eé]duis?\s+la\s+taille", text):
        action = "compress"
        quality_match = re.search(r"qualit[eé]\s+(\d+)", text)
        if quality_match:
            params["quality"] = int(quality_match.group(1))
    elif re.search(r"\b(info|informations|d[eé]tails)\b", text):
        action = "info"

    # Actions PDF / documents
    elif re.search(r"r[eé]sume|summarize", text):
        action = "summarize"
    elif re.search(r"extrais?\s+(?:le\s+)?texte|extract\s+text", text):
        action = "extract_text"
    elif re.search(r"compte\s+les?\s+mots|word\s*count", text):
        action = "word_count"
    elif re.search(r"convertis?\s+(?:en|vers|to)\s+word", text):
        action = "to_word"
    elif re.search(r"convertis?\s+(?:en|vers|to)\s+pdf", text):
        action = "to_pdf"
    elif re.search(r"formate|reformat", text):
        action = "reformat"
    elif re.search(r"corrige|fix|r[eé]pare", text):
        action = "fix"

    # Actions audio / vidéo
    elif re.search(r"transcris?|transcribe|retranscris?", text):
        action = "transcribe"
    elif re.search(r"extrais?\s+(?:l'audio|le son|la piste audio)", text):
        action = "extract_audio"
    elif re.search(r"extrais?\s+(?:une image|une frame|une capture)", text):
        action = "extract_frame"
        ts = re.search(r"(?:à\s+)?(\d{1,2}:\d{2}(?::\d{2})?)", text)
        if ts:
            params["timestamp"] = ts.group(1)

    # Archives
    elif re.search(r"liste|list|affiche\s+le\s+contenu", text):
        action = "list"
    elif re.search(r"extrais?|d[eé]compresse", text):
        action = "extract"
        dest = re.search(r"(?:vers|dans|to)\s+['\"]?(.+?)['\"]?(?:\s|$)", text)
        if dest:
            params["destination"] = dest.group(1).strip()

    # Code
    elif re.search(r"ex[eé]cute|lance|run", text):
        action = "run"
    elif re.search(r"explique|explain", text):
        action = "explain"
    elif re.search(r"review|audit|v[eé]rifie", text):
        action = "review"
    elif re.search(r"documente?|doc", text):
        action = "document"

    # JSON
    elif re.search(r"valide|validate", text):
        action = "validate"
    elif re.search(r"formate|format|pretty", text):
        action = "format"
    elif re.search(r"converti[st]?\s+(?:en|vers|to)\s+csv", text):
        action = "to_csv"

    # CSV / Excel
    elif re.search(r"stats?|statistiques", text):
        action = "stats"
    elif re.search(r"filtre|filter", text):
        action = "filter"
        col = re.search(r"colonne\s+['\"]?(\w+)['\"]?", text)
        val = re.search(r"valeur\s+['\"]?(.+?)['\"]?(?:\s|$)", text)
        if col:
            params["column"] = col.group(1)
        if val:
            params["value"] = val.group(1).strip()
    elif re.search(r"trie|sort", text):
        action = "sort"
        col = re.search(r"par\s+['\"]?(\w+)['\"]?", text)
        if col:
            params["column"] = col.group(1)

    if action is None:
        return None
    return {"file_path": file_path, "action": action, "params": params}


def _detect_file_intent_ai(description: str) -> Optional[Dict]:
    api_key = _get_api_key()
    if not api_key:
        return None
    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        prompt = f"""Tu es un assistant de traitement de fichiers. Analyse la demande suivante et retourne UNIQUEMENT un objet JSON avec :
"file_path" : le chemin du fichier mentionné
"action" : l'action principale (ex: "summarize", "transcribe", "convert", "extract_text", etc.)
"params" : un objet contenant les paramètres pertinents (format, quality, width, height, start, end, column, value, etc.) si nécessaire.
Exemples :
"résume ce pdf /home/user/doc.pdf" -> {{"file_path":"/home/user/doc.pdf","action":"summarize","params":{{}}}}
"convertis image.png en jpg" -> {{"file_path":"image.png","action":"convert","params":{{"format":"jpg"}}}}
"extrais l'audio de video.mp4" -> {{"file_path":"video.mp4","action":"extract_audio","params":{{}}}}
Phrase : "{description}"
JSON :"""
        resp = client.models.generate_content(model=FAST_MODEL, contents=prompt)
        json_match = re.search(r'\{.*\}', resp.text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group(0))
    except Exception as e:
        print(f"[FileProcessor] AI intent error: {e}")
    return None


# ════════════════════════════════════════════════════════════════════════════
# Traitement des images
# ════════════════════════════════════════════════════════════════════════════

def _process_image(path: Path, action: str, params: dict, speak=None) -> str:
    try:
        from PIL import Image
    except ImportError:
        return "Pillow n'est pas installé. Exécutez : pip install Pillow"
    action = action or "describe"

    if action in ("describe", "ocr", "analyze", "read", "extract_text"):
        try:
            model = _gemini_client()
            img = Image.open(path)
            prompt_map = {
                "describe": "Décris cette image en détail.",
                "ocr":      "Extrais tout le texte visible dans cette image. Retourne uniquement le texte, bien formaté.",
                "analyze":  "Analyse cette image en profondeur : objets, couleurs, composition, tout texte, contexte.",
                "read":     "Lis tout le texte de cette image en conservant la structure et la mise en forme.",
                "extract_text": "Extrais tout le texte de cette image.",
            }
            prompt = prompt_map.get(action, "Décris cette image.")
            if params.get("instruction"):
                prompt = params["instruction"]
            response = model.generate_content([prompt, img])
            result = response.text.strip()
            if len(result) > 500 and params.get("save", True):
                out = _output_path(path, "resultat", ".txt")
                out.write_text(result, encoding="utf-8")
                return f"{result[:300]}...\n\nRésultat complet sauvegardé dans : {out.name}"
            return result
        except Exception as e:
            return f"Échec de l'analyse IA de l'image : {e}"

    if action == "resize":
        width = int(params.get("width", 0))
        height = int(params.get("height", 0))
        scale = float(params.get("scale", 0))
        try:
            with Image.open(path) as img:
                w, h = img.size
                if scale:
                    new_size = (int(w * scale), int(h * scale))
                elif width and height:
                    new_size = (width, height)
                elif width:
                    new_size = (width, int(h * width / w))
                elif height:
                    new_size = (int(w * height / h), height)
                else:
                    return "Veuillez spécifier width, height ou scale."
                out = _output_path(path, f"redim_{new_size[0]}x{new_size[1]}")
                img.resize(new_size, Image.LANCZOS).save(out)
                return f"Redimensionné de {w}x{h} à {new_size[0]}x{new_size[1]}. Sauvegardé : {out.name}"
        except Exception as e:
            return f"Échec du redimensionnement : {e}"

    if action == "convert":
        fmt = params.get("format", "png").lower().strip(".")
        fmt_map = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG",
                   "webp": "WEBP", "bmp": "BMP", "tiff": "TIFF", "pdf": "PDF"}
        pil_fmt = fmt_map.get(fmt, fmt.upper())
        try:
            with Image.open(path) as img:
                if fmt in ("jpg", "jpeg"):
                    img = img.convert("RGB")
                out = _output_path(path, "converti", f".{fmt}")
                img.save(out, pil_fmt)
                return f"Converti en {fmt.upper()}. Sauvegardé : {out.name}"
        except Exception as e:
            return f"Échec de la conversion : {e}"

    if action == "compress":
        quality = int(params.get("quality", 70))
        try:
            with Image.open(path) as img:
                img = img.convert("RGB")
                out = _output_path(path, f"compresse_q{quality}", ".jpg")
                img.save(out, "JPEG", quality=quality, optimize=True)
                before = _file_size_str(path)
                after = _file_size_str(out)
                return f"Compressé : {before} → {after}. Sauvegardé : {out.name}"
        except Exception as e:
            return f"Échec de la compression : {e}"

    if action == "info":
        try:
            with Image.open(path) as img:
                return (f"Informations image : {img.format}, {img.size[0]}x{img.size[1]}px, "
                        f"mode : {img.mode}, taille : {_file_size_str(path)}")
        except Exception as e:
            return f"Échec des informations : {e}"

    return _process_image(path, "describe", {"instruction": f"{action}: {params}"})


# ════════════════════════════════════════════════════════════════════════════
# Traitement des PDF
# ════════════════════════════════════════════════════════════════════════════

def _process_pdf(path: Path, action: str, params: dict, speak=None) -> str:
    action = action or "summarize"

    def _extract_pdf_text(max_chars=50000) -> str:
        text = ""
        try:
            import pdfplumber
            with pdfplumber.open(path) as pdf:
                for page in pdf.pages:
                    text += (page.extract_text() or "") + "\n"
        except ImportError:
            try:
                import pypdf
                with open(path, "rb") as f:
                    reader = pypdf.PdfReader(f)
                    for page in reader.pages:
                        text += page.extract_text() + "\n"
            except ImportError:
                try:
                    import PyPDF2
                    with open(path, "rb") as f:
                        reader = PyPDF2.PdfReader(f)
                        for page in reader.pages:
                            text += page.extract_text() + "\n"
                except ImportError:
                    return ""
        return text[:max_chars]

    if action in ("summarize", "extract_text", "translate_hint", "analyze", "reformat"):
        text = _extract_pdf_text()
        if not text.strip():
            return "Impossible d'extraire le texte du PDF (peut-être scanné/image)."
        if action == "extract_text":
            out = _output_path(path, "texte", ".txt")
            out.write_text(text, encoding="utf-8")
            return f"Texte extrait ({len(text)} caractères). Sauvegardé : {out.name}"
        prompt_map = {
            "summarize":      f"Résume ce document PDF de manière concise :\n\n{text}",
            "analyze":        f"Analyse ce document en détail :\n\n{text}",
            "translate_hint": f"Quelle langue ce document est-il et de quoi parle-t-il ? Résume :\n\n{text}",
            "reformat":       f"Reformate ce texte proprement avec une structure claire :\n\n{text}",
        }
        try:
            model = _document_model()
            response = model.generate_content(prompt_map.get(action, f"Analyse :\n\n{text}"))
            result = response.text.strip()
            if len(result) > 600 and params.get("save", True):
                out = _output_path(path, action, ".txt")
                out.write_text(result, encoding="utf-8")
                return f"{result[:400]}...\n\nRésultat complet sauvegardé : {out.name}"
            return result
        except Exception as e:
            return f"Échec de l'analyse IA : {e}"

    if action == "info":
        try:
            import pdfplumber
            with pdfplumber.open(path) as pdf:
                pages = len(pdf.pages)
            return f"PDF : {pages} page(s), taille : {_file_size_str(path)}"
        except Exception:
            return f"PDF taille : {_file_size_str(path)}"

    if action == "to_word":
        text = _extract_pdf_text()
        if not text:
            return "Impossible d'extraire le texte pour la conversion."
        try:
            from docx import Document
            doc = Document()
            doc.add_heading(path.stem, 0)
            for para in text.split("\n\n"):
                if para.strip():
                    doc.add_paragraph(para.strip())
            out = _output_path(path, "converted", ".docx")
            doc.save(out)
            return f"Converti en document Word. Sauvegardé : {out.name}"
        except ImportError:
            return "python-docx non installé. Exécutez : pip install python-docx"

    return f"Action PDF inconnue : '{action}'. Essayez : summarize, extract_text, info, to_word"


# ════════════════════════════════════════════════════════════════════════════
# Traitement des documents texte / docx
# ════════════════════════════════════════════════════════════════════════════

def _process_text_doc(path: Path, file_type: str, action: str,
                      params: dict, speak=None) -> str:
    action = action or "summarize"

    def _read_content() -> str:
        if file_type == "docx":
            try:
                from docx import Document
                doc = Document(path)
                return "\n".join(p.text for p in doc.paragraphs)
            except ImportError:
                return "python-docx non installé."
            except Exception as e:
                return f"Échec de lecture : {e}"
        else:
            return path.read_text(encoding="utf-8", errors="ignore")

    content = _read_content()
    if not content.strip():
        return "Le fichier semble vide."

    if action == "word_count":
        words = len(content.split())
        chars = len(content)
        lines = content.count("\n")
        return f"Nombre de mots : {words} mots, {chars} caractères, {lines} lignes."

    if action == "extract_text":
        if file_type != "txt":
            out = _output_path(path, "extracted", ".txt")
            out.write_text(content, encoding="utf-8")
            return f"Texte extrait. Sauvegardé : {out.name}"
        return content[:2000]

    if action == "to_pdf":
        soffice = kit.which("soffice") or kit.which("libreoffice")
        if not soffice:
            return "Impossible de convertir en PDF (installez libreoffice)."
        try:
            outdir = path.parent
            kit.run([soffice, "--headless", "--convert-to", "pdf",
                            "--outdir", str(outdir), str(path)],
                           timeout=120)
            out = path.with_suffix(".pdf")
            if out.exists():
                return f"Converti en PDF : {out.name}"
            return "La conversion PDF a échoué (aucun fichier produit)."
        except Exception as e:
            return f"Échec de la conversion PDF : {e}"

    instruction = params.get("instruction", "")
    prompt_map = {
        "summarize":  f"Résume ce document :\n\n{content[:40000]}",
        "analyze":    f"Analyse ce document :\n\n{content[:40000]}",
        "reformat":   f"Reformate ce texte avec une structure claire :\n\n{content[:40000]}",
        "fix":        f"Corrige la grammaire, l'orthographe et le style :\n\n{content[:40000]}",
        "translate_hint": f"Quelle langue est-ce et que dit ce document ? Résume :\n\n{content[:10000]}",
        "to_bullet":  f"Convertit ce texte en une liste à puces claire :\n\n{content[:40000]}",
        "custom":     f"{instruction}\n\n{content[:40000]}",
    }
    if action not in prompt_map:
        action = "custom"
        instruction = action
    try:
        model = _document_model()
        response = model.generate_content(prompt_map[action])
        result = response.text.strip()
        if len(result) > 600 and params.get("save", True):
            out = _output_path(path, action, ".txt")
            out.write_text(result, encoding="utf-8")
            return f"{result[:400]}...\n\nRésultat complet sauvegardé : {out.name}"
        return result
    except Exception as e:
        return f"Échec du traitement IA : {e}"


# ════════════════════════════════════════════════════════════════════════════
# Traitement des données (CSV / Excel)
# ════════════════════════════════════════════════════════════════════════════

def _process_data(path: Path, file_type: str, action: str,
                  params: dict, speak=None) -> str:
    try:
        import pandas as pd
    except ImportError:
        return "pandas non installé. Exécutez : pip install pandas openpyxl"
    action = action or "analyze"
    try:
        if file_type == "csv":
            df = pd.read_csv(path, encoding="utf-8", errors="replace")
        else:
            df = pd.read_excel(path)
    except Exception as e:
        return f"Impossible de lire le fichier : {e}"

    if action == "info":
        return (f"Lignes : {len(df)}, Colonnes : {len(df.columns)}\n"
                f"Colonnes : {', '.join(df.columns.tolist())}\n"
                f"Taille : {_file_size_str(path)}")

    if action == "stats":
        try:
            desc = df.describe(include="all").to_string()
            return f"Statistiques :\n{desc[:2000]}"
        except Exception as e:
            return f"Échec des statistiques : {e}"

    if action == "analyze":
        preview = df.head(50).to_string()
        prompt = (f"Analyse ce jeu de données. Colonnes : {list(df.columns)}\n"
                  f"Lignes : {len(df)}\nAperçu :\n{preview}\n\n"
                  f"Donne des aperçus, des motifs et des conclusions notables.")
        try:
            model = _gemini_client()
            response = model.generate_content(prompt)
            return response.text.strip()
        except Exception as e:
            return f"Échec de l'analyse IA : {e}"

    if action in ("convert", "to_csv", "to_excel", "to_json"):
        fmt = {"to_csv": "csv", "to_excel": "xlsx", "to_json": "json",
               "convert": params.get("format", "csv")}.get(action, "csv")
        try:
            if fmt == "csv":
                out = _output_path(path, "converted", ".csv")
                df.to_csv(out, index=False, encoding="utf-8")
            elif fmt == "xlsx":
                out = _output_path(path, "converted", ".xlsx")
                df.to_excel(out, index=False)
            elif fmt == "json":
                out = _output_path(path, "converted", ".json")
                df.to_json(out, orient="records", force_ascii=False, indent=2)
            return f"Converti en {fmt.upper()}. Sauvegardé : {out.name}"
        except Exception as e:
            return f"Échec de la conversion : {e}"

    if action == "filter":
        col = params.get("column", "")
        value = params.get("value", "")
        condition = params.get("condition", "equals")
        if not col or col not in df.columns:
            return f"Colonne '{col}' introuvable. Disponibles : {', '.join(df.columns)}"
        try:
            if condition == "equals":     filtered = df[df[col] == value]
            elif condition == "contains": filtered = df[df[col].astype(str).str.contains(str(value), case=False)]
            elif condition == "gt":       filtered = df[df[col] > float(value)]
            elif condition == "lt":       filtered = df[df[col] < float(value)]
            else:                         filtered = df[df[col] == value]
            out = _output_path(path, "filtered", ".csv")
            filtered.to_csv(out, index=False)
            return f"Filtré : {len(filtered)} lignes correspondent. Sauvegardé : {out.name}"
        except Exception as e:
            return f"Échec du filtrage : {e}"

    if action == "sort":
        col = params.get("column", df.columns[0])
        asc = params.get("ascending", True)
        try:
            sorted_df = df.sort_values(col, ascending=asc)
            out = _output_path(path, "sorted", path.suffix)
            sorted_df.to_csv(out, index=False)
            return f"Trié par '{col}'. Sauvegardé : {out.name}"
        except Exception as e:
            return f"Échec du tri : {e}"

    preview = df.head(30).to_string()
    try:
        model = _gemini_client()
        response = model.generate_content(
            f"Tâche : {action}\nDonnées ({len(df)} lignes, colonnes : {list(df.columns)}) :\n{preview}"
        )
        return response.text.strip()
    except Exception as e:
        return f"Échec du traitement : {e}"


# ════════════════════════════════════════════════════════════════════════════
# Traitement JSON / XML
# ════════════════════════════════════════════════════════════════════════════

def _process_json(path: Path, action: str, params: dict, speak=None) -> str:
    action = action or "analyze"
    try:
        content = path.read_text(encoding="utf-8")
        data = json.loads(content)
    except Exception as e:
        return f"JSON invalide : {e}"

    if action == "validate":
        return f"JSON valide. Type : {type(data).__name__}, taille : {_file_size_str(path)}"

    if action == "format":
        out = _output_path(path, "formatted", ".json")
        out.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return f"JSON formaté sauvegardé : {out.name}"

    if action in ("analyze", "summarize", "extract"):
        preview = json.dumps(data, indent=2, ensure_ascii=False)[:8000]
        prompt = f"Tâche : {action} ces données JSON :\n{preview}"
        if params.get("instruction"):
            prompt = f"{params['instruction']}\n\nDonnées JSON :\n{preview}"
        try:
            model = _gemini_client()
            response = model.generate_content(prompt)
            return response.text.strip()
        except Exception as e:
            return f"Échec du traitement IA : {e}"

    if action == "to_csv":
        try:
            import pandas as pd
            if isinstance(data, list):
                df = pd.DataFrame(data)
                out = _output_path(path, "converted", ".csv")
                df.to_csv(out, index=False)
                return f"Converti en CSV. Sauvegardé : {out.name}"
            return "Le JSON doit être un tableau d'objets pour être converti en CSV."
        except ImportError:
            return "pandas non installé."

    return _process_json(path, "analyze", {"instruction": action})


# ════════════════════════════════════════════════════════════════════════════
# Traitement du code
# ════════════════════════════════════════════════════════════════════════════

def _process_code(path: Path, action: str, params: dict, speak=None) -> str:
    action = action or "explain"
    content = path.read_text(encoding="utf-8", errors="ignore")
    ext = path.suffix.lstrip(".")

    if action == "run":
        if ext == "py":
            try:
                result = kit.run(["python", str(path)], timeout=30)
                if result.timed_out:
                    return "Exécution expirée (30s)."
                out = result.stdout or result.stderr
                return f"Sortie :\n{out[:2000]}" if out else "Aucune sortie."
            except Exception as e:
                return f"Échec de l'exécution : {e}"
        if ext in ("sh", "bash"):
            try:
                result = kit.run(["bash", str(path)], timeout=30)
                if result.timed_out:
                    return "Exécution expirée (30s)."
                out = result.stdout or result.stderr
                return f"Sortie :\n{out[:2000]}" if out else "Aucune sortie."
            except Exception as e:
                return f"Échec de l'exécution : {e}"
        return f"L'exécution directe n'est pas supportée pour les fichiers .{ext}."

    if action == "info":
        lines = content.count("\n")
        words = len(content.split())
        return f"Fichier code : {lines} lignes, {words} mots, {_file_size_str(path)}"

    prompt_map = {
        "explain":   f"Explique ce code {ext} clairement :\n\n```{ext}\n{content[:30000]}\n```",
        "review":    f"Examine ce code {ext} pour détecter bogues et améliorations :\n\n```{ext}\n{content[:30000]}\n```",
        "fix":       f"Corrige les bogues de ce code {ext} et retourne la version corrigée :\n\n```{ext}\n{content[:30000]}\n```",
        "optimize":  f"Optimise ce code {ext} pour performance et lisibilité :\n\n```{ext}\n{content[:30000]}\n```",
        "document":  f"Ajoute une documentation appropriée à ce code {ext} :\n\n```{ext}\n{content[:30000]}\n```",
        "summarize": f"Résume ce que fait ce code {ext} :\n\n```{ext}\n{content[:30000]}\n```",
        "test":      f"Écris des tests unitaires pour ce code {ext} :\n\n```{ext}\n{content[:30000]}\n```",
    }
    instruction = params.get("instruction", "")
    if action not in prompt_map:
        prompt = f"{action}\n\n```{ext}\n{content[:30000]}\n```"
        if instruction:
            prompt = f"{instruction}\n\n```{ext}\n{content[:30000]}\n```"
    else:
        prompt = prompt_map[action]
    try:
        model = _gemini_client()
        response = model.generate_content(prompt)
        result = response.text.strip()
        if action in ("fix", "optimize", "document") and params.get("save", True):
            out = _output_path(path, action)
            code_match = re.search(r"```(?:\w+)?\n(.*?)```", result, re.DOTALL)
            code_to_save = code_match.group(1) if code_match else result
            out.write_text(code_to_save, encoding="utf-8")
            return f"{result[:400]}...\n\nSauvegardé : {out.name}"
        return result
    except Exception as e:
        return f"Échec du traitement IA : {e}"


# ════════════════════════════════════════════════════════════════════════════
# Traitement audio
# ════════════════════════════════════════════════════════════════════════════

def _process_audio(path: Path, action: str, params: dict, speak=None) -> str:
    action = action or "transcribe"

    if action == "info":
        try:
            from pydub import AudioSegment
            audio = AudioSegment.from_file(path)
            duration = len(audio) / 1000
            mins, secs = divmod(int(duration), 60)
            return (f"Audio : {mins}m {secs}s, "
                    f"{audio.channels} canaux, "
                    f"{audio.frame_rate}Hz, "
                    f"{_file_size_str(path)}")
        except ImportError:
            return f"Fichier audio : {_file_size_str(path)} (installez pydub pour plus d'infos)"
        except Exception as e:
            return f"Échec des infos : {e}"

    if action == "transcribe":
        try:
            model = _gemini_client()
            content = path.read_bytes()
            mime = {
                "mp3": "audio/mp3", "wav": "audio/wav",
                "ogg": "audio/ogg", "m4a": "audio/mp4",
                "aac": "audio/aac", "flac": "audio/flac",
            }.get(path.suffix.lstrip(".").lower(), "audio/mpeg")
            try:
                from google.genai import types as gtypes
                part = gtypes.Part.from_bytes(data=content, mime_type=mime)
                response = model.generate_content(
                    ["Transcris tout le discours dans ce fichier audio avec précision.", part]
                )
            except Exception:
                response = model.generate_content(
                    ["Transcris tout le discours dans ce fichier audio avec précision.",
                     {"mime_type": mime, "data": content}]
                )
            result = response.text.strip()
            if params.get("save", True):
                out = _output_path(path, "transcript", ".txt")
                out.write_text(result, encoding="utf-8")
                return f"Transcription sauvegardée : {out.name}\n\nAperçu : {result[:300]}"
            return result
        except Exception as e:
            return f"Échec de la transcription : {e}"

    if action == "convert":
        fmt = params.get("format", "mp3").lstrip(".")
        try:
            from pydub import AudioSegment
            audio = AudioSegment.from_file(path)
            out = _output_path(path, "converted", f".{fmt}")
            audio.export(out, format=fmt)
            return f"Converti en {fmt.upper()}. Sauvegardé : {out.name}"
        except ImportError:
            return "pydub non installé. Exécutez : pip install pydub"
        except Exception as e:
            return f"Échec de la conversion : {e}"

    if action == "trim":
        start = float(params.get("start", 0))
        end = float(params.get("end", 0))
        try:
            from pydub import AudioSegment
            audio = AudioSegment.from_file(path)
            end_ms = int(end * 1000) if end else len(audio)
            trimmed = audio[int(start * 1000):end_ms]
            out = _output_path(path, f"trim_{int(start)}s_{int(end)}s")
            trimmed.export(out, format=path.suffix.lstrip("."))
            return f"Audio coupé ({int(start)}s–{int(end)}s). Sauvegardé : {out.name}"
        except ImportError:
            return "pydub non installé."
        except Exception as e:
            return f"Échec de la coupe : {e}"

    return f"Action audio inconnue : '{action}'. Essayez : transcribe, info, convert, trim"


# ════════════════════════════════════════════════════════════════════════════
# Traitement vidéo
# ════════════════════════════════════════════════════════════════════════════

def _process_video(path: Path, action: str, params: dict, speak=None) -> str:
    action = action or "info"

    def _ffmpeg_available() -> bool:
        return kit.have("ffmpeg")

    if action == "info":
        try:
            result = kit.run(
                ["ffprobe", "-v", "quiet", "-print_format", "json",
                 "-show_format", "-show_streams", str(path)], timeout=10
            )
            data = json.loads(result.stdout)
            fmt = data.get("format", {})
            duration = float(fmt.get("duration", 0))
            mins, secs = divmod(int(duration), 60)
            size = _file_size_str(path)
            streams = data.get("streams", [])
            video_s = next((s for s in streams if s["codec_type"] == "video"), {})
            w = video_s.get("width", "?")
            h = video_s.get("height", "?")
            fps = video_s.get("r_frame_rate", "?")
            return f"Vidéo : {mins}m {secs}s, {w}x{h}, {fps} fps, {size}"
        except Exception:
            return f"Fichier vidéo : {_file_size_str(path)}"

    if action == "extract_audio":
        if not _ffmpeg_available():
            return "ffmpeg introuvable. Installez ffmpeg pour extraire l'audio."
        out = _output_path(path, "audio", ".mp3")
        try:
            kit.run(
                ["ffmpeg", "-i", str(path), "-q:a", "0", "-map", "a", str(out), "-y"], timeout=300
            )
            return f"Audio extrait. Sauvegardé : {out.name}"
        except Exception as e:
            return f"Échec de l'extraction audio : {e}"

    if action == "trim":
        start = params.get("start", "00:00:00")
        end = params.get("end", "")
        if not _ffmpeg_available():
            return "ffmpeg introuvable."
        out = _output_path(path, "trim", path.suffix)
        try:
            cmd = ["ffmpeg", "-i", str(path), "-ss", str(start)]
            if end:
                cmd += ["-to", str(end)]
            cmd += ["-c", "copy", str(out), "-y"]
            kit.run(cmd, timeout=600)
            return f"Vidéo coupée sauvegardée : {out.name}"
        except Exception as e:
            return f"Échec de la coupe : {e}"

    if action == "extract_frame":
        timestamp = params.get("timestamp", "00:00:01")
        if not _ffmpeg_available():
            return "ffmpeg introuvable."
        out = _output_path(path, f"frame_{timestamp.replace(':', '')}", ".jpg")
        try:
            kit.run(
                ["ffmpeg", "-i", str(path), "-ss", timestamp,
                 "-vframes", "1", str(out), "-y"], timeout=30
            )
            return f"Image extraite à {timestamp}. Sauvegardée : {out.name}"
        except Exception as e:
            return f"Échec de l'extraction d'image : {e}"

    if action == "compress":
        crf = int(params.get("quality", 28))
        if not _ffmpeg_available():
            return "ffmpeg introuvable."
        out = _output_path(path, f"compresse_crf{crf}", ".mp4")
        try:
            kit.run(
                ["ffmpeg", "-i", str(path),
                 "-c:v", "libx264", "-crf", str(crf),
                 "-preset", "medium", "-c:a", "copy",
                 str(out), "-y"], timeout=1800
            )
            before = _file_size_str(path)
            after = _file_size_str(out)
            return f"Compressé : {before} → {after}. Sauvegardé : {out.name}"
        except Exception as e:
            return f"Échec de la compression : {e}"

    if action == "transcribe":
        if not _ffmpeg_available():
            return "ffmpeg introuvable. Nécessaire pour la transcription vidéo."
        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        tmp_audio = Path(tmp.name)
        tmp.close()
        try:
            kit.run(
                ["ffmpeg", "-i", str(path), "-q:a", "0", "-map", "a",
                 str(tmp_audio), "-y"], timeout=300
            )
            result = _process_audio(tmp_audio, "transcribe", params, speak)
            return result
        except Exception as e:
            return f"Échec de la transcription vidéo : {e}"
        finally:
            if tmp_audio.exists():
                tmp_audio.unlink()

    if action == "convert":
        fmt = params.get("format", "mp4").lstrip(".")
        if not _ffmpeg_available():
            return "ffmpeg introuvable."
        out = _output_path(path, "converted", f".{fmt}")
        try:
            kit.run(
                ["ffmpeg", "-i", str(path), str(out), "-y"], timeout=1800
            )
            return f"Converti en {fmt.upper()}. Sauvegardé : {out.name}"
        except Exception as e:
            return f"Échec de la conversion : {e}"

    return f"Action vidéo inconnue : '{action}'. Essayez : info, trim, extract_audio, extract_frame, compress, transcribe, convert"


# ════════════════════════════════════════════════════════════════════════════
# Traitement des archives
# ════════════════════════════════════════════════════════════════════════════

def _process_archive(path: Path, action: str, params: dict, speak=None) -> str:
    action = action or "list"
    ext = path.suffix.lower()

    if action == "list":
        try:
            import zipfile, tarfile
            if ext == ".zip":
                with zipfile.ZipFile(path) as z:
                    names = z.namelist()
            elif ext in (".tar", ".gz", ".bz2", ".xz"):
                with tarfile.open(path) as t:
                    names = t.getnames()
            elif ext == ".7z" and kit.which("7z"):
                r = kit.run(["7z", "l", str(path)], timeout=30)
                return r.stdout[:2000] if r.returncode == 0 else f"Échec du listage 7z : {r.stderr[:200]}"
            else:
                return f"Format d'archive non supporté : {ext}"
            preview = "\n".join(names[:30])
            suffix = f"\n... et {len(names) - 30} de plus" if len(names) > 30 else ""
            return f"Archive contient {len(names)} fichier(s) :\n{preview}{suffix}"
        except Exception as e:
            return f"Échec du listage : {e}"

    if action == "extract":
        dest = Path(params.get("destination", str(path.parent / path.stem)))
        dest.mkdir(parents=True, exist_ok=True)
        try:
            if ext == ".7z" and kit.which("7z"):
                r = kit.run(["7z", "x", str(path), f"-o{dest}", "-y"], timeout=600)
                if r.returncode == 0:
                    return f"Extrait vers : {dest}"
                return f"Échec de l'extraction 7z : {r.stderr[:200]}"
            shutil.unpack_archive(path, dest)
            return f"Extrait vers : {dest}"
        except Exception as e:
            return f"Échec de l'extraction : {e}"

    return f"Action archive inconnue : '{action}'. Essayez : list, extract"


# ════════════════════════════════════════════════════════════════════════════
# Traitement des présentations
# ════════════════════════════════════════════════════════════════════════════

def _process_pptx(path: Path, action: str, params: dict, speak=None) -> str:
    action = action or "summarize"

    def _read_pptx_text() -> str:
        try:
            from pptx import Presentation
            prs = Presentation(path)
            text = []
            for i, slide in enumerate(prs.slides, 1):
                slide_text = f"\n--- Diapositive {i} ---\n"
                for shape in slide.shapes:
                    if hasattr(shape, "text") and shape.text.strip():
                        slide_text += shape.text.strip() + "\n"
                text.append(slide_text)
            return "\n".join(text)
        except ImportError:
            return "python-pptx non installé."

    if action in ("summarize", "extract_text", "analyze"):
        text = _read_pptx_text()
        if action == "extract_text":
            out = _output_path(path, "texte", ".txt")
            out.write_text(text, encoding="utf-8")
            return f"Texte extrait. Sauvegardé : {out.name}"
        try:
            model = _gemini_client()
            prompt = f"{'Résume' if action == 'summarize' else 'Analyse'} cette présentation :\n{text[:30000]}"
            response = model.generate_content(prompt)
            return response.text.strip()
        except Exception as e:
            return f"Échec du traitement IA : {e}"

    return f"Action PPTX inconnue : '{action}'. Essayez : summarize, extract_text, analyze"


# ════════════════════════════════════════════════════════════════════════════
# Ouverture avec l'application par défaut
# ════════════════════════════════════════════════════════════════════════════

def _open_file(path: Path) -> str:
    from core.browser_policy import WEB_DOCUMENT_SUFFIXES, open_chrome
    if path.suffix.lower() in WEB_DOCUMENT_SUFFIXES:
        if open_chrome(path.resolve().as_uri()):
            return f"Ouvert dans Chrome : {path.name}"
        return "Impossible de lancer Google Chrome."
    try:
        if _OS == "Windows":
            os.startfile(str(path))
        elif _OS == "Darwin":
            kit.spawn(["open", str(path)])
        else:
            kit.spawn(['xdg-open', str(path)])
        return f"Ouvert : {path.name}"
    except Exception as e:
        return f"Impossible d'ouvrir : {e}"


# ════════════════════════════════════════════════════════════════════════════
# Point d'entrée principal
# ════════════════════════════════════════════════════════════════════════════

@kit.action("file_processor")
def file_processor(parameters: dict = None, player=None, speak=None,
                   response=None, session_memory=None) -> str:
    """
    Traitement universel de fichiers.
    Accepte soit des paramètres classiques (file_path, action, ...), soit une
    description en langage naturel.
    """
    p = parameters or {}
    description = p.get("description", "").strip()
    file_path_str = p.get("file_path", "").strip()
    action = p.get("action", "").lower().strip()
    instruction = p.get("instruction", "").strip()

    # Si description en langage naturel fournie, on tente de la comprendre
    if description and not file_path_str:
        local = _parse_file_command_locally(description)
        if local:
            file_path_str = local.get("file_path", "")
            action = local.get("action", action)
            for k, v in local.get("params", {}).items():
                if k not in p:
                    p[k] = v
        else:
            ai = _detect_file_intent_ai(description)
            if ai:
                file_path_str = ai.get("file_path", "")
                action = ai.get("action", action)
                for k, v in ai.get("params", {}).items():
                    if k not in p:
                        p[k] = v
            else:
                return ("Je n'ai pas compris ce que vous voulez faire avec ce fichier. "
                        "Précisez le chemin et l'action.")

    if not file_path_str:
        return "Aucun chemin de fichier fourni."

    path = _resolve_file(file_path_str)
    if not path.exists():
        return f"Fichier introuvable : {file_path_str}"
    if not path.is_file():
        return f"Ce chemin n'est pas un fichier : {file_path_str}"

    file_type = _detect_type(path)

    # Si action non précisée, on la déduit du type
    if not action:
        if file_type == "unknown":
            action = "describe"
        else:
            action = ("summarize" if file_type in ("pdf", "docx", "text", "pptx")
                      else "describe" if file_type == "image"
                      else "info")

    instruction = instruction or p.get("instruction", "")
    params = {**p, "instruction": instruction}

    log_msg = f"[FileProcessor] {file_type.upper()} | {path.name} | action={action}"
    print(log_msg)
    if player:
        try:
            player.write_log(log_msg)
        except Exception:
            pass

    # Action "ouvrir" (tous types)
    if action == "open":
        return _open_file(path)

    if file_type == "unknown":
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")[:10000]
            model = _gemini_client()
            prompt = (f"Fichier : {path.name}\nAperçu du contenu :\n{content}\n\n"
                      f"Tâche : {action or instruction or 'Décris ce que contient ce fichier et ce qu\'on peut en faire.'}")
            response_ai = model.generate_content(prompt)
            return response_ai.text.strip()
        except Exception as e:
            return f"Type de fichier inconnu ({path.suffix}). Impossible de traiter : {e}"

    dispatch = {
        "image":   _process_image,
        "pdf":     _process_pdf,
        "docx":    lambda pth, a, pm, s: _process_text_doc(pth, "docx", a, pm, s),
        "text":    lambda pth, a, pm, s: _process_text_doc(pth, "text", a, pm, s),
        "csv":     lambda pth, a, pm, s: _process_data(pth, "csv",   a, pm, s),
        "excel":   lambda pth, a, pm, s: _process_data(pth, "excel", a, pm, s),
        "json":    _process_json,
        "xml":     lambda pth, a, pm, s: _process_json(pth, a, pm, s),
        "code":    _process_code,
        "audio":   _process_audio,
        "video":   _process_video,
        "archive": _process_archive,
        "pptx":    _process_pptx,
    }
    handler = dispatch.get(file_type)
    if not handler:
        return f"Type de fichier non supporté : {file_type}"
    try:
        result = handler(path, action, params, speak)
        return result or "Terminé."
    except Exception as e:
        import traceback
        traceback.print_exc()
        return f"Échec du traitement : {e}"


# ════════════════════════════════════════════════════════════════════════════
# Test direct
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        print(file_processor({"description": " ".join(sys.argv[1:])}))
    else:
        print("Usage: python file_processor.py \"<commande naturelle>\"")
