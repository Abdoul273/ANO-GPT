"""Registre de plugins ANO-GPT, versionné et rétrocompatible.

Formats : ``plugins/nom.py`` (historique) ou ``plugins/nom/plugin.json`` +
``main.py`` (public). Un plugin est du code de confiance une fois approuvé :
les permissions déclarent et auditent ses besoins, elles ne constituent pas
une sandbox. Avant approbation, sa métadonnée est lue SANS jamais exécuter
son code : ``discover()`` ne fait qu'analyser statiquement (JSON ou AST), et
le module n'est importé qu'au premier appel de ``run()`` sur un plugin
explicitement activé pour son empreinte de code actuelle.
"""
from __future__ import annotations

import ast
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
    entry_path: Path
    entry_func: str
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
        # (empreinte approuvée, fonction importée) par plugin — jamais rempli
        # tant que le plugin n'a pas été explicitement activé sur CETTE empreinte.
        self._loaded: dict[str, tuple[str, Callable[..., Any]]] = {}

    def discover(self) -> None:
        """Analyse statiquement chaque candidat (JSON ou AST) : aucun code de
        plugin n'est importé ni exécuté ici, activé ou non."""
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
                plugin = self._read_legacy_manifest(path) if path.is_file() else self._read_package_manifest(path)
                if plugin.name in self.core_names or plugin.name in found:
                    raise ValueError("nom déjà utilisé par un outil ou plugin")
                found[plugin.name] = plugin
            except Exception as exc:
                errors[label] = str(exc)
        with self._lock:
            self.plugins, self.errors = found, errors
            # Une empreinte disparue (fichier modifié/supprimé) ne doit jamais
            # laisser tourner l'ancien import mis en cache.
            for name in list(self._loaded):
                current = found.get(name)
                if current is None or self._loaded[name][0] != current.checksum:
                    self._loaded.pop(name, None)
            self._migrate_states(found)

    def _read_legacy_manifest(self, path: Path) -> Plugin:
        """Lit ``PLUGIN`` et confirme la présence de ``run`` par analyse
        statique du code source, sans jamais l'importer."""
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (OSError, SyntaxError) as exc:
            raise ValueError(f"fichier illisible : {exc}") from exc
        meta = None
        has_run = False
        for node in tree.body:
            if (isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "PLUGIN" for t in node.targets)):
                try:
                    meta = ast.literal_eval(node.value)
                except (ValueError, TypeError) as exc:
                    raise ValueError(f"PLUGIN doit être un littéral statique (dict) : {exc}") from exc
            if isinstance(node, ast.FunctionDef) and node.name == "run":
                has_run = True
        if not has_run:
            raise ValueError("fonction run(parameters, ...) manquante au niveau du module")
        checksum = self._checksum(path)
        return self._make_plugin(meta, path, source_format="legacy", checksum=checksum,
                                 entry_path=path, entry_func="run")

    def _read_package_manifest(self, folder: Path) -> Plugin:
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
        checksum = self._checksum(manifest_path, source)
        return self._make_plugin(meta, folder, source_format="package", checksum=checksum,
                                 entry_path=source, entry_func=function)

    @staticmethod
    def _load_module(module_name: str, path: Path) -> Any:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ValueError("module illisible")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module

    def _ensure_loaded(self, plugin: Plugin) -> Callable[..., Any]:
        """Importe le point d'entrée au tout premier appel — jamais avant —
        et met le résultat en cache tant que l'empreinte ne change pas."""
        with self._lock:
            cached = self._loaded.get(plugin.name)
            if cached is not None and cached[0] == plugin.checksum:
                return cached[1]
        module_name = f"ano_plugins.{plugin.source_format}_{plugin.entry_path.stem}_{plugin.checksum}"
        module = self._load_module(module_name, plugin.entry_path)
        func = getattr(module, plugin.entry_func, None)
        if not callable(func):
            raise ValueError(f"fonction {plugin.entry_func} introuvable dans {plugin.entry_path.name}")
        with self._lock:
            self._loaded[plugin.name] = (plugin.checksum, func)
        return func

    def _make_plugin(self, meta: Any, path: Path, *, source_format: str, checksum: str,
                     entry_path: Path, entry_func: str) -> Plugin:
        if not isinstance(meta, dict):
            raise ValueError("PLUGIN/plugin.json manquant ou invalide")
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
        return Plugin(name, description, parameters, entry_path, entry_func, path, version,
                      tuple(sorted(set(requested))), source_format, checksum)

    @staticmethod
    def _checksum(*paths: Path) -> str:
        digest = hashlib.sha256()
        for path in paths:
            digest.update(path.read_bytes())
        return digest.hexdigest()[:16]

    def _states(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if not isinstance(raw, dict):
            return {}
        states: dict[str, dict[str, Any]] = {}
        for key, value in raw.items():
            if isinstance(value, dict):
                states[str(key)] = {"enabled": bool(value.get("enabled")),
                                    "checksum": str(value.get("checksum") or "")}
            else:
                # Ancien format (avant l'épinglage par empreinte) : un booléen nu.
                states[str(key)] = {"enabled": bool(value), "checksum": ""}
        return states

    def _write_states(self, states: dict[str, dict[str, Any]]) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(states, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(self.state_file)

    def _migrate_states(self, found: dict[str, Plugin]) -> None:
        """Convertit les entrées héritées (booléen nu) en accordant une
        confiance ponctuelle sur l'empreinte actuelle d'un plugin déjà
        installé — pour ne pas couper silencieusement un plugin en service
        à la seule mise à jour de ce registre."""
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(raw, dict):
            return
        changed = False
        migrated: dict[str, dict[str, Any]] = {}
        for key, value in raw.items():
            if isinstance(value, dict):
                migrated[key] = value
                continue
            changed = True
            plugin = found.get(key)
            migrated[key] = {"enabled": bool(value), "checksum": plugin.checksum if plugin else ""}
        if changed:
            self._write_states(migrated)

    def enabled(self, name: str) -> bool:
        state = self._states().get(name)
        if state is None:
            return False  # jamais approuvé : refusé par défaut, y compris à la découverte
        plugin = self.plugins.get(name)
        if plugin is not None and state["checksum"] and state["checksum"] != plugin.checksum:
            return False  # le code a changé depuis la dernière approbation : à revalider
        return bool(state["enabled"])

    def set_enabled(self, name: str, enabled: bool) -> bool:
        with self._lock:
            plugin = self.plugins.get(name)
            if plugin is None:
                return False
            states = self._states()
            # Activer approuve explicitement le CODE ACTUEL de ce plugin ;
            # désactiver n'a pas besoin de connaître son empreinte.
            states[name] = {"enabled": bool(enabled),
                            "checksum": plugin.checksum if enabled else states.get(name, {}).get("checksum", "")}
            self._write_states(states)
            return True

    def declarations(self) -> list[dict[str, Any]]:
        with self._lock:
            return [{"name": p.name, "description": p.description, "parameters": p.parameters}
                    for p in self.plugins.values() if self.enabled(p.name)]

    def status(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = []
            for p in self.plugins.values():
                state = self._states().get(p.name)
                needs_approval = state is None or (state["checksum"] and state["checksum"] != p.checksum)
                rows.append({"name": p.name, "description": p.description, "enabled": self.enabled(p.name),
                             "file": str(p.path.relative_to(self.directory)), "error": "", "version": p.version,
                             "permissions": list(p.permissions), "format": p.source_format,
                             "checksum": p.checksum, "needs_approval": needs_approval})
            rows.extend({"name": f, "description": "", "enabled": False, "file": f, "error": e,
                         "version": "", "permissions": [], "format": "invalid", "checksum": "",
                         "needs_approval": False}
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
            func = self._ensure_loaded(plugin)
            signature = inspect.signature(func)
            all_kwargs = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values())
            kwargs: dict[str, Any] = {}
            if all_kwargs or "player" in signature.parameters: kwargs["player"] = player
            if all_kwargs or "session_memory" in signature.parameters: kwargs["session_memory"] = session_memory
            result = func(parameters, **kwargs)
            if inspect.isawaitable(result): result = asyncio.run(result)
            return str(result or "Terminé.")
        except Exception as exc:
            return f"Le plugin {name} a échoué : {exc}"
