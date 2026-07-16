#!/usr/bin/env bash
# Test Pack: sanitization_alignment_test
# Scope: Backend (Python) vs Frontend (TS) LIGHTRAG-WORKSPACE sanitization
#        alignment; covers get_workspace_from_request, regex equivalence,
#        and a WorkspaceRegistry roundtrip.
# Timeout: 120s (unit pack).
set -euo pipefail
SCRIPT_TIMEOUT=120
timeout "$SCRIPT_TIMEOUT" python -m pytest tests/workspace/test_sanitization_alignment.py -v --tb=short -q
EXIT_CODE=$?
if [ $EXIT_CODE -eq 124 ]; then
  echo "=== Test Pack: sanitization_alignment_test ==="
  echo "RESULT: TIMEOUT"
  exit 124
elif [ $EXIT_CODE -eq 0 ]; then
  echo "=== Test Pack: sanitization_alignment_test ==="
  echo "RESULT: PASS"
  exit 0
else
  echo "=== Test Pack: sanitization_alignment_test ==="
  echo "RESULT: FAIL"
  exit 1
fi
