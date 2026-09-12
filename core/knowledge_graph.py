"""Second Brain d'ANO-GPT : graphe associatif SQLite/FTS5 léger.

Les sources existantes restent propriétaires de leurs données. Le graphe ne
stocke que des extraits bornés et les relations nécessaires pour traverser
personnes, projets, fichiers, commandes, dates, e-mails et conversations.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

MAX_CONTENT_CHARS = 12_000
MAX_RESULTS = 20
_LOCK = threading.RLock()


@dataclass(frozen=True)
class GraphResult:
    id: int
    kind: str
    title: str
    content: str
    path: str
    happened: str
    score: float
    relation: str = ""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS kg_nodes (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title       TEXT NOT NULL,
    content     TEXT NOT NULL DEFAULT '',
    aliases     TEXT NOT NULL DEFAULT '',
    path        TEXT NOT NULL DEFAULT '',
    happened    TEXT NOT NULL DEFAULT '',
    metadata    TEXT NOT NULL DEFAULT '{}',
    updated     TEXT NOT NULL,
    UNIQUE(kind, external_id)
);
CREATE INDEX IF NOT EXISTS kg_nodes_kind ON kg_nodes(kind, happened DESC);

CREATE TABLE IF NOT EXISTS kg_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kg_edges (
    source_id INTEGER NOT NULL REFERENCES kg_nodes(id) ON DELETE CASCADE,
    target_id INTEGER NOT NULL REFERENCES kg_nodes(id) ON DELETE CASCADE,
    relation  TEXT NOT NULL,
    weight    REAL NOT NULL DEFAULT 1.0,
    origin_id INTEGER NOT NULL REFERENCES kg_nodes(id) ON DELETE CASCADE,
    UNIQUE(source_id, target_id, relation, origin_id)
);
CREATE INDEX IF NOT EXISTS kg_edges_source ON kg_edges(source_id);
CREATE INDEX IF NOT EXISTS kg_edges_target ON kg_edges(target_id);
CREATE INDEX IF NOT EXISTS kg_edges_origin ON kg_edges(origin_id);

CREATE VIRTUAL TABLE IF NOT EXISTS kg_nodes_fts USING fts5(
    title, content, aliases,
    content='kg_nodes', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS kg_nodes_ai AFTER INSERT ON kg_nodes BEGIN
    INSERT INTO kg_nodes_fts(rowid, title, content, aliases)
    VALUES (new.id, new.title, new.content, new.aliases);
END;
CREATE TRIGGER IF NOT EXISTS kg_nodes_ad AFTER DELETE ON kg_nodes BEGIN
    INSERT INTO kg_nodes_fts(kg_nodes_fts, rowid, title, content, aliases)
    VALUES ('delete', old.id, old.title, old.content, old.aliases);
END;
CREATE TRIGGER IF NOT EXISTS kg_nodes_au AFTER UPDATE ON kg_nodes BEGIN
    INSERT INTO kg_nodes_fts(kg_nodes_fts, rowid, title, content, aliases)
    VALUES ('delete', old.id, old.title, old.content, old.aliases);
    INSERT INTO kg_nodes_fts(rowid, title, content, aliases)
    VALUES (new.id, new.title, new.content, new.aliases);
END;
"""


def _default_db_path() -> Path:
    from core import memory_store

    return Path(memory_store.DB_PATH)


def _connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else _default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    return conn


def _fold(value: str) -> str:
    return "".join(
        char for char in unicodedata.normalize("NFKD", str(value).casefold())
        if not unicodedata.combining(char)
    )


def _clean(value: Any, limit: int = MAX_CONTENT_CHARS) -> str:
    return " ".join(str(value or "").replace("\x00", " ").split())[:limit]


def _external_hash(prefix: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()[:20]
    return f"{prefix}:{digest}"


def _upsert_node(
    conn: sqlite3.Connection, *, kind: str, external_id: str, title: str,
    content: str = "", aliases: str = "", path: str = "", happened: str = "",
    metadata: dict | None = None,
) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO kg_nodes
           (kind, external_id, title, content, aliases, path, happened, metadata, updated)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(kind, external_id) DO UPDATE SET
             title=excluded.title, content=excluded.content, aliases=excluded.aliases,
             path=excluded.path, happened=excluded.happened,
             metadata=excluded.metadata, updated=excluded.updated""",
        (
            _clean(kind, 40), _clean(external_id, 300), _clean(title, 300),
            _clean(content), _clean(aliases, 1000), _clean(path, 1200),
            _clean(happened, 40), json.dumps(metadata or {}, ensure_ascii=False), now,
        ),
    )
    row = conn.execute(
        "SELECT id FROM kg_nodes WHERE kind=? AND external_id=?",
        (kind, external_id),
    ).fetchone()
    return int(row["id"])


def _edge(conn: sqlite3.Connection, source: int, target: int, relation: str,
          origin: int, weight: float = 1.0) -> None:
    if source == target:
        return
    conn.execute(
        "INSERT OR REPLACE INTO kg_edges VALUES (?, ?, ?, ?, ?)",
        (source, target, relation, max(0.1, min(5.0, float(weight))), origin),
    )


def _prune_orphans(conn: sqlite3.Connection) -> None:
    conn.execute(
        """DELETE FROM kg_nodes
           WHERE kind IN ('command', 'date', 'project')
             AND NOT EXISTS (
                 SELECT 1 FROM kg_edges
                 WHERE source_id=kg_nodes.id OR target_id=kg_nodes.id
             )"""
    )


_COMMAND_LINE_RE = re.compile(
    r"(?m)(?:^|[`$>])\s*((?:ffmpeg|ffprobe|git|docker|docker-compose|podman|systemctl|journalctl|"
    r"python3?|pip|uv|poetry|npm|pnpm|yarn|bun|cargo|rustc|go|"
    r"tar|zip|unzip|7z|rsync|curl|wget|http|find|rg|grep|sed|awk|jq|make|cmake|ninja|"
    r"gcc|clang|g\+\+|clang\+\+|ssh|scp|sftp|pacman|yay|paru|apt|dnf|flatpak|"
    r"yt-dlp|youtube-dl|convert|magick|pandoc|sqlite3|psql|kubectl|ansible|"
    r"fastapi|uvicorn|pytest|hyprctl)\b[ \t]+[^\n`]{1,500})"
)
_PROJECT_RE = re.compile(
    r"(?i)\b(?:projet|repo|dépôt|depot|application|app|service|module)\s+([\wÀ-ÖØ-öø-ÿ][\wÀ-ÖØ-öø-ÿ._-]{0,60})"
)


def _extract_commands(text: str) -> list[str]:
    found: list[str] = []
    for match in _COMMAND_LINE_RE.finditer(str(text or "")):
        command = _clean(match.group(1), 500).rstrip(".,;:")
        if command and command not in found:
            found.append(command)
    return found[:20]


def _entity_nodes(conn: sqlite3.Connection, source_id: int, *, title: str,
                  content: str, happened: str, project: str = "") -> list[int]:
    entity_ids: list[int] = []
    combined = f"{title}\n{content}"

    projects = [project] if project else []
    projects += [m.group(1) for m in _PROJECT_RE.finditer(combined)]
    for name in dict.fromkeys(_clean(item, 80) for item in projects if item):
        node_id = _upsert_node(
            conn, kind="project", external_id=_fold(name), title=name,
            aliases="projet travail mission dossier application repo depot",
        )
        _edge(conn, source_id, node_id, "belongs_to", source_id, 1.4)
        entity_ids.append(node_id)

    if happened:
        day = happened[:10]
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            node_id = _upsert_node(
                conn, kind="date", external_id=day, title=day,
                aliases="date jour calendrier historique",
            )
            _edge(conn, source_id, node_id, "occurred_on", source_id, 1.2)
            entity_ids.append(node_id)

    for command in _extract_commands(combined):
        aliases = "commande terminal shell script"
        if command.startswith(("ffmpeg", "ffprobe")):
            aliases += " compression vidéo video ffmpeg encodage transcodage format"
        elif command.startswith("docker"):
            aliases += " conteneur container docker compose stack"
        elif command.startswith("git"):
            aliases += " git commit versioning branche repo"
        elif command.startswith(("pacman", "yay", "paru", "apt", "dnf")):
            aliases += " paquet package mise a jour update systeme"
        elif command.startswith("systemctl"):
            aliases += " systemd service daemon systemctl journalctl"

        node_id = _upsert_node(
            conn, kind="command", external_id=_external_hash("cmd", command),
            title=command[:120], content=command,
            aliases=aliases,
            happened=happened,
        )
        _edge(conn, source_id, node_id, "uses_command", source_id, 2.0)
        entity_ids.append(node_id)

    folded = _fold(combined)
    people = conn.execute(
        "SELECT id, title, aliases FROM kg_nodes WHERE kind='person'"
    ).fetchall()
    for person in people:
        names = [person["title"], *(person["aliases"] or "").split()]
        if any(len(_fold(name)) >= 3 and re.search(
            rf"(?<!\w){re.escape(_fold(name))}(?!\w)", folded
        ) for name in names):
            person_id = int(person["id"])
            _edge(conn, source_id, person_id, "mentions", source_id, 1.6)
            entity_ids.append(person_id)

    # Les entités citées ensemble deviennent directement traversables : une
    # recherche du projet retrouve ainsi son contact sans relire tout le texte.
    unique = list(dict.fromkeys(entity_ids))[:20]
    for index, left in enumerate(unique):
        for right in unique[index + 1:]:
            _edge(conn, left, right, "associated", source_id, 0.9)
            _edge(conn, right, left, "associated", source_id, 0.9)
    return unique


def upsert_source(
    *, kind: str, external_id: str, title: str, content: str = "",
    aliases: str = "", path: str = "", happened: str = "",
    metadata: dict | None = None, project: str = "",
    db_path: str | Path | None = None,
) -> int:
    """Ajoute une source et reconstruit atomiquement ses relations sortantes."""
    with _LOCK, _connect(db_path) as conn:
        node_id = _upsert_node(
            conn, kind=kind, external_id=external_id, title=title,
            content=content, aliases=aliases, path=path, happened=happened,
            metadata=metadata,
        )
        conn.execute("DELETE FROM kg_edges WHERE origin_id=?", (node_id,))
        _entity_nodes(
            conn, node_id, title=title, content=content,
            happened=happened, project=project,
        )
        _prune_orphans(conn)
        conn.commit()
        return node_id


def record_conversation_turn(
    user_text: str, assistant_text: str, *, happened: str = "",
    db_path: str | Path | None = None,
) -> int:
    """Archive un tour complet avec un identifiant déterministe et borné."""
    timestamp = happened or datetime.now().isoformat(timespec="seconds")
    content = f"Utilisateur : {_clean(user_text)}\nANO-GPT : {_clean(assistant_text)}"
    return upsert_source(
        kind="conversation",
        external_id=_external_hash("turn", f"{timestamp}\n{content}"),
        title=_clean(user_text, 160) or "Conversation",
        content=content,
        aliases="discussion échange historique conversation",
        happened=timestamp,
        db_path=db_path,
    )


def upsert_contact(contact: dict, *, db_path: str | Path | None = None) -> int:
    name = _clean(contact.get("name"), 120)
    external_id = _clean(contact.get("id") or _fold(name), 200)
    aliases_list = [*list(contact.get("aliases") or []), *list(contact.get("emails") or [])]
    content = " ".join(
        [name, *aliases_list, str(contact.get("phone") or ""),
         str(contact.get("notes") or ""),
         " ".join(f"{k} {v}" for k, v in dict(contact.get("handles") or {}).items())]
    )
    with _LOCK, _connect(db_path) as conn:
        person_id = _upsert_node(
            conn, kind="person", external_id=external_id, title=name,
            content=content, aliases=" ".join(aliases_list),
            metadata={"contact_id": external_id},
        )
        # L'ajout tardif d'un contact relie les sources déjà indexées qui le
        # nomment. FTS évite un balayage Python de tous les documents.
        terms = _fts_query(name)
        if terms:
            try:
                rows = conn.execute(
                    "SELECT rowid FROM kg_nodes_fts WHERE kg_nodes_fts MATCH ? LIMIT 100",
                    (terms,),
                ).fetchall()
                for row in rows:
                    source_id = int(row[0])
                    if source_id != person_id:
                        _edge(conn, source_id, person_id, "mentions", source_id, 1.6)
            except sqlite3.OperationalError:
                pass
        conn.commit()
        return person_id


def remove_source(kind: str, external_id: str, *, db_path: str | Path | None = None) -> bool:
    with _LOCK, _connect(db_path) as conn:
        cursor = conn.execute(
            "DELETE FROM kg_nodes WHERE kind=? AND external_id=?", (kind, external_id)
        )
        _prune_orphans(conn)
        conn.commit()
        return bool(cursor.rowcount)


_SEMANTIC_IDEA_EXPANSIONS = (
    (("compress", "compression", "compresser", "reduc", "taille", "encoder", "encodage", "transcod", "codec", "x264", "x265", "hevc", "video", "mp4", "mkv", "mov"),
     ("compression", "video", "ffmpeg", "encodage", "transcodage", "x264", "x265", "crf", "hevc", "mp4", "mkv")),
    (("telecharg", "télécharg", "download", "stream", "extraire", "youtube", "ytdl"),
     ("telechargement", "yt-dlp", "youtube", "download", "stream")),
    (("sauvegard", "backup", "archiv", "tar", "zip", "rsync", "restaur"),
     ("sauvegarde", "backup", "archive", "tar", "zip", "rsync")),
    (("docker", "conteneur", "container", "compose", "stack", "podman"),
     ("docker", "compose", "conteneur", "container", "stack", "image")),
    (("systemd", "service", "systemctl", "journalctl", "daemon", "plant", "crash"),
     ("systemd", "service", "systemctl", "journalctl", "daemon", "logs")),
    (("git", "commit", "branche", "rebase", "merge", "stash", "repo", "depot", "dépôt"),
     ("git", "commit", "rebase", "branche", "stash", "depot")),
    (("paquet", "package", "mise a jour", "update", "upgrade", "pacman", "yay", "apt", "dnf", "orphelin"),
     ("paquet", "package", "update", "pacman", "yay", "apt", "orphelin")),
    (("reseau", "réseau", "port", "ip", "ecoute", "listening", "firewall", "pare-feu", "ufw", "ssh", "secret", "securite", "sécurité"),
     ("port", "reseau", "listening", "firewall", "ufw", "ssh", "securite")),
    (("base", "bdd", "database", "sql", "sqlite", "postgres", "mysql", "redis"),
     ("sqlite", "database", "sql", "postgres", "bdd", "table")),
    (("api", "backend", "fastapi", "flask", "serveur", "endpoint", "route"),
     ("api", "fastapi", "backend", "serveur", "endpoint")),
)


def _fts_query(query: str) -> str:
    ignored = {
        "retrouve", "retrouver", "rappelle", "rappeler", "avait", "utilise",
        "utilisee", "utilisée", "etait", "était", "pour", "dans", "avec",
        "mois", "dernier", "derniere", "dernière", "cette", "notre", "nous",
        "qui", "quoi", "quel", "quelle", "contact", "fichier",
        "commande", "commandes", "terminal", "script",
        "les", "des", "une", "sur", "mon", "mes", "dont", "on", "parle", "parlé",
    }
    folded_query = _fold(query)
    words = re.findall(r"[0-9\wÀ-ÖØ-öø-ÿ]{2,}", folded_query)

    for triggers, expansions in _SEMANTIC_IDEA_EXPANSIONS:
        if any(trigger in folded_query for trigger in triggers):
            words.extend(expansions)

    short_project = re.search(r"(?i)\b(?:projet|repo|app)\s+([a-z0-9])\b", _fold(query))
    if short_project:
        words.append(short_project.group(1))

    terms = list(dict.fromkeys(word for word in words if word not in ignored))[:24]
    return " OR ".join(f'"{term}"*' for term in terms)


def _date_bounds(query: str, now: datetime | None = None) -> tuple[str, str]:
    current = now or datetime.now()
    folded = _fold(query)
    if "mois dernier" in folded or "mois precedente" in folded or "mois precedent" in folded:
        end = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        start = (end - timedelta(days=1)).replace(day=1)
        return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
    if "semaine derniere" in folded or "semaine passee" in folded:
        this_week = (current - timedelta(days=current.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return (
            (this_week - timedelta(days=7)).strftime("%Y-%m-%d"),
            this_week.strftime("%Y-%m-%d"),
        )
    if "il y a 2 semaines" in folded or "il y a deux semaines" in folded:
        target = current - timedelta(days=14)
        return (target - timedelta(days=4)).strftime("%Y-%m-%d"), (target + timedelta(days=4)).strftime("%Y-%m-%d")
    if "hier" in folded:
        yest = current - timedelta(days=1)
        return yest.strftime("%Y-%m-%d"), current.strftime("%Y-%m-%d")
    explicit = re.search(r"\b(20\d{2}-\d{2}(?:-\d{2})?)\b", folded)
    if explicit:
        value = explicit.group(1)
        if len(value) == 7:
            start = datetime.strptime(value, "%Y-%m")
            end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        else:
            start = datetime.strptime(value, "%Y-%m-%d")
            end = start + timedelta(days=1)
        return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
    return "", ""


def _intent_adjustment(query: str, kind: str) -> float:
    """Fait remonter l'entité demandée sans casser le classement FTS5."""
    folded = _fold(query)
    intents = (
        (("commande", "terminal", "script", "compresser", "compression", "lancer", "executer"), "command", -10.0),
        (("contact", "personne", "qui etait", "qui était", "qui gere", "qui gère", "qui s'occupe", "qui"), "person", -10.0),
        (("projet", "repo", "dépôt", "application", "app"), "project", -1.4),
        (("fichier", "document", "readme"), "file", -1.8),
        (("note", "memo", "souvenir", "fait"), "note", -1.8),
        (("email", "e-mail", "mail", "courriel"), "email", -1.8),
        (("conversation", "discussion"), "conversation", -1.5),
    )
    return sum(
        boost for words, target, boost in intents
        if kind == target and any(_fold(word) in folded for word in words)
    )


def _primary_kind(query: str) -> str:
    folded = _fold(query)
    # L'objet grammatical demandé prime sur les mots de contexte
    rules = (
        (("contact", "personne", "qui etait", "qui était", "qui gere", "qui gère", "qui s'occupe", "qui"), "person"),
        (("fichier", "document", "readme"), "file"),
        (("note", "memo"), "note"),
        (("email", "e-mail", "mail", "courriel"), "email"),
        (("commande", "terminal", "script", "compresser", "compression"), "command"),
        (("conversation", "discussion"), "conversation"),
        (("projet", "repo", "dépôt", "application"), "project"),
    )
    for words, kind in rules:
        if any(word in folded for word in words):
            return kind
    return ""


def search(query: str, *, limit: int = 8, db_path: str | Path | None = None,
           now: datetime | None = None) -> list[GraphResult]:
    match = _fts_query(query)
    if not match:
        return []
    start, end = _date_bounds(query, now)
    primary_kind = _primary_kind(query)
    with _LOCK, _connect(db_path) as conn:
        try:
            rows = conn.execute(
                """SELECT n.*, bm25(kg_nodes_fts, 2.0, 1.0, 1.4) score
                   FROM kg_nodes_fts JOIN kg_nodes n ON n.id=kg_nodes_fts.rowid
                   WHERE kg_nodes_fts MATCH ?
                   ORDER BY score LIMIT ?""",
                (match, max(20, int(limit) * 4)),
            ).fetchall()
            if primary_kind:
                focused = conn.execute(
                    """SELECT n.*, bm25(kg_nodes_fts, 2.0, 1.0, 1.4) score
                       FROM kg_nodes_fts JOIN kg_nodes n ON n.id=kg_nodes_fts.rowid
                       WHERE kg_nodes_fts MATCH ? AND n.kind=?
                       ORDER BY score LIMIT ?""",
                    (match, primary_kind, max(8, int(limit))),
                ).fetchall()
                seen = {int(row["id"]) for row in rows}
                rows.extend(row for row in focused if int(row["id"]) not in seen)
        except sqlite3.OperationalError:
            return []
        direct: dict[int, tuple[sqlite3.Row, float, str]] = {}
        for row in rows:
            happened = str(row["happened"] or "")
            if start and happened and not (start <= happened[:10] < end):
                continue
            # Les lignes sans date restent candidates : un contact ou projet
            # peut conduire vers une preuve datée au niveau suivant.
            direct[int(row["id"])] = (
                row, float(row["score"]) + _intent_adjustment(query, str(row["kind"])), ""
            )

        candidates = dict(direct)
        frontier = set(direct)
        visited = set(frontier)
        for depth in (1, 2):
            if not frontier:
                break
            marks = ",".join("?" * len(frontier))
            edges = conn.execute(
                f"""SELECT e.*, n.* FROM kg_edges e JOIN kg_nodes n
                     ON n.id = CASE WHEN e.source_id IN ({marks})
                                    THEN e.target_id ELSE e.source_id END
                     WHERE e.source_id IN ({marks}) OR e.target_id IN ({marks})""",
                [*frontier, *frontier, *frontier],
            ).fetchall()
            next_frontier: set[int] = set()
            for row in edges:
                node_id = int(row["id"])
                if node_id in visited:
                    continue
                happened = str(row["happened"] or "")
                if start and happened and not (start <= happened[:10] < end):
                    continue
                neighbor_kind = str(row["kind"])
                # Une source volumineuse peut contenir plusieurs commandes sans
                # rapport avec les mots qui l'ont fait remonter. Une commande
                # n'est prioritaire que si son propre texte/alias correspond à
                # la requête ; contacts et projets restent, eux, associatifs.
                neighbor_adjustment = (
                    0.0 if neighbor_kind == "command"
                    else _intent_adjustment(query, neighbor_kind)
                )
                score = (
                    -0.35 / depth - float(row["weight"]) * 0.08
                    + neighbor_adjustment
                )
                candidates[node_id] = (row, score, str(row["relation"]))
                visited.add(node_id)
                next_frontier.add(node_id)
            frontier = next_frontier

        ordered = sorted(
            candidates.values(),
            key=lambda item: (
                0 if (
                    primary_kind
                    and int(item[0]["id"]) in direct
                    and str(item[0]["kind"]) == primary_kind
                ) else 1,
                item[1],
            ),
        )[:max(1, min(MAX_RESULTS, int(limit)))]
        return [
            GraphResult(
                id=int(row["id"]), kind=str(row["kind"]), title=str(row["title"]),
                content=str(row["content"]), path=str(row["path"]),
                happened=str(row["happened"]), score=float(score), relation=relation,
            )
            for row, score, relation in ordered
        ]


def status(*, db_path: str | Path | None = None) -> dict[str, int]:
    with _LOCK, _connect(db_path) as conn:
        rows = conn.execute("SELECT kind, COUNT(*) n FROM kg_nodes GROUP BY kind").fetchall()
        result = {str(row["kind"]): int(row["n"]) for row in rows}
        result["relations"] = int(conn.execute("SELECT COUNT(*) FROM kg_edges").fetchone()[0])
        return result


def bootstrap(*, db_path: str | Path | None = None, force: bool = False,
              file_index_path: str | Path | None = None,
              contacts_path: str | Path | None = None) -> dict[str, int]:
    """Synchronise les sources historiques. Destiné à un thread de démarrage."""
    target = Path(db_path) if db_path else _default_db_path()
    stats = {"memories": 0, "files": 0, "contacts": 0}
    with _connect(target) as conn:
        current = conn.execute(
            "SELECT value FROM kg_meta WHERE key='bootstrap_version'"
        ).fetchone()
        if current and current["value"] == "1" and not force:
            return stats
        try:
            memories = conn.execute(
                "SELECT id, kind, key, value, category, aliases, happened, updated FROM memories"
            ).fetchall()
        except sqlite3.OperationalError:
            memories = []
    for row in memories:
        upsert_source(
            kind=f"memory:{row['kind']}", external_id=str(row["id"]),
            title=str(row["key"] or row["category"] or row["kind"]),
            content=str(row["value"]), aliases=str(row["aliases"] or ""),
            happened=str(row["happened"] or row["updated"] or ""), db_path=target,
        )
        stats["memories"] += 1

    if file_index_path is None:
        file_index_path = Path(__file__).resolve().parent.parent / "memory" / "personal_index.db"
    index_path = Path(file_index_path)
    if index_path.is_file():
        source = sqlite3.connect(index_path, timeout=10.0)
        source.row_factory = sqlite3.Row
        try:
            files = source.execute(
                """SELECT f.path, f.filename, f.mtime, f.summary,
                          COALESCE(x.content, '') content
                   FROM indexed_files f LEFT JOIN files_fts x ON x.path=f.path
                   LIMIT 5000"""
            ).fetchall()
        except sqlite3.OperationalError:
            files = []
        finally:
            source.close()
        for row in files:
            happened = datetime.fromtimestamp(float(row["mtime"])).strftime("%Y-%m-%d")
            path = Path(str(row["path"]))
            upsert_source(
                kind="file", external_id=str(path), title=str(row["filename"]),
                content=f"{row['summary'] or ''}\n{str(row['content'] or '')[:MAX_CONTENT_CHARS]}",
                path=str(path), happened=happened, project=path.parent.name,
                db_path=target,
            )
            stats["files"] += 1

    if contacts_path is None:
        contacts_path = Path(__file__).resolve().parent.parent / "memory" / "contacts.json"
    try:
        raw = json.loads(Path(contacts_path).read_text(encoding="utf-8"))
        contacts = raw.get("contacts", []) if isinstance(raw, dict) else raw
    except Exception:
        contacts = []
    for contact in contacts if isinstance(contacts, list) else []:
        if isinstance(contact, dict):
            upsert_contact(contact, db_path=target)
            stats["contacts"] += 1
    with _connect(target) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO kg_meta(key, value) VALUES ('bootstrap_version', '1')"
        )
        conn.commit()
    return stats


def format_results(results: Iterable[GraphResult]) -> str:
    rows = list(results)
    if not rows:
        return "Je n'ai trouvé aucune association dans le Second Brain."
    lines = ["Second Brain — résultats associés :"]
    labels = {
        "file": "Fichier", "person": "Contact", "project": "Projet",
        "command": "Commande", "email": "E-mail", "conversation": "Conversation",
        "date": "Date", "note": "Note",
    }
    for index, row in enumerate(rows, 1):
        label = labels.get(row.kind, "Souvenir" if row.kind.startswith("memory:") else row.kind)
        detail = row.content[:360] or row.title
        where = f" — {row.path}" if row.path else ""
        when = f" ({row.happened[:10]})" if row.happened else ""
        relation = f" · via {row.relation}" if row.relation else ""
        lines.append(f"{index}. [{label}] {row.title}{when}{where}{relation}\n   {detail}")
    return "\n".join(lines)


def ingest_project(
    project_path: str | Path,
    *,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Scanne et indexe un projet de code local dans le graphe associatif."""
    p = Path(project_path).resolve()
    if not p.exists() or not p.is_dir():
        return {"error": f"Dossier introuvable : {project_path}"}

    project_name = p.name
    readme_content = ""
    for r_name in ("README.md", "readme.md", "README.txt", "README"):
        readme_file = p / r_name
        if readme_file.is_file():
            try:
                readme_content = readme_file.read_text(encoding="utf-8", errors="ignore")[:4000]
            except Exception:
                pass
            break

    # Détection des technos et dépendances
    tech_tags = []
    if (p / "Cargo.toml").exists():
        tech_tags.append("Rust")
    if (p / "package.json").exists():
        tech_tags.append("JavaScript/TypeScript")
    if (p / "pyproject.toml").exists() or (p / "requirements.txt").exists():
        tech_tags.append("Python")
    if (p / "Dockerfile").exists() or (p / "docker-compose.yml").exists() or (p / "compose.yaml").exists():
        tech_tags.append("Docker")
    if (p / "CMakeLists.txt").exists():
        tech_tags.append("C/C++")

    desc = f"Projet {project_name} ({', '.join(tech_tags) if tech_tags else 'Code'}).\n{readme_content[:1000]}"
    aliases = f"projet {project_name} {' '.join(tech_tags).lower()} codebase repo dossier"

    node_id = upsert_source(
        kind="project",
        external_id=_fold(project_name),
        title=project_name,
        content=desc,
        aliases=aliases,
        path=str(p),
        happened=datetime.now().strftime("%Y-%m-%d"),
        project=project_name,
        db_path=db_path,
    )

    return {
        "id": node_id,
        "project": project_name,
        "path": str(p),
        "technologies": tech_tags,
        "readme_indexed": bool(readme_content),
    }


def ingest_command(
    command: str,
    description: str = "",
    *,
    project: str = "",
    happened: str = "",
    tags: str = "",
    db_path: str | Path | None = None,
) -> int:
    """Enregistre explicitement une commande shell avec son contexte d'utilisation."""
    clean_cmd = _clean(command, 500)
    timestamp = happened or datetime.now().isoformat(timespec="seconds")
    aliases = f"commande terminal shell script {tags} {project}".strip()
    if clean_cmd.startswith(("ffmpeg", "ffprobe")):
        aliases += " compression vidéo video ffmpeg encodage transcodage format"
    elif clean_cmd.startswith("docker"):
        aliases += " conteneur container docker compose stack"
    elif clean_cmd.startswith("git"):
        aliases += " git commit versioning branche repo"

    content = f"Commande : {clean_cmd}\nDescription : {description}\nProjet : {project}"
    return upsert_source(
        kind="command",
        external_id=_external_hash("cmd", clean_cmd),
        title=description or clean_cmd[:120],
        content=content,
        aliases=aliases,
        happened=timestamp,
        project=project,
        db_path=db_path,
    )


def ingest_note(
    title: str,
    content: str,
    *,
    project: str = "",
    happened: str = "",
    tags: str = "",
    db_path: str | Path | None = None,
) -> int:
    """Enregistre une note personnelle ou mémo dans le Second Brain."""
    timestamp = happened or datetime.now().isoformat(timespec="seconds")
    aliases = f"note memo rappel fait {tags} {project}".strip()
    return upsert_source(
        kind="note",
        external_id=_external_hash("note", f"{title}\n{content}\n{timestamp}"),
        title=title,
        content=content,
        aliases=aliases,
        happened=timestamp,
        project=project,
        db_path=db_path,
    )


def connect_nodes(
    source_kind: str,
    source_id: str,
    target_kind: str,
    target_id: str,
    relation: str = "associated",
    weight: float = 1.0,
    *,
    db_path: str | Path | None = None,
) -> bool:
    """Relie explicitement deux entités existantes dans le graphe."""
    with _LOCK, _connect(db_path) as conn:
        src = conn.execute(
            "SELECT id FROM kg_nodes WHERE kind=? AND (external_id=? OR title=?)",
            (source_kind, source_id, source_id),
        ).fetchone()
        tgt = conn.execute(
            "SELECT id FROM kg_nodes WHERE kind=? AND (external_id=? OR title=?)",
            (target_kind, target_id, target_id),
        ).fetchone()
        if not src or not tgt:
            return False
        _edge(conn, int(src["id"]), int(tgt["id"]), relation, int(src["id"]), weight)
        _edge(conn, int(tgt["id"]), int(src["id"]), relation, int(src["id"]), weight)
        conn.commit()
        return True


def generate_mermaid_graph(
    query: str = "",
    *,
    limit: int = 10,
    db_path: str | Path | None = None,
) -> str:
    """Génère un graphe Mermaid des associations autour d'une recherche ou global."""
    with _LOCK, _connect(db_path) as conn:
        if query:
            results = search(query, limit=limit, db_path=db_path)
            node_ids = {r.id for r in results}
        else:
            rows = conn.execute("SELECT id FROM kg_nodes ORDER BY updated DESC LIMIT ?", (limit,)).fetchall()
            node_ids = {int(r["id"]) for r in rows}

        if not node_ids:
            return "graph TD;\n  Empty([Second Brain vide]);"

        marks = ",".join("?" * len(node_ids))
        nodes = conn.execute(
            f"SELECT id, kind, title FROM kg_nodes WHERE id IN ({marks})",
            list(node_ids),
        ).fetchall()

        edges = conn.execute(
            f"SELECT source_id, target_id, relation FROM kg_edges WHERE source_id IN ({marks}) AND target_id IN ({marks})",
            [*list(node_ids), *list(node_ids)],
        ).fetchall()

        lines = ["graph TD;"]
        icons = {
            "project": "📁", "person": "👤", "file": "📄",
            "command": "⚡", "conversation": "💬", "note": "📝",
            "date": "📅", "email": "✉️",
        }
        for n in nodes:
            nid = n["id"]
            kind = n["kind"]
            icon = icons.get(kind, "🔹")
            title_clean = n["title"].replace('"', "'")[:30]
            lines.append(f'  N{nid}["{icon} {title_clean}"];')

        for e in edges:
            lines.append(f'  N{e["source_id"]} -->|{e["relation"]}| N{e["target_id"]};')

        return "\n".join(lines)

