"""
web_search.py — Recherche web ultra-réaliste
Parsing local avancé et résultat SerpApi vérifiable.

Questions générales et actualités : chaque sujet est cherché séparément et
en parallèle (Google Actualités daté + web), dans le budget de l'outil, avec
repli DuckDuckGo si SerpApi est en panne. Prix, lieux proches, comparaisons
et profils sociaux gardent leurs moteurs SerpApi dédiés.
"""

import json
import os
import re
import sys
import threading
import time
import unicodedata
import atexit
import contextvars
import importlib.util
import warnings
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FutureTimeout
from pathlib import Path
from typing import Optional, Dict, Any, Tuple

from core import action_kit as kit
from core.live_model_policy import BALANCED_MODEL, FAST_MODEL

# Vérification sans charger requests sur le chemin vocal.
_REQUESTS = importlib.util.find_spec("requests") is not None

# Supprimer les warnings de déprécation de paquets renommés
warnings.filterwarnings("ignore", message="This package.*has been renamed", category=RuntimeWarning)

_GEMINI_TIMEOUT = 8.0
# L'analyse d'intention n'est qu'un aiguillage : au-delà, la recherche
# générale sur la phrase brute vaut mieux qu'un tour coupé par le répartiteur.
_INTENT_TIMEOUT = 3.0
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="gemini-search")
atexit.register(_executor.shutdown, wait=False)

# Corps Markdown (liens cliquables) de la dernière recherche générale
# SerpApi, pour la carte riche de l'UI — le texte brut renvoyé au modèle
# n'a pas besoin de liens, donc on garde les deux formats séparés.
_last_card_markdown: Optional[str] = None


def get_last_card_markdown() -> Optional[str]:
    global _last_card_markdown
    v, _last_card_markdown = _last_card_markdown, None
    return v


# Échéance de l'appel `web_search` en cours. Chaque requête SerpApi réduit
# son délai au temps restant : sans elle, deux appels de 8 s enchaînés
# (localisation refusée, prix puis repli) dépassaient les 15 s du
# répartiteur, qui coupait l'outil sans qu'aucun texte ne revienne.
_deadline: contextvars.ContextVar[Optional[float]] = contextvars.ContextVar(
    "web_search_deadline", default=None,
)


def _time_left(default: float) -> float:
    """Délai à accorder à un appel réseau, borné par l'échéance en cours."""
    deadline = _deadline.get()
    if deadline is None:
        return default
    left = deadline - time.monotonic()
    if left < 0.5:
        raise TimeoutError("temps de recherche épuisé")
    return min(default, left)


def _run_with_timeout(fn, *args, timeout: float = _GEMINI_TIMEOUT, **kwargs):
    future = _executor.submit(fn, *args, **kwargs)
    try:
        return future.result(timeout=timeout)
    except _FutureTimeout:
        raise TimeoutError(f"L'appel Gemini a dépassé {timeout}s")


# Une même question posée deux fois dans la conversation (« et donc ? »,
# reformulation par le modèle) ne repaie pas SerpApi.
_RESULT_TTL = 90.0
_HEDGE_DELAY = 2.5  # Gemini répond en général sous 2 s ; au-delà on couvre.


def _hedged(primary, fallback, *, timeout: float, hedge_after: float = _HEDGE_DELAY):
    """Compatibilité interne pour deux opérations bornées.

    Aucun appel de recherche ne l'utilise : il ne doit jamais démarrer un
    fournisseur web secondaire après le retour vocal d'une action.
    """
    p_future = _executor.submit(primary)
    try:
        return p_future.result(timeout=hedge_after)
    except _FutureTimeout:
        pass
    except Exception as exc:
        print(f"[WebSearch] ⚠️ backend principal échoué ({exc}) — repli immédiat")
        return fallback()

    f_future = _executor.submit(fallback)
    deadline = time.monotonic() + max(0.5, timeout - hedge_after)
    primary_error: Optional[BaseException] = None
    while time.monotonic() < deadline:
        if p_future.done():
            if p_future.exception() is None:
                f_future.cancel()
                return p_future.result()
            primary_error = p_future.exception()
            break
        if f_future.done() and f_future.exception() is None and primary_error is None:
            # Le repli a fini le premier : on laisse encore un court instant à
            # Gemini, dont la réponse est plus riche, sans bloquer l'utilisateur.
            try:
                return p_future.result(timeout=0.8)
            except (_FutureTimeout, Exception):
                return f_future.result()
        time.sleep(0.05)
    try:
        return f_future.result(timeout=max(0.5, deadline - time.monotonic() + 3.0))
    except _FutureTimeout:
        if primary_error is not None:
            raise primary_error
        raise TimeoutError("aucun moteur de recherche n'a répondu à temps")


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR        = _get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"


def _get_api_key() -> str:
    try:
        with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("gemini_api_key", "")
    except Exception:
        return ""


# ── SerpApi Backend Integration ──────────────────────────────────────────────

# (gl SerpApi/Google, hl langue, devise locale, disponibilité connue de
# Google Shopping — la plupart des pays d'Afrique de l'Ouest n'y sont pas
# référencés, on le signale alors à l'utilisateur au lieu de mentir avec
# un prix en euros/dollars.)
_COUNTRY_GEO: Dict[str, Tuple[str, str, str, bool]] = {
    "guinee": ("gn", "fr", "GNF", False),
    "senegal": ("sn", "fr", "XOF", False),
    "cote d ivoire": ("ci", "fr", "XOF", False),
    "ivoire": ("ci", "fr", "XOF", False),
    "mali": ("ml", "fr", "XOF", False),
    "burkina faso": ("bf", "fr", "XOF", False),
    "burkina": ("bf", "fr", "XOF", False),
    "niger": ("ne", "fr", "XOF", False),
    "togo": ("tg", "fr", "XOF", False),
    "benin": ("bj", "fr", "XOF", False),
    "guinee bissau": ("gw", "pt", "XOF", False),
    "cameroun": ("cm", "fr", "XAF", False),
    "gabon": ("ga", "fr", "XAF", False),
    "congo": ("cg", "fr", "XAF", False),
    "republique democratique du congo": ("cd", "fr", "CDF", False),
    "rdc": ("cd", "fr", "CDF", False),
    "tchad": ("td", "fr", "XAF", False),
    "madagascar": ("mg", "fr", "MGA", False),
    "maroc": ("ma", "fr", "MAD", True),
    "algerie": ("dz", "fr", "DZD", False),
    "tunisie": ("tn", "fr", "TND", True),
    "france": ("fr", "fr", "EUR", True),
    "belgique": ("be", "fr", "EUR", True),
    "suisse": ("ch", "fr", "CHF", True),
    "canada": ("ca", "fr", "CAD", True),
    "etats unis": ("us", "en", "USD", True),
    "usa": ("us", "en", "USD", True),
    "royaume uni": ("gb", "en", "GBP", True),
    "angleterre": ("gb", "en", "GBP", True),
    "allemagne": ("de", "de", "EUR", True),
    "nigeria": ("ng", "en", "NGN", True),
    "ghana": ("gh", "en", "GHS", True),
}


# Nom anglais canonique attendu par le paramètre "location" de SerpApi
# (géocodage Google — accepte un nom de pays seul ou "Ville, Pays").
_COUNTRY_EN_NAME: Dict[str, str] = {
    "gn": "Guinea", "sn": "Senegal", "ci": "Ivory Coast", "ml": "Mali",
    "bf": "Burkina Faso", "ne": "Niger", "tg": "Togo", "bj": "Benin",
    "gw": "Guinea-Bissau", "cm": "Cameroon", "ga": "Gabon", "cg": "Congo",
    "cd": "Democratic Republic of the Congo", "td": "Chad",
    "mg": "Madagascar", "ma": "Morocco", "dz": "Algeria", "tn": "Tunisia",
    "fr": "France", "be": "Belgium", "ch": "Switzerland", "ca": "Canada",
    "us": "United States", "gb": "United Kingdom", "de": "Germany",
    "ng": "Nigeria", "gh": "Ghana",
}


def _strip_accents_ws(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c))


def _detect_country_geo(text: str) -> Optional[Tuple[str, str, str, str, bool]]:
    """Cherche un nom de pays dans `text` et renvoie
    (nom, gl, hl, devise, shopping_disponible) ou None si rien trouvé."""
    if not text:
        return None
    norm = _strip_accents_ws(text.lower())
    # Les clés les plus longues d'abord pour éviter qu'« ivoire » ne matche
    # avant « cote d ivoire ».
    for name in sorted(_COUNTRY_GEO, key=len, reverse=True):
        if re.search(rf"\b{re.escape(name)}\b", norm):
            gl, hl, currency, has_shopping = _COUNTRY_GEO[name]
            return name, gl, hl, currency, has_shopping
    return None


def _geo_by_code(gl: str) -> Tuple[str, bool]:
    """Devise + disponibilité connue de Google Shopping pour un code pays.
    Pays absent de la table -> on suppose Shopping disponible ; si la
    recherche ne remonte rien, le repli sur la recherche générale s'applique
    de toute façon."""
    gl = (gl or "").strip().lower()
    for _gl, _hl, currency, has_shopping in _COUNTRY_GEO.values():
        if _gl == gl:
            return currency, has_shopping
    return "", True


def _get_serpapi_api_key() -> Optional[str]:
    env_key = os.environ.get("SERPAPI_API_KEY")
    if env_key:
        return env_key
    try:
        with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        key = cfg.get("serpapi_api_key", "").strip()
        return key if key else None
    except Exception:
        return None


def _user_geo() -> Tuple[str, str, Optional[str]]:
    """Pays/langue/position réelle de l'utilisateur (réglage manuel ou IP),
    utilisés quand la requête ne nomme explicitement aucun pays. `location`
    est un nom de lieu geocodable par Google ("Conakry, Guinea" ou "Guinea") —
    sans lui, `gl` seul ne suffit pas à faire remonter des résultats vraiment
    locaux pour une requête « le plus proche de moi »."""
    try:
        from core.geolocation import get_user_location
        loc = get_user_location()
        gl = (loc.get("country_code") or "fr").strip().lower()
        hl = loc.get("hl") or "fr"
        city = (loc.get("city") or "").strip()
        country_en = _COUNTRY_EN_NAME.get(gl) or loc.get("country_name") or ""
        location = f"{city}, {country_en}" if city and country_en else (country_en or None)
        return gl, hl, location
    except Exception:
        return "fr", "fr", None


def _geo_params(query: str) -> Dict[str, str]:
    """gl/hl/location SerpApi pour cette requête : pays cité explicitement
    dans la requête en priorité, sinon la position réelle de l'utilisateur —
    jamais un repli silencieux sur la France."""
    geo = _detect_country_geo(query)
    if geo:
        _, gl, hl, _, _ = geo
        location = _COUNTRY_EN_NAME.get(gl)
    else:
        gl, hl, location = _user_geo()
    params = {"gl": gl, "hl": hl}
    if location:
        params["location"] = location
    return params


def _user_coords() -> Optional[Tuple[float, float]]:
    """Position GPS réelle (téléphone/IP) ou None si non vérifiée."""
    try:
        from core.geolocation import get_user_location
        loc = get_user_location()
        lat, lon = loc.get("lat"), loc.get("lon")
        if lat is None or lon is None:
            return None
        return float(lat), float(lon)
    except Exception:
        return None


def _redact_serpapi_error(exc: Exception) -> str:
    """Message d'erreur sans URL ni clé : ce texte remonte au modèle vocal."""
    text = re.sub(r"api_key=[^&\s]+", "api_key=***", str(exc))
    text = re.sub(r"https?://serpapi\.com/\S+", "serpapi.com", text)
    return text.strip()


def _location_fallbacks(location: Optional[str]) -> list:
    """SerpApi n'accepte dans `location` que les lieux de sa base canonique :
    un village inconnu (« Bailobaya Centre, Guinea ») renvoie un 400. On
    dégrade « Ville, Pays » -> « Pays » -> sans location plutôt que d'échouer."""
    chain = []
    if location:
        chain.append(location)
        if "," in location:
            country = location.rsplit(",", 1)[-1].strip()
            if country and country != location:
                chain.append(country)
    chain.append(None)
    return chain


def _call_serpapi(params: dict, timeout: Any = 8) -> dict:
    if not _REQUESTS:
        raise RuntimeError("requests non installé. Exécutez : pip install requests")
    key = _get_serpapi_api_key()
    if not key:
        raise ValueError("Clé SerpApi absente (SERPAPI_API_KEY ou config/api_keys.json)")
    params["api_key"] = key
    if "hl" not in params or "gl" not in params:
        params.update({k: v for k, v in _geo_params("").items() if k not in params})
    last_exc: Optional[Exception] = None
    for location in _location_fallbacks(params.get("location")):
        attempt = {k: v for k, v in params.items() if k != "location"}
        if location:
            attempt["location"] = location
        call_timeout = _time_left(float(timeout))
        try:
            r = kit.http().get("https://serpapi.com/search.json", params=attempt, timeout=call_timeout)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict) and data.get("error") and not any(
                k.endswith("_results") for k in data
            ):
                # « Google hasn't returned any results » n'est pas une panne :
                # on rend un résultat vide plutôt qu'une exception.
                if "hasn't returned any results" in str(data["error"]):
                    return {}
                raise RuntimeError(str(data["error"]))
            return data
        except Exception as exc:
            last_exc = exc
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status == 400 and location is not None:
                print(f"[WebSearch] ⚠️ location SerpApi refusée ({location}), repli plus large")
                continue
            break
    raise RuntimeError(_redact_serpapi_error(last_exc or Exception("SerpApi indisponible")))


def _format_serpapi_results(data: dict) -> str:
    lines = []
    ab = data.get("answer_box")
    if ab:
        ans = ab.get("answer") or ab.get("snippet") or ab.get("text")
        if ans:
            lines.append(f"💡 Réponse directe : {ans}")
            if ab.get("title"):
                lines.append(f"   ({ab['title']})")
            lines.append("")
    kg = data.get("knowledge_graph")
    if kg:
        title = kg.get("title")
        typ = kg.get("type")
        desc = kg.get("description")
        if title and desc:
            lines.append(f"📖 {title} ({typ or 'Information'})")
            lines.append(f"   {desc}")
            lines.append("")
    org = data.get("organic_results", [])
    if org:
        lines.append("🔍 Résultats de recherche :")
        for i, r in enumerate(org[:6], 1):
            title = r.get("title", "Sans titre")
            link = r.get("link", "")
            snippet = r.get("snippet", "")
            lines.append(f"{i}. {title}")
            if snippet:
                lines.append(f"   {snippet}")
            if link:
                lines.append(f"   Source : {link}")
            lines.append("")
    paa = data.get("related_questions", [])
    if paa:
        lines.append("❓ Questions connexes :")
        for q in paa[:3]:
            question = q.get("question")
            answer = q.get("snippet")
            if question:
                lines.append(f"  • {question}")
                if answer:
                    lines.append(f"    -> {answer}")
        lines.append("")
    return "\n".join(lines).strip()


def format_results_markdown(data: dict) -> str:
    """Même contenu que `_format_serpapi_results`, mais en Markdown avec
    titres cliquables — pour la carte riche affichée dans l'UI (le texte
    brut reste utilisé pour la réponse vocale/LLM, qui n'a pas besoin de
    liens)."""
    lines = []
    ab = data.get("answer_box")
    if ab:
        ans = ab.get("answer") or ab.get("snippet") or ab.get("text")
        if ans:
            lines.append(f"**💡 {ans}**")
            lines.append("")
    org = data.get("organic_results", [])
    if org:
        for i, r in enumerate(org[:6], 1):
            title = r.get("title", "Sans titre")
            link = r.get("link", "")
            snippet = r.get("snippet", "")
            lines.append(f"**{i}. [{title}]({link})**" if link else f"**{i}. {title}**")
            if snippet:
                lines.append(snippet)
            lines.append("")
    return "\n".join(lines).strip()


# ── Recherche fraîche multi-sujets ──────────────────────────────────────────
# Une question comme « Grok 4.7, GPT-6 Sol et Opus 5.5 sont sortis ? » porte
# sur plusieurs sujets. Les fondre en une seule requête Google ramène des
# pages qui ne parlent d'aucun d'eux (vidéos, homonymes) : chaque sujet est
# donc cherché séparément, en parallèle, dans Google Actualités (daté, trié
# par fraîcheur) et dans la recherche web classique.

_MAX_SUBJECTS = 4
_SERP_TIMEOUT = (3.0, 7.0)
_serp_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="serpapi")
atexit.register(_serp_pool.shutdown, wait=False)

# Éditions Google Actualités confirmées ; ailleurs on retombe sur l'édition
# francophone (ou anglophone) la plus riche plutôt que sur un flux vide.
_NEWS_EDITIONS = frozenset({
    "fr", "be", "ch", "ca", "us", "gb", "de", "ma", "sn", "ci", "cm", "gn",
    "ng", "gh", "tn", "dz",
})

_TIME_SENSITIVE_RE = re.compile(
    r"\b(?:sorti[es]?|sortie|nouveau|nouvelle|nouveaut[ée]s?|derni[eè]re?s?|actu\w*|"
    r"annonc\w*|lanc[ée]\w*|lancement|release[ds]?|launch\w*|update|mise\s+[àa]\s+jour|"
    r"aujourd'?hui|hier|cette\s+semaine|ce\s+mois|r[ée]cent\w*|en\s+ce\s+moment|"
    r"latest|news|infos?|breaking|score|r[ée]sultats?|match|[ée]lections?|"
    r"20[2-3]\d|v?\d+\.\d+|[a-z]+-\d+)\b",
    re.I,
)

# Mots qui n'aident pas Google Actualités à trouver un sujet : ils restent
# dans la requête web, mais sont retirés de la requête actualités.
_NEWS_FILLER_RE = re.compile(
    r"\b(?:release|released|news|latest|launch|actualit[ée]s?|actus?|infos?|sortie|"
    r"sorti[es]?|derni[eè]res?|nouvelles?|annonce|date\s+de\s+sortie|est[- ]il|"
    r"sont[- ]ils|sont|est|apparemment|officiel(?:le)?)\b",
    re.I,
)

_SUBJECT_SPLIT_RE = re.compile(r"\s*(?:,|;|\||/|&|\bet\b|\band\b|\bpuis\b|\bainsi\s+que\b)\s*", re.I)


def _is_time_sensitive(text: str) -> bool:
    return bool(_TIME_SENSITIVE_RE.search(text or ""))


def _split_subjects(query: str) -> list:
    """« Grok 4.7, GPT-6 Sol et Opus 5.5 » -> trois sujets. Une requête sans
    séparateur reste entière : on ne découpe jamais un nom au hasard."""
    text = query or ""
    # « et » seul ne sépare des sujets que dans une vraie liste (« A, B et C »)
    # ou entre deux noms versionnés (« GPT-6 et Opus 5.5 ») : « Tom et Jerry »
    # ou « la guerre et la paix » restent un seul sujet.
    if not re.search(r"[,;|/&]", text):
        halves = re.split(r"\s+(?:et|and)\s+", text, flags=re.I)
        if not (len(halves) > 1 and all(re.search(r"\d", h) for h in halves)):
            return [text.strip()] if text.strip() else []
    parts = [p.strip(" .?!") for p in _SUBJECT_SPLIT_RE.split(text)]
    parts = [p for p in parts if len(p) >= 2]
    if len(parts) < 2:
        return [query.strip()] if query and query.strip() else []
    # Un mot isolé (« et moi ») n'est pas un sujet : on garde la phrase entière.
    if any(len(p) < 3 for p in parts):
        return [query.strip()]
    return parts[:_MAX_SUBJECTS]


def _clean_subjects(queries: Any, query: str) -> list:
    subjects = []
    if isinstance(queries, str):
        queries = [queries]
    for q in queries or []:
        q = str(q or "").strip()
        if q and q.casefold() not in {s.casefold() for s in subjects}:
            subjects.append(q)
    if not subjects:
        subjects = _split_subjects(query)
    return subjects[:_MAX_SUBJECTS]


def _web_geo(query: str) -> Dict[str, str]:
    """gl/hl sans `location` : une ville ne sert qu'aux recherches locales
    (nearby, prix). Pour une question générale, elle ralentit SerpApi et
    restreint les résultats à ce que Google juge « local »."""
    geo = _geo_params(query)
    return {"gl": geo["gl"], "hl": geo["hl"]}


def _news_geo(query: str) -> Dict[str, str]:
    geo = _web_geo(query)
    if geo["gl"] not in _NEWS_EDITIONS:
        geo["gl"] = "fr" if geo["hl"] == "fr" else "us"
    return geo


def _news_query(subject: str) -> str:
    q = re.sub(r"\s{2,}", " ", _NEWS_FILLER_RE.sub(" ", subject)).strip(" ,.?!")
    return q or subject


def _parse_iso(value: str):
    from datetime import datetime, timezone
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _parse_news_date(item: dict):
    """Date absolue d'un article Google Actualités (iso_date ou « 09/22/2026,
    06:35 PM, +0000 UTC »)."""
    from datetime import datetime, timezone
    dt = _parse_iso(item.get("iso_date") or "")
    if dt:
        return dt
    raw = str(item.get("date") or "")
    m = re.match(r"(\d{2})/(\d{2})/(\d{4}), (\d{1,2}):(\d{2}) (AM|PM)", raw)
    if not m:
        return None
    month, day, year, hour, minute, ampm = m.groups()
    h = int(hour) % 12 + (12 if ampm == "PM" else 0)
    try:
        return datetime(int(year), int(month), int(day), h, int(minute), tzinfo=timezone.utc)
    except ValueError:
        return None


_MONTHS_FR = ("janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.",
              "août", "sept.", "oct.", "nov.", "déc.")


def _humanize_age(dt) -> str:
    from datetime import datetime, timezone
    if dt is None:
        return ""
    now = datetime.now(timezone.utc)
    delta = now - dt
    secs = delta.total_seconds()
    if secs < 0:
        secs = 0
    if secs < 3600:
        return f"il y a {max(1, int(secs // 60))} min"
    if secs < 86400:
        return f"il y a {int(secs // 3600)} h"
    if secs < 2 * 86400:
        return "hier"
    if secs < 7 * 86400:
        return f"il y a {int(secs // 86400)} jours"
    local = dt.astimezone()
    year = f" {local.year}" if local.year != now.astimezone().year else ""
    return f"le {local.day} {_MONTHS_FR[local.month - 1]}{year}"


def _source_name(raw: Any) -> str:
    if isinstance(raw, list):
        raw = raw[0] if raw else ""
    if isinstance(raw, dict):
        return str(raw.get("name") or raw.get("title") or "")
    return str(raw or "")


def _flatten_news(results: list) -> list:
    """Google Actualités regroupe parfois des articles dans « stories »."""
    flat = []
    for item in results or []:
        if not isinstance(item, dict):
            continue
        if item.get("title") and item.get("link"):
            flat.append(item)
        for sub in (item.get("highlight"),):
            if isinstance(sub, dict) and sub.get("title"):
                flat.append(sub)
        for sub in item.get("stories") or []:
            if isinstance(sub, dict) and sub.get("title"):
                flat.append(sub)
    return flat


def _words(text: str) -> set:
    norm = _strip_accents_ws((text or "").casefold())
    return {w for w in re.findall(r"[a-z0-9][a-z0-9.+-]*", norm) if len(w) > 1 or w.isdigit()}


def _relevant(subject: str, text: str) -> bool:
    """Au moins un terme distinctif du sujet (nom, numéro de version) doit
    figurer dans le titre/extrait : Google Actualités remplit sinon avec des
    articles voisins qui ne parlent pas du sujet demandé."""
    key = _words(_news_query(subject)) - {"le", "la", "les", "de", "du", "des", "the", "of"}
    if not key:
        return True
    hay = _words(text)
    numbers = {w for w in key if re.search(r"\d", w)}
    names = key - numbers
    if numbers and not numbers & hay:
        return False
    # Le numéro seul ne suffit pas : « DnD 5.5 » n'est pas « Opus 5.5 ».
    if names:
        return bool(names & hay)
    return True


def _fetch_news_items(subject: str, limit: int = 6) -> list:
    data = _call_serpapi(
        {"q": _news_query(subject), "engine": "google_news", **_news_geo(subject)},
        timeout=_SERP_TIMEOUT,
    )
    items = []
    seen = set()
    for n in _flatten_news(data.get("news_results", [])):
        title = (n.get("title") or "").strip()
        key = re.sub(r"\W+", "", title.casefold())[:80]
        if not title or key in seen:
            continue
        if not _relevant(subject, f"{title} {n.get('snippet') or ''}"):
            continue
        seen.add(key)
        items.append({
            "title": title,
            "source": _source_name(n.get("source")),
            "date": _parse_news_date(n),
            "link": n.get("link") or "",
            "snippet": n.get("snippet") or "",
        })
        if len(items) >= limit:
            break
    # Pertinence Google d'abord pour choisir, fraîcheur ensuite pour lire.
    from datetime import datetime, timezone
    floor = datetime(1970, 1, 1, tzinfo=timezone.utc)
    items.sort(key=lambda it: it["date"] or floor, reverse=True)
    return items


def _fetch_web(subject: str, fresh: bool = False) -> dict:
    params = {"q": subject, "engine": "google", **_web_geo(subject)}
    if fresh:
        # Résultats récents d'abord sans exclure les pages de référence.
        params["tbs"] = "qdr:m"
    return _call_serpapi(params, timeout=_SERP_TIMEOUT)


def _ai_overview_text(data: dict) -> str:
    ov = data.get("ai_overview") or {}
    parts = []
    for block in ov.get("text_blocks") or []:
        if block.get("snippet"):
            parts.append(block["snippet"])
        for li in block.get("list") or []:
            if isinstance(li, dict) and li.get("snippet"):
                parts.append("• " + li["snippet"])
        if sum(len(p) for p in parts) > 700:
            break
    return " ".join(parts).strip()


def _web_lines(data: dict, max_results: int = 4, subject: str = "") -> list:
    lines = []
    ab = data.get("answer_box") or {}
    ans = ab.get("answer") or ab.get("snippet") or ab.get("result")
    if ans:
        lines.append(f"  💡 Réponse directe : {ans}")
    ov = _ai_overview_text(data)
    if ov:
        lines.append(f"  🤖 Synthèse Google : {ov[:700]}")
    kg = data.get("knowledge_graph") or {}
    if kg.get("title") and kg.get("description"):
        lines.append(f"  📖 {kg['title']} : {kg['description']}")
    stories = data.get("top_stories") or []
    if subject:
        stories = [st for st in stories if _relevant(subject, st.get("title") or "")]
    for st in stories[:3]:
        if st.get("title"):
            src = _source_name(st.get("source"))
            meta = ", ".join(x for x in (src, st.get("date") or "") if x)
            lines.append(f"  📰 {st['title']}" + (f" — {meta}" if meta else ""))
    for r in _relevant_organic(data, subject)[:max_results]:
        title = r.get("title")
        if not title:
            continue
        date = f" ({r['date']})" if r.get("date") else ""
        lines.append(f"  • {title}{date}")
        if r.get("snippet"):
            lines.append(f"    {r['snippet']}")
        if r.get("link"):
            lines.append(f"    Source : {r['link']}")
    return lines


def _relevant_organic(data: dict, subject: str = "") -> list:
    rows = [r for r in (data.get("organic_results") or []) if r.get("title")]
    if not subject:
        return rows
    return [r for r in rows if _relevant(subject, f"{r['title']} {r.get('snippet') or ''}")]


def _ddgs_fallback(subject: str, news: bool) -> dict:
    """Dernier recours si SerpApi est en panne ou sans crédit."""
    from ddgs import DDGS
    with DDGS(timeout=5) as d:
        if news:
            rows = list(d.news(subject, max_results=6, timelimit="w"))
            return {"news": [{
                "title": r.get("title", ""), "source": r.get("source", ""),
                "date": _parse_iso(r.get("date") or ""), "link": r.get("url", ""),
                "snippet": r.get("body", ""),
            } for r in rows if r.get("title")]}
        rows = list(d.text(subject, max_results=5))
        return {"web": {"organic_results": [{
            "title": r.get("title"), "snippet": r.get("body"), "link": r.get("href"),
        } for r in rows]}}


_DDGS_AVAILABLE = importlib.util.find_spec("ddgs") is not None


def _gather(jobs: dict, deadline: float) -> Tuple[dict, dict]:
    """Lance toutes les requêtes en parallèle et rend ce qui est arrivé avant
    l'échéance — un moteur lent ne fait jamais perdre les autres résultats."""
    from concurrent.futures import wait
    futures = {_serp_pool.submit(fn): key for key, fn in jobs.items()}
    done, _pending = wait(futures, timeout=max(0.5, deadline - time.monotonic()))
    results, errors = {}, {}
    for fut, key in futures.items():
        if fut not in done:
            errors[key] = "délai dépassé"
            fut.cancel()
            continue
        exc = fut.exception()
        if exc is not None:
            errors[key] = _redact_serpapi_error(exc) if isinstance(exc, Exception) else str(exc)
        else:
            results[key] = fut.result()
    return results, errors


def _fresh_search(subjects: list, mode: str, budget_s: float = 13.0) -> Tuple[str, str]:
    """Recherche parallèle par sujet. Rend (texte pour le modèle, Markdown carte)."""
    from datetime import datetime
    deadline = time.monotonic() + max(1.0, budget_s - 1.0)
    with_news = mode == "news" or any(_is_time_sensitive(s) for s in subjects)
    with_web = mode != "news" or len(subjects) == 1
    use_serp = bool(_get_serpapi_api_key())

    jobs = {}
    for i, subject in enumerate(subjects):
        if use_serp:
            if with_news:
                jobs[("news", i)] = (lambda s=subject: _fetch_news_items(s))
            if with_web:
                jobs[("web", i)] = (lambda s=subject: _fetch_web(s))
        elif _DDGS_AVAILABLE:
            jobs[("ddgs", i)] = (lambda s=subject: _ddgs_fallback(s, with_news))
    if not jobs:
        raise RuntimeError("aucun moteur de recherche configuré (clé SerpApi absente)")

    results, errors = _gather(jobs, deadline)

    # SerpApi entièrement en échec (quota, réseau) : second moteur, dans le
    # temps restant seulement.
    if use_serp and not results and _DDGS_AVAILABLE and deadline - time.monotonic() > 2.5:
        print(f"[WebSearch] ⚠️ SerpApi en échec ({errors}), repli DuckDuckGo")
        more, _ = _gather(
            {("ddgs", i): (lambda s=s: _ddgs_fallback(s, with_news)) for i, s in enumerate(subjects)},
            deadline,
        )
        results.update(more)

    if not results:
        reason = next(iter(errors.values()), "aucune réponse")
        raise RuntimeError(reason)

    now = datetime.now().astimezone()
    out = [f"Recherche web du {now.day} {_MONTHS_FR[now.month - 1]} {now.year}, {now:%H:%M} "
           "(résultats réels, les plus récents d'abord)."]
    md = []
    for i, subject in enumerate(subjects):
        news = list(results.get(("news", i)) or [])
        web = results.get(("web", i)) or {}
        dd = results.get(("ddgs", i)) or {}
        news += dd.get("news") or []
        if not web and dd.get("web"):
            web = dd["web"]
        out.append("")
        out.append(f"▶ {subject}")
        md.append(f"### {subject}")
        if news:
            out.append("  Actualités :")
            for n in news[:5]:
                meta = ", ".join(x for x in (n["source"], _humanize_age(n["date"])) if x)
                out.append(f"  {n['title']}" + (f" — {meta}" if meta else ""))
                if n.get("snippet"):
                    out.append(f"    {n['snippet'][:200]}")
                link = n.get("link")
                md.append(f"- **[{n['title']}]({link})**" if link else f"- **{n['title']}**")
                if meta:
                    md.append(f"  _{meta}_")
        wl = _web_lines(web, max_results=3 if news else 5, subject=subject) if web else []
        if wl:
            out.append("  Web :")
            out.extend(wl)
            for r in _relevant_organic(web, subject)[:3 if news else 5]:
                if r.get("link"):
                    md.append(f"- [{r['title']}]({r['link']})")
        if not news and not wl:
            failed = [k for k in errors if k[1] == i]
            out.append("  Aucune source trouvée" + (" (moteur indisponible)." if failed else
                       " : rien ne confirme ce sujet pour l'instant."))
        md.append("")
    out.append("")
    out.append(
        "Consigne : réponds uniquement à partir de ces résultats, en citant la source et "
        "la date quand c'est utile. Pour un sujet sans source, dis que tu n'as trouvé "
        "aucune confirmation — ne dis pas que tu n'as pas accès aux informations."
    )
    return "\n".join(out).strip(), "\n".join(md).strip()


def _serpapi_news(query: str, budget_s: float = 13.0) -> str:
    text, _ = _fresh_search(_clean_subjects(None, query), "news", budget_s)
    return text


def _serpapi_headlines(count: int = 5, topic: str = "") -> str:
    """Titres du jour : la une Google Actualités de l'édition locale."""
    geo = _news_geo(topic)
    params = {"engine": "google_news", **geo}
    if topic:
        params["q"] = topic
    data = _call_serpapi(params, timeout=_SERP_TIMEOUT)
    items = _flatten_news(data.get("news_results", []))
    if not items:
        return "Aucun titre trouvé pour le moment."
    lines = ["📰 À la une :"]
    seen = set()
    for n in items:
        title = (n.get("title") or "").strip()
        if not title or title in seen:
            continue
        seen.add(title)
        meta = ", ".join(x for x in (_source_name(n.get("source")),
                                     _humanize_age(_parse_news_date(n))) if x)
        lines.append(f"{len(seen)}. {title}" + (f" — {meta}" if meta else ""))
        if len(seen) >= max(1, min(count, 10)):
            break
    return "\n".join(lines)


def _serpapi_price(query: str) -> str:
    country = _detect_country_geo(query)
    geo = _geo_params(query)
    currency, has_shopping = _geo_by_code(geo["gl"]) if not country else (country[3], country[4])

    shop = []
    if has_shopping:
        data = _call_serpapi({"q": query, "engine": "google_shopping", **geo})
        shop = data.get("shopping_results", [])

    if not shop:
        data = _call_serpapi({"q": f"{query} prix tarif", "engine": "google", **geo})
        result = _format_serpapi_results(data)
        if not has_shopping:
            devise = f" que le {currency} local" if currency else " que la devise locale"
            note = (
                f"\n\n(ℹ️ Les comparateurs de prix ne couvrent pas ce pays : les montants "
                f"ci-dessus viennent des résultats de recherche générale et peuvent être "
                f"affichés dans une devise différente{devise} — vérifiez sur un "
                f"site marchand local avant tout achat.)"
            )
            result = (result + note) if result else note.strip()
        return result
    lines = [f"🏷️ Prix et offres pour : {query}\n"]
    for i, s in enumerate(shop[:6], 1):
        title = s.get("title")
        price = s.get("price")
        source = s.get("source")
        link = s.get("link")
        rating = s.get("rating")
        delivery = s.get("delivery")
        rating_str = f" (★ {rating})" if rating else ""
        deliv_str = f" - {delivery}" if delivery else ""
        lines.append(f"{i}. {title} — {price}{rating_str}")
        lines.append(f"   Magasin : {source}{deliv_str}")
        if link:
            lines.append(f"   Lien d'achat : {link}")
        lines.append("")
    return "\n".join(lines).strip()


def _serpapi_research(query: str) -> str:
    data = _call_serpapi({"q": query, "engine": "google", **_geo_params(query)})
    lines = [f"🔬 Recherche approfondie (SerpApi) : {query}\n"]
    ab = data.get("answer_box")
    if ab:
        ans = ab.get("answer") or ab.get("snippet") or ab.get("text")
        if ans:
            lines.append("✨ RÉSUMÉ DIRECT :")
            lines.append(f"   {ans}\n")
    kg = data.get("knowledge_graph")
    if kg:
        title = kg.get("title")
        desc = kg.get("description")
        if title and desc:
            lines.append(f"📚 ENCYCLOPÉDIE : {title}")
            lines.append(f"   {desc}\n")
    org = data.get("organic_results", [])
    if org:
        lines.append("📖 LIENS ET DÉTAILS :")
        for i, r in enumerate(org[:8], 1):
            title = r.get("title")
            snippet = r.get("snippet")
            link = r.get("link")
            lines.append(f"  {i}. {title}")
            if snippet:
                lines.append(f"     {snippet}")
            if link:
                lines.append(f"     Lien : {link}")
        lines.append("")
    paa = data.get("related_questions", [])
    if paa:
        lines.append("🔍 INFORMATIONS CONNEXES :")
        for q in paa[:4]:
            question = q.get("question")
            answer = q.get("snippet")
            if question and answer:
                lines.append(f"  • {question}")
                lines.append(f"    -> {answer}")
        lines.append("")
    return "\n".join(lines).strip()


def _nearby_map_results(query: str, center: Tuple[float, float], *,
                        zoom: int = 14, language: str = "fr",
                        timeout: float = 6.0) -> dict:
    """Même recherche Google Maps GPS pour web_search et find_nearby.

    Le point fourni reste l'autorité, notamment pour une recherche `near` :
    aucun nom de ville déduit de l'IP n'est envoyé à SerpApi.
    """
    geo = _geo_params(query)
    return _call_serpapi({
        "q": query, "engine": "google_maps", "type": "search",
        "ll": f"@{center[0]:.6f},{center[1]:.6f},{zoom}z",
        "nearby": "true", "hl": language, "gl": geo["gl"],
    }, timeout=timeout)


def _serpapi_nearby(query: str) -> str:
    """Lieux à proximité (pharmacie, hôpital, restaurant…) — recherche
    locale (Google Local) plutôt que web classique, seule capable de
    remonter de vrais établissements géolocalisés au lieu de pages
    génériques les mieux référencées globalement."""
    geo = _geo_params(query)
    places = []
    coords = None if _detect_country_geo(query) else _user_coords()
    if coords:
        # Position GPS connue : Google Maps centré sur les coordonnées exactes,
        # sans dépendre d'un nom de lieu que SerpApi connaîtrait ou non.
        try:
            data = _nearby_map_results(query, coords, language=geo["hl"])
            places = data.get("local_results", []) or []
        except Exception as exc:
            print(f"[WebSearch] ⚠️ google_maps indisponible ({exc}), repli google_local")
    if not places:
        data = _call_serpapi({"q": query, "engine": "google_local", **geo})
        places = data.get("local_results", [])
        if isinstance(places, dict):
            places = places.get("places", [])
    if not places:
        data = _call_serpapi({"q": f"{query} près de moi", "engine": "google", **geo})
        result = _format_serpapi_results(data)
        return result or f"Aucun résultat local trouvé pour « {query} »."
    lines = [f"📍 « {query} » à proximité :\n"]
    for i, p in enumerate(places[:6], 1):
        title = p.get("title", "Sans nom")
        address = p.get("address", "")
        rating = p.get("rating")
        reviews = p.get("reviews")
        phone = p.get("phone")
        gps = p.get("gps_coordinates") or {}
        rating_str = ""
        if rating:
            rating_str = f" (★ {rating}" + (f", {reviews} avis" if reviews else "") + ")"
        lines.append(f"{i}. {title}{rating_str}")
        if address:
            lines.append(f"   {address}")
        if phone:
            lines.append(f"   Tél : {phone}")
        if gps.get("latitude") is not None:
            # Coordonnées exactes pour show_map(lat=…, lon=…) : plus fiable
            # que de re-géocoder un nom d'établissement.
            lines.append(f"   Coordonnées (pour show_map) : {gps['latitude']}, {gps['longitude']}")
        lines.append("")
    return "\n".join(lines).strip()


def _serpapi_compare(items: list, aspect: str) -> str:
    vs_query = f"{' vs '.join(items)} {aspect}"
    data = _call_serpapi({"q": vs_query, "engine": "google"})
    ab = data.get("answer_box")
    if ab:
        ans = ab.get("answer") or ab.get("snippet") or ab.get("text")
        if ans:
            return (f"⚖️ Comparaison {aspect} : {' vs '.join(items)}\n\n"
                    f"💡 Réponse directe : {ans}\n\n" + _format_serpapi_results(data))
    return f"⚖️ Comparaison {aspect} : {' vs '.join(items)}\n\n" + _format_serpapi_results(data)


# ── Gemini Live / SerpApi ───────────────────────────────────────────────────
_gemini_client_cache: Dict[str, Any] = {}
_gemini_client_lock = threading.Lock()


def _gemini_client():
    """Client Gemini réutilisé : sa construction (SSL, découverte) coûte
    plusieurs centaines de millisecondes à chaque recherche."""
    key = _get_api_key()
    with _gemini_client_lock:
        client = _gemini_client_cache.get(key)
        if client is None:
            from google import genai
            client = genai.Client(api_key=key)
            _gemini_client_cache.clear()
            _gemini_client_cache[key] = client
        return client


def _gemini_search_impl(query: str) -> str:
    client = _gemini_client()
    chat = client.chats.create(
        model=BALANCED_MODEL,
        config={"tools": [{"google_search": {}}]},
    )
    response = chat.send_message(query)
    if not response.candidates:
        raise ValueError("Gemini n'a retourné aucun candidat.")
    cand = response.candidates[0]
    if not cand.content or not cand.content.parts:
        raise ValueError("Gemini a retourné une réponse vide.")
    text = ""
    for part in cand.content.parts:
        if hasattr(part, "text") and part.text:
            text += part.text
    text = text.strip()
    if not text:
        raise ValueError("Gemini a retourné une réponse vide.")
    return text


def _gemini_search(query: str) -> str:
    return _run_with_timeout(_gemini_search_impl, query)


# ── Profils de réseaux sociaux ────────────────────────────────────────────
# Une recherche générale sur un pseudo est très bruitée (articles, vidéos,
# homonymes).  Ces domaines sont volontairement limités aux plateformes dont
# les URL de profil sont publiques et indexables par les moteurs.
_SOCIAL_PLATFORMS = {
    "tiktok": ("TikTok", "tiktok.com"),
    "instagram": ("Instagram", "instagram.com"),
    "youtube": ("YouTube", "youtube.com"),
    "facebook": ("Facebook", "facebook.com"),
    "x": ("X", "x.com"),
    "twitter": ("X", "x.com"),
    "twitch": ("Twitch", "twitch.tv"),
    "snapchat": ("Snapchat", "snapchat.com"),
}


def _social_profile_intent(text: str) -> Optional[Dict[str, str]]:
    """Reconnaît « connais-tu le tiktokeur techenclair ? » sans IA.

    Le pseudo est conservé tel qu'il est prononcé/écrit : il ne faut surtout
    pas le remplacer par une interprétation sémantique (par ex. « tech en
    clair » ou un sujet informatique).
    """
    source = (text or "").strip()
    norm = _strip_accents_ws(source.casefold())
    platform = None
    aliases = {
        "tiktok": ("tiktokeur", "tiktok", "tik tok", "tik-tokeur"),
        "instagram": ("instagrammeur", "instagram", "instagrameur", "insta"),
        "youtube": ("youtubeur", "youtubeuse", "youtube"),
        "facebook": ("facebook",),
        "x": ("sur x", "twitter", "twittos"),
        "twitch": ("twitch", "streameur", "streamer"),
        "snapchat": ("snapchat", "snapchateur"),
    }
    for name, words in aliases.items():
        if any(re.search(rf"\b{re.escape(word)}\b", norm) for word in words):
            platform = name
            break
    if not platform:
        return None

    # Cas usuels de la voix : « connais-tu le tiktokeur techenclair ? ».
    patterns = (
        r"(?:tiktokeur|tik[- ]?tok|instagrammeur|instagrameur|insta|youtubeur|youtubeuse|"
        r"youtube|facebook|twitter|twittos|twitch|streameur|streamer|snapchat(?:eur)?)\s+"
        r"(?:nomm[eé]\s+|appel[eé]\s+)?[@#]?([^?.!,;:]+)",
        r"(?:compte|profil)\s+(?:de|du|d['’])\s*[@#]?([^?.!,;:]+)",
    )
    handle = ""
    for pattern in patterns:
        match = re.search(pattern, norm)
        if match:
            handle = match.group(1).strip()
            break
    # Un appel direct « cherche @techenclair sur TikTok » est aussi accepté.
    if not handle:
        match = re.search(r"[@#]([\w.-]{2,})", source)
        if match:
            handle = match.group(1)
    handle = re.sub(r"^(?:le|la|un|une|de|du|des|d['’])\s+", "", handle).strip(" @#'\"")
    if not handle or len(handle) > 80:
        return None
    return {"handle": handle, "platform": platform}


def _social_search_query(handle: str, platform: str) -> str:
    label, domain = _SOCIAL_PLATFORMS[platform]
    # Les guillemets empêchent le moteur de transformer un pseudo composé en
    # thème de recherche. site: force un résultat de la plateforme demandée.
    return f'site:{domain} "{handle}"'


def _is_social_profile_url(url: str, domain: str) -> bool:
    """Écarte les pages de recherche, hashtags, vidéos et articles.

    On ne prétend jamais avoir trouvé un compte sur la seule base d'un mot
    dans un extrait. Un lien doit venir du domaine demandé et avoir une forme
    d'URL de profil plausible.
    """
    value = (url or "").casefold()
    if domain not in value:
        return False
    blocked = ("/search", "/tag/", "/hashtag/", "/video/", "/watch?", "/results")
    if any(part in value for part in blocked):
        return False
    return bool(re.search(r"/(?:@|channel/|c/|user/|[a-z0-9_.-]{2,})(?:[/?#]|$)", value))


def _format_social_profiles(handle: str, platform: str, results: list[dict]) -> str:
    label, domain = _SOCIAL_PLATFORMS[platform]
    profiles = [r for r in results if _is_social_profile_url(r.get("url") or r.get("link", ""), domain)]
    if not profiles:
        return (
            f"Aucun profil {label} public pour « {handle} » n'a pu être confirmé. "
            "Je ne vais pas inventer de compte : le pseudo peut être privé, non indexé, "
            "ou appartenir à plusieurs personnes."
        )
    lines = [f"Profil(s) {label} trouvé(s) pour le pseudo « {handle} » :"]
    for index, result in enumerate(profiles[:5], 1):
        title = result.get("title") or "Profil"
        url = result.get("url") or result.get("link") or ""
        snippet = result.get("snippet") or ""
        lines.append(f"{index}. {title}")
        if snippet:
            lines.append(f"   {snippet}")
        lines.append(f"   Profil : {url}")
    lines.append("Vérifie la bio et les publications avant de conclure qu'il s'agit bien de la bonne personne.")
    return "\n".join(lines)


def _social_profiles(handle: str, platform: str, player=None) -> str:
    """Cherche un pseudo avec SerpApi, sans moteur secondaire non confirmé."""
    platform = platform if platform in _SOCIAL_PLATFORMS else "tiktok"
    query = _social_search_query(handle, platform)
    if player:
        player.write_log(f"[Search:Social:{platform}] @{handle}")
    try:
        if not _get_serpapi_api_key():
            return "SerpApi n'est pas configuré : recherche de profil non confirmée."
        data = _call_serpapi({"q": query, "engine": "google", **_geo_params("")})
        results = data.get("organic_results", [])
        return _format_social_profiles(handle, platform, results)
    except Exception as exc:
        print(f"[WebSearch] ⚠️ Recherche profil social échouée ({exc})")
        return f"Recherche du profil { _SOCIAL_PLATFORMS[platform][0] } « {handle} » indisponible : {exc}"


def _format_news(query: str, results: list) -> str:
    if not results:
        return f"Aucune actualité pour : {query}"
    lines = [f"Actualités : {query}\n"]
    for i, r in enumerate(results, 1):
        title = r.get("title", "")
        if not title:
            continue
        src = f"  [{r['source']}]" if r.get("source") else ""
        lines.append(f"{i}. {title}{src}")
        if r.get("snippet"):
            lines.append(f"   {r['snippet'][:140]}")
        if r.get("url"):
            lines.append(f"   {r['url']}")
        lines.append("")
    return "\n".join(lines).strip()


DAILY_AI_CYBER_NEWS_QUERY = (
    "actualités aujourd'hui intelligence artificielle IA cybersécurité "
    "cyberattaques robotique technologies émergentes"
)


def _gemini_headlines_impl(n: int) -> tuple:
    from google import genai
    client = genai.Client(api_key=_get_api_key())
    chat = client.chats.create(
        model=BALANCED_MODEL,
        config={"tools": [{"google_search": {}}]},
    )
    response = chat.send_message(
        (
            f"Today's {n} most important and recent headlines about artificial "
            "intelligence, cybersecurity, cyberattacks, robotics and emerging "
            "technology. Prioritize events published today or in the last 24 hours, "
            "use reliable sources, avoid general politics and celebrity news unless "
            "directly relevant to AI or cyber. Numbered list, titles only."
        )
    )
    if not response.candidates:
        raise ValueError("Gemini n'a retourné aucun candidat pour les titres.")
    cand = response.candidates[0]
    if not cand.content or not cand.content.parts:
        raise ValueError("Réponse vide pour les titres.")
    raw = ""
    for part in cand.content.parts:
        if hasattr(part, "text") and part.text:
            raw += part.text
    headlines = []
    for line in raw.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        if not re.match(r'^[\d]+[.)-]', line):
            continue
        clean = re.sub(r'^[\d]+[.)-]\s*', '', line)
        clean = re.sub(r'^\*+\s*', '', clean).strip()
        if clean and len(clean) > 10:
            headlines.append(clean)
    return headlines[:n], raw.strip()


def _gemini_headlines(n: int = 5) -> tuple:
    return _run_with_timeout(_gemini_headlines_impl, n,
                             timeout=_time_left(_GEMINI_TIMEOUT))


# ── Modes ───────────────────────────────────────────────────────────────────
@kit.memo(_RESULT_TTL, key=lambda query, player=None, budget_s=15.0: query.casefold().strip())
def _search(query: str, player=None, budget_s: float = 15.0) -> str:
    print("[WebSearch] 🤖 Recherche standard avec Gemini Grounding...")
    if player:
        player.write_log("[Search:Gemini] Recherche standard...")

    return _gemini_search(query)


def _news(query: str, player=None) -> str:
    if _get_serpapi_api_key():
        try:
            return _serpapi_news(query or DAILY_AI_CYBER_NEWS_QUERY)
        except Exception as exc:
            print(f"[WebSearch] ⚠️ actualités SerpApi indisponibles ({exc}), repli Gemini")
    print("[WebSearch] 📰 Recherche d'actualités avec Gemini Grounding...")
    if player:
        player.write_log("[Search:Gemini] Recherche d'actualités...")
    gemini_query = (
        f"dernières actualités : {query}" if query else
        "Actualités des dernières 24 heures sur l'intelligence artificielle, "
        "la cybersécurité, les cyberattaques, la robotique et les technologies "
        "émergentes. Sources fiables, faits récents et vérifiables uniquement."
    )
    return _gemini_search(gemini_query)


_news = kit.memo(_RESULT_TTL, key=lambda query, player=None: ("news", query.casefold().strip()))(_news)


def _headlines(count: int = 5, player=None) -> str:
    if _get_serpapi_api_key():
        try:
            return _serpapi_headlines(count)
        except Exception as exc:
            print(f"[WebSearch] ⚠️ titres SerpApi indisponibles ({exc}), repli Gemini")
    print("[WebSearch] 📰 Titres IA & cyber du jour avec Gemini Grounding...")
    if player:
        player.write_log("[Search:Gemini] Titres du jour...")
    try:
        headlines, raw = _gemini_headlines(count)
        if headlines:
            return "📰 Actualités IA & cyber du jour :\n" + "\n".join(
                f"{i}. {h}" for i, h in enumerate(headlines, 1)
            )
        return raw or "Aucun titre trouvé."
    except Exception as e:
        return f"Titres Gemini indisponibles : {e}"


@kit.memo(_RESULT_TTL, key=lambda query, player=None, budget_s=15.0: ("research", query.casefold().strip()))
def _research(query: str, player=None, budget_s: float = 15.0) -> str:
    print("[WebSearch] 🔬 Recherche approfondie avec Gemini Grounding...")
    if player:
        player.write_log("[Search:Gemini] Recherche approfondie...")
    research_query = (
        f"Explication complète et détaillée de : {query}. "
        "Inclus le contexte, les faits clés, l'état actuel et les nuances importantes."
    )

    return _gemini_search(research_query)


def _price(query: str, player=None) -> str:
    print("[WebSearch] 🏷️ Recherche de prix avec Gemini Grounding...")
    if player:
        player.write_log("[Search:Gemini] Recherche de prix...")
    price_query = f"prix actuel de {query} — combien ça coûte aujourd'hui"
    return _gemini_search(price_query)


def _compare(items: list, aspect: str, player=None) -> str:
    print(f"[WebSearch] ⚖️ Comparaison de {', '.join(items)} avec Gemini Grounding...")
    if player:
        player.write_log("[Search:Gemini] Comparaison...")
    query = (
        f"Compare {', '.join(items)} en termes de {aspect}. "
        "Donne des faits précis et des données."
    )
    return _gemini_search(query)


# ── Parsing local intelligent ──────────────────────────────────────────────
def _parse_search_request_locally(text: str) -> Optional[Dict[str, Any]]:
    social = _social_profile_intent(text)
    if social:
        return {"mode": "social", **social, "query": social["handle"]}

    text = text.lower().strip()
    text = re.sub(r"\b(s'il te pla[iî]t|stp|please|peux-tu|tu peux|je veux|j'aimerais)\b",
                  "", text).strip()

    # Mode nearby / lieu le plus proche : nécessite une recherche locale
    # géolocalisée (Google Local), pas une recherche web classique — sinon
    # Google renvoie les pages les mieux référencées globalement (souvent
    # françaises) au lieu des établissements réels près de l'utilisateur.
    m = re.search(
        r"(?:o[uù]\s+(?:se\s+trouve|est|puis-je\s+trouver)\s+)?"
        r"(?:l'|le\s+|la\s+|les\s+|un\s+|une\s+|des\s+)?(.+?)\s+"
        r"(?:le\s+plus\s+proche|la\s+plus\s+proche|les\s+plus\s+proches|"
        r"à\s+proximit[ée]|autour\s+de\s+moi|pr[eè]s\s+de\s+moi|"
        r"pas\s+loin\s+d['e]|near\s+me|closest|nearby)\b",
        text,
    )
    if m and m.group(1).strip(" ,.'"):
        return {"mode": "nearby", "query": m.group(1).strip(" ,.'")}

    # Mode headlines / titres du jour
    if re.search(r"\b(titres?\s+du\s+jour|headlines|à la une|unes? du jour|principales? nouvelles?)\b", text):
        m = re.search(r"(\d+)", text)
        count = int(m.group(1)) if m else 5
        return {"mode": "headlines", "count": count}

    # Mode news / actualités
    if re.search(r"\b(actualités?|nouvelles?|news|infos?|derni[eè]re[s]?|breaking)\b", text):
        m = re.search(r"(?:sur |à propos de |de |d'|sur le |sur la )(.+)", text)
        query = m.group(1).strip() if m else ""
        if query in {"jour", "du jour", "aujourd'hui", "aujourdhui", "today"}:
            query = ""
        return {"mode": "news", "query": query}

    # Mode price
    if re.search(r"\b(prix|coûte|coût|combien|tarif|acheter|valeur|price|cost)\b", text):
        m = re.search(r"(?:prix de |coût de |combien coûte |tarif de |acheter |price of |cost of )(.+)", text)
        if m:
            return {"mode": "price", "query": m.group(1).strip()}
        rest = re.sub(r"\b(quel est le |quel est l'|combien |coûte |prix )", "", text).strip()
        if rest:
            return {"mode": "price", "query": rest}

    # Mode research / explication
    if re.search(r"\b(explique|explique-moi|recherche approfondie|deep dive|en détail|analyse|"
                 r"comprends|comprendre|qu'est-ce que|qu'est-ce qu'|c'est quoi|défini[st])\b", text):
        m = re.search(r"(?:explique|explique-moi|recherche approfondie sur|en détail sur|"
                      r"analyse de|qu'est-ce que|c'est quoi) (.+)", text)
        query = m.group(1).strip() if m else ""
        return {"mode": "research", "query": query}

    # Mode compare
    if re.search(r"\b(compare|comparaison|vs|versus|différence entre)\b", text):
        items = []
        aspect = "general"
        m = re.search(r"(?:compare|comparaison|différence entre)\s+(.+?)"
                      r"(?:\s+(?:en|en termes de|sur le plan|au niveau)\s+(.+))?$", text)
        if m:
            raw_items = m.group(1).strip()
            aspect = m.group(2).strip() if m.lastindex >= 2 and m.group(2) else aspect
            items = re.split(r"\s+(?:et|vs|versus|,)\s+", raw_items)
            items = [i.strip() for i in items if i.strip()]
        if len(items) >= 2:
            return {"mode": "compare", "items": items, "aspect": aspect}

    # Mode search par défaut
    m = re.search(r"(?:cherche|recherche|search|google|trouve|find|cherche moi|recherche pour)\s+(.+)", text)
    if m:
        return {"mode": "search", "query": m.group(1).strip()}
    return None


def _detect_search_intent_ai(description: str) -> Optional[Dict]:
    api_key = _get_api_key()
    if not api_key:
        return None
    try:
        client = _gemini_client()
        prompt = (
            f"Analyse la phrase suivante et détermine le mode de recherche et les paramètres. "
            f"Retourne UNIQUEMENT un objet JSON avec les clés nécessaires.\n"
            f"Modes possibles : search (query), social (query, platform : tiktok | instagram | youtube | facebook | x | twitch | snapchat), news (query), research (query), price (query), "
            f"nearby (query — un lieu/commerce/service physique proche de l'utilisateur, "
            f"ex. 'hôpital le plus proche', 'pharmacie près de moi'), "
            f"compare (items, aspect), headlines (count).\n"
            f"Phrase : \"{description}\""
        )
        resp = client.models.generate_content(model=FAST_MODEL, contents=prompt)
        json_match = re.search(r'{.*}', resp.text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group(0))
    except Exception as e:
        print(f"[WebSearch] Erreur IA : {e}")
    return None


# ── Point d'entrée unifié ──────────────────────────────────────────────────
@kit.action("web_search")
def web_search(
    parameters:     dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    """
    Recherche web intelligente avec sélection automatique du mode.
    Modes : search, social, news, research, price, nearby, compare, headlines.
    """
    try:
        budget_s = float((parameters or {}).get("_budget_s") or 13.0)
    except (TypeError, ValueError):
        budget_s = 13.0
    token = _deadline.set(time.monotonic() + budget_s)
    try:
        return _web_search(parameters, player)
    finally:
        _deadline.reset(token)


def _web_search(parameters: dict, player=None) -> str:
    params = parameters or {}
    description = params.get("description", "").strip()
    query  = params.get("query", "").strip()
    mode   = params.get("mode", "search").lower().strip()
    items  = params.get("items", [])
    aspect = params.get("aspect", "general").strip() or "general"
    count  = int(params.get("count", 5))
    platform = str(params.get("platform", "")).casefold().strip()
    queries = params.get("queries") or []

    # Interprétation de la description naturelle
    if description and not query and not items:
        local = _parse_search_request_locally(description)
        if local:
            mode   = local.get("mode", mode)
            query  = local.get("query", query)
            items  = local.get("items", items)
            aspect = local.get("aspect", aspect)
            count  = local.get("count", count)
            platform = local.get("platform", platform)
        else:
            try:
                ai = _run_with_timeout(_detect_search_intent_ai, description,
                                       timeout=_time_left(_INTENT_TIMEOUT))
            except Exception as exc:
                print(f"[WebSearch] analyse d'intention abandonnée ({exc})")
                ai = None
            if ai:
                mode   = ai.get("mode", mode)
                query  = ai.get("query", query)
                items  = ai.get("items", items)
                aspect = ai.get("aspect", aspect)
                count  = ai.get("count", count)
                platform = ai.get("platform", platform)
            else:
                mode = "search"
                query = description

    # Les anciens appels d'outil transmettent parfois directement
    # `query="tiktokeur techenclair"` plutôt qu'une description. On les
    # transforme ici afin que cette amélioration bénéficie aussi aux agents
    # MCP et aux modèles déjà en cours de session.
    if mode == "search":
        social = _social_profile_intent(query)
        if social:
            mode = "social"
            query = social["handle"]
            platform = social["platform"]

    if not query and queries:
        query = ", ".join(str(q) for q in queries if q)
    if not query and not items and mode != "headlines":
        return "Veuillez fournir une requête de recherche."

    if items and mode not in ("compare",):
        mode = "compare"

    subjects = _clean_subjects(queries, query)
    label = query or ", ".join(items)
    print(f"[WebSearch] 🌐 mode={mode!r} sujets={subjects or items!r}")
    if player:
        player.write_log(f"[Search:{mode}] {' | '.join(subjects) if subjects else label}")

    global _last_card_markdown
    try:
        if mode == "social":
            return _social_profiles(query, platform, player=player)
        if mode == "headlines":
            return _headlines(count, player=None)
        if mode in ("search", "news", "research") and subjects:
            # Recherche fraîche : un sujet par requête, actualités datées
            # et web en parallèle, repli automatique si SerpApi tombe.
            text, card = _fresh_search(subjects, mode, _time_left(60.0))
            _last_card_markdown = card or None
            return text
        if not _get_serpapi_api_key():
            raise RuntimeError("clé SerpApi absente")
        if mode == "compare" and items:
            return _serpapi_compare(items, aspect)
        if mode == "price":
            return _serpapi_price(query)
        if mode == "nearby":
            return _serpapi_nearby(query)
        data = _call_serpapi({"q": query, "engine": "google", **_web_geo(query)})
        _last_card_markdown = format_results_markdown(data)
        return _format_serpapi_results(data)
    except Exception as e:
        # Ne pas lancer un deuxième modèle texte ici : un repli local ferait
        # répondre un autre modèle que Gemini Live. La vraie panne est dite
        # telle quelle, sans renvoyer vers un outil qui n'existe pas.
        print(f"[WebSearch] ⚠️ Échec de la recherche ({e})")
        return (
            f"Échec: la recherche web n'a pas abouti ({_redact_serpapi_error(e)}). "
            "Dis-le simplement à l'utilisateur et propose de réessayer ; "
            "ne donne aucun fait non vérifié."
        )
