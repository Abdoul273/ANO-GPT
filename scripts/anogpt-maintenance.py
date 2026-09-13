#!/usr/bin/env python3
"""Entretien des bases locales d'ANO-GPT.

À lancer quand l'assistant est **arrêté** : un VACUUM verrouille la base et
réécrit le fichier entier.

    python scripts/anogpt-maintenance.py               # état seul, n'écrit rien
    python scripts/anogpt-maintenance.py --compact     # compacte ce qui le mérite
    python scripts/anogpt-maintenance.py --compact --force
    python scripts/anogpt-maintenance.py --incremental # bascule pour l'avenir
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import storage_maintenance as maintenance


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compact", action="store_true",
                        help="lance un VACUUM sur les bases qui le méritent")
    parser.add_argument("--force", action="store_true",
                        help="compacte même quand le gain est faible")
    parser.add_argument("--incremental", action="store_true",
                        help="passe les bases en auto_vacuum incrémental (coût unique)")
    args = parser.parse_args()

    entries = maintenance.report()
    if not entries:
        print("Aucune base trouvée dans memory/.")
        return 1

    total = sum(entry.total_bytes for entry in entries)
    free = sum(entry.free_bytes for entry in entries)
    print(f"{'base':24s} {'taille':>11s} | {'pages libres':>22s} | verdict")
    print("─" * 78)
    for entry in entries:
        print(entry)
    print("─" * 78)
    print(f"{'TOTAL':24s} {total/1e6:8.1f} Mo | libre {free/1e6:7.1f} Mo "
          f"({free/total:5.1%})" if total else "")

    if not (args.compact or args.incremental):
        candidates = [entry for entry in entries if entry.should_compact]
        print("\nRien à faire." if not candidates else
              f"\n{len(candidates)} base(s) gagneraient à être compactées : "
              "relancer avec --compact.")
        return 0

    print("\nEntretien léger (statistiques, reprise WAL) :")
    for entry in entries:
        état = "ok" if maintenance.light_maintenance(entry.path) else "échec"
        print(f"  {entry.name:24s} {état}")

    if args.incremental:
        print("\nBascule en compactage incrémental :")
        for entry in entries:
            _, message = maintenance.enable_incremental_vacuum(entry.path)
            print(f"  {message}")

    if args.compact:
        print("\nCompactage :")
        for entry in entries:
            _, message = maintenance.compact(entry.path, force=args.force)
            print(f"  {message}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
