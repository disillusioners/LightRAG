#!/usr/bin/env bash
set -euo pipefail
# Run new backward-compat tests + existing workspace tests (must not break)
timeout 180 python -m pytest tests/workspace/test_backward_compat.py -v --tb=short -q
EXIT_CODE=$?
if [ $EXIT_CODE -eq 124 ]; then echo "RESULT: TIMEOUT"; exit 124
elif [ $EXIT_CODE -eq 0 ]; then echo "RESULT: PASS"; exit 0
else echo "RESULT: FAIL"; exit 1; fi
