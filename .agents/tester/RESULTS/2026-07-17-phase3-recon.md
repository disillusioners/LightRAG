# Phase 3 Reconnaissance Report

**Date**: 2026-07-17
**Branch**: feature/workspace-isolation-v2
**Session**: recon-contract

## Scope: workspace isolation v2 testing

## Confirmed Issues

### Issue 1: API Contract Mismatch (CRITICAL)
**Backend** `workspace_routes.py` `WorkspaceInfo`:
```python
name: str
created_at: str
document_count: Optional[int] = None
```

**Backend** `WorkspacesResponse`:
```python
workspaces: List[WorkspaceInfo]
default_workspace: str
```

**Frontend** `lightrag.ts` `WorkspaceInfo`:
```typescript
name: string
first_seen: string      # backend does NOT emit this
last_seen: string       # backend does NOT emit this
document_count: number | null
```

**Frontend** `WorkspacesResponse`:
```typescript
workspaces: WorkspaceInfo[]  # backend also emits default_workspace; frontend ignores
```

**Impact**: Frontend gets `undefined` for first_seen/last_seen → broken UI display.
**Resolution**: Align backend to emit `first_seen`/`last_seen` (cheaper than changing frontend; Phase 2 already shipped with this contract).

### Issue 2: Broken existing tests (23 call sites)
Files passing `rag` (LightRAG) as first arg where new factories expect `workspace_mgr` (WorkspaceManager):
- `tests/api/routes/test_document_routes_chunking.py:482`
- `tests/api/routes/test_document_routes_paginated.py:71,148`
- `tests/api/routes/test_document_routes_docx_archive.py` (17 occurrences)
- `tests/api/routes/test_graph_routes_pipeline_busy.py:97,98`
- `tests/llm/ollama_impl/test_ollama_role_kwargs.py:58`

Signature-agnostic tests (lambda patches, work fine): test_health_auth.py, test_bedrock_llm.py

### Issue 3: Minor — backend regex no 64-char truncation
Frontend truncates to 64 chars; backend does not. Divergence noted; low risk.

## Test Pack Planning
- Phase 3 plan calls for 80 tests across 12 files (10 backend new + 2 frontend)
- Existing tests in tests/workspace/ (4 files) must continue to pass
- Strategy: unit test packs by area, then integration, then ensure.md
