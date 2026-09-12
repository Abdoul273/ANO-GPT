"""Journal d'usage des outils.

Le découpage « outils chauds / outils froids » ne peut pas se décider à
l'intuition : il faut savoir ce qui est réellement appelé, ce qui échoue et ce
qui coûte du temps. Chaque appel d'outil est donc écrit en JSONL ici, et
`résumé()` agrège le tout.

Contrainte absolue : ce module ne doit JAMAIS faire échouer un appel d'outil.
Tout est enveloppé — en cas de problème, on perd une ligne de statistique, pas
une commande de l'utilisateur.
"""

from __future__ import annotations

import json
import math
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

LOG_PATH = Path(__file__).resolve().parent.parent / "memory" / "tool_usage.jsonl"

_lock = threading.Lock()

# Au-delà de ce seuil, l'appel est compté comme « lent » : c'est ce qui déclenche
# déjà la carte « tâche en cours » dans l'UI (latence perçue > 1 s).
SLOW_MS = 1000.0
MAX_LOG_BYTES = 5 * 1024 * 1024
MAX_HISTORY = 20_000


def record(name: str, *, ok: bool, duration_ms: float, error: str = "") -> None:
    """Ajoute une ligne au journal. Silencieux en cas d'échec d'écriture."""
    try:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tool": name,
            "ok": ok,
            "ms": round(duration_ms, 1),
        }
        if error:
            # Les messages d'exception peuvent contenir URL, jetons ou contenu
            # personnel. Le diagnostic persistant garde seulement la catégorie.
            entry["error"] = "tool_execution_failed"
        line = json.dumps(entry, ensure_ascii=False)
        with _lock:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            if LOG_PATH.exists() and LOG_PATH.stat().st_size >= MAX_LOG_BYTES:
                LOG_PATH.replace(LOG_PATH.with_suffix(".jsonl.1"))
            with LOG_PATH.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception:
        pass


class timed:
    """Contexte qui chronomètre un appel d'outil et l'enregistre en sortant.

        with timed("open_app") as t:
            ...
            t.fail("boom")   # facultatif
    """

    def __init__(self, name: str):
        self.name = name
        self.ok = True
        self.error = ""
        self._t0 = 0.0

    def fail(self, error: str = "") -> None:
        self.ok = False
        self.error = str(error)

    def __enter__(self) -> "timed":
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None:
            self.ok = False
            self.error = f"{exc_type.__name__}: {exc}"
        record(
            self.name,
            ok=self.ok,
            duration_ms=(time.perf_counter() - self._t0) * 1000.0,
            error=self.error,
        )
        return False  # ne jamais avaler l'exception


def load(path: Path | None = None) -> list[dict]:
    """Relit le journal en ignorant les lignes corrompues."""
    p = path or LOG_PATH
    if not p.exists():
        return []
    from collections import deque
    out = deque(maxlen=MAX_HISTORY)
    try:
        with p.open(encoding="utf-8", errors="replace") as fh:
            # Ne lit qu'une fenêtre bornée, même pour un ancien journal géant.
            size = p.stat().st_size
            if size > MAX_LOG_BYTES:
                fh.seek(size - MAX_LOG_BYTES)
                fh.readline()
            for line in fh:
                try:
                    entry = json.loads(line)
                    if not isinstance(entry, dict) or not isinstance(entry.get("tool"), str):
                        continue
                    ms = float(entry.get("ms") or 0.0)
                    if not math.isfinite(ms) or ms < 0:
                        continue
                    entry["ms"] = ms
                    out.append(entry)
                except (TypeError, ValueError, OverflowError):
                    continue
    except OSError:
        return []
    return list(out)


def summary(path: Path | None = None) -> list[dict]:
    """Agrège par outil, du plus appelé au moins appelé."""
    stats: dict[str, dict] = defaultdict(
        lambda: {"calls": 0, "errors": 0, "slow": 0, "total_ms": 0.0, "latencies": []}
    )
    for e in load(path):
        tool = e.get("tool")
        if not tool:
            continue
        s = stats[tool]
        s["calls"] += 1
        ms = float(e.get("ms") or 0.0)
        s["total_ms"] += ms
        s["latencies"].append(ms)
        if not e.get("ok", True):
            s["errors"] += 1
        if ms >= SLOW_MS:
            s["slow"] += 1

    rows = []
    for tool, s in stats.items():
        calls = s["calls"]
        latencies = sorted(s["latencies"])
        rows.append(
            {
                "tool": tool,
                "calls": calls,
                "errors": s["errors"],
                "error_rate": s["errors"] / calls if calls else 0.0,
                "slow": s["slow"],
                "avg_ms": s["total_ms"] / calls if calls else 0.0,
                "p50_ms": latencies[max(0, math.ceil(calls * .50) - 1)],
                "p95_ms": latencies[max(0, math.ceil(calls * .95) - 1)],
                "max_ms": latencies[-1],
            }
        )
    rows.sort(key=lambda r: r["calls"], reverse=True)
    return rows


def declared_tools() -> list[str]:
    """Noms des outils réellement envoyés au modèle (déclarés moins retirés).

    Lu par analyse syntaxique de tool_dispatcher.py plutôt que par import : importer main
    démarre toute l'UI et l'audio, ce qui n'a pas sa place dans un rapport.
    """
    import ast

    main_py = Path(__file__).resolve().parent / "tool_dispatcher.py"
    try:
        tree = ast.parse(main_py.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []

    decls, retired = None, set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "TOOL_DECLARATIONS"
                and node.func.attr == "append" and node.args):
            try:
                declaration = ast.literal_eval(node.args[0])
                if decls is not None and isinstance(declaration, dict):
                    decls.append(declaration)
            except (ValueError, TypeError):
                pass
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            try:
                if target.id == "TOOL_DECLARATIONS" and decls is None:
                    decls = ast.literal_eval(node.value)
                elif target.id == "_RETIRED_TOOLS":
                    retired = set(ast.literal_eval(node.value))
            except (ValueError, TypeError):
                continue  # la ré-assignation filtrée n'est pas un littéral

    if not decls:
        return []
    return [d["name"] for d in decls if d.get("name") not in retired]


def print_report(path: Path | None = None) -> None:
    rows = summary(path)
    if not rows:
        print("Aucun appel d'outil enregistré pour l'instant.")
        print(f"(journal attendu : {path or LOG_PATH})")
        return

    total = sum(r["calls"] for r in rows)
    print(f"{total} appels enregistrés, {len(rows)} outils distincts\n")
    print(f"{'outil':<20}{'appels':>8}{'part':>8}{'err':>7}{'lents':>7}{'moy ms':>9}")
    print("-" * 59)
    for r in rows:
        print(
            f"{r['tool']:<20}{r['calls']:>8}{r['calls']/total:>7.0%}"
            f"{r['error_rate']:>7.0%}{r['slow']:>7}{r['avg_ms']:>9.0f}"
        )

    # Les outils jamais appelés sont les meilleurs candidats au retrait : ils
    # occupent le contexte et brouillent l'arbitrage sans rien rapporter.
    used = {r["tool"] for r in rows}
    declared = declared_tools()
    if declared:
        unused = [t for t in declared if t not in used]
        print()
        if unused:
            print(f"Jamais appelés ({len(unused)}/{len(declared)}) — candidats au retrait :")
            print("  " + ", ".join(unused))
        else:
            print(f"Les {len(declared)} outils déclarés ont tous été appelés au moins une fois.")


if __name__ == "__main__":
    print_report()
