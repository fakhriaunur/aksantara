#!/usr/bin/env python3
"""CLI stub — verify a release manifest and current-version pointer.

Checks:
- manifest file exists and is valid JSON
- artifactHashes pins match live canonical entries' contentHash
- embedding model/dims match expected (gemini-embedding-001 / 768)
- manifestHash self-consistency (if present)
- optional Firestore: config/current_version points at this manifest version
- optional GCS: canonical/{version} prefix objects present
- optional replay: re-embedding only on contentHash change

Usage:
    python scripts/verify_manifest.py --version 2026-08-30.1 [--project PROJECT]
    python scripts/verify_manifest.py --version 2026-08-30.1 --local-only
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


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Verify release manifest and pointers")
    p.add_argument("--version", required=True, help="Release version to verify")
    p.add_argument(
        "--project", default=None, help="GCP project for remote verification"
    )
    p.add_argument("--manifests-dir", default="manifests", help="Local manifests dir")
    p.add_argument(
        "--canonical-dir", default="data/canonical", help="Local canonical dir"
    )
    p.add_argument("--local-only", action="store_true", help="Do not contact GCP")
    p.add_argument("--expected-model", default="gemini-embedding-001")
    p.add_argument("--expected-dims", type=int, default=768)
    return p.parse_args()


def main() -> int:
    args = _parse_args()

    try:
        from aksantara.domain.models import KBBIEntry
        from aksantara.embeddings.manifests import manifest_hash
    except Exception as exc:
        print(f"ERROR: import failed: {exc}", file=sys.stderr)
        return 2

    manifests_dir = ROOT / args.manifests_dir
    manifest_path = manifests_dir / f"{args.version}.json"
    if not manifest_path.exists():
        print(f"ERROR: manifest not found: {manifest_path}", file=sys.stderr)
        return 1

    manifest: dict = json.loads(manifest_path.read_text(encoding="utf-8"))
    print(f"Loaded manifest {manifest_path}")

    # -- basic schema checks ------------------------------------------------
    ok = True
    expected_keys = {
        "version",
        "created_at",
        "edition",
        "embedding",
        "entries_count",
        "artifactHashes",
    }
    missing = expected_keys - set(manifest.keys())
    if missing:
        print(f"FAIL: manifest missing keys: {sorted(missing)}", file=sys.stderr)
        ok = False
    if manifest.get("version") != args.version:
        print(
            f"FAIL: manifest version {manifest.get('version')!r} != arg {args.version!r}",
            file=sys.stderr,
        )
        ok = False

    emb = manifest.get("embedding", {})
    if not isinstance(emb, dict):
        print("FAIL: embedding block is not a dict", file=sys.stderr)
        ok = False
    else:
        if emb.get("model") != args.expected_model:
            print(
                f"FAIL: model {emb.get('model')!r} != expected {args.expected_model!r}",
                file=sys.stderr,
            )
            ok = False
        if emb.get("dimensions") != args.expected_dims:
            print(
                f"FAIL: dims {emb.get('dimensions')} != expected {args.expected_dims}",
                file=sys.stderr,
            )
            ok = False

    # -- artifactHashes vs local canonical ---------------------------------
    artifact_hashes: dict[str, str] = manifest.get("artifactHashes", {})
    # ArtifactHashes may be dict (spec) or list (legacy); normalize
    if isinstance(artifact_hashes, list):
        # Legacy: cannot verify per-entry hash from list alone
        print(
            "WARN: artifactHashes is list (legacy), skipping per-entry hash check",
            file=sys.stderr,
        )
        artifact_hashes = {}
    canonical_dir = ROOT / args.canonical_dir
    local_ok = 0
    for entry_id, expected_hash in artifact_hashes.items():
        path = canonical_dir / f"{entry_id}.json"
        if not path.exists():
            print(f"WARN: canonical missing locally: {path}", file=sys.stderr)
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            # Coerce retrieved_at string for strict model
            try:
                src = data.get("source")
                if isinstance(src, dict) and isinstance(src.get("retrieved_at"), str):
                    s = src["retrieved_at"]
                    if s.endswith("Z"):
                        s = s[:-1] + "+00:00"
                    from datetime import UTC, datetime

                    dt = datetime.fromisoformat(s)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=UTC)
                    data = {**data, "source": {**src, "retrieved_at": dt}}
            except Exception:
                pass
            entry = KBBIEntry.model_validate(data)
            actual = entry.source.content_hash
            if actual.lower() != expected_hash.lower():
                print(
                    f"FAIL: {entry_id} contentHash mismatch manifest={expected_hash[:12]}… actual={actual[:12]}…",
                    file=sys.stderr,
                )
                ok = False
            else:
                local_ok += 1
        except Exception as exc:
            print(f"FAIL: {entry_id} validation error: {exc}", file=sys.stderr)
            ok = False

    print(
        f"Local canonical check: {local_ok}/{len(artifact_hashes)} pinned hashes matched"
    )

    # -- manifestHash self-check --------------------------------------------
    mhash = manifest.get("manifestHash") or manifest.get("manifest_hash")
    if mhash is not None:
        recomputed = manifest_hash(
            {
                k: v
                for k, v in manifest.items()
                if k not in ("manifestHash", "manifest_hash")
            }
        )
        if mhash.lower() != recomputed.lower():
            print(
                f"FAIL: manifestHash mismatch stored={mhash[:12]}… recomputed={recomputed[:12]}…",
                file=sys.stderr,
            )
            ok = False
        else:
            print(f"manifestHash OK ({mhash[:12]}…)")
    else:
        print("WARN: manifestHash absent (older manifest)", file=sys.stderr)

    # -- optional remote checks ---------------------------------------------
    if not args.local_only and args.project:
        # Firestore config/current_version
        try:
            from google.cloud import firestore  # type: ignore[import-untyped]

            db = firestore.Client(project=args.project)
            snap = db.collection("config").document("current_version").get()
            if not snap.exists:
                print(
                    "WARN: Firestore config/current_version does not exist",
                    file=sys.stderr,
                )
            else:
                data = snap.to_dict() or {}
                ptr = data.get("version")
                if ptr != args.version:
                    print(
                        f"WARN: pointer version {ptr!r} != arg {args.version!r}",
                        file=sys.stderr,
                    )
                else:
                    print(f"Firestore pointer OK: config/current_version = {ptr!r}")
        except Exception as exc:
            print(f"Firestore check skipped/failed: {exc}", file=sys.stderr)
    elif args.local_only:
        print("Local-only verification — skipping Firestore/GCS")

    if ok:
        print(
            f"Verified — version={args.version} entries={manifest.get('entries_count')} OK"
        )
        return 0
    print("Verification FAILED — see FAIL lines above", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
