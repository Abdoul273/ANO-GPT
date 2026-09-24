"""Azure Foundry models reserved for heavyweight specialist work.

Gemini Live remains the conversational runtime.  This module is intentionally
opt-in: code and document jobs use their dedicated Azure model only when one
is configured, and image or video generation never falls back to an unrelated
chat model.
"""
from __future__ import annotations

import base64
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout, as_completed
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


# Déploiements capables de lire une image, du plus fort au plus économe. Seuls
# ceux réellement déployés sur la ressource sont appelés ; « anogpt-brain » est
# le déploiement principal (gpt-5.6-terra).
# Vitesse d'abord : les modèles « raisonnement profond » (gpt-6-astra) mettent
# 30 s sur une image, l'utilisateur attend devant la caméra. Ils ne servent
# qu'en dernier recours.
_VISION_CASCADE = ("anogpt-brain", "gpt-5-mini", "gpt-5.1", "gpt-5.6-terra", "gpt-6-astra")
_SLOW_FIRST = ("gpt-6",)
_VISION_PARALLEL = 2
_VISION_SKIP = ("codex", "image", "sora", "flux", "embedding", "whisper", "tts", "realtime")


def vision_models() -> list[str]:
    """Cascade vision : choix explicite, modèle profond, puis les déploiements connus."""
    cfg = _load_config()
    ordered: list[str] = []
    for name in (
        str(cfg.get("azure_vision_model") or "").strip(),
        *_VISION_CASCADE,
        str(cfg.get("azure_openai_model") or "").strip(),
        str(cfg.get("azure_deep_model") or "").strip(),
    ):
        if name and name not in ordered and not any(k in name.lower() for k in _VISION_SKIP):
            ordered.append(name)
    try:
        from core.llm_client import azure_deployments
        deployed = set(azure_deployments())
    except Exception:
        deployed = set()
    if deployed:
        # Un nom jamais déployé ne mérite pas un aller-retour DeploymentNotFound.
        ordered = [m for m in ordered if m in deployed]
    return ordered


def vision(image_bytes: bytes, mime: str, prompt: str, *, system: str = "",
           json_mode: bool = True, timeout: int = 60) -> tuple[str, str]:
    """Décrit une image avec le premier déploiement Azure multimodal qui répond.

    Renvoie ``(texte, modèle)``. Sert de relais quand le quota Gemini est
    épuisé : la vision ne doit jamais s'arrêter sur un compteur.
    """
    cfg = _load_config()
    endpoint = str(cfg.get("azure_openai_endpoint") or "").strip()
    key = str(cfg.get("azure_openai_api_key") or "").strip()
    if not endpoint or not key:
        raise RuntimeError("La clé ou l'endpoint Azure Foundry manque.")
    models = vision_models()
    if not models:
        raise RuntimeError("Aucun déploiement Azure multimodal n'est disponible.")
    data_url = f"data:{mime or 'image/jpeg'};base64,{base64.b64encode(image_bytes).decode('ascii')}"
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": data_url, "detail": "high"}},
    ]})
    deadline = time.monotonic() + max(1, int(timeout))

    def _ask(model: str) -> str:
        """Un déploiement, tout le budget restant. Rend « » s'il n'a rien lu."""
        remaining = max(1, int(deadline - time.monotonic()))
        field = "max_tokens" if model.lower().startswith("claude-") else "max_completion_tokens"
        payload = {"model": model, "messages": messages, field: 1500, "stream": False}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if model.lower().startswith(("gpt-5", "gpt-6", "anogpt", "o")):
            # Décrire une image ne demande pas de longue réflexion : sans ce
            # réglage, un modèle « reasoning » y passe 20 à 30 s.
            payload["reasoning_effort"] = "low"
        response = _post_with_retry(
            _azure_openai_endpoint(endpoint, model), payload, remaining,
            headers={"api-key": key}, retries=0,
        )
        if response.status_code >= 400:
            body = response.text[:200]
            if response.status_code == 400 and ("response_format" in body.lower() or "reasoning" in body.lower()):
                # Ce déploiement ignore le mode JSON ou l'effort de raisonnement :
                # on redemande sans ces réglages.
                payload.pop("response_format", None)
                payload.pop("reasoning_effort", None)
                response = _post_with_retry(
                    _azure_openai_endpoint(endpoint, model), payload,
                    max(1, int(deadline - time.monotonic())),
                    headers={"api-key": key}, retries=0,
                )
                if response.status_code >= 400:
                    raise RuntimeError(f"HTTP {response.status_code} {response.text[:200]}")
            else:
                raise RuntimeError(f"HTTP {response.status_code} {body}")
        try:
            return str(response.json().get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
        except Exception:
            return ""

    # La latence Azure varie du simple au triple d'un appel à l'autre : les
    # deux premiers déploiements partent ensemble et le premier qui répond
    # gagne. Un fil qui attend le réseau ne coûte rien au GIL.
    racers = models[:_VISION_PARALLEL]
    errors: list[str] = []
    pool = ThreadPoolExecutor(max_workers=len(racers))
    try:
        futures = {pool.submit(_ask, model): model for model in racers}
        for future in as_completed(futures, timeout=max(0.1, deadline - time.monotonic())):
            model = futures[future]
            try:
                answer = future.result()
            except Exception as exc:
                errors.append(f"{model} : {exc}")
                continue
            if answer:
                return answer, model
            errors.append(f"{model} : réponse vide")
    except FuturesTimeout:
        errors.append(f"délai {timeout} s dépassé")
    finally:
        # Le context manager attend tous les appels, même quand le gagnant a
        # répondu ou que le délai est dépassé. Une requête HTTP en cours ne
        # peut pas être annulée ; son timeout réseau la terminera en fond.
        pool.shutdown(wait=False, cancel_futures=True)
    raise RuntimeError(f"Aucun modèle Azure n'a lu l'image ({' ; '.join(errors)}).")


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
