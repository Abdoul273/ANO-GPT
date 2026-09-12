"""Connexions personnelles ANO-GPT : API gratuite pour Notion/Figma, web sinon."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from core.browser_policy import open_chrome

_KEYRING_SERVICE = "ano-gpt-cloud-integrations"
_API_SERVICES = frozenset({"notion", "figma"})
_NOTION_PARENT_ACCOUNT = "notion-default-parent"


class CloudIntegrationError(RuntimeError):
    pass


class CloudIntegrationSetupRequired(CloudIntegrationError):
    pass


@dataclass(frozen=True)
class IntegrationStatus:
    service: str
    configured: bool
    authenticated: bool
    message: str
    next_step: str = ""


def _keyring():
    try:
        import keyring
        return keyring
    except ImportError as exc:
        raise CloudIntegrationSetupRequired(
            "Le trousseau système est absent. Installez-le avec « sudo pacman -S python-keyring »."
        ) from exc


def _api_request(url: str, *, method: str = "GET", token: str, payload: dict[str, Any] | None = None,
                 notion: bool = False) -> Any:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": "ANO-GPT/1.0"}
    if notion:
        headers["Notion-Version"] = "2022-06-28"
    if body:
        headers["Content-Type"] = "application/json"
    try:
        with urlopen(Request(url, data=body, method=method, headers=headers), timeout=25) as response:
            raw = response.read()
            return json.loads(raw.decode("utf-8")) if raw else {}
    except HTTPError as exc:
        if exc.code in {401, 403}:
            raise CloudIntegrationSetupRequired("Jeton absent, expiré ou sans droit. Recréez-le puis relancez la configuration sécurisée.") from None
        raise CloudIntegrationError(f"Le service a refusé la demande (HTTP {exc.code}).") from None
    except (URLError, TimeoutError) as exc:
        raise CloudIntegrationError("Service inaccessible. Réessayez plus tard.") from exc


class CloudIntegrations:
    _WEB_APPS = {
        "calendar": "https://calendar.google.com/", "notebooklm": "https://notebooklm.google.com/",
        "gemini": "https://gemini.google.com/", "stitch": "https://stitch.withgoogle.com/",
        "figma_make": "https://www.figma.com/make/", "figma": "https://www.figma.com/",
        "notion": "https://www.notion.so/",
    }
    _TOKEN_PAGES = {"notion": "https://www.notion.so/profile/integrations", "figma": "https://www.figma.com/settings"}

    def _check_service(self, service: str) -> str:
        service = service.casefold().strip()
        if service not in self._WEB_APPS:
            raise CloudIntegrationError("Service inconnu. Services : calendar, notion, figma, figma_make, notebooklm, gemini, stitch.")
        return service

    def token(self, service: str) -> str:
        token = _keyring().get_password(_KEYRING_SERVICE, service)
        if not token:
            raise CloudIntegrationSetupRequired(
                f"{service.title()} n'est pas encore relié. Dites « connecte {service} », créez un jeton personnel gratuit, puis lancez « python scripts/setup_cloud_token.py {service} » dans un terminal."
            )
        return token

    def set_token(self, service: str, token: str) -> None:
        service = self._check_service(service)
        if service not in _API_SERVICES:
            raise CloudIntegrationError("Seuls Notion et Figma utilisent un jeton personnel ; les autres services restent dans Chrome.")
        if len(str(token).strip()) < 16:
            raise CloudIntegrationError("Jeton invalide ou incomplet.")
        _keyring().set_password(_KEYRING_SERVICE, service, str(token).strip())

    @staticmethod
    def _notion_page_id(value: str) -> str:
        """Accepte l'URL copiée depuis Notion ou son identifiant brut."""
        match = re.search(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}|[0-9a-fA-F]{32}", str(value or ""))
        if not match:
            raise CloudIntegrationError("URL ou identifiant de page Notion invalide.")
        return match.group(0).replace("-", "")

    def set_notion_parent(self, page_url_or_id: str) -> None:
        """Définit l'emplacement par défaut des notes vocales."""
        _keyring().set_password(_KEYRING_SERVICE, _NOTION_PARENT_ACCOUNT, self._notion_page_id(page_url_or_id))

    def notion_parent(self) -> str:
        value = _keyring().get_password(_KEYRING_SERVICE, _NOTION_PARENT_ACCOUNT)
        if not value:
            raise CloudIntegrationSetupRequired(
                "Choisissez d'abord une page Notion pour vos notes : créez « Notes ANO-GPT », partagez-la avec votre connexion Notion, puis relancez « python scripts/setup_cloud_token.py notion » et collez l'URL de cette page."
            )
        return self._notion_page_id(value)

    def status(self, service: str) -> IntegrationStatus:
        service = self._check_service(service)
        if service not in _API_SERVICES:
            return IntegrationStatus(service, True, False, f"{service} s'utilise dans votre profil Google Chrome, sans API.", f"Dites « ouvre {service} ».")
        try:
            self.token(service)
            return IntegrationStatus(service, True, True, f"{service.title()} est relié via son API gratuite ; le jeton est dans le trousseau système.")
        except CloudIntegrationSetupRequired as exc:
            return IntegrationStatus(service, True, False, str(exc), f"Dites « connecte {service} ».")

    def open(self, service: str) -> str:
        service = self._check_service(service)
        if not open_chrome(self._WEB_APPS[service]):
            raise CloudIntegrationError("Google Chrome est introuvable ou n'a pas pu être lancé.")
        return f"{service} est ouvert dans votre profil Google Chrome."

    def connect(self, service: str) -> str:
        service = self._check_service(service)
        if service not in _API_SERVICES:
            return self.open(service)
        if not open_chrome(self._TOKEN_PAGES[service]):
            raise CloudIntegrationError("Google Chrome est introuvable ou n'a pas pu être lancé.")
        return (
            f"La page de connexion {service.title()} est ouverte. Créez un jeton personnel gratuit, puis lancez dans un terminal : "
            f"python scripts/setup_cloud_token.py {service}. Le jeton sera demandé sans s'afficher et restera uniquement dans le trousseau système."
        )

    def notion_search(self, query: str) -> str:
        data = _api_request("https://api.notion.com/v1/search", method="POST", token=self.token("notion"), notion=True, payload={"query": query, "page_size": 20})
        rows = data.get("results", []) if isinstance(data, dict) else []
        if not rows:
            return "Aucune page Notion trouvée."
        return "Pages Notion :\n" + "\n".join(f"- {row.get('url', '')}" for row in rows[:20])

    def notion_create_note(self, title: str, content: str, parent_id: str = "") -> str:
        if not title:
            raise CloudIntegrationError("Indiquez le titre de la note.")
        parent_id = self._notion_page_id(parent_id) if parent_id else self.notion_parent()
        paragraphs = [line.strip() for line in content.splitlines() if line.strip()] or [""]
        children = [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": line}}] if line else []}} for line in paragraphs[:100]]
        data = _api_request("https://api.notion.com/v1/pages", method="POST", token=self.token("notion"), notion=True, payload={"parent": {"page_id": parent_id}, "properties": {"title": {"title": [{"type": "text", "text": {"content": title}}]}}, "children": children})
        return f"Note Notion créée : {data.get('url', '')}" if isinstance(data, dict) else "Note Notion créée."

    def figma_file(self, file_key: str) -> str:
        if not file_key:
            raise CloudIntegrationError("Indiquez file_key, présent dans l'URL Figma après /file/ ou /design/.")
        data = _api_request(f"https://api.figma.com/v1/files/{file_key}", token=self.token("figma"))
        name = data.get("name", "Fichier sans nom") if isinstance(data, dict) else "Fichier"
        return f"Fichier Figma accessible : {name}. Figma Make reste ouvert dans Chrome pour créer ou modifier visuellement."


_service: CloudIntegrations | None = None


def get_cloud_integrations() -> CloudIntegrations:
    global _service
    if _service is None:
        _service = CloudIntegrations()
    return _service
