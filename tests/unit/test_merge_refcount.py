"""Runtime refcount leak test for merge verification (B.3).

Verifies that WorkspaceManager ref_counts return to zero after
background tasks complete, catching use-after-release bugs (B.1)
that structural checks alone cannot detect.

Run: pytest tests/unit/test_merge_refcount.py -v
"""

import asyncio
import pytest
from lightrag.api.workspace_manager import WorkspaceManager

pytestmark = pytest.mark.offline


class MockLightRAG:
    """Mock LightRAG instance for testing."""

    def __init__(self, workspace: str) -> None:
        self.workspace = workspace
        self.finalize_called = False

    async def initialize_storages(self) -> None:
        pass

    async def finalize_storages(self) -> None:
        self.finalize_called = True


def mock_factory(workspace: str) -> MockLightRAG:
    return MockLightRAG(workspace)


class TestMergeRefcountSafety:
    """Verify refcount balance after bg-task lifecycle."""

    @pytest.fixture
    def manager(self):
        return WorkspaceManager(factory=mock_factory, max_instances=10)

    @pytest.mark.asyncio
    async def test_refcount_zero_after_bg_task_completes(self, manager):
        """After bg task completes, ref_count must be 0 (B.1 check).

        Simulates: handler acquires ref -> schedules bg task -> bg task
        independently acquires its own ref -> both release -> ref_count=0.
        """
        workspace = "test-ws"

        # --- Request handler scope ---
        _rag_handler = await manager.get_or_create(workspace)  # noqa: F841
        assert manager.get_stats()["ref_counts"]["test-ws"] == 1

        # --- Background task scope (independent ref) ---
        async def bg_task():
            _rag_bg = await manager.get_or_create(workspace)  # noqa: F841
            assert manager.get_stats()["ref_counts"]["test-ws"] == 2
            await asyncio.sleep(0.01)  # simulate processing
            manager.release(workspace)  # bg task releases its ref

        await bg_task()
        assert manager.get_stats()["ref_counts"]["test-ws"] == 1

        # --- Handler releases its ref ---
        manager.release(workspace)

        # --- Assert all refs released ---
        ref_counts = manager.get_stats()["ref_counts"]
        assert ref_counts.get("test-ws", 0) == 0, (
            f"Ref leak! ref_count={ref_counts.get('test-ws')} (expected 0)"
        )
        assert all(v == 0 for v in ref_counts.values()), f"Leaked refs: {ref_counts}"

    @pytest.mark.asyncio
    async def test_bg_task_ref_survives_handler_release(self, manager):
        """B.1 core: bg task's ref must keep workspace alive even after
        handler releases its ref.

        Simulates: handler acquires -> schedules bg task -> handler releases
        (ref_count=1) -> bg task still runs safely -> bg task releases
        (ref_count=0).
        """
        workspace = "test-ws-bg"

        # Handler acquires
        await manager.get_or_create(workspace)
        assert manager.get_stats()["ref_counts"]["test-ws-bg"] == 1

        # Schedule bg task
        bg_rag_valid = False

        async def bg_task():
            nonlocal bg_rag_valid
            _rag = await manager.get_or_create(workspace)  # noqa: F841 - +1 own ref
            assert manager.get_stats()["ref_counts"]["test-ws-bg"] == 2
            bg_rag_valid = True
            await asyncio.sleep(0.05)  # handler releases during this
            # Workspace should still be alive (ref_count >= 1)
            assert workspace in manager._cache, (
                "Workspace evicted while bg task still running!"
            )
            manager.release(workspace)

        # Start bg task
        task = asyncio.create_task(bg_task())
        await asyncio.sleep(0.01)  # let bg_task acquire

        # Handler releases its ref (ref_count drops to 1 - bg task's ref)
        manager.release(workspace)
        assert manager.get_stats()["ref_counts"]["test-ws-bg"] == 1

        # Wait for bg task to finish
        await task

        # Now ref_count should be 0
        assert manager.get_stats()["ref_counts"].get("test-ws-bg", 0) == 0
        assert bg_rag_valid
