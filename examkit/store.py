"""Shared SQLite persistence.

One database file holds both independent subsystems (gradebook and seating).
All mutations run inside ``BEGIN IMMEDIATE`` transactions, so two open
connections are serialised by SQLite itself rather than by in-memory version
comparisons.  The schema is created on open; closing and reopening the same
path restores every fact, issued product, plan and confirmation.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);

-- gradebook ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS gb_rule_versions (
    rule_version TEXT PRIMARY KEY,
    rules_json    TEXT NOT NULL,
    created_seq   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS gb_students (
    rule_version TEXT NOT NULL,
    student      TEXT NOT NULL,
    PRIMARY KEY (rule_version, student)
);
CREATE TABLE IF NOT EXISTS gb_events (
    event_id   TEXT PRIMARY KEY,
    payload_hash TEXT NOT NULL,
    mirror_json TEXT NOT NULL,
    seq        INTEGER NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS gb_products (
    product_id TEXT PRIMARY KEY,
    prefix_seq INTEGER NOT NULL,
    rule_version TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    doc_json   TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- seating ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS se_room_versions (
    room_version TEXT PRIMARY KEY,
    doc_json     TEXT NOT NULL,
    created_op   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS se_ops (
    op_id    TEXT PRIMARY KEY,
    op_seq   INTEGER NOT NULL UNIQUE,
    payload_hash TEXT NOT NULL,
    doc_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS se_exam_product (
    exam_id    TEXT PRIMARY KEY,
    product_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS se_plans (
    plan_id    TEXT PRIMARY KEY,
    exam_id    TEXT NOT NULL,
    doc_json   TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_op INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS se_confirms (
    request_id TEXT PRIMARY KEY,
    plan_id    TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    doc_json   TEXT NOT NULL
);
"""


def canonical(obj: Any) -> str:
    """Deterministic JSON text used for hashing and replay comparisons."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def content_hash(obj: Any) -> str:
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()[:16]


def connect(path: str) -> sqlite3.Connection:
    if path != ":memory:":
        conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    else:
        conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    return conn


class Tx:
    """Context manager for a write transaction (BEGIN IMMEDIATE)."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")
