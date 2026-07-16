"""Tests for per-request workspace routing in the Ollama-compatible API.

The :class:`OllamaAPI` router (``lightrag/api/routers/ollama_api.py``) takes
a :class:`WorkspaceManager` instead of a global :class:`LightRAG` instance.
Each request resolves its workspace via the ``LIGHTRAG-WORKSPACE`` header,
acquires a per-workspace :class:`LightRAG`, and releases it when the
response is delivered. This test pack verifies that contract.

What this pack covers
---------------------

1. **Workspace-dependent endpoints** (``/api/generate``, ``/api/chat``)
   acquire the workspace parsed from the header, route the call through
   the acquired rag, and release the workspace after the response.
2. **Workspace-independent endpoints** (``/api/tags``, ``/api/ps``,
   ``/api/version``) do not acquire any workspace — they fall back to
   the default-instance ``ollama_server_infos`` via
   :meth:`OllamaAPI._resolve_ollama_server_infos` (or, for ``/api/version``,
   a hard-coded literal string).
3. **Default-workspace fallback** when no header is supplied: the
   handler passes ``None`` to ``acquire`` and ``release``, which the
   manager normalizes to the configured default workspace.
4. **Release discipline** for both streaming and non-streaming paths:
   the handler's ``_release_once`` ensures each request releases the
   workspace exactly once, even when the underlying rag method raises.

Hermetic test design
--------------------

The tests use a duck-typed :class:`SpyWorkspaceManager` plus an
``AsyncMock`` rag so no real LightRAG instance, storage backend, or LLM
provider is touched. The router is mounted on a minimal FastAPI app.

Import-time note: importing :mod:`lightrag.api.routers.ollama_api`
transitively triggers ``lightrag.api.auth.AuthHandler()`` at module load,
which forces ``parse_args()`` to run against ``sys.argv``. Under pytest
that argv contains test-path / flag arguments ``argparse`` doesn't
recognize, so we seed ``sys.argv`` with a ``lightrag-server``-shaped
argv **before** the first lightrag.api import.
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
from unittest.mock import AsyncMock, MagicMock  # noqa: E402

from lightrag.api.routers.ollama_api import OllamaAPI  # noqa: E402
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
            (``role_llm_funcs["query"]``, ``aquery``, ...) to control
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


def _build_ollama_infos() -> Any:
    """Build a mock for the ``OllamaServerInfos`` shape consumed by OllamaAPI.

    The Ollama-compat handlers read these fields from
    ``rag.ollama_server_infos`` (resolved via
    :meth:`OllamaAPI._resolve_ollama_server_infos`) when shaping
    ``/api/tags`` / ``/api/ps`` / ``/api/generate`` responses.
    """
    infos = MagicMock()
    infos.LIGHTRAG_MODEL = "mock-model"
    infos.LIGHTRAG_CREATED_AT = "2026-01-01T00:00:00Z"
    infos.LIGHTRAG_SIZE = 1_000_000_000
    infos.LIGHTRAG_DIGEST = "deadbeef"
    infos.LIGHTRAG_NAME = "mock-family"
    return infos


def _build_rag_mock() -> Any:
    """Build an AsyncMock that pretends to be a LightRAG instance.

    The mock covers every attribute the OllamaAPI handlers read:

    * ``role_llm_funcs["query"]`` — used by ``/api/generate`` and by
      ``/api/chat`` when the user prefixes the query with ``/bypass``
      or when an OpenWebUI session-title generation pattern is
      detected. Returns a string (non-streaming case).
    * ``role_llm_kwargs["query"]`` — ``None`` so the handler falls
      through to ``llm_model_kwargs`` for the kwargs dict.
    * ``llm_model_kwargs`` — empty dict, used as a fallback kwargs
      source.
    * ``aquery`` — used by ``/api/chat`` non-bypass non-streaming path.
      Returns a string response.
    * ``ollama_server_infos`` — used by the workspace-independent
      ``/api/tags`` and ``/api/ps`` handlers via
      ``_resolve_ollama_server_infos``.
    """
    rag = AsyncMock()

    rag.role_llm_funcs = {
        "query": AsyncMock(return_value="mocked llm response"),
    }
    rag.role_llm_kwargs = {"query": None}
    rag.llm_model_kwargs = {}

    rag.aquery = AsyncMock(return_value="mocked aquery response")

    rag.ollama_server_infos = _build_ollama_infos()

    return rag


# ---------------------------------------------------------------------------
# App builder
# ---------------------------------------------------------------------------


def _build_ollama_app(spy: SpyWorkspaceManager) -> FastAPI:
    """Mount only the Ollama router on a minimal FastAPI app at ``/api``."""
    app = FastAPI()
    ollama_api = OllamaAPI(spy, top_k=60, api_key=None)
    app.include_router(ollama_api.router, prefix="/api")
    return app


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def ollama_spy_and_client():
    """Return ``(spy, client, rag_mock)`` with the Ollama router mounted."""
    rag_mock = _build_rag_mock()
    spy = SpyWorkspaceManager(rag_mock)
    app = _build_ollama_app(spy)
    client = TestClient(app)
    return spy, client, rag_mock


# ---------------------------------------------------------------------------
# Tests — workspace-dependent endpoints (generate / chat)
# ---------------------------------------------------------------------------


class TestOllamaGenerateWorkspaceRouting:
    """Test 1: POST /api/generate with ``LIGHTRAG-WORKSPACE: ws-a`` causes
    ``workspace_mgr.acquire`` to be called with ``"ws-a"``."""

    def test_ollama_generate_workspace_routing(self, ollama_spy_and_client) -> None:
        spy, client, _rag = ollama_spy_and_client

        resp = client.post(
            "/api/generate",
            headers={"LIGHTRAG-WORKSPACE": "ws-a"},
            json={
                "model": "test-model",
                "prompt": "hello",
                "stream": False,
            },
        )

        assert resp.status_code == 200, resp.text
        assert spy.acquired_workspaces == ["ws-a"], (
            "Generate handler must pass the header-derived workspace to "
            "workspace_mgr.acquire()."
        )


class TestOllamaChatWorkspaceRouting:
    """Test 2: POST /api/chat with the workspace header acquires the
    right workspace and routes through ``rag.aquery`` (or, for the
    bypass-prefix case, ``rag.role_llm_funcs['query']``)."""

    def test_ollama_chat_workspace_routing(self, ollama_spy_and_client) -> None:
        spy, client, rag = ollama_spy_and_client

        resp = client.post(
            "/api/chat",
            headers={"LIGHTRAG-WORKSPACE": "chat-ws"},
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "/bypass hi"}],
                "stream": False,
            },
        )

        assert resp.status_code == 200, resp.text
        assert spy.acquired_workspaces == ["chat-ws"]
        # The bypass prefix in /api/chat routes through role_llm_funcs["query"]
        # rather than rag.aquery.
        rag.role_llm_funcs["query"].assert_awaited()


# ---------------------------------------------------------------------------
# Tests — workspace-independent endpoints (tags / ps / version)
# ---------------------------------------------------------------------------


class TestOllamaTagsWorkspaceIndependent:
    """Test 3: GET /api/tags does NOT acquire any workspace.

    ``/api/tags`` is workspace-independent: it reads
    ``ollama_server_infos`` from the default instance via
    :meth:`OllamaAPI._resolve_ollama_server_infos` and shapes the
    model list from there. It must NOT take a per-request refcount.
    """

    def test_ollama_tags_workspace_independent(self, ollama_spy_and_client) -> None:
        spy, client, _rag = ollama_spy_and_client

        resp = client.get("/api/tags")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "models" in body
        assert isinstance(body["models"], list)
        assert len(body["models"]) == 1
        assert body["models"][0]["name"] == "mock-model"
        # Tags never calls acquire — it's workspace-independent.
        assert spy.acquired_workspaces == []


# ---------------------------------------------------------------------------
# Tests — default-workspace fallback
# ---------------------------------------------------------------------------


class TestOllamaGenerateNoHeaderUsesDefault:
    """Test 4: POST /api/generate WITHOUT a header acquires the default
    workspace (the manager normalizes ``None`` to the configured
    default)."""

    def test_ollama_generate_no_header_uses_default(
        self, ollama_spy_and_client
    ) -> None:
        spy, client, _rag = ollama_spy_and_client

        resp = client.post(
            "/api/generate",
            json={
                "model": "test-model",
                "prompt": "hello",
                "stream": False,
            },
        )

        assert resp.status_code == 200, resp.text
        # No header → get_workspace_from_request returns None →
        # acquire is called with None (manager resolves the default).
        assert spy.acquired_workspaces == [None]


# ---------------------------------------------------------------------------
# Tests — release discipline
# ---------------------------------------------------------------------------


class TestOllamaGenerateReleaseCalled:
    """Test 5: After a successful /api/generate response, release has
    been called at least once with the same workspace that was
    acquired."""

    def test_ollama_generate_release_called(self, ollama_spy_and_client) -> None:
        spy, client, _rag = ollama_spy_and_client

        resp = client.post(
            "/api/generate",
            headers={"LIGHTRAG-WORKSPACE": "ws-a"},
            json={
                "model": "test-model",
                "prompt": "hello",
                "stream": False,
            },
        )

        assert resp.status_code == 200, resp.text
        assert len(spy.acquired_workspaces) >= 1
        assert len(spy.released_workspaces) >= 1
        # Acquire and release must agree on workspace value.
        assert spy.acquired_workspaces[0] == "ws-a"
        assert spy.released_workspaces[0] == "ws-a"


class TestOllamaGenerateReleaseCalledOnException:
    """Bonus: If ``role_llm_funcs['query']`` raises, the handler must
    still release the workspace (the ``except Exception`` branch
    invokes ``_release_once`` before re-raising)."""

    def test_ollama_generate_release_called_on_exception(
        self, ollama_spy_and_client
    ) -> None:
        spy, client, rag = ollama_spy_and_client

        rag.role_llm_funcs["query"] = AsyncMock(
            side_effect=RuntimeError("simulated llm failure"),
        )

        resp = client.post(
            "/api/generate",
            headers={"LIGHTRAG-WORKSPACE": "ws-a"},
            json={
                "model": "test-model",
                "prompt": "hello",
                "stream": False,
            },
        )

        # Handler maps generic exceptions to HTTP 500.
        assert resp.status_code == 500
        # Acquire and release must still be balanced.
        assert spy.acquired_workspaces == ["ws-a"]
        assert spy.released_workspaces == ["ws-a"]


class TestOllamaGenerateCacheFullReturns503:
    """Bonus: When ``acquire`` raises :class:`WorkspaceCacheFullError`,
    the handler maps it to HTTP 503 and does NOT call release (the
    rag was never returned to the handler)."""

    def test_ollama_generate_cache_full_returns_503(
        self, ollama_spy_and_client
    ) -> None:
        spy, client, _rag = ollama_spy_and_client

        async def raising_acquire(workspace):
            spy.acquired_workspaces.append(workspace)
            raise WorkspaceCacheFullError(max_instances=2)

        spy.acquire = raising_acquire  # type: ignore[assignment]

        resp = client.post(
            "/api/generate",
            headers={"LIGHTRAG-WORKSPACE": "ws-a"},
            json={
                "model": "test-model",
                "prompt": "hello",
                "stream": False,
            },
        )

        assert resp.status_code == 503
        assert resp.headers.get("retry-after") == "5"
        assert spy.acquired_workspaces == ["ws-a"]
        assert spy.released_workspaces == []


# ---------------------------------------------------------------------------
# Tests — workspace classification across all endpoints
# ---------------------------------------------------------------------------


class TestOllamaAllHandlersSafe:
    """Test 6: Hit each Ollama endpoint and verify nothing crashes.

    Documents which endpoints are workspace-dependent vs independent:

    * ``GET  /api/version``   — workspace-independent (literal string).
    * ``GET  /api/tags``      — workspace-independent (default-instance
                                ``ollama_server_infos``).
    * ``GET  /api/ps``        — workspace-independent (same as /tags).
    * ``POST /api/generate``  — workspace-dependent (acquires per-request).
    * ``POST /api/chat``      — workspace-dependent (acquires per-request).
    """

    def test_ollama_all_handlers_safe(self, ollama_spy_and_client) -> None:
        spy, client, _rag = ollama_spy_and_client

        # --- Workspace-independent endpoints -----------------------------
        v_resp = client.get("/api/version")
        assert v_resp.status_code == 200, v_resp.text
        assert v_resp.json() == {"version": "0.9.3"}

        tags_resp = client.get("/api/tags")
        assert tags_resp.status_code == 200, tags_resp.text
        assert "models" in tags_resp.json()

        ps_resp = client.get("/api/ps")
        assert ps_resp.status_code == 200, ps_resp.text
        assert "models" in ps_resp.json()

        # None of the workspace-independent endpoints must acquire.
        assert spy.acquired_workspaces == [], (
            f"workspace-independent endpoints must NOT acquire; "
            f"got acquires: {spy.acquired_workspaces}"
        )

        # --- Workspace-dependent endpoints ------------------------------
        gen_resp = client.post(
            "/api/generate",
            headers={"LIGHTRAG-WORKSPACE": "ws-a"},
            json={
                "model": "test-model",
                "prompt": "hello",
                "stream": False,
            },
        )
        assert gen_resp.status_code == 200, gen_resp.text

        chat_resp = client.post(
            "/api/chat",
            headers={"LIGHTRAG-WORKSPACE": "ws-b"},
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "/bypass hi"}],
                "stream": False,
            },
        )
        assert chat_resp.status_code == 200, chat_resp.text

        # Two workspace-dependent calls → two acquires (one per workspace).
        assert spy.acquired_workspaces == ["ws-a", "ws-b"]
        # Each acquire was matched by exactly one release.
        assert spy.released_workspaces == ["ws-a", "ws-b"]


class TestOllamaStreamingWorkspaceRouting:
    """Bonus: /api/generate with ``stream=True`` also acquires and
    releases the workspace. The async generator's ``finally`` runs
    ``_release_once``, so we must drain the streaming response before
    asserting on the release count."""

    def test_ollama_streaming_workspace_routing(self, ollama_spy_and_client) -> None:
        spy, client, _rag = ollama_spy_and_client

        with client.stream(
            "POST",
            "/api/generate",
            headers={"LIGHTRAG-WORKSPACE": "stream-ws"},
            json={
                "model": "test-model",
                "prompt": "hello",
                "stream": True,
            },
        ) as resp:
            assert resp.status_code == 200
            # Drain so the streaming generator's finally runs.
            for _ in resp.iter_lines():
                pass

        assert spy.acquired_workspaces == ["stream-ws"]
        assert spy.released_workspaces == ["stream-ws"], (
            "Streaming handler must release via _release_once after the "
            "async generator drains."
        )
