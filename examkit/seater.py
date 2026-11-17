"""Independent exam registration + room seating subsystem.

Persistent facts (registration, accessibility/aisle needs, conflict edges,
room versions) live in the same SQLite file as the gradebook but in separate
tables; they are never derived from scores or names.

Plans are immutable documents bound to

* a real, stored grade product (by id + content hash),
* a registration/needs/conflicts prefix (``registration_head``),
* a pinned room version.

Confirmation re-checks every binding inside a single ``BEGIN IMMEDIATE``
transaction, so two connections are ordered by SQLite's write lock rather than
by in-memory version numbers.
"""

from __future__ import annotations

import json
import sqlite3

from .rational import is_int
from .solver import SeatingError, solve, validate_rooms
from .store import Tx, canonical, content_hash


class SeatingProtocolError(ValueError):
    """Stable-id replay conflicts or unknown references."""


class ConfirmRejected(Exception):
    def __init__(self, reason: str, doc: dict):
        super().__init__(reason)
        self.reason = reason
        self.doc = doc


# --------------------------------------------------------------------------- #
# fact replay (pure over op documents)
# --------------------------------------------------------------------------- #
def replay_facts(ops: list[dict]):
    """Replay ordered operation documents into registrations/needs/conflicts."""
    registered: dict[tuple, bool] = {}
    needs: dict[tuple, dict] = {}
    conflicts: set[frozenset] = set()
    for op in ops:
        kind = op["op"]
        exam = op["exam_id"]
        if kind == "register":
            registered[(exam, op["student"])] = True
        elif kind == "withdraw":
            registered[(exam, op["student"])] = False
        elif kind == "set_need":
            needs[(exam, op["student"])] = {
                "accessible": bool(op.get("accessible", False)),
                "aisle": bool(op.get("aisle", False)),
            }
        elif kind == "add_conflict":
            edge = frozenset((op["a"], op["b"]))
            conflicts.add((exam, edge))
        elif kind == "remove_conflict":
            edge = frozenset((op["a"], op["b"]))
            conflicts.discard((exam, edge))
    return registered, needs, conflicts


def _student_need(needs, exam_id, student):
    return needs.get((exam_id, student), {"accessible": False, "aisle": False})


class Seater:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ------------------------------------------------------------------ #
    # rooms (versioned snapshots)
    # ------------------------------------------------------------------ #
    def put_rooms(self, op_id: str, room_version: str, rooms_doc: dict) -> dict:
        if not (isinstance(room_version, str) and room_version):
            raise SeatingError("room_version must be a non-empty string")
        norm = validate_rooms(rooms_doc)
        doc = {"op": "put_rooms", "room_version": room_version, "rooms": norm["rooms"]}
        with Tx(self.conn):
            row = self.conn.execute(
                "SELECT doc_json FROM se_room_versions WHERE room_version=?",
                (room_version,)).fetchone()
            if row is not None:
                if canonical(json.loads(row["doc_json"])) != canonical(doc):
                    raise SeatingProtocolError(
                        f"room version {room_version} exists with different content")
                return json.loads(row["doc_json"])
            # room versions carry their own counter: they are not part of the
            # registration/needs/conflicts operation stream that plans pin
            row = self.conn.execute("SELECT v FROM meta WHERE k='se_room_seq'").fetchone()
            seq = (int(row["v"]) + 1) if row else 1
            self.conn.execute(
                "INSERT INTO se_room_versions(room_version, doc_json, created_op) "
                "VALUES(?,?,?)", (room_version, canonical(doc), seq))
            self.conn.execute(
                "INSERT INTO meta(k,v) VALUES('se_room_seq', ?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (str(seq),))
            return doc

    def get_rooms(self, room_version: str) -> dict:
        row = self.conn.execute(
            "SELECT doc_json FROM se_room_versions WHERE room_version=?",
            (room_version,)).fetchone()
        if row is None:
            raise SeatingProtocolError(f"unknown room version {room_version}")
        return json.loads(row["doc_json"])

    def latest_room_version(self) -> str | None:
        row = self.conn.execute(
            "SELECT room_version FROM se_room_versions ORDER BY created_op DESC LIMIT 1"
        ).fetchone()
        return row["room_version"] if row else None

    # ------------------------------------------------------------------ #
    # registration / needs / conflicts
    # ------------------------------------------------------------------ #
    def _next_op_seq(self) -> int:
        row = self.conn.execute("SELECT COALESCE(MAX(op_seq),0) AS s FROM se_ops").fetchone()
        return row["s"] + 1

    def _insert_op(self, op_id: str, seq: int, doc: dict) -> None:
        self.conn.execute(
            "INSERT INTO se_ops(op_id, op_seq, payload_hash, doc_json) VALUES(?,?,?,?)",
            (op_id, seq, content_hash(doc), canonical(doc)))

    def _apply(self, op_id: str, doc: dict) -> int:
        with Tx(self.conn):
            row = self.conn.execute(
                "SELECT op_seq, payload_hash FROM se_ops WHERE op_id=?", (op_id,)).fetchone()
            h = content_hash(doc)
            if row is not None:
                if row["payload_hash"] != h:
                    raise SeatingProtocolError(
                        f"op_id {op_id} replayed with different content")
                return row["op_seq"]
            seq = self._next_op_seq()
            self._insert_op(op_id, seq, doc)
            return seq

    @staticmethod
    def _name(op_id, exam_id, student):
        if not (isinstance(exam_id, str) and exam_id and isinstance(student, str) and student):
            raise SeatingError("exam_id/student must be non-empty strings")

    def register(self, op_id: str, exam_id: str, student: str) -> int:
        self._name(op_id, exam_id, student)
        return self._apply(op_id, {"op": "register", "exam_id": exam_id, "student": student})

    def withdraw(self, op_id: str, exam_id: str, student: str) -> int:
        self._name(op_id, exam_id, student)
        return self._apply(op_id, {"op": "withdraw", "exam_id": exam_id, "student": student})

    def set_need(self, op_id: str, exam_id: str, student: str,
                 accessible: bool = False, aisle: bool = False) -> int:
        self._name(op_id, exam_id, student)
        if not (isinstance(accessible, bool) and isinstance(aisle, bool)):
            raise SeatingError("need flags must be bool")
        return self._apply(op_id, {"op": "set_need", "exam_id": exam_id,
                                   "student": student, "accessible": accessible,
                                   "aisle": aisle})

    def add_conflict(self, op_id: str, exam_id: str, a: str, b: str) -> int:
        self._name(op_id, a, b)
        if not isinstance(exam_id, str) or not exam_id:
            raise SeatingError("exam_id must be a non-empty string")
        if a == b:
            raise SeatingError("self conflict edges are illegal")
        return self._apply(op_id, {"op": "add_conflict", "exam_id": exam_id, "a": a, "b": b})

    def remove_conflict(self, op_id: str, exam_id: str, a: str, b: str) -> int:
        self._name(op_id, a, b)
        return self._apply(op_id, {"op": "remove_conflict", "exam_id": exam_id,
                                   "a": a, "b": b})

    def registration_head(self) -> int:
        row = self.conn.execute("SELECT COALESCE(MAX(op_seq),0) AS s FROM se_ops").fetchone()
        return row["s"]

    def _load_ops(self, head: int | None) -> list[dict]:
        if head is None:
            head = self.registration_head()
        if not is_int(head) or head < 0:
            raise SeatingError("registration_head must be a non-negative int")
        rows = self.conn.execute(
            "SELECT doc_json FROM se_ops WHERE op_seq <= ? ORDER BY op_seq", (head,)).fetchall()
        max_seq = self.registration_head()
        if head > max_seq:
            raise SeatingError(f"registration_head {head} ahead of current head {max_seq}")
        return [json.loads(r["doc_json"]) for r in rows]

    def snapshot(self, head: int | None = None) -> dict:
        ops = self._load_ops(head)
        registered, needs, conflicts = replay_facts(ops)
        return {"head": head if head is not None else self.registration_head(),
                "ops": ops, "registered": registered, "needs": needs, "conflicts": conflicts}

    # ------------------------------------------------------------------ #
    # current product binding
    # ------------------------------------------------------------------ #
    def set_current_product(self, exam_id: str, product_id: str) -> None:
        prow = self.conn.execute(
            "SELECT 1 FROM gb_products WHERE product_id=?", (product_id,)).fetchone()
        if prow is None:
            raise SeatingProtocolError(f"unknown grade product {product_id}")
        with Tx(self.conn):
            self.conn.execute(
                "INSERT INTO se_exam_product(exam_id, product_id) VALUES(?,?) "
                "ON CONFLICT(exam_id) DO UPDATE SET product_id=excluded.product_id",
                (exam_id, product_id))

    def current_product(self, exam_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT product_id FROM se_exam_product WHERE exam_id=?", (exam_id,)).fetchone()
        return row["product_id"] if row else None

    def _load_product(self, product_id: str) -> tuple[dict, str]:
        row = self.conn.execute(
            "SELECT doc_json, payload_hash FROM gb_products WHERE product_id=?",
            (product_id,)).fetchone()
        if row is None:
            raise SeatingProtocolError(
                f"grade product {product_id} does not exist; plans must read a real "
                "issued product")
        return json.loads(row["doc_json"]), row["payload_hash"]

    # ------------------------------------------------------------------ #
    # planning
    # ------------------------------------------------------------------ #
    def plan(self, exam_id: str, grade_product: str,
             registration_head: int | None = None,
             room_version: str | None = None,
             expected_eligibility: list[str] | None = None) -> dict:
        if room_version is None:
            room_version = self.latest_room_version()
        if room_version is None:
            raise SeatingProtocolError("no room version available")
        room_doc = self.get_rooms(room_version)
        product, product_hash = self._load_product(grade_product)

        snap = self.snapshot(registration_head)
        head = snap["head"]
        registered, needs, conflicts = (snap["registered"], snap["needs"], snap["conflicts"])

        eligibility = {s["student"]: s["eligible"] for s in product["students"]}
        reasons = {s["student"]: s["eligibility_reasons"] for s in product["students"]}
        eligible_set = sorted(s for s, ok in eligibility.items() if ok)
        if expected_eligibility is not None:
            if sorted(expected_eligibility) != eligible_set:
                raise SeatingProtocolError(
                    "stale eligibility supplied at consumption: caller expects "
                    f"{sorted(expected_eligibility)} but product {grade_product} says "
                    f"{eligible_set}")

        registrants = sorted(st for (ex, st), active in registered.items()
                             if ex == exam_id and active)
        qualified_registrants = [s for s in registrants if eligibility.get(s, False)]
        unqualified = []
        for s in registrants:
            if s not in eligibility:
                why = ["student absent from grade product " + grade_product]
            elif not eligibility[s]:
                why = reasons[s]
            else:
                continue
            unqualified.append({"student": s, "eligibility_reasons": why, "seated": False})

        exam_conflicts = [list(sorted(edge)) for (ex, edge) in conflicts if ex == exam_id]
        seats = [s for room in room_doc["rooms"] for s in room["seats"]]
        need_map = {st: _student_need(needs, exam_id, st) for st in qualified_registrants}
        result = solve(qualified_registrants, seats, need_map, exam_conflicts)

        plan_doc = {
            "kind": "seating_plan",
            "exam_id": exam_id,
            "product_id": grade_product,
            "product_hash": product_hash,
            "registration_head": head,
            "room_version": room_version,
            "registrants": registrants,
            "eligible_students": eligible_set,
            "qualified_registrants": qualified_registrants,
            "unqualified_registrants": unqualified,
            "needs": {st: need_map[st] for st in sorted(need_map)},
            "conflicts": exam_conflicts,
            "rooms_input": room_doc["rooms"],
            "status": "feasible" if result["feasible"] else "infeasible",
            "rooms_used": result.get("rooms"),
            "room_count": result["rooms_used"],
            "assignment": result["assignment"],
            "seat_order": ([result["assignment"][st] for st in qualified_registrants]
                           if result["feasible"] else None),
            "infeasible_reason": result["reason"],
            "search_stats": {"nodes": result["nodes"],
                             "room_sets_tried": result["room_sets_tried"]},
        }
        plan_id = "sp-" + content_hash(plan_doc)
        plan_doc["plan_id"] = plan_id
        with Tx(self.conn):
            self.conn.execute(
                "INSERT OR IGNORE INTO se_plans(plan_id, exam_id, doc_json, payload_hash, "
                "created_op) VALUES(?,?,?,?,?)",
                (plan_id, exam_id, canonical(plan_doc), content_hash(plan_doc), head))
            return plan_doc

    def get_plan(self, plan_id: str) -> dict:
        row = self.conn.execute(
            "SELECT doc_json FROM se_plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            raise SeatingProtocolError(f"unknown plan {plan_id}")
        return json.loads(row["doc_json"])

    def list_plans(self, exam_id: str | None = None) -> list[dict]:
        if exam_id is None:
            rows = self.conn.execute("SELECT doc_json FROM se_plans ORDER BY plan_id").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT doc_json FROM se_plans WHERE exam_id=? ORDER BY plan_id",
                (exam_id,)).fetchall()
        return [json.loads(r["doc_json"]) for r in rows]

    # ------------------------------------------------------------------ #
    # confirmation
    # ------------------------------------------------------------------ #
    def confirm(self, plan_id: str, request_id: str, payload: dict | None = None) -> dict:
        if not (isinstance(request_id, str) and request_id):
            raise SeatingError("request_id must be a non-empty string")
        payload = payload or {}
        replay_key = canonical({"plan_id": plan_id, "request_id": request_id,
                                "payload": payload})
        h = content_hash(replay_key)
        with Tx(self.conn):
            prior = self.conn.execute(
                "SELECT doc_json, payload_hash FROM se_confirms WHERE request_id=?",
                (request_id,)).fetchone()
            if prior is not None:
                if prior["payload_hash"] != h:
                    raise SeatingProtocolError(
                        f"request_id {request_id} reused with different content")
                stored = json.loads(prior["doc_json"])
                if stored["status"] == "rejected":
                    # the first (rejected) result is part of immutable history:
                    # identical replay must return the same rejection, committed
                    return stored  # caller can inspect status/reason directly
                return stored

            plan = self.get_plan(plan_id)
            failure = None
            if plan["status"] != "feasible":
                failure = "plan is infeasible and cannot be confirmed"
            head_now = self.registration_head()
            if failure is None and head_now != plan["registration_head"]:
                failure = (f"registration/needs/conflicts head moved "
                           f"{plan['registration_head']} -> {head_now}")
            if failure is None:
                room_row = self.conn.execute(
                    "SELECT 1 FROM se_room_versions WHERE room_version=?",
                    (plan["room_version"],)).fetchone()
                if room_row is None:
                    failure = f"room version {plan['room_version']} no longer exists"
            if failure is None:
                cur = self.current_product(plan["exam_id"])
                if cur is None:
                    failure = "exam has no current grade product binding"
                elif cur != plan["product_id"]:
                    failure = (f"current product for exam is {cur}, plan was built on "
                               f"{plan['product_id']}")
                else:
                    prod_row = self.conn.execute(
                        "SELECT payload_hash FROM gb_products WHERE product_id=?",
                        (cur,)).fetchone()
                    if prod_row is None or prod_row["payload_hash"] != plan["product_hash"]:
                        failure = "bound grade product content hash no longer matches"
            doc = {
                "request_id": request_id,
                "plan_id": plan_id,
                "status": "rejected" if failure else "confirmed",
                "reason": failure,
                "checked_head": head_now,
                "plan_head": plan["registration_head"],
                "current_product": self.current_product(plan["exam_id"]),
            }
            self.conn.execute(
                "INSERT INTO se_confirms(request_id, plan_id, payload_hash, doc_json) "
                "VALUES(?,?,?,?)", (request_id, plan_id, h, canonical(doc)))
        # transaction committed before raising so rejection records are durable
        if failure:
            raise ConfirmRejected(failure, doc)
        return doc

    def get_confirm(self, request_id: str) -> dict:
        row = self.conn.execute(
            "SELECT doc_json FROM se_confirms WHERE request_id=?", (request_id,)).fetchone()
        if row is None:
            raise SeatingProtocolError(f"unknown confirm request {request_id}")
        return json.loads(row["doc_json"])


def open_seater(path: str) -> Seater:
    from .store import connect
    return Seater(connect(path))
