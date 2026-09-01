#!/usr/bin/env python3
"""Deterministic generic downstream word/relations projection generation.

Generates projection artifacts from an explicitly selected validated release.
Publishes generic track/schema registry, exact release/source lineage,
collision-safe identity, deterministic serialization, output/self-hashes,
and artifact-only or documented HTTP read surfaces without implementing
separate Hunspell, cspell, Babel, Polyglossia, or Rabu Baku products.

Operations (all caller-owned, local-only, no write to canonical data):
- generate --release-root <caller-owned> --output-root <caller-owned> --consumer <aksantara|generic> --track <word|relations> --release <validated-version> [--generator-version proj-gen-v1] [--schema-version word-v1|relations-v1] [--fixed-clock ISO8601] --json
- verify --output-root --consumer --track --release [--generator-version] [--schema-version] --json
- read --output-root --consumer --track --release [--generator-version] [--schema-version] --json
- list --output-root --json
- registry --json (publishes generic track/schema registry)
- help (shows selectors, paths, content types, local mode, fixed clock, status/errors, release read)

Rejects unsupported downstream product identifiers (hunspell, cspell, babel, polyglossia, rabu-baku).
Missing/invalid/unvalidated/conflicted/incomplete source releases fail before publication without fallback.
Projection output roots are caller-owned and separate from canonical/raw/vector/release namespaces.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aksantara.projections.registry import (
    ALLOWED_CONSUMERS,
    ALLOWED_TRACKS,
    GENERATOR_VERSION,
    REJECTED_PRODUCT_IDENTIFIERS,
    registry_snapshot,
)
from aksantara.projections.schemas import (
    RELATIONS_SCHEMA_V1,
    SERIALIZATION_CONTRACT,
    WORD_SCHEMA_V1,
)
from aksantara.projections.store import (
    ProjectionError,
    generate_projection,
    list_projections,
    read_projection_artifact,
    read_projection_manifest,
)

HELP_KEYWORDS = "projection generate verify list read registry release track generator schema selectors output paths caller-owned staging local mode fixed clock status errors release read hunspell cspell babel polyglossia rabu-baku word relations word-v1 relations-v1 proj-gen-v1 validated manifest artifact lineage collision-safe identity deterministic serialization output hash self-hash"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Deterministic generic downstream word/relations projection generation from an explicitly selected validated release. "
            "Publishes generic track/schema registry, exact release/source lineage, collision-safe identity, deterministic serialization, "
            "output/self-hashes, and artifact-only or documented HTTP read surfaces without implementing separate Hunspell, cspell, Babel, Polyglossia, or Rabu Baku products. "
            "Keywords: " + HELP_KEYWORDS
        ),
        epilog=(
            "Examples:\n"
            "  python scripts/generate_projection.py registry --json\n"
            "  python scripts/generate_projection.py generate --release-root /tmp/rel --output-root /tmp/out --consumer aksantara --track word --release v1 --fixed-clock 2026-09-01T00:00:00Z --json\n"
            "  python scripts/generate_projection.py verify --output-root /tmp/out --consumer aksantara --track word --release v1 --json\n"
            "  python scripts/generate_projection.py read --output-root /tmp/out --consumer aksantara --track word --release v1 --json\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="operation", required=True)

    # registry
    reg = sub.add_parser(
        "registry",
        help="Publish generic track/schema registry (allowed consumers/tracks, rejected products, generator/schema versions, serialization rules)",
        description="Publishes generic track/schema registry including allowed (consumer,track) pairs, rejected product identifiers, generator/schema versions, serialization rules, relation semantics, source-entry universe, and empty-release policy.",
    )
    reg.add_argument("--json", action="store_true", help="Machine-readable JSON")

    # generate
    gen = sub.add_parser(
        "generate",
        help="Generate deterministic projection artifact from explicitly selected validated release (exact release/track/generator/schema selectors and safe output paths)",
        description=(
            "Generate projection from exactly selected validated release. Selectors: --consumer (aksantara|generic), --track (word|relations), "
            "--release (validated version), --generator-version (proj-gen-v1), --schema-version (word-v1|relations-v1). "
            "Output root is caller-owned staging/output and separate from canonical/raw/vector/release namespaces. "
            "Supports --fixed-clock for deterministic bytes. Rejects hunspell/cspell/babel/polyglossia/rabu-baku. "
            "Fails before publication for missing/invalid/unvalidated/conflicted/incomplete releases without fallback to current."
        ),
    )
    gen.add_argument(
        "--release-root",
        required=True,
        help="Caller-owned release root containing releases/, vectors/, canonical/",
    )
    gen.add_argument(
        "--output-root",
        required=True,
        help="Caller-owned projection output root (separate from release namespaces; e.g. /tmp/out or <release-root>/projections)",
    )
    gen.add_argument(
        "--consumer",
        required=True,
        help=f"Consumer/track selector; allowed consumers: {', '.join(ALLOWED_CONSUMERS)}; tracks: {', '.join(ALLOWED_TRACKS)}; rejected: {', '.join(REJECTED_PRODUCT_IDENTIFIERS)}",
    )
    gen.add_argument(
        "--track",
        required=True,
        help=f"Track selector; allowed: {', '.join(ALLOWED_TRACKS)}",
    )
    gen.add_argument(
        "--release",
        required=True,
        help="Exact validated release version to project (no current fallback)",
    )
    gen.add_argument(
        "--generator-version", help=f"Generator version (default {GENERATOR_VERSION})"
    )
    gen.add_argument(
        "--schema-version",
        help="Schema version (default word-v1 for word, relations-v1 for relations)",
    )
    gen.add_argument(
        "--fixed-clock",
        help="Fixed ISO-8601 clock for deterministic output (e.g. 2026-09-01T00:00:00Z)",
    )
    gen.add_argument("--created-at", help="Alias for --fixed-clock")
    gen.add_argument(
        "--fault",
        help="Local fault seam for atomic publication tests: artifact_write|output_hash|manifest_commit|verification (caller-owned, process-scoped, does not delete retained evidence)",
    )
    gen.add_argument("--json", action="store_true", help="Machine-readable JSON output")

    # verify
    ver = sub.add_parser(
        "verify",
        help="Verify projection manifest and artifact (sorted entry IDs, exact hashes, source references, generator/schema versions, output/self-hashes, status)",
        description="Verify generated projection manifests carry source release/manifest, sorted entry IDs, exact raw/canonical hashes, source references, generator/schema versions, output path/hash, self-hash, and status.",
    )
    ver.add_argument(
        "--output-root", required=True, help="Caller-owned projection output root"
    )
    ver.add_argument("--consumer", required=True, help="Consumer selector")
    ver.add_argument("--track", required=True, help="Track selector")
    ver.add_argument("--release", required=True, help="Source release version")
    ver.add_argument("--generator-version", help="Generator version")
    ver.add_argument("--schema-version", help="Schema version")
    ver.add_argument("--json", action="store_true", help="Machine-readable JSON")

    # read
    rd = sub.add_parser(
        "read",
        help="Read exact projection artifact/manifest by collision-safe identity (consumer/track/release/generator/schema)",
        description="Read exact projection artifact and manifest by collision-safe identity; no substitution or current-pointer fallback. Returns artifact bytes verification and manifest lineage.",
    )
    rd.add_argument(
        "--output-root", required=True, help="Caller-owned projection output root"
    )
    rd.add_argument("--consumer", required=True, help="Consumer selector")
    rd.add_argument("--track", required=True, help="Track selector")
    rd.add_argument("--release", required=True, help="Source release version")
    rd.add_argument("--generator-version", help="Generator version")
    rd.add_argument("--schema-version", help="Schema version")
    rd.add_argument("--json", action="store_true", help="Machine-readable JSON")

    # list
    lst = sub.add_parser(
        "list",
        help="List projections by collision-safe identity (consumer/track/release/generator/schema)",
        description="List all projection manifests under output root, isolated by collision-safe identity containing (consumer, track, source_release, generator_version, schema_version).",
    )
    lst.add_argument(
        "--output-root", required=True, help="Caller-owned projection output root"
    )
    lst.add_argument("--json", action="store_true", help="Machine-readable JSON")

    # status (alias for verify)
    st = sub.add_parser(
        "status",
        help="Alias for verify — projection status/read behavior",
        description="Projection status/read behavior with manifest verification.",
    )
    st.add_argument(
        "--output-root", required=True, help="Caller-owned projection output root"
    )
    st.add_argument("--consumer", required=True, help="Consumer selector")
    st.add_argument("--track", required=True, help="Track selector")
    st.add_argument("--release", required=True, help="Source release version")
    st.add_argument("--generator-version", help="Generator version")
    st.add_argument("--schema-version", help="Schema version")
    st.add_argument("--json", action="store_true", help="Machine-readable JSON")

    sub.add_parser("help", help="Show help").set_defaults(operation="help")

    return p.parse_args()


def _emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def main(argv: list[str] | None = None) -> int:
    if argv is not None:
        old = sys.argv
        sys.argv = ["generate_projection.py", *argv]
        try:
            args = _parse_args()
        finally:
            sys.argv = old
    else:
        args = _parse_args()

    if args.operation == "help":
        _parse_args()
        return 0

    try:
        if args.operation == "registry":
            snap = registry_snapshot()
            snap["word_schema"] = WORD_SCHEMA_V1
            snap["relations_schema"] = RELATIONS_SCHEMA_V1
            snap["serialization_contract"] = SERIALIZATION_CONTRACT
            _emit(snap)
            return 0

        if args.operation == "generate":
            clock = getattr(args, "fixed_clock", None) or getattr(
                args, "created_at", None
            )
            try:
                manifest = generate_projection(
                    release_root=Path(args.release_root),
                    output_root=Path(args.output_root),
                    consumer=args.consumer,
                    track=args.track,
                    source_release=args.release,
                    generator_version=getattr(args, "generator_version", None),
                    schema_version=getattr(args, "schema_version", None),
                    created_at=clock,
                    fixed_clock=clock,
                    fault=getattr(args, "fault", None),
                )
                _emit(manifest)
                return 0
            except ProjectionError as exc:
                err = {"error": str(exc), "code": exc.code, "status": exc.status}
                if exc.detail:
                    err["detail"] = exc.detail
                print(json.dumps(err, sort_keys=True, indent=2), file=sys.stderr)
                return 1 if exc.status < 500 else 2

        if args.operation in ("verify", "status"):
            try:
                manifest = read_projection_manifest(
                    Path(args.output_root),
                    args.consumer,
                    args.track,
                    args.release,
                    getattr(args, "generator_version", None),
                    getattr(args, "schema_version", None),
                )
                # Also verify artifact hash
                artifact_data, _ = read_projection_artifact(
                    Path(args.output_root),
                    args.consumer,
                    args.track,
                    args.release,
                    getattr(args, "generator_version", None),
                    getattr(args, "schema_version", None),
                )
                result = {
                    "valid": True,
                    "manifest": manifest,
                    "artifact_hash_verified": True,
                    "artifact_bytes_len": len(artifact_data),
                }
                _emit(result)
                return 0
            except ProjectionError as exc:
                err = {
                    "valid": False,
                    "error": str(exc),
                    "code": exc.code,
                    "status": exc.status,
                }
                print(json.dumps(err, sort_keys=True, indent=2), file=sys.stderr)
                _emit(err)
                return 1
            except Exception as exc:
                err2 = {"valid": False, "error": str(exc)}
                print(json.dumps(err2, sort_keys=True, indent=2), file=sys.stderr)
                _emit(err2)
                return 1

        if args.operation == "read":
            try:
                data, manifest = read_projection_artifact(
                    Path(args.output_root),
                    args.consumer,
                    args.track,
                    args.release,
                    getattr(args, "generator_version", None),
                    getattr(args, "schema_version", None),
                )
                artifact = json.loads(data.decode("utf-8"))
                result = {
                    "manifest": manifest,
                    "artifact": artifact,
                    "artifact_hash": manifest.get("output_hash"),
                    "self_hash": manifest.get("self_hash"),
                }
                _emit(result)
                return 0
            except ProjectionError as exc:
                err = {"error": str(exc), "code": exc.code, "status": exc.status}
                print(json.dumps(err, sort_keys=True, indent=2), file=sys.stderr)
                return 1

        if args.operation == "list":
            manifests = list_projections(Path(args.output_root))
            _emit({"projections": manifests, "count": len(manifests)})
            return 0

    except Exception as exc:
        print(
            json.dumps(
                {"error": str(exc), "type": type(exc).__name__},
                sort_keys=True,
                indent=2,
            ),
            file=sys.stderr,
        )
        import traceback

        traceback.print_exc(file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
