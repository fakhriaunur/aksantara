---
name: qa
description: >
  Run functional API QA for Aksantara. Routes changed backend files to curl-based
  flows and writes a concise report. Use for pull requests and smoke testing.
---

# QA Orchestrator

This skill performs manual/functional QA only. Do not run unit tests, lint, type checks, or static analysis here.

## Run

1. Read `.factory/skills/qa/config.yaml`.
2. Inspect `git diff` and run only `.factory/skills/qa-backend/SKILL.md` when files match its paths.
3. Use the local ephemeral target for branch validation. Never substitute a remote environment.
4. Run relevant flows plus one related negative or boundary flow.
5. Capture response bodies or concise command output as evidence.
6. Write `qa-results/report.md` using the report template.
7. If a new environment failure appears, add Suggested Skill Updates to the report.

Never silently skip a flow. If a flow cannot complete, report it as BLOCKED with what was tried and how the user can fix it.

If no app code changed, report INCONCLUSIVE: no app code changed, QA not applicable for this diff.
