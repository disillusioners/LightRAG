#!/usr/bin/env bash
# Test Pack: streaming_release_test
# Scope: Workspace release safety on /query/stream (success, error,
#        midstream disconnect, ASGI cancellation, no-double-release,
#        workspace header routing).
# Timeout: 180s (cold import of lightrag.api.* can be slow on first run)
set -euo pipefail
SCRIPT_TIMEOUT=180
timeout "$SCRIPT_TIMEOUT" python -m pytest tests/workspace/test_streaming_release.py -v --tb=short -q
EXIT_CODE=$?
if [ $EXIT_CODE -eq 124 ]; then
  echo "=== Test Pack: streaming_release_test ==="
  echo "RESULT: TIMEOUT"
  exit 124
elif [ $EXIT_CODE -eq 0 ]; then
  echo "=== Test Pack: streaming_release_test ==="
  echo "RESULT: PASS"
  exit 0
else
  echo "=== Test Pack: streaming_release_test ==="
  echo "RESULT: FAIL"
  exit 1
fi
