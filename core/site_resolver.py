"""Résolution « nom de site → vraie URL » pour l'ouverture dans Chrome.

Le modèle vocal devine souvent bien (« Google Vids » → vids.google.com),
mais une adresse inventée ouvre une page d'erreur. Ordre de résolution, du
plus rapide au plus coûteux :

1. catalogue des services connus (instantané) ;
2. sites déjà résolus avec succès (mémoire sur disque, instantané) ;
3. l'URL proposée, si son domaine existe et que la page n'est pas une 404
   (DNS + requête courte, résultats mis en cache) ;
4. recherche du site officiel (SerpApi, sinon DuckDuckGo) ;
5. en dernier recours, une recherche Google — jamais une page morte.
"""
from __future__ import annotations

import json
import re
import socket
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FutureTimeout
from dataclasses import dataclass
from difflib import get_close_matches
from pathlib import Path
from urllib.parse import quote_plus, urlparse

from core import action_kit as kit

_BASE = Path(__file__).resolve().parent.parent
_LEARNED_PATH = _BASE / "memory" / "learned_sites.json"

_DNS_TIMEOUT = 2.0
_PAGE_TIMEOUT = (1.5, 2.0)
_PAGE_BUDGET = 2.2
# Budget total pour un site inconnu (une seule fois : le résultat est mémorisé).
_LOOKUP_TIMEOUT = 4.5

_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="site-resolve")


@dataclass(frozen=True)
class Resolved:
    url: str
    label: str      # nom lisible à annoncer (« Google Vids »)
    how: str        # catalog | learned | checked | lookup | search | raw


# Noms tels qu'on les prononce → URL officielle. Clés normalisées par
# `_norm` (minuscules, sans accents ni ponctuation). Plusieurs clés peuvent
# pointer vers la même adresse.
_CATALOG: dict[str, tuple[str, str]] = {}


def _add(url: str, label: str, *names: str) -> None:
    for name in (label, *names):
        _CATALOG[_norm(name)] = (url, label)


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", (text or "").casefold())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# ── Google ────────────────────────────────────────────────────────────────
_add("https://www.google.com", "Google", "google search")
_add("https://mail.google.com", "Gmail", "google mail", "mes mails", "ma boite mail")
_add("https://drive.google.com", "Google Drive", "drive", "mon drive")
_add("https://docs.google.com/document/", "Google Docs", "docs", "google doc")
_add("https://docs.google.com/spreadsheets/", "Google Sheets", "sheets", "google sheet", "tableur google")
_add("https://docs.google.com/presentation/", "Google Slides", "slides", "google slide")
_add("https://docs.google.com/forms/", "Google Forms", "forms", "google form", "formulaire google")
_add("https://vids.google.com", "Google Vids", "vids")
_add("https://calendar.google.com", "Google Agenda", "google calendar", "agenda google", "calendrier google")
_add("https://meet.google.com", "Google Meet", "meet")
_add("https://keep.google.com", "Google Keep", "keep")
_add("https://photos.google.com", "Google Photos", "photos google")
_add("https://maps.google.com", "Google Maps", "maps", "google map")
_add("https://translate.google.com", "Google Traduction", "google translate", "traducteur google", "traduction")
_add("https://news.google.com", "Google Actualités", "google news")
_add("https://www.google.com/imghp", "Google Images", "images google")
_add("https://scholar.google.com", "Google Scholar", "google scholar")
_add("https://gemini.google.com", "Gemini", "google gemini", "bard")
_add("https://notebooklm.google.com", "NotebookLM", "notebook lm", "notebooklm")
_add("https://aistudio.google.com", "Google AI Studio", "ai studio", "aistudio")
_add("https://labs.google", "Google Labs", "labs google")
_add("https://labs.google/fx/tools/flow", "Google Flow", "flow")
_add("https://labs.google/fx/tools/whisk", "Whisk", "google whisk")
_add("https://sites.google.com", "Google Sites", "sites google")
_add("https://classroom.google.com", "Google Classroom", "classroom")
_add("https://chat.google.com", "Google Chat", "google chat")
_add("https://myaccount.google.com", "Compte Google", "mon compte google", "google account")
_add("https://one.google.com", "Google One", "google one")
_add("https://play.google.com", "Google Play", "play store", "google play store")
_add("https://console.cloud.google.com", "Google Cloud Console", "google cloud", "gcp", "cloud console")
_add("https://console.firebase.google.com", "Firebase", "firebase console")
_add("https://analytics.google.com", "Google Analytics", "analytics")
_add("https://search.google.com/search-console", "Search Console", "google search console")
_add("https://ads.google.com", "Google Ads", "adwords")
_add("https://trends.google.com", "Google Trends", "google tendances")
_add("https://www.youtube.com", "YouTube", "youtube", "you tube")
_add("https://studio.youtube.com", "YouTube Studio", "youtube studio", "studio youtube")
_add("https://music.youtube.com", "YouTube Music", "youtube music")

# ── IA ────────────────────────────────────────────────────────────────────
_add("https://chatgpt.com", "ChatGPT", "chat gpt", "gpt", "openai chat")
_add("https://sora.com", "Sora", "openai sora")
_add("https://platform.openai.com", "OpenAI Platform", "openai platform", "api openai", "openai api")
_add("https://claude.ai", "Claude", "claude ai", "anthropic claude")
_add("https://console.anthropic.com", "Console Anthropic", "anthropic console", "api anthropic")
_add("https://www.perplexity.ai", "Perplexity", "perplexity ai")
_add("https://grok.com", "Grok", "grok ai", "xai grok")
_add("https://chat.mistral.ai", "Le Chat Mistral", "mistral", "le chat", "mistral ai")
_add("https://chat.deepseek.com", "DeepSeek", "deep seek")
_add("https://copilot.microsoft.com", "Copilot", "microsoft copilot", "bing chat")
_add("https://github.com/copilot", "GitHub Copilot", "github copilot")
_add("https://huggingface.co", "Hugging Face", "huggingface", "hugging face")
_add("https://www.midjourney.com", "Midjourney", "mid journey")
_add("https://elevenlabs.io", "ElevenLabs", "eleven labs")
_add("https://runwayml.com", "Runway", "runway ml")
_add("https://suno.com", "Suno", "suno ai")
_add("https://www.canva.com", "Canva", "canva")
_add("https://www.kaggle.com", "Kaggle", "kaggle")
_add("https://lmarena.ai", "LMArena", "lm arena", "chatbot arena")

# ── Réseaux sociaux & messagerie ──────────────────────────────────────────
_add("https://www.facebook.com", "Facebook", "fb")
_add("https://www.messenger.com", "Messenger", "facebook messenger")
_add("https://www.instagram.com", "Instagram", "insta")
_add("https://www.tiktok.com", "TikTok", "tik tok")
_add("https://www.tiktok.com/tiktokstudio", "TikTok Studio", "tiktok studio", "studio tiktok")
_add("https://x.com", "X", "twitter", "x twitter")
_add("https://www.linkedin.com", "LinkedIn", "linked in")
_add("https://www.snapchat.com", "Snapchat", "snap")
_add("https://www.reddit.com", "Reddit", "reddit")
_add("https://www.pinterest.com", "Pinterest", "pinterest")
_add("https://www.threads.net", "Threads", "threads")
_add("https://web.whatsapp.com", "WhatsApp Web", "whatsapp", "whatsapp web")
_add("https://web.telegram.org", "Telegram Web", "telegram")
_add("https://discord.com/app", "Discord", "discord")
_add("https://www.twitch.tv", "Twitch", "twitch")

# ── Travail, dev ──────────────────────────────────────────────────────────
_add("https://github.com", "GitHub", "git hub")
_add("https://gitlab.com", "GitLab", "git lab")
_add("https://stackoverflow.com", "Stack Overflow", "stackoverflow")
_add("https://vercel.com/dashboard", "Vercel", "vercel")
_add("https://supabase.com/dashboard", "Supabase", "supabase")
_add("https://www.figma.com", "Figma", "figma")
_add("https://www.notion.so", "Notion", "notion")
_add("https://trello.com", "Trello", "trello")
_add("https://outlook.live.com", "Outlook", "hotmail", "outlook mail")
_add("https://www.office.com", "Microsoft 365", "office", "office 365", "microsoft office")
_add("https://onedrive.live.com", "OneDrive", "one drive")
_add("https://teams.microsoft.com", "Microsoft Teams", "teams")
_add("https://www.dropbox.com", "Dropbox", "dropbox")
_add("https://zoom.us", "Zoom", "zoom")
_add("https://slack.com", "Slack", "slack")
_add("https://pub.dev", "pub.dev", "pub dev", "packages flutter")
_add("https://docs.flutter.dev", "Documentation Flutter", "flutter docs", "doc flutter")
_add("https://developer.mozilla.org", "MDN", "mdn", "mozilla developer")
_add("https://pypi.org", "PyPI", "pypi")
_add("https://www.npmjs.com", "npm", "npm")
_add("https://archlinux.org", "Arch Linux", "arch linux")
_add("https://wiki.archlinux.org", "Arch Wiki", "arch wiki", "wiki arch")
_add("https://aur.archlinux.org", "AUR", "aur")
_add("https://wiki.hypr.land", "Wiki Hyprland", "hyprland wiki", "hyprland")

# ── Divertissement, achats, infos ─────────────────────────────────────────
_add("https://www.netflix.com", "Netflix", "netflix")
_add("https://www.primevideo.com", "Prime Video", "amazon prime video", "prime")
_add("https://www.disneyplus.com", "Disney+", "disney plus", "disney")
_add("https://open.spotify.com", "Spotify", "spotify")
_add("https://www.deezer.com", "Deezer", "deezer")
_add("https://soundcloud.com", "SoundCloud", "sound cloud")
_add("https://www.amazon.fr", "Amazon", "amazon")
_add("https://www.aliexpress.com", "AliExpress", "ali express")
_add("https://www.jumia.com.gn", "Jumia Guinée", "jumia")
_add("https://www.ebay.fr", "eBay", "ebay")
_add("https://store.steampowered.com", "Steam", "steam")
_add("https://store.epicgames.com", "Epic Games", "epic games", "epic")
_add("https://fr.wikipedia.org", "Wikipédia", "wikipedia", "wiki")
_add("https://www.lemonde.fr", "Le Monde", "le monde")
_add("https://www.rfi.fr", "RFI", "rfi")
_add("https://www.france24.com", "France 24", "france 24")
_add("https://www.bbc.com", "BBC", "bbc")
_add("https://guineenews.org", "Guinéenews", "guinee news", "guineenews")
_add("https://www.africaguinee.com", "Africaguinée", "africa guinee")
_add("https://www.booking.com", "Booking", "booking")
_add("https://www.airbnb.fr", "Airbnb", "airbnb")
_add("https://www.paypal.com", "PayPal", "paypal")


# Domaine → nom lisible, pour annoncer « Google Vids » plutôt qu'un hôte.
_HOST_LABELS: dict[str, str] = {}
for _url, _label in _CATALOG.values():
    _HOST_LABELS.setdefault((urlparse(_url).hostname or "").removeprefix("www."), _label)


# ── Mémoire des résolutions réussies ─────────────────────────────────────
_learned_lock = threading.Lock()
_learned: dict[str, dict] | None = None


def _load_learned() -> dict[str, dict]:
    global _learned
    with _learned_lock:
        if _learned is None:
            try:
                _learned = json.loads(_LEARNED_PATH.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                _learned = {}
        return _learned


def _remember(name: str, url: str, label: str) -> None:
    key = _norm(name)
    if not key or key in _CATALOG:
        return
    data = _load_learned()
    with _learned_lock:
        data[key] = {"url": url, "label": label, "ts": int(time.time())}
        if len(data) > 500:
            for old in sorted(data, key=lambda k: data[k].get("ts", 0))[: len(data) - 500]:
                data.pop(old, None)
        try:
            tmp = _LEARNED_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(_LEARNED_PATH)
        except OSError:
            pass


def forget(name: str) -> bool:
    """Oublie une résolution apprise (si elle s'est révélée fausse)."""
    data = _load_learned()
    with _learned_lock:
        return data.pop(_norm(name), None) is not None


# ── Vérifications réseau (bornées, en cache) ─────────────────────────────
@kit.memo(600.0, key=lambda host: host)
def _host_exists(host: str) -> bool | None:
    """True/False si le DNS a répondu, None s'il est trop lent pour trancher."""
    fut = _pool.submit(socket.getaddrinfo, host, 443)
    try:
        return bool(fut.result(timeout=_DNS_TIMEOUT))
    except _FutureTimeout:
        return None
    except OSError:
        return False


def _probe(url: str) -> bool:
    try:
        r = kit.http().get(url, timeout=_PAGE_TIMEOUT, allow_redirects=True, stream=True)
        status = r.status_code
        r.close()
        return status not in (404, 410)
    except Exception as exc:
        name = type(exc).__name__
        return not any(k in name for k in ("ConnectionError", "SSLError", "InvalidURL"))


@kit.memo(600.0, key=lambda url: url)
def _page_alive(url: str) -> bool:
    """Faux seulement si le serveur dit clairement que la page n'existe pas
    (404/410, domaine injoignable). Un 403/429 anti-robot ou une réponse
    lente ne condamne pas l'URL : Chrome, lui, y arrivera."""
    fut = _pool.submit(_probe, url)
    try:
        return fut.result(timeout=_PAGE_BUDGET)
    except _FutureTimeout:
        return True


# ── Recherche du site officiel ───────────────────────────────────────────
_JUNK_HOSTS = (
    "wikipedia.org", "youtube.com", "facebook.com", "reddit.com", "quora.com",
    "linkedin.com", "twitter.com", "x.com", "instagram.com", "tiktok.com",
    "play.google.com", "apps.apple.com", "trustpilot.", "similarweb.com",
)


def _pick_official(rows: list[dict], name: str) -> str | None:
    """Premier résultat dont le DOMAINE porte le nom demandé. Un résultat qui
    parle du site sans en être un (article, annuaire) n'est jamais retenu :
    mieux vaut une recherche Google qu'un site faux."""
    words = [w for w in _norm(name).split() if len(w) > 2 or w.isdigit()]
    joined = "".join(words)
    for row in rows:
        link = row.get("link") or row.get("href") or ""
        host = (urlparse(link).hostname or "").casefold()
        if not host or any(j in host for j in _JUNK_HOSTS if not any(w in j for w in words)):
            continue
        flat = re.sub(r"[^a-z0-9]", "", host)
        if joined and joined in flat:
            return link
        if words and words[0] in flat and (len(words) == 1 or any(w in flat for w in words[1:])):
            return link
    for row in rows:
        link = row.get("link") or row.get("href") or ""
        flat = re.sub(r"[^a-z0-9]", "", (urlparse(link).hostname or "").casefold())
        if words and words[0] in flat:
            return link
    return None


def _lookup_official(name: str) -> str | None:
    # Le nom seul, géolocalisé : « site officiel » dans la requête fait
    # remonter des pages qui contiennent littéralement ces mots.
    query = name
    try:
        from actions.web_search import _call_serpapi, _get_serpapi_api_key, _web_geo
        if _get_serpapi_api_key():
            data = _call_serpapi({"q": query, "engine": "google", **_web_geo(query)},
                                 timeout=(2.0, 5.5))
            kg = data.get("knowledge_graph") or {}
            if kg.get("website"):
                return kg["website"]
            return _pick_official(data.get("organic_results") or [], name)
    except Exception as exc:
        print(f"[SiteResolver] ⚠️ SerpApi indisponible ({exc})")
    try:
        from ddgs import DDGS
        with DDGS(timeout=3) as d:
            return _pick_official(list(d.text(query, max_results=6)), name)
    except Exception:
        return None


def _root_of(url: str) -> str:
    """Page d'accueil d'un résultat : on ouvre le site, pas un sous-article.
    Les adresses de service à chemin (« /copilot ») viennent du catalogue."""
    p = urlparse(url)
    return f"{p.scheme or 'https'}://{p.netloc}"


def _pretty(name: str) -> str:
    return name if any(c.isupper() for c in name) else name.title()


def _guess_dotcom(simple: str) -> str | None:
    guess = f"https://www.{simple}.com"
    return guess if _host_exists(f"www.{simple}.com") and _page_alive(guess) else None


# ── Point d'entrée ───────────────────────────────────────────────────────
_URL_RE = re.compile(r"^(?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,}(?::\d+)?(?:[/?#]\S*)?$", re.I)
_FILLER_RE = re.compile(
    r"\b(?:le|la|les|l|site|web|page|officiel(?:le)?|de|du|des|dans|sur|avec|"
    r"chrome|google chrome|navigateur|internet|ouvre|ouvrir|va|aller|lance|mon|ma|mes|"
    r"moi|stp|un|une)\b"
)


def _clean_name(text: str) -> str:
    """« le site de la RTG » → « RTG » (casse d'origine conservée)."""
    words = [w for w in re.split(r"\s+", (text or "").strip()) if w]
    kept = [w for w in words if not _FILLER_RE.fullmatch(_norm(w) or "x")]
    return " ".join(kept) or (text or "").strip()


def _search_url(text: str) -> str:
    return "https://www.google.com/search?q=" + quote_plus(text)


def _catalog_match(name: str) -> Resolved | None:
    key = _norm(name)
    if not key:
        return None
    # « le site google vids » / « youtube studio stp »
    stripped = re.sub(r"\s+", " ", _FILLER_RE.sub(" ", key)).strip()
    for k in (key, stripped):
        if k and k in _CATALOG:
            return Resolved(*_CATALOG[k], "catalog")
    learned = _load_learned()
    for k in (key, stripped):
        if k and k in learned:
            return Resolved(learned[k]["url"], learned[k]["label"], "learned")
    # Petites fautes de transcription (« youtub », « linkdin »).
    close = get_close_matches(stripped or key, list(_CATALOG), n=1, cutoff=0.88)
    return Resolved(*_CATALOG[close[0]], "catalog") if close else None


def resolve(target: str, *, site: str = "") -> Resolved:
    """Transforme ce que le modèle ou l'utilisateur a donné en URL ouvrable."""
    raw = (target or "").strip().strip(" \t'\"")
    site = _clean_name(site) if site else ""
    name = site or raw
    if not raw and not name:
        return Resolved("about:blank", "", "raw")

    # Schémas locaux ou spéciaux : ne rien toucher.
    if re.match(r"^(?:file|about|chrome|data|view-source|mailto|tel):", raw, re.I):
        return Resolved(raw, raw, "raw")
    if re.match(r"^(?:https?://)?(?:localhost|127\.0\.0\.1|\[::1\]|\d{1,3}(?:\.\d{1,3}){3})(?::\d+)?", raw, re.I):
        url = raw if "://" in raw else "http://" + raw
        return Resolved(url, raw, "raw")

    # 1-2. Nom connu (catalogue ou appris) : instantané et sûr. Le nom dit
    # par l'utilisateur prime sur l'URL devinée par le modèle.
    for candidate in (site, raw if not _URL_RE.match(raw) else ""):
        hit = _catalog_match(candidate) if candidate else None
        if hit:
            return hit

    # 3. URL fournie : on la garde si son domaine existe et que la page vit.
    if _URL_RE.match(raw):
        url = raw if re.match(r"^https?://", raw, re.I) else "https://" + raw
        host = urlparse(url).hostname or ""
        exists = _host_exists(host) if host else False
        if exists is None:
            # DNS trop lent pour trancher : la page elle-même répond-elle ?
            exists = _page_alive(f"https://{host}")
        if exists:
            label = site or _HOST_LABELS.get(host.removeprefix("www.")) or host
            path = urlparse(url).path
            if path in ("", "/") or _page_alive(url):
                return Resolved(url, label, "checked")
            # Chemin mort sur un domaine valide : l'accueil du site, tout de
            # suite, plutôt qu'une page d'erreur ou une recherche lente.
            return Resolved(f"https://{host}", label, "checked")
        lookup_name = site or re.sub(r"^www\.", "", host).rsplit(".", 1)[0].replace(".", " ")
    else:
        lookup_name = _clean_name(name)

    # 4. Recherche du site officiel et, pour un seul mot (« kayak »), essai
    # du .com habituel — les deux en parallèle, sous un budget commun.
    deadline = time.monotonic() + _LOOKUP_TIMEOUT
    fut = _pool.submit(_lookup_official, lookup_name)
    simple = _norm(lookup_name).replace(" ", "")
    guess_fut = None
    if not _URL_RE.match(raw) and " " not in lookup_name and re.fullmatch(r"[a-z0-9-]{2,40}", simple or ""):
        guess_fut = _pool.submit(_guess_dotcom, simple)
    try:
        official = fut.result(timeout=max(0.1, deadline - time.monotonic()))
    except _FutureTimeout:
        official = None
    if official:
        url = _root_of(official)
        label = _pretty(lookup_name)
        _remember(lookup_name, url, label)
        if site and site != lookup_name:
            _remember(site, url, label)
        return Resolved(url, label, "lookup")
    if guess_fut is not None:
        try:
            guess = guess_fut.result(timeout=max(0.1, deadline - time.monotonic()))
        except _FutureTimeout:
            guess = None
        if guess:
            return Resolved(guess, _pretty(lookup_name), "checked")

    # 5. Jamais de page morte : une recherche Google sur le nom demandé.
    return Resolved(_search_url(name), name, "search")
