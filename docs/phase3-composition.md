# Phase 3 End-to-End Composition — Checkpoint → Resume → Release → Retrieval → Projection

This document wires the **public entrypoints** into one coherent local path and maps every observable identity, hash, and gate between stages. It is the operational companion to `checkpoint-driver.md`, `projection-publication.md`, and `downstream-contract.md`.

## 1. Composition graph and stage identities

```text
catalog_selected
  -> run_started            { catalog_id, corpus_version, catalog_fingerprint, run_fingerprint, effective_limit }
  -> checkpoint_committed   { run_id, revision, checkpoint, cursor, outcome_counts, accepted_joins }
  -> candidate_ready        { candidate_id, eligibility, exclusion_reasons, manifest bytes }
  -> embedding_planned      { plan_id, new/changed/unchanged/removed/excluded, old/new canonical_hash }
  -> vectors_persisted      { batch_id, chunk_ids, committed_doc_ids, reused_from/origin_release }
  -> candidate_verified     { verify_id, valid, self_hash, vector set }
  -> release_validated      { release_version, manifestHash, artifactHashes, canonicalHashes }
  -> pointer_current        { version, generation, operation_id, history event }
    -> branch api_read_verified     { citation: entry_id, source_release, manifest_hash/hash, canonical/raw hash, source_url/kind/edition/version, retrieval }
    -> branch projection_verified   { consumer, track, source_release, generator_version, schema_version, output_hash, self_hash }
```

Handoffs are:
- `catalog/run fingerprints` — observer can recompute from sorted `(stable_key, source-ref identity)` records.
- `checkpoint revision` — `checkpoint.json` revision + `status.cursor` monotonic and bounded.
- `candidate/release manifestHash` — lower-case SHA-256 of canonical manifest bytes (excluding its own self field).
- `plan ID` — deterministic from prior/candidate canonical hashes and excluded set.
- `vector/batch digest` — per-vector SHA over payload + lineage.
- `pointer generation` — opaque CAS token (`gen-1`, `gen-2`, …) plus expected version; ABA-safe.
- `projection identity` — `(consumer, track, source_release, generator_version, schema_version)` collision-safe.

A failed predecessor **blocks** its successor; API serving is not a prerequisite for projection (branches independently).

## 2. Prerequisites — local deterministic mode (no GCP)

```bash
mise install                         # Python 3.13.15, pitchfork
cp .env.example .env                 # leave GCP vars empty for in-memory/fail-closed mode
pip install -e ".[dev]"

mise run lint && mise run type && mise run test
./scripts/qa_smoke.sh --ephemeral    # ephemeral API + curl all baseline routes
```

Local mode makes **zero** live-network, GCP, emulator, or unapproved-host attempts (`network_trace.live_network_attempts == 0`).

## 3. Stage 1 — Checkpoint (deterministic 100-key)

Public CLI:

```bash
python scripts/checkpoint.py contract --json
python scripts/checkpoint.py preflight --root /tmp/aksantara-ck --catalog catalog.json --limit 100 --json
python scripts/checkpoint.py run --root /tmp/aksantara-ck --catalog catalog.json --limit 100 --idempotency-key demo --json
python scripts/checkpoint.py status --root /tmp/aksantara-ck --run-id checkpoint-<fingerprint> --json
python scripts/checkpoint.py report --root /tmp/aksantara-ck --run-id checkpoint-<fingerprint> --json
python scripts/checkpoint.py outcomes --root /tmp/aksantara-ck --run-id checkpoint-<fingerprint> --json
python scripts/checkpoint.py attempts --root /tmp/aksantara-ck --run-id checkpoint-<fingerprint> --json
python scripts/checkpoint.py checkpoint --root /tmp/aksantara-ck --run-id checkpoint-<fingerprint> --json
```

FastAPI (local-only, caller-owned root required):

```bash
curl -s http://127.0.0.1:8000/openapi.json | jq '.paths | keys | grep(checkpoints)'
curl -s http://127.0.0.1:8000/checkpoints/contract | jq
curl -X POST http://127.0.0.1:8000/checkpoints/runs \
  -H 'Content-Type: application/json' \
  -d '{"root":"/tmp/aksantara-ck","catalog_path":"catalog.json","limit":100,"idempotency_key":"demo"}' | jq
```

Catalog is caller-owned JSON under `--root`; `transport.path` is relative and must stay below root. Selection is the first `effective_limit` keys after sorting normalized stable keys (NFKC + whitespace collapse + casefold). Fingerprints exclude `retrieved_at` and input order.

See `docs/checkpoint-driver.md` for the complete catalog schema, fingerprint preimages, lifecycle (`created/running/interrupted/blocked/failed/completed`), cursor/window semantics, and error mapping.

## 4. Stage 1b — Resume, retry, and fault recovery

Interrupted run resumes only published uncommitted/in-flight work (at most one key beyond `cursor`); committed keys never repeat.

```bash
# Interrupt after N committed keys (caller-owned, local-only)
python scripts/checkpoint.py run --root /tmp/aksantara-ck --catalog catalog.json --limit 100 \
  --interrupt-after 5 --json

# Resume with same tuple (lease is reclaimed with new generation, old generation fenced)
python scripts/checkpoint.py resume --root /tmp/aksantara-ck --run-id checkpoint-<fp> --catalog catalog.json --limit 100 --json

# Barrier/fault controls (caller-owned, process-scoped, local-only; cannot target cloud)
python scripts/checkpoint.py run --root /tmp/aksantara-ck --catalog catalog.json --limit 100 --barrier checkpoint-before-cursor --barrier-hold 2 --json
python scripts/checkpoint.py run --root /tmp/aksantara-ck --catalog catalog.json --limit 100 --persistence-fault checkpoint --persistence-fault-phase before_write --json
```

Retry policy: `max_retries=3`; each source key has at most `R+1` cumulative transport requests across restarts (validation attempts are separate); `Retry-After` parsing and jitter are bounded and documented in `checkpoint-driver.md`.

Equivalence proof: two isolated roots with identical bytes, `SourceRef` retrieval values, pins, release inputs, and fixed clock produce equal selected keys, fingerprints, canonical/raw hashes, outcomes, exclusions, candidate bytes/self-hash, and projection bytes after normalizing only documented volatile fields (`run_id`, `request_id`, `operation_id`, process times, lease owner/heartbeat, in-flight set).

## 5. Stage 2 — Incremental release (seed → plan → build → verify)

All operations are **caller-owned, local-only, never implicitly promote**.

```bash
# Seed v1 from caller-owned canonical dir (creates releases/v1.json, vectors/v1/, registry/)
python scripts/release_embeddings.py seed --root /tmp/release --version v1 --canonical-dir fixtures/canonical --json

# Delta planning (disjoint/exhaustive new/changed/unchanged/removed plus excluded_ids; ignores raw retrieval time)
python scripts/release_embeddings.py plan --root /tmp/release --prior v1 --candidate v2 \
  --prior-canonical-dir fixtures/prior --candidate-canonical-dir fixtures/candidate --json

# Build/work (delta-only: new+changed provider calls; unchanged reuse with zero calls; removed/excluded no work)
python scripts/release_embeddings.py build --root /tmp/release --plan-id plan-v1-to-v2 --mode local --fixed-clock 2026-09-01T00:00:00Z --json

# Vector inspection and strict verification (side-effect-free, fail-closed)
python scripts/release_embeddings.py inspect --root /tmp/release --release v2 --json
python scripts/release_embeddings.py verify --root /tmp/release --release v2 --json

# Release list/read
python scripts/release_embeddings.py list --root /tmp/release --json
python scripts/release_embeddings.py read --root /tmp/release --release v2 --json
```

FastAPI equivalents are under `openapi.json` paths `/releases/*` (`release_seed`, `release_plan`, `release_build`, `release_vector_inspect`, `release_verify`, `release_list`, `release_read`).

**Delta invariants:**
- `candidate_input_ids = eligible_candidate_ids ∪ excluded_ids`
- `prior_ids ∪ eligible_candidate_ids = new ∪ changed ∪ unchanged ∪ removed`
- Unchanged reuse materializes a **v2-scoped vector** with `reused_from`/`origin_release`, identical values/document digest, compatible metadata, zero provider calls, and separately counted persistence.
- Every vector carries `gemini-embedding-001`, 768 dims, `RETRIEVAL_DOCUMENT`, `DOT_PRODUCT`, `emb-768-v1`, finite values, and exact `(source_release, entry_id)` lineage.

**Batch persistence:** create-only/idempotent, preflights conflicting payloads before first write, chunks at 500, per-chunk atomic, later-chunk failure leaves candidate incomplete/ineligible with recoverable tail and no pointer change.

**Cost accounting:**

```json
{
  "provider_calls": 2,
  "retries": 0,
  "reused": 2,
  "request_units": 2,
  "estimate_version": "cost-v1",
  "formula": "request_units = provider_calls * 1 (retries excluded, bounded)",
  "mode": "local",
  "fake": true
}
```

No cloud work occurs in local mode; fake work is labeled `fake: true`.

## 6. Stage 3 — Promotion, rollback, and pointer (approval-bearing CAS)

```bash
# Promote v2 (requires validated candidate, human approval, expected version + generation, ABA-safe)
python scripts/release_embeddings.py promote --root /tmp/release --candidate v2 \
  --expected-version v1 --expected-generation gen-1 \
  --reviewer alice --reason "approve v2" --policy release-v1 --json

# Current pointer and history (append-only, previous events preserved byte-identical)
python scripts/release_embeddings.py current --root /tmp/release --json
python scripts/release_embeddings.py history --root /tmp/release --json

# Rollback to v1 (re-verifies exact validated target, changes only pointer + one typed event)
python scripts/release_embeddings.py rollback --root /tmp/release --target v1 \
  --expected-version v2 --expected-generation gen-2 \
  --reviewer alice --reason "rollback" --policy release-v1 --json
```

**User-visible behavior:**
- Invalid promotion (stale generation, invalid candidate, unapproved, losing writer) returns typed `409`/`422`/`503` and leaves active pointer unchanged (except typed failed-attempt audit). Candidate remains non-promotable.
- Valid promotion is one atomic generation-token CAS (`gen-N` → `gen-N+1`); same operation retry is idempotent (no second event); ABA `v1→v2→v1→v2` succeeds with new generation each step.
- Rollback re-verifies target, changes only pointer + one `rollback` event, preserves all `releases/`, `vectors/`, `canonical/`, `registry/history.json` bytes; repeat is idempotent; invalid target fails closed.

FastAPI mirrors are `POST /releases/promote`, `POST /releases/rollback`, `GET /releases/current`, `GET /releases/history`.

## 7. Stage 4a — Active API retrieval and citation

Active retrieval resolves to **one validated release snapshot** per request (`source_release` + `manifest_hash` + matching `canonical_content_hash`).

```bash
# Health and current pointer
curl -s http://127.0.0.1:8000/health | jq
curl -s http://127.0.0.1:8000/versions/current | jq '.version, .manifestHash'

# Exact / prefix / semantic / nonstandard (all cited)
curl -s http://127.0.0.1:8000/entries/entry-999 | jq '.citation'
curl -s "http://127.0.0.1:8000/entries?q=entry&limit=5" | jq '.count, .results[0].citation.source_release'
curl -s "http://127.0.0.1:8000/search/semantic?q=definisi%20baru" | jq '.results'
curl -s http://127.0.0.1:8000/relations/nonstandard/Pebruari | jq '.standard_form, .citation'
```

**Consistent during transitions:** before/after promotion, invalid promotion, and rollback, each request either serves the prior validated snapshot or `503/unavailable`; it never mixes candidate/old/new records. Historical/removed/candidate data does not leak; dangling/unvalidated pointer is fail-closed (`503`), not static fallback.

**Citations carry or resolve:** `entry_id`, `source_release`, `manifest_hash`, `canonical_content_hash`, `raw_content_hash`, `source {url, edition, source_version, retrievedAt, contentHash, parserVersion}`, and `retrieval {mode, distance, threshold}`.

**Fail-closed:** `GET /search/semantic` without a configured provider returns exactly `{"results":[]}`; configured failure maps to documented `unavailable` rather than hash-vector fallback.

OpenAPI for evidence: `GET /openapi.json` bindings `versions_current`, `entries_exact`, `entries_prefix`, `search_semantic`, `nonstandard_relation`, `projection_*`, `release_*`, `checkpoint_*`. Do not guess routes; missing conditional HTTP surfaces are `N/A`.

## 8. Stage 4b — Projection (generic word/relations)

```bash
python scripts/generate_projection.py registry --json | jq .allowed_tracks
python scripts/generate_projection.py generate \
  --release-root /tmp/release --output-root /tmp/out \
  --consumer aksantara --track word --release v2 --fixed-clock 2026-09-01T00:00:00Z --json

python scripts/generate_projection.py verify --output-root /tmp/out --consumer aksantara --track word --release v2 --json
python scripts/generate_projection.py read --output-root /tmp/out --consumer aksantara --track word --release v2 --json
python scripts/generate_projection.py list --output-root /tmp/out --json
```

Supported: `(consumer=aksantara|generic, track=word|relations)`, schema `word-v1`/`relations-v1`, generator `proj-gen-v1`. Rejected product identifiers (`hunspell`, `cspell`, `babel`, `polyglossia`, `rabu-baku`) are blocked via `422`.

Projection manifest carries: `consumer`, `track`, `source_release`, `source_manifest_hash`, sorted `source_entries[{id, canonical_content_hash, raw_content_hash, source_url, source_kind, source_release}]`, `generator_version`, `schema_version`, `output_path` (relative), `output_hash`, `self_hash`, and `status: validated`.

Artifact bytes and hashes are deterministic for fixed inputs/clock regardless of input file order; every scalar/edge is canonical-derived and witnessed; no raw HTML, transport envelope, invented endpoint, or product format appears.

**Atomic publication:** artifact bytes + `output_hash` + manifest + `self_hash` become one visibility unit (staging `.staging/<identity>/*.tmp` → atomic `Path.replace` under per-identity lock). Readers never see partial ready object; `list` shows only validated artifacts where manifest + artifact exist and hashes verify.

**Isolation:** projection writes only under `output_root/{.staging,.locks,projections}` and never to `canonical/raw/vectors/releases/registry`.

See `docs/projection-publication.md` for fault injection, status/read behavior, and upstream immutability proofs.

## 9. Quarantine propagation

One `blocked_entry_or_source_key_set` and the terminal policy propagate through all stages:

- Malformed bytes / hash mismatch / strict-type / authority violation → `rejected`/`quarantined`/`failed` in checkpoint `outcomes` with `excluded_keys`/`exclusions`.
- Lower-authority evidence stays labeled (`source_kind: fallback/governance/...`) and is never relabeled to official; never enters canonical candidate.
- Substantive official/fallback lexical conflicts (`makna`, `contoh`, …) produce a stable conflict ID with both sides, differing fields/value hashes, first-seen run, open review, release-block status, and append-only `select_official`/`block` history.
- **Blocked IDs are absent from:** embeddings (`vectors/`), release manifest `artifactHashes`, active retrieval citations, and validated projections. Earlier validated historical namespaces (`projections/.../v1`) may retain prior evidence and are excluded from this prohibition.

## 10. Incremental, historical, and active exclusion

- Removed-ID retention: `v1` retains byte-identical vectors/canonical/manifest/readable via `load_manifest(root, "v1")`; `v2` excludes removed from active manifest, release-scoped vectors, retrieval, and projection.
- Unchanged reuse joins: `entry_id`, `source_release`, `canonical_content_hash`, `model=gemini-embedding-001`, `dimensions=768`, `task=RETRIEVAL_DOCUMENT`, `distance_measure=DOT_PRODUCT`, `schema_version=emb-768-v1`, and `origin_release`/`reused_from`.
- Historical reads/projections do not alter current pointer or another namespace.

## 11. Provenance joins

For one accepted official entry, the following identities join:

| Boundary | Hash / identity |
|---|---|
| Raw snapshot | `raw_content_hash = SHA-256(raw bytes)` |
| Canonical record | `canonical_content_hash = SHA-256(published canonical JSON bytes)` |
| Embedding document | `embedding_document_hash` (deterministic allowed-fields serialization) |
| Release manifest | `manifestHash` (self-hash of canonical manifest bytes) |
| Vector record | `source_release`, `raw_content_hash`, `canonical_content_hash`, `embedding_document_hash`, `model/dims/task/distance/schema` |
| API citation | `source_release`, `manifest_hash`, `canonical_content_hash`, `raw_content_hash`, `source url/kind/edition/version`, `retrieval` |
| Projection source entry | `source_release`, `source_manifest_hash`, `canonical/raw hashes`, `source_url/kind` |
| Projection artifact | `output_hash = SHA-256(artifact bytes)`, `self_hash = SHA-256(manifest without self)` |

Legacy `contentHash` alone is not a join; both `raw` and `canonical` hashes are required.

## 12. Authorization, secrets, and resource boundaries

- **Baseline reads are public:** `GET /health`, `/openapi.json`, `/versions/current`, `/entries*`, `/search/semantic`, `/relations/*`, projection `registry/list/read` require no credentials.
- **Mutating operations are local-only with caller-owned roots:** every `POST /checkpoints/runs`, `/releases/*`, `/projections/generate` requires an explicit `root`/`release_root`/`output_root` that stays below the caller's `root` (path-containment proof). Cloud/production targeting is rejected; a documented `local-fixture-only` mode is the only supported mode locally.
- **Missing auth on a shared mutation is an implementation gap:** in this repo all mutations are local-only; there is no shared remote mutation without a scope.
- **Secrets never appear in logs, responses, or repos:** `GOOGLE_CLOUD_PROJECT`, `GOOGLE_APPLICATION_CREDENTIALS`, ADC tokens, cookies, signed URLs, and private keys are process-scoped env vars only. `grep -R token && grep -R credential` must be empty on logs/responses.
- **Resource bounds:** 100-key checkpoint is the fixed cap; cloud work is bounded to one fixture entry / one unique release in the approved sandbox; provider request units are bounded and reproducible; no persistent emulator, watch process, or broad corpus run is permitted.

## 13. Approved GCP sandbox smoke (separately approved)

- Project: `ata-devpost-sandbox` only.
- Firestore Native `(default)` in `asia-southeast1`.
- Bucket: `gs://ata-devpost-sandbox-aksantara`.
- Exactly one fixture entry, unique test release, `gemini-embedding-001` at 768 dimensions, bounded one-shot Vertex request, one vector doc, one current-version pointer verification, and explicit cost audit.
- Preflight in `scripts/gcp_release_smoke.py` rejects any other project/database/region/bucket, broad or >1 entry, non-unique release, new resource, delete, IAM, migration, bootstrap, production action, or implicit promotion before SDK init.

Do not run this smoke without explicit user approval.

## 14. Cleanup

After normal completion, interruption, abrupt termination, retry exhaustion, validation failure, fingerprint block, persistence fault, and successful resume:

- No owned worker / PID / lock remains, or documented expiry/fencing makes it recoverable without manual deletion.
- Durable `run/checkpoint/attempt/error`, raw, canonical, review, and candidate evidence remains readable.
- Only runner-owned temporary state, staging, locks, leases, processes, and ports are cleaned; historical evidence, vectors, pointers, and another owner's lease remain.
- Stale worker cannot write after reclaim (fenced).
- Sentinel test: a file outside `output_root` is unchanged after `cleanup_projection_staging`.

Verify:

```bash
lsof -ti :8000 | head      # should be empty after ./scripts/qa_smoke.sh --ephemeral
ls /tmp/aksantara-*/.staging 2>&1 | head   # no *.tmp remains
pytest tests/integration/test_phase3_composition.py -q
pytest tests/unit/test_projection_publication_isolation.py -q
./scripts/qa_smoke.sh --ephemeral  # cleanup verified via trap
```

## 15. Test index

| Test file | What it proves |
|---|---|
| `tests/integration/test_phase3_composition.py` | All 8 VAL-CROSS assertions locally |
| `tests/unit/test_projection_publication_isolation.py` | Atomic visibility, fault recovery, upstream immutability |
| `tests/unit/test_incremental_embeddings.py` | Delta sets, reuse, metadata, batch chunks |
| `tests/unit/test_release_verification_promotion.py` | Verification, promotion CAS, rollback preservation |
| `tests/replay/*` | Deterministic replay equivalence |

Run the exact milestone gate before handoff:

```bash
mise run lint
mise run type
mise run test
./scripts/qa_smoke.sh --ephemeral
```

