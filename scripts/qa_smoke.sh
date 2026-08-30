#!/usr/bin/env bash
set -euo pipefail

# Aksantara interactive QA smoke — agent-followable, no GCP creds required.
# Starts an ephemeral FastAPI server in-memory, curls every cited endpoint,
# and verifies expected shapes. Exits 0 on success, non-zero on failure.
#
# Usage:
#   ./scripts/qa_smoke.sh            # uses port 8000, leaves logs in /tmp
#   ./scripts/qa_smoke.sh --ephemeral  # random port, auto-cleanup
#   mise run qa / mise run smoke     # via mise

PORT=8000
EPHEMERAL=0
if [[ "${1:-}" == "--ephemeral" ]]; then
  EPHEMERAL=1
  PORT=$(python3 -c 'import socket; s=socket.socket(); s.bind(("",0)); print(s.getsockname()[1]); s.close()')
fi

HOST="127.0.0.1"
BASE="http://${HOST}:${PORT}"
LOG="/tmp/aksantara-qa-smoke-${PORT}.log"
PID=""

cleanup() {
  if [[ -n "${PID}" ]]; then
    kill "${PID}" 2>/dev/null || true
    wait "${PID}" 2>/dev/null || true
  fi
  echo "Log: ${LOG}"
}
trap cleanup EXIT

echo "=== Aksantara QA smoke — port ${PORT} (ephemeral=${EPHEMERAL}) ==="
echo "Starting uvicorn (in-memory, no GCP creds)..."
# PYTHONPATH src is set via mise.toml env, but ensure for direct bash:
export PYTHONPATH="${PYTHONPATH:-src}"
# Use --factory to call create_app() via python -m to avoid PATH issues with mise
nohup python -m uvicorn aksantara.api.routes:create_app --factory --host "${HOST}" --port "${PORT}" >"${LOG}" 2>&1 &
PID=$!
echo "PID ${PID}, waiting for /health..."

# Wait up to 15s for health
for i in $(seq 1 15); do
  if curl -sf "${BASE}/health" >/dev/null 2>&1; then
    echo "Health OK after ${i}s"
    break
  fi
  if ! kill -0 "${PID}" 2>/dev/null; then
    echo "uvicorn failed to start — log:"
    cat "${LOG}"
    exit 1
  fi
  sleep 1
  if [[ "${i}" == "15" ]]; then
    echo "Timed out waiting for /health"
    cat "${LOG}"
    exit 1
  fi
done

pass() { echo "  PASS $1"; }
fail() { echo "  FAIL $1 ($2)"; exit 1; }

echo "--- Driving meaningful interactions ---"

# 1. GET /health
if curl -sf "${BASE}/health" | python3 -c "import sys, json; d=json.load(sys.stdin); assert d.get('status')=='ok', d; print('health ok', d.get('firestore'))"; then
  pass "GET /health"
else
  fail "GET /health" "non-200 or bad body"
fi

# 2. GET /versions/current
if curl -sf "${BASE}/versions/current" | python3 -c "import sys, json; d=json.load(sys.stdin); assert 'version' in d, d; print('versions', d['version'])"; then
  pass "GET /versions/current"
else
  fail "GET /versions/current" "missing version"
fi

# 3. GET /docs (OpenAPI)
if curl -sf "${BASE}/docs" | grep -qi "swagger\|openapi"; then
  pass "GET /docs"
else
  fail "GET /docs" "no swagger"
fi

# 4. GET /entries?q= prefix (in-memory index is empty on fresh boot, but endpoint should 200 with empty results)
# Seed at least one entry via the test helper? For smoke, we accept empty but valid shape.
if curl -sf "${BASE}/entries?q=feb&limit=5" | python3 -c "import sys, json; d=json.load(sys.stdin); assert 'results' in d and 'count' in d, d; print('prefix count', d['count'])"; then
  pass "GET /entries?q=feb (prefix)"
else
  fail "GET /entries?q=feb" "bad shape"
fi

# 5. GET /entries/februari -> may be 404 on empty index, which is expected for in-memory fresh boot.
# We verify the contract: either 200 with lema or 404 with detail.
set +e
HTTP_CODE=$(curl -s -o /tmp/qa_februari.json -w "%{http_code}" "${BASE}/entries/februari")
set -e
if [[ "${HTTP_CODE}" == "200" ]]; then
  python3 -c "import json; d=json.load(open('/tmp/qa_februari.json')); assert d.get('lema') or d.get('entry',{}).get('lema'), d; print('exact found', d.get('lema', d.get('entry',{}).get('lema')))"
  pass "GET /entries/februari (200)"
elif [[ "${HTTP_CODE}" == "404" ]]; then
  echo "  INFO /entries/februari 404 on empty index — expected for in-memory QA (seed via pytest fixtures for data)"
  pass "GET /entries/februari (404 expected, contract OK)"
else
  fail "GET /entries/februari" "unexpected ${HTTP_CODE}"
fi

# 6. GET /search/semantic?q= (fail-closed: should 200 with empty results when no vector backend)
if curl -sf "${BASE}/search/semantic?q=bulan%20kedua&limit=3" | python3 -c "import sys, json; d=json.load(sys.stdin); assert 'results' in d and isinstance(d['results'], list), d; print('semantic count', d['count'])"; then
  pass "GET /search/semantic?q=bulan kedua (fail-closed OK)"
else
  fail "GET /search/semantic" "bad shape"
fi

# 7. GET /relations/nonstandard/Pebruari -> may be 404 on empty index, same contract as #5
set +e
HTTP_CODE2=$(curl -s -o /tmp/qa_pebruari.json -w "%{http_code}" "${BASE}/relations/nonstandard/Pebruari")
set -e
if [[ "${HTTP_CODE2}" == "200" ]]; then
  python3 -c "import json; d=json.load(open('/tmp/qa_pebruari.json')); assert 'standard_form' in d or 'entry' in d, d; print('nonstandard', d.get('standard_form'))"
  pass "GET /relations/nonstandard/Pebruari (200)"
elif [[ "${HTTP_CODE2}" == "404" ]]; then
  echo "  INFO /relations/nonstandard/Pebruari 404 on empty index — contract OK"
  pass "GET /relations/nonstandard/Pebruari (404 expected)"
else
  fail "GET /relations/nonstandard/Pebruari" "unexpected ${HTTP_CODE2}"
fi

# 8. Replay slice sanity (pytest)
echo "--- Verifying replay slice ---"
if python -m pytest tests/replay -q >/dev/null 2>&1; then
  pass "pytest tests/replay"
else
  fail "pytest tests/replay" "replay failed"
fi

echo ""
echo "=== QA smoke PASSED — all endpoints exercised ==="
echo "Next: mise run coverage, mise run check, or ./scripts/qa_smoke.sh --ephemeral"
