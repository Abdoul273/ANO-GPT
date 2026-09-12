"""OAuth GitHub Device Flow et API REST, sans token dans les fichiers ou remotes."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from core.browser_policy import open_chrome

_BASE_DIR = Path(__file__).resolve().parent.parent
_CONFIG_FILE = _BASE_DIR / "config" / "api_keys.json"
_KEYRING_SERVICE = "ano-gpt-github"
_KEYRING_ACCOUNT = "oauth-token"
_API = "https://api.github.com"


class GitHubError(RuntimeError):
    pass


class GitHubSetupRequired(GitHubError):
    pass


@dataclass(frozen=True)
class GitHubStatus:
    configured: bool
    authenticated: bool
    login: str = ""
    message: str = ""
    next_step: str = ""


def _config() -> dict[str, Any]:
    try:
        raw = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _json_request(url: str, *, data: dict[str, Any] | None = None,
                  headers: dict[str, str] | None = None, timeout: int = 20) -> dict[str, Any]:
    body = urlencode(data).encode() if data is not None else None
    request_headers = {"Accept": "application/json", "User-Agent": "ANO-GPT-GitHub"}
    request_headers.update(headers or {})
    if body is not None:
        request_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    request = Request(url, data=body, headers=request_headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return payload if isinstance(payload, dict) else {}
    except HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("message", "")
        except Exception:
            detail = ""
        raise GitHubError(f"GitHub HTTP {exc.code}{f' : {detail}' if detail else ''}") from None
    except (URLError, TimeoutError) as exc:
        raise GitHubError("GitHub est injoignable. Réessayez plus tard.") from exc


class GitHubService:
    """Token OAuth uniquement dans le trousseau système de l'utilisateur."""

    def _client_id(self) -> str:
        value = str(_config().get("github_oauth_client_id") or "").strip()
        if not value:
            raise GitHubSetupRequired(
                "GitHub OAuth doit être configuré : créez une OAuth App GitHub, activez "
                "Device Flow, puis renseignez github_oauth_client_id dans config/api_keys.json."
            )
        return value

    @staticmethod
    def _keyring():
        try:
            import keyring
            return keyring
        except ImportError as exc:
            raise GitHubSetupRequired(
                "Le trousseau système Python est absent. Installez-le avec « sudo pacman -S python-keyring », puis reconnectez GitHub."
            ) from exc

    def token(self) -> str:
        token = self._keyring().get_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT)
        if not token:
            raise GitHubSetupRequired("GitHub n'est pas connecté. Dites « ANO, connecte GitHub ».")
        return token

    def _store_token(self, token: str) -> None:
        if not token or len(token) < 20:
            raise GitHubError("GitHub a renvoyé un jeton OAuth invalide.")
        try:
            self._keyring().set_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT, token)
        except Exception as exc:
            raise GitHubError("Impossible d'enregistrer le jeton GitHub dans le trousseau système.") from exc

    def api(self, method: str, path: str, *, payload: dict[str, Any] | None = None) -> Any:
        token = self.token()
        url = f"{_API}{path}"
        content = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(url, data=content, method=method.upper(), headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ANO-GPT-GitHub",
            **({"Content-Type": "application/json"} if content is not None else {}),
        })
        try:
            with urlopen(request, timeout=25) as response:
                raw = response.read()
                return json.loads(raw.decode("utf-8")) if raw else {}
        except HTTPError as exc:
            if exc.code in (401, 403):
                raise GitHubSetupRequired("Le jeton GitHub est expiré, révoqué ou sans droit repo. Reconnectez GitHub.") from None
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("message", "")
            except Exception:
                detail = ""
            raise GitHubError(f"GitHub HTTP {exc.code}{f' : {detail}' if detail else ''}") from None
        except (URLError, TimeoutError) as exc:
            raise GitHubError("GitHub est injoignable. Réessayez plus tard.") from exc

    def connect(
        self,
        on_progress: Callable[[str], None] | None = None,
        on_device_code: Callable[[str, str], None] | None = None,
    ) -> dict[str, Any]:
        """Flux OAuth Device Flow : affiche le code, ouvre Chrome, puis attend GitHub."""
        client_id = self._client_id()
        device = _json_request("https://github.com/login/device/code", data={
            "client_id": client_id, "scope": "repo read:user",
        })
        device_code = str(device.get("device_code") or "")
        user_code = str(device.get("user_code") or "")
        url = str(device.get("verification_uri_complete") or device.get("verification_uri") or "")
        if not (device_code and user_code and url):
            raise GitHubError("Réponse OAuth GitHub incomplète.")
        # GitHub peut ignorer `verification_uri_complete` selon le navigateur
        # ou la session. Le code doit donc être visible *avant* l'ouverture de
        # Chrome, et pas seulement perdu dans le journal technique.
        if on_device_code:
            on_device_code(user_code, url)
        if on_progress:
            on_progress(
                f"SYS : code GitHub à saisir : {user_code}. La carte d'autorisation reste affichée."
            )
        if not open_chrome(url):
            raise GitHubError("Google Chrome est introuvable ou n'a pas pu ouvrir l'autorisation GitHub.")
        interval = max(5, int(device.get("interval") or 5))
        deadline = time.monotonic() + min(900, max(60, int(device.get("expires_in") or 900)))
        while time.monotonic() < deadline:
            time.sleep(interval)
            result = _json_request("https://github.com/login/oauth/access_token", data={
                "client_id": client_id, "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            })
            if result.get("access_token"):
                self._store_token(str(result["access_token"]))
                return self.api("GET", "/user")
            error = str(result.get("error") or "")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5
                continue
            if error == "access_denied":
                raise GitHubError("Autorisation GitHub annulée dans Google Chrome.")
            raise GitHubError(f"Autorisation GitHub échouée : {error or 'réponse inconnue'}")
        raise GitHubError("Autorisation GitHub expirée : recommencez la connexion.")

    def status(self, verify: bool = False) -> GitHubStatus:
        try:
            self._client_id()
        except GitHubSetupRequired as exc:
            return GitHubStatus(False, False, message=str(exc), next_step="Configurez l'OAuth App GitHub.")
        try:
            self.token()
        except GitHubSetupRequired:
            return GitHubStatus(True, False, message="GitHub attend une autorisation OAuth.", next_step="Dites « connecte GitHub ».")
        if not verify:
            return GitHubStatus(True, True, message="Jeton GitHub présent dans le trousseau système.")
        try:
            profile = self.api("GET", "/user")
            return GitHubStatus(True, True, login=str(profile.get("login") or ""), message="GitHub est connecté.")
        except GitHubError as exc:
            return GitHubStatus(True, False, message=str(exc), next_step="Dites « connecte GitHub ».")

    def disconnect(self) -> str:
        """Retire irréversiblement le jeton local du trousseau.

        GitHub exige le secret de l'OAuth App pour révoquer via son API ; le
        token n'est donc jamais envoyé à un endpoint de révocation sans ce secret.
        """
        try:
            self._keyring().delete_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT)
        except Exception as exc:
            raise GitHubError("Impossible de retirer le jeton GitHub du trousseau système.") from exc
        return "GitHub est déconnecté : le jeton a été retiré du trousseau système."


_service: GitHubService | None = None


def get_github_service() -> GitHubService:
    global _service
    if _service is None:
        _service = GitHubService()
    return _service
