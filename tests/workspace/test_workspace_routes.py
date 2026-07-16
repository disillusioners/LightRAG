"""Tests for ``GET /workspaces`` endpoint in ``lightrag.api.routers.workspace_routes``.

The router is mounted via the factory ``create_workspace_routes(workspace_mgr, api_key)``
and exposes a single endpoint ``GET /workspaces`` returning a ``WorkspacesResponse``
with the list of registered workspaces plus the default workspace name.

These tests exercise the route via FastAPI's ``TestClient`` against a minimal app
that only mounts the workspace routes. The fake manager wraps a real
``WorkspaceRegistry`` (pure in-memory, no external deps) so the data shape
matches production. No real LightRAG, no storage backends.

Import-time note
----------------
Importing :mod:`lightrag.api.routers.workspace_routes` transitively triggers
``lightrag.api.auth.AuthHandler()`` at module load, which accesses
``global_args.auth_accounts`` and forces ``parse_args()`` to run against
``sys.argv``. Under pytest that argv contains the test-path / flag arguments
``argparse`` doesn't recognize, so we have to seed ``sys.argv`` with a
``lightrag-server``-shaped argv **before** the import statement runs. The
result is cached in ``_global_args``, so subsequent tests are unaffected.
"""

from __future__ import annotations

import sys

# Seed sys.argv BEFORE importing the router. Must come before any lightrag.api
# import that transitively touches auth.py. Idempotent: argparse caches the
# parsed result in _global_args, so subsequent tests aren't disturbed.
sys.argv = ["lightrag-server"]

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lightrag.api.routers.workspace_routes import create_workspace_routes
from lightrag.api.workspace_registry import WorkspaceRegistry

pytestmark = pytest.mark.offline


# ---------------------------------------------------------------------------
# Hermetic env isolation
# ---------------------------------------------------------------------------
#
# The combined-auth dependency inside ``lightrag.api.utils_api`` reads
# module-level flags (``auth_configured`` and ``whitelist_patterns``) at request
# time. To keep these tests deterministic regardless of the developer's local
# ``.env`` (which may set ``AUTH_ACCOUNTS`` or whitelist ``/workspaces``), we
# force a known-empty auth surface: no accounts configured, no whitelisted
# paths. The ``api_key`` parameter is supplied per-test as needed.

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

    # Belt-and-suspenders: clear any accounts leaked into the singleton handler
    # AND force the module-level flag that the dependency reads.
    monkeypatch.setattr(auth_mod.auth_handler, "accounts", {})
    monkeypatch.setattr(utils_api, "auth_configured", False)
    monkeypatch.setattr(utils_api, "whitelist_patterns", [])
    yield


# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------


class FakeWorkspaceMgr:
    """Minimal duck-typed manager the router accepts.

    The router calls ``list_workspaces()`` (async) and
    ``get_default_workspace()`` (sync) on the manager. We forward both to a
    real ``WorkspaceRegistry`` so the data shape matches production exactly
    while still keeping the test layer dependency-free of ``WorkspaceManager``.
    """

    def __init__(self, registry: WorkspaceRegistry) -> None:
        self._registry = registry

    async def list_workspaces(self) -> list[dict]:
        return await self._registry.list_workspaces()

    def get_default_workspace(self) -> str:
        return self._registry.default_workspace


def _build_app(
    api_key: str | None = None,
    default_workspace: str = "default",
) -> tuple[FastAPI, FakeWorkspaceMgr, WorkspaceRegistry]:
    """Build a minimal FastAPI app with the workspace routes mounted.

    Returns the app plus the fake manager and the underlying registry so tests
    can register additional workspaces before exercising the endpoint.
    """
    registry = WorkspaceRegistry(default_workspace=default_workspace)
    mgr = FakeWorkspaceMgr(registry)
    app = FastAPI()
    app.include_router(create_workspace_routes(mgr, api_key=api_key))
    return app, mgr, registry


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestListWorkspacesEmpty:
    """A fresh manager seeds only the default workspace → GET /workspaces
    returns a list of length exactly 1."""

    async def test_list_workspaces_empty(self) -> None:
        app, _mgr, registry = _build_app()

        # Sanity: the registry only has the default workspace pre-seeded.
        seeded = await registry.list_workspaces()
        assert len(seeded) == 1
        assert seeded[0]["name"] == "default"

        client = TestClient(app)
        resp = client.get("/workspaces")

        assert resp.status_code == 200
        body = resp.json()
        assert "workspaces" in body
        assert isinstance(body["workspaces"], list)
        assert len(body["workspaces"]) == 1
        assert body["workspaces"][0]["name"] == "default"


class TestListWorkspacesAfterInsert:
    """After registering a new workspace, GET /workspaces includes it alongside
    the default."""

    async def test_list_workspaces_after_insert(self) -> None:
        app, _mgr, registry = _build_app()
        await registry.register("alpha")

        client = TestClient(app)
        resp = client.get("/workspaces")

        assert resp.status_code == 200
        names = {entry["name"] for entry in resp.json()["workspaces"]}
        assert names == {"default", "alpha"}, (
            f"Expected default and alpha, got {sorted(names)!r}"
        )


class TestWorkspacesResponseShape:
    """Response matches the WorkspacesResponse contract: a top-level object
    with exactly the keys ``workspaces`` (list) and ``default_workspace`` (str).
    """

    def test_workspaces_response_shape(self) -> None:
        app, _mgr, _registry = _build_app()
        client = TestClient(app)
        resp = client.get("/workspaces")

        assert resp.status_code == 200
        body = resp.json()

        # Top-level keys are exactly the documented two — no more, no less.
        assert set(body.keys()) == {"workspaces", "default_workspace"}, (
            f"Unexpected top-level keys: {sorted(body.keys())!r}"
        )
        assert isinstance(body["workspaces"], list)
        assert isinstance(body["default_workspace"], str)
        # The default_workspace field is non-empty: clients rely on it as a
        # fallback when workspaces is empty (per the handler docstring).
        assert body["default_workspace"] == "default"


class TestWorkspacesIncludesMetadata:
    """Each workspace entry exposes the documented field set with the expected
    scalar types."""

    async def test_workspaces_includes_metadata(self) -> None:
        app, _mgr, registry = _build_app()
        await registry.register("alpha")
        await registry.register("beta")

        client = TestClient(app)
        resp = client.get("/workspaces")

        assert resp.status_code == 200
        entries = resp.json()["workspaces"]
        assert entries, "registry must contain at least the default workspace"

        expected_keys = {"name", "first_seen", "last_seen", "document_count"}
        for entry in entries:
            assert set(entry.keys()) == expected_keys, (
                f"entry {entry!r} has unexpected key set "
                f"(got {sorted(entry.keys())!r}, expected {sorted(expected_keys)!r})"
            )
            assert isinstance(entry["name"], str)
            assert isinstance(entry["first_seen"], str)
            assert isinstance(entry["last_seen"], str)
            # document_count is None in v2 — must be null or int (forward
            # compat for the later workstream that computes it).
            assert entry["document_count"] is None or isinstance(
                entry["document_count"], int
            )


class TestWorkspacesAuth:
    """Verify the auth dependency is applied to GET /workspaces.

    When ``api_key`` is set on the router, a request without the ``X-API-Key``
    header is rejected (401/403); a request with the correct header passes.
    """

    def test_workspaces_auth(self) -> None:
        app, _mgr, _registry = _build_app(api_key="secret")
        client = TestClient(app)

        # Without API key → rejected.
        resp_no_key = client.get("/workspaces")
        assert resp_no_key.status_code in (401, 403), (
            f"Expected 401/403 without API key, got {resp_no_key.status_code}: "
            f"{resp_no_key.text!r}"
        )

        # With API key → success.
        resp_with_key = client.get("/workspaces", headers={"X-API-Key": "secret"})
        assert resp_with_key.status_code == 200, (
            f"Expected 200 with correct API key, got {resp_with_key.status_code}: "
            f"{resp_with_key.text!r}"
        )
        assert "workspaces" in resp_with_key.json()


class TestDocumentCountNullable:
    """The registry returns ``document_count=None`` for every workspace in v2
    (computing it is deferred to a later workstream). The endpoint must surface
    this as JSON ``null``.
    """

    async def test_document_count_nullable(self) -> None:
        app, _mgr, registry = _build_app()
        await registry.register("gamma")

        client = TestClient(app)
        resp = client.get("/workspaces")

        assert resp.status_code == 200
        entries = resp.json()["workspaces"]
        assert entries, "registry must contain at least the default workspace"

        for entry in entries:
            assert "document_count" in entry, (
                f"workspace {entry.get('name')!r} missing document_count field"
            )
            assert entry["document_count"] is None, (
                f"Expected document_count=null in v2, got "
                f"{entry['document_count']!r} for workspace {entry['name']!r}"
            )
