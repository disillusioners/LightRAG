"""Unit tests for ``lightrag.api.workspace_manager.WorkspaceManager``.

The WorkspaceManager is the multi-tenant LRU cache of ``LightRAG`` instances
that fronts every request handler in the API server. Building a real
``LightRAG`` instance for unit testing is out of scope (it requires real
storage backends and an LLM/embedding function), so these tests patch
:meth:`WorkspaceManager._create_rag_instance` to return ``AsyncMock`` doubles
that look enough like a ``LightRAG`` to satisfy the manager's contract
(``finalize_storages()`` is the only method the manager awaits on a cached
instance).

Behavior under test:

* Identity-preserving cache hit (``acquire`` returns the same instance on
  repeat ``acquire``).
* LRU eviction, including the requirement that an in-use instance
  (``refcount > 0``) cannot be evicted.
* Refcount lifecycle: every ``acquire`` bumps, every ``release`` decrements;
  release is lazy (no immediate eviction); over-releases are clamped.
* Concurrency: ``_cache_lock`` serializes acquires, ``_init_lock`` serializes
  instance creation, so two concurrent acquires for the same cold workspace
  yield a single instance, and two concurrent acquires for different cold
  workspaces do not deadlock.
* Cache-full error path: when every cached instance has ``refcount > 0``,
  ``acquire`` raises :class:`WorkspaceCacheFullError`; once any refcount
  drops to zero, the next acquire evicts the LRU candidate and proceeds.
* Default-workspace preloading via :meth:`WorkspaceManager.initialize`.
* Auto-registration of new workspaces in the underlying
  :class:`WorkspaceRegistry`.

All tests are offline — no real storage, no LLM, no network.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lightrag.api.workspace_manager import (
    WorkspaceCacheFullError,
    WorkspaceManager,
)

pytestmark = pytest.mark.offline


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _build_args(workspace: str = "") -> SimpleNamespace:
    """Build a minimal ``args`` namespace the manager constructor accepts.

    The manager reads ``args.workspace`` to learn the default workspace name
    and then forwards ``getattr(args, "<name>", None)`` for everything else
    inside :meth:`_create_rag_instance`. Because we override that method,
    the other attributes are never consulted; an empty namespace is enough.
    """
    return SimpleNamespace(workspace=workspace)


def _build_deps() -> dict:
    """Return the constructor kwargs the manager stores verbatim.

    None of these values are touched by the mocked ``_create_rag_instance``
    — they exist only so the ``WorkspaceManager.__init__`` body accepts the
    arguments without raising.
    """
    return {
        "args": _build_args(workspace=""),
        "embedding_func": None,
        "llm_model_func": None,
        "llm_model_kwargs": {},
        "llm_timeout": 30,
        "embedding_timeout": 30,
        "rerank_model_func": None,
        "role_llm_configs": {},
        "ollama_server_infos": None,
        "max_instances": 3,
    }


class MockRagFactory:
    """Factory + bookkeeping for ``AsyncMock`` ``LightRAG`` doubles.

    Each call to :meth:`create` returns a *fresh* mock whose
    ``finalize_storages`` is an ``AsyncMock`` the test can assert on. The
    factory also exposes a per-instance ``finalize`` callable so eviction
    tests can verify the right instance was finalized.
    """

    def __init__(self) -> None:
        self.instances: list[AsyncMock] = []
        self.call_count = 0

    def create(self, workspace: str = "") -> AsyncMock:
        """Build a fresh ``AsyncMock`` and record it.

        The ``workspace`` parameter is accepted because
        :meth:`WorkspaceManager._create_rag_instance` is awaited with the
        workspace name as its sole positional argument. The factory does
        not need the value — the test's per-workspace bookkeeping goes
        through the cache and refcount dicts — but the parameter MUST be
        present or ``AsyncMock(side_effect=...)`` will refuse to invoke us.
        """
        self.call_count += 1
        mock = AsyncMock(name=f"LightRAGMock#{self.call_count}")
        # finalize_storages is the only attribute the manager awaits on a
        # cached instance. Make it explicit so test assertions read clearly.
        mock.finalize_storages = AsyncMock(return_value=None)
        self.instances.append(mock)
        return mock

    def find(self, rag: AsyncMock) -> AsyncMock | None:
        """Return the recorded instance that is identity-equal to ``rag``."""
        for inst in self.instances:
            if inst is rag:
                return inst
        return None


@pytest.fixture
def mock_factory() -> MockRagFactory:
    """Fresh mock factory per test for easy bookkeeping."""
    return MockRagFactory()


@pytest.fixture
def manager_args():
    """Return a builder for ``WorkspaceManager`` constructor kwargs.

    Tests can override ``max_instances`` or ``args.workspace`` without
    mutating the shared defaults.
    """
    return _build_deps


@pytest.fixture
async def manager(mock_factory, manager_args):
    """A fully-initialized WorkspaceManager with a patched instance factory.

    The fixture constructs the manager, monkeypatches ``_create_rag_instance``
    to call :attr:`mock_factory.create`, and runs ``initialize()`` so the
    default-workspace instance is eagerly preloaded. Each test gets a fresh
    manager so cached state from one test cannot bleed into another.
    """
    kwargs = manager_args()
    mgr = WorkspaceManager(**kwargs)
    # Replace _create_rag_instance with a thin async shim that hands back a
    # fresh mock per call. This bypasses real LightRAG construction AND the
    # register_role_llm_builder side effect inside the real implementation.
    mgr._create_rag_instance = AsyncMock(side_effect=mock_factory.create)
    await mgr.initialize()
    return mgr


# ---------------------------------------------------------------------------
# 1. Identity on cache hit
# ---------------------------------------------------------------------------


class TestAcquireIdentity:
    async def test_get_or_create_returns_instance(self, manager):
        first = await manager.acquire("ws-a")
        await manager.release("ws-a")
        second = await manager.acquire("ws-a")
        await manager.release("ws-a")

        assert first is second, "Second acquire must return the cached instance"
        # Total _create_rag_instance calls = 1 (default workspace in
        # initialize()) + 1 (first acquire of "ws-a"); the second acquire
        # of "ws-a" is a cache hit and must NOT trigger another build.
        assert manager._create_rag_instance.call_count == 2  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# 2. LRU eviction
# ---------------------------------------------------------------------------


class TestLruEviction:
    async def test_lru_eviction(self, manager, mock_factory):
        # Fill the cache to capacity. Acquire+release each so refcount=0
        # and the next acquire-miss can evict.
        await manager.acquire("ws-a")
        await manager.release("ws-a")
        await manager.acquire("ws-b")
        await manager.release("ws-b")
        await manager.acquire("ws-c")
        await manager.release("ws-c")

        assert set(manager._cache.keys()) == {"ws-a", "ws-b", "ws-c"}
        # ws-a is the oldest (first in OrderedDict).
        rag_a = manager._cache["ws-a"]
        assert rag_a.finalize_storages.call_count == 0  # type: ignore[attr-defined]

        # Acquire a new workspace — triggers eviction of the LRU candidate.
        await manager.acquire("ws-d")
        await manager.release("ws-d")

        assert "ws-a" not in manager._cache, "Oldest entry must be evicted"
        assert "ws-d" in manager._cache
        # The evicted instance's finalize_storages must have been awaited.
        assert rag_a.finalize_storages.call_count == 1  # type: ignore[attr-defined]
        # Re-acquiring ws-a yields a *fresh* instance (different identity).
        rag_a_again = await manager.acquire("ws-a")
        await manager.release("ws-a")
        assert rag_a_again is not rag_a


# ---------------------------------------------------------------------------
# 3. Refcount prevents eviction
# ---------------------------------------------------------------------------


class TestRefcountProtectsFromEviction:
    async def test_refcount_prevents_eviction(self, manager):
        # ws-a stays held (refcount=1). ws-b and ws-c are released (refcount=0)
        # so they are eligible for eviction.
        await manager.acquire("ws-a")
        await manager.acquire("ws-b")
        await manager.release("ws-b")
        await manager.acquire("ws-c")
        await manager.release("ws-c")

        # Confirm the precondition: ws-a is the LRU candidate but still in use.
        assert next(iter(manager._cache)) == "ws-a"
        assert manager._refcounts["ws-a"] == 1

        # Acquire a new workspace. Eviction must skip ws-a (in use) and pick
        # the next refcount=0 candidate (ws-b, the oldest such).
        await manager.acquire("ws-d")
        await manager.release("ws-d")

        assert "ws-a" in manager._cache, "ws-a must NOT be evicted while in use"
        assert "ws-b" not in manager._cache, "ws-b should be the one evicted"


# ---------------------------------------------------------------------------
# 4. Acquire / release cycle
# ---------------------------------------------------------------------------


class TestAcquireReleaseCycle:
    async def test_acquire_release_cycle(self, manager):
        for _ in range(3):
            await manager.acquire("ws-a")
            await manager.release("ws-a")
        assert manager._refcounts["ws-a"] == 0

    async def test_release_triggers_no_immediate_eviction(self, manager):
        # Even after release drops refcount to 0, the entry remains in cache.
        await manager.acquire("ws-a")
        await manager.release("ws-a")
        assert manager._refcounts["ws-a"] == 0
        assert "ws-a" in manager._cache, (
            "release() is lazy; eviction happens on the next acquire-miss, "
            "not on release"
        )


# ---------------------------------------------------------------------------
# 5. Default workspace preloading
# ---------------------------------------------------------------------------


class TestInitialize:
    async def test_default_workspace_preloaded(self, mock_factory, manager_args):
        kwargs = manager_args()
        kwargs["args"] = _build_args(workspace="default-ws")
        mgr = WorkspaceManager(**kwargs)
        mgr._create_rag_instance = AsyncMock(side_effect=mock_factory.create)

        assert "default-ws" not in mgr._cache

        await mgr.initialize()

        assert "default-ws" in mgr._cache
        assert mgr._refcounts["default-ws"] == 0
        # initialize() must have built exactly one instance.
        assert mock_factory.call_count == 1


# ---------------------------------------------------------------------------
# 6. Auto-registration of new workspaces
# ---------------------------------------------------------------------------


class TestRegistryIntegration:
    async def test_auto_register_on_get_or_create(self, manager):
        # Default workspace is already registered by initialize(). Confirm.
        entries_before = await manager.list_workspaces()
        names_before = {e["name"] for e in entries_before}
        assert "" in names_before, "Default workspace must be registered"

        # Acquire a brand-new workspace.
        await manager.acquire("ws-x")
        await manager.release("ws-x")

        entries_after = await manager.list_workspaces()
        names_after = {e["name"] for e in entries_after}
        assert "ws-x" in names_after, "New workspace must be auto-registered on acquire"


# ---------------------------------------------------------------------------
# 7. Concurrent acquire of the same cold workspace
# ---------------------------------------------------------------------------


class TestConcurrency:
    async def test_concurrent_get_or_create(self, manager):
        # Two concurrent acquires for the SAME workspace. Because the
        # manager holds _cache_lock for the entire acquire body, the second
        # caller sees the cache hit and returns the same instance.
        rag1, rag2 = await asyncio.gather(
            manager.acquire("ws-shared"),
            manager.acquire("ws-shared"),
        )
        await manager.release("ws-shared")
        await manager.release("ws-shared")

        assert rag1 is rag2
        # Exactly one new instance was built for "ws-shared" beyond the
        # default-workspace preloading in initialize(). We measure the
        # delta around the gather to isolate the concurrent acquires from
        # the fixture's setup.
        before = manager._create_rag_instance.call_count  # type: ignore[union-attr]
        rag3, rag4 = await asyncio.gather(
            manager.acquire("ws-shared"),
            manager.acquire("ws-shared"),
        )
        await manager.release("ws-shared")
        await manager.release("ws-shared")
        after = manager._create_rag_instance.call_count  # type: ignore[union-attr]
        assert after - before == 0, (
            "Second concurrent pair is a cache hit; must NOT build a new instance."
        )
        assert rag3 is rag1 is rag2 is rag4


# ---------------------------------------------------------------------------
# 8. Background-task refcount independence
# ---------------------------------------------------------------------------


class TestBackgroundTaskRefcount:
    async def test_background_task_refcount_independence(self, manager):
        # Request path holds ws-a (refcount=1).
        await manager.acquire("ws-a")

        # Background task grabs its own reference (refcount=2).
        async def bg_hold():
            await manager.acquire("ws-a")
            try:
                # Simulate work; meanwhile the request handler releases.
                await asyncio.sleep(0.01)
            finally:
                await manager.release("ws-a")

        bg_task = asyncio.create_task(bg_hold())
        # Let the task enter acquire() so refcount is 2 before we release.
        await asyncio.sleep(0)
        assert manager._refcounts["ws-a"] == 2

        await manager.release("ws-a")
        assert manager._refcounts["ws-a"] == 1, (
            "Request release must drop refcount, not the bg task's hold"
        )

        await bg_task
        assert manager._refcounts["ws-a"] == 0
        # ws-a must still be in the cache — its refcount reached 0 only
        # after the bg task finished, so eviction is not triggered yet.
        assert "ws-a" in manager._cache


# ---------------------------------------------------------------------------
# 9. Eviction blocked by an active acquire (concurrent access)
# ---------------------------------------------------------------------------


class TestEvictionRaceWithActiveAcquire:
    async def test_lru_eviction_during_concurrent_access(self, manager):
        # 1) Fill cache: hold ws-a (refcount=1), acquire+release ws-b and
        #    ws-c so they have refcount=0 and are eviction-eligible.
        await manager.acquire("ws-a")  # refcount=1, oldest
        await manager.acquire("ws-b")
        await manager.release("ws-b")  # refcount=0
        await manager.acquire("ws-c")
        await manager.release("ws-c")  # refcount=0

        # 2) Spawn a holder task that bumps ws-a's refcount. Acquiring an
        #    already-cached workspace moves it to the MRU end, so after
        #    the holder runs, ws-a is the newest entry, not the oldest.
        release_event = asyncio.Event()

        async def hold_ws_a():
            await manager.acquire("ws-a")
            try:
                await release_event.wait()
            finally:
                await manager.release("ws-a")

        holder = asyncio.create_task(hold_ws_a())
        # Yield so the task reaches its acquire().
        await asyncio.sleep(0)
        assert manager._refcounts["ws-a"] == 2

        # 3) Acquire a new workspace while ws-a is held. ws-a (in use) must
        #    NOT be evicted; the LRU refcount=0 candidate (ws-b) gets evicted.
        await manager.acquire("ws-d")
        await manager.release("ws-d")
        assert "ws-a" in manager._cache, "ws-a is in use, must not be evicted"
        assert "ws-b" not in manager._cache, "ws-b should be the LRU victim"
        assert "ws-d" in manager._cache

        # 4) Release the holder. ws-a drops from refcount=2 to refcount=1
        #    (the main acquire still holds it).
        release_event.set()
        await holder
        assert manager._refcounts["ws-a"] == 1

        # 5) Drop main's hold on ws-a; refcount=0. Note ws-a is at the MRU
        #    end (the holder task's acquire moved it there on cache hit),
        #    so it is NOT the LRU candidate.
        await manager.release("ws-a")
        assert manager._refcounts["ws-a"] == 0
        assert next(iter(manager._cache)) == "ws-c", (
            "ws-c is the oldest refcount=0 candidate; ws-a was moved to MRU "
            "by the holder task's cache-hit acquire."
        )

        # 6) Acquiring a new workspace evicts ws-c (LRU refcount=0), not
        #    ws-a. Verifies eviction still respects LRU order after the
        #    holder task released.
        await manager.acquire("ws-e")
        await manager.release("ws-e")
        assert "ws-c" not in manager._cache
        assert "ws-a" in manager._cache
        assert "ws-e" in manager._cache


# ---------------------------------------------------------------------------
# 10. Evicted instance is finalized and replaced with a fresh one
# ---------------------------------------------------------------------------


class TestEvictedInstanceFinalized:
    async def test_evicted_instance_finalized(self, manager):
        await manager.acquire("ws-a")
        await manager.release("ws-a")
        rag_a = manager._cache["ws-a"]
        assert rag_a.finalize_storages.call_count == 0  # type: ignore[attr-defined]

        # Trigger eviction by filling the cache.
        await manager.acquire("ws-b")
        await manager.release("ws-b")
        await manager.acquire("ws-c")
        await manager.release("ws-c")
        await manager.acquire("ws-d")  # evicts ws-a
        await manager.release("ws-d")

        assert rag_a.finalize_storages.call_count == 1  # type: ignore[attr-defined]
        # Re-acquiring ws-a yields a NEW instance with different identity.
        rag_a_new = await manager.acquire("ws-a")
        await manager.release("ws-a")
        assert rag_a_new is not rag_a


# ---------------------------------------------------------------------------
# 11. Cache-full error path
# ---------------------------------------------------------------------------


class TestCacheFull:
    async def test_cache_full_returns_error(self, manager):
        # Hold every cached entry (refcount > 0) so nothing is evictable.
        # The default workspace was preloaded by initialize(); hold it too.
        await manager.acquire("")  # default workspace; refcount=1
        await manager.acquire("ws-b")
        await manager.acquire("ws-c")
        # Cache is full (3/3) with every refcount > 0.

        with pytest.raises(WorkspaceCacheFullError) as excinfo:
            await manager.acquire("ws-new")
        assert excinfo.value.max_instances == 3

    async def test_cache_full_recoverable_after_release(self, manager):
        # Fill and hold all entries.
        await manager.acquire("")
        await manager.acquire("ws-b")
        await manager.acquire("ws-c")

        # First acquire of a new workspace must fail.
        with pytest.raises(WorkspaceCacheFullError):
            await manager.acquire("ws-new")

        # Release one ref. The next acquire must succeed by evicting the
        # oldest refcount=0 candidate (the default workspace, oldest).
        await manager.release("")
        rag_new = await manager.acquire("ws-new")
        await manager.release("ws-new")
        assert rag_new is not None
        assert "" not in manager._cache
        assert "ws-new" in manager._cache


# ---------------------------------------------------------------------------
# 12. Concurrent cold-start for distinct workspaces must not deadlock
# ---------------------------------------------------------------------------


class TestConcurrentColdStartNoDeadlock:
    async def test_concurrent_cold_start_no_deadlock(self, manager):
        # Two concurrent acquires for DIFFERENT cold workspaces. The cache
        # is not full (initialize() preloaded only the default workspace),
        # so no eviction is needed. _init_lock serializes the body of
        # _create_rag_instance; _cache_lock serializes the whole acquire.
        # Both must succeed without deadlock.
        try:
            rag_x, rag_y = await asyncio.wait_for(
                asyncio.gather(
                    manager.acquire("ws-x"),
                    manager.acquire("ws-y"),
                ),
                timeout=5.0,
            )
        except asyncio.TimeoutError as exc:  # pragma: no cover - failure path
            pytest.fail(f"Concurrent cold-start deadlocked: {exc!r}")

        await manager.release("ws-x")
        await manager.release("ws-y")

        assert rag_x is not rag_y
        assert "ws-x" in manager._cache
        assert "ws-y" in manager._cache
