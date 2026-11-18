"""Test suite (stdlib ``unittest``; no third-party dependency).

Run with::

    PYTHONPATH=. python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from semantic_microdata import (  # noqa: E402
    DataService,
    Domains,
    Hierarchy,
    MicrodataPublisher,
    ServiceError,
    domains_from_material,
    load_material,
    optimal_vectors,
    select_best,
)
from semantic_microdata.errors import (  # noqa: E402
    ConsentMissing,
    DraftConflict,
    DraftNotApproved,
    IdenticalRecordConflict,
    Infeasible,
    InvalidArgument,
    StaleDraft,
    StaleRevision,
    AuthorizationConflict,
)

MATERIAL = ROOT.parent / "case.json"


def two_level_domains() -> Domains:
    return Domains(
        hierarchies={
            "age": Hierarchy("age", {"Y": ("Y", "*"), "O": ("O", "*")}, 1),
            "district": Hierarchy("district", {"R": ("R", "*")}, 1),
            "job": Hierarchy("job", {"J": ("J", "*")}, 1),
        },
        sensitive_values=("x", "y", "z"),
    )


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        if MATERIAL.exists():
            self.material = load_material(MATERIAL)
            self.domains = self.material.domains
        else:  # pragma: no cover - material is part of the task
            self.material = None
            self.domains = two_level_domains()

    def service(self, name: str = "s.db", domains: Domains | None = None) -> DataService:
        return DataService(self.base / name, domains or self.domains)

    def publisher(self, svc: DataService) -> MicrodataPublisher:
        return MicrodataPublisher(svc, svc.domains, self.base / "releases")


def row(age="Y", district="R", job="J", sensitive="x") -> dict[str, str]:
    return {"age": age, "district": district, "job": job, "sensitive": sensitive}


# --------------------------------------------------------------------------- #
# Service semantics
# --------------------------------------------------------------------------- #
class ServiceTests(Base):
    def test_revision_wins_and_stale_is_refused(self) -> None:
        svc = self.service()
        self.addCleanup(svc.close)
        svc.put("r1", "s1", 1, row())
        svc.put("r1", "s1", 3, row(sensitive="y"))
        svc.grant("s1", "p")
        view = svc.view(svc.last_seq(), "p")
        self.assertEqual(len(view), 1)
        self.assertEqual(view[0].sensitive, "y")
        self.assertEqual(view[0].revision, 3)
        # Revision 2 arrives after revision 3 was committed: it must not
        # resurrect the older row.
        with self.assertRaises(StaleRevision):
            svc.put("r1", "s1", 2, row(sensitive="x"))
        # An exact duplicate of an accepted revision with differing content is
        # a distinct failure from staleness.
        with self.assertRaises(IdenticalRecordConflict):
            svc.put("r1", "s1", 1, row(sensitive="y"))  # rev 1 held sensitive x
        self.assertEqual(svc.view(svc.last_seq(), "p")[0].revision, 3)

    def test_identical_replay_and_conflict(self) -> None:
        svc = self.service()
        self.addCleanup(svc.close)
        first = svc.put("r1", "s1", 0, row())
        replay = svc.put("r1", "s1", 0, row())
        self.assertEqual(first["status"], "accepted")
        self.assertEqual(replay["status"], "replay")
        self.assertEqual(first["commit_seq"], replay["commit_seq"])
        with self.assertRaises(IdenticalRecordConflict):
            svc.put("r1", "s1", 0, row(sensitive="y"))
        self.assertEqual(svc.last_seq(), 1)

    def test_retraction_keeps_original_and_no_resurrection(self) -> None:
        svc = self.service()
        self.addCleanup(svc.close)
        svc.put("r1", "s1", 0, row())
        svc.grant("s1", "p")
        before = svc.view(svc.last_seq(), "p")
        self.assertEqual([r.subject_id for r in before], ["s1"])
        svc.put("r1", "s1", 1, None)
        after = svc.view(svc.last_seq(), "p")
        self.assertEqual(after, [])
        # Historical prefix still shows the original.
        historical = svc.view(2, "p")
        self.assertEqual([r.subject_id for r in historical], ["s1"])
        # Grant does not resurrect.
        svc.grant("s1", "p")
        self.assertEqual(svc.view(svc.last_seq(), "p"), [])
        # Raw original still present in the version log.
        row_count = svc.connection.execute(
            "SELECT COUNT(*) FROM operations WHERE kind='data'"
        ).fetchone()[0]
        self.assertEqual(row_count, 2)

    def test_auth_prefix_and_replay(self) -> None:
        svc = self.service()
        self.addCleanup(svc.close)
        svc.put("r1", "s1", 0, row())
        svc.grant("s1", "p", op_id="g1")
        svc.revoke("s1", "p", op_id="r1")
        self.assertEqual(svc.view(1, "p"), [])
        self.assertEqual([r.subject_id for r in svc.view(2, "p")], ["s1"])
        self.assertEqual(svc.view(3, "p"), [])
        replay = svc.revoke("s1", "p", op_id="r1")
        self.assertEqual(replay["status"], "replay")
        self.assertEqual(svc.last_seq(), 3)
        with self.assertRaises(AuthorizationConflict):
            svc.revoke("s1", "p", op_id="g1")

    def test_rejected_operation_consumes_no_sequence(self) -> None:
        svc = self.service()
        self.addCleanup(svc.close)
        svc.put("r1", "s1", 0, row())
        head = svc.last_seq()
        with self.assertRaises(InvalidArgument):
            svc.put("", "s1", 1, row())
        with self.assertRaises(Exception):
            svc.put("r1", "s2", 1, row())
        with self.assertRaises(Exception):
            svc.put("r1", "s1", 1, row(age="ZZZ"))
        self.assertEqual(svc.last_seq(), head)

    def test_subject_and_record_binding(self) -> None:
        svc = self.service()
        self.addCleanup(svc.close)
        svc.put("r1", "s1", 0, row())
        with self.assertRaises(ServiceError):
            svc.put("r1", "s2", 1, row())
        with self.assertRaises(ServiceError):
            svc.put("r2", "s1", 1, row())

    def test_ordering_of_put_and_grant(self) -> None:
        svc = self.service()
        self.addCleanup(svc.close)
        svc.grant("s1", "p")            # seq 1
        svc.put("r1", "s1", 0, row())   # seq 2
        svc.revoke("s1", "p")           # seq 3
        svc.put("r1", "s1", 1, row(sensitive="y"))  # seq 4
        self.assertEqual(svc.view(1, "p"), [])
        self.assertEqual([r.subject_id for r in svc.view(2, "p")], ["s1"])
        self.assertEqual(svc.view(3, "p"), [])
        self.assertEqual(svc.view(4, "p"), [])


# --------------------------------------------------------------------------- #
# Optimiser / grouping contract
# --------------------------------------------------------------------------- #
class OptimizerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.domains = two_level_domains()

    def test_empty_is_zero(self) -> None:
        sel = select_best([], self.domains, 2, 2, (1, 0, 0))
        self.assertEqual((sel.levels, sel.loss), ((0, 0, 0), 0))

    def test_minimum_loss_and_lexicographic_tie(self) -> None:
        rows = [row("Y", sensitive="x"), row("O", sensitive="y")]
        optima = optimal_vectors(rows, self.domains, 2, 2, (1, 0, 0))
        # Only age must rise; district/job are free -> four ties.
        self.assertEqual(
            optima,
            [
                ((1, 0, 0), 2),
                ((1, 0, 1), 2),
                ((1, 1, 0), 2),
                ((1, 1, 1), 2),
            ],
        )
        self.assertEqual(select_best(rows, self.domains, 2, 2, (1, 0, 0)).levels,
                         (1, 0, 0))

    def test_repeated_sensitive_counts_once_for_d_but_all_rows_for_k(self) -> None:
        rows = [row(sensitive="x"), row(sensitive="x"), row(sensitive="y")]
        best = select_best(rows, self.domains, 3, 2, (0, 1, 0))
        self.assertEqual(best.levels, (0, 0, 0))
        with self.assertRaises(Infeasible):
            select_best(rows, self.domains, 3, 3, (0, 1, 0))

    def test_k_pass_d_fail_is_infeasible(self) -> None:
        rows = [row(sensitive="x"), row(sensitive="x")]
        with self.assertRaises(Infeasible):
            select_best(rows, self.domains, 2, 2, (1, 0, 0))

    def test_weights_are_not_boolean(self) -> None:
        rows = [row("Y", sensitive="x"), row("O", sensitive="y")]
        with self.assertRaises(InvalidArgument):
            select_best(rows, self.domains, True, 2, (1, 0, 0))
        with self.assertRaises(InvalidArgument):
            select_best(rows, self.domains, 2, True, (1, 0, 0))
        with self.assertRaises(InvalidArgument):
            select_best(rows, self.domains, 2, 3, (1, 0, 0))  # D > K
        with self.assertRaises(InvalidArgument):
            select_best(rows, self.domains, 2, 2, [1, 0])  # wrong length

    def test_loss_formula(self) -> None:
        from semantic_microdata.optimizer import loss_of

        self.assertEqual(loss_of((1, 2, 3), 5, (2, 0, 1)), (1 * 2 + 2 * 0 + 3 * 1) * 5)
        self.assertEqual(loss_of((1, 1, 1), 0, (1, 1, 1)), 0)


# --------------------------------------------------------------------------- #
# Publisher
# --------------------------------------------------------------------------- #
class PublisherTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.svc = self.service()
        self.addCleanup(self.svc.close)
        self.material = self.material or load_material(MATERIAL)
        self.svc.initialise(
            self.material.rows,
            purpose=self.material.purpose,
            grants=list(self.material.initial_grants),
            revoke=self.material.revoke,
        )
        self.pub = self.publisher(self.svc)

    def test_draft_and_release_lifecycle(self) -> None:
        head = self.svc.last_seq()
        draft = self.pub.draft("q1", head, "research", 2, 2, [1, 0, 0])
        self.assertEqual(draft["levels"], [1, 0, 0])
        self.assertEqual(draft["row_count"], 3)
        with self.assertRaises(DraftNotApproved):
            self.pub.release(draft["draft_id"], "rel-0")
        approved = self.pub.review(draft["draft_id"], "approve", "ok")
        self.assertEqual(approved["state"], "approved")
        release = self.pub.release(draft["draft_id"], "rel-1")
        # release_seq is the position in the global journal (after 9 service
        # operations, the draft and the review): 12.
        self.assertEqual(release["release_seq"], 12)
        public = json.loads(Path(release["public_path"]).read_text())
        self.assertEqual(len(public["rows"]), 3)
        self.assertNotIn("subject_id", public["rows"][0])
        self.assertEqual(public["rows"], sorted(
            public["rows"],
            key=lambda r: (r["age"], r["district"], r["job"], r["sensitive"]),
        ))

    def test_request_replay_and_conflict(self) -> None:
        head = self.svc.last_seq()
        first = self.pub.draft("q1", head, "research", 2, 2, [1, 0, 0])
        again = self.pub.draft("q1", head, "research", 2, 2, [1, 0, 0])
        self.assertEqual(first["draft_id"], again["draft_id"])
        with self.assertRaises(DraftConflict):
            self.pub.draft("q1", head, "research", 3, 2, [1, 0, 0])

    def test_infeasible_draft_cannot_be_approved(self) -> None:
        draft = self.pub.draft("q-inf", self.svc.last_seq(), "research", 4, 2,
                               [1, 0, 0])
        self.assertEqual(draft["state"], "infeasible")
        self.assertIn("group_failures", draft["infeasible_report"])
        with self.assertRaises(Infeasible):
            self.pub.review(draft["draft_id"], "approve", "no")
        self.assertEqual(draft["row_count"], 3)

    def test_empty_slice(self) -> None:
        # Withdraw every authorisation so the *current* slice is empty too; an
        # empty draft frozen at an older non-empty prefix is (correctly) stale.
        for subject in ("s1", "s3", "s4"):
            self.svc.revoke(subject, "research")
        draft = self.pub.draft("q-empty", self.svc.last_seq(), "research", 2, 2,
                               [1, 0, 0])
        self.assertEqual((draft["levels"], draft["loss"], draft["row_count"]),
                         ([0, 0, 0], 0, 0))
        self.pub.review(draft["draft_id"], "approve", "empty")
        release = self.pub.release(draft["draft_id"], "rel-empty")
        public = json.loads(Path(release["public_path"]).read_text())
        self.assertEqual(public["rows"], [])

    def test_empty_prefix_draft_is_historical(self) -> None:
        draft = self.pub.draft("q-prefix0", 0, "research", 2, 2, [1, 0, 0])
        self.assertEqual((draft["levels"], draft["loss"], draft["row_count"]),
                         ([0, 0, 0], 0, 0))

    def test_stale_draft_after_consent_change(self) -> None:
        head = self.svc.last_seq()
        draft = self.pub.draft("q-stale", head, "research", 2, 2, [1, 0, 0])
        self.pub.review(draft["draft_id"], "approve", "ok")
        self.svc.revoke("s4", "research")
        with self.assertRaises(ServiceError):
            self.pub.release(draft["draft_id"], "rel-stale")
        self.assertFalse(self.pub.inspect_draft(draft["draft_id"])["still_releasable"])

    def test_stale_draft_after_same_value_revision(self) -> None:
        head = self.svc.last_seq()
        draft = self.pub.draft("q-same", head, "research", 2, 2, [1, 0, 0])
        self.pub.review(draft["draft_id"], "approve", "ok")
        # Identical published values, higher revision -> identity set changed.
        self.svc.put("rec-0001", "s1", 1, row("Y", "R", "J", "x"))
        with self.assertRaises(StaleDraft):
            self.pub.release(draft["draft_id"], "rel-same")

    def test_regrant_restores_frozen_slice(self) -> None:
        head = self.svc.last_seq()
        draft = self.pub.draft("q-regrant", head, "research", 2, 2, [1, 0, 0])
        self.pub.review(draft["draft_id"], "approve", "ok")
        self.svc.revoke("s1", "research")
        with self.assertRaises(ConsentMissing):
            self.pub.release(draft["draft_id"], "rel-x")
        self.svc.grant("s1", "research")
        # Frozen identity set is restored exactly (no revision changed).
        release = self.pub.release(draft["draft_id"], "rel-ok")
        self.assertEqual(release["status"], "released")

    def test_release_replay(self) -> None:
        draft = self.pub.draft("q-r", self.svc.last_seq(), "research", 2, 2,
                               [1, 0, 0])
        self.pub.review(draft["draft_id"], "approve", "ok")
        first = self.pub.release(draft["draft_id"], "rel-r")
        second = self.pub.release(draft["draft_id"], "rel-r")
        self.assertEqual(first, second)

    def test_old_release_file_immutable(self) -> None:
        draft = self.pub.draft("q-imm", self.svc.last_seq(), "research", 2, 2,
                               [1, 0, 0])
        self.pub.review(draft["draft_id"], "approve", "ok")
        release = self.pub.release(draft["draft_id"], "rel-imm")
        text_before = Path(release["public_path"]).read_text()
        self.svc.revoke("s4", "research")
        self.assertEqual(Path(release["public_path"]).read_text(), text_before)

    def test_reopen_preserves_state(self) -> None:
        draft = self.pub.draft("q-reopen", self.svc.last_seq(), "research", 2, 2,
                               [1, 0, 0])
        self.pub.review(draft["draft_id"], "approve", "ok")
        release = self.pub.release(draft["draft_id"], "rel-reopen")
        self.svc.close()
        svc2 = self.service()
        self.addCleanup(svc2.close)
        pub2 = self.publisher(svc2)
        self.assertEqual([d["state"] for d in pub2.list_drafts()], ["approved"])
        self.assertEqual([r["release_id"] for r in pub2.list_releases()],
                         ["rel-reopen"])
        release2 = pub2.get_release("rel-reopen")
        self.assertEqual(release2["public_sha256"], release["public_sha256"])
        self.assertTrue(
            Path(release["public_path"]).read_text().endswith("\n")
        )


class VerifierTests(Base):
    def test_independent_verifier_accepts_good_release(self) -> None:
        from verify.reference_check import check

        svc = self.service()
        self.addCleanup(svc.close)
        material = self.material or load_material(MATERIAL)
        svc.initialise(
            material.rows,
            purpose=material.purpose,
            grants=list(material.initial_grants),
            revoke=material.revoke,
        )
        pub = self.publisher(svc)
        draft = pub.draft("v1", svc.last_seq(), material.purpose, 2, 2, [1, 0, 0])
        pub.review(draft["draft_id"], "approve", "ok")
        pub.release(draft["draft_id"], "rel-v")
        report = check(MATERIAL, self.base / "s.db", "rel-v")
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual(report["stored_levels"], [1, 0, 0])


class BoundTests(Base):
    """Exercise the stated input family bounds (60 rows, 3 levels/column)."""

    def three_level_domains(self) -> Domains:
        return Domains(
            hierarchies={
                "age": Hierarchy(
                    "age",
                    {
                        "A1": ("A1", "young", "*"),
                        "A2": ("A2", "young", "*"),
                        "A3": ("A3", "old", "*"),
                        "A4": ("A4", "old", "*"),
                    },
                    2,
                ),
                "district": Hierarchy(
                    "district",
                    {
                        "D1": ("D1", "north", "*"),
                        "D2": ("D2", "north", "*"),
                        "D3": ("D3", "south", "*"),
                        "D4": ("D4", "south", "*"),
                    },
                    2,
                ),
                "job": Hierarchy(
                    "job",
                    {
                        "J1": ("J1", "tech", "*"),
                        "J2": ("J2", "tech", "*"),
                        "J3": ("J3", "ops", "*"),
                        "J4": ("J4", "ops", "*"),
                    },
                    2,
                ),
            },
            sensitive_values=tuple(f"s{i}" for i in range(1, 9)),
        )

    def test_sixty_rows_three_levels_four_purposes(self) -> None:
        domains = self.three_level_domains()
        svc = self.service("bound.db", domains)
        self.addCleanup(svc.close)
        ages = ["A1", "A2", "A3", "A4"]
        districts = ["D1", "D2", "D3", "D4"]
        jobs = ["J1", "J2", "J3", "J4"]
        rows = []
        for index in range(60):
            rows.append(
                {
                    "subject_id": f"p{index:03d}",
                    "age": ages[index % 4],
                    "district": districts[(index // 4) % 4],
                    "job": jobs[(index // 16) % 4],
                    "sensitive": f"s{index % 8 + 1}",
                }
            )
        purposes = ["research", "statistics", "audit", "planning"]
        svc.initialise(rows, purpose="research", grants=[], revoke=None)
        # 60 data ops + 60 grants on the main purpose + 60 on a second
        # purpose = 180 accepted operations, within the 200 bound.
        for subject_row in rows:
            svc.grant(subject_row["subject_id"], "research")
        for subject_row in rows:
            svc.grant(subject_row["subject_id"], "statistics")
        # 120 auth ops + 60 data ops = 180 accepted operations.
        self.assertLessEqual(svc.last_seq(), 200)
        self.assertEqual(len(svc.view(svc.last_seq(), "research")), 60)
        pub = self.publisher(svc)
        draft = pub.draft("bound-1", svc.last_seq(), "research", 5, 4,
                          [1, 1, 1])
        self.assertEqual(draft["row_count"], 60)
        self.assertIsNotNone(draft["levels"])
        # 3 levels per column -> 3^3 = 27 vectors, all enumerated.  (The tie
        # list is whatever the contract yields; assert at least the chosen one.)
        self.assertGreaterEqual(len(draft["optimal_vectors"]), 1)
        self.assertEqual(draft["optimal_vectors"][0][0], draft["levels"])
        pub.review(draft["draft_id"], "approve", "bounds")
        release = pub.release(draft["draft_id"], "rel-bound")
        public = json.loads(Path(release["public_path"]).read_text())
        self.assertEqual(len(public["rows"]), 60)

    def test_hierarchy_rejects_group_splitting(self) -> None:
        # Two leaves share level-1 "g" but disagree on level 2 -> a level-1
        # group would be split by the parent level.  Both chains reach "*".
        with self.assertRaises(InvalidArgument):
            Hierarchy(
                "age",
                {"A": ("A", "g", "p1", "*"), "B": ("B", "g", "p2", "*")},
                3,
            )
        # A well-formed chain forest is accepted.
        Hierarchy(
            "age",
            {"A": ("A", "g", "*"), "B": ("B", "g", "*")},
            2,
        )


class IsolationTests(Base):
    def test_other_purpose_changes_do_not_stale_a_draft(self) -> None:
        svc = self.service()
        self.addCleanup(svc.close)
        material = self.material or load_material(MATERIAL)
        svc.initialise(
            material.rows,
            purpose=material.purpose,
            grants=list(material.initial_grants),
            revoke=material.revoke,
        )
        pub = self.publisher(svc)
        draft = pub.draft("iso-1", svc.last_seq(), material.purpose, 2, 2,
                          [1, 0, 0])
        pub.review(draft["draft_id"], "approve", "ok")
        svc.grant("s1", "other")
        svc.revoke("s4", "other")
        release = pub.release(draft["draft_id"], "rel-iso")
        self.assertEqual(release["status"], "released")

    def test_release_id_cannot_be_reused_for_another_draft(self) -> None:
        svc = self.service()
        self.addCleanup(svc.close)
        material = self.material or load_material(MATERIAL)
        svc.initialise(
            material.rows,
            purpose=material.purpose,
            grants=list(material.initial_grants),
            revoke=material.revoke,
        )
        pub = self.publisher(svc)
        head = svc.last_seq()
        first = pub.draft("reuse-a", head, material.purpose, 2, 2, [1, 0, 0])
        pub.review(first["draft_id"], "approve", "ok")
        pub.release(first["draft_id"], "shared-id")
        second = pub.draft("reuse-b", head, material.purpose, 2, 2, [1, 0, 0])
        pub.review(second["draft_id"], "approve", "ok")
        from semantic_microdata.errors import ReleaseConflict

        with self.assertRaises(ReleaseConflict):
            pub.release(second["draft_id"], "shared-id")

    def test_k_beyond_head_is_refused(self) -> None:
        svc = self.service()
        self.addCleanup(svc.close)
        material = self.material or load_material(MATERIAL)
        svc.initialise(
            material.rows,
            purpose=material.purpose,
            grants=list(material.initial_grants),
            revoke=material.revoke,
        )
        pub = self.publisher(svc)
        with self.assertRaises(InvalidArgument):
            pub.draft("too-far", svc.last_seq() + 1, material.purpose, 2, 2,
                      [1, 0, 0])

    def test_consent_history_tracks_every_decision(self) -> None:
        svc = self.service()
        self.addCleanup(svc.close)
        svc.grant("s1", "p", op_id="g")
        svc.revoke("s1", "p", op_id="r")
        svc.grant("s1", "p", op_id="g2")
        history = svc.consent_history("s1", "p")
        self.assertEqual([h["action"] for h in history], ["grant", "revoke", "grant"])
        self.assertEqual([h["service_seq"] for h in history], [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
