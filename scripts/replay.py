#!/usr/bin/env python3
"""Public deterministic replay for one caller-owned KBBI snapshot.

The command is read-only and hash-first.  It never fetches a URL, repairs
checkpoint state, creates a canonical artifact, or calls an LLM.

Example:

    python scripts/replay.py februari --root /tmp/replay \
      --raw tests/replay/fixtures/februari.html \
      --retrieved-at 2026-08-31T00:00:00Z --source-version VI --json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aksantara.domain.models import SourceRef  # noqa: E402
from aksantara.ingest.public_replay import (  # noqa: E402
    KNOWN_RAW_HASHES,
    TRANSFORM_VERSION,
    VALIDATION_POLICY_VERSION,
    ReplayError,
    replay_snapshot,
)
from aksantara.parse.parser_contract import PARSER_VERSION  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only deterministic replay of caller-owned KBBI bytes. "
            "Raw SHA-256 is verified before parsing; no network, LLM, repair, "
            "canonical, candidate, release, or pointer write is performed."
        )
    )
    parser.add_argument("stable_key", help="Expected stable key, e.g. februari")
    parser.add_argument("--root", required=True, help="Caller-owned root directory")
    parser.add_argument("--raw", required=True, help="Raw snapshot path under --root")
    parser.add_argument(
        "--source-url",
        help="Official source URL (defaults to the KBBI entry URL)",
    )
    parser.add_argument(
        "--source-kind",
        default="official-snapshot",
        choices=("official-live", "official-snapshot"),
    )
    parser.add_argument("--edition", default="VI")
    parser.add_argument("--source-version", default="VI")
    parser.add_argument(
        "--retrieved-at",
        default="1970-01-01T00:00:00Z",
        help="Immutable ISO-8601 retrieval timestamp",
    )
    parser.add_argument("--expected-raw-hash", help="Expected raw SHA-256")
    parser.add_argument(
        "--expected-canonical-hash",
        help="Optional expected canonical-record SHA-256",
    )
    parser.add_argument("--parser-version", default=PARSER_VERSION)
    parser.add_argument("--transform-version", default=TRANSFORM_VERSION)
    parser.add_argument("--validation-policy", default=VALIDATION_POLICY_VERSION)
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    return parser


def _source_ref(args: argparse.Namespace, raw_hash: str) -> SourceRef:
    url = args.source_url or (f"https://kbbi.kemdikbud.go.id/entri/{args.stable_key}")
    try:
        retrieved_at = datetime.fromisoformat(
            str(args.retrieved_at).replace("Z", "+00:00")
        )
        if retrieved_at.tzinfo is None:
            retrieved_at = retrieved_at.replace(tzinfo=UTC)
        return SourceRef(
            url=url,
            source_kind=args.source_kind,
            edition=args.edition,
            source_version=args.source_version,
            retrieved_at=retrieved_at.astimezone(UTC),
            content_hash=raw_hash,
            parser_version=args.parser_version,
        )
    except (TypeError, ValueError) as exc:
        raise ReplayError(
            "replay_source_ref_invalid",
            "source reference arguments are invalid",
            details={"error_type": type(exc).__name__},
        ) from exc


def _operation(args: argparse.Namespace) -> dict[str, object]:
    expected_hash = args.expected_raw_hash or KNOWN_RAW_HASHES.get(
        str(args.stable_key).casefold()
    )
    if expected_hash is None:
        raise ReplayError(
            "replay_expected_hash_required",
            "expected_raw_hash is required for an unpinned stable key",
        )
    source = _source_ref(args, str(expected_hash).lower())
    return replay_snapshot(
        root=Path(args.root),
        raw_path=Path(args.raw),
        source_ref=source,
        expected_raw_hash=str(expected_hash),
        expected_canonical_hash=args.expected_canonical_hash,
        stable_key=args.stable_key,
        parser_version=args.parser_version,
        transform_version=args.transform_version,
        validation_policy=args.validation_policy,
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        value = _operation(args)
    except ReplayError as exc:
        payload = {"error": exc.to_dict()}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            print(f"ERROR [{exc.code}]: {exc.message}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
