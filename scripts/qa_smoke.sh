#!/usr/bin/env bash
set -euo pipefail

# Aksantara functional QA smoke. Uses an ephemeral FastAPI server and no GCP credentials.
PORT=8000
if [[ "${1:-}" == "--ephemeral" ]]; then
  PORT=$(python3 -c 'import socket; s=socket.socket(); s.bind(("",0)); print(s.getsockname()[1]); s.close()')
fi
HOST=127.0.0.1
BASE="http://${HOST}:${PORT}"
RUN_DIR=$(mktemp -d "${TMPDIR:-/tmp}/aksantara-qa.XXXXXX")
LOG="${RUN_DIR}/server.log"
PID=""
cleanup() {
  if [[ -n "${PID}" ]]; then
    kill "${PID}" 2>/dev/null || true
    wait "${PID}" 2>/dev/null || true
  fi
  rm -rf "${RUN_DIR}"
}
trap cleanup EXIT
export PYTHONPATH="${PYTHONPATH:-src}"
export QA_RUN_DIR="${RUN_DIR}"

echo "=== Aksantara QA smoke — ${BASE} ==="
nohup python -m uvicorn aksantara.api.routes:create_app --factory --host "${HOST}" --port "${PORT}" >"${LOG}" 2>&1 &
PID=$!
for i in $(seq 1 15); do
  if curl -sf "${BASE}/health" >/dev/null 2>&1; then break; fi
  if ! kill -0 "${PID}" 2>/dev/null; then cat "${LOG}"; exit 1; fi
  sleep 1
  [[ "${i}" != 15 ]] || { cat "${LOG}"; exit 1; }
done
pass() { echo "  PASS $1"; }
fail() { echo "  FAIL $1 ($2)" >&2; cat "${LOG}" >&2; exit 1; }
json_check() { python3 -c "$1"; }

echo "--- Driving read-only API flows ---"
curl -sf "${BASE}/health" | json_check 'import sys,json; d=json.load(sys.stdin); assert d.get("status")=="ok",d' || fail 'GET /health' 'invalid response'
pass 'GET /health'
curl -sf "${BASE}/versions/current" | json_check 'import sys,json; d=json.load(sys.stdin); assert d.get("version"),d' || fail 'GET /versions/current' 'missing version'
pass 'GET /versions/current'
curl -sf "${BASE}/docs" | grep -qi 'swagger\|openapi' || fail 'GET /docs' 'missing OpenAPI UI'
pass 'GET /docs'
curl -sf "${BASE}/entries?q=feb&limit=5" | json_check 'import sys,json; d=json.load(sys.stdin); assert isinstance(d.get("results"),list) and "count" in d,d' || fail 'GET /entries?q=feb' 'invalid shape'
pass 'GET /entries?q=feb (prefix)'
HTTP_CODE=$(curl -sS -o "${RUN_DIR}/februari.json" -w '%{http_code}' "${BASE}/entries/februari")
if [[ "${HTTP_CODE}" == 200 ]]; then json_check 'import json; d=json.load(open(__import__("os").environ["QA_RUN_DIR"]+"/februari.json")); assert d.get("lema") or d.get("entry",{}).get("lema"),d' || fail 'GET /entries/februari' 'invalid entry'; pass 'GET /entries/februari (200)'
elif [[ "${HTTP_CODE}" == 404 ]]; then pass 'GET /entries/februari (404 empty-index contract)'
else fail 'GET /entries/februari' "unexpected ${HTTP_CODE}"; fi
curl -sf "${BASE}/search/semantic?q=bulan%20kedua&limit=3" | json_check 'import sys,json; d=json.load(sys.stdin); assert isinstance(d.get("results"),list) and "count" in d,d' || fail 'GET /search/semantic' 'invalid fail-closed shape'
pass 'GET /search/semantic (fail-closed)'
HTTP_CODE=$(curl -sS -o "${RUN_DIR}/pebruari.json" -w '%{http_code}' "${BASE}/relations/nonstandard/Pebruari")
if [[ "${HTTP_CODE}" == 200 ]]; then json_check 'import json,os; d=json.load(open(os.environ["QA_RUN_DIR"]+"/pebruari.json")); assert "standard_form" in d or "entry" in d,d' || fail 'GET /relations/nonstandard/Pebruari' 'invalid relation'; pass 'GET /relations/nonstandard/Pebruari (200)'
elif [[ "${HTTP_CODE}" == 404 ]]; then pass 'GET /relations/nonstandard/Pebruari (404 empty-index contract)'
else fail 'GET /relations/nonstandard/Pebruari' "unexpected ${HTTP_CODE}"; fi
python -m pytest tests/replay -q >/dev/null || fail 'replay acceptance' 'replay failed'
pass 'Februari deterministic replay'
echo '=== QA smoke PASSED ==='
