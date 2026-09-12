"""Tests pour l'auto-réparation et le diagnostic des outils (core/self_healing.py)."""

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
import pytest

from core.self_healing import (
    ToolDiagnostic,
    analyze_tool_health,
    generate_system_health_report,
    prepare_healing_task,
    _resolve_tool_source_file,
)


@pytest.fixture
def fake_tool_log(tmp_path):
    log_file = tmp_path / "tool_usage_test.jsonl"
    now = datetime.now(timezone.utc)

    # 4 appels météo dont 3 erreurs
    entries = [
        {"ts": (now - timedelta(hours=1)).isoformat(), "tool": "weather", "ok": True, "ms": 120.0},
        {"ts": (now - timedelta(minutes=45)).isoformat(), "tool": "weather", "ok": False, "ms": 3000.0, "error": "HTTP 429: Too Many Requests"},
        {"ts": (now - timedelta(minutes=30)).isoformat(), "tool": "weather", "ok": False, "ms": 3000.0, "error": "HTTP 429: Too Many Requests"},
        {"ts": (now - timedelta(minutes=10)).isoformat(), "tool": "weather", "ok": False, "ms": 3000.0, "error": "HTTP 429: Too Many Requests"},
        # 3 appels email tous ok
        {"ts": (now - timedelta(hours=2)).isoformat(), "tool": "email", "ok": True, "ms": 150.0},
        {"ts": (now - timedelta(hours=1)).isoformat(), "tool": "email", "ok": True, "ms": 140.0},
        {"ts": (now - timedelta(minutes=5)).isoformat(), "tool": "email", "ok": True, "ms": 160.0},
    ]

    with log_file.open("w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")

    return log_file


def test_analyze_tool_health_detects_failing_tool(fake_tool_log):
    diags = analyze_tool_health(log_path=fake_tool_log, min_calls=3, error_rate_threshold=0.4)
    assert len(diags) == 1
    d = diags[0]
    assert d.tool == "weather"
    assert d.total_calls == 4
    assert d.failed_calls == 3
    assert d.error_rate == 0.75
    assert d.status == "failing"
    assert len(d.recent_errors) == 3
    assert "429" in d.recent_errors[0]


def test_health_report_generation(fake_tool_log):
    report = generate_system_health_report(log_path=fake_tool_log)
    assert "⚠️ 1 outil(s) nécessitant une attention" in report
    assert "weather" in report
    assert "taux d'erreur 75%" in report


def test_prepare_healing_task():
    diag = ToolDiagnostic(
        tool="weather",
        total_calls=5,
        failed_calls=4,
        error_rate=0.8,
        recent_errors=["Timeout connecting to OpenMeteo"],
        target_file="/fake/actions/weather_report.py",
        status="failing",
    )
    task = prepare_healing_task(diag)
    assert task["project_name"] == "fix_weather"
    assert "weather_report.py" in task["description"]
    assert "Timeout" in task["description"]
    assert task["tool"] == "weather"


def test_resolve_tool_source_file():
    # Vérifie que les fichiers standards du projet sont bien résolus
    file_path = _resolve_tool_source_file("email")
    assert file_path is not None
    assert file_path.name in {"email.py", "email_service.py"}
