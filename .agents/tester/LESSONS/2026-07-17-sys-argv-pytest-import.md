# Lesson: sys.argv Must Be Seeded Before lightrag.api Imports in Tests

**Date**: 2026-07-17
**Phase**: Workspace Isolation v2 Phase 3

## Problem
The `lightrag.api` modules call `parse_args()` at module import time, which reads `sys.argv`. When pytest runs, `sys.argv` contains the test path and flags, causing argparse to raise `unrecognized arguments`.

## Fix
At the TOP of every test file that imports `lightrag.api.*`:
```python
import sys
sys.argv = sys.argv[:1]  # or sys.argv = ["lightrag-server"]
```
This must come BEFORE any `from lightrag.api...` import.

## Affected Files
All new workspace test files: test_workspace_routes.py, test_workspace_routing.py, test_ollama_workspace_routing.py, test_streaming_release.py, test_backward_compat.py, test_sanitization_alignment.py.

## Prevention
Consider making the argparse call lazy (inside a function) rather than at module load, so tests don't need to hack sys.argv.
