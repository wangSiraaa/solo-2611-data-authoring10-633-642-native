"""Load the fixed material (``case.json``) and expose validated structures."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .domains import Domains, domains_from_material, material_rows
from .errors import InvalidArgument
from .optimizer import validate_params


@dataclass(frozen=True)
class Material:
    path: Path
    raw: Mapping[str, Any]
    domains: Domains
    rows: list[dict[str, str]]
    purpose: str
    initial_grants: tuple[str, ...]
    revoke: str | None
    K: int
    D: int
    weights: tuple[int, int, int]


def load_material(path: str | Path) -> Material:
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise InvalidArgument(f"material file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise InvalidArgument(f"material file is not valid JSON: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise InvalidArgument("material must be a JSON object")

    domains = domains_from_material(raw)
    rows = material_rows(raw)
    for index, row in enumerate(rows):
        try:
            domains.check_row(row)
        except Exception as exc:  # noqa: BLE001 - re-wrap with row context
            raise InvalidArgument(f"row[{index}] outside fixed domains: {exc}") from exc

    subject_ids = [row["subject_id"] for row in rows]
    if len(set(subject_ids)) != len(subject_ids):
        raise InvalidArgument("material rows must have unique subject_id values")

    purpose = raw.get("purpose")
    if not isinstance(purpose, str) or not purpose:
        raise InvalidArgument("material.purpose must be a non-empty string")

    grants_raw = raw.get("initial_grants", [])
    if not isinstance(grants_raw, list) or not all(
        isinstance(item, str) for item in grants_raw
    ):
        raise InvalidArgument("material.initial_grants must be a list of strings")
    known = set(subject_ids)
    for subject in grants_raw:
        if subject not in known:
            raise InvalidArgument(
                f"initial grant references unknown subject {subject!r}"
            )
    if len(set(grants_raw)) != len(grants_raw):
        raise InvalidArgument("initial_grants contains duplicates")

    revoke = raw.get("revoke")
    if revoke is not None:
        if not isinstance(revoke, str) or revoke not in known:
            raise InvalidArgument("material.revoke must name a known subject or be null")

    try:
        K, D, weights = validate_params(raw.get("K"), raw.get("D"), raw.get("weights"))
    except InvalidArgument as exc:
        raise InvalidArgument(f"material K/D/weights invalid: {exc}") from exc

    return Material(
        path=path,
        raw=raw,
        domains=domains,
        rows=rows,
        purpose=purpose,
        initial_grants=tuple(grants_raw),
        revoke=None if revoke is None else str(revoke),
        K=K,
        D=D,
        weights=weights,
    )
