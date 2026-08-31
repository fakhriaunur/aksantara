#!/usr/bin/env python3
"""Run the bounded deterministic KBBI checkpoint in local fixture mode.

Examples:

    python scripts/checkpoint.py contract --json
    python scripts/checkpoint.py preflight --root /tmp/run --catalog catalog.json
    python scripts/checkpoint.py run --root /tmp/run --catalog catalog.json \
        --limit 100 --idempotency-key checkpoint-demo --json
    python scripts/checkpoint.py status --root /tmp/run --run-id checkpoint-...

The catalog is caller-owned JSON.  Successful entries bind ``stable_key``,
``source_ref``, and a fixture ``transport`` with a relative path or immutable
base64 bytes.  Local mode performs no live network, GCP, emulator, or release
promotion work.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aksantara.ingest.checkpoint import (  # noqa: E402
    CheckpointDriver,
    CheckpointError,
    CheckpointNotFoundError,
)
from aksantara.validate.review import ReviewError  # noqa: E402


def _common_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--root",
        help="Caller-owned root for the catalog, raw snapshots, and run artifacts",
    )
    common.add_argument(
        "--catalog",
        help="Caller-owned JSON catalog/fixture manifest path under --root",
    )
    common.add_argument(
        "--limit",
        help="Integer 1..100 inclusive; default 100; values above 100 reject",
    )
    common.add_argument(
        "--idempotency-key",
        help="Caller idempotency key, scoped to root and the complete run tuple",
    )
    common.add_argument("--run-id", help="Durable checkpoint run reference")
    common.add_argument("--review-id", help="Durable authority review reference")
    common.add_argument(
        "--decision",
        choices=("select_official", "block", "reject"),
        help="Explicit authority review decision",
    )
    common.add_argument("--reviewer", help="Human reviewer identity")
    common.add_argument("--reason", help="Human review reason")
    common.add_argument("--policy-version", help="Authority policy pin")
    common.add_argument(
        "--timestamp",
        help="Optional fixed ISO-8601 decision timestamp",
    )
    common.add_argument(
        "--release-approved",
        action="store_true",
        help="Explicit release-level human approval for candidate evaluation",
    )
    common.add_argument("--release-reviewer", help="Human release approver identity")
    common.add_argument("--release-reason", help="Release approval reason")
    common.add_argument(
        "--json",
        action="store_true",
        help="Emit one machine-readable JSON object instead of human text",
    )
    return common


def _parser() -> argparse.ArgumentParser:
    common = _common_parser()
    parser = argparse.ArgumentParser(
        description=(
            "Deterministic 100-key checkpoint driver. "
            "Keys are Unicode NFKC/casefold normalized, sorted, and bounded "
            "to an integer limit in 1..100. Catalog fingerprints hash sorted "
            "stable keys plus SourceRef identity. Local mode accepts only "
            "caller-owned fixture manifests under the caller root; it never "
            "uses live network/GCP/emulator or promotes a release. "
            "See the contract operation for lifecycle, idempotency, outcomes, "
            "roots, pins, and error mappings."
        ),
        parents=[common],
    )
    operations = parser.add_subparsers(dest="operation", required=True)
    for name, help_text in (
        ("contract", "Print the complete machine-readable contract"),
        ("preflight", "Validate catalog and print stable selection without reads"),
        ("run", "Create, durably execute, and report a local checkpoint"),
        ("status", "Read a durable run status"),
        ("report", "Read a conserved durable run report"),
        ("outcomes", "Read one current outcome per selected key"),
        ("attempts", "Read per-source attempt history"),
        ("history", "Read immutable checkpoint run history"),
        ("checkpoint", "Read the durable checkpoint revision"),
        ("execute", "Read an existing run as an idempotent no-op"),
        ("review-queue", "Read the deterministic open authority review queue"),
        ("review-read", "Read one authority review record"),
        ("review-decision", "Append one explicit authority review decision"),
        ("candidate-evaluate", "Evaluate fail-closed checkpoint candidate eligibility"),
        ("candidate-read", "Read checkpoint candidate eligibility"),
    ):
        operations.add_parser(
            name,
            parents=[common],
            help=help_text,
            description=help_text,
        )
    return parser


def _load_catalog(path_value: str, root: Path) -> dict[str, Any]:
    path = Path(path_value)
    if not path.is_absolute():
        path = root / path
    try:
        resolved = path.resolve()
        resolved.relative_to(root)
    except ValueError as exc:
        raise CheckpointError(
            "catalog path escapes caller root",
            details={"catalog": path_value, "root": str(root)},
        ) from exc
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CheckpointError(
            "catalog file was not found",
            details={"catalog": path_value},
        ) from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CheckpointError(
            "catalog file is not valid UTF-8 JSON",
            details={"catalog": path_value, "error_type": type(exc).__name__},
        ) from exc
    if not isinstance(value, dict):
        raise CheckpointError("catalog file must contain a JSON object")
    return value


def _emit(value: Any, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))
        return
    if isinstance(value, dict):
        print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print(value)


def _operation(args: argparse.Namespace) -> Any:
    operation = str(args.operation)
    if operation == "contract":
        return CheckpointDriver.contract()
    if not args.root:
        raise CheckpointError(
            "--root is required for checkpoint lifecycle operations",
            details={"operation": operation},
        )
    driver = CheckpointDriver(root=Path(args.root).expanduser().resolve())
    if operation in {"preflight", "run"}:
        if not args.catalog:
            raise CheckpointError(
                "--catalog is required for this operation",
                details={"operation": operation},
            )
        catalog = _load_catalog(args.catalog, driver.root)
        if operation == "preflight":
            return driver.preflight(catalog, limit=args.limit).to_dict()
        return driver.run(
            catalog,
            limit=args.limit,
            idempotency_key=args.idempotency_key,
        ).to_dict()
    if operation == "review-queue":
        reviews = driver.review_queue()
        return {
            "schema_version": "authority-review-v1",
            "count": len(reviews),
            "reviews": reviews,
        }
    if operation == "history":
        return driver.history()
    if operation == "review-read":
        if not args.review_id:
            raise CheckpointNotFoundError(
                "--review-id is required for this operation",
                details={"operation": operation},
            )
        return driver.review_read(args.review_id)
    if operation == "review-decision":
        if not args.review_id:
            raise CheckpointNotFoundError(
                "--review-id is required for this operation",
                details={"operation": operation},
            )
        if (
            not args.decision
            or not args.reviewer
            or not args.reason
            or not args.policy_version
        ):
            raise CheckpointError(
                "--decision, --reviewer, --reason, and --policy-version are required",
                details={"operation": operation},
            )
        return driver.review_decide(
            args.review_id,
            decision=args.decision,
            reviewer=args.reviewer,
            reason=args.reason,
            policy_version=args.policy_version,
            idempotency_key=args.idempotency_key,
            timestamp=args.timestamp,
        )
    if operation == "candidate-evaluate":
        if not args.run_id:
            raise CheckpointNotFoundError(
                "--run-id is required for this operation",
                details={"operation": operation},
            )
        return driver.evaluate_candidate(
            args.run_id,
            release_approved=args.release_approved,
            release_reviewer=args.release_reviewer,
            release_reason=args.release_reason,
        )
    if operation == "candidate-read":
        if not args.run_id:
            raise CheckpointNotFoundError(
                "--run-id is required for this operation",
                details={"operation": operation},
            )
        return driver.candidate_evaluation(args.run_id)
    if not args.run_id:
        raise CheckpointNotFoundError(
            "--run-id is required for this operation",
            details={"operation": operation},
        )
    if operation == "status":
        return driver.status(args.run_id)
    if operation == "report":
        return driver.report(args.run_id)
    if operation == "outcomes":
        return driver.current_outcomes(args.run_id)
    if operation == "attempts":
        return driver.attempts(args.run_id)
    if operation == "checkpoint":
        return driver.checkpoint(args.run_id)
    if operation == "execute":
        return driver.execute(args.run_id).to_dict()
    raise CheckpointError(
        "unsupported checkpoint operation",
        details={"operation": operation},
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        value = _operation(args)
    except CheckpointError as exc:
        error = {"error": exc.to_dict()}
        if args.json:
            _emit(error, as_json=True)
        else:
            print(f"ERROR [{exc.code}]: {exc.message}", file=sys.stderr)
            if exc.details:
                print(json.dumps(exc.details, sort_keys=True), file=sys.stderr)
        return 1 if exc.status_code >= 500 else 2
    except ReviewError as exc:
        status_code = (
            409 if type(exc).__name__ == "ReviewDecisionConflictError" else 422
        )
        error = {
            "error": {
                "code": (
                    "review_decision_conflict"
                    if status_code == 409
                    else "invalid_review_request"
                ),
                "message": str(exc),
            }
        }
        if args.json:
            _emit(error, as_json=True)
        else:
            print(f"ERROR [{error['error']['code']}]: {exc}", file=sys.stderr)
        return 2
    _emit(value, as_json=args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
