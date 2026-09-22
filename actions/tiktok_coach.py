#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tiktok_coach.py — Coach TikTok « à la Blow Up ».

Trois services, tous nourris par les lectures de `tiktok_tracker` :

- **diagnose** : pourquoi telle vidéo plafonne à N vues / J'aime. Les chiffres
  (vues, taux de J'aime, commentaires, partages, heure de publication, durée,
  hashtags) sont d'abord passés au crible de règles connues du fonctionnement
  de TikTok ; puis la vidéo elle-même est téléchargée (yt-dlp) et regardée par
  Gemini avec ces chiffres, pour un verdict qui parle du contenu réel
  (accroche, rythme, texte à l'écran, son, appel à l'action).
- **review** : bilan du compte — ce qui marche, ce qui ne marche pas, un plan.
- **draft** : analyse d'une vidéo PAS ENCORE publiée (fichier local) : accroche,
  rétention, description et hashtags proposés, meilleur moment pour poster
  d'après l'historique du compte.

Regarder une vidéo prend 30 à 90 s côté Gemini : diagnose et draft partent en
tâche de fond et l'assistant annonce le résultat quand il est prêt, comme la
génération de vidéo. Sans clé Gemini ou en cas d'échec, les règles seules
donnent déjà un diagnostic honnête.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import statistics
import subprocess
import threading
import time
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from actions import tiktok_tracker as tt
from core.live_model_policy import BALANCED_MODEL, FAST_MODEL

CARD_TYPE = "tiktok"
# Dossier où le créateur dépose ce qu'il s'apprête à publier : c'est la
# première source de `draft` et de `list`, avant les dossiers génériques.
TIKTOK_DIR = Path(os.environ.get("ANOGPT_TIKTOK_DIR") or Path.home() / "Vidéos" / "ANO-GPT" / "TIKTOK")
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "anogpt" / "tiktok"
INLINE_LIMIT = 19 * 1024 * 1024        # au-delà, passage par l'API Files
# Flash 3 en premier : c'est lui qui a le quota gratuit et qui digère une
# vidéo de 90 s en moins d'une minute ; les alias « latest » servent de repli.
VIDEO_MODELS = (BALANCED_MODEL, FAST_MODEL)
TEXT_MODELS = (BALANCED_MODEL, FAST_MODEL)
TRANSIENT_RETRY_S = 8.0
_ACTIVE_LOCK = threading.Lock()
_ACTIVE: dict[str, float] = {}       # analyses en cours, par clé
_REPORT_LOCK = threading.Lock()
_READY_REPORTS: dict[str, dict[str, Any]] = {}
_LAST_REPORT_ID = ""
REPORT_DIR = Path(os.environ.get("ANOGPT_TIKTOK_REPORT_DIR") or
                  Path.home() / "Documents" / "ANO-GPT" / "Diagnostics TikTok")


# ════════════════════════════════════════════════════════════════════════════
# Jeu de données du compte
# ════════════════════════════════════════════════════════════════════════════

def _rate(num: int, den: int) -> float:
    return (num / den) if den else 0.0


def enrich(video: dict, now: Optional[float] = None) -> dict:
    """Ajoute les taux et le moment de publication à une vidéo du suivi."""
    now = now or time.time()
    v = dict(video)
    plays = v.get("plays", 0)
    v["like_rate"] = _rate(v.get("likes", 0), plays)
    v["comment_rate"] = _rate(v.get("comments", 0), plays)
    v["share_rate"] = _rate(v.get("shares", 0), plays)
    v["save_rate"] = _rate(v.get("saves", 0), plays)
    v["engagement_rate"] = _rate(
        v.get("likes", 0) + v.get("comments", 0) + v.get("shares", 0) + v.get("saves", 0), plays
    )
    created = v.get("created") or 0
    if created:
        dt = datetime.fromtimestamp(created)
        v["posted_at"] = dt.strftime("%A %d/%m %H:%M")
        v["hour"] = dt.hour
        v["weekday"] = dt.strftime("%A")
        v["age_h"] = max(0.0, (now - created) / 3600)
    else:
        v["posted_at"], v["hour"], v["weekday"], v["age_h"] = "?", -1, "?", 0.0
    return v


def dataset(state: Optional[dict] = None) -> tuple[dict, list[dict]]:
    """Dernière lecture du compte + vidéos enrichies (récente en tête)."""
    state = state if state is not None else tt.load_state()
    snaps = state.get("snapshots") or []
    if not snaps or not snaps[-1].get("items"):
        if not state.get("handle"):
            raise LookupError("Aucun compte TikTok configuré : donne-moi ton @.")
        cur, _prev, state = tt.poll_once(None, state)
        snaps = state.get("snapshots") or []
    cur = snaps[-1]
    items = [enrich(v, cur.get("ts")) for v in cur.get("items") or []]
    return cur, items


def account_summary(items: list[dict]) -> dict:
    if not items:
        return {}
    plays = [v["plays"] for v in items]
    like_rates = [v["like_rate"] for v in items if v["plays"] >= 20]
    by_hour: dict[int, list[int]] = {}
    for v in items:
        if v["hour"] >= 0:
            by_hour.setdefault(v["hour"], []).append(v["plays"])
    best_hours = sorted(
        ((statistics.median(p), h) for h, p in by_hour.items() if len(p) >= 1),
        reverse=True,
    )[:3]
    created = sorted(v["created"] for v in items if v.get("created"))
    # Cadence réelle sur toute la période, pas l'écart médian : cinq vidéos
    # postées le même jour ne font pas « 30 par semaine ».
    span_days = max(1.0, (created[-1] - created[0]) / 86400) if len(created) > 1 else 0.0
    tags: dict[str, int] = {}
    for v in items:
        for t in v.get("hashtags") or []:
            tags[t] = tags.get(t, 0) + 1
    return {
        "count": len(items),
        "median_plays": statistics.median(plays),
        "max_plays": max(plays),
        "min_plays": min(plays),
        "median_like_rate": statistics.median(like_rates) if like_rates else 0.0,
        "best_hours": [h for _, h in best_hours],
        "posts_per_week": ((len(created) - 1) / span_days * 7) if span_days else 0.0,
        "median_duration": statistics.median([v["duration"] for v in items if v.get("duration")] or [0]),
        "top_tags": sorted(tags.items(), key=lambda kv: -kv[1])[:8],
        "best": max(items, key=lambda v: v["plays"]),
        "worst": min(items, key=lambda v: v["plays"]),
    }


_ORDINALS = {"premiere": 0, "première": 0, "deuxieme": 1, "deuxième": 1, "seconde": 1,
             "troisieme": 2, "troisième": 2, "quatrieme": 3, "quatrième": 3, "cinquieme": 4, "cinquième": 4}


def pick_video(items: list[dict], query: str) -> Optional[dict]:
    """Vidéo visée : URL/id, « la dernière », « l'avant-dernière », « la plus
    vue », « celle qui a le moins marché », un ordinal, ou des mots du titre."""
    if not items:
        return None
    q = " ".join(str(query or "").lower().split())
    m = re.search(r"/video/(\d+)", q) or re.search(r"\b(\d{15,})\b", q)
    if m:
        return next((v for v in items if v["id"] == m.group(1)), None)
    if not q or re.search(r"\b(derni[eè]re|last|latest|r[ée]cente)\b", q) and "avant" not in q:
        return items[0]
    if "avant" in q and "derni" in q:
        return items[1] if len(items) > 1 else items[0]
    if re.search(r"plus vue|mieux march|meilleure|best|top", q):
        return max(items, key=lambda v: v["plays"])
    if re.search(r"moins (?:vue|march)|pire|flop|worst|moins bien", q):
        return min(items, key=lambda v: v["plays"])
    for word, idx in _ORDINALS.items():
        if word in q and idx < len(items):
            return items[idx]
    words = [w for w in re.findall(r"[a-zà-ÿ0-9']{3,}", q)
             if w not in {"vidéo", "video", "pourquoi", "celle", "avec", "dans", "pas", "marché", "marche", "elle"}]
    best, score = None, 0
    for v in items:
        text = (v.get("desc") or "").lower()
        hits = sum(1 for w in words if w in text)
        if hits > score:
            best, score = v, hits
    return best or items[0]


# ════════════════════════════════════════════════════════════════════════════
# Règles : ce que les chiffres disent d'eux-mêmes
# ════════════════════════════════════════════════════════════════════════════

def heuristic_findings(v: dict, summary: dict) -> list[str]:
    """Lecture des chiffres à la lumière du fonctionnement de TikTok."""
    out: list[str] = []
    plays, _likes = v["plays"], v["likes"]
    median = summary.get("median_plays") or 0
    if v["age_h"] < 24:
        out.append("Elle a moins de 24 h : TikTok teste encore, le verdict n'est pas définitif.")
    if plays < 300:
        out.append("Sous 300 vues, la vidéo n'est pas sortie du premier lot de test : TikTok l'a montrée "
                   "à un petit échantillon et le temps de visionnage n'a pas justifié un second lot. "
                   "C'est presque toujours l'accroche des deux premières secondes ou la rétention.")
    elif plays < 1000:
        out.append("Entre 300 et 1 000 vues : elle a passé le premier lot mais pas le deuxième. "
                   "Le début accroche, la suite perd les gens ou ne fait pas réagir.")
    if median and plays < 0.6 * median:
        out.append(f"Elle fait nettement moins que ta médiane ({int(median)} vues) : le sujet ou "
                   "le format s'écarte de ce que ton audience attend.")
    elif median and plays > 1.8 * median:
        out.append(f"Elle fait bien mieux que ta médiane ({int(median)} vues) : c'est un format à répéter.")
    if plays >= 50:
        if v["like_rate"] < 0.03:
            out.append(f"Taux de J'aime {v['like_rate'] * 100:.1f} % (sous 3 %) : le contenu est vu mais "
                       "ne provoque rien — pas de moment « waouh » ou d'émotion nette.")
        elif v["like_rate"] >= 0.08:
            out.append(f"Taux de J'aime {v['like_rate'] * 100:.1f} % : excellent, ceux qui la voient "
                       "adorent. Le problème n'est pas le contenu mais la diffusion (accroche, sujet trop niche).")
        if v["comments"] == 0:
            out.append("Zéro commentaire : rien n'invite à répondre. Une question, une opinion tranchée "
                       "ou une erreur volontaire font parler — et les commentaires pèsent lourd.")
        if v["shares"] == 0 and v["saves"] == 0:
            out.append("Aucun partage ni enregistrement : la vidéo n'est ni « à envoyer à un pote » "
                       "ni « à garder ». Les partages sont le signal le plus fort de l'algorithme.")
    if v.get("duration", 0) > 60 and plays < 1000:
        out.append(f"{v['duration']} s : c'est long pour un compte qui démarre. La rétention chute "
                   "avec la durée ; vise 15 à 35 s tant que l'audience n'est pas installée, ou découpe en série.")
    tags = v.get("hashtags") or []
    generic = {"fyp", "foryou", "foryoupage", "pourtoi", "viral", "fy", "tiktok"}
    if tags and all(t in generic for t in tags):
        out.append("Hashtags uniquement génériques (#fyp, #foryou…) : ils n'apprennent rien à TikTok "
                   "sur le sujet. Mets 3 à 5 hashtags de niche précis.")
    elif not tags:
        out.append("Pas de hashtag : TikTok classe la vidéo à l'aveugle. 3 à 5 hashtags de niche aident au premier lot.")
    if 0 <= v["hour"] < 7:
        out.append(f"Publiée à {v['hour']} h : peu de monde éveillé pour le premier lot de test, "
                   "qui se joue dans la première heure. Vise 12 h–14 h ou 18 h–21 h.")
    best_hours = summary.get("best_hours") or []
    if best_hours and v["hour"] >= 0 and v["hour"] not in best_hours:
        out.append("Tes vidéos qui marchent le mieux ont été postées vers "
                   + ", ".join(f"{h} h" for h in best_hours) + ".")
    desc = (v.get("desc") or "")
    if len(re.sub(r"#\w+", "", desc).strip()) < 12:
        out.append("Description quasi vide : une phrase qui crée une attente (« attends la fin », "
                   "« personne ne fait ça ») ajoute du contexte à l'algorithme et aux curieux.")
    return out


def best_posting_advice(summary: dict) -> str:
    hours = summary.get("best_hours") or []
    if hours:
        return "D'après tes vidéos, poste vers " + " ou ".join(f"{h} h" for h in hours[:2]) + "."
    return "Pas encore assez d'historique : poste entre 12 h et 14 h ou 18 h et 21 h, et compare."


# ════════════════════════════════════════════════════════════════════════════
# Gemini : regarder la vidéo
# ════════════════════════════════════════════════════════════════════════════

def _gemini():
    from core.multimodal_vision import _get_api_key
    key = _get_api_key()
    if not key:
        raise RuntimeError("Clé Gemini absente.")
    from google import genai
    from google.genai import types as gtypes
    return genai.Client(api_key=key), gtypes


def _generate_json(contents: list, models: tuple[str, ...], *, low_res: bool = False) -> dict:
    from core.multimodal_vision import _parse_vision_json
    client, gtypes = _gemini()
    last: Optional[Exception] = None
    for model in models:
        for attempt in (1, 2):
            try:
                kwargs: dict[str, Any] = dict(response_mime_type="application/json", temperature=0.3)
                if low_res:
                    kwargs["media_resolution"] = "MEDIA_RESOLUTION_LOW"
                resp = client.models.generate_content(
                    model=model, contents=contents, config=gtypes.GenerateContentConfig(**kwargs),
                )
                data = _parse_vision_json(getattr(resp, "text", "") or "")
                if data:
                    return data
                last = RuntimeError(f"{model} : réponse vide")
                break
            except Exception as exc:
                last = exc
                text = str(exc)
                print(f"[Coach TikTok] {model} : {text[:160]}")
                # Surcharge passagère (503) : une seconde chance au même modèle
                # vaut mieux qu'un repli vers un modèle sans quota gratuit.
                if attempt == 1 and ("503" in text or "UNAVAILABLE" in text):
                    time.sleep(TRANSIENT_RETRY_S)
                    continue
                break
    raise RuntimeError(str(last) if last else "Aucun modèle n'a répondu.")


def _video_part(path: Path):
    client, gtypes = _gemini()
    size = path.stat().st_size
    if size <= INLINE_LIMIT:
        return gtypes.Part.from_bytes(data=path.read_bytes(), mime_type="video/mp4")
    uploaded = client.files.upload(file=str(path))
    for _ in range(60):
        state = str(getattr(getattr(uploaded, "state", None), "name", "") or "")
        if state == "ACTIVE":
            return uploaded
        if state == "FAILED":
            raise RuntimeError("TikTok : Gemini n'a pas pu traiter le fichier vidéo.")
        time.sleep(2)
        uploaded = client.files.get(name=uploaded.name)
    raise RuntimeError("TikTok : traitement du fichier vidéo trop long.")


def _draft_sample_times(duration: float) -> list[float]:
    """Instants qui donnent au modèle des preuves nettes, surtout au hook."""
    if duration <= 0:
        return [0.0, 0.4, 1.0, 2.0]
    anchors = [0.0, min(0.35, duration), min(0.8, duration), min(1.5, duration), min(3.0, duration)]
    if duration > 5:
        anchors.extend(duration * fraction for fraction in (0.12, 0.28, 0.45, 0.62, 0.8, 0.95))
    picked: list[float] = []
    for instant in sorted(anchors):
        instant = round(min(max(0.0, instant), duration), 2)
        if not picked or instant - picked[-1] >= 0.28:
            picked.append(instant)
    return picked[:11]


def _draft_frame_parts(path: Path, duration: float) -> list[Any]:
    """Extrait des images repères sans écrire de médias permanents sur disque."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return []
    try:
        _client, gtypes = _gemini()
    except Exception:
        return []
    parts: list[Any] = []
    for instant in _draft_sample_times(duration):
        try:
            result = subprocess.run(
                [ffmpeg, "-v", "error", "-ss", str(instant), "-i", str(path), "-frames:v", "1",
                 "-vf", "scale='min(720,iw)':-2", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
                capture_output=True, timeout=20, check=True,
            )
            if len(result.stdout) >= 1_000:
                parts.extend([
                    f"Image repère à {instant:.2f} s (à examiner comme une preuve visuelle) :",
                    gtypes.Part.from_bytes(data=result.stdout, mime_type="image/jpeg"),
                ])
        except Exception:
            # La vidéo entière reste analysable même si une vignette échoue.
            continue
    return parts


COACH_ROLE = (
    "Tu es un coach TikTok francophone de haut niveau, du calibre de Blow Up : direct, concret, "
    "sans langue de bois ni flatterie. Tu connais la mécanique de TikTok : premier lot de test de "
    "200 à 500 vues décidé sur le temps de visionnage et le taux de complétion, l'accroche décisive "
    "dans la première seconde (image + texte + son), le poids des partages, enregistrements et "
    "commentaires, le texte à l'écran et les sous-titres, le son tendance ou original, la boucle "
    "(fin qui renvoie au début), l'appel à l'action, la régularité et les séries.\n"
    "Le créateur publie PLUSIEURS genres de contenus sur le même compte : (a) la construction en "
    "public de son assistant IA ANO-GPT (démos, coulisses), (b) des vidéos générées par IA — "
    "sketches et mini-histoires avec des personnages (fruits animés à la Tentafruit, animaux, "
    "objets), humour, situations absurdes, dialogues doublés — et (c) d'autres formats. "
    "Commence TOUJOURS par identifier le genre de la vidéo que tu regardes et juge-la selon les "
    "codes de CE genre et de ses comptes de référence. Ne reproche JAMAIS à une vidéo de ne pas "
    "parler d'ANO-GPT ou de code : ce n'est pas une cause d'échec. Un sketch IA échoue pour des "
    "raisons de sketch (gag pas lisible en 1 s, chute trop lente, doublage plat, texte absent, "
    "personnage pas attachant, pas de boucle, son sans tendance, description sans accroche, "
    "hashtags hors niche), pas parce qu'il ne montre pas un assistant. Explique la VRAIE raison, "
    "vérifiable à l'écran, puis dis exactement comment faire pour que ça marche : ce qu'il faut "
    "couper, ajouter, réécrire, à quelle seconde. Tutoie-le. Ne récite pas de généralités : cite "
    "des moments précis (secondes) et des chiffres. Réponds en JSON strict."
)


def _video_facts(v: dict) -> str:
    return (
        f"Vues {v['plays']}, J'aime {v['likes']} ({v['like_rate'] * 100:.1f} %), commentaires "
        f"{v['comments']}, partages {v['shares']}, enregistrements {v.get('saves', 0)}, durée "
        f"{v.get('duration', 0)} s, publiée {v.get('posted_at')} (il y a {v['age_h']:.0f} h), "
        f"hashtags {', '.join('#' + t for t in v.get('hashtags') or []) or 'aucun'}, "
        f"son {'original' if v.get('original_sound') else (v.get('music') or 'inconnu')}. "
        f"Description : « {v.get('desc') or ''} »."
    )


def _summary_facts(summary: dict) -> str:
    if not summary:
        return "Pas d'historique de compte."
    return (
        f"Compte : {summary['count']} vidéos suivies, médiane {int(summary['median_plays'])} vues, "
        f"meilleure {summary['max_plays']} vues (« {summary['best'].get('desc', '')[:60]} »), "
        f"pire {summary['min_plays']} vues (« {summary['worst'].get('desc', '')[:60]} »), taux de J'aime "
        f"médian {summary['median_like_rate'] * 100:.1f} %, durée médiane {int(summary['median_duration'])} s, "
        f"{summary['posts_per_week']:.1f} publications par semaine, hashtags fréquents "
        f"{', '.join('#' + t for t, _ in summary['top_tags'][:6]) or 'aucun'}, "
        f"meilleures heures {', '.join(str(h) + ' h' for h in summary['best_hours']) or 'inconnues'}."
    )


def _download(handle: str, video_id: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    target = CACHE_DIR / f"{video_id}.mp4"
    if target.exists() and target.stat().st_size > 0:
        return target
    ytdlp = shutil.which("yt-dlp")
    if not ytdlp:
        raise RuntimeError("yt-dlp introuvable pour récupérer la vidéo.")
    subprocess.run(
        [ytdlp, "-q", "--no-warnings", "-f", "best[height<=720]/best",
         "-o", str(CACHE_DIR / f"{video_id}.%(ext)s"),
         f"https://www.tiktok.com/@{handle}/video/{video_id}"],
        check=True, timeout=120, capture_output=True,
    )
    if not target.exists():
        found = next(CACHE_DIR.glob(f"{video_id}.*"), None)
        if not found:
            raise RuntimeError("Téléchargement de la vidéo échoué.")
        found.rename(target)
    return target


def analyze_posted_video(v: dict, summary: dict, handle: str) -> dict:
    """Verdict complet : chiffres + règles + regard de Gemini sur la vidéo."""
    findings = heuristic_findings(v, summary)
    path = _download(handle, v["id"])
    prompt = f"""{COACH_ROLE}

Voici une vidéo déjà publiée sur TikTok par ce créateur, et ses résultats.
{_video_facts(v)}
{_summary_facts(summary)}
Ce que les chiffres suggèrent déjà :
- {chr(10).join('- ' + f for f in findings) if findings else 'rien de particulier'}

Regarde la vidéo. D'abord, dis de quel genre elle relève (sketch IA avec personnages, démo
ANO-GPT, autre) et quels sont les codes de ce genre. Ensuite explique POURQUOI elle a obtenu ce
résultat, en t'appuyant uniquement sur ce que tu vois et entends (accroche de la première seconde,
lisibilité du gag ou du sujet, rythme et chute, texte à l'écran / sous-titres, doublage et son,
personnages, fin/boucle, appel à l'action, description et hashtags par rapport à la niche). Le
manque de lien avec ANO-GPT n'est pas une cause recevable. Puis dis exactement quoi changer pour
que la PROCHAINE du même genre marche.

JSON attendu :
{{
  "genre": "genre identifié en quelques mots",
  "spoken": "verdict très bref : au plus 2 phrases et 55 mots, prêt à être dit à voix haute, tutoiement, sans markdown",
  "hook_score": 0-10,
  "retention_score": 0-10,
  "why": ["cause 1 précise", "cause 2", "cause 3"],
  "improvements": ["action concrète 1", "action 2", "action 3", "action 4"],
  "rewrite": {{"caption": "nouvelle description proposée", "hashtags": ["tag1", "tag2", "tag3", "tag4"]}},
  "repost_idea": "comment re-tourner ou re-monter cette idée pour qu'elle marche",
  "detailed_markdown": "audit approfondi en Markdown : observations écran/son avec timestamps, causes classées par impact, et corrections concrètes. 70 lignes max. Ne rien inventer : distingue observation et hypothèse."
}}"""
    data = _generate_json([_video_part(path), prompt], VIDEO_MODELS, low_res=True)
    data["findings"] = findings
    return data


def probe_file(path: Path) -> dict:
    """Faits techniques d'un fichier vidéo local (ffprobe)."""
    info: dict[str, Any] = {"size_mb": round(path.stat().st_size / 1e6, 1)}
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return info
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout
        data = json.loads(out)
        container = data.get("format", {})
        info["duration"] = round(float(container.get("duration") or 0), 1)
        if container.get("bit_rate"):
            info["bitrate_mbps"] = round(float(container["bit_rate"]) / 1_000_000, 2)
        info["container"] = str(container.get("format_name") or "")
        for st in data.get("streams") or []:
            if st.get("codec_type") == "video" and "width" not in info:
                info["width"], info["height"] = st.get("width"), st.get("height")
                info["video_codec"] = st.get("codec_name") or ""
                info["pixel_format"] = st.get("pix_fmt") or ""
                num, _, den = str(st.get("avg_frame_rate") or "0/1").partition("/")
                try:
                    info["fps"] = round(float(num) / float(den or 1), 1)
                except (ValueError, ZeroDivisionError):
                    pass
            if st.get("codec_type") == "audio":
                info["audio"] = True
                info["audio_codec"] = st.get("codec_name") or ""
                info["audio_rate"] = st.get("sample_rate") or ""
                info["audio_channels"] = st.get("channels") or ""
        info.setdefault("audio", False)
    except Exception as exc:
        info["probe_error"] = str(exc)[:120]
    return info


def file_findings(info: dict) -> list[str]:
    out = []
    w, h = info.get("width") or 0, info.get("height") or 0
    if w and h and w >= h:
        out.append("Format horizontal ou carré : TikTok c'est du vertical 9:16 plein écran, sinon "
                   "des bandes noires et une rétention qui s'effondre.")
    elif w and h and abs(w / h - 9 / 16) > 0.05:
        out.append(f"Ratio {w}x{h} : pas tout à fait 9:16, il y aura un recadrage ou des bandes.")
    if h and h < 1080 and w and w < 1080:
        out.append(f"Définition {w}x{h} : en dessous de 1080p, l'image paraît terne dans le flux.")
    d = info.get("duration") or 0
    if d > 60:
        out.append(f"{d:.0f} s : long pour un compte en construction ; garde 15–35 s ou fais une série.")
    elif 0 < d < 6:
        out.append(f"{d:.0f} s : très court, il faut que la boucle soit parfaite pour cumuler des revisionnages.")
    if info.get("audio") is False:
        out.append("Pas de piste audio : sans son ni voix, la vidéo est quasi invisible sur TikTok.")
    if info.get("size_mb", 0) > 280:
        out.append("Fichier très lourd : TikTok le recompressera, exporte en H.264 autour de 10–15 Mbit/s.")
    bitrate = info.get("bitrate_mbps") or 0
    if bitrate and bitrate < 3 and h >= 1080:
        out.append(f"Débit {bitrate:.1f} Mbit/s pour du 1080p : des artefacts peuvent apparaître après la recompression TikTok.")
    if info.get("video_codec") and info.get("video_codec") not in {"h264", "hevc", "av1"}:
        out.append(f"Codec {info['video_codec']} : fais un export H.264/AAC pour éviter un transcodage imprévisible.")
    return out


def analyze_draft(path: Path, summary: dict, note: str = "") -> dict:
    info = probe_file(path)
    findings = file_findings(info)
    prompt = f"""{COACH_ROLE}

Le créateur veut publier cette vidéo sur TikTok et te demande la meilleure façon de le faire.
Fichier : {info}.
{('Précision du créateur : ' + note) if note else ''}
{_summary_facts(summary)}
Points techniques déjà relevés : {'; '.join(findings) or 'aucun'}.

Identifie d'abord le genre (sketch IA à personnages, démo ANO-GPT, autre) et juge-la avec les
codes de CE genre. Visionne la vidéo entière et les images repères fournies. Tu es un analyste de
montage exigeant, pas un générateur de conseils TikTok génériques.

RÈGLES DE PREUVE ABSOLUES :
- Chaque défaut, qualité ou conseil de montage doit citer un timecode précis (ex. 00:01.2) ou une
  donnée locale de fichier. Si tu ne peux pas l'établir en regardant, écris « non vérifiable ».
- N'invente jamais de transcription, de texte à l'écran, de son tendance, de statistiques de
  rétention ou de comportement d'audience. Sépare clairement observation, hypothèse et test.
- Analyse au minimum : 0–1 s (arrêt du scroll), promesse et compréhension sans son, premier payoff,
  rythme/coupes/temps mort, lisibilité mobile du texte, cadrage et hiérarchie visuelle, voix/mixage,
  sous-titres, émotion ou curiosité, chute, boucle et CTA. Pour un sketch, juge spécifiquement la
  préparation du gag, l'escalade et la chute ; pour une démo, le problème, la preuve et le résultat.
- Classe les corrections : P0 bloque la publication, P1 augmente fortement les chances, P2 est une
  amélioration facultative. Donne le changement exact à faire, où et pourquoi il corrige ce moment.
- Produis trois ouvertures alternatives réellement filmables : plan exact à 0 s + texte écran +
  première phrase/son. Elles doivent être propres au contenu vu, jamais des slogans interchangeables.

Termine par un verdict franc : publie, publie après retouche légère, ou corrige d'abord.

JSON attendu :
{{
  "genre": "genre identifié en quelques mots",
  "verdict": "publie | publie après retouche légère | corrige d'abord",
  "spoken": "avis oral concis de 2 à 4 phrases, fondé sur 2 preuves avec timecodes, tutoiement, sans markdown, qui commence par le verdict",
  "hook_score": 0-10,
  "viral_potential": 0-10,
  "predicted_retention": "faible | moyenne | bonne",
  "scorecard": {{"hook": "x/10 + preuve", "clarity": "x/10 + preuve", "pacing": "x/10 + preuve", "audio": "x/10 + preuve ou non vérifiable", "payoff": "x/10 + preuve", "loop_cta": "x/10 + preuve"}},
  "timeline": [{{"time": "00:00.0–00:01.0", "observation": "fait visible/audible", "viewer_effect": "effet probable", "action": "correction exacte ou conserver", "evidence": "observation | hypothèse"}}],
  "strengths": ["force prouvée par un timecode"],
  "weaknesses": ["faiblesse précise avec timecode + impact"],
  "publish_blockers": ["P0 : blocage précis, ou [] si aucun"],
  "edits": ["P0/P1/P2 — timecode — modification de montage concrète — pourquoi"],
  "alternative_hooks": [{{"first_shot": "plan exact à 0 s", "on_screen_text": "texte", "first_line_or_sound": "phrase ou instruction son", "why": "raison propre à cette vidéo"}}],
  "caption": "description prête à coller, avec une accroche écrite",
  "hashtags": ["5 hashtags de niche pertinents, sans #"],
  "on_screen_text": "texte à afficher sur la première image (court)",
  "sound_advice": "son ou musique à utiliser et pourquoi (tendance, original, voix)",
  "pinned_comment": "premier commentaire à épingler pour lancer la discussion",
  "cover_advice": "quelle image de couverture et quel texte dessus",
  "detailed_markdown": "audit Markdown approfondi, 40 à 90 lignes : sections Observations prouvées, Hypothèses à tester, déroulé horodaté, décisions P0/P1/P2, 3 hooks, package de publication. Chaque ligne de conseil doit être rattachée à un timecode ou une donnée de fichier."
}}"""
    visual_evidence = _draft_frame_parts(path, float(info.get("duration") or 0))
    contents: list[Any] = [prompt, _video_part(path)]
    if visual_evidence:
        contents.extend(["Images repères HD : elles complètent la vidéo entière et servent à vérifier les détails.", *visual_evidence])
    data = _generate_json(contents, VIDEO_MODELS, low_res=False)
    data["findings"] = findings
    data["file"] = info
    data["posting_time"] = best_posting_advice(summary)
    return data


def analyze_account(cur: dict, items: list[dict], summary: dict) -> dict:
    rows = "\n".join(
        f"- {v['posted_at']} | {v['plays']} vues | {v['like_rate'] * 100:.1f} % J'aime | "
        f"{v['comments']} com. | {v['shares']} part. | {v.get('duration', 0)} s | "
        f"#{' #'.join(v.get('hashtags') or []) or '-'} | « {v.get('desc', '')[:70]} »"
        for v in items[:15]
    )
    prompt = f"""{COACH_ROLE}

Bilan du compte @{cur.get('handle')} ({cur.get('nickname')}) : {cur.get('followers')} abonnés,
{cur.get('likes')} J'aime, {cur.get('videos')} vidéos. {_summary_facts(summary)}
Dernières vidéos (plus récente en tête) :
{rows}

Dis-lui pourquoi il en est là, ce qui marche, ce qui plombe, et donne un plan sur 2 semaines.
JSON attendu :
{{
  "spoken": "bilan de 5 à 7 phrases prêt à être dit à voix haute, tutoiement, sans markdown",
  "working": ["ce qui marche 1", "2"],
  "blocking": ["ce qui bloque 1", "2", "3"],
  "plan": ["semaine 1 : ...", "semaine 2 : ..."],
  "video_ideas": ["idée 1 avec l'accroche exacte", "idée 2", "idée 3"],
  "detailed_markdown": "rapport Markdown, 20 lignes max"
}}"""
    return _generate_json([prompt], TEXT_MODELS)


# ════════════════════════════════════════════════════════════════════════════
# Présentation, annonces, tâches de fond
# ════════════════════════════════════════════════════════════════════════════

def _card(player: Any, title: str, body: str) -> None:
    try:
        show = getattr(player, "show_card", None)
        if callable(show):
            show(CARD_TYPE, title, body)
    except Exception:
        pass


def _announce(speak: Any, text: str) -> None:
    if not callable(speak) or not text:
        return
    try:
        speak("[COACH TIKTOK] Dis exactement ceci, sans rien ajouter et sans appeler le "
              f"moindre outil : {text}")
    except Exception:
        pass


def _remember_report(v: dict, summary: dict, data: dict) -> None:
    """Conserve le dernier diagnostic fini jusqu'à l'accord d'export."""
    global _LAST_REPORT_ID
    report_id = str(v.get("id") or "latest")
    with _REPORT_LOCK:
        _READY_REPORTS[report_id] = {
            "video": dict(v), "summary": dict(summary), "data": dict(data),
            "created_at": time.time(),
        }
        _LAST_REPORT_ID = report_id


def _markdown_safe_name(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9à-ÿÀ-Ÿ._-]+", "-", value or "video")
    return value.strip(".-")[:72] or "video"


def _bullet_section(title: str, values: Any) -> list[str]:
    rows = [str(value).strip() for value in (values or []) if str(value).strip()]
    return [f"## {title}", "", *(f"- {row}" for row in rows), ""] if rows else []


def _build_diagnosis_report(v: dict, summary: dict, data: dict, video_prompts: dict | None = None) -> str:
    """Rapport actionnable : chiffres mesurés, regard vidéo et protocole de test."""
    created = v.get("posted_at") or "date indisponible"
    desc = str(v.get("desc") or "Sans titre")
    lines = [
        f"# Diagnostic TikTok complet — {desc[:90]}",
        "",
        f"> Rapport généré le {datetime.now().strftime('%d/%m/%Y à %H:%M')} · Vidéo publiée {created}.",
        "> Les chiffres viennent du suivi public TikTok. Les constats visuels/sonores viennent du visionnage ; les causes non mesurables restent des hypothèses à tester.",
        "",
        "## Résumé exécutif",
        "",
        str(data.get("spoken") or "Diagnostic détaillé ci-dessous."),
        "",
        "## Données mesurées",
        "",
        "| Indicateur | Valeur |",
        "|---|---:|",
        f"| Vues | {v.get('plays', 0)} |",
        f"| J'aime | {v.get('likes', 0)} ({float(v.get('like_rate', 0)) * 100:.1f} %) |",
        f"| Commentaires | {v.get('comments', 0)} ({float(v.get('comment_rate', 0)) * 100:.2f} %) |",
        f"| Partages | {v.get('shares', 0)} ({float(v.get('share_rate', 0)) * 100:.2f} %) |",
        f"| Enregistrements | {v.get('saves', 0)} ({float(v.get('save_rate', 0)) * 100:.2f} %) |",
        f"| Durée | {v.get('duration', '?')} s |",
        f"| Publication | {created} |",
        f"| Médiane du compte | {int(summary.get('median_plays') or 0)} vues |",
        f"| Accroche / rétention estimées | {data.get('hook_score', '?')}/10 / {data.get('retention_score', '?')}/10 |",
        "",
        "## Ce que ces chiffres permettent réellement de conclure",
        "",
    ]
    findings = data.get("findings") or heuristic_findings(v, summary)
    lines += [f"- {finding}" for finding in findings] or ["- Données insuffisantes pour isoler une cause statistique."]
    lines += [""]
    lines += _bullet_section("Causes probables, classées par impact", data.get("why"))
    lines += _bullet_section("Corrections précises pour la prochaine version", data.get("improvements"))

    detailed = str(data.get("detailed_markdown") or "").strip()
    if detailed:
        lines += ["## Audit du contenu : image, rythme, texte et son", "", detailed, ""]

    rewrite = data.get("rewrite") or {}
    lines += ["## Pack de republication / nouvelle version", ""]
    if data.get("repost_idea"):
        lines += [f"**Angle à refaire :** {data['repost_idea']}", ""]
    if rewrite.get("caption"):
        lines += ["**Description proposée :**", "", str(rewrite["caption"]), ""]
    hashtags = [str(tag).lstrip("#") for tag in rewrite.get("hashtags") or [] if str(tag).strip()]
    if hashtags:
        lines += ["**Hashtags ciblés :** " + " ".join("#" + tag for tag in hashtags), ""]
    lines += _video_prompts_section(video_prompts or {})
    lines += [
        "## Plan de montage, seconde par seconde",
        "",
        "1. **0,0–1,0 s — promesse visible :** montrer le conflit, le résultat ou la phrase qui intrigue avant toute introduction ; ajouter une phrase lisible sans le son.",
        "2. **1–3 s — contexte minimal :** une seule information qui rend la promesse compréhensible. Couper les silences, logos et explications préparatoires.",
        "3. **3 s jusqu'à la chute — escalade :** changer de plan, de cadrage, de texte ou d'information dès que l'idée stagne ; chaque seconde doit faire avancer le gag ou la démonstration.",
        "4. **Chute + boucle :** livrer le payoff sans le diluer, puis terminer sur une image/phrase qui donne envie de revoir le début.",
        "5. **Dernière image :** poser une question spécifique liée à la vidéo pour obtenir des réponses, sans réclamer vaguement des abonnements.",
        "",
        "## Tests A/B à faire avant de conclure",
        "",
        "- Tester deux accroches réellement différentes pour la même idée, pas seulement une autre description.",
        "- Comparer une version courte (15–35 s) et la version actuelle si la rétention est le frein probable.",
        "- Tester un texte d'écran qui explique le contexte dès l'image 1 contre une ouverture sans texte.",
        "- Changer un seul paramètre par essai et noter vues, complétion, partages, enregistrements et commentaires après 24 h puis 72 h.",
        "",
        "## Décision de publication et suivi",
        "",
        f"- {best_posting_advice(summary)}",
        "- Ne supprime pas une vidéo encore jeune uniquement à cause des premières heures : observe-la au moins 24 h, sauf erreur technique ou problème de contenu.",
        "- Si les J'aime sont bons mais les vues faibles, priorise l'accroche et le ciblage. Si les vues existent mais pas les partages/commentaires, retravaille l'utilité, l'émotion ou le débat.",
        "",
        "## Limites du diagnostic",
        "",
        "TikTok ne fournit pas ici la courbe de rétention, les sources de trafic, le taux de complétion ni l'audience exacte. Ce rapport ne les invente pas : les recommandations sont donc des priorités de test, pas des certitudes absolues.",
        "",
    ]
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════════════
# Prompts vidéo prêts à coller (Grok Imagine 15 s · Gemini Veo 10 s)
# ════════════════════════════════════════════════════════════════════════════

VIDEO_PROMPT_TARGETS = (("Grok Imagine", 15), ("Gemini Veo", 10))

# Gabarit de référence : la STRUCTURE et le niveau de détail sont imposés, le
# contenu doit être entièrement nouveau et propre à la vidéo diagnostiquée.
_VIDEO_PROMPT_TEMPLATE = """FORMAT : Vertical 9:16, 15s, photoréaliste documentaire premium, qualité reportage cinéma/found-footage hybride, 24fps, lumière fluorescente froide, ambiance salle d'attente d'hôpital de nuit.

PERSONNAGE :
- LE SAGE (INFIRMIER RETRAITÉ) — 66 ans, blouse blanche usée encore portée par habitude, lunettes fines sur le bout du nez, cernes marqués mais regard vif, mains posées calmement sur les genoux, voix douce mais sans hésitation, calme presque troublant.
- LE JOURNALISTE (HORS-CHAMP) — jamais visible, seule sa main et son micro apparaissent dans le cadre.

DÉCOR : Couloir d'hôpital vide la nuit, néons froids qui bourdonnent légèrement, chaises en plastique alignées floues en arrière-plan, une porte entrouverte au loin qui laisse filtrer une lumière chaude, silence quasi total.

ANGLE CAMÉRA : Plan large qui se resserre très lentement en travelling avant sur tout le segment (dolly-in continu, aucune coupe), symétrie du couloir qui encadre le personnage au centre, légère distorsion optique douce en grand angle au début.

DÉCOUPAGE (timing très serré, rythme cut sec) :
0-1.5s — HOOK : plan large, il est assis seul au bout du couloir vide, immobile, silence total, on entend juste le bourdonnement des néons.
1.5-3.5s : "J'ai tenu la main de plus de mille personnes à leur dernier souffle."
3.5-5.5s : "Aucune n'a jamais parlé de son travail."
5.5-7.5s : il retire lentement ses lunettes, les plie, regarde enfin la caméra, le travelling se rapproche.
7.5-9.5s — TWIST : voix qui reste douce mais ferme : "Elles parlaient toutes des gens qu'elles n'avaient pas appelés assez."
9.5-11.5s : silence d'une seconde, puis : "Le temps qu'on croit avoir, on ne l'a jamais vraiment."
11.5-13s : il remet lentement ses lunettes, regard toujours fixe caméra, expression grave mais sereine.
13-15s : freeze frame progressif, le travelling s'arrête à mi-hauteur de son visage, la lumière chaude de la porte lointaine qui reste floue derrière lui.

AUDIO : Voix douce et posée avec de longs silences entre les phrases, bourdonnement continu et discret des néons, un bip lointain de moniteur médical à peine audible, aucune musique pendant les dialogues, ce même bip qui s'arrête net à 13s comme seul effet sonore de transition.

STYLE VISUEL : Rendu photoréaliste haut de gamme, peau avec texture réelle et imperfections naturelles (cernes profonds, peau fatiguée, rides d'expression), grain cinématographique léger type shot on iPhone, aucun style cartoon/3D/anime, pas de texte à l'écran, pas de morphing, cohérence du visage sur toute la durée du plan malgré le travelling avant continu.

HOOK COMMENTAIRE : Terminer sur le freeze frame — son regard calme après avoir vu tant de fins dire que la vraie urgence dans une vie, ce n'est jamais le travail qu'on n'a pas fini, c'est l'appel qu'on n'a jamais passé."""

_VIDEO_PROMPTS_INSTRUCTIONS = """Tu es directeur artistique et réalisateur de vidéos courtes générées par IA (Grok Imagine, Gemini Veo). À partir du DIAGNOSTIC ci-dessous d'une vidéo TikTok qui n'a pas marché, écris {count} prompts de génération vidéo pour la NOUVELLE version qui, elle, doit marcher.

RÈGLES ABSOLUES :
1. Chaque prompt suit EXACTEMENT la structure et le niveau de détail du GABARIT (mêmes rubriques dans le même ordre : FORMAT, PERSONNAGE, DÉCOR, ANGLE CAMÉRA, DÉCOUPAGE, AUDIO, STYLE VISUEL, HOOK COMMENTAIRE). Le gabarit n'est qu'un exemple de forme : n'en reprends NI le sujet, NI les personnages, NI le décor, NI les dialogues.
2. Le contenu vient de la vidéo diagnostiquée : même niche, même sujet, même intention, mais en appliquant les corrections du diagnostic (accroche visible dès 0-1 s, escalade, chute, boucle) et en ÉVITANT explicitement chaque défaut relevé.
3. DÉCOUPAGE : timing serré et continu, couvrant toute la durée cible ; la première tranche est le HOOK, une tranche est marquée TWIST, la dernière est la chute/boucle. Les tranches sont adaptées à la durée : {durations}.
4. Dialogues courts, en français, entre guillemets, dicibles dans le temps imparti. Pas de texte à l'écran demandé au générateur (le texte sera ajouté au montage).
5. Une seule prise, cohérence du personnage sur toute la durée, photoréaliste sauf si la niche impose un autre style.
6. Les prompts sont en français, prêts à coller tels quels, sans commentaire, sans markdown, sans titre.

Réponds en JSON strict :
{{
  "rationale": "2 phrases : ce que ces prompts corrigent par rapport à la vidéo diagnostiquée",
  "prompts": [
    {{"target": "Grok Imagine", "seconds": 15, "prompt": "..."}},
    {{"target": "Gemini Veo", "seconds": 10, "prompt": "..."}}
  ]
}}

=== GABARIT (forme uniquement) ===
{template}

=== DIAGNOSTIC DE LA VIDÉO ===
{diagnosis}
"""


def _diagnosis_digest(v: dict, summary: dict, data: dict) -> str:
    """Le diagnostic condensé pour le rédacteur de prompts."""
    parts = [
        f"Vidéo : « {str(v.get('desc') or 'Sans titre')[:200]} » — {v.get('duration', '?')} s, "
        f"{v.get('plays', 0)} vues (médiane du compte {int(summary.get('median_plays') or 0)}).",
    ]
    if data.get("spoken"):
        parts.append("Verdict : " + str(data["spoken"]))
    for title, key in (("Défauts relevés (À ÉVITER)", "why"), ("Corrections à appliquer", "improvements")):
        items = [str(x) for x in (data.get(key) or []) if str(x).strip()]
        if items:
            parts.append(title + " :\n" + "\n".join(f"- {x}" for x in items[:8]))
    findings = [str(x) for x in (data.get("findings") or []) if str(x).strip()]
    if findings:
        parts.append("Constats chiffrés :\n" + "\n".join(f"- {x}" for x in findings[:6]))
    if data.get("repost_idea"):
        parts.append("Angle retenu pour la nouvelle version : " + str(data["repost_idea"]))
    detailed = str(data.get("detailed_markdown") or "").strip()
    if detailed:
        parts.append("Audit détaillé :\n" + detailed[:2500])
    return "\n\n".join(parts)


def _prompt_targets_text() -> str:
    return " ; ".join(f"{name} = {secs} s" for name, secs in VIDEO_PROMPT_TARGETS)


def _prompts_json_fallback(prompt: str) -> dict:
    """Gemini indisponible (quota) : le modèle profond Azure rédige les prompts."""
    from core import azure_specialists
    from core.multimodal_vision import _parse_vision_json
    text = azure_specialists.text("deep", prompt, system="Réponds uniquement en JSON strict.")
    return _parse_vision_json(text)


def generate_video_prompts(v: dict, summary: dict, data: dict) -> dict:
    """Deux prompts vidéo (Grok 15 s, Gemini 10 s) adaptés au diagnostic.

    Renvoie ``{"rationale": str, "prompts": [{target, seconds, prompt}, …]}`` ;
    dict vide si aucun modèle n'a répondu — le rapport se fait alors sans.
    """
    prompt = _VIDEO_PROMPTS_INSTRUCTIONS.format(
        count=len(VIDEO_PROMPT_TARGETS), durations=_prompt_targets_text(),
        template=_VIDEO_PROMPT_TEMPLATE, diagnosis=_diagnosis_digest(v, summary, data),
    )
    result: dict = {}
    for engine in ("gemini", "azure"):
        try:
            result = _generate_json([prompt], TEXT_MODELS) if engine == "gemini" else _prompts_json_fallback(prompt)
        except Exception as exc:
            print(f"[Coach TikTok] prompts vidéo ({engine}) : {str(exc)[:160]}")
            continue
        if isinstance(result, dict) and result.get("prompts"):
            break
        result = {}
    prompts = []
    for item in (result.get("prompts") or []) if isinstance(result, dict) else []:
        if not isinstance(item, dict) or not str(item.get("prompt") or "").strip():
            continue
        prompts.append({
            "target": str(item.get("target") or "").strip(),
            "seconds": item.get("seconds"),
            "prompt": str(item["prompt"]).strip(),
        })
    if not prompts:
        return {}
    # Les cibles sont imposées : on réaligne nom et durée sur l'ordre attendu.
    for slot, (name, secs) in zip(prompts, VIDEO_PROMPT_TARGETS):
        slot["target"], slot["seconds"] = name, secs
    return {"rationale": str(result.get("rationale") or "").strip(), "prompts": prompts[:len(VIDEO_PROMPT_TARGETS)]}


_VIRAL_BRIEF_INSTRUCTIONS = """Tu es directeur artistique et réalisateur de vidéos courtes générées par IA (Sora). Ton client est un créateur TikTok dont voici le compte. Conçois LA vidéo de {seconds} secondes la plus susceptible de devenir virale pour CE compte : même niche, même public, mais en reprenant ce qui a marché et en évitant ce qui a échoué.
{idea}
RÈGLES ABSOLUES :
1. Le prompt suit EXACTEMENT la structure et le niveau de détail du GABARIT (rubriques FORMAT, PERSONNAGE, DÉCOR, ANGLE CAMÉRA, DÉCOUPAGE, AUDIO, STYLE VISUEL, HOOK COMMENTAIRE, dans cet ordre). Le gabarit n'est qu'un exemple de forme : n'en reprends ni le sujet, ni les personnages, ni le décor, ni les dialogues.
2. DÉCOUPAGE : tranches serrées couvrant exactement {seconds} s ; HOOK dès 0-1 s, un TWIST, une chute/boucle.
3. Une seule prise, cohérence du personnage, pas de texte à l'écran demandé au générateur (ajouté au montage). Dialogues courts en français entre guillemets.
4. Le prompt est en français, prêt à envoyer tel quel au générateur, sans commentaire ni markdown.

Réponds en JSON strict :
{{
  "concept": "2 phrases dites à voix haute au créateur : l'idée, et pourquoi elle peut percer sur son compte (tutoiement)",
  "caption": "description TikTok prête à coller avec 4-6 hashtags",
  "prompt": "..."
}}

=== GABARIT (forme uniquement) ===
{template}

=== LE COMPTE ===
{account}
"""


def _account_digest(cur: dict, items: list[dict], summary: dict) -> str:
    rows = "\n".join(
        f"- {v['plays']} vues | {v['like_rate'] * 100:.1f} % J'aime | {v['comments']} com. | "
        f"{v.get('duration', 0)} s | « {v.get('desc', '')[:80]} »"
        for v in sorted(items, key=lambda v: v.get('plays', 0), reverse=True)[:12]
    )
    parts = [
        f"@{cur.get('handle')} ({cur.get('nickname')}) : {cur.get('followers')} abonnés, "
        f"{cur.get('likes')} J'aime, {cur.get('videos')} vidéos. {_summary_facts(summary)}",
        "Vidéos, de la plus vue à la moins vue :\n" + rows,
    ]
    with _REPORT_LOCK:
        last = _READY_REPORTS.get(_LAST_REPORT_ID)
    if last:
        d = last.get("data") or {}
        if d.get("why"):
            parts.append("Défauts relevés sur la dernière vidéo diagnostiquée (À ÉVITER) :\n" +
                         "\n".join(f"- {x}" for x in d["why"][:6]))
        if d.get("improvements"):
            parts.append("Corrections recommandées :\n" + "\n".join(f"- {x}" for x in d["improvements"][:6]))
    return "\n\n".join(parts)


def viral_video_brief(query: str = "", seconds: int = 12) -> dict:
    """Concept + prompt Sora d'une vidéo virale pensée pour CE compte.

    Renvoie ``{"concept", "caption", "prompt", "seconds"}`` ; lève RuntimeError
    si aucun modèle ne répond ou si les statistiques manquent.
    """
    cur, items = dataset()
    if not items:
        raise RuntimeError("Je n'ai pas encore les statistiques de tes vidéos TikTok.")
    summary = account_summary(items)
    idea = f"Contrainte du créateur : « {query.strip()[:300]} »\n" if query.strip() else ""
    prompt = _VIRAL_BRIEF_INSTRUCTIONS.format(
        seconds=seconds, idea=idea, template=_VIDEO_PROMPT_TEMPLATE,
        account=_account_digest(cur, items, summary),
    )
    result: dict = {}
    for engine in ("gemini", "azure"):
        try:
            result = _generate_json([prompt], TEXT_MODELS) if engine == "gemini" else _prompts_json_fallback(prompt)
        except Exception as exc:
            print(f"[Coach TikTok] concept viral ({engine}) : {str(exc)[:160]}")
            continue
        if isinstance(result, dict) and str(result.get("prompt") or "").strip():
            break
        result = {}
    if not result:
        # La conception ne doit pas rendre la génération impossible quand un
        # modèle de rédaction est temporairement saturé. Les données du compte
        # suffisent à produire un brief sobre et explicitement perfectible ;
        # Sora reçoit ensuite ce prompt comme dans le chemin normal.
        return _local_viral_brief(query, seconds, summary)
    return {
        "concept": str(result.get("concept") or "").strip(),
        "caption": str(result.get("caption") or "").strip(),
        "prompt": str(result["prompt"]).strip(),
        "seconds": seconds,
    }


def _local_viral_brief(query: str, seconds: int, summary: dict) -> dict:
    """Brief déterministe de secours : jamais une promesse de viralité.

    Il évite qu'une panne ponctuelle Gemini/Azure-text bloque le vrai service
    demandé (la génération Sora). Le concept reprend une contrainte exprimée
    par l'utilisateur quand elle existe, sans prétendre avoir analysé une
    vidéo que les données ne décrivent pas.
    """
    subject = "une idée surprenante autour de ton univers"
    if query.strip():
        subject = query.strip()[:220]
    median = int(summary.get("median_plays") or 0)
    concept = f"Micro-histoire à chute sur {subject}"
    prompt = (
        f"FORMAT : Vertical 9:16, {seconds} secondes, vidéo TikTok.\n"
        f"PERSONNAGE : un créateur francophone naturel, expressif, face caméra.\n"
        f"DÉCOR : décor simple et lumineux lié à {subject}.\n"
        "ANGLE CAMÉRA : plan rapproché stable, regard caméra dès la première image.\n"
        f"DÉCOUPAGE : 0-1 s — HOOK : montrer immédiatement l'élément le plus surprenant de {subject}. "
        f"1-{max(2, seconds - 3)} s — démonstration très visuelle en une seule idée, sans introduction. "
        f"{max(2, seconds - 3)}-{max(3, seconds - 1)} s — TWIST : révélation qui inverse l'attente. "
        f"{max(3, seconds - 1)}-{seconds} s — CHUTE/BOUCLE : une question courte qui donne envie de revoir ou commenter.\n"
        "AUDIO : voix française claire, une phrase courte à la fois ; son original propre, sans musique qui couvre les mots.\n"
        "STYLE VISUEL : authentique, montage nerveux, pas de texte généré à l'écran, pas de logos, pas de morphing.\n"
        "HOOK COMMENTAIRE : « Tu l'aurais fait autrement ? »"
    )
    return {
        "concept": concept,
        "caption": (
            f"Je teste ça sur un petit compte (médiane actuelle : {median} vues). "
            "Tu valides la chute ?"
        ),
        "prompt": prompt,
        "seconds": seconds,
    }


def _video_prompts_section(generated: dict) -> list[str]:
    lines = ["## Prompts vidéo prêts à coller (Grok Imagine 15 s · Gemini Veo 10 s)", ""]
    if not generated:
        lines += ["_Les prompts n'ont pas pu être générés (modèle indisponible). Relance `tiktok_coach` action=report plus tard._", ""]
        return lines
    lines += [
        "Chaque prompt applique les corrections ci-dessus et évite les défauts relevés. Copier-coller tel quel dans l'outil indiqué ; le texte à l'écran se rajoute au montage.",
        "",
    ]
    if generated.get("rationale"):
        lines += [f"**Ce que ces prompts corrigent :** {generated['rationale']}", ""]
    for item in generated.get("prompts") or []:
        lines += [f"### {item['target']} — {item['seconds']} s", "", "```text", item["prompt"], "```", ""]
    return lines


def create_last_diagnosis_report(query: str = "") -> str:
    """Écrit le rapport seulement après la confirmation explicite de l'utilisateur."""
    with _REPORT_LOCK:
        report_id = _LAST_REPORT_ID
        if query:
            wanted = re.search(r"(?:/video/)?(\d{5,})", query)
            if wanted and wanted.group(1) in _READY_REPORTS:
                report_id = wanted.group(1)
        report = _READY_REPORTS.get(report_id)
    if not report:
        return ("Le diagnostic complet n'est pas encore prêt. Je te proposerai le rapport Markdown "
                "dès que le visionnage sera terminé.")
    video, summary, data = report["video"], report["summary"], report["data"]
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    path = REPORT_DIR / f"diagnostic-tiktok-{stamp}-{_markdown_safe_name(video.get('desc', 'video'))}.md"
    content = _build_diagnosis_report(video, summary, data, video_prompts=generate_video_prompts(video, summary, data))
    fd, tmp_name = tempfile.mkstemp(prefix=".diagnostic-", suffix=".md", dir=REPORT_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
        Path(tmp_name).replace(path)
    except Exception:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    global _LAST_REPORT_PATH
    _LAST_REPORT_PATH = path
    opened = _open_report(path)
    if opened:
        return (f"Rapport TikTok complet créé et OUVERT dans Markdown Studio : {path}. "
                "Il est déjà à l'écran : dis-le à l'utilisateur, n'appelle ni shell_exec ni open_app.")
    return (f"Rapport TikTok complet créé : {path}. Je n'ai pas pu l'ouvrir automatiquement ; "
            "si l'utilisateur veut le voir, appelle open_app avec ce chemin (jamais shell_exec).")


_LAST_REPORT_PATH: Path | None = None


def _open_report(path: Path) -> bool:
    """Ouvre le rapport dans Markdown Studio (ou l'application par défaut).

    L'utilisateur qui demande le rapport veut le lire, pas apprendre où il est
    rangé : sans ça le modèle improvisait un `code …` sous shell_exec, bloqué
    par la confirmation, et ne l'ouvrait jamais.
    """
    try:
        from actions.open_app import _launch_with_target
        return bool(_launch_with_target("", path))
    except Exception as exc:
        print(f"[TikTokCoach] ouverture du rapport impossible : {exc}")
        return False


def open_last_report() -> str:
    """« Ouvre le rapport » : le dernier rapport écrit, ou le plus récent du dossier."""
    path = _LAST_REPORT_PATH
    if path is None or not path.exists():
        candidates = sorted(REPORT_DIR.glob("diagnostic-tiktok-*.md"), key=lambda q: q.stat().st_mtime)
        path = candidates[-1] if candidates else None
    if path is None:
        return "Aucun rapport TikTok n'a encore été créé : demande d'abord le diagnostic d'une vidéo."
    if _open_report(path):
        return f"Rapport ouvert dans Markdown Studio : {path}. N'appelle aucun autre outil."
    return f"Je n'ai pas pu ouvrir {path} ; appelle open_app avec ce chemin (jamais shell_exec)."


def _diagnosis_markdown(v: dict, data: dict) -> str:
    lines = [f"**« {v.get('desc', '')[:60] or 'sans titre'} »** — {v['plays']} vues · "
             f"{v['likes']} ❤️ · {v['comments']} 💬 · {v['shares']} ↗️"]
    if "hook_score" in data:
        lines.append(f"Accroche **{data.get('hook_score')}/10** · Rétention **{data.get('retention_score', '?')}/10**")
    if data.get("why"):
        lines += ["", "**Pourquoi :**"] + [f"• {x}" for x in data["why"][:4]]
    if data.get("improvements"):
        lines += ["", "**À changer :**"] + [f"• {x}" for x in data["improvements"][:5]]
    rw = data.get("rewrite") or {}
    if rw.get("caption"):
        lines += ["", f"**Description proposée :** {rw['caption']}"]
    if rw.get("hashtags"):
        lines.append(" ".join("#" + str(t).lstrip("#") for t in rw["hashtags"][:6]))
    if data.get("repost_idea"):
        lines += ["", f"**À retenter :** {data['repost_idea']}"]
    return "\n".join(lines)


def _heuristic_spoken(v: dict, findings: list[str]) -> str:
    head = (f"Ta vidéo « {v.get('desc', '')[:40] or 'sans titre'} » est à {v['plays']} vues et "
            f"{v['likes']} J'aime. ")
    if not findings:
        return head + "Les chiffres sont dans la norme de ton compte ; je n'ai rien d'anormal à signaler."
    return head + " ".join(findings[:4])


def _run_background(key: str, work: Callable[[], None]) -> bool:
    with _ACTIVE_LOCK:
        if key in _ACTIVE and time.monotonic() - _ACTIVE[key] < 300:
            return False
        _ACTIVE[key] = time.monotonic()

    def _guarded() -> None:
        try:
            work()
        finally:
            with _ACTIVE_LOCK:
                _ACTIVE.pop(key, None)

    try:
        from core.thread_pool import get_thread_pool
        get_thread_pool().submit("network-heavy", _guarded, task_name=f"tiktok-coach-{key[:20]}",
                                 stall_timeout=400.0)
    except Exception:
        threading.Thread(target=_guarded, daemon=True, name="tiktok-coach").start()
    return True


def diagnose(query: str, player: Any, speak: Any) -> str:
    cur, items = dataset()
    v = pick_video(items, query)
    if v is None:
        return "Je ne trouve pas cette vidéo dans tes dernières publications. Dis-moi laquelle autrement."
    summary = account_summary(items)
    findings = heuristic_findings(v, summary)
    handle = cur.get("handle") or ""
    title = f"Diagnostic — {v.get('desc', '')[:36] or v['id']}"
    _card(player, title, _diagnosis_markdown(v, {"why": findings}) + "\n\n_Je regarde la vidéo…_")

    def _work() -> None:
        try:
            data = analyze_posted_video(v, summary, handle)
        except Exception as exc:
            print(f"[Coach TikTok] analyse vidéo impossible : {exc}")
            _card(player, title, _diagnosis_markdown(v, {"why": findings}))
            _announce(speak, "Je n'ai pas pu regarder la vidéo elle-même, mais d'après les chiffres : "
                             + _heuristic_spoken(v, findings))
            return
        _remember_report(v, summary, data)
        _card(player, title, _diagnosis_markdown(v, data))
        spoken = str(data.get("spoken") or _heuristic_spoken(v, findings)).strip()
        _announce(speak, spoken + " Veux-tu que je crée le rapport Markdown complet avec le diagnostic, "
                  "les corrections et le plan d'action ?")

    if not _run_background(f"diag:{v['id']}", _work):
        return "J'analyse déjà cette vidéo, le verdict arrive."
    return (f"Je regarde ta vidéo « {v.get('desc', '')[:40] or 'sans titre'} » et ses chiffres. "
            "Je te donne un verdict bref après le visionnage, puis je te proposerai le rapport Markdown complet.")


def review_account(player: Any) -> str:
    cur, items = dataset()
    if not items:
        return "Je n'ai pas encore les statistiques de tes vidéos ; réessaie dans un instant."
    summary = account_summary(items)
    try:
        data = analyze_account(cur, items, summary)
    except Exception as exc:
        print(f"[Coach TikTok] bilan Gemini impossible : {exc}")
        best, worst = summary["best"], summary["worst"]
        text = (f"Tu es à {cur['followers']} abonnés avec {summary['count']} vidéos suivies, médiane "
                f"{int(summary['median_plays'])} vues. Ta meilleure : « {best.get('desc', '')[:40]} » à "
                f"{best['plays']} vues ; la moins bonne : « {worst.get('desc', '')[:40]} » à {worst['plays']}. "
                f"Tu publies {summary['posts_per_week']:.1f} fois par semaine. " + best_posting_advice(summary))
        _card(player, f"Bilan TikTok @{cur['handle']}", text)
        return text
    body = [f"**{cur['followers']} abonnés · {cur['likes']} ❤️ · médiane {int(summary['median_plays'])} vues**"]
    for label, key in (("Ce qui marche", "working"), ("Ce qui bloque", "blocking"),
                       ("Plan", "plan"), ("Idées", "video_ideas")):
        if data.get(key):
            body += ["", f"**{label} :**"] + [f"• {x}" for x in data[key][:5]]
    _card(player, f"Bilan TikTok @{cur['handle']}", "\n".join(body))
    return str(data.get("spoken") or "Bilan affiché à l'écran.")


_VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}


def find_video_file(query: str) -> Optional[Path]:
    """Chemin donné, sinon le fichier vidéo le plus récent (ou celui dont le
    nom contient les mots) dans les dossiers habituels."""
    q = str(query or "").strip()
    if q:
        p = Path(q).expanduser()
        if p.exists() and p.is_file():
            return p
    # Le dossier TikTok du créateur passe en premier : s'il contient des
    # vidéos, on ne va pas en chercher d'autres dans Téléchargements.
    candidates = pending_videos()
    if not candidates:
        roots = [Path.home() / d for d in ("Vidéos", "Videos", "Téléchargements", "Downloads", "Bureau", "Desktop")]
        roots.append(Path.home() / "Vidéos" / "ANO-GPT")
        for root in roots:
            if root.is_dir():
                for p in root.rglob("*"):
                    if p.suffix.lower() in _VIDEO_EXT and p.is_file() and not p.name.startswith("."):
                        candidates.append(p)
    if not candidates:
        return None
    words = [w for w in re.findall(r"[a-zà-ÿ0-9]{3,}", q.lower()) if w not in {"vidéo", "video", "fichier"}]
    if words:
        scored = [(sum(1 for w in words if w in p.name.lower()), p.stat().st_mtime, p) for p in candidates]
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        if scored[0][0] > 0:
            return scored[0][2]
    return max(candidates, key=lambda p: p.stat().st_mtime)


def pending_videos() -> list[Path]:
    """Vidéos du dossier TikTok, de la plus récente à la plus ancienne."""
    if not TIKTOK_DIR.is_dir():
        return []
    found = [p for p in TIKTOK_DIR.rglob("*")
             if p.suffix.lower() in _VIDEO_EXT and p.is_file() and not p.name.startswith(".")]
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return found


def list_pending(player: Any) -> str:
    """Présente les vidéos prêtes à publier et demande laquelle analyser."""
    videos = pending_videos()
    if not videos:
        return (f"Le dossier {TIKTOK_DIR} est vide. Dépose-y la vidéo que tu veux publier et "
                "dis-moi « analyse-la ».")
    lines = []
    for i, p in enumerate(videos[:12], 1):
        info = probe_file(p)
        age_h = (time.time() - p.stat().st_mtime) / 3600
        when = f"il y a {age_h:.0f} h" if age_h < 48 else datetime.fromtimestamp(p.stat().st_mtime).strftime("%d/%m")
        lines.append(f"{i}. **{p.name}** · {info.get('duration', '?')} s · "
                     f"{info.get('width', '?')}x{info.get('height', '?')} · {when}")
    _card(player, f"Vidéos prêtes à publier ({len(videos)})",
          "\n".join(lines) + "\n\n_Dis-moi laquelle tu veux poster : je la regarde et je te "
          "donne la recette complète._")
    names = ", ".join(f"{i}. {p.stem[:30]}" for i, p in enumerate(videos[:5], 1))
    more = f" et {len(videos) - 5} autre(s)" if len(videos) > 5 else ""
    return (f"Tu as {len(videos)} vidéo(s) dans le dossier TikTok : {names}{more}. "
            "Laquelle es-tu prêt à poster ? Je la visionne et je te dis exactement comment la faire marcher.")


def _pick_pending(query: str) -> Optional[Path]:
    """« la 2 », « la deuxième », « la dernière », ou des mots du nom."""
    q = str(query or "").strip().lower()
    videos = pending_videos()
    if not videos:
        return None
    ordinals = {"première": 1, "premiere": 1, "deuxième": 2, "deuxieme": 2, "seconde": 2,
                "troisième": 3, "troisieme": 3, "quatrième": 4, "quatrieme": 4, "cinquième": 5}
    m = re.search(r"\b(\d{1,2})\b", q)
    idx = int(m.group(1)) if m else next((n for w, n in ordinals.items() if w in q), 0)
    if 1 <= idx <= len(videos):
        return videos[idx - 1]
    if "dernière" in q or "derniere" in q or "récente" in q or "recente" in q:
        return videos[0]
    return None


def review_draft(query: str, note: str, player: Any, speak: Any) -> str:
    path = _pick_pending(query) or find_video_file(query)
    if path is None:
        return (f"Je ne trouve pas de fichier vidéo. Mets-la dans {TIKTOK_DIR} ou dis-moi son chemin.")
    try:
        _cur, items = dataset()
        summary = account_summary(items)
    except Exception:
        summary = {}
    info = probe_file(path)
    findings = file_findings(info)
    title = f"Avant publication — {path.name[:36]}"
    _card(player, title, f"**{path.name}** · {info.get('duration', '?')} s · "
                         f"{info.get('width', '?')}x{info.get('height', '?')}\n\n"
                         + ("\n".join('• ' + f for f in findings) or "Format correct.")
                         + "\n\n_Je regarde la vidéo…_")

    def _work() -> None:
        try:
            data = analyze_draft(path, summary, note)
        except Exception as exc:
            print(f"[Coach TikTok] analyse du brouillon impossible : {exc}")
            _announce(speak, "Je n'ai pas pu visionner le fichier. Côté technique : "
                             + (" ".join(findings) if findings else "le format est bon.")
                             + " " + best_posting_advice(summary))
            return
        lines = [f"**{path.name}** — {data.get('genre', '')}",
                 f"Verdict : **{data.get('verdict', '?')}** · accroche **{data.get('hook_score', '?')}/10** · "
                 f"potentiel **{data.get('viral_potential', '?')}/10** · rétention prévue "
                 f"**{data.get('predicted_retention', '?')}**"]
        for label, key in (("Forces", "strengths"), ("Faiblesses", "weaknesses"), ("Montage", "edits")):
            if data.get(key):
                lines += ["", f"**{label} :**"] + [f"• {x}" for x in data[key][:4]]
        if data.get("publish_blockers"):
            lines += ["", "**À corriger avant publication (P0) :**"] + [
                f"• {x}" for x in data["publish_blockers"][:4]
            ]
        if data.get("timeline"):
            lines += ["", "**Déroulé vérifié :**"]
            for moment in data["timeline"][:7]:
                if isinstance(moment, dict):
                    lines.append(
                        f"• **{moment.get('time', '?')}** — {moment.get('observation', '')} "
                        f"→ {moment.get('action', '')}"
                    )
        if data.get("alternative_hooks"):
            lines += ["", "**3 accroches à tester :**"]
            for hook in data["alternative_hooks"][:3]:
                if isinstance(hook, dict):
                    lines.append(
                        f"• **Plan 0 s :** {hook.get('first_shot', '')} — "
                        f"« {hook.get('on_screen_text', '')} »"
                    )
        if data.get("caption"):
            lines += ["", f"**Description :** {data['caption']}"]
        if data.get("hashtags"):
            lines.append(" ".join("#" + str(t).lstrip("#") for t in data["hashtags"][:6]))
        if data.get("on_screen_text"):
            lines += ["", f"**Texte 1re image :** {data['on_screen_text']}"]
        if data.get("sound_advice"):
            lines.append(f"**Son :** {data['sound_advice']}")
        if data.get("cover_advice"):
            lines.append(f"**Couverture :** {data['cover_advice']}")
        if data.get("pinned_comment"):
            lines.append(f"**Commentaire à épingler :** {data['pinned_comment']}")
        if data.get("detailed_markdown"):
            lines += ["", "**Audit détaillé :**", str(data["detailed_markdown"]).strip()]
        lines += ["", f"⏰ {data.get('posting_time', '')}"]
        _card(player, title, "\n".join(lines))
        spoken = str(data.get("spoken") or "")
        if data.get("caption"):
            spoken += f" Pour la description, je te propose : {data['caption']}"
        spoken += " " + str(data.get("posting_time") or "")
        _announce(speak, spoken)

    if not _run_background(f"draft:{path}", _work):
        return "J'analyse déjà ce fichier, l'avis arrive."
    head = f"Je visionne {path.name} ({info.get('duration', '?')} secondes). "
    if findings:
        head += "Déjà, côté format : " + findings[0] + " "
    return head + "Je te donne mon avis complet et la description à mettre dans une minute."


# ════════════════════════════════════════════════════════════════════════════
# Outil
# ════════════════════════════════════════════════════════════════════════════

def tiktok_coach(parameters: dict | None = None, player: Any = None,
                 speak: Callable[[str], None] | None = None, **_kw) -> str:
    p = parameters or {}
    action = str(p.get("action") or "diagnose").strip().lower()
    query = str(p.get("query") or p.get("video") or "")
    try:
        if action in {"report", "rapport", "export", "markdown", "md"}:
            return create_last_diagnosis_report(query)
        if action in {"open_report", "open", "ouvre", "show_report", "ouvrir"}:
            return open_last_report()
        if action in {"diagnose", "why", "pourquoi", "analyse", "analyze", "video"}:
            return diagnose(query, player, speak)
        if action in {"review", "account", "bilan", "compte", "plan"}:
            return review_account(player)
        if action in {"list", "pending", "folder", "dossier", "liste", "choose", "which"}:
            return list_pending(player)
        if action in {"draft", "before_post", "pre_post", "file", "fichier", "avant"}:
            return review_draft(str(p.get("path") or query), str(p.get("note") or ""), player, speak)
        if action in {"best_time", "when", "quand", "heure"}:
            _cur, items = dataset()
            return best_posting_advice(account_summary(items))
    except LookupError as exc:
        return str(exc)
    except Exception as exc:
        return f"Le coach TikTok n'a pas pu répondre : {exc}"
    return ("Actions du coach : diagnose (pourquoi une vidéo), report (export Markdown après accord), "
            "review (bilan du compte), list (vidéos prêtes à publier), draft, best_time.")
