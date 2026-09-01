# Aksantara

Aksantara is a KBBI-first Indonesian language-data supply chain. It ingests the complete available KBBI corpus, preserves source evidence, creates structured LLM-friendly records, generates KBBI-only embeddings, and feeds lighter downstream language tools.

## Introduction: closing a documented gap

Indonesian language tooling has a large user base but a fragmented update
pipeline. The gap is documented in both research and upstream tooling:

- [Spell Checker for the Indonesian Language: Extensive Review](https://doi.org/10.46338/ijetae0522_01)
  (2022) describes Indonesian spell checkers as uncommon and prior work as
  fragmented across methods and local publications.
- [SPECIL: Spell Error Corpus for the Indonesian Language](https://doi.org/10.1109/access.2023.3307712)
  (IEEE Access, 2023) identifies the earlier absence of a recognized,
  publicly accessible Indonesian spelling-error corpus and contributes more
  than 180,000 tokens across 21,500 sentences.
- [Automatic Correction of Indonesian Grammatical Errors Based on Transformer](https://doi.org/10.3390/app122010380)
  (2022) characterizes Indonesian grammatical-error correction as
  low-resource, citing limited parallel training data.
- [A Simple Yet Effective Corpus Construction Framework for Indonesian Grammatical Error Correction](https://arxiv.org/abs/2410.20838)
  (2024) continues to identify limited Indonesian resources and the need for
  evaluation data that reflects real-world errors.
- [KBBI VI Daring update notes](https://kbbi.kemdikbud.go.id/Beranda/Pemutakhiran)
  show that official updates change entries, meanings, pronunciation,
  syllabification, word-class labels, examples, and active status. Static
  downstream dictionaries cannot reliably mirror all of these changes.
- [Sipebi](https://kbbi.kemdikbud.go.id/Sipebi/SeputarUrunDaya), maintained by
  Badan Bahasa, demonstrates the need for current KBBI data, expert review,
  morphology rules, and public contribution mechanisms.
- [babel-indonesian](https://ctan.org/pkg/babel-indonesian) version 1.0n
  recorded a 2025 correction from historical `Pebruari`/`Nopember` output to
  `Februari`/`November`, illustrating how downstream language artifacts can
  lag authoritative usage.

Aksantara addresses this gap upstream: one provenance-bearing KBBI fuel layer
feeds exact lookup, KBBI-only semantic retrieval, and lighter downstream
projections. It does not claim to solve Indonesian grammar generally, replace
expert language review, or represent government endorsement.

## Product map

- **Aksantara KBBI:** canonical corpus, provenance, refresh, validation, and release manifests.
- **Aksantara Pramana:** exact, prefix, and semantic retrieval over validated KBBI records.
- **Aksantara Hunspell:** future Hunspell/LibreOffice projection.
- **Aksantara cspell:** future code-spelling projection.
- **Aksantara Babel:** future legacy LaTeX Babel Indonesian projection.
- **Aksantara Polyglossia:** future modern LaTeX locale projection.
- **Aksantara Rabu Baku:** separate content/product track.

## Authority policy

Official KBBI is sole lexical authority. Official Badan Bahasa orthographic and structural material is modeled separately as rule authority. Community datasets, mirrors, SPECIL, CC100, OSCAR, Leipzig, news corpora, LLMs, and embeddings are fallback, enrichment, evaluation, or transformation inputs only. They cannot add or override canonical KBBI facts.

## Architecture

```text
KBBI source
  -> immutable raw snapshot
  -> deterministic parser
  -> validated canonical corpus
  -> Vertex AI embeddings
  -> Firestore vector search
  -> cited Aksantara Pramana retrieval
  -> versioned downstream projections
```

The initial vector solution is Google-native: Vertex AI generates embeddings, Firestore stores vectors and performs KNN search. Exact and prefix lookup run before semantic retrieval. The vector backend remains behind an interface for a measured future move to Vertex AI Vector Search, Milvus, or SurrealDB.

## Agentic pipeline

Three bounded agents coordinate the workflow:

1. **Lead Orchestrator:** run lifecycle, policy gates, retries, manifests, and escalation.
2. **Ingestion:** source access, rate limits, raw snapshots, hashes, and resumable batches.
3. **Retrieval/Normalization:** embedding preparation, anomaly proposals, and retrieval evaluation.

Parsing, validation, diffing, embedding storage, retrieval, and projections remain deterministic functions. AI cannot invent definitions, standard forms, or spelling rules.

## Development

Repository foundation uses `mise` for toolchain and task execution and Pitchfork for local service management. Podman containerization is planned later under `infra/containers/`.

```bash
mise install
mise run check
mise run test
```

Commands become active as implementation scaffolding lands. Copy `.env.example` to `.env`; `.env` is ignored and must never be committed.

## Deterministic 100-key checkpoint

The Phase 3 checkpoint driver accepts a caller-owned catalog and fixture
transport manifest. It normalizes and sorts stable keys, enforces the
`1..100` limit domain, hashes source-reference identity, and writes durable
run/report/checkpoint artifacts only below the requested root. Local mode makes
no live network, GCP, emulator, or release-pointer calls:

```bash
python scripts/checkpoint.py --help
python scripts/checkpoint.py contract --json
python scripts/checkpoint.py preflight --root /tmp/aksantara-checkpoint --catalog catalog.json --limit 100 --json
python scripts/checkpoint.py run --root /tmp/aksantara-checkpoint --catalog catalog.json --limit 100 --idempotency-key demo --json
python scripts/checkpoint.py status --root /tmp/aksantara-checkpoint --run-id checkpoint-<fingerprint> --json
python scripts/checkpoint.py report --root /tmp/aksantara-checkpoint --run-id checkpoint-<fingerprint> --json
python scripts/replay.py februari --root . \
  --raw tests/replay/fixtures/februari.html \
  --retrieved-at 2026-08-31T00:00:00Z --source-version VI --json
```

The complete catalog schema, fingerprint preimages, lifecycle, error mapping,
immutable report history, public replay contract, and FastAPI
`/checkpoints/*` operations are documented in
[`docs/checkpoint-driver.md`](docs/checkpoint-driver.md). An accepted
checkpoint is evidence only, never an implicit candidate or release.

## Phase 3 End-to-End Composition — Checkpoint → Resume → Release → Retrieval → Projection

A 100-entry local run traverses **checkpoint → resume → incremental release → active retrieval/citation → projection** with joinable identities; quarantined records never reach embeddings, releases, retrieval, or projections; interrupted vs uninterrupted output is equal after only documented volatile fields; delta/history is preserved.

The single authoritative flow (see [`docs/phase3-composition.md`](docs/phase3-composition.md) for the full stage/identity join table, failure matrix, and hash lineage):

```text
catalog_selected -> run_started -> checkpoint_committed -> candidate_ready
  -> embedding_planned -> vectors_persisted -> candidate_verified -> release_validated -> pointer_current
    -> branch api_read_verified, branch projection_verified
```

**Resume & recovery:**

```bash
python scripts/checkpoint.py run --root /tmp/ck --catalog catalog.json --limit 100 --interrupt-after 5 --json
python scripts/checkpoint.py resume --root /tmp/ck --run-id checkpoint-<fp> --catalog catalog.json --limit 100 --json
# Barrier/fault seams (caller-owned, process-scoped, local-only; cannot target cloud)
python scripts/checkpoint.py run --root /tmp/ck --catalog catalog.json --limit 100 --barrier checkpoint-before-cursor --barrier-hold 2 --json
python scripts/checkpoint.py run --root /tmp/ck --catalog catalog.json --limit 100 --persistence-fault checkpoint --persistence-fault-phase before_write --json
```

**Incremental release & pointer (all caller-owned, local-only, never implicitly promote):**

```bash
python scripts/release_embeddings.py seed --root /tmp/release --version v1 --canonical-dir fixtures/canonical --json
python scripts/release_embeddings.py plan --root /tmp/release --prior v1 --candidate v2 --prior-canonical-dir fixtures/prior --candidate-canonical-dir fixtures/candidate --json
python scripts/release_embeddings.py build --root /tmp/release --plan-id plan-v1-to-v2 --mode local --fixed-clock 2026-09-01T00:00:00Z --json
python scripts/release_embeddings.py verify --root /tmp/release --release v2 --json
python scripts/release_embeddings.py promote --root /tmp/release --candidate v2 --expected-version v1 --expected-generation gen-1 --reviewer alice --reason "approve" --policy release-v1 --json
python scripts/release_embeddings.py current --root /tmp/release --json
python scripts/release_embeddings.py rollback --root /tmp/release --target v1 --expected-version v2 --expected-generation gen-2 --reviewer alice --reason "rollback" --policy release-v1 --json
```

Invalid promotion leaves the old pointer active; valid promotion/rollback change only the pointer + one append-only history event while preserving all versioned data.

**Active retrieval (one validated snapshot per request) and fail-closed semantics:**

```bash
curl -s "http://127.0.0.1:8000/entries/entry-999" | jq .citation
curl -s "http://127.0.0.1:8000/search/semantic?q=xyzabc123" | jq  # -> {"results":[]}
```

Citations carry `source_release`, `manifest_hash`, `canonical_content_hash`, `raw_content_hash`, `source {url, edition, source_version, retrievedAt}`, and `retrieval`.

**Projection (generic `word`/`relations`, atomic visibility, upstream read-only):**

```bash
python scripts/generate_projection.py registry --json | jq .allowed_tracks
python scripts/generate_projection.py generate --release-root /tmp/release --output-root /tmp/out --consumer aksantara --track word --release v2 --fixed-clock 2026-09-01T00:00:00Z --json
python scripts/generate_projection.py verify --output-root /tmp/out --consumer aksantara --track word --release v2 --json
```

Rejected downstream products (`hunspell`/`cspell`/`babel`/`polyglossia`/`rabu-baku`) are blocked; missing/invalid/unvalidated sources fail before validated publication.

**Provenance joins:** `raw_content_hash` (SHA-256 raw bytes), `canonical_content_hash` (published JSON bytes), `embedding_document_hash`, `manifestHash`, `source_release`, `output_hash`/`self_hash` — all lower-case hex, byte-verified.

**Boundaries & safety:** local mode makes zero GCP/provider/network writes; approved sandbox is `ata-devpost-sandbox` / Firestore `(default)` `asia-southeast1` / `gs://ata-devpost-sandbox-aksantara` (one fixture entry, unique release, bounded 768-dim `gemini-embedding-001` call only after explicit approval). Baseline reads are public; mutations require explicit caller-owned roots; secrets never appear in logs/responses; only owned staging/locks/processes/ports are cleaned, historical evidence (`releases/`, `vectors/`, `registry/history.json`) is never deleted. See [`docs/projection-publication.md`](docs/projection-publication.md) for atomic publication proofs and [`docs/downstream-contract.md`](docs/downstream-contract.md) for manifest schemas.

## Interactive QA — Agent-Followable End-to-End

A complete, documented path to bring the app to an interactive state and exercise it. No external credentials required for the in-memory slice (Firestore/Vertex optional).

**Prerequisites:** Python 3.13 via `mise`, `mise` 2026.8.14+, no GCP credentials needed for basic QA (app boots with in-memory fakes and fail-closed semantic).

```bash
# 1. Install toolchain and deps (one command)
mise install
cp .env.example .env  # no edits needed for QA; GCP vars remain empty for in-memory mode
pip install -e ".[dev]"  # or mise will handle via python venv

# 2. Verify static checks and unit/replay slice
mise run check   # ruff format --check, ruff check, mypy src
mise run coverage  # pytest with 35% coverage gate, htmlcov output

# 3. Launch the API (no auth gate — bypass documented: empty .env runs in-memory)
mise run dev &
# or: uvicorn aksantara.api.routes:create_app --factory --host 127.0.0.1 --port 8000 --reload
sleep 3
curl -s http://127.0.0.1:8000/health | jq
# -> {"status":"ok","firestore":"not_configured",...}

# 4. Drive meaningful interactions (exact / prefix / semantic / nonstandard / versions)
curl -s http://127.0.0.1:8000/entries/februari | jq .lema
curl -s "http://127.0.0.1:8000/entries?q=feb&limit=5" | jq .count
curl -s "http://127.0.0.1:8000/search/semantic?q=bulan%20kedua&limit=3" | jq .results
curl -s http://127.0.0.1:8000/relations/nonstandard/Pebruari | jq .standard_form
curl -s http://127.0.0.1:8000/versions/current | jq .version
curl -s http://127.0.0.1:8000/docs | head -20  # OpenAPI UI
# -> no configured semantic backend: {"results":[]}

# 5. Replay and retrieval slice (deterministic, no network)
mise run replay  # pytest tests/replay -v (Februari fixture, hash 35a702...)
mise run slice   # replay + retrieval cascade exact→prefix→semantic
./scripts/qa_smoke.sh  # one-shot smoke: starts ephemeral uvicorn, curls all endpoints, exits 0 on success

# Cleanup
kill %1; wait %1 2>/dev/null || true
```

Auth handling: **No login gate.** API is public for QA; semantic search fails closed (empty results) without Vertex credentials, which is expected and documented. To test with live Firestore/Vertex, populate `.env` with `GOOGLE_CLOUD_PROJECT`, `GOOGLE_APPLICATION_CREDENTIALS`, and run `scripts/bootstrap_gcp.sh`.

Mise tasks for QA: `mise run dev`, `mise run qa`, `mise run smoke` — see `mise.toml`.


## Readiness roadmap

- **Level 1, Functional:** runnable setup, pinned dependencies, formatter, linter, type checker, tests, and local replay.
- **Level 2, Documented:** `AGENTS.md`, architecture and authority policy, reproducible setup, pre-commit, and explicit ownership boundaries.
- **Level 3, Standardized:** CI, integration/replay tests, secret/dependency scans, structured logs, trace IDs, and human review gates.
- **Level 4, Optimized:** cached fast CI, failure/flaky metrics, freshness/parser/vector-cost dashboards, and rollback metrics.
- **Level 5, Autonomous:** structured task discovery, bounded agent decomposition, least privilege, reviewable changes, deterministic gates, recovery, and self-improving estimates.

Factory requires 80% of one level's criteria before unlocking the next. Run `/readiness-report` after configuring a Git `origin`; use `/readiness-fix` only with explicit scope, review, tests, and a follow-up report.

## Current scope

The 100-entry dataset is an integration checkpoint, not the product boundary. Full-corpus ingestion and incremental refresh are the target. Hunspell/LibreOffice, cspell, Babel, Polyglossia, and Rabu Baku remain separate downstream tracks.
