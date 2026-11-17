"""Independent verifier.

This module deliberately re-implements every rule from scratch and only reads
the SQLite file (plus JSON).  It never imports the gradebook scoring code or
the seating solver, so it cannot inherit their mistakes:

* rational comparisons use hand-written integer cross multiplication;
* grade products are recomputed from raw event rows and compared as canonical
  documents against the stored product;
* seating plans are checked seat by seat against rooms/needs/conflicts;
* for micro families (<=8 eligible students) *all* legal assignments are
  enumerated with itertools to prove minimum rooms and lexicographic optimum.
"""

from __future__ import annotations

import json
import sqlite3


def _gcd(a: int, b: int) -> int:
    a, b = abs(a), abs(b)
    while b:
        a, b = b, a % b
    return a


def _red(n: int, d: int) -> list[int]:
    g = _gcd(n, d)
    return [n // g, d // g]


def _rat_lt(a: list[int], b: list[int]) -> bool:
    return a[0] * b[1] < b[0] * a[1]


def _rat_eq(a: list[int], b: list[int]) -> bool:
    return a[0] * b[1] == b[0] * a[1]


def _rat_add(a, b):
    return _red(a[0] * b[1] + b[0] * a[1], a[1] * b[1])


def _rat_mul(a, b):
    return _red(a[0] * b[0], a[1] * b[1])


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


# --------------------------------------------------------------------------- #
# gradebook re-derivation (independent of examkit.gradebook)
# --------------------------------------------------------------------------- #
def _recompute_grade(rules: dict, students: list[str], mirrors: list[dict],
                     prefix_seq: int, rule_version: str) -> dict:
    aidx = {a["id"]: (a["max"], a["mandatory"], c["id"])
            for c in rules["categories"] for a in c["assessments"]}

    # fold revisions: strictly highest revision wins, late low revisions ignored
    states: dict[str, dict] = {}
    for m in mirrors:
        cur = states.get(m["attempt_id"])
        if cur is None or m["revision"] > cur["revision"]:
            states[m["attempt_id"]] = dict(m)

    student_docs = []
    for student in sorted(students):
        mine = [s for s in states.values() if s["student"] == student]
        best = {}
        for s in mine:
            if s["void"]:
                continue
            cur = best.get(s["assessment_id"])
            if cur is None:
                best[s["assessment_id"]] = s
                continue
            ns, nm = s["score"], aidx[s["assessment_id"]][0]
            os_, om = cur["score"], aidx[cur["assessment_id"]][0]
            if ns * om > os_ * nm or (ns * om == os_ * nm
                                      and s["attempt_id"] < cur["attempt_id"]):
                best[s["assessment_id"]] = s

        assess_docs = {}
        mandatory_attended = {}
        for aid, (mx, mandatory, _cid) in aidx.items():
            sel = best.get(aid)
            attended = any(x["assessment_id"] == aid and not x["void"] for x in mine)
            if mandatory:
                mandatory_attended[aid] = attended
            assess_docs[aid] = {
                "assessment_id": aid,
                "max": mx,
                "mandatory": mandatory,
                "missing": sel is None,
                "attended": attended,
                "selected_attempt": None if sel is None else {
                    "attempt_id": sel["attempt_id"],
                    "revision": sel["revision"],
                    "score": sel["score"],
                    "ratio": _red(sel["score"], mx),
                },
                "ratio": _red(0, 1) if sel is None else _red(sel["score"], mx),
            }

        category_docs = []
        course = [0, 1]
        for cat in rules["categories"]:
            items = [assess_docs[a["id"]] for a in cat["assessments"]]
            non_mand = [it for it in items if not it["mandatory"]]
            dropped = None
            if len(non_mand) >= 2:
                worst = non_mand[0]
                for it in non_mand[1:]:
                    if _rat_lt(it["ratio"], worst["ratio"]) or (
                        _rat_eq(it["ratio"], worst["ratio"])
                        and it["assessment_id"] > worst["assessment_id"]
                    ):
                        worst = it
                dropped = worst["assessment_id"]
            kept = [it for it in items if it["assessment_id"] != dropped]
            total = [0, 1]
            for it in kept:
                total = _rat_add(total, it["ratio"])
            cat_score = _red(total[0], total[1] * len(kept))
            course = _rat_add(course, _rat_mul(cat_score, cat["weight"]))
            category_docs.append({
                "category_id": cat["id"],
                "weight": cat["weight"],
                "items": items,
                "dropped_assessment": dropped,
                "drop_rule": ("at least two non-mandatory items; dropped minimum ratio, "
                              "tie broken by largest assessment_id") if dropped else
                             ("no drop: fewer than two non-mandatory items"
                              if len(non_mand) < 2 else "no drop"),
                "score": cat_score,
            })

        missing_mand = sorted(a for a, ok in mandatory_attended.items() if not ok)
        reasons = []
        if _rat_lt(course, rules["threshold"]):
            reasons.append(f"course score {course[0]}/{course[1]} below threshold "
                           f"{rules['threshold'][0]}/{rules['threshold'][1]}")
        if missing_mand:
            reasons.append("missing valid attempt for mandatory: " + ",".join(missing_mand))
        student_docs.append({
            "student": student,
            "eligible": not reasons,
            "eligibility_reasons": reasons,
            "course_score": course,
            "threshold": rules["threshold"],
            "mandatory_attended": dict(sorted(mandatory_attended.items())),
            "assessments": [assess_docs[k] for k in sorted(assess_docs)],
            "categories": category_docs,
            "attempt_states": sorted(
                ({"attempt_id": s["attempt_id"], "assessment_id": s["assessment_id"],
                  "revision": s["revision"], "score": s["score"], "void": s["void"]}
                 for s in mine),
                key=lambda d: d["attempt_id"]),
        })

    return {"kind": "grade_product", "rule_version": rule_version,
            "prefix_seq": prefix_seq, "threshold": rules["threshold"],
            "students": student_docs}


def verify_gradebook(conn: sqlite3.Connection) -> dict:
    checks = []

    def ok(name, cond, detail=""):
        checks.append({"check": name, "ok": bool(cond), "detail": detail})

    events = conn.execute("SELECT seq, mirror_json FROM gb_events ORDER BY seq").fetchall()
    all_mirrors = [(r["seq"], json.loads(r["mirror_json"])) for r in events]
    products = conn.execute("SELECT * FROM gb_products ORDER BY prefix_seq").fetchall()

    for prow in products:
        stored = json.loads(prow["doc_json"])
        rules = json.loads(conn.execute(
            "SELECT rules_json FROM gb_rule_versions WHERE rule_version=?",
            (stored["rule_version"],)).fetchone()["rules_json"])
        students = [r["student"] for r in conn.execute(
            "SELECT student FROM gb_students WHERE rule_version=? ORDER BY student",
            (stored["rule_version"],)).fetchall()]
        mirrors = [m for seq, ms in all_mirrors if seq <= stored["prefix_seq"] for m in ms]
        recomputed = _recompute_grade(rules, students, mirrors,
                                      stored["prefix_seq"], stored["rule_version"])
        recomputed["product_id"] = stored["product_id"]
        ok(f"product {prow['product_id']} exact recomputation",
           _canon(recomputed) == _canon(stored),
           "canonical JSON of independent recomputation matches stored product")

        # independent hand arithmetic on the first student's first category
        if stored["students"]:
            sd = stored["students"][0]
            cat = sd["categories"][0]
            manual = [0, 1]
            for it in cat["items"]:
                if it["assessment_id"] != cat["dropped_assessment"]:
                    manual = _rat_add(manual, it["ratio"])
            manual = _red(manual[0], manual[1] * sum(
                1 for it in cat["items"]
                if it["assessment_id"] != cat["dropped_assessment"]))
            ok(f"product {prow['product_id']} hand cross-multiply category "
               f"{cat['category_id']} / {sd['student']}",
               _rat_eq(manual, cat["score"]),
               f"manual {manual} == stored {cat['score']}")
            # eligibility consistency: reasons exist exactly when not eligible
            ok(f"product {prow['product_id']} eligibility flag/reason consistency "
               f"({sd['student']})",
               sd["eligible"] == (len(sd["eligibility_reasons"]) == 0))
    ok("at least one grade product exists", len(products) > 0)
    return {"area": "gradebook", "passed": all(c["ok"] for c in checks), "checks": checks}


# --------------------------------------------------------------------------- #
# seating re-derivation (independent of examkit.solver)
# --------------------------------------------------------------------------- #
def _adjacent(s1: dict, s2: dict) -> bool:
    return s1["room"] == s2["room"] and (
        abs(s1["row"] - s2["row"]) + abs(s1["col"] - s2["col"]) == 1)


def _all_legal_assignments(students: list[str], seats: list[dict], needs: dict,
                           conflicts: set[frozenset]):
    """Enumerate every legal assignment (micro families only)."""
    seats = sorted(seats, key=lambda s: s["seat_id"])
    results = []

    def rec(i, used, assign):
        if i == len(students):
            results.append(dict(assign))
            return
        st = students[i]
        need = needs.get(st, {"accessible": False, "aisle": False})
        for seat in seats:
            sid = seat["seat_id"]
            if sid in used:
                continue
            if need.get("accessible") and not seat["accessible"]:
                continue
            if need.get("aisle") and not seat["aisle"]:
                continue
            clash = False
            for other, osid in assign.items():
                if frozenset((st, other)) in conflicts and _adjacent(
                        seat, next(s for s in seats if s["seat_id"] == osid)):
                    clash = True
                    break
            if clash:
                continue
            used.add(sid)
            assign[st] = sid
            rec(i + 1, used, assign)
            del assign[st]
            used.discard(sid)

    rec(0, set(), {})
    return results


def verify_seating(conn: sqlite3.Connection) -> dict:
    checks = []

    def ok(name, cond, detail=""):
        checks.append({"check": name, "ok": bool(cond), "detail": detail})

    plans = conn.execute("SELECT doc_json FROM se_plans ORDER BY plan_id").fetchall()
    for prow in plans:
        plan = json.loads(prow["doc_json"])
        pid = plan["plan_id"]
        rooms = plan["rooms_input"]
        all_seats = [s for room in rooms for s in room["seats"]]
        seat_by_id = {s["seat_id"]: s for s in all_seats}
        avail = [s for s in all_seats if s["available"]]
        students = plan["qualified_registrants"]
        needs = plan["needs"]
        conflicts = {frozenset(p) for p in plan["conflicts"]}

        # replay facts up to plan head and compare registrants / conflicts / needs
        ops = [json.loads(r["doc_json"]) for r in conn.execute(
            "SELECT doc_json FROM se_ops WHERE op_seq <= ? ORDER BY op_seq",
            (plan["registration_head"],)).fetchall()]
        reg, rneeds, rconflicts = set(), {}, set()
        for op in ops:
            if op["exam_id"] != plan["exam_id"]:
                continue
            if op["op"] == "register":
                reg.add(op["student"])
            elif op["op"] == "withdraw":
                reg.discard(op["student"])
            elif op["op"] == "set_need":
                rneeds[op["student"]] = {"accessible": op["accessible"],
                                         "aisle": op["aisle"]}
            elif op["op"] == "add_conflict":
                rconflicts.add(frozenset((op["a"], op["b"])))
            elif op["op"] == "remove_conflict":
                rconflicts.discard(frozenset((op["a"], op["b"])))
        ok(f"{pid}: registrants match replayed head",
           reg == set(plan["registrants"]))
        ok(f"{pid}: conflicts match replayed head",
           conflicts == rconflicts)
        ok(f"{pid}: needs match replayed head",
           all(rneeds.get(st, {"accessible": False, "aisle": False}) == plan["needs"][st]
               for st in students))

        # product binding exists and hash matches
        prod_row = conn.execute(
            "SELECT payload_hash FROM gb_products WHERE product_id=?",
            (plan["product_id"],)).fetchone()
        ok(f"{pid}: bound to a real stored grade product",
           prod_row is not None and prod_row["payload_hash"] == plan["product_hash"])

        if plan["status"] == "infeasible":
            # independently confirm infeasibility when small enough to enumerate
            if len(students) <= 8:
                legals = _all_legal_assignments(students, avail, needs, conflicts)
                ok(f"{pid}: infeasibility confirmed by full enumeration",
                   len(legals) == 0, plan.get("infeasible_reason", ""))
            else:
                ok(f"{pid}: infeasible plan recorded with replayable reason",
                   bool(plan["infeasible_reason"]))
            continue

        assign = plan["assignment"]
        ok(f"{pid}: every qualified registrant seated exactly once",
           sorted(assign.keys()) == sorted(students) and len(assign) == len(students))
        ok(f"{pid}: seats unique, available, and exist in pinned room version",
           len(set(assign.values())) == len(assign)
           and all(sid in seat_by_id and seat_by_id[sid]["available"]
                   for sid in assign.values()))
        for st, sid in assign.items():
            seat = seat_by_id[sid]
            need = needs.get(st, {"accessible": False, "aisle": False})
            ok(f"{pid}: needs of {st} satisfied at {sid}",
               (not need.get("accessible") or seat["accessible"])
               and (not need.get("aisle") or seat["aisle"]))
        for a, b in conflicts:
            if a in assign and b in assign:
                ok(f"{pid}: conflict {a}|{b} not adjacent",
                   not _adjacent(seat_by_id[assign[a]], seat_by_id[assign[b]]))

        used_rooms = {seat_by_id[sid]["room"] for sid in assign.values()}
        ok(f"{pid}: room count matches rooms_used field",
           len(used_rooms) == plan["room_count"]
           and sorted(used_rooms) == sorted(plan["rooms_used"]))

        # exhaustive proof of optimality for micro families
        if len(students) <= 8:
            legals = _all_legal_assignments(students, avail, needs, conflicts)
            ok(f"{pid}: enumeration finds the stored feasible plan",
               any(a == assign for a in legals))
            min_rooms = min(len({seat_by_id[s]["room"] for s in a.values()}) for a in legals)
            ok(f"{pid}: minimum room count proved by enumeration",
               plan["room_count"] == min_rooms)
            best_key = min([[a[st] for st in students] for a in legals
                            if len({seat_by_id[s]["room"] for s in a.values()}) == min_rooms])
            ok(f"{pid}: lexicographic optimum proved by enumeration",
               plan["seat_order"] == best_key,
               f"stored {plan['seat_order']} vs enumerated {best_key}")
        else:
            ok(f"{pid}: above micro-enumeration limit, seat-by-seat checks applied", True)

    ok("at least one seating plan exists", len(plans) > 0)
    return {"area": "seating", "passed": all(c["ok"] for c in checks), "checks": checks}


def verify_database(path: str) -> dict:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        g = verify_gradebook(conn)
        s = verify_seating(conn)
        return {"database": path,
                "passed": g["passed"] and s["passed"],
                "areas": [g, s]}
    finally:
        conn.close()
