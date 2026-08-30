# Readiness — Aksantara (L1–L4)

Factory requires 80% of a level's criteria before unlocking the next. Level 5 is a later Transform phase and is not claimed here.

## L1 — Functional (runnable slice)

| # | Criterion | Evidence | Owner | Status |
|---|-----------|----------|-------|--------|
| L1-1 | `mise install` on clean container succeeds; pins reproducible | `mise.toml` (min 2026.8.14, python 3.13.15, pitchfork 2.23.0) + `mise.lock` | Stream A | ☐ |
| L1-2 | `mise run check` green: `ruff format --check`, `ruff check`, `mypy --strict src` | CI log / local `mise run check` output | Stream A/B | ☐ |
| L1-3 | `mise run test` / `pytest` green (unit + replay) | `pytest tests/replay -k februari` deterministic | Stream B/C | ☐ |
| L1-4 | One `Februari` entry fetched, archived as `{hash}.html`, parsed, validated, stored in Firestore with full provenance | Firestore doc + GCS object + provenance fields (`url, source_kind, edition VI, source_version, retrievedAt, contentHash, parserVersion`) | Stream C | ☐ |
| L1-5 | Vertex embedding `gemini-embedding-001` dim 768 stored; Firestore composite vector index exists and KNN returns citation | `manifests/{version}.json` + `gcloud firestore indexes composite list` + query log | Stream D | ☐ |
| L1-6 | API `GET /entries/{lema}`, `/search/semantic`, `/health` reachable locally | `api/routes.py` + manual curl log | Stream D | ☐ |

**Gate:** All six pass. Failed/skipped checks recorded with residual risk owner (Lead Orchestrator).

## L2 — Documented (reviewable)

| # | Criterion | Evidence | Owner | Status |
|---|-----------|----------|-------|--------|
| L2-1 | `AGENTS.md` + project guide present | `AGENTS.md` at repo root | Stream B | ✅ |
| L2-2 | Architecture doc with module map and data-flow guarantees | `docs/architecture.md` | Stream B | ✅ |
| L2-3 | Authority policy with layer table and enforcement | `docs/authority-policy.md` | Stream B | ✅ |
| L2-4 | Source inventory gov-first table with provenance fields | `docs/source-inventory.md` | Stream B | ✅ |
| L2-5 | Downstream contract with manifest schema and retrieval order | `docs/downstream-contract.md` | Stream B | ✅ |
| L2-6 | Reproducible setup (`README.md` + `.env.example` + `mise.toml`) | `README.md`, `.env.example`, `mise.toml` | Stream A/B | ☐ |
| L2-7 | Pre-commit hooks (ruff, mypy, secrets scan) | `.pre-commit-config.yaml` | Stream A | ☐ |
| L2-8 | Ownership boundaries (Lead/Ingestion/Retrieval) documented | `docs/architecture.md` §3, `triage/worksheet.md` | Stream B | ✅ |

**Gate:** ≥80% (≥7/8) pass; `mise.toml`/pre-commit land with Stream A.

## L3 — Standardized (gated before broad rollout)

| # | Criterion | Evidence | Owner | Status |
|---|-----------|----------|-------|--------|
| L3-1 | CI runs `mise run check` + `pytest` on PR | `.github/workflows/ci.yml` | Stream A/QA | ☐ |
| L3-2 | Integration + replay tests (exact→prefix→semantic, quarantine, fail-closed) | `tests/replay`, `tests/retrieval`, `tests/security` | Stream C/D | ☐ |
| L3-3 | Secret scan passes; no `.env` or service key committed | `git secrets scan` log | QA | ☐ |
| L3-4 | Dependency scan clean | `pip-audit` / `dependabot` log | QA | ☐ |
| L3-5 | Structured logs with `runId`/`traceId` | `src/aksantara/observability/` | QA | ☐ |
| L3-6 | Human review gates for conflicts/releases enforced | `docs/authority-policy.md` §5 + review checklist | Lead | ☐ |

**Gate:** ≥80% pass before 100-entry checkpoint.

## L4 — Optimized (measured)

| # | Criterion | Evidence | Owner | Status |
|---|-----------|----------|-------|--------|
| L4-1 | Cached fast CI (≤3 min) | CI cache hit log | QA | ☐ |
| L4-2 | Failure/flaky metrics dashboard | `infra/dashboards/` | QA | ☐ |
| L4-3 | Freshness lag / parser drift / vector cost dashboards | `infra/dashboards/` + Firestore metrics | QA | ☐ |
| L4-4 | Rollback drill measured (pointer flip latency) | `triage/` drill record | Lead | ☐ |
| L4-5 | Embedding cost per 100 entries tracked | `manifests/` cost note | Lead | ☐ |

**Gate:** Progressive after L3; required before full-corpus rollout.

## How to verify this slice (Phase 2)

```bash
mise install
mise run check          # ruff format --check, ruff check, mypy --strict src
pytest tests/replay -k februari -v
pytest tests/retrieval -v
pytest tests/security -v
gcloud firestore indexes composite list --format=json | jq
curl -s localhost:8000/entries/februari | jq .source.contentHash
curl -s 'localhost:8000/search/semantic?q=bulan%20kedua' | jq
curl -s 'localhost:8000/search/semantic?q=xyzabc123' | jq # -> {"results":[]}
```

## Current verdict

- **L1:** Pending — awaits Stream C/D slice integration (`Februari` E2E).
- **L2:** ✅ Foundation complete (6/8 — remaining `mise.toml`/pre-commit from Stream A).
- **L3/L4:** Not yet — scheduled after slice gate per sequenced plan.

Next step: Spawn Stream C (Ingest/Parse/Validate) + Stream D (Vertex/Firestore/Retrieve) in parallel for `Februari` slice; then Slice Integration + QA.
