# Plan Overview: Workspace Isolation v2 — API + UI Layer

> **Revision 2** — Addresses 6 blocking Reviewer issues (C1–C6) and 4 warnings (W1–W4).

## Objective

Build the missing API routing + workspace management + frontend workspace switching layer on top of upstream `main`'s existing storage-level workspace isolation. This adds per-request workspace routing, a workspace listing API, and a UI workspace selector — without re-implementing any storage isolation that upstream already provides.

## Scope Assessment

**Scope: LARGE** (cross-module: backend API + frontend UI + testing)

**Justification:**
- Touches 3 routers (document, query, graph) with ~65+ closure references to replace
- Touches OllamaAPI class with 15 `self.rag.*` references across 5 handlers (C2)
- Introduces 3 new backend files (WorkspaceManager, WorkspaceRegistry, workspace_routes)
- Modifies the server bootstrap (`lightrag_server.py`) at multiple integration points including `register_role_llm_builder` (C3)
- Handles 7 background task call sites — 3 closures + 1 bound method + 3 explicit task helpers (C1)
- Streaming-safe refcount release for query + Ollama streaming paths (C4)
- Adds a new frontend component + store field + API client function + header injection
- Requires testing across both backend (pytest) and frontend (bun/playwright)
- Each phase is a coherent module deliverable (1 developer instance each)

## Context

- **Project**: LightRAG
- **Working Directory**: `/Users/nguyenminhkha/All/Code/opensource-projects/LightRAG`
- **Branch**: `feature/workspace-isolation-v2` (fresh from `main` @ `9af1d11f`)
- **Previous fork**: `feature/workspace-isolation` (merged into main upstream; reference only)

### What Upstream `main` Already Provides (DO NOT re-implement)

| Feature | Location | Details |
|---------|----------|---------|
| `get_final_namespace()` | `lightrag/kg/shared_storage.py:137` | `f"{workspace}:{namespace}"` prefixing |
| `workspace` parameter on `LightRAG` | `lightrag/lightrag.py:292` | `workspace: str = os.getenv("WORKSPACE", "")` |
| Per-workspace locks | `shared_storage.py:611-896` | `KeyedUnifiedLock` + `get_namespace_lock()` |
| Pipeline status isolation | `shared_storage.py:1376` | `initialize_pipeline_status(workspace=...)` |
| `validate_workspace()` | `lightrag/utils.py:4995` | Path-traversal guard (rejects `/`, `\`, `.`, `..`; **accepts hyphens**) |
| Multi-process support | `shared_storage.py` | `multiprocessing.Manager` for cross-worker |
| `set_default_workspace()` / `get_default_workspace()` | `shared_storage.py:1814-1843` | Backward-compat globals |
| `--workspace` CLI arg / `WORKSPACE` env | `config.py:374` | Sanitized via `re.sub(r"[^a-zA-Z0-9_]", "_", ...)` |
| `LIGHTRAG-WORKSPACE` header parsing | `lightrag_server.py:1426` | `get_workspace_from_request()` — used ONLY by `/health` |
| `register_role_llm_builder` | `lightrag_server.py:2108` | Must be called on EVERY new LightRAG instance (C3) |
| Workspace tests | `tests/workspace/` | 4 test files (validation, isolation, migration, sanitization) |
| `/health` returns workspace config | `lightrag_server.py:2290` | Returns `workspace` + `storage_workspaces` |

---

## Phase Index

| Phase | Name | Objective | Dependencies | Coupling | Est. Time |
|-------|------|-----------|-------------|----------|-----------|
| 1 | Backend: WorkspaceManager + Routes API + OllamaAPI | Build workspace caching/registry + `GET /workspaces` + wire header into all routers incl. Ollama | None | — (root) | 9-11h |
| 2 | Frontend: UI Workspace Switching | Add workspace selector + header injection + store field | Phase 1 (needs `/workspaces` endpoint) | loose (depends on API contract only) | 4-5h |
| 3 | Testing & Integration | E2E tests, backward-compat verification, edge cases | Phase 1 + Phase 2 | tight (needs both implementations) | 4-5h |

**Total Estimated Time: 17-21 hours** (increased from 13-17h to account for OllamaAPI, bg task wrapping, and additional tests)

### Coupling Assessment

| Phase Pair | Coupling | Reasoning | Scheduling |
|------------|----------|-----------|------------|
| Phase 1 → Phase 2 | **loose** | Frontend only needs the `/workspaces` API contract (response shape). Doesn't need Phase 1's internal implementation. Frontend can mock the API during development. | Can pipeline — start Phase 2 frontend work against API contract while Phase 1 finalizes |
| Phase 1 → Phase 3 | **tight** | Backend tests need the actual WorkspaceManager + router wiring to be complete | Sequential — Phase 1 must be code-complete |
| Phase 2 → Phase 3 | **tight** | E2E tests need the frontend selector component working with the backend | Sequential — Phase 2 must be code-complete |

**Parallel opportunity:** Phase 1 and Phase 2 can overlap if the `/workspaces` API response contract is agreed upfront (see API Contract section in phase1-plan.md).

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                         REQUEST FLOW                                 │
│                                                                     │
│  Client (WebUI)                                                     │
│    │ LIGHTRAG-WORKSPACE: "tenant-a"                                 │
│    ▼                                                                │
│  FastAPI Middleware / Route Handler                                 │
│    │ get_workspace_from_request(request)  ← EXISTING (line 1426)   │
│    ▼                                                                │
│  WorkspaceManager.acquire(workspace)   ← NEW (Phase 1)              │
│    │ Checks LRU cache → hit: return cached instance                │
│    │                       miss: create LightRAG(workspace=...)     │
│    │                       + register_role_llm_builder() (C3)       │
│    │                       + auto-register in WorkspaceRegistry     │
│    ▼                                                                │
│  LightRAG instance (per-workspace)                                  │
│    │ rag.workspace = "tenant-a"                                     │
│    │ All storage ops use get_final_namespace() ← EXISTING           │
│    ▼                                                                │
│  Storage backends (already workspace-isolated by upstream)          │
│                                                                     │
│  Release: workspace_mgr.release(workspace)                          │
│    │ Request handlers: release in finally block                     │
│    │ Background tasks: independent acquire/release (C1)             │
│    │ Streaming: one-shot release pattern (C4)                       │
└─────────────────────────────────────────────────────────────────────┘
```

### Key Design Decisions

1. **LRU cache with reference counting** — prevents evicting a LightRAG instance while a request or background task is still using it
2. **Auto-registration on first use** — workspaces are registered when documents are first inserted, not on every request
3. **Closure-preserving router wiring** — minimal change to existing factory pattern; each handler resolves `rag` via `workspace_mgr` instead of using a closure-captured global
4. **Background task safety — ALL 7 call sites wrapped** (C1) — each bg task independently acquires/releases workspace refs:
   - 3 `_indexing_task` closures (upload, text, texts) → add acquire/release inside existing `try/finally`
   - 3 explicit task helpers (`run_scanning_process`, `pipeline_index_texts`, `background_delete_documents`) → wrapper functions
   - 1 bound method (`rag.apipeline_process_enqueue_documents` at L3939) → wrapper function
5. **OllamaAPI fully refactored** (C2) — accepts `workspace_mgr`, per-request acquire/release in all 5 handlers
6. **`register_role_llm_builder` on every new instance** (C3) — extracted to a shared builder factory, called inside `_create_rag_instance()`
7. **Streaming-safe one-shot release** (C4) — exception-safe refcount release with `released` guard flag
8. **Sanitization aligned** (C5) — backend regex changed to `[^a-zA-Z0-9_-]` to accept hyphens, matching `validate_workspace()`
9. **Backward compatibility** — empty/missing workspace header = default behavior (same as current main). The default workspace instance is always pre-loaded.

---

## Risks & Mitigations

| # | Risk | Impact | Likelihood | Mitigation |
|---|------|--------|------------|------------|
| R1 | **LightRAG instance creation is expensive** (storage init, embedding model loading, role_llm_builder registration) | High | High | LRU cache with configurable max size. Pre-load default workspace at startup. Warn if cache miss for new workspace (cold start latency). |
| R2 | **Background task use-after-release** — LRU evicts instance while bg task is running | High | Medium | Reference counting: each bg task independently calls `workspace_mgr.acquire(workspace)` before starting and `release()` in `finally`. **All 7 bg task call sites must be wrapped** (C1). |
| R3 | **Memory pressure** with many workspaces | Medium | Medium | Configurable LRU max size (default: 8 instances). Reference counting ensures safe eviction. Log warnings when approaching limit. |
| R4 | **Breaking existing `/health` endpoint** behavior | Medium | Low | Keep `get_workspace_from_request()` as-is for `/health`. The new routing uses the same helper but resolves rag differently. |
| R5 | **Streaming response refcount leak** on exceptions or ASGI cancellation (C4) | High | Medium | One-shot release pattern with `released` guard flag. Release in both the generator's `finally` AND the outer `except Exception` block. See phase1-plan.md for exact pattern. |
| R6 | **OllamaAPI workspace routing silently broken** (C2) | High | High | Full refactor: `OllamaAPI.__init__` accepts `workspace_mgr`, every handler does acquire/release. Test all 5 endpoints with workspace header. |
| R7 | **Per-workspace instances missing role_llm_builder** (C3) | High | High | Extract builder lambda to a shared factory function. Call inside `_create_rag_instance()` after LightRAG construction. |
| R8 | **Sanitization mismatch causes silent routing bug** (C5) | High | High | Change backend regex from `[^a-zA-Z0-9_]` to `[^a-zA-Z0-9_-]` in `get_workspace_from_request()` and `config.py`. This aligns with `validate_workspace()` which already accepts hyphens. |
| R9 | **Multi-worker/Gunicorn deployment** — LRU cache, refcounting, and registry are per-process (C6) | High | Medium | **For v2: document single-worker requirement** (`--workers 1`). Add session affinity guidance for reverse proxy. Registry persistence uses atomic write (temp + rename). Future: route through `Manager().dict()` or external coordination. See "Multi-Worker Strategy" below. |
| R10 | **DocumentManager is also workspace-bound** | Medium | Medium | `DocumentManager` already accepts `workspace` param. Make per-request in upload/text handlers: `DocumentManager(base_input_dir, workspace=request_workspace)`. |
| R11 | **Store migration breaks existing users** | Low | Low | Follow established linear migration pattern (version 20 → 21). Add `if (version < 21) { state.currentWorkspace = '' }`. |
| R12 | **`/health` leaks workspace namespace info to unauthenticated callers** (W3) | Low | Medium | Audit `/health` response — ensure `storage_workspaces` is only included for authenticated callers (current behavior). Add note in code. |
| R13 | **`document_count` in `/workspaces` response is O(N)** (W4) | Low | Low | Keep `document_count` nullable. Return `null` when count is expensive. Document as best-effort. |
| R14 | **Concurrent cold-start deadlock** (Gap 2) | High | Medium | **`_init_lock = asyncio.Lock()`** serializes `_create_rag_instance()`. Upstream warns at `lightrag.py:1277` that storage init must be one-by-one to prevent deadlock. |
| R15 | **Cache-full under load** (Gap 3) | Medium | Low | Return HTTP 503 with `Retry-After: 5` header. Client retries after in-flight requests release refs. Extremely rare under normal operation. |
| R16 | **Sanitization backward-compat** (Approver note) | Medium | Low | Old deployments using `--workspace=team-a` have data under `team_a` (old regex). C5 fix routes to `team-a` (new, empty). Document migration: rename old storage dirs/prefixes. |

### Multi-Worker Strategy (C6)

**Problem:** Under Gunicorn with N workers, each worker has its own `WorkspaceManager` (LRU cache + refcounting) and `WorkspaceRegistry`. This causes:
1. **N× duplicate LightRAG instances** — memory waste (each worker loads its own set)
2. **Concurrent registry writes** — multiple workers writing `workspace_registry.json` simultaneously → corruption or last-writer-wins
3. **Background task cross-worker divergence** — task scheduled in worker A, request released in worker B → refcount mismatch

**V2 Mitigation (recommended): Single-worker mode**
- Document `--workers 1` as the required deployment mode for workspace isolation
- Add startup warning if `workers > 1` and workspace routing is enabled
- Registry file writes are atomic (write temp file + `os.rename`) — safe even if multiple workers exist, but may have stale reads
- Session affinity (sticky sessions) at reverse proxy level if multi-worker is needed

**Future options (post-v2):**
- (b) Route registry through `multiprocessing.Manager().dict()` for cross-worker sharing
- (c) Use `fcntl`/`portalocker` for file-level locking on registry read-modify-write
- (d) External coordination service (Redis-backed registry)

---

## Success Criteria

### Backend (Phase 1)
- [ ] `GET /workspaces` returns list of known workspaces (default workspace always present)
- [ ] `LIGHTRAG-WORKSPACE` header is honored by ALL document, query, graph, AND Ollama routes (C2)
- [ ] Missing/empty header falls back to default workspace (backward compatible)
- [ ] New workspaces auto-register when documents are first inserted
- [ ] LRU cache evicts least-recently-used instances when at capacity
- [ ] ALL 7 background task call sites correctly acquire/release workspace refs (C1)
- [ ] Every new LightRAG instance has `register_role_llm_builder` called (C3)
- [ ] Streaming handlers use exception-safe one-shot release pattern (C4)
- [ ] Backend sanitization accepts hyphens (`[^a-zA-Z0-9_-]`) (C5)
- [ ] No existing tests break

### Frontend (Phase 2)
- [ ] Workspace selector dropdown visible in `SiteHeader`
- [ ] Selecting a workspace sets `currentWorkspace` in settings store (persisted)
- [ ] All API calls include `LIGHTRAG-WORKSPACE` header when workspace is set
- [ ] Both axios (REST) and fetch (streaming) paths include the header
- [ ] Empty workspace selection = no header sent (backward compatible)
- [ ] Workspace list fetched from `/workspaces` on mount
- [ ] Frontend sanitization regex matches backend: `[^a-z0-9_-]` (C5)

### Testing (Phase 3)
- [ ] Unit tests for WorkspaceManager (LRU, refcount, registry, concurrent eviction) (S1)
- [ ] Unit tests for WorkspaceRegistry (persistence, listing)
- [ ] Integration tests for per-request workspace routing (all 4 routers)
- [ ] **OllamaAPI workspace routing tests** (C2/S1)
- [ ] **ASGI cancellation test** — client disconnect between acquire and generator entry (C4/S1)
- [ ] **`register_role_llm_builder` regression test** — non-default workspace queries use correct LLM (C3/S1)
- [ ] E2E test: create workspace via UI → insert document → query in that workspace
- [ ] Backward compatibility test: no header = default behavior unchanged
- [ ] All existing `tests/workspace/` tests still pass

---

## Revision History

| Version | Date | Changes |
|---------|------|---------|
| v1 | 2026-07-16 | Initial plan |
| v2 | 2026-07-16 | Addressed 6 blocking issues (C1–C6) + 4 warnings (W1–W4) + test gap S1 |
| v2.1 | 2026-07-16 | Added: Gap 1 (`check_and_migrate_data` in `_create_rag_instance`), Gap 2 (`_init_lock` serialization), Gap 3 (cache-full 503 policy), Approver notes (sanitization migration, DocumentManager consistency, ollama_server_infos ordering) |

---

## Tracking

- **Created**: 2026-07-16
- **Last Updated**: 2026-07-16 (Rev 2)
- **Status**: draft (revised)
