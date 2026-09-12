"""Destination GitHub du futur/éventuel sentinelle de sauvegarde projet.

Les projets doivent être explicitement inscrits : aucun dossier personnel n'est
jamais découvert ou publié automatiquement.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path


def _config_path() -> Path:
    return Path(__file__).resolve().parent.parent / "config" / "api_keys.json"


def tracked_projects() -> list[str]:
    try:
        raw = json.loads(_config_path().read_text(encoding="utf-8"))
        projects = raw.get("github_backup_projects", []) if isinstance(raw, dict) else []
        return [str(item) for item in projects if Path(str(item)).is_dir()]
    except Exception:
        return []


def run_periodic_pushes() -> list[str]:
    """Pousse seulement les projets explicitement suivis, via le même garde-fou."""
    from actions.github import github_control
    reports = []
    for path in tracked_projects():
        try:
            status = subprocess.run(
                ["git", "status", "--porcelain"], cwd=path, capture_output=True,
                text=True, timeout=15,
            )
            if status.returncode != 0 or not status.stdout.strip():
                continue
        except OSError:
            continue
        reports.append(github_control({"action": "commit_push", "project": path, "private": True}))
    return reports
