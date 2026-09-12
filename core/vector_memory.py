"""core/vector_memory.py — Mémoire vectorielle locale et graphe RDF pour ANO-GPT.

Architecture hybride :
1. Indexation vectorielle in-process avec sqlite-vec (aucune dépendance à un serveur externe type Chroma/Pinecone).
2. Embeddings locaux ONNX Runtime (384 dimensions) basés sur all-MiniLM-L6-v2 avec mean-pooling et normalisation L2.
3. Graphe de connaissances RDF léger : triplets (Sujet, Prédicat, Objet, Validité_Temporelle, Confiance).
4. Détection automatique d'entités (Projets, Logiciels/Outils, Personnes, Lieux) et gestion des contradictions (invalidation dynamique).
5. Recherche hybride RRF (Reciprocal Rank Fusion) combinant BM25 (FTS5) et similarité cosinus (sqlite-vec).
6. Injection automatique des 3 faits les plus pertinents dans le system prompt Gemini Live.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import struct
import sys
import threading
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np

try:
    import sqlite_vec
except ImportError:
    sqlite_vec = None

try:
    import onnxruntime as ort
    from tokenizers import Tokenizer
except ImportError:
    ort = None
    Tokenizer = None

logger = logging.getLogger("anogpt.vector_memory")


# ── Chemins et constantes ───────────────────────────────────────────────────

def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = _base_dir()
DB_PATH = BASE_DIR / "memory" / "vector_memory.db"
LEGACY_DB_PATH = BASE_DIR / "memory" / "memory.db"
LEGACY_JSON = BASE_DIR / "memory" / "long_term.json"
MODEL_LOCAL_DIR = BASE_DIR / "models" / "all-MiniLM-L6-v2"

EMBEDDING_DIM = 384
MAX_VALUE_CHARS = 1000
K_RRF_DEFAULT = 60

KIND_PROFILE = "profil"
KIND_FACT = "fait"
KIND_EPISODE = "episode"
KIND_TURN = "tour"
KIND_PREFERENCE = "preference"
KINDS = (KIND_PROFILE, KIND_FACT, KIND_EPISODE, KIND_TURN, KIND_PREFERENCE)

# Prédicats fonctionnels (à valeur unique) déclenchant la détection de contradiction
FUNCTIONAL_PREDICATES = {
    "langage_favori", "langage_prefere", "langage_actuel",
    "os_favori", "os_actuel", "systeme_actuel",
    "editeur_code", "editeur_favori",
    "projet_actuel", "projet_principal",
    "habite_a", "ville_residence", "lieu_travail",
    "boisson_preferee", "statut_actuel",
}

# Mots vides pour la génération de requête FTS5
_STOPWORDS = {
    "alors", "après", "aussi", "autre", "avec", "avoir", "bien", "cela",
    "cette", "chez", "comme", "comment", "dans", "des", "donc", "dont",
    "elle", "encore", "est", "etre", "être", "fait", "faire", "faut", "ici",
    "juste", "les", "leur", "mais", "merci", "mes", "mon", "moi", "meme",
    "même", "notre", "nous", "ont", "ou", "où", "par", "parce", "pas",
    "peut", "peux", "plus", "pour", "pourquoi", "quand", "que", "quel",
    "quelle", "qui", "quoi", "sans", "ses", "son", "sont", "sur", "tout",
    "toute", "très", "tres", "tu", "une", "vous", "peux-tu", "dis", "dit",
    "voir", "veux", "vais", "suis", "ai", "as", "the", "and", "you",
}


# ── Modèles de données ──────────────────────────────────────────────────────

@dataclass
class RDFTriple:
    id: int | None
    subject: str
    predicate: str
    object: str
    valid_from: str
    valid_until: str | None = None
    is_active: bool = True
    confidence: float = 1.0
    source_entry_id: int | None = None
    created_at: str = ""
    updated_at: str = ""

    def text_representation(self) -> str:
        return f"{self.subject} {self.predicate} {self.object}"


@dataclass
class MemoryEntry:
    id: int
    kind: str
    key: str | None
    value: str
    category: str | None
    metadata: dict[str, Any] = field(default_factory=dict)
    valid_from: str = ""
    valid_until: str | None = None
    is_active: bool = True
    confidence: float = 1.0
    hits: int = 0
    created_at: str = ""
    updated_at: str = ""
    happened_at: str | None = None
    score_rrf: float = 0.0
    bm25_rank: int | None = None
    vec_rank: int | None = None
    vec_distance: float | None = None


# ── Moteur d'embeddings local ONNX ──────────────────────────────────────────

class EmbeddingEngine:
    """Moteur d'embeddings local ONNX Runtime 384d avec lazy-loading thread-safe."""

    _instance: EmbeddingEngine | None = None
    _lock = threading.Lock()

    def __init__(self, model_dir: Path | str | None = None):
        self._model_dir = Path(model_dir) if model_dir else MODEL_LOCAL_DIR
        self._session: ort.InferenceSession | None = None
        self._tokenizer: Tokenizer | None = None
        self._init_lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> EmbeddingEngine:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = EmbeddingEngine()
        return cls._instance

    def _ensure_loaded(self) -> None:
        if self._session is not None and self._tokenizer is not None:
            return
        with self._init_lock:
            if self._session is not None and self._tokenizer is not None:
                return

            model_path = self._model_dir / "model.onnx"
            tokenizer_path = self._model_dir / "tokenizer.json"

            if not (model_path.is_file() and tokenizer_path.is_file()):
                # Recherche dans le cache huggingface
                try:
                    from huggingface_hub import hf_hub_download

                    logger.info("Recherche du modèle all-MiniLM-L6-v2 via huggingface_hub...")
                    repo = "sentence-transformers/all-MiniLM-L6-v2"
                    try:
                        m_file = hf_hub_download(repo, "onnx/model.onnx", local_files_only=True)
                        t_file = hf_hub_download(repo, "tokenizer.json", local_files_only=True)
                    except Exception:
                        m_file = hf_hub_download(repo, "onnx/model.onnx")
                        t_file = hf_hub_download(repo, "tokenizer.json")
                    model_path = Path(m_file)
                    tokenizer_path = Path(t_file)
                except Exception as exc:
                    raise RuntimeError(
                        f"Impossible de charger le modèle ONNX (fichiers introuvables dans {self._model_dir} "
                        f"et échec du hub Hugging Face) : {exc}"
                    ) from exc

            # Chargement de la session ONNX
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = 2
            opts.inter_op_num_threads = 1
            opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            self._session = ort.InferenceSession(
                str(model_path),
                sess_options=opts,
                providers=["CPUExecutionProvider"],
            )

            # Chargement du tokenizer Hugging Face
            self._tokenizer = Tokenizer.from_file(str(tokenizer_path))
            self._tokenizer.enable_truncation(max_length=256)
            self._tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
            logger.info("Modèle d'embedding ONNX (384d) initialisé avec succès.")

    def encode(self, texts: str | Sequence[str]) -> np.ndarray:
        """Encode une phrase ou liste de phrases en vecteur(s) de dimension 384 normalisé(s)."""
        self._ensure_loaded()
        is_single = isinstance(texts, str)
        text_list = [texts] if is_single else list(texts)

        if not text_list:
            return np.empty((0, EMBEDDING_DIM), dtype=np.float32)

        # Tokenisation par lots
        encoded = self._tokenizer.encode_batch(text_list)
        input_ids = np.array([e.ids for e in encoded], dtype=np.int64)
        attention_mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)
        token_type_ids = np.array([e.type_ids for e in encoded], dtype=np.int64)

        inputs = {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
            "token_type_ids": token_type_ids,
        }

        outputs = self._session.run(None, inputs)
        token_embeddings = outputs[0]  # Shape: (batch_size, seq_len, 384)

        # Mean pooling avec attention_mask
        mask_expanded = np.broadcast_to(
            np.expand_dims(attention_mask, -1), token_embeddings.shape
        ).astype(float)
        sum_embeddings = np.sum(token_embeddings * mask_expanded, axis=1)
        sum_mask = np.clip(mask_expanded.sum(axis=1), a_min=1e-9, a_max=None)
        embeddings = sum_embeddings / sum_mask

        # Normalisation L2
        norms = np.linalg.norm(embeddings, ord=2, axis=1, keepdims=True)
        norms = np.maximum(norms, 1e-12)
        normalized = (embeddings / norms).astype(np.float32)

        return normalized[0] if is_single else normalized

    def serialize(self, vector: np.ndarray) -> bytes:
        """Sérialise un vecteur float32 pour sqlite-vec."""
        if sqlite_vec is not None and hasattr(sqlite_vec, "serialize_float32"):
            return sqlite_vec.serialize_float32(vector)
        # Fallback struct
        v_list = vector.flatten().tolist()
        return struct.pack(f"{len(v_list)}f", *v_list)


# ── Utilitaires de normalisation et d'extraction ─────────────────────────────

def _fold(value: str) -> str:
    return "".join(
        char for char in unicodedata.normalize("NFKD", str(value or "").casefold())
        if not unicodedata.combining(char)
    ).strip()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _fts_query(text: str) -> str:
    """Génère une requête FTS5 robuste avec wildcards."""
    words = re.findall(r"[0-9\wÀ-ÖØ-öø-ÿ]{2,}", (text or "").lower())
    terms = [w for w in words if w not in _STOPWORDS]
    if not terms:
        return ""
    return " OR ".join(f'"{w}"*' for w in terms[:12])


# ── Détecteur d'entités et extracteur de triplets RDF ────────────────────────

class EntityTripleExtractor:
    """Extraction automatique d'entités (Projets, Logiciels, Personnes, Lieux) et de triplets RDF."""

    # Logiciels, langages, OS et outils courants
    SOFTWARE_PATTERNS = {
        "python": "Python", "rust": "Rust", "golang": "Go", "go": "Go",
        "javascript": "JavaScript", "typescript": "TypeScript", "c++": "C++", "cpp": "C++",
        "docker": "Docker", "podman": "Podman", "kubernetes": "Kubernetes",
        "ffmpeg": "ffmpeg", "git": "Git", "neovim": "Neovim", "nvim": "Neovim",
        "vim": "Vim", "vscode": "VSCode", "emacs": "Emacs",
        "hyprland": "Hyprland", "waybar": "Waybar", "sway": "Sway",
        "linux": "Linux", "arch": "Arch Linux", "archlinux": "Arch Linux",
        "debian": "Debian", "ubuntu": "Ubuntu", "fedora": "Fedora",
        "windows": "Windows", "macos": "macOS", "android": "Android",
        "fastapi": "FastAPI", "flask": "Flask", "qt": "Qt", "pyqt": "PyQt",
        "sqlite": "SQLite", "postgres": "PostgreSQL", "postgresql": "PostgreSQL",
    }

    # Projets connus et expressions de projets
    PROJECT_RE = re.compile(
        r"(?i)\b(?:projet|repo|dépôt|depot|application|app|service|module)\s+([A-Za-z0-9À-ÖØ-öø-ÿ._-]{2,40})"
    )
    KNOWN_PROJECTS = {"ano-gpt", "ano-remote", "anogpt", "jarvis"}

    # Villes / Lieux
    LOCATION_RE = re.compile(
        r"(?i)\b(?:à|a|en|au|dans la ville de|habite à|vit à)\s+([A-ZÀ-ÖØ-ß][a-zà-öø-ÿ]{2,25}(?:-[A-ZÀ-ÖØ-ß][a-zà-öø-ÿ]{2,25})?)"
    )
    KNOWN_LOCATIONS = {"paris", "lyon", "marseille", "bordeaux", "toulouse", "lille", "nantes", "conakry", "bureau", "domicile", "maison"}

    # Personnes
    PERSON_RE = re.compile(
        r"(?i)\b(?:avec|contact|collègue|collegue|ami|parlé à|vu|rencontré)\s+([A-ZÀ-ÖØ-ß][a-zà-öø-ÿ]{2,25})"
    )

    # Remplacement / Contradiction explicite
    CONTRADICTION_RE = re.compile(
        r"(?i)\b(?:n['’]\s*\w+\s+plus|ne\s+\w+\s+plus|plus\s+de|au\s+lieu\s+de|remplace|abandonne|fini\s+avec)\s+([A-Za-z0-9À-ÖØ-öø-ÿ._-]+)"
    )

    @classmethod
    def extract_entities(cls, text: str) -> dict[str, list[str]]:
        """Détecte les entités typées dans un texte."""
        entities: dict[str, list[str]] = {
            "projects": [],
            "software": [],
            "persons": [],
            "locations": [],
        }
        clean_text = " ".join(str(text or "").split())
        folded = _fold(clean_text)

        # 1. Projets
        for m in cls.PROJECT_RE.finditer(clean_text):
            p = m.group(1).strip(".,;:")
            if p.lower() not in {"le", "la", "un", "une", "mon", "ce", "notre"}:
                entities["projects"].append(p)
        for kp in cls.KNOWN_PROJECTS:
            if re.search(rf"\b{kp}\b", folded):
                entities["projects"].append(kp.upper() if "ano" in kp else kp.capitalize())

        # 2. Logiciels / Outils
        for key, canonical in cls.SOFTWARE_PATTERNS.items():
            pattern = rf"\b{re.escape(key)}\b"
            if re.search(pattern, folded):
                if canonical not in entities["software"]:
                    entities["software"].append(canonical)

        # 3. Lieux
        for m in cls.LOCATION_RE.finditer(clean_text):
            loc = m.group(1).strip(".,;:")
            if loc.lower() not in {"un", "une", "le", "la", "ce", "cette"}:
                entities["locations"].append(loc)
        for kl in cls.KNOWN_LOCATIONS:
            if re.search(rf"\b{kl}\b", folded):
                entities["locations"].append(kl.capitalize())

        # 4. Personnes
        for m in cls.PERSON_RE.finditer(clean_text):
            person = m.group(1).strip(".,;:")
            if person.lower() not in {"le", "la", "un", "mon", "son", "sa", "ce"}:
                entities["persons"].append(person)

        # Déduplication
        for k in entities:
            entities[k] = list(dict.fromkeys(entities[k]))

        return entities

    @classmethod
    def extract_triples(
        cls,
        text: str,
        *,
        category: str = "",
        key: str | None = None,
        happened: str | None = None,
        source_entry_id: int | None = None,
        source_id: int | None = None,
    ) -> list[RDFTriple]:
        """Extrait des triplets RDF (Sujet, Prédicat, Objet, Validité, Confiance)."""
        src_id = source_entry_id if source_entry_id is not None else source_id
        triples: list[RDFTriple] = []
        now_ts = happened or _now()
        clean = " ".join(str(text or "").split())
        folded = _fold(clean)
        subject = "utilisateur"

        entities = cls.extract_entities(clean)

        # Détection des entités explicitement niées/abandonnées
        negated_entities: set[str] = set()
        for cm in cls.CONTRADICTION_RE.finditer(clean):
            w = cm.group(1).strip(".,;:")
            canon = cls.SOFTWARE_PATTERNS.get(_fold(w), w)
            negated_entities.add(canon.lower())
            negated_entities.add(w.lower())
            negated_entities.add(_fold(w))

        LANGUAGES = {"Python", "Rust", "Go", "JavaScript", "TypeScript", "C++"}
        OPERATING_SYSTEMS = {"Linux", "Arch Linux", "Debian", "Ubuntu", "Fedora", "Windows", "macOS", "Android"}

        # 1. Préférences de langages ou d'outils
        is_lang_pref = bool(
            (key and any(k in key.lower() for k in ("langage", "lang")))
            or re.search(r"(?i)\b(?:langage\s+(?:préféré|favori|de\s+cœur)|préfère\s+coder\s+en|programme\s+en|code\s+en)\b", clean)
        )
        detected_lang = None
        for sw in entities["software"]:
            if sw in LANGUAGES and sw.lower() not in negated_entities:
                detected_lang = sw
                break
        if not detected_lang and is_lang_pref:
            lang_match = re.search(r"(?i)\b(?:langage\s+(?:préféré|favori)|code\s+en|programme\s+en)\s*(?:est|c'est|reste|soit|:)?\s*([A-Za-z0-9#+]+)", clean)
            if lang_match:
                w = lang_match.group(1).strip(".,;:")
                if w.lower() not in {"le", "la", "un", "une", "mon"} and w.lower() not in negated_entities:
                    detected_lang = cls.SOFTWARE_PATTERNS.get(_fold(w), w)

        if detected_lang and detected_lang.lower() not in negated_entities:
            pred = "langage_favori" if is_lang_pref else "utilise_logiciel"
            triples.append(RDFTriple(
                id=None,
                subject=subject,
                predicate=pred,
                object=detected_lang,
                valid_from=now_ts,
                confidence=0.95 if is_lang_pref else 0.85,
                source_entry_id=src_id,
            ))

        # 2. OS / Système d'exploitation
        is_os_mention = bool(
            (key and "os" in key.lower())
            or re.search(r"(?i)\b(?:utilise|tourne\s+sous|sur|système|os)\b", clean)
        )
        detected_os = None
        for sw in entities["software"]:
            if sw in OPERATING_SYSTEMS and sw.lower() not in negated_entities:
                detected_os = sw
                break
        if detected_os and detected_os.lower() not in negated_entities:
            pred = "os_actuel" if is_os_mention else "utilise_logiciel"
            triples.append(RDFTriple(
                id=None,
                subject=subject,
                predicate=pred,
                object=detected_os,
                valid_from=now_ts,
                confidence=0.95 if is_os_mention else 0.85,
                source_entry_id=src_id,
            ))

        # 3. Projets en cours
        is_proj_mention = bool(
            (key and "projet" in key.lower())
            or re.search(r"(?i)\b(?:travaille\s+sur|développe|developpe|conçoit|projet\s+actuel|focus\s+sur)\b", clean)
        )
        valid_projects = [p for p in entities["projects"] if p.lower() not in negated_entities]
        if valid_projects:
            pred = "projet_actuel" if is_proj_mention else "concerne_projet"
            triples.append(RDFTriple(
                id=None,
                subject=subject,
                predicate=pred,
                object=valid_projects[0],
                valid_from=now_ts,
                confidence=0.95 if is_proj_mention else 0.85,
                source_entry_id=src_id,
            ))

        # 4. Résidence / Localisation
        is_loc_mention = bool(
            (key and any(k in key.lower() for k in ("ville", "habite", "residence", "lieu")))
            or re.search(r"(?i)\b(?:habite|vit|réside|demeure)\s+à\b", clean)
        )
        valid_locations = [l for l in entities["locations"] if l.lower() not in negated_entities]
        if valid_locations:
            pred = "habite_a" if is_loc_mention else "se_trouve_a"
            triples.append(RDFTriple(
                id=None,
                subject=subject,
                predicate=pred,
                object=valid_locations[0],
                valid_from=now_ts,
                confidence=0.95 if is_loc_mention else 0.8,
                source_entry_id=src_id,
            ))

        # 5. Liaisons avec entités détectées (générique)
        for sw in entities["software"]:
            if sw.lower() not in negated_entities and not any(t.object == sw for t in triples):
                pred = "utilise_logiciel" if "linux" not in sw.lower() else "utilise_os"
                triples.append(RDFTriple(
                    id=None,
                    subject=subject,
                    predicate=pred,
                    object=sw,
                    valid_from=now_ts,
                    confidence=0.8,
                    source_entry_id=src_id,
                ))

        for pr in valid_projects:
            if not any(t.object == pr for t in triples):
                triples.append(RDFTriple(
                    id=None,
                    subject=subject,
                    predicate="concerne_projet",
                    object=pr,
                    valid_from=now_ts,
                    confidence=0.85,
                    source_entry_id=src_id,
                ))

        for person in entities["persons"]:
            if person.lower() not in negated_entities:
                triples.append(RDFTriple(
                    id=None,
                    subject=subject,
                    predicate="collabore_avec",
                    object=person,
                    valid_from=now_ts,
                    confidence=0.8,
                    source_entry_id=src_id,
                ))

        # Si clé explicite sous forme de relation (ex: key="langage_favori")
        if key and "_" in key and not triples:
            parts = key.split("_", 1)
            triples.append(RDFTriple(
                id=None,
                subject=subject,
                predicate=key,
                object=clean[:120],
                valid_from=now_ts,
                confidence=1.0,
                source_entry_id=src_id,
            ))

        return triples


# ── Base de données Vectorielle et Graphe RDF ────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,
    key          TEXT,
    value        TEXT NOT NULL,
    category     TEXT,
    metadata     TEXT NOT NULL DEFAULT '{}',
    valid_from   TEXT NOT NULL,
    valid_until  TEXT,
    is_active    INTEGER NOT NULL DEFAULT 1,
    confidence   REAL NOT NULL DEFAULT 1.0,
    hits         INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    happened_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_entries_kind ON entries(kind, is_active, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_entries_key ON entries(kind, key) WHERE key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_entries_active ON entries(is_active, valid_until);

CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
    key, value, category,
    content='entries', content_rowid='id',
    tokenize="unicode61 remove_diacritics 2"
);

CREATE TRIGGER IF NOT EXISTS entries_ai AFTER INSERT ON entries BEGIN
    INSERT INTO entries_fts(rowid, key, value, category)
    VALUES (new.id, new.key, new.value, new.category);
END;

CREATE TRIGGER IF NOT EXISTS entries_ad AFTER DELETE ON entries BEGIN
    INSERT INTO entries_fts(entries_fts, rowid, key, value, category)
    VALUES ('delete', old.id, old.key, old.value, old.category);
END;

CREATE TRIGGER IF NOT EXISTS entries_au AFTER UPDATE ON entries BEGIN
    INSERT INTO entries_fts(entries_fts, rowid, key, value, category)
    VALUES ('delete', old.id, old.key, old.value, old.category);
    INSERT INTO entries_fts(rowid, key, value, category)
    VALUES (new.id, new.key, new.value, new.category);
END;

CREATE TABLE IF NOT EXISTS rdf_triples (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    subject         TEXT NOT NULL,
    predicate       TEXT NOT NULL,
    object          TEXT NOT NULL,
    valid_from      TEXT NOT NULL,
    valid_until     TEXT,
    is_active       INTEGER NOT NULL DEFAULT 1,
    confidence      REAL NOT NULL DEFAULT 1.0,
    source_entry_id INTEGER REFERENCES entries(id) ON DELETE CASCADE,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_triples_sub_pred ON rdf_triples(subject, predicate, is_active);
CREATE INDEX IF NOT EXISTS idx_triples_obj ON rdf_triples(object, is_active);
CREATE INDEX IF NOT EXISTS idx_triples_active ON rdf_triples(is_active, valid_until);

CREATE VIRTUAL TABLE IF NOT EXISTS triples_fts USING fts5(
    subject, predicate, object,
    content='rdf_triples', content_rowid='id',
    tokenize="unicode61 remove_diacritics 2"
);

CREATE TRIGGER IF NOT EXISTS triples_ai AFTER INSERT ON rdf_triples BEGIN
    INSERT INTO triples_fts(rowid, subject, predicate, object)
    VALUES (new.id, new.subject, new.predicate, new.object);
END;

CREATE TRIGGER IF NOT EXISTS triples_ad AFTER DELETE ON rdf_triples BEGIN
    INSERT INTO triples_fts(triples_fts, rowid, subject, predicate, object)
    VALUES ('delete', old.id, old.subject, old.predicate, old.object);
END;

CREATE TRIGGER IF NOT EXISTS triples_au AFTER UPDATE ON rdf_triples BEGIN
    INSERT INTO triples_fts(triples_fts, rowid, subject, predicate, object)
    VALUES ('delete', old.id, old.subject, old.predicate, old.object);
    INSERT INTO triples_fts(rowid, subject, predicate, object)
    VALUES (new.id, new.subject, new.predicate, new.object);
END;

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


class VectorMemory:
    """Gestionnaire unifié de mémoire vectorielle sqlite-vec et graphe RDF."""

    def __init__(self, db_path: Path | str | None = None, model: EmbeddingEngine | None = None):
        self.db_path = Path(db_path) if db_path else DB_PATH
        self.model = model or EmbeddingEngine.get_instance()
        self._lock = threading.RLock()
        self._init_db()
        if self.db_path.resolve() == DB_PATH.resolve():
            self._migrate_legacy_db_once()

    def _get_connection(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")

        # Chargement de sqlite-vec
        if sqlite_vec is not None:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
        return conn

    def _init_db(self) -> None:
        with self._lock, self._get_connection() as conn:
            conn.executescript(_SCHEMA)

            # Création des tables virtuelles vec0 sqlite-vec
            if sqlite_vec is not None:
                conn.execute(f"""
                    CREATE VIRTUAL TABLE IF NOT EXISTS vec_entries USING vec0(
                        id INTEGER PRIMARY KEY,
                        embedding float[{EMBEDDING_DIM}] distance_metric=cosine
                    );
                """)
                conn.execute(f"""
                    CREATE VIRTUAL TABLE IF NOT EXISTS vec_triples USING vec0(
                        id INTEGER PRIMARY KEY,
                        embedding float[{EMBEDDING_DIM}] distance_metric=cosine
                    );
                """)
            conn.commit()

    def _migrate_legacy_db_once(self) -> None:
        """Importe l'ancien magasin FTS sans doublons, une seule fois.

        Une interruption est sans danger : ``save`` déduplique chaque ligne
        et le marqueur n'est écrit qu'après le dernier souvenir importé.
        """
        if not LEGACY_DB_PATH.is_file() or LEGACY_DB_PATH.resolve() == self.db_path.resolve():
            return
        with self._lock, self._get_connection() as conn:
            done = conn.execute(
                "SELECT v FROM meta WHERE k='legacy_memory_migrated'"
            ).fetchone()
        if done:
            return

        source = sqlite3.connect(LEGACY_DB_PATH, timeout=5.0)
        source.row_factory = sqlite3.Row
        try:
            rows = source.execute(
                "SELECT kind, key, value, category, happened FROM memories ORDER BY id"
            ).fetchall()
        except sqlite3.Error as exc:
            logger.warning("Migration mémoire historique impossible : %s", exc)
            return
        finally:
            source.close()

        for row in rows:
            try:
                self.save(
                    row["value"], kind=row["kind"], key=row["key"],
                    category=row["category"], happened=row["happened"],
                )
            except Exception as exc:
                logger.warning("Souvenir historique ignoré pendant la migration : %s", exc)

        with self._lock, self._get_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta(k, v) VALUES('legacy_memory_migrated', ?)",
                (_now(),),
            )
            conn.commit()

    # ── Gestion des contradictions et triplets ──────────────────────────────

    def resolve_contradictions(
        self,
        conn: sqlite3.Connection,
        new_triple: RDFTriple,
        timestamp: str,
    ) -> list[int]:
        """Invalide les faits contradictoires (is_active = 0, valid_until = timestamp)."""
        invalidated_ids: list[int] = []

        # 1. Contradiction sur prédicats fonctionnels (à valeur unique pour un sujet donné)
        if new_triple.predicate in FUNCTIONAL_PREDICATES:
            conflicts = conn.execute(
                """SELECT id, object FROM rdf_triples
                   WHERE subject = ? AND predicate = ? AND is_active = 1""",
                (new_triple.subject, new_triple.predicate),
            ).fetchall()

            for row in conflicts:
                if _fold(row["object"]) != _fold(new_triple.object):
                    cid = int(row["id"])
                    conn.execute(
                        """UPDATE rdf_triples
                           SET is_active = 0, valid_until = ?, updated_at = ?
                           WHERE id = ?""",
                        (timestamp, timestamp, cid),
                    )
                    invalidated_ids.append(cid)
                    logger.info(
                        "Contradiction résolue : (%s, %s, %s) invalidé par (%s, %s, %s)",
                        new_triple.subject, new_triple.predicate, row["object"],
                        new_triple.subject, new_triple.predicate, new_triple.object,
                    )

        # 2. Contradiction par remplacement de projet ou outil via regex explicite
        if new_triple.predicate in {"utilise_os", "utilise_logiciel", "projet_actuel"}:
            matches = conn.execute(
                """SELECT id, object FROM rdf_triples
                   WHERE subject = ? AND predicate = ? AND is_active = 1 AND object != ?""",
                (new_triple.subject, new_triple.predicate, new_triple.object),
            ).fetchall()
            # Pour l'OS et le projet_actuel, remplacer l'ancien
            if new_triple.predicate in {"utilise_os", "projet_actuel"}:
                for row in matches:
                    cid = int(row["id"])
                    conn.execute(
                        """UPDATE rdf_triples
                           SET is_active = 0, valid_until = ?, updated_at = ?
                           WHERE id = ?""",
                        (timestamp, timestamp, cid),
                    )
                    invalidated_ids.append(cid)

        return invalidated_ids

    def add_triple(
        self,
        triple: RDFTriple,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        """Enregistre un triplet RDF avec embedding vectoriel et détection des contradictions."""
        now = _now()
        ts = triple.valid_from or now
        triple_text = triple.text_representation()

        def _do(c: sqlite3.Connection) -> int:
            self.resolve_contradictions(c, triple, ts)

            # Vérifier si triplet identique actif existe
            existing = c.execute(
                """SELECT id FROM rdf_triples
                   WHERE subject = ? AND predicate = ? AND object = ? AND is_active = 1""",
                (triple.subject, triple.predicate, triple.object),
            ).fetchone()
            if existing:
                return int(existing["id"])

            cur = c.execute(
                """INSERT INTO rdf_triples
                   (subject, predicate, object, valid_from, valid_until, is_active,
                    confidence, source_entry_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    triple.subject, triple.predicate, triple.object,
                    ts, triple.valid_until, 1 if triple.is_active else 0,
                    float(triple.confidence), triple.source_entry_id, now, now,
                ),
            )
            triple_id = cur.lastrowid

            # Indexation vectorielle du triplet dans vec_triples
            if sqlite_vec is not None:
                try:
                    vec = self.model.encode(triple_text)
                    blob = self.model.serialize(vec)
                    c.execute("DELETE FROM vec_triples WHERE id = ?", (triple_id,))
                    c.execute("INSERT INTO vec_triples(id, embedding) VALUES (?, ?)", (triple_id, blob))
                except Exception as exc:
                    logger.warning("Échec indexation vectorielle du triplet %d : %s", triple_id, exc)

            return triple_id

        if conn is not None:
            return _do(conn)
        with self._lock, self._get_connection() as c:
            tid = _do(c)
            c.commit()
            return tid

    # ── Écriture et Ingestion de Souvenirs ────────────────────────────────────

    def save(
        self,
        value: str,
        *,
        kind: str = KIND_FACT,
        key: str | None = None,
        category: str | None = None,
        happened: str | None = None,
        confidence: float = 1.0,
        metadata: dict | None = None,
    ) -> str:
        """Enregistre un souvenir, extrait ses triplets RDF et met à jour l'index vectoriel."""
        clean_val = " ".join((value or "").split())[:MAX_VALUE_CHARS]
        if not clean_val:
            return "Rien à mémoriser."

        kind = kind if kind in KINDS else KIND_FACT
        norm_key = "_".join((key or "").lower().split()) or None
        now = _now()
        h_date = happened or (_today() if kind != KIND_PROFILE else None)
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)

        with self._lock, self._get_connection() as conn:
            entry_id: int | None = None
            msg = "C'est noté."

            # Dédoublonnage sur clé ou valeur exacte
            if norm_key:
                existing = conn.execute(
                    "SELECT id, value FROM entries WHERE kind=? AND key=?",
                    (kind, norm_key),
                ).fetchone()
                if existing:
                    entry_id = int(existing["id"])
                    if existing["value"] == clean_val:
                        return "C'était déjà noté."
                    conn.execute(
                        """UPDATE entries
                           SET value=?, category=COALESCE(?, category),
                               metadata=?, updated_at=?, happened_at=COALESCE(?, happened_at),
                               is_active=1, valid_until=NULL
                           WHERE id=?""",
                        (clean_val, category, meta_json, now, h_date, entry_id),
                    )
                    msg = "Noté, je remplace ce que je savais."
            else:
                dup = conn.execute(
                    "SELECT id FROM entries WHERE kind=? AND value=?",
                    (kind, clean_val),
                ).fetchone()
                if dup:
                    entry_id = int(dup["id"])
                    conn.execute(
                        "UPDATE entries SET updated_at=?, is_active=1, valid_until=NULL WHERE id=?",
                        (now, entry_id),
                    )
                    return "C'était déjà noté."

            if entry_id is None:
                cur = conn.execute(
                    """INSERT INTO entries
                       (kind, key, value, category, metadata, valid_from, is_active,
                        confidence, created_at, updated_at, happened_at)
                       VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)""",
                    (
                        kind, norm_key, clean_val, category, meta_json,
                        now, float(confidence), now, now, h_date,
                    ),
                )
                entry_id = cur.lastrowid

            # Calcul et stockage de l'embedding dans vec_entries
            if sqlite_vec is not None and entry_id:
                try:
                    embed_text = f"{norm_key or ''} {clean_val}".strip()
                    vec = self.model.encode(embed_text)
                    blob = self.model.serialize(vec)
                    conn.execute("DELETE FROM vec_entries WHERE id = ?", (entry_id,))
                    conn.execute(
                        "INSERT INTO vec_entries(id, embedding) VALUES (?, ?)",
                        (entry_id, blob),
                    )
                except Exception as exc:
                    logger.warning("Échec de l'indexation vectorielle du souvenir %d : %s", entry_id, exc)

            # Invalidation explicite d'entités contredites ou abandonnées
            for cm in EntityTripleExtractor.CONTRADICTION_RE.finditer(clean_val):
                neg_word = cm.group(1).strip(".,;:")
                canon_neg = EntityTripleExtractor.SOFTWARE_PATTERNS.get(_fold(neg_word), neg_word)
                conn.execute(
                    """UPDATE rdf_triples
                       SET is_active = 0, valid_until = ?, updated_at = ?
                       WHERE is_active = 1 AND (LOWER(object) = LOWER(?) OR LOWER(object) = LOWER(?))""",
                    (now, now, neg_word, canon_neg),
                )

            # Extraction et sauvegarde des triplets RDF
            triples = EntityTripleExtractor.extract_triples(
                clean_val,
                category=category or "",
                key=norm_key,
                happened=h_date or now,
                source_entry_id=entry_id,
            )
            for t in triples:
                self.add_triple(t, conn=conn)

            conn.commit()
            return msg

    def save_turn(
        self,
        user_text: str,
        assistant_text: str,
        *,
        happened: str | None = None,
    ) -> str:
        """Archive un tour de parole clé avec ses entités et son vecteur sémantique."""
        val = f"Utilisateur : {user_text.strip()}\nANO-GPT : {assistant_text.strip()}"
        return self.save(
            val,
            kind=KIND_TURN,
            category="conversation",
            happened=happened or _now(),
        )

    def save_preference(
        self,
        key: str,
        value: str,
        category: str = "preferences",
    ) -> str:
        """Enregistre explicitement une préférence utilisateur."""
        return self.save(
            value,
            kind=KIND_PREFERENCE,
            key=key,
            category=category,
            confidence=1.0,
        )

    # ── Recherche Hybride RRF (BM25 + Cosinus sqlite-vec) ────────────────────

    def hybrid_search(
        self,
        query: str,
        *,
        limit: int = 5,
        kinds: Sequence[str] | None = None,
        active_only: bool = True,
        k_rrf: int = K_RRF_DEFAULT,
        w_bm25: float = 1.0,
        w_vec: float = 1.0,
        exclude_ids: Iterable[int] = (),
    ) -> list[MemoryEntry]:
        """Fusion RRF (Reciprocal Rank Fusion) combinant le mot-clé exact (BM25) et le vecteur cosinus."""
        query_text = (query or "").strip()
        if not query_text:
            return []

        excluded = {int(i) for i in exclude_ids}

        with self._lock, self._get_connection() as conn:
            # 1. Classement BM25 via FTS5
            bm25_ranks: dict[int, int] = {}
            match_clause = _fts_query(query_text)
            if match_clause:
                sql_bm25 = [
                    "SELECT m.id, bm25(entries_fts, 2.0, 1.0, 0.5) AS bm_score",
                    "FROM entries_fts JOIN entries m ON m.id = entries_fts.rowid",
                    "WHERE entries_fts MATCH ?",
                ]
                params_bm25: list[Any] = [match_clause]
                if active_only:
                    sql_bm25.append("AND m.is_active = 1")
                if kinds:
                    sql_bm25.append("AND m.kind IN (%s)" % ",".join("?" * len(kinds)))
                    params_bm25.extend(kinds)
                sql_bm25.append("ORDER BY bm_score ASC LIMIT 50")

                try:
                    rows = conn.execute(" ".join(sql_bm25), params_bm25).fetchall()
                    for rank_idx, row in enumerate(rows, 1):
                        bm25_ranks[int(row["id"])] = rank_idx
                except sqlite3.OperationalError:
                    pass

            # 2. Classement Vectoriel Cosinus via sqlite-vec
            vec_ranks: dict[int, int] = {}
            vec_dists: dict[int, float] = {}
            if sqlite_vec is not None:
                try:
                    q_vec = self.model.encode(query_text)
                    q_blob = self.model.serialize(q_vec)
                    v_rows = conn.execute(
                        """SELECT id, distance
                           FROM vec_entries
                           WHERE embedding MATCH ? AND k = 50
                           ORDER BY distance ASC""",
                        (q_blob,),
                    ).fetchall()

                    for rank_idx, r in enumerate(v_rows, 1):
                        eid = int(r["id"])
                        vec_ranks[eid] = rank_idx
                        vec_dists[eid] = float(r["distance"])
                except Exception as exc:
                    logger.warning("Échec recherche vectorielle sqlite-vec : %s", exc)

            # 3. Calcul du score de fusion RRF
            all_candidate_ids = (set(bm25_ranks.keys()) | set(vec_ranks.keys())) - excluded
            if not all_candidate_ids:
                return []

            marks = ",".join("?" * len(all_candidate_ids))
            sql_fetch = [
                f"SELECT * FROM entries WHERE id IN ({marks})",
            ]
            params_fetch: list[Any] = list(all_candidate_ids)
            if active_only:
                sql_fetch.append("AND is_active = 1")
            if kinds:
                sql_fetch.append("AND kind IN (%s)" % ",".join("?" * len(kinds)))
                params_fetch.extend(kinds)

            entry_rows = conn.execute(" ".join(sql_fetch), params_fetch).fetchall()

            scored_entries: list[MemoryEntry] = []
            for row in entry_rows:
                eid = int(row["id"])
                r_bm = bm25_ranks.get(eid)
                r_vec = vec_ranks.get(eid)

                # Formule RRF : score = w_bm / (k + r_bm) + w_vec / (k + r_vec)
                score = 0.0
                if r_bm is not None:
                    score += w_bm25 / (k_rrf + r_bm)
                if r_vec is not None:
                    score += w_vec / (k_rrf + r_vec)

                # Priorisation des préférences et profils
                if row["kind"] in {KIND_PROFILE, KIND_PREFERENCE}:
                    score *= 1.25

                meta = {}
                try:
                    meta = json.loads(row["metadata"] or "{}")
                except Exception:
                    pass

                scored_entries.append(MemoryEntry(
                    id=eid,
                    kind=str(row["kind"]),
                    key=row["key"],
                    value=str(row["value"]),
                    category=row["category"],
                    metadata=meta,
                    valid_from=str(row["valid_from"]),
                    valid_until=row["valid_until"],
                    is_active=bool(row["is_active"]),
                    confidence=float(row["confidence"]),
                    hits=int(row["hits"]),
                    created_at=str(row["created_at"]),
                    updated_at=str(row["updated_at"]),
                    happened_at=row["happened_at"],
                    score_rrf=score,
                    bm25_rank=r_bm,
                    vec_rank=r_vec,
                    vec_distance=vec_dists.get(eid),
                ))

            # Tri descendant par score RRF
            scored_entries.sort(key=lambda item: item.score_rrf, reverse=True)
            results = scored_entries[:max(1, int(limit))]

            # Incrémentation des hits pour les souvenirs remontés
            if results:
                conn.execute(
                    "UPDATE entries SET hits = hits + 1 WHERE id IN (%s)"
                    % ",".join("?" * len(results)),
                    [item.id for item in results],
                )
                conn.commit()

            return results

    def search_active_triples(
        self,
        query: str,
        *,
        limit: int = 5,
        k_rrf: int = K_RRF_DEFAULT,
    ) -> list[tuple[RDFTriple, float]]:
        """Recherche hybride RRF directement sur les triplets RDF du graphe."""
        clean_q = (query or "").strip()
        if not clean_q:
            return []

        with self._lock, self._get_connection() as conn:
            bm25_ranks: dict[int, int] = {}
            match_clause = _fts_query(clean_q)
            if match_clause:
                try:
                    rows = conn.execute(
                        """SELECT t.id, bm25(triples_fts, 1.5, 1.0, 1.5) score
                           FROM triples_fts JOIN rdf_triples t ON t.id = triples_fts.rowid
                           WHERE triples_fts MATCH ? AND t.is_active = 1
                           ORDER BY score ASC LIMIT 30""",
                        (match_clause,),
                    ).fetchall()
                    for idx, r in enumerate(rows, 1):
                        bm25_ranks[int(r["id"])] = idx
                except sqlite3.OperationalError:
                    pass

            vec_ranks: dict[int, int] = {}
            if sqlite_vec is not None:
                try:
                    vec = self.model.encode(clean_q)
                    blob = self.model.serialize(vec)
                    v_rows = conn.execute(
                        """SELECT id, distance FROM vec_triples
                           WHERE embedding MATCH ? AND k = 30
                           ORDER BY distance ASC""",
                        (blob,),
                    ).fetchall()
                    for idx, r in enumerate(v_rows, 1):
                        vec_ranks[int(r["id"])] = idx
                except Exception as exc:
                    logger.warning("Erreur vec_triples : %s", exc)

            candidate_ids = set(bm25_ranks.keys()) | set(vec_ranks.keys())
            if not candidate_ids:
                return []

            marks = ",".join("?" * len(candidate_ids))
            triples_rows = conn.execute(
                f"""SELECT * FROM rdf_triples WHERE id IN ({marks}) AND is_active = 1""",
                list(candidate_ids),
            ).fetchall()

            scored: list[tuple[RDFTriple, float]] = []
            for r in triples_rows:
                tid = int(r["id"])
                r_bm = bm25_ranks.get(tid)
                r_vec = vec_ranks.get(tid)
                score = 0.0
                if r_bm is not None:
                    score += 1.0 / (k_rrf + r_bm)
                if r_vec is not None:
                    score += 1.0 / (k_rrf + r_vec)

                scored.append((
                    RDFTriple(
                        id=tid,
                        subject=str(r["subject"]),
                        predicate=str(r["predicate"]),
                        object=str(r["object"]),
                        valid_from=str(r["valid_from"]),
                        valid_until=r["valid_until"],
                        is_active=bool(r["is_active"]),
                        confidence=float(r["confidence"]),
                        source_entry_id=r["source_entry_id"],
                        created_at=str(r["created_at"]),
                        updated_at=str(r["updated_at"]),
                    ),
                    score,
                ))

            scored.sort(key=lambda x: x[1], reverse=True)
            return scored[:limit]

    # ── Injection Contexte Gemini Live (Top 3 faits) ─────────────────────────

    def recall_facts_for_prompt(
        self,
        query: str,
        *,
        limit: int = 3,
        exclude_ids: Iterable[int] = (),
    ) -> tuple[str, list[int]]:
        """Sélectionne et formate les 3 faits/souvenirs les plus pertinents pour Gemini Live."""
        memories = self.hybrid_search(query, limit=limit, exclude_ids=exclude_ids)
        if not memories:
            return "", []

        lines = ["[MÉMOIRE SÉMANTIQUE — CONTEXTE ACTIF]"]
        recalled_ids: list[int] = []

        for m in memories:
            recalled_ids.append(m.id)
            key_part = f"{m.key.replace('_', ' ')} : " if m.key and m.key.lower() not in m.value.lower() else ""
            when_part = f" ({m.happened_at[:10]})" if m.happened_at and m.kind != KIND_PROFILE else ""
            prefix = "· "
            if m.kind == KIND_PREFERENCE:
                prefix = "· [Préférence] "
            elif m.kind == KIND_PROFILE:
                prefix = "· [Profil] "

            lines.append(f"{prefix}{key_part}{m.value}{when_part}")

        lines.append(
            "(Utilise ces souvenirs pour contextualiser ta réponse sans jamais les réciter ni dire que tu les as en mémoire)"
        )
        return "\n".join(lines), recalled_ids

    def recall_block(
        self,
        query: str,
        *,
        limit: int = 3,
        exclude_ids: Iterable[int] = (),
    ) -> tuple[str, list[int]]:
        """Compatibilité descendante avec l'interface memory_store.recall_block."""
        return self.recall_facts_for_prompt(query, limit=limit, exclude_ids=exclude_ids)

    def session_block(self, max_chars: int = 1600) -> str:
        """Bloc initial de session injecté à l'ouverture de session Gemini Live."""
        with self._lock, self._get_connection() as conn:
            profiles = conn.execute(
                """SELECT key, value FROM entries
                   WHERE kind IN (?, ?) AND is_active = 1
                   ORDER BY hits DESC, updated_at DESC LIMIT 20""",
                (KIND_PROFILE, KIND_PREFERENCE),
            ).fetchall()

            facts = conn.execute(
                """SELECT key, value, happened_at FROM entries
                   WHERE kind = ? AND is_active = 1
                   ORDER BY updated_at DESC LIMIT 5""",
                (KIND_FACT,),
            ).fetchall()

            triples = conn.execute(
                """SELECT subject, predicate, object FROM rdf_triples
                   WHERE is_active = 1 AND predicate IN (?, ?, ?, ?)
                   ORDER BY confidence DESC, updated_at DESC LIMIT 5""",
                ("langage_favori", "os_actuel", "projet_actuel", "habite_a"),
            ).fetchall()

        parts: list[str] = []

        if profiles:
            parts.append("[CE QUE TU SAIS DE LUI]")
            for p in profiles:
                k = (p["key"] or "").replace("_", " ")
                prefix = f"{k} : " if k and k.lower() not in p["value"].lower() else ""
                parts.append(f"· {prefix}{p['value']}")

        if triples:
            parts.append("\n[FAITS CLÉS DU GRAPH]")
            for t in triples:
                pred_label = t["predicate"].replace("_", " ")
                parts.append(f"· {pred_label} → {t['object']}")

        if facts:
            parts.append("\n[FAITS RÉCENTS]")
            for f in facts:
                d = f" ({f['happened_at'][:10]})" if f["happened_at"] else ""
                parts.append(f"· {f['value']}{d}")

        if not parts:
            return ""

        parts.append(
            "\nCes souvenirs servent à comprendre, pas à réciter. Utilise-les uniquement "
            "quand ils rendent ta réponse plus juste."
        )
        return "\n".join(parts)[:max_chars]

    # ── Fonctions de consultation et suppression ─────────────────────────────

    def forget(self, key: str) -> str:
        """Supprime un souvenir par sa clé et invalide les triplets associés."""
        norm_key = "_".join((key or "").lower().split())
        with self._lock, self._get_connection() as conn:
            entry = conn.execute("SELECT id FROM entries WHERE key = ?", (norm_key,)).fetchone()
            if not entry:
                return "Je n'avais rien sous ce nom."

            eid = int(entry["id"])
            conn.execute("DELETE FROM entries WHERE id = ?", (eid,))
            if sqlite_vec is not None:
                conn.execute("DELETE FROM vec_entries WHERE id = ?", (eid,))
            conn.execute("DELETE FROM rdf_triples WHERE source_entry_id = ?", (eid,))
            conn.commit()
            return "Oublié."

    def list_memories(self, limit: int = 200, active_only: bool = True) -> list[dict]:
        """Vue structurée pour le panneau mémoire ANO-GPT."""
        with self._lock, self._get_connection() as conn:
            sql = ["SELECT id, kind, key, value, category, is_active, updated_at, happened_at, hits FROM entries"]
            if active_only:
                sql.append("WHERE is_active = 1")
            sql.append("ORDER BY updated_at DESC, id DESC LIMIT ?")
            rows = conn.execute(" ".join(sql), (max(1, min(int(limit), 1000)),)).fetchall()
            return [dict(r) for r in rows]

    def list_triples(self, limit: int = 100, active_only: bool = True) -> list[dict]:
        """Liste les triplets RDF actifs du graphe de connaissances."""
        with self._lock, self._get_connection() as conn:
            sql = ["SELECT id, subject, predicate, object, valid_from, valid_until, is_active, confidence FROM rdf_triples"]
            if active_only:
                sql.append("WHERE is_active = 1")
            sql.append("ORDER BY updated_at DESC LIMIT ?")
            rows = conn.execute(" ".join(sql), (limit,)).fetchall()
            return [dict(r) for r in rows]

    def stats(self) -> dict[str, int]:
        """Statistiques du magasin vectoriel et RDF."""
        with self._lock, self._get_connection() as conn:
            e_count = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
            e_active = conn.execute("SELECT COUNT(*) FROM entries WHERE is_active = 1").fetchone()[0]
            t_count = conn.execute("SELECT COUNT(*) FROM rdf_triples").fetchone()[0]
            t_active = conn.execute("SELECT COUNT(*) FROM rdf_triples WHERE is_active = 1").fetchone()[0]
            vec_count = 0
            if sqlite_vec is not None:
                vec_count = conn.execute("SELECT COUNT(*) FROM vec_entries").fetchone()[0]
            return {
                "entries_total": int(e_count),
                "entries_active": int(e_active),
                "triples_total": int(t_count),
                "triples_active": int(t_active),
                "vectors_total": int(vec_count),
            }


# ── Instance Singleton et Fonctions Top-Level ───────────────────────────────

_DEFAULT_VM: VectorMemory | None = None
_VM_LOCK = threading.Lock()


def get_vector_memory(db_path: Path | str | None = None) -> VectorMemory:
    """Retourne l'instance globale de la mémoire vectorielle."""
    global _DEFAULT_VM
    if _DEFAULT_VM is None:
        with _VM_LOCK:
            if _DEFAULT_VM is None:
                _DEFAULT_VM = VectorMemory(db_path=db_path)
    return _DEFAULT_VM


def save(
    value: str,
    *,
    kind: str = KIND_FACT,
    key: str | None = None,
    category: str | None = None,
    happened: str | None = None,
    confidence: float = 1.0,
) -> str:
    return get_vector_memory().save(
        value,
        kind=kind,
        key=key,
        category=category,
        happened=happened,
        confidence=confidence,
    )


def search(
    query: str,
    *,
    limit: int = 5,
    kinds: Sequence[str] | None = None,
    exclude_ids: Iterable[int] = (),
) -> list[dict]:
    results = get_vector_memory().hybrid_search(
        query,
        limit=limit,
        kinds=kinds,
        exclude_ids=exclude_ids,
    )
    return [
        {
            "id": r.id,
            "kind": r.kind,
            "key": r.key,
            "value": r.value,
            "category": r.category,
            "happened": r.happened_at,
            "score": r.score_rrf,
        }
        for r in results
    ]


def recall_block(
    query: str,
    *,
    limit: int = 3,
    exclude_ids: Iterable[int] = (),
) -> tuple[str, list[int]]:
    return get_vector_memory().recall_facts_for_prompt(
        query,
        limit=limit,
        exclude_ids=exclude_ids,
    )


def session_block(max_chars: int = 1600) -> str:
    return get_vector_memory().session_block(max_chars=max_chars)


def forget(key: str) -> str:
    return get_vector_memory().forget(key)


def list_memories(limit: int = 200) -> list[dict]:
    return get_vector_memory().list_memories(limit=limit)
