#!/usr/bin/env bash
# Test Pack: role_llm_builder_test
# Scope: register_role_llm_builder integration in WorkspaceManager._create_rag_instance
# Verifies role_llm_funcs and _llm_role_builder are populated on default,
# newly-created, and recreated-after-eviction LightRAG instances.
# Timeout: 120s (unit pack)
set -euo pipefail
SCRIPT_TIMEOUT=120
# Run the test with a hard timeout
timeout "$SCRIPT_TIMEOUT" python -m pytest tests/workspace/test_role_llm_builder.py -v --tb=short -q
EXIT_CODE=$?
if [ $EXIT_CODE -eq 124 ]; then
  echo "=== Test Pack: role_llm_builder_test ==="
  echo "RESULT: TIMEOUT"
  exit 124
elif [ $EXIT_CODE -eq 0 ]; then
  echo "=== Test Pack: role_llm_builder_test ==="
  echo "RESULT: PASS"
  exit 0
else
  echo "=== Test Pack: role_llm_builder_test ==="
  echo "RESULT: FAIL"
  exit 1
fi
