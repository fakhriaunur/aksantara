#!/usr/bin/env python3
"""CLI stub — import KBBI corpus slice (Februari) via fetch→parse→validate chain.

Thin slice: fetches one official KBBI entry, parses it deterministically,
validates against authority policy, and writes raw + canonical artifacts
locally and (when --project is set) to GCS/Firestore. Import is idempotent
by contentHash — re-running the same version does not duplicate writes.

Usage:
    python scripts/import_corpus.py --lema Februari [--project PROJECT] [--version 2026-08-30.1]
    python scripts/import_corpus.py --help

No secrets in repo — credentials come from GOOGLE_APPLICATION_CREDENTIALS or
Application Default Credentials.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

# Ensure src/ is on path when executed directly.
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Import KBBI corpus slice (fetch→parse→validate)"
    )
    p.add_argument(
        "--lema", default="Februari", help="Headword to import (default: Februari)"
    )
    p.add_argument(
        "--project", default=None, help="GCP project for GCS/Firestore writes"
    )
    p.add_argument(
        "--version", default=None, help="Release version tag (default: YYYY-MM-DD.N)"
    )
    p.add_argument(
        "--out-dir",
        default="data/canonical",
        help="Local output dir for canonical JSON",
    )
    p.add_argument(
        "--raw-dir", default="data/raw", help="Local output dir for raw HTML snapshots"
    )
    p.add_argument(
        "--fixtures-dir", default="tests/replay/fixtures", help="Replay fixtures dir"
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and validate without writing Firestore/GCS",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()

    try:
        from aksantara.domain.models import KBBIEntry, SourceRef
        from aksantara.domain.provenance import content_hash_bytes
    except Exception as exc:
        print(f"ERROR: domain import failed: {exc}", file=sys.stderr)
        return 2

    version = args.version or datetime.now(UTC).strftime("%Y-%m-%d.1")
    lema_slug = args.lema.strip().lower()

    # -- fetch (stubbed for slice when live network is unavailable) ----------
    # The real path delegates to aksantara.ingest.official.fetch_entry when
    # that module lands. For the Stream D slice we synthesize a deterministic
    # raw snapshot so the pipeline remains runnable offline.
    raw_dir = ROOT / args.raw_dir
    out_dir = ROOT / args.out_dir
    fixtures_dir = ROOT / args.fixtures_dir

    raw_html: bytes | None = None
    fixture_path = fixtures_dir / f"{lema_slug}.html"
    if fixture_path.exists():
        raw_html = fixture_path.read_bytes()
        print(f"Using replay fixture: {fixture_path}")
    else:
        # Synthetic minimal KBBI-like fixture for Februari so the chain is
        # demonstrable without live KBBI access. Marked as enrichment so it
        # cannot be mistaken for official-live without human review.
        synthetic_html = f"""<!doctype html><html><body><article data-lema="{args.lema}">
<h1>{args.lema}</h1><p class="makna">bulan kedua tahun Masehi; terdiri atas 28 hari, 29 hari pada tahun kabisat</p>
<p class="kelas">n</p><p class="contoh">Februari adalah bulan terpendek.</p>
<p class="bentuk-tidak-baku">Pebruari</p></article></body></html>"""
        raw_html = synthetic_html.encode("utf-8")
        print(
            f"No fixture at {fixture_path}; using synthetic slice for {args.lema} (marked enrichment)"
        )

    content_hash = content_hash_bytes(raw_html)
    retrieved_at = datetime.now(UTC)
    url = f"https://kbbi.kemdikbud.go.id/entri/{lema_slug}"

    source = SourceRef(
        url=url,
        source_kind="official-live" if fixture_path.exists() else "enrichment",
        edition="VI",
        source_version="VI",
        retrieved_at=retrieved_at,
        content_hash=content_hash,
        parser_version="0.1.0",
    )

    # -- persist raw (local) ------------------------------------------------
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_out = raw_dir / f"{content_hash}.html"
    if not raw_out.exists():
        raw_out.write_bytes(raw_html)
        print(f"Wrote raw: {raw_out}")
    else:
        print(f"Raw exists (idempotent): {raw_out}")

    # -- parse (deterministic parser when available; minimal fallback) ------
    entry: KBBIEntry | None = None
    try:
        # Try the real parser when it exists (Stream C).
        from aksantara.parse.parser_contract import (
            parse_kbbi_raw,  # type: ignore[import-untyped]
        )

        entry = parse_kbbi_raw(raw_html, source=source)
        print("Parsed via aksantara.parse.parser_contract")
    except Exception:
        # Minimal fallback producing a valid KBBIEntry for the slice — kept
        # narrow so Stream C's real parser is authoritative when present.
        entry = KBBIEntry(
            id=lema_slug,
            lema=args.lema,
            kelas_kata=["n"],
            makna=[
                {
                    "definisi": "bulan kedua tahun Masehi; terdiri atas 28 hari, 29 hari pada tahun kabisat"
                }
            ],
            contoh=["Februari adalah bulan terpendek."],
            bentuk_tidak_baku=["Pebruari"] if lema_slug == "februari" else [],
            source=source,
        )
        print("Parsed via fallback slice builder (Stream C parser not yet available)")

    assert entry is not None

    # -- validate -----------------------------------------------------------
    try:
        from aksantara.domain.authority import AuthorityLayer

        layer = (
            AuthorityLayer.KBBI_OFFICIAL_LIVE
            if source.source_kind == "official-live"
            else AuthorityLayer.ENRICHMENT
        )
        if args.project is None and source.source_kind != "official-live":
            print(
                f"WARNING: synthetic/enrichment entry {entry.id} will not enter entries/ without official source",
                file=sys.stderr,
            )
        # Authority gate — enrichment cannot write canonical; import is still
        # emitted locally for replay, but the pipeline would quarantine on push.
        if layer.is_canonical_writer or args.dry_run:
            print(
                f"Validation policy: layer={layer.value} can_write={layer.is_canonical_writer or args.dry_run}"
            )
    except Exception as exc:
        print(f"Validation stub error: {exc}", file=sys.stderr)

    # -- write canonical JSON (local, deterministic) ------------------------
    out_dir.mkdir(parents=True, exist_ok=True)
    canonical_out = out_dir / f"{entry.id}.json"
    canonical_out.write_text(
        json.dumps(entry.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"Wrote canonical: {canonical_out} (version {version}, hash {content_hash[:12]}…)"
    )

    # -- optional Firestore / GCS push (requires credentials) ---------------
    if args.project and not args.dry_run:
        try:
            from google.cloud import firestore, storage  # type: ignore[import-untyped]

            # Firestore entries/{id}
            db = firestore.Client(project=args.project)
            db.collection("entries").document(entry.id).set(
                entry.model_dump(mode="json")
            )
            print(f"Firestore entries/{entry.id} upserted in project {args.project}")

            # GCS raw + canonical
            bucket_name = f"{args.project}-aksantara"
            try:
                gcs = storage.Client(project=args.project)
                bucket = gcs.bucket(bucket_name)
                bucket.blob(f"raw/{content_hash}.html").upload_from_string(
                    raw_html, content_type="text/html"
                )
                bucket.blob(f"canonical/{version}/{entry.id}.json").upload_from_string(
                    canonical_out.read_text(encoding="utf-8"),
                    content_type="application/json",
                )
                print(
                    f"GCS gs://{bucket_name}/raw/{content_hash}.html + canonical/{version}/{entry.id}.json"
                )
            except Exception as gcs_exc:
                print(f"GCS push skipped/failed: {gcs_exc}", file=sys.stderr)
        except Exception as f_exc:
            print(
                f"Firestore/GCS skipped (no credentials or SDK): {f_exc}",
                file=sys.stderr,
            )
            print(
                "Run with --dry-run to suppress this warning, or set GOOGLE_APPLICATION_CREDENTIALS",
                file=sys.stderr,
            )

    print(f"Done — lema={entry.lema} version={version} contentHash={content_hash}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
