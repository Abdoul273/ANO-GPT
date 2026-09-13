"""tests/test_semantic_memory.py — Tests du rappel sémantique et enrichissement FTS5."""

import sqlite3
import pytest

from core import memory_store
from core.semantic_enricher import (
    extract_document_summary,
    extract_local_aliases,
    enrich_memory_record,
    schedule_memory_enrichment,
    shutdown_semantic_enricher,
)
from core.file_indexer import PersonalFileIndexer


@pytest.fixture
def temp_memory_db(tmp_path, monkeypatch):
    """Initialise une base de mémoire temporaire pour les tests."""
    db_file = tmp_path / "test_memory.db"
    monkeypatch.setattr(memory_store, "DB_PATH", db_file)
    monkeypatch.setattr(memory_store, "_INITIALISED", False)
    return db_file


@pytest.fixture
def temp_indexer(tmp_path):
    """Initialise un indexeur de fichiers temporaire."""
    db_file = tmp_path / "test_file_index.db"
    return PersonalFileIndexer(db_path=db_file)


# ── Tests de l'enrichissement sémantique local ────────────────────────────────

def test_extract_local_aliases():
    """Vérifie l'expansion de termes vers leurs grappes sémantiques."""
    # Véhicules
    aliases_car = extract_local_aliases("La Peugeot du garage de Matam", category="fact")
    assert "voiture" in aliases_car or "vehicule" in aliases_car or "auto" in aliases_car

    # Santé
    aliases_health = extract_local_aliases("Visite chez le docteur pour ma fièvre", category="fact")
    assert "sante" in aliases_health or "medecin" in aliases_health or "soin" in aliases_health

    # Technologie
    aliases_tech = extract_local_aliases("Configuration du switch et des vlan", category="dev")
    assert "reseau" in aliases_tech or "architecture" in aliases_tech or "serveur" in aliases_tech


# ── Tests de la mémoire (memory_store) avec recherche sémantique ─────────────

def test_semantic_recall_finds_unmentioned_synonyms(temp_memory_db):
    """« Mon histoire de voiture » retrouve « La Peugeot du garage de Matam »."""
    # Enregistrement sans le mot "voiture" explicitement
    memory_store.save(
        "La Peugeot du garage de Matam a été réparée",
        kind=memory_store.KIND_FACT,
        key="reparation_matam",
        enrich_async=False,
    )

    # Recherche avec le mot "voiture" (absent du texte original)
    results = memory_store.search("Mon histoire de voiture")
    assert len(results) >= 1
    assert "Peugeot" in results[0]["value"]
    assert results[0]["key"] == "reparation_matam"


def test_semantic_recall_medical(temp_memory_db):
    """« consultation santé » retrouve « rendez-vous chez le docteur »."""
    memory_store.save(
        "Consultation à la clinique Pasteur jeudi",
        kind=memory_store.KIND_FACT,
        key="rdv_pasteur",
        enrich_async=False,
    )

    results = memory_store.search("mes affaires de docteur et de santé")
    assert len(results) >= 1
    assert "Pasteur" in results[0]["value"]


def test_explicit_and_agent_aliases_enrichment(temp_memory_db, monkeypatch):
    """Vérifie l'enrichissement de fond par agent_brain."""
    memory_store.save(
        "Achat du billet pour Conakry",
        kind=memory_store.KIND_FACT,
        key="billet_voyage",
        enrich_async=False,
    )

    # Simuler le retour de l'agent_brain
    from core import agent_brain
    monkeypatch.setattr(agent_brain, "available", lambda: True)
    monkeypatch.setattr(
        agent_brain,
        "generate_memory_aliases",
        lambda text, category="": "avion vol aeroport voyage vacances sejour afrique",
    )

    conn = sqlite3.connect(temp_memory_db)
    row = conn.execute("SELECT id FROM memories WHERE key='billet_voyage'").fetchone()
    mem_id = row[0]
    conn.close()

    enrich_memory_record(
        temp_memory_db,
        mem_id,
        "Achat du billet pour Conakry",
        category="fact",
        key="billet_voyage",
        use_agent=True,
    )

    # Recherche par terme enrichi "aeroport"
    results = memory_store.search("départ à l'aéroport")
    assert len(results) >= 1
    assert "Conakry" in results[0]["value"]


# ── Tests de l'indexeur de fichiers avec résumé sémantique ───────────────────

def test_file_indexer_semantic_summary_search(temp_indexer, tmp_path):
    """« retrouve la note sur l'architecture réseau » retrouve un document technique."""
    doc = tmp_path / "infra_topology.txt"
    doc.write_text(
        "Configuration des sous-réseaux et routage des commutateurs Cisco.\n"
        "VLAN 10: serveurs\nVLAN 20: postes de travail\nPasserelle par défaut: 192.168.1.1",
        encoding="utf-8",
    )

    temp_indexer.index_file(doc)

    # Recherche sémantique avec mots-clés conceptuels
    results = temp_indexer.search("architecture reseau et serveurs")
    assert len(results) >= 1
    assert results[0].filename == "infra_topology.txt"


def test_extract_document_summary():
    content = """# Guide Déploiement Kubernetes
Ce document détaille l'infrastructure des conteneurs et les clusters de production.
Configuration des pods et ingress pour les microservices."""

    summary = extract_document_summary("deploy.md", content)
    assert "Guide Déploiement Kubernetes" in summary or "infrastructure" in summary


def test_async_enricher_worker_ne_bloque_pas_arret(tmp_path, monkeypatch):
    """Le worker est daemon et peut être arrêté explicitement."""
    from core import semantic_enricher

    calls = []
    monkeypatch.setattr(
        semantic_enricher,
        "enrich_memory_record",
        lambda *args: calls.append(args),
    )
    schedule_memory_enrichment(tmp_path / "memory.db", 1, "souvenir")
    worker = semantic_enricher._EXECUTOR
    assert worker is not None
    assert worker._thread.daemon is True
    shutdown_semantic_enricher(wait=True)
