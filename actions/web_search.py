"""
web_search.py — Recherche web ultra-réaliste
Parsing local avancé, modes automatiques, fallback IA, parallélisme optimisé.
Utilise Gemini grounding + DuckDuckGo en secours + SerpApi si clé présente.
"""

import json
import os
import random
import re
import sys
import threading
import time
import unicodedata
import atexit
import warnings
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FutureTimeout
from pathlib import Path
from typing import Optional, Dict, Any, Tuple

from core import action_kit as kit
from core.live_model_policy import BALANCED_MODEL, FAST_MODEL

# Vérification de la dépendance requests
try:
    import requests
    _REQUESTS = True
except ImportError:
    _REQUESTS = False

# Supprimer les warnings de déprécation de paquets renommés
warnings.filterwarnings("ignore", message="This package.*has been renamed", category=RuntimeWarning)

_GEMINI_TIMEOUT = 8.0
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


def _run_with_timeout(fn, *args, timeout: float = _GEMINI_TIMEOUT, **kwargs):
    future = _executor.submit(fn, *args, **kwargs)
    try:
        return future.result(timeout=timeout)
    except _FutureTimeout:
        raise TimeoutError(f"L'appel Gemini a dépassé {timeout}s")


# Une même question posée deux fois dans la conversation (« et donc ? »,
# reformulation par le modèle) ne repaie ni Gemini ni DuckDuckGo.
_RESULT_TTL = 90.0
_HEDGE_DELAY = 2.5  # Gemini répond en général sous 2 s ; au-delà on couvre.


def _hedged(primary, fallback, *, timeout: float, hedge_after: float = _HEDGE_DELAY):
    """Requête couverte : `fallback` démarre si `primary` traîne.

    Avant, l'assistant attendait l'échec complet de Gemini (jusqu'à 8 s) avant
    de seulement *commencer* DuckDuckGo. Ici le repli part dès que Gemini
    dépasse `hedge_after`, et le premier résultat exploitable gagne — Gemini
    reste prioritaire s'il arrive dans la fenêtre.
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


def _call_serpapi(params: dict) -> dict:
    if not _REQUESTS:
        raise RuntimeError("requests non installé. Exécutez : pip install requests")
    key = _get_serpapi_api_key()
    if not key:
        raise ValueError("Clé SerpApi absente (SERPAPI_API_KEY ou config/api_keys.json)")
    params["api_key"] = key
    if "hl" not in params or "gl" not in params:
        params.update({k: v for k, v in _geo_params("").items() if k not in params})
    r = kit.http().get("https://serpapi.com/search.json", params=params, timeout=8)
    r.raise_for_status()
    return r.json()


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


def _serpapi_news(query: str) -> str:
    geo = _geo_params(query)
    data = _call_serpapi({"q": query, "engine": "google_news", **geo})
    news = data.get("news_results", [])
    if not news:
        data = _call_serpapi({"q": f"{query} news", "engine": "google", **geo})
        news = data.get("news_results", []) or data.get("organic_results", [])
    if not news:
        return f"Aucune actualité trouvée pour : {query}"
    lines = [f"📰 Dernières actualités pour : {query}\n"]
    for i, n in enumerate(news[:8], 1):
        title = n.get("title")
        link = n.get("link")
        snippet = n.get("snippet") or n.get("description")
        source_raw = n.get("source")
        if isinstance(source_raw, list):
            source = source_raw[0].get("name", "") if source_raw and isinstance(source_raw[0], dict) else ""
        elif isinstance(source_raw, dict):
            source = source_raw.get("name", "")
        else:
            source = source_raw or ""
        date = n.get("date") or n.get("published_date")
        src_str = f" [{source}]" if source else ""
        date_str = f" ({date})" if date else ""
        lines.append(f"{i}. {title}{src_str}{date_str}")
        if snippet:
            lines.append(f"   {snippet}")
        if link:
            lines.append(f"   Lien : {link}")
        lines.append("")
    return "\n".join(lines).strip()


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


def _serpapi_nearby(query: str) -> str:
    """Lieux à proximité (pharmacie, hôpital, restaurant…) — recherche
    locale (Google Local) plutôt que web classique, seule capable de
    remonter de vrais établissements géolocalisés au lieu de pages
    génériques les mieux référencées globalement."""
    geo = _geo_params(query)
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


# ── Backends Gemini & DuckDuckGo ────────────────────────────────────────────
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


def _ddgs_client():
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            import warnings as _w
            with _w.catch_warnings():
                _w.simplefilter("ignore")
                from duckduckgo_search import DDGS
        except ImportError:
            raise ImportError("Installez ddgs: pip install ddgs")
    try:
        return DDGS(timeout=6)
    except TypeError:
        return DDGS()


def _ddg_with_retry(fn, *args, retries: int = 1, base_delay: float = 0.5, **kwargs):
    import time as _time
    last_exc = None
    for attempt in range(retries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last_exc = e
            is_ratelimit = "ratelimit" in str(e).lower() or "403" in str(e)
            if not is_ratelimit or attempt == retries:
                raise
            delay = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
            print(f"[WebSearch] ⏳ DDG rate-limité, nouvelle tentative dans {delay:.1f}s "
                  f"({attempt + 1}/{retries})...")
            _time.sleep(delay)
    raise last_exc


def _ddg_search(query: str, max_results: int = 6) -> list:
    def _do():
        results = []
        with _ddgs_client() as ddgs:
            for r in ddgs.text(query, max_results=max_results):
                results.append({
                    "title":   r.get("title", ""),
                    "snippet": r.get("body", ""),
                    "url":     r.get("href", ""),
                })
        return results
    return _ddg_with_retry(_do)


def _ddg_news(query: str, max_results: int = 8) -> list:
    def _do():
        results = []
        with _ddgs_client() as ddgs:
            for r in ddgs.news(query, max_results=max_results):
                results.append({
                    "title":   r.get("title", ""),
                    "snippet": r.get("body", ""),
                    "url":     r.get("url", ""),
                    "source":  r.get("source", ""),
                })
        return results
    try:
        return _ddg_with_retry(_do)
    except Exception as e:
        print(f"[WebSearch] ⚠️ DDG news() échoué ({e}) — repli sur recherche texte")
        return _ddg_search(query, max_results=max_results)


def _format_ddg(query: str, results: list) -> str:
    if not results:
        return f"Aucun résultat pour : {query}"
    lines = [f"Résultats pour : {query}\n"]
    for i, r in enumerate(results, 1):
        if r.get("title"):   lines.append(f"{i}. {r['title']}")
        if r.get("snippet"): lines.append(f"   {r['snippet']}")
        if r.get("url"):     lines.append(f"   Source : {r['url']}")
        lines.append("")
    return "\n".join(lines).strip()


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
    """Cherche un pseudo sur un réseau, avec SerpApi puis DDG en repli."""
    platform = platform if platform in _SOCIAL_PLATFORMS else "tiktok"
    query = _social_search_query(handle, platform)
    if player:
        player.write_log(f"[Search:Social:{platform}] @{handle}")
    try:
        if _get_serpapi_api_key():
            data = _call_serpapi({"q": query, "engine": "google", **_geo_params("")})
            results = data.get("organic_results", [])
        else:
            results = _ddg_search(query, max_results=8)
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
    return _run_with_timeout(_gemini_headlines_impl, n)


# ── Modes ───────────────────────────────────────────────────────────────────
@kit.memo(_RESULT_TTL, key=lambda query, player=None, budget_s=15.0: query.casefold().strip())
def _search(query: str, player=None, budget_s: float = 15.0) -> str:
    print("[WebSearch] 🤖 Recherche standard avec Gemini Grounding (couverte DDG)...")
    if player:
        player.write_log("[Search:Gemini] Recherche standard...")

    def _ddg() -> str:
        if player:
            player.write_log("[Search:DuckDuckGo] Couverture DuckDuckGo...")
        return _format_ddg(query, _ddg_search(query))

    # Appel direct de l'implémentation : `_hedged` borne déjà le temps, et un
    # `submit` imbriqué dans le même pool pourrait l'épuiser.
    return _hedged(lambda: _gemini_search_impl(query), _ddg,
                   timeout=min(_GEMINI_TIMEOUT + 4.0, max(1.0, budget_s)))


def _news(query: str, player=None) -> str:
    print("[WebSearch] 📰 Recherche d'actualités avec Gemini Grounding & DuckDuckGo...")
    if player:
        player.write_log("[Search:Gemini&DDG] Recherche d'actualités...")
    gemini_query = (
        f"dernières actualités : {query}" if query else
        "Actualités des dernières 24 heures sur l'intelligence artificielle, "
        "la cybersécurité, les cyberattaques, la robotique et les technologies "
        "émergentes. Sources fiables, faits récents et vérifiables uniquement."
    )
    ddg_query = query if query else DAILY_AI_CYBER_NEWS_QUERY
    result_box  = [None]
    lock        = threading.Lock()
    done_evt    = threading.Event()
    failures    = [0]

    def _store(r: str) -> None:
        if r and len(r) > 60:
            with lock:
                if result_box[0] is None:
                    result_box[0] = r
            done_evt.set()
        else:
            with lock:
                failures[0] += 1
                if failures[0] >= 2:
                    done_evt.set()

    def _try_gemini():
        try:
            _store(_gemini_search(gemini_query))
        except Exception as e:
            print(f"[WebSearch] ⚠️ Gemini news échoué ({e})")
            _store("")

    def _try_ddg():
        try:
            results = _ddg_news(ddg_query, max_results=8)
            _store(_format_news(ddg_query, results))
        except Exception as e:
            print(f"[WebSearch] ⚠️ DDG news échoué ({e})")
            _store("")

    threading.Thread(target=_try_gemini, daemon=True).start()
    threading.Thread(target=_try_ddg,    daemon=True).start()
    done_evt.wait(timeout=10.0)
    return result_box[0] or f"Aucune actualité trouvée pour : {query}"


_news = kit.memo(_RESULT_TTL, key=lambda query, player=None: ("news", query.casefold().strip()))(_news)


def _headlines(count: int = 5, player=None) -> str:
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
        print(f"[WebSearch] ⚠️ Titres Gemini échoué ({e}) — repli DDG")
        if player:
            player.write_log("[Search:DuckDuckGo] Repli sur DuckDuckGo...")
        results = _ddg_news(DAILY_AI_CYBER_NEWS_QUERY, max_results=count)
        return _format_news("IA & cybersécurité — titres du jour", results)


@kit.memo(_RESULT_TTL, key=lambda query, player=None, budget_s=15.0: ("research", query.casefold().strip()))
def _research(query: str, player=None, budget_s: float = 15.0) -> str:
    print("[WebSearch] 🔬 Recherche approfondie avec Gemini Grounding (couverte DDG)...")
    if player:
        player.write_log("[Search:Gemini] Recherche approfondie...")
    research_query = (
        f"Explication complète et détaillée de : {query}. "
        "Inclus le contexte, les faits clés, l'état actuel et les nuances importantes."
    )

    def _ddg() -> str:
        if player:
            player.write_log("[Search:DuckDuckGo] Couverture DuckDuckGo...")
        return _format_ddg(query, _ddg_search(query, max_results=10))

    # Une recherche approfondie mérite un peu plus de patience côté Gemini.
    return _hedged(lambda: _gemini_search_impl(research_query), _ddg,
                   timeout=min(_GEMINI_TIMEOUT + 8.0, max(1.0, budget_s)), hedge_after=4.0)


def _price(query: str, player=None) -> str:
    print("[WebSearch] 🏷️ Recherche de prix avec Gemini Grounding...")
    if player:
        player.write_log("[Search:Gemini] Recherche de prix...")
    price_query = f"prix actuel de {query} — combien ça coûte aujourd'hui"
    try:
        return _gemini_search(price_query)
    except Exception as e:
        print(f"[WebSearch] ⚠️ Gemini price échoué ({e}) — DuckDuckGo...")
        if player:
            player.write_log("[Search:DuckDuckGo] Repli sur DuckDuckGo...")
        results = _ddg_search(f"{query} prix achat", max_results=6)
        return _format_ddg(query, results)


def _compare(items: list, aspect: str, player=None) -> str:
    print(f"[WebSearch] ⚖️ Comparaison de {', '.join(items)} avec Gemini Grounding...")
    if player:
        player.write_log("[Search:Gemini] Comparaison...")
    query = (
        f"Compare {', '.join(items)} en termes de {aspect}. "
        "Donne des faits précis et des données."
    )
    try:
        return _gemini_search(query)
    except Exception as e:
        print(f"[WebSearch] ⚠️ Gemini compare échoué : {e} — repli DDG")
        if player:
            player.write_log("[Search:DuckDuckGo] Repli sur DuckDuckGo...")
        all_results: dict = {}
        for item in items:
            try:
                all_results[item] = _ddg_search(f"{item} {aspect}", max_results=3)
            except Exception:
                all_results[item] = []
        lines = [f"Comparaison — {aspect.upper()}", "─" * 40]
        for item in items:
            lines.append(f"\n▸ {item}")
            for r in all_results.get(item, [])[:2]:
                if r.get("snippet"):
                    lines.append(f"  • {r['snippet']}")
                if r.get("url"):
                    lines.append(f"    {r['url']}")
        return "\n".join(lines)


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
        from google import genai
        client = genai.Client(api_key=api_key)
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
    params = parameters or {}
    try:
        budget_s = max(1.0, float(params.get("_budget_s", 15.0)))
    except (TypeError, ValueError):
        budget_s = 15.0
    description = params.get("description", "").strip()
    query  = params.get("query", "").strip()
    mode   = params.get("mode", "search").lower().strip()
    items  = params.get("items", [])
    aspect = params.get("aspect", "general").strip() or "general"
    count  = int(params.get("count", 5))
    platform = str(params.get("platform", "")).casefold().strip()

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
            ai = _detect_search_intent_ai(description)
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

    if not query and not items and mode != "headlines":
        return "Veuillez fournir une requête de recherche."

    if items and mode not in ("compare",):
        mode = "compare"

    # Routage via SerpApi si la clé API est disponible
    serpapi_key = _get_serpapi_api_key()
    if serpapi_key:
        print(f"[WebSearch] 🌐 Routage via SerpApi (mode={mode!r}  query={query!r})")
        if player:
            player.write_log(f"[Search:SerpApi:{mode}] {query or ', '.join(items)}")
        try:
            if mode == "social":
                return _social_profiles(query, platform, player=player)
            if mode == "compare" and items:
                return _serpapi_compare(items, aspect)
            if mode == "news":
                return _serpapi_news(query)
            if mode == "research":
                return _serpapi_research(query)
            if mode == "price":
                return _serpapi_price(query)
            if mode == "nearby":
                return _serpapi_nearby(query)
            data = _call_serpapi({"q": query, "engine": "google", **_geo_params(query)})
            global _last_card_markdown
            _last_card_markdown = format_results_markdown(data)
            return _format_serpapi_results(data)
        except Exception as e:
            print(f"[WebSearch] ⚠️ Échec de l'appel SerpApi ({e}) — repli sur les backends standards...")

    if player:
        player.write_log(f"[Search:{mode}] {query or ', '.join(items)}")
    print(f"[WebSearch] 🔍 mode={mode!r}  query={query!r}")

    try:
        if mode == "social":
            return _social_profiles(query, platform, player=player)
        if mode == "compare" and items:
            return _compare(items, aspect, player=player)
        if mode == "news":
            return _news(query, player=player)
        if mode == "research":
            return _research(query, player=player, budget_s=budget_s)
        if mode == "price":
            return _price(query, player=player)
        if mode == "headlines":
            return _headlines(count, player=player)
        return _search(query, player=player, budget_s=budget_s)
    except Exception as e:
        print(f"[WebSearch] ❌ Tous les backends ont échoué : {e}")
        return f"Échec de la recherche : {e}"
