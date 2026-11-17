"""Independent quiz gradebook.

Covers one course and up to 12 students.  Everything is exact: proportions use
:class:`~examkit.rational.Rat`, never floats; course scores are emitted as
reduced ``[numerator, positive_denominator]`` pairs.

The gradebook knows nothing about exam registration or seating.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from .rational import Rat, ZERO, is_int
from .store import Tx, canonical, connect, content_hash

MAX_STUDENTS = 12
MAX_CATEGORIES = 4
MAX_ASSESSMENTS_PER_CATEGORY = 6


class GradebookError(ValueError):
    """Base class for all gradebook validation / protocol errors."""


class RuleError(GradebookError):
    pass


class IngestRejected(GradebookError):
    """A whole ingestion batch was rejected; no state changed."""


class IdempotencyConflict(GradebookError):
    """A stable event/request id was reused with different content."""


# --------------------------------------------------------------------------- #
# rule validation
# --------------------------------------------------------------------------- #
def _require(cond, msg):
    if not cond:
        raise RuleError(msg)


def validate_rules(rules: dict) -> dict:
    """Validate a rules document and return a normalised copy."""
    _require(isinstance(rules, dict), "rules must be an object")
    _require(isinstance(rules.get("threshold"), list) and len(rules["threshold"]) == 2,
             "threshold must be [numerator, denominator]")
    threshold = Rat.from_pair(rules["threshold"])
    _require(threshold >= ZERO, "threshold must be non-negative")

    cats = rules.get("categories")
    _require(isinstance(cats, list) and 1 <= len(cats) <= MAX_CATEGORIES,
             f"need 1..{MAX_CATEGORIES} categories")
    weight_sum = ZERO
    seen_aids: set[str] = set()
    norm_cats = []
    for cat in cats:
        _require(isinstance(cat, dict), "category must be an object")
        cid = cat.get("id")
        _require(isinstance(cid, str) and cid, "category id must be a non-empty string")
        _require(isinstance(cat.get("weight"), list) and len(cat["weight"]) == 2,
                 f"category {cid} weight must be [n,d]")
        w = Rat.from_pair(cat["weight"])
        _require(w.n > 0 and w.d > 0, f"category {cid} weight must be positive rational")
        weight_sum = weight_sum + w
        assessments = cat.get("assessments")
        _require(isinstance(assessments, list)
                 and 1 <= len(assessments) <= MAX_ASSESSMENTS_PER_CATEGORY,
                 f"category {cid} needs 1..{MAX_ASSESSMENTS_PER_CATEGORY} assessments")
        norm_assess = []
        for a in assessments:
            _require(isinstance(a, dict), "assessment must be an object")
            aid = a.get("id")
            _require(isinstance(aid, str) and aid, "assessment id must be a non-empty string")
            _require(aid not in seen_aids, f"duplicate assessment_id {aid}")
            seen_aids.add(aid)
            mx = a.get("max")
            _require(is_int(mx) and mx > 0, f"assessment {aid} max must be a positive int")
            mandatory = a.get("mandatory")
            _require(isinstance(mandatory, bool), f"assessment {aid} mandatory must be bool")
            norm_assess.append({"id": aid, "max": mx, "mandatory": mandatory})
        norm_cats.append({"id": cid, "weight": w.to_pair(), "assessments": norm_assess})
    _require(weight_sum == Rat(1), f"category weights must sum to exactly 1, got {weight_sum}")
    return {"threshold": threshold.to_pair(), "categories": norm_cats}


def _rule_index(rules: dict):
    """Maps assessment_id -> (max, mandatory, category_id)."""
    idx = {}
    for cat in rules["categories"]:
        for a in cat["assessments"]:
            idx[a["id"]] = (a["max"], a["mandatory"], cat["id"])
    return idx


# --------------------------------------------------------------------------- #
# pure grading (recomputed from a mirror prefix; no database access)
# --------------------------------------------------------------------------- #
def _attempt_states(mirrors: list[dict]):
    """Collapse revision mirrors per attempt_id into the current state.

    Returns ``{attempt_id: {student, assessment_id, revision, score, void}}``.
    Highest revision wins; a low revision arriving late never resurrects.
    """
    states: dict[str, dict] = {}
    for m in mirrors:
        cur = states.get(m["attempt_id"])
        if cur is None or m["revision"] > cur["revision"]:
            states[m["attempt_id"]] = dict(m)
    return states


def compute_product(rules: dict, students: list[str], mirrors: list[dict],
                    prefix_seq: int, rule_version: str) -> dict:
    """Deterministically build the full grade/eligibility product + explanation."""
    aidx = _rule_index(rules)
    threshold = Rat.from_pair(rules["threshold"])
    states = _attempt_states(mirrors)

    student_docs = []
    for student in sorted(students):
        # current, non-void attempts belonging to this student
        my_attempts = [s for s in states.values() if s["student"] == student]
        # best valid attempt per assessment
        best: dict[str, dict] = {}
        for s in my_attempts:
            if s["void"]:
                continue
            cur = best.get(s["assessment_id"])
            if cur is None:
                best[s["assessment_id"]] = s
                continue
            # exact cross-multiplied proportion comparison (no floats)
            new_s, new_m = s["score"], aidx[s["assessment_id"]][0]
            old_s, old_m = cur["score"], aidx[cur["assessment_id"]][0]
            if new_s * old_m > old_s * new_m or (
                new_s * old_m == old_s * new_m
                and s["attempt_id"] < cur["attempt_id"]
            ):
                best[s["assessment_id"]] = s

        assess_docs = {}
        mandatory_attended = {}
        for aid, (mx, mandatory, _cid) in aidx.items():
            sel = best.get(aid)
            # any non-void current state counts as attendance, even score 0
            attended = any(s["assessment_id"] == aid and not s["void"]
                           for s in my_attempts)
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
                    "ratio": Rat(sel["score"], mx).to_pair(),
                },
                "ratio": (ZERO.to_pair() if sel is None
                          else Rat(sel["score"], mx).to_pair()),
            }

        category_docs = []
        course = ZERO
        for cat in rules["categories"]:
            cid = cat["id"]
            items = [assess_docs[a["id"]] for a in cat["assessments"]]
            non_mand = [it for it in items if not it["mandatory"]]
            dropped = None
            if len(non_mand) >= 2:
                # lowest ratio; tie -> lexicographically largest assessment_id
                worst = non_mand[0]
                for it in non_mand[1:]:
                    wr, ir = Rat.from_pair(worst["ratio"]), Rat.from_pair(it["ratio"])
                    if ir < wr or (ir == wr and it["assessment_id"] > worst["assessment_id"]):
                        worst = it
                dropped = worst["assessment_id"]
                drop_rule = ("at least two non-mandatory items; dropped minimum ratio, "
                             "tie broken by largest assessment_id")
            else:
                drop_rule = "no drop: fewer than two non-mandatory items"
            kept = [it for it in items if it["assessment_id"] != dropped]
            cat_score = ZERO
            for it in kept:
                cat_score = cat_score + Rat.from_pair(it["ratio"])
            cat_score = cat_score / Rat(len(kept))
            course = course + cat_score * Rat.from_pair(cat["weight"])
            category_docs.append({
                "category_id": cid,
                "weight": cat["weight"],
                "items": items,
                "dropped_assessment": dropped,
                "drop_rule": drop_rule,
                "score": cat_score.to_pair(),
            })

        missing_mandatory = [aid for aid, ok in mandatory_attended.items() if not ok]
        reasons = []
        if course < threshold:
            reasons.append(f"course score {course.n}/{course.d} below threshold "
                           f"{threshold.n}/{threshold.d}")
        if missing_mandatory:
            reasons.append("missing valid attempt for mandatory: "
                           + ",".join(sorted(missing_mandatory)))
        eligible = not reasons
        student_docs.append({
            "student": student,
            "eligible": eligible,
            "eligibility_reasons": reasons,
            "course_score": course.to_pair(),
            "threshold": threshold.to_pair(),
            "mandatory_attended": dict(sorted(mandatory_attended.items())),
            "assessments": [assess_docs[k] for k in sorted(assess_docs)],
            "categories": category_docs,
            "attempt_states": sorted(
                ({"attempt_id": s["attempt_id"], "assessment_id": s["assessment_id"],
                  "revision": s["revision"], "score": s["score"], "void": s["void"]}
                 for s in my_attempts),
                key=lambda d: d["attempt_id"]),
        })

    return {
        "kind": "grade_product",
        "rule_version": rule_version,
        "prefix_seq": prefix_seq,
        "threshold": threshold.to_pair(),
        "students": student_docs,
    }


# --------------------------------------------------------------------------- #
# gradebook facade
# --------------------------------------------------------------------------- #
@dataclass
class IngestResult:
    seq: int
    replayed: bool


class Gradebook:
    def __init__(self, conn: sqlite3.Connection, course_id: str = "C"):
        self.conn = conn
        self.course_id = course_id
        self._meta("course_id", course_id)

    # -- small helpers -----------------------------------------------------
    def _meta(self, k, v=None):
        cur = self.conn.execute("SELECT v FROM meta WHERE k=?", (k,))
        row = cur.fetchone()
        if v is None:
            return row["v"] if row else None
        if row is None:
            self.conn.execute("INSERT INTO meta(k,v) VALUES(?,?)", (k, v))
        elif row["v"] != v:
            self.conn.execute("UPDATE meta SET v=? WHERE k=?", (v, k))

    @property
    def rule_version(self) -> str | None:
        return self._meta(f"{self.course_id}:rule_version")

    def latest_seq(self) -> int:
        row = self.conn.execute("SELECT COALESCE(MAX(seq),0) AS s FROM gb_events").fetchone()
        return row["s"]

    def get_rules(self, version: str | None = None) -> dict:
        version = version or self.rule_version
        if version is None:
            raise GradebookError("no rules installed")
        row = self.conn.execute(
            "SELECT rules_json FROM gb_rule_versions WHERE rule_version=?", (version,)).fetchone()
        if row is None:
            raise GradebookError(f"unknown rule version {version}")
        return json.loads(row["rules_json"])

    def get_students(self, version: str | None = None) -> list[str]:
        version = version or self.rule_version
        rows = self.conn.execute(
            "SELECT student FROM gb_students WHERE rule_version=? ORDER BY student",
            (version,)).fetchall()
        return [r["student"] for r in rows]

    # -- rules / students --------------------------------------------------
    def put_rules(self, version: str, rules: dict) -> None:
        if not (isinstance(version, str) and version):
            raise RuleError("rule version must be a non-empty string")
        norm = validate_rules(rules)
        with Tx(self.conn):
            row = self.conn.execute(
                "SELECT rules_json FROM gb_rule_versions WHERE rule_version=?",
                (version,)).fetchone()
            if row is not None:
                if canonical(norm) != canonical(json.loads(row["rules_json"])):
                    raise IdempotencyConflict(
                        f"rule version {version} already exists with different content")
                return
            old_version = self.rule_version
            if old_version is not None:
                # identity sets must remain stable so historical mirrors stay valid
                old = self.get_rules(old_version)
                old_idx = _rule_index(old)
                new_idx = _rule_index(norm)
                if set(old_idx) != set(new_idx) or any(
                        old_idx[k][0] != new_idx[k][0] for k in old_idx):
                    raise RuleError("new rule version must keep assessment ids and max scores")
            self.conn.execute(
                "INSERT INTO gb_rule_versions(rule_version, rules_json, created_seq) "
                "VALUES(?,?,?)", (version, canonical(norm), self.latest_seq()))
            students = self.get_students(old_version) if old_version else []
            self.conn.executemany(
                "INSERT OR IGNORE INTO gb_students(rule_version, student) VALUES(?,?)",
                [(version, s) for s in students])
            self._meta(f"{self.course_id}:rule_version", version)

    def set_students(self, students: list[str]) -> None:
        if self.rule_version is None:
            raise GradebookError("install rules before setting students")
        if not isinstance(students, list) or not students:
            raise GradebookError("students must be a non-empty list")
        if len(students) > MAX_STUDENTS:
            raise GradebookError(f"at most {MAX_STUDENTS} students")
        if any(not (isinstance(s, str) and s) for s in students):
            raise GradebookError("student ids must be non-empty strings")
        if len(set(students)) != len(students):
            raise GradebookError("duplicate student id")
        with Tx(self.conn):
            used = {r["student"] for r in self.conn.execute(
                "SELECT DISTINCT json_extract(m.value, '$.student') AS student "
                "FROM gb_events e, json_each(e.mirror_json) m").fetchall()}
            if used and not used.issubset(set(students)):
                raise GradebookError("cannot remove students already referenced by events")
            self.conn.execute("DELETE FROM gb_students WHERE rule_version=?",
                              (self.rule_version,))
            self.conn.executemany(
                "INSERT INTO gb_students(rule_version, student) VALUES(?,?)",
                [(self.rule_version, s) for s in students])

    # -- ingestion ---------------------------------------------------------
    def _validate_mirror(self, m, aidx, students):
        if not isinstance(m, dict):
            raise IngestRejected("each mirror must be an object")
        for k in ("attempt_id", "student", "assessment_id", "revision", "score", "void"):
            if k not in m:
                raise IngestRejected(f"mirror missing field {k}")
        if not (isinstance(m["attempt_id"], str) and m["attempt_id"]):
            raise IngestRejected("attempt_id must be a non-empty string")
        if m["student"] not in students:
            raise IngestRejected(f"unknown student {m['student']!r}")
        if m["assessment_id"] not in aidx:
            raise IngestRejected(f"unknown assessment {m['assessment_id']!r}")
        if not is_int(m["revision"]) or m["revision"] <= 0:
            raise IngestRejected("revision must be a positive int (bool rejected)")
        if not is_int(m["score"]):
            raise IngestRejected("score must be an int (bool rejected)")
        mx = aidx[m["assessment_id"]][0]
        if not 0 <= m["score"] <= mx:
            raise IngestRejected(
                f"score {m['score']} out of range 0..{mx} for {m['assessment_id']}")
        if not isinstance(m["void"], bool):
            raise IngestRejected("void must be a bool")

    def ingest(self, event_id: str, mirrors: list[dict]) -> IngestResult:
        if not (isinstance(event_id, str) and event_id):
            raise IngestRejected("event_id must be a non-empty string")
        if not isinstance(mirrors, list) or not mirrors:
            raise IngestRejected("ingestion needs a non-empty list of complete mirrors")
        rules = self.get_rules()
        students = self.get_students()
        aidx = _rule_index(rules)
        # full validation first; a single bad mirror rejects the whole batch
        seen_in_batch: dict[tuple, dict] = {}
        batch_ids: dict[str, dict] = {}
        for m in mirrors:
            self._validate_mirror(m, aidx, students)
            key = (m["attempt_id"], m["revision"])
            if key in seen_in_batch and canonical(seen_in_batch[key]) != canonical(m):
                raise IngestRejected(
                    f"same attempt_id/revision {key} with conflicting content in batch")
            seen_in_batch[key] = m
            prev = batch_ids.get(m["attempt_id"])
            if prev is not None and (prev["student"] != m["student"]
                                     or prev["assessment_id"] != m["assessment_id"]):
                raise IngestRejected(
                    f"attempt_id {m['attempt_id']} maps to different student/assessment")
            batch_ids[m["attempt_id"]] = m

        with Tx(self.conn):
            row = self.conn.execute(
                "SELECT seq, payload_hash, mirror_json FROM gb_events WHERE event_id=?",
                (event_id,)).fetchone()
            h = content_hash(mirrors)
            if row is not None:
                if row["payload_hash"] != h:
                    raise IdempotencyConflict(
                        f"event_id {event_id} re-posted with different content")
                return IngestResult(row["seq"], replayed=True)

            # cross-batch revision/identity consistency
            existing = self._load_mirrors_locked()
            ex_by_key = {(e["attempt_id"], e["revision"]): e for e in existing}
            ex_by_attempt = {e["attempt_id"]: e for e in existing}
            for m in mirrors:
                old = ex_by_key.get((m["attempt_id"], m["revision"]))
                if old is not None and canonical(old) != canonical(m):
                    raise IngestRejected(
                        f"revision {m['revision']} of {m['attempt_id']} "
                        "already exists with different content")
                old0 = ex_by_attempt.get(m["attempt_id"])
                if old0 is not None and (old0["student"] != m["student"]
                                         or old0["assessment_id"] != m["assessment_id"]):
                    raise IngestRejected(
                        f"attempt_id {m['attempt_id']} already bound to another identity")
            nxt = self.latest_seq() + 1
            self.conn.execute(
                "INSERT INTO gb_events(event_id, payload_hash, mirror_json, seq) "
                "VALUES(?,?,?,?)", (event_id, h, canonical(mirrors), nxt))
            return IngestResult(nxt, replayed=False)

    def _load_mirrors_locked(self, prefix_seq: int | None = None) -> list[dict]:
        sql = "SELECT mirror_json FROM gb_events"
        params = ()
        if prefix_seq is not None:
            sql += " WHERE seq <= ?"
            params = (prefix_seq,)
        sql += " ORDER BY seq"
        out = []
        for r in self.conn.execute(sql, params).fetchall():
            out.extend(json.loads(r["mirror_json"]))
        return out

    # -- products ----------------------------------------------------------
    def issue_product(self, prefix_seq: int | None = None) -> dict:
        latest = self.latest_seq()
        if prefix_seq is None:
            prefix_seq = latest
        if not is_int(prefix_seq) or not 0 <= prefix_seq <= latest:
            raise GradebookError(f"prefix_seq must be in 0..{latest}")
        version = self.rule_version
        rules = self.get_rules(version)
        students = self.get_students(version)
        with Tx(self.conn):
            mirrors = self._load_mirrors_locked(prefix_seq)
            doc = compute_product(rules, students, mirrors, prefix_seq, version)
            pid = "gp-" + content_hash(doc)
            doc["product_id"] = pid
            self.conn.execute(
                "INSERT OR IGNORE INTO gb_products(product_id, prefix_seq, rule_version, "
                "payload_hash, doc_json) VALUES(?,?,?,?,?)",
                (pid, prefix_seq, version, content_hash(doc), canonical(doc)))
            return doc

    def get_product(self, product_id: str) -> dict:
        row = self.conn.execute(
            "SELECT doc_json FROM gb_products WHERE product_id=?", (product_id,)).fetchone()
        if row is None:
            raise GradebookError(f"unknown product {product_id}")
        return json.loads(row["doc_json"])

    def list_products(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT doc_json FROM gb_products ORDER BY prefix_seq, product_id").fetchall()
        return [json.loads(r["doc_json"]) for r in rows]

    def explain(self, product_id: str) -> dict:
        """The stored product already carries a full, immutable explanation."""
        return self.get_product(product_id)


def open_gradebook(path: str, course_id: str = "C") -> Gradebook:
    return Gradebook(connect(path), course_id)
