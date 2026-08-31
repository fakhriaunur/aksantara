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
