# Test Packs — Workspace Isolation v2

## Summary
- Total: 9 packs (10 new test files + 5 existing test files updated)
- Unit: 4 | Integration/Feature: 5 | E2E: 0

## Unit Test Packs

| Pack | Location | Scope | Last Run | Status |
|------|----------|-------|----------|--------|
| workspace_registry_unit_test | tests/packs/workspace_registry_unit_test.sh | WorkspaceRegistry register/list/concurrency | 2026-07-17 | PASS (6/6) |
| workspace_manager_unit_test | tests/packs/workspace_manager_unit_test.sh | WorkspaceManager LRU, refcount, cache-full, eviction | 2026-07-17 | PASS (14/14) |
| role_llm_builder_test | tests/packs/role_llm_builder_test.sh | register_role_llm_builder on new/evicted instances | 2026-07-17 | PASS (5/5) |
| sanitization_alignment_test | tests/packs/sanitization_alignment_test.sh | Backend/frontend regex parity, hyphen preservation | 2026-07-17 | PASS (21/21) |

## Integration/Feature Test Packs

| Pack | Location | Scope | Last Run | Status |
|------|----------|-------|----------|--------|
| workspace_routing_test | tests/packs/workspace_routing_test.sh | Per-request routing, header extraction, acquire/release lifecycle, 503 | 2026-07-17 | PASS (13/13) |
| workspace_routes_api_test | tests/packs/workspace_routes_api_test.sh | GET /workspaces response shape, auth, contract | 2026-07-17 | PASS (6/6) |
| ollama_workspace_routing_test | tests/packs/ollama_workspace_routing_test.sh | OllamaAPI per-workspace routing, workspace-independent endpoints | 2026-07-17 | PASS (9/9) |
| streaming_release_test | tests/packs/streaming_release_test.sh | Streaming release safety, ASGI cancellation, double-release guard | 2026-07-17 | PASS (6/6) |
| backward_compat_test | tests/packs/backward_compat_test.sh | Backward compatibility, no-header default behavior | 2026-07-17 | PASS (9 passed, 1 skipped) |

## Updated Existing Test Files (route factory signature fix)

| File | Changes | Status |
|------|---------|--------|
| tests/api/routes/_fake_workspace_manager.py | NEW — shared FakeWorkspaceManager helper | PASS |
| tests/api/routes/test_document_routes_chunking.py | 1 call site updated | PASS (58 tests) |
| tests/api/routes/test_document_routes_paginated.py | 2 call sites updated | PASS (6 tests) |
| tests/api/routes/test_document_routes_docx_archive.py | 17 call sites + 9 endpoint-call updates | PASS (52 tests) |
| tests/api/routes/test_graph_routes_pipeline_busy.py | 2 call sites updated | PASS (11 tests) |
| tests/llm/ollama_impl/test_ollama_role_kwargs.py | 1 call site updated | PASS (2 tests) |

## Aggregate Results (2026-07-17)
- tests/workspace/ (excl. migration_isolation): **146 passed, 0 failed, 1 skipped** (3.76s)
- tests/api/routes/: **149 passed, 0 failed, 4 skipped** (2.69s)
- tests/llm/ollama_impl/test_ollama_role_kwargs.py: **2 passed, 0 failed** (0.91s)
- test_workspace_migration_isolation.py: BLOCKED (pre-existing `pgvector` missing — not a v2 regression)

---

## Merge Verification Run (2026-07-19)

**Merge:** `main` (reservation model) ⊗ `feature/workspace-isolation-v2` — merge commit `25bfca30`

| Pack | Scope | Result | Passed/Failed/Skipped | Runtime |
|------|-------|--------|----------------------|---------|
| import (inline) | 5 route modules + server | PASS | 5/5 import clean | < 1 min |
| feature-presence (inline) | 8 grep checks + force_reset wrap | PASS | 8/8 present | < 1 min |
| workspace suite | tests/workspace/ | PASS (after fix) | 147/0/1 | 3.36s |
| api routes | tests/api/routes/ | FAIL | 145/28/4 | 3.50s |
| upstream new | tests/extraction, kg, pipeline | FAIL (classified) | 673/16/28 + 28 coll err | 40.64s |

**Quick-fix commits during verification:**
- `25bfca30` — _FakeQueryRequest.include_progress (also concluded the merge)
- `5a565de3` — API route patch paths (lightrag_server.LightRAG → workspace_manager.LightRAG)

**Known debt (post-merge):**
- 27 failures in test_document_routes_docx_archive.py — needs structural refactor for v2 factory signature
- 3 failures in test_reservation_dead_process_recovery.py — needs design decision on http_request param
- 1 failure in test_query_stream_routes.py — needs deeper pipeline mocking
- 40 failures in tests/kg/ — pre-existing missing deps (pgvector/pymilvus/neo4j/memgraph), NOT regressions

See RESULTS/2026-07-19-merge-verification.md for full report.
