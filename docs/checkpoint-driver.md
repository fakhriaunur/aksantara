# Deterministic checkpoint driver

The checkpoint driver is the bounded, local validation surface for the Phase 3
corpus checkpoint. It does not perform live transport, use GCP or an emulator,
write the canonical namespace, create vectors, or change a release pointer.
It does persist immutable raw/provenance observations, review evidence, and a
candidate only after the explicit fail-closed candidate gate succeeds.

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
| `history` | Reads immutable report revisions for all runs under a root |
| `checkpoint` | Reads the committed checkpoint revision and bounded cursor |
| `execute` | Reads an existing run as an idempotent no-op |
| `review-queue` | Reads open authority reviews in stable key/review ID order |
| `review-read` | Reads one review record, including both immutable conflict sides |
| `review-decision` | Appends `select_official`, `block`, or `reject` with idempotency |
| `candidate-evaluate` | Evaluates exact official/raw/canonical/review/release joins |
| `candidate-read` | Reads a previously persisted candidate evaluation |

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
GET  /checkpoints/runs/history
GET  /checkpoints/runs/{run_id}/checkpoint
GET  /checkpoints/reviews
GET  /checkpoints/reviews/{review_id}
POST /checkpoints/reviews/{review_id}/decisions
POST /checkpoints/runs/{run_id}/candidate
GET  /checkpoints/runs/{run_id}/candidate
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

The catalog and entry objects reject unknown fields during preflight. The
documented snake-case fields above are canonical; the contract endpoint lists
the small set of accepted compatibility aliases (`catalogId`,
`corpusVersion`, `stableKey`, `sourceRef`, and so on). Aliases are accepted
only one at a time, never merged by precedence.

`transport.path` is relative to the caller root and must resolve to an
existing file below that root. Inline callers may instead use immutable
`content`, `base64`, or Python-only `bytes`. A successful fixture must bind
exactly one representation, except that an inline `content` value may
explicitly replace a path binding for a changed-source lineage test. The
adapter computes SHA-256 from actual bytes; declared hashes are checked, not
trusted.

Each entry has one primary `source_ref`/`transport` pair. Additional source
references, when intentionally supplied, must be represented by the
`observations` array. Each item contains `source_ref`, `transport`, and an
optional `role` (`official`, `fallback`, or `evidence`). Official bindings are
attempted before lower-authority bindings; the primary binding is first within
its authority tier, and remaining bindings are sorted by role and complete
source-reference identity before fingerprinting and processing, so input array
order is not meaningful. Unsupported containers or
aliases such as `sources`, `evidence`, `source_refs`, `sourceReferences`,
`references`, `additional_observations`, `official`, and `fallback` are
rejected during preflight, as are unknown observation fields. An ambiguous
pair of aliases for any source, transport, role, or identity field is also
rejected. This fail-closed rule prevents a reference from being accepted in
the manifest and silently omitted from source processing.

The adapter accepts official KBBI hosts (`kbbi.kemdikbud.go.id`) and the
labelled fallback host (`kbbi.web.id`) for evidence. The source kind must agree
with its host. Additional `observations` may bind official or lower-authority
evidence, but a lower-authority observation cannot be labelled official. A
fallback record remains evidence and is quarantined by the authority validator;
the checkpoint driver never relabels it.

## Authority review and candidate gate

All configured official bindings are attempted in deterministic order. The first
successful adapter-verified official observation after transport, raw-hash,
parse, schema, provenance, and identity checks is selected as the canonical
source. A successful backup official observation can therefore be selected
after an earlier retryable or failed official observation. Lower-authority
evidence is considered only after the official tier has been attempted; it is
always labelled evidence and never selected as canonical. Lexical fields are
compared in this order:
`lema`, `sub_lema`, `ejaan`, `kelas_kata`, `makna`, `contoh`, `turunan`,
`bentuk_baku`, `bentuk_tidak_baku`, `pelafalan`, `pemenggalan`, `etimologi`,
`labels`, and `status`. Retrieval/provenance metadata differences do not create
lexical conflicts.

Conflicts are stored below `.aksantara/review/conflicts/` with both source
references, raw/observation IDs, canonical hashes, differing fields, and
field-level value hashes. Quarantines are stored below
`.aksantara/review/quarantine/`. Review decisions append an event containing
the reviewer, reason, timestamp, policy pin, and idempotency key. Selecting an
official side resolves only the item conflict; release approval remains a
separate explicit gate.

`candidate-evaluate` returns `eligible=false` and an exclusion reason for every
non-terminal, non-official, malformed, unresolved, or hash/pin-invalid item.
It creates `.aksantara/candidates/<candidate-id>.json` only when the complete
fixed 100-key checkpoint has exact raw/canonical/source/observation joins,
matching authority roles and parser pins, resolved item reviews, and explicit
release approval with approver identity and reason. It never writes vectors,
canonical entries, or the current-version pointer.

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
        canonical/<stable-key>.json
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

`attempts.json` has two explicit ledgers. `attempts` contains one logical
attempt row for each selected stable key for compatibility with earlier
checkpoint consumers. `physical_attempts` contains exactly one ordered row for
each configured source observation, including retryable/permanent transport
failures, hash/parse/validation failures, successful backup official
observations, lower-authority evidence, and conflict sides. Each physical row
includes `attempt_id`, `run_id`, `stable_key`, `sequence`, `source_ref`,
`source_kind`, `source_role`, `outcome`, raw/observation IDs and hashes,
`canonical_content_hash`, parse/validation results, and `conflict_result`.
Reports expose `physical_attempt_count` and `physical_attempts` separately
from `logical_attempt_count`, current outcomes, and outcome counts.

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

The checkpoint report itself does not implicitly create a candidate and always
states `pointer_changed=false` and `release_operation=not_invoked`. Candidate
evaluation is a separate explicit operation; embedding, release promotion, and
pointer changes remain outside this driver.

## Public deterministic replay

Replay is a separate, read-only operation for one caller-owned snapshot. It
verifies the actual raw SHA-256 and the `SourceRef` identity before invoking
the deterministic parser. It does not fetch the source URL, call an LLM,
repair checkpoint state, or write canonical/candidate/release/pointer state:

```bash
python scripts/replay.py februari \
  --root . \
  --raw tests/replay/fixtures/februari.html \
  --retrieved-at 2026-08-31T00:00:00Z \
  --source-version VI --json
```

The response includes raw and canonical-record hashes, serialization and
parser/transform/policy pins, and `"writes":{"count":0}`. Use
`--expected-canonical-hash` when replaying against a previously published
canonical hash. Changed bytes, source identity, or pins return a structured
nonzero error and never silently normalize the input.
