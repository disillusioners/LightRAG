"""Regression tests for ``register_role_llm_builder`` integration.

Workspace isolation v2 relocated ``register_role_llm_builder`` from
``lightrag.api.lightrag_server`` to ``lightrag.api.llm_factory``. The
:func:`WorkspaceManager._create_rag_instance` builder calls it on every
new ``LightRAG`` instance it constructs, so role-specific LLM functions
(``role_llm_funcs``) and the runtime role builder callback
(``_llm_role_builder``) are populated for every workspace — including
default-workspace preloading, brand-new workspaces, and recreated ones
after an eviction.

These tests assert that contract end-to-end. The tests build real
:class:`LightRAG` instances with mocked LLM/embedding/tokenizer, but
patch :meth:`LightRAG.initialize_storages` and
:meth:`LightRAG.check_and_migrate_data` to skip the heavy storage
initialization that would otherwise touch the filesystem. The
register_role_llm_builder side effect itself is exercised through
:meth:`WorkspaceManager._create_rag_instance` (driven either by
``initialize()`` for the default workspace or by ``acquire()`` for
newly-requested workspaces).

Tests are offline — no external services, no LLM API calls.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest

from lightrag import ROLES
from lightrag.api.workspace_manager import WorkspaceManager
from lightrag.utils import EmbeddingFunc, Tokenizer

pytestmark = pytest.mark.offline


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _MockTokenizerImpl:
    """Minimal tokenizer used to satisfy LightRAG.__post_init__ validation."""

    def encode(self, content: str) -> list[int]:
        return [ord(ch) for ch in content]

    def decode(self, tokens: list[int]) -> str:
        return "".join(chr(t) for t in tokens)


def _build_args(default_workspace: str = "") -> SimpleNamespace:
    """Build a minimal ``args`` namespace accepted by ``WorkspaceManager``.

    Only ``workspace`` (the default-workspace name) is read by the manager
    constructor; the rest are fetched lazily via ``getattr(args, ..., None)``
    inside ``_create_rag_instance`` and so do not need to be present here.
    """
    return SimpleNamespace(workspace=default_workspace)


async def _mock_llm_func(prompt: str, **kwargs) -> str:
    """Stand-in for a real LLM callable. Always returns a tiny string."""
    return "ok"


async def _mock_embed(texts: list[str]) -> np.ndarray:
    """Stand-in for a real embedding callable. Returns zero vectors."""
    return np.zeros((len(texts), 4))


def _build_manager_kwargs(default_workspace: str = "") -> dict:
    """Build the constructor kwargs the WorkspaceManager expects."""
    return {
        "args": _build_args(default_workspace=default_workspace),
        "embedding_func": EmbeddingFunc(
            embedding_dim=4,
            max_token_size=10,
            func=_mock_embed,
        ),
        "llm_model_func": _mock_llm_func,
        "llm_model_kwargs": {},
        "llm_timeout": 100,
        "embedding_timeout": 100,
        "rerank_model_func": None,
        "role_llm_configs": {},
        "ollama_server_infos": None,
        "max_instances": 4,
    }


def _install_real_create_rag_instance(mgr: WorkspaceManager, working_dir: str):
    """Replace ``_create_rag_instance`` with a wrapper that uses real
    LightRAG construction + register_role_llm_builder, but stubs out
    storage initialization so the test stays hermetic.

    Returns a call-log dict so tests can inspect what was built.
    """
    log: dict = {"calls": []}

    async def wrapped_create(workspace: str):
        log["calls"].append(workspace)
        # Use a per-workspace working dir so each instance is file-isolated.
        ws_dir = f"{working_dir.rstrip('/')}/{workspace}"
        from lightrag import LightRAG  # local import keeps module load light
        from lightrag.api.llm_factory import register_role_llm_builder

        rag = LightRAG(
            working_dir=ws_dir,
            workspace=workspace,
            llm_model_func=mgr.llm_model_func,
            embedding_func=mgr.embedding_func,
            tokenizer=Tokenizer("mock", _MockTokenizerImpl()),
            llm_model_kwargs=mgr.llm_model_kwargs,
            default_llm_timeout=mgr.llm_timeout,
            default_embedding_timeout=mgr.embedding_timeout,
            role_llm_configs=mgr.role_llm_configs,
        )
        # This is the side effect we want to assert.
        register_role_llm_builder(rag, mgr.args, mgr.llm_timeout)

        # Skip heavy storage init — stubs both async methods on the
        # *instance* so any later call returns immediately.
        rag.initialize_storages = AsyncMock(return_value=None)  # type: ignore[method-assign]
        rag.check_and_migrate_data = AsyncMock(return_value=None)  # type: ignore[method-assign]
        return rag

    mgr._create_rag_instance = wrapped_create  # type: ignore[method-assign]
    return log


def _expected_role_names() -> set[str]:
    """Roles that must show up in role_llm_funcs / role_llm_kwargs."""
    return {spec.name for spec in ROLES}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def manager(tmp_path):
    """A WorkspaceManager wired up so ``_create_rag_instance`` builds real
    LightRAG instances (with register_role_llm_builder applied) but skips
    storage init. The manager is already initialized; tests can drive
    ``acquire`` / ``release`` to exercise additional builds.
    """
    mgr = WorkspaceManager(**_build_manager_kwargs(default_workspace="default-ws"))
    log = _install_real_create_rag_instance(mgr, str(tmp_path))
    await mgr.initialize()
    yield mgr, log
    # Cleanup: drop any held references and tear down remaining instances.
    await mgr.finalize()


# ---------------------------------------------------------------------------
# 1. Default workspace has role_llm_funcs populated
# ---------------------------------------------------------------------------


async def test_default_workspace_has_role_llm_builder(manager):
    """The default-workspace instance created during ``initialize()`` has
    ``role_llm_funcs`` populated for every role in :data:`ROLES`.
    """
    mgr, log = manager
    rag = await mgr.get_default_instance()

    actual_roles = set(rag.role_llm_funcs.keys())
    expected_roles = _expected_role_names()
    assert actual_roles == expected_roles, (
        f"role_llm_funcs missing roles {expected_roles - actual_roles} on "
        f"the default workspace"
    )

    # And ``_llm_role_builder`` was set by register_role_llm_builder.
    assert rag._llm_role_builder is not None, (
        "_llm_role_builder must be set after register_role_llm_builder runs"
    )
    assert callable(rag._llm_role_builder)

    # Initialize() must have built exactly one instance (the default workspace).
    assert log["calls"] == ["default-ws"]


# ---------------------------------------------------------------------------
# 2. New workspace has role_llm_funcs populated
# ---------------------------------------------------------------------------


async def test_new_workspace_has_role_llm_builder(manager):
    """A brand-new workspace created via ``acquire()`` also has
    ``role_llm_funcs`` populated for every role in :data:`ROLES`.
    """
    mgr, log = manager
    rag = await mgr.acquire("test-ws")
    try:
        actual_roles = set(rag.role_llm_funcs.keys())
        expected_roles = _expected_role_names()
        assert actual_roles == expected_roles, (
            f"role_llm_funcs missing roles {expected_roles - actual_roles} "
            f"on a newly-acquired workspace"
        )
        assert rag._llm_role_builder is not None
        assert callable(rag._llm_role_builder)
    finally:
        await mgr.release("test-ws")

    # Two _create_rag_instance calls total: initialize() for the default
    # plus one for the new workspace.
    assert log["calls"] == ["default-ws", "test-ws"]


# ---------------------------------------------------------------------------
# 3. role_llm_funcs["query"] is callable on a non-default workspace
# ---------------------------------------------------------------------------


async def test_role_llm_funcs_query_works(manager):
    """Query role func on a freshly-acquired workspace must be populated
    (not ``None``) and callable. This guards against a regression where a
    per-workspace instance would lack the role func and silently fall back
    to the base LLM.
    """
    mgr, _log = manager
    rag = await mgr.acquire("query-ws")
    try:
        query_func = rag.role_llm_funcs.get("query")
        assert query_func is not None, (
            "role_llm_funcs['query'] must not be None on a non-default workspace"
        )
        assert callable(query_func), (
            "role_llm_funcs['query'] must be callable on a non-default workspace"
        )
        # And it is not None for the other roles either.
        for role in _expected_role_names():
            assert role in rag.role_llm_funcs, f"role {role!r} missing"
            assert rag.role_llm_funcs[role] is not None, (
                f"role_llm_funcs[{role!r}] is unexpectedly None"
            )
    finally:
        await mgr.release("query-ws")


# ---------------------------------------------------------------------------
# 4. role_llm_kwargs populated for all roles
# ---------------------------------------------------------------------------


async def test_role_llm_kwargs_populated(manager):
    """``role_llm_kwargs`` returns one entry per role on a new instance.
    The value may be ``None`` (meaning "inherit base LLM kwargs") — what
    matters is that every role from :data:`ROLES` has an entry in the
    mapping.
    """
    mgr, _log = manager
    rag = await mgr.acquire("kwargs-ws")
    try:
        kwargs_map = rag.role_llm_kwargs
        actual = set(kwargs_map.keys())
        expected = _expected_role_names()
        assert actual == expected, f"role_llm_kwargs missing roles {expected - actual}"
        # Each entry must exist (value is dict | None by contract).
        for role in expected:
            assert role in kwargs_map
            assert kwargs_map[role] is None or isinstance(kwargs_map[role], dict), (
                f"role_llm_kwargs[{role!r}] must be dict or None, got "
                f"{type(kwargs_map[role]).__name__}"
            )
    finally:
        await mgr.release("kwargs-ws")


# ---------------------------------------------------------------------------
# 5. Evicted-then-recreated instance has role_llm_funcs populated
# ---------------------------------------------------------------------------


async def test_evicted_then_recreated_has_builder(manager):
    """When a workspace is evicted from the LRU cache and re-acquired,
    :meth:`WorkspaceManager._create_rag_instance` builds a fresh instance.
    That fresh instance must also have ``role_llm_funcs`` populated and
    ``_llm_role_builder`` set — i.e. the contract holds for the
    "second time" path, not only the first.
    """
    mgr, log = manager

    # Drive the manager directly so we can compare first and second builds
    # cleanly without juggling the full cache-eviction state machine.
    first_rag = await mgr._create_rag_instance("evict-ws")
    second_rag = await mgr._create_rag_instance("evict-ws")

    # Different objects (different builder callbacks attached).
    assert first_rag is not second_rag, (
        "_create_rag_instance must return a fresh object"
    )

    # Both have role_llm_funcs populated for every role.
    expected = _expected_role_names()
    assert set(first_rag.role_llm_funcs.keys()) == expected
    assert set(second_rag.role_llm_funcs.keys()) == expected

    # Both have _llm_role_builder set.
    assert first_rag._llm_role_builder is not None
    assert second_rag._llm_role_builder is not None
    assert callable(first_rag._llm_role_builder)
    assert callable(second_rag._llm_role_builder)
    # Each instance has its own builder — not a shared cached closure.
    assert first_rag._llm_role_builder is not second_rag._llm_role_builder

    # And each one wraps the same roles correctly.
    for rag, label in ((first_rag, "first"), (second_rag, "second")):
        for role in expected:
            assert rag.role_llm_funcs[role] is not None, (
                f"{label}-build role_llm_funcs[{role!r}] unexpectedly None"
            )
            assert callable(rag.role_llm_funcs[role]), (
                f"{label}-build role_llm_funcs[{role!r}] not callable"
            )

    # Three calls total: initialize's default-ws + the two explicit builds.
    assert log["calls"] == ["default-ws", "evict-ws", "evict-ws"]
