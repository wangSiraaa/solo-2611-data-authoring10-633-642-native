"""Authoritative data / consent service persisted in SQLite.

Guarantees implemented here
---------------------------
* One live *original* record per ``subject_id``.  ``put`` carries a revision;
  the highest revision wins.  A later commit with a *lower* revision is
  refused (``STALE_REVISION``) so it can never resurrect an older row.
* A retraction is a normal put whose row is ``None``.  Retracted rows are
  retained forever in the operation log but never appear in a view.
* Re-submitting byte-identical content for the same record/revision and the
  same retracted flag is an idempotent replay.  Different content for the same
  record/revision is refused (``IDENTICAL_RECORD_CONFLICT``).
* ``grant`` / ``revoke`` operate on ``(subject, purpose)`` and share one global
  receive order with data operations (``commit_log`` / ``operations``).
  Replays are idempotent; conflicting replays are refused.
* Accepted operations form a committed prefix with contiguous ``commit_seq``
  values; rejected operations roll back and consume none.
* ``view(k, purpose)`` is a snapshot at accepted-operation prefix ``k``: each
  subject's latest accepted data version with ``commit_seq <= k`` (if
  non-retracted), filtered by the subject's latest accepted authorisation
  action for ``purpose`` within ``[1, k]``.  A retraction never deletes the
  original; a later grant never resurrects a retracted row.

The ``live_*`` tables hold the current state for fast release-time checks; the
``operations`` history table is authoritative for every prefix view.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .domains import Domains
from .errors import (
    AuthorizationConflict,
    DomainViolation,
    IdenticalRecordConflict,
    InvalidArgument,
    RecordIdConflict,
    StaleRevision,
    SubjectConflict,
)
from .schema import init_schema
from .util import canonical_json, sha256_hex

_ROW_COLUMNS = ("age", "district", "job", "sensitive")


@dataclass(frozen=True)
class SliceRow:
    """One private view row.  Source identities are for the private ledger."""

    subject_id: str
    record_id: str
    revision: int
    version_op_id: str | None
    version_commit_seq: int        # service prefix position of the data op
    version_commit_order: int      # global commit order of the data op
    consent_commit_seq: int        # service prefix position of the grant
    age: str
    district: str
    job: str
    sensitive: str

    def public_fields(self) -> dict[str, str]:
        return {column: getattr(self, column) for column in _ROW_COLUMNS}

    def private_fields(self) -> dict[str, object]:
        return {
            "subject_id": self.subject_id,
            "record_id": self.record_id,
            "revision": self.revision,
            "version_op_id": self.version_op_id,
            "version_commit_seq": self.version_commit_seq,
            "version_commit_order": self.version_commit_order,
            "consent_commit_seq": self.consent_commit_seq,
        }


def _hash_payload(payload: Mapping[str, object]) -> str:
    return sha256_hex(canonical_json(payload))


def last_seq_at(conn: sqlite3.Connection) -> int:
    """Global commit head (data, authorisation, draft, review, release).

    Used for the concurrent-release gate; ``k`` slices use :func:`service_head`.
    """

    return int(
        conn.execute("SELECT COALESCE(MAX(seq),0) AS m FROM commit_log").fetchone()["m"]
    )


def service_head(conn: sqlite3.Connection) -> int:
    """Contiguous accepted data/authorisation prefix that ``view(k)`` reads."""

    return int(
        conn.execute(
            "SELECT COALESCE(MAX(service_seq),0) AS m FROM operations"
        ).fetchone()["m"]
    )


def view_at(
    conn: sqlite3.Connection, k: int, purpose: str
) -> list[SliceRow]:
    """Exact authorised private snapshot at accepted-operation prefix ``k``.

    Shared by :class:`DataService` and the publisher so a release can verify
    the frozen slice against authoritative history inside its own transaction.
    """

    if isinstance(k, bool) or not isinstance(k, int):
        raise InvalidArgument("k must be an integer")
    if k < 0:
        raise InvalidArgument("k must be >= 0")
    if not isinstance(purpose, str) or not purpose:
        raise InvalidArgument("purpose must be a non-empty string")

    version_rows = conn.execute(
        """
        SELECT v.subject_id, v.record_id, v.revision, v.op_id, v.service_seq,
               v.commit_seq, v.age, v.district, v.job, v.sensitive
        FROM operations v
        JOIN (
            SELECT subject_id, MAX(revision) AS revision
            FROM operations
            WHERE kind = 'data' AND service_seq <= ?
            GROUP BY subject_id
        ) latest
          ON latest.subject_id = v.subject_id
         AND latest.revision = v.revision
        WHERE v.kind = 'data' AND v.service_seq <= ? AND v.retracted = 0
        ORDER BY v.subject_id
        """,
        (k, k),
    ).fetchall()
    consent_rows = conn.execute(
        """
        SELECT a.subject_id, a.service_seq
        FROM operations a
        JOIN (
            SELECT subject_id, MAX(service_seq) AS service_seq
            FROM operations
            WHERE kind IN ('grant','revoke') AND purpose = ?
                  AND service_seq <= ?
            GROUP BY subject_id
        ) latest
          ON latest.subject_id = a.subject_id
         AND latest.service_seq = a.service_seq
        WHERE a.kind = 'grant' AND a.purpose = ?
        """,
        (purpose, k, purpose),
    ).fetchall()
    consent = {r["subject_id"]: int(r["service_seq"]) for r in consent_rows}

    out: list[SliceRow] = []
    for row in version_rows:
        seq = consent.get(row["subject_id"])
        if seq is None:
            continue
        out.append(
            SliceRow(
                subject_id=row["subject_id"],
                record_id=row["record_id"],
                revision=int(row["revision"]),
                version_op_id=row["op_id"],
                version_commit_seq=int(row["service_seq"]),
                version_commit_order=int(row["commit_seq"]),
                consent_commit_seq=seq,
                age=row["age"],
                district=row["district"],
                job=row["job"],
                sensitive=row["sensitive"],
            )
        )
    return out


class DataService:
    """Purpose-authorised original-linked data service."""

    def __init__(
        self,
        path: str | Path,
        domains: Domains,
        *,
        timeout: float = 30.0,
    ) -> None:
        self.path = str(path)
        self.domains = domains
        self._conn = sqlite3.connect(
            self.path, timeout=timeout, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        init_schema(self._conn)

    # ------------------------------------------------------------------ #
    # Transactions
    # ------------------------------------------------------------------ #
    def begin(self) -> None:
        if self._conn.in_transaction:
            raise InvalidArgument("a transaction is already open on this connection")
        self._conn.execute("BEGIN IMMEDIATE")

    def commit(self) -> None:
        self._conn.execute("COMMIT")

    def rollback(self) -> None:
        if self._conn.in_transaction:
            self._conn.execute("ROLLBACK")

    def close(self) -> None:
        if self._conn is None:
            return
        try:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
        except sqlite3.ProgrammingError:
            pass
        try:
            self._conn.close()
        finally:
            self._conn = None  # type: ignore[assignment]

    def __enter__(self) -> "DataService":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def connection(self) -> sqlite3.Connection:
        """Shared connection (used by the publisher for atomic transactions)."""

        return self._conn

    def _next_seq(self, kind: str, ref: str | None = None) -> tuple[int, int]:
        """Allocate one global journal slot and the next service prefix slot."""

        cur = self._conn.execute(
            "INSERT INTO commit_log(kind,ref) VALUES (?,?)", (kind, ref)
        )
        commit_order = int(cur.lastrowid)
        service_seq = service_head(self._conn) + 1
        return service_seq, commit_order

    # ------------------------------------------------------------------ #
    # Data operations
    # ------------------------------------------------------------------ #
    def put(
        self,
        record_id: str,
        subject_id: str,
        revision: int,
        row: Mapping[str, str] | None,
        retracted: bool = False,
        *,
        op_id: str | None = None,
        atomic: bool = False,
    ) -> dict[str, object]:
        """Apply one accepted data operation or return its idempotent replay.

        ``row is None`` encodes a retraction.  With ``retracted=True`` the row
        payload is ignored and the record is retracted.

        ``atomic=False`` starts/commits its own transaction.  ``atomic=True``
        requires the caller to have opened ``begin()`` so a competing writer
        and the publisher can share one commit order.
        """

        self._validate_ids(record_id, subject_id, revision)
        retracted = bool(retracted) or row is None
        settings = self._normalise_row(row) if not retracted else None
        op_id = self._check_op_id(op_id)
        payload = self._data_payload(
            record_id, subject_id, revision, retracted, settings
        )
        op_hash = _hash_payload(payload)

        own_txn = False
        if not atomic:
            self.begin()
            own_txn = True
        try:
            result = self._apply_put(
                record_id, subject_id, revision, retracted, settings, op_id, op_hash
            )
            if own_txn:
                self.commit()
            return result
        except Exception:
            if own_txn:
                self.rollback()
            raise

    def _apply_put(
        self,
        record_id: str,
        subject_id: str,
        revision: int,
        retracted: bool,
        settings: Mapping[str, str] | None,
        op_id: str | None,
        op_hash: str,
    ) -> dict[str, object]:
        conn = self._conn
        if op_id is not None:
            existing = conn.execute(
                "SELECT * FROM operations WHERE op_id = ?",
                (op_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["op_hash"] == op_hash
                    and existing["kind"] == "data"
                    and existing["record_id"] == record_id
                    and int(existing["revision"]) == revision
                ):
                    return self._put_replay(
                        record_id, subject_id, revision, retracted,
                        int(existing["commit_seq"]),
                    )
                raise IdenticalRecordConflict(
                    f"operation id {op_id!r} was already accepted with different "
                    "content"
                )

        same = conn.execute(
            "SELECT * FROM operations WHERE kind='data' "
            "AND record_id=? AND revision=?",
            (record_id, revision),
        ).fetchone()
        if same is not None:
            stored = self._data_payload(
                same["record_id"],
                same["subject_id"],
                int(same["revision"]),
                bool(same["retracted"]),
                self._row_from_operation(same),
            )
            if _hash_payload(stored) == op_hash:
                # Byte-identical replay: no new operation, no new sequence.
                if op_id is not None and same["op_id"] is None:
                    conn.execute(
                        "UPDATE operations SET op_id=? WHERE kind='data' "
                        "AND record_id=? AND revision=?",
                        (op_id, record_id, revision),
                    )
                return self._put_replay(
                    record_id, subject_id, revision, retracted,
                    int(same["commit_seq"]),
                )
            raise IdenticalRecordConflict(
                f"record {record_id!r} revision {revision} already exists with "
                "different content"
            )

        live = conn.execute(
            "SELECT * FROM live_records WHERE record_id=?", (record_id,)
        ).fetchone()
        if live is not None and live["subject_id"] != subject_id:
            raise RecordIdConflict(
                f"record_id {record_id!r} is already bound to a different subject"
            )
        bound = conn.execute(
            "SELECT record_id FROM live_records WHERE subject_id=?", (subject_id,)
        ).fetchone()
        if bound is not None and bound["record_id"] != record_id:
            raise SubjectConflict(
                f"subject {subject_id!r} is already bound to record "
                f"{bound['record_id']!r}"
            )
        if live is not None and revision <= int(live["revision"]):
            raise StaleRevision(
                f"revision {revision} for record {record_id!r} is not newer than "
                f"the live revision {live['revision']}"
            )

        service_seq, commit_order = self._next_seq("data", record_id)
        conn.execute(
            "INSERT INTO operations("
            "service_seq,commit_seq,kind,subject_id,record_id,revision,retracted,"
            "age,district,job,sensitive,op_id,op_hash) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                service_seq,
                commit_order,
                "data",
                subject_id,
                record_id,
                revision,
                1 if retracted else 0,
                None if settings is None else settings["age"],
                None if settings is None else settings["district"],
                None if settings is None else settings["job"],
                None if settings is None else settings["sensitive"],
                op_id,
                op_hash,
            ),
        )
        conn.execute(
            "INSERT INTO live_records("
            "record_id,subject_id,revision,retracted,age,district,job,sensitive,"
            "commit_seq) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(record_id) DO UPDATE SET "
            "subject_id=excluded.subject_id, revision=excluded.revision, "
            "retracted=excluded.retracted, age=excluded.age, "
            "district=excluded.district, job=excluded.job, "
            "sensitive=excluded.sensitive, commit_seq=excluded.commit_seq",
            (
                record_id,
                subject_id,
                revision,
                1 if retracted else 0,
                None if settings is None else settings["age"],
                None if settings is None else settings["district"],
                None if settings is None else settings["job"],
                None if settings is None else settings["sensitive"],
                commit_order,
            ),
        )
        return {
            "status": "accepted",
            "kind": "data",
            "record_id": record_id,
            "subject_id": subject_id,
            "revision": revision,
            "retracted": retracted,
            "service_seq": service_seq,
            "commit_seq": commit_order,
        }

    @staticmethod
    def _put_replay(
        record_id: str,
        subject_id: str,
        revision: int,
        retracted: bool,
        seq: int,
    ) -> dict[str, object]:
        return {
            "status": "replay",
            "kind": "data",
            "record_id": record_id,
            "subject_id": subject_id,
            "revision": revision,
            "retracted": retracted,
            "commit_seq": seq,
        }

    # ------------------------------------------------------------------ #
    # Authorisation operations
    # ------------------------------------------------------------------ #
    def grant(
        self,
        subject_id: str,
        purpose: str,
        *,
        op_id: str | None = None,
        atomic: bool = False,
    ) -> dict[str, object]:
        return self._authorize(subject_id, purpose, "grant", op_id, atomic)

    def revoke(
        self,
        subject_id: str,
        purpose: str,
        *,
        op_id: str | None = None,
        atomic: bool = False,
    ) -> dict[str, object]:
        return self._authorize(subject_id, purpose, "revoke", op_id, atomic)

    def _authorize(
        self,
        subject_id: str,
        purpose: str,
        action: str,
        op_id: str | None,
        atomic: bool,
    ) -> dict[str, object]:
        if not isinstance(subject_id, str) or not subject_id:
            raise InvalidArgument("subject_id must be a non-empty string")
        if not isinstance(purpose, str) or not purpose:
            raise InvalidArgument("purpose must be a non-empty string")
        op_id = self._check_op_id(op_id)

        own_txn = False
        if not atomic:
            self.begin()
            own_txn = True
        try:
            # A given stable operation id must always mean the same thing.
            if op_id is not None:
                clash = self._conn.execute(
                    "SELECT * FROM operations WHERE op_id=?", (op_id,)
                ).fetchone()
                if clash is not None:
                    if (
                        clash["kind"] == action
                        and clash["subject_id"] == subject_id
                        and clash["purpose"] == purpose
                    ):
                        result = {
                            "status": "replay",
                            "kind": "auth",
                            "action": action,
                            "subject_id": subject_id,
                            "purpose": purpose,
                            "service_seq": int(clash["service_seq"]),
                            "commit_seq": int(clash["commit_seq"]),
                        }
                        if own_txn:
                            self.commit()
                        return result
                    raise AuthorizationConflict(
                        f"operation id {op_id!r} was already accepted with "
                        "different content"
                    )

            live = self._conn.execute(
                "SELECT * FROM live_auth WHERE subject_id=? AND purpose=?",
                (subject_id, purpose),
            ).fetchone()
            wanted = 1 if action == "grant" else 0
            # Repeating the currently-effective state has no further effect.
            if live is not None and int(live["granted"]) == wanted:
                result = {
                    "status": "replay",
                    "kind": "auth",
                    "action": action,
                    "subject_id": subject_id,
                    "purpose": purpose,
                    "commit_seq": int(live["commit_seq"]),
                }
                if own_txn:
                    self.commit()
                return result

            service_seq, commit_order = self._next_seq(action, subject_id)
            self._conn.execute(
                "INSERT INTO operations(service_seq,commit_seq,kind,subject_id,"
                "purpose,action,op_id,op_hash) VALUES (?,?,?,?,?,?,?,?)",
                (
                    service_seq,
                    commit_order,
                    action,
                    subject_id,
                    purpose,
                    action,
                    op_id,
                    _hash_payload(_auth_payload(subject_id, purpose, action)),
                ),
            )
            self._conn.execute(
                "INSERT INTO live_auth(subject_id,purpose,granted,commit_seq) "
                "VALUES (?,?,?,?) ON CONFLICT(subject_id,purpose) DO UPDATE SET "
                "granted=excluded.granted, commit_seq=excluded.commit_seq",
                (subject_id, purpose, wanted, commit_order),
            )
            result = {
                "status": "accepted",
                "kind": "auth",
                "action": action,
                "subject_id": subject_id,
                "purpose": purpose,
                "service_seq": service_seq,
                "commit_seq": commit_order,
            }
            if own_txn:
                self.commit()
            return result
        except Exception:
            if own_txn:
                self.rollback()
            raise

    # ------------------------------------------------------------------ #
    # Snapshots / views
    # ------------------------------------------------------------------ #
    def last_seq(self) -> int:
        """Service prefix head: the largest ``k`` a view may currently use."""

        return service_head(self._conn)

    def global_seq(self) -> int:
        """Global journal head including drafts / reviews / releases."""

        return last_seq_at(self._conn)

    def view_head(self) -> int:
        return service_head(self._conn)

    def data_last_seq(self) -> int:
        return int(
            self._conn.execute(
                "SELECT COALESCE(MAX(service_seq),0) AS m FROM operations "
                "WHERE kind='data'"
            ).fetchone()["m"]
        )

    def auth_last_seq(self) -> int:
        return int(
            self._conn.execute(
                "SELECT COALESCE(MAX(service_seq),0) AS m FROM operations "
                "WHERE kind IN ('grant','revoke')"
            ).fetchone()["m"]
        )

    def view(self, k: int, purpose: str) -> list[SliceRow]:
        """Private, identity-linked snapshot at accepted-operation prefix ``k``."""

        return view_at(self._conn, k, purpose)

    def current_view(self, purpose: str) -> list[SliceRow]:
        """Live authorised slice (latest committed state), for pre-checks."""

        return self.view(self.last_seq(), purpose)

    def subject_view(self, subject_id: str, k: int, purpose: str) -> dict[str, object]:
        """Owner-facing explanation of one subject at prefix ``k``."""

        rows = [row for row in self.view(k, purpose) if row.subject_id == subject_id]
        versions = self._conn.execute(
            "SELECT revision,retracted,service_seq,commit_seq,op_id FROM operations "
            "WHERE kind='data' AND subject_id=? AND service_seq<=? ORDER BY revision",
            (subject_id, k),
        ).fetchall()
        auth = self._conn.execute(
            "SELECT kind AS action,service_seq,commit_seq FROM operations "
            "WHERE kind IN ('grant','revoke') AND subject_id=? AND purpose=? "
            "AND service_seq<=? ORDER BY service_seq",
            (subject_id, purpose, k),
        ).fetchall()
        return {
            "subject_id": subject_id,
            "k": k,
            "purpose": purpose,
            "authorised": bool(rows),
            "row": None if not rows else rows[0].public_fields(),
            "versions": [dict(v) for v in versions],
            "authorisation": [dict(a) for a in auth],
        }

    def live_record(self, subject_id: str) -> dict[str, object] | None:
        row = self._conn.execute(
            "SELECT * FROM live_records WHERE subject_id=?", (subject_id,)
        ).fetchone()
        return None if row is None else dict(row)

    def consent_history(self, subject_id: str, purpose: str) -> list[dict[str, object]]:
        """Every accepted grant/revoke for one subject+purpose, in order.

        Owner-facing: shows *when* (service prefix and global order) each
        decision was received.  Independent of ``k`` and of publisher state.
        """

        if not isinstance(subject_id, str) or not subject_id:
            raise InvalidArgument("subject_id must be a non-empty string")
        if not isinstance(purpose, str) or not purpose:
            raise InvalidArgument("purpose must be a non-empty string")
        rows = self._conn.execute(
            "SELECT service_seq,commit_seq,kind AS action,op_id FROM operations "
            "WHERE kind IN ('grant','revoke') AND subject_id=? AND purpose=? "
            "ORDER BY service_seq",
            (subject_id, purpose),
        ).fetchall()
        return [dict(row) for row in rows]

    def record_history(self, subject_id: str) -> list[dict[str, object]]:
        """Every accepted data revision for one subject, including retractions."""

        if not isinstance(subject_id, str) or not subject_id:
            raise InvalidArgument("subject_id must be a non-empty string")
        rows = self._conn.execute(
            "SELECT service_seq,commit_seq,record_id,revision,retracted,age,"
            "district,job,sensitive,op_id FROM operations "
            "WHERE kind='data' AND subject_id=? ORDER BY revision, service_seq",
            (subject_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def live_authorisation(
        self, subject_id: str, purpose: str
    ) -> dict[str, object] | None:
        row = self._conn.execute(
            "SELECT * FROM live_auth WHERE subject_id=? AND purpose=?",
            (subject_id, purpose),
        ).fetchone()
        return None if row is None else dict(row)

    def operation_log(self) -> list[dict[str, object]]:
        """Merged accepted history, for private audit / reproducibility."""

        rows = self._conn.execute(
            "SELECT o.service_seq,o.commit_seq,o.kind,o.subject_id,o.record_id,"
            "o.revision,o.retracted,o.purpose,o.action,o.op_id "
            "FROM operations o ORDER BY o.service_seq"
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _check_op_id(op_id: str | None) -> str | None:
        if op_id is None:
            return None
        if not isinstance(op_id, str) or not op_id:
            raise InvalidArgument("op_id must be a non-empty string when supplied")
        return op_id

    def _validate_ids(self, record_id: str, subject_id: str, revision: int) -> None:
        if not isinstance(record_id, str) or not record_id:
            raise InvalidArgument("record_id must be a non-empty string")
        if not isinstance(subject_id, str) or not subject_id:
            raise InvalidArgument("subject_id must be a non-empty string")
        if isinstance(revision, bool) or not isinstance(revision, int):
            raise InvalidArgument("revision must be an integer")
        if revision < 0:
            raise InvalidArgument("revision must be >= 0")

    def _normalise_row(self, row: Mapping[str, str]) -> dict[str, str]:
        if not isinstance(row, Mapping):
            raise DomainViolation("row must be a mapping of the four columns")
        settings = {}
        for column in _ROW_COLUMNS:
            value = row.get(column)
            if not isinstance(value, str):
                raise DomainViolation(f"row.{column} must be a string")
            settings[column] = value
        self.domains.check_row(settings)
        return settings

    @staticmethod
    def _row_from_operation(row: sqlite3.Row) -> dict[str, str] | None:
        if row["retracted"]:
            return None
        return {column: row[column] for column in _ROW_COLUMNS}

    @staticmethod
    def _data_payload(
        record_id: str,
        subject_id: str,
        revision: int,
        retracted: bool,
        settings: Mapping[str, str] | None,
    ) -> dict[str, object]:
        return {
            "kind": "data",
            "record_id": record_id,
            "subject_id": subject_id,
            "revision": revision,
            "retracted": bool(retracted),
            "row": None if settings is None else dict(settings),
        }

    def initialise(
        self,
        rows: Iterable[Mapping[str, str]],
        *,
        purpose: str,
        grants: Sequence[str],
        revoke: str | None = None,
        record_prefix: str = "rec",
    ) -> dict[str, object]:
        """Convenience bootstrap: put rows + grants + optional revoke atomically."""

        self.begin()
        try:
            for index, raw in enumerate(rows, start=1):
                row = {column: raw[column] for column in _ROW_COLUMNS}
                self.put(
                    record_id=f"{record_prefix}-{index:04d}",
                    subject_id=raw["subject_id"],
                    revision=0,
                    row=row,
                    atomic=True,
                )
            for subject_id in grants:
                self.grant(subject_id, purpose, atomic=True)
            if revoke is not None:
                self.revoke(revoke, purpose, atomic=True)
            self.commit()
        except Exception:
            self.rollback()
            raise
        return {
            "subjects": len(grants),
            "purpose": purpose,
            "last_seq": self.last_seq(),
        }


def _auth_payload(subject_id: str, purpose: str, action: str) -> dict[str, object]:
    return {
        "kind": "auth",
        "subject_id": subject_id,
        "purpose": purpose,
        "action": action,
    }
