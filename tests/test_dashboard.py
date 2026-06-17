"""Tests for the dashboard module (Issue #44)."""
from __future__ import annotations

import json

from code_sandbox_mcp.dashboard import start_dashboard, stop_dashboard


class TestDashboard:
    """Tests for dashboard start/stop."""

    def test_start_stop_dashboard(self):
        """Start and stop the dashboard."""
        result = start_dashboard(host="127.0.0.1", port=18766)
        assert "started on" in result

        result2 = start_dashboard(host="127.0.0.1", port=18766)
        assert "already running" in result2

        result3 = stop_dashboard()
        assert "stopped" in result3

    def test_stop_when_not_running(self):
        """Stopping when not running should indicate so."""
        stop_dashboard()  # ensure stopped
        result = stop_dashboard()
        assert "not running" in result

    def test_dashboard_serves_html(self):
        """Dashboard should serve HTML content on /."""
        import urllib.request

        start_dashboard(host="127.0.0.1", port=18767)
        try:
            with urllib.request.urlopen("http://127.0.0.1:18767/") as resp:
                assert resp.status == 200
                content = resp.read().decode("utf-8")
                assert "Code Sandbox MCP" in content
                assert "Dashboard" in content
        finally:
            stop_dashboard()

    def test_dashboard_api_runs(self):
        """Dashboard /api/runs should return JSON."""
        import urllib.request

        start_dashboard(host="127.0.0.1", port=18768)
        try:
            with urllib.request.urlopen("http://127.0.0.1:18768/api/runs") as resp:
                assert resp.status == 200
                data = json.loads(resp.read().decode("utf-8"))
                assert isinstance(data, list)
        finally:
            stop_dashboard()

    def test_dashboard_api_journal(self):
        """Dashboard /api/journal should return JSON array."""
        import urllib.request

        start_dashboard(host="127.0.0.1", port=18769)
        try:
            with urllib.request.urlopen("http://127.0.0.1:18769/api/journal") as resp:
                assert resp.status == 200
                data = json.loads(resp.read().decode("utf-8"))
                assert isinstance(data, list)
        finally:
            stop_dashboard()

    def test_dashboard_404(self):
        """Dashboard should return 404 for unknown paths."""
        import urllib.request
        import urllib.error

        start_dashboard(host="127.0.0.1", port=18770)
        try:
            urllib.request.urlopen("http://127.0.0.1:18770/nonexistent")
        except urllib.error.HTTPError as e:
            assert e.code == 404
        finally:
            stop_dashboard()

    def test_dashboard_trace_page(self):
        """Dashboard /trace/<run_id> should return HTML or 404."""
        import urllib.request
        import urllib.error

        start_dashboard(host="127.0.0.1", port=18771)
        try:
            urllib.request.urlopen("http://127.0.0.1:18771/trace/nonexistent")
        except urllib.error.HTTPError as e:
            assert e.code == 404
        finally:
            stop_dashboard()
