#!/usr/bin/env python3
"""Inventorie les ``except …: pass`` qui masquent les erreurs.

Usage : ``python scripts/audit_silent_handlers.py [--limit 40]``.
Les tests ont leur propre garde-fou ; cet outil sert au tri quotidien en
classant les fichiers les plus endettés et en donnant les numéros de ligne.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_PREFIXES = ("tests/", "build/", "mobile/")


def is_silent(handler: ast.ExceptHandler) -> bool:
    body = [
        node for node in handler.body
        if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))
    ]
    return len(body) == 1 and isinstance(body[0], ast.Pass)


def findings() -> list[tuple[str, int, str]]:
    rows: list[tuple[str, int, str]] = []
    for path in sorted(ROOT.glob("**/*.py")):
        relative = path.relative_to(ROOT).as_posix()
        if relative.startswith(EXCLUDED_PREFIXES) or "__pycache__" in relative:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for handler in ast.walk(tree):
            if isinstance(handler, ast.ExceptHandler) and is_silent(handler):
                caught = ast.unparse(handler.type) if handler.type is not None else "bare"
                rows.append((relative, handler.lineno, caught))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=30, help="nombre de lignes détaillées")
    args = parser.parse_args()
    rows = findings()
    by_file = Counter(path for path, _, _ in rows)
    print(f"{len(rows)} handlers silencieux")
    print("\nFichiers prioritaires :")
    for path, count in by_file.most_common(max(0, args.limit)):
        print(f"  {count:>3}  {path}")
    print("\nEmplacements :")
    for path, line, caught in rows[:max(0, args.limit)]:
        print(f"  {path}:{line}  except {caught}: pass")
    return 1 if rows else 0


if __name__ == "__main__":
    raise SystemExit(main())
