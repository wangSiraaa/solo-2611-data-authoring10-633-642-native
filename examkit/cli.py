"""Command-line entry points.

Examples::

    python3 -m examkit.cli demo                 # rebuild ./data demo end to end
    python3 -m examkit.cli verify data/examkit.db
    python3 -m examkit.cli products data/examkit.db
    python3 -m examkit.cli plans data/examkit.db
"""

from __future__ import annotations

import json
import sys

from .verify import verify_database


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        return 1
    cmd, *args = argv
    if cmd == "demo":
        from .demo import main as demo_main
        demo_main()
        return 0
    if cmd == "verify":
        if len(args) != 1:
            print("usage: verify <database-path>")
            return 2
        report = verify_database(args[0])
        print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
        return 0 if report["passed"] else 1
    if cmd in ("products", "plans"):
        import sqlite3
        if len(args) != 1:
            print(f"usage: {cmd} <database-path>")
            return 2
        conn = sqlite3.connect(args[0])
        conn.row_factory = sqlite3.Row
        table = "gb_products" if cmd == "products" else "se_plans"
        for row in conn.execute(f"SELECT doc_json FROM {table}"):
            print(json.dumps(json.loads(row["doc_json"]),
                             sort_keys=True, ensure_ascii=False))
        return 0
    print(f"unknown command {cmd!r}\n{__doc__}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
