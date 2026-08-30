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

## Git workflow

Use conventional commits. Never commit secrets or generated credentials. Review staged diff before committing. Do not push without explicit authorization.
