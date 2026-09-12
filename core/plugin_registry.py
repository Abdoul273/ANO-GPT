"""Registre de plugins ANO-GPT, versionné et rétrocompatible.

Formats : ``plugins/nom.py`` (historique) ou ``plugins/nom/plugin.json`` +
``main.py`` (public). Un plugin est du code de confiance : les permissions
déclarent et auditent ses besoins, elles ne constituent pas une sandbox.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import inspect
import json
import re
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

_NAME = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:[-+][a-zA-Z0-9.-]+)?$")
_PERMISSIONS = frozenset({"filesystem_read", "filesystem_write", "network", "subprocess", "clipboard"})
_API_VERSION = "1"


@dataclass(frozen=True)
class Plugin:
    name: str
    description: str
    parameters: dict[str, Any]
    run: Callable[..., Any]
    path: Path
    version: str = "0.0.0"
    permissions: tuple[str, ...] = ()
    source_format: str = "legacy"
    checksum: str = ""


class PluginRegistry:
    """Découvre, valide et exécute les extensions locales ANO-GPT."""
    def __init__(self, directory: Path, state_file: Path, core_names: set[str]):
        self.directory, self.state_file, self.core_names = directory, state_file, set(core_names)
        self.plugins: dict[str, Plugin] = {}
        self.errors: dict[str, str] = {}
        self._lock = threading.RLock()

    def discover(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        found: dict[str, Plugin] = {}
        errors: dict[str, str] = {}
        candidates = list(sorted(self.directory.glob("*.py")))
        candidates += sorted(p for p in self.directory.iterdir()
                             if p.is_dir() and not p.name.startswith("_") and (p / "plugin.json").is_file())
        for path in candidates:
            if path.name.startswith("_"):
                continue
            label = path.name if path.is_file() else f"{path.name}/plugin.json"
            try:
                plugin = self._load_legacy(path) if path.is_file() else self._load_package(path)
                if plugin.name in self.core_names or plugin.name in found:
                    raise ValueError("nom déjà utilisé par un outil ou plugin")
                found[plugin.name] = plugin
            except Exception as exc:
                errors[label] = str(exc)
        with self._lock:
            self.plugins, self.errors = found, errors

    def _load_legacy(self, path: Path) -> Plugin:
        module = self._load_module(f"ano_plugins.legacy_{path.stem}", path)
        return self._make_plugin(getattr(module, "PLUGIN", None), getattr(module, "run", None), path,
                                 source_format="legacy")

    def _load_package(self, folder: Path) -> Plugin:
        manifest_path = folder / "plugin.json"
        try:
            meta = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"plugin.json invalide : {exc.msg}") from exc
        if not isinstance(meta, dict):
            raise ValueError("plugin.json doit être un objet JSON")
        if str(meta.get("api_version", "")) != _API_VERSION:
            raise ValueError(f"api_version doit être {_API_VERSION!r}")
        filename, separator, function = str(meta.get("entrypoint", "main.py:run")).partition(":")
        if not separator or not filename or not function or Path(filename).name != filename:
            raise ValueError("entrypoint attendu : 'main.py:run' (fichier local uniquement)")
        source = folder / filename
        if source.suffix != ".py" or not source.is_file():
            raise ValueError(f"entrypoint introuvable : {filename}")
        module = self._load_module(f"ano_plugins.package_{folder.name}", source)
        return self._make_plugin(meta, getattr(module, function, None), folder, source_format="package",
                                 checksum=self._checksum(manifest_path, source))

    @staticmethod
    def _load_module(module_name: str, path: Path) -> Any:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ValueError("module illisible")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module

    def _make_plugin(self, meta: Any, run: Any, path: Path, *, source_format: str,
                     checksum: str = "") -> Plugin:
        if not isinstance(meta, dict) or not callable(run):
            raise ValueError("PLUGIN/plugin.json ou fonction run(parameters, ...) manquant")
        name, description = str(meta.get("name") or ""), str(meta.get("description") or "").strip()
        parameters = meta.get("parameters") or {"type": "OBJECT", "properties": {}}
        version = str(meta.get("version", "0.0.0" if source_format == "legacy" else ""))
        requested = meta.get("permissions", [])
        if not isinstance(requested, list) or not all(isinstance(p, str) for p in requested):
            raise ValueError("permissions doit être une liste de chaînes")
        unknown = sorted(set(requested) - _PERMISSIONS)
        if unknown:
            raise ValueError("permission inconnue : " + ", ".join(unknown))
        if not _NAME.fullmatch(name):
            raise ValueError("nom invalide (minuscules, chiffres et underscore)")
        if not description or not isinstance(parameters, dict) or parameters.get("type") != "OBJECT":
            raise ValueError("description ou schéma de paramètres invalide")
        if source_format == "package" and not _VERSION.fullmatch(version):
            raise ValueError("version doit suivre SemVer, par ex. 1.0.0")
        return Plugin(name, description, parameters, run, path, version, tuple(sorted(set(requested))),
                      source_format, checksum)

    @staticmethod
    def _checksum(*paths: Path) -> str:
        digest = hashlib.sha256()
        for path in paths:
            digest.update(path.read_bytes())
        return digest.hexdigest()[:16]

    def _states(self) -> dict[str, bool]:
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
            return {str(k): bool(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
        except Exception:
            return {}

    def enabled(self, name: str) -> bool:
        return self._states().get(name, True)

    def set_enabled(self, name: str, enabled: bool) -> bool:
        with self._lock:
            if name not in self.plugins:
                return False
            states = self._states(); states[name] = bool(enabled)
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_file.with_suffix(".tmp")
            temporary.write_text(json.dumps(states, indent=2, sort_keys=True), encoding="utf-8")
            temporary.replace(self.state_file)
            return True

    def declarations(self) -> list[dict[str, Any]]:
        with self._lock:
            return [{"name": p.name, "description": p.description, "parameters": p.parameters}
                    for p in self.plugins.values() if self.enabled(p.name)]

    def status(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = [{"name": p.name, "description": p.description, "enabled": self.enabled(p.name),
                     "file": str(p.path.relative_to(self.directory)), "error": "", "version": p.version,
                     "permissions": list(p.permissions), "format": p.source_format, "checksum": p.checksum}
                    for p in self.plugins.values()]
            rows.extend({"name": f, "description": "", "enabled": False, "file": f, "error": e,
                         "version": "", "permissions": [], "format": "invalid", "checksum": ""}
                        for f, e in self.errors.items())
            return rows

    def run(self, name: str, parameters: dict[str, Any], *, player=None, session_memory=None) -> str:
        with self._lock:
            plugin = self.plugins.get(name)
        if plugin is None or not self.enabled(name):
            return f"Plugin indisponible : {name}."
        if not isinstance(parameters, dict):
            return f"Paramètres invalides pour le plugin {name}."
        try:
            signature = inspect.signature(plugin.run)
            all_kwargs = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values())
            kwargs: dict[str, Any] = {}
            if all_kwargs or "player" in signature.parameters: kwargs["player"] = player
            if all_kwargs or "session_memory" in signature.parameters: kwargs["session_memory"] = session_memory
            result = plugin.run(parameters, **kwargs)
            if inspect.isawaitable(result): result = asyncio.run(result)
            return str(result or "Terminé.")
        except Exception as exc:
            return f"Le plugin {name} a échoué : {exc}"
