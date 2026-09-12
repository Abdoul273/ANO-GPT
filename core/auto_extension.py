"""Boucle d'auto-extension : propositions isolées, jamais du code cœur.

Les agents ne travaillent que dans ``.auto_extensions/staging``. La seule
écriture dans ``plugins/`` arrive après confirmation HUD et validation.
"""
from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from core import human_confirmation

_ROOT = Path(__file__).resolve().parent.parent
_STORE = _ROOT / "memory" / "auto_extensions.json"
_STAGING = _ROOT / ".auto_extensions" / "staging"
_FORBIDDEN_IMPORTS = {"main", "core.audio_engine", "core.session_manager", "core.tool_dispatcher", "core.action_runtime"}


@dataclass
class Need:
    id: str
    fingerprint: str
    samples: list[str]
    created_at: str
    updated_at: str
    status: str = "observed"  # observed | building | proposed | active | rejected | failed
    extension_name: str = ""
    specification: str = ""
    report: str = ""
    preview: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fold(text: str) -> str:
    return " ".join(re.findall(r"[a-zà-ÿ0-9]{3,}", str(text).casefold()))[:600]


def _fingerprint(text: str) -> str:
    ignored = {"ano", "peux", "veux", "faire", "pour", "avec", "dans", "une", "des", "les", "the", "and"}
    words = [word for word in _fold(text).split() if word not in ignored]
    return " ".join(sorted(set(words))[:8]) or "besoin_non_classe"


def _similarity(left: str, right: str) -> float:
    """Similarité de besoin bornée ; aucun modèle lourd sur le chemin voix."""
    a, b = set(left.split()), set(right.split())
    return len(a & b) / max(1, len(a | b))


class AutoExtensionManager:
    def __init__(self, store: Path = _STORE):
        self.store = store
        self._lock = threading.RLock()
        self._running: set[str] = set()

    def _load(self) -> list[Need]:
        try:
            raw = json.loads(self.store.read_text(encoding="utf-8"))
            return [Need(**item) for item in raw.get("needs", []) if isinstance(item, dict)]
        except Exception:
            return []

    def _save(self, needs: list[Need]) -> None:
        self.store.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.store.with_suffix(".tmp")
        tmp.write_text(json.dumps({"needs": [asdict(item) for item in needs]}, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.store)

    def record_unmet(self, request: str, reason: str) -> Need | None:
        """Enregistre seulement une lacune, jamais une panne d'un outil existant."""
        text = " ".join(str(request or "").split())
        if len(text) < 8:
            return None
        fingerprint = _fingerprint(text)
        with self._lock:
            needs = self._load()
            candidate = next(
                (item for item in needs if item.status == "observed"
                 and _similarity(item.fingerprint, fingerprint) >= 0.55),
                None,
            )
            if candidate is None:
                candidate = Need(uuid.uuid4().hex[:12], fingerprint, [], _now(), _now())
                needs.append(candidate)
            candidate.samples = (candidate.samples + [f"{text} [{reason}]"])[-12:]
            candidate.updated_at = _now()
            self._save(needs)
            return candidate

    def eligible(self, threshold: int = 3, days: int = 14) -> list[Need]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, days))
        result = []
        for item in self._load():
            if item.status != "observed":
                continue
            recent = [sample for sample in item.samples if self._sample_time(sample, item.updated_at) >= cutoff]
            if len(recent) >= max(1, threshold):
                result.append(item)
        return result

    @staticmethod
    def _sample_time(_sample: str, fallback: str) -> datetime:
        try:
            return datetime.fromisoformat(fallback)
        except ValueError:
            return datetime.now(timezone.utc)

    def status(self) -> list[dict[str, Any]]:
        return [asdict(item) for item in self._load()]

    def control(self, action: str = "list", extension_id: str = "", *, plugins=None,
                refresh: Callable[[], None] | None = None) -> str:
        if action in {"list", "status"}:
            rows = self.status()
            if not rows:
                return "Aucune extension autonome enregistrée."
            return "\n".join(
                f"- {item['id']} · {item.get('extension_name') or item['fingerprint']} · {item['status']}"
                for item in rows
            )
        needs = self._load()
        item = next((entry for entry in needs if entry.id == extension_id or entry.extension_name == extension_id), None)
        if item is None:
            return "Extension autonome introuvable."
        if action == "reject" and item.status == "proposed":
            self._update(item.id, status="rejected")
            return f"Proposition {item.id} rejetée."
        if action in {"disable", "delete"}:
            if plugins is None or not item.extension_name:
                return "Extension active introuvable dans le registre plugins."
            if action == "disable":
                if not plugins.set_enabled(item.extension_name, False):
                    return "Impossible de désactiver cette extension."
                if refresh:
                    refresh()
                self._update(item.id, status="rejected")
                return f"Extension {item.extension_name} désactivée."
            path = plugins.directory / f"{item.extension_name}.py"
            if not path.exists():
                return "Fichier plugin introuvable ; aucune suppression effectuée."
            path.unlink()
            plugins.discover()
            if refresh:
                refresh()
            self._update(item.id, status="rejected")
            return f"Extension {item.extension_name} supprimée."
        return "Action extensions inconnue : list, reject, disable ou delete."

    def poll(self, *, plugins, refresh: Callable[[], None], ui=None,
             threshold: int = 3, days: int = 14) -> None:
        for need in self.eligible(threshold, days):
            with self._lock:
                if need.id in self._running:
                    continue
                self._running.add(need.id)
            threading.Thread(target=self._build, args=(need.id, plugins, refresh, ui), daemon=True,
                             name=f"auto-extension-{need.id}").start()

    def _update(self, need_id: str, **changes: Any) -> Need | None:
        with self._lock:
            needs = self._load()
            item = next((entry for entry in needs if entry.id == need_id), None)
            if item:
                for key, value in changes.items():
                    setattr(item, key, value)
                item.updated_at = _now()
                self._save(needs)
            return item

    def _build(self, need_id: str, plugins, refresh: Callable[[], None], ui) -> None:
        try:
            need = next((item for item in self._load() if item.id == need_id), None)
            if need is None:
                return
            spec = self._specification(need)
            self._update(need_id, status="building", specification=spec)
            stage = _STAGING / need_id
            stage.mkdir(parents=True, exist_ok=True)
            (stage / "PLUGIN_CONTRACT.md").write_text(
                "Créer exactement un fichier Python avec PLUGIN dict et run(parameters, **kwargs). "
                "Interdit : modifier ou importer main.py, core.audio_engine, core.session_manager, "
                "core.tool_dispatcher, core.action_runtime. Ajoute un test pytest dans tests/.\n",
                encoding="utf-8",
            )
            from core.ghost_agent import run_mission
            result = run_mission(
                f"{spec}\nTravaille UNIQUEMENT dans ce dossier de staging. Produis une capacité isolée sous forme de plugin.",
                stage, stage / "report.md", timeout_seconds=20 * 60,
            )
            plugin_file = self._validated_plugin(stage)
            self._run_tests(stage)
            preview = plugin_file.read_text(encoding="utf-8")[:1600]
            if result.status != "completed":
                raise RuntimeError(result.summary)
            self._update(need_id, status="proposed", extension_name=plugin_file.stem,
                         report=result.summary, preview=preview)
            self._request_activation(need_id, plugin_file, plugins, refresh, ui)
        except Exception as exc:
            self._update(need_id, status="failed", report=str(exc)[:800])
        finally:
            with self._lock:
                self._running.discard(need_id)

    def _specification(self, need: Need) -> str:
        examples = "\n".join(f"- {sample}" for sample in need.samples[-5:])
        return (
            "CAHIER DES CHARGES AUTO-GÉNÉRÉ\n"
            f"Objectif : satisfaire la famille de besoins « {need.fingerprint} ».\n"
            f"Demandes observées :\n{examples}\n"
            "Comportement : un unique outil plugin, paramètres strictement validés, réponses françaises, "
            "sans accès aux mécanismes audio, sécurité, destruction ou secrets. "
            "Intégration : respecter le contrat plugins/ d'ANO-GPT ; aucune modification du cœur. "
            "Tests : créer des tests spécifiques et exécuter les tests existants pertinents."
        )

    def _validated_plugin(self, stage: Path) -> Path:
        files = [path for path in stage.glob("*.py") if path.name != "__init__.py"]
        if len(files) != 1:
            raise RuntimeError("Le staging doit contenir exactement un nouveau plugin Python.")
        source = files[0].read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(files[0]))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        if any(name == blocked or name.startswith(blocked + ".") for name in imported for blocked in _FORBIDDEN_IMPORTS):
            raise RuntimeError("Plugin rejeté : import d'un module cœur protégé.")
        names = {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        if "run" not in names or "PLUGIN" not in source:
            raise RuntimeError("Plugin rejeté : contrat PLUGIN/run absent.")
        return files[0]

    def _run_tests(self, stage: Path) -> None:
        """La proposition est impossible sans tests plugin + contrat registre."""
        specific = list((stage / "tests").glob("test_*.py")) if (stage / "tests").is_dir() else []
        if not specific:
            raise RuntimeError("Extension rejetée : aucun test spécifique produit.")
        commands = [
            [sys.executable, "-m", "pytest", "-q", *map(str, specific)],
            [sys.executable, "-m", "pytest", "-q", "tests/test_plugin_registry.py"],
        ]
        for command in commands:
            result = subprocess.run(command, cwd=_ROOT, capture_output=True, text=True, timeout=180)
            if result.returncode != 0:
                raise RuntimeError(f"Tests extension échoués : {(result.stdout + result.stderr)[-900:]}")

    def _request_activation(self, need_id: str, plugin_file: Path, plugins, refresh: Callable[[], None], ui) -> None:
        def activate() -> str:
            target = plugins.directory / plugin_file.name
            if target.exists():
                return "Activation refusée : un plugin porte déjà ce nom."
            shutil.copy2(plugin_file, target)
            plugins.discover()
            if plugin_file.stem not in plugins.plugins:
                target.unlink(missing_ok=True)
                return "Activation refusée : le registre plugin a rejeté le module."
            plugins.set_enabled(plugin_file.stem, True)
            refresh()
            self._update(need_id, status="active")
            return f"Extension {plugin_file.stem} activée à chaud."
        detail = f"Besoin : {need_id}\nModule : {plugin_file.stem}\nAperçu :\n{plugin_file.read_text(encoding='utf-8')[:900]}"
        answer = human_confirmation.request("auto-extension", "Nouvelle capacité proposée", detail, activate)
        if ui and hasattr(ui, "write_log"):
            ui.write_log("SYS : extension autonome prête ; confirmation HUD requise." if "ATTENTE" in answer else answer)


_manager = AutoExtensionManager()


def get_auto_extension_manager() -> AutoExtensionManager:
    return _manager
