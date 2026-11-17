"""Solver tests, including independent brute-force enumeration."""

import unittest
from itertools import permutations

from examkit.solver import (
    greedy_first_available, manhattan, solve, validate_rooms,
)


def seat(sid, room, row, col, available=True, accessible=False, aisle=False):
    return {"seat_id": sid, "room": room, "row": row, "col": col,
            "available": available, "accessible": accessible, "aisle": aisle}


def brute_force_optimal(students, seats, needs, conflicts):
    """Independent enumeration: minimum rooms, then lexicographic seat list."""
    avail = [s for s in seats if s["available"]]
    students = sorted(students)
    cset = {frozenset(p) for p in conflicts}
    legal = []

    def adjacent(s1, s2):
        return s1["room"] == s2["room"] and manhattan(s1, s2) == 1

    def rec(i, used, assign):
        if i == len(students):
            legal.append(dict(assign))
            return
        st = students[i]
        need = needs.get(st, {"accessible": False, "aisle": False})
        for s in avail:
            if s["seat_id"] in used:
                continue
            if need.get("accessible") and not s["accessible"]:
                continue
            if need.get("aisle") and not s["aisle"]:
                continue
            bad = False
            for other, osid in assign.items():
                if frozenset((st, other)) in cset:
                    ose = next(x for x in avail if x["seat_id"] == osid)
                    if adjacent(s, ose):
                        bad = True
                        break
            if bad:
                continue
            used.add(s["seat_id"])
            assign[st] = s["seat_id"]
            rec(i + 1, used, assign)
            del assign[st]
            used.discard(s["seat_id"])

    rec(0, set(), {})
    if not legal:
        return None
    by_id = {s["seat_id"]: s for s in avail}
    min_rooms = min(len({by_id[x]["room"] for x in a.values()}) for a in legal)
    best = min(([a[st] for st in students] for a in legal
                if len({by_id[x]["room"] for x in a.values()}) == min_rooms))
    return min_rooms, best


class SolverBasicTests(unittest.TestCase):
    def test_adjacency_manhattan_only(self):
        s1 = seat("x", "R", 0, 0)
        self.assertEqual(manhattan(s1, seat("y", "R", 0, 1)), 1)   # adjacent
        self.assertEqual(manhattan(s1, seat("z", "R", 1, 1)), 2)   # diagonal: not
        self.assertEqual(manhattan(s1, seat("w", "Q", 0, 1)), 1)   # diff room: not adj

    def test_room_validation(self):
        with self.assertRaises(ValueError):
            validate_rooms({"rooms": []})
        with self.assertRaises(ValueError):
            validate_rooms({"rooms": [{"id": "R", "seats": [
                seat("s1", "R", 0, 0), seat("s1", "R", 0, 1)]}]})  # duplicate id
        with self.assertRaises(ValueError):
            validate_rooms({"rooms": [{"id": "R", "seats": [
                seat("s1", "R", 0, 0), seat("s2", "R", 0, 0)]}]})  # duplicate coord
        with self.assertRaises(ValueError):
            validate_rooms({"rooms": [{"id": "R", "seats": [
                seat("s1", "R", 0, 4)]}]})                          # col out of range

    def test_empty_eligible_set_uses_zero_rooms(self):
        res = solve([], [seat("s1", "R", 0, 0)], {}, [])
        self.assertTrue(res["feasible"])
        self.assertEqual(res["rooms_used"], 0)
        self.assertEqual(res["assignment"], {})

    def test_unavailable_seats_never_assigned(self):
        seats = [seat("s1", "R", 0, 0, available=False),
                 seat("s2", "R", 0, 1)]
        res = solve(["A"], seats, {}, [])
        self.assertTrue(res["feasible"])
        self.assertEqual(res["assignment"], {"A": "s2"})
        res2 = solve(["A", "B"], seats, {}, [])
        self.assertFalse(res2["feasible"])


class GreedyFailureButFeasibleTests(unittest.TestCase):
    """Required example: name-order first-fit fails, a solution still exists."""

    def test_greedy_fails_but_complete_search_succeeds(self):
        # 3x2 grid, six students, one seat each.  Conflict chain verified by hand.
        seats = [
            seat("g1", "R1", 0, 0), seat("g2", "R1", 0, 1),
            seat("g3", "R1", 0, 2),
            seat("g4", "R1", 1, 0), seat("g5", "R1", 1, 1),
            seat("g6", "R1", 1, 2),
        ]
        students = ["A", "B", "C", "D", "E", "F"]
        conflicts = [("A", "B"), ("B", "C"), ("B", "D"),
                     ("C", "D"), ("D", "E"), ("E", "F")]
        # hand trace of the documented name-order first-available greedy:
        #  A=g1
        #  B conflicts A: g2 adjacent -> g3
        #  C conflicts B,D: g2 (B at g3 is dist2) -> g2
        #  D conflicts B,C,E: g4 adjacent C(g2)->no; g5 dist2 from g2 but
        #      adjacent B? g5 vs g3 manhattan 3 -> free -> g5
        #  E conflicts D,F: g4 (D at g5 adjacent -> blocked); g6 (D at g5
        #      adjacent -> blocked); nothing left -> FAIL
        greedy = greedy_first_available(students, seats, {}, conflicts)
        self.assertIsNone(greedy, "documented name-order greedy must fail here")
        res = solve(students, seats, {}, conflicts)
        self.assertTrue(res["feasible"], "complete search must find a plan")
        # hand-check the found assignment satisfies every edge
        by_id = {s["seat_id"]: s for s in seats}
        for a, b in conflicts:
            sa, sb = by_id[res["assignment"][a]], by_id[res["assignment"][b]]
            self.assertFalse(sa["room"] == sb["room"] and manhattan(sa, sb) == 1)
        # independently enumerated optimum agrees (one room, lex-min)
        bf = brute_force_optimal(students, seats, {}, conflicts)
        self.assertIsNotNone(bf)
        self.assertEqual(res["rooms_used"], bf[0])
        self.assertEqual([res["assignment"][s] for s in students], bf[1])


class OptimalVsEnumerationTests(unittest.TestCase):
    def setUp(self):
        self.rooms_seats = [
            seat("a1", "R1", 0, 0, accessible=True, aisle=True),
            seat("a2", "R1", 0, 1), seat("a3", "R1", 0, 2, aisle=True),
            seat("b1", "R2", 0, 0, accessible=True),
            seat("b2", "R2", 0, 1), seat("b3", "R2", 0, 2),
            seat("b4", "R2", 1, 0),
        ]

    def test_minimum_rooms_and_lexicographic_order(self):
        students = ["A", "B", "C", "D"]
        res = solve(students, self.rooms_seats, {}, [])
        bf = brute_force_optimal(students, self.rooms_seats, {}, [])
        self.assertIsNotNone(bf)
        self.assertEqual(res["rooms_used"], bf[0])
        self.assertEqual([res["assignment"][s] for s in sorted(students)], bf[1])
        # R1 has 3 seats, R2 has 4: 4 students fit in one room only via R2,
        # so minimum rooms = 1 with R2's four smallest seat ids
        self.assertEqual(res["rooms_used"], 1)
        self.assertEqual([res["assignment"][s] for s in sorted(students)],
                         ["b1", "b2", "b3", "b4"])

    def test_conflict_non_adjacency_forces_spread(self):
        students = ["A", "B", "C"]
        conflicts = [("A", "B"), ("B", "C"), ("A", "C")]  # clique of 3
        res = solve(students, self.rooms_seats, {}, conflicts)
        self.assertTrue(res["feasible"])
        by_id = {s["seat_id"]: s for s in self.rooms_seats}
        for a, b in conflicts:
            sa, sb = by_id[res["assignment"][a]], by_id[res["assignment"][b]]
            self.assertFalse(sa["room"] == sb["room"] and manhattan(sa, sb) == 1)
        bf = brute_force_optimal(students, self.rooms_seats, {}, conflicts)
        self.assertEqual(res["rooms_used"], bf[0])
        self.assertEqual([res["assignment"][s] for s in sorted(students)], bf[1])

    def test_accessible_and_aisle_needs(self):
        students = ["A", "B"]
        needs = {"A": {"accessible": True, "aisle": True},
                 "B": {"accessible": True, "aisle": False}}
        res = solve(students, self.rooms_seats, needs, [])
        self.assertTrue(res["feasible"])
        by_id = {s["seat_id"]: s for s in self.rooms_seats}
        self.assertTrue(by_id[res["assignment"]["A"]]["accessible"])
        self.assertTrue(by_id[res["assignment"]["A"]]["aisle"])
        self.assertTrue(by_id[res["assignment"]["B"]]["accessible"])
        bf = brute_force_optimal(students, self.rooms_seats, needs, [])
        self.assertEqual([res["assignment"][s] for s in sorted(students)], bf[1])

    def test_infeasible_need_confirmed_by_enumeration(self):
        # two students both needing the single aisle+accessible seat a1
        students = ["A", "B"]
        needs = {"A": {"accessible": True, "aisle": True},
                 "B": {"accessible": True, "aisle": True}}
        res = solve(students, self.rooms_seats, needs, [])
        self.assertFalse(res["feasible"])
        self.assertIsNone(brute_force_optimal(students, self.rooms_seats, needs, []))

    def test_micro_families_match_bruteforce(self):
        import random
        rng = random.Random(634)
        pool = self.rooms_seats
        for n in range(1, 7):
            students = [f"S{i}" for i in range(n)]
            for trial in range(20):
                seats = [dict(s) for s in pool]
                for s in seats:
                    if rng.random() < 0.15:
                        s["available"] = False
                edges = []
                for pair in permutations(students, 2):
                    if rng.random() < 0.12 and tuple(sorted(pair)) not in edges:
                        edges.append(tuple(sorted(pair)))
                needs = {st: {"accessible": rng.random() < 0.2,
                              "aisle": rng.random() < 0.2} for st in students}
                res = solve(students, seats, needs, edges)
                bf = brute_force_optimal(students, seats, needs, edges)
                if bf is None:
                    self.assertFalse(res["feasible"])
                else:
                    self.assertTrue(res["feasible"])
                    self.assertEqual(res["rooms_used"], bf[0])
                    self.assertEqual([res["assignment"][s] for s in students], bf[1])


if __name__ == "__main__":
    unittest.main()
