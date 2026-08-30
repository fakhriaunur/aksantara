#!/usr/bin/env python3
"""CLI stub — build Vertex embeddings + Firestore vectors + manifest for the canonical slice.

Reads canonical entries from ``data/canonical/`` (or a single --lema), builds
deterministic embedding documents, calls Vertex AI gemini-embedding-001
(768d, RETRIEVAL_DOCUMENT), writes Firestore vector_entries/{id}_{version},
and emits a versioned manifest at manifests/{version}.json. No raw HTML is
embedded — only the compact document text.

Usage:
    python scripts/build_embeddings.py --version 2026-08-30.1 [--lema Februari] [--project PROJECT]
    python scripts/build_embeddings.py --version 2026-08-30.1 --dry-run   # no Vertex/Firestore calls
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


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build embeddings + vectors + manifest")
    p.add_argument(
        "--version", required=True, help="Release version, e.g. 2026-08-30.1"
    )
    p.add_argument(
        "--lema",
        default=None,
        help="Only embed this lema (default: all in data/canonical/)",
    )
    p.add_argument(
        "--project",
        default=None,
        help="GCP project (required for live Vertex/Firestore)",
    )
    p.add_argument(
        "--location", default="global", help="Vertex AI location (default: global)"
    )
    p.add_argument(
        "--canonical-dir",
        default="data/canonical",
        help="Dir with KBBIEntry JSON files",
    )
    p.add_argument(
        "--manifests-dir",
        default="manifests",
        help="Dir to write manifests/{version}.json",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Build docs + manifest without calling Vertex/Firestore",
    )
    p.add_argument(
        "--bucket", default=None, help="GCS bucket (default: <project>-aksantara)"
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()

    try:
        from aksantara.domain.models import KBBIEntry
        from aksantara.embeddings.document import build_embedding_document
        from aksantara.embeddings.manifests import build_manifest
    except Exception as exc:
        print(f"ERROR: domain/embeddings import failed: {exc}", file=sys.stderr)
        return 2

    canonical_dir = ROOT / args.canonical_dir
    if args.lema:
        candidates = [canonical_dir / f"{args.lema.strip().lower()}.json"]
    else:
        candidates = (
            sorted(canonical_dir.glob("*.json")) if canonical_dir.exists() else []
        )

    if not candidates:
        # Fallback: generate a minimal Februari entry so the script is runnable
        # even before import_corpus has been executed (useful for slice smoke).
        print(
            f"No canonical entries found in {canonical_dir}; using synthetic Februari slice",
            file=sys.stderr,
        )
        from aksantara.domain.models import SourceRef

        src = SourceRef(
            url="https://kbbi.kemdikbud.go.id/entri/februari",
            source_kind="enrichment",
            edition="VI",
            source_version="VI",
            retrieved_at=datetime.now(UTC),
            content_hash="a" * 64,
            parser_version="0.1.0",
        )
        synthetic = KBBIEntry(
            id="februari",
            lema="Februari",
            kelas_kata=["n"],
            makna=[
                {
                    "definisi": "bulan kedua tahun Masehi; terdiri atas 28 hari, 29 hari pada tahun kabisat"
                }
            ],
            contoh=["Februari adalah bulan terpendek."],
            bentuk_tidak_baku=["Pebruari"],
            source=src,
        )
        entries = [synthetic]
    else:
        entries: list[KBBIEntry] = []

        def _coerce_retrieved_at(data: dict) -> dict:
            """Coerce string retrieved_at to datetime for strict SourceRef."""
            try:
                src = data.get("source")
                if isinstance(src, dict) and isinstance(src.get("retrieved_at"), str):
                    s = src["retrieved_at"]
                    # Handle Z suffix and ensure UTC
                    if s.endswith("Z"):
                        s = s[:-1] + "+00:00"
                    dt = datetime.fromisoformat(s)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=UTC)
                    data = {**data, "source": {**src, "retrieved_at": dt}}
            except Exception:
                pass
            return data

        for path in candidates:
            if not path.exists():
                print(f"Skipping missing {path}", file=sys.stderr)
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                data = _coerce_retrieved_at(data)
                entries.append(KBBIEntry.model_validate(data))
            except Exception as exc:
                print(f"Skipping {path}: {exc}", file=sys.stderr)
                continue
        if not entries:
            print("ERROR: no valid entries to embed", file=sys.stderr)
            return 1

    # Build deterministic embedding documents (no raw HTML).
    docs = [(e, build_embedding_document(e)) for e in entries]
    for e, d in docs:
        print(f"Doc {e.id} ({len(d)} chars): {d[:120].replace(chr(10), ' | ')}")

    bucket = args.bucket or (f"{args.project}-aksantara" if args.project else None)

    manifest = build_manifest(
        args.version,
        entries,
        bucket=bucket,
        created_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    )

    # Persist manifest locally regardless of dry-run.
    manifests_dir = ROOT / args.manifests_dir
    manifests_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifests_dir / f"{args.version}.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(
        f"Wrote manifest: {manifest_path} ({manifest['entries_count']} entries, model={manifest['embedding']['model']})"
    )

    if args.dry_run or not args.project:
        print(
            "Dry run — skipping Vertex/Firestore (use --project and omit --dry-run for live writes)"
        )
        if not args.project:
            print("No --project supplied, manifest + docs only", file=sys.stderr)
        return 0

    # -- live Vertex embedding + Firestore vector writes --------------------
    try:
        from aksantara.embeddings.firestore import FirestoreVectorStore, VectorRecord
        from aksantara.embeddings.vertex import (
            VertexGeminiEmbedding,
            VertexGeminiEmbeddingConfig,
        )
    except Exception as exc:
        print(f"ERROR: embedding store import failed: {exc}", file=sys.stderr)
        return 2

    texts = [d for _, d in docs]
    config = VertexGeminiEmbeddingConfig(location=args.location)
    embedder = VertexGeminiEmbedding(project=args.project, config=config)

    try:
        vectors = embedder.embed_documents(texts)
    except Exception as exc:
        print(f"ERROR: Vertex embedding failed: {exc}", file=sys.stderr)
        return 1

    if any(len(v) != 768 for v in vectors):
        print(
            f"ERROR: expected all vectors dims=768, got {[len(v) for v in vectors]}",
            file=sys.stderr,
        )
        return 1

    store = FirestoreVectorStore(project=args.project)
    records: list[VectorRecord] = []
    for (entry, doc), vec in zip(docs, vectors, strict=True):
        records.append(
            VectorRecord(
                id=entry.id,
                version=args.version,
                lema=entry.lema,
                embedding=tuple(float(x) for x in vec),
                model=config.model,
                dimensions=config.output_dimensionality,
                content_hash=entry.source.content_hash,
                source_kind=entry.source.source_kind,
                edition=entry.source.edition,
                source_version=entry.source.source_version,
                parser_version=entry.source.parser_version,
                embedding_document=doc,
            )
        )

    try:
        store.put_many(records)
        print(
            f"Firestore {len(records)} vector(s) written to vector_entries/{{id}}_{args.version}"
        )
    except Exception as exc:
        print(f"ERROR: Firestore write failed: {exc}", file=sys.stderr)
        return 1

    # Optionally flip current_version pointer (spec: atomic pointer flip).
    try:
        from google.cloud import firestore  # type: ignore[import-untyped]

        db = firestore.Client(project=args.project)
        db.collection("config").document("current_version").set(
            {
                "version": args.version,
                "updated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            }
        )
        print("config/current_version pointer flipped to", args.version)
    except Exception as exc:
        print(f"Pointer flip skipped/failed: {exc}", file=sys.stderr)

    print(f"Done — version={args.version} vectors={len(vectors)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
