# local_llm.py — Client LLM local ultra‑réaliste avec contrôle en langage naturel.
# Gère Ollama et les serveurs compatibles OpenAI (LM Studio, LocalAI, Jan, etc.).
# Ajout d'un point d'entrée 'llm_control' qui comprend des phrases comme
# "change le modèle pour llama3.2", "liste les modèles disponibles", etc.


import json
import os
import re
import socket
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from typing import Callable, Generator, Optional, Dict, Any


import requests
from core.live_model_policy import BALANCED_MODEL, FAST_MODEL, PINNED_FLASH_MODEL, PINNED_PRO_MODEL


# ── Configuration et chemins ────────────────────────────────────────────────
_SENT_END = re.compile(r'(?<=[.!?])\s+|(?<=\n)\s*\n')


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR    = get_base_dir()
CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"


_DEFAULTS = {
    # ANO-GPT fonctionne d'abord avec Gemini Live. Ces valeurs ne servent
    # qu'au premier démarrage, elles ne doivent pas désigner Ollama par défaut.
    "llm_url":      "https://generativelanguage.googleapis.com",
    "llm_model":    BALANCED_MODEL,
    "llm_provider": "gemini",
}


# ── Cache de config (évite de relire/re-parser le JSON à chaque appel) ─────
# Invalidation par mtime : un seul stat() bon marché par appel, et on ne
# relit vraiment le fichier que lorsqu'il a changé (ex: settings modifiés
# depuis l'UI). call_llm/call_llm_text/call_llm_stream appellent tous
# _load_config() en interne, donc ce cache réduit l'I/O disque sur le
# chemin chaud sans jamais servir de données périmées.
_config_cache: dict = {"mtime": None, "data": {}}
_config_write_lock = threading.Lock()


def _load_config() -> dict:
    try:
        mtime = CONFIG_PATH.stat().st_mtime
    except Exception:
        return _config_cache["data"]
    if _config_cache["mtime"] != mtime:
        try:
            _config_cache["data"]  = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            _config_cache["mtime"] = mtime
        except Exception:
            pass  # on garde l'ancien cache plutôt que de renvoyer {} en cas de JSON invalide transitoire
    return _config_cache["data"]


# ── Session HTTP partagée (keep-alive / pool de connexions) ────────────────
# Une nouvelle connexion TCP par appel coûte cher en latence, surtout pour
# des échanges fréquents avec Ollama en local. Une Session réutilise les
# connexions et réduit sensiblement le round-trip des appels successifs.
def _prefer_ipv4() -> None:
    """Résout en IPv4 d'abord : certaines box ne répondent jamais aux AAAA.

    ``getaddrinfo`` en AF_UNSPEC attend les deux familles ; si le résolveur
    laisse la requête IPv6 sans réponse, l'appel bloque jusqu'au timeout et
    toute génération (image, vidéo, document) échoue en « Failed to resolve »
    alors que l'hôte est parfaitement joignable en IPv4. Désactivable avec
    ``"force_ipv4": false`` dans config/api_keys.json.
    """
    try:
        if _load_config().get("force_ipv4") is False:
            return
        import urllib3.util.connection as _urllib3_conn
        _urllib3_conn.allowed_gai_family = lambda: socket.AF_INET
    except Exception:
        pass


_session = requests.Session()
_adapter = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=8)
_session.mount("http://",  _adapter)
_session.mount("https://", _adapter)
_prefer_ipv4()


_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _post_with_retry(endpoint: str, payload: dict, timeout: int, stream: bool = False,
                      retries: int = 2, backoff: float = 0.6, headers: dict | None = None):
    """POST avec connect-timeout court (échec rapide si le serveur est down)
    et retry/backoff sur erreurs transitoires (connexion, timeout, 5xx/429).
    N'avale jamais une erreur définitive (4xx hors 429) : elle remonte direct."""
    connect_timeout = min(5, timeout)
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = _session.post(endpoint, json=payload, timeout=(connect_timeout, timeout),
                                  stream=stream, headers=headers)
            if resp.status_code in _RETRYABLE_STATUS and attempt < retries:
                delay = backoff * (2 ** attempt)
                print(f"[LLM] ⏳ HTTP {resp.status_code}, nouvelle tentative dans {delay:.1f}s "
                      f"({attempt + 1}/{retries})...")
                time.sleep(delay)
                continue
            return resp
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last_exc = e
            if attempt == retries:
                raise
            delay = backoff * (2 ** attempt)
            print(f"[LLM] ⏳ {e.__class__.__name__}, nouvelle tentative dans {delay:.1f}s "
                  f"({attempt + 1}/{retries})...")
            time.sleep(delay)
    raise last_exc  # pragma: no cover


def _get_api_key() -> str:
    """Clé Gemini pour le fallback IA (si présente)."""
    try:
        return _load_config().get("gemini_api_key", "")
    except Exception:
        return ""


# ═══════════════════════════════════════════════════════════════════════════
#  Registre multi-provider : Ollama (local) + DeepSeek, Gemini, Grok, Groq,
#  Anthropic, OpenAI-compatible (personnalisé). Chaque provider peut être
#  configuré indépendamment (clé API + modèle) et appliqué immédiatement —
#  aucun redémarrage requis, grâce au cache de config basé sur le mtime.
# ═══════════════════════════════════════════════════════════════════════════
PROVIDERS: dict[str, dict] = {
    "auto": {
        "label":         "Automatique (1re clé disponible)",
        "family":        "auto",
        "needs_key":     False,
        "default_url":   "",
        "default_model": "Sélection automatique",
        "key_field":     None,
        "url_editable":  False,
    },
    "ollama": {
        "label":         "Ollama (local)",
        "family":        "ollama",
        "needs_key":     False,
        "default_url":   "http://localhost:11434",
        "default_model": "llama3.2",
        "key_field":     None,
        "url_editable":  True,
    },
    "gemini": {
        "label":         "Gemini (Google)",
        "family":        "gemini",
        "needs_key":     True,
        "default_url":   "https://generativelanguage.googleapis.com",
        "default_model": BALANCED_MODEL,
        "key_field":     "gemini_api_key",
        "url_editable":  False,
    },
    "anthropic": {
        "label":         "Anthropic (Claude)",
        "family":        "anthropic",
        "needs_key":     True,
        "default_url":   "https://api.anthropic.com",
        "default_model": "claude-sonnet-5",
        "key_field":     "anthropic_api_key",
        "url_editable":  False,
    },
    "deepseek": {
        "label":         "DeepSeek",
        "family":        "openai_compat",
        "needs_key":     True,
        "default_url":   "https://api.deepseek.com",
        "default_model": "deepseek-v4-flash",
        "key_field":     "deepseek_api_key",
        "url_editable":  False,
    },
    "groq": {
        "label":         "Groq",
        "family":        "openai_compat",
        "needs_key":     True,
        "default_url":   "https://api.groq.com/openai/v1",
        "default_model": "llama-3.3-70b-versatile",
        "key_field":     "groq_api_key",
        "url_editable":  False,
    },
    "grok": {
        "label":         "Grok (xAI)",
        "family":        "openai_compat",
        "needs_key":     True,
        "default_url":   "https://api.x.ai/v1",
        "default_model": "grok-4.3",
        "key_field":     "xai_api_key",
        "url_editable":  False,
    },
    "openai": {
        "label":         "OpenAI",
        "family":        "openai_compat",
        "needs_key":     True,
        "default_url":   "https://api.openai.com/v1",
        "default_model": "gpt-5.6-terra",
        "key_field":     "openai_api_key",
        "url_editable":  False,
    },
    "azure_openai": {
        "label":         "Azure OpenAI",
        "family":        "azure_openai",
        "needs_key":     True,
        "default_url":   "https://VOTRE-RESSOURCE.openai.azure.com",
        # Dans Azure, ce champ est le *nom du déploiement*, pas forcément le
        # nom public du modèle. Le nom est volontairement modifiable dans l'UI.
        "default_model": "anogpt-brain",
        "key_field":     "azure_openai_api_key",
        "url_editable":  True,
    },
    "custom": {
        "label":         "Serveur compatible OpenAI",
        "family":        "openai_compat",
        "needs_key":     False,
        "default_url":   "http://localhost:1234/v1",
        "default_model": "local-model",
        "key_field":     "custom_llm_api_key",
        "url_editable":  True,
    },
}

# Ordre volontairement explicite. Seules les clés réellement renseignées sont
# candidates ; aucune API payante n'est donc activée implicitement. Gemini
# ferme la chaîne car c'est le fournisseur vocal intégré à ANO-GPT.
DEFAULT_BRAIN_PRIORITY = (
    "azure_openai", "deepseek", "grok", "openai", "anthropic", "groq", "gemini", "ollama",
)

# Modèles proposés dans l'interface. La liste n'est jamais contraignante :
# le champ reste libre, on peut taper n'importe quel identifiant.
MODEL_CATALOG: dict[str, tuple[str, ...]] = {
    "gemini": (
        BALANCED_MODEL, PINNED_PRO_MODEL, PINNED_FLASH_MODEL,
    ),
    "anthropic": (
        "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5-20251001",
    ),
    "openai": (
        "gpt-5.6-terra", "gpt-5.1", "gpt-4.1", "o4-mini",
    ),
    # Repli hors ligne seulement : en marche normale, la liste Azure vient de
    # `azure_catalog()`, qui interroge la ressource et connaît bien plus large
    # (Claude, Grok, DeepSeek, Llama…) que ces quelques valeurs sûres.
    "azure_openai": (
        "gpt-6-astra", "gpt-5.6-terra", "gpt-5.1",
        "claude-opus-5", "claude-sonnet-5", "grok-4.6", "DeepSeek-V4-Pro",
    ),
    "deepseek": (
        "deepseek-v4-flash", "deepseek-chat", "deepseek-reasoner",
    ),
    "grok": (
        "grok-4.3", "grok-4", "grok-3-mini",
    ),
    "groq": (
        "llama-3.3-70b-versatile", "qwen3-32b", "moonshotai/kimi-k2-instruct",
    ),
    "ollama": (
        "llama3.2", "qwen2.5:7b", "mistral",
    ),
    "custom": (),
}


def models_for(provider: str) -> list[str]:
    """Modèles suggérés pour l'interface, le modèle configuré en tête."""
    if provider == "auto":
        return []
    catalog = list(MODEL_CATALOG.get(provider, ()))
    info = PROVIDERS.get(provider)
    if info and info["default_model"] not in catalog:
        catalog.insert(0, info["default_model"])
    current = str(_load_config().get(f"{provider}_model", "") or "").strip()
    if current and current not in catalog:
        catalog.insert(0, current)
    return catalog


def _write_config_patch(patch: dict) -> bool:
    """Écrit un patch dans api_keys.json (lecture-fusion-écriture). Le cache mtime
    se met à jour tout seul au prochain appel : aucun redémarrage n'est nécessaire."""
    try:
        with _config_write_lock:
            cfg = dict(_load_config())
            cfg.update(patch)
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(
                dir=CONFIG_PATH.parent, prefix=f".{CONFIG_PATH.name}.", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(cfg, handle, indent=4, ensure_ascii=False)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_name, CONFIG_PATH)
            except Exception:
                Path(temp_name).unlink(missing_ok=True)
                raise
            _config_cache["mtime"] = None
        return True
    except Exception as e:
        print(f"[LLM] ⚠️ Échec écriture config : {e}")
        return False


def get_provider_info(provider: str | None = None) -> dict:
    provider = provider or get_llm_provider()
    return PROVIDERS.get(provider, PROVIDERS["gemini"])


def get_api_key_for(provider: str) -> str:
    info = PROVIDERS.get(provider, {})
    field = info.get("key_field")
    return _load_config().get(field, "") if field else ""


def _brain_priority() -> tuple[str, ...]:
    """Ordre configuré, nettoyé et complété par les valeurs sûres par défaut."""
    raw = _load_config().get("brain_provider_priority", DEFAULT_BRAIN_PRIORITY)
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.split(",")]
    requested = list(raw) if isinstance(raw, (list, tuple)) else []
    ordered: list[str] = []
    for provider in [*requested, *DEFAULT_BRAIN_PRIORITY]:
        if provider in DEFAULT_BRAIN_PRIORITY and provider not in ordered:
            ordered.append(provider)
    return tuple(ordered)


def configured_brain_providers() -> list[str]:
    """Fournisseurs cérébraux utilisables, sans jamais révéler leurs clés."""
    return [provider for provider in _brain_priority() if bool(get_api_key_for(provider))]


def provider_is_usable(provider: str) -> bool:
    """Vrai si ce fournisseur peut réellement répondre en l'état.

    Un fournisseur sans clé n'est pas « configuré » : le proposer reviendrait
    à promettre une réponse qui finirait en erreur réseau au premier tour.
    """
    info = PROVIDERS.get(provider)
    if not info or info["family"] == "auto":
        return False
    return bool(get_api_key_for(provider)) if info.get("needs_key") else True


def resolve_brain_provider() -> str | None:
    """Résout le cerveau choisi ; ``auto`` respecte l'ordre configuré."""
    selected = str(_load_config().get("brain_provider", "auto") or "auto").strip().lower()
    if selected != "auto":
        return selected if provider_is_usable(selected) else None
    providers = configured_brain_providers()
    return providers[0] if providers else None


def save_provider_key(provider: str, api_key: str) -> bool:
    """Sauvegarde la clé API d'un provider et l'applique immédiatement (pas de restart)."""
    info = PROVIDERS.get(provider)
    if not info or not info.get("key_field"):
        return False
    patch = {info["key_field"]: api_key.strip()}
    # Les nouvelles installations restent en mode automatique : enregistrer
    # DeepSeek suffit pour qu'il devienne le premier cerveau au prochain appel.
    if "brain_provider" not in _load_config():
        patch["brain_provider"] = "auto"
    return _write_config_patch(patch)


def save_provider_config(provider: str, api_key: str, model: str | None = None,
                         url: str | None = None) -> bool:
    """Enregistre clé et modèle sans modifier le fournisseur actif."""
    info = PROVIDERS.get(provider)
    if not info or not info.get("key_field"):
        return False
    patch: dict = {info["key_field"]: api_key.strip()}
    if model:
        patch[f"{provider}_model"] = model.strip()
    if url and info.get("url_editable"):
        # Azure conserve son endpoint séparément : llm_url est l'ancien champ
        # générique et peut rester celui d'Ollama/du serveur personnalisé.
        field = "azure_openai_endpoint" if provider == "azure_openai" else "llm_url"
        patch[field] = url.rstrip("/")
    if "brain_provider" not in _load_config():
        patch["brain_provider"] = "auto"
    return _write_config_patch(patch)


def save_azure_configuration(*, openai_key: str, endpoint: str, deployment: str,
                             speech_key: str = "", speech_region: str = "",
                             speech_tier: str = "f0", speech_verify: bool = False,
                             deep_model: str = "", code_model: str = "",
                             document_model: str = "", image_model: str = "",
                             video_model: str = "") -> bool:
    """Enregistre les réglages Azure depuis l'UI sans afficher de secret."""
    tier = str(speech_tier or "f0").strip().lower()
    if tier not in {"f0", "s0"}:
        tier = "f0"
    patch = {
        "azure_openai_api_key": openai_key.strip(),
        "azure_openai_endpoint": _normalise_azure_endpoint(endpoint),
        "azure_openai_model": deployment.strip() or "anogpt-brain",
        "azure_speech_key": speech_key.strip(),
        "azure_speech_region": speech_region.strip().lower(),
        "azure_speech_tier": tier,
        "azure_speech_verify": bool(speech_verify),
        "azure_deep_model": deep_model.strip(),
        "azure_code_model": code_model.strip(),
        "azure_document_model": document_model.strip(),
        "azure_image_model": image_model.strip(),
        "azure_video_model": video_model.strip(),
    }
    # Enregistrer des identifiants ne choisit rien : c'est « Appliquer ce
    # fournisseur » (set_active_provider) qui décide du cerveau. Sans cette
    # séparation, saisir une clé Azure détournait silencieusement toute la
    # réflexion vers Azure, y compris pour qui voulait juste la garder sous
    # la main.
    return _write_config_patch(patch)


def save_azure_endpoint(endpoint: str, deployment: str = "anogpt-brain") -> bool:
    """Mémorise un endpoint/déploiement sans toucher à la clé existante."""
    return _write_config_patch({
        "azure_openai_endpoint": _normalise_azure_endpoint(endpoint),
        "azure_openai_model": deployment.strip() or "anogpt-brain",
    })


def set_active_provider(provider: str, model: str | None = None, url: str | None = None) -> bool:
    """Change le fournisseur/modèle actif. Appliqué immédiatement au prochain appel LLM."""
    if provider != "auto" and provider not in PROVIDERS:
        return False
    patch: dict = {"llm_provider": provider}
    if provider == "auto":
        # Le mode automatique n'épingle aucun cerveau : c'est la liste de
        # priorité qui décide, et elle ne retient que les clés existantes.
        patch["brain_provider"] = "auto"
        return _write_config_patch(patch)
    # Le fournisseur choisi devient le cerveau de TOUT ce qui se pense :
    # conversation, outils, réflexion. Gemini ne garde que la voix.
    patch["brain_provider"] = provider
    # ``brain_provider`` règle uniquement la réflexion optionnelle. Il ne doit
    # pas servir d'état d'affichage du fournisseur de conversation.
    if model and provider != "auto":
        patch["llm_model"] = model
        patch[f"{provider}_model"] = model
    if url and PROVIDERS[provider].get("url_editable"):
        patch["azure_openai_endpoint" if provider == "azure_openai" else "llm_url"] = url.rstrip("/")
    return _write_config_patch(patch)


def list_providers() -> list[dict]:
    """État de tous les providers pour l'UI de configuration IA."""
    cfg    = _load_config()
    active = get_llm_provider()
    out = []
    for pid, info in PROVIDERS.items():
        if pid == "auto":
            has_key = bool(configured_brain_providers())
        else:
            has_key = bool(cfg.get(info["key_field"])) if info.get("key_field") else True
        out.append({
            "id":             pid,
            "label":          info["label"],
            "needs_key":      info["needs_key"],
            "has_key":        has_key,
            "url_editable":   info["url_editable"],
            "url":            (
                cfg.get("azure_openai_endpoint", info["default_url"])
                if pid == "azure_openai" else cfg.get("llm_url", info["default_url"])
            ) if info["url_editable"] else info["default_url"],
            "model":          cfg.get(f"{pid}_model") or (
                cfg.get("llm_model") if pid == active else None
            ) or info["default_model"],
            "default_model":  info["default_model"],
            "models":         models_for(pid),
            "active":         pid == active,
        })
    return out


def test_provider_key(provider: str, api_key: str = "", model: str | None = None, url: str | None = None) -> tuple[bool, str]:
    """Vérifie qu'une clé/API fonctionne réellement (appel minimal). Retourne (ok, message).
    N'écrit rien en config — sert uniquement à valider avant de sauvegarder."""
    info = PROVIDERS.get(provider)
    if provider == "auto":
        configured = configured_brain_providers()
        if not configured:
            return True, "Mode automatique prêt ; ajoutez au moins une clé de cerveau."
        labels = " → ".join(PROVIDERS[item]["label"] for item in configured)
        return True, f"Ordre actif : {labels}."
    if not info:
        return False, "Fournisseur inconnu."
    model = model or info["default_model"]
    base  = (url or info["default_url"]).rstrip("/")
    try:
        if info["family"] == "ollama":
            resp = _session.get(f"{base}/api/tags", timeout=(3, 5))
            return (True, "Ollama joignable.") if resp.status_code == 200 \
                else (False, f"Ollama a répondu {resp.status_code}.")


        if info["family"] == "azure_openai":
            if not api_key:
                return False, "Clé API Azure OpenAI requise."
            # ``GET /models`` ne prouve pas qu'un modèle est réellement
            # déployé pour cette ressource.  Tester seulement cette route
            # affichait donc un faux succès, puis consult_brain tombait en 404
            # au premier échange vocal.  Une inférence minuscule valide les
            # trois éléments qui comptent : clé, endpoint et déploiement.
            if _azure_foundry_v1(base):
                status = _azure_foundry_preflight(base, api_key)
                if status != 200:
                    return False, f"Endpoint Foundry a répondu {status or 'sans réponse'}."
            endpoint = _azure_openai_endpoint(base, model)
            token_field = "max_tokens" if model.lower().startswith("claude-") else "max_completion_tokens"
            payload = {"messages": [{"role": "user", "content": "ping"}], token_field: 4}
            if _azure_foundry_v1(base) or _azure_foundry_models(base):
                payload["model"] = model
            resp = _post_with_retry(
                endpoint, payload,
                timeout=15, headers={"api-key": api_key}, retries=0,
            )
            if resp.status_code == 200:
                return True, "Endpoint, déploiement et clé Azure valides."
            if resp.status_code == 404 and "DeploymentNotFound" in resp.text:
                return False, (
                    f"Le modèle « {model} » n'est pas déployé sur cette ressource Foundry. "
                    f"Créez-le puis retestez :  python scripts/azure_deploy_models.py "
                    f"--modeles {model}"
                )
            return False, f"Erreur {resp.status_code} : {resp.text[:180]}"

        if info["family"] == "openai_compat":
            if info["needs_key"] and not api_key:
                return False, "Clé API requise."
            chat_url = f"{_openai_v1_base(base)}/chat/completions"
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            resp = _post_with_retry(
                chat_url,
                {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 4},
                timeout=15, headers=headers, retries=0,
            )
            if resp.status_code == 200:
                return True, "Clé valide, réponse reçue."
            return False, f"Erreur {resp.status_code} : {resp.text[:180]}"


        if info["family"] == "gemini":
            if not api_key:
                return False, "Clé API requise."
            _call_gemini([{"role": "user", "content": "ping"}], None, 15, model=model, api_key=api_key)
            return True, f"Clé Gemini valide (modèle '{model}')."


        if info["family"] == "anthropic":
            if not api_key:
                return False, "Clé API requise."
            _call_anthropic([{"role": "user", "content": "ping"}], None, 15, model=model, api_key=api_key)
            return True, f"Clé Anthropic valide (modèle '{model}')."


    except RuntimeError as e:
        return False, str(e)[:220]
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"[:220]
    return False, "Type de provider non géré."


# ── Fonctions originales (généralisées au registre multi-provider) ─────────
def get_llm_provider() -> str:
    """Fournisseur choisi par l'utilisateur, ``auto`` compris."""
    raw = str(_load_config().get("llm_provider", "gemini") or "gemini").strip().lower()
    if raw == "auto":
        return "auto"
    return raw if raw in PROVIDERS else "gemini"


def get_effective_llm_provider() -> str:
    """Provider concret derrière le mode auto, avec Gemini comme dernier repli."""
    provider = get_llm_provider()
    if provider != "auto":
        return provider
    return resolve_brain_provider() or ("gemini" if get_api_key_for("gemini") else "ollama")


def get_llm_settings() -> tuple[str, str]:
    cfg   = _load_config()
    provider = get_effective_llm_provider()
    info  = get_provider_info(provider)
    if provider == "azure_openai":
        url = cfg.get("azure_openai_endpoint", info["default_url"])
    else:
        url = cfg.get("llm_url", info["default_url"]) if info["url_editable"] else info["default_url"]
    model = cfg.get(f"{provider}_model") or (
        cfg.get("llm_model") if get_llm_provider() == provider else None
    ) or info["default_model"]
    return url.rstrip("/"), model


def _openai_v1_base(url: str) -> str:
    """Normalise une base URL OpenAI-compatible pour qu'elle se termine par /v1
    (DeepSeek n'inclut pas /v1 dans son URL par défaut, Groq/Grok si)."""
    url = url.rstrip("/")
    return url if url.endswith("/v1") else f"{url}/v1"


def _normalise_azure_endpoint(endpoint: str) -> str:
    """Accepte une URL Foundry complète et en conserve seulement la base API."""
    root = str(endpoint or "").strip().rstrip("/")
    for suffix in ("/chat/completions", "/responses"):
        if root.endswith(suffix):
            root = root[:-len(suffix)]
            break
    return root


def _azure_openai_endpoint(endpoint: str, deployment: str) -> str:
    """Construit l'URL Azure OpenAI/Foundry, sans journaliser les secrets."""
    root = _normalise_azure_endpoint(endpoint)
    if not root.startswith("https://"):
        raise ValueError("Endpoint Azure invalide : une URL HTTPS est attendue.")
    deployment = deployment.strip()
    if not deployment:
        raise ValueError("Nom de déploiement Azure OpenAI manquant.")
    if ".openai.azure.com" in root and not root.endswith("/openai/v1"):
        return f"{root}/openai/deployments/{deployment}/chat/completions?api-version=2024-10-21"
    if root.endswith("/openai/v1") and (
        ".openai.azure.com" in root or ".services.ai.azure.com" in root
    ):
        return f"{root}/chat/completions"
    if root.endswith("/models") and ".services.ai.azure.com" in root:
        return f"{root}/chat/completions?api-version=2024-05-01-preview"
    raise ValueError(
        "Endpoint Azure invalide (attendu : https://<ressource>.openai.azure.com "
        "ou un endpoint Microsoft Foundry /openai/v1 ou /models)."
    )


def _azure_foundry_v1(endpoint: str) -> bool:
    return _normalise_azure_endpoint(endpoint).endswith("/openai/v1")


def _azure_foundry_models(endpoint: str) -> bool:
    return _normalise_azure_endpoint(endpoint).endswith("/models")


def _azure_foundry_preflight(endpoint: str, api_key: str) -> int | None:
    """Valide rapidement clé+endpoint Foundry sans inférence coûteuse.

    Certaines passerelles Foundry gardent une requête ``requests`` ouverte plus
    longtemps que son délai de lecture. ``curl`` avec ses limites strictes évite
    de bloquer le thread Qt ; la clé passe par stdin, jamais par la ligne de
    commande ni dans les logs.
    """
    url = f"{_normalise_azure_endpoint(endpoint)}/models"
    if shutil.which("curl"):
        config = "\n".join((
            f'url = "{url}"',
            f'header = "api-key: {api_key}"',
            "connect-timeout = 5",
            "max-time = 12",
        ))
        try:
            result = subprocess.run(
                ["curl", "--config", "-", "-sS", "-o", os.devnull, "-w", "%{http_code}"],
                input=config, text=True, capture_output=True, timeout=15,
            )
            return int(result.stdout.strip()) if result.stdout.strip().isdigit() else None
        except (OSError, subprocess.TimeoutExpired):
            return None
    try:
        return _session.get(url, timeout=(5, 12), headers={"api-key": api_key}).status_code
    except requests.RequestException:
        return None


# ── Catalogue Azure vivant ───────────────────────────────────────────────────
# Le catalogue livré en dur vieillissait plus vite qu'on ne le mettait à jour, et
# taper un nom de déploiement à la main ne produisait que des 404. La ressource
# Foundry sait dire elle-même ce qu'elle peut servir : on le lui demande.

AZURE_CATALOG_CACHE = BASE_DIR / "config" / "azure_catalog.json"
AZURE_CATALOG_TTL = 24 * 3600

# Les rôles d'ANO-GPT ne se déduisent pas des drapeaux Azure : la liste
# `capabilities` ne distingue ni l'image ni la vidéo (tout y est à faux sauf
# `inference`). On les reconnaît donc à leur famille de nom.
_AZURE_IMAGE_HINT = ("image", "dall-e", "flux", "imagen", "stable-")
_AZURE_VIDEO_HINT = ("sora", "veo", "video")
# Variantes datées et privées : même modèle, nom plus long. Les masquer garde la
# liste lisible ; un nom déjà enregistré reste sélectionnable par ailleurs.
_AZURE_NOISE = re.compile(r"(-\d{4}-\d{2}-\d{2}$|-\d{8}$|-private$)")

# Les modèles « codex » ne répondent que sur l'API Responses. ANO-GPT n'appelle
# que chat/completions : déployés ou non, ils renvoient « unsupported operation »
# dans tous les rôles. Les proposer revenait à offrir un choix cassé en tête de
# liste, puisqu'un déploiement passe devant les autres.
_AZURE_CHAT_UNSUPPORTED = re.compile(r"-codex(-|$)")

# Ordre d'affichage. Trier seulement par date de sortie faisait remonter des
# modèles de niche au-dessus de gpt-6, Claude ou Grok : on range d'abord par
# famille, puis du plus récent au plus ancien à l'intérieur de chacune.
_AZURE_FAMILIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("gpt",      ("gpt-6", "gpt-5", "gpt-4", "o4", "o3")),
    ("claude",   ("claude-",)),
    ("grok",     ("grok",)),
    ("deepseek", ("deepseek",)),
    ("mistral",  ("mistral",)),
    ("llama",    ("llama",)),
    ("qwen",     ("qwen", "kimi")),
    ("phi",      ("phi-", "mai-")),
)


def _azure_family_rank(name: str) -> int:
    lowered = name.lower()
    for rank, (_, prefixes) in enumerate(_AZURE_FAMILIES):
        if any(lowered.startswith(prefix) for prefix in prefixes):
            return rank
    return len(_AZURE_FAMILIES)


def _azure_fetch_models(endpoint: str, api_key: str, timeout: int = 40) -> list[dict]:
    """Lit `{endpoint}/models`. Passe par curl : la passerelle Foundry garde une
    connexion ``requests`` ouverte bien au-delà de son délai de lecture, ce qui
    gèlerait le thread Qt. La clé transite par stdin, jamais par argv."""
    url = f"{_normalise_azure_endpoint(endpoint)}/models"
    payload = ""
    if shutil.which("curl"):
        config = "\n".join((
            f'url = "{url}"',
            f'header = "api-key: {api_key}"',
            "connect-timeout = 6",
            f"max-time = {timeout}",
        ))
        try:
            result = subprocess.run(
                ["curl", "--config", "-", "-sS"],
                input=config, text=True, capture_output=True, timeout=timeout + 6,
            )
            payload = result.stdout
        except (OSError, subprocess.TimeoutExpired):
            return []
    else:
        try:
            payload = _session.get(
                url, timeout=(6, timeout), headers={"api-key": api_key}).text
        except requests.RequestException:
            return []
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return []
    entries = data.get("data") if isinstance(data, dict) else data
    return [item for item in (entries or []) if isinstance(item, dict) and item.get("id")]


def _azure_fetch_deployments(endpoint: str, api_key: str, timeout: int = 30,
                             served: dict | None = None) -> list[str]:
    """Déploiements réellement créés sur la ressource, prêts à répondre.

    ``/models`` liste ce que la ressource *peut* servir ; choisir là-dedans un
    modèle non déployé donnait un ``DeploymentNotFound`` à la première commande.
    On passe par curl pour la même raison que le catalogue : la passerelle
    Foundry peut tenir la connexion ouverte et gèlerait le thread Qt.
    """
    served = served if served is not None else {}
    root = _normalise_azure_endpoint(endpoint)
    base = root[: -len("/openai/v1")] if root.endswith("/openai/v1") else root
    url = f"{base}/openai/deployments?api-version=2023-03-15-preview"
    payload = ""
    if shutil.which("curl"):
        config = "\n".join((
            f'url = "{url}"',
            f'header = "api-key: {api_key}"',
            "connect-timeout = 6",
            f"max-time = {timeout}",
        ))
        try:
            result = subprocess.run(
                ["curl", "--config", "-", "-sS"],
                input=config, text=True, capture_output=True, timeout=timeout + 6,
            )
            payload = result.stdout
        except (OSError, subprocess.TimeoutExpired):
            return []
    else:
        try:
            payload = _session.get(
                url, timeout=(6, timeout), headers={"api-key": api_key}).text
        except requests.RequestException:
            return []
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return []
    entries = data.get("data") if isinstance(data, dict) else data
    names: list[str] = []
    for item in entries or []:
        if not isinstance(item, dict) or item.get("status") != "succeeded":
            continue
        # Seul l'identifiant du déploiement est appelable. Le nom du modèle
        # sous-jacent ne l'est pas : « anogpt-brain » sert gpt-5.6-terra, mais
        # appeler « gpt-5.6-terra » répond DeploymentNotFound.
        value = str(item.get("id") or "").strip()
        if value and value not in names:
            names.append(value)
            model = str(item.get("model") or "").strip()
            if model and model != value:
                served[value] = model
    return names


def _azure_zero_quota(endpoint: str, timeout: int = 60) -> list[str]:
    """Modèles dont le quota régional est nul : ils ne se déploieront jamais.

    Un modèle au catalogue sans quota se présentait comme « à déployer », et la
    commande de déploiement échouait ensuite sur ``quota nul``. Mieux vaut le
    dire dans la liste. Lecture facultative : sans ``az`` connecté, on n'affirme
    rien plutôt que d'afficher une contre-vérité.
    """
    if not shutil.which("az"):
        return []
    resource = _normalise_azure_endpoint(endpoint).split("//", 1)[-1].split(".", 1)[0]
    if not resource:
        return []
    try:
        code, out, _ = (lambda r: (r.returncode, r.stdout, r.stderr))(subprocess.run(
            ["az", "cognitiveservices", "account", "list", "--query",
             f"[?name=='{resource}'].location | [0]", "-o", "tsv"],
            capture_output=True, text=True, timeout=timeout))
        location = out.strip() if code == 0 else ""
        if not location:
            return []
        result = subprocess.run(
            ["az", "cognitiveservices", "usage", "list", "-l", location, "-o", "json"],
            capture_output=True, text=True, timeout=timeout)
        rows = json.loads(result.stdout or "[]")
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
        return []
    empty: set[str] = set()
    seen: set[str] = set()
    for row in rows:
        name = str(((row or {}).get("name") or {}).get("value") or "")
        if not name or name.endswith(".Azure"):
            continue
        model = name.split("Standard.", 1)[-1]
        seen.add(model)
        if float(row.get("limit") or 0) > 0:
            empty.discard(model)
        elif model not in empty and all(
                float(other.get("limit") or 0) <= 0
                for other in rows
                if str(((other or {}).get("name") or {}).get("value") or "")
                .split("Standard.", 1)[-1] == model):
            empty.add(model)
    return sorted(empty)


def azure_deployments(endpoint: str = "", api_key: str = "", *,
                      force: bool = False) -> list[str]:
    """Déploiements utilisables, cache disque compris."""
    cfg = _load_config()
    endpoint = endpoint or cfg.get("azure_openai_endpoint", "")
    api_key = api_key or cfg.get("azure_openai_api_key", "")
    if not (endpoint and api_key):
        return []
    if not force:
        cached = _azure_cache_entry(endpoint)
        if cached is not None:
            return list(cached.get("deployed") or [])
    return _azure_fetch_deployments(endpoint, api_key)


def _azure_classify(entry: dict) -> str | None:
    """Range un modèle du catalogue dans un rôle d'ANO-GPT, ou l'écarte."""
    if entry.get("lifecycle_status") == "deprecated":
        return None
    name = str(entry.get("id", "")).lower()
    if any(hint in name for hint in _AZURE_VIDEO_HINT):
        return "video"
    if any(hint in name for hint in _AZURE_IMAGE_HINT):
        return "image"
    if _AZURE_CHAT_UNSUPPORTED.search(name):
        return None
    caps = entry.get("capabilities") or {}
    if caps.get("chat_completion"):
        return "chat"
    return None


def _azure_cache_entry(endpoint: str) -> dict | None:
    """Entrée de cache complète (rôles + déploiements) si elle est fraîche."""
    try:
        cache = json.loads(AZURE_CATALOG_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    entry = cache.get(_normalise_azure_endpoint(endpoint))
    if not isinstance(entry, dict):
        return None
    if time.time() - float(entry.get("fetched", 0)) > AZURE_CATALOG_TTL:
        return None
    return entry


def _azure_cache_read(endpoint: str) -> dict | None:
    entry = _azure_cache_entry(endpoint)
    if entry is None:
        return None
    return entry.get("roles") if isinstance(entry.get("roles"), dict) else None


def _azure_cache_write(endpoint: str, roles: dict, deployed: list | None = None) -> None:
    try:
        cache = json.loads(AZURE_CATALOG_CACHE.read_text(encoding="utf-8"))
        if not isinstance(cache, dict):
            cache = {}
    except (OSError, ValueError, TypeError):
        cache = {}
    cache[_normalise_azure_endpoint(endpoint)] = {
        "fetched": time.time(), "roles": roles, "deployed": list(deployed or []),
    }
    try:
        AZURE_CATALOG_CACHE.parent.mkdir(parents=True, exist_ok=True)
        AZURE_CATALOG_CACHE.write_text(
            json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def azure_catalog(endpoint: str = "", api_key: str = "", *,
                  force: bool = False) -> dict[str, list[str]]:
    """Modèles réellement proposés par la ressource, du plus récent au plus ancien.

    Renvoie ``{"chat": [...], "image": [...], "video": [...]}``. En cas d'échec
    réseau, on retombe sur le cache puis sur le catalogue livré : mieux vaut une
    liste un peu datée qu'un menu vide.
    """
    cfg = _load_config()
    endpoint = endpoint or cfg.get("azure_openai_endpoint", "")
    api_key = api_key or cfg.get("azure_openai_api_key", "")
    fallback = {"chat": list(MODEL_CATALOG.get("azure_openai", ())), "image": [], "video": []}
    if not (endpoint and api_key):
        return fallback

    if not force:
        cached = _azure_cache_read(endpoint)
        if cached:
            return cached

    entries = _azure_fetch_models(endpoint, api_key)
    if not entries:
        return _azure_cache_read(endpoint) or fallback

    # Un même identifiant revient plusieurs fois (une entrée par région ou par
    # révision) : on ne garde que la plus récente, sinon la liste se répète.
    roles: dict[str, dict[str, int]] = {"chat": {}, "image": {}, "video": {}}
    for entry in entries:
        role = _azure_classify(entry)
        if role is None:
            continue
        name = str(entry["id"])
        if _AZURE_NOISE.search(name):
            continue
        created = int(entry.get("created_at") or 0)
        roles[role][name] = max(created, roles[role].get(name, 0))

    # Un modèle déployé passe devant : c'est le seul qui répondra tout de suite.
    served: dict[str, str] = {}
    deployed = _azure_fetch_deployments(endpoint, api_key, served=served)
    ranked = set(deployed)
    result = {
        role: [
            name for name, _ in sorted(
                items.items(),
                key=lambda kv: (
                    0 if kv[0] in ranked else 1,
                    _azure_family_rank(kv[0]), -kv[1], kv[0].lower(),
                ),
            )
        ]
        for role, items in roles.items()
    }
    # Un déploiement au nom libre (« anogpt-brain ») n'est dans aucun catalogue :
    # sans ça, le seul modèle qui marche à coup sûr n'apparaîtrait pas.
    known = {name for names in result.values() for name in names}
    for name in deployed:
        if name not in known and not _AZURE_CHAT_UNSUPPORTED.search(name.lower()):
            result["chat"].insert(0, name)
    result["chat"] = [n for n in result["chat"]
                      if not _AZURE_CHAT_UNSUPPORTED.search(n.lower())]
    if not result["chat"]:
        return fallback
    result["deployed"] = list(deployed)
    result["no_quota"] = _azure_zero_quota(endpoint)
    # Un déploiement peut porter un nom libre : « anogpt-brain » sert en fait
    # gpt-5.6-terra. Sans cette carte, l'utilisateur ne sait pas ce qu'il choisit.
    result["served"] = served
    _azure_cache_write(endpoint, result, deployed)
    return result


def probe_azure_deployment(endpoint: str, api_key: str, deployment: str,
                           timeout: int = 45) -> tuple[bool, str]:
    """Envoie une requête d'un jeton sur un déploiement et dit ce qui s'est passé.

    Le catalogue liste ce que la ressource *peut* servir ; il ne dit pas ce qui
    est déployé. Sans cette vérification, on enregistrait une configuration qui
    ne échouait qu'à la première commande vocale, loin du panneau de réglages.
    """
    deployment = (deployment or "").strip()
    if not deployment:
        return False, "Aucun déploiement sélectionné."
    try:
        url = _azure_openai_endpoint(endpoint, deployment)
    except ValueError as exc:
        return False, str(exc)

    # 16 jetons, pas 1 : un modèle de raisonnement dépense d'abord des jetons de
    # réflexion et rendait un 400 « output limit reached » — un déploiement qui
    # marche était alors annoncé en panne.
    body = json.dumps({
        "model": deployment,
        "messages": [{"role": "user", "content": "ok"}],
        "max_completion_tokens": 16,
    })
    status, payload = 0, ""
    if shutil.which("curl"):
        config = "\n".join((
            f'url = "{url}"',
            f'header = "api-key: {api_key}"',
            'header = "Content-Type: application/json"',
            "connect-timeout = 6",
            f"max-time = {timeout}",
        ))
        try:
            # Le corps passe par -d ; seule la clé emprunte stdin, pour qu'elle
            # n'apparaisse ni dans argv ni dans la liste des processus.
            result = subprocess.run(
                ["curl", "--config", "-", "-sS", "-w", "\n%{http_code}", "-d", body],
                input=config, text=True, capture_output=True, timeout=timeout + 6,
            )
            lines = result.stdout.rsplit("\n", 1)
            payload = lines[0]
            status = int(lines[1].strip()) if len(lines) > 1 and lines[1].strip().isdigit() else 0
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return False, "Azure n'a pas répondu dans le délai imparti."
    else:
        try:
            response = _session.post(
                url, timeout=(6, timeout), data=body,
                headers={"api-key": api_key, "Content-Type": "application/json"})
            status, payload = response.status_code, response.text
        except requests.RequestException as exc:
            return False, f"Azure injoignable : {exc}"

    if 200 <= status < 300:
        return True, f"{deployment} répond."
    try:
        error = (json.loads(payload) or {}).get("error") or {}
    except (ValueError, TypeError):
        error = {}
    code = str(error.get("code") or status)
    message = str(error.get("message") or payload)[:200]
    if "unsupported" in message.lower():
        # Un déploiement d'image ou de vidéo existe bel et bien : il refuse
        # seulement qu'on lui parle en /chat/completions. Le déclarer en panne
        # aurait masqué un déploiement parfaitement fonctionnel.
        return True, f"{deployment} est déployé (modèle non conversationnel)."
    if "output limit" in message or "max_tokens" in message:
        # Le modèle a bien répondu : il a seulement épuisé le budget de la sonde.
        # C'est la preuve que le déploiement existe et sert des requêtes.
        return True, f"{deployment} répond (réponse tronquée par la sonde)."
    if code == "DeploymentNotFound" or status == 404:
        return False, (
            f"« {deployment} » est au catalogue mais n'a aucun déploiement dans "
            "cette ressource. Crée-le dans Azure AI Foundry → Deployments, sous "
            "exactement ce nom."
        )
    if status in (401, 403):
        return False, "Clé ou endpoint refusés par Azure."
    if status == 429:
        return False, "Quota Azure atteint pour ce déploiement."
    return False, f"Azure a répondu {code} : {message}"


def ensure_ollama_running(timeout: int = 15) -> bool:
    url, model = get_llm_settings()
    provider = get_effective_llm_provider()
    family   = get_provider_info(provider)["family"]


    if family != "ollama":
        # Fournisseurs cloud (ou serveur OpenAI-compatible externe) : rien à démarrer localement.
        # On vérifie juste la joignabilité pour donner un diagnostic utile.
        if family == "azure_openai":
            try:
                # Azure OpenAI ne propose pas une liste de modèles exploitable
                # dans tous les tenants ; un ping de déploiement est plus fiable.
                ok, _ = test_provider_key(provider, get_api_key_for(provider), model, url)
                return ok
            except Exception:
                return False
        if family == "openai_compat":
            try:
                headers = {"Authorization": f"Bearer {get_api_key_for(provider)}"} if get_api_key_for(provider) else {}
                ok = _session.get(f"{_openai_v1_base(url)}/models", timeout=(3, 5), headers=headers).status_code in (200, 401)
                print(f"[LLM] Serveur OpenAI-compatible {'joignable' if ok else 'injoignable'} à {url}")
                return ok
            except Exception:
                print(f"[LLM] Impossible de joindre le serveur OpenAI-compatible à {url}.")
                return False
        return True  # gemini/anthropic : pas de process local à surveiller


    health = f"{url}/api/tags"
    def _is_up() -> bool:
        try:
            return _session.get(health, timeout=(2, 3)).status_code == 200
        except Exception:
            return False


    if _is_up():
        return True


    print("[LLM] Ollama not running — launching 'ollama serve'…")
    try:
        kwargs: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        subprocess.Popen(["ollama", "serve"], **kwargs)
    except FileNotFoundError:
        print("[LLM] 'ollama' command not found. Install Ollama from https://ollama.com")
        return False
    except Exception as e:
        print(f"[LLM] Could not launch Ollama: {e}")
        return False


    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(1.0)
        if _is_up():
            print("[LLM] Ollama started successfully.")
            return True
    print("[LLM] Ollama did not respond within the timeout.")
    return False


def warmup_model(system_prompt: str | None = None) -> bool:
    url, model = get_llm_settings()
    provider   = get_effective_llm_provider()
    family     = get_provider_info(provider)["family"]
    print(f"[LLM] Warming up '{model}' ({provider})…")


    messages: list[dict] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": "hi"})


    if family == "azure_openai":
        try:
            resp = _session.post(
                _azure_openai_endpoint(url, model), json={"messages": messages, "max_completion_tokens": 1},
                timeout=(5, 180), headers={"api-key": get_api_key_for(provider)},
            )
            resp.raise_for_status()
            print(f"[LLM] '{model}' ready (Azure OpenAI).")
            return True
        except Exception as e:
            print(f"[LLM] Warmup Azure failed (non-fatal): {e}")
            return False
    if family == "openai_compat":
        payload = {"model": model, "messages": messages, "stream": False, "max_tokens": 1}
        headers = {"Authorization": f"Bearer {get_api_key_for(provider)}"} if get_api_key_for(provider) else {}
        try:
            resp = _session.post(f"{_openai_v1_base(url)}/chat/completions", json=payload,
                                  timeout=(5, 180), headers=headers)
            resp.raise_for_status()
            print(f"[LLM] '{model}' ready (OpenAI-compatible server).")
            return True
        except Exception as e:
            print(f"[LLM] Warmup failed (non-fatal): {e}")
            return False


    if family in ("gemini", "anthropic"):
        # Providers cloud à requête/réponse unique : rien à "chauffer" côté serveur.
        return True


    payload = {
        "model": model, "messages": messages, "stream": False, "keep_alive": -1,
        "options": {"num_predict": 1, "num_gpu": 99},
    }
    try:
        resp = _session.post(f"{url}/api/chat", json=payload, timeout=(5, 180))
        resp.raise_for_status()
        print(f"[LLM] '{model}' loaded and KV cache primed.")
        return True
    except Exception as e:
        print(f"[LLM] Warmup failed (non-fatal): {e}")
        return False


def check_model_available(log: Callable | None = None) -> bool:
    if get_effective_llm_provider() != "ollama":
        return True
    url, model = get_llm_settings()
    try:
        resp = _session.get(f"{url}/api/tags", timeout=(3, 5))
        resp.raise_for_status()
        pulled = [m.get("name", "") for m in resp.json().get("models", [])]
        model_base = model.split(":")[0]
        found = any(m == model or m == model_base or m.startswith(model_base + ":") for m in pulled)
        if not found:
            available = ", ".join(pulled) if pulled else "none"
            warn = f"WRN: Model '{model}' is not pulled in Ollama.\n     Available: {available}\n     Fix: ollama pull {model}"
            print(warn)
            if log:
                log(f"WRN: '{model}' not found — run: ollama pull {model}")
        return found
    except Exception:
        return True


def _normalize_openai_tool_calls(raw_tc: list) -> list:
    return [
        {
            "id": t.get("id", ""),
            "function": {
                "name": t["function"]["name"],
                "arguments": (
                    json.loads(t["function"]["arguments"])
                    if isinstance(t["function"].get("arguments"), str)
                    else t["function"].get("arguments", {})
                ),
            },
        }
        for t in raw_tc
    ]


def _call_openai_compat(messages: list, tools: list | None, timeout: int,
                         provider: str | None = None, model: str | None = None,
                         api_key: str | None = None, url: str | None = None) -> dict:
    """DeepSeek, Groq, Grok (xAI), OpenAI-compatible perso (LM Studio/LocalAI/Jan) —
    tous parlent le même dialecte /v1/chat/completions, seule l'auth Bearer diffère."""
    provider = provider or get_llm_provider()
    info     = get_provider_info(provider)
    # get_llm_settings() décrit le fournisseur *actif*. Quand un appel force un
    # autre fournisseur (réflexion, relais), son URL ne doit pas fuiter ici —
    # mais quand seul le modèle est forcé, il faut quand même une URL valide,
    # sinon la base restait None et l'appel explosait avant le réseau.
    if url is None and model is None:
        base_url, model = get_llm_settings()
    else:
        cfg = _load_config()
        base_url = url or (
            cfg.get("llm_url", info["default_url"]) if info["url_editable"]
            else info["default_url"]
        )
        model = model or cfg.get(f"{provider}_model") or info["default_model"]
    api_key  = api_key if api_key is not None else get_api_key_for(provider)


    endpoint = f"{_openai_v1_base(base_url)}/chat/completions"
    # Les déploiements GPT-5 récents attendent max_completion_tokens ; garder
    # max_tokens pour les autres serveurs compatibles OpenAI.
    token_field = (
        "max_completion_tokens"
        if provider == "openai" and str(model).strip().lower().startswith("gpt-5")
        else "max_tokens"
    )
    payload: dict = {"model": model, "messages": messages, "stream": False, token_field: 800}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        resp = _post_with_retry(endpoint, payload, timeout, headers=headers)
        resp.raise_for_status()
        choice = resp.json().get("choices", [{}])[0]
        msg    = choice.get("message", {})
        tc     = _normalize_openai_tool_calls(msg.get("tool_calls") or [])
        return {"content": (msg.get("content") or "").strip(), "tool_calls": tc}
    except requests.exceptions.HTTPError as e:
        body = ""
        try:
            body = e.response.text[:200]
        except Exception:
            pass
        raise RuntimeError(f"{info['label']} a répondu {e.response.status_code} : {body}")
    except Exception as e:
        raise RuntimeError(f"Appel {info['label']} échoué : {e}")


def _call_azure_openai(messages: list, tools: list | None, timeout: int,
                       *, model: str | None = None, api_key: str | None = None,
                       url: str | None = None) -> dict:
    """Appel Azure OpenAI déployé : auth ``api-key`` et déploiement dans l'URL."""
    cfg = _load_config()
    # Cette fonction est aussi appelée par ``think_deep`` alors que Gemini
    # demeure le provider vocal actif : ne jamais réutiliser son URL ici.
    base_url = cfg.get("azure_openai_endpoint", PROVIDERS["azure_openai"]["default_url"])
    configured_model = cfg.get("azure_openai_model", PROVIDERS["azure_openai"]["default_model"])
    deployment = model or configured_model
    key = api_key if api_key is not None else get_api_key_for("azure_openai")
    if not key:
        raise RuntimeError("Aucune clé Azure OpenAI configurée.")
    # Azure AI Foundry exposes non-OpenAI models (notably Claude) through the
    # OpenAI-compatible endpoint too.  Claude accepts ``max_tokens`` there,
    # while the GPT-5 deployments require ``max_completion_tokens``.  Sending
    # the GPT-only field to Claude made the relay appear to hang/fail before it
    # could return a spoken answer.
    token_field = (
        "max_tokens"
        if str(deployment).strip().lower().startswith("claude-")
        else "max_completion_tokens"
    )
    payload: dict = {"messages": messages, "stream": False, token_field: 800}
    if _azure_foundry_v1(url or base_url) or _azure_foundry_models(url or base_url):
        payload["model"] = deployment
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    try:
        resp = _post_with_retry(
            _azure_openai_endpoint(url or base_url, deployment), payload, timeout,
            headers={"api-key": key},
        )
        resp.raise_for_status()
        msg = resp.json().get("choices", [{}])[0].get("message", {})
        return {"content": (msg.get("content") or "").strip(),
                "tool_calls": _normalize_openai_tool_calls(msg.get("tool_calls") or [])}
    except requests.exceptions.HTTPError as e:
        body = e.response.text[:200] if e.response is not None else ""
        raise RuntimeError(f"Azure OpenAI a répondu {e.response.status_code} : {body}")
    except Exception as e:
        raise RuntimeError(f"Appel Azure OpenAI échoué : {e}")


def _call_gemini(messages: list, tools: list | None, timeout: int,
                  model: str | None = None, api_key: str | None = None) -> dict:
    from concurrent.futures import ThreadPoolExecutor
    from google import genai
    from google.genai import types


    model   = model or PROVIDERS["gemini"]["default_model"]
    api_key = api_key if api_key is not None else get_api_key_for("gemini")
    if not api_key:
        raise RuntimeError("Aucune clé API Gemini configurée.")


    def _do():
        client = genai.Client(api_key=api_key)
        system_instruction = None
        contents = []
        for m in messages:
            role, text = m.get("role"), m.get("content") or ""
            if role == "system":
                system_instruction = (system_instruction + "\n" if system_instruction else "") + text
                continue
            if text:
                contents.append(types.Content(role="model" if role == "assistant" else "user",
                                               parts=[types.Part(text=text)]))
        gen_tools = None
        if tools:
            decls = []
            for t in tools:
                fn = t.get("function", t)
                decls.append(types.FunctionDeclaration(
                    name=fn.get("name", ""),
                    description=fn.get("description", ""),
                    parameters=fn.get("parameters") or {"type": "object", "properties": {}},
                ))
            gen_tools = [types.Tool(function_declarations=decls)]
        config = types.GenerateContentConfig(
            system_instruction=system_instruction, tools=gen_tools, max_output_tokens=800,
            # Les outils sont de simples déclarations : c'est le répartiteur
            # d'ANO-GPT qui les exécute. Sans ce réglage, le SDK active son
            # appel automatique de fonctions, ne trouve aucun callable et
            # avertit « Direct use of AFC » à chaque requête.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        resp = client.models.generate_content(model=model, contents=contents, config=config)
        text_out, tool_calls = "", []
        cand = resp.candidates[0] if resp.candidates else None
        if cand and cand.content and cand.content.parts:
            for i, part in enumerate(cand.content.parts):
                if getattr(part, "text", None):
                    text_out += part.text
                fc = getattr(part, "function_call", None)
                if fc:
                    tool_calls.append({
                        "id": f"gemini_call_{i}",
                        "function": {"name": fc.name, "arguments": dict(fc.args) if fc.args else {}},
                    })
        return {"content": text_out.strip(), "tool_calls": tool_calls}


    try:
        with ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(_do).result(timeout=timeout)
    except FutureTimeout:
        raise RuntimeError(f"Gemini a dépassé le délai de {timeout}s.")
    except Exception as e:
        raise RuntimeError(f"Appel Gemini échoué : {e}")


def _call_anthropic(messages: list, tools: list | None, timeout: int,
                     model: str | None = None, api_key: str | None = None) -> dict:
    model   = model or PROVIDERS["anthropic"]["default_model"]
    api_key = api_key if api_key is not None else get_api_key_for("anthropic")
    if not api_key:
        raise RuntimeError("Aucune clé API Anthropic configurée.")


    system_text, conv = "", []
    for m in messages:
        role, content = m.get("role"), m.get("content") or ""
        if role == "system":
            system_text += ("\n" if system_text else "") + content
            continue
        if content:
            conv.append({"role": "assistant" if role == "assistant" else "user", "content": content})


    payload: dict = {"model": model, "max_tokens": 800, "messages": conv}
    if system_text:
        payload["system"] = system_text
    if tools:
        payload["tools"] = [
            {
                "name":         (t.get("function", t)).get("name", ""),
                "description":  (t.get("function", t)).get("description", ""),
                "input_schema": (t.get("function", t)).get("parameters") or {"type": "object", "properties": {}},
            }
            for t in tools
        ]
    headers = {
        "x-api-key":         api_key,
        "anthropic-version": "2023-06-01",
        "content-type":      "application/json",
    }
    try:
        resp = _post_with_retry(f"{PROVIDERS['anthropic']['default_url']}/v1/messages", payload, timeout, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        text_out, tool_calls = "", []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text_out += block.get("text", "")
            elif block.get("type") == "tool_use":
                tool_calls.append({
                    "id": block.get("id", ""),
                    "function": {"name": block.get("name", ""), "arguments": block.get("input", {})},
                })
        return {"content": text_out.strip(), "tool_calls": tool_calls}
    except requests.exceptions.HTTPError as e:
        body = ""
        try:
            body = e.response.text[:200]
        except Exception:
            pass
        raise RuntimeError(f"Anthropic a répondu {e.response.status_code} : {body}")
    except Exception as e:
        raise RuntimeError(f"Appel Anthropic échoué : {e}")


def call_llm(messages: list, tools: list | None = None, timeout: int = 120) -> dict:
    provider = get_effective_llm_provider()
    family   = get_provider_info(provider)["family"]


    if family == "gemini":
        return _call_gemini(messages, tools, timeout)
    if family == "anthropic":
        return _call_anthropic(messages, tools, timeout)
    if family == "azure_openai":
        return _call_azure_openai(messages, tools, timeout)
    if family == "openai_compat":
        return _call_openai_compat(messages, tools, timeout, provider=provider)


    url, model = get_llm_settings()


    endpoint = f"{url}/api/chat"
    payload = {"model": model, "messages": messages, "stream": False, "keep_alive": -1,
               "options": {"num_predict": 150, "num_gpu": 99}}
    if tools:
        payload["tools"] = tools
    try:
        resp = _post_with_retry(endpoint, payload, timeout)
        resp.raise_for_status()
        data = resp.json()
        msg  = data.get("message", {})
        return {"content": (msg.get("content") or "").strip(), "tool_calls": msg.get("tool_calls") or []}
    except requests.exceptions.ConnectionError as e:
        print(f"[LLM] ConnectionError — trying to restart Ollama… ({e})")
        if ensure_ollama_running():
            try:
                resp = _post_with_retry(endpoint, payload, timeout)
                resp.raise_for_status()
                data = resp.json()
                msg  = data.get("message", {})
                return {"content": (msg.get("content") or "").strip(), "tool_calls": msg.get("tool_calls") or []}
            except Exception:
                pass
        raise RuntimeError(f"Cannot connect to Ollama at {url}. Make sure Ollama is installed and run: ollama serve")
    except requests.exceptions.Timeout:
        raise RuntimeError(f"Ollama request timed out after {timeout} s.")
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(f"Ollama HTTP error: {e.response.status_code}")
    except Exception as e:
        raise RuntimeError(f"LLM call failed: {e}")


def call_llm_text(prompt: str, system: str | None = None, model: str | None = None, timeout: int = 120) -> str:
    """Appel texte simple (sans tools), quel que soit le provider actif.
    'model' permet de forcer un modèle ponctuellement (ex: variante rapide/légère)."""
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})


    if model:
        # Appel avec un modèle explicite différent du modèle actif configuré :
        # on route directement vers le bon handler de famille plutôt que de
        # passer par call_llm() qui utiliserait le modèle configuré par défaut.
        provider = get_effective_llm_provider()
        family   = get_provider_info(provider)["family"]
        if family == "gemini":
            return _call_gemini(messages, None, timeout, model=model).get("content", "")
        if family == "anthropic":
            return _call_anthropic(messages, None, timeout, model=model).get("content", "")
        if family == "azure_openai":
            return _call_azure_openai(messages, None, timeout, model=model).get("content", "")
        if family == "openai_compat":
            return _call_openai_compat(messages, None, timeout, provider=provider, model=model).get("content", "")
        # Ollama : on passe par le payload direct pour honorer num_predict=600 (réponses plus longues)
        url, _ = get_llm_settings()
        endpoint = f"{url}/api/chat"
        payload = {"model": model, "messages": messages, "stream": False, "keep_alive": -1,
                   "options": {"num_predict": 600}}
        try:
            resp = _post_with_retry(endpoint, payload, timeout)
            resp.raise_for_status()
            return (resp.json().get("message", {}).get("content") or "").strip()
        except requests.exceptions.ConnectionError:
            if ensure_ollama_running():
                resp = _post_with_retry(endpoint, payload, timeout)
                resp.raise_for_status()
                return (resp.json().get("message", {}).get("content") or "").strip()
            raise RuntimeError(f"Cannot connect to Ollama at {url}. Make sure Ollama is installed and run: ollama serve")
        except Exception as e:
            raise RuntimeError(f"LLM text call failed: {e}")


    return call_llm(messages, tools=None, timeout=timeout).get("content", "")




# ═══════════════════════════════════════════════════════════════════════════
#  Réflexion déléguée à un cerveau externe optionnel
# ═══════════════════════════════════════════════════════════════════════════
#  La voix et le pilotage courant restent sur Gemini Live : y insérer un
#  aller-retour réseau casserait le flux naturel de la parole. On ne délègue
#  que ce qui mérite de réfléchir — analyse, code, comparaison, plan — et le
#  résultat est ensuite prononcé par la voix habituelle.
#
#  Le mode auto essaie uniquement les fournisseurs dont une clé existe, dans
#  l'ordre DeepSeek → Grok → OpenAI → Claude. Il n'active ni ne facture donc
#  jamais un service que l'utilisateur n'a pas lui-même configuré.
# ═══════════════════════════════════════════════════════════════════════════
BRAIN_UNCONFIGURED = "__brain_unconfigured__"
# Alias conservé pour les extensions qui importaient l'ancien nom.
DEEPSEEK_UNCONFIGURED = BRAIN_UNCONFIGURED




def think_deep(question: str, context: str = "", timeout: int = 60) -> str:
    """Interroge le cerveau sélectionné avec repli entre clés configurées.

    Aucune clé : renvoie le sentinel historique, afin que Gemini/agy continue
    exactement comme avant. Une panne d'un fournisseur n'empêche pas le
    suivant de répondre.
    """
    if not question.strip():
        return "La question de réflexion est vide."
    selected = str(_load_config().get("brain_provider", "auto") or "auto").strip().lower()
    if selected == "auto":
        candidates = configured_brain_providers()
    else:
        # Le cerveau choisi passe d'abord ; les autres clés restent un filet
        # de secours plutôt qu'un silence quand ce fournisseur est en panne.
        candidates = ([selected] if resolve_brain_provider() else []) + [
            item for item in configured_brain_providers() if item != selected
        ]
    if not candidates:
        return BRAIN_UNCONFIGURED

    system = (
        "Tu es le module de réflexion d'un assistant vocal francophone. "
        "Réponds en français, de façon dense et directement utile : ta réponse "
        "va être lue à voix haute. Pas de markdown, pas de listes à puces, pas "
        "de titres — des phrases. Va droit au fond, sans préambule ni formule "
        "de politesse. Si la question appelle une commande shell ou du code, "
        "donne-la précisément et explique en une phrase ce qu'elle fait."
    )
    user = f"{context}\n\n{question}".strip() if context else question
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    errors: list[str] = []
    deadline = time.monotonic() + max(5, timeout)
    for provider in candidates:
        info = PROVIDERS[provider]
        remaining = max(5, int(deadline - time.monotonic()))
        try:
            model = _load_config().get(f"{provider}_model") or info["default_model"]
            if provider == "azure_openai":
                model = _load_config().get("azure_deep_model") or model
            if info["family"] == "anthropic":
                result = _call_anthropic(
                    messages, None, remaining, model=model,
                    api_key=get_api_key_for(provider),
                )
            elif info["family"] == "azure_openai":
                result = _call_azure_openai(
                    messages, None, remaining, model=model,
                    api_key=get_api_key_for(provider),
                )
            elif info["family"] == "gemini":
                # Gemini ferme la liste de priorité : sans ce cas, sa clé
                # partait vers /v1/chat/completions, une URL qu'il ne sert pas.
                result = _call_gemini(
                    messages, None, remaining, model=model,
                    api_key=get_api_key_for(provider),
                )
            else:
                result = _call_openai_compat(
                    messages, None, remaining, provider=provider, model=model,
                    api_key=get_api_key_for(provider), url=info["default_url"],
                )
            content = result.get("content", "").strip()
            if content:
                print(f"[Brain] Réflexion fournie par {info['label']} ({model}).")
                return content
            errors.append(f"{info['label']}: réponse vide")
        except Exception as exc:
            errors.append(f"{info['label']}: {str(exc)[:120]}")
            print(f"[Brain] ⚠️ Repli après échec {errors[-1]}", file=sys.stderr)
        if time.monotonic() >= deadline:
            break
    return (
        "Les cerveaux externes configurés sont temporairement indisponibles. "
        "Réponds toi-même à la demande sans relancer l'outil."
    )




# ═══════════════════════════════════════════════════════════════════════════
#  Cerveau principal — le fournisseur que l'utilisateur a choisi
# ═══════════════════════════════════════════════════════════════════════════
#  Gemini Live reste les oreilles et la voix : c'est lui qui entend le micro et
#  qui parle. Mais dès qu'un autre fournisseur est choisi, c'est lui qui pense
#  — analyse, décision, appels d'outils. Les fonctions ci-dessous adressent ce
#  fournisseur *explicitement* : elles ne retombent jamais sur la configuration
#  vocale, faute de quoi choisir Azure aurait continué à faire répondre Gemini.
# ═══════════════════════════════════════════════════════════════════════════


def main_brain() -> tuple[str, str] | None:
    """(fournisseur, modèle) du cerveau choisi, ou None s'il n'y en a pas.

    Renvoie None quand la conversation doit rester chez Gemini Live : ni
    fournisseur choisi, ni clé utilisable derrière le mode automatique.
    """
    selected = get_llm_provider()
    if selected == "auto":
        selected = resolve_brain_provider() or ""
    if not selected or selected == "gemini":
        return None
    if not provider_is_usable(selected):
        return None
    cfg = _load_config()
    if selected == "azure_openai":
        model = cfg.get("azure_openai_model") or PROVIDERS[selected]["default_model"]
    else:
        model = cfg.get(f"{selected}_model") or PROVIDERS[selected]["default_model"]
    return selected, str(model)


def relay_active() -> bool:
    """Vrai quand Gemini Live ne doit plus répondre de lui-même."""
    return main_brain() is not None


def main_brain_label() -> str:
    """Nom lisible du cerveau actif, pour l'interface et le prompt."""
    brain = main_brain()
    if brain is None:
        return "Gemini Live"
    provider, model = brain
    return f"{PROVIDERS[provider]['label']} ({model})"


def call_brain(messages: list, tools: list | None = None, timeout: int = 90) -> dict:
    """Un tour du cerveau principal, avec ses outils. Lève si aucun n'est choisi."""
    brain = main_brain()
    if brain is None:
        raise RuntimeError("Aucun cerveau externe sélectionné.")
    provider, model = brain
    info = PROVIDERS[provider]
    family = info["family"]
    cfg = _load_config()
    api_key = get_api_key_for(provider)
    if family == "azure_openai":
        return _call_azure_openai(
            messages, tools, timeout, model=model, api_key=api_key,
            url=cfg.get("azure_openai_endpoint", info["default_url"]),
        )
    if family == "anthropic":
        return _call_anthropic(messages, tools, timeout, model=model, api_key=api_key)
    if family == "gemini":
        return _call_gemini(messages, tools, timeout, model=model, api_key=api_key)
    if family == "ollama":
        url = cfg.get("llm_url", info["default_url"]).rstrip("/")
        payload = {"model": model, "messages": messages, "stream": False,
                   "keep_alive": -1, "options": {"num_predict": 600}}
        if tools:
            payload["tools"] = tools
        resp = _post_with_retry(f"{url}/api/chat", payload, timeout)
        resp.raise_for_status()
        msg = resp.json().get("message", {})
        return {"content": (msg.get("content") or "").strip(),
                "tool_calls": msg.get("tool_calls") or []}
    url = cfg.get("llm_url", info["default_url"]) if info["url_editable"] else info["default_url"]
    return _call_openai_compat(
        messages, tools, timeout, provider=provider, model=model,
        api_key=api_key, url=url,
    )


def _stream_openai(messages: list, tools: list | None, timeout: int, provider: str | None = None) -> Generator[dict, None, None]:
    provider = provider or get_effective_llm_provider()
    url, model = get_llm_settings()
    azure = get_provider_info(provider)["family"] == "azure_openai"
    endpoint = _azure_openai_endpoint(url, model) if azure else f"{_openai_v1_base(url)}/chat/completions"
    payload: dict = {"messages": messages, "stream": True,
                     "max_completion_tokens": 800} if azure else {
                         "messages": messages, "stream": True, "max_tokens": 800
                     }
    if not azure:
        payload["model"] = model
    elif _azure_foundry_v1(url) or _azure_foundry_models(url):
        payload["model"] = model
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    api_key = get_api_key_for(provider)
    headers = {"api-key": api_key} if azure else ({"Authorization": f"Bearer {api_key}"} if api_key else {})
    try:
        resp = _post_with_retry(endpoint, payload, timeout, stream=True, headers=headers)
        with resp:
            resp.raise_for_status()
            full_content = ""
            buf          = ""
            tc_fragments: dict[int, dict] = {}
            for raw in resp.iter_lines():
                if not raw:
                    continue
                line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choice = chunk.get("choices", [{}])[0]
                delta  = choice.get("delta", {})
                text   = delta.get("content") or ""
                full_content += text
                buf          += text
                while True:
                    m = _SENT_END.search(buf)
                    if not m:
                        break
                    sentence = buf[: m.start() + 1].strip()
                    buf      = buf[m.end():]
                    if sentence:
                        yield {"type": "sentence", "text": sentence}
                for tc in (delta.get("tool_calls") or []):
                    idx = tc.get("index", 0)
                    if idx not in tc_fragments:
                        tc_fragments[idx] = {"id": "", "function": {"name": "", "arguments": ""}}
                    frag = tc_fragments[idx]
                    frag["id"] = frag["id"] or tc.get("id", "")
                    fn = tc.get("function", {})
                    frag["function"]["name"]      += fn.get("name") or ""
                    frag["function"]["arguments"] += fn.get("arguments") or ""
                finish = choice.get("finish_reason")
                if finish in ("stop", "tool_calls", "length"):
                    break
            if buf.strip():
                yield {"type": "sentence", "text": buf.strip()}
            tool_calls: list = []
            for idx in sorted(tc_fragments):
                frag = tc_fragments[idx]
                args = frag["function"]["arguments"]
                try:
                    args = json.loads(args)
                except Exception:
                    pass
                tool_calls.append({
                    "id": frag["id"],
                    "function": {"name": frag["function"]["name"], "arguments": args},
                })
            yield {"type": "done", "content": full_content.strip(), "tool_calls": tool_calls}
    except requests.exceptions.ConnectionError:
        raise RuntimeError(f"Cannot reach OpenAI-compatible server at {url}.\nMake sure LM Studio / LocalAI / Jan is running and the server is started.")
    except requests.exceptions.Timeout:
        raise RuntimeError("OpenAI-compatible stream timed out.")
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(f"OpenAI-compatible HTTP error: {e.response.status_code}")
    except Exception as e:
        raise RuntimeError(f"OpenAI-compatible stream failed: {e}")


def _pseudo_stream(messages: list, tools: list | None, timeout: int, family: str) -> Generator[dict, None, None]:
    """Gemini/Anthropic : pas de streaming incrémental implémenté ici (nécessiterait
    un décodage SSE spécifique à chaque provider). On fait un appel complet puis on
    restitue le résultat au format attendu par les consommateurs de call_llm_stream —
    fonctionnellement correct, juste sans le token-par-token. À améliorer plus tard
    si besoin d'un vrai streaming pour ces deux providers."""
    result = _call_gemini(messages, tools, timeout) if family == "gemini" else _call_anthropic(messages, tools, timeout)
    content = result.get("content", "")
    if content.strip():
        yield {"type": "sentence", "text": content.strip()}
    yield {"type": "done", "content": content, "tool_calls": result.get("tool_calls", [])}


def call_llm_stream(messages: list, tools: list | None = None, timeout: int = 120) -> Generator[dict, None, None]:
    provider = get_llm_provider()
    family   = get_provider_info(provider)["family"]


    if family in ("openai_compat", "azure_openai"):
        yield from _stream_openai(messages, tools, timeout, provider=provider)
        return
    if family in ("gemini", "anthropic"):
        yield from _pseudo_stream(messages, tools, timeout, family)
        return


    url, model = get_llm_settings()
    endpoint   = f"{url}/api/chat"
    payload: dict = {"model": model, "messages": messages, "stream": True, "keep_alive": -1,
                     "options": {"num_predict": 150, "num_gpu": 99}}
    if tools:
        payload["tools"] = tools
    def _do_stream() -> Generator[dict, None, None]:
        resp = _post_with_retry(endpoint, payload, timeout, stream=True)
        with resp:
            resp.raise_for_status()
            full_content = ""
            tool_calls: list = []
            buf = ""
            for raw in resp.iter_lines():
                if not raw:
                    continue
                try:
                    chunk = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                msg   = chunk.get("message", {})
                delta = msg.get("content") or ""
                full_content += delta
                buf          += delta
                while True:
                    m = _SENT_END.search(buf)
                    if not m:
                        break
                    sentence = buf[: m.start() + 1].strip()
                    buf      = buf[m.end():]
                    if sentence:
                        yield {"type": "sentence", "text": sentence}
                tc = msg.get("tool_calls")
                if tc:
                    tool_calls.extend(tc)
                if chunk.get("done"):
                    if buf.strip():
                        yield {"type": "sentence", "text": buf.strip()}
                    yield {"type": "done", "content": full_content.strip(), "tool_calls": tool_calls}
                    return
    try:
        yield from _do_stream()
    except requests.exceptions.ConnectionError as e:
        print(f"[LLM] Stream ConnectionError — trying to restart Ollama… ({e})")
        if ensure_ollama_running():
            yield from _do_stream()
            return
        raise RuntimeError(f"Cannot connect to Ollama at {url}. Make sure Ollama is installed and run: ollama serve")
    except requests.exceptions.Timeout:
        raise RuntimeError("Ollama stream timed out.")
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(f"Ollama HTTP error: {e.response.status_code}")
    except Exception as e:
        raise RuntimeError(f"LLM stream failed: {e}")


# ═══════════════════════════════════════════════════════════════════════════
#  NOUVEAU : Contrôle de l'inférence locale en langage naturel
# ═══════════════════════════════════════════════════════════════════════════
def _parse_llm_command_locally(text: str) -> Optional[Dict[str, Any]]:
    """
    Extrait l'action et les paramètres pour le contrôle du LLM local.
    Commandes supportées :
    - "change le modèle pour llama3.2"
    - "liste les modèles disponibles"
    - "télécharge le modèle mistral"
    - "redémarre Ollama"
    - "quel fournisseur est utilisé ?"
    - "passe au fournisseur ollama" / "utilise le serveur local OpenAI"
    - "quel est le modèle actuel ?"
    - "vérifie si le serveur est en ligne"
    """
    text = text.lower().strip()
    text = re.sub(r"\b(s'il te pla[iî]t|stp|please|peux-tu|tu peux|je veux|j'aimerais)\b", "", text).strip()


    # ---- Changement de modèle ----
    m = re.search(r"(?:change|passe|mets?|utilise|définis?|set|switch)\s+(?:le |au |de |à |vers le |vers )?(?:modèle|model)[\s:]*(.+)", text)
    if m:
        model = m.group(1).strip().strip("'\"")
        return {"action": "set_model", "model": model}


    # ---- Téléchargement (pull) d'un modèle ----
    m = re.search(r"(?:télécharge|récupère|pull|download|installe)\s+(?:le |le modèle |le modèle )?['\"]?(.+?)['\"]?(?:\s+(?:modèle|model))?$", text)
    if m:
        model = m.group(1).strip()
        return {"action": "pull_model", "model": model}


    # ---- Liste des modèles ----
    if re.search(r"\b(liste|affiche|montre|quels?|quelles?|show|list)\s+(?:les |tous les |les )?(?:modèles?|models?)\b", text):
        return {"action": "list_models"}


    # ---- Statut / vérification ----
    if re.search(r"\b(vérifie|check|est-ce que|est[\s-]ce que|le serveur|le service|ollama|en ligne|running|démarré|actif)\b", text):
        return {"action": "check_status"}


    # ---- Redémarrage ----
    if re.search(r"\b(redémarre|relance|restart|reconnecte?)\s+(?:le |les |le serveur |ollama|le backend)\b", text):
        return {"action": "restart"}


    # ---- Changement de fournisseur ----
    m = re.search(r"(?:passe|utilise|bascule|change|set|switch)\s+(?:au |vers |le |le fournisseur )?(?:fournisseur|provider|backend)[\s:]*['\"]?(ollama|openai|lmstudio|localai)['\"]?", text)
    if m:
        provider = m.group(1).strip().lower()
        return {"action": "set_provider", "provider": provider}


    # ---- Modèle actuel ----
    if re.search(r"\b(quel|quel est|quel est le|affiche|donne)\s+(?:le |le modèle|modèle|model)\s+(?:actuel|utilisé|actif|en cours)\b", text):
        return {"action": "get_model"}


    # ---- Fournisseur actuel ----
    if re.search(r"\b(quel|quel est|quel est le|affiche|donne)\s+(?:le |le fournisseur|provider|fournisseur)\s+(?:actuel|utilisé|actif|en cours)\b", text):
        return {"action": "get_provider"}


    return None


def _detect_llm_intent_ai(description: str) -> Optional[Dict]:
    api_key = _get_api_key()
    if not api_key:
        return None
    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        prompt = (
            f"Analyse la phrase suivante et retourne UNIQUEMENT un objet JSON avec l'action pour le contrôle du LLM local.\n"
            f"Actions possibles : set_model (model), pull_model (model), list_models, check_status, restart, "
            f"set_provider (provider), get_model, get_provider.\n"
            f"Phrase : \"{description}\""
        )
        resp = client.models.generate_content(model=FAST_MODEL, contents=prompt)
        json_match = re.search(r'\{.*\}', resp.text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group(0))
    except Exception as e:
        print(f"[LocalLLM] Erreur IA : {e}")
    return None


# ── Implémentation des actions ────────────────────────────────────────────
def _list_models_local() -> str:
    provider = get_llm_provider()
    if provider != "ollama":
        return "La liste des modèles n'est disponible que pour Ollama. Pour les serveurs OpenAI-compatibles, consultez l'interface du serveur."
    url, _ = get_llm_settings()
    try:
        resp = _session.get(f"{url}/api/tags", timeout=(3, 5))
        resp.raise_for_status()
        models = resp.json().get("models", [])
        if not models:
            return "Aucun modèle trouvé dans Ollama."
        names = [m.get("name", "inconnu") for m in models]
        return "Modèles disponibles :\n" + "\n".join(f"  - {n}" for n in names)
    except Exception as e:
        return f"Impossible de contacter Ollama : {e}"


def _pull_model_ollama(model: str) -> str:
    if get_llm_provider() != "ollama":
        return "Le téléchargement de modèle n'est supporté que pour Ollama. Utilisez l'interface de votre serveur OpenAI-compatible."
    if not model:
        return "Aucun nom de modèle fourni."
    # On utilise la commande `ollama pull` pour éviter de bloquer avec une requête HTTP longue
    if not shutil.which("ollama"):
        return "La commande 'ollama' est introuvable. Veuillez installer Ollama depuis https://ollama.com"
    try:
        # On lance en arrière-plan et on renvoie un message immédiat
        subprocess.Popen(["ollama", "pull", model], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return f"Téléchargement du modèle '{model}' lancé en arrière-plan. Cela peut prendre quelques minutes."
    except Exception as e:
        return f"Échec du téléchargement : {e}"


def _set_model(model: str) -> str:
    if not model:
        return "Aucun nom de modèle spécifié."
    if _write_config_patch({"llm_model": model.strip()}):
        return f"Modèle changé pour '{model}'. Pris en compte immédiatement, aucun redémarrage requis."
    return "Impossible de sauvegarder la configuration."


def _set_provider(provider: str) -> str:
    provider = provider.strip().lower()
    # Alias historiques : "lmstudio"/"localai"/"jan"/"llamacpp" pointaient tous
    # vers le provider générique "openai" (serveur OpenAI-compatible local).
    provider = {"lmstudio": "openai", "localai": "openai", "jan": "openai", "llamacpp": "openai"}.get(provider, provider)
    if provider not in PROVIDERS:
        return f"Fournisseur inconnu. Choisissez parmi : {', '.join(sorted(PROVIDERS))}."
    info = PROVIDERS[provider]
    if info["needs_key"] and not get_api_key_for(provider):
        return f"Aucune clé API configurée pour {info['label']}. Ajoutez-la d'abord dans les réglages IA."
    if not set_active_provider(provider):
        return "Impossible de sauvegarder la configuration."
    if provider in ("gemini", "auto"):
        return f"Cerveau changé pour '{info['label']}'. Pris en compte immédiatement."
    return (
        f"Cerveau changé pour '{info['label']}' : il traite désormais tout, "
        "outils compris. La voix reste Gemini Live. Effectif à la prochaine "
        "reconnexion vocale."
    )


def _check_status() -> str:
    # En mode automatique, « auto » n'a ni URL ni famille : diagnostiquer ce
    # nom-là revenait à tester Ollama, qui n'a rien à voir avec le cerveau réel.
    provider = get_effective_llm_provider()
    info     = get_provider_info(provider)
    url, model = get_llm_settings()


    if info["family"] == "azure_openai":
        ok, msg = test_provider_key(provider, get_api_key_for(provider), model, url)
        return f"{info['label']} : {msg}" if ok else f"{info['label']} indisponible : {msg}"
    if info["family"] == "openai_compat":
        headers = {"Authorization": f"Bearer {get_api_key_for(provider)}"} if get_api_key_for(provider) else {}
        try:
            resp = _session.get(f"{_openai_v1_base(url)}/models", timeout=(3, 5), headers=headers)
            if resp.status_code in (200, 401):
                return f"{info['label']} accessible à {url} (modèle configuré : '{model}')."
            return f"{info['label']} à {url} a répondu avec le code {resp.status_code}."
        except Exception:
            return f"Impossible de se connecter à {info['label']} ({url})."


    if info["family"] in ("gemini", "anthropic"):
        if not get_api_key_for(provider):
            return f"Aucune clé API configurée pour {info['label']}."
        ok, msg = test_provider_key(provider, get_api_key_for(provider), model)
        return f"{info['label']} : {msg}"


    try:
        resp = _session.get(f"{url}/api/tags", timeout=(3, 5))
        if resp.status_code == 200:
            return f"Ollama est en cours d'exécution à {url}."
        else:
            return f"Ollama a répondu avec le code {resp.status_code}."
    except Exception:
        return f"Ollama ne répond pas à {url}. Voulez-vous le démarrer ? (commande 'redémarre Ollama')"


def _restart_ollama() -> str:
    if get_llm_provider() != "ollama":
        return "Le redémarrage n'est supporté que pour Ollama. Redémarrez manuellement votre serveur OpenAI-compatible."
    # Tuer les processus ollama existants (optionnel, sur Linux/macOS)
    if sys.platform != "win32":
        try:
            subprocess.run(["pkill", "-f", "ollama serve"], check=False, timeout=10)
        except Exception:
            pass
    success = ensure_ollama_running(timeout=20)
    return "Ollama redémarré avec succès." if success else "Échec du redémarrage d'Ollama."


# ── Point d'entrée principal ──────────────────────────────────────────────
def llm_control(
    parameters: dict = None,
    player=None,
    speak=None,
    session_memory=None,
) -> str:
    """
    Contrôle du LLM local en langage naturel.


    Paramètres acceptés :
        description : phrase naturelle
        action      : set_model, pull_model, list_models, check_status, restart, set_provider, get_model, get_provider
        model       : nom du modèle (pour set_model / pull_model)
        provider    : nom du fournisseur (pour set_provider)
    """
    params = parameters or {}
    description = params.get("description", "").strip()
    action = params.get("action", "").strip().lower()
    model  = params.get("model", "").strip()
    provider = params.get("provider", "").strip()


    # Interprétation naturelle
    if description and not action:
        local = _parse_llm_command_locally(description)
        if local:
            action = local.get("action", action)
            model  = local.get("model", model)
            provider = local.get("provider", provider)
        else:
            ai = _detect_llm_intent_ai(description)
            if ai:
                action = ai.get("action", action)
                model  = ai.get("model", model)
                provider = ai.get("provider", provider)
            else:
                return "Je n'ai pas compris votre demande concernant le LLM local. Dites par exemple 'change le modèle pour llama3.2' ou 'liste les modèles'."


    if player:
        player.write_log(f"[LLM] {action}")


    try:
        if action == "list_models":
            return _list_models_local()
        elif action == "pull_model":
            return _pull_model_ollama(model)
        elif action == "set_model":
            return _set_model(model)
        elif action == "set_provider":
            return _set_provider(provider)
        elif action == "check_status":
            return _check_status()
        elif action == "restart":
            return _restart_ollama()
        elif action == "get_model":
            _, current_model = get_llm_settings()
            return f"Le modèle actuel est '{current_model}'."
        elif action == "get_provider":
            return f"Le fournisseur actuel est '{get_llm_provider()}'."
        else:
            return f"Action inconnue : '{action}'. Utilisez set_model, pull_model, list_models, check_status, restart, etc."
    except Exception as e:
        return f"Erreur : {e}"
