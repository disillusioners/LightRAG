# Lesson: API Contract Mismatch — created_at vs first_seen/last_seen

**Date**: 2026-07-17
**Phase**: Workspace Isolation v2 Phase 3

## Problem
Backend `WorkspaceInfo` returned `created_at` but frontend expected `first_seen`/`last_seen`. The backend and frontend were developed in parallel phases (Phase 1 backend, Phase 2 frontend) and the contract diverged.

## Root Cause
- No shared contract definition (e.g., OpenAPI schema, shared types) between backend and frontend.
- Backend `WorkspaceRegistry` used a single `created_at` timestamp; frontend TypeScript interface declared `first_seen` + `last_seen` with different semantics (first_seen = immutable registration time; last_seen = last activity).

## Fix Applied
Aligned backend to frontend (Phase 2 frontend already shipped — changing backend is cheaper than rolling back frontend):
- `workspace_registry.py`: `register()` now sets both `first_seen` (immutable) and `last_seen` (bumped on re-registration).
- `workspace_routes.py`: `WorkspaceInfo` Pydantic model updated to `first_seen: str` + `last_seen: str`.

## Before/After
- Before: `{name, created_at, document_count}` → frontend gets `undefined` for first_seen/last_seen
- After: `{name, first_seen, last_seen, document_count}` → frontend renders correctly

## Prevention
- Use a shared contract (OpenAPI schema generated from Pydantic models, consumed by frontend codegen).
- Add integration tests that validate the actual API response shape against the frontend's expected type (test_workspace_routes.py now does this).

## Commit
`885a2421` — `fix(api): align workspace metadata contract to frontend (first_seen/last_seen)`
