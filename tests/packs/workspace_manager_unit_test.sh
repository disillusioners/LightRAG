#!/usr/bin/env bash
# Test Pack: workspace_manager_unit_test
# Scope: WorkspaceManager LRU cache, refcount, concurrency, eviction
# Timeout: 120s (unit pack)
set -euo pipefail
SCRIPT_TIMEOUT=120
# Run the test with a hard timeout
timeout "$SCRIPT_TIMEOUT" python -m pytest tests/workspace/test_workspace_manager.py -v --tb=short -q
EXIT_CODE=$?
if [ $EXIT_CODE -eq 124 ]; then
  echo "=== Test Pack: workspace_manager_unit_test ==="
  echo "RESULT: TIMEOUT"
  exit 124
elif [ $EXIT_CODE -eq 0 ]; then
  echo "=== Test Pack: workspace_manager_unit_test ==="
  echo "RESULT: PASS"
  exit 0
else
  echo "=== Test Pack: workspace_manager_unit_test ==="
  echo "RESULT: FAIL"
  exit 1
fi
