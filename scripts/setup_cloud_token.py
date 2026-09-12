#!/usr/bin/env python3
"""Enregistre un jeton personnel Notion ou Figma dans le trousseau système."""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.cloud_integrations import CloudIntegrationError, get_cloud_integrations


def main() -> int:
    parser = argparse.ArgumentParser(description="Configure un jeton personnel ANO-GPT de manière non affichée.")
    parser.add_argument("service", choices=("notion", "figma"))
    parser.add_argument("--parent-only", action="store_true", help="Notion : règle seulement la page par défaut, sans recréer le jeton.")
    args = parser.parse_args()
    if args.parent_only:
        if args.service != "notion":
            parser.error("--parent-only est réservé à Notion")
        parent = input("Collez l'URL de la page Notion qui recevra vos notes : ").strip()
        try:
            get_cloud_integrations().set_notion_parent(parent)
        except CloudIntegrationError as exc:
            print(f"Erreur : {exc}", file=sys.stderr)
            return 1
        print("Page Notion par défaut enregistrée : les prochaines notes vocales iront ici.")
        return 0
    token = getpass.getpass(f"Collez le jeton personnel {args.service.title()} (il ne sera pas affiché) : ")
    try:
        get_cloud_integrations().set_token(args.service, token)
    except CloudIntegrationError as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 1
    if args.service == "notion":
        print("\nDans Notion, créez une page « Notes ANO-GPT » puis partagez-la avec votre connexion Notion.")
        parent = input("Collez l'URL de cette page (ou son identifiant) : ").strip()
        try:
            get_cloud_integrations().set_notion_parent(parent)
        except CloudIntegrationError as exc:
            print(f"Erreur : {exc}", file=sys.stderr)
            return 1
        print("Notion est relié : les prochaines notes vocales iront dans cette page.")
        return 0
    print("Figma est relié à ANO-GPT. Le jeton est dans le trousseau système.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
