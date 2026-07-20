# Lesson: Quick Fixes Applied During Merge Verification (2026-07-19)

## Commit `25bfca30` — streaming test fake
- **File:** `tests/workspace/test_streaming_release.py:305`
- **Change:** +1 line (`self.include_progress = False` in `_FakeQueryRequest.__init__`)
- **Root cause:** Upstream added `include_progress` field to `QueryRequest`; test double not updated.
- **Tests fixed:** 3 (test_streaming_release_on_midstream_disconnect, test_streaming_release_on_asgi_cancellation, test_streaming_workspace_held_until_body_completes)
- **Note:** This commit ALSO concluded the pending git merge (2-parent merge commit). The merge conclusion was a side effect of committing during a mid-merge state.

## Commit `5a565de3` — API route patch paths
- **Files:** `tests/api/routes/test_login_route.py` (+9/-1), `tests/api/routes/test_query_stream_routes.py` (+26/-6)
- **Changes:**
  1. Patch target `lightrag.api.lightrag_server.LightRAG` → `lightrag.api.workspace_manager.LightRAG` (v2 moved construction into WorkspaceManager)
  2. `FakeWorkspaceManager(HangingRag())` wrap + `http_request` mock param for one streaming endpoint
- **Tests fixed:** 12 (4 login + 8 query-stream)

## Pattern
Both fixes are the canonical v2 migration patterns documented in:
- LESSONS/2026-07-17-fake-workspace-manager-helper.md (FakeWorkspaceManager wrap)
- LESSONS/2026-07-17-sys-argv-pytest-import.md (sys.argv seeding)
The merge re-introduced old code (from main) that needed the same v2 adaptations.

## What was NOT quick-fixed (out of scope)
- `test_document_routes_docx_archive.py` — 25+ call sites need factory signature migration + endpoint http_request params (> 50 lines). Needs a dedicated refactor pass.
- `test_reservation_dead_process_recovery.py` — 3 tests need design decision on how to invoke http_request-requiring handlers.
- `test_query_stream_routes.py::test_progress_query_failure_emits_structured_error` — needs deeper pipeline-level mocking.
