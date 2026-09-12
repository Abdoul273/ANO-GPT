"""Tests unitaires et d'intégration pour actions/second_brain.py et core/knowledge_graph.py."""

from datetime import datetime
from pathlib import Path
import pytest

from actions.second_brain import (
    parse_second_brain_intent,
    second_brain_action,
)
from core import knowledge_graph as graph


# ════════════════════════════════════════════════════════════════════════════
# 1. Parsing vocal local de l'intention Second Brain
# ════════════════════════════════════════════════════════════════════════════

def test_parse_second_brain_phrase_du_catalogue():
    """Vérifie la phrase exacte de la roadmap : « Retrouve-moi la commande pour compresser les vidéos dont on avait parlé le mois dernier. »"""
    phrase = "Retrouve-moi la commande pour compresser les vidéos dont on avait parlé le mois dernier."
    intent = parse_second_brain_intent(phrase)
    assert intent is not None
    assert intent["action"] == "search"
    assert "compresser les vidéos" in intent["query"]


def test_parse_second_brain_save_command_and_notes():
    p_cmd = parse_second_brain_intent("Mémorise la commande suivante : ffmpeg -i in.mov -crf 28 out.mp4")
    assert p_cmd is not None
    assert p_cmd["action"] == "save_command"
    assert "ffmpeg" in p_cmd["command"]

    p_proj = parse_second_brain_intent("Indexe le projet /home/anonymous/OUTILS/ANO-GPT")
    assert p_proj is not None
    assert p_proj["action"] == "index_project"
    assert "ANO-GPT" in p_proj["project"]

    p_status = parse_second_brain_intent("Quel est l'état du second brain ?")
    assert p_status is not None
    assert p_status["action"] == "status"


# ════════════════════════════════════════════════════════════════════════════
# 2. Recherche associative par analogie d'idées & filtrage temporel
# ════════════════════════════════════════════════════════════════════════════

def test_retrouve_commande_par_analogie_mois_dernier(tmp_path):
    """Teste la recherche par analogie d'idées et la fenêtre temporelle 'le mois dernier'."""
    db = tmp_path / "brain_test.db"

    # Enregistrement d'un échange le mois dernier (juillet 2026 pour un 'now' en août 2026)
    graph.upsert_source(
        kind="conversation",
        external_id="conv-orion-compress",
        title="Session encodage vidéo pour le projet Orion",
        content=(
            "Pour alléger les rushs vidéo sans perte visible, utilise :\n"
            "`ffmpeg -i input.mp4 -c:v libx264 -crf 28 compressed.mp4`"
        ),
        happened="2026-07-15",
        project="Orion",
        db_path=db,
    )

    # Enregistrement d'une commande récente (août 2026) sans rapport avec le mois dernier
    graph.upsert_source(
        kind="conversation",
        external_id="conv-recent",
        title="Session récente",
        content="`ffmpeg -i autre.mp4 -crf 18 haute_qualite.mp4`",
        happened="2026-08-25",
        db_path=db,
    )

    # Recherche par analogie exacte de la roadmap
    res = graph.search(
        "Retrouve-moi la commande pour compresser les vidéos dont on avait parlé le mois dernier.",
        db_path=db,
        now=datetime(2026, 8, 30),
        limit=5,
    )

    # La commande du mois dernier avec crf 28 doit remonter en tête
    assert len(res) >= 1
    assert any(r.kind == "command" and "crf 28" in r.content for r in res)
    # La commande récente d'août ne doit pas figurer dans les résultats du mois dernier
    assert all("crf 18" not in r.content for r in res)


def test_analogie_d_idees_sans_mots_cles_exacts(tmp_path):
    """Teste la recherche conceptuelle lorsque les mots de la requête diffèrent du texte indexé."""
    db = tmp_path / "brain_analogy.db"

    # On indexe une commande de téléchargement sans le mot 'telecharger'
    graph.upsert_source(
        kind="conversation",
        external_id="conv-ytdl",
        title="Extraction de stream",
        content="Pour récupérer le flux : `yt-dlp -f bestvideo+bestaudio url`",
        happened="2026-08-10",
        db_path=db,
    )

    # Requête de l'utilisateur avec synonymes/analogie
    res = graph.search(
        "l'outil pour télécharger des vidéos",
        db_path=db,
        now=datetime(2026, 8, 30),
    )
    assert len(res) >= 1
    assert any("yt-dlp" in r.content for r in res)


# ════════════════════════════════════════════════════════════════════════════
# 3. Réseau associatif reliant Personnes, Projets, Fichiers & Commandes
# ════════════════════════════════════════════════════════════════════════════

def test_reseau_associatif_multi_noeuds(tmp_path):
    """Vérifie la traversée de graphe reliant Contact -> Projet -> Fichier -> Commande."""
    db = tmp_path / "brain_graph.db"

    # 1. Contact
    graph.upsert_contact({"id": "bob", "name": "Bob Dupont", "aliases": ["Bob"]}, db_path=db)

    # 2. Projet relié au contact
    graph.upsert_source(
        kind="note",
        external_id="note-projet-alpha",
        title="Lancement Projet Alpha",
        content="Bob Dupont est le lead développeur sur le projet Alpha.",
        happened="2026-08-01",
        project="Alpha",
        db_path=db,
    )

    # 3. Fichier du projet contenant une commande
    graph.upsert_source(
        kind="file",
        external_id="/projects/alpha/deploy.sh",
        title="deploy.sh",
        content="Script de déploiement : `docker-compose up -d --build`",
        path="/projects/alpha/deploy.sh",
        happened="2026-08-05",
        project="Alpha",
        db_path=db,
    )

    # Recherche du contact associé au projet
    res_person = graph.search("qui gère le projet Alpha ?", db_path=db)
    assert res_person[0].kind == "person"
    assert res_person[0].title == "Bob Dupont"

    # Recherche de la commande de déploiement via le projet
    res_cmd = graph.search("commande déploiement projet Alpha", db_path=db)
    assert any(r.kind == "command" and "docker-compose" in r.content for r in res_cmd)


# ════════════════════════════════════════════════════════════════════════════
# 4. Ingestion de Projet & Commandes explicites
# ════════════════════════════════════════════════════════════════════════════

def test_ingest_project_directory(tmp_path):
    db = tmp_path / "brain_proj.db"
    proj_dir = tmp_path / "mon_super_projet"
    proj_dir.mkdir()
    (proj_dir / "README.md").write_text("# Super Projet\nArchitecture micro-services.", encoding="utf-8")
    (proj_dir / "Cargo.toml").write_text('[package]\nname = "super_projet"', encoding="utf-8")

    info = graph.ingest_project(proj_dir, db_path=db)
    assert info["project"] == "mon_super_projet"
    assert "Rust" in info["technologies"]

    # Le projet doit être trouvable dans le Second Brain
    res = graph.search("projet micro-services Rust", db_path=db)
    assert any(r.kind == "project" and r.title == "mon_super_projet" for r in res)


def test_generate_mermaid_graph(tmp_path):
    db = tmp_path / "brain_mermaid.db"
    graph.upsert_contact({"id": "alice", "name": "Alice"}, db_path=db)
    graph.record_conversation_turn("Alice travaille sur le projet Helios", "Noté.", db_path=db)

    mermaid = graph.generate_mermaid_graph("Helios", db_path=db)
    assert "graph TD;" in mermaid
    assert "Alice" in mermaid or "Helios" in mermaid


# ════════════════════════════════════════════════════════════════════════════
# 5. Contrôleur d'action Second Brain & Tolérance aux arguments vides
# ════════════════════════════════════════════════════════════════════════════

def test_second_brain_action_dispatcher(tmp_path, monkeypatch):
    monkeypatch.setattr("core.knowledge_graph._default_db_path", lambda: tmp_path / "sb_action.db")

    # 1. Enregistrement d'une commande
    res_save = second_brain_action({
        "action": "save_command",
        "command": "tar -czvf backup.tar.gz /data",
        "title": "Sauvegarde des données",
        "project": "Infra",
    })
    assert "Commande mémorisée" in res_save
    assert "tar -czvf" in res_save

    # 2. Statut
    res_status = second_brain_action({"action": "status"})
    assert "Second Brain opérationnel" in res_status

    # 3. Dictionnaire vide (doit rester safe)
    res_empty = second_brain_action({})
    assert isinstance(res_empty, str)
    assert res_empty
