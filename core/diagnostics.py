"""Local, read-only diagnostics without importing audio, Qt or model SDKs."""
from __future__ import annotations

import importlib.util
import json
import platform
import shutil
import sys
from pathlib import Path


def collect(root: Path | None = None) -> dict:
    root = root or Path(__file__).resolve().parent.parent
    checks = []

    def add(name, status, detail):
        checks.append({"name": name, "status": status, "detail": detail})

    add("python", "ok" if sys.version_info >= (3, 11) else "error", platform.python_version())
    for module, required in (("PyQt6", True), ("sounddevice", True),
                             ("numpy", True), ("fastapi", True),
                             ("vosk", False), ("onnxruntime", False)):
        try:
            present = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            present = False
        add(module, "ok" if present else ("error" if required else "warning"),
            "Disponible" if present else "Module absent")
    path = root / "config" / "api_keys.json"
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError("object expected")
        key = config.get("gemini_api_key")
        add("configuration", "ok" if isinstance(key, str) and bool(key.strip()) else "warning",
            "JSON valide ; clé configurée" if isinstance(key, str) and key.strip()
            else "JSON valide ; clé Gemini absente")
    except FileNotFoundError:
        add("configuration", "warning", "Configuration absente")
    except (OSError, ValueError):
        add("configuration", "error", "Configuration illisible ou JSON invalide")
    try:
        free = shutil.disk_usage(root).free
        add("disk", "ok" if free >= 512 * 1024 * 1024 else "warning",
            f"{free // (1024 * 1024)} Mio disponibles")
    except OSError:
        add("disk", "warning", "Espace disque non accessible")
    from core.tool_stats import summary
    rows = summary(root / "memory" / "tool_usage.jsonl")
    status = "error" if any(c["status"] == "error" for c in checks) else (
        "warning" if any(c["status"] == "warning" for c in checks) else "ok")
    return {"status": status, "checks": checks, "tools": rows,
            "scope": "Diagnostic local ; services distants et périphériques non testés"}


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Diagnostic local ANO-GPT")
    parser.add_argument("--json", action="store_true", help="Rapport JSON sans secrets")
    args = parser.parse_args(argv)
    report = collect()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"ANO-GPT — diagnostic : {report['status']}")
        for check in report["checks"]:
            print(f"  [{check['status']}] {check['name']} : {check['detail']}")
        for row in report["tools"]:
            print(f"  {row['tool']} : {row['calls']} appels, {row['error_rate']:.0%} erreurs, "
                  f"p95 {row['p95_ms']:.0f} ms")
        print(report["scope"])
    return 1 if report["status"] == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
