"""Finite value domains and generalisation hierarchies fixed by the material.

Nothing here is inferred from the data at publication time: the domains and the
per-leaf ancestor lists come from ``case.json`` (or any caller supplied
material with the same shape).  A hierarchy is a *forest of chains*: each leaf
value has exactly one ancestor per level, level 0 is the raw value and the top
level is always ``*``.  This makes "parent levels never split an existing
group" true by construction: if two leaves agree at level ``l`` they agree at
every ``l' >= l``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from .errors import DomainViolation, InvalidArgument

QUASI_COLUMNS: tuple[str, str, str] = ("age", "district", "job")
SENSITIVE_COLUMN = "sensitive"
TOP = "*"


@dataclass(frozen=True)
class Hierarchy:
    """Ancestor chain for every leaf of one quasi-identifier column."""

    column: str
    chains: Mapping[str, tuple[str, ...]]  # leaf -> ancestors[0..max]
    max_level: int

    def __post_init__(self) -> None:
        if not self.chains:
            raise InvalidArgument(f"hierarchy {self.column!r} is empty")
        expected = self.max_level
        for leaf, chain in self.chains.items():
            if not chain:
                raise InvalidArgument(f"leaf {leaf!r} in {self.column!r} has no chain")
            if chain[0] != leaf:
                raise InvalidArgument(
                    f"leaf {leaf!r} in {self.column!r} must be its own level 0"
                )
            if chain[-1] != TOP:
                raise InvalidArgument(
                    f"leaf {leaf!r} in {self.column!r} must reach {TOP!r} at top level"
                )
            if len(chain) != expected + 1:
                raise InvalidArgument(
                    f"leaf {leaf!r} in {self.column!r} has {len(chain)} levels, "
                    f"expected {expected + 1}"
                )
        if expected < 0:
            raise InvalidArgument("max_level must be >= 0")
        # Parent levels may only merge groups, never split them: if two leaves
        # agree at level l they must agree at level l+1.  This makes each level
        # a genuine coarsening of the one below (a tree over the leaves).
        for level in range(expected):
            parent_of: dict[str, str] = {}
            for leaf, chain in self.chains.items():
                current = chain[level]
                nxt = chain[level + 1]
                previous = parent_of.setdefault(current, nxt)
                if previous != nxt:
                    raise InvalidArgument(
                        f"hierarchy {self.column!r} splits a level-{level} group "
                        f"{current!r}: leaves disagree on level {level + 1}"
                    )

    @property
    def leaves(self) -> tuple[str, ...]:
        return tuple(self.chains)

    def generalize(self, leaf: str, level: int) -> str:
        if leaf not in self.chains:
            raise DomainViolation(f"{leaf!r} is not a value of column {self.column!r}")
        if not 0 <= level <= self.max_level:
            raise InvalidArgument(f"level {level} out of range for {self.column!r}")
        return self.chains[leaf][level]

    def as_dict(self) -> dict[str, object]:
        return {
            "column": self.column,
            "max_level": self.max_level,
            "chains": {leaf: list(chain) for leaf, chain in self.chains.items()},
        }


@dataclass(frozen=True)
class Domains:
    """Whole finite domain: three quasi columns plus the sensitive column."""

    hierarchies: Mapping[str, Hierarchy]
    sensitive_values: tuple[str, ...]

    def __post_init__(self) -> None:
        for column in QUASI_COLUMNS:
            if column not in self.hierarchies:
                raise InvalidArgument(f"missing hierarchy for {column!r}")
        if not self.sensitive_values:
            raise InvalidArgument("sensitive domain must not be empty")
        if len(set(self.sensitive_values)) != len(self.sensitive_values):
            raise InvalidArgument("sensitive domain must contain distinct values")

    @property
    def max_levels(self) -> tuple[int, int, int]:
        return tuple(self.hierarchies[c].max_level for c in QUASI_COLUMNS)  # type: ignore[return-value]

    def check_row(self, row: Mapping[str, str]) -> None:
        for column in QUASI_COLUMNS:
            value = row.get(column)
            if not isinstance(value, str):
                raise DomainViolation(f"column {column!r} must be a string")
            self.hierarchies[column].generalize(value, 0)  # raises if unknown
        sens = row.get(SENSITIVE_COLUMN)
        if not isinstance(sens, str):
            raise DomainViolation(f"column {SENSITIVE_COLUMN!r} must be a string")
        if sens not in self.sensitive_values:
            raise DomainViolation(
                f"sensitive value {sens!r} is not in the fixed domain"
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "quasi_columns": list(QUASI_COLUMNS),
            "sensitive_column": SENSITIVE_COLUMN,
            "hierarchies": {c: self.hierarchies[c].as_dict() for c in QUASI_COLUMNS},
            "sensitive_values": list(self.sensitive_values),
        }


def domains_from_material(material: Mapping[str, object]) -> Domains:
    """Build :class:`Domains` from the ``case.json`` style material mapping."""

    raw_hier = material.get("hierarchies")
    if not isinstance(raw_hier, Mapping):
        raise InvalidArgument("material.hierarchies must be an object")
    hierarchies: dict[str, Hierarchy] = {}
    for column in QUASI_COLUMNS:
        column_raw = raw_hier.get(column)
        if not isinstance(column_raw, Mapping):
            raise InvalidArgument(f"material.hierarchies.{column} must be an object")
        chains: dict[str, tuple[str, ...]] = {}
        level_counts: set[int] = set()
        for leaf, chain_raw in column_raw.items():
            if not isinstance(leaf, str):
                raise DomainViolation(f"{column} leaf must be a string")
            if not isinstance(chain_raw, Sequence) or isinstance(chain_raw, str):
                raise InvalidArgument(f"ancestor list for {column}:{leaf} must be a list")
            chain = tuple(str(item) for item in chain_raw)
            chains[leaf] = chain
            level_counts.add(len(chain))
        if len(level_counts) != 1:
            raise InvalidArgument(
                f"all leaves of {column!r} must have the same number of levels"
            )
        max_level = level_counts.pop() - 1
        hierarchies[column] = Hierarchy(column, chains, max_level)

    # Sensitive domain: explicit list if given, else every sensitive value that
    # occurs in the material rows, keeping first-seen order.
    raw_sens = material.get("sensitive_values")
    if raw_sens is not None:
        if not isinstance(raw_sens, Sequence) or isinstance(raw_sens, str):
            raise InvalidArgument("material.sensitive_values must be a list")
        sensitive = tuple(str(v) for v in raw_sens)
    else:
        seen: dict[str, None] = {}
        rows = material.get("rows")
        if not isinstance(rows, Sequence):
            raise InvalidArgument("material.rows must be a list")
        for raw in rows:
            if isinstance(raw, Mapping):
                value = raw.get(SENSITIVE_COLUMN)
                if isinstance(value, str):
                    seen.setdefault(value, None)
        sensitive = tuple(seen)
    return Domains(hierarchies=hierarchies, sensitive_values=sensitive)


def material_rows(material: Mapping[str, object]) -> list[dict[str, str]]:
    """Validate and normalise the initial row list (no revisions implied)."""

    rows = material.get("rows")
    if not isinstance(rows, Sequence) or isinstance(rows, str):
        raise InvalidArgument("material.rows must be a list")
    out: list[dict[str, str]] = []
    fields = ("subject_id", "age", "district", "job", SENSITIVE_COLUMN)
    for index, raw in enumerate(rows):
        if not isinstance(raw, Mapping):
            raise InvalidArgument(f"row[{index}] must be an object")
        record: dict[str, str] = {}
        for field in fields:
            value = raw.get(field)
            if not isinstance(value, str):
                raise DomainViolation(f"row[{index}].{field} must be a string")
            record[field] = value
        out.append(record)
    return out
