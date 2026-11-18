"""Command-line entry points for the data service and the publisher.

Examples
--------
    # bootstrap the supplied material into a state file
    python3 -m semantic_microdata.cli bootstrap \
        --material ../case.json --db state.db

    # inspect an exact prefix view (private)
    python3 -m semantic_microdata.cli view \
        --material ../case.json --db state.db --k 8 --purpose research

    # draft / approve / release
    python3 -m semantic_microdata.cli draft \
        --material ../case.json --db state.db --request-id r1 --k 9 \
        --purpose research --K 2 --D 2 --weights 1,0,0 --releases releases
    python3 -m semantic_microdata.cli review \
        --material ../case.json --db state.db --draft-id d-... \
        --decision approve --reason ok
    python3 -m semantic_microdata.cli release \
        --material ../case.json --db state.db --draft-id d-... \
        --release-id rel-1 --releases releases

    # run a declarative plan and print the per-step facts
    python3 -m semantic_microdata.cli plan --material ../case.json \
        --plan plan.json --db state.db --releases releases

Errors are printed as JSON ``{code, message, details}`` with exit status 2.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .data_service import DataService
from .errors import ServiceError
from .material import Material, load_material
from .plan import PlanExecutor, load_plan
from .publisher import MicrodataPublisher


def _material(args: argparse.Namespace) -> Material:
    return load_material(args.material)


def _publisher(args: argparse.Namespace) -> tuple[DataService, MicrodataPublisher]:
    service = DataService(args.db, _material(args).domains)
    return service, MicrodataPublisher(service, _material(args).domains, args.releases)


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def cmd_bootstrap(args: argparse.Namespace) -> int:
    material = _material(args)
    service = DataService(args.db, material.domains)
    try:
        _print(
            service.initialise(
                material.rows,
                purpose=args.purpose or material.purpose,
                grants=list(material.initial_grants),
                revoke=material.revoke,
            )
        )
    finally:
        service.close()
    return 0


def cmd_put(args: argparse.Namespace) -> int:
    material = _material(args)
    service = DataService(args.db, material.domains)
    try:
        row = None
        if args.age is not None:
            row = {
                "age": args.age,
                "district": args.district,
                "job": args.job,
                "sensitive": args.sensitive,
            }
        _print(
            service.put(
                args.record_id,
                args.subject_id,
                args.revision,
                row,
                args.retracted,
                op_id=args.op_id,
            )
        )
    finally:
        service.close()
    return 0


def cmd_grant(args: argparse.Namespace) -> int:
    material = _material(args)
    service = DataService(args.db, material.domains)
    try:
        _print(service.grant(args.subject, args.purpose, op_id=args.op_id))
    finally:
        service.close()
    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    material = _material(args)
    service = DataService(args.db, material.domains)
    try:
        _print(service.revoke(args.subject, args.purpose, op_id=args.op_id))
    finally:
        service.close()
    return 0


def cmd_view(args: argparse.Namespace) -> int:
    material = _material(args)
    service = DataService(args.db, material.domains)
    try:
        k = service.last_seq() if args.k is None else args.k
        rows = service.view(k, args.purpose)
        _print(
            {
                "k": k,
                "purpose": args.purpose,
                "rows": [row.private_fields() | row.public_fields() for row in rows],
            }
        )
    finally:
        service.close()
    return 0


def cmd_draft(args: argparse.Namespace) -> int:
    service, publisher = _publisher(args)
    try:
        k = service.last_seq() if args.k is None else args.k
        weights = [int(part) for part in args.weights.split(",")]
        _print(
            publisher.draft(
                args.request_id, k, args.purpose, args.K, args.D, weights
            )
        )
    finally:
        service.close()
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    service, publisher = _publisher(args)
    try:
        _print(publisher.review(args.draft_id, args.decision, args.reason))
    finally:
        service.close()
    return 0


def cmd_release(args: argparse.Namespace) -> int:
    service, publisher = _publisher(args)
    try:
        _print(publisher.release(args.draft_id, args.release_id))
    finally:
        service.close()
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    service, publisher = _publisher(args)
    try:
        _print(publisher.inspect_draft(args.draft_id))
    finally:
        service.close()
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    material = _material(args)
    executor = PlanExecutor(material, args.db, args.releases)
    try:
        results = executor.run(load_plan(args.plan))
    finally:
        executor.close()
    _print({"steps": results, "ok": all(step["ok"] for step in results)})
    return 0 if all(step["ok"] for step in results) else 1


def cmd_history(args: argparse.Namespace) -> int:
    material = _material(args)
    service = DataService(args.db, material.domains)
    try:
        _print(
            {
                "service_head": service.view_head(),
                "global_head": service.global_seq(),
                "operations": service.operation_log(),
            }
        )
    finally:
        service.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="semantic_microdata", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser, *, needs_db: bool = True) -> None:
        p.add_argument("--material", required=True, type=Path)
        if needs_db:
            p.add_argument("--db", required=True, type=Path)

    p = sub.add_parser("bootstrap")
    common(p)
    p.add_argument("--purpose")
    p.set_defaults(func=cmd_bootstrap)

    p = sub.add_parser("put")
    common(p)
    p.add_argument("--record-id", required=True)
    p.add_argument("--subject-id", required=True)
    p.add_argument("--revision", required=True, type=int)
    p.add_argument("--retracted", action="store_true")
    p.add_argument("--op-id")
    p.add_argument("--age")
    p.add_argument("--district")
    p.add_argument("--job")
    p.add_argument("--sensitive")
    p.set_defaults(func=cmd_put)

    for name, func, extra in (
        ("grant", cmd_grant, "grant"),
        ("revoke", cmd_revoke, "revoke"),
    ):
        p = sub.add_parser(name)
        common(p)
        p.add_argument("--subject", required=True)
        p.add_argument("--purpose", required=True)
        p.add_argument("--op-id")
        p.set_defaults(func=func)

    p = sub.add_parser("view")
    common(p)
    p.add_argument("--k", type=int)
    p.add_argument("--purpose", required=True)
    p.set_defaults(func=cmd_view)

    p = sub.add_parser("draft")
    common(p)
    p.add_argument("--releases", required=True, type=Path)
    p.add_argument("--request-id", required=True)
    p.add_argument("--k", type=int)
    p.add_argument("--purpose", required=True)
    p.add_argument("--K", required=True, type=int)
    p.add_argument("--D", required=True, type=int)
    p.add_argument("--weights", required=True)
    p.set_defaults(func=cmd_draft)

    p = sub.add_parser("review")
    common(p)
    p.add_argument("--releases", required=True, type=Path)
    p.add_argument("--draft-id", required=True)
    p.add_argument("--decision", required=True)
    p.add_argument("--reason", required=True)
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("release")
    common(p)
    p.add_argument("--releases", required=True, type=Path)
    p.add_argument("--draft-id", required=True)
    p.add_argument("--release-id", required=True)
    p.set_defaults(func=cmd_release)

    p = sub.add_parser("inspect")
    common(p)
    p.add_argument("--releases", required=True, type=Path)
    p.add_argument("--draft-id", required=True)
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("plan")
    common(p)
    p.add_argument("--releases", required=True, type=Path)
    p.add_argument("--plan", required=True, type=Path)
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("history")
    common(p)
    p.set_defaults(func=cmd_history)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ServiceError as exc:
        print(json.dumps(exc.as_dict(), indent=2, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
