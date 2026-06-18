"""Cross-tenant isolation test for merge verification (B.8).

Verifies that workspace isolation prevents data leakage between
tenants: a document posted to workspace A must NOT be retrievable
from workspace B.

Uses shared_storage directly (no HTTP server) to test the namespace
isolation layer that the merge resolution preserves.

Run: pytest tests/unit/test_merge_cross_tenant.py -v
"""

import pytest

pytestmark = pytest.mark.offline

from lightrag.kg.shared_storage import (
    initialize_share_data,
    finalize_share_data,
    initialize_pipeline_status,
    get_namespace_data,
    get_final_namespace,
)


class TestMergeCrossTenantIsolation:
    """Verify pipeline_status and namespace data is isolated per workspace."""

    def setup_method(self):
        """Initialize shared storage before each test."""
        initialize_share_data()

    def teardown_method(self):
        """Clean up shared storage after each test."""
        finalize_share_data()

    @pytest.mark.asyncio
    async def test_pipeline_status_isolation(self):
        """Workspace A's pending_enqueues must not affect workspace B (Issue 2).

        This directly tests that slot accounting is workspace-keyed.
        """

        await initialize_pipeline_status("ws-a")
        await initialize_pipeline_status("ws-b")

        status_a = await get_namespace_data("pipeline_status", workspace="ws-a")
        status_b = await get_namespace_data("pipeline_status", workspace="ws-b")

        # Verify they are different objects (isolation)
        assert status_a is not status_b

        # Simulate slot reservation on ws-a
        status_a["pending_enqueues"] = 1
        status_a["busy"] = True

        # ws-b must be unaffected
        assert status_b.get("pending_enqueues", 0) == 0
        assert status_b.get("busy", False) is False

    @pytest.mark.asyncio
    async def test_namespace_key_isolation(self):
        """Verify the namespace key format ensures isolation."""
        key_a = get_final_namespace("pipeline_status", workspace="ws-a")
        key_b = get_final_namespace("pipeline_status", workspace="ws-b")
        assert key_a == "ws-a:pipeline_status"
        assert key_b == "ws-b:pipeline_status"
        assert key_a != key_b

    @pytest.mark.asyncio
    async def test_data_written_to_ws_a_not_in_ws_b(self):
        """Write data to ws-a namespace, verify ws-b cannot see it."""

        await initialize_pipeline_status("ws-a")
        await initialize_pipeline_status("ws-b")

        data_a = await get_namespace_data("pipeline_status", workspace="ws-a")

        canary = "CROSS_TENANT_CANARY_TOKEN_12345"
        data_a["canary"] = canary

        # Re-fetch to ensure isolation
        data_a_check = await get_namespace_data("pipeline_status", workspace="ws-a")
        data_b_check = await get_namespace_data("pipeline_status", workspace="ws-b")

        assert data_a_check.get("canary") == canary
        assert "canary" not in data_b_check, (
            "CROSS-TENANT LEAK: ws-b can see data from ws-a!"
        )
