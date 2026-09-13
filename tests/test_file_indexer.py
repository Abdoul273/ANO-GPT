"""Tests pour l'index personnel de fichiers FTS5 (core/file_indexer.py)."""

import time
import pytest

from core.file_indexer import PersonalFileIndexer


@pytest.fixture
def temp_indexer(tmp_path):
    db_file = tmp_path / "test_index.db"
    return PersonalFileIndexer(db_path=db_file)


def test_index_and_search_by_filename_and_content(temp_indexer, tmp_path):
    # Création de fichiers de test
    doc1 = tmp_path / "contrat_assurance_habitation.txt"
    doc1.write_text("Voici le document officiel de l'assurance pour le logement.", encoding="utf-8")

    doc2 = tmp_path / "video_converter_ffmpeg.py"
    doc2.write_text("import subprocess\nsubprocess.run(['ffmpeg', '-i', 'input.mp4', 'output.mkv'])", encoding="utf-8")

    doc3 = tmp_path / "notes_vacances.md"
    doc3.write_text("# Voyage en Italie\nPlage, soleil et visites culturelles.", encoding="utf-8")

    # Indexation
    assert temp_indexer.index_file(doc1) is True
    assert temp_indexer.index_file(doc2) is True
    assert temp_indexer.index_file(doc3) is True
    assert temp_indexer.count_indexed_files() == 3

    # Recherche par contenu : "assurance"
    results_assurance = temp_indexer.search("assurance")
    assert len(results_assurance) >= 1
    assert results_assurance[0].filename == "contrat_assurance_habitation.txt"
    assert "assurance" in results_assurance[0].snippet.lower()

    # Recherche par contenu : "ffmpeg"
    results_ffmpeg = temp_indexer.search("ffmpeg")
    assert len(results_ffmpeg) >= 1
    assert results_ffmpeg[0].filename == "video_converter_ffmpeg.py"
    assert "ffmpeg" in results_ffmpeg[0].snippet.lower()

    # Recherche par nom de fichier : "vacances"
    results_vacances = temp_indexer.search("vacances")
    assert len(results_vacances) >= 1
    assert results_vacances[0].filename == "notes_vacances.md"


def test_incremental_indexing(temp_indexer, tmp_path):
    f = tmp_path / "script.sh"
    f.write_text("echo 'hello'", encoding="utf-8")

    # Première indexation : nouveau fichier -> True
    assert temp_indexer.index_file(f) is True

    # Deuxième indexation sans modification -> False (déjà à jour)
    assert temp_indexer.index_file(f) is False

    # Modification du fichier -> True
    time.sleep(0.05)
    f.write_text("echo 'hello world modified'", encoding="utf-8")
    assert temp_indexer.index_file(f) is True


def test_remove_file(temp_indexer, tmp_path):
    f = tmp_path / "tmp.txt"
    f.write_text("temporaire", encoding="utf-8")
    temp_indexer.index_file(f)
    assert temp_indexer.count_indexed_files() == 1

    temp_indexer.remove_file(f)
    assert temp_indexer.count_indexed_files() == 0
    assert temp_indexer.search("temporaire") == []
