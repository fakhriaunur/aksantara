#!/usr/bin/env bash
set -euo pipefail
# Tech debt tracking — ensures every TODO/FIXME references a tracked issue.
# Convention: TODO(#123) or TODO(TICKET-123) or FIXME(#123)
# SonarQube-style SQALE debt is mimicked via ruff/pylint fixme checks; this script is the CI gate.

echo "=== Tech debt tracking — TODO/FIXME must link to issue ==="

# Find all TODO/FIXME that are NOT linked to an issue
UNTRACKED=$(grep -rn -E "TODO|FIXME" --include="*.py" src/ tests/ 2>/dev/null | grep -v "TODO(#" | grep -v "TODO(TICKET" | grep -v "FIXME(#" | grep -v "FIXME(TICKET" || true)

if [ -n "$UNTRACKED" ]; then
  echo "FAIL: Untracked tech debt markers found — each TODO/FIXME must include an issue link:"
  echo "$UNTRACKED"
  echo ""
  echo "Expected format: TODO(#101): description — tracked in issue #101"
  echo "Or: FIXME(TICKET-123): description"
  echo "This enforces SQALE-style debt tracking (SonarQube has it built-in; we enforce via CI)."
  exit 1
else
  echo "No untracked TODOs — checking tracked debt count for dashboard..."
fi

# Report tracked debt for visibility (informational, not failing)
TRACKED=$(grep -rn -E "TODO\(#|TODO\(TICKET|FIXME\(#|FIXME\(TICKET" --include="*.py" src/ tests/ 2>/dev/null || true)
if [ -n "$TRACKED" ]; then
  COUNT=$(echo "$TRACKED" | wc -l)
  echo "Tracked debt markers: $COUNT"
  echo "$TRACKED" | head -20
else
  echo "No tracked debt markers — clean"
fi

# Also check for pylint fixme if available (optional)
if command -v pylint >/dev/null 2>&1; then
  echo "--- pylint fixme check (informational) ---"
  pylint --disable=all --enable=fixme src/ 2>&1 | head -20 || true
fi

echo "Tech debt gate: PASSED"
