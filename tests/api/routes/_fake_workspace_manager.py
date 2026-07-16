"""Test stand-in for ``lightrag.api.workspace_manager.WorkspaceManager``.

The workspace-isolation-v2 refactor moved per-request routing through a
:class:`WorkspaceManager` whose ``acquire``/``release`` calls hand out a
:class:`LightRAG` for the duration of one HTTP request. Route factories
(``create_document_routes``, ``create_graph_routes``, ``OllamaAPI``)
now take a ``workspace_mgr`` as their first argument.

These tests predate the refactor and were authored with the prior
``create_*_routes(rag, ...)`` shape. They provide a mock :class:`LightRAG`
stand-in but no :class:`WorkspaceManager`, so the new endpoints blow up
at ``workspace_mgr.acquire(workspace)`` with ``AttributeError``.

This module provides a minimal ``FakeWorkspaceManager`` that:

- returns a pre-set mock rag from ``acquire()`` so endpoints see the
  same object the tests used to build by hand,
- no-ops ``release()`` so the per-request try/finally cleanup doesn't
  crash,
- exposes ``get_default_workspace()`` (sync) and
  ``get_default_instance()`` / ``get_registry()`` (async) for the
  Ollama-compat API and the auto-register-on-upload path,
- carries a tiny ``_FakeRegistry`` whose ``register()`` is a no-op so
  the post-enqueue ``registry.register(workspace)`` call is harmless.

Keep this stub minimal — only implement what the routers under test
actually call. New methods should be added when (and only when) a real
test exercises them, so the fake stays an honest proxy for production.
"""

from __future__ import annotations

from typing import Any, Optional


class _FakeRegistry:
    """Minimal stand-in for ``WorkspaceRegistry`` used by the fake
    workspace manager.

    The route handlers call ``await registry.register(workspace)`` after a
    successful enqueue so the workspace shows up in ``GET /workspaces``.
    Tests don't read that listing, so ``register`` is a no-op.
    """

    async def register(self, workspace: Optional[str]) -> None:
        return None


class FakeWorkspaceManager:
    """Minimal stand-in for :class:`WorkspaceManager` for unit tests.

    Args:
        rag: The mock :class:`LightRAG` instance the existing tests pass
            directly to ``create_*_routes(rag, ...)``. ``acquire`` returns
            this object unchanged so the downstream endpoint sees the
            same ``rag`` it was getting before the refactor.
    """

    def __init__(self, rag: Any) -> None:
        self._rag = rag
        self._registry = _FakeRegistry()

    async def acquire(self, workspace: Optional[str] = None) -> Any:
        """Return the pre-set mock rag, ignoring the workspace name."""
        return self._rag

    async def release(self, workspace: Optional[str] = None) -> None:
        """No-op — the fake does not refcount or cache."""
        return None

    def get_default_workspace(self) -> str:
        """Return the canonical default workspace name (empty string)."""
        return ""

    async def get_default_instance(self) -> Any:
        """Return the pre-set mock rag (used by Ollama-compat endpoints)."""
        return self._rag

    async def get_registry(self) -> _FakeRegistry:
        """Return a no-op registry (used by the post-enqueue auto-register)."""
        return self._registry
