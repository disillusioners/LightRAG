"""Unit tests for ``lightrag.api.workspace_registry.WorkspaceRegistry``.

The registry is an in-memory, async-safe store of workspace descriptors keyed
by name. Each descriptor carries ``name``, ``first_seen`` (immutable), and
``last_seen`` (refreshed on every re-registration). ``document_count`` is
exposed in the public listing but always ``None`` for v2 — computing it is
deferred to a later workstream (see the registry docstring).

Persistence is intentionally out of scope (W1: in-memory only), so this
module does not exercise cross-instance or cross-process behaviour.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from lightrag.api.workspace_registry import WorkspaceRegistry

pytestmark = pytest.mark.offline


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_iso(ts: str) -> datetime:
    """Parse an ISO 8601 timestamp and return a ``datetime`` (no TZ required).

    The registry emits ``datetime.now(timezone.utc).isoformat()`` which is a
    fully-qualified ISO 8601 string. ``datetime.fromisoformat`` accepts both
    naive and aware forms on Python 3.11+, so a single call is sufficient.
    """
    return datetime.fromisoformat(ts)


def _by_name(entries: list[dict], name: str) -> dict:
    """Return the registry entry whose ``name`` matches, or fail the test."""
    for entry in entries:
        if entry["name"] == name:
            return entry
    raise AssertionError(f"Workspace {name!r} not in registry: {entries!r}")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRegisterNewWorkspace:
    """A brand-new workspace appears in ``list_workspaces`` with first/last
    timestamps equal at the moment of first registration."""

    async def test_register_new_workspace(self) -> None:
        registry = WorkspaceRegistry()

        await registry.register("alpha")

        entries = await registry.list_workspaces()
        alpha = _by_name(entries, "alpha")

        assert alpha["name"] == "alpha"
        # Timestamps must be valid ISO 8601 strings.
        first = _parse_iso(alpha["first_seen"])
        last = _parse_iso(alpha["last_seen"])
        # On first registration, first_seen == last_seen.
        assert alpha["first_seen"] == alpha["last_seen"]
        assert first == last


class TestRegisterExistingWorkspace:
    """Re-registering a known workspace is idempotent on identity but updates
    ``last_seen`` and preserves the immutable ``first_seen``."""

    async def test_register_existing_workspace(self) -> None:
        registry = WorkspaceRegistry()

        await registry.register("beta")
        original = _by_name(await registry.list_workspaces(), "beta")
        original_first = original["first_seen"]
        original_last = original["last_seen"]

        # Yield to the loop so the next timestamp is strictly later.
        await asyncio.sleep(0.05)

        await registry.register("beta")
        entries = await registry.list_workspaces()

        # No duplicate entry — exactly one row per name.
        matches = [e for e in entries if e["name"] == "beta"]
        assert len(matches) == 1

        refreshed = matches[0]
        assert refreshed["first_seen"] == original_first, (
            "first_seen must be immutable across re-registration"
        )
        assert refreshed["last_seen"] != original_last, (
            "last_seen must be updated on re-registration"
        )
        # The new last_seen must be strictly later than the original.
        assert _parse_iso(refreshed["last_seen"]) > _parse_iso(original_last)


class TestListWorkspacesIncludesDefault:
    """The default workspace is always present in the listing, even before any
    explicit registration."""

    async def test_list_workspaces_includes_default(self) -> None:
        registry = WorkspaceRegistry(default_workspace="default")

        entries = await registry.list_workspaces()
        names = {entry["name"] for entry in entries}

        assert "default" in names


class TestRegistryPersistence:
    """Per W1, the registry is in-memory only — there is no persistence layer.

    This test verifies the contract that is in scope: operations on the SAME
    registry instance are consistent. Cross-instance persistence is explicitly
    deferred and is not exercised here.
    """

    async def test_registry_persistence(self) -> None:
        # NOTE: persistence across WorkspaceRegistry instances is intentionally
        # out of scope for W1. The registry is in-memory; a persistent backend
        # will be added later. We assert only single-instance consistency.
        registry = WorkspaceRegistry()
        await registry.register("gamma")

        # Two consecutive listings on the same instance agree.
        first_listing = await registry.list_workspaces()
        second_listing = await registry.list_workspaces()

        assert first_listing == second_listing
        assert any(entry["name"] == "gamma" for entry in first_listing)


class TestConcurrentRegistration:
    """Concurrent ``register`` calls for distinct workspaces must all succeed
    without dropping entries or raising."""

    async def test_concurrent_registration(self) -> None:
        registry = WorkspaceRegistry()
        names = [f"ws-{i}" for i in range(20)]

        # True concurrency — gather schedules every coroutine on the same
        # loop and they interleave at await points.
        await asyncio.gather(*(registry.register(n) for n in names))

        entries = await registry.list_workspaces()
        registered = {entry["name"] for entry in entries}

        for n in names:
            assert n in registered, f"Concurrent registration lost workspace {n!r}"

        # No duplicates — exactly one entry per registered name.
        assert len([e for e in entries if e["name"] in names]) == len(names)


class TestListWorkspacesFieldShape:
    """Every entry exposed by ``list_workspaces`` has a stable, documented
    shape: exactly the keys ``name``, ``first_seen``, ``last_seen``,
    ``document_count``. Timestamps are ISO 8601 strings; ``document_count``
    is ``None`` for v2."""

    async def test_list_workspaces_field_shape(self) -> None:
        registry = WorkspaceRegistry()
        await registry.register("delta")

        entries = await registry.list_workspaces()
        expected_keys = {"name", "first_seen", "last_seen", "document_count"}

        assert entries, "registry must contain at least the default workspace"

        for entry in entries:
            # Strict shape — no extra, no missing.
            assert set(entry.keys()) == expected_keys, (
                f"entry {entry!r} has unexpected keys "
                f"(extra={set(entry.keys()) - expected_keys}, "
                f"missing={expected_keys - set(entry.keys())})"
            )

            # Type discipline.
            assert isinstance(entry["name"], str)
            assert entry["document_count"] is None

            # Timestamps are valid ISO 8601 — both must parse without raising.
            _parse_iso(entry["first_seen"])
            _parse_iso(entry["last_seen"])
