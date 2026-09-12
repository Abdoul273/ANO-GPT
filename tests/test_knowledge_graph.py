"""Second Brain : persistance locale et recherche associative multi-source."""

from datetime import datetime
from pathlib import Path

import actions.email as email_action
from core import knowledge_graph as graph
from core.file_indexer import PersonalFileIndexer


def test_retrouve_une_commande_du_mois_dernier(tmp_path):
    db = tmp_path / "brain.db"
    graph.upsert_source(
        kind="conversation", external_id="tour-juillet",
        title="Compression des vidéos du projet Orion",
        content=("On avait retenu cette commande : "
                 "`ffmpeg -i entree.mp4 -c:v libx264 -crf 28 sortie.mp4`"),
        happened="2026-07-18", db_path=db,
    )
    graph.upsert_source(
        kind="conversation", external_id="tour-aout",
        title="Autre compression vidéo",
        content="`ffmpeg -i neuf.mp4 -crf 22 neuf-compresse.mp4`",
        happened="2026-08-20", db_path=db,
    )

    rows = graph.search(
        "retrouve la commande du mois dernier pour compresser les vidéos",
        db_path=db, now=datetime(2026, 8, 28), limit=8,
    )

    commands = [row for row in rows if row.kind == "command"]
    assert commands
    assert "crf 28" in commands[0].content
    assert all("crf 22" not in row.content for row in commands)


def test_relations_projet_contact_sont_traversees(tmp_path):
    db = tmp_path / "brain.db"
    graph.upsert_contact(
        {"id": "alice", "name": "Alice Martin", "aliases": ["Alice"]},
        db_path=db,
    )
    graph.record_conversation_turn(
        "Qui gère le projet Orion ?",
        "Le contact pour le projet Orion est Alice Martin.",
        happened="2026-08-10", db_path=db,
    )

    rows = graph.search("c'était qui le contact pour le projet Orion ?", db_path=db)

    assert rows[0].kind == "person"
    assert rows[0].title == "Alice Martin"
    assert any(row.kind == "project" and row.title == "Orion" for row in rows)


def test_nom_de_projet_dune_lettre_reste_recherchable(tmp_path):
    db = tmp_path / "brain.db"
    graph.upsert_contact({"id": "zoe", "name": "Zoé"}, db_path=db)
    graph.record_conversation_turn(
        "Projet X", "Le contact du projet X est Zoé.", db_path=db,
    )

    rows = graph.search("qui était le contact du projet X ?", db_path=db)
    assert rows[0].kind == "person"
    assert rows[0].title == "Zoé"


def test_indexeur_de_fichiers_alimente_le_meme_graphe(tmp_path):
    db = tmp_path / "brain.db"
    index = PersonalFileIndexer(
        db_path=tmp_path / "files.db", graph_db_path=db,
    )
    note = tmp_path / "Projet-Nebula" / "compression.md"
    note.parent.mkdir()
    note.write_text(
        "# Encodage\nCommande finale: `ffmpeg -i source.mov -crf 30 archive.mp4`",
        encoding="utf-8",
    )

    assert index.index_file(note) is True
    rows = graph.search("commande compression projet Nebula", db_path=db)

    assert any(row.kind == "file" and Path(row.path) == note for row in rows)
    assert any(row.kind == "command" and "crf 30" in row.content for row in rows)


def test_emails_consultes_sont_indexes_sans_dependre_de_la_session(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        graph, "upsert_source", lambda **kwargs: calls.append(kwargs) or 1,
    )

    email_action._remember_results(None, [{
        "id": "gmail-42", "thread_id": "thread-7",
        "sender": "Alice <alice@example.com>",
        "to": "Anonymous <ano@example.com>",
        "subject": "Projet Orion", "snippet": "Le devis est prêt.",
        "date": "Mon, 17 Aug 2026 10:00:00 +0000",
    }])

    assert len(calls) == 1
    assert calls[0]["kind"] == "email"
    assert calls[0]["external_id"] == "gmail-42"
    assert "devis" in calls[0]["content"]


def test_suppression_dune_source_supprime_ses_relations(tmp_path):
    db = tmp_path / "brain.db"
    graph.upsert_source(
        kind="file", external_id="/tmp/note.md", title="Projet Atlas",
        content="`git status`", project="Atlas", db_path=db,
    )
    before = graph.status(db_path=db)
    assert before["relations"] > 0

    assert graph.remove_source("file", "/tmp/note.md", db_path=db) is True

    rows = graph.search("note Atlas git status", db_path=db)
    assert all(row.kind != "file" for row in rows)
