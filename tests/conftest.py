"""Shared fixtures for all tests.

An autouse fixture patches ``get_cached_result`` and ``set_cached_result``
in the tools submodules that use them (``exec``, ``container``) so existing
and new tests are never accidentally affected by real cache data written to
``~/.code-sandbox-mcp/cache/`` by a previous test run.

Tests that need to verify cache behaviour can still override these mocks
by patching the same targets with custom return values (decorators or
context managers).
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _mock_result_cache() -> None:
    """Prevent all tests from reading/writing real cache data."""
    with (
        patch("code_sandbox_mcp.tools.exec.get_cached_result", return_value=None),
        patch("code_sandbox_mcp.tools.exec.set_cached_result"),
        patch("code_sandbox_mcp.tools.container.get_cached_result", return_value=None),
        patch("code_sandbox_mcp.tools.container.set_cached_result"),
    ):
        yield
