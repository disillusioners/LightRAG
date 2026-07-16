"""Sanitization alignment tests between backend and frontend.

Workspace isolation v2 requires the backend HTTP sanitizer and the frontend
header sanitization helper to produce the same canonical name for every
plausible user input, so that a workspace selected in the UI persists on
the server under the exact same key.

Two regexes in play:

  Backend  -- ``lightrag/api/utils_api.py`` ``get_workspace_from_request``
    workspace = request.headers.get("LIGHTRAG-WORKSPACE", "").strip()
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "_", workspace)
    # NOTE: backend does NOT truncate length.

  Frontend -- ``lightrag_webui/src/api/lightrag.ts`` ``sanitizeWorkspaceHeader``
    const sanitized = workspace.replace(/[^a-zA-Z0-9_-]/g, '_')
    return sanitized.substring(0, 64)
    # NOTE: frontend does NOT strip whitespace first, but DOES truncate to
    # 64 characters.

This module verifies, for every important input class, that the backend
yields the canonical form and that the two regexes agree on every
input that does not exceed 64 characters (the truncation boundary).

Import-time note
----------------
Importing :mod:`lightrag.api.utils_api` transitively imports
:mod:`lightrag.api.auth`, which constructs ``AuthHandler()`` at module
load. ``AuthHandler`` reads ``global_args.auth_accounts`` and forces
``parse_args()`` to run against ``sys.argv``. Under pytest that argv
contains test-path / flag arguments ``argparse`` doesn't recognize, so
we seed ``sys.argv`` to a safe value **before** the first
``lightrag.api`` import. The result is cached in ``_global_args``, so
subsequent tests are unaffected.
"""

from __future__ import annotations

import sys

# Seed sys.argv BEFORE importing any lightrag.api module. Idempotent:
# argparse caches the parsed result in _global_args, so subsequent tests
# aren't disturbed.
sys.argv = sys.argv[:1]

import re  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

import pytest  # noqa: E402
from fastapi import Request  # noqa: E402

from lightrag.api.utils_api import get_workspace_from_request  # noqa: E402
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
# Helpers
# ---------------------------------------------------------------------------


def _make_request(header_value):
    """Build a FastAPI ``Request``-shaped mock carrying a single header.

    We use :class:`unittest.mock.MagicMock` with ``spec=Request`` so any
    stray attribute access surfaces as a clear ``AttributeError`` rather
    than a silent ``MagicMock``-shaped default. ``headers`` is overridden
    with a real dict because ``get_workspace_from_request`` calls
    ``headers.get("LIGHTRAG-WORKSPACE", "")`` -- a plain dict lookup that
    a real ``Headers`` instance would also resolve identically.
    """
    req = MagicMock(spec=Request)
    req.headers = {"LIGHTRAG-WORKSPACE": header_value}
    return req


def _backend_regex_python(s: str) -> str:
    """Mirror of the backend regex (no truncation)."""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", s)


def _frontend_regex_python(s: str) -> str:
    """Mirror of the frontend regex (truncates to 64 chars)."""
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "_", s)
    return sanitized[:64]


# ---------------------------------------------------------------------------
# Direct backend tests
# ---------------------------------------------------------------------------


class TestBackendSanitization:
    """``get_workspace_from_request`` strips, sanitizes, and preserves
    hyphens / case / digits as the canonical alphabet."""

    def test_backend_accepts_hyphens(self) -> None:
        """``my-tenant`` must survive unchanged -- hyphens are in the allow list."""
        req = _make_request("my-tenant")

        result = get_workspace_from_request(req)

        assert result == "my-tenant"

    def test_backend_strips_invalid_chars(self) -> None:
        """Space and ``!`` are outside ``[a-zA-Z0-9_-]`` and become ``_``."""
        req = _make_request("my tenant!")

        result = get_workspace_from_request(req)

        assert result == "my_tenant_"

    def test_uppercase_preserved(self) -> None:
        """The regex is case-sensitive -- ``TenantA`` must not be lowercased."""
        req = _make_request("TenantA")

        result = get_workspace_from_request(req)

        assert result == "TenantA"


# ---------------------------------------------------------------------------
# Cross-language regex equivalence
# ---------------------------------------------------------------------------


class TestFrontendBackendRegexMatch:
    """The backend Python regex and the frontend JS regex must produce
    identical output for any input whose sanitized form fits within 64
    characters (the frontend's truncation boundary)."""

    @pytest.mark.parametrize(
        "raw",
        [
            "my-tenant",
            "my_tenant",
            "My-Tenant",
            "a.b",
            "a b",
            "café",
            "测试",
            "",
            "---",
            "a@b#c",
            "123-456",
            "alpha",
            "abc_123",
            "TenantA",
            "my tenant!",
        ],
    )
    def test_regex_outputs_match_within_64_chars(self, raw: str) -> None:
        backend = _backend_regex_python(raw)
        frontend = _frontend_regex_python(raw)

        assert backend == frontend, (
            f"backend={backend!r} frontend={frontend!r} input={raw!r}"
        )

    def test_truncation_divergence_documented(self) -> None:
        """Frontend truncates to 64 chars; backend does NOT.

        For an input whose sanitized form exceeds 64 chars the two
        implementations diverge. This is a known cross-language
        inconsistency: backend accepts arbitrarily long names, frontend
        silently caps them. Keep this test in lockstep with the
        ``sanitizeWorkspaceHeader`` truncation length.
        """
        long_input = "a" * 100

        backend_out = _backend_regex_python(long_input)
        frontend_out = _frontend_regex_python(long_input)

        # Backend does not truncate.
        assert len(backend_out) == 100
        assert backend_out == "a" * 100

        # Frontend truncates to 64 chars.
        assert len(frontend_out) == 64
        assert frontend_out == "a" * 64

        # And therefore the two diverge.
        assert backend_out != frontend_out


# ---------------------------------------------------------------------------
# End-to-end roundtrip
# ---------------------------------------------------------------------------


class TestRoundtripConsistency:
    """User selects ``my-tenant`` in the UI -> header sent -> backend
    receives ``my-tenant`` -> registered in :class:`WorkspaceRegistry`
    -> appears in :meth:`WorkspaceRegistry.list_workspaces` as
    ``my-tenant`` unchanged."""

    async def test_roundtrip_through_registry(self) -> None:
        registry = WorkspaceRegistry()
        user_selected_workspace = "my-tenant"

        # Step 1: frontend would send the LIGHTRAG-WORKSPACE header with
        # the user-selected name. Backend parses it via
        # get_workspace_from_request, which sanitizes.
        req = _make_request(user_selected_workspace)
        backend_canonical = get_workspace_from_request(req)
        assert backend_canonical == "my-tenant"

        # Step 2: backend registers the canonical name in the registry.
        await registry.register(backend_canonical)

        # Step 3: /workspaces endpoint lists the registry contents; the
        # entry's ``name`` must match what the user selected.
        entries = await registry.list_workspaces()
        names = {entry["name"] for entry in entries}

        assert "my-tenant" in names, (
            f"workspace {user_selected_workspace!r} did not survive the "
            f"roundtrip; got names={names!r}"
        )

    async def test_roundtrip_preserves_alphanumeric_with_hyphens(self) -> None:
        """Multiple realistic workspace names survive a full roundtrip."""
        registry = WorkspaceRegistry()

        candidates = [
            "default",
            "my-tenant",
            "team_alpha",
            "project-123",
            "TenantA",
            "a-b-c-d-e",
        ]

        for name in candidates:
            req = _make_request(name)
            assert get_workspace_from_request(req) == name
            await registry.register(name)

        listed_names = {entry["name"] for entry in await registry.list_workspaces()}
        for name in candidates:
            assert name in listed_names, (
                f"{name!r} missing from registry listing {listed_names!r}"
            )
