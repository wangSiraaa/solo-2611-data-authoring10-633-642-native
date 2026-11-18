"""Execute a declarative JSON plan against the service and publisher.

A plan is a list of steps; each step is an object with an ``op`` key.  Drafts
are addressed by the ``request_id`` that produced them (via ``"@tag"``) so the
plan does not need to know generated draft ids ahead of time.  ``expect_error``
records the machine code a step is *required* to raise; the executor turns the
step into a pass/fail fact instead of aborting the plan.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Mapping

from .data_service import DataService
from .errors import ServiceError
from .material import Material
from .publisher import MicrodataPublisher


class PlanExecutor:
    def __init__(self, material: Material, db_path: str | Path, releases_dir: str | Path):
        self.material = material
        self.db_path = str(db_path)
        self.releases_dir = str(releases_dir)
        self.service = DataService(self.db_path, material.domains)
        self.publisher = MicrodataPublisher(
            self.service, material.domains, self.releases_dir
        )
        self.tags: dict[str, str] = {}
        self.results: list[dict[str, Any]] = []

    def close(self) -> None:
        self.service.close()

    def run(self, plan: Mapping[str, Any]) -> list[dict[str, Any]]:
        for index, step in enumerate(plan["steps"]):
            outcome = self._one(step)
            outcome["step"] = index
            self.results.append(outcome)
            if outcome.get("fatal"):
                break
        return self.results

    # ------------------------------------------------------------------ #
    def _one(self, step: Mapping[str, Any]) -> dict[str, Any]:
        op = step.get("op")
        try:
            handler = getattr(self, f"_op_{op}", None)
            if handler is None:
                raise ValueError(f"unknown plan op {op!r}")
            result = handler(step)
            expected = step.get("expect_error")
            if expected:
                return {
                    "op": op,
                    "name": step.get("name"),
                    "ok": False,
                    "unexpected_success": True,
                    "error": None,
                    "result": _compact(result),
                }
            mismatch = (
                _mismatches(step["expect"], result) if "expect" in step else []
            )
            return {
                "op": op,
                "name": step.get("name"),
                "ok": not mismatch,
                "result": _compact(result),
                **({"expect_mismatches": mismatch} if mismatch else {}),
            }
        except ServiceError as exc:
            expected = step.get("expect_error")
            return {
                "op": op,
                "name": step.get("name"),
                "ok": expected == exc.code,
                "expected_error": expected,
                "error": exc.code,
                "message": exc.message,
                "details": exc.details,
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "op": op,
                "name": step.get("name"),
                "ok": False,
                "error": type(exc).__name__,
                "message": str(exc),
                "fatal": True,
            }

    # ------------------------------------------------------------------ #
    def _resolve_k(self, value: Any) -> int:
        if value == "@head":
            return self.service.last_seq()
        return int(value)

    def _op_reopen(self, step: Mapping[str, Any]) -> Any:
        """Close and reopen every connection, proving state is durable."""

        before = {
            "service_head": self.service.view_head(),
            "global_head": self.service.global_seq(),
            "drafts": len(self.publisher.list_drafts()),
            "releases": len(self.publisher.list_releases()),
        }
        self.publisher = None  # type: ignore[assignment]
        self.service.close()
        self.service = DataService(self.db_path, self.material.domains)
        self.publisher = MicrodataPublisher(
            self.service, self.material.domains, self.releases_dir
        )
        after = {
            "service_head": self.service.view_head(),
            "global_head": self.service.global_seq(),
            "drafts": len(self.publisher.list_drafts()),
            "releases": len(self.publisher.list_releases()),
        }
        return {"before": before, "after": after, "preserved": before == after}


    def _op_bootstrap(self, step: Mapping[str, Any]) -> Any:
        """Insert material rows + initial grants (+ optional revoke) atomically."""

        return self.service.initialise(
            self.material.rows,
            purpose=step.get("purpose", self.material.purpose),
            grants=step.get("grants", list(self.material.initial_grants)),
            revoke=step.get("revoke", self.material.revoke),
            record_prefix=step.get("record_prefix", "rec"),
        )

    def _op_head(self, step: Mapping[str, Any]) -> Any:
        return {
            "head": self.service.last_seq(),
            "operations": self.service.operation_log(),
        }

    def _op_put(self, step: Mapping[str, Any]) -> Any:
        return self.service.put(
            step["record_id"],
            step["subject_id"],
            step["revision"],
            step.get("row"),
            bool(step.get("retracted", False)),
            op_id=step.get("op_id"),
        )

    def _op_grant(self, step: Mapping[str, Any]) -> Any:
        return self.service.grant(
            step["subject"], step["purpose"], op_id=step.get("op_id")
        )

    def _op_revoke(self, step: Mapping[str, Any]) -> Any:
        return self.service.revoke(
            step["subject"], step["purpose"], op_id=step.get("op_id")
        )

    def _op_view(self, step: Mapping[str, Any]) -> Any:
        rows = self.service.view(
            self._resolve_k(step["k"]), step["purpose"]
        )
        return {
            "k": self._resolve_k(step["k"]),
            "purpose": step["purpose"],
            "rows": [row.private_fields() | row.public_fields() for row in rows],
        }

    def _op_subject_view(self, step: Mapping[str, Any]) -> Any:
        return self.service.subject_view(
            step["subject"], self._resolve_k(step["k"]), step["purpose"]
        )

    def _op_draft(self, step: Mapping[str, Any]) -> Any:
        draft = self.publisher.draft(
            step["request_id"],
            self._resolve_k(step["k"]),
            step["purpose"],
            step["K"],
            step["D"],
            step["weights"],
        )
        tag = step.get("tag")
        if tag:
            self.tags[tag] = draft["draft_id"]
            self.tags[f"{tag}:state"] = draft["state"]
        return draft

    def _op_subjects(self, step: Mapping[str, Any]) -> Any:
        """Return just the subject ids of a prefix view (compact assertion)."""

        k = self._resolve_k(step["k"])
        rows = self.service.view(k, step["purpose"])
        return {"k": k, "subjects": [row.subject_id for row in rows]}

    def _draft_id(self, step: Mapping[str, Any]) -> str:
        ref = step["draft"]
        if isinstance(ref, str) and ref.startswith("@"):
            return self.tags[ref[1:]]
        return str(ref)

    def _op_review(self, step: Mapping[str, Any]) -> Any:
        return self.publisher.review(
            self._draft_id(step), step["decision"], step["reason"]
        )

    def _op_release(self, step: Mapping[str, Any]) -> Any:
        release = self.publisher.release(
            self._draft_id(step), step["release_id"]
        )
        if step.get("tag"):
            self.tags[step["tag"]] = release["release_id"]
        return release

    def _op_inspect(self, step: Mapping[str, Any]) -> Any:
        return self.publisher.inspect_draft(self._draft_id(step))

    def _op_get_release(self, step: Mapping[str, Any]) -> Any:
        ref = step["release_id"]
        if isinstance(ref, str) and ref.startswith("@"):
            ref = self.tags[ref[1:]]
        return self.publisher.get_release(ref)

    def _op_private_ledger(self, step: Mapping[str, Any]) -> Any:
        ref = step["release_id"]
        if isinstance(ref, str) and ref.startswith("@"):
            ref = self.tags[ref[1:]]
        return self.publisher.get_private_file(ref)

    def _op_consent_history(self, step: Mapping[str, Any]) -> Any:
        return self.service.consent_history(step["subject"], step["purpose"])

    def _op_record_history(self, step: Mapping[str, Any]) -> Any:
        return self.service.record_history(step["subject"])

    def _op_race(self, step: Mapping[str, Any]) -> Any:
        """Controlled two-connection competition, deterministic by a gate."""

        winner = step["winner"]
        draft_id = self._draft_id(step)
        release_id = step["release_id"]
        subject = step["revoke_subject"]
        purpose = step["purpose"]

        release_outcome: dict[str, Any] = {}
        revoke_outcome: dict[str, Any] = {}

        def do_release() -> None:
            conn = DataService(self.db_path, self.material.domains)
            try:
                pub = MicrodataPublisher(conn, self.material.domains, self.releases_dir)
                release_outcome["result"] = pub.release(draft_id, release_id)
            except ServiceError as exc:
                release_outcome["error"] = exc.code
                release_outcome["message"] = exc.message
            finally:
                conn.close()

        def do_revoke() -> None:
            conn = DataService(self.db_path, self.material.domains)
            try:
                revoke_outcome["result"] = conn.revoke(subject, purpose)
            except ServiceError as exc:
                revoke_outcome["error"] = exc.code
                revoke_outcome["message"] = exc.message
            finally:
                conn.close()

        first_done = threading.Event()

        if winner == "release":
            first = threading.Thread(target=lambda: (do_release(), first_done.set()))
            second = threading.Thread(
                target=lambda: (first_done.wait(), do_revoke())
            )
        elif winner == "revoke":
            first = threading.Thread(target=lambda: (do_revoke(), first_done.set()))
            second = threading.Thread(
                target=lambda: (first_done.wait(), do_release())
            )
        else:
            raise ValueError("race.winner must be 'release' or 'revoke'")

        first.start()
        second.start()
        first.join()
        second.join()

        return {
            "winner": winner,
            "release": release_outcome,
            "revoke": revoke_outcome,
            "release_seq": self._seq_of("release", release_id),
        }

    def _seq_of(self, kind: str, ref: str) -> int | None:
        row = self.service.connection.execute(
            "SELECT seq FROM commit_log WHERE kind=? AND ref=? ORDER BY seq DESC LIMIT 1",
            (kind, ref),
        ).fetchone()
        return None if row is None else int(row["seq"])


def load_plan(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _compact(value: Any, depth: int = 0) -> Any:
    """Trim large private row lists so plan reports stay readable."""

    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in ("rows", "operations", "public_payload", "group_stats") and \
                    isinstance(item, list) and len(item) > 12:
                out[key] = {"count": len(item), "sample": item[:4]}
            else:
                out[key] = _compact(item, depth + 1)
        return out
    if isinstance(value, list):
        return [_compact(item, depth + 1) for item in value]
    return value


def _mismatches(expected: Any, actual: Any, path: str = "") -> list[str]:
    """Compare an ``expect`` fragment against a step result.

    Lists of strings are compared as sets when the expected value has a
    ``$set`` wrapper, otherwise structurally.  Missing keys are reported.
    """

    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            if "$len" in expected and isinstance(actual, list):
                if len(actual) != expected["$len"]:
                    return [
                        f"{path or '<root>'}: length {len(actual)} != "
                        f"{expected['$len']}"
                    ]
                return []
            return [f"{path or '<root>'}: expected object, got {type(actual).__name__}"]
        problems: list[str] = []
        for key, want in expected.items():
            if key == "$len":
                continue
            if key not in actual:
                problems.append(f"{path}.{key}: missing (want {want!r})")
                continue
            problems.extend(_mismatches(want, actual[key], f"{path}.{key}"))
        if "$len" in expected:
            target = actual
            if not isinstance(target, (list, dict)) or len(target) != expected["$len"]:
                problems.append(
                    f"{path or '<root>'}: length {len(target) if hasattr(target,'__len__') else '?'} "
                    f"!= {expected['$len']}"
                )
        return problems
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return [f"{path}: expected list"]
        if len(expected) != len(actual):
            return [f"{path}: length {len(actual)} != {len(expected)}"]
        problems = []
        for index, (want, got) in enumerate(zip(expected, actual)):
            problems.extend(_mismatches(want, got, f"{path}[{index}]"))
        return problems
    if expected != actual:
        return [f"{path or '<root>'}: {actual!r} != {expected!r}"]
    return []
