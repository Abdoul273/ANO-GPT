"""core/file_indexer.py — Index personnel de fichiers (Nom + Plein Texte FTS5).

Indexation incrémentale et recherche plein texte ultra-rapide sur les dossiers
personnels de l'utilisateur (~/Documents, ~/development, ~/Bureau).

Points forts :
- Base SQLite locale avec extension FTS5 native (tolérance accents / unicode61).
- Extraction intelligente du contenu texte (.py, .md, .sh, .txt, .json, .pdf...).
- Indexation incrémentale basée sur mtime & taille pour un coût CPU quasi nul.
- Recherche FTS5 instantanée avec snippets contextuels pour retrouver :
  « le PDF de l'assurance », « ce script où je faisais du ffmpeg », etc.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence

logger = logging.getLogger("anogpt.file_indexer")

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "memory" / "personal_index.db"

DEFAULT_INDEX_ROOTS = [
    Path.home() / "Documents",
    Path.home() / "development",
    Path.home() / "dev",
    Path.home() / "Notes",
    Path.home() / "Bureau",
    Path.home() / "Desktop",
]

TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".rst", ".py", ".sh", ".bash", ".zsh",
    ".js", ".ts", ".jsx", ".tsx", ".html", ".css", ".json", ".yaml",
    ".yml", ".toml", ".ini", ".conf", ".cfg", ".csv", ".sql", ".log",
    ".c", ".cpp", ".h", ".hpp", ".rs", ".go", ".dart", ".java", ".kt",
}

MAX_TEXT_SIZE_BYTES = 128 * 1024  # 128 Ko max par fichier pour l'index de contenu
MAX_SNIPPET_LENGTH = 160


@dataclass
class SearchResult:
    path: str
    filename: str
    size: int
    mtime: float
    snippet: str
    rank: float


class PersonalFileIndexer:
    """Gestionnaire de l'index SQLite FTS5 personnel."""

    def __init__(self, db_path: Optional[Path] = None,
                 graph_db_path: Optional[Path] = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        # Un indexeur temporaire de test ne doit jamais écrire dans la mémoire
        # réelle. En production (db_path omis), le graphe partage memory.db.
        self.graph_db_path = graph_db_path if graph_db_path is not None else (
            None if db_path is not None else "default"
        )
        self._lock = threading.Lock()
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        new_database = not self.db_path.exists()
        with self._lock:
            with self._get_connection() as conn:
                if new_database:
                    conn.execute("PRAGMA auto_vacuum=INCREMENTAL;")
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute("PRAGMA synchronous=NORMAL;")
                
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS indexed_files (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        path TEXT UNIQUE NOT NULL,
                        filename TEXT NOT NULL,
                        extension TEXT NOT NULL,
                        size INTEGER NOT NULL,
                        mtime REAL NOT NULL,
                        indexed_at REAL NOT NULL,
                        summary TEXT
                    );
                """)
                
                # Migration : ajout de la colonne summary si absente
                cols = [r[1] for r in conn.execute("PRAGMA table_info(indexed_files)").fetchall()]
                if "summary" not in cols:
                    conn.execute("ALTER TABLE indexed_files ADD COLUMN summary TEXT;")
                    conn.execute("DROP TABLE IF EXISTS files_fts;")

                # Table FTS5 pour recherche plein texte et par résumé sémantique
                conn.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
                        path UNINDEXED,
                        filename,
                        content,
                        summary,
                        tokenize = 'unicode61 remove_diacritics 2'
                    );
                """)

                # Vérifier si files_fts a bien la colonne summary
                cur = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='files_fts'").fetchone()
                if cur and "summary" not in str(cur[0]):
                    conn.execute("DROP TABLE IF EXISTS files_fts;")
                    conn.execute("""
                        CREATE VIRTUAL TABLE files_fts USING fts5(
                            path UNINDEXED,
                            filename,
                            content,
                            summary,
                            tokenize = 'unicode61 remove_diacritics 2'
                        );
                    """)

                conn.commit()

    @staticmethod
    def extract_file_content(path: Path) -> str:
        """Extrait le texte d'un fichier texte ou document."""
        if not path.is_file():
            return ""
        
        ext = path.suffix.lower()
        
        # 1. Fichiers texte brut et code
        if ext in TEXT_EXTENSIONS or not ext:
            try:
                with path.open("r", encoding="utf-8", errors="ignore") as fh:
                    return fh.read(MAX_TEXT_SIZE_BYTES)
            except Exception:
                return ""

        # 2. Fichiers PDF via pdftotext si présent
        if ext == ".pdf" and shutil.which("pdftotext"):
            try:
                r = subprocess.run(
                    ["pdftotext", "-l", "5", str(path), "-"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    check=False,
                )
                if r.returncode == 0 and r.stdout:
                    return r.stdout[:MAX_TEXT_SIZE_BYTES]
            except Exception:
                pass

        return ""

    def index_file(self, path: Path) -> bool:
        """Indexe ou met à jour un fichier dans la base FTS5."""
        try:
            resolved = path.resolve()
            if not resolved.is_file():
                return False
            
            st = resolved.stat()
            size = st.st_size
            mtime = st.st_mtime
            filename = resolved.name
            extension = resolved.suffix.lower()
            str_path = str(resolved)

            with self._lock:
                with self._get_connection() as conn:
                    # Vérifier si déjà à jour
                    cur = conn.execute(
                        "SELECT mtime, size FROM indexed_files WHERE path = ?", (str_path,)
                    )
                    row = cur.fetchone()
                    if row and row["mtime"] == mtime and row["size"] == size:
                        return False  # Déjà à jour

                    content = self.extract_file_content(resolved)
                    from core.semantic_enricher import extract_document_summary
                    summary = extract_document_summary(filename, content)

                    # Mise à jour de la table principale
                    now = time.time()
                    conn.execute("""
                        INSERT INTO indexed_files (path, filename, extension, size, mtime, indexed_at, summary)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(path) DO UPDATE SET
                            filename = excluded.filename,
                            extension = excluded.extension,
                            size = excluded.size,
                            mtime = excluded.mtime,
                            indexed_at = excluded.indexed_at,
                            summary = excluded.summary;
                    """, (str_path, filename, extension, size, mtime, now, summary))

                    # Mise à jour de la table FTS5
                    conn.execute("DELETE FROM files_fts WHERE path = ?", (str_path,))
                    conn.execute("""
                        INSERT INTO files_fts (path, filename, content, summary)
                        VALUES (?, ?, ?, ?);
                    """, (str_path, filename, content, summary))

                    conn.commit()
            if self.graph_db_path:
                try:
                    from core.knowledge_graph import upsert_source

                    graph_path = None if self.graph_db_path == "default" else self.graph_db_path
                    upsert_source(
                        kind="file", external_id=str_path, title=filename,
                        content=f"{summary}\n{content[:MAX_TEXT_SIZE_BYTES]}",
                        path=str_path,
                        happened=datetime.fromtimestamp(mtime).strftime("%Y-%m-%d"),
                        project=resolved.parent.name,
                        db_path=graph_path,
                    )
                except Exception as exc:
                    logger.debug("Liaison Second Brain ignorée pour %s: %s", resolved, exc)
            return True
        except Exception as exc:
            logger.debug("Échec indexation %s: %s", path, exc)
            return False

    def remove_file(self, path: Path | str) -> bool:
        """Supprime un fichier de l'index."""
        str_path = str(Path(path).resolve())
        with self._lock:
            with self._get_connection() as conn:
                conn.execute("DELETE FROM indexed_files WHERE path = ?", (str_path,))
                conn.execute("DELETE FROM files_fts WHERE path = ?", (str_path,))
                conn.commit()
        if self.graph_db_path:
            try:
                from core.knowledge_graph import remove_source

                graph_path = None if self.graph_db_path == "default" else self.graph_db_path
                remove_source("file", str_path, db_path=graph_path)
            except Exception:
                pass
        return True

    def scan_directory_incremental(
        self,
        roots: Optional[Sequence[Path]] = None,
        max_files: int = 2000,
    ) -> Dict[str, int]:
        """Parcourt les répertoires cibles et indexe les fichiers modifiés."""
        target_roots = [r for r in (roots or DEFAULT_INDEX_ROOTS) if r.exists() and r.is_dir()]
        stats = {"scanned": 0, "indexed": 0, "errors": 0}

        for root in target_roots:
            try:
                for dirpath, dirnames, filenames in os.walk(root):
                    # Ignorer les dossiers cachés lourds (.git, node_modules, .cache)
                    dirnames[:] = [
                        d for d in dirnames
                        if not d.startswith(".") and d not in {"node_modules", "vendor", "__pycache__", "build", "dist"}
                    ]
                    for fname in filenames:
                        if fname.startswith("."):
                            continue
                        stats["scanned"] += 1
                        file_path = Path(dirpath) / fname
                        try:
                            if self.index_file(file_path):
                                stats["indexed"] += 1
                        except Exception:
                            stats["errors"] += 1

                        if stats["scanned"] >= max_files:
                            return stats
            except Exception as exc:
                logger.warning("Erreur scan répertoire %s : %s", root, exc)
                stats["errors"] += 1

        return stats

    def search(
        self,
        query: str,
        limit: int = 15,
    ) -> List[SearchResult]:
        """Recherche plein texte dans les noms de fichiers, le contenu et les résumés conceptuels."""
        cleaned_query = query.strip()
        if not cleaned_query:
            return []

        # Construction d'une requête FTS5 sécurisée
        # Nettoyage des caractères spéciaux FTS5
        words = [w for w in re.sub(r'[^\w\s-]', ' ', cleaned_query).split() if len(w) >= 2]
        if not words:
            return []

        fts_pattern = " OR ".join(f'"{w}"*' for w in words)

        results: List[SearchResult] = []
        with self._lock:
            with self._get_connection() as conn:
                cur = conn.execute("""
                    SELECT 
                        f.path,
                        f.filename,
                        f.size,
                        f.mtime,
                        snippet(files_fts, 2, '<b>', '</b>', '...', 12) AS snippet,
                        bm25(files_fts, 10.0, 1.0, 4.0) AS rank
                    FROM files_fts
                    JOIN indexed_files f ON f.path = files_fts.path
                    WHERE files_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?;
                """, (fts_pattern, limit))

                for row in cur.fetchall():
                    results.append(SearchResult(
                        path=row["path"],
                        filename=row["filename"],
                        size=row["size"],
                        mtime=row["mtime"],
                        snippet=row["snippet"] or "",
                        rank=float(row["rank"]),
                    ))

        return results

    def count_indexed_files(self) -> int:
        """Retourne le nombre total de fichiers indexés."""
        with self._lock:
            with self._get_connection() as conn:
                cur = conn.execute("SELECT COUNT(*) AS total FROM indexed_files")
                row = cur.fetchone()
                return row["total"] if row else 0


# Instance globale
_global_file_indexer: Optional[PersonalFileIndexer] = None
_indexer_lock = threading.Lock()


def get_file_indexer() -> PersonalFileIndexer:
    global _global_file_indexer
    with _indexer_lock:
        if _global_file_indexer is None:
            _global_file_indexer = PersonalFileIndexer()
        return _global_file_indexer


def search_personal_files(query: str, limit: int = 10) -> List[SearchResult]:
    """Recherche rapide dans l'index personnel."""
    return get_file_indexer().search(query, limit=limit)
