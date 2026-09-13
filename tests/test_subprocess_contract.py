"""Aucun appel externe ne peut bloquer un thread sans limite.

Sur deux cœurs, Qt et l'audio partagent le GIL : un ``hyprctl`` ou un
``gtk-launch`` qui ne rend jamais la main gèle un thread — et la voix avec.
``core/action_kit.py`` existe pour ça (délai obligatoire, groupe de processus
tué en entier, lancements détachés). Ces tests vérifient que la règle du
CLAUDE.md est réellement tenue par le code, pas seulement écrite.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / "core" / "action_kit.py"

# Les appels qui attendent la fin du processus : sans délai, ils attendent
# pour toujours.
BLOCKING = {"run", "check_output", "check_call", "call"}

# ``Popen`` n'accepte aucun ``timeout`` : c'est un lancement, pas une attente.
# Il n'est donc légitime que détaché — un feu et oubli qui ne retient aucun
# thread. Cette liste recense les modules qui en gardent un usage direct
# assumé ; elle doit rétrécir, jamais grandir. Tout nouveau lancement passe
# par ``core.action_kit``.
POPEN_HERITAGE = {
    "actions/app_control.py",
    "actions/capture.py",
    "actions/desktop_apps.py",
    "actions/download_music.py",
    "actions/music.py",
    "actions/open_app.py",
    "actions/shell_exec.py",
    "core/audio_capture.py",
    "core/audio_router.py",
    "core/browser_policy.py",
    "core/ghost_agent.py",
    "core/isolated_interrupt.py",
    "core/llm_client.py",
    "core/player_ipc.py",
    "ui/orb/radial_waveform.py",
    "ui/window/system_ops.py",
}


def _sources():
    for path in sorted(ROOT.glob("**/*.py")):
        relative = path.relative_to(ROOT).as_posix()
        if (relative.startswith(("tests/", "build/", "mobile/"))
                or "__pycache__" in relative
                or path == KIT):
            continue
        try:
            yield relative, ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue


def _subprocess_calls(tree):
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "subprocess"):
            yield node


def test_aucun_appel_bloquant_sans_delai():
    faults = []
    for relative, tree in _sources():
        for node in _subprocess_calls(tree):
            if node.func.attr not in BLOCKING:
                continue
            if "timeout" not in {keyword.arg for keyword in node.keywords}:
                faults.append(f"{relative}:{node.lineno} subprocess.{node.func.attr}()")
    assert not faults, (
        "appel externe sans timeout — il peut geler un thread, donc la voix :\n  "
        + "\n  ".join(faults)
    )


def test_la_dette_popen_ne_grandit_pas():
    found = {
        relative for relative, tree in _sources()
        for node in _subprocess_calls(tree) if node.func.attr == "Popen"
    }
    nouveaux = found - POPEN_HERITAGE
    assert not nouveaux, (
        "nouveau Popen direct — passe par core.action_kit (lancement détaché, "
        "groupe de processus tué en entier) :\n  " + "\n  ".join(sorted(nouveaux))
    )
    # Un module nettoyé doit sortir de la liste, sinon elle ne rétrécit jamais.
    obsoletes = POPEN_HERITAGE - found
    assert not obsoletes, (
        "ces modules n'utilisent plus Popen : retire-les de POPEN_HERITAGE :\n  "
        + "\n  ".join(sorted(obsoletes))
    )


def test_le_kit_reste_la_reference():
    source = KIT.read_text(encoding="utf-8")
    assert "timeout" in source
    assert "killpg" in source or "setsid" in source or "start_new_session" in source


# ── HTTP : même contrat que les processus ────────────────────────────────────

HTTP_METHODS = {"get", "post", "put", "delete", "patch", "head", "request", "urlopen"}


def _http_calls(tree):
    """Appels ``requests.X(``, ``urllib.request.urlopen(``, ``<session>.request(``."""
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr not in HTTP_METHODS:
            continue
        base = node.func.value
        if isinstance(base, ast.Name) and base.id in {"requests", "httpx"}:
            yield node, base.id
        elif (isinstance(base, ast.Attribute) and base.attr == "request"
                and isinstance(base.value, ast.Name) and base.value.id == "urllib"):
            yield node, "urllib"
        elif node.func.attr == "request" and isinstance(base, (ast.Name, ast.Attribute)):
            name = base.id if isinstance(base, ast.Name) else base.attr
            if "session" in name.lower():
                yield node, "session"


def test_aucun_appel_http_sans_delai():
    faults = []
    for relative, tree in _sources():
        for node, _ in _http_calls(tree):
            keywords = {keyword.arg for keyword in node.keywords}
            if "timeout" in keywords or None in keywords:  # **kwargs : délai posé en amont
                continue
            faults.append(f"{relative}:{node.lineno} {ast.unparse(node.func)}()")
    assert not faults, (
        "appel HTTP sans timeout — un service muet retient l'action et le micro :\n  "
        + "\n  ".join(faults)
    )


def test_les_actions_passent_par_kit_http():
    """Dans ``actions/``, pas de ``requests`` direct : ``kit.http()`` impose le délai."""
    faults = [
        f"{relative}:{node.lineno} {lib}.{node.func.attr}()"
        for relative, tree in _sources() if relative.startswith("actions/")
        for node, lib in _http_calls(tree) if lib in {"requests", "httpx"}
    ]
    assert not faults, (
        "requests direct dans une action — utilise core.action_kit.http() :\n  "
        + "\n  ".join(faults)
    )


def test_kit_http_impose_un_delai(monkeypatch):
    import core.action_kit as kit

    monkeypatch.setattr(kit, "_http_client", None)
    client = kit.http()
    seen = {}

    def fake_request(self, method, url, **kwargs):
        seen.update(kwargs)
        return None

    import requests
    monkeypatch.setattr(requests.Session, "request", fake_request)
    client.get("http://localhost:1/")
    assert seen["timeout"] == kit.HTTP_TIMEOUT
    client.get("http://localhost:1/", timeout=30)
    assert seen["timeout"] == 30
    assert kit.http() is client
