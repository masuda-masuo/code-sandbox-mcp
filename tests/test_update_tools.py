"""Tests for the update MCP tools (sandbox_update_start / sandbox_update_check)."""
from __future__ import annotations

from code_sandbox_mcp.server import (
    _CURRENT_UPDATE_LOG_PATH,
    _UPDATE_SPEC,
    sandbox_update_check,
    sandbox_update_start,
)


class TestSandboxUpdateStart:
    """Tests for sandbox_update_start()."""

    def test_returns_job_id(self) -> None:
        result = sandbox_update_start()
        assert "Update started in background" in result
        assert "Log:" in result




class TestSandboxUpdateCheck:
    """Tests for sandbox_update_check()."""

    def test_no_job_returns_error(self, monkeypatch) -> None:
        import code_sandbox_mcp.server as srv
        monkeypatch.setattr(srv, "_CURRENT_UPDATE_LOG_PATH", None)
        result = sandbox_update_check()
        assert "no update job found" in result

    def test_log_not_found_returns_error(self, monkeypatch, tmp_path) -> None:
        import code_sandbox_mcp.server as srv
        nonexistent = str(tmp_path / "nonexistent" / "update.log")
        monkeypatch.setattr(srv, "_CURRENT_UPDATE_LOG_PATH", nonexistent)
        result = sandbox_update_check()
        assert "update log not found" in result

    def test_running_status(self, monkeypatch, tmp_path) -> None:
        import code_sandbox_mcp.server as srv
        log_path = tmp_path / "update.log"
        log_path.write_text(
            "=== Update started (spec: test) ===\n"
            "Collecting package...\n"
        )
        monkeypatch.setattr(srv, "_CURRENT_UPDATE_LOG_PATH", str(log_path))
        result = sandbox_update_check()
        assert "Status: running" in result

    def test_done_status(self, monkeypatch, tmp_path) -> None:
        import code_sandbox_mcp.server as srv
        log_path = tmp_path / "update.log"
        log_path.write_text(
            "=== Update started (spec: test) ===\n"
            "Installing...\n"
            "=== Update succeeded ===\n"
        )
        monkeypatch.setattr(srv, "_CURRENT_UPDATE_LOG_PATH", str(log_path))
        result = sandbox_update_check()
        assert "Status: done" in result

    def test_error_status(self, monkeypatch, tmp_path) -> None:
        import code_sandbox_mcp.server as srv
        log_path = tmp_path / "update.log"
        log_path.write_text(
            "=== Update started (spec: test) ===\n"
            "ERROR: Something went wrong\n"
            "=== Update failed (exit code: 1) ===\n"
        )
        monkeypatch.setattr(srv, "_CURRENT_UPDATE_LOG_PATH", str(log_path))
        result = sandbox_update_check()
        assert "Status: error" in result
        assert "exit code: 1" in result
class TestUpdateSpecDefault:
    """Tests for the update spec default value."""

    def test_default_update_spec_is_absolute_path(self) -> None:
        from pathlib import Path
        p = Path(_UPDATE_SPEC)
        assert p.is_absolute()
        assert p.is_dir()
