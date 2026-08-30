# Downstream Contract — Aksantara

## 1. Rule

Downstream tracks consume versioned manifests and never mutate canonical records. Every projection references exact source hashes and model versions.

## 2. Consumers

| Track | Consumes | Owns | Separate ticket |
|-------|----------|------|-----------------|
| Aksantara Hunspell | KBBI lexical projection + reviewed affix rules | `.dic`, `.aff`, `.oxt` | Yes |
| Aksantara cspell | KBBI word projection + technical vocabulary | Code dictionary | Yes |
| Aksantara Babel | KBBI/rule manifests | Dates, captions, aliases, hyphenation | Yes |
| Aksantara Polyglossia | Reviewed locale/rule adapter | Modern Unicode localization | Yes |
| Aksantara Rabu Baku | KBBI standard/nonstandard relations (`bentuk_baku`/`bentuk_tidak_baku`, e.g. `Pebruari→Februari`) | Weekly content mechanic | Yes |

No track may invent lemmas, meanings, or standard forms.

## 3. Manifest schema (canonical)

Each release publishes `manifests/{version}.json`:

```json
{
  "version": "2026-08-30.1",
  "created_at": "2026-08-30T03:00:00Z",
  "edition": "VI",
  "parser_version": "0.1.0",
  "transform_version": "0.1.0",
  "embedding": {
    "model": "gemini-embedding-001",
    "task_type_document": "RETRIEVAL_DOCUMENT",
    "task_type_query": "RETRIEVAL_QUERY",
    "dimensions": 768,
    "distance_measure": "DOT_PRODUCT"
  },
  "entries_count": 1,
  "source": {
    "kind": "official-live",
    "url": "https://kbbi.kemdikbud.go.id/entri/februari",
    "content_hash": "<64-hex-sha256>",
    "retrieved_at": "2026-08-30T03:00:00Z"
  },
  "artifacts": {
    "canonical_gcs_prefix": "gs://<bucket>/canonical/2026-08-30.1/",
    "raw_gcs_prefix": "gs://<bucket>/raw/"
  }
}
```

Downstream projections include `generator` and `source_hash` in their own manifests for end-to-end traceability.

## 4. Retrieval contract (Aksantara Pramana)

- **Order:** `exact` → `prefix` → `semantic` (vector). Exact and prefix run before semantic.
- **Vector index:** Firestore composite `source_kind ASC, edition ASC` + flat KNN `find_nearest(distance_measure=DOT_PRODUCT, distance_result_field="vector_distance", distance_threshold=0.70)`.
- **Fail-closed:** Unknown or weak semantic queries return `{"results": []}` — never a low-confidence authoritative claim.
- **Citations:** Every API result includes `source {url, edition, source_version, retrievedAt, contentHash, parserVersion}`, `retrieval {mode, distance, threshold}`, and `vector_distance`.
- **Auth:** No unauthenticated canonical writes; read endpoints are `GET /entries/{lema}`, `GET /search/semantic?q=...&limit=10`, `GET /health`.

## 5. Stability guarantees

- Field names in `KBBIEntry` are stable; additive fields are optional. Renames require a major `transform_version` bump and dual-serve window.
- Raw snapshots are immutable; canonical releases are append-only. Rollback is atomic pointer flip of `config/current_version` to prior `manifests/{version}.json`; previous `canonical/{version}` remains re-hydratable in GCS.
- Generic corpora occupy a separate enrichment namespace and cannot appear in `entries/` or `vector_entries/`.

## 6. Example: Februari slice

- `GET /entries/februari` → one `KBBIEntry` with provenance, `bentuk_tidak_baku` includes `Pebruari` when present in source.
- `GET /search/semantic?q=bulan kedua` → `Februari` with citation and `vector_distance`.
- `GET /search/semantic?q=xyzabc123` → `{"results":[]}`.
- `GET /search/semantic?q=Pebruari` → normalized to `Februari` via `bentuk_tidak_baku` → `bentuk_baku` relation, with citation.

## 7. Version discovery

Clients read `config/current_version` to discover the live release. They must verify `content_hash` against the manifest before caching projections.
