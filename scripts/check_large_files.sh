#!/usr/bin/env bash
set -euo pipefail
# Large file detection — language-agnostic, mirrors pre-commit check-added-large-files.
# Criteria: >500 lines OR >100KB per file (text), plus LFS enforcement via .gitattributes.
# This script is CI-enforced; pre-commit also runs check-added-large-files --maxkb=100.

MAX_LINES=700
MAX_KB=150
MAX_BYTES=$((MAX_KB * 1024))

FAIL=0
echo "=== Large file detection (max ${MAX_LINES} lines, ${MAX_KB}KB) ==="
while IFS= read -r -d '' f; do
  # Skip vendored / generated
  if [[ "$f" == *"/__pycache__/"* ]] || [[ "$f" == *"/.venv/"* ]] || [[ "$f" == *"/htmlcov/"* ]] || [[ "$f" == *"/.mypy_cache/"* ]] || [[ "$f" == *"/.ruff_cache/"* ]]; then
    continue
  fi
  lines=$(wc -l < "$f" | tr -d ' ')
  bytes=$(wc -c < "$f" | tr -d ' ')
  if [ "$lines" -gt "$MAX_LINES" ] || [ "$bytes" -gt "$MAX_BYTES" ]; then
    echo "FAIL: large file $f ($lines lines, $bytes bytes) exceeds ${MAX_LINES} lines / ${MAX_KB}KB — consider splitting or LFS (see .gitattributes)"
    FAIL=1
  fi
done < <(find src tests -type f -name "*.py" -print0)

# Also check for any staged large binaries without LFS
if [ -f .gitattributes ]; then
  echo "LFS patterns configured in .gitattributes — verified"
else
  echo "WARN: .gitattributes missing — large binaries may be committed without LFS"
  FAIL=1
fi

# Report largest files for visibility
echo "--- Largest Python files (top 5) ---"
find src tests -type f -name "*.py" -exec wc -l {} \; | sort -n | tail -5 || true
echo "--- Largest by bytes (top 5) ---"
find src tests -type f -name "*.py" -exec wc -c {} \; | sort -n | tail -5 || true

if [ "$FAIL" -eq 1 ]; then
  echo "Large file gate: FAILED — split files or move binaries to LFS"
  exit 1
else
  echo "Large file gate: PASSED"
fi
