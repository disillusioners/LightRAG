# Lesson: Merge Regressions — main into workspace-isolation-v2 (2026-07-19)

**Merge:** `main` (reservation model: managed_tasks + token reservations) ⊗ `feature/workspace-isolation-v2`
**Merge commit:** `25bfca30`
**Scope:** 77 files, ~13k insertions, cross-module (api/routers, kg, pipeline, operate, core lightrag)

## Three categories of merge regression found

### Category 1: New model field not reflected in test fakes (FIXED)
Upstream `main` added `include_progress: bool = False` to the `QueryRequest` Pydantic model. The streaming endpoint `query_text_stream` reads it (`request.include_progress or False`). Our v2 workspace test file `tests/workspace/test_streaming_release.py` had a `_FakeQueryRequest` test double that didn't include the new field.

**Symptom:** `AttributeError: '_FakeQueryRequest' object has no attribute 'include_progress'`
**Affected:** 3 streaming-release tests
**Fix:** Added `self.include_progress = False` to `_FakeQueryRequest.__init__` (commit `25bfca30`)
**Prevention:** When a Pydantic model gains a field, grep test files for fakes/doubles of that model and update them.

### Category 2: Construction-path moved in v2 (FIXED)
In v2, `LightRAG` construction moved out of `lightrag_server.py` and into `WorkspaceManager._create_rag_instance`. Tests that monkeypatch `lightrag.api.lightrag_server.LightRAG` no longer intercept construction.

**Symptom:** `_FakeLightRAG` not used; real LightRAG instantiated (and fails on missing config).
**Affected:** 4 login tests, 8 query-stream tests
**Fix:** Changed patch target from `lightrag.api.lightrag_server.LightRAG` → `lightrag.api.workspace_manager.LightRAG` (commit `5a565de3`)
**Prevention:** When a construction site moves, update all `monkeypatch.setattr` / `patch(...)` targets that reference the old module path.

### Category 3: Handler signature gained required param (UNFIXED — needs design decision)
Workspace-isolation v2 re-layered route handlers to take `http_request: Request` as the first positional arg (so the handler can `get_workspace_from_request(http_request)` and `workspace_mgr.acquire(workspace)`). Upstream tests in `tests/kg/test_reservation_dead_process_recovery.py` introspect route handlers via `_endpoint(router, name)` and invoke them DIRECTLY without a Request object.

**Affected handlers** (all in `lightrag/api/routers/document_routes.py` — the conflict epicenter):
- `reprocess_failed_documents` (L4616)
- `force_reset_recovery` (L4751)
- `get_pipeline_status` (L3823)

**Symptom:** `TypeError: missing 1 required positional argument: 'http_request'` or `AttributeError: 'set' object has no attribute 'headers'`
**Fix options** (needs decision — route factories were the hardest conflict):
1. Update tests to pass a mock `Request` (test-side fix)
2. Make `http_request` optional with a default
3. Investigate FastAPI `Depends` injection so direct calls still work

## Non-regression failures (pre-existing / infra)
- **40 failures** across tests/kg/ are missing-dependency (pgvector, pymilvus, neo4j, memgraph not installed in this env). These are upstream test imports that transitively require optional backend deps. NOT merge regressions.
- **27 failures** in `test_document_routes_docx_archive.py` are the old factory signature (`create_document_routes(rag, ...)`) needing migration to v2 (`create_document_routes(FakeWorkspaceManager(rag), ...)`) + endpoint `http_request` param. This is the established v2 migration pattern (see LESSONS/2026-07-17-fake-workspace-manager-helper.md) but the file needs > 50 lines of changes, beyond quick-fix scope.

## Key insight
The merge's conflict epicenter (`document_routes.py`, 24 conflict hunks) is where ALL the unfixed regressions live. The signature change (`rag` → `workspace_mgr` at factory level, `+ http_request` at handler level) is the root cause of every unfixed failure. This is consistent with the merge strategy ("Workspace Isolation outer, slot management inner") — the failures are EXPECTED consequences, not accidental damage.
