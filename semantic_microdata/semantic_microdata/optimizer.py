"""Exhaustive level-vector enumeration and K/D grouping checks.

This module is a pure function over an *exact* private slice: a list of rows
with the four original columns, the inherited hierarchies, and the caller's
K/D/weights.  It has no access to the database and no knowledge of identities
beyond the row values, so it can be reused by tests and by the independent
verifier.

Definitions (the fixed synthetic contract):

* A level vector ``L = (L_age, L_district, L_job)`` applies one level to every
  row of a column.  Levels range over ``0 .. max_level`` per column.
* ``loss(L) = sum over columns of (L_col * weight_col * n_rows)``.
* Grouping key is ``(generalized_age, generalized_district,
  generalized_job)``; ``sensitive`` keeps its raw value (it is *not* a
  quasi-identifier).  Every group must contain at least ``K`` rows and at least
  ``D`` distinct raw sensitive values.
* Choice: minimum loss, then lexicographically smallest ``L``.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from .domains import QUASI_COLUMNS, Domains
from .errors import Infeasible, InvalidArgument
from .util import non_negative_int, positive_int

Level = tuple[int, int, int]


@dataclass(frozen=True)
class Selection:
    levels: Level
    loss: int


@dataclass(frozen=True)
class GroupStat:
    generalized: tuple[str, str, str]
    count: int
    distinct_sensitive: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "generalized": list(self.generalized),
            "count": self.count,
            "distinct_sensitive": list(self.distinct_sensitive),
            "distinct_sensitive_count": len(self.distinct_sensitive),
        }


def validate_params(
    K: object, D: object, weights: object
) -> tuple[int, int, tuple[int, int, int]]:
    try:
        k_int = positive_int(K, "K")
        d_int = positive_int(D, "D")
    except ValueError as exc:
        raise InvalidArgument(str(exc)) from exc
    if not 1 <= d_int <= k_int:
        raise InvalidArgument("require 1 <= D <= K")
    if not isinstance(weights, Sequence) or isinstance(weights, (str, bytes)):
        raise InvalidArgument("weights must be a sequence of 3 integers")
    if len(weights) != 3:
        raise InvalidArgument("weights must contain exactly 3 integers")
    try:
        w = tuple(
            non_negative_int(item, f"weights[{i}]") for i, item in enumerate(weights)
        )
    except ValueError as exc:
        raise InvalidArgument(str(exc)) from exc
    return k_int, d_int, w  # type: ignore[return-value]


def level_vectors(domains: Domains) -> Iterable[Level]:
    ranges = [range(domains.hierarchies[c].max_level + 1) for c in QUASI_COLUMNS]
    # itertools.product over ascending ranges yields lexicographic order.
    yield from itertools.product(*ranges)


def generalize(
    row: Mapping[str, str], levels: Level, domains: Domains
) -> tuple[str, str, str]:
    return tuple(  # type: ignore[return-value]
        domains.hierarchies[column].generalize(row[column], levels[idx])
        for idx, column in enumerate(QUASI_COLUMNS)
    )


def group_stats(
    rows: Sequence[Mapping[str, str]], levels: Level, domains: Domains
) -> list[GroupStat]:
    """Per generalised triple: row count and sorted distinct raw sensitive values.

    Different *occurrences* of the same sensitive value count once in the
    distinct-value test but every row counts toward K.
    """

    buckets: dict[tuple[str, str, str], list[str]] = {}
    for row in rows:
        triple = generalize(row, levels, domains)
        buckets.setdefault(triple, []).append(row["sensitive"])
    return [
        GroupStat(triple, len(values), tuple(sorted(set(values))))
        for triple, values in sorted(buckets.items())
    ]


def is_feasible(
    rows: Sequence[Mapping[str, str]],
    levels: Level,
    domains: Domains,
    K: int,
    D: int,
) -> tuple[bool, list[dict[str, object]]]:
    failures: list[dict[str, object]] = []
    for stat in group_stats(rows, levels, domains):
        if stat.count < K or len(stat.distinct_sensitive) < D:
            failures.append(
                {
                    "generalized": list(stat.generalized),
                    "count": stat.count,
                    "distinct_sensitive": len(stat.distinct_sensitive),
                    "required_count": K,
                    "required_distinct": D,
                }
            )
    return (not failures), failures


def loss_of(levels: Level, n_rows: int, weights: tuple[int, int, int]) -> int:
    """Literal contract: sum(level_i * weight_i * n_rows)."""

    return sum(levels[i] * weights[i] * n_rows for i in range(3))


def normalised_loss(
    levels: Level,
    n_rows: int,
    weights: tuple[int, int, int],
    domains: Domains,
) -> int:
    """Height-aware variant, for material whose columns have unequal heights.

    ``level / max_level`` is expressed with integer arithmetic (multiply the
    whole sum by the least common multiple of column heights).  The default
    selection path uses :func:`loss_of` exactly as specified; this helper is
    provided so callers with unequal heights can compare columns sensibly.
    """

    heights = [domains.hierarchies[c].max_level for c in QUASI_COLUMNS]
    for height in heights:
        if height == 0:
            raise InvalidArgument(
                "normalised loss requires every column to have at least one "
                "generalisation step"
            )
    scale = 1
    for height in heights:
        scale = _lcm(scale, height)
    total = 0
    for index, height in enumerate(heights):
        total += levels[index] * (scale // height) * weights[index] * n_rows
    return total // scale


def _lcm(a: int, b: int) -> int:
    from math import gcd

    return a * b // gcd(a, b)


def optimal_vectors(
    rows: Sequence[Mapping[str, str]],
    domains: Domains,
    K: int,
    D: int,
    weights: tuple[int, int, int],
) -> list[tuple[Level, int]]:
    """All minimum-loss feasible vectors, in lexicographic order.

    Empty input has the single optimal vector ``(0,0,0)`` with loss 0.
    """

    K, D, weights = validate_params(K, D, weights)
    if not rows:
        return [((0, 0, 0), 0)]
    feasible_vectors: list[tuple[int, Level]] = []
    for levels in level_vectors(domains):
        feasible, _ = is_feasible(rows, levels, domains, K, D)
        if feasible:
            feasible_vectors.append((loss_of(levels, len(rows), weights), levels))
    if not feasible_vectors:
        return []
    best = min(loss for loss, _ in feasible_vectors)
    return sorted(
        (levels, loss) for loss, levels in feasible_vectors if loss == best
    )


def select_best(
    rows: Sequence[Mapping[str, str]],
    domains: Domains,
    K: int,
    D: int,
    weights: tuple[int, int, int],
) -> Selection:
    """Minimum loss, then lexicographically smallest level vector."""

    optima = optimal_vectors(rows, domains, K, D, weights)
    if not optima:
        raise Infeasible(
            "no generalisation vector reaches K rows and D distinct sensitive "
            "values in every equivalence group",
            code="INFEASIBLE",
        )
    levels, loss = optima[0]
    return Selection(levels, loss)


def top_levels(domains: Domains) -> Level:
    return tuple(  # type: ignore[return-value]
        domains.hierarchies[c].max_level for c in QUASI_COLUMNS
    )


def infeasibility_report(
    rows: Sequence[Mapping[str, str]],
    domains: Domains,
    K: int,
    D: int,
) -> dict[str, object]:
    """Explain why no vector works, using the most general vector as witness.

    Generalisation only merges groups.  If the fully generalised vector still
    has a group with fewer than K rows or fewer than D distinct sensitive
    values, every less general vector is also infeasible; the reported
    failures are therefore conclusive rather than a single failed attempt.
    """

    top = top_levels(domains)
    _, failures = is_feasible(rows, top, domains, K, D)
    return {
        "code": "INFEASIBLE",
        "message": (
            "no level vector satisfies every group's K-row and D-distinct-"
            "sensitive requirement for this exact slice"
        ),
        "row_count": len(rows),
        "K": K,
        "D": D,
        "distinct_sensitive_values": len({row["sensitive"] for row in rows}),
        "checked_levels": list(top),
        "group_failures": failures,
        "reason": (
            "full generalisation merges all rows sharing a generalised triple; "
            "a group failing here cannot be split by a less general vector"
        ),
    }
