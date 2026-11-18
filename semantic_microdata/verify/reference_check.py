#!/usr/bin/env python3
"""Independent verification of a published release.

Deliberately re-derives everything from the raw material + read-only SQLite
history; it imports only generic helpers (json/sqlite3/itertools) from the
standard library and never calls the package's optimizer or grouping code.

Checks
------
1. The frozen private slice equals the authoritative authorised view at k.
2. Exhaustive enumeration of all level vectors over the raw ancestor tables
   reproduces the stored minimum loss and the lexicographically smallest L.
3. Every group on the stored vector meets K rows and D distinct sensitive.
4. The public file is exactly the sorted generalised multiset (same
   multiplicity, no dedup, no sampling) with raw sensitive values.
5. No subject_id / record_id / revision / commit sequence in the public file.
6. The public file's SHA-256 equals the digest recorded at release time.

Usage
-----
    python3 verify/reference_check.py \
        --material ../case.json --db state.db --release-id rel-1
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterable


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _chains(hierarchies: dict[str, dict[str, list[str]]], column: str):
    return {leaf: tuple(chain) for leaf, chain in hierarchies[column].items()}


def _gen(chains, leaf: str, level: int) -> str:
    return chains[leaf][level]


def _vectors(hierarchies):
    columns = ("age", "district", "job")
    heights = []
    for column in columns:
        chains = _chains(hierarchies, column)
        heights.append(len(next(iter(chains.values()))))
    return columns, heights, list(itertools.product(*[range(h) for h in heights]))


def _groups(rows, levels, hierarchies, columns):
    buckets: dict[tuple[str, str, str], list[str]] = {}
    for row in rows:
        triple = tuple(
            _gen(_chains(hierarchies, column), row[column], levels[i])
            for i, column in enumerate(columns)
        )
        buckets.setdefault(triple, []).append(row["sensitive"])
    return buckets


def recompute_view(
    conn: sqlite3.Connection, k: int, purpose: str
) -> list[tuple[Any, ...]]:
    rows = conn.execute(
        "SELECT * FROM operations WHERE service_seq<=? ORDER BY service_seq", (k,)
    ).fetchall()
    latest_data: dict[str, sqlite3.Row] = {}
    latest_consent: dict[str, sqlite3.Row] = {}
    for row in rows:
        if row["kind"] == "data":
            current = latest_data.get(row["subject_id"])
            if current is None or row["revision"] > current["revision"]:
                latest_data[row["subject_id"]] = row
        else:
            latest_consent[row["subject_id"]] = row
    out = []
    for subject, data in latest_data.items():
        if data["retracted"]:
            continue
        consent = latest_consent.get(subject)
        if consent is None or consent["kind"] != "grant":
            continue
        out.append(
            (
                subject,
                data["record_id"],
                data["revision"],
                data["service_seq"],
                data["age"],
                data["district"],
                data["job"],
                data["sensitive"],
            )
        )
    return sorted(out)


def check(
    material_path: Path,
    db_path: Path,
    release_id: str,
    expected_k: int | None = None,
) -> dict[str, Any]:
    material = _load(material_path)
    hierarchies = material["hierarchies"]
    columns, heights, vectors = _vectors(hierarchies)

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        release = conn.execute(
            "SELECT * FROM releases WHERE release_id=?", (release_id,)
        ).fetchone()
        if release is None:
            raise SystemExit(f"no such release: {release_id}")
        draft = conn.execute(
            "SELECT * FROM drafts WHERE draft_id=?", (release["draft_id"],)
        ).fetchone()
        ledger = _load(Path(release["private_path"]))
        public = _load(Path(release["public_path"]))
        public_text = Path(release["public_path"]).read_text(encoding="utf-8")

        source_rows = [
            {
                "age": item["age"],
                "district": item["district"],
                "job": item["job"],
                "sensitive": item["sensitive"],
            }
            for item in ledger["frozen_slice"]["rows"]
        ]
        purpose = draft["purpose"]
        k = draft["prefix_k"]
        K, D = draft["req_k"], draft["req_d"]
        weights = json.loads(draft["weights"])

        problems: list[str] = []

        # 1. Exact prefix-k slice.
        recomputed = recompute_view(conn, k, purpose)
        stored = sorted(
            (
                item["subject_id"],
                item["record_id"],
                item["revision"],
                item["version_commit_seq"],
                item["age"],
                item["district"],
                item["job"],
                item["sensitive"],
            )
            for item in ledger["frozen_slice"]["rows"]
        )
        if recomputed != stored:
            problems.append("frozen slice does not match authoritative prefix view")

        # 2. Independent optimum.
        feasible: list[tuple[tuple[int, int, int], int]] = []
        for levels in vectors:
            buckets = _groups(source_rows, levels, hierarchies, columns)
            if all(
                len(values) >= K and len(set(values)) >= D
                for values in buckets.values()
            ):
                loss = sum(
                    levels[i] * weights[i] * len(source_rows) for i in range(3)
                )
                feasible.append((levels, loss))

        stored_levels = tuple(json.loads(draft["levels"])) if draft["levels"] else None
        stored_loss = draft["loss"]
        if not feasible:
            problems.append("verifier finds no feasible vector but draft is feasible")
        else:
            best_loss = min(loss for _, loss in feasible)
            best_vectors = sorted(lv for lv, loss in feasible if loss == best_loss)
            if stored_loss != best_loss:
                problems.append(
                    f"stored loss {stored_loss} != independently minimal {best_loss}"
                )
            if stored_levels != best_vectors[0]:
                problems.append(
                    f"stored levels {stored_levels} != lexicographically minimal "
                    f"{best_vectors[0]}"
                )

        # 3. K / D on the stored vector.
        buckets = _groups(source_rows, stored_levels, hierarchies, columns)
        group_checks = []
        for triple, values in sorted(buckets.items()):
            group_checks.append(
                {
                    "generalized": list(triple),
                    "count": len(values),
                    "distinct_sensitive": sorted(set(values)),
                    "passes_K": len(values) >= K,
                    "passes_D": len(set(values)) >= D,
                }
            )
            if len(values) < K:
                problems.append(f"group {triple} has {len(values)} < K={K}")
            if len(set(values)) < D:
                problems.append(
                    f"group {triple} has {len(set(values))} < D={D} distinct sensitive"
                )

        # 4. Public multiset, multiplicity and ordering.
        expected_public = [
            {
                "age": triple[0],
                "district": triple[1],
                "job": triple[2],
                "sensitive": sensitive,
            }
            for triple, values in buckets.items()
            for sensitive in values
        ]
        expected_public.sort(
            key=lambda row: (row["age"], row["district"], row["job"], row["sensitive"])
        )
        if len(public["rows"]) != len(source_rows):
            problems.append(
                f"public row count {len(public['rows'])} != input rows "
                f"{len(source_rows)}"
            )
        if public["rows"] != expected_public:
            problems.append(
                "public rows are not the exact generalised multiset in order"
            )

        # 5. Identity leakage.
        public_blob = json.dumps(public)
        leaked = [
            token
            for item in ledger["frozen_slice"]["rows"]
            for token in (item["subject_id"], item["record_id"])
            if token in public_blob
        ]
        if leaked:
            problems.append(f"public file leaks identities: {sorted(set(leaked))}")
        for forbidden in ("commit_seq", "revision", "subject_id", "record_id",
                          "service_seq", "version_op_id"):
            if forbidden in public_blob:
                problems.append(f"public file mentions {forbidden!r}")

        # 6. File digest.
        if hashlib.sha256(public_text.encode("utf-8")).hexdigest() != \
                release["public_sha256"]:
            problems.append("public file digest does not match release record")

        if expected_k is not None and k != expected_k:
            problems.append(f"draft prefix k={k} != expected {expected_k}")

        return {
            "release_id": release_id,
            "draft_id": draft["draft_id"],
            "k": k,
            "released_k": release["released_k"],
            "purpose": purpose,
            "K": K,
            "D": D,
            "weights": weights,
            "row_count": len(source_rows),
            "level_vectors_enumerated": len(vectors),
            "feasible_vector_count": len(feasible),
            "minimum_loss": None if not feasible else min(l for _, l in feasible),
            "stored_levels": None if stored_levels is None else list(stored_levels),
            "stored_loss": stored_loss,
            "groups": group_checks,
            "problems": problems,
            "ok": not problems,
        }
    finally:
        conn.close()


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--material", required=True, type=Path)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--expect-k", type=int, default=None)
    args = parser.parse_args(list(argv) if argv is not None else None)
    report = check(args.material, args.db, args.release_id, args.expect_k)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
