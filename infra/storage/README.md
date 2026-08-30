# Storage — Aksantara

GCS layout for the canonical fuel layer:

```
gs://<bucket>/raw/{source}/{date}/{hash}.html     — immutable raw snapshots (sha256-named)
gs://<bucket>/canonical/{version}/entries/*.json  — validated canonical corpus per release
gs://<bucket>/manifests/{version}.json            — release manifests (embedding model, dims, hashes)
gs://<bucket>/replay/fixtures/                    — golden fixtures for deterministic replay
```

- Raw snapshots are immutable; `contentHash` (hex sha256, 64 chars) is the filename.
- Canonical releases are append-only; rollback flips `config/current_version` in Firestore.
- Buckets are created by `scripts/bootstrap_gcp.sh` (idempotent, least-privilege SA).
- Local mirror of `raw/` lives in `tests/replay/fixtures/` for offline replay.

No secrets are stored in GCS objects; service-account keys must not be committed.
