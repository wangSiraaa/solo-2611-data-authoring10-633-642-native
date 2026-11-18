"""Declarative JSON plans that exercise every contract clause end to end.

Run either with the CLI (``python3 -m semantic_microdata.cli plan ...``) or the
small runner at the bottom.  ``expect_error`` marks a step that *must* be
refused with the given machine code; ``expect`` compares a fragment of the
step's result.  Steps are executed against a fresh database next to the plan
unless ``--db`` is given.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PLANS: dict[str, dict] = {}

# --------------------------------------------------------------------------- #
# 1. Main lifecycle over the supplied material: bootstrap, prefix views,
#    historical vs current drafts, review and release.
# --------------------------------------------------------------------------- #
PLANS["main"] = {
    "name": "main-lifecycle",
    "steps": [
        {"op": "bootstrap", "name": "load material + grants + revoke"},
        {"op": "head", "name": "prefix after bootstrap"},
        {"op": "subjects", "name": "prefix before revoke", "k": 8,
         "purpose": "research", "expect": {"subjects": ["s1", "s2", "s3", "s4"]}},
        {"op": "subjects", "name": "prefix after revoke", "k": 9,
         "purpose": "research", "expect": {"subjects": ["s1", "s3", "s4"]}},
        {"op": "draft", "name": "historical draft at k=8", "tag": "old",
         "request_id": "hist-8", "k": 8, "purpose": "research",
         "K": 2, "D": 2, "weights": [1, 0, 0],
         "expect": {"state": "pending", "levels": [0, 0, 0], "loss": 0,
                    "row_count": 4}},
        {"op": "review", "draft": "@old", "decision": "approve",
         "reason": "historical audit", "expect": {"state": "approved"}},
        {"op": "release", "name": "old draft must not release now",
         "draft": "@old", "release_id": "rel-old",
         "expect_error": "CONSENT_MISSING"},
        {"op": "inspect", "name": "old slice still privately reproducible",
         "draft": "@old",
         "expect": {"still_releasable": False, "levels": [0, 0, 0],
                    "row_count": 4}},
        {"op": "draft", "name": "current draft at k=9", "tag": "cur",
         "request_id": "cur-9", "k": 9, "purpose": "research",
         "K": 2, "D": 2, "weights": [1, 0, 0],
         "expect": {"state": "pending", "levels": [1, 0, 0], "loss": 3,
                    "row_count": 3}},
        {"op": "draft", "name": "same request replay is idempotent",
         "request_id": "cur-9", "k": 9, "purpose": "research",
         "K": 2, "D": 2, "weights": [1, 0, 0],
         "expect": {"levels": [1, 0, 0], "loss": 3}},
        {"op": "draft", "name": "same request, different params",
         "request_id": "cur-9", "k": 9, "purpose": "research",
         "K": 3, "D": 2, "weights": [1, 0, 0],
         "expect_error": "DRAFT_CONFLICT"},
        {"op": "review", "draft": "@cur", "decision": "approve", "reason": "ok",
         "expect": {"state": "approved"}},
        {"op": "review", "name": "review decision immutable", "draft": "@cur",
         "decision": "reject", "reason": "changed mind",
         "expect_error": "DRAFT_CONFLICT"},
        {"op": "release", "name": "current release succeeds", "tag": "rel1",
         "draft": "@cur", "release_id": "rel-1",
         "expect": {"status": "released", "k": 9, "levels": [1, 0, 0],
                    "loss": 3, "row_count": 3}},
        {"op": "release", "name": "release replay returns first result",
         "draft": "@cur", "release_id": "rel-1",
         "expect": {"status": "released", "k": 9, "levels": [1, 0, 0]}},
        {"op": "private_ledger", "release_id": "rel-1",
         "expect": {"k": 9, "release_id": "rel-1"}},
    ],
}

# --------------------------------------------------------------------------- #
# 2. Retraction / revision / consent ordering and idempotence.
# --------------------------------------------------------------------------- #
PLANS["ordering"] = {
    "name": "ordering-and-idempotence",
    "steps": [
        {"op": "bootstrap"},
        {"op": "put", "name": "retract s3 row", "record_id": "rec-0003",
         "subject_id": "s3", "revision": 1, "row": None,
         "expect": {"status": "accepted", "retracted": True}},
        {"op": "subjects", "k": "@head", "purpose": "research",
         "expect": {"subjects": ["s1", "s4"]}},
        {"op": "grant", "name": "re-grant s2 does not resurrect s3",
         "subject": "s2", "purpose": "research",
         "expect": {"status": "accepted"}},
        {"op": "subjects", "k": "@head", "purpose": "research",
         "expect": {"subjects": ["s1", "s2", "s4"]}},
        {"op": "put", "name": "revoke s1 by retraction", "record_id": "rec-0001",
         "subject_id": "s1", "revision": 2, "row": None,
         "expect": {"status": "accepted"}},
        {"op": "put", "name": "stale revision refused", "record_id": "rec-0001",
         "subject_id": "s1", "revision": 1,
         "row": {"age": "Y", "district": "R", "job": "J", "sensitive": "x"},
         "expect_error": "STALE_REVISION"},
        {"op": "put", "name": "identical replay of a retraction",
         "record_id": "rec-0001", "subject_id": "s1", "revision": 2,
         "row": None, "expect": {"status": "replay", "retracted": True}},
        {"op": "put", "name": "conflicting content for accepted revision",
         "record_id": "rec-0004", "subject_id": "s4", "revision": 0,
         "row": {"age": "O", "district": "R", "job": "J", "sensitive": "x"},
         "expect_error": "IDENTICAL_RECORD_CONFLICT"},
        {"op": "revoke", "name": "revoke s4", "subject": "s4",
         "purpose": "research", "expect": {"status": "accepted"}},
        {"op": "revoke", "name": "revoke replay is idempotent", "subject": "s4",
         "purpose": "research", "expect": {"status": "replay"}},
        {"op": "grant", "name": "grant s4 again", "subject": "s4",
         "purpose": "research", "expect": {"status": "accepted"}},
        {"op": "subjects", "k": "@head", "purpose": "research",
         "expect": {"subjects": ["s2", "s4"]}},
        {"op": "subject_view", "subject": "s1", "k": "@head",
         "purpose": "research", "expect": {"authorised": False,
                                           "row": None}},
        {"op": "consent_history", "subject": "s4", "purpose": "research",
         "expect": {"$len": 3}},
        {"op": "record_history", "subject": "s1",
         "expect": {"$len": 2}},
    ],
}

# --------------------------------------------------------------------------- #
# 3. Human review rejection leaves the data service and views untouched.
# --------------------------------------------------------------------------- #
PLANS["review"] = {
    "name": "review-isolation",
    "steps": [
        {"op": "bootstrap"},
        {"op": "draft", "tag": "d", "request_id": "reject-1", "k": "@head",
         "purpose": "research", "K": 2, "D": 2, "weights": [1, 0, 0]},
        {"op": "subjects", "k": "@head", "purpose": "research", "tag": "before"},
        {"op": "review", "draft": "@d", "decision": "reject",
         "reason": "not needed", "expect": {"state": "rejected"}},
        {"op": "subjects", "k": "@head", "purpose": "research",
         "expect": {"subjects": ["s1", "s3", "s4"]}},
        {"op": "release", "draft": "@d", "release_id": "rel-nope",
         "expect_error": "DRAFT_NOT_APPROVED"},
        {"op": "draft", "name": "new draft after rejection",
         "request_id": "reject-2", "k": "@head", "purpose": "research",
         "K": 2, "D": 2, "weights": [1, 0, 0],
         "expect": {"state": "pending", "levels": [1, 0, 0]}},
    ],
}

# --------------------------------------------------------------------------- #
# 4. Infeasible and empty slices.
# --------------------------------------------------------------------------- #
PLANS["infeasible"] = {
    "name": "infeasible",
    "steps": [
        {"op": "bootstrap", "grants": ["s1"], "revoke": None},
        {"op": "draft", "tag": "inf", "request_id": "inf-1", "k": "@head",
         "purpose": "research", "K": 2, "D": 2, "weights": [1, 0, 0],
         "expect": {"state": "infeasible", "levels": None, "loss": None,
                    "row_count": 1}},
        {"op": "review", "draft": "@inf", "decision": "approve",
         "reason": "no", "expect_error": "INFEASIBLE"},
        {"op": "release", "draft": "@inf", "release_id": "rel-inf",
         "expect_error": "DRAFT_NOT_APPROVED"},
    ],
}

PLANS["empty"] = {
    "name": "empty-slice",
    "steps": [
        # No subject is authorised, so even the head slice has zero rows.
        {"op": "bootstrap", "grants": [], "revoke": None},
        {"op": "draft", "tag": "empty", "request_id": "empty-1", "k": 0,
         "purpose": "research", "K": 2, "D": 2, "weights": [1, 0, 0],
         "expect": {"state": "pending", "levels": [0, 0, 0], "loss": 0,
                    "row_count": 0}},
        {"op": "draft", "tag": "empty_head", "request_id": "empty-2",
         "k": "@head", "purpose": "research", "K": 2, "D": 2,
         "weights": [1, 0, 0],
         "expect": {"state": "pending", "levels": [0, 0, 0], "loss": 0,
                    "row_count": 0}},
        {"op": "review", "draft": "@empty_head", "decision": "approve",
         "reason": "empty current", "expect": {"state": "approved"}},
        {"op": "release", "tag": "rel-empty", "draft": "@empty_head",
         "release_id": "rel-empty",
         "expect": {"status": "released", "levels": [0, 0, 0], "loss": 0,
                    "row_count": 0}},
        {"op": "private_ledger", "release_id": "rel-empty",
         "expect": {"k": 4}},  # head at bootstrap (0 data + 0 grants + 4 data ops)
    ],
}

# --------------------------------------------------------------------------- #
# 5. Two-connection controlled race, both orders.
# --------------------------------------------------------------------------- #
PLANS["race_revoke_first"] = {
    "name": "race-revoke-first",
    "steps": [
        {"op": "bootstrap"},
        {"op": "draft", "tag": "d", "request_id": "race-r1", "k": "@head",
         "purpose": "research", "K": 2, "D": 2, "weights": [1, 0, 0]},
        {"op": "review", "draft": "@d", "decision": "approve", "reason": "race"},
        {"op": "race", "draft": "@d", "release_id": "rel-race",
         "revoke_subject": "s1", "purpose": "research", "winner": "revoke",
         "expect": {"winner": "revoke"}},
    ],
}

PLANS["race_release_first"] = {
    "name": "race-release-first",
    "steps": [
        {"op": "bootstrap"},
        {"op": "draft", "tag": "d", "request_id": "race-r2", "k": "@head",
         "purpose": "research", "K": 2, "D": 2, "weights": [1, 0, 0]},
        {"op": "review", "draft": "@d", "decision": "approve", "reason": "race"},
        {"op": "race", "draft": "@d", "release_id": "rel-race",
         "revoke_subject": "s1", "purpose": "research", "winner": "release",
         "expect": {"winner": "release"}},
    ],
}

# --------------------------------------------------------------------------- #
# 6. Reopen: everything durable survives a restart.
# --------------------------------------------------------------------------- #
PLANS["reopen"] = {
    "name": "reopen",
    "steps": [
        {"op": "bootstrap"},
        {"op": "draft", "tag": "d", "request_id": "reopen-1", "k": "@head",
         "purpose": "research", "K": 2, "D": 2, "weights": [1, 0, 0]},
        {"op": "review", "draft": "@d", "decision": "approve",
         "reason": "durable"},
        {"op": "release", "draft": "@d", "release_id": "rel-durable"},
        {"op": "reopen", "expect": {"preserved": True}},
        {"op": "inspect", "draft": "@d", "expect": {"still_releasable": True}},
        {"op": "get_release", "release_id": "rel-durable",
         "expect": {"status": "released", "levels": [1, 0, 0]}},
    ],
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    names = argv or list(PLANS)
    here = Path(__file__).resolve().parent
    base = here / "out" / "plans"
    base.mkdir(parents=True, exist_ok=True)
    from semantic_microdata import load_material
    from semantic_microdata.plan import PlanExecutor

    material = load_material(ROOT.parent / "case.json")
    summary = {}
    for name in names:
        plan = PLANS[name]
        db = base / f"{name}.db"
        for suffix in ("", "-wal", "-shm"):
            Path(str(db) + suffix).unlink(missing_ok=True)
        executor = PlanExecutor(material, db, base / f"{name}_releases")
        try:
            results = executor.run(plan)
        finally:
            executor.close()
        summary[name] = {
            "steps": results,
            "ok": all(step["ok"] for step in results),
        }
    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    return 0 if all(item["ok"] for item in summary.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
