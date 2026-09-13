#!/usr/bin/env python3
"""Accès Gmail OAuth 2.0 fiable pour ANO-GPT.

Le service ne confond jamais une boîte vide avec une connexion absente. Il
expose un diagnostic exploitable, conserve le jeton avec des permissions 0600,
décode correctement les en-têtes MIME et parcourt les messages multipart imbriqués.
"""

from __future__ import annotations

import base64
import html
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence
import unicodedata
import webbrowser

_BASE_DIR = Path(__file__).resolve().parent.parent
_CREDENTIALS_DIR = _BASE_DIR / "memory" / "credentials"
_TOKEN_FILE = _BASE_DIR / "memory" / "gmail_token.json"
_CLIENT_SECRET_FILE = _CREDENTIALS_DIR / "gmail_client_secret.json"

# Lecture seule historiquement ; `gmail.modify` couvre la lecture, le marquage,
# l'archivage et la corbeille, `gmail.send` l'envoi. Un jeton ancien (lecture
# seule) reste utilisable pour lire : seules les écritures demandent de
# reconnecter le compte.
_SCOPE_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
_SCOPE_MODIFY = "https://www.googleapis.com/auth/gmail.modify"
_SCOPE_SEND = "https://www.googleapis.com/auth/gmail.send"
_SCOPES = [_SCOPE_MODIFY, _SCOPE_SEND]
_WRITE_SCOPES = {_SCOPE_MODIFY, _SCOPE_SEND}

# Veille : Gmail n'autorise pas de notification poussée sans Pub/Sub et une
# adresse publique. On interroge donc l'historique, qui ne renvoie que le delta
# et coûte un quota négligeable — assez souvent pour être vécu comme du direct.
_WATCH_INTERVAL = 10

# Mémoire des messages déjà annoncés. Bornée : la veille tourne des journées
# entières et un ensemble sans limite finit par peser.
_SEEN_LIMIT = 500

# Une recherche interactive doit rester rapide, même sur une boîte énorme. On
# récupère davantage de candidats que de résultats demandés pour pouvoir les
# classer, mais avec une borne dure sur les appels réseau.
_MAX_RESULTS = 100
_MAX_SEARCH_CANDIDATES = 150
_MAX_LIST_PAGES = 10

# Dossiers où atterrit une clé OAuth téléchargée depuis Google Cloud.
_DOWNLOAD_DIRS = ("Downloads", "Téléchargements", "Telechargements", "Bureau", "Desktop")


def _register_gmail_chrome() -> str:
    """Fournit à OAuth un contrôleur Chrome explicite, même sans navigateur par défaut."""
    executable = (
        shutil.which("google-chrome-stable")
        or shutil.which("google-chrome")
        or shutil.which("chrome")
        or shutil.which("chrome.exe")
    )
    if not executable:
        candidates = [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ]
        for variable in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
            root = os.environ.get(variable)
            if root:
                candidates.append(Path(root) / "Google/Chrome/Application/chrome.exe")
        executable = next((str(path) for path in candidates if path.is_file()), None)
    if not executable:
        raise GmailError(
            "Google Chrome est introuvable. La configuration Gmail est conservée ; "
            "installez Chrome puis redemandez « connecte Gmail »."
        )

    class GmailChrome(webbrowser.BackgroundBrowser):
        def open(self, url, new=0, autoraise=True):
            if not super().open(url, new=new, autoraise=autoraise):
                raise GmailError(
                    "Impossible de lancer Google Chrome pour l'autorisation Gmail. "
                    "La clé OAuth est conservée ; aucune réinstallation Gmail n'est nécessaire."
                )
            return True

    name = "anogpt-gmail-chrome"
    webbrowser.register(name, None, GmailChrome(executable))
    return name


SETUP_STEPS = (
    "1. Ouvrez console.cloud.google.com, créez (ou choisissez) un projet.",
    "2. « API et services » → « Bibliothèque » → activez l'API Gmail.",
    "3. « Écran de consentement OAuth » : type Externe, puis ajoutez votre "
    "adresse Gmail dans « Utilisateurs de test ».",
    "4. « Identifiants » → « Créer des identifiants » → « ID client OAuth » → "
    "type « Application de bureau » → Télécharger le JSON.",
    "5. Lancez : python scripts/setup_gmail.py "
    "(il trouve le fichier téléchargé tout seul).",
)


class GmailError(RuntimeError):
    """Erreur Gmail affichable à l'utilisateur."""


class GmailSetupRequired(GmailError):
    """L'autorisation OAuth n'est pas encore configurée."""


@dataclass(frozen=True)
class GmailSearchPlan:
    """Requête Gmail compilée et informations utiles pour son classement."""

    original_query: str
    gmail_query: str
    free_terms: tuple[str, ...] = ()
    interpreted: bool = False
    explanation: str = ""


_NATIVE_OPERATOR_RE = re.compile(
    r"(?<![\w-])-?(?:from|to|cc|bcc|subject|label|category|filename|after|before|"
    r"older|newer|older_than|newer_than|larger|smaller|size|rfc822msgid|"
    r"deliveredto|has|is|in):",
    re.IGNORECASE,
)


def _fold(value: str) -> str:
    """Minuscule sans accents, pratique pour comprendre la dictée française."""
    return "".join(
        char for char in unicodedata.normalize("NFKD", value.casefold())
        if not unicodedata.combining(char)
    )


def _gmail_quote(value: Any) -> str:
    cleaned = re.sub(r"[\x00-\x1f]+", " ", str(value or "")).strip()
    cleaned = cleaned.replace("\\", " ").replace('"', " ")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return f'"{cleaned}"'


def _parse_search_date(value: Any) -> str:
    """Accepte les dates usuelles et produit le format stable de Gmail."""
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y/%m/%d")
        except ValueError:
            continue
    raise GmailError(
        f"Date de recherche invalide : {text!r}. Utilisez AAAA-MM-JJ ou JJ/MM/AAAA."
    )


def _search_tokens(value: str) -> tuple[str, ...]:
    ignored = {
        "a", "au", "aux", "avec", "dans", "de", "des", "du", "email",
        "emails", "e-mail", "e-mails", "et", "la", "le", "les", "mail",
        "mails", "message", "messages", "mon", "mes", "qui", "sur", "un",
        "une", "the", "an", "and", "from",
    }
    tokens = re.findall(r"[\w@.+-]{2,}", _fold(value), re.UNICODE)
    return tuple(dict.fromkeys(token for token in tokens if token not in ignored))


def _query_free_terms(value: str) -> tuple[str, ...]:
    """Retire opérateurs et valeurs techniques avant le score plein texte."""
    operator = (
        r"(?:from|to|cc|bcc|subject|label|category|filename|after|before|older|"
        r"newer|older_than|newer_than|larger|smaller|size|rfc822msgid|"
        r"deliveredto|has|is|in)"
    )
    cleaned = re.sub(
        rf"(?<![\w-])-?{operator}:(?:\"[^\"]*\"|\S+)", " ", value,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\b(?:AND|OR)\b|[{}()]", " ", cleaned, flags=re.IGNORECASE)
    return _search_tokens(cleaned)


def build_gmail_search_query(
    query: str,
    filters: Optional[Mapping[str, Any]] = None,
    *,
    today: Optional[date] = None,
) -> GmailSearchPlan:
    """Compile une demande naturelle ou structurée en syntaxe Gmail.

    Une requête contenant déjà un opérateur Gmail reste intacte. Ce choix est
    essentiel pour les utilisateurs avancés : leur syntaxe ne doit jamais être
    « corrigée » de façon imprévisible.
    """
    original = re.sub(r"\s+", " ", str(query or "")).strip()
    if len(original) > 2048:
        raise GmailError("La recherche Gmail est trop longue (maximum 2 048 caractères).")
    structured = dict(filters or {})
    parts: List[str] = []
    explanations: List[str] = []
    remaining = original
    native = bool(_NATIVE_OPERATOR_RE.search(original))

    if native:
        if original:
            parts.append(original)
        free_terms = _query_free_terms(original)
    else:
        current_day = today or datetime.now(timezone.utc).date()

        def consume(pattern: str, replacement: Callable[[re.Match[str]], str], note: str) -> None:
            nonlocal remaining
            updated, count = re.subn(pattern, replacement, remaining, flags=re.IGNORECASE)
            if count:
                remaining = updated
                explanations.append(note)

        # Périodes calendaires exactes pour « aujourd'hui » et « hier ».
        consume(
            r"\b(?:aujourd'hui|today)\b",
            lambda _m: (
                f" after:{current_day:%Y/%m/%d} "
                f"before:{current_day + timedelta(days=1):%Y/%m/%d} "
            ),
            "période aujourd'hui",
        )
        yesterday = current_day - timedelta(days=1)
        consume(
            r"\b(?:hier|yesterday)\b",
            lambda _m: (
                f" after:{yesterday:%Y/%m/%d} before:{current_day:%Y/%m/%d} "
            ),
            "période hier",
        )
        week_start = current_day - timedelta(days=current_day.weekday())
        consume(
            r"\b(?:cette\s+semaine|this\s+week)\b",
            lambda _m: f" after:{week_start:%Y/%m/%d} ",
            "cette semaine",
        )
        month_start = current_day.replace(day=1)
        consume(
            r"\b(?:ce\s+mois|this\s+month)\b",
            lambda _m: f" after:{month_start:%Y/%m/%d} ",
            "ce mois",
        )
        year_start = current_day.replace(month=1, day=1)
        consume(
            r"\b(?:cette\s+ann(?:e|é)e|this\s+year)\b",
            lambda _m: f" after:{year_start:%Y/%m/%d} ",
            "cette année",
        )

        date_pattern = r"\d{1,2}[/-]\d{1,2}[/-]\d{4}|\d{4}[/-]\d{1,2}[/-]\d{1,2}"

        def between_dates(match: re.Match[str]) -> str:
            first = _parse_search_date(match.group("first"))
            second = datetime.strptime(
                _parse_search_date(match.group("second")), "%Y/%m/%d"
            ).date() + timedelta(days=1)
            return f" after:{first} before:{second:%Y/%m/%d} "

        consume(
            rf"\b(?:entre|between)\s+(?:le\s+)?(?P<first>{date_pattern})\s+"
            rf"(?:et|and)\s+(?:le\s+)?(?P<second>{date_pattern})\b",
            between_dates,
            "intervalle de dates",
        )
        consume(
            rf"\b(?:depuis|apr(?:e|è)s|after)\s+(?:le\s+)?(?P<value>{date_pattern})\b",
            lambda m: f" after:{_parse_search_date(m.group('value'))} ",
            "date minimale",
        )
        consume(
            rf"\b(?:avant|before)\s+(?:le\s+)?(?P<value>{date_pattern})\b",
            lambda m: f" before:{_parse_search_date(m.group('value'))} ",
            "date maximale",
        )

        units = {
            "jour": "d", "jours": "d", "day": "d", "days": "d",
            "semaine": "d", "semaines": "d", "week": "d", "weeks": "d",
            "mois": "m", "month": "m", "months": "m",
            "an": "y", "ans": "y", "annee": "y", "annees": "y",
            "year": "y", "years": "y",
        }

        def relative(match: re.Match[str]) -> str:
            count = max(1, min(int(match.group("count")), 999))
            unit_word = _fold(match.group("unit"))
            unit = units.get(unit_word, "d")
            if unit_word in {"semaine", "semaines", "week", "weeks"}:
                count *= 7
            return f" newer_than:{count}{unit} "

        consume(
            r"\b(?:depuis\s+les?|ces?|dans\s+les?|sur\s+les?|derniers?|last)\s+"
            r"(?P<count>\d{1,3})\s*(?P<unit>jours?|days?|semaines?|weeks?|mois|"
            r"months?|ans?|ann(?:e|é)es?|years?)\b",
            relative,
            "période relative",
        )
        consume(
            r"\b(?:des?|les?|ces?)\s+(?P<count>\d{1,3})\s+(?:derniers?|last)\s+"
            r"(?P<unit>jours?|days?|semaines?|weeks?|mois|months?|ans?|"
            r"ann(?:e|é)es?|years?)\b",
            relative,
            "période relative",
        )
        word_numbers = {
            "un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4,
            "cinq": 5, "six": 6, "sept": 7, "huit": 8, "neuf": 9,
            "dix": 10, "quinze": 15, "vingt": 20, "trente": 30,
            "quarante": 40, "cinquante": 50, "soixante": 60,
            "ninety": 90, "thirty": 30, "sixty": 60,
        }

        def relative_words(match: re.Match[str]) -> str:
            count = word_numbers[_fold(match.group("count_word"))]
            unit_word = _fold(match.group("unit"))
            unit = units.get(unit_word, "d")
            if unit_word in {"semaine", "semaines", "week", "weeks"}:
                count *= 7
            return f" newer_than:{count}{unit} "

        word_alternatives = "|".join(word_numbers)
        consume(
            rf"\b(?:des?|les?|ces?)\s+(?P<count_word>{word_alternatives})\s+"
            rf"(?:derniers?|last)\s+(?P<unit>jours?|days?|semaines?|weeks?|"
            rf"mois|months?|ans?|ann(?:e|é)es?|years?)\b",
            relative_words,
            "période relative",
        )

        consume(
            r"\b(?:avec\s+(?:une?\s+)?pi(?:e|è)ce[s]?\s+jointe[s]?|"
            r"avec\s+fichier[s]?\s+joint[s]?|has\s+attachments?)\b",
            lambda _m: " has:attachment ",
            "avec pièce jointe",
        )
        consume(
            r"\b(?:sans\s+(?:une?\s+)?pi(?:e|è)ce[s]?\s+jointe[s]?|"
            r"without\s+attachments?)\b",
            lambda _m: " -has:attachment ",
            "sans pièce jointe",
        )
        consume(
            r"\bavec\s+(?:un\s+)?(?:fichier\s+)?(?:au\s+format\s+)?"
            r"(?P<extension>pdf|docx?|xlsx?|pptx?|csv|zip|jpe?g|png)\b",
            lambda m: f" filename:{m.group('extension').lower()} ",
            "type de pièce jointe",
        )
        consume(
            r"\b(?:non[- ]?lus?|unread)\b", lambda _m: " is:unread ", "non lus"
        )
        consume(
            r"\b(?:d(?:e|é)j(?:a|à)\s+lus?|messages?\s+lus?|read)\b",
            lambda _m: " is:read ",
            "déjà lus",
        )
        consume(
            r"\b(?:dans\s+(?:la\s+)?bo(?:i|î)te\s+de\s+r(?:e|é)ception|inbox)\b",
            lambda _m: " in:inbox ",
            "boîte de réception",
        )
        consume(
            r"\b(?:dans\s+(?:les\s+)?(?:messages?\s+)?envoy(?:e|é)s?|sent)\b",
            lambda _m: " in:sent ",
            "messages envoyés",
        )
        consume(
            r"\b(?:marqu(?:e|é)s?\s+(?:d'une?\s+)?(?:e|é)toile|starred)\b",
            lambda _m: " is:starred ",
            "suivis",
        )
        consume(
            r"\b(?:important[s]?|prioritaires?|priority)\b",
            lambda _m: " is:important ",
            "importants",
        )

        # Le nom ou l'adresse s'arrête devant un filtre temporel ou d'état.
        sender_pattern = (
            r"\b(?:envoy(?:e|é)s?\s+par|provenant\s+de|re[cç]us?\s+de|from|de)\s+"
            r"(?P<value>[^,;]+?)"
            r"(?=\s+(?:avec|sans|depuis|ces?|dans\s+les?|sur\s+les?|derniers?|"
            r"aujourd'hui|hier|non[- ]?lus?|lus?|important|objet|contenant|"
            r"newer_than:|after:|before:|has:|is:)\b|[,;]|$)"
        )
        consume(
            sender_pattern,
            lambda m: f" from:{_gmail_quote(m.group('value'))} ",
            "expéditeur",
        )
        consume(
            r"\b(?:objet|subject)\s+(?:contient|contenant|avec|:)?\s*"
            r"(?P<value>[^,;]+?)(?=\s+(?:avec|depuis|ces?|non[- ]?lus?|"
            r"newer_than:|after:|before:|has:|is:)\b|[,;]|$)",
            lambda m: f" subject:{_gmail_quote(m.group('value'))} ",
            "objet",
        )

        # Les opérateurs produits ci-dessus sont déjà dans remaining. Le reste
        # demeure du texte libre et Gmail applique une intersection des mots.
        remaining = re.sub(r"\b(?:cherche|chercher|trouve|trouver|montre|affiche|"
                           r"moi|mes|les?|des?|emails?|e-mails?|mails?|messages?)\b",
                           " ", remaining, flags=re.IGNORECASE)
        remaining = re.sub(r"\s+", " ", remaining).strip(" ,;")
        if remaining:
            parts.append(remaining)
        free_terms = _query_free_terms(remaining)

    def add_operator(operator: str, value: Any, label: str, *, quote: bool = True) -> None:
        if value is None or value == "":
            return
        rendered = _gmail_quote(value) if quote else str(value)
        parts.append(f"{operator}:{rendered}")
        explanations.append(label)

    add_operator("from", structured.get("from"), "expéditeur")
    add_operator("to", structured.get("to"), "destinataire")
    add_operator("subject", structured.get("subject"), "objet")
    add_operator("filename", structured.get("filename"), "nom de pièce jointe")
    add_operator("label", structured.get("label"), "libellé")
    if structured.get("after"):
        add_operator("after", _parse_search_date(structured["after"]), "date minimale", quote=False)
    if structured.get("before"):
        add_operator("before", _parse_search_date(structured["before"]), "date maximale", quote=False)
    if structured.get("has_attachment") is True:
        parts.append("has:attachment")
        explanations.append("avec pièce jointe")
    if structured.get("unread") is True:
        parts.append("is:unread")
    elif structured.get("unread") is False:
        parts.append("is:read")
    if structured.get("starred") is True:
        parts.append("is:starred")
    if structured.get("important") is True:
        parts.append("is:important")
    scope = str(structured.get("scope") or "").strip().lower()
    if scope and scope != "all":
        allowed = {"inbox", "sent", "drafts", "trash", "spam", "anywhere"}
        if scope not in allowed:
            raise GmailError(f"Dossier Gmail inconnu : {scope}.")
        parts.append(f"in:{scope}")
    if structured.get("larger_than"):
        add_operator("larger", structured["larger_than"], "taille minimale", quote=False)
    if structured.get("smaller_than"):
        add_operator("smaller", structured["smaller_than"], "taille maximale", quote=False)

    gmail_query = re.sub(r"\s+", " ", " ".join(parts)).strip()
    if not gmail_query:
        raise GmailError("La recherche Gmail est vide.")
    return GmailSearchPlan(
        original_query=original,
        gmail_query=gmail_query,
        free_terms=free_terms,
        interpreted=not native and gmail_query != original,
        explanation=", ".join(dict.fromkeys(explanations)),
    )


@dataclass(frozen=True)
class GmailStatus:
    configured: bool
    authenticated: bool
    account: str = ""
    message: str = ""
    # Ce qu'il reste à faire, en une phrase actionnable. Vide quand tout est en
    # place : « la configuration n'est pas terminée » sans dire par quoi
    # continuer est le message le plus frustrant que puisse rendre un outil.
    next_step: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def find_downloaded_client_secret() -> Optional[Path]:
    """Cherche une clé OAuth Google fraîchement téléchargée.

    Google nomme le fichier `client_secret_<id>.apps.googleusercontent.com.json`
    et le dépose dans les téléchargements. Demander à l'utilisateur de retrouver
    ce chemin à la main est la marche la plus haute de toute l'installation.
    """
    candidates: List[Path] = []
    home = Path.home()
    for name in _DOWNLOAD_DIRS:
        directory = home / name
        if not directory.is_dir():
            continue
        for pattern in ("client_secret*.json", "*googleusercontent.com.json"):
            candidates.extend(directory.glob(pattern))

    valid = []
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if (data.get("installed") or {}).get("client_id"):
            valid.append(path)
    if not valid:
        return None
    # Le plus récent : on refait souvent la manipulation après un échec.
    return max(valid, key=lambda p: p.stat().st_mtime)


def _decode_header_value(value: str) -> str:
    """Décode les objets comme =?UTF-8?Q?...?= sans casser les mails anciens."""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _decode_body_data(data: str) -> str:
    if not data:
        return ""
    try:
        padding = "=" * (-len(data) % 4)
        raw = base64.urlsafe_b64decode((data + padding).encode("ascii"))
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _execute(request: Any, retries: int = 3) -> Any:
    """Exécute une requête Google avec reprise sur les pannes transitoires.

    Les petits doubles utilisés dans les tests et certains transports anciens
    n'acceptent pas `num_retries`; le repli conserve leur compatibilité.
    """
    try:
        return request.execute(num_retries=max(0, int(retries)))
    except TypeError as exc:
        if "num_retries" not in str(exc):
            raise
        return request.execute()


def _html_to_text(value: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    value = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>", "\n", value)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _walk_parts(part: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    yield part
    for child in part.get("parts", []) or []:
        yield from _walk_parts(child)


def _extract_message_content(payload: Dict[str, Any]) -> tuple[str, List[Dict[str, Any]]]:
    """Extrait le meilleur corps lisible et la liste des pièces jointes."""
    plain: List[str] = []
    rich: List[str] = []
    attachments: List[Dict[str, Any]] = []

    for part in _walk_parts(payload or {}):
        mime = (part.get("mimeType") or "").lower()
        filename = _decode_header_value(part.get("filename") or "").strip()
        body = part.get("body", {}) or {}
        attachment_id = body.get("attachmentId")
        if filename or attachment_id:
            attachments.append({
                "filename": filename or "pièce-jointe",
                "mime_type": mime or "application/octet-stream",
                "size": int(body.get("size") or 0),
                "attachment_id": attachment_id or "",
            })
            # Une pièce jointe text/plain n'est pas le corps principal.
            continue

        decoded = _decode_body_data(body.get("data") or "")
        if not decoded:
            continue
        if mime == "text/plain":
            plain.append(decoded.strip())
        elif mime == "text/html":
            rich.append(_html_to_text(decoded))

    body_text = "\n\n".join(x for x in plain if x).strip()
    if not body_text:
        body_text = "\n\n".join(x for x in rich if x).strip()
    return body_text, attachments


class GmailService:
    """Gestionnaire thread-safe de connexion et de lecture Gmail."""

    def __init__(self, token_file: Path | None = None,
                 client_secret_file: Path | None = None):
        self.token_file = Path(token_file or _TOKEN_FILE)
        self.client_secret_file = Path(client_secret_file or _CLIENT_SECRET_FILE)
        self._service = None
        self._lock = threading.RLock()
        self._last_error = ""
        self._polling_thread: Optional[threading.Thread] = None
        self._polling_active = False
        self._polling_stop = threading.Event()
        # OrderedDict plutôt que set : il faut pouvoir jeter les plus anciens.
        self._seen: "OrderedDict[str, None]" = OrderedDict()
        self._history_id = ""
        self._last_search_info: Dict[str, Any] = {}

    @staticmethod
    def dependencies_available() -> bool:
        try:
            import google.auth.transport.requests
            import google.oauth2.credentials  # noqa: F401
            import google_auth_oauthlib.flow  # noqa: F401
            import googleapiclient.discovery  # noqa: F401
            return True
        except ImportError:
            return False

    def _setup_message(self) -> str:
        if self.client_secret_file.is_file():
            return (
                "La clé OAuth Gmail est déjà configurée. "
                "Dites « connecte Gmail » pour renouveler l'autorisation dans Google Chrome. "
                "Aucun nouveau projet Google ni téléchargement de clé n'est nécessaire."
            )
        downloaded = find_downloaded_client_secret()
        if downloaded is not None:
            # La clé est déjà sur le disque : il ne reste qu'un mot à dire.
            return (
                "Gmail n'est pas encore relié, mais votre clé OAuth est déjà "
                f"téléchargée ({downloaded.name}). Dites « connecte Gmail » : "
                "je l'installe et j'ouvre l'autorisation Google."
            )
        return (
            "Gmail n'est pas encore relié. Il manque la clé OAuth de Google. "
            + " ".join(SETUP_STEPS)
        )

    def setup_steps(self) -> List[str]:
        """Marche à suivre, dans l'ordre, pour terminer l'installation."""
        if self.client_secret_file.is_file() or find_downloaded_client_secret() is not None:
            return ["Dites « connecte Gmail » pour ouvrir l’autorisation dans Google Chrome."]
        return list(SETUP_STEPS)

    def _validate_client_secret(self, path: Path | None = None) -> Dict[str, Any]:
        target = Path(path or self.client_secret_file)
        if not target.is_file():
            raise GmailSetupRequired(self._setup_message())
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
            installed = data.get("installed") or {}
            if not installed.get("client_id") or not installed.get("client_secret"):
                raise ValueError("section 'installed' absente")
        except GmailSetupRequired:
            raise
        except Exception as exc:
            raise GmailSetupRequired(
                f"Le fichier OAuth {target} est invalide ({exc}). "
                "Il faut une clé Google OAuth de type Application de bureau."
            ) from exc
        return data

    def configure_client_secret(self, source_path: str | Path) -> Path:
        """Valide et installe atomiquement un JSON OAuth téléchargé."""
        source = Path(source_path).expanduser().resolve()
        data = self._validate_client_secret(source)
        self.client_secret_file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=".gmail_client.", suffix=".tmp",
            dir=self.client_secret_file.parent,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, self.client_secret_file)
            os.chmod(self.client_secret_file, 0o600)
        except Exception:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        return self.client_secret_file

    def adopt_downloaded_client_secret(self) -> Optional[Path]:
        """Installe la clé OAuth trouvée dans les téléchargements, s'il en est.

        Sans cela, « connecte Gmail » échouait alors que le fichier attendu
        était déjà sur le disque, à un dossier près.
        """
        if self.client_secret_file.is_file():
            return None
        found = find_downloaded_client_secret()
        if found is None:
            return None
        try:
            return self.configure_client_secret(found)
        except GmailError:
            return None

    def _write_token(self, creds: Any) -> None:
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=".gmail_token.", suffix=".tmp", dir=self.token_file.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(creds.to_json())
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, self.token_file)
            os.chmod(self.token_file, 0o600)
        except Exception:
            Path(tmp_name).unlink(missing_ok=True)
            raise

    def _load_credentials(self) -> Any:
        from google.oauth2.credentials import Credentials

        if not self.token_file.is_file():
            return None
        try:
            # Sans `scopes` : les portées réellement accordées sont relues du
            # jeton, sinon google-auth refuse le rafraîchissement d'un ancien
            # jeton lecture seule (« not all requested scopes were granted »).
            return Credentials.from_authorized_user_file(str(self.token_file))
        except Exception as exc:
            raise GmailSetupRequired(
                f"Le jeton Gmail est illisible ou incompatible ({exc}). "
                "Relancez l'action connect pour autoriser de nouveau le compte."
            ) from exc

    def connect(self, interactive: bool = True):
        """Connecte Gmail ; ouvre le navigateur uniquement si explicitement demandé."""
        with self._lock:
            if not self.dependencies_available():
                raise GmailSetupRequired(
                    "Modules Gmail absents. Installez google-api-python-client, "
                    "google-auth et google-auth-oauthlib."
                )
            from google.auth.transport.requests import Request
            from google_auth_oauthlib.flow import InstalledAppFlow
            from googleapiclient.discovery import build

            try:
                creds = self._load_credentials()
            except GmailSetupRequired:
                if not interactive:
                    raise
                # Une demande explicite de reconnexion doit pouvoir remplacer
                # un ancien jeton corrompu ou créé avec de mauvais scopes.
                creds = None
            try:
                if creds and creds.expired and creds.refresh_token:
                    try:
                        creds.refresh(Request())
                        self._write_token(creds)
                    except Exception as exc:
                        if not interactive:
                            raise GmailSetupRequired(
                                "L'autorisation Gmail a expiré ou a été révoquée. "
                                "Demandez « connecte Gmail » pour la renouveler."
                            ) from exc
                        creds = None
                if creds and creds.valid and interactive and not self._creds_can_write(creds):
                    # Reconnexion explicite : on en profite pour obtenir les
                    # portées d'écriture (envoi, classement).
                    creds = None
                if not creds or not creds.valid:
                    if not interactive:
                        raise GmailSetupRequired(self._setup_message())
                    self.adopt_downloaded_client_secret()
                    self._validate_client_secret()
                    flow = InstalledAppFlow.from_client_secrets_file(
                        str(self.client_secret_file), _SCOPES
                    )
                    server_kwargs: dict[str, Any] = {
                        "host": "127.0.0.1",
                        "port": 0,
                        "open_browser": True,
                        "browser": _register_gmail_chrome(),
                        "timeout_seconds": 180,
                        "authorization_prompt_message": (
                            "Terminez l’autorisation Gmail dans Google Chrome."
                        ),
                        "success_message": (
                            "Gmail est maintenant relié à ANO-GPT. "
                            "Vous pouvez fermer cet onglet."
                        ),
                    }
                    creds = flow.run_local_server(**server_kwargs)
                    self._write_token(creds)
                self._service = build(
                    "gmail", "v1", credentials=creds,
                    cache_discovery=False,
                )
                # Appel réel : valide le jeton et permet d'afficher le compte.
                profile = _execute(self._service.users().getProfile(userId="me"))
                self._last_error = ""
                return profile
            except GmailSetupRequired:
                raise
            except Exception as exc:
                self._service = None
                self._last_error = str(exc)
                raise GmailError(f"Connexion Gmail impossible : {exc}") from exc

    def _get_service(self):
        with self._lock:
            if self._service is not None:
                return self._service
        self.connect(interactive=False)
        return self._service

    @staticmethod
    def _creds_can_write(creds: Any) -> bool:
        granted = set(getattr(creds, "granted_scopes", None) or getattr(creds, "scopes", None) or [])
        return _WRITE_SCOPES.issubset(granted)

    @property
    def can_write(self) -> bool:
        """Vrai si le jeton autorise l'envoi et le classement."""
        try:
            data = json.loads(self.token_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        scopes = data.get("scopes") or []
        if isinstance(scopes, str):
            scopes = scopes.split()
        return _WRITE_SCOPES.issubset(set(scopes))

    def _writable_service(self):
        service = self._get_service()
        if not self.can_write:
            raise GmailSetupRequired(
                "Le compte Gmail est relié en lecture seule. Dites « connecte "
                "Gmail » pour autoriser l'envoi et le classement des messages."
            )
        return service

    # ── écritures ────────────────────────────────────────────────────────

    def send_email(self, to: str, subject: str, body: str, *,
                   cc: str = "", reply_to_id: str = "") -> Dict[str, Any]:
        """Envoie un message (ou répond dans le fil si `reply_to_id`)."""
        import base64
        from email.message import EmailMessage

        service = self._writable_service()
        if not to or "@" not in to:
            raise GmailError("Destinataire invalide.")
        msg = EmailMessage()
        msg["To"] = to
        msg["Subject"] = subject or "(sans objet)"
        if cc:
            msg["Cc"] = cc
        thread_id = ""
        if reply_to_id:
            try:
                original = _execute(service.users().messages().get(
                    userId="me", id=reply_to_id, format="metadata",
                    metadataHeaders=["Message-ID", "Subject"],
                ))
                headers = {h["name"].lower(): h["value"]
                           for h in original.get("payload", {}).get("headers", [])}
                if headers.get("message-id"):
                    msg["In-Reply-To"] = headers["message-id"]
                    msg["References"] = headers["message-id"]
                if not subject and headers.get("subject"):
                    original_subject = headers["subject"]
                    msg.replace_header("Subject", original_subject if original_subject.lower().startswith("re:")
                                       else f"Re: {original_subject}")
                thread_id = original.get("threadId", "")
            except Exception as exc:
                raise GmailError(f"Message d'origine introuvable : {exc}") from exc
        msg.set_content(body or "")
        payload: Dict[str, Any] = {
            "raw": base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii"),
        }
        if thread_id:
            payload["threadId"] = thread_id
        try:
            return _execute(service.users().messages().send(userId="me", body=payload))
        except Exception as exc:
            raise GmailError(f"Envoi impossible : {exc}") from exc

    def modify_labels(self, msg_id: str, *, add: List[str] | None = None,
                      remove: List[str] | None = None) -> Dict[str, Any]:
        service = self._writable_service()
        if not msg_id:
            raise GmailError("Identifiant de message manquant.")
        body = {"addLabelIds": list(add or []), "removeLabelIds": list(remove or [])}
        try:
            return _execute(service.users().messages().modify(userId="me", id=msg_id, body=body))
        except Exception as exc:
            raise GmailError(f"Modification impossible : {exc}") from exc

    def mark_read(self, msg_id: str, read: bool = True) -> Dict[str, Any]:
        return self.modify_labels(msg_id, remove=["UNREAD"] if read else None,
                                  add=None if read else ["UNREAD"])

    def archive(self, msg_id: str) -> Dict[str, Any]:
        return self.modify_labels(msg_id, remove=["INBOX"])

    def star(self, msg_id: str, starred: bool = True) -> Dict[str, Any]:
        return self.modify_labels(msg_id, add=["STARRED"] if starred else None,
                                  remove=None if starred else ["STARRED"])

    def trash(self, msg_id: str) -> Dict[str, Any]:
        service = self._writable_service()
        try:
            return _execute(service.users().messages().trash(userId="me", id=msg_id))
        except Exception as exc:
            raise GmailError(f"Mise à la corbeille impossible : {exc}") from exc

    def untrash(self, msg_id: str) -> Dict[str, Any]:
        service = self._writable_service()
        try:
            return _execute(service.users().messages().untrash(userId="me", id=msg_id))
        except Exception as exc:
            raise GmailError(f"Restauration impossible : {exc}") from exc

    def status(self, verify: bool = False) -> GmailStatus:
        if not self.dependencies_available():
            return GmailStatus(
                False, False,
                message="Bibliothèques Gmail absentes.",
                next_step="pip install google-api-python-client google-auth "
                          "google-auth-oauthlib",
            )
        configured = self.client_secret_file.is_file() or self.token_file.is_file()
        if not configured:
            downloaded = find_downloaded_client_secret()
            return GmailStatus(
                False, False,
                message=self._setup_message(),
                next_step=("Dites « connecte Gmail »." if downloaded
                           else SETUP_STEPS[0]),
            )
        if not self.token_file.is_file():
            return GmailStatus(
                True, False,
                message="Clé OAuth en place ; il manque votre autorisation Google.",
                next_step="Dites « connecte Gmail » et autorisez le compte "
                          "dans le navigateur.",
            )
        if not verify:
            return GmailStatus(True, True, message="Jeton Gmail présent.")
        try:
            service = self._get_service()
            profile = _execute(service.users().getProfile(userId="me"))
            return GmailStatus(
                True, True, account=profile.get("emailAddress", ""),
                message="Connexion Gmail opérationnelle.",
            )
        except GmailError as exc:
            return GmailStatus(
                True, False, message=str(exc),
                next_step="Dites « connecte Gmail » pour renouveler l'autorisation.",
            )

    def _message_from_api(self, message: Dict[str, Any], include_body: bool = False) -> Dict[str, Any]:
        payload = message.get("payload", {}) or {}
        headers = {
            str(h.get("name", "")).lower(): _decode_header_value(str(h.get("value", "")))
            for h in payload.get("headers", []) or []
        }
        body, attachments = _extract_message_content(payload) if include_body else ("", [])
        labels = message.get("labelIds", []) or []
        return {
            "id": message.get("id", ""),
            "thread_id": message.get("threadId", ""),
            "sender": headers.get("from") or "Inconnu",
            "reply_to": headers.get("reply-to") or "",
            "to": headers.get("to") or "",
            "subject": headers.get("subject") or "Sans objet",
            "date": headers.get("date") or "",
            "internal_date": str(message.get("internalDate") or ""),
            "snippet": html.unescape(message.get("snippet", "") or ""),
            "body": body,
            "attachments": attachments,
            "unread": "UNREAD" in labels,
            "important": "IMPORTANT" in labels,
            "labels": list(labels),
        }

    def _list_message_ids(
        self,
        query: str,
        limit: int,
        *,
        include_spam_trash: bool = False,
    ) -> tuple[List[str], int]:
        """Liste paginée et bornée des identifiants correspondant à la requête."""
        service = self._get_service()
        ids: List[str] = []
        estimate = 0
        page_token = ""
        for _ in range(_MAX_LIST_PAGES):
            remaining = limit - len(ids)
            if remaining <= 0:
                break
            list_args: Dict[str, Any] = {
                "userId": "me", "maxResults": min(remaining, 100),
            }
            if query:
                list_args["q"] = query
            if include_spam_trash:
                list_args["includeSpamTrash"] = True
            if page_token:
                list_args["pageToken"] = page_token
            response = _execute(service.users().messages().list(**list_args))
            try:
                estimate = max(estimate, int(response.get("resultSizeEstimate") or 0))
            except (TypeError, ValueError):
                pass
            for item in response.get("messages", []) or []:
                msg_id = str(item.get("id") or "")
                if msg_id and msg_id not in ids:
                    ids.append(msg_id)
                    if len(ids) >= limit:
                        break
            page_token = str(response.get("nextPageToken") or "")
            if not page_token:
                break
        return ids, max(estimate, len(ids))

    def _fetch_messages(
        self,
        message_ids: Sequence[str],
        *,
        include_structure: bool = False,
    ) -> List[Dict[str, Any]]:
        """Récupère les métadonnées, en batch Gmail quand il est disponible."""
        service = self._get_service()
        fmt = "full" if include_structure else "metadata"

        def request(msg_id: str):
            args: Dict[str, Any] = {"userId": "me", "id": msg_id, "format": fmt}
            if fmt == "metadata":
                args["metadataHeaders"] = ["From", "To", "Cc", "Subject", "Date"]
            return service.users().messages().get(**args)

        raw_by_id: Dict[str, Dict[str, Any]] = {}
        fetch_errors: List[Exception] = []
        new_batch = getattr(service, "new_batch_http_request", None)
        if callable(new_batch) and len(message_ids) > 1:
            def callback(request_id: str, response: Any, exception: Any) -> None:
                if exception is None and isinstance(response, dict):
                    raw_by_id[request_id] = response

            try:
                batch = new_batch()
                for msg_id in message_ids:
                    batch.add(request(msg_id), callback=callback, request_id=msg_id)
                batch.execute()
            except Exception:
                # Certains transports/proxys ne prennent pas en charge le batch.
                # On ne perd pas la recherche : les éléments absents seront lus
                # individuellement juste après.
                pass

        for msg_id in message_ids:
            if msg_id in raw_by_id:
                continue
            try:
                raw = _execute(request(msg_id))
                if isinstance(raw, dict):
                    raw_by_id[msg_id] = raw
            except Exception as exc:
                # Un message peut être supprimé entre list et get.
                fetch_errors.append(exc)
                continue

        if message_ids and not raw_by_id and fetch_errors:
            raise GmailError(
                "Gmail a trouvé des messages, mais leurs détails sont inaccessibles : "
                f"{fetch_errors[0]}"
            )

        output: List[Dict[str, Any]] = []
        for msg_id in message_ids:
            raw = raw_by_id.get(msg_id)
            if raw is None:
                continue
            parsed = self._message_from_api(raw)
            if include_structure:
                _body, parsed["attachments"] = _extract_message_content(
                    raw.get("payload", {}) or {}
                )
            output.append(parsed)
        return output

    def list_emails(
        self,
        query: str = "",
        max_results: int = 10,
        *,
        include_spam_trash: bool = False,
    ) -> List[Dict[str, Any]]:
        max_results = max(1, min(int(max_results), _MAX_RESULTS))
        try:
            ids, _estimate = self._list_message_ids(
                query, max_results, include_spam_trash=include_spam_trash
            )
            return self._fetch_messages(ids)
        except GmailError:
            raise
        except Exception as exc:
            raise GmailError(f"Impossible de lire Gmail : {exc}") from exc

    def get_unread(self, max_results: int = 5) -> List[Dict[str, Any]]:
        return self.list_emails("is:unread", max_results)

    def get_recent(self, max_results: int = 10) -> List[Dict[str, Any]]:
        return self.list_emails("in:inbox", max_results)

    @property
    def last_search_info(self) -> Dict[str, Any]:
        """Copie du diagnostic de la dernière recherche, sûre à afficher."""
        with self._lock:
            return dict(self._last_search_info)

    @staticmethod
    def _relevance(message: Mapping[str, Any], terms: Sequence[str]) -> tuple[int, float]:
        if not terms:
            return 0, GmailService._message_timestamp(message)
        subject = _fold(str(message.get("subject") or ""))
        sender = _fold(str(message.get("sender") or ""))
        snippet = _fold(str(message.get("snippet") or ""))
        score = 0
        for term in terms:
            if term in subject:
                score += 12
            if term in sender:
                score += 8
            if term in snippet:
                score += 3
        phrase = " ".join(terms)
        if len(terms) > 1 and phrase in subject:
            score += 15
        return score, GmailService._message_timestamp(message)

    @staticmethod
    def _message_timestamp(message: Mapping[str, Any]) -> float:
        internal = str(message.get("internal_date") or "")
        if internal.isdigit():
            return int(internal) / 1000.0
        try:
            parsed = parsedate_to_datetime(str(message.get("date") or ""))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except (TypeError, ValueError, OverflowError):
            return 0.0

    def search_emails(
        self,
        query: str,
        max_results: int = 10,
        *,
        filters: Optional[Mapping[str, Any]] = None,
        include_spam_trash: bool = False,
    ) -> List[Dict[str, Any]]:
        """Recherche avancée : langage naturel, pagination et pertinence locale."""
        plan = build_gmail_search_query(query, filters)
        max_results = max(1, min(int(max_results), _MAX_RESULTS))
        candidate_limit = min(
            _MAX_SEARCH_CANDIDATES,
            max(max_results, max_results * 3 if plan.free_terms else max_results),
        )
        if filters:
            scope = str(filters.get("scope") or "").lower()
            include_spam_trash = include_spam_trash or scope in {"all", "anywhere", "spam", "trash"}
        try:
            ids, estimate = self._list_message_ids(
                plan.gmail_query,
                candidate_limit,
                include_spam_trash=include_spam_trash,
            )
            need_structure = "has:attachment" in plan.gmail_query.lower() or "filename:" in plan.gmail_query.lower()
            messages = self._fetch_messages(ids, include_structure=need_structure)
            if plan.free_terms:
                messages.sort(
                    key=lambda message: self._relevance(message, plan.free_terms),
                    reverse=True,
                )
            messages = messages[:max_results]
            for message in messages:
                message["relevance_score"] = self._relevance(message, plan.free_terms)[0]
            with self._lock:
                self._last_search_info = {
                    "original_query": plan.original_query,
                    "gmail_query": plan.gmail_query,
                    "interpreted": plan.interpreted,
                    "explanation": plan.explanation,
                    "result_size_estimate": estimate,
                    "candidates_checked": len(ids),
                    "returned": len(messages),
                }
            return messages
        except GmailError:
            raise
        except Exception as exc:
            raise GmailError(
                f"Recherche Gmail impossible pour « {plan.gmail_query} » : {exc}"
            ) from exc

    def read_email(self, msg_id: str) -> Dict[str, Any]:
        service = self._get_service()
        if not msg_id:
            raise GmailError("Identifiant de message manquant.")
        try:
            raw = _execute(service.users().messages().get(
                userId="me", id=msg_id, format="full"
            ))
            result = self._message_from_api(raw, include_body=True)
            if not result["body"]:
                result["body"] = result["snippet"] or "Contenu vide."
            return result
        except Exception as exc:
            raise GmailError(f"Impossible de lire ce message : {exc}") from exc

    def get_email_header(self, msg_id: str) -> Optional[Dict[str, Any]]:
        try:
            message = self.read_email(msg_id)
            return {k: message[k] for k in ("id", "sender", "subject", "date", "snippet")}
        except GmailError:
            return None

    def read_email_body(self, msg_id: str) -> str:
        return self.read_email(msg_id)["body"]

    # ── veille temps réel ────────────────────────────────────────────────

    @property
    def watching(self) -> bool:
        return self._polling_active

    def _mark_seen(self, msg_id: str) -> None:
        self._seen[msg_id] = None
        self._seen.move_to_end(msg_id)
        while len(self._seen) > _SEEN_LIMIT:
            self._seen.popitem(last=False)

    def _profile_history_id(self) -> str:
        service = self._get_service()
        profile = _execute(service.users().getProfile(userId="me"))
        return str(profile.get("historyId") or "")

    def new_unread(self) -> List[Dict[str, Any]]:
        """Messages non lus arrivés depuis le passage précédent.

        On interroge l'historique plutôt que la liste des non-lus : Gmail ne
        renvoie alors que le delta, ce qui permet d'interroger toutes les
        vingt-cinq secondes sans épuiser le quota. Le premier appel ne fait
        qu'établir le point de départ — annoncer d'un coup les cent messages
        non lus d'une boîte ordinaire serait insupportable.
        """
        service = self._get_service()
        if not self._history_id:
            self._history_id = self._profile_history_id()
            return []

        added: List[str] = []
        page_token = ""
        latest = self._history_id
        try:
            for _ in range(5):  # borne dure : jamais de pagination sans fin
                args: Dict[str, Any] = {
                    "userId": "me", "startHistoryId": self._history_id,
                    "historyTypes": ["messageAdded"], "labelId": "UNREAD",
                }
                if page_token:
                    args["pageToken"] = page_token
                response = _execute(service.users().history().list(**args))
                latest = str(response.get("historyId") or latest)
                for record in response.get("history", []) or []:
                    for entry in record.get("messagesAdded", []) or []:
                        message = entry.get("message", {}) or {}
                        msg_id = message.get("id")
                        labels = message.get("labelIds") or []
                        if not msg_id or "UNREAD" not in labels:
                            continue
                        if "TRASH" in labels or "SPAM" in labels:
                            continue
                        if msg_id in self._seen:
                            continue
                        added.append(msg_id)
                page_token = response.get("nextPageToken") or ""
                if not page_token:
                    break
        except Exception as exc:
            # 404 : l'historique Gmail ne remonte qu'environ une semaine. Après
            # une longue coupure il faut repartir d'un point neuf plutôt que de
            # rejouer la boîte entière.
            if "404" in str(exc) or "startHistoryId" in str(exc):
                self._history_id = self._profile_history_id()
                return []
            raise GmailError(f"Veille Gmail impossible : {exc}") from exc

        self._history_id = latest
        fresh: List[Dict[str, Any]] = []
        for msg_id in added:
            self._mark_seen(msg_id)
            try:
                raw = _execute(service.users().messages().get(
                    userId="me", id=msg_id, format="metadata",
                    metadataHeaders=["From", "To", "Subject", "Date"],
                ))
            except Exception:
                continue  # message effacé entre-temps : rien à annoncer
            fresh.append(self._message_from_api(raw))
        return fresh

    def start_polling(self, callback: Callable[[Dict[str, Any]], None],
                      interval_sec: int = _WATCH_INTERVAL) -> bool:
        """Surveille la boîte et appelle `callback` à chaque nouveau message.

        Renvoie False si une veille tourne déjà ou si Gmail n'est pas relié :
        boucler sur une autorisation absente ne ferait qu'imprimer la même
        erreur toutes les vingt-cinq secondes.
        """
        with self._lock:
            if self._polling_active:
                return False
            self._polling_active = True
        self._polling_stop.clear()

        interval = max(10, int(interval_sec))

        def _poll_loop():
            try:
                while not self._polling_stop.is_set():
                    try:
                        for mail in self.new_unread():
                            try:
                                callback(mail)
                            except Exception as exc:
                                print(f"[GmailService] Notification : {exc}",
                                      file=sys.stderr)
                    except GmailSetupRequired as exc:
                        print(f"[GmailService] Veille arrêtée : {exc}",
                              file=sys.stderr)
                        return
                    except Exception as exc:
                        print(f"[GmailService] Veille : {exc}", file=sys.stderr)
                    self._polling_stop.wait(interval)
            finally:
                self._polling_active = False

        self._polling_thread = threading.Thread(
            target=_poll_loop, name="gmail-watch", daemon=True
        )
        self._polling_thread.start()
        return True

    def stop_polling(self) -> None:
        self._polling_stop.set()
        self._polling_active = False


_gmail_instance: Optional[GmailService] = None
_instance_lock = threading.Lock()


def get_gmail_service() -> GmailService:
    global _gmail_instance
    with _instance_lock:
        if _gmail_instance is None:
            _gmail_instance = GmailService()
        return _gmail_instance
