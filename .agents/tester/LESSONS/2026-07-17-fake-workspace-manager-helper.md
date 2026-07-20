# Lesson: Shared FakeWorkspaceManager Helper for Route Factory Tests

**Date**: 2026-07-17
**Phase**: Workspace Isolation v2 Phase 3

## Problem
When route factory signatures changed from `create_*_routes(rag, ...)` to `create_*_routes(workspace_mgr, ...)`, 23 test call sites across 5 files broke. Each needed a fake WorkspaceManager that wraps the existing mock rag.

## Solution
Created a shared helper: `tests/api/routes/_fake_workspace_manager.py` with a `FakeWorkspaceManager` class that:
- Wraps a mock rag and returns it on `acquire()`
- No-ops on `release()`
- Returns `""` for `get_default_workspace()`
- Returns the mock rag for `get_default_instance()`

For spy-based tests (routing, streaming, ollama), a separate `SpyWorkspaceManager` class was used inline per test file (records acquire/release calls).

## Pattern
- **Signature-agnostic tests** (lambda patches): already work — no change needed.
- **Direct-call tests**: use `FakeWorkspaceManager` (wraps existing mock, minimal surface).
- **Spy tests** (verify acquire/release lifecycle): use a dedicated `SpyWorkspaceManager` with counters.

## Commit
`ae921d8d` — `test: update route factory signatures for workspace_mgr`
