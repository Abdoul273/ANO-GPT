"""Une panne doit toujours laisser une trace exploitable.

Le symptôme d'origine : « L'outil a échoué », prononcé sans que rien nulle part
ne dise pourquoi. Trente-cinq modules journalisaient dans le vide — aucun
collecteur n'était installé — et le chemin des outils jetait la pile d'appel
après en avoir tiré une phrase pour l'utilisateur.
"""

import ast
import json
import logging
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Un `pass` seul reste légitime autour d'un effet de bord décoratif — fermer une
# carte, rafraîchir un affichage — où l'échec n'apprend rien. Il ne l'est jamais
# autour de l'exécution elle-même. Ce plafond rend la dette visible et
# l'empêche de croître ; il doit descendre, jamais monter.
PLAFOND_HANDLERS_MUETS = 584


def _sources():
    for path in sorted(ROOT.glob("**/*.py")):
        relative = path.relative_to(ROOT).as_posix()
        if relative.startswith(("tests/", "build/", "mobile/")) or "__pycache__" in relative:
            continue
        try:
            yield relative, ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue


def _is_silent(handler: ast.ExceptHandler) -> bool:
    body = [
        node for node in handler.body
        if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))
    ]
    return len(body) == 1 and isinstance(body[0], ast.Pass)


def test_aucun_except_nu():
    """`except:` avale Ctrl-C et SystemExit : il rend l'arrêt impossible."""
    faults = [
        f"{relative}:{node.lineno}"
        for relative, tree in _sources()
        for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler) and node.type is None
    ]
    assert not faults, "except nu — précise le type attrapé :\n  " + "\n  ".join(faults)


def test_la_dette_de_silence_ne_grandit_pas():
    muets = sum(
        1 for _, tree in _sources() for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler) and _is_silent(node)
    )
    assert muets <= PLAFOND_HANDLERS_MUETS, (
        f"{muets} handlers muets contre un plafond de {PLAFOND_HANDLERS_MUETS} : "
        "journalise l'échec ou renvoie une phrase exploitable."
    )


def test_le_chemin_des_outils_garde_la_pile_dappel():
    source = (ROOT / "core" / "tool_dispatcher.py").read_text(encoding="utf-8")
    assert "tool_failure(" in source, (
        "l'échec d'un outil ne laisse aucune trace : la phrase lue à "
        "l'utilisateur ne suffit pas à diagnostiquer"
    )


def test_le_collecteur_est_installe_au_demarrage():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "setup_logging()" in source, "aucun collecteur : tout se journalise dans le vide"
    assert "install_asyncio_handler(" in source, "une tâche asyncio peut mourir en silence"


def test_le_journal_est_du_jsonl_qui_garde_la_trace_separee():
    from core import observability as obs

    with tempfile.TemporaryDirectory() as folder:
        destination = obs.setup_logging(directory=Path(folder))
        try:
            logging.getLogger("anogpt.essai").info("démarrage", extra={"outils": 25})
            try:
                raise ValueError("panne simulée")
            except ValueError as exc:
                obs.tool_failure("phone_sms", exc, message="Envoi impossible.",
                                 duration_ms=42.0, args={"target": "maman"})
        finally:
            obs.shutdown_logging()

        lignes = [json.loads(l) for l in destination.read_text(encoding="utf-8").splitlines()]

    debut = next(l for l in lignes if l["logger"] == "anogpt.essai")
    assert debut["outils"] == 25, "les champs de l'appelant doivent survivre"

    echec = next(l for l in lignes if l["logger"] == "anogpt.tools")
    # La pile reste un champ séparé : aplatie dans le message, elle redevient
    # illisible pour un script de diagnostic.
    assert "panne simulée" not in echec["msg"]
    assert "ValueError: panne simulée" in echec["exc"]
    assert echec["tool"] == "phone_sms" and echec["duration_ms"] == 42.0
    assert echec["arg_keys"] == ["target"], "les valeurs restent privées, les clés suffisent"


def test_le_journal_tourne_et_ne_bloque_pas_le_thread_qui_parle():
    from core import observability as obs

    assert obs.MAX_BYTES <= 10 * 1024 * 1024 and obs.BACKUP_COUNT >= 1
    source = (ROOT / "core" / "observability.py").read_text(encoding="utf-8")
    # Sur deux cœurs, écrire sur disque depuis le thread audio hache la voix :
    # la file déporte l'écriture vers un thread unique.
    assert "QueueListener" in source and "QueueHandler" in source
    assert "threading.excepthook" in source, "un thread démon peut mourir en silence"
