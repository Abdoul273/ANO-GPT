#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/benchmark_screen_consciousness.py — Mesure CPU de la veille visuelle."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.screen_consciousness import _print_benchmark, benchmark_cpu


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark CPU de core/screen_consciousness.py")
    parser.add_argument("--rounds", type=int, default=40)
    parser.add_argument(
        "--live",
        action="store_true",
        help="Inclut un tick grim/hyprctl réel (sinon synthétique uniquement)",
    )
    args = parser.parse_args()
    _print_benchmark(benchmark_cpu(rounds=args.rounds, include_live=args.live))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
