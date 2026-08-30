# Aksantara Project Guide

## Mission

Aksantara is a KBBI-first Indonesian language-data fuel layer. KBBI is the sole lexical source of truth. Preserve provenance, deterministic replay, versioned releases, and fail-closed retrieval. Downstream tracks consume manifests and never mutate canonical records.

## Repository layout

- `src/`: application and domain code.
- `tests/`: unit, integration, replay, retrieval, and security tests.
- `scripts/`: import, refresh, embedding, and manifest commands.
- `infra/`: Cloud Run, Firestore, and Cloud Storage configuration.
- `docs/`: architecture, authority, source, downstream, and readiness docs.
- `triage/`: event worksheet and execution record.

## File editing

Do not use the ApplyPatch tool. Use edit, multiedit, create, or bounded Python/shell scripts for file changes.

## Tooling contract

Use **mise** as project toolchain and task runner. Pin runtime and dependency versions in project configuration; prefer `mise run <task>` over ad-hoc commands.

Use **Pitchfork** as local service manager for dependent services and development processes. Keep service definitions, health checks, logs, and shutdown behavior explicit. Do not start persistent services without user authorization.

Containerize later with **Podman**. Keep container files and commands under `infra/containers/`; do not make Podman a prerequisite for the first local slice.

## Required checks

Before commit, run the narrowest relevant `mise` tasks for formatting, linting, type checking, tests, replay, and security. Record failed or skipped checks. Keep fast checks separate from full checks.

## Authority and safety

- Official KBBI owns lexical facts.
- Official Badan Bahasa/PUEBI/EYD material owns structural rules.
- Community dumps and mirrors are labeled fallback or derived.
- Other corpora are enrichment/evaluation only.
- AI output is a proposal, never canonical truth.
- Vertex AI generates embeddings; Firestore stores and searches the initial vector index.
- Exact and prefix lookup precede semantic retrieval.
- Unknown semantic queries fail closed.
- Do not expose secrets, commit `.env`, or use unapproved scraping bypasses.
- Human reviews source conflicts, lexical changes, public claims, and releases.

## Agent operating model

Three bounded agents: Lead Orchestrator, Ingestion, and Retrieval/Normalization. Keep parsing, validation, diffing, embedding, retrieval, and projection generation deterministic. Agents must produce reviewable changes and evidence. Level 1–2 readiness comes first; add Level 3 controls, measure Level 4, and treat Level 5 as a later Transform phase.

## Interactive QA — Agent-Followable Path

Concrete end-to-end QA for an agent (backend/API, no auth gate, no external services required for in-memory slice):

**Deps & services:** `mise install` (Python 3.13.15, pitchfork), `cp .env.example .env` (leave GCP vars empty for in-memory mode), `pip install -e ".[dev]"`. No DB or emulator needed — Firestore/Vertex are optional and app boots `not_configured` via fail-closed.

**Auth:** None. API is public for QA (`/health`, `/docs` require no credentials). Semantic search returns `[]` without Vertex, which is expected.

**Launch:**
```bash
mise run dev        # uvicorn aksantara.api.routes:create_app --factory --host 127.0.0.1 --port 8000 --reload
# or: mise run qa  # one-shot smoke that starts ephemeral server and curls every endpoint
```

**Drive (meaningful interactions):**
```bash
curl -s http://127.0.0.1:8000/health | jq
curl -s http://127.0.0.1:8000/versions/current | jq .version
curl -s "http://127.0.0.1:8000/entries?q=feb&limit=5" | jq .count
curl -s http://127.0.0.1:8000/docs | head  # Swagger UI
curl -s "http://127.0.0.1:8000/search/semantic?q=bulan%20kedua" | jq
./scripts/qa_smoke.sh --ephemeral  # full smoke: health, versions, docs, prefix, exact, semantic, nonstandard, replay
mise run replay && mise run slice
```

Expected: `GET /health` → `{"status":"ok"}`, `GET /search/semantic` → `{"results":[]}` fail-closed without creds, `GET /entries/februari` → `404` on empty index (seed via `pytest` fixtures). See `README.md#interactive-qa-agent-followable-end-to-end` and `scripts/qa_smoke.sh`.

## Git workflow

Use conventional commits. Never commit secrets or generated credentials. Review staged diff before committing. Do not push without explicit authorization.
