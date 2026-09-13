"""core/multimodal_vision.py — Moteur de vision multimodale spécialisée.

Analyse approfondie pour schémas, graphiques, documents, code, UI et caméra.

La capture Hyprland, l'OCR local (terminaux sombres compris) et le meilleur
modèle Gemini Pro/Flash disponible produisent une explication vocale, une
fiche HUD, et des cibles UI que le pointeur néon peut encadrer.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core import screen_capture
from core import screen_reader
from core.live_model_policy import BALANCED_MODEL, PINNED_PRO_MODEL, REASONING_MODEL, REASONING_PREVIEW_MODEL


@dataclass
class VisionAnalysisResult:
    """Résultat complet d'une analyse visuelle multimodale."""
    domain: str
    spoken_summary: str
    detailed_markdown: str
    extracted_text: str = ""
    hud_card: Dict[str, Any] = field(default_factory=dict)
    key_points: List[str] = field(default_factory=list)
    confidence: float = 1.0
    objects: List[str] = field(default_factory=list)
    ui_targets: List[Dict[str, Any]] = field(default_factory=list)
    model_used: str = ""
    pointed: bool = False

    def as_tool_result(self, question: str = "") -> str:
        """Compte-rendu que Gemini Live lit au lieu de deviner l'image."""
        lines = [
            f"[VISION EXPERTE — {self.domain}"
            + (f" — {self.model_used}" if self.model_used else "")
            + "]",
            "L'analyse est TERMINÉE. Réponds à l'utilisateur à partir de ce "
            "compte-rendu. Ne rappelle PAS screen_process. Ne décris PAS une "
            "image qui arriverait ensuite : il n'y en a pas.",
        ]
        if self.spoken_summary:
            lines.append(f"\nSynthèse vocale (à dire, adaptée au mode de ton) :\n{self.spoken_summary}")
        if self.key_points:
            bullets = "\n".join(f"- {pt}" for pt in self.key_points[:8])
            lines.append(f"\nPoints clés :\n{bullets}")
        if self.objects:
            lines.append("Objets / éléments visibles : " + ", ".join(self.objects[:12]))
        if self.extracted_text and len(self.extracted_text) > 30:
            lines.append(
                "\nTexte lu sur l'image (exact, ne pas inventer) :\n"
                f"```\n{self.extracted_text[:2500]}\n```"
            )
        if self.ui_targets:
            lines.append("\nCibles UI (boîtes 0-1000 ymin,xmin,ymax,xmax) :")
            for target in self.ui_targets[:8]:
                label = str(target.get("label") or "élément")
                box = (
                    f"{target.get('ymin', 0)},{target.get('xmin', 0)},"
                    f"{target.get('ymax', 0)},{target.get('xmax', 0)}"
                )
                lines.append(f"- {label} @ {box}")
            lines.append(
                "Si l'utilisateur demande où se trouve un élément, appelle "
                "point_on_screen avec description=libellé et ces coordonnées."
            )
        if self.pointed:
            lines.append("Un cadre néon a déjà été posé sur la cible principale.")
        if self.detailed_markdown and self.detailed_markdown != self.spoken_summary:
            lines.append(f"\nDétail :\n{self.detailed_markdown[:1800]}")
        if question:
            lines.append(f"\nQuestion posée : {question[:200]}")
        return "\n".join(lines)


# ── Configuration ────────────────────────────────────────────────────────────

_DEFAULT_VISION_MODELS = (
    REASONING_MODEL,
    REASONING_PREVIEW_MODEL,
    PINNED_PRO_MODEL,
    BALANCED_MODEL,
)

_MODEL_FAILURE_MARKERS = (
    "model not found", "not found", "not supported", "unknown model",
    "invalid model", "is not found", "404",
    # Un quota épuisé rend le modèle tout aussi inutilisable qu'un modèle
    # absent : sur un compte gratuit, gemini-pro a une limite de zéro et
    # remontait une 429 qui interrompait toute la cascade au lieu de laisser
    # Flash répondre. La vision tombait alors en panne complète.
    "resource_exhausted", "429", "quota", "rate limit", "rate_limit",
    # Surcharge passagère : le modèle suivant de la cascade répondra, alors
    # qu'abandonner ici laisse l'utilisateur sans réponse visuelle du tout.
    "unavailable", "503", "overloaded", "high demand",
)

_working_vision_model: str = ""

_EXPERT_HINTS = (
    "analyse", "explique", "schema", "schéma", "graphique", "courbe",
    "architecture", "diagramme", "diagram", "pdf", "document", "photo",
    "image", "regarde", "vois-tu", "vois tu", "que vois", "montre",
    "où est", "ou est", "pointe", "surligne", "c'est quoi ça", "cest quoi ca",
    "c'est quoi ca", "objet", "logo", "personne", "visage", "écran", "ecran",
    "caméra", "camera", "à quoi", "a quoi", "décris", "decris",
)

_POINT_HINTS = (
    "où est", "ou est", "montre", "pointe", "surligne", "encadre",
    "c'est où", "cest ou", "where is", "highlight", "montre-moi", "montre moi",
)


def _base_dir() -> Path:
    import sys
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def _load_cfg() -> dict:
    try:
        return json.loads((_base_dir() / "config" / "api_keys.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _get_api_key() -> str:
    return str(_load_cfg().get("gemini_api_key") or "")


def vision_model_cascade(cfg: Optional[dict] = None) -> List[str]:
    """Ordre des modèles vision : Pro d'abord, Flash en repli, sans doublon."""
    data = cfg if cfg is not None else _load_cfg()
    primary = str(data.get("vision_model") or "").strip()
    fallback = str(data.get("vision_model_fallback") or "").strip()
    ordered: List[str] = []
    for name in (primary, *_DEFAULT_VISION_MODELS, fallback, BALANCED_MODEL):
        if name and name not in ordered:
            ordered.append(name)
    # Un modèle déjà validé passe devant les replis, jamais devant le choix
    # explicite de l'utilisateur.
    if _working_vision_model and _working_vision_model in ordered and not primary:
        ordered.remove(_working_vision_model)
        ordered.insert(0, _working_vision_model)
    return ordered


def capture_policy(
    window_info: Optional[screen_capture.WindowInfo] = None,
    domain: str = "general",
) -> Dict[str, Any]:
    """Plus de pixels sur le texte, compression plus légère sur les schémas."""
    text_heavy = domain in {"code", "debug", "document"}
    if window_info is not None:
        text_heavy = text_heavy or window_info.is_terminal or window_info.is_ide or window_info.is_doc_or_pdf
    if text_heavy:
        return {"max_dim": (2560, 1600), "quality": 92}
    if domain in {"architecture", "chart"}:
        return {"max_dim": (1920, 1080), "quality": 90}
    return {"max_dim": (1600, 1000), "quality": 85}


def wants_pointing(query: str) -> bool:
    lowered = " ".join((query or "").lower().split())
    return any(hint in lowered for hint in _POINT_HINTS)


def should_use_expert_vision(
    query: str,
    *,
    ocr_usable: bool = False,
    domain: str = "general",
    angle: str = "screen",
) -> bool:
    """L'analyse Pro+OCR+cibles UI, pas juste le dump JPEG vers Live."""
    if (angle or "screen").lower() == "camera":
        return True
    if screen_reader.wants_image(query):
        return True
    if domain in {"architecture", "chart", "document", "code"}:
        return True
    lowered = " ".join((query or "").lower().split())
    if any(hint in lowered for hint in _EXPERT_HINTS):
        return True
    return not ocr_usable


# ── Détection de domaine ─────────────────────────────────────────────────────

_DOMAIN_KEYWORDS = {
    "architecture": (
        "architecture", "reseau", "réseau", "schema", "schéma", "diagramme", "diagram",
        "topology", "topologie", "infrastructure", "cloud", "aws", "kubernetes", "k8s",
        "docker", "microservice", "flux", "database", "bdd", "load balancer", "firewall",
        "subnet", "vpc", "api gateway", "kafka", "queue",
    ),
    "chart": (
        "graphique", "chart", "courbe", "histogramme", "bar chart", "pie chart", "camembert",
        "statistique", "stats", "metrique", "métrique", "dashboard", "grafana", "tendance",
        "scatter", "axe", "donnees", "données", "pourcentage", "evolution", "évolution",
    ),
    "document": (
        "pdf", "document", "article", "page", "texte", "tableau", "table", "spec",
        "rapport", "formule", "chapitre", "paragraphe", "livre", "contrat", "facture",
    ),
    "code": (
        "code", "fonction", "classe", "algorithme", "programme", "script", "diff",
        "git", "refactor", "syntaxe", "variable", "boucle", "async", "thread",
    ),
    "debug": (
        "bug", "erreur", "error", "traceback", "panic", "crash", "exception", "failed",
        "echec", "échec", "terminal", "log", "compilation", "build",
    ),
    "ui": (
        "bouton", "menu", "icône", "icone", "champ", "formulaire", "interface",
        "où est", "ou est", "pointe", "surligne", "clique", "onglet",
    ),
}


def detect_visual_domain(query: str, window_info: Optional[screen_capture.WindowInfo] = None) -> str:
    """Détecte la catégorie de perception visuelle la plus adaptée."""
    q = (query or "").lower()

    for domain, kws in _DOMAIN_KEYWORDS.items():
        if any(kw in q for kw in kws):
            return domain

    if window_info:
        if window_info.is_terminal:
            return "debug" if any(w in q for w in ("quoi", "pourquoi", "aide", "regarde", "explique")) else "code"
        if window_info.is_ide:
            return "code"
        if window_info.is_doc_or_pdf:
            return "document"
        if window_info.is_browser and wants_pointing(query):
            return "ui"

    return "general"


def _tone_directive() -> str:
    try:
        from core.personality_modes import PersonalityMode, active_mode
        mode = active_mode()
    except Exception:
        return (
            "spoken_summary : français naturel, 2 à 3 phrases, tutoiement ou "
            "vouvoiement selon le mode de ton actif."
        )
    if mode is PersonalityMode.ASTRO:
        return (
            "spoken_summary : TUTOIEMENT, cash, une vanne courte collée à ce "
            "qui est vu, jamais Monsieur, jamais JARVIS."
        )
    if mode is PersonalityMode.COQUIN:
        return "spoken_summary : tutoiement, complice, une pique légère, jamais Monsieur."
    if mode is PersonalityMode.MAJEUR:
        return "spoken_summary : vouvoiement, dis Monsieur, concis, zéro vanne."
    return "spoken_summary : vouvoiement souple, calme, jamais Monsieur, jamais de vanne."


def _build_domain_system_prompt(domain: str) -> str:
    base = (
        "Tu es l'œil expert d'ANO-GPT. Tu analyses l'image capturée sur l'écran "
        "ou la caméra avec une précision technique maximale.\n"
        "RÈGLE DE FIABILITÉ : fonde chaque affirmation sur les pixels de cette image "
        "et le texte OCR fourni. N'infère jamais un contenu hors champ. "
        "Si l'information demandée n'est pas lisible, dis-le explicitement.\n"
        f"{_tone_directive()}\n"
    )

    if domain == "architecture":
        return base + (
            "DOMAINE : SCHÉMA D'ARCHITECTURE & RÉSEAU\n"
            "- Identifie les composants clés (serveurs, conteneurs, clusters K8s, bases de données, passerelles API, routeurs, files de messages).\n"
            "- Décris les flux de communication, protocoles (HTTP, gRPC, WebSocket), zones de sécurité et sens des flèches.\n"
            "- Mets en évidence les goulots d'étranglement potentiels, points uniques de défaillance (SPOF) ou règles d'isolation réseau.\n"
            "- Sois rigoureux sur les noms exacts des technologies et blocs visibles."
        )
    if domain == "chart":
        return base + (
            "DOMAINE : GRAPHIQUES & VISUALISATION DE DONNÉES\n"
            "- Identifie le type de graphique (courbes temporelles, barres comparatives, nuage de points, camembert).\n"
            "- Nomme précisément les axes X et Y, les unités de mesure et les légendes.\n"
            "- Dégage les points clés : pics, tendances générales (hausse/baisse), valeurs extrêmes, anomalies remarquables.\n"
            "- Conclus par la signification concrète des données."
        )
    if domain == "document":
        return base + (
            "DOMAINE : DOCUMENTS, PDFS & TABLEAUX\n"
            "- Structure l'analyse : titre principal, sections majeures, résumé des paragraphes clés.\n"
            "- Extrais les données chiffrées exactes, tableaux ou équations mathématiques importantes.\n"
            "- Fournis une synthèse claire permettant de comprendre l'essentiel sans lire le document entier."
        )
    if domain == "code":
        return base + (
            "DOMAINE : ANALYSE DE CODE & LOGIQUE ALGORITHMIQUE\n"
            "- Identifie le langage, les structures de données, classes et fonctions à l'écran.\n"
            "- Analyse la logique : complexité, gestion des cas limites, concurrency, fuites ou anti-patterns.\n"
            "- Fournis des explications concrètes et montre comment optimiser ou corriger si pertinent."
        )
    if domain == "debug":
        return base + (
            "DOMAINE : DÉBOGAGE LIVE & SORTIE TERMINAL\n"
            "- Isole l'erreur exacte, la ligne incriminée et la cause racine du plantage.\n"
            "- Fournis la commande exacte ou le correctif de code pour résoudre le problème immédiatement."
        )
    if domain == "ui":
        return base + (
            "DOMAINE : INTERFACE & CIBLAGE\n"
            "- Repère boutons, menus, champs, onglets, messages et leur libellé exact.\n"
            "- Remplis ui_targets avec une boîte 0-1000 (ymin, xmin, ymax, xmax) pour chaque élément utile.\n"
            "- Si une question demande « où est X », X doit être la première cible."
        )
    return base + (
        "DOMAINE : ANALYSE VISUELLE GÉNÉRALE\n"
        "- Décris clairement ce qui est affiché : contenu principal, objets, texte, personnes, éléments interactifs.\n"
        "- Réponds précisément et directement à la question posée.\n"
        "- Remplis ui_targets pour tout bouton, titre ou zone que l'utilisateur pourrait vouloir qu'on montre."
    )


def _parse_vision_json(raw: str) -> dict:
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return {"spoken_summary": text[:250], "detailed_markdown": text, "key_points": []}
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {"spoken_summary": text[:250], "detailed_markdown": text, "key_points": []}


def _normalize_targets(raw: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in raw[:12]:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or item.get("name") or "").strip()
        box = item.get("box") if isinstance(item.get("box"), (list, tuple)) else None
        try:
            if box and len(box) >= 4:
                ymin, xmin, ymax, xmax = (float(box[0]), float(box[1]), float(box[2]), float(box[3]))
            else:
                ymin = float(item.get("ymin", 0))
                xmin = float(item.get("xmin", 0))
                ymax = float(item.get("ymax", 0))
                xmax = float(item.get("xmax", 0))
        except (TypeError, ValueError):
            continue
        if ymax <= ymin or xmax <= xmin:
            continue
        out.append({
            "label": label or "élément",
            "ymin": int(ymin), "xmin": int(xmin),
            "ymax": int(ymax), "xmax": int(xmax),
        })
    return out


def _model_unavailable(exc: BaseException) -> bool:
    message = " ".join(str(exc).casefold().split())
    return any(marker in message for marker in _MODEL_FAILURE_MARKERS)


# Une pointe de charge chez Google dure quelques secondes. Traverser toute la
# cascade sans jamais réessayer laissait l'utilisateur sans réponse visuelle
# alors qu'une seconde tentative aurait suffi.
_TRANSIENT_MARKERS = ("503", "unavailable", "overloaded", "high demand")
_TRANSIENT_RETRY_DELAY = 1.2


def _is_transient(exc: BaseException) -> bool:
    message = str(exc).casefold()
    return any(marker in message for marker in _TRANSIENT_MARKERS)


def _call_gemini_vision(client: Any, gtypes: Any, contents: list, models: Sequence[str]) -> Tuple[Any, str]:
    """Essaie Pro puis Flash. Mémorise le premier modèle qui répond."""
    global _working_vision_model
    last_exc: Optional[BaseException] = None
    retried: set = set()
    models = list(models)
    for model in models:
        try:
            config = gtypes.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.2,
            )
            resp = client.models.generate_content(
                model=model, contents=contents, config=config,
            )
        except TypeError:
            try:
                resp = client.models.generate_content(model=model, contents=contents)
            except Exception as exc:
                last_exc = exc
                if _model_unavailable(exc):
                    continue
                raise
        except Exception as exc:
            last_exc = exc
            if _is_transient(exc) and model not in retried:
                # Surcharge passagère : on redonne sa chance au modèle courant
                # avant de descendre d'un cran en qualité.
                retried.add(model)
                print(f"[Vision] {model} surchargé, nouvelle tentative.")
                time.sleep(_TRANSIENT_RETRY_DELAY)
                models.append(model)
                continue
            if _model_unavailable(exc):
                print(f"[Vision] modèle {model} indisponible, repli.")
                continue
            raise
        text = (getattr(resp, "text", None) or "").strip()
        if not text:
            last_exc = RuntimeError(f"{model} a renvoyé une réponse vide")
            continue
        _working_vision_model = model
        return resp, model
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("Aucun modèle vision n'a répondu.")


def _boxes_to_pixels(
    target: Dict[str, Any],
    origin: Tuple[int, int],
    size: Tuple[int, int],
) -> Optional[Tuple[int, int, int, int]]:
    w, h = size
    if w <= 0 or h <= 0:
        return None
    ox, oy = origin
    x = ox + int(target["xmin"] * w / 1000)
    y = oy + int(target["ymin"] * h / 1000)
    bw = max(8, int((target["xmax"] - target["xmin"]) * w / 1000))
    bh = max(8, int((target["ymax"] - target["ymin"]) * h / 1000))
    return x, y, bw, bh


def highlight_vision_targets(
    targets: Sequence[Dict[str, Any]],
    metadata: Optional[Dict[str, Any]] = None,
    query: str = "",
) -> bool:
    """Encadre la première cible utile si la question demande où c'est."""
    if not targets or not wants_pointing(query):
        return False
    meta = metadata or {}
    origin = tuple(meta.get("capture_origin") or (0, 0))
    size = tuple(meta.get("capture_size") or (0, 0))
    if len(origin) < 2 or len(size) < 2 or not size[0]:
        return False
    try:
        from ui.visual_pointer import get_visual_pointer
        pointer = get_visual_pointer()
    except Exception:
        return False
    q = (query or "").casefold()
    chosen = targets[0]
    for candidate in targets:
        label = str(candidate.get("label") or "").casefold()
        if label and label in q:
            chosen = candidate
            break
    box = _boxes_to_pixels(chosen, (int(origin[0]), int(origin[1])), (int(size[0]), int(size[1])))
    if box is None:
        return False
    try:
        pointer.highlight_region(*box, label=str(chosen.get("label") or "Ici"), duration=3.5)
        return True
    except Exception as exc:
        print(f"[Vision] pointeur impossible : {exc}")
        return False


def analyze_visual_content(
    image_bytes: bytes,
    mime_type: str = "image/jpeg",
    user_query: str = "",
    domain: Optional[str] = None,
    window_info: Optional[screen_capture.WindowInfo] = None,
    metadata: Optional[Dict[str, Any]] = None,
    player: Optional[Any] = None,
    extracted_text: Optional[str] = None,
) -> VisionAnalysisResult:
    """Exécute l'analyse multimodale Gemini avec injection du contexte de domaine."""
    api_key = _get_api_key()
    resolved_domain = domain or detect_visual_domain(user_query, window_info)

    if not api_key:
        return VisionAnalysisResult(
            domain=resolved_domain,
            spoken_summary="La clé API Gemini est nécessaire pour l'analyse visuelle multimodale.",
            detailed_markdown="⚠️ Clé API Gemini introuvable dans `config/api_keys.json`.",
            hud_card={"title": "👁️ Vision Indisponible", "body": "Clé API absente.", "type": "error"},
        )

    local_text = extracted_text if extracted_text is not None else ""
    if extracted_text is None:
        try:
            # question="" : on veut le texte même si la demande est visuelle
            # (axes d'un graphique, libellés d'une UI).
            ocr_res = screen_reader.read(image_bytes, question="")
            if ocr_res and ocr_res.text:
                local_text = ocr_res.text
        except Exception:
            pass

    from google import genai
    from google.genai import types as gtypes

    client = genai.Client(api_key=api_key)
    system_instruction = _build_domain_system_prompt(resolved_domain)

    win_desc = ""
    if window_info and window_info.window_class:
        win_desc = f"\nFenêtre active : {window_info.window_class} — \"{window_info.title}\""

    ocr_ctx = ""
    if local_text and len(local_text) > 30:
        ocr_ctx = f"\nTexte brut extrait localement par OCR :\n```\n{local_text[:2500]}\n```\n"

    query_text = user_query.strip() if user_query.strip() else "Explique et analyse ce que tu vois à l'écran."

    user_prompt = f"""{system_instruction}

Question de l'utilisateur : "{query_text}"{win_desc}{ocr_ctx}

Fournis ta réponse sous forme d'un objet JSON strict avec :
{{
  "spoken_summary": "2 à 3 phrases prêtes à dire à voix haute, dans le ton imposé.",
  "key_points": ["Point clé 1", "Point clé 2", "Point clé 3"],
  "objects": ["élément visible 1", "élément 2"],
  "ui_targets": [
    {{"label": "libellé exact", "ymin": 0, "xmin": 0, "ymax": 0, "xmax": 0}}
  ],
  "confidence": 0.0,
  "detailed_markdown": "Rapport Markdown structuré."
}}
ui_targets : boîtes normalisées 0-1000. Vide si rien n'est localisable.
Rends UNIQUEMENT le JSON."""

    contents = [
        gtypes.Part.from_bytes(data=image_bytes, mime_type=mime_type),
        user_prompt,
    ]

    model_used = ""
    try:
        resp, model_used = _call_gemini_vision(
            client, gtypes, contents, vision_model_cascade(),
        )
        data = _parse_vision_json(resp.text)
    except Exception as exc:
        data = {
            "spoken_summary": f"L'analyse visuelle a rencontré une difficulté : {exc}",
            "key_points": [],
            "detailed_markdown": f"Erreur lors de l'appel vision : {exc}",
        }

    spoken = str(data.get("spoken_summary") or "").strip() or "Analyse visuelle terminée."
    detailed = str(data.get("detailed_markdown") or "").strip() or spoken
    key_pts = data.get("key_points") or []
    if not isinstance(key_pts, list):
        key_pts = []
    objects = data.get("objects") or []
    if not isinstance(objects, list):
        objects = []
    objects = [str(item) for item in objects if str(item).strip()]
    targets = _normalize_targets(data.get("ui_targets"))
    try:
        confidence = float(data.get("confidence") or 1.0)
    except (TypeError, ValueError):
        confidence = 1.0

    pointed = highlight_vision_targets(targets, metadata, user_query)

    domain_titles = {
        "architecture": "🏗️ Architecture & Réseau",
        "chart": "📊 Données & Graphiques",
        "document": "📄 Analyse de Document",
        "code": "💻 Code & Algorithmes",
        "debug": "🐞 Diagnostic Système",
        "ui": "🎯 Interface",
        "general": "👁️ Perception d'Écran",
    }
    card_title = domain_titles.get(resolved_domain, "👁️ Vision JARVIS")

    card_body = detailed
    if key_pts and not detailed.startswith("#"):
        bullet_list = "\n".join(f"• {pt}" for pt in key_pts)
        card_body = f"**Points Clés :**\n{bullet_list}\n\n{detailed}"

    hud_card = {
        "title": card_title,
        "body": card_body[:2000],
        "type": "info" if resolved_domain != "debug" else "error",
    }

    result = VisionAnalysisResult(
        domain=resolved_domain,
        spoken_summary=spoken,
        detailed_markdown=detailed,
        extracted_text=local_text,
        hud_card=hud_card,
        key_points=[str(pt) for pt in key_pts],
        confidence=confidence,
        objects=objects,
        ui_targets=targets,
        model_used=model_used,
        pointed=pointed,
    )
    return result


def inspect_screen_live(
    user_query: str = "",
    target: str = "active_window",
    domain: Optional[str] = None,
    player: Optional[Any] = None,
    image_bytes: Optional[bytes] = None,
    mime_type: str = "image/jpeg",
    window_info: Optional[screen_capture.WindowInfo] = None,
    metadata: Optional[Dict[str, Any]] = None,
    extracted_text: Optional[str] = None,
) -> Tuple[str, VisionAnalysisResult]:
    """
    Capture instantanément l'écran/fenêtre et exécute l'analyse multimodale spécialisée.
    Affiche la carte de résultat dans le HUD et retourne la synthèse vocale.
    """
    win_info = window_info
    meta = dict(metadata or {})
    if image_bytes is None:
        if win_info is None:
            win_info = screen_capture.get_active_window(skip_anogpt=True)
        guessed = domain or detect_visual_domain(user_query, win_info)
        policy = capture_policy(win_info, guessed)
        image_bytes, mime_type, meta = screen_capture.capture_window_or_screen(
            target=target,
            compress=True,
            max_dim=policy["max_dim"],
            quality=policy["quality"],
        )
        if win_info is None:
            win_info = screen_capture.get_active_window(skip_anogpt=True)

    result = analyze_visual_content(
        image_bytes=image_bytes,
        mime_type=mime_type,
        user_query=user_query,
        domain=domain,
        window_info=win_info,
        metadata=meta,
        player=player,
        extracted_text=extracted_text,
    )

    if player and hasattr(player, "show_card"):
        try:
            player.show_card(
                type=result.hud_card.get("type", "info"),
                title=result.hud_card.get("title", "👁️ Vision"),
                body=result.hud_card.get("body", ""),
            )
        except Exception:
            pass

    return result.spoken_summary, result
