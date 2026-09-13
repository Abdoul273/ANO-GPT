#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tiktok_tracker.py — Suivi « à la Blow » d'un compte TikTok.

TikTok n'offre aucune API publique pour les statistiques d'un compte sans
revue d'application. Comme Blow, on lit donc la page publique du profil à
intervalle régulier et on compare les compteurs d'une lecture à l'autre.

Un simple `requests` est refusé par le pare-feu anti-robot (page vide de
1,4 Ko) : la page doit être rendue par un vrai Chrome. Playwright pilote un
Chrome headless. Constats mesurés sur cette machine :

- la page profil embarque un JSON `__UNIVERSAL_DATA_FOR_REHYDRATION__` avec
  les compteurs du compte (abonnés, abonnements, j'aime, vidéos) ;
- la liste des vidéos et leurs vues/likes/commentaires/partages arrive par
  l'appel réseau `/api/post/item_list/`, qu'on intercepte ;
- **un contexte navigateur neuf à chaque lecture est indispensable** : un
  profil persistant ou un rechargement de la même page reçoit une réponse
  `item_list` vide dès la deuxième fois (cookies marqués).

Une lecture coûte 8 à 15 s de Chrome. Sur cette machine modeste, la veille
lit donc toutes les deux minutes par défaut, jamais en dessous de 45 s.

Fichier d'état : $XDG_CONFIG_HOME/jarvis/tiktok_tracker.json
"""
from __future__ import annotations

import json
import os
import random
import shutil
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

MIN_INTERVAL_S = 45
DEFAULT_INTERVAL_S = 120
MAX_INTERVAL_S = 3600
FETCH_TIMEOUT_S = 45.0
HISTORY_LIMIT = 720          # ~24 h à 2 min
CARD_TYPE = "tiktok"
MILESTONES = (10, 25, 50, 100, 250, 500, 1_000, 2_500, 5_000, 10_000, 25_000,
              50_000, 100_000, 250_000, 500_000, 1_000_000)

_LOCK = threading.RLock()
_FETCH_LOCK = threading.Lock()   # jamais deux Chrome en même temps


def _state_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "jarvis" / "tiktok_tracker.json"


def _default_state() -> dict[str, Any]:
    return {
        "handle": "",
        "enabled": False,
        "interval_s": DEFAULT_INTERVAL_S,
        "snapshots": [],
        "day_baseline": None,
        "last_error": "",
    }


def load_state() -> dict[str, Any]:
    with _LOCK:
        try:
            data = json.loads(_state_path().read_text(encoding="utf-8"))
        except Exception:
            data = {}
        state = _default_state()
        if isinstance(data, dict):
            state.update(data)
        state["snapshots"] = list(state.get("snapshots") or [])[-HISTORY_LIMIT:]
        return state


def save_state(state: dict[str, Any]) -> None:
    with _LOCK:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        state = dict(state)
        state["snapshots"] = list(state.get("snapshots") or [])[-HISTORY_LIMIT:]
        fd, tmp = tempfile.mkstemp(prefix=".tiktok-", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)


def normalize_handle(text: str) -> str:
    text = str(text or "").strip()
    if "tiktok.com" in text:
        text = text.split("tiktok.com", 1)[1]
        text = text.split("?", 1)[0].strip("/").split("/", 1)[0]
    text = text.lstrip("@").strip().lower()
    return "".join(ch for ch in text if ch.isalnum() or ch in "._")


# ════════════════════════════════════════════════════════════════════════════
# Lecture de la page profil
# ════════════════════════════════════════════════════════════════════════════

_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")


def _browser_executable() -> Optional[str]:
    override = os.environ.get("ANO_TIKTOK_BROWSER")
    if override and Path(override).exists():
        return override
    for name in ("google-chrome-stable", "google-chrome", "chromium",
                 "chromium-browser", "brave", "brave-browser", "microsoft-edge"):
        found = shutil.which(name)
        if found:
            return found
    return None   # Playwright utilisera son Chromium embarqué s'il existe


def _to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def parse_profile_json(raw: str) -> dict[str, Any]:
    """Extrait le compte depuis le JSON de réhydratation de la page."""
    data = json.loads(raw) if raw else {}
    scope = data.get("__DEFAULT_SCOPE__", {}) if isinstance(data, dict) else {}
    detail = scope.get("webapp.user-detail", {}) or {}
    status = detail.get("statusCode", 0)
    if status not in (0, "0", None):
        msg = str(detail.get("statusMsg") or "")
        if status in (10202, "10202") or "not exist" in msg.lower():
            raise LookupError("Ce compte TikTok n'existe pas.")
        raise RuntimeError(f"TikTok a refusé la page (code {status}) {msg}".strip())
    info = detail.get("userInfo", {}) or {}
    stats = info.get("stats", {}) or {}
    user = info.get("user", {}) or {}
    if not user.get("uniqueId"):
        raise RuntimeError("Page TikTok sans données de profil (anti-robot ?).")
    return {
        "handle": str(user.get("uniqueId") or ""),
        "nickname": str(user.get("nickname") or ""),
        "verified": bool(user.get("verified")),
        "followers": _to_int(stats.get("followerCount")),
        "following": _to_int(stats.get("followingCount")),
        "likes": _to_int(stats.get("heartCount") or stats.get("heart")),
        "videos": _to_int(stats.get("videoCount")),
    }


def parse_item_list(payload: Any) -> list[dict[str, Any]]:
    """Réduit la réponse `item_list` aux chiffres utiles, plus récente en tête."""
    items = []
    if not isinstance(payload, dict):
        return items
    for it in payload.get("itemList") or []:
        if not isinstance(it, dict):
            continue
        st = it.get("stats") or {}
        items.append({
            "id": str(it.get("id") or ""),
            "desc": " ".join(str(it.get("desc") or "").split())[:80],
            "created": _to_int(it.get("createTime")),
            "plays": _to_int(st.get("playCount")),
            "likes": _to_int(st.get("diggCount")),
            "comments": _to_int(st.get("commentCount")),
            "shares": _to_int(st.get("shareCount")),
        })
    items.sort(key=lambda v: v["created"], reverse=True)
    return items


def fetch_snapshot(handle: str, timeout_s: float = FETCH_TIMEOUT_S) -> dict[str, Any]:
    """Lit le profil public dans un Chrome headless neuf. Bloquant : à appeler
    hors du thread audio (``asyncio.to_thread``)."""
    handle = normalize_handle(handle)
    if not handle:
        raise ValueError("Aucun compte TikTok configuré.")
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # pragma: no cover - dépend de l'installation
        raise RuntimeError(
            "Playwright manquant : pip install playwright && playwright install chromium"
        ) from exc

    deadline = time.monotonic() + timeout_s
    item_payloads: list[Any] = []

    def _on_response(resp) -> None:
        if "/api/post/item_list" not in resp.url:
            return
        try:
            item_payloads.append(resp.json())
        except Exception:
            pass   # corps vide = anti-robot, on garde au moins le compte

    with _FETCH_LOCK, sync_playwright() as pw:
        launch: dict[str, Any] = {
            "headless": True,
            "args": [
                "--headless=new", "--no-sandbox", "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
                "--renderer-process-limit=2", "--mute-audio",
            ],
        }
        exe = _browser_executable()
        if exe:
            launch["executable_path"] = exe
        browser = pw.chromium.launch(**launch)
        try:
            ctx = browser.new_context(
                user_agent=_UA, locale="fr-FR",
                viewport={"width": 1280, "height": 900},
            )
            page = ctx.new_page()
            # Ni images, ni vidéos, ni polices : seuls le HTML et les appels
            # JSON nous intéressent, et la machine a deux cœurs.
            page.route(
                "**/*",
                lambda route: route.abort()
                if route.request.resource_type in ("image", "media", "font")
                else route.continue_(),
            )
            page.on("response", _on_response)
            page.goto(f"https://www.tiktok.com/@{handle}",
                      wait_until="domcontentloaded",
                      timeout=int(max(5.0, deadline - time.monotonic()) * 1000))
            raw = ""
            # La liste des vidéos suit le HTML de quelques secondes.
            for _ in range(40):
                if not raw:
                    try:
                        raw = page.evaluate(
                            "() => (document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__')"
                            "||{}).textContent || ''"
                        )
                    except Exception:
                        raw = ""   # la page se re-navigue parfois juste après le DOM
                if raw and item_payloads:
                    break
                if time.monotonic() >= deadline:
                    break
                page.wait_for_timeout(250)
            account = parse_profile_json(raw)
        finally:
            browser.close()

    videos: list[dict[str, Any]] = []
    for payload in item_payloads:
        videos = parse_item_list(payload)
        if videos:
            break
    account["items"] = videos
    account["items_available"] = bool(videos)
    account["ts"] = time.time()
    return account


# ════════════════════════════════════════════════════════════════════════════
# Comparaison et présentation
# ════════════════════════════════════════════════════════════════════════════

def _fmt(n: int) -> str:
    n = int(n or 0)
    if abs(n) >= 1_000_000:
        return f"{n / 1_000_000:.1f} M".replace(".0 ", " ")
    if abs(n) >= 10_000:
        return f"{n / 1000:.1f} k".replace(".0 ", " ")
    return f"{n:,}".replace(",", " ")


def _signed(n: int) -> str:
    n = int(n or 0)
    return f"+{_fmt(n)}" if n > 0 else (f"−{_fmt(-n)}" if n < 0 else "±0")


def deltas(prev: Optional[dict], cur: dict) -> dict[str, int]:
    if not prev:
        return {"followers": 0, "likes": 0, "videos": 0, "plays": 0}
    plays_prev = sum(v.get("plays", 0) for v in prev.get("items") or [])
    plays_cur = sum(v.get("plays", 0) for v in cur.get("items") or [])
    return {
        "followers": cur.get("followers", 0) - prev.get("followers", 0),
        "likes": cur.get("likes", 0) - prev.get("likes", 0),
        "videos": cur.get("videos", 0) - prev.get("videos", 0),
        "plays": (plays_cur - plays_prev) if prev.get("items") and cur.get("items") else 0,
    }


def _same_day(ts_a: float, ts_b: float) -> bool:
    return datetime.fromtimestamp(ts_a).date() == datetime.fromtimestamp(ts_b).date()


def roll_day_baseline(state: dict[str, Any], cur: dict[str, Any]) -> dict[str, Any]:
    """Point de départ « aujourd'hui » : première lecture de la journée."""
    base = state.get("day_baseline")
    if not base or not _same_day(float(base.get("ts", 0)), float(cur.get("ts", 0))):
        base = {k: cur.get(k) for k in ("ts", "followers", "likes", "videos", "items")}
        state["day_baseline"] = base
    return base


def video_changes(prev: Optional[dict], cur: dict) -> list[dict[str, Any]]:
    """Par vidéo : vues gagnées depuis la lecture précédente."""
    if not prev or not prev.get("items") or not cur.get("items"):
        return []
    before = {v["id"]: v for v in prev["items"]}
    out = []
    for v in cur["items"]:
        old = before.get(v["id"])
        gained = v["plays"] - old["plays"] if old else 0
        out.append({**v, "new": old is None, "plays_gained": gained,
                    "likes_gained": v["likes"] - old["likes"] if old else 0})
    return out


def format_card(cur: dict, prev: Optional[dict], baseline: Optional[dict]) -> str:
    d = deltas(prev, cur)
    day = deltas(baseline, cur) if baseline else d
    when = datetime.fromtimestamp(cur.get("ts", time.time())).strftime("%H:%M:%S")
    lines = [
        f"**{_fmt(cur['followers'])} abonnés**  ({_signed(d['followers'])} · aujourd'hui {_signed(day['followers'])})",
        f"❤️ {_fmt(cur['likes'])} j'aime  ({_signed(d['likes'])} · aujourd'hui {_signed(day['likes'])})",
        f"🎬 {cur['videos']} vidéos · 👥 {_fmt(cur['following'])} suivis",
    ]
    items = cur.get("items") or []
    if items:
        total = sum(v["plays"] for v in items)
        lines.append(f"▶️ {_fmt(total)} vues sur les {len(items)} dernières  ({_signed(d['plays'])} · aujourd'hui {_signed(day['plays'])})")
        changes = {c["id"]: c for c in video_changes(prev, cur)}
        lines.append("")
        for v in items[:5]:
            c = changes.get(v["id"], {})
            gained = c.get("plays_gained", 0)
            tag = " 🆕" if c.get("new") else (f"  {_signed(gained)}" if gained else "")
            desc = v["desc"] or "(sans titre)"
            lines.append(f"• {_fmt(v['plays'])} vues · {_fmt(v['likes'])} ❤️ · {v['comments']} 💬 — {desc[:34]}{tag}")
    else:
        lines.append("▶️ vues par vidéo indisponibles sur cette lecture")
    lines.append("")
    lines.append(f"_Mis à jour à {when}_")
    return "\n".join(lines)


def format_spoken(cur: dict, prev: Optional[dict], baseline: Optional[dict]) -> str:
    d = deltas(prev, cur)
    day = deltas(baseline, cur) if baseline else d
    name = cur.get("nickname") or cur.get("handle")
    parts = [f"{name} : {_fmt(cur['followers'])} abonnés"]
    if day["followers"]:
        parts[-1] += f" ({_signed(day['followers'])} aujourd'hui)"
    parts.append(f"{_fmt(cur['likes'])} j'aime")
    if day["likes"]:
        parts[-1] += f" ({_signed(day['likes'])} aujourd'hui)"
    parts.append(f"{cur['videos']} vidéos")
    items = cur.get("items") or []
    if items:
        total = sum(v["plays"] for v in items)
        seg = f"{_fmt(total)} vues sur les {len(items)} dernières vidéos"
        if day["plays"]:
            seg += f" ({_signed(day['plays'])} aujourd'hui)"
        parts.append(seg)
        best = max(items, key=lambda v: v["plays"])
        parts.append(f"la plus vue : « {best['desc'][:40] or 'sans titre'} » à {_fmt(best['plays'])} vues")
    return ", ".join(parts) + "."


def notable_events(prev: Optional[dict], cur: dict) -> list[tuple[str, str, int]]:
    """(clé de dédoublonnage, message, priorité) à annoncer à la voix."""
    events: list[tuple[str, str, int]] = []
    if not prev:
        return events
    d = deltas(prev, cur)
    f_prev, f_cur = prev.get("followers", 0), cur.get("followers", 0)
    for m in MILESTONES:
        if f_prev < m <= f_cur:
            events.append((f"tiktok:milestone:{m}",
                           f"Palier TikTok franchi : {_fmt(m)} abonnés ! Tu es maintenant à {_fmt(f_cur)}.",
                           90))
            break
    else:
        if d["followers"] > 0:
            if d["followers"] == 1:
                msg = f"Nouvel abonné TikTok : tu passes à {_fmt(f_cur)}."
            else:
                msg = f"{d['followers']} nouveaux abonnés TikTok : tu passes à {_fmt(f_cur)}."
            events.append((f"tiktok:followers:{f_cur}", msg, 70))
        elif d["followers"] < 0:
            events.append((f"tiktok:followers:{f_cur}",
                           f"{-d['followers']} désabonnement{'s' if d['followers'] < -1 else ''} TikTok : tu es à {_fmt(f_cur)}.",
                           55))
    for c in video_changes(prev, cur):
        if c.get("new"):
            events.append((f"tiktok:new:{c['id']}",
                           f"Ta nouvelle vidéo « {c['desc'][:40] or 'sans titre'} » est en ligne, déjà {_fmt(c['plays'])} vues.",
                           75))
            continue
        old_plays = max(1, c["plays"] - c["plays_gained"])
        # Une vidéo qui décolle : +100 vues d'un coup, ou +50 % depuis la
        # dernière lecture quand elle a déjà un peu d'audience.
        if c["plays_gained"] >= 100 or (c["plays_gained"] >= 25 and c["plays_gained"] / old_plays >= 0.5):
            events.append((f"tiktok:surge:{c['id']}:{c['plays'] // 100}",
                           f"Ta vidéo « {c['desc'][:40] or 'sans titre'} » décolle : {_signed(c['plays_gained'])} vues, elle est à {_fmt(c['plays'])}.",
                           80))
    return events


# ════════════════════════════════════════════════════════════════════════════
# Cycle d'une lecture (utilisé par la veille et par l'outil)
# ════════════════════════════════════════════════════════════════════════════

def show_card(player: Any, state: dict[str, Any], cur: dict, prev: Optional[dict]) -> None:
    if player is None:
        return
    title = f"TikTok @{cur.get('handle') or state.get('handle')}"
    body = format_card(cur, prev, state.get("day_baseline"))
    try:
        update = getattr(player, "update_card", None)
        if callable(update) and update(CARD_TYPE, title, body):
            return
        show = getattr(player, "show_card", None)
        if callable(show):
            show(CARD_TYPE, title, body)
    except Exception:
        pass


def record_snapshot(state: dict[str, Any], cur: dict) -> Optional[dict]:
    """Ajoute la lecture à l'historique ; retourne la lecture précédente."""
    snaps = list(state.get("snapshots") or [])
    prev = snaps[-1] if snaps else None
    roll_day_baseline(state, cur)
    snaps.append(cur)
    state["snapshots"] = snaps[-HISTORY_LIMIT:]
    state["last_error"] = ""
    return prev


def poll_once(player: Any = None, state: Optional[dict] = None) -> tuple[dict, Optional[dict], dict]:
    """Une lecture complète : page → historique → carte. Retourne
    (lecture, précédente, état). Lève en cas d'échec réseau."""
    state = state if state is not None else load_state()
    handle = normalize_handle(state.get("handle", ""))
    try:
        cur = fetch_snapshot(handle)
    except Exception as exc:
        state["last_error"] = str(exc)[:200]
        save_state(state)
        raise
    prev = record_snapshot(state, cur)
    save_state(state)
    show_card(player, state, cur, prev)
    return cur, prev, state


# ════════════════════════════════════════════════════════════════════════════
# Outil exposé au modèle
# ════════════════════════════════════════════════════════════════════════════

def _history_summary(state: dict[str, Any], hours: float) -> str:
    snaps = state.get("snapshots") or []
    if len(snaps) < 2:
        return "Pas encore assez de lectures pour une évolution : lance le suivi et repasse plus tard."
    cur = snaps[-1]
    cutoff = cur["ts"] - hours * 3600
    ref = next((s for s in snaps if s["ts"] >= cutoff), snaps[0])
    span_h = max(0.02, (cur["ts"] - ref["ts"]) / 3600)
    d = deltas(ref, cur)
    label = f"{span_h:.1f} h" if span_h < 48 else f"{span_h / 24:.1f} jours"
    txt = (f"Sur les dernières {label} : {_signed(d['followers'])} abonnés "
           f"(à {_fmt(cur['followers'])}), {_signed(d['likes'])} j'aime")
    if d["plays"]:
        txt += f", {_signed(d['plays'])} vues sur les dernières vidéos"
    if d["videos"]:
        txt += f", {_signed(d['videos'])} vidéo(s)"
    return txt + "."


def _videos_summary(state: dict[str, Any]) -> str:
    snaps = state.get("snapshots") or []
    if not snaps or not snaps[-1].get("items"):
        return "Les statistiques par vidéo ne sont pas disponibles pour l'instant."
    items = snaps[-1]["items"][:8]
    lines = []
    for i, v in enumerate(items, 1):
        lines.append(f"{i}. « {v['desc'][:40] or 'sans titre'} » : {_fmt(v['plays'])} vues, "
                     f"{_fmt(v['likes'])} j'aime, {v['comments']} commentaires, {v['shares']} partages")
    return "Dernières vidéos, de la plus récente à la plus ancienne : " + " ; ".join(lines) + "."


def tiktok_tracker(parameters: dict | None = None, player: Any = None,
                   speak: Callable[[str], None] | None = None, **_kw) -> str:
    p = parameters or {}
    action = str(p.get("action") or "status").strip().lower()
    state = load_state()

    new_handle = normalize_handle(p.get("handle", ""))
    if new_handle and new_handle != state.get("handle"):
        state["handle"] = new_handle
        state["snapshots"] = []
        state["day_baseline"] = None
        save_state(state)

    if action in {"set_handle", "handle", "compte"}:
        if not new_handle:
            return "Quel compte TikTok dois-je suivre ? Donne-moi le @."
        return f"Compte TikTok enregistré : @{new_handle}. Dis « suis mon TikTok » pour lancer la veille."

    if action in {"set_interval", "interval", "intervalle"}:
        try:
            seconds = int(float(p.get("interval_s") or 0))
        except (TypeError, ValueError):
            seconds = 0
        if seconds <= 0:
            return f"Intervalle actuel : {int(state.get('interval_s') or DEFAULT_INTERVAL_S)} secondes."
        seconds = max(MIN_INTERVAL_S, min(MAX_INTERVAL_S, seconds))
        state["interval_s"] = seconds
        save_state(state)
        return f"Le TikTok sera relu toutes les {seconds} secondes."

    if not state.get("handle"):
        return "Aucun compte TikTok configuré. Donne-moi ton @ TikTok pour commencer."

    handle = state["handle"]

    if action in {"start", "follow", "suivre", "watch", "on", "enable"}:
        state["enabled"] = True
        save_state(state)
        try:
            cur, prev, state = poll_once(player, state)
        except Exception as exc:
            return (f"Suivi TikTok de @{handle} activé, mais la première lecture a échoué : {exc}. "
                    "La veille réessaiera toute seule.")
        return (f"Suivi TikTok de @{handle} activé, relu toutes les "
                f"{int(state.get('interval_s') or DEFAULT_INTERVAL_S)} secondes. "
                + format_spoken(cur, prev, state.get("day_baseline"))
                + " Je t'annoncerai les nouveaux abonnés et les vidéos qui décollent.")

    if action in {"stop", "off", "disable", "arrete", "arrêter", "pause"}:
        state["enabled"] = False
        save_state(state)
        try:
            dismiss = getattr(player, "dismiss_cards", None)
            if callable(dismiss):
                dismiss(CARD_TYPE, f"TikTok @{handle}")
        except Exception:
            pass
        return f"Suivi TikTok de @{handle} arrêté."

    if action in {"history", "evolution", "évolution", "trend", "tendance"}:
        try:
            hours = float(p.get("hours") or 24)
        except (TypeError, ValueError):
            hours = 24.0
        return _history_summary(state, hours)

    if action in {"videos", "vidéos", "video", "top"}:
        return _videos_summary(state)

    # status (défaut) : lecture fraîche si la dernière est trop vieille.
    snaps = state.get("snapshots") or []
    interval = int(state.get("interval_s") or DEFAULT_INTERVAL_S)
    if snaps and time.time() - snaps[-1]["ts"] < interval:
        cur = snaps[-1]
        prev = snaps[-2] if len(snaps) > 1 else None
        show_card(player, state, cur, prev)
    else:
        try:
            cur, prev, state = poll_once(player, state)
        except Exception as exc:
            if snaps:
                cur, prev = snaps[-1], (snaps[-2] if len(snaps) > 1 else None)
                age = int((time.time() - cur["ts"]) / 60)
                return (f"TikTok injoignable à l'instant ({exc}). Dernière lecture il y a {age} min : "
                        + format_spoken(cur, prev, state.get("day_baseline")))
            return f"Impossible de lire le profil TikTok @{handle} : {exc}"
    text = format_spoken(cur, prev, state.get("day_baseline"))
    if not state.get("enabled"):
        text += " Le suivi continu n'est pas actif : dis « suis mon TikTok » pour l'activer."
    return text


def next_delay(state: dict[str, Any]) -> float:
    """Intervalle configuré avec une gigue : des lectures parfaitement
    régulières ressemblent à un robot."""
    base = float(state.get("interval_s") or DEFAULT_INTERVAL_S)
    base = max(MIN_INTERVAL_S, min(MAX_INTERVAL_S, base))
    return base * random.uniform(0.9, 1.25)
