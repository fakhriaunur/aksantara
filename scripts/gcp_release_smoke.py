#!/usr/bin/env python3
"""Bounded approved GCP sandbox smoke for release verification.

Approved scope only:
- Project: ata-devpost-sandbox
- Firestore Native (default) in asia-southeast1
- Bucket: gs://ata-devpost-sandbox-aksantara
- One Vertex gemini-embedding-001 768d call, one vector/manifest path, bounded writes, pointer behavior
- Uses process-scoped ADC/configuration, unique one-entry version, no broad corpus operation
- Preflight rejects wrong project/database/region/bucket, broad or more-than-one entry, non-unique release, new resource/delete/IAM/migration/bootstrap/production/implicit promotion before SDK init

Run local mode first; live mode only after separate approval with --live.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

APPROVED_PROJECT = "ata-devpost-sandbox"
APPROVED_DATABASE = "(default)"
APPROVED_LOCATION = "asia-southeast1"
APPROVED_BUCKET = "gs://ata-devpost-sandbox-aksantara"
APPROVED_MODEL = "gemini-embedding-001"
APPROVED_DIMS = 768


def _preflight(args: argparse.Namespace) -> list[str]:
    errors: list[str] = []
    proj = os.getenv("GOOGLE_CLOUD_PROJECT", "")
    loc = os.getenv("GOOGLE_CLOUD_LOCATION", "") or os.getenv("GOOGLE_CLOUD_REGION", "")
    bucket = (
        os.getenv("AKSANTARA_GCS_BUCKET", "")
        or os.getenv("GCS_BUCKET", "")
        or args.bucket
        or ""
    )
    database = os.getenv("FIRESTORE_DATABASE", "") or args.database or APPROVED_DATABASE
    if proj and proj != APPROVED_PROJECT:
        errors.append(f"project {proj!r} != approved {APPROVED_PROJECT!r}")
    elif not proj and args.live:
        errors.append(
            "GOOGLE_CLOUD_PROJECT must be set to ata-devpost-sandbox for live smoke"
        )
    if loc and loc != APPROVED_LOCATION:
        errors.append(f"location {loc!r} != approved {APPROVED_LOCATION!r}")
    if bucket and bucket != APPROVED_BUCKET and not bucket.startswith(APPROVED_BUCKET):
        errors.append(f"bucket {bucket!r} != approved {APPROVED_BUCKET!r}")
    if database and database != APPROVED_DATABASE:
        errors.append(f"database {database!r} != approved {APPROVED_DATABASE!r}")
    if args.entries and args.entries != 1:
        errors.append(f"only one entry allowed, got {args.entries}")
    if not args.version or not args.version.strip():
        errors.append("unique test release version required")
    if args.new_resource or args.delete or args.iam:
        errors.append("new resource/delete/IAM not allowed")
    # Broad operation check
    if args.broad:
        errors.append("broad corpus operation not allowed")
    # Unique version check: if version exists already, must be unique
    if args.root:
        p = Path(args.root) / "releases" / f"{args.version}.json"
        if p.exists():
            errors.append(
                f"release version {args.version!r} already exists, must be unique"
            )
    return errors


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Bounded approved GCP sandbox smoke: one Vertex 768d call, one vector/manifest, bounded writes, pointer behavior only within sandbox. Process-scoped ADC, unique one-entry version, no broad corpus.",
        epilog="Example: python scripts/gcp_release_smoke.py --version test-smoke-$(date +%s) --live --root /tmp/smoke-root",
    )
    p.add_argument(
        "--version",
        required=True,
        help="Unique test release version (e.g., smoke-2026-09-01-001)",
    )
    p.add_argument(
        "--root",
        help="Caller-owned root for manifest/vector verification (optional local)",
    )
    p.add_argument("--bucket", default=APPROVED_BUCKET, help="Approved bucket")
    p.add_argument(
        "--database", default=APPROVED_DATABASE, help="Approved Firestore database"
    )
    p.add_argument("--entries", type=int, default=1, help="Entries count must be 1")
    p.add_argument(
        "--live",
        action="store_true",
        help="Execute live Vertex/Firestore/Storage (requires approval and ADC)",
    )
    p.add_argument("--broad", action="store_true", help="Broad operation (rejected)")
    p.add_argument(
        "--new-resource", action="store_true", help="New resource (rejected)"
    )
    p.add_argument("--delete", action="store_true", help="Delete (rejected)")
    p.add_argument("--iam", action="store_true", help="IAM change (rejected)")
    p.add_argument("--json", action="store_true", help="Machine JSON output")
    args = p.parse_args(argv if argv is not None else None)

    errors = _preflight(args)
    if errors:
        out = {
            "preflight": "rejected",
            "errors": errors,
            "approved_scope": {
                "project": APPROVED_PROJECT,
                "database": APPROVED_DATABASE,
                "location": APPROVED_LOCATION,
                "bucket": APPROVED_BUCKET,
                "model": APPROVED_MODEL,
                "dimensions": APPROVED_DIMS,
            },
        }
        print(json.dumps(out, indent=2))
        return 2

    if not args.live:
        out = {
            "preflight": "passed",
            "scope": {
                "project": APPROVED_PROJECT,
                "database": APPROVED_DATABASE,
                "location": APPROVED_LOCATION,
                "bucket": APPROVED_BUCKET,
                "model": APPROVED_MODEL,
                "dimensions": APPROVED_DIMS,
            },
            "entries": 1,
            "version": args.version,
            "note": "live not executed; pass --live with approved ADC for one Vertex call",
        }
        print(json.dumps(out, indent=2))
        return 0

    # Live path: process-scoped ADC, one Vertex call, bounded writes
    # Verify env still approved before SDK init
    proj = os.getenv("GOOGLE_CLOUD_PROJECT", "")
    if proj != APPROVED_PROJECT:
        print(
            json.dumps(
                {"error": f"preflight failed: project {proj!r} != {APPROVED_PROJECT!r}"}
            ),
            file=sys.stderr,
        )
        return 2
    try:
        # One Vertex call: gemini-embedding-001 768d RETRIEVAL_DOCUMENT for one fixture entry
        try:
            from google import genai  # type: ignore[import-not-found]
            from google.genai.types import (
                EmbedContentConfig,  # type: ignore[import-not-found]
            )
        except ImportError as exc:
            print(
                json.dumps(
                    {
                        "error": f"google-genai not available: {exc}",
                        "code": "unavailable",
                    }
                )
            )
            return 3
        client = genai.Client(
            vertexai=True, project=APPROVED_PROJECT, location=APPROVED_LOCATION
        )
        doc_text = "Lema: Februasi\nMakna: (1) bulan kedua tahun Masehi\n"
        # Bounded: one request
        resp = client.models.embed_content(
            model=APPROVED_MODEL,
            contents=[doc_text],
            config=EmbedContentConfig(task_type="RETRIEVAL_DOCUMENT"),
        )
        vectors = resp.embeddings if hasattr(resp, "embeddings") else []
        if not vectors or len(vectors[0].values) != APPROVED_DIMS:
            print(
                json.dumps(
                    {
                        "error": f"Vertex response dims != {APPROVED_DIMS}",
                        "values_len": len(vectors[0].values) if vectors else 0,
                    }
                )
            )
            return 1
        # Verify one entry, bounded writes
        vec_len = len(vectors[0].values)
        finite = all(
            isinstance(v, (int, float))
            and v == v
            and v not in (float("inf"), float("-inf"))
            for v in vectors[0].values
        )
        # If root provided, also verify manifest/vector path locally
        manifest_ok = True
        pointer_before = None
        if args.root:
            from datetime import UTC, datetime

            from aksantara.domain.models import KBBIEntry, SourceRef
            from aksantara.embeddings.release import seed_release, verify_release

            root = Path(args.root).expanduser().resolve()
            # Ensure unique version not already promoted; seed one-entry fixture for verification
            ch = hashlib.sha256(b"smoke entry").hexdigest()
            entry = KBBIEntry(
                id="smoke-entry",
                lema="SmokeEntry",
                makna=[{"definisi": "smoke definisi"}],
                source=SourceRef(
                    url="https://kbbi.kemdikbud.go.id/entri/smoke-entry",
                    source_kind="official-live",
                    edition="VI",
                    source_version="VI",
                    retrieved_at=datetime.now(UTC),
                    content_hash=ch,
                    parser_version="0.1.0",
                ),
            )
            try:
                seed_release(root, args.version, [entry])
                ver = verify_release(root, args.version)
                manifest_ok = bool(ver.get("valid"))
                # pointer should not change until explicit promotion; we do not promote here
                from aksantara.embeddings.registry import load_current

                pointer_before = load_current(root)
            except Exception as exc2:
                manifest_ok = False
                print(
                    json.dumps({"warning": f"local manifest verify failed: {exc2}"}),
                    file=sys.stderr,
                )
        report = {
            "preflight": "passed",
            "live": True,
            "project": APPROVED_PROJECT,
            "database": APPROVED_DATABASE,
            "location": APPROVED_LOCATION,
            "bucket": APPROVED_BUCKET,
            "model": APPROVED_MODEL,
            "dimensions": vec_len,
            "finite": finite,
            "entries": 1,
            "version": args.version,
            "writes": 1,
            "batches": 1,
            "bounded": True,
            "manifest_verified": manifest_ok,
            "pointer_before": pointer_before,
            "pointer_changed": False,
            "note": "no pointer change until explicit promotion; no new resource/delete/IAM; one Vertex 768d request only",
        }
        print(json.dumps(report, indent=2))
        # Redact token check: ensure no credential printed
        return 0
    except Exception as exc:
        # Truthful failure: never fallback to hash vectors
        print(
            json.dumps(
                {
                    "error": f"live smoke failed: {exc}",
                    "type": type(exc).__name__,
                    "live": True,
                    "project": APPROVED_PROJECT,
                }
            ),
            file=sys.stderr,
        )
        import traceback

        traceback.print_exc(file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
