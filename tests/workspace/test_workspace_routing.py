"""Tests for per-request workspace routing in LightRAG API routers.

Every API route reads the ``LIGHTRAG-WORKSPACE`` header via
``get_workspace_from_request(request)`` in ``lightrag/api/utils_api.py``,
passes it to ``workspace_mgr.acquire(workspace)``, calls the underlying
:mod:`lightrag` methods on the returned rag, and finally invokes
``workspace_mgr.release(workspace)``.

This test pack verifies that:

1. The header value (sanitized or not) reaches ``acquire`` unchanged.
2. Missing / empty / whitespace-only headers fall back to ``None`` so the
   manager resolves the default workspace.
3. Invalid characters are sanitized while **hyphens are preserved**
   (the regex is ``re.sub(r"[^a-zA-Z0-9_-]", "_", workspace)``).
4. ``release`` is always called exactly once per request — both on
   success and when the underlying rag method raises.
5. ``WorkspaceCacheFullError`` from ``acquire`` surfaces as ``HTTP 503``.
6. Streaming endpoints (``/query/stream``) also acquire / release
   correctly under the same header.

The tests use a tiny :class:`SpyWorkspaceManager` plus an ``AsyncMock``
``rag`` so no real LightRAG instance, storage backend, or LLM provider
is touched. Routers are mounted one at a time via the factory functions
(``create_graph_routes``, ``create_query_routes``) on a minimal FastAPI
app.

Import-time note
----------------
Importing :mod:`lightrag.api.routers.graph_routes` /
:mod:`lightrag.api.routers.query_routes` transitively triggers
``lightrag.api.auth.AuthHandler()`` at module load, which reads
``global_args.auth_accounts`` and forces ``parse_args()`` to run against
``sys.argv``. Under pytest that argv contains the test-path / flag
arguments ``argparse`` doesn't recognize, so we seed ``sys.argv`` with a
``lightrag-server``-shaped argv **before** the first lightrag.api import.
The result is cached in ``_global_args``, so subsequent tests are
unaffected.
"""

from __future__ import annotations

import sys

# Seed sys.argv BEFORE importing any lightrag.api router. Idempotent:
# argparse caches the parsed result in _global_args, so subsequent tests
# aren't disturbed.
sys.argv = ["lightrag-server"]

from typing import Any, Optional  # noqa: E402

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from lightrag.api.routers.graph_routes import create_graph_routes  # noqa: E402
from lightrag.api.routers.query_routes import create_query_routes  # noqa: E402
from lightrag.api.workspace_manager import WorkspaceCacheFullError  # noqa: E402
from lightrag.api.workspace_registry import WorkspaceRegistry  # noqa: E402

pytestmark = pytest.mark.offline


# ---------------------------------------------------------------------------
# Hermetic env isolation
# ---------------------------------------------------------------------------
#
# The combined-auth dependency inside ``lightrag.api.utils_api`` reads
# module-level flags (``auth_configured`` and ``whitelist_patterns``) at
# request time. Force a known-empty auth surface so the developer's local
# ``.env`` cannot trip a 401 in these tests.

_ENV_VARS_TO_ISOLATE = (
    "AUTH_ACCOUNTS",
    "LIGHTRAG_API_KEY",
    "TOKEN_SECRET",
    "WHITELIST_PATHS",
)


@pytest.fixture(autouse=True)
def _isolate_auth_env(monkeypatch):
    """Strip auth-related env vars and pin module flags for hermetic runs."""
    import lightrag.api.auth as auth_mod
    import lightrag.api.utils_api as utils_api

    for var in _ENV_VARS_TO_ISOLATE:
        monkeypatch.delenv(var, raising=False)

    monkeypatch.setattr(auth_mod.auth_handler, "accounts", {})
    monkeypatch.setattr(utils_api, "auth_configured", False)
    monkeypatch.setattr(utils_api, "whitelist_patterns", [])
    yield


# ---------------------------------------------------------------------------
# Spy WorkspaceManager
# ---------------------------------------------------------------------------


class SpyWorkspaceManager:
    """Duck-typed WorkspaceManager that records every acquire / release call.

    Attributes:
        rag_mock: An object the route handlers treat as the resolved
            :class:`LightRAG` instance. Tests configure methods on it
            (``get_graph_labels``, ``aquery_llm``, ...) to control
            downstream behaviour.
        acquired_workspaces: Workspace names passed to :meth:`acquire`,
            in call order.
        released_workspaces: Workspace names passed to :meth:`release`,
            in call order.
    """

    def __init__(self, rag_mock: Any) -> None:
        self.rag_mock = rag_mock
        self.acquired_workspaces: list[Optional[str]] = []
        self.released_workspaces: list[Optional[str]] = []

    async def acquire(self, workspace: Optional[str] = None) -> Any:
        self.acquired_workspaces.append(workspace)
        return self.rag_mock

    async def release(self, workspace: Optional[str] = None) -> None:
        self.released_workspaces.append(workspace)

    def get_default_workspace(self) -> str:
        return ""

    async def get_default_instance(self) -> Any:
        return self.rag_mock

    async def get_registry(self) -> WorkspaceRegistry:
        return WorkspaceRegistry()


# ---------------------------------------------------------------------------
# RAG mock helpers
# ---------------------------------------------------------------------------


def _build_rag_mock(graph_labels: list[str] | None = None) -> Any:
    """Build an AsyncMock that pretends to be a LightRAG instance.

    ``aquery_llm`` returns a shape that satisfies the ``/query`` /
    ``/query/stream`` response builders, and ``get_graph_labels`` returns
    a configurable list so ``/graph/label/list`` tests can assert on
    output if desired.
    """
    from unittest.mock import AsyncMock

    rag = AsyncMock()

    rag.get_graph_labels = AsyncMock(return_value=graph_labels or [])

    # aquery_llm return shape: {"llm_response": {"content": str},
    #                           "data": {"references": [...]}}
    rag.aquery_llm = AsyncMock(
        return_value={
            "llm_response": {"content": "mocked answer"},
            "data": {"references": [], "chunks": []},
        }
    )

    return rag


# ---------------------------------------------------------------------------
# App builders
# ---------------------------------------------------------------------------


def _build_graph_app(spy: SpyWorkspaceManager) -> FastAPI:
    """Mount only the graph router on a minimal FastAPI app."""
    app = FastAPI()
    app.include_router(create_graph_routes(spy, api_key=None))
    return app


def _build_query_app(spy: SpyWorkspaceManager) -> FastAPI:
    """Mount only the query router on a minimal FastAPI app."""
    app = FastAPI()
    app.include_router(create_query_routes(spy, api_key=None))
    return app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def spy_and_client():
    """Return a tuple ``(spy, client, rag_mock)`` with the graph router mounted.

    The TestClient is fresh per-test; the FastAPI app is the same one
    the client wraps, so re-creating the client each test avoids any
    risk of stale state across tests.
    """
    rag_mock = _build_rag_mock(graph_labels=["Person", "Organization"])
    spy = SpyWorkspaceManager(rag_mock)
    app = _build_graph_app(spy)
    client = TestClient(app)
    return spy, client, rag_mock


@pytest.fixture
def spy_and_query_client():
    """Return ``(spy, client, rag_mock)`` with the query router mounted."""
    rag_mock = _build_rag_mock()
    spy = SpyWorkspaceManager(rag_mock)
    app = _build_query_app(spy)
    client = TestClient(app)
    return spy, client, rag_mock


# ---------------------------------------------------------------------------
# Tests — header -> acquire(workspace) routing
# ---------------------------------------------------------------------------


class TestHeaderRoutesToCorrectWorkspace:
    """Test 1: A request carrying ``LIGHTRAG-WORKSPACE: ws-a`` causes
    ``workspace_mgr.acquire`` to be called with ``"ws-a"``."""

    def test_header_routes_to_correct_workspace(self, spy_and_client) -> None:
        spy, client, _rag = spy_and_client

        resp = client.get(
            "/graph/label/list",
            headers={"LIGHTRAG-WORKSPACE": "ws-a"},
        )

        assert resp.status_code == 200
        assert spy.acquired_workspaces == ["ws-a"]


class TestMissingHeaderUsesDefault:
    """Test 2: A request without the header falls back to ``None`` so the
    manager resolves the default workspace."""

    def test_missing_header_uses_default(self, spy_and_client) -> None:
        spy, client, _rag = spy_and_client

        resp = client.get("/graph/label/list")

        assert resp.status_code == 200
        # No header → get_workspace_from_request returns None →
        # acquire is called with None.
        assert spy.acquired_workspaces == [None]


class TestEmptyHeaderUsesDefault:
    """Test 3: A literal empty-string header is treated as 'unset'."""

    def test_empty_header_uses_default(self, spy_and_client) -> None:
        spy, client, _rag = spy_and_client

        resp = client.get("/graph/label/list", headers={"LIGHTRAG-WORKSPACE": ""})

        assert resp.status_code == 200
        assert spy.acquired_workspaces == [None]


class TestWhitespaceOnlyHeaderUsesDefault:
    """Bonus: Whitespace-only header is treated as unset.

    ``get_workspace_from_request`` strips whitespace and then treats an
    empty result as 'no header'.
    """

    def test_whitespace_only_header_uses_default(self, spy_and_client) -> None:
        spy, client, _rag = spy_and_client

        resp = client.get("/graph/label/list", headers={"LIGHTRAG-WORKSPACE": "   "})

        assert resp.status_code == 200
        assert spy.acquired_workspaces == [None]


class TestInvalidCharsSanitized:
    """Test 4: Characters outside ``[a-zA-Z0-9_-]`` are replaced by '_'."""

    def test_invalid_chars_sanitized(self, spy_and_client) -> None:
        spy, client, _rag = spy_and_client

        resp = client.get(
            "/graph/label/list",
            headers={"LIGHTRAG-WORKSPACE": "my..workspace!!"},
        )

        assert resp.status_code == 200
        # Dots and '!' are not in [a-zA-Z0-9_-] → replaced with '_'.
        assert spy.acquired_workspaces == ["my__workspace__"]


class TestHyphenPreserved:
    """Test 5 (CRITICAL): Hyphens survive sanitization.

    The regex is ``re.sub(r"[^a-zA-Z0-9_-]", "_", workspace)`` — the
    trailing ``-`` keeps hyphens. Tenant names commonly include
    hyphens; rewriting them to underscores would merge distinct tenants
    in the LRU cache.
    """

    def test_hyphen_preserved(self, spy_and_client) -> None:
        spy, client, _rag = spy_and_client

        resp = client.get(
            "/graph/label/list",
            headers={"LIGHTRAG-WORKSPACE": "my-tenant"},
        )

        assert resp.status_code == 200
        assert spy.acquired_workspaces == ["my-tenant"], (
            "Hyphen must survive sanitization; the regex preserves '-' "
            "alongside alphanumerics and '_'."
        )


class TestMultipleWorkspacesStayDistinct:
    """Sanity: Different header values route to different acquires.

    Verifies that the per-request routing actually differentiates
    workspaces rather than collapsing to a single value.
    """

    def test_multiple_workspaces_stay_distinct(self, spy_and_client) -> None:
        spy, client, _rag = spy_and_client

        client.get("/graph/label/list", headers={"LIGHTRAG-WORKSPACE": "alpha"})
        client.get("/graph/label/list", headers={"LIGHTRAG-WORKSPACE": "beta-2"})
        client.get(
            "/graph/label/list",
            headers={"LIGHTRAG-WORKSPACE": "gamma..3"},
        )

        assert spy.acquired_workspaces == ["alpha", "beta-2", "gamma__3"]


# ---------------------------------------------------------------------------
# Tests — acquire/release lifecycle
# ---------------------------------------------------------------------------


class TestReleaseAlwaysCalledOnSuccess:
    """Test 6: A successful request results in exactly one acquire and one
    release, with matching workspace names."""

    def test_release_always_called_on_success(self, spy_and_client) -> None:
        spy, client, _rag = spy_and_client

        resp = client.get(
            "/graph/label/list",
            headers={"LIGHTRAG-WORKSPACE": "ws-a"},
        )

        assert resp.status_code == 200
        assert len(spy.acquired_workspaces) == 1
        assert len(spy.released_workspaces) == 1
        assert spy.acquired_workspaces == spy.released_workspaces == ["ws-a"]


class TestReleaseCalledOnException:
    """Test 7: If the underlying rag method raises, ``release`` is still
    invoked exactly once.

    The handler pattern is::

        try:
            rag = await workspace_mgr.acquire(workspace)
            return await rag.<method>()
        except Exception:
            raise HTTPException(...)
        finally:
            if rag is not None:
                await workspace_mgr.release(workspace)

    so the ``finally`` clause runs even when the call body fails.
    """

    def test_release_called_on_exception(self, spy_and_client) -> None:
        from unittest.mock import AsyncMock

        spy, client, rag = spy_and_client

        # Configure the rag method to raise — the handler will convert
        # the error into a 500, but release must still fire.
        rag.get_graph_labels = AsyncMock(
            side_effect=RuntimeError("simulated failure"),
        )

        resp = client.get(
            "/graph/label/list",
            headers={"LIGHTRAG-WORKSPACE": "ws-a"},
        )

        # Handler turns the exception into HTTP 500.
        assert resp.status_code == 500
        # Acquire fired once, release fired once, with the same value.
        assert spy.acquired_workspaces == ["ws-a"]
        assert spy.released_workspaces == ["ws-a"]
        assert len(spy.acquired_workspaces) == len(spy.released_workspaces) == 1


class TestReleaseCalledOnWorkspaceCacheFull:
    """When ``acquire`` raises :class:`WorkspaceCacheFullError` the
    handler maps it to HTTP 503. No rag was acquired, so the handler's
    ``finally`` short-circuits (``rag is not None``) — no release call.
    This test pins that contract."""

    def test_release_called_on_workspace_cache_full(self, spy_and_client) -> None:
        spy, client, _rag = spy_and_client

        # Make acquire itself raise the cache-full error.
        async def raising_acquire(workspace):
            spy.acquired_workspaces.append(workspace)
            raise WorkspaceCacheFullError(max_instances=2)

        spy.acquire = raising_acquire  # type: ignore[assignment]

        resp = client.get(
            "/graph/label/list",
            headers={"LIGHTRAG-WORKSPACE": "ws-a"},
        )

        assert resp.status_code == 503
        # acquire was attempted; release is NOT called (rag was never
        # returned to the handler).
        assert spy.acquired_workspaces == ["ws-a"]
        assert spy.released_workspaces == []


class TestCacheFullReturns503:
    """Test 8: ``WorkspaceCacheFullError`` from ``acquire`` surfaces as
    HTTP 503 with a Retry-After hint."""

    def test_cache_full_returns_503(self, spy_and_client) -> None:
        spy, client, _rag = spy_and_client

        async def raising_acquire(workspace):
            spy.acquired_workspaces.append(workspace)
            raise WorkspaceCacheFullError(max_instances=4)

        spy.acquire = raising_acquire  # type: ignore[assignment]

        resp = client.get("/graph/label/list")

        assert resp.status_code == 503
        # Retry-After header signals clients to back off.
        assert resp.headers.get("retry-after") == "5"


# ---------------------------------------------------------------------------
# Tests — streaming + background tasks + acquired-rag identity
# ---------------------------------------------------------------------------


class TestStreamingWorkspaceRouting:
    """Test 9: ``/query/stream`` acquires the right workspace before
    streaming and releases it after streaming finishes.

    Streaming is delicate: the handler releases once via an inner
    ``_release_once`` callable so the async generator's ``finally`` and
    the outer ``finally`` cannot double-release. This test verifies that
    exactly one release fires, regardless of streaming internals.
    """

    def test_streaming_workspace_routing(self, spy_and_query_client) -> None:
        spy, client, _rag = spy_and_query_client

        resp = client.post(
            "/query/stream",
            headers={"LIGHTRAG-WORKSPACE": "stream-ws"},
            json={"query": "hello world", "stream": True},
        )

        assert resp.status_code == 200
        # Drain the streaming body so the async generator's finally
        # runs before we assert on the release count.
        _ = resp.text

        assert spy.acquired_workspaces == ["stream-ws"], (
            "Streaming endpoint must acquire the workspace derived from "
            "the LIGHTRAG-WORKSPACE header."
        )
        assert len(spy.released_workspaces) == 1, (
            f"Expected exactly one release after streaming; got "
            f"{len(spy.released_workspaces)}. The handler uses "
            "_release_once to avoid double-release between the streaming "
            "generator's finally and the outer try/finally."
        )
        assert spy.released_workspaces[0] == "stream-ws"


class TestAcquiredWorkspaceMatchesRag:
    """Test 12: The rag returned from ``acquire`` is the one the handler
    uses to call downstream methods.

    Concretely: the handler does ``await rag.<method>(...)`` and the
    spy returns ``self.rag_mock`` from every acquire, so the mock's
    method must be the one that fires. This is a stronger guarantee
    than mere acquire-counting — it pins down that the *same* rag
    flows through to the call site.
    """

    def test_acquired_workspace_matches_rag(self, spy_and_client) -> None:
        from unittest.mock import AsyncMock

        spy, client, rag = spy_and_client
        # Replace get_graph_labels with a sentinel-returning mock so we
        # can assert the *exact* rag object that the handler invokes.
        rag.get_graph_labels = AsyncMock(return_value=["SENTINEL"])

        resp = client.get(
            "/graph/label/list",
            headers={"LIGHTRAG-WORKSPACE": "ws-a"},
        )

        assert resp.status_code == 200
        assert resp.json() == ["SENTINEL"]
        # The spy returned self.rag_mock from acquire; the handler
        # called get_graph_labels on it.
        rag.get_graph_labels.assert_awaited_once()
        # And the rag object the handler used is the one the spy
        # returned — same identity, not a copy.
        assert rag is spy.rag_mock
