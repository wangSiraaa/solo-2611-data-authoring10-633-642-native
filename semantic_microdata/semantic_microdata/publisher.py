"""Anonymous microdata publisher with human review and atomic release.

Lifecycle
---------
``draft(request_id, k, purpose, K, D, weights)``
    Freezes the exact authorised private slice at accepted prefix ``k``,
    enumerates every level vector, stores the minimum-loss selection (ties by
    lexicographic ``L``), per-group counts and the public result.  A
    byte-identical retry of the same request returns the original draft; the
    same request id with different parameters is refused.

``review(draft_id, decision, reason)``
    Human approve / reject.  Decisions are persistent and (after the first)
    immutable.  Rejecting never touches the data service.

``release(draft_id, release_id)``
    Only for an approved draft, and only if at *release time* the current
    authorised slice for the draft's purpose is still the frozen
    identity/version set.  The check and the release row are committed in one
    SQLite ``BEGIN IMMEDIATE`` transaction: whichever of a concurrent release
    and revocation commits first determines the outcome.  Successful releases
    write an immutable public file plus a private source ledger.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from .data_service import (
    DataService,
    SliceRow,
    last_seq_at,
    service_head,
    view_at,
)
from .domains import QUASI_COLUMNS, Domains
from .errors import (
    ConsentMissing,
    DraftConflict,
    DraftNotApproved,
    Infeasible,
    InvalidArgument,
    ReleaseConflict,
    StaleDraft,
    UnknownDraft,
    UnknownRelease,
)
from .optimizer import (
    GroupStat,
    Selection,
    infeasibility_report,
    optimal_vectors,
    select_best,
    validate_params,
)
from .util import canonical_json, require_str, sha256_hex

_DECISIONS = {"approve": "approved", "approved": "approved",
              "reject": "rejected", "rejected": "rejected"}


class MicrodataPublisher:
    def __init__(
        self,
        data_service: DataService,
        domains: Domains,
        releases_dir: str | Path,
    ) -> None:
        self.service = data_service
        self.domains = domains
        self.conn = data_service.connection
        self.releases_dir = Path(releases_dir)
        self.releases_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    # Draft
    # ------------------------------------------------------------------ #
    def draft(
        self,
        request_id: str,
        k: int,
        purpose: str,
        K: object,
        D: object,
        weights: Sequence[int],
    ) -> dict[str, Any]:
        request_id = require_str(request_id, "request_id")
        purpose = require_str(purpose, "purpose")
        if isinstance(k, bool) or not isinstance(k, int):
            raise InvalidArgument("k must be an integer")
        if k < 0:
            raise InvalidArgument("k must be >= 0")
        K_int, D_int, weights_t = validate_params(K, D, weights)
        params = {
            "k": k,
            "purpose": purpose,
            "K": K_int,
            "D": D_int,
            "weights": list(weights_t),
        }
        params_hash = sha256_hex(canonical_json(params))

        self._begin()
        try:
            existing = self.conn.execute(
                "SELECT * FROM drafts WHERE request_id=?", (request_id,)
            ).fetchone()
            if existing is not None:
                if existing["params_hash"] != params_hash:
                    raise DraftConflict(
                        f"request {request_id!r} was already used with different "
                        "parameters"
                    )
                result = self._draft_result(existing)
                self._commit()
                return result

            head = service_head(self.conn)
            if k > head:
                raise InvalidArgument(
                    f"k={k} exceeds the current accepted service prefix {head}"
                )

            rows = view_at(self.conn, k, purpose)
            public_rows_input = [row.public_fields() for row in rows]
            created_seq = self._next_seq("draft", request_id)

            try:
                selection = select_best(
                    public_rows_input, self.domains, K_int, D_int, weights_t
                )
                optima = optimal_vectors(
                    public_rows_input, self.domains, K_int, D_int, weights_t
                )
                groups = self._group_stats(public_rows_input, selection.levels)
                public_payload = self._public_payload(public_rows_input, selection)
                private_ledger = self._private_ledger(rows, purpose, k)
                state = "pending"
                infeasible = None
                result_status = "drafted"
            except Infeasible:
                report = infeasibility_report(
                    public_rows_input, self.domains, K_int, D_int
                )
                report.update({"k": k, "purpose": purpose})
                selection = None
                optima = []
                groups = []
                public_payload = []
                private_ledger = self._private_ledger(rows, purpose, k)
                state = "infeasible"
                infeasible = report
                result_status = "infeasible"

            draft_id = "d-" + sha256_hex(
                canonical_json({"request_id": request_id, **params})
            )[:24]
            snapshot_digest = sha256_hex(
                canonical_json(
                    {
                        "purpose": purpose,
                        "levels": list(selection.levels) if selection else None,
                        "rows": [row.private_fields() | row.public_fields()
                                 for row in rows],
                    }
                )
            )
            # Identity/version digest used for release revalidation: does NOT
            # include consent seq, so revoke+regrant of the *same* versions
            # restores the frozen slice.
            identity_digest = self._identity_digest(rows, purpose)

            self.conn.execute(
                "INSERT INTO drafts("
                "draft_id,request_id,purpose,prefix_k,req_k,req_d,weights,params_hash,"
                "created_head_seq,created_seq,state,snapshot_digest,row_count,"
                "private_ledger,levels,loss,optimal_vectors,group_stats,"
                "public_payload,infeasible_report,result) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    draft_id,
                    request_id,
                    purpose,
                    k,
                    K_int,
                    D_int,
                    canonical_json(list(weights_t)),
                    params_hash,
                    head,
                    created_seq,
                    state,
                    snapshot_digest,
                    len(rows),
                    canonical_json(private_ledger),
                    None if selection is None else canonical_json(list(selection.levels)),
                    None if selection is None else selection.loss,
                    canonical_json([[list(lv), lo] for lv, lo in optima]),
                    canonical_json([group.as_dict() for group in groups]),
                    canonical_json(public_payload),
                    None if infeasible is None else canonical_json(infeasible),
                    canonical_json(
                        {
                            "status": result_status,
                            "levels": None if selection is None else list(selection.levels),
                            "loss": None if selection is None else selection.loss,
                            "row_count": len(rows),
                            "identity_digest": identity_digest,
                        }
                    ),
                ),
            )
            self._audit(created_seq, "draft_created", {
                "draft_id": draft_id,
                "request_id": request_id,
                "k": k,
                "purpose": purpose,
                "state": state,
            })
            row = self.conn.execute(
                "SELECT * FROM drafts WHERE draft_id=?", (draft_id,)
            ).fetchone()
            result = self._draft_result(row)
            self._commit()
            return result
        except Exception:
            self._rollback()
            raise

    # ------------------------------------------------------------------ #
    # Human review
    # ------------------------------------------------------------------ #
    def review(
        self, draft_id: str, decision: str, reason: str
    ) -> dict[str, Any]:
        draft_id = require_str(draft_id, "draft_id")
        if not isinstance(reason, str):
            raise InvalidArgument("reason must be a string")
        if decision not in _DECISIONS:
            raise InvalidArgument(
                "decision must be one of 'approve', 'approved', 'reject', "
                "'rejected'"
            )
        target = _DECISIONS[decision]

        self._begin()
        try:
            row = self.conn.execute(
                "SELECT * FROM drafts WHERE draft_id=?", (draft_id,)
            ).fetchone()
            if row is None:
                raise UnknownDraft(f"unknown draft {draft_id!r}")
            if row["state"] == "infeasible" and target == "approved":
                raise Infeasible(
                    "an infeasible draft cannot be approved",
                    details=json.loads(row["infeasible_report"] or "{}"),
                )
            if row["state"] in ("approved", "rejected"):
                if row["state"] == target and row["review_reason"] == reason:
                    result = self._draft_result(row)
                    self._commit()
                    return result
                raise DraftConflict(
                    f"draft {draft_id!r} was already {row['state']}; the human "
                    "decision is immutable"
                )
            seq = self._next_seq("review", draft_id)
            self.conn.execute(
                "UPDATE drafts SET state=?,review_decision=?,review_reason=?,"
                "reviewed_seq=? WHERE draft_id=?",
                (target, target, reason, seq, draft_id),
            )
            self._audit(seq, "review_" + target, {
                "draft_id": draft_id,
                "reason": reason,
            })
            row = self.conn.execute(
                "SELECT * FROM drafts WHERE draft_id=?", (draft_id,)
            ).fetchone()
            result = self._draft_result(row)
            self._commit()
            return result
        except Exception:
            self._rollback()
            raise

    # ------------------------------------------------------------------ #
    # Release
    # ------------------------------------------------------------------ #
    def release(self, draft_id: str, release_id: str) -> dict[str, Any]:
        draft_id = require_str(draft_id, "draft_id")
        release_id = require_str(release_id, "release_id")

        self._begin()
        written: list[Path] = []
        try:
            first = self.conn.execute(
                "SELECT * FROM releases WHERE release_id=?", (release_id,)
            ).fetchone()
            if first is not None:
                if first["draft_id"] != draft_id:
                    raise ReleaseConflict(
                        f"release id {release_id!r} already belongs to draft "
                        f"{first['draft_id']!r}"
                    )
                result = json.loads(first["payload"])
                self._commit()
                return result

            draft = self.conn.execute(
                "SELECT * FROM drafts WHERE draft_id=?", (draft_id,)
            ).fetchone()
            if draft is None:
                raise UnknownDraft(f"unknown draft {draft_id!r}")

            already = self.conn.execute(
                "SELECT release_id FROM releases WHERE draft_id=?", (draft_id,)
            ).fetchone()
            if already is not None:
                raise ReleaseConflict(
                    f"draft {draft_id!r} was already released as "
                    f"{already['release_id']!r}"
                )
            if draft["state"] != "approved":
                raise DraftNotApproved(
                    f"draft {draft_id!r} is {draft['state']}, not approved"
                )

            purpose = draft["purpose"]
            service_k = service_head(self.conn)
            head = last_seq_at(self.conn)
            current_rows = view_at(self.conn, service_k, purpose)
            frozen_rows = json.loads(draft["private_ledger"])["rows"]

            # Integrity invariant: the stored frozen slice must still equal the
            # authoritative prefix-k slice.  History is immutable, so this does
            # not depend on freshness; it catches a tampered ledger.
            recomputed = view_at(self.conn, draft["prefix_k"], purpose)
            frozen_slice = self._slice_from_ledger(frozen_rows)
            if self._identity_digest(frozen_slice, purpose) != self._identity_digest(
                recomputed, purpose
            ):
                raise StaleDraft(
                    "stored frozen slice no longer matches authoritative history "
                    f"at k={draft['prefix_k']}",
                    details={"draft_id": draft_id},
                )

            current_identities = self._identity_set(current_rows)
            frozen_identities = {
                (
                    item["subject_id"],
                    item["record_id"],
                    item["revision"],
                    item["version_op_id"],
                )
                for item in frozen_rows
            }

            # Explicit per-source checks (clear reasons) before the whole-slice
            # digest comparison.
            self._check_sources_current(frozen_identities, purpose)
            current_digest = self._identity_digest(current_rows, purpose)
            frozen_digest = json.loads(draft["result"])["identity_digest"]
            if current_identities != frozen_identities or current_digest != frozen_digest:
                raise StaleDraft(
                    f"the authorised slice for purpose {purpose!r} changed since "
                    "draft creation; freeze a new draft",
                    details={
                        "added": sorted(
                            [list(item) for item in current_identities
                             - frozen_identities]
                        ),
                        "removed": sorted(
                            [list(item) for item in frozen_identities
                             - current_identities]
                        ),
                        "frozen_digest": frozen_digest,
                        "current_digest": current_digest,
                        "head_seq": head,
                    },
                )

            seq = self._next_seq("release", release_id)
            public_path, private_path, public_sha, private_sha, payload = (
                self._write_release_files(
                    draft, release_id, seq, service_k, head, purpose
                )
            )
            written.extend([public_path, private_path])
            self.conn.execute(
                "INSERT INTO releases("
                "release_id,draft_id,release_seq,released_k,released_head_seq,"
                "public_path,private_path,public_sha256,private_sha256,payload) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    release_id,
                    draft_id,
                    seq,
                    service_k,
                    head,
                    str(public_path),
                    str(private_path),
                    public_sha,
                    private_sha,
                    canonical_json(payload),
                ),
            )
            self._audit(seq, "released", {
                "release_id": release_id,
                "draft_id": draft_id,
                "levels": payload["levels"],
                "loss": payload["loss"],
            })
            self._commit()
            return payload
        except Exception:
            self._rollback()
            # If the transaction failed after writing files, do not leave an
            # unreferenced public/private pair behind.
            for path in written:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise

    # ------------------------------------------------------------------ #
    # Inspection (private)
    # ------------------------------------------------------------------ #
    def inspect_draft(self, draft_id: str) -> dict[str, Any]:
        """Return the frozen private slice + result, regardless of staleness.

        This is the *private verification* path: an old slice at ``k`` stays
        fully reproducible even after later data / consent changes.  It is not
        a statement that the draft may be released.
        """

        draft_id = require_str(draft_id, "draft_id")
        row = self.conn.execute(
            "SELECT * FROM drafts WHERE draft_id=?", (draft_id,)
        ).fetchone()
        if row is None:
            raise UnknownDraft(f"unknown draft {draft_id!r}")
        result = self._draft_result(row)
        current = self._identity_digest(
            view_at(self.conn, service_head(self.conn), row["purpose"]),
            row["purpose"],
        )
        result["still_releasable"] = (
            row["state"] == "approved"
            and current == json.loads(row["result"])["identity_digest"]
        )
        result["current_identity_digest"] = current
        return result

    def get_release(self, release_id: str) -> dict[str, Any]:
        release_id = require_str(release_id, "release_id")
        row = self.conn.execute(
            "SELECT * FROM releases WHERE release_id=?", (release_id,)
        ).fetchone()
        if row is None:
            raise UnknownRelease(f"unknown release {release_id!r}")
        payload = json.loads(row["payload"])
        payload["public_sha256"] = row["public_sha256"]
        payload["private_sha256"] = row["private_sha256"]
        return payload

    def list_drafts(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM drafts ORDER BY created_seq"
        ).fetchall()
        return [self._draft_result(row) for row in rows]

    def list_releases(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT release_id, draft_id, release_seq, released_k, "
            "released_head_seq, public_path, private_path, public_sha256, "
            "private_sha256 FROM releases ORDER BY release_seq"
        ).fetchall()
        return [dict(row) for row in rows]

    def get_private_file(self, release_id: str) -> dict[str, Any]:
        """Private source ledger for an already published release."""

        row = self.conn.execute(
            "SELECT private_path FROM releases WHERE release_id=?", (release_id,)
        ).fetchone()
        if row is None:
            raise UnknownRelease(f"unknown release {release_id!r}")
        return json.loads(Path(row["private_path"]).read_text(encoding="utf-8"))

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _begin(self) -> None:
        self.conn.execute("BEGIN IMMEDIATE")

    def _commit(self) -> None:
        self.conn.execute("COMMIT")

    def _rollback(self) -> None:
        if self.conn.in_transaction:
            self.conn.execute("ROLLBACK")

    def _next_seq(self, kind: str, ref: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO commit_log(kind,ref) VALUES (?,?)", (kind, ref)
        )
        return int(cur.lastrowid)

    def _audit(self, at_seq: int, event: str, detail: Mapping[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO audit(at_seq,event,detail) VALUES (?,?,?)",
            (at_seq, event, canonical_json(detail)),
        )

    def _group_stats(
        self, rows: Sequence[Mapping[str, str]], levels: tuple[int, int, int]
    ) -> list[GroupStat]:
        from .optimizer import group_stats

        return group_stats(rows, levels, self.domains)

    def _generalize_row(
        self, row: Mapping[str, str], levels: tuple[int, int, int]
    ) -> tuple[str, str, str]:
        return tuple(  # type: ignore[return-value]
            self.domains.hierarchies[column].generalize(row[column], levels[i])
            for i, column in enumerate(QUASI_COLUMNS)
        )

    def _public_payload(
        self, rows: Sequence[Mapping[str, str]], selection: Selection
    ) -> list[dict[str, str]]:
        enriched = []
        for row in rows:
            triple = self._generalize_row(row, selection.levels)
            enriched.append((triple, row["sensitive"]))
        # Sort by generalised triple then sensitive; identical rows keep their
        # multiplicity because they are individual list entries, never a set.
        enriched.sort(key=lambda item: (item[0], item[1]))
        return [
            {
                "age": triple[0],
                "district": triple[1],
                "job": triple[2],
                "sensitive": sensitive,
            }
            for triple, sensitive in enriched
        ]

    def _private_ledger(
        self, rows: Sequence[SliceRow], purpose: str, k: int
    ) -> dict[str, Any]:
        return {
            "purpose": purpose,
            "k": k,
            "rows": [
                row.private_fields() | row.public_fields() for row in rows
            ],
        }

    def _identity_set(
        self, rows: Sequence[SliceRow]
    ) -> set[tuple[str, str, int, str | None]]:
        return {
            (row.subject_id, row.record_id, row.revision, row.version_op_id)
            for row in rows
        }

    def _identity_digest(self, rows: Sequence[SliceRow], purpose: str) -> str:
        payload = {
            "purpose": purpose,
            "rows": [
                {
                    "subject_id": row.subject_id,
                    "record_id": row.record_id,
                    "revision": row.revision,
                    "version_op_id": row.version_op_id,
                    "version_commit_seq": row.version_commit_seq,
                    "age": row.age,
                    "district": row.district,
                    "job": row.job,
                    "sensitive": row.sensitive,
                }
                for row in sorted(rows, key=lambda r: r.subject_id)
            ],
        }
        return sha256_hex(canonical_json(payload))

    def _slice_from_ledger(self, items: Sequence[Mapping[str, Any]]) -> list[SliceRow]:
        """Rebuild private slice rows from a stored ledger, preserving order."""

        out = []
        for item in items:
            out.append(
                SliceRow(
                    subject_id=item["subject_id"],
                    record_id=item["record_id"],
                    revision=int(item["revision"]),
                    version_op_id=item["version_op_id"],
                    version_commit_seq=int(item["version_commit_seq"]),
                    version_commit_order=int(item.get("version_commit_order", 0)),
                    consent_commit_seq=int(item["consent_commit_seq"]),
                    age=item["age"],
                    district=item["district"],
                    job=item["job"],
                    sensitive=item["sensitive"],
                )
            )
        return out

    def _check_sources_current(
        self, frozen: set[tuple[str, str, int, str | None]], purpose: str
    ) -> None:
        for subject_id, record_id, revision, _op in sorted(frozen):
            live = self.conn.execute(
                "SELECT * FROM live_records WHERE subject_id=?", (subject_id,)
            ).fetchone()
            if (
                live is None
                or live["record_id"] != record_id
                or int(live["revision"]) != revision
                or int(live["retracted"]) != 0
            ):
                raise StaleDraft(
                    f"source record for subject {subject_id!r} is no longer the "
                    "frozen non-retracted revision",
                    details={"subject_id": subject_id},
                )
            auth = self.conn.execute(
                "SELECT granted FROM live_auth WHERE subject_id=? AND purpose=?",
                (subject_id, purpose),
            ).fetchone()
            if auth is None or int(auth["granted"]) != 1:
                raise ConsentMissing(
                    f"subject {subject_id!r} no longer grants purpose {purpose!r}",
                    details={"subject_id": subject_id, "purpose": purpose},
                )

    def _write_release_files(
        self,
        draft: Any,
        release_id: str,
        release_seq: int,
        service_k: int,
        head: int,
        purpose: str,
    ) -> tuple[Path, Path, str, str, dict[str, Any]]:
        public_rows = json.loads(draft["public_payload"])
        levels = json.loads(draft["levels"])
        loss = draft["loss"]
        row_count = draft["row_count"]
        public_document = {
            "release_id": release_id,
            "draft_id": draft["draft_id"],
            "release_seq": release_seq,
            # No requested prefix / source position in the public file; the
            # exact slice boundary lives only in the private ledger.
            "purpose": purpose,
            "K": draft["req_k"],
            "D": draft["req_d"],
            "weights": json.loads(draft["weights"]),
            "levels": levels,
            "loss": loss,
            "row_count": row_count,
            "rows": public_rows,
        }
        private_document = {
            "release_id": release_id,
            "draft_id": draft["draft_id"],
            "release_seq": release_seq,
            "request_id": draft["request_id"],
            "purpose": purpose,
            "k": draft["prefix_k"],
            "released_k": service_k,
            "released_head_seq": head,
            "levels": levels,
            "loss": loss,
            "frozen_slice": json.loads(draft["private_ledger"]),
            "review": {
                "decision": draft["review_decision"],
                "reason": draft["review_reason"],
                "seq": draft["reviewed_seq"],
            },
        }

        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", release_id)[:60]
        if not slug or slug in (".", ".."):
            slug = "release"
        suffix = sha256_hex(release_id)[:12]
        public_path = self.releases_dir / f"{slug}-{suffix}.public.json"
        private_path = self.releases_dir / f"{slug}-{suffix}.private.json"

        public_sha = self._atomic_write(public_path, public_document)
        private_sha = self._atomic_write(private_path, private_document)
        payload = {
            "status": "released",
            "release_id": release_id,
            "draft_id": draft["draft_id"],
            "release_seq": release_seq,
            "k": draft["prefix_k"],
            "released_k": service_k,
            "purpose": purpose,
            "levels": levels,
            "loss": loss,
            "row_count": row_count,
            "public_path": str(public_path),
            "private_path": str(private_path),
            "public_sha256": public_sha,
            "private_sha256": private_sha,
        }
        return public_path, private_path, public_sha, private_sha, payload

    def _atomic_write(self, path: Path, document: Any) -> str:
        text = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False)
        text += "\n"
        tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        return sha256_hex(text)

    def _draft_result(self, row: Any) -> dict[str, Any]:
        levels = None if row["levels"] is None else json.loads(row["levels"])
        groups = json.loads(row["group_stats"] or "[]")
        result = {
            "status": json.loads(row["result"])["status"],
            "draft_id": row["draft_id"],
            "request_id": row["request_id"],
            "purpose": row["purpose"],
            "k": row["prefix_k"],
            "K": row["req_k"],
            "D": row["req_d"],
            "weights": json.loads(row["weights"]),
            "state": row["state"],
            "row_count": row["row_count"],
            "levels": levels,
            "loss": row["loss"],
            "optimal_vectors": json.loads(row["optimal_vectors"] or "[]"),
            "group_stats": groups,
            "public_payload": json.loads(row["public_payload"] or "[]"),
            "review": {
                "decision": row["review_decision"],
                "reason": row["review_reason"],
                "seq": row["reviewed_seq"],
            },
            "created_head_seq": row["created_head_seq"],
            "created_seq": row["created_seq"],
            "identity_digest": json.loads(row["result"])["identity_digest"],
        }
        if row["infeasible_report"] is not None:
            result["infeasible_report"] = json.loads(row["infeasible_report"])
        return result
