"""Small deterministic helpers (canonical JSON, digests, validation)."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer (not bool)")
    if value < 1:
        raise ValueError(f"{name} must be >= 1")
    return value


def non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer (not bool)")
    if value < 0:
        raise ValueError(f"{name} must be >= 0")
    return value


def require_str(value: Any, name: str, *, non_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    if non_empty and not value:
        raise ValueError(f"{name} must not be empty")
    return value


def slice_digest(purpose: str, rows: list[dict[str, Any]]) -> str:
    """Digest of the exact private slice *including* source identities.

    The digest deliberately covers ``subject_id``, ``record_id`` and the
    revision / operation that produced each value, but **not** the consent
    operation sequence.  Consequences:

    * a revision that happens to generalise to identical published bytes still
      changes the digest, so an old draft cannot be released;
    * revoking then re-granting a subject restores the identical authorised
      slice, so the digest returns to the same value -- the slice really is
      the frozen one again.
    """

    payload = {
        "purpose": purpose,
        "rows": [
            {
                "subject_id": row["subject_id"],
                "record_id": row["record_id"],
                "revision": row["revision"],
                "version_op_id": row["version_op_id"],
                "version_commit_seq": row["version_commit_seq"],
                "age": row["age"],
                "district": row["district"],
                "job": row["job"],
                "sensitive": row["sensitive"],
            }
            for row in rows
        ],
    }
    payload["rows"].sort(key=lambda r: (r["subject_id"], r["record_id"], r["revision"]))
    return sha256_hex(canonical_json(payload))
