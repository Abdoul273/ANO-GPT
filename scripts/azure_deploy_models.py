#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/azure_deploy_models.py — Déploie les modèles Azure d'ANO-GPT.

Le catalogue Azure (`azure_catalog()`) liste ce que la ressource *peut* servir.
Il ne dit pas ce qui est *déployé* — et sans déploiement, tout appel répond
``DeploymentNotFound``. Ce script comble l'écart : il crée les déploiements
manquants, puis vérifie un par un qu'ils répondent vraiment.

Rien n'est deviné. Le format de modèle, sa version et son SKU sont lus dans le
catalogue de la région via ``az cognitiveservices model list`` : ces valeurs
diffèrent d'un éditeur à l'autre (OpenAI, Anthropic, xAI, DeepSeek) et une
constante écrite en dur ici serait fausse la semaine suivante.

    python scripts/azure_deploy_models.py --liste
    python scripts/azure_deploy_models.py --preset flagship
    python scripts/azure_deploy_models.py --modeles claude-opus-5,grok-4.6
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.llm_client import probe_azure_deployment  # noqa: E402
from ui.paths import _read_full_config  # noqa: E402


# Le nécessaire pour qu'ANO-GPT tourne : un cerveau, un second avis, du code,
# une image, une vidéo. Chaque rôle du panneau « Configurer l'IA » a sa place.
PRESETS: dict[str, tuple[str, ...]] = {
    # Grok n'est pas retenu : dans les régions testées son quota est à zéro et
    # les variantes qui en ont ne figurent pas au catalogue régional. gpt-6-astra
    # tient le rôle de second cerveau, avec un quota nettement plus large.
    "flagship": (
        "claude-opus-5",      # raisonnement profond
        "gpt-6-astra",        # second cerveau, quota large
        "gpt-5.1",            # documents, usage courant
        "gpt-image-2",        # images
    ),
    "complet": (
        "claude-opus-5", "claude-sonnet-5", "claude-fable-5-1",
        "grok-4.6", "gpt-6-astra", "gpt-5.1", "DeepSeek-V4-Pro",
        "gpt-image-2", "sora-2",
    ),
    "econome": (
        "gpt-5.1",            # un seul déploiement, pour démarrer
    ),
}

DEFAULT_CAPACITY = 10  # milliers de jetons/minute — relevable dans le portail


def _run(args: list[str], *, timeout: int = 180) -> tuple[int, str, str]:
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return result.returncode, result.stdout, result.stderr


def _require_az() -> None:
    if shutil.which("az"):
        code, _, _ = _run(["az", "account", "show", "-o", "none"], timeout=60)
        if code == 0:
            return
        sys.exit(
            "Azure CLI est installé mais aucune session n'est ouverte.\n"
            "  az login"
        )
    sys.exit(
        "Azure CLI est nécessaire pour créer un déploiement (l'API par clé ne\n"
        "sait que consommer un déploiement, pas en créer un).\n\n"
        "  sudo pacman -S azure-cli      # ou : pipx install azure-cli\n"
        "  az login"
    )


def _resource_name(endpoint: str) -> str:
    """« https://xxx-resource.services.ai.azure.com/... » -> « xxx-resource »."""
    host = endpoint.split("//", 1)[-1].split("/", 1)[0]
    return host.split(".", 1)[0]


def _find_account(name: str) -> dict:
    code, out, err = _run(["az", "cognitiveservices", "account", "list", "-o", "json"])
    if code != 0:
        sys.exit(f"Impossible de lister les ressources Azure :\n{err.strip()}")
    for account in json.loads(out or "[]"):
        if account.get("name") == name:
            return account
    sys.exit(
        f"Aucune ressource « {name} » dans cet abonnement.\n"
        "Vérifie que `az login` porte sur le bon compte."
    )


def _region_catalog(location: str) -> dict[str, dict]:
    """Modèles déployables dans la région, avec leur format, version et SKU."""
    code, out, err = _run(
        ["az", "cognitiveservices", "model", "list", "-l", location, "-o", "json"],
        timeout=300,
    )
    if code != 0:
        sys.exit(f"Catalogue de la région {location} illisible :\n{err.strip()}")
    catalog: dict[str, dict] = {}
    for item in json.loads(out or "[]"):
        model = item.get("model") or {}
        name = model.get("name")
        if not name:
            continue
        # Un modèle déprécié figure encore au catalogue mais refuse tout nouveau
        # déploiement : le proposer ne menait qu'à un échec en fin de course.
        if str(model.get("lifecycleStatus", "")).lower() == "deprecated":
            continue
        # Plusieurs entrées par modèle (une par version) : on garde la plus récente.
        previous = catalog.get(name)
        if previous and str(model.get("version", "")) <= str(previous.get("version", "")):
            continue
        catalog[name] = {
            "format": model.get("format", "OpenAI"),
            "version": model.get("version", ""),
            "skus": [sku.get("name") for sku in (model.get("skus") or []) if sku.get("name")],
        }
    return catalog


def _region_quota(location: str) -> dict[str, int]:
    """Capacité encore disponible par modèle, dans la région.

    Demander 10 unités quand le quota en autorise 2 fait échouer la création
    entière. Azure nomme ces compteurs ``<éditeur>.<sku>.<modèle>`` — c'est la
    seule source qui dise ce qu'on a le droit de déployer.
    """
    code, out, _ = _run(
        ["az", "cognitiveservices", "usage", "list", "-l", location, "-o", "json"],
        timeout=180,
    )
    if code != 0:
        return {}
    free: dict[str, int] = {}
    for item in json.loads(out or "[]"):
        parts = str((item.get("name") or {}).get("value", "")).split(".", 2)
        if len(parts) != 3:
            continue
        _publisher, sku, model = parts
        if sku != "GlobalStandard":
            continue
        available = int(item.get("limit", 0)) - int(item.get("currentValue", 0))
        free[model] = max(free.get(model, 0), available)
    return free


def _deployment_states(group: str, account: str) -> dict[str, str]:
    """État réel de chaque déploiement, tel qu'Azure le connaît.

    C'est la seule vérification qui vaille pour un modèle d'image ou de vidéo :
    il n'écoute pas sur ``/chat/completions``, et une sonde conversationnelle le
    déclarait « inexistant » alors qu'il était en service.
    """
    code, out, _ = _run([
        "az", "cognitiveservices", "account", "deployment", "list",
        "-g", group, "-n", account, "-o", "json",
    ])
    if code != 0:
        return {}
    states = {}
    for entry in json.loads(out or "[]"):
        name = entry.get("name")
        if name:
            states[name] = (entry.get("properties") or {}).get("provisioningState", "?")
    return states


def _pick_sku(skus: list[str]) -> str:
    """Préfère un SKU facturé à l'usage : un débit réservé coûte à l'arrêt."""
    for wanted in ("GlobalStandard", "DataZoneStandard", "Standard"):
        if wanted in skus:
            return wanted
    return skus[0] if skus else "GlobalStandard"


# Anthropic exige une déclaration du déployeur. Le champ n'existe pas dans
# `az cognitiveservices` (2.87) : il faut passer par l'API ARM brute, et par une
# version d'API assez récente — les anciennes ignorent le champ en silence puis
# reprochent son absence.
_PROVIDER_DATA_FORMATS = {"Anthropic"}
_ARM_API_VERSION = "2026-07-01"


def _deploy_with_provider_data(subscription: str, group: str, account: str,
                               name: str, spec: dict, capacity: int,
                               provider_data: dict) -> bool:
    body = {
        "sku": {"name": _pick_sku(spec["skus"]), "capacity": capacity},
        "properties": {
            "model": {"format": spec["format"], "name": name, "version": spec["version"]},
            "modelProviderData": provider_data,
        },
    }
    url = (
        f"https://management.azure.com/subscriptions/{subscription}"
        f"/resourceGroups/{group}/providers/Microsoft.CognitiveServices"
        f"/accounts/{account}/deployments/{name}?api-version={_ARM_API_VERSION}"
    )
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(body, handle)
        path = handle.name
    code, _, err = _run(
        ["az", "rest", "--method", "put", "--url", url, "--body", f"@{path}", "-o", "none"],
        timeout=600,
    )
    Path(path).unlink(missing_ok=True)
    if code == 0:
        return True
    detail = err.strip()
    if "Marketplace" in detail and "free subscription" in detail:
        print("    ✗ refusé : les modèles Anthropic passent par la Place de marché "
              "Azure, fermée aux abonnements gratuits (Azure for Students). "
              "Il faut un abonnement avec moyen de paiement.")
    else:
        print(f"    ✗ refusé : {detail.splitlines()[-1] if detail else code}")
    return False


def _deploy(group: str, account: str, name: str, spec: dict, capacity: int,
            subscription: str = "", provider_data: dict | None = None) -> bool:
    sku = _pick_sku(spec["skus"])
    print(f"  → création : {name} ({spec['format']} {spec['version']}, {sku})")
    if spec["format"] in _PROVIDER_DATA_FORMATS:
        if not provider_data:
            print("    ✗ refusé : ce modèle réclame --secteur, --organisation et --pays.")
            return False
        return _deploy_with_provider_data(
            subscription, group, account, name, spec, capacity, provider_data)
    code, _, err = _run([
        "az", "cognitiveservices", "account", "deployment", "create",
        "-g", group, "-n", account,
        "--deployment-name", name,
        "--model-name", name,
        "--model-version", spec["version"],
        "--model-format", spec["format"],
        "--sku-name", sku,
        "--sku-capacity", str(capacity),
        "-o", "none",
    ], timeout=600)
    if code != 0:
        print(f"    ✗ refusé : {err.strip().splitlines()[-1] if err.strip() else code}")
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preset", choices=sorted(PRESETS), default="flagship")
    parser.add_argument("--modeles", default="", help="liste séparée par des virgules")
    parser.add_argument("--capacite", type=int, default=DEFAULT_CAPACITY)
    parser.add_argument("--liste", action="store_true",
                        help="montre l'état sans rien créer")
    parser.add_argument("--secteur", default="Technology",
                        help="secteur déclaré aux éditeurs qui l'exigent (Anthropic)")
    parser.add_argument("--organisation", default="", help="nom déclaré à l'éditeur")
    parser.add_argument("--pays", default="", help="code pays ISO à deux lettres")
    args = parser.parse_args()

    cfg = _read_full_config()
    endpoint = cfg.get("azure_openai_endpoint", "")
    api_key = cfg.get("azure_openai_api_key", "")
    if not (endpoint and api_key):
        sys.exit("Endpoint ou clé Azure absents de config/api_keys.json.")

    _require_az()
    name = _resource_name(endpoint)
    account = _find_account(name)
    group, location = account["resourceGroup"], account["location"]
    print(f"Ressource : {name}  ·  groupe {group}  ·  région {location}\n")

    wanted = [m.strip() for m in args.modeles.split(",") if m.strip()] or list(PRESETS[args.preset])
    catalog = _region_catalog(location)
    quota = _region_quota(location)
    deployed = set(_deployment_states(group, account["name"]))

    print(f"Déploiements existants : {sorted(deployed) or 'aucun'}\n")
    for model in wanted:
        spec = catalog.get(model)
        if spec is None:
            print(f"  ✗ {model} : absent du catalogue de {location}.")
            continue
        if model in deployed:
            print(f"  = {model} : déjà déployé.")
            continue
        available = quota.get(model)
        if available is not None and available <= 0:
            print(f"  ✗ {model} : quota nul dans {location}. "
                  "Demande une augmentation dans le portail (Quotas → Cognitive Services).")
            continue
        # On ne demande jamais plus que ce que le quota autorise : un dépassement
        # ne rogne pas la demande, il annule la création.
        capacity = min(args.capacite, available) if available else args.capacite
        if args.liste:
            print(f"  + {model} : à créer ({spec['format']} {spec['version']}, "
                  f"{_pick_sku(spec['skus'])}, capacité {capacity})")
            continue
        provider_data = None
        if args.organisation and args.pays:
            provider_data = {
                "industry": args.secteur,
                "organizationName": args.organisation,
                "countryCode": args.pays.upper(),
            }
        _deploy(group, account["name"], model, spec, capacity,
                subscription=account["id"].split("/")[2], provider_data=provider_data)

    if args.liste:
        return 0

    print("\nVérification :")
    states = _deployment_states(group, account["name"])
    for model in wanted:
        state = states.get(model)
        if state != "Succeeded":
            print(f"  ✗ {model} — non déployé{f' (état : {state})' if state else ''}.")
            continue
        # Le déploiement existe. On ne tente l'appel réel que sur les modèles
        # conversationnels : les autres répondent légitimement « unsupported ».
        chat_capable = (catalog.get(model, {}).get("format") in {"OpenAI", "DeepSeek", "Meta"}
                        and not any(hint in model.lower()
                                    for hint in ("image", "sora", "dall-e", "flux")))
        if not chat_capable:
            print(f"  ✓ {model} — déployé (modèle non conversationnel).")
            continue
        ok, message = probe_azure_deployment(endpoint, api_key, model)
        print(f"  {'✓' if ok else '✗'} {model} — {message}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
