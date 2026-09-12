from __future__ import annotations

from core import decision_simulator as simulator


def test_extrait_les_options_depuis_une_comparaison_vocale():
    assert simulator.extract_options("location ou achat") == ["location", "achat"]
    assert simulator.extract_options("A vs B") == ["A", "B"]


def test_simulation_recherche_debat_et_archive(monkeypatch):
    saved = {}
    monkeypatch.setattr(simulator, "_memory_context", lambda _: "Projet ANO, budget limité.")
    monkeypatch.setattr(simulator, "_research", lambda option, _: f"Données web pour {option}")
    monkeypatch.setattr(simulator, "simulation_timeout_seconds", lambda: 90)
    calls = []

    def reason(prompt, context, timeout):
        calls.append((prompt, context, timeout))
        if "arbitre indépendant" in prompt:
            return (
                "## Recommandation\nOption A sous condition.\n\n"
                "## Trade-offs\nCoût contre délai.\n\n"
                "## Risques par option\nÀ vérifier.\n\n"
                "## Hypothèses à vérifier\nBudget.\n\n## Prochain pas\nDemander un devis."
            )
        return "Bénéfice, risque et hypothèse."

    monkeypatch.setattr(simulator, "_reason", reason)
    monkeypatch.setattr(
        simulator.knowledge_graph,
        "ingest_note",
        lambda **kwargs: saved.update(kwargs) or 1,
    )

    result = simulator.run_simulation("Option A vs Option B")

    assert result.options == ("Option A", "Option B")
    assert set(result.evidence) == {"Option A", "Option B"}
    assert len(calls) == 3  # un avocat par option, puis l'arbitre
    assert "Simulation décisionnelle" in saved["title"]
    assert "## Recommandation" in saved["content"]


def test_simulation_refuse_une_option_unique():
    try:
        simulator.run_simulation("Faire un choix")
    except simulator.DecisionSimulationError as exc:
        assert "deux options" in str(exc)
    else:
        raise AssertionError("une simulation sans comparaison doit être refusée")
