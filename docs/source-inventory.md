# Source Inventory — KBBI

Gov-first priority. Access method, license, last update, schema, and cadence recorded per source.

| Source | URL | Access | Kind | Edition | License | Schema | Cadence | Provenance tag |
|---|---|---|---|---|---|---|---|---|
| KBBI VI Daring (official) | https://kbbi.kemdikbud.go.id | HTTPS GET `/entri/{lema}` | official-live | VI | Government data, review before bulk redistribution | HTML entry pages | Continuous updates (additions, changed meanings, word-class, examples, inactive) | `official-live` |
| Badan Bahasa PUEBI/EYD | https://badanbahasa.kemdikbud.go.id | HTTPS | rule | — | Official structural material | Rule text | Periodic | `rule` |
| Sipebi | https://kbbi.kemdikbud.go.id/Sipebi | HTTPS | sipebi | — | Public/nonconfidential | Morphology/compat | Ongoing | `sipebi` |
| Community KBBI dumps | academic repos, data.kemdikbud mirrors | git/http snapshot | gov-derived | III-VI labeled | Bootstrap only, verify vintage | JSON/HTML dump | Labeled vintage, not live | `gov-derived` |
| kbbi.web.id | https://kbbi.web.id | HTTPS | mirror | varies | Fallback, edition-labeled | Mirror HTML | Unknown | `fallback` |
| SPECIL | published corpus 180k tokens | download | evaluation | — | Evaluation only | Token corpus | Static | `evaluation` |
| CC100/OSCAR/Leipzig/news | various | bulk | enrichment | — | Enrichment only | Text corpora | Static | `enrichment` |

## Fetch Policy
- Official access first, labeled fallback second, enrichment never canonical
- Low concurrency, bounded retries, caching, `respect robots`, no unapproved proxy/scraping bypass
- Every fetch stores `url, sourceKind, edition, sourceVersion, retrievedAt, contentHash`

## Rights
No bulk redistribution until reviewed. Keep private/local processing and source links if rights unclear. Lower freshness claim, do not relabel fallback data.

## Cadence Evidence
- KBBI VI Daring update notes: https://kbbi.kemdikbud.go.id/Beranda/Pemutakhiran
- Sipebi docs: https://kbbi.kemdikbud.go.id/Sipebi/SeputarUrunDaya
