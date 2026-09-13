"""Garanties de latence pour l'archivage vectoriel des tours."""

from __future__ import annotations

import sqlite3

from core import vector_memory
from core import knowledge_graph


class _NoopPool:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def submit(self, pool_name, fn, *args, **kwargs):
        self.calls.append({"pool_name": pool_name, "kwargs": kwargs})
        fn(*args)


class _RecordingModel:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def encode(self, text):
        self.events.append("encode")
        return text

    def serialize(self, vector):
        return b"vector"


def _memory(tmp_path, monkeypatch):
    # sqlite-vec est hors sujet ici : ces tests vérifient le cycle SQLite et
    # restent exécutables sur les installations sans extension native.
    monkeypatch.setattr(vector_memory, "sqlite_vec", None)
    return vector_memory.VectorMemory(tmp_path / "vector.db")


def test_connection_uses_wal_normal_and_busy_timeout(tmp_path, monkeypatch):
    memory = _memory(tmp_path, monkeypatch)
    with memory._get_connection() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_save_turns_uses_one_commit_for_a_batch(tmp_path, monkeypatch):
    memory = _memory(tmp_path, monkeypatch)
    commits: list[str] = []
    original = memory._get_connection

    def traced_connection():
        conn = original()
        conn.set_trace_callback(lambda sql: commits.append(sql) if sql == "COMMIT" else None)
        return conn

    monkeypatch.setattr(memory, "_get_connection", traced_connection)
    memory.save_turns([
        ("Je travaille avec Python", "Bien noté.", None),
        ("Le projet ANO-GPT avance", "Super.", None),
    ])

    assert commits == ["COMMIT"]
    assert memory.stats()["entries_total"] == 2


def test_embeddings_are_prepared_before_opening_the_write_connection(tmp_path, monkeypatch):
    memory = _memory(tmp_path, monkeypatch)
    events: list[str] = []
    memory.model = _RecordingModel(events)
    # Le schéma de test suffit à exercer le chemin sqlite-vec sans extension C.
    with sqlite3.connect(memory.db_path) as conn:
        conn.execute("CREATE TABLE vec_entries (id INTEGER PRIMARY KEY, embedding BLOB)")
        conn.execute("CREATE TABLE vec_triples (id INTEGER PRIMARY KEY, embedding BLOB)")

    monkeypatch.setattr(vector_memory, "sqlite_vec", object())

    def connection_after_preparation():
        events.append("connection")
        conn = sqlite3.connect(memory.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(memory, "_get_connection", connection_after_preparation)
    memory.save("Je travaille avec Python.")

    assert events and events[0] == "encode"
    assert events.index("connection") > max(
        index for index, event in enumerate(events) if event == "encode"
    )


def test_five_queued_turns_submit_one_disk_batch_with_extended_watchdog(tmp_path, monkeypatch):
    memory = _memory(tmp_path, monkeypatch)
    pool = _NoopPool()
    monkeypatch.setattr("core.thread_pool.get_thread_pool", lambda: pool)
    monkeypatch.setattr("core.knowledge_graph.record_conversation_turns", lambda *args, **kwargs: [])

    for number in range(vector_memory.TURN_BATCH_MAX):
        memory.enqueue_turn(f"question {number}", f"réponse {number}")

    assert len(pool.calls) == 1
    assert pool.calls[0]["pool_name"] == "disk-io"
    assert pool.calls[0]["kwargs"] == {
        "task_name": "record-conversation-turn", "stall_timeout": 30.0,
    }
    assert memory.stats()["entries_total"] == vector_memory.TURN_BATCH_MAX


def test_secondary_conversation_graph_is_committed_once_per_batch(tmp_path, monkeypatch):
    commits: list[str] = []
    original = knowledge_graph._connect

    def traced_connection(db_path=None):
        conn = original(db_path)
        conn.set_trace_callback(lambda sql: commits.append(sql) if sql == "COMMIT" else None)
        return conn

    monkeypatch.setattr(knowledge_graph, "_connect", traced_connection)
    knowledge_graph.record_conversation_turns([
        ("première question", "première réponse", None),
        ("deuxième question", "deuxième réponse", None),
    ], db_path=tmp_path / "memory.db")

    assert commits == ["COMMIT"]
