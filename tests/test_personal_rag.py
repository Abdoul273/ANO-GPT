"""tests/test_personal_rag.py — Tests unitaires et d'intégration pour core/personal_rag.py.

Couvre :
1. Extracteurs de code Tree-sitter (Python, JS, TS, Bash, Rust).
2. Extracteur Markdown avec fil d'Ariane récursif et préservation des lignes.
3. Règles d'exclusion automatique (.git/, node_modules/, __pycache__/, venv/, etc.).
4. Hachage SHA256 et indexation incrémentale par blocs modifiés.
5. Base vectorielle locale sqlite-vec et recherche kNN.
6. Surveillance watchdog avec dérebond en tâche de fond.
7. Outil Gemini Live search_personal_docs avec liens cliquables 'file:///...' et numéros de lignes.
"""

import time
from pathlib import Path
import pytest

from core.personal_rag import (
    MarkdownDocExtractor,
    PersonalRAG,
    TextDocExtractor,
    TreeSitterCodeExtractor,
    search_personal_docs,
    should_ignore_path,
)


@pytest.fixture
def temp_rag(tmp_path):
    """Fixture créant une instance isolée de PersonalRAG."""
    db_file = tmp_path / "rag_test.db"
    rag = PersonalRAG(db_path=db_file, roots=[tmp_path])
    yield rag
    rag.stop_watcher()


# ══════════════════════════════════════════════════════════════════════════════
# 1. TESTS DES EXTRACTEURS DE CODE TREE-SITTER
# ══════════════════════════════════════════════════════════════════════════════

def test_tree_sitter_python_extractor(tmp_path):
    py_file = tmp_path / "service.py"
    py_file.write_text(
        '"""Module d\'orchestration."""\n'
        '\n'
        '@custom_decorator\n'
        'def launch_worker(worker_id: int) -> bool:\n'
        '    return worker_id > 0\n'
        '\n'
        'class TaskManager:\n'
        '    """Gestionnaire principal."""\n'
        '    def __init__(self, name: str):\n'
        '        self.name = name\n'
        '\n'
        '    def execute(self) -> str:\n'
        '        return "done"\n'
        '\n'
        'def standalone_cleanup():\n'
        '    pass\n',
        encoding="utf-8",
    )

    extractor = TreeSitterCodeExtractor()
    chunks = extractor.extract_chunks(py_file)

    symbols = [c.symbol_name for c in chunks]
    assert "service.py" in symbols  # module docstring
    assert "launch_worker" in symbols
    assert "TaskManager" in symbols
    assert "TaskManager.__init__" in symbols
    assert "TaskManager.execute" in symbols
    assert "standalone_cleanup" in symbols

    # Vérification des numéros de lignes (1-indexés)
    method_chunk = next(c for c in chunks if c.symbol_name == "TaskManager.execute")
    assert method_chunk.start_line == 12
    assert method_chunk.end_line == 13
    assert "def execute" in method_chunk.content
    assert method_chunk.breadcrumb == "service.py > class TaskManager > def execute"
    assert method_chunk.chunk_hash != ""


def test_tree_sitter_js_ts_extractor(tmp_path):
    ts_file = tmp_path / "auth.ts"
    ts_file.write_text(
        'export interface UserProfile {\n'
        '    id: string;\n'
        '    email: string;\n'
        '}\n'
        '\n'
        'export function authenticate(token: string): boolean {\n'
        '    return token.length > 10;\n'
        '}\n'
        '\n'
        'export class AuthService {\n'
        '    validate() {\n'
        '        return true;\n'
        '    }\n'
        '}\n'
        '\n'
        'export const logoutUser = () => {\n'
        '    console.log("Logged out");\n'
        '};\n',
        encoding="utf-8",
    )

    extractor = TreeSitterCodeExtractor()
    chunks = extractor.extract_chunks(ts_file)

    symbols = [c.symbol_name for c in chunks]
    assert "UserProfile" in symbols
    assert "authenticate" in symbols
    assert "AuthService" in symbols
    assert "AuthService.validate" in symbols
    assert "logoutUser" in symbols


def test_tree_sitter_bash_extractor(tmp_path):
    sh_file = tmp_path / "deploy.sh"
    sh_file.write_text(
        '#!/usr/bin/env bash\n'
        '\n'
        'setup_database() {\n'
        '    echo "Initialisation DB"\n'
        '    systemctl start postgresql\n'
        '}\n'
        '\n'
        'run_migrations() {\n'
        '    ./migrate.sh up\n'
        '}\n',
        encoding="utf-8",
    )

    extractor = TreeSitterCodeExtractor()
    chunks = extractor.extract_chunks(sh_file)

    symbols = [c.symbol_name for c in chunks]
    assert "setup_database" in symbols
    assert "run_migrations" in symbols

    chunk = next(c for c in chunks if c.symbol_name == "setup_database")
    assert chunk.start_line == 3
    assert chunk.end_line == 6
    assert chunk.breadcrumb == "deploy.sh > setup_database()"


def test_tree_sitter_rust_extractor(tmp_path):
    rs_file = tmp_path / "engine.rs"
    rs_file.write_text(
        'pub struct AudioBuffer {\n'
        '    pub capacity: usize,\n'
        '}\n'
        '\n'
        'impl AudioBuffer {\n'
        '    pub fn new(capacity: usize) -> Self {\n'
        '        Self { capacity }\n'
        '    }\n'
        '}\n'
        '\n'
        'pub fn calculate_rms(samples: &[f32]) -> f32 {\n'
        '    0.5\n'
        '}\n',
        encoding="utf-8",
    )

    extractor = TreeSitterCodeExtractor()
    chunks = extractor.extract_chunks(rs_file)

    symbols = [c.symbol_name for c in chunks]
    assert "AudioBuffer" in symbols
    assert "AudioBuffer::new" in symbols
    assert "calculate_rms" in symbols


# ══════════════════════════════════════════════════════════════════════════════
# 2. TESTS DES DOCUMENTS (MARKDOWN BREADCRUMBS & TEXT)
# ══════════════════════════════════════════════════════════════════════════════

def test_markdown_breadcrumb_hierarchy(tmp_path):
    md_file = tmp_path / "architecture.md"
    md_file.write_text(
        '# ANO-GPT Documentation\n'
        'Présentation générale du système d\'assistance.\n'
        '\n'
        '## Architecture Core\n'
        'Détail des modules centraux.\n'
        '\n'
        '### Moteur Audio\n'
        'Gestion half-duplex et suppression de bruit.\n'
        '\n'
        '### Base Vectorielle\n'
        'Indexation sqlite-vec et découpage Tree-sitter.\n'
        '\n'
        '## Protocoles Externes\n'
        'Communication avec MCP et Hyprland.\n',
        encoding="utf-8",
    )

    extractor = MarkdownDocExtractor()
    chunks = extractor.extract_chunks(md_file)

    assert len(chunks) == 5

    # Vérification des fils d'Ariane hiérarchiques
    c_audio = next(c for c in chunks if c.symbol_name == "Moteur Audio")
    assert c_audio.breadcrumb == "architecture.md > ANO-GPT Documentation > Architecture Core > Moteur Audio"
    assert "half-duplex" in c_audio.content
    assert c_audio.start_line == 7
    assert c_audio.end_line == 9

    c_vec = next(c for c in chunks if c.symbol_name == "Base Vectorielle")
    assert c_vec.breadcrumb == "architecture.md > ANO-GPT Documentation > Architecture Core > Base Vectorielle"

    c_proto = next(c for c in chunks if c.symbol_name == "Protocoles Externes")
    assert c_proto.breadcrumb == "architecture.md > ANO-GPT Documentation > Protocoles Externes"


def test_text_doc_extractor(tmp_path):
    txt_file = tmp_path / "notes.txt"
    txt_file.write_text(
        "Premier paragraphe avec des idées de conception.\n"
        "Suite du premier paragraphe.\n"
        "\n"
        "Deuxième paragraphe traitant de la performance CPU sur 2 cœurs.\n"
        "Fin du deuxième paragraphe.\n",
        encoding="utf-8",
    )

    extractor = TextDocExtractor()
    chunks = extractor.extract_chunks(txt_file)
    assert len(chunks) == 2
    assert "idées de conception" in chunks[0].content
    assert "performance CPU" in chunks[1].content
    assert chunks[0].start_line == 1
    assert chunks[1].start_line == 4


# ══════════════════════════════════════════════════════════════════════════════
# 3. TESTS DU FILTRAGE ET EXCLUSION AUTOMATIQUE
# ══════════════════════════════════════════════════════════════════════════════

def test_ignored_directories_and_files():
    assert should_ignore_path(Path("/home/user/project/.git/config")) is True
    assert should_ignore_path(Path("/home/user/project/node_modules/pkg/index.js")) is True
    assert should_ignore_path(Path("/home/user/project/__pycache__/module.cpython-314.pyc")) is True
    assert should_ignore_path(Path("/home/user/project/venv/bin/activate")) is True
    assert should_ignore_path(Path("/home/user/project/target/debug/app")) is True
    assert should_ignore_path(Path("/home/user/project/app.pyc")) is True
    assert should_ignore_path(Path("/home/user/project/model.whl")) is True
    assert should_ignore_path(Path("/home/user/project/image.png")) is True

    # Fichiers légitimes à indexer
    assert should_ignore_path(Path("/home/user/project/main.py")) is False
    assert should_ignore_path(Path("/home/user/project/core/engine.ts")) is False
    assert should_ignore_path(Path("/home/user/project/docs/README.md")) is False


# ══════════════════════════════════════════════════════════════════════════════
# 4. TESTS DU HACHAGE SHA256 & INDEXATION INCRÉMENTALE PAR BLOCS
# ══════════════════════════════════════════════════════════════════════════════

def test_incremental_indexing_block_level(temp_rag, tmp_path):
    py_file = tmp_path / "calculator.py"
    py_file.write_text(
        'def add(a: int, b: int) -> int:\n'
        '    return a + b\n'
        '\n'
        'def multiply(a: int, b: int) -> int:\n'
        '    return a * b\n',
        encoding="utf-8",
    )

    # 1. Première indexation : nouveau fichier -> 2 blocs créés
    modified, added, reused = temp_rag.index_file(py_file)
    assert modified is True
    assert added == 2
    assert reused == 0
    assert temp_rag.storage.count_chunks() == 2

    # 2. Deuxième appel sans modification -> fichier ignoré
    modified2, added2, reused2 = temp_rag.index_file(py_file)
    assert modified2 is False
    assert added2 == 0
    assert reused2 == 2

    # 3. Modification d'UNE SEULE fonction sur les deux (add reste inchangé, multiply est modifié)
    time.sleep(0.05)
    py_file.write_text(
        'def add(a: int, b: int) -> int:\n'
        '    return a + b\n'
        '\n'
        'def multiply(a: int, b: int) -> int:\n'
        '    # Optimisation avec log\n'
        '    print("Calcul produit")\n'
        '    return a * b\n',
        encoding="utf-8",
    )

    modified3, added3, reused3 = temp_rag.index_file(py_file)
    assert modified3 is True
    # EXACTEMENT 1 bloc ré-indexé, 1 bloc réutilisé sans recalcul !
    assert added3 == 1
    assert reused3 == 1
    assert temp_rag.storage.count_chunks() == 2


def test_file_removal(temp_rag, tmp_path):
    file_to_del = tmp_path / "temp_script.sh"
    file_to_del.write_text("clean_tmp() { rm -rf /tmp/demo; }", encoding="utf-8")

    temp_rag.index_file(file_to_del)
    assert temp_rag.storage.count_indexed_files() == 1
    assert temp_rag.storage.count_chunks() == 1

    temp_rag.remove_file(file_to_del)
    assert temp_rag.storage.count_indexed_files() == 0
    assert temp_rag.storage.count_chunks() == 0


# ══════════════════════════════════════════════════════════════════════════════
# 5. TESTS DE RECHERCHE VECTORIELLE & HYBRIDE AVEC SQLITE-VEC
# ══════════════════════════════════════════════════════════════════════════════

def test_vector_and_hybrid_search(temp_rag, tmp_path):
    f_audio = tmp_path / "sound.py"
    f_audio.write_text(
        'def process_audio_stream(input_buffer: bytes) -> bytes:\n'
        '    """Traite le flux microphone avec suppression de bruit Speex."""\n'
        '    return input_buffer\n',
        encoding="utf-8",
    )

    f_db = tmp_path / "database.py"
    f_db.write_text(
        'def connect_sqlite(db_path: str):\n'
        '    """Ouvre la base de données relationnelle locale avec SQLite WAL."""\n'
        '    pass\n',
        encoding="utf-8",
    )

    temp_rag.index_file(f_audio)
    temp_rag.index_file(f_db)

    # Recherche vectorielle / hybride pour audio
    results_audio = temp_rag.search("suppression de bruit microphone audio")
    assert len(results_audio) >= 1
    top_audio = results_audio[0]
    assert top_audio.filename == "sound.py"
    assert top_audio.symbol_name == "process_audio_stream"
    assert "file://" in top_audio.link
    assert top_audio.start_line == 1

    # Recherche vectorielle / hybride pour base sqlite
    results_db = temp_rag.search("connexion base sqlite wal")
    assert len(results_db) >= 1
    assert results_db[0].filename == "database.py"
    assert results_db[0].symbol_name == "connect_sqlite"

    # Filtrage par file_pattern
    results_filtered = temp_rag.search("audio", file_pattern="*database*")
    assert len(results_filtered) == 0


# ══════════════════════════════════════════════════════════════════════════════
# 6. TEST DE L'OUTIL GEMINI LIVE search_personal_docs
# ══════════════════════════════════════════════════════════════════════════════

def test_search_personal_docs_formatting(temp_rag, tmp_path, monkeypatch):
    test_code = tmp_path / "controller.py"
    test_code.write_text(
        'class SystemController:\n'
        '    def reboot_system(self):\n'
        '        """Redémarre le système proprement."""\n'
        '        pass\n',
        encoding="utf-8",
    )
    temp_rag.index_file(test_code)

    # Monkeypatch pour que l'outil global utilise notre temp_rag
    import core.personal_rag as rag_module
    monkeypatch.setattr(rag_module, "_GLOBAL_RAG", temp_rag)

    output = search_personal_docs(query="reboot_system redémarrer", max_results=3)

    # Doit contenir des liens cliquables 'file:///'
    assert "file:///" in output
    assert "controller.py" in output
    assert "lignes" in output
    assert "SystemController.reboot_system" in output
    assert "```python" in output
    assert "Redémarre le système proprement" in output


# ══════════════════════════════════════════════════════════════════════════════
# 7. TEST DE LA SURVEILLANCE WATCHDOG EN ARRIÈRE-PLAN
# ══════════════════════════════════════════════════════════════════════════════

def test_watchdog_background_monitoring(temp_rag, tmp_path):
    temp_rag.roots = [tmp_path]
    started = temp_rag.start_watcher()
    assert started is True

    # Création d'un nouveau fichier surveillé
    new_doc = tmp_path / "live_watch_test.py"
    new_doc.write_text("def live_tested_function():\n    return 42\n", encoding="utf-8")

    # Attente du délai de dérebond (0.5s + marge)
    time.sleep(1.2)

    # Vérification que le fichier a bien été indexé automatiquement
    assert temp_rag.storage.count_indexed_files() >= 1
    res = temp_rag.search("live_tested_function")
    assert len(res) >= 1
    assert res[0].symbol_name == "live_tested_function"

    temp_rag.stop_watcher()
