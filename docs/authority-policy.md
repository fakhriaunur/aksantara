# Authority Policy — Aksantara

## Principle
Official KBBI is sole lexical authority. No generic corpus or LLM may add or override canonical facts.

| Layer | Allowed role | Authority | Writes canonical? |
|---|---|---|---|
| Official KBBI VI | Lemmas, meanings, word classes, examples, pronunciation, standard/nonstandard | Sole lexical | Yes |
| Official PUEBI/EYD + Badan Bahasa rules | Orthography, punctuation, morphology, structural rules | Separate rule authority | No (rule adapter only) |
| Sipebi public/nonconfidential | Compatibility, morphology reference | Reference/adapter | No |
| Community KBBI dumps | Full-corpus bootstrap candidate | gov-derived, never live official | No |
| kbbi.web.id / mirrors | Fallback lookup | Mirror, edition-labeled | No |
| SPECIL, CC100, OSCAR, Leipzig, news | Evaluation, context, frequency, example discovery | Enrichment only | No |
| Gemini / embeddings | Transformation, retrieval representation | Never authority | No |

## Provenance
Every record preserves: `sourceRef, sourceKind, edition, sourceVersion, retrievedAt, contentHash, parserVersion, transformVersion, schemaVersion, reviewStatus`

- `contentHash` = hex sha256 of raw snapshot bytes (lower-case, 64 chars)
- `parserVersion` pinned per build; mismatch → quarantine
- Missing source fields stay missing unless separately labeled enrichment exists

## Rules
- Pebruari→Februari and Nopember→November require explicit authoritative relationship or approved rule data — never inferred by LLM
- Unknown semantic queries return no authoritative answer (fail-closed, `results:[]`)
- Downstream consumers cannot mutate upstream canonical records — they consume versioned manifests only
- Direct/fallback overlap compared field-by-field; conflicts quarantined, human-reviewed
- AI output is proposal, never canonical truth

## Human Review Gates
- Source conflicts, lexical changes, public claims, releases — all require human approval before promotion
- `ValidationPolicy` (see `src/aksantara/domain/authority.py`) enforces `quarantine_on_status_conflict`, `fail_closed_on_unknown_semantic`, `require_human_review_for_conflicts/release`
