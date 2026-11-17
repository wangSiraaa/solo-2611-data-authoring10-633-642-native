"""End-to-end tests across gradebook + seater persistence."""

import json
import os
import tempfile
import unittest

from examkit.gradebook import Gradebook, IdempotencyConflict, IngestRejected
from examkit.seater import ConfirmRejected, Seater, SeatingProtocolError
from examkit.solver import SeatingError
from examkit.store import connect


def m(aid, student, assessment, score, revision=1, void=False):
    return {"attempt_id": aid, "student": student,
            "assessment_id": assessment, "revision": revision,
            "score": score, "void": void}


def rules():
    return {
        "threshold": [3, 5],
        "categories": [
            {"id": "C", "weight": [1, 1], "assessments": [
                {"id": "m1", "max": 10, "mandatory": True},
                {"id": "n1", "max": 20, "mandatory": False},
                {"id": "n2", "max": 10, "mandatory": False},
            ]},
        ],
    }


def rooms_doc():
    return {"rooms": [
        {"id": "R1", "seats": [
            {"seat_id": "r11", "room": "R1", "row": 0, "col": 0,
             "available": True, "accessible": True, "aisle": True},
            {"seat_id": "r12", "room": "R1", "row": 0, "col": 1,
             "available": True, "accessible": False, "aisle": False},
            {"seat_id": "r13", "room": "R1", "row": 0, "col": 2,
             "available": True, "accessible": False, "aisle": True},
            {"seat_id": "r14", "room": "R1", "row": 1, "col": 0,
             "available": False, "accessible": False, "aisle": False},
        ]},
        {"id": "R2", "seats": [
            {"seat_id": "r21", "room": "R2", "row": 0, "col": 0,
             "available": True, "accessible": True, "aisle": False},
            {"seat_id": "r22", "room": "R2", "row": 0, "col": 1,
             "available": True, "accessible": False, "aisle": False},
        ]},
    ]}


class IntegrationBase(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.gb = Gradebook(connect(self.path), "COURSE1")
        self.se = Seater(connect(self.path))
        self.gb.put_rules("r1", rules())
        self.gb.set_students(["A", "B", "C"])
        self.se.put_rooms("rooms-1", "v1", rooms_doc())

    def tearDown(self):
        self.gb.conn.close()
        self.se.conn.close()
        os.unlink(self.path)

    def seed_grades(self):
        # A: m1 6/10, n1 4/20, n2 5/10. Non-mand ratios 1/5 vs 1/2 -> n1 dropped.
        # C1 = (3/5 + 1/2)/2 = 11/20 = 0.55 ; threshold 3/5=0.6 -> A NOT eligible
        self.gb.ingest("g1", [
            m("am", "A", "m1", 6), m("an1", "A", "n1", 4), m("an2", "A", "n2", 5),
            m("bm", "B", "m1", 8), m("bn1", "B", "n1", 16), m("bn2", "B", "n2", 8),
            m("cm", "C", "m1", 7), m("cn1", "C", "n1", 12),
        ])
        # B: ratios 4/5, 4/5, 4/5 -> 4/5 eligible.
        # C: m1 7/10, n1 3/5, n2 missing=0 -> drop n2(0); kept 7/10,3/5 -> 13/20=.65
        #    eligible, but only if mandatory m1 attended (yes).
        return self.gb.issue_product()

    def register_all(self):
        for i, st in enumerate(["A", "B", "C"]):
            self.se.register(f"reg-{st}", "E1", st)


class PlanAndEligibilityTests(IntegrationBase):
    def test_unqualified_listed_with_reasons_but_not_seated(self):
        product = self.seed_grades()
        self.register_all()
        self.se.set_current_product("E1", product["product_id"])
        plan = self.se.plan("E1", product["product_id"])
        self.assertEqual(plan["status"], "feasible")
        self.assertEqual(plan["qualified_registrants"], ["B", "C"])
        unq = {u["student"]: u for u in plan["unqualified_registrants"]}
        self.assertIn("A", unq)
        self.assertFalse(unq["A"]["seated"])
        self.assertTrue(any("below threshold" in r for r in unq["A"]["eligibility_reasons"]))
        # exactly one seat per qualified registrant, no seat for A
        self.assertEqual(sorted(plan["assignment"].keys()), ["B", "C"])
        self.assertEqual(len(set(plan["assignment"].values())), 2)
        # qualified but not registered -> not seated
        self.se.withdraw("wd-c", "E1", "C")
        plan2 = self.se.plan("E1", product["product_id"])
        self.assertEqual(plan2["qualified_registrants"], ["B"])

    def test_makeup_product_changes_roster_and_stale_eligibility_rejected(self):
        p1 = self.seed_grades()
        self.register_all()
        self.se.set_current_product("E1", p1["product_id"])
        plan1 = self.se.plan("E1", p1["product_id"])
        self.assertEqual(plan1["qualified_registrants"], ["B", "C"])
        # legal makeup for A on n2: 8/10 -> ratios n1 1/5 dropped, kept 3/5 & 4/5
        # category = 7/10 = 0.7 >= 3/5 -> A becomes eligible
        self.gb.ingest("g2", [m("an2-new", "A", "n2", 8)])
        p2 = self.gb.issue_product()
        self.assertNotEqual(p1["product_id"], p2["product_id"])
        self.se.set_current_product("E1", p2["product_id"])
        # caller that cached the OLD eligibility set gets an explicit error
        with self.assertRaises(SeatingProtocolError):
            self.se.plan("E1", p2["product_id"],
                         expected_eligibility=["B", "C"])
        plan2 = self.se.plan("E1", p2["product_id"])
        self.assertEqual(plan2["qualified_registrants"], ["A", "B", "C"])
        # old plan remains queryable and still names the old roster
        self.assertEqual(self.se.get_plan(plan1["plan_id"])["qualified_registrants"],
                         ["B", "C"])
        # old product is still a legal query
        self.assertEqual(self.gb.get_product(p1["product_id"])["product_id"],
                         p1["product_id"])

    def test_plan_must_read_real_stored_product(self):
        with self.assertRaises(SeatingProtocolError):
            self.se.plan("E1", "gp-does-not-exist")

    def test_empty_eligible_roster_uses_zero_rooms(self):
        # no grades at all -> no one eligible
        product = self.gb.issue_product()
        self.register_all()
        self.se.set_current_product("E1", product["product_id"])
        plan = self.se.plan("E1", product["product_id"])
        self.assertEqual(plan["room_count"], 0)
        self.assertEqual(plan["assignment"], {})
        self.assertEqual(plan["qualified_registrants"], [])
        self.assertEqual(plan["status"], "feasible")

    def test_infeasible_plan_stored_with_replayable_reason(self):
        product = self.seed_grades()
        self.register_all()
        # B and C both need the single accessible+aisle seat r11
        self.se.set_need("need-b", "E1", "B", accessible=True, aisle=True)
        self.se.set_need("need-c", "E1", "C", accessible=True, aisle=True)
        self.se.set_current_product("E1", product["product_id"])
        plan = self.se.plan("E1", product["product_id"])
        self.assertEqual(plan["status"], "infeasible")
        self.assertIsNotNone(plan["infeasible_reason"])
        # full inputs retained for replay
        self.assertEqual(len(plan["rooms_input"]), 2)
        with self.assertRaises(ConfirmRejected):
            self.se.confirm(plan["plan_id"], "req-infeasible")

    def test_conflicts_needs_and_room_facts_persist_and_validate(self):
        self.se.add_conflict("cf1", "E1", "A", "B")
        self.se.add_conflict("cf1", "E1", "A", "B")  # duplicate edge, no effect
        with self.assertRaises(SeatingError):
            self.se.add_conflict("cf-self", "E1", "A", "A")  # self loop illegal
        self.se.set_need("n1", "E1", "B", accessible=True)
        product = self.seed_grades()
        self.register_all()
        self.se.set_current_product("E1", product["product_id"])
        plan = self.se.plan("E1", product["product_id"])
        # B needs accessible: r11 or r21; A unqualified anyway
        by_id = {s["seat_id"]: s
                 for room in plan["rooms_input"] for s in room["seats"]}
        self.assertTrue(by_id[plan["assignment"]["B"]]["accessible"])
        # add a second edge and confirm it is carried into the next plan
        self.se.add_conflict("cf2", "E1", "B", "C")
        plan2 = self.se.plan("E1", product["product_id"])
        self.assertIn(["B", "C"], plan2["conflicts"])
        # and the seated B/C are not adjacent
        sb = by_id[plan2["assignment"]["B"]]
        sc = by_id[plan2["assignment"]["C"]]
        self.assertFalse(sb["room"] == sc["room"]
                         and abs(sb["row"] - sc["row"]) + abs(sb["col"] - sc["col"]) == 1)


class ConfirmationTests(IntegrationBase):
    def test_confirm_success_then_change_blocks_reconfirm_but_keeps_history(self):
        product = self.seed_grades()
        self.register_all()
        self.se.set_current_product("E1", product["product_id"])
        plan = self.se.plan("E1", product["product_id"])
        ok = self.se.confirm(plan["plan_id"], "req-1")
        self.assertEqual(ok["status"], "confirmed")
        # same request id, same payload replays the first result
        again = self.se.confirm(plan["plan_id"], "req-1")
        self.assertEqual(again, ok)
        # same request id, different payload -> rejected as protocol conflict
        with self.assertRaises(SeatingProtocolError):
            self.se.confirm(plan["plan_id"], "req-1", payload={"note": "changed"})
        # a registration change makes the old plan unconfirmable, but it stays
        # queryable and the prior confirmation remains immutable history
        self.se.register("reg-D", "E1", "D")
        with self.assertRaises(ConfirmRejected) as ctx:
            self.se.confirm(plan["plan_id"], "req-2")
        self.assertIn("head moved", ctx.exception.reason)
        old = self.se.get_plan(plan["plan_id"])
        self.assertEqual(old["plan_id"], plan["plan_id"])
        self.assertEqual(self.se.get_confirm("req-1")["status"], "confirmed")
        # new facts require a new plan, which can be confirmed
        plan2 = self.se.plan("E1", product["product_id"])
        self.assertNotEqual(plan2["plan_id"], plan["plan_id"])
        ok2 = self.se.confirm(plan2["plan_id"], "req-3")
        self.assertEqual(ok2["status"], "confirmed")

    def test_rejected_confirm_is_persisted_and_replays_identically(self):
        product = self.seed_grades()
        self.register_all()
        self.se.set_current_product("E1", product["product_id"])
        plan = self.se.plan("E1", product["product_id"])
        self.se.confirm(plan["plan_id"], "req-base")
        # change facts so the plan becomes stale
        self.se.register("reg-extra", "E1", "D")
        with self.assertRaises(ConfirmRejected) as ctx:
            self.se.confirm(plan["plan_id"], "req-stale")
        first = ctx.exception.doc
        # rejection record was committed, not rolled back
        stored = self.se.get_confirm("req-stale")
        self.assertEqual(stored, first)
        # identical replay returns the same rejected document (no exception now,
        # because the first result is immutable history); the doc still says
        # rejected with the original reason
        replay = self.se.confirm(plan["plan_id"], "req-stale")
        self.assertEqual(replay["status"], "rejected")
        self.assertEqual(replay["reason"], first["reason"])

    def test_product_switch_rejects_old_plan_confirm(self):
        p1 = self.seed_grades()
        self.register_all()
        self.se.set_current_product("E1", p1["product_id"])
        plan = self.se.plan("E1", p1["product_id"])
        self.se.confirm(plan["plan_id"], "req-old")
        # grade change -> new product, exam binding moves; old plan cannot confirm
        self.gb.ingest("g2", [m("an2-new", "A", "n2", 8)])
        p2 = self.gb.issue_product()
        self.se.set_current_product("E1", p2["product_id"])
        with self.assertRaises(ConfirmRejected) as ctx:
            self.se.confirm(plan["plan_id"], "req-after-product")
        self.assertIn("current product", ctx.exception.reason)

    def test_two_connections_serialized_by_sqlite_transaction(self):
        product = self.seed_grades()
        self.register_all()
        self.se.set_current_product("E1", product["product_id"])
        plan = self.se.plan("E1", product["product_id"])
        # second independent connection to the same file
        other = Seater(connect(self.path))
        # both read the same plan; other changes registration, then both confirm
        other.read_plan = other.get_plan(plan["plan_id"])
        self.se.get_plan(plan["plan_id"])
        other.register("late-reg", "E1", "D")  # commits first under BEGIN IMMEDIATE
        # the original connection observes the moved head and must reject
        with self.assertRaises(ConfirmRejected):
            self.se.confirm(plan["plan_id"], "req-tx-1")
        # replanning on the original connection sees the committed change
        plan2 = self.se.plan("E1", product["product_id"])
        self.assertGreater(plan2["registration_head"], plan["registration_head"])
        ok = self.se.confirm(plan2["plan_id"], "req-tx-2")
        self.assertEqual(ok["status"], "confirmed")
        other.conn.close()

    def test_reopen_preserves_everything(self):
        product = self.seed_grades()
        self.register_all()
        self.se.set_current_product("E1", product["product_id"])
        plan = self.se.plan("E1", product["product_id"])
        self.se.confirm(plan["plan_id"], "req-persist")
        plan_json = json.dumps(plan, sort_keys=True)
        self.gb.conn.close()
        self.se.conn.close()
        # normal close + reopen: new facade objects, same file
        gb2 = Gradebook(connect(self.path), "COURSE1")
        se2 = Seater(connect(self.path))
        self.assertEqual(gb2.latest_seq(), 1)
        self.assertEqual(gb2.get_product(product["product_id"])["product_id"],
                         product["product_id"])
        self.assertEqual(json.dumps(se2.get_plan(plan["plan_id"]), sort_keys=True),
                         plan_json)
        self.assertEqual(se2.get_confirm("req-persist")["status"], "confirmed")
        gb2.conn.close()
        se2.conn.close()


class IdempotencyTests(IntegrationBase):
    def test_gradebook_event_replay_rules(self):
        batch = [m("am", "A", "m1", 6)]
        r1 = self.gb.ingest("e-x", batch)
        self.assertFalse(r1.replayed)
        r2 = self.gb.ingest("e-x", batch)
        self.assertTrue(r2.replayed)
        self.assertEqual(r1.seq, r2.seq)
        with self.assertRaises(IdempotencyConflict):
            self.gb.ingest("e-x", [m("am", "A", "m1", 7)])

    def test_seater_op_replay_rules(self):
        s1 = self.se.register("op-x", "E1", "A")
        s2 = self.se.register("op-x", "E1", "A")
        self.assertEqual(s1, s2)
        with self.assertRaises(SeatingProtocolError):
            self.se.withdraw("op-x", "E1", "A")  # same id, different document

    def test_ingest_rejection_is_atomic(self):
        before = self.gb.latest_seq()
        with self.assertRaises(IngestRejected):
            self.gb.ingest("bad", [m("ok", "A", "m1", 5),
                                   m("bad-score", "A", "m1", 99)])
        self.assertEqual(self.gb.latest_seq(), before)


if __name__ == "__main__":
    unittest.main()
