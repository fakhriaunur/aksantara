# Projection Publication Isolation — Atomic Visibility, Fault Recovery, and Upstream Immutability

This document proves the guarantees required by `VAL-PIPE-PROJ-004/005/006` and `VAL-API-PROJ-004/005/006`.

## 1. Publication model

* **Caller-owned roots.** `release_root` contains immutable upstream state
  (`releases/`, `canonical/`, `vectors/`, `registry/`, `runs/`, `conflicts/`, `review/`).
  `output_root` is a separate caller-owned directory (either `release_root/projections`
  or an external temp dir). `is_safe_output_root` rejects any `output_root` that equals
  `release_root` or lies inside a forbidden namespace — projection can never write to
  canonical/raw/vector/release history.

* **Collision-safe identity.** Every publication is addressed by
  `(consumer, track, source_release, generator_version, schema_version)` via
  `projection_identity`. This tuple is present in the manifest, artifact path,
  list, and read. Two tracks (`word`, `relations`) and two releases remain
  independently addressable; same-identity with different payload returns typed
  `409 conflict` and never overwrites.

* **Atomic visibility.** Artifact bytes, `output_hash`, projection manifest, and
  `self_hash` become **one visibility unit**:

  1. Build artifact deterministically (sorted IDs, fixed serialization, fixed clock).
  2. Write artifact and manifest to `output_root/.staging/<identity>/` as `.tmp` files.
  3. Verify `artifact_hash` and `manifest_self_hash` on staged files.
  4. Hold a per-identity file lock (`output_root/.locks/<hash>.lock`) for
     existence check + both renames. Readers hold no lock but verify both files
     exist with matching hashes; staged files are under `.staging` and never
     enumerated by `list_projections` nor returned by `read_*`.

  Renames are `Path.replace` (atomic on POSIX). A torn state (artifact without
  manifest, manifest without artifact, hash mismatch) is exposed as `failed` /
  `unavailable` / `pending`, never as `validated`. `get_projection_status`
  returns exactly `pending|validated|failed|unavailable`.

* **One writer, same-identity conflict fails closed.** The per-identity lock
  serializes concurrent `generate_projection` calls. Identical payload is
  idempotent (second call returns existing manifest without second event);
  conflicting payload raises `409 conflict`. An interleaved `status`/`read`
  observes either the prior valid artifact or explicit non-ready, never partial.

## 2. Status / read behavior

* Only `validated` artifacts are readable. `read_projection_manifest` and
  `read_projection_artifact` verify `self_hash` and `output_hash`, check that
  both files exist under `output_root/projections`, and that `status` is
  `validated`. Any mismatch, missing file, staged file, or `pending/failed/
  unavailable` status raises `ProjectionError` with typed `code`/`status`
  (`404 not_found`, `409 conflict`, `422 hash_mismatch/ineligible`, `500 failed`).

* `list_projections` enumerates only validated manifests whose artifact exists
  and whose `output_hash`/`self_hash` verify. Staged, tampered, or
  hash-mismatched entries are hidden, not listed as ready.

* Unknown or tampered reads never substitute another track/release. Selectors
  are validated (`validate_selector` rejects path traversal, empty, and
  rejected products like `hunspell/cspell/babel/polyglossia/rabu-baku`);
  every read resolves the exact identity path and verifies hashes against that
  identity's manifest.

## 3. Invalid-source blocking

`generate_projection` calls `verify_release` (strict, side-effect-free) **before**
any publication:

* Verifies `manifestHash` self-hash, complete `artifactHashes`/`canonicalHashes`,
  exact vector set (model `gemini-embedding-001`, 768 dims, `DOT_PRODUCT`,
  `RETRIEVAL_DOCUMENT`, schema `emb-768-v1`, finite values, exact
  `(source_release, entry_id)` lineage, no extra/duplicate/missing vectors).

* Any missing/ineligible/blocked release (`conflicts`, `quarantine`,
  `blocked_ids`, vector-count mismatch, tampered hash, unavailable store) raises
  `ProjectionError(code=ineligible, status=422)` **before** staging.

* A copied source mutation that changes canonical bytes is eligible only if the
  new release re-verifies; otherwise the valid prior projection hash remains
  unchanged and is still readable.

## 4. Fault recovery — failed or interrupted publication leaves prior valid intact

Supported local faults (caller-owned, process-scoped via `fault=` param or
`AKSANTARA_PROJECTION_FAULT` env var, CLI `--fault`):

| Phase | Injection point | Effect |
|---|---|---|
| `artifact_write` | before staging artifact write | raises `failed` before any rename; no validated artifact exposed |
| `output_hash` | after staging artifact, before hash verification | raises `hash_mismatch`/`failed`; staged files removed |
| `manifest_commit` | after staging both files, before `replace` | raises `failed`; staged cleared, orphan artifact removed if no prior valid |
| `verification` | after both renames, before final hash check | raises `failed`; downstream reads see `failed` (manifest exists but hash verification fails) |

On any fault:

* `finally` block removes `.tmp` files and empties `.staging/<identity>` and
  `.staging` if empty.
* If the fault occurred after the artifact `replace` but before manifest
  commit and no prior valid existed, the orphan artifact is removed so it is
  not exposed as staged-ready.
* Prior valid artifacts (`output_root/projections/<consumer>/<track>/<release>/…`)
  remain byte-identical and readable; `list_projections` still returns only the
  prior valid entries.

Tests in `tests/unit/test_projection_publication_isolation.py`:

* `TestAtomicPublicationAndImmutability::test_fault_at_each_phase_preserves_prior_valid`
  — publishes `v1` valid, faults `v2` at each phase, asserts `v1` hash unchanged
  and `v2` not found / not listed, staging has no `*.tmp`.
* `test_interrupted_publication_leaves_no_staged_ready` — simulates crash after
  staging without manifest commit; asserts `get_projection_status` is
  `pending/failed/unavailable`, reads raise, list hides it.
* `TestNamespaceIsolationAndAtomicWriter::test_concurrent_writers_one_wins_atomically`
  — two threads race on same identity; asserts one hash visible and reader
  never sees hash mismatch.

## 5. Upstream immutability

Projection has **no write path** to upstream state. `generate_projection`,
`read_*`, `list_projections`, `get_projection_status`, and
`cleanup_projection_staging` only read from `release_root` via `verify_release`
and `_load_release_entries`; they only write under `output_root/.staging`,
`output_root/.locks`, and `output_root/projections/…`.

Proof: `snapshot_upstream_hashes(release_root)` hashes every file under
`canonical/`, `raw/`, `runs/`, `candidate_snapshots/`, `vectors/`, `releases/`,
`registry/` (including `current.json`/`history.json`), `conflicts/`, `review/`.
`TestAtomicPublicationAndImmutability::test_upstream_byte_identical_across_projection_ops`
snapshots before/after a sequence of successful, faulted, and read operations
and asserts digests are byte-identical.

Projection operations also **cannot** promote, roll back, rewrite, or delete
upstream state:

* No call to `promote_release` / `rollback_release` exists in
  `src/aksantara/projections/*` (verified by `rg "promote|rollback"`).
* No delete of `releases/`, `canonical/`, `vectors/`, or `registry/` — only
  `cleanup_projection_staging` deletes, and it is scoped to
  `output_root/.staging` and `output_root/.locks` (never crossing output roots
  or touching retained evidence; sentinel test in the same suite asserts a file
  outside `output_root` is unchanged).

## 6. Cleanup ownership

* **Owned temporary state:** `.staging/<identity>/*.tmp`, empty
  `.staging/<identity>` dirs, empty `.staging`, and per-identity lock files.
* **Never deleted:** validated `projections/<identity>/artifact.json` and
  `manifest.json`, upstream `releases/`, `vectors/`, `canonical/`,
  `registry/current.json` and `history.json`, and any file outside `output_root`.
* **Never crosses roots:** cleanup resolves `output_root` and operates only
  under `output_root/.staging` and `output_root/.locks`; a sentinel file
  outside the root is proved unchanged after cleanup.

API of `cleanup_projection_staging`:

```python
cleanup_projection_staging(output_root)  # purge orphan .tmp/empty dirs
cleanup_projection_staging(output_root, identity="consumer:track:release:gen:schema")
```

## 7. How to exercise

```bash
# Registry / help (publishes selectors, schemas, serialization, status values)
python scripts/generate_projection.py registry --json | jq .allowed_tracks
python scripts/generate_projection.py --help

# Generate validated
python scripts/generate_projection.py generate \
  --release-root /tmp/release --output-root /tmp/out \
  --consumer aksantara --track word --release v1 \
  --fixed-clock 2026-09-01T00:00:00Z --json

# Fault injection (local only, preserves prior valid)
python scripts/generate_projection.py generate \
  --release-root /tmp/release --output-root /tmp/out \
  --consumer aksantara --track word --release v2 \
  --fault artifact_write --json   # -> status 500, v1 still readable

# Status / read / list — never shows staged-ready or mismatched
python scripts/generate_projection.py verify --output-root /tmp/out \
  --consumer aksantara --track word --release v1 --json
python scripts/generate_projection.py read --output-root /tmp/out \
  --consumer aksantara --track word --release v1 --json
python scripts/generate_projection.py list --output-root /tmp/out --json

# HTTP (artifact-only or documented read surfaces)
curl -s http://127.0.0.1:8000/projections/registry | jq
curl -s "http://127.0.0.1:8000/projections/artifact?output_root=/tmp/out&consumer=aksantara&track=word&release=v1" | jq
curl -s "http://127.0.0.1:8000/projections?output_root=/tmp/out" | jq

# Upstream immutability proof (in tests)
pytest tests/unit/test_projection_publication_isolation.py -q
```

All operations are local, deterministic, caller-owned, and offline — no GCP,
emulator, or live network is required. The same assertions are exercised via
the FastAPI routes (`/projections/*`) in the TestClient suite, confirming the
HTTP surfaces preserve the identical isolation guarantees.
