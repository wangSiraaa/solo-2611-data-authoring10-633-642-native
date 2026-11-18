"""SQLite schema shared by the data service and the publisher.

Everything lives in one database file and one WAL so a release transaction can
read the authoritative record / consent state and commit its own release under
the same write lock.  ``commit_log`` is the single persistent total order over
accepted data, authorisation and release operations.
"""

from __future__ import annotations

import sqlite3

CORE_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Single global receive / commit order for every accepted operation.
CREATE TABLE IF NOT EXISTS commit_log (
    seq     INTEGER PRIMARY KEY AUTOINCREMENT,
    kind    TEXT NOT NULL,
    ref     TEXT,
    payload TEXT
);

-- One row per accepted data or authorisation operation.
--   service_seq : contiguous prefix over data/authorisation only (the ``k``)
--   commit_seq  : global order shared with drafts / reviews / releases
CREATE TABLE IF NOT EXISTS operations (
    service_seq INTEGER NOT NULL UNIQUE,
    commit_seq  INTEGER NOT NULL UNIQUE,
    kind        TEXT    NOT NULL CHECK (kind IN ('data','grant','revoke')),
    subject_id  TEXT    NOT NULL,
    -- data-only
    record_id   TEXT,
    revision    INTEGER,
    retracted   INTEGER,
    age         TEXT,
    district    TEXT,
    job         TEXT,
    sensitive   TEXT,
    -- auth-only
    purpose     TEXT,
    action      TEXT,
    -- replay identity
    op_id       TEXT,
    op_hash     TEXT    NOT NULL,
    PRIMARY KEY (kind, service_seq),
    CHECK (
        (kind = 'data' AND record_id IS NOT NULL AND revision IS NOT NULL
         AND retracted IS NOT NULL AND purpose IS NULL AND action IS NULL)
        OR
        (kind IN ('grant','revoke') AND record_id IS NULL AND revision IS NULL
         AND retracted IS NULL AND purpose IS NOT NULL AND action = kind)
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS data_record_revision
    ON operations(record_id, revision) WHERE kind = 'data';
CREATE UNIQUE INDEX IF NOT EXISTS operation_op_id
    ON operations(op_id) WHERE op_id IS NOT NULL;

-- Current materialised state (highest accepted revision per record).
CREATE TABLE IF NOT EXISTS live_records (
    record_id     TEXT PRIMARY KEY,
    subject_id    TEXT NOT NULL UNIQUE,
    revision      INTEGER NOT NULL,
    retracted     INTEGER NOT NULL,
    age           TEXT,
    district      TEXT,
    job           TEXT,
    sensitive     TEXT,
    commit_seq    INTEGER NOT NULL
);

-- Current materialised authorisation state.
CREATE TABLE IF NOT EXISTS live_auth (
    subject_id  TEXT NOT NULL,
    purpose     TEXT NOT NULL,
    granted     INTEGER NOT NULL,
    commit_seq  INTEGER NOT NULL,
    PRIMARY KEY (subject_id, purpose)
);
"""

PUBLISHER_SCHEMA = """
CREATE TABLE IF NOT EXISTS drafts (
    draft_id          TEXT PRIMARY KEY,
    request_id        TEXT NOT NULL UNIQUE,
    purpose           TEXT NOT NULL,
    prefix_k          INTEGER NOT NULL,
    req_k             INTEGER NOT NULL,
    req_d             INTEGER NOT NULL,
    weights           TEXT NOT NULL,
    params_hash       TEXT NOT NULL,
    created_head_seq  INTEGER NOT NULL,
    created_seq       INTEGER NOT NULL,
    state             TEXT NOT NULL
                      CHECK (state IN ('pending','approved','rejected','infeasible')),
    -- frozen exact slice (private)
    snapshot_digest   TEXT NOT NULL,
    row_count         INTEGER NOT NULL,
    private_ledger    TEXT NOT NULL,
    -- selection / result
    levels            TEXT,
    loss              INTEGER,
    optimal_vectors   TEXT,
    group_stats       TEXT,
    public_payload    TEXT,
    infeasible_report TEXT,
    result            TEXT NOT NULL,
    -- human review
    review_decision   TEXT,
    review_reason     TEXT,
    reviewed_seq      INTEGER
);

CREATE TABLE IF NOT EXISTS releases (
    release_id     TEXT PRIMARY KEY,
    draft_id       TEXT NOT NULL UNIQUE,
    release_seq    INTEGER NOT NULL UNIQUE,
    released_k     INTEGER NOT NULL,
    released_head_seq INTEGER NOT NULL,
    public_path    TEXT NOT NULL,
    private_path   TEXT NOT NULL,
    public_sha256  TEXT NOT NULL,
    private_sha256 TEXT NOT NULL,
    payload        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    at_seq     INTEGER NOT NULL,
    event      TEXT NOT NULL,
    detail     TEXT NOT NULL
);
"""


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(CORE_SCHEMA)
    conn.executescript(PUBLISHER_SCHEMA)
    conn.execute("INSERT OR IGNORE INTO meta(key,value) VALUES ('schema_version','3')")
