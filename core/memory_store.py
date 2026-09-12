"""core/memory_store.py — La mémoire longue durée d'ANO-GPT.

Trois natures de souvenirs, volontairement séparées parce qu'on ne s'en sert
pas au même moment :

* **profil** — ce qui est vrai en permanence (son prénom, sa machine, ses
  préférences). Injecté une fois à l'ouverture de la session : le modèle doit
  le savoir avant même la première phrase.
* **fait** — ce qui s'est passé, avec une date (« projet livré le 12 août »,
  « rendez-vous jeudi »). Trop nombreux pour tenir dans le prompt : on ne les
  ressort que quand la conversation les touche.
* **épisode** — deux lignes qui résument une conversation passée. C'est ce qui
  permet de dire « comme hier soir » au lieu de repartir de zéro.

Le moteur est SQLite avec FTS5. Pas d'embeddings : sur deux cœurs partagés
avec la boucle audio, une recherche vectorielle se paierait sur la voix, alors
qu'une requête FTS5 sur quelques milliers de lignes coûte moins d'une
milliseconde.

Chaque appel ouvre sa propre connexion. Le module est traversé par le thread
Qt, la boucle asyncio et des threads de fond ; une connexion partagée
demanderait un verrou global pour un gain nul à ce volume.
"""

from __future__ import annotations

import re
import sqlite3
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional, Sequence


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = _base_dir()
DB_PATH = BASE_DIR / "memory" / "memory.db"
_PRODUCTION_DB_PATH = DB_PATH
LEGACY_JSON = BASE_DIR / "memory" / "long_term.json"

KIND_PROFILE = "profil"
KIND_FACT = "fait"
KIND_EPISODE = "episode"
KINDS = (KIND_PROFILE, KIND_FACT, KIND_EPISODE)

# Un souvenir qui déborde n'est plus un souvenir, c'est un document : le modèle
# le lit mal et il mange la place des autres.
MAX_VALUE_CHARS = 400

# Les catégories héritées du fichier JSON, réparties selon qu'elles décrivent
# une constante de la personne ou un événement.
_PROFILE_CATEGORIES = {"identity", "preferences", "relationships", "profile"}

_INIT_LOCK = threading.Lock()
_INITIALISED = False

# Mots trop fréquents pour discriminer quoi que ce soit : les garder dans la
# requête FTS remonte n'importe quel souvenir avec le même bruit.
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


# ── infrastructure ──────────────────────────────────────────────────────────

def _connect() -> sqlite3.Connection:
    _ensure_schema()
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id       INTEGER PRIMARY KEY,
    kind     TEXT NOT NULL,
    key      TEXT,
    value    TEXT NOT NULL,
    category TEXT,
    aliases  TEXT,
    created  TEXT NOT NULL,
    updated  TEXT NOT NULL,
    happened TEXT,
    hits     INTEGER NOT NULL DEFAULT 0
);

-- Une clé identifie un souvenir : le réenregistrer met à jour au lieu
-- d'empiler dix versions du prénom.
CREATE UNIQUE INDEX IF NOT EXISTS memories_key
    ON memories(kind, key) WHERE key IS NOT NULL;
CREATE INDEX IF NOT EXISTS memories_kind ON memories(kind, updated DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    key, value, category, aliases,
    content='memories', content_rowid='id',
    tokenize="unicode61 remove_diacritics 2"
);

CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, key, value, category, aliases)
    VALUES (new.id, new.key, new.value, new.category, new.aliases);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, key, value, category, aliases)
    VALUES ('delete', old.id, old.key, old.value, old.category, old.aliases);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, key, value, category, aliases)
    VALUES ('delete', old.id, old.key, old.value, old.category, old.aliases);
    INSERT INTO memories_fts(rowid, key, value, category, aliases)
    VALUES (new.id, new.key, new.value, new.category, new.aliases);
END;

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""

_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    key, value, category, aliases,
    content='memories', content_rowid='id',
    tokenize="unicode61 remove_diacritics 2"
);

CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, key, value, category, aliases)
    VALUES (new.id, new.key, new.value, new.category, new.aliases);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, key, value, category, aliases)
    VALUES ('delete', old.id, old.key, old.value, old.category, old.aliases);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, key, value, category, aliases)
    VALUES ('delete', old.id, old.key, old.value, old.category, old.aliases);
    INSERT INTO memories_fts(rowid, key, value, category, aliases)
    VALUES (new.id, new.key, new.value, new.category, new.aliases);
END;
"""


def _ensure_schema() -> None:
    global _INITIALISED
    if _INITIALISED:
        return
    with _INIT_LOCK:
        if _INITIALISED:
            return
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=5.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)

            # Migration de la colonne aliases si absente
            cols = [r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()]
            if "aliases" not in cols:
                conn.execute("ALTER TABLE memories ADD COLUMN aliases TEXT;")
                conn.execute("DROP TRIGGER IF EXISTS memories_ai;")
                conn.execute("DROP TRIGGER IF EXISTS memories_ad;")
                conn.execute("DROP TRIGGER IF EXISTS memories_au;")
                conn.execute("DROP TABLE IF EXISTS memories_fts;")
                conn.executescript(_FTS_SCHEMA)
                conn.execute("""
                    INSERT INTO memories_fts(rowid, key, value, category, aliases)
                    SELECT id, key, value, category, aliases FROM memories;
                """)
            else:
                fts_sql = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='memories_fts'"
                ).fetchone()
                if fts_sql and "aliases" not in str(fts_sql[0]):
                    conn.execute("DROP TRIGGER IF EXISTS memories_ai;")
                    conn.execute("DROP TRIGGER IF EXISTS memories_ad;")
                    conn.execute("DROP TRIGGER IF EXISTS memories_au;")
                    conn.execute("DROP TABLE IF EXISTS memories_fts;")
                    conn.executescript(_FTS_SCHEMA)
                    conn.execute("""
                        INSERT INTO memories_fts(rowid, key, value, category, aliases)
                        SELECT id, key, value, category, aliases FROM memories;
                    """)

            conn.commit()
        finally:
            conn.close()
        _INITIALISED = True
        try:
            _migrate_legacy_json()
        except Exception as exc:
            print(f"[Mémoire] migration JSON ignorée : {exc}")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# ── migration depuis long_term.json ─────────────────────────────────────────

def _migrate_legacy_json() -> None:
    """Reprend une fois pour toutes le contenu de l'ancien fichier JSON.

    L'ancien fichier reste en place et continue d'être écrit par le reste de
    l'application : la migration doit donc pouvoir être rejouée sans créer de
    doublons — c'est le rôle des clés uniques.
    """
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    try:
        done = conn.execute(
            "SELECT v FROM meta WHERE k='json_migrated'").fetchone()
    finally:
        conn.close()
    if done or not LEGACY_JSON.exists():
        return

    import json
    try:
        data = json.loads(LEGACY_JSON.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(data, dict):
        return

    for category, entries in data.items():
        if not isinstance(entries, dict):
            continue
        for key, entry in entries.items():
            if isinstance(entry, dict):
                value = str(entry.get("value", "")).strip()
                updated = str(entry.get("updated", "")).strip()
            else:
                value, updated = str(entry).strip(), ""
            if not value:
                continue
            kind = (KIND_PROFILE if category in _PROFILE_CATEGORIES
                    else KIND_FACT)
            save(value, kind=kind, key=key, category=category,
                 happened=updated or None)

    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    try:
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('json_migrated', ?)",
                     (_now(),))
        conn.commit()
    finally:
        conn.close()


# ── écriture ────────────────────────────────────────────────────────────────

def save(value: str, *, kind: str = KIND_FACT, key: str | None = None,
         category: str | None = None, happened: str | None = None,
         aliases: str | None = None, enrich_async: bool = True) -> str:
    """Enregistre un souvenir, l'enrichit sémantiquement et rend une phrase prête à être dite."""
    value = " ".join((value or "").split())[:MAX_VALUE_CHARS]
    if not value:
        return "Rien à mémoriser."
    kind = kind if kind in KINDS else KIND_FACT
    # Normalisée, la clé est une identité : sans ça « Numero Pharmacie » et
    # « numero_pharmacie » cohabitent et le même fait est dit deux fois.
    key = "_".join((key or "").lower().split()) or None
    now = _now()

    from core.semantic_enricher import extract_local_aliases, schedule_memory_enrichment
    initial_aliases = aliases if aliases is not None else extract_local_aliases(
        value, category=category or "", key=key or ""
    )

    conn = _connect()
    record_id: Optional[int] = None
    try:
        if key:
            existing = conn.execute(
                "SELECT id, value FROM memories WHERE kind=? AND key=?",
                (kind, key)).fetchone()
            if existing:
                if existing["value"] == value and not aliases:
                    return "C'était déjà noté."
                record_id = existing["id"]
                conn.execute(
                    "UPDATE memories SET value=?, category=COALESCE(?, category),"
                    " aliases=COALESCE(?, aliases), updated=?, happened=COALESCE(?, happened) WHERE id=?",
                    (value, category, initial_aliases or None, now, happened, record_id))
                conn.commit()
                msg = "Noté, je remplace ce que je savais."
            else:
                cur = conn.execute(
                    "INSERT INTO memories (kind, key, value, category, aliases, created,"
                    " updated, happened) VALUES (?,?,?,?,?,?,?,?)",
                    (kind, key, value, category, initial_aliases or None, now, now,
                     happened or (_today() if kind == KIND_FACT else None)))
                conn.commit()
                record_id = cur.lastrowid
                msg = "C'est noté."
        else:
            duplicate = conn.execute(
                "SELECT id FROM memories WHERE kind=? AND value=?",
                (kind, value)).fetchone()
            if duplicate:
                record_id = duplicate["id"]
                conn.execute(
                    "UPDATE memories SET updated=?, aliases=COALESCE(aliases, ?) WHERE id=?",
                    (now, initial_aliases or None, record_id))
                conn.commit()
                msg = "C'était déjà noté."
            else:
                cur = conn.execute(
                    "INSERT INTO memories (kind, key, value, category, aliases, created,"
                    " updated, happened) VALUES (?,?,?,?,?,?,?,?)",
                    (kind, key, value, category, initial_aliases or None, now, now,
                     happened or (_today() if kind == KIND_FACT else None)))
                conn.commit()
                record_id = cur.lastrowid
                msg = "C'est noté."
    finally:
        conn.close()

    if record_id and enrich_async and not aliases:
        schedule_memory_enrichment(DB_PATH, record_id, value, category=category or "", key=key or "")

    if record_id:
        try:
            from core.knowledge_graph import upsert_source

            upsert_source(
                kind=f"memory:{kind}", external_id=str(record_id),
                title=key or category or kind, content=value,
                aliases=initial_aliases or "",
                happened=happened or (_today() if kind != KIND_PROFILE else ""),
                db_path=DB_PATH,
            )
        except Exception as exc:
            # La mémoire principale reste autoritaire : une relation manquante
            # ne doit jamais faire échouer l'enregistrement du souvenir.
            print(f"[Second Brain] liaison mémoire ignorée : {exc}")

    # Le magasin historique reste alimenté pour le panneau et les extensions.
    # Le moteur vectoriel reçoit la même écriture et devient la source des
    # rappels. Les tests qui redirigent DB_PATH restent strictement isolés.
    if DB_PATH.resolve() == _PRODUCTION_DB_PATH.resolve():
        try:
            from core import vector_memory
            vector_memory.save(
                value, kind=kind, key=key, category=category,
                happened=happened,
            )
        except Exception as exc:
            print(f"[Mémoire vectorielle] indexation ignorée : {exc}")

    return msg


def record_episode(summary: str, *, when: str | None = None) -> str:
    """Écrit le résumé d'une conversation. Le `when` est la date du jour."""
    return save(summary, kind=KIND_EPISODE, happened=when or _today())


def forget(key: str) -> str:
    conn = _connect()
    try:
        rows = conn.execute("SELECT id, kind FROM memories WHERE key=?", (key.strip(),)).fetchall()
        cursor = conn.execute("DELETE FROM memories WHERE key=?", (key.strip(),))
        conn.commit()
    finally:
        conn.close()
    if cursor.rowcount:
        try:
            from core.knowledge_graph import remove_source

            for row in rows:
                remove_source(f"memory:{row['kind']}", str(row["id"]), db_path=DB_PATH)
        except Exception:
            pass
    if DB_PATH.resolve() == _PRODUCTION_DB_PATH.resolve():
        try:
            from core import vector_memory
            vector_memory.forget(key)
        except Exception:
            pass
    return "Oublié." if cursor.rowcount else "Je n'avais rien sous ce nom."


def list_memories(limit: int = 200) -> list[dict]:
    """Vue structurée pour le panneau local, sans exposer la connexion SQLite."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, kind, key, value, category, updated, happened "
            "FROM memories ORDER BY updated DESC, id DESC LIMIT ?",
            (max(1, min(int(limit), 1000)),),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def forget_id(memory_id: int) -> bool:
    """Supprime exactement le souvenir choisi dans le panneau de mémoire."""
    conn = _connect()
    try:
        row = conn.execute("SELECT kind FROM memories WHERE id=?", (int(memory_id),)).fetchone()
        cursor = conn.execute("DELETE FROM memories WHERE id=?", (int(memory_id),))
        conn.commit()
    finally:
        conn.close()
    if cursor.rowcount and row:
        try:
            from core.knowledge_graph import remove_source
            remove_source(f"memory:{row['kind']}", str(int(memory_id)), db_path=DB_PATH)
        except Exception:
            pass
    return bool(cursor.rowcount)


# ── lecture ─────────────────────────────────────────────────────────────────

def _fts_query(text: str) -> str:
    """Transforme une phrase parlée en requête FTS5 tolérante.

    Les termes sont mis entre guillemets (une apostrophe ou un tiret suffirait
    sinon à provoquer une erreur de syntaxe FTS), reliés par OR, et suivis de
    `*` pour rattraper les pluriels et les conjugaisons — le tokenizer ne fait
    aucune racinisation.
    """
    words = re.findall(r"[0-9\w]{3,}", (text or "").lower(), flags=re.UNICODE)
    terms = [w for w in words if w not in _STOPWORDS]
    if not terms:
        return ""
    # Au-delà de huit termes la requête ne discrimine plus rien.
    return " OR ".join(f'"{w}"*' for w in terms[:8])


def search(query: str, *, limit: int = 5, kinds: Sequence[str] | None = None,
           exclude_ids: Iterable[int] = ()) -> list[dict]:
    """Souvenirs les plus proches de la phrase, du plus pertinent au moins."""
    if DB_PATH.resolve() == _PRODUCTION_DB_PATH.resolve():
        try:
            from core import vector_memory
            return vector_memory.search(
                query, limit=limit, kinds=kinds, exclude_ids=exclude_ids,
            )
        except Exception as exc:
            print(f"[Mémoire vectorielle] recherche en repli FTS : {exc}")
    match = _fts_query(query)
    if not match:
        return []

    # bm25(memories_fts, key, value, category, aliases)
    sql = [
        "SELECT m.id, m.kind, m.key, m.value, m.category, m.aliases, m.happened,",
        "       bm25(memories_fts, 2.0, 1.0, 0.5, 1.5) AS score",
        "FROM memories_fts JOIN memories m ON m.id = memories_fts.rowid",
        "WHERE memories_fts MATCH ?",
    ]
    params: list = [match]
    if kinds:
        sql.append("AND m.kind IN (%s)" % ",".join("?" * len(kinds)))
        params += list(kinds)
    excluded = [int(i) for i in exclude_ids]
    if excluded:
        sql.append("AND m.id NOT IN (%s)" % ",".join("?" * len(excluded)))
        params += excluded
    sql.append("ORDER BY score LIMIT ?")
    params.append(int(limit))

    conn = _connect()
    try:
        rows = conn.execute(" ".join(sql), params).fetchall()
        if rows:
            conn.execute(
                "UPDATE memories SET hits = hits + 1 WHERE id IN (%s)"
                % ",".join("?" * len(rows)), [r["id"] for r in rows])
            conn.commit()
    except sqlite3.OperationalError:
        # Requête FTS malformée malgré l'échappement : mieux vaut aucun
        # souvenir qu'une exception au milieu d'un tour de parole.
        return []
    finally:
        conn.close()
    return [dict(r) for r in rows]


def profile_entries(limit: int = 40) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, key, value, category FROM memories WHERE kind=?"
            " ORDER BY hits DESC, updated DESC LIMIT ?",
            (KIND_PROFILE, limit)).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def recent_episodes(limit: int = 3) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, value, happened FROM memories WHERE kind=?"
            " ORDER BY id DESC LIMIT ?", (KIND_EPISODE, limit)).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def recent_facts(limit: int = 5, days: int = 14) -> list[dict]:
    """Faits récents — ce qui est arrivé cette semaine reste d'actualité."""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, key, value, happened FROM memories WHERE kind=?"
            " AND COALESCE(happened, updated) >= ? ORDER BY id DESC LIMIT ?",
            (KIND_FACT, since, limit)).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def count() -> dict:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT kind, COUNT(*) n FROM memories GROUP BY kind").fetchall()
    finally:
        conn.close()
    return {r["kind"]: r["n"] for r in rows}


# ── mise en forme pour le prompt ────────────────────────────────────────────

def _line(row: dict) -> str:
    value = row.get("value", "")
    key = (row.get("key") or "").replace("_", " ")
    when = (row.get("happened") or "")[:10]
    prefix = f"{key} : " if key and key.lower() not in value.lower() else ""
    suffix = f" ({when})" if when and row.get("kind") != KIND_PROFILE else ""
    return f"· {prefix}{value}{suffix}"


def session_block(max_chars: int = 1600) -> str:
    """Ce que le modèle doit savoir avant la première phrase de la session."""
    if DB_PATH.resolve() == _PRODUCTION_DB_PATH.resolve():
        try:
            from core import vector_memory
            block = vector_memory.session_block(max_chars=max_chars)
            if block:
                return block
        except Exception as exc:
            print(f"[Mémoire vectorielle] contexte initial en repli FTS : {exc}")
    parts: list[str] = []

    profile = profile_entries()
    if profile:
        parts.append("[CE QUE TU SAIS DE LUI]")
        parts += [_line(r) for r in profile]

    facts = recent_facts()
    if facts:
        parts.append("\n[CES DERNIERS JOURS]")
        parts += [_line(dict(r, kind=KIND_FACT)) for r in facts]

    episodes = recent_episodes()
    if episodes:
        parts.append("\n[VOS DERNIÈRES CONVERSATIONS]")
        for row in reversed(episodes):
            when = (row.get("happened") or "")[:10]
            parts.append(f"· {when} — {row['value']}")

    if not parts:
        return ""
    parts.append(
        "\nCes souvenirs servent à comprendre, pas à réciter : ne les énumère "
        "jamais et ne dis pas que tu les as en mémoire. Utilise-les seulement "
        "quand ils rendent ta réponse plus juste."
    )
    block = "\n".join(parts)
    return block[:max_chars]


def recall_block(query: str, *, limit: int = 4,
                 exclude_ids: Iterable[int] = ()) -> tuple[str, list[int]]:
    """Souvenirs à injecter en cours de conversation, et leurs identifiants.

    Les identifiants remontent pour que l'appelant n'injecte jamais deux fois
    le même souvenir dans une session — le contexte Live est cumulatif.
    """
    if DB_PATH.resolve() == _PRODUCTION_DB_PATH.resolve():
        try:
            from core import vector_memory
            return vector_memory.recall_block(
                query, limit=limit, exclude_ids=exclude_ids,
            )
        except Exception as exc:
            print(f"[Mémoire vectorielle] rappel en repli FTS : {exc}")
    rows = search(query, limit=limit, exclude_ids=exclude_ids)
    if not rows:
        return "", []
    lines = ["[MÉMOIRE — contexte, ne le lis jamais à voix haute]"]
    lines += [_line(r) for r in rows]
    return "\n".join(lines), [r["id"] for r in rows]
