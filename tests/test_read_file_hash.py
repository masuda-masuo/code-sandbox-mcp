"""Opt-in conditional reads always observe the live file and capture guard."""
import hashlib
import json
from unittest.mock import MagicMock, patch

import pytest

from sunaba.tools.file import read_file_range


@pytest.fixture
def live_read():
    client = MagicMock()
    container = client.containers.get.return_value
    with (
        patch("sunaba.tools.file._docker", return_value=client),
        patch("sunaba.tools.file.record_tool_use"),
        patch("sunaba.tools.file.read_file_lines") as read,
        patch("sunaba.tools.file._gate_read_result", return_value=None) as guard,
    ):
        read.return_value = {
            "content": "hello\n世界", "total_lines": 2, "shown": 2,
            "has_more": False, "next_offset": None, "error": None,
        }
        yield read, guard, container


def call(**kwargs):
    return json.loads(read_file_range("abc123def456", "/workspace/f.txt", **kwargs))


def test_default_response_unchanged(live_read):
    read, _, _ = live_read
    assert call() == read.return_value


def test_matching_hash_omits_content_but_reads_and_checks_again(live_read):
    read, guard, container = live_read
    initial = call(if_content_hash="")
    assert initial["content_hash"] == hashlib.sha256("hello\n世界".encode()).hexdigest()
    result = call(if_content_hash=initial["content_hash"])
    assert "content" not in result
    assert result["not_modified"] is True
    assert result["content_hash"] == initial["content_hash"]
    assert result["total_lines"] == 2
    assert read.call_count == guard.call_count == 2
    read.assert_called_with(container, "/workspace/f.txt", offset=0, limit=50)


def test_changed_content_returns_new_body_and_hash(live_read):
    read, _, _ = live_read
    initial = call(if_content_hash="")
    read.return_value = {**read.return_value, "content": "changed"}
    result = call(if_content_hash=initial["content_hash"])
    assert result["content"] == "changed"
    assert result["content_hash"] != initial["content_hash"]
    assert "not_modified" not in result


@pytest.mark.parametrize("kwargs", [{"offset": 10, "limit": 2}, {"start_line": 11, "end_line": 12}, {"tail_lines": 2}])
def test_hash_describes_returned_text_and_preserves_live_metadata(live_read, kwargs):
    read, _, _ = live_read
    initial = call(if_content_hash="", **kwargs)
    # Content outside this window can change without requiring the body again.
    read.return_value = {**read.return_value, "total_lines": 50}
    result = call(if_content_hash=initial["content_hash"], **kwargs)
    assert result["not_modified"] is True
    assert result["total_lines"] == 50


@pytest.mark.parametrize("kwargs", [{}, {"tail_lines": 2}])
def test_deleted_or_unreadable_file_does_not_return_not_modified(live_read, kwargs):
    read, _, _ = live_read
    validator = call(if_content_hash="")["content_hash"]
    read.return_value = {"error": "No such file"}
    assert call(if_content_hash=validator, **kwargs) == {"error": "No such file"}


def test_guard_failure_is_never_suppressed(live_read):
    _, guard, _ = live_read
    validator = call(if_content_hash="")["content_hash"]
    guard.return_value = '{"error": "capture unhealthy"}'
    assert call(if_content_hash=validator) == {"error": "capture unhealthy"}


def test_empty_content_is_valid_and_missing_context_refetches(live_read):
    read, _, _ = live_read
    read.return_value = {**read.return_value, "content": ""}
    initial = call(if_content_hash="")
    assert call(if_content_hash=initial["content_hash"])["not_modified"] is True
    assert call()["content"] == ""
