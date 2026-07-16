#!/usr/bin/env bash
# Test Pack: ollama_workspace_routing_test
# Scope: tests/workspace/test_ollama_workspace_routing.py
#
# Verifies that the Ollama-compatible API router (``/api/generate``,
# ``/api/chat``, ``/api/tags``, ``/api/ps``, ``/api/version``) routes
# requests to the right workspace via ``workspace_mgr.acquire`` /
# ``workspace_mgr.release`` and that workspace-independent endpoints
# (``/api/version``, ``/api/tags``, ``/api/ps``) never take a per-request
# refcount.
#
# Timeout: 180s (unit pack).
set -euo pipefail
SCRIPT_TIMEOUT=180
timeout "$SCRIPT_TIMEOUT" python -m pytest tests/workspace/test_ollama_workspace_routing.py -v --tb=short -q
EXIT_CODE=$?
if [ $EXIT_CODE -eq 124 ]; then
  echo "=== Test Pack: ollama_workspace_routing_test ==="
  echo "RESULT: TIMEOUT"
  exit 124
elif [ $EXIT_CODE -eq 0 ]; then
  echo "=== Test Pack: ollama_workspace_routing_test ==="
  echo "RESULT: PASS"
  exit 0
else
  echo "=== Test Pack: ollama_workspace_routing_test ==="
  echo "RESULT: FAIL"
  exit 1
fi
