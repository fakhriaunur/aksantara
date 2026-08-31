# Deterministic checkpoint driver

The checkpoint driver is the bounded, local validation surface for the Phase 3
corpus checkpoint. It does not perform live transport, use GCP or an emulator,
write the canonical namespace, create a release candidate, or change a release
pointer.

## Public operations

The machine-readable contract is available without a catalog:

```bash
python scripts/checkpoint.py contract --json
```

The CLI operations are:

| Operation | Effect |
| --- | --- |
| `contract` | Prints normalization, bounds, fixture, fingerprint, lifecycle, and error rules |
| `preflight` | Validates a catalog and computes selection/fingerprints without reading fixture bytes |
| `run` | Creates durable caller-rooted state and evaluates the selected fixture keys |
| `status` | Reads the durable run status |
| `report` | Reads the conserved current-outcome report |
| `outcomes` | Reads one current outcome row per selected key |
| `attempts` | Reads the separate attempt history |
| `checkpoint` | Reads the committed checkpoint revision and bounded cursor |
| `execute` | Reads an existing run as an idempotent no-op |

Use `--json` for one machine-readable JSON object. Invalid catalog or limit
input exits nonzero before a run directory or fixture read is created. A
durable unknown-run read is also a structured nonzero result.

The FastAPI surface exposes the same contract and lifecycle:

```text
GET  /checkpoints/contract
POST /checkpoints/runs
POST /checkpoints/runs/{run_id}/execute
GET  /checkpoints/runs/{run_id}
GET  /checkpoints/runs/{run_id}/report
GET  /checkpoints/runs/{run_id}/outcomes
GET  /checkpoints/runs/{run_id}/attempts
GET  /checkpoints/runs/{run_id}/checkpoint
```

`POST /checkpoints/runs` is explicitly local-only and requires a caller-owned
`root`, plus exactly one of `catalog_path` or inline `catalog`. Read operations
may supply `?root=...` after an API process restart because the durable root is
never guessed.

## Catalog and fixture manifest

The catalog is caller-owned JSON. Its identity and every entry are required:

```json
{
  "catalog_id": "kbbi-checkpoint-fixture-v1",
  "corpus_version": "kbbi-vi-fixture-v1",
  "pins": {
    "parser_version": "0.1.0",
    "transform_version": "0.1.0",
    "validation_policy": "official-first-v1"
  },
  "authority_mode": "official-first",
  "comparison_mode": "sha256-exact-v1",
  "entries": [
    {
      "stable_key": "entry-000",
      "source_ref": {
        "url": "https://kbbi.kemdikbud.go.id/entri/entry-000",
        "source_kind": "official-snapshot",
        "edition": "VI",
        "source_version": "fixture-v1",
        "retrieved_at": "2026-08-31T00:00:00Z",
        "content_hash": "<64 lower-case hex characters>",
        "parser_version": "0.1.0"
      },
      "transport": {
        "adapter": "fixture",
        "path": "fixtures/entry-000.html",
        "content_type": "text/html",
        "expected_raw_hash": "<same 64 lower-case hex characters>",
        "comparison_mode": "exact",
        "status": 200
      }
    }
  ]
}
```

`transport.path` is relative to the caller root and must resolve to an
existing file below that root. Inline callers may instead use immutable
`content`, `base64`, or Python-only `bytes`. A successful fixture must bind
exactly one representation. The adapter computes SHA-256 from actual bytes;
declared hashes are checked, not trusted.

The adapter accepts official KBBI hosts (`kbbi.kemdikbud.go.id`) and the
labelled fallback host (`kbbi.web.id`) for evidence. The source kind must agree
with its host. A fallback record remains evidence and is quarantined by the
authority validator; the checkpoint driver never relabels it.

## Determinism and bounds

Stable keys use Unicode NFKC normalization, Unicode whitespace collapse, and
case-folding. Blank, control-character, URL-like, slash/backslash, dot-like,
`..`-containing, and overlong keys are rejected. Normalization collisions and
multiple references for one normalized key fail before source processing.

`--limit` is an integer in `1..100`, defaulting to `100`. Zero, negative,
non-integer, and values above `100` are rejected. A valid catalog shorter than
the requested limit is processed without padding and reports its exact
shortfall as ineligible. Selection is the first `effective_limit` records after
sorting normalized stable keys; input order never affects the selection.

The catalog fingerprint is SHA-256 over canonical JSON containing sorted
`(stable_key, source-reference identity)` records. Source-reference identity
contains URL, source kind, edition, source version, content hash, and parser
version. Retrieval time and input order are excluded. The run fingerprint adds
catalog/corpus identity, effective limit, selection algorithm, authority and
comparison policy, and parser/transform/validation pins. The caller root is a
separate idempotency/storage scope boundary and is not a release-relevant hash
input.

## Durable state and safety

Artifacts are written below:

```text
<caller-root>/
  .aksantara/
    checkpoint-idempotency.json
    checkpoint-runs/
      checkpoint-<run-fingerprint>/
        request.json
        preflight.json
        raw/<raw-sha256>.bin
        parsed/<stable-key>.json
        outcomes.json
        attempts.json
        checkpoint.json
        report.json
        status.json
```

Raw and parsed evidence is hash-addressed or immutable. Status is a mutable
current snapshot; outcome, attempt, checkpoint, report, and request identities
remain readable in the caller root. An identical idempotency key and run tuple
returns the existing run without source reads or duplicate artifacts. Reusing
the key with a changed tuple returns a conflict before source processing.

Each report conserves:

```text
selected_count
== unique_current_outcome_keys
== sum(outcome_counts)
```

Attempt count is reported separately. Item outcomes are `pending`,
`in_progress`, `accepted`, `retryable`, `quarantined`, `rejected`, or `failed`.
The local driver only completes after one terminal current outcome per selected
key. A retryable transport status is `blocked`; deterministic parser/schema/hash
failures are terminal `rejected` or `quarantined`; permanent transport failures
are `failed`.

Accepted checkpoint rows are not a release candidate. Reports always state
`candidate_created=false`, `pointer_changed=false`, and
`release_operation=not_invoked`. Authority review, candidate eligibility,
embedding, approval, and release promotion belong to later pipeline surfaces.
