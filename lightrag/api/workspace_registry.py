"""
Workspace registry for tracking known workspaces.

Persistence is optional for v2 (W1) — this module uses in-memory tracking via
:class:`collections.OrderedDict`. The public interface is intentionally stable
so that a persistent backend (file-based, Redis, database, etc.) can be added
later without changing call sites.

A single :class:`WorkspaceRegistry` instance is shared by the API server and
guards all mutations with an :class:`asyncio.Lock` so concurrent requests do
not race on registration or list traversal.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Optional

from lightrag.utils import logger


class WorkspaceRegistry:
    """Thread-safe, in-memory registry of known workspaces.

    The registry stores an entry per workspace name. Insertion order is
    preserved via :class:`collections.OrderedDict` so listing yields a
    deterministic, oldest-first view. Methods are all ``async`` and guarded
    by a single :class:`asyncio.Lock` so concurrent registration and lookup
    are safe within a single event loop.

    Attributes:
        default_workspace: The canonical name of the default workspace.
            ``None`` / empty strings passed to other methods are normalized
            to this value.
        _workspaces: Ordered mapping ``workspace_name -> {"name": str,
            "first_seen": str, "last_seen": str}``. ``first_seen`` is the
            immutable ISO 8601 timestamp of the workspace's first
            registration; ``last_seen`` is updated on every subsequent
            registration call.
        _lock: Async lock that serializes mutations.
    """

    def __init__(self, default_workspace: str = "") -> None:
        """Initialize the registry and register the default workspace.

        Args:
            default_workspace: Canonical name for the default workspace.
                Empty string is treated as the default.
        """
        self.default_workspace: str = default_workspace or ""
        self._workspaces: "OrderedDict[str, dict]" = OrderedDict()
        self._lock: asyncio.Lock = asyncio.Lock()

        # Seed with the default workspace so it always exists in listings.
        _now = datetime.now(timezone.utc).isoformat()
        self._workspaces[self.default_workspace] = {
            "name": self.default_workspace,
            "first_seen": _now,
            "last_seen": _now,
        }

    @staticmethod
    def _normalize(workspace: Optional[str], default: str) -> str:
        """Normalize a workspace name to its canonical form.

        ``None`` and empty / whitespace-only strings collapse to ``default``.
        """
        if workspace is None:
            return default
        normalized = workspace.strip()
        return normalized if normalized else default

    async def register(self, workspace: str) -> None:
        """Register a workspace (or refresh a known one).

        ``None`` or empty values are normalized to the default workspace.

        Semantics:
            * If the workspace is NEW, it is inserted with both ``first_seen``
              and ``last_seen`` set to the current timestamp (identical
              values at creation).
            * If the workspace ALREADY EXISTS, ``first_seen`` is preserved
              (immutable) and ``last_seen`` is bumped to the current
              timestamp. Re-registering is therefore idempotent for the
              registration status but updates ``last_seen``.

        Args:
            workspace: Name of the workspace to register. ``None`` or empty
                values map to the default workspace.
        """
        name = self._normalize(workspace, self.default_workspace)
        async with self._lock:
            now = datetime.now(timezone.utc).isoformat()
            if name in self._workspaces:
                self._workspaces[name]["last_seen"] = now
                logger.debug(
                    "Workspace %r already registered; preserving first_seen=%s, updated last_seen=%s",
                    name,
                    self._workspaces[name]["first_seen"],
                    now,
                )
                return
            self._workspaces[name] = {
                "name": name,
                "first_seen": now,
                "last_seen": now,
            }
            logger.info("Registered workspace %r", name)

    async def list_workspaces(self) -> list[dict]:
        """Return a snapshot of registered workspaces in insertion order.

        Each entry is a dict with ``name``, ``first_seen`` (ISO 8601
        timestamp of the workspace's first registration, immutable),
        ``last_seen`` (ISO 8601 timestamp of the most recent registration
        call), and ``document_count`` (``None`` for v2 — computing it is
        best-effort and expensive, deferred to a later workstream).

        Returns:
            List of workspace descriptors in registration order.
        """
        async with self._lock:
            return [
                {
                    "name": entry["name"],
                    "first_seen": entry["first_seen"],
                    "last_seen": entry["last_seen"],
                    "document_count": None,
                }
                for entry in self._workspaces.values()
            ]

    async def exists(self, workspace: str) -> bool:
        """Return whether a workspace is currently registered.

        ``None`` or empty values are normalized to the default workspace.

        Args:
            workspace: Name of the workspace to look up.

        Returns:
            ``True`` if the (normalized) workspace is registered, else
            ``False``.
        """
        name = self._normalize(workspace, self.default_workspace)
        async with self._lock:
            return name in self._workspaces
