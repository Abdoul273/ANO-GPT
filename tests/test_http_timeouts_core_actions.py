"""Aucun appel HTTP direct ne peut attendre sans borne dans le chemin vocal."""

import ast
from pathlib import Path


def test_direct_http_calls_have_explicit_timeout():
    root = Path(__file__).resolve().parents[1]
    missing = []
    for folder in (root / "core", root / "actions"):
        for path in folder.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
                name = ast.unparse(call.func)
                direct = (
                    name in {"urllib.request.urlopen", "urlopen"}
                    or (name.startswith(("requests.", "_session."))
                        and name.split(".")[-1] in {"get", "post", "put", "delete", "patch", "request"})
                )
                if direct and not any(keyword.arg == "timeout" for keyword in call.keywords):
                    missing.append(f"{path.relative_to(root)}:{call.lineno}")
    assert not missing, "HTTP sans limite de temps : " + ", ".join(missing)
