"""Tests for the independent verifier (never imports scoring/solver code)."""

import os
import tempfile
import unittest

from examkit.gradebook import Gradebook
from examkit.seater import Seater
from examkit.store import connect
from examkit.verify import (
    _all_legal_assignments, _recompute_grade, verify_database,
)


def m(aid, st, a, score, revision=1, void=False):
    return {"attempt_id": aid, "student": st, "assessment_id": a,
            "revision": revision, "score": score, "void": void}


class VerifyTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.gb = Gradebook(connect(self.path), "C1")
        self.gb.put_rules("r1", {
            "threshold": [1, 2],
            "categories": [{
                "id": "C", "weight": [1, 1],
                "assessments": [
                    {"id": "m1", "max": 10, "mandatory": True},
                    {"id": "n1", "max": 20, "mandatory": False},
                    {"id": "n2", "max": 10, "mandatory": False},
                ],
            }],
        })
        self.gb.set_students(["A", "B", "C"])
        # A passes (4/5 on m1, full n1/n2 -> one drop keeps high average),
        # B fails the threshold, C misses the mandatory.
        self.gb.ingest("e1", [
            m("a1", "A", "m1", 8), m("a2", "A", "n1", 20), m("a3", "A", "n2", 10),
            m("b1", "B", "m1", 1), m("b2", "B", "n1", 2), m("b3", "B", "n2", 1),
            m("c2", "C", "n1", 20), m("c3", "C", "n2", 10),
        ])
        self.product = self.gb.issue_product()
        self.se = Seater(connect(self.path))
        self.se.put_rooms("rv", "v1", {"rooms": [
            {"id": "R1", "seats": [
                {"seat_id": "s1", "room": "R1", "row": 0, "col": 0,
                 "available": True, "accessible": True, "aisle": True},
                {"seat_id": "s2", "room": "R1", "row": 0, "col": 1,
                 "available": True, "accessible": False, "aisle": False},
                {"seat_id": "s3", "room": "R1", "row": 0, "col": 2,
                 "available": True, "accessible": False, "aisle": True},
                {"seat_id": "x", "room": "R1", "row": 1, "col": 0,
                 "available": False, "accessible": False, "aisle": False},
            ]},
            {"id": "R2", "seats": [
                {"seat_id": "t1", "room": "R2", "row": 0, "col": 0,
                 "available": True, "accessible": True, "aisle": False},
                {"seat_id": "t2", "room": "R2", "row": 0, "col": 1,
                 "available": True, "accessible": False, "aisle": False},
            ]},
        ]})
        for st in ("A", "B", "C"):
            self.se.register(f"reg-{st}", "E", st)
        self.se.set_need("need-a", "E", "A", aisle=True)
        self.se.add_conflict("cf-ab", "E", "A", "B")
        self.se.set_current_product("E", self.product["product_id"])
        self.plan = self.se.plan("E", self.product["product_id"])
        self.rules_doc = self.gb.get_rules("r1")
        self.gb.conn.close()
        self.se.conn.close()

    def test_independent_recompute_matches_stored_product(self):
        import json
        import sqlite3
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        mirrors = [x for r in conn.execute(
            "SELECT mirror_json FROM gb_events ORDER BY seq")
            for x in json.loads(r["mirror_json"])]
        rec = _recompute_grade(self.rules_doc, ["A", "B", "C"], mirrors,
                               self.product["prefix_seq"], "r1")
        rec["product_id"] = self.product["product_id"]
        self.assertEqual(rec["students"], self.product["students"])
        # hand integer check for A's single category: m1 8/10, n1 20/20=1,
        # n2 10/10=1 -> non-mandatory tie at 1 -> drop largest id n2
        a = next(s for s in rec["students"] if s["student"] == "A")
        cat = a["categories"][0]
        self.assertEqual(cat["dropped_assessment"], "n2")
        # kept: m1 4/5 and n1 1 -> (4/5+1)/2 = 9/10
        self.assertEqual(cat["score"], [9, 10])
        self.assertEqual(a["course_score"], [9, 10])
        conn.close()

    def test_micro_enumeration_independently_enumerates(self):
        # micro family: 3 qualified-ish students; use the verifier's enumerator
        seats = [
            {"seat_id": "s1", "room": "R", "row": 0, "col": 0},
            {"seat_id": "s2", "room": "R", "row": 0, "col": 1},
            {"seat_id": "s3", "room": "R", "row": 0, "col": 2},
        ]
        legals = _all_legal_assignments(
            ["A", "B"], seats, {}, {frozenset(("A", "B"))})
        # legal: not adjacent -> s1/s3, s3/s1 (2 of 6 permutations)
        self.assertEqual(len(legals), 2)

    def test_verify_database_passes_and_covers_enumeration(self):
        report = verify_database(self.path)
        self.assertTrue(report["passed"])
        seating_checks = report["areas"][1]["checks"]
        names = " ".join(c["check"] for c in seating_checks)
        self.assertIn("enumeration", names)  # <=8 eligible -> exhaustive proof
        # every check ok
        self.assertTrue(all(c["ok"] for c in seating_checks))


if __name__ == "__main__":
    unittest.main()
