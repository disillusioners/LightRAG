# Phase 1: Backend — WorkspaceManager + Routes API + OllamaAPI

> **Revision 2** — Addresses C1 (bg tasks), C2 (OllamaAPI), C3 (register_role_llm_builder), C4 (streaming release), C5 (sanitization), C6 (multi-worker), W1–W4.

## Objective

Build a lightweight workspace instance caching/registry system, add a `GET /workspaces` listing endpoint, and wire the `LIGHTRAG-WORKSPACE` header into ALL route handlers (document, query, graph, **and Ollama**) so each request operates on the correct per-workspace `LightRAG` instance.

## Coupling

- **Depends on**: None (root phase)
- **Coupling type**: — (root)
- **Shared files with other phases**: `lightrag/api/lightrag_server.py` (modified), `lightrag/api/routers/document_routes.py` (modified), `lightrag/api/routers/query_routes.py` (modified), `lightrag/api/routers/graph_routes.py` (modified), `lightrag/api/routers/ollama_api.py` (modified)
- **Shared APIs/interfaces**: The `/workspaces` endpoint response shape (Phase 2 frontend depends on this contract)
- **Why this coupling**: Phase 1 defines the API contract that Phase 2 consumes. Internal implementation of WorkspaceManager is not coupled to frontend.

---

## API Contract (for Phase 2 parallelization)

### `GET /workspaces`

**Response 200:**
```json
{
  "workspaces": [
    {
      "name": "default",
      "created_at": "2026-07-16T18:00:00Z",
      "document_count": null
    },
    {
      "name": "tenant-a",
      "created_at": "2026-07-15T10:30:00Z",
      "document_count": 5
    }
  ],
  "default_workspace": "default"
}
```

- Always includes the default workspace (even if no documents)
- `document_count` is best-effort — may be `null` when expensive to compute (W4)
- `created_at` is the first-seen timestamp from the registry

---

## Tasks

### Task 1: Create `WorkspaceRegistry` — workspace persistence

**File**: `lightrag/api/workspace_registry.py` (NEW)

| # | Sub-task | Details |
|---|----------|---------|
| 1a | Define `WorkspaceRegistry` class | Manages a JSON registry file at `{working_dir}/workspace_registry.json` |
| 1b | `register(workspace: str)` | Adds workspace to registry if not present. Records `created_at` timestamp. Thread-safe via `asyncio.Lock`. |
| 1c | `list_workspaces() -> List[dict]` | Returns all registered workspaces with metadata |
| 1d | `exists(workspace: str) -> bool` | Check if workspace is registered |
| 1e | `_load()` / `_save()` | Load from disk on init; save atomically (write temp + `os.rename`) on changes |

**Design notes:**
- Registry file path: `{args.working_dir}/workspace_registry.json`
- Schema: `{"workspaces": [{"name": "...", "created_at": "..."}], "default": "..."}`
- The default workspace (from `args.workspace` or `""`) is always registered at init
- No deletion API in this phase (workspaces persist; cleanup is manual/ops concern)
- **W1 — Persistence is OPTIONAL for v2:** Start with in-memory `OrderedDict` tracking. Add file persistence as a follow-up when multi-worker support is addressed. The `WorkspaceRegistry` class should have the same interface either way — persistence is an internal implementation detail.

### Task 2: Create `WorkspaceManager` — LRU cache + refcounting

**File**: `lightrag/api/workspace_manager.py` (NEW)

| # | Sub-task | Details |
|---|----------|---------|
| 2a | Define `WorkspaceManager` class | Constructor takes `args` (config namespace), `embedding_func`, `llm_model_func`, plus all kwargs needed to construct `LightRAG` instances |
| 2b | `get_or_create(workspace: str) -> LightRAG` | Returns cached instance or creates new one. Updates LRU order. Auto-registers via registry if new. |
| 2c | `acquire(workspace: str) -> LightRAG` | Increments refcount for workspace, returns instance. Used by request handlers and background tasks. |
| 2d | `release(workspace: str)` | Decrements refcount. If refcount reaches 0 and workspace is LRU victim, can be evicted. |
| 2e | `_create_rag_instance(workspace: str) -> LightRAG` | Constructs `LightRAG(workspace=workspace, ...)` with all the same params as `lightrag_server.py:2043`. Calls `await rag.initialize_storages()` AND `await rag.check_and_migrate_data()` (matching main's lifespan — see `lightrag.py:1277`). For fresh workspaces `check_and_migrate_data()` is a no-op; for users importing existing storage data, omitting it causes silent data inconsistency. **CRITICAL (C3): Must also call `rag.register_role_llm_builder(...)` after construction** — see Task 2f below. |
| 2f | **`_register_role_llm_builder(rag)`** (C3) | Extracts the builder lambda from `lightrag_server.py:2108-2113` into a reusable function. Called inside `_create_rag_instance()` after `LightRAG(...)` construction and before `initialize_storages()`. See Task 9 for the exact extracted code. |
| 2g | `_evict_if_needed()` | If cache size > `max_instances` (configurable, default 8), evict least-recently-used instance with refcount 0. Call `await rag.finalize_storages()` on eviction. **Cache-full behavior (Gap 3):** If cache is full AND all entries have `refcount > 0` (all in use), no eviction victim exists. Raise `WorkspaceCacheFullError` → route handler returns HTTP 503 with `Retry-After: 5` header. This signals the client to retry after in-flight requests complete and refs are released. |

#### Gap 3 — Cache-Full Policy

When the LRU cache is at capacity (`max_instances`, default 8) and every cached instance has `refcount > 0` (all actively in use), a new workspace request cannot be served:

1. **`_create_rag_instance()` detects no eviction victim** → raises `WorkspaceCacheFullError`
2. **Route handler catches `WorkspaceCacheFullError`** → returns:
   ```python
   raise HTTPException(
       status_code=503,
       detail="Workspace cache is full. All instances are in use. Retry shortly.",
       headers={"Retry-After": "5"},
   )
   ```
3. **Client behavior:** The WebUI axios interceptor should handle 503 with `Retry-After` by displaying a transient "Workspace is initializing, please wait..." message and retrying after the indicated delay.

**Why 503 + Retry-After** rather than blocking: blocking would hold a connection open indefinitely (potential deadlock under load). 503 is the HTTP-standard way to signal temporary unavailability. Under normal operation (8+ concurrent workspaces all actively processing), this is extremely rare.
| 2h | `get_default_workspace() -> str` | Returns the configured default workspace name |
| 2i | `list_workspaces() -> List[dict]` | Delegates to `WorkspaceRegistry.list_workspaces()` |

**Design notes:**
- **`_init_lock = asyncio.Lock()`** (Gap 2) — serializes the `_create_rag_instance()` body. Upstream explicitly warns at `lightrag.py:1277`: *"Storage initialization must be called one by one to prevent deadlock."* Two concurrent cold-start `acquire()` calls for different workspaces would call `initialize_storages()` concurrently, risking deadlock on shared storage resources (e.g., shared storage locks, multiprocessing Manager). The `_init_lock` ensures only one instance creation runs at a time; concurrent `acquire()` calls for the same workspace wait on the lock and then find the instance already cached.
- LRU tracking via `collections.OrderedDict` or similar
- `acquire()`/`release()` pattern prevents use-after-release for background tasks
- The default workspace instance is pre-loaded at startup (eager init, not lazy)
- Reference: old fork's `workspace_manager.py` for patterns (but simplify — no isolation logic)

### Task 3: Create `workspace_routes.py` — `GET /workspaces` endpoint

**File**: `lightrag/api/routers/workspace_routes.py` (NEW)

| # | Sub-task | Details |
|---|----------|---------|
| 3a | Define `create_workspace_routes(workspace_mgr, api_key)` factory | Follows existing factory pattern |
| 3b | `GET /workspaces` handler | Returns `{"workspaces": [...], "default_workspace": "..."}` |
| 3c | Response model | Define `WorkspaceInfo` and `WorkspacesResponse` Pydantic models |

### Task 4: Wire `WorkspaceManager` into `lightrag_server.py`

**File**: `lightrag/api/lightrag_server.py` (MODIFY)

| # | Sub-task | Details | Key Lines |
|---|----------|---------|-----------|
| 4a | Import WorkspaceManager + WorkspaceRegistry + workspace_routes | Add imports near existing router imports | L53-64 |
| 4b | Replace single `rag` creation with WorkspaceManager | Instead of `rag = LightRAG(...)` at L2043, create `workspace_mgr = WorkspaceManager(args, ...)` and initialize it. The default workspace instance is created eagerly. | L2043-2101 |
| 4c | **Remove `register_role_llm_builder` from server** (C3) | The call at L2108-2113 is now inside `_create_rag_instance()`. Remove it from `lightrag_server.py`. | L2106-2113 |
| 4d | Update router factory calls | Change from `create_document_routes(rag, doc_manager, api_key)` to `create_document_routes(workspace_mgr, doc_manager, api_key)` (and same for query, graph routes) | L2118-2120 |
| 4e | Register workspace routes | Add `app.include_router(create_workspace_routes(workspace_mgr, api_key))` | After L2120 |
| 4f | **Refactor `OllamaAPI` instantiation** (C2) | Change from `OllamaAPI(rag, ...)` to `OllamaAPI(workspace_mgr, ...)`. See Task 8 for full OllamaAPI refactor. | L2123 |
| 4g | Update lifespan to init/finalize workspace manager | Replace `await rag.initialize_storages()` / `await rag.finalize_storages()` with workspace manager init/finalize | L1283, L1294 |
| 4h | **Fix sanitization regex** (C5) | In `get_workspace_from_request()` at L1446, change `re.sub(r"[^a-zA-Z0-9_]", "_", workspace)` to `re.sub(r"[^a-zA-Z0-9_-]", "_", workspace)`. Also fix `config.py:741`. See Task 10. | L1446, config.py:741 |
| 4i | Move `get_workspace_from_request()` to shared utility | Extract from `lightrag_server.py:1426` to a shared location (e.g., `lightrag/api/utils_api.py` or a new `workspace_utils.py`) so routers can import it. | L1426-1454 |

### Task 5: Modify `document_routes.py` — per-request workspace routing + ALL bg tasks

**File**: `lightrag/api/routers/document_routes.py` (MODIFY)

| # | Sub-task | Details | Key Lines |
|---|----------|---------|-----------|
| 5a | Change factory signature | `create_document_routes(workspace_mgr, doc_manager, api_key)` instead of `(rag, doc_manager, api_key)` | L2450 |
| 5b | Add workspace extraction to each handler | Each handler: extract workspace from `Request` → `rag = await workspace_mgr.acquire(workspace)` → operate → `workspace_mgr.release(workspace)` in finally block | All handlers |
| 5c | Handle the `request: Request` parameter | Add `request: Request` param to handler signatures that don't already have it | Various |
| 5d | **Background task closures — `_indexing_task`** (C1) | For the 3 inline closures at L2802, L2906, L3030: add `rag = await workspace_mgr.acquire(workspace)` at closure start and `await workspace_mgr.release(workspace)` in the existing `finally` block. Capture `workspace` (not `rag`) in the closure scope. | L2802, L2906, L3030 |
| 5e | **Background task — `reprocess_failed` bound method** (C1) | For L3939 (`background_tasks.add_task(rag.apipeline_process_enqueue_documents)`): create a wrapper async function that acquires the workspace, calls the bound method, then releases. | L3939 |
| 5f | **Background task helpers** (C1) | For `run_scanning_process`, `pipeline_index_texts`, `background_delete_documents`: create wrapper functions `_run_*_with_workspace(workspace_mgr, workspace, ...)` that acquire at start, release in finally. | L2506, L2578, L2908, L3032, L3559-3566 |
| 5g | Auto-register workspace on document insert | In `upload_to_input_dir` and `insert_text` handlers, call `workspace_mgr.registry.register(workspace)` after successful insert | L2585, L2834 |

**Complete handler list** (all need workspace routing):
POST `/documents/scan`, POST `/documents/upload`, POST `/documents/text`, POST `/documents/texts`, GET `/documents`, POST `/documents/paginated`, DELETE `/documents`, DELETE `/documents/delete_document`, POST `/documents/reprocess_failed`, GET `/documents/pipeline_status`, POST `/documents/cancel_pipeline`, GET `/documents/track_status/{track_id}`, GET `/documents/status_counts`, POST `/documents/clear_cache`

#### C1 Background Task Wrapping Patterns (VERIFIED)

**Pattern A — Inline `_indexing_task` closures** (L2802, L2906, L3030):

```python
# BEFORE (upload handler, L2802):
async def _indexing_task():
    try:
        await pipeline_index_file(rag, file_path, track_id)
    finally:
        await _release_enqueue_slot(rag)

# AFTER:
async def _indexing_task():
    bg_rag = await workspace_mgr.acquire(workspace)
    try:
        await pipeline_index_file(bg_rag, file_path, track_id)
    finally:
        await _release_enqueue_slot(bg_rag)
        await workspace_mgr.release(workspace)
```

**Pattern B — `reprocess_failed` bound method** (L3939):

```python
# BEFORE:
background_tasks.add_task(rag.apipeline_process_enqueue_documents)

# AFTER — wrapper function:
async def _run_enqueue_with_workspace():
    bg_rag = await workspace_mgr.acquire(workspace)
    try:
        await bg_rag.apipeline_process_enqueue_documents()
    finally:
        await workspace_mgr.release(workspace)

background_tasks.add_task(_run_enqueue_with_workspace)
```

**Pattern C — Explicit task helpers** (run_scanning_process, pipeline_index_texts, background_delete_documents):

```python
# BEFORE:
background_tasks.add_task(run_scanning_process, rag, doc_manager, track_id)

# AFTER — wrapper with independent ref management:
async def _run_scanning_with_workspace():
    bg_rag = await workspace_mgr.acquire(workspace)
    try:
        await run_scanning_process(bg_rag, doc_manager, track_id)
    finally:
        await workspace_mgr.release(workspace)

background_tasks.add_task(_run_scanning_with_workspace)
```

> **W2 note:** Verify whether `doc_manager` is actually used by these bg tasks or just passed through. If `doc_manager` is workspace-specific (it has `workspace` in its constructor at L1003), pass a per-workspace `DocumentManager` instead of the default one.

### Task 6: Modify `query_routes.py` — per-request workspace routing + streaming-safe release

**File**: `lightrag/api/routers/query_routes.py` (MODIFY)

| # | Sub-task | Details | Key Lines |
|---|----------|---------|-----------|
| 6a | Change factory signature | `create_query_routes(workspace_mgr, api_key, top_k)` instead of `(rag, api_key, top_k)` | L191 |
| 6b | Add workspace extraction + acquire/release to handlers | Same pattern as document routes | All handlers |
| 6c | **Streaming handler — exception-safe one-shot release** (C4) | The `query_text_stream` handler (L520) must use the one-shot release pattern. See C4 pattern below. | L520-753 |

**Handlers to modify:**
- POST `/query` (`query_text`, L200, rag call at L412)
- POST `/query/stream` (`query_text_stream`, L520, rag call at L734) — **C4 streaming pattern**
- POST `/query/data` (`query_data`, L755, rag call at L1156)

#### C4 Streaming Release Pattern (VERIFIED)

**Key insight from code exploration:** `_generate()` (the inner generator from `_build_stream_generator`) closes over `result` only — it does NOT close over `rag`. The rag ref is only needed during the synchronous `await rag.aquery_llm()` call. After that, `result` contains the `response_iterator` and all data needed for streaming.

This means we can release immediately after `aquery_llm` returns, BEFORE streaming starts — as long as the `response_iterator` is self-contained. But to be safe (some LLM implementations may lazily fetch from rag), we keep the ref until the stream finishes.

**Exception-safe one-shot release:**
```python
@router.post("/query/stream")
async def query_text_stream(request: QueryRequest, http_request: Request):
    workspace = get_workspace_from_request(http_request)
    rag = await workspace_mgr.acquire(workspace)
    released = False

    async def _release_once():
        nonlocal released
        if not released:
            released = True
            await workspace_mgr.release(workspace)

    try:
        param = request.to_query_params(stream_mode)
        result = await rag.aquery_llm(request.query, param=param)

        stream_gen = _build_stream_generator(
            result=result,
            include_references=request.include_references,
            include_chunk_content=request.include_chunk_content,
        )

        async def _generate():
            try:
                # _generate only closes over `result`, NOT `rag`
                # This is verified: the inner generator iterates
                # result["llm_response"]["response_iterator"]
                gen = stream_gen()
                async for chunk in gen:
                    yield chunk
            finally:
                await _release_once()

        return StreamingResponse(
            _generate(),
            media_type="application/x-ndjson",
            headers={...},
        )
    except Exception:
        await _release_once()
        raise
```

**Why this works:**
1. **`aquery_llm` raises** → `except Exception` catches → `_release_once()` releases → re-raise
2. **`StreamingResponse` constructor raises** → same `except` path
3. **Client disconnect before first byte** (ASGI cancellation) → generator never entered → `_release_once()` never called in generator, BUT the handler's `except` path doesn't fire either. **This case is handled by ASGI**: when the client disconnects, FastAPI cancels the task, raising `asyncio.CancelledError`. The `except Exception` won't catch `CancelledError` (it's `BaseException`). Add explicit `except BaseException` or use a `finally` with a guard:
   ```python
   except BaseException:
       await _release_once()
       raise
   ```

> **Implementation note:** Use `except BaseException` instead of `except Exception` to also handle `asyncio.CancelledError` (ASGI client disconnect). Alternatively, add a separate `except asyncio.CancelledError` block.

### Task 7: Modify `graph_routes.py` — per-request workspace routing

**File**: `lightrag/api/routers/graph_routes.py` (MODIFY)

| # | Sub-task | Details | Key Lines |
|---|----------|---------|-----------|
| 7a | Change factory signature | `create_graph_routes(workspace_mgr, api_key)` instead of `(rag, api_key)` | L112 |
| 7b | Add workspace extraction + acquire/release to handlers | Same pattern | All handlers |
| 7c | `check_pipeline_busy_or_raise(rag)` | Imported from `document_routes` at L13, takes `rag` as first arg. Since we acquire the per-request rag before calling it, this still works. | L385, L453, L526, L614, L706, L751, L787 |

**All 12 handlers need modification:**
GET `/graph/label/list`, GET `/graph/label/popular`, GET `/graph/label/search`, GET `/graphs`, GET `/graph/entity/exists`, POST `/graph/entity/edit`, POST `/graph/relation/edit`, POST `/graph/entity/create`, POST `/graph/relation/create`, POST `/graph/entities/merge`, DELETE `/graph/entity/delete`, DELETE `/graph/relation/delete`

### Task 8: Refactor `OllamaAPI` — per-request workspace routing (C2)

**File**: `lightrag/api/routers/ollama_api.py` (MODIFY)

> **CRITICAL:** Passing default rag silently breaks workspace isolation for all Ollama API consumers (`/api/chat`, `/api/generate`, `/api/tags`, `/api/ps`, `/api/version`).

| # | Sub-task | Details | Key Lines |
|---|----------|---------|-----------|
| 8a | Change `__init__` signature | `def __init__(self, workspace_mgr, top_k=60, api_key=None)` instead of `(self, rag, ...)` | L222 |
| 8b | Store `workspace_mgr` instead of `rag` | `self.workspace_mgr = workspace_mgr` instead of `self.rag = rag` | L223 |
| 8c | Remove `self.ollama_server_infos` binding | Currently `self.ollama_server_infos = rag.ollama_server_infos` (L224). This is workspace-independent (server config, not data). Keep it by extracting from default instance at init: `self.ollama_server_infos = workspace_mgr.get_default_instance().ollama_server_infos`. **Approver note — ordering:** Ensure the default workspace instance is pre-loaded (eager init) BEFORE `OllamaAPI.__init__` runs, so `get_default_instance()` returns a valid object. | L224 |
| 8d | Add per-request acquire/release to ALL handlers | Each of the 5 handlers: extract workspace from `raw_request` (already a param!) → acquire → operate → release | All handlers |
| 8e | Replace all 15 `self.rag.*` references | Replace with per-request `rag` variable from acquire. See mapping below. | L304-714 |
| 8f | Streaming closures in `/generate` and `/chat` | The `stream_generator` closures (L319-426, L549-678) close over `self.rag` and `self.ollama_server_infos`. Since `self.ollama_server_infos` is workspace-independent, keep it. But `self.rag.role_llm_kwargs` and `self.rag.role_llm_funcs` must be replaced with per-request `rag`. **For streaming: use one-shot release pattern** (same as C4). | L319-426, L549-678 |

**`self.rag.*` → per-request `rag.*` mapping (15 references):**

| Line | Current | After |
|------|---------|-------|
| 304 | `self.rag.role_llm_kwargs["query"]` | `rag.role_llm_kwargs["query"]` |
| 305 | `self.rag.role_llm_kwargs["query"]` | `rag.role_llm_kwargs["query"]` |
| 306 | `self.rag.llm_model_kwargs` | `rag.llm_model_kwargs` |
| 312 | `self.rag.role_llm_funcs["query"]` | `rag.role_llm_funcs["query"]` |
| 440 | `self.rag.role_llm_funcs["query"]` | `rag.role_llm_funcs["query"]` |
| 531 | `self.rag.role_llm_kwargs["query"]` | `rag.role_llm_kwargs["query"]` |
| 532 | `self.rag.role_llm_kwargs["query"]` | `rag.role_llm_kwargs["query"]` |
| 533 | `self.rag.llm_model_kwargs` | `rag.llm_model_kwargs` |
| 537 | `self.rag.role_llm_funcs["query"]` | `rag.role_llm_funcs["query"]` |
| 545 | `self.rag.aquery(...)` | `rag.aquery(...)` |
| 699 | `self.rag.role_llm_kwargs["query"]` | `rag.role_llm_kwargs["query"]` |
| 700 | `self.rag.role_llm_kwargs["query"]` | `rag.role_llm_kwargs["query"]` |
| 701 | `self.rag.llm_model_kwargs` | `rag.llm_model_kwargs` |
| 706 | `self.rag.role_llm_funcs["query"]` | `rag.role_llm_funcs["query"]` |
| 714 | `self.rag.aquery(...)` | `rag.aquery(...)` |

**Handler-by-handler pattern:**
```python
# Each OllamaAPI handler already receives `raw_request: Request`:
@self.router.post("/generate", dependencies=[Depends(combined_auth)])
async def generate(raw_request: Request):
    workspace = get_workspace_from_request(raw_request)
    rag = await self.workspace_mgr.acquire(workspace)
    try:
        # ... all self.rag.* → rag.* ...
    finally:
        await self.workspace_mgr.release(workspace)
```

**For Ollama streaming handlers** (`/generate` stream and `/chat` stream):
Use the same one-shot release pattern as C4 (query_text_stream). The stream generators use `self.ollama_server_infos` (workspace-independent — keep as-is) but also use `rag.role_llm_funcs` / `rag.aquery` (workspace-specific — must be per-request).

### Task 9: Extract `register_role_llm_builder` to shared factory (C3)

**File**: `lightrag/api/workspace_manager.py` or `lightrag/api/llm_factory.py` (NEW section/file)

> **CRITICAL:** Without this, per-workspace instances have empty `role_llm_funcs` → queries on non-default workspaces fail or use wrong LLM.

**Current code at `lightrag_server.py:2108-2113`:**
```python
rag.register_role_llm_builder(
    lambda role, meta: (
        create_role_llm_func(role, meta),
        create_role_llm_model_kwargs(role, meta),
    )
)
```

**Extracted to a reusable function:**
```python
# In workspace_manager.py or a new llm_factory.py:
from lightrag.api.llm_server_utils import create_role_llm_func, create_role_llm_model_kwargs

def _register_role_llm_builder(rag: LightRAG):
    """Register the role-LLM builder on a LightRAG instance.

    This must be called on EVERY new LightRAG instance, including
    per-workspace instances created by WorkspaceManager.
    """
    rag.register_role_llm_builder(
        lambda role, meta: (
            create_role_llm_func(role, meta),
            create_role_llm_model_kwargs(role, meta),
        )
    )
```

**Called inside `_create_rag_instance()`:**
```python
async def _create_rag_instance(self, workspace: str) -> LightRAG:
    async with self._init_lock:  # Gap 2: serialize creation — prevents storage init deadlock
        rag = LightRAG(
            working_dir=self.args.working_dir,
            workspace=workspace,
            # ... all the same kwargs as lightrag_server.py:2043-2101 ...
        )
        _register_role_llm_builder(rag)  # C3: CRITICAL — before initialize_storages
        await rag.initialize_storages()
        await rag.check_and_migrate_data()  # Gap 1: matching main's lifespan; no-op for fresh workspaces
    return rag
```

> **Note:** The `role_llm_configs` dict (passed as a kwarg to `LightRAG(...)` at L2076-2100) is already in the constructor. The builder lambda at L2108-2113 is what converts those configs into callable functions. Both are needed.

### Task 10: Fix sanitization regex — accept hyphens (C5)

**Files**: `lightrag/api/lightrag_server.py`, `lightrag/api/config.py` (MODIFY)

| # | Sub-task | Details | Key Lines |
|---|----------|---------|-----------|
| 10a | Fix `get_workspace_from_request()` | Change `re.sub(r"[^a-zA-Z0-9_]", "_", workspace)` → `re.sub(r"[^a-zA-Z0-9_-]", "_", workspace)` | `lightrag_server.py:1446` |
| 10b | Fix CLI sanitization in `config.py` | Change `re.sub(r"[^a-zA-Z0-9_]", "_", args.workspace)` → `re.sub(r"[^a-zA-Z0-9_-]", "_", args.workspace)` | `config.py:741` |

**Why:** `validate_workspace()` at `utils.py:4995` already accepts hyphens (it only rejects `/`, `\`, `.`, `..`). The CLI/header sanitization was stricter than the validator. This alignment prevents silent routing bugs where user selects `my-tenant` in the UI, backend rewrites to `my_tenant`, and the stored `currentWorkspace` never matches.

### Task 11: Handle DocumentManager per-workspace

**File**: `lightrag/api/routers/document_routes.py` (MODIFY — part of Task 5)

| # | Sub-task | Details |
|---|----------|---------|
| 11a | Make DocumentManager per-request | `DocumentManager.__init__` at L999 takes `workspace` param and creates `input_dir = base_input_dir / workspace` if set. Construct `DocumentManager(base_input_dir, workspace=request_workspace)` in upload/text handlers. |
| 11b | Store `base_input_dir` | The factory receives `doc_manager` — extract `doc_manager.base_input_dir` and use it to construct per-workspace DocumentManager instances in handlers. |

**Recommendation**: Option A — construct per-request `DocumentManager(base_input_dir, workspace=workspace)` in upload/text/texts handlers.

> **Approver note — DocumentManager consistency:** Either (a) cache `DocumentManager` instances alongside `LightRAG` instances in the `WorkspaceManager` (return both from `acquire()`), or (b) construct fresh per request. Option (b) is simpler since `DocumentManager` is lightweight (just stores paths). Whichever option is chosen, ensure consistency: never mix a workspace-A `DocumentManager` with a workspace-B `LightRAG` instance.

### Task 12: Add multi-worker deployment guard (C6)

**File**: `lightrag/api/lightrag_server.py` (MODIFY)

| # | Sub-task | Details |
|---|----------|---------|
| 12a | Add startup warning | If `workers > 1` (check uvicorn config), log a warning that workspace isolation requires `--workers 1` or session affinity |
| 12b | Document in README/code comment | Add a comment block explaining the per-process nature of WorkspaceManager and the single-worker recommendation |

---

## Key Files

### New Files
- `lightrag/api/workspace_manager.py` — WorkspaceManager class (LRU cache + refcounting + role_llm_builder registration)
- `lightrag/api/workspace_registry.py` — WorkspaceRegistry class (persistence, optional per W1)
- `lightrag/api/routers/workspace_routes.py` — `GET /workspaces` endpoint

### Modified Files
- `lightrag/api/lightrag_server.py` — Replace single `rag` with `workspace_mgr`; fix sanitization; remove `register_role_llm_builder` call (moved to manager); add multi-worker guard
- `lightrag/api/routers/document_routes.py` — Factory signature + per-request workspace routing + ALL 7 bg task wrappings
- `lightrag/api/routers/query_routes.py` — Factory signature + per-request workspace routing + C4 streaming-safe release
- `lightrag/api/routers/graph_routes.py` — Factory signature + per-request workspace routing
- `lightrag/api/routers/ollama_api.py` — Full refactor: `workspace_mgr` + per-request acquire/release in all 5 handlers (C2)
- `lightrag/api/config.py` — Fix sanitization regex (C5)

### Reference Files (from old fork — DO NOT copy, reference patterns only)
- `lightrag/api/workspace_manager.py` (old fork) — LRU + refcount patterns
- `lightrag/api/workspace_registry.py` (old fork) — Registry persistence
- `lightrag/api/routers/workspace_routes.py` (old fork) — Route factory

---

## Integration Points (Exact Locations)

| Integration Point | File:Line | Current State | Target State |
|---|---|---|---|
| RAG instance creation | `lightrag_server.py:2043` | `rag = LightRAG(workspace=args.workspace, ...)` | `workspace_mgr = WorkspaceManager(args, ...)` |
| `register_role_llm_builder` (C3) | `lightrag_server.py:2108` | Called once on startup rag | Moved into `_create_rag_instance()` — called on every new instance |
| Document router registration | `lightrag_server.py:2118` | `create_document_routes(rag, doc_manager, api_key)` | `create_document_routes(workspace_mgr, doc_manager, api_key)` |
| Query router registration | `lightrag_server.py:2119` | `create_query_routes(rag, api_key, top_k)` | `create_query_routes(workspace_mgr, api_key, top_k)` |
| Graph router registration | `lightrag_server.py:2120` | `create_graph_routes(rag, api_key)` | `create_graph_routes(workspace_mgr, api_key)` |
| OllamaAPI instantiation (C2) | `lightrag_server.py:2123` | `OllamaAPI(rag, top_k, api_key)` | `OllamaAPI(workspace_mgr, top_k, api_key)` |
| Lifespan init | `lightrag_server.py:1283` | `await rag.initialize_storages()` | `await workspace_mgr.initialize()` |
| Lifespan cleanup | `lightrag_server.py:1294` | `await rag.finalize_storages()` | `await workspace_mgr.finalize()` |
| Header sanitization (C5) | `lightrag_server.py:1446` | `re.sub(r"[^a-zA-Z0-9_]", "_", ws)` | `re.sub(r"[^a-zA-Z0-9_-]", "_", ws)` |
| CLI sanitization (C5) | `config.py:741` | `re.sub(r"[^a-zA-Z0-9_]", "_", ws)` | `re.sub(r"[^a-zA-Z0-9_-]", "_", ws)` |
| DocumentManager creation | `lightrag_server.py:1272` | `DocumentManager(args.input_dir, workspace=args.workspace)` | Store `base_input_dir`; construct per-request in handlers |

---

## Complete Background Task Inventory (C1)

| # | Location | Pattern | Fix |
|---|----------|---------|-----|
| 1 | `document_routes.py:2802` | `_indexing_task` closure in `upload_document` — captures `rag` | Add acquire/release inside closure's try/finally |
| 2 | `document_routes.py:2906` | `_indexing_task` closure in `insert_text` — captures `rag` | Same |
| 3 | `document_routes.py:3030` | `_indexing_task` closure in `insert_texts` — captures `rag` | Same |
| 4 | `document_routes.py:3939` | `rag.apipeline_process_enqueue_documents` bound method — NO wrapper | Create wrapper async function |
| 5 | `document_routes.py:2506` | `background_tasks.add_task(run_scanning_process, rag, ...)` | Create `_run_scanning_with_workspace` wrapper |
| 6 | `document_routes.py:2578` | `background_tasks.add_task(run_scanning_process, rag, ...)` | Same wrapper |
| 7 | `document_routes.py:3559` | `background_tasks.add_task(background_delete_documents, rag, ...)` | Create `_run_delete_with_workspace` wrapper |

> **Note:** `pipeline_index_texts` is called inside closures at items 1-3 (via `pipeline_index_file` and `pipeline_index_texts`), not directly via `add_task`. Those are handled by fixing the closure itself.

---

## Constraints

1. **Do NOT re-implement storage isolation** — upstream `get_final_namespace()` already handles `{workspace}:{namespace}` prefixing
2. **Do NOT add `WORKSPACE_ISOLATION` env flag** — main always namespaces when workspace is set
3. **Backward compatibility** — empty/missing workspace header = default workspace behavior (same as current main)
4. **Follow existing factory pattern** — `create_*_routes()` factory functions with closure-based handlers
5. **Thread/async safety** — all workspace manager operations must be async-safe
6. **Reference counting** — ALL background tasks must acquire/release independently of request handlers (C1)
7. **Every new LightRAG instance must have `register_role_llm_builder` called** (C3)
8. **Streaming handlers must use exception-safe one-shot release** including `asyncio.CancelledError` (C4)
9. **Single-worker mode** required for v2 (C6) — add startup warning for `--workers > 1`
10. **Sanitization backward-compat migration** (Approver note): Existing deployments using `--workspace=team-a` (old regex `[^a-zA-Z0-9_]`) have data stored under `team_a` (hyphen stripped to underscore). After the C5 fix (`[^a-zA-Z0-9_-]`), `LIGHTRAG-WORKSPACE: team-a` routes to `team-a` directory (new, empty). **Old data becomes inaccessible.** Mitigation: add a migration script or documentation that renames old storage directories/prefixes from the old sanitized name to the new one (e.g., `team_a` → `team-a`). Alternatively, document that users should manually rename their workspace storage directories.

---

## Deliverables

- [ ] `lightrag/api/workspace_registry.py` — WorkspaceRegistry class (persistence optional per W1)
- [ ] `lightrag/api/workspace_manager.py` — WorkspaceManager class with LRU + refcount + role_llm_builder
- [ ] `lightrag/api/routers/workspace_routes.py` — `GET /workspaces` endpoint
- [ ] `lightrag/api/lightrag_server.py` — Updated to use WorkspaceManager; sanitization fixed; multi-worker guard
- [ ] `lightrag/api/routers/document_routes.py` — Per-request routing + ALL 7 bg tasks wrapped
- [ ] `lightrag/api/routers/query_routes.py` — Per-request routing + C4 streaming-safe release
- [ ] `lightrag/api/routers/graph_routes.py` — Per-request routing
- [ ] `lightrag/api/routers/ollama_api.py` — Full refactor (C2): all 15 self.rag.* references updated
- [ ] `lightrag/api/config.py` — Sanitization regex fixed (C5)
- [ ] All existing `tests/workspace/` tests pass unchanged
- [ ] Manual smoke test: send request with `LIGHTRAG-WORKSPACE: test-ws` header and verify correct namespace
