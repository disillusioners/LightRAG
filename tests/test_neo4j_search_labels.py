#!/usr/bin/env python
"""
Unit tests for Neo4j search_labels() method.

Tests the fix where the CONTAINS fallback is triggered not only on exceptions,
but also when the fulltext index returns empty results (e.g., underscore-containing
entities like `reasoning_content` that get split by the CJK tokenizer).

These tests use mocked Neo4j driver and do not require a real Neo4j instance.
"""

import os
import sys
from unittest.mock import AsyncMock

import pytest

# Add the project root directory to the Python path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lightrag.kg.neo4j_impl import Neo4JStorage


# ----------------------------------------------------------------------
# Mock result classes for simulating Neo4j cursor behavior
# ----------------------------------------------------------------------


class MockResult:
    """Mock Neo4j result cursor that supports async iteration."""

    def __init__(self, records: list):
        """
        Args:
            records: List of dicts with keys matching the query's RETURN clause.
                     E.g., [{"label": "TestEntity"}, ...]
        """
        self._records = records

    def __aiter__(self):
        self._iter = iter(self._records)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration

    async def consume(self):
        """Consume remaining records (no-op for mock)."""
        pass


class MockSession:
    """Mock Neo4j session with configurable run() behavior."""

    def __init__(
        self, run_result: MockResult | None = None, run_error: Exception | None = None
    ):
        """
        Args:
            run_result: The MockResult to return from run()
            run_error: If set, run() will raise this exception instead of returning result
        """
        self._run_result = run_result
        self._run_error = run_error
        if run_error is None:
            self.run = AsyncMock(return_value=run_result)
        else:
            self._setup_error_run(run_error)

    def _setup_error_run(self, error):
        async def run_error_fn(*args, **kwargs):
            raise error

        self.run = run_error_fn


class SessionContextManager:
    """Simple async context manager that yields a mock session."""

    def __init__(self, session: MockSession):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False


class MockDriver:
    """
    Mock Neo4j driver that can be configured with multiple session behaviors.

    The session() method returns an async context manager directly (not a coroutine).
    This matches the Neo4j driver API.
    """

    def __init__(self):
        self._session_context_managers = []
        self._session_call_count = 0

    def set_session_context_managers(self, context_managers: list):
        """Set a list of session context managers to return on each call."""
        self._session_context_managers = list(context_managers)
        self._session_call_count = 0

    def session(self, database=None, default_access_mode=None):
        """
        Return an async context manager for a mock session.

        This method is NOT async - it returns the context manager directly.
        The caller will use: async with driver.session(...) as session:
        """
        if self._session_context_managers:
            ctx = self._session_context_managers[
                self._session_call_count % len(self._session_context_managers)
            ]
            self._session_call_count += 1
            return ctx
        # Default: return a session context manager with no results
        return SessionContextManager(MockSession(run_result=MockResult([])))

    @property
    def session_call_count(self):
        return self._session_call_count


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture
def mock_embedding_func():
    """Simple mock embedding function."""

    async def func(texts):
        return [[0.0] * 10 for _ in texts]

    return func


@pytest.fixture
def global_config():
    """Minimal global config for Neo4JStorage."""
    return {
        "embedding_batch_num": 10,
        "vector_db_storage_cls_kwargs": {"cosine_better_than_threshold": 0.5},
        "working_dir": "./rag_storage",
    }


def create_mock_storage(mock_embedding_func, global_config):
    """Create a Neo4JStorage instance with a mocked driver."""
    storage = Neo4JStorage(
        namespace="test_search_labels",
        workspace="test_workspace",
        global_config=global_config,
        embedding_func=mock_embedding_func,
    )
    # Replace driver with our mock
    storage._driver = MockDriver()
    storage._DATABASE = "test_db"
    return storage


def create_session_context_manager(result_records: list):
    """Create an async context manager that yields a mock session with given results."""
    session = MockSession(run_result=MockResult(result_records))
    return SessionContextManager(session)


def create_error_session_context_manager(error: Exception):
    """Create an async context manager that yields a session that raises on run()."""
    session = MockSession(run_error=error)
    return SessionContextManager(session)


def create_none_result_session_context_manager():
    """Create an async context manager that yields a session returning None from run()."""
    session = MockSession(run_result=None)
    return SessionContextManager(session)


# ----------------------------------------------------------------------
# Test 1: Fulltext returns results (NO fallthrough to CONTAINS)
# ----------------------------------------------------------------------


@pytest.mark.offline
async def test_fulltext_returns_results_no_fallthrough(
    mock_embedding_func, global_config
):
    """
    Test that when fulltext index returns results, they are returned immediately
    without falling through to the CONTAINS fallback.

    Verifies:
    - Returns ["TestEntity"] from fulltext
    - CONTAINS fallback is NOT called
    """
    storage = create_mock_storage(mock_embedding_func, global_config)

    # Set up: fulltext returns result, CONTAINS would return different result
    storage._driver.set_session_context_managers(
        [
            create_session_context_manager([{"label": "TestEntity"}]),
            create_session_context_manager([{"label": "FallbackEntity"}]),
        ]
    )

    # Execute
    labels = await storage.search_labels("Test", limit=50)

    # Verify fulltext results were returned
    assert labels == ["TestEntity"], f"Expected ['TestEntity'], got {labels}"

    # Verify only 1 session call was made (fulltext only, not CONTAINS)
    assert (
        storage._driver.session_call_count == 1
    ), f"Expected 1 session call (fulltext only), got {storage._driver.session_call_count}"


# ----------------------------------------------------------------------
# Test 2: Fulltext returns empty, CONTAINS finds match (THE BUG FIX)
# ----------------------------------------------------------------------


@pytest.mark.offline
async def test_fulltext_empty_triggers_contains_fallback(
    mock_embedding_func, global_config
):
    """
    Test the core bug fix: when fulltext index returns empty results,
    the CONTAINS fallback is triggered and returns matching labels.

    This handles entities like `reasoning_content` that get split by CJK tokenizer
    and don't match via full-text search.

    Verifies:
    - Fulltext returns empty list
    - CONTAINS fallback returns ["reasoning_content"]
    - Final result is ["reasoning_content"]
    """
    storage = create_mock_storage(mock_embedding_func, global_config)

    # Set up: fulltext returns empty, CONTAINS returns the underscore entity
    storage._driver.set_session_context_managers(
        [
            create_session_context_manager([]),
            create_session_context_manager([{"label": "reasoning_content"}]),
        ]
    )

    # Execute
    labels = await storage.search_labels("reasoning_content", limit=50)

    # Verify CONTAINS fallback results were returned
    assert labels == [
        "reasoning_content"
    ], f"Expected ['reasoning_content'], got {labels}"

    # Verify both sessions were called
    assert (
        storage._driver.session_call_count == 2
    ), f"Expected 2 session calls (fulltext + contains), got {storage._driver.session_call_count}"


# ----------------------------------------------------------------------
# Test 3: Fulltext throws exception, CONTAINS finds match
# ----------------------------------------------------------------------


@pytest.mark.offline
async def test_fulltext_exception_triggers_contains_fallback(
    mock_embedding_func, global_config
):
    """
    Test that when fulltext index throws an exception, the CONTAINS fallback
    is triggered and returns matching labels.

    Verifies:
    - Fulltext throws an exception
    - CONTAINS fallback returns ["FallbackEntity"]
    - Final result is ["FallbackEntity"]
    """
    storage = create_mock_storage(mock_embedding_func, global_config)

    # Set up: fulltext raises, CONTAINS returns result
    storage._driver.set_session_context_managers(
        [
            create_error_session_context_manager(
                RuntimeError("Fulltext index unavailable")
            ),
            create_session_context_manager([{"label": "FallbackEntity"}]),
        ]
    )

    # Execute
    labels = await storage.search_labels("Fallback", limit=50)

    # Verify CONTAINS fallback results were returned
    assert labels == ["FallbackEntity"], f"Expected ['FallbackEntity'], got {labels}"

    # Verify both sessions were called
    assert (
        storage._driver.session_call_count == 2
    ), f"Expected 2 session calls, got {storage._driver.session_call_count}"


# ----------------------------------------------------------------------
# Test 4: Both fulltext and CONTAINS return empty
# ----------------------------------------------------------------------


@pytest.mark.offline
async def test_both_fulltext_and_contains_return_empty(
    mock_embedding_func, global_config
):
    """
    Test that when both fulltext and CONTAINS return empty results,
    an empty list is returned.

    Verifies:
    - Both searches return no results
    - Final result is []
    """
    storage = create_mock_storage(mock_embedding_func, global_config)

    # Set up: both return empty
    storage._driver.set_session_context_managers(
        [
            create_session_context_manager([]),
            create_session_context_manager([]),
        ]
    )

    # Execute
    labels = await storage.search_labels("Nonexistent", limit=50)

    # Verify empty result
    assert labels == [], f"Expected [], got {labels}"

    # Verify both sessions were called
    assert (
        storage._driver.session_call_count == 2
    ), f"Expected 2 session calls, got {storage._driver.session_call_count}"


# ----------------------------------------------------------------------
# Test 5: Edge case - empty query string
# ----------------------------------------------------------------------


@pytest.mark.offline
async def test_empty_query_returns_immediately(mock_embedding_func, global_config):
    """
    Test that empty or whitespace-only queries return immediately
    without opening any database sessions.

    Verifies:
    - Empty string returns []
    - Whitespace-only string returns []
    - No database sessions are opened
    """
    storage = create_mock_storage(mock_embedding_func, global_config)

    # Execute with empty string
    labels_empty = await storage.search_labels("", limit=50)
    assert labels_empty == [], f"Expected [] for empty string, got {labels_empty}"

    # Execute with whitespace
    labels_whitespace = await storage.search_labels("   ", limit=50)
    assert (
        labels_whitespace == []
    ), f"Expected [] for whitespace, got {labels_whitespace}"

    # Verify no sessions were opened
    assert (
        storage._driver.session_call_count == 0
    ), f"Expected 0 session calls for empty query, got {storage._driver.session_call_count}"


# ----------------------------------------------------------------------
# Test 6: Edge case - session.run() returns None in fulltext
# ----------------------------------------------------------------------


@pytest.mark.offline
async def test_fulltext_run_returns_none_triggers_fallback(
    mock_embedding_func, global_config
):
    """
    Test that when session.run() returns None (error condition),
    the code falls to exception handler and then uses CONTAINS fallback.

    The async for loop over None would raise TypeError.

    Verifies:
    - Fulltext session.run() returns None
    - TypeError is caught
    - CONTAINS fallback returns results
    """
    storage = create_mock_storage(mock_embedding_func, global_config)

    # Set up: fulltext returns None from run(), CONTAINS returns result
    storage._driver.set_session_context_managers(
        [
            create_none_result_session_context_manager(),
            create_session_context_manager([{"label": "FallbackFromNone"}]),
        ]
    )

    # Execute
    labels = await storage.search_labels("Fallback", limit=50)

    # Verify CONTAINS fallback results were returned
    assert labels == [
        "FallbackFromNone"
    ], f"Expected ['FallbackFromNone'], got {labels}"

    # Verify both sessions were called
    assert (
        storage._driver.session_call_count == 2
    ), f"Expected 2 session calls, got {storage._driver.session_call_count}"


# ----------------------------------------------------------------------
# Additional Test: Multiple results from fulltext
# ----------------------------------------------------------------------


@pytest.mark.offline
async def test_fulltext_returns_multiple_results(mock_embedding_func, global_config):
    """
    Test that multiple results from fulltext are all returned.

    Verifies:
    - Returns all matched entities from fulltext
    - Results are in expected order (though order may vary)
    """
    storage = create_mock_storage(mock_embedding_func, global_config)

    # Set up: fulltext returns multiple results
    storage._driver.set_session_context_managers(
        [
            create_session_context_manager(
                [
                    {"label": "Machine Learning"},
                    {"label": "Deep Learning"},
                    {"label": "Transfer Learning"},
                ]
            ),
            create_session_context_manager([]),  # CONTAINS would be empty
        ]
    )

    # Execute
    labels = await storage.search_labels("Learning", limit=50)

    # Verify all results returned
    assert len(labels) == 3, f"Expected 3 results, got {len(labels)}"
    assert set(labels) == {"Machine Learning", "Deep Learning", "Transfer Learning"}

    # Only fulltext session should be called
    assert storage._driver.session_call_count == 1


# ----------------------------------------------------------------------
# Additional Test: Chinese text triggers correct query path
# ----------------------------------------------------------------------


@pytest.mark.offline
async def test_chinese_text_uses_correct_query_path(mock_embedding_func, global_config):
    """
    Test that Chinese text triggers the Chinese query path in fulltext search.

    Verifies:
    - Chinese text doesn't add wildcard suffix
    - Returns results from fulltext
    """
    storage = create_mock_storage(mock_embedding_func, global_config)

    # Set up: fulltext returns Chinese entity
    storage._driver.set_session_context_managers(
        [
            create_session_context_manager([{"label": "机器学习"}]),
            create_session_context_manager([]),  # CONTAINS would be empty
        ]
    )

    # Execute with Chinese text
    labels = await storage.search_labels("机器", limit=50)

    # Verify results
    assert labels == ["机器学习"], f"Expected ['机器学习'], got {labels}"

    # Fulltext session should be called
    assert storage._driver.session_call_count == 1


# ----------------------------------------------------------------------
# Additional Test: Chinese text fallback
# ----------------------------------------------------------------------


@pytest.mark.offline
async def test_chinese_text_fallback_to_contains(mock_embedding_func, global_config):
    """
    Test that Chinese text falls back to CONTAINS when fulltext returns empty.
    """
    storage = create_mock_storage(mock_embedding_func, global_config)

    # Set up: fulltext returns empty, CONTAINS returns Chinese entity
    storage._driver.set_session_context_managers(
        [
            create_session_context_manager([]),
            create_session_context_manager([{"label": "深度学习"}]),
        ]
    )

    # Execute with Chinese text
    labels = await storage.search_labels("深度", limit=50)

    # Verify CONTAINS fallback was used
    assert labels == ["深度学习"], f"Expected ['深度学习'], got {labels}"
    assert storage._driver.session_call_count == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
