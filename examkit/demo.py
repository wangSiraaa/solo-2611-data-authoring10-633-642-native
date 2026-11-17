"""End-to-end demonstration on fully synthetic data.

Run with::

    python3 -m examkit.demo

It rebuilds ``data/examkit.db`` from scratch and writes JSON grade products and
seating plans under ``data/``.  The script also exercises the supplied
``materials/634/case.json`` micro input, the required greedy-fails-but-feasible
seating example, eligibility changes between two real products, confirmation
boundaries, two-connection interleaving and normal reopen.
"""

from __future__ import annotations

import json
import os

from .gradebook import Gradebook, IdempotencyConflict, IngestRejected
from .seater import ConfirmRejected, Seater, SeatingProtocolError
from .solver import greedy_first_available, manhattan
from .store import connect
from .verify import verify_database

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(HERE, "data")
DB_PATH = os.path.join(DATA, "examkit.db")
EXAM = "E-634"


def banner(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def dump(name, doc):
    path = os.path.join(DATA, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2, sort_keys=True)
    print(f"  wrote {os.path.relpath(path, HERE)}")


# --------------------------------------------------------------------------- #
# section 1: the supplied materials/634 micro case
# --------------------------------------------------------------------------- #
def section_micro_case():
    banner("1. Supplied materials/634 micro case (A/B, one category, 3 seats)")
    case_path = os.path.join(HERE, "materials", "634", "case.json")
    case = json.load(open(case_path, encoding="utf-8"))
    conn = connect(":memory:")
    gb = Gradebook(conn, "MICRO")
    rules = {
        "threshold": case["rules"]["threshold"],
        "categories": [{
            "id": "C", "weight": [1, 1],
            "assessments": case["rules"]["assessments"],
        }],
    }
    gb.put_rules("micro-r1", rules)
    gb.set_students(["A", "B"])
    for i, a in enumerate(case["before_attempts"], start=1):
        gb.ingest(f"e-{a['id']}", [{
            "attempt_id": a["id"], "student": a["student"],
            "assessment_id": a["assessment"], "revision": 1,
            "score": a["score"], "void": False}])
    p_before = gb.issue_product()
    add = case["added_attempt"]
    gb.ingest("e-added", [{
        "attempt_id": add["id"], "student": add["student"],
        "assessment_id": add["assessment"], "revision": 1,
        "score": add["score"], "void": False}])
    p_after = gb.issue_product()
    for sid in ("A", "B"):
        s0 = next(x for x in p_before["students"] if x["student"] == sid)
        s1 = next(x for x in p_after["students"] if x["student"] == sid)
        print(f"  {sid}: {s0['course_score']} eligible={s0['eligible']} "
              f"-> {s1['course_score']} eligible={s1['eligible']}")
    # hand check for A after: m1 6/10, n1 4/20(=1/5), n2 best 8/10(=4/5);
    # n1 dropped -> (3/5 + 4/5)/2 = 7/10 ; threshold 3/5 -> eligible
    a_after = next(x for x in p_after["students"] if x["student"] == "A")
    assert a_after["course_score"] == [7, 10] and a_after["eligible"]

    se = Seater(conn)
    rooms = {"rooms": [{"id": "R", "seats": [
        {**s, "available": True} for s in case["seats"]]}]}
    se.put_rooms("micro-rooms", "micro-v1", rooms)
    for st in case["registration"]:
        se.register(f"mreg-{st}", EXAM, st)
    for st, need in case["requirements"].items():
        se.set_need(f"mneed-{st}", EXAM, st,
                    accessible=need.get("accessible", False),
                    aisle=need.get("aisle", False))
    for a, b in case["conflicts"]:
        se.add_conflict(f"mcf-{a}-{b}", EXAM, a, b)
    se.set_current_product(EXAM, p_after["product_id"])
    plan = se.plan(EXAM, p_after["product_id"])
    print("  micro plan:", plan["status"], plan["assignment"],
          "rooms:", plan["room_count"])
    assert plan["status"] == "feasible"
    conn.close()
    return p_after, plan


# --------------------------------------------------------------------------- #
# section 2: full-scale synthetic gradebook
# --------------------------------------------------------------------------- #
FULL_STUDENTS = [f"S{i:02d}" for i in range(1, 13)]  # 12 students


def full_rules():
    # 4 categories x 6 assessments = 24; weights 1/4 each (sum exactly 1)
    cats = []
    for c in range(1, 5):
        assessments = []
        for k in range(1, 7):
            assessments.append({
                "id": f"c{c}-a{k}",
                "max": 5 + ((c * 7 + k * 3) % 20),       # varied max 5..24
                "mandatory": k <= 2,                       # 2 mandatory per category
            })
        cats.append({"id": f"C{c}", "weight": [1, 4], "assessments": assessments})
    return {"threshold": [3, 5], "categories": cats}


def section_full_gradebook():
    banner("2. Full-scale gradebook: 12 students, 24 quizzes, 100 mirrors")
    print("  envelope: <=12 students, <=24 quizzes, <=100 mirrors in the base "
          "fact stream;\n  later teaching makeups are post-prefix events, not "
          "part of that 100-mirror cap.")
    if os.path.exists(DB_PATH):
        os.unlink(DB_PATH)
    for suffix in ("-wal", "-shm"):
        p = DB_PATH + suffix
        if os.path.exists(p):
            os.unlink(p)
    conn = connect(DB_PATH)
    gb = Gradebook(conn, "COURSE-634")
    gb.put_rules("rules-v1", full_rules())
    gb.set_students(FULL_STUDENTS)
    rules = gb.get_rules()
    aids = [a["id"] for c in rules["categories"] for a in c["assessments"]]
    assert len(aids) == 24
    max_of = {a["id"]: a["max"]
              for c in rules["categories"] for a in c["assessments"]}

    # deterministic synthetic mirrors, bounded to 100 accepted event mirrors
    mirror_count = 0

    def send(event_id, batch):
        nonlocal mirror_count
        r = gb.ingest(event_id, batch)
        if not r.replayed:
            mirror_count += len(batch)
        return r

    # base round: 96 first-attempt mirrors — every mandatory quiz for every
    # student (12 students x 8 mandatory quizzes).  The 100-mirror envelope
    # leaves 4 slots; two go to non-mandatory attempts below so the issued
    # product already contains a mixed eligible/ineligible roster, and the
    # last two drive the withdraw scenario.
    mandatory = {a["id"] for c in rules["categories"] for a in c["assessments"]
                 if a["mandatory"]}
    base_mirrors = []
    for si, st in enumerate(FULL_STUDENTS):
        for ai, aid in enumerate(aids):
            mx = max_of[aid]
            if aid not in mandatory:
                continue
            score = (mx * (7 + ((si * 13 + ai * 5) % 4))) // 10  # 70..100%
            base_mirrors.append({"attempt_id": f"{st}-{aid}-v1", "student": st,
                                 "assessment_id": aid, "revision": 1,
                                 "score": score, "void": False})
    assert len(base_mirrors) == 96, len(base_mirrors)
    base_event_payloads = {}
    for i in range(4):
        chunk = base_mirrors[i * 24:(i + 1) * 24]
        eid = f"base-batch-{i + 1}"
        send(eid, chunk)
        base_event_payloads[eid] = chunk

    # targeted teaching scenarios (4 mirrors -> exactly 100 accepted mirrors)
    # (a) different-max retake pair: raw points are never compared across maxes
    send("mk-s01", [
        {"attempt_id": "S01-c1-a1-v2", "student": "S01", "assessment_id": "c1-a1",
         "revision": 2, "score": max_of["c1-a1"], "void": False},
        {"attempt_id": "S01-c2-a1-v2", "student": "S01", "assessment_id": "c2-a1",
         "revision": 2, "score": 1, "void": False},
    ])
    # (b)+(c) last two mirror slots: one non-mandatory attempt (different-max
    #     ratio material for the explanation) and the S05 mandatory withdraw
    #     that leaves c1-a1 missing -> ineligible.  Late-low-revision,
    #     zero-score attendance, drop ties and exact-threshold behavior are
    #     asserted on hand-built cases in tests/test_gradebook.py.
    send("extra-and-wd", [
        {"attempt_id": "S01-c1-a3-extra", "student": "S01",
         "assessment_id": "c1-a3", "revision": 1,
         "score": max_of["c1-a3"] // 5, "void": False},
        {"attempt_id": "S05-c1-a1-v1", "student": "S05", "assessment_id": "c1-a1",
         "revision": 2, "score": 0, "void": True},
    ])

    # protocol rejections (state unchanged)
    rejects = [
        ("rej-student", [{"attempt_id": "x", "student": "NOBODY",
                          "assessment_id": aids[0], "revision": 1,
                          "score": 1, "void": False}]),
        ("rej-bool", [{"attempt_id": "x", "student": "S01",
                       "assessment_id": aids[0], "revision": 1,
                       "score": 1, "void": "yes"}]),
        ("rej-over", [{"attempt_id": "x", "student": "S01",
                       "assessment_id": aids[0], "revision": 1,
                       "score": max_of[aids[0]] + 1, "void": False}]),
    ]
    for eid, batch in rejects:
        try:
            gb.ingest(eid, batch)
            raise AssertionError("rejection expected")
        except IngestRejected as e:
            print(f"  rejected {eid}: {e}")
    # stable event id: same text replay is a no-op, different text conflicts
    r = send("base-batch-1", base_event_payloads["base-batch-1"])
    assert r.replayed
    try:
        gb.ingest("base-batch-1", [{"attempt_id": "z", "student": "S01",
                                    "assessment_id": aids[0], "revision": 1,
                                    "score": 1, "void": False}])
        raise AssertionError("conflict expected")
    except IdempotencyConflict as e:
        print(f"  idempotency conflict: {e}")
    assert mirror_count == 100, mirror_count
    print(f"  accepted mirrors: {mirror_count} (cap 100), events: {gb.latest_seq()}")

    p1 = gb.issue_product()
    dump("grade_product_v1.json", p1)
    elig1 = {s["student"] for s in p1["students"] if s["eligible"]}
    print("  eligible under v1 (mandatory-only base):", sorted(elig1))
    s05 = next(s for s in p1["students"] if s["student"] == "S05")
    sel = next(a for a in s05["assessments"] if a["assessment_id"] == "c1-a1")
    print("  S05 c1-a1 after withdraw:",
          "missing" if sel["selected_attempt"] is None
          else sel["selected_attempt"]["attempt_id"], sel["ratio"],
          "attended:", sel["attended"])
    assert sel["selected_attempt"] is None and not sel["attended"]
    assert not s05["eligible"]

    # Post-p1 makeups (outside the 100-mirror base envelope): every student
    # attempts every non-mandatory quiz so a mixed roster emerges.  Products
    # stay immutable, so the earlier mandatory-only product remains fixed.
    nm_by_cat = {c["id"]: [a["id"] for a in c["assessments"] if not a["mandatory"]]
                 for c in rules["categories"]}
    for st in FULL_STUDENTS:
        batch = []
        for cid, nm in nm_by_cat.items():
            for j, aid in enumerate(nm):
                # S05 stays weak on the first quiz of each category
                factor = 2 if (st == "S05" and j == 0) else 9
                batch.append({"attempt_id": f"{st}-{aid}-mk", "student": st,
                              "assessment_id": aid, "revision": 1,
                              "score": (max_of[aid] * factor) // 10, "void": False})
        gb.ingest(f"makeup-{st}", batch)
    p1b = gb.issue_product()
    dump("grade_product_v1b.json", p1b)
    elig1b = {s["student"] for s in p1b["students"] if s["eligible"]}
    print("  eligible after non-mandatory makeups:", sorted(elig1b))
    # the first product is unchanged history
    assert gb.get_product(p1["product_id"]) == p1
    # every stored fraction is a reduced [n, d] pair with positive denominator
    from math import gcd
    for sd in p1b["students"]:
        n, d = sd["course_score"]
        assert d > 0 and gcd(n, d) == 1
    return gb, conn, p1b, max_of


# --------------------------------------------------------------------------- #
# section 3: full-scale rooms and seating facts
# --------------------------------------------------------------------------- #
def full_rooms():
    # 3 rooms, up to 3 rows x 4 cols each, exactly 24 available seats total
    # (12 in R1, 8 in R2, 4 in R3); every seat is usable.
    layout = [("R1", 3, 4), ("R2", 2, 4), ("R3", 1, 4)]
    rooms = []
    for rid, rows, cols in layout:
        seats = []
        for r in range(rows):
            for c in range(cols):
                sid = f"{rid.lower()}-{r}{c}"
                seats.append({
                    "seat_id": sid, "room": rid, "row": r, "col": c,
                    "available": True,
                    "accessible": c == 0,
                    "aisle": c in (0, cols - 1),
                })
        rooms.append({"id": rid, "seats": seats})
    total = sum(len(room["seats"]) for room in rooms)
    assert total == 24 and all(s["available"] for room in rooms for s in room["seats"])
    return {"rooms": rooms}


def section_full_seating(conn, p1):
    banner("3. Seating facts: 3 rooms/24 seats, <=60 registration/need/conflict ops")
    se = Seater(conn)
    se.put_rooms("rooms-full-1", "rooms-v1", full_rooms())
    op = 0

    def nid(prefix):
        nonlocal op
        op += 1
        return f"{prefix}-{op:02d}"

    # all 12 students register for the exam; two withdraw then re-register
    for st in FULL_STUDENTS:
        se.register(nid("reg"), EXAM, st)
    se.withdraw(nid("wd"), EXAM, "S11")
    se.register(nid("rereg"), EXAM, "S11")
    se.withdraw(nid("wd"), EXAM, "S12")  # stays withdrawn
    # needs: a few accessible / aisle requirements
    for st in ("S02", "S07"):
        se.set_need(nid("need"), EXAM, st, accessible=True, aisle=False)
    se.set_need(nid("need"), EXAM, "S09", accessible=False, aisle=True)
    # conflict edges (undirected); include one duplicate and one removal
    pairs = [("S01", "S02"), ("S02", "S03"), ("S03", "S04"),
             ("S04", "S05"), ("S05", "S06"), ("S01", "S06"),
             ("S07", "S08"), ("S08", "S09"), ("S09", "S10"),
             ("S01", "S10"), ("S02", "S08"), ("S03", "S09")]
    for a, b in pairs:
        se.add_conflict(nid("cf"), EXAM, a, b)
    se.add_conflict("cf-dup", EXAM, "S01", "S02")       # no extra effect
    se.add_conflict(nid("cf"), EXAM, "S10", "S11")
    se.remove_conflict(nid("rmcf"), EXAM, "S03", "S09")
    try:
        se.add_conflict("cf-self", EXAM, "S01", "S01")
        raise AssertionError("self loop must be rejected")
    except Exception as e:
        print("  self-loop rejected:", e)
    print(f"  seating operation count: {se.registration_head()} (cap 60)")
    assert se.registration_head() <= 60
    room_seats = sum(len(r["seats"]) for r in full_rooms()["rooms"])
    assert len(FULL_STUDENTS) <= 12 and room_seats <= 24

    se.set_current_product(EXAM, p1["product_id"])
    plan = se.plan(EXAM, p1["product_id"], room_version="rooms-v1")
    dump("seating_plan_v1.json", plan)
    print("  plan v1:", plan["status"], "rooms:", plan["room_count"],
          "seated:", len(plan["assignment"]),
          "unqualified:", [u["student"] for u in plan["unqualified_registrants"]])
    return se, plan


# --------------------------------------------------------------------------- #
# section 4: greedy failure example and eligibility-changing second product
# --------------------------------------------------------------------------- #
def section_greedy_and_second_product(gb, se, p1, plan1, max_of):
    banner("4. Name-order greedy fails while a feasible plan exists")
    seats = [
        {"seat_id": "g1", "room": "G", "row": 0, "col": 0, "available": True,
         "accessible": False, "aisle": False},
        {"seat_id": "g2", "room": "G", "row": 0, "col": 1, "available": True,
         "accessible": False, "aisle": False},
        {"seat_id": "g3", "room": "G", "row": 0, "col": 2, "available": True,
         "accessible": False, "aisle": False},
        {"seat_id": "g4", "room": "G", "row": 1, "col": 0, "available": True,
         "accessible": False, "aisle": False},
        {"seat_id": "g5", "room": "G", "row": 1, "col": 1, "available": True,
         "accessible": False, "aisle": False},
        {"seat_id": "g6", "room": "G", "row": 1, "col": 2, "available": True,
         "accessible": False, "aisle": False},
    ]
    students = ["A", "B", "C", "D", "E", "F"]
    conflicts = [("A", "B"), ("B", "C"), ("B", "D"), ("C", "D"),
                 ("D", "E"), ("E", "F")]
    greedy = greedy_first_available(students, seats, {}, conflicts)
    print("  greedy first-available result:", greedy)
    assert greedy is None
    # the complete solver receives exactly the same facts and must succeed;
    # the 6-seat room version is also persisted so plans can pin it
    se.put_rooms("greedy-rooms", "greedy-v1",
                 {"rooms": [{"id": "G", "seats": seats}]})
    from .solver import solve
    res = solve(students, seats, {}, conflicts)
    assert res["feasible"]
    by_id = {s["seat_id"]: s for s in seats}
    for a, b in conflicts:
        sa, sb = by_id[res["assignment"][a]], by_id[res["assignment"][b]]
        assert not (sa["room"] == sb["room"] and manhattan(sa, sb) == 1)
    dump("greedy_feasible_solution.json", res)
    print("  complete-search solution:", res["assignment"],
          "rooms:", res["rooms_used"], "nodes:", res["nodes"])

    # persist the same six-student micro family as a real plan (course student
    # names mapped onto the greedy room's six seats) so the independent
    # verifier's <=8 exhaustive enumeration runs against a stored plan
    micro_exam = "E-MICRO"
    micro_students = FULL_STUDENTS[:6]
    name_map = dict(zip(students, micro_students))
    micro_product = gb.issue_product()
    for st in micro_students:
        se.register(f"micro-reg-{st}", micro_exam, st)
    for a, b in conflicts:
        se.add_conflict(f"micro-cf-{a}-{b}", micro_exam, name_map[a], name_map[b])
    se.set_current_product(micro_exam, micro_product["product_id"])
    micro_plan = se.plan(micro_exam, micro_product["product_id"],
                         room_version="greedy-v1")
    dump("micro_seating_plan.json", micro_plan)
    print("  persisted micro plan (verifier enumerates all assignments):",
          micro_plan["status"], "seated:", len(micro_plan["assignment"]))

    banner("5. Second real product: legal makeup/withdraw changes the roster")
    s05_before = next(s for s in p1["students"] if s["student"] == "S05")
    print("  S05 eligible before:", s05_before["eligible"],
          s05_before["eligibility_reasons"])
    assert not s05_before["eligible"]
    # heal S05: a legal new valid attempt on the missing mandatory quiz flips
    # ineligible -> eligible (the old withdrawn revisions stay in the history)
    gb.ingest("heal-s05", [{"attempt_id": "S05-c1-a1-healed", "student": "S05",
                            "assessment_id": "c1-a1", "revision": 3,
                            "score": max_of["c1-a1"], "void": False}])
    p2 = gb.issue_product()
    dump("grade_product_v2.json", p2)
    e1 = {s["student"] for s in p1["students"] if s["eligible"]}
    e2 = {s["student"] for s in p2["students"] if s["eligible"]}
    print("  eligibility v1:", sorted(e1))
    print("  eligibility v2:", sorted(e2))
    print("  added:", sorted(e2 - e1), "removed:", sorted(e1 - e2))
    assert e1 != e2

    se.set_current_product(EXAM, p2["product_id"])
    # consumer still holding the v1 eligibility set must get an explicit error
    try:
        se.plan(EXAM, p2["product_id"], room_version="rooms-v1",
                expected_eligibility=sorted(e1))
        raise AssertionError("stale eligibility use must fail loudly")
    except SeatingProtocolError as e:
        print("  stale-eligibility consumer rejected:", e)
    plan2 = se.plan(EXAM, p2["product_id"], room_version="rooms-v1")
    dump("seating_plan_v2.json", plan2)
    print("  plan v2:", plan2["status"], "rooms:", plan2["room_count"],
          "seated:", len(plan2["assignment"]))
    assert plan2["plan_id"] != plan1["plan_id"]
    # old product still a legal, unchanged query; old plans still readable
    assert gb.get_product(p1["product_id"])["product_id"] == p1["product_id"]
    return p2, plan2


# --------------------------------------------------------------------------- #
# section 5: confirmation boundaries, interleaving, reopen, verify
# --------------------------------------------------------------------------- #
def section_confirm_and_verify(gb, se, p1, p2, plan1, plan2):
    banner("6. Confirmation boundaries and two-connection interleaving")
    ok = se.confirm(plan2["plan_id"], "confirm-v2")
    print("  confirm current plan:", ok["status"])
    # identical replay
    again = se.confirm(plan2["plan_id"], "confirm-v2")
    assert again == ok
    try:
        se.confirm(plan2["plan_id"], "confirm-v2", payload={"tamper": True})
        raise AssertionError("different payload must be rejected")
    except SeatingProtocolError as e:
        print("  same request id, different text rejected:", e)

    # post-plan grade change: current product moves, old plan confirm rejected
    gb.ingest("late-makeup", [
        {"attempt_id": "S06-c1-a3-late", "student": "S06",
         "assessment_id": "c1-a3", "revision": 2,
         "score": gb.get_rules()["categories"][0]["assessments"][2]["max"],
         "void": False}])
    p3 = gb.issue_product()
    se.set_current_product(EXAM, p3["product_id"])
    try:
        se.confirm(plan2["plan_id"], "confirm-after-grade-change")
        raise AssertionError("confirmation must fail after product change")
    except ConfirmRejected as e:
        print("  post-change confirm rejected:", e.reason)
    # immutable history survives
    assert se.get_confirm("confirm-v2")["status"] == "confirmed"
    assert se.get_plan(plan2["plan_id"])["plan_id"] == plan2["plan_id"]

    # post-plan registration change on a fresh plan, then two connections
    plan3 = se.plan(EXAM, p3["product_id"], room_version="rooms-v1")
    other = Seater(connect(DB_PATH))
    other.get_plan(plan3["plan_id"])  # both connections read the same plan
    se.get_plan(plan3["plan_id"])
    other.register("interleaved-reg", EXAM, "S12")  # S12 re-registers, commits
    try:
        se.confirm(plan3["plan_id"], "confirm-interleave-a")
        raise AssertionError("serialized transaction must reject stale plan")
    except ConfirmRejected as e:
        print("  interleaved confirm rejected on original connection:", e.reason)
    plan4 = se.plan(EXAM, p3["product_id"], room_version="rooms-v1")
    ok4 = se.confirm(plan4["plan_id"], "confirm-interleave-b")
    print("  replan after committed change confirms:", ok4["status"])
    other.conn.close()

    # plan count stays within the 20-plan envelope
    all_plans = se.list_plans()
    print("  total stored plans:", len(all_plans), "(cap 20)")
    assert len(all_plans) <= 20
    gb.conn.commit() if False else None
    gb.conn.close()
    se.conn.close()

    banner("7. Reopen + independent verification")
    gb2 = Gradebook(connect(DB_PATH), "COURSE-634")
    se2 = Seater(connect(DB_PATH))
    print("  reopened: seq =", gb2.latest_seq(),
          "products =", len(gb2.list_products()),
          "plans =", len(se2.list_plans()),
          "confirms =", se2.registration_head(), "ops")
    report = verify_database(DB_PATH)
    dump("verification_report.json", report)
    total = sum(len(a["checks"]) for a in report["areas"])
    print(f"  independent verify: {'PASS' if report['passed'] else 'FAIL'} "
          f"({total} checks)")
    for area in report["areas"]:
        for c in area["checks"]:
            if not c["ok"]:
                print("   !! FAIL", c)
    assert report["passed"]
    gb2.conn.close()
    se2.conn.close()


def main():
    os.makedirs(DATA, exist_ok=True)
    section_micro_case()
    gb, conn, p1, max_of = section_full_gradebook()
    se, plan1 = section_full_seating(conn, p1)
    p2, plan2 = section_greedy_and_second_product(gb, se, p1, plan1, max_of)
    section_confirm_and_verify(gb, se, p1, p2, plan1, plan2)
    banner("DONE")
    print("Artifacts in data/: grade products, seating plans, greedy solution,")
    print("verification report, and the persistent SQLite database examkit.db")


if __name__ == "__main__":
    main()
