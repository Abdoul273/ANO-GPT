"""Azure Foundry models reserved for heavyweight specialist work.

Gemini Live remains the conversational runtime.  This module is intentionally
opt-in: code and document jobs use their dedicated Azure model only when one
is configured, and image or video generation never falls back to an unrelated
chat model.
"""
from __future__ import annotations

import base64
import time
from pathlib import Path

import requests

from core.llm_client import (
    _azure_openai_endpoint, _load_config, _post_with_retry, _session,
)

# Rôle interne → rôle du catalogue Foundry (`azure_catalog()`).
_CATALOG_ROLE = {"image": "image", "video": "video"}


def _catalog_default(role: str) -> str:
    """Premier modèle du catalogue pour ce rôle, quand rien n'est choisi.

    Sans ça, une ressource qui déploie bien Sora ou GPT-Image reste inutilisable
    tant que l'utilisateur n'a pas ouvert les réglages.
    """
    try:
        from core.llm_client import azure_catalog
        return str((azure_catalog().get(_CATALOG_ROLE.get(role, "chat")) or [""])[0] or "").strip()
    except Exception:
        return ""


def _settings(role: str) -> tuple[dict, str, str, str]:
    cfg = _load_config()
    model = str(cfg.get(f"azure_{role}_model") or "").strip()
    endpoint = str(cfg.get("azure_openai_endpoint") or "").strip()
    key = str(cfg.get("azure_openai_api_key") or "").strip()
    if not endpoint or not key:
        raise RuntimeError("La clé ou l'endpoint Azure Foundry manque.")
    if not model and role in _CATALOG_ROLE:
        model = _catalog_default(role)
    if not model:
        raise RuntimeError(f"Aucun modèle Azure n'est sélectionné pour « {role} ».")
    return cfg, model, endpoint, key


def _v1_root(endpoint: str, what: str) -> str:
    """Racine ``/openai/v1`` exigée par les API images et vidéo de Foundry."""
    root = endpoint.rstrip("/")
    if root.endswith("/openai/v1"):
        return root
    if root.endswith("/openai"):
        return root + "/v1"
    if ".services.ai.azure.com" in root or ".openai.azure.com" in root:
        return root + "/openai/v1"
    raise RuntimeError(
        f"La génération {what} nécessite un endpoint Azure Foundry se terminant par /openai/v1."
    )


def _deployment_error(response, model: str, what: str) -> None:
    if response.status_code == 404 and "DeploymentNotFound" in response.text:
        raise RuntimeError(
            f"Le modèle {what} Azure « {model} » n'est pas déployé. "
            f"Créez-le avec : python scripts/azure_deploy_models.py --modeles {model}"
        )


def _raise_for_status(response, what: str) -> None:
    if response.status_code >= 400:
        detail = ""
        try:
            payload = response.json()
            detail = str((payload.get("error") or {}).get("message") or "")
        except Exception:
            detail = response.text[:200]
        raise RuntimeError(f"Azure a refusé la {what} (HTTP {response.status_code}) : {detail[:300]}")


def text(role: str, prompt: str, *, system: str = "", timeout: int = 90) -> str:
    """Call a configured Azure text specialist and return only its answer."""
    _cfg, model, endpoint, key = _settings(role)
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    field = "max_tokens" if model.lower().startswith("claude-") else "max_completion_tokens"
    payload = {"model": model, "messages": messages, field: 1800, "stream": False}
    response = _post_with_retry(
        _azure_openai_endpoint(endpoint, model), payload, timeout,
        headers={"api-key": key}, retries=2,
    )
    if response.status_code == 404 and "DeploymentNotFound" in response.text:
        raise RuntimeError(
            f"Le modèle Azure « {model} » n'est pas déployé. Créez-le avec : "
            f"python scripts/azure_deploy_models.py --modeles {model}"
        )
    if response.status_code == 400 and "unsupported" in response.text.lower():
        # Les déploiements « codex » ne répondent que sur l'API Responses : ils
        # sont bien déployés, mais inutilisables ici. Le sondage de déploiement
        # les déclare pourtant valides, d'où ce message explicite.
        raise RuntimeError(
            f"Le déploiement Azure « {model} » n'accepte pas les requêtes de "
            "conversation (chat/completions). Choisissez un autre modèle pour ce rôle."
        )
    response.raise_for_status()
    answer = str(response.json().get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
    if not answer:
        raise RuntimeError(f"Le modèle Azure « {model} » a répondu sans texte.")
    return answer


def image(prompt: str, output_path: Path, *, size: str = "1024x1024", timeout: int = 180) -> bytes:
    """Generate one image with the configured Azure image deployment."""
    _cfg, model, endpoint, key = _settings("image")
    root = _v1_root(endpoint, "d'image")
    payload = {"model": model, "prompt": prompt, "n": 1, "size": size}
    # GPT-Image et FLUX renvoient toujours du base64 et rejettent le paramètre
    # hérité ``response_format`` ; seul DALL-E a besoin qu'on le demande.
    if model.lower().startswith("dall-e"):
        payload["response_format"] = "b64_json"
    response = _post_with_retry(
        f"{root}/images/generations", payload, timeout,
        headers={"api-key": key}, retries=2,
    )
    _deployment_error(response, model, "d'image")
    _raise_for_status(response, "génération d'image")
    item = (response.json().get("data") or [{}])[0]
    encoded = item.get("b64_json") or ""
    if encoded:
        data = base64.b64decode(encoded)
    elif item.get("url"):
        # Certains déploiements (DALL-E 3) ne rendent qu'une URL temporaire.
        fetched = _session.get(str(item["url"]), timeout=(5, timeout))
        fetched.raise_for_status()
        data = fetched.content
    else:
        raise RuntimeError("Azure n'a pas retourné d'image.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(data)
    return data


def video(prompt: str, output_path: Path, *, seconds: int = 8,
          size: str = "720x1280", timeout: int = 900,
          progress=None) -> bytes:
    """Génère une vidéo avec le déploiement Sora et l'écrit sur le disque.

    Foundry expose l'API Videos compatible OpenAI (``/openai/v1/videos``) :
    on crée la vidéo, on interroge son statut jusqu'à ``completed``, puis on
    télécharge le MP4 via ``/videos/{id}/content``.
    """
    _cfg, model, endpoint, key = _settings("video")
    root = _v1_root(endpoint, "vidéo")
    headers = {"api-key": key}
    response = _post_with_retry(
        f"{root}/videos",
        {
            "model": model, "prompt": prompt,
            "size": str(size), "seconds": str(max(1, min(int(seconds or 8), 20))),
        },
        90, headers=headers, retries=2,
    )
    _deployment_error(response, model, "vidéo")
    _raise_for_status(response, "génération vidéo")
    job = response.json()
    job_id = str(job.get("id") or "")
    if not job_id:
        raise RuntimeError("Azure n'a pas ouvert de tâche vidéo.")

    deadline = time.monotonic() + max(60, int(timeout))
    status = str(job.get("status") or "queued")
    while status not in {"completed", "succeeded", "failed", "cancelled"}:
        if time.monotonic() > deadline:
            raise RuntimeError("La génération vidéo Azure dépasse le délai autorisé.")
        time.sleep(5)
        try:
            polled = _session.get(f"{root}/videos/{job_id}", headers=headers, timeout=(5, 30))
        except requests.exceptions.RequestException:
            continue
        if polled.status_code >= 400:
            _raise_for_status(polled, "génération vidéo")
        job = polled.json()
        status = str(job.get("status") or status)
        if progress is not None:
            try:
                progress(f"{status} {job.get('progress') or 0}%")
            except Exception:
                pass
    if status not in {"completed", "succeeded"}:
        error = job.get("error") or {}
        reason = str(error.get("message") if isinstance(error, dict) else error).strip()
        raise RuntimeError(f"Azure a interrompu la vidéo ({status}) {reason}".strip())

    fetched = _session.get(f"{root}/videos/{job_id}/content",
                           headers=headers, timeout=(5, 300))
    _raise_for_status(fetched, "récupération vidéo")
    data = fetched.content
    if not data:
        raise RuntimeError("Le fichier vidéo renvoyé par Azure est vide.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(data)
    return data
