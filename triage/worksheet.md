---
event_id: H-ATA-001
name: All Things Agentic Hackathon
platform: Devpost
status: committed
decision: commit
external_registration: confirmed
event_url: https://allthingsagentichackathon.devpost.com
registration_url: https://allthingsagentichackathon.devpost.com
worksheet_path: all-things-agentic/triage/worksheet.md
discovered_at: 2026-08-27
registration_deadline: null
start_at: 2026-08-04T21:45:00+07:00
submission_deadline: 2026-09-01T07:00:00+07:00
timezone: UTC+07:00
source_timezone: PDT
priority_1_5: 5
opportunity_1_5: 4
urgency_1_5: 5
strategic_fit_1_5: 4
p_ship: 0.65
p_win: 0.18
confidence: low
evidence_state: researched
constraint_load_1_5: 4
constraint_flags: [time, required_stack, saturation]
estimated_hours: 40
capacity_status: reserved
next_action: Implement Aksantara KBBI fuel layer — full-corpus ingest, deterministic parser, validation, and KBBI-only embeddings
next_action_due: 2026-08-31
last_reviewed: 2026-08-31
source_links:
  - https://allthingsagentichackathon.devpost.com
  - https://allthingsagentichackathon.devpost.com/rules
  - https://allthingsagentichackathon.devpost.com/details/dates
notes: >-
  AI-owned scores provisional on canonical research, 8,925 participants,
  required Google stack, and KBBI utilization pipeline. Finalized scope:
  Aksantara is the umbrella name for the KBBI fuel layer. KBBI remains the
  sole lexical source of truth; build a full-corpus, replayable,
  provenance-bearing digital fuel layer with incremental refresh and
  KBBI-only embeddings/RAG. The 100-lema slice is validation only, not
  product scope. Other corpora are enrichment only and cannot create or
  override lemmas, meanings, standard forms, or rules. Separate downstream
  tracks: Hunspell/LibreOffice, cspell, LaTeX Babel Indonesian, LaTeX
  Polyglossia, and Rabu Baku. Stenotype remains on hold. Source priority is
  gov-first: KBBI VI Daring, official Badan Bahasa rule material, Sipebi
  public/nonconfidential data, then clearly labeled gov-derived snapshots and
  mirrors. Research grounding: Indonesian spell-checker review, SPECIL,
  Indonesian GEC studies, official KBBI update notes, Sipebi documentation,
  and CTAN's 2025 Februari/November correction. Full-corpus ingestion is
  mandatory architecture; downstream artifacts consume versioned manifests.
  Source window Aug 4 07:45 PDT - Aug 31 17:00 PDT stored UTC+07:00.
  Registration deadline not captured separately. Track is Taskmaster; verify
  exact rubric before freeze. Google/Gemini Cloud credits USD 150+40+300.
---

# Event Triage Worksheet

> Copy this file per event: `<event-slug>/triage/worksheet.md`. Work top to bottom.
> Standalone by design. Full method and evidence appendix live in
> `__templates/triage/playbook.md`.

> **Timezone:** Store local timestamps in `UTC+07:00`; preserve published event
> timezone and conflicts in notes or source fields.
>
> **Score ownership:** AI owns `priority`, `opportunity`, `p_ship`, and `p_win`;
> keep evidence, confidence, assumptions, and review date with those values.
>
> **Portfolio link:** Keep summary fields above aligned with the matching row
> in root `TRIAGE.md`. Root register owns aggregate status/capacity; this file
> owns detailed event execution. Use `null` for unknown probabilities, never
> invented zeros.

**Event:** All Things Agentic  **Dates:** 2026-08-04 → 2026-09-01  **Format:** solo ☒ team ☐
**Total your-hours available:** 40 h  *(portfolio reservation; Level 1–2 first, then progressive Level 3–4 readiness)*
**Event link / rubric URL:** https://allthingsagentichackathon.devpost.com

---

## Stage 0 — Recon (target: T-7d or ASAP)

### Judging criteria (copy verbatim from event page)
| Published criterion | Weight if given | My answer plan |
|---|---|---|
| Event criteria | Weight if given | My answer plan |
| Taskmaster track criteria | Not fully captured | Verify Taskmaster rubric exactly; map every criterion to demo, README, diagram, and Cloud proof |

*No published rubric? Use Devpost defaults: technological implementation · ease of use · demonstration · potential impact · quality of idea · design.*

### Requirements screen (hard floor)
- [x] Eligibility (age/geo/affiliation) checked — rules research captured
- [ ] Required tech/platform listed — Gemini 3.5+, Google agent framework, Google Cloud service; I can satisfy all: yes ☐ no ☐
- [ ] Required APIs/sponsors tools identified: Google/Gemini stack
- [ ] Submission artifacts required: video? ☒ length 4 min · links? ☒ public repo · deck? ☐ · other: README, architecture diagram, cloud proof
- [ ] Pre-existing code allowed? rules say: verify before submission

### Sponsor & prize map
| Sponsor | Prize/bounty | Fits my stack? | Category crowded? (L/M/H) |
|---|---|---|---|
| Google | Advertised pool USD 180k; exact track prizes require verification | Yes | H |

### Saturation forecast (re-check at T-24h!)
- Theme chatter (Discord/registrations): agents 8,925 participants · my-theme pending
- Near-identical builds already visible? describe: agentic category highly crowded; inspect selected track submissions

### Pre-event prep (boilerplate, deploy path, recording setup)
- [ ] Auth/deploy scaffold ready at: __________
- [ ] Demo recording setup tested
- [ ] Stack frozen at **4–5 technologies**: 1 Gemini 2 Google agent framework 3 Google Cloud 4 app UI 5 logging/observability
- [ ] Vector decision recorded: Vertex AI embeddings + Firestore vector search; SurrealDB, Milvus, and Vertex AI Vector Search deferred pending measured scale evidence

---

## Idea candidates (fill one row per candidate, aim 3–5)

| # | Idea (one line) | Named user + pain | Demo path in ≤90s? | Fresh window? Why |
|---|---|---|---|---|
| 1 | Aksantara: Lead + Ingestion + Retrieval/Normalization -> full-corpus canonical KBBI records, KBBI-only embeddings/RAG, and versioned projection feeds | Editors, developers, educators, and language-tool maintainers need current formal Indonesian data without rebuilding source ingestion | Yes — refresh trigger -> source snapshot -> validated KBBI record -> cited semantic lookup -> downstream projection manifest | Yes — shared, continuously refreshed upstream fuel shortens fragmented dictionary-tool supply chains |
| 2 | | | | |
| 3 | | | | |
| 4 | | | | |
| 5 | | | | |

---

## Gate A — kill-gates (pass/fail per idea; any FAIL = kill or reshape once)

| Gate | Question | #1 | #2 | #3 | #4 | #5 |
|---|---|---|---|---|---|---|
| A1 Requirements fit | every rule/artifact satisfiable in my hours? | | | | | |
| A2 Saturation | not a clone-flooded category under its own bounty? | | | | | |
| A3 Solo feasibility | one demo path, ≤60% of my hours? | | | | | |
| A4 Window alive | edge not post-saturation decayed? | | | | | |
| **Verdict** | PASS → Gate B · FAIL-once-A2/A3 → reshape & re-enter · else KILL | | | | | |

**Killed & why:** None yet; run requirements and skeleton gates first.
**Reshaped ideas (one re-entry max):** ________________________________

---

## Gate B — HackScore matrix (survivors only)

Anchors (1 / 3 / 5):
- **B1 Demo-path** = tour of features / one flow but fragile / one 90s path that survives failure
- **B2 Rubric balance** = one criterion strong others absent / covers all, thin on two / deliberate answer for every criterion
- **B3 Edge-window** = saturated default category / established, some differentiation / fresh specialist-sponsored category, low clones
- **B4 Judge-consensus** = polarizing / broadly liked, one skeptic persona / all personas score up
- **B5 Impact & story** = solution seeking problem / real pain personal only / named user + citable stat + visible before-state
- **B6 Feasibility margin** = new stack+domain+integration at once / known stack one unknown tight / known stack ≥40% buffer at measured pace

Scale each 1–5. Weights fixed.

| Criterion | Wt | Idea ____ | Idea ____ | Idea ____ |
|---|---|---|---|---|
| B1 Demo-path strength | 0.25 | | | |
| B2 Rubric coverage balance | 0.20 | | | |
| B3 Edge-window freshness | 0.20 | | | |
| B4 Judge-consensus breadth | 0.15 | | | |
| B5 Impact & story shape | 0.10 | | | |
| B6 Feasibility margin | 0.10 | | | |
| **Weighted total** | 1.00 | | | |
| Any criterion = 1? | — | | | |

**Kill bar: total < 3.5 OR any single 1.**

Quadrant check for remaining ties: x = B3 freshness, y = mean(B1,B2,B6). Upper-right wins; final tie → higher B1.

**CHOSEN IDEA:** User's specific idea, after track-fit check
**Predicted HackScore (log for post-mortem):** pending proof

---

## Finalized concept and authority contract — Aksantara

**Product thesis:** **Aksantara** performs heavy KBBI work once upstream, then makes
downstream dictionary engines lighter. The fuel layer supplies current,
structured, LLM-friendly KBBI records, embeddings, change manifests, and
stable projection interfaces. This may create public-interest value through
interoperability and a tighter contribution-to-tooling loop; it must not imply
government endorsement or adoption.

**Authority layers:** official KBBI is lexical truth; official PUEBI/EYD and
Badan Bahasa material supplies orthographic and structural rules; Sipebi
public/nonconfidential material is a compatibility and morphology reference;
community dumps and mirrors are labeled bootstrap/fallback; SPECIL, CC100,
OSCAR, Leipzig, news, and other corpora are enrichment/evaluation only; AI
and embeddings are transformation/retrieval aids, never authority.

**Canonical record:** `KBBIEntry{id, lema, subLema[], ejaan, kelasKata[],
makna[], contoh[], turunan[], bentukBaku, bentukTidakBaku[], pelafalan,
pemenggalan, etimologi, labels[], status, sourceRef, sourceKind, edition,
sourceVersion, retrievedAt, contentHash, parserVersion, transformVersion,
reviewStatus, confidence}`.

**Invariants:** immutable raw snapshots; deterministic parser replay;
idempotent sync; no silent source overwrite; unresolved conflicts quarantined;
embeddings reference exact canonical hashes; unknown semantic queries fail
closed; downstream artifacts include source and generator manifests.

**Embedding implementation:** Vertex AI generates embeddings; Firestore stores
canonical records and vector fields and performs KNN search. Firestore does not
generate embeddings. Exact and prefix lookup precede vector retrieval. Record
model, dimensions, distance measure, and content hash; re-embed only changed
records. Keep `EmbeddingStore` behind an interface so a measured future need
can move to Vertex AI Vector Search, Milvus, or SurrealDB.

## Main pipeline

`INV -> SCHEMA -> FETCH -> PARSE -> VALIDATE -> SLICE -> FULL -> EMBED ->
RETRIEVE -> PUBLISH -> PROOF`

- **FETCH:** official access first, labeled fallback second.
- **PARSE:** one deterministic parser for both transports.
- **FULL:** resumable complete-corpus bootstrap and incremental change import.
- **EMBED:** validated KBBI records only; exact/prefix lookup precedes vectors.
- **RETRIEVE:** citations, source version, content hash, score, and fail-closed
  unknown behavior.

Use three agents: **Lead Orchestrator** for lifecycle and policy, **Ingestion**
for fetching and raw snapshots, and **Retrieval/Normalization** for embedding
preparation and anomaly proposals. Keep parser, validator, publisher, diff,
embedding, retrieval, and projection generation deterministic functions.
Vertex AI is the embedding provider; Firestore is the initial vector backend.

## Separate downstream tracks

| Track | Consumes | Owns |
|---|---|---|
| Aksantara Hunspell | KBBI lexical projection + reviewed affix rules | `.dic`, `.aff`, `.oxt`; separate ticket |
| Aksantara cspell | KBBI word projection + technical vocabulary | Code dictionary; separate ticket |
| Aksantara Babel | KBBI/rule manifests | Dates, captions, aliases, hyphenation |
| Aksantara Polyglossia | Reviewed locale/rule adapter | Modern Unicode localization |
| Aksantara Rabu Baku | KBBI standard/nonstandard relations | Weekly content mechanic; separate track |

No downstream track may mutate canonical KBBI records.

## Embedding/RAG contract

Embed only validated KBBI `lema`, meanings, examples, labels, and relations
through **Aksantara Pramana**, the semantic retrieval layer.
Each vector stores `entry_id`, `source_version`, `content_hash`, model,
dimensions, distance measure, and schema version. Vertex AI generates vectors;
Firestore stores and searches them. Exact and prefix lookup run first. Semantic
results return KBBI records and citations. Unknown or weak matches return no
authoritative result. Generic corpora may occupy a separate enrichment
namespace for context or frequency, never correction decisions.

## Build plan (time budget)

| Block | % of hours | Hours | Output |
|---|---|---|---|
| Skeleton (end-to-end thinnest path) | first 25% | 10 | Gemini + ADK + Cloud path, one official fixture, one Vertex embedding in Firestore |
| Core features (max 2–3 must-haves) | 25→60% | 20 | resumable full-corpus path, canonical store, KBBI-only retrieval |
| Polish + submission artifacts | last 15%+slack | 10 | provenance UI, manifests, README, diagram, video, proof |

Must-have features: full-corpus/resumable KBBI ingestion architecture;
Gemini + Google agent framework + Google Cloud proof; KBBI-only embedding/RAG
with citations and fail-closed unknowns; exact and semantic lookup; stable
downstream projection manifest.
Explicitly NOT building in this ticket: Hunspell/LibreOffice, cspell, Babel,
Polyglossia, Rabu Baku, stenotype, generic-corpus RAG, full grammar correction,
SurrealDB, Milvus, Vertex AI Vector Search migration, or CockroachDB dependency.

## Agent readiness progression

Factory readiness target: begin at Levels 1–2, establish Level 3 controls,
progressively measure Level 4, and treat Level 5 as a later Transform phase.
Factory requires 80% of a level's criteria before unlocking the next level.

| Level | Aksantara evidence |
|---|---|
| 1 Functional | README, pinned dependencies, formatter, linter, type checker, unit tests, local replay |
| 2 Documented | project `AGENTS.md`, setup/deploy/debug docs, authority policy, pre-commit, ownership boundaries |
| 3 Standardized | CI, integration/replay tests, secret/dependency scans, structured logs, run/trace IDs, human review ownership |
| 4 Optimized | cached fast CI, failure/flaky metrics, freshness/parser/vector cost dashboards, rollback and deployment metrics |
| 5 Autonomous | structured task discovery, bounded agent decomposition, least privilege, reviewable changes, deterministic gates, recovery and self-improving estimates |

Run `/readiness-report` after the repository has an `origin` remote and
Level 1–2 foundation. Use `/readiness-fix` only with explicit scope, review
every change, run validation, commit intentionally, and rerun the report.
Level 5 must not be claimed from agent count alone.

## Checkpoint tracker (check honestly, on the clock)

| Clock | Checkpoint | Pass? | Notes (velocity felt vs actual?) |
|---|---|---|---|
| T+25% | Working skeleton runs end-to-end | ☐ | |
| T+40% | Mid-triage: re-score B1/B6 vs reality; kill off-path features | ☐ | |
| T+60% | Feature freeze — copy only beyond this | ☐ | |
| T−8h | QA sweep started | ☐ | |
| T−4h | CODE LOCK — docs/copy only; backup video recorded | ☐ | |
| T−2h | Rehearsed demo ×5, timed | ☐ | |
| T−30m | SUBMITTED | ☐ | |

AI-delegation log (what was AI-built, what did I personally verify?): ________________________________

---

## Submission QA (all boxes before submit)

- [ ] Every required field filled; all URLs public and logged-out testable
- [ ] Required tech listed under Built With
- [ ] Video: problem (+ citable stat) → solution → user demo → tech how → close; ~60% explain / 40% demo; within length limit
- [ ] Backup video recorded & linked
- [ ] Description embeds GIF/images; markdown-formatted
- [ ] Honesty pass: what-works/what-doesn't stated plainly
- [ ] Submitted ≥30 min before deadline

---

## Post-mortem (within 48h of results — this is the compounding asset)

| Log | Prediction | Actual | Lesson |
|---|---|---|---|
| Gate A near-misses (which gate almost killed winner / passed loser?) | | | |
| Per-criterion score vs judge feedback | | | |
| Velocity: felt vs measured at checkpoints | | | |
| Window verdict: edge alive at judging? | | | |
| Placement / prizes | | | |

Weight/bar changes proposed (need 3 consistent events before editing playbook): ________________________________
