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
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from actions import tiktok_tracker as tt

CARD_TYPE = "tiktok"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "anogpt" / "tiktok"
INLINE_LIMIT = 19 * 1024 * 1024        # au-delà, passage par l'API Files
# Flash 3 en premier : c'est lui qui a le quota gratuit et qui digère une
# vidéo de 90 s en moins d'une minute ; les alias « latest » servent de repli.
VIDEO_MODELS = ("gemini-3-flash-preview", "gemini-flash-latest", "gemini-flash-lite-latest")
TEXT_MODELS = ("gemini-3-flash-preview", "gemini-flash-latest", "gemini-flash-lite-latest")
TRANSIENT_RETRY_S = 8.0
_ACTIVE_LOCK = threading.Lock()
_ACTIVE: dict[str, float] = {}       # analyses en cours, par clé


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
    plays, likes = v["plays"], v["likes"]
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


COACH_ROLE = (
    "Tu es un coach TikTok francophone de haut niveau, du calibre de Blow Up : direct, concret, "
    "sans langue de bois ni flatterie. Tu connais la mécanique de TikTok : premier lot de test de "
    "200 à 500 vues décidé sur le temps de visionnage et le taux de complétion, l'accroche décisive "
    "dans les 1 à 2 premières secondes, le poids des partages, enregistrements et commentaires, la "
    "cohérence de niche, le texte à l'écran, le son, la boucle (fin qui renvoie au début), l'appel à "
    "l'action, la régularité de publication. Tu parles à un créateur qui construit un assistant IA "
    "(ANO-GPT) en public. Tutoie-le. Ne récite pas de généralités : cite des moments précis de la "
    "vidéo (secondes) et des chiffres. Réponds en JSON strict."
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

Regarde la vidéo et explique POURQUOI elle a obtenu ce résultat, en t'appuyant sur ce que tu vois
et entends (accroche des 2 premières secondes, rythme, texte à l'écran, son, clarté du sujet,
fin/boucle, appel à l'action). Puis dis exactement quoi changer.

JSON attendu :
{{
  "spoken": "verdict de 4 à 6 phrases, prêt à être dit à voix haute, tutoiement, sans markdown",
  "hook_score": 0-10,
  "retention_score": 0-10,
  "why": ["cause 1 précise", "cause 2", "cause 3"],
  "improvements": ["action concrète 1", "action 2", "action 3", "action 4"],
  "rewrite": {{"caption": "nouvelle description proposée", "hashtags": ["tag1", "tag2", "tag3", "tag4"]}},
  "repost_idea": "comment re-tourner ou re-monter cette idée pour qu'elle marche",
  "detailed_markdown": "rapport structuré en Markdown (titres, puces), 15 lignes max"
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
        info["duration"] = round(float(data.get("format", {}).get("duration") or 0), 1)
        for st in data.get("streams") or []:
            if st.get("codec_type") == "video" and "width" not in info:
                info["width"], info["height"] = st.get("width"), st.get("height")
                num, _, den = str(st.get("avg_frame_rate") or "0/1").partition("/")
                try:
                    info["fps"] = round(float(num) / float(den or 1), 1)
                except (ValueError, ZeroDivisionError):
                    pass
            if st.get("codec_type") == "audio":
                info["audio"] = True
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

Regarde-la comme le ferait un spectateur qui scrolle : est-ce que tu t'arrêtes dans la première
seconde ? où décroches-tu ? Sois précis (secondes). Puis donne la recette exacte pour la publier.

JSON attendu :
{{
  "spoken": "avis de 4 à 6 phrases prêt à être dit à voix haute, tutoiement, sans markdown",
  "hook_score": 0-10,
  "predicted_retention": "faible | moyenne | bonne",
  "strengths": ["force 1", "force 2"],
  "weaknesses": ["faiblesse précise avec le moment (s)", "..."],
  "edits": ["modification de montage concrète 1", "2", "3"],
  "caption": "description prête à coller, avec une accroche écrite",
  "hashtags": ["5 hashtags de niche pertinents, sans #"],
  "on_screen_text": "texte à afficher sur la première image (court)",
  "cover_advice": "quelle image de couverture et quel texte dessus",
  "detailed_markdown": "rapport Markdown, 15 lignes max"
}}"""
    data = _generate_json([_video_part(path), prompt], VIDEO_MODELS, low_res=True)
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
        _card(player, title, _diagnosis_markdown(v, data))
        _announce(speak, str(data.get("spoken") or _heuristic_spoken(v, findings)))

    if not _run_background(f"diag:{v['id']}", _work):
        return "J'analyse déjà cette vidéo, le verdict arrive."
    return (f"Je regarde ta vidéo « {v.get('desc', '')[:40] or 'sans titre'} » ({v['plays']} vues, "
            f"{v['likes']} J'aime). Premier constat sur les chiffres : "
            + (findings[0] if findings else "rien d'anormal.")
            + " Je te donne le verdict complet dans une minute, après l'avoir visionnée.")


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
    roots = [Path.home() / d for d in ("Vidéos", "Videos", "Téléchargements", "Downloads", "Bureau", "Desktop")]
    roots.append(Path.home() / "Vidéos" / "ANO-GPT")
    candidates: list[Path] = []
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


def review_draft(query: str, note: str, player: Any, speak: Any) -> str:
    path = find_video_file(query)
    if path is None:
        return "Je ne trouve pas de fichier vidéo. Dis-moi son chemin ou mets-le dans Vidéos."
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
        lines = [f"**{path.name}** — accroche **{data.get('hook_score', '?')}/10**, rétention prévue "
                 f"**{data.get('predicted_retention', '?')}**"]
        for label, key in (("Forces", "strengths"), ("Faiblesses", "weaknesses"), ("Montage", "edits")):
            if data.get(key):
                lines += ["", f"**{label} :**"] + [f"• {x}" for x in data[key][:4]]
        if data.get("caption"):
            lines += ["", f"**Description :** {data['caption']}"]
        if data.get("hashtags"):
            lines.append(" ".join("#" + str(t).lstrip("#") for t in data["hashtags"][:6]))
        if data.get("on_screen_text"):
            lines += ["", f"**Texte 1re image :** {data['on_screen_text']}"]
        if data.get("cover_advice"):
            lines.append(f"**Couverture :** {data['cover_advice']}")
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
        if action in {"diagnose", "why", "pourquoi", "analyse", "analyze", "video"}:
            return diagnose(query, player, speak)
        if action in {"review", "account", "bilan", "compte", "plan"}:
            return review_account(player)
        if action in {"draft", "before_post", "pre_post", "file", "fichier", "avant"}:
            return review_draft(str(p.get("path") or query), str(p.get("note") or ""), player, speak)
        if action in {"best_time", "when", "quand", "heure"}:
            _cur, items = dataset()
            return best_posting_advice(account_summary(items))
    except LookupError as exc:
        return str(exc)
    except Exception as exc:
        return f"Le coach TikTok n'a pas pu répondre : {exc}"
    return "Actions du coach : diagnose (pourquoi une vidéo), review (bilan du compte), draft (vidéo à publier), best_time."
