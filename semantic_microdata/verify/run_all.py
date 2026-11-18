"""End-to-end: run every plan against a fresh DB, verify each release with the
independent checker, and check the private/public separation.

Run from the package directory::

    PYTHONPATH=. python3 verify/run_all.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
MATERIAL = ROOT.parent / "case.json"

sys.path.insert(0, str(ROOT))

from semantic_microdata import load_material  # noqa: E402
from semantic_microdata.plan import PlanExecutor  # noqa: E402
from verify.reference_check import check  # noqa: E402


def main() -> int:
    material = load_material(MATERIAL)
    plan_dir = ROOT / "examples" / "plans"
    tmp = Path(tempfile.mkdtemp(prefix="verify-all-"))
    failures: list[str] = []
    records = []
    try:
        for plan_path in sorted(plan_dir.glob("*.json")):
            name = plan_path.stem
            db = tmp / f"{name}.db"
            releases = tmp / f"{name}-releases"
            executor = PlanExecutor(material, db, releases)
            try:
                results = executor.run(json.loads(plan_path.read_text()))
            finally:
                executor.close()
            if not all(step["ok"] for step in results):
                failures.append(f"plan {name} had failing steps")
                records.append({"plan": name, "steps_ok": False})
                continue
            # Verify every released id that the plan produced.
            import sqlite3

            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                release_ids = [
                    row[0] for row in conn.execute("SELECT release_id FROM releases")
                ]
            finally:
                conn.close()
            verified = []
            for release_id in release_ids:
                report = check(MATERIAL, db, release_id)
                verified.append(
                    {"release_id": release_id, "ok": report["ok"],
                     "problems": report["problems"],
                     "k": report["k"], "levels": report["stored_levels"]}
                )
                if not report["ok"]:
                    failures.append(
                        f"plan {name} release {release_id} failed verify: "
                        f"{report['problems']}"
                    )
            records.append(
                {"plan": name, "steps_ok": True, "releases": verified}
            )

        # The `main` plan also demonstrates the private/public split.
        main_db = tmp / "main.db"
        conn = sqlite3.connect(f"file:{main_db}?mode=ro", uri=True)
        try:
            public_path = conn.execute(
                "SELECT public_path FROM releases LIMIT 1"
            ).fetchone()[0]
            private_path = conn.execute(
                "SELECT private_path FROM releases LIMIT 1"
            ).fetchone()[0]
        finally:
            conn.close()
        public_text = Path(public_path).read_text()
        private_text = Path(private_path).read_text()
        split_ok = (
            "subject_id" not in public_text
            and "subject_id" in private_text
            and "version_commit_seq" in private_text
        )
        if not split_ok:
            failures.append("private/public separation broken")
            records.append(
                {"plan": name, "steps_ok": True, "releases": verified}
            )

        # The `main` plan also demonstrates the private/public split.
        main_db = tmp / "main.db"
        conn = sqlite3.connect(f"file:{main_db}?mode=ro", uri=True)
        try:
            public_path = conn.execute(
                "SELECT public_path FROM releases LIMIT 1"
            ).fetchone()[0]
            private_path = conn.execute(
                "SELECT private_path FROM releases LIMIT 1"
            ).fetchone()[0]
        finally:
            conn.close()
        public_text = Path(public_path).read_text()
        private_text = Path(private_path).read_text()
        split_ok = (
            "subject_id" not in public_text
            and "subject_id" in private_text
            and "version_commit_seq" in private_text
        )
        if not split_ok:
            failures.append("private/public separation broken")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    summary = {"records": records, "failures": failures}
    report_path = HERE / "reports" / "run_all.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
