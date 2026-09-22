"""core/personal_rag.py — RAG personnel haute précision pour code source et documents.

Fournit :
1. Extracteurs de contenu intelligents :
   - Code source (.py, .js, .ts, .sh, .rs) : découpage syntaxique par fonctions/classes/méthodes
     via Tree-sitter avec fil d'Ariane et numéros de lignes exacts.
   - Documents (.md, .txt, .pdf) : découpage récursif par titres et paragraphes avec
     conservation de la hiérarchie du fil d'Ariane.
   - Exclusion automatique (.git/, node_modules/, __pycache__/, venv/, etc.).
2. Indexation incrémentale en arrière-plan :
   - Surveillance des modifications de fichiers en temps réel via `watchdog`.
   - Hachage SHA256 des fichiers et des blocs pour ne ré-indexer QUE les blocs modifiés.
   - Stockage et recherche vectorielle locale via `sqlite-vec` + recherche plein texte FTS5.
3. Outil Gemini Live & MCP 'search_personal_docs' :
   - query (str), file_pattern (str, optionnel), max_results (int).
   - Retourne les extraits exacts avec liens cliquables 'file:///...' et numéros de lignes.
"""

from __future__ import annotations

import array
import ast
import fnmatch
import hashlib
import logging
import math
import os
import re
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# Bibliothèque sqlite-vec
try:
    import sqlite_vec
    HAS_SQLITE_VEC = True
except ImportError:
    sqlite_vec = None
    HAS_SQLITE_VEC = False

# Bibliothèque watchdog
try:
    from watchdog.events import (
        FileMovedEvent,
        FileSystemEvent,
        FileSystemEventHandler,
    )
    from watchdog.observers import Observer
    HAS_WATCHDOG = True
except ImportError:
    HAS_WATCHDOG = False

# Bibliothèque tree-sitter. Ne même pas charger ses extensions binaires sous
# CPython 3.14 : leurs wheels actuels ont provoqué des SIGSEGV reproductibles.
if sys.version_info >= (3, 14):
    HAS_TREE_SITTER = False
else:
    try:
        from tree_sitter import Language, Node, Parser
        import tree_sitter_python as tspython
        import tree_sitter_javascript as tsjavascript
        import tree_sitter_typescript as tstypescript
        import tree_sitter_bash as tsbash
        import tree_sitter_rust as tsrust
        HAS_TREE_SITTER = True
    except ImportError:
        HAS_TREE_SITTER = False

# Extraction PDF
try:
    import pypdf
    HAS_PYPDF = True
except ImportError:
    HAS_PYPDF = False

# Intégration décorateur d'outil ANO-GPT
try:
    from core.tool_registry import tool
except ImportError:
    def tool(**kwargs):
        def decorator(f):
            return f
        return decorator

logger = logging.getLogger("anogpt.personal_rag")

# ══════════════════════════════════════════════════════════════════════════════
# 1. CONSTANTES & CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "memory" / "personal_rag.db"
DEFAULT_EMBEDDING_DIM = 384
MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024  # 5 Mo max par fichier

def _default_index_roots() -> list[Path]:
    """Racines sûres pour l'indexation automatique.

    L'index vectoriel calcule des empreintes et des embeddings : parcourir tous
    les dépôts de développement dès le démarrage monopolise facilement les
    deux coeurs d'une petite machine. Les documents restent utiles par défaut;
    les arbres de code sont un choix explicite via
    ``ANOGPT_PERSONAL_RAG_CODE_ROOTS`` (chemins séparés par ``os.pathsep``).
    """
    roots = [Path.home() / "Documents"]
    configured = os.environ.get("ANOGPT_PERSONAL_RAG_CODE_ROOTS", "")
    if configured:
        roots.extend(Path(value).expanduser() for value in configured.split(os.pathsep) if value)
    return roots


DEFAULT_INDEX_ROOTS = _default_index_roots()

IGNORED_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    "venv",
    ".venv",
    ".env",
    "env",
    "dist",
    "build",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".cache",
    "target",
    ".idea",
    ".vscode",
    ".tox",
    ".next",
    ".nuxt",
    ".turbo",
    ".gemini",
    ".gradle",
}

IGNORED_EXTENSIONS = {
    ".pyc", ".pyo", ".pyd", ".o", ".obj", ".so", ".a", ".dll", ".dylib",
    ".exe", ".bin", ".whl", ".tar", ".gz", ".tgz", ".zip", ".bz2", ".xz",
    ".7z", ".rar", ".iso", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico",
    ".svg", ".mp3", ".wav", ".ogg", ".flac", ".m4a", ".mp4", ".mkv", ".avi",
    ".mov", ".lock", ".log", ".tmp", ".swp", ".bak",
}

SUPPORTED_CODE_EXTENSIONS = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    ".rs": "rust",
}

SUPPORTED_DOC_EXTENSIONS = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".txt": "text",
    ".text": "text",
    ".rst": "text",
    ".pdf": "pdf",
}

SUPPORTED_EXTENSIONS = set(SUPPORTED_CODE_EXTENSIONS.keys()) | set(SUPPORTED_DOC_EXTENSIONS.keys())


def should_ignore_path(path: Path | str) -> bool:
    """Vérifie si un chemin ou un de ses répertoires parents doit être ignoré."""
    p = Path(path)
    # Vérification de chaque composant du chemin
    for part in p.parts:
        if part in IGNORED_DIRS:
            return True
        if part.startswith(".") and part not in {".", ".."}:
            # Dossiers cachés (sauf racine courante)
            return True
    # Vérification extension
    if p.suffix.lower() in IGNORED_EXTENSIONS:
        return True
    return False


# ══════════════════════════════════════════════════════════════════════════════
# 2. STRUCTURES DE DONNÉES (CHUNKS & RÉSULTATS)
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class CodeChunk:
    """Représente un bloc sémantique ou syntaxique extrait d'un fichier."""
    file_path: str
    chunk_index: int
    chunk_type: str        # 'function', 'class', 'method', 'struct', 'impl', 'module', 'section', 'paragraph'
    symbol_name: str       # Nom du symbole ou titre de section
    breadcrumb: str        # Fil d'Ariane hiérarchique
    start_line: int        # 1-indexed
    end_line: int          # 1-indexed
    content: str           # Extrait de code ou de texte
    chunk_hash: str = ""   # Empreinte SHA-256 du bloc

    def compute_hash(self) -> str:
        """Calcule l'empreinte SHA256 unique pour détecter les blocs modifiés."""
        h = hashlib.sha256()
        h.update(self.chunk_type.encode("utf-8"))
        h.update(b":")
        h.update(self.symbol_name.encode("utf-8"))
        h.update(b":")
        h.update(self.content.strip().encode("utf-8"))
        self.chunk_hash = h.hexdigest()
        return self.chunk_hash


@dataclass
class RAGSearchResult:
    """Résultat d'une recherche vectorielle / hybride dans les documents."""
    file_path: str
    filename: str
    chunk_type: str
    symbol_name: str
    breadcrumb: str
    start_line: int
    end_line: int
    content: str
    score: float
    distance: float
    link: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "file_path": self.file_path,
            "filename": self.filename,
            "chunk_type": self.chunk_type,
            "symbol_name": self.symbol_name,
            "breadcrumb": self.breadcrumb,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "content": self.content,
            "score": round(self.score, 4),
            "distance": round(self.distance, 4),
            "link": self.link,
        }


@dataclass
class IndexStats:
    """Statistiques d'une opération d'indexation."""
    files_indexed: int = 0
    files_skipped: int = 0
    chunks_created: int = 0
    chunks_reused: int = 0
    chunks_deleted: int = 0
    errors: int = 0


# ══════════════════════════════════════════════════════════════════════════════
# 3. MODÈLE D'EMBEDDINGS LOCAL DÉTERMINISTE & RAPIDE
# ══════════════════════════════════════════════════════════════════════════════

class LocalDenseEmbedder:
    """Modèle d'embedding vectoriel dense (384D) optimisé pour code source et texte.

    Conçu pour tourner en moins de 0.2ms sur CPU 2-cœurs sans charger le GIL audio :
    - Tokenisation bilingue adaptée au code (camelCase, snake_case, symboles, n-grammes).
    - Projection aléatoire signée (SimHash / Feature Hashing) sur 384 dimensions.
    - Normalisation L2 stricte pour correspondre à la similarité cosinus.
    """

    def __init__(self, dim: int = DEFAULT_EMBEDDING_DIM):
        self.dim = dim

    def _tokenize(self, text: str) -> List[str]:
        words = re.findall(r"[A-Za-z0-9_]+", text.lower())
        tokens: List[str] = []
        for w in words:
            tokens.append(w)
            # Découpage snake_case / camelCase
            parts = re.findall(r"[a-z]+|[0-9]+", w)
            if len(parts) > 1:
                tokens.extend(parts)
            # Sous-mots n-grammes (3-grammes) pour tolérance fautes de frappe et suffixes
            if len(w) >= 3:
                for i in range(len(w) - 2):
                    tokens.append(w[i : i + 3])
        return tokens

    def embed(self, text: str) -> List[float]:
        tokens = self._tokenize(text)
        vec = [0.0] * self.dim
        if not tokens:
            return vec

        for t in tokens:
            # Hachage déterministe avec MurmurHash/MD5
            h_int = int(hashlib.md5(t.encode("utf-8")).hexdigest()[:8], 16)
            idx = h_int % self.dim
            # Signe pseudo-aléatoire
            sign = 1.0 if (h_int & 0x10000) else -1.0
            vec[idx] += sign

        # Normalisation euclidienne (L2 norm)
        norm = math.sqrt(sum(x * x for x in vec))
        if norm > 1e-9:
            vec = [x / norm for x in vec]
        return vec


# ══════════════════════════════════════════════════════════════════════════════
# 4. EXTRACTEURS DE CONTENU INTELLIGENTS
# ══════════════════════════════════════════════════════════════════════════════

class TreeSitterCodeExtractor:
    """Découpage syntaxique du code source via Tree-sitter par fonctions et classes."""

    def __init__(self):
        self._parsers: Dict[str, Parser] = {}
        self._parser_lock = threading.RLock()
        # Les wheels tree-sitter installés segfaultent sous CPython 3.14
        # (constaté avec les grammaires Python ET Bash). Aucun try/except ne
        # peut récupérer un SIGSEGV : les extracteurs Python sûrs ci-dessous
        # prennent donc le relais sur cette version.
        if HAS_TREE_SITTER and sys.version_info < (3, 14):
            self._init_parsers()

    def _init_parsers(self) -> None:
        try:
            self._parsers["python"] = Parser(Language(tspython.language()))
            self._parsers["javascript"] = Parser(Language(tsjavascript.language()))
            self._parsers["typescript"] = Parser(Language(tstypescript.language_typescript()))
            self._parsers["bash"] = Parser(Language(tsbash.language()))
            self._parsers["rust"] = Parser(Language(tsrust.language()))
        except Exception as e:
            logger.warning("Erreur initialisation Tree-sitter: %s", e)

    def extract_chunks(self, file_path: Path) -> List[CodeChunk]:
        """Extrait les blocs sémantiques (fonctions, classes, méthodes) du code."""
        ext = file_path.suffix.lower()
        lang = SUPPORTED_CODE_EXTENSIONS.get(ext)
        if lang == "python":
            return self._extract_python_ast(file_path)
        if lang and lang not in self._parsers:
            safe_chunks = self._extract_structural_fallback(file_path, lang)
            if safe_chunks:
                return safe_chunks
        if not lang or lang not in self._parsers:
            return self._fallback_extract(file_path)

        try:
            content_bytes = file_path.read_bytes()
            content_str = content_bytes.decode("utf-8", errors="replace")
        except Exception as e:
            logger.error("Impossible de lire %s: %s", file_path, e)
            raise

        parser = self._parsers[lang]
        chunks: List[CodeChunk] = []
        filename = file_path.name
        try:
            with self._parser_lock:
                tree = parser.parse(content_bytes)
                if lang in ("javascript", "typescript"):
                    self._extract_js_ts_nodes(tree.root_node, content_bytes, content_str, file_path, filename, chunks)
                elif lang == "bash":
                    self._extract_bash_nodes(tree.root_node, content_bytes, content_str, file_path, filename, chunks)
                elif lang == "rust":
                    self._extract_rust_nodes(tree.root_node, content_bytes, content_str, file_path, filename, chunks)
        except Exception as e:
            logger.warning("Échec parsing Tree-sitter sur %s: %s", file_path, e)
            return self._fallback_extract(file_path, content_str)

        # Si aucun bloc syntaxique n'a été détecté (ex: script séquentiel simple), fallback
        if not chunks and content_str.strip():
            return self._fallback_extract(file_path, content_str)

        # Assigner chunk_index et calculer le hash de chaque bloc
        for idx, chunk in enumerate(chunks):
            chunk.chunk_index = idx
            chunk.compute_hash()

        return chunks

    def _extract_python_ast(self, file_path: Path) -> List[CodeChunk]:
        """Extrait Python avec ``ast`` : sûr, natif CPython et thread-safe.

        Tree-sitter reste utile pour les autres langages sur les versions où
        son binding est stable, mais il n'apporte rien qui justifie un SIGSEGV
        pour Python, dont la bibliothèque standard connaît déjà la grammaire.
        """
        try:
            text = file_path.read_text("utf-8", errors="replace")
            tree = ast.parse(text, filename=str(file_path))
        except (OSError, SyntaxError, ValueError):
            return self._fallback_extract(file_path, locals().get("text", ""))

        lines = text.splitlines()
        filename = file_path.name
        chunks: List[CodeChunk] = []

        def bounds(node: ast.AST) -> tuple[int, int]:
            start = int(getattr(node, "lineno", 1) or 1)
            decorators = getattr(node, "decorator_list", ())
            if decorators:
                start = min(start, *(int(getattr(d, "lineno", start) or start) for d in decorators))
            end = int(getattr(node, "end_lineno", start) or start)
            return start, end

        def append_node(node: ast.AST, parent_class: str = "") -> None:
            if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                return
            start, end = bounds(node)
            if isinstance(node, ast.ClassDef):
                chunk_type = "class"
                symbol = node.name
                breadcrumb = f"{filename} > class {node.name}"
            else:
                chunk_type = "method" if parent_class else "function"
                symbol = f"{parent_class}.{node.name}" if parent_class else node.name
                prefix = f"class {parent_class} > " if parent_class else ""
                breadcrumb = f"{filename} > {prefix}def {node.name}"
            chunk = CodeChunk(
                file_path=str(file_path), chunk_index=len(chunks),
                chunk_type=chunk_type, symbol_name=symbol,
                breadcrumb=breadcrumb, start_line=start, end_line=end,
                content=text_from_lines(lines, start, end),
            )
            chunk.compute_hash()
            chunks.append(chunk)
            if isinstance(node, ast.ClassDef):
                for child in node.body:
                    append_node(child, node.name)

        body = list(tree.body)
        if body and isinstance(body[0], ast.Expr):
            value = body[0].value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                start, end = bounds(body[0])
                chunk = CodeChunk(
                    file_path=str(file_path), chunk_index=0,
                    chunk_type="module", symbol_name=filename,
                    breadcrumb=f"{filename} > docstring",
                    start_line=start, end_line=end,
                    content=text_from_lines(lines, start, end),
                )
                chunk.compute_hash()
                chunks.append(chunk)

        for node in body:
            append_node(node)
        return chunks or self._fallback_extract(file_path, text)

    def _extract_structural_fallback(self, file_path: Path, lang: str) -> List[CodeChunk]:
        """Découpage symbolique sans extension native pour CPython 3.14."""
        text = file_path.read_text("utf-8", errors="replace")
        lines = text.splitlines()
        filename = file_path.name
        chunks: List[CodeChunk] = []

        def block_end(start_idx: int) -> int:
            depth = 0
            opened = False
            for idx in range(start_idx, len(lines)):
                # Suffisant pour délimiter les symboles ; les accolades dans
                # les chaînes peuvent élargir un chunk mais jamais crasher.
                depth += lines[idx].count("{") - lines[idx].count("}")
                opened = opened or "{" in lines[idx]
                if opened and depth <= 0:
                    return idx + 1
            return len(lines)

        def add(start_idx: int, end_line: int, kind: str, symbol: str, crumb: str) -> None:
            chunk = CodeChunk(
                file_path=str(file_path), chunk_index=len(chunks),
                chunk_type=kind, symbol_name=symbol,
                breadcrumb=f"{filename} > {crumb}",
                start_line=start_idx + 1, end_line=end_line,
                content=text_from_lines(lines, start_idx + 1, end_line),
            )
            chunk.compute_hash()
            chunks.append(chunk)

        if lang in {"javascript", "typescript"}:
            patterns = (
                (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)"), "function", "function"),
                (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)"), "class", "class"),
                (re.compile(r"^\s*(?:export\s+)?interface\s+([A-Za-z_$][\w$]*)"), "interface", "interface"),
                (re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=.*=>"), "function", "const"),
            )
            for idx, line in enumerate(lines):
                for regex, kind, label in patterns:
                    match = regex.search(line)
                    if match:
                        name = match.group(1)
                        end = block_end(idx)
                        add(idx, end, kind, name, f"{label} {name}")
                        if kind == "class":
                            method_re = re.compile(
                                r"^\s+(?:static\s+)?(?:async\s+)?([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{"
                            )
                            for method_idx in range(idx + 1, max(idx + 1, end - 1)):
                                method_match = method_re.search(lines[method_idx])
                                if method_match:
                                    method = method_match.group(1)
                                    add(
                                        method_idx, block_end(method_idx), "method",
                                        f"{name}.{method}", f"class {name} > {method}()",
                                    )
                        break
        elif lang == "bash":
            regex = re.compile(
                r"^\s*(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*(?:\(\s*\))?\s*\{"
            )
            for idx, line in enumerate(lines):
                match = regex.search(line)
                if match:
                    name = match.group(1)
                    add(idx, block_end(idx), "function", name, f"{name}()")
        elif lang == "rust":
            item_re = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(struct|enum|trait)\s+([A-Za-z_]\w*)")
            fn_re = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)")
            impl_re = re.compile(r"^\s*impl(?:<[^>]+>)?\s+([A-Za-z_]\w*)")
            current_impl = ""
            impl_end = 0
            for idx, line in enumerate(lines):
                impl_match = impl_re.search(line)
                if impl_match:
                    current_impl = impl_match.group(1)
                    impl_end = block_end(idx)
                    continue
                if current_impl and idx + 1 > impl_end:
                    current_impl = ""
                item_match = item_re.search(line)
                if item_match:
                    kind, name = item_match.groups()
                    add(idx, block_end(idx), kind, name, f"{kind} {name}")
                    continue
                fn_match = fn_re.search(line)
                if fn_match:
                    name = fn_match.group(1)
                    symbol = f"{current_impl}::{name}" if current_impl else name
                    crumb = f"impl {current_impl} > fn {name}" if current_impl else f"fn {name}"
                    add(idx, block_end(idx), "method" if current_impl else "function", symbol, crumb)
        return chunks

    def _extract_python_nodes(
        self,
        root: Node,
        raw: bytes,
        text: str,
        path: Path,
        filename: str,
        chunks: List[CodeChunk],
    ) -> None:
        lines = text.splitlines()

        # Docstring ou en-tête de module s'il existe
        first_child = root.children[0] if root.children else None
        if first_child and first_child.type == "expression_statement":
            doc_node = first_child.children[0] if first_child.children else None
            if doc_node and doc_node.type == "string":
                start_l = first_child.start_point.row + 1
                end_l = first_child.end_point.row + 1
                doc_text = text_from_lines(lines, start_l, end_l)
                chunks.append(
                    CodeChunk(
                        file_path=str(path),
                        chunk_index=0,
                        chunk_type="module",
                        symbol_name=filename,
                        breadcrumb=f"{filename} > docstring",
                        start_line=start_l,
                        end_line=end_l,
                        content=doc_text,
                    )
                )

        def walk(node: Node, parent_class: str = ""):
            t = node.type
            target_node = node

            if t == "decorated_definition":
                for child in node.children:
                    if child.type in ("function_definition", "class_definition"):
                        target_node = child
                        t = child.type
                        break

            if t == "class_definition":
                name_node = target_node.child_by_field_name("name")
                cls_name = name_node.text.decode("utf-8") if name_node else "UnknownClass"
                start_l = node.start_point.row + 1
                end_l = node.end_point.row + 1
                breadcrumb = f"{filename} > class {cls_name}"
                snippet = text_from_lines(lines, start_l, end_l)

                chunks.append(
                    CodeChunk(
                        file_path=str(path),
                        chunk_index=len(chunks),
                        chunk_type="class",
                        symbol_name=cls_name,
                        breadcrumb=breadcrumb,
                        start_line=start_l,
                        end_line=end_l,
                        content=snippet,
                    )
                )

                body = target_node.child_by_field_name("body")
                if body:
                    for body_child in body.children:
                        walk(body_child, parent_class=cls_name)

            elif t == "function_definition":
                name_node = target_node.child_by_field_name("name")
                fn_name = name_node.text.decode("utf-8") if name_node else "anonymous"
                start_l = node.start_point.row + 1
                end_l = node.end_point.row + 1

                if parent_class:
                    breadcrumb = f"{filename} > class {parent_class} > def {fn_name}"
                    chunk_type = "method"
                    symbol_name = f"{parent_class}.{fn_name}"
                else:
                    breadcrumb = f"{filename} > def {fn_name}"
                    chunk_type = "function"
                    symbol_name = fn_name

                snippet = text_from_lines(lines, start_l, end_l)
                chunks.append(
                    CodeChunk(
                        file_path=str(path),
                        chunk_index=len(chunks),
                        chunk_type=chunk_type,
                        symbol_name=symbol_name,
                        breadcrumb=breadcrumb,
                        start_line=start_l,
                        end_line=end_l,
                        content=snippet,
                    )
                )

        for child in root.children:
            walk(child)

    def _extract_js_ts_nodes(
        self,
        root: Node,
        raw: bytes,
        text: str,
        path: Path,
        filename: str,
        chunks: List[CodeChunk],
    ) -> None:
        lines = text.splitlines()

        def process_node(node: Node, parent_name: str = ""):
            target = node
            if node.type == "export_statement":
                decl = node.child_by_field_name("declaration")
                if decl:
                    target = decl

            t = target.type
            if t in ("function_declaration", "function_expression"):
                name_node = target.child_by_field_name("name")
                fn_name = name_node.text.decode("utf-8") if name_node else "anonymous"
                start_l = node.start_point.row + 1
                end_l = node.end_point.row + 1
                breadcrumb = f"{filename} > function {fn_name}"
                chunks.append(
                    CodeChunk(
                        file_path=str(path),
                        chunk_index=len(chunks),
                        chunk_type="function",
                        symbol_name=fn_name,
                        breadcrumb=breadcrumb,
                        start_line=start_l,
                        end_line=end_l,
                        content=text_from_lines(lines, start_l, end_l),
                    )
                )

            elif t in ("class_declaration", "class_expression"):
                name_node = target.child_by_field_name("name")
                cls_name = name_node.text.decode("utf-8") if name_node else "UnknownClass"
                start_l = node.start_point.row + 1
                end_l = node.end_point.row + 1
                breadcrumb = f"{filename} > class {cls_name}"
                chunks.append(
                    CodeChunk(
                        file_path=str(path),
                        chunk_index=len(chunks),
                        chunk_type="class",
                        symbol_name=cls_name,
                        breadcrumb=breadcrumb,
                        start_line=start_l,
                        end_line=end_l,
                        content=text_from_lines(lines, start_l, end_l),
                    )
                )
                body = target.child_by_field_name("body")
                if body:
                    for child in body.children:
                        if child.type == "method_definition":
                            m_name = child.child_by_field_name("name")
                            method_name = m_name.text.decode("utf-8") if m_name else "anonymous"
                            m_start = child.start_point.row + 1
                            m_end = child.end_point.row + 1
                            chunks.append(
                                CodeChunk(
                                    file_path=str(path),
                                    chunk_index=len(chunks),
                                    chunk_type="method",
                                    symbol_name=f"{cls_name}.{method_name}",
                                    breadcrumb=f"{filename} > class {cls_name} > {method_name}",
                                    start_line=m_start,
                                    end_line=m_end,
                                    content=text_from_lines(lines, m_start, m_end),
                                )
                            )

            elif t in ("interface_declaration", "type_alias_declaration"):
                name_node = target.child_by_field_name("name")
                item_name = name_node.text.decode("utf-8") if name_node else "type"
                start_l = node.start_point.row + 1
                end_l = node.end_point.row + 1
                chunks.append(
                    CodeChunk(
                        file_path=str(path),
                        chunk_index=len(chunks),
                        chunk_type="type",
                        symbol_name=item_name,
                        breadcrumb=f"{filename} > {item_name}",
                        start_line=start_l,
                        end_line=end_l,
                        content=text_from_lines(lines, start_l, end_l),
                    )
                )

            elif t == "lexical_declaration":
                for declarator in target.children:
                    if declarator.type == "variable_declarator":
                        val = declarator.child_by_field_name("value")
                        if val and val.type in ("arrow_function", "function_expression"):
                            name_node = declarator.child_by_field_name("name")
                            fn_name = name_node.text.decode("utf-8") if name_node else "arrow"
                            start_l = node.start_point.row + 1
                            end_l = node.end_point.row + 1
                            chunks.append(
                                CodeChunk(
                                    file_path=str(path),
                                    chunk_index=len(chunks),
                                    chunk_type="function",
                                    symbol_name=fn_name,
                                    breadcrumb=f"{filename} > const {fn_name}",
                                    start_line=start_l,
                                    end_line=end_l,
                                    content=text_from_lines(lines, start_l, end_l),
                                )
                            )

        for child in root.children:
            process_node(child)

    def _extract_bash_nodes(
        self,
        root: Node,
        raw: bytes,
        text: str,
        path: Path,
        filename: str,
        chunks: List[CodeChunk],
    ) -> None:
        lines = text.splitlines()
        for child in root.children:
            if child.type == "function_definition":
                name_node = child.child_by_field_name("name")
                fn_name = name_node.text.decode("utf-8") if name_node else "function"
                start_l = child.start_point.row + 1
                end_l = child.end_point.row + 1
                chunks.append(
                    CodeChunk(
                        file_path=str(path),
                        chunk_index=len(chunks),
                        chunk_type="function",
                        symbol_name=fn_name,
                        breadcrumb=f"{filename} > {fn_name}()",
                        start_line=start_l,
                        end_line=end_l,
                        content=text_from_lines(lines, start_l, end_l),
                    )
                )

    def _extract_rust_nodes(
        self,
        root: Node,
        raw: bytes,
        text: str,
        path: Path,
        filename: str,
        chunks: List[CodeChunk],
    ) -> None:
        lines = text.splitlines()
        for child in root.children:
            t = child.type
            if t == "function_item":
                name_node = child.child_by_field_name("name")
                fn_name = name_node.text.decode("utf-8") if name_node else "fn"
                start_l = child.start_point.row + 1
                end_l = child.end_point.row + 1
                chunks.append(
                    CodeChunk(
                        file_path=str(path),
                        chunk_index=len(chunks),
                        chunk_type="function",
                        symbol_name=fn_name,
                        breadcrumb=f"{filename} > fn {fn_name}",
                        start_line=start_l,
                        end_line=end_l,
                        content=text_from_lines(lines, start_l, end_l),
                    )
                )
            elif t in ("struct_item", "enum_item", "trait_item"):
                name_node = child.child_by_field_name("name")
                item_name = name_node.text.decode("utf-8") if name_node else t
                start_l = child.start_point.row + 1
                end_l = child.end_point.row + 1
                chunks.append(
                    CodeChunk(
                        file_path=str(path),
                        chunk_index=len(chunks),
                        chunk_type=t.replace("_item", ""),
                        symbol_name=item_name,
                        breadcrumb=f"{filename} > {t[:6]} {item_name}",
                        start_line=start_l,
                        end_line=end_l,
                        content=text_from_lines(lines, start_l, end_l),
                    )
                )
            elif t == "impl_item":
                type_node = child.child_by_field_name("type")
                impl_name = type_node.text.decode("utf-8") if type_node else "impl"
                body = child.child_by_field_name("body")
                if body:
                    for item in body.children:
                        if item.type == "function_item":
                            fn_node = item.child_by_field_name("name")
                            fn_name = fn_node.text.decode("utf-8") if fn_node else "method"
                            start_l = item.start_point.row + 1
                            end_l = item.end_point.row + 1
                            chunks.append(
                                CodeChunk(
                                    file_path=str(path),
                                    chunk_index=len(chunks),
                                    chunk_type="method",
                                    symbol_name=f"{impl_name}::{fn_name}",
                                    breadcrumb=f"{filename} > impl {impl_name} > fn {fn_name}",
                                    start_line=start_l,
                                    end_line=end_l,
                                    content=text_from_lines(lines, start_l, end_l),
                                )
                            )

    def _fallback_extract(self, path: Path, content: str = "") -> List[CodeChunk]:
        """Découpage par blocs de lignes avec chevauchement en cas d'absence de parser."""
        if not content:
            content = path.read_text("utf-8", errors="replace")

        lines = content.splitlines()
        chunks: List[CodeChunk] = []
        window_size = 50
        step = 40

        for idx, start in enumerate(range(0, max(1, len(lines)), step)):
            end = min(len(lines), start + window_size)
            snippet = "\n".join(lines[start:end])
            if not snippet.strip():
                continue
            c = CodeChunk(
                file_path=str(path),
                chunk_index=idx,
                chunk_type="block",
                symbol_name=f"{path.name}:L{start+1}",
                breadcrumb=f"{path.name} > lignes {start+1}-{end}",
                start_line=start + 1,
                end_line=end,
                content=snippet,
            )
            c.compute_hash()
            chunks.append(c)

        return chunks


class MarkdownDocExtractor:
    """Découpage récursif de documents Markdown par titres et paragraphes avec fil d'Ariane."""

    HEADING_REGEX = re.compile(r"^(#{1,6})\s+(.*)$")

    def extract_chunks(self, file_path: Path) -> List[CodeChunk]:
        try:
            content = file_path.read_text("utf-8", errors="replace")
        except Exception as e:
            logger.error("Erreur lecture markdown %s: %s", file_path, e)
            raise

        lines = content.splitlines()
        filename = file_path.name
        chunks: List[CodeChunk] = []

        stack: List[str] = []
        current_content: List[str] = []
        current_start: int = 1
        current_heading: str = filename

        for i, line in enumerate(lines, start=1):
            m = self.HEADING_REGEX.match(line)
            if m:
                # Flush de la section précédente si non vide
                if current_content and any(l.strip() for l in current_content):
                    breadcrumb = " > ".join([filename] + stack) if stack else filename
                    snippet = "\n".join(current_content)
                    chunks.append(
                        CodeChunk(
                            file_path=str(file_path),
                            chunk_index=len(chunks),
                            chunk_type="section",
                            symbol_name=current_heading,
                            breadcrumb=breadcrumb,
                            start_line=current_start,
                            end_line=i - 1,
                            content=snippet,
                        )
                    )
                    current_content = []

                hashes, title = m.groups()
                level = len(hashes)
                title = title.strip()
                # Mise à jour de la pile hiérarchique du fil d'Ariane
                stack = stack[: level - 1]
                stack.append(title)
                current_heading = title
                current_start = i
                current_content.append(line)
            else:
                current_content.append(line)

        # Dernier bloc
        if current_content and any(l.strip() for l in current_content):
            breadcrumb = " > ".join([filename] + stack) if stack else filename
            chunks.append(
                CodeChunk(
                    file_path=str(file_path),
                    chunk_index=len(chunks),
                    chunk_type="section",
                    symbol_name=current_heading,
                    breadcrumb=breadcrumb,
                    start_line=current_start,
                    end_line=len(lines),
                    content="\n".join(current_content),
                )
            )

        for idx, c in enumerate(chunks):
            c.chunk_index = idx
            c.compute_hash()

        return chunks


class TextDocExtractor:
    """Découpage par paragraphes et sections pour fichiers texte (.txt, .rst)."""

    def extract_chunks(self, file_path: Path) -> List[CodeChunk]:
        try:
            content = file_path.read_text("utf-8", errors="replace")
        except Exception as e:
            logger.error("Erreur lecture fichier texte %s: %s", file_path, e)
            raise

        lines = content.splitlines()
        filename = file_path.name
        chunks: List[CodeChunk] = []

        current_para: List[str] = []
        start_line = 1

        for i, line in enumerate(lines, start=1):
            if not line.strip():
                if current_para:
                    snippet = "\n".join(current_para)
                    chunks.append(
                        CodeChunk(
                            file_path=str(file_path),
                            chunk_index=len(chunks),
                            chunk_type="paragraph",
                            symbol_name=f"{filename}:L{start_line}",
                            breadcrumb=f"{filename} > paragraphe L{start_line}-{i-1}",
                            start_line=start_line,
                            end_line=i - 1,
                            content=snippet,
                        )
                    )
                    current_para = []
                start_line = i + 1
            else:
                current_para.append(line)

        if current_para:
            chunks.append(
                CodeChunk(
                    file_path=str(file_path),
                    chunk_index=len(chunks),
                    chunk_type="paragraph",
                    symbol_name=f"{filename}:L{start_line}",
                    breadcrumb=f"{filename} > paragraphe L{start_line}-{len(lines)}",
                    start_line=start_line,
                    end_line=len(lines),
                    content="\n".join(current_para),
                )
            )

        for idx, c in enumerate(chunks):
            c.chunk_index = idx
            c.compute_hash()

        return chunks


class PDFDocExtractor:
    """Extraction et découpage par page et sections pour fichiers PDF."""

    def extract_chunks(self, file_path: Path) -> List[CodeChunk]:
        if not HAS_PYPDF:
            raise RuntimeError("pypdf non disponible pour extraire le document")

        chunks: List[CodeChunk] = []
        filename = file_path.name

        try:
            reader = pypdf.PdfReader(str(file_path))
            for page_idx, page in enumerate(reader.pages, start=1):
                text = page.extract_text() or ""
                text = text.strip()
                if not text:
                    continue

                first_line = text.splitlines()[0][:60].strip() if text.splitlines() else f"Page {page_idx}"
                breadcrumb = f"{filename} > Page {page_idx} > {first_line}"

                chunks.append(
                    CodeChunk(
                        file_path=str(file_path),
                        chunk_index=len(chunks),
                        chunk_type="page",
                        symbol_name=f"Page {page_idx}",
                        breadcrumb=breadcrumb,
                        start_line=page_idx,
                        end_line=page_idx,
                        content=text,
                    )
                )
        except Exception as e:
            logger.error("Erreur extraction PDF %s: %s", file_path, e)
            raise

        for idx, c in enumerate(chunks):
            c.chunk_index = idx
            c.compute_hash()

        return chunks


def text_from_lines(lines: List[str], start_1: int, end_1: int) -> str:
    """Extrait une portion de texte à partir de numéros de lignes 1-indexés inclusifs."""
    s = max(0, start_1 - 1)
    e = min(len(lines), end_1)
    return "\n".join(lines[s:e])


# ══════════════════════════════════════════════════════════════════════════════
# 5. BASE VECTORIELLE LOCALE AVEC SQLITE-VEC & FTS5
# ══════════════════════════════════════════════════════════════════════════════

class PersonalRAGStorage:
    """Gestionnaire de la base SQLite locale avec sqlite-vec et table FTS5."""

    def __init__(self, db_path: Path = DEFAULT_DB_PATH, dim: int = DEFAULT_EMBEDDING_DIM):
        self.db_path = Path(db_path)
        self.dim = dim
        self._lock = threading.Lock()
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        if HAS_SQLITE_VEC:
            try:
                conn.enable_load_extension(True)
                sqlite_vec.load(conn)
            except Exception as e:
                logger.warning("Impossible de charger sqlite-vec: %s", e)
        return conn

    def _init_db(self) -> None:
        new_database = not self.db_path.exists()
        with self._lock:
            with self._get_connection() as conn:
                # SQLite ne peut activer ce mode sans réécrire une base déjà
                # remplie. Sur une création neuve il ne coûte rien et permet de
                # rendre les pages libres sans futur VACUUM complet.
                if new_database:
                    conn.execute("PRAGMA auto_vacuum=INCREMENTAL;")
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute("PRAGMA synchronous=NORMAL;")

                # Table de métadonnées des fichiers indexés
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS indexed_files (
                        file_path TEXT PRIMARY KEY,
                        file_hash TEXT NOT NULL,
                        mtime REAL NOT NULL,
                        size INTEGER NOT NULL,
                        chunk_count INTEGER NOT NULL,
                        last_indexed REAL NOT NULL
                    );
                """)

                # Table relationnelle des blocs (chunks)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS chunks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        file_path TEXT NOT NULL,
                        chunk_index INTEGER NOT NULL,
                        chunk_hash TEXT NOT NULL,
                        chunk_type TEXT NOT NULL,
                        symbol_name TEXT NOT NULL,
                        breadcrumb TEXT NOT NULL,
                        start_line INTEGER NOT NULL,
                        end_line INTEGER NOT NULL,
                        content TEXT NOT NULL,
                        indexed_at REAL NOT NULL,
                        FOREIGN KEY(file_path) REFERENCES indexed_files(file_path) ON DELETE CASCADE
                    );
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_path);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_hash ON chunks(chunk_hash);")

                # Table vectorielle native sqlite-vec
                if HAS_SQLITE_VEC:
                    try:
                        conn.execute(f"""
                            CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(
                                id INTEGER PRIMARY KEY,
                                embedding float[{self.dim}]
                            );
                        """)
                    except Exception as e:
                        logger.warning("Erreur création table vec0: %s", e)

                # Table plein texte FTS5 pour recherche lexicale / hybride
                try:
                    conn.execute("""
                        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                            symbol_name,
                            breadcrumb,
                            content,
                            content='chunks',
                            content_rowid='id'
                        );
                    """)
                except Exception as e:
                    logger.debug("Table FTS5 déjà prête ou non disponible: %s", e)

    def get_file_record(self, file_path: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            with self._get_connection() as conn:
                row = conn.execute(
                    "SELECT file_hash, mtime, size, chunk_count FROM indexed_files WHERE file_path = ?",
                    (file_path,),
                ).fetchone()
                return dict(row) if row else None

    def get_existing_chunks(self, file_path: str) -> Dict[str, int]:
        """Retourne le mapping {chunk_hash: chunk_id} pour un fichier donné."""
        with self._lock:
            with self._get_connection() as conn:
                rows = conn.execute(
                    "SELECT id, chunk_hash FROM chunks WHERE file_path = ?",
                    (file_path,),
                ).fetchall()
                return {r["chunk_hash"]: r["id"] for r in rows}

    def delete_chunks(self, chunk_ids: Sequence[int]) -> None:
        if not chunk_ids:
            return
        with self._lock:
            with self._get_connection() as conn:
                placeholders = ",".join("?" for _ in chunk_ids)
                if HAS_SQLITE_VEC:
                    try:
                        conn.execute(
                            f"DELETE FROM vec_chunks WHERE id IN ({placeholders})",
                            tuple(chunk_ids),
                        )
                    except Exception as e:
                        logger.debug("Erreur delete vec_chunks: %s", e)
                try:
                    conn.execute(
                        f"DELETE FROM chunks_fts WHERE rowid IN ({placeholders})",
                        tuple(chunk_ids),
                    )
                except Exception:
                    pass
                conn.execute(
                    f"DELETE FROM chunks WHERE id IN ({placeholders})",
                    tuple(chunk_ids),
                )

    def insert_chunk(self, chunk: CodeChunk, embedding: Sequence[float]) -> int:
        """Insère un bloc dans chunks, vec_chunks et chunks_fts."""
        with self._lock:
            with self._get_connection() as conn:
                cur = conn.cursor()
                now = time.time()
                cur.execute(
                    """
                    INSERT INTO chunks (
                        file_path, chunk_index, chunk_hash, chunk_type,
                        symbol_name, breadcrumb, start_line, end_line,
                        content, indexed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk.file_path,
                        chunk.chunk_index,
                        chunk.chunk_hash,
                        chunk.chunk_type,
                        chunk.symbol_name,
                        chunk.breadcrumb,
                        chunk.start_line,
                        chunk.end_line,
                        chunk.content,
                        now,
                    ),
                )
                chunk_id = cur.lastrowid

                # Insertion vectorielle sqlite-vec
                if HAS_SQLITE_VEC and embedding:
                    blob = array.array("f", embedding).tobytes()
                    try:
                        conn.execute(
                            "INSERT INTO vec_chunks(id, embedding) VALUES (?, ?)",
                            (chunk_id, blob),
                        )
                    except Exception as e:
                        logger.debug("Erreur insertion vec_chunks: %s", e)

                # Insertion FTS5
                try:
                    conn.execute(
                        "INSERT INTO chunks_fts(rowid, symbol_name, breadcrumb, content) VALUES (?, ?, ?, ?)",
                        (chunk_id, chunk.symbol_name, chunk.breadcrumb, chunk.content),
                    )
                except Exception:
                    pass

                return chunk_id

    def update_file_record(
        self, file_path: str, file_hash: str, mtime: float, size: int, chunk_count: int
    ) -> None:
        with self._lock:
            with self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO indexed_files (file_path, file_hash, mtime, size, chunk_count, last_indexed)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(file_path) DO UPDATE SET
                        file_hash=excluded.file_hash,
                        mtime=excluded.mtime,
                        size=excluded.size,
                        chunk_count=excluded.chunk_count,
                        last_indexed=excluded.last_indexed
                    """,
                    (file_path, file_hash, mtime, size, chunk_count, time.time()),
                )

    def remove_file(self, file_path: str) -> None:
        """Supprime complètement un fichier et tous ses blocs associés."""
        with self._lock:
            with self._get_connection() as conn:
                rows = conn.execute("SELECT id FROM chunks WHERE file_path = ?", (file_path,)).fetchall()
                chunk_ids = [r["id"] for r in rows]

                if chunk_ids:
                    placeholders = ",".join("?" for _ in chunk_ids)
                    if HAS_SQLITE_VEC:
                        try:
                            conn.execute(f"DELETE FROM vec_chunks WHERE id IN ({placeholders})", tuple(chunk_ids))
                        except Exception:
                            pass
                    try:
                        conn.execute(f"DELETE FROM chunks_fts WHERE rowid IN ({placeholders})", tuple(chunk_ids))
                    except Exception:
                        pass
                    conn.execute(f"DELETE FROM chunks WHERE id IN ({placeholders})", tuple(chunk_ids))

                conn.execute("DELETE FROM indexed_files WHERE file_path = ?", (file_path,))

    def vector_search(self, query_vec: Sequence[float], limit: int = 20) -> List[Tuple[int, float]]:
        """Recherche k-plus proches voisins dans vec_chunks."""
        if not HAS_SQLITE_VEC or not query_vec:
            return []
        blob = array.array("f", query_vec).tobytes()
        with self._lock:
            with self._get_connection() as conn:
                try:
                    rows = conn.execute(
                        """
                        SELECT id, distance
                        FROM vec_chunks
                        WHERE embedding MATCH ? AND k = ?
                        ORDER BY distance
                        """,
                        (blob, limit),
                    ).fetchall()
                    return [(r["id"], float(r["distance"])) for r in rows]
                except Exception as e:
                    logger.warning("Erreur requête kNN vec_chunks: %s", e)
                    return []

    def fts_search(self, query_text: str, limit: int = 20) -> List[Tuple[int, float]]:
        """Recherche plein texte FTS5 avec BM25."""
        cleaned = re.sub(r"[^\w\s]", " ", query_text).strip()
        tokens = [t for t in cleaned.split() if len(t) > 1]
        if not tokens:
            return []

        # Construction requête FTS : term1 OR term2 OR "phrase"
        fts_query = " OR ".join(tokens)
        with self._lock:
            with self._get_connection() as conn:
                try:
                    rows = conn.execute(
                        """
                        SELECT rowid, bm25(chunks_fts) as rank
                        FROM chunks_fts
                        WHERE chunks_fts MATCH ?
                        ORDER BY rank
                        LIMIT ?
                        """,
                        (fts_query, limit),
                    ).fetchall()
                    return [(r["rowid"], float(r["rank"])) for r in rows]
                except Exception as e:
                    logger.debug("Erreur recherche FTS5: %s", e)
                    return []

    def get_chunk_by_id(self, chunk_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            with self._get_connection() as conn:
                row = conn.execute("SELECT * FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
                return dict(row) if row else None

    def count_indexed_files(self) -> int:
        with self._lock:
            with self._get_connection() as conn:
                row = conn.execute("SELECT count(*) FROM indexed_files").fetchone()
                return row[0] if row else 0

    def count_chunks(self) -> int:
        with self._lock:
            with self._get_connection() as conn:
                row = conn.execute("SELECT count(*) FROM chunks").fetchone()
                return row[0] if row else 0


# ══════════════════════════════════════════════════════════════════════════════
# 6. MOTEUR RAG PERSONNEL (INDEXATION & RECHERCHE CHIRURGICALE)
# ══════════════════════════════════════════════════════════════════════════════

class PersonalRAG:
    """Moteur RAG personnel orchestrant extracteurs, hachage incrémental et sqlite-vec."""

    def __init__(
        self,
        db_path: Optional[Path] = None,
        roots: Optional[Sequence[Path]] = None,
        embedder: Optional[LocalDenseEmbedder] = None,
    ):
        self.db_path = Path(db_path or DEFAULT_DB_PATH)
        self.roots = [Path(r) for r in roots] if roots else [r for r in DEFAULT_INDEX_ROOTS if r.exists()]
        self.embedder = embedder or LocalDenseEmbedder(dim=DEFAULT_EMBEDDING_DIM)
        self.storage = PersonalRAGStorage(db_path=self.db_path, dim=self.embedder.dim)

        # Extracteurs spécialisés
        self.code_extractor = TreeSitterCodeExtractor()
        self.md_extractor = MarkdownDocExtractor()
        self.txt_extractor = TextDocExtractor()
        self.pdf_extractor = PDFDocExtractor()

        # Surveillance watchdog
        self._watcher: Optional[BackgroundWatcher] = None
        self._initial_index_future = None
        self._initial_index_lock = threading.Lock()

    def compute_file_sha256(self, path: Path) -> str:
        """Calcule le SHA256 complet d'un fichier."""
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
        return h.hexdigest()

    def index_file(self, path: Path | str, force: bool = False) -> Tuple[bool, int, int]:
        """Indexe un fichier de façon incrémentale.

        Retourne : (a_ete_modifie, nb_chunks_ajoutes, nb_chunks_conserves)
        """
        file_path = Path(path).resolve()
        if not file_path.is_file():
            return False, 0, 0

        if should_ignore_path(file_path):
            return False, 0, 0

        ext = file_path.suffix.lower()
        if ext not in SUPPORTED_EXTENSIONS:
            return False, 0, 0

        try:
            stat = file_path.stat()
            if stat.st_size > MAX_FILE_SIZE_BYTES:
                logger.info("Fichier ignoré car trop volumineux (%d octets): %s", stat.st_size, file_path)
                return False, 0, 0
        except OSError:
            return False, 0, 0

        path_str = str(file_path)
        current_hash = self.compute_file_sha256(file_path)
        current_mtime = stat.st_mtime
        current_size = stat.st_size

        # Vérification incrémentale niveau fichier : si hash identique, rien à faire
        existing_rec = self.storage.get_file_record(path_str)
        if not force and existing_rec:
            if existing_rec["file_hash"] == current_hash and abs(existing_rec["mtime"] - current_mtime) < 0.01:
                return False, 0, existing_rec["chunk_count"]

        try:
            # Extraction des nouveaux blocs (chunks)
            if ext in SUPPORTED_CODE_EXTENSIONS:
                new_chunks = self.code_extractor.extract_chunks(file_path)
            elif ext in (".md", ".markdown"):
                new_chunks = self.md_extractor.extract_chunks(file_path)
            elif ext in (".txt", ".text", ".rst"):
                new_chunks = self.txt_extractor.extract_chunks(file_path)
            elif ext == ".pdf":
                new_chunks = self.pdf_extractor.extract_chunks(file_path)
            else:
                new_chunks = []
        except Exception:
            logger.exception("Indexation impossible ; version précédente conservée : %s", file_path)
            return False, 0, 0

        # Récupération des blocs existants pour ce fichier
        existing_chunks = self.storage.get_existing_chunks(path_str)
        new_chunks_by_hash = {c.chunk_hash: c for c in new_chunks}

        # 1. Blocs supprimés : présents en base mais plus dans le fichier
        chunks_to_delete_ids = [
            cid for chash, cid in existing_chunks.items() if chash not in new_chunks_by_hash
        ]
        self.storage.delete_chunks(chunks_to_delete_ids)

        # 2. Blocs nouveaux / modifiés : à vectoriser et insérer
        added_count = 0
        reused_count = 0

        for chunk in new_chunks:
            if chunk.chunk_hash in existing_chunks:
                # Bloc inchangé : on réutilise le vecteur existant sans recalcul
                reused_count += 1
            else:
                # Bloc nouveau ou modifié : calcul de l'embedding dense et insertion
                emb_text = f"{chunk.symbol_name} {chunk.breadcrumb}\n{chunk.content}"
                embedding = self.embedder.embed(emb_text)
                self.storage.insert_chunk(chunk, embedding)
                added_count += 1

        # Mise à jour de l'enregistrement fichier
        self.storage.update_file_record(
            file_path=path_str,
            file_hash=current_hash,
            mtime=current_mtime,
            size=current_size,
            chunk_count=len(new_chunks),
        )

        return True, added_count, reused_count

    def remove_file(self, path: Path | str) -> None:
        """Supprime un fichier supprimé du disque de la base vectorielle."""
        self.storage.remove_file(str(Path(path).resolve()))

    def index_directory(self, root_dir: Path | str, recursive: bool = True) -> IndexStats:
        """Indexe récursivement un répertoire en ignorant automatiquement .git, node_modules, etc."""
        root = Path(root_dir).resolve()
        stats = IndexStats()
        if not root.exists():
            return stats

        logger.info("Début indexation de %s...", root)
        for dirpath, dirnames, filenames in os.walk(root):
            # Élagage en place des dossiers à ignorer pour éviter de les parcourir
            dirnames[:] = [] if not recursive else [
                d for d in dirnames if d not in IGNORED_DIRS and not d.startswith(".")
            ]

            for fname in filenames:
                file_path = Path(dirpath) / fname
                if should_ignore_path(file_path):
                    stats.files_skipped += 1
                    continue

                if file_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                    continue

                try:
                    modified, added, reused = self.index_file(file_path)
                    if modified:
                        stats.files_indexed += 1
                        stats.chunks_created += added
                        stats.chunks_reused += reused
                    else:
                        stats.files_skipped += 1
                        stats.chunks_reused += reused
                except Exception as e:
                    logger.error("Erreur indexation %s: %s", file_path, e)
                    stats.errors += 1

        return stats

    def search(
        self,
        query: str,
        file_pattern: Optional[str] = None,
        max_results: int = 5,
    ) -> List[RAGSearchResult]:
        """Recherche hybride (vectorielle sqlite-vec + textuelle FTS5 + symbol match)."""
        if not query.strip():
            return []

        # 1. Recherche vectorielle dense via sqlite-vec
        query_vec = self.embedder.embed(query)
        vec_matches = self.storage.vector_search(query_vec, limit=max(max_results * 5, 50))
        # Distance L2 -> Similarité cosinus [0.0, 1.0]
        vec_scores: Dict[int, float] = {}
        vec_distances: Dict[int, float] = {}
        for cid, dist in vec_matches:
            cos_sim = max(0.0, 1.0 - (dist * dist) / 2.0)
            vec_scores[cid] = cos_sim
            vec_distances[cid] = dist

        # 2. Recherche textuelle FTS5
        fts_matches = self.storage.fts_search(query, limit=max(max_results * 5, 50))
        fts_scores: Dict[int, float] = {}
        for rank_idx, (cid, bm25) in enumerate(fts_matches):
            # Rank normalisé
            fts_scores[cid] = 1.0 / (1.0 + rank_idx * 0.1)

        # 3. Fusion hybride des scores
        candidate_ids = set(vec_scores.keys()) | set(fts_scores.keys())
        scored_results: List[Tuple[float, Dict[str, Any], float]] = []

        query_terms = [t.lower() for t in re.findall(r"\w+", query) if len(t) > 2]

        for cid in candidate_ids:
            chunk_data = self.storage.get_chunk_by_id(cid)
            if not chunk_data:
                continue

            file_path = chunk_data["file_path"]

            # Filtrage par pattern de fichier si spécifié
            if file_pattern:
                pat = file_pattern.strip()
                match = (
                    fnmatch.fnmatch(file_path, pat)
                    or fnmatch.fnmatch(Path(file_path).name, pat)
                    or pat.lower() in file_path.lower()
                )
                if not match:
                    continue

            v_score = vec_scores.get(cid, 0.0)
            f_score = fts_scores.get(cid, 0.0)
            dist = vec_distances.get(cid, 2.0)

            # Bonus pour correspondance exacte de symbole / identifiant
            symbol_boost = 0.0
            sym_lower = chunk_data["symbol_name"].lower()
            for term in query_terms:
                if term in sym_lower:
                    symbol_boost += 0.25

            # Élimination du bruit de fond (vecteurs sans aucune correspondance lexicale/symbole et faible similarité)
            if v_score < 0.10 and f_score == 0.0 and symbol_boost == 0.0:
                continue

            # Score composite
            final_score = 0.60 * v_score + 0.30 * f_score + 0.10 * min(1.0, symbol_boost)
            if final_score < 0.05:
                continue

            scored_results.append((final_score, chunk_data, dist))

        # Tri décroissant par score
        scored_results.sort(key=lambda x: x[0], reverse=True)

        results: List[RAGSearchResult] = []
        for score, data, dist in scored_results[:max_results]:
            p = Path(data["file_path"])
            start_l = data["start_line"]
            end_l = data["end_line"]
            link = f"file://{data['file_path']}#L{start_l}-L{end_l}"

            results.append(
                RAGSearchResult(
                    file_path=data["file_path"],
                    filename=p.name,
                    chunk_type=data["chunk_type"],
                    symbol_name=data["symbol_name"],
                    breadcrumb=data["breadcrumb"],
                    start_line=start_l,
                    end_line=end_l,
                    content=data["content"],
                    score=score,
                    distance=dist,
                    link=link,
                )
            )

        return results

    def start_watcher(self, poll_interval: float = 1.0) -> bool:
        """Démarre la surveillance en tâche de fond des répertoires via watchdog."""
        if not HAS_WATCHDOG:
            logger.warning("Watchdog non installé, surveillance continue désactivée.")
            return False

        if self._watcher and self._watcher.is_alive():
            return True

        self._watcher = BackgroundWatcher(self, roots=self.roots, poll_interval=poll_interval)
        self._watcher.start()
        return True

    def stop_watcher(self) -> None:
        """Arrête proprement la surveillance en tâche de fond."""
        if self._watcher:
            self._watcher.stop()
            self._watcher = None

    def start_background_indexing(self) -> bool:
        """Lance le scan initial puis maintient l'index à jour sans bloquer la voix."""
        with self._initial_index_lock:
            if self._initial_index_future is not None:
                return False

            def _initial_scan() -> None:
                for root in self.roots:
                    if self._stop_event_requested():
                        break
                    self.index_directory(root)
                # Watchdog parcourt lui aussi l'arborescence lors de son
                # démarrage. Le lancer après le scan évite deux marches disque
                # simultanées et rend le bootstrap instantané côté appelant.
                self.start_watcher()

            from core.thread_pool import get_thread_pool
            self._initial_index_future = get_thread_pool().submit(
                "disk-io", _initial_scan,
                task_name="rag-initial-index", stall_timeout=float("inf"),
            )
            return True

    def _stop_event_requested(self) -> bool:
        watcher = self._watcher
        return bool(watcher and watcher._stop_event.is_set())


# ══════════════════════════════════════════════════════════════════════════════
# 7. SURVEILLANCE TEMPS RÉEL EN ARRIÈRE-PLAN AVEC WATCHDOG
# ══════════════════════════════════════════════════════════════════════════════

class RAGWatchdogHandler(FileSystemEventHandler):
    """Gestionnaire d'événements watchdog avec mise en file d'attente et dérebond."""

    def __init__(self, queue_callback: Callable[[str, str], None]):
        super().__init__()
        self.queue_callback = queue_callback

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self.queue_callback("modified", event.src_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self.queue_callback("modified", event.src_path)

    def on_deleted(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self.queue_callback("deleted", event.src_path)

    def on_moved(self, event: FileMovedEvent) -> None:
        if not event.is_directory:
            self.queue_callback("deleted", event.src_path)
            self.queue_callback("modified", event.dest_path)


class BackgroundWatcher:
    """Thread de surveillance et d'indexation incrémentale en arrière-plan."""

    def __init__(self, rag: PersonalRAG, roots: Sequence[Path], poll_interval: float = 1.0):
        self.rag = rag
        self.roots = roots
        self.poll_interval = poll_interval
        self._observer: Optional[Observer] = None
        self._pending_events: Dict[str, Tuple[str, float]] = {}  # path -> (action, timestamp)
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None

    def _queue_event(self, action: str, path: str) -> None:
        p = Path(path)
        if should_ignore_path(p):
            return
        if p.suffix.lower() not in SUPPORTED_EXTENSIONS:
            return
        with self._lock:
            self._pending_events[path] = (action, time.time())

    def start(self) -> None:
        if not HAS_WATCHDOG:
            return
        self._observer = Observer()
        handler = RAGWatchdogHandler(self._queue_event)

        for root in self.roots:
            if root.exists() and root.is_dir():
                try:
                    self._observer.schedule(handler, str(root), recursive=True)
                    logger.info("Watchdog surveille %s", root)
                except Exception as e:
                    logger.warning("Impossible de surveiller %s: %s", root, e)

        self._observer.start()
        from core.thread_pool import get_thread_pool
        self._worker_thread = get_thread_pool().spawn_thread(
            "disk-io", "rag-index-watcher", self._process_loop,
            stall_timeout=float("inf"),
        )

    def _process_loop(self) -> None:
        while not self._stop_event.is_set():
            time.sleep(0.3)
            now = time.time()
            to_process: List[Tuple[str, str]] = []

            with self._lock:
                # Dérebond : traiter uniquement les fichiers stables depuis plus de 0.5s
                for path, (action, ts) in list(self._pending_events.items()):
                    if now - ts >= 0.5:
                        to_process.append((path, action))
                        del self._pending_events[path]

            for path, action in to_process:
                try:
                    p = Path(path)
                    if action == "deleted" or not p.exists():
                        self.rag.remove_file(p)
                        logger.debug("RAG: Retiré %s", p.name)
                    else:
                        mod, add, reused = self.rag.index_file(p)
                        if mod:
                            logger.info("RAG: Ré-indexé %s (+%d blocs, %d conservés)", p.name, add, reused)
                except Exception as e:
                    logger.error("Erreur traitement événement %s sur %s: %s", action, path, e)

    def is_alive(self) -> bool:
        return bool(self._observer and self._observer.is_alive())

    def stop(self) -> None:
        self._stop_event.set()
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=2.0)
        if self._worker_thread and self._worker_thread is not threading.current_thread():
            self._worker_thread.join(timeout=2.0)
        self._worker_thread = None


# ══════════════════════════════════════════════════════════════════════════════
# 8. OUTIL GEMINI LIVE & MCP : search_personal_docs
# ══════════════════════════════════════════════════════════════════════════════

_GLOBAL_RAG: Optional[PersonalRAG] = None
_GLOBAL_LOCK = threading.Lock()


def get_personal_rag(db_path: Optional[Path] = None, roots: Optional[Sequence[Path]] = None) -> PersonalRAG:
    """Accès singleton thread-safe au moteur RAG."""
    global _GLOBAL_RAG
    with _GLOBAL_LOCK:
        if _GLOBAL_RAG is None:
            _GLOBAL_RAG = PersonalRAG(db_path=db_path, roots=roots)
        return _GLOBAL_RAG


def format_search_results_markdown(results: List[RAGSearchResult], query: str) -> str:
    """Formate les extraits RAG avec liens cliquables 'file:///...' et syntaxe markdown."""
    if not results:
        return f"Aucun extrait pertinent trouvé pour « {query} » dans les documents et projets locaux."

    lines: List[str] = [
        f"### 🔍 Extraits pertinents ({len(results)}) pour « {query} » :",
        "",
    ]

    lang_map = {
        "py": "python",
        "js": "javascript",
        "ts": "typescript",
        "sh": "bash",
        "bash": "bash",
        "zsh": "bash",
        "rs": "rust",
        "md": "markdown",
    }

    for i, r in enumerate(results, start=1):
        ext = Path(r.file_path).suffix.lstrip(".").lower()
        lang_tag = lang_map.get(ext, ext)

        # Détermination du titre
        symbol_display = f"`{r.symbol_name}`" if r.symbol_name else r.filename
        lines.append(f"#### {i}. {symbol_display}")
        lines.append(f"- **Fichier** : [{r.filename}]({r.link}) (lignes {r.start_line} à {r.end_line})")
        lines.append(f"- **Fil d'Ariane** : `{r.breadcrumb}`")
        lines.append(f"- **Type** : `{r.chunk_type}` | **Score de pertinence** : {r.score:.2f}")
        lines.append("")
        lines.append(f"```{lang_tag}")
        lines.append(r.content.strip())
        lines.append("```")
        lines.append("")

    return "\n".join(lines)


@tool(
    name="search_personal_docs",
    description=(
        "Recherche sémantique et syntaxique chirurgicale dans tous les documents et projets "
        "locaux (~/Documents, ~/OUTILS, dépôts git). Découpe le code par fonctions/classes via "
        "Tree-sitter et les documents par titres avec fil d'Ariane. Retourne des extraits "
        "précis avec liens cliquables 'file:///...' et numéros de lignes exacts."
    ),
    destructive=False,
    rate_limit="30/minute",
    requires_voice_id=False,
)
def search_personal_docs(
    query: str,
    file_pattern: str = "",
    max_results: int = 5,
) -> str:
    """Outil exposé à Gemini Live et au serveur MCP pour interroger la base de code locale.

    Args:
        query: Requête en langage naturel ou symboles recherchés (ex: "comment démarrer audio engine").
        file_pattern: Filtre optionnel sur le fichier ou dossier (ex: "*.py", "core/*", "ANO-GPT").
        max_results: Nombre maximum d'extraits retournés (défaut: 5).

    Returns:
        Markdown avec liens cliquables file:/// et numéros de lignes.
    """
    rag = get_personal_rag()
    results = rag.search(query=query, file_pattern=file_pattern, max_results=max_results)
    return format_search_results_markdown(results, query=query)
