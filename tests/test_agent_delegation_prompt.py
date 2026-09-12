"""Contrat de routage : les opérations ambiguës doivent passer par agy."""

from pathlib import Path


def test_les_depots_ambigus_sont_delegues_a_agy_avant_execution():
    prompt = (Path(__file__).resolve().parents[1] / "core" / "prompt.txt").read_text(
        encoding="utf-8"
    )

    assert "cloner ou\n  télécharger un dépôt" in prompt
    assert "Ne compose jamais une URL GitHub" in prompt
    assert "dossier ciblé comme workspace" in prompt
    assert "agy` fait la recherche et l'exécution" in prompt
