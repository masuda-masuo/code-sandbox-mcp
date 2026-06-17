"""Tests for the notification module (Issue #44)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from code_sandbox_mcp.notify import (
    configure,
    notify_boundary_crossing,
    notify_failure_threshold,
    notify_general,
    notify_long_running,
)


class TestNotifyConfigure:
    """Tests for notification configuration."""

    def test_configure_defaults(self) -> None:
        configure()
        # Should not raise

    def test_configure_with_webhook(self) -> None:
        configure(webhook_url="https://example.com/webhook")
        # Should not raise

    def test_configure_with_custom_thresholds(self) -> None:
        configure(failure_threshold=10, long_run_seconds=600)
        # Should not raise


class TestNotify:
    """Tests for notification functions."""

    @patch("code_sandbox_mcp.notify._notify_os")
    @patch("code_sandbox_mcp.notify._notify_webhook")
    def test_notify_boundary_crossing(
        self,
        mock_webhook: MagicMock,
        mock_os: MagicMock,
    ) -> None:
        notify_boundary_crossing(
            operation="git_push",
            details="pushed to main",
            container_id="abc123",
        )
        mock_os.assert_called_once()
        mock_webhook.assert_called_once()

    @patch("code_sandbox_mcp.notify._notify_os")
    @patch("code_sandbox_mcp.notify._notify_webhook")
    def test_notify_failure_threshold(
        self,
        mock_webhook: MagicMock,
        mock_os: MagicMock,
    ) -> None:
        notify_failure_threshold(
            run_id="run1",
            failure_count=5,
            last_error="AssertionError: assert 1 == 2",
        )
        mock_os.assert_called_once()
        mock_webhook.assert_called_once()

    @patch("code_sandbox_mcp.notify._notify_os")
    @patch("code_sandbox_mcp.notify._notify_webhook")
    def test_notify_long_running(
        self,
        mock_webhook: MagicMock,
        mock_os: MagicMock,
    ) -> None:
        notify_long_running(
            run_id="run1",
            duration_seconds=360.0,
            container_id="abc123",
        )
        mock_os.assert_called_once()
        mock_webhook.assert_called_once()

    @patch("code_sandbox_mcp.notify._notify_os")
    @patch("code_sandbox_mcp.notify._notify_webhook")
    def test_notify_general(
        self,
        mock_webhook: MagicMock,
        mock_os: MagicMock,
    ) -> None:
        notify_general(
            title="Test Alert",
            message="This is a test notification.",
            event="test_event",
        )
        mock_os.assert_called_once()
        mock_webhook.assert_called_once()

    @patch("code_sandbox_mcp.notify._notify_os")
    @patch("code_sandbox_mcp.notify._notify_webhook")
    def test_notify_without_webhook_url(
        self,
        mock_webhook: MagicMock,
        mock_os: MagicMock,
    ) -> None:
        # Configure without webhook URL
        configure(webhook_url=None)
        # _notify_webhook should return False silently (no URL)
        notify_general("Test", "message")
        mock_os.assert_called_once()
        # webhook should still be called but return False internally
