"""Smoke test: WorkspaceCacheFullError must surface as HTTP 503, not HTTP 500.

This is the regression test for the pre-existing bug where
:class:`lightrag.api.workspace_manager.WorkspaceCacheFullError` was being
caught by a generic ``except Exception`` block (and remapped to 500 via
``internal_server_error``) instead of by the dedicated
``except WorkspaceCacheFullError`` block that raises ``HTTPException(503, ...)``.

The fix is applied across all route handlers in
``lightrag/api/routers/document_routes.py`` (and the other route modules).

Strategy: wire a fake workspace manager whose ``acquire()`` raises
``WorkspaceCacheFullError``, then drive a small set of routes through
``fastapi.testclient.TestClient``. Every endpoint must respond 503 with
a ``Retry-After`` header — anything else is a regression.
"""

from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Force module reload under test: matches the pattern used by the other
# document-routes tests, which capture argv before importing the router so
# module-level configuration stays predictable.
_original_argv = sys.argv[:]
sys.argv = [sys.argv[0]]
_document_routes = importlib.import_module("lightrag.api.routers.document_routes")
sys.argv = _original_argv

create_document_routes = _document_routes.create_document_routes

from lightrag.api.workspace_manager import WorkspaceCacheFullError  # noqa: E402

pytestmark = pytest.mark.offline


class _CacheFullWorkspaceManager:
    """Stand-in whose ``acquire()`` always raises ``WorkspaceCacheFullError``.

    Mirrors the surface of the production ``WorkspaceManager`` just enough
    to satisfy the route handlers' lifecycle (acquire/release/get_registry).
    """

    def __init__(self) -> None:
        self.acquire_calls: list[str | None] = []
        self.release_calls: list[str | None] = []

    async def acquire(self, workspace: str | None = None):
        self.acquire_calls.append(workspace)
        raise WorkspaceCacheFullError(max_instances=1)

    async def release(self, workspace: str | None = None) -> None:
        self.release_calls.append(workspace)

    async def get_registry(self):
        # /documents/text's success path calls registry.register(workspace);
        # for this smoke test acquire() always fails first, so registry is
        # never reached — but we still expose it for completeness.
        class _NoOpRegistry:
            async def register(self, workspace: str | None = None) -> None:
                return None

        return _NoOpRegistry()


def _build_client(mgr: _CacheFullWorkspaceManager) -> TestClient:
    # ``base_input_dir`` is read by ``/documents/scan`` before the handler
    # reaches ``workspace_mgr.acquire()``; the route is exercised below, so
    # a real-looking path keeps the failure mode under test on the
    # ``acquire()`` call rather than masking it with an unrelated AttributeError.
    doc_manager = SimpleNamespace(base_input_dir="/tmp")
    app = FastAPI()
    # Reservation-holding background work is tracked via app.state.background_tasks;
    # /documents/text, /upload, etc. resolve ``get_managed_background_tasks`` from
    # there. Without this set the dependency would 500 for an unrelated reason.
    app.state.background_tasks = set()
    app.include_router(create_document_routes(mgr, doc_manager, api_key="smoke-key"))
    return TestClient(app)


_HEADERS = {"X-API-Key": "smoke-key"}


@pytest.mark.parametrize(
    "method,path,json_body",
    [
        # Plain text insert — JSON body, exercises /documents/text path.
        ("POST", "/documents/text", {"text": "hello", "file_source": "smoke.txt"}),
        # Multi-text insert — JSON body, exercises /documents/texts path.
        (
            "POST",
            "/documents/texts",
            {"texts": ["a", "b"], "file_sources": ["a.txt", "b.txt"]},
        ),
        # Read-only endpoint — exercises a non-mutating handler.
        ("GET", "/documents/pipeline_status", None),
    ],
)
def test_workspace_cache_full_returns_503(method, path, json_body):
    mgr = _CacheFullWorkspaceManager()
    client = _build_client(mgr)

    if method == "POST":
        response = client.post(path, headers=_HEADERS, json=json_body)
    else:
        response = client.get(path, headers=_HEADERS)

    # Core assertion: the bug mapped this to 500; the fix maps it to 503.
    assert response.status_code == 503, (
        f"{method} {path} returned {response.status_code}, expected 503. "
        "WorkspaceCacheFullError may have been swallowed by a generic "
        "except Exception handler — see the dedicated handler block."
    )
    # Belt-and-braces: production sets Retry-After; verify it's there.
    assert "retry-after" in {k.lower() for k in response.headers.keys()}, (
        f"{method} {path} 503 response is missing the Retry-After header. "
        f"Headers seen: {dict(response.headers)}"
    )


def test_workspace_cache_full_503_payload_contract():
    """Verify the 503 body and header match the production contract exactly."""
    mgr = _CacheFullWorkspaceManager()
    client = _build_client(mgr)

    response = client.post(
        "/documents/text",
        headers=_HEADERS,
        json={"text": "x", "file_source": "smoke.txt"},
    )

    assert response.status_code == 503
    assert response.headers.get("retry-after") == "5"
    body = response.json()
    # FastAPI wraps the detail into {"detail": ...}; the production handler
    # sets detail="Workspace cache is full."
    assert body.get("detail") == "Workspace cache is full."
    # acquire() must have been invoked at least once for this request.
    assert mgr.acquire_calls, "workspace_mgr.acquire() was never called"


def test_workspace_cache_full_does_not_leak_500_on_outer_handler():
    """Regression: ensure no route surfaces as 500 for this error class.

    Sweeps a broader set of document routes in one client to catch any
    handler that regressed back to the pre-fix behavior.
    """
    mgr = _CacheFullWorkspaceManager()
    client = _build_client(mgr)

    endpoints = [
        ("POST", "/documents/text", {"text": "x", "file_source": "a.txt"}),
        ("POST", "/documents/texts", {"texts": ["x"], "file_sources": ["a.txt"]}),
        ("POST", "/documents/scan", {}),
        ("GET", "/documents", None),
        ("GET", "/documents/pipeline_status", None),
    ]

    failures: list[tuple[str, str, int]] = []
    for method, path, body in endpoints:
        if method == "POST":
            resp = client.post(path, headers=_HEADERS, json=body or {})
        else:
            resp = client.get(path, headers=_HEADERS)
        if resp.status_code == 500:
            failures.append((method, path, resp.status_code))

    assert not failures, (
        "The following routes still return 500 for WorkspaceCacheFullError "
        f"(should be 503): {failures}"
    )
