"""Build the six+ demonstration scenarios against the supplied material.

Each scenario opens a fresh database so cases stay independent.  Everything
uses the *real* service and publisher entry points; nothing is stubbed.

Scenarios
---------
1. unique raw groups, feasible after generalisation
2. K met but D fails  (needs D=3 with only two sensitive values)
3. repeated identical sensitive value inside one group (counts once for D,
   every row counts for K)
4. weight-0 column produces a loss tie, resolved by lexicographic L
5. fully infeasible slice (single subject)
6. empty slice (empty release, L=(0,0,0), loss 0)
7. consent change before/after: fixed old k stays reproducible, new k gives a
   different feasible optimum, old draft cannot be released
8. revision / retraction / grant ordering, rejection does not affect data,
   approval then retraction invalidates the draft
9. controlled two-connection race: revoke first vs release first
10. reopen: state, human decisions, releases and files survive

Run ``python3 examples/scenarios.py`` for a JSON summary and ``--write`` to
persist each scenario's state under ``examples/out/``.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from semantic_microdata import (  # noqa: E402
    DataService,
    MicrodataPublisher,
    ServiceError,
    load_material,
)

MATERIAL = ROOT.parent / "case.json"
OUT = ROOT / "examples" / "out"


def _fresh(name: str) -> tuple[DataService, MicrodataPublisher, Path]:
    base = OUT / name
    base.mkdir(parents=True, exist_ok=True)
    db = base / "state.db"
    releases = base / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    svc = DataService(db, MATERIAL_DOMAINS)
    pub = MicrodataPublisher(svc, MATERIAL_DOMAINS, releases)
    return svc, pub, base


def _try(fn, *args, **kwargs) -> dict[str, Any]:
    try:
        return {"ok": True, "result": fn(*args, **kwargs)}
    except ServiceError as exc:
        return {"ok": False, "error": exc.code, "message": exc.message,
                "details": exc.details}


def _bootstrap(svc: DataService, material, *, purpose=None, grants=None,
               revoke="__default__") -> int:
    return svc.initialise(
        material.rows,
        purpose=purpose or material.purpose,
        grants=grants if grants is not None else list(material.initial_grants),
        revoke=(material.revoke if revoke == "__default__" else revoke),
    )["last_seq"]


def scenario_unique_groups(material) -> dict[str, Any]:
    svc, pub, _ = _fresh("01_unique_groups")
    # s1 = (Y,R,J,x), s4 = (O,R,J,y): two raw triples, each unique.
    head = _bootstrap(svc, material, grants=["s1", "s4"], revoke=None)
    view = svc.view(head, material.purpose)
    draft = pub.draft("unique-1", head, material.purpose, 2, 2, [1, 0, 0])
    return {
        "raw_rows": len(view),
        "raw_triples": sorted({(r.age, r.district, r.job) for r in view}),
        "raw_groups_all_singletons": len(
            {(r.age, r.district, r.job) for r in view}
        ) == len(view),
        "levels": draft["levels"],
        "loss": draft["loss"],
        "groups": draft["group_stats"],
        "note": "every raw triple is its own singleton group, so K=2 fails at "
                "level 0; age->* merges both rows into one feasible group",
    }


def scenario_k_ok_d_fails(material) -> dict[str, Any]:
    svc, pub, _ = _fresh("02_d_fails")
    # s1 and s3 both carry sensitive x: K=2 is satisfiable, D=2 never is.
    head = _bootstrap(svc, material, grants=["s1", "s3"], revoke=None)
    draft = pub.draft("dfail-1", head, material.purpose, 2, 2, [1, 0, 0])
    return {
        "state": draft["state"],
        "row_count": draft["row_count"],
        "levels": draft["levels"],
        "loss": draft["loss"],
        "group_at_top": draft.get("infeasible_report", {}).get("group_failures"),
        "infeasible": draft.get("infeasible_report"),
        "note": "the single group reaches K=2 rows but has only one distinct "
                "sensitive value, so D=2 fails at every level",
    }


def scenario_repeated_sensitive(material) -> dict[str, Any]:
    svc, pub, _ = _fresh("03_repeat_sensitive")
    head = _bootstrap(svc, material)
    draft = pub.draft("repeat-1", head, material.purpose, 3, 2, [0, 1, 0])
    return {
        "rows": draft["row_count"],
        "levels": draft["levels"],
        "groups": draft["group_stats"],
        "public_rows": draft["public_payload"],
        "note": "group count 3 >= K; distinct sensitive x,y = 2 >= D; the "
                "duplicate x is kept as two public rows",
    }


def scenario_weight_zero_tie(material) -> dict[str, Any]:
    svc, pub, _ = _fresh("04_weight_tie")
    head = _bootstrap(svc, material)
    draft = pub.draft("tie-1", head, material.purpose, 2, 2, [1, 0, 0])
    return {
        "weights": draft["weights"],
        "optimal_vectors": draft["optimal_vectors"],
        "chosen": draft["levels"],
        "note": "age must rise to 1; district/job cost 0 so all four vectors "
                "tie and the lexicographically smallest wins",
    }


def scenario_infeasible_single(material) -> dict[str, Any]:
    svc, pub, _ = _fresh("05_infeasible")
    # Only s1 granted: one row can never reach K=2.
    head = _bootstrap(svc, material, grants=["s1"], revoke=None)
    draft = pub.draft("inf-1", head, material.purpose, 2, 2, [1, 0, 0])
    review = _try(pub.review, draft["draft_id"], "approve", "cannot approve")
    return {
        "state": draft["state"],
        "row_count": draft["row_count"],
        "infeasible": draft.get("infeasible_report"),
        "approve_attempt": review,
    }


def scenario_empty(material) -> dict[str, Any]:
    svc, pub, _ = _fresh("06_empty")
    # k=0 is the empty prefix; no grants exist yet.
    draft = pub.draft("empty-1", 0, material.purpose, 2, 2, [1, 0, 0])
    pub.review(draft["draft_id"], "approve", "empty release permitted")
    released = pub.release(draft["draft_id"], "rel-empty")
    svc.initialise(material.rows, purpose=material.purpose,
                   grants=[], revoke=None)
    # No subject is authorised -> the current slice is also empty.
    head = svc.last_seq()
    draft2 = pub.draft("empty-2", head, material.purpose, 2, 2, [1, 0, 0])
    return {
        "prefix_zero": {
            "state": draft["state"],
            "levels": draft["levels"],
            "loss": draft["loss"],
            "row_count": draft["row_count"],
            "public": draft["public_payload"],
            "released_rows": json.loads(
                Path(released["public_path"]).read_text(encoding="utf-8")
            )["rows"],
        },
        "no_consent": {
            "state": draft2["state"],
            "levels": draft2["levels"],
            "loss": draft2["loss"],
            "row_count": draft2["row_count"],
        },
    }


def scenario_consent_change(material) -> dict[str, Any]:
    svc, pub, base = _fresh("07_consent_change")
    head = _bootstrap(svc, material)  # 4 rows, s2 revoked -> 3 authorised
    old_k = head - 1  # prefix before the revoke: all four authorised
    old = pub.draft("old-k", old_k, material.purpose, 2, 2, [1, 0, 0])
    pub.review(old["draft_id"], "approve", "historical slice")
    refused = _try(pub.release, old["draft_id"], "rel-old")
    current = pub.draft("new-k", head, material.purpose, 2, 2, [1, 0, 0])
    approved = pub.review(current["draft_id"], "approve", "current slice")
    released = _try(pub.release, current["draft_id"], "rel-current")
    # The historical slice is still exactly reproducible for private audit.
    audit = pub.inspect_draft(old["draft_id"])
    # A *new* draft is feasible at the old k too, and byte-identical.
    repeat = pub.draft("old-k-again", old_k, material.purpose, 2, 2, [1, 0, 0])
    return {
        "old_k": old_k,
        "old_levels": old["levels"],
        "old_loss": old["loss"],
        "old_rows": old["row_count"],
        "old_release_after_change": refused,
        "new_k": head,
        "new_levels": current["levels"],
        "new_loss": current["loss"],
        "new_rows": current["row_count"],
        "new_release": released,
        "old_private_audit": {
            "still_releasable": audit["still_releasable"],
            "levels": audit["levels"],
            "rows": audit["public_payload"],
        },
        "old_k_redraft_matches": repeat["levels"] == old["levels"]
        and repeat["public_payload"] == old["public_payload"],
        "note": "old k is legal for private checks and reproduces the same "
                "release bytes, but release additionally revalidates current "
                "consent and refuses",
    }


def scenario_revision_retraction_order(material) -> dict[str, Any]:
    svc, pub, _ = _fresh("08_revision_order")
    head = _bootstrap(svc, material)
    before = {"head": head, "subjects": [r.subject_id for r in svc.view(head, material.purpose)]}

    # Retraction does not revoke consent; it removes the row from views.
    svc.put("rec-0003", "s3", 1, None, op_id="retract-s3")
    after_retract = svc.last_seq()
    view_after = [r.subject_id for r in svc.view(after_retract, material.purpose)]

    # Re-granting s2 does not resurrect the retracted s3.
    svc.grant("s2", material.purpose, op_id="grant-s2-again")
    after_grant = svc.last_seq()
    view_after_grant = [r.subject_id for r in svc.view(after_grant, material.purpose)]

    # Out-of-order low revision is refused and does not move the head.  s3's
    # live revision is 1 (the retraction).  An *exact* re-submission of an
    # accepted operation is a replay (tested below), so demonstrate staleness on
    # a fresh record whose live revision jumps to 5 and then receives rev 2.
    row = {"age": "Y", "district": "R", "job": "J", "sensitive": "x"}
    _try(
        svc.put,
        "rec-9001",
        "s9",
        5,
        {"age": "O", "district": "R", "job": "J", "sensitive": "y"},
        op_id="late-base-rev5",
    )
    head_before_low = svc.last_seq()
    low = _try(svc.put, "rec-9001", "s9", 2, row, op_id="late-rev2")
    head_after_low = svc.last_seq()

    # A same-content replay is idempotent.
    replay = svc.put("rec-0003", "s3", 1, None)

    # Human rejection leaves data untouched.
    draft = pub.draft("reject-me", after_grant, material.purpose, 2, 2, [1, 0, 0])
    rejected = _try(pub.review, draft["draft_id"], "reject", "policy review")
    view_after_reject = [r.subject_id for r in svc.view(svc.last_seq(), material.purpose)]

    # Approve then retract: the draft cannot release.
    other = pub.draft("approve-then-retract", after_grant, material.purpose, 2, 2, [1, 0, 0])
    pub.review(other["draft_id"], "approve", "looks good")
    svc.put("rec-0001", "s1", 1,
            {"age": "Y", "district": "R", "job": "J", "sensitive": "x"},
            op_id="revision-same-values")
    after_revision = svc.last_seq()
    blocked = _try(pub.release, other["draft_id"], "rel-blocked")
    return {
        "before": before,
        "after_retract": {"head": after_retract, "subjects": view_after},
        "after_regrant": {"head": after_grant, "subjects": view_after_grant},
        "stale_low_revision": low,
        "head_before_low": head_before_low,
        "head_after_low": head_after_low,
        "replay": replay["status"],
        "reject": rejected,
        "subjects_after_reject": view_after_reject,
        "same_value_revision": {"head": after_revision, "release": blocked},
        "note": "revision with identical published values still changes the "
                "frozen identity set, so release is refused",
    }


def scenario_race(material) -> dict[str, Any]:
    results = {}
    for winner in ("revoke", "release"):
        name = f"09_race_{winner}"
        svc, pub, base = _fresh(name)
        head = _bootstrap(svc, material)
        draft = pub.draft(f"race-{winner}", head, material.purpose, 2, 2, [1, 0, 0])
        pub.review(draft["draft_id"], "approve", "race test")
        db_path = str(base / "state.db")
        releases = str(base / "releases")

        import threading

        release_box: dict[str, Any] = {}
        revoke_box: dict[str, Any] = {}

        def do_release() -> None:
            conn = DataService(db_path, MATERIAL_DOMAINS)
            try:
                p = MicrodataPublisher(conn, MATERIAL_DOMAINS, releases)
                try:
                    release_box["result"] = p.release(draft["draft_id"], f"rel-{winner}")
                except ServiceError as exc:
                    release_box["error"] = exc.code
            finally:
                conn.close()

        def do_revoke() -> None:
            conn = DataService(db_path, MATERIAL_DOMAINS)
            try:
                try:
                    revoke_box["result"] = conn.revoke("s1", material.purpose)
                except ServiceError as exc:
                    revoke_box["error"] = exc.code
            finally:
                conn.close()

        first_done = threading.Event()
        if winner == "revoke":
            first = threading.Thread(target=lambda: (do_revoke(), first_done.set()))
            second = threading.Thread(target=lambda: (first_done.wait(), do_release()))
        else:
            first = threading.Thread(target=lambda: (do_release(), first_done.set()))
            second = threading.Thread(target=lambda: (first_done.wait(), do_revoke()))
        first.start()
        second.start()
        first.join()
        second.join()

        released = "result" in release_box
        svc2 = DataService(db_path, MATERIAL_DOMAINS)
        try:
            stored = svc2.connection.execute(
                "SELECT release_id FROM releases WHERE draft_id=?",
                (draft["draft_id"],),
            ).fetchone()
            public_ok = None
            if released:
                public_ok = Path(release_box["result"]["public_path"]).exists()
        finally:
            svc2.close()
        results[winner] = {
            "release": release_box,
            "revoke": revoke_box,
            "persisted_release": None if stored is None else stored["release_id"],
            "public_file_exists": public_ok,
            "note": (
                "revoke committed first: release transaction sees missing "
                "consent and refuses; no release row"
                if winner == "revoke"
                else "release committed first: its immutable file stays even "
                "though the later revoke changes the live slice"
            ),
        }
    return results


def scenario_reopen(material) -> dict[str, Any]:
    svc, pub, base = _fresh("10_reopen")
    head = _bootstrap(svc, material)
    draft = pub.draft("reopen-1", head, material.purpose, 2, 2, [1, 0, 0])
    pub.review(draft["draft_id"], "approve", "before restart")
    released = pub.release(draft["draft_id"], "rel-reopen")
    svc.close()

    svc2 = DataService(base / "state.db", MATERIAL_DOMAINS)
    pub2 = MicrodataPublisher(svc2, MATERIAL_DOMAINS, base / "releases")
    try:
        drafts = pub2.list_drafts()
        releases = pub2.list_releases()
        audit = pub2.inspect_draft(draft["draft_id"])
        release = pub2.get_release("rel-reopen")
        ledger = pub2.get_private_file("rel-reopen")
        public_text = Path(released["public_path"]).read_text(encoding="utf-8")
        return {
            "service_head": svc2.view_head(),
            "draft_states": [d["state"] for d in drafts],
            "release_ids": [r["release_id"] for r in releases],
            "release_payload_ok": release["public_sha256"] == released["public_sha256"],
            "audit_still_releasable": audit["still_releasable"],
            "ledger_rows": len(ledger["frozen_slice"]["rows"]),
            "public_file_sha_matches": __import__("hashlib").sha256(
                public_text.encode()
            ).hexdigest() == released["public_sha256"],
        }
    finally:
        svc2.close()


SCENARIOS = {
    "unique_groups": scenario_unique_groups,
    "k_ok_d_fails": scenario_k_ok_d_fails,
    "repeated_sensitive": scenario_repeated_sensitive,
    "weight_zero_tie": scenario_weight_zero_tie,
    "infeasible_single": scenario_infeasible_single,
    "empty": scenario_empty,
    "consent_change": scenario_consent_change,
    "revision_retraction_order": scenario_revision_retraction_order,
    "race": scenario_race,
    "reopen": scenario_reopen,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", nargs="*", default=None)
    args = parser.parse_args(argv)

    global MATERIAL_DOMAINS
    material = load_material(MATERIAL)
    MATERIAL_DOMAINS = material.domains

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    summary = {}
    for name, fn in SCENARIOS.items():
        if args.only and name not in args.only:
            continue
        try:
            summary[name] = fn(material)
        except Exception as exc:  # noqa: BLE001 - keep other scenarios running
            summary[name] = {"error": type(exc).__name__, "message": str(exc)}
    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    return 0


MATERIAL_DOMAINS = None  # populated in main()


if __name__ == "__main__":
    sys.exit(main())
