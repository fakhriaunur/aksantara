---
name: qa-backend
description: >
  Functional QA for Aksantara FastAPI backend using an ephemeral local server and curl.
---

# Backend QA

## Target and setup

Test checked-out branch code only. Run `./scripts/qa_smoke.sh --ephemeral`; it starts the API on a random localhost port, exercises read-only endpoints, and cleans up. No GCP credentials or authentication are needed.

Do not run pytest, lint, mypy, or other automated suites as QA. The smoke command is the functional harness; its replay check is an application acceptance check for this slice.

## Flow menu

Run flows relevant to changed files:

1. **Health and API contract** — `GET /health` returns HTTP 200 and status `ok`; `GET /docs` exposes OpenAPI/Swagger.
2. **Version pointer** — `GET /versions/current` returns a non-empty version.
3. **Prefix lookup** — `GET /entries?q=feb&limit=5` returns HTTP 200 with `results` list and `count`.
4. **Exact lookup boundary** — `GET /entries/februari` returns either a valid entry or documented 404 on an empty in-memory index; unexpected status fails.
5. **Semantic fail-closed** — `GET /search/semantic?q=bulan%20kedua&limit=3` returns HTTP 200 with a list and no fabricated result when Vertex is unavailable.
6. **Nonstandard relation boundary** — `GET /relations/nonstandard/Pebruari` returns valid relation data or documented 404 on an empty index; unexpected status fails.
7. **Deterministic replay** — smoke replay validation passes for the Februari fixture.

## Negative checks

For lookup changes, verify malformed or absent data does not produce a 500. For semantic changes, verify unavailable credentials return an empty result rather than guessed canonical data.

## Evidence and failures

Record endpoint, status, and response shape in report evidence. Report any startup, dependency, timeout, or environment issue as BLOCKED with remediation. Never silently skip a flow.

## Known Failure Modes

- Empty in-memory index can legitimately return 404 for exact and nonstandard lookups.
- Vertex and Firestore are intentionally unavailable in local QA; semantic search must fail closed.
- QA requires Python dependencies installed and `python -m uvicorn` available through the project environment.
