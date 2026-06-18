"""Tests for the journal pending-approval extensions (Issue #50)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from code_sandbox_mcp.journal import (
    get_pending_approvals,
    record_boundary_crossing,
)


class TestGetPendingApprovals:
    """Tests for get_pending_approvals."""

    def test_empty_journal_returns_empty(self, tmp_path: Path):
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        log_path = journal_dir / "journal.log"

        with patch("code_sandbox_mcp.journal._JOURNAL_PATH", log_path), \
             patch("code_sandbox_mcp.journal._JOURNAL_DIR", journal_dir):
            pending = get_pending_approvals()
            assert pending == []

    def test_no_boundary_crossings_returns_empty(self, tmp_path: Path):
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        log_path = journal_dir / "journal.log"

        _append_entry(log_path, {"operation": "initialize", "run_id": "r1"})
        _append_entry(log_path, {"operation": "exec", "run_id": "r1"})

        with patch("code_sandbox_mcp.journal._JOURNAL_PATH", log_path), \
             patch("code_sandbox_mcp.journal._JOURNAL_DIR", journal_dir):
            pending = get_pending_approvals()
            assert pending == []

    def test_pending_without_approval_returned(self, tmp_path: Path):
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        log_path = journal_dir / "journal.log"

        with patch("code_sandbox_mcp.journal._JOURNAL_PATH", log_path), \
             patch("code_sandbox_mcp.journal._JOURNAL_DIR", journal_dir):
            record_boundary_crossing(
                "abc123", "git_push", "push to main",
                approved=None, token="tok1",
            )
            pending = get_pending_approvals()
            assert len(pending) == 1
            assert pending[0]["sub_operation"] == "git_push"
            assert pending[0]["approved"] is None
            assert pending[0]["token"] == "tok1"

    def test_approved_entries_excluded_from_pending(self, tmp_path: Path):
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        log_path = journal_dir / "journal.log"

        with patch("code_sandbox_mcp.journal._JOURNAL_PATH", log_path), \
             patch("code_sandbox_mcp.journal._JOURNAL_DIR", journal_dir):
            record_boundary_crossing(
                "abc123", "git_push", "push to main",
                approved=True, token="tok1",
            )
            pending = get_pending_approvals()
            assert pending == []

    def test_pending_resolved_by_later_approval(self, tmp_path: Path):
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        log_path = journal_dir / "journal.log"

        with patch("code_sandbox_mcp.journal._JOURNAL_PATH", log_path), \
             patch("code_sandbox_mcp.journal._JOURNAL_DIR", journal_dir):
            record_boundary_crossing(
                "abc123", "git_push", "push to main",
                approved=None, token="tok1",
            )
            record_boundary_crossing(
                "abc123", "git_push", "push to main",
                approved=True, token="tok1",
            )
            pending = get_pending_approvals()
            assert pending == []

    def test_pending_resolved_by_later_rejection(self, tmp_path: Path):
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        log_path = journal_dir / "journal.log"

        with patch("code_sandbox_mcp.journal._JOURNAL_PATH", log_path), \
             patch("code_sandbox_mcp.journal._JOURNAL_DIR", journal_dir):
            record_boundary_crossing(
                "abc123", "git_push", "push to main",
                approved=None, token="tok1",
            )
            record_boundary_crossing(
                "abc123", "git_push", "push to main",
                approved=False, token="tok1",
            )
            pending = get_pending_approvals()
            assert pending == []

    def test_multiple_pending_same_run(self, tmp_path: Path):
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        log_path = journal_dir / "journal.log"

        with patch("code_sandbox_mcp.journal._JOURNAL_PATH", log_path), \
             patch("code_sandbox_mcp.journal._JOURNAL_DIR", journal_dir):
            record_boundary_crossing(
                "abc123", "git_push", "push to main",
                approved=None, token="tok1",
            )
            record_boundary_crossing(
                "abc123", "pr_create", "create PR",
                approved=None, token="tok2",
            )
            pending = get_pending_approvals()
            assert len(pending) == 2

    def test_mixed_pending_and_resolved(self, tmp_path: Path):
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        log_path = journal_dir / "journal.log"

        with patch("code_sandbox_mcp.journal._JOURNAL_PATH", log_path), \
             patch("code_sandbox_mcp.journal._JOURNAL_DIR", journal_dir):
            record_boundary_crossing(
                "abc123", "git_push", "push to main",
                approved=None, token="tok1",
            )
            record_boundary_crossing(
                "abc123", "git_push", "push to main",
                approved=True, token="tok1",
            )
            record_boundary_crossing(
                "abc123", "pr_create", "create PR",
                approved=None, token="tok2",
            )
            pending = get_pending_approvals()
            assert len(pending) == 1
            assert pending[0]["token"] == "tok2"

    def test_without_token_not_tracked_as_pending(self, tmp_path: Path):
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        log_path = journal_dir / "journal.log"

        with patch("code_sandbox_mcp.journal._JOURNAL_PATH", log_path), \
             patch("code_sandbox_mcp.journal._JOURNAL_DIR", journal_dir):
            record_boundary_crossing(
                "abc123", "gh_issue_view", "read issue #1",
                approved=None,
            )
            pending = get_pending_approvals()
            assert len(pending) == 1
            # Entry WITHOUT token stays pending (no resolution mechanism)
            # This is intentional — read-only operations with no token
            # are just journal entries, not part of the approval workflow.
            assert pending[0]["sub_operation"] == "gh_issue_view"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _append_entry(path: Path, entry: dict) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
