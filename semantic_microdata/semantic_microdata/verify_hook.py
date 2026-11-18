"""Optional lazy hook to the independent verifier.

The verifier lives outside the package (``verify/reference_check.py``) so it
cannot accidentally reuse the publisher's grouping/selection code.  This
module merely exposes it for callers who already have a material path and a
read-only database, without making the package depend on the ``verify``
directory at import time.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

_VERIFIER = Path(__file__).resolve().parents[1] / "verify" / "reference_check.py"


def _load() -> ModuleType:
    if not _VERIFIER.exists():
        raise FileNotFoundError(f"independent verifier not found at {_VERIFIER}")
    spec = importlib.util.spec_from_file_location(
        "semantic_microdata_reference_check", _VERIFIER
    )
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError("cannot load the independent verifier")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def independent_check(
    material_path: str | Path,
    db_path: str | Path,
    release_id: str,
    expected_k: int | None = None,
) -> dict[str, Any]:
    """Run :func:`verify.reference_check.check` without importing its package."""

    return _load().check(
        Path(material_path), Path(db_path), release_id, expected_k
    )
