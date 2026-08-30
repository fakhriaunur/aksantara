#!/usr/bin/env bash
# Aksantara GCP bootstrap — project, APIs, Firestore Native, buckets, indexes (idempotent).
# Usage:
#   ./scripts/bootstrap_gcp.sh [PROJECT_ID] [LOCATION] [BUCKET_NAME]
# Env fallbacks: GOOGLE_CLOUD_PROJECT, GOOGLE_CLOUD_LOCATION (default asia-southeast1)
# Requires: gcloud authenticated with project creation permission.
set -euo pipefail

PROJECT_ID="${1:-${GOOGLE_CLOUD_PROJECT:-}}"
LOCATION="${2:-${GOOGLE_CLOUD_LOCATION:-asia-southeast1}}"
BUCKET_NAME="${3:-${AKSANTARA_GCS_BUCKET:-}}"

if [[ -z "${PROJECT_ID}" ]]; then
  echo "ERROR: PROJECT_ID is required (arg 1 or GOOGLE_CLOUD_PROJECT)" >&2
  exit 1
fi

# Infer bucket when not supplied — deterministic, globally unique via project suffix.
if [[ -z "${BUCKET_NAME}" ]]; then
  BUCKET_NAME="${PROJECT_ID}-aksantara"
fi

# Firestore database location — Native mode only accepts a subset of regions for
# asia-southeast1; this matches the spec.
FIRESTORE_LOCATION="asia-southeast1"
FIRESTORE_DATABASE="(default)"

echo "==> Aksantara bootstrap: project=${PROJECT_ID} location=${LOCATION} bucket=${BUCKET_NAME}"

# -- project (idempotent: describe then create if missing) -----------------
if ! gcloud projects describe "${PROJECT_ID}" >/dev/null 2>&1; then
  echo "==> Creating GCP project ${PROJECT_ID}"
  gcloud projects create "${PROJECT_ID}" --name="Aksantara" --quiet
else
  echo "==> Project ${PROJECT_ID} exists, skipping create"
fi

gcloud config set project "${PROJECT_ID}" >/dev/null

# -- enable APIs (idempotent) ---------------------------------------------
echo "==> Enabling required APIs"
gcloud services enable \
  aiplatform.googleapis.com \
  firestore.googleapis.com \
  storage.googleapis.com \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  logging.googleapis.com \
  --project="${PROJECT_ID}" --quiet

# -- Firestore Native in asia-southeast1 (idempotent) ----------------------
echo "==> Ensuring Firestore Native database in ${FIRESTORE_LOCATION}"
if ! gcloud firestore databases describe --project="${PROJECT_ID}" --database="${FIRESTORE_DATABASE}" >/dev/null 2>&1; then
  gcloud firestore databases create \
    --project="${PROJECT_ID}" \
    --database="${FIRESTORE_DATABASE}" \
    --location="${FIRESTORE_LOCATION}" \
    --type=firestore-native \
    --quiet
else
  echo "==> Firestore database ${FIRESTORE_DATABASE} exists, skipping create"
fi

# -- GCS buckets (idempotent) ---------------------------------------------
echo "==> Ensuring GCS buckets"
for suffix in "" "-raw" "-canonical" "-manifests"; do
  # Single bucket with prefixes is the default; create if it does not exist.
  if [[ "${suffix}" == "" ]]; then
    bucket="gs://${BUCKET_NAME}"
  else
    # Optional split buckets — skip if single-bucket mode; keep command as no-op.
    continue
  fi
  if ! gsutil ls -b "${bucket}" >/dev/null 2>&1 && ! gcloud storage buckets describe "${bucket}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    echo "   Creating ${bucket} in ${LOCATION}"
    gcloud storage buckets create "${bucket}" --project="${PROJECT_ID}" --location="${LOCATION}" --uniform-bucket-level-access --quiet || \
      gsutil mb -p "${PROJECT_ID}" -l "${LOCATION}" -b on "${bucket}" || true
  else
    echo "   Bucket ${bucket} exists, skipping"
  fi
done

# Ensure canonical prefix placeholders (GCS is flat; objects create prefixes on write).
echo "==> Bucket prefixes are created on first object write (raw/, canonical/, manifests/)"

# -- Firestore composite indexes (idempotent: create or update) ------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INDEXES_FILE="${SCRIPT_DIR}/../infra/firestore/indexes.json"

if [[ -f "${INDEXES_FILE}" ]]; then
  echo "==> Applying Firestore indexes from ${INDEXES_FILE}"
  # Newer gcloud supports firestore indexes composite create from JSON via
  # `gcloud firestore indexes composite create --file=`. Fall back to
  # per-index commands for older gcloud, and tolerate already-exists errors.

  if gcloud firestore indexes composite create --help >/dev/null 2>&1; then
    # Attempt file-based bulk apply when supported; ignore if the flag differs.
    if gcloud firestore indexes composite create --file="${INDEXES_FILE}" --project="${PROJECT_ID}" --quiet 2>/dev/null; then
      echo "   Indexes applied via file import"
    else
      echo "   File import not supported by this gcloud version — applying indexes individually"
      # Apply vector index on vector_entries (DOT_PRODUCT, flat, 768d) + composite fields
      gcloud firestore indexes composite create \
        --project="${PROJECT_ID}" \
        --database="${FIRESTORE_DATABASE}" \
        --collection-group=vector_entries \
        --field-config field-path=source_kind,order=ascending \
        --field-config field-path=edition,order=ascending \
        --field-config field-path=embedding_vector,vector-config='{"dimension":"768","flat":{}}' \
        --query-scope=COLLECTION --quiet 2>&1 || echo "   vector_entries index already exists or pending"
      gcloud firestore indexes composite create \
        --project="${PROJECT_ID}" \
        --database="${FIRESTORE_DATABASE}" \
        --collection-group=entries \
        --field-config field-path=source_kind,order=ascending \
        --field-config field-path=edition,order=ascending \
        --field-config field-path=lema,order=ascending \
        --query-scope=COLLECTION --quiet 2>&1 || echo "   entries composite index already exists or pending"
      gcloud firestore indexes composite create \
        --project="${PROJECT_ID}" \
        --database="${FIRESTORE_DATABASE}" \
        --collection-group=entries \
        --field-config field-path=lema,order=ascending \
        --query-scope=COLLECTION --quiet 2>&1 || echo "   entries lema index already exists or pending"
    fi
  else
    # Fallback for gcloud without firestore indexes composite subcommand.
    echo "   gcloud firestore indexes composite not available — create indexes via Console or upgrade gcloud"
    echo "   Expected indexes from ${INDEXES_FILE}:"
    cat "${INDEXES_FILE}"
  fi
else
  echo "WARNING: indexes file not found at ${INDEXES_FILE}, skipping index creation" >&2
fi

# -- Least-privilege service accounts (optional, idempotent) ---------------
echo "==> Ensuring service accounts (aksantara-runner, aksantara-api)"
for sa in aksantara-runner aksantara-api; do
  sa_email="${sa}@${PROJECT_ID}.iam.gserviceaccount.com"
  if ! gcloud iam service-accounts describe "${sa_email}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    gcloud iam service-accounts create "${sa}" --project="${PROJECT_ID}" --display-name="${sa}" --quiet
    echo "   Created ${sa_email}"
  else
    echo "   SA ${sa_email} exists, skipping"
  fi
done

# Bind minimal roles (idempotent; additive only).
echo "==> Binding least-privilege IAM roles"
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:aksantara-runner@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/datastore.user" --condition=None --quiet >/dev/null 2>&1 || true
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:aksantara-runner@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/storage.objectAdmin" --condition=None --quiet >/dev/null 2>&1 || true
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:aksantara-runner@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/aiplatform.user" --condition=None --quiet >/dev/null 2>&1 || true
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:aksantara-api@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/datastore.viewer" --condition=None --quiet >/dev/null 2>&1 || true
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:aksantara-api@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/storage.legacyBucketReader" --condition=None --quiet >/dev/null 2>&1 || true

# -- summary --------------------------------------------------------------
echo ""
echo "==> Bootstrap complete"
echo "    Project: ${PROJECT_ID}"
echo "    Region:  ${LOCATION}"
echo "    Firestore: ${FIRESTORE_LOCATION} (${FIRESTORE_DATABASE}) Native"
echo "    Bucket:  gs://${BUCKET_NAME}"
echo "    Vector:  gemini-embedding-001 768d DOT_PRODUCT + threshold 0.70"
echo ""
echo "Next:"
echo "  1. cp .env.example .env  # fill GOOGLE_CLOUD_PROJECT=${PROJECT_ID}"
echo "  2. python scripts/import_corpus.py --lema Februari --project ${PROJECT_ID}"
echo "  3. python scripts/build_embeddings.py --version 2026-08-30.1 --project ${PROJECT_ID}"
echo "  4. python -m uvicorn aksantara.api.routes:create_app --factory --reload"
