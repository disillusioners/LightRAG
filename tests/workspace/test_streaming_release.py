"""Tests for the streaming endpoint's workspace release safety.

The ``/query/stream`` endpoint wraps the streaming response with a release
guard that must fire **exactly once** regardless of how the stream ends:

* Successful stream completion.
* ``aquery_llm`` raises before the :class:`StreamingResponse` is built.
* The inner response iterator raises mid-stream (caught inside the helper).
* The client disconnects mid-stream (ASGI cancellation).
* ``asyncio.CancelledError`` raised inside the generator.

The contract is implemented with a ``released`` flag inside
``_release_once()``: every code path that needs to release goes through the
same idempotent helper, so a double-release cannot happen even if both
the streaming generator's ``finally`` and the outer ``try/finally`` would
otherwise both fire.

These tests pin the contract down by mounting ``create_query_routes`` on
a minimal FastAPI app, swapping the workspace manager for a spy that
counts acquire/release calls, and configuring the underlying rag mock's
``aquery_llm`` to either succeed, raise, or yield a configurable
``response_iterator`` so we can drive the streaming helper through its
end-of-stream and cancellation paths.

Import-time note
----------------
Importing :mod:`lightrag.api.routers.query_routes` transitively triggers
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

import asyncio  # noqa: E402
from typing import Any, AsyncIterator, Optional  # noqa: E402

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from lightrag.api.routers.query_routes import create_query_routes  # noqa: E402
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

    Mirrors the shape used by ``test_workspace_routing`` but exposes
    count-style attributes (``acquire_count``, ``release_count``) that the
    release-safety tests assert on directly.

    Attributes:
        rag_mock: An object the route handlers treat as the resolved
            :class:`LightRAG` instance. Tests configure methods on it
            (``aquery_llm``, ``response_iterator``, ...) to drive the
            streaming endpoint through different end-of-stream scenarios.
        acquired_workspaces: Workspace names passed to :meth:`acquire`,
            in call order.
        released_workspaces: Workspace names passed to :meth:`release`,
            in call order.
    """

    def __init__(self, rag_mock: Any) -> None:
        self.rag_mock = rag_mock
        self.acquired_workspaces: list[Optional[str]] = []
        self.released_workspaces: list[Optional[str]] = []
        # Convenience counters; semantics are identical to len() of the
        # workspace lists above, kept as named attributes for readability
        # in the assertions.
        self.acquire_count: int = 0
        self.release_count: int = 0

    async def acquire(self, workspace: Optional[str] = None) -> Any:
        self.acquired_workspaces.append(workspace)
        self.acquire_count += 1
        return self.rag_mock

    async def release(self, workspace: Optional[str] = None) -> None:
        self.released_workspaces.append(workspace)
        self.release_count += 1

    def get_default_workspace(self) -> str:
        return ""

    async def get_default_instance(self) -> Any:
        return self.rag_mock

    async def get_registry(self) -> WorkspaceRegistry:
        return WorkspaceRegistry()


# ---------------------------------------------------------------------------
# RAG mock helpers
# ---------------------------------------------------------------------------


async def _trivial_response_iterator(chunks: list[str]) -> AsyncIterator[str]:
    """Yield each chunk verbatim from the supplied list.

    ``_build_stream_generator`` iterates ``llm_response["response_iterator"]``
    via ``async for chunk in response_stream``. Tests inject this generator
    to drive the streaming helper through different completion scenarios.
    """
    for chunk in chunks:
        yield chunk


async def _raise_after_first_chunk(
    exc: BaseException, chunks: list[str]
) -> AsyncIterator[str]:
    """Yield each chunk then raise ``exc`` on the next iteration.

    Useful for tests that want to observe end-of-stream handling without
    exhausting the iterator naturally — the inner generator catches
    ``Exception`` but lets ``BaseException`` (such as
    ``asyncio.CancelledError``) propagate.
    """
    for chunk in chunks:
        yield chunk
    raise exc


def _build_rag_mock_streaming(
    response_chunks: list[str] | None = None,
    response_raiser: BaseException | None = None,
    aquery_error: BaseException | None = None,
) -> Any:
    """Build a rag mock whose ``aquery_llm`` returns a streaming result.

    Args:
        response_chunks: Chunks to yield from the synthetic
            ``response_iterator``. Ignored if ``response_raiser`` is set
            after the first chunk (the iterator still yields the supplied
            chunks then raises).
        response_raiser: Optional exception to raise after the chunks are
            exhausted. Use a ``BaseException`` subclass (such as
            ``asyncio.CancelledError``) to bypass the inner generator's
            ``except Exception`` guard.
        aquery_error: If set, ``aquery_llm`` itself raises this exception
            before returning a result — used to drive the outer
            ``except Exception`` release path in the route handler.
    """
    chunks = response_chunks or ["hello", " ", "world"]

    if aquery_error is not None:

        async def _raise_aquery(*_a: Any, **_kw: Any) -> Any:
            raise aquery_error

        rag_aquery = _raise_aquery
    else:

        async def _aquery(*_a: Any, **_kw: Any) -> dict[str, Any]:
            if response_raiser is None:
                iterator: AsyncIterator[str] = _trivial_response_iterator(chunks)
            else:
                iterator = _raise_after_first_chunk(response_raiser, chunks)
            return {
                "llm_response": {
                    "is_streaming": True,
                    "response_iterator": iterator,
                },
                "data": {"references": [], "chunks": []},
            }

        rag_aquery = _aquery

    class _Rag:
        aquery_llm = staticmethod(rag_aquery)

    return _Rag()


# ---------------------------------------------------------------------------
# App builders
# ---------------------------------------------------------------------------


def _build_query_app(spy: SpyWorkspaceManager) -> FastAPI:
    """Mount only the query router on a minimal FastAPI app.

    The factory takes ``top_k``; we pass 60 to match the API server's
    default and keep this test agnostic of that detail.
    """
    app = FastAPI()
    app.include_router(create_query_routes(spy, api_key=None, top_k=60))
    return app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def spy_and_query_client():
    """Return ``(spy, client, rag_mock)`` with the query router mounted."""
    rag_mock = _build_rag_mock_streaming()
    spy = SpyWorkspaceManager(rag_mock)
    app = _build_query_app(spy)
    client = TestClient(app)
    return spy, client, rag_mock


# ---------------------------------------------------------------------------
# Helpers — direct route invocation
# ---------------------------------------------------------------------------


async def _invoke_streaming_endpoint(endpoint, rag_mock: Any, workspace: str) -> Any:
    """Invoke the ``/query/stream`` route handler directly and return the
    :class:`StreamingResponse`.

    Bypassing TestClient gives the tests precise control over the
    StreamingResponse's ``body_iterator`` — we can advance it, cancel it,
    or close it mid-iteration. That maps onto what real ASGI servers do
    on client disconnect, where Starlette's body iterator is ``aclose()``-d
    once the http.disconnect message arrives.
    """
    fake_request = _make_fake_request(workspace)
    req_data = _make_fake_query_request()
    return await endpoint(req_data, fake_request)


def _make_fake_request(workspace: str) -> Any:
    """Build a minimal stand-in for the FastAPI ``Request`` object.

    The route only reads ``request.headers`` (``get_workspace_from_request``),
    so a duck-typed object exposing that attribute is sufficient.
    """

    class _FakeRequest:
        def __init__(self, ws: str) -> None:
            self.headers = {"LIGHTRAG-WORKSPACE": ws}

    return _FakeRequest(workspace)


def _make_fake_query_request() -> Any:
    """Build a minimal ``QueryRequest`` shape accepted by the route.

    The route calls ``request.to_query_params(stream_mode)`` and reads a
    handful of fields. We build a duck-typed object that returns a simple
    ``QueryParam``-shaped instance.
    """
    from lightrag.base import QueryParam

    class _FakeQueryRequest:
        def __init__(self) -> None:
            self.query = "hello world"
            self.mode = "mix"
            self.include_references = True
            self.include_chunk_content = False
            self.stream = True
            self.conversation_history = None
            self.user_prompt = None

        def to_query_params(self, is_stream: bool) -> QueryParam:
            return QueryParam(stream=is_stream)

    return _FakeQueryRequest()


def _find_streaming_endpoint(spy: SpyWorkspaceManager) -> Any:
    """Locate the ``/query/stream`` endpoint function.

    Builds a fresh router via :func:`create_query_routes` so the test
    doesn't have to walk the FastAPI app's ``routes`` (which mixes the
    app's own routes with mounted router routes). The factory returns a
    new router per call (see the comment in ``query_routes.py``) so
    we get a clean lookup.
    """
    router = create_query_routes(spy, api_key=None, top_k=60)
    for route in router.routes:
        if getattr(route, "path", None) == "/query/stream":
            return route.endpoint
    raise AssertionError("/query/stream route not found on router")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _drain_streaming_response(response: Any) -> str:
    """Consume the entire streaming response body and return its text.

    Reading ``response.text`` forces the underlying httpx / Starlette
    streaming body to be exhausted, which in turn forces the FastAPI
    async generator's ``finally`` to run before this function returns.
    """
    return response.text


# ---------------------------------------------------------------------------
# Tests — release-on-success
# ---------------------------------------------------------------------------


class TestStreamingReleaseOnSuccess:
    """``/query/stream`` must release the workspace ref once when the
    stream completes normally.

    The release happens inside the async generator's ``finally`` (the
    streaming helper in ``query_routes.py`` wraps ``_build_stream_generator``
    in ``_generate()`` whose ``finally`` calls ``_release_once()``).
    """

    def test_streaming_release_on_success(self, spy_and_query_client) -> None:
        spy, client, _rag = spy_and_query_client

        resp = client.post(
            "/query/stream",
            headers={"LIGHTRAG-WORKSPACE": "stream-success"},
            json={"query": "hello world", "stream": True},
        )

        assert resp.status_code == 200
        # Drain the streaming body so the async generator's finally
        # runs before we assert on the release count.
        body = _drain_streaming_response(resp)
        # Sanity-check the body actually carried our streamed chunks.
        assert "hello" in body

        assert spy.acquire_count == 1
        assert spy.release_count == 1, (
            f"Expected exactly one release after a successful stream; "
            f"got {spy.release_count}."
        )
        assert spy.released_workspaces[0] == "stream-success"


# ---------------------------------------------------------------------------
# Tests — release-on-error
# ---------------------------------------------------------------------------


class TestStreamingReleaseOnError:
    """When the underlying ``aquery_llm`` raises before the
    :class:`StreamingResponse` is built, the workspace must still be
    released exactly once.

    The route's outer ``try / except Exception`` branch calls
    ``_release_once()`` so the ref doesn't leak when the LLM call fails.
    """

    def test_streaming_release_on_error(self, spy_and_query_client) -> None:
        spy, client, _rag = spy_and_query_client

        # Replace the rag mock so aquery_llm raises. We rebuild the spy
        # around the new mock because the route already wired the
        # previous mock into the StreamingResponse generator closure.
        error_mock = _build_rag_mock_streaming(aquery_error=RuntimeError("boom"))
        spy = SpyWorkspaceManager(error_mock)
        app = _build_query_app(spy)
        client = TestClient(app)

        resp = client.post(
            "/query/stream",
            headers={"LIGHTRAG-WORKSPACE": "stream-error"},
            json={"query": "hello world", "stream": True},
        )

        # Handler converts the exception into HTTP 500.
        assert resp.status_code == 500

        assert spy.acquire_count == 1, (
            "Acquire must still fire before the LLM call is attempted."
        )
        assert spy.release_count == 1, (
            f"Expected exactly one release after a streaming error; "
            f"got {spy.release_count}."
        )
        assert spy.released_workspaces[0] == "stream-error"


# ---------------------------------------------------------------------------
# Tests — no-double-release
# ---------------------------------------------------------------------------


class TestStreamingNoDoubleRelease:
    """Release must happen **at most once** even when both the streaming
    generator's ``finally`` and the outer ``try/finally`` could otherwise
    run.

    The handler uses a ``released`` flag inside ``_release_once()`` as the
    canonical idempotency guard. This test exercises a path where the
    inner generator catches a streaming error, yields the error line,
    then completes normally — driving the outer generator's ``finally``
    once and verifying no second release sneaks through.
    """

    def test_streaming_no_double_release(self, spy_and_query_client) -> None:
        # Plain ``Exception`` inside the inner iterator — caught by the
        # helper's ``except Exception``, yields an error line, then the
        # inner generator completes. The outer generator's ``finally``
        # runs once; ``_release_once``'s flag prevents a second release.
        inner_error = RuntimeError("mid-stream transient failure")

        rag_mock = _build_rag_mock_streaming(
            response_chunks=["hi", "there"],
        )
        # Inject the mid-stream error by wrapping the streaming response.
        # We replace the synthetic generator factory so the inner raises
        # after the configured chunks.
        rag_mock.aquery_llm = lambda *_a, **_kw: _async_return(
            {
                "llm_response": {
                    "is_streaming": True,
                    "response_iterator": _raise_after_first_chunk(
                        inner_error, ["hi", "there"]
                    ),
                },
                "data": {"references": [], "chunks": []},
            }
        )

        spy = SpyWorkspaceManager(rag_mock)
        app = _build_query_app(spy)
        client = TestClient(app)

        resp = client.post(
            "/query/stream",
            headers={"LIGHTRAG-WORKSPACE": "stream-no-double"},
            json={"query": "hello world", "stream": True},
        )

        assert resp.status_code == 200
        body = _drain_streaming_response(resp)

        # The helper should have caught the inner exception and yielded
        # an error line as the last NDJSON record.
        assert '"error"' in body

        assert spy.release_count == 1, (
            f"Expected exactly one release after an inner-generator "
            f"exception; got {spy.release_count}. The released flag "
            f"inside _release_once must prevent a second release."
        )


async def _async_return(value: Any) -> Any:
    """Tiny async-return helper for ad-hoc rag lambdas."""
    return value


# ---------------------------------------------------------------------------
# Tests — mid-stream disconnect
# ---------------------------------------------------------------------------


class TestStreamingReleaseOnMidstreamDisconnect:
    """If the client stops reading the streaming response partway through,
    the generator's ``finally`` must still fire and release the workspace.

    Starlette delivers an ``http.disconnect`` ASGI message when the HTTP
    client closes the connection, which causes Starlette's
    :class:`StreamingResponse` to call ``aclose()`` on the body iterator.
    We exercise the same code path by invoking the route handler
    directly, reading one chunk, and then closing the iterator — that is
    the literal sequence Starlette performs on disconnect.
    """

    def test_streaming_release_on_midstream_disconnect(self) -> None:
        # Configure the iterator to yield one chunk then block forever
        # (mirroring a slow LLM that never finishes). We never observe
        # the second chunk because we close the iterator mid-iteration.
        hang_event = asyncio.Event()

        async def _iter() -> AsyncIterator[str]:
            yield "first"
            # Park until the test closes the iterator. The hang models
            # a long-running LLM token generation.
            await hang_event.wait()
            yield "should-never-be-seen"  # pragma: no cover

        rag_mock = _RagIterator(_iter)
        spy = SpyWorkspaceManager(rag_mock)
        endpoint = _find_streaming_endpoint(spy)

        async def _scenario() -> None:
            streaming_resp = await _invoke_streaming_endpoint(
                endpoint, rag_mock, "stream-midstream"
            )
            body_iter = streaming_resp.body_iterator

            # Drive the iterator past the references line and the
            # first response chunk, then park on hang_event.wait()
            # (the third __anext__ call). Two chunks is enough to
            # prove the streaming generator has actually started.
            _ = await body_iter.__anext__()  # references line
            first = await body_iter.__anext__()  # first response line
            assert "first" in (first if isinstance(first, str) else first.decode())

            # Closing the body iterator is what Starlette does on ASGI
            # disconnect. It must trigger the outer _generate()'s
            # finally, which calls _release_once().
            await body_iter.aclose()

        try:
            asyncio.run(_scenario())
        finally:
            # Unblock any remaining generator task so it can wind down
            # cleanly (defensive: prevents "coroutine was never awaited"
            # warnings if Starlette scheduled cleanup tasks that are
            # still parked on hang_event).
            hang_event.set()

        assert spy.acquire_count == 1
        assert spy.release_count == 1, (
            f"Expected exactly one release after a midstream disconnect; "
            f"got {spy.release_count}. The generator's finally must "
            f"fire when Starlette closes the body iterator."
        )
        assert spy.released_workspaces[0] == "stream-midstream"


class _RagIterator:
    """Minimal rag-shaped mock that streams from a custom iterator factory."""

    def __init__(self, iterator_factory) -> None:
        self._iterator_factory = iterator_factory

    async def aquery_llm(self, *_a: Any, **_kw: Any) -> dict[str, Any]:
        return {
            "llm_response": {
                "is_streaming": True,
                "response_iterator": self._iterator_factory(),
            },
            "data": {"references": [], "chunks": []},
        }


# ---------------------------------------------------------------------------
# Tests — ASGI cancellation
# ---------------------------------------------------------------------------


class TestStreamingReleaseOnAsgiCancellation:
    """``asyncio.CancelledError`` raised inside the generator must still
    trigger a release.

    The helper ``_build_stream_generator`` catches ``Exception`` (not
    ``BaseException``), so ``asyncio.CancelledError`` propagates out of
    the inner generator. The outer ``_generate()`` wraps it in a
    ``try / finally`` whose ``finally`` calls ``_release_once()`` — that
    is the line of defense that prevents a leak on ASGI cancellation.

    The synthetic iterator raises ``CancelledError`` from its second
    ``__anext__`` to model what Starlette does on ``http.disconnect``:
    it raises ``CancelledError`` into the body iterator, which propagates
    through both the inner generator's ``except Exception`` (skipped
    because CancelledError is a BaseException) and the outer
    ``_generate()``'s ``try/finally`` (caught only in the finally).
    """

    def test_streaming_release_on_asgi_cancellation(self) -> None:
        # Configure a response_iterator that yields one chunk then
        # raises CancelledError on the next iteration.
        rag_mock = _build_rag_mock_streaming(
            response_chunks=["first"],
            response_raiser=asyncio.CancelledError(),
        )
        spy = SpyWorkspaceManager(rag_mock)
        endpoint = _find_streaming_endpoint(spy)

        async def _scenario() -> None:
            streaming_resp = await _invoke_streaming_endpoint(
                endpoint, rag_mock, "stream-cancel"
            )
            body_iter = streaming_resp.body_iterator

            # Walk the iterator past the references line and the
            # single response chunk ("first"). The next __anext__
            # raises CancelledError from the response_stream's second
            # iteration — that exception propagates through the inner
            # generator's except-Exception (skipped because
            # CancelledError is a BaseException) into the outer
            # _generate()'s try/finally, which fires _release_once().
            _ = await body_iter.__anext__()  # references line
            first = await body_iter.__anext__()
            assert "first" in (first if isinstance(first, str) else first.decode())

            # The third __anext__ raises CancelledError. We catch it
            # at the test boundary so asyncio.run() doesn't trip.
            with pytest.raises(asyncio.CancelledError):
                await body_iter.__anext__()

        # The CancelledError that escapes _generate() propagates out
        # of the iterator. It does not propagate out of the route
        # handler (which has already returned the StreamingResponse by
        # the time the generator runs), so aclose() does not raise it
        # back into the test — only the release matters.
        asyncio.run(_scenario())

        assert spy.acquire_count == 1
        assert spy.release_count == 1, (
            f"Expected exactly one release after CancelledError inside "
            f"the generator; got {spy.release_count}. The except "
            f"BaseException branch + generator finally must cooperate "
            f"with the released flag."
        )
        assert spy.released_workspaces[0] == "stream-cancel"


# ---------------------------------------------------------------------------
# Tests — workspace header routing
# ---------------------------------------------------------------------------


class TestStreamingWorkspaceHeaderRouted:
    """The ``LIGHTRAG-WORKSPACE`` header on a streaming request must reach
    ``workspace_mgr.acquire()`` before the stream begins, so the stream
    is served from the correct tenant's :class:`LightRAG` instance.

    This test complements ``test_workspace_routing.py::test_streaming_workspace_routing``
    by being part of the dedicated streaming-release-safety pack and
    pinning down the routing assertion alongside the release counts.
    """

    def test_streaming_workspace_header_routed(self, spy_and_query_client) -> None:
        spy, client, _rag = spy_and_query_client

        resp = client.post(
            "/query/stream",
            headers={"LIGHTRAG-WORKSPACE": "ws-a"},
            json={"query": "hello world", "stream": True},
        )

        assert resp.status_code == 200
        _ = _drain_streaming_response(resp)

        assert spy.acquired_workspaces == ["ws-a"], (
            "Streaming endpoint must acquire the workspace derived from "
            "the LIGHTRAG-WORKSPACE header."
        )
