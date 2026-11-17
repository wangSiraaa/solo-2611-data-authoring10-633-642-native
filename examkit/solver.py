"""Seat assignment solver.

Finite complete search (no external solver dependency), two phases:

1. *feasibility* — exact DFS with MRV variable order, conflict-aware seat
   blocking and Hall's-condition pruning over room subsets tried from one room
   upward, so the minimum feasible room count is found;
2. *lexicographic optimum* — on subsets of that minimum size, a second DFS
   expands students in sorted order and seats in seat_id order; the first
   complete assignment is the lexicographically smallest student->seat list.

Adjacency: same room and Manhattan distance 1.  Diagonals do not count and
unavailable seats never block distance.
"""

from __future__ import annotations

from itertools import combinations

MAX_ROWS = 3
MAX_COLS = 4


class SeatingError(ValueError):
    pass


def manhattan(a: dict, b: dict) -> int:
    return abs(a["row"] - b["row"]) + abs(a["col"] - b["col"])


def validate_rooms(rooms_doc: dict) -> dict:
    if not isinstance(rooms_doc, dict) or not isinstance(rooms_doc.get("rooms"), list):
        raise SeatingError("room doc needs a 'rooms' list")
    if not rooms_doc["rooms"]:
        raise SeatingError("at least one room required")
    seen_seats: set[str] = set()
    norm_rooms = []
    for room in rooms_doc["rooms"]:
        rid = room.get("id")
        if not (isinstance(rid, str) and rid):
            raise SeatingError("room id must be a non-empty string")
        if any(r["id"] == rid for r in norm_rooms):
            raise SeatingError(f"duplicate room id {rid}")
        seats = room.get("seats", [])
        if not isinstance(seats, list) or not seats:
            raise SeatingError(f"room {rid} needs seats")
        norm_seats = []
        coords = set()
        for s in seats:
            sid = s.get("seat_id")
            if not (isinstance(sid, str) and sid):
                raise SeatingError("seat_id must be a non-empty string")
            if sid in seen_seats:
                raise SeatingError(f"seat_id {sid} not unique across the exam")
            r, c = s.get("row"), s.get("col")
            if not (isinstance(r, int) and not isinstance(r, bool)
                    and 0 <= r < MAX_ROWS):
                raise SeatingError(f"seat {sid} row must be 0..{MAX_ROWS - 1}")
            if not (isinstance(c, int) and not isinstance(c, bool)
                    and 0 <= c < MAX_COLS):
                raise SeatingError(f"seat {sid} col must be 0..{MAX_COLS - 1}")
            if (rid, r, c) in coords:
                raise SeatingError(f"duplicate coordinate in room {rid}: ({r},{c})")
            coords.add((rid, r, c))
            available = s.get("available", True)
            if not isinstance(available, bool):
                raise SeatingError(f"seat {sid} available must be bool")
            accessible = s.get("accessible", False)
            aisle = s.get("aisle", False)
            if not (isinstance(accessible, bool) and isinstance(aisle, bool)):
                raise SeatingError(f"seat {sid} accessible/aisle must be bool")
            seen_seats.add(sid)
            norm_seats.append({
                "seat_id": sid, "room": rid, "row": r, "col": c,
                "available": available, "accessible": accessible, "aisle": aisle,
            })
        norm_rooms.append({"id": rid, "seats": norm_seats})
    return {"rooms": norm_rooms}


def _adjacency(seats: list[dict]) -> dict[str, set[str]]:
    adj = {s["seat_id"]: set() for s in seats}
    for i, a in enumerate(seats):
        for b in seats[i + 1:]:
            if a["room"] == b["room"] and manhattan(a, b) == 1:
                adj[a["seat_id"]].add(b["seat_id"])
                adj[b["seat_id"]].add(a["seat_id"])
    return adj


def _bin_pack(items: int, capacities: list[int]) -> bool:
    """Can ``items`` unit balls be placed in bins of given capacities?"""
    if items > sum(capacities):
        return False
    bins = [0] * len(capacities)

    def rec(left):
        if left == 0:
            return True
        seen = set()
        for i in range(len(bins)):
            if bins[i] in seen or bins[i] >= capacities[i]:
                continue
            seen.add(bins[i])
            bins[i] += 1
            if rec(left - 1):
                return True
            bins[i] -= 1
        return False

    return rec(items)


def greedy_first_available(students: list[str], seats: list[dict],
                           needs: dict, conflicts: list[tuple]) -> dict | None:
    """Reference 'first available seat in name order' greedy (may fail).

    Students are processed in sorted order; each takes the first seat (seat_id
    order) that is free, satisfies needs and is not adjacent to a conflict
    partner already placed.  It is intentionally not a solver: a ``None`` result
    says nothing about feasibility.
    """
    adj = _adjacency(seats)
    occupied: set[str] = set()
    seated: dict[str, str] = {}
    cset = {frozenset(p) for p in conflicts}
    assignment: dict[str, str] = {}
    for student in sorted(students):
        need = needs.get(student, {})
        placed = False
        for seat in seats:
            sid = seat["seat_id"]
            if sid in occupied:
                continue
            if need.get("accessible") and not seat["accessible"]:
                continue
            if need.get("aisle") and not seat["aisle"]:
                continue
            if any(frozenset((student, other)) in cset
                   and other in seated and seated[other] in adj[sid]
                   for other in students):
                continue
            assignment[student] = sid
            occupied.add(sid)
            seated[student] = sid
            placed = True
            break
        if not placed:
            return None
    return assignment


def solve(students: list[str], seats: list[dict], needs: dict,
          conflicts: list[tuple]) -> dict:
    """Return ``{feasible, rooms_used, rooms, assignment, reason, ...stats}``.

    ``assignment`` maps student -> seat_id for the lexicographically smallest
    minimum-room plan when feasible.
    """
    students = sorted(students)
    available = [s for s in seats if s["available"]]
    stats = {"nodes": 0, "room_sets_tried": 0}
    if len(available) < len(students):
        return {"feasible": False, "rooms_used": None, "assignment": None,
                "reason": f"only {len(available)} available seats for "
                          f"{len(students)} students", **stats}

    room_of = {s["seat_id"]: s["room"] for s in available}
    rooms = sorted({s["room"] for s in available})
    cset = {frozenset(p) for p in conflicts if p[0] in students and p[1] in students}
    partners = {st: set() for st in students}
    for edge in cset:
        a, b = tuple(edge)
        partners[a].add(b)
        partners[b].add(a)

    def base_candidates(subset_seats):
        local_cand = {}
        for st in students:
            need = needs.get(st, {})
            local_cand[st] = sorted(
                s["seat_id"] for s in subset_seats
                if (not need.get("accessible") or s["accessible"])
                and (not need.get("aisle") or s["aisle"]))
        return local_cand

    def legal_seats(st, local_cand, occupied, blocked):
        return [sid for sid in local_cand[st]
                if sid not in occupied and sid not in blocked.get(st, set())]

    def hall_ok(remaining, local_cand, occupied, blocked):
        """Hall's condition on the residual availability graph (exact).

        Full subset enumeration is cubic only for small residual sets; larger
        sets use the singleton test plus :func:`conflict_capacity_ok`, which
        catches clique-style impossibility in linear time.
        """
        rem = list(remaining)
        if any(not legal_seats(st, local_cand, occupied, blocked) for st in rem):
            return False
        if len(rem) > 8:
            return conflict_capacity_ok(rem, local_cand, occupied, blocked)
        for r in range(1, len(rem) + 1):
            for combo in combinations(rem, r):
                union = set()
                for st in combo:
                    union.update(legal_seats(st, local_cand, occupied, blocked))
                if len(union) < r:
                    return False
        return True

    def conflict_capacity_ok(rem, local_cand, occupied, blocked):
        """Linear necessary bound: per room, mutually conflicting unplaced
        students need distinct, mutually non-adjacent seats.  We approximate
        exactly the tight clique check on every conflict-connected group."""
        rem_set = set(rem)
        seen = set()
        for st in rem:
            if st in seen:
                continue
            # BFS the conflict component restricted to unplaced students
            comp = []
            stack = [st]
            while stack:
                cur = stack.pop()
                if cur in seen:
                    continue
                seen.add(cur)
                comp.append(cur)
                stack.extend(p for p in partners[cur] if p in rem_set and p not in seen)
            # seats legally open to ANY member, grouped by room
            by_room: dict[str, set[str]] = {}
            for member in comp:
                for sid in legal_seats(member, local_cand, occupied, blocked):
                    by_room.setdefault(room_of[sid], set()).add(sid)
            # a clique of size k needs at least k seats pairwise non-adjacent;
            # cheapest sound bound: total open seats across rooms >= component
            if sum(len(v) for v in by_room.values()) < len(comp):
                return False
        return True

    def block_for(student, sid, local_adj, assign, blocked):
        added = []
        for other in partners[student]:
            if other in assign:
                continue
            for nb in local_adj[sid]:
                if nb not in blocked.setdefault(other, set()):
                    blocked[other].add(nb)
                    added.append((other, nb))
        return added

    def feasible_fast(subset_seats):
        """Feasibility-only exact DFS (MRV order; no assignment returned)."""
        local_adj = _adjacency(subset_seats)
        local_cand = base_candidates(subset_seats)
        if any(not c for c in local_cand.values()):
            return False

        def dfs(occupied, assign, blocked):
            stats["nodes"] += 1
            remaining = [st for st in students if st not in assign]
            if not remaining:
                return True
            counts = [(len(legal_seats(st, local_cand, occupied, blocked)), st)
                      for st in remaining]
            if min(c[0] for c in counts) == 0:
                return False
            _, student = min(counts)  # MRV, ties broken by student name
            for sid in local_cand[student]:
                if sid in occupied or sid in blocked.get(student, set()):
                    continue
                added = block_for(student, sid, local_adj, assign, blocked)
                occupied.add(sid)
                assign[student] = sid
                still = (st for st in students if st not in assign)
                if hall_ok(still, local_cand, occupied, blocked) and dfs(
                        occupied, assign, blocked):
                    return True
                del assign[student]
                occupied.discard(sid)
                for other, nb in added:
                    blocked[other].discard(nb)
            return False

        return dfs(set(), {}, {})

    def lex_smallest(subset_seats):
        """Lexicographically smallest assignment for one fixed seat set."""
        local_adj = _adjacency(subset_seats)
        local_cand = base_candidates(subset_seats)

        def dfs(idx, occupied, assign, blocked):
            stats["nodes"] += 1
            if idx == len(students):
                return dict(assign)
            student = students[idx]
            for sid in local_cand[student]:
                if sid in occupied or sid in blocked.get(student, set()):
                    continue
                added = block_for(student, sid, local_adj, assign, blocked)
                occupied.add(sid)
                assign[student] = sid
                still = (st for st in students if st not in assign)
                if hall_ok(still, local_cand, occupied, blocked):
                    res = dfs(idx + 1, occupied, assign, blocked)
                    if res is not None:
                        return res
                del assign[student]
                occupied.discard(sid)
                for other, nb in added:
                    blocked[other].discard(nb)
            return None

        return dfs(0, set(), {}, {})

    def room_independent_capacities(subset_seats):
        """Exact maximum number of pairwise non-adjacent seats per room.

        Rooms hold at most 3x4=12 seats, so the maximum independent set of the
        induced adjacency graph is found by subset enumeration (checkerboard
        colouring is only exact on a full grid and overestimates sparse ones).
        """
        adj = _adjacency(subset_seats)
        by_room: dict[str, list[str]] = {}
        for s in subset_seats:
            by_room.setdefault(s["room"], []).append(s["seat_id"])
        caps = {}
        for rid, sids in by_room.items():
            best = 0
            for r in range(1, len(sids) + 1):
                for combo in combinations(sids, r):
                    if all(b not in adj[a] for i, a in enumerate(combo)
                           for b in combo[i + 1:]):
                        best = r
                        break  # any independent set of size r suffices
            caps[rid] = best
        return caps

    def clique_prune_possible(subset_seats):
        """Every conflict clique needs its members spread so that no two share
        an adjacency.  For each clique and each room, at most the room's
        independent-set capacity members fit; sum over rooms must cover it."""
        caps = room_independent_capacities(subset_seats)
        # enumerate cliques from the present edge set (bounded by 12 students)
        adj_students = {st: set() for st in students}
        for edge in cset:
            a, b = tuple(edge)
            adj_students[a].add(b)
            adj_students[b].add(a)
        # only maximal cliques matter; Bron-Kerbosch without pivot is fine at 12
        maximal = []

        def bron(r, p, x):
            if not p and not x:
                maximal.append(set(r))
                return
            for v in sorted(p):
                bron(r | {v}, p & adj_students[v], x & adj_students[v])
                p = p - {v}
                x = x | {v}

        bron(set(), set(students), set())
        total_cap = sum(caps.values())
        cap_values = sorted(caps.values(), reverse=True)
        for clique in maximal:
            k = len(clique)
            if k > total_cap:
                return False
            # can k mutually conflicting members be split over the rooms so
            # that no room exceeds its independent-set capacity?
            if not _bin_pack(k, cap_values):
                return False
        return True

    if not students:
        return {"feasible": True, "rooms_used": 0, "rooms": [],
                "assignment": {}, "reason": "empty eligible set", **stats}

    # phase 1: minimum feasible room count
    min_k = None
    for k in range(1, len(rooms) + 1):
        for room_subset in combinations(rooms, k):
            stats["room_sets_tried"] += 1
            subset_seats = [s for s in available if s["room"] in set(room_subset)]
            if len(subset_seats) < len(students):
                continue
            if not clique_prune_possible(subset_seats):
                continue
            if feasible_fast(subset_seats):
                min_k = k
                break
        if min_k is not None:
            break

    if min_k is None:
        diag = []
        for st in students:
            need = needs.get(st, {})
            if not any((not need.get("accessible") or s["accessible"])
                       and (not need.get("aisle") or s["aisle"])
                       for s in available):
                diag.append(f"{st}: no seat satisfies accessibility/aisle needs")
        return {"feasible": False, "rooms_used": None, "rooms": None,
                "assignment": None,
                "reason": "exhaustive search over all room subsets found no "
                          "legal assignment"
                          + ("; " + "; ".join(diag) if diag else ""),
                **stats}

    # phase 2: global lex minimum among all minimum-size room subsets
    best = None
    for room_subset in combinations(rooms, min_k):
        subset_seats = [s for s in available if s["room"] in set(room_subset)]
        if len(subset_seats) < len(students):
            continue
        res = lex_smallest(subset_seats)
        if res is not None:
            rooms_used = sorted({room_of[sid] for sid in res.values()})
            key = [res[st] for st in students]
            if best is None or key < best[0]:
                best = (key, res, rooms_used)
    _, assignment, rooms_used = best
    return {"feasible": True, "rooms_used": len(rooms_used),
            "rooms": rooms_used, "assignment": assignment,
            "reason": None, **stats}
