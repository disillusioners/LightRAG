# Phase 3: Testing & Integration

> **Revision 2** — Adds S1 test coverage gaps (LRU concurrent eviction, OllamaAPI routing, ASGI cancellation, register_role_llm_builder regression) and tests for C1–C6 fixes.

## Objective

Verify end-to-end workspace routing works correctly across backend and frontend, ensure backward compatibility, and add comprehensive tests for the new WorkspaceManager, WorkspaceRegistry, per-request routing, OllamaAPI refactor, background task safety, streaming release, and sanitization alignment.

## Coupling

- **Depends on**: Phase 1 (Backend complete) + Phase 2 (Frontend complete)
- **Coupling type**: **tight** — Tests exercise the actual implementations from both phases
- **Shared files with other phases**: Tests touch all Phase 1 and Phase 2 files
- **Why this coupling**: Integration and E2E tests require both the backend routing and frontend selector to be functional

---

## Tasks

### Task 1: Backend Unit Tests — WorkspaceRegistry

**File**: `tests/workspace/test_workspace_registry.py` (NEW)

| # | Test | Description |
|---|------|-------------|
| 1a | `test_register_new_workspace` | Register a new workspace → appears in `list_workspaces()` with correct `created_at` |
| 1b | `test_register_existing_workspace` | Register same workspace twice → no duplicate, `created_at` unchanged |
| 1c | `test_list_workspaces_includes_default` | Default workspace always present in list |
| 1d | `test_registry_persistence` | Create registry → register workspace → new registry instance → list includes previously registered workspace (only if persistence enabled per W1) |
| 1e | `test_registry_atomic_save` | Simulate failure during save → registry file not corrupted |
| 1f | `test_concurrent_registration` | Multiple concurrent `register()` calls → no race conditions |

### Task 2: Backend Unit Tests — WorkspaceManager

**File**: `tests/workspace/test_workspace_manager.py` (NEW)

| # | Test | Description |
|---|------|-------------|
| 2a | `test_get_or_create_returns_instance` | First call creates instance; second call returns same instance |
| 2b | `test_lru_eviction` | Fill cache to max → add one more → oldest (refcount=0) is evicted; `finalize_storages()` called |
| 2c | `test_refcount_prevents_eviction` | Acquire workspace → try to evict → not evicted (refcount > 0) |
| 2d | `test_acquire_release_cycle` | acquire → release → acquire → release; refcount correctly tracked |
| 2e | `test_release_triggers_eviction_if_lru` | After release, if workspace is LRU victim and cache is full, it gets evicted |
| 2f | `test_default_workspace_preloaded` | After init, default workspace instance is available immediately |
| 2g | `test_auto_register_on_get_or_create` | New workspace appears in registry after `get_or_create()` |
| 2h | `test_concurrent_get_or_create` | Two concurrent `get_or_create()` for same workspace → only one instance created |
| 2i | `test_background_task_refcount_independence` | Simulate: request acquires → background task acquires → request releases → background task still holds valid ref |
| **2j** | **`test_lru_eviction_during_concurrent_access`** (S1) | **Fill cache → start async task that acquires ws-oldest → try to evict ws-oldest (blocked by refcount) → release in task → eviction now proceeds. Verifies no race between eviction and active acquire.** |
| **2k** | **`test_evicted_instance_finalized`** (S1) | **Evict an instance → verify `finalize_storages()` was called on it → verify subsequent acquire creates a fresh instance** |
| **2l** | **`test_cache_full_returns_503`** (Gap 3) | **Fill cache to max (all `refcount > 0`) then request new workspace. Verify `WorkspaceCacheFullError` raised and HTTP 503 with `Retry-After` header returned.** |
| **2m** | **`test_cache_full_recoverable_after_release`** (Gap 3) | **Trigger cache-full 503, release one ref, retry acquire. Verify it succeeds (eviction proceeds, new instance created).** |
| **2n** | **`test_concurrent_cold_start_no_deadlock`** (Gap 2) | **Two concurrent `acquire()` for different uncached workspaces. `_init_lock` serializes creation. Both succeed, no deadlock, no timeout.** |

### Task 3: Backend Integration Tests — Per-Request Routing

**File**: `tests/workspace/test_workspace_routing.py` (NEW)

| # | Test | Description |
|---|------|-------------|
| 3a | `test_header_routes_to_correct_workspace` | Send request with `LIGHTRAG-WORKSPACE: ws-a` → verify `rag.workspace == "ws-a"` inside handler |
| 3b | `test_missing_header_uses_default` | Send request without header → uses default workspace |
| 3c | `test_empty_header_uses_default` | Send request with `LIGHTRAG-WORKSPACE: ""` → uses default workspace |
| 3d | `test_invalid_chars_sanitized` | Send `LIGHTRAG-WORKSPACE: my..workspace!!` → sanitized to `my__workspace__` |
| 3e | `test_hyphen_preserved` (C5) | Send `LIGHTRAG-WORKSPACE: my-tenant` → workspace is `my-tenant` (NOT `my_tenant`) |
| 3f | `test_document_isolation` | Insert doc in workspace-a → query in workspace-b → doc not found |
| 3g | `test_query_isolation` | Insert doc in ws-a → query in ws-a finds it; query in ws-b doesn't |
| 3h | `test_graph_isolation` | Create entity in ws-a → check exists in ws-b → false |
| 3i | `test_auto_register_on_document_insert` | Upload document with new workspace header → workspace appears in `GET /workspaces` |
| 3j | `test_streaming_workspace_routing` | Stream query with workspace header → operates on correct workspace instance |
| 3k | `test_background_task_workspace_correctness` (C1) | Upload doc → background scan runs → verify scan operates on correct workspace (not default) |
| 3l | `test_all_7_bg_tasks_acquire_release` (C1) | Verify that each of the 7 background task call sites correctly acquires and releases workspace refs. Use refcount assertions. |

### Task 4: Backend API Tests — `GET /workspaces`

**File**: `tests/workspace/test_workspace_routes.py` (NEW)

| # | Test | Description |
|---|------|-------------|
| 4a | `test_list_workspaces_empty` | Fresh server with no documents → returns just default workspace |
| 4b | `test_list_workspaces_after_insert` | Insert doc in workspace → `GET /workspaces` includes it |
| 4c | `test_workspaces_response_shape` | Verify response matches contract: `{ workspaces: [...], default_workspace: "..." }` |
| 4d | `test_workspaces_includes_metadata` | Each workspace has `name`, `created_at`, `document_count` (may be null per W4) |
| 4e | `test_workspaces_auth` | Verify auth dependency is applied to `/workspaces` endpoint |
| 4f | `test_document_count_nullable` (W4) | When document count is expensive, response has `document_count: null` |

### Task 5: OllamaAPI Workspace Routing Tests (C2/S1)

**File**: `tests/workspace/test_ollama_workspace_routing.py` (NEW)

| # | Test | Description |
|---|------|-------------|
| 5a | `test_ollama_generate_workspace_routing` | `POST /api/generate` with `LIGHTRAG-WORKSPACE: ws-a` → uses ws-a's `role_llm_funcs["query"]` |
| 5b | `test_ollama_chat_workspace_routing` | `POST /api/chat` with workspace header → uses correct workspace instance |
| 5c | `test_ollama_tags_workspace_independent` | `GET /api/tags` → returns model list regardless of workspace (workspace-independent endpoint) |
| 5d | `test_ollama_streaming_workspace_release` (C2+C4) | `POST /api/generate` with `stream=true` → verify ref released after stream completes or on exception |
| 5e | `test_ollama_no_header_uses_default` | `POST /api/generate` without header → uses default workspace instance |
| 5f | `test_ollama_all_handlers_acquire_release` (C2) | Verify all 5 OllamaAPI handlers (`/version`, `/tags`, `/ps`, `/generate`, `/chat`) correctly acquire and release workspace refs |

### Task 6: Streaming Release Safety Tests (C4/S1)

**File**: `tests/workspace/test_streaming_release.py` (NEW)

| # | Test | Description |
|---|------|-------------|
| 6a | `test_streaming_release_on_success` | Normal stream completion → ref released exactly once |
| 6b | `test_streaming_release_on_aquery_error` | `rag.aquery_llm()` raises → ref released via `except` path |
| 6c | `test_streaming_release_on_streamingresponse_error` | `StreamingResponse(...)` constructor raises → ref released |
| 6d | **`test_streaming_release_on_asgi_cancellation`** (S1) | **Client disconnects before first byte (ASGI cancellation via `asyncio.CancelledError`) → ref released via `except BaseException` path. Use `httpx.AsyncClient` with manual cancellation.** |
| 6e | `test_streaming_no_double_release` | Verify `_release_once()` guard prevents double release (idempotent) |
| 6f | `test_streaming_release_on_client_midstream_disconnect` | Client disconnects mid-stream → generator's `finally` releases ref |

### Task 7: `register_role_llm_builder` Regression Tests (C3/S1)

**File**: `tests/workspace/test_role_llm_builder.py` (NEW)

| # | Test | Description |
|---|------|-------------|
| 7a | `test_default_workspace_has_role_llm_builder` | Default workspace instance → `role_llm_funcs` is populated for all ROLES |
| 7b | **`test_new_workspace_has_role_llm_builder`** (S1) | **Create new workspace via `workspace_mgr.get_or_create("test-ws")` → verify `role_llm_funcs` is populated** |
| 7c | `test_role_llm_funcs_query_works` | Query on non-default workspace → uses the correct LLM function (not empty/null) |
| 7d | `test_role_llm_kwargs_populated` | Verify `role_llm_kwargs` dict is populated for all roles on new instances |
| 7e | `test_evicted_then_recreated_has_builder` | Evict a workspace instance → recreate via `get_or_create` → verify `role_llm_builder` is re-registered |

### Task 8: Backward Compatibility Tests

**File**: `tests/workspace/test_backward_compat.py` (NEW)

| # | Test | Description |
|---|------|-------------|
| 8a | `test_no_header_default_behavior` | All operations without `LIGHTRAG-WORKSPACE` header behave exactly as current main |
| 8b | `test_existing_workspace_tests_still_pass` | Run all 4 existing test files in `tests/workspace/` unchanged |
| 8c | `test_cli_workspace_still_works` | Start server with `--workspace my-ws` → default workspace is `my-ws` |
| 8d | `test_env_workspace_still_works` | Set `WORKSPACE=my-ws` env → default workspace is `my-ws` |
| 8e | `test_health_endpoint_unchanged` | `/health` still returns same response shape with workspace info |
| 8f | `test_health_no_leak_unauthenticated` (W3) | Unauthenticated `/health` request → does NOT expose `storage_workspaces` or sensitive workspace namespace details |

### Task 9: Sanitization Alignment Tests (C5)

**File**: `tests/workspace/test_sanitization_alignment.py` (NEW)

| # | Test | Description |
|---|------|-------------|
| 9a | `test_backend_accepts_hyphens` | `LIGHTRAG-WORKSPACE: my-tenant` → workspace is `my-tenant` (not rewritten) |
| 9b | `test_backend_strips_invalid_chars` | `LIGHTRAG-WORKSPACE: my tenant!` → sanitized to `my_tenant_` |
| 9c | `test_frontend_backend_regex_match` | Verify frontend `/[^a-zA-Z0-9_-]/g` and backend `[^a-zA-Z0-9_-]` produce identical results for edge cases |
| 9d | `test_uppercase_preserved` | `LIGHTRAG-WORKSPACE: TenantA` → workspace is `TenantA` (case-sensitive, no lowercasing) |
| 9e | `test_roundtrip_consistency` | User selects `my-tenant` in UI → header sent → backend receives `my-tenant` → stored in registry as `my-tenant` → appears in `/workspaces` list as `my-tenant` → matches UI selection |

### Task 10: Frontend Tests

**File**: `lightrag_webui/src/features/WorkspaceSelector.test.tsx` (NEW)

| # | Test | Description |
|---|------|-------------|
| 10a | `test_renders_selector` | Component renders with workspace list from mock API |
| 10b | `test_select_workspace_updates_store` | Selecting a workspace calls `setCurrentWorkspace` |
| 10c | `test_empty_workspace_no_dropdown` | When only default workspace exists, selector is hidden |
| 10d | `test_header_persisted` | After selecting workspace, `useSettingsStore.getState().currentWorkspace` is set |
| 10e | `test_migration_v20_to_v21` (C5) | Verify store migration from v20 → v21 adds `currentWorkspace: ''` |

**File**: `lightrag_webui/src/api/lightrag.test.ts` (MODIFY — add test cases)

| # | Test | Description |
|---|------|-------------|
| 10f | `test_axios_interceptor_adds_header` | When `currentWorkspace` is set, axios request includes `LIGHTRAG-WORKSPACE` header |
| 10g | `test_axios_interceptor_no_header_when_empty` | When `currentWorkspace` is empty, no `LIGHTRAG-WORKSPACE` header |
| 10h | `test_stream_headers_include_workspace` | `_buildStreamHeaders()` includes workspace when set |
| 10i | `test_header_sanitized_correctly` (C5) | `sanitizeWorkspaceHeader('my tenant!')` → `'my_tenant_'`; `sanitizeWorkspaceHeader('my-tenant')` → `'my-tenant'` (hyphen preserved) |
| 10j | `test_uppercase_not_lowercased` (C5) | `sanitizeWorkspaceHeader('TenantA')` → `'TenantA'` (case preserved) |

### Task 11: End-to-End Test

**File**: `tests/workspace/test_e2e_workspace_flow.py` (NEW)

| # | Test | Description |
|---|------|-------------|
| 11a | `test_e2e_create_and_switch_workspace` | Full flow: Start server → send request with `LIGHTRAG-WORKSPACE: test-ws` → Upload document → Query document → Verify results are workspace-isolated |
| 11b | `test_e2e_workspace_switch` | Insert doc in ws-a via API → Switch header to ws-b → Verify doc-a not visible in ws-b |
| 11c | `test_e2e_default_fallback` | Clear workspace header → All operations use default workspace |
| 11d | `test_e2e_hyphen_workspace_roundtrip` (C5) | Create workspace `my-tenant` via header → upload → query → list workspaces → verify name preserved throughout |

---

## Test Summary Matrix

| Test File | Tests | Covers |
|-----------|-------|--------|
| `test_workspace_registry.py` | 6 | Registry CRUD, persistence (W1), concurrency |
| `test_workspace_manager.py` | 14 | LRU, refcount, **concurrent eviction** (S1), eviction finalization (S1), **cache-full 503** (Gap 3), **serial cold-start** (Gap 2) |
| `test_workspace_routing.py` | 12 | Per-request routing, isolation, **bg task correctness** (C1), **hyphen preservation** (C5) |
| `test_workspace_routes.py` | 6 | `/workspaces` API, response shape, auth, nullable count (W4) |
| `test_ollama_workspace_routing.py` | 6 | **OllamaAPI all 5 handlers** (C2), streaming release (C4) |
| `test_streaming_release.py` | 6 | **All 4 release scenarios** (C4), **ASGI cancellation** (S1), double-release prevention |
| `test_role_llm_builder.py` | 5 | **register_role_llm_builder on new instances** (C3/S1), eviction+recreate |
| `test_backward_compat.py` | 6 | Backward compat, **`/health` no leak** (W3) |
| `test_sanitization_alignment.py` | 5 | **Backend+frontend regex parity** (C5), roundtrip consistency |
| `test_e2e_workspace_flow.py` | 4 | Full-stack E2E, **hyphen workspace** (C5) |
| `WorkspaceSelector.test.tsx` | 5 | Component, store migration |
| `lightrag.test.ts` (new cases) | 5 | Header injection, **sanitization** (C5) |
| **Total** | **80** | |

---

## Key Files

### New Test Files (Backend)
- `tests/workspace/test_workspace_registry.py`
- `tests/workspace/test_workspace_manager.py`
- `tests/workspace/test_workspace_routing.py`
- `tests/workspace/test_workspace_routes.py`
- `tests/workspace/test_ollama_workspace_routing.py`
- `tests/workspace/test_streaming_release.py`
- `tests/workspace/test_role_llm_builder.py`
- `tests/workspace/test_backward_compat.py`
- `tests/workspace/test_sanitization_alignment.py`
- `tests/workspace/test_e2e_workspace_flow.py`

### New Test Files (Frontend)
- `lightrag_webui/src/features/WorkspaceSelector.test.tsx`

### Modified Test Files (Frontend)
- `lightrag_webui/src/api/lightrag.test.ts` (add header/sanitization test cases)

### Test Fixtures
- `tests/workspace/conftest.py` (MODIFY) — Add fixtures for WorkspaceManager, mock LightRAG instances, test client with workspace header support, ASGI cancellation utilities

---

## Test Execution

```bash
# Backend tests — all workspace tests
pytest tests/workspace/ -v

# Backend tests — specific fix verification
pytest tests/workspace/test_streaming_release.py -v          # C4
pytest tests/workspace/test_ollama_workspace_routing.py -v   # C2
pytest tests/workspace/test_role_llm_builder.py -v           # C3
pytest tests/workspace/test_sanitization_alignment.py -v     # C5

# Frontend tests
cd lightrag_webui && bun test

# Full suite
pytest tests/workspace/ -v && cd lightrag_webui && bun test
```

---

## Constraints

1. **Mock storage backends** — unit tests should not require real storage (PostgreSQL, Redis, etc.). Use in-memory mock implementations.
2. **No external API calls** — mock LLM and embedding functions in tests
3. **Existing tests must pass unchanged** — the 4 existing test files in `tests/workspace/` must pass without modification
4. **Test isolation** — each test should clean up its workspace instances and registry state
5. **Concurrent test safety** — tests for concurrent get_or_create must use actual asyncio concurrency, not sequential simulation
6. **ASGI cancellation tests** — use `httpx.AsyncClient` with manual task cancellation to simulate client disconnect

---

## Deliverables

- [ ] `tests/workspace/test_workspace_registry.py` — 6 tests
- [ ] `tests/workspace/test_workspace_manager.py` — 14 tests (incl. S1 concurrent eviction, Gap 2 serial cold-start, Gap 3 cache-full 503)
- [ ] `tests/workspace/test_workspace_routing.py` — 12 tests (incl. C1 bg task verification, C5 hyphens)
- [ ] `tests/workspace/test_workspace_routes.py` — 6 tests
- [ ] `tests/workspace/test_ollama_workspace_routing.py` — 6 tests (C2)
- [ ] `tests/workspace/test_streaming_release.py` — 6 tests (C4, incl. S1 ASGI cancellation)
- [ ] `tests/workspace/test_role_llm_builder.py` — 5 tests (C3/S1)
- [ ] `tests/workspace/test_backward_compat.py` — 6 tests (incl. W3 health leak)
- [ ] `tests/workspace/test_sanitization_alignment.py` — 5 tests (C5)
- [ ] `tests/workspace/test_e2e_workspace_flow.py` — 4 tests
- [ ] `lightrag_webui/src/features/WorkspaceSelector.test.tsx` — 5 tests
- [ ] `lightrag_webui/src/api/lightrag.test.ts` — 5 new test cases (C5)
- [ ] All existing `tests/workspace/` tests pass unchanged
- [ ] `pytest tests/workspace/ -v` shows 0 failures
- [ ] `cd lightrag_webui && bun test` shows 0 failures
