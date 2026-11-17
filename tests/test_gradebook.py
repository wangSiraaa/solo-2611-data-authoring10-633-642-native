"""Gradebook tests.

Every expected fraction below is hand-derived with integer cross
multiplication; the tests never call the production scoring code to produce
the expected numbers.
"""

import os
import tempfile
import unittest

from examkit.gradebook import (
    Gradebook, IdempotencyConflict, IngestRejected, RuleError,
)
from examkit.rational import Rat
from examkit.store import connect


def make_rules():
    # Two categories, weights 1/3 and 2/3 (sum exactly 1).
    return {
        "threshold": [1, 2],
        "categories": [
            {"id": "C1", "weight": [1, 3], "assessments": [
                {"id": "m1", "max": 10, "mandatory": True},
                {"id": "n1", "max": 20, "mandatory": False},
                {"id": "n2", "max": 10, "mandatory": False},
            ]},
            {"id": "C2", "weight": [2, 3], "assessments": [
                {"id": "m2", "max": 5, "mandatory": True},
                {"id": "n3", "max": 2, "mandatory": False},
            ]},
        ],
    }


STUDENTS = ["A", "B", "C"]


def new_book():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = connect(path)
    book = Gradebook(conn, "COURSE1")
    book.put_rules("r1", make_rules())
    book.set_students(STUDENTS)
    return book, path


def mirror(aid, student, assessment, score, revision=1, void=False):
    return {"attempt_id": aid, "student": student, "assessment_id": assessment,
            "revision": revision, "score": score, "void": void}


class RationalTests(unittest.TestCase):
    def test_reduction_and_cross_multiply(self):
        self.assertEqual(Rat(2, 4).to_pair(), [1, 2])
        self.assertEqual(Rat(-3, -6).to_pair(), [1, 2])
        # 5/20 == 1/4 and 2/8 == 1/4 across different denominators
        self.assertEqual(Rat(5, 20), Rat(2, 8))
        self.assertTrue(Rat(3, 10) < Rat(7, 20))   # 60 < 70 by cross multiply
        self.assertTrue(Rat(7, 20) > Rat(3, 10))
        self.assertEqual((Rat(1, 3) + Rat(2, 3)).to_pair(), [1, 1])
        self.assertEqual((Rat(1, 3) * Rat(3, 4)).to_pair(), [1, 4])
        with self.assertRaises(ZeroDivisionError):
            Rat(1, 0)

    def test_rule_weight_validation(self):
        from examkit.gradebook import validate_rules
        rules = make_rules()
        validate_rules(rules)  # 1/3 + 2/3 == 1 exactly
        bad = make_rules()
        bad["categories"][1]["weight"] = [3, 4]  # sums to 13/12
        with self.assertRaises(RuleError):
            validate_rules(bad)
        bad2 = make_rules()
        bad2["categories"][0]["assessments"][0]["max"] = 0
        with self.assertRaises(RuleError):
            validate_rules(bad2)
        bad3 = make_rules()
        bad3["categories"][0]["assessments"][0]["mandatory"] = "yes"
        with self.assertRaises(RuleError):
            validate_rules(bad3)


class GradingTests(unittest.TestCase):
    def setUp(self):
        self.book, self.path = new_book()

    def tearDown(self):
        self.book.conn.close()
        os.unlink(self.path)

    def ingest(self, event_id, mirrors):
        return self.book.ingest(event_id, mirrors)

    def student(self, product, sid):
        return next(s for s in product["students"] if s["student"] == sid)

    def assess(self, sd, aid):
        return next(a for a in sd["assessments"] if a["assessment_id"] == aid)

    def test_different_max_proportions_not_raw_sum(self):
        # A: m1 = 8/10, n1 = 4/20 (raw 4 < 8 but ratio 1/5 vs 4/5),
        #    n2 = 5/10, m2 = 5/5, n3 = 1/2
        self.ingest("e1", [
            mirror("a-m1", "A", "m1", 8),
            mirror("a-n1", "A", "n1", 4),
            mirror("a-n2", "A", "n2", 5),
            mirror("a-m2", "A", "m2", 5),
            mirror("a-n3", "A", "n3", 1),
        ])
        p = self.book.issue_product(0)
        p = self.book.issue_product()
        a = self.student(p, "A")
        # C1 non-mandatory: n1 ratio 1/5, n2 ratio 1/2 -> n1 dropped
        c1 = a["categories"][0]
        self.assertEqual(c1["dropped_assessment"], "n1")
        # C1 kept ratios: m1 4/5 and n2 1/2 -> (4/5+1/2)/2 = 13/20
        self.assertEqual(c1["score"], [13, 20])
        # C2: m2 1, n3 1/2 -> 3/4
        self.assertEqual(a["categories"][1]["score"], [3, 4])
        # course = 13/20 * 1/3 + 3/4 * 2/3
        #        = 13/60 + 1/2 = 13/60 + 30/60 = 43/60
        self.assertEqual(a["course_score"], [43, 60])
        # 43/60 >= 1/2 (30/60) -> eligible
        self.assertTrue(a["eligible"])

    def test_makeup_takes_best_ratio_and_tie_lexicographic(self):
        self.ingest("e1", [mirror("a-m1", "A", "m1", 3)])      # 3/10
        self.ingest("e2", [mirror("a-m1", "A", "m1", 6, revision=2)])  # 6/10
        self.ingest("e3", [mirror("a-m1-late", "A", "m1", 9)])  # 9/10 wins
        p = self.book.issue_product()
        self.assertEqual(self.assess(self.student(p, "A"), "m1")
                         ["selected_attempt"]["attempt_id"], "a-m1-late")
        # equal ratio across different maxes: 5/10 == 1/2
        self.ingest("e4", [
            mirror("z-equal", "B", "n2", 5),   # 5/10 = 1/2
            mirror("a-equal", "B", "n3", 1),   # 1/2 = 1/2
        ])
        # tie inside one assessment requires two attempts on the SAME assessment
        self.ingest("e5", [
            mirror("b-z", "B", "m1", 2),       # 2/10 = 1/5
            mirror("b-a", "B", "m1", 4, revision=1),  # 4/10 = 2/5 distinct attempt
        ])
        p = self.book.issue_product()
        bm1 = self.assess(self.student(p, "B"), "m1")
        self.assertEqual(bm1["selected_attempt"]["attempt_id"], "b-a")
        # exact equal ratio tie on one assessment: z1 and a1 both 5/10
        self.ingest("e6", [
            mirror("z1", "C", "m1", 5),
            mirror("a1", "C", "m1", 5),
        ])
        p = self.book.issue_product()
        self.assertEqual(self.assess(self.student(p, "C"), "m1")
                         ["selected_attempt"]["attempt_id"], "a1")

    def test_withdraw_top_attempt_reveals_other_and_late_low_revision_dead(self):
        self.ingest("e1", [
            mirror("t1", "A", "m1", 9),
            mirror("t2", "A", "m1", 3),
        ])
        p = self.book.issue_product()
        self.assertEqual(self.assess(self.student(p, "A"), "m1")
                         ["selected_attempt"]["attempt_id"], "t1")
        # withdraw t1: t2 (3/10) reappears; arrival order does not decide
        self.ingest("e2", [mirror("t1", "A", "m1", 9, revision=2, void=True)])
        p = self.book.issue_product()
        am1 = self.assess(self.student(p, "A"), "m1")
        self.assertEqual(am1["selected_attempt"]["attempt_id"], "t2")
        self.assertEqual(am1["ratio"], [3, 10])
        # a late lower revision of t1 must not resurrect it
        self.ingest("e3", [mirror("t1", "A", "m1", 9, revision=1)])
        p = self.book.issue_product()
        am1 = self.assess(self.student(p, "A"), "m1")
        self.assertEqual(am1["selected_attempt"]["attempt_id"], "t2")
        states = {s["attempt_id"]: s for s in self.student(p, "A")["attempt_states"]}
        self.assertEqual(states["t1"]["revision"], 2)
        self.assertTrue(states["t1"]["void"])

    def test_drop_tie_picks_largest_assessment_id(self):
        # C1: m1 present; n1 = n2 = same ratio 1/2 (10/20 and 5/10)
        self.ingest("e1", [
            mirror("c-m1", "C", "m1", 10),
            mirror("c-n1", "C", "n1", 10),
            mirror("c-n2", "C", "n2", 5),
            mirror("c-m2", "C", "m2", 5),
            mirror("c-n3", "C", "n3", 2),
        ])
        p = self.book.issue_product()
        c1 = self.student(p, "C")["categories"][0]
        self.assertEqual(c1["dropped_assessment"], "n2")  # n2 > n1 lexicographically

    def test_mandatory_zero_attended_but_missing_fails(self):
        # A attends m1 with 0 -> attended; give enough score to pass threshold
        self.ingest("e1", [
            mirror("a-m1", "A", "m1", 0),
            mirror("a-n1", "A", "n1", 20),
            mirror("a-n2", "A", "n2", 10),
            mirror("a-m2", "A", "m2", 5),
            mirror("a-n3", "A", "n3", 2),
        ])
        p = self.book.issue_product()
        a = self.student(p, "A")
        self.assertTrue(self.assess(a, "m1")["attended"])
        self.assertEqual(self.assess(a, "m1")["ratio"], [0, 1])
        self.assertTrue(a["eligible"])
        # B never attempts mandatory m2 -> ineligible regardless of score
        self.ingest("e2", [
            mirror("b-m1", "B", "m1", 10),
            mirror("b-n1", "B", "n1", 20),
            mirror("b-n2", "B", "n2", 10),
            mirror("b-n3", "B", "n3", 2),
        ])
        p = self.book.issue_product()
        b = self.student(p, "B")
        self.assertFalse(b["eligible"])
        self.assertTrue(any("m2" in r for r in b["eligibility_reasons"]))

    def test_exact_threshold_equality(self):
        # Engineer A's course score to exactly 1/2:
        # C1 all ratios 1/2 -> category 1/2; C2 all ratios 1/2 -> category 1/2
        # course = 1/2*1/3 + 1/2*2/3 = 1/2 exactly
        self.ingest("e1", [
            mirror("a-m1", "A", "m1", 5),
            mirror("a-n1", "A", "n1", 10),
            mirror("a-n2", "A", "n2", 5),
            mirror("a-m2", "A", "m2", 2),  # 2/5 != 1/2 ... adjust below
        ])
        # build exact: m2 max5 -> use fraction 1/2 impossible; instead use C2=1/2
        # via m2=5/5(1), n3=0/2(0) -> avg 1/2
        self.ingest("e2", [mirror("a-m2", "A", "m2", 5, revision=2),
                           mirror("a-n3", "A", "n3", 0)])
        p = self.book.issue_product()
        a = self.student(p, "A")
        self.assertEqual(a["course_score"], [1, 2])
        self.assertTrue(a["eligible"])  # "not below" threshold => passes

    def test_rejected_batches_change_nothing(self):
        good = [mirror("a-m1", "A", "m1", 5)]
        self.ingest("e1", good)
        bad_batches = [
            ("x1", [mirror("x", "Z", "m1", 1)]),                      # unknown student
            ("x2", [mirror("x", "A", "zzz", 1)]),                     # unknown assessment
            ("x3", [{"attempt_id": "x", "student": "A",
                     "assessment_id": "m1", "revision": 1,
                     "score": 1, "void": "no"}]),                      # bool value
            ("x4", [mirror("x", "A", "m1", 1, revision=0)]),          # non-positive rev
            ("x5", [mirror("x", "A", "m1", 11)]),                     # over max
            ("x6", [mirror("x", "A", "m1", 1, revision=True)]),       # bool revision
            ("x7", [mirror("x", "A", "m1", 1, revision=1.0)]),        # float revision
        ]
        for eid, batch in bad_batches:
            with self.assertRaises(IngestRejected):
                self.ingest(eid, batch)
        self.assertEqual(self.book.latest_seq(), 1)
        # same event id, different content -> conflict
        with self.assertRaises(IdempotencyConflict):
            self.ingest("e1", [mirror("a-m1", "A", "m1", 6)])
        # identical replay -> no new seq
        r = self.ingest("e1", good)
        self.assertTrue(r.replayed)
        self.assertEqual(self.book.latest_seq(), 1)

    def test_prefix_products_are_immutable_history(self):
        self.ingest("e1", [mirror("a-m1", "A", "m1", 1),
                           mirror("b-m1", "B", "m1", 1),
                           mirror("c-m1", "C", "m1", 1)])
        p_before = self.book.issue_product(prefix_seq=1)
        self.ingest("e2", [mirror("a-m1", "A", "m1", 10, revision=2)])
        p_after = self.book.issue_product()
        self.assertNotEqual(p_before["product_id"], p_after["product_id"])
        # old product unchanged when re-read after later facts and a new rule version
        rules2 = make_rules()
        rules2["threshold"] = [9, 10]
        self.book.put_rules("r2", rules2)
        old_again = self.book.get_product(p_before["product_id"])
        self.assertEqual(old_again, p_before)
        p_r2 = self.book.issue_product()
        self.assertEqual(p_r2["rule_version"], "r2")
        # products bind their own rule version
        self.assertEqual(old_again["rule_version"], "r1")

    def test_no_drop_with_fewer_than_two_nonmandatory_and_missing_is_zero(self):
        # C2 has exactly one non-mandatory (n3): never dropped; missing -> 0
        self.ingest("e1", [
            mirror("a-m1", "A", "m1", 10),
            mirror("a-n1", "A", "n1", 20),
            mirror("a-n2", "A", "n2", 10),
            mirror("a-m2", "A", "m2", 5),
            # n3 missing on purpose
        ])
        p = self.book.issue_product()
        c2 = self.student(p, "A")["categories"][1]
        self.assertIsNone(c2["dropped_assessment"])
        # C2 = (1 + 0)/2 = 1/2
        self.assertEqual(c2["score"], [1, 2])
        n3 = self.assess(self.student(p, "A"), "n3")
        self.assertTrue(n3["missing"])
        self.assertEqual(n3["ratio"], [0, 1])


if __name__ == "__main__":
    unittest.main()
