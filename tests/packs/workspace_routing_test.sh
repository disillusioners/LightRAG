#!/usr/bin/env bash
# Test Pack: workspace_routing_test
# Scope: tests/workspace/test_workspace_routing.py
#
# Verifies that the per-request ``LIGHTRAG-WORKSPACE`` header is parsed,
# sanitized (hyphens preserved), and routed through
# ``workspace_mgr.acquire`` / ``workspace_mgr.release`` exactly once
# across graph routes, query routes, streaming, and exception paths.
#
# Timeout: 180s (unit pack).
set -euo pipefail
SCRIPT_TIMEOUT=180
timeout "$SCRIPT_TIMEOUT" python -m pytest tests/workspace/test_workspace_routing.py -v --tb=short -q
EXIT_CODE=$?
if [ $EXIT_CODE -eq 124 ]; then
  echo "=== Test Pack: workspace_routing_test ==="
  echo "RESULT: TIMEOUT"
  exit 124
elif [ $EXIT_CODE -eq 0 ]; then
  echo "=== Test Pack: workspace_routing_test ==="
  echo "RESULT: PASS"
  exit 0
else
  echo "=== Test Pack: workspace_routing_test ==="
  echo "RESULT: FAIL"
  exit 1
fi
