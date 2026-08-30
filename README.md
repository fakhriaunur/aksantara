# Aksantara

Aksantara is a KBBI-first Indonesian language-data supply chain. It ingests the complete available KBBI corpus, preserves source evidence, creates structured LLM-friendly records, generates KBBI-only embeddings, and feeds lighter downstream language tools.

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

## Readiness roadmap

- **Level 1, Functional:** runnable setup, pinned dependencies, formatter, linter, type checker, tests, and local replay.
- **Level 2, Documented:** `AGENTS.md`, architecture and authority policy, reproducible setup, pre-commit, and explicit ownership boundaries.
- **Level 3, Standardized:** CI, integration/replay tests, secret/dependency scans, structured logs, trace IDs, and human review gates.
- **Level 4, Optimized:** cached fast CI, failure/flaky metrics, freshness/parser/vector-cost dashboards, and rollback metrics.
- **Level 5, Autonomous:** structured task discovery, bounded agent decomposition, least privilege, reviewable changes, deterministic gates, recovery, and self-improving estimates.

Factory requires 80% of one level's criteria before unlocking the next. Run `/readiness-report` after configuring a Git `origin`; use `/readiness-fix` only with explicit scope, review, tests, and a follow-up report.

## Current scope

The 100-entry dataset is an integration checkpoint, not the product boundary. Full-corpus ingestion and incremental refresh are the target. Hunspell/LibreOffice, cspell, Babel, Polyglossia, and Rabu Baku remain separate downstream tracks.
