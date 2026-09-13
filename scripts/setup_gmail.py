#!/usr/bin/env python3
"""Installe une clé OAuth Google Desktop puis ouvre l'autorisation Gmail.

Sans argument, le script cherche lui-même la clé dans les téléchargements :
c'est là qu'elle atterrit en sortant de Google Cloud, et retrouver ce chemin à
la main était la marche la plus haute de toute l'installation.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.email_service import (
    SETUP_STEPS,
    GmailError,
    GmailService,
    find_downloaded_client_secret,
)


def _explain_and_fail() -> int:
    print("Aucune clé OAuth trouvée dans vos téléchargements.\n")
    print("Marche à suivre, une fois pour toutes :\n")
    for step in SETUP_STEPS:
        print(f"  {step}")
    print("\nSi le fichier est ailleurs, indiquez son chemin :")
    print("  python scripts/setup_gmail.py /chemin/vers/client_secret.json")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Relier un compte Gmail à ANO-GPT avec OAuth 2.0."
    )
    parser.add_argument(
        "client_secret",
        nargs="?",
        help="Chemin du JSON OAuth 'Application de bureau'. Omis, le script "
             "le cherche dans vos téléchargements.",
    )
    args = parser.parse_args()

    source = Path(args.client_secret) if args.client_secret else None
    if source is None:
        source = find_downloaded_client_secret()
        if source is None:
            return _explain_and_fail()
        print(f"Clé OAuth repérée : {source}")

    service = GmailService()
    try:
        installed = service.configure_client_secret(source)
        print(f"Clé OAuth installée : {installed}")
        print("Ouverture de Google dans le navigateur…")
        profile = service.connect(interactive=True)
        account = profile.get("emailAddress", "compte autorisé")
        print(f"Gmail connecté : {account}")
        print("ANO-GPT lit désormais vos e-mails en direct : "
              "il annoncera les nouveaux messages dès leur arrivée.")
        return 0
    except GmailError as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
