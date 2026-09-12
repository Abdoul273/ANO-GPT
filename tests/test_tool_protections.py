"""Les protections d'exécution s'appliquent à TOUS les outils, pas à quelques-uns.

Deux moteurs coexistent dans le projet :

* ``core/action_runtime.py`` — celui qui tourne réellement. Chaque appel passe
  par ``lease()`` : validation du schéma, délai propre à l'outil, limite de
  concurrence, file bornée et circuit breaker.
* ``core/tool_registry.py`` — il ne sert plus qu'à **fabriquer les
  déclarations**. Son moteur d'exécution (limiteur de débit, circuit breaker,
  ``ToolResult``) n'est appelé nulle part en production.

Ces tests verrouillent cette répartition : le jour où quelqu'un exécutera un
outil hors du ``lease``, il perdra silencieusement les cinq protections d'un
coup — et ces tests le diront avant l'utilisateur.
"""

import asyncio
import time

import pytest

from core.action_runtime import (
    ActionCircuitOpen,
    ActionQueueFull,
    ActionRuntime,
    ActionValidationError,
)
from core.tool_dispatcher import TOOL_DECLARATIONS


def _runtime() -> ActionRuntime:
    return ActionRuntime(TOOL_DECLARATIONS)


def test_chaque_outil_declare_est_connu_du_moteur():
    runtime = _runtime()
    declared = {tool["name"] for tool in TOOL_DECLARATIONS}
    assert declared <= runtime.known_actions
    # Un outil inconnu est refusé avant d'exister : c'est la validation.
    with pytest.raises(ActionValidationError):
        runtime.prepare("outil_invente_par_le_modele", {})


def test_chaque_outil_a_un_delai_borne():
    runtime = _runtime()
    for tool in TOOL_DECLARATIONS:
        policy = runtime.policy_for(tool["name"])
        assert 0 < policy.timeout_s <= 1800, tool["name"]
        assert policy.max_concurrency >= 1


def test_le_circuit_souvre_apres_des_echecs_repetes():
    runtime = _runtime()
    policy = runtime.policy_for("web_search")
    for _ in range(policy.failure_threshold):
        runtime.note_failure("web_search")
    with pytest.raises(ActionCircuitOpen):
        runtime.ensure_available("web_search")
    # Un succès referme immédiatement : une panne passagère ne doit pas
    # condamner l'outil pour toute la durée du refroidissement.
    runtime.note_success("web_search")
    runtime.ensure_available("web_search")


def test_le_circuit_se_referme_seul_apres_le_refroidissement(monkeypatch):
    runtime = _runtime()
    policy = runtime.policy_for("web_search")
    for _ in range(policy.failure_threshold):
        runtime.note_failure("web_search")
    with pytest.raises(ActionCircuitOpen):
        runtime.ensure_available("web_search")

    plus_tard = time.monotonic() + policy.cooldown_s + 1
    monkeypatch.setattr(time, "monotonic", lambda: plus_tard)
    runtime.ensure_available("web_search")  # ne lève plus


def test_le_bail_applique_le_circuit_et_borne_la_file():
    async def scenario():
        runtime = _runtime()
        policy = runtime.policy_for("web_search")

        # Circuit ouvert : le bail refuse avant même d'exécuter quoi que ce soit.
        for _ in range(policy.failure_threshold):
            runtime.note_failure("web_search")
        with pytest.raises(ActionCircuitOpen):
            async with runtime.lease("web_search"):
                pass
        runtime.note_success("web_search")

        # File bornée : au-delà de max_pending, on refuse au lieu d'empiler.
        runtime._pending["web_search"] = policy.max_pending
        with pytest.raises(ActionQueueFull):
            async with runtime.lease("web_search"):
                pass

    asyncio.run(scenario())


def test_le_repartiteur_execute_toujours_sous_bail_et_sous_delai():
    """Le câblage : sans lui, les protections ci-dessus ne servent à rien."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "core" / "tool_dispatcher.py").read_text(
        encoding="utf-8"
    )
    corps = source.split("async def _execute_tool(self, fc)")[1].split("\n    async def ")[0]
    assert "self._action_runtime.lease(" in corps, "exécution hors bail : plus aucune protection"
    assert "asyncio.timeout(" in corps, "aucun délai appliqué à l'exécution"
    assert "_execute_tool_impl(fc, prepared)" in corps
    # L'ordre compte : le délai doit envelopper l'exécution, à l'intérieur du bail.
    assert corps.index("lease(") < corps.index("asyncio.timeout(") < corps.index(
        "_execute_tool_impl(fc, prepared)"
    )


def test_le_registre_ne_sert_qu_a_declarer():
    """Son moteur d'exécution est dormant : personne ne doit croire l'inverse."""
    from pathlib import Path

    racine = Path(__file__).resolve().parents[1]
    principal = (racine / "main.py").read_text(encoding="utf-8")
    assert "build_production_declarations(" in principal
    # Si un jour le registre exécute pour de bon, ce test doit être réécrit en
    # connaissance de cause plutôt que de laisser deux moteurs se marcher dessus.
    assert "self._tool_registry.execute(" not in principal
